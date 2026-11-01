//! ``/webhook-endpoints`` — outbound webhook registration (Prompt 20 deliverable 4).
//!
//! Ports the **GET** routes of `baskfy_api.routers.webhook_endpoints`
//! (`APIRouter(prefix="/webhook-endpoints", tags=["webhooks"])`):
//!
//! * `GET /webhook-endpoints` — the caller's endpoints, newest first;
//! * `GET /webhook-endpoints/{public_id}/deliveries` — the 50 most recent deliveries.
//!
//! No signing secret is returned by either (Python returns one only on create and rotate).
//!
//! The **writes** — `POST /webhook-endpoints`, `PATCH`/`DELETE /webhook-endpoints/{public_id}`,
//! `POST /webhook-endpoints/{public_id}/rotate-secret` — are in [`writes`] (wave 4c, lane
//! writes-accounts), with the SSRF guard in [`ssrf`]. **No route may place, modify or cancel an
//! order** (law 2).

mod ssrf;
mod writes;

use axum::Router;
use axum::extract::{Extension, Path, State};
use axum::response::Response;
use axum::routing::{get, patch, post};
use baskfy_db::{OrderedJson, row};
use chrono::{DateTime, Utc};
use serde::Serialize;
use sqlx::Row;

use super::screens::accounts::{db, dbe};
use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, ProblemType, not_found};
use crate::state::AppState;
use crate::wire;

/// Why `baskfy_api.webhooks.assert_public_webhook_url` refused a URL (wave 6, lane ops_alerts:
/// the worker's webhook sweep re-checks every endpoint at delivery time).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WebhookUrlRefusal {
    /// `WebhookUrlNotPublic(message)` — the sweep records the message and fails the delivery.
    NotPublic(String),
    /// A `ValueError`/`UnicodeError` Python does not catch there (a bad port, an unencodable
    /// host): the sweep raises it.
    Unhandled(String),
}

impl From<ssrf::Refusal> for WebhookUrlRefusal {
    fn from(value: ssrf::Refusal) -> Self {
        match value {
            ssrf::Refusal::BadRequest(m) => Self::NotPublic(m),
            ssrf::Refusal::Unhandled(m) => Self::Unhandled(m),
        }
    }
}

impl From<WebhookUrlRefusal> for ssrf::Refusal {
    fn from(value: WebhookUrlRefusal) -> Self {
        match value {
            WebhookUrlRefusal::NotPublic(m) => Self::BadRequest(m),
            WebhookUrlRefusal::Unhandled(m) => Self::Unhandled(m),
        }
    }
}

/// `baskfy_api.webhooks.assert_public_webhook_url(url)` with the resolution injected:
/// `resolve(host, port)` answers the addresses `getaddrinfo` would, or the refusal. The same
/// checks the create route's guard applies (`ssrf::assert_public`), for the worker's sweep.
///
/// # Errors
/// [`WebhookUrlRefusal`].
pub async fn assert_public_webhook_url_with<F, Fut>(
    url: &str,
    resolve: F,
) -> Result<(), WebhookUrlRefusal>
where
    F: FnOnce(String, u16) -> Fut,
    Fut: std::future::Future<Output = Result<Vec<std::net::IpAddr>, WebhookUrlRefusal>>,
{
    ssrf::assert_public_resolving(url, |host, port| async move {
        resolve(host, port).await.map_err(ssrf::Refusal::from)
    })
    .await
    .map_err(WebhookUrlRefusal::from)
}

/// The checks `getaddrinfo` applies to `host` before resolving (an embedded NUL, the `idna`
/// codec's label lengths): Python raises them uncaught.
///
/// # Errors
/// [`WebhookUrlRefusal::Unhandled`].
pub fn webhook_host_encodable(host: &str) -> Result<(), WebhookUrlRefusal> {
    ssrf::host_encodable(host).map_err(WebhookUrlRefusal::from)
}

