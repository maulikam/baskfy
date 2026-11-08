//! The swing book's money-free writes (lane writes-sleeves): `POST /swing/scan` (SW15),
//! `POST /swing/watch`, `PATCH /swing/watch/{id}`, `DELETE /swing/watch/{id}` (SW5, SW10.5) and
//! `PATCH /swing/config` (SW4) — ports of `routers/swing.py`'s write handlers and the services
//! behind them (`swing_scan.request_scan`, `swing_watch.add_manual/annotate/reconfirm/dismiss`,
//! `swing_settings.apply_patch`).
//!
//! None of them can place, size, modify or cancel an order: a scan inserts one `sw_scan_run` row
//! and publishes a worker task; the watchlist stores levels, notes and states; the settings
//! patch writes seven numbers under server ceilings, and the two system-owned fields (the
//! exposure rung, the first-live countdown) are not fields of its model at all (law 2).
//!
//! **The ORM's net-change rule.** SQLAlchemy emits an `UPDATE` only for attributes whose value
//! changed (`Numeric` compared as numbers), and `updated_at` moves (`onupdate=func.now()`) only
//! when an `UPDATE` is emitted; every statement here is written to the same rule.

use std::cmp::Ordering;

use axum::extract::{Extension, Path, Request, State};
use axum::response::Response;
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::Row;

use super::{WATCHING, swing_tenant};
use crate::auth::{Principal, require_authenticated};
use crate::body::receive;
use crate::problems::{Problem, bad_request, not_found, setting_above_ceiling};
use crate::query::Validation;
use crate::routes::sleeves::common::{LaxInt, db, field_error, id_or_500, int_param};
use crate::routes::sleeves::pyd::{
    Bound, Default as D, Model, PV, PyDec, Ty, dec_const, validate_body,
};
use crate::routes::sleeves::writes::{
    Bind, Changes, ScanSpec, Stored, bindable, has_queue, now_micros, pool, publish_after_commit,
    queued_response, read_body, request_scan, write_changes,
};
use crate::session::{self, Tx, WritePool};
use crate::state::AppState;

/// `swing_scan.SCAN_TASK_NAME`.
pub const SCAN_TASK_NAME: &str = "baskfy.swing.scan_now";
/// `DEFAULT_SWING_CONFIG.watch.manual_valid_bars`.
const MANUAL_VALID_BARS: i64 = 10;
/// `swing_watch.SOURCE_MANUAL`.
const SOURCE_MANUAL: &str = "MANUAL";
/// `swing_watch.DISMISSED`.
const DISMISSED: &str = "DISMISSED";
/// `baskfy_core.seed_data.NSE_EXCHANGE_ID`.
const NSE_EXCHANGE_ID: i16 = 1;

// ---------------------------------------------------------------------------------------------
// POST /swing/scan
// ---------------------------------------------------------------------------------------------

/// `POST /swing/scan` (SW15): one `sw_scan_run` row and one task name published.
///
/// # Errors
/// 401, 404/503 (the tenant), 409 (a scan in flight), 429 (inside the minute), 500.
pub async fn post_scan(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let user_id = swing_tenant(&state, &principal).await?;
    let pool = pool(write)?;
    let spec = ScanSpec {
        table: "sw_scan_run",
        insert_sql: "INSERT INTO sw_scan_run (user_id, requested_at, status, detail) \
                     VALUES ($1, $2, 'QUEUED', '{\"source\": \"web\"}'::jsonb) RETURNING id",
        task: SCAN_TASK_NAME,
        min_interval_seconds: state.settings.swing_scan_min_interval_seconds,
        stale_after_seconds: state.settings.swing_scan_stale_after_seconds,
    };
    let (run_id, requested_at) = request_scan(&pool, user_id, spec).await?;
    if has_queue(&state.settings.redis_url) {
        publish_after_commit(
            &state.settings.redis_url,
            &pool,
            spec.table,
            spec.task,
            run_id,
        )
        .await;
    } else {
        crate::logging::warning(
            "baskfy_api.swing_scan",
            &format!("scan now {run_id} queued with no broker; the worker's sweep will publish it"),
            &[],
        );
    }
    Ok(queued_response(run_id, requested_at))
}

// ---------------------------------------------------------------------------------------------
// The watchlist
// ---------------------------------------------------------------------------------------------

