//! `/explore` catalog + `/watchlist` — curated-basket discovery API (SC2).
//!
//! Ports the **GET** routes of `baskfy_api.routers.explore`:
//!
//! * `GET /explore` — the catalogue grid, filtered and sorted;
//! * `GET /explore/managers`, `GET /explore/managers/{slug}`;
//! * `GET /explore/collections`, `GET /explore/collections/{slug}` — shelves expanded to cards;
//! * `GET /explore/{slug}` — one card;
//! * `GET /explore/{slug}/constituents` — the newest published version's names;
//! * `GET /explore/{slug}/performance` — the version-aware NAV over fully covered days;
//! * `GET /watchlist` — the sole tenant's watched baskets.
//!
//! `POST /watchlist` and `DELETE /watchlist/{slug}` write and wait for the pool's write decision
//! (R5.2); they are not registered, so Python keeps serving them. **No order or execute route.**
//! Every route needs a signed-in caller; every read of `cb_basket` applies
//! `curated_visibility.visible_baskets()` (not archived, `PUBLISHED`).
//!
//! Matching: Python registers `/explore/managers` and `/explore/collections` before
//! `/explore/{slug}`, so the static paths win there exactly as axum's static-first rule makes them.

use std::collections::{BTreeMap, HashMap};

use axum::extract::{Path, RawQuery, State};
use axum::response::Response;
use axum::routing::get;
use axum::{Extension, Router};
use baskfy_core::decimal::{Rounding, WideDecimal};
use chrono::{DateTime, Duration, NaiveDate, Utc};
use rust_decimal::Decimal;
use serde::Serialize;
use sqlx::Row;
use sqlx::postgres::PgRow;

use super::meta::reference::curated::{self, BasketVersion};
use super::meta::reference::{self as r, Params, PyDec, path_max_length};
use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, not_found};
use crate::query::Validation;
use crate::state::AppState;
use crate::wire;

/// `SLUG_MAX_LENGTH`.
pub const SLUG_MAX_LENGTH: usize = 120;

/// `SortField`.
pub const SORT_FIELDS: [&str; 7] = [
    "min_amount",
    "ret_1y",
    "cagr_3y",
    "cagr_5y",
    "name",
    "launched_at",
    "volatility",
];

/// The visibility predicate over `cb_basket b`.
const VISIBLE: &str = "b.archived_at IS NULL AND b.visibility = 'PUBLISHED'";

/// The card query's select list and joins (basket `b`, manager `m`, latest metrics `x`).
const CARD_SQL: &str = "SELECT b.id, b.slug, b.name, b.access, b.visibility, b.type, \
    b.categories, b.rebalance_frequency, b.source, b.description_md, b.launched_at, \
    m.slug AS m_slug, m.name AS m_name, m.kind AS m_kind, \
    x.basket_id AS x_basket_id, x.as_of_date, x.min_amount, x.volatility_bucket, \
    x.volatility_value, x.ret_1m, x.ret_6m, x.ret_1y, x.cagr_3y, x.cagr_5y, \
    x.since_inception_pct \
    FROM cb_basket b JOIN cb_manager m ON m.id = b.manager_id \
    LEFT OUTER JOIN (SELECT basket_id, max(as_of_date) AS as_of_date FROM cb_metrics \
      GROUP BY basket_id) l ON l.basket_id = b.id \
    LEFT OUTER JOIN cb_metrics x ON x.basket_id = b.id AND x.as_of_date = l.as_of_date";

// ---------------------------------------------------------------------------------------------
// Shapes
// ---------------------------------------------------------------------------------------------

/// `MetricsOut`.
#[derive(Debug, Clone, Serialize)]
struct MetricsOut {
    #[serde(serialize_with = "wire::ser::opt_date")]
    as_of_date: Option<NaiveDate>,
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    min_amount: Option<Decimal>,
    volatility_bucket: Option<String>,
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    volatility_value: Option<Decimal>,
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    ret_1m: Option<Decimal>,
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    ret_6m: Option<Decimal>,
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    ret_1y: Option<Decimal>,
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    cagr_3y: Option<Decimal>,
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    cagr_5y: Option<Decimal>,
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    since_inception_pct: Option<Decimal>,
    headline_label: Option<String>,
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    headline_pct: Option<Decimal>,
    return_convention: &'static str,
    dividends_included: bool,
    return_convention_note: String,
}

/// `ManagerBriefOut`.
#[derive(Debug, Clone, Serialize)]
struct ManagerBriefOut {
    slug: String,
    name: String,
    kind: String,
}

