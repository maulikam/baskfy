//! ``/managers/*`` — a manager identity, as its holder sees it.
//!
//! Ports the **GET** routes of `baskfy_api.routers.managers` (`APIRouter(tags=["managers"])`):
//!
//! * `GET /managers/me` — the caller's manager identity, or `No manager with id 'me'.`;
//! * `GET /managers/me/revenue-share` — the recorded agreement; **404 while
//!   `BASKFY_FEE_COLLECTION_ENABLED` is false** (`track_b.require_fee_collection_enabled`), and
//!   `unset: true` (never a rate of zero) when no agreement exists.
//!
//! The writes (`POST /managers/me`, `PATCH /managers/{slug}` — staff review —, and
//! `POST`/`DELETE /managers/me/baskets/{basket_slug}/publish`) are ported at wave 4c
//! ([`writes`]). It places nothing (law 2).

use axum::Router;
use axum::extract::{Extension, State};
use axum::response::Response;
use axum::routing::{get, patch, post};
use baskfy_db::row;
use chrono::{DateTime, NaiveDate, Utc};
use serde::Serialize;
use sqlx::Row;
use sqlx::postgres::PgRow;

mod writes;

use super::screens::accounts::{db, dbe};
use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, not_found};
use crate::state::AppState;
use crate::wire;

/// `_DISCLAIMER`.
pub const DISCLAIMER: &str = "Registration details are captured as supplied and are not verified \
by Baskfy unless a verification date is shown. A registration is not a statement by Baskfy that \
this manager may manage your money.";

/// `manager_onboarding.PUBLISHABLE_STATES`.
pub const PUBLISHABLE_STATES: [&str; 1] = ["APPROVED"];

/// `manager_onboarding.may_publish(state)`: the single predicate.
#[must_use]
pub fn may_publish(state: &str) -> bool {
    PUBLISHABLE_STATES.contains(&state)
}

/// `ManagerOut`.
#[derive(Debug, Clone, Serialize)]
pub struct ManagerOut {
    /// `slug`.
    pub slug: String,
    /// `name`.
    pub name: String,
    /// `kind`.
    pub kind: String,
    /// `state`.
    pub state: String,
    /// `may_publish`.
    pub may_publish: bool,
    /// `sebi_reg_type`.
    pub sebi_reg_type: String,
    /// `sebi_reg_no`.
    pub sebi_reg_no: Option<String>,
    /// `sebi_reg_valid_from`.
    #[serde(serialize_with = "wire::ser::opt_date")]
    pub sebi_reg_valid_from: Option<NaiveDate>,
    /// `sebi_reg_valid_to`.
    #[serde(serialize_with = "wire::ser::opt_date")]
    pub sebi_reg_valid_to: Option<NaiveDate>,
    /// `sebi_reg_verified_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub sebi_reg_verified_at: Option<DateTime<Utc>>,
    /// `registration_disclaimer`.
    pub registration_disclaimer: &'static str,
}

/// `RevenueShareOut`.
#[derive(Debug, Clone, Serialize)]
pub struct RevenueShareOut {
    /// `manager_slug`.
    pub manager_slug: String,
    /// `rate_bps`.
    pub rate_bps: Option<i64>,
    /// `effective_from`.
    #[serde(serialize_with = "wire::ser::opt_date")]
    pub effective_from: Option<NaiveDate>,
    /// `effective_to`.
    #[serde(serialize_with = "wire::ser::opt_date")]
    pub effective_to: Option<NaiveDate>,
    /// `note`.
    pub note: Option<String>,
    /// `unset`.
    pub unset: bool,
}

pub(crate) fn manager_out(r: &PgRow) -> Result<ManagerOut, Problem> {
    let state: String = r.try_get("state").map_err(db)?;
    Ok(ManagerOut {
        slug: r.try_get("slug").map_err(db)?,
        name: r.try_get("name").map_err(db)?,
        kind: r.try_get("kind").map_err(db)?,
        may_publish: may_publish(&state),
        state,
        sebi_reg_type: r.try_get("sebi_reg_type").map_err(db)?,
        sebi_reg_no: r.try_get("sebi_reg_no").map_err(db)?,
        sebi_reg_valid_from: row::opt_date(r, "sebi_reg_valid_from").map_err(|e| dbe(&e))?,
        sebi_reg_valid_to: row::opt_date(r, "sebi_reg_valid_to").map_err(|e| dbe(&e))?,
        sebi_reg_verified_at: row::opt_timestamptz(r, "sebi_reg_verified_at")
            .map_err(|e| dbe(&e))?,
        registration_disclaimer: DISCLAIMER,
    })
}

/// `_mine(session, user_id)`: the caller's manager identity, or 404. Never another account's.
async fn mine(state: &AppState, user_id: i64) -> Result<PgRow, Problem> {
    sqlx::query(
        "SELECT id, slug, name, kind, state, sebi_reg_type, sebi_reg_no, sebi_reg_valid_from, \
         sebi_reg_valid_to, sebi_reg_verified_at FROM cb_manager WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?
    .ok_or_else(|| not_found("manager", "me"))
}

/// `GET /managers/me` (`my_manager`).
///
/// # Errors
/// 401 for an anonymous caller; 404 when the caller holds no manager identity; 500.
pub async fn my_manager(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let found = mine(&state, principal.require_user()?).await?;
    Ok(wire::json(&manager_out(&found)?))
}

/// `GET /managers/me/revenue-share` (`revenue_share`).
///
/// # Errors
/// 401 for an anonymous caller; 404 `No route with id 'fee-collect'.` while fee collection is
/// dark; 404 for no manager identity; 500.
pub async fn revenue_share(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    if !state.settings.fee_collection_enabled {
        return Err(not_found("route", "fee-collect"));
    }
    let manager = mine(&state, principal.require_user()?).await?;
    let manager_id: i64 = manager.try_get("id").map_err(db)?;
    let manager_slug: String = manager.try_get("slug").map_err(db)?;
    let agreement = sqlx::query(
        "SELECT rate_bps, effective_from, effective_to, note FROM cb_manager_revenue_share \
         WHERE manager_id = $1 AND effective_to IS NULL ORDER BY effective_from DESC LIMIT 1",
    )
    .bind(manager_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    let out = match agreement {
        None => RevenueShareOut {
            manager_slug,
            rate_bps: None,
            effective_from: None,
            effective_to: None,
            note: None,
            unset: true,
        },
        Some(a) => RevenueShareOut {
            manager_slug,
            rate_bps: Some(i64::from(a.try_get::<i32, _>("rate_bps").map_err(db)?)),
            effective_from: Some(row::date(&a, "effective_from").map_err(|e| dbe(&e))?),
            effective_to: row::opt_date(&a, "effective_to").map_err(|e| dbe(&e))?,
            note: a.try_get("note").map_err(db)?,
            unset: false,
        },
    };
    Ok(wire::json(&out))
}

/// The `/managers` routes, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/managers/{slug}", patch(writes::review_manager))
        .route(
            "/managers/me/baskets/{basket_slug}/publish",
            post(writes::publish_basket).delete(writes::unpublish_basket),
        )
        .route(
            "/managers/me",
            get(my_manager)
                .post(writes::apply_as_manager)
                .patch(writes::review_manager_me),
        )
        .route("/managers/me/revenue-share", get(revenue_share))
}
