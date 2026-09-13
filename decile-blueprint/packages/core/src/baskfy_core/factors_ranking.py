"""Stored ranking factors — docs/ranking/PLAN.md Phase 2, contract C1. Pure: no I/O, no clock.

Every column here is one row of the C1 table, computed per instrument on the **adjusted**
``close``/``high``/``low``, point-in-time (only bars dated on or before the row), and NULL when
its window is not full. The factor engine in ``baskfy_core.factors`` calls the two entry points
in order, and ``compute_factors`` rounds the result through ``precision.COLUMN_PRECISION`` like
every other ``factor_daily`` column:

* :func:`with_ranking_series` — the factors that are rolling expressions over each instrument's
  whole history (``.over("instrument_id")``, one Polars pass, no loop over instruments).
* :func:`with_ranking_at_as_of` — the factors that only mean something at the as-of row: the
  path statistics over a calendar-month window (drawdown, underwater, return ex top days), which
  need a running maximum *within* the window; the benchmark-relative returns, whose base is a
  calendar date; and the NSE momentum ratios, whose anchors are month ends.

Two columns are cross-sectional or cross-date and are therefore NULL from ``compute_factors``
itself. The worker fills them after the day's rows exist, with the pure functions at the bottom
of this module: :func:`cross_sectional_pctile` (``mom_pctile``) and :func:`rank_persistence`
(``rank_persist_20``).

Conventions, stated once (C1 preamble)
--------------------------------------
* "Window N sessions" is the last N bars ending at and including the row's date.
* ``r_t = c_t / c_{t-1} - 1`` is the simple daily return (``daily_return`` in ``factors.py``);
  ``l_t = ln(c_t / c_{t-1})`` is the log return.
* A calendar-month window reuses ``windows.py`` exactly as ``ret_Nm`` does: its length ``N`` is
  resolved at the as-of date, the instrument contributes its last ``N`` bars, and the whole
  window is NULL when the calendar does not reach back to the offset.
* An "N-session return" is ``c_t / c_{t-N} - 1`` — N daily returns compounded — the same count
  ``accel_21_105`` means by "the last 21 sessions" of log returns. ``DECISIONS-MERGE.md``
  "Ranking 2.A" records the choice.
"""

from __future__ import annotations

import calendar
import datetime as dt
import math
from typing import Final

import numpy as np
import numpy.typing as npt
import polars as pl

from baskfy_core.blends import blend_expr
from baskfy_core.precision import PERCENT_DP, round_expr
from baskfy_core.windows import FactorWindow, resolve_window

#: ``numeric(precision, scale)`` of every decimal C1 column, as ``factor_daily`` declares it.
#: The storage-range guard reads this, and a test pins it to the ORM model, so the two cannot
#: drift apart silently.
RANKING_NUMERIC_TYPES: Final[dict[str, tuple[int, int]]] = {
    "atr_14": (18, 4),
    "atr_ext_20": (10, 4),
    "ma50_slope_20": (14, 2),
    "eff_ratio_63": (10, 4),
    "max_dd_6m": (14, 2),
    "max_dd_12m": (14, 2),
    "downside_vol_6m": (18, 10),
    "downside_vol_12m": (18, 10),
    "sortino_6m": (14, 2),
    "sortino_12m": (14, 2),
    "underwater_12m": (7, 2),
    "ret_ex_top3_12m": (14, 2),
    "accel_21_105": (18, 10),
    "accel_21_105_vs": (10, 4),
    "vol_exp_21_126": (10, 4),
    "excess_ret_3m": (14, 2),
    "excess_ret_6m": (14, 2),
    "excess_ret_12m": (14, 2),
    "resid_ret_12m": (14, 2),
    "rs_persist_126": (7, 2),
    "mom_pctile": (7, 2),
    "rank_persist_20": (7, 2),
    "nse_mr6": (18, 10),
    "nse_mr12": (18, 10),
}

#: C1 columns stored as ``smallint``.
RANKING_INTEGER_COLUMNS: Final[tuple[str, ...]] = ("vol_persist_20",)

#: Every C1 column, in the contract's order.
RANKING_FACTOR_COLUMNS: Final[tuple[str, ...]] = (
    "atr_14",
    "atr_ext_20",
    "ma50_slope_20",
    "eff_ratio_63",
    "max_dd_6m",
    "max_dd_12m",
    "downside_vol_6m",
    "downside_vol_12m",
    "sortino_6m",
    "sortino_12m",
    "underwater_12m",
    "ret_ex_top3_12m",
    "accel_21_105",
    "accel_21_105_vs",
    "vol_exp_21_126",
    "vol_persist_20",
    "excess_ret_3m",
    "excess_ret_6m",
    "excess_ret_12m",
    "resid_ret_12m",
    "rs_persist_126",
    "mom_pctile",
    "rank_persist_20",
    "nse_mr6",
    "nse_mr12",
)

# --- Window constants, each named after the C1 column that fixes it ---------------------------

