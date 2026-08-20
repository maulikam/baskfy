"""Streaming CSV export — Prompt 9 deliverable 6, and docs/13 §5 step 6.

    "The CSV must match the reference export byte-for-byte in shape: the 93 columns in the order
     listed in docs/13 §1, UTF-8 **with BOM**, only the `name` field quoted."

    docs/13 §5 step 6: "Assert our CSV export reproduces this file's exact column names, order,
    quoting and BOM."

**This replaces what Prompt 7 built.** Prompt 7 deliverable 6 said "with the screen's column set",
and that is what it shipped. docs/13 settles it the other way: the reference product's export
carries all 93 columns regardless of which the user has chosen to see, and the committed fixture
is the artefact both prompts have to agree with. Written up in ``docs/09a`` §1.

Byte-level details, all taken from the committed file rather than from the prose:

* **BOM.** ``EF BB BF``, then an unquoted header row.
* **Quoting.** Only ``name``, and *always* — not "when it contains a comma". ``csv.writer`` with
  ``QUOTE_MINIMAL`` would leave ``CUPID LIMITED`` bare and ``QUOTE_ALL`` would quote everything,
  so the rows are assembled directly.
* **Line endings.** ``\\n``. The file has no carriage returns, despite being an Excel-facing export.
* **Precision.** Whatever the column is stored at — ``273.00``, ``0.5793179400``, ``38192``. The
  values are already rounded (CLAUDE.md house rule 8: "Round at write time. Storage precision is
  the contract"), so nothing here re-rounds and nothing goes through ``float``.

Streamed means streamed: rows are read with SQLAlchemy's async streaming cursor and yielded in
chunks, so peak memory is a chunk rather than the whole file (docs/11 budgets 2 s for 4,000 rows).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Final, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from decile_core.reference_export import EXPORT_COLUMNS, FACTOR_COLUMN_MAP
from decile_core.screen_definition import ScreenDefinition
from decile_core.screener import MAX_RESULT_ROWS, build_export_query
from decile_core.universes import UNIVERSES

#: docs/13 §1: "the file is **UTF-8 with BOM** (Excel-friendly)".
BOM: Final = "﻿"

#: The one column the reference file quotes, on every row.
QUOTED_COLUMN: Final = "name"

LINE_ENDING: Final = "\n"

#: Rows per yielded chunk. Small enough to keep memory flat, large enough that a 4,000-row export
#: is not 4,000 awaits.
CHUNK_ROWS: Final = 250

#: Export column -> the mask column and bit that decide its 0/1 value.
#:
#: ``Universe.csv_flag`` is the export's own column name for a universe (docs/13 §1), and the
#: three suffixes are the three groups of fourteen. Derived rather than listed, so a universe
#: added to ``decile_core.universes`` cannot end up with a flag column nobody fills in.
_FLAG_SOURCES: Final[dict[str, tuple[str, int]]] = {
    f"{universe.csv_flag}{suffix}": (mask_column, universe.mask_value)
    for universe in UNIVERSES
    for suffix, mask_column in (
        ("", "universe_mask"),
        ("_top_beta", "top_beta_mask"),
        ("_top_volatility", "top_volatility_mask"),
    )
}

#: Export column -> the query column it reads, for everything that is not a flag or the date.
_DIRECT_SOURCES: Final[dict[str, str]] = {
    **FACTOR_COLUMN_MAP,
    "name": "name",
    "symbol": "symbol",
    "open": "open",
    "high": "high",
    "low": "low",
    "volume_shares": "volume_shares",
}


def filename_for(screen_name: str, as_of: dt.date) -> str:
    """``Investing 001`` + 2026-08-18 -> ``investing-001-2026-08-18.csv``."""
    slug = "".join(c.lower() if c.isalnum() else "-" for c in screen_name).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return f"{slug or 'screen'}-{as_of.isoformat()}.csv"


def format_cell(value: object) -> str:
    """One cell as text, at the precision it is stored at.

    ``Decimal`` is written with ``format(..., "f")`` and never through ``float``: CLAUDE.md house
    rule 8 makes storage precision the contract, "so the API, the UI and the CSV export can never
    disagree", and ``float(Decimal("273.00"))`` would write ``273.0``.
    """
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def quote(value: str) -> str:
    """Excel-style quoting: wrap, and double any embedded quote."""
    return '"' + value.replace('"', '""') + '"'


class RowLookup(Protocol):
    """Anything that can hand back a column by name — a ``RowMapping`` in practice.

    Stated structurally because SQLAlchemy's ``RowMapping`` is not a ``Mapping[str, object]``, and
    naming the concrete type here would make ``export_row`` untestable without a database.
    """

    def get(self, key: str, /) -> object: ...


def export_row(row: RowLookup, as_of: dt.date) -> str:
    """One data line of the reference export, in docs/13 §1's column order."""
    cells: list[str] = []
    for column in EXPORT_COLUMNS:
        if column == "date":
            cells.append(as_of.isoformat())
            continue
        flag = _FLAG_SOURCES.get(column)
        if flag is not None:
            mask_column, bit = flag
            mask = row.get(mask_column)
            cells.append("1" if isinstance(mask, int) and mask & bit else "0")
            continue
        source = _DIRECT_SOURCES.get(column)
        text = format_cell(row.get(source)) if source is not None else ""
        cells.append(quote(text) if column == QUOTED_COLUMN else text)
    return ",".join(cells)


def header_line() -> str:
    """The 93 column names, unquoted, exactly as the committed file writes them."""
    return ",".join(EXPORT_COLUMNS)


async def stream_screen_csv(
    session: AsyncSession,
    definition: ScreenDefinition,
    *,
    as_of: dt.date,
    limit: int = MAX_RESULT_ROWS,
) -> AsyncGenerator[bytes, None]:
    """Yield the export a chunk at a time, BOM and header first."""
    statement = build_export_query(definition, as_of, limit=limit)

    yield (BOM + header_line() + LINE_ENDING).encode("utf-8")

    pending: list[str] = []
    result = await session.stream(statement)
    async for row in result:
        pending.append(export_row(row._mapping, as_of))
        if len(pending) >= CHUNK_ROWS:
            yield (LINE_ENDING.join(pending) + LINE_ENDING).encode("utf-8")
            pending = []
    if pending:
        yield (LINE_ENDING.join(pending) + LINE_ENDING).encode("utf-8")
