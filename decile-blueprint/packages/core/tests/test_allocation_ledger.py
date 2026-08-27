"""The allocation ledger's rules — `PORTFOLIO_REDESIGN.md` §4, §5.2 and §11.

These tests assert the **spec**, not the implementation (house rule 2). Where the spec numbers an
acceptance criterion, the test is named after it, so a reader can go from "criterion 4" in the
document to the thing that proves it without searching.

Numbers are chosen adversarially rather than conveniently: a 1:3 split on a price that does not
divide evenly, a monitoring view that overlaps a capital portfolio completely, a sell larger than
the recorded position. A test that only exercises the easy case proves the easy case.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
from decimal import Decimal

import pytest

from baskfy_core.allocation_ledger import (
    UNALLOCATED,
    Allocation,
    CorporateAction,
    CorporateActionKind,
    DetectedSell,
    Holding,
    HoldingKey,
    MetricKind,
    Portfolio,
    PortfolioKind,
    PortfolioSource,
    ReconciliationItem,
    ReconciliationReason,
    ReturnFigure,
    SellAttribution,
    allocation_of,
    apply_corporate_action,
    attribute_sell,
    consolidated_value,
    cost_basis,
    headline_metric,
    holding_value,
    model_figure,
    portfolio_value,
    portfolio_values,
    unallocated_holdings,
    validate_allocations,
)


def _module_source() -> str:
    """The ledger's own source. Several rules here are properties of the text (law 1, house rules
    3 and 9), and the honest way to assert a property of the text is to read it."""

    return (
        pathlib.Path(__file__).resolve().parents[1] / "src/baskfy_core/allocation_ledger.py"
    ).read_text()


GROUPED_ON = dt.date(2026, 1, 2)

HDFC = HoldingKey(instrument_id=1, broker_account_id=10)
TCS = HoldingKey(instrument_id=2, broker_account_id=10)
#: The same instrument at a second broker — a *different* holding (§6.7).
HDFC_AT_UPSTOX = HoldingKey(instrument_id=1, broker_account_id=20)


def capital(portfolio_id: int, name: str = "Long term") -> Portfolio:
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


class TestCriterion1NetWorthBalances:
    """ "Sum of all capital portfolios + unallocated (stocks + cash) equals consolidated net
    worth, to the paisa, at all times." — §11.1"""

    def test_criterion_1_parts_sum_to_the_whole_to_the_paisa(self) -> None:
        holdings = [
            Holding(HDFC, Decimal("13")),
            Holding(TCS, Decimal("7")),
            Holding(HDFC_AT_UPSTOX, Decimal("11")),
        ]
        allocations = [
            Allocation(HDFC, 1),
            Allocation(TCS, 2),
            # HDFC_AT_UPSTOX deliberately has no row at all: absence means Unallocated.
        ]
        portfolios = {1: capital(1), 2: capital(2, "Momentum")}
        # Prices that do not divide evenly, so a float implementation drifts.
        prices = {1: Decimal("1666.67"), 2: Decimal("3333.33")}
        cash = Decimal("1234.56")

        one = portfolio_value(1, holdings, allocations, prices)
        two = portfolio_value(2, holdings, allocations, prices)
        un = portfolio_value(UNALLOCATED, holdings, allocations, prices)
        whole = consolidated_value(holdings, allocations, portfolios, prices, cash=cash)

        assert one + two + un + cash == whole

    def test_criterion_1_holds_when_everything_is_unallocated(self) -> None:
        """The first-run state (§6.6): a broker connects and nothing is sorted yet."""
        holdings = [Holding(HDFC, Decimal("13")), Holding(TCS, Decimal("7"))]
        prices = {1: Decimal("1666.67"), 2: Decimal("3333.33")}

        un = portfolio_value(UNALLOCATED, holdings, [], prices)
        whole = consolidated_value(holdings, [], {}, prices)

        assert un == whole

    def test_net_worth_counts_the_same_instrument_at_two_brokers_once_each(self) -> None:
        """Two brokers, one stock: two holdings, both counted, neither merged nor doubled."""
        holdings = [Holding(HDFC, Decimal("200")), Holding(HDFC_AT_UPSTOX, Decimal("120"))]
        prices = {1: Decimal("100")}

        assert consolidated_value(holdings, [], {}, prices) == Decimal("32000.00")

    def test_a_missing_price_raises_rather_than_valuing_at_zero(self) -> None:
        """A zero would produce a total that is wrong and still balances."""
        with pytest.raises(KeyError, match="no price for instrument"):
            holding_value(Holding(HDFC, Decimal("1")), {})


