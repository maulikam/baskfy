//! Lane **twt** — the three-weeks-tight sleeve: page, confirm, halt, re-arm, sweep, reconcile,
//! scan, and `twt-auto` (TW17–TW20 incl. LIVE plans and `SELL_AT_OPEN`; flag default false).
//! Owned by the twt-vbt lane (which also owns `vbt.rs`).
//!
//! **Python it ports:** `app/twt_desk.py` (this file and [`store`]), `app/twt_execute.py`
//! ([`execute`]), `app/twt_auto.py` ([`auto`]); [`common`] holds what TWT and VBT share
//! (`_jsonable`, the row helpers, `app.reconcile`'s entry refusal, `app.exit_rules`).
//!
//! **Routes:** `GET /twt`, `GET /twt/data`, `POST /twt/scan`, `GET /twt/scan/{run_id}`,
//! `POST /twt/execute`, `POST /twt/halt`, `POST /twt/rearm`, `POST /twt/sweep`,
//! `POST /twt/reconcile`. **Service:** `twt-auto` (`python -m app.twt_auto`).
//!
//! **The order path.** Every line goes through this sleeve's **own** gateway —
//! [`crate::gateway::Gateway::derive_sleeve`] with [`execute::twt_gates`] (intraday and options
//! off unconditionally, simulated unless `DRY_RUN=false` **and**
//! `BASKFY_TWT_EXECUTION_ENABLED=true`), `twt_orders_journal.jsonl` and the 0.5–30 % band —
//! built once per process, as `twt_desk.twt_gateway()` is. Until the exec_core lane implements
//! `derive_sleeve` the route refuses every send (fail closed), and nothing reaches a broker.

pub mod auto;
pub mod auto_loop;
pub mod common;
pub mod execute;
pub mod store;
pub mod twt_exec;

use std::sync::{Arc, Mutex};

use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::{Path as AxPath, State};
use axum::http::{Response, Uri};
use axum::routing::{get, post};
use chrono::{DateTime, Duration, FixedOffset, NaiveTime};
use rust_decimal::Decimal;

use crate::app::AppState;
use crate::gateway::{
    CancelOrder, DeleteGtt, DerivedGateway, Gateway, GatewayError, GttStop, ModifyGtt, PlaceOrder,
    SleeveGatewaySpec,
};
use crate::storage::StorageError;
use crate::wire::{PyDict, PyValue};

use common::{
    FormFields, HttpError, Rec, Val, html_response, json_response, jsonable, jsonable_rec,
    last_price, path_int, server_error, template_value, to_ist,
};
use execute::{ExecError, ExecOutcome, twt_gates};
use store::{PgTwtStore, StoreError, TwtStore};

/// `KIND_LABEL`.
pub const KIND_LABEL: [(&str, &str); 4] = [
    ("BUY_AT_OPEN", "TWT BUY AT OPEN"),
    ("ARM_GTT", "TWT ARM STOP"),
    ("RAISE_GTT_STOP", "TWT RAISE STOP"),
    ("SELL_AT_OPEN", "TWT SELL AT OPEN"),
];

/// `PLAN_TTL_MINUTES`.
pub const PLAN_TTL_MINUTES: i64 = 30;

/// `SCAN_STALE_AFTER_SECONDS` (mirrored from `baskfy_worker.tasks.twt_scan`).
pub const SCAN_STALE_AFTER_SECONDS: i64 = 600;

/// `SCAN_MIN_INTERVAL_SECONDS`.
pub const SCAN_MIN_INTERVAL_SECONDS: i64 = 60;

/// `SWEEP_AT`: 15:15 IST.
#[must_use]
pub fn sweep_at() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 15, 0).unwrap_or_default()
}

/// The lane's routes.
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/twt", get(twt_page))
        .route("/twt/data", get(twt_data))
        .route("/twt/scan", post(twt_scan_now))
        .route("/twt/scan/{run_id}", get(twt_scan_status))
        .route("/twt/execute", post(twt_execute_line))
        .route("/twt/halt", post(twt_halt))
        .route("/twt/rearm", post(twt_rearm))
        .route("/twt/sweep", post(twt_sweep))
        .route("/twt/reconcile", post(twt_reconcile))
}

