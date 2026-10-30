//! Lane **vbt** — the volume-breakout sleeve: page, confirm, rescan. **No auto-execute flag,
//! ever** (DECISIONS-VB PACK.2: non-negotiable 1's named exceptions belong to the swing, TWT and
//! F3 books; this sleeve has none and an agent may not add one).
//!
//! **Python it ports:** `app/vbt_desk.py` (this file and [`store`]), `app/vbt_execute.py`
//! ([`execute`]). What VBT shares with TWT (`_jsonable`, the row helpers, `app.reconcile`'s
//! entry refusal, `app.exit_rules`, `last_price`) is read from `lanes::twt::common`, which the
//! twt-vbt lane wrote for both books.
//!
//! **Routes:** `GET /vbt`, `GET /vbt/data`, `GET /vbt/scan/{run_id}`, `POST /vbt/execute`,
//! `POST /vbt/rescan`, `POST /vbt/scan`.
//!
//! **The order path.** Every line goes through this sleeve's **own** gateway —
//! [`crate::gateway::Gateway::derive_sleeve`] with [`execute::vbt_gates`] (intraday and options
//! off unconditionally, simulated unless `DRY_RUN=false` **and**
//! `BASKFY_VBT_EXECUTION_ENABLED=true`), `vbt_orders_journal.jsonl` and the 0.5–15 % band —
//! built once per process, as `vbt_desk.vbt_gateway()` is. A base gateway that cannot derive
//! one refuses every send (fail closed).

pub mod execute;
pub mod store;

use std::sync::Arc;

use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::{Path as AxPath, State};
use axum::http::{Response, Uri};
use axum::routing::{get, post};
use chrono::{DateTime, FixedOffset};
use rust_decimal::{Decimal, RoundingStrategy};

use super::twt::common::{
    FormFields, HttpError, Rec, Val, json_response, jsonable, jsonable_rec, last_price, path_int,
    server_error, to_ist,
};
use crate::app::AppState;
use crate::gateway::Gateway;
use crate::storage::StorageError;
use crate::wire::PyValue;

use execute::{ExecError, vbt_gates};
use store::{PgVbtStore, StoreError, VbtStore, date_of, stamp_of};

/// `KIND_LABEL`.
pub const KIND_LABEL: [(&str, &str); 6] = [
    ("PLACE_LIMIT", "VBT BUY LIMIT"),
    ("BUY_AT_MARKET", "VBT BUY AT MARKET (live scan)"),
    ("SELL_AT_OPEN", "VBT SELL AT OPEN"),
    ("CANCEL_LIMIT", "VBT CANCEL"),
    ("ARM_GTT", "VBT ARM STOP"),
    ("RAISE_GTT_STOP", "VBT RAISE STOP (breakeven)"),
];

/// `DRY_RUN_SESSIONS_REQUIRED` (`baskfy_core.vbt.config`).
pub const DRY_RUN_SESSIONS_REQUIRED: i64 = baskfy_core::vbt::config::DRY_RUN_SESSIONS_REQUIRED;

/// The lane's routes.
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/vbt", get(vbt_page))
        .route("/vbt/data", get(vbt_data))
        .route("/vbt/rescan", post(vbt_rescan))
        .route("/vbt/scan", post(vbt_scan_now))
        .route("/vbt/scan/{run_id}", get(vbt_scan_run))
        .route("/vbt/execute", post(vbt_execute_line))
}

/// The lane's template globals and filters (none).
pub fn register_templates(templates: &mut crate::templates::Templates) {
    let _ = templates;
}

/// Run one of the lane's background services by name; `None` if the name is not this lane's.
/// VBT has none: no monitor, no auto-execute.
pub async fn run_service(name: &str, state: &AppState) -> Option<Result<(), String>> {
    let _ = (name, state);
    None
}

// =============================================================================================
// Wiring
// =============================================================================================

/// `open_store()`: the sole user's store over the desk's connection.
///
/// # Errors
/// The connection.
pub async fn open_store(state: &AppState) -> Result<PgVbtStore, StorageError> {
    let s = state.settings_snapshot();
    let conn = state.db.connect().await?;
    let schema = if state.db.config().is_postgres() {
        store::SCHEMA
    } else {
        ""
    };
    Ok(PgVbtStore::new(
        conn,
        s.sole_user_id,
        schema,
        Some(s.sole_broker_account_id),
    ))
}