#: ``atr_14``: Wilder's period.
ATR_PERIOD: Final = 14
#: ``ma50_slope_20``: the moving average and the lookback its slope is measured over.
MA_SLOPE_LOOKBACK: Final = 20
#: ``eff_ratio_63``: the net move and the path are both measured over 63 daily changes.
EFFICIENCY_CHANGES: Final = 63
#: ``accel_21_105``: the recent block and the block of log returns immediately before it.
ACCEL_RECENT: Final = 21
ACCEL_PRIOR: Final = 105
#: ``vol_exp_21_126``: recent traded value against the 126 sessions before it.
VOLUME_RECENT: Final = 21
VOLUME_BASE: Final = 126
#: ``vol_persist_20``: the sessions counted, against the 126 sessions before them.
VOLUME_PERSIST_SESSIONS: Final = 20
#: ``rs_persist_126``: the sessions counted, and the return horizon each one compares.
RS_PERSIST_SESSIONS: Final = 126
RS_RETURN_SESSIONS: Final = 20
#: ``ret_ex_top3_12m``: how many of the largest daily log returns are removed.
TOP_DAYS_EXCLUDED: Final = 3
#: ``rank_persist_20``: the dates looked back over and the percentile that counts as a hit.
RANK_PERSIST_DATES: Final = 20
RANK_PERSIST_THRESHOLD: Final = 80.0
#: ``nse_mr6``/``nse_mr12`` (NSE methodology §16): sigma is one year of daily log returns, and the
#: return bases are the month ends 7 and 13 months before the rebalance month.
NSE_SIGMA_SESSIONS: Final = 252
NSE_MR6_BASE_MONTHS: Final = 7
NSE_MR12_BASE_MONTHS: Final = 13
NSE_RETURN_END_MONTHS: Final = 1

#: The calendar-month windows C1 reads: 3M for ``excess_ret_3m``, 6M and 12M for the rest.
RANKING_WINDOW_MONTHS: Final[tuple[int, ...]] = (3, 6, 12)

#: docs/05 §2's annualisation, shared with ``factors.py``.
TRADING_DAYS_PER_YEAR: Final = 252
_SQRT_YEAR: Final = math.sqrt(TRADING_DAYS_PER_YEAR)

#: Below this a volatility-like denominator is zero for storage purposes: half the last place a
#: 10-dp column keeps. The same argument as ``factors.ZERO_VOLATILITY_EPSILON`` (and the same
#: value): a ratio over a denominator that rounds to 0.0000000000 could not be reproduced from the
#: stored row, so NULL is the only self-consistent answer. It also catches the ~1e-17 standard
#: deviation floating point gives a constant series instead of exactly zero.
ZERO_DENOMINATOR_EPSILON: Final = 0.5e-10
#: ``atr_14`` is stored at 4 dp, so an ATR below half of that place rounds to zero on write and
#: ``atr_ext_20`` over it could not be reproduced from the stored row.
ZERO_ATR_EPSILON: Final = 0.5e-4

#: The written blend ``mom_pctile`` ranks, and its components (docs/05 §4: NULL if any is NULL).
MOM_PCTILE_SOURCE: Final = "avg_sharpe_12_6_3_1"
MOM_PCTILE_COMPONENTS: Final[tuple[str, ...]] = (
    "sharpe_12m",
    "sharpe_6m",
    "sharpe_3m",
    "sharpe_1m",
)

# Working columns: prefixed so they cannot collide with a stored name, and dropped before return.
_LOG_RETURN: Final = "_rk_log_return"
_TRUE_RANGE: Final = "_rk_true_range"
_TR_SEED: Final = "_rk_tr_seed"
_TR_SEEDS_SEEN: Final = "_rk_tr_seeds_seen"
_VOLUME_BASELINE: Final = "_rk_volume_baseline"
_BENCH_RETURN_20: Final = "_rk_bench_return_20"
_NSE_SIGMA: Final = "_rk_nse_sigma"
_PCTILE_VALUE: Final = "_rk_pctile_value"
INTERNAL_COLUMNS: Final[tuple[str, ...]] = (
    _LOG_RETURN,
    _TRUE_RANGE,
    _TR_SEED,
    _TR_SEEDS_SEEN,
    _VOLUME_BASELINE,
    _BENCH_RETURN_20,
    _NSE_SIGMA,
)

_ID: Final = "instrument_id"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def resolve_ranking_windows(
    as_of: dt.date, trading_days: list[dt.date] | tuple[dt.date, ...]
) -> dict[int, FactorWindow]:
    """The 3M, 6M and 12M windows, resolved exactly as ``ret_Nm``'s are.

    Resolved here rather than taken from ``FactorConfig.window_months`` so that a config which
    drops a window from the legacy families cannot silently change what a C1 column measures.
    """
    return {months: resolve_window(as_of, months, trading_days) for months in RANKING_WINDOW_MONTHS}


