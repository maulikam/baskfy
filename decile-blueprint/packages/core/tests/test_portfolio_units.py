"""The spec for :mod:`baskfy_core.portfolio_units` (tree 5, leaf A3).

House rule 2: these assert the *spec* — the arithmetic a sleeve is entitled to rely on — not
whatever the implementation happens to do today. The four promises being pinned down are:

1. units are whole and the allocation is affordable at the prices given;
2. ``sum(units x price) + remainder == capital`` exactly, in ``Decimal``;
3. a name that cannot be priced stops the allocation instead of quietly becoming zero;
4. held units and target units are both reported, so a sleeve can show drift.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from decimal import Decimal

import pytest

from baskfy_core import portfolio_units as module
from baskfy_core.portfolio_units import (
    FULL_WEIGHT,
    InvalidPriceError,
    MissingPriceError,
    PriceRefusedError,
    UnitAllocation,
    allocate_units,
    units_affordable,
)

# A three-name sleeve whose weights and prices divide badly on purpose: 40/35/25 of ₹1,00,000
# against prices that leave a different leftover on every line.
CAPITAL = Decimal("100000.00")
WEIGHTS = {"CUPID": Decimal("0.4000"), "TATAMOTORS": Decimal("0.3500"), "INFY": Decimal("0.2500")}
PRICES: dict[str, Decimal | None] = {
    "CUPID": Decimal("317.45"),
    "TATAMOTORS": Decimal("1042.10"),
    "INFY": Decimal("1533.60"),
}


def sleeve(
    *,
    capital: Decimal | None = None,
    weights: Mapping[str, Decimal] | None = None,
    prices: Mapping[str, Decimal | None] | None = None,
    held: Mapping[str, int | Decimal] | None = None,
) -> UnitAllocation:
    """The reference sleeve, with named overrides — keeps each test's deviation visible."""
    return allocate_units(
        capital=CAPITAL if capital is None else capital,
        weights=WEIGHTS if weights is None else weights,
        prices=PRICES if prices is None else prices,
        held=held,
    )


def refuse(**arguments: object) -> UnitAllocation:
    """Call the allocator with a deliberately wrong *type*.

    House rule 3 forbids per-line type suppressions, and the annotations already say what the
    types are — so a test that a wrong type is refused at *runtime* has to reach the function
    through a signature mypy does not re-check. That is what this is for, and it is why it is
    one named helper rather than a suppression comment scattered through the file.
    """
    call: Callable[..., UnitAllocation] = allocate_units
    return call(**arguments)


