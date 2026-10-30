//! `app/strategies/base.py` and `app/strategies/momentum_weekly.py`: the strategy plugin
//! interface and the live weekly momentum strategy (score → plan → targets).
//!
//! A strategy **only proposes**. It talks to the market through the gateway it is given (law
//! 2), and `generate_targets` returns rows for an engine to review; nothing here sends an
//! order, and the weekly strategy has no auto-execute flag (non-negotiable 1).

use std::sync::Arc;

use super::basket::{BasketConfig, PlanError, PlanInputs, build_plan};
use super::score::{ScanError, load_scan, score};
use crate::gateway::Gateway;
use crate::wire::{PyDict, PyValue, dict};

/// What a strategy's batch hook could not do.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum StrategyError {
    /// The scan (`load_scan` / `score`).
    #[error("{0}")]
    Scan(String),
    /// The plan (`build_plan`).
    #[error("{0}")]
    Plan(String),
}

impl From<ScanError> for StrategyError {
    fn from(e: ScanError) -> Self {
        Self::Scan(e.0)
    }
}

impl From<PlanError> for StrategyError {
    fn from(e: PlanError) -> Self {
        Self::Plan(e.0)
    }
}

/// `context` for the batch hook: the scan's bytes (`context["scan_path"]` read), holdings and
/// cash; `plan` is filled in, as Python writes `context["plan"]`.
#[derive(Debug, Clone, Default)]
pub struct Context {
    /// The scan CSV's bytes.
    pub scan: Vec<u8>,
    /// `context["holdings"]`.
    pub holdings: Vec<PyDict>,
    /// `context["cash"]`.
    pub cash: f64,
    /// The new plan's id (`uuid4().hex[:12]`).
    pub plan_id: String,
    /// `rebalance._load_clusters()` (`data/sectors.csv`, symbol → cluster): Python's
    /// `build_plan` binding reads it on every call, so the caller supplies it here
    /// ([`super::analyze::load_clusters`]); the cluster cap cannot be skipped by omission.
    pub clusters: std::collections::HashMap<String, String>,
    /// `context["plan"]`, set by `generate_targets`.
    pub plan: Option<PyDict>,
}

/// `BaseStrategy`: every engine implements this and reaches the market **only** via the
/// gateway it is given.
pub trait Strategy: Send + Sync {
    /// `name`.
    fn name(&self) -> &'static str;
    /// `products`: which products this strategy may use.
    fn products(&self) -> &'static [&'static str];
    /// The gateway the strategy was built with (`self.gw`).
    fn gateway(&self) -> &Arc<dyn Gateway>;
    /// `generate_targets(context)`: `[{symbol, side, qty, price, product, stop}]`.
    ///
    /// # Errors
    /// The scan or the plan could not be built.
    fn generate_targets(&self, context: &mut Context) -> Result<Vec<PyDict>, StrategyError>;
}

/// `MomentumWeekly`: wraps the scorer and the planner. CNC only.
pub struct MomentumWeekly {
    gw: Arc<dyn Gateway>,
    cfg: BasketConfig,
}

impl MomentumWeekly {
    /// `MomentumWeekly(gateway)` with the desk's constants.
    #[must_use]
    pub fn new(gateway: Arc<dyn Gateway>, cfg: BasketConfig) -> Self {
        Self { gw: gateway, cfg }
    }
}

impl std::fmt::Debug for MomentumWeekly {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("MomentumWeekly").finish_non_exhaustive()
    }
}

impl Strategy for MomentumWeekly {
    fn name(&self) -> &'static str {
        "momentum_weekly"
    }

    fn products(&self) -> &'static [&'static str] {
        &["CNC"]
    }

    fn gateway(&self) -> &Arc<dyn Gateway> {
        &self.gw
    }

    fn generate_targets(&self, context: &mut Context) -> Result<Vec<PyDict>, StrategyError> {
        let scored = score(
            &load_scan(&context.scan, &self.cfg.scoring)?,
            &self.cfg.scoring,
        )?;
        let inputs = PlanInputs {
            holdings: context.holdings.clone(),
            cash: context.cash,
            plan_id: context.plan_id.clone(),
            clusters: context.clusters.clone(),
            ..PlanInputs::default()
        };
        let plan = build_plan(&scored, &inputs, &self.cfg)?;
        let mut out = Vec::new();
        if let Some(PyValue::List(orders)) = plan.get("orders") {
            for o in orders {
                let PyValue::Map(o) = o else { continue };
                let delta = match o.get("delta") {
                    Some(PyValue::Int(i)) => *i,
                    _ => 0,
                };
                if delta == 0 {
                    continue;
                }
                out.push(dict([
                    ("symbol", o.get("symbol").cloned().unwrap_or(PyValue::None)),
                    (
                        "side",
                        PyValue::Str(if delta < 0 { "SELL" } else { "BUY" }.to_owned()),
                    ),
                    ("qty", PyValue::Int(delta.abs())),
                    (
                        "price",
                        o.get("ref_price").cloned().unwrap_or(PyValue::None),
                    ),
                    ("product", PyValue::Str("CNC".to_owned())),
                    ("stop", o.get("stop").cloned().unwrap_or(PyValue::None)),
                ]));
            }
        }
        context.plan = Some(plan);
        Ok(out)
    }
}