def benchmark_levels(frame: pl.DataFrame | None) -> pl.DataFrame | None:
    """Normalise a benchmark series to ``(date, level)``, ascending; ``None`` when absent.

    C1 names the frames ``(date, level)`` — ``index_snapshot_daily.level`` — while the beta path
    has always been handed ``(date, close)``. Both spellings are accepted; anything else is a
    caller bug and raises. A duplicated date is refused too, because a join on it would double
    every instrument row for that day.
    """
    if frame is None or frame.height == 0:
        return None
    if "level" in frame.columns:
        column = "level"
    elif "close" in frame.columns:
        column = "close"
    else:
        raise ValueError(f"a benchmark needs a 'level' (or 'close') column; got {frame.columns}")
    # NULL levels are kept, not dropped: a missing level must make that day's return NULL, not
    # stretch the next return across two sessions.
    levels = frame.select(pl.col("date"), pl.col(column).cast(pl.Float64).alias("level")).sort(
        "date"
    )
    if levels["date"].n_unique() != levels.height:
        raise ValueError("a benchmark series carries the same date twice")
    return levels


def _level_on(levels: pl.DataFrame | None, day: dt.date) -> float | None:
    """The benchmark level on exactly ``day``, or ``None`` — never a neighbouring day's."""
    if levels is None:
        return None
    match = levels.filter(pl.col("date") == day)["level"]
    if match.len() == 0:
        return None
    raw = match[0]
    if raw is None:
        return None
    value = float(raw)
    return value if math.isfinite(value) else None


def benchmark_window_return(
    levels: pl.DataFrame | None, as_of: dt.date, window: FactorWindow
) -> float | None:
    """``(level_t / level_{window start} - 1) x 100``, the index's return over ``ret_Nm``'s window.

    The base is the level on the window's first trading day — the same date ``ret_Nm`` takes its
    base bar from. NULL when either level is missing, the base is not positive, or the calendar
    does not span the window.
    """
    if not window.spans_full_window:
        return None
    end = _level_on(levels, as_of)
    start = _level_on(levels, window.start)
    if end is None or start is None or start <= 0:
        return None
    return (end / start - 1.0) * 100.0


def month_end_trading_day(
    trading_days: list[dt.date] | tuple[dt.date, ...], as_of: dt.date, months_back: int
) -> dt.date | None:
    """The last trading day in the calendar month ``months_back`` months before ``as_of``'s.

    C1: "month ends are the last trading day in ``trading_days``". ``None`` when the calendar
    holds no trading day in that month.
    """
    if months_back < 1:
        raise ValueError(f"months_back must be at least 1; got {months_back}")
    total = as_of.year * 12 + as_of.month - 1 - months_back
    year, month_index = divmod(total, 12)
    first = dt.date(year, month_index + 1, 1)
    last = dt.date(year, month_index + 1, calendar.monthrange(year, month_index + 1)[1])
    inside = [day for day in trading_days if first <= day <= last]
    return max(inside) if inside else None


# ---------------------------------------------------------------------------
# Rolling factors over the whole history
# ---------------------------------------------------------------------------


def with_ranking_series(
    frame: pl.DataFrame,
    windows: dict[int, FactorWindow],
    market_benchmark: pl.DataFrame | None,
) -> pl.DataFrame:
    """Attach every C1 column that is a rolling expression over each instrument's history.

    ``frame`` is ``factors.py``'s long frame, sorted by ``(instrument_id, date)``, already carrying
    ``close``, ``high``, ``low``, ``daily_return``, ``vol_day_val``, ``ma_20`` and ``ma_50``. Every
    expression looks backwards only, so a bar dated after a row can never change that row.
    """
    close = pl.col("close")
    previous_close = close.shift(1).over(_ID)
    frame = frame.with_columns(
        pl.when(previous_close.is_null() | (previous_close <= 0) | close.is_null() | (close <= 0))
        .then(None)
        .otherwise((close / previous_close).log())
        .alias(_LOG_RETURN),
        # TR needs the previous close, so an instrument's first bar has none (TA-Lib's reading;
        # Wilder's paper seeds with high - low instead — DECISIONS-MERGE "Ranking 2.A").
        pl.when(pl.col("high").is_null() | pl.col("low").is_null() | previous_close.is_null())
        .then(None)
        .otherwise(
            pl.max_horizontal(
                pl.col("high") - pl.col("low"),
                (pl.col("high") - previous_close).abs(),
                (pl.col("low") - previous_close).abs(),
            )
        )
        .alias(_TRUE_RANGE),
    )
    frame = _with_atr(frame)
    frame = _with_trend(frame)
    frame = _with_downside_volatility(frame, windows)
    frame = _with_acceleration(frame)
    frame = _with_participation(frame)
    frame = _with_relative_strength(frame, market_benchmark)
    return frame.with_columns(
        (
            pl.col(_LOG_RETURN)
            .rolling_std(window_size=NSE_SIGMA_SESSIONS, min_samples=NSE_SIGMA_SESSIONS, ddof=1)
            .over(_ID)
            * _SQRT_YEAR
        ).alias(_NSE_SIGMA)
    )


