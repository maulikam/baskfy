//! ``/cb/trending`` — the ranked lists the home surface shows (SC9).
//!
//! Ports `baskfy_api.routers.curated_trending` (`APIRouter(tags=["curated-trending"])`), its one
//! route `GET /cb/trending`, and the pure ranking it calls (`baskfy_core.curated_trending`, here
//! as the lane's private `accounts::trending`).
//!
//! The aggregates are platform-wide on purpose ("how many people watchlisted this" is not a
//! per-tenant number); the sole-tenant gate decides **who may ask**, and `MIN_POPULATION` decides
//! what they see. All nine lists come back every time, published or withheld with a reason.
//! Computed on request from six grouped reads, each filtered by the same visibility predicate as
//! `/explore` (not archived, `PUBLISHED`). **No order route** (law 2).

use std::collections::HashMap;

use axum::Router;
use axum::extract::{Extension, State};
use axum::response::Response;
use axum::routing::get;
use baskfy_db::row;
use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde::Serialize;
use sqlx::Row;

use super::screens::accounts::trending::{
    Candidate, MIN_ENTRIES, MIN_POPULATION, Population, TrendingList, rank_lists,
};
use super::screens::accounts::{db, dbe, scoped_sole_user_id};
use crate::auth::{Principal, require_authenticated};
use crate::problems::Problem;
use crate::state::AppState;
use crate::wire;

/// `curated_metrics.RETURN_CONVENTION`.
pub const RETURN_CONVENTION: &str = "PRICE_RETURN";
/// `curated_metrics.DIVIDENDS_INCLUDED`.
pub const DIVIDENDS_INCLUDED: bool = false;
/// `curated_metrics.RETURN_CONVENTION_NOTE`.
pub const RETURN_CONVENTION_NOTE: &str = "Returns are price returns computed from split- and \
bonus-adjusted closes. Cash dividends are excluded (docs/DECISIONS-MERGE.md M39.3), so a \
total-return series would be higher by roughly the basket's dividend yield each year - about \
1.2% for the broad Indian market - compounding.";

/// `TrendingEntryOut`.
#[derive(Debug, Clone, Serialize)]
pub struct TrendingEntryOut {
    /// `rank`.
    pub rank: i64,
    /// `basket_slug`.
    pub basket_slug: String,
    /// `basket_name`.
    pub basket_name: String,
    /// `metric_value`.
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    pub metric_value: Option<Decimal>,
    /// `metric_date`.
    #[serde(serialize_with = "wire::ser::opt_date")]
    pub metric_date: Option<NaiveDate>,
    /// `metric_display`.
    pub metric_display: String,
}

/// `TrendingListOut`.
#[derive(Debug, Clone, Serialize)]
pub struct TrendingListOut {
    /// `key`.
    pub key: &'static str,
    /// `title`.
    pub title: &'static str,
    /// `ranks_by`.
    pub ranks_by: &'static str,
    /// `metric_label`.
    pub metric_label: &'static str,
    /// `metric_kind`.
    pub metric_kind: &'static str,
    /// `population_based`.
    pub population_based: bool,
    /// `population`.
    pub population: Option<i64>,
    /// `eligible`.
    pub eligible: i64,
    /// `entries`.
    pub entries: Vec<TrendingEntryOut>,
    /// `withheld_reason`.
    pub withheld_reason: Option<&'static str>,
    /// `withheld_note`.
    pub withheld_note: Option<String>,
    /// `price_return_caveat`.
    pub price_return_caveat: bool,
}

/// `TrendingOut`.
#[derive(Debug, Clone, Serialize)]
pub struct TrendingOut {
    /// `items`.
    pub items: Vec<TrendingListOut>,
    /// `count`.
    pub count: i64,
    /// `catalog_size`.
    pub catalog_size: i64,
    /// `min_entries`.
    pub min_entries: i64,
    /// `min_population`.
    pub min_population: i64,
    /// `return_convention`.
    pub return_convention: &'static str,
    /// `dividends_included`.
    pub dividends_included: bool,
    /// `return_convention_note`.
    pub return_convention_note: &'static str,
}

fn list_out(row: TrendingList) -> TrendingListOut {
    TrendingListOut {
        key: row.def.key,
        title: row.def.title,
        ranks_by: row.def.ranks_by,
        metric_label: row.def.metric_label,
        metric_kind: row.def.metric_kind.value(),
        population_based: row.def.population_based,
        population: row.population,
        eligible: row.eligible,
        entries: row
            .entries
            .into_iter()
            .map(|e| TrendingEntryOut {
                rank: e.rank,
                basket_slug: e.slug,
                basket_name: e.name,
                metric_value: e.metric_value,
                metric_date: e.metric_date,
                metric_display: e.metric_display,
            })
            .collect(),
        withheld_reason: row.withheld_reason,
        withheld_note: row.withheld_note,
        price_return_caveat: row.price_return_caveat,
    }
}

const VISIBLE: &str =
    "SELECT id FROM cb_basket WHERE archived_at IS NULL AND visibility = 'PUBLISHED'";

async fn counts_by_basket(state: &AppState, sql: &str) -> Result<HashMap<i64, i64>, Problem> {
    let rows = sqlx::query(sql)
        .fetch_all(state.db.pool())
        .await
        .map_err(db)?;
    rows.iter()
        .map(|r| {
            Ok((
                r.try_get::<i64, _>(0).map_err(db)?,
                r.try_get::<i64, _>(1).map_err(db)?,
            ))
        })
        .collect()
}

