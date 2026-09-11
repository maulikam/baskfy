"""TW4 — the nightly three-weeks-tight detection job (``docs/twt/06-module-plan.md`` § TW4).

Every trading session, from bars the chain has already published, this writes what TWT-1 saw:
one ``tw_state_daily`` row per name that **holds** the tight state (about fifty a day), one
``tw_signal_daily`` row per **entry event** inside that state (about eighteen a year), one
``tw_breadth_daily`` row saying whether the tape allowed a new entry at all — and, for every
position the sleeve still holds, **tomorrow's trailing trigger, computed tonight**.

WHERE THE ARITHMETIC LIVES
--------------------------
Not here. ``baskfy_core.twt`` computes the calendar, every indicator, the three weekly closes,
the month-3 low, the state, the entry event, the breadth reading and the ratchet; this module
reads rows, hands them over as a DataFrame, and writes what comes back. The same split as
``tasks.vbt`` beside it and ``compute_market_health`` before both, and for the same reason: the
backtest (TW2, TW9) and the nightly job must not be able to disagree about what a signal is.

FIVE THINGS THIS MODULE DOES THAT THE CORE CANNOT
-------------------------------------------------
**It reads the universe.** ``04`` §1's ETF exclusion needs the ``etf`` index universe, which is a
table; law 1 forbids core from touching one. ``04`` §1.2's *keep* of a null series is applied
here too — a delisted name with no current listing row stays in, because dropping today's list
would drop the history of names later demoted, which is a survivorship bias in reverse.

**It turns adjusted levels back into exchange prices.** Every rule reads the adjusted series, so
a split inside a 200-session window does not fake a tight base. But ``entry_reference_close`` is
a number a person reads a fill against and ``stop_preview`` is a level a broker would be sent, so
both are divided by the row's ``adj_factor`` (``03`` §10) and the factor is stored beside them.
``tw_state_daily`` deliberately stores the **adjusted** bar and the **adjusted** comparands —
``week_close_0/1/2``, ``month_low_3``, ``sma_dma`` — because ``week_range_pct``,
``month_low_ratio`` and the breadth funnel must all remain auditable from the stored row, and a
ratio whose numerator was converted and whose denominator was not is a different number.

``upper_circuit`` goes the other way, and it is the subtle one: the band is an exchange print
with no adjusted twin, while ``high`` has been adjusted in place, so on the morning after a 1:2
split an unconverted comparison would report the whole market locked. It is multiplied by the
row's factor on the way *in*, so ``03`` §2's ``upper_circuit > 0 and high >= upper_circuit``
happens in one space.

**It counts the funnel.** "No signals today" is only useful with "…out of 4,186 names, 1,412 of
which have a 200-day average, 47 of which held the state". The counts go on the pipeline step and
into ``tw_breadth_daily.detail``, so an operator reading ``/admin/pipeline`` at 21:00 can tell a
quiet day from a broken one — and this book signals about eighteen times a **year**, so a run of
empty days is the normal case rather than the alarming one.

**It records a thin session as a thin session.** ``04`` §2.1's rule drops muhurat and special
Saturdays from the rolling calendar. Rather than writing nothing on such a day, the job writes a
breadth row with ``thin_session = true``, a shut gate and null percentages, so the history says
*why* there was no signal instead of leaving a hole that looks like a failed job. It does not
ratchet on such a session either: a session the strategy does not count is not a close the trail
may move on.

**It does the ratchet's arithmetic the night before.** ``04`` §7.2 for every ``OPEN`` position:
``high_since`` raised by the session's high, ``next_trigger`` tick-floored and clamped under the
close, stored with the session it was computed for. ``04`` §7.3's corporate-action branch when
the as-of factor differs from the position's ``entry_adj_factor`` — and its **refusal**: a
re-derived trigger below the resting stop is not written at all, because cancelling a resting
stop and arming a lower one is the one thing this sleeve must never do on its own (TW0.7).
**Nothing here arms, cancels or places anything**, and ``stop_price`` is never moved: the ratchet
is a plan line a person confirms (``02`` Track C §3), and this job only does its arithmetic.

UPSERT, NEVER DELETE-AND-REINSERT
---------------------------------
``tw_order.signal_date`` is a real composite foreign key into ``tw_signal_daily`` (``03`` §6), so
once an order references a signal row the row cannot be deleted — a re-run that cleared the day
first would fail on the one day it mattered most. Every write below is
``INSERT … ON CONFLICT DO UPDATE``. House rule 7: running the job twice for a date changes no
rows. TW3's finding, recorded for this module.

THE STEP CANNOT FAIL THE NIGHT
------------------------------
``COMPUTE_TWT`` runs after ``publish``, after ``compute_swing`` and after ``compute_vbt``, and
like all three it records its own failure and returns rather than raising (M30's rule for
post-publish steps, DECISIONS-VB VB0.5, DECISIONS-TW TW4.5). A sleeve that could hold back
``data_version`` would make a screener outage out of a detector bug.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Final

import polars as pl
from sqlalchemy import Select, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import (
    IndexDef,
    IndexMemberDaily,
    Instrument,
    OhlcvDaily,
    TradingDay,
    TwBreadthDaily,
    TwConfig,
    TwPosition,
    TwSignalDaily,
    TwStateDaily,
)
from baskfy_core.models.base import JsonObject
from baskfy_core.precision import COLUMN_PRECISION, apply_storage_precision, quantise
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.twt.breadth import BreadthReading, breadth_above_dma
from baskfy_core.twt.calendar import SessionCalendar, drop_thin_sessions
from baskfy_core.twt.config import (
    DEFAULT_TWT_CONFIG,
    TICK_INR,
    Gate,
    SignalState,
    TwtConfig,
)
from baskfy_core.twt.exits import Bar, OpenPosition, initial_stop, on_adjustment, ratchet
from baskfy_core.twt.signals import detect_signals, weekly_column, with_twt_columns
from baskfy_worker.steps import StepOutcome, StepStatus

log = logging.getLogger(__name__)

#: How many trading sessions of history the detector is given. ``04`` §2.3 states it rather than
#: deriving it: 200 for the average, plus a quarter's slack for the month-3 low and the thin
#: sessions the calendar drops. It is a field of the config, not a number written here.
LOOKBACK_SESSIONS: Final[int] = DEFAULT_TWT_CONFIG.data.bars_required

#: PostgreSQL caps one statement at 32,767 bound parameters and ``tw_state_daily`` is ~22 columns
#: wide. The same arithmetic as ``tasks.vbt.MAX_BIND_PARAMS``, for the same reason.
MAX_BIND_PARAMS: Final = 32_767

#: ``03`` §3: the one filter that can reject an entry event is the liquidity floor.
TURNOVER_FILTER: Final[str] = "TURNOVER"

#: The frame the loader returns and ``with_twt_columns`` reads — typed even when a column is all
#: null (a month with no circuit band), so a frame built elsewhere in the same shape concatenates
#: without a cast failing on a ``Null`` column.
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
        "is_etf": pl.Boolean,
    }
)

_OVER: Final = "instrument_id"
_DATE: Final = "date"


@dataclass(frozen=True, slots=True)
class TwtFunnel:
    """What happened to the universe on the way to a signal (``03`` §4's ``detail``).

    ``universe -> with a bar -> with a 200-DMA -> above it -> in state -> entries -> signals``,
    and the thin sessions the window dropped.

    An empty result is the **normal** answer here: the research's nine years produced 164 trades,
    which is about eighteen entries a year, so most sessions have states and no entries at all.
    The only way to tell that from a job that silently read nothing is to say how many names were
    at each stage.
    """

    instruments: int
    bars: int
    with_a_bar: int
    with_a_dma: int
    above_the_dma: int
    in_state: int
    entries: int
    signals: int
    dropped_thin_sessions: int

    def as_detail(self) -> dict[str, object]:
        return {
            "instruments": self.instruments,
            "bars": self.bars,
            "with_a_bar": self.with_a_bar,
            "with_a_dma": self.with_a_dma,
            "above_the_dma": self.above_the_dma,
            "in_state": self.in_state,
            "entries": self.entries,
            "signals": self.signals,
            "dropped_thin_sessions": self.dropped_thin_sessions,
        }


@dataclass(frozen=True, slots=True)
class RatchetReport:
    """What the evening's arithmetic did to the book (``04`` §7.2, §7.3).

    ``raised`` is the number that says whether the sleeve's one new mechanism actually ran:
    ``03`` §8 keeps it as a column of its own because a book of ten open lines in a rising market
    should ratchet most nights, and a week of zeroes with the market up is a bug rather than a
    quiet spell.
    """

    positions: int
    raised: int
    adjusted: int
    adjustment_alerts: int
    without_a_bar: int

    def as_detail(self) -> dict[str, object]:
        return {
            "positions": self.positions,
            "ratchets": self.raised,
            "adjusted": self.adjusted,
            "adjustment_alerts": self.adjustment_alerts,
            "positions_without_a_bar": self.without_a_bar,
        }


async def load_twt_config(session: AsyncSession, user_id: int) -> TwtConfig:
    """``DEFAULT_TWT_CONFIG`` with this user's two *detector* settings applied.

    Only ``stop_pct`` and ``trail_pct`` reach this job: the first decides ``stop_preview`` on a
    signal row and the second decides tomorrow's trigger. ``sleeve_capital_inr``,
    ``max_open_positions`` and ``max_position_pct`` decide what may be **bought**, not what is a
    signal, so they are read where the plan is built (TW5, TW6) and deliberately not here — a
    detector whose output moved when somebody changed a position cap would make
    ``tw_state_daily`` a record of the settings rather than of the tape.

    A user with no ``tw_config`` row gets the defaults rather than an error: the detector writes
    no money and a missing settings row is a seeding problem, not a reason to skip a night.
    """
    row = (
        await session.execute(select(TwConfig).where(TwConfig.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        return DEFAULT_TWT_CONFIG
    return replace(
        DEFAULT_TWT_CONFIG,
        exits=replace(
            DEFAULT_TWT_CONFIG.exits,
            stop_pct=Decimal(str(row.stop_pct)),
            trail_pct=Decimal(str(row.trail_pct)),
        ),
    )


async def lookback_start(session: AsyncSession, as_of: dt.date, sessions: int) -> dt.date:
    """The date ``sessions`` trading days before ``as_of``, from the calendar rather than by
    subtracting days.

    Counting calendar days would be wrong by about a third: 260 trading days is roughly 375
    calendar days, and the error is not constant. The calendar is a table for exactly this. (The
    swing book and VBT-1 each have the same helper; neither imports the other, because a sleeve
    reaching into another sleeve's task module is how two books come to share a bug.)
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
        # calendar produces a short window (and no state, visibly) rather than one bar and a
        # detector that silently agrees there is nothing to see.
        return as_of - dt.timedelta(days=sessions * 2)
    return min(dates)


async def etf_instrument_ids(
    session: AsyncSession, config: TwtConfig = DEFAULT_TWT_CONFIG
) -> set[int]:
    """Every instrument that has **ever** been in the ``etf`` universe (``04`` §1.3).

    Ever, not point-in-time, and that is deliberate: this is an exclusion of a *kind of
    instrument*, not a statement about an index's membership on a date. A fund that joined the
    list last year was a fund the year before too, and a basket is not a company with a base.
    """
    rows = await session.execute(
        select(IndexMemberDaily.instrument_id)
        .join(IndexDef, IndexDef.id == IndexMemberDaily.index_id)
        .where(IndexDef.slug == config.data.etf_universe_slug)
        .distinct()
    )
    return {int(instrument_id) for (instrument_id,) in rows}


def _universe_query(config: TwtConfig) -> Select[tuple[int, str, str]]:
    """``04`` §1.1 and §1.2, as one SELECT.

    The ``series IS NULL`` arm is ``keep_null_series`` [True] and is the half VBT-1's own loader
    does not carry: a delisted name has no current listing row and therefore no series, and
    dropping it would drop the history of every name later demoted.
    """
    data = config.data
    series = Instrument.series.in_(list(data.series_allowed))
    admitted = or_(series, Instrument.series.is_(None)) if data.keep_null_series else series
    return select(Instrument.id, Instrument.symbol, Instrument.name).where(
        Instrument.instrument_type == data.instrument_type,
        admitted,
    )


async def load_universe(session: AsyncSession, config: TwtConfig = DEFAULT_TWT_CONFIG) -> set[int]:
    """The instrument ids ``04`` §1 admits: NSE cash, EQ/BE/BZ (or none), no ETFs, no SME.

    ``BE`` and ``BZ`` stay in because every trade here is delivery anyway. The two ETF patterns
    are narrow on purpose (``04`` §1.3): "GOLD" would flag GOLDIAM.
    """
    data = config.data
    name_pattern = re.compile(data.etf_name_pattern, re.IGNORECASE)
    symbol_pattern = re.compile(data.etf_symbol_pattern, re.IGNORECASE)
    etfs = await etf_instrument_ids(session, config)
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


async def load_twt_bars(
    session: AsyncSession, start: dt.date, as_of: dt.date, universe: set[int]
) -> pl.DataFrame:
    """The frame ``with_twt_columns`` expects, in the adjusted space.

    ``upper_circuit`` is multiplied by the row's ``adj_factor`` on the way in, for the reason the
    module docstring gives. ``close_raw`` is carried untouched: ``04`` §3.1's line 1 is a
    statement about what the exchange printed, and so is §3.5's rupee turnover.

    ``is_etf`` is False for every row because :func:`load_universe` has already refused them.
    It is passed rather than left absent so that ``tight_state``'s ``~is_etf`` term is reading a
    column this module filled, not a default the core invented for a hand-built fixture.
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
                "open": None if record.open is None else float(record.open),
                "high": None if record.high is None else float(record.high),
                "low": None if record.low is None else float(record.low),
                "close": float(record.close),
                "close_raw": float(record.close_raw)
                if record.close_raw is not None
                else float(record.close) / (factor or 1.0),
                "volume": float(record.volume),
                "upper_circuit": None if circuit is None else float(circuit) * factor,
                "adj_factor": factor,
                "is_etf": False,
            }
        )
    return pl.DataFrame(rows, schema=BAR_SCHEMA)


def _tick() -> Decimal:
    return Decimal(TICK_INR)


def _as_exchange_price(value: float | None, factor: float) -> float | None:
    """An adjusted level in today's money (``03`` §10). ``None`` passes through."""
    if value is None:
        return None
    return value / factor if factor else value


def with_storage_columns(
    detected: pl.DataFrame, config: TwtConfig = DEFAULT_TWT_CONFIG
) -> pl.DataFrame:
    """The two columns ``03`` §2 wants that no rule reads: ``sessions_in_state`` and
    ``bars_in_window``.

    ``sessions_in_state`` is how many consecutive sessions **of the run's own calendar**,
    including this one, the state has held — ``1`` on an entry day, which is what makes "how long
    has this been tight" a read rather than a re-detection. It is the distance from the last
    session on which the state was false, so a name whose state has held since its first bar
    counts every session it has existed for.

    ``bars_in_window`` is how many bars the 200-session window actually held, so ``04`` §2.2's
    10 % tolerance is auditable from the stored row rather than asserted in a document.
    """
    position = pl.int_range(pl.len())
    last_false = (
        pl.when(~pl.col("tight_state")).then(position).otherwise(None).forward_fill().fill_null(-1)
    )
    return detected.sort([_OVER, _DATE]).with_columns(
        (position - last_false).over(_OVER).cast(pl.Int64).alias("sessions_in_state"),
        pl.col("close")
        .is_not_null()
        .cast(pl.Int64)
        .rolling_sum(config.breadth.dma_bars, min_samples=1)
        .over(_OVER)
        .cast(pl.Int64)
        .alias("bars_in_window"),
    )


def state_rows(detected: pl.DataFrame, as_of: dt.date) -> pl.DataFrame:
    """The names holding the state on ``as_of``, with their storage precision applied.

    Everything here is the **adjusted** series and the adjusted comparands, deliberately: ``03``
    §2's ``week_range_pct`` and ``month_low_ratio`` have to stay derivable from the columns
    beside them, and a ratio whose numerator was converted to exchange prices and whose
    denominator was not is a different number. ``adj_factor`` rides along so any of them can be
    turned back into an exchange price by a reader who wants one.
    """
    held = detected.filter((pl.col(_DATE) == as_of) & pl.col("tight_state"))
    if held.is_empty():
        return held
    circuit = pl.col("upper_circuit")
    rows = held.with_columns(
        pl.col(weekly_column(0)).alias("week_close_0"),
        pl.col(weekly_column(1)).alias("week_close_1"),
        pl.col(weekly_column(2)).alias("week_close_2"),
        pl.col("tight_range_pct").alias("week_range_pct"),
        pl.col("month_low_back").alias("month_low_3"),
        (pl.col("close") / pl.col("month_low_back")).alias("month_low_ratio"),
        pl.col("vol_sma").alias("vol_sma_50"),
        (
            circuit.is_not_null()
            & (circuit > 0)
            & pl.col("high").is_not_null()
            & (pl.col("high") >= circuit)
        )
        .fill_null(value=False)
        .alias("locked_upper_circuit"),
    )
    return apply_storage_precision(rows)


def signal_rows(
    detected: pl.DataFrame,
    as_of: dt.date,
    config: TwtConfig = DEFAULT_TWT_CONFIG,
    *,
    tick: Decimal | None = None,
) -> pl.DataFrame:
    """The session's entry events — ``SIGNAL`` rows *and* ``SCAN_ONLY`` rows (``04`` §3.5).

    An entry event below the liquidity floor is stored, never dropped: the funnel is the argument
    for the floor, and a system that stores only what it accepted cannot show a person what it
    passed over.

    ``entry_reference_close`` and ``stop_preview`` are the two numbers on this table that are
    **exchange prices** — the first is read against tomorrow's fill and the second is a preview of
    a level a broker would be sent — so both are ``level / adj_factor`` (``03`` §10) and the stop
    is snapped down to the exchange's tick.
    """
    events = detect_signals(detected, as_of, config)
    if events.is_empty():
        return events
    step = tick or _tick()
    rows: list[dict[str, object]] = []
    for row in events.iter_rows(named=True):
        factor = float(row["adj_factor"] or 1.0)
        # `close` is non-null on every row of the state — `04` §3.1's lines 1, 2 and 3 all read
        # it and a null on either side of a comparison is a fail — so this conversion needs no
        # None branch, and writing one would be a branch no test could reach.
        close = float(row["close"])
        reference = close / factor if factor else close
        stop = initial_stop(Decimal(str(reference)), config.exits, step)
        out_before = row["sessions_out_before"]
        rows.append(
            {
                **row,
                "entry_reference_close": reference,
                "stop_preview": float(stop),
                # A name that has **never** held the state has no `sessions_out_before` (TW1.4):
                # the research's counter is "sessions since the state was last true" and there is
                # no last. `03` §3 wants a number and the honest one is every session the name
                # has been listed and out, which the entry event has already required to be at
                # least `entry_min_sessions_out`. DECISIONS-TW TW4.2.
                "sessions_out_before": int(
                    out_before if out_before is not None else (row["sessions_listed"] or 1) - 1
                ),
                "failed_filters": []
                if row["signal_state"] == SignalState.SIGNAL.value
                else [TURNOVER_FILTER],
            }
        )
    return apply_storage_precision(pl.DataFrame(rows))


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _int(value: object) -> int | None:
    if value is None:
        return None
    return int(float(str(value)))


async def _upsert(
    session: AsyncSession,
    table: type[TwStateDaily] | type[TwSignalDaily],
    payload: list[dict[str, object]],
) -> int:
    """``INSERT … ON CONFLICT DO UPDATE`` in bind-parameter-sized chunks.

    **Never a delete first.** ``tw_order.signal_date`` is a composite foreign key into
    ``tw_signal_daily`` (``03`` §6), so clearing the day before rewriting it would fail the moment
    an order referenced one of its rows — which is the day a re-run matters most. TW3 found it;
    this is the module that had to know.
    """
    if not payload:
        return 0
    updatable = [key for key in payload[0] if key not in ("user_id", "date", "instrument_id")]
    chunk = max(1, MAX_BIND_PARAMS // max(len(payload[0]), 1))
    written = 0
    for offset in range(0, len(payload), chunk):
        batch = payload[offset : offset + chunk]
        statement = insert(table).values(batch)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[table.user_id, table.date, table.instrument_id],
                set_={name: getattr(statement.excluded, name) for name in updatable},
            )
        )
        written += len(batch)
    return written


async def upsert_states(
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
    payload: list[dict[str, object]] = [
        {
            "user_id": user_id,
            "date": trade_date,
            "instrument_id": int(row["instrument_id"]),
            "open": _decimal(row["open"]),
            "high": _decimal(row["high"]),
            "low": _decimal(row["low"]),
            "close": _decimal(row["close"]),
            "close_raw": _decimal(row["close_raw"]),
            "adj_factor": _decimal(row["adj_factor"]),
            "week_close_0": _decimal(row["week_close_0"]),
            "week_close_1": _decimal(row["week_close_1"]),
            "week_close_2": _decimal(row["week_close_2"]),
            "week_range_pct": _decimal(row["week_range_pct"]),
            "month_low_3": _decimal(row["month_low_3"]),
            "month_low_ratio": _decimal(row["month_low_ratio"]),
            "vol_sma_50": _int(row["vol_sma_50"]),
            "volume": _int(row["volume"]),
            "turnover_inr": _int(row["turnover_inr"]),
            "turnover_avg_20": _int(row["turnover_avg_20"]),
            "sma_dma": _decimal(row["sma_dma"]),
            "sessions_in_state": _int(row["sessions_in_state"]),
            "bars_in_window": _int(row["bars_in_window"]),
            "locked_upper_circuit": bool(row["locked_upper_circuit"]),
            "pipeline_run_id": pipeline_run_id,
        }
        for row in frame.iter_rows(named=True)
    ]
    return await _upsert(session, TwStateDaily, payload)


async def upsert_signals(
    session: AsyncSession,
    frame: pl.DataFrame,
    *,
    user_id: int,
    trade_date: dt.date,
    pipeline_run_id: int | None,
) -> int:
    """Idempotent, and an upsert rather than a rewrite for the reason :func:`_upsert` gives."""
    if frame.is_empty():
        return 0
    payload: list[dict[str, object]] = [
        {
            "user_id": user_id,
            "date": trade_date,
            "instrument_id": int(row["instrument_id"]),
            "state": str(row["signal_state"]),
            "failed_filters": list(row["failed_filters"] or []),
            "entry_reference_close": _decimal(row["entry_reference_close"]),
            "stop_preview": _decimal(row["stop_preview"]),
            "sessions_out_before": _int(row["sessions_out_before"]),
            "rank_key": _int(row["rank_key"]) or 0,
            "turnover_avg_20": _int(row["turnover_avg_20"]),
            "pipeline_run_id": pipeline_run_id,
        }
        for row in frame.iter_rows(named=True)
    ]
    return await _upsert(session, TwSignalDaily, payload)


async def write_breadth_row(  # noqa: PLR0913 - one keyword per input the row records
    session: AsyncSession,
    *,
    user_id: int,
    trade_date: dt.date,
    reading: BreadthReading | None,
    funnel: TwtFunnel,
    thin_session: bool,
    config: TwtConfig,
    pipeline_run_id: int | None,
) -> None:
    """One row per session, including the sessions the rules refused to trade.

    A **thin** session gets a row with a shut gate and null percentages (``03`` §4), so the
    history says *why* there was no signal instead of leaving a hole that reads like a job that
    failed. A session that was measured always carries its percentage, and a measured session
    with a zero denominator carries ``0.0000`` and a shut gate (``04`` §4.2) — the two cases the
    table's own CHECK constraints keep apart.
    """
    values: dict[str, object] = {
        "user_id": user_id,
        "date": trade_date,
        "universe_count": funnel.with_a_bar,
        "measured_count": 0 if reading is None else reading.measured_count,
        "above_count": 0 if reading is None else reading.above_count,
        "pct_above_dma": None
        if thin_session
        else Decimal("0.0000")
        if reading is None
        else reading.pct_above_dma,
        "gate": Gate.SHUT.value if reading is None else reading.gate.value,
        "dma_bars": config.breadth.dma_bars,
        "thin_session": thin_session,
        "detail": funnel.as_detail(),
        "pipeline_run_id": pipeline_run_id,
    }
    statement = insert(TwBreadthDaily).values(values)
    updatable = [key for key in values if key not in ("user_id", "date")]
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[TwBreadthDaily.user_id, TwBreadthDaily.date],
            set_={name: getattr(statement.excluded, name) for name in updatable},
        )
    )


