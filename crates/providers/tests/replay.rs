//! The replay adapter (DECISIONS-RS R7.3) over a small bundle written here: answers, frames on
//! the schemas, the explicit refusals, and the composite's wording around them. Its agreement
//! with the Python twin is graded by `tools/parity/replay_selfcheck.py` over a real bundle.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;

use std::sync::Arc;

use baskfy_providers::composite::{CompositeProvider, SupplementaryReads};
use baskfy_providers::errors::ErrorKind;
use baskfy_providers::ports::{BarsProvider, HealthReporting, ReferenceProvider};
use baskfy_providers::records::{bhavcopy_schema, daily_bars_schema};
use baskfy_providers::replay::ReplayProvider;
use chrono::NaiveDate;
use common::arm;

fn day(s: &str) -> NaiveDate {
    NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap()
}

fn write(root: &std::path::Path, rel: &str, text: &str) {
    let p = root.join(rel);
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    std::fs::write(p, text).unwrap();
}

fn bundle(refuse: &str) -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    let r = dir.path();
    write(
        r,
        "manifest.json",
        &format!(
            r#"{{"format": 1, "trade_date": "2026-10-29", "variant": "t", "refuse": {refuse}}}"#
        ),
    );
    write(
        r,
        "calls/list_instruments.json",
        r#"[{"symbol": "ABC", "name": "Abc Ltd", "instrument_type": "EQ", "series": null, "isin": null,
            "exchange": "NSE", "kite_token": 11, "lot_size": 1, "face_value": null, "listed_on": null,
            "delisted_on": null}]"#,
    );
    write(
        r,
        "calls/daily_bars.json",
        r#"{"start": "2026-10-29", "end": "2026-10-29", "by_token": {
            "11": [{"symbol": "ABC", "date": "2026-10-29", "open": "10.5000", "high": "11", "low": "10.1",
                    "close": "10.9000", "volume": 1200}],
            "12": []}}"#,
    );
    write(
        r,
        "calls/bhavcopy/2026-10-29.json",
        r#"[{"symbol": "ABC", "series": "EQ", "date": "2026-10-29", "open": "10.5000", "high": "11.0000",
             "low": "10.1000", "close": "10.9000", "prev_close": null, "volume": 1200, "turnover": "13080.00",
             "trades": null, "upper_circuit": "12.0000", "lower_circuit": "9.0000"}]"#,
    );
    write(r, "calls/etf_symbols.json", r#"["NIFTYBEES"]"#);
    write(
        r,
        "calls/equity_fundamentals/2026-10-29.json",
        r#"[{"symbol": "ABC", "date": "2026-10-29", "shares_outstanding": 100, "last_price": "10.9000",
             "marketcap_cr": 1, "pe": null, "pb": null, "div_yield": null},
            {"symbol": "XYZ", "date": "2026-10-29", "shares_outstanding": null, "last_price": null,
             "marketcap_cr": null, "pe": null, "pb": null, "div_yield": null}]"#,
    );
    dir
}

#[tokio::test]
async fn recorded_answers_come_back_on_the_schemas() {
    arm();
    let dir = bundle("[]");
    let p = ReplayProvider::open(dir.path()).unwrap();
    assert_eq!(p.name(), "replay");
    assert!(p.check().available);
    let inst = p.list_instruments().await.unwrap();
    assert_eq!(inst[0].symbol, "ABC");
    let bars = p
        .daily_bars(11, day("2026-10-29"), day("2026-10-29"))
        .await
        .unwrap();
    assert_eq!(bars.schema().as_ref(), &daily_bars_schema());
    assert_eq!(bars.height(), 1);
    let empty = p
        .daily_bars(12, day("2026-10-29"), day("2026-10-29"))
        .await
        .unwrap();
    assert_eq!(empty.height(), 0);
    assert_eq!(empty.schema().as_ref(), &daily_bars_schema());
    let bhav = p.bhavcopy(day("2026-10-29")).await.unwrap();
    assert_eq!(bhav.schema().as_ref(), &bhavcopy_schema());
    assert_eq!(p.etf_symbols().await.unwrap(), vec!["NIFTYBEES"]);
    let funds = p
        .equity_fundamentals(
            day("2026-10-29"),
            &[
                "XYZ".to_owned(),
                "NOPE".to_owned(),
                "ABC".to_owned(),
                "XYZ".to_owned(),
            ],
            None,
        )
        .await
        .unwrap();
    let order: Vec<&str> = funds.iter().map(|f| f.symbol.as_str()).collect();
    assert_eq!(
        order,
        ["XYZ", "ABC"],
        "asked order, each once, unknowns omitted"
    );
}

