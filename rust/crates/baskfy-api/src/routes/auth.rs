//! `/auth/*` and `/me` — docs/07 §"Account & billing". Ports `baskfy_api.routers.auth` (partly).
//!
//! Ported (foundation, RS4b): `GET /me` — profile + entitlements. Ported (RS5, accounts lane):
//! `GET /me/export` — the DPDP data export (read-only: it writes nothing in Python either).
//!
//! **Not yet ported** (every one of them writes, and this wave's pool is read-only): `POST
//! /auth/google`, `POST /auth/refresh`, `POST /auth/logout` (cookie + CSRF, [`crate::csrf`]),
//! `PATCH /me`, `DELETE /me`, `POST /me/restore`.
//!
//! **One deliberate difference on a read-only pool** (DECISIONS-RS R5.7): Python's `GET /me`
//! runs `ensure_staff_from_allowlist`, which *writes* `app_user.is_staff = true` for an
//! allowlisted address that is not yet staff, and then answers `is_staff: true`. The Rust port
//! answers the same `is_staff: true` without the write (it cannot write); the next request
//! computes it again. On the bed every allowlisted account is already staff, so the two agree
//! byte for byte; the write moves here when the pool may write `app_user`.

use axum::Router;
use axum::extract::{Extension, State};
use axum::response::Response;
use axum::routing::get;
use chrono::{DateTime, Utc};
use serde::Serialize;
use sqlx::Row;

use crate::auth::{Principal, require_authenticated};
use baskfy_db::OrderedJson;

use crate::entitlements::{EntitlementsOut, entitlements_for};
use crate::problems::{Problem, unauthenticated};
use crate::state::AppState;
use crate::wire;

/// `MeOut`, field for field in its declaration order.
#[derive(Debug, Clone, Serialize)]
pub struct MeOut {
    /// `public_id`.
    pub public_id: String,
    /// `email`.
    pub email: String,
    /// `name`.
    pub name: Option<String>,
    /// `email_verified`.
    pub email_verified: bool,
    /// `created_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub created_at: DateTime<Utc>,
    /// `plan_code`.
    pub plan_code: Option<String>,
    /// `subscription_status`.
    pub subscription_status: Option<String>,
    /// `entitlements`.
    pub entitlements: EntitlementsOut,
    /// `is_staff`.
    pub is_staff: bool,
    /// `deletion_scheduled_for`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub deletion_scheduled_for: Option<DateTime<Utc>>,
    /// `session_epoch`.
    pub session_epoch: i64,
}

fn db(e: sqlx::Error) -> Problem {
    crate::db_failure(&e.into())
}

/// One `app_user` row, as `_load_me` returns it.
#[derive(Debug, Clone)]
struct MeRow {
    public_id: String,
    email: String,
    name: Option<String>,
    email_verified_at: Option<DateTime<Utc>>,
    created_at: DateTime<Utc>,
    is_staff: bool,
    session_epoch: i64,
}