# --- the ratchet: tomorrow's trigger, computed tonight -------------------------


@dataclass(frozen=True, slots=True)
class SessionBar:
    """One instrument's as-of bar, in **exchange prices** and with its factor.

    ``04`` §7.2 works in exchange prints throughout — ``high_since`` is one, the resting GTT
    trigger is one — while ``ohlcv_daily`` stores the adjusted series. The conversion happens
    once, here, so the ratchet never sees an adjusted number.
    """

    instrument_id: int
    adj_factor: Decimal
    bar: Bar


def _exchange(value: Decimal | None, factor: Decimal) -> Decimal | None:
    if value is None:
        return None
    return value / factor if factor else value


async def load_session_bars(
    session: AsyncSession, instrument_ids: list[int], trade_date: dt.date
) -> dict[int, SessionBar]:
    """The as-of bar of every instrument the book holds, converted to exchange prices."""
    if not instrument_ids:
        return {}
    rows = (
        await session.execute(
            select(
                OhlcvDaily.instrument_id,
                OhlcvDaily.open,
                OhlcvDaily.high,
                OhlcvDaily.low,
                OhlcvDaily.close,
                OhlcvDaily.close_raw,
                OhlcvDaily.adj_factor,
            ).where(
                OhlcvDaily.date == trade_date,
                OhlcvDaily.instrument_id.in_(instrument_ids),
            )
        )
    ).all()
    bars: dict[int, SessionBar] = {}
    for row in rows:
        factor = Decimal(str(row.adj_factor)) if row.adj_factor is not None else Decimal(1)
        close = (
            Decimal(str(row.close_raw))
            if row.close_raw is not None
            else _exchange(Decimal(str(row.close)), factor)
        )
        bars[int(row.instrument_id)] = SessionBar(
            instrument_id=int(row.instrument_id),
            adj_factor=factor,
            bar=Bar(
                session=trade_date,
                open=_exchange(None if row.open is None else Decimal(str(row.open)), factor),
                high=_exchange(None if row.high is None else Decimal(str(row.high)), factor),
                low=_exchange(None if row.low is None else Decimal(str(row.low)), factor),
                close=close,
            ),
        )
    return bars


