"""Parsing a messy portfolio upload — PROMPTS.md Prompt 14's second acceptance criterion.

    "CSV import test with messy input: extra columns, whitespace, lowercase symbols, a BSE-style
     code, and a blank row."

The end-to-end version of that criterion is ``services/api/tests/test_api_portfolios.py``, which
puts the same file through ``POST /portfolios/import-csv`` and checks the report. This module
asserts the parser alone, where the interesting cases are cheap to enumerate.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from baskfy_core.portfolio_csv import (
    SAMPLE_CSV,
    CsvParseError,
    RowIssue,
    SkipReason,
    SymbolShape,
    classify_symbol,
    normalise_symbol,
    parse_portfolio_csv,
)

#: The acceptance criterion's file, with one instance of each named problem.
MESSY = (
    "Symbol , Quantity ,Avg Price,Broker Note,ISIN\r\n"
    "  cupid , 100 , 284.56 ,bought on dip,INE ...\r\n"
    "\r\n"
    "hfcl,250,89.10,,\r\n"
    "532540,10,1000,a BSE scrip code,\r\n"
    "WELCORP , 40 , 912.40 , ,\r\n"
)


def parsed_symbols(text: str) -> list[str]:
    return [row.symbol for row in parse_portfolio_csv(text).rows]


class TestTheMessyFile:
    def test_lowercase_symbols_are_upper_cased(self) -> None:
        assert "CUPID" in parsed_symbols(MESSY)

    def test_whitespace_is_stripped_from_values_and_headers(self) -> None:
        row = next(row for row in parse_portfolio_csv(MESSY).rows if row.symbol == "CUPID")
        assert row.raw_symbol == "cupid"
        assert row.quantity == Decimal("100")
        assert row.avg_price == Decimal("284.56")

    def test_extra_columns_are_ignored_and_named(self) -> None:
        parsed = parse_portfolio_csv(MESSY)
        assert parsed.ignored_columns == ("Broker Note", "ISIN")

    def test_a_blank_row_is_skipped_and_counted_not_dropped(self) -> None:
        parsed = parse_portfolio_csv(MESSY)
        blanks = [row for row in parsed.skipped if row.reason is SkipReason.BLANK]
        assert len(blanks) == 1
        assert blanks[0].line_number == 3

    def test_a_bse_style_code_is_classified_rather_than_guessed_at(self) -> None:
        row = next(row for row in parse_portfolio_csv(MESSY).rows if row.symbol == "532540")
        assert row.shape is SymbolShape.BSE_CODE

    def test_every_line_is_accounted_for(self) -> None:
        parsed = parse_portfolio_csv(MESSY)
        assert parsed.total_lines == len(MESSY.splitlines()) - 1  # minus the header
        assert len(parsed.rows) == 4
        assert len(parsed.skipped) == 1


class TestHeaders:
    def test_a_headerless_list_of_symbols_parses(self) -> None:
        """docs/01 §8: users upload "a portfolio CSV of symbols"."""
        assert parsed_symbols("CUPID\nHFCL\nWELCORP\n") == ["CUPID", "HFCL", "WELCORP"]

    @pytest.mark.parametrize("header", ["symbol", "Ticker", "TRADINGSYMBOL", "Scrip"])
    def test_the_symbol_column_is_found_under_any_common_name(self, header: str) -> None:
        assert parsed_symbols(f"{header},qty\nCUPID,10\n") == ["CUPID"]

    def test_a_file_with_a_header_but_no_symbol_column_is_refused(self) -> None:
        with pytest.raises(CsvParseError, match="symbol column"):
            parse_portfolio_csv("quantity,avg_price\n10,20\n")

    def test_an_empty_file_is_refused(self) -> None:
        with pytest.raises(CsvParseError, match="empty"):
            parse_portfolio_csv("   \n\n")

    def test_a_utf8_bom_does_not_become_part_of_the_first_header(self) -> None:
        assert parsed_symbols("﻿symbol\nCUPID\n") == ["CUPID"]


class TestNormalisation:
    def test_a_trailing_nse_suffix_is_stripped_and_reported(self) -> None:
        row = parse_portfolio_csv("symbol\ncupid.ns\n").rows[0]
        assert row.symbol == "CUPID"
        assert RowIssue.SUFFIX_STRIPPED in row.issues

    @pytest.mark.parametrize("symbol", ["M&M", "BAJAJ-AUTO", "NIFTYBEES"])
    def test_real_nse_symbol_shapes_are_symbols(self, symbol: str) -> None:
        assert classify_symbol(symbol) is SymbolShape.SYMBOL

    @pytest.mark.parametrize("token", ["TOTAL VALUE", "—", "#N/A!"])
    def test_a_token_that_is_not_a_symbol_is_invalid(self, token: str) -> None:
        assert classify_symbol(normalise_symbol(token)[0]) is SymbolShape.INVALID

    def test_indian_thousands_separators_and_rupee_signs_parse(self) -> None:
        row = parse_portfolio_csv('symbol,qty,price\nCUPID,"1,000",₹1234.50\n').rows[0]
        assert row.quantity == Decimal("1000")
        assert row.avg_price == Decimal("1234.50")

    def test_an_unreadable_quantity_keeps_the_row_and_reports_it(self) -> None:
        """Dropping the row would lose the symbol, which is the part that matters."""
        row = parse_portfolio_csv("symbol,qty\nCUPID,lots\n").rows[0]
        assert row.symbol == "CUPID"
        assert row.quantity is None
        assert RowIssue.UNREADABLE_QUANTITY in row.issues

    def test_a_duplicate_symbol_keeps_the_first_and_reports_the_second(self) -> None:
        parsed = parse_portfolio_csv("symbol,qty\nCUPID,10\ncupid,20\n")
        assert [row.quantity for row in parsed.rows] == [Decimal("10")]
        assert parsed.skipped[0].reason is SkipReason.DUPLICATE

    def test_a_row_whose_symbol_cell_is_empty_is_skipped_with_its_content(self) -> None:
        parsed = parse_portfolio_csv("symbol,qty\n,10\n")
        assert parsed.rows == ()
        assert parsed.skipped[0].reason is SkipReason.NO_SYMBOL
        assert parsed.skipped[0].raw == ",10"

    def test_the_row_cap_truncates_visibly(self) -> None:
        text = "symbol\n" + "".join(f"SYM{index}\n" for index in range(10))
        parsed = parse_portfolio_csv(text, max_rows=4)
        assert len(parsed.rows) == 4
        assert {row.reason for row in parsed.skipped} == {SkipReason.TRUNCATED}


class TestTheSampleFile:
    def test_it_parses_cleanly_through_our_own_parser(self) -> None:
        parsed = parse_portfolio_csv(SAMPLE_CSV)
        assert len(parsed.rows) == 5
        assert parsed.skipped == ()
        assert all(row.shape is SymbolShape.SYMBOL for row in parsed.rows)
        assert all(row.issues == () for row in parsed.rows)

    def test_its_header_names_are_the_ones_the_api_body_uses(self) -> None:
        """docs/07: `POST /portfolios { holdings:[{symbol, quantity?, avg_price?}] }`."""
        assert SAMPLE_CSV.splitlines()[0] == "symbol,quantity,avg_price"