class TestUnitsAreWholeAndAffordable:
    def test_units_are_whole_numbers_not_fractions(self) -> None:
        """You cannot buy 3.4 shares, so a unit count is an ``int`` and never a ``Decimal``."""
        for row in sleeve().rows:
            assert isinstance(row.target_units, int)
            assert not isinstance(row.target_units, bool)

    def test_the_whole_allocation_is_affordable_at_the_given_prices(self) -> None:
        result = sleeve()
        assert result.deployed <= result.capital

    def test_no_single_name_is_given_units_its_own_budget_cannot_pay_for(self) -> None:
        """Affordability binds per name, not only in aggregate."""
        result = sleeve()
        for row in result.rows:
            assert row.target_value <= result.capital * row.weight

    def test_one_more_unit_of_any_name_would_exceed_its_budget(self) -> None:
        """Rounded down, but not further down than necessary: the count is maximal."""
        result = sleeve()
        for row in result.rows:
            budget = result.capital * row.weight
            assert (row.target_units + 1) * row.price > budget

    def test_units_affordable_rounds_down_and_never_up(self) -> None:
        assert units_affordable(budget=Decimal("1000.00"), price=Decimal("300.00")) == 3
        assert units_affordable(budget=Decimal("899.99"), price=Decimal("300.00")) == 2
        assert units_affordable(budget=Decimal("900.00"), price=Decimal("300.00")) == 3

    def test_units_affordable_stays_exact_past_the_decimal_context(self) -> None:
        """A quotient carried past 28 significant digits must not round up into a unit.

        ``Decimal`` division obeys a 28-digit context, so ``9.999…/1`` *rounds to 10* and a naive
        floor of it would hand out one unit the budget cannot pay for. The first assertion pins
        that hazard so nobody removes the exact-integer path thinking it is decoration.
        """
        budget = Decimal("9." + "9" * 30)
        assert int(budget / Decimal(1)) == 10, "the hazard this function exists to avoid"
        assert units_affordable(budget=budget, price=Decimal(1)) == 9

    def test_the_allocation_stays_affordable_past_the_default_decimal_context(self) -> None:
        """``capital x weight`` must not round *up* in its 28th digit and overspend.

        Nobody holds 10^27 rupees, and no equity prints at 5 paise. Both are chosen to push the
        products past ``Decimal``'s 28-significant-digit default, where a rounded budget buys one
        unit too many and the reconciliation identity stops holding. "Affordable" is this
        module's money-safety promise, and a promise that holds only for typical magnitudes is a
        promise with an unstated footnote.
        """
        capital = Decimal("9" * 27 + ".99")
        result = allocate_units(
            capital=capital,
            weights={"A": Decimal("0.4000"), "B": Decimal("0.6000")},
            prices={"A": Decimal("0.05"), "B": Decimal("0.05")},
        )
        assert result.deployed <= capital
        assert result.remainder >= 0
        assert result.balances

    def test_a_budget_smaller_than_one_unit_buys_nothing(self) -> None:
        assert units_affordable(budget=Decimal("100.00"), price=Decimal("317.45")) == 0

    def test_a_negative_budget_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            units_affordable(budget=Decimal("-1.00"), price=Decimal("10.00"))


class TestTheRemainderReconciles:
    def test_units_times_price_plus_the_remainder_equals_the_capital(self) -> None:
        result = sleeve()
        priced = sum((row.target_units * row.price for row in result.rows), Decimal(0))
        assert priced + result.remainder == result.capital
        assert result.balances

    def test_the_stated_deployed_total_is_the_sum_of_the_rows(self) -> None:
        result = sleeve()
        assert result.deployed == sum((row.target_value for row in result.rows), Decimal(0))

    def test_the_remainder_is_never_negative(self) -> None:
        assert sleeve().remainder >= 0

    def test_the_remainder_carries_the_paise_rather_than_losing_them(self) -> None:
        """Rounding money away is the defect; the remainder is the honest place for it."""
        result = sleeve(capital=Decimal("100000.37"))
        assert result.deployed + result.remainder == Decimal("100000.37")
        assert result.remainder % 1 != 0, "the paise survived into the remainder"

    def test_capital_the_weights_left_unallocated_lands_in_the_remainder(self) -> None:
        held_back = {"CUPID": Decimal("0.5000"), "INFY": Decimal("0.3000")}
        result = allocate_units(capital=CAPITAL, weights=held_back, prices=PRICES)
        assert result.weight_total == Decimal("0.8000")
        assert result.remainder >= CAPITAL * Decimal("0.2000")
        assert result.balances

    def test_reconciliation_holds_at_crore_scale(self) -> None:
        result = sleeve(capital=Decimal("100000000.00"))
        assert result.balances
        assert result.deployed <= result.capital

    def test_every_reconciled_total_is_a_decimal(self) -> None:
        result = sleeve()
        for value in (result.capital, result.deployed, result.remainder, result.held_value):
            assert isinstance(value, Decimal)


