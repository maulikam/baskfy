//! ``/screens/*`` — docs/07 §Screens and §"Running a screen" (Prompt 7 deliverable 2).
//!
//! Ports the **GET** routes of `baskfy_api.routers.screens` (`APIRouter(prefix="/screens",
//! tags=["screens"])`):
//!
//! * `GET /screens` — the caller's screens plus the example screens (anonymous: examples only);
//! * `GET /screens/{public_id}` — one visible screen;
//! * `GET /screens/{public_id}/runs` — keyset-paginated run summaries;
//! * `GET /screens/{public_id}/csv` — the entitlement-gated export ([`csv`]).
//!
//! The CRUD writes are ported at wave 4c ([`writes`]); the screener-running routes
//! (`POST /screens/preview`, `/explain`, `/selection`, `/screens/{public_id}/run`) in `runs`.
//!
//! Visibility, as Python states it once: a screen with `user_id IS NULL` is a system/example
//! screen everyone can read; a screen with a `user_id` is visible only to that user, and
//! not-yours is a **404**, never a 403. **No route may place, modify or cancel an order**
//! (law 2).

#[path = "accounts/mod.rs"]
pub(crate) mod accounts;
pub(crate) mod body_model;
mod runs;
pub(crate) mod write_kit;
mod writes;

use axum::Router;
use axum::extract::{Extension, Path, RawQuery, State};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use baskfy_core::screener::{Payload, ScreenDefinition};
use baskfy_db::{OrderedJson, row};
use chrono::{DateTime, NaiveDate, Utc};
use serde::Serialize;
use sqlx::Row;
use sqlx::postgres::PgRow;

use crate::auth::Principal;
use crate::problems::{Problem, ProblemType, internal_error, not_found};
use crate::query::{QueryParams, Validation};
use crate::state::AppState;
use crate::wire;
use accounts::{db, dbe};

/// `schemas.DEFAULT_PAGE_SIZE`.
pub const DEFAULT_PAGE_SIZE: i64 = 100;
/// `schemas.MAX_PAGE_SIZE` (`Limit = Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE)]`).
pub const MAX_PAGE_SIZE: i64 = 500;

/// `ScreenOut`, field for field.
#[derive(Debug, Clone, Serialize)]
pub struct ScreenOut {
    /// `public_id`.
    pub public_id: String,
    /// `name`.
    pub name: String,
    /// `definition`: `ScreenDefinition.model_dump(mode="json", by_alias=True)`.
    pub definition: OrderedJson,
    /// `columns`.
    pub columns: Vec<String>,
    /// `is_example`.
    pub is_example: bool,
    /// `editable`.
    pub editable: bool,
    /// `created_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub created_at: DateTime<Utc>,
    /// `updated_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub updated_at: DateTime<Utc>,
}

/// `ScreenListOut`.
#[derive(Debug, Clone, Serialize)]
pub struct ScreenListOut {
    /// `data`.
    pub data: Vec<ScreenOut>,
}

/// `ScreenRunSummaryOut`.
#[derive(Debug, Clone, Serialize)]
pub struct ScreenRunSummaryOut {
    /// `as_of`.
    #[serde(serialize_with = "wire::ser::date")]
    pub as_of: NaiveDate,
    /// `definition_hash`.
    pub definition_hash: String,
    /// `result_count`.
    pub result_count: i64,
    /// `created_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub created_at: DateTime<Utc>,
}

/// `ScreenRunPage`.
#[derive(Debug, Clone, Serialize)]
pub struct ScreenRunPage {
    /// `data`.
    pub data: Vec<ScreenRunSummaryOut>,
    /// `next_cursor`.
    pub next_cursor: Option<String>,
}

/// One `screen` row, as the handlers need it.
#[derive(Debug, Clone)]
pub struct ScreenRow {
    /// `screen.id`.
    pub id: i64,
    /// `screen.public_id`.
    pub public_id: String,
    /// `screen.user_id`.
    pub user_id: Option<i64>,
    /// `screen.name`.
    pub name: String,
    /// `screen.definition` (jsonb).
    pub definition: OrderedJson,
    /// `screen.columns` (jsonb).
    pub columns: OrderedJson,
    /// `screen.is_example`.
    pub is_example: bool,
    /// `screen.created_at`.
    pub created_at: DateTime<Utc>,
    /// `screen.updated_at`.
    pub updated_at: DateTime<Utc>,
}

