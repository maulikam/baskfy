//! ``/vbt/*`` — the volume-breakout sleeve's read surfaces (VB8, VB12).
//!
//! Ports the GET routes of `baskfy_api.routers.vbt` and the reads behind them
//! (`baskfy_api.vbt.today`/`bars_for`/`breadth`/`book`/`backtests`, `vbt_scan`,
//! `vbt_settings.read_config`/`to_view`). Python declares `APIRouter(prefix="/vbt")`.
//!
//! Ported: `GET /today`, `GET /today/{instrument_id}/bars`, `GET /breadth`, `GET /book`,
//! `GET /backtest`, `GET /config`, `GET /scan/{run_id}`. `PATCH /config` and `POST /scan` are
//! writes and stay on the Python API until writes are ported (not registered here).
//!
//! Every body is `canonical_json` (decimals unquoted with their stored scale: a limit price is a
//! number somebody types into a broker). The live overlay (`last_price`) is always `null` here —
//! Python's answer without a Kite session (DECISIONS-RS, lane api-sleeves). **No route may place, modify or
//! cancel an order** (law 2).

use std::collections::HashMap;

use axum::Router;
use axum::extract::{Extension, Path, RawQuery, State};
use axum::response::Response;
use axum::routing::get;
use baskfy_core::vbt::config::{BreadthConfig, DRY_RUN_SESSIONS_REQUIRED, EntryConfig};
use baskfy_core::vbt::published::PUBLISHED;
use chrono::{Duration, NaiveDate};
use rust_decimal::Decimal;
use sqlx::postgres::PgRow;

use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, not_found};
use crate::query::{QueryParams, Validation};
use crate::routes::sleeves::common::{
    J, LaxInt, Obj, as_dict, canonical_response, db, dec, decimal_of_float, div, get as col,
    id_or_500, int_param, ist, json_strict_int, mul, now_utc, odec, ojson, quantize, quantize_j,
    scoped_sole_user_id,
};
use crate::routes::twt::backtest_run_j;
use crate::state::AppState;

/// `CHART_BARS`.
pub const CHART_BARS: i64 = 130;
/// `BREADTH_DEFAULT_DAYS`.
pub const BREADTH_DEFAULT_DAYS: i64 = 365;
/// `SHUT_WINDOW_SESSIONS`.
pub const SHUT_WINDOW_SESSIONS: i64 = 60;
/// `LIVE_STATES`, sorted as the query binds them.
const LIVE_STATES: [&str; 4] = ["CONFIRMED", "PARTIAL", "PROPOSED", "SENT"];

/// `_threshold_pct()`: `Decimal(str(DEFAULT_VBT_CONFIG.breadth.min_pct_above_dma))`.
fn threshold_pct() -> Decimal {
    decimal_of_float(BreadthConfig::default().min_pct_above_dma)
}

const BREADTH_COLUMNS: &str = "date, gate, pct_above_dma, \
    measured_count::bigint AS measured_count, above_count::bigint AS above_count, thin_session, \
    detail";

/// The newest `vb_breadth_daily` row, or the one for `day`.
async fn reading(
    state: &AppState,
    user_id: i64,
    day: Option<NaiveDate>,
) -> Result<Option<PgRow>, Problem> {
    match day {
        Some(d) => {
            sqlx::query(&format!(
                "SELECT {BREADTH_COLUMNS} FROM vb_breadth_daily WHERE user_id = $1 AND date = $2"
            ))
            .bind(user_id)
            .bind(d)
            .fetch_optional(state.db.pool())
            .await
        }
        None => {
            sqlx::query(&format!(
                "SELECT {BREADTH_COLUMNS} FROM vb_breadth_daily WHERE user_id = $1 \
             ORDER BY date DESC LIMIT 1"
            ))
            .bind(user_id)
            .fetch_optional(state.db.pool())
            .await
        }
    }
    .map_err(db)
}

/// `today(...).as_of` alone: the latest session the detector wrote (or `day` itself).
async fn latest_as_of(state: &AppState, user_id: i64) -> Result<Option<NaiveDate>, Problem> {
    sqlx::query_scalar(
        "SELECT date FROM vb_breadth_daily WHERE user_id = $1 ORDER BY date DESC LIMIT 1",
    )
    .bind(user_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)
}

