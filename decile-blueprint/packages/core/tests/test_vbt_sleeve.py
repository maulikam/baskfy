"""The sleeve's own money (``docs/vbt/06`` VB5, DECISIONS-VB PACK.4).

Two claims, and both are about what this arithmetic **refuses** to see:

* The sleeve's equity is its own capital plus its own profit. A deposit, a Friday rebalance or a
  swing entry cannot move it, because none of them appears in any input.
* A resting limit has spoken for money it has not spent. ``cash_available`` subtracts it and
  ``cash`` does not, and the difference is what stops a fourth line being sized against a balance
  three limits have already claimed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from baskfy_core.vbt.sleeve import (
    OpenPositionValue,
    WorkingCommitment,
    sleeve_value,
)

LAKH = Decimal("1000000")


def position(
    *, instrument_id: int = 1, quantity: int = 1_000, entry: str = "96.24", mark: str = "110.00"
) -> OpenPositionValue:
    return OpenPositionValue(
        instrument_id=instrument_id,
        quantity_open=quantity,
        entry_avg=Decimal(entry),
        mark=Decimal(mark),
    )


def order(
    *, instrument_id: int = 2, quantity: int = 500, limit: str = "200.00"
) -> WorkingCommitment:
    return WorkingCommitment(
        instrument_id=instrument_id, quantity=quantity, limit_price=Decimal(limit)
    )


class TestAnUntouchedSleeve:
    def test_its_equity_is_its_capital(self) -> None:
        value = sleeve_value(
            capital_inr=LAKH, realised_inr=Decimal(0), open_positions=[], working_orders=[]
        )
        assert value.equity_inr == LAKH
        assert value.cash_inr == LAKH
        assert value.cash_available_inr == LAKH
        assert value.open_exposure_inr == Decimal(0)

    def test_a_sleeve_with_no_capital_has_no_equity(self) -> None:
        """`02` §3.4 — and `04` §5.1 then refuses every entry with ``NO_SLEEVE_CAPITAL``."""
        value = sleeve_value(
            capital_inr=Decimal(0), realised_inr=Decimal(0), open_positions=[], working_orders=[]
        )
        assert value.equity_inr == Decimal(0)


class TestAnOpenPosition:
    def test_cash_falls_by_what_it_cost_and_equity_by_nothing_at_the_entry(self) -> None:
        value = sleeve_value(
            capital_inr=LAKH,
            realised_inr=Decimal(0),
            open_positions=[position(entry="100.00", mark="100.00", quantity=1_000)],
            working_orders=[],
        )
        assert value.cash_inr == Decimal("900000")
        assert value.equity_inr == LAKH
        assert value.unrealised_inr == Decimal(0)

    def test_a_gain_shows_up_in_equity_and_not_in_cash(self) -> None:
        value = sleeve_value(
            capital_inr=LAKH,
            realised_inr=Decimal(0),
            open_positions=[position(entry="100.00", mark="120.00", quantity=1_000)],
            working_orders=[],
        )
        assert value.cash_inr == Decimal("900000")
        assert value.equity_inr == Decimal("1020000")
        assert value.unrealised_inr == Decimal("20000")

    def test_open_exposure_is_what_the_position_is_worth_now(self) -> None:
        value = sleeve_value(
            capital_inr=LAKH,
            realised_inr=Decimal(0),
            open_positions=[position(entry="100.00", mark="120.00", quantity=1_000)],
            working_orders=[],
        )
        assert value.open_exposure_inr == Decimal("120000")


class TestARestingLimit:
    def test_it_commits_cash_without_spending_it(self) -> None:
        """The distinction a naive implementation loses, and the one `04` §9.1 depends on."""
        value = sleeve_value(
            capital_inr=LAKH,
            realised_inr=Decimal(0),
            open_positions=[],
            working_orders=[order(quantity=500, limit="200.00")],
        )
        assert value.cash_inr == LAKH
        assert value.cash_available_inr == Decimal("900000")
        assert value.equity_inr == LAKH
        assert value.open_exposure_inr == Decimal("100000")

    def test_three_limits_cannot_be_spent_four_times(self) -> None:
        value = sleeve_value(
            capital_inr=LAKH,
            realised_inr=Decimal(0),
            open_positions=[],
            working_orders=[
                order(instrument_id=i, quantity=500, limit="200.00") for i in (1, 2, 3)
            ],
        )
        assert value.cash_available_inr == Decimal("700000")

    def test_cash_available_never_goes_negative(self) -> None:
        """A book cannot un-commit money, and a negative budget would size a line at zero rather
        than refusing it, which is a different sentence on the page."""
        value = sleeve_value(
            capital_inr=Decimal("100000"),
            realised_inr=Decimal(0),
            open_positions=[],
            working_orders=[order(quantity=5_000, limit="200.00")],
        )
        assert value.cash_available_inr == Decimal(0)


class TestRealisedProfit:
    def test_it_is_cash(self) -> None:
        value = sleeve_value(
            capital_inr=LAKH,
            realised_inr=Decimal("50000"),
            open_positions=[],
            working_orders=[],
        )
        assert value.cash_inr == Decimal("1050000")
        assert value.equity_inr == Decimal("1050000")

    def test_a_loss_is_cash_too(self) -> None:
        value = sleeve_value(
            capital_inr=LAKH,
            realised_inr=Decimal("-40000"),
            open_positions=[],
            working_orders=[],
        )
        assert value.equity_inr == Decimal("960000")


class TestTheWholeBook:
    def test_capital_realised_open_and_committed_together(self) -> None:
        value = sleeve_value(
            capital_inr=LAKH,
            realised_inr=Decimal("25000"),
            open_positions=[
                position(instrument_id=1, entry="100.00", mark="130.00", quantity=1_000),
                position(instrument_id=2, entry="50.00", mark="45.00", quantity=2_000),
            ],
            working_orders=[order(instrument_id=3, quantity=400, limit="250.00")],
        )
        # cash = 10,00,000 + 25,000 - (1,00,000 + 1,00,000)
        assert value.cash_inr == Decimal("825000")
        assert value.cash_available_inr == Decimal("725000")
        # equity = cash + (1,30,000 + 90,000)
        assert value.equity_inr == Decimal("1045000")
        assert value.open_exposure_inr == Decimal("320000")
        assert value.unrealised_inr == Decimal("20000")

    def test_every_rupee_quantizes_to_the_paise(self) -> None:
        """House rule 8 — money is stored rounded, so a page and a CSV cannot disagree."""
        value = sleeve_value(
            capital_inr=LAKH,
            realised_inr=Decimal("0.005"),
            open_positions=[position(entry="96.2412345", mark="110.1234567", quantity=7)],
            working_orders=[],
        ).quantize()
        for amount in (
            value.capital_inr,
            value.realised_inr,
            value.cost_of_open_inr,
            value.value_of_open_inr,
            value.committed_inr,
        ):
            assert amount == amount.quantize(Decimal("0.01"))


def test_money_is_never_a_float() -> None:
    value = sleeve_value(
        capital_inr=LAKH,
        realised_inr=Decimal(0),
        open_positions=[position()],
        working_orders=[order()],
    )
    for amount in (value.cash_inr, value.equity_inr, value.open_exposure_inr):
        assert isinstance(amount, Decimal)


@pytest.mark.parametrize("quantity", [0, 1, 10_000])
def test_a_position_of_any_size_values_consistently(quantity: int) -> None:
    one = position(entry="100.00", mark="110.00", quantity=quantity)
    assert one.value - one.cost == Decimal(10) * quantity
