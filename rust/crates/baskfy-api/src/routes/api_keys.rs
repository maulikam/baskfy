//! ``/keys`` — the API-key list and its usage dashboard (Prompt 20 deliverable 1).
//!
//! Ports the **GET** routes of `baskfy_api.routers.api_keys` (`APIRouter(prefix="/keys",
//! tags=["api-keys"])`):
//!
//! * `GET /keys` — every key the account holds (revoked ones included) with 30 days of usage;
//! * `GET /keys/{public_id}/usage?days=` — one key's daily series.
//!
//! **A key's secret is never returned here**: `ApiKeyOut` carries the display prefix
//! (`dk_<prefix>`) and nothing else of the token; the hash column is never even selected. Every
//! route requires a signed-in account, never a key.
//!
//! The **writes** (`POST /keys`, `POST /keys/{public_id}/rotate`, `POST /keys/{public_id}/revoke`,
//! `DELETE /keys/{public_id}`) are in [`writes`] (wave 4c, lane writes-accounts); the request
//! models of the whole lane's writes are validated by [`model`]. A created or rotated key's secret
//! appears once, in that response, and is never logged. The public API itself stays shut (D9).
//! **No route may place, modify or cancel an order** (law 2).

pub(crate) mod model;
mod writes;

use std::collections::HashMap;

use axum::Router;
use axum::extract::{Extension, Path, RawQuery, State};
use axum::response::Response;
use axum::routing::{delete, get, post};
use baskfy_db::{OrderedJson, row};
use chrono::{DateTime, Duration, NaiveDate, Utc};
use serde::Serialize;
use sqlx::Row;
use sqlx::postgres::PgRow;

use super::admin::REVIEW_SIGNED_OFF;
use super::screens::accounts::{self, db, dbe};
use crate::auth::{Principal, require_authenticated};
use crate::entitlements::utc_now;
use crate::problems::{Problem, not_found};
use crate::query::{QueryParams, Validation};
use crate::state::AppState;
use crate::wire;

/// `USAGE_WINDOW_DAYS`.
pub const USAGE_WINDOW_DAYS: i64 = 30;
/// `MAX_USAGE_WINDOW_DAYS`.
pub const MAX_USAGE_WINDOW_DAYS: i64 = 366;
/// `baskfy_core.api_keys.KEY_TOKEN_MARKER`.
pub const KEY_TOKEN_MARKER: &str = "dk";

/// `ApiKeyOut` — one key as its owner sees it. **Never carries the secret.**
#[derive(Debug, Clone, Serialize)]
pub struct ApiKeyOut {
    /// `public_id`.
    pub public_id: String,
    /// `name`.
    pub name: String,
    /// `display`: `dk_<prefix>`.
    pub display: String,
    /// `scopes`.
    pub scopes: Vec<String>,
    /// `rate_limit_per_minute`.
    pub rate_limit_per_minute: Option<i64>,
    /// `created_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub created_at: DateTime<Utc>,
    /// `last_used_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub last_used_at: Option<DateTime<Utc>>,
    /// `expires_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub expires_at: Option<DateTime<Utc>>,
    /// `revoked_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub revoked_at: Option<DateTime<Utc>>,
    /// `revoked_reason`.
    pub revoked_reason: Option<String>,
    /// `requests_30d`.
    pub requests_30d: i64,
    /// `throttled_30d`.
    pub throttled_30d: i64,
    /// `active`.
    pub active: bool,
}

/// `ApiKeyListOut`.
#[derive(Debug, Clone, Serialize)]
pub struct ApiKeyListOut {
    /// `keys`.
    pub keys: Vec<ApiKeyOut>,
    /// `public_api_enabled`.
    pub public_api_enabled: bool,
    /// `data_redistribution_signed_off`.
    pub data_redistribution_signed_off: bool,
}

