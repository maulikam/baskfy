//! ``POST /support`` — the destination of the contact form (PROMPTS.md Prompt 18 §2).
//!
//! Ports `baskfy_api.routers.support` — **not yet ported** (a documented stub). Python declares
//! `APIRouter(tags=["support"])`.
//!
//! Python routes (relative to its prefix), for the porter's checklist:
//!
//! * `POST /support`
//!
//! The lane that ports this router owns this file and never edits the shared wiring
//! (`routes.rs`, `app.rs`). Its routes are relative to `/api/v1` and run behind the
//! principal + rate-limit layer. **No route may place, modify or cancel an order** (law 2).

use axum::Router;

use crate::state::AppState;

/// The routes of `baskfy_api.routers.support`, relative to `/api/v1`. Empty until ported.
pub fn router() -> Router<AppState> {
    Router::new()
}
