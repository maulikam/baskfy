"""The EOD NAV series and the numbers read off it — `PORTFOLIO_REDESIGN.md` §5.1, §5.2, §6.2-§6.5.

These tests assert the **spec**, not the implementation (house rule 2). The two that matter most
are properties rather than values:

* **TWR is flow neutral.** The same market path with and without a mid-series cash assign must
  produce the same TWR to the digit. That property is the entire reason §5.2 asks for TWR, and a
  test that only compared a hand-computed number against this code would pass just as happily on
  an implementation that counted the transfer as profit.
* **A transfer cannot fake a drawdown recovery.** Assigning cash while a portfolio is under water
  must not create a new peak. The naive running-maximum-over-rupees implementation passes every
  drawdown test that has no flows in it, so this one has flows in it.

Numbers are chosen adversarially rather than conveniently: a series whose TWR is exactly zero
while its XIRR is around +70%, a fall to exactly 80% of a peak, paise that a machine's binary
fractions cannot represent.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
from decimal import Decimal

import pytest

from baskfy_core.allocation_ledger import (
    MetricKind,
    Portfolio,
    PortfolioKind,
    PortfolioSource,
    ReturnFigure,
    model_figure,
)
from baskfy_core.curated_accounting import CashFlow, xirr
from baskfy_core.portfolio_nav import (
    BenchmarkComparison,
    NavPoint,
    benchmark_comparison,
    consolidated_pnl,
    contributions,
    daily_pnl,
    drawdown_series,
    max_drawdown,
    since_grouped_figure,
    since_purchase_figure,
    twr_figure,
)


def _module_source() -> str:
    """The module's own source. Law 1 and house rule 9 are properties of the *text* — this file
    may not reach a database either, so the honest way to assert them is to read the text."""

    return (
        pathlib.Path(__file__).resolve().parents[1] / "src/baskfy_core/portfolio_nav.py"
    ).read_text()


STARTED_ON = dt.date(2026, 1, 2)
D0 = dt.date(2026, 1, 2)
D1 = dt.date(2026, 1, 5)
D2 = dt.date(2026, 1, 6)
D3 = dt.date(2026, 1, 7)


def portfolio(source: PortfolioSource, *, started_on: dt.date = STARTED_ON) -> Portfolio:
    return Portfolio(
        portfolio_id=1,
        name="Momentum",
        kind=PortfolioKind.CAPITAL,
        source=source,
        started_on=started_on,
    )


SUBSCRIBED = portfolio(PortfolioSource.SUBSCRIBED)
SCREEN = portfolio(PortfolioSource.MY_SCREEN)
STRATEGY = portfolio(PortfolioSource.MY_STRATEGY)
GROUP = portfolio(PortfolioSource.HOLDING_GROUP)

#: Three days of +10%. No transfers, so nothing can hide in the arithmetic.
UNDISTURBED = [
    NavPoint(D0, Decimal("100000")),
    NavPoint(D1, Decimal("110000")),
    NavPoint(D2, Decimal("121000")),
    NavPoint(D3, Decimal("133100")),
]

#: The identical market path, with ₹50,000 assigned at the close of D2 (§4.4's internal inflow).
#: Every day still gained exactly 10% on the money that was actually invested that day.
WITH_ASSIGN = [
    NavPoint(D0, Decimal("100000")),
    NavPoint(D1, Decimal("110000")),
    NavPoint(D2, Decimal("171000"), net_flow=Decimal("50000")),
    NavPoint(D3, Decimal("188100")),
]

#: And with ₹50,000 taken out instead, because a withdrawal is the same mistake mirrored.
WITH_WITHDRAWAL = [
    NavPoint(D0, Decimal("100000")),
    NavPoint(D1, Decimal("110000")),
    NavPoint(D2, Decimal("71000"), net_flow=Decimal("-50000")),
    NavPoint(D3, Decimal("78100")),
]

THREE_TENTHS_UP = Decimal("0.331")

#: §5.2's whole point in four marks: halve the money, double down at the bottom, then double.
#: The strategy went nowhere (TWR 0) while the user's timing was excellent (XIRR near +70%).
X0 = dt.date(2025, 1, 1)
X1 = dt.date(2025, 7, 1)
X2 = dt.date(2025, 7, 2)
X3 = dt.date(2026, 1, 1)

TIMING_SERIES = [
    NavPoint(X0, Decimal("100000"), net_flow=Decimal("100000")),
    NavPoint(X1, Decimal("50000")),
    NavPoint(X2, Decimal("150000"), net_flow=Decimal("100000")),
    NavPoint(X3, Decimal("300000")),
]

#: The same events as §4.4's internal cash flows: two assigns and the terminal value.
TIMING_FLOWS = [
    CashFlow(X0, Decimal("-100000")),
    CashFlow(X2, Decimal("-100000")),
    CashFlow(X3, Decimal("300000")),
]

#: Up 25%, all the way back down, then to a new high. The trough is exactly 80% of the peak.
PEAK_AND_TROUGH = [
    NavPoint(D0, Decimal("100")),
    NavPoint(D1, Decimal("125")),
    NavPoint(D2, Decimal("100")),
    NavPoint(D3, Decimal("137.50")),
]

#: An index over the same window: +5%, and no subscriptions or redemptions, ever.
NIFTY_500 = [NavPoint(D0, Decimal("1000")), NavPoint(D3, Decimal("1050"))]


class TestTheSeriesIsOneOfficialValuePerDay:
    """ "A nightly job computes an official EOD value per portfolio" — §5.1."""

    def test_two_valuations_for_one_day_are_refused(self) -> None:
        """Picking one silently would change every number derived from the series."""
        with pytest.raises(ValueError, match="exactly one"):
            twr_figure(
                SUBSCRIBED,
                [NavPoint(D0, Decimal("100")), NavPoint(D0, Decimal("101"))],
            )

    def test_a_negative_valuation_is_refused(self) -> None:
        """v1 has no leverage; a negative mark is a P&L passed where a valuation belongs."""
        with pytest.raises(ValueError, match="cannot be negative"):
            NavPoint(D0, Decimal("-1"))

    def test_the_series_need_not_arrive_sorted(self) -> None:
        """A query with no ORDER BY is a caller inconvenience, not a wrong number."""
        shuffled = [UNDISTURBED[2], UNDISTURBED[0], UNDISTURBED[3], UNDISTURBED[1]]
        assert twr_figure(SUBSCRIBED, shuffled).value == twr_figure(SUBSCRIBED, UNDISTURBED).value

    def test_a_series_starting_before_the_portfolio_did_is_refused(self) -> None:
        """Criterion 3 ties a figure to its start date; measuring earlier reports a return the
        user never had."""
        early = [NavPoint(dt.date(2025, 12, 1), Decimal("100")), NavPoint(D1, Decimal("110"))]
        with pytest.raises(ValueError, match="before"):
            twr_figure(SUBSCRIBED, early)


class TestTwrIsFlowNeutral:
    """The property §5.2 buys by asking for TWR at all."""

    def test_a_cash_assign_does_not_change_twr(self) -> None:
        assert (
            twr_figure(SUBSCRIBED, WITH_ASSIGN).value == twr_figure(SUBSCRIBED, UNDISTURBED).value
        )

    def test_a_withdrawal_does_not_change_twr_either(self) -> None:
        assert (
            twr_figure(SUBSCRIBED, WITH_WITHDRAWAL).value
            == twr_figure(SUBSCRIBED, UNDISTURBED).value
        )

    def test_the_shared_answer_is_the_compounded_market_move(self) -> None:
        """Flow neutrality would also be satisfied by returning the same wrong number twice."""
        assert twr_figure(SUBSCRIBED, UNDISTURBED).value == THREE_TENTHS_UP

    def test_funding_a_brand_new_portfolio_is_not_a_gain(self) -> None:
        """The first assign lands in a portfolio worth nothing; it establishes the base."""
        funded = [
            NavPoint(D0, Decimal("0")),
            NavPoint(D1, Decimal("100000"), net_flow=Decimal("100000")),
            NavPoint(D2, Decimal("110000")),
        ]
        assert twr_figure(SUBSCRIBED, funded).value == Decimal("0.1")


class TestTwrAndXirrAreDifferentQuestions:
    """ "Consolidated level: show XIRR ... and TWR ... as two labelled numbers." — §5.2

    A user who halves their money, doubles down at the bottom and then doubles again has a TWR of
    exactly zero — the strategy went nowhere — and an XIRR near +70%, because their timing was
    excellent. Both are true. Showing either one alone, or unlabelled, is what §5.2 forbids.
    """

    def test_twr_says_the_strategy_went_nowhere(self) -> None:
        figure = twr_figure(portfolio(PortfolioSource.MY_STRATEGY, started_on=X0), TIMING_SERIES)
        assert figure.value == Decimal("0")

    def test_xirr_says_the_user_did_very_well(self) -> None:
        rate = xirr(TIMING_FLOWS)
        assert rate is not None
        assert rate > Decimal("0.6")

    def test_the_two_numbers_differ_and_that_is_why_both_are_shown(self) -> None:
        figure = twr_figure(portfolio(PortfolioSource.MY_STRATEGY, started_on=X0), TIMING_SERIES)
        rate = xirr(TIMING_FLOWS)
        assert figure.value != rate
        assert figure.kind is MetricKind.TWR_SINCE_GO_LIVE


class TestEveryFigureSaysWhatItIs:
    """ "Every displayed return number carries a label stating what it is ... and its start
    date" — criterion 3."""

    @pytest.mark.parametrize(
        ("source", "kind"),
        [
            (PortfolioSource.SUBSCRIBED, MetricKind.TWR_SINCE_SUBSCRIBED),
            (PortfolioSource.MY_STRATEGY, MetricKind.TWR_SINCE_GO_LIVE),
            (PortfolioSource.MY_SCREEN, MetricKind.TWR_SINCE_CREATED),
        ],
    )
    def test_each_source_gets_its_own_label(
        self, source: PortfolioSource, kind: MetricKind
    ) -> None:
        figure = twr_figure(portfolio(source), UNDISTURBED)
        assert figure.kind is kind
        assert figure.since == STARTED_ON
        assert figure.label == kind.label

    def test_a_twr_figure_is_never_the_publishers_model(self) -> None:
        """Criterion 5: this module only ever measures what the user actually experienced."""
        assert twr_figure(SUBSCRIBED, UNDISTURBED).is_model is False


