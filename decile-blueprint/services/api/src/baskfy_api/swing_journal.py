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
* **The backtest card**, filled by SW9; ``None`` until a run exists, and the page says so.

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
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG, SwingConfig
from baskfy_core.swing.journal import ClosedTrade, JournalStats, summarize

#: `docs/swing/02` §3.2 — the paper track record the real-money flag is gated on. Defined here,
#: in the API package, because the worker depends on the API and not the other way round; the
#: EOD job's email and this page must count against the same number.
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
    one end of it. ``reads`` names the book the ladder is reading (PACK.6): the simulated one
    until `BASKFY_SWING_EXECUTION_ENABLED` is true, the real one after.
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
class JournalView:
    real: JournalCard
    simulated: JournalCard
    sessions: SessionsCard
    ladder: LadderCard
    #: SW9's. ``None`` until a backtest run exists; the page renders "not run yet".
    backtest: None


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
            reads="REAL" if execution_enabled else "SIMULATED",
        )
    return LadderCard(
        level=rung,
        gate=market.gate,
        max_open_positions=market.max_open_positions,
        max_exposure_pct=market.max_exposure_pct,
        new_entries_allowed=market.new_entries_allowed,
        last_r=recent,
        reads="REAL" if execution_enabled else "SIMULATED",
    )


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
    # PACK.6: the ladder reads the real book once execution is enabled, the paper one before.
    ladder_rows = real_rows if execution_enabled else paper_rows
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
        backtest=None,
    )


__all__ = [
    "GATE_UNKNOWN",
    "HISTOGRAM_BUCKETS",
    "MAX_TRADES",
    "PAPER_SESSIONS_REQUIRED",
    "HistogramBar",
    "JournalCard",
    "JournalView",
    "LadderCard",
    "MonthStats",
    "SessionsCard",
    "SetupStats",
    "TradeRow",
    "bucket_of",
    "card",
    "closed_trades",
    "journal",
    "ladder",
    "sessions_logged",
]
