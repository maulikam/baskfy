//! Create PRIVATE stock baskets for the sole tenant (SC8 / leaf 2.2).
//!
//! Ports `baskfy_api.routers.curated_create` (`APIRouter(tags=["curated-create"])`) at wave 4c
//! (lane writes-screens):
//!
//! * `POST /cb/baskets` — a MANUAL + PRIVATE basket with a GENESIS version and at least two
//!   weighted constituents (weights reconcile to 1.0000, `assert_weights_sum_to_one`).
//!
//! A basket is a catalog row: no broker path, no invest/apply hand-off, and **no route may place,
//! modify or cancel an order** (law 2).

use axum::Router;
use axum::routing::post;

use crate::state::AppState;

pub(crate) mod writes;

/// The routes of `baskfy_api.routers.curated_create`, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new().route("/cb/baskets", post(writes::create_private_basket))
}