class TestHoldingGroupsSayOnlyWhatTheyKnow:
    """ "Since grouped" return (from EOD marks at grouping date) ... Until then, do NOT display
    XIRR or since-purchase P&L for these." — §5.2"""

    def test_since_grouped_is_measured_from_the_grouping_date(self) -> None:
        figure = since_grouped_figure(GROUP, UNDISTURBED)
        assert figure.kind is MetricKind.SINCE_GROUPED
        assert figure.since == STARTED_ON
        assert figure.value == THREE_TENTHS_UP
        assert figure.label == "Since grouped"

    def test_since_grouped_is_flow_neutral_too(self) -> None:
        """A stock added to the group later is not a gain."""
        assert since_grouped_figure(GROUP, WITH_ASSIGN).value == THREE_TENTHS_UP

    def test_a_holding_group_is_not_offered_a_twr(self) -> None:
        with pytest.raises(ValueError, match="since grouped"):
            twr_figure(GROUP, UNDISTURBED)

    def test_only_a_holding_group_has_a_since_grouped_figure(self) -> None:
        with pytest.raises(ValueError, match="twr_figure"):
            since_grouped_figure(SCREEN, UNDISTURBED)

    def test_with_no_transaction_history_xirr_is_refused_with_a_reason(self) -> None:
        """The case §5.2 is explicit about: we know what these shares are worth, not what they
        cost. The refusal carries a sentence the user can act on, not an empty cell."""
        figure = since_purchase_figure(GROUP, [])
        assert figure.value is None
        assert figure.displayable is False
        assert figure.unavailable_reason is not None
        assert "CAS" in figure.unavailable_reason

    def test_flows_that_cannot_solve_are_refused_the_same_way(self) -> None:
        """Buys with no sell and no terminal value have no rate; a plausible number would be
        invented rather than computed."""
        buys_only = [
            CashFlow(D0, Decimal("-1000")),
            CashFlow(D1, Decimal("-1000")),
        ]
        assert since_purchase_figure(GROUP, buys_only).value is None

    def test_the_refusal_is_specific_to_holding_groups(self) -> None:
        with pytest.raises(ValueError, match="holding group"):
            since_purchase_figure(STRATEGY, [])

    def test_a_solvable_history_now_returns_a_labelled_figure(self) -> None:
        """This test asserted a `raise` until `MetricKind.XIRR_SINCE_PURCHASE` existed.

        The refusal was correct while the vocabulary lacked a member for a money-weighted return:
        the alternatives were to invent a metric name in this module, or to print an XIRR under
        the `SINCE_GROUPED` label, which is criterion 3's unlabelled column. The owning module
        added the member, so the honest behaviour is now to return the figure — and this test
        follows the spec rather than preserving the old workaround.
        """
        solvable = [
            CashFlow(dt.date(2025, 1, 1), Decimal("-100000")),
            CashFlow(dt.date(2026, 1, 1), Decimal("140000")),
        ]
        figure = since_purchase_figure(
            portfolio(PortfolioSource.HOLDING_GROUP), solvable, first_bought=dt.date(2025, 1, 1)
        )
        assert figure.kind is MetricKind.XIRR_SINCE_PURCHASE
        assert figure.displayable


