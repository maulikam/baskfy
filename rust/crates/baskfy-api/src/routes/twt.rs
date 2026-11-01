//! ``/twt/*`` — the three-weeks-tight sleeve's read surface (TW8, TW12).
//!
//! Ports the GET routes of `baskfy_api.routers.twt` and the reads behind them
//! (`baskfy_api.twt.today`/`backtest`, `twt_scan.scan_run`/`newest_run`, the marks of
//! `twt_sleeve.load_sleeve`). Python declares `APIRouter(prefix="/twt")`.
//!
//! Ported: `GET /today`, `GET /backtest`, `GET /scan/{run_id}`. `POST /scan` is a write and stays
//! on the Python API until writes are ported (not registered here).
//!
//! `/today` and `/backtest` answer with **pydantic's** JSON (a `Decimal` is a quoted string — the
//! page does BigInt arithmetic over decimal strings); `/scan/{run_id}` with `canonical_json`. Open
//! positions are marked at the last published close on or before the session; the live overlay
//! is always absent here, which is Python's answer without a Kite session (DECISIONS-RS, lane api-sleeves).
//! **No route may place, modify or cancel an order** (law 2).

use std::collections::HashMap;

pub(crate) mod writes;

use axum::Router;
use axum::extract::{Extension, Path, RawQuery, State};
use axum::response::Response;
use axum::routing::{get, post};
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::postgres::PgRow;

use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, not_found};
use crate::query::{QueryParams, Validation};
use crate::routes::sleeves::common::{
    J, LaxInt, Obj, as_dict, canonical_response, db, dec, decimal_of_float, div, get as col,
    id_or_500, int_param, json_int, json_strict_int, mul, odec, ojson, pydantic_response,
    quantize_j, scoped_sole_user_id,
};
use crate::state::AppState;

/// `FIRST_LIVE_ENTRIES` (`DEFAULT_TWT_CONFIG.sizing.first_live_entries`).
pub const FIRST_LIVE_ENTRIES: i64 = 10;

/// `THRESHOLD_PCT`: `Decimal(str(DEFAULT_TWT_CONFIG.breadth.min_pct_above_dma))`.
fn threshold_pct() -> Decimal {
    decimal_of_float(baskfy_core::twt::config::BreadthConfig::default().min_pct_above_dma)
}

/// `BACKTEST_SOURCES`, in the order Python reads them.
const BACKTEST_SOURCES: [&str; 2] = ["PLANT", "RESEARCH_EXPORT"];

const CAVEATS: [(&str, &str); 4] = [
    (
        "trades",
        "**164 trades.** Ten of them are 53 % of gross profit; the best single trade is 11 % of it. Three years carry the CAGR on 12–15 trades each. Sharpe 1.2 on that trade count is a result a different draw of the same market could easily make 0.6, and a one-year average hold means the 2017–2026 window contains perhaps **fifteen independent observations of the book**. Read 20.9 % as \"an edge with the right sign and a wide confidence interval\". The 50-SMA variant's 543 trades and 15.6 % are the more believable statement of the same thing.",
    ),
    (
        "giveback",
        "**The trail is the strategy, and it is slow.** Average hold 105 sessions, longest 601 (BOSCHLTD, Aug 2022 → Jan 2025, +78 %). The GTT has to be re-set every session it ratchets, which is a process, not a signal — and a 20 % give-back on a ₹2.5 lakh line is a ₹50,000 open loss the book will sit through **as a matter of routine**. Anyone watching the page needs to have agreed to that in advance; `05` §2 puts the distance-to-trigger on every open line for exactly this reason.",
    ),
    (
        "history",
        "**Regime and history.** The out-of-sample 36 % is 2023–24; the in-sample 11 % is the guide. Modelled fills, 25 bps, no interest on idle cash, sparse corporate actions before 2024, one history, research code. And §2's reproduction question: the scan traded here is the one visible at the close, not the one in Chartink's backtest export.",
    ),
    (
        "capital",
        "**One thing no backtest in this repository has measured:** the book at ₹25 lakh. Every number above was produced at ₹10 lakh, where the 1 %-of-turnover cap binds on nothing. TW9's run from the plant's own bars is sized against its own parameter, not against the sleeve's capital, so the two can never be confused — but the first live line is the first line the cap has ever bound.",
    ),
];
const NO_RUN_AT_ALL: &str = "No backtest has been run for this strategy yet. The rule has never been replayed over this product's own price history, so there is no result to show — which is not the same as a result of zero.";
const RUN_IN_FLIGHT: &str = "A backtest is running and has not produced a result yet. Only a settled run is shown here, and there is no earlier settled run to fall back to.";
const RUN_FAILED: &str = "The most recent backtest did not finish successfully, so nothing from it is shown. A failed run must never displace a settled number, and there is no earlier settled number to show.";

