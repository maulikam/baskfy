//! `/baskets/plan/kite` and `/explore/{slug}/kite` — the Zerodha Publisher hand-off payload.
//!
//! Ports `baskfy_api.routers.kite` (with `baskfy_api.kite_basket` in
//! [`super::meta::reference::curated`]). **Two GETs, and they stay GETs:** each returns the
//! `api_key` and the `data` rows the *user's browser* posts to Kite's basket form, where the user
//! reviews and confirms in their own session. Nothing here talks to a broker, holds a credential
//! or produces a fill — law 2 and non-negotiable #1 are untouched because there is no order.
//!
//! * `GET /baskets/plan/kite` — the desk's latest plan (`desk.rebalance_*`), shaped for Kite;
//! * `GET /explore/{slug}/kite?amount=` — a published basket's weights turned into whole-share
//!   quantities at `amount` (`curated_plans.build_invest_plan`), priced at the latest `close_raw`.

use std::collections::BTreeMap;

use axum::extract::{Path, RawQuery, State};
use axum::response::Response;
use axum::routing::get;
use axum::{Extension, Router};
use chrono::Duration;
use rust_decimal::Decimal;
use sqlx::Row;

use super::meta::reference::curated::{PlanError, build_basket, build_invest_plan};
use super::meta::reference::{self as r, Params};
use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, ProblemType, bad_request, not_found, pipeline_degraded};
use crate::state::AppState;
use crate::wire;

/// `CLOSE_LOOKBACK_DAYS`.
pub const CLOSE_LOOKBACK_DAYS: i64 = 30;

