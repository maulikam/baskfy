"""FixtureProvider and the fixture data (Prompt 2 deliverable 5).

    "FixtureProvider reading Parquet fixtures from tests/fixtures/, used by all tests and by
     `make seed` for local development. Include real-shaped fixtures for ~40 instruments over
     3 years, including at least one instrument with a split and one with a bonus."

docs/09's table adds the constraint that makes it useful: "reads Parquet fixtures, zero network".
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from baskfy_core.reference_export import to_rows
from baskfy_providers.errors import ProviderUnavailable, UnexpectedPayload
from baskfy_providers.fixture_builder import (
    FIXTURE_AS_OF,
    FIXTURE_INSTRUMENT_COUNT,
    FIXTURE_YEARS,
    build,
)
from baskfy_providers.fixtures import FixtureProvider
from baskfy_providers.ports import BARS_CAPABILITIES, REFERENCE_CAPABILITIES
from baskfy_providers.records import BHAVCOPY_SCHEMA, DAILY_BARS_SCHEMA

WIDE_WINDOW_START = dt.date(2000, 1, 1)


class TestShape:
    """The dimensions Prompt 2 asks for."""

    def test_about_forty_instruments(self, fixture_provider: FixtureProvider) -> None:
        assert len(fixture_provider.list_instruments()) == FIXTURE_INSTRUMENT_COUNT

    def test_three_years_of_history(self, fixture_provider: FixtureProvider) -> None:
        bars = fixture_provider.daily_bars_for_symbol("CUPID", WIDE_WINDOW_START, FIXTURE_AS_OF)
        span = (bars["date"].max(), bars["date"].min())
        assert isinstance(span[0], dt.date) and isinstance(span[1], dt.date)
        assert (span[0] - span[1]).days >= FIXTURE_YEARS * 365 - 5

    def test_every_instrument_has_bars(self, fixture_provider: FixtureProvider) -> None:
        """A fixture instrument with no history would silently skip whatever it was added for."""
        for record in fixture_provider.list_instruments():
            assert record.kite_token is not None
            frame = fixture_provider.daily_bars(record.kite_token, WIDE_WINDOW_START, FIXTURE_AS_OF)
            assert frame.height > 0, f"{record.symbol} has no bars"

    def test_it_serves_both_ports(self, fixture_provider: FixtureProvider) -> None:
        """docs/03's `local` environment stubs the whole provider layer with this one object."""
        assert fixture_provider.capabilities() == BARS_CAPABILITIES | REFERENCE_CAPABILITIES


class TestCorporateActions:
    """Prompt 2 §5: "at least one instrument with a split and one with a bonus"."""

    def test_there_is_a_split(self, fixture_provider: FixtureProvider) -> None:
        actions = fixture_provider.corporate_actions(WIDE_WINDOW_START)
        assert [a for a in actions if a.action_type == "split"]

    def test_there_is_a_bonus(self, fixture_provider: FixtureProvider) -> None:
        actions = fixture_provider.corporate_actions(WIDE_WINDOW_START)
        assert [a for a in actions if a.action_type == "bonus"]

    def test_cupids_documented_actions_are_present(self, fixture_provider: FixtureProvider) -> None:
        """docs/01 §9: CUPID had a bonus 4:1 in Mar 2026 and a 10:1 split + 1:1 bonus in Apr 2024.

        These are the actions that make docs/01's 753% unadjusted return an adjustment artefact,
        so they are exactly what Prompt 3's adjustment step needs to be tested against.
        """
        actions = [
            a for a in fixture_provider.corporate_actions(WIDE_WINDOW_START) if a.symbol == "CUPID"
        ]
        by_type = {(a.action_type, a.ex_date.year) for a in actions}
        assert ("bonus", 2026) in by_type
        assert ("split", 2024) in by_type
        assert ("bonus", 2024) in by_type

    def test_the_bonus_ratio_follows_the_documented_orientation(
        self, fixture_provider: FixtureProvider
    ) -> None:
        """docs/04: bonus 4:1 -> ratio_from=4, ratio_to=1."""
        bonus = next(
            a
            for a in fixture_provider.corporate_actions(WIDE_WINDOW_START)
            if a.symbol == "CUPID" and a.ex_date == dt.date(2026, 3, 9)
        )
        assert (bonus.ratio_from, bonus.ratio_to) == (Decimal(4), Decimal(1))

    def test_a_dividend_is_present_so_all_four_shapes_are_exercised(
        self, fixture_provider: FixtureProvider
    ) -> None:
        """docs/09's adjustment table covers split, bonus, rights and cash dividend."""
        actions = fixture_provider.corporate_actions(WIDE_WINDOW_START)
        dividend = next(a for a in actions if a.action_type == "dividend")
        assert dividend.amount is not None

    def test_provenance_travels_with_each_action(self, fixture_provider: FixtureProvider) -> None:
        """Which rows are real and which are synthetic must be readable from the data itself."""
        for action in fixture_provider.corporate_actions(WIDE_WINDOW_START):
            assert action.raw.get("provenance")

    def test_the_since_filter_is_applied(self, fixture_provider: FixtureProvider) -> None:
        recent = fixture_provider.corporate_actions(dt.date(2026, 1, 1))
        assert all(a.ex_date >= dt.date(2026, 1, 1) for a in recent)
        assert recent


