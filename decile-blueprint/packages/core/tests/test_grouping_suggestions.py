"""The suggestion engine's rules — `PORTFOLIO_REDESIGN.md` §6.6, §6.7 and §10 Phase 3.

These assert the **spec**, not the implementation (house rule 2). The centrepiece is §6.6's own
scenario, built as a fixture rather than described: forty holdings land in Unallocated after a
broker connects, and the product has to turn that into a handful of named groups. If that fixture
ever stops producing a short, useful, reproducible list, activation is broken and this file says
so before a user finds out.

Numbers are chosen adversarially. Prices do not divide evenly, the day's contributions are
tenths and hundredths whose binary approximations do not add to their decimal sum, one instrument
is held at two brokers, one sector has a single holding, and one lens overlaps two capital
portfolios and Unallocated at once.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
from decimal import Decimal

import pytest

from baskfy_core.allocation_ledger import (
    Allocation,
    Holding,
    HoldingKey,
    Portfolio,
    PortfolioKind,
    PortfolioSource,
)
from baskfy_core.grouping_suggestions import (
    DEFAULT_SUGGESTION_LIMIT,
    MIN_HOLDINGS_PER_SUGGESTION,
    MONITORING_OVERLAP_NOTE,
    ContributionReport,
    GroupingSuggestion,
    HoldingContribution,
    MonitoringOverlap,
    PortfolioContribution,
    SubscribedBasket,
    SuggestionBasis,
    SuggestionInputs,
    contribution_breakdown,
    monitoring_overlaps,
    purchase_era,
    rank_suggestions,
    suggest_by_basket_overlap,
    suggest_by_purchase_era,
    suggest_by_sector,
    suggest_groupings,
)


def _module_source() -> str:
    """The module's own source. Law 1 and house rule 9 are properties of the *text*, and the
    honest way to assert a property of the text is to read it — the same guard
    `test_allocation_ledger.py` carries."""

    return (
        pathlib.Path(__file__).resolve().parents[1] / "src/baskfy_core/grouping_suggestions.py"
    ).read_text()


AS_OF = dt.date(2026, 8, 25)
GROUPED_ON = dt.date(2026, 1, 2)

ZERODHA = 10
UPSTOX = 20

#: Five sectors, dealt round-robin over the pile so every one of them clears
#: MIN_HOLDINGS_PER_SUGGESTION. A sector with one holding gets its own test, not its own fixture.
SECTORS = ("Financials", "Information Technology", "Energy", "Healthcare", "Consumer")

#: Three financial years, all inside the era horizon, so none of them collapses.
ERA_DATES = (dt.date(2024, 6, 10), dt.date(2025, 7, 15), dt.date(2026, 5, 20))


def key(instrument_id: int, broker_account_id: int = ZERODHA) -> HoldingKey:
    return HoldingKey(instrument_id=instrument_id, broker_account_id=broker_account_id)


def capital(portfolio_id: int, name: str) -> Portfolio:
    return Portfolio(
        portfolio_id=portfolio_id,
        name=name,
        kind=PortfolioKind.CAPITAL,
        source=PortfolioSource.HOLDING_GROUP,
        started_on=GROUPED_ON,
    )


def monitoring(portfolio_id: int, name: str = "All defence stocks") -> Portfolio:
    return Portfolio(
        portfolio_id=portfolio_id,
        name=name,
        kind=PortfolioKind.MONITORING,
        source=PortfolioSource.HOLDING_GROUP,
        started_on=GROUPED_ON,
    )


# ---------------------------------------------------------------------------
# §6.6's scenario, as data
# ---------------------------------------------------------------------------


def first_run_holdings() -> list[Holding]:
    """Forty holdings, exactly as §6.6 describes them: everything, unsorted, all at once.

    Thirty-eight instruments at one broker plus two of them held again at a second broker, so the
    fixture carries §6.7's "HDFC Bank — 320 (Zerodha 200 · Upstox 120)" case: one instrument, two
    holdings, allocatable apart.
    """
    holdings = [
        Holding(key(instrument_id), Decimal(3 + instrument_id % 7))
        for instrument_id in range(1, 39)
    ]
    holdings.append(Holding(key(1, UPSTOX), Decimal("11")))
    holdings.append(Holding(key(2, UPSTOX), Decimal("7")))
    return holdings


def first_run_prices() -> dict[int, Decimal]:
    """Prices that do not divide evenly, so an implementation using binary fractions drifts."""
    return {
        instrument_id: Decimal("100.11") + Decimal(instrument_id) * Decimal("37.77")
        for instrument_id in range(1, 39)
    }


def first_run_sectors() -> dict[int, str]:
    return {instrument_id: SECTORS[instrument_id % len(SECTORS)] for instrument_id in range(1, 39)}


def first_run_first_bought() -> dict[HoldingKey, dt.date]:
    """Buy dates for thirty of the forty. The other ten have none — a broker that does not
    publish buy history before a CAS import (§5.3), which §5.2 refuses to conflate with "bought
    long ago"."""
    return {
        key(instrument_id): ERA_DATES[instrument_id % len(ERA_DATES)]
        for instrument_id in range(1, 31)
    }


