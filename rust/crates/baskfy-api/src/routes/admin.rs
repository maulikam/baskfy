//! ``/admin/*`` — the staff surface (PROMPTS.md Prompt 17 deliverable 4).
//!
//! Ports the **GET** routes of `baskfy_api.routers.admin` (`APIRouter(prefix="/admin",
//! tags=["admin"], dependencies=[Depends(require_staff)])`) and the reads of
//! `baskfy_api.admin` / `baskfy_api.resync` they call:
//!
//! * `GET /admin/pipeline/runs?limit=` and `GET /admin/pipeline/runs/{run_id}`;
//! * `GET /admin/resync?days=` — the dry inspection ([`resync`]);
//! * `GET /admin/data-versions?limit=`;
//! * `GET /admin/providers` — `providers doctor`, rendered ([`providers`]);
//! * `GET /admin/users?q=&limit=` and `GET /admin/users/{public_id}`;
//! * `GET /admin/actions?limit=`;
//! * `GET /admin/public-api`.
//!
//! **The staff gate is exactly Python's**: every route runs [`require_staff`] first (the router
//! dependency) — 401 for an anonymous caller, **404** (not 403) for a signed-in non-staff one —
//! before any parameter is validated.
//!
//! The **writes** — `POST /admin/pipeline/runs/{trade_date}/rerun`,
//! `POST /admin/instruments/{symbol}/reprocess`, `POST /admin/resync`,
//! `PUT /admin/users/{public_id}/entitlements` and
//! `DELETE /admin/users/{public_id}/entitlements/{feature}` — are in [`writes`] (wave 4c, lane
//! writes-accounts); the three that enqueue publish through [`queue`] (Celery's Redis transport).
//! **No route here can place, modify or cancel an order** (law 2).

#[path = "accounts/admin_providers.rs"]
mod providers;
mod queue;
/// `baskfy_api.resync` — public so the worker's `baskfy.ops.resync` inspects exactly as the
/// API does (wave 6, lane api_routes).
#[path = "accounts/admin_resync.rs"]
pub mod resync;
mod writes;

use std::collections::HashMap;

use axum::Router;
use axum::extract::{Extension, Path, RawQuery, State};
use axum::response::Response;
use axum::routing::{delete, get, post, put};
use baskfy_core::factor_registry::FactorUnit;
use baskfy_db::{OrderedJson, row};
use chrono::{DateTime, NaiveDate, Utc};
use serde::Serialize;
use sqlx::Row;
use sqlx::postgres::PgRow;

use super::screens::accounts::{self, db, dbe};
use crate::auth::{Principal, PrincipalKind, require_staff};
use crate::entitlements::{EntitlementsOut, entitlements_for, utc_now};
use crate::problems::{Problem, ProblemType};
use crate::query::{QueryParams, Validation};
use crate::state::AppState;
use crate::wire;

/// `admin.DEFAULT_RUN_LIMIT`.
pub const DEFAULT_RUN_LIMIT: i64 = 60;
/// `admin.MAX_RUN_LIMIT`.
pub const MAX_RUN_LIMIT: i64 = 365;
/// `admin.MAX_USER_HITS`.
pub const MAX_USER_HITS: i64 = 50;

/// `PipelineStepOut`.
#[derive(Debug, Clone, Serialize)]
pub struct PipelineStepOut {
    /// `step`.
    pub step: String,
    /// `status`.
    pub status: String,
    /// `rows_in`.
    pub rows_in: Option<i64>,
    /// `rows_out`.
    pub rows_out: Option<i64>,
    /// `duration_ms`.
    pub duration_ms: Option<i64>,
    /// `detail` (`pipeline_run_step.error`, jsonb).
    pub detail: Option<OrderedJson>,
}

/// `PipelineRunDetailOut`.
#[derive(Debug, Clone, Serialize)]
pub struct PipelineRunDetailOut {
    /// `id`.
    pub id: i64,
    /// `trade_date`.
    #[serde(serialize_with = "wire::ser::date")]
    pub trade_date: NaiveDate,
    /// `status`.
    pub status: String,
    /// `started_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub started_at: DateTime<Utc>,
    /// `finished_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub finished_at: Option<DateTime<Utc>>,
    /// `data_version`.
    pub data_version: Option<i64>,
    /// `publish_latency_seconds`.
    #[serde(serialize_with = "wire::ser::opt_float")]
    pub publish_latency_seconds: Option<f64>,
    /// `steps`.
    pub steps: Vec<PipelineStepOut>,
}