class TestAnEmptySeriesIsUnknownAndNotZero:
    """Zero says "flat". An unmeasured portfolio is not flat."""

    def test_no_valuations_at_all_produces_an_unavailable_figure(self) -> None:
        figure = twr_figure(SUBSCRIBED, [])
        assert figure.value is None
        assert figure.displayable is False
        assert figure.unavailable_reason == "No end-of-day valuation for this portfolio yet"

    def test_one_valuation_says_something_different_from_none(self) -> None:
        """The user can act on the difference, so the sentences differ."""
        figure = twr_figure(SUBSCRIBED, [NavPoint(D0, Decimal("100000"))])
        assert figure.value is None
        assert figure.unavailable_reason is not None
        assert "start and an end" in figure.unavailable_reason

    def test_a_holding_group_with_no_marks_is_unavailable_too(self) -> None:
        assert since_grouped_figure(GROUP, []).value is None

    def test_an_empty_series_has_no_drawdown_and_no_pnl(self) -> None:
        assert drawdown_series([]) == []
        assert daily_pnl([]) == []
        assert max_drawdown([]) is None

    def test_one_mark_cannot_claim_a_portfolio_has_never_fallen(self) -> None:
        assert max_drawdown([NavPoint(D0, Decimal("100000"))]) is None


class TestDrawdown:
    """ "Toggles: ... drawdown." — §6.3, computed on the wealth index."""

    def test_the_drawdown_at_the_trough_is_exactly_the_fall_from_the_peak(self) -> None:
        series = drawdown_series(PEAK_AND_TROUGH)
        assert [point.drawdown for point in series] == [
            Decimal("0"),
            Decimal("0"),
            Decimal("-0.2"),
            Decimal("0"),
        ]

    def test_max_drawdown_names_the_peak_and_the_trough(self) -> None:
        worst = max_drawdown(PEAK_AND_TROUGH)
        assert worst is not None
        assert worst.drawdown == Decimal("-0.2")
        assert worst.peak_on == D1
        assert worst.trough_on == D2

    def test_a_cash_assign_cannot_fake_a_recovery(self) -> None:
        """The failure a running maximum over rupees produces: ₹1,000 assigned while 20% under
        water prints a brand new all-time high, and every later drawdown is measured from a peak
        that never existed."""
        rescued = [
            NavPoint(D0, Decimal("100")),
            NavPoint(D1, Decimal("125")),
            NavPoint(D2, Decimal("100")),
            NavPoint(D3, Decimal("1100"), net_flow=Decimal("1000")),
        ]
        assert drawdown_series(rescued)[-1].drawdown == Decimal("-0.2")
        worst = max_drawdown(rescued)
        assert worst is not None
        assert worst.drawdown == Decimal("-0.2")

    def test_a_series_that_only_rose_has_a_drawdown_of_zero(self) -> None:
        """A real answer, not an absence: the portfolio genuinely never fell."""
        worst = max_drawdown(UNDISTURBED)
        assert worst is not None
        assert worst.drawdown == Decimal("0")
        assert worst.peak_on == worst.trough_on == D0

    def test_the_index_is_the_growth_of_one_rupee_and_ignores_transfers(self) -> None:
        assert [point.index for point in drawdown_series(WITH_ASSIGN)] == [
            point.index for point in drawdown_series(UNDISTURBED)
        ]