async def rederived_high_since(
    session: AsyncSession, position: TwPosition, trade_date: dt.date, factor: Decimal
) -> Decimal:
    """``04`` §7.3 step 1: the hold's highest high, off the **newly adjusted** series, scaled by
    the new factor.

    The point is that the level means the same fraction of the price it meant yesterday. The
    adjusted series is what a corporate action rewrites, so reading it today and dividing by
    today's factor gives the hold's high **in today's exchange prices** — which is the space the
    resting GTT trigger is quoted in, and the space ``high_since`` is stored in.
    """
    highest = (
        await session.execute(
            select(func.max(OhlcvDaily.high)).where(
                OhlcvDaily.instrument_id == position.instrument_id,
                OhlcvDaily.date >= position.entry_date,
                OhlcvDaily.date <= trade_date,
            )
        )
    ).scalar_one_or_none()
    if highest is None:
        return position.high_since
    converted = _exchange(Decimal(str(highest)), factor)
    return converted if converted is not None else position.high_since


async def high_since_from_bars(
    session: AsyncSession, position: TwPosition, trade_date: dt.date
) -> Decimal:
    """``high_since`` recomputed from ``ohlcv_daily`` over the whole hold.

    ``03`` §5 explains why the column is stored rather than queried — the stop resting at the
    exchange was derived from a specific number on a specific evening — and then asks **this
    module** to assert that the stored number agrees with a recomputation. This is the
    recomputation; ``test_twt_detect.py`` is the assertion.

    It is the maximum of the fill price and every session's high over the hold, each converted to
    an exchange price by *its own* row's factor, which is what ``04`` §7.2 accumulates one close
    at a time.
    """
    rows = (
        await session.execute(
            select(OhlcvDaily.high, OhlcvDaily.adj_factor).where(
                OhlcvDaily.instrument_id == position.instrument_id,
                OhlcvDaily.date >= position.entry_date,
                OhlcvDaily.date <= trade_date,
                OhlcvDaily.high.is_not(None),
            )
        )
    ).all()
    highest = position.entry_avg
    for high, factor in rows:
        converted = _exchange(
            Decimal(str(high)), Decimal(str(factor)) if factor is not None else Decimal(1)
        )
        if converted is not None and converted > highest:
            highest = converted
    return highest