// ---------------------------------------------------------------------------------------------
// GET /twt/today
// ---------------------------------------------------------------------------------------------

const BREADTH_COLUMNS: &str = "date, gate, pct_above_dma, universe_count::bigint AS universe_count, \
    measured_count::bigint AS measured_count, above_count::bigint AS above_count, thin_session, \
    detail";

fn gate_j(reading: Option<&PgRow>, asked_for: Option<NaiveDate>) -> Result<J, Problem> {
    let Some(r) = reading else {
        return Ok(Obj::new()
            .put("date", J::opt_date(asked_for))
            .put("gate", J::Null)
            .put("pct_above_dma", J::Null)
            .put("threshold_pct", J::Dec(threshold_pct()))
            .put("universe_count", J::Null)
            .put("with_bar_count", J::Null)
            .put("measured_count", J::Null)
            .put("above_count", J::Null)
            .put("thin_session", J::Bool(false))
            .done());
    };
    // `_funnel_count(reading, "instruments")`: an `int` (Python's `bool` included), else null.
    let detail = ojson(r, "detail")?;
    let instruments = detail.as_ref().and_then(|d| json_int(d.get("instruments")));
    Ok(Obj::new()
        .put("date", J::Date(col(r, "date")?))
        .put("gate", J::Str(col(r, "gate")?))
        .put("pct_above_dma", J::opt_dec(odec(r, "pct_above_dma")?))
        .put("threshold_pct", J::Dec(threshold_pct()))
        .put("universe_count", J::opt_int(instruments))
        .put("with_bar_count", J::Int(col(r, "universe_count")?))
        .put("measured_count", J::Int(col(r, "measured_count")?))
        .put("above_count", J::Int(col(r, "above_count")?))
        .put("thin_session", J::Bool(col(r, "thin_session")?))
        .done())
}

