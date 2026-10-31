//! ``/swing/*`` — the swing book's read surfaces (SW4…SW15).
//!
//! Ports the GET routes of `baskfy_api.routers.swing` and the reads behind them
//! (`baskfy_api.swing`, `swing_watch.list_watch`, `swing_catalyst.latest_for`, `swing_journal`,
//! `swing_settings.read_config`/`to_view`). Python declares `APIRouter(prefix="/swing")`.
//!
//! Ported: `GET /setups`, `GET /scan/{run_id}`, `GET /setups/{instrument_id}/bars`,
//! `GET /market`, `GET /signals`, `GET /sectors`, `GET /watch`, `GET /positions`, `GET /config`,
//! `GET /journal`. The writes (`POST /scan`, `POST|PATCH|DELETE /watch`, `PATCH /config`) stay on
//! the Python API until writes are ported, and are not registered here.
//!
//! Every body is `canonical_json` (decimals unquoted, with their stored scale — a trigger is a
//! price somebody types into a broker). The live-quote overlay (`last_price`) is always `null`
//! here: this server has no Kite read yet, which is exactly Python's answer without a session
//! (DECISIONS-RS, lane api-sleeves). **No route may place, modify or cancel an order** (law 2).

use std::collections::HashMap;

pub(crate) mod writes;

use axum::Router;
use axum::extract::{Extension, Path, RawQuery, State};
use axum::response::Response;
use axum::routing::{get, patch, post};
use baskfy_db::OrderedJson;
use chrono::{DateTime, NaiveDate, Utc};
use rust_decimal::Decimal;
use sqlx::postgres::PgRow;

use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, ProblemType, bad_request, not_found};
use crate::query::{QueryParams, Validation};
use crate::routes::sleeves::common::{
    J, LaxInt, Obj, as_dict, canonical_response, db, dec, div, field_error, from_isoformat,
    get as col, id_or_500, int_param, mul, odec, ojson, overlay_last_price, py_list_repr, quantize,
    quantize_j, scoped_sole_user_id,
};
use crate::state::AppState;

/// `MINI_CHART_BARS`.
pub const MINI_CHART_BARS: i64 = 130;
/// `MAX_MARKET_SPAN` in days.
pub const MAX_MARKET_SPAN_DAYS: i64 = 400;
/// `HOT_SECTORS_ON_THE_STRIP`.
pub const HOT_SECTORS_ON_THE_STRIP: usize = 3;
/// `swing_journal.PAPER_SESSIONS_REQUIRED`.
pub const PAPER_SESSIONS_REQUIRED: i64 = 20;
/// `swing_journal.HISTOGRAM_BUCKETS`.
pub const HISTOGRAM_BUCKETS: [&str; 6] = ["<-1", "-1..0", "0..1", "1..2", "2..3", ">3"];
/// `swing_journal.MAX_TRADES`.
pub const MAX_TRADES: usize = 200;
/// `swing_watch.WATCHING`.
pub const WATCHING: &str = "WATCHING";

/// `SETUPS`, sorted (Python's `sorted(frozenset)` in the refusal).
const SETUPS: [&str; 3] = ["EP", "FLAG", "PARABOLIC_SHORT"];
/// `STATUSES`, sorted.
const STATUSES: [&str; 5] = [
    "BREAKOUT_TODAY",
    "EXHAUSTION",
    "GAP_DAY",
    "RUNNING",
    "SETTING_UP",
];

/// `DEFAULT_SWING_CONFIG.market.tiers`.
const TIERS: [(i64, f64); 4] = [(2, 25.0), (4, 50.0), (6, 75.0), (10, 100.0)];
/// `DEFAULT_SWING_CONFIG.market.lookback_trades`.
const LOOKBACK_TRADES: usize = 5;

fn opt_date_param(v: &mut Validation, q: &QueryParams, name: &str) -> Option<NaiveDate> {
    let raw = q.get(name);
    v.date(name, raw)
}

/// `_one_of(value, allowed, field)`.
fn one_of(value: Option<&str>, allowed: &[&str], field: &str) -> Result<(), Problem> {
    match value {
        None => Ok(()),
        Some(v) if allowed.contains(&v) => Ok(()),
        Some(v) => Err(bad_request(
            &format!("{} is not a {field}.", baskfy_providers::py::repr_str(v)),
            Some(vec![field_error(
                field,
                &format!("choose one of {}", py_list_repr(allowed)),
            )]),
        )),
    }
}

async fn swing_tenant(state: &AppState, principal: &Principal) -> Result<i64, Problem> {
    scoped_sole_user_id(state, principal.user_id, None).await
}

// ---------------------------------------------------------------------------------------------
// Scan runs
// ---------------------------------------------------------------------------------------------

const SCAN_COLUMNS: &str =
    "id, status, requested_at, started_at, finished_at, session_date, provisional, detail, error";

fn scan_run_j(row: &PgRow) -> Result<J, Problem> {
    let detail = as_dict(ojson(row, "detail")?);
    let funnel = detail
        .as_ref()
        .and_then(|d| d.get("funnel").cloned())
        .and_then(|f| as_dict(Some(f)));
    Ok(Obj::new()
        .put("run_id", J::Int(col(row, "id")?))
        .put("status", J::Str(col(row, "status")?))
        .put("requested_at", J::Dt(col(row, "requested_at")?))
        .put("started_at", J::opt_dt(col(row, "started_at")?))
        .put("finished_at", J::opt_dt(col(row, "finished_at")?))
        .put("session_date", J::opt_date(col(row, "session_date")?))
        .put("provisional", J::Bool(col(row, "provisional")?))
        .put("funnel", funnel.map_or(J::Null, J::Raw))
        .put("detail", detail.map_or(J::Null, J::Raw))
        .put("error", J::opt_str(col(row, "error")?))
        .done())
}