/// `(a / b - 1) * 100`, to 0.01; `None` when `b` is missing or zero.
fn pct_over(a: Option<Decimal>, b: Option<Decimal>) -> Result<Option<J>, Problem> {
    let (Some(a), Some(b)) = (a, b) else {
        return Ok(None);
    };
    if b.is_zero() {
        return Ok(None);
    }
    Ok(Some(quantize_j(
        mul(div(a, b)? - Decimal::ONE, Decimal::from(100))?,
        2,
    )))
}

/// `_r_multiple(entry, stop, mark)`.
fn r_multiple(entry: Decimal, stop: Decimal, mark: Option<Decimal>) -> Result<Option<J>, Problem> {
    let risk = entry - stop;
    let Some(mark) = mark else {
        return Ok(None);
    };
    if risk <= Decimal::ZERO {
        return Ok(None);
    }
    Ok(Some(quantize_j(div(mark - entry, risk)?, 2)))
}

async fn candidates(
    state: &AppState,
    user_id: i64,
    day: NaiveDate,
    wanted: &str,
) -> Result<Vec<J>, Problem> {
    let rows = sqlx::query(
        "SELECT s.instrument_id, i.symbol, i.name, s.state, s.failed_filters::text[] AS failed_filters, \
                s.close, s.close_raw, s.limit_price, s.stop_price, s.change_pct, s.rvol, \
                s.close_position, s.ret_20_pct, s.turnover_avg_20, s.sma_200, s.ema_21, \
                s.high_20_prior, s.locked_upper_circuit, s.rank_key \
         FROM vb_signal_daily s JOIN instrument i ON i.id = s.instrument_id \
         WHERE s.user_id = $1 AND s.date = $2 AND s.state = $3 ORDER BY s.rank_key DESC",
    )
    .bind(user_id)
    .bind(day)
    .bind(wanted)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut out = Vec::with_capacity(rows.len());
    for row in &rows {
        let filters: Vec<String> = col(row, "failed_filters")?;
        let sma = odec(row, "sma_200")?;
        out.push(
            Obj::new()
                .put("instrument_id", J::Int(col(row, "instrument_id")?))
                .put("symbol", J::Str(col(row, "symbol")?))
                .put("name", J::Str(col(row, "name")?))
                .put("state", J::Str(col(row, "state")?))
                .put(
                    "failed_filters",
                    J::Arr(filters.into_iter().map(J::Str).collect()),
                )
                .put("close", J::Dec(dec(row, "close_raw")?))
                .put("limit_price", J::Dec(dec(row, "limit_price")?))
                .put("stop_price", J::Dec(dec(row, "stop_price")?))
                .put("change_pct", J::opt_dec(odec(row, "change_pct")?))
                .put("rvol", J::opt_dec(odec(row, "rvol")?))
                .put("close_position", J::opt_dec(odec(row, "close_position")?))
                .put("ret_20_pct", J::opt_dec(odec(row, "ret_20_pct")?))
                .put("turnover_avg_20", J::opt_int(col(row, "turnover_avg_20")?))
                .put("sma_200", J::opt_dec(sma))
                .put("ema_21", J::opt_dec(odec(row, "ema_21")?))
                .put("high_20_prior", J::opt_dec(odec(row, "high_20_prior")?))
                .put(
                    "pct_above_dma",
                    pct_over(Some(dec(row, "close")?), sma)?.unwrap_or(J::Null),
                )
                .put(
                    "locked_upper_circuit",
                    J::Bool(col(row, "locked_upper_circuit")?),
                )
                .put("rank_key", J::Int(col(row, "rank_key")?))
                .put("last_price", J::Null)
                .done(),
        );
    }
    Ok(out)
}

const SCAN_COLUMNS: &str = "id, status, source, requested_at, started_at, finished_at, \
    session_date, detail, error, provisional";