/// `BasketCardOut`.
#[derive(Debug, Clone, Serialize)]
struct BasketCardOut {
    slug: String,
    name: String,
    access: String,
    visibility: String,
    #[serde(rename = "type")]
    kind: String,
    categories: Vec<String>,
    rebalance_frequency: String,
    source: String,
    description_md: Option<String>,
    #[serde(serialize_with = "wire::ser::opt_date")]
    launched_at: Option<NaiveDate>,
    manager: ManagerBriefOut,
    metrics: Option<MetricsOut>,
}

/// `BasketListOut`.
#[derive(Debug, Serialize)]
struct BasketListOut {
    items: Vec<BasketCardOut>,
    total: i64,
    categories: Vec<String>,
}

/// `ManagerOut`.
#[derive(Debug, Serialize)]
struct ManagerOut {
    slug: String,
    name: String,
    kind: String,
    sebi_reg_no: Option<String>,
    bio: Option<String>,
    strategies: Vec<String>,
    disclosures_md: Option<String>,
}

/// `ManagerListOut`.
#[derive(Debug, Serialize)]
struct ManagerListOut {
    items: Vec<ManagerOut>,
}

/// `CollectionOut`.
#[derive(Debug, Serialize)]
struct CollectionOut {
    slug: String,
    title: String,
    subtitle: Option<String>,
    basket_slugs: Vec<String>,
    baskets: Vec<BasketCardOut>,
    position: i64,
    withheld: i64,
}

/// `CollectionListOut`.
#[derive(Debug, Serialize)]
struct CollectionListOut {
    items: Vec<CollectionOut>,
}

/// `ConstituentOut`.
#[derive(Debug, Serialize)]
struct ConstituentOut {
    symbol: String,
    name: Option<String>,
    segment: String,
    #[serde(serialize_with = "wire::ser::decimal")]
    weight: Decimal,
    #[serde(serialize_with = "wire::ser::decimal")]
    weight_pct: Decimal,
}

/// `ConstituentsOut`.
#[derive(Debug, Serialize)]
struct ConstituentsOut {
    slug: String,
    version_no: i64,
    #[serde(serialize_with = "wire::ser::opt_date")]
    effective_date: Option<NaiveDate>,
    label: String,
    added_count: i64,
    removed_count: i64,
    version_count: i64,
    constituents: Vec<ConstituentOut>,
}

/// `PerformancePointOut`.
#[derive(Debug, Serialize)]
struct PerformancePointOut {
    #[serde(serialize_with = "wire::ser::date")]
    date: NaiveDate,
    basket: String,
    benchmark: Option<String>,
}

/// `PerformanceOut` (decimals pre-rendered with `str(Decimal)`).
#[derive(Debug, Serialize)]
struct PerformanceOut {
    slug: String,
    points: Vec<PerformancePointOut>,
    coverage: String,
}

/// `WatchlistItemOut`.
#[derive(Debug, Serialize)]
struct WatchlistItemOut {
    basket_slug: String,
    basket_name: String,
    #[serde(serialize_with = "wire::ser::datetime")]
    watched_at: DateTime<Utc>,
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    nav_at_watch: Option<Decimal>,
    #[serde(serialize_with = "wire::ser::opt_decimal")]
    moved_pct: Option<Decimal>,
}

/// `WatchlistOut`.
#[derive(Debug, Serialize)]
struct WatchlistOut {
    items: Vec<WatchlistItemOut>,
    count: i64,
}

// ---------------------------------------------------------------------------------------------
// Reading rows
// ---------------------------------------------------------------------------------------------

fn dec(row: &PgRow, column: &str) -> Result<Option<Decimal>, Problem> {
    baskfy_db::row::opt_decimal(row, column).map_err(|e| r::dbe(&e))
}

