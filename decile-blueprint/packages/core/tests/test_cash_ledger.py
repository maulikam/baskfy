"""The cash ledger's rules — `PORTFOLIO_REDESIGN.md` §4.4 and §11.

These tests assert the **spec**, not the implementation (house rule 2). §4.4 is three sentences
long and each of them is a test class below: the Unallocated bucket, the assignment that is the
XIRR event, and the buy that is not one.

The buy is the one worth watching. `TestPerPortfolioXirrIsPure` builds a portfolio, computes its
XIRR, then adds a buy and a sell and a dividend and computes it again — and asserts the answer did
not move by a single basis point. A per-portfolio XIRR that reacted to trading would still look
plausible on a page, which is exactly why it is asserted rather than reviewed.

Numbers are chosen adversarially: tenths and thirds of a rupee that binary arithmetic cannot
represent, an assignment that is not a round number, a portfolio whose cash goes to exactly zero.
A test that only exercises round numbers proves only round numbers.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
from decimal import Decimal

import pytest

from baskfy_core.cash_ledger import (
    CASH_ONLY_KINDS,
    EXTERNAL_KINDS,
    STOCK_KINDS,
    XIRR_EVENT_KINDS,
    CashFlowKind,
    PortfolioCashFlow,
    portfolio_cash,
    portfolio_cash_by_portfolio,
    portfolio_xirr,
    total_cash,
    unallocated_cash,
    unallocated_cash_by_broker,
    xirr_events,
)
from baskfy_core.curated_accounting import CashFlow, xirr


def _module_source() -> str:
    """The ledger's own source. Law 1 and house rule 9 are properties of the *text*, and the
    honest way to assert a property of the text is to read it."""

    return (
        pathlib.Path(__file__).resolve().parents[1] / "src/baskfy_core/cash_ledger.py"
    ).read_text()


ZERODHA = 10
UPSTOX = 20

MOMENTUM = 1
LONG_TERM = 2

HDFC = 101
TCS = 102

DAY_ONE = dt.date(2026, 1, 2)
A_YEAR_LATER = dt.date(2027, 1, 2)


def deposit(amount: str, *, broker: int = ZERODHA, on: dt.date = DAY_ONE) -> PortfolioCashFlow:
    return PortfolioCashFlow(
        broker_account_id=broker,
        kind=CashFlowKind.EXTERNAL_DEPOSIT,
        amount=Decimal(amount),
        occurred_on=on,
    )


def withdrawal(amount: str, *, broker: int = ZERODHA, on: dt.date = DAY_ONE) -> PortfolioCashFlow:
    return PortfolioCashFlow(
        broker_account_id=broker,
        kind=CashFlowKind.EXTERNAL_WITHDRAWAL,
        amount=Decimal(amount),
        occurred_on=on,
    )


def assign(
    amount: str,
    *,
    portfolio: int = MOMENTUM,
    broker: int = ZERODHA,
    on: dt.date = DAY_ONE,
) -> PortfolioCashFlow:
    return PortfolioCashFlow(
        broker_account_id=broker,
        kind=CashFlowKind.ASSIGN,
        amount=Decimal(amount),
        occurred_on=on,
        portfolio_id=portfolio,
    )


def release(
    amount: str,
    *,
    portfolio: int = MOMENTUM,
    broker: int = ZERODHA,
    on: dt.date = DAY_ONE,
) -> PortfolioCashFlow:
    return PortfolioCashFlow(
        broker_account_id=broker,
        kind=CashFlowKind.RELEASE,
        amount=Decimal(amount),
        occurred_on=on,
        portfolio_id=portfolio,
    )


def buy(
    amount: str,
    *,
    portfolio: int = MOMENTUM,
    broker: int = ZERODHA,
    quantity: str = "100",
    on: dt.date = DAY_ONE,
) -> PortfolioCashFlow:
    return PortfolioCashFlow(
        broker_account_id=broker,
        kind=CashFlowKind.BUY,
        amount=Decimal(amount),
        occurred_on=on,
        portfolio_id=portfolio,
        instrument_id=HDFC,
        quantity=Decimal(quantity),
    )


def sell(
    amount: str,
    *,
    portfolio: int = MOMENTUM,
    broker: int = ZERODHA,
    quantity: str = "100",
    on: dt.date = DAY_ONE,
) -> PortfolioCashFlow:
    return PortfolioCashFlow(
        broker_account_id=broker,
        kind=CashFlowKind.SELL,
        amount=Decimal(amount),
        occurred_on=on,
        portfolio_id=portfolio,
        instrument_id=HDFC,
        quantity=Decimal(quantity),
    )


def dividend(
    amount: str,
    *,
    portfolio: int = MOMENTUM,
    broker: int = ZERODHA,
    on: dt.date = DAY_ONE,
) -> PortfolioCashFlow:
    return PortfolioCashFlow(
        broker_account_id=broker,
        kind=CashFlowKind.DIVIDEND,
        amount=Decimal(amount),
        occurred_on=on,
        portfolio_id=portfolio,
        instrument_id=HDFC,
    )


class TestUnallocatedBucket:
    """§4.4: "One Unallocated cash bucket per broker account. External deposits/withdrawals land
    there"."""

    def test_a_deposit_lands_in_unallocated_and_a_withdrawal_leaves_it(self) -> None:
        flows = [deposit("250000.00"), withdrawal("40000.50")]
        assert unallocated_cash(flows, ZERODHA) == Decimal("209999.50")

    def test_assigning_cash_takes_it_out_of_the_bucket(self) -> None:
        """The money did not leave the broker; it left the *unallocated* part of it."""
        flows = [deposit("250000.00"), assign("100000.00")]
        assert unallocated_cash(flows, ZERODHA) == Decimal("150000.00")

    def test_releasing_cash_puts_it_back(self) -> None:
        flows = [deposit("250000.00"), assign("100000.00"), release("30000.00")]
        assert unallocated_cash(flows, ZERODHA) == Decimal("180000.00")

    @pytest.mark.parametrize(
        "flow",
        [buy("60000.00"), sell("70000.00"), dividend("1250.00")],
        ids=["buy", "sell", "dividend"],
    )
    def test_a_flow_inside_a_portfolio_never_touches_the_bucket(
        self, flow: PortfolioCashFlow
    ) -> None:
        """Buying inside a portfolio is a cash->stock transfer, and the cash it spends was
        assigned out of Unallocated long before. Touching the bucket here would spend it twice."""
        before = [deposit("250000.00"), assign("100000.00")]
        assert unallocated_cash([*before, flow], ZERODHA) == unallocated_cash(before, ZERODHA)

    def test_each_broker_account_has_its_own_bucket(self) -> None:
        """§4.4 says one bucket *per broker account*, and a user with two brokers must not see one
        account's deposit fund the other's assignment."""
        flows = [
            deposit("250000.00", broker=ZERODHA),
            deposit("80000.00", broker=UPSTOX),
            assign("100000.00", broker=ZERODHA),
        ]
        assert unallocated_cash(flows, ZERODHA) == Decimal("150000.00")
        assert unallocated_cash(flows, UPSTOX) == Decimal("80000.00")

    def test_a_broker_we_have_never_seen_has_an_empty_bucket(self) -> None:
        assert unallocated_cash([deposit("1000.00")], UPSTOX) == Decimal("0.00")

    def test_the_per_broker_table_agrees_with_asking_one_at_a_time(self) -> None:
        flows = [
            deposit("250000.00", broker=ZERODHA),
            deposit("80000.00", broker=UPSTOX),
            assign("100000.00", broker=ZERODHA),
            buy("60000.00", broker=ZERODHA),
        ]
        table = unallocated_cash_by_broker(flows)
        assert table == {
            ZERODHA: unallocated_cash(flows, ZERODHA),
            UPSTOX: unallocated_cash(flows, UPSTOX),
        }

    def test_an_account_seen_only_inside_a_portfolio_still_gets_a_row(self) -> None:
        """A bucket that is empty and a broker we know nothing about are different facts, and
        §6.6's section must be able to tell them apart."""
        table = unallocated_cash_by_broker([buy("60000.00", broker=UPSTOX)])
        assert table == {UPSTOX: Decimal("0.00")}