/// `PipelineRunListOut`.
#[derive(Debug, Clone, Serialize)]
pub struct PipelineRunListOut {
    /// `data`.
    pub data: Vec<PipelineRunDetailOut>,
}

/// `DataVersionOut`.
#[derive(Debug, Clone, Serialize)]
pub struct DataVersionOut {
    /// `data_version`.
    pub data_version: i64,
    /// `trade_date`.
    #[serde(serialize_with = "wire::ser::date")]
    pub trade_date: NaiveDate,
    /// `published_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub published_at: Option<DateTime<Utc>>,
    /// `publish_latency_seconds`.
    #[serde(serialize_with = "wire::ser::opt_float")]
    pub publish_latency_seconds: Option<f64>,
}

/// `DataVersionListOut`.
#[derive(Debug, Clone, Serialize)]
pub struct DataVersionListOut {
    /// `data`.
    pub data: Vec<DataVersionOut>,
    /// `current`.
    pub current: i64,
}

/// `AdminUserOut`.
#[derive(Debug, Clone, Serialize)]
pub struct AdminUserOut {
    /// `public_id`.
    pub public_id: String,
    /// `email`.
    pub email: String,
    /// `name`.
    pub name: Option<String>,
    /// `created_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub created_at: DateTime<Utc>,
    /// `email_verified`.
    pub email_verified: bool,
    /// `is_staff`.
    pub is_staff: bool,
    /// `deleted_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub deleted_at: Option<DateTime<Utc>>,
}

/// `AdminUserListOut`.
#[derive(Debug, Clone, Serialize)]
pub struct AdminUserListOut {
    /// `data`.
    pub data: Vec<AdminUserOut>,
}

/// `EntitlementOverrideOut`.
#[derive(Debug, Clone, Serialize)]
pub struct EntitlementOverrideOut {
    /// `feature`.
    pub feature: String,
    /// `effect`: `grant` or `revoke`.
    pub effect: &'static str,
    /// `value`.
    pub value: Option<i64>,
    /// `reason`.
    pub reason: String,
    /// `granted_by`.
    pub granted_by: Option<String>,
    /// `expires_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub expires_at: Option<DateTime<Utc>>,
    /// `created_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub created_at: DateTime<Utc>,
    /// `active`.
    pub active: bool,
}

/// `AdminUserDetailOut`.
#[derive(Debug, Clone, Serialize)]
pub struct AdminUserDetailOut {
    /// `user`.
    pub user: AdminUserOut,
    /// `plan_code`.
    pub plan_code: Option<String>,
    /// `subscription_status`.
    pub subscription_status: Option<String>,
    /// `screen_count`.
    pub screen_count: i64,
    /// `entitlements`.
    pub entitlements: EntitlementsOut,
    /// `overrides`.
    pub overrides: Vec<EntitlementOverrideOut>,
}

/// `AdminActionOut`.
#[derive(Debug, Clone, Serialize)]
pub struct AdminActionOut {
    /// `action`.
    pub action: String,
    /// `target`.
    pub target: String,
    /// `actor`.
    pub actor: Option<String>,
    /// `detail`.
    pub detail: Option<OrderedJson>,
    /// `created_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub created_at: DateTime<Utc>,
}

/// `AdminActionListOut`.
#[derive(Debug, Clone, Serialize)]
pub struct AdminActionListOut {
    /// `data`.
    pub data: Vec<AdminActionOut>,
}

/// `PublicApiGateOut`.
#[derive(Debug, Clone, Serialize)]
pub struct PublicApiGateOut {
    /// `flag_enabled`.
    pub flag_enabled: bool,
    /// `review_signed_off`.
    pub review_signed_off: bool,
    /// `serving`.
    pub serving: bool,
    /// `requirement`.
    pub requirement: &'static str,
    /// `opinion_reference`.
    pub opinion_reference: &'static str,
    /// `signed_off_on`.
    pub signed_off_on: &'static str,
    /// `signed_off_by`.
    pub signed_off_by: &'static str,
    /// `api_version`.
    pub api_version: &'static str,
    /// `prefix`.
    pub prefix: &'static str,
    /// `served_fields`.
    pub served_fields: Vec<String>,
    /// `withheld_fields`.
    pub withheld_fields: Vec<String>,
}

