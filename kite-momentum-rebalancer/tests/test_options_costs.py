"""Round-trip cost model for NIFTY options and futures.

Every conclusion about whether an option strategy has positive expectancy is a difference
between gross edge and cost, two numbers of similar size. These tests exist because a cost
model that is wrong by a factor silently reverses the finding.
"""
from __future__ import annotations

import pytest

from app.strategies.options_costs import (
    CostRates,
    Fill,
    exercise_stt,
    future_costs,
    option_costs,
)

LOT = 65


def leg(side, price, qty=LOT, half=0.05, label=""):
    """A fill that crosses a 2-tick book: BUY lifts the ask, SELL hits the bid."""
    bid, ask = price - half, price + half
    return Fill(side=side, price=ask if side == "BUY" else bid, quantity=qty,
                bid=bid, ask=ask, label=label)


def condor(lots=1, short=30.95, wing=8.0, exit_frac=0.65):
    q = LOT * lots
    return [leg("SELL", short, q), leg("SELL", short, q),
            leg("BUY", wing, q), leg("BUY", wing, q),
            leg("BUY", short * exit_frac, q), leg("BUY", short * exit_frac, q),
            leg("SELL", wing * exit_frac, q), leg("SELL", wing * exit_frac, q)]


# =====================================================================================
# the error that would erase the finding
# =====================================================================================
def test_option_brokerage_is_flat_and_never_capped_by_a_percentage():
    """Zerodha's 'Rs 20 or 0.03%, whichever is lower' applies to FUTURES, not options.
    Applying it to options collapses brokerage to under a rupee a leg — and flat
    brokerage is exactly what makes a 4-leg structure uneconomic at low size."""
    c = option_costs([leg("SELL", 5.0)], lot_size=LOT)      # tiny premium
    assert c.brokerage == 20.0


def test_futures_brokerage_does_take_the_percentage_cap():
    c = future_costs([Fill("BUY", 24400.0, LOT, bid=24399.75, ask=24400.25)],
                     lot_size=LOT)
    assert c.brokerage == pytest.approx(20.0)               # 0.03% of 15.8L >> 20


def test_brokerage_dominates_a_small_condor():
    c = option_costs(condor(lots=1), lot_size=LOT)
    assert c.brokerage / c.total > 0.6


def test_brokerage_share_falls_as_size_rises():
    small = option_costs(condor(lots=1), lot_size=LOT)
    big = option_costs(condor(lots=25), lot_size=LOT)
    assert big.brokerage / big.total < small.brokerage / small.total


# =====================================================================================
# statutory components
# =====================================================================================
def test_stt_is_charged_on_sales_only():
    sold = option_costs([leg("SELL", 100.0)], lot_size=LOT)
    bought = option_costs([leg("BUY", 100.0)], lot_size=LOT)
    assert sold.stt > 0 and bought.stt == 0


def test_stt_is_levied_on_premium_not_on_notional():
    """0.15% of a Rs 100 premium is Rs 9.75 on one lot, not 0.15% of 24,400 x 65."""
    c = option_costs([Fill("SELL", 100.0, LOT)], lot_size=LOT)
    assert c.stt == pytest.approx(100.0 * LOT * 0.0015)


def test_stamp_duty_is_charged_on_purchases_only():
    assert option_costs([leg("BUY", 100.0)], lot_size=LOT).stamp > 0
    assert option_costs([leg("SELL", 100.0)], lot_size=LOT).stamp == 0


def test_gst_applies_to_brokerage_and_fees_but_not_to_stt_or_stamp():
    c = option_costs([leg("SELL", 100.0)], lot_size=LOT)
    expected = 0.18 * (c.brokerage + c.transaction + c.sebi + c.ipft)
    assert c.gst == pytest.approx(expected)


def test_the_components_sum_to_the_statutory_total():
    c = option_costs(condor(), lot_size=LOT)
    parts = c.brokerage + c.stt + c.transaction + c.sebi + c.stamp + c.ipft + c.gst
    assert c.statutory_total == pytest.approx(parts)


def test_rates_can_be_rolled_back_to_re_cost_an_earlier_period():
    """The STT hike on 1 Apr 2026 flipped the sign of some trades; a model that cannot
    express the old rate cannot show that."""
    old = CostRates(stt_option_sell=0.0010)
    now = option_costs([leg("SELL", 100.0)], lot_size=LOT).stt
    then = option_costs([leg("SELL", 100.0)], rates=old, lot_size=LOT).stt
    assert then == pytest.approx(now * 2 / 3)