def _with_atr(frame: pl.DataFrame) -> pl.DataFrame:
    """``atr_14`` — Wilder: the first value is the mean of the first 14 TRs, then (prev·13+TR)/14.

    An exponential mean with ``alpha = 1/14`` and ``adjust=False`` *is* that recursion; what it
    gets wrong on its own is the seed, which it takes from the first observation. So the input is
    NULL before the seed row, the 14-TR mean on it, and the raw TR after it. A NULL TR later in
    the series (a bar with no high or low) is skipped by the recursion and is NULL on its own row.
    """
    seed = pl.col(_TRUE_RANGE).rolling_mean(window_size=ATR_PERIOD, min_samples=ATR_PERIOD)
    frame = frame.with_columns(seed.over(_ID).alias(_TR_SEED))
    frame = frame.with_columns(
        pl.col(_TR_SEED).is_not_null().cast(pl.Int64).cum_sum().over(_ID).alias(_TR_SEEDS_SEEN)
    )
    wilder_input = (
        pl.when(pl.col(_TR_SEEDS_SEEN) == 0)
        .then(None)
        .when((pl.col(_TR_SEEDS_SEEN) == 1) & pl.col(_TR_SEED).is_not_null())
        .then(pl.col(_TR_SEED))
        .otherwise(pl.col(_TRUE_RANGE))
    )
    frame = frame.with_columns(
        wilder_input.ewm_mean(alpha=1.0 / ATR_PERIOD, adjust=False, ignore_nulls=True)
        .over(_ID)
        .alias("atr_14")
    )
    return frame.with_columns(
        pl.when(
            pl.col("atr_14").is_null()
            | (pl.col("atr_14") < ZERO_ATR_EPSILON)
            | pl.col("ma_20").is_null()
            | pl.col("close").is_null()
        )
        .then(None)
        .otherwise((pl.col("close") - pl.col("ma_20")) / pl.col("atr_14"))
        .alias("atr_ext_20")
    )


def _with_trend(frame: pl.DataFrame) -> pl.DataFrame:
    """``ma50_slope_20`` and ``eff_ratio_63``."""
    ma_base = pl.col("ma_50").shift(MA_SLOPE_LOOKBACK).over(_ID)
    net = (pl.col("close") - pl.col("close").shift(EFFICIENCY_CHANGES)).abs().over(_ID)
    path = (
        pl.col("close")
        .diff()
        .abs()
        .rolling_sum(window_size=EFFICIENCY_CHANGES, min_samples=EFFICIENCY_CHANGES)
        .over(_ID)
    )
    return frame.with_columns(
        pl.when(pl.col("ma_50").is_null() | ma_base.is_null() | (ma_base <= 0))
        .then(None)
        .otherwise((pl.col("ma_50") / ma_base - 1) * 100)
        .alias("ma50_slope_20"),
        # A flat path has no efficiency: 0/0 is undefined, not zero (C1 "NULL if denominator 0").
        pl.when(net.is_null() | path.is_null() | (path < ZERO_DENOMINATOR_EPSILON))
        .then(None)
        .otherwise(net / path)
        .alias("eff_ratio_63"),
    )


def _with_downside_volatility(
    frame: pl.DataFrame, windows: dict[int, FactorWindow]
) -> pl.DataFrame:
    """``downside_vol_Nm`` = sqrt(mean of min(r, 0)^2 over vol_Nm's N returns) x sqrt(252).

    The mean divides by N — every return, not only the negative ones — which is what makes it
    comparable with ``vol_Nm`` (population moments on both sides).
    """
    returns = pl.col("daily_return")
    squared_loss = (
        pl.when(returns.is_null())
        .then(None)
        .when(returns < 0)
        .then(returns * returns)
        .otherwise(0.0)
    )
    expressions: list[pl.Expr] = []
    for months in (6, 12):
        window = windows[months]
        name = f"downside_vol_{months}m"
        if not window.spans_full_window:
            expressions.append(pl.lit(None, dtype=pl.Float64).alias(name))
            continue
        n = window.length
        # A rolling mean of non-negative numbers can land a hair below zero from the sliding
        # add/subtract; clip before the square root rather than let it become NaN.
        mean_square = (
            squared_loss.rolling_mean(window_size=n, min_samples=n).over(_ID).clip(lower_bound=0.0)
        )
        expressions.append((mean_square.sqrt() * _SQRT_YEAR).alias(name))
    return frame.with_columns(expressions)


def _with_acceleration(frame: pl.DataFrame) -> pl.DataFrame:
    """``accel_21_105`` and its volatility-scaled form, on non-overlapping log-return blocks."""
    logs = pl.col(_LOG_RETURN)
    recent = logs.rolling_mean(window_size=ACCEL_RECENT, min_samples=ACCEL_RECENT).over(_ID)
    prior = (
        logs.rolling_mean(window_size=ACCEL_PRIOR, min_samples=ACCEL_PRIOR)
        .shift(ACCEL_RECENT)
        .over(_ID)
    )
    span = ACCEL_RECENT + ACCEL_PRIOR
    spread = logs.rolling_std(window_size=span, min_samples=span, ddof=1).over(_ID)
    frame = frame.with_columns((recent - prior).alias("accel_21_105"))
    return frame.with_columns(
        pl.when(
            pl.col("accel_21_105").is_null()
            | spread.is_null()
            | (spread < ZERO_DENOMINATOR_EPSILON)
        )
        .then(None)
        .otherwise(pl.col("accel_21_105") / spread)
        .alias("accel_21_105_vs")
    )