/// `_metrics_out(row, launched_at)` from the card query's `x.*` columns.
fn metrics_out(row: &PgRow, launched_at: Option<NaiveDate>) -> Result<Option<MetricsOut>, Problem> {
    let present: Option<i64> = row.try_get("x_basket_id").map_err(r::db)?;
    if present.is_none() {
        return Ok(None);
    }
    let as_of_date = baskfy_db::row::opt_date(row, "as_of_date").map_err(|e| r::dbe(&e))?;
    let months = match (launched_at, as_of_date) {
        (Some(l), Some(a)) => curated::whole_months_between(l, a),
        _ => 0,
    };
    let (ret_1m, ret_6m, ret_1y) = (
        dec(row, "ret_1m")?,
        dec(row, "ret_6m")?,
        dec(row, "ret_1y")?,
    );
    let (cagr_3y, cagr_5y) = (dec(row, "cagr_3y")?, dec(row, "cagr_5y")?);
    let since = dec(row, "since_inception_pct")?;
    let (label, pct) =
        curated::headline_return(months, ret_1m, ret_6m, ret_1y, cagr_3y, cagr_5y, since);
    Ok(Some(MetricsOut {
        as_of_date,
        min_amount: dec(row, "min_amount")?,
        volatility_bucket: row.try_get("volatility_bucket").map_err(r::db)?,
        volatility_value: dec(row, "volatility_value")?,
        ret_1m,
        ret_6m,
        ret_1y,
        cagr_3y,
        cagr_5y,
        since_inception_pct: since,
        headline_label: Some(label),
        headline_pct: pct,
        return_convention: curated::RETURN_CONVENTION,
        dividends_included: curated::DIVIDENDS_INCLUDED,
        return_convention_note: curated::RETURN_CONVENTION_NOTE
            .replace("(docs/DECISIONS-MERGE.md M39.3), ", ""),
    }))
}

/// `_card(basket, manager, metrics)` from one card-query row.
fn card(row: &PgRow) -> Result<BasketCardOut, Problem> {
    let launched_at = baskfy_db::row::opt_date(row, "launched_at").map_err(|e| r::dbe(&e))?;
    let categories: Option<Vec<String>> = row.try_get("categories").map_err(r::db)?;
    Ok(BasketCardOut {
        slug: row.try_get("slug").map_err(r::db)?,
        name: row.try_get("name").map_err(r::db)?,
        access: row.try_get("access").map_err(r::db)?,
        visibility: row.try_get("visibility").map_err(r::db)?,
        kind: row.try_get("type").map_err(r::db)?,
        categories: categories.unwrap_or_default(),
        rebalance_frequency: row.try_get("rebalance_frequency").map_err(r::db)?,
        source: row.try_get("source").map_err(r::db)?,
        description_md: row.try_get("description_md").map_err(r::db)?,
        launched_at,
        manager: ManagerBriefOut {
            slug: row.try_get("m_slug").map_err(r::db)?,
            name: row.try_get("m_name").map_err(r::db)?,
            kind: row.try_get("m_kind").map_err(r::db)?,
        },
        metrics: metrics_out(row, launched_at)?,
    })
}

fn manager_out(row: &PgRow) -> Result<ManagerOut, Problem> {
    let strategies: Option<Vec<String>> = row.try_get("strategies").map_err(r::db)?;
    Ok(ManagerOut {
        slug: row.try_get("slug").map_err(r::db)?,
        name: row.try_get("name").map_err(r::db)?,
        kind: row.try_get("kind").map_err(r::db)?,
        sebi_reg_no: row.try_get("sebi_reg_no").map_err(r::db)?,
        bio: row.try_get("bio").map_err(r::db)?,
        strategies: strategies.unwrap_or_default(),
        disclosures_md: row.try_get("disclosures_md").map_err(r::db)?,
    })
}

/// The authenticated principal's user id (`AuthenticatedDep` then `require_user()`).
fn user(principal: &Principal) -> Result<i64, Problem> {
    require_authenticated(principal)?;
    principal.require_user()
}

/// A slug path parameter's `max_length`, as FastAPI validates it before anything else.
fn check_slug(slug: &str) -> Result<(), Problem> {
    let mut v = Validation::new();
    path_max_length(&mut v, "slug", slug, SLUG_MAX_LENGTH);
    v.finish()
}

/// `select(CbBasket).where(CbBasket.slug == slug, *_visible())`: the id, or 404 `basket`.
async fn visible_basket_id(state: &AppState, slug: &str) -> Result<i64, Problem> {
    let ids: Vec<i64> = sqlx::query_scalar(&format!(
        "SELECT b.id FROM cb_basket b WHERE b.slug = $1 AND {VISIBLE}"
    ))
    .bind(slug)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    if ids.len() > 1 {
        return Err(r::unexpected("MultipleResultsFound: cb_basket"));
    }
    ids.first()
        .copied()
        .ok_or_else(|| not_found("basket", slug))
}

// ---------------------------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------------------------

/// asyncpg's binary `numeric` encoder refuses a `Decimal` whose exponent is below this
/// (`DataError: cannot encode Decimal`), which the route answers with a 500.
const ASYNCPG_MIN_EXPONENT: i64 = -16_383;