const SCREEN_COLUMNS: &str = "id, public_id, user_id, name, definition, columns, is_example, \
                              created_at, updated_at";

fn screen_row(r: &PgRow) -> Result<ScreenRow, Problem> {
    Ok(ScreenRow {
        id: r.try_get("id").map_err(db)?,
        public_id: r.try_get("public_id").map_err(db)?,
        user_id: r.try_get("user_id").map_err(db)?,
        name: r.try_get("name").map_err(db)?,
        definition: row::jsonb(r, "definition").map_err(|e| dbe(&e))?,
        columns: row::jsonb(r, "columns").map_err(|e| dbe(&e))?,
        is_example: r.try_get("is_example").map_err(db)?,
        created_at: row::timestamptz(r, "created_at").map_err(|e| dbe(&e))?,
        updated_at: row::timestamptz(r, "updated_at").map_err(|e| dbe(&e))?,
    })
}

/// A jsonb value as the `Payload` pydantic's Python-mode validation would see (`json.loads`).
#[must_use]
pub fn payload_of(value: &OrderedJson) -> Payload {
    match value {
        OrderedJson::Null => Payload::Null,
        OrderedJson::Bool(b) => Payload::Bool(*b),
        OrderedJson::Int(i) => Payload::Int(*i),
        // Beyond i64: a Python int pydantic would see; no stored definition holds one.
        OrderedJson::UInt(u) => Payload::Opaque(format!("int:{u}")),
        OrderedJson::BigInt(big) => Payload::Opaque(format!("int:{big}")),
        OrderedJson::Float(f) => Payload::Float(*f),
        OrderedJson::Str(s) => Payload::Str(s.clone()),
        OrderedJson::Array(items) => Payload::List(items.iter().map(payload_of).collect()),
        OrderedJson::Object(entries) => Payload::Map(
            entries
                .iter()
                .map(|(k, v)| (k.clone(), payload_of(v)))
                .collect(),
        ),
    }
}

/// A `Payload` in JSON mode (after `model_dump(mode="json")` every value is JSON-native).
#[must_use]
pub fn json_of(value: &Payload) -> OrderedJson {
    match value {
        Payload::Null | Payload::Opaque(_) => OrderedJson::Null,
        Payload::Bool(b) => OrderedJson::Bool(*b),
        Payload::Int(i) => OrderedJson::Int(*i),
        Payload::Float(f) => OrderedJson::Float(*f),
        Payload::Str(s) => OrderedJson::Str(s.clone()),
        // `json_payload` writes every Decimal as its `str()` already; a bare Decimal cannot
        // reach here, and `format_f` is the closest spelling if one ever does.
        Payload::Decimal(d) => OrderedJson::Str(d.format_f()),
        Payload::Date(d) => OrderedJson::Str(wire::py_date(*d)),
        Payload::List(items) => OrderedJson::Array(items.iter().map(json_of).collect()),
        Payload::Map(entries) => OrderedJson::Object(
            entries
                .iter()
                .map(|(k, v)| (k.clone(), json_of(v)))
                .collect(),
        ),
    }
}

