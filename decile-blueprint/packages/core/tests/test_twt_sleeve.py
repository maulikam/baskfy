"""The sleeve's own money (``docs/twt/04`` §9).

Two claims are asserted here, and both are about what the sleeve **cannot** see: it never sizes
against the account, and it never owns what it did not buy. ``tw_position`` is the source of truth,
so a name in the broker's account that this sleeve never bought is invisible to the arithmetic — a
claim about absence, and a claim about absence is worth exactly as much as the test that checks it.
"""

from __future__ import annotations

from decimal import Decimal

from baskfy_core.twt.sleeve import MarkSource, OpenPositionValue, sleeve_value

CAPITAL = Decimal("2500000")


def held(
    *,
    instrument_id: int = 1,
    quantity: int = 100,
    entry: str = "100",
    mark: str = "120",
    source: MarkSource = MarkSource.SESSION_CLOSE,
) -> OpenPositionValue:
    return OpenPositionValue(
        instrument_id=instrument_id,
        quantity_open=quantity,
        entry_avg=Decimal(entry),
        mark=Decimal(mark),
        mark_source=source,
    )


class TestTheArithmetic:
    def test_an_empty_sleeve_is_worth_its_capital(self) -> None:
        value = sleeve_value(capital_inr=CAPITAL, realised_inr=Decimal(0), open_positions=[])
        assert value.equity_inr == CAPITAL
        assert value.cash_available_inr == CAPITAL
        assert value.open_exposure_inr == Decimal(0)

    def test_equity_is_capital_plus_realised_plus_the_marked_value_of_the_open(self) -> None:
        value = sleeve_value(
            capital_inr=CAPITAL,
            realised_inr=Decimal("50000"),
            open_positions=[held(quantity=1000, entry="100", mark="120")],
        )
        assert value.cost_of_open_inr == Decimal("100000")
        assert value.value_of_open_inr == Decimal("120000")
        assert value.cash_inr == CAPITAL + Decimal("50000") - Decimal("100000")
        assert value.equity_inr == Decimal("2570000")
        assert value.unrealised_inr == Decimal("20000")

    def test_cash_available_is_equity_less_the_value_of_the_open_positions(self) -> None:
        """``04`` §9.2, and the identity worth stating rather than hiding: with no working orders
        to reserve against, cash available **is** cash. VBT-1 has to subtract its committed limits
        and this sleeve does not, which is the difference between a book that bids and waits and a
        book that takes the open."""
        value = sleeve_value(
            capital_inr=CAPITAL,
            realised_inr=Decimal(0),
            open_positions=[held(quantity=1000)],
        )
        assert value.cash_available_inr == value.cash_inr
        assert value.cash_available_inr == value.equity_inr - value.value_of_open_inr

    def test_cash_available_is_never_negative(self) -> None:
        value = sleeve_value(
            capital_inr=Decimal("10000"),
            realised_inr=Decimal("-50000"),
            open_positions=[held(quantity=100)],
        )
        assert value.cash_available_inr == Decimal(0)

    def test_money_is_stored_to_the_paise(self) -> None:
        value = sleeve_value(
            capital_inr=Decimal("2500000.126"),
            realised_inr=Decimal("0.004"),
            open_positions=[],
        ).quantize()
        assert value.capital_inr == Decimal("2500000.13")
        assert value.realised_inr == Decimal("0.00")


class TestASleeveAtZeroPlansNothing:
    """``04`` §9.3. ``tw_config.sleeve_capital_inr`` is seeded at zero and **the run never sets
    it** (root ``CLAUDE.md`` safety rails)."""

    def test_no_capital_is_no_equity(self) -> None:
        value = sleeve_value(capital_inr=Decimal(0), realised_inr=Decimal(0), open_positions=[])
        assert value.has_capital is False

    def test_capital_is_capital(self) -> None:
        value = sleeve_value(capital_inr=CAPITAL, realised_inr=Decimal(0), open_positions=[])
        assert value.has_capital is True


class TestItNeverSellsWhatItDidNotBuy:
    def test_a_holding_the_sleeve_never_bought_is_not_in_its_equity(self) -> None:
        """The list of open positions comes from ``tw_position`` and from nothing else. There is no
        parameter on this function through which the account's holdings could arrive — which is the
        strongest form the assertion can take."""
        mine = sleeve_value(
            capital_inr=CAPITAL, realised_inr=Decimal(0), open_positions=[held(quantity=100)]
        )
        assert mine.value_of_open_inr == Decimal("12000")
        assert "holdings" not in sleeve_value.__code__.co_varnames
        assert "account" not in sleeve_value.__code__.co_varnames


class TestTheMarkSaysWhereItCameFrom:
    """``04`` §9.1: a position with no bar on the session falls back to its last close **and says
    so** in the plan's detail."""

    def test_a_session_close_is_not_stale(self) -> None:
        assert held(source=MarkSource.SESSION_CLOSE).is_stale_mark is False

    def test_a_last_known_close_is(self) -> None:
        assert held(source=MarkSource.LAST_KNOWN_CLOSE).is_stale_mark is True

    def test_a_name_that_has_not_printed_since_the_fill_is_marked_at_the_entry(self) -> None:
        position = held(entry="100", mark="100", source=MarkSource.ENTRY)
        assert position.is_stale_mark is True
        assert position.value == position.cost