#: A model the user subscribes to. Nine of its twelve stocks are in the pile, three are not, and
#: one of the nine is held at two brokers — which must not inflate coverage.
SUBSCRIBED = SubscribedBasket(
    basket_id=7,
    name="Smallcap Momentum",
    constituent_instrument_ids=frozenset({2, 4, 6, 8, 10, 12, 14, 16, 18, 50, 51, 52}),
)


def first_run_inputs() -> SuggestionInputs:
    return SuggestionInputs(
        sectors=first_run_sectors(),
        first_bought=first_run_first_bought(),
        as_of=AS_OF,
        baskets=[SUBSCRIBED],
    )


class TestSection66FortyHoldingsBecomeAFewNamedGroups:
    """ "Getting from 40 unallocated holdings to 4 named portfolios IS activation." — §6.6"""

    def test_forty_unallocated_holdings_produce_a_small_set_of_suggestions(self) -> None:
        suggestions = suggest_groupings(
            first_run_holdings(), first_run_prices(), first_run_inputs()
        )

        assert len(suggestions) == DEFAULT_SUGGESTION_LIMIT
        assert len(first_run_holdings()) == 40

    def test_every_suggestion_is_a_group_a_user_could_name_and_accept(self) -> None:
        """Useful, not merely few: each one has a name, a reason, real money and real holdings."""
        for suggestion in suggest_groupings(
            first_run_holdings(), first_run_prices(), first_run_inputs()
        ):
            assert suggestion.proposed_name
            assert suggestion.rationale
            assert suggestion.holding_count >= MIN_HOLDINGS_PER_SUGGESTION
            assert suggestion.value > 0

    def test_all_three_bases_from_the_spec_are_offered(self) -> None:
        """§6.6 names sector, purchase era and basket overlap. A first run that only ever showed
        one of them would be a narrower product than the spec asks for."""
        offered = {
            suggestion.basis
            for suggestion in suggest_groupings(
                first_run_holdings(),
                first_run_prices(),
                first_run_inputs(),
                limit=20,
            )
        }
        assert offered == set(SuggestionBasis)

    def test_the_suggestions_between_them_cover_most_of_the_pile(self) -> None:
        """Activation means the pile shrinks. A list of six suggestions that touched four
        holdings would satisfy every other assertion here and help nobody."""
        covered = {
            holding_key
            for suggestion in suggest_groupings(
                first_run_holdings(), first_run_prices(), first_run_inputs()
            )
            for holding_key in suggestion.keys
        }
        assert len(covered) >= 30

    def test_the_first_suggestion_is_the_one_that_sorts_the_most_money(self) -> None:
        """The stated rank rule, checked against the whole candidate set rather than the top of
        it: the head of the list is the largest single decision available."""
        everything = suggest_groupings(
            first_run_holdings(), first_run_prices(), first_run_inputs(), limit=50
        )
        assert everything[0].value == max(suggestion.value for suggestion in everything)

    def test_an_empty_unallocated_set_produces_no_suggestions_at_all(self) -> None:
        """Not a placeholder, not a zero-valued group, not an "Unknown" bucket — nothing. §6.6's
        section is only shown when something is unallocated in the first place."""
        assert suggest_groupings([], first_run_prices(), first_run_inputs()) == []

    def test_a_pile_the_product_knows_nothing_about_produces_no_suggestions(self) -> None:
        """No sectors, no dates, no subscriptions: forty holdings and nothing to say about them.
        Silence is the honest output; an invented grouping would be worse than none."""
        assert suggest_groupings(first_run_holdings(), first_run_prices(), SuggestionInputs()) == []