class TestDailyPnl:
    """ "Today's P&L (₹ and %, vs previous close)" — §6.2."""

    def test_the_move_is_measured_against_the_previous_close(self) -> None:
        rows = daily_pnl(UNDISTURBED)
        assert [row.on for row in rows] == [D1, D2, D3]
        assert rows[0].amount == Decimal("10000.00")
        assert rows[0].pct == Decimal("0.1")

    def test_assigning_cash_is_not_a_profit(self) -> None:
        """The same rule §4.5 states for a split, applied to the other non-event that changes a
        portfolio's value."""
        rows = daily_pnl(
            [
                NavPoint(D0, Decimal("100000")),
                NavPoint(D1, Decimal("150000"), net_flow=Decimal("50000")),
            ]
        )
        assert rows[0].amount == Decimal("0.00")
        assert rows[0].pct == Decimal("0")

    def test_the_first_mark_is_not_a_flat_day(self) -> None:
        assert len(daily_pnl(UNDISTURBED)) == len(UNDISTURBED) - 1

    def test_a_portfolio_worth_nothing_yesterday_has_no_percentage_today(self) -> None:
        rows = daily_pnl(
            [
                NavPoint(D0, Decimal("0")),
                NavPoint(D1, Decimal("100000"), net_flow=Decimal("100000")),
            ]
        )
        assert rows[0].pct is None
        assert rows[0].amount == Decimal("0.00")


