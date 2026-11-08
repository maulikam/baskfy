//! The account and session **writes** of `baskfy_api.routers.auth` (wave 4c's foundation,
//! DECISIONS-RS R6.3): `POST /auth/google`, `POST /auth/refresh`, `POST /auth/logout`,
//! `PATCH /me`, `DELETE /me`, `POST /me/restore`, with `auth_service`'s session and erasure logic.
//!
//! Each runs in one write transaction ([`crate::session::run`]) that commits only when the
//! handler returns `Ok` — so, exactly as in Python, a 401 that follows a flushed change rolls the
//! change back: `rotate_refresh`'s reuse revocation and its expiry stamp do not persist (R6.4,
//! recorded for Maulik). Nothing here places, modifies or cancels an order.
//!
//! `POST /auth/google` is ported up to the verifier: body validation, the uniform 401, and the
//! "not configured" refusal are Python's. **Google's RS256 verification is not** (it needs the
//! JWKS over the network and an RSA verifier — R6.3), so with `BASKFY_GOOGLE_CLIENT_ID` set the
//! Rust route refuses every token with the same uniform 401 and a log line saying why; the route
//! stays on Python wherever sign-in is configured (a cutover gate).

use axum::extract::{Extension, Request, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use chrono::{DateTime, Duration, Timelike, Utc};
use serde::Serialize;
use sqlx::Row;

use super::{MeRow, me_payload};
use crate::auth::{Principal, require_authenticated};
use crate::body::{FieldKind, FieldValue, receive, validate_model};
use crate::csrf::{self, REFRESH_COOKIE};
use crate::entitlements::{entitlements_for, utc_now};
use crate::logging;
use crate::mailer::{Mailer, Message};
use crate::problems::{
    ErrorEntry, Problem, ProblemType, bad_request, internal_error, unauthenticated,
};
use crate::security::{digest, new_opaque_token, token_hex};
use crate::session::{self, Tx, WritePool};
use crate::state::AppState;
use crate::wire;
use baskfy_db::OrderedJson;

/// `RESTORE_PURPOSE`.
const RESTORE_PURPOSE: &str = "account-restore";
/// The largest body read (Starlette reads it whole; this bounds memory, far above any model here).
const MAX_BODY_BYTES: usize = 1 << 20;

fn db(e: sqlx::Error) -> Problem {
    crate::db_failure(&e.into())
}

/// `datetime.now(tz=UTC)`: microsecond precision, as Python's clock and Postgres's column hold it.
fn now() -> DateTime<Utc> {
    let t = utc_now();
    t.with_nanosecond(t.nanosecond() / 1000 * 1000).unwrap_or(t)
}

/// The request's body, read whole (`await request.body()`), and its `Content-Type`.
async fn read_body(request: Request) -> Result<(Option<String>, Vec<u8>, HeaderMap), Problem> {
    let headers = request.headers().clone();
    let content_type = headers
        .get(header::CONTENT_TYPE)
        .map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned());
    let bytes = axum::body::to_bytes(request.into_body(), MAX_BODY_BYTES)
        .await
        .map_err(|_| crate::problems::framework(400, "There was an error parsing the body"))?;
    Ok((content_type, bytes.to_vec(), headers))
}

fn header_str(headers: &HeaderMap, name: &str) -> Option<String> {
    headers
        .get(name)
        .map(|v| v.as_bytes().iter().map(|&b| char::from(b)).collect())
}

fn with_cookies(mut response: Response, cookies: &[String]) -> Response {
    for c in cookies {
        if let Ok(v) = HeaderValue::from_str(c) {
            response.headers_mut().append(header::SET_COOKIE, v);
        }
    }
    response
}

/// `normalise_email(email)`: `strip().lower()`.
fn normalise_email(email: &str) -> String {
    email.trim().to_lowercase()
}

/// The write pool, or the 500 a missing session gives (a state built without one).
fn pool(pool: Option<Extension<WritePool>>) -> Result<WritePool, Problem> {
    pool.map(|Extension(p)| p).ok_or_else(internal_error)
}

// ---------------------------------------------------------------------------------------------
// Sessions (auth_service)
// ---------------------------------------------------------------------------------------------