class TestDeterminism:
    """Same input, same order out. A first-run screen that reshuffled between two renders would
    make the product look like it was guessing — which, by construction, it is not."""

    def test_repeated_calls_return_an_identical_list(self) -> None:
        holdings, prices, inputs = first_run_holdings(), first_run_prices(), first_run_inputs()

        assert suggest_groupings(holdings, prices, inputs) == suggest_groupings(
            holdings, prices, inputs
        )

    def test_the_order_the_holdings_arrive_in_changes_nothing(self) -> None:
        """The real risk: grouping walks a dict, and a differently ordered input would otherwise
        produce differently ordered suggestions that still contained the same names."""
        prices, inputs = first_run_prices(), first_run_inputs()
        forwards = suggest_groupings(first_run_holdings(), prices, inputs)
        backwards = suggest_groupings(list(reversed(first_run_holdings())), prices, inputs)

        assert forwards == backwards

    def test_holdings_inside_a_suggestion_are_ordered_too(self) -> None:
        suggestions = suggest_groupings(
            list(reversed(first_run_holdings())), first_run_prices(), first_run_inputs()
        )
        for suggestion in suggestions:
            ordered = sorted(suggestion.keys, key=lambda k: (k.instrument_id, k.broker_account_id))
            assert list(suggestion.keys) == ordered


class TestRankingRule:
    """The four-part key from the module docstring, one test per part."""

    def sector(self, name: str, count: int, value: str) -> GroupingSuggestion:
        return GroupingSuggestion(
            basis=SuggestionBasis.SECTOR,
            proposed_name=name,
            keys=tuple(key(i) for i in range(1, count + 1)),
            value=Decimal(value),
            rationale="fixture",
        )

    def test_value_covered_outranks_holding_count(self) -> None:
        """Money first: §6.6 measures progress by what stops being Unallocated, not by row
        count. Two holdings worth more beat five holdings worth less."""
        big = self.sector("Energy", 2, "500000.00")
        many = self.sector("Consumer", 5, "400000.00")

        assert rank_suggestions([many, big]) == [big, many]

    def test_holding_count_breaks_a_value_tie(self) -> None:
        wide = self.sector("Consumer", 5, "400000.00")
        narrow = self.sector("Energy", 2, "400000.00")

        assert rank_suggestions([narrow, wide]) == [wide, narrow]

    def test_a_subscribed_basket_breaks_a_value_and_count_tie(self) -> None:
        """The user subscribed to the model, so they have already said this grouping matters."""
        overlap = GroupingSuggestion(
            basis=SuggestionBasis.BASKET_OVERLAP,
            proposed_name="Smallcap Momentum",
            keys=(key(1), key(2)),
            value=Decimal("400000.00"),
            rationale="fixture",
            basket_id=7,
            basket_coverage=Decimal("0.5000"),
        )
        era = GroupingSuggestion(
            basis=SuggestionBasis.PURCHASE_ERA,
            proposed_name="Bought in FY 2025-26",
            keys=(key(3), key(4)),
            value=Decimal("400000.00"),
            rationale="fixture",
        )
        sector = self.sector("Energy", 2, "400000.00")

        assert rank_suggestions([era, sector, overlap]) == [overlap, sector, era]

    def test_the_name_is_the_tie_break_of_last_resort(self) -> None:
        """Not meaningful, and that is the point: nothing is left to iteration order."""
        zulu = self.sector("Zinc", 2, "400000.00")
        alpha = self.sector("Aluminium", 2, "400000.00")

        assert rank_suggestions([zulu, alpha]) == [alpha, zulu]


class TestOneHoldingSuggestionsAreNoise:
    """A grouping of one is a rename dressed as a grouping — see the module docstring."""

    def test_a_sector_with_a_single_holding_is_not_suggested(self) -> None:
        holdings = [Holding(key(1), Decimal("10")), Holding(key(2), Decimal("10"))]
        prices = {1: Decimal("100.11"), 2: Decimal("200.22")}
        sectors = {1: "Energy", 2: "Healthcare"}

        assert suggest_by_sector(holdings, prices, sectors) == []

    def test_a_single_matching_constituent_is_not_a_basket_overlap(self) -> None:
        holdings = [Holding(key(2), Decimal("10")), Holding(key(99), Decimal("10"))]
        prices = {2: Decimal("100.11"), 99: Decimal("200.22")}

        assert suggest_by_basket_overlap(holdings, prices, SUBSCRIBED) is None

    def test_no_suggestion_can_even_be_constructed_with_one_holding(self) -> None:
        """Enforced by the type, not by each producer remembering to filter."""
        with pytest.raises(ValueError, match="rename, not a grouping"):
            GroupingSuggestion(
                basis=SuggestionBasis.SECTOR,
                proposed_name="Energy",
                keys=(key(1),),
                value=Decimal("100.00"),
                rationale="fixture",
            )

    def test_a_suggestion_cannot_name_the_same_holding_twice(self) -> None:
        with pytest.raises(ValueError, match="same holding twice"):
            GroupingSuggestion(
                basis=SuggestionBasis.SECTOR,
                proposed_name="Energy",
                keys=(key(1), key(1)),
                value=Decimal("100.00"),
                rationale="fixture",
            )


