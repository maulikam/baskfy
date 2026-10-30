//! Lane **analytics / analytics_views** (wave D, DECISIONS-RS R8.5's finer split).
//!
//! Ported here, test-first from the Python suites:
//!
//! * [`protection`] — `app/analytics/protection.py` (`test_protection.py`): stop coverage
//!   review and the stop plan. Read-only; the `/stops` page and the holdings table (other
//!   lanes) call it.
//! * [`corporate_actions`] — `app/analytics/corporate_actions.py`
//!   (`test_corporate_actions.py`): parse, describe, record, load, listing, remove, and
//!   `apply_to_book` over a [`corporate_actions::BookLot`] the tradebook lane implements.
//! * [`autorun`] — `app/analytics/autorun.py` (`test_autorun.py`, the equity half; the
//!   options-lab half is frozen in Python too).
//! * [`backtest_view`] — `app/analytics/backtest_view.py` (`test_backtest_view.py`) and the
//!   route that renders it, `GET /regime/backtest`.
//!
//! Already ported before this lane by the views lane, in the parent's own submodules (kept
//! as they are, graded by `desk_cases/views.json`): `settings.py` → `desk_settings`,
//! `ops.py` → `ops`, `daily_runs.py` → `daily_runs`. `db.py`/`pg.py` are the foundation's
//! `storage` module.
//!
//! * [`metrics`], [`benchmark`], [`performance_view`] — `metrics.py`, `benchmark.py`,
//!   `performance_view.py` and `main._holdings_block`, rendered by `GET /performance` and
//!   `GET /performance/data`.

pub mod autorun;
pub mod backtest_view;
pub mod benchmark;
pub mod corporate_actions;
pub mod metrics;
pub mod performance_view;
pub mod protection;

use axum::Router;
use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderValue, Response, StatusCode, Uri, header};
use axum::routing::{MethodRouter, get};

use crate::app::AppState;
use crate::wire::{PyValue, dict};

use super::web::{internal_error, render};

/// Where `main.regime_backtest` reads its results, relative to the desk's working directory.
pub const REGIME_BACKTEST_PATH: &str = "data/outputs/regime_backtest.json";

/// The lane's routes.
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/regime/backtest", get_only(get(regime_backtest)))
        .route("/performance", get_only(get(performance_page)))
        .route("/performance/data", get_only(get(performance_data)))
}

async fn performance_view(state: &AppState) -> Result<crate::wire::PyDict, String> {
    let mut conn = state.db.connect().await.map_err(|e| e.to_string())?;
    conn.migrate().await.map_err(|e| e.to_string())?;
    performance_view::build(&mut conn)
        .await
        .map_err(|e| e.to_string())
}

/// `GET /performance`: the stored record, then the live holdings block.
async fn performance_page(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let view = match performance_view(&state).await {
        Ok(v) => v,
        Err(e) => return internal_error(&e),
    };
    let holdings = match performance_view::holdings_block(&state).await {
        Ok(h) => h,
        Err(e) => return internal_error(&e),
    };
    render(
        &state,
        uri.path(),
        "performance.html",
        dict([("p", PyValue::Map(view)), ("h", PyValue::Map(holdings))]),
    )
}

/// `GET /performance/data`: the view model as Starlette's `JSONResponse` (NaN refused).
async fn performance_data(State(state): State<AppState>) -> Response<Body> {
    let view = match performance_view(&state).await {
        Ok(v) => v,
        Err(e) => return internal_error(&e),
    };
    match performance_view::starlette_json(&PyValue::Map(view)) {
        Ok(text) => {
            let mut r = Response::new(Body::from(text));
            r.headers_mut().insert(
                header::CONTENT_TYPE,
                HeaderValue::from_static("application/json"),
            );
            r
        }
        Err(e) => internal_error(&e),
    }
}

/// FastAPI's `@app.get`: the route's methods are `{"GET"}` alone, so `HEAD` (which axum's
/// `get` would answer and list in `Allow`) and every other verb get Starlette's 405 with
/// `Allow: GET` — the pattern the regime lane uses.
fn get_only(router: MethodRouter<AppState>) -> MethodRouter<AppState> {
    router.head(get_405).fallback(get_405)
}

async fn get_405() -> Response<Body> {
    let mut r = super::web::detail(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed");
    r.headers_mut()
        .insert(header::ALLOW, HeaderValue::from_static("GET"));
    r
}

/// `GET /regime/backtest`: the detailed backtest results, kept off the live status page.
async fn regime_backtest(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let file = state.paths.resolve(REGIME_BACKTEST_PATH);
    let data = if file.exists() {
        let bytes = match std::fs::read(&file) {
            Ok(b) => b,
            Err(e) => return internal_error(&e.to_string()),
        };
        match PyValue::from_json(&bytes) {
            Ok(v) => Some(v),
            Err(e) => return internal_error(&e),
        }
    } else {
        None
    };
    let charts = backtest_view::build(data.as_ref());
    render(
        &state,
        uri.path(),
        "regime_backtest.html",
        dict([
            ("b", data.unwrap_or(PyValue::None)),
            ("path", PyValue::Str(REGIME_BACKTEST_PATH.to_owned())),
            ("charts", charts),
        ]),
    )
}