fn scan_run_j(row: &PgRow) -> Result<J, Problem> {
    let detail = as_dict(ojson(row, "detail")?);
    let funnel = detail
        .as_ref()
        .and_then(|d| as_dict(d.get("funnel").cloned()));
    // `source_of(row)`: `detail.source` when the writer recorded one, else the column.
    let named = match detail.as_ref().and_then(|d| d.get("source")) {
        Some(baskfy_db::OrderedJson::Str(s)) if !s.is_empty() => Some(s.clone()),
        _ => None,
    };
    let source = match named {
        Some(s) => s,
        None => col(row, "source")?,
    };
    let found = detail
        .as_ref()
        .and_then(|d| json_strict_int(d.get("signals")));
    Ok(Obj::new()
        .put("run_id", J::Int(col(row, "id")?))
        .put("status", J::Str(col(row, "status")?))
        .put("source", J::Str(source))
        .put("requested_at", J::Dt(col(row, "requested_at")?))
        .put("started_at", J::opt_dt(col(row, "started_at")?))
        .put("finished_at", J::opt_dt(col(row, "finished_at")?))
        .put("session_date", J::opt_date(col(row, "session_date")?))
        .put("funnel", funnel.map_or(J::Null, J::Raw))
        .put("detail", detail.map_or(J::Null, J::Raw))
        .put("error", J::opt_str(col(row, "error")?))
        .put("found", J::opt_int(found))
        .put("provisional", J::Bool(col(row, "provisional")?))
        .done())
}

/// `GET /vbt/today?date=`: the session's candidates and rejects, the gate and the funnel.
///
/// # Errors
/// 401, 400 (a bad date), 404 (not the tenant), 503 (no tenant), 500.
pub async fn get_today(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let day = v.date("date", q.get("date"));
    v.finish()?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let read = reading(&state, user_id, day).await?;
    let filters: Vec<J> = baskfy_core::vbt::config::filter_legend(None)
        .into_iter()
        .map(|f| {
            Obj::new()
                .put("code", J::Str(f.code))
                .put("label", J::Str(f.label))
                .put("rule", J::Str(f.rule))
                .done()
        })
        .collect();
    let mut body = match &read {
        None => Obj::new()
            .put("as_of", J::opt_date(day))
            .put("gate", J::Null)
            .put("pct_above_dma", J::Null)
            .put("above_count", J::Null)
            .put("measured_count", J::Null)
            .put("gate_threshold_pct", J::Dec(threshold_pct()))
            .put("thin_session", J::Bool(false))
            .put("funnel", J::Null)
            .put("shut_sessions_recent", J::Int(0))
            .put("shut_window", J::Int(SHUT_WINDOW_SESSIONS))
            .put("candidates", J::Arr(Vec::new()))
            .put("rejects", J::Arr(Vec::new())),
        Some(r) => {
            let date: NaiveDate = col(r, "date")?;
            let shut: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM (SELECT gate FROM vb_breadth_daily \
                 WHERE user_id = $1 AND date <= $2 ORDER BY date DESC LIMIT $3) recent \
                 WHERE recent.gate = 'SHUT'",
            )
            .bind(user_id)
            .bind(date)
            .bind(SHUT_WINDOW_SESSIONS)
            .fetch_one(state.db.pool())
            .await
            .map_err(db)?;
            Obj::new()
                .put("as_of", J::Date(date))
                .put("gate", J::Str(col(r, "gate")?))
                .put("pct_above_dma", J::Dec(dec(r, "pct_above_dma")?))
                .put("above_count", J::Int(col(r, "above_count")?))
                .put("measured_count", J::Int(col(r, "measured_count")?))
                .put("gate_threshold_pct", J::Dec(threshold_pct()))
                .put("thin_session", J::Bool(col(r, "thin_session")?))
                .put("funnel", ojson(r, "detail")?.map_or(J::Null, J::Raw))
                .put("shut_sessions_recent", J::Int(shut))
                .put("shut_window", J::Int(SHUT_WINDOW_SESSIONS))
                .put(
                    "candidates",
                    J::Arr(candidates(&state, user_id, date, "SIGNAL").await?),
                )
                .put(
                    "rejects",
                    J::Arr(candidates(&state, user_id, date, "SCAN_ONLY").await?),
                )
        }
    };
    let newest = sqlx::query(&format!(
        "SELECT {SCAN_COLUMNS} FROM vb_scan_run WHERE user_id = $1 \
         ORDER BY requested_at DESC, id DESC LIMIT 1"
    ))
    .bind(user_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    body = body.put("filters", J::Arr(filters)).put(
        "last_scan",
        match &newest {
            Some(row) => scan_run_j(row)?,
            None => J::Null,
        },
    );
    Ok(canonical_response(&body.done()))
}

