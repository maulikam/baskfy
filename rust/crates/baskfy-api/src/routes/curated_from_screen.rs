//! ``POST /cb/baskets/from-screen`` — save a screen as an investable basket (SB1).
//!
//! Ports `baskfy_api.routers.curated_from_screen` — **not yet ported** (a documented stub). Python declares
//! `APIRouter(tags=["curated-create"])`.
//!
//! Python routes (relative to its prefix), for the porter's checklist:
//!
//! * `POST /cb/baskets/from-screen`
//! * `POST /cb/baskets/{slug}/recut`
//!
//! The lane that ports this router owns this file and never edits the shared wiring
//! (`routes.rs`, `app.rs`). Its routes are relative to `/api/v1` and run behind the
//! principal + rate-limit layer. **No route may place, modify or cancel an order** (law 2).

use axum::Router;

use crate::state::AppState;

/// The routes of `baskfy_api.routers.curated_from_screen`, relative to `/api/v1`. Empty until ported.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/cb/baskets/from-screen", super::common::write_only())
        .route("/cb/baskets/{slug}/recut", super::common::write_only())
}