class TestPortfolioValuesTable:
    """§6.5 draws a row per portfolio, so it needs every value at once."""

    def test_every_capital_portfolio_appears_even_with_nothing_allocated(self) -> None:
        """A caller rendering the table must not have to guess whether a missing key means zero
        or means the portfolio was deleted."""
        values = portfolio_values(
            [Holding(HDFC, Decimal("10"))],
            [Allocation(HDFC, 1)],
            {1: capital(1), 2: capital(2, "Momentum")},
            {1: Decimal("100")},
        )
        assert values[1] == Decimal("1000.00")
        assert values[2] == Decimal("0.00")
        assert values[UNALLOCATED] == Decimal("0.00")

    def test_a_monitoring_view_gets_no_row_in_the_totals(self) -> None:
        values = portfolio_values(
            [Holding(HDFC, Decimal("10"))],
            [Allocation(HDFC, 1)],
            {1: capital(1), 9: monitoring(9)},
            {1: Decimal("100")},
        )
        assert 9 not in values

    def test_the_table_sums_to_consolidated_net_worth(self) -> None:
        """Criterion 1 again, via the path the page actually uses."""
        holdings = [
            Holding(HDFC, Decimal("13")),
            Holding(TCS, Decimal("7")),
            Holding(HDFC_AT_UPSTOX, Decimal("11")),
        ]
        allocations = [Allocation(HDFC, 1), Allocation(TCS, 2)]
        portfolios = {1: capital(1), 2: capital(2, "Momentum"), 9: monitoring(9)}
        prices = {1: Decimal("1666.67"), 2: Decimal("3333.33")}

        values = portfolio_values(holdings, allocations, portfolios, prices)
        whole = consolidated_value(holdings, allocations, portfolios, prices)

        assert sum(values.values()) == whole

    def test_the_table_refuses_an_illegal_allocation_set(self) -> None:
        with pytest.raises(ValueError, match="exactly one capital portfolio"):
            portfolio_values(
                [Holding(HDFC, Decimal("1"))],
                [Allocation(HDFC, 1), Allocation(HDFC, 2)],
                {1: capital(1), 2: capital(2, "Momentum")},
                {1: Decimal("100")},
            )