/// `CbMetrics.min_amount <= max_min_amount` binds the value as SQLAlchemy's asyncpg dialect
/// renders it, `$n::NUMERIC(20, 2)` (the model's type): Postgres rounds it to two places half
/// away from zero, and a value of 10**18 or more is `numeric field overflow`, a 500. The text
/// bound here is the exact value, so Postgres does the same rounding; the two failures that never
/// reach Postgres in Python's path, or would make an absurd text here, are answered directly.
fn max_min_amount_bind(value: &PyDec) -> Result<String, Problem> {
    let wide = value
        .wide()
        .ok_or_else(|| r::unexpected("max_min_amount is not a finite decimal"))?;
    if wide.exponent() < ASYNCPG_MIN_EXPONENT {
        return Err(r::unexpected("asyncpg: cannot encode Decimal"));
    }
    if wide.is_zero() {
        return Ok("0".to_owned());
    }
    // A coefficient is at most as long as the query string that spelled it, so `exponent() > 18`
    // (or a positive exponent whose `str()` already puts the leading digit at 10**18) is an
    // overflow whatever the digits; anything else is short enough to spell out.
    let sci = wide.to_sci_string();
    let adjusted = sci
        .rsplit_once('E')
        .and_then(|(_, e)| e.parse::<i64>().ok());
    if wide.exponent() > 18 || adjusted.is_some_and(|a| a >= 18) {
        return Err(r::unexpected("numeric field overflow"));
    }
    Ok(wide.to_string())
}

/// `GET /explore`: the catalogue grid (docs/smallcase/05 filters and sorts).
///
/// # Errors
/// 401 for an anonymous caller, 400 for a bad parameter, 500 for a database failure.
pub async fn list_explore_baskets(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let mut p = Params::new(raw.as_deref());
    let max_min_amount = p.decimal("max_min_amount", false);
    let access = p.pattern("access", "^(FREE|FEE)$", &["FREE", "FEE"]);
    let volatility = p.pattern("volatility", "^(LOW|MED|HIGH)$", &["LOW", "MED", "HIGH"]);
    let category = p.string("category", None, Some(60));
    let rebalance = p.pattern(
        "rebalance_frequency",
        "^(WEEKLY|MONTHLY|QUARTERLY|ANNUAL|NEED_BASIS)$",
        &["WEEKLY", "MONTHLY", "QUARTERLY", "ANNUAL", "NEED_BASIS"],
    );
    let basket_type = p.pattern("basket_type", "^(STOCK|MF|US)$", &["STOCK", "MF", "US"]);
    let include_new = p.boolean("include_new", true);
    let sort = p.literal("sort", &SORT_FIELDS, "name");
    let order = p.literal("order", &["asc", "desc"], "asc");
    let q = p.string("q", None, Some(80));
    let limit = p.int("limit", None, Some(1), Some(200));
    let offset = p.int("offset", Some(0), Some(0), None);
    p.finish()?;
    principal.require_user()?;

    let mut sql = format!("{CARD_SQL} WHERE {VISIBLE}");
    let mut texts: Vec<String> = Vec::new();
    let mut bind = |sql: &mut String, fragment: &str, value: String| {
        texts.push(value);
        sql.push_str(&fragment.replace("$?", &format!("${}", texts.len())));
    };
    if let Some(value) = max_min_amount.ok().flatten() {
        bind(
            &mut sql,
            " AND x.min_amount IS NOT NULL AND x.min_amount <= $?::numeric(20, 2)",
            max_min_amount_bind(&value)?,
        );
    }
    if let Some(access) = access.ok().flatten() {
        bind(&mut sql, " AND b.access = $?", access);
    }
    if let Some(volatility) = volatility.ok().flatten() {
        bind(&mut sql, " AND x.volatility_bucket = $?", volatility);
    }
    if let Some(category) = category.ok().flatten() {
        bind(&mut sql, " AND $?::varchar = ANY(b.categories)", category);
    }
    if let Some(rebalance) = rebalance.ok().flatten() {
        bind(&mut sql, " AND b.rebalance_frequency = $?", rebalance);
    }
    if let Some(basket_type) = basket_type.ok().flatten() {
        bind(&mut sql, " AND b.type = $?", basket_type);
    }
    if include_new == Some(false) {
        let cutoff = r::today_ist() - Duration::days(30);
        bind(
            &mut sql,
            " AND (b.launched_at IS NULL OR b.launched_at < $?::date)",
            wire::py_date(cutoff),
        );
    }
    if let Some(q) = q.ok().flatten().filter(|q| !q.is_empty()) {
        bind(
            &mut sql,
            " AND (b.name ILIKE $? OR b.slug ILIKE $?)",
            format!("%{}%", r::py_strip(&q)),
        );
    }
    let column = match sort.as_deref().unwrap_or("name") {
        "min_amount" => "x.min_amount",
        "ret_1y" => "x.ret_1y",
        "cagr_3y" => "x.cagr_3y",
        "cagr_5y" => "x.cagr_5y",
        "launched_at" => "b.launched_at",
        "volatility" => "x.volatility_value",
        _ => "b.name",
    };
    let direction = if order.as_deref() == Some("desc") {
        "DESC"
    } else {
        "ASC"
    };
    sql.push_str(&format!(
        " ORDER BY {column} {direction} NULLS LAST, b.id ASC"
    ));
    let mut query = sqlx::query(&sql);
    for t in &texts {
        query = query.bind(t.as_str());
    }
    let rows = query.fetch_all(state.db.pool()).await.map_err(r::db)?;
    let mut items: Vec<BasketCardOut> = rows.iter().map(card).collect::<Result<_, _>>()?;
    let mut categories: Vec<String> = items
        .iter()
        .flat_map(|c| c.categories.iter().cloned())
        .collect();
    categories.sort();
    categories.dedup();
    let total = i64::try_from(items.len()).unwrap_or(i64::MAX);
    let offset = usize::try_from(offset.flatten().unwrap_or(0)).unwrap_or(usize::MAX);
    if offset > 0 {
        items = items.into_iter().skip(offset).collect();
    }
    if let Some(limit) = limit.flatten() {
        items.truncate(usize::try_from(limit).unwrap_or(usize::MAX));
    }
    Ok(wire::json(&BasketListOut {
        items,
        total,
        categories,
    }))
}

