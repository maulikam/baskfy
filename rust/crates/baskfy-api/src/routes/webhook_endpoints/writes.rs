//! The `/webhook-endpoints` **writes** of `baskfy_api.routers.webhook_endpoints`, with
//! `baskfy_api.webhooks`' `master_secret`, `signing_secret` and the SSRF guard ([`super::ssrf`]):
//!
//! * `POST /webhook-endpoints` (201) — register a URL for a screen the caller can see; answers the
//!   signing secret;
//! * `PATCH /webhook-endpoints/{public_id}` — URL (re-checked), events, `is_active` (re-enabling
//!   clears the circuit breaker);
//! * `POST /webhook-endpoints/{public_id}/rotate-secret` — `secret_version + 1`, the new secret;
//! * `DELETE /webhook-endpoints/{public_id}` (204).
//!
//! The signing secret is **derived, never stored**: `"whsec_" + HMAC-SHA256(master,
//! "<public_id>:<version>")[:32]`, the master being `BASKFY_WEBHOOK_SIGNING_SECRET` or the JWT
//! secret. Registering a URL makes no request to it (delivery is the worker's). Nothing here can
//! place, modify or cancel an order (law 2).

use axum::extract::{Extension, Path, Request, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use baskfy_db::{OrderedJson, row};
use chrono::{DateTime, Utc};
use hmac::{Hmac, Mac};
use serde::Serialize;
use sha2::Sha256;
use sqlx::Row;

use super::ssrf::{Refusal, check_url};
use super::{KNOWN_EVENTS, WebhookEndpointOut};
use crate::auth::{Principal, require_authenticated};
use crate::logging;
use crate::problems::{
    ErrorEntry, Problem, bad_request, internal_error, not_found, pipeline_degraded,
};
use crate::routes::api_keys::model::{
    Field, Kind, Presence, dbw, field, read_body, validate, write_pool,
};
use crate::security::token_hex;
use crate::session::{self, Tx, WritePool};
use crate::settings::Settings;
use crate::state::AppState;
use crate::wire;

/// `MAX_ENDPOINTS_PER_ACCOUNT`.
pub const MAX_ENDPOINTS_PER_ACCOUNT: usize = 10;
/// `PUBLIC_ID_BYTES`.
const PUBLIC_ID_BYTES: usize = 12;
/// `SECRET_DISPLAY_PREFIX`.
const SECRET_DISPLAY_PREFIX: &str = "whsec_";
/// `SECRET_HEX_LENGTH`.
const SECRET_HEX_LENGTH: usize = 32;

/// `master_secret(settings)`, or `None` (`WebhookNotConfigured`).
fn master_secret(settings: &Settings) -> Option<&str> {
    let signing = settings.webhook_signing_secret.expose();
    let master = if signing.is_empty() {
        settings.jwt_secret.expose()
    } else {
        signing
    };
    (!master.is_empty()).then_some(master)
}

/// `signing_secret(settings, public_id, secret_version)`.
fn signing_secret(master: &str, public_id: &str, version: i64) -> Option<String> {
    let mut mac = Hmac::<Sha256>::new_from_slice(master.as_bytes()).ok()?;
    mac.update(format!("{public_id}:{version}").as_bytes());
    let hex: String = mac
        .finalize()
        .into_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    Some(format!(
        "{SECRET_DISPLAY_PREFIX}{}",
        &hex[..SECRET_HEX_LENGTH]
    ))
}

const NOT_CONFIGURED: &str = "BASKFY_WEBHOOK_SIGNING_SECRET (or BASKFY_JWT_SECRET) is empty; \
                              webhooks cannot be signed, so no endpoint can be created.";

/// `_bad_request(detail)`: `bad-request` with the detail as the one error.
fn refuse(detail: &str) -> Problem {
    let errors: Vec<ErrorEntry> = vec![vec![(
        "message".to_owned(),
        OrderedJson::Str(detail.to_owned()),
    )]];
    bad_request(detail, Some(errors))
}

fn refusal(r: Refusal) -> Problem {
    match r {
        Refusal::BadRequest(detail) => refuse(&detail),
        Refusal::Unhandled(reason) => {
            logging::error("baskfy_api.app", "unhandled error", &[("error", &reason)]);
            internal_error()
        }
    }
}

/// An endpoint row and its screen.
#[derive(Debug, Clone)]
struct EndpointRow {
    id: i64,
    public_id: String,
    url: String,
    events: Vec<String>,
    secret_version: i64,
    is_active: bool,
    consecutive_failures: i64,
    disabled_at: Option<DateTime<Utc>>,
    disabled_reason: Option<String>,
    last_delivery_at: Option<DateTime<Utc>>,
    created_at: DateTime<Utc>,
    screen_public_id: String,
    screen_name: String,
}

impl EndpointRow {
    /// `_out(endpoint, screen)`: unknown events dropped.
    fn out(&self) -> WebhookEndpointOut {
        WebhookEndpointOut {
            public_id: self.public_id.clone(),
            screen_public_id: self.screen_public_id.clone(),
            screen_name: self.screen_name.clone(),
            url: self.url.clone(),
            events: self
                .events
                .iter()
                .filter(|e| KNOWN_EVENTS.contains(&e.as_str()))
                .map(|e| {
                    if e == "screen.entries" {
                        "screen.entries"
                    } else {
                        "screen.exits"
                    }
                })
                .collect(),
            secret_version: self.secret_version,
            is_active: self.is_active,
            consecutive_failures: self.consecutive_failures,
            disabled_at: self.disabled_at,
            disabled_reason: self.disabled_reason.clone(),
            last_delivery_at: self.last_delivery_at,
            created_at: self.created_at,
        }
    }
}

fn events_of(value: OrderedJson) -> Vec<String> {
    match value {
        OrderedJson::Array(items) => items
            .into_iter()
            .filter_map(|i| match i {
                OrderedJson::Str(s) => Some(s),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    }
}

fn events_json(events: &[String]) -> serde_json::Value {
    serde_json::Value::Array(
        events
            .iter()
            .map(|e| serde_json::Value::String(e.clone()))
            .collect(),
    )
}

/// `_load(session, principal, public_id)`, with its screen.
async fn load(tx: &mut Tx, user_id: i64, public_id: &str) -> Result<EndpointRow, Problem> {
    let found = sqlx::query(
        "SELECT e.id, e.public_id, e.url, e.events, e.secret_version::bigint AS secret_version, \
         e.is_active, e.consecutive_failures::bigint AS consecutive_failures, e.disabled_at, \
         e.disabled_reason, e.last_delivery_at, e.created_at, s.public_id AS screen_public_id, \
         s.name AS screen_name FROM webhook_endpoint e JOIN screen s ON s.id = e.screen_id \
         WHERE e.user_id = $1 AND e.public_id = $2",
    )
    .bind(user_id)
    .bind(public_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(dbw)?;
    let Some(r) = found else {
        return Err(not_found("webhook endpoint", public_id));
    };
    let dbe = |e: baskfy_db::DbError| crate::db_failure(&e);
    Ok(EndpointRow {
        id: r.try_get("id").map_err(dbw)?,
        public_id: r.try_get("public_id").map_err(dbw)?,
        url: r.try_get("url").map_err(dbw)?,
        events: events_of(row::jsonb(&r, "events").map_err(dbe)?),
        secret_version: r.try_get("secret_version").map_err(dbw)?,
        is_active: r.try_get("is_active").map_err(dbw)?,
        consecutive_failures: r.try_get("consecutive_failures").map_err(dbw)?,
        disabled_at: row::opt_timestamptz(&r, "disabled_at").map_err(dbe)?,
        disabled_reason: r.try_get("disabled_reason").map_err(dbw)?,
        last_delivery_at: row::opt_timestamptz(&r, "last_delivery_at").map_err(dbe)?,
        created_at: row::timestamptz(&r, "created_at").map_err(dbe)?,
        screen_public_id: r.try_get("screen_public_id").map_err(dbw)?,
        screen_name: r.try_get("screen_name").map_err(dbw)?,
    })
}

/// `WebhookEndpointWithSecretOut`.
#[derive(Debug, Clone, Serialize)]
struct WithSecretOut {
    endpoint: WebhookEndpointOut,
    signing_secret: String,
}

/// `WebhookEventName`.
const EVENTS: &[&str] = &["screen.entries", "screen.exits"];

/// `WebhookEndpointCreate`.
const CREATE: [Field; 3] = [
    field(
        "screen_public_id",
        Kind::Str {
            min: None,
            max: None,
        },
        Presence::Required,
    ),
    field(
        "url",
        Kind::Str {
            min: Some(8),
            max: Some(2000),
        },
        Presence::Required,
    ),
    field("events", Kind::LiteralList(EVENTS), Presence::Defaulted),
];

/// `POST /webhook-endpoints` (`create_endpoint`): 201 with the signing secret.
///
/// # Errors
/// 400 (body; the URL; no events; ten endpoints; a duplicate target), 401, 404 (a screen the
/// caller cannot see), 503 (no master secret), 500.
pub async fn create_endpoint(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let received = read_body(request).await?;
    let principal = require_authenticated(&principal)?;
    let values = validate(&received, &CREATE)?;
    let user_id = principal.require_user()?;
    let Some(master) = master_secret(&state.settings).map(str::to_owned) else {
        return Err(pipeline_degraded(NOT_CONFIGURED));
    };
    let screen_public_id = values[0].str().unwrap_or_default().to_owned();
    let url = values[1].str().unwrap_or_default().to_owned();
    check_url(&url, &state.settings.environment)
        .await
        .map_err(refusal)?;
    let events: Vec<String> = match values[2].list() {
        Some(e) => e.to_vec(),
        None => EVENTS.iter().map(|e| (*e).to_owned()).collect(),
    };
    if events.is_empty() {
        return Err(refuse("An endpoint with no events would never fire."));
    }
    let pool = write_pool(write)?;
    let endpoint = session::run(&pool, move |tx| {
        Box::pin(async move {
            let screen =
                sqlx::query("SELECT id, user_id, public_id, name FROM screen WHERE public_id = $1")
                    .bind(&screen_public_id)
                    .fetch_optional(&mut **tx)
                    .await
                    .map_err(dbw)?;
            let Some(screen) = screen else {
                return Err(not_found("screen", &screen_public_id));
            };
            let owner: Option<i64> = screen.try_get("user_id").map_err(dbw)?;
            if owner.is_some_and(|o| o != user_id) {
                return Err(not_found("screen", &screen_public_id));
            }
            let screen_id: i64 = screen.try_get("id").map_err(dbw)?;
            let existing =
                sqlx::query("SELECT screen_id, url FROM webhook_endpoint WHERE user_id = $1")
                    .bind(user_id)
                    .fetch_all(&mut **tx)
                    .await
                    .map_err(dbw)?;
            if existing.len() >= MAX_ENDPOINTS_PER_ACCOUNT {
                return Err(refuse(&format!(
                    "This account already has {MAX_ENDPOINTS_PER_ACCOUNT} webhook endpoints."
                )));
            }
            for row in &existing {
                let sid: i64 = row.try_get("screen_id").map_err(dbw)?;
                let u: String = row.try_get("url").map_err(dbw)?;
                if sid == screen_id && u == url {
                    return Err(refuse("That screen already posts to that URL."));
                }
            }
            let public_id = token_hex(PUBLIC_ID_BYTES);
            let inserted = sqlx::query(
                "INSERT INTO webhook_endpoint (public_id, user_id, screen_id, url, \
                 secret_version, events, is_active, consecutive_failures) \
                 VALUES ($1, $2, $3, $4, 1, $5, true, 0) RETURNING id, created_at",
            )
            .bind(&public_id)
            .bind(user_id)
            .bind(screen_id)
            .bind(&url)
            .bind(sqlx::types::Json(events_json(&events)))
            .fetch_one(&mut **tx)
            .await
            .map_err(dbw)?;
            Ok(EndpointRow {
                id: inserted.try_get("id").map_err(dbw)?,
                public_id,
                url,
                events,
                secret_version: 1,
                is_active: true,
                consecutive_failures: 0,
                disabled_at: None,
                disabled_reason: None,
                last_delivery_at: None,
                created_at: row::timestamptz(&inserted, "created_at")
                    .map_err(|e| crate::db_failure(&e))?,
                screen_public_id: screen.try_get("public_id").map_err(dbw)?,
                screen_name: screen.try_get("name").map_err(dbw)?,
            })
        })
    })
    .await?;
    with_secret(&master, &endpoint, StatusCode::CREATED)
}

fn with_secret(master: &str, e: &EndpointRow, status: StatusCode) -> Result<Response, Problem> {
    let secret =
        signing_secret(master, &e.public_id, e.secret_version).ok_or_else(internal_error)?;
    Ok(wire::json_status(
        status,
        &WithSecretOut {
            endpoint: e.out(),
            signing_secret: secret,
        },
    ))
}

/// `WebhookEndpointUpdate`.
const UPDATE: [Field; 3] = [
    field(
        "url",
        Kind::Str {
            min: Some(8),
            max: Some(2000),
        },
        Presence::Optional,
    ),
    field("events", Kind::LiteralList(EVENTS), Presence::Optional),
    field("is_active", Kind::Bool, Presence::Optional),
];

/// `PATCH /webhook-endpoints/{public_id}` (`update_endpoint`).
///
/// # Errors
/// 400 (body; the URL; no events), 401, 404, 500 (a URL that collides with another endpoint of
/// the account on the same screen: the unique constraint).
pub async fn update_endpoint(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(public_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let received = read_body(request).await?;
    let principal = require_authenticated(&principal)?;
    let values = validate(&received, &UPDATE)?;
    let user_id = principal.require_user()?;
    let url = values[0].str().map(str::to_owned);
    let events = values[1].list().map(<[String]>::to_vec);
    let is_active = values[2].bool();
    let environment = state.settings.environment.clone();
    let pool = write_pool(write)?;
    let endpoint = session::run(&pool, move |tx| {
        Box::pin(async move {
            let mut e = load(tx, user_id, &public_id).await?;
            let before = e.clone();
            if let Some(url) = url {
                check_url(&url, &environment).await.map_err(refusal)?;
                e.url = url;
            }
            if let Some(events) = events {
                if events.is_empty() {
                    return Err(refuse("An endpoint with no events would never fire."));
                }
                e.events = events;
            }
            if let Some(active) = is_active {
                e.is_active = active;
                if active {
                    e.consecutive_failures = 0;
                    e.disabled_at = None;
                    e.disabled_reason = None;
                }
            }
            // Net changes only, and `updated_at` only with them (the ORM's flush).
            let mut sets: Vec<String> = Vec::new();
            let url_changed = e.url != before.url;
            let events_changed = e.events != before.events;
            let active_changed = e.is_active != before.is_active;
            let failures_changed = e.consecutive_failures != before.consecutive_failures;
            let disabled_changed = e.disabled_at != before.disabled_at;
            let reason_changed = e.disabled_reason != before.disabled_reason;
            let mut n = 1;
            for (changed, col) in [
                (url_changed, "url"),
                (events_changed, "events"),
                (active_changed, "is_active"),
                (failures_changed, "consecutive_failures"),
                (disabled_changed, "disabled_at"),
                (reason_changed, "disabled_reason"),
            ] {
                if changed {
                    n += 1;
                    sets.push(format!("{col} = ${n}"));
                }
            }
            if !sets.is_empty() {
                let sql = format!(
                    "UPDATE webhook_endpoint SET {}, updated_at = now() WHERE id = $1",
                    sets.join(", ")
                );
                let mut q = sqlx::query(&sql).bind(e.id);
                if url_changed {
                    q = q.bind(e.url.clone());
                }
                if events_changed {
                    q = q.bind(sqlx::types::Json(events_json(&e.events)));
                }
                if active_changed {
                    q = q.bind(e.is_active);
                }
                if failures_changed {
                    q = q.bind(i32::try_from(e.consecutive_failures).unwrap_or(0));
                }
                if disabled_changed {
                    q = q.bind(e.disabled_at);
                }
                if reason_changed {
                    q = q.bind(e.disabled_reason.clone());
                }
                q.execute(&mut **tx).await.map_err(dbw)?;
            }
            Ok(e)
        })
    })
    .await?;
    Ok(wire::json(&endpoint.out()))
}

/// `POST /webhook-endpoints/{public_id}/rotate-secret` (`rotate_secret`).
///
/// # Errors
/// 401, 404, 500.
pub async fn rotate_secret(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(public_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let user_id = principal.require_user()?;
    let master = master_secret(&state.settings).map(str::to_owned);
    let pool = write_pool(write)?;
    let (endpoint, master) = session::run(&pool, move |tx| {
        Box::pin(async move {
            let mut e = load(tx, user_id, &public_id).await?;
            e.secret_version += 1;
            sqlx::query(
                "UPDATE webhook_endpoint SET secret_version = $2, updated_at = now() WHERE id = $1",
            )
            .bind(e.id)
            .bind(i32::try_from(e.secret_version).map_err(|_| internal_error())?)
            .execute(&mut **tx)
            .await
            .map_err(dbw)?;
            // `signing_secret` raises `WebhookNotConfigured` after the flush: the 500, and the
            // bump rolls back.
            let Some(master) = master else {
                logging::error(
                    "baskfy_api.app",
                    "unhandled error",
                    &[("error", NOT_CONFIGURED)],
                );
                return Err(internal_error());
            };
            Ok((e, master))
        })
    })
    .await?;
    with_secret(&master, &endpoint, StatusCode::OK)
}

/// `DELETE /webhook-endpoints/{public_id}` (`delete_endpoint`): 204.
///
/// # Errors
/// 401, 404, 500.
pub async fn delete_endpoint(
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(public_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let user_id = principal.require_user()?;
    let pool = write_pool(write)?;
    session::run(&pool, move |tx| {
        Box::pin(async move {
            let e = load(tx, user_id, &public_id).await?;
            sqlx::query("DELETE FROM webhook_endpoint WHERE id = $1")
                .bind(e.id)
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
    fn the_signing_secret_is_derived_and_versioned() {
        // test_api_webhooks.py: whsec_ + 32 hex, deterministic, and a rotation changes it.
        let one = signing_secret("master", "abc", 1).unwrap();
        // "whsec_" + hmac.new(b"master", b"abc:1", sha256).hexdigest()[:32]
        assert_eq!(one, "whsec_975f7b5ce23cd8adcea8838e49b8622a");
        assert_eq!(one, signing_secret("master", "abc", 1).unwrap());
        assert_ne!(one, signing_secret("master", "abc", 2).unwrap());
    }
}
