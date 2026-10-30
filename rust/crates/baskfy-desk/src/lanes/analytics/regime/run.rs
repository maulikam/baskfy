//! `app/analytics/regime_run.py`: regime planning, execution orchestration and reconciliation.
//!
//! **Three independent gates, in this order:** `REGIME_ENABLED` (the feature switch),
//! `REGIME_MODE` (observe: no executable plan / propose: a plan that needs approval / enforce: a
//! plan that may be executed) and `DRY_RUN` (the global no-order guarantee, checked again inside
//! the gateway). `DRY_RUN` permits downloading data, computing tiers, proposing sales and showing
//! tax flags; it forbids submitting an order, committing a tier transition
//! ([`super::super::regime_store::commit_evaluation`] refuses) and marking a rebalance executed.
//!
//! **Order path:** [`execute_regime_plan`] reaches the broker only through the desk's
//! [`Gateway`] — guards → risk → rate limits → journal, unmodified. Nothing here holds a Kite
//! write handle. As in Python, the plan's orders carry no `client_id` (the regime plan has no
//! `plan_id` of its own); no route reaches this function — the evaluation is a CLI and the
//! `/regime` page only reads.

use std::collections::BTreeSet;

use super::alloc::{
    Allocation, AllocationError, Candidate, OrderLine, Position, RegimePlan, build_orders,
    projected_equity_pct, select_survivors, solve_allocation,
};
use super::engine::{ExecutionStatus, NewBuyMode, RegimeMode, RegimeState, RegimeTier, py_sum};
use crate::gateway::{Gateway, GatewayError, PlaceOrder};
use crate::lanes::analytics::regime_store;
use crate::lanes::weekly::pandas::py_round;
use crate::lanes::weekly::weekly_books::tax_lots::{self, TaxReview};
use crate::pyfmt::{PyArg, format_value};
use crate::storage::{Conn, StorageError};
use crate::wire::{PyDict, PyValue, dict};

/// `PLAN_INFEASIBLE`.
pub const PLAN_INFEASIBLE: &str = "PLAN_INFEASIBLE";
/// `PLAN_CAP_UNREACHABLE`.
pub const PLAN_CAP_UNREACHABLE: &str = "PLAN_CAP_UNREACHABLE";
/// `PLAN_TAX_DEFERRED`.
pub const PLAN_TAX_DEFERRED: &str = "PLAN_TAX_DEFERRED";
/// `PLAN_TAX_SOLD_ANYWAY`.
pub const PLAN_TAX_SOLD_ANYWAY: &str = "PLAN_TAX_SOLD_ANYWAY";
/// `PLAN_NOT_EXECUTABLE_OBSERVE`.
pub const PLAN_NOT_EXECUTABLE_OBSERVE: &str = "PLAN_NOT_EXECUTABLE_OBSERVE";
/// `PLAN_NEEDS_APPROVAL`.
pub const PLAN_NEEDS_APPROVAL: &str = "PLAN_NEEDS_APPROVAL";
/// `PLAN_DRY_RUN`.
pub const PLAN_DRY_RUN: &str = "PLAN_DRY_RUN";
/// `PLAN_GUARD_EXITS_PRESERVED`.
pub const PLAN_GUARD_EXITS_PRESERVED: &str = "PLAN_GUARD_EXITS_PRESERVED";

