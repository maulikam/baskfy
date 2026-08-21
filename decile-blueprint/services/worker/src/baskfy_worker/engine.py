"""Wires ``baskfy_core.factors`` into the pipeline (Prompt 5 deliverable 5).

The ``FactorEngine`` protocol was introduced in Prompt 3 as an explicit seam, with a skeleton
implementation that wrote only what the pipeline could honestly derive. This is the real engine
behind that seam: nothing else in the pipeline changes, which is what the seam was for.

Loading history
---------------
Every factor needs history, not just the as-of bar: a 1-year window needs 248 bars, a 200-day
moving average needs 200, and the all-time high needs everything. So the loader pulls a lookback
window ending at ``as_of`` — long enough for the longest factor, and bounded so a fifteen-year
backfill does not read fifteen years for every single date.

The all-time high is the exception: ``high_ath`` genuinely means *all* time, so it is carried in
separately as a running maximum rather than recomputed from a truncated window, which would put
an ATH lower than the real one on any instrument whose peak predates the lookback.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Final

import polars as pl
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.factors import DEFAULT_FACTOR_CONFIG, FactorConfig, FactorResult
from baskfy_core.factors import compute_factors as compute_factor_frame
from baskfy_core.models import (
    FundamentalDaily,
    IndexDef,
    IndexSnapshotDaily,
    Instrument,
    OhlcvDaily,
    TradingDay,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID

#: The longest lookback any factor needs, in bars: a 1-year window (247 + 1) plus enough history
#: to seed a 247-period Wilder RSI (docs/05 §5) and settle its smoothing.
#: 3 calendar years is comfortably above that and keeps a backfill's per-date read bounded.
DEFAULT_LOOKBACK_DAYS: Final = 365 * 3

#: docs/05 §6 — beta is measured against NIFTY 50.
BENCHMARK_SLUG: Final = "nifty-50"


@dataclass(frozen=True, slots=True)
class LoadedHistory:
    bars: pl.DataFrame
    benchmark: pl.DataFrame
    trading_days: list[dt.date]
    #: Running all-time high per instrument as at ``as_of``, from the full history rather than
    #: the truncated lookback.
    all_time_highs: dict[int, float]


async def load_history(
    session: AsyncSession, as_of: dt.date, lookback_days: int = DEFAULT_LOOKBACK_DAYS
) -> LoadedHistory:
    """Read everything ``baskfy_core.factors`` needs for one as-of date."""
    start = as_of - dt.timedelta(days=lookback_days)

    rows = await session.execute(
        select(
            OhlcvDaily.instrument_id,
            OhlcvDaily.date,
            OhlcvDaily.close,
            OhlcvDaily.close_raw,
            OhlcvDaily.high,
            OhlcvDaily.low,
            OhlcvDaily.volume_raw,
            OhlcvDaily.turnover,
            OhlcvDaily.upper_circuit,
            OhlcvDaily.lower_circuit,
            Instrument.series,
        )
        .join(Instrument, Instrument.id == OhlcvDaily.instrument_id)
        .where(OhlcvDaily.date >= start, OhlcvDaily.date <= as_of)
        .order_by(OhlcvDaily.instrument_id, OhlcvDaily.date)
    )
    records = rows.all()
    bars = (
        pl.DataFrame(
            [
                {
                    "instrument_id": r[0],
                    "date": r[1],
                    "close": float(r[2]) if r[2] is not None else None,
                    "close_raw": float(r[3]) if r[3] is not None else None,
                    "high": float(r[4]) if r[4] is not None else None,
                    "low": float(r[5]) if r[5] is not None else None,
                    "volume_raw": float(r[6]) if r[6] is not None else None,
                    "turnover": float(r[7]) if r[7] is not None else None,
                    "upper_circuit": float(r[8]) if r[8] is not None else None,
                    "lower_circuit": float(r[9]) if r[9] is not None else None,
                    "series": r[10],
                }
                for r in records
            ],
            strict=False,
        )
        if records
        else _empty_bars()
    )

    calendar = await session.execute(
        select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date >= start,
            TradingDay.date <= as_of,
        )
        .order_by(TradingDay.date)
    )
    trading_days = [row[0] for row in calendar]

    benchmark_rows = await session.execute(
        select(IndexSnapshotDaily.date, IndexSnapshotDaily.level)
        .join(IndexDef, IndexDef.id == IndexSnapshotDaily.index_id)
        .where(
            IndexDef.slug == BENCHMARK_SLUG,
            IndexSnapshotDaily.date >= start,
            IndexSnapshotDaily.date <= as_of,
            IndexSnapshotDaily.level.is_not(None),
        )
        .order_by(IndexSnapshotDaily.date)
    )
    benchmark_records = benchmark_rows.all()
    benchmark = (
        pl.DataFrame(
            [{"date": r[0], "close": float(r[1])} for r in benchmark_records], strict=False
        )
        if benchmark_records
        else pl.DataFrame(schema={"date": pl.Date(), "close": pl.Float64()})
    )

    # The true all-time high, from the whole table rather than the lookback.
    ath_rows = await session.execute(
        select(OhlcvDaily.instrument_id, func.max(OhlcvDaily.close))
        .where(OhlcvDaily.date <= as_of)
        .group_by(OhlcvDaily.instrument_id)
    )
    all_time_highs = {row[0]: float(row[1]) for row in ath_rows.tuples() if row[1] is not None}

    return LoadedHistory(bars, benchmark, trading_days, all_time_highs)


def _empty_bars() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "instrument_id": pl.Int64(),
            "date": pl.Date(),
            "close": pl.Float64(),
            "close_raw": pl.Float64(),
            "high": pl.Float64(),
            "low": pl.Float64(),
            "volume_raw": pl.Float64(),
            "turnover": pl.Float64(),
            "upper_circuit": pl.Float64(),
            "lower_circuit": pl.Float64(),
            "series": pl.String(),
        }
    )


class PolarsFactorEngine:
    """The real engine, satisfying the ``FactorEngine`` protocol Prompt 3 introduced."""

    def __init__(self, config: FactorConfig = DEFAULT_FACTOR_CONFIG) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "polars"

    @property
    def config(self) -> FactorConfig:
        return self._config

    def compute(self, bars: pl.DataFrame, as_of: dt.date) -> pl.DataFrame:
        """Protocol entry point. ``bars`` must carry ``trading_days`` context via ``run``."""
        raise NotImplementedError(
            "PolarsFactorEngine needs the trading calendar and benchmark; call `run` instead"
        )

    def run(self, history: LoadedHistory, as_of: dt.date) -> FactorResult:
        result = compute_factor_frame(
            history.bars,
            as_of,
            history.trading_days,
            history.benchmark,
            self._config,
        )
        return _apply_true_ath(result, history.all_time_highs)


def _apply_true_ath(result: FactorResult, all_time_highs: dict[int, float]) -> FactorResult:
    """Replace the lookback-window ATH with the one computed over the full history.

    Without this, an instrument whose peak predates the lookback would report an all-time high it
    never had, and ``away_high_ath`` — the filter docs/01 §2.4 exposes — would say it is nearer
    its high than it is.
    """
    if result.frame.height == 0 or not all_time_highs:
        return result
    override = pl.DataFrame(
        {
            "instrument_id": list(all_time_highs.keys()),
            "_true_ath": list(all_time_highs.values()),
        },
        strict=False,
    ).with_columns(pl.col("instrument_id").cast(result.frame.schema["instrument_id"]))

    frame = result.frame.join(override, on="instrument_id", how="left")
    frame = frame.with_columns(
        pl.max_horizontal([pl.col("high_ath"), pl.col("_true_ath")]).alias("high_ath")
    ).with_columns(
        pl.when(pl.col("high_ath").is_null() | (pl.col("high_ath") == 0))
        .then(None)
        .otherwise(((pl.col("close") / pl.col("high_ath") - 1) * 100).round(2))
        .alias("away_high_ath")
    )
    return FactorResult(frame.drop("_true_ath"), result.windows)


async def load_fundamentals(session: AsyncSession, as_of: dt.date) -> pl.DataFrame:
    """docs/05 §14 — marketcap and P/E come from ``fundamental_daily``, sourced from NSE."""
    rows = await session.execute(
        select(
            FundamentalDaily.instrument_id, FundamentalDaily.marketcap_cr, FundamentalDaily.pe
        ).where(FundamentalDaily.date == as_of)
    )
    records = rows.all()
    if not records:
        return pl.DataFrame(
            schema={
                "instrument_id": pl.Int64(),
                "marketcap_cr": pl.Float64(),
                "pe": pl.Float64(),
            }
        )
    return pl.DataFrame(
        [
            {
                "instrument_id": r[0],
                "marketcap_cr": float(r[1]) if r[1] is not None else None,
                "pe": float(r[2]) if r[2] is not None else None,
            }
            for r in records
        ],
        strict=False,
    )