/// `GET /swing/scan/{run_id}`.
///
/// # Errors
/// 401, 400 (a non-integer id), 404 (no such run of this tenant's, or not the tenant), 500.
pub async fn get_scan(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(run_id): Path<String>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let mut v = Validation::new();
    let id = int_param(&mut v, "run_id", Some(&run_id));
    v.finish()?;
    let user_id = swing_tenant(&state, &principal).await?;
    let id = id_or_500(id.unwrap_or(LaxInt::Invalid))?;
    let row = sqlx::query(&format!(
        "SELECT {SCAN_COLUMNS} FROM sw_scan_run WHERE user_id = $1 AND id = $2"
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

// ---------------------------------------------------------------------------------------------
// Catalysts (`swing_catalyst.latest_for`)
// ---------------------------------------------------------------------------------------------

async fn catalysts(
    state: &AppState,
    user_id: i64,
    instrument_ids: &[i64],
) -> Result<HashMap<i64, J>, Problem> {
    let mut wanted: Vec<i64> = instrument_ids.to_vec();
    wanted.sort_unstable();
    wanted.dedup();
    if wanted.is_empty() {
        return Ok(HashMap::new());
    }
    let rows = sqlx::query(
        "SELECT instrument_id, source, headline, published_at, url, earnings_date \
         FROM sw_catalyst WHERE user_id = $1 AND instrument_id = ANY($2) \
         ORDER BY instrument_id, published_at DESC NULLS LAST, id DESC",
    )
    .bind(user_id)
    .bind(&wanted)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut newest: HashMap<i64, (String, Option<DateTime<Utc>>, String)> = HashMap::new();
    let mut earnings: HashMap<i64, NaiveDate> = HashMap::new();
    for row in &rows {
        let key: i64 = col(row, "instrument_id")?;
        let source: String = col(row, "source")?;
        if source == "NSE_EVENT_CALENDAR" {
            let e: Option<NaiveDate> = col(row, "earnings_date")?;
            if let Some(e) = e {
                earnings.entry(key).or_insert(e);
            }
            continue;
        }
        if let std::collections::hash_map::Entry::Vacant(slot) = newest.entry(key) {
            slot.insert((
                col(row, "headline")?,
                col(row, "published_at")?,
                col(row, "url")?,
            ));
        }
    }
    let mut out = HashMap::new();
    for key in newest.keys().chain(earnings.keys()) {
        if out.contains_key(key) {
            continue;
        }
        let top = newest.get(key);
        let earn = earnings.get(key).copied();
        // `CatalystView.empty`: no url and no earnings date. `url` is NOT NULL, so a feed row
        // always makes it non-empty.
        if top.is_none() && earn.is_none() {
            continue;
        }
        out.insert(
            *key,
            Obj::new()
                .put("headline", top.map_or(J::Null, |t| J::Str(t.0.clone())))
                .put("published_at", J::opt_dt(top.and_then(|t| t.1)))
                .put("url", top.map_or(J::Null, |t| J::Str(t.2.clone())))
                .put("earnings_date", J::opt_date(earn))
                .done(),
        );
    }
    Ok(out)
}

// ---------------------------------------------------------------------------------------------
// GET /swing/setups
// ---------------------------------------------------------------------------------------------

/// `latest_detected_date`: `max(sw_market_daily.date)`, else `max(sw_setup_daily.date)`.
async fn latest_detected_date(
    state: &AppState,
    user_id: i64,
) -> Result<Option<NaiveDate>, Problem> {
    let market = latest_market_date(state, user_id).await?;
    if market.is_some() {
        return Ok(market);
    }
    sqlx::query_scalar("SELECT max(date) FROM sw_setup_daily WHERE user_id = $1")
        .bind(user_id)
        .fetch_one(state.db.pool())
        .await
        .map_err(db)
}

async fn latest_market_date(state: &AppState, user_id: i64) -> Result<Option<NaiveDate>, Problem> {
    sqlx::query_scalar("SELECT max(date) FROM sw_market_daily WHERE user_id = $1")
        .bind(user_id)
        .fetch_one(state.db.pool())
        .await
        .map_err(db)
}

async fn latest_scan_run(state: &AppState, user_id: i64) -> Result<Option<J>, Problem> {
    let row = sqlx::query(&format!(
        "SELECT {SCAN_COLUMNS} FROM sw_scan_run WHERE user_id = $1 \
         ORDER BY requested_at DESC, id DESC LIMIT 1"
    ))
    .bind(user_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    row.as_ref().map(scan_run_j).transpose()
}

/// `SetupRow.stop_distance_pct` (and the router's `_distance`): `((a - b) / a * 100)` to 0.01,
/// a negative that rounds to zero spelled `-0.00` as Python's `quantize` leaves it.
fn pct_below(top: Option<Decimal>, below: Option<Decimal>) -> Result<Option<J>, Problem> {
    let (Some(top), Some(below)) = (top, below) else {
        return Ok(None);
    };
    if top <= Decimal::ZERO {
        return Ok(None);
    }
    let ratio = div(top - below, top)?;
    Ok(Some(quantize_j(mul(ratio, Decimal::from(100))?, 2)))
}

/// `GET /swing/setups?date=&setup=&status=`.
///
/// # Errors
/// 401, 400 (a bad date, or a setup/status outside the vocabulary), 404 (not the tenant), 503
/// (no tenant), 500.
pub async fn get_setups(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let on = opt_date_param(&mut v, &q, "date");
    let setup = q.get("setup");
    let status = q.get("status");
    v.finish()?;
    let user_id = swing_tenant(&state, &principal).await?;
    one_of(setup, &SETUPS, "setup")?;
    one_of(status, &STATUSES, "status")?;

    let as_of = match on {
        Some(d) => Some(d),
        None => latest_detected_date(&state, user_id).await?,
    };
    let last_scan = latest_scan_run(&state, user_id).await?;
    let Some(as_of) = as_of else {
        let body = Obj::new()
            .put("as_of", J::Null)
            .put("as_of_provisional", J::Bool(false))
            .put("scanned_at", J::Null)
            .put("last_scan", last_scan.unwrap_or(J::Null))
            .put("gate", J::Null)
            .put("exposure_level", J::Null)
            .put("max_open_positions", J::Null)
            .put("max_exposure_pct", J::Null)
            .put("new_entries_allowed", J::Null)
            .put("funnel", J::Null)
            .put("data", J::Arr(Vec::new()))
            .done();
        return Ok(canonical_response(&body));
    };

    let mut sql = String::from(
        "SELECT d.instrument_id, i.symbol, i.name, d.setup, d.status, d.score, d.close, \
                d.trigger, d.stop_ref, d.pivot_high, d.adr_pct, d.prior_move_pct, \
                d.base_depth_pct, d.tightness_adr, d.dryup_ratio, d.rvol, d.gap_pct, \
                d.turnover_avg, d.base_bars::bigint AS base_bars, \
                d.up_streak::bigint AS up_streak, d.locked_upper_circuit, d.sector_slug, \
                d.listed_within_2y, d.provisional \
         FROM sw_setup_daily d JOIN instrument i ON i.id = d.instrument_id \
         WHERE d.user_id = $1 AND d.date = $2",
    );
    if setup.is_some() {
        sql.push_str(" AND d.setup = $3");
    }
    if status.is_some() {
        sql.push_str(if setup.is_some() {
            " AND d.status = $4"
        } else {
            " AND d.status = $3"
        });
    }
    sql.push_str(" ORDER BY d.setup, d.score DESC, i.symbol");
    let mut query = sqlx::query(&sql).bind(user_id).bind(as_of);
    if let Some(s) = setup {
        query = query.bind(s);
    }
    if let Some(s) = status {
        query = query.bind(s);
    }
    let found = query.fetch_all(state.db.pool()).await.map_err(db)?;
    let ids: Vec<i64> = found
        .iter()
        .map(|r| col::<i64>(r, "instrument_id"))
        .collect::<Result<_, _>>()?;
    let feed = catalysts(&state, user_id, &ids).await?;

    let mut data = Vec::with_capacity(found.len());
    let mut any_provisional = false;
    for row in &found {
        let instrument_id: i64 = col(row, "instrument_id")?;
        let trigger = odec(row, "trigger")?;
        let stop_ref = odec(row, "stop_ref")?;
        any_provisional |= col::<bool>(row, "provisional")?;
        data.push(
            Obj::new()
                .put("instrument_id", J::Int(instrument_id))
                .put("symbol", J::Str(col(row, "symbol")?))
                .put("name", J::Str(col(row, "name")?))
                .put("setup", J::Str(col(row, "setup")?))
                .put("status", J::Str(col(row, "status")?))
                .put("score", J::Dec(dec(row, "score")?))
                .put("close", J::opt_dec(odec(row, "close")?))
                .put("last_price", J::Null)
                .put("trigger", J::opt_dec(trigger))
                .put("stop_ref", J::opt_dec(stop_ref))
                .put("pivot_high", J::opt_dec(odec(row, "pivot_high")?))
                .put(
                    "stop_distance_pct",
                    pct_below(trigger, stop_ref)?.unwrap_or(J::Null),
                )
                .put("adr_pct", J::opt_dec(odec(row, "adr_pct")?))
                .put("prior_move_pct", J::opt_dec(odec(row, "prior_move_pct")?))
                .put("base_depth_pct", J::opt_dec(odec(row, "base_depth_pct")?))
                .put("tightness_adr", J::opt_dec(odec(row, "tightness_adr")?))
                .put("dryup_ratio", J::opt_dec(odec(row, "dryup_ratio")?))
                .put("rvol", J::opt_dec(odec(row, "rvol")?))
                .put("gap_pct", J::opt_dec(odec(row, "gap_pct")?))
                .put("turnover_avg", J::opt_int(col(row, "turnover_avg")?))
                .put("base_bars", J::opt_int(col(row, "base_bars")?))
                .put("up_streak", J::opt_int(col(row, "up_streak")?))
                .put(
                    "locked_upper_circuit",
                    J::Bool(col(row, "locked_upper_circuit")?),
                )
                .put("sector_slug", J::opt_str(col(row, "sector_slug")?))
                .put("listed_within_2y", J::Bool(col(row, "listed_within_2y")?))
                .put(
                    "catalyst_feed",
                    feed.get(&instrument_id).cloned().unwrap_or(J::Null),
                )
                .done(),
        );
    }
    overlay_last_price(&mut data).await?;

    let market = sqlx::query(
        "SELECT gate, exposure_level::bigint AS exposure_level, \
                max_open_positions::bigint AS max_open_positions, max_exposure_pct, \
                new_entries_allowed, provisional, detail \
         FROM sw_market_daily WHERE user_id = $1 AND date = $2",
    )
    .bind(user_id)
    .bind(as_of)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    let detail = match &market {
        Some(m) => ojson(m, "detail")?,
        None => None,
    };
    let funnel = match &detail {
        Some(d @ OrderedJson::Object(_)) => as_dict(d.get("funnel").cloned()),
        _ => None,
    };
    let scanned_at = match &detail {
        Some(d @ OrderedJson::Object(_)) => match d.get("scan") {
            Some(scan @ OrderedJson::Object(_)) => match scan.get("scanned_at") {
                Some(OrderedJson::Str(s)) => from_isoformat(s),
                _ => None,
            },
            _ => None,
        },
        _ => None,
    };
    let (gate, level, max_open, max_exposure, allowed, provisional) = match &market {
        Some(m) => (
            J::Str(col(m, "gate")?),
            J::Int(col(m, "exposure_level")?),
            J::Int(col(m, "max_open_positions")?),
            J::Dec(dec(m, "max_exposure_pct")?),
            J::Bool(col(m, "new_entries_allowed")?),
            col::<bool>(m, "provisional")?,
        ),
        None => (J::Null, J::Null, J::Null, J::Null, J::Null, false),
    };
    let body = Obj::new()
        .put("as_of", J::Date(as_of))
        .put("as_of_provisional", J::Bool(provisional || any_provisional))
        .put("scanned_at", scanned_at.map_or(J::Null, J::DtFixed))
        .put("last_scan", last_scan.unwrap_or(J::Null))
        .put("gate", gate)
        .put("exposure_level", level)
        .put("max_open_positions", max_open)
        .put("max_exposure_pct", max_exposure)
        .put("new_entries_allowed", allowed)
        .put("funnel", funnel.map_or(J::Null, J::Raw))
        .put("data", J::Arr(data))
        .done();
    Ok(canonical_response(&body))
}

// ---------------------------------------------------------------------------------------------
// GET /swing/setups/{instrument_id}/bars
// ---------------------------------------------------------------------------------------------

/// `_mean(values, index, window)`: the trailing mean to 0.01, `None` before the window fills.
fn trailing_mean(
    values: &[Decimal],
    index: usize,
    window: usize,
) -> Result<Option<Decimal>, Problem> {
    if index + 1 < window {
        return Ok(None);
    }
    let sum: Decimal = values[index + 1 - window..=index].iter().sum();
    let count = Decimal::from(i64::try_from(window).unwrap_or(1));
    Ok(Some(quantize(div(sum, count)?, 2)))
}

/// `GET /swing/setups/{instrument_id}/bars?date=&count=`.
///
/// # Errors
/// 401, 400 (bad id, date or count outside 20..=500), 404 (no such instrument, or not the
/// tenant), 503, 500.
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
    let end = opt_date_param(&mut v, &q, "date");
    let mut count = MINI_CHART_BARS;
    match int_param(&mut v, "count", q.get("count")) {
        Some(LaxInt::Small(c)) if c < 20 => v.error(
            "count",
            "Input should be greater than or equal to 20",
            "greater_than_equal",
        ),
        Some(LaxInt::Big { negative: true }) => v.error(
            "count",
            "Input should be greater than or equal to 20",
            "greater_than_equal",
        ),
        Some(LaxInt::Small(c)) if c > 500 => v.error(
            "count",
            "Input should be less than or equal to 500",
            "less_than_equal",
        ),
        Some(LaxInt::Big { negative: false }) => v.error(
            "count",
            "Input should be less than or equal to 500",
            "less_than_equal",
        ),
        Some(LaxInt::Small(c)) => count = c,
        _ => {}
    }
    v.finish()?;
    swing_tenant(&state, &principal).await?;
    let id = id_or_500(id.unwrap_or(LaxInt::Invalid))?;
    let symbol: Option<String> = sqlx::query_scalar("SELECT symbol FROM instrument WHERE id = $1")
        .bind(id)
        .fetch_optional(state.db.pool())
        .await
        .map_err(db)?;
    let Some(symbol) = symbol else {
        return Err(not_found("instrument", &id.to_string()));
    };
    let rows = if let Some(end) = end {
        sqlx::query(
            "SELECT date, close FROM ohlcv_daily WHERE instrument_id = $1 AND date <= $2 \
             ORDER BY date DESC LIMIT $3",
        )
        .bind(id)
        .bind(end)
        .bind(count)
        .fetch_all(state.db.pool())
        .await
    } else {
        sqlx::query(
            "SELECT date, close FROM ohlcv_daily WHERE instrument_id = $1 \
             ORDER BY date DESC LIMIT $2",
        )
        .bind(id)
        .bind(count)
        .fetch_all(state.db.pool())
        .await
    }
    .map_err(db)?;
    let mut dates = Vec::with_capacity(rows.len());
    let mut closes = Vec::with_capacity(rows.len());
    for row in rows.iter().rev() {
        dates.push(col::<NaiveDate>(row, "date")?);
        closes.push(dec(row, "close")?);
    }
    let mut data = Vec::with_capacity(dates.len());
    for (i, date) in dates.iter().enumerate() {
        data.push(
            Obj::new()
                .put("date", J::Date(*date))
                .put("close", J::Dec(closes[i]))
                .put("ma_fast", J::opt_dec(trailing_mean(&closes, i, 10)?))
                .put("ma_slow", J::opt_dec(trailing_mean(&closes, i, 20)?))
                .done(),
        );
    }
    Ok(canonical_response(
        &Obj::new()
            .put("instrument_id", J::Int(id))
            .put("symbol", J::Str(symbol))
            .put("adjusted", J::Bool(true))
            .put("data", J::Arr(data))
            .done(),
    ))
}

// ---------------------------------------------------------------------------------------------
// GET /swing/market
// ---------------------------------------------------------------------------------------------

/// `GET /swing/market?from=&to=`.
///
/// # Errors
/// 401, 400 (bad dates, a backwards range, a span over 400 days), 404, 503, 500.
pub async fn get_market(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let from = opt_date_param(&mut v, &q, "from");
    let to = opt_date_param(&mut v, &q, "to");
    v.finish()?;
    let user_id = swing_tenant(&state, &principal).await?;
    if let (Some(f), Some(t)) = (from, to) {
        if f > t {
            return Err(bad_request(
                "`from` is after `to`.",
                Some(vec![field_error("from", "the range runs forwards")]),
            ));
        }
        if (t - f).num_days() > MAX_MARKET_SPAN_DAYS {
            return Err(bad_request(
                &format!("that span is wider than {MAX_MARKET_SPAN_DAYS} days."),
                Some(vec![field_error("from", "ask for a narrower range")]),
            ));
        }
    }
    let mut sql = String::from(
        "SELECT date, constituent_count::bigint AS constituent_count, pct_up_strong_1m, \
                pct_new_52w_high, pct_above_ma_slow, index_slug, index_close, index_ma_fast, \
                index_ma_slow, gate, exposure_level::bigint AS exposure_level, \
                max_open_positions::bigint AS max_open_positions, max_exposure_pct, \
                new_entries_allowed, parabolic_count::bigint AS parabolic_count, drawdown_pct, \
                drawdown_locked \
         FROM sw_market_daily WHERE user_id = $1",
    );
    let mut n = 1;
    if from.is_some() {
        n += 1;
        sql.push_str(&format!(" AND date >= ${n}"));
    }
    if to.is_some() {
        n += 1;
        sql.push_str(&format!(" AND date <= ${n}"));
    }
    sql.push_str(" ORDER BY date");
    let mut query = sqlx::query(&sql).bind(user_id);
    if let Some(f) = from {
        query = query.bind(f);
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
                .put("constituent_count", J::Int(col(row, "constituent_count")?))
                .put(
                    "pct_up_strong_1m",
                    J::opt_dec(odec(row, "pct_up_strong_1m")?),
                )
                .put(
                    "pct_new_52w_high",
                    J::opt_dec(odec(row, "pct_new_52w_high")?),
                )
                .put(
                    "pct_above_ma_slow",
                    J::opt_dec(odec(row, "pct_above_ma_slow")?),
                )
                .put("index_slug", J::opt_str(col(row, "index_slug")?))
                .put("index_close", J::opt_dec(odec(row, "index_close")?))
                .put("index_ma_fast", J::opt_dec(odec(row, "index_ma_fast")?))
                .put("index_ma_slow", J::opt_dec(odec(row, "index_ma_slow")?))
                .put("gate", J::Str(col(row, "gate")?))
                .put("exposure_level", J::Int(col(row, "exposure_level")?))
                .put(
                    "max_open_positions",
                    J::Int(col(row, "max_open_positions")?),
                )
                .put("max_exposure_pct", J::Dec(dec(row, "max_exposure_pct")?))
                .put(
                    "new_entries_allowed",
                    J::Bool(col(row, "new_entries_allowed")?),
                )
                .put("parabolic_count", J::Int(col(row, "parabolic_count")?))
                .put("drawdown_pct", J::Dec(dec(row, "drawdown_pct")?))
                .put("drawdown_locked", J::Bool(col(row, "drawdown_locked")?))
                .done(),
        );
    }
    Ok(canonical_response(
        &Obj::new().put("data", J::Arr(data)).done(),
    ))
}

// ---------------------------------------------------------------------------------------------
// GET /swing/signals
// ---------------------------------------------------------------------------------------------

/// `GET /swing/signals?date=&instrument_id=`.
///
/// # Errors
/// 401, 400 (bad date or id), 404, 503, 500.
pub async fn get_signals(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let on = opt_date_param(&mut v, &q, "date");
    let instrument = int_param(&mut v, "instrument_id", q.get("instrument_id"));
    v.finish()?;
    let user_id = swing_tenant(&state, &principal).await?;
    let as_of = match on {
        Some(d) => Some(d),
        None => sqlx::query_scalar("SELECT max(session_date) FROM sw_signal WHERE user_id = $1")
            .bind(user_id)
            .fetch_one(state.db.pool())
            .await
            .map_err(db)?,
    };
    let Some(as_of) = as_of else {
        return Ok(canonical_response(
            &Obj::new()
                .put("session_date", J::Null)
                .put("data", J::Arr(Vec::new()))
                .done(),
        ));
    };
    let instrument = instrument.map(id_or_500).transpose()?;
    let mut sql = String::from(
        "SELECT s.id, s.watch_id, s.instrument_id, i.symbol, i.name, s.setup, s.session_date, \
                s.raised_at, s.state, s.or_window_minutes::bigint AS or_window_minutes, \
                s.range_high, s.range_low, s.low_of_day, s.last_price, s.entry, s.stop, \
                s.plan_line_id \
         FROM sw_signal s JOIN instrument i ON i.id = s.instrument_id \
         WHERE s.user_id = $1 AND s.session_date = $2",
    );
    if instrument.is_some() {
        sql.push_str(" AND s.instrument_id = $3");
    }
    sql.push_str(" ORDER BY s.raised_at DESC, s.id DESC");
    let mut query = sqlx::query(&sql).bind(user_id).bind(as_of);
    if let Some(i) = instrument {
        query = query.bind(i);
    }
    let rows = query.fetch_all(state.db.pool()).await.map_err(db)?;
    let mut data = Vec::with_capacity(rows.len());
    for row in &rows {
        data.push(
            Obj::new()
                .put("id", J::Int(col(row, "id")?))
                .put("watch_id", J::opt_int(col(row, "watch_id")?))
                .put("instrument_id", J::Int(col(row, "instrument_id")?))
                .put("symbol", J::Str(col(row, "symbol")?))
                .put("name", J::Str(col(row, "name")?))
                .put("setup", J::Str(col(row, "setup")?))
                .put("session_date", J::Date(col(row, "session_date")?))
                .put("raised_at", J::Dt(col(row, "raised_at")?))
                .put("state", J::Str(col(row, "state")?))
                .put(
                    "or_window_minutes",
                    J::opt_int(col(row, "or_window_minutes")?),
                )
                .put("range_high", J::opt_dec(odec(row, "range_high")?))
                .put("range_low", J::opt_dec(odec(row, "range_low")?))
                .put("low_of_day", J::opt_dec(odec(row, "low_of_day")?))
                .put("last_price", J::opt_dec(odec(row, "last_price")?))
                .put("entry", J::opt_dec(odec(row, "entry")?))
                .put("stop", J::opt_dec(odec(row, "stop")?))
                .put("plan_line_id", J::opt_int(col(row, "plan_line_id")?))
                .done(),
        );
    }
    Ok(canonical_response(
        &Obj::new()
            .put("session_date", J::Date(as_of))
            .put("data", J::Arr(data))
            .done(),
    ))
}

// ---------------------------------------------------------------------------------------------
// GET /swing/sectors
// ---------------------------------------------------------------------------------------------

/// Python's `float(x or 0.0)` for a stored JSON value.
fn py_float_or_zero(v: Option<&OrderedJson>) -> Result<f64, Problem> {
    match v {
        None | Some(OrderedJson::Null) => Ok(0.0),
        Some(OrderedJson::Bool(b)) => Ok(if *b { 1.0 } else { 0.0 }),
        #[allow(clippy::cast_precision_loss)]
        Some(OrderedJson::Int(i)) => Ok(*i as f64),
        #[allow(clippy::cast_precision_loss)]
        Some(OrderedJson::UInt(u)) => Ok(*u as f64),
        Some(OrderedJson::BigInt(big)) => big
            .parse::<f64>()
            .map_err(|_| crate::problems::internal_error()),
        Some(OrderedJson::Float(f)) => Ok(*f),
        Some(OrderedJson::Str(s)) if s.is_empty() => Ok(0.0),
        Some(OrderedJson::Str(s)) => s
            .trim()
            .parse::<f64>()
            .map_err(|_| crate::problems::internal_error()),
        Some(_) => Err(crate::problems::internal_error()),
    }
}

/// Python's `int(x or 0)` for a stored JSON value.
fn py_int_or_zero(v: Option<&OrderedJson>) -> Result<i64, Problem> {
    match v {
        None | Some(OrderedJson::Null) => Ok(0),
        Some(OrderedJson::Bool(b)) => Ok(i64::from(*b)),
        Some(OrderedJson::Int(i)) => Ok(*i),
        #[allow(clippy::cast_possible_truncation)]
        Some(OrderedJson::Float(f)) if f.is_finite() => Ok(f.trunc() as i64),
        Some(OrderedJson::Str(s)) if s.is_empty() => Ok(0),
        Some(OrderedJson::Str(s)) => s
            .trim()
            .parse::<i64>()
            .map_err(|_| crate::problems::internal_error()),
        Some(_) => Err(crate::problems::internal_error()),
    }
}

/// `GET /swing/sectors?date=`.
///
/// # Errors
/// 401, 400 (bad date), 404, 503, 500.
pub async fn get_sectors(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let on = opt_date_param(&mut v, &q, "date");
    v.finish()?;
    let user_id = swing_tenant(&state, &principal).await?;
    let as_of = match on {
        Some(d) => Some(d),
        None => latest_market_date(&state, user_id).await?,
    };
    let empty = |as_of: Option<NaiveDate>| {
        canonical_response(
            &Obj::new()
                .put("as_of", J::opt_date(as_of))
                .put("data", J::Arr(Vec::new()))
                .done(),
        )
    };
    let Some(as_of) = as_of else {
        return Ok(empty(None));
    };
    let row = sqlx::query("SELECT detail FROM sw_market_daily WHERE user_id = $1 AND date = $2")
        .bind(user_id)
        .bind(as_of)
        .fetch_optional(state.db.pool())
        .await
        .map_err(db)?;
    let detail = match &row {
        Some(r) => as_dict(ojson(r, "detail")?),
        None => None,
    };
    let Some(listed) = detail.as_ref().and_then(|d| match d.get("sectors") {
        Some(OrderedJson::Array(items)) => Some(items.clone()),
        _ => None,
    }) else {
        return Ok(empty(Some(as_of)));
    };
    let counted: Vec<(String, i64)> = sqlx::query_as(
        "SELECT sector_slug, count(*) FROM sw_setup_daily WHERE user_id = $1 AND date = $2 \
         AND sector_slug IS NOT NULL GROUP BY sector_slug",
    )
    .bind(user_id)
    .bind(as_of)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let counts: HashMap<String, i64> = counted.into_iter().collect();
    let mut data = Vec::new();
    for (index, entry) in listed.iter().enumerate() {
        if !matches!(entry, OrderedJson::Object(_)) {
            continue;
        }
        let slug = entry
            .get("slug")
            .map_or_else(String::new, crate::routes::sleeves::common::py_str_json);
        data.push(
            Obj::new()
                .put("slug", J::Str(slug.clone()))
                .put(
                    "pct_above_ma_slow",
                    J::Float(py_float_or_zero(entry.get("pct_above_ma_slow"))?),
                )
                .put("members", J::Int(py_int_or_zero(entry.get("members"))?))
                .put(
                    "candidates",
                    J::Int(counts.get(&slug).copied().unwrap_or(0)),
                )
                .put("hot", J::Bool(index < HOT_SECTORS_ON_THE_STRIP))
                .done(),
        );
    }
    Ok(canonical_response(
        &Obj::new()
            .put("as_of", J::Date(as_of))
            .put("data", J::Arr(data))
            .done(),
    ))
}

// ---------------------------------------------------------------------------------------------
// GET /swing/watch
// ---------------------------------------------------------------------------------------------

/// `_latest_closes(session, ids)`: the newest adjusted close per instrument.
async fn latest_closes(state: &AppState, ids: &[i64]) -> Result<HashMap<i64, Decimal>, Problem> {
    if ids.is_empty() {
        return Ok(HashMap::new());
    }
    let rows = sqlx::query(
        "SELECT o.instrument_id, o.close FROM ohlcv_daily o JOIN ( \
             SELECT instrument_id, max(date) AS date FROM ohlcv_daily \
             WHERE instrument_id = ANY($1) GROUP BY instrument_id) n \
         ON o.instrument_id = n.instrument_id AND o.date = n.date",
    )
    .bind(ids)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut out = HashMap::new();
    for row in &rows {
        out.insert(col::<i64>(row, "instrument_id")?, dec(row, "close")?);
    }
    Ok(out)
}

/// `list_watch(...)` rendered as `SwingWatchOut`s, each with its row id (newest first).
///
/// # Errors
/// 500.
pub(crate) async fn watch_rows(
    state: &AppState,
    user_id: i64,
    wanted: Option<&str>,
) -> Result<Vec<(i64, J)>, Problem> {
    let mut sql = String::from(
        "SELECT w.id, w.instrument_id, i.symbol, i.name, w.setup, w.source, w.added_on, \
                w.expires_on, w.trigger, w.stop_ref, w.note, w.catalyst, w.state, w.score, \
                w.adr_pct, w.focus, w.reconfirmed_on, w.earnings_date \
         FROM sw_watch w JOIN instrument i ON i.id = w.instrument_id \
         WHERE w.user_id = $1",
    );
    if wanted.is_some() {
        sql.push_str(" AND w.state = $2");
    }
    sql.push_str(" ORDER BY w.added_on DESC, i.symbol");
    let mut query = sqlx::query(&sql).bind(user_id);
    if let Some(w) = wanted {
        query = query.bind(w);
    }
    let rows = query.fetch_all(state.db.pool()).await.map_err(db)?;
    let ids: Vec<i64> = rows
        .iter()
        .map(|r| col::<i64>(r, "instrument_id"))
        .collect::<Result<_, _>>()?;
    let (closes, feed) = if rows.is_empty() {
        (HashMap::new(), HashMap::new())
    } else {
        (
            latest_closes(state, &ids).await?,
            catalysts(state, user_id, &ids).await?,
        )
    };
    let mut data = Vec::with_capacity(rows.len());
    for row in &rows {
        let instrument_id: i64 = col(row, "instrument_id")?;
        let trigger = odec(row, "trigger")?;
        let last_close = closes.get(&instrument_id).copied();
        let id: i64 = col(row, "id")?;
        data.push((
            id,
            Obj::new()
                .put("id", J::Int(id))
                .put("instrument_id", J::Int(instrument_id))
                .put("symbol", J::Str(col(row, "symbol")?))
                .put("name", J::Str(col(row, "name")?))
                .put("setup", J::Str(col(row, "setup")?))
                .put("source", J::Str(col(row, "source")?))
                .put("added_on", J::Date(col(row, "added_on")?))
                .put("expires_on", J::opt_date(col(row, "expires_on")?))
                .put("trigger", J::opt_dec(trigger))
                .put("stop_ref", J::opt_dec(odec(row, "stop_ref")?))
                .put(
                    "distance_to_trigger_pct",
                    pct_below(trigger, last_close)?.unwrap_or(J::Null),
                )
                .put("last_close", J::opt_dec(last_close))
                .put("last_price", J::Null)
                .put("note", J::opt_str(col(row, "note")?))
                .put("catalyst", J::opt_str(col(row, "catalyst")?))
                .put("state", J::Str(col(row, "state")?))
                .put("score", J::opt_dec(odec(row, "score")?))
                .put("adr_pct", J::opt_dec(odec(row, "adr_pct")?))
                .put("focus", J::Bool(col(row, "focus")?))
                .put("reconfirmed_on", J::opt_date(col(row, "reconfirmed_on")?))
                .put("earnings_date", J::opt_date(col(row, "earnings_date")?))
                .put(
                    "catalyst_feed",
                    feed.get(&instrument_id).cloned().unwrap_or(J::Null),
                )
                .done(),
        ));
    }
    let marks = crate::routes::sleeves::common::live_marks(
        &data.iter().map(|(_, j)| j).collect::<Vec<_>>(),
    )
    .await?;
    for (_, j) in &mut data {
        let mark = marks
            .get(&crate::routes::sleeves::common::symbol_key(j))
            .copied();
        crate::routes::sleeves::common::set_field(j, "last_price", J::opt_dec(mark));
        if let J::Obj(fields) = j {
            recompute_distance(fields)?;
        }
    }
    Ok(data)
}

/// `distance_to_trigger_pct` again, from a rendered row's own `trigger` and `last_close` (after
/// a write overlaid the in-memory trigger).
///
/// # Errors
/// 500 on arithmetic overflow.
pub(crate) fn recompute_distance(fields: &mut [(String, J)]) -> Result<(), Problem> {
    let get = |fields: &[(String, J)], key: &str| {
        fields
            .iter()
            .find(|(k, _)| k == key)
            .and_then(|(_, v)| match v {
                J::Dec(d) => Some(*d),
                _ => None,
            })
    };
    let mark = get(fields, "last_price").or_else(|| get(fields, "last_close"));
    let distance = pct_below(get(fields, "trigger"), mark)?.unwrap_or(J::Null);
    for (k, v) in fields.iter_mut() {
        if k == "distance_to_trigger_pct" {
            *v = distance.clone();
        }
    }
    Ok(())
}

/// `GET /swing/watch?state=` (`WATCHING` by default; `all` for every row).
///
/// # Errors
/// 401, 404, 503, 500.
pub async fn get_watch(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let wanted = match q.get("state") {
        None => Some(WATCHING),
        Some("all") => None,
        Some(s) => Some(s),
    };
    let user_id = swing_tenant(&state, &principal).await?;
    let data = watch_rows(&state, user_id, wanted)
        .await?
        .into_iter()
        .map(|(_, j)| j)
        .collect();
    Ok(canonical_response(
        &Obj::new().put("data", J::Arr(data)).done(),
    ))
}

// ---------------------------------------------------------------------------------------------
// GET /swing/positions
// ---------------------------------------------------------------------------------------------

async fn latest_plan(state: &AppState, user_id: i64) -> Result<J, Problem> {
    let plan = sqlx::query(
        "SELECT id, plan_id::text AS plan_id, as_of, source, built_at, expires_at, gate, \
                exposure_level::bigint AS exposure_level, total_risk_inr, total_new_exposure_inr \
         FROM sw_plan WHERE user_id = $1 ORDER BY built_at DESC, id DESC LIMIT 1",
    )
    .bind(user_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    let Some(plan) = plan else {
        return Ok(J::Null);
    };
    let pk: i64 = col(&plan, "id")?;
    let lines = sqlx::query(
        "SELECT l.id, l.kind, i.symbol, i.name, l.setup, l.quantity::bigint AS quantity, \
                l.trigger, l.stop, l.risk_inr, l.position_value, l.trail, l.note, l.state \
         FROM sw_plan_line l JOIN instrument i ON i.id = l.instrument_id \
         WHERE l.plan_id = $1 ORDER BY l.id",
    )
    .bind(pk)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let skips = sqlx::query(
        "SELECT symbol, reason, detail FROM sw_plan_skip WHERE plan_id = $1 ORDER BY symbol",
    )
    .bind(pk)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut line_js = Vec::with_capacity(lines.len());
    for row in &lines {
        line_js.push(
            Obj::new()
                .put("id", J::Int(col(row, "id")?))
                .put("kind", J::Str(col(row, "kind")?))
                .put("symbol", J::Str(col(row, "symbol")?))
                .put("name", J::Str(col(row, "name")?))
                .put("setup", J::opt_str(col(row, "setup")?))
                .put("quantity", J::Int(col(row, "quantity")?))
                .put("trigger", J::opt_dec(odec(row, "trigger")?))
                .put("stop", J::opt_dec(odec(row, "stop")?))
                .put("risk_inr", J::Dec(dec(row, "risk_inr")?))
                .put("position_value", J::Dec(dec(row, "position_value")?))
                .put("trail", J::opt_str(col(row, "trail")?))
                .put("note", J::opt_str(col(row, "note")?))
                .put("state", J::Str(col(row, "state")?))
                .done(),
        );
    }
    let mut skip_js = Vec::with_capacity(skips.len());
    for row in &skips {
        skip_js.push(
            Obj::new()
                .put("symbol", J::Str(col(row, "symbol")?))
                .put("reason", J::Str(col(row, "reason")?))
                .put("detail", J::opt_str(col(row, "detail")?))
                .done(),
        );
    }
    Ok(Obj::new()
        .put("plan_id", J::Str(col(&plan, "plan_id")?))
        .put("as_of", J::Date(col(&plan, "as_of")?))
        .put("source", J::Str(col(&plan, "source")?))
        .put("built_at", J::Dt(col(&plan, "built_at")?))
        .put("expires_at", J::Dt(col(&plan, "expires_at")?))
        .put("gate", J::Str(col(&plan, "gate")?))
        .put("exposure_level", J::Int(col(&plan, "exposure_level")?))
        .put("total_risk_inr", J::Dec(dec(&plan, "total_risk_inr")?))
        .put(
            "total_new_exposure_inr",
            J::Dec(dec(&plan, "total_new_exposure_inr")?),
        )
        .put("lines", J::Arr(line_js))
        .put("skips", J::Arr(skip_js))
        .done())
}

/// `GET /swing/positions`: the book (open first, then closed, newest first) and the newest plan.
///
/// # Errors
/// 401, 404, 503, 500.
pub async fn get_positions(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let user_id = swing_tenant(&state, &principal).await?;
    let rows = sqlx::query(
        "SELECT p.id, p.instrument_id, i.symbol, i.name, p.setup, p.entry_date, p.entry_avg, \
                p.quantity_entered::bigint AS quantity_entered, \
                p.quantity_open::bigint AS quantity_open, p.initial_stop, p.stop, p.gtt_id, \
                p.trail, p.partial_done, p.state, p.closed_on, p.exit_avg, p.close_reason, \
                p.r_multiple, p.pnl_inr, p.simulated \
         FROM sw_position p JOIN instrument i ON i.id = p.instrument_id \
         WHERE p.user_id = $1 ORDER BY p.state, p.entry_date DESC, p.id DESC",
    )
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let ids: Vec<i64> = rows
        .iter()
        .map(|r| col::<i64>(r, "instrument_id"))
        .collect::<Result<_, _>>()?;
    let closes = latest_closes(&state, &ids).await?;
    let plan = latest_plan(&state, user_id).await?;
    let mut data = Vec::with_capacity(rows.len());
    for row in &rows {
        let instrument_id: i64 = col(row, "instrument_id")?;
        let quantity_open: i64 = col(row, "quantity_open")?;
        let gtt_id: Option<String> = col(row, "gtt_id")?;
        data.push(
            Obj::new()
                .put("id", J::Int(col(row, "id")?))
                .put("instrument_id", J::Int(instrument_id))
                .put("symbol", J::Str(col(row, "symbol")?))
                .put("name", J::Str(col(row, "name")?))
                .put("setup", J::Str(col(row, "setup")?))
                .put("entry_date", J::Date(col(row, "entry_date")?))
                .put("entry_avg", J::Dec(dec(row, "entry_avg")?))
                .put("quantity_entered", J::Int(col(row, "quantity_entered")?))
                .put("quantity_open", J::Int(quantity_open))
                .put("initial_stop", J::Dec(dec(row, "initial_stop")?))
                .put("stop", J::Dec(dec(row, "stop")?))
                .put("gtt_id", J::opt_str(gtt_id.clone()))
                .put("naked", J::Bool(quantity_open > 0 && gtt_id.is_none()))
                .put("trail", J::Str(col(row, "trail")?))
                .put("partial_done", J::Bool(col(row, "partial_done")?))
                .put("state", J::Str(col(row, "state")?))
                .put("closed_on", J::opt_date(col(row, "closed_on")?))
                .put("exit_avg", J::opt_dec(odec(row, "exit_avg")?))
                .put("close_reason", J::opt_str(col(row, "close_reason")?))
                .put("r_multiple", J::opt_dec(odec(row, "r_multiple")?))
                .put("pnl_inr", J::opt_dec(odec(row, "pnl_inr")?))
                .put("simulated", J::Bool(col(row, "simulated")?))
                .put(
                    "last_close",
                    J::opt_dec(closes.get(&instrument_id).copied()),
                )
                .put("last_price", J::Null)
                .done(),
        );
    }
    overlay_last_price(&mut data).await?;
    Ok(canonical_response(
        &Obj::new()
            .put("data", J::Arr(data))
            .put("plan", plan)
            .done(),
    ))
}

// ---------------------------------------------------------------------------------------------
// GET /swing/config
// ---------------------------------------------------------------------------------------------

/// `GET /swing/config`: the settings, the server's ceilings, and the job-owned fields.
///
/// # Errors
/// 401, 404, 503 (no tenant, or no `sw_config` row), 500.
pub async fn get_config(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let user_id = swing_tenant(&state, &principal).await?;
    config_response(&state, user_id).await
}

/// The `SwingConfigView` for `user_id`, read after any write committed.
///
/// # Errors
/// 503 (not seeded), 500.
pub(crate) async fn config_response(state: &AppState, user_id: i64) -> Result<Response, Problem> {
    let row = sqlx::query(
        "SELECT sleeve_capital_inr, risk_per_trade_pct, max_position_pct, \
                max_open_positions::bigint AS max_open_positions, \
                or_window_minutes::bigint AS or_window_minutes, stop_mode, adr_min_pct, \
                turnover_min_inr, price_min, exposure_level::bigint AS exposure_level, \
                first_live_sessions_left::bigint AS first_live_sessions_left, updated_at, \
                updated_by \
         FROM sw_config WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    let Some(row) = row else {
        return Err(Problem::new(
            ProblemType::PipelineDegraded,
            "The swing book is not set up on this deployment; run `make seed`.",
        ));
    };
    let s = &state.settings;
    let body = Obj::new()
        .put(
            "sleeve_capital_inr",
            J::Dec(dec(&row, "sleeve_capital_inr")?),
        )
        .put(
            "risk_per_trade_pct",
            J::Dec(dec(&row, "risk_per_trade_pct")?),
        )
        .put("max_position_pct", J::Dec(dec(&row, "max_position_pct")?))
        .put(
            "max_open_positions",
            J::Int(col(&row, "max_open_positions")?),
        )
        .put("or_window_minutes", J::Int(col(&row, "or_window_minutes")?))
        .put("stop_mode", J::Str(col(&row, "stop_mode")?))
        .put("adr_min_pct", J::Dec(dec(&row, "adr_min_pct")?))
        .put("turnover_min_inr", J::Dec(dec(&row, "turnover_min_inr")?))
        .put("price_min", J::Dec(dec(&row, "price_min")?))
        .put("exposure_level", J::Int(col(&row, "exposure_level")?))
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
                    "risk_per_trade_pct",
                    J::Str(crate::wire::py_decimal(s.swing_risk_per_trade_pct_max)),
                )
                .put(
                    "max_position_pct",
                    J::Str(crate::wire::py_decimal(s.swing_max_position_pct_max)),
                )
                .put(
                    "max_open_positions",
                    J::Str(s.swing_max_open_positions_max.to_string()),
                )
                .done(),
        )
        .put("execution_enabled", J::Bool(s.swing_execution_enabled))
        .done();
    Ok(canonical_response(&body))
}

// ---------------------------------------------------------------------------------------------
// GET /swing/journal
// ---------------------------------------------------------------------------------------------

/// `swing_journal.bucket_of(r)`.
#[must_use]
pub fn bucket_of(r: Decimal) -> &'static str {
    if r < Decimal::from(-1) {
        HISTOGRAM_BUCKETS[0]
    } else if r < Decimal::ZERO {
        HISTOGRAM_BUCKETS[1]
    } else if r < Decimal::ONE {
        HISTOGRAM_BUCKETS[2]
    } else if r < Decimal::from(2) {
        HISTOGRAM_BUCKETS[3]
    } else if r <= Decimal::from(3) {
        HISTOGRAM_BUCKETS[4]
    } else {
        HISTOGRAM_BUCKETS[5]
    }
}

