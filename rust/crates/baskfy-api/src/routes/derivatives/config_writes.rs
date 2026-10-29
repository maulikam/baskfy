//! The options tab's and the F&O page's money-free writes (lane writes-sleeves):
//! `POST /options/event-day`, `DELETE /options/event-day`, `PATCH /options/config` (OP5 `05` §2's
//! two mutations) and `PATCH /fno/config` (FO5) — ports of the handlers in `routers/options.py`
//! and `routers/fno.py`, with `options_read.add_event_day/remove_event_day`,
//! `options_settings` and `fno_settings`.
//!
//! An event day can only make the book *do less*; a settings patch writes numbers under server
//! ceilings (every part checked before any row changes), and the system-owned fields
//! (`paused_until`/`paused_reason`) and every execution flag are not fields of the models. No
//! route here builds, confirms or places an order (law 2).

use std::cmp::Ordering;

use axum::extract::{Extension, RawQuery, Request, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use baskfy_db::OrderedJson;
use chrono::{NaiveDate, NaiveTime};
use rust_decimal::{Decimal, RoundingStrategy};
use sqlx::Row;

use super::shared::scoped_sole_user_id;
use crate::auth::{Principal, require_authenticated};
use crate::body::receive;
use crate::problems::{
    Problem, bad_request, internal_error, not_found, setting_above_ceiling, underlying_not_allowed,
};
use crate::query::{QueryParams, Validation};
use crate::routes::sleeves::common::{db, py_repr_json, py_str_json};
use crate::routes::sleeves::pyd::{
    Bound, Default as D, Model, PV, PyDec, Ty, dec_const, iso_date, validate_body,
};
use crate::routes::sleeves::writes::{Stored, now_micros, pool, read_body, write_changes};
use crate::session::{self, Tx, WritePool};
use crate::state::AppState;
use crate::wire;

/// `baskfy_core.models.options.OP_EVENT_REASONS`.
const OP_EVENT_REASONS: [&str; 4] = ["RBI_POLICY", "UNION_BUDGET", "ELECTION_RESULT", "MANUAL"];
/// `options_read.USER_SOURCE`.
const USER_SOURCE: &str = "USER";
/// `options_settings.ALLOWED_UNDERLYINGS`.
const ALLOWED_UNDERLYINGS: [&str; 1] = ["NIFTY"];
/// `SleeveGroup`.
const OPTION_GROUPS: [&str; 4] = ["O1M", "O1W", "O2", "O3"];
/// `FoSleeveGroup`.
const FNO_GROUPS: [&str; 3] = ["F1", "F2", "F3"];

// ---------------------------------------------------------------------------------------------
// Event days
// ---------------------------------------------------------------------------------------------

fn event_day_in() -> Model {
    Model {
        fields: vec![
            ("date", Ty::Date, D::Required),
            ("reason", Ty::str_len(None, None), D::Unset),
            ("note", Ty::str_len(None, Some(280)).opt(), D::Unset),
        ],
        forbid_extra: true,
        before: None,
        after: None,
    }
}

/// `OptionsEventDayOut` (pydantic's JSON).
fn event_out(
    day: NaiveDate,
    reason: &str,
    source: &str,
    source_url: Option<&str>,
    note: Option<&str>,
) -> OrderedJson {
    let s = |v: &str| OrderedJson::Str(v.to_owned());
    let o = |v: Option<&str>| v.map_or(OrderedJson::Null, s);
    OrderedJson::Object(vec![
        ("date".to_owned(), s(&iso_date(day))),
        ("reason".to_owned(), s(reason)),
        ("source".to_owned(), s(source)),
        ("source_url".to_owned(), o(source_url)),
        ("note".to_owned(), o(note)),
        (
            "removable".to_owned(),
            OrderedJson::Bool(source == USER_SOURCE),
        ),
    ])
}

/// `POST /options/event-day`: a day no sleeve trades (it can only make the book do less).
///
/// # Errors
/// 400 (body; an unknown reason; the day already an event day), 401, 404/503 (tenant), 500.
pub async fn post_event_day(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let (content_type, bytes) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    let principal = require_authenticated(&principal)?.clone();
    let body = validate_body(&received, &event_day_in()).map_err(|f| f.into_problem(Vec::new()))?;
    let user_id = scoped_sole_user_id(&state.db, principal.user_id, None).await?;
    let Some(PV::Date(day)) = body.field("date").cloned() else {
        return Err(internal_error());
    };
    let reason = match body.field("reason") {
        Some(PV::Str(r)) => r.clone(),
        _ => "MANUAL".to_owned(),
    };
    let note = match body.field("note") {
        Some(PV::Str(n)) => Some(n.clone()),
        _ => None,
    };
    if !OP_EVENT_REASONS.contains(&reason.as_str()) {
        return Err(bad_request(
            &format!("reason must be one of {}", OP_EVENT_REASONS.join(", ")),
            None,
        ));
    }
    let pool = pool(write)?;
    let (r2, n2) = (reason.clone(), note.clone());
    session::run(&pool, move |tx| {
        Box::pin(async move {
            let existing: Option<String> = sqlx::query_scalar(
                "SELECT reason FROM op_event_day WHERE user_id = $1 AND date = $2",
            )
            .bind(user_id)
            .bind(day)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            if let Some(existing) = existing {
                return Err(bad_request(
                    &format!("{} is already an event day ({existing})", iso_date(day)),
                    None,
                ));
            }
            sqlx::query(
                "INSERT INTO op_event_day (user_id, date, reason, source, note) \
                 VALUES ($1, $2, $3, $4, $5)",
            )
            .bind(user_id)
            .bind(day)
            .bind(&r2)
            .bind(USER_SOURCE)
            .bind(&n2)
            .execute(&mut **tx)
            .await
            .map_err(db)?;
            Ok(())
        })
    })
    .await?;
    let mut response = wire::json(&event_out(day, &reason, USER_SOURCE, None, note.as_deref()));
    *response.status_mut() = StatusCode::CREATED;
    Ok(response)
}

/// `DELETE /options/event-day?date=`: a person's own day removed; a seeded one is refused.
///
/// # Errors
/// 400 (the date; a seeded day), 401, 404 (no such day; tenant), 503, 500.
pub async fn delete_event_day(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?.clone();
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let raw_date = v.required(&q, "date");
    let day = v.date("date", raw_date);
    v.finish()?;
    let user_id = scoped_sole_user_id(&state.db, principal.user_id, None).await?;
    let day = day.ok_or_else(internal_error)?;
    let pool = pool(write)?;
    session::run(&pool, move |tx| {
        Box::pin(async move {
            let row = sqlx::query(
                "SELECT reason, source FROM op_event_day WHERE user_id = $1 AND date = $2",
            )
            .bind(user_id)
            .bind(day)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            let Some(row) = row else {
                return Err(not_found("event day", &iso_date(day)));
            };
            let reason: String = row.try_get("reason").map_err(db)?;
            let source: String = row.try_get("source").map_err(db)?;
            if source != USER_SOURCE {
                return Err(bad_request(
                    &format!(
                        "{} is a seeded, source-verified day ({reason}); it is removed by a \
                         DECISIONS-OP entry, not from the web",
                        iso_date(day)
                    ),
                    None,
                ));
            }
            sqlx::query("DELETE FROM op_event_day WHERE user_id = $1 AND date = $2")
                .bind(user_id)
                .bind(day)
                .execute(&mut **tx)
                .await
                .map_err(db)?;
            Ok(())
        })
    })
    .await?;
    Ok(StatusCode::NO_CONTENT.into_response())
}

// ---------------------------------------------------------------------------------------------
// Shared: the ceilings and the audited write of one config row
// ---------------------------------------------------------------------------------------------

/// `refuse_other_underlying(raw)`: the 422 for any underlying but NIFTY, before field validation.
fn refuse_other_underlying(raw: &OrderedJson) -> Result<(), Problem> {
    if let OrderedJson::Object(entries) = raw
        && let Some((_, value)) = entries.iter().find(|(k, _)| k == "underlying")
    {
        let text = py_str_json(value).to_uppercase();
        if !ALLOWED_UNDERLYINGS.contains(&text.trim()) {
            return Err(underlying_not_allowed(
                &py_repr_json(value),
                &py_str_json(value),
                &ALLOWED_UNDERLYINGS,
            ));
        }
    }
    Ok(())
}

/// `str(time)` of a settings time.
fn time_str(t: NaiveTime) -> String {
    PV::Time(t, false).py_str()
}

/// `Decimal(str(capital)) * Decimal(str(pct)) / Decimal(100)` against the per-trade ₹ ceiling.
fn check_risk_inr(
    capital: Decimal,
    pct: Decimal,
    ceiling: Decimal,
    env: &str,
) -> Result<(), Problem> {
    let risk = capital
        .checked_mul(pct)
        .and_then(|x| x.checked_div(Decimal::from(100)))
        .ok_or_else(internal_error)?;
    if risk > ceiling {
        let mut q = risk.round_dp_with_strategy(2, RoundingStrategy::MidpointNearestEven);
        q.rescale(2);
        return Err(setting_above_ceiling(
            "risk_per_trade_inr",
            &wire::py_decimal(q),
            &wire::py_decimal(ceiling),
            env,
        ));
    }
    Ok(())
}

/// A change's value as a `Decimal` for arithmetic (`Decimal(str(value))`).
fn as_decimal(v: &PV) -> Result<Decimal, Problem> {
    v.as_pydec()
        .and_then(|d| d.to_decimal())
        .ok_or_else(internal_error)
}

/// One `_check_ceiling` against a decimal or integer ceiling.
fn over_ceiling(field: &str, value: &PV, ceiling: PyDec, env: &str) -> Result<(), Problem> {
    if let Some(v) = value.as_pydec()
        && v.cmp_num(&ceiling) == Ordering::Greater
    {
        return Err(setting_above_ceiling(
            field,
            &value.py_str(),
            &ceiling.py_str(),
            env,
        ));
    }
    Ok(())
}

/// `_audit(...)` + flush for one config row, and its audit rows.
#[allow(clippy::too_many_arguments)]
async fn audited_write(
    tx: &mut Tx,
    table: &'static str,
    audit_table: &'static str,
    key: &str,
    key_binds: &[i64],
    key_text: Option<&str>,
    stored: &[(String, Stored)],
    updated_by: Option<&str>,
    changes: &[(String, PV)],
    changed_by: &str,
    scope: &str,
    note: Option<&str>,
    user_id: i64,
    now: chrono::DateTime<chrono::Utc>,
) -> Result<Vec<(String, PV)>, Problem> {
    // The row key may carry a text column (the sleeve group): bind it through a CTE-free WHERE.
    let key_sql = match key_text {
        Some(group) => key.replace("{group}", &format!("'{}'", group.replace('\'', "''"))),
        None => key.to_owned(),
    };
    let audits = write_changes(
        tx, table, &key_sql, key_binds, stored, updated_by, changes, changed_by, true,
    )
    .await?;
    let mut applied = Vec::new();
    for a in &audits {
        sqlx::query(&format!(
            "INSERT INTO {audit_table} (user_id, scope, key, old_value, new_value, changed_at, \
             changed_by, note) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)"
        ))
        .bind(user_id)
        .bind(scope)
        .bind(&a.key)
        .bind(&a.old_value)
        .bind(&a.new_value)
        .bind(now)
        .bind(changed_by)
        .bind(note)
        .execute(&mut **tx)
        .await
        .map_err(db)?;
        if let Some((_, v)) = changes.iter().find(|(k, _)| *k == a.key) {
            applied.push((a.key.clone(), v.clone()));
        }
    }
    Ok(applied)
}

/// The `(group, patch)` pairs of a parsed `sleeves` dict, in arrival order.
fn sleeve_patches(patch: &PV) -> Vec<(String, PV)> {
    match patch.field("sleeves") {
        Some(PV::Dict(items)) => items
            .iter()
            .filter_map(|(k, v)| match k {
                PV::Str(g) => Some((g.clone(), v.clone())),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    }
}

/// The `book` patch, when one was sent.
fn book_patch(patch: &PV) -> Option<PV> {
    match patch.field("book") {
        Some(PV::Null) | None => None,
        Some(b) => Some(b.clone()),
    }
}

// ---------------------------------------------------------------------------------------------
// PATCH /options/config
// ---------------------------------------------------------------------------------------------

fn options_patch() -> Model {
    let book = Model {
        fields: vec![
            ("underlying", Ty::str_len(None, None).opt(), D::Unset),
            ("account_inr", Ty::Dec(vec![Bound::Ge("0")]).opt(), D::Unset),
            (
                "margin_pool_inr",
                Ty::Dec(vec![Bound::Ge("0")]).opt(),
                D::Unset,
            ),
            (
                "daily_loss_limit_inr",
                Ty::Dec(vec![Bound::Ge("0")]).opt(),
                D::Unset,
            ),
            (
                "monthly_pause_inr",
                Ty::Dec(vec![Bound::Ge("0")]).opt(),
                D::Unset,
            ),
        ],
        forbid_extra: true,
        before: Some(refuse_other_underlying),
        after: None,
    };
    let sleeve = Model {
        fields: vec![
            (
                "sleeve_capital_inr",
                Ty::Dec(vec![Bound::Ge("0")]).opt(),
                D::Unset,
            ),
            (
                "risk_per_trade_pct",
                Ty::Dec(vec![Bound::Gt("0"), Bound::Le("100")]).opt(),
                D::Unset,
            ),
            ("max_lots", Ty::Int(vec![Bound::Gt("0")]).opt(), D::Unset),
            ("paper_enabled", Ty::Bool.opt(), D::Unset),
            ("hard_exit_time", Ty::Time.opt(), D::Unset),
        ],
        forbid_extra: true,
        before: Some(refuse_other_underlying),
        after: None,
    };
    Model {
        fields: vec![
            ("book", Ty::Model(Box::new(book)).opt(), D::Unset),
            (
                "sleeves",
                Ty::Dict(
                    Box::new(Ty::Enum(&OPTION_GROUPS)),
                    Box::new(Ty::Model(Box::new(sleeve))),
                ),
                D::Unset,
            ),
        ],
        forbid_extra: true,
        before: None,
        after: None,
    }
}

/// `OptionsBookPatch.changes()`: the underlying normalised (`str(...).upper().strip()`).
fn options_book_changes(book: &PV) -> Vec<(String, PV)> {
    book.changes()
        .into_iter()
        .map(|(k, v)| match (k.as_str(), &v) {
            ("underlying", PV::Str(s)) => (k, PV::Str(s.to_uppercase().trim().to_owned())),
            _ => (k, v),
        })
        .collect()
}

/// `check_sleeve(changes, ceilings, current=row)` for an options group.
fn options_check_sleeve(
    state: &AppState,
    changes: &[(String, PV)],
    current_capital: Decimal,
    current_pct: Decimal,
) -> Result<(), Problem> {
    let s = &state.settings;
    for (field, value) in changes {
        match field.as_str() {
            "risk_per_trade_pct" => over_ceiling(
                field,
                value,
                dec_const(s.options_risk_pct_max),
                "BASKFY_OPTIONS_RISK_PCT_MAX",
            )?,
            "max_lots" => over_ceiling(
                field,
                value,
                dec_const(Decimal::from(s.options_max_lots_max)),
                "BASKFY_OPTIONS_MAX_LOTS_MAX",
            )?,
            "hard_exit_time" => {
                let PV::Time(t, aware) = value else { continue };
                if *aware {
                    // `time > time` across an offset-aware and a naive time raises TypeError.
                    crate::logging::error(
                        "baskfy_api.app",
                        "unhandled error",
                        &[("error", "can't compare offset-naive and offset-aware times")],
                    );
                    return Err(internal_error());
                }
                if *t > s.options_hard_exit_latest {
                    return Err(setting_above_ceiling(
                        field,
                        &value.py_str(),
                        &time_str(s.options_hard_exit_latest),
                        "BASKFY_OPTIONS_HARD_EXIT_LATEST",
                    ));
                }
            }
            _ => {}
        }
    }
    let get = |name: &str, current: Decimal| -> Result<Decimal, Problem> {
        changes
            .iter()
            .find(|(k, _)| k == name)
            .map_or(Ok(current), |(_, v)| as_decimal(v))
    };
    check_risk_inr(
        get("sleeve_capital_inr", current_capital)?,
        get("risk_per_trade_pct", current_pct)?,
        s.options_risk_per_trade_inr_max,
        "BASKFY_OPTIONS_RISK_PER_TRADE_INR_MAX",
    )
}

/// `PATCH /options/config`: the book and any sleeve group, atomic across the request.
///
/// # Errors
/// 400 (body), 401, 404 (tenant; not seeded), 422 (a ceiling, another underlying), 503, 500.
pub async fn patch_options_config(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let (content_type, bytes) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    let principal = require_authenticated(&principal)?.clone();
    let patch =
        validate_body(&received, &options_patch()).map_err(|f| f.into_problem(Vec::new()))?;
    let user_id = scoped_sole_user_id(&state.db, principal.user_id, None).await?;
    let changed_by = format!("user:{user_id}");
    let now = now_micros();
    let book = book_patch(&patch).map(|b| options_book_changes(&b));
    let sleeves: Vec<(String, Vec<(String, PV)>)> = sleeve_patches(&patch)
        .into_iter()
        .map(|(g, p)| (g, p.changes()))
        .collect();
    let pool = pool(write)?;
    let st = state.clone();
    session::run(&pool, move |tx| {
        Box::pin(async move {
            let not_seeded = || not_found("options config", &user_id.to_string());
            let s = &st.settings;
            let book_row = if let Some(changes) = &book {
                let row = sqlx::query(
                    "SELECT underlying, account_inr, margin_pool_inr, daily_loss_limit_inr, \
                     monthly_pause_inr, updated_by FROM op_book_config WHERE user_id = $1",
                )
                .bind(user_id)
                .fetch_optional(&mut **tx)
                .await
                .map_err(db)?
                .ok_or_else(not_seeded)?;
                for (field, value) in changes {
                    match field.as_str() {
                        "daily_loss_limit_inr" => over_ceiling(
                            field,
                            value,
                            dec_const(s.options_book_daily_loss_inr_max),
                            "BASKFY_OPTIONS_BOOK_DAILY_LOSS_INR_MAX",
                        )?,
                        "monthly_pause_inr" => over_ceiling(
                            field,
                            value,
                            dec_const(s.options_book_monthly_loss_inr_max),
                            "BASKFY_OPTIONS_BOOK_MONTHLY_LOSS_INR_MAX",
                        )?,
                        _ => {}
                    }
                }
                Some(row)
            } else {
                None
            };
            let mut sleeve_rows = Vec::new();
            for (group, changes) in &sleeves {
                let row = sqlx::query(
                    "SELECT sleeve_capital_inr, risk_per_trade_pct, max_lots::bigint AS max_lots, \
                     paper_enabled, hard_exit_time, updated_by FROM op_sleeve_config \
                     WHERE user_id = $1 AND sleeve = $2",
                )
                .bind(user_id)
                .bind(group)
                .fetch_optional(&mut **tx)
                .await
                .map_err(db)?
                .ok_or_else(not_seeded)?;
                options_check_sleeve(
                    &st,
                    changes,
                    crate::routes::sleeves::common::dec(&row, "sleeve_capital_inr")?,
                    crate::routes::sleeves::common::dec(&row, "risk_per_trade_pct")?,
                )?;
                sleeve_rows.push(row);
            }
            if let (Some(changes), Some(row)) = (&book, &book_row) {
                let d = |c: &str| -> Result<Stored, Problem> {
                    Ok(Stored::Dec(crate::routes::sleeves::common::dec(row, c)?))
                };
                let stored = vec![
                    (
                        "underlying".to_owned(),
                        Stored::Str(row.try_get("underlying").map_err(db)?),
                    ),
                    ("account_inr".to_owned(), d("account_inr")?),
                    ("margin_pool_inr".to_owned(), d("margin_pool_inr")?),
                    (
                        "daily_loss_limit_inr".to_owned(),
                        d("daily_loss_limit_inr")?,
                    ),
                    ("monthly_pause_inr".to_owned(), d("monthly_pause_inr")?),
                ];
                let updated_by: Option<String> = row.try_get("updated_by").map_err(db)?;
                audited_write(
                    tx,
                    "op_book_config",
                    "op_config_audit",
                    "user_id = ?",
                    &[user_id],
                    None,
                    &stored,
                    updated_by.as_deref(),
                    changes,
                    &changed_by,
                    "BOOK",
                    None,
                    user_id,
                    now,
                )
                .await?;
            }
            for ((group, changes), row) in sleeves.iter().zip(sleeve_rows.iter()) {
                let stored = vec![
                    (
                        "sleeve_capital_inr".to_owned(),
                        Stored::Dec(crate::routes::sleeves::common::dec(
                            row,
                            "sleeve_capital_inr",
                        )?),
                    ),
                    (
                        "risk_per_trade_pct".to_owned(),
                        Stored::Dec(crate::routes::sleeves::common::dec(
                            row,
                            "risk_per_trade_pct",
                        )?),
                    ),
                    (
                        "max_lots".to_owned(),
                        Stored::Int(row.try_get("max_lots").map_err(db)?),
                    ),
                    (
                        "paper_enabled".to_owned(),
                        Stored::Bool(row.try_get("paper_enabled").map_err(db)?),
                    ),
                    (
                        "hard_exit_time".to_owned(),
                        Stored::Time(row.try_get("hard_exit_time").map_err(db)?),
                    ),
                ];
                let updated_by: Option<String> = row.try_get("updated_by").map_err(db)?;
                audited_write(
                    tx,
                    "op_sleeve_config",
                    "op_config_audit",
                    "user_id = ? AND sleeve = {group}",
                    &[user_id],
                    Some(group),
                    &stored,
                    updated_by.as_deref(),
                    changes,
                    &changed_by,
                    group,
                    None,
                    user_id,
                    now,
                )
                .await?;
            }
            Ok(())
        })
    })
    .await?;
    crate::routes::options::config_response(&state, user_id).await
}

// ---------------------------------------------------------------------------------------------
// PATCH /fno/config
// ---------------------------------------------------------------------------------------------

/// `MAX_LOTS_CEILING` (`DEFAULT_FNO_CONFIG.common.max_lots_ceiling`).
const FNO_MAX_LOTS_CEILING: &str = "10";

fn fno_patch() -> Model {
    let book = Model {
        fields: vec![(
            "monthly_pause_inr",
            Ty::Dec(vec![Bound::Ge("0")]).opt(),
            D::Unset,
        )],
        forbid_extra: true,
        before: None,
        after: None,
    };
    let sleeve = Model {
        fields: vec![
            ("capital_inr", Ty::Dec(vec![Bound::Ge("0")]).opt(), D::Unset),
            (
                "risk_per_trade_pct",
                Ty::Dec(vec![Bound::Gt("0"), Bound::Le("100")]).opt(),
                D::Unset,
            ),
            (
                "max_lots",
                Ty::Int(vec![Bound::Gt("0"), Bound::Le(FNO_MAX_LOTS_CEILING)]).opt(),
                D::Unset,
            ),
            (
                "max_open_positions",
                Ty::Int(vec![Bound::Gt("0")]).opt(),
                D::Unset,
            ),
            ("paper_enabled", Ty::Bool.opt(), D::Unset),
        ],
        forbid_extra: true,
        before: None,
        after: None,
    };
    Model {
        fields: vec![
            ("book", Ty::Model(Box::new(book)).opt(), D::Unset),
            (
                "sleeves",
                Ty::Dict(
                    Box::new(Ty::Enum(&FNO_GROUPS)),
                    Box::new(Ty::Model(Box::new(sleeve))),
                ),
                D::Unset,
            ),
        ],
        forbid_extra: true,
        before: None,
        after: None,
    }
}

/// `_GROUP_MAX_OPEN`.
fn group_max_open(group: &str) -> i64 {
    match group {
        "F2" => 10,
        _ => 2,
    }
}

/// `check_sleeve(group, changes, ceilings, current=row)` for an F&O group.
fn fno_check_sleeve(
    state: &AppState,
    group: &str,
    changes: &[(String, PV)],
    current_capital: Decimal,
    current_pct: Decimal,
) -> Result<(), Problem> {
    let s = &state.settings;
    for (field, value) in changes {
        match field.as_str() {
            "risk_per_trade_pct" => over_ceiling(
                field,
                value,
                dec_const(s.fno_risk_pct_max),
                "BASKFY_FNO_RISK_PCT_MAX",
            )?,
            "max_open_positions" => over_ceiling(
                field,
                value,
                dec_const(Decimal::from(s.fno_max_open_positions_max)),
                "BASKFY_FNO_MAX_OPEN_POSITIONS_MAX",
            )?,
            _ => {}
        }
    }
    let bound = group_max_open(group);
    if let Some((_, value)) = changes.iter().find(|(k, _)| k == "max_open_positions")
        && let PV::Int(i) = value
        && i.saturated() > i128::from(bound)
    {
        return Err(bad_request(
            &format!(
                "max_open_positions = {} is above its bound {bound} (docs/fno/04)",
                i.canonical
            ),
            None,
        ));
    }
    let get = |name: &str, current: Decimal| -> Result<Decimal, Problem> {
        changes
            .iter()
            .find(|(k, _)| k == name)
            .map_or(Ok(current), |(_, v)| as_decimal(v))
    };
    check_risk_inr(
        get("capital_inr", current_capital)?,
        get("risk_per_trade_pct", current_pct)?,
        s.fno_risk_per_trade_inr_max,
        "BASKFY_FNO_RISK_PER_TRADE_INR_MAX",
    )
}

/// `PATCH /fno/config`: the book's loss pause and any sleeve group's settings, atomic.
///
/// # Errors
/// 400 (body; a group's own bound), 401, 404 (tenant; not seeded), 422 (a ceiling), 503, 500.
pub async fn patch_fno_config(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let (content_type, bytes) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    let principal = require_authenticated(&principal)?.clone();
    let patch = validate_body(&received, &fno_patch()).map_err(|f| f.into_problem(Vec::new()))?;
    let user_id = scoped_sole_user_id(&state.db, principal.user_id, None).await?;
    let changed_by = format!("user:{user_id}");
    let now = now_micros();
    let book = book_patch(&patch).map(|b| b.changes());
    let sleeves: Vec<(String, Vec<(String, PV)>)> = sleeve_patches(&patch)
        .into_iter()
        .map(|(g, p)| (g, p.changes()))
        .collect();
    let pool = pool(write)?;
    let st = state.clone();
    let overlay = session::run(&pool, move |tx| {
        Box::pin(async move {
            let not_seeded = || not_found("fno config", &user_id.to_string());
            let s = &st.settings;
            let book_row = if let Some(changes) = &book {
                let row = sqlx::query(
                    "SELECT monthly_pause_inr, updated_by FROM fo_book_config WHERE user_id = $1",
                )
                .bind(user_id)
                .fetch_optional(&mut **tx)
                .await
                .map_err(db)?
                .ok_or_else(not_seeded)?;
                for (field, value) in changes {
                    if field == "monthly_pause_inr" {
                        over_ceiling(
                            field,
                            value,
                            dec_const(s.fno_book_monthly_loss_inr_max),
                            "BASKFY_FNO_BOOK_MONTHLY_LOSS_INR_MAX",
                        )?;
                    }
                }
                Some(row)
            } else {
                None
            };
            let mut sleeve_rows = Vec::new();
            for (group, changes) in &sleeves {
                let row = sqlx::query(
                    "SELECT capital_inr, risk_per_trade_pct, max_lots::bigint AS max_lots, \
                     max_open_positions::bigint AS max_open_positions, paper_enabled, updated_by \
                     FROM fo_sleeve_config WHERE user_id = $1 AND sleeve = $2",
                )
                .bind(user_id)
                .bind(group)
                .fetch_optional(&mut **tx)
                .await
                .map_err(db)?
                .ok_or_else(not_seeded)?;
                fno_check_sleeve(
                    &st,
                    group,
                    changes,
                    crate::routes::sleeves::common::dec(&row, "capital_inr")?,
                    crate::routes::sleeves::common::dec(&row, "risk_per_trade_pct")?,
                )?;
                sleeve_rows.push(row);
            }
            let mut overlay: Vec<Overlay> = Vec::new();
            if let (Some(changes), Some(row)) = (&book, &book_row) {
                let stored = vec![(
                    "monthly_pause_inr".to_owned(),
                    Stored::Dec(crate::routes::sleeves::common::dec(
                        row,
                        "monthly_pause_inr",
                    )?),
                )];
                let updated_by: Option<String> = row.try_get("updated_by").map_err(db)?;
                let applied = audited_write(
                    tx,
                    "fo_book_config",
                    "fo_config_audit",
                    "user_id = ?",
                    &[user_id],
                    None,
                    &stored,
                    updated_by.as_deref(),
                    changes,
                    &changed_by,
                    "BOOK",
                    Some("FO5 settings PATCH"),
                    user_id,
                    now,
                )
                .await?;
                overlay.push((None, applied));
            }
            for ((group, changes), row) in sleeves.iter().zip(sleeve_rows.iter()) {
                let stored = vec![
                    (
                        "capital_inr".to_owned(),
                        Stored::Dec(crate::routes::sleeves::common::dec(row, "capital_inr")?),
                    ),
                    (
                        "risk_per_trade_pct".to_owned(),
                        Stored::Dec(crate::routes::sleeves::common::dec(
                            row,
                            "risk_per_trade_pct",
                        )?),
                    ),
                    (
                        "max_lots".to_owned(),
                        Stored::Int(row.try_get("max_lots").map_err(db)?),
                    ),
                    (
                        "max_open_positions".to_owned(),
                        Stored::Int(row.try_get("max_open_positions").map_err(db)?),
                    ),
                    (
                        "paper_enabled".to_owned(),
                        Stored::Bool(row.try_get("paper_enabled").map_err(db)?),
                    ),
                ];
                let updated_by: Option<String> = row.try_get("updated_by").map_err(db)?;
                let applied = audited_write(
                    tx,
                    "fo_sleeve_config",
                    "fo_config_audit",
                    "user_id = ? AND sleeve = {group}",
                    &[user_id],
                    Some(group),
                    &stored,
                    updated_by.as_deref(),
                    changes,
                    &changed_by,
                    group,
                    Some("FO5 settings PATCH"),
                    user_id,
                    now,
                )
                .await?;
                overlay.push((Some(group.clone()), applied));
            }
            Ok(overlay)
        })
    })
    .await?;
    let body = crate::routes::fno::config_body(&state, user_id).await?;
    let mut json = OrderedJson::parse(&String::from_utf8_lossy(&wire::to_json_bytes(&body)))
        .map_err(|_| internal_error())?;
    // Only the last sleeve group's row survives in the ORM's (weak) identity map: the router's
    // loop variable `current` still holds it when `_config_out` runs `session.get`. The book and
    // every earlier group were released by CPython's refcounting and are re-read from the table.
    let survivor: Vec<_> = overlay
        .into_iter()
        .rfind(|(group, _)| group.is_some())
        .into_iter()
        .collect();
    apply_overlay(&mut json, &survivor);
    Ok(wire::json(&json))
}

/// The ORM's identity map after `fno_settings`' unrefreshed flush: a field the patch set on a
/// row still held in memory is served as the value sent (its own scale), not as the column
/// stored it (DECISIONS-RS, lane writes-sleeves).
/// One row's applied changes: `None` is the book, `Some(group)` a sleeve group.
type Overlay = (Option<String>, Vec<(String, PV)>);

fn apply_overlay(json: &mut OrderedJson, overlay: &[Overlay]) {
    let to_json = |v: &PV| match v {
        PV::Dec(d) => OrderedJson::Str(d.py_str()),
        PV::Int(i) => i.as_i64().map_or_else(
            || OrderedJson::BigInt(i.canonical.clone()),
            OrderedJson::Int,
        ),
        PV::Bool(b) => OrderedJson::Bool(*b),
        PV::Str(s) => OrderedJson::Str(s.clone()),
        _ => OrderedJson::Null,
    };
    let OrderedJson::Object(top) = json else {
        return;
    };
    for (group, fields) in overlay {
        let target = match group {
            None => top.iter_mut().find(|(k, _)| k == "book").map(|(_, v)| v),
            Some(g) => top
                .iter_mut()
                .find(|(k, _)| k == "sleeves")
                .and_then(|(_, v)| match v {
                    OrderedJson::Array(items) => items
                        .iter_mut()
                        .find(|i| i.get("sleeve") == Some(&OrderedJson::Str(g.clone()))),
                    _ => None,
                }),
        };
        if let Some(OrderedJson::Object(entries)) = target {
            for (field, value) in fields {
                if let Some((_, slot)) = entries.iter_mut().find(|(k, _)| k == field) {
                    *slot = to_json(value);
                }
            }
        }
    }
}
