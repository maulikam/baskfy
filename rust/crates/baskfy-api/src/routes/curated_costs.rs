//! Costs and returns after accrued (uncollected) fees (T8.5). Collection stays off.
//!
//! Ports `baskfy_api.routers.curated_costs` (`APIRouter(tags=["curated-costs"])`), its one route
//! `GET /cb/investments/{investment_id}/costs`: the investor snapshot plus the platform fees
//! accrued against the investment's batches. **It does not collect**, and it places nothing
//! (law 2). Holdings are marked at cost without a live Kite mark (no session reader in this
//! wave — Python's own no-session answer).

use axum::Router;
use axum::extract::{Extension, Path, State};
use axum::response::Response;
use axum::routing::get;
use baskfy_core::decimal::WideDecimal;
use baskfy_db::row;
use rust_decimal::Decimal;
use serde::Serialize;

use super::curated_investments::investment_id_param;
use super::screens::accounts::investments::{
    Batch, Holding, SnapshotOut, batches_of, buy_amounts, holdings_of, live_prices_by_instrument,
    load_investment_for_user, marked_prices, money, snapshot,
};
use super::screens::accounts::{db, dbe};
use crate::auth::{Principal, require_authenticated};
use crate::entitlements::utc_now;
use crate::problems::Problem;
use crate::state::AppState;
use crate::wire;

/// `CostsOut`.
#[derive(Debug, Clone, Serialize)]
pub struct CostsOut {
    /// `snapshot` (`CostsSnapshotOut`).
    pub snapshot: SnapshotOut,
    /// `accrued_fees_total`.
    #[serde(serialize_with = "wire::ser::decimal")]
    pub accrued_fees_total: Decimal,
    /// `returns_after_fees`.
    #[serde(serialize_with = "wire::ser::decimal")]
    pub returns_after_fees: Decimal,
    /// `collected` — always false: collection is off.
    pub collected: bool,
    /// `marked_at_cost`.
    pub marked_at_cost: bool,
}

/// `GET /cb/investments/{investment_id}/costs` (`get_investment_costs`).
///
/// # Errors
/// 401 for an anonymous caller; 400 for a non-integer id; the sole-tenant refusals; 404; 500.
pub async fn get_investment_costs(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(raw_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let investment_id = investment_id_param(&raw_id)?;
    let inv = load_investment_for_user(&state, principal.user_id, investment_id).await?;
    let now = utc_now();
    let held: Vec<Holding> = holdings_of(&state, &[inv.id])
        .await?
        .into_iter()
        .map(|(_, h)| h)
        .collect();
    let batches = batches_of(&state, &[inv.id], "").await?;
    let own: Vec<&Batch> = batches.iter().collect();
    let ids: Vec<i64> = held.iter().map(|h| h.instrument_id).collect();
    let live = live_prices_by_instrument(&ids);
    let (prices, marked_at_cost) = marked_prices(&held, &live);
    let snap = snapshot(
        &buy_amounts(&own),
        &held,
        &prices,
        inv.created_at.date_naive(),
        now.date_naive(),
        marked_at_cost,
    )?;
    let batch_ids: Vec<i64> = batches.iter().map(|b| b.id).collect();
    let mut fees = WideDecimal::zero();
    if !batch_ids.is_empty() {
        for r in sqlx::query("SELECT total FROM cb_fee_ledger WHERE batch_id = ANY($1)")
            .bind(&batch_ids)
            .fetch_all(state.db.pool())
            .await
            .map_err(db)?
        {
            fees = fees.add(&WideDecimal::from(
                row::decimal(&r, "total").map_err(|e| dbe(&e))?,
            ));
        }
    }
    let accrued = money(&fees)?;
    let returns = WideDecimal::from(snap.current_returns);
    let returns_after_fees = money(&returns.sub(&WideDecimal::from(accrued)))?;
    Ok(wire::json(&CostsOut {
        snapshot: snap,
        accrued_fees_total: accrued,
        returns_after_fees,
        collected: false,
        marked_at_cost,
    }))
}

/// The costs route, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new().route(
        "/cb/investments/{investment_id}/costs",
        get(get_investment_costs),
    )
}
