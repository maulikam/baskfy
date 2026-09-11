"""Ten equal slots, every cap of ``docs/twt/04`` §6.2 **in order**, and §6.4's half size.

``docs/twt/06`` TW1's acceptance criterion. Each test moves one input and names the cap it expects
to bind, because "which cap bound" is part of the answer a plan line has to give: a person reading
"412 shares" should be able to see whether that is the slot or a verdict about the name's
liquidity.

The numbers are the sleeve's own: ₹25 lakh over ten slots is a ₹2.5 lakh line, which is the size
``04`` §3.5's whole argument about the ₹5 crore liquidity floor is made at.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, SizingConfig
from baskfy_core.twt.sizing import (
    SizeCap,
    SizedEntry,
    SizeRefusal,
    first_live_multiplier,
    size_entry,
)

SIZING = DEFAULT_TWT_CONFIG.sizing
#: The sleeve's own capital (``docs/twt/02``: ₹25 lakh, seeded at 0 and set by Maulik).
EQUITY = Decimal("2500000")
#: A tenth of it. Every cap below is compared against this number.
SLOT = Decimal("250000")
PRICE = Decimal("100")
STOP = Decimal("80")
#: ₹50 crore a day: 1 % of it is ₹50 lakh and the turnover cap cannot bind at this size.
DEEP = Decimal("500000000")


def size(  # noqa: PLR0913 - the test's own knobs mirror the function's
    *,
    equity: Decimal = EQUITY,
    cash: Decimal = EQUITY,
    price: Decimal = PRICE,
    stop: Decimal = STOP,
    turnover: Decimal | None = DEEP,
    config: SizingConfig = SIZING,
    multiplier: Decimal = Decimal(1),
) -> SizedEntry:
    return size_entry(
        equity=equity,
        cash_available=cash,
        entry_price=price,
        stop_price=stop,
        turnover_avg_inr=turnover,
        config=config,
        slot_multiplier=multiplier,
    )


class TestEqualWeight:
    """``04`` §6.1: the target value of one new line is ``sleeve_equity / max_slots``."""

    def test_ten_slots_is_a_tenth_of_the_sleeve(self) -> None:
        sized = size()
        assert SIZING.max_slots == 10
        assert sized.value_inr == SLOT
        assert sized.quantity == 2_500
        assert sized.cap is SizeCap.SLOT
        assert sized.caps_applied == ()

    def test_the_quantity_is_an_integer_number_of_shares_rounded_down(self) -> None:
        sized = size(price=Decimal("99.70"))
        assert sized.quantity == 2_507
        assert sized.value_inr == Decimal("249947.90")

    def test_the_position_percentage_is_reported_against_the_sleeve_not_the_account(self) -> None:
        assert size().position_pct == Decimal("10.00")

    def test_the_risk_is_the_distance_to_the_stop(self) -> None:
        assert size().risk_inr == (PRICE - STOP) * 2_500


class TestTheCapsInOrder:
    """``04`` §6.2's table, applied per-position ceiling -> liquidity -> cash -> floor."""

    def test_the_per_position_ceiling_binds_when_equity_has_drifted(self) -> None:
        """12.5 % is above the slot size, so it binds only when the slot is larger than it — which
        is what a book run at fewer slots than the strategy's ten would be."""
        four = SizingConfig(max_slots=4)
        sized = size(config=four)
        assert sized.cap is SizeCap.POSITION_PCT
        assert sized.value_inr == EQUITY * SIZING.max_position_pct / Decimal(100)
        assert sized.caps_applied == (SizeCap.POSITION_PCT,)

    def test_the_turnover_cap_binds_on_a_two_crore_name_at_twenty_five_lakh(self) -> None:
        """``04`` §3.5's argument, as a test. 1 % of ₹2 crore is ₹2 lakh against a ₹2.5 lakh line:
        a floor below the cap means the plan is routinely sized by the cap rather than by the
        strategy, which is why this sleeve ships at ₹5 crore."""
        sized = size(turnover=Decimal("20000000"))
        assert sized.cap is SizeCap.TURNOVER
        assert sized.value_inr == Decimal("200000")
        assert sized.turnover_capped is True

    def test_the_turnover_cap_does_not_bind_on_a_fifty_crore_name(self) -> None:
        sized = size(turnover=DEEP)
        assert sized.cap is SizeCap.SLOT
        assert sized.turnover_capped is False

    def test_a_name_with_no_turnover_average_is_not_capped_by_one(self) -> None:
        """The liquidity *floor* is the plan's job (``04`` §10.1's ``BELOW_LIQUIDITY_FLOOR``);
        sizing does not invent a ceiling out of a missing window."""
        assert size(turnover=None).cap is SizeCap.SLOT

    def test_the_sleeves_cash_binds_last(self) -> None:
        sized = size(cash=Decimal("150000"))
        assert sized.cap is SizeCap.CASH
        assert sized.value_inr == Decimal("150000")

    def test_every_cap_that_bound_is_recorded_in_the_order_it_was_applied(self) -> None:
        """A line can be capped twice, and a note that named only the last one would be telling a
        person the name is illiquid when the sleeve is also out of cash."""
        sized = size(
            config=SizingConfig(max_slots=4),
            turnover=Decimal("20000000"),
            cash=Decimal("100000"),
        )
        assert sized.caps_applied == (SizeCap.POSITION_PCT, SizeCap.TURNOVER, SizeCap.CASH)
        assert sized.cap is SizeCap.CASH
        assert sized.value_inr == Decimal("100000")

    def test_a_cap_that_does_not_lower_the_target_is_not_recorded(self) -> None:
        sized = size(turnover=Decimal("20000000"), cash=EQUITY)
        assert sized.caps_applied == (SizeCap.TURNOVER,)


