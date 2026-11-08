"""CAS import — `PORTFOLIO_REDESIGN.md` §5.3 and §5.2, asserted against the spec.

The fixtures below are the point of this file. §5.3's whole risk is that a statement *nearly*
parses, so the two statements here are written the way the depositories actually print them —
CDSL with the date first and dd/mm/yyyy, NSDL with the ISIN first and dd-Mon-yyyy, both with
comma-grouped numbers, an unpriced bonus row and an off-market transfer — and every failure case
is derived from one of them by a single edit, the way a real corrupted upload differs from a real
good one.

The numbers are chosen adversarially (house rule 2's spirit). The purchases of HDFC BANK are
quantities and prices whose weighted average is exact in `Decimal` and drifts in binary
arithmetic, so the test proves house rule 9 rather than asserting it. The ITC purchases have an
average that does not land on a paisa, which is what proves the invested amount is rounded once
from the total cost rather than derived from a rounded average.
"""

from __future__ import annotations

import datetime as dt
import inspect
import pathlib
import re
from decimal import Decimal

import pytest

from baskfy_core.allocation_ledger import Holding, HoldingKey
from baskfy_core.cas_import import (
    CasParseError,
    CasPosition,
    CasStatement,
    CasTransactionType,
    Depository,
    MalformedStatement,
    TruncatedStatement,
    UnknownStatementFormat,
    UnknownTransactionType,
    UnmatchedReason,
    backfilled_holding,
    derive_positions,
    detect_depository,
    parse_cas,
    reconcile,
)


def _module_source() -> str:
    """The module's own source. Law 1 and house rule 9 are properties of the text, so the honest
    way to assert them is to read it — the same guard `test_allocation_ledger.py` carries."""

    return (
        pathlib.Path(__file__).resolve().parents[1] / "src/baskfy_core/cas_import.py"
    ).read_text()


# ---------------------------------------------------------------------------
# Fixtures: two statements, as their depositories print them
# ---------------------------------------------------------------------------

CDSL_STATEMENT = """\
CENTRAL DEPOSITORY SERVICES (INDIA) LIMITED
CDSL CONSOLIDATED ACCOUNT STATEMENT

Statement Period : 01/04/2025 to 31/03/2026
BO ID : 1208160012345678
DP Name : ZERODHA BROKING LTD
Account Holder : A N OTHER

TRANSACTION STATEMENT
Date        ISIN          Security                   Transaction  Quantity  Price     Value
01/04/2025  INE040A01034  HDFC BANK LTD              BUY                23  1,625.24  37,380.52
17/06/2025  INE467B01029  TATA CONSULTANCY SERV LTD  BUY                12  3,456.78  41,481.36
05/08/2025  INE040A01034  HDFC BANK LTD              BUY                14  1,724.01  24,136.14
22/09/2025  INE467B01029  TATA CONSULTANCY SERV LTD  SELL                2  3,600.00  7,200.00
06/01/2026  INE040A01034  HDFC BANK LTD              BUY                22  1,888.12  41,538.64
12/02/2026  INE154A01025  ITC LTD                    BUY                 3  999.99    2,999.97
20/02/2026  INE154A01025  ITC LTD                    BUY                 4  1,000.05  4,000.20

*** End of Statement ***
"""

NSDL_STATEMENT = """\
NATIONAL SECURITIES DEPOSITORY LIMITED
NSDL CONSOLIDATED ACCOUNT STATEMENT (CAS)

Statement for the period from 01-Apr-2025 to 31-Mar-2026
DP ID : IN300394        Client ID : 12345678
DP Name : HDFC BANK LIMITED

TRANSACTION DETAILS
ISIN          Security Name      Txn Date     Description       Quantity  Price     Value
INE002A01018  RELIANCE INDS LTD  12-May-2025  Purchase                25  1,412.40  35,310.00
INE009A01021  INFOSYS LIMITED    03-Jul-2025  Purchase                40  1,555.25  62,210.00
INE002A01018  RELIANCE INDS LTD  19-Nov-2025  Bonus Issue             25  -         -
INE009A01021  INFOSYS LIMITED    20-Jan-2026  Off-market Debit        10  -         -

End of Statement
"""

