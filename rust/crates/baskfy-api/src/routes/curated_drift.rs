//! Drift scan and ledger rebase (T8.6). Compares intended holdings to broker qty.
//!
//! Ports `baskfy_api.routers.curated_drift` — **not yet ported** (a documented stub). Python declares
//! `APIRouter(tags=["curated-drift"])`.
//!
//! Python routes (relative to its prefix), for the porter's checklist:
//!
//! * `POST /cb/investments/{investment_id}/drift/scan`
//! * `POST /cb/investments/{investment_id}/drift/fix`
//!
//! The lane that ports this router owns this file and never edits the shared wiring
//! (`routes.rs`, `app.rs`). Its routes are relative to `/api/v1` and run behind the
//! principal + rate-limit layer. **No route may place, modify or cancel an order** (law 2).

use axum::Router;

use crate::state::AppState;

/// The routes of `baskfy_api.routers.curated_drift`, relative to `/api/v1`. Empty until ported.
pub fn router() -> Router<AppState> {
    Router::new()
}
