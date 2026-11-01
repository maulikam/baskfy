//! The port records and the port traits — ports `tests/test_records_and_ports.py`.
//!
//! Python's `isinstance(KiteProvider(settings), BarsProvider)` checks are compile-time here:
//! `StubProvider` (tests/common) implements both ports, so a trait that drifted from the
//! capability list fails to build. The Kite/NSE/fixture conformance cases belong to those
//! ports' porters (STATUS.md).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;

use std::sync::Arc;

use baskfy_providers::ports::{
    BarsProvider, Capability, CapabilitySet, HealthReporting, ReferenceProvider, bars_capabilities,
    holdings_capabilities, reference_capabilities,
};
use baskfy_providers::records::{
    BrokerHoldingFields, BrokerHoldingRecord, CorporateAction, CorporateActionFields,
    CorporateActionType, IndexSnapshot, IndexSnapshotFields, InstrumentFields, InstrumentRecord,
    InstrumentType, ListingFields, ListingRecord, MinuteBarRecord, PyDateTime, bhavcopy_schema,
    conform, daily_bars_schema, empty_frame, fo_bhavcopy_schema,
};
use chrono::NaiveDate;
use common::{StubProvider, arm};
use polars::prelude::{DataType, NamedFrom, Series, df};
use rust_decimal::Decimal;
use serde_json::json;

fn d(y: i32, m: u32, day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, day).unwrap()
}

// --- TestProtocolConformance ----------------------------------------------------------------

#[tokio::test]
async fn a_stub_serves_both_ports_through_trait_objects() {
    arm();
    let all: CapabilitySet = bars_capabilities()
        .union(&reference_capabilities())
        .copied()
        .collect();
    let stub = Arc::new(StubProvider::new("stub", all));
    let bars: Arc<dyn BarsProvider> = stub.clone();
    let reference: Arc<dyn ReferenceProvider> = stub.clone();
    assert_eq!(bars.list_instruments().await.unwrap().len(), 1);
    assert_eq!(reference.listings().await.unwrap().len(), 1);
    assert!(bars.check().available);
}

#[test]
fn capability_values_match_the_port_method_names() {
    arm();
    // The trait methods, as declared in ports.rs (a rename there must be made here too).
    let bars_methods = ["list_instruments", "daily_bars"];
    let reference_methods = [
        "index_constituents",
        "index_snapshots",
        "corporate_actions",
        "listings",
        "bhavcopy",
        "equity_fundamentals",
    ];
    let holdings_methods = ["broker_holdings", "broker_cash"];
    for c in bars_capabilities() {
        assert!(bars_methods.contains(&c.value()), "{c:?}");
    }
    for c in reference_capabilities() {
        assert!(reference_methods.contains(&c.value()), "{c:?}");
    }
    for c in holdings_capabilities() {
        assert!(holdings_methods.contains(&c.value()), "{c:?}");
    }
    for c in Capability::ALL {
        assert_eq!(Capability::from_value(c.value()), Some(c));
    }
}

#[test]
fn the_capability_sets_are_disjoint_and_complete() {
    arm();
    assert!(bars_capabilities().is_disjoint(&reference_capabilities()));
    assert!(bars_capabilities().is_disjoint(&holdings_capabilities()));
    assert!(reference_capabilities().is_disjoint(&holdings_capabilities()));
    let union: CapabilitySet = bars_capabilities()
        .into_iter()
        .chain(reference_capabilities())
        .chain(holdings_capabilities())
        .collect();
    assert_eq!(
        union,
        Capability::ALL.into_iter().collect::<CapabilitySet>()
    );
}

#[test]
fn an_unavailable_provider_serves_nothing() {
    arm();
    let mut stub = StubProvider::new("x", bars_capabilities());
    stub.available = false;
    assert!(stub.check().served_capabilities().is_empty());
}

// --- TestInstrumentRecord -----------------------------------------------------------------

#[test]
fn an_unknown_instrument_type_is_rejected() {
    arm();
    let r: Result<InstrumentRecord, _> =
        serde_json::from_value(json!({"symbol": "X", "name": "X", "instrument_type": "FUT"}));
    assert!(r.is_err());
}