/// `SessionOut`, field for field.
#[derive(Debug, Clone, Serialize)]
struct SessionOut {
    access_token: String,
    token_type: &'static str,
    expires_in: i64,
    public_id: String,
    email: String,
    name: Option<String>,
    email_verified: bool,
    session_epoch: i64,
}

/// What `issue_session` produces.
struct Issued {
    body: SessionOut,
    refresh_token: String,
    refresh_expires_in: i64,
    csrf_token: String,
}

/// The `app_user` columns a session needs.
struct UserRow {
    id: i64,
    public_id: String,
    email: String,
    name: Option<String>,
    email_verified: bool,
    session_epoch: i64,
}

/// `issue_session(session, user, settings, family_id=)`: a refresh row in the family, and an
/// access token stamped with the account's epoch.
async fn issue_session(
    tx: &mut Tx,
    state: &AppState,
    user: &UserRow,
    family_id: Option<&str>,
) -> Result<Issued, Problem> {
    let settings = &state.settings;
    let refresh = new_opaque_token();
    let ttl = Duration::days(settings.refresh_token_ttl_days);
    let family = family_id.map_or_else(|| token_hex(16), str::to_owned);
    sqlx::query(
        "INSERT INTO refresh_token (user_id, family_id, token_hash, expires_at) \
         VALUES ($1, $2, $3, $4)",
    )
    .bind(user.id)
    .bind(&family)
    .bind(digest(&refresh))
    .bind(now() + ttl)
    .execute(&mut **tx)
    .await
    .map_err(db)?;
    let issued_at = now().timestamp();
    let access = crate::auth::encode_token_full(
        &user.public_id,
        settings.jwt_secret.expose().as_bytes(),
        issued_at,
        settings.access_token_ttl_seconds,
        settings.jwt_audience.as_deref(),
        settings.jwt_issuer.as_deref(),
        user.session_epoch,
    );
    Ok(Issued {
        body: SessionOut {
            access_token: access,
            token_type: "Bearer",
            expires_in: settings.access_token_ttl_seconds,
            public_id: user.public_id.clone(),
            email: user.email.clone(),
            name: user.name.clone(),
            email_verified: user.email_verified,
            session_epoch: user.session_epoch,
        },
        refresh_token: refresh,
        refresh_expires_in: ttl.num_seconds(),
        csrf_token: new_opaque_token(),
    })
}

/// `_revoke_where(session, predicate, reason)` for `family_id = $1` or `user_id = $1`.
async fn revoke_where(
    tx: &mut Tx,
    column: &str,
    value: Bind<'_>,
    reason: &str,
) -> Result<usize, Problem> {
    let sql = format!(
        "UPDATE refresh_token SET revoked_at = $2, revoked_reason = $3 \
         WHERE {column} = $1 AND revoked_at IS NULL RETURNING id"
    );
    let q = sqlx::query(&sql);
    let q = match value {
        Bind::Text(s) => q.bind(s.to_owned()),
        Bind::Id(i) => q.bind(i),
    };
    Ok(q.bind(now())
        .bind(reason)
        .fetch_all(&mut **tx)
        .await
        .map_err(db)?
        .len())
}

/// A predicate's value.
enum Bind<'a> {
    Text(&'a str),
    Id(i64),
}

/// `revoke_all_for_user(session, user_id, reason)`: bump `session_epoch` (every web session
/// issued before dies at its next check), then revoke every live refresh token. The new epoch.
async fn revoke_all_for_user(tx: &mut Tx, user_id: i64, reason: &str) -> Result<i64, Problem> {
    let epoch: i64 = sqlx::query_scalar(
        "UPDATE app_user SET session_epoch = session_epoch + 1 WHERE id = $1 \
         RETURNING session_epoch::bigint",
    )
    .bind(user_id)
    .fetch_one(&mut **tx)
    .await
    .map_err(db)?;
    revoke_where(tx, "user_id", Bind::Id(user_id), reason).await?;
    Ok(epoch)
}

/// Why `rotate_refresh` refused.
enum RotateError {
    /// `InvalidCredentials`.
    Invalid,
    /// `TokenReused`.
    Reused,
    /// A database failure.
    Db(Problem),
}

