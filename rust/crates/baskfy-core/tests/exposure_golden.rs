#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
//! `baskfy_core.exposure` against the goldens of `tools/parity/golden_exposure.py`.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::exposure::allocation::{
    Allocation, Candidate, Position, SurvivorParams, build_orders, feasible_position_range,
    order_sales, projected_equity_pct, select_survivors, solve_allocation,
    validate_allocation_request,
};
use baskfy_core::exposure::regime::{
    BookWeights, BreadthReading, Candle, EvaluateArgs, ExposureSnapshot, IndexSignals, NewBuyMode,
    PreviousRegime, RegimeConfig, RegimeTier, TargetPosition, apply_recovery_override,
    apply_sentinel_floor, apply_transition, bearish_stack_weight, build_index_signals,
    classify_candidate, confirmed_state_series, describe, evaluate, make_evaluation_id,
    moving_average, next_evaluation_date, optimistic_health, resolve_book_weights,
    resolve_forced_action, resolve_new_buys, weighted_health,
};
use baskfy_core::exposure::{ExposureError, ExposureResult};
use baskfy_testkit::{Golden, cases_for, to_golden_value};
use chrono::NaiveDate;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};

type R = Result<Value, String>;

fn s<E: std::fmt::Display>(e: E) -> String {
    e.to_string()
}

fn inp<T: DeserializeOwned>(g: &Golden, k: &str) -> Result<T, String> {
    g.input_as(k).map_err(s)
}

fn val<T: serde::Serialize>(x: &T) -> R {
    to_golden_value(x).map_err(s)
}

fn raises(e: &ExposureError) -> Value {
    json!({"raises": {"type": e.python_type(), "message": e.to_string()}})
}

fn out<T: serde::Serialize>(r: ExposureResult<T>) -> R {
    match r {
        Ok(v) => val(&v),
        Err(e) => Ok(raises(&e)),
    }
}

fn tier(g: &Golden, k: &str) -> Result<RegimeTier, String> {
    inp(g, k)
}

fn date(g: &Golden, k: &str) -> Result<NaiveDate, String> {
    g.input_date(k).map_err(s)
}

fn cfg(g: &Golden) -> Result<RegimeConfig, String> {
    inp(g, "cfg")
}

fn sigs(g: &Golden) -> Result<BTreeMap<String, IndexSignals>, String> {
    inp(g, "index_signals")
}

fn health(g: &Golden) -> Result<BTreeMap<usize, f64>, String> {
    inp(g, "health")
}

fn run(module_fn: &str, expected: usize, f: impl Fn(&Golden) -> R) {
    let cases = cases_for(&format!("baskfy_core.exposure.{module_fn}")).unwrap();
    assert_eq!(cases.len(), expected, "{module_fn} case count");
    let mut fails = Vec::new();
    for g in &cases {
        match f(g) {
            Ok(v) => {
                if let Err(m) = g.check_value(&v) {
                    fails.push(format!("{}: {m}", g.case));
                }
            }
            Err(e) => fails.push(format!("{}: decode/run: {e}", g.case)),
        }
    }
    assert!(fails.is_empty(), "{}", fails.join("\n"));
}

#[test]
fn moving_average_golden() {
    run("regime.moving_average", 15, |g| {
        let closes: Vec<f64> = inp(g, "closes")?;
        out(moving_average(&closes, inp(g, "length")?))
    });
}

#[test]
fn confirmed_state_series_golden() {
    run("regime.confirmed_state_series", 28, |g| {
        let closes: Vec<f64> = inp(g, "closes")?;
        let mas = moving_average(&closes, inp(g, "length")?).map_err(s)?;
        let st = confirmed_state_series(&closes, &mas, inp(g, "buffer")?, inp(g, "confirm_days")?);
        val(&st)
    });
}

