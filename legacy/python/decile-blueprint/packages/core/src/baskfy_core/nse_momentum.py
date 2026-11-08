"""NIFTY200 Momentum 30 score, exactly as NSE's methodology states it (docs/ranking/PLAN.md C3/C4).

Source: NSE Indices, *Method_NIFTY_Equity_Indices.pdf* (Sep 2026), §16 "Nifty200 Momentum 30".
PLAN correction 8: the "NSE Momentum" label is used only for the exact methodology and universe,
so every step below cites the text and nothing is added that the text does not say.

1. **Eligible** — a constituent of NIFTY 200 at the review, at least one year of listing history,
   and available for trading in the F&O segment.
2. **Momentum ratio** per horizon N ∈ {6, 12}: ``MR_N = R_N / sigma_p`` where
   ``R_N = P(M-1) / P(M-1-N) - 1`` with prices on the last trading day of those calendar months
   (M = the rebalance month), and ``sigma_p`` = the annualised standard deviation of daily lognormal
   returns over one year. The return is a fraction, not a percentage — the ratio is unit-free.
3. **Z-score** per horizon over the eligible universe: ``Z = (MR - mu) / sigma``.
4. **Weighted Z** = 0.5·Z12 + 0.5·Z6.
5. **Normalised score** = ``1 + Z`` if ``Z ≥ 0`` else ``(1 - Z)^-1``.
6. Top 30 by score form the index; the top 15 are compulsorily in and an existing constituent is
   dropped only below rank 45; semi-annual (June/December) reviews; weights are free-float
   market cap x score, capped at min(5%, 5 x FF weight). Steps 6's buffer, schedule and weights
   are **index construction**, not the score, so this module does not implement them and the
   preset says so.

Two readings the text leaves open, recorded in ``docs/DECISIONS-MERGE.md`` "Ranking 2.D":

* ``sigma_p`` uses the sample standard deviation (``ddof=1``) of the 252 log returns ending on the
  last trading day of M-1 (contract C1, shared with the stored ``nse_mr6``/``nse_mr12``).
* The Z-score's ``sigma`` is the **population** standard deviation (``ddof=0``): the PDF says
  "standard deviation" of the eligible universe, which is the whole population, not a sample.

No winsorisation or capping of Z is stated, so none is applied.

Pure: Series and dates in, Series and floats out. No clock — the rebalance month comes from the
caller's ``as_of``.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np
import pandas as pd

#: Bumped whenever a formula or reading in this module changes.
NSE_MOMENTUM_VERSION: Final = "nifty200-momentum30-2026.09"

#: The index whose point-in-time membership defines eligibility (step 1).
NSE_MOMENTUM_INDEX: Final = "nifty-200"

#: The published index keeps this many names (step 6, informational).
NSE_MOMENTUM_INDEX_SIZE: Final = 30

#: Sessions in the one-year volatility window and the annualisation factor (step 2).
SESSIONS_PER_YEAR: Final = 252

#: Horizon (months) → weight in the weighted Z (step 4).
HORIZON_WEIGHTS: Final[dict[int, float]] = {12: 0.5, 6: 0.5}


@dataclass(frozen=True, slots=True)
class Anchors:
    """Last trading day of months M-1, M-7 and M-13 for a rebalance month M."""

    end_m1: dt.date
    end_m7: dt.date
    end_m13: dt.date


@dataclass(frozen=True, slots=True)
class HorizonStats:
    """Mean and population standard deviation of one horizon's MR over the eligible universe."""

    mean: float
    std: float


@dataclass(frozen=True, slots=True)
class NsePopulation:
    """Z-score statistics computed over the *whole* eligible universe on ``as_of``.

    A screen's frame is often narrower than NSE's universe (a decile bucket, a smaller index).
    Standardising over that frame would produce a different number under the same label, so the
    caller that can see the full eligible set passes its statistics here.
    """

    mr6: HorizonStats
    mr12: HorizonStats


def _month_index(day: dt.date) -> int:
    return day.year * 12 + (day.month - 1)


def month_end_sessions(trading_days: Sequence[dt.date]) -> dict[int, dt.date]:
    """``month index → last trading day in that month`` from a trading calendar."""
    ends: dict[int, dt.date] = {}
    for day in trading_days:
        key = _month_index(day)
        current = ends.get(key)
        if current is None or day > current:
            ends[key] = day
    return ends


def anchors_for(as_of: dt.date, trading_days: Sequence[dt.date]) -> Anchors | None:
    """The three month-end anchors for the rebalance month containing ``as_of``.

    Only trading days strictly before the rebalance month are read, so the anchors of a month never
    change as that month's own sessions arrive (no look-ahead, and constant within the month).
    Returns ``None`` when the calendar does not reach back to M-13.
    """
    month = _month_index(as_of)
    ends = month_end_sessions([d for d in trading_days if _month_index(d) < month])
    try:
        return Anchors(end_m1=ends[month - 1], end_m7=ends[month - 7], end_m13=ends[month - 13])
    except KeyError:
        return None


