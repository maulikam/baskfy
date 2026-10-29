//! Curated-basket plan previews — invest / apply / exit (SC3).
//!
//! Ports `baskfy_api.routers.curated_plans` — **not yet ported** (a documented stub). Python declares
//! `APIRouter(prefix="/cb/plans", tags=["curated-plans"])`.
//!
//! Python routes (relative to its prefix), for the porter's checklist:
//!
//! * `POST /invest`
//! * `POST /apply`
//! * `POST /exit`
//!
//! The lane that ports this router owns this file and never edits the shared wiring
//! (`routes.rs`, `app.rs`). Its routes are relative to `/api/v1` and run behind the
//! principal + rate-limit layer. **No route may place, modify or cancel an order** (law 2).

use axum::Router;

use crate::state::AppState;

/// The routes of `baskfy_api.routers.curated_plans`, relative to `/api/v1`. Empty until ported.
pub fn router() -> Router<AppState> {
    Router::new()
}
