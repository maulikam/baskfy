//! SW15 — "Scan now": the swing detection on demand, intraday from Kite quotes
//! (`tasks/swing_scan_now.py`, and the API's `swing_scan.request_scan` the login path calls).
//!
//! **The one decision** ([`decide_session`]): on a trading day from 09:15 IST onwards — at any
//! hour after the open, not only until the close — and before tonight's publish, the session is
//! **today**, and today's bar is built per liquid name from the live quote
//! ([`provisional_bars`]); before the open it is the last *published* session and the run is
//! the nightly's body, plain (a quote at 08:00 carries yesterday's close). Once today is
//! published nothing provisional is ever written over it. That is CLAUDE.md's two clocks: a
//! scan's rows say `provisional`, the published series never claims today.
//!
//! **Fail soft.** The run row is marked `RUNNING`; the detection writes run in a savepoint; a
//! failure anywhere in them rolls the savepoint back and the row takes `FAILED` and the reason.
//!
//! **Law 2.** A scan reads quotes and writes detection rows. Nothing here reaches an order.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::py::float_repr;
use baskfy_core::swing::setups::{SwingConfig, with_swing_indicators};
use baskfy_db::OrderedJson;
use baskfy_providers::factory::KiteLane;
use baskfy_providers::records::QuoteRecord;
use chrono::{DateTime, NaiveDate, NaiveDateTime, NaiveTime, Utc};
use polars::prelude::{DataFrame, IntoColumn, NamedFrom, Series};
use rust_decimal::Decimal;
use sqlx::{Acquire as _, PgConnection};

use super::TaskContext;
use super::published_session::last_published_session;
use super::quotes::kite_provider;
use crate::alerts::py_isoformat;
use crate::error::WorkerError;
use crate::pyjson::{self, int, object, s};
use crate::steps::StepOutcome;
use crate::steps::sleeves::for_swing_live::{
    BAR_COLUMNS, ScanExtras, config_row, date_column, dates, day_number, floats, ints, is_liquid,
    load_swing_bars, lookback_start, pl_err, py_round, run_detect_swing_scan, strings,
    swing_config,
};

/// `NSE_EXCHANGE_ID`.
const NSE_EXCHANGE_ID: i16 = 1;
/// `swing.LOOKBACK_SESSIONS`.
const LOOKBACK_SESSIONS: i64 = 200;

/// `MARKET_OPEN` / `MARKET_CLOSE`: NSE's cash session.
pub(super) fn market_open() -> NaiveTime {
    NaiveTime::from_hms_opt(9, 15, 0).unwrap_or(NaiveTime::MIN)
}
fn market_close() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 30, 0).unwrap_or(NaiveTime::MIN)
}

/// The run's four states.
const QUEUED: &str = "QUEUED";
const RUNNING: &str = "RUNNING";
const DONE: &str = "DONE";
const FAILED: &str = "FAILED";

/// How the decision explains itself on the run row.
const REASON_MARKET_OPEN: &str = "market open: today so far, from live quotes";
const REASON_PUBLISHED: &str = "before the open: the last published session, from published bars";
const REASON_AFTER_CLOSE: &str = "after the close: today so far, from the day's final quotes";
const REASON_ALREADY_PUBLISHED: &str =
    "today is already published: re-detected from published bars";

/// `ScanNotRunnable(message)`.
pub(super) fn not_runnable(message: impl Into<String>) -> WorkerError {
    WorkerError::python("ScanNotRunnable", message)
}

/// Is this error Python's `ScanNotRunnable`?
pub(super) fn is_not_runnable(error: &WorkerError) -> bool {
    error.python_type() == "ScanNotRunnable"
}

/// `ScanDecision`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) struct ScanDecision {
    /// The session the scan is of.
    pub session_date: NaiveDate,
    /// Whether its bar has to be built from quotes.
    pub provisional: bool,
    /// The last published session.
    pub published_as_of: NaiveDate,
    /// The reason written on the row.
    pub reason: &'static str,
}

/// `swing_scan_now.is_trading_day`: `bool(flag)` — a day the calendar does not carry is not.
async fn is_trading_day(conn: &mut PgConnection, on: NaiveDate) -> Result<bool, WorkerError> {
    let row: Option<(bool,)> = sqlx::query_as(
        "SELECT trading_day.is_trading_day FROM trading_day \
         WHERE trading_day.exchange_id = $1 AND trading_day.date = $2",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(on)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.is_some_and(|(flag,)| flag))
}

