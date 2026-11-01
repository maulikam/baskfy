#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
//! TWT lane B against the goldens (`go/testdata/golden/L1/twt`): `exits`, `plan` and
//! `backtest`.
//!
//! Each case is decoded exactly as `tools/parity/golden_twt.py`'s `call_*` decodes it, the Rust
//! port is called, and the result is encoded the way the dumper encodes the Python result (a
//! raise as `{"raises": {"type", "message"}}`). Every case of every function is graded strictly
//! and the per-function case count is asserted, so a dropped case fails.

use baskfy_core::CoreError;
use baskfy_core::twt::backtest::{
    BacktestParams, BacktestResult, Panel, gate_vector, panel_from_frame, run_backtest, summarise,
    yearly,
};
use baskfy_core::twt::config::{ExitConfig, Gate, TwtConfig};
use baskfy_core::twt::exits::{
    Bar, OpenPosition, clamp_under_close, fill_day_stop, initial_stop, manage, on_adjustment,
    r_multiple, ratchet, stop_fill, stop_is_below_entry, tick, tick_floor, trail_from,
};
use baskfy_core::twt::plan::{
    BookState, Candidate, LineKind, PlanLine, Skipped, assemble, build_entries, client_id_for,
    exit_lines, gtt_limit_price, stop_for,
};
use baskfy_testkit::{Golden, cases_for, to_golden_value};
use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde_json::{Value, json};

const MODULE: &str = "baskfy_core.twt.";

fn raises(e: &CoreError) -> Value {
    let (kind, message) = match e {
        CoreError::InvalidInput(m) => ("ValueError", m.clone()),
        other => ("CoreError", other.to_string()),
    };
    json!({"raises": {"type": kind, "message": message}})
}

fn value<T: serde::Serialize>(v: &T) -> Value {
    to_golden_value(v).unwrap()
}

fn result<T: serde::Serialize>(r: Result<T, CoreError>) -> Value {
    match r {
        Ok(v) => value(&v),
        Err(e) => raises(&e),
    }
}

fn twt_config(g: &Golden) -> TwtConfig {
    match g.input("config").unwrap() {
        Value::Null => TwtConfig::default(),
        _ => g.input_as("config").unwrap(),
    }
}

fn exit_config(g: &Golden) -> ExitConfig {
    g.input_as("config").unwrap()
}

fn d(g: &Golden, key: &str) -> Decimal {
    g.input_decimal(key).unwrap()
}

fn opt_d(g: &Golden, key: &str) -> Option<Decimal> {
    g.input_opt_decimal(key).unwrap()
}

fn position(g: &Golden) -> OpenPosition {
    g.input_as("position").unwrap()
}

fn bar(g: &Golden) -> Bar {
    g.input_as("bar").unwrap()
}

fn grader(fn_name: &str) -> (usize, Grader) {
    TABLE
        .iter()
        .find(|(name, _, _)| *name == fn_name)
        .map(|(_, n, f)| (*n, *f))
        .unwrap_or_else(|| panic!("no grader for {fn_name}"))
}

fn grade_all(cases: &[Golden], grade: Grader) -> Vec<String> {
    let mut failures = Vec::new();
    for g in cases {
        let actual = grade(g);
        if let Err(m) = g.check_value(&actual) {
            failures.push(format!(
                "{} ({}): {} mismatch(es)\n{m}",
                g.case,
                g.fn_name,
                m.0.len()
            ));
        }
    }
    failures
}

/// Grades every golden case of `fn_name`, asserts the count, and fails with every mismatch at
/// once.
fn run(fn_name: &str) {
    let full = format!("{MODULE}{fn_name}");
    let (expected, grade) = grader(fn_name);
    let cases = cases_for(&full).unwrap();
    assert_eq!(cases.len(), expected, "{full}: case count");
    let failures = grade_all(&cases, grade);
    assert!(failures.is_empty(), "{}", failures.join("\n\n"));
}

