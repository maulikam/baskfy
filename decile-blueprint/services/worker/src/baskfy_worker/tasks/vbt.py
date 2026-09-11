"""VB4 — the nightly volume-breakout detection job (``docs/vbt/06-module-plan.md``).

Every trading session, from bars the chain has already published, this writes what VBT-1 saw:
one ``vb_signal_daily`` row per name that cleared Chartink's five lines — flagged ``SIGNAL`` when
the six trend filters held too and ``SCAN_ONLY`` when they did not, with the letters that failed —
and one ``vb_breadth_daily`` row saying whether the tape allowed a new entry at all.

WHERE THE ARITHMETIC LIVES
--------------------------
Not here. ``baskfy_core.vbt`` computes the calendar, every indicator, the scan, the six filters
and the breadth; this module reads rows, hands them over as a DataFrame, and writes what comes
back. The same split as ``compute_market_health`` versus ``baskfy_core.breadth``, and for the
same reason: the backtest (VB2, VB9) and the nightly job must not be able to disagree about what
a signal is.

FOUR THINGS THIS MODULE DOES THAT THE CORE CANNOT
-------------------------------------------------
**It reads the universe.** ``04`` §1's ETF exclusion needs the ``etf`` index universe, which is a
table; law 1 forbids core from touching one. The narrow name and symbol patterns come from
``DataConfig`` and are applied here, over the register.

**It turns adjusted levels back into exchange prices.** Every rule reads the adjusted series, so
a split inside a 200-day window does not fake a trend. But a *limit* is a price a person sends to
a broker, and the broker has never heard of our adjustment. ``limit_price`` is the bar's own
``close_raw``; the context levels (the 200-day average, the 21-day EMA, the prior 20-session high)
are divided by the row's ``adj_factor`` so a page can compare them with it, and the factor is
stored beside them so tomorrow's job can notice a split happened overnight.

``upper_circuit`` goes the other way, and it is the subtle one: the band is an exchange print with
no adjusted twin, while ``high`` has been adjusted in place, so on the morning after a 1:2 split
an unconverted comparison would report the whole market locked. It is multiplied by the row's
factor on the way *in*, so the comparison happens in one space.

**It counts the funnel.** "No signals today" is only useful with "…out of 4,186 names, 1,412 of
which have a 200-day average". The counts go on the pipeline step and into
``vb_breadth_daily.detail``, so an operator reading ``/admin/pipeline`` at 21:00 can tell a quiet
day from a broken one — and about thirteen signals a week is what a normal market gives.

**It records a thin session as a thin session.** ``04`` §2.1's rule drops muhurat and special
Saturdays from the rolling calendar. Rather than writing nothing on such a day, the job writes a
breadth row with ``thin_session = true`` and a shut gate, so the history says *why* there was no
signal instead of leaving a hole that looks like a failed job.

THE STEP CANNOT FAIL THE NIGHT
------------------------------
``COMPUTE_VBT`` runs after ``publish`` and after ``compute_swing``, and like both of them it
records its own failure and returns rather than raising (M30's rule for post-publish steps,
DECISIONS-VB VB0.5). A sleeve that could hold back ``data_version`` would make a screener outage
out of a detector bug.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Final

import polars as pl
from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import (
    IndexDef,
    IndexMemberDaily,
    Instrument,
    OhlcvDaily,
    TradingDay,
    VbBreadthDaily,
    VbConfig,
    VbSignalDaily,
)
from baskfy_core.precision import apply_storage_precision
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.vbt.breadth import BreadthReading, breadth_above_dma
from baskfy_core.vbt.calendar import SessionCalendar, drop_thin_sessions
from baskfy_core.vbt.config import (
    DEFAULT_VBT_CONFIG,
    TICK_INR,
    ExitConfig,
    Gate,
    SignalState,
    SizingConfig,
    VbtConfig,
)
from baskfy_core.vbt.exits import initial_stop, tick_floor
from baskfy_core.vbt.indicators import with_vbt_indicators
from baskfy_core.vbt.signals import detect_signals
from baskfy_worker.steps import StepOutcome, StepStatus

log = logging.getLogger(__name__)

#: How many trading sessions of history the detector is given. ``VbtConfig.bars_required`` is 201
#: (the 200-day average plus today); 260 is that with room for a name that missed a few sessions
#: — a suspension, a series move — and still has 201 real bars behind today.
LOOKBACK_SESSIONS: Final = 260

#: PostgreSQL caps one statement at 32,767 bound parameters and ``vb_signal_daily`` is ~28
#: columns wide. The same arithmetic as ``tasks.factors.MAX_BIND_PARAMS``, for the same reason.
MAX_BIND_PARAMS: Final = 32_767

#: The frame the loader returns and ``with_vbt_indicators`` reads — typed even when a column is
#: all null (a month with no circuit band), so a frame built elsewhere in the same shape
#: concatenates without a cast failing on a ``Null`` column.
BAR_SCHEMA: Final = pl.Schema(
    {
        "instrument_id": pl.Int64,
        "symbol": pl.String,
        "date": pl.Date,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "close_raw": pl.Float64,
        "volume": pl.Float64,
        "upper_circuit": pl.Float64,
        "adj_factor": pl.Float64,
    }
)


@dataclass(frozen=True, slots=True)
class VbtFunnel:
    """What happened to the universe on the way to a signal list.

    An empty result is a legitimate answer on most days — about thirteen signals a week in a
    normal market, and none at all when the tape is thin — and the only way to tell that from a
    job that silently read nothing is to say how many names were at each stage.
    """

    instruments: int
    bars: int
    with_a_bar: int
    with_an_average: int
    above_the_average: int
    scan_hits: int
    signals: int
    thin_sessions_dropped: int

    def as_detail(self) -> dict[str, object]:
        return {
            "instruments": self.instruments,
            "bars": self.bars,
            "with_a_bar": self.with_a_bar,
            "with_an_average": self.with_an_average,
            "above_the_average": self.above_the_average,
            "scan_hits": self.scan_hits,
            "signals": self.signals,
            "thin_sessions_dropped": self.thin_sessions_dropped,
        }


async def load_vbt_config(session: AsyncSession, user_id: int) -> VbtConfig:
    """``DEFAULT_VBT_CONFIG`` with this user's four settings applied (PACK.5).

    Only the sleeve's capital, the position count, the position cap and the stop are settings;
    everything else in ``docs/vbt/04`` is a code default, because a threshold that can be changed
    in a form gets changed after a bad week. The capital is not a *detector* input at all — it
    decides what may be bought, not what is a signal — so it is read where the plan is built.

    A user with no ``vb_config`` row gets the defaults rather than an error: the detector is
    read-only, and a missing settings row is a seeding problem, not a reason to skip a night.
    """
    row = (
        await session.execute(select(VbConfig).where(VbConfig.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        return DEFAULT_VBT_CONFIG
    return replace(
        DEFAULT_VBT_CONFIG,
        sizing=replace(
            SizingConfig(),
            max_slots=min(int(row.max_open_positions), SizingConfig().max_slots),
            max_position_pct=float(row.max_position_pct),
        ),
        exits=replace(ExitConfig(), stop_pct=float(row.stop_pct)),
    )


async def lookback_start(session: AsyncSession, as_of: dt.date, sessions: int) -> dt.date:
    """The date ``sessions`` trading days before ``as_of``, from the calendar rather than by
    subtracting days.

    Counting calendar days would be wrong by about a third: 260 trading days is roughly 375
    calendar days, and the error is not constant — a quarter with two long weekends and Diwali is
    a different number from one without. The calendar is a table for exactly this. (The swing
    book has the same helper for the same reason; they are eight lines each and neither imports
    the other, because a sleeve reaching into another sleeve's task module is how two books come
    to share a bug.)
    """
    rows = await session.execute(
        select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date <= as_of,
        )
        .order_by(TradingDay.date.desc())
        .limit(sessions)
    )
    dates: list[dt.date] = [row[0] for row in rows]
    if not dates:
        # No calendar at all. A generous span of calendar days rather than `as_of`, so a missing
        # calendar produces a short window (and no signals, visibly) rather than one bar and a
        # detector that silently agrees there is nothing to see.
        return as_of - dt.timedelta(days=sessions * 2)
    return min(dates)


async def etf_instrument_ids(session: AsyncSession) -> set[int]:
    """Every instrument that has **ever** been in the ``etf`` universe.

    Ever, not point-in-time, and that is deliberate: this is an exclusion of a *kind of
    instrument*, not a statement about an index's membership on a date. A fund that joined the
    list last year was a fund the year before too, and admitting its earlier bars into a
    momentum-continuation scan would be admitting a basket where the rules mean a company.
    """
    rows = await session.execute(
        select(IndexMemberDaily.instrument_id)
        .join(IndexDef, IndexDef.id == IndexMemberDaily.index_id)
        .where(IndexDef.slug == DEFAULT_VBT_CONFIG.data.etf_universe_slug)
        .distinct()
    )
    return {int(instrument_id) for (instrument_id,) in rows}


def _universe_query(config: VbtConfig) -> Select[tuple[int, str, str]]:
    data = config.data
    return select(Instrument.id, Instrument.symbol, Instrument.name).where(
        Instrument.instrument_type == data.instrument_type,
        Instrument.series.in_(list(data.series_allowed)),
    )


async def load_universe(session: AsyncSession, config: VbtConfig = DEFAULT_VBT_CONFIG) -> set[int]:
    """The instrument ids ``04`` §1 admits: NSE cash, EQ/BE/BZ, no ETFs, no SME.

    ``BE`` and ``BZ`` stay in because every trade here is delivery anyway, and dropping today's
    trade-for-trade list would drop the history of names that were *later* demoted — a
    survivorship bias in reverse.
    """
    data = config.data
    name_pattern = re.compile(data.etf_name_pattern, re.IGNORECASE)
    symbol_pattern = re.compile(data.etf_symbol_pattern, re.IGNORECASE)
    etfs = await etf_instrument_ids(session)
    rows = await session.execute(_universe_query(config))
    keep: set[int] = set()
    for instrument_id, symbol, name in rows:
        if int(instrument_id) in etfs:
            continue
        if name and name_pattern.search(name):
            continue
        if symbol_pattern.search(symbol):
            continue
        keep.add(int(instrument_id))
    return keep


async def load_vbt_bars(
    session: AsyncSession, start: dt.date, as_of: dt.date, universe: set[int]
) -> pl.DataFrame:
    """The frame ``with_vbt_indicators`` expects, in the adjusted space.

    ``upper_circuit`` is multiplied by the row's ``adj_factor`` on the way in, for the reason the
    module docstring gives. ``close_raw`` is carried untouched: Chartink's ``Close > 30`` and
    filter F's rupee turnover are both statements about what the exchange printed.
    """
    if not universe:
        return pl.DataFrame(schema=BAR_SCHEMA)
    records = (
        await session.execute(
            select(
                OhlcvDaily.instrument_id,
                Instrument.symbol,
                OhlcvDaily.date,
                OhlcvDaily.open,
                OhlcvDaily.high,
                OhlcvDaily.low,
                OhlcvDaily.close,
                OhlcvDaily.close_raw,
                OhlcvDaily.volume,
                OhlcvDaily.upper_circuit,
                OhlcvDaily.adj_factor,
            )
            .join(Instrument, Instrument.id == OhlcvDaily.instrument_id)
            .where(
                OhlcvDaily.date >= start,
                OhlcvDaily.date <= as_of,
                OhlcvDaily.instrument_id.in_(universe),
            )
            .order_by(OhlcvDaily.instrument_id, OhlcvDaily.date)
        )
    ).all()
    if not records:
        return pl.DataFrame(schema=BAR_SCHEMA)
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
                "close_raw": float(record.close_raw)
                if record.close_raw is not None
                else float(record.close) / (factor or 1.0),
                "volume": float(record.volume),
                "upper_circuit": None if circuit is None else float(circuit) * factor,
                "adj_factor": factor,
            }
        )
    return pl.DataFrame(rows, schema=BAR_SCHEMA)


def _tick() -> Decimal:
    return Decimal(TICK_INR)


def _as_exchange_price(value: float | None, factor: float) -> float | None:
    """An adjusted level in today's money. ``None`` passes through."""
    if value is None:
        return None
    return value / factor if factor else value