HDFC_ISIN = "INE040A01034"
TCS_ISIN = "INE467B01029"
ITC_ISIN = "INE154A01025"
RELIANCE_ISIN = "INE002A01018"
INFOSYS_ISIN = "INE009A01021"

#: What a `services/` layer would look up in the instrument table. A mapping, because resolving an
#: ISIN is a database read and law 1 keeps it out of the module under test.
INSTRUMENTS = {HDFC_ISIN: 1, TCS_ISIN: 2, ITC_ISIN: 3}

ZERODHA = 10
UPSTOX = 20


def cdsl_positions() -> dict[str, CasPosition]:
    return {p.isin: p for p in derive_positions(parse_cas(CDSL_STATEMENT).transactions)}


def nsdl_positions() -> dict[str, CasPosition]:
    return {p.isin: p for p in derive_positions(parse_cas(NSDL_STATEMENT).transactions)}


class TestCdslParsing:
    """A CDSL statement parses into the transactions it prints, and nothing else."""

    def test_a_cdsl_statement_parses_into_its_transactions(self) -> None:
        statement = parse_cas(CDSL_STATEMENT)
        assert statement.depository is Depository.CDSL
        assert len(statement.transactions) == 7
        first = statement.transactions[0]
        assert first.isin == HDFC_ISIN
        assert first.symbol == "HDFC BANK LTD"
        assert first.on == dt.date(2025, 4, 1)
        assert first.kind is CasTransactionType.BUY
        assert first.quantity == Decimal("23")
        assert first.price == Decimal("1625.24")

    def test_the_date_is_read_as_day_month_year(self) -> None:
        """CDSL prints dd/mm/yyyy. Read as mm/dd/yyyy, 05/08/2025 becomes a different month and
        the first-bought date — the whole point of §5.3 — is silently wrong."""
        dates = [t.on for t in parse_cas(CDSL_STATEMENT).transactions]
        assert dt.date(2025, 8, 5) in dates
        assert dt.date(2026, 1, 6) in dates

    def test_cdsl_vocabulary_maps_onto_the_transaction_types(self) -> None:
        assert [t.kind for t in parse_cas(CDSL_STATEMENT).transactions] == [
            CasTransactionType.BUY,
            CasTransactionType.BUY,
            CasTransactionType.BUY,
            CasTransactionType.SELL,
            CasTransactionType.BUY,
            CasTransactionType.BUY,
            CasTransactionType.BUY,
        ]

    def test_the_period_and_the_account_come_from_the_statement(self) -> None:
        """Not from a clock and not from an argument: the statement is the only calendar."""
        statement = parse_cas(CDSL_STATEMENT)
        assert statement.period_start == dt.date(2025, 4, 1)
        assert statement.period_end == dt.date(2026, 3, 31)
        assert statement.account_id == "1208160012345678"

    def test_comma_grouped_numbers_are_read(self) -> None:
        values = {t.value for t in parse_cas(CDSL_STATEMENT).transactions}
        assert Decimal("41481.36") in values


