"""SW3 — the daily swing detection job (``docs/swing/06-module-plan.md``).

Every trading day, from bars that have already been published, this writes what the method saw:
one ``sw_setup_daily`` row per candidate per setup, and one ``sw_market_daily`` row saying whether
the tape allowed new entries at all and how much the book may carry tomorrow.

WHERE THE ARITHMETIC LIVES
--------------------------
Not here. ``baskfy_core.swing`` computes every indicator, every detector, the breadth snapshot,
the gate and the ladder; this module reads rows, hands them over as DataFrames, and writes what
comes back. The same split as ``compute_market_health`` versus ``baskfy_core.breadth``, and for
the same reason: the backtest (SW9) and the nightly job must not be able to disagree about what a
flag is.

THREE THINGS THIS MODULE DOES THAT THE CORE CANNOT
--------------------------------------------------
**It turns adjusted levels back into exchange prices.** ``ohlcv_daily.close`` is
``raw x adj_factor`` and every detector reads the adjusted series, so a split inside a base does
not fake a 50% flagpole. But a *trigger* is a price a person types into a broker, and the broker
has never heard of our adjustment. So each level is divided by the as-of row's ``adj_factor``
before it is stored, and that factor is stored beside it, which is what lets tomorrow's job
notice that a split happened overnight.

``upper_circuit`` goes the other way, and it is the subtle one. The band is an **exchange print**
with no adjusted twin, while ``high`` has been adjusted in place — so on the day after a split,
comparing them directly would say every stock in the market is locked. It is multiplied by the
row's factor on the way *in*, so the comparison happens in one space.

**It adds the two things the score needs and core must not read.** ``docs/swing/04`` §2.6's
``+5`` for a young listing needs ``instrument.listed_on``; its ``+5`` for a hot sector needs
``index_member_daily``. Law 1 forbids core from touching either, so the base score comes back from
the detector and the adjustments are applied here, capped at 100.

**It counts the funnel.** "No flags today" is only useful with "…out of 2,431 instruments, 380 of
them liquid". The counts go on the pipeline step so an operator reading ``/admin/pipeline`` at
21:00 can tell an empty day from a broken one.

THE STEP CANNOT FAIL THE NIGHT
------------------------------
``COMPUTE_SWING`` runs after ``publish``, and like ``refresh_basket`` before it, it records its
own failure and returns rather than raising (M30's rule for post-publish steps). A swing book that
could hold back ``data_version`` would make a screener outage out of a detector bug.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Final

import polars as pl
from sqlalchemy import Select, delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.swing_settings import SYSTEM_OWNED_FIELDS
from baskfy_core.models import (
    IndexDef,
    IndexMemberDaily,
    IndexSnapshotDaily,
    Instrument,
    OhlcvDaily,
    SwConfig,
    SwFill,
    SwMarketDaily,
    SwPosition,
    SwSetupDaily,
    TradingDay,
)
from baskfy_core.precision import apply_storage_precision
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.swing.config import (
    DEFAULT_SWING_CONFIG,
    LiquidityConfig,
    Setup,
    SizingConfig,
    SwingConfig,
)
from baskfy_core.swing.indicators import liquid_expr, with_swing_indicators
from baskfy_core.swing.journal import ClosedTrade
from baskfy_core.swing.market import (
    BreadthSnapshot,
    ExposureTier,
    IndexReading,
    MarketGate,
    breadth_snapshot,
    drawdown_pct,
    exposure_tier,
    market_gate,
)
from baskfy_core.swing.setups import CANDIDATE_COLUMNS, detect_setups
from baskfy_core.universes import SECTOR_INDEX_SLUGS
from baskfy_worker.steps import StepOutcome, StepStatus
from baskfy_worker.telemetry import swing_span, swing_timed

log = logging.getLogger(__name__)

#: How many trading sessions of history the detectors are given. `04` §2's window is
#: `lookback_bars + base_max_bars` = 125 and `SwingConfig.bars_required` is 126; 200 is that with
#: room for a name that missed a few sessions (a suspension, a T+ series move) and still has 125
#: real bars behind today.
LOOKBACK_SESSIONS: Final = 200

#: The cash series the swing book may trade. `EQ` is the rolling-settlement equity series and `BE`
#: the trade-for-trade one; everything else on the register is an ETF, a debt instrument, an SME
#: platform listing or a derivative, and none of them is what this method is about.
CASH_SERIES: Final[tuple[str, ...]] = ("EQ", "BE")

#: `04` §2.6: "+5 for `listed_within_2y`". Two years, in days, so the comparison is one
#: subtraction rather than a calendar walk.
YOUNG_LISTING_DAYS: Final = 730

#: The two score bonuses of `04` §2.6, and the ceiling they cannot push a score past.
YOUNG_LISTING_BONUS: Final = 5.0
HOT_SECTOR_BONUS: Final = 5.0
MAX_SCORE: Final = 100.0

#: How many sectors count as "hot" (`04` §2.6: "a sector in the top-3 breadth strip").
HOT_SECTOR_COUNT: Final = 3

#: A sector needs at least this many liquid names before its breadth means anything. Two names
#: with one above its 20-day average is not a 50% sector.
MIN_SECTOR_MEMBERS: Final = 5

#: PostgreSQL caps one statement at 32,767 bound parameters, and `sw_setup_daily` is ~25 columns
#: wide. The same arithmetic as `tasks.factors.MAX_BIND_PARAMS`, for the same reason.
MAX_BIND_PARAMS: Final = 32_767

#: How many sessions the Saturday scan re-detects (`docs/swing/06` SW4: "the last 5 sessions").
WEEKEND_SCAN_SESSIONS: Final = 5

#: The closed trades the ladder reads. `04` §8.4's `lookback_trades` is 5; ten are loaded so a
#: recalibration of that number does not need a query change.
CLOSED_TRADE_WINDOW: Final = 10


@dataclass(frozen=True, slots=True)
class SwingFunnel:
    """What happened to the universe on the way to a candidate list.

    An empty result is a legitimate answer on most days — his own scan finds a handful of flags
    in a market of two thousand names — and the only way to tell that from a job that silently
    read nothing is to say how many names were at each stage.
    """

    instruments: int
    bars: int
    with_a_bar_today: int
    liquid: int
    per_setup: dict[str, int]

    def as_detail(self) -> dict[str, object]:
        return {
            "instruments": self.instruments,
            "bars": self.bars,
            "with_a_bar_today": self.with_a_bar_today,
            "liquid": self.liquid,
            "candidates": dict(self.per_setup),
        }


async def load_swing_config(session: AsyncSession, user_id: int) -> SwingConfig:
    """`DEFAULT_SWING_CONFIG` with this user's settings applied (PACK.5).

    Only the liquidity floors and the risk knobs are settings — the three floors of `04` §1 and,
    since SW9.5, the three sizing numbers `03` §1 lists as the person's own (`risk_per_trade_pct`,
    `max_position_pct`, `max_open_positions`; each already validated against its server ceiling
    where it was written). Everything else — base geometry, the EP gap, the ladder — is a code
    default, because a threshold that can be changed in a form gets changed after a bad week.
    The plan takes the smaller of the ladder's rung and `max_open_positions` (`04` §9.1), which
    is only true if the plan is handed the person's number rather than the pack's (SW9.5.3). A
    user with no ``sw_config`` row gets the defaults rather than an error: the detectors are
    read-only and a missing settings row is a seeding problem, not a reason to skip a night.
    """
    row = (
        await session.execute(select(SwConfig).where(SwConfig.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        return DEFAULT_SWING_CONFIG
    return replace(
        DEFAULT_SWING_CONFIG,
        liquidity=replace(
            LiquidityConfig(),
            adr_min_pct=float(row.adr_min_pct),
            turnover_min_inr=float(row.turnover_min_inr),
            price_min=float(row.price_min),
        ),
        sizing=replace(
            SizingConfig(),
            risk_per_trade_pct=float(row.risk_per_trade_pct),
            max_position_pct=float(row.max_position_pct),
            max_open_positions=int(row.max_open_positions),
        ),
    )


async def lookback_start(session: AsyncSession, as_of: dt.date, sessions: int) -> dt.date:
    """The date ``sessions`` trading days before ``as_of``, from the calendar rather than by
    subtracting days.

    Counting calendar days would be wrong by about a third: 200 trading days is roughly 290
    calendar days, and the error is not constant — a quarter with two long weekends and Diwali
    is a different number from one without. The calendar is a table for exactly this.
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
        # No calendar at all. Fall back to a generous span of calendar days rather than returning
        # `as_of` and silently detecting on one bar: a short window produces no candidates, which
        # looks exactly like a quiet day.
        return as_of - dt.timedelta(days=sessions * 2)
    return min(dates)