// ---------------------------------------------------------------------------------------------
// baskfy_core.public_api constants (the gate's wording is docs/11's, verbatim)
// ---------------------------------------------------------------------------------------------

/// `DATA_REDISTRIBUTION_REVIEW.requirement`.
pub const REVIEW_REQUIREMENT: &str = "Broker-sourced market data is licensed for the licensee's \
own use. Serve derived analytics; do not expose a raw-bar API to third parties without written \
clearance. Get a written data-redistribution opinion before enabling the public API tier. \
(docs/11-nonfunctional.md, Compliance & legal (India).)";
/// `DATA_REDISTRIBUTION_REVIEW.signed_off` — `False`, and only a source change may flip it.
pub const REVIEW_SIGNED_OFF: bool = false;
/// `PUBLIC_API_VERSION`.
pub const PUBLIC_API_VERSION: &str = "v1";
/// `PUBLIC_API_PREFIX`.
pub const PUBLIC_API_PREFIX: &str = "/api/public/v1";
/// `RAW_BAR_FIELDS`.
pub const RAW_BAR_FIELDS: [&str; 15] = [
    "open",
    "high",
    "low",
    "close",
    "close_raw",
    "volume",
    "turnover",
    "adj_factor",
    "vol_day_val",
    "high_1y",
    "high_ath",
    "ma_20",
    "ma_50",
    "ma_100",
    "ma_200",
];

/// `is_public_column(key, unit)`.
#[must_use]
pub fn is_public_column(key: &str, unit: FactorUnit) -> bool {
    !RAW_BAR_FIELDS.contains(&key) && unit != FactorUnit::Price
}

// ---------------------------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------------------------

/// `metrics.publish_latency_seconds`: seconds from the trade date's 15:30 IST close to
/// `finished_at` (negative is not clamped).
#[must_use]
pub fn publish_latency_seconds(trade_date: NaiveDate, finished_at: DateTime<Utc>) -> f64 {
    // 15:30 IST (+05:30) is 10:00 UTC on the same date.
    let close = trade_date
        .and_hms_opt(10, 0, 0)
        .map_or(finished_at, |c| c.and_utc());
    accounts::total_seconds(finished_at - close)
}

fn opt_i64(r: &PgRow, column: &str) -> Result<Option<i64>, Problem> {
    r.try_get::<Option<i64>, _>(column).map_err(db)
}

/// The staff gate plus a query string's FastAPI validation of `limit` (`ge=1, le=max`).
fn limit_param(raw: Option<&str>, max: i64, default: i64) -> Result<i64, Problem> {
    let q = QueryParams::parse(raw);
    let mut v = Validation::new();
    let limit = accounts::int_param(&mut v, "limit", q.get("limit"), Some(1), Some(max));
    v.finish()?;
    Ok(limit.and_then(|n| i64::try_from(n).ok()).unwrap_or(default))
}

/// `RunWithSteps` rendered by `_run_out`.
async fn runs_out(state: &AppState, runs: &[PgRow]) -> Result<Vec<PipelineRunDetailOut>, Problem> {
    if runs.is_empty() {
        return Ok(Vec::new());
    }
    let mut ids = Vec::with_capacity(runs.len());
    for r in runs {
        ids.push(r.try_get::<i64, _>("id").map_err(db)?);
    }
    let steps = sqlx::query(
        "SELECT run_id, step, status, rows_in, rows_out, duration_ms, error FROM pipeline_run_step \
         WHERE run_id = ANY($1) ORDER BY run_id DESC, id ASC",
    )
    .bind(&ids)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut by_run: HashMap<i64, Vec<PipelineStepOut>> = HashMap::new();
    for s in &steps {
        let run_id: i64 = s.try_get("run_id").map_err(db)?;
        by_run.entry(run_id).or_default().push(step_out(s)?);
    }
    let mut out = Vec::with_capacity(runs.len());
    for r in runs {
        let id: i64 = r.try_get("id").map_err(db)?;
        out.push(run_out(r, by_run.remove(&id).unwrap_or_default())?);
    }
    Ok(out)
}

