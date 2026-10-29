//! Curated engagement — pending actions + update posts for the sole user (SC9).
//!
//! Ports the **GET** routes of `baskfy_api.routers.curated_engage`
//! (`APIRouter(tags=["curated-engage"])`):
//!
//! * `GET /cb/pending-actions` — the sole tenant's open actions (neither dismissed nor
//!   resolved), newest first;
//! * `GET /cb/updates` — the 50 newest manager/engine update posts (a global feed, read only by
//!   a signed-in sole tenant in Track A).
//!
//! **Not ported in this wave** (they write; the Python API keeps serving them):
//! `POST /cb/pending-actions/{action_id}/dismiss` and `/resolve`. They never place orders
//! (Track C / PACK.2), and nothing here does either (law 2).

use axum::Router;
use axum::extract::{Extension, State};
use axum::response::Response;
use axum::routing::get;
use baskfy_db::{OrderedJson, row};
use chrono::{DateTime, Utc};
use serde::Serialize;
use sqlx::Row;

use super::screens::accounts::investments::{list_open_pending, pending_body, pending_title};
use super::screens::accounts::{db, dbe, scoped_sole_user_id};
use crate::auth::{Principal, require_authenticated};
use crate::problems::Problem;
use crate::state::AppState;
use crate::wire;

/// `PendingActionOut`.
#[derive(Debug, Clone, Serialize)]
pub struct PendingActionOut {
    /// `id`.
    pub id: i64,
    /// `type`.
    #[serde(rename = "type")]
    pub kind: String,
    /// `title`.
    pub title: String,
    /// `body`.
    pub body: Option<String>,
    /// `payload`: the jsonb object verbatim (`{}` when it is not an object).
    pub payload: OrderedJson,
    /// `created_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub created_at: DateTime<Utc>,
}

/// `PendingActionListOut`.
#[derive(Debug, Clone, Serialize)]
pub struct PendingActionListOut {
    /// `items`.
    pub items: Vec<PendingActionOut>,
    /// `count`.
    pub count: i64,
}

/// `UpdatePostOut`.
#[derive(Debug, Clone, Serialize)]
pub struct UpdatePostOut {
    /// `id`.
    pub id: i64,
    /// `title`.
    pub title: String,
    /// `body_md`.
    pub body_md: String,
    /// `published_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub published_at: DateTime<Utc>,
    /// `source`.
    pub source: String,
    /// `basket_id`.
    pub basket_id: Option<i64>,
    /// `manager_id`.
    pub manager_id: Option<i64>,
}

/// `UpdatePostListOut`.
#[derive(Debug, Clone, Serialize)]
pub struct UpdatePostListOut {
    /// `items`.
    pub items: Vec<UpdatePostOut>,
    /// `count`.
    pub count: i64,
}

/// `GET /cb/pending-actions` (`list_pending_actions`).
///
/// # Errors
/// 401 for an anonymous caller; the sole-tenant refusals; 500.
pub async fn list_pending_actions(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let rows = list_open_pending(&state, user_id).await?;
    let items: Vec<PendingActionOut> = rows
        .iter()
        .map(|p| PendingActionOut {
            id: p.id,
            kind: p.kind.clone(),
            title: pending_title(p),
            body: pending_body(p),
            payload: match &p.payload {
                OrderedJson::Object(_) => p.payload.clone(),
                _ => OrderedJson::Object(Vec::new()),
            },
            created_at: p.created_at,
        })
        .collect();
    let count = i64::try_from(items.len()).unwrap_or(i64::MAX);
    Ok(wire::json(&PendingActionListOut { items, count }))
}

/// `GET /cb/updates` (`list_update_posts`).
///
/// # Errors
/// 401 for an anonymous caller; the sole-tenant refusals; 500.
pub async fn list_update_posts(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    scoped_sole_user_id(&state, principal.user_id, None).await?;
    let rows = sqlx::query(
        "SELECT id, title, body_md, published_at, source, basket_id, manager_id \
         FROM cb_update_post ORDER BY published_at DESC LIMIT 50",
    )
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut items = Vec::with_capacity(rows.len());
    for r in &rows {
        items.push(UpdatePostOut {
            id: r.try_get("id").map_err(db)?,
            title: r.try_get("title").map_err(db)?,
            body_md: r.try_get("body_md").map_err(db)?,
            published_at: row::timestamptz(r, "published_at").map_err(|e| dbe(&e))?,
            source: r.try_get("source").map_err(db)?,
            basket_id: r.try_get("basket_id").map_err(db)?,
            manager_id: r.try_get("manager_id").map_err(db)?,
        });
    }
    let count = i64::try_from(items.len()).unwrap_or(i64::MAX);
    Ok(wire::json(&UpdatePostListOut { items, count }))
}

/// The ported curated-engagement GET routes, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/cb/pending-actions", get(list_pending_actions))
        .route("/cb/updates", get(list_update_posts))
}