/// One closed trade: the `ClosedTrade` inputs and the stored numbers.
#[derive(Debug, Clone)]
struct Closed {
    symbol: String,
    setup: String,
    entry_date: NaiveDate,
    exit_date: NaiveDate,
    entry: Decimal,
    initial_stop: Decimal,
    exit_avg: Decimal,
    quantity: i64,
    stored_r: Decimal,
    pnl_inr: Option<Decimal>,
    close_reason: Option<String>,
}

impl Closed {
    /// `ClosedTrade.r_multiple`: the value, and how Python spells it (a loss that rounds to
    /// zero is `-0.00`).
    fn r_multiple(&self) -> Result<(Decimal, J), Problem> {
        let one_r = self.entry - self.initial_stop;
        if one_r <= Decimal::ZERO {
            return Err(crate::problems::internal_error());
        }
        let raw = div(self.exit_avg - self.entry, one_r)?;
        Ok((quantize(raw, 2), quantize_j(raw, 2)))
    }

    /// `ClosedTrade.pnl_inr`.
    fn computed_pnl(&self) -> Result<J, Problem> {
        Ok(quantize_j(
            mul(self.exit_avg - self.entry, Decimal::from(self.quantity))?,
            2,
        ))
    }
}

/// `_mean(values)`: `sum(values, Decimal(0)) / len` to 0.01. A zero sum is always `+0` in
/// Python (`0 + -0.00` is `0.00`), so only a negative mean that rounds to zero is `-0.00`.
fn mean2(values: &[Decimal]) -> Result<J, Problem> {
    if values.is_empty() {
        return Ok(J::Dec(Decimal::ZERO));
    }
    let sum: Decimal = values.iter().sum();
    Ok(quantize_j(div(sum, Decimal::from(values.len()))?, 2))
}

