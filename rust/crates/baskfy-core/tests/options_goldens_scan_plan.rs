//! Grades the options scan and the three plan builders against every golden in
//! `go/testdata/golden/L1/options`: `scan.scan_all`, `plan.decide_o1` (+ `finalize_o1`),
//! `plan_o2.decide_o2` (+ `finalize_o2`) and `plan_o3.decide_o3` (+ `finalize_o3`). Each case is
//! called from its stored inputs exactly as `tools/parity/golden_options.py` calls Python.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeMap;

use baskfy_core::options::config::{Mode, OptionsCeilings, OptionsConfig, Sleeve};
use baskfy_core::options::plan::{Finalize, MarginQuote, decide_o1, finalize_o1};
use baskfy_core::options::plan_o2::{decide_o2, finalize_o2};
use baskfy_core::options::plan_o3::{decide_o3, finalize_o3};
use baskfy_core::options::scan::{DayContext, MarketDay, Snapshot, SnapshotBook, scan_all};
use baskfy_core::options::structures::{Candidate, to_json};
use baskfy_testkit::{Golden, cases_for};
use chrono::{DateTime, FixedOffset};
use serde::Deserialize;
use serde_json::{Value, json};

/// Runs every case, collecting failures so one report names them all; returns the count.
fn grade(fn_name: &str, run: impl Fn(&Golden) -> Result<(), String>) -> usize {
    let cases = cases_for(fn_name).unwrap();
    let failures: Vec<String> = cases
        .iter()
        .filter_map(|g| run(g).err().map(|e| format!("{}: {e}", g.case)))
        .collect();
    assert!(
        failures.is_empty(),
        "{fn_name}: {} of {} cases failed:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
    cases.len()
}

fn err(e: impl std::fmt::Display) -> String {
    e.to_string()
}

#[derive(Deserialize)]
struct BookEntry {
    minute: Option<DateTime<FixedOffset>>,
    snapshot: Option<Snapshot>,
}

/// The common inputs of every case here.
struct Day {
    market: MarketDay,
    context: DayContext,
    now: DateTime<FixedOffset>,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
}

fn day(g: &Golden) -> Result<Day, String> {
    Ok(Day {
        market: g.input_as("market").map_err(err)?,
        context: g.input_as("context").map_err(err)?,
        now: g.input_as("now").map_err(err)?,
        options: g.input_as("options").map_err(err)?,
        ceilings: g.input_as("ceilings").map_err(err)?,
    })
}

fn book(g: &Golden) -> Result<SnapshotBook, String> {
    let entries: Vec<BookEntry> = g.input_as("snapshots").map_err(err)?;
    Ok(SnapshotBook::new(
        entries
            .into_iter()
            .map(|e| (e.minute, e.snapshot))
            .collect::<BTreeMap<_, _>>(),
    ))
}

fn broker(g: &Golden) -> Result<(Option<MarginQuote>, Finalize), String> {
    let margin: Option<MarginQuote> = g.input_as("margin").map_err(err)?;
    let inputs = Finalize {
        user_id: g.input_as("user_id").map_err(err)?,
        mode: g.input_as("mode").map_err(err)?,
        margin_pool_inr: g.input_decimal("margin_pool_inr").map_err(err)?,
        margin_in_use_inr: g.input_decimal("margin_in_use_inr").map_err(err)?,
    };
    Ok((margin, inputs))
}

/// A decision as Python's `_decision_json`: every field, the candidate as `to_json`.
fn decision_json(
    decision: &impl serde::Serialize,
    candidate: Option<&Candidate>,
) -> Result<Value, String> {
    let mut v = serde_json::to_value(decision).map_err(err)?;
    let c = candidate.map(to_json).transpose().map_err(err)?;
    v["candidate"] = serde_json::to_value(c).map_err(err)?;
    Ok(v)
}

/// An outcome as Python's `_outcome_json`.
fn outcome_json(
    state: &impl serde::Serialize,
    reasons: &[String],
    plan: &impl serde::Serialize,
    decision: &Value,
) -> Value {
    json!({
        "state": state,
        "reasons": reasons,
        "plan": plan,
        "decision_state": decision["state"],
        "decision_reasons": decision["reasons"],
        "decision_numbers": decision["numbers"],
    })
}

#[test]
fn scan_all_matches_every_golden() {
    let n = grade("baskfy_core.options.scan.scan_all", |g| {
        let d = day(g)?;
        let mut snapshots = book(g)?;
        let rows = scan_all(
            &d.market,
            &d.context,
            &d.now,
            &mut snapshots,
            &d.options,
            &d.ceilings,
        )
        .map_err(err)?;
        let out = rows
            .iter()
            .map(|r| {
                Ok(json!({
                    "sleeve": r.sleeve,
                    "ts": r.ts,
                    "state": r.state,
                    "reasons": r.reasons,
                    "numbers": r.numbers,
                    "candidates": r.candidates_json().map_err(err)?,
                    "as_of_minute": r.as_of_minute,
                    "stale": r.stale,
                }))
            })
            .collect::<Result<Vec<Value>, String>>()?;
        g.check_output(&out)
    });
    assert_eq!(n, 21);
}

#[test]
fn decide_o1_matches_every_golden() {
    let n = grade("baskfy_core.options.plan.decide_o1", |g| {
        let d = day(g)?;
        let sleeve: Sleeve = g.input_as("sleeve").map_err(err)?;
        let snapshot: Option<Snapshot> = g.input_as("snapshot").map_err(err)?;
        let (margin, inputs) = broker(g)?;
        let decision = decide_o1(
            sleeve,
            &d.market,
            &d.context,
            snapshot.as_ref(),
            &d.now,
            &d.options,
            &d.ceilings,
        )
        .map_err(err)?;
        let outcome = finalize_o1(&decision, margin.as_ref(), &inputs, &d.options).map_err(err)?;
        let after = decision_json(&outcome.decision, outcome.decision.candidate.as_ref())?;
        g.check_output(&json!({
            "decision": decision_json(&decision, decision.candidate.as_ref())?,
            "outcome": outcome_json(&outcome.state, &outcome.reasons, &outcome.plan, &after),
        }))
    });
    assert_eq!(n, 17);
}

#[test]
fn decide_o2_matches_every_golden() {
    let n = grade("baskfy_core.options.plan_o2.decide_o2", |g| {
        let d = day(g)?;
        let mut snapshots = book(g)?;
        let user_id: i64 = g.input_as("user_id").map_err(err)?;
        let mode: Mode = g.input_as("mode").map_err(err)?;
        let decision = decide_o2(
            &d.market,
            &d.context,
            &mut snapshots,
            &d.now,
            &d.options,
            &d.ceilings,
        )
        .map_err(err)?;
        let outcome = finalize_o2(&decision, user_id, mode, &d.options).map_err(err)?;
        let after = decision_json(&outcome.decision, outcome.decision.candidate.as_ref())?;
        g.check_output(&json!({
            "decision": decision_json(&decision, decision.candidate.as_ref())?,
            "outcome": outcome_json(&outcome.state, &outcome.reasons, &outcome.plan, &after),
        }))
    });
    assert_eq!(n, 14);
}

#[test]
fn decide_o3_matches_every_golden() {
    let n = grade("baskfy_core.options.plan_o3.decide_o3", |g| {
        let d = day(g)?;
        let sleeve: Sleeve = g.input_as("sleeve").map_err(err)?;
        let mut snapshots = book(g)?;
        let (margin, inputs) = broker(g)?;
        let decision = decide_o3(
            sleeve,
            &d.market,
            &d.context,
            &mut snapshots,
            &d.now,
            &d.options,
            &d.ceilings,
        )
        .map_err(err)?;
        let outcome = finalize_o3(&decision, margin.as_ref(), &inputs, &d.options).map_err(err)?;
        let after = decision_json(&outcome.decision, outcome.decision.candidate.as_ref())?;
        g.check_output(&json!({
            "decision": decision_json(&decision, decision.candidate.as_ref())?,
            "outcome": outcome_json(&outcome.state, &outcome.reasons, &outcome.plan, &after),
        }))
    });
    assert_eq!(n, 14);
}

/// The rules every case writes into its inputs are the Rust defaults too.
#[test]
fn rules_in_the_inputs_are_the_defaults() {
    let g = &cases_for("baskfy_core.options.scan.scan_all").unwrap()[0];
    let options: OptionsConfig = g.input_as("options").unwrap();
    let ceilings: OptionsCeilings = g.input_as("ceilings").unwrap();
    assert_eq!(options, OptionsConfig::default());
    assert_eq!(ceilings, OptionsCeilings::default());
}
