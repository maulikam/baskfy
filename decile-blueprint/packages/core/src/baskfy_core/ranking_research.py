"""Research ranking candidates — not Sort By keys until a promotion decision.

docs/ranking/PLAN.md Phase 1.3. These helpers compute candidate signals for research /
hold-out validation. They are deliberately **absent** from ``FACTORS`` so they cannot appear
in the Sort By dropdown without an explicit registry + decision entry.

Corrections locked in (PLAN, 13 Sep 2026)
-----------------------------------------
1. Excess return vs a *common* index is a **filter / column, not a rank key** (identical
   subtraction does not reorder). Sector-relative / residual momentum can reorder later.
2. ``% above MA`` separates qualification from extension; extension prefers ``target_range``.
3. Positive-days % must pair with drawdown / downside before promotion alone — not computed here.
4. Acceleration uses **non-overlapping** log-return rates, never ``3m − 12m`` on overlapping windows.
5. High RSI / Sortino / Wasserstein stay out of this module until validated.
"""

from __future__ import annotations

from typing import Final

import numpy as np
import numpy.typing as npt
import pandas as pd

from baskfy_core.ranking import FactorPreference

#: Candidate keys — must never appear in ``FACTORS`` until promoted.
RESEARCH_CANDIDATE_KEYS: Final[tuple[str, ...]] = (
    "atr_extension",
    "ma_slope",
    "efficiency_ratio",
    "acceleration_nonoverlap",
    "excess_return_common_index",  # filter/column only — see preference
)

#: Preference metadata for research consumers (UI copy, notebooks). Not a Sort By contract.
RESEARCH_PREFERENCE: Final[dict[str, FactorPreference]] = {
    "atr_extension": FactorPreference.TARGET_RANGE,
    "ma_slope": FactorPreference.HIGHER,
    "efficiency_ratio": FactorPreference.HIGHER,
    "acceleration_nonoverlap": FactorPreference.HIGHER,
    # Explicitly not a rank key — preference is eligibility/filter semantics.
    "excess_return_common_index": FactorPreference.ELIGIBILITY,
}


def atr_extension(
    close: npt.NDArray[np.floating] | pd.Series,
    atr: npt.NDArray[np.floating] | pd.Series,
    ma: npt.NDArray[np.floating] | pd.Series,
) -> npt.NDArray[np.floating]:
    """(close − MA) / ATR — extension in volatility units.

    Preference is ``target_range``: large positive values are overextension risk, not "better".
    Qualification (close > MA) is a separate boolean filter, not this number.
    """
    c = np.asarray(close, dtype=float)
    a = np.asarray(atr, dtype=float)
    m = np.asarray(ma, dtype=float)
    out = np.full(c.shape, np.nan, dtype=float)
    ok = np.isfinite(c) & np.isfinite(a) & np.isfinite(m) & (a > 0)
    out[ok] = (c[ok] - m[ok]) / a[ok]
    return out


def ma_slope(
    ma: npt.NDArray[np.floating] | pd.Series,
    *,
    lookback: int = 20,
) -> npt.NDArray[np.floating]:
    """Relative change in an MA over ``lookback`` sessions: (MA_t / MA_{t-L}) − 1."""
    if lookback < 1:
        raise ValueError(f"lookback must be >= 1, got {lookback}")
    series = pd.Series(np.asarray(ma, dtype=float))
    prior = series.shift(lookback)
    ratio = series / prior - 1.0
    return ratio.to_numpy(dtype=float)


def efficiency_ratio(
    close: npt.NDArray[np.floating] | pd.Series,
    *,
    window: int = 20,
) -> npt.NDArray[np.floating]:
    """Kaufman efficiency: |net move| / sum(|daily moves|) over ``window``.

    1 = straight path; near 0 = noisy. Higher is cleaner trend participation.
    """
    if window < 2:
        raise ValueError(f"window must be >= 2, got {window}")
    series = pd.Series(np.asarray(close, dtype=float))
    net = (series - series.shift(window)).abs()
    path = series.diff().abs().rolling(window, min_periods=window).sum()
    out = net / path
    out = out.where(path > 0)
    return out.to_numpy(dtype=float)


def acceleration_nonoverlap(
    close: npt.NDArray[np.floating] | pd.Series,
    *,
    short: int = 63,
    long: int = 252,
) -> npt.NDArray[np.floating]:
    """Recent log-return rate minus the *preceding* equal-length rate (non-overlapping).

    With ``short=63`` (~3m) and ``long=252`` (~12m): compare the last 63 sessions' annualised
    log return to the 63 sessions that ended ``short`` ago — not ``3m − 12m`` on overlapping
    horizons (PLAN correction #4).
    """
    if short < 1 or long < short * 2:
        raise ValueError(
            f"need long >= 2*short for a non-overlapping pair; got short={short}, long={long}"
        )
    series = pd.Series(np.asarray(close, dtype=float))
    log_px = np.log(series.replace(0, np.nan))
    # Rate over the most recent `short` sessions.
    recent = (log_px - log_px.shift(short)) / short
    # Rate over the prior `short` sessions (ends where recent begins) — non-overlapping.
    prior = (log_px.shift(short) - log_px.shift(2 * short)) / short
    return (recent - prior).to_numpy(dtype=float)


def excess_return_common_index(
    asset_return: npt.NDArray[np.floating] | pd.Series,
    index_return: npt.NDArray[np.floating] | pd.Series,
) -> npt.NDArray[np.floating]:
    """Asset return minus a *common* index return.

    **Not a rank key.** Subtracting the same index return from every name does not reorder.
    Use as a filter / display column only (PLAN correction #1). Sector-relative excess that
    can reorder belongs in a later research promotion with its own decision entry.
    """
    a = np.asarray(asset_return, dtype=float)
    i = np.asarray(index_return, dtype=float)
    out = np.full(a.shape, np.nan, dtype=float)
    ok = np.isfinite(a) & np.isfinite(i)
    out[ok] = a[ok] - i[ok]
    return out


def research_frame(ohlc: pd.DataFrame, *, atr_col: str = "atr_14") -> pd.DataFrame:
    """Attach research columns to an OHLC(+ATR, optional index_return) frame.

    Expected columns: ``close``; optional ``ma_50`` (defaults to 50-session SMA of close);
    optional ``atr_14`` / ``atr_col``; optional ``index_return`` for the excess column.
    """
    if "close" not in ohlc.columns:
        raise ValueError("research_frame requires a close column")
    out = ohlc.copy()
    close = out["close"]
    ma = out["ma_50"] if "ma_50" in out.columns else close.rolling(50, min_periods=50).mean()
    if atr_col in out.columns:
        out["atr_extension"] = atr_extension(close, out[atr_col], ma)
    out["ma_slope"] = ma_slope(ma, lookback=20)
    out["efficiency_ratio"] = efficiency_ratio(close, window=20)
    out["acceleration_nonoverlap"] = acceleration_nonoverlap(close, short=63, long=252)
    if "ret_12m" in out.columns and "index_return" in out.columns:
        # filter / column, not a rank key
        out["excess_return_common_index"] = excess_return_common_index(
            out["ret_12m"], out["index_return"]
        )
    return out
