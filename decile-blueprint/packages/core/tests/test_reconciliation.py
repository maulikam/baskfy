"""The reconciliation inbox's rules — `PORTFOLIO_REDESIGN.md` §4.3, §4.5, §6.4 and §11.

These assert the **spec**, not the implementation (house rule 2). Where the spec numbers an
acceptance criterion the test is named after it, so a reader can go from "criterion 4" in the
document to the thing that proves it without searching.

The setup is deliberately hostile to the easy answer: two capital portfolios rather than one, so
"freezes the right portfolio" can fail by freezing everything; the same instrument at two brokers,
so a freeze keyed on the instrument rather than the physical position shows up; a monitoring view
that overlaps a capital portfolio completely, so a corporate-action fan-out that stops at the
capital row is visible as a disagreement rather than as a rounding difference. A test that only
exercises one portfolio proves one portfolio.
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
    Portfolio,
    PortfolioKind,
    PortfolioSource,
    ReconciliationItem,
    ReconciliationReason,
    attribute_sell,
    cost_basis,
)
from baskfy_core.reconciliation import (
    AttentionInputs,
    AttentionItem,
    AttentionKind,
    InboxEntry,
    LedgerPosition,
    ReconciliationState,
    attention_items,
    dismiss,
    entry_for,
    fan_out_corporate_action,
    freeze_report,
    open_entries,
    positions_from_allocations,
    resolve,
)


def _module_source() -> str:
    """The module's own source. Law 1 and house rule 9 are properties of the *text*, and the
    honest way to assert a property of the text is to read it."""

    return (
        pathlib.Path(__file__).resolve().parents[1] / "src/baskfy_core/reconciliation.py"
    ).read_text()


TODAY = dt.date(2026, 8, 25)
GROUPED_ON = dt.date(2026, 1, 2)

HDFC = HoldingKey(instrument_id=1, broker_account_id=10)
TCS = HoldingKey(instrument_id=2, broker_account_id=10)
#: The same instrument at a second broker — a *different* physical position (§6.7).
HDFC_AT_UPSTOX = HoldingKey(instrument_id=1, broker_account_id=20)

ONE = Decimal("1")

LONG_TERM = 1
MOMENTUM = 2
DEFENCE_LENS = 3


def capital(portfolio_id: int, name: str = "Long term") -> Portfolio:
    return Portfolio(
        portfolio_id=portfolio_id,
        name=name,
        kind=PortfolioKind.CAPITAL,
        source=PortfolioSource.HOLDING_GROUP,
        started_on=GROUPED_ON,
    )


def monitoring(portfolio_id: int = DEFENCE_LENS, name: str = "All defence stocks") -> Portfolio:
    return Portfolio(
        portfolio_id=portfolio_id,
        name=name,
        kind=PortfolioKind.MONITORING,
        source=PortfolioSource.HOLDING_GROUP,
        started_on=GROUPED_ON,
    )


PORTFOLIOS = {
    LONG_TERM: capital(LONG_TERM),
    MOMENTUM: capital(MOMENTUM, "Momentum"),
    DEFENCE_LENS: monitoring(),
}


def open_entry(
    key: HoldingKey = HDFC,
    reason: ReconciliationReason = ReconciliationReason.UNALLOCATED_HOLDING,
    item_id: int = 100,
    quantity: str = "100",
    suggested: int | None = None,
) -> InboxEntry:
    return InboxEntry(
        item_id=item_id,
        item=ReconciliationItem(
            key=key,
            quantity=Decimal(quantity),
            reason=reason,
            suggested_portfolio_id=suggested,
        ),
        detected_on=TODAY,
    )


class TestTheInboxLifecycle:
    """§4.3 plus migration 0022: three states, and RESOLVED must name a portfolio."""

    def test_the_states_are_exactly_the_ones_migration_0022_allows(self) -> None:
        """0022's ``reconciliation_item_state_known`` is the contract; a fourth state here would
        be a row the database refuses to store."""
        assert {state.value for state in ReconciliationState} == {"OPEN", "RESOLVED", "DISMISSED"}

    def test_a_resolved_item_without_a_portfolio_is_rejected(self) -> None:
        """Otherwise "resolved" means only that somebody clicked something, and a return series
        moves on a decision nobody recorded."""
        with pytest.raises(ValueError, match="must name the portfolio it resolved to"):
            InboxEntry(
                item_id=1,
                item=ReconciliationItem(
                    HDFC, Decimal("100"), ReconciliationReason.UNALLOCATED_HOLDING
                ),
                detected_on=TODAY,
                state=ReconciliationState.RESOLVED,
            )

    def test_an_unresolved_item_naming_a_portfolio_is_rejected_too(self) -> None:
        """The same check constraint, read in the other direction: an OPEN item that already
        names a portfolio has silently attributed itself."""
        with pytest.raises(ValueError, match="must name the portfolio it resolved to"):
            InboxEntry(
                item_id=1,
                item=ReconciliationItem(
                    HDFC, Decimal("100"), ReconciliationReason.UNALLOCATED_HOLDING
                ),
                detected_on=TODAY,
                resolved_portfolio_id=LONG_TERM,
            )

    def test_a_dismissed_item_names_no_portfolio(self) -> None:
        """ "Nothing to attribute" is an answer, and it attributes to nothing."""
        with pytest.raises(ValueError, match="must name the portfolio it resolved to"):
            InboxEntry(
                item_id=1,
                item=ReconciliationItem(
                    HDFC, Decimal("100"), ReconciliationReason.UNALLOCATED_HOLDING
                ),
                detected_on=TODAY,
                state=ReconciliationState.DISMISSED,
                resolved_portfolio_id=LONG_TERM,
            )

    def test_an_open_item_cannot_carry_a_resolution_date(self) -> None:
        with pytest.raises(ValueError, match="has not been answered on any day"):
            InboxEntry(
                item_id=1,
                item=ReconciliationItem(
                    HDFC, Decimal("100"), ReconciliationReason.UNALLOCATED_HOLDING
                ),
                detected_on=TODAY,
                resolved_on=TODAY,
            )

    def test_only_open_entries_are_open(self) -> None:
        resolved = resolve(open_entry(item_id=7), LONG_TERM, PORTFOLIOS, TODAY).entry
        dismissed = dismiss(open_entry(item_id=8), TODAY).entry
        still_open = open_entry(item_id=9)

        assert [entry.item_id for entry in open_entries([resolved, dismissed, still_open])] == [9]

    def test_the_entry_carries_the_question_the_ledger_wrote(self) -> None:
        """§4.3 wants a question, not an error code — and the wording stays owned by the ledger
        rather than being restated here where the two could drift apart."""
        entry = open_entry(reason=ReconciliationReason.QUANTITY_MISMATCH)
        assert entry.question == ReconciliationReason.QUANTITY_MISMATCH.question


class TestTheBridgeFromTheLedger:
    """The inbox sits on top of `attribute_sell`; it does not re-decide what it decided."""

    def test_a_silently_attributed_sell_creates_no_inbox_row(self) -> None:
        """§4.2 -> §4.3: whole-holding allocation makes the common case free, and an inbox padded
        with resolved-on-arrival rows buries the ones that matter."""
        holdings = [Holding(HDFC, Decimal("320"))]
        allocations = [Allocation(HDFC, LONG_TERM)]
        attribution = attribute_sell(DetectedSell(HDFC, Decimal("100")), holdings, allocations)

        assert attribution.attributed
        assert entry_for(attribution, 1, TODAY) is None

    def test_a_sell_that_cannot_be_attributed_becomes_an_open_row(self) -> None:
        holdings = [Holding(HDFC, Decimal("320"))]
        attribution = attribute_sell(DetectedSell(HDFC, Decimal("100")), holdings, [])

        entry = entry_for(attribution, 42, TODAY)

        assert entry is not None
        assert entry.item_id == 42
        assert entry.state is ReconciliationState.OPEN
        assert entry.item.reason is ReconciliationReason.UNALLOCATED_HOLDING
        assert entry.detected_on == TODAY


class TestSection43FreezesTheHoldingAndNothingElse:
    """ "Unresolved reconciliation items freeze that holding's contribution to performance
    (show as 'pending reconciliation') rather than guessing." — §4.3"""

    def test_an_open_item_freezes_exactly_the_affected_holding(self) -> None:
        holdings = [Holding(HDFC, Decimal("320")), Holding(TCS, Decimal("50"))]
        allocations = [Allocation(HDFC, LONG_TERM), Allocation(TCS, MOMENTUM)]
        positions = positions_from_allocations(holdings, allocations)

        report = freeze_report([open_entry(HDFC)], positions)

        assert report.frozen == frozenset({HDFC})
        assert report.is_frozen(HDFC)
        assert not report.is_frozen(TCS)

    def test_an_open_item_marks_its_portfolio_pending_and_not_the_others(self) -> None:
        """The blast radius is the point. A user with two portfolios and one unanswered sell
        keeps one honest headline number."""
        holdings = [Holding(HDFC, Decimal("320")), Holding(TCS, Decimal("50"))]
        allocations = [Allocation(HDFC, LONG_TERM), Allocation(TCS, MOMENTUM)]
        positions = positions_from_allocations(holdings, allocations)

        report = freeze_report([open_entry(HDFC)], positions)

        assert report.is_pending(LONG_TERM)
        assert not report.is_pending(MOMENTUM)
        assert not report.is_pending(UNALLOCATED)
        assert report.pending_portfolio_ids == frozenset({LONG_TERM})

    def test_the_same_instrument_at_another_broker_is_not_frozen(self) -> None:
        """A freeze keyed on the instrument rather than the physical position would stop a
        portfolio that has nothing to do with the question."""
        holdings = [Holding(HDFC, Decimal("200")), Holding(HDFC_AT_UPSTOX, Decimal("120"))]
        allocations = [Allocation(HDFC, LONG_TERM), Allocation(HDFC_AT_UPSTOX, MOMENTUM)]

        report = freeze_report(
            [open_entry(HDFC)], positions_from_allocations(holdings, allocations)
        )

        assert not report.is_frozen(HDFC_AT_UPSTOX)
        assert not report.is_pending(MOMENTUM)

    def test_a_monitoring_view_over_a_frozen_holding_is_pending_too(self) -> None:
        """A lens is excluded from every *total* (§4.1); it still prints a return in §6.5's muted
        tab, and a lens over a position we cannot value honestly is exactly as dishonest."""
        holdings = [Holding(HDFC, Decimal("320"))]
        allocations = [Allocation(HDFC, LONG_TERM)]
        positions = positions_from_allocations(holdings, allocations, {HDFC: [DEFENCE_LENS]})

        report = freeze_report([open_entry(HDFC)], positions)

        assert report.pending_portfolio_ids == frozenset({LONG_TERM, DEFENCE_LENS})

    def test_the_consolidated_figure_is_pending_whenever_anything_is_frozen(self) -> None:
        """Criterion 1 makes the whole the sum of the parts, so one dishonest part makes a
        dishonest whole — the one place the blast radius is deliberately total."""
        holdings = [Holding(HDFC, Decimal("320"))]
        positions = positions_from_allocations(holdings, [Allocation(HDFC, LONG_TERM)])

        assert freeze_report([open_entry(HDFC)], positions).consolidated_pending

    def test_an_unknown_inflow_marks_unallocated_pending(self) -> None:
        """Shares we cannot account for physically exist, so criterion 1 already counts them.
        The only bucket that can own something nobody assigned is Unallocated (§6.6)."""
        entry = open_entry(TCS, ReconciliationReason.UNKNOWN_INFLOW)

        report = freeze_report([entry], [])

        assert report.frozen == frozenset({TCS})
        assert report.unallocated_pending
        assert report.is_pending(UNALLOCATED)

    def test_a_suggestion_never_marks_a_portfolio_pending(self) -> None:
        """The ledger calls it "a suggestion, never an attribution". Letting it freeze a
        portfolio would attribute the item to it by the back door."""
        entry = open_entry(TCS, ReconciliationReason.UNKNOWN_INFLOW, suggested=MOMENTUM)

        assert not freeze_report([entry], []).is_pending(MOMENTUM)

    def test_nothing_is_frozen_when_the_inbox_is_empty(self) -> None:
        holdings = [Holding(HDFC, Decimal("320"))]
        positions = positions_from_allocations(holdings, [Allocation(HDFC, LONG_TERM)])

        report = freeze_report([], positions)

        assert report.frozen == frozenset()
        assert not report.consolidated_pending
        assert not report.is_pending(LONG_TERM)