class TestAMissingPriceIsRefusedLoudly:
    def test_a_missing_price_raises_rather_than_treating_the_name_as_zero(self) -> None:
        with pytest.raises(MissingPriceError) as raised:
            sleeve(prices={"CUPID": PRICES["CUPID"], "TATAMOTORS": PRICES["TATAMOTORS"]})
        assert raised.value.symbols == ("INFY",)

    def test_a_none_entry_is_a_missing_price_and_not_a_zero(self) -> None:
        with pytest.raises(MissingPriceError) as raised:
            sleeve(prices={**PRICES, "INFY": None})
        assert raised.value.symbols == ("INFY",)

    def test_every_unpriced_name_is_named_in_one_raise(self) -> None:
        """A caller discovering them one retry at a time is a worse loop than fixing them all."""
        with pytest.raises(MissingPriceError) as raised:
            sleeve(prices={"CUPID": PRICES["CUPID"]})
        assert set(raised.value.symbols) == {"INFY", "TATAMOTORS"}
        for symbol in ("INFY", "TATAMOTORS"):
            assert symbol in str(raised.value)

    def test_an_unpriced_name_is_never_silently_dropped_from_the_allocation(self) -> None:
        """Dropping it is not harmless: the sleeve's total would be wrong by that name's worth."""
        without = allocate_units(
            capital=CAPITAL,
            weights={k: v for k, v in WEIGHTS.items() if k != "INFY"},
            prices=PRICES,
        )
        assert without.deployed != sleeve().deployed
        with pytest.raises(MissingPriceError):
            sleeve(prices={**PRICES, "INFY": None})

    def test_an_unpriced_held_name_is_refused_too(self) -> None:
        """A held-only row still reports a value, so it still needs a price."""
        with pytest.raises(MissingPriceError) as raised:
            sleeve(held={"ITC": 12})
        assert raised.value.symbols == ("ITC",)

    def test_the_missing_price_refusal_is_a_value_error_an_api_layer_renders(self) -> None:
        assert issubclass(MissingPriceError, PriceRefusedError)
        assert issubclass(PriceRefusedError, ValueError)

    def test_an_unpriced_name_produces_no_partial_result(self) -> None:
        with pytest.raises(PriceRefusedError):
            sleeve(prices={**PRICES, "INFY": Decimal("0.00")})


class TestDecimalEndToEndAndFloatsRefused:
    def test_a_float_price_is_rejected_rather_than_coerced(self) -> None:
        with pytest.raises(TypeError) as raised:
            refuse(capital=CAPITAL, weights=WEIGHTS, prices={**PRICES, "INFY": 1533.60})
        assert "house rule 9" in str(raised.value)

    def test_a_float_capital_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="house rule 9"):
            refuse(capital=100000.0, weights=WEIGHTS, prices=PRICES)

    def test_a_float_weight_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="house rule 9"):
            refuse(capital=CAPITAL, weights={**WEIGHTS, "INFY": 0.25}, prices=PRICES)

    def test_a_float_holding_quantity_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="house rule 9"):
            refuse(capital=CAPITAL, weights=WEIGHTS, prices=PRICES, held={"CUPID": 12.0})

    def test_the_refusal_explains_why_rather_than_only_that(self) -> None:
        """A coercion bug is fixed at the boundary, so the message points there."""
        with pytest.raises(TypeError) as raised:
            refuse(capital=100000.0, weights=WEIGHTS, prices=PRICES)
        message = str(raised.value)
        assert "Decimal" in message
        assert "boundary" in message

    def test_a_decimal_quantity_from_a_numeric_column_is_accepted(self) -> None:
        """``portfolio_holding.qty`` is ``numeric``; a caller should not have to convert it."""
        result = sleeve(held={"CUPID": Decimal("125")})
        row = next(r for r in result.rows if r.symbol == "CUPID")
        assert row.held_units == 125
        assert isinstance(row.held_units, int)

    def test_a_fractional_holding_is_refused_because_equities_are_whole(self) -> None:
        with pytest.raises(ValueError, match="whole number of units"):
            sleeve(held={"CUPID": Decimal("125.5")})

    def test_a_boolean_is_not_a_quantity(self) -> None:
        with pytest.raises(TypeError, match="bool"):
            sleeve(held={"CUPID": True})

    def test_every_price_and_value_on_a_row_is_a_decimal(self) -> None:
        for row in sleeve(held={"CUPID": 100}).rows:
            for value in (row.weight, row.price, row.target_value, row.held_value):
                assert isinstance(value, Decimal)

    def test_a_string_price_is_refused_rather_than_parsed(self) -> None:
        with pytest.raises(TypeError, match="must be a Decimal"):
            refuse(capital=CAPITAL, weights=WEIGHTS, prices={**PRICES, "INFY": "1533.60"})