class TestCriterion2OneCapitalPortfolio:
    """ "A holding can never be in two capital portfolios; monitoring views never affect any
    total." — §11.2"""

    def test_criterion_2_a_holding_in_two_capital_portfolios_is_refused(self) -> None:
        allocations = [Allocation(HDFC, 1), Allocation(HDFC, 2)]
        with pytest.raises(ValueError, match="exactly one capital portfolio"):
            validate_allocations(allocations, {1: capital(1), 2: capital(2, "Momentum")})

    def test_criterion_2_the_same_instrument_at_two_brokers_is_not_a_double_allocation(
        self,
    ) -> None:
        """The rule is about a *position*, not a stock; allocating each broker's lot apart is
        legal and is what makes a per-broker sell attributable."""
        validate_allocations(
            [Allocation(HDFC, 1), Allocation(HDFC_AT_UPSTOX, 2)],
            {1: capital(1), 2: capital(2, "Momentum")},
        )

    def test_criterion_2_a_monitoring_view_holds_no_allocation(self) -> None:
        with pytest.raises(ValueError, match="monitoring view"):
            validate_allocations([Allocation(HDFC, 9)], {9: monitoring(9)})

    def test_criterion_2_monitoring_views_do_not_change_a_total(self) -> None:
        """A lens overlapping a capital portfolio *completely* must move nothing."""
        holdings = [Holding(HDFC, Decimal("10"))]
        allocations = [Allocation(HDFC, 1)]
        prices = {1: Decimal("1000")}

        without = consolidated_value(holdings, allocations, {1: capital(1)}, prices)
        with_lens = consolidated_value(
            holdings, allocations, {1: capital(1), 9: monitoring(9)}, prices
        )

        assert without == with_lens == Decimal("10000.00")

    def test_an_allocation_to_a_portfolio_that_does_not_exist_is_refused(self) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            validate_allocations([Allocation(HDFC, 77)], {})

    def test_consolidated_value_validates_before_it_sums(self) -> None:
        """The invalid set must never produce a number, however plausible."""
        with pytest.raises(ValueError, match="exactly one capital portfolio"):
            consolidated_value(
                [Holding(HDFC, Decimal("1"))],
                [Allocation(HDFC, 1), Allocation(HDFC, 2)],
                {1: capital(1), 2: capital(2, "Momentum")},
                {1: Decimal("100")},
            )

    def test_criterion_2_a_single_portfolio_value_refuses_an_illegal_set_too(self) -> None:
        """The hole this closes: validating only on the consolidated path let one row return a
        number while the total refused, so the parts and the whole disagreed about whether the
        data was even legal."""
        with pytest.raises(ValueError, match="exactly one capital portfolio"):
            portfolio_value(
                1,
                [Holding(HDFC, Decimal("1"))],
                [Allocation(HDFC, 1), Allocation(HDFC, 2)],
                {1: Decimal("100")},
            )

    def test_a_holding_with_no_allocation_row_is_unallocated(self) -> None:
        assert allocation_of([], HDFC) is UNALLOCATED
        assert unallocated_holdings([Holding(HDFC, Decimal("1"))], []) == [
            Holding(HDFC, Decimal("1"))
        ]


class TestCriterion4SellAttribution:
    """ "A sell detected by sync either auto-attributes (whole-holding case) or creates a
    reconciliation item — it never silently alters a return series." — §11.4"""

    def test_criterion_4_a_whole_allocated_holding_attributes_silently(self) -> None:
        outcome = attribute_sell(
            DetectedSell(HDFC, Decimal("100")),
            [Holding(HDFC, Decimal("320"))],
            [Allocation(HDFC, 1)],
        )
        assert outcome.attributed
        assert outcome.portfolio_id == 1
        assert outcome.item is None

    def test_criterion_4_an_unallocated_holding_asks_instead_of_guessing(self) -> None:
        outcome = attribute_sell(
            DetectedSell(HDFC, Decimal("100")), [Holding(HDFC, Decimal("320"))], []
        )
        assert not outcome.attributed
        assert outcome.item is not None
        assert outcome.item.reason is ReconciliationReason.UNALLOCATED_HOLDING
        assert "which portfolio" in outcome.item.question.lower()

    def test_criterion_4_selling_more_than_we_recorded_asks_rather_than_absorbing_it(
        self,
    ) -> None:
        """Attributing the excess to the one portfolio we know would corrupt its return series
        with volume it never held."""
        outcome = attribute_sell(
            DetectedSell(HDFC, Decimal("400")),
            [Holding(HDFC, Decimal("320"))],
            [Allocation(HDFC, 1)],
        )
        assert not outcome.attributed
        assert outcome.item is not None
        assert outcome.item.reason is ReconciliationReason.QUANTITY_MISMATCH
        # The known portfolio is *suggested*, never applied.
        assert outcome.item.suggested_portfolio_id == 1

    def test_criterion_4_a_holding_we_have_never_seen_asks(self) -> None:
        outcome = attribute_sell(DetectedSell(HDFC, Decimal("1")), [], [])
        assert outcome.item is not None
        assert outcome.item.reason is ReconciliationReason.UNKNOWN_INFLOW

    def test_criterion_4_there_is_no_third_outcome(self) -> None:
        """The type refuses both-at-once and neither, so no caller can invent a quiet guess."""
        with pytest.raises(ValueError, match="never both and never neither"):
            SellAttribution()
        with pytest.raises(ValueError, match="never both and never neither"):
            SellAttribution(
                portfolio_id=1,
                item=ReconciliationItem(
                    key=HDFC,
                    quantity=Decimal("1"),
                    reason=ReconciliationReason.UNALLOCATED_HOLDING,
                ),
            )

    def test_a_sell_must_actually_reduce_a_position(self) -> None:
        with pytest.raises(ValueError, match="must reduce a position"):
            DetectedSell(HDFC, Decimal("0"))