const MANAGER_SQL: &str =
    "SELECT slug, name, kind, sebi_reg_no, bio, strategies, disclosures_md FROM cb_manager";

/// `GET /explore/managers`: every manager, by slug.
///
/// # Errors
/// 401 for an anonymous caller, 500 for a database failure.
pub async fn list_managers(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    user(&principal)?;
    let rows = sqlx::query(&format!("{MANAGER_SQL} ORDER BY slug"))
        .fetch_all(state.db.pool())
        .await
        .map_err(r::db)?;
    let items = rows.iter().map(manager_out).collect::<Result<_, _>>()?;
    Ok(wire::json(&ManagerListOut { items }))
}

/// `GET /explore/managers/{slug}`.
///
/// # Errors
/// 401 for an anonymous caller, 400 for an over-long slug, 404 for an unknown one, 500 for a
/// database failure.
pub async fn get_manager(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(slug): Path<String>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    check_slug(&slug)?;
    principal.require_user()?;
    let rows = sqlx::query(&format!("{MANAGER_SQL} WHERE slug = $1"))
        .bind(&slug)
        .fetch_all(state.db.pool())
        .await
        .map_err(r::db)?;
    if rows.len() > 1 {
        return Err(r::unexpected("MultipleResultsFound: cb_manager"));
    }
    let row = rows.first().ok_or_else(|| not_found("manager", &slug))?;
    Ok(wire::json(&manager_out(row)?))
}

/// `_collection_out(session, row)`: a shelf expanded into cards, in the shelf's own order.
async fn collection_out(state: &AppState, row: &PgRow) -> Result<CollectionOut, Problem> {
    let stored: Option<Vec<i64>> = row.try_get("basket_ids").map_err(r::db)?;
    let mut wanted: Vec<i64> = Vec::new();
    for id in stored.unwrap_or_default() {
        if !wanted.contains(&id) {
            wanted.push(id);
        }
    }
    let mut cards: HashMap<i64, BasketCardOut> = HashMap::new();
    if !wanted.is_empty() {
        let rows = sqlx::query(&format!("{CARD_SQL} WHERE b.id = ANY($1) AND {VISIBLE}"))
            .bind(&wanted)
            .fetch_all(state.db.pool())
            .await
            .map_err(r::db)?;
        for r in &rows {
            let id: i64 = r.try_get("id").map_err(super::meta::reference::db)?;
            cards.insert(id, card(r)?);
        }
    }
    let ordered: Vec<BasketCardOut> = wanted
        .iter()
        .filter_map(|i| cards.get(i).cloned())
        .collect();
    Ok(CollectionOut {
        slug: row.try_get("slug").map_err(r::db)?,
        title: row.try_get("title").map_err(r::db)?,
        subtitle: row.try_get("subtitle").map_err(r::db)?,
        basket_slugs: ordered.iter().map(|c| c.slug.clone()).collect(),
        withheld: i64::try_from(wanted.len() - ordered.len()).unwrap_or(0),
        baskets: ordered,
        position: i64::from(row.try_get::<i16, _>("position").map_err(r::db)?),
    })
}