#: One row of :func:`_bar_query`, spelled once so the query and its reader agree.
BarRow = tuple[
    int,
    str,
    dt.date,
    Decimal,
    Decimal,
    Decimal,
    Decimal,
    int,
    Decimal | None,
    Decimal | None,
    Decimal,
]


async def recent_trading_days(session: AsyncSession, as_of: dt.date, count: int) -> list[dt.date]:
    """The last ``count`` trading days up to and including ``as_of``, oldest first.

    Only days the calendar knows about, so a Saturday scan run on a long weekend re-detects the
    five sessions that happened rather than five calendar days of which two were holidays.
    """
    rows = await session.execute(
        select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date <= as_of,
        )
        .order_by(TradingDay.date.desc())
        .limit(count)
    )
    days: list[dt.date] = [row[0] for row in rows]
    return sorted(days)


def _bar_query(start: dt.date, as_of: dt.date) -> Select[BarRow]:
    """Adjusted bars for every live cash-market name, oldest first."""
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
            OhlcvDaily.date <= as_of,
            Instrument.delisted_on.is_(None),
            Instrument.instrument_type == "EQ",
            Instrument.series.in_(CASH_SERIES),
        )
        .order_by(OhlcvDaily.instrument_id, OhlcvDaily.date)
    )


#: The frame `load_swing_bars` returns and `with_swing_indicators` reads — typed even when a
#: column is all null (a month with no circuit band), so a frame built elsewhere in the same
#: shape (SW15's provisional bar) concatenates without a cast failing on a `Null` column.
BAR_SCHEMA: Final = pl.Schema(
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


async def load_swing_bars(session: AsyncSession, start: dt.date, as_of: dt.date) -> pl.DataFrame:
    """The frame ``with_swing_indicators`` expects, in the adjusted space.

    ``upper_circuit`` is multiplied by the row's ``adj_factor`` on the way in. The band is an
    exchange print and ``high`` has been adjusted in place, so on the morning after a 1:2 split
    an unconverted comparison would find every high at twice its band and report the whole market
    locked at its upper circuit.
    """
    records = (await session.execute(_bar_query(start, as_of))).all()
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
                "volume": float(record.volume),
                "turnover": None if record.turnover is None else float(record.turnover),
                "upper_circuit": None if circuit is None else float(circuit) * factor,
                "adj_factor": factor,
            }
        )
    return pl.DataFrame(rows, schema=BAR_SCHEMA)


