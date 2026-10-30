//! Lane **analytics / analytics_regime** (wave D, DECISIONS-RS R8.5's finer split; wave D2
//! R8.AR1–R8.AR7): the read-only regime overlay pages `GET /regime` and `GET /regime/data`,
//! and the index board `GET /indices`, `GET /indices/data`, `GET /indices/constituents`.
//!
//! The regime pages read precomputed rows only (`main.py`: "No Kite calls, no signal
//! recomputation, no writes: a status page must never be able to change the state it
//! reports"). The modules behind them live beside this file in the parent: [`super::regime`]
//! (`core/regime.py`'s config with the engine [`super::regime::engine`], the allocation solver
//! [`super::regime::alloc`] and `regime_run.py` [`super::regime::run`]),
//! [`super::regime_store`], [`super::breadth`], [`super::index_cache`] and
//! [`super::regime_view`]. The index board is [`index_view`] over [`nse_indices`] — read-only in
//! the strongest sense: NSE's public data and Kite quotes, no path to an order; a failure is a
//! stale or empty panel, never a 500.
//!
//! Starlette's `redirect_slashes` is reproduced for this lane's own paths (a toggled slash is a
//! 307 to its twin), as the session lane does for its paths (R8.23).

pub mod index_view;
pub mod nse_indices;

use axum::Router;
use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderMap, HeaderValue, Response, StatusCode, Uri, header};
use axum::routing::{MethodRouter, any, get};

use crate::app::AppState;
use crate::wire::{PyDict, PyValue, dict};

use super::desk_settings::stored_overrides;
use super::regime_view::{BuildError, DeskConstants, build};
use super::web::{MultiDict, internal_error, json, missing_field, redirect_307, render, storage_500};

/// The lane's routes.
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/regime", get_only(get(regime_page)))
        .route("/regime/data", get_only(get(regime_data)))
        .route("/indices", get_only(get(indices_page)))
        .route("/indices/data", get_only(get(indices_data)))
        .route("/indices/constituents", get_only(get(indices_constituents)))
        .route("/regime/", any(toggle_slash))
        .route("/regime/data/", any(toggle_slash))
        .route("/indices/", any(toggle_slash))
        .route("/indices/data/", any(toggle_slash))
        .route("/indices/constituents/", any(toggle_slash))
}

/// Starlette's `redirect_slashes`: `RedirectResponse(URL(scope=redirect_scope))` — absolute,
/// from the `Host` header, the query string kept, any verb.
async fn toggle_slash(headers: HeaderMap, uri: Uri) -> Response<Body> {
    let path = uri.path();
    let twin = match path.strip_suffix('/') {
        Some(stripped) => stripped.to_owned(),
        None => format!("{path}/"),
    };
    let host = headers
        .get(header::HOST)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("127.0.0.1");
    let query = uri.query().map(|q| format!("?{q}")).unwrap_or_default();
    redirect_307(&format!("http://{host}{twin}{query}"))
}

/// FastAPI's `@app.get`: the route's methods are `{"GET"}` alone, so `HEAD` (which axum's `get`
/// would answer, and would list in `Allow`) and every other verb get Starlette's 405 with
/// `Allow: GET`.
fn get_only(router: MethodRouter<AppState>) -> MethodRouter<AppState> {
    router.head(get_405).fallback(get_405)
}

async fn get_405() -> Response<Body> {
    let mut r = super::web::detail(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed");
    r.headers_mut()
        .insert(header::ALLOW, HeaderValue::from_static("GET"));
    r
}

async fn view(state: &AppState) -> Result<PyDict, Box<Response<Body>>> {
    let mut conn = super::connect(state)
        .await
        .map_err(|e| Box::new(storage_500(&e)))?;
    let settings = state.settings_snapshot();
    let overrides = stored_overrides(&mut conn)
        .await
        .map_err(|e| Box::new(internal_error(&e.to_string())))?;
    let k = DeskConstants::resolve(&settings, &overrides);
    let schema = state.db.config().schema.clone();
    let today = state.clock.today_ist();
    build(&mut conn, &settings, &k, &schema, today)
        .await
        .map_err(|e| {
            Box::new(match e {
                BuildError::Storage(s) => storage_500(&s),
                other => internal_error(&other.to_string()),
            })
        })
}

async fn regime_page(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    match view(&state).await {
        Ok(v) => render(
            &state,
            uri.path(),
            "regime.html",
            dict([("v", PyValue::Map(v))]),
        ),
        Err(r) => *r,
    }
}

async fn regime_data(State(state): State<AppState>) -> Response<Body> {
    match view(&state).await {
        Ok(v) => json(StatusCode::OK, &PyValue::Map(v)),
        Err(r) => *r,
    }
}

// ---------------------------------------------------------------------------------------------
// the index board (read-only)
// ---------------------------------------------------------------------------------------------

/// `scan_scores()` with the desk's scoring constants (the stored overrides included) — read only
/// when a scan file exists, as Python reads them only to score one.
async fn scan(state: &AppState) -> index_view::ScanScores {
    if index_view::latest_scan_path(&state.paths.root).is_none() {
        return index_view::ScanScores::default();
    }
    let settings = state.settings_snapshot();
    let overrides = match super::connect(state).await {
        Ok(mut conn) => stored_overrides(&mut conn).await.unwrap_or_default(),
        Err(_) => std::collections::BTreeMap::new(),
    };
    let cfg = crate::lanes::weekly::analyze::weekly_config(&settings, &overrides).scoring;
    index_view::scan_scores(&state.paths.root, &cfg)
}

async fn board_view(state: &AppState, uri: &Uri) -> PyDict {
    let q = MultiDict::from_query(uri);
    let nse = nse_indices::desk_nse(state);
    let today = state.clock.today_ist();
    let b = nse.full_board(today).await;
    let scan = scan(state).await;
    index_view::build_from_board(&b, &scan, &q.str_or_empty("index"))
}

async fn indices_page(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let v = board_view(&state, &uri).await;
    render(
        &state,
        uri.path(),
        "indices.html",
        dict([("v", PyValue::Map(v))]),
    )
}

async fn indices_data(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let v = board_view(&state, &uri).await;
    json(StatusCode::OK, &PyValue::Map(v))
}

/// `GET /indices/constituents?index=`: who is in the index, with a live quote and the screen's
/// score for each name. The Kite client is best-effort — an expired token costs the price
/// columns, not the panel.
async fn indices_constituents(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let q = MultiDict::from_query(&uri);
    let Some(name) = q.get("index").map(str::to_owned) else {
        return missing_field("query", "index");
    };
    let kite = crate::lanes::exec_core::exec_kite::kite(&state).ok();
    let nse = nse_indices::desk_nse(&state);
    let c = nse.constituents(&name).await;
    let scan = scan(&state).await;
    let symbols: Vec<String> = match c.get("rows") {
        Some(PyValue::List(rows)) => rows
            .iter()
            .filter_map(|r| match r {
                PyValue::Map(m) => m.get("symbol").map(PyValue::py_str),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    };
    let quotes = match kite {
        Some(k) if !symbols.is_empty() => Some(
            k.quotes(&symbols, "NSE")
                .await
                .map_err(|e| e.message),
        ),
        _ => None,
    };
    json(
        StatusCode::OK,
        &PyValue::Map(index_view::drilldown(&name, &c, &scan, quotes)),
    )
}