class TestCriterion5ModelAndActualNeverBlend:
    """ "Model performance and the user's actual performance are never combined into one
    figure." — §11.5"""

    def test_criterion_5_a_model_figure_is_labelled_as_the_publishers(self) -> None:
        subscribed = Portfolio(
            portfolio_id=1,
            name="Momentum 30",
            kind=PortfolioKind.CAPITAL,
            source=PortfolioSource.SUBSCRIBED,
            started_on=GROUPED_ON,
        )
        actual = headline_metric(subscribed, Decimal("12.5"))
        model = model_figure(subscribed, Decimal("18.9"))

        assert actual.is_model is False
        assert model.is_model is True
        assert actual.label != model.label
        assert model.label.startswith("Model ")

    def test_criterion_5_figures_cannot_be_added_together(self) -> None:
        """Blending is not merely discouraged, it is *absent*: the type defines no arithmetic, so
        `model + actual` does not typecheck and does not run. Asserted as absence rather than by
        provoking a TypeError, which would need a suppression comment house rule 3 forbids."""
        one = ReturnFigure(kind=MetricKind.SINCE_GROUPED, since=GROUPED_ON, value=Decimal("1"))
        for operation in ("__add__", "__radd__", "__iadd__", "__sub__"):
            assert not hasattr(one, operation), (
                f"ReturnFigure grew {operation}; combining a model figure with a user's actual "
                "return is exactly what acceptance criterion 5 forbids"
            )

    def test_criterion_5_only_a_subscribed_portfolio_has_a_model(self) -> None:
        with pytest.raises(ValueError, match="acceptance criterion 5"):
            model_figure(capital(1), Decimal("10"))


class TestCriterion6CorporateActionsAreNotPnl:
    """ "A split/bonus changes quantity and average price but produces zero P&L." — §11.6"""

    def test_criterion_6_a_split_preserves_cost_basis_exactly(self) -> None:
        """1:3 on 333.33 is the case a naive recompute gets wrong: scaling the price leaves a
        remainder that surfaces as P&L on a day the user did nothing."""
        before = Holding(HDFC, Decimal("100"), Decimal("333.33"))
        after = apply_corporate_action(
            before,
            CorporateAction(HDFC, CorporateActionKind.SPLIT, Decimal("1"), Decimal("3")),
        )

        assert after.quantity == Decimal("300.0000")
        assert cost_basis(after) == cost_basis(before)

    def test_criterion_6_a_bonus_preserves_cost_basis_exactly(self) -> None:
        before = Holding(HDFC, Decimal("7"), Decimal("1234.56"))
        after = apply_corporate_action(
            before,
            CorporateAction(HDFC, CorporateActionKind.BONUS, Decimal("1"), Decimal("5")),
        )

        assert after.quantity == Decimal("35.0000")
        assert cost_basis(after) == cost_basis(before)

    def test_criterion_6_the_average_price_does_change(self) -> None:
        """Zero P&L is not achieved by leaving the holding alone — §4.5 requires both to move."""
        before = Holding(HDFC, Decimal("100"), Decimal("333.33"))
        after = apply_corporate_action(
            before,
            CorporateAction(HDFC, CorporateActionKind.SPLIT, Decimal("1"), Decimal("3")),
        )
        assert after.quantity != before.quantity
        assert after.avg_price is not None
        assert after.avg_price != before.avg_price

    def test_an_unknown_average_price_stays_unknown_after_a_split(self) -> None:
        """Inventing one would unlock a since-purchase P&L that §5.2 forbids."""
        after = apply_corporate_action(
            Holding(HDFC, Decimal("100")),
            CorporateAction(HDFC, CorporateActionKind.SPLIT, Decimal("1"), Decimal("3")),
        )
        assert after.avg_price is None
        assert after.quantity == Decimal("300.0000")

    def test_a_corporate_action_never_zeroes_a_position(self) -> None:
        with pytest.raises(ValueError, match="never removes a position"):
            apply_corporate_action(
                Holding(HDFC, Decimal("0.0001")),
                CorporateAction(HDFC, CorporateActionKind.SPLIT, Decimal("100000"), Decimal("1")),
            )

    def test_ratios_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="must both be positive"):
            CorporateAction(HDFC, CorporateActionKind.SPLIT, Decimal("0"), Decimal("3"))


