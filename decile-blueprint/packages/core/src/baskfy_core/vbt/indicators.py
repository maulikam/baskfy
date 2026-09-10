"""Per-bar columns the VBT-1 rules read (``docs/vbt/04`` §1).

One frame in, one frame out. Every column is a Polars window expression evaluated
``.over("instrument_id")``, the same shape as :mod:`baskfy_core.factors` and
:mod:`baskfy_core.swing.indicators`.

**The frame is densified first, and that is not an implementation detail.** A rolling window here
counts *sessions*, not *rows*: the 50-day volume average of a name that did not trade on two of
the last fifty sessions is the average of the forty-eight bars it did print, over that same
fifty-session span — not the average of its last fifty traded bars, which would reach back
further in time for exactly the illiquid names the rules are trying to judge. So the instrument's
bars are laid out against the calendar with a **null** where no bar exists, and every rolling
statistic carries ``min_samples`` from ``DataConfig.rolling_min_share`` (``docs/vbt/04`` §2.2).

The adjusted series is the input. ``ohlcv_daily.open/high/low/close`` are ``raw x adj_factor``
(:mod:`baskfy_core.adjustments`), so a split inside a 200-day window does not fake a trend.
``close_raw`` rides along because two rules are about exchange prices and not about the adjusted
series: Chartink's ``Close > 30`` (``04`` §3.1 line 2) and the rupee turnover of filter F.
"""

from __future__ import annotations

from typing import Final

import polars as pl

from baskfy_core.vbt.calendar import SessionCalendar
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, VbtConfig

#: Columns :func:`with_vbt_indicators` requires.
REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "instrument_id",
    "date",
    "open",
    "high",
    "low",
    "close",
    "close_raw",
    "volume",
)

#: Columns used when present and tolerated when absent.
OPTIONAL_COLUMNS: Final[tuple[str, ...]] = ("upper_circuit", "adj_factor", "symbol")

#: The columns :func:`with_vbt_indicators` adds, in the order it adds them.
INDICATOR_COLUMNS: Final[tuple[str, ...]] = (
    "prev_close",
    "change_pct",
    "vol_sma",
    "rvol",
    "sma_dma",
    "high_prior",
    "ret_lookback_pct",
    "close_position",
    "turnover_inr",
    "turnover_avg",
    "ema_exit",
    "locked_upper_circuit",
)

_PCT: Final = 100.0
_OVER: Final = "instrument_id"


def require_columns(bars: pl.DataFrame) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in bars.columns]
    if missing:
        raise ValueError(f"bars are missing required columns {missing}; got {bars.columns}")


def _prepare(bars: pl.DataFrame) -> pl.DataFrame:
    frame = bars
    for column in OPTIONAL_COLUMNS:
        if column not in frame.columns:
            dtype = pl.String if column == "symbol" else pl.Float64
            frame = frame.with_columns(pl.lit(None, dtype=dtype).alias(column))
    return frame.with_columns(
        [
            pl.col(c).cast(pl.Float64)
            for c in ("open", "high", "low", "close", "close_raw", "volume", "upper_circuit")
        ]
        + [pl.col("adj_factor").cast(pl.Float64).fill_null(1.0)]
    )


def densify(bars: pl.DataFrame, calendar: SessionCalendar) -> pl.DataFrame:
    """One row per (instrument, session) of the calendar; nulls where the name did not print.

    The instrument's identity columns (``instrument_id``, ``symbol``) are carried onto the blank
    rows so a downstream ``.over("instrument_id")`` still groups them; every price and volume
    stays null, because a bar that did not happen must never be forward-filled into a fill price.
    """
    sessions = pl.DataFrame({"date": list(calendar.sessions)}).with_columns(
        pl.col("date").cast(bars.schema["date"])
    )
    names = bars.select("instrument_id").unique().sort("instrument_id")
    if "symbol" in bars.columns:
        symbols = (
            bars.filter(pl.col("symbol").is_not_null())
            .group_by("instrument_id")
            .agg(pl.col("symbol").first())
        )
        names = names.join(symbols, on="instrument_id", how="left")
    grid = names.join(sessions, how="cross")
    join_on = ["instrument_id", "date"]
    payload = bars.drop("symbol") if "symbol" in bars.columns else bars
    return grid.join(payload, on=join_on, how="left").sort(join_on)