/// `PHASE_2_DEFAULTS`, in `ScreenDefinition`'s field order (they are its last five fields).
fn phase_2_defaults() -> [(&'static str, OrderedJson); 5] {
    [
        ("ranking_terms", OrderedJson::Array(Vec::new())),
        ("family_weights", OrderedJson::Null),
        ("missing_data", OrderedJson::Str("penalize".to_owned())),
        ("factor_ranges", OrderedJson::Array(Vec::new())),
        ("regime_in", OrderedJson::Null),
    ]
}

/// `ScreenDefinition.model_dump(mode="json", by_alias=True)`: every field, Phase-2 ones
/// included. `json_payload` (the hash input) drops a Phase-2 field at its default; the response
/// model does not, so the defaults are put back in their field-order place.
#[must_use]
pub fn definition_dump(definition: &ScreenDefinition) -> OrderedJson {
    let OrderedJson::Object(entries) = json_of(&definition.json_payload()) else {
        return OrderedJson::Null;
    };
    let defaults = phase_2_defaults();
    let is_phase_2 = |k: &str| defaults.iter().any(|(name, _)| *name == k);
    let mut out: Vec<(String, OrderedJson)> = entries
        .iter()
        .filter(|(k, _)| !is_phase_2(k))
        .cloned()
        .collect();
    for (name, default) in defaults {
        let value = entries
            .iter()
            .find(|(k, _)| k == name)
            .map_or(default, |(_, v)| v.clone());
        out.push((name.to_owned(), value));
    }
    OrderedJson::Object(out)
}

/// `ScreenDefinition.model_validate(screen.definition)` then its JSON dump. A stored definition
/// that no longer validates is a `ValidationError` outside any handler: the 500 catch-all.
///
/// # Errors
/// 500 when the stored definition does not validate.
pub fn stored_definition(value: &OrderedJson) -> Result<ScreenDefinition, Problem> {
    ScreenDefinition::model_validate(&payload_of(value)).map_err(|e| {
        crate::logging::error(
            "baskfy_api.app",
            "unhandled error",
            &[("error", &e.to_string())],
        );
        internal_error()
    })
}

fn is_visible(screen: &ScreenRow, principal: &Principal) -> bool {
    visible_to(screen.user_id, principal)
}

/// `is_visible(screen, principal)` on the owner alone: a system screen (`user_id IS NULL`) is
/// everyone's, a private one its owner's only. `GET /instruments/{symbol}/rank-history` uses it
/// too (DECISIONS-RS R5.28).
#[must_use]
pub fn visible_to(owner: Option<i64>, principal: &Principal) -> bool {
    owner.is_none() || owner == principal.user_id
}

fn is_editable(screen: &ScreenRow, principal: &Principal) -> bool {
    screen.user_id.is_some() && screen.user_id == principal.user_id
}

fn string_list(value: &OrderedJson) -> Vec<String> {
    match value {
        OrderedJson::Array(items) => items
            .iter()
            .map(|i| match i {
                OrderedJson::Str(s) => s.clone(),
                other => other.to_python_json().unwrap_or_default(),
            })
            .collect(),
        _ => Vec::new(),
    }
}

/// `_to_out(screen, principal)`.
///
/// # Errors
/// 500 when the stored definition does not validate.
pub fn to_out(screen: &ScreenRow, principal: &Principal) -> Result<ScreenOut, Problem> {
    let definition = stored_definition(&screen.definition)?;
    Ok(ScreenOut {
        public_id: screen.public_id.clone(),
        name: screen.name.clone(),
        definition: definition_dump(&definition),
        columns: string_list(&screen.columns),
        is_example: screen.is_example,
        editable: is_editable(screen, principal),
        created_at: screen.created_at,
        updated_at: screen.updated_at,
    })
}

/// `_load_visible`: the screen, or `not_found("screen", public_id)` when absent or not yours.
///
/// # Errors
/// 404; 500 for a database failure.
pub async fn load_visible(
    state: &AppState,
    public_id: &str,
    principal: &Principal,
) -> Result<ScreenRow, Problem> {
    let found = sqlx::query(&format!(
        "SELECT {SCREEN_COLUMNS} FROM screen WHERE public_id = $1"
    ))
    .bind(public_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    let screen = match found {
        Some(r) => screen_row(&r)?,
        None => return Err(not_found("screen", public_id)),
    };
    if !is_visible(&screen, principal) {
        return Err(not_found("screen", public_id));
    }
    Ok(screen)
}

/// `GET /screens` (`list_screens`): user screens + example screens, examples first, then
/// most-recently updated, then id.
///
/// # Errors
/// 500 for a database failure or a stored definition that no longer validates.
pub async fn list_screens(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let order = "ORDER BY is_example DESC, updated_at DESC, id ASC";
    let rows =
        match principal.user_id {
            None => {
                sqlx::query(&format!(
                    "SELECT {SCREEN_COLUMNS} FROM screen WHERE user_id IS NULL {order}"
                ))
                .fetch_all(state.db.pool())
                .await
            }
            Some(uid) => sqlx::query(&format!(
                "SELECT {SCREEN_COLUMNS} FROM screen WHERE user_id IS NULL OR user_id = $1 {order}"
            ))
            .bind(uid)
            .fetch_all(state.db.pool())
            .await,
        }
        .map_err(db)?;
    let mut data = Vec::with_capacity(rows.len());
    for r in &rows {
        data.push(to_out(&screen_row(r)?, &principal)?);
    }
    Ok(wire::json(&ScreenListOut { data }))
}

/// `GET /screens/{public_id}` (`get_screen`).
///
/// # Errors
/// 404 when absent or not yours; 500 for a database failure.
pub async fn get_screen(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(public_id): Path<String>,
) -> Result<Response, Problem> {
    let screen = load_visible(&state, &public_id, &principal).await?;
    Ok(wire::json(&to_out(&screen, &principal)?))
}

/// `GET /screens/{public_id}/runs?limit=&cursor=` (`list_screen_runs`): keyset pagination on
/// `screen_run.id` descending.
///
/// # Errors
/// 400 for a bad `limit`; 404 for an invisible screen or a cursor that is not an integer; 500
/// for a database failure.
pub async fn list_screen_runs(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(public_id): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let limit = accounts::int_param(
        &mut v,
        "limit",
        q.get("limit"),
        Some(1),
        Some(MAX_PAGE_SIZE),
    );
    v.finish()?;
    let limit = limit
        .and_then(|n| i64::try_from(n).ok())
        .unwrap_or(DEFAULT_PAGE_SIZE);
    let cursor = q.get("cursor");
    let screen = load_visible(&state, &public_id, &principal).await?;
    let rows = match cursor {
        Some(c) => {
            let Some(n) = accounts::python_int(c) else {
                return Err(Problem::new(
                    ProblemType::NotFound,
                    format!(
                        "{} is not a valid pagination cursor.",
                        baskfy_providers::py::repr_str(c)
                    ),
                ));
            };
            // Beyond bigint, asyncpg refuses the bind: an unhandled DBAPIError, the 500.
            let n = i64::try_from(n).map_err(|_| internal_error())?;
            sqlx::query(
                "SELECT id, as_of, definition_hash, result_count, created_at FROM screen_run \
                 WHERE screen_id = $1 AND id < $2 ORDER BY id DESC LIMIT $3",
            )
            .bind(screen.id)
            .bind(n)
            .bind(limit + 1)
            .fetch_all(state.db.pool())
            .await
        }
        None => {
            sqlx::query(
                "SELECT id, as_of, definition_hash, result_count, created_at FROM screen_run \
                 WHERE screen_id = $1 ORDER BY id DESC LIMIT $2",
            )
            .bind(screen.id)
            .bind(limit + 1)
            .fetch_all(state.db.pool())
            .await
        }
    }
    .map_err(db)?;
    let has_more = i64::try_from(rows.len()).unwrap_or(i64::MAX) > limit;
    let page = &rows[..rows.len().min(usize::try_from(limit).unwrap_or(usize::MAX))];
    let mut data = Vec::with_capacity(page.len());
    let mut last_id: Option<i64> = None;
    for r in page {
        let id: i64 = r.try_get("id").map_err(db)?;
        last_id = Some(id);
        data.push(ScreenRunSummaryOut {
            as_of: row::date(r, "as_of").map_err(|e| dbe(&e))?,
            definition_hash: r.try_get("definition_hash").map_err(db)?,
            result_count: r
                .try_get::<i32, _>("result_count")
                .map(i64::from)
                .map_err(db)?,
            created_at: row::timestamptz(r, "created_at").map_err(|e| dbe(&e))?,
        });
    }
    let next_cursor = if has_more {
        last_id.map(|i| i.to_string())
    } else {
        None
    };
    Ok(wire::json(&ScreenRunPage { data, next_cursor }))
}

/// `screener.DATA_START_DATE`: docs/01 §2.13's "from 1 Nov 2024".
pub const DATA_START_DATE: (i32, u32, u32) = (2024, 11, 1);

/// `AsOfResolution`.
#[derive(Debug, Clone, Copy)]
pub struct AsOfResolution {
    /// `requested`.
    pub requested: Option<NaiveDate>,
    /// `as_of`.
    pub as_of: NaiveDate,
    /// `latest_published`.
    pub latest_published: NaiveDate,
}

/// `screener.current_data_version`: the highest version ever published, `0` before the first.
///
/// # Errors
/// 500 for a database failure.
pub async fn current_data_version(state: &AppState) -> Result<i64, Problem> {
    let v: Option<i64> = sqlx::query_scalar(
        "SELECT max(data_version) FROM pipeline_run WHERE data_version IS NOT NULL",
    )
    .fetch_one(state.db.pool())
    .await
    .map_err(db)?;
    Ok(v.unwrap_or(0))
}

/// `screener.resolve_as_of(session, requested)`: the latest published date by default; a date
/// inside the servable range snaps **backwards** to a trading day; outside it is 422
/// `no-trading-day`; nothing published at all is 503 `pipeline-degraded`.
///
/// # Errors
/// As described; 500 for a database failure.
pub async fn resolve_as_of(
    state: &AppState,
    requested: Option<NaiveDate>,
) -> Result<AsOfResolution, Problem> {
    let (y, m, d) = DATA_START_DATE;
    let data_start = NaiveDate::from_ymd_opt(y, m, d).ok_or_else(internal_error)?;
    let latest: Option<NaiveDate> = sqlx::query_scalar(
        "SELECT max(trade_date) FROM pipeline_run WHERE data_version IS NOT NULL",
    )
    .fetch_one(state.db.pool())
    .await
    .map_err(db)?;
    let Some(latest) = latest else {
        return Err(crate::problems::pipeline_degraded(
            "no pipeline_run has been published, so factor_daily has no trustworthy as-of date",
        ));
    };
    let Some(requested) = requested else {
        return Ok(AsOfResolution {
            requested: None,
            as_of: latest,
            latest_published: latest,
        });
    };
    if requested > latest || requested < data_start {
        return Err(crate::problems::no_trading_day(
            requested, data_start, latest,
        ));
    }
    let snapped: Option<NaiveDate> = sqlx::query_scalar(
        "SELECT max(date) FROM trading_day WHERE exchange_id = 1 AND is_trading_day IS true \
         AND date <= $1",
    )
    .bind(requested)
    .fetch_one(state.db.pool())
    .await
    .map_err(db)?;
    match snapped {
        Some(s) if s >= data_start => Ok(AsOfResolution {
            requested: Some(requested),
            as_of: s,
            latest_published: latest,
        }),
        _ => Err(crate::problems::no_trading_day(
            requested, data_start, latest,
        )),
    }
}

/// `Entitlements.require_universe(code)`: the ₹0 tier's restriction.
pub(crate) fn require_universe(
    e: &crate::entitlements::Entitlements,
    code: &str,
) -> Result<(), Problem> {
    let Some(allowed) = &e.universes else {
        return Ok(());
    };
    if allowed.contains(code) {
        return Ok(());
    }
    let mut values: Vec<&str> = allowed.iter().map(String::as_str).collect();
    values.sort_unstable();
    let english = match values.as_slice() {
        [] => "no universe".to_owned(),
        [one] => (*one).to_owned(),
        [init @ .., last] => format!("{} and {last}", init.join(", ")),
    };
    Err(crate::problems::payment_required(
        "universes",
        Some(&format!(
            "Your plan can screen {english} only, not {}.",
            baskfy_providers::py::repr_str(code)
        )),
    ))
}

pub(crate) fn require_feature(
    e: &crate::entitlements::Entitlements,
    feature: &str,
) -> Result<(), Problem> {
    if e.allows(feature) {
        Ok(())
    } else {
        Err(crate::problems::payment_required(feature, None))
    }
}

/// `GET /screens/{public_id}/csv?as_of=` (`export_screen_csv`): docs/13's 93-column export,
/// gated by `export_csv` (and `historical_ranks` for a past date, `custom_columns` for a
/// non-default column set).
///
/// # Errors
/// 400 for a bad `as_of`; 402 for a missing entitlement; 404 for an invisible screen; 422
/// `no-trading-day`; 503 before the first publish; 500.
pub async fn export_screen_csv(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(public_id): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    use axum::http::{HeaderValue, StatusCode, header};
    let entitlements =
        crate::entitlements::entitlements_for(&state.db, &principal, &state.settings).await?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let as_of = v.date("as_of", q.get("as_of"));
    v.finish()?;
    require_feature(&entitlements, "export_csv")?;
    let screen = load_visible(&state, &public_id, &principal).await?;
    let definition = stored_definition(&screen.definition)?;
    require_universe(&entitlements, &definition.index)?;
    let resolution = resolve_as_of(&state, as_of.or(definition.historical_date)).await?;
    if resolution.requested.is_some() && resolution.as_of != resolution.latest_published {
        require_feature(&entitlements, "historical_ranks")?;
    }
    let columns = string_list(&screen.columns);
    let defaults: [&str; 3] = baskfy_core::screener::DEFAULT_RESULT_COLUMNS;
    if columns.iter().any(|c| !defaults.contains(&c.as_str())) {
        require_feature(&entitlements, "custom_columns")?;
    }
    let filename = accounts::csv_export::filename_for(&screen.name, resolution.as_of);
    let data_version = current_data_version(&state).await?;

    // stream_screen_csv: BOM + header, then every row of build_export_query.
    // The statement holds `Rc`s (not `Send`): compiled and dropped before the first await.
    let (sql, args) = {
        let statement = baskfy_core::screener::build_export_query(
            &definition,
            resolution.as_of,
            baskfy_core::screener::MAX_RESULT_ROWS,
        )
        .map_err(|e| {
            crate::logging::error(
                "baskfy_api.app",
                "unhandled error",
                &[("error", &e.to_string())],
            );
            internal_error()
        })?;
        accounts::csv_export::executable(&statement.compile())?
    };
    let rows = sqlx::query_with(&sql, args)
        .fetch_all(state.db.pool())
        .await
        .map_err(db)?;
    let mut body = String::new();
    body.push_str(accounts::csv_export::BOM);
    body.push_str(&accounts::csv_export::header_line());
    body.push_str(accounts::csv_export::LINE_ENDING);
    for r in &rows {
        body.push_str(&accounts::csv_export::export_row(r, resolution.as_of)?);
        body.push_str(accounts::csv_export::LINE_ENDING);
    }
    let mut response = (StatusCode::OK, body).into_response();
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/csv; charset=utf-8"),
    );
    let disposition = format!("attachment; filename=\"{filename}\"");
    headers.insert(
        header::CONTENT_DISPOSITION,
        HeaderValue::from_str(&disposition).map_err(|_| internal_error())?,
    );
    for (name, value) in
        crate::http_cache::snapshot_headers(Some(resolution.as_of), Some(data_version))
    {
        if let Ok(v) = HeaderValue::from_str(&value) {
            headers.insert(name, v);
        }
    }
    Ok(response)
}

/// The ported `/screens` routes (the GETs and the CRUD writes), relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route(
            "/screens/{public_id}/duplicate",
            post(writes::duplicate_screen),
        )
        .route("/screens/{public_id}/run", post(runs::run_saved_screen))
        .route(
            "/screens/preview",
            post(runs::preview_screen)
                .get(runs::as_id::preview_get)
                .patch(runs::as_id::preview_patch)
                .delete(runs::as_id::preview_delete),
        )
        .route(
            "/screens/explain",
            post(runs::explain_screen_row)
                .get(runs::as_id::explain_get)
                .patch(runs::as_id::explain_patch)
                .delete(runs::as_id::explain_delete),
        )
        .route(
            "/screens/selection",
            post(runs::select_over_screen)
                .get(runs::as_id::selection_get)
                .patch(runs::as_id::selection_patch)
                .delete(runs::as_id::selection_delete),
        )
        .route("/screens", get(list_screens).post(writes::create_screen))
        .route(
            "/screens/{public_id}",
            get(get_screen)
                .patch(writes::update_screen)
                .delete(writes::delete_screen),
        )
        .route("/screens/{public_id}/runs", get(list_screen_runs))
        .route("/screens/{public_id}/csv", get(export_screen_csv))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn the_response_dump_keeps_phase_2_defaults_in_field_order() {
        let def = ScreenDefinition::new("nifty-500", "ret_12m");
        let dumped = definition_dump(&def).to_python_json().unwrap();
        assert!(
            dumped.ends_with(
                r#""custom_filters":[],"ranking_terms":[],"family_weights":null,"missing_data":"penalize","factor_ranges":[],"regime_in":null}"#
            ),
            "{dumped}"
        );
        assert!(
            dumped
                .starts_with(r#"{"index":"nifty-500","sort_by":"ret_12m","sort_direction":"desc""#)
        );
    }
}