class TestResolvingAnItem:
    """§4.3's answer: it unfreezes, and it says what allocation it implies."""

    def test_resolving_unfreezes_the_holding(self) -> None:
        entry = open_entry(HDFC)
        holdings = [Holding(HDFC, Decimal("320"))]
        positions = positions_from_allocations(holdings, [])

        outcome = resolve(entry, LONG_TERM, PORTFOLIOS, TODAY)
        report = freeze_report([outcome.entry], positions)

        assert outcome.unfreezes
        assert report.frozen == frozenset()
        assert not report.is_pending(LONG_TERM)
        assert not report.consolidated_pending

    def test_resolving_yields_the_allocation_it_implies(self) -> None:
        """Whole-holding allocation (§4.2) is what makes one row a complete answer."""
        outcome = resolve(open_entry(HDFC), MOMENTUM, PORTFOLIOS, TODAY)

        assert outcome.implied_allocation == Allocation(key=HDFC, portfolio_id=MOMENTUM)

    def test_resolving_records_the_portfolio_and_the_date_it_was_answered(self) -> None:
        outcome = resolve(open_entry(HDFC), MOMENTUM, PORTFOLIOS, TODAY)

        assert outcome.entry.state is ReconciliationState.RESOLVED
        assert outcome.entry.resolved_portfolio_id == MOMENTUM
        assert outcome.entry.resolved_on == TODAY

    def test_resolving_writes_nothing_and_leaves_the_original_untouched(self) -> None:
        """Law 1 makes it mandatory; it is the right shape anyway, because the allocation and the
        state change must land in one transaction with the valuations they invalidate."""
        entry = open_entry(HDFC)

        resolve(entry, LONG_TERM, PORTFOLIOS, TODAY)

        assert entry.state is ReconciliationState.OPEN
        assert entry.resolved_portfolio_id is None

    def test_resolving_to_a_monitoring_view_is_refused(self) -> None:
        """A lens holds no allocation (§4.1), so the holding would still be in no capital
        portfolio and the consolidated total would still be short."""
        with pytest.raises(ValueError, match="monitoring view holds no"):
            resolve(open_entry(HDFC), DEFENCE_LENS, PORTFOLIOS, TODAY)

    def test_resolving_to_a_portfolio_that_does_not_exist_is_refused(self) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            resolve(open_entry(HDFC), 999, PORTFOLIOS, TODAY)

    def test_an_answered_item_cannot_be_answered_again(self) -> None:
        outcome = resolve(open_entry(HDFC), LONG_TERM, PORTFOLIOS, TODAY)

        with pytest.raises(ValueError, match="already RESOLVED"):
            resolve(outcome.entry, MOMENTUM, PORTFOLIOS, TODAY)