def _with_participation(frame: pl.DataFrame) -> pl.DataFrame:
    """``vol_exp_21_126`` and ``vol_persist_20`` over ``vol_day_val`` (traded value, ₹)."""
    value = pl.col("vol_day_val")
    recent = value.rolling_mean(window_size=VOLUME_RECENT, min_samples=VOLUME_RECENT).over(_ID)
    base = (
        value.rolling_mean(window_size=VOLUME_BASE, min_samples=VOLUME_BASE)
        .shift(VOLUME_RECENT)
        .over(_ID)
    )
    frame = frame.with_columns(
        pl.when(recent.is_null() | base.is_null() | (base <= 0))
        .then(None)
        .otherwise(recent / base)
        .alias("vol_exp_21_126"),
        value.rolling_mean(window_size=VOLUME_BASE, min_samples=VOLUME_BASE)
        .shift(VOLUME_PERSIST_SESSIONS)
        .over(_ID)
        .alias(_VOLUME_BASELINE),
    )
    # One fixed baseline per row, compared with each of the last 20 sessions' own values.
    above = [
        (value.shift(lag).over(_ID) > pl.col(_VOLUME_BASELINE)).cast(pl.Float64)
        for lag in range(VOLUME_PERSIST_SESSIONS)
    ]
    complete = (
        value.is_not_null()
        .cast(pl.Float64)
        .rolling_sum(window_size=VOLUME_PERSIST_SESSIONS, min_samples=VOLUME_PERSIST_SESSIONS)
        .over(_ID)
        == VOLUME_PERSIST_SESSIONS
    )
    return frame.with_columns(
        pl.when(pl.col(_VOLUME_BASELINE).is_null() | complete.is_null() | ~complete)
        .then(None)
        .otherwise(pl.sum_horizontal(above))
        .alias("vol_persist_20")
    )


def _with_relative_strength(
    frame: pl.DataFrame, market_benchmark: pl.DataFrame | None
) -> pl.DataFrame:
    """``rs_persist_126`` — % of the last 126 sessions whose 20-session return beat NIFTY 500's."""
    levels = benchmark_levels(market_benchmark)
    if levels is None:
        return frame.with_columns(
            pl.lit(None, dtype=pl.Float64).alias(_BENCH_RETURN_20),
            pl.lit(None, dtype=pl.Float64).alias("rs_persist_126"),
        )
    base_level = pl.col("level").shift(RS_RETURN_SESSIONS)
    bench = levels.with_columns(
        pl.when(base_level.is_null() | (base_level <= 0))
        .then(None)
        .otherwise(pl.col("level") / base_level - 1)
        .alias(_BENCH_RETURN_20)
    ).select("date", _BENCH_RETURN_20)
    if _BENCH_RETURN_20 in frame.columns:
        frame = frame.drop(_BENCH_RETURN_20)
    frame = frame.join(bench, on="date", how="left", maintain_order="left")

    stock_base = pl.col("close").shift(RS_RETURN_SESSIONS).over(_ID)
    stock_return = (
        pl.when(stock_base.is_null() | (stock_base <= 0))
        .then(None)
        .otherwise(pl.col("close") / stock_base - 1)
    )
    beat = (
        pl.when(stock_return.is_null() | pl.col(_BENCH_RETURN_20).is_null())
        .then(None)
        .otherwise((stock_return > pl.col(_BENCH_RETURN_20)).cast(pl.Float64))
    )
    return frame.with_columns(
        (
            beat.rolling_mean(
                window_size=RS_PERSIST_SESSIONS, min_samples=RS_PERSIST_SESSIONS
            ).over(_ID)
            * 100
        ).alias("rs_persist_126")
    )


# ---------------------------------------------------------------------------
# Factors that are only defined at the as-of row
# ---------------------------------------------------------------------------


def with_ranking_at_as_of(  # noqa: PLR0913 - two frames, a date, a calendar, two benchmarks
    at_as_of: pl.DataFrame,
    history: pl.DataFrame,
    *,
    as_of: dt.date,
    trading_days: list[dt.date] | tuple[dt.date, ...],
    windows: dict[int, FactorWindow],
    benchmark: pl.DataFrame | None,
    market_benchmark: pl.DataFrame | None,
) -> pl.DataFrame:
    """Attach the as-of-only C1 columns, NULL the unrepresentable, and drop working columns.

    ``at_as_of`` is one row per instrument dated ``as_of``, carrying the columns
    :func:`with_ranking_series` added plus ``ret_Nm`` and ``beta_12m``. ``history`` is the long
    frame it was filtered from; only its rows dated on or before ``as_of`` are read.
    """
    past = history.filter(pl.col("date") <= as_of)
    frame = _with_sortino(at_as_of)
    frame = _with_path_statistics(frame, past, windows)
    frame = _with_benchmark_relative(frame, as_of, windows, benchmark, market_benchmark)
    frame = _with_nse_ratios(frame, past, as_of, trading_days)
    frame = frame.with_columns(
        pl.lit(None, dtype=pl.Float64).alias("mom_pctile"),
        pl.lit(None, dtype=pl.Float64).alias("rank_persist_20"),
    )
    frame = frame.drop([c for c in INTERNAL_COLUMNS if c in frame.columns])
    return null_unrepresentable(frame)


