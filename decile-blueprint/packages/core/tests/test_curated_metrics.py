from __future__ import annotations

import datetime as dt
import time
import tracemalloc
from decimal import Decimal
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from baskfy_core.curated_metrics import (
    AdjustedClose,
    ConstituentVolatility,
    CuratedMetricsError,
    DailyAdjustedCloses,
    NavPoint,
    PublishedVolatility,
    TargetWeight,
    VersionWeights,
    VolatilityBucket,
    WeightedPrice,
    absolute_return_pct,
    annualized_volatility,
    assign_volatility_buckets,
    basket_volatility,
    cagr_pct,
    chain_linked_nav,
    fixed_volatility_bucket,
    headline_metric,
    minimum_investment_amount,
    nav_daily_returns,
    round_money,
    round_return,
    whole_share_allocation,
    window_metrics,
)

D = Decimal


def _price_day(day: dt.date, first: str, second: str) -> DailyAdjustedCloses:
    return DailyAdjustedCloses(
        day,
        (
            AdjustedClose(1, D(first)),
            AdjustedClose(2, D(second)),
        ),
    )


def _two_name_version(day: dt.date, first: str, second: str) -> VersionWeights:
    return VersionWeights(
        day,
        (
            TargetWeight(1, D(first)),
            TargetWeight(2, D(second)),
        ),
    )


def test_minimum_amount_and_whole_share_allocation_golden() -> None:
    constituents = (
        WeightedPrice(1, D("0.6000"), D("120.25")),
        WeightedPrice(2, D("0.4000"), D("80.10")),
    )

    minimum = minimum_investment_amount(constituents)
    allocation = whole_share_allocation(minimum, constituents)

    assert minimum == D(201)
    assert [row.shares for row in allocation] == [1, 1]
    assert [row.invested_amount for row in allocation] == [D("120.25"), D("80.10")]
    assert whole_share_allocation(minimum - 1, constituents)[0].shares == 0


@given(
    first_weight_units=st.integers(min_value=1, max_value=9_999),
    first_price_paise=st.integers(min_value=1, max_value=2_000_000),
    second_price_paise=st.integers(min_value=1, max_value=2_000_000),
)
@settings(max_examples=150, deadline=None)
def test_minimum_amount_property_exactly_fits_at_least_one_and_each_floor_is_maximal(
    first_weight_units: int,
    first_price_paise: int,
    second_price_paise: int,
) -> None:
    first_weight = D(first_weight_units) / D(10_000)
    constituents = (
        WeightedPrice(1, first_weight, D(first_price_paise) / 100),
        WeightedPrice(2, D(1) - first_weight, D(second_price_paise) / 100),
    )

    amount = minimum_investment_amount(constituents)
    allocation = whole_share_allocation(amount, constituents)

    assert all(row.shares >= 1 for row in allocation)
    for row in allocation:
        budget = amount * row.target_weight
        assert row.invested_amount <= budget
        assert (row.shares + 1) * row.raw_price > budget


def test_allocation_does_not_redistribute_residual_cash() -> None:
    constituents = (
        WeightedPrice(1, D("0.5000"), D(60)),
        WeightedPrice(2, D("0.5000"), D(70)),
    )
    allocation = whole_share_allocation(D(200), constituents)
    assert [row.shares for row in allocation] == [1, 1]


def test_minimum_and_allocation_stay_exact_beyond_decimal_context_precision() -> None:
    constituents = (
        WeightedPrice(1, D("0.0001"), D("99999999999999.9999")),
        WeightedPrice(2, D("0.9999"), D("0.0001")),
    )
    minimum = minimum_investment_amount(constituents)
    assert minimum == D(999_999_999_999_999_999)
    assert whole_share_allocation(minimum, constituents)[0].shares == 1


def test_minimum_validation_rejects_duplicate_nonpositive_and_non_decimal_inputs() -> None:
    valid = WeightedPrice(1, D("1.0000"), D(100))
    with pytest.raises(CuratedMetricsError, match="duplicate"):
        minimum_investment_amount((valid, valid))
    with pytest.raises(CuratedMetricsError, match="raw_price"):
        minimum_investment_amount((WeightedPrice(1, D("1.0000"), D(0)),))
    with pytest.raises(CuratedMetricsError, match="finite Decimal"):
        minimum_investment_amount((WeightedPrice(1, D("1.0000"), cast(Decimal, 100.0)),))


