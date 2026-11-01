//! The `/alerts` **writes** of `baskfy_api.routers.alerts`, with `baskfy_api.alerts`'
//! `create_alert`, `update_alert`, `delete_alert`, `unsubscribe` and `unsubscribe_token`:
//!
//! * `POST /alerts` (201) — subscribe a screen the caller can see (a system screen, or one of
//!   theirs); one alert per (account, screen);
//! * `PATCH /alerts/{public_id}` — the five options and `is_active`; only net changes are written
//!   (the ORM's rule), so `updated_at` moves only when something did;
//! * `DELETE /alerts/{public_id}` (204);
//! * `POST /alerts/unsubscribe` — **no session**: the derived token in the email turns one alert
//!   off; the answer is the same whether it matched or not (never an oracle).
//!
//! The unsubscribe token is `HMAC-SHA256(master, "alert:<public_id>")[:32]` with the master the
//! webhook signing secret or, failing that, the JWT secret; only its SHA-256 is stored. Nothing
//! here can place, modify or cancel an order (law 2).

use axum::extract::{Extension, Path, Request, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use baskfy_db::row;
use hmac::{Hmac, Mac};
use serde::Serialize;
use sha2::Sha256;
use sqlx::Row;

use super::ScreenAlertOut;
use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, ProblemType, internal_error, not_found};
use crate::routes::api_keys::model::{
    Field, Kind, Presence, Val, dbw, field, read_body, validate, write_pool,
};
use crate::routes::common::PyInt;
use crate::routes::screens::accounts::invalid_with_message;
use crate::security::{digest, token_hex};
use crate::session::{self, Tx, WritePool};
use crate::settings::Settings;
use crate::state::AppState;
use crate::wire;

/// `PUBLIC_ID_BYTES`.
const PUBLIC_ID_BYTES: usize = 12;
/// `UNSUBSCRIBE_TOKEN_HEX`.
const UNSUBSCRIBE_TOKEN_HEX: usize = 32;
/// `AlertFrequency`.
const FREQUENCIES: &[&str] = &["daily", "weekly"];

/// `unsubscribe_token(settings, public_id)`, or `None` when no master secret is configured
/// (Python raises `RuntimeError`: the 500 catch-all).
pub(crate) fn unsubscribe_token(settings: &Settings, public_id: &str) -> Option<String> {
    let master = if settings.webhook_signing_secret.expose().is_empty() {
        settings.jwt_secret.expose()
    } else {
        settings.webhook_signing_secret.expose()
    };
    if master.is_empty() {
        return None;
    }
    let mut mac = Hmac::<Sha256>::new_from_slice(master.as_bytes()).ok()?;
    mac.update(format!("alert:{public_id}").as_bytes());
    let hex: String = mac
        .finalize()
        .into_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    Some(hex[..UNSUBSCRIBE_TOKEN_HEX].to_owned())
}

/// An alert row and its screen, as `_out(alert, screen)` reads them.
#[derive(Debug, Clone)]
struct AlertRow {
    id: i64,
    public_id: String,
    frequency: String,
    weekday: Option<i64>,
    top_n: Option<i64>,
    min_move: i64,
    digest: bool,
    is_active: bool,
    last_sent_at: Option<chrono::DateTime<chrono::Utc>>,
    screen_public_id: String,
    screen_name: String,
}

impl AlertRow {
    fn out(&self) -> Result<ScreenAlertOut, Problem> {
        let frequency = match self.frequency.as_str() {
            "daily" => "daily",
            "weekly" => "weekly",
            _ => {
                return Err(Problem::new(
                    ProblemType::InternalError,
                    format!("alert {} has a bad frequency", self.public_id),
                ));
            }
        };
        Ok(ScreenAlertOut {
            public_id: self.public_id.clone(),
            screen_public_id: self.screen_public_id.clone(),
            screen_name: self.screen_name.clone(),
            frequency,
            weekday: self.weekday,
            top_n: self.top_n,
            min_move: self.min_move,
            digest: self.digest,
            is_active: self.is_active,
            last_sent_at: self.last_sent_at,
        })
    }
}