class TestSectorSuggestions:
    def test_holdings_of_one_sector_are_grouped_and_valued_together(self) -> None:
        holdings = [Holding(key(i), Decimal("10")) for i in (1, 2, 3)]
        prices = {1: Decimal("100.11"), 2: Decimal("200.22"), 3: Decimal("50.05")}
        sectors = {1: "Energy", 2: "Energy", 3: "Healthcare"}

        suggestions = suggest_by_sector(holdings, prices, sectors)

        assert [s.proposed_name for s in suggestions] == ["Energy"]
        assert suggestions[0].keys == (key(1), key(2))
        assert suggestions[0].value == Decimal("3003.30")

    def test_an_instrument_with_no_known_sector_is_skipped_not_filed_under_unknown(self) -> None:
        """A portfolio named after a gap in our own data is worse than no suggestion."""
        holdings = [Holding(key(i), Decimal("10")) for i in (1, 2, 3)]
        prices = {1: Decimal("100.11"), 2: Decimal("200.22"), 3: Decimal("50.05")}
        sectors = {1: "Energy", 2: "Energy"}

        suggestions = suggest_by_sector(holdings, prices, sectors)

        assert [s.proposed_name for s in suggestions] == ["Energy"]
        assert key(3) not in suggestions[0].keys

    def test_the_same_instrument_at_two_brokers_contributes_two_holdings(self) -> None:
        """§6.7 displays them as one row; the ledger allocates them apart, so a suggestion has
        to carry both or accepting it would leave half the position behind."""
        holdings = [Holding(key(1), Decimal("10")), Holding(key(1, UPSTOX), Decimal("5"))]
        prices = {1: Decimal("100.11")}

        suggestions = suggest_by_sector(holdings, prices, {1: "Energy"})

        assert suggestions[0].keys == (key(1, ZERODHA), key(1, UPSTOX))
        assert suggestions[0].value == Decimal("1501.65")

    def test_a_missing_price_raises_rather_than_ranking_the_group_too_low(self) -> None:
        holdings = [Holding(key(1), Decimal("10")), Holding(key(2), Decimal("10"))]

        with pytest.raises(KeyError):
            suggest_by_sector(holdings, {1: Decimal("100.11")}, {1: "Energy", 2: "Energy"})


class TestPurchaseEras:
    """Financial-year buckets, and the reasons they are financial years (see `purchase_era`)."""

    def test_the_era_boundary_is_the_first_of_april(self) -> None:
        assert purchase_era(dt.date(2026, 3, 31), AS_OF) == "Bought in FY 2025-26"
        assert purchase_era(dt.date(2026, 4, 1), AS_OF) == "Bought in FY 2026-27"

    def test_old_purchases_collapse_into_one_bucket(self) -> None:
        """Otherwise a fifteen-year investor is handed fifteen era suggestions, most of them
        below the one-holding threshold anyway."""
        assert purchase_era(dt.date(2013, 9, 1), AS_OF) == purchase_era(dt.date(2019, 9, 1), AS_OF)
        assert purchase_era(dt.date(2013, 9, 1), AS_OF).endswith("or earlier")

    def test_an_era_does_not_move_when_the_as_of_date_moves(self) -> None:
        """The property that ruled out rolling windows: a holding never migrates out of the era
        the user named a portfolio after."""
        bought = dt.date(2025, 7, 15)

        assert purchase_era(bought, dt.date(2025, 8, 1)) == purchase_era(bought, AS_OF)

    def test_a_purchase_after_the_as_of_date_is_refused(self) -> None:
        with pytest.raises(ValueError, match="bought in the future"):
            purchase_era(dt.date(2027, 1, 1), AS_OF)

    def test_holdings_bought_in_the_same_year_are_grouped(self) -> None:
        holdings = [Holding(key(i), Decimal("10")) for i in (1, 2, 3)]
        prices = {1: Decimal("100.11"), 2: Decimal("200.22"), 3: Decimal("50.05")}
        first_bought = {
            key(1): dt.date(2025, 7, 15),
            key(2): dt.date(2026, 3, 31),
            key(3): dt.date(2026, 4, 1),
        }

        suggestions = suggest_by_purchase_era(holdings, prices, first_bought, as_of=AS_OF)

        assert [s.proposed_name for s in suggestions] == ["Bought in FY 2025-26"]
        assert suggestions[0].keys == (key(1), key(2))

    def test_a_holding_with_no_known_buy_date_is_skipped(self) -> None:
        """§5.2: "no history yet" and "bought long ago" are never the same statement."""
        holdings = [Holding(key(i), Decimal("10")) for i in (1, 2, 3)]
        prices = {1: Decimal("100.11"), 2: Decimal("200.22"), 3: Decimal("50.05")}
        first_bought = {key(1): dt.date(2025, 7, 15), key(2): dt.date(2025, 8, 15)}

        suggestions = suggest_by_purchase_era(holdings, prices, first_bought, as_of=AS_OF)

        assert key(3) not in suggestions[0].keys

    def test_purchase_dates_without_an_as_of_date_are_refused(self) -> None:
        """Law 1 in one argument: this package cannot supply the missing date itself."""
        with pytest.raises(ValueError, match="reads no clock"):
            SuggestionInputs(first_bought={key(1): dt.date(2025, 7, 15)})


