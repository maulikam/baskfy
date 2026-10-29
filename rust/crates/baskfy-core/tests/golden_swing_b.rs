#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
//! Swing area B against the goldens: `detect_setups` and `build_entries`
//! (`go/testdata/golden/L1/swing/{detect_setups,build_entries}.case_NNN.json`).
//!
//! Every case for each function is loaded and graded strictly by the testkit. One case is
//! held apart: `build_entries.case_010` was dumped on 3 Sep 2026, before LV10 (669cf4b,
//! Maulik's pyramiding decision of 28 Sep 2026) made a held name a fresh entry up to
//! `max_entries_per_name`. The live Python gives a BUY line for `HELD`; the golden still says
//! `ALREADY_HELD`. The port follows the decision, so that case's own test fails until the
//! golden is regenerated — it is not filtered out of the grading.

use baskfy_core::swing::market::{ExposureTier, MarketGate};
use baskfy_core::swing::plan::{SwingAccount, WatchItem, build_entries};
use baskfy_core::swing::setups::{SwingConfig, detect_setups, with_swing_indicators};
use baskfy_testkit::Golden;

const DETECT: &str = "baskfy_core.swing.setups.detect_setups";
const BUILD: &str = "baskfy_core.swing.plan.build_entries";
/// Stale against LV10 (see the module docs).
const STALE_BUILD_CASE: &str = "build_entries.case_010";

fn is_stale(g: &Golden) -> bool {
    g.case.contains(STALE_BUILD_CASE)
}

fn run_detect(g: &Golden) -> Result<(), String> {
    let config: SwingConfig = g.input_as("config").map_err(|e| e.to_string())?;
    let bars = g.input_frame("bars").map_err(|e| e.to_string())?;
    let as_of = g.input_date("as_of").map_err(|e| e.to_string())?;
    let indicated = with_swing_indicators(&bars, &config).map_err(|e| e.to_string())?;
    let out = detect_setups(&indicated, as_of, &config).map_err(|e| e.to_string())?;
    g.check_frame(&out)
}

fn run_build(g: &Golden) -> Result<(), String> {
    let e = |e: baskfy_testkit::TestkitError| e.to_string();
    let watch: Vec<WatchItem> = g.input_as("watch").map_err(e)?;
    let account: SwingAccount = g.input_as("account").map_err(e)?;
    let gate: MarketGate = g.input_as("gate").map_err(e)?;
    let tier: ExposureTier = g.input_as("tier").map_err(e)?;
    let config: SwingConfig = g.input_as("config").map_err(e)?;
    let already: i64 = g.input_as("entries_already_today").map_err(e)?;
    let multiplier = g.input_decimal("risk_multiplier").map_err(e)?;
    let as_of = g.input_date("as_of").map_err(e)?;
    let out = build_entries(
        as_of, &watch, &account, gate, &tier, &config, already, multiplier,
    )
    .map_err(|e| e.to_string())?;
    g.check_output(&out)
}

fn grade(
    fn_name: &str,
    run: fn(&Golden) -> Result<(), String>,
    keep: fn(&Golden) -> bool,
) -> usize {
    let cases: Vec<Golden> = baskfy_testkit::cases_for(fn_name)
        .unwrap()
        .into_iter()
        .filter(keep)
        .collect();
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
fn detect_setups_matches_every_golden() {
    let n = grade(DETECT, run_detect, |_| true);
    assert_eq!(n, 10, "detect_setups case count changed");
}

#[test]
fn build_entries_matches_every_current_golden() {
    let n = grade(BUILD, run_build, |g| !is_stale(g));
    assert_eq!(n, 15, "build_entries case count changed");
}

/// Fails by design until `build_entries.case_010` is regenerated after LV10 (pyramiding):
/// see the module docs and the swing-B deviation note.
#[test]
fn build_entries_case_010_predates_lv10_pyramiding() {
    let n = grade(BUILD, run_build, is_stale);
    assert_eq!(n, 1);
}
