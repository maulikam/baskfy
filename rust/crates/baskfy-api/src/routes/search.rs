//! `GET /search` — the ⌘K palette's one call.
//!
//! Ports `baskfy_api.routers.search` and the service `baskfy_api.search`: stocks, indices,
//! baskets and screens in one round trip, at most `limit` of each, each ranked exact → prefix →
//! contains with a stable tie-break. Visibility is inherited from each kind's own list route:
//! instruments and indices are public, baskets need a signed-in caller and a published,
//! unarchived basket, screens are the templates plus the caller's own.
//!
//! `Cache-Control: no-store` — the answer depends on the caller.

use axum::Extension;
use axum::Router;
use axum::extract::{RawQuery, State};
use axum::http::{HeaderValue, header};
use axum::response::Response;
use axum::routing::get;
use serde::Serialize;
use sqlx::Row;

use super::meta::reference::factsheet::search_instruments as search_instrument_rows;
use super::meta::reference::{self as r, Params};
use crate::auth::Principal;
use crate::problems::Problem;
use crate::state::AppState;
use crate::wire;

/// `DEFAULT_LIMIT`.
pub const DEFAULT_LIMIT: i64 = 5;
/// `MAX_LIMIT`.
pub const MAX_LIMIT: i64 = 20;
/// `MIN_QUERY_LENGTH`.
pub const MIN_QUERY_LENGTH: usize = 1;
/// `MAX_QUERY_LENGTH`.
pub const MAX_QUERY_LENGTH: usize = 64;
/// `NO_STORE`.
pub const NO_STORE: &str = "no-store";

/// `CatalogHitOut`.
#[derive(Debug, Clone, Serialize)]
pub struct CatalogHitOut {
    /// `kind`: `instrument` | `index` | `basket` | `screen`.
    pub kind: &'static str,
    /// `id`: symbol, index slug, basket slug or screen `public_id`.
    pub id: String,
    /// `title`.
    pub title: String,
    /// `subtitle`.
    pub subtitle: Option<String>,
}

/// `CatalogSearchOut`.
#[derive(Debug, Clone, Serialize)]
pub struct CatalogSearchOut {
    /// `query` (normalised).
    pub query: String,
    /// `limit`.
    pub limit: i64,
    /// `data`.
    pub data: Vec<CatalogHitOut>,
}

/// `normalise(query)`: stripped, at most `MAX_QUERY_LENGTH` characters.
fn normalise(query: &str) -> String {
    r::py_strip(query).chars().take(MAX_QUERY_LENGTH).collect()
}

/// `_rank(column, needle)` as SQL over `$1` (the needle) and `$2` (`needle%`).
fn rank_sql(column: &str) -> String {
    format!("CASE WHEN {column} = $1 THEN 0 WHEN {column} ILIKE $2 THEN 1 ELSE 2 END")
}

async fn search_indices(
    state: &AppState,
    needle: &str,
    limit: i64,
) -> Result<Vec<CatalogHitOut>, Problem> {
    if needle.is_empty() {
        return Ok(Vec::new());
    }
    let sql = format!(
        "SELECT slug, name, is_universe FROM index_def WHERE name ILIKE $3 OR slug ILIKE $3 \
         ORDER BY {} ASC, is_universe DESC, sort_order ASC, name ASC LIMIT $4",
        rank_sql("name")
    );
    let rows = sqlx::query(&sql)
        .bind(needle)
        .bind(format!("{needle}%"))
        .bind(format!("%{needle}%"))
        .bind(limit)
        .fetch_all(state.db.pool())
        .await
        .map_err(r::db)?;
    rows.iter()
        .map(|row| {
            let is_universe: bool = row.try_get("is_universe").map_err(r::db)?;
            Ok(CatalogHitOut {
                kind: "index",
                id: row.try_get("slug").map_err(r::db)?,
                title: row.try_get("name").map_err(r::db)?,
                subtitle: Some(if is_universe { "Universe" } else { "Index" }.to_owned()),
            })
        })
        .collect()
}