class TestPortfolioCash:
    """The other side of every internal flow: cash assigned but not yet spent."""

    def test_assigned_cash_sits_in_the_portfolio_until_it_is_spent(self) -> None:
        flows = [deposit("250000.00"), assign("100000.00"), buy("60000.00")]
        assert portfolio_cash(flows, MOMENTUM) == Decimal("40000.00")

    def test_a_sell_returns_cash_to_the_portfolio_not_to_unallocated(self) -> None:
        """§4.3 attributes a sell to the holding's one capital portfolio; §4.4 says the proceeds
        stay there. Sweeping them to Unallocated would look like a withdrawal from the strategy."""
        flows = [deposit("250000.00"), assign("100000.00"), buy("60000.00"), sell("70000.00")]
        assert portfolio_cash(flows, MOMENTUM) == Decimal("110000.00")
        assert unallocated_cash(flows, ZERODHA) == Decimal("150000.00")

    def test_a_dividend_credits_the_portfolio_that_owns_the_share(self) -> None:
        flows = [deposit("250000.00"), assign("100000.00"), dividend("1234.56")]
        assert portfolio_cash(flows, MOMENTUM) == Decimal("101234.56")

    def test_releasing_everything_leaves_the_portfolio_at_exactly_zero(self) -> None:
        flows = [deposit("250000.00"), assign("100000.00"), release("100000.00")]
        assert portfolio_cash(flows, MOMENTUM) == Decimal("0.00")

    def test_portfolios_do_not_see_each_others_cash(self) -> None:
        flows = [
            deposit("250000.00"),
            assign("100000.00", portfolio=MOMENTUM),
            assign("70000.00", portfolio=LONG_TERM),
        ]
        assert portfolio_cash(flows, MOMENTUM) == Decimal("100000.00")
        assert portfolio_cash(flows, LONG_TERM) == Decimal("70000.00")

    def test_the_per_portfolio_table_agrees_with_asking_one_at_a_time(self) -> None:
        flows = [
            deposit("250000.00"),
            assign("100000.00", portfolio=MOMENTUM),
            assign("70000.00", portfolio=LONG_TERM),
            buy("60000.00", portfolio=MOMENTUM),
        ]
        assert portfolio_cash_by_portfolio(flows) == {
            MOMENTUM: portfolio_cash(flows, MOMENTUM),
            LONG_TERM: portfolio_cash(flows, LONG_TERM),
        }

    def test_unallocated_is_not_a_portfolio(self) -> None:
        """Unallocated cash is per broker account, so there is no single number to return."""
        with pytest.raises(ValueError, match="not a portfolio"):
            portfolio_cash([deposit("1000.00")], None)