async fn user_for_session(tx: &mut Tx, user_id: i64) -> Result<Option<UserRow>, Problem> {
    let row = sqlx::query(
        "SELECT id, public_id, email::text AS email, name, email_verified_at IS NOT NULL AS v, \
         session_epoch::bigint AS e FROM app_user WHERE id = $1 AND deleted_at IS NULL",
    )
    .bind(user_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(db)?;
    row.map(|r| {
        Ok(UserRow {
            id: r.try_get("id").map_err(db)?,
            public_id: r.try_get("public_id").map_err(db)?,
            email: r.try_get("email").map_err(db)?,
            name: r.try_get("name").map_err(db)?,
            email_verified: r.try_get("v").map_err(db)?,
            session_epoch: r.try_get("e").map_err(db)?,
        })
    })
    .transpose()
}

/// `rotate_refresh(session, supplied, settings)`.
async fn rotate_refresh(
    tx: &mut Tx,
    state: &AppState,
    supplied: &str,
) -> Result<(Issued, String), RotateError> {
    let rows = sqlx::query(
        "SELECT id, user_id, family_id, expires_at, revoked_at FROM refresh_token \
         WHERE token_hash = $1",
    )
    .bind(digest(supplied))
    .fetch_all(&mut **tx)
    .await
    .map_err(|e| RotateError::Db(db(e)))?;
    if rows.len() > 1 {
        return Err(RotateError::Db(crate::db_failure(
            &baskfy_db::DbError::Query {
                sqlstate: None,
                message: "MultipleResultsFound: refresh_token".to_owned(),
            },
        )));
    }
    let Some(row) = rows.first() else {
        return Err(RotateError::Invalid);
    };
    let id: i64 = row.try_get("id").map_err(|e| RotateError::Db(db(e)))?;
    let user_id: i64 = row.try_get("user_id").map_err(|e| RotateError::Db(db(e)))?;
    let family: String = row
        .try_get("family_id")
        .map_err(|e| RotateError::Db(db(e)))?;
    let expires_at: DateTime<Utc> = row
        .try_get("expires_at")
        .map_err(|e| RotateError::Db(db(e)))?;
    let revoked_at: Option<DateTime<Utc>> = row
        .try_get("revoked_at")
        .map_err(|e| RotateError::Db(db(e)))?;
    if revoked_at.is_some() {
        let revoked = revoke_where(tx, "family_id", Bind::Text(&family), "reuse")
            .await
            .map_err(RotateError::Db)?;
        logging::warning(
            "baskfy_api.auth_service",
            "refresh token reuse detected",
            &[
                ("family_id", &family),
                ("user_id", &user_id.to_string()),
                ("revoked", &revoked.to_string()),
            ],
        );
        return Err(RotateError::Reused);
    }
    if expires_at <= now() {
        sqlx::query(
            "UPDATE refresh_token SET revoked_at = $2, revoked_reason = 'expired' WHERE id = $1",
        )
        .bind(id)
        .bind(now())
        .execute(&mut **tx)
        .await
        .map_err(|e| RotateError::Db(db(e)))?;
        return Err(RotateError::Invalid);
    }
    let Some(user) = user_for_session(tx, user_id)
        .await
        .map_err(RotateError::Db)?
    else {
        revoke_where(tx, "family_id", Bind::Text(&family), "deleted")
            .await
            .map_err(RotateError::Db)?;
        return Err(RotateError::Invalid);
    };
    let issued = issue_session(tx, state, &user, Some(&family))
        .await
        .map_err(RotateError::Db)?;
    let successor: i64 = sqlx::query_scalar("SELECT id FROM refresh_token WHERE token_hash = $1")
        .bind(digest(&issued.refresh_token))
        .fetch_one(&mut **tx)
        .await
        .map_err(|e| RotateError::Db(db(e)))?;
    sqlx::query(
        "UPDATE refresh_token SET revoked_at = $2, revoked_reason = 'rotated', \
         replaced_by_id = $3 WHERE id = $1",
    )
    .bind(id)
    .bind(now())
    .bind(successor)
    .execute(&mut **tx)
    .await
    .map_err(|e| RotateError::Db(db(e)))?;
    Ok((issued, user.email))
}

/// `_session_response(response, issued, settings)`: the cookies and the body.
fn session_response(state: &AppState, issued: &Issued) -> Response {
    let cookies = csrf::set_auth_cookies(
        &issued.refresh_token,
        &issued.csrf_token,
        issued.refresh_expires_in,
        &state.settings,
    );
    with_cookies(wire::json(&issued.body), &cookies)
}

// ---------------------------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------------------------

/// `POST /auth/google` (`sign_in_with_google`). See the module docs for what is not ported.
///
/// # Errors
/// 400 for a body that does not validate; the uniform 401 otherwise.
pub async fn sign_in_with_google(
    State(state): State<AppState>,
    request: Request,
) -> Result<Response, Problem> {
    let (content_type, bytes, _) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    let values = validate_model(
        &received,
        &[(
            "id_token",
            FieldKind::Str {
                min: Some(1),
                max: Some(8192),
            },
        )],
    )?;
    let token = values
        .first()
        .and_then(FieldValue::as_str)
        .unwrap_or_default();
    let reason = if state.settings.google_client_id.is_empty() {
        "Google sign-in is not configured"
    } else if token.is_empty() {
        "no token supplied"
    } else {
        "token rejected: the RS256 verifier is not ported to Rust (DECISIONS-RS R6.3)"
    };
    logging::warning(
        "baskfy_api.routers.auth",
        "google sign-in rejected",
        &[("reason", reason)],
    );
    Err(unauthenticated(
        "That Google sign-in could not be verified.",
    ))
}

/// `POST /auth/refresh` (`refresh`): CSRF, then rotate the presented refresh token.
///
/// # Errors
/// 401 for a missing CSRF token, a missing/unknown/expired/reused token, a vanished account or an
/// address no longer allowed; the 500 catch-all on a database failure.
pub async fn refresh(
    State(state): State<AppState>,
    write: Option<Extension<WritePool>>,
    headers: HeaderMap,
) -> Result<Response, Problem> {
    let cookie_header = header_str(&headers, "cookie");
    csrf::require_csrf(
        cookie_header.as_deref(),
        header_str(&headers, "x-csrf-token").as_deref(),
    )?;
    let supplied = csrf::cookie(cookie_header.as_deref(), REFRESH_COOKIE).unwrap_or_default();
    if supplied.is_empty() {
        return Err(unauthenticated("No refresh token was presented."));
    }
    let pool = pool(write)?;
    let st = state.clone();
    session::run(&pool, move |tx| {
        Box::pin(async move {
            let (issued, email) = match rotate_refresh(tx, &st, &supplied).await {
                Ok(v) => v,
                Err(RotateError::Reused) => {
                    return Err(unauthenticated(
                        "That session has been ended for security reasons. Please sign in again.",
                    ));
                }
                Err(RotateError::Invalid) => {
                    return Err(unauthenticated("That refresh token is not valid."));
                }
                Err(RotateError::Db(p)) => return Err(p),
            };
            if !st.settings.login_permitted(&email) {
                logging::warning(
                    "baskfy_api.routers.auth",
                    "refresh refused by allowlist",
                    &[("reason", "not in allowlist")],
                );
                return Err(unauthenticated("That refresh token is not valid."));
            }
            Ok(session_response(&st, &issued))
        })
    })
    .await
}

/// `POST /auth/logout` (`logout`): idempotent, never an error once the CSRF check passes.
///
/// # Errors
/// 401 for a missing CSRF token; the 500 catch-all on a database failure.
pub async fn logout(
    State(state): State<AppState>,
    write: Option<Extension<WritePool>>,
    headers: HeaderMap,
) -> Result<Response, Problem> {
    let cookie_header = header_str(&headers, "cookie");
    csrf::require_csrf(
        cookie_header.as_deref(),
        header_str(&headers, "x-csrf-token").as_deref(),
    )?;
    let supplied = csrf::cookie(cookie_header.as_deref(), REFRESH_COOKIE).unwrap_or_default();
    let pool = pool(write)?;
    session::run(&pool, move |tx| {
        Box::pin(async move {
            if !supplied.is_empty() {
                sqlx::query(
                    "UPDATE refresh_token SET revoked_at = $2, revoked_reason = 'logout' \
                     WHERE token_hash = $1 AND revoked_at IS NULL",
                )
                .bind(digest(&supplied))
                .bind(now())
                .execute(&mut **tx)
                .await
                .map_err(db)?;
            }
            Ok(())
        })
    })
    .await?;
    // `Response(status_code=204)`: no body, no content type, no length.
    let response = StatusCode::NO_CONTENT.into_response();
    Ok(with_cookies(
        response,
        &csrf::clear_auth_cookies(&state.settings),
    ))
}

/// `_load_me` on the write session.
async fn load_me_tx(tx: &mut Tx, user_id: i64) -> Result<MeRow, Problem> {
    let user = sqlx::query(
        "SELECT public_id, email::text AS email, name, email_verified_at, created_at, is_staff, \
         session_epoch::bigint AS session_epoch, deleted_at FROM app_user WHERE id = $1",
    )
    .bind(user_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(db)?
    .ok_or_else(|| unauthenticated("That token refers to an account that no longer exists."))?;
    Ok(MeRow {
        public_id: user.try_get("public_id").map_err(db)?,
        email: user.try_get("email").map_err(db)?,
        name: user.try_get("name").map_err(db)?,
        email_verified_at: user.try_get("email_verified_at").map_err(db)?,
        created_at: user.try_get("created_at").map_err(db)?,
        is_staff: user.try_get("is_staff").map_err(db)?,
        session_epoch: user.try_get("session_epoch").map_err(db)?,
    })
}

/// `PATCH /me` (`patch_me`): only the display name.
///
/// # Errors
/// 401 (anonymous, vanished account), 400 (body), 500 (database).
pub async fn patch_me(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?.clone();
    let entitlements = entitlements_for(&state.db, &principal, &state.settings).await?;
    let (content_type, bytes, _) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    let values = validate_model(
        &received,
        &[(
            "name",
            FieldKind::OptStr {
                min: Some(1),
                max: Some(120),
            },
        )],
    )?;
    let name = values
        .first()
        .and_then(FieldValue::as_str)
        .map(str::to_owned);
    let user_id = principal.require_user()?;
    let pool = pool(write)?;
    let user = session::run(&pool, move |tx| {
        Box::pin(async move {
            let mut user = load_me_tx(tx, user_id).await?;
            if let Some(name) = name {
                // The ORM writes only a net change.
                sqlx::query(
                    "UPDATE app_user SET name = $1 WHERE id = $2 AND name IS DISTINCT FROM $1",
                )
                .bind(&name)
                .bind(user_id)
                .execute(&mut **tx)
                .await
                .map_err(db)?;
                user.name = Some(name);
            }
            Ok(user)
        })
    })
    .await?;
    let is_staff = user.is_staff;
    Ok(wire::json(
        &me_payload(&state, user_id, user, entitlements.as_out(), is_staff).await?,
    ))
}

/// `DeletionOut`.
#[derive(Debug, Clone, Serialize)]
struct DeletionOut {
    status: &'static str,
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    purge_after: Option<DateTime<Utc>>,
    detail: String,
}

/// `_mint_restore_code(user, settings, purge_after=)`: the signed one-shot cancel code.
fn mint_restore_code(public_id: &str, secret: &[u8], purge_after: DateTime<Utc>) -> String {
    let claims = vec![
        ("sub".to_owned(), OrderedJson::Str(public_id.to_owned())),
        (
            "purpose".to_owned(),
            OrderedJson::Str(RESTORE_PURPOSE.to_owned()),
        ),
        ("iat".to_owned(), OrderedJson::Int(now().timestamp())),
        ("exp".to_owned(), OrderedJson::Int(purge_after.timestamp())),
    ];
    crate::auth::sign_hs256(&claims, secret)
}

/// `_deletion_mail(to, days, restore_code)`.
fn deletion_mail(to: &str, days: i64, restore_code: &str) -> Message {
    let heading = "Your Baskfy account is scheduled for deletion";
    let body = [
        format!("Your account has been deactivated and will be permanently erased in {days} days."),
        "Signing in again before then cancels the deletion and restores the account.".to_owned(),
        format!("To cancel without signing in, POST /me/restore with this code: {restore_code}"),
        "After that, the data is gone and cannot be recovered.".to_owned(),
    ];
    let mut parts = vec![heading.to_owned()];
    parts.extend(body);
    Message {
        to: to.to_owned(),
        subject: format!("Your Baskfy account will be deleted in {days} days"),
        text: parts.join("\n\n"),
        html: parts.join("<br/>"),
        reply_to: None,
    }
}

/// `DELETE /me` (`delete_me`): the address retyped, then the seven-day soft delete.
///
/// # Errors
/// 401 (anonymous, vanished account), 400 (body, or not this account's address), 500.
pub async fn delete_me(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?.clone();
    let (content_type, bytes, _) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    let values = validate_model(&received, &[("email", FieldKind::Email)])?;
    let typed = values
        .first()
        .and_then(FieldValue::as_str)
        .unwrap_or_default()
        .to_owned();
    let user_id = principal.require_user()?;
    let pool = pool(write)?;
    let st = state.clone();
    let purge_after = session::run(&pool, move |tx| {
        Box::pin(async move {
            let user = load_me_tx(tx, user_id).await?;
            if normalise_email(&typed) != normalise_email(&user.email) {
                let errors: Vec<ErrorEntry> = vec![vec![
                    ("field".to_owned(), OrderedJson::Str("email".to_owned())),
                    (
                        "message".to_owned(),
                        OrderedJson::Str("does not match".to_owned()),
                    ),
                ]];
                return Err(bad_request(
                    "That is not this account's email address.",
                    Some(errors),
                ));
            }
            // request_deletion: soft-delete now, purge after the window.
            let moment = now();
            sqlx::query("UPDATE app_user SET deleted_at = $1 WHERE id = $2")
                .bind(moment)
                .bind(user_id)
                .execute(&mut **tx)
                .await
                .map_err(db)?;
            let purge_after = moment + Duration::days(st.settings.account_purge_after_days);
            let existing: Option<i64> =
                sqlx::query_scalar("SELECT user_id FROM account_deletion WHERE user_id = $1")
                    .bind(user_id)
                    .fetch_optional(&mut **tx)
                    .await
                    .map_err(db)?;
            if existing.is_none() {
                sqlx::query("INSERT INTO account_deletion (user_id, purge_after) VALUES ($1, $2)")
                    .bind(user_id)
                    .bind(purge_after)
                    .execute(&mut **tx)
                    .await
                    .map_err(db)?;
            } else {
                sqlx::query(
                    "UPDATE account_deletion SET purge_after = $2, cancelled_at = NULL \
                     WHERE user_id = $1",
                )
                .bind(user_id)
                .bind(purge_after)
                .execute(&mut **tx)
                .await
                .map_err(db)?;
            }
            revoke_all_for_user(tx, user_id, "deleted").await?;
            let code = mint_restore_code(
                &user.public_id,
                st.settings.jwt_secret.expose().as_bytes(),
                purge_after,
            );
            // Delivered before the commit, as Python awaits the mailer inside the session.
            Mailer::from_settings(&st.settings)
                .deliver(&deletion_mail(
                    &user.email,
                    st.settings.account_purge_after_days,
                    &code,
                ))
                .await;
            Ok(purge_after)
        })
    })
    .await?;
    let body = DeletionOut {
        status: "scheduled",
        purge_after: Some(purge_after),
        detail: format!(
            "Your account is deactivated and will be erased on {}. Signing in again before then \
             cancels it.",
            purge_after.date_naive().format("%Y-%m-%d")
        ),
    };
    Ok(with_cookies(
        wire::json(&body),
        &csrf::clear_auth_cookies(&state.settings),
    ))
}

/// `_restore_code_valid(code, user, settings)`.
fn restore_code_valid(code: &str, public_id: &str, secret: &[u8]) -> Result<bool, Problem> {
    let claims = match crate::auth::pyjwt_decode_requiring(
        code,
        secret,
        None,
        None,
        0.0,
        crate::auth::unix_now(),
        &["exp", "sub", "purpose"],
    ) {
        Ok(c) => c,
        Err(crate::auth::TokenError::Invalid(_)) => return Ok(false),
        Err(crate::auth::TokenError::Internal(class)) => {
            logging::error(
                "baskfy_api.app",
                "unhandled error",
                &[("error_class", class)],
            );
            return Err(internal_error());
        }
    };
    let get = |k: &str| claims.iter().find(|(key, _)| key == k).map(|(_, v)| v);
    Ok(get("sub") == Some(&OrderedJson::Str(public_id.to_owned()))
        && get("purpose") == Some(&OrderedJson::Str(RESTORE_PURPOSE.to_owned())))
}

/// `POST /me/restore` (`restore_me`): the signed code from the deletion mail cancels it.
///
/// # Errors
/// 400 (body), 401 (unknown address or a code that is not valid — one answer), 500.
pub async fn restore_me(
    State(state): State<AppState>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let (content_type, bytes, _) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    let values = validate_model(
        &received,
        &[
            ("email", FieldKind::Email),
            (
                "code",
                FieldKind::Str {
                    min: Some(20),
                    max: Some(2048),
                },
            ),
        ],
    )?;
    let email = normalise_email(
        values
            .first()
            .and_then(FieldValue::as_str)
            .unwrap_or_default(),
    );
    let code = values
        .get(1)
        .and_then(FieldValue::as_str)
        .unwrap_or_default()
        .to_owned();
    let pool = pool(write)?;
    let st = state.clone();
    let cancelled = session::run(&pool, move |tx| {
        Box::pin(async move {
            let found = sqlx::query("SELECT id, public_id FROM app_user WHERE email = $1")
                .bind(&email)
                .fetch_all(&mut **tx)
                .await
                .map_err(db)?;
            if found.len() > 1 {
                return Err(internal_error());
            }
            let user = match found.first() {
                Some(r) => Some((
                    r.try_get::<i64, _>("id").map_err(db)?,
                    r.try_get::<String, _>("public_id").map_err(db)?,
                )),
                None => None,
            };
            let valid = match &user {
                Some((_, public_id)) => restore_code_valid(
                    &code,
                    public_id,
                    st.settings.jwt_secret.expose().as_bytes(),
                )?,
                None => false,
            };
            let Some((user_id, _)) = user.filter(|_| valid) else {
                return Err(Problem::new(
                    ProblemType::Unauthenticated,
                    "That restore code is not valid.",
                ));
            };
            // cancel_deletion
            let pending: Option<i64> = sqlx::query_scalar(
                "SELECT user_id FROM account_deletion WHERE user_id = $1 \
                 AND cancelled_at IS NULL AND purged_at IS NULL",
            )
            .bind(user_id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            if pending.is_none() {
                return Ok(false);
            }
            sqlx::query("UPDATE app_user SET deleted_at = NULL WHERE id = $1")
                .bind(user_id)
                .execute(&mut **tx)
                .await
                .map_err(db)?;
            sqlx::query("UPDATE account_deletion SET cancelled_at = $2 WHERE user_id = $1")
                .bind(user_id)
                .bind(now())
                .execute(&mut **tx)
                .await
                .map_err(db)?;
            revoke_all_for_user(tx, user_id, "restored").await?;
            Ok(true)
        })
    })
    .await?;
    let detail = if cancelled {
        "The pending deletion has been cancelled."
    } else {
        "There is no pending deletion."
    };
    Ok(wire::json(&DeletionOut {
        status: "cancelled",
        purge_after: None,
        detail: detail.to_owned(),
    }))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn the_deletion_mail_is_pythons() {
        let m = deletion_mail("a@b.co", 7, "CODE");
        assert_eq!(m.subject, "Your Baskfy account will be deleted in 7 days");
        assert!(
            m.text
                .starts_with("Your Baskfy account is scheduled for deletion\n\nYour account")
        );
        assert!(m.html.contains(
            "<br/>To cancel without signing in, POST /me/restore with this code: CODE<br/>"
        ));
    }

    #[test]
    fn a_restore_code_round_trips_and_is_bound_to_its_account() {
        let secret = b"a-test-secret-of-some-length-000";
        let code = mint_restore_code("abc123abc123", secret, now() + Duration::days(7));
        assert!(restore_code_valid(&code, "abc123abc123", secret).unwrap());
        assert!(!restore_code_valid(&code, "someoneelse0", secret).unwrap());
        assert!(
            !restore_code_valid(&code, "abc123abc123", b"another-secret-of-some-length-00")
                .unwrap()
        );
        let expired = mint_restore_code("abc123abc123", secret, now() - Duration::days(1));
        assert!(!restore_code_valid(&expired, "abc123abc123", secret).unwrap());
    }
}