/// `market_is_open`: between the open and the close, inclusive.
pub(super) fn market_is_open(now: NaiveDateTime) -> bool {
    market_open() <= now.time() && now.time() <= market_close()
}

/// `session_has_started`: from 09:15 onwards, at any hour after it (8 Sep 2026).
pub(super) fn session_has_started(now: NaiveDateTime) -> bool {
    now.time() >= market_open()
}

/// `decide_session(session, now)` — `now` is IST, naive.
///
/// # Errors
/// `ScanNotRunnable` when nothing is published.
pub(super) async fn decide_session(
    conn: &mut PgConnection,
    now: NaiveDateTime,
) -> Result<ScanDecision, WorkerError> {
    let Some(published) = last_published_session(conn, None).await? else {
        return Err(not_runnable(
            "no published bars: the pipeline has not run yet",
        ));
    };
    let today = now.date();
    if published >= today {
        return Ok(ScanDecision {
            session_date: published,
            provisional: false,
            published_as_of: published,
            reason: REASON_ALREADY_PUBLISHED,
        });
    }
    if session_has_started(now) && is_trading_day(conn, today).await? {
        let reason = if market_is_open(now) {
            REASON_MARKET_OPEN
        } else {
            REASON_AFTER_CLOSE
        };
        return Ok(ScanDecision {
            session_date: today,
            provisional: true,
            published_as_of: published,
            reason,
        });
    }
    Ok(ScanDecision {
        session_date: published,
        provisional: false,
        published_as_of: published,
        reason: REASON_PUBLISHED,
    })
}

/// `Decimal(str(x))` for a float (its `repr`).
pub(super) fn decimal_of_float(x: f64) -> Result<Decimal, WorkerError> {
    let text = float_repr(x);
    let parsed = if text.contains('e') {
        Decimal::from_scientific(&text)
    } else {
        text.parse::<Decimal>()
    };
    parsed.map_err(|e| WorkerError::python("InvalidOperation", format!("{text}: {e}")))
}

/// `float(Decimal)`, correctly rounded (through the decimal string, as CPython does).
pub(super) fn float_of(d: Decimal) -> f64 {
    d.to_string().parse::<f64>().unwrap_or(f64::NAN)
}

/// `LiquidName`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct LiquidName {
    pub instrument_id: i64,
    pub symbol: String,
    pub avg_daily_volume: Decimal,
    pub prev_close: Decimal,
    pub adr_pct: Decimal,
    pub turnover_avg: Option<Decimal>,
}

