"""A deliberately naive pandas implementation of docs/05 — the **oracle** (Prompt 19 §3).

    "compute every factor with an independent, deliberately naive pandas implementation, and
     assert agreement with the Polars implementation to 4 decimal places. Keep both
     implementations; the naive one is the oracle."

The point of an oracle is that it shares no code with the thing it checks. So this module:

* imports nothing from :mod:`baskfy_core` except the *constants* that are part of docs/05's
  statement of the problem (window lengths, band tolerances) — never an expression, never a
  helper, never a Polars call;
* loops in Python, one instrument at a time, one bar at a time where the formula is a recursion.
  Every performance decision the engine makes (window expressions ``.over("instrument_id")``, the
  NumPy RSI matrix, the rolling-moment beta) is deliberately *not* made here. If the two agree,
  those optimisations preserved the arithmetic;
* transcribes each formula from docs/05 in the document's own notation, section by section, rather
  than from :mod:`baskfy_core.factors`.

docs/02 locks "Polars (primary) + NumPy; pandas only at boundaries". A test oracle is a boundary
in the only sense that matters here: nothing in ``packages/core/src`` imports it, and it never
runs in production. Using the *same* library for both implementations would make the comparison
much weaker — a shared misunderstanding of ``rolling(...).std()`` would cancel out.

WHAT IS NOT ORACLED, AND WHY
----------------------------
``regime`` (docs/05 §15) is an INFERRED *design* rather than a formula — docs/05 §15 specifies a
procedure ("take two reference distributions from that instrument's own history") whose
discretisation is a choice this repository made, not a number the document fixes. Transcribing
:mod:`baskfy_core.regime` into pandas would produce a copy, not an oracle, so it is left out and
said so here rather than counted as covered.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from typing import Final

import numpy as np
import pandas as pd

from baskfy_core.circuits import DEFAULT_BAND_TOLERANCE, DEFAULT_BANDS, DEFAULT_TICK
from baskfy_core.momentum import LOOKBACK_BARS, SKIP_1M_BARS, SKIP_2M_BARS
from baskfy_core.windows import (
    HIGH_1Y_BARS,
    MA_LENGTHS,
    MEDIAN_VOL_BARS,
    MIN_BETA_OBSERVATIONS,
    VOL_AVG_BARS,
)

#: docs/05 §2 — "x sqrt(252)".
SQRT_TRADING_YEAR: Final = math.sqrt(252)


def _returns(closes: list[float]) -> list[float | None]:
    """docs/05 §Notation: ``r_t = P_t / P_{t-1} - 1``, on the adjusted close."""
    out: list[float | None] = [None]
    for i in range(1, len(closes)):
        previous = closes[i - 1]
        out.append(None if previous == 0 else closes[i] / previous - 1)
    return out


def _sample_stdev(values: list[float]) -> float:
    """Sample standard deviation, ddof=1, written out longhand."""
    n = len(values)
    mean = sum(values) / n
    total = 0.0
    for v in values:
        total += (v - mean) ** 2
    return math.sqrt(total / (n - 1))


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    middle = n // 2
    if n % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _wilder_rsi_at(closes: list[float], index: int, period: int) -> float | None:
    """docs/05 §5, transcribed literally and run forward from the very first bar.

    "Seed with a simple mean over the first N observations, then
     avg_x_t = (avg_x_{t-1} x (N-1) + x_t) / N".

    One Python loop per instrument per window. The engine does this over a
    ``(time, instrument)`` NumPy matrix; agreeing with this loop is the evidence that the
    vectorisation did not change the recursion.
    """
    changes: list[float] = []
    for i in range(1, index + 1):
        changes.append(closes[i] - closes[i - 1])
    if len(changes) < period or period < 1:
        return None

    gains = [c if c > 0 else 0.0 for c in changes]
    losses = [-c if c < 0 else 0.0 for c in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for step in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[step]) / period
        avg_loss = (avg_loss * (period - 1) + losses[step]) / period

    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _circuit_hits(frame: pd.DataFrame) -> list[bool]:
    """docs/05 §12, transcribed clause by clause. INFERRED — see baskfy_core.circuits."""
    hits: list[bool] = []
    close_raw = frame["close_raw"].tolist()
    high = frame["high"].tolist()
    low = frame["low"].tolist()
    upper = frame["upper_circuit"].tolist()
    lower = frame["lower_circuit"].tolist()

    for i in range(len(frame)):
        published = False
        if upper[i] is not None and not pd.isna(upper[i]):
            published = published or close_raw[i] >= upper[i] - DEFAULT_TICK
        if lower[i] is not None and not pd.isna(lower[i]):
            published = published or close_raw[i] <= lower[i] + DEFAULT_TICK

        heuristic = False
        if i > 0:
            previous = close_raw[i - 1]
            flat = high[i] == low[i] and low[i] == close_raw[i]
            if flat and previous not in (0, None):
                move = abs(close_raw[i] / previous - 1)
                heuristic = any(move >= band - DEFAULT_BAND_TOLERANCE for band in DEFAULT_BANDS)

        hits.append(bool(published or heuristic))
    return hits


def _beta_at(
    stock_returns: list[float | None],
    bench_returns: list[float | None],
    index: int,
    span: int = 252,
    minimum: int = MIN_BETA_OBSERVATIONS,
) -> float | None:
    """docs/05 §6 — ``Cov(r_stock, r_bench)_252 / Var(r_bench)_252`` over paired observations.

    Textbook two-pass covariance over the aligned pairs, not the rolling-moment identity the
    engine uses. Population moments on both sides, so the ``1/n`` cancels exactly as it does
    there — that is the property being checked, and it is checked with different arithmetic.
    """
    start = max(0, index - span + 1)
    xs: list[float] = []
    ys: list[float] = []
    for i in range(start, index + 1):
        x, y = stock_returns[i], bench_returns[i]
        if x is None or y is None:
            continue
        xs.append(x)
        ys.append(y)
    if len(xs) < minimum:
        return None

    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    covariance = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n)) / n
    variance = sum((y - mean_y) ** 2 for y in ys) / n
    if variance <= 0:
        return None
    return covariance / variance


def compute_factors_pandas(
    bars: pd.DataFrame,
    as_of: dt.date,
    window_lengths: dict[int, int],
    benchmark: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Every docs/05 factor for one as-of date, one instrument at a time, in plain Python.

    ``bars`` carries ``instrument_id, date, close, close_raw, high, low, volume_raw`` and
    optionally ``turnover, upper_circuit, lower_circuit``. ``window_lengths`` is
    ``{months: N}`` — resolved by the caller from the trading calendar, because window
    *resolution* (docs/05 §Notation) is a separate concern from the factor arithmetic and has its
    own golden test.
    """
    bench_by_date: dict[dt.date, float] = {}
    if benchmark is not None and len(benchmark) > 0:
        ordered_bench = benchmark.sort_values("date")
        bench_dates = ordered_bench["date"].tolist()
        bench_closes = [float(c) for c in ordered_bench["close"].tolist()]
        bench_rets = _returns(bench_closes)
        for i, day in enumerate(bench_dates):
            value = bench_rets[i]
            if value is not None:
                bench_by_date[day] = value

    rows: list[dict[str, object]] = []
    for key_value, group in bars.groupby("instrument_id", sort=True):
        instrument_id = int(str(key_value))
        frame = group.sort_values("date").reset_index(drop=True)
        dates = frame["date"].tolist()
        if as_of not in dates:
            continue
        t = dates.index(as_of)

        closes = [float(c) for c in frame["close"].tolist()]
        # docs/05 §10: the highs are the INTRADAY highs, corrected 2026-08-22.
        highs = [float(h) for h in frame["high"].tolist()]
        returns = _returns(closes)
        # Typed as float-or-None throughout: docs/05's factors are all numbers, and keeping the
        # identifying columns out until the end means no cast is needed anywhere below.
        values: dict[str, float | None] = {}

        # --- docs/05 §1, §2, §3, §11, §12 --------------------------------
        hits = _circuit_hits(frame)
        for months, n in sorted(window_lengths.items()):
            key = f"{months}m"

            # §1: ret_N = (P_t / P_{t-(N-1)} - 1) x 100 — the base is the window's FIRST bar.
            # Corrected 2026-08-22 with docs/05 §1; this oracle is deliberately a naive
            # re-implementation of the spec, so it moves when the spec does, never to chase
            # baskfy_core.factors.
            if t - (n - 1) < 0:
                values[f"ret_{key}"] = None
            else:
                base = closes[t - (n - 1)]
                values[f"ret_{key}"] = None if base == 0 else (closes[t] / base - 1) * 100

            # §2: stdev(r over the window's N returns) x sqrt(252). Stored as a FRACTION.
            window_returns = [r for r in returns[t - n + 1 : t + 1] if r is not None]
            if t - n + 1 < 1 or len(window_returns) < n:
                values[f"vol_{key}"] = None
            else:
                values[f"vol_{key}"] = _sample_stdev(window_returns) * SQRT_TRADING_YEAR

            # §11: pos_days_N = count(r_i > 0) / N x 100
            if t - n + 1 < 1 or len(window_returns) < n:
                values[f"pos_days_{key}"] = None
            else:
                positive = sum(1 for r in window_returns if r > 0)
                values[f"pos_days_{key}"] = positive / n * 100

            # §12: circuits_N = count of circuit_hit over the last N bars
            if t - n + 1 < 0:
                values[f"circuits_{key}"] = None
            else:
                values[f"circuits_{key}"] = float(sum(1 for h in hits[t - n + 1 : t + 1] if h))

            # §3: sharpe_N = ret_N_pct / (vol_N_fraction x 100)
            ret = values[f"ret_{key}"]
            vol = values[f"vol_{key}"]
            if ret is None or vol is None or abs(vol) < 0.5e-10:
                values[f"sharpe_{key}"] = None
            else:
                values[f"sharpe_{key}"] = ret / (vol * 100)

            # §5: RSI with period = the window's trading-day count
            values[f"rsi_{key}"] = _wilder_rsi_at(closes, t, n)

            # docs/05's rule: a short window nulls everything derived from it.
            if values[f"vol_{key}"] is None or values[f"ret_{key}"] is None:
                for prefix in ("sharpe", "pos_days", "circuits"):
                    values[f"{prefix}_{key}"] = None

        # --- docs/05 §9 moving averages ----------------------------------
        for k in MA_LENGTHS:
            if t - k + 1 < 0:
                values[f"ma_{k}"] = None
            else:
                window = closes[t - k + 1 : t + 1]
                values[f"ma_{k}"] = sum(window) / k

        # --- docs/05 §10 highs and distance from high ---------------------
        if t - HIGH_1Y_BARS + 1 < 0:
            values["high_1y"] = None
        else:
            values["high_1y"] = max(highs[t - HIGH_1Y_BARS + 1 : t + 1])
        values["high_ath"] = max(highs[: t + 1])
        for high_key, away_key in (("high_1y", "away_high_1y"), ("high_ath", "away_high_ath")):
            high_value = values[high_key]
            if high_value is None or high_value == 0:
                values[away_key] = None
            else:
                values[away_key] = (closes[t] / high_value - 1) * 100

        # --- docs/05 §8 skip-month momentum (INFERRED, SKIP_END default) --
        for skip, name in ((SKIP_1M_BARS, "ret_12m_minus_1m"), (SKIP_2M_BARS, "ret_12m_minus_2m")):
            if t - LOOKBACK_BARS < 0 or t - skip < 0:
                values[name] = None
            else:
                denominator = closes[t - LOOKBACK_BARS]
                values[name] = (
                    None if denominator == 0 else (closes[t - skip] / denominator - 1) * 100
                )

        # --- docs/05 §13 liquidity ----------------------------------------
        daily_value: list[float] = []
        for i in range(len(frame)):
            turnover = frame["turnover"].iloc[i] if "turnover" in frame.columns else None
            if turnover is not None and not pd.isna(turnover) and float(turnover) > 0:
                daily_value.append(float(turnover))
            else:
                daily_value.append(
                    float(frame["close_raw"].iloc[i]) * float(frame["volume_raw"].iloc[i])
                )
        for label, span in VOL_AVG_BARS.items():
            if t - span + 1 < 0:
                values[f"vol_avg_{label}"] = None
            else:
                window = daily_value[t - span + 1 : t + 1]
                values[f"vol_avg_{label}"] = sum(window) / span
        if t - MEDIAN_VOL_BARS + 1 < 0:
            values["median_vol_12m"] = None
        else:
            values["median_vol_12m"] = _median(daily_value[t - MEDIAN_VOL_BARS + 1 : t + 1])

        # --- docs/05 §6 beta ----------------------------------------------
        if bench_by_date:
            paired_bench: list[float | None] = [bench_by_date.get(day) for day in dates]
            values["beta_12m"] = _beta_at(returns, paired_bench, t)
        else:
            values["beta_12m"] = None

        # --- docs/05 §7 beta-scaled ---------------------------------------
        beta = values["beta_12m"]
        beta_scaled = (
            ("ret_12m", "abs_div_beta_12m"),
            ("sharpe_12m", "sharpe_div_beta_12m"),
        )
        for source, name in beta_scaled:
            value = values[source]
            values[name] = (
                value / beta if beta is not None and beta > 0 and value is not None else None
            )

        rows.append({"instrument_id": instrument_id, "date": as_of, **values})

    return pd.DataFrame(rows)


def blend_pandas(row: pd.Series, prefix: str, shape: tuple[int, ...]) -> float | None:
    """docs/05 §4 — the plain arithmetic mean, NULL if *any* component is NULL."""
    components: list[float] = []
    for months in shape:
        value = row.get(f"{prefix}_{months}m")
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        components.append(float(value))
    return sum(components) / len(components)


def to_float_or_none(value: object) -> float | None:
    """pandas hands back ``nan`` where docs/05 says NULL; normalise before comparing.

    ``object`` in, because the value has just come out of a ``pd.Series`` or a ``pl.DataFrame``
    row and its static type is whatever the frame's schema says. Anything that is not a number is
    a bug in the caller's column list, so it raises rather than being coerced.
    """
    if value is None:
        return None
    if isinstance(value, float | np.floating):
        return None if math.isnan(float(value)) else float(value)
    if isinstance(value, int | np.integer):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"{value!r} ({type(value).__name__}) is not a numeric factor value")
