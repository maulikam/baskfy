#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
//! `run_backtest` and `run_fragility` against the goldens (`go/testdata/golden/L1/backtest`):
//! 22 + 3 cases.
//!
//! Each case is decoded exactly as `tools/parity/golden_backtest.py`'s `call_run_backtest` /
//! `call_run_fragility` decode it (`decode_config`, `decode_data`, the optional `schedule`),
//! the Rust port is called, and the result is encoded the way the dumper encodes the Python
//! result: the dataclasses field for field, a raised `BacktestError` as `{"error": {"type",
//! "message"}}` (a `LookAheadError` adds `what`, `offending`, `as_of`). Every case is graded
//! strictly by the testkit and the per-function case count is asserted, so a dropped case
//! fails.

use std::collections::{BTreeMap, HashMap};

use baskfy_core::backtest::engine::{
    BacktestConfig, BacktestProgress, run_backtest, run_fragility,
};
use baskfy_core::backtest::helpers::{AdjustmentCoverage, BacktestData, BacktestError, PricePanel};
use baskfy_testkit::{Golden, decode_frame};
use chrono::NaiveDate;
use polars::prelude::DataFrame;
use serde_json::{Map, Value, json};

fn s<E: ToString>(e: E) -> String {
    e.to_string()
}

fn error_value(e: &BacktestError) -> Value {
    let mut body = Map::new();
    body.insert("type".into(), json!(e.type_name()));
    body.insert("message".into(), json!(e.to_string()));
    if let BacktestError::LookAhead {
        what,
        offending,
        as_of,
    } = e
    {
        body.insert("what".into(), json!(what));
        body.insert("offending".into(), json!(offending.to_string()));
        body.insert("as_of".into(), json!(as_of.to_string()));
    }
    json!({ "error": Value::Object(body) })
}

fn date(v: &Value) -> Result<NaiveDate, String> {
    baskfy_testkit::date(v).map_err(s)
}

fn dates(v: &Value) -> Result<Vec<NaiveDate>, String> {
    v.as_array()
        .ok_or("expected a list of dates")?
        .iter()
        .map(date)
        .collect()
}

fn opt_frame(v: &Value) -> Result<Option<DataFrame>, String> {
    if v.is_null() {
        Ok(None)
    } else {
        decode_frame(v).map(Some).map_err(s)
    }
}

fn int_key_map<T>(
    v: &Value,
    f: impl Fn(&Value) -> Result<T, String>,
) -> Result<HashMap<i64, T>, String> {
    v.as_object()
        .ok_or("expected an object")?
        .iter()
        .map(|(k, x)| Ok((k.parse::<i64>().map_err(s)?, f(x)?)))
        .collect()
}

/// The dumper's `decode_data`. A `PricePanel` refusal is a `BacktestError` raised inside the
/// dumper's guarded call, so it is returned as such.
fn decode_data(doc: &Value) -> Result<Result<BacktestData, BacktestError>, String> {
    let calendar = dates(&doc["calendar"])?;
    let prices = decode_frame(&doc["prices"]).map_err(s)?;
    let price_only = doc["price_only"].as_bool().ok_or("price_only")?;
    let panel = match PricePanel::new(&prices, &calendar, price_only) {
        Ok(p) => p,
        Err(e) => return Ok(Err(e)),
    };
    let mut screens = BTreeMap::new();
    for (k, v) in doc["screens"].as_object().ok_or("screens")? {
        screens.insert(date(&json!(k))?, decode_frame(v).map_err(s)?);
    }
    let coverage: Option<AdjustmentCoverage> =
        serde_json::from_value(doc["adjustment_coverage"].clone()).map_err(s)?;
    let string = |x: &Value| {
        x.as_str()
            .map(str::to_owned)
            .ok_or_else(|| "not a string".to_owned())
    };
    Ok(Ok(BacktestData {
        calendar,
        prices: panel,
        screens,
        benchmark: opt_frame(&doc["benchmark"])?,
        delistings: int_key_map(&doc["delistings"], date)?,
        symbols: int_key_map(&doc["symbols"], string)?,
        names: int_key_map(&doc["names"], string)?,
        dividends: opt_frame(&doc["dividends"])?,
        data_version: doc["data_version"].as_i64(),
        adjustment_coverage: coverage,
    }))
}

fn encode<T: serde::Serialize>(r: Result<T, BacktestError>) -> Result<Value, String> {
    match r {
        Ok(v) => serde_json::to_value(&v).map_err(s),
        Err(e) => Ok(error_value(&e)),
    }
}

fn run_backtest_case(g: &Golden) -> Result<Value, String> {
    let config: BacktestConfig = g.input_as("config").map_err(s)?;
    let data = match decode_data(g.input("data").map_err(s)?)? {
        Ok(d) => d,
        Err(e) => return Ok(error_value(&e)),
    };
    let schedule = match g.inputs.get("schedule") {
        Some(v) => Some(dates(v)?),
        None => None,
    };
    encode(run_backtest(&config, &data, None, schedule.as_deref()))
}

fn run_fragility_case(g: &Golden) -> Result<Value, String> {
    let config: BacktestConfig = g.input_as("config").map_err(s)?;
    let data = match decode_data(g.input("data").map_err(s)?)? {
        Ok(d) => d,
        Err(e) => return Ok(error_value(&e)),
    };
    encode(run_fragility(&config, &data, None, None))
}

fn grade(fn_name: &str, expected: usize, run: fn(&Golden) -> Result<Value, String>) {
    let cases: Vec<Golden> = baskfy_testkit::cases_for(fn_name).unwrap();
    assert_eq!(cases.len(), expected, "{fn_name} case count changed");
    let mut failures = Vec::new();
    for g in &cases {
        match run(g) {
            Ok(actual) => {
                if let Err(m) = g.check_value(&actual) {
                    failures.push(format!("{}: {m}", g.case));
                }
            }
            Err(e) => failures.push(format!("{}: could not run: {e}", g.case)),
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} {fn_name} goldens failed:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
}

#[test]
fn run_backtest_matches_every_golden() {
    grade("baskfy_core.backtest.run_backtest", 22, run_backtest_case);
}

#[test]
fn run_fragility_matches_every_golden() {
    grade("baskfy_core.backtest.run_fragility", 3, run_fragility_case);
}

/// The progress sink sees the first day, every 250th, and the last (Python's `_PROGRESS_EVERY`).
#[test]
fn progress_reports_first_and_last_step() {
    let g = baskfy_testkit::cases_for("baskfy_core.backtest.run_backtest")
        .unwrap()
        .into_iter()
        .next()
        .unwrap();
    let config: BacktestConfig = g.input_as("config").unwrap();
    let data = decode_data(g.input("data").unwrap()).unwrap().unwrap();
    let mut seen: Vec<BacktestProgress> = Vec::new();
    let mut sink = |p: &BacktestProgress| seen.push(p.clone());
    let result = run_backtest(&config, &data, Some(&mut sink), None).unwrap();
    let total = result.dates.len();
    assert_eq!(seen.len(), 2);
    assert_eq!(seen[0].completed, 1);
    assert_eq!(seen[1].completed, total);
    assert_eq!(seen[1].percent(), 100);
    assert_eq!(seen[0].as_of, result.dates.first().copied());
}
