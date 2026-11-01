#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
//! `baskfy_core.ranking_engine` against the goldens
//! (`go/testdata/golden/L1/ranking/ranking_engine.*.case_NNN.json`).
//!
//! Every case for each function is loaded and graded strictly by the testkit, and each
//! function's case count is asserted so a dropped golden fails loudly. A Python raise is graded
//! as `{"raises": {"type", "message"}}`, exactly as `tools/parity/golden_ranking.py` stores it.
//!
//! The harness mirrors the dumper: a spec is built from JSON (each `TermSpec` validated, then
//! the `RankingSpec`); a series input is `_float_series` (`null` → NaN); frames are pandas
//! frames. Pandas dtype *names* are representation, not data, so the harness supplies them when
//! re-encoding the port's polars frames: an input column keeps the dtype pandas inferred for it
//! (a boolean column holding `None` is `object` in pandas, which the testkit's inferred name
//! cannot know), `sector` becomes `object` under `within_sector` (the engine assigns an object
//! series over it), `excluded_reason` is `object`, `rank` is the nullable `Int64`, and every
//! other new column takes the default name of its polars dtype (`bool`, `float64`).

use std::collections::BTreeMap;

use baskfy_core::ranking_engine::{
    ExplainContext, RankingError, RankingSpec, Scope, TermSpec, add_nse_momentum_score,
    effective_weights, explain, explain_instrument, family_shares, percentile, rank_frame,
    rank_history, target_distance, target_score, transform,
};
use baskfy_testkit::{Golden, PandasFrame, encode_pandas_frame, pandas_dtype_name};
use chrono::NaiveDate;
use polars::prelude::{DataFrame, DataType};
use serde_json::{Map, Value, json};

const P: &str = "baskfy_core.ranking_engine.";

fn raises(e: &RankingError) -> Value {
    json!({"raises": {"type": e.python_type(), "message": e.message()}})
}

fn capture(result: Result<Value, RankingError>) -> Value {
    result.unwrap_or_else(|e| raises(&e))
}

fn spec_of(v: &Value) -> Result<RankingSpec, RankingError> {
    let spec: RankingSpec = serde_json::from_value(v.clone()).expect("spec decodes");
    spec.validate()?;
    Ok(spec)
}

fn floats(v: &Value) -> Vec<Option<f64>> {
    v.as_array()
        .expect("a list of floats")
        .iter()
        .map(Value::as_f64)
        .collect()
}

fn opt_f64(v: &Value) -> Option<f64> {
    v.as_f64()
}

fn series(values: &[Option<f64>]) -> Value {
    json!({
        "dtype": "float64",
        "index": (0..values.len()).collect::<Vec<_>>(),
        "values": values,
    })
}

fn pairs(pairs: &[(String, f64)]) -> Value {
    Value::Object(
        pairs
            .iter()
            .map(|(k, v)| (k.clone(), json!(v)))
            .collect::<Map<String, Value>>(),
    )
}

/// The pandas dtype of each input column (see the module docs).
fn input_dtypes(pf: &PandasFrame) -> Vec<(String, String)> {
    pf.dtypes
        .iter()
        .map(|(name, dtype)| {
            let column = pf.frame.column(name).expect("column");
            let fixed = if column.dtype() == &DataType::Boolean && column.null_count() > 0 {
                "object".to_owned()
            } else {
                dtype.clone()
            };
            (name.clone(), fixed)
        })
        .collect()
}

fn output_dtypes(
    input: &[(String, String)],
    df: &DataFrame,
    within_sector: bool,
) -> Vec<(String, String)> {
    df.columns()
        .iter()
        .map(|c| {
            let name = c.name().to_string();
            let dtype = if name == "sector" && within_sector {
                "object".to_owned()
            } else if let Some((_, d)) = input.iter().find(|(n, _)| *n == name) {
                d.clone()
            } else {
                match name.as_str() {
                    "excluded_reason" => "object".to_owned(),
                    "rank" => "Int64".to_owned(),
                    _ => pandas_dtype_name(c.dtype()).to_owned(),
                }
            };
            (name, dtype)
        })
        .collect()
}

fn encode(df: &DataFrame, input: &[(String, String)], within_sector: bool) -> Value {
    let index: Vec<Value> = (0..df.height()).map(Value::from).collect();
    encode_pandas_frame(df, &index, &output_dtypes(input, df, within_sector)).expect("encodes")
}

// ---------------------------------------------------------------------------
// Runners
// ---------------------------------------------------------------------------

fn run_family_shares(g: &Golden) -> Value {
    capture(spec_of(g.input("spec").unwrap()).and_then(|s| family_shares(&s).map(|p| pairs(&p))))
}

fn run_effective_weights(g: &Golden) -> Value {
    capture(
        spec_of(g.input("spec").unwrap()).and_then(|s| effective_weights(&s).map(|p| pairs(&p))),
    )
}

fn run_percentile(g: &Golden) -> Value {
    series(&percentile(&floats(g.input("values").unwrap())))
}

fn run_target_distance(g: &Golden) -> Value {
    series(&target_distance(
        &floats(g.input("values").unwrap()),
        opt_f64(g.input("lower").unwrap()),
        opt_f64(g.input("upper").unwrap()),
    ))
}

fn run_target_score(g: &Golden) -> Value {
    series(&target_score(&floats(g.input("distances").unwrap())))
}

fn run_transform(g: &Golden) -> Value {
    let term: TermSpec = g.input_as("term").unwrap();
    capture(
        term.validate()
            .map(|()| series(&transform(&floats(g.input("values").unwrap()), &term))),
    )
}