class TestHeldVersusTargetUnits:
    def test_both_held_and_target_units_are_reported(self) -> None:
        """The gap this leaf closes: a sleeve can finally show what it actually holds."""
        result = sleeve(held={"CUPID": 100, "INFY": 16})
        by_symbol = {row.symbol: row for row in result.rows}
        assert by_symbol["CUPID"].held_units == 100
        assert by_symbol["INFY"].held_units == 16
        assert all(row.target_units >= 0 for row in result.rows)

    def test_drift_is_target_minus_held(self) -> None:
        result = sleeve(held={"CUPID": 100})
        row = next(r for r in result.rows if r.symbol == "CUPID")
        assert row.drift_units == row.target_units - 100
        assert row.drift_value == row.target_value - row.held_value

    def test_holding_more_than_the_target_drifts_negative(self) -> None:
        result = sleeve(held={"CUPID": 10_000})
        row = next(r for r in result.rows if r.symbol == "CUPID")
        assert row.drift_units < 0

    def test_holding_nothing_makes_the_whole_target_the_drift(self) -> None:
        result = sleeve()
        for row in result.rows:
            assert row.held_units == 0
            assert row.drift_units == row.target_units

    def test_a_name_held_but_no_longer_targeted_keeps_a_row(self) -> None:
        """Dropping it would understate what is held — the failure this module ends."""
        result = sleeve(held={"ITC": 40}, prices={**PRICES, "ITC": Decimal("415.00")})
        row = next(r for r in result.rows if r.symbol == "ITC")
        assert row.weight == 0
        assert row.target_units == 0
        assert row.held_units == 40
        assert row.held_value == Decimal("16600.00")

    def test_held_value_marks_holdings_at_the_same_prices_as_the_target(self) -> None:
        result = sleeve(held={"CUPID": 100})
        assert result.held_value == Decimal("317.45") * 100
        assert result.drift_value == result.deployed - result.held_value

    def test_only_the_names_that_moved_are_listed_as_drifting(self) -> None:
        on_target = sleeve()
        matched = {row.symbol: row.target_units for row in on_target.rows}
        assert sleeve(held=matched).drifting == ()
        assert "CUPID" in sleeve(held={**matched, "CUPID": 1}).drifting

    def test_a_sleeve_holding_exactly_its_target_still_reports_the_remainder(self) -> None:
        target = sleeve()
        matched = {row.symbol: row.target_units for row in target.rows}
        result = sleeve(held=matched)
        assert result.held_value == result.deployed
        assert result.balances