class TestContribution:
    """ "per-portfolio contribution" — §6.2 and §6.5."""

    def test_the_parts_sum_to_the_consolidated_move(self) -> None:
        moves = {7: Decimal("1234.56"), 3: Decimal("-234.56"), 11: Decimal("0.01")}
        rows = contributions(moves)
        assert sum((row.amount for row in rows), Decimal("0")) == consolidated_pnl(moves)

    def test_rows_come_back_in_a_stable_order(self) -> None:
        rows = contributions({7: Decimal("1"), 3: Decimal("2"), 11: Decimal("3")})
        assert [row.portfolio_id for row in rows] == [3, 7, 11]

    def test_offsetting_portfolios_keep_their_true_shares(self) -> None:
        """One portfolio gains ₹1,000 while another loses ₹900: the consolidated move is ₹100 and
        the shares are +10 and -9. Normalising them to a prettier chart would state a fact that
        did not happen."""
        rows = contributions({1: Decimal("1000"), 2: Decimal("-900")})
        assert [row.share for row in rows] == [Decimal("10"), Decimal("-9")]

    def test_a_share_of_a_zero_move_is_unknown_rather_than_zero(self) -> None:
        rows = contributions({1: Decimal("500"), 2: Decimal("-500")})
        assert [row.share for row in rows] == [None, None]