class TestNsdlParsing:
    """The other depository, the other column order, the other date format."""

    def test_an_nsdl_statement_parses_into_its_transactions(self) -> None:
        statement = parse_cas(NSDL_STATEMENT)
        assert statement.depository is Depository.NSDL
        assert len(statement.transactions) == 4
        first = statement.transactions[0]
        assert first.isin == RELIANCE_ISIN
        assert first.symbol == "RELIANCE INDS LTD"
        assert first.on == dt.date(2025, 5, 12)
        assert first.kind is CasTransactionType.BUY
        assert first.quantity == Decimal("25")
        assert first.price == Decimal("1412.40")

    def test_nsdl_vocabulary_maps_onto_the_same_types(self) -> None:
        kinds = [t.kind for t in parse_cas(NSDL_STATEMENT).transactions]
        assert kinds == [
            CasTransactionType.BUY,
            CasTransactionType.BUY,
            CasTransactionType.BONUS,
            CasTransactionType.TRANSFER_OUT,
        ]

    def test_an_unpriced_row_has_no_price_rather_than_a_zero(self) -> None:
        """A zero would say the bonus shares were free. They were paid for by diluting the ones
        already held, which is why §5.2 must not show a purchase-based return for them."""
        bonus = parse_cas(NSDL_STATEMENT).transactions[2]
        assert bonus.kind is CasTransactionType.BONUS
        assert bonus.price is None
        assert bonus.value is None

    def test_the_account_is_dp_id_and_client_id_together(self) -> None:
        statement = parse_cas(NSDL_STATEMENT)
        assert statement.account_id == "IN300394-12345678"
        assert statement.period_start == dt.date(2025, 4, 1)


class TestFormatIsDetectedFromContent:
    """§5.3 never says which depository sent the file, and neither does the caller."""

    def test_parse_cas_is_given_the_text_and_nothing_else(self) -> None:
        """A format parameter is a format parameter someone eventually gets wrong — and the two
        layouts order their columns differently enough that the wrong guess parses cleanly."""
        parameters = list(inspect.signature(parse_cas).parameters)
        assert parameters == ["text"]

    def test_each_statement_is_recognised_from_its_own_content(self) -> None:
        assert detect_depository(CDSL_STATEMENT) is Depository.CDSL
        assert detect_depository(NSDL_STATEMENT) is Depository.NSDL

    def test_text_with_no_depository_markers_is_refused(self) -> None:
        with pytest.raises(UnknownStatementFormat, match="no CDSL or NSDL markers"):
            parse_cas("Dear customer,\n\nYour portfolio statement is attached.\n")

    def test_text_matching_both_depositories_equally_is_refused(self) -> None:
        """An NSDL eCAS names CDSL in its summary, so the name alone is not evidence. When the
        evidence genuinely does not distinguish them, a coin flip corrupts every price."""
        ambiguous = "CDSL\nBO ID : 1208160012345678\nNSDL\nDP ID : IN300394\n"
        with pytest.raises(UnknownStatementFormat, match="equally"):
            parse_cas(ambiguous)