/// `baskfy_core.swing.journal.summarize(trades)` (oldest first), as its JSON.
fn summarize(trades: &[&Closed]) -> Result<J, Problem> {
    let spelled: Vec<(Decimal, J)> = trades
        .iter()
        .map(|t| t.r_multiple())
        .collect::<Result<_, _>>()?;
    let rs: Vec<Decimal> = spelled.iter().map(|(r, _)| *r).collect();
    let wins: Vec<Decimal> = rs.iter().copied().filter(|r| *r > Decimal::ZERO).collect();
    let losses: Vec<Decimal> = rs.iter().copied().filter(|r| *r <= Decimal::ZERO).collect();
    let gross_win: Decimal = wins.iter().sum();
    let gross_loss: Decimal = -losses.iter().sum::<Decimal>();
    let streak = rs.iter().rev().take_while(|r| **r < Decimal::ZERO).count();
    let count = rs.len();
    let win_rate = if count == 0 {
        Decimal::ZERO
    } else {
        quantize(
            mul(
                div(Decimal::from(wins.len()), Decimal::from(count))?,
                Decimal::from(100),
            )?,
            2,
        )
    };
    let profit_factor = if gross_loss > Decimal::ZERO {
        J::Dec(quantize(div(gross_win, gross_loss)?, 2))
    } else {
        J::Null
    };
    // `max`/`min` keep the first extreme, as Python's do, and its spelling (`-0.00` included).
    let largest_win = spelled
        .iter()
        .fold(None, |best: Option<&(Decimal, J)>, r| match best {
            Some(b) if r.0 <= b.0 => Some(b),
            _ => Some(r),
        })
        .map_or(J::Dec(Decimal::ZERO), |(_, j)| j.clone());
    let largest_loss = spelled
        .iter()
        .fold(None, |best: Option<&(Decimal, J)>, r| match best {
            Some(b) if r.0 >= b.0 => Some(b),
            _ => Some(r),
        })
        .map_or(J::Dec(Decimal::ZERO), |(_, j)| j.clone());
    Ok(Obj::new()
        .put("trades", J::Int(i64::try_from(count).unwrap_or(i64::MAX)))
        .put("win_rate_pct", J::Dec(win_rate))
        .put("avg_win_r", mean2(&wins)?)
        .put("avg_loss_r", mean2(&losses)?)
        .put("expectancy_r", mean2(&rs)?)
        .put("profit_factor", profit_factor)
        .put("net_r", J::Dec(quantize(rs.iter().sum(), 2)))
        .put("largest_win_r", largest_win)
        .put("largest_loss_r", largest_loss)
        .put(
            "current_loss_streak",
            J::Int(i64::try_from(streak).unwrap_or(i64::MAX)),
        )
        .done())
}

