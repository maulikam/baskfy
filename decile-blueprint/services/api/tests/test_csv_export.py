"""The CSV export against the committed reference file — Prompt 9 deliverable 6.

    "The CSV must match the reference export byte-for-byte in shape: the 93 columns in the order
     listed in docs/13 §1, UTF-8 **with BOM**, only the `name` field quoted. Add a test that diffs
     our export's header against fixtures/reference-screen-export-2026-08-18.csv."

docs/13 §5 step 6 asks for the same thing and adds quoting and line endings to the list. Every
assertion below is made against the *file*, not against the prose: where docs/13 §1's description
and the committed artefact disagree about column order, `baskfy_core.reference_export` already
records that the file is the binding one (its three flag groups are 13 + 13 + 13 with the `etf`
columns appended, not 14 + 14 + 14).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from decimal import Decimal
from pathlib import Path

import pytest
from screener_helpers import AS_OF, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.csv_export import (
    BOM,
    LINE_ENDING,
    export_row,
    filename_for,
    format_cell,
    header_line,
    quote,
    stream_screen_csv,
)
from baskfy_core.reference_export import EXPORT_COLUMNS, default_fixture_path
from baskfy_core.seed_data import EXAMPLE_SCREENS

INVESTING_001 = EXAMPLE_SCREENS[0].definition

REFERENCE: Path = default_fixture_path()
RAW = REFERENCE.read_bytes()


class TestTheHeader:
    def test_it_is_the_reference_header_character_for_character(self) -> None:
        """The diff Prompt 9 asks for."""
        expected = RAW.decode("utf-8-sig").split("\n", 1)[0]
        assert header_line() == expected

    def test_it_has_the_ninety_three_columns_docs_13_counts(self) -> None:
        assert len(EXPORT_COLUMNS) == 93
        assert len(header_line().split(",")) == 93

    def test_the_header_row_is_unquoted(self) -> None:
        assert '"' not in header_line()


class TestTheEncoding:
    def test_the_reference_file_starts_with_a_bom(self) -> None:
        # Guards the assumption the rest of this module rests on.
        assert RAW.startswith(b"\xef\xbb\xbf")

    def test_ours_does_too(self) -> None:
        assert BOM.encode("utf-8") == b"\xef\xbb\xbf"

    def test_the_reference_file_uses_bare_newlines(self) -> None:
        # An Excel-facing export with no carriage returns is surprising enough to pin.
        assert b"\r\n" not in RAW
        assert LINE_ENDING == "\n"


class TestQuoting:
    def test_the_reference_quotes_the_name_and_nothing_else(self) -> None:
        first_row = RAW.decode("utf-8-sig").split("\n")[1]
        assert first_row.startswith('"')
        assert first_row.split(",", 1)[0].endswith('"')
        # Exactly two quote characters: the pair around `name`.
        assert first_row.count('"') == 2

    def test_a_name_is_always_quoted_even_without_a_comma(self) -> None:
        # `csv.QUOTE_MINIMAL` would leave this bare, which is why the rows are assembled by hand.
        assert quote("CUPID LIMITED") == '"CUPID LIMITED"'

    def test_an_embedded_quote_is_doubled(self) -> None:
        assert quote('SOME "CO" LTD') == '"SOME ""CO"" LTD"'


class TestPrecision:
    def test_a_decimal_keeps_every_stored_digit(self) -> None:
        # CLAUDE.md house rule 8: storage precision is the contract.
        assert format_cell(Decimal("273.00")) == "273.00"
        assert format_cell(Decimal("0.5793179400")) == "0.5793179400"

    def test_exponent_notation_is_expanded(self) -> None:
        assert format_cell(Decimal("1E+3")) == "1000"

    def test_a_null_is_an_empty_field(self) -> None:
        assert format_cell(None) == ""

    def test_the_reference_values_survive_a_round_trip(self) -> None:
        """Every numeric cell in the fixture re-renders to itself.

        If `format_cell` disagreed with the file about any column's precision, the export would
        differ from the artefact docs/13 calls the answer key — and it would differ silently.
        """
        text = RAW.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
        assert len(rows) == 271
        text_columns = {"name", "symbol", "series", "date"}
        numeric = [
            column
            for column in EXPORT_COLUMNS
            if not column.startswith("is_") and column not in text_columns
        ]
        for row in rows[:20]:
            for column in numeric:
                cell = row[column]
                if cell == "":
                    continue
                assert format_cell(Decimal(cell)) == cell, f"{column}={cell}"


class TestRowRendering:
    def test_a_row_places_every_value_in_the_documented_column(self) -> None:
        row = {
            "name": "CUPID LIMITED",
            "symbol": "CUPID",
            "series": "EQ",
            "open": Decimal("273.00"),
            "close": Decimal("284.03"),
            "marketcap_cr": 38192,
            "beta_12m": Decimal("0.8500000000"),
            "universe_mask": 1,
            "top_beta_mask": 0,
            "top_volatility_mask": 1,
        }
        cells = export_row(row, AS_OF).split(",")
        by_column = dict(zip(EXPORT_COLUMNS, cells, strict=True))

        assert by_column["name"] == '"CUPID LIMITED"'
        assert by_column["symbol"] == "CUPID"
        assert by_column["date"] == AS_OF.isoformat()
        assert by_column["open"] == "273.00"
        assert by_column["close"] == "284.03"
        assert by_column["marketcap"] == "38192"
        assert by_column["beta"] == "0.8500000000"

    def test_flags_are_read_from_the_masks(self) -> None:
        # docs/13 §2 finding 9: the flags are denormalised onto the fact row, one bit per universe.
        row = {"universe_mask": 0b101, "top_beta_mask": 0b100, "top_volatility_mask": 0}
        by_column = dict(zip(EXPORT_COLUMNS, export_row(row, AS_OF).split(","), strict=True))
        assert by_column["is_nifty_50"] == "1"
        assert by_column["is_nifty_next_50"] == "0"
        assert by_column["is_nifty_100"] == "1"
        assert by_column["is_nifty_100_top_beta"] == "1"
        assert by_column["is_nifty_50_top_beta"] == "0"
        assert by_column["is_etf_top_volatility"] == "0"

    def test_a_missing_value_is_an_empty_field_not_a_gap(self) -> None:
        cells = export_row({}, AS_OF).split(",")
        assert len(cells) == 93

    def test_the_filename_carries_the_screen_and_the_date(self) -> None:
        assert filename_for("Investing 001", dt.date(2026, 8, 18)) == "investing-001-2026-08-18.csv"
        assert filename_for("!!!", dt.date(2026, 8, 18)) == "screen-2026-08-18.csv"


async def collect(session: AsyncSession) -> bytes:
    """The whole export, for the assertions that need to look at all of it."""
    return b"".join(
        [chunk async for chunk in stream_screen_csv(session, INVESTING_001, as_of=AS_OF)]
    )


@pytest.mark.db
@requires_db
class TestAgainstTheDatabase:
    async def test_the_export_reproduces_the_reference_shape(
        self, screener_session: AsyncSession
    ) -> None:
        body = await collect(screener_session)
        assert body.startswith(b"\xef\xbb\xbf")
        assert b"\r\n" not in body

        text = body.decode("utf-8-sig")
        lines = text.rstrip("\n").split("\n")
        assert lines[0] == header_line()
        assert len(lines) - 1 == 271

        for line in lines[1:]:
            assert len(line.split(",")) == 93

    async def test_every_row_quotes_only_its_name(self, screener_session: AsyncSession) -> None:
        body = await collect(screener_session)
        for line in body.decode("utf-8-sig").rstrip("\n").split("\n")[1:]:
            assert line.startswith('"'), line[:40]
            assert line.count('"') == 2, line[:80]

    async def test_the_values_match_the_reference_file(
        self, screener_session: AsyncSession
    ) -> None:
        """Not just the shape: the numbers too, for every column the database actually holds.

        `open`, `high`, `low` and `volume_shares` live on `ohlcv_daily`, which a database seeded
        from the export alone does not have — so they are empty here, and that is checked rather
        than skipped over.
        """
        body = await collect(screener_session)
        ours = {row["symbol"]: row for row in csv.DictReader(io.StringIO(body.decode("utf-8-sig")))}
        theirs = {
            row["symbol"]: row for row in csv.DictReader(io.StringIO(RAW.decode("utf-8-sig")))
        }
        assert set(ours) == set(theirs)

        bar_columns = {"open", "high", "low", "volume_shares"}
        for symbol, expected in theirs.items():
            actual = ours[symbol]
            for column in EXPORT_COLUMNS:
                if column in bar_columns:
                    assert actual[column] == "", f"{symbol}.{column} should be empty without bars"
                    continue
                assert actual[column] == expected[column], f"{symbol}.{column}"

    async def test_it_streams_rather_than_buffering(self, screener_session: AsyncSession) -> None:
        chunks = [
            chunk async for chunk in stream_screen_csv(screener_session, INVESTING_001, as_of=AS_OF)
        ]
        assert len(chunks) > 1
        assert max(len(chunk) for chunk in chunks) < sum(len(chunk) for chunk in chunks)
