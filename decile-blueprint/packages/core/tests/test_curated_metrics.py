"""Curated-basket catalog metrics - docs/smallcase/04 section section 2-3 (SC2).

Tests assert the *spec*: min-amount buys >=1 share of every constituent; volatility buckets
follow PACK.1 thresholds; chain-linked NAV is deterministic for a fixture series.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from baskfy_core.curated_baskets import WEIGHT_QUANTIZE, assert_weights_sum_to_one
from baskfy_core.curated_metrics import (
    TERCILE_SWITCH_PUBLISHED_COUNT,
    VOL_BUCKET_HIGH_FLOOR,
    VOL_BUCKET_MED_FLOOR,
    absolute_return,
    annualized_volatility,
    basket_day_return,
    cagr,
    chain_link_nav,
    headline_return,
    min_amount,
    shares_at_amount,
    volatility_bucket,
    volatility_bucket_for_catalog,
)


def _d(value: str) -> Decimal:
    return Decimal(value)


class TestMinAmountSpec:
    def test_equal_weight_two_names(self) -> None:
        # max(100/0.5, 200/0.5) = 400 → ceil 400; each gets floor(400*0.5/price) >= 1.
        amount = min_amount([_d("100"), _d("200")], [_d("0.5"), _d("0.5")])
        assert amount == _d("400")
        shares = shares_at_amount(amount, [_d("100"), _d("200")], [_d("0.5"), _d("0.5")])
        assert shares == [2, 1]

    def test_expensive_low_weight_dominates(self) -> None:
        # price/weight = 500/0.1 = 5000.
        amount = min_amount(
            [_d("100"), _d("500")],
            [_d("0.9"), _d("0.1")],
        )
        assert amount == _d("5000")
        shares = shares_at_amount(amount, [_d("100"), _d("500")], [_d("0.9"), _d("0.1")])
        assert all(s >= 1 for s in shares)

    def test_lot_size_scales_the_bound(self) -> None:
        unit = min_amount([_d("100")], [_d("1")], lot_size=1)
        lots = min_amount([_d("100")], [_d("1")], lot_size=5)
        assert lots == unit * 5

    def test_rejects_non_positive_price(self) -> None:
        with pytest.raises(ValueError, match="price"):
            min_amount([_d("0")], [_d("1")])

    @given(
        n=st.integers(min_value=2, max_value=8),
        seed=st.integers(min_value=1, max_value=10_000),
    )
    def test_property_min_amount_buys_at_least_one_share(self, n: int, seed: int) -> None:
        """docs/smallcase/04 section 2: buying exactly min_amount yields >=1 share of every name.

        Weights are quantized to 4 dp (``cb_constituent.weight`` storage) before the bound
        is computed - the same inputs the EOD job and plan preview will pass.
        """
        prices = [Decimal(10 + ((seed * (i + 3)) % 500)) for i in range(n)]
        raw = [Decimal(1 + ((seed * (i + 7)) % 20)) for i in range(n)]
        total = sum(raw)
        weights = [(w / total).quantize(WEIGHT_QUANTIZE) for w in raw]
        # Absorb residual on the last weight so the domain contract holds.
        residual = Decimal("1.0000") - sum(weights)
        weights[-1] = (weights[-1] + residual).quantize(WEIGHT_QUANTIZE)
        assert_weights_sum_to_one(weights)
        amount = min_amount(prices, weights)
        shares = shares_at_amount(amount, prices, weights)
        assert all(s >= 1 for s in shares), (amount, prices, weights, shares)


class TestVolatilityBucket:
    def test_pack1_boundaries(self) -> None:
        assert volatility_bucket(_d("0.1499")) == "LOW"
        assert volatility_bucket(VOL_BUCKET_MED_FLOOR) == "MED"
        assert volatility_bucket(_d("0.2499")) == "MED"
        assert volatility_bucket(VOL_BUCKET_HIGH_FLOOR) == "HIGH"
        assert volatility_bucket(_d("0.50")) == "HIGH"

    def test_fixed_until_twelve_published(self) -> None:
        assert (
            volatility_bucket_for_catalog(
                _d("0.20"),
                published_count=TERCILE_SWITCH_PUBLISHED_COUNT - 1,
                peer_vols=[_d("0.10")] * 11,
            )
            == "MED"
        )


class TestChainLinkedSeries:
    def test_golden_nav_from_fixture_returns(self) -> None:
        dates = [dt.date(2024, 1, d) for d in (2, 3, 4)]
        rets = [_d("0.01"), _d("-0.005"), _d("0.02")]
        series = chain_link_nav(dates, rets, base=_d("100"))
        assert series[0] == (dates[0], _d("101.00"))
        assert series[1][1] == _d("100.50")  # 101 * 0.995 → 100.495 half-up
        assert series[2][1] == _d("102.51")  # 100.50 * 1.02

    def test_absolute_and_cagr(self) -> None:
        assert absolute_return(_d("100"), _d("110")) == _d("10.00")
        assert cagr(nav_start=_d("100"), nav_end=_d("121"), years=_d("2")) == _d("10.00")

    def test_basket_day_return_equal_weight(self) -> None:
        ret = basket_day_return(
            {"AAA": _d("0.5"), "BBB": _d("0.5")},
            {"AAA": _d("100"), "BBB": _d("200")},
            {"AAA": _d("110"), "BBB": _d("200")},
        )
        assert ret == _d("0.05")  # 0.5*0.10 + 0.5*0

    def test_headline_by_age(self) -> None:
        h = headline_return(
            age_years=_d("5"),
            ret_1m=_d("1"),
            ret_1y=_d("10"),
            cagr_3y=_d("12"),
            cagr_5y=_d("14"),
        )
        assert h.label == "5Y CAGR"
        assert h.value_pct == _d("14")


class TestAnnualizedVol:
    def test_constant_series_is_zero(self) -> None:
        assert annualized_volatility([_d("0.01")] * 5) == _d("0")

    def test_too_short_is_none(self) -> None:
        assert annualized_volatility([_d("0.01")]) is None