/// `HISTORY_LIMIT`.
pub const HISTORY_LIMIT: i64 = 50;
/// `_KNOWN_EVENTS`.
pub const KNOWN_EVENTS: [&str; 2] = ["screen.entries", "screen.exits"];

/// `WebhookEndpointOut`.
#[derive(Debug, Clone, Serialize)]
pub struct WebhookEndpointOut {
    /// `public_id`.
    pub public_id: String,
    /// `screen_public_id`.
    pub screen_public_id: String,
    /// `screen_name`.
    pub screen_name: String,
    /// `url`.
    pub url: String,
    /// `events` (the known ones only).
    pub events: Vec<&'static str>,
    /// `secret_version`.
    pub secret_version: i64,
    /// `is_active`.
    pub is_active: bool,
    /// `consecutive_failures`.
    pub consecutive_failures: i64,
    /// `disabled_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub disabled_at: Option<DateTime<Utc>>,
    /// `disabled_reason`.
    pub disabled_reason: Option<String>,
    /// `last_delivery_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub last_delivery_at: Option<DateTime<Utc>>,
    /// `created_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub created_at: DateTime<Utc>,
}

/// `WebhookEndpointListOut`.
#[derive(Debug, Clone, Serialize)]
pub struct WebhookEndpointListOut {
    /// `endpoints`.
    pub endpoints: Vec<WebhookEndpointOut>,
}

/// `WebhookDeliveryOut`.
#[derive(Debug, Clone, Serialize)]
pub struct WebhookDeliveryOut {
    /// `id`.
    pub id: i64,
    /// `event`.
    pub event: &'static str,
    /// `status`: `delivered`, `failed` or `pending`.
    pub status: &'static str,
    /// `attempts`.
    pub attempts: i64,
    /// `response_status`.
    pub response_status: Option<i64>,
    /// `last_error`.
    pub last_error: Option<String>,
    /// `next_attempt_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub next_attempt_at: Option<DateTime<Utc>>,
    /// `created_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub created_at: DateTime<Utc>,
    /// `delivered_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub delivered_at: Option<DateTime<Utc>>,
}

/// `WebhookDeliveryListOut`.
#[derive(Debug, Clone, Serialize)]
pub struct WebhookDeliveryListOut {
    /// `deliveries`.
    pub deliveries: Vec<WebhookDeliveryOut>,
}

/// `_out`'s event narrowing: unknown events dropped, the rest mapped onto the Literal.
fn events(value: &OrderedJson) -> Vec<&'static str> {
    let OrderedJson::Array(items) = value else {
        return Vec::new();
    };
    items
        .iter()
        .filter_map(|i| match i {
            OrderedJson::Str(s) if KNOWN_EVENTS.contains(&s.as_str()) => {
                Some(if s == "screen.entries" {
                    "screen.entries"
                } else {
                    "screen.exits"
                })
            }
            _ => None,
        })
        .collect()
}

/// `GET /webhook-endpoints` (`list_endpoints`).
///
/// # Errors
/// 401 for an anonymous caller; 500.
pub async fn list_endpoints(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let user_id = principal.require_user()?;
    let rows = sqlx::query(
        "SELECT e.public_id, e.url, e.events, e.secret_version, e.is_active, \
         e.consecutive_failures, e.disabled_at, e.disabled_reason, e.last_delivery_at, \
         e.created_at, s.public_id AS screen_public_id, s.name AS screen_name \
         FROM webhook_endpoint e LEFT JOIN screen s ON s.id = e.screen_id \
         WHERE e.user_id = $1 ORDER BY e.created_at DESC, e.id DESC",
    )
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut endpoints = Vec::with_capacity(rows.len());
    for r in &rows {
        let screen_public_id: Option<String> = r.try_get("screen_public_id").map_err(db)?;
        let screen_name: Option<String> = r.try_get("screen_name").map_err(db)?;
        let (Some(screen_public_id), Some(screen_name)) = (screen_public_id, screen_name) else {
            return Err(Problem::new(
                ProblemType::InternalError,
                "endpoint points at a screen that is gone",
            ));
        };
        endpoints.push(WebhookEndpointOut {
            public_id: r.try_get("public_id").map_err(db)?,
            screen_public_id,
            screen_name,
            url: r.try_get("url").map_err(db)?,
            events: events(&row::jsonb(r, "events").map_err(|e| dbe(&e))?),
            secret_version: r
                .try_get::<i32, _>("secret_version")
                .map(i64::from)
                .map_err(db)?,
            is_active: r.try_get("is_active").map_err(db)?,
            consecutive_failures: r
                .try_get::<i32, _>("consecutive_failures")
                .map(i64::from)
                .map_err(db)?,
            disabled_at: row::opt_timestamptz(r, "disabled_at").map_err(|e| dbe(&e))?,
            disabled_reason: r.try_get("disabled_reason").map_err(db)?,
            last_delivery_at: row::opt_timestamptz(r, "last_delivery_at").map_err(|e| dbe(&e))?,
            created_at: row::timestamptz(r, "created_at").map_err(|e| dbe(&e))?,
        });
    }
    Ok(wire::json(&WebhookEndpointListOut { endpoints }))
}

