"""What `/swing/journal` reads — the book's results in R (SW8, `docs/swing/05` §2).

    "The journal tells him, in R, whether he should be pressing or sitting."

Four things on one page, and each answers a different question:

* **Two cards, real and simulated, never mixed.** `04` §10: "Simulated fills are summarised
  separately from real ones on the page." A paper streak and a real streak are different facts
  about different money, and a card that averaged them would be a number nobody could act on.
* **The ladder**, as it stands tonight: the rung in force (`sw_config.exposure_level`, the
  number `swing-eod` wrote), the gate and the tier the detectors measured, and the last R
  values the ladder read — so a person can see *why* the rung is what it is.
* **The session count** against the twenty-session paper gate (`02` §3.2): "14 of 20 paper
  sessions logged".
* **The backtest card** (SW9): the latest **finished** run in `sw_backtest_run` — `04` §11's
  numbers with its caveats verbatim — and ``None`` until one exists, which the page says.

The arithmetic is `baskfy_core.swing.journal`'s. This module reads closed positions, hands them
to `summarize`, and shapes the result. Like `baskfy_api.swing`, it is **read-only,
structurally**: no INSERT, UPDATE or DELETE, and `services/api/tests/test_swing_readonly.py`
scans it for one.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import Instrument, SwConfig, SwMarketDaily, SwPosition, SwSession
from baskfy_core.models.base import JsonObject
from baskfy_core.models.swing import SwBacktestRun
from baskfy_core.swing.backtest import CAVEATS, INDEX_ABSENT_CAVEAT, GateMode
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG, SwingConfig
from baskfy_core.swing.journal import ClosedTrade, JournalStats, summarize

#: The session counter's denominator — "14 of 20 sessions logged" on `/swing/journal` and in
#: the evening email. **Information, not a gate** since Maulik rewrote `docs/swing/02` §3
#: (STANDING-ANSWERS A11): the real-money flag is gated on one DRY_RUN drill morning, the
#: backtest on the page and his written risk decision, and is flipped by his hand. Defined here,
#: in the API package, because the worker depends on the API and not the other way round; the
#: EOD job's email and this page count against the same number.
PAPER_SESSIONS_REQUIRED: Final = 20

#: The R-distribution buckets, in the order the page draws them. The labels are the contract
#: (C2): a bar takes every trade from its lower bound up to but not including the next one, and
#: the two tails mean what they say — a 1R loss (`-1.00`, the commonest loss there is: a stop hit
#: exactly) sits in `-1..0`, not under `<-1`; a `3.00` sits in `2..3`, not under `>3`.
HISTOGRAM_BUCKETS: Final[tuple[str, ...]] = ("<-1", "-1..0", "0..1", "1..2", "2..3", ">3")

#: The page lists the most recent trades and says how many there are; two hundred is more than
#: a year of his own trading, and a longer list is a CSV export rather than a page.
MAX_TRADES: Final = 200

#: What the ladder card says when there is no `sw_market_daily` row at all: the detectors have
#: never run, no gate has been measured, and — because the EOD job plans nothing without one —
#: no entry is allowed.
GATE_UNKNOWN: Final = "UNKNOWN"

_ZERO = Decimal(0)
_TWO_DP = Decimal("0.01")
#: A stored equity-curve point is ``[date, equity]``.
_CURVE_POINT_ARITY: Final = 2


@dataclass(frozen=True, slots=True)
class TradeRow:
    """One closed trade as the page lists it — the **stored** numbers, not recomputed ones.

    `r_multiple` and `pnl_inr` are what was written at close (`03` §7: "written at close from
    `journal.ClosedTrade`"). House rule 8 makes the stored number the record; the page shows it.
    """

    symbol: str
    setup: str
    entry_date: dt.date
    exit_date: dt.date
    entry: Decimal
    initial_stop: Decimal
    exit_avg: Decimal
    quantity: int
    r_multiple: Decimal
    pnl_inr: Decimal
    close_reason: str | None


@dataclass(frozen=True, slots=True)
class HistogramBar:
    bucket: str
    count: int


@dataclass(frozen=True, slots=True)
class SetupStats:
    setup: str
    trades: int
    net_r: Decimal
    expectancy_r: Decimal


@dataclass(frozen=True, slots=True)
class MonthStats:
    #: ``YYYY-MM`` of the exit date — a trade belongs to the month it was closed in, because that
    #: is the month its R became a fact.
    month: str
    trades: int
    net_r: Decimal


@dataclass(frozen=True, slots=True)
class JournalCard:
    """One book's results: the statistics, the distribution, the two groupings, the list."""

    stats: JournalStats
    histogram: tuple[HistogramBar, ...]
    by_setup: tuple[SetupStats, ...]
    by_month: tuple[MonthStats, ...]
    #: Newest first, at most :data:`MAX_TRADES`.
    trades: tuple[TradeRow, ...]


@dataclass(frozen=True, slots=True)
class SessionsCard:
    logged: int
    required: int


@dataclass(frozen=True, slots=True)
class LadderCard:
    """The rung in force and what it was computed from.

    ``level`` is `sw_config.exposure_level` — the number the EOD job writes back and the plan is
    built with — rather than the market row's copy, so the card shows the loop closed rather than
    one end of it. ``reads`` names the book the ladder is reading: always the real one since
    SW10.5 (STANDING-ANSWERS A10 — the ladder reads real closes from day one, PACK.6's paper
    clause is void). The ``SIMULATED`` literal stays in the type for the wire contract; nothing
    writes it any more (SW11).
    """

    level: int
    gate: str
    max_open_positions: int
    max_exposure_pct: Decimal
    new_entries_allowed: bool
    #: The last `MarketConfig.lookback_trades` closes of the book the ladder reads, oldest first
    #: — the five numbers the rung is explained by.
    last_r: tuple[Decimal, ...]
    reads: Literal["SIMULATED", "REAL"]


@dataclass(frozen=True, slots=True)
class BacktestCard:
    """SW9's card: the latest finished run, in contract C2's shape.

    ``stats`` is **not** the stored JSON: the row keeps ``BacktestResult.to_json()`` whole (the
    trade list, the equity curve, the ladder trace), and a card is what a page can read. The
    headline `04` §10 statistics sit at the top level, the R distribution is the journal's own
    six buckets (:data:`HISTOGRAM_BUCKETS`, so the page draws it with the same component the
    two journal cards use), ``by_setup`` and ``by_year`` are §10's statistics per group,
    ``funnel`` is the engine's counts, and ``equity`` summarises the curve. Every stored decimal
    string is a ``Decimal`` here, so it reaches the wire as a number with its precision
    (DECISIONS-SW SW9.7). SW9.6 adds ``max_drawdown_pct`` at the top level, ``drawdown`` (the
    constant-sleeve curve's deepest peak-to-trough and how long the lock-out held) and
    ``comparison`` — gate-off, breadth-only and full side by side, overall, by year entered and
    by setup, with breadth's and the index rule's contributions (STANDING-ANSWERS A12).
    """

    run_id: int
    params: JsonObject
    started_at: str
    finished_at: str | None
    stats: JsonObject
    #: `04` §11's sentences, verbatim, from the engine's own constant — never from the row, so a
    #: run stored under an older wording still shows the sentences the document has today — plus
    #: the index-absent sentence when the stored run says its gate was breadth-only (SW9.6).
    caveats: tuple[str, ...]

    def as_json(self) -> JsonObject:
        return {
            "run_id": self.run_id,
            "params": self.params,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stats": self.stats,
            "caveats": list(self.caveats),
        }


@dataclass(frozen=True, slots=True)
class JournalView:
    real: JournalCard
    simulated: JournalCard
    sessions: SessionsCard
    ladder: LadderCard
    #: SW9's card as C2's JSON object (``BacktestCard.as_json()``), or ``None`` until a finished
    #: run exists — the page renders "not run yet". A plain object rather than the dataclass
    #: because the route hands it to ``SwingBacktestCardOut`` as-is, and pydantic validates a
    #: mapping into a model where it would refuse a foreign dataclass.
    backtest: JsonObject | None


def bucket_of(r: Decimal) -> str:
    """Which histogram bar an R-multiple belongs to. See :data:`HISTOGRAM_BUCKETS`."""
    if r < Decimal(-1):
        return HISTOGRAM_BUCKETS[0]
    if r < _ZERO:
        return HISTOGRAM_BUCKETS[1]
    if r < Decimal(1):
        return HISTOGRAM_BUCKETS[2]
    if r < Decimal(2):
        return HISTOGRAM_BUCKETS[3]
    if r <= Decimal(3):
        return HISTOGRAM_BUCKETS[4]
    return HISTOGRAM_BUCKETS[5]


def _r_values(rows: Sequence[TradeRow]) -> list[Decimal]:
    return [row.r_multiple for row in rows]


def card(trades: Sequence[ClosedTrade], rows: Sequence[TradeRow]) -> JournalCard:
    """Shape one book. ``trades`` and ``rows`` describe the same closes, newest first.

    The statistics come from `summarize` over :class:`ClosedTrade` — the same function the
    backtest reports through, so the paper card and the backtest card cannot disagree about what
    expectancy is. The groupings and the histogram read the stored R on each row, which is the
    number the ladder read; the two agree whenever the close was written through `ClosedTrade`,
    which is the only way `03` §7 admits. Empty input is a card of zeros, not an error.
    """
    # `summarize` computes the loss streak from the *end* of the sequence, so it is handed the
    # trades oldest first — the order they happened in.
    stats = summarize(list(reversed(trades)))
    counts = Counter(bucket_of(r) for r in _r_values(rows))
    histogram = tuple(HistogramBar(bucket, counts.get(bucket, 0)) for bucket in HISTOGRAM_BUCKETS)

    by_setup: dict[str, list[Decimal]] = {}
    by_month: dict[str, list[Decimal]] = {}
    for row in rows:
        by_setup.setdefault(row.setup, []).append(row.r_multiple)
        by_month.setdefault(row.exit_date.strftime("%Y-%m"), []).append(row.r_multiple)
    return JournalCard(
        stats=stats,
        histogram=histogram,
        by_setup=tuple(
            SetupStats(
                setup=setup,
                trades=len(values),
                net_r=sum(values, _ZERO).quantize(_TWO_DP),
                expectancy_r=(sum(values, _ZERO) / len(values)).quantize(_TWO_DP),
            )
            for setup, values in sorted(by_setup.items())
        ),
        by_month=tuple(
            MonthStats(month=month, trades=len(values), net_r=sum(values, _ZERO).quantize(_TWO_DP))
            for month, values in sorted(by_month.items())
        ),
        trades=tuple(rows[:MAX_TRADES]),
    )


async def closed_trades(
    session: AsyncSession, *, user_id: int, simulated: bool
) -> tuple[list[ClosedTrade], list[TradeRow]]:
    """One book's closed trades, newest first.

    A closed trade is a `sw_position` row in state ``CLOSED`` **with** an `r_multiple`, an
    `exit_avg` and a `closed_on`. A row that is ``CLOSED`` without them is a position whose
    close-out never finished writing (`06` SW8: "when `quantity_open` hits 0, write `r_multiple`,
    `pnl_inr`, `close_reason`"), and counting it would put a trade with no result into the
    statistics as a zero.
    """
    result = await session.execute(
        select(SwPosition, Instrument.symbol)
        .join(Instrument, Instrument.id == SwPosition.instrument_id)
        .where(
            SwPosition.user_id == user_id,
            SwPosition.state == "CLOSED",
            SwPosition.simulated.is_(simulated),
            SwPosition.r_multiple.is_not(None),
            SwPosition.exit_avg.is_not(None),
            SwPosition.closed_on.is_not(None),
        )
        .order_by(SwPosition.closed_on.desc(), SwPosition.id.desc())
    )
    trades: list[ClosedTrade] = []
    rows: list[TradeRow] = []
    for position, symbol in result.all():
        if position.closed_on is None or position.exit_avg is None or position.r_multiple is None:
            continue  # pragma: no cover - excluded by the query; narrows the Optional types
        trade = ClosedTrade(
            symbol=str(symbol),
            setup=position.setup,
            entry_date=position.entry_date,
            exit_date=position.closed_on,
            entry=position.entry_avg,
            initial_stop=position.initial_stop,
            exit_avg=position.exit_avg,
            quantity=position.quantity_entered,
        )
        trades.append(trade)
        rows.append(
            TradeRow(
                symbol=trade.symbol,
                setup=trade.setup,
                entry_date=trade.entry_date,
                exit_date=trade.exit_date,
                entry=trade.entry,
                initial_stop=trade.initial_stop,
                exit_avg=trade.exit_avg,
                quantity=trade.quantity,
                r_multiple=position.r_multiple,
                pnl_inr=position.pnl_inr if position.pnl_inr is not None else trade.pnl_inr,
                close_reason=position.close_reason,
            )
        )
    return trades, rows


async def sessions_logged(session: AsyncSession, *, user_id: int) -> int:
    """How many sessions the system has run — every `sw_session` row, the way the EOD job's own
    counter and its email count them, so the page and the message never disagree."""
    return int(
        (
            await session.execute(
                select(func.count()).select_from(SwSession).where(SwSession.user_id == user_id)
            )
        ).scalar_one()
    )


async def ladder(
    session: AsyncSession,
    *,
    user_id: int,
    execution_enabled: bool,
    last_r: Sequence[Decimal],
    config: SwingConfig = DEFAULT_SWING_CONFIG,
) -> LadderCard:
    """The ladder card. ``last_r`` is the book's closes newest first; the card keeps the last
    ``lookback_trades`` of them, oldest first, which is the order the ladder read them in."""
    level = (
        await session.execute(select(SwConfig.exposure_level).where(SwConfig.user_id == user_id))
    ).scalar_one_or_none()
    rung = int(level or 0)
    market = (
        await session.execute(
            select(SwMarketDaily)
            .where(SwMarketDaily.user_id == user_id)
            .order_by(SwMarketDaily.date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    recent = tuple(reversed(list(last_r)[: config.market.lookback_trades]))
    if market is None:
        positions, exposure = config.market.tiers[min(rung, len(config.market.tiers) - 1)]
        return LadderCard(
            level=rung,
            gate=GATE_UNKNOWN,
            max_open_positions=positions,
            max_exposure_pct=Decimal(str(exposure)).quantize(_TWO_DP),
            new_entries_allowed=False,
            last_r=recent,
            reads="REAL",
        )
    return LadderCard(
        level=rung,
        gate=market.gate,
        max_open_positions=market.max_open_positions,
        max_exposure_pct=market.max_exposure_pct,
        new_entries_allowed=market.new_entries_allowed,
        last_r=recent,
        reads="REAL",
    )


def _record(value: object) -> JsonObject:
    """A JSON object out of stored JSON, or an empty one — never a crash on a missing key."""
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    return {}


def _entries(value: object) -> list[JsonObject]:
    if isinstance(value, list):
        return [_record(item) for item in value]
    return []


def _number(value: object) -> object:
    """The engine stores every ``Decimal`` as its string; the card carries the ``Decimal``, so
    the canonical encoder puts a number with its precision on the wire. Anything else is
    passed through as stored."""
    if isinstance(value, str):
        try:
            return Decimal(value)
        except ArithmeticError:
            return value
    return value


def _numbers(record: JsonObject) -> JsonObject:
    return {key: _number(value) for key, value in record.items()}


def _equity_summary(curve: object) -> JsonObject:
    """``equity_curve`` is one point per session; the card says where it started, ended and
    ranged. The curve itself stays on the row."""
    points = curve if isinstance(curve, list) else []
    values = [
        Decimal(str(point[1]))
        for point in points
        if isinstance(point, list) and len(point) == _CURVE_POINT_ARITY
    ]
    if not values:
        return {"sessions": 0, "start": None, "end": None, "low": None, "high": None}
    return {
        "sessions": len(values),
        "start": values[0],
        "end": values[-1],
        "low": min(values),
        "high": max(values),
    }


#: The books in the order the page draws them — the engine's own (gate off, breadth only, full).
#: JSONB keeps no key order, so the card restores it.
GATE_MODE_ORDER: Final[tuple[str, ...]] = tuple(mode.value for mode in GateMode)


def _by_mode(value: object) -> list[tuple[str, object]]:
    record = _record(value)
    return [(mode, record[mode]) for mode in GATE_MODE_ORDER if mode in record]


def _cells(value: object) -> JsonObject:
    """``{mode: cell}`` in the engine's mode order, every cell's numbers as ``Decimal``."""
    return {mode: _numbers(_record(cell)) for mode, cell in _by_mode(value)}


def _grouped_cells(value: object) -> JsonObject:
    """``{year or setup: {mode: cell}}``, the groups sorted."""
    return {key: _cells(cells) for key, cells in sorted(_record(value).items())}


def _contribution(value: object) -> JsonObject | None:
    """One contribution — ``overall``, ``by_year``, ``by_setup`` — or ``None`` as stored."""
    if value is None:
        return None
    record = _record(value)
    return {
        "overall": _numbers(_record(record.get("overall"))),
        "by_year": {
            key: _numbers(_record(cell))
            for key, cell in sorted(_record(record.get("by_year")).items())
        },
        "by_setup": {
            key: _numbers(_record(cell))
            for key, cell in sorted(_record(record.get("by_setup")).items())
        },
    }


def backtest_comparison(stored: JsonObject) -> JsonObject | None:
    """The stored ``comparison`` with its numbers as ``Decimal``; ``None`` for a run stored
    before SW9.6, which the page reads as "no comparison in this run"."""
    value = stored.get("comparison")
    if not isinstance(value, dict):
        return None
    record = _record(value)
    contribution = _record(record.get("contribution"))
    return {
        "index_supplied": bool(record.get("index_supplied", False)),
        "primary": record.get("primary"),
        "modes": dict(_by_mode(record.get("modes"))),
        "overall": _cells(record.get("overall")),
        "by_year": _grouped_cells(record.get("by_year")),
        "by_setup": _grouped_cells(record.get("by_setup")),
        "contribution": {
            "breadth": _contribution(contribution.get("breadth")),
            "index_rule": _contribution(contribution.get("index_rule")),
        },
    }


def index_supplied(stored: JsonObject) -> bool:
    """Did the stored run read an index? A run stored before SW9.6 had none."""
    comparison = stored.get("comparison")
    return isinstance(comparison, dict) and bool(comparison.get("index_supplied", False))


def backtest_caveats(stored: JsonObject) -> tuple[str, ...]:
    """The engine's standing sentences, plus the index-absent one when the run had no index."""
    if index_supplied(stored):
        return CAVEATS
    return (*CAVEATS, INDEX_ABSENT_CAVEAT)


def backtest_stats(stored: JsonObject) -> JsonObject:
    """The card's ``stats`` from a row's stored ``BacktestResult.to_json()``."""
    headline = _numbers(_record(stored.get("stats")))
    r_values = [
        Decimal(str(trade["r_multiple"]))
        for trade in _entries(stored.get("trades"))
        if "r_multiple" in trade
    ]
    counts = Counter(bucket_of(r) for r in r_values)
    drawdown = _numbers(_record(stored.get("drawdown")))
    return {
        **headline,
        "max_drawdown_pct": drawdown.get("max_pct"),
        "histogram": [
            {"bucket": bucket, "count": counts.get(bucket, 0)} for bucket in HISTOGRAM_BUCKETS
        ],
        "by_setup": {
            setup: _numbers(_record(stats))
            for setup, stats in _record(stored.get("by_setup")).items()
        },
        "by_year": {
            year: _numbers(_record(stats)) for year, stats in _record(stored.get("by_year")).items()
        },
        "funnel": _record(stored.get("funnel")),
        "equity": _equity_summary(stored.get("equity_curve")),
        "drawdown": drawdown,
        "comparison": backtest_comparison(stored),
    }


def backtest_params(stored: JsonObject) -> JsonObject:
    """The run's parameters as the card shows them: the money as numbers, the rest as stored."""
    return {
        key: _number(value) if key in ("sleeve_inr", "cost_pct_per_side") else value
        for key, value in stored.items()
    }


def backtest_card(row: SwBacktestRun) -> BacktestCard:
    stored = row.stats or {}
    return BacktestCard(
        run_id=int(row.id),
        params=backtest_params(row.params),
        started_at=row.started_at.isoformat(),
        finished_at=row.finished_at.isoformat() if row.finished_at is not None else None,
        stats=backtest_stats(stored),
        caveats=backtest_caveats(stored),
    )


async def latest_backtest(session: AsyncSession, *, user_id: int) -> BacktestCard | None:
    """The latest **finished** run — ``finished_at`` set and ``error`` null — not the latest
    started: a run in flight, or a re-run that failed, never displaces the last good number."""
    row = (
        await session.execute(
            select(SwBacktestRun)
            .where(
                SwBacktestRun.user_id == user_id,
                SwBacktestRun.finished_at.is_not(None),
                SwBacktestRun.error.is_(None),
                SwBacktestRun.stats.is_not(None),
            )
            .order_by(SwBacktestRun.finished_at.desc(), SwBacktestRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return None if row is None else backtest_card(row)


async def journal(
    session: AsyncSession,
    *,
    user_id: int,
    execution_enabled: bool,
    config: SwingConfig = DEFAULT_SWING_CONFIG,
) -> JournalView:
    """The whole page in one read."""
    real_trades, real_rows = await closed_trades(session, user_id=user_id, simulated=False)
    paper_trades, paper_rows = await closed_trades(session, user_id=user_id, simulated=True)
    # A10 (SW10.5, closed by SW11): the ladder reads the real book from day one, whatever the
    # flag says; the paper card is still shown, apart, and never feeds the rung.
    ladder_rows = real_rows
    return JournalView(
        real=card(real_trades, real_rows),
        simulated=card(paper_trades, paper_rows),
        sessions=SessionsCard(
            logged=await sessions_logged(session, user_id=user_id),
            required=PAPER_SESSIONS_REQUIRED,
        ),
        ladder=await ladder(
            session,
            user_id=user_id,
            execution_enabled=execution_enabled,
            last_r=_r_values(ladder_rows),
            config=config,
        ),
        backtest=(
            backtest.as_json()
            if (backtest := await latest_backtest(session, user_id=user_id)) is not None
            else None
        ),
    )


__all__ = [
    "GATE_MODE_ORDER",
    "GATE_UNKNOWN",
    "HISTOGRAM_BUCKETS",
    "MAX_TRADES",
    "PAPER_SESSIONS_REQUIRED",
    "BacktestCard",
    "HistogramBar",
    "JournalCard",
    "JournalView",
    "LadderCard",
    "MonthStats",
    "SessionsCard",
    "SetupStats",
    "TradeRow",
    "backtest_card",
    "backtest_caveats",
    "backtest_comparison",
    "backtest_params",
    "backtest_stats",
    "bucket_of",
    "card",
    "closed_trades",
    "index_supplied",
    "journal",
    "ladder",
    "latest_backtest",
    "sessions_logged",
]
