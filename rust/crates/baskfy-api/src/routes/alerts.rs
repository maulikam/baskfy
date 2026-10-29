//! ``/alerts`` — screen-alert subscriptions (Prompt 20 deliverable 3).
//!
//! Ports the **GET** routes of `baskfy_api.routers.alerts` (`APIRouter(prefix="/alerts",
//! tags=["alerts"])`):
//!
//! * `GET /alerts` — every alert on the caller's account, newest first;
//! * `GET /alerts/{public_id}/deliveries` — what one alert did, night by night (30 nights).
//!
//! Both need a signed-in account (401 otherwise); another account's alert is a 404.
//!
//! **Not ported in this wave** (they write; the Python API keeps serving them): `POST /alerts`,
//! `PATCH`/`DELETE /alerts/{public_id}`, `POST /alerts/unsubscribe` (the session-less
//! one-click unsubscribe). **No route may place, modify or cancel an order** (law 2).

use axum::Router;
use axum::extract::{Extension, Path, State};
use axum::response::Response;
use axum::routing::get;
use baskfy_db::{OrderedJson, row};
use chrono::{DateTime, NaiveDate, Utc};
use serde::Serialize;
use sqlx::Row;

use super::screens::accounts::{db, dbe};
use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, ProblemType, not_found};
use crate::state::AppState;
use crate::wire;

/// `HISTORY_LIMIT`: past nights the delivery history returns.
pub const HISTORY_LIMIT: i64 = 30;

/// `ScreenAlertOut`.
#[derive(Debug, Clone, Serialize)]
pub struct ScreenAlertOut {
    /// `public_id`.
    pub public_id: String,
    /// `screen_public_id`.
    pub screen_public_id: String,
    /// `screen_name`.
    pub screen_name: String,
    /// `frequency`: `daily` or `weekly`.
    pub frequency: &'static str,
    /// `weekday`.
    pub weekday: Option<i64>,
    /// `top_n`.
    pub top_n: Option<i64>,
    /// `min_move`.
    pub min_move: i64,
    /// `digest`.
    pub digest: bool,
    /// `is_active`.
    pub is_active: bool,
    /// `last_sent_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub last_sent_at: Option<DateTime<Utc>>,
}

/// `ScreenAlertListOut`.
#[derive(Debug, Clone, Serialize)]
pub struct ScreenAlertListOut {
    /// `alerts`.
    pub alerts: Vec<ScreenAlertOut>,
}

/// `ScreenAlertDeliveryOut`.
#[derive(Debug, Clone, Serialize)]
pub struct ScreenAlertDeliveryOut {
    /// `as_of`.
    #[serde(serialize_with = "wire::ser::date")]
    pub as_of: NaiveDate,
    /// `previous_as_of`.
    #[serde(serialize_with = "wire::ser::opt_date")]
    pub previous_as_of: Option<NaiveDate>,
    /// `status`: `sent`, `failed` or `skipped`.
    pub status: &'static str,
    /// `entry_count`.
    pub entry_count: i64,
    /// `exit_count`.
    pub exit_count: i64,
    /// `change_count`.
    pub change_count: i64,
    /// `detail` (jsonb).
    pub detail: Option<OrderedJson>,
}

/// `ScreenAlertDeliveryListOut`.
#[derive(Debug, Clone, Serialize)]
pub struct ScreenAlertDeliveryListOut {
    /// `deliveries`.
    pub deliveries: Vec<ScreenAlertDeliveryOut>,
}

fn int(r: &sqlx::postgres::PgRow, column: &str) -> Result<i64, Problem> {
    r.try_get::<i32, _>(column).map(i64::from).map_err(db)
}