async fn tight(state: &AppState, user_id: i64, day: NaiveDate) -> Result<Vec<J>, Problem> {
    let rows = sqlx::query(
        "SELECT s.instrument_id, i.symbol, i.name, s.close_raw, s.week_close_0, s.week_close_1, \
                s.week_close_2, s.week_range_pct, s.month_low_ratio, \
                s.sessions_in_state::bigint AS sessions_in_state, s.turnover_avg_20, \
                s.locked_upper_circuit, g.state AS signal_state, \
                g.failed_filters::text[] AS failed_filters \
         FROM tw_state_daily s JOIN instrument i ON i.id = s.instrument_id \
         LEFT OUTER JOIN tw_signal_daily g ON g.user_id = s.user_id AND g.date = s.date \
              AND g.instrument_id = s.instrument_id \
         WHERE s.user_id = $1 AND s.date = $2 ORDER BY s.instrument_id",
    )
    .bind(user_id)
    .bind(day)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut out = Vec::with_capacity(rows.len());
    for row in &rows {
        let filters: Option<Vec<String>> = col(row, "failed_filters")?;
        out.push(
            Obj::new()
                .put("instrument_id", J::Int(col(row, "instrument_id")?))
                .put("symbol", J::Str(col(row, "symbol")?))
                .put("name", J::Str(col(row, "name")?))
                .put("close_raw", J::Dec(dec(row, "close_raw")?))
                .put("week_close_0", J::Dec(dec(row, "week_close_0")?))
                .put("week_close_1", J::Dec(dec(row, "week_close_1")?))
                .put("week_close_2", J::Dec(dec(row, "week_close_2")?))
                .put("week_range_pct", J::Dec(dec(row, "week_range_pct")?))
                .put("month_low_ratio", J::Dec(dec(row, "month_low_ratio")?))
                .put("sessions_in_state", J::Int(col(row, "sessions_in_state")?))
                .put("signal_state", J::opt_str(col(row, "signal_state")?))
                .put(
                    "failed_filters",
                    J::Arr(
                        filters
                            .unwrap_or_default()
                            .into_iter()
                            .map(J::Str)
                            .collect(),
                    ),
                )
                .put("turnover_avg_20", J::opt_int(col(row, "turnover_avg_20")?))
                .put(
                    "locked_upper_circuit",
                    J::Bool(col(row, "locked_upper_circuit")?),
                )
                .put("last_price", J::Null)
                .done(),
        );
    }
    Ok(out)
}

/// `twt_sleeve.marks`: the newest published close on or before `as_of`, per instrument.
async fn marks(
    state: &AppState,
    ids: &[i64],
    as_of: NaiveDate,
) -> Result<HashMap<i64, Decimal>, Problem> {
    if ids.is_empty() {
        return Ok(HashMap::new());
    }
    let rows = sqlx::query(
        "SELECT o.instrument_id, o.close FROM ohlcv_daily o JOIN ( \
             SELECT instrument_id, max(date) AS date FROM ohlcv_daily \
             WHERE instrument_id = ANY($1) AND date <= $2 GROUP BY instrument_id) n \
         ON o.instrument_id = n.instrument_id AND o.date = n.date",
    )
    .bind(ids)
    .bind(as_of)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut out = HashMap::new();
    for row in &rows {
        out.insert(col::<i64>(row, "instrument_id")?, dec(row, "close")?);
    }
    Ok(out)
}

/// `(mark - entry) * qty` and its fraction of cost (4 dp; `None` at zero cost).
fn unrealised(mark: Decimal, entry: Decimal, quantity: i64) -> Result<(J, J), Problem> {
    let qty = Decimal::from(quantity);
    let gain = mul(mark - entry, qty)?;
    let cost = mul(entry, qty)?;
    // Python's `(mark - entry) * 0` with a falling mark is `-0.0000`; `rust_decimal` has no
    // negative zero, so the sign is carried explicitly.
    let unrealised = if gain.is_zero() && mark < entry {
        J::NegZero(gain.scale())
    } else {
        J::Dec(gain)
    };
    let fraction = if cost.is_zero() {
        J::Null
    } else {
        quantize_j(div(gain, cost)?, 4)
    };
    Ok((unrealised, fraction))
}