def _store(value: Decimal | None, column: str) -> Decimal | None:
    """House rule 8: round at write time, at the precision the column's own table declares."""
    return quantise(value, COLUMN_PRECISION[column])


async def run_twt_ratchet(
    session: AsyncSession,
    trade_date: dt.date,
    *,
    user_id: int,
    config: TwtConfig,
    tick: Decimal | None = None,
) -> RatchetReport:
    """``04`` §7.2 and §7.3 for every ``OPEN`` position, the night before the morning needs it.

    What is written: ``high_since``, ``high_since_date``, ``next_trigger`` and
    ``next_trigger_for``. What is **not**: ``stop_price``, ``gtt_trigger``, ``gtt_id``. The
    ratchet is a plan line a person confirms (``02`` Track C §3) — this job does its arithmetic
    and nothing else, and the stop in force moves only when the desk has actually replaced the
    resting order.

    ``next_trigger`` is written **only when it exceeds the stop in force** (``03`` §5: "null when
    it does not exceed ``gtt_trigger``"), and the stop in force is read as the greater of
    ``stop_price`` and the resting ``gtt_trigger`` — the two can differ for a session while a
    raise is in flight, and the safe reading of "a stop never falls" is the higher of them.

    ``04`` §7.3's branch fires when the as-of row's factor differs from the position's
    ``entry_adj_factor``. It uses :func:`baskfy_core.twt.exits.on_adjustment`, which derives its
    trigger from the re-derived high **alone** (TW1.3) and therefore *can* come out below the
    resting stop — and when it does, **nothing is written**: the position keeps its resting stop,
    the alert is what happens instead, and the person who has to look at a pre-split trigger on a
    post-split instrument is the one who decides (TW0.7).
    """
    step = tick or _tick()
    positions = list(
        (
            await session.execute(
                select(TwPosition).where(
                    TwPosition.user_id == user_id,
                    TwPosition.state == "OPEN",
                    TwPosition.quantity_open > 0,
                )
            )
        )
        .scalars()
        .all()
    )
    if not positions:
        return RatchetReport(0, 0, 0, 0, 0)
    bars = await load_session_bars(
        session, [int(position.instrument_id) for position in positions], trade_date
    )
    raised = adjusted = alerts = missing = 0
    for position in positions:
        found = bars.get(int(position.instrument_id))
        if found is None or found.bar.close is None:
            missing += 1
            continue
        resting = (
            position.stop_price
            if position.gtt_trigger is None
            else max(position.stop_price, position.gtt_trigger)
        )
        book = OpenPosition(
            instrument_id=int(position.instrument_id),
            entry_date=position.entry_date,
            fill_price=position.entry_avg,
            quantity=int(position.quantity_open),
            stop_price=resting,
            initial_stop=position.initial_stop,
            high_since=position.high_since,
        )
        if found.adj_factor != position.entry_adj_factor:
            adjusted += 1
            high = await rederived_high_since(session, position, trade_date, found.adj_factor)
            outcome = on_adjustment(book, high, found.bar, config.exits, step)
            high_since = outcome.high_since
            trigger = outcome.next_trigger if outcome.emit_raise else None
            alerts += 1 if outcome.alert else 0
            raised += 1 if outcome.emit_raise else 0
        else:
            moved = ratchet(book, found.bar, config.exits, step)
            if moved is None:  # pragma: no cover - guarded by the `close is None` check above
                missing += 1
                continue
            high_since = moved.high_since
            trigger = moved.next_trigger if moved.raises else None
            raised += 1 if moved.raises else 0
        stored_high = _store(high_since, "high_since")
        if stored_high is not None and stored_high != position.high_since:
            position.high_since = stored_high
            position.high_since_date = trade_date
        position.next_trigger = _store(trigger, "next_trigger")
        position.next_trigger_for = trade_date
    await session.flush()
    return RatchetReport(
        positions=len(positions),
        raised=raised,
        adjusted=adjusted,
        adjustment_alerts=alerts,
        without_a_bar=missing,
    )