class TestNeverAPartialParse:
    """The worst failure §5.3 can have is a half-read statement that looks complete.

    Each case below is one edit away from a statement that parses into seven transactions. Every
    one of them must raise, and the assertion that matters is that nothing is returned at all.
    """

    def test_a_truncated_statement_raises_before_any_row_is_trusted(self) -> None:
        truncated = CDSL_STATEMENT.split("*** End of Statement ***", maxsplit=1)[0]
        assert "INE040A01034" in truncated
        with pytest.raises(TruncatedStatement, match="end-of-statement marker"):
            parse_cas(truncated)

    def test_a_truncation_that_cuts_a_row_in_half_also_raises(self) -> None:
        cut = CDSL_STATEMENT[: CDSL_STATEMENT.index("06/01/2026") + 40]
        with pytest.raises(CasParseError):
            parse_cas(cut)

    def test_a_row_that_disagrees_with_itself_raises(self) -> None:
        """23 x 1,625.24 is 37,380.52. A statement printing 37,880.52 has a corrupted digit, and
        that digit would become a cost basis nobody can see is wrong."""
        corrupt = CDSL_STATEMENT.replace("37,380.52", "37,880.52")
        with pytest.raises(MalformedStatement, match="disagrees with itself"):
            parse_cas(corrupt)

    def test_rounding_in_the_value_column_is_not_treated_as_corruption(self) -> None:
        """The guard has to survive the rounding statements actually do, or it is a guard that
        gets removed the first time a real statement fails it."""
        rounded = CDSL_STATEMENT.replace("37,380.52", "37,380.00")
        assert len(parse_cas(rounded).transactions) == 7

    def test_a_lost_column_raises_rather_than_shifting_every_field_left(self) -> None:
        shifted = CDSL_STATEMENT.replace(
            "BUY                12  3,456.78  41,481.36", "BUY                12  41,481.36"
        )
        with pytest.raises(MalformedStatement, match="7 columns"):
            parse_cas(shifted)

    def test_an_unknown_transaction_description_raises(self) -> None:
        """Most likely a corporate action. Ignoring it leaves shares in the position with no
        explanation of where they came from, which is what makes an average buy price a lie."""
        unknown = CDSL_STATEMENT.replace("SELL                2", "PLEDGE INVOKED      2")
        with pytest.raises(UnknownTransactionType, match="PLEDGE INVOKED"):
            parse_cas(unknown)

    def test_a_date_that_is_not_on_the_calendar_raises(self) -> None:
        with pytest.raises(MalformedStatement, match="not a date on the calendar"):
            parse_cas(
                CDSL_STATEMENT.replace("01/04/2025  INE040A01034", "31/02/2025  INE040A01034")
            )

    def test_a_quantity_that_is_not_a_number_raises(self) -> None:
        with pytest.raises(MalformedStatement, match="quantity column"):
            parse_cas(CDSL_STATEMENT.replace("BUY                23", "BUY                2O"))

    def test_a_statement_with_no_transaction_table_raises(self) -> None:
        """A holdings summary parses into no transactions at all, which would look like a
        successful import of a statement containing nothing."""
        headless = CDSL_STATEMENT.replace("Quantity  Price     Value\n", "\n")
        with pytest.raises(MalformedStatement, match="no transaction table header"):
            parse_cas(headless)

    def test_a_purchase_with_no_price_raises(self) -> None:
        """Dropping the money while keeping the quantity pulls the weighted average toward zero
        and every return derived from it upward."""
        priceless = NSDL_STATEMENT.replace(
            "Purchase                25  1,412.40  35,310.00", "Purchase                25  -     -"
        )
        with pytest.raises(MalformedStatement, match="must carry a price"):
            parse_cas(priceless)

    def test_a_statement_with_no_period_raises(self) -> None:
        undated = CDSL_STATEMENT.replace("Statement Period : 01/04/2025 to 31/03/2026\n", "")
        with pytest.raises(MalformedStatement, match="no statement period"):
            parse_cas(undated)

    def test_a_statement_with_no_account_raises(self) -> None:
        anonymous = CDSL_STATEMENT.replace("BO ID : 1208160012345678", "BO ID :")
        with pytest.raises(CasParseError):
            parse_cas(anonymous)

    def test_every_refusal_is_one_catchable_type(self) -> None:
        """A caller wrapping the import needs one `except`, or it will miss one of them."""
        for error in (
            UnknownStatementFormat,
            TruncatedStatement,
            MalformedStatement,
            UnknownTransactionType,
        ):
            assert issubclass(error, CasParseError)
            assert issubclass(error, ValueError)