/// `GET /vbt/today/{instrument_id}/bars?date=`: adjusted closes for the mini chart.
///
/// # Errors
/// 401, 400, 404 (no published session yet, or not the tenant), 503, 500.
pub async fn get_bars(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(instrument_id): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let id = int_param(&mut v, "instrument_id", Some(&instrument_id));
    let date = v.date("date", q.get("date"));
    v.finish()?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let id = id.unwrap_or(LaxInt::Invalid);
    let as_of = match date {
        Some(d) => Some(d),
        None => latest_as_of(&state, user_id).await?,
    };
    let Some(as_of) = as_of else {
        // `str(instrument_id)` of the int pydantic parsed.
        let shown = match id {
            LaxInt::Small(i) => i.to_string(),
            _ => instrument_id.trim().replace('_', ""),
        };
        return Err(not_found("published volume-breakout session", &shown));
    };
    let id = id_or_500(id)?;
    let rows = sqlx::query(
        "SELECT date, close FROM ohlcv_daily WHERE instrument_id = $1 AND date <= $2 \
         ORDER BY date DESC LIMIT $3",
    )
    .bind(id)
    .bind(as_of)
    .bind(CHART_BARS)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut data = Vec::with_capacity(rows.len());
    for row in rows.iter().rev() {
        data.push(
            Obj::new()
                .put("date", J::Date(col(row, "date")?))
                .put("close", J::Dec(dec(row, "close")?))
                .done(),
        );
    }
    Ok(canonical_response(
        &Obj::new()
            .put("instrument_id", J::Int(id))
            .put("adjusted", J::Bool(true))
            .put("data", J::Arr(data))
            .done(),
    ))
}

/// `GET /vbt/breadth?from=&to=`: the breadth series with the line that decides the gate; the
/// last year when no window is given.
///
/// # Errors
/// 401, 400 (bad dates), 404, 503, 500.
pub async fn get_breadth(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let from = v.date("from", q.get("from"));
    let to = v.date("to", q.get("to"));
    v.finish()?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let mut start = from;
    if start.is_none()
        && to.is_none()
        && let Some(latest) = latest_as_of(&state, user_id).await?
    {
        start = Some(latest - Duration::days(BREADTH_DEFAULT_DAYS));
    }
    let mut sql = String::from(
        "SELECT date, pct_above_dma, above_count::bigint AS above_count, \
                measured_count::bigint AS measured_count, gate, thin_session \
         FROM vb_breadth_daily WHERE user_id = $1",
    );
    let mut n = 1;
    if start.is_some() {
        n += 1;
        sql.push_str(&format!(" AND date >= ${n}"));
    }
    if to.is_some() {
        n += 1;
        sql.push_str(&format!(" AND date <= ${n}"));
    }
    sql.push_str(" ORDER BY date");
    let mut query = sqlx::query(&sql).bind(user_id);
    if let Some(s) = start {
        query = query.bind(s);
    }
    if let Some(t) = to {
        query = query.bind(t);
    }
    let rows = query.fetch_all(state.db.pool()).await.map_err(db)?;
    let mut data = Vec::with_capacity(rows.len());
    for row in &rows {
        data.push(
            Obj::new()
                .put("date", J::Date(col(row, "date")?))
                .put("pct_above_dma", J::Dec(dec(row, "pct_above_dma")?))
                .put("above_count", J::Int(col(row, "above_count")?))
                .put("measured_count", J::Int(col(row, "measured_count")?))
                .put("gate", J::Str(col(row, "gate")?))
                .put("thin_session", J::Bool(col(row, "thin_session")?))
                .done(),
        );
    }
    Ok(canonical_response(
        &Obj::new()
            .put("threshold_pct", J::Dec(threshold_pct()))
            .put("data", J::Arr(data))
            .done(),
    ))
}