# --- the step ------------------------------------------------------------------


def _thin_funnel(
    universe: set[int], bars: pl.DataFrame, calendar: SessionCalendar, trade_date: dt.date
) -> TwtFunnel:
    return TwtFunnel(
        instruments=len(universe),
        bars=bars.height,
        with_a_bar=calendar.counts.get(trade_date, 0),
        with_a_dma=0,
        above_the_dma=0,
        in_state=0,
        entries=0,
        signals=0,
        dropped_thin_sessions=len(calendar.dropped),
    )


async def run_detect_twt(
    session: AsyncSession,
    outcome: StepOutcome,
    trade_date: dt.date,
    *,
    user_id: int,
    pipeline_run_id: int | None = None,
) -> int:
    """Detect the session's state, its entry events and its breadth row, then ratchet the book.

    Returns the number of full ``SIGNAL`` rows — which on most sessions is zero, because this
    strategy enters about eighteen times a year.

    Returns rather than raises on a date with nothing to read: a non-trading day, or a date the
    pipeline has not published, is not a failure of this step. ``04`` §11.1 is the clock — the
    signal and the breadth reading come from the **last completed session** and never from a day
    still running, and the root ``CLAUDE.md`` records two attempts to move that boundary that had
    to be reverted.
    """
    config = await load_twt_config(session, user_id)
    universe = await load_universe(session, config)
    start = await lookback_start(session, trade_date, LOOKBACK_SESSIONS)
    bars = await load_twt_bars(session, start, trade_date, universe)
    if bars.is_empty():
        outcome.status = StepStatus.SKIPPED
        outcome.note(skipped_reason=f"no bars for {trade_date} in the TWT universe")
        return 0

    kept, calendar = drop_thin_sessions(bars, config)
    thin = trade_date in calendar.dropped
    if not thin and trade_date not in calendar.sessions:
        # The bars do not reach this date at all — an unpublished session, or a date in the
        # future. **Write nothing.** `03` §2 has these tables record what the sleeve *saw* on a
        # session, and a row saying "0 of 0 names were above their average, gate SHUT" for a day
        # that has not closed is a false record in the table the evening job reads as its
        # calendar. The nightly chain cannot reach here — it only ever runs the published date —
        # but `make twt DATE=<anything>` can, and VB13.4 is the bug that proved it.
        outcome.status = StepStatus.SKIPPED
        outcome.note(
            skipped_reason=f"{trade_date} is not a session the bars know about",
            instruments=len(universe),
            bars=bars.height,
        )
        return 0

    if thin:
        # `04` §2.1: a muhurat session is not a trading session for this strategy. The row is
        # written for the record; the book does **not** ratchet on a close the calendar does not
        # count, because a trail that moved on a 200-name session would be a stop derived from a
        # day the strategy says did not happen. DECISIONS-TW TW4.4.
        funnel = _thin_funnel(universe, bars, calendar, trade_date)
        await write_breadth_row(
            session,
            user_id=user_id,
            trade_date=trade_date,
            reading=None,
            funnel=funnel,
            thin_session=True,
            config=config,
            pipeline_run_id=pipeline_run_id,
        )
        outcome.status = StepStatus.SKIPPED
        outcome.note(
            skipped_reason=(
                "a thin session — only "
                f"{calendar.counts.get(trade_date, 0)} names printed, so 04 §2.1 removed it from "
                "the rolling calendar and the sleeve neither traded nor ratcheted on it"
            ),
            **funnel.as_detail(),
        )
        return 0

    detected = with_storage_columns(with_twt_columns(kept, calendar, config), config)
    reading = breadth_above_dma(detected, trade_date, config)
    states = state_rows(detected, trade_date)
    signals = signal_rows(detected, trade_date, config)
    states_written = await upsert_states(
        session, states, user_id=user_id, trade_date=trade_date, pipeline_run_id=pipeline_run_id
    )
    entries_written = await upsert_signals(
        session, signals, user_id=user_id, trade_date=trade_date, pipeline_run_id=pipeline_run_id
    )
    full = (
        int((signals["signal_state"] == SignalState.SIGNAL.value).sum())
        if not signals.is_empty()
        else 0
    )
    funnel = TwtFunnel(
        instruments=len(universe),
        bars=bars.height,
        with_a_bar=reading.universe_count,
        with_a_dma=reading.measured_count,
        above_the_dma=reading.above_count,
        in_state=states_written,
        entries=entries_written,
        signals=full,
        dropped_thin_sessions=len(calendar.dropped),
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
    book = await run_twt_ratchet(session, trade_date, user_id=user_id, config=config)
    outcome.rows_in = bars.height
    outcome.rows_out = states_written + entries_written
    outcome.note(gate=reading.gate.value, **funnel.as_detail(), **book.as_detail())
    if states_written == 0 and book.positions == 0:
        outcome.status = StepStatus.SKIPPED
    return full


async def detect_session(
    session: AsyncSession, day: dt.date, *, user_id: int, force: bool = False
) -> JsonObject:
    """One session, detected unless it has already been — **the 21:00 retry's whole rule.**

    The Beat task and ``make twt`` both call this, so the two cannot come to disagree about when
    a session counts as done. ``force`` is the CLI's ``--force``: re-detect a date that already
    has rows, which is what a threshold change needs and which is still idempotent, because the
    write underneath is an upsert.

    It lives here rather than in ``twt_cli`` because it is the job's rule and not the CLI's, and a
    rule that lives in the module a scheduler does not import is a rule that drifts.
    """
    if not force:
        already = await published_session_count(session, user_id, day)
        if already:
            return {"date": day.isoformat(), "skipped": "already detected", "rows": already}
    outcome = StepOutcome()
    signals = await run_detect_twt(session, outcome, day, user_id=user_id)
    return {
        "date": day.isoformat(),
        "signals": signals,
        "status": outcome.status.value,
        "detail": outcome.detail,
    }


async def published_session_count(session: AsyncSession, user_id: int, on: dt.date) -> int:
    """Whether this session has already been written — what the 21:00 retry asks before it works.

    It counts **breadth** rows, not signal rows, and that is the difference from VBT-1's
    equivalent: this sleeve signals about eighteen times a year, so "no signal rows" is the
    ordinary state of a session that ran perfectly and a retry keyed on it would re-detect the
    whole universe every night. ``tw_breadth_daily`` has exactly one row per session the job has
    seen, including the thin ones it refused to trade. DECISIONS-TW **TW4.3**.
    """
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(TwBreadthDaily)
                .where(TwBreadthDaily.user_id == user_id, TwBreadthDaily.date == on)
            )
        ).scalar_one()
    )


__all__ = [
    "BAR_SCHEMA",
    "LOOKBACK_SESSIONS",
    "RatchetReport",
    "SessionBar",
    "SessionCalendar",
    "TwtFunnel",
    "detect_session",
    "etf_instrument_ids",
    "high_since_from_bars",
    "load_session_bars",
    "load_twt_bars",
    "load_twt_config",
    "load_universe",
    "lookback_start",
    "published_session_count",
    "rederived_high_since",
    "run_detect_twt",
    "run_twt_ratchet",
    "signal_rows",
    "state_rows",
    "upsert_signals",
    "upsert_states",
    "with_storage_columns",
    "write_breadth_row",
]