const ALERT_SELECT: &str = "SELECT a.id, a.public_id, a.frequency, a.weekday::bigint AS weekday, \
     a.top_n::bigint AS top_n, a.min_move::bigint AS min_move, a.digest, a.is_active, \
     a.last_sent_at, s.public_id AS screen_public_id, s.name AS screen_name \
     FROM screen_alert a JOIN screen s ON s.id = a.screen_id";

fn alert_row(r: &sqlx::postgres::PgRow) -> Result<AlertRow, Problem> {
    Ok(AlertRow {
        id: r.try_get("id").map_err(dbw)?,
        public_id: r.try_get("public_id").map_err(dbw)?,
        frequency: r.try_get("frequency").map_err(dbw)?,
        weekday: r.try_get("weekday").map_err(dbw)?,
        top_n: r.try_get("top_n").map_err(dbw)?,
        min_move: r.try_get("min_move").map_err(dbw)?,
        digest: r.try_get("digest").map_err(dbw)?,
        is_active: r.try_get("is_active").map_err(dbw)?,
        last_sent_at: row::opt_timestamptz(r, "last_sent_at").map_err(|e| crate::db_failure(&e))?,
        screen_public_id: r.try_get("screen_public_id").map_err(dbw)?,
        screen_name: r.try_get("screen_name").map_err(dbw)?,
    })
}