fn watch_in() -> Model {
    Model {
        fields: vec![
            ("instrument_id", Ty::Int(vec![]).opt(), D::Unset),
            ("symbol", Ty::str_len(Some(1), Some(32)).opt(), D::Unset),
            ("setup", Ty::StrLit(&["FLAG", "EP"]), D::Required),
            ("trigger", Ty::Dec(vec![Bound::Gt("0")]).opt(), D::Unset),
            ("stop_ref", Ty::Dec(vec![Bound::Gt("0")]).opt(), D::Unset),
            ("note", Ty::str_len(None, Some(2000)).opt(), D::Unset),
            ("catalyst", Ty::str_len(None, Some(2000)).opt(), D::Unset),
        ],
        forbid_extra: true,
        before: None,
        after: Some(|fields| {
            let is_none = |name: &str| {
                fields
                    .iter()
                    .find(|(n, _)| n == name)
                    .and_then(|(_, v)| v.as_ref())
                    .is_none_or(|v| matches!(v, PV::Null))
            };
            if is_none("instrument_id") == is_none("symbol") {
                Err("give exactly one of instrument_id or symbol".to_owned())
            } else {
                Ok(())
            }
        }),
    }
}

fn watch_patch() -> Model {
    Model {
        fields: vec![
            ("note", Ty::str_len(None, Some(2000)).opt(), D::Unset),
            ("catalyst", Ty::str_len(None, Some(2000)).opt(), D::Unset),
            ("reconfirm", Ty::Bool, D::Unset),
        ],
        forbid_extra: true,
        before: None,
        after: None,
    }
}

/// A set, non-null string field.
fn opt_str(model: &PV, name: &str) -> Option<String> {
    match model.field(name) {
        Some(PV::Str(s)) => Some(s.clone()),
        _ => None,
    }
}

/// A set, non-null `Decimal` field.
fn opt_dec(model: &PV, name: &str) -> Option<PyDec> {
    match model.field(name) {
        Some(PV::Dec(d)) => Some(d.clone()),
        _ => None,
    }
}

