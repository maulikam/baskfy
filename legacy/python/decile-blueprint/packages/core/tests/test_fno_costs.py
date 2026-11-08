"""``docs/fno/04`` §3 (F1: the options rates on eight orders, share of credit, 25 % cap) and §10
(F2: the futures charges per order and per roll)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from baskfy_core.fno.condor import LegRole
from baskfy_core.fno.config import F1Config, FutureCostRates, PlanState
from baskfy_core.fno.costs import (
    condor_fills,
    condor_round_trip,
    crossing_cost,
    future_roll,
    future_round_trip,
)
from baskfy_core.options.config import CostRates, Side
from baskfy_core.options.costs import charges

F1 = F1Config()
RATES = CostRates()
MIDS = {
    LegRole.LONG_CALL: Decimal("20"),
    LegRole.SHORT_CALL: Decimal("45"),
    LegRole.SHORT_PUT: Decimal("60"),
    LegRole.LONG_PUT: Decimal("30"),
}


def test_eight_orders_with_the_right_sides() -> None:
    fills = condor_fills(MIDS, MIDS, 30)
    assert len(fills) == 8
    sells = {(f.price, f.side) for f in fills if f.side is Side.SELL}
    assert (Decimal(45), Side.SELL) in sells  # the short call opens as a sale
    assert (Decimal(20), Side.SELL) in sells  # the long call closes as a sale


def test_crossing_cost_has_the_tick_floor() -> None:
    assert crossing_cost(Decimal(5), F1) == Decimal("0.05")
    assert crossing_cost(Decimal(100), F1) == Decimal("0.5")


def test_round_trip_is_charges_plus_slippage_and_its_share_of_credit() -> None:
    cost = condor_round_trip(entry=MIDS, exit_=None, quantity=30, rates=RATES, config=F1)
    expected_charges = charges(condor_fills(MIDS, MIDS, 30), RATES)
    assert cost.charges == expected_charges
    assert cost.charges.brokerage == Decimal(160)  # ₹20 x 8 orders
    slip_units = sum(
        (2 * max(Decimal("0.05"), m * Decimal("0.005")) for m in MIDS.values()), Decimal(0)
    )
    assert cost.slippage_inr == (slip_units * 30).quantize(Decimal("0.01"))
    assert cost.credit_inr == Decimal(1650)  # 55 x 30
    assert cost.round_trip_inr == expected_charges.total + cost.slippage_inr
    assert cost.cost_share_pct == (cost.round_trip_inr / Decimal(1650) * 100).quantize(
        Decimal("0.01")
    )


def test_above_twenty_five_percent_is_rejected_cost() -> None:
    # One lot of a thin credit (29 a unit, ₹870): ₹160 of brokerage alone is ~18 % of it.
    thin = dict(MIDS)
    thin[LegRole.SHORT_PUT] = Decimal("34")
    cost = condor_round_trip(entry=thin, exit_=None, quantity=30, rates=RATES, config=F1)
    assert cost.cost_share_pct is not None
    assert cost.cost_share_pct > Decimal(25)
    assert cost.state is PlanState.REJECTED_COST
    ok = condor_round_trip(entry=MIDS, exit_=None, quantity=300, rates=RATES, config=F1)
    assert ok.state is None


def test_no_exercise_stt_ever() -> None:
    cost = condor_round_trip(entry=MIDS, exit_=None, quantity=30, rates=RATES, config=F1)
    sells = sum(f.turnover for f in condor_fills(MIDS, MIDS, 30) if f.side is Side.SELL)
    assert cost.charges.stt == (sells * Decimal("0.0015")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def test_future_round_trip_is_04_section_10() -> None:
    rates = FutureCostRates()
    c = future_round_trip(Decimal(1000), Decimal(1100), 500, rates)
    buys, sells = Decimal(500_000), Decimal(550_000)
    assert c.orders == 2
    assert c.brokerage == Decimal(40)
    assert c.stt == Decimal("275.00")  # 0.05 % of the sale
    assert c.exchange_txn == ((buys + sells) * Decimal("0.0000173")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    assert c.stamp == Decimal("10.00")  # 0.002 % of the buy
    assert c.gst == ((c.brokerage + c.exchange_txn) * Decimal("0.18")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    assert c.slippage == Decimal("315.00")  # 0.03 % a side
    assert c.total == c.brokerage + c.stt + c.exchange_txn + c.stamp + c.gst + c.slippage


def test_a_roll_is_one_sale_and_one_buy() -> None:
    rates = FutureCostRates()
    r = future_roll(Decimal(1000), Decimal(1005), 500, rates)
    assert r.orders == 2
    assert r.stt == Decimal("250.00")
    assert r.stamp == Decimal("10.05")
