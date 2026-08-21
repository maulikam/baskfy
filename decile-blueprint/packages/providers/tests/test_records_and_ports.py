"""The port records and the Protocols (Prompt 2 deliverable 1).

docs/09 §"Provider ports" defines the five method signatures and the four record types. These
assert that contract — including the parts that only matter when a vendor sends something odd,
since the whole reason these are Pydantic models rather than dataclasses is to catch that at the
seam instead of three steps later inside the factor engine.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from functools import partial

import polars as pl
import pytest
from pydantic import ValidationError

from decile_providers.fixtures import FixtureProvider
from decile_providers.kite import KiteProvider
from decile_providers.nse import NSEProvider
from decile_providers.ports import (
    BARS_CAPABILITIES,
    REFERENCE_CAPABILITIES,
    BarsProvider,
    Capability,
    ReferenceProvider,
)
from decile_providers.records import (
    BHAVCOPY_SCHEMA,
    DAILY_BARS_SCHEMA,
    CorporateAction,
    IndexSnapshot,
    InstrumentRecord,
    ListingRecord,
    conform,
    empty_frame,
)
from decile_providers.settings import ProviderSettings


class TestProtocolConformance:
    """docs/02: "adding a paid vendor later is a new adapter, not a rewrite"."""

    def test_kite_satisfies_the_bars_port(self, settings: ProviderSettings) -> None:
        assert isinstance(KiteProvider(settings), BarsProvider)

    def test_nse_satisfies_the_reference_port(self, settings: ProviderSettings) -> None:
        assert isinstance(NSEProvider(settings), ReferenceProvider)

    def test_the_fixture_provider_satisfies_both(self, fixture_provider: FixtureProvider) -> None:
        assert isinstance(fixture_provider, BarsProvider)
        assert isinstance(fixture_provider, ReferenceProvider)

    def test_kite_does_not_satisfy_the_reference_port(self, settings: ProviderSettings) -> None:
        """docs/02: Kite has no constituents, PE/PB or corporate actions. The type says so."""
        assert not isinstance(KiteProvider(settings), ReferenceProvider)

    def test_capability_values_match_the_port_method_names(self) -> None:
        """Routing is by capability; a mismatch would route a call to a method that is not there."""
        for capability in BARS_CAPABILITIES:
            assert hasattr(BarsProvider, capability.value)
        for capability in REFERENCE_CAPABILITIES:
            assert hasattr(ReferenceProvider, capability.value)

    def test_the_two_capability_sets_are_disjoint_and_complete(self) -> None:
        assert BARS_CAPABILITIES.isdisjoint(REFERENCE_CAPABILITIES)
        assert set(Capability) == BARS_CAPABILITIES | REFERENCE_CAPABILITIES


class TestInstrumentRecord:
    def test_an_unknown_instrument_type_is_rejected(self) -> None:
        """docs/04 constrains instrument_type to EQ | ETF | INDEX."""
        with pytest.raises(ValidationError):
            InstrumentRecord.model_validate({"symbol": "X", "name": "X", "instrument_type": "FUT"})

    def test_an_empty_symbol_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InstrumentRecord(symbol="", name="X", instrument_type="EQ")

    def test_unknown_keys_are_rejected(self) -> None:
        """A vendor adding a field must be a visible decision, not a silent absorption.

        Constructed through ``model_validate`` because the point is what happens to an unexpected
        key at runtime, and the typed constructor would not let one be written at all.
        """
        with pytest.raises(ValidationError):
            InstrumentRecord.model_validate(
                {"symbol": "X", "name": "X", "instrument_type": "EQ", "segment": "NSE"}
            )

    def test_records_are_immutable(self) -> None:
        """A record is a reading of the world at a moment, never a mutable buffer."""
        record = InstrumentRecord(symbol="SBIN", name="STATE BANK", instrument_type="EQ")
        mutate = partial(setattr, record, "symbol")
        with pytest.raises(ValidationError):
            mutate("OTHER")

    def test_a_kite_token_is_optional(self) -> None:
        """Instruments NSE lists but Kite does not carry still belong in the universe."""
        assert InstrumentRecord(symbol="X", name="X", instrument_type="EQ").kite_token is None


class TestCorporateActionRecord:
    def test_an_unknown_action_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CorporateAction.model_validate(
                {"symbol": "X", "action_type": "buyback", "ex_date": dt.date(2026, 1, 1)}
            )

    def test_a_non_positive_ratio_leg_is_rejected(self) -> None:
        """A zero or negative leg would produce a nonsensical adjustment factor (docs/09)."""
        with pytest.raises(ValidationError):
            CorporateAction(
                symbol="X",
                action_type="split",
                ex_date=dt.date(2026, 1, 1),
                ratio_from=Decimal(0),
                ratio_to=Decimal(1),
            )

    def test_the_documented_split_orientation_is_accepted(self) -> None:
        """docs/04: split 10:1 -> from=10, to=1."""
        action = CorporateAction(
            symbol="CUPID",
            action_type="split",
            ex_date=dt.date(2024, 4, 15),
            ratio_from=Decimal(10),
            ratio_to=Decimal(1),
        )
        assert action.ratio_from == Decimal(10)

    def test_the_raw_payload_is_retained(self) -> None:
        """docs/09 makes adjustments re-derivable; that needs the source payload."""
        action = CorporateAction(
            symbol="X",
            action_type="dividend",
            ex_date=dt.date(2026, 1, 1),
            raw={"purpose": "DIVIDEND RS.5"},
        )
        assert action.raw["purpose"] == "DIVIDEND RS.5"


class TestIndexSnapshotRecord:
    def test_fundamentals_may_be_absent(self) -> None:
        """docs/01 §7: India VIX and inverse indices publish no PE/PB/DivYield."""
        snapshot = IndexSnapshot(index_slug="india-vix", date=dt.date(2026, 8, 18))
        assert (snapshot.pe, snapshot.pb, snapshot.div_yield) == (None, None, None)

    def test_levels_are_decimals(self) -> None:
        snapshot = IndexSnapshot(
            index_slug="nifty-50", date=dt.date(2026, 8, 18), level=Decimal("24500.35")
        )
        assert isinstance(snapshot.level, Decimal)


class TestListingRecord:
    def test_a_listing_date_may_be_absent(self) -> None:
        assert ListingRecord(symbol="X", name="X").listed_on is None


class TestFrameSchemas:
    def test_an_empty_frame_still_carries_the_schema(self) -> None:
        """Returning a bare DataFrame for "no data" would silently change the column set."""
        assert dict(empty_frame(DAILY_BARS_SCHEMA).schema) == DAILY_BARS_SCHEMA

    def test_conform_projects_and_casts(self) -> None:
        frame = pl.DataFrame(
            {
                "symbol": ["SBIN"],
                "date": [dt.date(2026, 8, 18)],
                "open": ["100.5"],
                "high": ["101.5"],
                "low": ["99.5"],
                "close": ["100.0"],
                "volume": [1000],
                "source": ["kite"],
                "extra": ["dropped"],
            }
        )
        conformed = conform(frame, DAILY_BARS_SCHEMA)
        assert dict(conformed.schema) == DAILY_BARS_SCHEMA
        assert "extra" not in conformed.columns

    def test_conform_rejects_a_missing_column(self) -> None:
        """A vendor that stops sending `volume` must fail here, not produce silent nulls."""
        with pytest.raises(ValueError, match="missing required columns"):
            conform(pl.DataFrame({"symbol": ["SBIN"]}), DAILY_BARS_SCHEMA)

    def test_prices_are_decimal_typed_in_both_schemas(self) -> None:
        """docs/04: money in numeric, never float — enforced at the provider boundary."""
        for schema in (DAILY_BARS_SCHEMA, BHAVCOPY_SCHEMA):
            for column in ("open", "high", "low", "close"):
                assert isinstance(schema[column], pl.Decimal)

    def test_the_bhavcopy_schema_carries_series_and_circuit_bands(self) -> None:
        """docs/09 §"Provider ports": "incl. circuit bands, series"."""
        assert {"series", "upper_circuit", "lower_circuit"} <= set(BHAVCOPY_SCHEMA)

    def test_volumes_are_integers_not_decimals(self) -> None:
        assert DAILY_BARS_SCHEMA["volume"] == pl.Int64()