/// `ApiKeyUsagePointOut`.
#[derive(Debug, Clone, Serialize)]
pub struct ApiKeyUsagePointOut {
    /// `date`.
    #[serde(serialize_with = "wire::ser::date")]
    pub date: NaiveDate,
    /// `requests`.
    pub requests: i64,
    /// `throttled`.
    pub throttled: i64,
}

/// `ApiKeyUsageOut` (`from_` serialised by its alias `from`).
#[derive(Debug, Clone, Serialize)]
pub struct ApiKeyUsageOut {
    /// `public_id`.
    pub public_id: String,
    /// `from`.
    #[serde(rename = "from", serialize_with = "wire::ser::date")]
    pub from: NaiveDate,
    /// `to`.
    #[serde(serialize_with = "wire::ser::date")]
    pub to: NaiveDate,
    /// `points`.
    pub points: Vec<ApiKeyUsagePointOut>,
    /// `total_requests`.
    pub total_requests: i64,
    /// `total_throttled`.
    pub total_throttled: i64,
}

// The token hash is deliberately not in this list: no read here may ever touch it.
const KEY_COLUMNS: &str = "id, public_id, name, prefix, scopes, rate_limit_per_minute, created_at, \
                           last_used_at, expires_at, revoked_at, revoked_reason";

/// `ApiKey.is_active(now=)`: not revoked, and not past an expiry.
#[must_use]
pub fn is_active(
    revoked_at: Option<DateTime<Utc>>,
    expires_at: Option<DateTime<Utc>>,
    now: DateTime<Utc>,
) -> bool {
    revoked_at.is_none() && expires_at.is_none_or(|x| x > now)
}

fn key_out(
    r: &PgRow,
    requests: i64,
    throttled: i64,
    now: DateTime<Utc>,
) -> Result<ApiKeyOut, Problem> {
    let prefix: String = r.try_get("prefix").map_err(db)?;
    let scopes = match row::jsonb(r, "scopes").map_err(|e| dbe(&e))? {
        OrderedJson::Array(items) => items
            .into_iter()
            .map(|i| match i {
                OrderedJson::Str(s) => s,
                other => other.to_python_json().unwrap_or_default(),
            })
            .collect(),
        _ => Vec::new(),
    };
    let revoked_at = row::opt_timestamptz(r, "revoked_at").map_err(|e| dbe(&e))?;
    let expires_at = row::opt_timestamptz(r, "expires_at").map_err(|e| dbe(&e))?;
    Ok(ApiKeyOut {
        public_id: r.try_get("public_id").map_err(db)?,
        name: r.try_get("name").map_err(db)?,
        display: format!("{KEY_TOKEN_MARKER}_{prefix}"),
        scopes,
        rate_limit_per_minute: r
            .try_get::<Option<i32>, _>("rate_limit_per_minute")
            .map_err(db)?
            .map(i64::from),
        created_at: row::timestamptz(r, "created_at").map_err(|e| dbe(&e))?,
        last_used_at: row::opt_timestamptz(r, "last_used_at").map_err(|e| dbe(&e))?,
        expires_at,
        revoked_at,
        revoked_reason: r.try_get("revoked_reason").map_err(db)?,
        requests_30d: requests,
        throttled_30d: throttled,
        active: is_active(revoked_at, expires_at, now),
    })
}

