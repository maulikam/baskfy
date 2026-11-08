//! `GET /reconcile` and `GET /reconcile/data` (`main.py`): plan against broker, settling fills
//! from the order book as a side effect of loading.

use axum::Router;
use axum::body::Body;
use axum::extract::State;
use axum::http::{Response, StatusCode, Uri};
use axum::routing::get;

use super::get_only;
use crate::app::AppState;
use crate::lanes::analytics::analytics_views::protection;
use crate::lanes::analytics::web::{self, MultiDict};
use crate::lanes::exec_core::exec_kite;
use crate::lanes::exec_core::stops::StopConfig;
use crate::lanes::weekly::plan_store;
use crate::lanes::weekly::weekly_books::reconcile_view;
use crate::wire::{PyDict, PyValue, dict};

/// The routes.
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/reconcile", get_only(get(page)))
        .route("/reconcile/data", get_only(get(data)))
}

#[allow(clippy::print_stderr)]
fn warn(line: &str) {
    eprintln!("WARNING:root:{line}");
}

fn not_authed() -> PyDict {
    dict([
        ("available", PyValue::Bool(false)),
        ("reason", PyValue::Str("not authenticated".to_owned())),
    ])
}

/// `x or []` over a broker list read.
fn list_or_empty(v: PyValue) -> Vec<PyValue> {
    match v {
        PyValue::List(l) => l,
        _ => Vec::new(),
    }
}

fn dicts(rows: &[PyValue]) -> Vec<PyDict> {
    rows.iter()
        .filter_map(|r| match r {
            PyValue::Map(m) => Some(m.clone()),
            _ => None,
        })
        .collect()
}

/// The broker's three reads, then the store's half: settle, then build the view.
async fn compare(state: &AppState, plan_id: &str) -> Result<(PyDict, PyDict), String> {
    let k = exec_kite::kite(state).map_err(|e| e.message)?;
    if !k.is_authed().await {
        return Ok((not_authed(), PyDict::new()));
    }
    let orders = list_or_empty(state.kite.orders().await.map_err(|e| e.to_string())?);
    let gtts = list_or_empty(state.kite.get_gtts().await.map_err(|e| e.to_string())?);
    let holdings = k.holdings().await.map_err(|e| e.message)?;
    let mut conn = state.db.connect().await.map_err(|e| e.to_string())?;
    conn.migrate().await.map_err(|e| e.to_string())?;
    let pid = if plan_id.is_empty() {
        plan_store::latest(&mut conn, None)
            .await
            .map_err(|e| e.to_string())?
            .and_then(|row| match row.get("version_id") {
                Some(PyValue::Str(s)) => Some(s.clone()),
                Some(PyValue::None) | None => None,
                Some(other) => Some(other.py_str()),
            })
    } else {
        Some(plan_id.to_owned())
    };
    let broker_orders = dicts(&orders);
    let settled = match pid.as_deref() {
        Some(p) if !p.is_empty() => {
            plan_store::reconcile_fills(&mut conn, p, &broker_orders, &web::now_iso_seconds(state))
                .await
                .map_err(|e| e.to_string())?
        }
        _ => PyDict::new(),
    };
    let plan = pid.as_deref().and_then(|p| state.plans.get(p));
    let held: Vec<PyValue> = holdings.iter().cloned().map(PyValue::Map).collect();
    let stops = protection::review(&held, &gtts, StopConfig::default()).to_py();
    let mut view = reconcile_view::build(
        &mut conn,
        pid.as_deref(),
        plan.as_ref().map(|p| &p.body),
        &broker_orders,
        &stops,
        &holdings,
    )
    .await
    .map_err(|e| e.to_string())?;
    view.insert("available", PyValue::Bool(true));
    Ok((view, settled))
}

/// `GET /reconcile?plan_id=`: any failure becomes the page's reason, never a 500.
async fn page(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let plan_id = MultiDict::from_query(&uri).str_or_empty("plan_id");
    let view = match compare(&state, &plan_id).await {
        Ok((mut view, settled)) => {
            if view.get("available").is_some_and(PyValue::truthy) {
                view.insert("settled", PyValue::Map(settled));
            }
            view
        }
        Err(e) => {
            warn(&format!("reconcile failed: {e}"));
            dict([
                ("available", PyValue::Bool(false)),
                ("reason", PyValue::Str(e.chars().take(200).collect())),
            ])
        }
    };
    web::render(
        &state,
        uri.path(),
        "reconcile.html",
        dict([("v", PyValue::Map(view))]),
    )
}

/// `GET /reconcile/data?plan_id=`: the same comparison as JSON; a failure is a 500.
async fn data(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let plan_id = MultiDict::from_query(&uri).str_or_empty("plan_id");
    match compare(&state, &plan_id).await {
        Ok((view, _)) => web::json(StatusCode::OK, &PyValue::Map(view)),
        Err(e) => web::internal_error(&e),
    }
}