/// `_load_me`: the account, or 401 when it no longer exists.
async fn load_me(state: &AppState, user_id: i64) -> Result<MeRow, Problem> {
    let user = sqlx::query(
        "SELECT public_id, email::text AS email, name, email_verified_at, created_at, is_staff, \
         session_epoch::bigint AS session_epoch FROM app_user WHERE id = $1",
    )
    .bind(user_id)
    .fetch_optional(state.db.pool())
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

/// `_me_payload`: the newest subscription of any status, its plan's code, a pending deletion.
async fn me_payload(
    state: &AppState,
    user_id: i64,
    user: MeRow,
    entitlements: EntitlementsOut,
    is_staff: bool,
) -> Result<MeOut, Problem> {
    let pool = state.db.pool();
    let subscription = sqlx::query(
        "SELECT plan_id, status FROM subscription WHERE user_id = $1 \
         ORDER BY started_at DESC LIMIT 1",
    )
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(db)?;
    let (plan_code, subscription_status) = match subscription {
        Some(row) => {
            let plan_id: i16 = row.try_get("plan_id").map_err(db)?;
            let status: String = row.try_get("status").map_err(db)?;
            let code: Option<String> = sqlx::query_scalar("SELECT code FROM plan WHERE id = $1")
                .bind(plan_id)
                .fetch_optional(pool)
                .await
                .map_err(db)?;
            (code, Some(status))
        }
        None => (None, None),
    };
    let pending: Option<DateTime<Utc>> = sqlx::query_scalar(
        "SELECT purge_after FROM account_deletion WHERE user_id = $1 \
         AND cancelled_at IS NULL AND purged_at IS NULL",
    )
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(db)?;
    Ok(MeOut {
        public_id: user.public_id,
        email: user.email,
        name: user.name,
        email_verified: user.email_verified_at.is_some(),
        created_at: user.created_at,
        plan_code,
        subscription_status,
        entitlements,
        is_staff,
        deletion_scheduled_for: pending,
        session_epoch: user.session_epoch,
    })
}

/// `GET /me` (`get_me`): dependencies resolve in Python's order — the session, then
/// `AuthenticatedDep` (401 for anonymous), then the entitlements — then `_load_me`,
/// `ensure_staff_from_allowlist` (read-only here, see the module docs) and `_me_payload`.
///
/// # Errors
/// 401 for an anonymous caller or a vanished account; 500 for a database failure.
pub async fn get_me(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let entitlements = entitlements_for(&state.db, principal, &state.settings).await?;
    let user_id = principal.require_user()?;
    let user = load_me(&state, user_id).await?;
    // ensure_staff_from_allowlist: grants, never demotes (not persisted here — R5.7).
    let is_staff = user.is_staff || state.settings.is_staff_email(&user.email);
    Ok(wire::json(
        &me_payload(&state, user_id, user, entitlements.as_out(), is_staff).await?,
    ))
}

/// `DataExportOut`, field for field. The four lists are `list[JsonValue]` built from dicts in
/// Python, so each element is an [`OrderedJson`] object in the dict's insertion order.
#[derive(Debug, Clone, Serialize)]
pub struct DataExportOut {
    /// `exported_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub exported_at: DateTime<Utc>,
    /// `profile`.
    pub profile: MeOut,
    /// `screens`.
    pub screens: Vec<OrderedJson>,
    /// `consents`.
    pub consents: Vec<OrderedJson>,
    /// `subscriptions`.
    pub subscriptions: Vec<OrderedJson>,
    /// `payments`.
    pub payments: Vec<OrderedJson>,
}

/// `datetime.isoformat()` of an aware UTC datetime (what asyncpg returns): `+00:00`, never `Z`.
#[must_use]
pub fn py_isoformat_utc(t: DateTime<Utc>) -> String {
    wire::py_datetime_fixed(t.fixed_offset())
        .strip_suffix('Z')
        .map_or_else(|| wire::py_datetime(t), |base| format!("{base}+00:00"))
}

fn obj(entries: Vec<(&str, OrderedJson)>) -> OrderedJson {
    OrderedJson::Object(
        entries
            .into_iter()
            .map(|(k, v)| (k.to_owned(), v))
            .collect(),
    )
}

fn s_or_null(v: Option<String>) -> OrderedJson {
    v.map_or(OrderedJson::Null, OrderedJson::Str)
}

fn iso_or_null(v: Option<DateTime<Utc>>) -> OrderedJson {
    v.map_or(OrderedJson::Null, |t| OrderedJson::Str(py_isoformat_utc(t)))
}

fn dec_or_null(r: &sqlx::postgres::PgRow, column: &str) -> Result<OrderedJson, Problem> {
    Ok(baskfy_db::row::opt_decimal(r, column)
        .map_err(|e| crate::db_failure(&e))?
        .map_or(OrderedJson::Null, |d| OrderedJson::Str(wire::py_decimal(d))))
}

/// `GET /me/export` (`export_me`): everything keyed to the account, in one document (docs/11
/// §Compliance, DPDP). Market data is not included. Rows are read as Python reads them — no
/// `ORDER BY`, so both see the heap's order.
///
/// # Errors
/// 401 for an anonymous caller or a vanished account; 500 for a database failure.
pub async fn export_me(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    use baskfy_db::row;
    let principal = require_authenticated(&principal)?;
    let entitlements = entitlements_for(&state.db, principal, &state.settings).await?;
    let user_id = principal.require_user()?;
    let user = load_me(&state, user_id).await?;
    let pool = state.db.pool();
    let dbe = |e: baskfy_db::DbError| crate::db_failure(&e);

    let mut screens = Vec::new();
    for r in sqlx::query(
        "SELECT public_id, name, definition, columns, created_at FROM screen WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_all(pool)
    .await
    .map_err(db)?
    {
        screens.push(obj(vec![
            (
                "public_id",
                OrderedJson::Str(r.try_get("public_id").map_err(db)?),
            ),
            ("name", OrderedJson::Str(r.try_get("name").map_err(db)?)),
            ("definition", row::jsonb(&r, "definition").map_err(dbe)?),
            ("columns", row::jsonb(&r, "columns").map_err(dbe)?),
            (
                "created_at",
                OrderedJson::Str(py_isoformat_utc(
                    row::timestamptz(&r, "created_at").map_err(dbe)?,
                )),
            ),
        ]));
    }
    let mut consents = Vec::new();
    for r in sqlx::query(
        "SELECT kind, document_version, granted_at, withdrawn_at, source_ip FROM consent_record \
         WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_all(pool)
    .await
    .map_err(db)?
    {
        consents.push(obj(vec![
            ("kind", OrderedJson::Str(r.try_get("kind").map_err(db)?)),
            (
                "document_version",
                OrderedJson::Str(r.try_get("document_version").map_err(db)?),
            ),
            (
                "granted_at",
                OrderedJson::Str(py_isoformat_utc(
                    row::timestamptz(&r, "granted_at").map_err(dbe)?,
                )),
            ),
            (
                "withdrawn_at",
                iso_or_null(row::opt_timestamptz(&r, "withdrawn_at").map_err(dbe)?),
            ),
            ("source_ip", s_or_null(r.try_get("source_ip").map_err(db)?)),
        ]));
    }
    let mut subscriptions = Vec::new();
    for r in sqlx::query(
        "SELECT plan_id, status, started_at, current_period_end FROM subscription WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_all(pool)
    .await
    .map_err(db)?
    {
        subscriptions.push(obj(vec![
            ("plan_id", OrderedJson::Int(i64::from(r.try_get::<i16, _>("plan_id").map_err(db)?))),
            ("status", OrderedJson::Str(r.try_get("status").map_err(db)?)),
            ("started_at", OrderedJson::Str(py_isoformat_utc(row::timestamptz(&r, "started_at").map_err(dbe)?))),
            ("current_period_end", iso_or_null(row::opt_timestamptz(&r, "current_period_end").map_err(dbe)?)),
        ]));
    }
    let mut payments = Vec::new();
    for r in sqlx::query(
        "SELECT amount_inr, status, invoice_number, invoice_date, taxable_inr, cgst_inr, sgst_inr, \
         igst_inr, gst_inr, gst_rate, place_of_supply, customer_gstin, sac_code, \
         razorpay_payment_id, razorpay_order_id, created_at FROM payment WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_all(pool)
    .await
    .map_err(db)?
    {
        payments.push(obj(vec![
            (
                "amount_inr",
                OrderedJson::Str(wire::py_decimal(
                    row::decimal(&r, "amount_inr").map_err(dbe)?,
                )),
            ),
            ("status", OrderedJson::Str(r.try_get("status").map_err(db)?)),
            (
                "invoice_number",
                s_or_null(r.try_get("invoice_number").map_err(db)?),
            ),
            (
                "invoice_date",
                row::opt_date(&r, "invoice_date")
                    .map_err(dbe)?
                    .map_or(OrderedJson::Null, |d| OrderedJson::Str(wire::py_date(d))),
            ),
            ("taxable_inr", dec_or_null(&r, "taxable_inr")?),
            ("cgst_inr", dec_or_null(&r, "cgst_inr")?),
            ("sgst_inr", dec_or_null(&r, "sgst_inr")?),
            ("igst_inr", dec_or_null(&r, "igst_inr")?),
            ("gst_inr", dec_or_null(&r, "gst_inr")?),
            ("gst_rate", dec_or_null(&r, "gst_rate")?),
            (
                "place_of_supply",
                s_or_null(r.try_get("place_of_supply").map_err(db)?),
            ),
            (
                "customer_gstin",
                s_or_null(r.try_get("customer_gstin").map_err(db)?),
            ),
            ("sac_code", s_or_null(r.try_get("sac_code").map_err(db)?)),
            (
                "razorpay_payment_id",
                s_or_null(r.try_get("razorpay_payment_id").map_err(db)?),
            ),
            (
                "razorpay_order_id",
                s_or_null(r.try_get("razorpay_order_id").map_err(db)?),
            ),
            (
                "created_at",
                OrderedJson::Str(py_isoformat_utc(
                    row::timestamptz(&r, "created_at").map_err(dbe)?,
                )),
            ),
        ]));
    }
    // The export reports the stored staff bit: `_me_payload` without the allowlist grant.
    let is_staff = user.is_staff;
    let profile = me_payload(&state, user_id, user, entitlements.as_out(), is_staff).await?;
    Ok(wire::json(&DataExportOut {
        exported_at: crate::entitlements::utc_now(),
        profile,
        screens,
        consents,
        subscriptions,
        payments,
    }))
}

/// The `/auth` and `/me` routes ported so far.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/me", get(get_me))
        .route("/me/export", get(export_me))
}
