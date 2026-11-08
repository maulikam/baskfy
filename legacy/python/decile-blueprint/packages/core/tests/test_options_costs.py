"""``docs/options/04`` §6 — costs, every rate a field, and the expiry-day STT trap.

The worked round trip below is computed by hand from ``04`` §6's table (OP0-verified rates), to
the paisa, component by component.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest
from options_fixtures import LOT

from baskfy_core.options.config import (
    DEFAULT_CEILINGS,
    DEFAULT_OPTIONS_CONFIG,
    CostRates,
    OptionsConfig,
    Side,
    Sleeve,
    check_hard_exits,
)
from baskfy_core.options.costs import (
    CostFill,
    CostVerdict,
    HeldLong,
    charges,
    cost_test,
    exercise_stt,
    expected_gain_o1,
    expected_gain_o2,
    expected_gain_o3,
    rates_review_due,
    settlement_stt,
    synthetic_half_spread,
)

RATES = DEFAULT_OPTIONS_CONFIG.costs
SETTLE = DEFAULT_OPTIONS_CONFIG.calendar.market_close
BUY = CostFill(Side.BUY, Decimal("200.05"), LOT)  # turnover 13,003.25
SELL = CostFill(Side.SELL, Decimal("260.00"), LOT)  # turnover 16,900.00


class TestTheWorkedRoundTrip:
    """O2: buy one lot at 200.05, sell it at 260.00. Premium turnover 29,903.25."""

    def test_each_component_to_the_paisa(self) -> None:
        got = charges([BUY, SELL], RATES)
        assert got.orders == 2
        assert got.brokerage == Decimal("40.00")  # ₹20 x 2 orders
        assert got.stt == Decimal("25.35")  # 16,900 x 0.15 %, sell side only
        assert got.exchange_txn == Decimal("10.62")  # 29,903.25 x 0.03553 % = 10.6246
        assert got.sebi == Decimal("0.03")  # 29,903.25 x ₹10 / crore = 0.0299
        assert got.ipft == Decimal("0.00")  # 29,903.25 x ₹0.01 / crore
        assert got.stamp == Decimal("0.39")  # 13,003.25 x 0.003 %, buy side only
        assert got.gst == Decimal("9.12")  # 18 % of (40 + 10.62 + 0.03 + 0.00) = 9.117
        assert got.total == Decimal("85.51")

    def test_a_condor_pays_eight_brokerages(self) -> None:
        legs = [CostFill(Side.BUY, Decimal("5"), LOT)] * 4 + [
            CostFill(Side.SELL, Decimal("5"), LOT)
        ] * 4
        assert charges(legs, RATES).brokerage == Decimal("160.00")

    def test_stt_is_sell_side_only_and_stamp_buy_side_only(self) -> None:
        assert charges([BUY], RATES).stt == 0
        assert charges([SELL], RATES).stamp == 0


@pytest.mark.parametrize(
    ("field", "component", "fills"),
    [
        ("brokerage_per_order_inr", "brokerage", [BUY, SELL]),
        ("stt_sell_premium_pct", "stt", [SELL]),
        ("exchange_txn_pct", "exchange_txn", [BUY, SELL]),
        ("sebi_per_crore_inr", "sebi", [BUY, SELL]),
        ("stamp_buy_pct", "stamp", [BUY]),
    ],
)
def test_each_rate_is_a_field_that_moves_its_component(
    field: str, component: str, fills: list[CostFill]
) -> None:
    """Doubling a rate doubles its component — the rate is read, not a literal."""
    base = getattr(charges(fills, RATES), component)
    doubled_rates = dataclasses.replace(RATES, **{field: getattr(RATES, field) * 2})
    doubled = getattr(charges(fills, doubled_rates), component)
    assert abs(doubled - 2 * base) <= Decimal("0.01")
    assert doubled > base


def test_ipft_and_gst_are_fields() -> None:
    big = [CostFill(Side.BUY, Decimal("1000"), 100_000)]  # ₹10 crore of premium
    assert charges(big, RATES).ipft == Decimal("0.10")  # ₹0.01 x 10
    assert charges(
        big, dataclasses.replace(RATES, ipft_per_crore_inr=Decimal("0.02"))
    ).ipft == Decimal("0.20")
    no_gst = charges([BUY, SELL], dataclasses.replace(RATES, gst_pct=Decimal(0)))
    assert no_gst.gst == 0


class TestTheSttTrap:
    """``04`` §6.3."""

    def test_exercise_stt_is_on_intrinsic(self) -> None:
        assert exercise_stt(Decimal(300), LOT, RATES) == Decimal("29.25")  # 300 x 65 x 0.15 %
        assert exercise_stt(Decimal(-50), LOT, RATES) == 0

    DEEP_ITM = (HeldLong(strike=Decimal(24500), is_call=True, quantity=LOT),)

    @pytest.mark.parametrize("sleeve", list(Sleeve))
    def test_every_hard_exit_makes_the_trap_unreachable(self, sleeve: Sleeve) -> None:
        hard_exit = DEFAULT_OPTIONS_CONFIG.hard_exit_time(sleeve)
        assert hard_exit < SETTLE
        assert settlement_stt(self.DEEP_ITM, Decimal(25500), hard_exit, SETTLE, RATES) == 0

    def test_a_hard_exit_past_1530_would_put_the_charge_in_the_journal(self) -> None:
        charge = settlement_stt(self.DEEP_ITM, Decimal(25500), dt.time(15, 31), SETTLE, RATES)
        assert charge == Decimal("97.50")  # 1,000 intrinsic x 65 x 0.15 %

    def test_a_put_leg_and_an_otm_leg(self) -> None:
        held = (
            HeldLong(strike=Decimal(26000), is_call=False, quantity=LOT),
            HeldLong(strike=Decimal(26000), is_call=True, quantity=LOT),
        )
        assert settlement_stt(held, Decimal(25500), dt.time(15, 30), SETTLE, RATES) == Decimal(
            "48.75"
        )

    def test_the_defaults_respect_the_hard_exit_ceiling(self) -> None:
        assert check_hard_exits(DEFAULT_OPTIONS_CONFIG, DEFAULT_CEILINGS) == ()
        late = dataclasses.replace(
            DEFAULT_OPTIONS_CONFIG,
            directional=dataclasses.replace(
                DEFAULT_OPTIONS_CONFIG.directional, hard_exit_time=dt.time(15, 5)
            ),
        )
        assert check_hard_exits(late, DEFAULT_CEILINGS) == ("O2",)
        assert isinstance(late, OptionsConfig)


class TestSlippageAndTheCostTest:
    def test_synthetic_half_spread(self) -> None:
        """``04`` §6.2: 1.5 % of premium, minimum ₹0.10."""
        assert synthetic_half_spread(Decimal(4), RATES) == Decimal("0.10")
        assert synthetic_half_spread(Decimal(100), RATES) == Decimal("1.5")

    def test_expected_gains(self) -> None:
        """``04`` §6.4, one formula per sleeve."""
        assert expected_gain_o1(Decimal(2600), Decimal("0.50")) == 1300
        assert expected_gain_o2(Decimal(200), LOT, Decimal("0.60")) == Decimal(7800)
        assert expected_gain_o3(Decimal(100), Decimal(45), LOT, Decimal("0.80")) == Decimal(2275)

    def test_the_share_boundary(self) -> None:
        ok = cost_test(Decimal(260), Decimal(1300), Decimal("0.20"))
        assert ok.verdict is CostVerdict.OK
        assert ok.cost_share == Decimal("0.2")
        assert (
            cost_test(Decimal("260.01"), Decimal(1300), Decimal("0.20")).verdict
            is CostVerdict.REJECTED_COST
        )

    def test_no_gain_cannot_pay_a_cost(self) -> None:
        test = cost_test(Decimal(10), Decimal(0), Decimal("0.20"))
        assert test.verdict is CostVerdict.REJECTED_COST
        assert test.cost_share is None

    def test_the_rates_review_warning(self) -> None:
        assert RATES.reviewed_on == dt.date(2026, 9, 22)
        assert not rates_review_due(RATES, dt.date(2026, 12, 21))  # 90 days
        assert rates_review_due(RATES, dt.date(2026, 12, 22))

    def test_the_verified_rates(self) -> None:
        """OP0.1's verified numbers — the stale condor/frozen rates are not the defaults."""
        assert CostRates().stt_sell_premium_pct == Decimal("0.15")
        assert CostRates().stt_exercise_intrinsic_pct == Decimal("0.15")
        assert CostRates().exchange_txn_pct == Decimal("0.03553")
        assert CostRates().ipft_per_crore_inr == Decimal("0.01")