class TestDismissingAnItem:
    """A recorded human decision is the opposite of the guess §4.3 forbids."""

    def test_dismissing_unfreezes_and_implies_no_allocation(self) -> None:
        outcome = dismiss(open_entry(HDFC), TODAY)

        assert outcome.entry.state is ReconciliationState.DISMISSED
        assert outcome.entry.resolved_portfolio_id is None
        assert outcome.implied_allocation is None
        assert freeze_report([outcome.entry], []).frozen == frozenset()

    def test_dismissing_keeps_the_row_and_its_date_for_the_activity_tab(self) -> None:
        """§7 lists reconciliation history; a question asked and waved away is part of why a
        return series looks the way it does."""
        assert dismiss(open_entry(HDFC), TODAY).entry.resolved_on == TODAY

    def test_an_answered_item_cannot_be_dismissed(self) -> None:
        outcome = dismiss(open_entry(HDFC), TODAY)

        with pytest.raises(ValueError, match="cannot be dismissed twice"):
            dismiss(outcome.entry, TODAY)


class TestSection45CorporateActionsFanOutAtomically:
    """ "...update quantity + average price across the affected holding wherever it appears (its
    capital portfolio and all monitoring views) atomically." — §4.5

    "A corporate action MUST NOT appear as a P&L event." — §4.5, criterion 6"""

    #: 1:3 on 100 shares at 333.33 — the case a naive recompute gets wrong, per the ledger's own
    #: worked example. Scaling the price leaves a remainder that surfaces as P&L on a day the
    #: user did nothing.
    SPLIT = CorporateAction(HDFC, CorporateActionKind.SPLIT, Decimal("1"), Decimal("3"))

    def positions(self) -> list[LedgerPosition]:
        """One physical position in a capital portfolio and two overlapping lenses, plus an
        unrelated holding that must not move."""
        held = Holding(HDFC, Decimal("100"), Decimal("333.33"))
        return [
            LedgerPosition(LONG_TERM, held),
            LedgerPosition(DEFENCE_LENS, held),
            LedgerPosition(4, held),
            LedgerPosition(MOMENTUM, Holding(TCS, Decimal("50"), Decimal("3000"))),
        ]

    def test_the_action_reaches_the_capital_portfolio_and_every_monitoring_view(self) -> None:
        result = fan_out_corporate_action(self.SPLIT, self.positions())

        assert set(result.touched_portfolio_ids) == {LONG_TERM, DEFENCE_LENS, 4}
        assert len(result.updated) == 3
        assert {position.holding.quantity for position in result.updated} == {Decimal("300.0000")}

    def test_criterion_6_total_cost_basis_is_unchanged_across_the_whole_fan_out(self) -> None:
        """Checked across the set, not per row: a fan-out that preserved each row and lost a
        paisa in the sum would satisfy a per-row test and still print P&L."""
        result = fan_out_corporate_action(self.SPLIT, self.positions())

        assert result.cost_basis_before == Decimal("99999.00")
        assert result.cost_basis_after == result.cost_basis_before

    def test_criterion_6_every_row_keeps_its_own_cost_basis_too(self) -> None:
        before = cost_basis(Holding(HDFC, Decimal("100"), Decimal("333.33")))
        result = fan_out_corporate_action(self.SPLIT, self.positions())

        assert [cost_basis(position.holding) for position in result.updated] == [before] * 3

    def test_the_average_price_moves_in_every_portfolio_at_once(self) -> None:
        """Zero P&L is not achieved by leaving the rows alone — §4.5 requires both to move, and
        it requires them to move together or the portfolios disagree about one position."""
        result = fan_out_corporate_action(self.SPLIT, self.positions())

        assert {position.holding.avg_price for position in result.updated} == {Decimal("111.11")}

    def test_untouched_rows_are_returned_unchanged_so_the_caller_writes_one_picture(self) -> None:
        original = self.positions()
        result = fan_out_corporate_action(self.SPLIT, original)

        assert len(result.positions) == len(original)
        assert result.positions[-1] == original[-1]

    def test_an_action_that_matches_no_row_is_refused_rather_than_ignored(self) -> None:
        """A silent no-op looks like success and leaves the position wrong from that day on."""
        with pytest.raises(ValueError, match="appears in no portfolio"):
            fan_out_corporate_action(self.SPLIT, [LedgerPosition(MOMENTUM, Holding(TCS, ONE))])

    def test_rows_that_already_disagree_about_quantity_are_refused(self) -> None:
        """Multiplying a disagreement by three encodes it into a number three times larger."""
        positions = [
            LedgerPosition(LONG_TERM, Holding(HDFC, Decimal("320"))),
            LedgerPosition(DEFENCE_LENS, Holding(HDFC, Decimal("300"))),
        ]
        with pytest.raises(ValueError, match="different quantities"):
            fan_out_corporate_action(self.SPLIT, positions)

    def test_rows_that_already_disagree_about_average_price_are_refused(self) -> None:
        positions = [
            LedgerPosition(LONG_TERM, Holding(HDFC, Decimal("100"), Decimal("333.33"))),
            LedgerPosition(DEFENCE_LENS, Holding(HDFC, Decimal("100"), Decimal("300.00"))),
        ]
        with pytest.raises(ValueError, match="different average prices"):
            fan_out_corporate_action(self.SPLIT, positions)

    def test_an_unknown_purchase_price_stays_unknown_and_claims_no_cost_basis(self) -> None:
        """§5.3: before a CAS import there is no cost basis to preserve, and ``None`` is not a
        zero — claiming zero would be a claim about a number that does not exist."""
        result = fan_out_corporate_action(
            self.SPLIT, [LedgerPosition(LONG_TERM, Holding(HDFC, Decimal("100")))]
        )

        assert result.cost_basis_before is None
        assert result.cost_basis_after is None
        assert result.updated[0].holding.avg_price is None
        assert result.updated[0].holding.quantity == Decimal("300.0000")

    def test_a_bonus_fans_out_on_the_same_terms(self) -> None:
        bonus = CorporateAction(HDFC, CorporateActionKind.BONUS, Decimal("1"), Decimal("5"))

        result = fan_out_corporate_action(bonus, self.positions())

        assert {position.holding.quantity for position in result.updated} == {Decimal("500.0000")}
        assert result.cost_basis_after == result.cost_basis_before

    def test_a_frozen_holding_still_receives_its_corporate_action(self) -> None:
        """A split is a fact published by an exchange, not an attribution: postponing it would
        leave the frozen position at a steadily more wrong quantity while the question waits."""
        result = fan_out_corporate_action(self.SPLIT, self.positions())
        report = freeze_report([open_entry(HDFC)], result.positions)

        assert report.is_frozen(HDFC)
        assert {position.holding.quantity for position in result.updated} == {Decimal("300.0000")}