/// `swing_journal.card(trades, rows)`: rows newest first.
fn card(rows: &[Closed]) -> Result<J, Problem> {
    let oldest_first: Vec<&Closed> = rows.iter().rev().collect();
    let stats = summarize(&oldest_first)?;
    let mut counts: HashMap<&'static str, i64> = HashMap::new();
    for row in rows {
        *counts.entry(bucket_of(row.stored_r)).or_insert(0) += 1;
    }
    let histogram = HISTOGRAM_BUCKETS
        .iter()
        .map(|b| {
            Obj::new()
                .put("bucket", J::s(b))
                .put("count", J::Int(counts.get(b).copied().unwrap_or(0)))
                .done()
        })
        .collect();
    let mut by_setup: Vec<(String, Vec<Decimal>)> = Vec::new();
    let mut by_month: Vec<(String, Vec<Decimal>)> = Vec::new();
    for row in rows {
        push_group(&mut by_setup, &row.setup, row.stored_r);
        push_group(
            &mut by_month,
            &row.exit_date.format("%Y-%m").to_string(),
            row.stored_r,
        );
    }
    by_setup.sort_by(|a, b| a.0.cmp(&b.0));
    by_month.sort_by(|a, b| a.0.cmp(&b.0));
    let mut setup_js = Vec::with_capacity(by_setup.len());
    for (setup, values) in &by_setup {
        let sum: Decimal = values.iter().sum();
        setup_js.push(
            Obj::new()
                .put("setup", J::s(setup))
                .put("trades", J::Int(i64::try_from(values.len()).unwrap_or(0)))
                .put("net_r", J::Dec(quantize(sum, 2)))
                .put(
                    "expectancy_r",
                    quantize_j(div(sum, Decimal::from(values.len()))?, 2),
                )
                .done(),
        );
    }
    let month_js = by_month
        .iter()
        .map(|(month, values)| {
            Obj::new()
                .put("month", J::s(month))
                .put("trades", J::Int(i64::try_from(values.len()).unwrap_or(0)))
                .put("net_r", J::Dec(quantize(values.iter().sum(), 2)))
                .done()
        })
        .collect();
    let mut trade_js = Vec::new();
    for row in rows.iter().take(MAX_TRADES) {
        let pnl = match row.pnl_inr {
            Some(p) => J::Dec(p),
            None => row.computed_pnl()?,
        };
        trade_js.push(
            Obj::new()
                .put("symbol", J::s(&row.symbol))
                .put("setup", J::s(&row.setup))
                .put("entry_date", J::Date(row.entry_date))
                .put("exit_date", J::Date(row.exit_date))
                .put("entry", J::Dec(row.entry))
                .put("initial_stop", J::Dec(row.initial_stop))
                .put("exit_avg", J::Dec(row.exit_avg))
                .put("quantity", J::Int(row.quantity))
                .put("r_multiple", J::Dec(row.stored_r))
                .put("pnl_inr", pnl)
                .put("close_reason", J::opt_str(row.close_reason.clone()))
                .done(),
        );
    }
    Ok(Obj::new()
        .put("stats", stats)
        .put("histogram", J::Arr(histogram))
        .put("by_setup", J::Arr(setup_js))
        .put("by_month", J::Arr(month_js))
        .put("trades", J::Arr(trade_js))
        .done())
}