def signal_rows(
    detected: pl.DataFrame, config: VbtConfig, *, tick: Decimal | None = None
) -> pl.DataFrame:
    """The scan hits, with their levels as exchange prices and their storage precision applied."""
    if detected.is_empty():
        return detected
    step = tick or _tick()
    rows: list[dict[str, object]] = []
    for row in detected.iter_rows(named=True):
        factor = float(row["adj_factor"] or 1.0)
        raw_close = float(row["close_raw"])
        limit = _snap(Decimal(str(raw_close)), step)
        rows.append(
            {
                **row,
                "limit_price": float(limit),
                "stop_price": float(initial_stop(limit, config.exits, step)),
                "sma_200": _as_exchange_price(row["sma_dma"], factor),
                "ema_21": _as_exchange_price(row["ema_exit"], factor),
                "high_20_prior": _as_exchange_price(row["high_prior"], factor),
                "ret_20_pct": row["ret_lookback_pct"],
                "vol_sma_50": row["vol_sma"],
                "turnover_avg_20": row["turnover_avg"],
                "rank_key": row["rank_key"],
            }
        )
    return apply_storage_precision(pl.DataFrame(rows))


def _snap(price: Decimal, tick: Decimal) -> Decimal:
    """The nearest tick at or below ``price`` — a limit is never rounded up into the spread."""
    return tick_floor(price, tick)