#[test]
fn an_empty_symbol_is_rejected() {
    arm();
    let err =
        InstrumentRecord::new(InstrumentFields::new("", "X", InstrumentType::Eq)).unwrap_err();
    assert_eq!(err.field, "symbol");
    let r: Result<InstrumentRecord, _> =
        serde_json::from_value(json!({"symbol": "", "name": "X", "instrument_type": "EQ"}));
    assert!(r.is_err());
}

#[test]
fn unknown_keys_are_rejected() {
    arm();
    let r: Result<InstrumentRecord, _> = serde_json::from_value(
        json!({"symbol": "X", "name": "X", "instrument_type": "EQ", "segment": "NSE"}),
    );
    assert!(r.is_err());
}

#[test]
fn records_are_immutable() {
    // Python asserts setattr raises. In Rust a record derefs to its fields read-only and has no
    // setter; the only way to change one is to take its fields out and validate them again.
    arm();
    let record = InstrumentRecord::new(InstrumentFields::new(
        "SBIN",
        "STATE BANK",
        InstrumentType::Eq,
    ))
    .unwrap();
    let mut fields = record.clone().into_fields();
    fields.symbol = String::new();
    assert_eq!(record.symbol, "SBIN");
    assert!(InstrumentRecord::new(fields).is_err());
}

#[test]
fn a_kite_token_is_optional() {
    arm();
    let r = InstrumentRecord::new(InstrumentFields::new("X", "X", InstrumentType::Eq)).unwrap();
    assert_eq!(r.kite_token, None);
    assert_eq!(r.exchange, "NSE");
}

// --- TestCorporateActionRecord ------------------------------------------------------------

#[test]
fn an_unknown_action_type_is_rejected() {
    arm();
    let r: Result<CorporateAction, _> = serde_json::from_value(
        json!({"symbol": "X", "action_type": "buyback", "ex_date": "2026-01-01"}),
    );
    assert!(r.is_err());
}

#[test]
fn a_non_positive_ratio_leg_is_rejected() {
    arm();
    let mut f = CorporateActionFields::new("X", CorporateActionType::Split, d(2026, 1, 1));
    f.ratio_from = Some(Decimal::ZERO);
    f.ratio_to = Some(Decimal::ONE);
    let err = CorporateAction::new(f).unwrap_err();
    assert_eq!(err.field, "ratio_from");
}

#[test]
fn the_documented_split_orientation_is_accepted() {
    arm();
    let mut f = CorporateActionFields::new("CUPID", CorporateActionType::Split, d(2024, 4, 15));
    f.ratio_from = Some(Decimal::from(10));
    f.ratio_to = Some(Decimal::ONE);
    let action = CorporateAction::new(f).unwrap();
    assert_eq!(action.ratio_from, Some(Decimal::from(10)));
}

#[test]
fn the_raw_payload_is_retained() {
    arm();
    let action: CorporateAction = serde_json::from_value(json!({
        "symbol": "X", "action_type": "dividend", "ex_date": "2026-01-01",
        "raw": {"purpose": "DIVIDEND RS.5"}
    }))
    .unwrap();
    assert_eq!(action.raw["purpose"], "DIVIDEND RS.5");
}

// --- TestIndexSnapshotRecord --------------------------------------------------------------

#[test]
fn fundamentals_may_be_absent() {
    arm();
    let s = IndexSnapshot::new(IndexSnapshotFields::new("india-vix", d(2026, 8, 18))).unwrap();
    assert_eq!((s.pe, s.pb, s.div_yield), (None, None, None));
}

#[test]
fn levels_are_decimals() {
    arm();
    let s: IndexSnapshot = serde_json::from_value(
        json!({"index_slug": "nifty-50", "date": "2026-08-18", "level": "24500.35"}),
    )
    .unwrap();
    assert_eq!(s.level, Some(Decimal::new(2_450_035, 2)));
    assert_eq!(s.level.unwrap().to_string(), "24500.35");
}

// --- TestListingRecord --------------------------------------------------------------------

#[test]
fn a_listing_date_may_be_absent() {
    arm();
    assert_eq!(
        ListingRecord::new(ListingFields::new("X", "X"))
            .unwrap()
            .listed_on,
        None
    );
}