class TestBasketOverlap:
    def test_coverage_is_constituents_held_over_constituents_in_the_model(self) -> None:
        holdings = first_run_holdings()

        overlap = suggest_by_basket_overlap(holdings, first_run_prices(), SUBSCRIBED)

        assert overlap is not None
        assert overlap.basket_coverage == Decimal("0.7500")
        assert overlap.missing_instrument_ids == (50, 51, 52)

    def test_a_second_broker_account_does_not_inflate_coverage(self) -> None:
        """Instrument 2 is held twice in the fixture. Ten holdings, nine constituents, and the
        question coverage answers is "how much of the model do I hold"."""
        overlap = suggest_by_basket_overlap(first_run_holdings(), first_run_prices(), SUBSCRIBED)

        assert overlap is not None
        assert overlap.holding_count == 10
        assert overlap.basket_coverage == Decimal("0.7500")

    def test_nothing_held_produces_no_suggestion_rather_than_a_zero_percent_row(self) -> None:
        holdings = [Holding(key(90), Decimal("10")), Holding(key(91), Decimal("10"))]
        prices = {90: Decimal("100.11"), 91: Decimal("200.22")}

        assert suggest_by_basket_overlap(holdings, prices, SUBSCRIBED) is None

    def test_a_model_with_no_constituents_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no constituents"):
            SubscribedBasket(basket_id=8, name="Empty", constituent_instrument_ids=frozenset())

    def test_only_a_basket_overlap_carries_coverage(self) -> None:
        """Coverage of a model a grouping was not derived from is a number with no meaning."""
        with pytest.raises(ValueError, match="has no basket"):
            GroupingSuggestion(
                basis=SuggestionBasis.SECTOR,
                proposed_name="Energy",
                keys=(key(1), key(2)),
                value=Decimal("100.00"),
                rationale="fixture",
                basket_id=7,
                basket_coverage=Decimal("0.5"),
            )

    def test_a_basket_overlap_must_say_how_complete_it_is(self) -> None:
        with pytest.raises(ValueError, match="must name its basket"):
            GroupingSuggestion(
                basis=SuggestionBasis.BASKET_OVERLAP,
                proposed_name="Smallcap Momentum",
                keys=(key(1), key(2)),
                value=Decimal("100.00"),
                rationale="fixture",
            )


