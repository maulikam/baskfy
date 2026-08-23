"""Catalog card metrics for curated baskets - docs/smallcase/04 section section 2-4 (SC2).

Pure functions: prices/weights/returns in, Decimals out. No database, no network, no clock.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Final, Literal

from baskfy_core.gst import money

__all__ = [
    "TERCILE_SWITCH_PUBLISHED_COUNT",
    "VOL_BUCKET_HIGH_FLOOR",
    "VOL_BUCKET_MED_FLOOR",
    "VolatilityBucket",
    "absolute_return",
    "annualized_volatility",
    "cagr",
    "chain_link_nav",
    "headline_return",
    "min_amount",
    "shares_at_amount",
    "volatility_bucket",
    "volatility_bucket_fixed",
    "volatility_bucket_for_catalog",
]

VolatilityBucket = Literal["LOW", "MED", "HIGH"]

#: PACK.1 - fixed thresholds until enough published baskets exist for terciles.
VOL_BUCKET_MED_FLOOR: Final = Decimal("0.15")
VOL_BUCKET_HIGH_FLOOR: Final = Decimal("0.25")
TERCILE_SWITCH_PUBLISHED_COUNT: Final = 12

#: Annualisation factor for daily return std-dev (NSE ~252 trading days).
TRADING_DAYS_PER_YEAR: Final = Decimal("252")

#: Sample std-dev needs at least two observations.
MIN_VOL_OBSERVATIONS: Final = 2

#: Money amounts on catalog cards are whole rupees (ceil of the share-coverage bound).
RUPEE: Final = Decimal("1")

PCT_2DP: Final = Decimal("0.01")


def min_amount(
    prices: Sequence[Decimal],
    weights: Sequence[Decimal],
    *,
    lot_size: int = 1,
) -> Decimal:
    """Smallest lump sum that buys >=1 lot of every constituent at prescribed weights.

    docs/smallcase/04 section 2::

        min_amount = ceil( max_i( price_i / weight_i ) )   # lot_size = 1
        shares_i(amount) = floor( amount * weight_i / price_i )  # >= 1 at min_amount

    With ``lot_size > 1``, each constituent must receive at least one lot, so the bound is
    ``ceil(max_i(price_i * lot_size / weight_i))``.
    """
    if lot_size < 1:
        raise ValueError("lot_size must be >= 1")
    if len(prices) != len(weights):
        raise ValueError("prices and weights must have the same length")
    if not prices:
        raise ValueError("prices cannot be empty")

    lot = Decimal(lot_size)
    worst = Decimal("0")
    for price, weight in zip(prices, weights, strict=True):
        if price <= 0:
            raise ValueError(f"price must be positive, got {price}")
        if weight <= 0:
            raise ValueError(f"weight must be positive, got {weight}")
        bound = (price * lot) / weight
        worst = max(worst, bound)
    return worst.to_integral_value(rounding=ROUND_CEILING)


def shares_at_amount(
    amount: Decimal,
    prices: Sequence[Decimal],
    weights: Sequence[Decimal],
    *,
    lot_size: int = 1,
) -> list[int]:
    """Whole-share (lot-rounded) allocation at *amount* - docs/smallcase/04 section 2."""
    if lot_size < 1:
        raise ValueError("lot_size must be >= 1")
    if amount < 0:
        raise ValueError("amount cannot be negative")
    if len(prices) != len(weights):
        raise ValueError("prices and weights must have the same length")

    lot = Decimal(lot_size)
    out: list[int] = []
    for price, weight in zip(prices, weights, strict=True):
        if price <= 0 or weight <= 0:
            raise ValueError("price and weight must be positive")
        raw = (amount * weight) / price
        lots = (raw / lot).to_integral_value(rounding=ROUND_DOWN)
        out.append(int(lots * lot))
    return out


def volatility_bucket_fixed(annualized_vol: Decimal) -> VolatilityBucket:
    """PACK.1 fixed thresholds: LOW < 15% ≤ MED < 25% ≤ HIGH."""
    if annualized_vol < Decimal("0"):
        raise ValueError("annualized_vol cannot be negative")
    if annualized_vol < VOL_BUCKET_MED_FLOOR:
        return "LOW"
    if annualized_vol < VOL_BUCKET_HIGH_FLOOR:
        return "MED"
    return "HIGH"


def volatility_bucket(annualized_vol: Decimal) -> VolatilityBucket:
    """Alias for the fixed PACK.1 thresholds (default until terciles apply)."""
    return volatility_bucket_fixed(annualized_vol)


def volatility_bucket_for_catalog(
    annualized_vol: Decimal,
    *,
    published_count: int,
    peer_vols: Sequence[Decimal] | None = None,
) -> VolatilityBucket:
    """Bucket by PACK.1 fixed cutoffs, or terciles once ``published_count >= 12``.

    Terciles use the sorted peer annualized vols (including this basket). Ties at boundaries
    assign the higher bucket (same edge convention as the fixed thresholds).
    """
    if published_count < TERCILE_SWITCH_PUBLISHED_COUNT or peer_vols is None:
        return volatility_bucket_fixed(annualized_vol)
    if not peer_vols:
        return volatility_bucket_fixed(annualized_vol)
    ordered = sorted(peer_vols)
    n = len(ordered)
    # Lower tercile boundary indices (exclusive upper for LOW/MED).
    low_end = max(1, n // 3)
    med_end = max(low_end + 1, (2 * n) // 3)
    low_cut = ordered[low_end - 1]
    med_cut = ordered[med_end - 1]
    if annualized_vol <= low_cut:
        return "LOW"
    if annualized_vol <= med_cut:
        return "MED"
    return "HIGH"


def annualized_volatility(daily_returns: Sequence[Decimal]) -> Decimal | None:
    """Annualized sample std-dev of daily returns (fraction, not percent).

    Needs >=2 observations. Returns ``None`` when the series is too short.
    """
    if len(daily_returns) < MIN_VOL_OBSERVATIONS:
        return None
    n = Decimal(len(daily_returns))
    mean = sum(daily_returns, Decimal("0")) / n
    variance = sum((r - mean) ** 2 for r in daily_returns) / (n - Decimal("1"))
    if variance < 0:
        return None
    # Decimal.sqrt keeps the series in numeric space (house rule 9); only the irrational
    # root needs a transcendental — it stays on Decimal's own implementation.
    daily_std = variance.sqrt()
    annualized = daily_std * TRADING_DAYS_PER_YEAR.sqrt()
    return annualized.quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_UP)


def absolute_return(nav_start: Decimal, nav_end: Decimal) -> Decimal:
    """``(nav_end / nav_start) - 1``, rounded half-up to 2 dp percent storage."""
    if nav_start <= 0:
        raise ValueError("nav_start must be positive")
    raw = (nav_end / nav_start) - Decimal("1")
    return (raw * Decimal("100")).quantize(PCT_2DP, rounding=ROUND_HALF_UP)


def cagr(*, nav_start: Decimal, nav_end: Decimal, years: Decimal) -> Decimal | None:
    """Compound annual growth rate as a percent (2 dp). ``None`` if *years* ≤ 0."""
    if nav_start <= 0 or nav_end <= 0:
        raise ValueError("nav values must be positive")
    if years <= 0:
        return None
    # Fractional powers are transcendental: compute in float, re-enter Decimal immediately
    # and quantize to 2 dp — the storage contract — so money never lives as float.
    ratio = nav_end / nav_start
    rate = Decimal(str(math.exp(math.log(float(ratio)) / float(years)))) - Decimal("1")
    return (rate * Decimal("100")).quantize(PCT_2DP, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class NavPoint:
    """One day of a chain-linked basket index (base 100 at inception)."""

    date: object  # dt.date - kept untyped here to avoid a datetime import cycle in stubs
    nav: Decimal


def chain_link_nav(
    dates: Sequence[object],
    daily_returns: Sequence[Decimal],
    *,
    base: Decimal = Decimal("100"),
) -> list[tuple[object, Decimal]]:
    """Build a NAV series from daily basket returns, starting at *base*.

    ``dates[i]`` is the date of ``daily_returns[i]`` (return from prior close to this close).
    The series begins with ``(dates[0], base * (1 + daily_returns[0]))`` - callers who want
    an explicit base day should prepend a zero-return row.
    """
    if len(dates) != len(daily_returns):
        raise ValueError("dates and daily_returns must have the same length")
    nav = base
    out: list[tuple[object, Decimal]] = []
    for day, ret in zip(dates, daily_returns, strict=True):
        nav = money(nav * (Decimal("1") + ret))
        out.append((day, nav))
    return out


def basket_day_return(
    weights: Mapping[str, Decimal],
    prices_prev: Mapping[str, Decimal],
    prices_today: Mapping[str, Decimal],
) -> Decimal:
    """Point-in-time weighted return for one day from constituent closes.

    Missing prices contribute nothing (weight redistributed implicitly by only summing
    available names). Empty overlap yields 0.
    """
    total_w = Decimal("0")
    contrib = Decimal("0")
    for symbol, weight in weights.items():
        prev = prices_prev.get(symbol)
        today = prices_today.get(symbol)
        if prev is None or today is None or prev <= 0:
            continue
        total_w += weight
        contrib += weight * ((today / prev) - Decimal("1"))
    if total_w <= 0:
        return Decimal("0")
    return contrib / total_w


@dataclass(frozen=True, slots=True)
class HeadlineReturn:
    """Card headline: value plus the window label docs/smallcase/04 section 4 requires."""

    label: str
    value_pct: Decimal | None


def headline_return(  # noqa: PLR0913 - one arg per card metric window
    *,
    age_years: Decimal,
    ret_1m: Decimal | None,
    ret_1y: Decimal | None,
    cagr_3y: Decimal | None,
    cagr_5y: Decimal | None,
    months_available: int | None = None,
) -> HeadlineReturn:
    """Pick the card metric by basket age - docs/smallcase/04 section 4."""
    if age_years >= Decimal("5") and cagr_5y is not None:
        return HeadlineReturn(label="5Y CAGR", value_pct=cagr_5y)
    if age_years >= Decimal("3") and cagr_3y is not None:
        return HeadlineReturn(label="3Y CAGR", value_pct=cagr_3y)
    if age_years >= Decimal("1") and ret_1y is not None:
        return HeadlineReturn(label="1Y returns", value_pct=ret_1y)
    months = months_available if months_available is not None else max(1, int(age_years * 12))
    return HeadlineReturn(label=f"{months}M returns", value_pct=ret_1m)