fn step_out(s: &PgRow) -> Result<PipelineStepOut, Problem> {
    Ok(PipelineStepOut {
        step: s.try_get("step").map_err(db)?,
        status: s.try_get("status").map_err(db)?,
        rows_in: opt_i64(s, "rows_in")?,
        rows_out: opt_i64(s, "rows_out")?,
        duration_ms: s
            .try_get::<Option<i32>, _>("duration_ms")
            .map_err(db)?
            .map(i64::from),
        detail: row::opt_jsonb(s, "error").map_err(|e| dbe(&e))?,
    })
}

fn run_out(r: &PgRow, steps: Vec<PipelineStepOut>) -> Result<PipelineRunDetailOut, Problem> {
    let trade_date = row::date(r, "trade_date").map_err(|e| dbe(&e))?;
    let finished_at = row::opt_timestamptz(r, "finished_at").map_err(|e| dbe(&e))?;
    let data_version = opt_i64(r, "data_version")?;
    let latency = match (finished_at, data_version) {
        (Some(f), Some(_)) => Some(publish_latency_seconds(trade_date, f)),
        _ => None,
    };
    Ok(PipelineRunDetailOut {
        id: r.try_get("id").map_err(db)?,
        trade_date,
        status: r.try_get("status").map_err(db)?,
        started_at: row::timestamptz(r, "started_at").map_err(|e| dbe(&e))?,
        finished_at,
        data_version,
        publish_latency_seconds: latency,
        steps,
    })
}

const RUN_COLUMNS: &str = "id, trade_date, status, started_at, finished_at, data_version";

// ---------------------------------------------------------------------------------------------
// Pipeline history
// ---------------------------------------------------------------------------------------------

/// `GET /admin/pipeline/runs?limit=` (`list_pipeline_runs`): newest trade date first, each run
/// with its step rows.
///
/// # Errors
/// The staff gate; 400 for a bad `limit`; 500.
pub async fn list_pipeline_runs(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_staff(&principal)?;
    let limit = limit_param(raw.as_deref(), MAX_RUN_LIMIT, DEFAULT_RUN_LIMIT)?;
    let runs = sqlx::query(&format!(
        "SELECT {RUN_COLUMNS} FROM pipeline_run ORDER BY trade_date DESC, id DESC LIMIT $1"
    ))
    .bind(limit.clamp(1, MAX_RUN_LIMIT))
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let data = runs_out(&state, &runs).await?;
    Ok(wire::json(&PipelineRunListOut { data }))
}

