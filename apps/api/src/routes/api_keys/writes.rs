//! The `/keys` **writes** of `baskfy_api.routers.api_keys`, with `baskfy_api.api_keys`'
//! `create_key`, `rotate_key` and `revoke_key` and `baskfy_core.api_keys.normalise_scopes`:
//!
//! * `POST /keys` (201) — mint a key; **the response is the only place its secret ever
//!   appears** (`secret`, shown once). Only its SHA-256 is stored, and nothing logs either;
//! * `POST /keys/{public_id}/rotate` — a replacement, and the original revoked at once;
//! * `POST /keys/{public_id}/revoke` — idempotent (the first revocation's stamp is kept);
//! * `DELETE /keys/{public_id}` (204) — only a revoked key.
//!
//! Every route requires a signed-in account, never a key. Each runs in one write transaction
//! (`session::run`): a refusal after a write rolls the write back, as Python's session does.
//! Nothing here can place, modify or cancel an order (law 2).

use axum::extract::{Extension, Path, Request, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use baskfy_db::{OrderedJson, row};
use chrono::{DateTime, Duration, Utc};
use serde::Serialize;
use sqlx::Row;

use super::model::{
    Field, Kind, Presence, PyDateTime, Val, dbw, field, now_micro, read_body, ser_opt_datetime,
    validate, write_pool,
};
use super::{KEY_TOKEN_MARKER, is_active};
use crate::auth::{Principal, require_authenticated};
use crate::logging;
use crate::problems::{Problem, not_found};
use crate::routes::screens::accounts::invalid_with_message;
use crate::security::{digest, new_opaque_token, token_hex};
use crate::session::{self, Tx, WritePool};
use crate::state::AppState;
use crate::wire;

/// `baskfy_core.api_keys.Scope`, in declaration order (`normalise_scopes` returns this order).
pub const SCOPES: [&str; 4] = ["screens:read", "factors:read", "breadth:read", "meta:read"];
/// `MAX_KEYS_PER_ACCOUNT`.
pub const MAX_KEYS_PER_ACCOUNT: i64 = 10;
/// `PUBLIC_ID_BYTES`.
const PUBLIC_ID_BYTES: usize = 12;
/// `KEY_PREFIX_LENGTH` (hex characters).
const KEY_PREFIX_LENGTH: usize = 12;

/// `ApiKeyOut` as a write answers it: `expires_at` is the object Python holds, which for a new
/// key is the parsed request value with its own offset (or none), not the stored instant.
#[derive(Debug, Clone, Serialize)]
struct KeyOut {
    public_id: String,
    name: String,
    display: String,
    scopes: Vec<String>,
    rate_limit_per_minute: Option<i64>,
    #[serde(serialize_with = "wire::ser::datetime")]
    created_at: DateTime<Utc>,
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    last_used_at: Option<DateTime<Utc>>,
    #[serde(serialize_with = "ser_opt_datetime")]
    expires_at: Option<PyDateTime>,
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    revoked_at: Option<DateTime<Utc>>,
    revoked_reason: Option<String>,
    requests_30d: i64,
    throttled_30d: i64,
    active: bool,
}

/// `ApiKeyIssuedOut`: the one response carrying the plaintext.
#[derive(Debug, Clone, Serialize)]
struct IssuedOut {
    key: KeyOut,
    secret: String,
}

/// A key row as the routes need it (the hash is never selected).
#[derive(Debug, Clone)]
struct KeyRow {
    id: i64,
    public_id: String,
    user_id: i64,
    name: String,
    prefix: String,
    scopes: Vec<String>,
    rate_limit_per_minute: Option<i64>,
    created_at: DateTime<Utc>,
    last_used_at: Option<DateTime<Utc>>,
    expires_at: Option<PyDateTime>,
    revoked_at: Option<DateTime<Utc>>,
    revoked_reason: Option<String>,
}

impl KeyRow {
    fn active(&self, now: DateTime<Utc>) -> bool {
        is_active(
            self.revoked_at,
            self.expires_at.as_ref().map(PyDateTime::aware),
            now,
        )
    }

    /// `_out(key)`: the usage counters are zero on every write's answer.
    fn out(&self, now: DateTime<Utc>) -> KeyOut {
        KeyOut {
            public_id: self.public_id.clone(),
            name: self.name.clone(),
            display: format!("{KEY_TOKEN_MARKER}_{}", self.prefix),
            scopes: self.scopes.clone(),
            rate_limit_per_minute: self.rate_limit_per_minute,
            created_at: self.created_at,
            last_used_at: self.last_used_at,
            expires_at: self.expires_at,
            revoked_at: self.revoked_at,
            revoked_reason: self.revoked_reason.clone(),
            requests_30d: 0,
            throttled_30d: 0,
            active: self.active(now),
        }
    }
}

fn scopes_of(value: OrderedJson) -> Vec<String> {
    match value {
        OrderedJson::Array(items) => items
            .into_iter()
            .map(|i| match i {
                OrderedJson::Str(s) => s,
                other => other.to_python_json().unwrap_or_default(),
            })
            .collect(),
        _ => Vec::new(),
    }
}

/// `load_key(session, user_id, public_id)`.
async fn load(tx: &mut Tx, user_id: i64, public_id: &str) -> Result<KeyRow, Problem> {
    let found = sqlx::query(
        "SELECT id, public_id, user_id, name, prefix, scopes, rate_limit_per_minute, created_at, \
         last_used_at, expires_at, revoked_at, revoked_reason FROM api_key \
         WHERE user_id = $1 AND public_id = $2",
    )
    .bind(user_id)
    .bind(public_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(dbw)?;
    let Some(r) = found else {
        return Err(not_found("API key", public_id));
    };
    let dbe = |e: baskfy_db::DbError| crate::db_failure(&e);
    Ok(KeyRow {
        id: r.try_get("id").map_err(dbw)?,
        public_id: r.try_get("public_id").map_err(dbw)?,
        user_id: r.try_get("user_id").map_err(dbw)?,
        name: r.try_get("name").map_err(dbw)?,
        prefix: r.try_get("prefix").map_err(dbw)?,
        scopes: scopes_of(row::jsonb(&r, "scopes").map_err(dbe)?),
        rate_limit_per_minute: r
            .try_get::<Option<i32>, _>("rate_limit_per_minute")
            .map_err(dbw)?
            .map(i64::from),
        created_at: row::timestamptz(&r, "created_at").map_err(dbe)?,
        last_used_at: row::opt_timestamptz(&r, "last_used_at").map_err(dbe)?,
        expires_at: row::opt_timestamptz(&r, "expires_at")
            .map_err(dbe)?
            .map(PyDateTime::from_utc),
        revoked_at: row::opt_timestamptz(&r, "revoked_at").map_err(dbe)?,
        revoked_reason: r.try_get("revoked_reason").map_err(dbw)?,
    })
}

/// `normalise_scopes(requested)`: every scope when omitted; an unknown one or an empty list is
/// an error; the result in the registry's order.
fn normalise_scopes(requested: Option<&[String]>) -> Result<Vec<String>, String> {
    let Some(requested) = requested else {
        return Ok(SCOPES.iter().map(|s| (*s).to_owned()).collect());
    };
    for name in requested {
        if !SCOPES.contains(&name.as_str()) {
            return Err(format!(
                "unknown scope {}; known scopes are {}",
                baskfy_providers::py::repr_str(name),
                SCOPES.join(", ")
            ));
        }
    }
    if requested.is_empty() {
        return Err(
            "a key with no scopes could read nothing; omit the field for all scopes".to_owned(),
        );
    }
    Ok(SCOPES
        .iter()
        .filter(|s| requested.iter().any(|r| r == *s))
        .map(|s| (*s).to_owned())
        .collect())
}

/// What `create_key` raises, mapped by its caller.
enum CreateError {
    /// `KeyLimitReached`: the router's 400 on both routes.
    Limit(String),
    /// Any other `ValueError`: `create_api_key`'s 400, `rotate_api_key`'s 500.
    Value(String),
    /// `rotate_key` lets a plain `ValueError` escape: the 500 catch-all.
    Problem(Problem),
}

impl From<Problem> for CreateError {
    fn from(p: Problem) -> Self {
        Self::Problem(p)
    }
}

/// The inputs of `create_key`.
struct NewKey<'a> {
    user_id: i64,
    name: &'a str,
    scopes: Vec<String>,
    rate_limit_per_minute: Option<&'a crate::routes::common::PyInt>,
    expires_at: Option<PyDateTime>,
    rotated_from_id: Option<i64>,
}

/// `create_key(...)`: mint, store the digest, return the row and the plaintext.
async fn create_key(
    tx: &mut Tx,
    state: &AppState,
    new: NewKey<'_>,
) -> Result<(KeyRow, String), CreateError> {
    let name = crate::routes::screens::accounts::investments::py_strip(new.name);
    if name.is_empty() {
        return Err(CreateError::Value(
            "a key needs a name; an unlabelled key is one nobody dares revoke".to_owned(),
        ));
    }
    let ceiling = state.settings.api_key_max_rate_limit_per_minute;
    let mut rate_limit = None;
    if let Some(limit) = new.rate_limit_per_minute {
        let v = limit.saturated();
        if v < 1 {
            return Err(CreateError::Value(
                "rate_limit_per_minute must be at least 1".to_owned(),
            ));
        }
        if v > i128::from(ceiling) {
            return Err(CreateError::Value(format!(
                "rate_limit_per_minute may not exceed {ceiling}"
            )));
        }
        rate_limit = i32::try_from(v).ok();
    }
    let live: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM api_key WHERE user_id = $1 AND revoked_at IS NULL",
    )
    .bind(new.user_id)
    .fetch_one(&mut **tx)
    .await
    .map_err(dbw)?;
    if live >= MAX_KEYS_PER_ACCOUNT {
        return Err(CreateError::Limit(format!(
            "This account already holds {MAX_KEYS_PER_ACCOUNT} live keys. Revoke one first."
        )));
    }
    let expiry = match (new.expires_at, state.settings.api_key_default_ttl_days) {
        (Some(e), _) => Some(e),
        (None, Some(days)) => Some(PyDateTime::from_utc(now_micro() + Duration::days(days))),
        (None, None) => None,
    };
    let prefix = token_hex(KEY_PREFIX_LENGTH / 2);
    let secret = new_opaque_token();
    let public_id = token_hex(PUBLIC_ID_BYTES);
    let scopes_json = serde_json::Value::Array(
        new.scopes
            .iter()
            .map(|s| serde_json::Value::String(s.clone()))
            .collect(),
    );
    let inserted = sqlx::query(
        "INSERT INTO api_key (public_id, user_id, name, prefix, token_hash, scopes, \
         rate_limit_per_minute, expires_at, rotated_from_id) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id, created_at",
    )
    .bind(&public_id)
    .bind(new.user_id)
    .bind(name)
    .bind(&prefix)
    .bind(digest(&secret))
    .bind(sqlx::types::Json(&scopes_json))
    .bind(rate_limit)
    .bind(expiry.as_ref().map(PyDateTime::instant))
    .bind(new.rotated_from_id)
    .fetch_one(&mut **tx)
    .await
    .map_err(dbw)?;
    let created_at =
        row::timestamptz(&inserted, "created_at").map_err(|e| crate::db_failure(&e))?;
    logging::info(
        "baskfy_api.api_keys",
        "api key created",
        &[("key_public_id", &public_id), ("prefix", &prefix)],
    );
    let plaintext = format!("{KEY_TOKEN_MARKER}_{prefix}_{secret}");
    Ok((
        KeyRow {
            id: inserted.try_get("id").map_err(dbw)?,
            public_id,
            user_id: new.user_id,
            name: name.to_owned(),
            prefix,
            scopes: new.scopes,
            rate_limit_per_minute: rate_limit.map(i64::from),
            created_at,
            last_used_at: None,
            expires_at: expiry,
            revoked_at: None,
            revoked_reason: None,
        },
        plaintext,
    ))
}

