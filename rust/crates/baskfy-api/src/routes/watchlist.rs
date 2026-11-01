//! Instrument watchlist + discover preferences (AF I.2 / I.4).
//!
//! Ports the **GET** routes of `baskfy_api.routers.watchlist` (`APIRouter(tags=["watchlist"])`,
//! mounted inside `explore`'s router in Python):
//!
//! * `GET /watchlist/instruments` — the sole tenant's watched instruments, newest first, each
//!   with its last close and the move since it was watched;
//! * `GET /watchlist/discover-preferences` — the saved preferences, or the documented defaults
//!   with `saved: false`.
//!
//! Both are sole-tenant surfaces (`scoped_sole_user_id`): a signed-in account that is not the
//! deployment's tenant gets a 404 with `reason: not-the-sole-tenant`.
//!
//! The writes (`POST /watchlist/instruments`, `DELETE /watchlist/instruments/{symbol}`,
//! `PUT /watchlist/discover-preferences`) are ported at wave 4c ([`writes`]). **No order or
//! execute route** (law 2).

use axum::Router;
use axum::extract::{Extension, State};
use axum::response::Response;
use axum::routing::{delete, get};
use baskfy_db::row;
use chrono::{DateTime, Utc};
use rust_decimal::{Decimal, RoundingStrategy};
use serde::Serialize;
use sqlx::Row;

mod writes;

use super::screens::accounts::{db, dbe, scoped_sole_user_id};
use crate::auth::{Principal, require_authenticated};
use crate::problems::Problem;
use crate::state::AppState;
use crate::wire;

/// `InstrumentWatchItemOut`.
#[derive(Debug, Clone, Serialize)]
pub struct InstrumentWatchItemOut {
    /// `symbol`.
    pub symbol: String,
    /// `name`.
    pub name: String,
    /// `watched_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub watched_at: DateTime<Utc>,
    /// `close_at_watch`.
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    pub close_at_watch: Option<Decimal>,
    /// `last_close`.
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    pub last_close: Option<Decimal>,
    /// `moved_pct`.
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    pub moved_pct: Option<Decimal>,
}

/// `InstrumentWatchlistOut`.
#[derive(Debug, Clone, Serialize)]
pub struct InstrumentWatchlistOut {
    /// `items`.
    pub items: Vec<InstrumentWatchItemOut>,
    /// `count`.
    pub count: i64,
}

/// `DiscoverPreferencesOut`.
#[derive(Debug, Clone, Serialize)]
pub struct DiscoverPreferencesOut {
    /// `goal`.
    pub goal: String,
    /// `horizon`.
    pub horizon: String,
    /// `risk`.
    pub risk: String,
    /// `amount`.
    #[serde(serialize_with = "wire::ser::decimal")]
    pub amount: Decimal,
    /// `rebalance`.
    pub rebalance: String,
    /// `onboarding_completed_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub onboarding_completed_at: Option<DateTime<Utc>>,
    /// `updated_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub updated_at: Option<DateTime<Utc>>,
    /// `saved`.
    pub saved: bool,
}

/// `Decimal.quantize(Decimal("0.01"))` under the default context (half-even), always two places.
#[must_use]
pub fn quantize_2(d: Decimal) -> Decimal {
    let mut q = d.round_dp_with_strategy(2, RoundingStrategy::MidpointNearestEven);
    q.rescale(2);
    q
}

/// `_moved_pct(close_at_watch, last_close)`.
#[must_use]
pub fn moved_pct(close_at_watch: Option<Decimal>, last_close: Option<Decimal>) -> Option<Decimal> {
    let (watch, last) = (close_at_watch?, last_close?);
    if watch.is_zero() {
        return None;
    }
    let ratio = (last - watch).checked_div(watch)?;
    Some(quantize_2(ratio * Decimal::ONE_HUNDRED))
}