class TestAnchoredToRealData:
    """The fixture's believability comes from being anchored to the docs/13 export."""

    def test_the_final_close_matches_the_reference_export(
        self, fixture_provider: FixtureProvider
    ) -> None:
        reference = {str(f["symbol"]): f["close"] for f in to_rows().factors}
        bars = fixture_provider.daily_bars_for_symbol("CUPID", FIXTURE_AS_OF, FIXTURE_AS_OF)
        assert bars["close"][0] == reference["CUPID"]

    def test_every_symbol_exists_in_the_reference_export(
        self, fixture_provider: FixtureProvider
    ) -> None:
        real = {i.symbol for i in to_rows().instruments}
        assert {i.symbol for i in fixture_provider.list_instruments()} <= real

    def test_index_memberships_come_from_the_export(
        self, fixture_provider: FixtureProvider
    ) -> None:
        members = fixture_provider.index_constituents("nifty-total-market", FIXTURE_AS_OF)
        assert members
        real = {m.symbol for m in to_rows().memberships if m.universe_slug == "nifty-total-market"}
        assert set(members) <= real

    def test_the_provenance_file_ships_with_the_fixtures(
        self, fixture_provider: FixtureProvider
    ) -> None:
        """Anyone reading these numbers must be able to see what is real and what is not."""
        text = (fixture_provider.directory / "PROVENANCE.md").read_text(encoding="utf-8")
        assert "Synthetic" in text
        assert "not market data" in text


class TestSchemas:
    def test_bars_conform_to_the_shared_schema(self, fixture_provider: FixtureProvider) -> None:
        """The whole point of a fixed schema: FixtureProvider and KiteProvider are swappable."""
        frame = fixture_provider.daily_bars_for_symbol("CUPID", WIDE_WINDOW_START, FIXTURE_AS_OF)
        assert dict(frame.schema) == DAILY_BARS_SCHEMA

    def test_bhavcopy_conforms_to_the_shared_schema(
        self, fixture_provider: FixtureProvider
    ) -> None:
        assert dict(fixture_provider.bhavcopy(FIXTURE_AS_OF).schema) == BHAVCOPY_SCHEMA

    def test_bhavcopy_carries_series_and_circuit_bands(
        self, fixture_provider: FixtureProvider
    ) -> None:
        """docs/09 §"Provider ports": bhavcopy includes circuit bands and series."""
        frame = fixture_provider.bhavcopy(FIXTURE_AS_OF)
        assert frame["series"].null_count() == 0
        assert frame["upper_circuit"].null_count() == 0
        assert frame["lower_circuit"].null_count() == 0

    def test_prices_are_decimals(self, fixture_provider: FixtureProvider) -> None:
        frame = fixture_provider.daily_bars_for_symbol("CUPID", WIDE_WINDOW_START, FIXTURE_AS_OF)
        assert isinstance(frame["close"].dtype, pl.Decimal)

    def test_an_empty_window_yields_an_empty_frame_with_the_schema(
        self, fixture_provider: FixtureProvider
    ) -> None:
        frame = fixture_provider.daily_bars_for_symbol(
            "CUPID", dt.date(1990, 1, 1), dt.date(1990, 12, 31)
        )
        assert frame.height == 0
        assert dict(frame.schema) == DAILY_BARS_SCHEMA