/// `load_swing_config(session, user_id)`.
pub(crate) async fn load_swing_config(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<SwingConfig, WorkerError> {
    let row = config_row(conn, user_id).await?;
    Ok(swing_config(row.as_ref()))
}

/// `swing_premarket.liquid_universe`: the names `04` §1 admits as of the last close, sorted by
/// symbol — the same predicate the detectors apply over the same bars.
pub(crate) async fn liquid_universe(
    conn: &mut PgConnection,
    as_of: NaiveDate,
    config: &SwingConfig,
) -> Result<Vec<LiquidName>, WorkerError> {
    let start = lookback_start(conn, as_of, LOOKBACK_SESSIONS).await?;
    let bars = load_swing_bars(conn, start, as_of).await?;
    if bars.height() == 0 {
        return Ok(Vec::new());
    }
    let indicated = with_swing_indicators(&bars, config)
        .map_err(|e| WorkerError::python("ComputeError", e.to_string()))?;
    let ids = ints(&indicated, "instrument_id")?;
    let symbols = strings(&indicated, "symbol")?;
    let vol = floats(&indicated, "vol_avg_rvol")?;
    let close = floats(&indicated, "close")?;
    let factor = floats(&indicated, "adj_factor")?;
    let day = dates(&indicated, "date")?;
    let adr = floats(&indicated, "adr_pct")?;
    let turnover = floats(&indicated, "turnover_avg")?;
    // `group_by("instrument_id", maintain_order=True).agg(... .last())`: the last row of each
    // instrument, in first-seen order.
    let mut last: BTreeMap<i64, usize> = BTreeMap::new();
    let mut order: Vec<i64> = Vec::new();
    for (i, id) in ids.iter().enumerate() {
        let Some(id) = *id else { continue };
        if last.insert(id, i).is_none() {
            order.push(id);
        }
    }
    let as_of_day = day_number(as_of);
    let mut picked: Vec<(String, usize, i64)> = Vec::new();
    for id in order {
        let Some(&i) = last.get(&id) else { continue };
        if !is_liquid(adr[i], turnover[i], close[i], &config.liquidity) || day[i] != Some(as_of_day)
        {
            continue;
        }
        picked.push((symbols[i].clone().unwrap_or_default(), i, id));
    }
    picked.sort_by(|a, b| a.0.cmp(&b.0));
    let mut names = Vec::with_capacity(picked.len());
    for (symbol, i, id) in picked {
        let (Some(volume), Some(last_close)) = (vol[i], close[i]) else {
            continue;
        };
        let f = match factor[i] {
            Some(x) if x != 0.0 => x,
            _ => 1.0,
        };
        names.push(LiquidName {
            instrument_id: id,
            symbol,
            avg_daily_volume: decimal_of_float(volume)?,
            prev_close: decimal_of_float(py_round(last_close / f, 2))?,
            adr_pct: match adr[i] {
                Some(a) if a != 0.0 => decimal_of_float(py_round(a, 2))?,
                _ => Decimal::ZERO,
            },
            turnover_avg: match turnover[i] {
                Some(t) if t != 0.0 => {
                    // `round(float)` is an int, half to even.
                    let whole = t.round_ties_even();
                    Some(decimal_of_float(whole)?.normalize())
                }
                _ => None,
            },
        });
    }
    Ok(names)
}

/// `last_factors`: `instrument_id -> adj_factor` of the bar published for `on`.
async fn last_factors(
    conn: &mut PgConnection,
    on: NaiveDate,
    instrument_ids: &[i64],
) -> Result<BTreeMap<i64, f64>, WorkerError> {
    if instrument_ids.is_empty() {
        return Ok(BTreeMap::new());
    }
    let rows: Vec<(i64, Option<f64>)> = sqlx::query_as(
        "SELECT ohlcv_daily.instrument_id, ohlcv_daily.adj_factor::float8 FROM ohlcv_daily \
         WHERE ohlcv_daily.date = $1 AND ohlcv_daily.instrument_id = ANY($2)",
    )
    .bind(on)
    .bind(instrument_ids)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows
        .into_iter()
        .map(|(id, factor)| (id, factor.unwrap_or(1.0)))
        .collect())
}

/// `BarBuild`.
#[derive(Debug, Default)]
pub(super) struct BarBuild {
    pub frame: Option<DataFrame>,
    pub bars: usize,
    pub quotes: usize,
    pub no_quote: usize,
    pub no_factor: usize,
    pub bad_price: usize,
}

impl BarBuild {
    fn as_detail(&self) -> Vec<(String, OrderedJson)> {
        let n = |x: usize| int(i64::try_from(x).unwrap_or(i64::MAX));
        vec![
            ("quotes".to_owned(), n(self.quotes)),
            ("bars_built".to_owned(), n(self.bars)),
            (
                "skipped".to_owned(),
                object([
                    ("no_quote", n(self.no_quote)),
                    ("no_factor", n(self.no_factor)),
                    ("bad_price", n(self.bad_price)),
                ]),
            ),
        ]
    }
}

/// `_money(value)`: `float(Decimal)` or `None`.
fn money(value: Option<Decimal>) -> Option<f64> {
    value.map(float_of)
}

/// `x or last` for a float: `0.0` and `None` are falsy.
fn or_last(x: Option<f64>, last: f64) -> f64 {
    match x {
        Some(v) if v != 0.0 => v,
        _ => last,
    }
}

