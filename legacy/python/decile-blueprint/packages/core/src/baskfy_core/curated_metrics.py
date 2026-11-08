"""Catalog card metrics for curated baskets - docs/smallcase/04 sections 2-4 (SC2).

Pure functions: prices/weights/returns in, Decimals out. No database, no network, no clock.

What this module is careful about
---------------------------------
**Version-aware chain-linking (docs/smallcase/04 section 4).** A basket's history is the
history of the weights it actually held, version by version. Replaying today's weight map
over five years of prices is a look-ahead bug (CLAUDE.md house rule 5) and it overstates a
momentum basket badly - measured at +15.59pp on the 5Y CAGR of a 300-basket simulation. So
:func:`version_aware_nav` walks the versions forward: the return earned on day *t* uses the
weights of the version with the greatest ``effective_date <= t``, and the daily returns are
chained across every version boundary. A basket has no history before its first version:
returns are ``None`` there rather than invented.

**Calendar windows, not bar counts (docs/05 section Notation, docs/13 section 3).** "1Y" is
``as_of - 12 months`` snapped forward to a trading day - not 252 bars, which lands 373-378
calendar days back once NSE holidays are counted. :mod:`baskfy_core.windows` already resolves
this and is reused here rather than re-approximated. A window of ``N`` trading days compounds
``N`` daily returns, so its base is the bar *immediately before* the window - the off-by-one
that ``navs[-N]`` against ``navs[-1]`` gets wrong by one day's return.

**CAGR years are measured, never assumed.** The 3Y slice of a real calendar spans ~3.01 years,
not exactly 3; the 5Y slice ~5.01. :attr:`WindowSlice.years` reports the elapsed span of the
slice that was actually compounded, and passing a hardcoded 3 or 5 instead biases every CAGR
upward.

**Rounding happens once, at write time (house rule 8).** NAV is carried at full Decimal
precision through the chain-link; quantising each day drifts the 1260-day NAV by -25 to +30
bps. :func:`nav_for_storage` is the single quantisation point, called when a NAV is stored or
served - never inside the compounding loop.

**A risk label is never published from a sample too small to carry one (section 3).**
:func:`catalog_volatility` returns ``None`` - no value, therefore no bucket - below 60 trading
days unless constituent volatilities are supplied for the spec's weighted fallback. Two days of
returns produced a published "LOW" before this.

Return convention - what these numbers do NOT include
-----------------------------------------------------
Every return, CAGR and volatility here is a **price return**. It is computed from
``ohlcv_daily.close``, which is split/bonus adjusted but **excludes cash dividends** - the
convention chosen in ``docs/DECISIONS-MERGE.md`` M39.3 (price convention won 42 of 45 deciding
windows). Total return would be higher by roughly the basket's dividend yield each year -
:data:`ESTIMATED_DIVIDEND_DRAG_PCT_PER_YEAR` for the broad Indian market - and the gap
compounds. :func:`return_convention_fields` is the machine-readable form of this paragraph so
the API payload and the UI can say it out loud instead of leaving the reader to assume total
return.
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Final, Literal

from baskfy_core.gst import money
from baskfy_core.windows import resolve_window

__all__ = [
    "CARD_WINDOW_MONTHS",
    "DIVIDENDS_INCLUDED",
    "ESTIMATED_DIVIDEND_DRAG_PCT_PER_YEAR",
    "MIN_BASKET_VOL_DAYS",
    "NO_HEADLINE_LABEL",
    "RETURN_CONVENTION",
    "RETURN_CONVENTION_NOTE",
    "TERCILE_SWITCH_PUBLISHED_COUNT",
    "VOL_BUCKET_HIGH_FLOOR",
    "VOL_BUCKET_MED_FLOOR",
    "VOL_WINDOW_BARS",
    "BasketNav",
    "BasketVersion",
    "CatalogVolatility",
    "HeadlineReturn",
    "NavPoint",
    "VolatilityBasis",
    "VolatilityBucket",
    "WindowSlice",
    "absolute_return",
    "annualized_volatility",
    "basket_day_return",
    "cagr",
    "catalog_volatility",
    "chain_link_nav",
    "constituent_volatilities",
    "constituent_weighted_volatility",
    "daily_returns_for_symbol",
    "headline_return",
    "min_amount",
    "nav_for_storage",
    "resolve_nav_window",
    "return_convention_fields",
    "shares_at_amount",
    "since_inception_anchor",
    "since_inception_return",
    "version_aware_nav",
    "volatility_bucket",
    "volatility_bucket_fixed",
    "volatility_bucket_for_catalog",
    "weights_on",
    "whole_months_between",
    "window_cagr",
    "window_return",
]

VolatilityBucket = Literal["LOW", "MED", "HIGH"]

#: Which series a published ``volatility_value`` was measured on - docs/smallcase/04 section 3.
VolatilityBasis = Literal["BASKET_252D", "BASKET_FULL_HISTORY", "CONSTITUENT_WEIGHTED"]

#: PACK.1 - fixed thresholds until enough published baskets exist for terciles.
VOL_BUCKET_MED_FLOOR: Final = Decimal("0.15")
VOL_BUCKET_HIGH_FLOOR: Final = Decimal("0.25")
TERCILE_SWITCH_PUBLISHED_COUNT: Final = 12

#: Annualisation factor for daily return std-dev (NSE ~252 trading days).
TRADING_DAYS_PER_YEAR: Final = Decimal("252")

#: Sample std-dev needs at least two observations. This is the arithmetic floor, NOT the floor
#: at which a risk label may be published - that is :data:`MIN_BASKET_VOL_DAYS`.
MIN_VOL_OBSERVATIONS: Final = 2

#: docs/smallcase/04 section 3: the volatility window is stated as a bar count ("trailing 252
#: trading days"), unlike the return windows which are calendar offsets. Kept as the spec
#: states it.
VOL_WINDOW_BARS: Final = 252

#: docs/smallcase/04 section 3: below 60 trading days the bucket comes from constituent
#: volatilities weighted by target weight, never from the basket's own stub of a series.
MIN_BASKET_VOL_DAYS: Final = 60

#: Money amounts on catalog cards are whole rupees (ceil of the share-coverage bound).
RUPEE: Final = Decimal("1")

PCT_2DP: Final = Decimal("0.01")

#: Calendar year length used to turn an elapsed span into CAGR years (leap years included).
DAYS_PER_YEAR: Final = Decimal("365.25")

#: The card's return windows as calendar-month offsets - docs/smallcase/04 section 4.
CARD_WINDOW_MONTHS: Final[Mapping[str, int]] = {
    "1m": 1,
    "6m": 6,
    "1y": 12,
    "3y": 36,
    "5y": 60,
}

HEADLINE_5Y_MONTHS: Final = 60
HEADLINE_3Y_MONTHS: Final = 36
HEADLINE_1Y_MONTHS: Final = 12
HEADLINE_6M_MONTHS: Final = 6
HEADLINE_1M_MONTHS: Final = 1

#: What the card shows when no window is both spanned and computable. A label with no window
#: behind it is the bug this constant exists to avoid.
NO_HEADLINE_LABEL: Final = "Not enough history"

#: A6 - the return convention, machine-readable. ``ohlcv_daily.close`` is split/bonus adjusted
#: and dividend-free (docs/DECISIONS-MERGE.md M39.3), so every number here is a price return.
RETURN_CONVENTION: Final = "PRICE_RETURN"
DIVIDENDS_INCLUDED: Final = False
#: Broad-market Indian dividend yield, the order of magnitude by which a total-return series
#: would exceed these numbers each year. An estimate for disclosure, never used in arithmetic.
ESTIMATED_DIVIDEND_DRAG_PCT_PER_YEAR: Final = Decimal("1.2")
RETURN_CONVENTION_NOTE: Final = (
    "Returns are price returns computed from split- and bonus-adjusted closes. Cash dividends "
    "are excluded (docs/DECISIONS-MERGE.md M39.3), so a total-return series would be higher by "
    "roughly the basket's dividend yield each year - about "
    f"{ESTIMATED_DIVIDEND_DRAG_PCT_PER_YEAR}% for the broad Indian market - compounding."
)


def return_convention_fields() -> dict[str, str | bool]:
    """The disclosure as payload fields, so the API and the UI can surface it verbatim (A6).

    ``dividends_included`` is a real boolean, not the string ``"false"``. It reads as a flag,
    and every non-empty string is truthy in JavaScript — so a UI writing the natural
    ``if (metrics.dividends_included)`` against ``"false"`` would tell the reader dividends
    *are* included. On a disclosure field, the failure mode is the disclosure saying the
    opposite of the truth.
    """
    return {
        "return_convention": RETURN_CONVENTION,
        "dividends_included": DIVIDENDS_INCLUDED,
        "return_convention_note": RETURN_CONVENTION_NOTE,
    }


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
    """PACK.1 fixed thresholds: LOW < 15% <= MED < 25% <= HIGH."""
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

    Terciles use the sorted peer annualized vols (including this basket). A vol sitting exactly
    on a boundary takes the **higher** bucket, which is the same edge convention as the fixed
    thresholds (``LOW < 15% <= MED``) - hence the strict ``<`` against each cut. Before
    SC-hardening the code used ``<=`` while the docstring promised the higher bucket, and a
    single peer walked off the end of the sorted list with an ``IndexError``.
    """
    if published_count < TERCILE_SWITCH_PUBLISHED_COUNT or not peer_vols:
        return volatility_bucket_fixed(annualized_vol)
    if annualized_vol < Decimal("0"):
        raise ValueError("annualized_vol cannot be negative")
    ordered = sorted(peer_vols)
    n = len(ordered)
    # Tercile boundaries as ranks, clamped into the list: with one or two peers the terciles
    # collapse onto the same element rather than indexing past the end.
    low_end = min(n, max(1, n // 3))
    med_end = min(n, max(low_end + 1, (2 * n) // 3))
    low_cut = ordered[low_end - 1]
    med_cut = ordered[med_end - 1]
    if annualized_vol < low_cut:
        return "LOW"
    if annualized_vol < med_cut:
        return "MED"
    return "HIGH"


def annualized_volatility(daily_returns: Sequence[Decimal]) -> Decimal | None:
    """Annualized sample std-dev of daily returns (fraction, not percent).

    The arithmetic needs >=2 observations and returns ``None`` below that. It says nothing about
    whether the sample is long enough to *publish*: two days of returns produce a number here
    and must never reach a card. :func:`catalog_volatility` is the function that decides.
    """
    if len(daily_returns) < MIN_VOL_OBSERVATIONS:
        return None
    n = Decimal(len(daily_returns))
    mean = sum(daily_returns, Decimal("0")) / n
    variance = sum((r - mean) ** 2 for r in daily_returns) / (n - Decimal("1"))
    if variance < 0:
        return None
    # Decimal.sqrt keeps the series in numeric space (house rule 9); only the irrational
    # root needs a transcendental - it stays on Decimal's own implementation.
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
    """Compound annual growth rate as a percent (2 dp). ``None`` if *years* <= 0.

    *years* is the **measured** span of the slice being compounded (see
    :attr:`WindowSlice.years`). Passing a nominal 3 or 5 for a slice that spans 3.08 or 5.12
    years inflates the rate.
    """
    if nav_start <= 0 or nav_end <= 0:
        raise ValueError("nav values must be positive")
    if years <= 0:
        return None
    # Fractional powers are transcendental: compute in float, re-enter Decimal immediately
    # and quantize to 2 dp - the storage contract - so money never lives as float.
    ratio = nav_end / nav_start
    rate = Decimal(str(math.exp(math.log(float(ratio)) / float(years)))) - Decimal("1")
    return (rate * Decimal("100")).quantize(PCT_2DP, rounding=ROUND_HALF_UP)


def nav_for_storage(nav: Decimal) -> Decimal:
    """Quantise a NAV **once**, where it is written or served (house rule 8).

    Never call this inside a compounding loop: over 1260 days, per-day quantisation drifts the
    index by tens of basis points and moves the 5Y CAGR.
    """
    return money(nav)


@dataclass(frozen=True, slots=True)
class NavPoint:
    """One day of a chain-linked basket index (base 100 at inception)."""

    date: dt.date
    nav: Decimal


@dataclass(frozen=True, slots=True)
class BasketVersion:
    """One published cut of a basket: the weights it held from *effective_date* onward."""

    effective_date: dt.date
    weights: Mapping[str, Decimal]


@dataclass(frozen=True, slots=True)
class BasketNav:
    """A chain-linked, version-aware basket index.

    ``points`` is bar-aligned and starts at the basket's first day with ``base``: ``points[i]``
    is the index level at the close of ``points[i].date``. ``daily_returns[i]`` is the return
    that carried ``points[i]`` to ``points[i + 1]``.
    """

    points: tuple[tuple[dt.date, Decimal], ...]
    daily_returns: tuple[tuple[dt.date, Decimal], ...]

    @property
    def dates(self) -> tuple[dt.date, ...]:
        return tuple(day for day, _ in self.points)

    @property
    def returns_only(self) -> tuple[Decimal, ...]:
        return tuple(value for _, value in self.daily_returns)


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


def weights_on(versions: Sequence[BasketVersion], day: dt.date) -> Mapping[str, Decimal] | None:
    """The weights in force on *day*: the version with the greatest ``effective_date <= day``.

    ``None`` before the first version - a basket that did not exist has no weights, and giving
    it today's is the look-ahead house rule 5 forbids.
    """
    ordered = sorted(versions, key=lambda version: version.effective_date)
    index = bisect.bisect_right([version.effective_date for version in ordered], day) - 1
    if index < 0:
        return None
    return ordered[index].weights


def chain_link_nav(
    dates: Sequence[dt.date],
    daily_returns: Sequence[Decimal],
    *,
    base: Decimal = Decimal("100"),
) -> list[tuple[dt.date, Decimal]]:
    """Build a NAV series from daily basket returns, starting at *base*.

    ``dates[i]`` is the date of ``daily_returns[i]`` (return from prior close to this close).
    The series begins with ``(dates[0], base * (1 + daily_returns[0]))`` - callers who want
    an explicit base day should prepend a zero-return row, which is what
    :func:`version_aware_nav` does.

    NAV is carried **unrounded**: quantising each day is the house-rule-8 violation A4
    measured. Quantise once, at the write, with :func:`nav_for_storage`.
    """
    if len(dates) != len(daily_returns):
        raise ValueError("dates and daily_returns must have the same length")
    nav = base
    out: list[tuple[dt.date, Decimal]] = []
    for day, ret in zip(dates, daily_returns, strict=True):
        nav = nav * (Decimal("1") + ret)
        out.append((day, nav))
    return out


def version_aware_nav(
    prices_by_date: Mapping[dt.date, Mapping[str, Decimal]],
    versions: Sequence[BasketVersion],
    *,
    base: Decimal = Decimal("100"),
) -> BasketNav:
    """Chain-link a basket across every version it has ever had (docs/smallcase/04 section 4).

    The return earned on day *t* uses the weights of the version with the greatest
    ``effective_date <= t`` - the composition the basket actually held into that close - and
    the daily returns are chained straight through each version boundary. The series starts on
    the first priced trading day on or after the earliest version's ``effective_date``: before
    that the basket has no history, and inventing one from today's winners is exactly the
    look-ahead that overstated the 5Y CAGR by 15.59pp.
    """
    if not versions or not prices_by_date:
        return BasketNav(points=(), daily_returns=())
    ordered = sorted(versions, key=lambda version: version.effective_date)
    effective_dates = [version.effective_date for version in ordered]
    inception = effective_dates[0]
    dates = [day for day in sorted(prices_by_date) if day >= inception]
    if not dates:
        return BasketNav(points=(), daily_returns=())

    returns: list[tuple[dt.date, Decimal]] = []
    for prev_day, day in pairwise(dates):
        index = bisect.bisect_right(effective_dates, day) - 1
        weights = ordered[max(index, 0)].weights
        returns.append(
            (day, basket_day_return(weights, prices_by_date[prev_day], prices_by_date[day]))
        )
    chained = chain_link_nav(
        [day for day, _ in returns], [value for _, value in returns], base=base
    )
    return BasketNav(
        points=((dates[0], base), *chained),
        daily_returns=tuple(returns),
    )


@dataclass(frozen=True, slots=True)
class WindowSlice:
    """The NAV pair a window return is computed from, and the span it actually covers."""

    #: The bar immediately *before* the window - the base of the return (docs/05 section 1).
    start_date: dt.date
    end_date: dt.date
    nav_start: Decimal
    nav_end: Decimal
    #: ``N`` - the number of trading days inside the window, hence of returns compounded.
    trading_days: int

    @property
    def years(self) -> Decimal:
        """The measured elapsed span, for CAGR. Never a nominal 3 or 5."""
        return Decimal((self.end_date - self.start_date).days) / DAYS_PER_YEAR


def resolve_nav_window(
    points: Sequence[tuple[dt.date, Decimal]], *, months: int
) -> WindowSlice | None:
    """Resolve a calendar-offset window over a NAV series - ``None`` if it is not spanned.

    ``months`` is a calendar offset (1 / 6 / 12 / 36 / 60), resolved by
    :func:`baskfy_core.windows.resolve_window` against the series' own trading days. The base
    is the bar immediately before the window, so exactly ``N`` daily returns are compounded:
    ``points[-N]`` against ``points[-1]`` compounds ``N - 1`` and understates the window.
    """
    if len(points) < MIN_VOL_OBSERVATIONS:
        return None
    dates = [day for day, _ in points]
    window = resolve_window(dates[-1], months, dates)
    if not window.spans_full_window:
        return None
    start_index = bisect.bisect_left(dates, window.start)
    base_index = start_index - 1
    if base_index < 0:
        return None
    return WindowSlice(
        start_date=dates[base_index],
        end_date=dates[-1],
        nav_start=points[base_index][1],
        nav_end=points[-1][1],
        trading_days=window.length,
    )


def window_return(points: Sequence[tuple[dt.date, Decimal]], *, months: int) -> Decimal | None:
    """Absolute % return over a calendar-offset window. ``None`` when it is not spanned."""
    window = resolve_nav_window(points, months=months)
    if window is None:
        return None
    return absolute_return(window.nav_start, window.nav_end)


def window_cagr(points: Sequence[tuple[dt.date, Decimal]], *, months: int) -> Decimal | None:
    """CAGR over a calendar-offset window, annualised by the slice's *measured* span."""
    window = resolve_nav_window(points, months=months)
    if window is None:
        return None
    return cagr(nav_start=window.nav_start, nav_end=window.nav_end, years=window.years)


def since_inception_anchor(
    points: Sequence[tuple[dt.date, Decimal]], *, launched_at: dt.date | None
) -> dt.date | None:
    """The date the since-inception number is actually measured from.

    ``launched_at`` snapped forward into the series - so a basket launched before its price
    history begins is measured from where the history begins, and the caller can label the
    number with the span it really covers rather than with the basket's nominal age.
    """
    if len(points) < MIN_VOL_OBSERVATIONS:
        return None
    dates = [day for day, _ in points]
    index = 0 if launched_at is None else bisect.bisect_left(dates, launched_at)
    if index >= len(points) - 1:
        return None
    return dates[index]


def since_inception_return(
    points: Sequence[tuple[dt.date, Decimal]], *, launched_at: dt.date | None
) -> Decimal | None:
    """Absolute % return from ``launched_at`` - docs/smallcase/04 section 4.

    Anchored to the basket's launch, not to the first day of whatever fetch window a caller
    happened to use: anchoring on ``points[0]`` silently retitles an 11-year record as a
    5.97-year one. ``None`` when the series does not reach the launch date or has no history
    after it. With no ``launched_at`` the anchor is the first day of the series, which is the
    basket's first version - the earliest date a return can honestly be claimed from.
    """
    anchor = since_inception_anchor(points, launched_at=launched_at)
    if anchor is None:
        return None
    index = bisect.bisect_left([day for day, _ in points], anchor)
    return absolute_return(points[index][1], points[-1][1])


def daily_returns_for_symbol(
    prices_by_date: Mapping[dt.date, Mapping[str, Decimal]], symbol: str
) -> list[Decimal]:
    """One constituent's daily returns from the same price map the basket is chained on."""
    series = [
        (day, prices_by_date[day][symbol])
        for day in sorted(prices_by_date)
        if symbol in prices_by_date[day]
    ]
    out: list[Decimal] = []
    for (_, prev), (_, today) in pairwise(series):
        if prev <= 0:
            continue
        out.append((today / prev) - Decimal("1"))
    return out


def constituent_volatilities(
    prices_by_date: Mapping[dt.date, Mapping[str, Decimal]],
    symbols: Sequence[str],
    *,
    window_bars: int = VOL_WINDOW_BARS,
    min_observations: int = MIN_BASKET_VOL_DAYS,
) -> dict[str, Decimal]:
    """Annualized vol per constituent, for the section 3 fallback.

    A constituent with fewer than *min_observations* returns is left out rather than measured
    on a stub - the same floor the basket's own series is held to.
    """
    out: dict[str, Decimal] = {}
    for symbol in symbols:
        returns = daily_returns_for_symbol(prices_by_date, symbol)
        if len(returns) < min_observations:
            continue
        value = annualized_volatility(returns[-window_bars:])
        if value is not None:
            out[symbol] = value
    return out


def constituent_weighted_volatility(
    weights: Mapping[str, Decimal], constituent_vols: Mapping[str, Decimal]
) -> Decimal | None:
    """Target-weighted average of constituent vols - docs/smallcase/04 section 3's fallback.

    "the spec's 'std. deviation of constituents' reading": weight each name's annualized vol by
    its target weight, renormalising over the names a vol is available for. ``None`` when none
    is - a bucket must not be invented for a basket nothing is known about.
    """
    total_w = Decimal("0")
    contrib = Decimal("0")
    for symbol, weight in weights.items():
        value = constituent_vols.get(symbol)
        if value is None or weight <= 0:
            continue
        total_w += weight
        contrib += weight * value
    if total_w <= 0:
        return None
    return (contrib / total_w).quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class CatalogVolatility:
    """A publishable volatility: the value and the series it was measured on, together.

    The bucket is reachable only through this object, so a risk label cannot exist without a
    value that cleared docs/smallcase/04 section 3's sample floor.
    """

    value: Decimal
    basis: VolatilityBasis

    def bucket(
        self, *, published_count: int, peer_vols: Sequence[Decimal] | None = None
    ) -> VolatilityBucket:
        return volatility_bucket_for_catalog(
            self.value, published_count=published_count, peer_vols=peer_vols
        )


def catalog_volatility(
    *,
    basket_daily_returns: Sequence[Decimal],
    weights: Mapping[str, Decimal] | None = None,
    constituent_vols: Mapping[str, Decimal] | None = None,
) -> CatalogVolatility | None:
    """The publishable volatility for a catalog card - docs/smallcase/04 section 3.

    * >= 252 returns: trailing 252 trading days of the basket's own series.
    * >= 60 returns: the basket's full available history.
    * below 60: the constituent-weighted fallback, if constituent vols were supplied.
    * otherwise ``None`` - **no value and therefore no risk label**. Two consecutive +2% days
      used to publish "LOW"; they now publish nothing.
    """
    count = len(basket_daily_returns)
    if count >= VOL_WINDOW_BARS:
        value = annualized_volatility(basket_daily_returns[-VOL_WINDOW_BARS:])
        return None if value is None else CatalogVolatility(value=value, basis="BASKET_252D")
    if count >= MIN_BASKET_VOL_DAYS:
        value = annualized_volatility(basket_daily_returns)
        return (
            None if value is None else CatalogVolatility(value=value, basis="BASKET_FULL_HISTORY")
        )
    if weights is None or constituent_vols is None:
        return None
    fallback = constituent_weighted_volatility(weights, constituent_vols)
    if fallback is None:
        return None
    return CatalogVolatility(value=fallback, basis="CONSTITUENT_WEIGHTED")


@dataclass(frozen=True, slots=True)
class HeadlineReturn:
    """Card headline: value plus the window label docs/smallcase/04 section 4 requires."""

    label: str
    value_pct: Decimal | None


def whole_months_between(start: dt.date, end: dt.date) -> int:
    """Completed calendar months from *start* to *end* (0 if *end* precedes *start*)."""
    if end < start:
        return 0
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return max(months, 0)


def headline_return(  # noqa: PLR0913 - one kwarg per card window, and that is the point
    *,
    months_available: int,
    ret_1m: Decimal | None,
    ret_6m: Decimal | None,
    ret_1y: Decimal | None,
    cagr_3y: Decimal | None,
    cagr_5y: Decimal | None,
    since_inception_pct: Decimal | None,
) -> HeadlineReturn:
    """Pick the card metric by basket age - docs/smallcase/04 section 4.

    Every argument is required and keyword-only on purpose: the label and the value it names
    are chosen from the *same* row of the table below, so the function cannot label a window it
    was not given the number for. The bug this replaces rendered "6M returns" over the 21-day
    figure for a 210-day-old basket, because the caller passed neither ``ret_6m`` nor
    ``months_available`` and the defaults let it through.

    Below a year the label names the basket's whole life ("{n}M returns"), so the value must be
    the since-inception number - the only one measured over exactly that span. When nothing is
    both spanned and computable the card says :data:`NO_HEADLINE_LABEL` and shows no number.
    """
    if months_available < 0:
        raise ValueError("months_available cannot be negative")
    if (
        HEADLINE_1M_MONTHS <= months_available < HEADLINE_1Y_MONTHS
        and since_inception_pct is not None
    ):
        return HeadlineReturn(label=f"{months_available}M returns", value_pct=since_inception_pct)
    candidates: tuple[tuple[int, str, Decimal | None], ...] = (
        (HEADLINE_5Y_MONTHS, "5Y CAGR", cagr_5y),
        (HEADLINE_3Y_MONTHS, "3Y CAGR", cagr_3y),
        (HEADLINE_1Y_MONTHS, "1Y returns", ret_1y),
        (HEADLINE_6M_MONTHS, "6M returns", ret_6m),
        (HEADLINE_1M_MONTHS, "1M returns", ret_1m),
    )
    for required, label, value in candidates:
        if months_available >= required and value is not None:
            return HeadlineReturn(label=label, value_pct=value)
    return HeadlineReturn(label=NO_HEADLINE_LABEL, value_pct=None)