class TestCashIsConserved:
    """Criterion 1: the parts sum to the whole, to the paisa, at all times."""

    def test_an_assignment_moves_cash_without_creating_or_destroying_any(self) -> None:
        before = [deposit("250000.00")]
        after = [*before, assign("100000.00")]
        assert total_cash(after) == total_cash(before) == Decimal("250000.00")

    def test_unallocated_plus_portfolio_cash_is_all_the_cash(self) -> None:
        flows = [
            deposit("250000.00", broker=ZERODHA),
            deposit("80000.00", broker=UPSTOX),
            withdrawal("15000.25", broker=ZERODHA),
            assign("100000.00", portfolio=MOMENTUM, broker=ZERODHA),
            assign("70000.00", portfolio=LONG_TERM, broker=UPSTOX),
            buy("60000.00", portfolio=MOMENTUM, broker=ZERODHA),
            sell("33333.33", portfolio=MOMENTUM, broker=ZERODHA),
            dividend("1234.56", portfolio=LONG_TERM, broker=UPSTOX),
            release("5000.00", portfolio=LONG_TERM, broker=UPSTOX),
        ]
        buckets = sum(unallocated_cash_by_broker(flows).values(), Decimal("0"))
        inside = sum(portfolio_cash_by_portfolio(flows).values(), Decimal("0"))
        assert buckets + inside == total_cash(flows)

    def test_a_buy_genuinely_lowers_total_cash(self) -> None:
        """The other half of that rupee is in the holdings, which allocation_ledger values."""
        before = [deposit("250000.00"), assign("100000.00")]
        after = [*before, buy("60000.00")]
        assert total_cash(after) == total_cash(before) - Decimal("60000.00")


