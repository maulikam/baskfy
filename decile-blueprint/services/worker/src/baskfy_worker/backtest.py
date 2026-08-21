"""Backtests against the database — the I/O half of Prompt 15.

``baskfy_core.backtest`` is pure and knows nothing about PostgreSQL (docs/02 §"Repo layout").
This module is the other side of that line: it reads the trading calendar, runs the screen once
per rebalance date, pulls the adjusted bars for every name the run could possibly hold, fetches
the benchmark's levels, hands the whole panel to the engine, and writes the outcome back.

Why the screen is run per rebalance date rather than once
---------------------------------------------------------
docs/10 §"Execution model" step 1: "Run the screen **as of `d`** using `index_member_daily` at `d`
and `factor_daily` at `d`." ``baskfy_core.screener`` already builds exactly that statement for a
given as-of, so a backtest is *the screener, executed on a series of past dates* — not a second
implementation of the same semantics. If the screener changes, the backtest changes with it,
which is the only arrangement in which the two can be trusted to agree.

Why only the top ``top_n + hold_buffer`` rows are loaded
--------------------------------------------------------
A name ranked worse than ``top_n + hold_buffer`` is sold at the next rebalance whether it appears
in the frame or not (``baskfy_core.rebalance`` exits it as ``rank_outside_buffer`` if it is there
and as ``not_in_screen`` if it is not; the engine does not record the reason). Loading four
thousand rows for every one of 180 rebalance dates to decide something already decided would be
the slowest possible way to reach the same answer.

Why the bar panel is restricted to candidates
----------------------------------------------
The union of every name that ever appears inside the buffer band is a few hundred instruments,
not the two thousand in the universe. A name the book never holds needs no price, and loading
fifteen years of bars for it is the difference between a backtest and a table scan.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import polars as pl
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.screener import current_data_version, execute_screen
from baskfy_core.backtest import (
    BacktestConfig,
    BacktestData,
    BacktestDataError,
    BacktestProgress,
    BacktestResult,
    FragilityReport,
    PricePanel,
    ProgressSink,
    rebalance_dates,
    run_backtest,
    run_fragility,
    shift_schedule,
)
from baskfy_core.models import (
    IndexDef,
    IndexSnapshotDaily,
    Instrument,
    OhlcvDaily,
    TradingDay,
)
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_core.seed_data import NSE_EXCHANGE_ID

log = logging.getLogger(__name__)

__all__ = [
    "BACKTEST_SCREEN_COLUMNS",
    "LoadedBacktest",
    "execute_backtest",
    "load_backtest_data",
    "trading_calendar",
]

#: What the engine needs from a screen row beyond ``rank`` and ``instrument_id``. ``symbol`` and
#: ``name`` are identity columns the screener always projects; these two are the weighting inputs
#: docs/10 §Config's ``marketcap`` and ``inverse_volatility`` schemes read.
BACKTEST_SCREEN_COLUMNS: tuple[str, ...] = ("marketcap_cr", "vol_12m")

#: A hard ceiling on the number of rebalance dates one run may ask the database for. Weekly over
#: fifteen years is ~780; anything past this is a configuration mistake, and finding out by
#: running 5,000 screen queries is an expensive way to learn it.
MAX_REBALANCE_DATES = 1_000


@dataclass(frozen=True, slots=True)
class LoadedBacktest:
    """A panel ready for the engine, plus what it cost to assemble."""

    data: BacktestData
    schedule: tuple[dt.date, ...]
    #: Rebalance dates the offset probe needs on top of ``schedule`` (docs/10 §fragility).
    offset_dates: tuple[dt.date, ...] = ()
    instruments: int = 0
    bars: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)


async def trading_calendar(
    session: AsyncSession, start: dt.date, end: dt.date
) -> tuple[dt.date, ...]:
    """NSE trading days in ``[start, end]``, in order.

    ``trading_day`` is the calendar of record (``docs/04a``); deriving weekdays here would
    re-introduce every holiday the calendar exists to record.
    """
    rows = (
        (
            await session.execute(
                select(TradingDay.date)
                .where(
                    TradingDay.exchange_id == NSE_EXCHANGE_ID,
                    TradingDay.is_trading_day.is_(True),
                    TradingDay.date >= start,
                    TradingDay.date <= end,
                )
                .order_by(TradingDay.date)
            )
        )
        .scalars()
        .all()
    )
    return tuple(rows)


async def _screen_frames(  # noqa: PLR0913 - the screen, the dates and the version are separate
    session: AsyncSession,
    definition: ScreenDefinition,
    dates: Sequence[dt.date],
    *,
    data_version: int,
    limit: int,
    progress: ProgressSink | None,
) -> tuple[dict[dt.date, pl.DataFrame], set[int]]:
    """One screen per rebalance date, as of that date. See the module docstring."""
    frames: dict[dt.date, pl.DataFrame] = {}
    candidates: set[int] = set()
    for index, day in enumerate(dates):
        result = await execute_screen(
            session,
            definition,
            as_of=day,
            data_version=data_version,
            columns=BACKTEST_SCREEN_COLUMNS,
            limit=limit,
        )
        rows = result.rows
        candidates.update(row.instrument_id for row in rows)
        frames[day] = pl.DataFrame(
            {
                # The engine's look-ahead guard checks this column. It is the as-of the screen was
                # actually run for, not the date requested, so a snapped weekend cannot smuggle a
                # later row past the guard.
                "date": [result.as_of] * len(rows),
                "instrument_id": [row.instrument_id for row in rows],
                "symbol": [str(row.values["symbol"]) for row in rows],
                "name": [str(row.values["name"]) for row in rows],
                "rank": [row.rank for row in rows],
                "marketcap_cr": [_number(row.values.get("marketcap_cr")) for row in rows],
                "vol_12m": [_number(row.values.get("vol_12m")) for row in rows],
            },
            schema={
                "date": pl.Date,
                "instrument_id": pl.Int64,
                "symbol": pl.String,
                "name": pl.String,
                "rank": pl.Int64,
                "marketcap_cr": pl.Float64,
                "vol_12m": pl.Float64,
            },
        )
        if progress is not None:
            progress(BacktestProgress("screening", index + 1, len(dates), day))
    return frames, candidates


def _number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal | int | float):
        return float(value)
    raise BacktestDataError(f"{value!r} is not a number a weighting scheme can use")


async def _bar_frame(
    session: AsyncSession, instrument_ids: Sequence[int], start: dt.date, end: dt.date
) -> pl.DataFrame:
    """Adjusted opens and closes for the candidate set.

    ``open`` and ``close`` are the **adjusted** columns (``docs/09`` §"Adjustment algorithm":
    ``ohlcv_daily.close_raw`` is the exchange print and ``close`` is the adjusted series). A
    backtest that marked to raw prices would show a split as a 90% loss.
    """
    if not instrument_ids:
        return pl.DataFrame(
            schema={
                "date": pl.Date,
                "instrument_id": pl.Int64,
                "open": pl.Float64,
                "close": pl.Float64,
            }
        )
    statement: Select[tuple[dt.date, int, Decimal, Decimal]] = (
        select(OhlcvDaily.date, OhlcvDaily.instrument_id, OhlcvDaily.open, OhlcvDaily.close)
        .where(
            OhlcvDaily.instrument_id.in_(instrument_ids),
            OhlcvDaily.date >= start,
            OhlcvDaily.date <= end,
        )
        .order_by(OhlcvDaily.instrument_id, OhlcvDaily.date)
    )
    rows = (await session.execute(statement)).all()
    return pl.DataFrame(
        {
            "date": [row[0] for row in rows],
            "instrument_id": [int(row[1]) for row in rows],
            "open": [float(row[2]) for row in rows],
            "close": [float(row[3]) for row in rows],
        },
        schema={
            "date": pl.Date,
            "instrument_id": pl.Int64,
            "open": pl.Float64,
            "close": pl.Float64,
        },
    )


async def _benchmark_frame(
    session: AsyncSession, slug: str, start: dt.date, end: dt.date
) -> tuple[pl.DataFrame | None, str | None]:
    """The benchmark's daily level, or a note saying why there is none.

    A missing benchmark is not fatal: docs/10 §Outputs needs it for alpha, beta, tracking error
    and the information ratio, and those come back ``null``. Refusing the whole run because an
    index's history has a gap would be a worse answer than reporting the metrics we can compute.
    """
    index_id = (
        await session.execute(select(IndexDef.id).where(IndexDef.slug == slug))
    ).scalar_one_or_none()
    if index_id is None:
        return None, f"no index_def row has slug {slug!r}, so no benchmark was loaded"
    rows = (
        await session.execute(
            select(IndexSnapshotDaily.date, IndexSnapshotDaily.level)
            .where(
                IndexSnapshotDaily.index_id == index_id,
                IndexSnapshotDaily.date >= start,
                IndexSnapshotDaily.date <= end,
                IndexSnapshotDaily.level.is_not(None),
            )
            .order_by(IndexSnapshotDaily.date)
        )
    ).all()
    if not rows:
        return None, f"{slug} has no index_snapshot_daily levels between {start} and {end}"
    frame = pl.DataFrame(
        {"date": [row[0] for row in rows], "level": [float(row[1]) for row in rows]},
        schema={"date": pl.Date, "level": pl.Float64},
    )
    return frame, None


async def _instrument_meta(
    session: AsyncSession, instrument_ids: Iterable[int]
) -> tuple[dict[int, str], dict[int, str], dict[int, dt.date]]:
    ids = sorted(set(instrument_ids))
    if not ids:
        return {}, {}, {}
    rows = (
        await session.execute(
            select(Instrument.id, Instrument.symbol, Instrument.name, Instrument.delisted_on).where(
                Instrument.id.in_(ids)
            )
        )
    ).all()
    symbols = {int(row[0]): str(row[1]) for row in rows}
    names = {int(row[0]): str(row[2]) for row in rows}
    delistings = {int(row[0]): row[3] for row in rows if row[3] is not None}
    return symbols, names, delistings


async def load_backtest_data(
    session: AsyncSession,
    config: BacktestConfig,
    definition: ScreenDefinition,
    *,
    with_offsets: bool = True,
    progress: ProgressSink | None = None,
) -> LoadedBacktest:
    """Assemble everything :func:`baskfy_core.backtest.run_backtest` reads.

    ``with_offsets`` also precomputes the screen for each rebalance date +/-1 trading day, which
    is what docs/10's fragility probe needs. It costs two more screen queries per rebalance date
    and is the only way the probe can be honest — reusing the neighbouring date's screen would
    make every result look perfectly stable.
    """
    calendar = await trading_calendar(session, config.start, config.end)
    if not calendar:
        raise BacktestDataError(
            f"the NSE trading calendar has no day between {config.start.isoformat()} and "
            f"{config.end.isoformat()}"
        )

    schedule = rebalance_dates(calendar, config.start, config.end, config.rebalance)
    if not schedule:
        raise BacktestDataError("the configuration produces no rebalance dates")
    offsets = (
        tuple(
            sorted(
                (
                    set(shift_schedule(calendar, schedule, -1))
                    | set(shift_schedule(calendar, schedule, 1))
                )
                - set(schedule)
            )
        )
        if with_offsets
        else ()
    )
    wanted = tuple(sorted(set(schedule) | set(offsets)))
    if len(wanted) > MAX_REBALANCE_DATES:
        raise BacktestDataError(
            f"this configuration needs {len(wanted)} screen runs, above the {MAX_REBALANCE_DATES} "
            "a single backtest may issue. Use a longer rebalance interval or a shorter window."
        )

    data_version = await current_data_version(session)
    limit = config.selection.top_n + config.selection.hold_buffer
    frames, candidates = await _screen_frames(
        session, definition, wanted, data_version=data_version, limit=limit, progress=progress
    )

    bars = await _bar_frame(session, sorted(candidates), config.start, config.end)
    if not bars.height:
        raise BacktestDataError(
            "no adjusted bars exist for any name this screen selected in the requested window; "
            "there is nothing to simulate"
        )
    benchmark, benchmark_note = await _benchmark_frame(
        session, config.benchmark, config.start, config.end
    )
    symbols, names, delistings = await _instrument_meta(session, candidates)

    notes: list[str] = []
    if benchmark_note is not None:
        notes.append(benchmark_note)

    data = BacktestData(
        calendar=tuple(calendar),
        prices=PricePanel(bars, calendar),
        screens=frames,
        benchmark=benchmark,
        delistings=delistings,
        symbols=symbols,
        names=names,
        data_version=data_version,
    )
    return LoadedBacktest(
        data=data,
        schedule=schedule,
        offset_dates=offsets,
        instruments=len(candidates),
        bars=bars.height,
        notes=tuple(notes),
    )


@dataclass(frozen=True, slots=True)
class BacktestOutcome:
    """One completed run: the result, and the fragility probe if it was asked for."""

    loaded: LoadedBacktest
    result: BacktestResult
    fragility: FragilityReport | None


async def execute_backtest(
    session: AsyncSession,
    config: BacktestConfig,
    definition: ScreenDefinition,
    *,
    fragility: bool = True,
    progress: ProgressSink | None = None,
) -> BacktestOutcome:
    """Load, simulate, and — unless asked not to — run docs/10's four perturbations."""
    loaded = await load_backtest_data(
        session, config, definition, with_offsets=fragility, progress=progress
    )
    result = run_backtest(config, loaded.data, progress=progress)
    report: FragilityReport | None = None
    if fragility:
        if progress is not None:
            progress(BacktestProgress("fragility", 0, 4))
        report = run_fragility(config, loaded.data, base=result)
        if progress is not None:
            progress(BacktestProgress("fragility", 4, 4))
    return BacktestOutcome(loaded=loaded, result=result, fragility=report)


def notes_for(loaded: LoadedBacktest, result: BacktestResult) -> tuple[str, ...]:
    """Everything the assumptions panel should say that is specific to *this* run."""
    return tuple(dict.fromkeys((*loaded.notes, *result.notes)))
