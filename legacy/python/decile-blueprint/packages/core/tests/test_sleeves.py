"""Splitting a portfolio across screens, with a slice run by hand (M34).

The arithmetic decides what somebody does with a crore, so the properties that matter are the
boring ones: every sleeve reconciles to the rupee, nothing is ever proposed for the manual slice,
and the regime cap withholds capital rather than quietly shrinking the portfolio.

Pure throughout — no database, no prices.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from baskfy_core.sleeves import (
    MANUAL,
    SCREEN,
    SleeveSpec,
    allocate,
    cap_for_tier,
)

CRORE = Decimal("10000000")


def screen_sleeve(name: str, capital: str, symbols: tuple[str, ...]) -> SleeveSpec:
    return SleeveSpec(name=name, kind=SCREEN, capital=Decimal(capital), symbols=symbols)


def manual_sleeve(capital: str) -> SleeveSpec:
    return SleeveSpec(name="Run by hand", kind=MANUAL, capital=Decimal(capital))


class TestItAddsUp:
    def test_every_sleeve_reconciles_to_the_rupee(self) -> None:
        """`deployed + cash == capital`, for each sleeve and for the whole portfolio.

        Equal weights over three names rarely divide into whole rupees. A tracker whose columns do
        not add up is worse than one that does not exist.
        """
        result = allocate(
            [
                screen_sleeve("Momentum 50", "4000000", ("A", "B", "C")),
                screen_sleeve("Quality 30", "3000000", ("D", "E", "F", "G")),
                manual_sleeve("3000000"),
            ]
        )

        for sleeve in result.sleeves:
            assert sleeve.balances, f"{sleeve.name} does not reconcile"
        assert result.balances
        assert result.capital == CRORE

    def test_the_remainder_is_cash_and_never_over_allocated(self) -> None:
        """A sleeve must never propose more than its capital, so the rounding goes down."""
        result = allocate([screen_sleeve("Thirds", "100", ("A", "B", "C"))])

        sleeve = result.sleeves[0]
        assert [row.amount for row in sleeve.rows] == [Decimal(33)] * 3
        assert sleeve.deployed == Decimal(99)
        assert sleeve.cash == Decimal(1)

    def test_weights_are_of_the_sleeve_not_the_portfolio(self) -> None:
        """A sleeve is the unit a person reasons about: four names in it are 25% each."""
        result = allocate(
            [
                screen_sleeve("Small", "1000000", ("A", "B", "C", "D")),
                screen_sleeve("Large", "9000000", ("E", "F")),
            ]
        )
        assert [r.weight_pct for r in result.sleeves[0].rows] == [Decimal("25.00")] * 4
        assert [r.weight_pct for r in result.sleeves[1].rows] == [Decimal("50.00")] * 2


class TestTheManualSleeve:
    def test_nothing_is_ever_proposed_for_it(self) -> None:
        result = allocate([manual_sleeve("2000000")])

        sleeve = result.sleeves[0]
        assert sleeve.rows == ()
        assert sleeve.deployed == Decimal(0)
        assert sleeve.capital == Decimal("2000000")

    def test_it_still_counts_toward_the_portfolio_total(self) -> None:
        """Leaving it out of the total would report a crore portfolio as an eighty-lakh one."""
        result = allocate([screen_sleeve("Screened", "8000000", ("A",)), manual_sleeve("2000000")])
        assert result.capital == CRORE

    def test_the_regime_cap_does_not_touch_it(self) -> None:
        """Scaling it would be deciding about capital this module was told not to touch."""
        result = allocate([manual_sleeve("2000000")], equity_cap_pct=Decimal(40))
        assert result.sleeves[0].capital == Decimal("2000000")

    def test_a_screen_sleeve_with_no_names_behaves_like_a_manual_one(self) -> None:
        """An empty screen proposes nothing rather than dividing by zero."""
        result = allocate([screen_sleeve("Empty", "1000000", ())])
        assert result.sleeves[0].rows == ()
        assert result.sleeves[0].cash == Decimal("1000000")


class TestTheRegimeCap:
    def test_it_withholds_capital_rather_than_shrinking_the_portfolio(self) -> None:
        """At R2 a crore still *is* a crore; 30 lakh of it is held as cash.

        The distinction matters on the page: a portfolio that quietly became seventy lakh would
        be a reporting bug, not a defensive stance.
        """
        result = allocate(
            [screen_sleeve("Momentum", "10000000", ("A", "B"))], equity_cap_pct=Decimal(70)
        )

        sleeve = result.sleeves[0]
        assert sleeve.capital == CRORE
        assert sleeve.deployed == Decimal("7000000")
        assert sleeve.cash == Decimal("3000000")
        assert result.capital == CRORE

    @pytest.mark.parametrize(("tier", "expected"), [("R1", 100), ("R2", 70), ("R3", 40), ("R4", 0)])
    def test_the_default_caps_mirror_the_desks_own(self, tier: str, expected: int) -> None:
        assert cap_for_tier(tier) == Decimal(expected)

    def test_an_unknown_tier_is_none_rather_than_a_guess(self) -> None:
        assert cap_for_tier("R9") is None

    def test_the_desks_live_caps_override_the_defaults(self) -> None:
        """A cap this module invented would disagree with the one the desk actually trades on."""
        assert cap_for_tier("R2", {"R2": Decimal(65)}) == Decimal(65)

    def test_no_cap_means_full_capital(self) -> None:
        result = allocate([screen_sleeve("Momentum", "1000000", ("A", "B"))])
        assert result.sleeves[0].deployed == Decimal("1000000")
        assert result.equity_cap_pct is None

    def test_r4_deploys_nothing_and_holds_everything(self) -> None:
        result = allocate(
            [screen_sleeve("Momentum", "1000000", ("A", "B"))], equity_cap_pct=Decimal(0)
        )
        assert result.deployed == Decimal(0)
        assert result.cash == Decimal("1000000")

    @pytest.mark.parametrize("bad", [Decimal(-1), Decimal(101)])
    def test_a_cap_outside_zero_to_a_hundred_is_refused(self, bad: Decimal) -> None:
        with pytest.raises(ValueError, match="percentage"):
            allocate([manual_sleeve("100")], equity_cap_pct=bad)


class TestRefusals:
    def test_negative_capital_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            allocate([screen_sleeve("Bad", "-1", ("A",))])

    def test_no_sleeves_is_an_empty_portfolio_not_an_error(self) -> None:
        result = allocate([])
        assert (result.capital, result.deployed, result.cash) == (
            Decimal(0),
            Decimal(0),
            Decimal(0),
        )
        assert result.balances


class TestTheWholeExample:
    def test_a_crore_across_three_screens_and_a_manual_slice(self) -> None:
        """The shape the feature was asked for, end to end."""
        result = allocate(
            [
                screen_sleeve("Momentum 50", "4000000", ("CUPID", "RRKABEL", "SONACOMS")),
                screen_sleeve("Quality 30", "2500000", ("A", "B")),
                screen_sleeve("Value 20", "1500000", ("C",)),
                manual_sleeve("2000000"),
            ]
        )

        assert result.capital == CRORE
        assert result.balances
        assert [s.name for s in result.sleeves][-1] == "Run by hand"
        assert result.sleeves[-1].rows == ()
        # Every rupee is accounted for in exactly one sleeve.
        assert sum((s.capital for s in result.sleeves), Decimal(0)) == CRORE
