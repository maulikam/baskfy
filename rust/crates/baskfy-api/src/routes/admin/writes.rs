//! The `/admin/*` **writes** of `baskfy_api.routers.admin`, with `baskfy_api.admin`'s actions:
//!
//! * `POST /admin/pipeline/runs/{trade_date}/rerun` (202) — publishes `baskfy.pipeline.nightly`;
//! * `POST /admin/instruments/{symbol}/reprocess` (202) — publishes
//!   `baskfy.compute.reprocess_instrument`;
//! * `POST /admin/resync?days=` (202) — inspects, then publishes `baskfy.ops.resync`;
//! * `PUT /admin/users/{public_id}/entitlements` — upserts one override, answers the account's
//!   effective detail;
//! * `DELETE /admin/users/{public_id}/entitlements/{feature}` (204) — absent is not an error.
//!
//! Every route is behind the staff gate (401 anonymous, **404** non-staff) and writes one
//! `admin_action` row in the same transaction as the action. The three enqueueing actions publish
//! through [`super::queue`] (Celery's Redis transport) **before** the audit row, as Python does;
//! none of their tasks reaches `packages/execution`, an order or a GTT (law 2).

use std::collections::HashMap;

use axum::extract::{Extension, Path, RawQuery, Request, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use baskfy_db::row;
use chrono::{DateTime, Utc};
use serde::Serialize;
use sqlx::Row;

use super::AdminUserOut;
use super::queue::{self, Arg, NotSent};
use super::resync::{build_plan, days_param};
use crate::auth::{Principal, require_staff};
use crate::entitlements::{
    ENTITLING_STATUSES, Entitlements, EntitlementsOut, FEATURES, MAX_SCREENS_KEY, Override,
    apply_overrides, utc_now,
};
use crate::logging;
use crate::problems::{Problem, ProblemType, internal_error, invalid_screen_definition};
use crate::routes::api_keys::model::{
    Field, Kind, Presence, PyDateTime, dbw, entry, field, parse_date_str, read_body,
    ser_opt_datetime, validate, write_pool,
};
use crate::session::{self, Tx, WritePool};
use crate::settings::Settings;
use crate::state::AppState;
use crate::wire;

/// `NIGHTLY_TASK_NAME`.
pub const NIGHTLY_TASK_NAME: &str = "baskfy.pipeline.nightly";
/// `REPROCESS_TASK_NAME`.
pub const REPROCESS_TASK_NAME: &str = "baskfy.compute.reprocess_instrument";
/// `resync.RESYNC_TASK_NAME`.
pub const RESYNC_TASK_NAME: &str = "baskfy.ops.resync";
/// `resync.RESYNC_REQUESTED_ACTION`.
pub const RESYNC_REQUESTED_ACTION: &str = "data_resync_requested";

/// `TaskAcceptedOut`.
#[derive(Debug, Clone, Serialize)]
struct TaskAcceptedOut {
    task: &'static str,
    task_id: String,
    target: String,
    detail: String,
}

fn accepted(out: &TaskAcceptedOut) -> Response {
    wire::json_status(StatusCode::ACCEPTED, out)
}

/// `queue is None` (no broker) is the action's own 500 sentence; a failed publish is the
/// catch-all.
fn not_sent(e: NotSent, what: &str) -> Problem {
    match e {
        NotSent::NoBroker => Problem::new(
            ProblemType::InternalError,
            format!("No task broker is configured, so {what} from here."),
        ),
        NotSent::Failed(reason) => {
            logging::error("baskfy_api.app", "unhandled error", &[("error", &reason)]);
            internal_error()
        }
    }
}

/// `record_action(session, actor, action=, target=, detail=)`.
async fn record_action(
    tx: &mut Tx,
    actor: &Principal,
    action: &str,
    target: &str,
    detail: serde_json::Value,
) -> Result<(), Problem> {
    let actor_id = actor.require_user()?;
    sqlx::query(
        "INSERT INTO admin_action (actor_user_id, action, target, detail) VALUES ($1, $2, $3, $4)",
    )
    .bind(actor_id)
    .bind(action)
    .bind(target)
    .bind(sqlx::types::Json(detail))
    .execute(&mut **tx)
    .await
    .map_err(dbw)?;
    logging::info(
        "baskfy_api.admin",
        "admin action",
        &[
            ("action", action),
            ("target", target),
            ("actor", actor.public_id.as_deref().unwrap_or("")),
        ],
    );
    Ok(())
}

/// `POST /admin/pipeline/runs/{trade_date}/rerun` (`rerun_pipeline`).
///
/// # Errors
/// The staff gate; 400 for a `trade_date` that is not a date; 500 (no broker, or the publish
/// failed).
pub async fn rerun_pipeline(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(trade_date): Path<String>,
) -> Result<Response, Problem> {
    let staff = require_staff(&principal)?.clone();
    let date = parse_date_str(&trade_date)
        .map_err(|(kind, msg)| invalid_screen_definition(vec![entry("trade_date", &msg, kind)]))?;
    let iso = wire::py_date(date);
    let pool = write_pool(write)?;
    let task_id = queue::send_task(&state.settings, NIGHTLY_TASK_NAME, &[Arg::Str(iso.clone())])
        .await
        .map_err(|e| not_sent(e, "the pipeline cannot be re-run"))?;
    let (target, id) = (iso.clone(), task_id.clone());
    session::run(&pool, move |tx| {
        Box::pin(async move {
            record_action(
                tx,
                &staff,
                "pipeline_rerun",
                &target,
                serde_json::json!({"task_id": id, "task": NIGHTLY_TASK_NAME}),
            )
            .await
        })
    })
    .await?;
    Ok(accepted(&TaskAcceptedOut {
        task: NIGHTLY_TASK_NAME,
        task_id,
        target: iso,
        detail: "The nightly chain has been queued. Watch the run history for its steps."
            .to_owned(),
    }))
}

/// `POST /admin/instruments/{symbol}/reprocess` (`reprocess_instrument`).
///
/// # Errors
/// The staff gate; 400 (a symbol over 64 characters); 404 (no such instrument); 500.
pub async fn reprocess_instrument(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(symbol): Path<String>,
) -> Result<Response, Problem> {
    let staff = require_staff(&principal)?.clone();
    let n = symbol.chars().count();
    if n > 64 {
        return Err(invalid_screen_definition(vec![entry(
            "symbol",
            "String should have at most 64 characters",
            "string_too_long",
        )]));
    }
    if n < 1 {
        return Err(invalid_screen_definition(vec![entry(
            "symbol",
            "String should have at least 1 character",
            "string_too_short",
        )]));
    }
    let pool = write_pool(write)?;
    let settings = state.settings.clone();
    let (task_id, target) = session::run(&pool, move |tx| {
        Box::pin(async move {
            let rows = sqlx::query("SELECT id, symbol FROM instrument WHERE symbol = $1")
                .bind(symbol.to_uppercase())
                .fetch_all(&mut **tx)
                .await
                .map_err(dbw)?;
            let row = match rows.as_slice() {
                [] => {
                    return Err(Problem::new(
                        ProblemType::NotFound,
                        format!(
                            "No instrument with symbol {}.",
                            baskfy_providers::py::repr_str(&symbol)
                        ),
                    ));
                }
                [one] => one,
                _ => {
                    logging::error(
                        "baskfy_api.app",
                        "unhandled error",
                        &[(
                            "error",
                            "Multiple rows were found when one or none was required",
                        )],
                    );
                    return Err(internal_error());
                }
            };
            let id: i64 = row.try_get("id").map_err(dbw)?;
            let found: String = row.try_get("symbol").map_err(dbw)?;
            let task_id = queue::send_task(&settings, REPROCESS_TASK_NAME, &[Arg::Int(id)])
                .await
                .map_err(|e| not_sent(e, "the instrument cannot be reprocessed"))?;
            record_action(
                tx,
                &staff,
                "instrument_reprocess",
                &found,
                serde_json::json!({
                    "task_id": task_id,
                    "instrument_id": id,
                    "task": REPROCESS_TASK_NAME,
                }),
            )
            .await?;
            Ok((task_id, found))
        })
    })
    .await?;
    Ok(accepted(&TaskAcceptedOut {
        task: REPROCESS_TASK_NAME,
        task_id,
        target,
        detail: "Adjusted bars will be rebuilt from close_raw and the corporate-action history."
            .to_owned(),
    }))
}

/// `POST /admin/resync?days=` (`start_resync`).
///
/// # Errors
/// The staff gate; 400 (a bad `days`); 500.
pub async fn start_resync(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let staff = require_staff(&principal)?.clone();
    let days = days_param(raw.as_deref())?;
    let plan = build_plan(&state, days).await?;
    let pool = write_pool(write)?;
    let user_id = staff.require_user()?;
    let target = format!(
        "{}..{}",
        wire::py_date(plan.window_start),
        wire::py_date(plan.window_end)
    );
    let pending = plan.findings.len();
    let mut kinds: Vec<&str> = plan.findings.iter().map(|f| f.kind).collect();
    kinds.sort_unstable();
    kinds.dedup();
    let task_id = queue::send_task(
        &state.settings,
        RESYNC_TASK_NAME,
        &[Arg::Int(user_id), Arg::Int(days)],
    )
    .await
    .map_err(|e| not_sent(e, "a resync cannot be started"))?;
    let detail = serde_json::json!({
        "task_id": task_id,
        "task": RESYNC_TASK_NAME,
        "days": days,
        "pending_at_request": pending,
        "kinds_at_request": kinds,
    });
    let audit_target = target.clone();
    session::run(&pool, move |tx| {
        Box::pin(async move {
            record_action(tx, &staff, RESYNC_REQUESTED_ACTION, &audit_target, detail).await
        })
    })
    .await?;
    let detail = if pending == 0 {
        "Nothing was pending when this was queued; the task re-checks before acting and will \
         report that it had nothing to do."
            .to_owned()
    } else {
        format!(
            "{pending} pending item(s) queued for repair. The result appears here when it \
             finishes — including anything it could not fix."
        )
    };
    Ok(accepted(&TaskAcceptedOut {
        task: RESYNC_TASK_NAME,
        task_id,
        target,
        detail,
    }))
}

/// `admin.OVERRIDABLE`: the six features and `max_screens`.
fn overridable(feature: &str) -> bool {
    feature == MAX_SCREENS_KEY || FEATURES.contains(&feature)
}

/// `EntitlementOverrideOut` (its `expires_at` through [`PyDateTime`], always the stored UTC
/// instant here).
#[derive(Debug, Clone, Serialize)]
struct OverrideOut {
    feature: String,
    effect: &'static str,
    value: Option<i64>,
    reason: String,
    granted_by: Option<String>,
    #[serde(serialize_with = "ser_opt_datetime")]
    expires_at: Option<PyDateTime>,
    #[serde(serialize_with = "wire::ser::datetime")]
    created_at: DateTime<Utc>,
    active: bool,
}

/// `AdminUserDetailOut`, with [`OverrideOut`].
#[derive(Debug, Clone, Serialize)]
struct DetailOut {
    user: AdminUserOut,
    plan_code: Option<String>,
    subscription_status: Option<String>,
    screen_count: i64,
    entitlements: EntitlementsOut,
    overrides: Vec<OverrideOut>,
}

const USER_COLUMNS: &str = "id, public_id, email::text AS email, name, created_at, \
                            email_verified_at, is_staff, deleted_at";

/// `_load_user(session, public_id)`: the row, or `404 No account with id '…'.`.
async fn load_user(tx: &mut Tx, public_id: &str) -> Result<sqlx::postgres::PgRow, Problem> {
    sqlx::query(&format!(
        "SELECT {USER_COLUMNS} FROM app_user WHERE public_id = $1"
    ))
    .bind(public_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(dbw)?
    .ok_or_else(|| {
        Problem::new(
            ProblemType::NotFound,
            format!(
                "No account with id {}.",
                baskfy_providers::py::repr_str(public_id)
            ),
        )
    })
}

fn user_out(r: &sqlx::postgres::PgRow) -> Result<AdminUserOut, Problem> {
    let dbe = |e: baskfy_db::DbError| crate::db_failure(&e);
    Ok(AdminUserOut {
        public_id: r.try_get("public_id").map_err(dbw)?,
        email: r.try_get("email").map_err(dbw)?,
        name: r.try_get("name").map_err(dbw)?,
        created_at: row::timestamptz(r, "created_at").map_err(dbe)?,
        email_verified: row::opt_timestamptz(r, "email_verified_at")
            .map_err(dbe)?
            .is_some(),
        is_staff: r.try_get("is_staff").map_err(dbw)?,
        deleted_at: row::opt_timestamptz(r, "deleted_at").map_err(dbe)?,
    })
}

/// `entitlements_for(session, subject)` inside the write transaction (it must see the override
/// just written).
async fn entitlements_tx(
    tx: &mut Tx,
    settings: &Settings,
    user_id: i64,
    now: DateTime<Utc>,
) -> Result<Entitlements, Problem> {
    let dbe = |e: baskfy_db::DbError| crate::db_failure(&e);
    let rows = sqlx::query(
        "SELECT s.current_period_end, p.features FROM subscription s \
         JOIN plan p ON p.id = s.plan_id \
         WHERE s.user_id = $1 AND s.status = ANY($2) \
         ORDER BY s.started_at DESC, s.id DESC",
    )
    .bind(user_id)
    .bind(ENTITLING_STATUSES.as_slice())
    .fetch_all(&mut **tx)
    .await
    .map_err(dbw)?;
    let mut base: Option<Entitlements> = None;
    for r in &rows {
        let end = row::opt_timestamptz(r, "current_period_end").map_err(dbe)?;
        if end.is_none_or(|end| end > now) {
            base = Some(Entitlements::from_plan_features(
                &row::jsonb(r, "features").map_err(dbe)?,
            ));
            break;
        }
    }
    let base = base.unwrap_or_else(|| {
        if settings.free_tier_enabled {
            Entitlements::free_tier()
        } else {
            Entitlements::registered()
        }
    });
    let rows = sqlx::query(
        "SELECT feature, effect, value, expires_at FROM entitlement_override WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_all(&mut **tx)
    .await
    .map_err(dbw)?;
    let mut overrides = Vec::new();
    for r in &rows {
        let expires = row::opt_timestamptz(r, "expires_at").map_err(dbe)?;
        if expires.is_some_and(|x| x <= now) {
            continue;
        }
        let effect: String = r.try_get("effect").map_err(dbw)?;
        overrides.push(Override {
            feature: r.try_get("feature").map_err(dbw)?,
            grant: effect == "grant",
            value: r.try_get("value").map_err(dbw)?,
        });
    }
    Ok(if overrides.is_empty() {
        base
    } else {
        apply_overrides(&base, &overrides)
    })
}

/// `_plan_summary(session, user_id)` inside the write transaction.
async fn plan_summary_tx(
    tx: &mut Tx,
    user_id: i64,
    now: DateTime<Utc>,
) -> Result<(Option<String>, Option<String>), Problem> {
    let dbe = |e: baskfy_db::DbError| crate::db_failure(&e);
    let rows = sqlx::query(
        "SELECT s.status, s.current_period_end, p.code FROM subscription s \
         JOIN plan p ON p.id = s.plan_id WHERE s.user_id = $1 AND s.status = ANY($2) \
         ORDER BY s.started_at DESC, s.id DESC",
    )
    .bind(user_id)
    .bind(ENTITLING_STATUSES.as_slice())
    .fetch_all(&mut **tx)
    .await
    .map_err(dbw)?;
    for r in &rows {
        let end = row::opt_timestamptz(r, "current_period_end").map_err(dbe)?;
        if end.is_none_or(|end| end > now) {
            return Ok((
                Some(r.try_get("code").map_err(dbw)?),
                Some(r.try_get("status").map_err(dbw)?),
            ));
        }
    }
    let latest = sqlx::query(
        "SELECT s.status, p.code FROM subscription s JOIN plan p ON p.id = s.plan_id \
         WHERE s.user_id = $1 ORDER BY s.started_at DESC, s.id DESC LIMIT 1",
    )
    .bind(user_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(dbw)?;
    match latest {
        Some(r) => Ok((
            Some(r.try_get("code").map_err(dbw)?),
            Some(r.try_get("status").map_err(dbw)?),
        )),
        None => Ok((None, None)),
    }
}

/// `user_detail(...)` + `_detail_out(...)` inside the write transaction.
async fn detail_tx(
    tx: &mut Tx,
    settings: &Settings,
    public_id: &str,
) -> Result<DetailOut, Problem> {
    let dbe = |e: baskfy_db::DbError| crate::db_failure(&e);
    let user = load_user(tx, public_id).await?;
    let user_id: i64 = user.try_get("id").map_err(dbw)?;
    let now = utc_now();
    let (plan_code, subscription_status) = plan_summary_tx(tx, user_id, now).await?;
    let overrides = sqlx::query(
        "SELECT feature, effect, value, reason, granted_by_user_id, expires_at, created_at \
         FROM entitlement_override WHERE user_id = $1 ORDER BY feature ASC",
    )
    .bind(user_id)
    .fetch_all(&mut **tx)
    .await
    .map_err(dbw)?;
    let screen_count: i64 = sqlx::query_scalar("SELECT count(*) FROM screen WHERE user_id = $1")
        .bind(user_id)
        .fetch_one(&mut **tx)
        .await
        .map_err(dbw)?;
    let entitlements = entitlements_tx(tx, settings, user_id, now).await?;
    let mut authors: HashMap<i64, String> = HashMap::new();
    for o in &overrides {
        let by: i64 = o.try_get("granted_by_user_id").map_err(dbw)?;
        if let std::collections::hash_map::Entry::Vacant(slot) = authors.entry(by) {
            let email: Option<String> =
                sqlx::query_scalar("SELECT email::text FROM app_user WHERE id = $1")
                    .bind(by)
                    .fetch_optional(&mut **tx)
                    .await
                    .map_err(dbw)?;
            slot.insert(email.unwrap_or_else(|| "(deleted account)".to_owned()));
        }
    }
    let mut out = Vec::with_capacity(overrides.len());
    for o in &overrides {
        let by: i64 = o.try_get("granted_by_user_id").map_err(dbw)?;
        let feature: String = o.try_get("feature").map_err(dbw)?;
        let effect: String = o.try_get("effect").map_err(dbw)?;
        // Re-read from the row: the answer carries the stored instant (UTC), not the request's
        // offset (graded: `admin-put-grant`).
        let expires_at = row::opt_timestamptz(o, "expires_at")
            .map_err(dbe)?
            .map(PyDateTime::from_utc);
        out.push(OverrideOut {
            feature,
            effect: if effect == "grant" { "grant" } else { "revoke" },
            value: o.try_get("value").map_err(dbw)?,
            reason: o.try_get("reason").map_err(dbw)?,
            granted_by: authors.get(&by).cloned(),
            expires_at,
            created_at: row::timestamptz(o, "created_at").map_err(dbe)?,
            active: expires_at.is_none_or(|x| x.aware() > now),
        });
    }
    Ok(DetailOut {
        user: user_out(&user)?,
        plan_code,
        subscription_status,
        screen_count,
        entitlements: entitlements.as_out(),
        overrides: out,
    })
}

/// `EntitlementOverrideIn`.
const OVERRIDE: [Field; 5] = [
    field(
        "feature",
        Kind::Str {
            min: None,
            max: None,
        },
        Presence::Required,
    ),
    field(
        "effect",
        Kind::Literal(&["grant", "revoke"]),
        Presence::Required,
    ),
    field(
        "value",
        Kind::Int {
            ge: Some(0),
            le: None,
        },
        Presence::Optional,
    ),
    field(
        "reason",
        Kind::Str {
            min: Some(3),
            max: Some(500),
        },
        Presence::Required,
    ),
    field("expires_at", Kind::DateTime, Presence::Optional),
];

fn json_of(v: Option<i64>) -> serde_json::Value {
    v.map_or(serde_json::Value::Null, serde_json::Value::from)
}

/// `PUT /admin/users/{public_id}/entitlements` (`put_entitlement_override`).
///
/// # Errors
/// The staff gate; 400 (body; a feature that cannot be overridden; `max_screens` granted with no
/// value); 404 (no such account); 500.
pub async fn put_entitlement_override(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(public_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let received = read_body(request).await?;
    let staff = require_staff(&principal)?.clone();
    let values = validate(&received, &OVERRIDE)?;
    let feature = values[0].str().unwrap_or_default().to_owned();
    let grant = values[1].str() == Some("grant");
    let raw_value = values[2].int().cloned();
    let reason = values[3].str().unwrap_or_default().to_owned();
    let expires_at = values[4].datetime().copied();
    if !overridable(&feature) {
        return Err(Problem::new(
            ProblemType::InvalidScreenDefinition,
            format!(
                "{} is not an overridable entitlement.",
                baskfy_providers::py::repr_str(&feature)
            ),
        ));
    }
    if feature == MAX_SCREENS_KEY && grant && raw_value.is_none() {
        return Err(Problem::new(
            ProblemType::InvalidScreenDefinition,
            "Overriding max_screens requires a value.",
        ));
    }
    let pool = write_pool(write)?;
    let settings = state.settings.clone();
    let detail = session::run(&pool, move |tx| {
        Box::pin(async move {
            let user = load_user(tx, &public_id).await?;
            let user_id: i64 = user.try_get("id").map_err(dbw)?;
            let user_public_id: String = user.try_get("public_id").map_err(dbw)?;
            let actor = staff.require_user()?;
            // asyncpg refuses a `bigint` beyond int64 when the row is flushed: the catch-all.
            let value = match &raw_value {
                Some(v) => Some(v.as_i64().ok_or_else(|| {
                    logging::error(
                        "baskfy_api.app",
                        "unhandled error",
                        &[("error", "value out of int64 range")],
                    );
                    internal_error()
                })?),
                None => None,
            };
            let existing = sqlx::query(
                "SELECT id, effect, value, reason, expires_at, granted_by_user_id \
                 FROM entitlement_override WHERE user_id = $1 AND feature = $2",
            )
            .bind(user_id)
            .bind(&feature)
            .fetch_optional(&mut **tx)
            .await
            .map_err(dbw)?;
            let effect = if grant { "grant" } else { "revoke" };
            let instant = expires_at.as_ref().map(PyDateTime::instant);
            let previous = match &existing {
                None => serde_json::Value::Null,
                Some(r) => {
                    let old_effect: String = r.try_get("effect").map_err(dbw)?;
                    let old_value: Option<i64> = r.try_get("value").map_err(dbw)?;
                    let old_reason: String = r.try_get("reason").map_err(dbw)?;
                    serde_json::json!({
                        "effect": old_effect,
                        "value": json_of(old_value),
                        "reason": old_reason,
                    })
                }
            };
            match &existing {
                None => {
                    sqlx::query(
                        "INSERT INTO entitlement_override (user_id, feature, effect, value, \
                         granted_by_user_id, reason, expires_at) \
                         VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    )
                    .bind(user_id)
                    .bind(&feature)
                    .bind(effect)
                    .bind(value)
                    .bind(actor)
                    .bind(&reason)
                    .bind(instant)
                    .execute(&mut **tx)
                    .await
                    .map_err(dbw)?;
                }
                Some(r) => {
                    // The ORM writes only the columns whose value changed.
                    let id: i64 = r.try_get("id").map_err(dbw)?;
                    let old_effect: String = r.try_get("effect").map_err(dbw)?;
                    let old_value: Option<i64> = r.try_get("value").map_err(dbw)?;
                    let old_reason: String = r.try_get("reason").map_err(dbw)?;
                    let old_expiry =
                        row::opt_timestamptz(r, "expires_at").map_err(|e| crate::db_failure(&e))?;
                    let old_by: i64 = r.try_get("granted_by_user_id").map_err(dbw)?;
                    let mut sets = Vec::new();
                    let mut n = 1;
                    let changed = [
                        (old_effect != effect, "effect"),
                        (old_value != value, "value"),
                        (old_reason != reason, "reason"),
                        (old_expiry != instant, "expires_at"),
                        (old_by != actor, "granted_by_user_id"),
                    ];
                    for (c, col) in changed {
                        if c {
                            n += 1;
                            sets.push(format!("{col} = ${n}"));
                        }
                    }
                    if !sets.is_empty() {
                        let sql = format!(
                            "UPDATE entitlement_override SET {} WHERE id = $1",
                            sets.join(", ")
                        );
                        let mut q = sqlx::query(&sql).bind(id);
                        if changed[0].0 {
                            q = q.bind(effect);
                        }
                        if changed[1].0 {
                            q = q.bind(value);
                        }
                        if changed[2].0 {
                            q = q.bind(reason.clone());
                        }
                        if changed[3].0 {
                            q = q.bind(instant);
                        }
                        if changed[4].0 {
                            q = q.bind(actor);
                        }
                        q.execute(&mut **tx).await.map_err(dbw)?;
                    }
                }
            }
            record_action(
                tx,
                &staff,
                "entitlement_override_set",
                &user_public_id,
                serde_json::json!({
                    "feature": feature,
                    "effect": effect,
                    "value": json_of(value),
                    "reason": reason,
                    "expires_at": expires_at.as_ref().map(PyDateTime::isoformat),
                    "previous": previous,
                }),
            )
            .await?;
            detail_tx(tx, &settings, &public_id).await
        })
    })
    .await?;
    Ok(wire::json(&detail))
}

/// `DELETE /admin/users/{public_id}/entitlements/{feature}` (`delete_entitlement_override`).
///
/// # Errors
/// The staff gate; 404 (no such account); 500.
pub async fn delete_entitlement_override(
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path((public_id, feature)): Path<(String, String)>,
) -> Result<Response, Problem> {
    let staff = require_staff(&principal)?.clone();
    let pool = write_pool(write)?;
    session::run(&pool, move |tx| {
        Box::pin(async move {
            let user = load_user(tx, &public_id).await?;
            let user_id: i64 = user.try_get("id").map_err(dbw)?;
            let user_public_id: String = user.try_get("public_id").map_err(dbw)?;
            let existing = sqlx::query(
                "SELECT id, effect, value FROM entitlement_override \
                 WHERE user_id = $1 AND feature = $2",
            )
            .bind(user_id)
            .bind(&feature)
            .fetch_optional(&mut **tx)
            .await
            .map_err(dbw)?;
            let Some(r) = existing else {
                return Ok(());
            };
            let id: i64 = r.try_get("id").map_err(dbw)?;
            let effect: String = r.try_get("effect").map_err(dbw)?;
            let value: Option<i64> = r.try_get("value").map_err(dbw)?;
            sqlx::query("DELETE FROM entitlement_override WHERE id = $1")
                .bind(id)
                .execute(&mut **tx)
                .await
                .map_err(dbw)?;
            record_action(
                tx,
                &staff,
                "entitlement_override_cleared",
                &user_public_id,
                serde_json::json!({"feature": feature, "effect": effect, "value": json_of(value)}),
            )
            .await
        })
    })
    .await?;
    Ok(StatusCode::NO_CONTENT.into_response())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_module_of_this_lane_can_reach_an_order() {
        // test_admin_resync.py `TestItCannotPlaceAnOrder`, over every write module of the lane:
        // the guarantee wanted is "there is no such path", which only reading the source says.
        // The tokens are assembled at run time so this file does not name them itself.
        let forbidden = [
            ["Order", "Gateway"].concat(),
            ["place", "_order"].concat(),
            ["place", "_gtt"].concat(),
            ["baskfy", "_execution"].concat(),
            ["baskfy", "-execution"].concat(),
            ["kite", "::orders"].concat(),
        ];
        let sources = [
            ("admin.rs", include_str!("../admin.rs")),
            ("admin/writes.rs", include_str!("writes.rs")),
            ("admin/queue.rs", include_str!("queue.rs")),
            (
                "accounts/admin_resync.rs",
                include_str!("../accounts/admin_resync.rs"),
            ),
            ("api_keys/writes.rs", include_str!("../api_keys/writes.rs")),
            ("alerts/writes.rs", include_str!("../alerts/writes.rs")),
            (
                "webhook_endpoints/writes.rs",
                include_str!("../webhook_endpoints/writes.rs"),
            ),
            ("support.rs", include_str!("../support.rs")),
            ("billing/writes.rs", include_str!("../billing/writes.rs")),
        ];
        for (file, text) in sources {
            assert!(!text.is_empty(), "{file} exists, so this is not vacuous");
            for token in &forbidden {
                assert!(!text.contains(token.as_str()), "{file} names {token}");
            }
        }
    }

    #[test]
    fn the_overridable_set_is_the_six_features_and_max_screens() {
        for f in FEATURES {
            assert!(overridable(f));
        }
        assert!(overridable("max_screens"));
        assert!(!overridable("universes"));
        assert!(!overridable("Screener"));
    }
}