fn issued(key: &KeyRow, secret: String, status: StatusCode) -> Response {
    wire::json_status(
        status,
        &IssuedOut {
            key: key.out(utc_now()),
            secret,
        },
    )
}

fn utc_now() -> DateTime<Utc> {
    crate::entitlements::utc_now()
}

/// `ApiKeyCreate`.
const CREATE: [Field; 4] = [
    field(
        "name",
        Kind::Str {
            min: Some(1),
            max: Some(120),
        },
        Presence::Required,
    ),
    field("scopes", Kind::StrList, Presence::Optional),
    field(
        "rate_limit_per_minute",
        Kind::Int {
            ge: Some(1),
            le: None,
        },
        Presence::Optional,
    ),
    field("expires_at", Kind::DateTime, Presence::Optional),
];

/// `POST /keys` (`create_api_key`): 201 with the key and, once, its secret.
///
/// # Errors
/// 400 (body; an unknown or empty scope list; a blank name; a rate limit above the ceiling; ten
/// live keys already), 401 (anonymous), 500.
pub async fn create_api_key(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let received = read_body(request).await?;
    let principal = require_authenticated(&principal)?;
    let values = validate(&received, &CREATE)?;
    let user_id = principal.require_user()?;
    let name = values[0].str().unwrap_or_default().to_owned();
    let scopes =
        normalise_scopes(values[1].list()).map_err(|message| invalid_with_message(&message))?;
    let rate_limit = values[2].int().cloned();
    let expires_at = values[3].datetime().copied();
    let pool = write_pool(write)?;
    let state2 = state.clone();
    let (key, secret) = session::run(&pool, move |tx| {
        Box::pin(async move {
            let new = NewKey {
                user_id,
                name: &name,
                scopes,
                rate_limit_per_minute: rate_limit.as_ref(),
                expires_at,
                rotated_from_id: None,
            };
            match create_key(tx, &state2, new).await {
                Ok(done) => Ok(done),
                Err(CreateError::Value(message) | CreateError::Limit(message)) => {
                    Err(invalid_with_message(&message))
                }
                Err(CreateError::Problem(p)) => Err(p),
            }
        })
    })
    .await?;
    Ok(issued(&key, secret, StatusCode::CREATED))
}