class TestWhichFlowsAreXirrEvents:
    """§4.4: assigning cash "is the XIRR cash-flow event"; buying inside a portfolio is not."""

    @pytest.mark.parametrize("kind", sorted(XIRR_EVENT_KINDS))
    def test_assign_and_release_are_xirr_events(self, kind: CashFlowKind) -> None:
        assert kind.is_xirr_event

    @pytest.mark.parametrize(
        "kind",
        sorted(set(CashFlowKind) - XIRR_EVENT_KINDS),
    )
    def test_nothing_else_is(self, kind: CashFlowKind) -> None:
        assert not kind.is_xirr_event

    def test_the_two_xirr_kinds_are_exactly_assign_and_release(self) -> None:
        assert {CashFlowKind.ASSIGN, CashFlowKind.RELEASE} == XIRR_EVENT_KINDS

    def test_the_seven_kinds_are_migration_0022s_seven(self) -> None:
        """The database CHECK constraint refuses anything else, so a kind spelled differently here
        is a row that fails at insert time rather than in this suite."""
        assert {kind.value for kind in CashFlowKind} == {
            "EXTERNAL_DEPOSIT",
            "EXTERNAL_WITHDRAWAL",
            "ASSIGN",
            "RELEASE",
            "BUY",
            "SELL",
            "DIVIDEND",
        }

    def test_only_assignments_and_releases_reach_the_solver(self) -> None:
        flows = [
            deposit("250000.00"),
            assign("100000.00"),
            buy("60000.00"),
            sell("70000.00"),
            dividend("1234.56"),
            release("5000.00", on=A_YEAR_LATER),
        ]
        assert xirr_events(flows, MOMENTUM) == [
            CashFlow(on=DAY_ONE, amount=Decimal("-100000.00")),
            CashFlow(on=A_YEAR_LATER, amount=Decimal("5000.00")),
        ]

    def test_money_in_is_negative_and_money_out_is_positive(self) -> None:
        """`curated_accounting`'s convention, reused rather than re-invented — a second sign
        convention in this repo would be a second way to be wrong."""
        (assigned,) = xirr_events([assign("1000.00")], MOMENTUM)
        (released,) = xirr_events([release("1000.00")], MOMENTUM)
        assert assigned.amount == Decimal("-1000.00")
        assert released.amount == Decimal("1000.00")

    def test_one_portfolios_events_are_not_anothers(self) -> None:
        flows = [assign("100000.00", portfolio=MOMENTUM), assign("70000.00", portfolio=LONG_TERM)]
        assert xirr_events(flows, LONG_TERM) == [CashFlow(on=DAY_ONE, amount=Decimal("-70000.00"))]


