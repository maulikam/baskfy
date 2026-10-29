//! Drift scan and ledger rebase (T8.6). Compares intended holdings to broker qty.
//!
//! Ports `baskfy_api.routers.curated_drift` (`APIRouter(tags=["curated-drift"])`) at wave 4c (lane
//! writes-screens), with `baskfy_core.curated_drift`'s pure rules ported privately:
//!
//! * `POST /cb/investments/{investment_id}/drift/scan` — ledger vs the declared broker quantities;
//!   a shortfall raises (or refreshes) a `DRIFT` pending action;
//! * `POST /cb/investments/{investment_id}/drift/fix` — rebase the intended ledger to the broker's
//!   quantities and resolve the open `DRIFT` actions.
//!
//! The broker side is what the body declares. Python falls back to the desk's latest holdings
//! snapshot when nothing is declared, but reads its `holdings_json` through a bare `text()` query,
//! which hands back a string (a `text` column on the bed, and asyncpg's untyped `jsonb` alike) and
//! is never a list — so the fallback is always empty and the answer is the 400. The port gives that
//! 400 without the read (DECISIONS-RS, lane writes-screens R6.14). **No order path** (law 2).

use axum::Router;
use axum::routing::post;

use crate::state::AppState;

mod writes;

/// The routes of `baskfy_api.routers.curated_drift`, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route(
            "/cb/investments/{investment_id}/drift/scan",
            post(writes::scan_investment_drift),
        )
        .route(
            "/cb/investments/{investment_id}/drift/fix",
            post(writes::fix_investment_drift),
        )
}