/// The lane's template globals and filters (none).
pub fn register_templates(templates: &mut crate::templates::Templates) {
    let _ = templates;
}

/// Run one of the lane's background services by name; `None` if the name is not this lane's.
pub async fn run_service(name: &str, state: &AppState) -> Option<Result<(), String>> {
    match name {
        "twt-auto" => Some(auto::main(state).await),
        "twt-auto-loop" => {
            let mut seams = crate::lanes::service_loop::ProcessSeams {
                clock: Arc::clone(&state.clock),
            };
            let program = crate::lanes::service_loop::program();
            Some(
                crate::lanes::service_loop::blocking(move || {
                    auto_loop::run_forever(&mut seams, &program, None);
                })
                .await,
            )
        }
        _ => None,
    }
}

// =============================================================================================
// Wiring: the store, the gateway, the clock
// =============================================================================================

/// `open_store()`: the sole user's store over the desk's connection.
///
/// # Errors
/// The connection.
pub async fn open_store(state: &AppState) -> Result<PgTwtStore, StorageError> {
    let s = state.settings_snapshot();
    let conn = state.db.connect().await?;
    let schema = if state.db.config().is_postgres() {
        "public"
    } else {
        ""
    };
    Ok(PgTwtStore::new(
        conn,
        s.sole_user_id,
        schema,
        Some(s.sole_broker_account_id),
    ))
}