class TestBenchmark:
    """ "benchmark overlay (default Nifty 500)" — §6.3; "benchmark diff" — §7."""

    def test_the_three_numbers_stay_three_numbers(self) -> None:
        figure = twr_figure(SUBSCRIBED, UNDISTURBED)
        comparison = benchmark_comparison(figure, NIFTY_500, name="Nifty 500")
        assert comparison.portfolio is figure
        assert comparison.benchmark.value == Decimal("0.05")
        assert comparison.difference == Decimal("0.281")

    def test_there_is_no_way_to_render_a_comparison_as_the_return(self) -> None:
        """The type has no single value, on purpose: a benchmark is a second line, never
        something folded into the portfolio's own number."""
        assert not hasattr(BenchmarkComparison, "value")

    def test_the_benchmark_carries_the_same_window_as_the_portfolio(self) -> None:
        figure = twr_figure(SUBSCRIBED, UNDISTURBED)
        comparison = benchmark_comparison(figure, NIFTY_500, name="Nifty 500")
        assert comparison.benchmark.kind is figure.kind
        assert comparison.benchmark.since == figure.since
        assert comparison.benchmark.is_model is False

    def test_criterion_5_a_model_figure_cannot_be_compared(self) -> None:
        """The difference would be labelled "vs Nifty 500" and read as this user's gap."""
        with pytest.raises(ValueError, match="model"):
            benchmark_comparison(
                model_figure(SUBSCRIBED, Decimal("0.4")), NIFTY_500, name="Nifty 500"
            )

    def test_a_benchmark_series_with_a_cash_flow_is_a_portfolio_in_disguise(self) -> None:
        with pytest.raises(ValueError, match="index has no subscriptions"):
            benchmark_comparison(
                twr_figure(SUBSCRIBED, UNDISTURBED),
                [NavPoint(D0, Decimal("1000")), NavPoint(D3, Decimal("1050"), Decimal("5"))],
                name="Nifty 500",
            )

    def test_a_longer_benchmark_window_is_refused(self) -> None:
        with pytest.raises(ValueError, match="two different windows"):
            benchmark_comparison(
                twr_figure(SUBSCRIBED, UNDISTURBED),
                [NavPoint(dt.date(2025, 12, 1), Decimal("1000")), NavPoint(D3, Decimal("1050"))],
                name="Nifty 500",
            )

    def test_an_unavailable_portfolio_figure_produces_an_unavailable_difference(self) -> None:
        comparison = benchmark_comparison(twr_figure(SUBSCRIBED, []), NIFTY_500, name="Nifty 500")
        assert comparison.difference is None
        assert comparison.unavailable_reason == "No end-of-day valuation for this portfolio yet"

    def test_a_missing_benchmark_history_says_so(self) -> None:
        comparison = benchmark_comparison(twr_figure(SUBSCRIBED, UNDISTURBED), [], name="Nifty 500")
        assert comparison.difference is None
        assert comparison.unavailable_reason == "No Nifty 500 history for this period yet"

    def test_a_comparison_with_no_difference_must_say_why(self) -> None:
        figure = ReturnFigure(kind=MetricKind.SINCE_GROUPED, since=STARTED_ON, value=Decimal("0"))
        with pytest.raises(ValueError, match="must say why"):
            BenchmarkComparison(name="Nifty 500", portfolio=figure, benchmark=figure)


