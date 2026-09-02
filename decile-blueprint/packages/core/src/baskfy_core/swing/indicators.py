"""Per-bar columns the swing detectors read (docs/swing/04 §1).

One long frame sorted by ``(instrument_id, date)``; every column is a Polars window expression
evaluated ``.over("instrument_id")``, the same shape as ``baskfy_core.factors``. Nothing here
is rounded: these columns are inputs to a decision, not a stored fact row. What the worker
persists (``sw_setup_daily``) is rounded by ``apply_storage_precision`` at write time, as house
rule 8 requires.

The adjusted series is the input. ``ohlcv_daily.open/high/low/close`` are ``raw x adj_factor``
(``baskfy_core.adjustments``), so a split inside a base does not fake a 50% flagpole. The row's
``adj_factor`` is carried through so a trigger level can be turned back into an exchange price
(``raw = adjusted / adj_factor``) at the moment an order is built.
"""

from __future__ import annotations

from typing import Final

import polars as pl

from baskfy_core.swing.config import DEFAULT_SWING_CONFIG, SwingConfig

#: Columns :func:`with_swing_indicators` expects on the input frame.
REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "instrument_id",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
)

#: Columns used when present and tolerated when absent.
OPTIONAL_COLUMNS: Final[tuple[str, ...]] = ("turnover", "upper_circuit", "adj_factor", "symbol")

#: Return-lookback columns the detectors read, as ``ret_{bars}``.
RETURN_BARS: Final[tuple[int, ...]] = (5, 10, 20, 60)

_PCT: Final = 100.0
_OVER: Final = "instrument_id"


def require_columns(bars: pl.DataFrame) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in bars.columns]
    if missing:
        raise ValueError(f"bars are missing required columns {missing}; got {bars.columns}")


def _prepare(bars: pl.DataFrame) -> pl.DataFrame:
    frame = bars.sort([_OVER, "date"])
    for column in OPTIONAL_COLUMNS:
        if column not in frame.columns:
            dtype = pl.String if column == "symbol" else pl.Float64
            frame = frame.with_columns(pl.lit(None, dtype=dtype).alias(column))
    return frame.with_columns(
        [pl.col(c).cast(pl.Float64) for c in ("open", "high", "low", "close", "volume")]
        + [pl.col(c).cast(pl.Float64) for c in ("turnover", "upper_circuit")]
        + [pl.col("adj_factor").cast(pl.Float64).fill_null(1.0)]
    )


def with_swing_indicators(
    bars: pl.DataFrame, config: SwingConfig = DEFAULT_SWING_CONFIG
) -> pl.DataFrame:
    """Add every indicator column, over the instrument, in one pass.

    Columns added (all ``Float64`` unless stated):

    * ``prev_close``, ``gap_pct`` — today's open over yesterday's close, in percent.
    * ``range_pct`` — ``(high / low - 1) x 100`` for the bar; ``adr_pct`` is its mean over
      ``liquidity.adr_bars`` **including** today.
    * ``turnover_inr`` — exchange turnover, else ``close x volume``; ``turnover_avg`` its mean.
    * ``ma_fast``, ``ma_slow``, ``ma_trend`` — simple MAs of the close (10/20/50 by default).
    * ``vol_avg_fast`` (``flag.dryup_bars``), ``vol_avg_rvol`` (``ep.rvol_bars``, **excluding**
      today, so ``rvol`` is today against the past), ``rvol``.
    * ``ret_5``, ``ret_10``, ``ret_20``, ``ret_60`` — close over the close N bars ago, percent.
    * ``up_streak`` (``Int32``) — consecutive bars with ``close > prev_close``, today included.
    * ``close_position`` — where the close sits in the day's range, 0 (low) to 1 (high).
    """
    require_columns(bars)
    frame = _prepare(bars)
    liquidity = config.liquidity

    frame = frame.with_columns(
        pl.col("close").shift(1).over(_OVER).alias("prev_close"),
        ((pl.col("high") / pl.col("low") - 1.0) * _PCT).alias("range_pct"),
        pl.when(pl.col("turnover").is_not_null() & (pl.col("turnover") > 0))
        .then(pl.col("turnover"))
        .otherwise(pl.col("close") * pl.col("volume"))
        .alias("turnover_inr"),
    )
    frame = frame.with_columns(
        ((pl.col("open") / pl.col("prev_close") - 1.0) * _PCT).alias("gap_pct"),
        pl.col("range_pct").rolling_mean(liquidity.adr_bars).over(_OVER).alias("adr_pct"),
        pl.col("turnover_inr")
        .rolling_mean(liquidity.turnover_bars)
        .over(_OVER)
        .alias("turnover_avg"),
        pl.col("close").rolling_mean(config.ma_fast).over(_OVER).alias("ma_fast"),
        pl.col("close").rolling_mean(config.ma_slow).over(_OVER).alias("ma_slow"),
        pl.col("close").rolling_mean(config.ma_trend).over(_OVER).alias("ma_trend"),
        pl.col("volume").rolling_mean(config.flag.dryup_bars).over(_OVER).alias("vol_avg_fast"),
        pl.col("volume")
        .shift(1)
        .rolling_mean(config.ep.rvol_bars)
        .over(_OVER)
        .alias("vol_avg_rvol"),
        *[
            ((pl.col("close") / pl.col("close").shift(n) - 1.0) * _PCT)
            .over(_OVER)
            .alias(f"ret_{n}")
            for n in RETURN_BARS
        ],
        pl.when(pl.col("high") > pl.col("low"))
        .then((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low")))
        .otherwise(pl.lit(0.5))
        .alias("close_position"),
    )
    frame = frame.with_columns(
        pl.when(pl.col("vol_avg_rvol") > 0)
        .then(pl.col("volume") / pl.col("vol_avg_rvol"))
        .otherwise(None)
        .alias("rvol"),
        (pl.col("close") > pl.col("prev_close")).fill_null(False).alias("_up"),
    )
    # A run-length count: every down (or first) bar starts a new group, and the streak is the
    # cumulative count of up bars inside the group.
    frame = frame.with_columns(
        (~pl.col("_up")).cast(pl.Int32).cum_sum().over(_OVER).alias("_streak_group")
    )
    frame = frame.with_columns(
        pl.col("_up").cast(pl.Int32).cum_sum().over([_OVER, "_streak_group"]).alias("up_streak")
    )
    return frame.drop(["_up", "_streak_group"])


def liquid_expr(config: SwingConfig = DEFAULT_SWING_CONFIG) -> pl.Expr:
    """The universe predicate of docs/swing/04 §1, as one expression over indicator columns."""
    liquidity = config.liquidity
    return (
        (pl.col("adr_pct") >= liquidity.adr_min_pct)
        & (pl.col("turnover_avg") >= liquidity.turnover_min_inr)
        & (pl.col("close") >= liquidity.price_min)
    )