async def load_sector_membership(session: AsyncSession, on: dt.date) -> dict[int, str]:
    """``instrument_id -> sector slug`` for the sector indices of :data:`SECTOR_INDEX_SLUGS`.

    Point-in-time (house rule 5): membership is read for ``on``, never "as it is today". A name
    that joined NIFTY IT last week was not an IT stock in a base that started in March, and
    scoring it as one would be a look-ahead in the one place the score is used — ranking what to
    buy tomorrow.

    A name in two sector indices (a bank is in both ``nifty-bank`` and
    ``nifty-financial-services``) gets the **narrower** one, measured by how many members the
    index has that day: the narrower index is the more specific claim about what the stock is.
    """
    rows = await session.execute(
        select(
            IndexMemberDaily.instrument_id,
            IndexDef.slug,
            func.count().over(partition_by=IndexMemberDaily.index_id),
        )
        .join(IndexDef, IndexDef.id == IndexMemberDaily.index_id)
        .where(IndexMemberDaily.date == on, IndexDef.slug.in_(SECTOR_INDEX_SLUGS))
    )
    best: dict[int, tuple[int, str]] = {}
    for instrument_id, slug, members in rows:
        current = best.get(int(instrument_id))
        if current is None or int(members) < current[0]:
            best[int(instrument_id)] = (int(members), str(slug))
    return {instrument_id: slug for instrument_id, (_, slug) in best.items()}


async def load_listing_dates(session: AsyncSession) -> dict[int, dt.date]:
    rows = await session.execute(
        select(Instrument.id, Instrument.listed_on).where(Instrument.listed_on.is_not(None))
    )
    return {int(instrument_id): listed for instrument_id, listed in rows}


def sector_breadth(liquid: pl.DataFrame, sectors: dict[int, str]) -> list[tuple[str, float, int]]:
    """``(slug, % above the 20-day average, members)`` for each sector, strongest first.

    Computed here rather than read from ``market_health_daily``, which only covers the twelve
    size universes — no sector index has a breadth row, so ``docs/swing/05`` §2's "top-5 sectors
    by `pct_above_20dma` from `market_health_daily`" has nothing to read. The same number over
    this book's own liquid universe is a better answer anyway: it is the breadth of the names the
    detectors could actually have picked. ``docs/swing/DECISIONS-SW.md`` SW3.1.
    """
    if liquid.is_empty() or not sectors:
        return []
    tagged = liquid.with_columns(
        pl.col("instrument_id")
        .replace_strict(sectors, default=None, return_dtype=pl.String)
        .alias("sector_slug")
    ).filter(pl.col("sector_slug").is_not_null() & pl.col("ma_slow").is_not_null())
    if tagged.is_empty():
        return []
    grouped = (
        tagged.with_columns((pl.col("close") > pl.col("ma_slow")).alias("_above"))
        .group_by("sector_slug")
        .agg(pl.len().alias("members"), pl.col("_above").sum().alias("above"))
        .filter(pl.col("members") >= MIN_SECTOR_MEMBERS)
    )
    result = [
        (
            str(row["sector_slug"]),
            round(int(row["above"]) / int(row["members"]) * 100.0, 2),
            int(row["members"]),
        )
        for row in grouped.iter_rows(named=True)
    ]
    return sorted(result, key=lambda entry: (-entry[1], entry[0]))


def to_exchange_prices(candidates: pl.DataFrame) -> pl.DataFrame:
    """Divide every level by its row's ``adj_factor``: adjusted in, exchange print out."""
    levels = ("close", "trigger", "stop_ref", "pivot_high")
    factor = pl.when(pl.col("adj_factor") > 0).then(pl.col("adj_factor")).otherwise(1.0)
    return candidates.with_columns([(pl.col(name) / factor).alias(name) for name in levels])


def apply_score_adjustments(
    candidates: pl.DataFrame,
    *,
    listed_within_2y: dict[int, bool],
    sectors: dict[int, str],
    hot_sectors: set[str],
) -> pl.DataFrame:
    """`04` §2.6's two `+5`s, capped at 100.

    Applied after the detector rather than inside it because both need a table core may not read
    (law 1): ``instrument.listed_on`` and ``index_member_daily``.
    """
    return candidates.with_columns(
        pl.col("instrument_id")
        .replace_strict(listed_within_2y, default=False, return_dtype=pl.Boolean)
        .alias("listed_within_2y"),
        pl.col("instrument_id")
        .replace_strict(sectors, default=None, return_dtype=pl.String)
        .alias("sector_slug"),
    ).with_columns(
        pl.min_horizontal(
            pl.col("score")
            + pl.when(pl.col("listed_within_2y")).then(YOUNG_LISTING_BONUS).otherwise(0.0)
            + pl.when(pl.col("sector_slug").is_in(sorted(hot_sectors)))
            .then(HOT_SECTOR_BONUS)
            .otherwise(0.0),
            pl.lit(MAX_SCORE),
        ).alias("score")
    )


async def _upsert_setups(  # noqa: PLR0913 - one keyword per fact the write depends on
    session: AsyncSession,
    frame: pl.DataFrame,
    *,
    user_id: int,
    pipeline_run_id: int | None,
    trade_date: dt.date,
    provisional: bool = False,
) -> int:
    """Idempotent by ``(user_id, date, instrument_id, setup)`` — house rule 7.

    Re-running a date overwrites its own rows rather than adding to them, so a night that was
    interrupted and restarted leaves the same table as one that ran once.

    SW15: every row carries ``provisional`` — ``True`` from a daytime "Scan now" over a bar built
    from live quotes, ``False`` from a published close — and after the upsert, the provisional
    rows of ``trade_date`` that this run did **not** re-detect are deleted. So the nightly
    replaces a 13:42 scan's rows (the same keys flip to ``False``; the rest go), and a second
    scan replaces the first's: a flag that only existed at one moment never lingers.
    """
    written = await _write_setups(
        session, frame, user_id=user_id, pipeline_run_id=pipeline_run_id, provisional=provisional
    )
    await _drop_stale_provisional(session, frame, user_id=user_id, trade_date=trade_date)
    return written


