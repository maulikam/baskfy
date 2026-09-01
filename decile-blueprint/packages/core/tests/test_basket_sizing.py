"""The spec for sizing a basket cut from a screen (`baskfy_core.basket_sizing`).

These assert the *contract* the module documents, not the arithmetic it happens to perform today:
which number a profile suggests, who wins when the investor disagrees with it, that the columns
add up to the rupee, and that a name count and an exposure tier never become the same idea.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from baskfy_core.basket_sizing import (
    DEFAULT_METHOD,
    DEFAULT_PROFILE,
    MAX_CASH_PCT,
    MAX_HOLDINGS,
    MIN_CASH_BUFFER_PCT,
    MIN_HOLDINGS,
    SUGGESTED_HOLDINGS,
    ZERO_CASH_PCT,
    Candidate,
    HoldingProfile,
    WeightMethod,
    cash_pct_for_tier,
    minimum_amount,
    resolve_holdings,
    scheme_weights,
    size_basket,
    suggested_holdings,
)
from baskfy_core.curated_baskets import assert_weights_sum_to_one

LAKH = Decimal(100_000)


def candidates(n: int, *, price: Decimal | None = Decimal("100.00")) -> list[Candidate]:
    return [Candidate(symbol=f"SYM{i:03d}", rank=i, price=price) for i in range(1, n + 1)]


class TestTheProfileSuggests:
    def test_the_three_profiles_are_25_20_12(self) -> None:
        assert SUGGESTED_HOLDINGS == {
            HoldingProfile.CONSERVATIVE: 25,
            HoldingProfile.BALANCED: 20,
            HoldingProfile.AGGRESSIVE: 12,
        }

    def test_more_aggressive_means_fewer_names(self) -> None:
        """The direction people get backwards: concentration is the aggressive choice."""
        assert (
            SUGGESTED_HOLDINGS[HoldingProfile.AGGRESSIVE]
            < SUGGESTED_HOLDINGS[HoldingProfile.BALANCED]
            < SUGGESTED_HOLDINGS[HoldingProfile.CONSERVATIVE]
        )

    def test_the_default_is_balanced_and_changes_no_existing_basket(self) -> None:
        """20 is what the web layer has always materialized a screen at (`DEFAULT_TOP_N`)."""
        assert DEFAULT_PROFILE is HoldingProfile.BALANCED
        assert suggested_holdings() == 20
        assert suggested_holdings(None) == suggested_holdings(DEFAULT_PROFILE)

    def test_every_suggestion_is_within_the_permitted_range(self) -> None:
        assert all(MIN_HOLDINGS <= n <= MAX_HOLDINGS for n in SUGGESTED_HOLDINGS.values())


class TestTheInvestorDecides:
    def test_an_explicit_count_beats_the_profile(self) -> None:
        assert resolve_holdings(available=40, profile=HoldingProfile.BALANCED, requested=7) == 7

    def test_a_suggestion_is_trimmed_to_what_the_screen_found(self) -> None:
        """The module absorbs this quietly: it is its problem, not the investor's to read about."""
        assert resolve_holdings(available=14, profile=HoldingProfile.BALANCED) == 14

    def test_an_explicit_count_the_screen_cannot_fill_is_refused(self) -> None:
        """Asked for something specific, so being told it is not there beats a silent clamp."""
        with pytest.raises(ValueError, match="you asked for 20 names"):
            resolve_holdings(available=11, requested=20)

    @pytest.mark.parametrize("requested", [MIN_HOLDINGS - 1, 0, -3, MAX_HOLDINGS + 1])
    def test_a_count_outside_the_permitted_range_is_refused(self, requested: int) -> None:
        with pytest.raises(ValueError, match="holds between"):
            resolve_holdings(available=200, requested=requested)

    def test_a_screen_too_thin_for_any_basket_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least 2 names"):
            resolve_holdings(available=1)


class TestCashComesFromTheExposureTier:
    @pytest.mark.parametrize(
        ("tier", "expected"),
        [("R1", Decimal(5)), ("R2", Decimal(30)), ("R3", Decimal(60)), ("R4", MAX_CASH_PCT)],
    )
    def test_it_is_the_complement_of_the_desks_equity_cap(
        self, tier: str, expected: Decimal
    ) -> None:
        """R2 caps equity at 70%, so 30% is withheld. R1's 0% is lifted to the rounding floor."""
        assert cash_pct_for_tier(tier) == expected

    def test_a_risk_off_tier_is_clamped_rather_than_emptying_the_basket(self) -> None:
        """R4 would withhold everything; "do not deploy" is a person's call, not a rounding one."""
        assert cash_pct_for_tier("R4") == MAX_CASH_PCT
        assert cash_pct_for_tier("R4") < Decimal(100)

    def test_no_regime_reading_is_not_a_reason_to_withhold_money(self) -> None:
        assert cash_pct_for_tier(None) == MIN_CASH_BUFFER_PCT
        assert cash_pct_for_tier("R9") == MIN_CASH_BUFFER_PCT

    def test_the_desks_live_caps_override_the_defaults(self) -> None:
        """A cap this module invented would disagree with the one the desk trades on."""
        assert cash_pct_for_tier("R2", caps={"R2": Decimal(90)}) == Decimal(10)

    def test_it_is_case_insensitive(self) -> None:
        assert cash_pct_for_tier("r3") == cash_pct_for_tier("R3")


