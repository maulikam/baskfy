//! Goldens for the swing area A ports: `market.exposure_tier`, `opening_range.evaluate_trigger`,
//! `sizing.size_position` and `stops.manage`. Every case under `L1/swing` for each function is
//! graded; a failing case is reported with every other failure before the test fails.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_core::swing::market::{MarketConfig, MarketGate, exposure_tier};
use baskfy_core::swing::opening_range::{OpeningRange, OpeningRangeConfig, evaluate_trigger};
use baskfy_core::swing::sizing::{SizingConfig, size_position};
use baskfy_core::swing::stops::{DailyBar, OpenPosition, StopConfig, manage};
use baskfy_testkit::Golden;
use rust_decimal::Decimal;

fn cases(fn_name: &str) -> Vec<Golden> {
    let cases: Vec<Golden> = baskfy_testkit::cases_for(fn_name)
        .unwrap()
        .into_iter()
        .filter(|g| g.case.starts_with("L1/swing/"))
        .collect();
    assert!(!cases.is_empty(), "no L1/swing goldens for {fn_name}");
    cases
}

/// Grades every case, then fails once with all the failures listed.
fn grade(fn_name: &str, run: impl Fn(&Golden) -> Result<(), String>) -> usize {
    let cases = cases(fn_name);
    let failures: Vec<String> = cases
        .iter()
        .filter_map(|g| {
            run(g).err().map(|e| {
                format!(
                    "{}: {e}{}",
                    g.case,
                    g.meta
                        .known_bug
                        .as_ref()
                        .map(|b| format!(" [known_bug: {b}]"))
                        .unwrap_or_default()
                )
            })
        })
        .collect();
    assert!(
        failures.is_empty(),
        "{fn_name}: {}/{} cases failed:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
    cases.len()
}

#[test]
fn exposure_tier_matches_every_golden() {
    let n = grade("baskfy_core.swing.market.exposure_tier", |g| {
        let current_level: i64 = g.input_as("current_level").map_err(|e| e.to_string())?;
        let closed: Vec<Decimal> = g
            .input_as("closed_r_multiples")
            .map_err(|e| e.to_string())?;
        let gate: MarketGate = g.input_as("gate").map_err(|e| e.to_string())?;
        let config: MarketConfig = g.input_as("config").map_err(|e| e.to_string())?;
        let drawdown_pct: f64 = g.input_as("drawdown_pct").map_err(|e| e.to_string())?;
        let was_locked: bool = g
            .input_as("was_drawdown_locked")
            .map_err(|e| e.to_string())?;
        let out = exposure_tier(
            current_level,
            &closed,
            gate,
            &config,
            drawdown_pct,
            was_locked,
        )
        .map_err(|e| e.to_string())?;
        g.check_output(&out)
    });
    assert_eq!(n, 14);
}

#[test]
fn evaluate_trigger_matches_every_golden() {
    let n = grade("baskfy_core.swing.opening_range.evaluate_trigger", |g| {
        let opening: OpeningRange = g.input_as("opening").map_err(|e| e.to_string())?;
        let config: OpeningRangeConfig = g.input_as("config").map_err(|e| e.to_string())?;
        let at = baskfy_testkit::naive_datetime(g.input("at").map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?;
        let out = evaluate_trigger(
            g.input_decimal("last_price").map_err(|e| e.to_string())?,
            &opening,
            g.input_opt_decimal("pivot_high")
                .map_err(|e| e.to_string())?,
            g.input_decimal("low_of_day").map_err(|e| e.to_string())?,
            g.input_opt_decimal("upper_circuit")
                .map_err(|e| e.to_string())?,
            at,
            &config,
        )
        .map_err(|e| e.to_string())?;
        g.check_output(&out)
    });
    assert_eq!(n, 12);
}

#[test]
fn size_position_matches_every_golden() {
    let n = grade("baskfy_core.swing.sizing.size_position", |g| {
        let config: SizingConfig = g.input_as("config").map_err(|e| e.to_string())?;
        let out = size_position(
            g.input_decimal("equity").map_err(|e| e.to_string())?,
            g.input_decimal("cash_available")
                .map_err(|e| e.to_string())?,
            g.input_decimal("entry").map_err(|e| e.to_string())?,
            g.input_decimal("stop").map_err(|e| e.to_string())?,
            g.input_opt_decimal("avg_turnover_inr")
                .map_err(|e| e.to_string())?,
            &config,
            g.input_decimal("max_stop_distance_pct")
                .map_err(|e| e.to_string())?,
        )
        .map_err(|e| e.to_string())?;
        g.check_output(&out)
    });
    assert_eq!(n, 11);
}

#[test]
fn manage_matches_every_golden() {
    let n = grade("baskfy_core.swing.stops.manage", |g| {
        let position: OpenPosition = g.input_as("position").map_err(|e| e.to_string())?;
        let bar: DailyBar = g.input_as("bar").map_err(|e| e.to_string())?;
        let config: StopConfig = g.input_as("config").map_err(|e| e.to_string())?;
        let out = manage(&position, &bar, &config).map_err(|e| e.to_string())?;
        g.check_output(&out)
    });
    assert_eq!(n, 16);
}
