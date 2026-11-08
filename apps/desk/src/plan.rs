//! Plans and the confirm gate (non-negotiable 1; DECISIONS-RS R8.0).
//!
//! `app/main.py`: `/analyze` builds a plan, stamps `created_at = time.time()` and keeps it in
//! the process-local `PLANS` dict; `/execute` fires **only** with `confirm == "true"`, a
//! `plan_id` that is in `PLANS`, and a plan no older than 1,800 seconds — in that order, with
//! these answers. [`PlanStore`] and [`check_execute`] are that, once, for every lane; the
//! sleeves' plans (swing/TWT/VBT/FNO, stored in Postgres with their own expiry columns) use
//! [`PLAN_TTL_SECONDS`] and [`is_expired`] where their Python uses the same 30 minutes.
//!
//! [`plan_digest`] is what `desk_diff.py --plans` compares besides the bytes: SHA-256 of the
//! plan's `orjson` text.

use std::collections::HashMap;
use std::sync::RwLock;

use sha2::{Digest, Sha256};

use crate::wire::{PyDict, PyValue, WireError, orjson_dumps};

/// `/execute`'s staleness bound: 30 minutes.
pub const PLAN_TTL_SECONDS: f64 = 1800.0;

/// A plan as `/analyze` stored it.
#[derive(Debug, Clone, PartialEq)]
pub struct Plan {
    /// `plan["plan_id"]` (`uuid4().hex[:12]` for the weekly book).
    pub plan_id: String,
    /// `plan["created_at"]` (`time.time()` at build).
    pub created_at: f64,
    /// The whole plan dict, in Python's key order (it is the response body).
    pub body: PyDict,
}

/// One order line of a weekly plan (`plan["orders"][i]`): the four keys `/execute` reads, and
/// the row itself.
#[derive(Debug, Clone, PartialEq)]
pub struct PlanLine {
    /// `o["symbol"]`.
    pub symbol: String,
    /// `o["action"]`.
    pub action: String,
    /// `o["delta"]` (shares; negative sells).
    pub delta: i64,
    /// `o["ref_price"]`.
    pub ref_price: f64,
    /// The row as built.
    pub row: PyDict,
}

impl PlanLine {
    /// Read the four keys from a plan row; `None` when one is missing or mistyped.
    #[must_use]
    pub fn from_row(row: &PyDict) -> Option<Self> {
        let symbol = match row.get("symbol")? {
            PyValue::Str(s) => s.clone(),
            _ => return None,
        };
        let action = match row.get("action")? {
            PyValue::Str(s) => s.clone(),
            _ => return None,
        };
        let delta = match row.get("delta")? {
            PyValue::Int(i) => *i,
            _ => return None,
        };
        #[allow(clippy::cast_precision_loss)]
        let ref_price = match row.get("ref_price")? {
            PyValue::Float(f) => *f,
            PyValue::Int(i) => *i as f64,
            _ => return None,
        };
        Some(Self {
            symbol,
            action,
            delta,
            ref_price,
            row: row.clone(),
        })
    }

    /// `"SELL" if o["delta"] < 0 else "BUY"`.
    #[must_use]
    pub const fn side(&self) -> &'static str {
        if self.delta < 0 { "SELL" } else { "BUY" }
    }
}

/// `time.time() - plan["created_at"] > 1800`.
#[must_use]
pub fn is_expired(created_at: f64, now: f64) -> bool {
    now - created_at > PLAN_TTL_SECONDS
}

/// A refusal `/execute` answers with (`HTTPException(status, detail)`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refusal {
    /// The HTTP status.
    pub status: u16,
    /// FastAPI's `detail`.
    pub detail: &'static str,
}

/// The confirm gate, in Python's order: 400 without `confirm == "true"`, 404 for an unknown
/// plan, 410 for a plan older than 30 minutes.
///
/// # Errors
/// The [`Refusal`] Python raises.
pub fn check_execute<'a>(
    confirm: &str,
    plan: Option<&'a Plan>,
    now: f64,
) -> Result<&'a Plan, Refusal> {
    if confirm != "true" {
        return Err(Refusal {
            status: 400,
            detail: "Execution requires explicit confirmation.",
        });
    }
    let Some(plan) = plan else {
        return Err(Refusal {
            status: 404,
            detail: "Unknown or expired plan_id \u{2014} re-run Analyze.",
        });
    };
    if is_expired(plan.created_at, now) {
        return Err(Refusal {
            status: 410,
            detail: "Plan older than 30 minutes \u{2014} prices stale, re-run Analyze.",
        });
    }
    Ok(plan)
}

/// `PLANS`: plan id → plan, process-local, as Python's module dict (lost on restart, which is
/// part of the gate: a restarted desk cannot execute yesterday's plan).
#[derive(Debug, Default)]
pub struct PlanStore {
    plans: RwLock<HashMap<String, Plan>>,
}

impl PlanStore {
    /// `PLANS[plan["plan_id"]] = plan`.
    pub fn insert(&self, plan: Plan) {
        if let Ok(mut p) = self.plans.write() {
            p.insert(plan.plan_id.clone(), plan);
        }
    }

    /// `PLANS.get(plan_id)`.
    #[must_use]
    pub fn get(&self, plan_id: &str) -> Option<Plan> {
        self.plans.read().ok().and_then(|p| p.get(plan_id).cloned())
    }
}

/// SHA-256 (hex) of the plan's orjson bytes — the hash `desk_diff.py --plans` compares.
///
/// # Errors
/// A plan orjson cannot serialise.
pub fn plan_digest(plan: &PyValue) -> Result<String, WireError> {
    let text = orjson_dumps(plan)?;
    let digest = Sha256::digest(text.as_bytes());
    Ok(digest.iter().fold(String::with_capacity(64), |mut s, b| {
        use std::fmt::Write as _;
        let _ = write!(s, "{b:02x}");
        s
    }))
}