/// `str(int(raw))` for a form value pydantic's lax `int` accepts but no `i64` holds: the same
/// grammar as [`super::twt::common::lax_int`] (whitespace, a sign, single `_` between digits, a
/// `.0…` tail), without the bound. `None` for anything that is not an integer at all.
#[must_use]
pub fn unbounded_int_text(raw: &str) -> Option<String> {
    let t = raw.trim();
    let (neg, body) = match t.as_bytes().first() {
        Some(b'-') => (true, &t[1..]),
        Some(b'+') => (false, &t[1..]),
        _ => (false, t),
    };
    let int_part = match body.split_once('.') {
        Some((i, f)) => {
            if f.is_empty() || !f.bytes().all(|b| b == b'0') {
                return None;
            }
            i
        }
        None => body,
    };
    if int_part.is_empty()
        || int_part.starts_with('_')
        || int_part.ends_with('_')
        || int_part.contains("__")
        || !int_part.bytes().all(|b| b.is_ascii_digit() || b == b'_')
    {
        return None;
    }
    let digits: String = int_part.chars().filter(char::is_ascii_digit).collect();
    let trimmed = digits.trim_start_matches('0');
    if trimmed.is_empty() {
        return Some("0".to_owned());
    }
    Some(if neg {
        format!("-{trimmed}")
    } else {
        trimmed.to_owned()
    })
}

/// `vbt_gateway()`: this sleeve's gateway, built once per base gateway.
///
/// `None` when `VBT_STOP_BAND_MIN`/`MAX` are out of range: Python's `StopBand(...)` raises
/// `ValueError` inside `build_vbt_gateway` and the confirm is a 500 — it never falls back to the
/// weekly book's 8–12 % band, and neither does this (fail closed).
pub fn vbt_gateway(state: &AppState) -> Option<Arc<dyn Gateway>> {
    let band = execute::vbt_stop_band(&state.settings_snapshot()).ok()?;
    Some(super::twt::sleeve_gateway(
        state,
        "vbt",
        execute::VBT_JOURNAL_NAME,
        band,
        execute::vbt_gates,
    ))
}

/// `vbt_gateway()` as the confirm route calls it: Python's builds it through
/// `app.main.gateway()`, which builds the process's shared risk manager as a side effect (on
/// Postgres: the day's `risk_ledger` row) **before** `execute_line` runs — so even a confirm
/// refused with a 400/404/409/410 has built it. The same happens here, then the sleeve's own
/// gateway is derived.
pub async fn vbt_gateway_ready(state: &AppState) -> Option<Arc<dyn Gateway>> {
    if let Some(desk) = state
        .gateway
        .as_any()
        .and_then(|a| a.downcast_ref::<crate::lanes::exec_core::desk::DeskGateway>())
    {
        let _ = desk.risk().await;
    }
    vbt_gateway(state)
}

fn now_ist(state: &AppState) -> DateTime<FixedOffset> {
    py_fromtimestamp_ist(state.clock.time())
}

/// `datetime.fromtimestamp(t, IST)` exactly as CPython computes it (what `_now()` is under the
/// desk's clock): the fraction is rounded **half-even to microseconds**
/// (`_PyTime_DoubleToDenominator`), not to nanoseconds as the foundation's `Clock::now_ist` does.
/// The difference decides a plan's exact expiry instant: at `now == expires_at` Python's clock
/// equals the stored microsecond and the plan is live (404 for a missing line), where a
/// nanosecond reading can sit a fraction of a microsecond past it (410).
#[must_use]
pub fn py_fromtimestamp_ist(t: f64) -> DateTime<FixedOffset> {
    use chrono::TimeZone;
    let mut int_part = t.trunc();
    let mut micros = ((t - int_part) * 1e6).round_ties_even();
    if micros >= 1e6 {
        micros -= 1e6;
        int_part += 1.0;
    } else if micros < 0.0 {
        micros += 1e6;
        int_part -= 1.0;
    }
    // Seconds since 1970 fit an i64 and a microsecond count a u32 for any clock this desk reads.
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    let (secs, nanos) = (int_part as i64, (micros as u32) * 1_000);
    chrono::Utc
        .timestamp_opt(secs, nanos)
        .single()
        .unwrap_or_default()
        .with_timezone(&crate::clock::ist())
}

/// `str(exc)[:200]` for a store failure, as the page shows it.
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