fn push_group(groups: &mut Vec<(String, Vec<Decimal>)>, key: &str, value: Decimal) {
    match groups.iter_mut().find(|(k, _)| k == key) {
        Some((_, values)) => values.push(value),
        None => groups.push((key.to_owned(), vec![value])),
    }
}

async fn closed_trades(
    state: &AppState,
    user_id: i64,
    simulated: bool,
) -> Result<Vec<Closed>, Problem> {
    let rows = sqlx::query(
        "SELECT i.symbol, p.setup, p.entry_date, p.closed_on, p.entry_avg, p.initial_stop, \
                p.exit_avg, p.quantity_entered::bigint AS quantity_entered, p.r_multiple, \
                p.pnl_inr, p.close_reason \
         FROM sw_position p JOIN instrument i ON i.id = p.instrument_id \
         WHERE p.user_id = $1 AND p.state = 'CLOSED' AND p.simulated = $2 \
           AND p.r_multiple IS NOT NULL AND p.exit_avg IS NOT NULL AND p.closed_on IS NOT NULL \
         ORDER BY p.closed_on DESC, p.id DESC",
    )
    .bind(user_id)
    .bind(simulated)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut out = Vec::with_capacity(rows.len());
    for row in &rows {
        out.push(Closed {
            symbol: col(row, "symbol")?,
            setup: col(row, "setup")?,
            entry_date: col(row, "entry_date")?,
            exit_date: col(row, "closed_on")?,
            entry: dec(row, "entry_avg")?,
            initial_stop: dec(row, "initial_stop")?,
            exit_avg: dec(row, "exit_avg")?,
            quantity: col(row, "quantity_entered")?,
            stored_r: dec(row, "r_multiple")?,
            pnl_inr: odec(row, "pnl_inr")?,
            close_reason: col(row, "close_reason")?,
        });
    }
    Ok(out)
}