#[test]
fn build_index_signals_golden() {
    run("regime.build_index_signals", 16, |g| {
        let name: String = inp(g, "index_name")?;
        let candles: Vec<Candle> = inp(g, "candles")?;
        let as_of: Option<NaiveDate> = inp(g, "as_of")?;
        out(build_index_signals(
            &name,
            &candles,
            &cfg(g)?,
            as_of,
            inp(g, "instrument_token")?,
        ))
    });
}

#[test]
fn resolve_book_weights_golden() {
    run("regime.resolve_book_weights", 11, |g| {
        let target: Option<Vec<TargetPosition>> = inp(g, "full_risk_target")?;
        let snap: Option<BookWeights> = inp(g, "last_r1_snapshot")?;
        out(resolve_book_weights(
            &cfg(g)?,
            target.as_deref(),
            snap.as_ref(),
        ))
    });
}

#[test]
fn health_goldens() {
    run("regime.weighted_health", 36, |g| {
        let b: BookWeights = inp(g, "book")?;
        val(&weighted_health(
            &b,
            &sigs(g)?,
            &cfg(g)?,
            inp(g, "ma_length")?,
        ))
    });
    run("regime.optimistic_health", 36, |g| {
        let b: BookWeights = inp(g, "book")?;
        val(&optimistic_health(
            &b,
            &sigs(g)?,
            &cfg(g)?,
            inp(g, "ma_length")?,
        ))
    });
    run("regime.bearish_stack_weight", 12, |g| {
        let b: BookWeights = inp(g, "book")?;
        val(&bearish_stack_weight(&b, &sigs(g)?, &cfg(g)?))
    });
}

#[test]
fn classifier_goldens() {
    run("regime.classify_candidate", 36, |g| {
        let sentinel: Option<IndexSignals> = inp(g, "sentinel")?;
        val(&classify_candidate(
            &health(g)?,
            inp(g, "stack_weight")?,
            inp(g, "breadth_pct")?,
            inp(g, "breadth_usable")?,
            sentinel.as_ref(),
            &cfg(g)?,
        ))
    });
    run("regime.apply_sentinel_floor", 28, |g| {
        let sentinel: Option<IndexSignals> = inp(g, "sentinel")?;
        val(&apply_sentinel_floor(tier(g, "tier")?, sentinel.as_ref()))
    });
    run("regime.apply_recovery_override", 32, |g| {
        val(&apply_recovery_override(
            tier(g, "tier")?,
            &health(g)?,
            inp(g, "breadth_pct")?,
            inp(g, "breadth_usable")?,
            inp(g, "confirmations")?,
            &cfg(g)?,
        ))
    });
    run("regime.apply_transition", 20, |g| {
        let prev: Option<RegimeTier> = inp(g, "previous")?;
        val(&apply_transition(prev, tier(g, "candidate")?))
    });
    run("regime.resolve_new_buys", 24, |g| {
        let sentinel: Option<IndexSignals> = inp(g, "sentinel")?;
        val(&resolve_new_buys(
            tier(g, "tier")?,
            sentinel.as_ref(),
            inp(g, "data_ok")?,
        ))
    });
    run("regime.resolve_forced_action", 40, |g| {
        val(&resolve_forced_action(
            tier(g, "tier")?,
            inp(g, "gap_pct")?,
            &cfg(g)?,
            inp(g, "bootstrap_suppressed")?,
        ))
    });
    run("regime.describe", 1, |g| {
        let codes: Vec<String> = inp(g, "codes")?;
        val(&describe(&codes))
    });
}

#[test]
fn config_goldens() {
    run("regime.next_evaluation_date", 21, |g| {
        val(&next_evaluation_date(date(g, "after")?, &cfg(g)?))
    });
    run("regime.RegimeConfig.config_hash", 7, |g| {
        val(&cfg(g)?.config_hash())
    });
    run("regime.make_evaluation_id", 14, |g| {
        val(&make_evaluation_id(
            date(g, "scheduled_week_end")?,
            date(g, "signal_session_date")?,
            &cfg(g)?,
        ))
    });
    run("regime.RegimeConfig.validate", 17, |g| {
        out(cfg(g)?.validate())
    });
}