class TestWeightedAverageBuyPrice:
    """§5.3's payoff: the two numbers that unlock §5.2's true XIRR and since-purchase P&L."""

    def test_the_weighted_average_is_exact_where_binary_arithmetic_drifts(self) -> None:
        """23 at 1,625.24 · 14 at 1,724.01 · 22 at 1,888.12 is exactly 1,746.70 a share.

        The same sum in binary arithmetic lands a few units in the last place below it. House rule
        9 is not stylistic here: this number is multiplied by the quantity to produce the invested
        amount and divided into the market value to produce every return figure §5.2 labels.
        """
        hdfc = cdsl_positions()[HDFC_ISIN]
        assert hdfc.bought_quantity == Decimal("59")
        assert hdfc.weighted_average_buy_price == Decimal("1746.70")

        drifting = (23 * 1625.24 + 14 * 1724.01 + 22 * 1888.12) / 59
        assert Decimal(repr(drifting)) != Decimal("1746.70")

    def test_the_first_bought_date_is_the_earliest_purchase(self) -> None:
        assert cdsl_positions()[HDFC_ISIN].first_bought_on == dt.date(2025, 4, 1)

    def test_a_sale_does_not_move_the_average_buy_price(self) -> None:
        """A sale realises a gain; it does not change what the remaining shares cost. Letting the
        sale price into the average would move the cost basis every time the user sold."""
        tcs = cdsl_positions()[TCS_ISIN]
        assert tcs.weighted_average_buy_price == Decimal("3456.78")
        assert tcs.bought_quantity == Decimal("12")
        assert tcs.net_quantity == Decimal("10")

    def test_the_invested_amount_is_rounded_once_from_the_total_cost(self) -> None:
        """3 at 999.99 plus 4 at 1,000.05 cost 7,000.17, and the average is 1,000.0242857…

        Rounding the average to paise first and multiplying back gives 7,000.14 — three paise the
        user really paid, gone. That is why the average is left unquantised and only the money is
        rounded, at the end (house rule 8).
        """
        itc = cdsl_positions()[ITC_ISIN]
        assert itc.invested_amount == Decimal("7000.17")
        assert itc.weighted_average_buy_price is not None
        assert itc.weighted_average_buy_price.quantize(Decimal("0.01")) * 7 == Decimal("7000.14")

    def test_unpriced_inflows_keep_the_purchase_history_locked(self) -> None:
        """§5.2: a holding group shows "since grouped" until the history is genuinely complete. A
        bonus issue means the known cost does not cover every share held."""
        reliance = nsdl_positions()[RELIANCE_ISIN]
        assert reliance.unpriced_inflow_quantity == Decimal("25")
        assert reliance.net_quantity == Decimal("50")
        assert reliance.unlocks_purchase_history is False

    def test_a_priced_history_with_an_outflow_still_unlocks(self) -> None:
        infosys = nsdl_positions()[INFOSYS_ISIN]
        assert infosys.net_quantity == Decimal("30")
        assert infosys.weighted_average_buy_price == Decimal("1555.25")
        assert infosys.unlocks_purchase_history is True

    def test_a_security_that_was_never_bought_has_no_average_price(self) -> None:
        """`None`, not zero — the same distinction `Holding.avg_price` draws, and the reason §5.2
        can tell "we do not know" from "it cost nothing"."""
        transferred = derive_positions(
            parse_cas(
                NSDL_STATEMENT.replace(
                    "Purchase                25  1,412.40  35,310.00",
                    "Off-market Credit       25  -         -",
                )
            ).transactions
        )
        reliance = next(p for p in transferred if p.isin == RELIANCE_ISIN)
        assert reliance.weighted_average_buy_price is None
        assert reliance.invested_amount is None
        assert reliance.first_bought_on is None
        assert reliance.unlocks_purchase_history is False