fn run_add_nse(g: &Golden) -> Value {
    let pf = g.input_pandas_frame("frame").unwrap();
    let dtypes = input_dtypes(&pf);
    capture(add_nse_momentum_score(&pf.frame).map(|df| encode(&df, &dtypes, false)))
}

fn run_rank_frame(g: &Golden) -> Value {
    let pf = g.input_pandas_frame("frame").unwrap();
    let dtypes = input_dtypes(&pf);
    capture(spec_of(g.input("spec").unwrap()).and_then(|spec| {
        let within = spec.scope == Scope::WithinSector;
        let result = rank_frame(&pf.frame, &spec)?;
        Ok(json!({
            "ranked": encode(&result.ranked, &dtypes, within),
            "scored": encode(&result.scored, &dtypes, within),
            "weights": pairs(&result.weights),
        }))
    }))
}

fn run_rank_history(g: &Golden) -> Value {
    let today: Option<i64> = g.input_as("today").unwrap();
    let previous: Option<i64> = g.input_as("previous").unwrap();
    let prev: Option<NaiveDate> = g.input_as("previous_as_of").unwrap();
    serde_json::to_value(rank_history(today, previous, prev)).unwrap()
}

fn date(v: &Value) -> Option<NaiveDate> {
    v.as_str()
        .map(|s| NaiveDate::parse_from_str(s, "%Y-%m-%d").expect("an ISO date"))
}

/// `_context`: rank the frame, then build the `ExplainContext`; `then` explains with it.
fn with_context(
    g: &Golden,
    then: impl Fn(&ExplainContext<'_>) -> Result<Value, RankingError>,
) -> Value {
    let pf = g.input_pandas_frame("frame").unwrap();
    let c = g.input("context").unwrap();
    capture(spec_of(g.input("spec").unwrap()).and_then(|spec| {
        let result = rank_frame(&pf.frame, &spec)?;
        let clause_details: BTreeMap<String, String> = c
            .get("clause_details")
            .and_then(Value::as_object)
            .map(|m| {
                m.iter()
                    .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_owned()))
                    .collect()
            })
            .unwrap_or_default();
        let context = ExplainContext {
            result: &result,
            universe: c["universe"].as_str().unwrap().to_owned(),
            as_of: date(&c["as_of"]).unwrap(),
            data_version: c.get("data_version").and_then(Value::as_i64),
            desk_score_version: c
                .get("desk_score_version")
                .and_then(Value::as_str)
                .map(str::to_owned),
            clause_details,
            previous_as_of: c.get("previous_as_of").and_then(date),
            previous_rank: c.get("previous_rank").and_then(Value::as_i64),
        };
        then(&context)
    }))
}

fn run_explain(g: &Golden) -> Value {
    let position = usize::try_from(g.input("row_position").unwrap().as_i64().unwrap()).unwrap();
    with_context(g, |ctx| {
        explain(position, ctx).map(|e| serde_json::to_value(e).unwrap())
    })
}

fn run_explain_instrument(g: &Golden) -> Value {
    let id = g.input("instrument_id").unwrap().as_i64().unwrap();
    with_context(g, |ctx| {
        explain_instrument(id, ctx).map(|e| serde_json::to_value(e).unwrap())
    })
}

// ---------------------------------------------------------------------------
// Grading
// ---------------------------------------------------------------------------

fn grade(function: &str, run: fn(&Golden) -> Value) -> usize {
    let name = format!("{P}{function}");
    let cases: Vec<Golden> = baskfy_testkit::cases_for(&name).unwrap();
    assert!(!cases.is_empty(), "no cases for {name}");
    let mut failures = Vec::new();
    for g in &cases {
        let actual = run(g);
        if let Err(why) = g.check_value(&actual) {
            failures.push(format!(
                "{} (known_bug: {:?}):\n{why}",
                g.case, g.meta.known_bug
            ));
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} {name} cases failed:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n\n")
    );
    cases.len()
}

#[test]
fn family_shares_matches_every_golden() {
    assert_eq!(grade("family_shares", run_family_shares), 8);
}

#[test]
fn effective_weights_matches_every_golden() {
    assert_eq!(grade("effective_weights", run_effective_weights), 8);
}

#[test]
fn percentile_matches_every_golden() {
    assert_eq!(grade("percentile", run_percentile), 8);
}

#[test]
fn target_distance_matches_every_golden() {
    assert_eq!(grade("target_distance", run_target_distance), 5);
}

#[test]
fn target_score_matches_every_golden() {
    assert_eq!(grade("target_score", run_target_score), 5);
}

#[test]
fn transform_matches_every_golden() {
    assert_eq!(grade("transform", run_transform), 7);
}

#[test]
fn add_nse_momentum_score_matches_every_golden() {
    assert_eq!(grade("add_nse_momentum_score", run_add_nse), 6);
}

#[test]
fn rank_frame_matches_every_golden() {
    assert_eq!(grade("rank_frame", run_rank_frame), 15);
}

#[test]
fn rank_history_matches_every_golden() {
    assert_eq!(grade("rank_history", run_rank_history), 6);
}

#[test]
fn explain_matches_every_golden() {
    assert_eq!(grade("explain", run_explain), 13);
}

#[test]
fn explain_instrument_matches_every_golden() {
    assert_eq!(grade("explain_instrument", run_explain_instrument), 4);
}

#[test]
fn every_ranking_engine_golden_is_graded() {
    let all = baskfy_testkit::all_cases().unwrap();
    let ours = all.iter().filter(|g| g.fn_name.starts_with(P)).count();
    assert_eq!(ours, 85, "ranking_engine golden count changed");
}