class TestOverlapDetectionIsInformationNotAWarning:
    """§4.1: a monitoring view is *supposed* to overlap. §10 asks for the report, and the report
    is never an accusation."""

    def scenario(
        self,
    ) -> tuple[
        list[Holding],
        list[Allocation],
        dict[int, Portfolio],
        dict[int, list[HoldingKey]],
        dict[int, Decimal],
    ]:
        holdings = [Holding(key(i), Decimal("10")) for i in (1, 2, 3, 4)]
        allocations = [
            Allocation(key(1), 1, Decimal("10")),
            Allocation(key(2), 1, Decimal("10")),
            Allocation(key(3), 2, Decimal("10")),
            # key(4) has no row at all: absence means Unallocated.
        ]
        portfolios = {
            1: capital(1, "Momentum"),
            2: capital(2, "Long term"),
            3: monitoring(3),
        }
        memberships = {3: [key(1), key(3), key(4)]}
        prices = {
            1: Decimal("100.11"),
            2: Decimal("200.22"),
            3: Decimal("50.05"),
            4: Decimal("333.33"),
        }
        return holdings, allocations, portfolios, memberships, prices

    def test_a_lens_overlapping_a_capital_portfolio_is_reported_with_its_value(self) -> None:
        holdings, allocations, portfolios, memberships, prices = self.scenario()

        rows = monitoring_overlaps(holdings, allocations, portfolios, memberships, prices)

        against = {row.portfolio_name: row for row in rows}
        assert set(against) == {"Momentum", "Long term", "Unallocated"}
        assert against["Momentum"].keys == (key(1),)
        assert against["Momentum"].value == Decimal("1001.10")
        assert against["Long term"].value == Decimal("500.50")

    def test_holdings_in_the_lens_that_are_in_no_portfolio_report_against_unallocated(
        self,
    ) -> None:
        """On a first run this is the commonest row and the most actionable: a lens over unsorted
        holdings is a §6.7 grouping waiting to be accepted."""
        holdings, allocations, portfolios, memberships, prices = self.scenario()

        rows = monitoring_overlaps(holdings, allocations, portfolios, memberships, prices)
        unallocated = next(row for row in rows if row.portfolio_id is None)

        assert unallocated.portfolio_name == "Unallocated"
        assert unallocated.keys == (key(4),)

    def test_an_overlap_never_raises_and_is_never_called_an_error(self) -> None:
        """The point of §4.1. Nothing in a row's own words may suggest the user did anything
        wrong, because they did not — the overlap is the feature."""
        holdings, allocations, portfolios, memberships, prices = self.scenario()

        rows = monitoring_overlaps(holdings, allocations, portfolios, memberships, prices)

        forbidden = (
            "error",
            "warning",
            "conflict",
            "problem",
            "invalid",
            "wrong",
            "violation",
            "unexpected",
            "duplicate",
            "double",
            "fix ",
            "should not",
            "must not",
        )
        assert rows
        for row in rows:
            spoken = row.note.lower()
            assert [word for word in forbidden if word in spoken] == []

    def test_every_row_carries_section_4_1s_own_sentence(self) -> None:
        holdings, allocations, portfolios, memberships, prices = self.scenario()

        rows = monitoring_overlaps(holdings, allocations, portfolios, memberships, prices)

        assert all(row.note.endswith(MONITORING_OVERLAP_NOTE) for row in rows)
        assert "excluded from totals" in MONITORING_OVERLAP_NOTE

    def test_the_report_is_deterministic(self) -> None:
        holdings, allocations, portfolios, memberships, prices = self.scenario()

        assert monitoring_overlaps(
            holdings, allocations, portfolios, memberships, prices
        ) == monitoring_overlaps(
            list(reversed(holdings)), allocations, portfolios, memberships, prices
        )

    def test_a_capital_portfolio_cannot_be_passed_as_a_lens(self) -> None:
        """Its membership is already stated by its allocations; a second way to say where a
        holding sits is a second way to get it wrong."""
        holdings, allocations, portfolios, _, prices = self.scenario()

        with pytest.raises(ValueError, match="capital portfolio"):
            monitoring_overlaps(holdings, allocations, portfolios, {1: [key(1)]}, prices)

    def test_a_lens_that_does_not_exist_is_refused(self) -> None:
        holdings, allocations, portfolios, _, prices = self.scenario()

        with pytest.raises(ValueError, match="does not exist"):
            monitoring_overlaps(holdings, allocations, portfolios, {9: [key(1)]}, prices)

    def test_a_lens_over_a_holding_we_do_not_have_is_refused(self) -> None:
        holdings, allocations, portfolios, _, prices = self.scenario()

        with pytest.raises(KeyError):
            monitoring_overlaps(holdings, allocations, portfolios, {3: [key(77)]}, prices)

    def test_an_illegal_allocation_set_is_refused_before_anything_is_reported(self) -> None:
        holdings, _, portfolios, memberships, prices = self.scenario()
        # Phase 3 made two capital portfolios legal, so the illegal set is now one that claims
        # more shares than exist — 10 + 10 against a 10-share position.
        over = [Allocation(key(1), 1, Decimal("10")), Allocation(key(1), 2, Decimal("10"))]

        with pytest.raises(ValueError, match="over-allocated"):
            monitoring_overlaps(holdings, over, portfolios, memberships, prices)