def test_chain_linked_golden_uses_version_effective_on_each_return_date() -> None:
    versions = (
        _two_name_version(dt.date(2026, 1, 2), "0.6000", "0.4000"),
        _two_name_version(dt.date(2026, 1, 6), "0.2500", "0.7500"),
    )
    prices = (
        _price_day(dt.date(2026, 1, 2), "100", "100"),
        _price_day(dt.date(2026, 1, 5), "110", "90"),
        _price_day(dt.date(2026, 1, 6), "121", "90"),
    )

    series = chain_linked_nav(versions, prices)

    # Day two still uses v1: .6(+10%) + .4(-10%) = +2%.
    # The version effective on day three uses v2: .25(+10%) + .75(0%) = +2.5%.
    assert series == (
        NavPoint(dt.date(2026, 1, 2), D(100)),
        NavPoint(dt.date(2026, 1, 5), D("102.00")),
        NavPoint(dt.date(2026, 1, 6), D("104.550000")),
    )
    assert nav_daily_returns(series) == (D("0.02"), D("0.025"))


def test_chain_golden_ignores_pre_inception_prices_and_future_versions() -> None:
    versions = (
        _two_name_version(dt.date(2026, 1, 2), "0.5000", "0.5000"),
        _two_name_version(dt.date(2027, 1, 2), "0.9000", "0.1000"),
    )
    prices = (
        _price_day(dt.date(2026, 1, 1), "1", "1"),
        _price_day(dt.date(2026, 1, 2), "100", "100"),
        _price_day(dt.date(2026, 1, 5), "110", "100"),
    )

    assert chain_linked_nav(versions, prices)[-1].value == D(105)


def test_chain_rejects_missing_adjusted_close_instead_of_renormalizing() -> None:
    versions = (_two_name_version(dt.date(2026, 1, 2), "0.5000", "0.5000"),)
    prices = (
        _price_day(dt.date(2026, 1, 2), "100", "100"),
        DailyAdjustedCloses(dt.date(2026, 1, 5), (AdjustedClose(1, D(101)),)),
    )
    with pytest.raises(CuratedMetricsError, match=r"missing adjusted close.*instrument_id 2"):
        chain_linked_nav(versions, prices)


def test_chain_rejects_datetime_dates_and_non_increasing_dates() -> None:
    bad_version = VersionWeights(
        cast(dt.date, dt.datetime(2026, 1, 2)),
        (TargetWeight(1, D("1.0000")),),
    )
    with pytest.raises(CuratedMetricsError, match="date, not a datetime"):
        chain_linked_nav((bad_version,), (_price_day(dt.date(2026, 1, 2), "100", "100"),))

    version = VersionWeights(dt.date(2026, 1, 2), (TargetWeight(1, D("1.0000")),))
    duplicate_days = (
        DailyAdjustedCloses(dt.date(2026, 1, 2), (AdjustedClose(1, D(100)),)),
        DailyAdjustedCloses(dt.date(2026, 1, 2), (AdjustedClose(1, D(101)),)),
    )
    with pytest.raises(CuratedMetricsError, match="strictly increasing"):
        chain_linked_nav((version,), duplicate_days)


def test_absolute_window_returns_are_percentages() -> None:
    assert absolute_return_pct(D(100), D(112)) == D(12)
    series = (
        NavPoint(dt.date(2025, 1, 1), D(100)),
        NavPoint(dt.date(2025, 12, 31), D(120)),
        NavPoint(dt.date(2026, 1, 1), D(121)),
    )
    metrics = window_metrics(series)
    assert metrics.ret_1y == D(21)
    assert metrics.ret_1m is None
    assert metrics.ret_6m is None
    assert metrics.cagr_3y is None
    assert metrics.since_inception_pct == D(21)


def test_window_uses_first_trading_observation_on_or_after_calendar_cutoff() -> None:
    series = (
        NavPoint(dt.date(2025, 1, 1), D(90)),
        NavPoint(dt.date(2025, 1, 3), D(100)),
        NavPoint(dt.date(2026, 1, 2), D(120)),
    )
    assert window_metrics(series).ret_1y == D(20)


def test_cagr_uses_calendar_days_and_decimal_arithmetic() -> None:
    # 2020-01-01..2024-01-01 is exactly 4 * 365.25 days; 146.41 is 1.1**4 * 100.
    result = cagr_pct(D(100), D("146.41"), dt.date(2020, 1, 1), dt.date(2024, 1, 1))
    assert abs(result - D(10)) < D("0.0000000000000000000000001")


@pytest.mark.parametrize(
    ("days", "label"),
    [
        (31, "1M returns"),
        (366, "1Y returns"),
        (1_096, "3Y CAGR"),
        (1_827, "5Y CAGR"),
    ],
)
def test_headline_window_label_is_age_sensitive(days: int, label: str) -> None:
    start = dt.date(2020, 1, 1)
    series = (NavPoint(start, D(100)), NavPoint(start + dt.timedelta(days=days), D(110)))
    assert headline_metric(series).label == label