async def _drop_stale_provisional(
    session: AsyncSession, frame: pl.DataFrame, *, user_id: int, trade_date: dt.date
) -> None:
    """Delete ``trade_date``'s provisional rows whose key this run did not write."""
    statement = delete(SwSetupDaily).where(
        SwSetupDaily.user_id == user_id,
        SwSetupDaily.date == trade_date,
        SwSetupDaily.provisional.is_(True),
    )
    if not frame.is_empty():
        keys = [
            (int(row["instrument_id"]), str(row["setup"]))
            for row in frame.select("instrument_id", "setup").iter_rows(named=True)
        ]
        statement = statement.where(
            tuple_(SwSetupDaily.instrument_id, SwSetupDaily.setup).not_in(keys)
        )
    await session.execute(statement)


async def _drop_provisional_day(
    session: AsyncSession, *, user_id: int, trade_date: dt.date
) -> None:
    """A run that found no bar for ``trade_date`` supersedes an earlier scan's provisional rows
    for it — setups and the market row both — rather than leaving them to be read as today."""
    await _drop_stale_provisional(session, pl.DataFrame(), user_id=user_id, trade_date=trade_date)
    await session.execute(
        delete(SwMarketDaily).where(
            SwMarketDaily.user_id == user_id,
            SwMarketDaily.date == trade_date,
            SwMarketDaily.provisional.is_(True),
        )
    )


async def _write_setups(
    session: AsyncSession,
    frame: pl.DataFrame,
    *,
    user_id: int,
    pipeline_run_id: int | None,
    provisional: bool,
) -> int:
    if frame.is_empty():
        return 0
    payload = [
        {
            "user_id": user_id,
            "date": row["date"],
            "instrument_id": int(row["instrument_id"]),
            "setup": row["setup"],
            "status": row["status"],
            "score": row["score"],
            "close": row["close"],
            "trigger": row["trigger"],
            "stop_ref": row["stop_ref"],
            "pivot_high": row["pivot_high"],
            "adj_factor": row["adj_factor"],
            "adr_pct": row["adr_pct"],
            "prior_move_pct": row["prior_move_pct"],
            "base_depth_pct": row["base_depth_pct"],
            "tightness_adr": row["tightness_adr"],
            "dryup_ratio": row["dryup_ratio"],
            "dist_ma_fast_pct": row["dist_ma_fast_pct"],
            "dist_ma_slow_pct": row["dist_ma_slow_pct"],
            "rvol": row["rvol"],
            "gap_pct": row["gap_pct"],
            "turnover_avg": None if row["turnover_avg"] is None else int(row["turnover_avg"]),
            "base_bars": None if row["base_bars"] is None else int(row["base_bars"]),
            "up_streak": None if row["up_streak"] is None else int(row["up_streak"]),
            "locked_upper_circuit": bool(row["locked_upper_circuit"]),
            "sector_slug": row["sector_slug"],
            "listed_within_2y": bool(row["listed_within_2y"]),
            "pipeline_run_id": pipeline_run_id,
            "provisional": provisional,
        }
        for row in frame.iter_rows(named=True)
    ]
    updatable = [
        key for key in payload[0] if key not in ("user_id", "date", "instrument_id", "setup")
    ]
    chunk = max(1, MAX_BIND_PARAMS // max(len(payload[0]), 1))
    written = 0
    for offset in range(0, len(payload), chunk):
        batch = payload[offset : offset + chunk]
        statement = insert(SwSetupDaily).values(batch)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    SwSetupDaily.user_id,
                    SwSetupDaily.date,
                    SwSetupDaily.instrument_id,
                    SwSetupDaily.setup,
                ],
                set_={name: getattr(statement.excluded, name) for name in updatable},
            )
        )
        written += len(batch)
    return written


async def load_index_reading(  # noqa: PLR0913 - one keyword per input the reading depends on
    session: AsyncSession,
    on: dt.date,
    *,
    slug: str,
    fallback: str,
    config: SwingConfig,
    live_level: float | None = None,
) -> tuple[IndexReading | None, str | None]:
    """The benchmark's close and its two moving averages (`04` §8.2).

    Returns ``(None, None)`` when neither index has enough history: "no index reading" is a
    documented state — `market_gate` treats a missing benchmark as neutral, because a benchmark
    we cannot read is not a bear market.
    """
    for candidate in (slug, fallback):
        levels = await _index_levels(session, on, candidate, config.market.index_ma_slow)
        # TODAY'S LEVEL, WHEN THE CALLER HAS ONE (9 Sep 2026).
        #
        # Maulik: "tomorrow it might get green on live market data ... once a user logs in and
        # connects the Kite broker, verify whether it's red or green."
        #
        # `index_snapshot_daily` only holds PUBLISHED sessions, so during the day its newest row
        # is yesterday's. A provisional scan therefore recomputed breadth from live bars — that
        # part always worked — and then read the index rule off yesterday's averages, so the half
        # of the gate that actually decides the verdict could not move until the nightly ran. On
        # 9 Sep the live row and the published row carried the SAME index close, 23,106.10, while
        # breadth had already moved from 16.61% to 15.81%.
        #
        # `live_level` is today's level from a Kite index quote. It is appended, not substituted:
        # the 10- and 20-day windows then end on today, which is what the rule means by "the
        # 10-day over the 20-day" while a session is running. Only for the FIRST candidate — the
        # level belongs to the benchmark the caller quoted, and silently applying it to the
        # fallback would put one index's price in another index's average.
        if live_level is not None and candidate == slug:
            levels = [*levels, float(live_level)][-config.market.index_ma_slow :]
        if len(levels) >= config.market.index_ma_slow:
            fast = sum(levels[-config.market.index_ma_fast :]) / config.market.index_ma_fast
            slow = sum(levels[-config.market.index_ma_slow :]) / config.market.index_ma_slow
            return IndexReading(close=levels[-1], ma_fast=fast, ma_slow=slow), candidate
    return None, None