class TestContributionsSumToTheMove:
    """§10's "explain today's move". The parts add to the whole or there is no report."""

    def penny_scenario(
        self,
    ) -> tuple[
        list[Holding],
        list[Allocation],
        dict[int, Portfolio],
        dict[HoldingKey, Decimal],
        dict[HoldingKey, Decimal],
    ]:
        """Ten holdings that each moved seven paise and one that fell twenty-one.

        Chosen because 0.07 and 0.21 have no exact binary representation: accumulating ten of the
        first and subtracting the second is the classic case where a binary total misses its own
        decimal sum, and the miss is small enough to survive a code review and large enough to
        show in a paise column.
        """
        holdings = [Holding(key(i), Decimal("1")) for i in range(1, 12)]
        allocations = [Allocation(key(i), 1, Decimal("1")) for i in range(1, 6)] + [
            Allocation(key(i), 2, Decimal("1")) for i in range(6, 11)
        ]
        portfolios = {1: capital(1, "Momentum"), 2: capital(2, "Long term")}
        values = {key(i): Decimal("1000.00") for i in range(1, 12)}
        day_changes = {key(i): Decimal("0.07") for i in range(1, 11)}
        day_changes[key(11)] = Decimal("-0.21")
        return holdings, allocations, portfolios, values, day_changes

    def test_holding_contributions_sum_exactly_to_the_total_move(self) -> None:
        report = contribution_breakdown(*self.penny_scenario())

        assert report.total_move == Decimal("0.49")
        assert sum((row.contribution for row in report.holdings), Decimal("0")) == report.total_move

    def test_portfolio_contributions_sum_exactly_to_the_total_move_as_well(self) -> None:
        """A second decomposition of the same rows, so a bug in the allocation index refuses to
        build a report rather than printing a plausible column."""
        report = contribution_breakdown(*self.penny_scenario())

        assert (
            sum((row.contribution for row in report.portfolios), Decimal("0")) == report.total_move
        )

    def test_the_unallocated_holding_gets_its_own_row(self) -> None:
        report = contribution_breakdown(*self.penny_scenario())
        unallocated = next(row for row in report.portfolios if row.portfolio_id is None)

        assert unallocated.contribution == Decimal("-0.21")

    def test_each_capital_portfolio_gets_the_sum_of_its_own_holdings(self) -> None:
        report = contribution_breakdown(*self.penny_scenario())
        by_id = {row.portfolio_id: row for row in report.portfolios}

        assert by_id[1].contribution == Decimal("0.35")
        assert by_id[2].contribution == Decimal("0.35")
        assert by_id[1].value == Decimal("5000.00")

    def test_a_report_whose_parts_miss_its_total_cannot_be_built(self) -> None:
        """The invariant lives in the type, so no future producer can skip the check."""
        with pytest.raises(ValueError, match="worse than none"):
            ContributionReport(
                total_move=Decimal("1.00"),
                holdings=(
                    HoldingContribution(
                        key=key(1),
                        portfolio_id=1,
                        value=Decimal("1000.00"),
                        contribution=Decimal("0.99"),
                    ),
                ),
                portfolios=(
                    PortfolioContribution(
                        portfolio_id=1,
                        value=Decimal("1000.00"),
                        contribution=Decimal("1.00"),
                    ),
                ),
            )

    def test_the_two_decompositions_must_agree_with_each_other(self) -> None:
        with pytest.raises(ValueError, match="two decompositions must agree"):
            ContributionReport(
                total_move=Decimal("1.00"),
                holdings=(
                    HoldingContribution(
                        key=key(1),
                        portfolio_id=1,
                        value=Decimal("1000.00"),
                        contribution=Decimal("1.00"),
                    ),
                ),
                portfolios=(
                    PortfolioContribution(
                        portfolio_id=1,
                        value=Decimal("1000.00"),
                        contribution=Decimal("0.90"),
                    ),
                ),
            )

    def test_shares_of_the_move_are_display_only_and_absent_on_a_flat_day(self) -> None:
        holdings, allocations, portfolios, values, _ = self.penny_scenario()
        flat = {holding.key: Decimal("0.00") for holding in holdings}

        report = contribution_breakdown(holdings, allocations, portfolios, values, flat)

        assert report.total_move == Decimal("0.00")
        assert report.share_of_move(Decimal("0.00")) is None

    def test_a_share_is_the_rows_part_of_the_day(self) -> None:
        report = contribution_breakdown(*self.penny_scenario())

        assert report.share_of_move(Decimal("0.49")) == Decimal("1.0000")

    def test_a_missing_day_change_raises_rather_than_counting_as_flat(self) -> None:
        """Same rule as a missing price in the ledger: a zero produces a move that is wrong and
        still balances, and balancing is the evidence a reader would trust."""
        holdings, allocations, portfolios, values, day_changes = self.penny_scenario()
        del day_changes[key(11)]

        with pytest.raises(KeyError):
            contribution_breakdown(holdings, allocations, portfolios, values, day_changes)

    def test_a_missing_value_raises_too(self) -> None:
        holdings, allocations, portfolios, values, day_changes = self.penny_scenario()
        del values[key(11)]

        with pytest.raises(KeyError):
            contribution_breakdown(holdings, allocations, portfolios, values, day_changes)

    def test_a_monitoring_view_never_gets_a_contribution_row(self) -> None:
        """§4.1: it is excluded from every total, and a total made of moves is still a total.
        Its holdings are already counted wherever they actually sit."""
        holdings, allocations, portfolios, values, day_changes = self.penny_scenario()
        portfolios[3] = monitoring(3)

        report = contribution_breakdown(holdings, allocations, portfolios, values, day_changes)

        assert 3 not in {row.portfolio_id for row in report.portfolios}

    def test_the_breakdown_is_deterministic(self) -> None:
        holdings, allocations, portfolios, values, day_changes = self.penny_scenario()

        assert contribution_breakdown(
            holdings, allocations, portfolios, values, day_changes
        ) == contribution_breakdown(
            list(reversed(holdings)), allocations, portfolios, values, day_changes
        )


