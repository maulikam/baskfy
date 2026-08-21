"""The factor engine (Prompt 5). Pure: DataFrames in, DataFrames out, no I/O.

docs/05 is the numerical contract; this module follows it section by section, and each block
below cites the section it implements. docs/02 §"Repo layout" requires this package to stay
I/O-free — "it takes DataFrames in and returns DataFrames out. That is what makes the factor math
unit-testable and the backtest engine reusable" — so the worker owns every read and write.

Shape of the computation
------------------------
One long frame, sorted by ``(instrument_id, date)``, carrying every instrument's history. Every
factor is a Polars window expression evaluated ``.over("instrument_id")``, so 2,300 instruments
are computed in one pass rather than in a loop — Prompt 5 deliverable 5: "vectorised — no Python
loops over instruments".

Two things resist a pure Polars expression and are done with NumPy over a *date x instrument*
matrix, which is still vectorised across instruments:

* **Wilder's RSI** (docs/05 §5) is a recursion seeded by a simple mean. Polars' ``ewm_mean`` seeds
  with the first observation instead, and for a 247-period RSI that difference has decayed by only
  a factor of ``(1 - 1/247)^250 ≈ 0.36`` after a year — nowhere near negligible. The recursion is
  run over time steps with every instrument advanced together.
* **The Wasserstein regime** (docs/05 §15) needs sorted-quantile distances against distributions
  built from each instrument's own rolling history.

Units, stated once
------------------
* ``ret_*``, ``sharpe_*``, ``away_high_*``, ``pos_days_*`` are **percent**.
* ``vol_*`` is a **decimal fraction** (0.5793…), not a percentage. docs/13 §2 finding 4 and §4
  settle this; docs/05 §2's ``x 100`` is the display form, and §3 spells the consequence out:
  ``sharpe_N = ret_N_pct / (vol_N_fraction * 100)``.
* ``vol_day_val`` and friends are **rupees**; ``marketcap_cr`` is **₹ crore**.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Final

import numpy as np
import polars as pl

from baskfy_core.circuits import (
    DEFAULT_CIRCUIT_CONFIG,
    CircuitConfig,
    circuit_hit_expr,
    circuit_method_expr,
)
from baskfy_core.instrument_regime import RegimeConfig, classify_regimes
from baskfy_core.momentum import (
    DEFAULT_SKIP_MONTH_CONFIG,
    SKIP_1M_BARS,
    SKIP_2M_BARS,
    SkipMonthConfig,
    skip_month_return_expr,
)
from baskfy_core.precision import apply_storage_precision
from baskfy_core.windows import (
    HIGH_1Y_BARS,
    MA_LENGTHS,
    MEDIAN_VOL_BARS,
    MIN_BETA_OBSERVATIONS,
    VOL_AVG_BARS,
    WINDOW_MONTHS,
    FactorWindow,
    resolve_windows,
)

#: docs/05 §2 — "x sqrt(252)". The annualisation constant, in trading days.
TRADING_DAYS_PER_YEAR: Final = 252
_SQRT_YEAR: Final = math.sqrt(TRADING_DAYS_PER_YEAR)

#: docs/05 §3's guard is "if vol_N == 0 ... -> sharpe_N = NULL", but a constant-return series has
#: a *floating-point* standard deviation around 1e-17 rather than exactly zero, so an equality
#: test never fires and the ratio explodes to ~1e14.
#:
#: The threshold is half of the last decimal place volatility is stored at (docs/13 §4: 10 dp).
#: Below it the value rounds to 0.0000000000 on write, so a sharpe derived from it could not be
#: reproduced from the stored row — which makes NULL the only self-consistent answer.
ZERO_VOLATILITY_EPSILON: Final = 0.5e-10

#: docs/06 §"Step 4" — "Threshold: top decile by beta / by 1-year volatility within each
#: universe ... Make it a named constant (`TOP_RISK_FLAG_PERCENTILE = 0.10`) so it can be
#: recalibrated against the fixture."
TOP_RISK_FLAG_PERCENTILE: Final = 0.10

#: Which turnover source a row used (docs/05 §13: "record which was used").
TURNOVER_EXCHANGE: Final = "exchange"
TURNOVER_DERIVED: Final = "close_x_volume"

#: Columns :func:`compute_factors` expects on the input frame.
REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "instrument_id",
    "date",
    "close",
    "close_raw",
    "high",
    "low",
    "volume_raw",
)

#: Columns it will use if present, and tolerate the absence of.
OPTIONAL_COLUMNS: Final[tuple[str, ...]] = (
    "turnover",
    "upper_circuit",
    "lower_circuit",
    "series",
    "marketcap_cr",
    "pe",
)


@dataclass(frozen=True, slots=True)
class FactorConfig:
    """Everything the engine can be recalibrated on without an edit."""

    circuits: CircuitConfig = DEFAULT_CIRCUIT_CONFIG
    skip_month: SkipMonthConfig = DEFAULT_SKIP_MONTH_CONFIG
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    window_months: tuple[int, ...] = WINDOW_MONTHS
    min_beta_observations: int = MIN_BETA_OBSERVATIONS


DEFAULT_FACTOR_CONFIG: Final = FactorConfig()


@dataclass(frozen=True, slots=True)
class FactorResult:
    """One row per instrument at ``as_of``, plus the window lengths that produced it."""

    frame: pl.DataFrame
    windows: dict[int, FactorWindow]

    @property
    def window_lengths(self) -> dict[int, int]:
        return {months: window.length for months, window in self.windows.items()}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_factors(
    bars: pl.DataFrame,
    as_of: dt.date,
    trading_days: list[dt.date] | tuple[dt.date, ...],
    benchmark: pl.DataFrame | None = None,
    config: FactorConfig = DEFAULT_FACTOR_CONFIG,
) -> FactorResult:
    """Compute every factor in docs/05 for one as-of date, rounded for storage.

    ``bars`` is the long history for every instrument being computed — not just the as-of day.
    ``benchmark`` is the NIFTY 50 level series (``date``, ``close``) for beta (docs/05 §6).

    Rounding happens here because CLAUDE.md house rule 8 requires it at write time: the API, the
    UI and the CSV export all read the stored row, so a value rounded three times independently
    would disagree in the last digit on three surfaces. :func:`compute_factors_unrounded` is the
    same computation without that final step, and has exactly one caller.
    """
    return FactorResult(
        apply_storage_precision(
            compute_factors_unrounded(bars, as_of, trading_days, benchmark, config).frame
        ),
        resolve_windows(as_of, trading_days, config.window_months),
    )


def compute_factors_unrounded(
    bars: pl.DataFrame,
    as_of: dt.date,
    trading_days: list[dt.date] | tuple[dt.date, ...],
    benchmark: pl.DataFrame | None = None,
    config: FactorConfig = DEFAULT_FACTOR_CONFIG,
) -> FactorResult:
    """:func:`compute_factors` without docs/13 §4's storage rounding. **Never write this.**

    It exists for Prompt 19 §3's cross-validation harness, which compares this engine against an
    independent pandas oracle "to 4 decimal places" — a comparison that is meaningless against a
    frame already rounded to 2 dp. ``packages/core/tests/test_factor_crossvalidation.py`` asserts
    that no file under any ``src/`` tree calls it, so a write path cannot quietly adopt it and
    break the CSV/API/UI tie that house rule 8 exists to protect.
    """
    _require_columns(bars)
    windows = resolve_windows(as_of, trading_days, config.window_months)

    frame = _prepare(bars, config)
    frame = _with_returns(frame)
    frame = _with_window_factors(frame, windows, config)
    frame = _with_price_levels(frame)
    frame = _with_skip_month(frame, config)
    frame = _with_liquidity(frame)
    frame = _with_rsi(frame, windows)
    frame = _with_beta(frame, benchmark, config)
    frame = classify_regimes(frame, config.regime)

    at_as_of = frame.filter(pl.col("date") == as_of)
    at_as_of = _null_short_windows(at_as_of, windows)
    return FactorResult(at_as_of, windows)


def _require_columns(bars: pl.DataFrame) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in bars.columns]
    if missing:
        raise ValueError(f"bars are missing required columns {missing}; got {bars.columns}")


def _prepare(bars: pl.DataFrame, config: FactorConfig) -> pl.DataFrame:
    """Sort, fill in optional columns, and derive the per-row inputs the rest depends on."""
    frame = bars.sort(["instrument_id", "date"])
    for column in OPTIONAL_COLUMNS:
        if column not in frame.columns:
            dtype = pl.String if column == "series" else pl.Float64
            frame = frame.with_columns(pl.lit(None, dtype=dtype).alias(column))

    frame = frame.with_columns(
        [pl.col(c).cast(pl.Float64) for c in ("close", "close_raw", "high", "low")]
        + [pl.col("volume_raw").cast(pl.Float64)]
        + [pl.col(c).cast(pl.Float64) for c in ("turnover", "upper_circuit", "lower_circuit")]
    )

    frame = frame.with_columns(
        pl.col("close_raw").shift(1).over("instrument_id").alias("prev_close_raw")
    )

    # docs/05 §13, corrected from the export: "the reference product's `volume` column is the
    # exchange's traded turnover in ₹, not close x shares ... Use the exchange turnover field;
    # only fall back to close x volume when turnover is unavailable, and record which was used."
    frame = frame.with_columns(
        pl.when(pl.col("turnover").is_not_null() & (pl.col("turnover") > 0))
        .then(pl.col("turnover"))
        .otherwise(pl.col("close_raw") * pl.col("volume_raw"))
        .alias("vol_day_val"),
        pl.when(pl.col("turnover").is_not_null() & (pl.col("turnover") > 0))
        .then(pl.lit(TURNOVER_EXCHANGE))
        .otherwise(pl.lit(TURNOVER_DERIVED))
        .alias("turnover_source"),
    )

    # docs/05 §12 — INFERRED circuit detection, and the method used, per row.
    return frame.with_columns(
        circuit_hit_expr(config.circuits).alias("circuit_hit"),
        circuit_method_expr(config.circuits).alias("circuit_method"),
    )


def _with_returns(frame: pl.DataFrame) -> pl.DataFrame:
    """docs/05 §Notation: ``r_t = P_t / P_{t-1} - 1`` on the *adjusted* close."""
    previous = pl.col("close").shift(1).over("instrument_id")
    return frame.with_columns(
        pl.when(previous.is_null() | (previous == 0))
        .then(None)
        .otherwise(pl.col("close") / previous - 1)
        .alias("daily_return")
    )


def _with_window_factors(
    frame: pl.DataFrame, windows: dict[int, FactorWindow], config: FactorConfig
) -> pl.DataFrame:
    """docs/05 §1 returns, §2 volatility, §3 sharpe, §11 positive days, §12 circuits."""
    del config
    expressions: list[pl.Expr] = []
    for window in windows.values():
        n = window.length
        key = window.key

        if not window.spans_full_window:
            # The calendar does not reach the offset, so this is not an N-month window — it is
            # whatever history happened to exist, and docs/05 §Notation forbids computing on it.
            for name in (
                f"ret_{key}",
                f"vol_{key}",
                f"sharpe_{key}",
                f"rsi_{key}",
                f"pos_days_{key}",
                f"circuits_{key}",
            ):
                expressions.append(pl.lit(None, dtype=pl.Float64).alias(name))
            continue

        # §1: ret_N = (P_t / P_{t-(N-1)} - 1) x 100. The base is the window's FIRST BAR, because
        # the calendar-offset window is inclusive of both endpoints (docs/13 §3).
        #
        # This was `shift(n)` until 2026-08-22 — the bar *before* the window — which is what
        # docs/05 §1 said and what nothing had ever checked, because the parity test needed price
        # history the repository did not have and therefore skipped. On real bars against all 271
        # export rows, `shift(n)` reproduces 0, 1, 0, 0, 0 cells across the five windows and
        # `shift(n - 1)` reproduces 265, 260, 256, 250, 240. docs/05 §1 was corrected first, then
        # this line, in that order (docs/DECISIONS.md §21.7).
        base = pl.col("close").shift(n - 1).over("instrument_id")
        expressions.append(
            pl.when(base.is_null() | (base == 0))
            .then(None)
            .otherwise((pl.col("close") / base - 1) * 100)
            .alias(f"ret_{key}")
        )

        # §2: sample stdev (ddof=1) of the window's N daily returns, annualised.
        # Stored as a FRACTION (docs/13 §2 finding 4), so no x100 here.
        expressions.append(
            (pl.col("daily_return").rolling_std(window_size=n, min_samples=n, ddof=1) * _SQRT_YEAR)
            .over("instrument_id")
            .alias(f"vol_{key}")
        )

        # §11: pos_days_N = count(r_i > 0 over the window) / N x 100. The denominator is N, which
        # is why docs/13 §3 could recover it from the values being multiples of 1/N.
        expressions.append(
            (
                (pl.col("daily_return") > 0)
                .cast(pl.Float64)
                .rolling_sum(window_size=n, min_samples=n)
                / n
                * 100
            )
            .over("instrument_id")
            .alias(f"pos_days_{key}")
        )

        # §12: circuit-hit count over the window (INFERRED — see baskfy_core.circuits).
        expressions.append(
            pl.col("circuit_hit")
            .cast(pl.Float64)
            .rolling_sum(window_size=n, min_samples=n)
            .over("instrument_id")
            .alias(f"circuits_{key}")
        )

    frame = frame.with_columns(expressions)

    # §3: sharpe_N = ret_N_pct / (vol_N_fraction x 100). "Guard: if vol_N == 0 or is NULL ->
    # sharpe_N = NULL." A zero-volatility window is a flat series, where the ratio is undefined
    # rather than infinite.
    return frame.with_columns(
        [
            pl.when(
                pl.col(f"vol_{months}m").is_null()
                | (pl.col(f"vol_{months}m").abs() < ZERO_VOLATILITY_EPSILON)
                | pl.col(f"ret_{months}m").is_null()
            )
            .then(None)
            .otherwise(pl.col(f"ret_{months}m") / (pl.col(f"vol_{months}m") * 100))
            .alias(f"sharpe_{months}m")
            for months in windows
        ]
    )


def _with_price_levels(frame: pl.DataFrame) -> pl.DataFrame:
    """docs/05 §9 moving averages, §10 highs and away-from-high."""
    expressions = [
        pl.col("close")
        .rolling_mean(window_size=k, min_samples=k)
        .over("instrument_id")
        .alias(f"ma_{k}")
        for k in MA_LENGTHS
    ]
    expressions += [
        # THE HIGH IS THE INTRADAY HIGH, NOT THE CLOSE. Measured 2026-08-22 against all 271 rows
        # of the reference export on real bars: `max(high)` reproduces **249 of 268** and
        # `max(close)` reproduces **6**. The result is flat across every window length from 243 to
        # 248 bars, so the gain is the input and not the window — which is what makes it safe to
        # change while the window question is still open (docs/DECISIONS-MERGE.md M11.5).
        #
        # It reads naturally too: "away from its 1-year high" means away from the highest price
        # the stock traded at, not the highest price it happened to close at.
        pl.col("high")
        .rolling_max(window_size=HIGH_1Y_BARS, min_samples=HIGH_1Y_BARS)
        .over("instrument_id")
        .alias("high_1y"),
        # §10: "max(P over the full history)". cum_max is the all-time high as at each bar, which
        # is what point-in-time correctness requires — an ATH set next year must not affect today.
        # Same input for the same reason; unverifiable here because our history starts in 2024
        # (§21.2), so it is a 2024-onward maximum wearing an all-time label either way.
        pl.col("high").cum_max().over("instrument_id").alias("high_ath"),
    ]
    frame = frame.with_columns(expressions)

    return frame.with_columns(
        [
            pl.when(pl.col(high).is_null() | (pl.col(high) == 0))
            .then(None)
            .otherwise((pl.col("close") / pl.col(high) - 1) * 100)
            .alias(away)
            for high, away in (("high_1y", "away_high_1y"), ("high_ath", "away_high_ath"))
        ]
    )


def _with_skip_month(frame: pl.DataFrame, config: FactorConfig) -> pl.DataFrame:
    """docs/05 §8 — INFERRED. See baskfy_core.momentum for the two candidate definitions."""
    return frame.with_columns(
        skip_month_return_expr("close", SKIP_1M_BARS, config.skip_month)
        .over("instrument_id")
        .alias("ret_12m_minus_1m"),
        skip_month_return_expr("close", SKIP_2M_BARS, config.skip_month)
        .over("instrument_id")
        .alias("ret_12m_minus_2m"),
    )


def _with_liquidity(frame: pl.DataFrame) -> pl.DataFrame:
    """docs/05 §13 — rolling means of daily rupee turnover, and the 1-year *median*.

    docs/05 on why the filter uses the median: "a single block deal should not qualify an
    illiquid name."
    """
    expressions = [
        pl.col("vol_day_val")
        .rolling_mean(window_size=bars, min_samples=bars)
        .over("instrument_id")
        .alias(f"vol_avg_{key}")
        for key, bars in VOL_AVG_BARS.items()
    ]
    expressions.append(
        pl.col("vol_day_val")
        .rolling_median(window_size=MEDIAN_VOL_BARS, min_samples=MEDIAN_VOL_BARS)
        .over("instrument_id")
        .alias("median_vol_12m")
    )
    return frame.with_columns(expressions)


def _with_rsi(frame: pl.DataFrame, windows: dict[int, FactorWindow]) -> pl.DataFrame:
    """docs/05 §5 — Wilder's RSI with period = the window's trading-day count.

    "so 'RSI 1 year' is a 252-period RSI, not a 14-period RSI sampled yearly".

    Seeded with a simple mean over the first ``N`` observations of the available history and then
    smoothed forward, exactly as docs/05 specifies. That seeding is why this cannot be Polars'
    ``ewm_mean``, which seeds with the first observation: at N=247 the two answers are still ~36%
    apart a year later.
    """
    empty = [pl.lit(None, dtype=pl.Float64).alias(f"rsi_{months}m") for months in windows]
    if frame.height == 0:
        return frame.with_columns(empty)

    pivot = frame.pivot(on="instrument_id", index="date", values="close", aggregate_function=None)
    dates = pivot["date"].to_list()
    columns = [c for c in pivot.columns if c != "date"]
    # A single bar yields no daily changes, so there is nothing for Wilder to smooth. Returning
    # NULL is docs/05's own answer for a window without enough history.
    if len(dates) < 2:  # noqa: PLR2004 - two bars are the minimum for one change
        return frame.with_columns(empty)

    prices = pivot.select(columns).to_numpy().astype(float)
    changes = np.diff(prices, axis=0)
    gains = np.where(changes > 0, changes, 0.0)
    losses = np.where(changes < 0, -changes, 0.0)

    additions: list[pl.DataFrame] = []
    for months, window in windows.items():
        rsi = _wilder_rsi(gains, losses, window.length)
        additions.append(
            pl.DataFrame({"date": dates[1:], **{col: rsi[:, i] for i, col in enumerate(columns)}})
            .unpivot(index="date", variable_name="instrument_id", value_name=f"rsi_{months}m")
            .with_columns(pl.col("instrument_id").cast(frame.schema["instrument_id"]))
        )

    for addition in additions:
        frame = frame.join(addition, on=["date", "instrument_id"], how="left")
    return frame


def _wilder_rsi(gains: np.ndarray, losses: np.ndarray, period: int) -> np.ndarray:
    """Wilder RSI over a ``(time, instrument)`` matrix. One loop over time, all instruments at once.

    NaN before the seed is complete, which becomes NULL on the way back into Polars — docs/05's
    rule that an instrument without a full window gets no value.
    """
    steps, instruments = gains.shape
    out = np.full((steps, instruments), np.nan, dtype=float)
    if steps < period or period < 1:
        return out

    with np.errstate(invalid="ignore"):
        avg_gain = np.nanmean(gains[:period], axis=0)
        avg_loss = np.nanmean(losses[:period], axis=0)
    out[period - 1] = _rsi_from(avg_gain, avg_loss)

    # docs/05 §5: avg_x_t = (avg_x_{t-1} x (N-1) + x_t) / N
    decay = (period - 1) / period
    increment = 1.0 / period
    for step in range(period, steps):
        avg_gain = avg_gain * decay + gains[step] * increment
        avg_loss = avg_loss * decay + losses[step] * increment
        out[step] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: np.ndarray, avg_loss: np.ndarray) -> np.ndarray:
    """``RSI = 100 - 100/(1 + RS)``, with docs/05's rule that no losses means RSI 100."""
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.divide(avg_gain, avg_loss)
        rsi = 100.0 - 100.0 / (1.0 + rs)
    return np.where(avg_loss == 0, np.where(avg_gain == 0, 50.0, 100.0), rsi)