/// `GET /vbt/book`: working orders, the open book, what is closed, and the fill rate.
///
/// # Errors
/// 401, 404, 503, 500.
pub async fn get_book(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let day = match latest_as_of(&state, user_id).await? {
        Some(d) => d,
        None => now_utc().with_timezone(&ist()).date_naive(),
    };
    let orders = sqlx::query(
        "SELECT id, instrument_id, limit_price, stop_price, quantity::bigint AS quantity, state, \
                signal_date, working_from, expires_after_session, \
                sessions_worked::bigint AS sessions_worked, broker_order_id, \
                filled_quantity::bigint AS filled_quantity, simulated \
         FROM vb_order WHERE user_id = $1 AND state = ANY($2) ORDER BY signal_date DESC, id",
    )
    .bind(user_id)
    .bind(LIVE_STATES.as_slice())
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let positions = sqlx::query(
        "SELECT id, instrument_id, entry_date, entry_avg, quantity_entered::bigint AS quantity_entered, \
                quantity_open::bigint AS quantity_open, initial_stop, stop_price, gtt_id, state, \
                exit_queued_for, exit_reason_queued, closed_on, exit_avg, close_reason, simulated \
         FROM vb_position WHERE user_id = $1 ORDER BY entry_date DESC, id",
    )
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut ids: Vec<i64> = Vec::new();
    for row in orders.iter().chain(positions.iter()) {
        let id: i64 = col(row, "instrument_id")?;
        if !ids.contains(&id) {
            ids.push(id);
        }
    }
    let mut names: HashMap<i64, (String, String)> = HashMap::new();
    let mut closes: HashMap<i64, Decimal> = HashMap::new();
    if !ids.is_empty() {
        let rows = sqlx::query("SELECT id, symbol, name FROM instrument WHERE id = ANY($1)")
            .bind(&ids)
            .fetch_all(state.db.pool())
            .await
            .map_err(db)?;
        for row in &rows {
            names.insert(col(row, "id")?, (col(row, "symbol")?, col(row, "name")?));
        }
        let rows = sqlx::query(
            "SELECT o.instrument_id, o.close_raw FROM ohlcv_daily o JOIN ( \
                 SELECT instrument_id, max(date) AS date FROM ohlcv_daily \
                 WHERE instrument_id = ANY($1) AND date <= $2 GROUP BY instrument_id) n \
             ON o.instrument_id = n.instrument_id AND o.date = n.date",
        )
        .bind(&ids)
        .bind(day)
        .fetch_all(state.db.pool())
        .await
        .map_err(db)?;
        for row in &rows {
            closes.insert(col(row, "instrument_id")?, dec(row, "close_raw")?);
        }
    }
    let unknown = ("?".to_owned(), "?".to_owned());
    let allowed = EntryConfig::default().valid_sessions;
    let mut working = Vec::with_capacity(orders.len());
    for row in &orders {
        let instrument_id: i64 = col(row, "instrument_id")?;
        let (symbol, name) = names.get(&instrument_id).unwrap_or(&unknown);
        let limit = dec(row, "limit_price")?;
        let quantity: i64 = col(row, "quantity")?;
        let expires: Option<NaiveDate> = col(row, "expires_after_session")?;
        working.push(
            Obj::new()
                .put("id", J::Int(col(row, "id")?))
                .put("instrument_id", J::Int(instrument_id))
                .put("symbol", J::s(symbol))
                .put("name", J::s(name))
                .put("limit_price", J::Dec(limit))
                .put("stop_price", J::Dec(dec(row, "stop_price")?))
                .put("quantity", J::Int(quantity))
                .put(
                    "value_inr",
                    J::Dec(quantize(mul(limit, Decimal::from(quantity))?, 2)),
                )
                .put("state", J::Str(col(row, "state")?))
                .put("signal_date", J::Date(col(row, "signal_date")?))
                .put("working_from", J::opt_date(col(row, "working_from")?))
                .put("expires_after_session", J::opt_date(expires))
                .put("sessions_worked", J::Int(col(row, "sessions_worked")?))
                .put("sessions_allowed", J::Int(allowed))
                .put(
                    "expires_tonight",
                    J::Bool(expires.is_some_and(|e| e <= day)),
                )
                .put("broker_order_id", J::opt_str(col(row, "broker_order_id")?))
                .put("filled_quantity", J::Int(col(row, "filled_quantity")?))
                .put("simulated", J::Bool(col(row, "simulated")?))
                .done(),
        );
    }
    let mut open = Vec::new();
    let mut closed = Vec::new();
    for row in &positions {
        let instrument_id: i64 = col(row, "instrument_id")?;
        let (symbol, name) = names.get(&instrument_id).unwrap_or(&unknown);
        let entry = dec(row, "entry_avg")?;
        let initial_stop = dec(row, "initial_stop")?;
        let state_name: String = col(row, "state")?;
        if state_name == "OPEN" {
            let mark = closes.get(&instrument_id).copied();
            let quantity_open: i64 = col(row, "quantity_open")?;
            let gtt_id: Option<String> = col(row, "gtt_id")?;
            let entry_date: NaiveDate = col(row, "entry_date")?;
            let held: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM vb_breadth_daily WHERE user_id = $1 AND date > $2 \
                 AND date <= $3 AND thin_session IS false",
            )
            .bind(user_id)
            .bind(entry_date)
            .bind(day)
            .fetch_one(state.db.pool())
            .await
            .map_err(db)?;
            open.push(
                Obj::new()
                    .put("id", J::Int(col(row, "id")?))
                    .put("instrument_id", J::Int(instrument_id))
                    .put("symbol", J::s(symbol))
                    .put("name", J::s(name))
                    .put("entry_date", J::Date(entry_date))
                    .put("entry_avg", J::Dec(entry))
                    .put("quantity_open", J::Int(quantity_open))
                    .put("initial_stop", J::Dec(initial_stop))
                    .put("stop_price", J::Dec(dec(row, "stop_price")?))
                    .put("gtt_id", J::opt_str(gtt_id.clone()))
                    .put("naked", J::Bool(gtt_id.is_none() && quantity_open > 0))
                    .put("last_close", J::opt_dec(mark))
                    .put("last_price", J::Null)
                    .put("ema_21", J::Null)
                    .put("distance_to_ema_pct", J::Null)
                    .put(
                        "return_pct",
                        pct_over(mark, Some(entry))?.unwrap_or(J::Null),
                    )
                    .put(
                        "r_multiple",
                        r_multiple(entry, initial_stop, mark)?.unwrap_or(J::Null),
                    )
                    .put("sessions_held", J::Int(held))
                    .put("exit_queued_for", J::opt_date(col(row, "exit_queued_for")?))
                    .put(
                        "exit_reason_queued",
                        J::opt_str(col(row, "exit_reason_queued")?),
                    )
                    .put("simulated", J::Bool(col(row, "simulated")?))
                    .done(),
            );
        } else {
            let exit = odec(row, "exit_avg")?;
            closed.push(
                Obj::new()
                    .put("id", J::Int(col(row, "id")?))
                    .put("instrument_id", J::Int(instrument_id))
                    .put("symbol", J::s(symbol))
                    .put("name", J::s(name))
                    .put("entry_date", J::Date(col(row, "entry_date")?))
                    .put("closed_on", J::opt_date(col(row, "closed_on")?))
                    .put("entry_avg", J::Dec(entry))
                    .put("exit_avg", J::opt_dec(exit))
                    .put("quantity_entered", J::Int(col(row, "quantity_entered")?))
                    .put("close_reason", J::opt_str(col(row, "close_reason")?))
                    .put(
                        "return_pct",
                        pct_over(exit, Some(entry))?.unwrap_or(J::Null),
                    )
                    .put(
                        "r_multiple",
                        r_multiple(entry, initial_stop, exit)?.unwrap_or(J::Null),
                    )
                    .put("simulated", J::Bool(col(row, "simulated")?))
                    .done(),
            );
        }
    }
    let counts: Vec<(String, i64)> =
        sqlx::query_as("SELECT state, count(*) FROM vb_order WHERE user_id = $1 GROUP BY state")
            .bind(user_id)
            .fetch_all(state.db.pool())
            .await
            .map_err(db)?;
    let filled = counts
        .iter()
        .find(|(s, _)| s == "FILLED")
        .map_or(0, |(_, n)| *n);
    let resolved: i64 = counts
        .iter()
        .filter(|(s, _)| !LIVE_STATES.contains(&s.as_str()))
        .map(|(_, n)| *n)
        .sum();
    let rate = if resolved == 0 {
        J::Null
    } else {
        J::Dec(quantize(
            mul(
                div(Decimal::from(filled), Decimal::from(resolved))?,
                Decimal::from(100),
            )?,
            1,
        ))
    };
    let body = Obj::new()
        .put("working", J::Arr(working))
        .put("open_positions", J::Arr(open))
        .put("closed_positions", J::Arr(closed))
        .put(
            "fill_rate",
            Obj::new()
                .put("filled", J::Int(filled))
                .put("resolved", J::Int(resolved))
                .put("rate_pct", rate)
                .put(
                    "modelled_pct",
                    J::Dec(decimal_of_float(PUBLISHED.modelled_fill_rate_pct)),
                )
                .done(),
        )
        .done();
    Ok(canonical_response(&body))
}

