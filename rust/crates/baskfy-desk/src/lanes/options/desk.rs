//! Ports `app.options_desk` (`docs/options/05` §3, `06` OP10): `/nifty-options`, its JSON twin
//! and its two POSTs. The desk console is the only place an options plan becomes an order; no
//! scheduler reaches either POST, and there is no route that confirms without a request.

use std::sync::Arc;

use axum::body::{Body, Bytes};
use axum::extract::State;
use axum::http::{HeaderMap, Response};
use chrono::{DateTime, FixedOffset};

use super::core::{Mode, Sleeve, mode_str, role_str, side_str, sleeve_str};
use super::execute::{self, ExecError, KiteQuotes, outcome_json};
use super::gates;
use super::py::{self, col, int, text};
use super::store::{ExitVerdict, PgOptionsStore, StoreError};
use super::web::{self, Form};
use crate::app::AppState;
use crate::storage::Param;
use crate::wire::{PyDict, PyValue, dict};

/// `EXIT_SENTENCES` (`05` §3, verbatim).
#[must_use]
pub fn sentence_for(sleeve: &str) -> &'static str {
    match sleeve.get(..2).unwrap_or(sleeve) {
        "O1" => {
            "Confirming this plan also authorises its rule-driven exits \u{2014} profit at \u{bd}C, stop at 1.5C, a \
             short-strike touch, and the mandatory flat at 14:30 \u{2014} without a second click."
        }
        "O2" => {
            "Confirming this plan also authorises its exits \u{2014} the 30 % stop, the 60 % target, a close \
             back inside the opening range, the 45-minute time stop, and the flat at 15:00 \u{2014} without a \
             second click."
        }
        _ => {
            "Confirming this plan also authorises its exits \u{2014} 80 % of width, half the debit lost, the \
             setup's invalidation, and the flat at 14:45 \u{2014} without a second click."
        }
    }
}

fn mode_of(state: &AppState, sleeve: Sleeve) -> Mode {
    gates::mode_of(&state.settings_snapshot(), sleeve)
}

/// `build_view(store, now=)`: today's entry plans and open positions.
///
/// # Errors
/// The database.
pub async fn build_view(
    state: &AppState,
    store: &mut PgOptionsStore<'_>,
    now: DateTime<FixedOffset>,
) -> Result<PyDict, StoreError> {
    let day = now.date_naive();
    let sql = format!(
        "SELECT p.plan_id, p.sleeve, p.status, p.expires_at, p.structure, s.state \
         FROM {} p JOIN {} s ON s.id = p.session_id \
         WHERE p.user_id = ? AND s.trade_date = ? AND p.kind = 'ENTRY' ORDER BY p.issued_at",
        store.t("op_plan"),
        store.t("op_session")
    );
    let rows = store
        .conn
        .execute(&sql, &[Param::Int(store.user_id), Param::Date(day)])
        .await?
        .rows;
    let mut plans = Vec::new();
    for row in &rows {
        let Some(plan) = store.plan(&text(col(row, "plan_id"))).await? else {
            continue;
        };
        let confirmable =
            plan.status == "ISSUED" && plan.session_state == "PLANNED" && now < plan.expires_at;
        let legs: Vec<PyValue> = plan
            .legs
            .iter()
            .map(|lg| {
                PyValue::Map(dict([
                    ("seq", PyValue::Int(lg.seq)),
                    ("role", PyValue::Str(role_str(lg.role).to_owned())),
                    ("symbol", PyValue::Str(lg.tradingsymbol.clone())),
                    ("side", PyValue::Str(side_str(lg.side).to_owned())),
                    ("quantity", PyValue::Int(lg.quantity)),
                ]))
            })
            .collect();
        let sleeve = sleeve_str(plan.sleeve);
        plans.push(PyValue::Map(dict([
            ("plan_id", PyValue::Str(plan.plan_id.clone())),
            ("sleeve", PyValue::Str(sleeve.to_owned())),
            (
                "structure",
                PyValue::Str(plan.structure.as_str().to_owned()),
            ),
            ("status", PyValue::Str(plan.status.clone())),
            ("session_state", PyValue::Str(plan.session_state.clone())),
            ("expires_at", PyValue::Str(py::iso_aware(&plan.expires_at))),
            ("confirmable", PyValue::Bool(confirmable)),
            (
                "paper",
                PyValue::Bool(mode_str(mode_of(state, plan.sleeve)) == "PAPER"),
            ),
            ("sentence", PyValue::Str(sentence_for(sleeve).to_owned())),
            ("legs", PyValue::List(legs)),
        ])));
    }
    let sql = format!(
        "SELECT p.session_id, s.sleeve, p.entry_points, p.last_mark_points, p.exit_plan_id, \
         p.hard_exit_at FROM {} p \
         JOIN {} s ON s.id = p.session_id \
         WHERE p.user_id = ? AND p.closed_at IS NULL ORDER BY p.session_id",
        store.t("op_position"),
        store.t("op_session")
    );
    let rows = store
        .conn
        .execute(&sql, &[Param::Int(store.user_id)])
        .await?
        .rows;
    let positions: Vec<PyValue> = rows
        .iter()
        .map(|r| {
            PyValue::Map(dict([
                ("session_id", PyValue::Int(int(col(r, "session_id")))),
                ("sleeve", PyValue::Str(text(col(r, "sleeve")))),
                ("entry_points", PyValue::Str(text(col(r, "entry_points")))),
                (
                    "last_mark_points",
                    py::vstr(py::opt_text(col(r, "last_mark_points"))),
                ),
                ("exit_plan_id", py::db_to_py(col(r, "exit_plan_id"))),
                (
                    "hard_exit_at",
                    if py::truthy(col(r, "hard_exit_at")) {
                        py::aware(col(r, "hard_exit_at"))
                            .map_or(PyValue::None, |t| PyValue::Str(py::iso_aware(&t)))
                    } else {
                        PyValue::None
                    },
                ),
            ]))
        })
        .collect();
    Ok(dict([
        ("day", PyValue::Str(day.format("%Y-%m-%d").to_string())),
        ("plans", PyValue::List(plans)),
        ("positions", PyValue::List(positions)),
    ]))
}