async fn search_baskets(
    state: &AppState,
    needle: &str,
    limit: i64,
    principal: &Principal,
) -> Result<Vec<CatalogHitOut>, Problem> {
    if needle.is_empty() || principal.user_id.is_none() {
        return Ok(Vec::new());
    }
    let sql = format!(
        "SELECT b.slug, b.name, m.name AS manager_name FROM cb_basket b \
         JOIN cb_manager m ON m.id = b.manager_id \
         WHERE b.archived_at IS NULL AND b.visibility = 'PUBLISHED' \
           AND (b.name ILIKE $3 OR b.slug ILIKE $3) \
         ORDER BY {} ASC, b.name ASC LIMIT $4",
        rank_sql("b.name")
    );
    let rows = sqlx::query(&sql)
        .bind(needle)
        .bind(format!("{needle}%"))
        .bind(format!("%{needle}%"))
        .bind(limit)
        .fetch_all(state.db.pool())
        .await
        .map_err(r::db)?;
    rows.iter()
        .map(|row| {
            Ok(CatalogHitOut {
                kind: "basket",
                id: row.try_get("slug").map_err(r::db)?,
                title: row.try_get("name").map_err(r::db)?,
                subtitle: Some(row.try_get("manager_name").map_err(r::db)?),
            })
        })
        .collect()
}

async fn search_screens(
    state: &AppState,
    needle: &str,
    limit: i64,
    principal: &Principal,
) -> Result<Vec<CatalogHitOut>, Problem> {
    if needle.is_empty() {
        return Ok(Vec::new());
    }
    let visible = if principal.user_id.is_some() {
        "(user_id IS NULL OR user_id = $5)"
    } else {
        "user_id IS NULL"
    };
    let sql = format!(
        "SELECT public_id, name, is_example FROM screen WHERE {visible} AND name ILIKE $3 \
         ORDER BY {} ASC, is_example DESC, name ASC LIMIT $4",
        rank_sql("name")
    );
    let mut query = sqlx::query(&sql)
        .bind(needle)
        .bind(format!("{needle}%"))
        .bind(format!("%{needle}%"))
        .bind(limit);
    if let Some(uid) = principal.user_id {
        query = query.bind(uid);
    }
    let rows = query.fetch_all(state.db.pool()).await.map_err(r::db)?;
    rows.iter()
        .map(|row| {
            let is_example: bool = row.try_get("is_example").map_err(r::db)?;
            Ok(CatalogHitOut {
                kind: "screen",
                id: row.try_get("public_id").map_err(r::db)?,
                title: row.try_get("name").map_err(r::db)?,
                subtitle: is_example.then(|| "Example".to_owned()),
            })
        })
        .collect()
}

/// `GET /search?q=&limit=`: every kind the caller may see, in `KIND_ORDER`.
///
/// # Errors
/// 400 for a missing or bad parameter, 500 for a database failure.
pub async fn catalog_search(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let mut p = Params::new(raw.as_deref());
    let q = p.required_string("q", Some(MIN_QUERY_LENGTH), Some(MAX_QUERY_LENGTH));
    let limit = p.int("limit", Some(DEFAULT_LIMIT), Some(1), Some(MAX_LIMIT));
    p.finish()?;
    let limit = limit.flatten().unwrap_or(DEFAULT_LIMIT);
    let needle = normalise(q.as_deref().unwrap_or(""));
    let mut data = Vec::new();
    if needle.chars().count() >= MIN_QUERY_LENGTH {
        for hit in search_instrument_rows(&state, &needle, limit).await? {
            data.push(CatalogHitOut {
                kind: "instrument",
                id: hit.symbol.clone(),
                title: hit.symbol,
                subtitle: Some(hit.name),
            });
        }
        data.extend(search_indices(&state, &needle, limit).await?);
        data.extend(search_baskets(&state, &needle, limit, &principal).await?);
        data.extend(search_screens(&state, &needle, limit, &principal).await?);
    }
    let mut response = wire::json(&CatalogSearchOut {
        query: needle,
        limit,
        data,
    });
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static(NO_STORE));
    Ok(response)
}

/// The routes of `baskfy_api.routers.search`, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new().route("/search", get(catalog_search))
}