/// `GET /vbt/backtest`: STRATEGY §4's published numbers and the newest finished run per source.
///
/// # Errors
/// 401, 404, 503, 500.
pub async fn get_backtest(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let rows = sqlx::query(
        "SELECT r.id, r.source, r.started_at, r.finished_at, r.params, r.stats, r.drift, r.error \
         FROM vb_backtest_run r JOIN ( \
             SELECT source, max(finished_at) AS finished_at FROM vb_backtest_run \
             WHERE user_id = $1 AND finished_at IS NOT NULL AND stats IS NOT NULL \
             GROUP BY source) n ON r.source = n.source AND r.finished_at = n.finished_at \
         WHERE r.user_id = $1 ORDER BY r.source",
    )
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let runs = rows
        .iter()
        .map(backtest_run_j)
        .collect::<Result<Vec<_>, _>>()?;
    let p = &PUBLISHED;
    let published = Obj::new()
        .put("start", J::s(&p.start))
        .put("end", J::s(&p.end))
        .put("years", J::Float(p.years))
        .put("cagr_pct", J::Float(p.cagr_pct))
        .put("max_drawdown_pct", J::Float(p.max_drawdown_pct))
        .put("trades", J::Int(p.trades))
        .put("win_rate_pct", J::Float(p.win_rate_pct))
        .put("profit_factor", J::Float(p.profit_factor))
        .put("avg_hold_sessions", J::Float(p.avg_hold_sessions))
        .put("exposure_pct", J::Float(p.exposure_pct))
        .put("sharpe", J::Float(p.sharpe))
        .put("in_sample_cagr_pct", J::Float(p.in_sample_cagr_pct))
        .put("in_sample_dd_pct", J::Float(p.in_sample_dd_pct))
        .put("out_of_sample_cagr_pct", J::Float(p.out_of_sample_cagr_pct))
        .put("out_of_sample_dd_pct", J::Float(p.out_of_sample_dd_pct))
        .put("modelled_fill_rate_pct", J::Float(p.modelled_fill_rate_pct))
        .done();
    Ok(canonical_response(
        &Obj::new()
            .put("published", published)
            .put("runs", J::Arr(runs))
            .done(),
    ))
}