/// `_countdown(expires_at, now)`.
#[must_use]
pub fn countdown(expires_at: Option<DateTime<FixedOffset>>, now: &DateTime<FixedOffset>) -> String {
    let Some(expires) = expires_at else {
        return "no expiry".to_owned();
    };
    let left = store::total_seconds(expires - *now).trunc();
    #[allow(clippy::cast_possible_truncation)]
    let left = left as i64;
    if left <= 0 {
        return "expired \u{2014} rebuild the plan".to_owned();
    }
    format!("{}m {}s left", left.div_euclid(60), left.rem_euclid(60))
}

/// `_price(value)`: `"—"` for `None`, else `f"₹{value:,.2f}"`.
#[must_use]
pub fn price_text(value: Option<Decimal>) -> String {
    let Some(d) = value else {
        return "\u{2014}".to_owned();
    };
    let r = d.round_dp_with_strategy(2, RoundingStrategy::MidpointNearestEven);
    let text = format!("{:.2}", r.abs());
    let (int_part, frac) = text.split_once('.').unwrap_or((text.as_str(), "00"));
    let mut grouped = String::new();
    for (i, c) in int_part.chars().enumerate() {
        if i > 0 && (int_part.len() - i) % 3 == 0 {
            grouped.push(',');
        }
        grouped.push(c);
    }
    let sign = if r.is_sign_negative() && !r.is_zero() {
        "-"
    } else {
        ""
    };
    format!("\u{20b9}{sign}{grouped}.{frac}")
}

fn label_of(kind: &str) -> String {
    KIND_LABEL
        .iter()
        .find(|(k, _)| *k == kind)
        .map_or_else(|| kind.to_owned(), |(_, v)| (*v).to_owned())
}

/// `line_view(line, now=...)`: confirmable only while `PROPOSED` **and** the plan is unexpired.
#[must_use]
pub fn line_view(line: &Rec, now: &DateTime<FixedOffset>) -> Rec {
    let expired = line.stamp("expires_at").is_some_and(|e| *now > e);
    let mut out = line.clone();
    out.set("label", Val::Str(label_of(&line.s("kind"))));
    out.set(
        "confirmable",
        Val::Bool(line.s("state") == "PROPOSED" && !expired),
    );
    out.set("expired", Val::Bool(expired));
    out.set("limit_text", Val::Str(price_text(line.dec("limit_price"))));
    out.set("stop_text", Val::Str(price_text(line.dec("stop_price"))));
    out.set("value_text", Val::Str(price_text(line.dec("value_inr"))));
    out
}

/// `working_view(order, valid_sessions=...)`.
#[must_use]
pub fn working_view(order: &Rec, valid_sessions: i64) -> Rec {
    let worked = order.int("sessions_worked");
    let mut out = order.clone();
    out.set(
        "sessions_text",
        Val::Str(format!("{worked} of {valid_sessions}")),
    );
    out.set("expires_tonight", Val::Bool(worked >= valid_sessions - 1));
    out.set("limit_text", Val::Str(price_text(order.dec("limit_price"))));
    out.set(
        "remaining",
        Val::Int(order.int("quantity") - order.int("filled_quantity")),
    );
    out
}

/// `position_view(position)`.
///
/// # Errors
/// An entry or stop Python's `Decimal(str(...))` would refuse.
pub fn position_view(position: &Rec) -> Result<Rec, StoreError> {
    let need = |key: &str| {
        position.dec(key).ok_or_else(|| {
            StoreError::Bad(format!(
                "[<class 'decimal.ConversionSyntax'>] ({key} is {})",
                position.s(key)
            ))
        })
    };
    let risk =
        (need("entry_avg")? - need("stop_price")?) * Decimal::from(position.int("quantity_open"));
    let mut out = position.clone();
    out.set("naked", Val::Bool(position.is_none("gtt_id")));
    out.set(
        "entry_text",
        Val::Str(price_text(position.dec("entry_avg"))),
    );
    out.set(
        "stop_text",
        Val::Str(price_text(position.dec("stop_price"))),
    );
    out.set(
        "initial_stop_text",
        Val::Str(price_text(position.dec("initial_stop"))),
    );
    out.set("risk_text", Val::Str(price_text(Some(risk))));
    out.set(
        "sells_tomorrow",
        Val::Bool(!position.is_none("exit_queued_for")),
    );
    Ok(out)
}

