//! The **write** routes of `baskfy_api.routers.curated_engage` (wave 4c, lane writes-screens):
//! `POST /cb/pending-actions/{action_id}/dismiss` and `.../resolve` stamp `dismissed_at` /
//! `resolved_at` on one of the sole tenant's open pending actions. They never place an order
//! (law 2).

use axum::extract::{Extension, Path, State};
use axum::response::Response;
use baskfy_db::row;
use chrono::{DateTime, Utc};
use serde::Serialize;
use sqlx::Row;

use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, internal_error, not_found};
use crate::routes::screens::accounts::scoped_sole_user_id;
use crate::routes::screens::body_model::{self, Errors};
use crate::routes::screens::write_kit::{self, db};
use crate::session::{self, WritePool};
use crate::state::AppState;
use crate::wire;

/// `PendingActionMutationOut`.
#[derive(Debug, Clone, Serialize)]
struct PendingActionMutationOut {
    id: i64,
    #[serde(rename = "type")]
    kind: String,
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    dismissed_at: Option<DateTime<Utc>>,
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    resolved_at: Option<DateTime<Utc>>,
}

async fn stamp(
    state: &AppState,
    principal: &Principal,
    write: Option<Extension<WritePool>>,
    raw_id: &str,
    column: &'static str,
) -> Result<Response, Problem> {
    let principal = require_authenticated(principal)?.clone();
    let mut e = Errors::new();
    let action_id = body_model::path_int(&mut e, "action_id", raw_id);
    e.finish()?;
    let action_id = action_id.ok_or_else(internal_error)?;
    let user_id = scoped_sole_user_id(state, principal.user_id, None).await?;
    // `session.get(CbPendingAction, action_id)` with an id beyond bigint: asyncpg's DataError.
    let id = i64::try_from(action_id).map_err(|_| internal_error())?;
    let pool = write_kit::pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let found = sqlx::query(
                "SELECT user_id, dismissed_at, resolved_at FROM cb_pending_action WHERE id = $1",
            )
            .bind(id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            let open = match &found {
                Some(r) => {
                    let owner: i64 = r.try_get("user_id").map_err(db)?;
                    let dismissed = row::opt_timestamptz(r, "dismissed_at")
                        .map_err(|e| crate::db_failure(&e))?;
                    let resolved = row::opt_timestamptz(r, "resolved_at")
                        .map_err(|e| crate::db_failure(&e))?;
                    owner == user_id && dismissed.is_none() && resolved.is_none()
                }
                None => false,
            };
            if !open {
                return Err(not_found("pending action", &action_id.to_string()));
            }
            let r = sqlx::query(&format!(
                "UPDATE cb_pending_action SET {column} = $1 WHERE id = $2 \
                 RETURNING id, type, dismissed_at, resolved_at"
            ))
            .bind(write_kit::now_utc())
            .bind(id)
            .fetch_one(&mut **tx)
            .await
            .map_err(db)?;
            Ok(PendingActionMutationOut {
                id: r.try_get("id").map_err(db)?,
                kind: r.try_get("type").map_err(db)?,
                dismissed_at: row::opt_timestamptz(&r, "dismissed_at")
                    .map_err(|e| crate::db_failure(&e))?,
                resolved_at: row::opt_timestamptz(&r, "resolved_at")
                    .map_err(|e| crate::db_failure(&e))?,
            })
        })
    })
    .await?;
    Ok(wire::json(&out))
}

/// `POST /cb/pending-actions/{action_id}/dismiss` (`dismiss_pending_action`).
///
/// # Errors
/// 400 (path), 401, the sole-tenant refusals, 404 (absent, another tenant's, already closed), 500.
pub async fn dismiss_pending_action(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(raw_id): Path<String>,
) -> Result<Response, Problem> {
    stamp(&state, &principal, write, &raw_id, "dismissed_at").await
}

/// `POST /cb/pending-actions/{action_id}/resolve` (`resolve_pending_action`).
///
/// # Errors
/// As [`dismiss_pending_action`].
pub async fn resolve_pending_action(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(raw_id): Path<String>,
) -> Result<Response, Problem> {
    stamp(&state, &principal, write, &raw_id, "resolved_at").await
}
