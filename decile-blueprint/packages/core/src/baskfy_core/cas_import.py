"""CAS import — `PORTFOLIO_REDESIGN.md` §5.3, as pure text parsing and reconciliation.

§5.3 in one sentence: *broker APIs often lack pre-connection buy history, so a CDSL or NSDL
consolidated account statement is imported to backfill buy prices and dates, and only after that
import does a holding group unlock true XIRR and since-purchase P&L (§5.2); before it, the group
shows "since grouped" and nothing else.* This module is the arithmetic and the grammar of that
import. It is Phase 3 in §10's build order and it is the most dangerous thing in the phase.

WHY THIS TAKES TEXT AND NOT A PDF
---------------------------------
A CAS arrives as a PDF, and turning a PDF into text is I/O: it opens a file, it needs a third
party decoder, and on a password protected statement it needs a secret. All three are forbidden
here by law 1 — `packages/core` touches nothing — so extraction lives in `services/`, which reads
the upload, decrypts it, runs the extractor and hands the resulting string to :func:`parse_cas`.

That split is not bureaucracy. It is what makes the risky half testable: every statement shape
this module has to survive — a truncated page, a mangled column, a depository whose vocabulary we
have never seen — is a *string*, so it is a fixture rather than a binary nobody can review in a
diff. Nothing below imports a decoder, reads a clock or knows today's date; the statement's own
period line is the only calendar in play.

**The layout contract.** The extractor must preserve column gaps: two or more spaces between
columns, one row per line (``pdftotext -layout`` and pdfplumber's ``extract_text`` both do). A
row is split on runs of two or more spaces, which is what lets a security name and a transaction
description keep their internal single spaces. A statement that arrives with columns collapsed
into single spaces does not parse into fewer transactions — it raises.

THE WORST FAILURE THIS FEATURE CAN HAVE
---------------------------------------
Not a rejected upload. A *partial* parse that looks complete. Half a statement still produces an
average buy price, that price is still a number, and that number silently becomes the cost basis
under every return figure the user is ever shown — XIRR, since-purchase P&L, invested amount, the
consolidated hero metrics in §6.2. Nobody can see it is wrong by looking at it, and it is wrong
forever.

So every ambiguity here raises. A statement with no depository markers raises; one with markers
for both raises; a row whose column count is wrong raises; a transaction description outside the
known vocabulary raises; a row whose printed value disagrees with quantity times price raises; a
statement missing its end-of-statement marker raises as truncated *before* any row is read. There
is no ``skip_bad_rows`` parameter and no partial result type, because the only safe half-parse is
the one that never reaches the caller.

The same instinct runs through the reconciliation half. A position in the statement that no
holding matches is *reported*, never dropped; a holding that the statement does not mention is
reported too, because a CAS covers one depository and a portfolio can span both. And an ISIN that
resolves to an instrument held in two broker accounts is reported as ambiguous rather than
attributed to whichever one came first — the same refusal to guess that `allocation_ledger`'s
reconciliation inbox is built on (§4.3).

WHAT THIS IS NOT
----------------
Not an importer. It writes nothing, and it never decides that an import happened. Not a valuation
either: it derives what was paid, never what anything is worth now. And it is not a sync — a CAS
is a historical document, so a position derived here is a claim about the past that
:func:`reconcile` measures against the present, rather than a new picture of the present.

`Holding` and `HoldingKey` come from `allocation_ledger` unchanged. Restating them here would let
the ledger and the importer disagree about what a holding is, which is precisely the disagreement
that would let a backfill land on the wrong position.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

from baskfy_core.allocation_ledger import Holding, HoldingKey
from baskfy_core.gst import money

__all__ = [
    "CasMatch",
    "CasParseError",
    "CasPosition",
    "CasReconciliation",
    "CasStatement",
    "CasTransaction",
    "CasTransactionType",
    "Depository",
    "MalformedStatement",
    "TruncatedStatement",
    "UnknownStatementFormat",
    "UnknownTransactionType",
    "UnmatchedHolding",
    "UnmatchedPosition",
    "UnmatchedReason",
    "backfilled_holding",
    "derive_positions",
    "detect_depository",
    "parse_cas",
    "reconcile",
]


# ---------------------------------------------------------------------------
# Errors — every one of them names what was wrong
# ---------------------------------------------------------------------------


class CasParseError(ValueError):
    """A statement that could not be read *completely*, and therefore was not read at all.

    A ``ValueError`` because a statement is a value the caller supplied, and because the API layer
    already turns one into a 422 with the message shown to the user. The message is part of the
    contract: "your statement could not be read" is useless to someone holding a PDF, so every
    subclass below names the page, the line or the word that stopped it.
    """


class UnknownStatementFormat(CasParseError):
    """The text is not recognisably CDSL's or NSDL's — or it is recognisably both.

    Both cases are refusals, and the second one matters more than it looks: the two depositories
    order their columns differently, so guessing wrong reads a *date* column as a quantity and
    still produces numbers.
    """


class TruncatedStatement(CasParseError):
    """The statement ended early. Raised before a single row is trusted.

    A CAS ends with an explicit end-of-statement marker. Its absence means pages were lost —
    truncated upload, a failed extraction, a user who exported one page of six — and the rows that
    *did* arrive are the most dangerous data this module can see: they parse perfectly and they
    describe part of a purchase history.
    """


class UnknownTransactionType(CasParseError):
    """A transaction description outside the known vocabulary.

    Deliberately fatal rather than ignorable. An unrecognised description is most likely a
    corporate action or an inter-depository move, and treating it as noise would leave shares in
    the position with no explanation of where they came from — which is exactly the state that
    makes an average buy price a lie.
    """


class MalformedStatement(CasParseError):
    """A row or a header that did not parse. Carries the line so a human can look at it."""

    def __init__(
        self,
        problem: str,
        *,
        line_number: int | None = None,
        line: str | None = None,
    ) -> None:
        where = f" at line {line_number}" if line_number is not None else ""
        excerpt = f": {line.strip()!r}" if line else ""
        super().__init__(f"{problem}{where}{excerpt}")
        self.problem = problem
        self.line_number = line_number
        self.line = line


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class Depository(StrEnum):
    """The two Indian depositories. A CAS is issued by one of them, in its own layout."""

    CDSL = "CDSL"
    NSDL = "NSDL"


class CasTransactionType(StrEnum):
    """What a row did to the position, reduced to the five cases §5.3 has to distinguish."""

    BUY = "BUY"
    SELL = "SELL"
    BONUS = "BONUS"
    TRANSFER_IN = "TRANSFER_IN"
    TRANSFER_OUT = "TRANSFER_OUT"

    @property
    def carries_price(self) -> bool:
        """True where a missing price makes the row meaningless rather than merely incomplete.

        A bonus issue or an off-market transfer legitimately prints no price — nothing was paid,
        or the price was agreed off the exchange and the depository never saw it. A purchase or a
        sale without a price is a broken row, and accepting one would drop real money out of the
        weighted average without changing the quantity it is divided by.
        """
        return self in {CasTransactionType.BUY, CasTransactionType.SELL}

    @property
    def is_inflow(self) -> bool:
        return self in {
            CasTransactionType.BUY,
            CasTransactionType.BONUS,
            CasTransactionType.TRANSFER_IN,
        }


#: Both depositories' descriptions, normalised to upper case with hyphens read as spaces, in one
#: table. They overlap heavily ("Purchase"/"BUY", "Off-market Credit"/"OFF MARKET CREDIT") and
#: keeping two tables would mean a word learned for one depository silently staying unknown for
#: the other — a difference with no reason behind it.
_TRANSACTION_VOCABULARY: Final[Mapping[str, CasTransactionType]] = {
    "BUY": CasTransactionType.BUY,
    "PURCHASE": CasTransactionType.BUY,
    "MARKET PURCHASE": CasTransactionType.BUY,
    "CREDIT PURCHASE": CasTransactionType.BUY,
    "SELL": CasTransactionType.SELL,
    "SALE": CasTransactionType.SELL,
    "MARKET SALE": CasTransactionType.SELL,
    "DEBIT SALE": CasTransactionType.SELL,
    "BONUS": CasTransactionType.BONUS,
    "BONUS ISSUE": CasTransactionType.BONUS,
    "OFF MARKET CREDIT": CasTransactionType.TRANSFER_IN,
    "INTER DEPOSITORY CREDIT": CasTransactionType.TRANSFER_IN,
    "TRANSFER IN": CasTransactionType.TRANSFER_IN,
    "OFF MARKET DEBIT": CasTransactionType.TRANSFER_OUT,
    "INTER DEPOSITORY DEBIT": CasTransactionType.TRANSFER_OUT,
    "TRANSFER OUT": CasTransactionType.TRANSFER_OUT,
}

#: An Indian ISIN: ``IN`` then ten alphanumerics. Narrow on purpose — it is also how a transaction
#: row is told apart from a heading, so a looser pattern would start parsing page furniture.
_ISIN_RE: Final = re.compile(r"\bIN[A-Z0-9]{10}\b")

#: Markers scored per depository. Structural as well as nominal, because NSDL's own eCAS
#: summarises holdings held at CDSL: the word "CDSL" appearing somewhere is not evidence, while
#: "BO ID" (CDSL's account label) against "DP ID"/"Client ID" (NSDL's) is.
_MARKERS: Final[Mapping[Depository, tuple[str, ...]]] = {
    Depository.CDSL: (
        r"CENTRAL DEPOSITORY SERVICES",
        r"\bBO\s*ID\b",
        r"\bCDSL\b",
    ),
    Depository.NSDL: (
        r"NATIONAL SECURITIES DEPOSITORY",
        r"\bDP\s*ID\b",
        r"\bCLIENT\s*ID\b",
        r"\bNSDL\b",
    ),
}

_MONTHS: Final[Mapping[str, int]] = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

#: The column header that must exist before any row is read.
_TABLE_HEADER_RE: Final = re.compile(r"ISIN.*QUANTITY", re.IGNORECASE)
#: The end-of-statement marker whose absence means pages were lost.
_END_MARKER_RE: Final = re.compile(r"END OF (?:STATEMENT|REPORT)", re.IGNORECASE)
#: Columns are separated by two or more spaces. See "The layout contract" above.
_COLUMN_GAP_RE: Final = re.compile(r"\s{2,}")

_CDSL_PERIOD_RE: Final = re.compile(r"(\d{2}/\d{2}/\d{4})\s+to\s+(\d{2}/\d{2}/\d{4})", re.I)
_NSDL_PERIOD_RE: Final = re.compile(
    r"(\d{2}-[A-Za-z]{3}-\d{4})\s+to\s+(\d{2}-[A-Za-z]{3}-\d{4})", re.I
)
_CDSL_ACCOUNT_RE: Final = re.compile(r"BO\s*ID\s*[:\-]?\s*([0-9][0-9 ]{9,})", re.I)
_NSDL_DP_RE: Final = re.compile(r"DP\s*ID\s*[:\-]?\s*([A-Z0-9]{6,})", re.I)
_NSDL_CLIENT_RE: Final = re.compile(r"CLIENT\s*ID\s*[:\-]?\s*([A-Z0-9]{6,})", re.I)

#: A row prints its price rounded to paise, so ``quantity * price`` and the printed value differ
#: by up to half a paisa per share. Anything past that is a corrupted digit, not rounding.
_VALUE_ROUNDING_SLACK: Final = Decimal("0.005")
#: …with a floor, because a five-share row has almost no slack and statements themselves round
#: the value column to the rupee often enough that a hard zero would reject clean statements.
_MINIMUM_VALUE_SLACK: Final = Decimal("1.00")

#: How many columns a transaction row has, in both layouts. The orders differ; the count does not.
_ROW_COLUMNS: Final = 7


# ---------------------------------------------------------------------------
# Parsed records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CasTransaction:
    """One row of a transaction statement, typed.

    ``symbol`` is the depository's **security description** — "HDFC BANK LTD", "INFOSYS LIMITED" —
    and not an exchange trading symbol. The two are not the same string and never reliably will
    be: CDSL and NSDL print different descriptions for the same company, and neither is what NSE
    calls it. Resolving a position to an instrument is done on ``isin`` (see :func:`reconcile`),
    which is why ``symbol`` is carried for display and audit only. A caller that matches on it is
    matching on a label the depository is free to change.

    ``price`` and ``value`` are ``None`` where the statement printed none, which is normal for a
    bonus issue and for an off-market transfer. That is not a zero: a zero would say the shares
    were free, and a bonus issue's shares were paid for by the dilution of the ones already held.
    """

    isin: str
    symbol: str
    on: dt.date
    kind: CasTransactionType
    quantity: Decimal
    price: Decimal | None = None
    value: Decimal | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise MalformedStatement(
                f"a transaction moves a positive number of shares; got {self.quantity} "
                f"for {self.isin}"
            )
        if self.price is not None and self.price < 0:
            raise MalformedStatement(f"a price cannot be negative; got {self.price}")
        if self.kind.carries_price and self.price is None:
            raise MalformedStatement(
                f"a {self.kind} row must carry a price — dropping the money while keeping the "
                f"quantity would pull the weighted average buy price toward zero ({self.isin})"
            )


@dataclass(frozen=True, slots=True)
class CasStatement:
    """A whole statement, with the period it covers and the account it belongs to.

    The period is kept because it is the honest bound on everything derived below: a statement
    covering one financial year cannot know about a purchase made before it, and
    :func:`reconcile` uses the resulting quantity disagreement to refuse the backfill rather than
    trusting an average price computed from part of the history.
    """

    depository: Depository
    account_id: str
    period_start: dt.date
    period_end: dt.date
    transactions: tuple[CasTransaction, ...]


@dataclass(frozen=True, slots=True)
class CasPosition:
    """What the statement says about one ISIN — the thing §5.3 exists to produce.

    ``first_bought_on`` and ``weighted_average_buy_price`` are the two numbers that unlock §5.2's
    true XIRR and since-purchase P&L for a holding group. Both are ``None`` when the statement
    contains no purchase of this security, and ``None`` is not zero: `allocation_ledger.Holding`
    draws exactly that distinction for ``avg_price``, and this module preserves it end to end.

    ``weighted_average_buy_price`` is deliberately **not** quantised to paise. It is a divisor,
    not a printed figure: rounding it and then multiplying by the quantity again multiplies the
    rounding error by that quantity, so a 400-share position would report an invested amount two
    rupees away from what was actually paid. The quantised figure the user reads is
    ``invested_amount``, which is rounded once, at the end, from the exact total cost — the same
    "round at write time" rule house rule 8 states and `gst.money` implements.

    ``unpriced_inflow_quantity`` is the shares that arrived with no price: a bonus issue, an
    off-market credit. They are the reason :attr:`unlocks_purchase_history` exists. Dividing a
    known cost by a quantity that includes them produces an average price that is too high, every
    return derived from it is then too low, and nothing about the number looks wrong.
    """

    isin: str
    symbol: str
    first_bought_on: dt.date | None
    weighted_average_buy_price: Decimal | None
    bought_quantity: Decimal
    invested_amount: Decimal | None
    net_quantity: Decimal
    unpriced_inflow_quantity: Decimal

    @property
    def unlocks_purchase_history(self) -> bool:
        """Whether §5.2 may show true XIRR and since-purchase P&L for this security.

        Three conditions, all of them about honesty rather than convenience: something was bought
        (there is a cost at all), nothing arrived unpriced (the cost covers every share held), and
        the position is not closed (there is something left to measure). Anything short of that
        keeps the holding group on "since grouped", which is what §5.2 says it must show.
        """
        return (
            self.bought_quantity > 0
            and self.unpriced_inflow_quantity == 0
            and self.net_quantity > 0
        )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RawRow:
    """One row's columns after the layout is understood but before anything is trusted.

    A record rather than nine positional arguments so that the two layouts converge on a single
    validation path: the only thing CDSL and NSDL disagree about is which column is which, and
    everything that can be *wrong* with a row is the same for both.
    """

    isin: str
    symbol: str
    on: dt.date
    description: str
    quantity_text: str
    price_text: str
    value_text: str
    line_number: int
    line: str


def detect_depository(text: str) -> Depository:
    """Which depository issued this statement, decided from the text alone.

    No parameter tells us. A caller that could pass the format would eventually pass the wrong
    one — an upload form's dropdown, a filename convention, a user in a hurry — and the two
    layouts order their columns differently enough that the wrong guess parses cleanly into
    nonsense rather than failing.

    Scored rather than matched on a single phrase, because NSDL's eCAS summarises holdings that
    sit at CDSL and therefore contains the other depository's name. A tie is not resolved by
    preferring one: it raises, because a tie means the evidence genuinely does not distinguish
    them, and a coin flip here corrupts every price it touches.
    """
    scores = {
        depository: sum(1 for marker in markers if re.search(marker, text, re.IGNORECASE))
        for depository, markers in _MARKERS.items()
    }
    best = max(scores.values())
    if best == 0:
        raise UnknownStatementFormat(
            "this text carries no CDSL or NSDL markers, so it is not a consolidated account "
            "statement this module knows how to read; check that the PDF extraction produced "
            "text rather than an empty page"
        )
    winners = sorted(name for name, score in scores.items() if score == best)
    if len(winners) > 1:
        raise UnknownStatementFormat(
            f"this text matches {' and '.join(winners)} equally, and the two depositories order "
            "their transaction columns differently — reading it as the wrong one would parse "
            "into numbers that are wrong rather than into an error"
        )
    return Depository(winners[0])


def parse_cas(text: str) -> CasStatement:
    """Parse an extracted CDSL or NSDL statement, or raise. There is no third outcome.

    The order of the checks is the point. Format, period, account and table header come first
    because each is cheap and each failure is unambiguous; the end-of-statement marker is checked
    **before any row is parsed**, so a truncated upload is refused while its surviving rows are
    still untouched rather than after they have been turned into a plausible purchase history.

    Rows are then every line carrying an ISIN. A line that carries one and does not parse stops
    the whole statement — it is never skipped, because the rows that skip cleanly are exactly the
    ones that would leave a purchase out of the average.
    """
    depository = detect_depository(text)
    lines = text.splitlines()
    period_start, period_end = _parse_period(text, depository)
    account_id = _parse_account_id(text, depository)

    if not any(_TABLE_HEADER_RE.search(line) for line in lines):
        raise MalformedStatement(
            "no transaction table header (a line naming both ISIN and Quantity) was found; the "
            "extractor may have produced a holdings summary rather than the transaction statement"
        )
    if not _END_MARKER_RE.search(text):
        raise TruncatedStatement(
            "the statement has no end-of-statement marker, so pages are missing; a partial "
            "purchase history would backfill an average buy price that looks right and is not"
        )

    parse_row = _CDSL_ROW_PARSER if depository is Depository.CDSL else _NSDL_ROW_PARSER
    transactions = tuple(
        parse_row(line, number)
        for number, line in enumerate(lines, start=1)
        if _ISIN_RE.search(line)
    )
    return CasStatement(
        depository=depository,
        account_id=account_id,
        period_start=period_start,
        period_end=period_end,
        transactions=transactions,
    )


def _parse_period(text: str, depository: Depository) -> tuple[dt.date, dt.date]:
    """The window the statement covers. Required, because it bounds what the statement can know."""
    if depository is Depository.CDSL:
        found = _CDSL_PERIOD_RE.search(text)
        if found is None:
            raise MalformedStatement("no statement period (dd/mm/yyyy to dd/mm/yyyy) was found")
        start, end = _date_from_slashes(found.group(1)), _date_from_slashes(found.group(2))
    else:
        found = _NSDL_PERIOD_RE.search(text)
        if found is None:
            raise MalformedStatement("no statement period (dd-Mon-yyyy to dd-Mon-yyyy) was found")
        start, end = _date_from_month_name(found.group(1)), _date_from_month_name(found.group(2))
    if end < start:
        raise MalformedStatement(f"the statement period ends before it starts: {start} to {end}")
    return start, end


def _parse_account_id(text: str, depository: Depository) -> str:
    """The demat account the statement belongs to, in whichever label its depository uses."""
    if depository is Depository.CDSL:
        found = _CDSL_ACCOUNT_RE.search(text)
        if found is None:
            raise MalformedStatement("no BO ID was found, so this statement names no account")
        return found.group(1).replace(" ", "")
    dp_id = _NSDL_DP_RE.search(text)
    client_id = _NSDL_CLIENT_RE.search(text)
    if dp_id is None or client_id is None:
        raise MalformedStatement(
            "an NSDL statement identifies its account by DP ID and Client ID together, and one "
            "of the two is missing"
        )
    return f"{dp_id.group(1)}-{client_id.group(1)}"


def _columns(line: str, line_number: int) -> list[str]:
    """Split a row on the layout contract, and refuse anything that is not seven columns.

    The count check is the cheapest guard in the module and it catches the failure that matters:
    an extractor that lost a column shifts every field left, so a price is read as a quantity and
    the row still parses into numbers.
    """
    fields = [field for field in _COLUMN_GAP_RE.split(line.strip()) if field]
    if len(fields) != _ROW_COLUMNS:
        raise MalformedStatement(
            f"expected {_ROW_COLUMNS} columns separated by two or more spaces, found {len(fields)}",
            line_number=line_number,
            line=line,
        )
    return fields


def _cdsl_row(line: str, line_number: int) -> CasTransaction:
    """CDSL prints the date first: date, ISIN, security, transaction, quantity, price, value."""
    date_text, isin, symbol, description, quantity, price, value = _columns(line, line_number)
    return _to_transaction(
        _RawRow(
            isin=isin,
            symbol=symbol,
            on=_date_from_slashes(date_text, line_number=line_number, line=line),
            description=description,
            quantity_text=quantity,
            price_text=price,
            value_text=value,
            line_number=line_number,
            line=line,
        )
    )


def _nsdl_row(line: str, line_number: int) -> CasTransaction:
    """NSDL prints the ISIN first: ISIN, security, date, description, quantity, price, value."""
    isin, symbol, date_text, description, quantity, price, value = _columns(line, line_number)
    return _to_transaction(
        _RawRow(
            isin=isin,
            symbol=symbol,
            on=_date_from_month_name(date_text, line_number=line_number, line=line),
            description=description,
            quantity_text=quantity,
            price_text=price,
            value_text=value,
            line_number=line_number,
            line=line,
        )
    )


_CDSL_ROW_PARSER: Final = _cdsl_row
_NSDL_ROW_PARSER: Final = _nsdl_row


def _to_transaction(row: _RawRow) -> CasTransaction:
    """Validate one row's contents, whichever layout it came from."""
    if _ISIN_RE.fullmatch(row.isin) is None:
        raise MalformedStatement(
            f"{row.isin!r} is not in the ISIN column",
            line_number=row.line_number,
            line=row.line,
        )
    kind = _transaction_kind(row)
    quantity = _decimal(row.quantity_text, "quantity", row)
    price = _optional_decimal(row.price_text, "price", row)
    value = _optional_decimal(row.value_text, "value", row)
    if price is not None and value is not None:
        _check_printed_value(quantity, price, value, row)
    return CasTransaction(
        isin=row.isin,
        symbol=" ".join(row.symbol.split()),
        on=row.on,
        kind=kind,
        quantity=quantity,
        price=price,
        value=value,
    )