fn gates_rec(s: &crate::settings::Settings) -> [(&'static str, Val); 3] {
    [
        ("dry_run", Val::Bool(vbt_gates(s).dry_run)),
        ("execution_enabled", Val::Bool(s.vbt_execution_enabled)),
        ("desk_dry_run", Val::Bool(s.dry_run)),
    ]
}

fn exit_rule() -> Val {
    Val::Str(super::twt::common::vbt_exit_rule())
}

/// `build_view(store, now=...)`: everything `vbt.html` needs, in one pass over the store.
///
/// # Errors
/// The store.
pub async fn build_view(
    store: &mut PgVbtStore,
    settings: &crate::settings::Settings,
    now: DateTime<FixedOffset>,
) -> Result<Rec, StoreError> {
    let today = to_ist(&now).date_naive();
    let plan = store.todays_plan(today).await?;
    let (lines, skips) = match &plan {
        Some(p) => (
            store.lines_for(p.int("id")).await?,
            store.skips_for(p.int("id")).await?,
        ),
        None => (Vec::new(), Vec::new()),
    };
    let config = store.config().await?;
    let breadth = store.breadth(today).await?;
    let session = match plan.as_ref().and_then(|p| p.date("session_date")) {
        Some(day) => store.session(day).await?,
        None => None,
    };
    let valid = baskfy_core::vbt::config::VbtConfig::default()
        .entry
        .valid_sessions;
    let working: Vec<Val> = store
        .working()
        .await?
        .iter()
        .map(|o| Val::Map(working_view(o, valid)))
        .collect();
    let mut book = Vec::new();
    for p in store.book().await? {
        book.push(Val::Map(position_view(&p)?));
    }
    let scan = store.newest_scan().await?;
    let [dry, enabled, desk_dry] = gates_rec(settings);
    let dry_run_sessions = config.val("dry_run_sessions");
    let dry_run_sessions = if config.contains("dry_run_sessions") {
        dry_run_sessions
    } else {
        Val::Int(0)
    };
    Ok(Rec::of([
        ("available", Val::Bool(true)),
        ("now", Val::Stamp(now)),
        ("today", Val::Date(today)),
        ("plan", plan.clone().map_or(Val::None, Val::Map)),
        (
            "countdown",
            Val::Str(countdown(
                plan.as_ref().and_then(|p| p.stamp("expires_at")),
                &now,
            )),
        ),
        (
            "lines",
            Val::List(lines.iter().map(|l| Val::Map(line_view(l, &now))).collect()),
        ),
        (
            "skips",
            Val::List(skips.into_iter().map(Val::Map).collect()),
        ),
        ("working", Val::List(working)),
        ("book", Val::List(book)),
        ("exit_rule", exit_rule()),
        ("breadth", breadth.map_or(Val::None, Val::Map)),
        ("config", Val::Map(config)),
        ("session", session.map_or(Val::None, Val::Map)),
        dry,
        enabled,
        desk_dry,
        ("dry_run_sessions", dry_run_sessions),
        (
            "dry_run_sessions_required",
            Val::Int(DRY_RUN_SESSIONS_REQUIRED),
        ),
        ("scan", scan.map_or(Val::None, Val::Map)),
        ("reason", Val::Str(String::new())),
    ]))
}

/// `unavailable_view(reason, now=...)`: a page that says why it is empty.
#[must_use]
pub fn unavailable_view(
    reason: &str,
    settings: &crate::settings::Settings,
    now: DateTime<FixedOffset>,
) -> Rec {
    let [dry, enabled, desk_dry] = gates_rec(settings);
    Rec::of([
        ("available", Val::Bool(false)),
        ("now", Val::Stamp(now)),
        ("today", Val::Date(to_ist(&now).date_naive())),
        ("plan", Val::None),
        ("countdown", Val::Str(String::new())),
        ("lines", Val::List(Vec::new())),
        ("skips", Val::List(Vec::new())),
        ("working", Val::List(Vec::new())),
        ("book", Val::List(Vec::new())),
        ("exit_rule", exit_rule()),
        ("breadth", Val::None),
        ("config", Val::Map(Rec::new())),
        ("session", Val::None),
        dry,
        enabled,
        desk_dry,
        ("dry_run_sessions", Val::Int(0)),
        (
            "dry_run_sessions_required",
            Val::Int(DRY_RUN_SESSIONS_REQUIRED),
        ),
        ("scan", Val::None),
        ("reason", Val::Str(reason.to_owned())),
    ])
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

fn ok_json(rec: &Rec) -> Response<Body> {
    json_response(200, &PyValue::Map(jsonable_rec(rec)))
}

/// `jsonable` of a whole view, for tests.
#[must_use]
pub fn view_json(v: &Rec) -> PyValue {
    jsonable(&Val::Map(v.clone()))
}

// =============================================================================================
// Routes
// =============================================================================================

async fn vbt_page(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let v = view(&state).await;
    super::twt::render_page(&state, &uri, "vbt.html", &v)
}

async fn vbt_data(State(state): State<AppState>) -> Response<Body> {
    let v = view(&state).await;
    ok_json(&v)
}

async fn vbt_rescan(State(state): State<AppState>) -> Response<Body> {
    let now = now_ist(&state);
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    match store.request_scan(now, None, None).await {
        Ok(run_id) => ok_json(&Rec::of([
            ("accepted", Val::Bool(true)),
            ("run_id", Val::Int(run_id)),
            ("status", Val::from("QUEUED")),
        ])),
        Err(StoreError::Scan(refused)) => {
            let mut body = Rec::of([
                ("accepted", Val::Bool(false)),
                (
                    "reason",
                    Val::from(if refused.status == 409 {
                        "a re-detect is already in flight"
                    } else {
                        "too soon"
                    }),
                ),
                ("run_id", Val::Int(refused.run_id)),
                ("status", Val::Str(refused.run_status)),
            ]);
            if let Some(after) = refused.retry_after {
                body.set("retry_after_seconds", Val::Int(after));
            }
            ok_json(&body)
        }
        Err(_) => server_error(),
    }
}

async fn vbt_scan_now(State(state): State<AppState>) -> Response<Body> {
    let now = now_ist(&state);
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    match store.request_scan(now, None, None).await {
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
            if let Some(after) = refused.retry_after {
                err.headers
                    .push(("Retry-After".to_owned(), after.to_string()));
            }
            err.response()
        }
        Err(_) => server_error(),
    }
}

async fn vbt_scan_run(
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
    let row = match store.scan_run(run_id).await {
        Ok(Some(row)) => row,
        Ok(None) => return HttpError::new(404, format!("No scan {run_id}.")).response(),
        Err(_) => return server_error(),
    };
    let detail = match row.val("detail") {
        v @ Val::Py(PyValue::Map(_)) => v,
        v @ Val::Map(_) => v,
        _ => Val::None,
    };
    ok_json(&Rec::of([
        ("run_id", row.val("id")),
        ("status", row.val("status")),
        ("source", row.val("source")),
        ("requested_at", stamp_of(&row.val("requested_at"))),
        ("started_at", stamp_of(&row.val("started_at"))),
        ("finished_at", stamp_of(&row.val("finished_at"))),
        ("session_date", date_of(&row.val("session_date"))),
        ("detail", detail),
        ("error", row.val("error")),
        ("task_id", row.val("task_id")),
    ]))
}

async fn vbt_execute_line(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let mut form = FormFields::parse(&body);
    let plan_id = form.str("plan_id");
    // `line_id: int = Form(...)`: Python's int is unbounded, so a well-formed id past `i64`
    // is not a 422 there — it reaches the store, finds no line and is the validator's 404.
    let line_raw = form.str("line_id");
    let mut huge_line: Option<String> = None;
    let line_id = if line_raw.is_empty() {
        0
    } else if let Some(n) = super::twt::common::lax_int(&line_raw) {
        n
    } else if let Some(text) = unbounded_int_text(&line_raw) {
        huge_line = Some(text);
        0
    } else {
        form.int("line_id")
    };
    let confirm = form.str("confirm");
    if let Some(r) = form.finish() {
        return r;
    }
    let Ok(mut store) = open_store(&state).await else {
        return server_error();
    };
    if let Some(text) = huge_line {
        // Python: `store.line(huge)` is `None`, no last price is read, `vbt_gateway()` is
        // built, then `_validate` refuses.
        if vbt_gateway_ready(&state).await.is_none() {
            return server_error();
        }
        return match execute::validate_unknown_line(
            &mut store,
            &plan_id,
            &text,
            &confirm,
            now_ist(&state),
        )
        .await
        {
            ExecError::Http(h) => h.response(),
            _ => server_error(),
        };
    }
    let line = match store.line(line_id).await {
        Ok(l) => l,
        Err(_) => return server_error(),
    };
    // Python reads a last price for a SELL_AT_OPEN line only.
    let price = match &line {
        Some(l) if l.s("kind") == execute::SELL_AT_OPEN => last_price(&state, &l.s("symbol")).await,
        _ => None,
    };
    let Some(gateway) = vbt_gateway_ready(&state).await else {
        return server_error();
    };
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
        Ok(outcome) => {
            let mut r = outcome.as_rec();
            r.set("line_id", Val::Int(line_id));
            ok_json(&r)
        }
        Err(ExecError::Http(h)) => h.response(),
        Err(_) => server_error(),
    }
}