def _month_end_position(series: pd.Series, anchor: dt.date) -> int | None:
    """Position of the close "at the end of" ``anchor``'s month.

    The bar on ``anchor`` itself, else the last bar earlier in the same calendar month: a stock
    suspended on the month's final session still has a month-end price. One with no bar in that
    month at all does not.
    """
    position = int(series.index.searchsorted(pd.Timestamp(anchor), side="right")) - 1
    if position < 0:
        return None
    day = series.index[position]
    if (day.year, day.month) != (anchor.year, anchor.month):
        return None
    return position


def momentum_ratios(closes: pd.Series, anchors: Anchors) -> tuple[float | None, float | None]:
    """``(MR6, MR12)`` for one instrument (step 2). ``None`` where the text cannot be applied.

    ``closes`` is the instrument's **adjusted** close indexed by session date, ascending. Nothing
    after the M-1 month-end bar is read. The volatility window is the 252 daily log returns ending
    at that bar (253 closes); a listing shorter than that has neither ratio, which is also step 1's
    one-year listing rule.
    """
    series = _as_dated_series(closes)
    end = _month_end_position(series, anchors.end_m1)
    if end is None or end < SESSIONS_PER_YEAR:
        return None, None
    window = series.iloc[end - SESSIONS_PER_YEAR : end + 1].to_numpy(dtype="float64")
    if not np.all(np.isfinite(window)) or np.any(window <= 0):
        return None, None
    sigma = float(np.std(np.diff(np.log(window)), ddof=1)) * math.sqrt(SESSIONS_PER_YEAR)
    if not sigma > 0:
        return None, None
    end_price = float(window[-1])

    def ratio(anchor: dt.date) -> float | None:
        start = _month_end_position(series, anchor)
        if start is None:
            return None
        price = float(series.iloc[start])
        if not (math.isfinite(price) and price > 0):
            return None
        return (end_price / price - 1.0) / sigma

    return ratio(anchors.end_m7), ratio(anchors.end_m13)


def _as_dated_series(closes: pd.Series) -> pd.Series:
    index = pd.DatetimeIndex(pd.to_datetime(closes.index))
    series = pd.Series(pd.to_numeric(closes.to_numpy()), index=index, dtype="float64")
    if not series.index.is_monotonic_increasing:
        raise ValueError("closes must be sorted by date ascending")
    return series


def normalise(z: float) -> float:
    """Step 5: ``1 + Z`` for ``Z ≥ 0``, else ``1 / (1 - Z)``. Always positive; continuous at 0."""
    return 1.0 + z if z >= 0 else 1.0 / (1.0 - z)


def eligibility(
    mr6: pd.Series, mr12: pd.Series, in_nifty_200: pd.Series, is_fno: pd.Series
) -> pd.Series:
    """Step 1 on a cross-section: NIFTY 200 on the date ∧ F&O ∧ both ratios defined."""
    return (
        in_nifty_200.fillna(False).astype(bool)
        & is_fno.fillna(False).astype(bool)
        & mr6.notna()
        & mr12.notna()
    )


def horizon_stats(values: pd.Series) -> HorizonStats | None:
    """Mean and population standard deviation (``ddof=0``) of an eligible cross-section."""
    clean = pd.to_numeric(values).astype("float64").dropna()
    if clean.empty:
        return None
    return HorizonStats(mean=float(clean.mean()), std=float(clean.std(ddof=0)))


def population_of(
    mr6: pd.Series, mr12: pd.Series, in_nifty_200: pd.Series, is_fno: pd.Series
) -> NsePopulation | None:
    """The Z-score statistics over whichever cross-section is passed in (step 3)."""
    eligible = eligibility(mr6, mr12, in_nifty_200, is_fno)
    six = horizon_stats(mr6[eligible])
    twelve = horizon_stats(mr12[eligible])
    if six is None or twelve is None:
        return None
    return NsePopulation(mr6=six, mr12=twelve)


def scores(
    mr6: pd.Series,
    mr12: pd.Series,
    in_nifty_200: pd.Series,
    is_fno: pd.Series,
    *,
    population: NsePopulation | None = None,
) -> pd.Series:
    """Steps 1, 3, 4, 5: the normalised score per row, NaN for every ineligible row.

    ``population`` defaults to the statistics of the eligible rows passed in. A horizon whose
    population standard deviation is zero (one eligible name, or identical ratios) has no defined
    Z, so every score is NaN rather than a division by zero dressed up as a number.
    """
    six = pd.to_numeric(mr6).astype("float64")
    twelve = pd.to_numeric(mr12).astype("float64")
    eligible = eligibility(six, twelve, in_nifty_200, is_fno)
    stats = (
        population if population is not None else population_of(six, twelve, in_nifty_200, is_fno)
    )
    out = pd.Series(np.nan, index=six.index, dtype="float64")
    if stats is None or not (stats.mr6.std > 0 and stats.mr12.std > 0):
        return out
    z6 = (six - stats.mr6.mean) / stats.mr6.std
    z12 = (twelve - stats.mr12.mean) / stats.mr12.std
    weighted = HORIZON_WEIGHTS[12] * z12 + HORIZON_WEIGHTS[6] * z6
    out[eligible] = weighted[eligible].map(normalise)
    return out