async fn positions(state: &AppState, user_id: i64, day: NaiveDate) -> Result<Vec<J>, Problem> {
    let rows = sqlx::query(
        "SELECT p.id, p.instrument_id, i.symbol, i.name, p.entry_date, p.entry_avg, \
                p.quantity_open::bigint AS quantity_open, p.high_since, p.high_since_date, \
                p.gtt_trigger, p.gtt_id, p.next_trigger, p.next_trigger_for, \
                p.hold_sessions::bigint AS hold_sessions, p.half_size, p.simulated \
         FROM tw_position p LEFT JOIN instrument i ON i.id = p.instrument_id \
         WHERE p.user_id = $1 AND p.state = 'OPEN' AND p.quantity_open > 0 \
         ORDER BY p.instrument_id, p.id",
    )
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    if rows.is_empty() {
        return Ok(Vec::new());
    }
    let ids: Vec<i64> = rows
        .iter()
        .map(|r| col::<i64>(r, "instrument_id"))
        .collect::<Result<_, _>>()?;
    let marked = marks(state, &ids, day).await?;
    let mut out = Vec::with_capacity(rows.len());
    for row in &rows {
        let instrument_id: i64 = col(row, "instrument_id")?;
        let entry = dec(row, "entry_avg")?;
        let quantity: i64 = col(row, "quantity_open")?;
        let (mut last, mut unrealised, mut fraction) = (None, J::Null, J::Null);
        if let Some(mark) = marked.get(&instrument_id).copied() {
            last = Some(mark);
            (unrealised, fraction) = self::unrealised(mark, entry, quantity)?;
        }
        out.push(
            Obj::new()
                .put("id", J::Int(col(row, "id")?))
                .put("instrument_id", J::Int(instrument_id))
                .put(
                    "symbol",
                    J::Str(col::<Option<String>>(row, "symbol")?.unwrap_or_default()),
                )
                .put(
                    "name",
                    J::Str(col::<Option<String>>(row, "name")?.unwrap_or_default()),
                )
                .put("entry_date", J::Date(col(row, "entry_date")?))
                .put("entry_avg", J::Dec(entry))
                .put("quantity_open", J::Int(quantity))
                .put("high_since", J::Dec(dec(row, "high_since")?))
                .put("high_since_date", J::opt_date(col(row, "high_since_date")?))
                .put("gtt_trigger", J::opt_dec(odec(row, "gtt_trigger")?))
                .put("gtt_id", J::opt_str(col(row, "gtt_id")?))
                .put("next_trigger", J::opt_dec(odec(row, "next_trigger")?))
                .put(
                    "next_trigger_for",
                    J::opt_date(col(row, "next_trigger_for")?),
                )
                .put("last_price", J::opt_dec(last))
                .put("unrealised_inr", unrealised)
                .put("unrealised_fraction", fraction)
                .put("hold_sessions", J::opt_int(col(row, "hold_sessions")?))
                .put("half_size", J::Bool(col(row, "half_size")?))
                .put("simulated", J::Bool(col(row, "simulated")?))
                .done(),
        );
    }
    Ok(out)
}

const SCAN_COLUMNS: &str =
    "id, status, requested_at, started_at, finished_at, session_date, detail, error, provisional";

/// `twt_scan.found_in(detail)`: `detail["signals"]` when it is an `int` and not a `bool`.
fn found(detail: Option<&OrderedJson>) -> Option<i64> {
    json_strict_int(detail?.get("signals"))
}

