//! Lane **analytics / analytics_regime** (wave D, DECISIONS-RS R8.5's finer split): the
//! read-only regime overlay pages, `GET /regime` and `GET /regime/data`.
//!
//! Both read precomputed rows only (`main.py`: "No Kite calls, no signal recomputation, no
//! writes: a status page must never be able to change the state it reports"). The modules
//! behind them live beside this file in the parent: [`super::regime`] (`core/regime.py`'s
//! config, tiers and reason text), [`super::regime_store`], [`super::breadth`],
//! [`super::index_cache`] and [`super::regime_view`].
//!
//! **Not ported (loud, DECISIONS-RS lane analytics_regime R-ar.1):** `analytics/regime_run.py` and the engine it drives
//! (`core/regime.py`'s `evaluate`, `core/regime_alloc.py`) — the daily/weekly evaluation that
//! *writes* `regime_evaluations`, with its Kite index fetch; `index_view.py`/`nse_indices.py`
//! and the `/indices*` routes, whose every first paint fetches NSE live (no replay exists for
//! NSE in the harness, and this wave calls no network endpoint).

use axum::Router;
use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderValue, Response, StatusCode, Uri, header};
use axum::routing::{MethodRouter, get};

use crate::app::AppState;
use crate::wire::{PyDict, PyValue, dict};

use super::desk_settings::stored_overrides;
use super::regime_view::{BuildError, DeskConstants, build};
use super::web::{internal_error, json, render, storage_500};

/// The lane's routes.
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/regime", get_only(get(regime_page)))
        .route("/regime/data", get_only(get(regime_data)))
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