/// `GET /vbt/config`: the editable numbers, the server's ceilings, and the job-owned fields.
///
/// # Errors
/// 401, 404 (no `vb_config` row, or not the tenant), 503, 500.
pub async fn get_config(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let row = sqlx::query(
        "SELECT sleeve_capital_inr, max_open_positions::bigint AS max_open_positions, \
                max_position_pct, stop_pct, dry_run_sessions::bigint AS dry_run_sessions, \
                first_live_sessions_left::bigint AS first_live_sessions_left, updated_at, \
                updated_by \
         FROM vb_config WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    let Some(row) = row else {
        return Err(not_found(
            "volume-breakout sleeve",
            &format!("user:{user_id}"),
        ));
    };
    let s = &state.settings;
    let body = Obj::new()
        .put(
            "sleeve_capital_inr",
            J::Dec(dec(&row, "sleeve_capital_inr")?),
        )
        .put(
            "max_open_positions",
            J::Int(col(&row, "max_open_positions")?),
        )
        .put("max_position_pct", J::Dec(dec(&row, "max_position_pct")?))
        .put("stop_pct", J::Dec(dec(&row, "stop_pct")?))
        .put("dry_run_sessions", J::Int(col(&row, "dry_run_sessions")?))
        .put(
            "first_live_sessions_left",
            J::Int(col(&row, "first_live_sessions_left")?),
        )
        .put("updated_at", J::Dt(col(&row, "updated_at")?))
        .put("updated_by", J::opt_str(col(&row, "updated_by")?))
        .put(
            "ceilings",
            Obj::new()
                .put(
                    "max_open_positions",
                    J::Str(s.vbt_max_open_positions_max.to_string()),
                )
                .put(
                    "max_position_pct",
                    J::Str(crate::wire::py_decimal(s.vbt_max_position_pct_max)),
                )
                .put(
                    "stop_pct",
                    J::Str(crate::wire::py_decimal(s.vbt_stop_pct_max)),
                )
                .done(),
        )
        .put("execution_enabled", J::Bool(s.vbt_execution_enabled))
        .put(
            "dry_run_sessions_required",
            J::Int(DRY_RUN_SESSIONS_REQUIRED),
        )
        .done();
    Ok(canonical_response(&body))
}

