//! Lane **weekly** — the weekly momentum book: /analyze → review → /execute (confirm) → /stops/arm.
//!
//! **STATUS (lane weekly_plan, 30 Oct 2026):** `POST /analyze` and `POST /execute` are ported
//! (`analyze.rs`, with `plan_store.rs`, `strategy.rs` and the engine modules), the generated
//! scan included (`scan_source.rs`, wave D2, DECISIONS-RS R8.WP1); the other
//! routes below belong to the sibling lanes sharing this parent and answer 404 until they land. The lane owns this file (and may add
//! private submodules under `src/lanes/weekly/`); it never edits `lib.rs`, `app.rs` or
//! `lanes.rs`. A new dependency is a DECISIONS-RS entry the integrator applies.
//!
//! **Python it ports:**
//! * `app/main.py: home, analyze, execute, stops, stops/arm, _funding_check, _plan_from_scan, _uploaded_scan, _generated_scan`
//! * `app/{scoring,rebalance,scan_source,breadth_source,costs,reconcile,exit_rules}.py`
//! * `app/analytics/{plan_store,protection,reconcile_view}.py`
//! * `app/strategies/{base,momentum_weekly,intraday_skeleton}.py`
//!
//! **Routes:**
//! * `GET /`
//! * `POST /analyze`
//! * `POST /execute`
//! * `GET /stops`
//! * `POST /stops/arm`
//! * `GET /reconcile`
//! * `GET /reconcile/data`
//!
//! **Tests to port first** (`kite-momentum-rebalancer/tests/`, test functions per file):
//! * `test_plan_store.py` (30)
//! * `test_stops_route.py` (21)
//! * `test_protection.py` (41)
//! * `test_rebalance_pricing.py` (12)
//! * `test_no_trade_band.py` (12)
//! * `test_generated_scan.py` (15)
//! * `test_breadth_and_shadow.py` (15)
//! * `test_costs.py` (14)
//! * `test_execution_report.py` (12)
//! * `test_holdings_same_day.py` (15)
//! * `test_reconcile.py` (21)
//! * `test_reconcile_view.py` (12)
//! * `test_desk_score_parity.py` (3)
//! * `test_desk_config_parity.py` (4)
//! * `test_scoring_moved_to_core.py` (2)
//! * `test_basket_moved_to_core.py` (4)
//! * `test_exposure_moved_to_core.py` (4)
//!
//! 237 test functions in all (before parametrisation).

pub mod analyze;
pub mod basket;
pub mod costs;
pub mod frame;
pub mod pandas;
pub mod plan_store;
pub mod scan_source;
pub mod score;
pub mod stops;
pub mod strategy;
pub mod weekly_books;

use axum::Router;

use crate::app::AppState;

/// The lane's routes: `POST /analyze`, `POST /execute`, `GET /stops`, `POST /stops/arm`.
pub fn routes() -> Router<AppState> {
    Router::new()
        .merge(analyze::routes())
        .merge(stops::routes())
        .merge(weekly_books::routes())
}

/// The lane's template globals and filters (e.g. the fno lane's `fno_badge`).
pub fn register_templates(templates: &mut crate::templates::Templates) {
    let _ = templates;
}

/// Run one of the lane's background services by name; `None` if the name is not this lane's.
pub async fn run_service(name: &str, state: &AppState) -> Option<Result<(), String>> {
    let _ = state;
    let _ = name;
    None
}
