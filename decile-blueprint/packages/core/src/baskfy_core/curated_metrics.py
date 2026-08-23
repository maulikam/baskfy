"""Exact, pure computations for curated-basket catalog metrics.

Raw/LTP prices are used only for affordability.  Performance uses adjusted closes and applies
the version effective on each *return date*, so a later version can never leak backwards.  The
module reads no clock, database, filesystem, or network; callers supply every dated observation.

All public numeric inputs and results are :class:`~decimal.Decimal`.  Calculations retain Decimal
precision and the two explicit ``round_*`` helpers are the only storage-boundary quantizers.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from enum import StrEnum

MONEY_QUANTUM = Decimal("0.01")
RETURN_QUANTUM = Decimal("0.0000000001")
EXACT_TOTAL_WEIGHT = Decimal("1.0000")
BASE_NAV = Decimal("100")
TRADING_DAYS_PER_YEAR = Decimal(252)
YOUNG_BASKET_RETURN_DAYS = 60
MAX_VOLATILITY_RETURN_DAYS = 252
TERCILE_MINIMUM_PUBLISHED = 12
MAX_WINDOW_START_GAP_DAYS = 7
MIN_VOLATILITY_RETURN_COUNT = 2


class CuratedMetricsError(ValueError):
    """An input cannot produce a truthful curated-basket metric."""


class VolatilityBucket(StrEnum):
    """The three volatility labels stored by ``cb_metrics``."""

    LOW = "LOW"
    MED = "MED"
    HIGH = "HIGH"


@dataclass(frozen=True, slots=True)
class WeightedPrice:
    """A target weight and current raw/LTP price used for affordability."""

    instrument_id: int
    weight: Decimal
    raw_price: Decimal


@dataclass(frozen=True, slots=True)
class ShareAllocation:
    """The documented whole-share floor for one constituent."""

    instrument_id: int
    target_weight: Decimal
    raw_price: Decimal
    shares: int
    invested_amount: Decimal


@dataclass(frozen=True, slots=True)
class TargetWeight:
    """One point-in-time version constituent."""

    instrument_id: int
    weight: Decimal


@dataclass(frozen=True, slots=True)
class VersionWeights:
    """Target weights effective from ``effective_date`` onwards."""

    effective_date: dt.date
    constituents: tuple[TargetWeight, ...]


@dataclass(frozen=True, slots=True)
class AdjustedClose:
    """One split/dividend-adjusted close used only for performance."""

    instrument_id: int
    value: Decimal


@dataclass(frozen=True, slots=True)
class DailyAdjustedCloses:
    """All adjusted closes needed on one trading day."""

    day: dt.date
    closes: tuple[AdjustedClose, ...]


@dataclass(frozen=True, slots=True)
class NavPoint:
    """One value in a base-100 chain-linked basket series."""

    day: dt.date
    value: Decimal


@dataclass(frozen=True, slots=True)
class WindowMetrics:
    """The exact percentage fields persisted in ``cb_metrics``."""

    ret_1m: Decimal | None
    ret_6m: Decimal | None
    ret_1y: Decimal | None
    cagr_3y: Decimal | None
    cagr_5y: Decimal | None
    since_inception_pct: Decimal


@dataclass(frozen=True, slots=True)
class HeadlineMetric:
    """The age-sensitive card label and percentage value from docs/smallcase/04."""

    label: str
    value_pct: Decimal


@dataclass(frozen=True, slots=True)
class ConstituentVolatility:
    """A target weight and annualized constituent volatility, both decimal fractions."""

    instrument_id: int
    weight: Decimal
    annualized_volatility: Decimal


@dataclass(frozen=True, slots=True)
class PublishedVolatility:
    """One published basket's stable identity and annualized volatility fraction."""

    basket_id: int
    value: Decimal