class TestHeadlineMetricPerSource:
    """§5.2 — "The return metric differs by source — never one unlabelled column"."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            (PortfolioSource.SUBSCRIBED, MetricKind.TWR_SINCE_SUBSCRIBED),
            (PortfolioSource.MY_STRATEGY, MetricKind.TWR_SINCE_GO_LIVE),
            (PortfolioSource.MY_SCREEN, MetricKind.TWR_SINCE_CREATED),
            (PortfolioSource.HOLDING_GROUP, MetricKind.SINCE_GROUPED),
        ],
    )
    def test_every_source_has_its_own_headline_metric(
        self, source: PortfolioSource, expected: MetricKind
    ) -> None:
        portfolio = Portfolio(
            portfolio_id=1,
            name="X",
            kind=PortfolioKind.CAPITAL,
            source=source,
            started_on=GROUPED_ON,
        )
        figure = headline_metric(portfolio, Decimal("5"))
        assert figure.kind is expected
        assert figure.since == GROUPED_ON
        assert figure.label

    def test_a_holding_group_without_history_says_why_rather_than_showing_zero(self) -> None:
        """§5.2: before a CAS import, do NOT display XIRR or since-purchase P&L."""
        figure = headline_metric(capital(1), None, has_transaction_history=False)
        assert figure.kind is MetricKind.SINCE_GROUPED
        assert not figure.displayable
        assert figure.unavailable_reason is not None
        assert "CAS" in figure.unavailable_reason

    def test_a_figure_with_no_value_must_carry_a_reason(self) -> None:
        """An empty cell with no reason leaves the user unable to tell unknown from zero."""
        with pytest.raises(ValueError, match="must say why"):
            ReturnFigure(kind=MetricKind.SINCE_GROUPED, since=GROUPED_ON)

    def test_a_figure_cannot_be_both_valued_and_unavailable(self) -> None:
        with pytest.raises(ValueError, match="cannot both have a value"):
            ReturnFigure(
                kind=MetricKind.SINCE_GROUPED,
                since=GROUPED_ON,
                value=Decimal("1"),
                unavailable_reason="nope",
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
        """Every rule here is a rule *about a moment*, so the moment is always an argument."""
        assert (
            re.findall(r"\b(?:datetime\.now|dt\.date\.today|time\.time)\s*\(", _module_source())
            == []
        )

    def test_the_module_names_no_order(self) -> None:
        """Non-negotiable #1: nothing in core may drift toward an execution path. The same guard
        `portfolio_units` carries, for the same reason."""
        # `sell` is legitimate vocabulary here (§4.3 detects sells); an *order* is not.
        assert (
            re.findall(
                r"\b(place_order|order_type|transact_type|exchange_segment)\b", _module_source()
            )
            == []
        )


class TestMoneyIsDecimal:
    """House rule 9 — money and prices are `numeric`, never `float`."""

    def test_the_module_never_names_float(self) -> None:
        """House rule 9 is about *this* module, so scan it — asserting that `Decimal * float`
        raises would only be testing Python."""
        assert re.findall(r"\bfloat\b", _module_source()) == []

    def test_values_are_quantised_to_paise(self) -> None:
        value = holding_value(Holding(HDFC, Decimal("3")), {1: Decimal("33.333")})
        assert value == Decimal("100.00")
        assert value.as_tuple().exponent == -2