def _with_sortino(frame: pl.DataFrame) -> pl.DataFrame:
    """``sortino_Nm`` = ret_Nm / (downside_vol_Nm x 100) — ``sharpe_Nm``'s shape and guard."""
    expressions: list[pl.Expr] = []
    for months in (6, 12):
        name = f"sortino_{months}m"
        ret = f"ret_{months}m"
        downside = f"downside_vol_{months}m"
        if ret not in frame.columns:
            expressions.append(pl.lit(None, dtype=pl.Float64).alias(name))
            continue
        expressions.append(
            pl.when(
                pl.col(downside).is_null()
                | (pl.col(downside) < ZERO_DENOMINATOR_EPSILON)
                | pl.col(ret).is_null()
            )
            .then(None)
            .otherwise(pl.col(ret) / (pl.col(downside) * 100))
            .alias(name)
        )
    return frame.with_columns(expressions)


def _with_path_statistics(
    frame: pl.DataFrame, past: pl.DataFrame, windows: dict[int, FactorWindow]
) -> pl.DataFrame:
    """``max_dd_6m``, ``max_dd_12m``, ``underwater_12m`` and ``ret_ex_top3_12m``.

    Each needs a running maximum or a sort *within* the window, which no rolling expression
    gives. The last ``N`` closes of every instrument are laid out as one ``(instrument, N)``
    matrix — right-aligned, NaN where an instrument has fewer bars — and the statistics are
    NumPy operations along the time axis, vectorised across instruments.
    """
    names = ("max_dd_6m", "max_dd_12m", "underwater_12m", "ret_ex_top3_12m")
    empty = frame.with_columns([pl.lit(None, dtype=pl.Float64).alias(n) for n in names])
    if frame.height == 0 or past.height == 0:
        return empty

    longest = max(windows[m].length for m in (6, 12))
    ids, closes = _tail_matrix(past, longest)
    results: dict[str, npt.NDArray[np.float64]] = {}
    for months in (6, 12):
        window = windows[months]
        n = window.length
        if not window.spans_full_window or n < 2:  # noqa: PLR2004 - one bar has no path
            nan = np.full(len(ids), np.nan)
            results[f"max_dd_{months}m"] = nan
            if months == 12:  # noqa: PLR2004 - the 12M-only statistics
                results["underwater_12m"] = nan
                results["ret_ex_top3_12m"] = nan
            continue
        block = closes[:, longest - n :]
        valid = np.all(np.isfinite(block) & (block > 0), axis=1)
        safe = np.where(valid[:, None], block, 1.0)
        running_max = np.maximum.accumulate(safe, axis=1)
        drawdown = (1.0 - safe / running_max).max(axis=1) * 100.0
        results[f"max_dd_{months}m"] = np.where(valid, drawdown, np.nan)
        if months == 12:  # noqa: PLR2004 - the 12M-only statistics
            under = (safe < running_max).sum(axis=1) / n * 100.0
            results["underwater_12m"] = np.where(valid, under, np.nan)
            results["ret_ex_top3_12m"] = np.where(
                valid & (n - 1 >= TOP_DAYS_EXCLUDED), _return_ex_top_days(safe), np.nan
            )

    addition = pl.DataFrame(
        {_ID: ids, **{name: results[name] for name in names}},
        schema_overrides={_ID: frame.schema[_ID]},
    ).with_columns([pl.col(n).fill_nan(None) for n in names])
    return frame.join(addition, on=_ID, how="left", maintain_order="left")


def _return_ex_top_days(closes: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """``(exp(sum l - sum of the 3 largest l) - 1) x 100`` over the window's N - 1 log returns.

    The window's N bars span N - 1 returns — the same N - 1 that ``ret_12m`` compounds — so with
    nothing removed this is exactly ``ret_12m``.
    """
    logs = np.diff(np.log(closes), axis=1)
    if logs.shape[1] < TOP_DAYS_EXCLUDED:
        return np.full(closes.shape[0], np.nan)
    largest = np.sort(logs, axis=1)[:, -TOP_DAYS_EXCLUDED:].sum(axis=1)
    result: npt.NDArray[np.float64] = np.expm1(logs.sum(axis=1) - largest) * 100.0
    return result


def _tail_matrix(past: pl.DataFrame, length: int) -> tuple[list[int], npt.NDArray[np.float64]]:
    """The last ``length`` closes per instrument, oldest first, right-aligned, NaN-padded."""
    ranked = past.select(_ID, "close").with_columns(
        (pl.len() - pl.int_range(pl.len())).over(_ID).alias("_from_end")
    )
    tails = ranked.filter(pl.col("_from_end") <= length)
    wide = tails.pivot(on="_from_end", index=_ID, values="close", aggregate_function=None).sort(_ID)
    ordered = [str(position) for position in range(length, 0, -1)]
    for column in ordered:
        if column not in wide.columns:
            wide = wide.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))
    matrix = wide.select([pl.col(c).cast(pl.Float64) for c in ordered]).to_numpy()
    return wide[_ID].to_list(), np.asarray(matrix, dtype=np.float64)