async def _index_levels(session: AsyncSession, on: dt.date, slug: str, count: int) -> list[float]:
    rows = await session.execute(
        select(IndexSnapshotDaily.level)
        .join(IndexDef, IndexDef.id == IndexSnapshotDaily.index_id)
        .where(
            IndexDef.slug == slug,
            IndexSnapshotDaily.date <= on,
            IndexSnapshotDaily.level.is_not(None),
        )
        .order_by(IndexSnapshotDaily.date.desc())
        .limit(count)
    )
    levels = [float(row[0]) for row in rows if row[0] is not None]
    return list(reversed(levels))


async def load_closed_trades(
    session: AsyncSession, *, user_id: int, on: dt.date | None = None
) -> list[Decimal]:
    """The R-multiples of the last **real** closed trades, oldest first.

    The ladder reads real closes from day one (STANDING-ANSWERS A10, SW10.5): there is no paper
    book for it to read — PACK.6's paper clause is void — and a simulated close, whatever
    ``DRY_RUN`` or the flag said when it was written, never moves the rung. A paper winning
    streak sizing real money was the failure PACK.6 named; reading nothing but real closes is
    the stricter answer to it.

    ``on`` bounds the closes by date (SW11, the SW8.1 carry-forward): a re-detect of a past
    session — the Saturday five-session re-scan, ``make swing DATE=`` — must not read a close
    from after that session (house rule 5). ``None`` keeps the old, unbounded read.
    """
    conditions = [
        SwPosition.user_id == user_id,
        SwPosition.state == "CLOSED",
        SwPosition.simulated.is_(False),
        SwPosition.r_multiple.is_not(None),
    ]
    if on is not None:
        conditions.append(SwPosition.closed_on.is_not(None))
        conditions.append(SwPosition.closed_on <= on)
    rows = await session.execute(
        select(SwPosition.r_multiple)
        .where(*conditions)
        .order_by(SwPosition.closed_on.desc(), SwPosition.id.desc())
        .limit(CLOSED_TRADE_WINDOW)
    )
    values = [row[0] for row in rows if row[0] is not None]
    return list(reversed(values))


@dataclass(frozen=True, slots=True)
class SleeveDrawdown:
    """The sleeve's EOD NAV against its peak — `04` §8.5's one measurement (SW9.5.1).

    ``nav`` is the sleeve's capital plus the realised P&L of every closed position of the book
    the ladder reads, plus the unrealised P&L of every open one at the day's close (and the
    realised part of a partial that is still open). ``peak`` is the higher of the stored peak
    and tonight's NAV — so it only ever rises; ``pct`` is how far below it the NAV sits (zero at
    the peak, zero when the peak is not a positive number — nothing divides by it); ``was_locked``
    is the lock-out state the sleeve carried into the evening, which the hysteresis needs.
    """

    nav: Decimal
    peak: Decimal
    pct: Decimal
    was_locked: bool


_TWO_DP: Final = Decimal("0.01")