class TestPerPortfolioXirrIsPure:
    """§4.4: "Sub-portfolio XIRR is computed only from these internal flows. This keeps
    per-portfolio XIRR mathematically pure"."""

    def test_one_assignment_held_for_a_year(self) -> None:
        flows = [deposit("250000.00"), assign("100000.00")]
        rate = portfolio_xirr(
            flows, MOMENTUM, as_of=A_YEAR_LATER, closing_value=Decimal("110000.00")
        )
        assert rate == Decimal("0.1000")

    @pytest.mark.parametrize(
        "trading",
        [
            [buy("60000.00")],
            [buy("60000.00"), sell("33333.33")],
            [buy("60000.00"), dividend("1234.56")],
            [buy("60000.00"), sell("33333.33"), dividend("1234.56"), buy("20000.00")],
        ],
        ids=["buy", "buy-sell", "buy-dividend", "everything"],
    )
    def test_trading_inside_the_portfolio_does_not_move_its_xirr(
        self, trading: list[PortfolioCashFlow]
    ) -> None:
        """The whole point of §4.4. The portfolio is worth the same on the same day either way —
        the buy converted cash into stock, it did not add or remove money — so the money-weighted
        return must be identical, not merely close."""
        assigned_only = [deposit("250000.00"), assign("100000.00")]
        closing = Decimal("110000.00")
        assert portfolio_xirr(
            [*assigned_only, *trading], MOMENTUM, as_of=A_YEAR_LATER, closing_value=closing
        ) == portfolio_xirr(assigned_only, MOMENTUM, as_of=A_YEAR_LATER, closing_value=closing)

    def test_a_second_assignment_does_move_it(self) -> None:
        """The contrast that makes the test above mean something: real money going in *is* an
        event, and a solver that ignored everything would pass the previous test too."""
        assigned_once = [assign("100000.00")]
        assigned_twice = [*assigned_once, assign("50000.00", on=dt.date(2026, 7, 1))]
        closing = Decimal("170000.00")
        assert portfolio_xirr(
            assigned_twice, MOMENTUM, as_of=A_YEAR_LATER, closing_value=closing
        ) != portfolio_xirr(assigned_once, MOMENTUM, as_of=A_YEAR_LATER, closing_value=closing)

    def test_it_is_the_one_solver_and_not_a_second_one(self) -> None:
        """CLAUDE.md/PLAN: "XIRR is `curated_accounting.xirr`. One solver." Asserted against that
        solver directly, so a copy of it living here would show up as a difference."""
        flows = [assign("123456.78"), release("10000.00", on=dt.date(2026, 9, 30))]
        expected = xirr(
            [
                *xirr_events(flows, MOMENTUM),
                CashFlow(on=A_YEAR_LATER, amount=Decimal("140000.00")),
            ]
        )
        assert (
            portfolio_xirr(flows, MOMENTUM, as_of=A_YEAR_LATER, closing_value=Decimal("140000.00"))
            == expected
        )

    def test_a_portfolio_that_was_never_assigned_cash_has_no_xirr(self) -> None:
        """Not zero. A money-weighted return with nothing to weight is not a number, and
        `headline_metric` turns None into a labelled "unavailable" rather than a 0%."""
        assert (
            portfolio_xirr(
                [deposit("250000.00")],
                MOMENTUM,
                as_of=A_YEAR_LATER,
                closing_value=Decimal("0.00"),
            )
            is None
        )

    def test_a_portfolio_whose_only_flows_are_trades_has_no_xirr_either(self) -> None:
        assert (
            portfolio_xirr(
                [buy("60000.00"), sell("70000.00"), dividend("1234.56")],
                MOMENTUM,
                as_of=A_YEAR_LATER,
                closing_value=Decimal("70000.00"),
            )
            is None
        )

    def test_valuing_a_portfolio_before_its_history_finished_is_refused(self) -> None:
        with pytest.raises(ValueError, match="precedes the last cash-flow event"):
            portfolio_xirr(
                [assign("100000.00", on=A_YEAR_LATER)],
                MOMENTUM,
                as_of=DAY_ONE,
                closing_value=Decimal("100000.00"),
            )

    def test_a_negative_closing_value_is_refused(self) -> None:
        with pytest.raises(ValueError, match="closing value cannot be negative"):
            portfolio_xirr(
                [assign("100000.00")],
                MOMENTUM,
                as_of=A_YEAR_LATER,
                closing_value=Decimal("-1.00"),
            )

    def test_a_loss_is_reported_as_a_loss(self) -> None:
        rate = portfolio_xirr(
            [assign("100000.00")], MOMENTUM, as_of=A_YEAR_LATER, closing_value=Decimal("90000.00")
        )
        assert rate is not None
        assert rate < Decimal("0")