/// `GET /keys` (`list_api_keys`): newest first, revoked keys included, with 30 days of usage.
///
/// # Errors
/// 401 for an anonymous caller; 500.
pub async fn list_api_keys(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let user_id = principal.require_user()?;
    let rows = sqlx::query(&format!(
        "SELECT {KEY_COLUMNS} FROM api_key WHERE user_id = $1 ORDER BY created_at DESC, id DESC"
    ))
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let now = utc_now();
    let since = now.date_naive() - Duration::days(USAGE_WINDOW_DAYS);
    let mut ids = Vec::with_capacity(rows.len());
    for r in &rows {
        ids.push(r.try_get::<i64, _>("id").map_err(db)?);
    }
    let mut totals: HashMap<i64, (i64, i64)> = HashMap::new();
    if !ids.is_empty() {
        let sums = sqlx::query(
            "SELECT api_key_id, coalesce(sum(requests), 0)::bigint AS requests, \
             coalesce(sum(throttled), 0)::bigint AS throttled FROM api_key_usage_daily \
             WHERE api_key_id = ANY($1) AND date >= $2 GROUP BY api_key_id",
        )
        .bind(&ids)
        .bind(since)
        .fetch_all(state.db.pool())
        .await
        .map_err(db)?;
        for s in &sums {
            totals.insert(
                s.try_get("api_key_id").map_err(db)?,
                (
                    s.try_get("requests").map_err(db)?,
                    s.try_get("throttled").map_err(db)?,
                ),
            );
        }
    }
    let mut keys = Vec::with_capacity(rows.len());
    for (r, id) in rows.iter().zip(&ids) {
        let (requests, throttled) = totals.get(id).copied().unwrap_or((0, 0));
        keys.push(key_out(r, requests, throttled, now)?);
    }
    Ok(wire::json(&ApiKeyListOut {
        keys,
        public_api_enabled: state.settings.public_api_enabled && REVIEW_SIGNED_OFF,
        data_redistribution_signed_off: REVIEW_SIGNED_OFF,
    }))
}

/// `GET /keys/{public_id}/usage?days=` (`api_key_usage`): days with no traffic are absent.
///
/// # Errors
/// 401 for an anonymous caller; 400 for a non-integer `days` (FastAPI) or one outside 1–366
/// (`invalid-screen-definition`, checked before the key is looked up); 404 for a key not on the
/// account; 500.
pub async fn api_key_usage(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(public_id): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let days = accounts::int_param(&mut v, "days", q.get("days"), None, None);
    v.finish()?;
    let days = days.unwrap_or(i128::from(USAGE_WINDOW_DAYS));
    if days < 1 || days > i128::from(MAX_USAGE_WINDOW_DAYS) {
        return Err(accounts::invalid_with_message(&format!(
            "days must be between 1 and {MAX_USAGE_WINDOW_DAYS}"
        )));
    }
    let days = i64::try_from(days).unwrap_or(USAGE_WINDOW_DAYS);
    let user_id = principal.require_user()?;
    let key =
        sqlx::query("SELECT id, public_id FROM api_key WHERE user_id = $1 AND public_id = $2")
            .bind(user_id)
            .bind(&public_id)
            .fetch_optional(state.db.pool())
            .await
            .map_err(db)?;
    let Some(key) = key else {
        return Err(not_found("API key", &public_id));
    };
    let key_id: i64 = key.try_get("id").map_err(db)?;
    let until = utc_now().date_naive();
    let since = until - Duration::days(days - 1);
    let rows = sqlx::query(
        "SELECT date, requests, throttled FROM api_key_usage_daily WHERE api_key_id = $1 \
         AND date >= $2 AND date <= $3 ORDER BY date",
    )
    .bind(key_id)
    .bind(since)
    .bind(until)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut points = Vec::with_capacity(rows.len());
    for r in &rows {
        points.push(ApiKeyUsagePointOut {
            date: row::date(r, "date").map_err(|e| dbe(&e))?,
            requests: r.try_get("requests").map_err(db)?,
            throttled: r.try_get("throttled").map_err(db)?,
        });
    }
    let total_requests = points.iter().map(|p| p.requests).sum();
    let total_throttled = points.iter().map(|p| p.throttled).sum();
    Ok(wire::json(&ApiKeyUsageOut {
        public_id: key.try_get("public_id").map_err(db)?,
        from: since,
        to: until,
        points,
        total_requests,
        total_throttled,
    }))
}

/// The ported `/keys` routes, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/keys/{public_id}/rotate", post(writes::rotate_api_key))
        .route("/keys/{public_id}/revoke", post(writes::revoke_api_key))
        .route("/keys/{public_id}", delete(writes::delete_api_key))
        .route("/keys", get(list_api_keys).post(writes::create_api_key))
        .route("/keys/{public_id}/usage", get(api_key_usage))
}