class TestEdgesThatMustNotCrash:
    def test_zero_capital_yields_zero_units_and_a_zero_remainder(self) -> None:
        result = sleeve(capital=Decimal("0.00"))
        assert [row.target_units for row in result.rows] == [0, 0, 0]
        assert result.deployed == 0
        assert result.remainder == 0
        assert result.balances

    def test_zero_capital_still_reports_what_is_held(self) -> None:
        """An unfunded sleeve is an ordinary state, and its holdings are the informative half."""
        result = sleeve(capital=Decimal("0.00"), held={"CUPID": 100})
        row = next(r for r in result.rows if r.symbol == "CUPID")
        assert row.held_units == 100
        assert row.drift_units == -100

    def test_a_zero_weight_keeps_its_row_at_zero_units(self) -> None:
        weights = {**WEIGHTS, "INFY": Decimal("0.0000")}
        result = allocate_units(capital=CAPITAL, weights=weights, prices=PRICES)
        row = next(r for r in result.rows if r.symbol == "INFY")
        assert row.target_units == 0
        assert row.symbol in result.symbols, "a zero weight is not a reason to drop a name"

    def test_a_single_name_sleeve_takes_the_whole_capital(self) -> None:
        result = allocate_units(
            capital=Decimal("100000.00"),
            weights={"CUPID": FULL_WEIGHT},
            prices={"CUPID": Decimal("317.45")},
        )
        assert len(result.rows) == 1
        assert result.rows[0].target_units == 315
        assert result.deployed == Decimal("317.45") * 315
        assert result.balances

    def test_a_zero_price_is_refused_rather_than_dividing_by_zero(self) -> None:
        with pytest.raises(InvalidPriceError) as raised:
            sleeve(prices={**PRICES, "INFY": Decimal("0.00")})
        assert raised.value.symbols == ("INFY",)

    def test_a_negative_price_is_refused(self) -> None:
        with pytest.raises(InvalidPriceError):
            sleeve(prices={**PRICES, "INFY": Decimal("-1.00")})

    def test_weights_that_sum_below_one_are_accepted_as_a_cash_decision(self) -> None:
        result = allocate_units(
            capital=CAPITAL, weights={"CUPID": Decimal("0.5000")}, prices=PRICES
        )
        assert result.weight_total == Decimal("0.5000")
        assert result.balances

    def test_weights_that_sum_above_one_are_refused_rather_than_normalised(self) -> None:
        """Scaling silently would change a number the caller believes they chose."""
        with pytest.raises(ValueError, match="more than the whole sleeve"):
            sleeve(weights={**WEIGHTS, "INFY": Decimal("0.5000")})

    def test_an_empty_sleeve_reconciles_to_all_cash(self) -> None:
        result = allocate_units(capital=CAPITAL, weights={}, prices={})
        assert result.rows == ()
        assert result.remainder == CAPITAL
        assert result.balances

    def test_negative_capital_is_refused(self) -> None:
        with pytest.raises(ValueError, match="capital cannot be negative"):
            sleeve(capital=Decimal("-1.00"))

    def test_a_negative_weight_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            sleeve(weights={**WEIGHTS, "INFY": Decimal("-0.1000")})

    def test_negative_held_units_are_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            sleeve(held={"CUPID": -5})

    def test_two_spellings_of_one_symbol_are_refused_not_silently_merged(self) -> None:
        with pytest.raises(ValueError, match="twice"):
            sleeve(weights={"CUPID": Decimal("0.5000"), " cupid ": Decimal("0.5000")})

    def test_symbols_are_normalised_the_way_the_rest_of_the_repo_normalises_them(self) -> None:
        result = allocate_units(
            capital=CAPITAL,
            weights={" cupid ": FULL_WEIGHT},
            prices={"cupid": Decimal("317.45")},
        )
        assert result.symbols == ("CUPID",)

    def test_an_empty_symbol_is_refused(self) -> None:
        with pytest.raises(ValueError, match="empty symbol"):
            sleeve(weights={"   ": FULL_WEIGHT})

    def test_rows_come_back_in_a_stable_order(self) -> None:
        """Two calls with the same inputs must render identically."""
        assert sleeve().symbols == sleeve().symbols
        assert sleeve().symbols == ("CUPID", "TATAMOTORS", "INFY")


class TestItStaysAReportAndNotAnOrderList:
    def test_the_module_reaches_nothing_at_all(self) -> None:
        """Law 1, asserted over the source: prices arrive as an argument or not at all."""
        source = inspect.getsource(module)
        for forbidden in ("sqlalchemy", "httpx", "requests", "kiteconnect", "datetime.now"):
            assert forbidden not in source, f"portfolio_units names {forbidden}"

    def test_the_module_carries_no_order_vocabulary(self) -> None:
        """A unit count is a report. Execution stays behind the desk's confirmation step."""
        source = inspect.getsource(module)
        for forbidden in (
            "place_order",
            "place_gtt",
            "baskfy_execution",
            "KiteConnect",
            "transaction_type",
            "order_type",
        ):
            assert forbidden not in source, f"portfolio_units names {forbidden}"

    def test_it_states_rather_than_recommends(self) -> None:
        """Baskfy publishes no advice. A drift number is a fact; 'you should buy' is not."""
        prose = inspect.getsource(module).lower()
        for phrase in ("we recommend", "you should buy", "you should sell", "recommended for you"):
            assert phrase not in prose