class TestExternalAndInternalCannotBeConfused:
    """Migration 0022's `portfolio_cash_flow_external_has_no_portfolio`, enforced where a caller
    meets it first. The whole XIRR story depends on telling the two apart."""

    @pytest.mark.parametrize("kind", sorted(EXTERNAL_KINDS))
    def test_an_external_flow_with_a_portfolio_is_refused(self, kind: CashFlowKind) -> None:
        """Funding a demat account says nothing about which strategy the money is for. Letting a
        deposit name a portfolio would make it an XIRR-shaped event that never happened."""
        with pytest.raises(ValueError, match="belongs to no portfolio"):
            PortfolioCashFlow(
                broker_account_id=ZERODHA,
                kind=kind,
                amount=Decimal("1000.00"),
                occurred_on=DAY_ONE,
                portfolio_id=MOMENTUM,
            )

    @pytest.mark.parametrize("kind", sorted(set(CashFlowKind) - EXTERNAL_KINDS))
    def test_an_internal_flow_without_a_portfolio_is_refused(self, kind: CashFlowKind) -> None:
        with pytest.raises(ValueError, match="must name the portfolio"):
            PortfolioCashFlow(
                broker_account_id=ZERODHA,
                kind=kind,
                amount=Decimal("1000.00"),
                occurred_on=DAY_ONE,
                instrument_id=HDFC if kind in STOCK_KINDS else None,
                quantity=Decimal("1") if kind in STOCK_KINDS else None,
            )

    def test_is_external_is_exactly_the_two_external_kinds(self) -> None:
        assert {kind for kind in CashFlowKind if kind.is_external} == EXTERNAL_KINDS


class TestAFlowDescribesWhatHappened:
    """The remaining constructor rules: direction, precision, and the stock a trade names."""

    @pytest.mark.parametrize("amount", ["0", "-1000.00"], ids=["zero", "negative"])
    def test_the_amount_is_a_magnitude_and_the_kind_carries_the_direction(
        self, amount: str
    ) -> None:
        """A signed amount plus a kind is two sources of truth for one direction, and a
        withdrawal of -500 and one of +500 would both parse."""
        with pytest.raises(ValueError, match="positive amount"):
            deposit(amount)

    def test_an_amount_finer_than_a_paisa_is_refused_rather_than_rounded(self) -> None:
        """`portfolio_cash_flow.amount` is numeric(18,2); a balance the table cannot reproduce is
        a reconciliation nobody can explain later."""
        with pytest.raises(ValueError, match="finer than a paisa"):
            deposit("1000.005")

    def test_a_trade_must_name_the_stock_it_traded(self) -> None:
        with pytest.raises(ValueError, match="must name it"):
            PortfolioCashFlow(
                broker_account_id=ZERODHA,
                kind=CashFlowKind.BUY,
                amount=Decimal("60000.00"),
                occurred_on=DAY_ONE,
                portfolio_id=MOMENTUM,
                quantity=Decimal("100"),
            )

    def test_a_trade_must_carry_its_quantity(self) -> None:
        with pytest.raises(ValueError, match="quantity traded"):
            PortfolioCashFlow(
                broker_account_id=ZERODHA,
                kind=CashFlowKind.SELL,
                amount=Decimal("70000.00"),
                occurred_on=DAY_ONE,
                portfolio_id=MOMENTUM,
                instrument_id=HDFC,
            )

    def test_a_quantity_finer_than_four_decimals_is_refused(self) -> None:
        """The precision `allocation_ledger.QUANTITY_PRECISION` fixes, imported rather than
        restated so a bonus remainder means the same thing in both ledgers."""
        with pytest.raises(ValueError, match="four decimals"):
            buy("60000.00", quantity="100.00001")

    @pytest.mark.parametrize("kind", sorted(CASH_ONLY_KINDS))
    def test_a_cash_only_flow_names_no_stock(self, kind: CashFlowKind) -> None:
        """An activity feed (§7) would read a stray instrument as a trade that never happened."""
        with pytest.raises(ValueError, match="moves cash and nothing else"):
            PortfolioCashFlow(
                broker_account_id=ZERODHA,
                kind=kind,
                amount=Decimal("1000.00"),
                occurred_on=DAY_ONE,
                portfolio_id=None if kind in EXTERNAL_KINDS else MOMENTUM,
                instrument_id=HDFC,
            )

    def test_a_dividend_moves_no_shares(self) -> None:
        with pytest.raises(ValueError, match="moves no shares"):
            PortfolioCashFlow(
                broker_account_id=ZERODHA,
                kind=CashFlowKind.DIVIDEND,
                amount=Decimal("1234.56"),
                occurred_on=DAY_ONE,
                portfolio_id=MOMENTUM,
                instrument_id=HDFC,
                quantity=Decimal("100"),
            )

    def test_a_dividend_may_name_the_stock_that_paid_it(self) -> None:
        """§7's activity tab shows dividends by stock, so the instrument is allowed here — it is
        the *quantity* that would be meaningless."""
        assert dividend("1234.56").instrument_id == HDFC

    def test_a_flow_is_frozen(self) -> None:
        """A ledger entry records something that happened; one you can edit in place is a balance
        that changes with no event behind it."""
        recorded = deposit("1000.00")
        # Through `setattr` because a literal assignment would not compile under the type
        # checker either -- which is the point, but a test has to reach the runtime refusal.
        field = "amount"
        with pytest.raises((AttributeError, TypeError)):
            setattr(recorded, field, Decimal("2000.00"))