/// `GET /explore/collections`: every shelf, by position then slug.
///
/// # Errors
/// 401 for an anonymous caller, 500 for a database failure.
pub async fn list_collections(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    user(&principal)?;
    let rows = sqlx::query(
        "SELECT slug, title, subtitle, basket_ids, position FROM cb_collection \
         ORDER BY position, slug",
    )
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    let mut items = Vec::with_capacity(rows.len());
    for row in &rows {
        items.push(collection_out(&state, row).await?);
    }
    Ok(wire::json(&CollectionListOut { items }))
}

/// `GET /explore/collections/{slug}`.
///
/// # Errors
/// 401 for an anonymous caller, 400 for an over-long slug, 404 for an unknown one, 500 for a
/// database failure.
pub async fn get_collection(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(slug): Path<String>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    check_slug(&slug)?;
    principal.require_user()?;
    let rows = sqlx::query(
        "SELECT slug, title, subtitle, basket_ids, position FROM cb_collection WHERE slug = $1",
    )
    .bind(&slug)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    if rows.len() > 1 {
        return Err(r::unexpected("MultipleResultsFound: cb_collection"));
    }
    let row = rows.first().ok_or_else(|| not_found("collection", &slug))?;
    Ok(wire::json(&collection_out(&state, row).await?))
}

/// `GET /explore/{slug}`: one card, with its newest metrics row.
///
/// # Errors
/// 401 for an anonymous caller, 400 for an over-long slug, 404 for an unknown or hidden basket,
/// 500 for a database failure.
pub async fn get_explore_basket(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(slug): Path<String>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    check_slug(&slug)?;
    principal.require_user()?;
    // The newest metrics row for this basket, by `as_of_date` (one statement for the card).
    let rows = sqlx::query(&format!(
        "SELECT b.id, b.slug, b.name, b.access, b.visibility, b.type, b.categories, \
         b.rebalance_frequency, b.source, b.description_md, b.launched_at, \
         m.slug AS m_slug, m.name AS m_name, m.kind AS m_kind, \
         x.basket_id AS x_basket_id, x.as_of_date, x.min_amount, x.volatility_bucket, \
         x.volatility_value, x.ret_1m, x.ret_6m, x.ret_1y, x.cagr_3y, x.cagr_5y, \
         x.since_inception_pct \
         FROM cb_basket b JOIN cb_manager m ON m.id = b.manager_id \
         LEFT JOIN LATERAL (SELECT * FROM cb_metrics WHERE basket_id = b.id \
           ORDER BY as_of_date DESC LIMIT 1) x ON true \
         WHERE b.slug = $1 AND {VISIBLE}"
    ))
    .bind(&slug)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    if rows.len() > 1 {
        return Err(r::unexpected("MultipleResultsFound: cb_basket"));
    }
    let row = rows.first().ok_or_else(|| not_found("basket", &slug))?;
    Ok(wire::json(&card(row)?))
}