/// `_load(session, principal, public_id)`: the caller's alert, or 404.
async fn load(tx: &mut Tx, user_id: i64, public_id: &str) -> Result<AlertRow, Problem> {
    let found = sqlx::query(&format!(
        "{ALERT_SELECT} WHERE a.user_id = $1 AND a.public_id = $2"
    ))
    .bind(user_id)
    .bind(public_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(dbw)?;
    match found {
        Some(r) => alert_row(&r),
        None => Err(not_found("alert", public_id)),
    }
}

/// An `int` column write: asyncpg refuses a value outside int32 before the statement is sent,
/// an unhandled `DataError` (the 500 catch-all).
fn int32(v: &PyInt) -> Result<i32, Problem> {
    v.value.and_then(|x| i32::try_from(x).ok()).ok_or_else(|| {
        crate::logging::error(
            "baskfy_api.app",
            "unhandled error",
            &[("error", "value out of int32 range")],
        );
        internal_error()
    })
}

fn opt_int32(v: &Val) -> Result<Option<i32>, Problem> {
    v.int().map(int32).transpose()
}

/// `ScreenAlertCreate`.
const CREATE: [Field; 6] = [
    field(
        "screen_public_id",
        Kind::Str {
            min: None,
            max: None,
        },
        Presence::Required,
    ),
    field("frequency", Kind::Literal(FREQUENCIES), Presence::Defaulted),
    field(
        "weekday",
        Kind::Int {
            ge: Some(0),
            le: Some(6),
        },
        Presence::Optional,
    ),
    field(
        "top_n",
        Kind::Int {
            ge: Some(1),
            le: None,
        },
        Presence::Optional,
    ),
    field(
        "min_move",
        Kind::Int {
            ge: Some(1),
            le: None,
        },
        Presence::Optional,
    ),
    field("digest", Kind::Bool, Presence::Defaulted),
];

/// `POST /alerts` (`create_screen_alert`): 201.
///
/// # Errors
/// 400 (body; the screen already has an alert on this account), 401, 404 (a screen the caller
/// cannot see), 500.
pub async fn create_screen_alert(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let received = read_body(request).await?;
    let principal = require_authenticated(&principal)?;
    let values = validate(&received, &CREATE)?;
    let user_id = principal.require_user()?;
    let screen_public_id = values[0].str().unwrap_or_default().to_owned();
    let frequency = values[1].str().unwrap_or("daily").to_owned();
    let weekday = values[2].clone();
    let top_n = values[3].clone();
    let min_move = values[4].clone();
    let digest_preference = values[5].bool().unwrap_or(false);
    let pool = write_pool(write)?;
    let settings = state.settings.clone();
    let alert = session::run(&pool, move |tx| {
        Box::pin(async move {
            let screen =
                sqlx::query("SELECT id, user_id, public_id, name FROM screen WHERE public_id = $1")
                    .bind(&screen_public_id)
                    .fetch_optional(&mut **tx)
                    .await
                    .map_err(dbw)?;
            let screen = match screen {
                Some(s) => {
                    let owner: Option<i64> = s.try_get("user_id").map_err(dbw)?;
                    if owner.is_some_and(|o| o != user_id) {
                        return Err(not_found("screen", &screen_public_id));
                    }
                    s
                }
                None => return Err(not_found("screen", &screen_public_id)),
            };
            let screen_id: i64 = screen.try_get("id").map_err(dbw)?;
            let existing: Option<i64> = sqlx::query_scalar(
                "SELECT id FROM screen_alert WHERE user_id = $1 AND screen_id = $2",
            )
            .bind(user_id)
            .bind(screen_id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(dbw)?;
            if existing.is_some() {
                return Err(invalid_with_message(
                    "This screen already has an alert on this account; update it instead of \
                     adding one.",
                ));
            }
            let public_id = token_hex(PUBLIC_ID_BYTES);
            let weekday = if frequency == "weekly" {
                opt_int32(&weekday)?
            } else {
                None
            };
            let top_n = opt_int32(&top_n)?;
            let min_move = match min_move.int() {
                Some(v) => int32(v)?,
                None => i32::try_from(settings.alert_min_move).map_err(|_| internal_error())?,
            };
            let Some(token) = unsubscribe_token(&settings, &public_id) else {
                crate::logging::error(
                    "baskfy_api.app",
                    "unhandled error",
                    &[("error", "no master secret to sign an unsubscribe link")],
                );
                return Err(internal_error());
            };
            let id: i64 = sqlx::query_scalar(
                "INSERT INTO screen_alert (public_id, user_id, screen_id, frequency, weekday, \
                 top_n, min_move, digest, is_active, unsubscribe_token_hash) \
                 VALUES ($1, $2, $3, $4, $5::smallint, $6, $7, $8, true, $9) RETURNING id",
            )
            .bind(&public_id)
            .bind(user_id)
            .bind(screen_id)
            .bind(&frequency)
            .bind(weekday)
            .bind(top_n)
            .bind(min_move)
            .bind(digest_preference)
            .bind(digest(&token))
            .fetch_one(&mut **tx)
            .await
            .map_err(dbw)?;
            Ok(AlertRow {
                id,
                public_id,
                frequency,
                weekday: weekday.map(i64::from),
                top_n: top_n.map(i64::from),
                min_move: i64::from(min_move),
                digest: digest_preference,
                is_active: true,
                last_sent_at: None,
                screen_public_id: screen.try_get("public_id").map_err(dbw)?,
                screen_name: screen.try_get("name").map_err(dbw)?,
            })
        })
    })
    .await?;
    Ok(wire::json_status(StatusCode::CREATED, &alert.out()?))
}

/// `ScreenAlertUpdate`.
const UPDATE: [Field; 6] = [
    field("frequency", Kind::Literal(FREQUENCIES), Presence::Optional),
    field(
        "weekday",
        Kind::Int {
            ge: Some(0),
            le: Some(6),
        },
        Presence::Optional,
    ),
    field(
        "top_n",
        Kind::Int {
            ge: Some(1),
            le: None,
        },
        Presence::Optional,
    ),
    field(
        "min_move",
        Kind::Int {
            ge: Some(1),
            le: None,
        },
        Presence::Optional,
    ),
    field("digest", Kind::Bool, Presence::Optional),
    field("is_active", Kind::Bool, Presence::Optional),
];

/// `PATCH /alerts/{public_id}` (`update_screen_alert`).
///
/// # Errors
/// 400 (body), 401, 404, 500.
pub async fn update_screen_alert(
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(public_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    update(principal, write, public_id, request).await
}

/// `PATCH /alerts/unsubscribe`: Starlette matches it to `PATCH /alerts/{public_id}`.
///
/// # Errors
/// As [`update_screen_alert`].
pub async fn update_unsubscribe_path(
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    update(principal, write, "unsubscribe".to_owned(), request).await
}

async fn update(
    principal: Principal,
    write: Option<Extension<WritePool>>,
    public_id: String,
    request: Request,
) -> Result<Response, Problem> {
    let received = read_body(request).await?;
    let principal = require_authenticated(&principal)?;
    let values = validate(&received, &UPDATE)?;
    let user_id = principal.require_user()?;
    let pool = write_pool(write)?;
    let alert = session::run(&pool, move |tx| {
        Box::pin(async move {
            let mut alert = load(tx, user_id, &public_id).await?;
            let before = alert.clone();
            if let Some(f) = values[0].str() {
                alert.frequency = f.to_owned();
                if f == "daily" {
                    alert.weekday = None;
                }
            }
            if let Some(w) = values[1].int()
                && alert.frequency == "weekly"
            {
                alert.weekday = Some(i64::from(int32(w)?));
            }
            if let Some(n) = values[2].int() {
                alert.top_n = Some(i64::from(int32(n)?));
            }
            if let Some(m) = values[3].int() {
                alert.min_move = i64::from(int32(m)?);
            }
            if let Some(d) = values[4].bool() {
                alert.digest = d;
            }
            if let Some(a) = values[5].bool() {
                alert.is_active = a;
            }
            // The ORM flushes only net changes, and `updated_at` only with them.
            let mut sets: Vec<String> = Vec::new();
            let mut n = 1;
            let mut next = |col: &str, cast: &str| {
                n += 1;
                sets.push(format!("{col} = ${n}{cast}"));
            };
            let frequency_changed = alert.frequency != before.frequency;
            let weekday_changed = alert.weekday != before.weekday;
            let top_n_changed = alert.top_n != before.top_n;
            let min_move_changed = alert.min_move != before.min_move;
            let digest_changed = alert.digest != before.digest;
            let active_changed = alert.is_active != before.is_active;
            if frequency_changed {
                next("frequency", "");
            }
            if weekday_changed {
                next("weekday", "::smallint");
            }
            if top_n_changed {
                next("top_n", "::integer");
            }
            if min_move_changed {
                next("min_move", "::integer");
            }
            if digest_changed {
                next("digest", "");
            }
            if active_changed {
                next("is_active", "");
            }
            if !sets.is_empty() {
                let sql = format!(
                    "UPDATE screen_alert SET {}, updated_at = now() WHERE id = $1",
                    sets.join(", ")
                );
                let mut q = sqlx::query(&sql).bind(alert.id);
                if frequency_changed {
                    q = q.bind(alert.frequency.clone());
                }
                if weekday_changed {
                    q = q.bind(alert.weekday);
                }
                if top_n_changed {
                    q = q.bind(alert.top_n);
                }
                if min_move_changed {
                    q = q.bind(alert.min_move);
                }
                if digest_changed {
                    q = q.bind(alert.digest);
                }
                if active_changed {
                    q = q.bind(alert.is_active);
                }
                q.execute(&mut **tx).await.map_err(dbw)?;
            }
            Ok(alert)
        })
    })
    .await?;
    Ok(wire::json(&alert.out()?))
}

/// `DELETE /alerts/{public_id}` (`delete_screen_alert`): 204.
///
/// # Errors
/// 401, 404, 500.
pub async fn delete_screen_alert(
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(public_id): Path<String>,
) -> Result<Response, Problem> {
    delete(principal, write, public_id).await
}

/// `DELETE /alerts/unsubscribe`: Starlette matches it to `DELETE /alerts/{public_id}`.
///
/// # Errors
/// As [`delete_screen_alert`].
pub async fn delete_unsubscribe_path(
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
) -> Result<Response, Problem> {
    delete(principal, write, "unsubscribe".to_owned()).await
}

async fn delete(
    principal: Principal,
    write: Option<Extension<WritePool>>,
    public_id: String,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let user_id = principal.require_user()?;
    let pool = write_pool(write)?;
    session::run(&pool, move |tx| {
        Box::pin(async move {
            let alert = load(tx, user_id, &public_id).await?;
            sqlx::query("DELETE FROM screen_alert WHERE id = $1")
                .bind(alert.id)
                .execute(&mut **tx)
                .await
                .map_err(dbw)?;
            Ok(())
        })
    })
    .await?;
    Ok(StatusCode::NO_CONTENT.into_response())
}

/// `UnsubscribeOut`.
#[derive(Debug, Clone, Serialize)]
struct UnsubscribeOut {
    unsubscribed: bool,
    screen_name: Option<String>,
}

/// `UnsubscribeIn`.
const UNSUBSCRIBE: [Field; 1] = [field(
    "token",
    Kind::Str {
        min: None,
        max: Some(128),
    },
    Presence::Required,
)];

/// `POST /alerts/unsubscribe` (`unsubscribe_from_alert`): no session; idempotent; never an
/// oracle.
///
/// # Errors
/// 400 (body), 500.
pub async fn unsubscribe_from_alert(
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let received = read_body(request).await?;
    let values = validate(&received, &UNSUBSCRIBE)?;
    let token = match &values[0] {
        Val::Str(s) => s.clone(),
        _ => String::new(),
    };
    let pool = write_pool(write)?;
    let name = session::run(&pool, move |tx| {
        Box::pin(async move {
            let found = sqlx::query(&format!(
                "{ALERT_SELECT} WHERE a.unsubscribe_token_hash = $1"
            ))
            .bind(digest(&token))
            .fetch_optional(&mut **tx)
            .await
            .map_err(dbw)?;
            let Some(r) = found else {
                return Ok(None);
            };
            let alert = alert_row(&r)?;
            if alert.is_active {
                sqlx::query(
                    "UPDATE screen_alert SET is_active = false, updated_at = now() WHERE id = $1",
                )
                .bind(alert.id)
                .execute(&mut **tx)
                .await
                .map_err(dbw)?;
            }
            Ok(Some(alert.screen_name))
        })
    })
    .await?;
    Ok(wire::json(&UnsubscribeOut {
        unsubscribed: true,
        screen_name: name,
    }))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;
    use baskfy_providers::secret::Secret;

    #[test]
    fn the_unsubscribe_token_is_the_derived_hmac() {
        // hmac.new(b"master", b"alert:abc", sha256).hexdigest()[:32]
        let settings = Settings {
            webhook_signing_secret: Secret::new("master"),
            ..Settings::default()
        };
        let token = unsubscribe_token(&settings, "abc").unwrap();
        assert_eq!(token, "969987d1c694d18a027b28253eecbd73");
        // Falls back to the JWT secret, and refuses with neither.
        let jwt_only = Settings {
            webhook_signing_secret: Secret::new(""),
            jwt_secret: Secret::new("master"),
            ..Settings::default()
        };
        assert_eq!(unsubscribe_token(&jwt_only, "abc"), Some(token));
        let neither = Settings {
            webhook_signing_secret: Secret::new(""),
            jwt_secret: Secret::new(""),
            ..Settings::default()
        };
        assert_eq!(unsubscribe_token(&neither, "abc"), None);
    }
}