class TestMoneyStaysExact:
    """House rule 9 — money and prices are `numeric`, never binary fractions."""

    def test_tenths_of_a_rupee_sum_to_the_paisa(self) -> None:
        """0.1 + 0.2 is the canonical drift, and a ledger that drifts by 1e-17 per row drifts
        visibly over a year of a busy account."""
        flows = [deposit("0.10"), deposit("0.20"), deposit("0.30")]
        assert unallocated_cash(flows, ZERODHA) == Decimal("0.60")

    def test_a_thousand_thirty_three_paise_deposits_do_not_drift(self) -> None:
        flows = [deposit("0.33") for _ in range(1000)]
        assert unallocated_cash(flows, ZERODHA) == Decimal("330.00")

    def test_thirds_of_a_rupee_survive_an_assignment_round_trip(self) -> None:
        flows = [
            deposit("100000.00"),
            assign("33333.33"),
            assign("33333.33"),
            assign("33333.34"),
        ]
        assert unallocated_cash(flows, ZERODHA) == Decimal("0.00")
        assert portfolio_cash(flows, MOMENTUM) == Decimal("100000.00")

    @pytest.mark.parametrize(
        "balance",
        [
            unallocated_cash([deposit("0.10")], ZERODHA),
            portfolio_cash([assign("0.10")], MOMENTUM),
            total_cash([deposit("0.10")]),
        ],
        ids=["unallocated", "portfolio", "total"],
    )
    def test_every_balance_is_quantised_to_paise(self, balance: Decimal) -> None:
        """House rule 8: the API, the UI and the CSV export cannot disagree about a number they
        all read from here."""
        assert balance.as_tuple().exponent == -2


class TestLaw1TouchesNothing:
    """`packages/core` touches nothing — CLAUDE.md law 1, asserted by scanning the source."""

    def test_the_module_imports_no_io(self) -> None:
        forbidden = re.findall(
            r"^\s*(?:import|from)\s+(sqlalchemy|httpx|requests|redis|asyncpg|boto3|pathlib|os)\b",
            _module_source(),
            re.MULTILINE,
        )
        assert forbidden == []

    def test_the_module_never_reads_a_clock(self) -> None:
        """Every balance here is a statement about a moment, so the moment is an argument."""
        assert (
            re.findall(
                r"\b(?:datetime\.now|dt\.datetime\.now|dt\.date\.today|time\.time)\s*\(",
                _module_source(),
            )
            == []
        )

    def test_the_module_names_no_order(self) -> None:
        """Non-negotiable #1. It matters more here than anywhere else in core: this module's own
        vocabulary contains BUY and SELL, and they are ledger kinds for trades that already
        happened, not sides of an order anything could place."""
        assert (
            re.findall(
                r"\b(place_order|order_type|transact_type|exchange_segment|kite)\b",
                _module_source(),
            )
            == []
        )

    def test_the_module_never_names_a_binary_fraction_type(self) -> None:
        """House rule 9 is about *this* module, so scan it — asserting that Decimal * a binary
        fraction raises would only be testing Python."""
        assert re.findall(r"\bfloat\b", _module_source()) == []