class TestSection64NeedsAttention:
    """§6.4's v1 ribbon: broker connection expired · N holdings unallocated · reconciliation
    items pending · rebalance available · stale price data."""

    def settled(self) -> AttentionInputs:
        """A user with nothing to do: everything allocated, nothing open, prices at today's
        close, no expired connection, no rebalance waiting."""
        return AttentionInputs(
            as_of=TODAY,
            holdings=[Holding(HDFC, Decimal("320")), Holding(TCS, Decimal("50"))],
            allocations=[Allocation(HDFC, LONG_TERM), Allocation(TCS, MOMENTUM)],
            prices_as_of=TODAY,
        )

    def test_the_ribbon_is_empty_when_nothing_needs_attention(self) -> None:
        """A ribbon that always has something in it is furniture, and furniture does not get
        read."""
        assert attention_items(self.settled()) == []

    def test_every_v1_item_fires_when_every_v1_condition_holds(self) -> None:
        inputs = AttentionInputs(
            as_of=TODAY,
            holdings=[Holding(HDFC, Decimal("320")), Holding(TCS, Decimal("50"))],
            allocations=[Allocation(HDFC, LONG_TERM)],
            entries=[open_entry(HDFC)],
            expired_broker_account_ids=[10, 20],
            rebalance_due_portfolio_ids=[MOMENTUM],
            prices_as_of=dt.date(2026, 8, 1),
        )

        assert [item.kind for item in attention_items(inputs)] == [
            AttentionKind.BROKER_CONNECTION_EXPIRED,
            AttentionKind.RECONCILIATION_PENDING,
            AttentionKind.STALE_PRICE_DATA,
            AttentionKind.HOLDINGS_UNALLOCATED,
            AttentionKind.REBALANCE_AVAILABLE,
        ]

    def test_the_expired_connection_item_names_the_broker_accounts(self) -> None:
        inputs = AttentionInputs(
            as_of=TODAY, prices_as_of=TODAY, expired_broker_account_ids=[10, 20]
        )
        (item,) = attention_items(inputs)

        assert item.kind is AttentionKind.BROKER_CONNECTION_EXPIRED
        assert item.count == 2
        assert item.subject_ids == (10, 20)

    def test_the_unallocated_item_counts_what_the_ledger_calls_unallocated(self) -> None:
        """Derived rather than passed, so a caller's arithmetic cannot disagree with the ledger
        about what §6.6's centerpiece contains."""
        inputs = AttentionInputs(
            as_of=TODAY,
            prices_as_of=TODAY,
            holdings=[
                Holding(HDFC, Decimal("320")),
                Holding(TCS, Decimal("50")),
                Holding(HDFC_AT_UPSTOX, Decimal("120")),
            ],
            allocations=[Allocation(HDFC, LONG_TERM)],
        )
        (item,) = attention_items(inputs)

        assert item.kind is AttentionKind.HOLDINGS_UNALLOCATED
        assert item.count == 2
        assert item.message == "2 holdings are not in any portfolio yet"

    def test_the_reconciliation_item_counts_frozen_holdings_not_open_rows(self) -> None:
        """Two questions about one position are one position whose returns are on hold; saying
        "2 holdings" would overstate the damage."""
        inputs = AttentionInputs(
            as_of=TODAY,
            prices_as_of=TODAY,
            holdings=[Holding(HDFC, Decimal("320"))],
            allocations=[Allocation(HDFC, LONG_TERM)],
            entries=[
                open_entry(HDFC, item_id=1),
                open_entry(HDFC, ReconciliationReason.QUANTITY_MISMATCH, item_id=2),
            ],
        )
        (item,) = attention_items(inputs)

        assert item.kind is AttentionKind.RECONCILIATION_PENDING
        assert item.count == 1
        assert item.message == (
            "1 holding is pending reconciliation — returns are on hold until you answer"
        )

    def test_an_answered_item_leaves_the_ribbon(self) -> None:
        settled = self.settled()
        resolved = resolve(open_entry(HDFC), LONG_TERM, PORTFOLIOS, TODAY).entry
        dismissed = dismiss(open_entry(TCS, item_id=2), TODAY).entry

        inputs = AttentionInputs(
            as_of=settled.as_of,
            holdings=settled.holdings,
            allocations=settled.allocations,
            entries=[resolved, dismissed],
            prices_as_of=TODAY,
        )

        assert attention_items(inputs) == []

    @pytest.mark.parametrize(
        ("days_behind", "expected"),
        [(0, False), (4, False), (5, True), (30, True)],
    )
    def test_prices_are_stale_only_after_a_long_weekend_could_explain_them(
        self, days_behind: int, expected: bool
    ) -> None:
        """§5.1 is end-of-day, so a Friday close is the newest honest price all weekend and a
        Monday holiday makes Tuesday four days out with nothing wrong."""
        settled = self.settled()
        inputs = AttentionInputs(
            as_of=TODAY,
            holdings=settled.holdings,
            allocations=settled.allocations,
            prices_as_of=TODAY - dt.timedelta(days=days_behind),
        )

        kinds = [item.kind for item in attention_items(inputs)]
        assert (AttentionKind.STALE_PRICE_DATA in kinds) is expected

    def test_no_prices_at_all_gets_its_own_sentence_rather_than_a_blank_row(self) -> None:
        (item,) = attention_items(AttentionInputs(as_of=TODAY))

        assert item.kind is AttentionKind.STALE_PRICE_DATA
        assert item.message == "No closing prices yet — values cannot be shown"

    def test_a_stale_price_row_quotes_the_close_it_is_stuck_at(self) -> None:
        settled = self.settled()
        inputs = AttentionInputs(
            as_of=TODAY,
            holdings=settled.holdings,
            allocations=settled.allocations,
            prices_as_of=dt.date(2026, 8, 1),
        )
        (item,) = attention_items(inputs)

        assert item.message == "Prices are still at the close of 2026-08-01"

    def test_the_ribbon_can_be_switched_off_for_a_caller_with_no_market_data(self) -> None:
        assert attention_items(AttentionInputs(as_of=TODAY, check_prices=False)) == []

    @pytest.mark.parametrize("kind", list(AttentionKind))
    def test_every_kind_has_a_singular_and_a_plural_sentence(self, kind: AttentionKind) -> None:
        """A ribbon that says "1 holdings" reads as a bug report."""
        one = AttentionItem(kind=kind, count=1, since=TODAY).message
        many = AttentionItem(kind=kind, count=3, since=TODAY).message
        assert one and many
        if kind is not AttentionKind.STALE_PRICE_DATA:
            assert one != many

    def test_criterion_7_no_renamed_jargon_reaches_a_sentence(self) -> None:
        """§8's left column never appears in the UI, and a string defined in core is one the UI
        cannot fix later."""
        jargon = re.compile(
            r"\b(box|boxes|book|books|sleeve|sleeves|nest|nesting|divide|"
            r"file under|spans brokers|your rule|run by hand)\b",
            re.IGNORECASE,
        )
        sentences = [
            AttentionItem(kind=kind, count=count, since=TODAY).message
            for kind in AttentionKind
            for count in (0, 1, 3)
        ]
        sentences += [AttentionItem(kind=AttentionKind.STALE_PRICE_DATA, count=0).message]
        assert [line for line in sentences if jargon.search(line)] == []


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
        """ "Are these prices stale?" is a question about a moment, so the moment is an argument."""
        assert (
            re.findall(r"\b(?:datetime\.now|dt\.date\.today|time\.time)\s*\(", _module_source())
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

    def test_the_ledgers_rules_are_imported_rather_than_restated(self) -> None:
        """Two modules that both know what a split does to an average price are two modules that
        will eventually disagree, and the one users meet is whichever ran last."""
        source = _module_source()
        assert "from baskfy_core.allocation_ledger import (" in source
        for borrowed in ("apply_corporate_action", "cost_basis", "unallocated_holdings"):
            assert re.search(rf"^\s+{borrowed},$", source, re.MULTILINE)
        # The ledger's own split arithmetic never reappears here under another name.
        assert "to_ratio" not in source and "from_ratio" not in source


class TestMoneyIsDecimal:
    """House rule 9 — money and prices are `numeric`, never `float`."""

    def test_the_module_never_names_float(self) -> None:
        assert re.findall(r"\bfloat\b", _module_source()) == []

    def test_a_preserved_cost_basis_is_an_exact_decimal_not_an_approximation(self) -> None:
        result = fan_out_corporate_action(
            CorporateAction(HDFC, CorporateActionKind.SPLIT, Decimal("1"), Decimal("3")),
            [LedgerPosition(LONG_TERM, Holding(HDFC, Decimal("100"), Decimal("333.33")))],
        )
        assert isinstance(result.cost_basis_after, Decimal)
        assert result.cost_basis_after == Decimal("33333.00")