def with_vbt_indicators(
    bars: pl.DataFrame,
    calendar: SessionCalendar,
    config: VbtConfig = DEFAULT_VBT_CONFIG,
) -> pl.DataFrame:
    """Add every indicator column, over the instrument, against the calendar.

    Columns added (all ``Float64`` unless stated):

    * ``prev_close`` — the close of the **previous session**, null when the name did not print
      then; ``change_pct`` is ``(close / prev_close - 1) x 100`` and is therefore null after a
      blank session rather than measuring a two-day move as a one-day one.
    * ``vol_sma`` — mean volume over ``scan.vol_sma_bars`` [50] **including today**, which is how
      Chartink reads ``Sma(Volume, 50)``; ``rvol`` is ``volume / vol_sma``.
    * ``sma_dma`` — mean close over ``trend.dma_bars`` [200], including today (filter A).
    * ``high_prior`` — the highest high of the ``trend.breakout_high_bars`` [20] sessions
      **ending yesterday** (filter B). Today is excluded, or the rule could never be true.
    * ``ret_lookback_pct`` — close over the close ``trend.ret_bars`` [20] **sessions** ago
      (filter C), null when the name did not print that session.
    * ``close_position`` — ``(close - low) / (high - low)``, **0.5 when ``high == low``** (a
      locked bar has no position within its range, and 0.5 refuses to call it either strong or
      weak) (filter D).
    * ``turnover_inr`` — ``close_raw x volume``, the exchange print times the traded quantity;
      ``turnover_avg`` its mean over ``trend.turnover_bars`` [20] (filter F).
    * ``ema_exit`` — exponential MA of the close, span ``exits.trail_ema_bars`` [21],
      ``adjust=False``, null until the span's worth of bars exists.
    * ``locked_upper_circuit`` (``Boolean``) — the bar printed at or through the band.

    ``turnover_inr`` is computed here rather than read from ``ohlcv_daily.turnover``: filter F's
    ₹2 crore is a rupee number about an exchange print, and the stored turnover column is not
    guaranteed to be present for every instrument-day in the plant's history. One definition, so
    a page and a test cannot disagree about what "₹2 crore of turnover" means.
    """
    require_columns(bars)
    frame = _prepare(densify(bars, calendar))
    data, scan, trend, exits = config.data, config.scan, config.trend, config.exits

    frame = frame.with_columns(
        pl.col("close").shift(1).over(_OVER).alias("prev_close"),
        (pl.col("close_raw") * pl.col("volume")).alias("turnover_inr"),
    )
    frame = frame.with_columns(
        ((pl.col("close") / pl.col("prev_close") - 1.0) * _PCT).alias("change_pct"),
        pl.col("volume")
        .rolling_mean(scan.vol_sma_bars, min_samples=data.min_samples(scan.vol_sma_bars))
        .over(_OVER)
        .alias("vol_sma"),
        pl.col("close")
        .rolling_mean(trend.dma_bars, min_samples=data.min_samples(trend.dma_bars))
        .over(_OVER)
        .alias("sma_dma"),
        pl.col("high")
        .rolling_max(
            trend.breakout_high_bars, min_samples=data.min_samples(trend.breakout_high_bars)
        )
        .shift(1)
        .over(_OVER)
        .alias("high_prior"),
        ((pl.col("close") / pl.col("close").shift(trend.ret_bars) - 1.0) * _PCT)
        .over(_OVER)
        .alias("ret_lookback_pct"),
        pl.when(pl.col("high") > pl.col("low"))
        .then((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low")))
        .otherwise(pl.when(pl.col("close").is_null()).then(None).otherwise(pl.lit(0.5)))
        .alias("close_position"),
        pl.col("turnover_inr")
        .rolling_mean(trend.turnover_bars, min_samples=data.min_samples(trend.turnover_bars))
        .over(_OVER)
        .alias("turnover_avg"),
        pl.col("close")
        .ewm_mean(
            span=exits.trail_ema_bars,
            adjust=False,
            min_samples=exits.trail_ema_bars,
            ignore_nulls=False,
        )
        .over(_OVER)
        .alias("ema_exit"),
    )
    return frame.with_columns(
        pl.when(pl.col("vol_sma") > 0)
        .then(pl.col("volume") / pl.col("vol_sma"))
        .otherwise(None)
        .alias("rvol"),
        (
            pl.col("upper_circuit").is_not_null()
            & (pl.col("upper_circuit") > 0)
            & (pl.col("high") >= pl.col("upper_circuit"))
        )
        .fill_null(False)
        .alias("locked_upper_circuit"),
    )