async def upsert_signals(
    session: AsyncSession,
    frame: pl.DataFrame,
    *,
    user_id: int,
    trade_date: dt.date,
    pipeline_run_id: int | None,
) -> int:
    """Idempotent: running the job twice for a date changes no row (house rule 7)."""
    if frame.is_empty():
        return 0
    payload = [
        {
            "user_id": user_id,
            "date": trade_date,
            "instrument_id": int(row["instrument_id"]),
            "state": str(row["state"]),
            "failed_filters": list(row["failed_filters"] or []),
            "open": _decimal(row["open"]),
            "high": _decimal(row["high"]),
            "low": _decimal(row["low"]),
            "close": _decimal(row["close"]),
            "close_raw": _decimal(row["close_raw"]),
            "adj_factor": _decimal(row["adj_factor"]),
            "limit_price": _decimal(row["limit_price"]),
            "stop_price": _decimal(row["stop_price"]),
            "change_pct": _decimal(row["change_pct"]),
            "rvol": _decimal(row["rvol"]),
            "close_position": _decimal(row["close_position"]),
            "ret_20_pct": _decimal(row["ret_20_pct"]),
            "volume": _int(row["volume"]),
            "vol_sma_50": _int(row["vol_sma_50"]),
            "turnover_inr": _int(row["turnover_inr"]),
            "turnover_avg_20": _int(row["turnover_avg_20"]),
            "sma_200": _decimal(row["sma_200"]),
            "ema_21": _decimal(row["ema_21"]),
            "high_20_prior": _decimal(row["high_20_prior"]),
            "rank_key": _int(row["rank_key"]) or 0,
            "locked_upper_circuit": bool(row["locked_upper_circuit"]),
            "bars_in_window": None,
            "pipeline_run_id": pipeline_run_id,
        }
        for row in frame.iter_rows(named=True)
    ]
    updatable = [key for key in payload[0] if key not in ("user_id", "date", "instrument_id")]
    chunk = max(1, MAX_BIND_PARAMS // max(len(payload[0]), 1))
    written = 0
    for offset in range(0, len(payload), chunk):
        batch = payload[offset : offset + chunk]
        statement = insert(VbSignalDaily).values(batch)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    VbSignalDaily.user_id,
                    VbSignalDaily.date,
                    VbSignalDaily.instrument_id,
                ],
                set_={name: getattr(statement.excluded, name) for name in updatable},
            )
        )
        written += len(batch)
    return written


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _int(value: object) -> int | None:
    if value is None:
        return None
    return int(float(str(value)))


