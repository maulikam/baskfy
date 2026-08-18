"""Zerodha delivery cost model, calibrated against a real session.

18 Aug 2026: Rs 7,695.87 on Rs 73.5 lakh of turnover across 21 delivery orders, 8 of them
sells. The breakdown is what matters, because it decides what a minimum trade size can
achieve — and the answer is "less than you would hope".
"""
from __future__ import annotations

import pytest

from app import config as C
from app import costs


def test_the_model_reproduces_the_live_session_within_ten_percent():
    """Buys 56,04,878 and sells 17,44,673 across 8 sell scrips."""
    c = costs.plan_cost([{"delta": 1, "ref_price": 5_604_878.0},
                         {"delta": -1, "ref_price": 1_744_673.0}])
    assert c["total"] == pytest.approx(7_695.87, rel=0.15)
    assert 9.0 < c["bps_of_turnover"] < 13.0


def test_stt_dominates_and_no_threshold_can_avoid_it():
    """95% of the bill. It is 0.1% of turnover on BOTH sides, so the only way to pay less
    is to trade less — a fact worth pinning, because a no-trade band cannot touch it."""
    c = costs.plan_cost([{"delta": 1, "ref_price": 5_604_878.0},
                         {"delta": -1, "ref_price": 1_744_673.0}])
    assert c["stt"] / c["total"] > 0.85


def test_delivery_brokerage_is_free():
    assert costs.order_cost(100_000, "BUY").brokerage == 0.0


def test_only_sells_pay_the_dp_charge():
    assert costs.order_cost(50_000, "SELL").dp == pytest.approx(costs.DP_CHARGE_PER_SELL)
    assert costs.order_cost(50_000, "BUY").dp == 0.0


def test_only_buys_pay_stamp_duty():
    assert costs.order_cost(50_000, "BUY").stamp > 0
    assert costs.order_cost(50_000, "SELL").stamp == 0.0


# =====================================================================================
# the asymmetry a single rupee threshold misses
# =====================================================================================
def test_a_small_sell_is_disproportionately_expensive():
    """Rs 16 of DP is 0.9% of a Rs 2,000 sale and 0.03% of a Rs 50,000 one. That curve is
    the whole reason the band judges cost as a percentage rather than a flat floor."""
    assert costs.cost_pct(2_000, "SELL") > 0.8
    assert costs.cost_pct(50_000, "SELL") < 0.15


def test_a_buy_costs_the_same_percentage_at_any_size():
    """No fixed component, so size does not change efficiency — which is why the rupee
    floor is still needed for buys."""
    small, large = costs.cost_pct(2_000, "BUY"), costs.cost_pct(500_000, "BUY")
    assert small == pytest.approx(large, rel=0.01)


def test_the_cost_ceiling_rejects_a_small_sell_and_accepts_a_large_one():
    assert costs.cost_pct(2_000, "SELL") > C.MAX_TRADE_COST_PCT
    assert costs.cost_pct(200_000, "SELL") < C.MAX_TRADE_COST_PCT


def test_every_rate_is_a_named_constant():
    """A rate buried in an expression cannot be reviewed when it changes."""
    src = open("app/costs.py").read()
    for name in ("STT_DELIVERY", "STAMP_DUTY_BUY", "DP_CHARGE_PER_SELL",
                 "EXCHANGE_TXN_NSE", "GST"):
        assert f"{name} =" in src


def test_a_plan_reports_its_own_bill():
    c = costs.plan_cost([{"delta": 100, "ref_price": 1000.0},
                         {"delta": -50, "ref_price": 2000.0}])
    assert c["turnover"] == 200_000 and c["total"] > 0
    assert c["dp"] == pytest.approx(costs.DP_CHARGE_PER_SELL)