/// `GET /webhook-endpoints/{public_id}/deliveries` (`endpoint_deliveries`).
///
/// # Errors
/// 401 for an anonymous caller; 404 `No webhook endpoint with id '…'.`; 500.
pub async fn endpoint_deliveries(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(public_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let user_id = principal.require_user()?;
    let endpoint_id: Option<i64> =
        sqlx::query_scalar("SELECT id FROM webhook_endpoint WHERE user_id = $1 AND public_id = $2")
            .bind(user_id)
            .bind(&public_id)
            .fetch_optional(state.db.pool())
            .await
            .map_err(db)?;
    let Some(endpoint_id) = endpoint_id else {
        return Err(not_found("webhook endpoint", &public_id));
    };
    let rows = sqlx::query(
        "SELECT id, event, status, attempts, response_status, last_error, next_attempt_at, \
         created_at, delivered_at FROM webhook_delivery WHERE endpoint_id = $1 \
         ORDER BY created_at DESC, id DESC LIMIT $2",
    )
    .bind(endpoint_id)
    .bind(HISTORY_LIMIT)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut deliveries = Vec::with_capacity(rows.len());
    for r in &rows {
        let event: String = r.try_get("event").map_err(db)?;
        let status: String = r.try_get("status").map_err(db)?;
        deliveries.push(WebhookDeliveryOut {
            id: r.try_get("id").map_err(db)?,
            event: if event == "screen.entries" {
                "screen.entries"
            } else {
                "screen.exits"
            },
            status: match status.as_str() {
                "delivered" => "delivered",
                "failed" => "failed",
                _ => "pending",
            },
            attempts: r.try_get::<i32, _>("attempts").map(i64::from).map_err(db)?,
            response_status: r
                .try_get::<Option<i32>, _>("response_status")
                .map_err(db)?
                .map(i64::from),
            last_error: r.try_get("last_error").map_err(db)?,
            next_attempt_at: row::opt_timestamptz(r, "next_attempt_at").map_err(|e| dbe(&e))?,
            created_at: row::timestamptz(r, "created_at").map_err(|e| dbe(&e))?,
            delivered_at: row::opt_timestamptz(r, "delivered_at").map_err(|e| dbe(&e))?,
        });
    }
    Ok(wire::json(&WebhookDeliveryListOut { deliveries }))
}

/// The ported `/webhook-endpoints` routes, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route(
            "/webhook-endpoints/{public_id}",
            patch(writes::update_endpoint).delete(writes::delete_endpoint),
        )
        .route(
            "/webhook-endpoints/{public_id}/rotate-secret",
            post(writes::rotate_secret),
        )
        .route(
            "/webhook-endpoints",
            get(list_endpoints).post(writes::create_endpoint),
        )
        .route(
            "/webhook-endpoints/{public_id}/deliveries",
            get(endpoint_deliveries),
        )
}