def _transaction_kind(row: _RawRow) -> CasTransactionType:
    """Map a description to a type, or raise naming the word we did not know."""
    normalised = " ".join(row.description.replace("-", " ").upper().split())
    kind = _TRANSACTION_VOCABULARY.get(normalised)
    if kind is None:
        raise UnknownTransactionType(
            f"line {row.line_number} describes its transaction as {row.description.strip()!r}, "
            "which is not in the known vocabulary; it is most likely a corporate action or an "
            "inter-depository move, and ignoring it would leave shares in the position with no "
            "explanation of where they came from"
        )
    return kind


def _check_printed_value(quantity: Decimal, price: Decimal, value: Decimal, row: _RawRow) -> None:
    """Cross-check the row against itself: quantity times price must be the printed value.

    Text extracted from a PDF loses and transposes digits, and a wrong digit in a price column is
    invisible downstream — it becomes the cost basis and stays there. The statement prints the
    product as its own third number, so the row can be made to prove itself. The tolerance is the
    rounding the statement itself did, not a fudge factor: prices are printed to paise, so the
    product can be out by half a paisa per share, with a one-rupee floor for the rupee-rounded
    value columns some statements use.
    """
    slack = max(_MINIMUM_VALUE_SLACK, quantity * _VALUE_ROUNDING_SLACK)
    if abs(quantity * price - value) > slack:
        raise MalformedStatement(
            f"quantity {quantity} times price {price} is {quantity * price}, but the row prints "
            f"a value of {value}; a row that disagrees with itself has a corrupted digit",
            line_number=row.line_number,
            line=row.line,
        )


