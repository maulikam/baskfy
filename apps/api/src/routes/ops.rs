//! `app.py`'s own routes, outside `/api/v1`: `GET /health`.
//!
//! `/health` is liveness only and deliberately does not touch the database (a health check that
//! fails while PostgreSQL blips takes the process out of the load balancer at the moment it would
//! have recovered). It carries no rate limit and no principal.
//!
//! Not ported yet: `GET /metrics` (the Prometheus scrape, `baskfy_api.metrics`) and FastAPI's
//! generated `GET /api/v1/openapi.json` and `/api/v1/docs` — STATUS.md lists them.

use axum::Router;
use axum::extract::State;
use axum::response::Response;
use axum::routing::get;
use serde::Serialize;

use crate::state::AppState;
use crate::wire;

/// `HealthOut`.
#[derive(Debug, Clone, Serialize)]
pub struct HealthOut {
    /// `status` (`ok` | `degraded`).
    pub status: &'static str,
    /// `environment`.
    pub environment: String,
}

/// `health()`.
pub async fn health(State(state): State<AppState>) -> Response {
    wire::json(&HealthOut {
        status: "ok",
        environment: state.settings.environment.clone(),
    })
}

/// The ops routes, at the root (not under `/api/v1`).
pub fn router() -> Router<AppState> {
    Router::new().route("/health", get(health))
}