class TestMoneyIsDecimal:
    """House rule 9 — money and prices are `numeric`, never binary fractions."""

    def test_the_module_never_names_the_forbidden_type(self) -> None:
        """House rule 9 is about *this* module, so scan it — asserting that Decimal times a
        machine fraction raises would only be testing Python."""
        assert re.findall(r"\bfloat\b", _module_source()) == []

    def test_paise_survive_a_subtraction_that_would_drift(self) -> None:
        """1000.30 - 1000.10 is 0.19999999999999996 in a machine's binary fractions. The rupee
        column on §6.2's hero card is the number a user compares against their broker's app."""
        rows = daily_pnl([NavPoint(D0, Decimal("1000.10")), NavPoint(D1, Decimal("1000.30"))])
        assert rows[0].amount == Decimal("0.20")
        assert rows[0].amount.as_tuple().exponent == -2

    def test_a_tenth_and_a_fifth_add_to_exactly_three_tenths(self) -> None:
        assert consolidated_pnl({1: Decimal("0.10"), 2: Decimal("0.20")}) == Decimal("0.30")

    def test_p_and_l_amounts_are_quantised_to_paise(self) -> None:
        rows = daily_pnl([NavPoint(D0, Decimal("3")), NavPoint(D1, Decimal("3.3333"))])
        assert rows[0].amount == Decimal("0.33")


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
        """Every number here is a number *as of a date*, so the date is always an argument."""
        assert (
            re.findall(r"\b(?:datetime\.now|dt\.date\.today|time\.time)\s*\(", _module_source())
            == []
        )

    def test_the_module_names_no_order(self) -> None:
        """Non-negotiable #1: nothing in core may drift toward an execution path. The same guard
        `allocation_ledger` and `portfolio_units` carry, for the same reason."""
        assert (
            re.findall(
                r"\b(place_order|order_type|transact_type|exchange_segment)\b", _module_source()
            )
            == []
        )

    def test_the_metric_vocabulary_is_imported_and_not_restated(self) -> None:
        """One MetricKind, in the module that owns it. A second copy here is how two surfaces
        come to label the same number differently."""
        assert (
            re.findall(
                r"^class\s+(MetricKind|ReturnFigure|PortfolioSource)\b",
                _module_source(),
                re.MULTILINE,
            )
            == []
        )


class TestSincePurchaseNowResolves:
    """The parent added `MetricKind.XIRR_SINCE_PURCHASE` after this leaf reported.

    B2 refused to label a solvable money-weighted return `SINCE_GROUPED` and raised instead,
    because inventing a metric name in this module would have let two surfaces drift apart about
    one number. That was the right refusal, and the fix belonged to the module that owns the
    vocabulary. These tests cover the path the fix opened.
    """

    def test_a_solvable_history_returns_a_properly_labelled_xirr(self) -> None:
        portfolio = GROUP
        flows = [
            CashFlow(on=dt.date(2024, 1, 1), amount=Decimal("-100000")),
            CashFlow(on=dt.date(2026, 1, 1), amount=Decimal("130000")),
        ]

        figure = since_purchase_figure(portfolio, flows, first_bought=dt.date(2024, 1, 1))

        assert figure.kind is MetricKind.XIRR_SINCE_PURCHASE
        assert figure.displayable
        assert figure.value is not None
        assert figure.label == "XIRR since purchase"

    def test_it_is_measured_from_the_purchase_date_not_the_grouping_date(self) -> None:
        """The two dates tell different stories, and §5.2 measures from the money going in."""
        portfolio = GROUP
        bought = dt.date(2024, 1, 1)
        flows = [
            CashFlow(on=bought, amount=Decimal("-100000")),
            CashFlow(on=dt.date(2026, 1, 1), amount=Decimal("130000")),
        ]

        figure = since_purchase_figure(portfolio, flows, first_bought=bought)

        assert figure.since == bought
        assert figure.since != portfolio.started_on

    def test_a_solvable_history_with_no_purchase_date_is_refused(self) -> None:
        """Labelling it with the grouping date would be a label that lies."""
        portfolio = GROUP
        flows = [
            CashFlow(on=dt.date(2024, 1, 1), amount=Decimal("-100000")),
            CashFlow(on=dt.date(2026, 1, 1), amount=Decimal("130000")),
        ]

        with pytest.raises(ValueError, match="no first-bought date"):
            since_purchase_figure(portfolio, flows)

    def test_no_history_still_refuses_and_still_says_why(self) -> None:
        """The §5.2 refusal must survive the fix: it is the pre-CAS state of every
        holding group, and the fix must not have quietly removed it."""
        figure = since_purchase_figure(GROUP, [])
        assert not figure.displayable
        assert figure.unavailable_reason is not None
        assert "CAS" in figure.unavailable_reason