class TestReconciliation:
    """Matched, unmatched in the statement, unmatched in the portfolio. Nothing is dropped."""

    def holdings(self) -> list[Holding]:
        return [
            Holding(HoldingKey(1, ZERODHA), Decimal("59")),
            Holding(HoldingKey(2, ZERODHA), Decimal("10")),
            Holding(HoldingKey(4, ZERODHA), Decimal("100")),
        ]

    def test_a_position_pairs_with_the_holding_it_backfills(self) -> None:
        result = reconcile(list(cdsl_positions().values()), self.holdings(), INSTRUMENTS)
        matched = {m.position.isin: m for m in result.matched}
        assert set(matched) == {HDFC_ISIN, TCS_ISIN}
        assert matched[HDFC_ISIN].holding.key == HoldingKey(1, ZERODHA)
        assert matched[HDFC_ISIN].quantities_agree is True

    def test_a_position_the_portfolio_does_not_hold_is_reported(self) -> None:
        """Sold before the broker was connected, or held somewhere we do not sync. Either way the
        user is told, because a dropped position is a purchase history they think was imported."""
        result = reconcile(list(cdsl_positions().values()), self.holdings(), INSTRUMENTS)
        reasons = {u.position.isin: u.reason for u in result.unmatched_in_statement}
        assert reasons == {ITC_ISIN: UnmatchedReason.NOT_HELD}
        assert "do not hold it" in result.unmatched_in_statement[0].explanation

    def test_a_holding_the_statement_never_mentions_is_reported(self) -> None:
        """Expected, not exceptional: a CDSL statement is silent about NSDL holdings."""
        result = reconcile(list(cdsl_positions().values()), self.holdings(), INSTRUMENTS)
        assert [(u.key, u.reason) for u in result.unmatched_in_portfolio] == [
            (HoldingKey(4, ZERODHA), UnmatchedReason.NOT_IN_STATEMENT)
        ]

    def test_an_isin_with_no_instrument_is_reported_rather_than_skipped(self) -> None:
        without_itc = {HDFC_ISIN: 1, TCS_ISIN: 2}
        result = reconcile(list(cdsl_positions().values()), self.holdings(), without_itc)
        assert [u.reason for u in result.unmatched_in_statement] == [
            UnmatchedReason.NO_INSTRUMENT_FOR_ISIN
        ]

    def test_the_same_instrument_at_two_brokers_is_ambiguous_in_both_directions(self) -> None:
        """A CAS belongs to one demat account, so it cannot say which broker's shares it
        describes. Reported from both sides rather than attributed to whichever came first."""
        holdings = [
            Holding(HoldingKey(1, ZERODHA), Decimal("30")),
            Holding(HoldingKey(1, UPSTOX), Decimal("29")),
        ]
        result = reconcile([cdsl_positions()[HDFC_ISIN]], holdings, INSTRUMENTS)
        assert result.matched == ()
        assert [u.reason for u in result.unmatched_in_statement] == [
            UnmatchedReason.AMBIGUOUS_BROKER_ACCOUNT
        ]
        assert [u.reason for u in result.unmatched_in_portfolio] == [
            UnmatchedReason.AMBIGUOUS_BROKER_ACCOUNT,
            UnmatchedReason.AMBIGUOUS_BROKER_ACCOUNT,
        ]

    def test_every_position_and_every_holding_lands_in_exactly_one_bucket(self) -> None:
        positions = list(cdsl_positions().values())
        holdings = self.holdings()
        result = reconcile(positions, holdings, INSTRUMENTS)
        assert len(result.matched) + len(result.unmatched_in_statement) == len(positions)
        assert len(result.matched) + len(result.unmatched_in_portfolio) == len(holdings)


class TestBackfill:
    """The one place this module produces something the product treats as truth."""

    def matched_hdfc(self, quantity: str = "59") -> Holding:
        holding = Holding(HoldingKey(1, ZERODHA), Decimal(quantity))
        result = reconcile([cdsl_positions()[HDFC_ISIN]], [holding], INSTRUMENTS)
        return backfilled_holding(result.matched[0])

    def test_a_backfilled_holding_carries_the_statements_average_price(self) -> None:
        assert self.matched_hdfc().avg_price == Decimal("1746.70")

    def test_the_quantity_is_never_touched(self) -> None:
        """The broker is the authority on what is held now; the statement is history (§3)."""
        assert self.matched_hdfc().quantity == Decimal("59")

    def test_a_quantity_disagreement_refuses_the_backfill(self) -> None:
        """A statement covering one financial year against a position built over three describes
        some of the shares and none of the money paid for the rest."""
        with pytest.raises(ValueError, match="cannot establish a buy price"):
            self.matched_hdfc(quantity="100")

    def test_a_bonus_issue_refuses_the_backfill(self) -> None:
        reliance = nsdl_positions()[RELIANCE_ISIN]
        holding = Holding(HoldingKey(5, ZERODHA), Decimal("50"))
        result = reconcile([reliance], [holding], {RELIANCE_ISIN: 5})
        assert result.matched[0].quantities_agree is True
        assert result.matched[0].backfillable is False
        with pytest.raises(ValueError, match="arrived with no price"):
            backfilled_holding(result.matched[0])

    def test_backfillable_is_a_subset_of_matched(self) -> None:
        holdings = [
            Holding(HoldingKey(1, ZERODHA), Decimal("59")),
            Holding(HoldingKey(2, ZERODHA), Decimal("999")),
        ]
        result = reconcile(list(cdsl_positions().values()), holdings, INSTRUMENTS)
        assert len(result.matched) == 2
        assert [m.position.isin for m in result.backfillable] == [HDFC_ISIN]

    def test_an_unknown_average_price_before_the_import_is_not_a_zero(self) -> None:
        """§5.2 forbids a since-purchase figure for a holding whose buy price is unknown, and the
        import is the event that changes that. Before it, `None`; after it, a price."""
        before = Holding(HoldingKey(1, ZERODHA), Decimal("59"))
        assert before.avg_price is None
        assert self.matched_hdfc().avg_price is not None