def _finite_decimal(value: Decimal, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise CuratedMetricsError(f"{field} must be a finite Decimal")
    return value


def _positive_decimal(value: Decimal, field: str) -> Decimal:
    result = _finite_decimal(value, field)
    if result <= 0:
        raise CuratedMetricsError(f"{field} must be greater than zero")
    return result


def _valid_date(value: dt.date, field: str) -> dt.date:
    if type(value) is not dt.date:
        raise CuratedMetricsError(f"{field} must be a date, not a datetime")
    return value


def _ceil_decimal_ratio(numerator: Decimal, denominator: Decimal) -> int:
    """Ceil a ratio of finite Decimals through their exact integer representations."""
    numerator_top, numerator_bottom = numerator.as_integer_ratio()
    denominator_top, denominator_bottom = denominator.as_integer_ratio()
    ratio_top = numerator_top * denominator_bottom
    ratio_bottom = numerator_bottom * denominator_top
    return (ratio_top + ratio_bottom - 1) // ratio_bottom


def _floor_weighted_amount(amount: Decimal, weight: Decimal, price: Decimal) -> int:
    """Floor ``amount * weight / price`` without a finite Decimal context rounding it up."""
    amount_top, amount_bottom = amount.as_integer_ratio()
    weight_top, weight_bottom = weight.as_integer_ratio()
    price_top, price_bottom = price.as_integer_ratio()
    numerator = amount_top * weight_top * price_bottom
    denominator = amount_bottom * weight_bottom * price_top
    return numerator // denominator


def round_money(value: Decimal) -> Decimal:
    """Round a finite money value half-up to the ``numeric(20, 2)`` storage scale."""
    return _finite_decimal(value, "money value").quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def round_return(value: Decimal) -> Decimal:
    """Round a finite return/volatility value half-up to ``numeric(18, 10)``."""
    return _finite_decimal(value, "return value").quantize(RETURN_QUANTUM, rounding=ROUND_HALF_UP)


def _validated_weighted_prices(constituents: Sequence[WeightedPrice]) -> tuple[WeightedPrice, ...]:
    rows = tuple(constituents)
    if not rows:
        raise CuratedMetricsError("at least one constituent price is required")
    seen: set[int] = set()
    total_weight = Decimal(0)
    for row in rows:
        if not isinstance(row, WeightedPrice):
            raise CuratedMetricsError("every constituent price must be a WeightedPrice")
        if type(row.instrument_id) is not int or row.instrument_id <= 0:
            raise CuratedMetricsError("instrument_id must be a positive integer")
        if row.instrument_id in seen:
            raise CuratedMetricsError(f"duplicate instrument_id: {row.instrument_id}")
        seen.add(row.instrument_id)
        weight = _positive_decimal(row.weight, "weight")
        if weight > 1:
            raise CuratedMetricsError("weight must be at most one")
        _positive_decimal(row.raw_price, "raw_price")
        total_weight += weight
    if total_weight != EXACT_TOTAL_WEIGHT:
        raise CuratedMetricsError(
            f"constituent weights must total exactly {EXACT_TOTAL_WEIGHT}; got {total_weight}"
        )
    return rows


def minimum_investment_amount(constituents: Sequence[WeightedPrice]) -> Decimal:
    """Return ``ceil(max(raw_price / weight))`` exactly as an integral rupee Decimal."""
    rows = _validated_weighted_prices(constituents)
    required_rupees = max(_ceil_decimal_ratio(row.raw_price, row.weight) for row in rows)
    return Decimal(required_rupees)


def whole_share_allocation(
    amount: Decimal, constituents: Sequence[WeightedPrice]
) -> tuple[ShareAllocation, ...]:
    """Apply the documented independent floors; residual cash is not redistributed."""
    available = _finite_decimal(amount, "amount")
    if available < 0:
        raise CuratedMetricsError("amount must be non-negative")
    rows = _validated_weighted_prices(constituents)
    result: list[ShareAllocation] = []
    for row in rows:
        shares = _floor_weighted_amount(available, row.weight, row.raw_price)
        result.append(
            ShareAllocation(
                instrument_id=row.instrument_id,
                target_weight=row.weight,
                raw_price=row.raw_price,
                shares=shares,
                invested_amount=row.raw_price * shares,
            )
        )
    return tuple(result)


def _validated_versions(versions: Sequence[VersionWeights]) -> tuple[VersionWeights, ...]:
    rows = tuple(versions)
    if not rows:
        raise CuratedMetricsError("at least one basket version is required")
    previous_date: dt.date | None = None
    for version in rows:
        if not isinstance(version, VersionWeights):
            raise CuratedMetricsError("every version must be VersionWeights")
        effective_date = _valid_date(version.effective_date, "effective_date")
        if previous_date is not None and effective_date <= previous_date:
            raise CuratedMetricsError("version effective dates must be strictly increasing")
        previous_date = effective_date
        if not version.constituents:
            raise CuratedMetricsError("every version must contain at least one constituent")
        seen: set[int] = set()
        total_weight = Decimal(0)
        for row in version.constituents:
            if not isinstance(row, TargetWeight):
                raise CuratedMetricsError("every version constituent must be TargetWeight")
            if type(row.instrument_id) is not int or row.instrument_id <= 0:
                raise CuratedMetricsError("instrument_id must be a positive integer")
            if row.instrument_id in seen:
                raise CuratedMetricsError(f"duplicate instrument_id: {row.instrument_id}")
            seen.add(row.instrument_id)
            weight = _positive_decimal(row.weight, "weight")
            if weight > 1:
                raise CuratedMetricsError("weight must be at most one")
            total_weight += weight
        if total_weight != EXACT_TOTAL_WEIGHT:
            raise CuratedMetricsError(
                f"version weights must total exactly {EXACT_TOTAL_WEIGHT}; got {total_weight}"
            )
    return rows


def _validated_price_days(
    price_days: Sequence[DailyAdjustedCloses],
) -> Iterator[tuple[dt.date, dict[int, Decimal]]]:
    """Validate and convert one price day at a time, retaining no historical close maps."""
    if not price_days:
        raise CuratedMetricsError("at least one adjusted-close day is required")
    previous_date: dt.date | None = None
    for price_day in price_days:
        if not isinstance(price_day, DailyAdjustedCloses):
            raise CuratedMetricsError("every price day must be DailyAdjustedCloses")
        day = _valid_date(price_day.day, "price day")
        if previous_date is not None and day <= previous_date:
            raise CuratedMetricsError("price days must be strictly increasing")
        previous_date = day
        if not price_day.closes:
            raise CuratedMetricsError("every price day must contain at least one adjusted close")
        closes: dict[int, Decimal] = {}
        for close in price_day.closes:
            if not isinstance(close, AdjustedClose):
                raise CuratedMetricsError("every close must be AdjustedClose")
            if type(close.instrument_id) is not int or close.instrument_id <= 0:
                raise CuratedMetricsError("instrument_id must be a positive integer")
            if close.instrument_id in closes:
                raise CuratedMetricsError(f"duplicate instrument_id: {close.instrument_id}")
            closes[close.instrument_id] = _positive_decimal(close.value, "adjusted close")
        yield day, closes


def chain_linked_nav(
    versions: Sequence[VersionWeights],
    price_days: Sequence[DailyAdjustedCloses],
) -> tuple[NavPoint, ...]:
    """Build a base-100 series using the version effective on every return date.

    Price days before the first version are ignored.  Each active constituent must have an
    adjusted close on both ends of its daily return; incomplete coverage is rejected rather than
    silently renormalized, allowing the caller to render the required history caveat.
    """
    version_rows = _validated_versions(versions)
    version_index = 0
    nav = BASE_NAV
    previous_closes: dict[int, Decimal] | None = None
    result: list[NavPoint] = []
    for day, current_closes in _validated_price_days(price_days):
        if day < version_rows[0].effective_date:
            continue
        if previous_closes is None:
            previous_closes = current_closes
            result.append(NavPoint(day, nav))
            continue
        while (
            version_index + 1 < len(version_rows)
            and version_rows[version_index + 1].effective_date <= day
        ):
            version_index += 1
        daily_return = Decimal(0)
        for constituent in version_rows[version_index].constituents:
            instrument_id = constituent.instrument_id
            try:
                previous_close = previous_closes[instrument_id]
                current_close = current_closes[instrument_id]
            except KeyError as exc:
                raise CuratedMetricsError(
                    f"missing adjusted close for instrument_id {instrument_id} on return date {day}"
                ) from exc
            daily_return += constituent.weight * (current_close / previous_close - 1)
        nav *= 1 + daily_return
        if nav < 0:
            raise CuratedMetricsError("chain-linked NAV cannot be negative")
        result.append(NavPoint(day, nav))
        previous_closes = current_closes
    if not result:
        raise CuratedMetricsError("no adjusted-close day is on or after basket inception")
    return tuple(result)


def nav_daily_returns(series: Sequence[NavPoint]) -> tuple[Decimal, ...]:
    """Return exact daily fractional changes from a validated NAV series."""
    points = _validated_nav_series(series)
    result: list[Decimal] = []
    previous = points[0]
    for point in points[1:]:
        result.append(point.value / previous.value - 1)
        previous = point
    return tuple(result)


def _validated_nav_series(series: Sequence[NavPoint]) -> tuple[NavPoint, ...]:
    points = tuple(series)
    if not points:
        raise CuratedMetricsError("at least one NAV point is required")
    previous_date: dt.date | None = None
    for point in points:
        if not isinstance(point, NavPoint):
            raise CuratedMetricsError("every series point must be a NavPoint")
        day = _valid_date(point.day, "NAV day")
        if previous_date is not None and day <= previous_date:
            raise CuratedMetricsError("NAV days must be strictly increasing")
        previous_date = day
        _positive_decimal(point.value, "NAV value")
    return points


def absolute_return_pct(start_value: Decimal, end_value: Decimal) -> Decimal:
    """Return an unrounded absolute percentage, not a decimal fraction."""
    start = _positive_decimal(start_value, "start value")
    end = _positive_decimal(end_value, "end value")
    return (end / start - 1) * 100


def cagr_pct(
    start_value: Decimal,
    end_value: Decimal,
    start_date: dt.date,
    end_date: dt.date,
) -> Decimal:
    """Return unrounded CAGR percent using the repo's 365.25-day year convention."""
    start = _positive_decimal(start_value, "start value")
    end = _positive_decimal(end_value, "end value")
    first_day = _valid_date(start_date, "start_date")
    last_day = _valid_date(end_date, "end_date")
    day_count = (last_day - first_day).days
    if day_count <= 0:
        raise CuratedMetricsError("CAGR requires end_date after start_date")
    exponent = Decimal("365.25") / Decimal(day_count)
    with localcontext() as context:
        context.prec = 50
        return ((end / start) ** exponent - 1) * 100


def _subtract_months(day: dt.date, months: int) -> dt.date:
    if type(months) is not int or months <= 0:
        raise CuratedMetricsError("months must be a positive integer")
    zero_based_month = day.year * 12 + day.month - 1 - months
    year, month_index = divmod(zero_based_month, 12)
    month = month_index + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _window_start(points: tuple[NavPoint, ...], months: int) -> NavPoint | None:
    cutoff = _subtract_months(points[-1].day, months)
    if points[0].day > cutoff:
        return None
    for point in points:
        if point.day >= cutoff:
            # A final observation by itself is not a historical window.  This also prevents a
            # sparse/incomplete series from manufacturing a zero-day CAGR.  A complete EOD series
            # reaches the first trading day within a week of any calendar cutoff; a larger gap is
            # incomplete price coverage and must remain unavailable for the history caveat.
            if point.day == points[-1].day or (point.day - cutoff).days > MAX_WINDOW_START_GAP_DAYS:
                return None
            return point
    raise AssertionError("the final NAV point must be on or after its own trailing cutoff")


def _window_return(points: tuple[NavPoint, ...], months: int) -> Decimal | None:
    start = _window_start(points, months)
    if start is None:
        return None
    return absolute_return_pct(start.value, points[-1].value)


def _window_cagr(points: tuple[NavPoint, ...], months: int) -> Decimal | None:
    start = _window_start(points, months)
    if start is None:
        return None
    return cagr_pct(start.value, points[-1].value, start.day, points[-1].day)


def window_metrics(series: Sequence[NavPoint]) -> WindowMetrics:
    """Compute the catalog's 1M/6M/1Y absolute and 3Y/5Y CAGR percentages."""
    points = _validated_nav_series(series)
    return WindowMetrics(
        ret_1m=_window_return(points, 1),
        ret_6m=_window_return(points, 6),
        ret_1y=_window_return(points, 12),
        cagr_3y=_window_cagr(points, 36),
        cagr_5y=_window_cagr(points, 60),
        since_inception_pct=absolute_return_pct(points[0].value, points[-1].value),
    )


def _complete_calendar_months(start: dt.date, end: dt.date) -> int:
    months = (end.year - start.year) * 12 + end.month - start.month
    if end.day < start.day:
        months -= 1
    return max(0, months)


def headline_metric(series: Sequence[NavPoint]) -> HeadlineMetric:
    """Choose the card metric by age, with an explicit window in every label."""
    points = _validated_nav_series(series)
    metrics = window_metrics(points)
    final_day = points[-1].day
    if points[0].day <= _subtract_months(final_day, 60):
        if metrics.cagr_5y is None:
            raise CuratedMetricsError("5Y headline history is incomplete")
        return HeadlineMetric("5Y CAGR", metrics.cagr_5y)
    if points[0].day <= _subtract_months(final_day, 36):
        if metrics.cagr_3y is None:
            raise CuratedMetricsError("3Y headline history is incomplete")
        return HeadlineMetric("3Y CAGR", metrics.cagr_3y)
    if points[0].day <= _subtract_months(final_day, 12):
        if metrics.ret_1y is None:
            raise CuratedMetricsError("1Y headline history is incomplete")
        return HeadlineMetric("1Y returns", metrics.ret_1y)
    months = max(1, _complete_calendar_months(points[0].day, points[-1].day))
    return HeadlineMetric(f"{months}M returns", metrics.since_inception_pct)


def annualized_volatility(daily_returns: Sequence[Decimal]) -> Decimal:
    """Sample standard deviation (ddof=1), annualized by ``sqrt(252)``."""
    count = len(daily_returns)
    if count < MIN_VOLATILITY_RETURN_COUNT:
        raise CuratedMetricsError("annualized volatility requires at least two daily returns")
    total = Decimal(0)
    for value in daily_returns:
        _finite_decimal(value, "daily return")
        if value < -1:
            raise CuratedMetricsError("daily return cannot be below -1")
        total += value
    with localcontext() as context:
        context.prec = 50
        mean = total / Decimal(count)
        squared_deviations = sum(((value - mean) ** 2 for value in daily_returns), start=Decimal(0))
        sample_variance = squared_deviations / Decimal(count - 1)
        return sample_variance.sqrt() * TRADING_DAYS_PER_YEAR.sqrt()


def weighted_constituent_volatility(
    constituents: Sequence[ConstituentVolatility],
) -> Decimal:
    """Return the docs/04 young-basket fallback: target-weighted annual constituent vols."""
    rows = tuple(constituents)
    if not rows:
        raise CuratedMetricsError("constituent volatility fallback requires constituents")
    seen: set[int] = set()
    total_weight = Decimal(0)
    result = Decimal(0)
    for row in rows:
        if not isinstance(row, ConstituentVolatility):
            raise CuratedMetricsError("every fallback row must be ConstituentVolatility")
        if type(row.instrument_id) is not int or row.instrument_id <= 0:
            raise CuratedMetricsError("instrument_id must be a positive integer")
        if row.instrument_id in seen:
            raise CuratedMetricsError(f"duplicate instrument_id: {row.instrument_id}")
        seen.add(row.instrument_id)
        weight = _positive_decimal(row.weight, "weight")
        if weight > 1:
            raise CuratedMetricsError("weight must be at most one")
        volatility = _finite_decimal(row.annualized_volatility, "annualized volatility")
        if volatility < 0:
            raise CuratedMetricsError("annualized volatility must be non-negative")
        total_weight += weight
        result += weight * volatility
    if total_weight != EXACT_TOTAL_WEIGHT:
        raise CuratedMetricsError(
            f"constituent weights must total exactly {EXACT_TOTAL_WEIGHT}; got {total_weight}"
        )
    return result


def basket_volatility(
    daily_returns: Sequence[Decimal],
    constituent_fallback: Sequence[ConstituentVolatility],
) -> Decimal:
    """Use trailing basket returns at 60+ days, otherwise the constituent fallback."""
    if len(daily_returns) < YOUNG_BASKET_RETURN_DAYS:
        return weighted_constituent_volatility(constituent_fallback)
    trailing_returns = daily_returns[-MAX_VOLATILITY_RETURN_DAYS:]
    return annualized_volatility(trailing_returns)


def fixed_volatility_bucket(value: Decimal) -> VolatilityBucket:
    """Apply LOW <15%, MED <25%, HIGH at/above 25% using fractional values."""
    volatility = _finite_decimal(value, "volatility")
    if volatility < 0:
        raise CuratedMetricsError("volatility must be non-negative")
    if volatility < Decimal("0.15"):
        return VolatilityBucket.LOW
    if volatility < Decimal("0.25"):
        return VolatilityBucket.MED
    return VolatilityBucket.HIGH


def assign_volatility_buckets(
    published: Sequence[PublishedVolatility],
) -> dict[int, VolatilityBucket]:
    """Assign fixed buckets below 12 baskets, else stable rank-based terciles.

    Ties are broken by ``basket_id`` so a recomputation is deterministic and each tercile differs
    in size by at most one.  This is a catalog label, not a claim that tied estimates differ.
    """
    rows = tuple(published)
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, PublishedVolatility):
            raise CuratedMetricsError("every published row must be PublishedVolatility")
        if type(row.basket_id) is not int or row.basket_id <= 0:
            raise CuratedMetricsError("basket_id must be a positive integer")
        if row.basket_id in seen:
            raise CuratedMetricsError(f"duplicate basket_id: {row.basket_id}")
        seen.add(row.basket_id)
        volatility = _finite_decimal(row.value, "volatility")
        if volatility < 0:
            raise CuratedMetricsError("volatility must be non-negative")
    if len(rows) < TERCILE_MINIMUM_PUBLISHED:
        return {row.basket_id: fixed_volatility_bucket(row.value) for row in rows}

    sorted_rows = sorted(rows, key=lambda row: (row.value, row.basket_id))
    result: dict[int, VolatilityBucket] = {}
    labels = (VolatilityBucket.LOW, VolatilityBucket.MED, VolatilityBucket.HIGH)
    count = len(sorted_rows)
    for index, row in enumerate(sorted_rows):
        result[row.basket_id] = labels[index * 3 // count]
    return result