async fn view(state: &AppState) -> PyDict {
    let now = state.clock.now_ist();
    let user = state.settings_snapshot().sole_user_id;
    let built = match state.db.connect().await {
        Ok(mut conn) => {
            let mut store = PgOptionsStore::new(&mut conn, user);
            build_view(state, &mut store, now).await
        }
        Err(e) => Err(StoreError::Db(e)),
    };
    match built {
        Ok(v) => {
            let mut out = dict([("ok", PyValue::Bool(true))]);
            for (k, val) in v.iter() {
                out.insert(k, val.clone());
            }
            out
        }
        Err(e) => dict([
            ("ok", PyValue::Bool(false)),
            ("error", PyValue::Str(e.py_text())),
            ("plans", PyValue::List(Vec::new())),
            ("positions", PyValue::List(Vec::new())),
        ]),
    }
}

/// `GET /nifty-options`.
pub async fn page(State(state): State<AppState>) -> Response<Body> {
    super::badge::install(&state);
    let v = view(&state).await;
    web::render(
        &state,
        "/nifty-options",
        "nifty_options.html",
        dict([("v", PyValue::Map(v))]),
    )
}

/// `GET /nifty-options/data`.
pub async fn data(State(state): State<AppState>) -> Response<Body> {
    super::badge::install(&state);
    web::json(200, &PyValue::Map(view(&state).await))
}

fn answer(result: Result<execute::ExecOutcome, ExecError>) -> Response<Body> {
    match result {
        Ok(o) => web::json(200, &outcome_json(&o)),
        Err(ExecError::Refused(r)) => web::refusal(r.status, &r.code, &r.message),
        Err(ExecError::Other(m)) => web::internal_error(&m),
    }
}

/// `POST /nifty-options/execute` (form `plan_id`, `confirm`): the one entry route.
pub async fn execute(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response<Body> {
    super::badge::install(&state);
    let mut form = Form::new(web::content_type(&headers).as_deref(), &body);
    let plan_id = form.required("plan_id");
    let confirm = form.required("confirm");
    if let Some(r) = form.rejection() {
        return r;
    }
    if confirm != "true" {
        return web::refusal(400, "CONFIRM_REQUIRED", "confirm must be true");
    }
    let settings = state.settings_snapshot();
    let mut conn = match state.db.connect().await {
        Ok(c) => c,
        Err(e) => return web::internal_error(&e.to_string()),
    };
    let mut store = PgOptionsStore::new(&mut conn, settings.sole_user_id);
    let sleeve = match store.plan(&plan_id).await {
        Ok(Some(p)) => p.sleeve,
        Ok(None) => Sleeve::O2,
        Err(e) => return web::internal_error(&e.py_text()),
    };
    let gateway = execute::options_gateway_ready(&state, sleeve).await;
    let quotes = KiteQuotes {
        kite: Arc::clone(&state.kite),
    };
    let modes = |s: Sleeve| gates::mode_of(&settings, s);
    let now = state.clock.now_ist();
    answer(execute::execute_entry(&mut store, gateway, &quotes, &plan_id, true, &modes, now).await)
}

/// `POST /nifty-options/close` (form `session_id`, `confirm`): **Close now** (MANUAL).
pub async fn close(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response<Body> {
    super::badge::install(&state);
    let mut form = Form::new(web::content_type(&headers).as_deref(), &body);
    let session_id = form.required_int("session_id");
    let confirm = form.required("confirm");
    if let Some(r) = form.rejection() {
        return r;
    }
    if confirm != "true" {
        return web::refusal(400, "CONFIRM_REQUIRED", "confirm must be true");
    }
    let settings = state.settings_snapshot();
    let mut conn = match state.db.connect().await {
        Ok(c) => c,
        Err(e) => return web::internal_error(&e.to_string()),
    };
    let mut store = PgOptionsStore::new(&mut conn, settings.sole_user_id);
    let result: Result<Response<Body>, ExecError> = async {
        let Some(plan) = store.entry_plan_for(session_id).await? else {
            return Ok(web::refusal(404, "UNKNOWN_SESSION", "no such position"));
        };
        let open = store.open_positions(plan.trade_date).await?;
        let Some(tracked) = open.into_iter().find(|t| t.session_id == session_id) else {
            return Ok(web::refusal(409, "NOT_OPEN", "the position is not open"));
        };
        let now = state.clock.now_ist();
        let exit_id = match tracked.exit_plan_id.clone() {
            Some(id) if !id.is_empty() => Some(id),
            _ => {
                store
                    .raise_exit(session_id, &ExitVerdict::rule("MANUAL", "RULE"), now)
                    .await?
            }
        };
        let gateway = execute::options_gateway_ready(&state, plan.sleeve).await;
        let quotes = KiteQuotes {
            kite: Arc::clone(&state.kite),
        };
        let mode = gates::mode_of(&settings, plan.sleeve);
        let outcome = execute::execute_exit(
            &mut store,
            gateway,
            &quotes,
            &plan,
            exit_id.as_deref(),
            "MANUAL",
            mode,
            now,
        )
        .await?;
        Ok(web::json(200, &outcome_json(&outcome)))
    }
    .await;
    match result {
        Ok(r) => r,
        Err(e) => answer(Err(e)),
    }
}