class TestLaw1TouchesNothing:
    """`packages/core` touches nothing — CLAUDE.md law 1, asserted by scanning the source."""

    def test_the_module_imports_no_io(self) -> None:
        forbidden = re.findall(
            r"^\s*(?:import|from)\s+"
            r"(sqlalchemy|httpx|requests|redis|asyncpg|boto3|pathlib|os|io|tempfile)\b",
            _module_source(),
            re.MULTILINE,
        )
        assert forbidden == []

    def test_the_module_imports_no_pdf_decoder(self) -> None:
        """§5.3 says "PDF parse", and the PDF half is I/O: it opens a file, needs a third-party
        decoder and, on a password-protected statement, a secret. It lives in `services/`."""
        forbidden = re.findall(
            r"^\s*(?:import|from)\s+"
            r"(pypdf|PyPDF2|pdfplumber|fitz|pikepdf|camelot|tabula|pdfminer|baskfy_core\.pdf)\b",
            _module_source(),
            re.MULTILINE,
        )
        assert forbidden == []

    def test_the_module_never_reads_a_clock(self) -> None:
        """Every date here comes out of the statement. A parser that could read today's date would
        be a parser whose "first bought" answer depended on when it ran."""
        assert (
            re.findall(
                r"\b(?:datetime\.now|dt\.datetime\.now|dt\.date\.today|time\.time)\s*\(",
                _module_source(),
            )
            == []
        )

    def test_the_module_names_no_order(self) -> None:
        """Non-negotiable #1: nothing in core drifts toward an execution path."""
        assert (
            re.findall(
                r"\b(place_order|order_type|transact_type|exchange_segment)\b", _module_source()
            )
            == []
        )

    def test_holdings_are_not_restated(self) -> None:
        """`Holding` and `HoldingKey` come from the ledger. A second definition would let the
        ledger and the importer disagree about what a holding is, which is exactly the
        disagreement that lands a backfill on the wrong position."""
        source = _module_source()
        assert "from baskfy_core.allocation_ledger import Holding, HoldingKey" in source
        assert re.findall(r"^class (?:Holding|HoldingKey)\b", source, re.MULTILINE) == []


class TestMoneyIsDecimal:
    """House rule 9 — money and prices are `numeric`, never binary."""

    def test_the_module_never_names_the_binary_type(self) -> None:
        """House rule 9 is about *this* module, so scan it (the same guard the ledger carries)."""
        assert re.findall(r"\bfloat\b", _module_source()) == []

    def test_every_parsed_price_is_a_decimal(self) -> None:
        for statement in (parse_cas(CDSL_STATEMENT), parse_cas(NSDL_STATEMENT)):
            assert isinstance(statement, CasStatement)
            for transaction in statement.transactions:
                assert isinstance(transaction.quantity, Decimal)
                assert transaction.price is None or isinstance(transaction.price, Decimal)