/// What execution refuses.
#[derive(Debug, thiserror::Error)]
pub enum RunError {
    /// `RegimeDisabledError`.
    #[error("{0}")]
    RegimeDisabled(String),
    /// `PlanNotExecutableError`.
    #[error("{0}")]
    PlanNotExecutable(String),
    /// What the gateway raised.
    #[error(transparent)]
    Gateway(#[from] GatewayError),
}

/// The sizing knobs `plan_regime_rebalance` reads from `config` when the caller passes none.
#[derive(Debug, Clone, PartialEq)]
pub struct PlanKnobs {
    /// `MIN_POSITION_WEIGHT` (% of the sleeve).
    pub min_sleeve_w: f64,
    /// `MAX_SINGLE_WEIGHT` (% of the sleeve).
    pub max_sleeve_w: f64,
    /// `CLUSTER_CAP`.
    pub cluster_cap: f64,
    /// `TARGET_POSITIONS`.
    pub target_positions: (i64, i64),
    /// `REGIME_R4_MAX_RESIDUAL_NAMES` (used only in R4).
    pub residual_max_names: i64,
    /// `REGIME_R4_RESIDUAL_ORDERING`.
    pub residual_ordering: Vec<String>,
    /// `REGIME_NEARBY_RANK_BAND`.
    pub nearby_band: i64,
    /// `REGIME_HALF_SIZE_FACTOR`.
    pub half_factor: f64,
}

impl Default for PlanKnobs {
    /// `app/config.py`'s defaults.
    fn default() -> Self {
        Self {
            min_sleeve_w: 6.0,
            max_sleeve_w: 15.0,
            cluster_cap: 25.0,
            target_positions: (12, 15),
            residual_max_names: 2,
            residual_ordering: vec!["rank".to_owned(), "liquidity".to_owned()],
            nearby_band: 3,
            half_factor: 0.5,
        }
    }
}

impl PlanKnobs {
    /// The live configuration: the settings for the regime knobs, the defaults for the sizing
    /// constants (which `app/config.py` holds as module constants).
    #[must_use]
    pub fn from_settings(s: &crate::settings::Settings) -> Self {
        Self {
            residual_max_names: s.regime_r4_max_residual_names,
            residual_ordering: s.regime_r4_residual_ordering.clone(),
            nearby_band: s.regime_nearby_rank_band,
            half_factor: s.regime_half_size_factor,
            ..Self::default()
        }
    }
}

fn head<T: Clone>(v: &[T], n: i64) -> Vec<T> {
    let len = i64::try_from(v.len()).unwrap_or(i64::MAX);
    let end = if n < 0 { (len + n).max(0) } else { n.min(len) };
    v[..usize::try_from(end).unwrap_or(0)].to_vec()
}

/// `plan_regime_rebalance(state, candidates, positions, capital=, tax_reviews=, mode=, dry_run=,
/// ...)`: **one** final target book for the committed policy tier, then the netted orders.
///
/// # Errors
/// A target quantity Python's `int(round(...))` cannot make (never an infeasible allocation:
/// that becomes a manual-review plan).
#[allow(clippy::too_many_lines, clippy::too_many_arguments)]
pub fn plan_regime_rebalance(
    state: &RegimeState,
    candidates: &[Candidate],
    positions: &[Position],
    capital: f64,
    tax_reviews: &[(String, TaxReview)],
    mode: RegimeMode,
    dry_run: bool,
    knobs: &PlanKnobs,
) -> Result<RegimePlan, AllocationError> {
    let mut min_w = knobs.min_sleeve_w;
    let mut max_w = knobs.max_sleeve_w;
    let (lo, hi) = knobs.target_positions;
    let mut codes: Vec<&'static str> = Vec::new();

    let guard_exits: BTreeSet<String> = positions
        .iter()
        .filter(|p| p.guard_exit)
        .map(|p| p.symbol.clone())
        .collect();
    if !guard_exits.is_empty() {
        codes.push(PLAN_GUARD_EXITS_PRESERVED);
    }
    let flagged: BTreeSet<String> = tax_lots::flagged_symbols(tax_reviews)
        .into_iter()
        .collect();
    let mut sleeve = state.target_equity_cap_pct;

    let mut residual = None;
    if state.policy_tier == RegimeTier::R4 {
        residual = Some(knobs.residual_max_names);
        if knobs.residual_max_names <= 0 {
            // No residual policy means full cash — never invent names to retain.
            sleeve = 0.0;
        }
    }

    let (survivors, _dropped) = select_survivors(
        candidates,
        positions,
        hi,
        state.new_buys,
        residual,
        &knobs.residual_ordering,
        knobs.nearby_band,
        &flagged,
    );
    let mut survivors: Vec<Candidate> = survivors
        .into_iter()
        .filter(|c| !guard_exits.contains(&c.symbol))
        .collect();

    if sleeve <= 0.0 || survivors.is_empty() {
        let alloc = Allocation::empty(sleeve);
        let orders = build_orders(&alloc, positions, candidates, capital, &flagged, &guard_exits)?;
        let proj = projected_equity_pct(&orders, capital);
        return Ok(finalise(
            state, alloc, orders, proj, mode, dry_run, codes, tax_reviews, None,
        ));
    }

    // A residual policy deliberately holds very few names, so the normal floor and cap are
    // relaxed for the residual case only, and only within the shrunken sleeve.
    if residual.is_some() {
        #[allow(clippy::cast_precision_loss)]
        let share = 100.0 / survivors.len() as f64;
        min_w = min_w.min(share);
        max_w = max_w.max(share);
    }

    let held: BTreeSet<&str> = positions.iter().map(|p| p.symbol.as_str()).collect();
    let mut half: BTreeSet<String> = BTreeSet::new();
    if state.new_buys == NewBuyMode::Half {
        half = survivors
            .iter()
            .filter(|c| !held.contains(c.symbol.as_str()))
            .map(|c| c.symbol.clone())
            .collect();
    }

    let alloc = match solve_allocation(
        &survivors,
        sleeve,
        min_w,
        max_w,
        knobs.cluster_cap,
        &half,
        knobs.half_factor,
    ) {
        Ok(a) => a,
        Err(exc) => {
            // Retry once at the low end of the target range before giving up.
            let trimmed = head(&survivors, lo);
            let trimmed_names: BTreeSet<String> =
                trimmed.iter().map(|c| c.symbol.clone()).collect();
            let half_trimmed: BTreeSet<String> =
                half.intersection(&trimmed_names).cloned().collect();
            match solve_allocation(
                &trimmed,
                sleeve,
                min_w,
                max_w,
                knobs.cluster_cap,
                &half_trimmed,
                knobs.half_factor,
            ) {
                Ok(a) => {
                    survivors = trimmed;
                    let _ = &survivors;
                    a
                }
                Err(_) => {
                    codes.push(PLAN_INFEASIBLE);
                    return Ok(finalise(
                        state,
                        Allocation::empty(sleeve),
                        Vec::new(),
                        state.actual_equity_pct,
                        mode,
                        dry_run,
                        codes,
                        tax_reviews,
                        Some(exc.0),
                    ));
                }
            }
        }
    };

    let orders = build_orders(&alloc, positions, candidates, capital, &flagged, &guard_exits)?;
    let proj = projected_equity_pct(&orders, capital);
    if !flagged.is_empty() && orders.iter().any(|o| o.tax_review) {
        // Tax is advisory: the sale still happens, surfaced for review rather than leaving the
        // exposure cap silently breached.
        codes.push(if proj <= sleeve + 1e-6 {
            PLAN_TAX_SOLD_ANYWAY
        } else {
            PLAN_TAX_DEFERRED
        });
    }
    Ok(finalise(
        state, alloc, orders, proj, mode, dry_run, codes, tax_reviews, None,
    ))
}

#[allow(clippy::too_many_arguments)]
fn finalise(
    state: &RegimeState,
    alloc: Allocation,
    orders: Vec<OrderLine>,
    projected: f64,
    mode: RegimeMode,
    dry_run: bool,
    mut codes: Vec<&'static str>,
    reviews: &[(String, TaxReview)],
    infeasible: Option<String>,
) -> RegimePlan {
    let gap = py_round(projected - state.target_equity_cap_pct, 6);
    let mut manual = infeasible.as_deref().is_some_and(|s| !s.is_empty())
        || reviews.iter().any(|(_, r)| r.manual_review());
    if gap > 1e-6 {
        codes.push(PLAN_CAP_UNREACHABLE);
        manual = true;
    }
    let executable = mode == RegimeMode::Enforce
        && !dry_run
        && infeasible.as_deref().is_none_or(str::is_empty);
    match mode {
        RegimeMode::Observe => codes.push(PLAN_NOT_EXECUTABLE_OBSERVE),
        RegimeMode::Propose => codes.push(PLAN_NEEDS_APPROVAL),
        RegimeMode::Enforce => {}
    }
    if dry_run {
        codes.push(PLAN_DRY_RUN);
    }
    let mut deduped: Vec<&'static str> = Vec::new();
    for c in codes {
        if !deduped.contains(&c) {
            deduped.push(c);
        }
    }
    let mut tax_flagged: Vec<String> = reviews
        .iter()
        .filter(|(_, r)| r.flagged)
        .map(|(s, _)| s.clone())
        .collect();
    tax_flagged.sort();
    tax_flagged.dedup();
    RegimePlan {
        tier: state.policy_tier,
        sleeve_pct: alloc.sleeve_pct,
        allocation: alloc,
        orders,
        projected_equity_pct: projected,
        exposure_gap_after_plan_pct: gap,
        manual_action_required: manual,
        executable,
        reason_codes: deduped,
        tax_flagged,
        infeasible_reason: infeasible,
    }
}

// =============================================================================================
// execution — the ONLY path is the gateway
// =============================================================================================

/// `ExecutionReport`.
#[derive(Debug, Clone, PartialEq)]
pub struct ExecutionReport {
    /// `submitted`: the gateway's answers whose status is `PLACED` or `DRY_RUN`.
    pub submitted: Vec<PyDict>,
    /// `skipped`: every other answer.
    pub skipped: Vec<PyDict>,
    /// `status`.
    pub status: ExecutionStatus,
    /// `dry_run`.
    pub dry_run: bool,
    /// `mode`.
    pub mode: String,
}

fn status_of(r: &PyDict) -> String {
    match r.get("status") {
        Some(PyValue::Str(s)) => s.clone(),
        _ => String::new(),
    }
}

impl ExecutionReport {
    /// `filled_pct`.
    #[must_use]
    #[allow(clippy::cast_precision_loss)]
    pub fn filled_pct(&self) -> f64 {
        if self.submitted.is_empty() {
            return 0.0;
        }
        let ok = self
            .submitted
            .iter()
            .filter(|r| matches!(status_of(r).as_str(), "PLACED" | "DRY_RUN"))
            .count();
        ok as f64 / self.submitted.len() as f64 * 100.0
    }
}

/// `execute_regime_plan(gateway, plan, mode=, dry_run=, approved=False)`: submit a plan through
/// the gateway, sells first (largest first), then buys. Refuses unless the overlay is enabled,
/// the mode is enforce, the plan is explicitly approved, `DRY_RUN` is off and the plan is
/// feasible; the gateway then performs its own `DRY_RUN` check — belt and braces.
///
/// # Errors
/// [`RunError`] for every refusal, and whatever the gateway raises.
pub async fn execute_regime_plan(
    gateway: &dyn Gateway,
    plan: &RegimePlan,
    regime_enabled: bool,
    mode: RegimeMode,
    dry_run: bool,
    approved: bool,
) -> Result<ExecutionReport, RunError> {
    if !regime_enabled {
        return Err(RunError::RegimeDisabled(
            "REGIME_ENABLED is false — no regime orders are placed".to_owned(),
        ));
    }
    if mode != RegimeMode::Enforce {
        return Err(RunError::PlanNotExecutable(format!(
            "mode={}: only enforce mode may execute a regime plan",
            mode.value()
        )));
    }
    if !approved {
        return Err(RunError::PlanNotExecutable(
            "regime plan requires explicit approval".to_owned(),
        ));
    }
    if dry_run {
        return Err(RunError::PlanNotExecutable(
            "DRY_RUN is active — refusing to submit regime orders".to_owned(),
        ));
    }
    if let Some(reason) = plan.infeasible_reason.as_deref().filter(|r| !r.is_empty()) {
        return Err(RunError::PlanNotExecutable(format!(
            "plan is infeasible: {reason}"
        )));
    }
    let mut ordered: Vec<&OrderLine> = plan.orders.iter().filter(|o| o.delta != 0).collect();
    #[allow(clippy::cast_precision_loss)]
    ordered.sort_by(|a, b| {
        let ka = (a.delta > 0, -(a.delta as f64 * a.ref_price).abs());
        let kb = (b.delta > 0, -(b.delta as f64 * b.ref_price).abs());
        ka.0.cmp(&kb.0)
            .then(ka.1.partial_cmp(&kb.1).unwrap_or(std::cmp::Ordering::Equal))
    });
    let mut submitted = Vec::new();
    let mut skipped = Vec::new();
    for o in ordered {
        let side = if o.delta < 0 { "SELL" } else { "BUY" };
        let mut res = gateway
            .place(PlaceOrder {
                symbol: o.symbol.clone(),
                qty: o.delta.abs(),
                side: side.to_owned(),
                product: "CNC".to_owned(),
                order_type: "LIMIT".to_owned(),
                price: Some(o.ref_price),
                exchange: "NSE".to_owned(),
                ..PlaceOrder::default()
            })
            .await?;
        res.insert("regime_action", PyValue::Str(o.action.to_owned()));
        if matches!(status_of(&res).as_str(), "PLACED" | "DRY_RUN") {
            submitted.push(res);
        } else {
            skipped.push(res);
        }
    }
    let status = match (submitted.is_empty(), skipped.is_empty()) {
        (true, true) => ExecutionStatus::NotPlanned,
        (false, false) => ExecutionStatus::Partial,
        (true, false) => ExecutionStatus::Failed,
        (false, true) => ExecutionStatus::Completed,
    };
    Ok(ExecutionReport {
        submitted,
        skipped,
        status,
        dry_run,
        mode: mode.value().to_owned(),
    })
}

// =============================================================================================
// reconciliation
// =============================================================================================

/// `reconcile(conn, state, plan, actual_equity_pct=, capital=None, execution_status=, observed_at=)`:
/// record where exposure stands — safe and expected on every invocation. Pending legs are rupee
/// values measured against **rupee** capital (never the exposure percentage); without capital
/// they are reported as 0, never invented.
///
/// # Errors
/// The driver's.
#[allow(clippy::too_many_arguments)]
pub async fn reconcile(
    conn: &mut Conn,
    state: &RegimeState,
    plan: &RegimePlan,
    actual_equity_pct: f64,
    capital: Option<f64>,
    execution_status: ExecutionStatus,
    observed_at: &str,
) -> Result<PyDict, StorageError> {
    #[allow(clippy::cast_precision_loss)]
    let pending_sell = py_sum(
        plan.sells()
            .iter()
            .map(|o| o.delta.abs() as f64 * o.ref_price),
    );
    #[allow(clippy::cast_precision_loss)]
    let pending_buy = py_sum(plan.buys().iter().map(|o| o.delta as f64 * o.ref_price));
    let base = capital.filter(|c| *c > 0.0);
    let pct = |x: f64| base.map_or(0.0, |b| py_round(x / b * 100.0, 6));
    let note = format!(
        "tier={} projected={}%",
        state.policy_tier.value(),
        format_value(&PyArg::Float(plan.projected_equity_pct), ".2f").unwrap_or_default()
    );
    regime_store::record_exposure_status(
        conn,
        &state.evaluation_id,
        actual_equity_pct,
        state.target_equity_cap_pct,
        pct(pending_buy),
        pct(pending_sell),
        execution_status,
        observed_at,
        Some(&note),
    )
    .await
}

fn opt_i(x: Option<i64>) -> PyValue {
    x.map_or(PyValue::None, PyValue::Int)
}

fn strs<S: AsRef<str>>(v: &[S]) -> PyValue {
    PyValue::List(
        v.iter()
            .map(|s| PyValue::Str(s.as_ref().to_owned()))
            .collect(),
    )
}

/// `summarise(plan)`.
#[must_use]
pub fn summarise(plan: &RegimePlan) -> PyDict {
    let mut largest: (PyValue, f64) = (PyValue::None, 0.0);
    let mut first = true;
    for (s, w) in &plan.allocation.weights_sleeve {
        if first || *w > largest.1 {
            largest = (PyValue::Str(s.clone()), *w);
            first = false;
        }
    }
    let mut clusters = PyDict::new();
    for (k, v) in &plan.allocation.cluster_weights {
        clusters.insert(k.clone(), PyValue::Float(*v));
    }
    let half: Vec<&String> = plan.allocation.half_sized.iter().collect();
    dict([
        ("tier", PyValue::Str(plan.tier.value().to_owned())),
        ("sleeve_pct", PyValue::Float(plan.sleeve_pct)),
        (
            "positions",
            PyValue::Int(i64::try_from(plan.allocation.n_positions()).unwrap_or(i64::MAX)),
        ),
        ("half_sized", strs(&half)),
        ("cluster_weights", PyValue::Map(clusters)),
        (
            "largest_position",
            PyValue::Map(dict([
                ("symbol", largest.0),
                ("weight_sleeve", PyValue::Float(py_round(largest.1, 2))),
            ])),
        ),
        ("allocation_notes", strs(&plan.allocation.notes)),
        (
            "projected_equity_pct",
            PyValue::Float(plan.projected_equity_pct),
        ),
        (
            "exposure_gap_after_plan_pct",
            PyValue::Float(plan.exposure_gap_after_plan_pct),
        ),
        (
            "buys",
            PyValue::Int(i64::try_from(plan.buys().len()).unwrap_or(i64::MAX)),
        ),
        (
            "sells",
            PyValue::Int(i64::try_from(plan.sells().len()).unwrap_or(i64::MAX)),
        ),
        ("round_trips", strs(&plan.round_trips())),
        ("executable", PyValue::Bool(plan.executable)),
        (
            "manual_action_required",
            PyValue::Bool(plan.manual_action_required),
        ),
        ("tax_flagged", strs(&plan.tax_flagged)),
        ("reason_codes", strs(&plan.reason_codes)),
        (
            "infeasible_reason",
            plan.infeasible_reason
                .clone()
                .map_or(PyValue::None, PyValue::Str),
        ),
    ])
}

/// One proposed order as `evaluation_snapshot` stores it.
#[must_use]
pub fn order_dict(o: &OrderLine) -> PyDict {
    dict([
        ("symbol", PyValue::Str(o.symbol.clone())),
        ("action", PyValue::Str(o.action.to_owned())),
        ("qty_now", PyValue::Int(o.qty_now)),
        ("delta", PyValue::Int(o.delta)),
        ("qty_final", PyValue::Int(o.qty_final)),
        ("ref_price", PyValue::Float(o.ref_price)),
        ("weight_nav", PyValue::Float(o.weight_nav)),
        ("weight_sleeve", PyValue::Float(o.weight_sleeve)),
        ("value", PyValue::Float(o.value)),
        ("rank", opt_i(o.rank)),
        ("half_sized", PyValue::Bool(o.half_sized)),
        ("tax_review", PyValue::Bool(o.tax_review)),
        ("note", PyValue::Str(o.note.clone())),
    ])
}

/// `evaluation_snapshot(plan=None, tax_reviews=None)`: the `input_snapshot` payload the `/regime`
/// page renders — the plan summary, the proposed (non-zero) orders and the manual-review items.
#[must_use]
pub fn evaluation_snapshot(
    plan: Option<&RegimePlan>,
    tax_reviews: Option<&[(String, TaxReview)]>,
) -> PyDict {
    let mut snap = PyDict::new();
    if let Some(plan) = plan {
        snap.insert("plan", PyValue::Map(summarise(plan)));
        snap.insert(
            "proposed_orders",
            PyValue::List(
                plan.orders
                    .iter()
                    .filter(|o| o.delta != 0)
                    .map(|o| PyValue::Map(order_dict(o)))
                    .collect(),
            ),
        );
    }
    if let Some(reviews) = tax_reviews.filter(|r| !r.is_empty()) {
        snap.insert(
            "manual_review_items",
            PyValue::List(
                tax_lots::manual_review_items(reviews)
                    .into_iter()
                    .map(PyValue::Map)
                    .collect(),
            ),
        );
    }
    snap
}