def _with_benchmark_relative(
    frame: pl.DataFrame,
    as_of: dt.date,
    windows: dict[int, FactorWindow],
    benchmark: pl.DataFrame | None,
    market_benchmark: pl.DataFrame | None,
) -> pl.DataFrame:
    """``excess_ret_3m/6m/12m`` (NIFTY 500) and ``resid_ret_12m`` (beta x NIFTY 50)."""
    market = benchmark_levels(market_benchmark)
    expressions: list[pl.Expr] = []
    for months in RANKING_WINDOW_MONTHS:
        name = f"excess_ret_{months}m"
        ret = f"ret_{months}m"
        index_return = benchmark_window_return(market, as_of, windows[months])
        if index_return is None or ret not in frame.columns:
            expressions.append(pl.lit(None, dtype=pl.Float64).alias(name))
            continue
        expressions.append((pl.col(ret) - pl.lit(index_return)).alias(name))

    nifty_50_return = benchmark_window_return(benchmark_levels(benchmark), as_of, windows[12])
    if nifty_50_return is None or "ret_12m" not in frame.columns or "beta_12m" not in frame.columns:
        expressions.append(pl.lit(None, dtype=pl.Float64).alias("resid_ret_12m"))
    else:
        expressions.append(
            pl.when(pl.col("ret_12m").is_null() | pl.col("beta_12m").is_null())
            .then(None)
            .otherwise(pl.col("ret_12m") - pl.col("beta_12m") * pl.lit(nifty_50_return))
            .alias("resid_ret_12m")
        )
    return frame.with_columns(expressions)


def _with_nse_ratios(
    frame: pl.DataFrame,
    past: pl.DataFrame,
    as_of: dt.date,
    trading_days: list[dt.date] | tuple[dt.date, ...],
) -> pl.DataFrame:
    """``nse_mr6``/``nse_mr12`` for the rebalance month containing ``as_of``.

    NSE §16: MR = (P(end M-1) / P(end M-7 or M-13) - 1) / sigma_p, sigma_p the annualised sample
    standard deviation of the last 252 daily log returns ending at the end of M-1. Every price is
    the instrument's own bar on that exact month-end date; a missing bar is a NULL, not a
    neighbouring day's price.
    """
    end = month_end_trading_day(trading_days, as_of, NSE_RETURN_END_MONTHS)
    bases = {
        "nse_mr6": month_end_trading_day(trading_days, as_of, NSE_MR6_BASE_MONTHS),
        "nse_mr12": month_end_trading_day(trading_days, as_of, NSE_MR12_BASE_MONTHS),
    }
    if end is None or past.height == 0:
        return frame.with_columns([pl.lit(None, dtype=pl.Float64).alias(n) for n in bases])

    at_end = past.filter(pl.col("date") == end).select(
        _ID, pl.col("close").alias("_rk_p_end"), pl.col(_NSE_SIGMA).alias("_rk_sigma_end")
    )
    frame = frame.join(at_end, on=_ID, how="left", maintain_order="left")
    working = ["_rk_p_end", "_rk_sigma_end"]
    expressions: list[pl.Expr] = []
    for name, base_day in bases.items():
        if base_day is None:
            expressions.append(pl.lit(None, dtype=pl.Float64).alias(name))
            continue
        base_column = f"_rk_p_{name}"
        working.append(base_column)
        at_base = past.filter(pl.col("date") == base_day).select(
            _ID, pl.col("close").alias(base_column)
        )
        frame = frame.join(at_base, on=_ID, how="left", maintain_order="left")
        expressions.append(
            pl.when(
                pl.col("_rk_p_end").is_null()
                | pl.col(base_column).is_null()
                | (pl.col(base_column) <= 0)
                | pl.col("_rk_sigma_end").is_null()
                | (pl.col("_rk_sigma_end") < ZERO_DENOMINATOR_EPSILON)
            )
            .then(None)
            .otherwise((pl.col("_rk_p_end") / pl.col(base_column) - 1) / pl.col("_rk_sigma_end"))
            .alias(name)
        )
    return frame.with_columns(expressions).drop(working)


def null_unrepresentable(frame: pl.DataFrame) -> pl.DataFrame:
    """NULL any C1 value that ``numeric(p, s)`` cannot hold, or that is NaN / infinite.

    ``numeric(10,4)`` overflows at 10^6: a volume expansion over a base period that barely traded,
    or an extension over an ATR a hair above the guard, can exceed it, and one such value would
    fail the whole nightly INSERT. A value that does not fit its column is not a measurement the
    column can state, so it is stored as NULL like every other undefined ratio.
    """
    expressions: list[pl.Expr] = []
    for name, (precision, scale) in RANKING_NUMERIC_TYPES.items():
        if name not in frame.columns:
            continue
        limit = 10.0 ** (precision - scale) - 0.5 * 10.0 ** (-scale)
        value = pl.col(name)
        expressions.append(
            pl.when(value.is_null() | value.is_nan() | value.is_infinite() | (value.abs() >= limit))
            .then(None)
            .otherwise(value)
            .alias(name)
        )
    return frame.with_columns(expressions) if expressions else frame


