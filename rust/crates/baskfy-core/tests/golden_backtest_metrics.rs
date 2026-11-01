#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
//! `baskfy_core.backtest_metrics` against the goldens
//! (`go/testdata/golden/L1/backtest/<fn>.case_NNN.json`, written by
//! `tools/parity/golden_backtest.py`). Every case of every function is graded strictly by the
//! testkit, and each function's case count is pinned so a dropped case fails.

use baskfy_core::backtest_metrics::{
    BacktestOutcome, EQUITY_CURVE_POINTS, Json, Trade, compute_metrics, downsample_curve,
    drawdown_series, metrics_hash, monthly_returns, rolling_distribution, round_trips,
};
use baskfy_testkit::{Golden, TestkitError};
use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde_json::json;

const PREFIX: &str = "baskfy_core.backtest_metrics.";

fn e(err: TestkitError) -> String {
    err.to_string()
}

fn series(g: &Golden) -> Result<(Vec<NaiveDate>, Vec<Decimal>), String> {
    Ok((
        g.input_as("dates").map_err(e)?,
        g.input_as("equity").map_err(e)?,
    ))
}

fn run_drawdown(g: &Golden) -> Result<(), String> {
    let (dates, equity) = series(g)?;
    let out = drawdown_series(&dates, &equity).map_err(|e| e.to_string())?;
    g.check_output(&out)
}

fn run_monthly(g: &Golden) -> Result<(), String> {
    let (dates, equity) = series(g)?;
    let out = monthly_returns(&dates, &equity).map_err(|e| e.to_string())?;
    let rows: Vec<_> = out
        .iter()
        .map(|r| json!({"year": r.year, "month": r.month, "key": r.key(), "ret": r.ret}))
        .collect();
    g.check_output(&rows)
}

fn run_downsample(g: &Golden) -> Result<(), String> {
    let (dates, equity) = series(g)?;
    let benchmark: Vec<Option<Decimal>> = g.input_as("benchmark").map_err(e)?;
    let limit: Option<usize> = g.input_as("limit").map_err(e)?;
    let out = downsample_curve(
        &dates,
        &equity,
        &benchmark,
        limit.unwrap_or(EQUITY_CURVE_POINTS),
    )
    .map_err(|e| e.to_string())?;
    g.check_output(&out)
}

fn run_round_trips(g: &Golden) -> Result<(), String> {
    let trades: Vec<Trade> = g.input_as("trades").map_err(e)?;
    let out = round_trips(&trades).map_err(|e| e.to_string())?;
    let rows: Vec<_> = out
        .iter()
        .map(|t| {
            let mut v = serde_json::to_value(t).unwrap();
            v["is_win"] = json!(t.is_win());
            v
        })
        .collect();
    g.check_output(&rows)
}

fn run_rolling(g: &Golden) -> Result<(), String> {
    let (dates, equity) = series(g)?;
    let dist = rolling_distribution(&dates, &equity).map_err(|e| e.to_string())?;
    g.check_output(&json!({"raw": dist, "as_dict": dist.as_dict()}))
}

fn run_compute(g: &Golden) -> Result<(), String> {
    let result: BacktestOutcome = g.input_as("result").map_err(e)?;
    let metrics = compute_metrics(&result).map_err(|e| e.to_string())?;
    g.check_output(&metrics.as_dict())
}

fn run_hash(g: &Golden) -> Result<(), String> {
    let result: BacktestOutcome = g.input_as("result").map_err(e)?;
    let config: Json = serde_json::from_value(g.input("result").map_err(e)?["config"].clone())
        .map_err(|e| e.to_string())?;
    let version: Option<i64> = g.input_as("data_version").map_err(e)?;
    let metrics = compute_metrics(&result).map_err(|e| e.to_string())?;
    let hash = metrics_hash(&metrics, &config, version);
    g.check_output(&json!({"metrics": metrics.as_dict(), "hash": hash}))
}

fn grade(name: &str, run: fn(&Golden) -> Result<(), String>) -> usize {
    let fn_name = format!("{PREFIX}{name}");
    let cases: Vec<Golden> = baskfy_testkit::cases_for(&fn_name).unwrap();
    assert!(!cases.is_empty(), "no cases for {fn_name}");
    let mut failures = Vec::new();
    for g in &cases {
        if let Err(why) = run(g) {
            failures.push(format!(
                "{} (known_bug: {:?}):\n{why}",
                g.case, g.meta.known_bug
            ));
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} {fn_name} cases failed:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n\n")
    );
    cases.len()
}

#[test]
fn drawdown_series_matches_every_golden() {
    assert_eq!(grade("drawdown_series", run_drawdown), 5);
}

#[test]
fn monthly_returns_matches_every_golden() {
    assert_eq!(grade("monthly_returns", run_monthly), 5);
}

#[test]
fn downsample_curve_matches_every_golden() {
    assert_eq!(grade("downsample_curve", run_downsample), 5);
}

#[test]
fn round_trips_matches_every_golden() {
    assert_eq!(grade("round_trips", run_round_trips), 7);
}

#[test]
fn rolling_distribution_matches_every_golden() {
    assert_eq!(grade("rolling_distribution", run_rolling), 7);
}

#[test]
fn compute_metrics_matches_every_golden() {
    assert_eq!(grade("compute_metrics", run_compute), 9);
}

#[test]
fn metrics_hash_matches_every_golden() {
    assert_eq!(grade("metrics_hash", run_hash), 5);
}