/// Differential fuzz against the live Python: `TWT_B_FUZZ_DIR` holds golden-shaped cases
/// written by a generator that calls the dumper's own `CALLS` (never into `go/testdata`).
/// Ignored by default; run with `TWT_B_FUZZ_DIR=... cargo test --test golden_twt_b --
/// --ignored`.
#[test]
#[ignore = "needs TWT_B_FUZZ_DIR from the differential generator"]
fn differential_fuzz() {
    let dir = std::env::var("TWT_B_FUZZ_DIR").expect("TWT_B_FUZZ_DIR");
    let mut paths: Vec<_> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    paths.sort();
    let mut failures = Vec::new();
    for p in &paths {
        let g = baskfy_testkit::load(p.to_str().unwrap()).unwrap();
        let name = g.fn_name.strip_prefix(MODULE).unwrap().to_owned();
        let (_, grade) = grader(&name);
        failures.extend(grade_all(std::slice::from_ref(&g), grade));
    }
    assert!(!paths.is_empty(), "no fuzz cases in {dir}");
    assert!(
        failures.is_empty(),
        "{} of {} fuzz cases differ:\n{}",
        failures.len(),
        paths.len(),
        failures.join("\n\n")
    );
}

/// Every graded function: (name under `baskfy_core.twt.`, golden case count, grader).
type Grader = fn(&Golden) -> Value;

const TABLE: &[(&str, usize, Grader)] = &[
    // -- exits ---------------------------------------------------------------------------------
    ("exits.tick", 3, |g| {
        let raw: Option<String> = g.input_as("tick_inr").unwrap();
        result(tick(raw.as_deref()))
    }),
    ("exits.tick_floor", 7, |g| {
        result(tick_floor(d(g, "price"), d(g, "tick_size")))
    }),
    ("exits.initial_stop", 6, |g| {
        result(initial_stop(
            d(g, "fill_price"),
            &exit_config(g),
            d(g, "tick_size"),
        ))
    }),
    ("exits.stop_is_below_entry", 6, |g| {
        value(&stop_is_below_entry(d(g, "fill_price"), d(g, "stop")))
    }),
    ("exits.trail_from", 3, |g| {
        result(trail_from(d(g, "high_since"), &exit_config(g)))
    }),
    ("exits.clamp_under_close", 7, |g| {
        result(clamp_under_close(
            d(g, "raw_trigger"),
            d(g, "close"),
            &exit_config(g),
            d(g, "tick_size"),
        ))
    }),
    ("exits.ratchet", 8, |g| {
        result(ratchet(
            &position(g),
            &bar(g),
            &exit_config(g),
            d(g, "tick_size"),
        ))
    }),
    ("exits.on_adjustment", 5, |g| {
        result(on_adjustment(
            &position(g),
            d(g, "rederived_high_since"),
            &bar(g),
            &exit_config(g),
            d(g, "tick_size"),
        ))
    }),
    ("exits.stop_fill", 7, |g| {
        result(stop_fill(&bar(g), d(g, "stop")))
    }),
    ("exits.fill_day_stop", 6, |g| {
        result(fill_day_stop(&position(g), &bar(g)))
    }),
    ("exits.manage", 9, |g| {
        let blank: i64 = g.input_as("blank_sessions").unwrap();
        result(manage(&position(g), &bar(g), blank, &exit_config(g)))
    }),
    ("exits.r_multiple", 7, |g| {
        result(r_multiple(
            d(g, "entry"),
            d(g, "initial"),
            d(g, "exit_price"),
        ))
    }),
    // -- plan ----------------------------------------------------------------------------------
    ("plan.client_id_for", 4, |g| {
        let plan_id: String = g.input_as("plan_id").unwrap();
        let symbol: String = g.input_as("symbol").unwrap();
        let kind: LineKind = g.input_as("kind").unwrap();
        value(&client_id_for(&plan_id, &symbol, kind))
    }),
    ("plan.stop_for", 3, |g| {
        result(stop_for(
            d(g, "reference_price"),
            &twt_config(g),
            d(g, "tick"),
        ))
    }),
    ("plan.gtt_limit_price", 4, |g| {
        result(gtt_limit_price(
            d(g, "trigger"),
            &twt_config(g),
            opt_d(g, "tick"),
        ))
    }),
    ("plan.build_entries", 19, |g| {
        let candidates: Vec<Candidate> = g.input_as("candidates").unwrap();
        let gate: Gate = g.input_as("gate").unwrap();
        let book: BookState = g.input_as("book").unwrap();
        let max_open: Option<i64> = g.input_as("max_open_positions").unwrap();
        match build_entries(
            &candidates,
            gate,
            d(g, "equity"),
            &book,
            &twt_config(g),
            d(g, "slot_multiplier"),
            max_open,
            opt_d(g, "tick"),
        ) {
            Ok((lines, skips)) => json!({"lines": value(&lines), "skips": value(&skips)}),
            Err(e) => raises(&e),
        }
    }),
    ("plan.exit_lines", 5, |g| {
        let book: BookState = g.input_as("book").unwrap();
        value(&exit_lines(&book, g.input_date("session").unwrap()))
    }),
    ("plan.assemble", 4, |g| {
        let exits: Vec<PlanLine> = g.input_as("exits").unwrap();
        let entries: Vec<PlanLine> = g.input_as("entries").unwrap();
        let skips: Vec<Skipped> = g.input_as("skips").unwrap();
        let source: String = g.input_as("source").unwrap();
        let gate: Gate = g.input_as("gate").unwrap();
        let plan = assemble(
            g.input_date("session").unwrap(),
            gate,
            d(g, "equity"),
            &exits,
            &entries,
            &skips,
            &source,
        );
        let canonical: Vec<_> = plan.lines.iter().map(PlanLine::canonical).collect();
        json!({
            "plan": value(&plan),
            "derived": {
                "entries": value(&plan.entries()),
                "total_new_exposure_inr": value(&plan.total_new_exposure_inr().unwrap()),
                "plan_hash": plan.plan_hash(),
                "canonical": value(&canonical),
            },
        })
    }),
    // -- backtest ------------------------------------------------------------------------------
    ("backtest.panel_from_frame", 5, |g| {
        let signal: String = g.input_as("signal_column").unwrap();
        result(panel_from_frame(
            &g.input_frame("indicated").unwrap(),
            &signal,
        ))
    }),
    ("backtest.gate_vector", 4, |g| {
        let sessions: Vec<NaiveDate> = g.input_as("sessions").unwrap();
        result(gate_vector(&g.input_frame("breadth").unwrap(), &sessions))
    }),
    ("backtest.run_backtest", 15, |g| {
        let panel: Panel = g.input_as("panel").unwrap();
        let gate: Vec<bool> = g.input_as("gate_open").unwrap();
        let params: BacktestParams = g.input_as("params").unwrap();
        result(run_backtest(&panel, &gate, &params))
    }),
    ("backtest.summarise", 11, |g| {
        let r: BacktestResult = g.input_as("result").unwrap();
        result(summarise(&r))
    }),
    ("backtest.yearly", 6, |g| {
        let r: BacktestResult = g.input_as("result").unwrap();
        result(yearly(&r))
    }),
];