/// `_sessions_ahead(session, start, count)`: the trading day `count` sessions after `start`.
async fn sessions_ahead(
    tx: &mut Tx,
    start: NaiveDate,
    count: i64,
) -> Result<Option<NaiveDate>, Problem> {
    let dates: Vec<NaiveDate> = sqlx::query_scalar(
        "SELECT date FROM trading_day WHERE exchange_id = $1 AND is_trading_day IS true \
         AND date > $2 ORDER BY date LIMIT $3",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(start)
    .bind(count)
    .fetch_all(&mut **tx)
    .await
    .map_err(db)?;
    Ok(if i64::try_from(dates.len()).ok() == Some(count) {
        dates.last().copied()
    } else {
        None
    })
}

/// One `sw_watch` row as the ORM loaded it.
struct WatchRow {
    id: i64,
    trigger: Option<Decimal>,
    stop_ref: Option<Decimal>,
    note: Option<String>,
    catalyst: Option<String>,
    source: String,
    state: String,
    reconfirmed_on: Option<NaiveDate>,
    expires_on: Option<NaiveDate>,
}

const WATCH_COLUMNS: &str =
    "id, trigger, stop_ref, note, catalyst, source, state, reconfirmed_on, expires_on";

fn watch_row(row: &sqlx::postgres::PgRow) -> Result<WatchRow, Problem> {
    Ok(WatchRow {
        id: row.try_get("id").map_err(db)?,
        trigger: crate::routes::sleeves::common::odec(row, "trigger")?,
        stop_ref: crate::routes::sleeves::common::odec(row, "stop_ref")?,
        note: row.try_get("note").map_err(db)?,
        catalyst: row.try_get("catalyst").map_err(db)?,
        source: row.try_get("source").map_err(db)?,
        state: row.try_get("state").map_err(db)?,
        reconfirmed_on: row.try_get("reconfirmed_on").map_err(db)?,
        expires_on: row.try_get("expires_on").map_err(db)?,
    })
}

/// The ORM's comparison for a `Numeric` attribute.
fn same_num(before: Option<Decimal>, after: &PyDec) -> bool {
    before.is_some_and(|b| dec_const(b).cmp_num(after) == Ordering::Equal)
}

/// `_one_watch(...)` (every state: its `state` defaults to `None`) after the commit, with the
/// in-memory `trigger`/`stop_ref` the ORM's identity map would have served (a value the column
/// rounds is echoed as it was sent).
async fn one_watch(
    state: &AppState,
    user_id: i64,
    watch_id: i64,
    overlay: (Option<PyDec>, Option<PyDec>),
) -> Result<Response, Problem> {
    let rows = super::watch_rows(state, user_id, None).await?;
    for (id, mut obj) in rows {
        if id == watch_id {
            if let super::J::Obj(fields) = &mut obj {
                for (key, value) in fields.iter_mut() {
                    let given = match key.as_str() {
                        "trigger" => overlay.0.as_ref(),
                        "stop_ref" => overlay.1.as_ref(),
                        _ => None,
                    };
                    if let Some(d) = given {
                        *value = super::J::Dec(bindable(d)?);
                    }
                }
                super::recompute_distance(fields)?;
            }
            return Ok(crate::routes::sleeves::common::canonical_response(&obj));
        }
    }
    Err(not_found("watchlist row", &watch_id.to_string()))
}

/// `POST /swing/watch`: a name added by hand (`swing_watch.add_manual`).
///
/// # Errors
/// 400 (body), 401, 404 (instrument; tenant), 503, 500.
pub async fn post_watch(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let (content_type, bytes) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    require_authenticated(&principal)?;
    let payload = validate_body(&received, &watch_in()).map_err(|f| f.into_problem(Vec::new()))?;
    let user_id = swing_tenant(&state, &principal).await?;
    let instrument_id = match payload.field("instrument_id") {
        Some(PV::Int(i)) => Some(i.clone()),
        _ => None,
    };
    let symbol = opt_str(&payload, "symbol");
    let found: Option<i64> = match &instrument_id {
        Some(i) => {
            let Some(id) = i.as_i64() else {
                return Err(crate::routes::sleeves::writes::out_of_range());
            };
            sqlx::query_scalar("SELECT id FROM instrument WHERE id = $1")
                .bind(id)
                .fetch_optional(state.db.pool())
                .await
                .map_err(db)?
        }
        None => {
            let wanted = crate::routes::sleeves::pyd::py_strip(&symbol.clone().unwrap_or_default())
                .to_uppercase();
            sqlx::query_scalar("SELECT id FROM instrument WHERE symbol = $1")
                .bind(wanted)
                .fetch_optional(state.db.pool())
                .await
                .map_err(db)?
        }
    };
    let Some(instrument_id_found) = found else {
        // `str(payload.instrument_id or payload.symbol)`: an id of 0 is falsy.
        let shown = match &instrument_id {
            Some(i) if i.canonical != "0" => i.canonical.clone(),
            _ => symbol.clone().unwrap_or_else(|| "None".to_owned()),
        };
        return Err(not_found("instrument", &shown));
    };
    let setup = opt_str(&payload, "setup").unwrap_or_default();
    let trigger = opt_dec(&payload, "trigger");
    let stop_ref = opt_dec(&payload, "stop_ref");
    let note = opt_str(&payload, "note");
    let catalyst = opt_str(&payload, "catalyst");
    let trigger_b = trigger.as_ref().map(bindable).transpose()?;
    let stop_b = stop_ref.as_ref().map(bindable).transpose()?;
    let on = now_micros().date_naive();
    let pool = pool(write)?;
    let (trigger_o, stop_o) = (trigger.clone(), stop_ref.clone());
    let watch_id = session::run(&pool, move |tx| {
        Box::pin(async move {
            let existing = sqlx::query(&format!(
                "SELECT {WATCH_COLUMNS} FROM sw_watch WHERE user_id = $1 AND instrument_id = $2 \
                 AND setup = $3 AND state = $4"
            ))
            .bind(user_id)
            .bind(instrument_id_found)
            .bind(&setup)
            .bind(WATCHING)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            let expires_on = sessions_ahead(tx, on, MANUAL_VALID_BARS).await?;
            if let Some(row) = existing {
                let row = watch_row(&row)?;
                let mut c = Changes::default();
                if let (Some(t), Some(b)) = (trigger_o.as_ref(), trigger_b)
                    && !same_num(row.trigger, t)
                {
                    c.set("trigger", Bind::Dec(b));
                }
                if let (Some(s), Some(b)) = (stop_o.as_ref(), stop_b)
                    && !same_num(row.stop_ref, s)
                {
                    c.set("stop_ref", Bind::Dec(b));
                }
                if let Some(n) = note
                    && row.note.as_deref() != Some(n.as_str())
                {
                    c.set("note", Bind::Str(n));
                }
                if let Some(k) = catalyst
                    && row.catalyst.as_deref() != Some(k.as_str())
                {
                    c.set("catalyst", Bind::Str(k));
                }
                if row.source != SOURCE_MANUAL {
                    c.set("source", Bind::Str(SOURCE_MANUAL.to_owned()));
                }
                if row.reconfirmed_on != Some(on) {
                    c.set("reconfirmed_on", Bind::Date(Some(on)));
                }
                if row.expires_on != expires_on {
                    c.set("expires_on", Bind::Date(expires_on));
                }
                c.apply(tx, "sw_watch", "id = ?", &[row.id]).await?;
                return Ok(row.id);
            }
            let id: i64 = sqlx::query_scalar(
                "INSERT INTO sw_watch (user_id, instrument_id, setup, source, added_on, \
                 expires_on, trigger, stop_ref, note, catalyst, state) \
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) RETURNING id",
            )
            .bind(user_id)
            .bind(instrument_id_found)
            .bind(&setup)
            .bind(SOURCE_MANUAL)
            .bind(on)
            .bind(expires_on)
            .bind(trigger_b)
            .bind(stop_b)
            .bind(note)
            .bind(catalyst)
            .bind(WATCHING)
            .fetch_one(&mut **tx)
            .await
            .map_err(db)?;
            Ok(id)
        })
    })
    .await?;
    one_watch(&state, user_id, watch_id, (trigger, stop_ref)).await
}

/// The path's `watch_id` (`int`), as FastAPI validates it.
fn watch_id_param(raw: &str) -> Result<Option<LaxInt>, Vec<crate::problems::ErrorEntry>> {
    let mut v = Validation::new();
    let id = int_param(&mut v, "watch_id", Some(raw));
    match v.finish() {
        Ok(()) => Ok(id),
        Err(p) => Err(p
            .extra
            .iter()
            .find(|(k, _)| k == "errors")
            .and_then(|(_, e)| match e {
                OrderedJson::Array(items) => Some(
                    items
                        .iter()
                        .filter_map(|i| match i {
                            OrderedJson::Object(entries) => Some(entries.clone()),
                            _ => None,
                        })
                        .collect(),
                ),
                _ => None,
            })
            .unwrap_or_default()),
    }
}

async fn load_watch(tx: &mut Tx, user_id: i64, watch_id: i64) -> Result<WatchRow, Problem> {
    let row = sqlx::query(&format!(
        "SELECT {WATCH_COLUMNS} FROM sw_watch WHERE id = $1 AND user_id = $2"
    ))
    .bind(watch_id)
    .bind(user_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(db)?;
    match row {
        Some(r) => watch_row(&r),
        None => Err(not_found("watchlist row", &watch_id.to_string())),
    }
}

/// `PATCH /swing/watch/{watch_id}`: the note, the catalyst, and a MANUAL row's re-confirmation.
///
/// # Errors
/// 400 (path, body, a row that cannot be re-confirmed), 401, 404, 503, 500.
pub async fn patch_watch(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(raw_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let (content_type, bytes) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    require_authenticated(&principal)?;
    let (id, path_errors) = match watch_id_param(&raw_id) {
        Ok(id) => (id, Vec::new()),
        Err(errors) => (None, errors),
    };
    let payload = validate_body(&received, &watch_patch());
    let payload = match payload {
        Ok(p) if path_errors.is_empty() => p,
        Ok(_) => return Err(crate::problems::invalid_screen_definition(path_errors)),
        Err(f) => return Err(f.into_problem(path_errors)),
    };
    let user_id = swing_tenant(&state, &principal).await?;
    let watch_id = id_or_500(id.unwrap_or(LaxInt::Invalid))?;
    let note = opt_str(&payload, "note");
    let catalyst = opt_str(&payload, "catalyst");
    let reconfirm = matches!(payload.field("reconfirm"), Some(PV::Bool(true)));
    let on = now_micros().date_naive();
    let pool = pool(write)?;
    session::run(&pool, move |tx| {
        Box::pin(async move {
            let row = load_watch(tx, user_id, watch_id).await?;
            let mut c = Changes::default();
            if let Some(n) = note
                && row.note.as_deref() != Some(n.as_str())
            {
                c.set("note", Bind::Str(n));
            }
            if let Some(k) = catalyst
                && row.catalyst.as_deref() != Some(k.as_str())
            {
                c.set("catalyst", Bind::Str(k));
            }
            if reconfirm {
                let refuse = |why: String| {
                    bad_request(
                        &format!("Only a WATCHING MANUAL row can be re-confirmed: {why}."),
                        Some(vec![field_error("reconfirm", &why)]),
                    )
                };
                if row.source != SOURCE_MANUAL {
                    return Err(refuse(format!(
                        "watch row {watch_id} is a {} row",
                        row.source
                    )));
                }
                if row.state != WATCHING {
                    return Err(refuse(format!(
                        "watch row {watch_id} is {}, not {WATCHING}",
                        row.state
                    )));
                }
                let expires_on = sessions_ahead(tx, on, MANUAL_VALID_BARS).await?;
                if row.reconfirmed_on != Some(on) {
                    c.set("reconfirmed_on", Bind::Date(Some(on)));
                }
                if row.expires_on != expires_on {
                    c.set("expires_on", Bind::Date(expires_on));
                }
            }
            c.apply(tx, "sw_watch", "id = ?", &[row.id]).await?;
            Ok(())
        })
    })
    .await?;
    one_watch(&state, user_id, watch_id, (None, None)).await
}

/// `DELETE /swing/watch/{watch_id}`: the row moves to `DISMISSED` (a state change, never a delete).
///
/// # Errors
/// 400 (path), 401, 404, 503, 500.
pub async fn delete_watch(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(raw_id): Path<String>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let id = match watch_id_param(&raw_id) {
        Ok(id) => id,
        Err(errors) => return Err(crate::problems::invalid_screen_definition(errors)),
    };
    let user_id = swing_tenant(&state, &principal).await?;
    let watch_id = id_or_500(id.unwrap_or(LaxInt::Invalid))?;
    let pool = pool(write)?;
    session::run(&pool, move |tx| {
        Box::pin(async move {
            let row = load_watch(tx, user_id, watch_id).await?;
            let mut c = Changes::default();
            if row.state != DISMISSED {
                c.set("state", Bind::Str(DISMISSED.to_owned()));
            }
            c.apply(tx, "sw_watch", "id = ?", &[row.id]).await?;
            Ok(())
        })
    })
    .await?;
    one_watch(&state, user_id, watch_id, (None, None)).await
}

// ---------------------------------------------------------------------------------------------
// PATCH /swing/config
// ---------------------------------------------------------------------------------------------

/// `SwingConfigPatch`, field for field.
fn config_patch() -> Model {
    let dec_ge0 = || Ty::Dec(vec![Bound::Ge("0")]).opt();
    Model {
        fields: vec![
            ("sleeve_capital_inr", dec_ge0(), D::Unset),
            (
                "risk_per_trade_pct",
                Ty::Dec(vec![Bound::Gt("0")]).opt(),
                D::Unset,
            ),
            (
                "max_position_pct",
                Ty::Dec(vec![Bound::Gt("0"), Bound::Le("100")]).opt(),
                D::Unset,
            ),
            (
                "max_open_positions",
                Ty::Int(vec![Bound::Gt("0")]).opt(),
                D::Unset,
            ),
            ("or_window_minutes", Ty::IntLit(&[1, 5, 60]).opt(), D::Unset),
            (
                "stop_mode",
                Ty::Enum(&["LOW_OF_DAY", "OPENING_RANGE_LOW"]).opt(),
                D::Unset,
            ),
            ("adr_min_pct", dec_ge0(), D::Unset),
            ("turnover_min_inr", dec_ge0(), D::Unset),
            ("price_min", dec_ge0(), D::Unset),
        ],
        forbid_extra: true,
        before: None,
        after: None,
    }
}

/// `SwingCeilings.check` for each change, in order.
fn check_ceilings(state: &AppState, changes: &[(String, PV)]) -> Result<(), Problem> {
    let s = &state.settings;
    for (field, value) in changes {
        let (ceiling, env): (PyDec, &str) = match field.as_str() {
            "risk_per_trade_pct" => (
                dec_const(s.swing_risk_per_trade_pct_max),
                "BASKFY_SWING_RISK_PER_TRADE_PCT_MAX",
            ),
            "max_position_pct" => (
                dec_const(s.swing_max_position_pct_max),
                "BASKFY_SWING_MAX_POSITION_PCT_MAX",
            ),
            "max_open_positions" => (
                dec_const(Decimal::from(s.swing_max_open_positions_max)),
                "BASKFY_SWING_MAX_OPEN_POSITIONS_MAX",
            ),
            _ => continue,
        };
        let Some(v) = value.as_pydec() else { continue };
        if v.cmp_num(&ceiling) == Ordering::Greater {
            return Err(setting_above_ceiling(
                field,
                &value.py_str(),
                &ceiling.py_str(),
                env,
            ));
        }
    }
    Ok(())
}

/// `PATCH /swing/config`: seven numbers, audited, bounded by the server's ceilings.
///
/// # Errors
/// 400 (body), 401, 404 (tenant), 422 (a ceiling), 503 (not seeded), 500.
pub async fn patch_config(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let (content_type, bytes) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    require_authenticated(&principal)?;
    let patch =
        validate_body(&received, &config_patch()).map_err(|f| f.into_problem(Vec::new()))?;
    let user_id = swing_tenant(&state, &principal).await?;
    let changes = patch.changes();
    let changed_by = format!("user:{user_id}");
    let now = now_micros();
    let pool = pool(write)?;
    let ceilings_state = state.clone();
    session::run(&pool, move |tx| {
        Box::pin(async move {
            let row = sqlx::query(
                "SELECT sleeve_capital_inr, risk_per_trade_pct, max_position_pct, \
                        max_open_positions::bigint AS max_open_positions, \
                        or_window_minutes::bigint AS or_window_minutes, stop_mode, adr_min_pct, \
                        turnover_min_inr, price_min, updated_by \
                 FROM sw_config WHERE user_id = $1",
            )
            .bind(user_id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            let Some(row) = row else {
                return Err(Problem::new(
                    crate::problems::ProblemType::PipelineDegraded,
                    "The swing book is not set up on this deployment; run `make seed`.",
                ));
            };
            check_ceilings(&ceilings_state, &changes)?;
            let d = |c: &str| -> Result<Stored, Problem> {
                Ok(Stored::Dec(crate::routes::sleeves::common::dec(&row, c)?))
            };
            let i = |c: &str| -> Result<Stored, Problem> {
                Ok(Stored::Int(row.try_get::<i64, _>(c).map_err(db)?))
            };
            let stored = vec![
                ("sleeve_capital_inr".to_owned(), d("sleeve_capital_inr")?),
                ("risk_per_trade_pct".to_owned(), d("risk_per_trade_pct")?),
                ("max_position_pct".to_owned(), d("max_position_pct")?),
                ("max_open_positions".to_owned(), i("max_open_positions")?),
                ("or_window_minutes".to_owned(), i("or_window_minutes")?),
                (
                    "stop_mode".to_owned(),
                    Stored::Str(row.try_get("stop_mode").map_err(db)?),
                ),
                ("adr_min_pct".to_owned(), d("adr_min_pct")?),
                ("turnover_min_inr".to_owned(), d("turnover_min_inr")?),
                ("price_min".to_owned(), d("price_min")?),
            ];
            let updated_by: Option<String> = row.try_get("updated_by").map_err(db)?;
            let audits = write_changes(
                tx,
                "sw_config",
                "user_id = ?",
                &[user_id],
                &stored,
                updated_by.as_deref(),
                &changes,
                &changed_by,
                true,
            )
            .await?;
            for a in audits {
                sqlx::query(
                    "INSERT INTO sw_config_audit (user_id, key, old_value, new_value, changed_at, \
                     changed_by, note) VALUES ($1, $2, $3, $4, $5, $6, NULL)",
                )
                .bind(user_id)
                .bind(&a.key)
                .bind(&a.old_value)
                .bind(&a.new_value)
                .bind(now)
                .bind(&changed_by)
                .execute(&mut **tx)
                .await
                .map_err(db)?;
            }
            Ok(())
        })
    })
    .await?;
    super::config_response(&state, user_id).await
}