/// `provisional_bars`: one bar per name that answered, in the adjusted space the detectors read.
///
/// # Errors
/// A frame that cannot be built.
pub(super) fn provisional_bars(
    names: &[LiquidName],
    quotes: &[QuoteRecord],
    factors: &BTreeMap<i64, f64>,
    on: NaiveDate,
) -> Result<BarBuild, WorkerError> {
    let by_symbol: BTreeMap<&str, &LiquidName> =
        names.iter().map(|n| (n.symbol.as_str(), n)).collect();
    let mut build = BarBuild {
        quotes: quotes.len(),
        ..BarBuild::default()
    };
    let mut answered: BTreeSet<&str> = BTreeSet::new();
    let mut ids = Vec::new();
    let mut symbols = Vec::new();
    let mut cols: [Vec<Option<f64>>; 8] = Default::default();
    for quote in quotes {
        let Some(name) = by_symbol.get(quote.symbol.as_str()) else {
            continue;
        };
        answered.insert(name.symbol.as_str());
        let factor = match factors.get(&name.instrument_id) {
            Some(f) if *f > 0.0 => *f,
            _ => {
                build.no_factor += 1;
                continue;
            }
        };
        let last = float_of(quote.last_price);
        if last.is_nan() || last <= 0.0 {
            build.bad_price += 1;
            continue;
        }
        let open = or_last(money(quote.open), last);
        let high = or_last(money(quote.high), last).max(last);
        let low = or_last(money(quote.low), last).min(last);
        #[allow(clippy::cast_precision_loss)]
        let volume = quote.volume.max(0) as f64;
        let circuit = money(quote.upper_circuit);
        ids.push(name.instrument_id);
        symbols.push(name.symbol.clone());
        cols[0].push(Some(open * factor));
        cols[1].push(Some(high * factor));
        cols[2].push(Some(low * factor));
        cols[3].push(Some(last * factor));
        cols[4].push(Some(volume));
        cols[5].push(Some(py_round(last * volume, 2)));
        cols[6].push(circuit.map(|c| c * factor));
        cols[7].push(Some(factor));
    }
    build.no_quote = by_symbol.len() - answered.len();
    build.bars = ids.len();
    if !ids.is_empty() {
        let n = ids.len();
        let mut columns = vec![
            Series::new(BAR_COLUMNS[0].into(), ids).into_column(),
            Series::new(BAR_COLUMNS[1].into(), symbols).into_column(),
            date_column(BAR_COLUMNS[2], vec![Some(day_number(on)); n])?,
        ];
        for (name, values) in BAR_COLUMNS[3..].iter().zip(cols) {
            columns.push(Series::new((*name).into(), values).into_column());
        }
        build.frame = Some(DataFrame::new(n, columns).map_err(|e| pl_err(&e))?);
    }
    Ok(build)
}

/// `ScanReport`.
#[derive(Debug, Clone)]
pub(super) struct ScanReport {
    pub run_id: i64,
    pub status: &'static str,
    pub session_date: Option<NaiveDate>,
    pub provisional: bool,
    pub candidates: i64,
    pub detail: OrderedJson,
    pub error: Option<String>,
}

impl ScanReport {
    pub(super) fn as_detail(&self) -> Vec<(String, OrderedJson)> {
        vec![
            ("run_id".to_owned(), int(self.run_id)),
            ("status".to_owned(), s(self.status)),
            (
                "session_date".to_owned(),
                pyjson::opt_s(self.session_date.map(|d| d.format("%Y-%m-%d").to_string())),
            ),
            (
                "provisional".to_owned(),
                OrderedJson::Bool(self.provisional),
            ),
            ("candidates".to_owned(), int(self.candidates)),
            ("detail".to_owned(), self.detail.clone()),
            ("error".to_owned(), pyjson::opt_s(self.error.clone())),
        ]
    }
}

/// `f"{type(exc).__name__}: {exc}"`.
pub(super) fn describe(error: &WorkerError) -> String {
    format!("{}: {error}", error.python_type())
}

fn jsonb(value: &OrderedJson) -> Result<String, WorkerError> {
    value
        .to_python_json()
        .map_err(|e| WorkerError::python("TypeError", e.to_string()))
}