/// `GET /alerts` (`list_screen_alerts`): newest first, each with its screen.
///
/// # Errors
/// 401 for an anonymous caller; 500.
pub async fn list_screen_alerts(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let user_id = principal.require_user()?;
    let rows = sqlx::query(
        "SELECT a.public_id, a.frequency, a.weekday, a.top_n, a.min_move, a.digest, a.is_active, \
         a.last_sent_at, s.public_id AS screen_public_id, s.name AS screen_name \
         FROM screen_alert a LEFT JOIN screen s ON s.id = a.screen_id \
         WHERE a.user_id = $1 ORDER BY a.created_at DESC, a.id DESC",
    )
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut alerts = Vec::with_capacity(rows.len());
    for r in &rows {
        let screen_public_id: Option<String> = r.try_get("screen_public_id").map_err(db)?;
        let screen_name: Option<String> = r.try_get("screen_name").map_err(db)?;
        let (Some(screen_public_id), Some(screen_name)) = (screen_public_id, screen_name) else {
            // `_screen_for`: the FK cascades, so this cannot normally happen.
            return Err(Problem::new(
                ProblemType::InternalError,
                "alert points at a screen that is gone",
            ));
        };
        let frequency: String = r.try_get("frequency").map_err(db)?;
        let public_id: String = r.try_get("public_id").map_err(db)?;
        let frequency = match frequency.as_str() {
            "daily" => "daily",
            "weekly" => "weekly",
            _ => {
                return Err(Problem::new(
                    ProblemType::InternalError,
                    format!("alert {public_id} has a bad frequency"),
                ));
            }
        };
        alerts.push(ScreenAlertOut {
            public_id,
            screen_public_id,
            screen_name,
            frequency,
            weekday: r
                .try_get::<Option<i16>, _>("weekday")
                .map_err(db)?
                .map(i64::from),
            top_n: r
                .try_get::<Option<i32>, _>("top_n")
                .map_err(db)?
                .map(i64::from),
            min_move: int(r, "min_move")?,
            digest: r.try_get("digest").map_err(db)?,
            is_active: r.try_get("is_active").map_err(db)?,
            last_sent_at: row::opt_timestamptz(r, "last_sent_at").map_err(|e| dbe(&e))?,
        });
    }
    Ok(wire::json(&ScreenAlertListOut { alerts }))
}

/// `GET /alerts/{public_id}/deliveries` (`alert_deliveries`): the last 30 nights, skips and
/// their reasons included.
///
/// # Errors
/// 401 for an anonymous caller; 404 `No alert with id '…'.` for an alert not on the account;
/// 500.
pub async fn alert_deliveries(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(public_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let user_id = principal.require_user()?;
    let alert_id: Option<i64> =
        sqlx::query_scalar("SELECT id FROM screen_alert WHERE user_id = $1 AND public_id = $2")
            .bind(user_id)
            .bind(&public_id)
            .fetch_optional(state.db.pool())
            .await
            .map_err(db)?;
    let Some(alert_id) = alert_id else {
        return Err(not_found("alert", &public_id));
    };
    let rows = sqlx::query(
        "SELECT as_of, previous_as_of, status, entry_count, exit_count, change_count, detail \
         FROM screen_alert_delivery WHERE alert_id = $1 ORDER BY as_of DESC LIMIT $2",
    )
    .bind(alert_id)
    .bind(HISTORY_LIMIT)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut deliveries = Vec::with_capacity(rows.len());
    for r in &rows {
        let status: String = r.try_get("status").map_err(db)?;
        deliveries.push(ScreenAlertDeliveryOut {
            as_of: row::date(r, "as_of").map_err(|e| dbe(&e))?,
            previous_as_of: row::opt_date(r, "previous_as_of").map_err(|e| dbe(&e))?,
            status: match status.as_str() {
                "sent" => "sent",
                "failed" => "failed",
                _ => "skipped",
            },
            entry_count: int(r, "entry_count")?,
            exit_count: int(r, "exit_count")?,
            change_count: int(r, "change_count")?,
            detail: row::opt_jsonb(r, "detail").map_err(|e| dbe(&e))?,
        });
    }
    Ok(wire::json(&ScreenAlertDeliveryListOut { deliveries }))
}

/// The ported `/alerts` GET routes, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/alerts", get(list_screen_alerts))
        .route("/alerts/{public_id}/deliveries", get(alert_deliveries))
}