/// A gateway that refuses every call: what a sleeve gets when the base gateway cannot derive
/// one (fail closed — nothing is sent, nothing is journalled).
#[derive(Debug, Clone, Copy)]
pub struct NoSleeveGateway(pub &'static str);

fn not_derived(sleeve: &str, what: &str) -> GatewayError {
    GatewayError {
        python_type: "NotPorted".to_owned(),
        message: format!(
            "the {sleeve} sleeve's gateway cannot be built here ({what}); nothing was sent"
        ),
    }
}

#[async_trait::async_trait]
impl Gateway for NoSleeveGateway {
    async fn place(&self, _order: PlaceOrder) -> Result<PyDict, GatewayError> {
        Err(not_derived(self.0, "place"))
    }
    async fn place_gtt_stop(&self, _stop: GttStop) -> Result<PyDict, GatewayError> {
        Err(not_derived(self.0, "place_gtt_stop"))
    }
    async fn delete_gtt(&self, _req: DeleteGtt) -> Result<PyDict, GatewayError> {
        Err(not_derived(self.0, "delete_gtt"))
    }
    async fn modify_gtt_quantity(&self, _req: ModifyGtt) -> Result<PyDict, GatewayError> {
        Err(not_derived(self.0, "modify_gtt_quantity"))
    }
    async fn cancel_order(&self, _req: CancelOrder) -> Result<PyDict, GatewayError> {
        Err(not_derived(self.0, "cancel_order"))
    }
}

type GatewayCache = Mutex<Vec<(std::sync::Weak<dyn Gateway>, &'static str, Arc<dyn Gateway>)>>;

static SLEEVE_GATEWAYS: GatewayCache = Mutex::new(Vec::new());

/// A sleeve's own gateway, built once per base gateway (the idempotency map is per instance,
/// as Python's `_twt_gateway`/`_vbt_gateway` globals are).
pub(crate) fn sleeve_gateway(
    state: &AppState,
    sleeve: &'static str,
    journal_name: &str,
    stop_band: crate::gateway::StopBand,
    gates: fn(&crate::settings::Settings) -> crate::gateway::ProductGates,
) -> Arc<dyn Gateway> {
    let key = Arc::downgrade(&state.gateway);
    if let Ok(mut cache) = SLEEVE_GATEWAYS.lock() {
        cache.retain(|(w, _, _)| w.strong_count() > 0);
        if let Some((_, _, gw)) = cache
            .iter()
            .find(|(w, s, _)| w.ptr_eq(&key) && *s == sleeve)
        {
            return gw.clone();
        }
    }
    let settings = state.settings.clone();
    let journal = std::path::Path::new(crate::gateway::JOURNAL)
        .parent()
        .map(|p| p.join(journal_name))
        .unwrap_or_else(|| std::path::PathBuf::from(journal_name));
    let spec = SleeveGatewaySpec {
        derived: DerivedGateway {
            gates: Arc::new(move || {
                let s = match settings.read() {
                    Ok(s) => s.clone(),
                    Err(p) => p.into_inner().clone(),
                };
                gates(&s)
            }),
            journal_path: state.paths.resolve(&journal.to_string_lossy()),
            fo_coverage: false,
        },
        stop_band,
    };
    let Some(gw) = state.gateway.derive_sleeve(spec) else {
        return Arc::new(NoSleeveGateway(sleeve));
    };
    if let Ok(mut cache) = SLEEVE_GATEWAYS.lock() {
        cache.push((key, sleeve, gw.clone()));
    }
    gw
}

/// `twt_gateway()`.
pub fn twt_gateway(state: &AppState) -> Arc<dyn Gateway> {
    let band = execute::twt_stop_band().unwrap_or_default();
    sleeve_gateway(
        state,
        "twt",
        execute::TWT_JOURNAL_NAME,
        band,
        execute::twt_gates,
    )
}

/// `twt_gateway()` as the routes call it: Python builds it through `app.main.gateway()`, which
/// builds the process's shared risk manager as a side effect (on Postgres: the day's
/// `risk_ledger` row) before the sleeve's gateway is derived; the same happens here through the
/// exec_core lane's shared risk manager. The sleeve's gateway itself is [`twt_gateway`].
pub async fn twt_gateway_ready(state: &AppState) -> Arc<dyn Gateway> {
    if let Some(desk) = state
        .gateway
        .as_any()
        .and_then(|a| a.downcast_ref::<crate::lanes::exec_core::desk::DeskGateway>())
    {
        let _ = desk.risk().await;
    }
    twt_gateway(state)
}

/// `_now()`: `datetime.now(IST)`.
#[must_use]
pub fn now_ist(state: &AppState) -> DateTime<FixedOffset> {
    state.clock.now_ist()
}

/// `str(exc)` for a store failure, as the page shows it (`[:200]`).
#[must_use]
pub fn error_text(e: &StoreError) -> String {
    let text = match e {
        StoreError::Storage(StorageError::Driver(sqlx::Error::Database(db))) => {
            db.message().to_owned()
        }
        other => other.to_string(),
    };
    text.chars().take(200).collect()
}

// =============================================================================================
// The view
// =============================================================================================

fn gates_rec(s: &crate::settings::Settings) -> Rec {
    Rec::of([
        ("dry_run", Val::Bool(twt_gates(s).dry_run)),
        ("execution_enabled", Val::Bool(s.twt_execution_enabled)),
        ("desk_dry_run", Val::Bool(s.dry_run)),
    ])
}

fn labels() -> Val {
    Val::Map(Rec::of(KIND_LABEL.iter().map(|(k, v)| (*k, Val::from(*v)))))
}

fn label_of(kind: &str) -> String {
    KIND_LABEL
        .iter()
        .find(|(k, _)| *k == kind)
        .map_or_else(|| kind.to_owned(), |(_, v)| (*v).to_owned())
}

/// `_countdown(plan, now)`.
#[must_use]
pub fn countdown(plan: Option<&Rec>, now: &DateTime<FixedOffset>) -> String {
    let Some(expires) = plan.and_then(|p| p.stamp("expires_at")) else {
        return String::new();
    };
    let left = store::micros_to_secs(expires - *now);
    if left <= 0.0 {
        return format!(
            "expired at {} \u{2014} rebuild it (make twt-plan SOURCE=MORNING) and read it again",
            to_ist(&expires).format("%H:%M")
        );
    }
    #[allow(clippy::cast_possible_truncation)]
    let (mins, secs) = ((left / 60.0).floor() as i64, left.rem_euclid(60.0) as i64);
    format!("{mins} min {secs} s left")
}

/// `_decorated(line, expired=...)`.
fn decorated(line: &Rec, expired: bool) -> Rec {
    let mut out = line.clone();
    out.set("label", Val::Str(label_of(&line.s("kind"))));
    out.set("expired", Val::Bool(expired));
    out.set(
        "confirmable",
        Val::Bool(!expired && line.s("state") == "PROPOSED"),
    );
    out
}

/// `build_view(store, now=...)`: everything `05` §2's page shows. Reads only.
///
/// # Errors
/// The store.
pub async fn build_view(
    store: &mut PgTwtStore,
    settings: &crate::settings::Settings,
    now: DateTime<FixedOffset>,
) -> Result<Rec, StoreError> {
    let today = to_ist(&now).date_naive();
    let plan = store.todays_plan(today).await?;
    let expired = plan
        .as_ref()
        .and_then(|p| p.stamp("expires_at"))
        .is_some_and(|e| e <= now);
    let lines: Vec<Rec> = match &plan {
        Some(p) => store
            .lines_for(p.int("id"))
            .await?
            .iter()
            .map(|l| decorated(l, expired))
            .collect(),
        None => Vec::new(),
    };
    let skips = match &plan {
        Some(p) => store.skips_for(p.int("id")).await?,
        None => Vec::new(),
    };
    let book = store.open_positions().await?;
    let naked: Vec<Val> = book
        .iter()
        .filter(|r| r.is_none("gtt_id") && r.int("quantity_open") > 0)
        .map(|r| Val::Str(r.s("symbol")))
        .collect();
    let session_date = plan
        .as_ref()
        .and_then(|p| p.date("session_date"))
        .unwrap_or(today);
    let gates = gates_rec(settings);
    let config = store.config().await?;
    let breadth = store.breadth(session_date).await?;
    let session = store.session(session_date).await?;
    let money = store.sleeve_money(session_date).await?;
    let exits: Vec<Val> = lines
        .iter()
        .filter(|l| matches!(l.s("kind").as_str(), "ARM_GTT" | "RAISE_GTT_STOP"))
        .cloned()
        .map(Val::Map)
        .collect();
    let entries: Vec<Val> = lines
        .iter()
        .filter(|l| l.s("kind") == "BUY_AT_OPEN")
        .cloned()
        .map(Val::Map)
        .collect();
    let first_left = config.val("first_live_entries_left");
    let mut v = Rec::of([
        ("available", Val::Bool(true)),
        ("now", Val::Stamp(now)),
        ("today", Val::Date(today)),
        ("plan", plan.clone().map_or(Val::None, Val::Map)),
        ("countdown", Val::Str(countdown(plan.as_ref(), &now))),
        ("expired", Val::Bool(expired)),
        (
            "lines",
            Val::List(lines.iter().cloned().map(Val::Map).collect()),
        ),
        ("exits", Val::List(exits)),
        ("entries", Val::List(entries)),
        ("labels", labels()),
        (
            "skips",
            Val::List(skips.into_iter().map(Val::Map).collect()),
        ),
        ("book", Val::List(book.into_iter().map(Val::Map).collect())),
        ("exit_rule", Val::Str(common::twt_exit_rule())),
        ("naked", Val::List(naked)),
        ("breadth", breadth.map_or(Val::None, Val::Map)),
        ("session", session.map_or(Val::None, Val::Map)),
        ("config", Val::Map(config)),
        ("money", Val::Map(money.as_rec())),
        ("first_live_entries_left", first_left),
        (
            "max_new_entries_per_session",
            Val::Int(
                baskfy_core::twt::config::TwtConfig::default()
                    .sizing
                    .max_new_entries_per_session,
            ),
        ),
        ("sweep_due", Val::Bool(to_ist(&now).time() >= sweep_at())),
        ("plan_ttl_minutes", Val::Int(PLAN_TTL_MINUTES)),
    ]);
    v.update(&gates);
    v.set("reason", Val::Str(String::new()));
    Ok(v)
}

/// `unavailable_view(reason, now=...)`: a page that says why it is empty.
#[must_use]
pub fn unavailable_view(
    reason: &str,
    settings: &crate::settings::Settings,
    now: DateTime<FixedOffset>,
) -> Rec {
    let mut v = Rec::of([
        ("available", Val::Bool(false)),
        ("now", Val::Stamp(now)),
        ("today", Val::Date(to_ist(&now).date_naive())),
        ("plan", Val::None),
        ("countdown", Val::Str(String::new())),
        ("expired", Val::Bool(false)),
        ("lines", Val::List(Vec::new())),
        ("exits", Val::List(Vec::new())),
        ("entries", Val::List(Vec::new())),
        ("labels", labels()),
        ("skips", Val::List(Vec::new())),
        ("book", Val::List(Vec::new())),
        ("naked", Val::List(Vec::new())),
        ("breadth", Val::None),
        ("session", Val::None),
        ("config", Val::Map(Rec::new())),
        ("money", Val::Map(Rec::new())),
        ("first_live_entries_left", Val::Int(0)),
        (
            "max_new_entries_per_session",
            Val::Int(
                baskfy_core::twt::config::TwtConfig::default()
                    .sizing
                    .max_new_entries_per_session,
            ),
        ),
        ("sweep_due", Val::Bool(false)),
        ("plan_ttl_minutes", Val::Int(PLAN_TTL_MINUTES)),
    ]);
    v.update(&gates_rec(settings));
    v.set("reason", Val::Str(reason.to_owned()));
    v
}

async fn view(state: &AppState) -> Rec {
    let now = now_ist(state);
    let settings = state.settings_snapshot();
    let built = match open_store(state).await {
        Ok(mut store) => build_view(&mut store, &settings, now).await,
        Err(e) => Err(StoreError::Storage(e)),
    };
    built.unwrap_or_else(|e| unavailable_view(&error_text(&e), &settings, now))
}

/// Render a desk page with `request` and `v`, as `TemplateResponse(request, name, {"v": v})`.
pub(crate) fn render_page(state: &AppState, uri: &Uri, name: &str, v: &Rec) -> Response<Body> {
    let request = minijinja::Value::from_pairs(vec![(
        "url".to_owned(),
        minijinja::Value::from_pairs(vec![(
            "path".to_owned(),
            minijinja::Value::from(uri.path().to_owned()),
        )]),
    )]);
    let ctx = minijinja::Value::from_pairs(vec![
        ("request".to_owned(), request),
        ("v".to_owned(), template_value(&Val::Map(v.clone()))),
    ]);
    match state.templates.render(name, ctx) {
        Ok(text) => html_response(text),
        Err(_) => server_error(),
    }
}

fn ok_json(rec: &Rec) -> Response<Body> {
    json_response(200, &PyValue::Map(jsonable_rec(rec)))
}

/// `_outcome_json(outcome, **extra)`.
fn outcome_json(outcome: &ExecOutcome, extra: &[(&str, Val)]) -> Rec {
    let mut r = outcome.as_rec();
    for (k, v) in extra {
        r.set(*k, v.clone());
    }
    r
}

fn exec_error_response(e: &ExecError) -> Response<Body> {
    match e {
        ExecError::Http(h) => h.response(),
        _ => server_error(),
    }
}

// =============================================================================================
// Routes
// =============================================================================================

async fn twt_page(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let v = view(&state).await;
    render_page(&state, &uri, "twt.html", &v)
}

async fn twt_data(State(state): State<AppState>) -> Response<Body> {
    let v = view(&state).await;
    ok_json(&v)
}

async fn twt_scan_now(State(state): State<AppState>) -> Response<Body> {
    let now = now_ist(&state);
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    match store
        .request_scan(
            now,
            Duration::seconds(SCAN_MIN_INTERVAL_SECONDS),
            Duration::seconds(SCAN_STALE_AFTER_SECONDS),
            "desk",
        )
        .await
    {
        Ok(run_id) => json_response(
            202,
            &PyValue::Map(jsonable_rec(&Rec::of([
                ("run_id", Val::Int(run_id)),
                ("status", Val::from("QUEUED")),
                ("requested_at", Val::Stamp(now)),
            ]))),
        ),
        Err(StoreError::Scan(refused)) => {
            let mut err = HttpError::new(refused.status, refused.reason);
            if let Some(after) = refused.retry_after.filter(|a| *a != 0) {
                err.headers
                    .push(("Retry-After".to_owned(), after.to_string()));
            }
            err.response()
        }
        Err(_) => server_error(),
    }
}

async fn twt_scan_status(
    State(state): State<AppState>,
    AxPath(raw): AxPath<String>,
) -> Response<Body> {
    let run_id = match path_int("run_id", &raw) {
        Ok(id) => id,
        Err(r) => return *r,
    };
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    match store.scan_run(run_id).await {
        Ok(Some(run)) => ok_json(&run),
        Ok(None) => HttpError::new(404, format!("No scan {run_id}.")).response(),
        Err(_) => server_error(),
    }
}

fn refused_response(state: &AppState, e: &ExecError, extra: (&str, i64)) -> Option<Response<Body>> {
    if !e.is_untouchable() {
        return None;
    }
    let ExecError::Gateway(g) = e else {
        return None;
    };
    let mut r = execute::refused_rec(&g.message, twt_gates(&state.settings_snapshot()).dry_run);
    r.set(extra.0, Val::Int(extra.1));
    Some(ok_json(&r))
}

async fn twt_execute_line(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let mut form = FormFields::parse(&body);
    let plan_id = form.str("plan_id");
    let line_id = form.int("line_id");
    let confirm = form.str("confirm");
    if let Some(r) = form.finish() {
        return r;
    }
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    let line = match store.line(line_id).await {
        Ok(l) => l,
        Err(_) => return server_error(),
    };
    let price = match &line {
        Some(l) => last_price(&state, &l.s("symbol")).await,
        None => None,
    };
    let gateway = twt_gateway_ready(&state).await;
    let settings = state.settings_snapshot();
    match execute::execute_line(
        &mut store,
        gateway.as_ref(),
        &settings,
        &plan_id,
        line_id,
        &confirm,
        now_ist(&state),
        price,
    )
    .await
    {
        Ok(outcome) => ok_json(&outcome_json(&outcome, &[("line_id", Val::Int(line_id))])),
        Err(e) => refused_response(&state, &e, ("line_id", line_id))
            .unwrap_or_else(|| exec_error_response(&e)),
    }
}

async fn twt_halt(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let mut form = FormFields::parse(&body);
    let confirm = form.str("confirm");
    if let Some(r) = form.finish() {
        return r;
    }
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    match execute::halt_sleeve(&mut store, &confirm, now_ist(&state)).await {
        Ok(report) => ok_json(&Rec::of([
            ("halted", Val::Bool(report.halted)),
            (
                "sleeve_capital_inr_before",
                Val::from(report.sleeve_capital_inr_before),
            ),
            ("plans_expired", Val::Int(report.plans_expired)),
            ("protection_untouched", Val::Bool(true)),
            ("message", Val::Str(report.line())),
        ])),
        Err(e) => exec_error_response(&e),
    }
}

async fn twt_rearm(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let mut form = FormFields::parse(&body);
    let position_id = form.int("position_id");
    let confirm = form.str("confirm");
    if let Some(r) = form.finish() {
        return r;
    }
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    let position = match store.position(position_id).await {
        Ok(p) => p,
        Err(_) => return server_error(),
    };
    let price = match &position {
        Some(p) => last_price(&state, &p.s("symbol")).await,
        None => None,
    };
    let gateway = twt_gateway_ready(&state).await;
    let settings = state.settings_snapshot();
    match execute::rearm_gtt(
        &mut store,
        gateway.as_ref(),
        &settings,
        position_id,
        &confirm,
        now_ist(&state),
        price,
    )
    .await
    {
        Ok(outcome) => ok_json(&outcome_json(
            &outcome,
            &[("position_id", Val::Int(position_id))],
        )),
        Err(e) => refused_response(&state, &e, ("position_id", position_id))
            .unwrap_or_else(|| exec_error_response(&e)),
    }
}

async fn prices_for(state: &AppState, book: &[Rec]) -> Vec<(String, Decimal)> {
    let mut out: Vec<(String, Decimal)> = Vec::new();
    for row in book {
        let symbol = row.s("symbol");
        if let Some(p) = last_price(state, &symbol).await {
            if let Some(slot) = out.iter_mut().find(|(s, _)| *s == symbol) {
                slot.1 = p;
            } else {
                out.push((symbol, p));
            }
        }
    }
    out
}

async fn twt_sweep(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let mut form = FormFields::parse(&body);
    let confirm = form.str("confirm");
    if let Some(r) = form.finish() {
        return r;
    }
    if confirm.to_lowercase() != "true" {
        return HttpError::new(400, "The 15:15 sweep requires explicit confirmation.").response();
    }
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    let gateway = twt_gateway_ready(&state).await;
    let now = now_ist(&state);
    let naked = match execute::naked_positions(&mut store).await {
        Ok(n) => n,
        Err(e) => return exec_error_response(&e),
    };
    let prices = prices_for(&state, &naked).await;
    let settings = state.settings_snapshot();
    match execute::sweep_naked(&mut store, gateway.as_ref(), &settings, now, &prices).await {
        Ok(report) => ok_json(&Rec::of([
            ("naked_before", Val::Int(report.naked_before)),
            ("rearmed", Val::Int(report.rearmed)),
            ("naked", Val::Int(report.naked)),
            (
                "outcomes",
                Val::List(
                    report
                        .outcomes
                        .iter()
                        .map(|o| Val::Map(outcome_json(o, &[])))
                        .collect(),
                ),
            ),
        ])),
        Err(e) => exec_error_response(&e),
    }
}

/// `KiteGtts(kite).list_gtts()`: the broker's resting triggers, as a read. `None` when the
/// broker will not answer (the reconcile then attaches nothing).
pub async fn list_gtts(state: &AppState) -> Option<Vec<Rec>> {
    let triggers = state.kite.get_gtts().await.ok()?;
    let items = match &triggers {
        PyValue::List(items) => items.clone(),
        PyValue::None => Vec::new(),
        _ => return None,
    };
    let mut out = Vec::with_capacity(items.len());
    for trigger in &items {
        let PyValue::Map(t) = trigger else {
            return None;
        };
        let condition = match t.get("condition") {
            Some(PyValue::Map(c)) => c.clone(),
            _ => PyDict::new(),
        };
        let first = match condition.get("trigger_values") {
            Some(PyValue::List(v)) => v.first().cloned(),
            _ => None,
        };
        let get = |m: &PyDict, k: &str| m.get(k).cloned().map_or(Val::None, Val::Py);
        out.push(Rec::of([
            ("gtt_id", get(t, "id")),
            ("symbol", get(&condition, "tradingsymbol")),
            ("trigger", first.map_or(Val::None, Val::Py)),
            ("status", get(t, "status")),
        ]));
    }
    Some(out)
}

async fn twt_reconcile(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let mut form = FormFields::parse(&body);
    let confirm = form.str("confirm");
    if let Some(r) = form.finish() {
        return r;
    }
    if confirm.to_lowercase() != "true" {
        return HttpError::new(400, "Reconciling stops requires explicit confirmation.").response();
    }
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    let gtts = list_gtts(&state).await;
    match execute::reconcile_gtts(&mut store, gtts, now_ist(&state)).await {
        Ok(report) => ok_json(&Rec::of([
            ("attached", Val::Int(report.attached)),
            ("unmatched", Val::Int(report.unmatched)),
            (
                "positions",
                Val::List(report.positions.iter().map(|p| Val::Int(*p)).collect()),
            ),
        ])),
        Err(e) => exec_error_response(&e),
    }
}

/// `jsonable` of a whole view, for tests.
#[must_use]
pub fn view_json(v: &Rec) -> PyValue {
    jsonable(&Val::Map(v.clone()))
}