/// `run_scan_now(session, run_id=, user_id=, index_slug=, execution_enabled=, quote_source=,
/// now=)` inside the caller's transaction.
///
/// # Errors
/// `ScanNotRunnable` when the run row is not there (the task retries it); the database's.
pub(super) async fn run_scan_now(
    ctx: &TaskContext,
    conn: &mut PgConnection,
    run_id: i64,
    user_id: i64,
    index_slug: &str,
) -> Result<ScanReport, WorkerError> {
    let row: Option<(Option<OrderedJson>,)> =
        sqlx::query_as::<_, (Option<sqlx::types::Json<OrderedJson>>,)>(
            "SELECT sw_scan_run.detail FROM sw_scan_run \
         WHERE sw_scan_run.id = $1 AND sw_scan_run.user_id = $2",
        )
        .bind(run_id)
        .bind(user_id)
        .fetch_optional(&mut *conn)
        .await?
        .map(|(d,)| (d.map(|j| j.0),));
    let Some((detail,)) = row else {
        return Err(not_runnable(format!(
            "no sw_scan_run {run_id} for this user"
        )));
    };
    let started: DateTime<Utc> = ctx.now_utc();
    sqlx::query("UPDATE sw_scan_run SET status = $1, started_at = $2 WHERE sw_scan_run.id = $3")
        .bind(RUNNING)
        .bind(started)
        .bind(run_id)
        .execute(&mut *conn)
        .await?;
    let requested: Vec<(String, OrderedJson)> = match detail {
        Some(OrderedJson::Object(entries)) => entries,
        _ => Vec::new(),
    };
    let now = ctx.now_ist();

    let mut decision: Option<ScanDecision> = None;
    let outcome = {
        let mut sp = conn.begin().await?;
        let result = async {
            let d = decide_session(&mut sp, now).await?;
            decision = Some(d);
            scan(ctx, &mut sp, run_id, user_id, &d, index_slug).await
        }
        .await;
        match result {
            Ok(done) => {
                sp.commit().await?;
                Ok(done)
            }
            Err(e) => {
                sp.rollback().await?;
                Err(e)
            }
        }
    };
    match outcome {
        Err(e) => {
            let error = describe(&e);
            crate::steps::log_warning(&format!("scan now {run_id} failed: {error}"));
            let finished = ctx.now_utc();
            if let Some(d) = decision {
                sqlx::query(
                    "UPDATE sw_scan_run SET finished_at = $1, session_date = $2, \
                     provisional = $3, status = $4, error = $5 WHERE sw_scan_run.id = $6",
                )
                .bind(finished)
                .bind(d.session_date)
                .bind(d.provisional)
                .bind(FAILED)
                .bind(&error)
                .bind(run_id)
                .execute(&mut *conn)
                .await?;
            } else {
                sqlx::query(
                    "UPDATE sw_scan_run SET finished_at = $1, status = $2, error = $3 \
                     WHERE sw_scan_run.id = $4",
                )
                .bind(finished)
                .bind(FAILED)
                .bind(&error)
                .bind(run_id)
                .execute(&mut *conn)
                .await?;
            }
            Ok(ScanReport {
                run_id,
                status: FAILED,
                session_date: decision.map(|d| d.session_date),
                provisional: decision.is_some_and(|d| d.provisional),
                candidates: 0,
                detail: OrderedJson::Object(requested),
                error: Some(error),
            })
        }
        Ok((written, scan_detail)) => {
            let mut merged = requested;
            for (k, v) in scan_detail {
                pyjson::update(&mut merged, &k, v);
            }
            let merged = OrderedJson::Object(merged);
            let d = decision.ok_or_else(|| not_runnable("the scan made no decision"))?;
            let finished = ctx.now_utc();
            sqlx::query(
                "UPDATE sw_scan_run SET finished_at = $1, session_date = $2, provisional = $3, \
                 status = $4, detail = CAST($5 AS JSONB), error = NULL WHERE sw_scan_run.id = $6",
            )
            .bind(finished)
            .bind(d.session_date)
            .bind(d.provisional)
            .bind(DONE)
            .bind(jsonb(&merged)?)
            .bind(run_id)
            .execute(&mut *conn)
            .await?;
            Ok(ScanReport {
                run_id,
                status: DONE,
                session_date: Some(d.session_date),
                provisional: d.provisional,
                candidates: written,
                detail: merged,
                error: None,
            })
        }
    }
}