class TestTheColumnsAddUp:
    def test_deployed_plus_cash_equals_the_amount_to_the_rupee(self) -> None:
        basket = size_basket(candidates(30), amount=LAKH, holdings=7)
        assert basket.balances
        assert basket.deployed + basket.cash == basket.amount

    def test_the_remainder_becomes_cash_rather_than_being_smeared(self) -> None:
        """3 names out of ₹1,00,000 at 5% cash: ₹95,000 / 3 does not divide evenly."""
        basket = size_basket(candidates(3), amount=LAKH, holdings=3, cash_pct=Decimal(5))
        assert {row.amount for row in basket.holdings} == {Decimal(31_666)}
        assert basket.deployed == Decimal(94_998)
        assert basket.cash == Decimal(5_002)
        assert basket.balances

    def test_the_basket_never_proposes_more_than_its_budget(self) -> None:
        for count in (3, 7, 11, 13, 17):
            basket = size_basket(candidates(count), amount=LAKH, holdings=count)
            budget = basket.amount * (Decimal(100) - basket.cash_pct) / Decimal(100)
            assert basket.deployed <= budget

    def test_constituent_weights_sum_to_one_so_the_catalog_will_accept_them(self) -> None:
        """`cb_constituent.weight` is a share of the deployed money; cash is not a constituent."""
        for count in (2, 3, 7, 12, 20, 25):
            basket = size_basket(candidates(30), amount=LAKH, holdings=count)
            assert_weights_sum_to_one(list(basket.weights))

    def test_the_displayed_percentages_leave_the_cash_visible(self) -> None:
        basket = size_basket(candidates(20), amount=LAKH, holdings=20, cash_pct=Decimal(30))
        shown = sum(row.weight_pct_of_amount for row in basket.holdings)
        assert abs(shown - Decimal(70)) <= Decimal("0.5")

    def test_a_higher_cash_share_deploys_less(self) -> None:
        light = size_basket(candidates(20), amount=LAKH, holdings=10, cash_pct=Decimal(5))
        heavy = size_basket(candidates(20), amount=LAKH, holdings=10, cash_pct=Decimal(60))
        assert heavy.deployed < light.deployed
        assert heavy.cash > light.cash

    def test_opting_out_of_cash_deploys_the_whole_budget(self) -> None:
        """SB7: 0% is an explicit choice. The leftover is only the whole-rupee remainder."""
        invested = size_basket(candidates(3), amount=LAKH, holdings=3, cash_pct=ZERO_CASH_PCT)
        buffered = size_basket(candidates(3), amount=LAKH, holdings=3, cash_pct=Decimal(5))
        assert invested.cash_pct == ZERO_CASH_PCT
        assert invested.deployed > buffered.deployed
        assert invested.cash < buffered.cash
        assert invested.balances
        assert {row.amount for row in invested.holdings} == {Decimal(33_333)}
        assert invested.cash == Decimal(1)


class TestWhichNamesAreChosen:
    def test_the_best_ranks_win_in_rank_order(self) -> None:
        rows = list(reversed(candidates(10)))
        basket = size_basket(rows, amount=LAKH, holdings=3)
        assert [row.symbol for row in basket.holdings] == ["SYM001", "SYM002", "SYM003"]

    def test_a_repeated_symbol_is_taken_once_at_its_best_rank(self) -> None:
        rows = [
            Candidate(symbol="AAA", rank=1),
            Candidate(symbol="AAA", rank=2),
            Candidate(symbol="BBB", rank=3),
        ]
        basket = size_basket(rows, amount=LAKH, holdings=2)
        assert [row.symbol for row in basket.holdings] == ["AAA", "BBB"]
        assert [row.rank for row in basket.holdings] == [1, 3]

    def test_symbols_are_normalised(self) -> None:
        rows = [Candidate(symbol=" aaa ", rank=1), Candidate(symbol="bbb", rank=2)]
        basket = size_basket(rows, amount=LAKH, holdings=2)
        assert [row.symbol for row in basket.holdings] == ["AAA", "BBB"]


