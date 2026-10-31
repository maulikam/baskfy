from __future__ import annotations

import math
from typing import Final

import polars as pl

from baskfy_core.opportunities.config import DEFAULT_SWING_PARAMS, SwingParams

BAR_COLUMNS: Final[tuple[str, ...]] = (
    "instrument_id",
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
)

_OVER: Final = "instrument_id"
_PCT: Final = 100.0
_TRADING_DAYS: Final = 252
_VOLUME_BARS: Final = 20
_HV_BARS: Final = 20
_SMA_FAST: Final = 20
_SMA_MID: Final = 50


def require_bars(bars: pl.DataFrame) -> None:
    missing = [c for c in BAR_COLUMNS if c not in bars.columns]
    if missing:
        raise ValueError(f"bars are missing required columns {missing}")


def with_indicators(
    bars: pl.DataFrame,
    benchmark: pl.DataFrame | None,
    params: SwingParams = DEFAULT_SWING_PARAMS,
) -> pl.DataFrame:
    require_bars(bars)
    frame = bars.sort([_OVER, "date"])
    for optional in ("turnover", "adj_factor"):
        if optional not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias(optional))
    frame = frame.with_columns(
        [pl.col(c).cast(pl.Float64) for c in ("open", "high", "low", "close", "volume")]
        + [
            pl.col("turnover").cast(pl.Float64),
            pl.col("adj_factor").cast(pl.Float64).fill_null(1.0),
        ]
    )
    if benchmark is not None and not benchmark.is_empty():
        bench = benchmark.select(
            pl.col("date"), pl.col("level").cast(pl.Float64).alias("bench_level")
        )
        frame = frame.join(bench, on="date", how="left")
    else:
        frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias("bench_level"))

    risk = params.risk
    brk = params.breakout
    pb = params.pullback
    rs = params.relative_strength

    frame = frame.with_columns(
        pl.col("close").shift(1).over(_OVER).alias("prev_close"),
        pl.col("high").shift(1).over(_OVER).alias("prev_high"),
        pl.int_range(1, pl.len() + 1).over(_OVER).alias("bars"),
        pl.when(pl.col("turnover").is_not_null() & (pl.col("turnover") > 0))
        .then(pl.col("turnover"))
        .otherwise(pl.col("close") * pl.col("volume"))
        .alias("turnover_inr"),
    )
    frame = frame.with_columns(
        pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - pl.col("prev_close")).abs(),
            (pl.col("low") - pl.col("prev_close")).abs(),
        ).alias("true_range"),
        (pl.col("close") / pl.col("prev_close")).log().alias("log_ret"),
    )
    frame = frame.with_columns(
        pl.col("true_range").rolling_mean(risk.atr_bars).over(_OVER).alias("atr"),
        pl.col("close").rolling_mean(_SMA_FAST).over(_OVER).alias("sma_fast"),
        pl.col("close").rolling_mean(_SMA_MID).over(_OVER).alias("sma_mid"),
        pl.col("close").rolling_mean(params.data.long_trend_bars).over(_OVER).alias("sma_long"),
        (
            pl.col("log_ret").rolling_std(_HV_BARS).over(_OVER) * math.sqrt(_TRADING_DAYS) * _PCT
        ).alias("hv_pct"),
        pl.col("volume").shift(1).rolling_mean(_VOLUME_BARS).over(_OVER).alias("vol_avg"),
        pl.col("turnover_inr").rolling_mean(_VOLUME_BARS).over(_OVER).alias("turnover_avg"),
        pl.col("volume").rolling_mean(_VOLUME_BARS).over(_OVER).alias("volume_avg"),
        pl.col("high").shift(1).rolling_max(brk.pivot_lookback).over(_OVER).alias("pivot"),
        pl.col("low").shift(1).rolling_min(brk.pivot_lookback).over(_OVER).alias("base_low"),
        pl.col("high").rolling_max(pb.swing_lookback).over(_OVER).alias("swing_high"),
        pl.col("low").rolling_min(pb.pullback_bars).over(_OVER).alias("pullback_low"),
        pl.col("high").shift(1).rolling_max(rs.trigger_lookback).over(_OVER).alias("rs_trigger"),
        ((pl.col("close") / pl.col("close").shift(rs.lookback) - 1.0) * _PCT)
        .over(_OVER)
        .alias("ret_lb"),
        ((pl.col("bench_level") / pl.col("bench_level").shift(rs.lookback) - 1.0) * _PCT)
        .over(_OVER)
        .alias("bench_ret_lb"),
        (pl.col("close") / pl.col("bench_level")).alias("rs_line"),
    )
    frame = frame.with_columns(
        (pl.col("atr") / pl.col("close") * _PCT).alias("atr_pct"),
        pl.when(pl.col("vol_avg") > 0)
        .then(pl.col("volume") / pl.col("vol_avg"))
        .otherwise(None)
        .alias("rvol"),
        pl.col("sma_mid").shift(pb.slope_bars).over(_OVER).alias("sma_mid_prev"),
        pl.col("low").rolling_min(pb.pullback_bars).over(_OVER).alias("recent_low"),
        pl.col("rs_line").rolling_max(rs.lookback).over(_OVER).alias("rs_line_high"),
        (pl.col("ret_lb") - pl.col("bench_ret_lb")).alias("rs_excess_pct"),
        (pl.col("sma_fast") - pl.col("low")).alias("_touch_gap"),
    )
    return frame.with_columns(
        (pl.col("_touch_gap") <= pl.col("atr") * pb.touch_atr)
        .rolling_max(pb.pullback_bars)
        .over(_OVER)
        .fill_null(False)
        .alias("touched_fast")
    ).drop("_touch_gap")