/// `GET /swing/journal`: two cards (real, simulated), the session count, the ladder, and the
/// backtest card.
///
/// # Errors
/// 401, 404, 503 (no tenant; or a finished backtest run, whose card this server does not build
/// yet — DECISIONS-RS, lane api-sleeves), 500.
pub async fn get_journal(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let user_id = swing_tenant(&state, &principal).await?;
    let real = closed_trades(&state, user_id, false).await?;
    let paper = closed_trades(&state, user_id, true).await?;
    let real_card = card(&real)?;
    let paper_card = card(&paper)?;
    let logged: i64 = sqlx::query_scalar("SELECT count(*) FROM sw_session WHERE user_id = $1")
        .bind(user_id)
        .fetch_one(state.db.pool())
        .await
        .map_err(db)?;

    let level: Option<i64> =
        sqlx::query_scalar("SELECT exposure_level::bigint FROM sw_config WHERE user_id = $1")
            .bind(user_id)
            .fetch_optional(state.db.pool())
            .await
            .map_err(db)?;
    let rung = level.unwrap_or(0);
    let market = sqlx::query(
        "SELECT gate, max_open_positions::bigint AS max_open_positions, max_exposure_pct, \
                new_entries_allowed \
         FROM sw_market_daily WHERE user_id = $1 ORDER BY date DESC LIMIT 1",
    )
    .bind(user_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    let mut recent: Vec<J> = real
        .iter()
        .take(LOOKBACK_TRADES)
        .map(|r| J::Dec(r.stored_r))
        .collect();
    recent.reverse();
    let ladder = match &market {
        None => {
            let index = usize::try_from(rung.max(0))
                .unwrap_or(0)
                .min(TIERS.len() - 1);
            let (positions, exposure) = TIERS[index];
            Obj::new()
                .put("level", J::Int(rung))
                .put("gate", J::s("UNKNOWN"))
                .put("max_open_positions", J::Int(positions))
                .put(
                    "max_exposure_pct",
                    J::Dec(quantize(
                        crate::routes::sleeves::common::decimal_of_float(exposure),
                        2,
                    )),
                )
                .put("new_entries_allowed", J::Bool(false))
                .put("last_r", J::Arr(recent))
                .put("reads", J::s("REAL"))
                .done()
        }
        Some(m) => Obj::new()
            .put("level", J::Int(rung))
            .put("gate", J::Str(col(m, "gate")?))
            .put("max_open_positions", J::Int(col(m, "max_open_positions")?))
            .put("max_exposure_pct", J::Dec(dec(m, "max_exposure_pct")?))
            .put(
                "new_entries_allowed",
                J::Bool(col(m, "new_entries_allowed")?),
            )
            .put("last_r", J::Arr(recent))
            .put("reads", J::s("REAL"))
            .done(),
    };
    let finished: Option<i64> = sqlx::query_scalar(
        "SELECT id FROM sw_backtest_run WHERE user_id = $1 AND finished_at IS NOT NULL \
         AND error IS NULL AND stats IS NOT NULL ORDER BY finished_at DESC, id DESC LIMIT 1",
    )
    .bind(user_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    if finished.is_some() {
        return Err(Problem::new(
            ProblemType::PipelineDegraded,
            "This server does not build the swing backtest card yet; the journal is served by \
             the Python API.",
        ));
    }
    let body = Obj::new()
        .put("real", real_card)
        .put("simulated", paper_card)
        .put(
            "sessions",
            Obj::new()
                .put("logged", J::Int(logged))
                .put("required", J::Int(PAPER_SESSIONS_REQUIRED))
                .done(),
        )
        .put("ladder", ladder)
        .put("backtest", J::Null)
        .done();
    Ok(canonical_response(&body))
}

/// The GET routes of `baskfy_api.routers.swing`, relative to `/api/v1`. The writes stay on the
/// Python API until they are ported.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/swing/scan", post(writes::post_scan))
        .route(
            "/swing/watch/{watch_id}",
            patch(writes::patch_watch).delete(writes::delete_watch),
        )
        .route("/swing/setups", get(get_setups))
        .route("/swing/scan/{run_id}", get(get_scan))
        .route("/swing/setups/{instrument_id}/bars", get(get_bars))
        .route("/swing/market", get(get_market))
        .route("/swing/signals", get(get_signals))
        .route("/swing/sectors", get(get_sectors))
        .route("/swing/watch", get(get_watch).post(writes::post_watch))
        .route("/swing/positions", get(get_positions))
        .route("/swing/config", get(get_config).patch(writes::patch_config))
        .route("/swing/journal", get(get_journal))
}

#[cfg(test)]
mod tests {
    //! The pure halves of `test_api_swing.py` / `test_api_swing_journal.py`.
    #![allow(clippy::unwrap_used)]
    use super::*;
    use std::str::FromStr;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).unwrap()
    }

    #[test]
    fn buckets_are_the_journals_six() {
        assert_eq!(bucket_of(d("-1.01")), "<-1");
        assert_eq!(bucket_of(d("-1")), "-1..0");
        assert_eq!(bucket_of(d("0")), "0..1");
        assert_eq!(bucket_of(d("1.99")), "1..2");
        assert_eq!(bucket_of(d("3")), "2..3");
        assert_eq!(bucket_of(d("3.01")), ">3");
    }

    #[test]
    fn distance_is_a_percentage_of_the_trigger_and_may_be_negative() {
        assert_eq!(
            pct_below(Some(d("172.44")), Some(d("163.1100"))).unwrap(),
            Some(J::Dec(d("5.41")))
        );
        assert_eq!(
            pct_below(Some(d("100")), Some(d("110"))).unwrap(),
            Some(J::Dec(d("-10.00")))
        );
        assert_eq!(
            pct_below(Some(d("100000")), Some(d("100000.001"))).unwrap(),
            Some(J::NegZero(2))
        );
        assert_eq!(pct_below(Some(d("0")), Some(d("1"))).unwrap(), None);
        assert_eq!(pct_below(None, Some(d("1"))).unwrap(), None);
    }

    #[test]
    fn an_empty_book_is_a_card_of_zeros() {
        let j = card(&[]).unwrap().canonical();
        assert!(j.starts_with(
            r#"{"stats":{"trades":0,"win_rate_pct":0,"avg_win_r":0,"avg_loss_r":0,"expectancy_r":0,"profit_factor":null,"net_r":0.00,"largest_win_r":0,"largest_loss_r":0,"current_loss_streak":0}"#
        ), "{j}");
    }

    fn trade(exit: &str, exit_date: (u32, u32)) -> Closed {
        Closed {
            symbol: "X".into(),
            setup: "FLAG".into(),
            entry_date: NaiveDate::from_ymd_opt(2026, 9, 1).unwrap(),
            exit_date: NaiveDate::from_ymd_opt(2026, exit_date.0, exit_date.1).unwrap(),
            entry: d("100"),
            initial_stop: d("95"),
            exit_avg: d(exit),
            quantity: 10,
            stored_r: quantize(div(d(exit) - d("100"), d("5")).unwrap(), 2),
            pnl_inr: None,
            close_reason: None,
        }
    }

    #[test]
    fn summarize_reads_r_and_the_losing_streak() {
        // newest first: a loss, a loss, a win
        let rows = vec![
            trade("97", (10, 3)),
            trade("96", (10, 2)),
            trade("110", (9, 20)),
        ];
        let j = card(&rows).unwrap().canonical();
        assert!(j.contains(r#""trades":3,"win_rate_pct":33.33"#), "{j}");
        assert!(j.contains(r#""profit_factor":1.43"#), "{j}");
        assert!(j.contains(r#""current_loss_streak":2"#), "{j}");
        assert!(j.contains(r#""by_month":[{"month":"2026-09","trades":1,"net_r":2.00},{"month":"2026-10","trades":2,"net_r":-1.40}]"#), "{j}");
    }

    #[test]
    fn a_loss_that_rounds_to_zero_r_is_minus_zero_as_python_spells_it() {
        // newest first; the oldest (first in `rs`) is the -0.00, so max and min both pick it,
        // while a sum of zeros is +0 in Python (checked against `baskfy_core.swing.journal`).
        let rows = vec![trade("100", (10, 3)), trade("99.99", (10, 2))];
        let j = card(&rows).unwrap().canonical();
        assert!(
            j.contains(r#""avg_loss_r":0.00,"expectancy_r":0.00,"profit_factor":null,"net_r":0.00,"largest_win_r":-0.00,"largest_loss_r":-0.00"#),
            "{j}"
        );
        // a negative mean that rounds to zero keeps its sign: -0.01 / 3
        let rows = vec![
            trade("100", (10, 3)),
            trade("100", (10, 2)),
            trade("99.95", (10, 1)),
        ];
        let j = card(&rows).unwrap().canonical();
        assert!(
            j.contains(r#""avg_loss_r":-0.00,"expectancy_r":-0.00"#),
            "{j}"
        );
        assert!(j.contains(r#""pnl_inr":-0.50"#), "{j}");
        // a computed P&L that rounds to zero keeps its sign too: (99.9999 - 100) * 10
        let rows = vec![trade("99.9999", (10, 3))];
        let j = card(&rows).unwrap().canonical();
        assert!(j.contains(r#""pnl_inr":-0.00"#), "{j}");
    }

    #[test]
    fn trailing_mean_waits_for_a_full_window() {
        let closes: Vec<Decimal> = (1..=10).map(Decimal::from).collect();
        assert_eq!(trailing_mean(&closes, 8, 10).unwrap(), None);
        assert_eq!(trailing_mean(&closes, 9, 10).unwrap(), Some(d("5.50")));
    }

    #[test]
    fn a_refused_filter_names_the_vocabulary() {
        let p = one_of(Some("NOPE"), &SETUPS, "setup").unwrap_err();
        let body = p.body(Some("/x")).to_python_json().unwrap();
        assert!(
            body.contains(r#""detail":"'NOPE' is not a setup.""#),
            "{body}"
        );
        assert!(
            body.contains(r#""message":"choose one of ['EP', 'FLAG', 'PARABOLIC_SHORT']""#),
            "{body}"
        );
    }
}
