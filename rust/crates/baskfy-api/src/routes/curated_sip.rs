//! SIP reminder plans on an investment (T8.4). REMINDER only — AUTO is refused.
//!
//! Ports the **GET** route of `baskfy_api.routers.curated_sip` (`APIRouter(tags=["curated-sip"])`):
//! `GET /cb/investments/{investment_id}/sip` — the newest plan on the investment, or
//! `No sip plan with id '…'.`.
//!
//! **Not ported in this wave**: `POST /cb/investments/{investment_id}/sip` (it writes; the Python
//! API keeps serving it). A SIP here is a reminder; nothing places an order (law 2).

use axum::Router;
use axum::extract::{Extension, Path, State};
use axum::response::Response;
use axum::routing::get;
use baskfy_db::row;
use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde::Serialize;
use sqlx::Row;

use super::curated_investments::investment_id_param;
use super::screens::accounts::investments::load_investment_for_user;
use super::screens::accounts::{db, dbe};
use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, not_found};
use crate::state::AppState;
use crate::wire;

/// `SipOut`.
#[derive(Debug, Clone, Serialize)]
pub struct SipOut {
    /// `id`.
    pub id: i64,
    /// `mode`.
    pub mode: String,
    /// `status`.
    pub status: String,
    /// `amount`.
    #[serde(serialize_with = "wire::ser::decimal")]
    pub amount: Decimal,
    /// `day_of_month`.
    pub day_of_month: i64,
    /// `next_fire_date`.
    #[serde(serialize_with = "wire::ser::date")]
    pub next_fire_date: NaiveDate,
}

/// `GET /cb/investments/{investment_id}/sip` (`get_sip_plan`).
///
/// # Errors
/// 401 for an anonymous caller; 400 for a non-integer id; the sole-tenant refusals; 404 for the
/// investment or for no plan; 500.
pub async fn get_sip_plan(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(raw_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let investment_id = investment_id_param(&raw_id)?;
    let inv = load_investment_for_user(&state, principal.user_id, investment_id).await?;
    let plan = sqlx::query(
        "SELECT id, mode, status, amount, day_of_month, next_fire_date FROM cb_sip_plan \
         WHERE investment_id = $1 ORDER BY id DESC LIMIT 1",
    )
    .bind(inv.id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    let Some(r) = plan else {
        return Err(not_found("sip plan", &inv.id.to_string()));
    };
    Ok(wire::json(&SipOut {
        id: r.try_get("id").map_err(db)?,
        mode: r.try_get("mode").map_err(db)?,
        status: r.try_get("status").map_err(db)?,
        amount: row::decimal(&r, "amount").map_err(|e| dbe(&e))?,
        day_of_month: i64::from(r.try_get::<i16, _>("day_of_month").map_err(db)?),
        next_fire_date: row::date(&r, "next_fire_date").map_err(|e| dbe(&e))?,
    }))
}

/// The SIP GET route, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new().route("/cb/investments/{investment_id}/sip", get(get_sip_plan))
}
