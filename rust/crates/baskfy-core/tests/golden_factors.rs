//! The factor engine against the goldens: `compute_factors` and `compute_factors_unrounded`
//! (`go/testdata/golden/L1/ranking/factors.compute_factors{,_unrounded}.case_NNN.json`).
//!
//! Every case is loaded and graded strictly by the testkit; none is filtered. A case whose
//! Python raised stores `{"raises": {"type": "ValueError", "message": …}}`, which the port
//! answers with `CoreError::InvalidInput(message)`. `compute_factors.case_005` and
//! `compute_factors_unrounded.case_003` carry `known_bug` (DECISIONS-RS R1.4, the regime
//! look-ahead): the port reproduces it.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_core::CoreError;
use baskfy_core::factors::{
    FactorConfig, FactorResult, compute_factors, compute_factors_unrounded,
};
use baskfy_testkit::{Golden, encode_frame};
use chrono::NaiveDate;
use polars::prelude::DataFrame;
use serde_json::{Map, Value, json};

const ROUNDED: &str = "baskfy_core.factors.compute_factors";
const UNROUNDED: &str = "baskfy_core.factors.compute_factors_unrounded";

type Compute = fn(
    &DataFrame,
    NaiveDate,
    &[NaiveDate],
    Option<&DataFrame>,
    &FactorConfig,
    Option<&DataFrame>,
) -> Result<FactorResult, CoreError>;

fn iso(d: NaiveDate) -> Value {
    Value::String(d.format("%Y-%m-%d").to_string())
}

/// golden_ranking.py's `_factor_result`.
fn encode(result: &FactorResult) -> Result<Value, String> {
    let mut windows = Map::new();
    for (months, w) in &result.windows {
        windows.insert(
            months.to_string(),
            json!({
                "months": w.months,
                "as_of": iso(w.as_of),
                "calendar_start": iso(w.calendar_start),
                "start": iso(w.start),
                "end": w.trading_days.last().map_or(Value::Null, |d| iso(*d)),
                "length": w.length(),
                "calendar_first": iso(w.calendar_first),
                "spans_full_window": w.spans_full_window(),
            }),
        );
    }
    let frame = encode_frame(&result.frame).map_err(|e| e.to_string())?;
    Ok(json!({"frame": frame, "windows": Value::Object(windows)}))
}

fn optional_frame(g: &Golden, key: &str) -> Result<Option<DataFrame>, String> {
    match g.input(key).map_err(|e| e.to_string())? {
        Value::Null => Ok(None),
        _ => g.input_frame(key).map(Some).map_err(|e| e.to_string()),
    }
}

fn run(g: &Golden, compute: Compute) -> Result<(), String> {
    let e = |e: baskfy_testkit::TestkitError| e.to_string();
    let bars = g.input_frame("bars").map_err(e)?;
    let as_of = g.input_date("as_of").map_err(e)?;
    let days: Vec<String> = g.input_as("trading_days").map_err(e)?;
    let trading_days = days
        .iter()
        .map(|d| NaiveDate::parse_from_str(d, "%Y-%m-%d").map_err(|x| x.to_string()))
        .collect::<Result<Vec<_>, _>>()?;
    let benchmark = optional_frame(g, "benchmark")?;
    let market = optional_frame(g, "market_benchmark")?;
    let config: FactorConfig = g.input_as("config").map_err(e)?;
    let actual = match compute(
        &bars,
        as_of,
        &trading_days,
        benchmark.as_ref(),
        &config,
        market.as_ref(),
    ) {
        Ok(result) => encode(&result)?,
        Err(CoreError::InvalidInput(message)) => {
            json!({"raises": {"type": "ValueError", "message": message}})
        }
        Err(other) => return Err(format!("unexpected error: {other}")),
    };
    g.check_value(&actual).map_err(|m| {
        format!(
            "{} mismatch(es) [known_bug: {:?}]\n{m}",
            m.0.len(),
            g.meta.known_bug
        )
    })
}

fn grade(fn_name: &str, compute: Compute) -> usize {
    let cases = baskfy_testkit::cases_for(fn_name).unwrap();
    let mut failures = Vec::new();
    for g in &cases {
        if let Err(why) = run(g, compute) {
            failures.push(format!("{}:\n{why}", g.case));
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
fn compute_factors_matches_every_golden() {
    let n = grade(ROUNDED, compute_factors);
    assert_eq!(n, 12, "compute_factors case count changed");
}

#[test]
fn compute_factors_unrounded_matches_every_golden() {
    let n = grade(UNROUNDED, compute_factors_unrounded);
    assert_eq!(n, 3, "compute_factors_unrounded case count changed");
}

#[test]
fn the_regime_look_ahead_is_reproduced_not_fixed() {
    // DECISIONS-RS R1.4: with bars after as_of, the as_of row's regime is NULL.
    for case in [
        "L1/ranking/factors.compute_factors.case_005",
        "L1/ranking/factors.compute_factors_unrounded.case_003",
    ] {
        let g = baskfy_testkit::load(case).unwrap();
        assert!(g.meta.known_bug.is_some(), "{case} lost its known_bug");
    }
}