class TestLaw1TouchesNothing:
    """`packages/core` touches nothing — CLAUDE.md law 1, asserted by scanning the source."""

    def test_law_1_the_module_imports_no_io(self) -> None:
        forbidden = re.findall(
            r"^\s*(?:import|from)\s+(sqlalchemy|httpx|requests|redis|asyncpg|boto3|pathlib|os)\b",
            _module_source(),
            re.MULTILINE,
        )
        assert forbidden == []

    def test_law_1_the_module_never_reads_a_clock(self) -> None:
        """Purchase eras are measured against a date, and the date is always an argument."""
        assert (
            re.findall(
                r"\b(?:datetime\.now|dt\.date\.today|date\.today|time\.time)\s*\(",
                _module_source(),
            )
            == []
        )

    def test_law_1_the_module_holds_no_randomness(self) -> None:
        """Determinism is a property of the text as well as of the tests above: a suggestion
        engine that could reach for a random number could not be reproduced by a user."""
        assert (
            re.findall(r"^\s*(?:import|from)\s+(random|secrets)\b", _module_source(), re.MULTILINE)
            == []
        )

    def test_the_module_names_no_order(self) -> None:
        """Non-negotiable #1: nothing in core may drift toward an execution path."""
        assert (
            re.findall(
                r"\b(place_order|order_type|transact_type|exchange_segment)\b", _module_source()
            )
            == []
        )

    def test_the_module_uses_no_wording_section_9_forbids(self) -> None:
        """§9: Baskfy is not SEBI-registered, so third-party content is a *model*, never a
        managed or advisory product. The vocabulary is easiest to keep out of the UI by keeping
        it out of the layer the UI reads its nouns from."""
        assert re.findall(r"\b(managed|advisory|PMS)\b", _module_source()) == []


class TestMoneyIsDecimal:
    """House rule 9 — money and prices are `numeric`, never `float`."""

    def test_the_module_never_names_float(self) -> None:
        assert re.findall(r"\bfloat\b", _module_source()) == []

    def test_suggestion_values_are_quantised_to_paise(self) -> None:
        holdings = [Holding(key(1), Decimal("3")), Holding(key(2), Decimal("3"))]
        prices = {1: Decimal("33.333"), 2: Decimal("33.333")}

        suggestions = suggest_by_sector(holdings, prices, {1: "Energy", 2: "Energy"})

        assert suggestions[0].value == Decimal("200.00")
        assert suggestions[0].value.as_tuple().exponent == -2

    def test_overlap_values_are_quantised_to_paise(self) -> None:
        holdings = [Holding(key(1), Decimal("3"))]
        portfolios = {3: monitoring(3)}

        rows = monitoring_overlaps(holdings, [], portfolios, {3: [key(1)]}, {1: Decimal("33.333")})

        assert rows[0].value == Decimal("100.00")
        assert isinstance(rows[0], MonitoringOverlap)


class TestSuggestionLimits:
    def test_a_limit_of_zero_is_refused(self) -> None:
        with pytest.raises(ValueError, match="help nobody"):
            suggest_groupings(first_run_holdings(), first_run_prices(), first_run_inputs(), limit=0)

    def test_the_limit_keeps_the_highest_ranked_suggestions(self) -> None:
        everything = suggest_groupings(
            first_run_holdings(), first_run_prices(), first_run_inputs(), limit=50
        )
        few = suggest_groupings(
            first_run_holdings(), first_run_prices(), first_run_inputs(), limit=3
        )

        assert few == everything[:3]
