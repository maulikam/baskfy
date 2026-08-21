"""Reading a user's portfolio CSV — docs/01 §8, PROMPTS.md Prompt 14 §1.

    "Upload a portfolio CSV of symbols (sample CSV provided)" — docs/01 §8

    "CSV import that returns a parse report listing matched, ambiguous and unmatched symbols
     rather than silently dropping rows. Provide a downloadable sample CSV, as the reference
     product does." — PROMPTS.md Prompt 14 §1

    "Show unmatched symbols prominently rather than silently dropping." — docs/08 §"Rebalance
    tracker"

This module is the **parser**, and only the parser: bytes in, normalised rows and a list of what
it could not use out. Deciding whether ``INFY`` is an instrument we hold data for needs the
database, so it happens in ``baskfy_api.portfolios``; the vocabulary that report is written in
(:class:`MatchStatus`, :class:`UnmatchedReason`) lives here so the parser, the resolver and the
web app all name the same things.

What "messy" means here
-----------------------
Prompt 14's second acceptance criterion names the inputs this has to survive: "extra columns,
whitespace, lowercase symbols, a BSE-style code, and a blank row". None of those is an error the
user should have to fix by hand:

* **Extra columns** are ignored — a Zerodha or ICICI holdings export has fifteen of them.
* **Whitespace** is stripped, from the header names as well as the values.
* **Lowercase** symbols are upper-cased. NSE symbols are upper-case by definition.
* **A BSE-style code** (``532540``) is recognised for what it is and reported as unmatched *with
  that reason*, because this service holds NSE instruments and has no BSE-code mapping to resolve
  it with. Guessing would be worse than saying so — see ``docs/DECISIONS.md`` §14.
* **A blank row** is skipped and counted, not silently dropped.

Every line of the file lands in exactly one of :attr:`ParsedCsv.rows` or
:attr:`ParsedCsv.skipped`, so the counts in the report always add back up to the file.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

__all__ = [
    "MAX_IMPORT_ROWS",
    "SAMPLE_CSV",
    "SAMPLE_CSV_FILENAME",
    "CsvParseError",
    "MatchStatus",
    "ParsedCsv",
    "ParsedHolding",
    "RowIssue",
    "SkipReason",
    "SkippedRow",
    "SymbolShape",
    "UnmatchedReason",
    "classify_symbol",
    "normalise_symbol",
    "parse_portfolio_csv",
]

#: A portfolio is a portfolio, not a universe. The cap exists so an accidental upload of the
#: 2,000-row bhavcopy is refused with a sentence rather than absorbed into a user's account.
MAX_IMPORT_ROWS: Final = 500

#: docs/01 §8: "(sample CSV provided)". Real NSE symbols from the reference export, so a user who
#: downloads this, uploads it unedited and runs a rebalance sees the feature work rather than five
#: unmatched rows. The header names are the ones docs/07's ``POST /portfolios`` body uses —
#: ``{ symbol, quantity?, avg_price? }`` — so the file and the API agree on their spelling.
SAMPLE_CSV: Final = (
    "symbol,quantity,avg_price\n"
    "CUPID,100,284.56\n"
    "HFCL,250,89.10\n"
    "WELCORP,40,912.40\n"
    "DIACABS,60,1130.00\n"
    "SANSERA,75,1420.25\n"
)

SAMPLE_CSV_FILENAME: Final = "baskfy-portfolio-sample.csv"

#: Accepted spellings of the one column that is required. Anything a broker's export is likely to
#: call it; the match is case-insensitive and ignores spaces and underscores.
SYMBOL_HEADERS: Final[frozenset[str]] = frozenset(
    {"symbol", "symbols", "ticker", "tradingsymbol", "scrip", "scripcode", "instrument", "stock"}
)
QUANTITY_HEADERS: Final[frozenset[str]] = frozenset(
    {"quantity", "qty", "shares", "units", "holdingqty"}
)
AVG_PRICE_HEADERS: Final[frozenset[str]] = frozenset(
    {"avgprice", "averageprice", "avgcost", "averagecost", "buyprice", "price", "avgtradedprice"}
)

#: What an NSE trading symbol may contain. ``&`` (M&M), ``-`` (BAJAJ-AUTO) and ``.`` are all real.
_SYMBOL_RE: Final = re.compile(r"^[A-Z0-9][A-Z0-9&.\-]{0,29}$")

#: A BSE scrip code: six digits, occasionally five for the older listings.
_BSE_CODE_RE: Final = re.compile(r"^\d{5,6}$")

#: Yahoo-style suffixes that ride along in exported files. ``.NS`` is this exchange and is simply
#: dropped; ``.BO`` is the other one and is *kept*, so the symbol is reported as unmatched rather
#: than quietly resolved to the NSE listing of a code the user took from BSE.
_NSE_SUFFIX: Final = ".NS"

_THOUSANDS: Final = re.compile(r"[,\s₹]")


class CsvParseError(ValueError):
    """The upload is not a portfolio CSV at all — empty, or with no symbol column."""


class SkipReason(StrEnum):
    """Why a line of the file produced no holding."""

    BLANK = "blank"
    #: The symbol cell was empty even though the row had content.
    NO_SYMBOL = "no_symbol"
    #: The same symbol appeared earlier in the file; the first occurrence won.
    DUPLICATE = "duplicate"
    #: The file is longer than :data:`MAX_IMPORT_ROWS`.
    TRUNCATED = "truncated"


class RowIssue(StrEnum):
    """Something the parser corrected or could not read, on a row it *did* keep."""

    #: ``quantity`` was present but not a number.
    UNREADABLE_QUANTITY = "unreadable_quantity"
    #: ``avg_price`` was present but not a number.
    UNREADABLE_AVG_PRICE = "unreadable_avg_price"
    #: A trailing ``.NS`` was stripped.
    SUFFIX_STRIPPED = "suffix_stripped"


class SymbolShape(StrEnum):
    """What a normalised token looks like, before any database is consulted."""

    SYMBOL = "symbol"
    BSE_CODE = "bse_code"
    INVALID = "invalid"


class MatchStatus(StrEnum):
    """The three buckets Prompt 14 §1 requires the parse report to list."""

    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    UNMATCHED = "unmatched"


class UnmatchedReason(StrEnum):
    """Why a parsed symbol did not become a holding."""

    #: No ``instrument`` and no ``symbol_alias`` row carries it.
    UNKNOWN_SYMBOL = "unknown_symbol"
    #: A BSE scrip code. We hold NSE instruments; there is no mapping to resolve it with.
    BSE_CODE = "bse_code"
    #: Not a plausible trading symbol at all (punctuation, a sentence, a total row).
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ParsedHolding:
    """One usable line of the file.

    ``line_number`` is the 1-based line in the uploaded file, header included, so the report can
    point at something the user can actually find in their spreadsheet.
    """

    line_number: int
    raw_symbol: str
    symbol: str
    shape: SymbolShape
    quantity: Decimal | None = None
    avg_price: Decimal | None = None
    issues: tuple[RowIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class SkippedRow:
    """A line that produced no holding, and why."""

    line_number: int
    reason: SkipReason
    raw: str = ""


@dataclass(frozen=True, slots=True)
class ParsedCsv:
    """The whole file, accounted for."""

    rows: tuple[ParsedHolding, ...]
    skipped: tuple[SkippedRow, ...]
    #: The header row as the file spelled it, or ``()`` for a headerless list of symbols.
    header: tuple[str, ...] = ()
    #: Header columns the parser ignored — surfaced so "extra columns" is visibly a decision.
    ignored_columns: tuple[str, ...] = field(default=())

    @property
    def total_lines(self) -> int:
        return len(self.rows) + len(self.skipped)


def normalise_symbol(raw: str) -> tuple[str, tuple[RowIssue, ...]]:
    """Trim, upper-case, and drop a trailing ``.NS``. Returns the token and what was corrected."""
    token = raw.strip().strip('"').strip("'").strip().upper()
    issues: list[RowIssue] = []
    if token.endswith(_NSE_SUFFIX) and len(token) > len(_NSE_SUFFIX):
        token = token[: -len(_NSE_SUFFIX)]
        issues.append(RowIssue.SUFFIX_STRIPPED)
    return token, tuple(issues)


def classify_symbol(token: str) -> SymbolShape:
    """What the token is, before any lookup. A BSE code is a code, not an unknown symbol."""
    if _BSE_CODE_RE.match(token):
        return SymbolShape.BSE_CODE
    if _SYMBOL_RE.match(token):
        return SymbolShape.SYMBOL
    return SymbolShape.INVALID


def parse_portfolio_csv(text: str, *, max_rows: int = MAX_IMPORT_ROWS) -> ParsedCsv:
    """Parse an uploaded portfolio CSV. Never raises on a bad *row* — only on a bad *file*."""
    body = text.lstrip("﻿")
    if not body.strip():
        raise CsvParseError("The file is empty.")

    lines = list(csv.reader(io.StringIO(body)))
    header, start = _read_header(lines)
    mapping = _column_indices(header)
    if header and mapping.get("symbol") is None:
        raise CsvParseError(
            "No symbol column. Expected a header naming one of: "
            + ", ".join(sorted(SYMBOL_HEADERS))
            + "."
        )

    symbol_at = mapping.get("symbol", 0) or 0
    rows: list[ParsedHolding] = []
    skipped: list[SkippedRow] = []
    seen: dict[str, int] = {}

    for offset, cells in enumerate(lines[start:], start=start):
        line_number = offset + 1
        if not any(cell.strip() for cell in cells):
            skipped.append(SkippedRow(line_number=line_number, reason=SkipReason.BLANK))
            continue

        raw_symbol = cells[symbol_at] if symbol_at < len(cells) else ""
        token, issues = normalise_symbol(raw_symbol)
        if not token:
            skipped.append(
                SkippedRow(
                    line_number=line_number,
                    reason=SkipReason.NO_SYMBOL,
                    raw=",".join(cell.strip() for cell in cells)[:120],
                )
            )
            continue
        if token in seen:
            skipped.append(
                SkippedRow(line_number=line_number, reason=SkipReason.DUPLICATE, raw=token)
            )
            continue
        if len(rows) >= max_rows:
            skipped.append(
                SkippedRow(line_number=line_number, reason=SkipReason.TRUNCATED, raw=token)
            )
            continue

        quantity, quantity_issue = _number(cells, mapping.get("quantity"))
        avg_price, price_issue = _number(cells, mapping.get("avg_price"))
        row_issues = [*issues]
        if quantity_issue:
            row_issues.append(RowIssue.UNREADABLE_QUANTITY)
        if price_issue:
            row_issues.append(RowIssue.UNREADABLE_AVG_PRICE)

        seen[token] = line_number
        rows.append(
            ParsedHolding(
                line_number=line_number,
                raw_symbol=raw_symbol.strip(),
                symbol=token,
                shape=classify_symbol(token),
                quantity=quantity,
                avg_price=avg_price,
                issues=tuple(row_issues),
            )
        )

    return ParsedCsv(
        rows=tuple(rows),
        skipped=tuple(skipped),
        header=tuple(cell.strip() for cell in header),
        ignored_columns=_ignored(header, mapping),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _canonical(name: str) -> str:
    """A header cell reduced to letters and digits, so ``Avg. Price`` matches ``avg_price``."""
    return re.sub(r"[^a-z0-9]", "", name.strip().lower())


#: Every header name the parser recognises. A first row containing any of them *is* a header,
#: even if none of them is the symbol column — which is how a file headed
#: ``quantity,avg_price`` is refused rather than read as two symbols called QUANTITY and
#: AVG_PRICE.
KNOWN_HEADERS: Final[frozenset[str]] = SYMBOL_HEADERS | QUANTITY_HEADERS | AVG_PRICE_HEADERS


def _read_header(lines: Sequence[Sequence[str]]) -> tuple[list[str], int]:
    """The header row and the index of the first data row.

    A file whose first line names no column we recognise at all is treated as a **headerless list
    of symbols** — which is exactly what docs/01 §8 describes users uploading ("a portfolio CSV of
    symbols"). Requiring a header would reject the most common real input.
    """
    if not lines:
        return [], 0
    first = list(lines[0])
    if any(_canonical(cell) in KNOWN_HEADERS for cell in first):
        return first, 1
    return [], 0


def _column_indices(header: Sequence[str]) -> Mapping[str, int | None]:
    """Which column holds what. ``symbol`` is 0 for a headerless file."""
    if not header:
        return {"symbol": 0, "quantity": None, "avg_price": None}
    found: dict[str, int | None] = {"symbol": None, "quantity": None, "avg_price": None}
    for index, cell in enumerate(header):
        canonical = _canonical(cell)
        if found["symbol"] is None and canonical in SYMBOL_HEADERS:
            found["symbol"] = index
        elif found["quantity"] is None and canonical in QUANTITY_HEADERS:
            found["quantity"] = index
        elif found["avg_price"] is None and canonical in AVG_PRICE_HEADERS:
            found["avg_price"] = index
    return found


def _ignored(header: Sequence[str], mapping: Mapping[str, int | None]) -> tuple[str, ...]:
    used = {index for index in mapping.values() if index is not None}
    return tuple(
        cell.strip() for index, cell in enumerate(header) if index not in used and cell.strip()
    )


def _number(cells: Sequence[str], index: int | None) -> tuple[Decimal | None, bool]:
    """A numeric cell as ``Decimal``. Returns ``(value, unreadable)``.

    ``Decimal``, never ``float`` — CLAUDE.md house rule 9, and ``portfolio_holding.quantity`` is
    ``numeric(20,4)``. Indian thousands separators and a stray ``₹`` are stripped, because they
    are what a spreadsheet produces, not what a user typed wrong.
    """
    if index is None or index >= len(cells):
        return None, False
    raw = cells[index].strip()
    if not raw:
        return None, False
    cleaned = _THOUSANDS.sub("", raw)
    try:
        return Decimal(cleaned), False
    except (InvalidOperation, ValueError):
        return None, True


def symbols_of(rows: Iterable[ParsedHolding]) -> tuple[str, ...]:
    """The distinct symbols to resolve, in file order."""
    return tuple(row.symbol for row in rows)
