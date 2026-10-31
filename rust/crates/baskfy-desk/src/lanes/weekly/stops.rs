//! `app/main.py`'s `GET /stops` (review: builds a plan, places nothing) and `POST /stops/arm`
//! (confirm-gated: cancels first, then one GTT per row through `kite_client.place_gtt_stop`).

use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock, Weak};

use async_trait::async_trait;
use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::State;
use axum::http::{HeaderMap, Response, StatusCode, Uri};
use axum::routing::{get, post};

use super::analyze::{dumps_indent2, form_fields, load_config, missing_fields};
use super::plan_store::py_str;
use crate::app::AppState;
use crate::lanes::analytics::analytics_views::protection::build_stop_plan;
use crate::lanes::analytics::web::{self, MultiDict, detail, internal_error};
use crate::lanes::exec_core::desk::latest_nav;
use crate::lanes::exec_core::exec_kite;
use crate::lanes::exec_core::kite_client::{ClientError, DeskKite};
use crate::lanes::exec_core::ratelimit::{GatewayLimits, KiteLimits};
use crate::lanes::exec_core::risk::risk_config;
use crate::lanes::exec_core::stops::StopConfig;
use crate::lanes::session::telemetry;
use crate::plan::{PlanStore, is_expired};
use crate::wire::{PyDict, PyValue, dict};

/// `STOP_OK`: the statuses `kite_client.place_gtt_stop` returns on success.
pub const STOP_OK: [&str; 2] = ["GTT_PLACED", "DRY_RUN_GTT"];

/// The statuses `kite_client.delete_gtt` returns when a trigger is gone.
pub const DELETE_OK: [&str; 2] = ["GTT_DELETED", "DRY_RUN_GTT_DELETE"];

/// The two routes.
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/stops", crate::lanes::session::get_only(get(page)))
        .route("/stops/arm", post(arm))
}

/// One reviewed plan: `build_stop_plan(...)` plus `created_at`.
#[derive(Debug, Clone)]
pub struct StopPlan {
    /// `plan["plan_id"]`.
    pub plan_id: String,
    /// `plan["created_at"]` (`time.time()` at review).
    pub created_at: f64,
    /// The plan dict.
    pub body: PyDict,
}

/// `STOP_PLANS` and the limiter that paces arming, one per desk.
#[derive(Debug, Default)]
pub struct StopPlans {
    plans: Mutex<HashMap<String, StopPlan>>,
    limits: KiteLimits,
}

impl StopPlans {
    /// `STOP_PLANS[plan_id] = plan`.
    pub fn insert(&self, plan: StopPlan) {
        if let Ok(mut p) = self.plans.lock() {
            p.insert(plan.plan_id.clone(), plan);
        }
    }

    /// `STOP_PLANS.get(plan_id)`.
    #[must_use]
    pub fn get(&self, plan_id: &str) -> Option<StopPlan> {
        self.plans.lock().ok().and_then(|p| p.get(plan_id).cloned())
    }

    /// `STOP_PLANS.pop(plan_id, None)`.
    pub fn pop(&self, plan_id: &str) -> Option<StopPlan> {
        self.plans.lock().ok().and_then(|mut p| p.remove(plan_id))
    }

    /// `STOP_PLANS[plan_id]["created_at"] = at`.
    pub fn set_created_at(&self, plan_id: &str, at: f64) {
        if let Ok(mut p) = self.plans.lock()
            && let Some(plan) = p.get_mut(plan_id)
        {
            plan.created_at = at;
            plan.body.insert("created_at", PyValue::Float(at));
        }
    }

    /// The ids held.
    #[must_use]
    pub fn ids(&self) -> Vec<String> {
        self.plans
            .lock()
            .map(|p| p.keys().cloned().collect())
            .unwrap_or_default()
    }

    /// `STOP_PLANS.clear()`.
    pub fn clear(&self) {
        if let Ok(mut p) = self.plans.lock() {
            p.clear();
        }
    }
}

type Desks = Mutex<Vec<(Weak<PlanStore>, Arc<StopPlans>)>>;

fn desks() -> &'static Desks {
    static DESKS: OnceLock<Desks> = OnceLock::new();
    DESKS.get_or_init(|| Mutex::new(Vec::new()))
}