def _decimal(text: str, field: str, row: _RawRow) -> Decimal:
    """A required number, comma-grouped as Indian statements print it.

    ``Decimal`` from the start, never through a binary intermediate: house rule 9, and here it is
    load-bearing rather than stylistic, because these numbers are multiplied by share counts and
    then divided again to produce the average price every return figure hangs off.
    """
    cleaned = text.replace(",", "").replace("₹", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation as error:
        raise MalformedStatement(
            f"the {field} column reads {text.strip()!r}, which is not a number",
            line_number=row.line_number,
            line=row.line,
        ) from error


def _optional_decimal(text: str, field: str, row: _RawRow) -> Decimal | None:
    """A number the statement is allowed to omit, printed as ``-``, ``NA`` or nothing at all."""
    if text.strip() in {"", "-", "--", "NA", "N.A.", "N/A"}:
        return None
    return _decimal(text, field, row)


def _date_from_slashes(
    text: str, *, line_number: int | None = None, line: str | None = None
) -> dt.date:
    """``dd/mm/yyyy`` — CDSL's format. Parsed by hand rather than by ``strptime``.

    Not for speed. ``strptime`` reads month names through the process locale, so the same file
    would parse on a developer's machine and fail on a server started with a different ``LC_TIME``
    — a difference nobody would look for. Digits and an explicit month table have no locale.
    """
    found = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", text.strip())
    if found is None:
        raise MalformedStatement(
            f"{text.strip()!r} is not a dd/mm/yyyy date", line_number=line_number, line=line
        )
    day, month, year = (int(part) for part in found.groups())
    return _date((year, month, day), text, line_number, line)


def _date_from_month_name(
    text: str, *, line_number: int | None = None, line: str | None = None
) -> dt.date:
    """``dd-Mon-yyyy`` — NSDL's format. Same reasoning, plus an explicit month table."""
    found = re.fullmatch(r"(\d{2})-([A-Za-z]{3})-(\d{4})", text.strip())
    if found is None:
        raise MalformedStatement(
            f"{text.strip()!r} is not a dd-Mon-yyyy date", line_number=line_number, line=line
        )
    month = _MONTHS.get(found.group(2).upper())
    if month is None:
        raise MalformedStatement(
            f"{found.group(2)!r} is not a month name", line_number=line_number, line=line
        )
    parts = (int(found.group(3)), month, int(found.group(1)))
    return _date(parts, text, line_number, line)


def _date(
    parts: tuple[int, int, int], text: str, line_number: int | None, line: str | None
) -> dt.date:
    """Build the date from ``(year, month, day)``, turning the calendar's own refusal into this
    module's error type — 31 February parses as three integers and is still not a date."""
    year, month, day = parts
    try:
        return dt.date(year, month, day)
    except ValueError as error:
        raise MalformedStatement(
            f"{text.strip()!r} is not a date on the calendar",
            line_number=line_number,
            line=line,
        ) from error


# ---------------------------------------------------------------------------
# Derivation — the two numbers §5.2 is waiting for
# ---------------------------------------------------------------------------


def derive_positions(transactions: Sequence[CasTransaction]) -> tuple[CasPosition, ...]:
    """Collapse transactions into one position per ISIN, in first-seen order.

    The weighted average is ``sum(quantity * price) / sum(quantity)`` over **purchases only**.
    Sales are excluded deliberately: a sale realises a gain, it does not change what the remaining
    shares cost, and letting a sale price into the average would move the cost basis every time
    the user sold something. That is the identity `allocation_ledger.apply_corporate_action` also
    protects — cost basis changes only when money changes hands in the buying direction.

    Order is first appearance rather than sorted, so a caller rendering the import preview shows
    the statement back to the user in the order they can read it off the PDF.
    """
    order: list[str] = []
    symbols: dict[str, str] = {}
    bought: dict[str, Decimal] = {}
    cost: dict[str, Decimal] = {}
    first_buy: dict[str, dt.date] = {}
    net: dict[str, Decimal] = {}
    unpriced: dict[str, Decimal] = {}

    for transaction in transactions:
        isin = transaction.isin
        if isin not in symbols:
            order.append(isin)
            symbols[isin] = transaction.symbol
            bought[isin] = Decimal("0")
            cost[isin] = Decimal("0")
            net[isin] = Decimal("0")
            unpriced[isin] = Decimal("0")
        net[isin] += transaction.quantity if transaction.kind.is_inflow else -transaction.quantity
        if transaction.kind is CasTransactionType.BUY and transaction.price is not None:
            bought[isin] += transaction.quantity
            cost[isin] += transaction.quantity * transaction.price
            previous = first_buy.get(isin)
            if previous is None or transaction.on < previous:
                first_buy[isin] = transaction.on
        elif transaction.kind.is_inflow and transaction.price is None:
            unpriced[isin] += transaction.quantity

    return tuple(
        CasPosition(
            isin=isin,
            symbol=symbols[isin],
            first_bought_on=first_buy.get(isin),
            weighted_average_buy_price=(cost[isin] / bought[isin]) if bought[isin] > 0 else None,
            bought_quantity=bought[isin],
            invested_amount=money(cost[isin]) if bought[isin] > 0 else None,
            net_quantity=net[isin],
            unpriced_inflow_quantity=unpriced[isin],
        )
        for isin in order
    )


# ---------------------------------------------------------------------------
# Reconciliation — nothing is dropped, in either direction
# ---------------------------------------------------------------------------


class UnmatchedReason(StrEnum):
    """Why a position or a holding could not be paired. Reported, never used to discard one."""

    NO_INSTRUMENT_FOR_ISIN = "NO_INSTRUMENT_FOR_ISIN"
    NOT_HELD = "NOT_HELD"
    NOT_IN_STATEMENT = "NOT_IN_STATEMENT"
    AMBIGUOUS_BROKER_ACCOUNT = "AMBIGUOUS_BROKER_ACCOUNT"

    @property
    def explanation(self) -> str:
        """The sentence the import preview shows. A reason code helps nobody holding a PDF."""
        return {
            UnmatchedReason.NO_INSTRUMENT_FOR_ISIN: (
                "We do not have this security in our instrument list, so we cannot tell which "
                "holding it is."
            ),
            UnmatchedReason.NOT_HELD: (
                "Your statement shows this security, but you do not hold it in any connected "
                "broker account — it was probably sold or moved before you connected."
            ),
            UnmatchedReason.NOT_IN_STATEMENT: (
                "You hold this, but this statement does not mention it — a statement covers one "
                "depository and one period, so import the other one too."
            ),
            UnmatchedReason.AMBIGUOUS_BROKER_ACCOUNT: (
                "You hold this security in more than one broker account, and the statement "
                "cannot say which of them these purchases belong to."
            ),
        }[self]


@dataclass(frozen=True, slots=True)
class CasMatch:
    """A statement position paired with the holding it backfills.

    Matched is not the same as safe. ``quantity_delta`` is what the statement failed to explain,
    and it is almost always a statement that starts after the first purchase: a CAS covering one
    financial year against a position built over three describes some of the shares and none of
    the money paid for the rest. :attr:`backfillable` is the gate, and it is deliberately strict.
    """

    position: CasPosition
    holding: Holding

    @property
    def quantity_delta(self) -> Decimal:
        """Statement minus portfolio. Zero means the statement explains the whole position."""
        return self.position.net_quantity - self.holding.quantity

    @property
    def quantities_agree(self) -> bool:
        return self.quantity_delta == 0

    @property
    def backfillable(self) -> bool:
        """Whether this pair may set an average buy price on the holding.

        Both halves have to hold: the statement's own purchase history must be complete
        (:attr:`CasPosition.unlocks_purchase_history`), and it must account for exactly the shares
        actually held. Either one alone is not enough — a complete-looking history of half the
        position is the specific failure that makes every downstream return number wrong while
        looking entirely ordinary.
        """
        return self.position.unlocks_purchase_history and self.quantities_agree


@dataclass(frozen=True, slots=True)
class UnmatchedPosition:
    """Something the statement contains that the portfolio could not account for."""

    position: CasPosition
    reason: UnmatchedReason

    @property
    def explanation(self) -> str:
        return self.reason.explanation


@dataclass(frozen=True, slots=True)
class UnmatchedHolding:
    """Something the portfolio holds that the statement did not mention."""

    key: HoldingKey
    reason: UnmatchedReason

    @property
    def explanation(self) -> str:
        return self.reason.explanation


@dataclass(frozen=True, slots=True)
class CasReconciliation:
    """The three buckets, and the guarantee that everything is in exactly one of them."""

    matched: tuple[CasMatch, ...]
    unmatched_in_statement: tuple[UnmatchedPosition, ...]
    unmatched_in_portfolio: tuple[UnmatchedHolding, ...]

    @property
    def backfillable(self) -> tuple[CasMatch, ...]:
        """The subset an import may actually write. Usually smaller than :attr:`matched`."""
        return tuple(match for match in self.matched if match.backfillable)


def reconcile(
    positions: Sequence[CasPosition],
    holdings: Sequence[Holding],
    instrument_ids_by_isin: Mapping[str, int],
) -> CasReconciliation:
    """Pair statement positions with holdings, reporting everything that does not pair.

    Matching is on ISIN, resolved to an instrument by a mapping the caller supplies, because
    resolving an ISIN is a database lookup and law 1 keeps that out of here. A holding is
    ``(instrument, broker account)``, so one ISIN can name two holdings — the same stock at two
    brokers is two positions the ledger keeps apart (§6.7). That case is reported as ambiguous
    from *both* sides rather than resolved: a CAS belongs to one demat account, and a caller who
    knows which one can disambiguate simply by passing only that account's holdings.

    Unmatched in either direction is expected, not exceptional. A CDSL statement is silent about
    NSDL holdings, a statement's period starts somewhere, and a position sold before the broker
    was connected exists in the statement and nowhere else. All three are shown to the user; none
    of them is dropped, because a dropped position is a purchase history the user believes was
    imported.
    """
    by_instrument: dict[int, list[Holding]] = {}
    for holding in holdings:
        by_instrument.setdefault(holding.key.instrument_id, []).append(holding)

    matched: list[CasMatch] = []
    unmatched_positions: list[UnmatchedPosition] = []
    paired: set[HoldingKey] = set()
    ambiguous: set[int] = set()

    for position in positions:
        instrument_id = instrument_ids_by_isin.get(position.isin)
        if instrument_id is None:
            unmatched_positions.append(
                UnmatchedPosition(position, UnmatchedReason.NO_INSTRUMENT_FOR_ISIN)
            )
            continue
        candidates = by_instrument.get(instrument_id, [])
        if not candidates:
            unmatched_positions.append(UnmatchedPosition(position, UnmatchedReason.NOT_HELD))
            continue
        if len(candidates) > 1:
            ambiguous.add(instrument_id)
            unmatched_positions.append(
                UnmatchedPosition(position, UnmatchedReason.AMBIGUOUS_BROKER_ACCOUNT)
            )
            continue
        matched.append(CasMatch(position=position, holding=candidates[0]))
        paired.add(candidates[0].key)

    unmatched_holdings = tuple(
        UnmatchedHolding(
            key=holding.key,
            reason=(
                UnmatchedReason.AMBIGUOUS_BROKER_ACCOUNT
                if holding.key.instrument_id in ambiguous
                else UnmatchedReason.NOT_IN_STATEMENT
            ),
        )
        for holding in holdings
        if holding.key not in paired
    )
    return CasReconciliation(
        matched=tuple(matched),
        unmatched_in_statement=tuple(unmatched_positions),
        unmatched_in_portfolio=unmatched_holdings,
    )


def backfilled_holding(match: CasMatch) -> Holding:
    """The holding with its average buy price filled in from the statement. Raises if unsafe.

    The one function in this module that produces something the rest of the product will treat as
    truth, so it is the one that refuses hardest. It raises rather than returning the holding
    unchanged: an unchanged holding is what a caller writing a loop would happily store, and the
    resulting portfolio would claim an import that did not happen for the positions where it
    mattered most.

    The quantity is not touched. A statement is a historical document and the broker is the
    authority on what is held now (§3) — this fills in what was paid, never what is there.
    """
    if not match.backfillable:
        raise ValueError(
            f"the statement cannot establish a buy price for {match.position.isin}: "
            f"bought {match.position.bought_quantity}, "
            f"{match.position.unpriced_inflow_quantity} share(s) arrived with no price, and the "
            f"statement's net {match.position.net_quantity} differs from the "
            f"{match.holding.quantity} actually held by {match.quantity_delta}; backfilling "
            "anyway would put a wrong cost basis under every return figure this holding appears in"
        )
    return replace(match.holding, avg_price=match.position.weighted_average_buy_price)