def _with_beta(
    frame: pl.DataFrame, benchmark: pl.DataFrame | None, config: FactorConfig
) -> pl.DataFrame:
    """docs/05 §6 — ``Cov(r_stock, r_bench)_252 / Var(r_bench)_252`` against NIFTY 50.

    "Align on common trading days; require >= 200 overlapping observations else NULL. Negative
    betas are legitimate and must not be clipped."

    Computed from rolling moments — ``cov = E[xy] - E[x]E[y]``, ``var = E[y^2] - E[y]^2`` — with
    population moments on both sides, whose ``1/n`` factors cancel in the ratio. That keeps it a
    Polars expression over every instrument at once rather than a per-instrument regression.
    """
    if benchmark is None or benchmark.height == 0:
        return frame.with_columns(pl.lit(None, dtype=pl.Float64).alias("beta_12m"))

    bench = (
        benchmark.select(["date", pl.col("close").cast(pl.Float64).alias("bench_close")])
        .sort("date")
        .with_columns(
            (pl.col("bench_close") / pl.col("bench_close").shift(1) - 1).alias("bench_return")
        )
        .select(["date", "bench_return"])
    )
    frame = frame.join(bench, on="date", how="left")

    n = TRADING_DAYS_PER_YEAR
    paired = pl.col("daily_return").is_not_null() & pl.col("bench_return").is_not_null()
    stock = pl.when(paired).then(pl.col("daily_return")).otherwise(None)
    market = pl.when(paired).then(pl.col("bench_return")).otherwise(None)

    frame = frame.with_columns(
        stock.alias("_r_stock"), market.alias("_r_bench"), (stock * market).alias("_r_product")
    )
    frame = frame.with_columns(
        pl.col("_r_stock")
        .rolling_mean(window_size=n, min_samples=config.min_beta_observations)
        .over("instrument_id")
        .alias("_mean_stock"),
        pl.col("_r_bench")
        .rolling_mean(window_size=n, min_samples=config.min_beta_observations)
        .over("instrument_id")
        .alias("_mean_bench"),
        pl.col("_r_product")
        .rolling_mean(window_size=n, min_samples=config.min_beta_observations)
        .over("instrument_id")
        .alias("_mean_product"),
        (pl.col("_r_bench") ** 2)
        .rolling_mean(window_size=n, min_samples=config.min_beta_observations)
        .over("instrument_id")
        .alias("_mean_bench_sq"),
    )

    covariance = pl.col("_mean_product") - pl.col("_mean_stock") * pl.col("_mean_bench")
    variance = pl.col("_mean_bench_sq") - pl.col("_mean_bench") ** 2
    frame = frame.with_columns(
        pl.when(variance.is_null() | (variance <= 0))
        .then(None)
        .otherwise(covariance / variance)
        .alias("beta_12m")
    )
    return frame.drop(
        [
            "_r_stock",
            "_r_bench",
            "_r_product",
            "_mean_stock",
            "_mean_bench",
            "_mean_product",
            "_mean_bench_sq",
        ]
    )


def _null_short_windows(frame: pl.DataFrame, windows: dict[int, FactorWindow]) -> pl.DataFrame:
    """docs/05 §Notation: an instrument without a full window gets NULL for that window.

    "never computed on a short window, because that would break the shared-denominator property".

    Polars' ``min_samples`` already enforces this for the rolling factors; this makes the rule
    explicit for the derived ones, so a change to a rolling call cannot quietly reintroduce a
    part-window value.
    """
    expressions: list[pl.Expr] = []
    for months in windows:
        key = f"{months}m"
        incomplete = pl.col(f"vol_{key}").is_null() | pl.col(f"ret_{key}").is_null()
        for prefix in ("sharpe", "pos_days", "circuits"):
            column = f"{prefix}_{key}"
            if column in frame.columns:
                expressions.append(
                    pl.when(incomplete).then(None).otherwise(pl.col(column)).alias(column)
                )
    return frame.with_columns(expressions) if expressions else frame
