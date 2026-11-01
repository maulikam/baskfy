//! `/api/public/v1` — the public read API (Prompt 20 deliverable 2). **Shut** (D9).
//!
//! Ports the gate of `baskfy_api.routers.public`: [`is_enabled`] is Python's `is_enabled` — the
//! setting `BASKFY_PUBLIC_API_ENABLED` **and** the source constant
//! `baskfy_core.public_api.DATA_REDISTRIBUTION_REVIEW.signed_off` ([`DATA_REDISTRIBUTION_REVIEW_SIGNED_OFF`],
//! `false`). `app.py` mounts the router only when both are open, so a caller gets the
//! framework's plain 404 — a 403 would confirm that a public tier exists.
//!
//! Rust reproduces that refusal by never mounting anything: `routes::api_router` does not merge
//! this module and [`router`] is empty. Nobody can turn the public tier on without a commit, in
//! either language. Its routes (`/terms`, `/status`, `/screens/{public_id}/results`,
//! `/instruments/{symbol}/factors`, `/breadth`, `/openapi.json`, `/docs`) are ported when — and
//! only when — that review is signed off.

use axum::Router;

use crate::settings::Settings;
use crate::state::AppState;

/// `baskfy_core.public_api.PUBLIC_API_PREFIX`.
pub const PUBLIC_API_PREFIX: &str = "/api/public/v1";

/// `baskfy_core.public_api.DATA_REDISTRIBUTION_REVIEW.signed_off` — the source-constant lock.
pub const DATA_REDISTRIBUTION_REVIEW_SIGNED_OFF: bool = false;

/// `is_enabled(settings)`: both locks. False in every committed configuration.
#[must_use]
pub fn is_enabled(settings: &Settings) -> bool {
    settings.public_api_enabled && DATA_REDISTRIBUTION_REVIEW_SIGNED_OFF
}

/// Empty, and never merged (D9).
pub fn router() -> Router<AppState> {
    Router::new()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_flag_alone_cannot_open_the_public_api() {
        let settings = Settings {
            public_api_enabled: true,
            ..Settings::default()
        };
        assert!(!is_enabled(&settings));
        assert!(!is_enabled(&Settings::default()));
    }
}