#[test]
fn evaluate_golden() {
    run("regime.evaluate", 114, |g| {
        let idx = sigs(g)?;
        let sentinel: Option<IndexSignals> = inp(g, "sentinel")?;
        let breadth: Option<BreadthReading> = inp(g, "breadth")?;
        let book: BookWeights = inp(g, "book")?;
        let exposure: ExposureSnapshot = inp(g, "exposure")?;
        let previous: Option<PreviousRegime> = inp(g, "previous")?;
        let c = cfg(g)?;
        out(evaluate(&EvaluateArgs {
            as_of_date: date(g, "as_of_date")?,
            scheduled_week_end: date(g, "scheduled_week_end")?,
            signal_session_date: date(g, "signal_session_date")?,
            index_signals: &idx,
            sentinel: sentinel.as_ref(),
            breadth: breadth.as_ref(),
            book: &book,
            exposure: &exposure,
            cfg: &c,
            previous: previous.as_ref(),
        }))
    });
}

#[test]
fn allocation_goldens() {
    run("allocation.feasible_position_range", 7, |g| {
        out(feasible_position_range(
            inp(g, "min_sleeve_w")?,
            inp(g, "max_sleeve_w")?,
        ))
    });
    run("allocation.validate_allocation_request", 8, |g| {
        out(validate_allocation_request(
            inp(g, "n")?,
            inp(g, "min_sleeve_w")?,
            inp(g, "max_sleeve_w")?,
            inp(g, "n_half")?,
        ))
    });
    run("allocation.solve_allocation", 30, |g| {
        let cands: Vec<Candidate> = inp(g, "candidates")?;
        let half: BTreeSet<String> = inp(g, "half_sized")?;
        out(solve_allocation(
            &cands,
            inp(g, "sleeve_pct")?,
            inp(g, "min_sleeve_w")?,
            inp(g, "max_sleeve_w")?,
            inp(g, "cluster_cap")?,
            &half,
            inp(g, "half_factor")?,
        ))
    });
    run("allocation.order_sales", 14, |g| {
        let ps: Vec<Position> = inp(g, "positions")?;
        let flagged: BTreeSet<String> = inp(g, "tax_flagged")?;
        val(&order_sales(&ps, inp(g, "nearby_band")?, &flagged))
    });
    run("allocation.select_survivors", 8, |g| {
        let cands: Vec<Candidate> = inp(g, "candidates")?;
        let ps: Vec<Position> = inp(g, "positions")?;
        let new_buys: NewBuyMode = inp(g, "new_buys")?;
        let p = SurvivorParams {
            max_positions: inp(g, "max_positions")?,
            new_buys,
            residual_max_names: inp(g, "residual_max_names")?,
            residual_ordering: inp(g, "residual_ordering")?,
            nearby_band: inp(g, "nearby_band")?,
            tax_flagged: inp(g, "tax_flagged")?,
        };
        let (survivors, dropped) = select_survivors(&cands, &ps, &p);
        Ok(json!({"survivors": val(&survivors)?, "dropped": dropped}))
    });
    run("allocation.build_orders", 10, |g| {
        let alloc: Allocation = inp(g, "allocation")?;
        let ps: Vec<Position> = inp(g, "positions")?;
        let cands: Vec<Candidate> = inp(g, "candidates")?;
        let capital: f64 = inp(g, "capital")?;
        let lines = build_orders(
            &alloc,
            &ps,
            &cands,
            capital,
            &inp(g, "tax_flagged")?,
            &inp(g, "guard_exits")?,
        );
        Ok(json!({
            "lines": val(&lines)?,
            "projected_equity_pct": projected_equity_pct(&lines, capital),
        }))
    });
}