class TestTheMinimumAmount:
    def test_the_dearest_name_binds_it(self) -> None:
        """Equal weights give every name the same slice, so the priciest share sets the floor."""
        rows = [
            Candidate(symbol="CHEAP", rank=1, price=Decimal(10)),
            Candidate(symbol="DEAR", rank=2, price=Decimal(5_000)),
        ]
        floor = minimum_amount(rows, holdings=2, cash_pct=Decimal(5))
        assert floor is not None
        assert floor >= Decimal(5_000) * 2 / (Decimal(95) / Decimal(100))

    def test_it_is_rounded_up_so_it_reads_as_a_threshold(self) -> None:
        rows = [
            Candidate(symbol="AAA", rank=1, price=Decimal("1234.56")),
            Candidate(symbol="BBB", rank=2, price=Decimal("10.00")),
        ]
        floor = minimum_amount(rows, holdings=2, cash_pct=Decimal(5))
        assert floor is not None
        assert floor % Decimal(100) == 0

    def test_unpriced_names_yield_no_floor_rather_than_a_made_up_one(self) -> None:
        assert minimum_amount(candidates(5, price=None), holdings=5, cash_pct=Decimal(5)) is None

    def test_an_amount_below_the_floor_is_reported_not_refused(self) -> None:
        """The basket is still built: telling the investor is more use than a stack trace."""
        rows = [
            Candidate(symbol="DEAR", rank=1, price=Decimal(50_000)),
            Candidate(symbol="ALSO", rank=2, price=Decimal(50_000)),
        ]
        basket = size_basket(rows, amount=Decimal(1_000), holdings=2)
        assert basket.minimum is not None
        assert not basket.is_fundable
        assert basket.balances

    def test_a_sufficient_amount_is_fundable(self) -> None:
        assert size_basket(candidates(30), amount=LAKH, holdings=20).is_fundable


class TestWhatTheBasketRemembers:
    def test_it_names_the_profile_it_followed(self) -> None:
        basket = size_basket(candidates(40), amount=LAKH, profile=HoldingProfile.CONSERVATIVE)
        assert basket.profile is HoldingProfile.CONSERVATIVE
        assert not basket.holdings_overridden
        assert len(basket.holdings) == 25

    def test_an_override_records_that_no_profile_chose_the_count(self) -> None:
        basket = size_basket(
            candidates(40), amount=LAKH, profile=HoldingProfile.BALANCED, holdings=9
        )
        assert basket.holdings_overridden
        assert basket.profile is None
        assert len(basket.holdings) == 9

    def test_no_stated_profile_falls_back_to_the_default(self) -> None:
        basket = size_basket(candidates(40), amount=LAKH)
        assert basket.profile is DEFAULT_PROFILE
        assert len(basket.holdings) == suggested_holdings()


class TestRefusals:
    @pytest.mark.parametrize("amount", [Decimal(0), Decimal(-1), Decimal("-0.01")])
    def test_a_non_positive_amount_is_refused(self, amount: Decimal) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            size_basket(candidates(10), amount=amount, holdings=3)

    @pytest.mark.parametrize("cash", [Decimal("-0.01"), Decimal(96), Decimal(100)])
    def test_a_cash_share_outside_the_permitted_band_is_refused(self, cash: Decimal) -> None:
        with pytest.raises(ValueError, match="cash must be between"):
            size_basket(candidates(10), amount=LAKH, holdings=3, cash_pct=cash)

    def test_zero_cash_is_allowed_when_the_investor_opts_out(self) -> None:
        basket = size_basket(candidates(10), amount=LAKH, holdings=3, cash_pct=ZERO_CASH_PCT)
        assert basket.cash_pct == ZERO_CASH_PCT


class TestTheTwoVocabulariesStaySeparate:
    """docs/DECISIONS-MERGE.md: R1-R4 are exposure tiers. A name count is not one of them."""

    def test_no_profile_is_named_like_an_exposure_tier(self) -> None:
        tiers = {"R1", "R2", "R3", "R4"}
        assert {profile.value for profile in HoldingProfile}.isdisjoint(tiers)
        assert {profile.name for profile in HoldingProfile}.isdisjoint(tiers)

    def test_a_tier_cannot_be_passed_where_a_profile_belongs(self) -> None:
        with pytest.raises(ValueError):
            HoldingProfile("R1")

    def test_a_profile_changes_the_count_and_never_the_cash(self) -> None:
        """The whole point of keeping them apart, asserted rather than trusted."""
        baskets = [
            size_basket(candidates(40), amount=LAKH, profile=profile) for profile in HoldingProfile
        ]
        assert len({len(b.holdings) for b in baskets}) == len(HoldingProfile)
        assert len({b.cash_pct for b in baskets}) == 1

    def test_a_tier_changes_the_cash_and_never_the_count(self) -> None:
        baskets = [
            size_basket(
                candidates(40),
                amount=LAKH,
                profile=HoldingProfile.BALANCED,
                cash_pct=cash_pct_for_tier(tier),
            )
            for tier in ("R1", "R2", "R3")
        ]
        assert len({b.cash_pct for b in baskets}) == 3
        assert len({len(b.holdings) for b in baskets}) == 1