/// `_scan`: the bar build when the session is live, then the nightly's body.
async fn scan(
    ctx: &TaskContext,
    conn: &mut PgConnection,
    run_id: i64,
    user_id: i64,
    decision: &ScanDecision,
    index_slug: &str,
) -> Result<(i64, Vec<(String, OrderedJson)>), WorkerError> {
    let scanned_at = py_isoformat(ctx.now_utc());
    let mut scan_meta: Vec<(String, OrderedJson)> = vec![
        ("run_id".to_owned(), int(run_id)),
        ("scanned_at".to_owned(), s(scanned_at.clone())),
        (
            "provisional".to_owned(),
            OrderedJson::Bool(decision.provisional),
        ),
        ("reason".to_owned(), s(decision.reason)),
    ];
    let mut build: Option<BarBuild> = None;
    let mut live_index_level: Option<f64> = None;
    if decision.provisional {
        let source = kite_provider(ctx, KiteLane::Interactive).await?;
        let config = load_swing_config(conn, user_id).await?;
        let names = liquid_universe(conn, decision.published_as_of, &config).await?;
        let built = if names.is_empty() {
            BarBuild::default()
        } else {
            let ids: Vec<i64> = names.iter().map(|n| n.instrument_id).collect();
            let factors = last_factors(conn, decision.published_as_of, &ids).await?;
            let symbols: Vec<&str> = names.iter().map(|n| n.symbol.as_str()).collect();
            let records = source.quotes(&symbols, "NSE").await?;
            provisional_bars(&names, &records, &factors, decision.session_date)?
        };
        scan_meta.push((
            "quotes".to_owned(),
            int(i64::try_from(built.quotes).unwrap_or(i64::MAX)),
        ));
        // Best-effort: a provider that cannot quote the index answers None and the gate reads
        // published closes — late, never wrong.
        match source.index_level(index_slug).await {
            Ok(Some(level)) => {
                let level = float_of(level);
                live_index_level = Some(level);
                scan_meta.push(("live_index_level".to_owned(), OrderedJson::Float(level)));
            }
            Ok(None) => {}
            Err(_) => crate::steps::log_warning(&format!(
                "could not read a live level for {index_slug}; the gate reads published closes"
            )),
        }
        build = Some(built);
    }
    let mut outcome = StepOutcome::default();
    let extras = ScanExtras {
        extra_bars: build.as_ref().and_then(|b| b.frame.clone()),
        provisional: decision.provisional,
        reference_date: decision.provisional.then_some(decision.published_as_of),
        scan: Some(OrderedJson::Object(scan_meta)),
        live_index_level,
    };
    let written = run_detect_swing_scan(
        conn,
        &mut outcome,
        decision.session_date,
        user_id,
        index_slug,
        None,
        &extras,
    )
    .await?;
    let mut detail: Vec<(String, OrderedJson)> = vec![
        ("reason".to_owned(), s(decision.reason)),
        ("candidates".to_owned(), int(written)),
        ("scanned_at".to_owned(), s(scanned_at)),
    ];
    for key in ["funnel", "skipped_reason"] {
        if let Some((_, v)) = outcome.detail.iter().find(|(k, _)| k == key) {
            pyjson::update(&mut detail, key, v.clone());
        }
    }
    if let Some(b) = &build {
        for (k, v) in b.as_detail() {
            pyjson::update(&mut detail, &k, v);
        }
    }
    Ok((written, detail))
}

/// `sweep_queued`: every `QUEUED` row nobody has published (`task_id` null), oldest first.
pub(super) async fn queued_unpublished(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Vec<i64>, WorkerError> {
    let rows: Vec<(i64,)> = sqlx::query_as(
        "SELECT sw_scan_run.id FROM sw_scan_run WHERE sw_scan_run.user_id = $1 \
         AND sw_scan_run.status = $2 AND sw_scan_run.task_id IS NULL ORDER BY sw_scan_run.id",
    )
    .bind(user_id)
    .bind(QUEUED)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().map(|(id,)| id).collect())
}