/// This desk's `STOP_PLANS` (keyed by its `/analyze` store, so two test desks never share one).
#[must_use]
pub fn stop_plans(state: &AppState) -> Arc<StopPlans> {
    let Ok(mut all) = desks().lock() else {
        return Arc::new(StopPlans::default());
    };
    all.retain(|(k, _)| k.strong_count() > 0);
    if let Some((_, s)) = all
        .iter()
        .find(|(k, _)| k.upgrade().is_some_and(|p| Arc::ptr_eq(&p, &state.plans)))
    {
        return s.clone();
    }
    let fresh = Arc::new(StopPlans::default());
    all.push((Arc::downgrade(&state.plans), fresh.clone()));
    fresh
}

/// The two `kite_client` calls arming makes (the documented GTT exception, its own guard).
#[async_trait]
pub trait StopArmer: Send + Sync {
    /// `k.place_gtt_stop(symbol, qty, trigger, last_price)`.
    async fn place_gtt_stop(
        &self,
        symbol: &str,
        qty: i64,
        trigger: f64,
        last_price: f64,
    ) -> Result<PyDict, ClientError>;

    /// `k.delete_gtt(gtt_id, symbol)`.
    async fn delete_gtt(&self, gtt_id: i64, symbol: &str) -> Result<PyDict, ClientError>;
}

#[async_trait]
impl StopArmer for DeskKite {
    async fn place_gtt_stop(
        &self,
        symbol: &str,
        qty: i64,
        trigger: f64,
        last_price: f64,
    ) -> Result<PyDict, ClientError> {
        Self::place_gtt_stop(self, symbol, qty, trigger, last_price, "NSE").await
    }

    async fn delete_gtt(&self, gtt_id: i64, symbol: &str) -> Result<PyDict, ClientError> {
        Self::delete_gtt(self, gtt_id, symbol).await
    }
}

fn band_of(cfg: &super::basket::BasketConfig) -> StopConfig {
    StopConfig {
        stop_min: cfg.scoring.stop_min,
        stop_max: cfg.scoring.stop_max,
        stop_vol_mult: cfg.scoring.stop_vol_mult,
    }
}

fn list_of(v: Option<&PyValue>) -> Vec<PyValue> {
    match v {
        Some(PyValue::List(l)) => l.clone(),
        _ => Vec::new(),
    }
}

fn field<'a>(d: &'a PyValue, k: &str) -> &'a PyValue {
    match d {
        PyValue::Map(m) => m.get(k).unwrap_or(&PyValue::None),
        _ => &PyValue::None,
    }
}

fn as_f64(v: &PyValue) -> f64 {
    match v {
        PyValue::Float(f) => *f,
        #[allow(clippy::cast_precision_loss)]
        PyValue::Int(i) => *i as f64,
        _ => 0.0,
    }
}

const fn as_i64(v: &PyValue) -> Option<i64> {
    match v {
        PyValue::Int(i) => Some(*i),
        _ => None,
    }
}

/// Holdings, triggers, then the plan; or the page's `problem`.
async fn review(state: &AppState, band: StopConfig) -> Result<PyDict, String> {
    let k = exec_kite::kite(state)
        .map_err(|e| format!("Could not read holdings or triggers: {}", e.message))?;
    if !k.is_authed().await {
        return Err(
            "Kite session expired \u{2014} log in first. Stops are sized from live prices."
                .to_owned(),
        );
    }
    let holdings = k
        .holdings()
        .await
        .map_err(|e| format!("Could not read holdings or triggers: {}", e.message))?;
    let gtts = match state.kite.get_gtts().await {
        Ok(PyValue::List(l)) => l,
        Ok(_) => Vec::new(),
        Err(e) => return Err(format!("Could not read holdings or triggers: {e}")),
    };
    let held: Vec<PyValue> = holdings.into_iter().map(PyValue::Map).collect();
    let plan_id: String = state.ids.uuid4_hex().chars().take(12).collect();
    Ok(build_stop_plan(&held, &gtts, &[], &plan_id, band))
}