macro_rules! golden_tests {
    ($($test:ident => $name:literal),* $(,)?) => {
        $(
            #[test]
            fn $test() {
                run($name);
            }
        )*
    };
}

golden_tests! {
    exits_tick => "exits.tick",
    exits_tick_floor => "exits.tick_floor",
    exits_initial_stop => "exits.initial_stop",
    exits_stop_is_below_entry => "exits.stop_is_below_entry",
    exits_trail_from => "exits.trail_from",
    exits_clamp_under_close => "exits.clamp_under_close",
    exits_ratchet => "exits.ratchet",
    exits_on_adjustment => "exits.on_adjustment",
    exits_stop_fill => "exits.stop_fill",
    exits_fill_day_stop => "exits.fill_day_stop",
    exits_manage => "exits.manage",
    exits_r_multiple => "exits.r_multiple",
    plan_client_id_for => "plan.client_id_for",
    plan_stop_for => "plan.stop_for",
    plan_gtt_limit_price => "plan.gtt_limit_price",
    plan_build_entries => "plan.build_entries",
    plan_exit_lines => "plan.exit_lines",
    plan_assemble => "plan.assemble",
    backtest_panel_from_frame => "backtest.panel_from_frame",
    backtest_gate_vector => "backtest.gate_vector",
    backtest_run_backtest => "backtest.run_backtest",
    backtest_summarise => "backtest.summarise",
    backtest_yearly => "backtest.yearly",
}

/// Every function of the three modules that has goldens is graded here: no golden file for
/// `exits`, `plan` or `backtest` goes ungraded.
#[test]
fn every_lane_b_golden_is_graded() {
    let mut total = 0;
    let cases = baskfy_testkit::all_cases().unwrap();
    for module in ["exits", "plan", "backtest"] {
        for case in &cases {
            if let Some(rest) = case.fn_name.strip_prefix(&format!("{MODULE}{module}.")) {
                let name = format!("{module}.{rest}");
                assert!(
                    TABLE.iter().any(|(n, _, _)| *n == name),
                    "ungraded golden function {name}"
                );
                total += 1;
            }
        }
    }
    let expected: usize = TABLE.iter().map(|(_, n, _)| n).sum();
    assert_eq!(total, expected);
}