/// Stamp a published row's `task_id`.
pub(super) async fn stamp_task_id(
    conn: &mut PgConnection,
    run_id: i64,
    task_id: &str,
) -> Result<(), WorkerError> {
    sqlx::query("UPDATE sw_scan_run SET task_id = $1 WHERE sw_scan_run.id = $2")
        .bind(task_id)
        .bind(run_id)
        .execute(&mut *conn)
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::float_cmp)]
    use super::*;
    use baskfy_providers::records::QuoteFields;

    fn at(text: &str) -> NaiveDateTime {
        NaiveDateTime::parse_from_str(text, "%Y-%m-%d %H:%M").unwrap_or_default()
    }

    fn dec(text: &str) -> Decimal {
        text.parse().unwrap_or_default()
    }

    fn name(id: i64, symbol: &str) -> LiquidName {
        LiquidName {
            instrument_id: id,
            symbol: symbol.to_owned(),
            avg_daily_volume: Decimal::ONE,
            prev_close: Decimal::ONE,
            adr_pct: Decimal::ZERO,
            turnover_avg: None,
        }
    }

    fn quote(symbol: &str, last: &str, f: impl FnOnce(&mut QuoteFields)) -> QuoteRecord {
        let mut fields = QuoteFields::new(symbol, dec(last));
        f(&mut fields);
        QuoteRecord::new(fields).unwrap_or_else(|_| {
            QuoteRecord::new(QuoteFields::new("X", Decimal::ONE)).unwrap_or_else(|_| unreachable!())
        })
    }

    #[test]
    fn the_session_starts_at_the_open_and_never_wraps_to_the_small_hours() {
        assert!(!session_has_started(at("2026-10-30 09:14")));
        assert!(session_has_started(at("2026-10-30 09:15")));
        assert!(session_has_started(at("2026-10-30 16:00")));
        assert!(session_has_started(at("2026-10-30 23:59")));
        assert!(!session_has_started(at("2026-10-30 00:05")));
        assert!(market_is_open(at("2026-10-30 15:30")));
        assert!(!market_is_open(at("2026-10-30 15:31")));
    }

    #[test]
    fn a_provisional_bar_is_the_quote_in_the_adjusted_space() {
        let names = [
            name(1, "AAA"),
            name(2, "BBB"),
            name(3, "CCC"),
            name(4, "DDD"),
        ];
        let quotes = [
            quote("AAA", "110", |q| {
                q.open = Some(dec("100"));
                q.high = Some(dec("105"));
                q.low = Some(dec("99"));
                q.volume = 1000;
                q.upper_circuit = Some(dec("120"));
            }),
            quote("BBB", "0", |_| {}),
            quote("CCC", "50", |_| {}),
            quote("ZZZ", "50", |_| {}),
        ];
        let factors = BTreeMap::from([(1, 0.5), (2, 1.0)]);
        let on = NaiveDate::from_ymd_opt(2026, 10, 30).unwrap_or_default();
        let build = provisional_bars(&names, &quotes, &factors, on).unwrap_or_default();
        assert_eq!(build.quotes, 4);
        assert_eq!(build.bars, 1);
        assert_eq!(build.bad_price, 1);
        assert_eq!(build.no_factor, 1);
        assert_eq!(build.no_quote, 1);
        let frame = build.frame.unwrap_or_default();
        assert_eq!(floats(&frame, "open").unwrap_or_default(), [Some(50.0)]);
        // The high is never below the last print, nor the low above it.
        assert_eq!(floats(&frame, "high").unwrap_or_default(), [Some(55.0)]);
        assert_eq!(floats(&frame, "low").unwrap_or_default(), [Some(49.5)]);
        assert_eq!(floats(&frame, "close").unwrap_or_default(), [Some(55.0)]);
        assert_eq!(
            floats(&frame, "turnover").unwrap_or_default(),
            [Some(110_000.0)]
        );
        assert_eq!(
            floats(&frame, "upper_circuit").unwrap_or_default(),
            [Some(60.0)]
        );
    }

    fn today() -> NaiveDate {
        NaiveDate::from_ymd_opt(2026, 9, 3).unwrap_or_default()
    }

    fn ohlc_quote(symbol: &str, last: &str, volume: i64, ohlc: bool) -> QuoteRecord {
        quote(symbol, last, |q| {
            q.volume = volume;
            if ohlc {
                q.open = Some(dec("101"));
                q.high = Some(dec("104"));
                q.low = Some(dec("99"));
            }
            q.upper_circuit = Some(dec("120"));
        })
    }

    /// Ports `test_one_bar_per_quote_in_the_adjusted_space`: a 1:2 split left `adj_factor` 0.5.
    #[test]
    fn one_bar_per_quote_in_the_adjusted_space() {
        let build = provisional_bars(
            &[name(1, "SPLITCO")],
            &[ohlc_quote("SPLITCO", "102", 400_000, true)],
            &BTreeMap::from([(1, 0.5)]),
            today(),
        )
        .unwrap_or_default();
        let frame = build.frame.unwrap_or_default();
        assert_eq!(frame.height(), 1);
        assert_eq!(
            dates(&frame, "date").unwrap_or_default(),
            [Some(day_number(today()))]
        );
        for (column, want) in [
            ("open", 50.5),
            ("high", 52.0),
            ("low", 49.5),
            ("close", 51.0),
            ("volume", 400_000.0),
            ("turnover", 102.0 * 400_000.0),
            ("upper_circuit", 60.0),
            ("adj_factor", 0.5),
        ] {
            assert_eq!(
                floats(&frame, column).unwrap_or_default(),
                [Some(want)],
                "{column}"
            );
        }
        assert_eq!(build.quotes, 1);
        assert_eq!(
            (build.no_quote, build.no_factor, build.bad_price),
            (0, 0, 0)
        );
    }

    #[test]
    fn a_quote_with_no_volume_is_still_a_bar() {
        let build = provisional_bars(
            &[name(1, "QUIET")],
            &[ohlc_quote("QUIET", "100", 0, true)],
            &BTreeMap::from([(1, 1.0)]),
            today(),
        )
        .unwrap_or_default();
        let frame = build.frame.unwrap_or_default();
        assert_eq!(floats(&frame, "turnover").unwrap_or_default(), [Some(0.0)]);
    }

    #[test]
    fn a_missing_ohlc_side_takes_the_last_price_and_the_high_low_bracket_it() {
        let build = provisional_bars(
            &[name(1, "THIN")],
            &[ohlc_quote("THIN", "100", 10, false)],
            &BTreeMap::from([(1, 1.0)]),
            today(),
        )
        .unwrap_or_default();
        let frame = build.frame.unwrap_or_default();
        for column in ["open", "high", "low", "close"] {
            assert_eq!(floats(&frame, column).unwrap_or_default(), [Some(100.0)]);
        }
    }

    #[test]
    fn a_name_with_no_factor_is_skipped_not_invented_and_a_silent_name_is_counted() {
        let names = [
            name(1, "STALE"),
            name(2, "SILENT"),
            name(3, "FINE"),
            name(4, "ZERO"),
        ];
        let build = provisional_bars(
            &names,
            &[
                ohlc_quote("STALE", "100", 1, true),
                ohlc_quote("FINE", "100", 1, true),
                ohlc_quote("ZERO", "0", 1, true),
                ohlc_quote("STRANGER", "100", 1, true),
            ],
            &BTreeMap::from([(2, 1.0), (3, 1.0), (4, 1.0)]),
            today(),
        )
        .unwrap_or_default();
        let frame = build.frame.unwrap_or_default();
        assert_eq!(
            strings(&frame, "symbol").unwrap_or_default(),
            [Some("FINE".to_owned())]
        );
        assert_eq!(
            (build.no_quote, build.no_factor, build.bad_price),
            (1, 1, 1)
        );
        assert_eq!(build.quotes, 4);
    }

    #[test]
    fn no_quotes_is_no_frame_and_no_bar() {
        let build = provisional_bars(&[name(1, "A")], &[], &BTreeMap::from([(1, 1.0)]), today())
            .unwrap_or_default();
        assert!(build.frame.is_none());
        assert_eq!(build.bars, 0);
        assert_eq!(build.no_quote, 1);
    }

    #[test]
    fn the_window_opens_at_the_open_and_stays_open() {
        for moment in [
            "2026-09-08 09:15",
            "2026-09-08 13:00",
            "2026-09-08 16:00",
            "2026-09-08 23:59",
        ] {
            assert!(session_has_started(at(moment)), "{moment}");
        }
        for moment in ["2026-09-08 00:01", "2026-09-08 08:00", "2026-09-08 09:14"] {
            assert!(!session_has_started(at(moment)), "{moment}");
        }
    }

    #[test]
    fn it_is_strictly_wider_than_market_is_open_and_never_narrower() {
        for hour in 0..24 {
            for minute in [0, 14, 15, 30, 59] {
                let moment = NaiveDate::from_ymd_opt(2026, 9, 8)
                    .and_then(|d| d.and_hms_opt(hour, minute, 0))
                    .unwrap_or_default();
                if market_is_open(moment) {
                    assert!(session_has_started(moment), "{moment}");
                }
            }
        }
    }

    #[test]
    fn a_float_becomes_the_decimal_of_its_repr() {
        assert_eq!(decimal_of_float(0.1).unwrap_or_default(), dec("0.1"));
        assert_eq!(decimal_of_float(1234.0).unwrap_or_default(), dec("1234.0"));
        assert_eq!(float_of(dec("123.45")), 123.45);
    }
}