class TestBarIntegrity:
    def test_bars_are_internally_consistent(self, fixture_provider: FixtureProvider) -> None:
        """low <= open,close <= high, or the fixture would teach the factor engine nonsense."""
        frame = fixture_provider.daily_bars_for_symbol("CUPID", WIDE_WINDOW_START, FIXTURE_AS_OF)
        assert frame.filter(pl.col("low") > pl.col("high")).height == 0
        assert frame.filter(pl.col("close") > pl.col("high")).height == 0
        assert frame.filter(pl.col("close") < pl.col("low")).height == 0
        assert frame.filter(pl.col("open") > pl.col("high")).height == 0
        assert frame.filter(pl.col("open") < pl.col("low")).height == 0

    def test_prices_are_positive(self, fixture_provider: FixtureProvider) -> None:
        frame = fixture_provider.daily_bars_for_symbol("CUPID", WIDE_WINDOW_START, FIXTURE_AS_OF)
        assert frame.filter(pl.col("low") <= 0).height == 0

    def test_dates_are_unique_and_ordered(self, fixture_provider: FixtureProvider) -> None:
        frame = fixture_provider.daily_bars_for_symbol("CUPID", WIDE_WINDOW_START, FIXTURE_AS_OF)
        dates = frame["date"].to_list()
        assert dates == sorted(dates)
        assert len(set(dates)) == len(dates)

    def test_there_are_no_weekend_bars(self, fixture_provider: FixtureProvider) -> None:
        """The fixture is built on baskfy_core.trading_calendar, so weekends must be absent."""
        frame = fixture_provider.daily_bars_for_symbol("CUPID", WIDE_WINDOW_START, FIXTURE_AS_OF)
        weekend = [
            d for d in frame["date"].to_list() if isinstance(d, dt.date) and d.weekday() >= 5
        ]
        assert weekend == []


class TestDeterminism:
    def test_rebuilding_produces_identical_files(self, tmp_path: Path) -> None:
        """A fixture that shifts between machines makes every downstream test unreproducible."""
        first = tmp_path / "a"
        second = tmp_path / "b"
        rows = to_rows()
        build(first, rows)
        build(second, rows)
        for name in ("daily_bars.parquet", "corporate_actions.parquet", "instruments.parquet"):
            assert (first / name).read_bytes() == (second / name).read_bytes(), name


class TestFailureModes:
    def test_a_missing_fixture_directory_is_reported_not_crashed(self, tmp_path: Path) -> None:
        health = FixtureProvider(tmp_path / "nope").check()
        assert health.available is False
        assert "fixture_builder" in health.detail

    def test_reading_from_an_empty_directory_raises_provider_unavailable(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ProviderUnavailable, match="fixture_builder"):
            FixtureProvider(tmp_path).list_instruments()

    def test_an_unknown_token_is_refused(self, fixture_provider: FixtureProvider) -> None:
        with pytest.raises(UnexpectedPayload, match="kite_token"):
            fixture_provider.daily_bars(1, WIDE_WINDOW_START, FIXTURE_AS_OF)

    def test_an_inverted_window_is_rejected(self, fixture_provider: FixtureProvider) -> None:
        with pytest.raises(ValueError, match="precedes"):
            fixture_provider.daily_bars_for_symbol("CUPID", FIXTURE_AS_OF, WIDE_WINDOW_START)