def test_headline_window_does_not_fall_back_when_age_required_history_is_incomplete() -> None:
    series = (
        NavPoint(dt.date(2020, 1, 1), D(100)),
        NavPoint(dt.date(2024, 12, 1), D(105)),
        NavPoint(dt.date(2025, 1, 10), D(110)),
    )
    with pytest.raises(CuratedMetricsError, match="5Y headline history is incomplete"):
        headline_metric(series)


def test_annualized_volatility_is_sample_standard_deviation() -> None:
    volatility = annualized_volatility((D("0.01"), D("-0.01")))
    # variance=.0002; annual variance=.0504. Squaring avoids an imprecise float oracle.
    assert abs(volatility * volatility - D("0.0504")) < D("1e-45")
    assert annualized_volatility((D("0.01"), D("0.01"), D("0.01"))) == 0


def test_young_basket_volatility_uses_target_weighted_constituent_fallback() -> None:
    fallback = (
        ConstituentVolatility(1, D("0.2500"), D("0.10")),
        ConstituentVolatility(2, D("0.7500"), D("0.30")),
    )
    assert basket_volatility((D("0.99"),) * 59, fallback) == D("0.250000")


def test_volatility_switches_from_young_fallback_at_sixty_returns() -> None:
    fallback = (ConstituentVolatility(1, D("1.0000"), D("0.80")),)
    assert basket_volatility((D(0),) * 60, fallback) == 0


def test_volatility_uses_only_trailing_252_daily_returns() -> None:
    fallback = (ConstituentVolatility(1, D("1.0000"), D("0.80")),)
    returns = (D("0.90"), D("-0.90")) + (D("0.01"),) * 252
    assert basket_volatility(returns, fallback) == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", VolatilityBucket.LOW),
        ("0.149999", VolatilityBucket.LOW),
        ("0.15", VolatilityBucket.MED),
        ("0.249999", VolatilityBucket.MED),
        ("0.25", VolatilityBucket.HIGH),
    ],
)
def test_fixed_volatility_bucket_boundaries(value: str, expected: VolatilityBucket) -> None:
    assert fixed_volatility_bucket(D(value)) is expected


def test_volatility_buckets_use_fixed_thresholds_below_twelve_published() -> None:
    rows = (
        PublishedVolatility(1, D("0.14")),
        PublishedVolatility(2, D("0.20")),
        PublishedVolatility(3, D("0.30")),
    )
    assert assign_volatility_buckets(rows) == {
        1: VolatilityBucket.LOW,
        2: VolatilityBucket.MED,
        3: VolatilityBucket.HIGH,
    }


def test_volatility_buckets_switch_to_deterministic_terciles_at_twelve() -> None:
    descending = tuple(PublishedVolatility(basket_id, D("0.30")) for basket_id in range(12, 0, -1))
    buckets = assign_volatility_buckets(descending)
    assert [buckets[basket_id] for basket_id in range(1, 13)] == [
        *(VolatilityBucket.LOW for _ in range(4)),
        *(VolatilityBucket.MED for _ in range(4)),
        *(VolatilityBucket.HIGH for _ in range(4)),
    ]


def test_decimal_storage_rounding_is_explicit_half_up() -> None:
    assert round_money(D("1.005")) == D("1.01")
    assert round_money(D("-1.005")) == D("-1.01")
    assert round_return(D("0.12345678905")) == D("0.1234567891")
    assert isinstance(round_return(D("0.1")), Decimal)


def test_float_inputs_are_rejected_instead_of_silently_coerced() -> None:
    with pytest.raises(CuratedMetricsError, match="finite Decimal"):
        round_money(cast(Decimal, 1.005))
    with pytest.raises(CuratedMetricsError, match="finite Decimal"):
        annualized_volatility((D("0.1"), cast(Decimal, 0.2)))


def test_chain_performance_and_memory_are_linear_and_bounded() -> None:
    version = VersionWeights(
        dt.date(2000, 1, 1),
        (
            TargetWeight(1, D("0.2500")),
            TargetWeight(2, D("0.2500")),
            TargetWeight(3, D("0.2500")),
            TargetWeight(4, D("0.2500")),
        ),
    )
    closes = tuple(
        DailyAdjustedCloses(
            dt.date(2000, 1, 1) + dt.timedelta(days=index),
            (
                AdjustedClose(1, D(100) + index),
                AdjustedClose(2, D(200) + index),
                AdjustedClose(3, D(300) + index),
                AdjustedClose(4, D(400) + index),
            ),
        )
        for index in range(8_000)
    )

    tracemalloc.start()
    started = time.perf_counter()
    result = chain_linked_nav((version,), closes)
    elapsed = time.perf_counter() - started
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(result) == 8_000
    assert elapsed < 8
    assert peak_bytes < 12 * 1024 * 1024