/// `GET /stops?armed=&error=`: review the stops that would be armed; places nothing.
async fn page(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let q = MultiDict::from_query(&uri);
    let (cfg, _) = load_config(&state).await;
    let band = band_of(&cfg);
    let (plan, rev, problem) = match review(&state, band).await {
        Ok(mut plan) => {
            let created_at = state.clock.time();
            plan.insert("created_at", PyValue::Float(created_at));
            let rev = plan.get("review").cloned().unwrap_or(PyValue::None);
            stop_plans(&state).insert(StopPlan {
                plan_id: py_str(plan.get("plan_id").unwrap_or(&PyValue::None)),
                created_at,
                body: plan.clone(),
            });
            (PyValue::Map(plan), rev, String::new())
        }
        Err(problem) => (PyValue::None, PyValue::None, problem),
    };
    let settings = state.settings_snapshot();
    let cap = match risk_config(latest_nav(&state.db).await, &settings) {
        Ok(c) => c.max_daily_loss,
        Err(e) => return internal_error(&e.to_string()),
    };
    web::render(
        &state,
        uri.path(),
        "stops.html",
        dict([
            ("plan", plan),
            ("review", rev),
            ("problem", PyValue::Str(problem)),
            ("dry_run", PyValue::Bool(settings.dry_run)),
            ("armed", PyValue::Str(q.str_or_empty("armed"))),
            ("error", PyValue::Str(q.str_or_empty("error"))),
            (
                "band",
                PyValue::List(vec![
                    PyValue::Float(band.stop_min * 100.0),
                    PyValue::Float(band.stop_max * 100.0),
                ]),
            ),
            ("daily_loss_cap", PyValue::Float(cap)),
        ]),
    )
}

/// The confirm gate, in Python's order: 400, 404, 410.
fn gate(state: &AppState, plan_id: &str, confirm: &str) -> Result<StopPlan, Box<Response<Body>>> {
    if confirm != "true" {
        return Err(Box::new(detail(
            StatusCode::BAD_REQUEST,
            "Arming stops requires explicit confirmation.",
        )));
    }
    let Some(plan) = stop_plans(state).get(plan_id) else {
        return Err(Box::new(detail(
            StatusCode::NOT_FOUND,
            "Unknown or expired plan \u{2014} reload the review page.",
        )));
    };
    if is_expired(plan.created_at, state.clock.time()) {
        return Err(Box::new(detail(
            StatusCode::GONE,
            "Plan older than 30 minutes \u{2014} prices stale, reload it.",
        )));
    }
    Ok(plan)
}

/// `POST /stops/arm` (form `plan_id`, `confirm`).
async fn arm(State(state): State<AppState>, headers: HeaderMap, body: Bytes) -> Response<Body> {
    let form = form_fields(&headers, &body);
    let (Some(plan_id), Some(confirm)) = (form.get("plan_id"), form.get("confirm")) else {
        let missing: Vec<&str> = ["plan_id", "confirm"]
            .into_iter()
            .filter(|n| form.get(n).is_none())
            .collect();
        return missing_fields("body", &missing);
    };
    let plan = match gate(&state, plan_id, confirm) {
        Ok(p) => p,
        Err(r) => return *r,
    };
    let k = match exec_kite::kite(&state) {
        Ok(k) => k,
        Err(e) => return internal_error(&e.to_string()),
    };
    arm_plan(&state, &plan, k.as_ref()).await
}

/// The gate, then [`arm_plan`] over `armer` (the route's seam: `DeskKite` in production).
pub async fn arm_with(
    state: &AppState,
    plan_id: &str,
    confirm: &str,
    armer: &dyn StopArmer,
) -> Response<Body> {
    match gate(state, plan_id, confirm) {
        Ok(plan) => arm_plan(state, &plan, armer).await,
        Err(r) => *r,
    }
}

fn outcome_of(res: &PyDict) -> String {
    match res.get("status") {
        Some(v) if v.truthy() => py_str(v),
        _ => "UNKNOWN".to_owned(),
    }
}