/// `GET /vbt/scan/{run_id}`: that run, if it is this tenant's.
///
/// # Errors
/// 401, 400 (a non-integer id), 404, 503, 500.
pub async fn get_scan(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(run_id): Path<String>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let mut v = Validation::new();
    let id = int_param(&mut v, "run_id", Some(&run_id));
    v.finish()?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let id = id_or_500(id.unwrap_or(LaxInt::Invalid))?;
    let row = sqlx::query(&format!(
        "SELECT {SCAN_COLUMNS} FROM vb_scan_run WHERE user_id = $1 AND id = $2"
    ))
    .bind(user_id)
    .bind(id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    match row {
        Some(row) => Ok(canonical_response(&scan_run_j(&row)?)),
        None => Err(not_found("scan", &id.to_string())),
    }
}

/// The GET routes of `baskfy_api.routers.vbt`, relative to `/api/v1`. The writes stay on the
/// Python API until they are ported.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/vbt/today", get(get_today))
        .route("/vbt/today/{instrument_id}/bars", get(get_bars))
        .route("/vbt/breadth", get(get_breadth))
        .route("/vbt/book", get(get_book))
        .route("/vbt/backtest", get(get_backtest))
        .route("/vbt/config", get(get_config))
        .route("/vbt/scan/{run_id}", get(get_scan))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;
    use std::str::FromStr;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).unwrap()
    }

    #[test]
    fn derived_percentages_are_two_places() {
        assert_eq!(
            pct_over(Some(d("110")), Some(d("100"))).unwrap(),
            Some(J::Dec(d("10.00")))
        );
        assert_eq!(
            pct_over(Some(d("199.9999")), Some(d("200"))).unwrap(),
            Some(J::NegZero(2))
        );
        assert_eq!(pct_over(Some(d("1")), Some(d("0"))).unwrap(), None);
        assert_eq!(pct_over(Some(d("1")), None).unwrap(), None);
        assert_eq!(
            r_multiple(d("100"), d("90"), Some(d("115"))).unwrap(),
            Some(J::Dec(d("1.50")))
        );
        assert_eq!(
            r_multiple(d("200"), d("190"), Some(d("199.9999"))).unwrap(),
            Some(J::NegZero(2))
        );
        assert_eq!(
            r_multiple(d("100"), d("100"), Some(d("115"))).unwrap(),
            None
        );
    }

    #[test]
    fn the_threshold_and_the_modelled_rate_print_as_python_does() {
        assert_eq!(threshold_pct().to_string(), "40.0");
        assert_eq!(
            decimal_of_float(PUBLISHED.modelled_fill_rate_pct).to_string(),
            "83.8"
        );
    }
}