async fn scalar_count(state: &AppState, sql: &str) -> Result<i64, Problem> {
    let n: Option<i64> = sqlx::query_scalar(sql)
        .fetch_one(state.db.pool())
        .await
        .map_err(db)?;
    Ok(n.unwrap_or(0))
}

/// `_candidates(session)`: every visible basket with every metric a list might rank it on.
async fn candidates(state: &AppState) -> Result<(Vec<Candidate>, Population), Problem> {
    let pool = state.db.pool();
    let catalog = sqlx::query(&format!(
        "SELECT b.id, b.slug, b.name, b.launched_at, m.ret_1m, m.ret_1y, m.cagr_5y, m.min_amount \
         FROM cb_basket b JOIN ({VISIBLE}) v ON v.id = b.id \
         LEFT JOIN (SELECT basket_id, max(as_of_date) AS as_of_date FROM cb_metrics \
                    GROUP BY basket_id) lm ON lm.basket_id = b.id \
         LEFT JOIN cb_metrics m ON m.basket_id = b.id AND m.as_of_date = lm.as_of_date \
         ORDER BY b.slug"
    ))
    .fetch_all(pool)
    .await
    .map_err(db)?;
    let reviewed: HashMap<i64, NaiveDate> = sqlx::query(&format!(
        "SELECT basket_id, max(effective_date) AS d FROM cb_basket_version \
         WHERE basket_id IN ({VISIBLE}) GROUP BY basket_id"
    ))
    .fetch_all(pool)
    .await
    .map_err(db)?
    .iter()
    .map(|r| {
        Ok((
            r.try_get::<i64, _>("basket_id").map_err(db)?,
            row::date(r, "d").map_err(|e| dbe(&e))?,
        ))
    })
    .collect::<Result<_, Problem>>()?;
    let watchers = counts_by_basket(
        state,
        &format!(
            "SELECT basket_id, count(DISTINCT user_id) FROM cb_watchlist_item \
             WHERE basket_id IN ({VISIBLE}) GROUP BY basket_id"
        ),
    )
    .await?;
    let investors = counts_by_basket(
        state,
        &format!(
            "SELECT basket_id, count(DISTINCT user_id) FROM cb_investment \
             WHERE basket_id IN ({VISIBLE}) AND status = 'ACTIVE' GROUP BY basket_id"
        ),
    )
    .await?;
    let mut inflows: HashMap<i64, Decimal> = HashMap::new();
    for r in sqlx::query(&format!(
        "SELECT i.basket_id, sum(o.requested_amount) AS amount FROM cb_order_batch o \
         JOIN cb_investment i ON i.id = o.investment_id WHERE i.basket_id IN ({VISIBLE}) \
         AND o.kind IN ('BUY') AND o.requested_amount IS NOT NULL GROUP BY i.basket_id"
    ))
    .fetch_all(pool)
    .await
    .map_err(db)?
    {
        if let Some(amount) = row::opt_decimal(&r, "amount").map_err(|e| dbe(&e))? {
            inflows.insert(r.try_get("basket_id").map_err(db)?, amount);
        }
    }
    let mut out = Vec::with_capacity(catalog.len());
    for r in &catalog {
        let id: i64 = r.try_get("id").map_err(db)?;
        out.push(Candidate {
            slug: r.try_get("slug").map_err(db)?,
            name: r.try_get("name").map_err(db)?,
            ret_1m: row::opt_decimal(r, "ret_1m").map_err(|e| dbe(&e))?,
            ret_1y: row::opt_decimal(r, "ret_1y").map_err(|e| dbe(&e))?,
            cagr_5y: row::opt_decimal(r, "cagr_5y").map_err(|e| dbe(&e))?,
            min_amount: row::opt_decimal(r, "min_amount").map_err(|e| dbe(&e))?,
            launched_at: row::opt_date(r, "launched_at").map_err(|e| dbe(&e))?,
            last_rebalanced_on: reviewed.get(&id).copied(),
            watchers: Some(watchers.get(&id).copied().unwrap_or(0)),
            investors: Some(investors.get(&id).copied().unwrap_or(0)),
            inflow_amount: inflows.get(&id).copied(),
        });
    }
    let population = Population {
        watchers: scalar_count(
            state,
            "SELECT count(DISTINCT user_id) FROM cb_watchlist_item",
        )
        .await?,
        investors: scalar_count(
            state,
            "SELECT count(DISTINCT user_id) FROM cb_investment WHERE status = 'ACTIVE'",
        )
        .await?,
        contributors: scalar_count(
            state,
            "SELECT count(DISTINCT user_id) FROM cb_order_batch WHERE kind IN ('BUY')",
        )
        .await?,
    };
    Ok((out, population))
}

/// `GET /cb/trending` (`list_trending`).
///
/// # Errors
/// 401 for an anonymous caller; the sole-tenant refusals; 500.
pub async fn list_trending(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    scoped_sole_user_id(&state, principal.user_id, None).await?;
    let (candidates, population) = candidates(&state).await?;
    let rows = rank_lists(&candidates, &population);
    let count = i64::try_from(rows.len()).unwrap_or(i64::MAX);
    Ok(wire::json(&TrendingOut {
        items: rows.into_iter().map(list_out).collect(),
        count,
        catalog_size: i64::try_from(candidates.len()).unwrap_or(i64::MAX),
        min_entries: i64::try_from(MIN_ENTRIES).unwrap_or(i64::MAX),
        min_population: MIN_POPULATION,
        return_convention: RETURN_CONVENTION,
        dividends_included: DIVIDENDS_INCLUDED,
        return_convention_note: RETURN_CONVENTION_NOTE,
    }))
}

/// The `/cb/trending` route, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new().route("/cb/trending", get(list_trending))
}