/// `POST /keys/{public_id}/rotate` (`rotate_api_key`): a replacement, the original revoked.
///
/// # Errors
/// 400 (already revoked or expired; ten live keys), 401, 404, 500.
pub async fn rotate_api_key(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(public_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let user_id = principal.require_user()?;
    let pool = write_pool(write)?;
    let state2 = state.clone();
    let (key, secret) = session::run(&pool, move |tx| {
        Box::pin(async move {
            let old = load(tx, user_id, &public_id).await?;
            let now = utc_now();
            if !old.active(now) {
                return Err(invalid_with_message(
                    "That key is already revoked; create a new one instead.",
                ));
            }
            let limit = old
                .rate_limit_per_minute
                .and_then(|v| crate::routes::common::lax_int(&v.to_string()));
            let new = NewKey {
                user_id: old.user_id,
                name: &old.name,
                scopes: old.scopes.clone(),
                rate_limit_per_minute: limit.as_ref(),
                expires_at: old.expires_at,
                rotated_from_id: Some(old.id),
            };
            let issued = match create_key(tx, &state2, new).await {
                Ok(done) => done,
                Err(CreateError::Limit(message)) => {
                    return Err(invalid_with_message(&message));
                }
                // `rotate_api_key` catches only `KeyLimitReached`; any other `ValueError`
                // escapes as the 500 catch-all.
                Err(CreateError::Value(message)) => {
                    logging::error("baskfy_api.app", "unhandled error", &[("error", &message)]);
                    return Err(crate::problems::internal_error());
                }
                Err(CreateError::Problem(p)) => return Err(p),
            };
            sqlx::query(
                "UPDATE api_key SET revoked_at = $1, revoked_reason = 'rotated' WHERE id = $2",
            )
            .bind(now_micro())
            .bind(old.id)
            .execute(&mut **tx)
            .await
            .map_err(dbw)?;
            Ok(issued)
        })
    })
    .await?;
    Ok(issued(&key, secret, StatusCode::OK))
}

/// `ApiKeyRevokeIn`.
const REVOKE: [Field; 1] = [field(
    "reason",
    Kind::Str {
        min: None,
        max: Some(200),
    },
    Presence::Defaulted,
)];

/// `POST /keys/{public_id}/revoke` (`revoke_api_key`): idempotent.
///
/// # Errors
/// 400 (body), 401, 404, 500.
pub async fn revoke_api_key(
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(public_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let received = read_body(request).await?;
    let principal = require_authenticated(&principal)?;
    let values = validate(&received, &REVOKE)?;
    let reason = match &values[0] {
        Val::Str(s) => s.clone(),
        _ => "revoked".to_owned(),
    };
    let user_id = principal.require_user()?;
    let pool = write_pool(write)?;
    let key = session::run(&pool, move |tx| {
        Box::pin(async move {
            let mut key = load(tx, user_id, &public_id).await?;
            if key.revoked_at.is_none() {
                let now = now_micro();
                sqlx::query(
                    "UPDATE api_key SET revoked_at = $1, revoked_reason = $2 WHERE id = $3",
                )
                .bind(now)
                .bind(&reason)
                .bind(key.id)
                .execute(&mut **tx)
                .await
                .map_err(dbw)?;
                key.revoked_at = Some(now);
                key.revoked_reason = Some(reason.clone());
                logging::info(
                    "baskfy_api.api_keys",
                    "api key revoked",
                    &[("key_public_id", &key.public_id), ("reason", &reason)],
                );
            }
            Ok(key)
        })
    })
    .await?;
    Ok(wire::json(&key.out(utc_now())))
}

/// `DELETE /keys/{public_id}` (`delete_api_key`): only a revoked (or expired) key; 204.
///
/// # Errors
/// 400 (still active), 401, 404, 500 (a key another key was rotated from: the foreign key).
pub async fn delete_api_key(
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(public_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let user_id = principal.require_user()?;
    let pool = write_pool(write)?;
    session::run(&pool, move |tx| {
        Box::pin(async move {
            let key = load(tx, user_id, &public_id).await?;
            if key.active(utc_now()) {
                return Err(invalid_with_message("Revoke the key before deleting it."));
            }
            sqlx::query("DELETE FROM api_key WHERE id = $1")
                .bind(key.id)
                .execute(&mut **tx)
                .await
                .map_err(dbw)?;
            Ok(())
        })
    })
    .await?;
    Ok(StatusCode::NO_CONTENT.into_response())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn scopes_normalise_as_the_registry_orders_them() {
        // test_api_keys.py: omitted means every scope; order is the enum's, not the request's.
        assert_eq!(normalise_scopes(None).unwrap(), SCOPES.to_vec());
        let asked = vec!["meta:read".to_owned(), "screens:read".to_owned()];
        assert_eq!(
            normalise_scopes(Some(&asked)).unwrap(),
            vec!["screens:read", "meta:read"]
        );
        let bad = vec!["screens:write".to_owned()];
        assert_eq!(
            normalise_scopes(Some(&bad)).unwrap_err(),
            "unknown scope 'screens:write'; known scopes are screens:read, factors:read, \
             breadth:read, meta:read"
        );
        assert!(normalise_scopes(Some(&[])).is_err());
    }

    #[test]
    fn every_scope_is_a_read() {
        // packages/core/tests/test_public_api_policy.py: no member that is not a read.
        assert!(SCOPES.iter().all(|s| s.ends_with(":read")));
    }
}
