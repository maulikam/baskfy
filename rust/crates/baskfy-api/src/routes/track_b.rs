//! Track B dark surfaces — paywall / public signup / fee collect (SC10). Ports
//! `baskfy_api.routers.track_b` (its GET routes) and `baskfy_api.track_b`'s gates.
//!
//! Python declares `APIRouter(tags=["track-b"])`. Ported: `GET /cb/track-b/flags`,
//! `GET /cb/track-b/free-access`, `GET /cb/paywall`, and (wave 4c) the three `POST` stubs
//! `/cb/paywall/checkout`, `/cb/public-signup`, `/cb/fees/collect`, which write nothing. The flags stay false (docs/smallcase/02): every
//! surface here 404s while its flag is off, exactly as Python's does.

use axum::Router;
use axum::extract::{RawQuery, State};
use axum::response::Response;
use axum::routing::{get, post};
use serde::Serialize;

use crate::problems::{Problem, not_found};
use crate::query::QueryParams;
use crate::settings::Settings;
use crate::state::AppState;
use crate::wire;

/// `FlagStateOut`.
#[derive(Debug, Clone, Serialize)]
pub struct FlagStateOut {
    /// `subscriptions_enabled`.
    pub subscriptions_enabled: bool,
    /// `fee_collection_enabled`.
    pub fee_collection_enabled: bool,
    /// `public_signup_enabled`.
    pub public_signup_enabled: bool,
}

/// `FreeAccessOut`.
#[derive(Debug, Clone, Serialize)]
pub struct FreeAccessOut {
    /// `basket_access` (echoed as sent).
    pub basket_access: String,
    /// `free_access`.
    pub free_access: bool,
}

/// `EnabledStubOut`.
#[derive(Debug, Clone, Serialize)]
pub struct EnabledStubOut {
    /// `enabled` (always `true`).
    pub enabled: bool,
    /// `surface`.
    pub surface: &'static str,
}

/// `baskfy_api.track_b.free_access`: everything is Free Access while subscriptions are dark.
#[must_use]
pub fn free_access(basket_access: &str, settings: &Settings) -> bool {
    if !settings.subscriptions_enabled {
        return true;
    }
    basket_access.to_uppercase() == "FREE"
}

/// `require_subscriptions_enabled`.
///
/// # Errors
/// 404 `No route with id 'paywall'.` while subscriptions are off.
pub fn require_subscriptions_enabled(settings: &Settings) -> Result<(), Problem> {
    if settings.subscriptions_enabled {
        Ok(())
    } else {
        Err(not_found("route", "paywall"))
    }
}

/// `GET /cb/track-b/flags`.
pub async fn track_b_flags(State(state): State<AppState>) -> Response {
    let s = &state.settings;
    wire::json(&FlagStateOut {
        subscriptions_enabled: s.subscriptions_enabled,
        fee_collection_enabled: s.fee_collection_enabled,
        public_signup_enabled: s.public_signup_enabled,
    })
}

/// `GET /cb/track-b/free-access?basket_access=FEE`.
pub async fn track_b_free_access(
    State(state): State<AppState>,
    RawQuery(raw): RawQuery,
) -> Response {
    let q = QueryParams::parse(raw.as_deref());
    let basket_access = q.get("basket_access").unwrap_or("FEE").to_owned();
    let free = free_access(&basket_access, &state.settings);
    wire::json(&FreeAccessOut {
        basket_access,
        free_access: free,
    })
}

/// `GET /cb/paywall`: 404 while `BASKFY_SUBSCRIPTIONS_ENABLED` is false.
///
/// # Errors
/// As [`require_subscriptions_enabled`].
pub async fn paywall(State(state): State<AppState>) -> Result<Response, Problem> {
    require_subscriptions_enabled(&state.settings)?;
    Ok(wire::json(&EnabledStubOut {
        enabled: true,
        surface: "paywall",
    }))
}

/// `require_public_signup_enabled`.
///
/// # Errors
/// 404 `No route with id 'public-signup'.` while public signup is off.
pub fn require_public_signup_enabled(settings: &Settings) -> Result<(), Problem> {
    if settings.public_signup_enabled {
        Ok(())
    } else {
        Err(not_found("route", "public-signup"))
    }
}

/// `require_fee_collection_enabled`.
///
/// # Errors
/// 404 `No route with id 'fee-collect'.` while fee collection is off.
pub fn require_fee_collection_enabled(settings: &Settings) -> Result<(), Problem> {
    if settings.fee_collection_enabled {
        Ok(())
    } else {
        Err(not_found("route", "fee-collect"))
    }
}

/// `POST /cb/paywall/checkout`: 404 while subscriptions are off. Writes nothing.
///
/// # Errors
/// As [`require_subscriptions_enabled`].
pub async fn paywall_checkout(State(state): State<AppState>) -> Result<Response, Problem> {
    require_subscriptions_enabled(&state.settings)?;
    Ok(wire::json(&EnabledStubOut {
        enabled: true,
        surface: "paywall-checkout",
    }))
}

/// `POST /cb/public-signup`: 404 while public signup is off. Writes nothing.
///
/// # Errors
/// As [`require_public_signup_enabled`].
pub async fn public_signup(State(state): State<AppState>) -> Result<Response, Problem> {
    require_public_signup_enabled(&state.settings)?;
    Ok(wire::json(&EnabledStubOut {
        enabled: true,
        surface: "public-signup",
    }))
}

/// `POST /cb/fees/collect`: 404 while fee collection is off. Writes nothing.
///
/// # Errors
/// As [`require_fee_collection_enabled`].
pub async fn fee_collect(State(state): State<AppState>) -> Result<Response, Problem> {
    require_fee_collection_enabled(&state.settings)?;
    Ok(wire::json(&EnabledStubOut {
        enabled: true,
        surface: "fee-collect",
    }))
}

/// The routes of `baskfy_api.routers.track_b`, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/cb/track-b/flags", get(track_b_flags))
        .route("/cb/track-b/free-access", get(track_b_free_access))
        .route("/cb/paywall", get(paywall))
        .route("/cb/paywall/checkout", post(paywall_checkout))
        .route("/cb/public-signup", post(public_signup))
        .route("/cb/fees/collect", post(fee_collect))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_basket_is_free_while_subscriptions_are_dark() {
        // Ports test_track_b_gates.py's pure assertions.
        let mut settings = Settings {
            subscriptions_enabled: false,
            ..Settings::default()
        };
        assert!(free_access("FEE", &settings));
        assert!(require_subscriptions_enabled(&settings).is_err());
        settings.subscriptions_enabled = true;
        assert!(!free_access("FEE", &settings));
        assert!(free_access("free", &settings));
        assert!(require_subscriptions_enabled(&settings).is_ok());
    }
}