async def _mark_of(
    session: AsyncSession, *, instrument_id: int, on: dt.date, fallback: Decimal
) -> Decimal:
    """The latest close on or before ``on`` — the mark for an open position; ``fallback`` (the
    entry) when the name has printed no bar yet, so an unmarked position is neither a gain nor
    a loss."""
    close = (
        await session.execute(
            select(OhlcvDaily.close)
            .where(OhlcvDaily.instrument_id == instrument_id, OhlcvDaily.date <= on)
            .order_by(OhlcvDaily.date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return fallback if close is None else Decimal(close)


async def sleeve_nav(
    session: AsyncSession,
    *,
    user_id: int,
    on: dt.date,
    capital: Decimal,
    simulated: bool = False,
) -> Decimal:
    """`03` §1: the sleeve's EOD NAV (SW9.5.1) — nothing in `portfolio_nav_daily` describes this
    sleeve, so it is computed here from the book itself. The ladder and the drawdown read the
    **real** book (``simulated=False``, the default — A10, SW10.5); the keyword remains for a
    reader that wants the paper book's own NAV (the journal page, a report), never the rung.

    ``capital + Σ pnl_inr (CLOSED, closed on or before ``on``) + Σ (mark - entry_avg) x
    quantity_open (OPEN / PARTIAL) + Σ (fill - entry_avg) x quantity over the SELL fills of those
    open positions`` — the last term is a partial's realised half, which no column carries until
    the position closes. Bounded by date like the closes the ladder reads (house rule 5): a
    re-run for a past session must not mark today's book.
    """
    realised = (
        await session.execute(
            select(func.sum(SwPosition.pnl_inr)).where(
                SwPosition.user_id == user_id,
                SwPosition.state == "CLOSED",
                SwPosition.simulated.is_(simulated),
                SwPosition.pnl_inr.is_not(None),
                SwPosition.closed_on.is_not(None),
                SwPosition.closed_on <= on,
            )
        )
    ).scalar_one_or_none()
    nav = capital + (Decimal(0) if realised is None else Decimal(realised))
    open_rows = (
        await session.execute(
            select(SwPosition).where(
                SwPosition.user_id == user_id,
                SwPosition.state.in_(("OPEN", "PARTIAL")),
                SwPosition.quantity_open > 0,
                SwPosition.simulated.is_(simulated),
                SwPosition.entry_date <= on,
            )
        )
    ).scalars()
    for position in open_rows:
        mark = await _mark_of(
            session, instrument_id=position.instrument_id, on=on, fallback=position.entry_avg
        )
        nav += (mark - position.entry_avg) * position.quantity_open
        sold = (
            await session.execute(
                select(SwFill.price, SwFill.quantity).where(
                    SwFill.position_id == position.id, SwFill.side == "SELL"
                )
            )
        ).all()
        nav += sum(
            ((price - position.entry_avg) * quantity for price, quantity in sold), Decimal(0)
        )
    return nav.quantize(_TWO_DP)


async def sleeve_drawdown(
    session: AsyncSession,
    *,
    user_id: int,
    on: dt.date,
    config_row: SwConfig | None,
) -> SleeveDrawdown:
    """Tonight's :class:`SleeveDrawdown`, read without writing anything — over the real book.

    A deployment with no ``sw_config`` row has no sleeve, no peak and no drawdown. Otherwise the
    peak the sleeve carries (``sleeve_peak_inr``) is raised to tonight's NAV if that is higher;
    a null peak — the first evening — is tonight's NAV, so the first session is never locked.
    There is no book switch to reset the peak for since SW10.5 (A10): the ladder and the
    drawdown have read the real book from the first evening, and a paper position never
    counted toward the NAV the peak is measured on.
    """
    if config_row is None:
        return SleeveDrawdown(nav=Decimal(0), peak=Decimal(0), pct=Decimal(0), was_locked=False)
    nav = await sleeve_nav(session, user_id=user_id, on=on, capital=config_row.sleeve_capital_inr)
    stored = config_row.sleeve_peak_inr
    peak = nav if stored is None else max(Decimal(stored), nav)
    pct = Decimal(str(drawdown_pct(peak=peak, equity=nav))).quantize(_TWO_DP)
    return SleeveDrawdown(nav=nav, peak=peak, pct=pct, was_locked=bool(config_row.drawdown_locked))


def breadth_frame(liquid: pl.DataFrame, highs: dict[int, float]) -> pl.DataFrame:
    """The frame ``breadth_snapshot`` reads: ``ret_20``, ``close``, ``ma_slow``, ``high_1y``.

    ``high_1y`` comes from ``factor_daily`` where the factor engine has computed it. A name with
    no factor row is given its own window high from the bars in hand — which is a *lower* bound,
    so the "% at a 52-week high" it produces can only be too generous, and the alternative
    (dropping the name) would make the denominator disagree with the liquid count the funnel
    reports.
    """
    return liquid.with_columns(
        pl.col("instrument_id")
        .replace_strict(highs, default=None, return_dtype=pl.Float64)
        .fill_null(pl.col("high"))
        .alias("high_1y")
    ).select("ret_20", "close", "ma_slow", "high_1y")


async def load_year_highs(session: AsyncSession, on: dt.date) -> dict[int, float]:
    from baskfy_core.models import FactorDaily  # noqa: PLC0415 - one import site for one query

    rows = await session.execute(
        select(FactorDaily.instrument_id, FactorDaily.high_1y).where(
            FactorDaily.date == on, FactorDaily.high_1y.is_not(None)
        )
    )
    return {int(instrument_id): float(high) for instrument_id, high in rows if high is not None}


async def run_detect_swing(  # noqa: PLR0913 - one keyword per input the day depends on
    session: AsyncSession,
    outcome: StepOutcome,
    trade_date: dt.date,
    *,
    user_id: int,
    index_slug: str = "nifty-mid-small-400",
    execution_enabled: bool = False,
    pipeline_run_id: int | None = None,
    extra_bars: pl.DataFrame | None = None,
    provisional: bool = False,
    reference_date: dt.date | None = None,
    scan: dict[str, object] | None = None,
    live_index_level: float | None = None,
) -> int:
    """Detect the day's setups and write the day's market row. Returns the candidate count.

    Returns rather than raises on a date with nothing to read: a non-trading day, or a date the
    pipeline has not published, is not a failure of this step. SW11: one span and one timing
    (`baskfy_swing_task_duration_seconds{task="detect"}`) around the whole step, through the
    worker's guarded helpers — neither can raise into it.

    SW15 ("Scan now"): ``extra_bars`` is a frame in `load_swing_bars`'s shape appended to the
    published bars — the one provisional bar per name a daytime scan builds from live quotes —
    and ``provisional`` is stamped on every row written. ``reference_date`` is the last
    *published* session, where the point-in-time reads that have no row for an unpublished
    ``trade_date`` (sector membership, the year highs, the index reading) look instead; the
    nightly leaves it ``None`` and reads ``trade_date``. ``scan`` is stamped into the market
    row's ``detail.scan`` so the page can say when the rows were scanned and from what.
    """
    with swing_span("swing.detect", date=trade_date.isoformat()), swing_timed("detect"):
        return await _detect_swing(
            session,
            outcome,
            trade_date,
            user_id=user_id,
            index_slug=index_slug,
            execution_enabled=execution_enabled,
            pipeline_run_id=pipeline_run_id,
            extra_bars=extra_bars,
            provisional=provisional,
            reference_date=reference_date,
            scan=scan,
            live_index_level=live_index_level,
        )


def append_bars(bars: pl.DataFrame, extra: pl.DataFrame | None) -> pl.DataFrame:
    """The published bars plus a scan's provisional ones, in the order the indicators expect."""
    if extra is None or extra.is_empty():
        return bars
    columns = BAR_SCHEMA.names()
    return pl.concat(
        [bars.select(columns).cast(BAR_SCHEMA), extra.select(columns).cast(BAR_SCHEMA)],
        how="vertical",
    ).sort("instrument_id", "date")


async def _detect_swing(  # noqa: PLR0913 - one keyword per input the day depends on
    session: AsyncSession,
    outcome: StepOutcome,
    trade_date: dt.date,
    *,
    user_id: int,
    index_slug: str,
    execution_enabled: bool,
    pipeline_run_id: int | None,
    extra_bars: pl.DataFrame | None = None,
    provisional: bool = False,
    reference_date: dt.date | None = None,
    scan: dict[str, object] | None = None,
    live_index_level: float | None = None,
) -> int:
    config = await load_swing_config(session, user_id)
    start = await lookback_start(session, trade_date, LOOKBACK_SESSIONS)
    bars = append_bars(await load_swing_bars(session, start, trade_date), extra_bars)
    lookup = reference_date or trade_date
    if bars.is_empty():
        outcome.status = StepStatus.SKIPPED
        outcome.note(
            date=trade_date.isoformat(),
            skipped_reason="no published bars in the lookback window",
            window=[start.isoformat(), trade_date.isoformat()],
        )
        await _drop_provisional_day(session, user_id=user_id, trade_date=trade_date)
        return 0

    indicated = with_swing_indicators(bars, config)
    today = indicated.filter(pl.col("date") == trade_date)
    if today.is_empty():
        outcome.status = StepStatus.SKIPPED
        outcome.note(
            date=trade_date.isoformat(),
            skipped_reason="no instrument has a bar on this date",
            bars=bars.height,
        )
        await _drop_provisional_day(session, user_id=user_id, trade_date=trade_date)
        return 0

    liquid = today.filter(liquid_expr(config))
    sectors = await load_sector_membership(session, lookup)
    strip = sector_breadth(liquid, sectors)
    hot = {slug for slug, _, _ in strip[:HOT_SECTOR_COUNT]}

    candidates = detect_setups(indicated, trade_date, config)
    listing = await load_listing_dates(session)
    young = {
        instrument_id: (trade_date - listed).days <= YOUNG_LISTING_DAYS
        for instrument_id, listed in listing.items()
    }
    if not candidates.is_empty():
        candidates = apply_score_adjustments(
            to_exchange_prices(candidates),
            listed_within_2y=young,
            sectors=sectors,
            hot_sectors=hot,
        )
        candidates = apply_storage_precision(candidates)
    else:
        candidates = candidates.with_columns(
            pl.lit(None, dtype=pl.String).alias("sector_slug"),
            pl.lit(False, dtype=pl.Boolean).alias("listed_within_2y"),
        )

    written = await _upsert_setups(
        session,
        candidates,
        user_id=user_id,
        pipeline_run_id=pipeline_run_id,
        trade_date=trade_date,
        provisional=provisional,
    )

    per_setup = {
        setup.value: (
            0 if candidates.is_empty() else candidates.filter(pl.col("setup") == setup.value).height
        )
        for setup in Setup
    }
    funnel = SwingFunnel(
        instruments=today.height,
        bars=bars.height,
        with_a_bar_today=today.height,
        liquid=liquid.height,
        per_setup=per_setup,
    )

    await write_market_row(
        session,
        trade_date,
        user_id=user_id,
        config=config,
        liquid=liquid,
        parabolic_count=per_setup.get(Setup.PARABOLIC_SHORT.value, 0),
        sectors=strip,
        index_slug=index_slug,
        execution_enabled=execution_enabled,
        funnel=funnel.as_detail(),
        provisional=provisional,
        reference_date=reference_date,
        scan=scan,
        live_index_level=live_index_level,
    )

    outcome.rows_in = bars.height
    outcome.rows_out = written
    outcome.note(date=trade_date.isoformat(), funnel=funnel.as_detail())
    return written


async def write_market_row(  # noqa: PLR0913 - one keyword per input the row depends on
    session: AsyncSession,
    trade_date: dt.date,
    *,
    user_id: int,
    config: SwingConfig,
    liquid: pl.DataFrame,
    parabolic_count: int,
    sectors: list[tuple[str, float, int]],
    index_slug: str,
    execution_enabled: bool,
    funnel: dict[str, object] | None = None,
    provisional: bool = False,
    reference_date: dt.date | None = None,
    scan: dict[str, object] | None = None,
    live_index_level: float | None = None,
) -> MarketGate:
    """Breadth, the gate and tomorrow's tier — `04` §8, written to ``sw_market_daily``.

    SW15: ``funnel`` lands in ``detail.funnel`` (`GET /swing/setups` reads it there — the empty
    state's "41 names were liquid, 0 met the rules"); ``provisional`` says the breadth was
    measured over live-quote bars and ``scan`` (``detail.scan``) says when; ``reference_date``
    is where the year highs and the index reading are read when ``trade_date`` has no
    published row yet (a daytime scan), the same reading the evening will make of the last
    close. A settled row keeps its ladder either way.

    SW11 (the SW8.1 carry-forward): the tier this job computes is a *preview*; the evening's
    `settle_ladder` is the fact. A row the evening has already settled — ``detail.ladder.
    settled_by == "swing-eod"`` — keeps its tier columns and its ladder record through a
    re-detect (the Saturday re-scan, a repair), so a re-scan after a GREEN Friday cannot hand
    Monday morning a rung one above `sw_config` (SW8.1). Breadth, the gate, the index reading
    and the sectors are rewritten either way — those are the detectors' own numbers. The
    closes the preview reads are bounded by ``trade_date``, as the evening's are.
    """
    lookup = reference_date or trade_date
    highs = await load_year_highs(session, lookup)
    breadth: BreadthSnapshot = (
        breadth_snapshot(breadth_frame(liquid, highs), config.market)
        if not liquid.is_empty()
        else BreadthSnapshot(0, 0.0, 0.0, 0.0)
    )
    reading, used_slug = await load_index_reading(
        session,
        lookup,
        slug=index_slug,
        fallback="nifty-500",
        config=config,
        live_level=live_index_level,
    )
    gate = market_gate(breadth, reading, config.market)

    closed = await load_closed_trades(session, user_id=user_id, on=trade_date)
    config_row = (
        await session.execute(select(SwConfig).where(SwConfig.user_id == user_id))
    ).scalar_one_or_none()
    current = 0 if config_row is None else int(config_row.exposure_level)
    # `04` §8.5: the sleeve's drawdown, read the way the evening reads it (the evening's
    # `settle_ladder` is the one that writes the peak back; this is the preview, as the rung is).
    drawdown = await sleeve_drawdown(session, user_id=user_id, on=trade_date, config_row=config_row)
    tier = exposure_tier(
        current_level=current,
        closed_r_multiples=closed,
        gate=gate,
        config=config.market,
        drawdown_pct=float(drawdown.pct),
        was_drawdown_locked=drawdown.was_locked,
    )

    detail: dict[str, object] = {
        "closed_r_multiples": [str(value) for value in closed],
        # A10 (SW10.5): the ladder reads real closes only; the flag no longer picks a book.
        "closed_trades_read": "real",
        "drawdown": {
            "nav": str(drawdown.nav),
            "peak": str(drawdown.peak),
            "pct": str(drawdown.pct),
            "was_locked": drawdown.was_locked,
            "locked": tier.drawdown_locked,
        },
        "sectors": [
            {"slug": slug, "pct_above_ma_slow": pct, "members": members}
            for slug, pct, members in sectors
        ],
    }
    if funnel is not None:
        detail["funnel"] = funnel
    if scan is not None:
        detail["scan"] = scan
    settled = await settled_market_row(session, user_id=user_id, on=trade_date)
    if settled is not None:
        # The evening has settled this row: its rung, its record and what it read stand.
        tier = ExposureTier(
            level=int(settled.exposure_level),
            max_open_positions=int(settled.max_open_positions),
            max_exposure_pct=float(settled.max_exposure_pct),
            new_entries_allowed=bool(settled.new_entries_allowed),
            drawdown_locked=bool(settled.drawdown_locked),
        )
        kept = settled.detail if isinstance(settled.detail, dict) else {}
        for key in ("ladder", "closed_r_multiples", "closed_trades_read", "drawdown"):
            if key in kept:
                detail[key] = kept[key]
        drawdown = replace(drawdown, pct=Decimal(str(settled.drawdown_pct or 0)))
    statement = insert(SwMarketDaily).values(
        user_id=user_id,
        date=trade_date,
        constituent_count=breadth.constituent_count,
        pct_up_strong_1m=breadth.pct_up_strong_1m,
        pct_new_52w_high=breadth.pct_new_52w_high,
        pct_above_ma_slow=breadth.pct_above_ma_slow,
        index_slug=used_slug,
        index_close=None if reading is None else round(reading.close, 2),
        index_ma_fast=None if reading is None else round(reading.ma_fast, 2),
        index_ma_slow=None if reading is None else round(reading.ma_slow, 2),
        gate=gate.value,
        exposure_level=tier.level,
        max_open_positions=tier.max_open_positions,
        max_exposure_pct=tier.max_exposure_pct,
        new_entries_allowed=tier.new_entries_allowed,
        drawdown_pct=drawdown.pct,
        drawdown_locked=tier.drawdown_locked,
        parabolic_count=parabolic_count,
        detail=detail,
        provisional=provisional,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[SwMarketDaily.user_id, SwMarketDaily.date],
            set_={
                name: getattr(statement.excluded, name)
                for name in (
                    "provisional",
                    "constituent_count",
                    "pct_up_strong_1m",
                    "pct_new_52w_high",
                    "pct_above_ma_slow",
                    "index_slug",
                    "index_close",
                    "index_ma_fast",
                    "index_ma_slow",
                    "gate",
                    "exposure_level",
                    "max_open_positions",
                    "max_exposure_pct",
                    "new_entries_allowed",
                    "drawdown_pct",
                    "drawdown_locked",
                    "parabolic_count",
                    "detail",
                )
            },
        )
    )
    return gate


async def settled_market_row(
    session: AsyncSession, *, user_id: int, on: dt.date
) -> SwMarketDaily | None:
    """The market row for ``on`` if the evening job has settled it — ``detail.ladder.settled_by
    == "swing-eod"`` (`swing_eod.LADDER_CHANGED_BY`, spelled here rather than imported: that
    module imports this one) — else ``None``."""
    row = (
        await session.execute(
            select(SwMarketDaily).where(SwMarketDaily.user_id == user_id, SwMarketDaily.date == on)
        )
    ).scalar_one_or_none()
    if row is None or not isinstance(row.detail, dict):
        return None
    ladder = row.detail.get("ladder")
    if isinstance(ladder, dict) and ladder.get("settled_by") == SETTLED_BY_EVENING:
        return row
    return None


#: The value `swing_eod.settle_ladder` writes into ``detail.ladder.settled_by`` — the owner of
#: ``exposure_level`` in the settings' own table, so the two cannot drift apart.
SETTLED_BY_EVENING: Final = SYSTEM_OWNED_FIELDS["exposure_level"]


__all__ = [
    "BAR_SCHEMA",
    "CANDIDATE_COLUMNS",
    "CASH_SERIES",
    "LOOKBACK_SESSIONS",
    "WEEKEND_SCAN_SESSIONS",
    "ClosedTrade",
    "SleeveDrawdown",
    "SwingFunnel",
    "append_bars",
    "apply_score_adjustments",
    "breadth_frame",
    "load_closed_trades",
    "load_index_reading",
    "load_listing_dates",
    "load_sector_membership",
    "load_swing_bars",
    "load_swing_config",
    "lookback_start",
    "recent_trading_days",
    "run_detect_swing",
    "sector_breadth",
    "settled_market_row",
    "sleeve_drawdown",
    "sleeve_nav",
    "to_exchange_prices",
    "write_market_row",
]