// --- Holdings and timestamps ----------------------------------------------------------------

#[test]
fn total_quantity_is_the_three_way_sum() {
    // Desk non-negotiable #2.
    arm();
    let mut f = BrokerHoldingFields::new("SBIN");
    f.quantity = Decimal::from(60);
    f.collateral_quantity = Decimal::from(40);
    f.t1_quantity = Decimal::new(5, 0);
    assert_eq!(
        BrokerHoldingRecord::new(f).unwrap().total_quantity(),
        Decimal::from(105)
    );
    let mut bad = BrokerHoldingFields::new("SBIN");
    bad.quantity = Decimal::from(-1);
    assert!(BrokerHoldingRecord::new(bad).is_err());
}

#[test]
fn a_minute_bar_needs_an_aware_timestamp() {
    arm();
    let ok: Result<MinuteBarRecord, _> = serde_json::from_value(json!({
        "ts": "2026-08-20T09:15:00+05:30", "open": "1", "high": "1", "low": "1", "close": "1"
    }));
    assert!(ok.is_ok());
    let naive: Result<MinuteBarRecord, _> = serde_json::from_value(json!({
        "ts": "2026-08-20 09:15:00", "open": "1", "high": "1", "low": "1", "close": "1"
    }));
    assert!(naive.is_err());
    assert_eq!(
        PyDateTime::parse("2026-08-20 09:15:00.500000")
            .unwrap()
            .isoformat(),
        "2026-08-20T09:15:00.500000"
    );
}

// --- TestFrameSchemas ---------------------------------------------------------------------

#[test]
fn an_empty_frame_still_carries_the_schema() {
    arm();
    let frame = empty_frame(&daily_bars_schema());
    assert_eq!(frame.height(), 0);
    assert_eq!(frame.schema().as_ref(), &daily_bars_schema());
}

#[test]
fn conform_projects_and_casts() {
    arm();
    let frame = df!(
        "symbol" => ["SBIN"],
        "date" => [d(2026, 8, 18)],
        "open" => ["100.5"],
        "high" => ["101.5"],
        "low" => ["99.5"],
        "close" => ["100.0"],
        "volume" => [1000_i64],
        "source" => ["kite"],
        "extra" => ["dropped"],
    )
    .unwrap();
    let conformed = conform(&frame, &daily_bars_schema()).unwrap();
    assert_eq!(conformed.schema().as_ref(), &daily_bars_schema());
    assert!(
        !conformed
            .get_column_names()
            .iter()
            .any(|c| c.as_str() == "extra")
    );
    let open = conformed.column("open").unwrap().get(0).unwrap();
    assert_eq!(open.to_string(), "100.5000");
}

#[test]
fn conform_rejects_a_missing_column() {
    arm();
    let frame = polars::prelude::DataFrame::new_infer_height(vec![
        Series::new("symbol".into(), ["SBIN"]).into(),
    ])
    .unwrap();
    let err = conform(&frame, &daily_bars_schema()).unwrap_err();
    assert_eq!(err.python_type(), "ValueError");
    assert_eq!(
        err.to_string(),
        "frame is missing required columns ['date', 'open', 'high', 'low', 'close', 'volume', \
         'source']; got ['symbol']"
    );
}

#[test]
fn prices_are_decimal_typed_in_both_schemas() {
    arm();
    for schema in [daily_bars_schema(), bhavcopy_schema()] {
        for column in ["open", "high", "low", "close"] {
            assert!(matches!(schema.get(column), Some(DataType::Decimal(18, 4))));
        }
    }
}

#[test]
fn the_bhavcopy_schema_carries_series_and_circuit_bands() {
    arm();
    let schema = bhavcopy_schema();
    for column in ["series", "upper_circuit", "lower_circuit"] {
        assert!(schema.get(column).is_some(), "{column}");
    }
    assert!(matches!(
        schema.get("turnover"),
        Some(DataType::Decimal(20, 2))
    ));
    assert_eq!(fo_bhavcopy_schema().len(), 17);
}

#[test]
fn volumes_are_integers_not_decimals() {
    arm();
    assert_eq!(daily_bars_schema().get("volume"), Some(&DataType::Int64));
}