# ---------------------------------------------------------------------------
# Cross-sectional and cross-date columns, filled by the worker
# ---------------------------------------------------------------------------


def cross_sectional_pctile(frame: pl.DataFrame) -> pl.DataFrame:
    """Return ``frame`` with ``mom_pctile`` filled: C1's momentum percentile for one date.

    Percent rank x 100 of ``avg_sharpe_12_6_3_1`` among the date's written rows with
    ``universe_mask <> 0``: ``100 x (average-tie rank - 1) / (n - 1)``, ascending in value, so the
    best name is 100 and the worst 0; a population of one is 100. NULL for rows outside a
    universe or without a blend value. Pass the frame **as written** (rounded): the blend is the
    screener's SQL mean over the stored components, and ranking anything else would disagree with
    the screen on ties.

    ``avg_sharpe_12_6_3_1`` is read if present, otherwise computed from its four components with
    docs/05 §4's rule (NULL if any is NULL). The result is rounded to the column's 2 dp.
    """
    if _ID not in frame.columns or "universe_mask" not in frame.columns:
        raise ValueError("cross_sectional_pctile needs instrument_id and universe_mask")
    if MOM_PCTILE_SOURCE in frame.columns:
        source = pl.col(MOM_PCTILE_SOURCE).cast(pl.Float64)
    else:
        missing = [c for c in MOM_PCTILE_COMPONENTS if c not in frame.columns]
        if missing:
            raise ValueError(f"cross_sectional_pctile needs {MOM_PCTILE_SOURCE} or {missing}")
        source = blend_expr([f"{c}" for c in MOM_PCTILE_COMPONENTS]).cast(pl.Float64)

    value = pl.col(_PCTILE_VALUE)
    eligible = (
        pl.col("universe_mask").is_not_null()
        & (pl.col("universe_mask") != 0)
        & value.is_not_null()
        & value.is_finite()
    )
    work = frame.with_columns(source.alias(_PCTILE_VALUE))
    population = work.select(eligible.sum()).item()
    count = int(population) if population is not None else 0
    ranked = pl.when(eligible).then(value).otherwise(None).rank(method="average")
    percentile = (
        pl.when(~eligible)
        .then(None)
        .when(pl.lit(count) == 1)
        .then(pl.lit(100.0))
        .otherwise((ranked - 1) / max(count - 1, 1) * 100.0)
    )
    return (
        work.with_columns(percentile.cast(pl.Float64).alias("mom_pctile"))
        .with_columns(round_expr("mom_pctile", PERCENT_DP))
        .drop(_PCTILE_VALUE)
    )


def rank_persistence(history_frame: pl.DataFrame, as_of: dt.date) -> pl.DataFrame:
    """``rank_persist_20`` for every instrument with a row dated ``as_of``.

    ``history_frame`` carries ``(instrument_id, date, mom_pctile)`` for prior dates and ``as_of``.
    The window is the last 20 distinct dates in the frame on or before ``as_of`` (including it);
    the value is the % of those dates on which the instrument's ``mom_pctile`` was >= 80, and NULL
    unless all 20 carry a non-NULL ``mom_pctile`` for it. Rows dated after ``as_of`` are ignored.
    Returns ``(instrument_id, rank_persist_20)``, rounded to 2 dp.
    """
    required = {_ID, "date", "mom_pctile"}
    if not required.issubset(history_frame.columns):
        raise ValueError(f"rank_persistence needs {sorted(required)}; got {history_frame.columns}")
    past = history_frame.filter(pl.col("date") <= as_of).select(
        _ID, "date", pl.col("mom_pctile").cast(pl.Float64)
    )
    if past.select(pl.struct(_ID, "date").is_duplicated().any()).item():
        raise ValueError("rank_persistence was given the same (instrument_id, date) twice")

    today = past.filter(pl.col("date") == as_of).select(_ID)
    dates = past["date"].unique().sort()
    if dates.len() < RANK_PERSIST_DATES or today.height == 0:
        return today.with_columns(pl.lit(None, dtype=pl.Float64).alias("rank_persist_20"))

    window = dates.tail(RANK_PERSIST_DATES)
    counts = (
        past.filter(pl.col("date").is_in(window.implode()))
        .group_by(_ID)
        .agg(
            pl.col("mom_pctile").is_not_null().sum().alias("_rk_observed"),
            (pl.col("mom_pctile") >= RANK_PERSIST_THRESHOLD).sum().alias("_rk_hits"),
        )
    )
    return (
        today.join(counts, on=_ID, how="left", maintain_order="left")
        .with_columns(
            pl.when(
                pl.col("_rk_observed").is_null() | (pl.col("_rk_observed") < RANK_PERSIST_DATES)
            )
            .then(None)
            .otherwise(pl.col("_rk_hits").cast(pl.Float64) / RANK_PERSIST_DATES * 100.0)
            .alias("rank_persist_20")
        )
        .with_columns(round_expr("rank_persist_20", PERCENT_DP))
        .select(_ID, "rank_persist_20")
    )
