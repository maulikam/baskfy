"""SW9 — the runner for the swing EOD backtest (``docs/swing/06-module-plan.md``).

    "A number, with its caveats, before any real money."

The arithmetic is not here. :func:`baskfy_core.swing.backtest.run_backtest` is pure — bars in, a
result out, no clock and no I/O — and it calls the same functions the live book calls, so the
backtest cannot disagree with the desk about what the method is. This module does the three
things the core may not:

**It loads the bars the way the detectors are given them.** The same adjusted frame
:func:`baskfy_worker.tasks.swing.load_swing_bars` produces for the nightly job — ``upper_circuit``
multiplied into the adjusted space on the way in, the cash series only, the user's own liquidity
floors from ``sw_config`` — over ``start - LOOKBACK_SESSIONS .. end``, so the first session of
the run has the same 200 sessions behind it that the first night of the live job would. One
difference, and it is the one `04` §11 names: **survivorship is handled by
``instrument.delisted_on``**. The nightly query keeps ``delisted_on IS NULL`` because a retired
name cannot be a candidate tonight; a backtest that read the same query would be a study of the
survivors. So the runner's query keeps every name that was listed inside the run — delisted on
or after ``start``, or not at all — and the engine's own ``NO_BAR`` rule sells a name that stops
printing bars (DECISIONS-SW SW9.4, SW9.6).

**It supplies the calendar.** ``trading_day`` rows for the NSE from the lookback start to
``end``; the engine trades only on days the calendar names, so a bar on a holiday is read by the
detectors and never traded on.

**It supplies the index and the delisting dates** (SW9.6, STANDING-ANSWERS A12 and B2). The
index rule of `04` §8.2 reads NIFTY 500's closes from ``index_snapshot_daily`` — the nightly
job's slug, with the same NIFTY 50 fallback when NIFTY 500 has too few rows in the window, and
no index at all when neither has (the result's caveats say so) — and ``params.index_slug``
records which series the run read. The engine computes the two averages in-frame from the closes
on or before each session. ``instrument.delisted_on`` for every name in the frame is handed over
so a held name is sold at its last close on its last bar and counted ``DELISTED``.

**It stores the run.** One ``sw_backtest_run`` row per run, written on the way in (``params``)
and on the way out (``stats = result.to_json()``), never edited afterwards: a re-run is a second
row. A run that raises records the exception in ``error`` with ``finished_at`` set and
**re-raises**, so Celery reports the failure and nothing about it is swallowed.

The Celery binding is in ``celery_tasks`` (``baskfy.swing.backtest``, the compute queue, no
Beat entry: a five-minute run over nine years is something a person asks for, not a schedule).
``tools/swing/backtest.py`` at the repo root runs this body from the command line.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import traceback
from collections.abc import Callable, Sequence
from dataclasses import asdict, replace
from decimal import Decimal
from typing import Final

import polars as pl
from sqlalchemy import Row, Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import IndexDef, IndexSnapshotDaily, Instrument, OhlcvDaily, TradingDay
from baskfy_core.models.base import JsonObject
from baskfy_core.models.swing import SwBacktestRun
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.swing.backtest import BacktestParams, BacktestResult, run_backtest
from baskfy_core.swing.config import SwingConfig
from baskfy_worker.tasks.swing import (
    CASH_SERIES,
    LOOKBACK_SESSIONS,
    BarRow,
    load_swing_config,
    lookback_start,
)

log = logging.getLogger(__name__)

#: `04` §11: "For each session 2017→". The first session a run covers when nobody says otherwise.
DEFAULT_START: Final = dt.date(2017, 1, 1)

#: The task name, so the binding, the CLI and the tests spell it once.
SWING_BACKTEST_TASK: Final = "baskfy.swing.backtest"

#: The index the gate reads (`04` §8.2), and the one it falls back to — the nightly job's own
#: pair (``run_detect_swing``'s ``index_slug`` and its ``fallback``).
INDEX_SLUG: Final = "nifty-500"
INDEX_FALLBACK_SLUG: Final = "nifty-50"

#: The columns of the index frame the engine reads.
INDEX_SCHEMA: Final = pl.Schema({"date": pl.Date, "close": pl.Float64})


def utc_now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


# ---------------------------------------------------------------------------
# Inputs: the bars and the calendar
# ---------------------------------------------------------------------------


def _bar_query(start: dt.date, end: dt.date, *, listed_through: dt.date) -> Select[BarRow]:
    """``tasks.swing._bar_query`` with one clause changed — the survivorship one.

    Every name in the cash series with a bar inside the window, whether it is listed today or
    was delisted on or after ``listed_through`` (the run's first session). A name delisted
    *before* the run started never had a bar the run could trade, and is left out for the same
    reason the nightly query leaves out every retired name.
    """
    return (
        select(
            OhlcvDaily.instrument_id,
            Instrument.symbol,
            OhlcvDaily.date,
            OhlcvDaily.open,
            OhlcvDaily.high,
            OhlcvDaily.low,
            OhlcvDaily.close,
            OhlcvDaily.volume,
            OhlcvDaily.turnover,
            OhlcvDaily.upper_circuit,
            OhlcvDaily.adj_factor,
        )
        .join(Instrument, Instrument.id == OhlcvDaily.instrument_id)
        .where(
            OhlcvDaily.date >= start,
            OhlcvDaily.date <= end,
            or_(Instrument.delisted_on.is_(None), Instrument.delisted_on >= listed_through),
            Instrument.instrument_type == "EQ",
            Instrument.series.in_(CASH_SERIES),
        )
        .order_by(OhlcvDaily.instrument_id, OhlcvDaily.date)
    )


#: The columns of the bars frame, in the order and dtypes ``load_swing_bars`` produces them.
BARS_SCHEMA: Final = pl.Schema(
    {
        "instrument_id": pl.Int64,
        "symbol": pl.String,
        "date": pl.Date,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Float64,
        "turnover": pl.Float64,
        "upper_circuit": pl.Float64,
        "adj_factor": pl.Float64,
    }
)


def bars_frame(records: Sequence[Row[BarRow]]) -> pl.DataFrame:
    """Rows of :func:`_bar_query` as the frame ``with_swing_indicators`` expects.

    The same reading ``load_swing_bars`` gives the nightly job — ``upper_circuit`` is an exchange
    print and ``high`` has been adjusted in place, so the band is multiplied by the row's factor
    on the way in and the two are compared in one space. Mirrored rather than imported because
    the nightly reader is inline in its query function and that file is SW3's; a keyword on it
    for the delisting clause is the one-line merge (SW9.6).
    """
    if not records:
        return pl.DataFrame(schema=BARS_SCHEMA)
    rows: list[dict[str, object]] = []
    for record in records:
        factor = float(record.adj_factor) if record.adj_factor is not None else 1.0
        circuit = record.upper_circuit
        rows.append(
            {
                "instrument_id": int(record.instrument_id),
                "symbol": record.symbol,
                "date": record.date,
                "open": float(record.open),
                "high": float(record.high),
                "low": float(record.low),
                "close": float(record.close),
                "volume": float(record.volume),
                "turnover": None if record.turnover is None else float(record.turnover),
                "upper_circuit": None if circuit is None else float(circuit) * factor,
                "adj_factor": factor,
            }
        )
    return pl.DataFrame(rows, schema=BARS_SCHEMA)


async def load_backtest_bars(session: AsyncSession, params: BacktestParams) -> pl.DataFrame:
    """The adjusted bars for ``params.start - LOOKBACK_SESSIONS .. params.end``.

    The lookback is counted on the trading calendar, as the detectors count it, so the run's
    first session is detected on the same 200 sessions of history the live job would have had
    that night — not on one bar, which would make the first half-year of every run a desert.
    """
    window_start = await lookback_start(session, params.start, LOOKBACK_SESSIONS)
    records = (
        await session.execute(_bar_query(window_start, params.end, listed_through=params.start))
    ).all()
    return bars_frame(records)


async def load_backtest_calendar(session: AsyncSession, params: BacktestParams) -> list[dt.date]:
    """Every NSE trading day from the lookback start to ``params.end``, oldest first."""
    window_start = await lookback_start(session, params.start, LOOKBACK_SESSIONS)
    rows = await session.execute(
        select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date >= window_start,
            TradingDay.date <= params.end,
        )
        .order_by(TradingDay.date)
    )
    return [row[0] for row in rows]


async def _index_rows(
    session: AsyncSession, slug: str, *, window_start: dt.date, end: dt.date
) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(IndexSnapshotDaily)
                .join(IndexDef, IndexDef.id == IndexSnapshotDaily.index_id)
                .where(
                    IndexDef.slug == slug,
                    IndexSnapshotDaily.date >= window_start,
                    IndexSnapshotDaily.date <= end,
                    IndexSnapshotDaily.level.is_not(None),
                )
            )
        ).scalar_one()
    )


async def resolve_index_slug(  # noqa: PLR0913 - the window, the config, the two slugs
    session: AsyncSession,
    *,
    start: dt.date,
    end: dt.date,
    config: SwingConfig,
    slug: str = INDEX_SLUG,
    fallback: str = INDEX_FALLBACK_SLUG,
) -> str | None:
    """Which index series the run reads — the nightly job's rule, once for the whole window.

    ``load_index_reading`` takes ``slug`` when it has ``index_ma_slow`` levels on or before the
    date and ``fallback`` otherwise; a run reads one series throughout, so the choice is made on
    the window: the first of the two with at least ``index_ma_slow`` levels between the lookback
    start and ``end``. Neither → ``None``, and the engine's caveats say the gate was breadth-only.
    """
    window_start = await lookback_start(session, start, LOOKBACK_SESSIONS)
    for candidate in (slug, fallback):
        rows = await _index_rows(session, candidate, window_start=window_start, end=end)
        if rows >= config.market.index_ma_slow:
            return candidate
    return None


async def load_backtest_index(session: AsyncSession, params: BacktestParams) -> pl.DataFrame | None:
    """``params.index_slug``'s closes from the lookback start to ``params.end``, oldest first —
    the ``(date, close)`` frame the engine's index rule reads; ``None`` with no slug resolved."""
    if params.index_slug is None:
        return None
    window_start = await lookback_start(session, params.start, LOOKBACK_SESSIONS)
    rows = await session.execute(
        select(IndexSnapshotDaily.date, IndexSnapshotDaily.level)
        .join(IndexDef, IndexDef.id == IndexSnapshotDaily.index_id)
        .where(
            IndexDef.slug == params.index_slug,
            IndexSnapshotDaily.date >= window_start,
            IndexSnapshotDaily.date <= params.end,
            IndexSnapshotDaily.level.is_not(None),
        )
        .order_by(IndexSnapshotDaily.date)
    )
    records = [(on, float(level)) for on, level in rows if level is not None]
    return pl.DataFrame(
        {"date": [on for on, _ in records], "close": [close for _, close in records]},
        schema=INDEX_SCHEMA,
    )


async def load_delisted(session: AsyncSession, bars: pl.DataFrame) -> dict[int, dt.date]:
    """``instrument_id -> delisted_on`` for every name in the frame that has one (B2)."""
    if bars.is_empty():
        return {}
    ids = [int(value) for value in bars["instrument_id"].unique().to_list()]
    rows = await session.execute(
        select(Instrument.id, Instrument.delisted_on).where(
            Instrument.id.in_(ids), Instrument.delisted_on.is_not(None)
        )
    )
    return {int(instrument_id): on for instrument_id, on in rows if on is not None}


async def params_for(  # noqa: PLR0913 - one keyword per parameter a run can be given
    session: AsyncSession,
    *,
    user_id: int,
    start: dt.date,
    end: dt.date,
    sleeve_inr: Decimal | None = None,
    cost_pct_per_side: Decimal | None = None,
    config: SwingConfig | None = None,
    index_slug: str = INDEX_SLUG,
    index_fallback: str = INDEX_FALLBACK_SLUG,
) -> BacktestParams:
    """``BacktestParams`` for a run: the user's liquidity floors, the pack's defaults otherwise.

    The config is what the detectors run with tonight (``load_swing_config``: ``sw_config``'s
    three floors over ``DEFAULT_SWING_CONFIG``) so the backtest's universe is the one the user
    actually trades. The sleeve is ``params.sleeve_inr`` — `04` §11's constant ₹10 lakh — and
    never ``sw_config.sleeve_capital_inr``, which is ₹0 until Maulik sets it and would plan
    nothing (SW9.6). ``index_slug`` is resolved here (:func:`resolve_index_slug`) so the row's
    ``params`` names the series the run read from the moment the run is started.
    """
    resolved = config if config is not None else await load_swing_config(session, user_id)
    slug = await resolve_index_slug(
        session, start=start, end=end, config=resolved, slug=index_slug, fallback=index_fallback
    )
    params = BacktestParams(start=start, end=end, config=resolved, index_slug=slug)
    if sleeve_inr is not None:
        params = replace(params, sleeve_inr=sleeve_inr)
    if cost_pct_per_side is not None:
        params = replace(params, cost_pct_per_side=cost_pct_per_side)
    return params


def params_json(params: BacktestParams) -> JsonObject:
    """``BacktestParams`` as plain JSON — the same rendering ``to_json()['params']`` uses:
    ``str`` for every ``Decimal`` and date, lists for tuples, enum values for enums."""
    rendered = json.loads(json.dumps(asdict(params), default=str))
    if not isinstance(rendered, dict):  # pragma: no cover - asdict of a dataclass is a dict
        raise TypeError("params did not render as an object")
    return {str(key): value for key, value in rendered.items()}


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


async def start_run(
    session: AsyncSession,
    *,
    user_id: int,
    params: BacktestParams,
    clock: Callable[[], dt.datetime] = utc_now,
) -> SwBacktestRun:
    """Insert the row that says a run was asked for. Flushed, not committed — the caller owns
    the transaction (the Celery binding commits here so an operator can see the run in flight)."""
    row = SwBacktestRun(user_id=user_id, params=params_json(params), started_at=clock())
    session.add(row)
    await session.flush()
    return row


async def finish_run(
    session: AsyncSession,
    row: SwBacktestRun,
    params: BacktestParams,
    *,
    clock: Callable[[], dt.datetime] = utc_now,
) -> BacktestResult:
    """Load the inputs, run the engine, store the result on ``row``.

    On any exception the row gets ``error`` (type, message and the traceback) and
    ``finished_at``, is flushed, and the exception is **re-raised**: the failure is recorded and
    reported, never swallowed. A caller that wants the failed row to survive its own rollback
    commits before letting the exception out — the Celery binding does.
    """
    try:
        bars = await load_backtest_bars(session, params)
        calendar = await load_backtest_calendar(session, params)
        index = await load_backtest_index(session, params)
        delisted = await load_delisted(session, bars)
        log.info(
            "swing backtest %s: %d bars, %d calendar days, %s..%s, index %s (%d closes), "
            "%d delisted",
            row.id,
            bars.height,
            len(calendar),
            params.start,
            params.end,
            params.index_slug,
            0 if index is None else index.height,
            len(delisted),
        )
        result = run_backtest(bars, params, calendar=calendar, index=index, delisted=delisted)
    except Exception as exc:
        row.error = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
        row.finished_at = clock()
        await session.flush()
        log.warning("swing backtest %s failed", row.id, extra={"error": str(exc)})
        raise
    row.stats = result.to_json()
    row.finished_at = clock()
    await session.flush()
    return result


async def run_swing_backtest(
    session: AsyncSession,
    *,
    user_id: int,
    params: BacktestParams,
    clock: Callable[[], dt.datetime] = utc_now,
) -> tuple[SwBacktestRun, BacktestResult]:
    """The whole run in one transaction — what the tests and the CLI drive."""
    row = await start_run(session, user_id=user_id, params=params, clock=clock)
    result = await finish_run(session, row, params, clock=clock)
    return row, result


async def run_and_commit(  # noqa: PLR0913 - one keyword per parameter a run can be given
    session: AsyncSession,
    *,
    user_id: int,
    start: dt.date,
    end: dt.date,
    sleeve_inr: Decimal | None = None,
    cost_pct_per_side: Decimal | None = None,
    clock: Callable[[], dt.datetime] = utc_now,
) -> JsonObject:
    """The task body, over a session whose commits the caller owns (``run_checkpointed``).

    Two commits rather than one transaction: the row is committed as soon as it is started, so
    an operator can see the run in flight, and a run that raises commits its ``error`` before the
    exception reaches Celery — a failed run is a row, not a rollback.
    """
    params = await params_for(
        session,
        user_id=user_id,
        start=start,
        end=end,
        sleeve_inr=sleeve_inr,
        cost_pct_per_side=cost_pct_per_side,
    )
    row = await start_run(session, user_id=user_id, params=params, clock=clock)
    await session.commit()
    try:
        result = await finish_run(session, row, params, clock=clock)
    except Exception:
        # The row already carries `error` and `finished_at`; make them durable, then let the
        # failure out. Not a swallow: the exception continues on its way to the caller.
        await session.commit()
        raise
    await session.commit()
    return summary(row, result)


def summary(row: SwBacktestRun, result: BacktestResult) -> JsonObject:
    """What the task returns: enough to read the run without opening the row."""
    stats = result.stats
    comparison = result.comparison
    return {
        "run_id": int(row.id),
        "start": result.params.start.isoformat(),
        "end": result.params.end.isoformat(),
        "index_slug": result.params.index_slug,
        "sessions": len(result.equity_curve),
        "trades": stats.trades,
        "win_rate_pct": str(stats.win_rate_pct),
        "expectancy_r": str(stats.expectancy_r),
        "net_r": str(stats.net_r),
        "profit_factor": None if stats.profit_factor is None else str(stats.profit_factor),
        "max_drawdown_pct": str(result.drawdown.max_pct),
        "gate_comparison": (
            None
            if comparison is None
            else {
                mode.value: {"entered": cell.entered, "net_r": str(cell.net_r)}
                for mode, cell in comparison.overall.items()
            }
        ),
        "funnel": dict(result.funnel),
        "started_at": row.started_at.isoformat(),
        "finished_at": row.finished_at.isoformat() if row.finished_at is not None else None,
    }


__all__ = [
    "BARS_SCHEMA",
    "DEFAULT_START",
    "INDEX_FALLBACK_SLUG",
    "INDEX_SCHEMA",
    "INDEX_SLUG",
    "SWING_BACKTEST_TASK",
    "bars_frame",
    "finish_run",
    "load_backtest_bars",
    "load_backtest_calendar",
    "load_backtest_index",
    "load_delisted",
    "params_for",
    "params_json",
    "resolve_index_slug",
    "run_and_commit",
    "run_swing_backtest",
    "start_run",
    "summary",
    "utc_now",
]