/// `GET /twt/today?date=`: the gate's reading, the names in the state, the open book, the
/// first-live counter and the newest scan.
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
    let reading =
        match day {
            Some(d) => sqlx::query(&format!(
                "SELECT {BREADTH_COLUMNS} FROM tw_breadth_daily WHERE user_id = $1 AND date = $2"
            ))
            .bind(user_id)
            .bind(d)
            .fetch_optional(state.db.pool())
            .await,
            None => {
                sqlx::query(&format!(
                    "SELECT {BREADTH_COLUMNS} FROM tw_breadth_daily WHERE user_id = $1 \
             ORDER BY date DESC LIMIT 1"
                ))
                .bind(user_id)
                .fetch_optional(state.db.pool())
                .await
            }
        }
        .map_err(db)?;
    let as_of = match &reading {
        Some(r) => Some(col::<NaiveDate>(r, "date")?),
        None => day,
    };
    let left: Option<i64> = sqlx::query_scalar(
        "SELECT first_live_entries_left::bigint FROM tw_config WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    let half_size = Obj::new()
        .put("entries_left", J::Int(left.unwrap_or(FIRST_LIVE_ENTRIES)))
        .put("entries_total", J::Int(FIRST_LIVE_ENTRIES))
        .put(
            "execution_enabled",
            J::Bool(state.settings.twt_execution_enabled),
        )
        .done();
    let scan = sqlx::query(&format!(
        "SELECT {SCAN_COLUMNS} FROM tw_scan_run WHERE user_id = $1 \
         ORDER BY requested_at DESC, id DESC LIMIT 1"
    ))
    .bind(user_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    let last_scan = match &scan {
        None => J::Null,
        Some(row) => {
            let detail = as_dict(ojson(row, "detail")?);
            Obj::new()
                .put("id", J::Int(col(row, "id")?))
                .put("status", J::Str(col(row, "status")?))
                .put("requested_at", J::Dt(col(row, "requested_at")?))
                .put("finished_at", J::opt_dt(col(row, "finished_at")?))
                .put("error", J::opt_str(col(row, "error")?))
                .put("session_date", J::opt_date(col(row, "session_date")?))
                .put("found", J::opt_int(found(detail.as_ref())))
                .done()
        }
    };
    let (gate, mut tight_rows, mut open) = match as_of {
        None => (gate_j(None, None)?, Vec::new(), Vec::new()),
        Some(d) => (
            gate_j(reading.as_ref(), Some(d))?,
            tight(&state, user_id, d).await?,
            positions(&state, user_id, d).await?,
        ),
    };
    let marks = crate::routes::sleeves::common::live_marks(
        &tight_rows.iter().chain(open.iter()).collect::<Vec<_>>(),
    )
    .await?;
    for row in &mut tight_rows {
        let mark = marks
            .get(&crate::routes::sleeves::common::symbol_key(row))
            .copied();
        crate::routes::sleeves::common::set_field(row, "last_price", J::opt_dec(mark));
    }
    for row in &mut open {
        if let Some(live) = marks
            .get(&crate::routes::sleeves::common::symbol_key(row))
            .copied()
        {
            let entry =
                crate::routes::sleeves::common::field_dec(row, "entry_avg").unwrap_or_default();
            let quantity = match crate::routes::sleeves::common::field(row, "quantity_open") {
                Some(J::Int(q)) => *q,
                _ => 0,
            };
            let (unrealised, fraction) = unrealised(live, entry, quantity)?;
            crate::routes::sleeves::common::set_field(row, "last_price", J::Dec(live));
            crate::routes::sleeves::common::set_field(row, "unrealised_inr", unrealised);
            crate::routes::sleeves::common::set_field(row, "unrealised_fraction", fraction);
        }
    }
    let body = Obj::new()
        .put("as_of", J::opt_date(as_of))
        .put("gate", gate)
        .put("tight", J::Arr(tight_rows))
        .put("positions", J::Arr(open))
        .put("half_size", half_size)
        .put("last_scan", last_scan)
        .done();
    Ok(pydantic_response(&body))
}

// ---------------------------------------------------------------------------------------------
// GET /twt/backtest
// ---------------------------------------------------------------------------------------------

/// `GET /twt/backtest`: the newest settled run per source, `01` §8's caveats, and which absence
/// it is when there is none.
///
/// # Errors
/// 401, 404, 503, 500.
pub async fn get_backtest(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let mut runs = Vec::new();
    for source in BACKTEST_SOURCES {
        let row = sqlx::query(
            "SELECT id, source, started_at, finished_at, params, stats, drift, error \
             FROM tw_backtest_run WHERE user_id = $1 AND source = $2 \
             AND finished_at IS NOT NULL AND stats IS NOT NULL \
             ORDER BY finished_at DESC LIMIT 1",
        )
        .bind(user_id)
        .bind(source)
        .fetch_optional(state.db.pool())
        .await
        .map_err(db)?;
        if let Some(row) = row {
            runs.push(backtest_run_j(&row)?);
        }
    }
    let reason = if runs.is_empty() {
        let (total, finished): (i64, i64) = sqlx::query_as(
            "SELECT count(*), count(finished_at) FROM tw_backtest_run WHERE user_id = $1",
        )
        .bind(user_id)
        .fetch_one(state.db.pool())
        .await
        .map_err(db)?;
        J::s(if total == 0 {
            NO_RUN_AT_ALL
        } else if finished < total {
            RUN_IN_FLIGHT
        } else {
            RUN_FAILED
        })
    } else {
        J::Null
    };
    let caveats = CAVEATS
        .iter()
        .map(|(id, text)| {
            Obj::new()
                .put("id", J::s(id))
                .put("text", J::s(text))
                .done()
        })
        .collect();
    Ok(pydantic_response(
        &Obj::new()
            .put("runs", J::Arr(runs))
            .put("caveats", J::Arr(caveats))
            .put("reason", reason)
            .done(),
    ))
}

/// One settled `tw_backtest_run` / `vb_backtest_run` row: the stored JSON passed through.
pub(crate) fn backtest_run_j(row: &PgRow) -> Result<J, Problem> {
    Ok(Obj::new()
        .put("id", J::Int(col(row, "id")?))
        .put("source", J::Str(col(row, "source")?))
        .put("started_at", J::Dt(col(row, "started_at")?))
        .put("finished_at", J::opt_dt(col(row, "finished_at")?))
        .put(
            "params",
            J::Raw(
                baskfy_db::row::jsonb(row, "params")
                    .map_err(crate::routes::sleeves::common::dbe)?,
            ),
        )
        .put("stats", ojson(row, "stats")?.map_or(J::Null, J::Raw))
        .put("drift", ojson(row, "drift")?.map_or(J::Null, J::Raw))
        .put("error", J::opt_str(col(row, "error")?))
        .done())
}

// ---------------------------------------------------------------------------------------------
// GET /twt/scan/{run_id}
// ---------------------------------------------------------------------------------------------

/// `GET /twt/scan/{run_id}`: that run, if it is this tenant's.
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
        "SELECT {SCAN_COLUMNS} FROM tw_scan_run WHERE user_id = $1 AND id = $2"
    ))
    .bind(user_id)
    .bind(id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    let Some(row) = row else {
        return Err(not_found("scan", &id.to_string()));
    };
    let detail = as_dict(ojson(&row, "detail")?);
    let body = Obj::new()
        .put("run_id", J::Int(col(&row, "id")?))
        .put("status", J::Str(col(&row, "status")?))
        .put("requested_at", J::Dt(col(&row, "requested_at")?))
        .put("started_at", J::opt_dt(col(&row, "started_at")?))
        .put("finished_at", J::opt_dt(col(&row, "finished_at")?))
        .put("session_date", J::opt_date(col(&row, "session_date")?))
        .put("detail", detail.clone().map_or(J::Null, J::Raw))
        .put("error", J::opt_str(col(&row, "error")?))
        .put("found", J::opt_int(found(detail.as_ref())))
        .put("provisional", J::Bool(col(&row, "provisional")?))
        .done();
    Ok(canonical_response(&body))
}

/// The routes of `baskfy_api.routers.twt`, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/twt/scan", post(writes::post_twt_scan))
        .route("/twt/today", get(get_today))
        .route("/twt/backtest", get(get_backtest))
        .route("/twt/scan/{run_id}", get(get_scan))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn the_threshold_prints_as_python_does() {
        assert_eq!(threshold_pct().to_string(), "40.0");
    }

    #[test]
    fn found_is_an_int_and_never_a_bool() {
        let d = OrderedJson::parse(r#"{"signals":3}"#).unwrap();
        assert_eq!(found(Some(&d)), Some(3));
        let d = OrderedJson::parse(r#"{"signals":true}"#).unwrap();
        assert_eq!(found(Some(&d)), None);
        assert_eq!(found(None), None);
    }

    #[test]
    fn the_caveats_are_section_eights_four() {
        let ids: Vec<&str> = CAVEATS.iter().map(|(id, _)| *id).collect();
        assert_eq!(ids, ["trades", "giveback", "history", "capital"]);
    }
}