async def write_breadth_row(  # noqa: PLR0913 - one keyword per input the row records
    session: AsyncSession,
    *,
    user_id: int,
    trade_date: dt.date,
    reading: BreadthReading | None,
    funnel: VbtFunnel,
    thin_session: bool,
    config: VbtConfig,
    pipeline_run_id: int | None,
) -> None:
    """One row per session, including the sessions the rules refused to trade.

    A thin session gets a row with a shut gate and zero counts rather than no row at all, so the
    history says *why* there was no signal instead of leaving a hole that reads like a job that
    failed.
    """
    values = {
        "user_id": user_id,
        "date": trade_date,
        "universe_count": funnel.with_a_bar,
        "measured_count": 0 if reading is None else reading.measured_count,
        "above_count": 0 if reading is None else reading.above_count,
        "pct_above_dma": Decimal("0.0000") if reading is None else reading.pct_above_dma,
        "gate": Gate.SHUT.value if reading is None else reading.gate.value,
        "dma_bars": config.breadth.dma_bars,
        "thin_session": thin_session,
        "detail": funnel.as_detail(),
        "pipeline_run_id": pipeline_run_id,
    }
    statement = insert(VbBreadthDaily).values(values)
    updatable = [key for key in values if key not in ("user_id", "date")]
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[VbBreadthDaily.user_id, VbBreadthDaily.date],
            set_={name: getattr(statement.excluded, name) for name in updatable},
        )
    )