#[tokio::test]
async fn an_unrecorded_call_is_provider_unavailable_never_an_empty_answer() {
    arm();
    let dir = bundle("[]");
    let p = ReplayProvider::open(dir.path()).unwrap();
    let e = p
        .daily_bars(99, day("2026-10-29"), day("2026-10-29"))
        .await
        .unwrap_err();
    assert_eq!(e.kind(), ErrorKind::ProviderUnavailable);
    assert_eq!(
        e.to_string(),
        "[replay] no recorded answer for daily_bars(token=99, start=2026-10-29, end=2026-10-29)"
    );
    let e = p
        .daily_bars(11, day("2026-10-28"), day("2026-10-29"))
        .await
        .unwrap_err();
    assert_eq!(
        e.to_string(),
        "[replay] no recorded answer for daily_bars(token=11, start=2026-10-28, end=2026-10-29): \
         the bundle holds 2026-10-29..2026-10-29"
    );
    let e = p.bhavcopy(day("2026-10-28")).await.unwrap_err();
    assert_eq!(
        e.to_string(),
        "[replay] no recorded answer for bhavcopy(on=2026-10-28)"
    );
    let e = p
        .index_constituents("../manifest", day("2026-10-29"))
        .await
        .unwrap_err();
    assert_eq!(e.kind(), ErrorKind::ProviderUnavailable);
    let e = p.index_level("nifty-50").await.unwrap_err();
    assert_eq!(
        e.to_string(),
        "[replay] no recorded answer for index_level(slug=nifty-50)"
    );
    assert!(p.listings().await.is_err());
}

#[tokio::test]
async fn a_refusal_names_its_call_and_its_class() {
    arm();
    let dir = bundle(
        r#"[{"method": "daily_bars", "args": {"token": 11}, "kind": "UpstreamUnavailable",
             "message": "down"},
            {"method": "etf_symbols", "message": "no list today"}]"#,
    );
    let p = ReplayProvider::open(dir.path()).unwrap();
    let e = p
        .daily_bars(11, day("2026-10-29"), day("2026-10-29"))
        .await
        .unwrap_err();
    assert_eq!(e.kind(), ErrorKind::UpstreamUnavailable);
    assert_eq!(e.to_string(), "[replay] down");
    assert!(
        p.daily_bars(12, day("2026-10-29"), day("2026-10-29"))
            .await
            .is_ok()
    );
    let e = p.etf_symbols().await.unwrap_err();
    assert_eq!(e.kind(), ErrorKind::ProviderUnavailable);
}

#[tokio::test]
async fn behind_the_composite_an_unavailable_answer_is_capability_not_available() {
    arm();
    let dir = bundle("[]");
    let replay = Arc::new(ReplayProvider::open(dir.path()).unwrap());
    let stack = CompositeProvider::new(vec![replay.register()], 5, 60.0, None).unwrap();
    let e = stack.bhavcopy(day("2026-10-28")).await.unwrap_err();
    assert_eq!(e.kind(), ErrorKind::CapabilityNotAvailable);
    assert_eq!(
        e.to_string(),
        "[composite] no provider could serve 'bhavcopy'. Tried: replay: [replay] no recorded \
         answer for bhavcopy(on=2026-10-28)"
    );
    assert_eq!(stack.etf_symbols().await.unwrap(), vec!["NIFTYBEES"]);
}

#[test]
fn a_bundle_of_another_format_is_refused() {
    arm();
    let dir = tempfile::tempdir().unwrap();
    write(
        dir.path(),
        "manifest.json",
        r#"{"format": 2, "trade_date": "2026-10-29"}"#,
    );
    let e = ReplayProvider::open(dir.path()).unwrap_err();
    assert_eq!(e.to_string(), "[replay] replay bundle format 2 is not 1");
}
