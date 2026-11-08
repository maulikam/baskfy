//! FO2 — `KiteProvider::fno_option_contracts`: the NFO master for every F&O underlying at
//! once. Ports `tests/test_kite_fno_master.py`: one dump, one limiter token, every
//! underlying's options and nothing else (no futures), each row's underlying its own `name`, a
//! row missing a sizing fact skipped, and a lot above a smallint kept exactly.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod common;
mod kite_common;

use std::collections::BTreeSet;
use std::sync::{Arc, Mutex};

use baskfy_providers::kite::{KiteMap, KiteValue};
use common::arm;
use kite_common::{CountingLimiter, DumpKite, date, options_provider};

/// `_row(token, name, kind, strike=, lot=, tick=)`.
fn row(token: i64, name: &str, kind: &str, strike: f64, lot: i64) -> KiteMap {
    #[allow(clippy::cast_possible_truncation)]
    let whole = strike as i64;
    let segment = if kind == "CE" || kind == "PE" {
        "NFO-OPT"
    } else {
        "NFO-FUT"
    };
    [
        ("instrument_token", token.into()),
        ("tradingsymbol", format!("{name}26OCT{whole}{kind}").into()),
        ("name", name.into()),
        ("expiry", date(2026, 10, 27).into()),
        ("strike", strike.into()),
        ("instrument_type", kind.into()),
        ("lot_size", lot.into()),
        ("tick_size", 0.05.into()),
        ("segment", segment.into()),
        ("exchange", "NFO".into()),
    ]
    .into_iter()
    .collect()
}

fn dump(rows: Vec<KiteMap>) -> Arc<DumpKite> {
    Arc::new(DumpKite {
        rows: rows.into_iter().map(KiteValue::Map).collect(),
        calls: Mutex::new(Vec::new()),
    })
}

#[tokio::test]
async fn every_underlyings_options_in_one_call() {
    arm();
    let mut no_lot = row(6, "SBIN", "CE", 100.0, 50);
    // A sizing fact missing: skipped, not guessed.
    no_lot.insert("lot_size", 0_i64.into());
    let fake = dump(vec![
        row(5, "NIFTY", "CE", 25_000.0, 65),
        row(4, "BANKNIFTY", "PE", 55_000.0, 30),
        row(3, "IDEA", "CE", 7.5, 71_475),
        row(2, "RELIANCE", "FUT", 0.0, 50),
        row(1, "RELIANCE", "PE", 1_400.0, 500),
        no_lot,
    ]);
    let limiter = Arc::new(CountingLimiter::default());
    let (_dir, provider) = options_provider(fake.clone(), limiter.clone());
    let records = provider.fno_option_contracts().await.unwrap();
    assert_eq!(*fake.calls.lock().unwrap(), vec![Some("NFO".to_owned())]);
    assert_eq!(limiter.count(), 1);
    assert_eq!(
        records
            .iter()
            .map(|r| r.instrument_token)
            .collect::<Vec<_>>(),
        vec![1, 3, 4, 5],
        "sorted by token, no future"
    );
    assert_eq!(
        records
            .iter()
            .map(|r| r.underlying.clone())
            .collect::<BTreeSet<_>>(),
        ["NIFTY", "BANKNIFTY", "IDEA", "RELIANCE"]
            .into_iter()
            .map(str::to_owned)
            .collect()
    );
    let idea = records.iter().find(|r| r.underlying == "IDEA").unwrap();
    assert_eq!(idea.lot_size, 71_475);
    assert_eq!(idea.strike.to_string(), "7.5");
    let nifty = records.iter().find(|r| r.underlying == "NIFTY").unwrap();
    assert_eq!(nifty.strike.to_string(), "25000.0");
    assert_eq!(nifty.tick_size.to_string(), "0.05");
    assert_eq!(nifty.expiry, date(2026, 10, 27));
    assert_eq!(nifty.tradingsymbol, "NIFTY26OCT25000CE");
}

#[tokio::test]
async fn it_agrees_with_the_per_underlying_read_for_nifty() {
    arm();
    let fake = dump(vec![
        row(1, "NIFTY", "CE", 25_000.0, 65),
        row(2, "NIFTY", "PE", 25_000.0, 65),
        row(3, "BANKNIFTY", "CE", 55_000.0, 30),
    ]);
    let (_dir, provider) = options_provider(fake, Arc::new(CountingLimiter::default()));
    let everything = provider.fno_option_contracts().await.unwrap();
    let nifty: Vec<_> = everything
        .into_iter()
        .filter(|r| r.underlying == "NIFTY")
        .collect();
    assert_eq!(nifty, provider.option_contracts("NIFTY").await.unwrap());
    assert_eq!(nifty.len(), 2);
}

#[tokio::test]
async fn fno_underlyings_are_the_distinct_futures_names() {
    arm();
    // `fno_underlyings` (9 Sep 2026) reads the same dump: FUT rows only, upper-cased, sorted.
    let mut lower = row(7, "tcs", "FUT", 0.0, 175);
    lower.insert("name", " tcs ".into());
    let fake = dump(vec![
        row(1, "RELIANCE", "FUT", 0.0, 500),
        row(2, "RELIANCE", "FUT", 0.0, 500),
        row(3, "NIFTY", "CE", 25_000.0, 65),
        row(4, "BANKNIFTY", "FUT", 0.0, 30),
        lower,
    ]);
    let (_dir, provider) = options_provider(fake, Arc::new(CountingLimiter::default()));
    assert_eq!(
        provider.fno_underlyings().await.unwrap(),
        vec!["BANKNIFTY", "RELIANCE", "TCS"]
    );
}
