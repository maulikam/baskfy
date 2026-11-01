//! Grades the options ports against every golden in `go/testdata/golden/L1/options`:
//! `condor.exit_decision`, `costs.charges`, `execution.never_naked`, `execution.simulate_fill`,
//! `gating.options_gates` and `ledger.journal_figures`. Each case is called from its stored
//! inputs exactly as `tools/parity/golden_options.py` calls the Python function.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeMap;

use baskfy_core::options::condor::{
    CondorConfig, CondorTick, CondorVariant, RiskConfig, exit_decision,
};
use baskfy_core::options::costs::{CostFill, CostRates, Side, charges};
use baskfy_core::options::execution::{
    ExecutionConfig, LegRole, Level, never_naked, simulate_fill,
};
use baskfy_core::options::gating::{OptionsFlags, Sleeve, options_gates};
use baskfy_core::options::ledger::{JournalSession, LegFill, journal_figures};
use baskfy_testkit::{Golden, cases_for};

/// Runs every case, collecting failures so one report names them all; returns the count.
fn grade(fn_name: &str, run: impl Fn(&Golden) -> Result<(), String>) -> usize {
    let cases = cases_for(fn_name).unwrap();
    let failures: Vec<String> = cases
        .iter()
        .filter_map(|g| {
            run(g).err().map(|e| {
                let bug = g
                    .meta
                    .known_bug
                    .as_deref()
                    .map(|b| format!(" (known_bug: {b})"))
                    .unwrap_or_default();
                format!("{}{bug}: {e}", g.case)
            })
        })
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

#[test]
fn options_gates_matches_every_golden() {
    let n = grade("baskfy_core.options.gating.options_gates", |g| {
        let sleeve: Sleeve = g.input_as("sleeve").map_err(|e| e.to_string())?;
        let flags: OptionsFlags = g.input_as("flags").map_err(|e| e.to_string())?;
        g.check_output(&options_gates(sleeve, &flags))
    });
    assert_eq!(n, 20);
}

#[test]
fn charges_matches_every_golden() {
    let n = grade("baskfy_core.options.costs.charges", |g| {
        let fills: Vec<CostFill> = g.input_as("fills").map_err(|e| e.to_string())?;
        let rates: CostRates = g.input_as("rates").map_err(|e| e.to_string())?;
        let breakdown = charges(&fills, &rates).map_err(|e| e.to_string())?;
        let total = breakdown.total().map_err(|e| e.to_string())?;
        g.check_output(&serde_json::json!({ "breakdown": breakdown, "total": total }))
    });
    assert_eq!(n, 4);
}

#[test]
fn simulate_fill_matches_every_golden() {
    let n = grade("baskfy_core.options.execution.simulate_fill", |g| {
        let side: Side = g.input_as("side").map_err(|e| e.to_string())?;
        let ladder: Vec<Level> = g.input_as("ladder").map_err(|e| e.to_string())?;
        let quantity: i64 = g.input_as("quantity").map_err(|e| e.to_string())?;
        let limit = g.input_decimal("limit_price").map_err(|e| e.to_string())?;
        let tick = g.input_decimal("tick").map_err(|e| e.to_string())?;
        let config: ExecutionConfig = g.input_as("config").map_err(|e| e.to_string())?;
        let fill = simulate_fill(side, &ladder, quantity, limit, tick, &config)
            .map_err(|e| e.to_string())?;
        g.check_output(&fill)
    });
    assert_eq!(n, 6);
}

#[test]
fn never_naked_matches_every_golden() {
    let n = grade("baskfy_core.options.execution.never_naked", |g| {
        let position: BTreeMap<LegRole, i64> = g.input_as("position").map_err(|e| e.to_string())?;
        g.check_output(&never_naked(&position))
    });
    assert_eq!(n, 6);
}

#[test]
fn exit_decision_matches_every_golden() {
    let n = grade("baskfy_core.options.condor.exit_decision", |g| {
        let variant: CondorVariant = g.input_as("variant").map_err(|e| e.to_string())?;
        let tick: CondorTick = g.inputs_as().map_err(|e| e.to_string())?;
        let config = CondorConfig::for_variant(variant);
        let decision =
            exit_decision(&tick, &config, &RiskConfig::default()).map_err(|e| e.to_string())?;
        g.check_output(&decision)
    });
    assert_eq!(n, 8);
}

#[test]
fn journal_figures_matches_every_golden() {
    let n = grade("baskfy_core.options.ledger.journal_figures", |g| {
        let fills: Vec<LegFill> = g.input_as("fills").map_err(|e| e.to_string())?;
        let session: JournalSession = g.inputs_as().map_err(|e| e.to_string())?;
        let rates: CostRates = g.input_as("rates").map_err(|e| e.to_string())?;
        let figures = journal_figures(&fills, &session, &rates).map_err(|e| e.to_string())?;
        g.check_output(&figures)
    });
    assert_eq!(n, 3);
}

/// The defaults the goldens write into their inputs are the Rust defaults too, so a caller
/// that takes `CostRates::default()` / `ExecutionConfig::default()` prices like Python's
/// `OptionsConfig()`.
#[test]
fn config_defaults_match_the_goldens_inputs() {
    let charges_case = cases_for("baskfy_core.options.costs.charges").unwrap();
    let rates: CostRates = charges_case[0].input_as("rates").unwrap();
    assert_eq!(rates, CostRates::default());
    let fill_case = cases_for("baskfy_core.options.execution.simulate_fill").unwrap();
    let config: ExecutionConfig = fill_case[0].input_as("config").unwrap();
    assert_eq!(config, ExecutionConfig::default());
}