async def run_detect_vbt(
    session: AsyncSession,
    outcome: StepOutcome,
    trade_date: dt.date,
    *,
    user_id: int,
    pipeline_run_id: int | None = None,
) -> int:
    """Detect the session's signals and write its breadth row. Returns the signal count.

    Returns rather than raises on a date with nothing to read: a non-trading day, or a date the
    pipeline has not published, is not a failure of this step.
    """
    config = await load_vbt_config(session, user_id)
    universe = await load_universe(session, config)
    start = await lookback_start(session, trade_date, LOOKBACK_SESSIONS)
    bars = await load_vbt_bars(session, start, trade_date, universe)
    if bars.is_empty():
        outcome.status = StepStatus.SKIPPED
        outcome.note(skipped_reason=f"no bars for {trade_date} in the VBT universe")
        return 0

    kept, calendar = drop_thin_sessions(bars, config)
    thin = trade_date in calendar.dropped
    if not thin and trade_date not in calendar.sessions:
        # The bars do not reach this date at all — an unpublished session, or a date in the
        # future. **Write nothing.** `03` §3 has this table record what the sleeve *saw* on a
        # session, and a row saying "0 of 0 names were above their average, gate SHUT" for a day
        # that has not closed is a false record in the table the evening job reads as its
        # calendar. A thin session below is different: it happened, it was thin, and the record
        # that the sleeve did not trade it is worth keeping.
        #
        # Found on the box, 11 Sep 2026: VB13.4's bug asked for today at 14:14 and left exactly
        # such a row behind. The nightly chain cannot reach here — it only ever runs the
        # published date — but `make vbt DATE=<anything>` can.
        outcome.status = StepStatus.SKIPPED
        outcome.note(
            skipped_reason=f"{trade_date} is not a session the bars know about",
            instruments=len(universe),
            bars=bars.height,
        )
        return 0

    if thin:
        funnel = VbtFunnel(
            instruments=len(universe),
            bars=bars.height,
            with_a_bar=calendar.counts.get(trade_date, 0),
            with_an_average=0,
            above_the_average=0,
            scan_hits=0,
            signals=0,
            thin_sessions_dropped=len(calendar.dropped),
        )
        await write_breadth_row(
            session,
            user_id=user_id,
            trade_date=trade_date,
            reading=None,
            funnel=funnel,
            thin_session=thin,
            config=config,
            pipeline_run_id=pipeline_run_id,
        )
        outcome.status = StepStatus.SKIPPED
        outcome.note(
            skipped_reason=(
                "a thin session — only "
                f"{calendar.counts.get(trade_date, 0)} names printed, so 04 §2.1 removed it from "
                "the rolling calendar and the sleeve did not trade it"
            ),
            **funnel.as_detail(),
        )
        return 0

    indicated = with_vbt_indicators(kept, calendar, config)
    reading = breadth_above_dma(indicated, trade_date, config)
    detected = detect_signals(indicated, trade_date, config)
    rows = signal_rows(detected, config)
    written = await upsert_signals(
        session,
        rows,
        user_id=user_id,
        trade_date=trade_date,
        pipeline_run_id=pipeline_run_id,
    )
    signals = int((rows["state"] == SignalState.SIGNAL.value).sum()) if not rows.is_empty() else 0
    funnel = VbtFunnel(
        instruments=len(universe),
        bars=bars.height,
        with_a_bar=reading.universe_count,
        with_an_average=reading.measured_count,
        above_the_average=reading.above_count,
        scan_hits=written,
        signals=signals,
        thin_sessions_dropped=len(calendar.dropped),
    )
    await write_breadth_row(
        session,
        user_id=user_id,
        trade_date=trade_date,
        reading=reading,
        funnel=funnel,
        thin_session=False,
        config=config,
        pipeline_run_id=pipeline_run_id,
    )
    outcome.rows_in = bars.height
    outcome.rows_out = written
    outcome.note(gate=reading.gate.value, **funnel.as_detail())
    if written == 0:
        outcome.status = StepStatus.SKIPPED
    return signals


async def published_signal_count(session: AsyncSession, user_id: int, on: dt.date) -> int:
    """How many rows this session already has — what the 21:00 retry asks before it works."""
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(VbSignalDaily)
                .where(VbSignalDaily.user_id == user_id, VbSignalDaily.date == on)
            )
        ).scalar_one()
    )


__all__ = [
    "BAR_SCHEMA",
    "LOOKBACK_SESSIONS",
    "SessionCalendar",
    "VbtFunnel",
    "etf_instrument_ids",
    "load_universe",
    "load_vbt_bars",
    "load_vbt_config",
    "lookback_start",
    "published_signal_count",
    "run_detect_vbt",
    "signal_rows",
    "upsert_signals",
    "write_breadth_row",
]