/// Cancels first, then one stop per row; single use; the outcome as a 303 back to `/stops`.
pub async fn arm_plan(state: &AppState, plan: &StopPlan, armer: &dyn StopArmer) -> Response<Body> {
    let desk = stop_plans(state);
    let cancels = list_of(plan.body.get("cancels"));
    let rows = list_of(plan.body.get("rows"));

    let mut cancelled: Vec<PyDict> = Vec::new();
    for c in &cancels {
        desk.limits.api_slot().await;
        let symbol = py_str(field(c, "symbol"));
        let gtt_id = field(c, "gtt_id").clone();
        let tried = match as_i64(&gtt_id) {
            Some(id) => armer.delete_gtt(id, &symbol).await,
            None => Err(ClientError {
                python_type: "TypeError".to_owned(),
                message: format!("gtt_id must be an int, not {}", py_str(&gtt_id)),
            }),
        };
        let mut res = tried.unwrap_or_else(|e| {
            dict([
                ("symbol", PyValue::Str(symbol.clone())),
                ("gtt_id", gtt_id.clone()),
                ("status", PyValue::Str("GTT_DELETE_ERROR".to_owned())),
                ("error", PyValue::Str(e.message)),
            ])
        });
        res.insert("reason", field(c, "reason").clone());
        cancelled.push(res);
        telemetry::count("gtt", &[("outcome", "CANCELLED")]);
    }

    let mut placed: Vec<PyDict> = Vec::new();
    for r in &rows {
        desk.limits.api_slot().await;
        let symbol = py_str(field(r, "symbol"));
        let size = as_i64(field(r, "qty")).unwrap_or(0);
        let trigger = as_f64(field(r, "trigger"));
        let last = as_f64(field(r, "last_price"));
        let mut res = match armer.place_gtt_stop(&symbol, size, trigger, last).await {
            Ok(d) => d,
            Err(e) if e.python_type == "UntouchableInstrumentError" => dict([
                ("symbol", PyValue::Str(symbol.clone())),
                ("status", PyValue::Str("BLOCKED".to_owned())),
                ("error", PyValue::Str(e.message)),
            ]),
            Err(e) => dict([
                ("symbol", PyValue::Str(symbol.clone())),
                ("status", PyValue::Str("FAILED".to_owned())),
                ("error", PyValue::Str(e.message)),
            ]),
        };
        res.insert("qty", field(r, "qty").clone());
        res.insert("trigger", field(r, "trigger").clone());
        let outcome = outcome_of(&res);
        placed.push(res);
        telemetry::count("gtt", &[("outcome", outcome.as_str())]);
    }

    desk.pop(&plan.plan_id);
    let log_path = format!("data/outputs/stops_{}.json", plan.plan_id);
    let as_list = |v: &[PyDict]| PyValue::List(v.iter().cloned().map(PyValue::Map).collect());
    let log = PyValue::Map(dict([
        ("plan", PyValue::List(rows)),
        ("cancels", PyValue::List(cancels)),
        ("cancelled", as_list(&cancelled)),
        ("results", as_list(&placed)),
    ]));
    if let Err(e) = std::fs::write(state.paths.resolve(&log_path), dumps_indent2(&log)) {
        return internal_error(&format!("FileNotFoundError: {e}"));
    }

    let is_ok = |p: &PyDict| {
        let s = p.get("status").map(py_str).unwrap_or_default();
        STOP_OK.contains(&s.as_str())
    };
    let ok = placed.iter().filter(|p| is_ok(p)).count();
    let failed: Vec<&PyDict> = placed.iter().filter(|p| !is_ok(p)).collect();
    let dry_run = state.settings_snapshot().dry_run;
    let mut msg = format!(
        "{ok} of {} stops {}",
        placed.len(),
        if dry_run { "simulated" } else { "armed" }
    );
    if !cancelled.is_empty() {
        let gone = cancelled
            .iter()
            .filter(|c| {
                let s = c
                    .get("status")
                    .map(py_str)
                    .unwrap_or_else(|| "None".to_owned());
                DELETE_OK.contains(&s.as_str())
            })
            .count();
        msg = format!(
            "{gone} of {} wrong triggers cancelled \u{b7} {msg}",
            cancelled.len()
        );
    }
    if !failed.is_empty() {
        let named: Vec<String> = failed
            .iter()
            .take(3)
            .map(|p| {
                let why = p.get("error").map_or_else(|| "unknown".to_owned(), py_str);
                format!(
                    "{} ({})",
                    p.get("symbol").map(py_str).unwrap_or_default(),
                    why.chars().take(60).collect::<String>()
                )
            })
            .collect();
        msg.push_str(&format!(
            " \u{b7} {} failed: {}",
            failed.len(),
            named.join(", ")
        ));
    }
    web::redirect_303(&format!("/stops?armed={msg}"))
}