/// `GET /admin/pipeline/runs/{run_id}` (`get_pipeline_run`): `run_id: Path(ge=1)`.
///
/// # Errors
/// The staff gate; 400 for a bad id; 404 `No pipeline run with id N.`; 500.
pub async fn get_pipeline_run(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(raw_id): Path<String>,
) -> Result<Response, Problem> {
    require_staff(&principal)?;
    let mut v = Validation::new();
    let run_id = accounts::int_param(&mut v, "run_id", Some(&raw_id), Some(1), None);
    v.finish()?;
    let Some(run_id) = run_id else {
        return Err(crate::problems::internal_error());
    };
    // `session.get(PipelineRun, run_id)` with an id beyond bigint: asyncpg refuses the bind.
    let run_id = i64::try_from(run_id).map_err(|_| crate::problems::internal_error())?;
    let run = sqlx::query(&format!(
        "SELECT {RUN_COLUMNS} FROM pipeline_run WHERE id = $1"
    ))
    .bind(run_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    let Some(run) = run else {
        return Err(Problem::new(
            ProblemType::NotFound,
            format!("No pipeline run with id {run_id}."),
        ));
    };
    let steps = sqlx::query(
        "SELECT run_id, step, status, rows_in, rows_out, duration_ms, error FROM pipeline_run_step \
         WHERE run_id = $1 ORDER BY id ASC",
    )
    .bind(run_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let steps = steps.iter().map(step_out).collect::<Result<Vec<_>, _>>()?;
    Ok(wire::json(&run_out(&run, steps)?))
}

/// `GET /admin/data-versions?limit=` (`list_data_versions`): every published version, newest
/// first; `current` is the top of it (0 when nothing has published).
///
/// # Errors
/// The staff gate; 400 for a bad `limit`; 500.
pub async fn list_data_versions(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_staff(&principal)?;
    let limit = limit_param(raw.as_deref(), MAX_RUN_LIMIT, DEFAULT_RUN_LIMIT)?;
    let runs = sqlx::query(&format!(
        "SELECT {RUN_COLUMNS} FROM pipeline_run WHERE data_version IS NOT NULL \
         ORDER BY data_version DESC LIMIT $1"
    ))
    .bind(limit.clamp(1, MAX_RUN_LIMIT))
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut data = Vec::with_capacity(runs.len());
    for r in &runs {
        let trade_date = row::date(r, "trade_date").map_err(|e| dbe(&e))?;
        let finished_at = row::opt_timestamptz(r, "finished_at").map_err(|e| dbe(&e))?;
        data.push(DataVersionOut {
            data_version: opt_i64(r, "data_version")?.unwrap_or(0),
            trade_date,
            published_at: finished_at,
            publish_latency_seconds: finished_at.map(|f| publish_latency_seconds(trade_date, f)),
        });
    }
    let current = data.first().map_or(0, |d| d.data_version);
    Ok(wire::json(&DataVersionListOut { data, current }))
}

// ---------------------------------------------------------------------------------------------
// Users
// ---------------------------------------------------------------------------------------------

const USER_COLUMNS: &str = "id, public_id, email::text AS email, name, created_at, \
                            email_verified_at, is_staff, deleted_at";

fn user_out(r: &PgRow) -> Result<AdminUserOut, Problem> {
    Ok(AdminUserOut {
        public_id: r.try_get("public_id").map_err(db)?,
        email: r.try_get("email").map_err(db)?,
        name: r.try_get("name").map_err(db)?,
        created_at: row::timestamptz(r, "created_at").map_err(|e| dbe(&e))?,
        email_verified: row::opt_timestamptz(r, "email_verified_at")
            .map_err(|e| dbe(&e))?
            .is_some(),
        is_staff: r.try_get("is_staff").map_err(db)?,
        deleted_at: row::opt_timestamptz(r, "deleted_at").map_err(|e| dbe(&e))?,
    })
}

/// `GET /admin/users?q=&limit=` (`search_users`): an email fragment (`ILIKE`, the fragment's own
/// `%`/`_` left as wildcards, as Python builds it) or an exact public id; newest account first.
///
/// # Errors
/// The staff gate; 400 for a missing/over-long `q` or a bad `limit`; 500.
pub async fn search_users(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_staff(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let query = v.required(&q, "q");
    let query = accounts::str_param(&mut v, "q", query, Some(1), Some(200));
    let limit = accounts::int_param(
        &mut v,
        "limit",
        q.get("limit"),
        Some(1),
        Some(MAX_USER_HITS),
    );
    v.finish()?;
    let limit = limit.and_then(|n| i64::try_from(n).ok()).unwrap_or(20);
    let needle = accounts::investments::py_strip(query.unwrap_or(""));
    if needle.is_empty() {
        return Ok(wire::json(&AdminUserListOut { data: Vec::new() }));
    }
    let rows = sqlx::query(&format!(
        "SELECT {USER_COLUMNS} FROM app_user WHERE email ILIKE $1 OR public_id = $2 \
         ORDER BY created_at DESC LIMIT $3"
    ))
    .bind(format!("%{needle}%"))
    .bind(needle)
    .bind(limit.clamp(1, MAX_USER_HITS))
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let data = rows.iter().map(user_out).collect::<Result<Vec<_>, _>>()?;
    Ok(wire::json(&AdminUserListOut { data }))
}

async fn email_of(state: &AppState, user_id: i64) -> Result<Option<String>, Problem> {
    sqlx::query_scalar("SELECT email::text FROM app_user WHERE id = $1")
        .bind(user_id)
        .fetch_optional(state.db.pool())
        .await
        .map_err(db)
}

/// `admin._plan_summary`: the active grant's plan and status, else the newest subscription of
/// any status, else nothing.
async fn plan_summary(
    state: &AppState,
    user_id: i64,
) -> Result<(Option<String>, Option<String>), Problem> {
    let moment = utc_now();
    let rows = sqlx::query(
        "SELECT s.status, s.current_period_end, p.code FROM subscription s \
         JOIN plan p ON p.id = s.plan_id WHERE s.user_id = $1 AND s.status = ANY($2) \
         ORDER BY s.started_at DESC, s.id DESC",
    )
    .bind(user_id)
    .bind(crate::entitlements::ENTITLING_STATUSES.as_slice())
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    for r in &rows {
        let end = row::opt_timestamptz(r, "current_period_end").map_err(|e| dbe(&e))?;
        if end.is_none_or(|end| end > moment) {
            return Ok((
                Some(r.try_get("code").map_err(db)?),
                Some(r.try_get("status").map_err(db)?),
            ));
        }
    }
    let latest = sqlx::query(
        "SELECT s.status, p.code FROM subscription s JOIN plan p ON p.id = s.plan_id \
         WHERE s.user_id = $1 ORDER BY s.started_at DESC, s.id DESC LIMIT 1",
    )
    .bind(user_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    match latest {
        Some(r) => Ok((
            Some(r.try_get("code").map_err(db)?),
            Some(r.try_get("status").map_err(db)?),
        )),
        None => Ok((None, None)),
    }
}

/// `GET /admin/users/{public_id}` (`get_user`): one account with its **effective**
/// entitlements (resolved by the same `entitlements_for` every gated route uses) and its
/// overrides, by feature.
///
/// # Errors
/// The staff gate; 404 `No account with id '…'.`; 500.
pub async fn get_user(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(public_id): Path<String>,
) -> Result<Response, Problem> {
    require_staff(&principal)?;
    let found = sqlx::query(&format!(
        "SELECT {USER_COLUMNS} FROM app_user WHERE public_id = $1"
    ))
    .bind(&public_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    let Some(user) = found else {
        return Err(Problem::new(
            ProblemType::NotFound,
            format!(
                "No account with id {}.",
                baskfy_providers::py::repr_str(&public_id)
            ),
        ));
    };
    let user_id: i64 = user.try_get("id").map_err(db)?;
    let subject = Principal {
        kind: PrincipalKind::User,
        user_id: Some(user_id),
        public_id: Some(user.try_get("public_id").map_err(db)?),
        email: Some(user.try_get("email").map_err(db)?),
        is_staff: user.try_get("is_staff").map_err(db)?,
    };
    let (plan_code, subscription_status) = plan_summary(&state, user_id).await?;
    let overrides = sqlx::query(
        "SELECT feature, effect, value, reason, granted_by_user_id, expires_at, created_at \
         FROM entitlement_override WHERE user_id = $1 ORDER BY feature ASC",
    )
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let screen_count: i64 = sqlx::query_scalar("SELECT count(*) FROM screen WHERE user_id = $1")
        .bind(user_id)
        .fetch_one(state.db.pool())
        .await
        .map_err(db)?;
    let entitlements = entitlements_for(&state.db, &subject, &state.settings).await?;
    // `_detail_out`: one author lookup per distinct granter, "(deleted account)" when gone.
    let mut authors: HashMap<i64, String> = HashMap::new();
    for o in &overrides {
        let by: i64 = o.try_get("granted_by_user_id").map_err(db)?;
        if let std::collections::hash_map::Entry::Vacant(slot) = authors.entry(by) {
            let email = email_of(&state, by).await?;
            slot.insert(email.unwrap_or_else(|| "(deleted account)".to_owned()));
        }
    }
    let now = utc_now();
    let mut out = Vec::with_capacity(overrides.len());
    for o in &overrides {
        let by: i64 = o.try_get("granted_by_user_id").map_err(db)?;
        let effect: String = o.try_get("effect").map_err(db)?;
        let expires_at = row::opt_timestamptz(o, "expires_at").map_err(|e| dbe(&e))?;
        out.push(EntitlementOverrideOut {
            feature: o.try_get("feature").map_err(db)?,
            effect: if effect == "grant" { "grant" } else { "revoke" },
            value: opt_i64(o, "value")?,
            reason: o.try_get("reason").map_err(db)?,
            granted_by: authors.get(&by).cloned(),
            expires_at,
            created_at: row::timestamptz(o, "created_at").map_err(|e| dbe(&e))?,
            active: expires_at.is_none_or(|x| x > now),
        });
    }
    Ok(wire::json(&AdminUserDetailOut {
        user: user_out(&user)?,
        plan_code,
        subscription_status,
        screen_count,
        entitlements: entitlements.as_out(),
        overrides: out,
    }))
}

// ---------------------------------------------------------------------------------------------
// Audit trail, public-API gate
// ---------------------------------------------------------------------------------------------

/// `GET /admin/actions?limit=` (`list_actions`): the staff audit trail, newest first.
///
/// # Errors
/// The staff gate; 400 for a bad `limit`; 500.
pub async fn list_actions(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_staff(&principal)?;
    let limit = limit_param(raw.as_deref(), 200, 50)?;
    let rows = sqlx::query(
        "SELECT actor_user_id, action, target, detail, created_at FROM admin_action \
         ORDER BY created_at DESC, id DESC LIMIT $1",
    )
    .bind(limit.clamp(1, 200))
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut actors: HashMap<i64, String> = HashMap::new();
    for r in &rows {
        let actor: i64 = r.try_get("actor_user_id").map_err(db)?;
        if let std::collections::hash_map::Entry::Vacant(slot) = actors.entry(actor) {
            let email = email_of(&state, actor).await?;
            slot.insert(email.unwrap_or_else(|| "(deleted account)".to_owned()));
        }
    }
    let mut data = Vec::with_capacity(rows.len());
    for r in &rows {
        let actor: i64 = r.try_get("actor_user_id").map_err(db)?;
        data.push(AdminActionOut {
            action: r.try_get("action").map_err(db)?,
            target: r.try_get("target").map_err(db)?,
            actor: actors.get(&actor).cloned(),
            detail: row::opt_jsonb(r, "detail").map_err(|e| dbe(&e))?,
            created_at: row::timestamptz(r, "created_at").map_err(|e| dbe(&e))?,
        });
    }
    Ok(wire::json(&AdminActionListOut { data }))
}

/// `GET /admin/public-api` (`public_api_gate`): two constants and one setting, no database.
///
/// # Errors
/// The staff gate.
pub async fn public_api_gate(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    require_staff(&principal)?;
    let columns = baskfy_core::factor_registry::columns();
    let served_fields = columns
        .iter()
        .filter(|c| is_public_column(&c.key, c.unit))
        .map(|c| c.key.clone())
        .collect();
    let withheld_fields = columns
        .iter()
        .filter(|c| !is_public_column(&c.key, c.unit))
        .map(|c| c.key.clone())
        .collect();
    Ok(wire::json(&PublicApiGateOut {
        flag_enabled: state.settings.public_api_enabled,
        review_signed_off: REVIEW_SIGNED_OFF,
        serving: state.settings.public_api_enabled && REVIEW_SIGNED_OFF,
        requirement: REVIEW_REQUIREMENT,
        opinion_reference: "",
        signed_off_on: "",
        signed_off_by: "",
        api_version: PUBLIC_API_VERSION,
        prefix: PUBLIC_API_PREFIX,
        served_fields,
        withheld_fields,
    }))
}

/// The ported `/admin` routes, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route(
            "/admin/pipeline/runs/{trade_date}/rerun",
            post(writes::rerun_pipeline),
        )
        .route(
            "/admin/instruments/{symbol}/reprocess",
            post(writes::reprocess_instrument),
        )
        .route(
            "/admin/users/{public_id}/entitlements",
            put(writes::put_entitlement_override),
        )
        .route(
            "/admin/users/{public_id}/entitlements/{feature}",
            delete(writes::delete_entitlement_override),
        )
        .route("/admin/pipeline/runs", get(list_pipeline_runs))
        .route("/admin/pipeline/runs/{run_id}", get(get_pipeline_run))
        .route(
            "/admin/resync",
            get(resync::inspect_resync).post(writes::start_resync),
        )
        .route("/admin/data-versions", get(list_data_versions))
        .route("/admin/providers", get(providers::get_provider_health))
        .route("/admin/users", get(search_users))
        .route("/admin/users/{public_id}", get(get_user))
        .route("/admin/actions", get(list_actions))
        .route("/admin/public-api", get(public_api_gate))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn latency_is_measured_from_the_1530_ist_close() {
        let d = NaiveDate::from_ymd_opt(2026, 10, 1).unwrap();
        let f = DateTime::parse_from_rfc3339("2026-10-01T10:00:00.500000Z")
            .unwrap()
            .with_timezone(&Utc);
        // 15:30 IST = 10:00 UTC.
        assert!((publish_latency_seconds(d, f) - 0.5).abs() < 1e-12);
    }

    #[test]
    fn price_columns_and_raw_bars_are_withheld() {
        assert!(!is_public_column("close_raw", FactorUnit::Ratio));
        assert!(!is_public_column("anything", FactorUnit::Price));
        assert!(is_public_column("ret_12m", FactorUnit::Percent));
    }
}