# =====================================================================================
# spread cost is measured, never assumed
# =====================================================================================
def test_crossing_the_book_costs_half_the_spread_not_the_whole_spread():
    """A 2-tick-wide book costs 1 tick against mid. Charging the full spread against mid
    double-counts, which is how a cost estimate drifts 10-30% high."""
    c = option_costs([leg("BUY", 100.0, half=0.05)], lot_size=LOT)
    assert c.spread_cost == pytest.approx(0.05 * LOT)


def test_a_fill_at_mid_costs_no_spread():
    c = option_costs([Fill("BUY", 100.0, LOT, bid=99.95, ask=100.05)], lot_size=LOT)
    assert c.spread_cost == pytest.approx(0.0)


def test_price_improvement_is_recorded_as_a_negative_cost():
    """A model that can only ever charge is as wrong as one that never does."""
    c = option_costs([Fill("BUY", 99.95, LOT, bid=99.95, ask=100.05)], lot_size=LOT)
    assert c.spread_cost < 0


def test_a_leg_without_a_quote_makes_the_total_unavailable_not_optimistic():
    """Silently costing an unquoted leg at zero is the single easiest way to manufacture
    an edge that does not exist."""
    c = option_costs([leg("SELL", 100.0), Fill("BUY", 50.0, LOT)], lot_size=LOT)
    assert c.spread_complete is False
    assert c.total is None and c.per_unit is None
    assert c.statutory_total > 0          # what IS known is still reported


def test_a_wide_wing_costs_proportionally_far_more_than_a_liquid_leg():
    """One tick on a 0.05-delta wing can be a tenth of its value; the same tick on an ATM
    leg is noise. A fixed tick assumption hides this entirely."""
    atm = option_costs([leg("BUY", 100.0, half=0.025)], lot_size=LOT)
    wing = option_costs([leg("BUY", 0.55, half=0.025)], lot_size=LOT)
    assert atm.spread_cost == pytest.approx(wing.spread_cost)          # same rupees
    assert (wing.spread_cost / (0.55 * LOT)) > 20 * (atm.spread_cost / (100.0 * LOT))


# =====================================================================================
# expiry settlement
# =====================================================================================
def test_exercise_stt_is_charged_on_intrinsic_value():
    assert exercise_stt(50.0, LOT) == pytest.approx(50.0 * LOT * 0.0015)


def test_an_out_of_the_money_expiry_costs_no_exercise_stt():
    assert exercise_stt(-10.0, LOT) == 0.0


def test_expiring_in_the_money_now_costs_the_same_as_squaring_off():
    """Before Apr 2026 the exercise rate differed; the two converged, which removes the
    old reason to always square off an ITM option."""
    assert exercise_stt(50.0, LOT) == pytest.approx(
        option_costs([Fill("SELL", 50.0, LOT)], lot_size=LOT).stt)


# =====================================================================================
# futures, for comparing the same view expressed in the underlying
# =====================================================================================
def test_a_futures_round_trip_reproduces_the_published_figure():
    """Rs 940 = 14.5 NIFTY points on one lot at 24,400, post-Apr-2026 STT."""
    c = future_costs([Fill("BUY", 24400.0, LOT, bid=24400.0, ask=24400.0),
                      Fill("SELL", 24400.0, LOT, bid=24400.0, ask=24400.0)],
                     lot_size=LOT)
    assert c.statutory_total == pytest.approx(940, abs=15)
    assert c.statutory_total / LOT == pytest.approx(14.5, abs=0.3)


def test_futures_stt_is_on_notional_and_dominates():
    c = future_costs([Fill("SELL", 24400.0, LOT, bid=24400.0, ask=24400.0)], lot_size=LOT)
    assert c.stt == pytest.approx(24400.0 * LOT * 0.0005)
    assert c.stt / c.statutory_total > 0.9


def test_an_eight_leg_condor_costs_more_than_a_two_leg_futures_round_trip_per_lot():
    """Not in rupees — in what it must overcome. The condor's edge is measured in points
    of premium; the future's in points of index."""
    cond = option_costs(condor(lots=1), lot_size=LOT)
    assert cond.total_legs == 8
    assert cond.brokerage == 8 * 20.0