/// `GET /watchlist/instruments` (`list_instrument_watchlist`).
///
/// # Errors
/// 401 for an anonymous caller; the sole-tenant refusals (404/503); 500.
pub async fn list_instrument_watchlist(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let rows = sqlx::query(
        "SELECT w.watched_at, w.close_at_watch, i.id AS instrument_id, i.symbol, i.name \
         FROM instrument_watch_item w JOIN instrument i ON i.id = w.instrument_id \
         WHERE w.user_id = $1 ORDER BY w.watched_at DESC",
    )
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut items = Vec::with_capacity(rows.len());
    for r in &rows {
        let instrument_id: i64 = r.try_get("instrument_id").map_err(db)?;
        // `_last_close`: the newest adjusted close.
        let last = sqlx::query(
            "SELECT close FROM ohlcv_daily WHERE instrument_id = $1 ORDER BY date DESC LIMIT 1",
        )
        .bind(instrument_id)
        .fetch_optional(state.db.pool())
        .await
        .map_err(db)?;
        let last_close = match last {
            Some(l) => Some(row::decimal(&l, "close").map_err(|e| dbe(&e))?),
            None => None,
        };
        let close_at_watch = row::opt_decimal(r, "close_at_watch").map_err(|e| dbe(&e))?;
        items.push(InstrumentWatchItemOut {
            symbol: r.try_get("symbol").map_err(db)?,
            name: r.try_get("name").map_err(db)?,
            watched_at: row::timestamptz(r, "watched_at").map_err(|e| dbe(&e))?,
            close_at_watch,
            last_close,
            moved_pct: moved_pct(close_at_watch, last_close),
        });
    }
    let count = i64::try_from(items.len()).unwrap_or(i64::MAX);
    Ok(wire::json(&InstrumentWatchlistOut { items, count }))
}

/// `GET /watchlist/discover-preferences` (`get_discover_preferences`).
///
/// # Errors
/// 401 for an anonymous caller; the sole-tenant refusals; 500.
pub async fn get_discover_preferences(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let found = sqlx::query(
        "SELECT goal, horizon, risk, amount, rebalance, onboarding_completed_at, updated_at \
         FROM user_discover_preferences WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    let out = match found {
        None => DiscoverPreferencesOut {
            goal: "long-term-growth".to_owned(),
            horizon: "5-plus".to_owned(),
            risk: "moderate".to_owned(),
            amount: Decimal::from(500_000),
            rebalance: "any".to_owned(),
            onboarding_completed_at: None,
            updated_at: None,
            saved: false,
        },
        Some(r) => DiscoverPreferencesOut {
            goal: r.try_get("goal").map_err(db)?,
            horizon: r.try_get("horizon").map_err(db)?,
            risk: r.try_get("risk").map_err(db)?,
            amount: row::decimal(&r, "amount").map_err(|e| dbe(&e))?,
            rebalance: r.try_get("rebalance").map_err(db)?,
            onboarding_completed_at: row::opt_timestamptz(&r, "onboarding_completed_at")
                .map_err(|e| dbe(&e))?,
            updated_at: Some(row::timestamptz(&r, "updated_at").map_err(|e| dbe(&e))?),
            saved: true,
        },
    };
    Ok(wire::json(&out))
}

/// The watchlist routes, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        // `explore.remove_watchlist` (wave 6): Starlette tries it before this router's routes, so a
        // DELETE of either static path below is that route with the segment as the slug.
        .route(
            "/watchlist/{slug}",
            delete(super::explore::remove_watchlist),
        )
        .route(
            "/watchlist/instruments/{symbol}",
            delete(writes::remove_instrument_watch),
        )
        .route(
            "/watchlist/instruments",
            get(list_instrument_watchlist)
                .post(writes::add_instrument_watch)
                .delete(super::explore::remove_watchlist_instruments),
        )
        .route(
            "/watchlist/discover-preferences",
            get(get_discover_preferences)
                .put(writes::put_discover_preferences)
                .delete(super::explore::remove_watchlist_discover_preferences),
        )
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;
    use std::str::FromStr;

    #[test]
    fn moved_pct_is_quantized_half_even_to_two_places() {
        let d = |s: &str| Decimal::from_str(s).unwrap();
        assert_eq!(
            moved_pct(Some(d("87.41")), Some(d("100.0000")))
                .unwrap()
                .to_string(),
            "14.40"
        );
        assert_eq!(
            moved_pct(Some(d("200")), Some(d("200")))
                .unwrap()
                .to_string(),
            "0.00"
        );
        assert_eq!(moved_pct(Some(d("0")), Some(d("1"))), None);
        assert_eq!(moved_pct(None, Some(d("1"))), None);
        assert_eq!(quantize_2(d("0.125")).to_string(), "0.12");
        assert_eq!(quantize_2(d("-0.135")).to_string(), "-0.14");
    }
}