/// `GET /baskets/plan/kite`: the desk's latest plan as a Kite Publisher basket.
///
/// # Errors
/// 401 for an anonymous caller, 404 when the desk has recorded no plans (or has no desk schema),
/// 500 for a database failure.
pub async fn plan_as_kite_basket(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let none = || Problem::new(ProblemType::NotFound, "the desk has recorded no plans yet.");
    let version = sqlx::query(
        "SELECT version_id FROM \"desk\".rebalance_versions ORDER BY created_ts DESC LIMIT 1",
    )
    .fetch_optional(state.db.pool())
    .await;
    let version = match version {
        Ok(v) => v,
        Err(e) => {
            // `is_missing_desk_data`: asyncpg's UndefinedTableError (SQLSTATE 42P01) is "there is
            // no desk history here"; anything else is a 500.
            let e = baskfy_db::DbError::from(e);
            if e.sqlstate() == Some("42P01") {
                return Err(none());
            }
            return Err(crate::db_failure(&e));
        }
    };
    let Some(version) = version else {
        return Err(none());
    };
    let version_id: String = version.try_get("version_id").map_err(r::db)?;
    let orders = sqlx::query(
        "SELECT symbol, side, planned_qty FROM \"desk\".rebalance_orders WHERE version_id = $1 \
         ORDER BY side DESC, symbol",
    )
    .bind(&version_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    let mut legs = Vec::with_capacity(orders.len());
    for o in &orders {
        let qty: Option<i64> = o.try_get("planned_qty").map_err(r::db)?;
        legs.push((
            o.try_get::<String, _>("symbol").map_err(r::db)?,
            o.try_get::<String, _>("side").map_err(r::db)?,
            qty.unwrap_or(0).to_string(),
        ));
    }
    Ok(wire::json(&build_basket(
        &legs,
        state.settings.kite_publisher_api_key.as_str(),
    )))
}

/// `GET /explore/{slug}/kite?amount=`: a published basket at `amount`, as a Kite basket.
///
/// # Errors
/// 401 for an anonymous caller; 400 for a missing or bad amount or a plan the arithmetic refuses;
/// 404 for an unknown, hidden, version-less or empty basket; 503 when a constituent has no recent
/// close; 500 for a database failure.
pub async fn basket_as_kite_basket(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(slug): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let mut p = Params::new(raw.as_deref());
    if p.q.get("amount").is_none() {
        p.v.error("amount", "Field required", "missing");
    }
    let amount = p.decimal("amount", true);
    p.finish()?;
    // Finite or extreme alike: `Decimal` holds the literal exactly, and so does `WideDecimal`.
    let amount = amount
        .ok()
        .flatten()
        .and_then(|value| value.wide())
        .ok_or_else(|| r::unexpected("amount is not a finite decimal"))?;

    let trimmed = r::py_strip(&slug);
    let ids: Vec<i64> = sqlx::query_scalar(
        "SELECT id FROM cb_basket WHERE slug = $1 AND archived_at IS NULL \
         AND visibility = 'PUBLISHED'",
    )
    .bind(trimmed)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    let Some(basket_id) = ids.first().copied() else {
        return Err(not_found("basket", &slug));
    };
    let version_id: Option<i64> = sqlx::query_scalar(
        "SELECT id FROM cb_basket_version WHERE basket_id = $1 ORDER BY version_no DESC LIMIT 1",
    )
    .bind(basket_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(r::db)?;
    let repr = baskfy_providers::py::repr_str(&slug);
    let Some(version_id) = version_id else {
        return Err(Problem::new(
            ProblemType::NotFound,
            format!("basket {repr} has no published version."),
        ));
    };
    let rows = sqlx::query(
        "SELECT i.id, i.symbol, c.weight FROM instrument i \
         JOIN cb_constituent c ON c.instrument_id = i.id WHERE c.version_id = $1",
    )
    .bind(version_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    if rows.is_empty() {
        return Err(Problem::new(
            ProblemType::NotFound,
            format!("basket {repr} has no constituents."),
        ));
    }
    let mut weights: BTreeMap<String, Decimal> = BTreeMap::new();
    let mut by_id: BTreeMap<i64, String> = BTreeMap::new();
    for row in &rows {
        let id: i64 = row.try_get("id").map_err(r::db)?;
        let symbol: String = row.try_get("symbol").map_err(r::db)?;
        let weight = baskfy_db::row::decimal(row, "weight").map_err(|e| r::dbe(&e))?;
        weights.insert(symbol.clone(), weight);
        by_id.insert(id, symbol);
    }
    let today = r::today_ist();
    let ids: Vec<i64> = by_id.keys().copied().collect();
    let price_rows = sqlx::query(
        "SELECT DISTINCT ON (instrument_id) instrument_id, close_raw FROM ohlcv_daily \
         WHERE instrument_id = ANY($1) AND date >= $2 AND date <= $3 \
         ORDER BY instrument_id, date DESC",
    )
    .bind(&ids)
    .bind(today - Duration::days(CLOSE_LOOKBACK_DAYS))
    .bind(today)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    let mut prices: BTreeMap<String, Decimal> = BTreeMap::new();
    for row in &price_rows {
        let id: i64 = row.try_get("instrument_id").map_err(r::db)?;
        let close = baskfy_db::row::decimal(row, "close_raw").map_err(|e| r::dbe(&e))?;
        if let Some(symbol) = by_id.get(&id) {
            prices.insert(symbol.clone(), close);
        }
    }
    let missing: Vec<&str> = weights
        .keys()
        .filter(|s| !prices.contains_key(*s))
        .map(String::as_str)
        .collect();
    if !missing.is_empty() {
        return Err(pipeline_degraded(&format!(
            "no recent close for {}, so a share count cannot be worked out.",
            missing.join(", ")
        )));
    }
    let legs = build_invest_plan(&weights, &prices, &amount).map_err(|e| match e {
        PlanError::Value(message) => bad_request(&message, None),
        PlanError::Arithmetic(trap) => r::unexpected(&trap),
    })?;
    let orders: Vec<(String, String, String)> = legs
        .into_iter()
        .map(|l| (l.symbol, "BUY".to_owned(), l.quantity))
        .collect();
    Ok(wire::json(&build_basket(
        &orders,
        state.settings.kite_publisher_api_key.as_str(),
    )))
}

/// The routes of `baskfy_api.routers.kite`, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/baskets/plan/kite", get(plan_as_kite_basket))
        .route("/explore/{slug}/kite", get(basket_as_kite_basket))
}