class TestWeightMethods:
    def test_the_default_is_equal_so_existing_baskets_do_not_move(self) -> None:
        assert DEFAULT_METHOD is WeightMethod.EQUAL
        equal = size_basket(candidates(10), amount=LAKH, holdings=5)
        implied = size_basket(candidates(10), amount=LAKH, holdings=5, method=WeightMethod.EQUAL)
        assert [row.amount for row in equal.holdings] == [row.amount for row in implied.holdings]
        assert equal.method is WeightMethod.EQUAL

    def test_rank_gives_the_best_name_the_largest_slice(self) -> None:
        basket = size_basket(candidates(5), amount=LAKH, holdings=4, method=WeightMethod.RANK)
        amounts = [row.amount for row in basket.holdings]
        assert amounts == sorted(amounts, reverse=True)
        assert basket.holdings[0].symbol == "SYM001"
        assert basket.holdings[0].weight > basket.holdings[-1].weight
        assert basket.balances

    def test_score_follows_the_screen_factor_not_the_rank(self) -> None:
        rows = [
            Candidate(symbol="LOW", rank=1, score=Decimal(1), price=Decimal(100)),
            Candidate(symbol="HIGH", rank=2, score=Decimal(9), price=Decimal(100)),
        ]
        basket = size_basket(rows, amount=LAKH, holdings=2, method=WeightMethod.SCORE)
        by_symbol = {row.symbol: row for row in basket.holdings}
        assert by_symbol["HIGH"].amount > by_symbol["LOW"].amount

    def test_inverse_vol_gives_the_calmer_name_more(self) -> None:
        rows = [
            Candidate(symbol="JUMPY", rank=1, vol=Decimal("0.50"), price=Decimal(100)),
            Candidate(symbol="CALM", rank=2, vol=Decimal("0.10"), price=Decimal(100)),
        ]
        basket = size_basket(rows, amount=LAKH, holdings=2, method=WeightMethod.INV_VOL)
        by_symbol = {row.symbol: row for row in basket.holdings}
        assert by_symbol["CALM"].amount > by_symbol["JUMPY"].amount

    def test_custom_applies_the_investor_numbers_to_the_screens_names(self) -> None:
        basket = size_basket(
            candidates(3),
            amount=LAKH,
            holdings=3,
            method=WeightMethod.CUSTOM,
            custom_weights={"SYM001": Decimal(70), "SYM002": Decimal(20), "SYM003": Decimal(10)},
        )
        assert basket.holdings[0].weight == Decimal("0.7000")
        assert basket.holdings[1].weight == Decimal("0.2000")
        assert basket.holdings[2].weight == Decimal("0.1000")
        assert basket.balances

    def test_custom_cannot_name_a_holding_the_screen_did_not_select(self) -> None:
        with pytest.raises(ValueError, match="did not select"):
            size_basket(
                candidates(2),
                amount=LAKH,
                holdings=2,
                method=WeightMethod.CUSTOM,
                custom_weights={"SYM001": Decimal(1), "SYM002": Decimal(1), "FAKE": Decimal(1)},
            )

    def test_custom_without_a_weight_for_a_chosen_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="SYM002"):
            size_basket(
                candidates(2),
                amount=LAKH,
                holdings=2,
                method=WeightMethod.CUSTOM,
                custom_weights={"SYM001": Decimal(1)},
            )

    def test_score_without_a_score_is_refused_rather_than_becoming_equal(self) -> None:
        with pytest.raises(ValueError, match="positive screen score"):
            size_basket(candidates(3), amount=LAKH, holdings=3, method=WeightMethod.SCORE)

    def test_inverse_vol_without_vol_is_refused(self) -> None:
        with pytest.raises(ValueError, match="volatility"):
            size_basket(candidates(3), amount=LAKH, holdings=3, method=WeightMethod.INV_VOL)

    def test_rank_weights_inside_the_selected_set_not_absolute_rank(self) -> None:
        """A slice from ranks 10-12 must not be almost equal just because 10 ~ 12."""
        rows = [
            Candidate(symbol="A", rank=10, price=Decimal(100)),
            Candidate(symbol="B", rank=11, price=Decimal(100)),
            Candidate(symbol="C", rank=12, price=Decimal(100)),
        ]
        weights = scheme_weights(rows, WeightMethod.RANK)
        assert weights[0] > weights[1] > weights[2]
        assert weights[0] == Decimal("0.5000")
        assert weights[1] == Decimal("0.3333")
        assert weights[2] == Decimal("0.1667")
