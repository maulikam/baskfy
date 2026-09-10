"""Ten equal slots, and the cap that bound named (``docs/vbt/04`` §5).

Every test here changes one budget and asserts both the quantity **and** which cap the answer
attributes it to, because "412 shares" and "412 shares, capped by 1% of the name's 20-day
turnover" are different sentences on a page a person is about to act on.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, SizingConfig
from baskfy_core.vbt.sizing import (
    SizeCap,
    SizedEntry,
    SizeRefusal,
    first_live_multiplier,
    size_entry,
)

SIZING = DEFAULT_VBT_CONFIG.sizing
LAKH = Decimal("1000000")  # the study's ₹10 lakh sleeve


def size(  # noqa: PLR0913 - the same keywords the function takes
    *,
    equity: Decimal = LAKH,
    cash: Decimal = LAKH,
    limit: Decimal = Decimal("100.00"),
    stop: Decimal = Decimal("88.00"),
    turnover: Decimal | None = None,
    config: SizingConfig = SIZING,
    multiplier: Decimal = Decimal(1),
) -> SizedEntry:
    return size_entry(
        equity=equity,
        cash_available=cash,
        limit_price=limit,
        stop_price=stop,
        turnover_avg_inr=turnover,
        config=config,
        slot_multiplier=multiplier,
    )


class TestTheOrdinaryAnswer:
    def test_ten_slots_means_a_tenth_of_the_sleeve(self) -> None:
        sized = size()
        assert sized.quantity == 1_000  # ₹1,00,000 / ₹100
        assert sized.cap is SizeCap.SLOT
        assert sized.value_inr == Decimal("100000.00")
        assert sized.position_pct == Decimal("10.00")

    def test_the_risk_is_the_distance_to_the_stop(self) -> None:
        sized = size()
        assert sized.risk_inr == Decimal("12000.00")

    def test_the_quantity_is_floored_never_rounded_up(self) -> None:
        sized = size(limit=Decimal("30.00"), stop=Decimal("26.40"))
        assert sized.quantity == 3_333  # ₹1,00,000 / ₹30 = 3,333.33
        assert sized.value_inr == Decimal("99990.00")


class TestEachCapInTurn:
    def test_the_position_cap_binds_when_the_slot_would_be_larger(self) -> None:
        """A sleeve of five slots would put 20% in a name; ``max_position_pct`` says 12.5%."""
        config = SizingConfig(max_slots=5)
        sized = size(config=config)
        assert sized.cap is SizeCap.POSITION_PCT
        assert sized.value_inr == Decimal("125000.00")

    def test_cash_binds_when_earlier_lines_have_spent_it(self) -> None:
        sized = size(cash=Decimal("40000"))
        assert sized.cap is SizeCap.CASH
        assert sized.quantity == 400

    def test_the_turnover_cap_binds_on_a_thin_name(self) -> None:
        """1% of ₹50 lakh is ₹50,000 — half a slot, and the reason the page says so."""
        sized = size(turnover=Decimal("5000000"))
        assert sized.cap is SizeCap.TURNOVER
        assert sized.quantity == 500

    def test_the_turnover_cap_does_not_bind_at_ten_lakh_in_a_two_crore_name(self) -> None:
        """STRATEGY §3's claim, as a test: the liquidity cap binds nowhere at ₹10 lakh."""
        sized = size(turnover=Decimal("20000000"))
        assert sized.cap is SizeCap.SLOT

    def test_it_does_bind_at_a_crore(self) -> None:
        """...and it will at ₹1 crore, which is why it is in from day one."""
        sized = size(
            equity=Decimal("10000000"), cash=Decimal("10000000"), turnover=Decimal("20000000")
        )
        assert sized.cap is SizeCap.TURNOVER
        assert sized.value_inr <= Decimal("200000.00")

    def test_an_unknown_turnover_simply_does_not_cap(self) -> None:
        assert size(turnover=None).cap is SizeCap.SLOT


class TestTheRefusals:
    def test_a_sleeve_with_no_capital_plans_nothing(self) -> None:
        """`02` §3.4: ``vb_config.sleeve_capital_inr`` is seeded at 0 and stays there until
        Maulik writes his risk decision."""
        sized = size(equity=Decimal(0))
        assert sized.quantity == 0
        assert sized.refusal is SizeRefusal.NO_SLEEVE_CAPITAL

    def test_a_stop_at_or_above_the_limit_is_refused(self) -> None:
        assert size(stop=Decimal("100.00")).refusal is SizeRefusal.STOP_NOT_BELOW_ENTRY
        assert size(stop=Decimal("120.00")).refusal is SizeRefusal.STOP_NOT_BELOW_ENTRY

    def test_a_trade_below_the_floor_is_refused_not_shrunk(self) -> None:
        sized = size(cash=Decimal("9000"))
        assert sized.quantity == 0
        assert sized.refusal is SizeRefusal.BELOW_MIN_TRADE_VALUE

    def test_a_turnover_ceiling_too_small_to_fund_the_floor_blames_the_name(self) -> None:
        """A liquidity verdict about the name, not about the sleeve's cash — the page should
        say which, because they call for different actions."""
        sized = size(turnover=Decimal("500000"))
        assert sized.refusal is SizeRefusal.TURNOVER_CAP

    def test_a_refusal_carries_no_quantity_and_no_value(self) -> None:
        sized = size(equity=Decimal(0))
        assert (sized.quantity, sized.value_inr, sized.risk_inr) == (0, Decimal(0), Decimal(0))
        assert sized.placed is False


class TestTheFirstLiveSessions:
    def test_half_a_slot_while_the_countdown_runs_and_execution_is_real(self) -> None:
        multiplier = first_live_multiplier(sessions_left=5, execution_enabled=True, config=SIZING)
        assert multiplier == Decimal("0.5")
        assert size(multiplier=multiplier).quantity == 500

    def test_a_paper_plan_is_full_size(self) -> None:
        """`04` §5.4 — the paper record rehearses the rules at the size the rules describe."""
        assert first_live_multiplier(
            sessions_left=5, execution_enabled=False, config=SIZING
        ) == Decimal(1)

    def test_the_sixth_live_session_is_full_size(self) -> None:
        assert first_live_multiplier(
            sessions_left=0, execution_enabled=True, config=SIZING
        ) == Decimal(1)

    def test_the_halving_happens_before_every_cap(self) -> None:
        """So the line shown is the line sent, and the caps see the real size."""
        sized = size(multiplier=Decimal("0.5"), cash=Decimal("60000"))
        assert sized.cap is SizeCap.SLOT
        assert sized.quantity == 500


def test_money_never_becomes_a_float() -> None:
    sized = size()
    for value in (sized.limit_price, sized.stop_price, sized.value_inr, sized.risk_inr):
        assert isinstance(value, Decimal)


def test_the_documented_defaults_are_the_ones_in_force() -> None:
    assert (SIZING.max_slots, SIZING.max_position_pct) == (10, 12.5)
    assert SIZING.max_position_vs_turnover == pytest.approx(0.01)
    assert SIZING.min_trade_value_inr == pytest.approx(10_000.0)
