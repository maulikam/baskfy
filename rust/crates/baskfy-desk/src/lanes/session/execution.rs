//! Runtime execution control: readiness, pause/resume of new submissions, and the operator's
//! ruling on an order whose outcome the broker's book cannot settle. Cancels, GTT changes and
//! reconciliation are untouched by a pause.

use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::State;
use axum::http::{Response, StatusCode};
use axum::routing::{get, post};
use chrono::{Datelike as _, FixedOffset, NaiveTime};
use serde::Deserialize;
use serde_json::json;

use super::get_only;
use crate::app::AppState;
use crate::lanes::analytics::web;
use crate::lanes::exec_core::{exec_kite, intent};
use crate::storage::{Db, DbValue};
use crate::wire::PyValue;

const RECONCILE_FRESH_SECONDS: i64 = 60;

/// The routes.
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/execution/ready", get_only(get(ready)))
        .route("/execution/pause", post(pause))
        .route("/execution/resume", post(resume))
        .route("/execution/intents/resolve", post(resolve))
}

fn reply(status: StatusCode, body: &serde_json::Value) -> Response<Body> {
    match PyValue::from_json(body.to_string().as_bytes()) {
        Ok(value) => web::json(status, &value),
        Err(why) => web::detail(StatusCode::INTERNAL_SERVER_ERROR, &why),
    }
}

fn in_session(now: chrono::DateTime<FixedOffset>) -> bool {
    let open = NaiveTime::from_hms_opt(9, 15, 0).unwrap_or_default();
    let close = NaiveTime::from_hms_opt(15, 30, 0).unwrap_or_default();
    now.weekday().num_days_from_monday() < 5 && (open..=close).contains(&now.time())
}

async fn reconcile_age(db: &Db, now: chrono::DateTime<FixedOffset>) -> Option<i64> {
    let table = if db.config().is_postgres() {
        "public.lv_heartbeat"
    } else {
        "lv_heartbeat"
    };
    let sql =
        format!("SELECT at FROM {table} WHERE process = 'reconciler' ORDER BY at DESC LIMIT 1");
    let mut conn = db.connect().await.ok()?;
    let cursor = conn.execute(&sql, &[]).await.ok()?;
    match cursor.fetchone()?.get("at")? {
        DbValue::Aware(at) => Some((now - *at).num_seconds()),
        _ => None,
    }
}

async fn ready(State(state): State<AppState>) -> Response<Body> {
    let now = state.clock.now_ist();
    let kite_session = match exec_kite::kite(&state) {
        Ok(k) => k.try_is_authed().await.unwrap_or(false),
        Err(_) => false,
    };
    let open = intent::open_intents(&state.db).await;
    let control = intent::pause_state(&state.db).await;
    let age = reconcile_age(&state.db, now).await;
    let mut reasons = Vec::new();
    if !kite_session {
        reasons.push("no valid Kite session".to_owned());
    }
    match &open {
        Ok(rows) if !rows.is_empty() => {
            reasons.push(format!("{} order(s) have no known outcome", rows.len()));
        }
        Ok(_) => {}
        Err(why) => reasons.push(format!("intents unreadable: {why}")),
    }
    if let Err(why) = &control {
        reasons.push(format!("pause state unreadable: {why}"));
    }
    if in_session(now) && !age.is_some_and(|a| a <= RECONCILE_FRESH_SECONDS) {
        reasons.push("the reconciler has not run in the last minute".to_owned());
    }
    let unresolved: Vec<serde_json::Value> = open
        .unwrap_or_default()
        .into_iter()
        .map(|i| json!({"client_id": i.client_id, "symbol": i.symbol, "state": i.state}))
        .collect();
    let (paused, pause_reason) = control.unwrap_or((true, String::new()));
    let is_ready = reasons.is_empty();
    reply(
        if is_ready {
            StatusCode::OK
        } else {
            StatusCode::SERVICE_UNAVAILABLE
        },
        &json!({
            "ready": is_ready,
            "reasons": reasons,
            "kite_session": kite_session,
            "dry_run": state.settings_snapshot().dry_run,
            "paused": paused,
            "pause_reason": pause_reason,
            "reconcile_age_s": age,
            "unresolved": unresolved,
        }),
    )
}

#[derive(Deserialize, Default)]
struct PauseIn {
    #[serde(default)]
    reason: String,
}

async fn pause(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let input: PauseIn = serde_json::from_slice(&body).unwrap_or_default();
    let reason = if input.reason.trim().is_empty() {
        "paused by the operator".to_owned()
    } else {
        input.reason
    };
    match intent::set_paused(&state.db, state.clock.time(), true, &reason).await {
        Ok(()) => reply(StatusCode::OK, &json!({"paused": true, "reason": reason})),
        Err(why) => web::detail(StatusCode::INTERNAL_SERVER_ERROR, &why.to_string()),
    }
}

async fn resume(State(state): State<AppState>) -> Response<Body> {
    match intent::open_intents(&state.db).await {
        Ok(rows) if rows.is_empty() => {}
        Ok(rows) => {
            return web::detail(
                StatusCode::CONFLICT,
                &format!(
                    "{} order(s) have no known outcome; resolve them before resuming",
                    rows.len()
                ),
            );
        }
        Err(why) => return web::detail(StatusCode::INTERNAL_SERVER_ERROR, &why.to_string()),
    }
    match intent::set_paused(&state.db, state.clock.time(), false, "").await {
        Ok(()) => reply(StatusCode::OK, &json!({"paused": false})),
        Err(why) => web::detail(StatusCode::INTERNAL_SERVER_ERROR, &why.to_string()),
    }
}

#[derive(Deserialize)]
struct ResolveIn {
    client_id: String,
    state: String,
    #[serde(default)]
    order_id: Option<String>,
}

async fn resolve(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let Ok(input) = serde_json::from_slice::<ResolveIn>(&body) else {
        return web::detail(StatusCode::BAD_REQUEST, "client_id and state are required");
    };
    let valid = match input.state.as_str() {
        intent::NOT_SENT => true,
        intent::PLACED => input.order_id.as_deref().is_some_and(|id| !id.is_empty()),
        _ => false,
    };
    if !valid {
        return web::detail(
            StatusCode::BAD_REQUEST,
            "state must be NOT_SENT, or PLACED with an order_id",
        );
    }
    let ruled = intent::resolve_by_operator(
        &state.db,
        state.clock.time(),
        &input.client_id,
        &input.state,
        input.order_id.as_deref(),
    )
    .await;
    match ruled {
        Ok(true) => reply(StatusCode::OK, &json!({"resolved": true})),
        Ok(false) => web::detail(StatusCode::NOT_FOUND, "no open intent has that client_id"),
        Err(why) => web::detail(StatusCode::INTERNAL_SERVER_ERROR, &why.to_string()),
    }
}