class TestTheFloorAndTheRefusals:
    def test_a_line_below_the_minimum_trade_value_is_skipped(self) -> None:
        sized = size(turnover=Decimal("900000"))
        assert sized.placed is False
        assert sized.refusal is SizeRefusal.BELOW_MIN_TRADE_VALUE
        assert sized.quantity == 0

    def test_a_line_exactly_at_the_minimum_trade_value_is_placed(self) -> None:
        """``04`` §6.2 reads *below* the floor, so ₹10,000 clears it."""
        sized = size(cash=SIZING.min_trade_value_inr)
        assert sized.value_inr == SIZING.min_trade_value_inr
        assert sized.placed is True

    def test_a_sleeve_with_no_capital_plans_nothing(self) -> None:
        """``04`` §9.3: a sleeve at ₹0 plans nothing. ``tw_config.sleeve_capital_inr`` is seeded at
        zero and the run never sets it."""
        sized = size(equity=Decimal(0))
        assert sized.refusal is SizeRefusal.NO_SLEEVE_CAPITAL

    def test_a_stop_that_is_not_below_the_entry_is_refused(self) -> None:
        """``04`` §7.1. It cannot arise at 20 % and the check is there because a ceiling edit
        could make it arise."""
        assert size(stop=PRICE).refusal is SizeRefusal.STOP_NOT_BELOW_ENTRY
        assert size(stop=PRICE + Decimal(1)).refusal is SizeRefusal.STOP_NOT_BELOW_ENTRY
        assert size(stop=Decimal(0)).refusal is SizeRefusal.STOP_NOT_BELOW_ENTRY

    def test_negative_cash_is_read_as_none_rather_than_as_a_negative_budget(self) -> None:
        assert size(cash=Decimal("-50000")).refusal is SizeRefusal.BELOW_MIN_TRADE_VALUE


class TestHalfSizeForTheFirstTenLiveEntries:
    """``04`` §6.4. It counts **entries, not sessions**: this book enters about eighteen times a
    year, and a five-session allowance would be spent by a quiet week."""

    def test_the_multiplier_is_a_half_while_the_counter_runs_and_execution_is_enabled(self) -> None:
        assert SIZING.first_live_entries == 10
        assert (
            first_live_multiplier(entries_left=10, execution_enabled=True, config=SIZING)
            == SIZING.risk_multiplier_first_live
        )

    def test_a_dry_run_plan_is_full_size(self) -> None:
        """Half size is a live-money discipline, and a paper plan that is not the plan is not a
        rehearsal."""
        assert first_live_multiplier(
            entries_left=10, execution_enabled=False, config=SIZING
        ) == Decimal(1)

    def test_the_eleventh_live_entry_is_full_size(self) -> None:
        assert first_live_multiplier(
            entries_left=0, execution_enabled=True, config=SIZING
        ) == Decimal(1)

    def test_half_size_is_applied_before_every_cap_so_the_line_shown_is_the_line_sent(self) -> None:
        """The discriminating case. At a ₹2 crore name the turnover cap is ₹2 lakh and the half
        slot is ₹1.25 lakh: applied **first**, the slot is what binds and the line is ₹1.25 lakh.
        Applied to the quantity at send time instead, the page would have shown ₹2 lakh and the
        broker would have received ₹1 lakh."""
        sized = size(turnover=Decimal("20000000"), multiplier=Decimal("0.5"))
        assert sized.value_inr == SLOT / 2
        assert sized.cap is SizeCap.SLOT
        assert sized.caps_applied == ()

    @pytest.mark.parametrize("multiplier", [Decimal(1), Decimal("0.5")])
    def test_the_refusals_see_the_real_size(self, multiplier: Decimal) -> None:
        """A half line that cannot clear the ₹10,000 floor is skipped at the size it would be
        sent at, not at the size it would have been. A ₹1.5 lakh sleeve has a ₹15,000 slot and a
        ₹7,500 half slot, so the floor is between them."""
        small = Decimal("150000")
        sized = size(equity=small, cash=small, multiplier=multiplier)
        assert sized.placed is (multiplier == Decimal(1))
        if not sized.placed:
            assert sized.refusal is SizeRefusal.BELOW_MIN_TRADE_VALUE