/// `GET /explore/{slug}/constituents`: the newest published version and its names.
///
/// # Errors
/// 401 for an anonymous caller, 400 for an over-long slug, 404 for an unknown or hidden basket,
/// 500 for a database failure.
pub async fn get_explore_constituents(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(slug): Path<String>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    check_slug(&slug)?;
    principal.require_user()?;
    let basket_id = visible_basket_id(&state, &slug).await?;
    let version = sqlx::query(
        "SELECT id, version_no, effective_date, label, added_count, removed_count \
         FROM cb_basket_version WHERE basket_id = $1 ORDER BY version_no DESC LIMIT 1",
    )
    .bind(basket_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(r::db)?;
    let version_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM cb_basket_version WHERE basket_id = $1")
            .bind(basket_id)
            .fetch_one(state.db.pool())
            .await
            .map_err(r::db)?;
    let Some(version) = version else {
        return Ok(wire::json(&ConstituentsOut {
            slug,
            version_no: 0,
            effective_date: None,
            label: "GENESIS".to_owned(),
            added_count: 0,
            removed_count: 0,
            version_count: 0,
            constituents: Vec::new(),
        }));
    };
    let version_id: i64 = version.try_get("id").map_err(r::db)?;
    let rows = sqlx::query(
        "SELECT i.symbol, i.name, c.segment, c.weight FROM cb_constituent c \
         JOIN instrument i ON i.id = c.instrument_id WHERE c.version_id = $1 \
         ORDER BY c.weight DESC, i.symbol ASC",
    )
    .bind(version_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    let mut constituents = Vec::with_capacity(rows.len());
    for row in &rows {
        let weight = baskfy_db::row::decimal(row, "weight").map_err(|e| r::dbe(&e))?;
        let pct = WideDecimal::from(weight)
            .mul(&WideDecimal::from(Decimal::ONE_HUNDRED))
            .quantize(-2, Rounding::HalfEven)
            .map_err(|e| r::unexpected(&e.to_string()))?;
        constituents.push(ConstituentOut {
            symbol: row.try_get("symbol").map_err(r::db)?,
            name: row.try_get("name").map_err(r::db)?,
            segment: row.try_get("segment").map_err(r::db)?,
            weight,
            weight_pct: pct
                .to_decimal()
                .ok_or_else(|| r::unexpected("weight_pct beyond Decimal"))?,
        });
    }
    Ok(wire::json(&ConstituentsOut {
        slug,
        version_no: i64::from(version.try_get::<i32, _>("version_no").map_err(r::db)?),
        effective_date: Some(
            baskfy_db::row::date(&version, "effective_date").map_err(|e| r::dbe(&e))?,
        ),
        label: version.try_get("label").map_err(r::db)?,
        added_count: i64::from(version.try_get::<i32, _>("added_count").map_err(r::db)?),
        removed_count: i64::from(version.try_get::<i32, _>("removed_count").map_err(r::db)?),
        version_count,
        constituents,
    }))
}

fn empty_performance(slug: String) -> Response {
    wire::json(&PerformanceOut {
        slug,
        points: Vec::new(),
        coverage: "0.00".to_owned(),
    })
}

/// `GET /explore/{slug}/performance`: the version-aware NAV over fully covered days.
///
/// # Errors
/// 401 for an anonymous caller, 400 for an over-long slug, 404 for an unknown or hidden basket,
/// 500 for a database failure.
pub async fn get_explore_performance(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(slug): Path<String>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    check_slug(&slug)?;
    principal.require_user()?;
    let basket_id = visible_basket_id(&state, &slug).await?;
    let versions = sqlx::query(
        "SELECT id, effective_date FROM cb_basket_version WHERE basket_id = $1 \
         ORDER BY version_no ASC",
    )
    .bind(basket_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    if versions.is_empty() {
        return Ok(empty_performance(slug));
    }
    let mut version_rows: Vec<(i64, NaiveDate)> = Vec::new();
    for v in &versions {
        version_rows.push((
            v.try_get("id").map_err(r::db)?,
            baskfy_db::row::date(v, "effective_date").map_err(|e| r::dbe(&e))?,
        ));
    }
    let ids: Vec<i64> = version_rows.iter().map(|v| v.0).collect();
    let constituents = sqlx::query(
        "SELECT c.version_id, c.instrument_id, c.weight, i.symbol FROM cb_constituent c \
         JOIN instrument i ON i.id = c.instrument_id WHERE c.version_id = ANY($1)",
    )
    .bind(&ids)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    let mut by_version: HashMap<i64, Vec<(String, Decimal)>> = HashMap::new();
    let mut instrument_ids: Vec<i64> = Vec::new();
    for c in &constituents {
        let version_id: i64 = c.try_get("version_id").map_err(r::db)?;
        let symbol: String = c.try_get("symbol").map_err(r::db)?;
        let weight = baskfy_db::row::decimal(c, "weight").map_err(|e| r::dbe(&e))?;
        let weights = by_version.entry(version_id).or_default();
        match weights.iter_mut().find(|(s, _)| *s == symbol) {
            Some(slot) => slot.1 = weight,
            None => weights.push((symbol, weight)),
        }
        let iid: i64 = c.try_get("instrument_id").map_err(r::db)?;
        if !instrument_ids.contains(&iid) {
            instrument_ids.push(iid);
        }
    }
    let history_versions: Vec<BasketVersion> = version_rows
        .iter()
        .filter_map(|(id, effective)| {
            by_version
                .get(id)
                .filter(|w| !w.is_empty())
                .map(|w| BasketVersion {
                    effective_date: *effective,
                    weights: w.clone(),
                })
        })
        .collect();
    let Some(first) = history_versions.first() else {
        return Ok(empty_performance(slug));
    };
    if instrument_ids.is_empty() {
        return Ok(empty_performance(slug));
    }
    let start = first.effective_date;
    let as_of: Option<NaiveDate> = sqlx::query_scalar("SELECT max(date) FROM ohlcv_daily")
        .fetch_one(state.db.pool())
        .await
        .map_err(r::db)?;
    let Some(as_of) = as_of.filter(|a| *a >= start) else {
        return Ok(empty_performance(slug));
    };
    let price_rows = sqlx::query(
        "SELECT o.date, i.symbol, o.close FROM ohlcv_daily o \
         JOIN instrument i ON i.id = o.instrument_id \
         WHERE o.instrument_id = ANY($1) AND o.date >= $2 AND o.date <= $3 ORDER BY o.date",
    )
    .bind(&instrument_ids)
    .bind(start)
    .bind(as_of)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    let mut history: BTreeMap<NaiveDate, BTreeMap<String, Decimal>> = BTreeMap::new();
    for row in &price_rows {
        let day = baskfy_db::row::date(row, "date").map_err(|e| r::dbe(&e))?;
        let symbol: String = row.try_get("symbol").map_err(r::db)?;
        let close = baskfy_db::row::decimal(row, "close").map_err(|e| r::dbe(&e))?;
        history.entry(day).or_default().insert(symbol, close);
    }
    let mut ordered: Vec<&BasketVersion> = history_versions.iter().collect();
    ordered.sort_by_key(|v| v.effective_date);
    let effective: Vec<NaiveDate> = ordered.iter().map(|v| v.effective_date).collect();
    let mut covered: BTreeMap<NaiveDate, BTreeMap<String, Decimal>> = BTreeMap::new();
    let (mut trading_days, mut covered_days) = (0_i64, 0_i64);
    for (day, prices) in &history {
        if *day < start {
            continue;
        }
        trading_days += 1;
        let weights = &ordered[curated::version_index(&effective, *day)].weights;
        let full = weights
            .iter()
            .all(|(s, _)| prices.get(s).is_some_and(|p| *p > Decimal::ZERO));
        if full {
            covered.insert(*day, prices.clone());
            covered_days += 1;
        }
    }
    let nav =
        curated::version_aware_nav(&covered, &history_versions).map_err(|e| r::unexpected(&e))?;
    let coverage = if trading_days > 0 {
        WideDecimal::from(Decimal::from(covered_days))
            .div(&WideDecimal::from(Decimal::from(trading_days)))
            .and_then(|x| x.quantize(-4, Rounding::HalfEven))
            .map_err(|e| r::unexpected(&e.to_string()))?
            .to_sci_string()
    } else {
        "0.00".to_owned()
    };
    let mut points = Vec::with_capacity(nav.len());
    for (day, value) in &nav {
        points.push(PerformancePointOut {
            date: *day,
            basket: curated::nav_for_storage(value)
                .map_err(|e| r::unexpected(&e))?
                .to_sci_string(),
            benchmark: None,
        });
    }
    Ok(wire::json(&PerformanceOut {
        slug,
        points,
        coverage,
    }))
}

/// `GET /watchlist`: the sole tenant's watched baskets, newest first.
///
/// # Errors
/// 401 for an anonymous caller, 404 for a caller who is not the sole tenant, 503 when no sole
/// tenant is configured, 500 for a database failure.
pub async fn list_watchlist(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let user_id = r::scoped_sole_user_id(&state, principal.user_id, None).await?;
    let rows = sqlx::query(&format!(
        "SELECT b.slug, b.name, w.watched_at, w.nav_at_watch FROM cb_watchlist_item w \
         JOIN cb_basket b ON b.id = w.basket_id WHERE w.user_id = $1 AND {VISIBLE} \
         ORDER BY w.watched_at DESC"
    ))
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    let mut items = Vec::with_capacity(rows.len());
    for row in &rows {
        items.push(WatchlistItemOut {
            basket_slug: row.try_get("slug").map_err(r::db)?,
            basket_name: row.try_get("name").map_err(r::db)?,
            watched_at: baskfy_db::row::timestamptz(row, "watched_at").map_err(|e| r::dbe(&e))?,
            nav_at_watch: dec(row, "nav_at_watch")?,
            moved_pct: None,
        });
    }
    let count = i64::try_from(items.len()).unwrap_or(i64::MAX);
    Ok(wire::json(&WatchlistOut { items, count }))
}

/// The GET routes of `baskfy_api.routers.explore`, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/explore", get(list_explore_baskets))
        .route("/explore/managers", get(list_managers))
        .route("/explore/managers/{slug}", get(get_manager))
        .route("/explore/collections", get(list_collections))
        .route("/explore/collections/{slug}", get(get_collection))
        .route("/explore/{slug}", get(get_explore_basket))
        .route(
            "/explore/{slug}/constituents",
            get(get_explore_constituents),
        )
        .route("/explore/{slug}/performance", get(get_explore_performance))
        .route("/watchlist", get(list_watchlist))
}
