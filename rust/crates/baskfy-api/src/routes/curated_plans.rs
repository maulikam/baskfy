//! Curated-basket plan previews — invest / apply / exit (SC3).
//!
//! Ports `baskfy_api.routers.curated_plans` (`APIRouter(prefix="/cb/plans",
//! tags=["curated-plans"])`) at wave 4c (lane writes-screens), with the pure arithmetic of
//! `baskfy_core.curated_plans`, `baskfy_core.curated_metrics.shares_at_amount` and
//! `baskfy_core.market_hours_cb` ported privately ([`core`]):
//!
//! * `POST /cb/plans/invest`, `/apply`, `/exit` — a desk-shaped **preview** with a synthetic
//!   `cb-sim-{uuid}` id while the NSE cash session is open; the closed-market payload (200)
//!   outside it, never an invented plan.
//!
//! These routes create **plans, never orders**: nothing here hands off to the execution gateway,
//! and **no route may place, modify or cancel an order** (law 2; the web app never gains an
//! execute route). Python declares no principal on them, so neither does the port.

use axum::Router;
use axum::routing::post;

use crate::state::AppState;

pub(crate) mod core;
pub(crate) mod writes;

/// The routes of `baskfy_api.routers.curated_plans`, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/cb/plans/invest", post(writes::plan_invest))
        .route("/cb/plans/apply", post(writes::plan_apply))
        .route("/cb/plans/exit", post(writes::plan_exit))
}
