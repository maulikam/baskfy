//! SW5 — the evening job. Ports `baskfy_worker.tasks.swing_eod` (`run_swing_eod` and every helper
//! it calls: `manage_open_positions`, `settle_ladder` with `rung_in_force` and
//! `closed_r_multiples`, `_record_session`, `count_first_live_session`, `watch_items`,
//! `sleeve_account`, `held_instrument_ids`, `send_eod_email`) and the writes of
//! `baskfy_api.swing_settings.record_system_change`.
//!
//! The order matters and is Python's: auto-watch and expire; manage the book (exits); settle the
//! ladder (and the drawdown); count the session and the first-live countdown; plan tomorrow
//! (entries after exits); store the plan; record it on the session; send the email.
//!
//! **This job places nothing.** It writes a plan with a 30-minute expiry that the desk console
//! can confirm, line by line, on a click.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_api::Settings;
use baskfy_api::mailer::Mailer;
use baskfy_core::decimal::WideDecimal;
use baskfy_core::swing::market::{ExposureTier, MarketGate, exposure_tier};
use baskfy_core::swing::plan::{
    LineKind, PlanLine, SwingAccount, WatchItem, build_entries, first_live_multiplier,
};
use baskfy_core::swing::setups::{Setup, SwingConfig};
use baskfy_core::swing::stops::{ActionKind, DailyBar, OpenPosition, TrailMa, manage};
use baskfy_db::OrderedJson;
use chrono::{DateTime, NaiveDate, Utc};
use rust_decimal::Decimal;
use sqlx::{PgConnection, Row as _};

use super::mail::{SwingCandidate, SwingDigest, money, swing_eod};
use super::plan::{SwingPlan, assemble, enum_value, exit_lines, store_plan};
use super::watch::{auto_watch, list_watch};
use crate::error::WorkerError;
use crate::pyjson::{self, int, object, s, strings};
use crate::steps::sleeves::for_swing_eod::{
    NSE_EXCHANGE_ID, config_row, lookback_start, sleeve_drawdown, swing_config,
};
use crate::steps::{StepOutcome, StepStatus, log_warning};

/// `MA_LOOKBACK_SESSIONS`: `manage` reads MA10 and MA20, so 20 plus room for a missed session.
const MA_LOOKBACK_SESSIONS: i64 = 40;
/// `SYSTEM_OWNED_FIELDS[...]`: the evening's name on the audit rows.
const LADDER_CHANGED_BY: &str = "swing-eod";
/// `swing_journal.PAPER_SESSIONS_REQUIRED`.
const PAPER_SESSIONS_REQUIRED: i64 = 20;
/// `MAX_FLAGS_IN_EMAIL`.
const MAX_FLAGS_IN_EMAIL: usize = 10;

fn core_err(e: baskfy_core::error::CoreError) -> WorkerError {
    WorkerError::python("ValueError", e.to_string())
}

fn dec_err(e: baskfy_core::decimal::DecimalError) -> WorkerError {
    WorkerError::python("InvalidOperation", e.to_string())
}

fn overflow(what: &str) -> WorkerError {
    WorkerError::python("OverflowError", what.to_owned())
}

fn count(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

fn iso(day: NaiveDate) -> String {
    day.format("%Y-%m-%d").to_string()
}

fn wide_str(value: &WideDecimal) -> String {
    value.to_sci_string()
}

fn wide_dec(value: &WideDecimal) -> Result<Decimal, WorkerError> {
    value.try_to_decimal().map_err(dec_err)
}

/// `float(Decimal)`.
fn wide_float(value: &WideDecimal) -> Result<f64, WorkerError> {
    wide_str(value)
        .parse::<f64>()
        .map_err(|e| WorkerError::python("ValueError", e.to_string()))
}

/// `EodReport`.
#[derive(Debug, Clone)]
pub(super) struct EodReport {
    trade_date: NaiveDate,
    gate: String,
    exposure_level: i64,
    rung_before: i64,
    closed_r: Vec<String>,
    drawdown_pct: String,
    drawdown_locked: bool,
    watch_added: i64,
    watch_expired: i64,
    watching: i64,
    positions_managed: i64,
    exit_lines: i64,
    entry_lines: i64,
    pending_lines: i64,
    skips: i64,
    naked_positions: Vec<String>,
    plan_id: Option<String>,
    sessions_logged: i64,
    first_live_sessions_left: i64,
    risk_multiplier: String,
    risk_pct_in_force: String,
}

impl EodReport {
    fn new(trade_date: NaiveDate, gate: String, exposure_level: i64) -> Self {
        Self {
            trade_date,
            gate,
            exposure_level,
            rung_before: 0,
            closed_r: Vec::new(),
            drawdown_pct: "0.00".to_owned(),
            drawdown_locked: false,
            watch_added: 0,
            watch_expired: 0,
            watching: 0,
            positions_managed: 0,
            exit_lines: 0,
            entry_lines: 0,
            pending_lines: 0,
            skips: 0,
            naked_positions: Vec::new(),
            plan_id: None,
            sessions_logged: 0,
            first_live_sessions_left: 0,
            risk_multiplier: "1".to_owned(),
            risk_pct_in_force: "0.500".to_owned(),
        }
    }

    /// `first_live_header()`.
    fn first_live_header(&self) -> String {
        first_live_header(self.first_live_sessions_left, &self.risk_pct_in_force)
    }

    /// `as_detail()`.
    pub(super) fn as_detail(&self) -> OrderedJson {
        object([
            ("trade_date", s(iso(self.trade_date))),
            ("gate", s(self.gate.clone())),
            ("exposure_level", int(self.exposure_level)),
            (
                "ladder",
                object([
                    ("from", int(self.rung_before)),
                    ("to", int(self.exposure_level)),
                    ("closed_r_multiples", strings(self.closed_r.clone())),
                    ("drawdown_pct", s(self.drawdown_pct.clone())),
                    ("drawdown_locked", OrderedJson::Bool(self.drawdown_locked)),
                ]),
            ),
            (
                "watch",
                object([
                    ("added", int(self.watch_added)),
                    ("expired", int(self.watch_expired)),
                    ("watching", int(self.watching)),
                ]),
            ),
            (
                "plan",
                object([
                    ("plan_id", pyjson::opt_s(self.plan_id.clone())),
                    ("exits", int(self.exit_lines)),
                    ("entries", int(self.entry_lines)),
                    ("pending", int(self.pending_lines)),
                    ("skips", int(self.skips)),
                    ("risk_multiplier", s(self.risk_multiplier.clone())),
                    ("risk_pct_in_force", s(self.risk_pct_in_force.clone())),
                ]),
            ),
            (
                "first_live",
                object([
                    ("sessions_left", int(self.first_live_sessions_left)),
                    ("header", s(self.first_live_header())),
                ]),
            ),
            ("positions_managed", int(self.positions_managed)),
            ("naked_positions", strings(self.naked_positions.clone())),
            ("sessions_logged", int(self.sessions_logged)),
        ])
    }
}

/// `first_live_header(sessions_left, risk_pct_in_force)`.
pub(super) fn first_live_header(sessions_left: i64, risk_pct_in_force: &str) -> String {
    if sessions_left <= 0 {
        return String::new();
    }
    format!("first live sessions: {sessions_left} left · risk {risk_pct_in_force}%")
}

/// `risk_pct_in_force(config, multiplier)`: `Decimal(str(risk_per_trade_pct)) x multiplier` at
/// three places (the context's half-even).
pub(crate) fn risk_pct_in_force(
    config: &SwingConfig,
    multiplier: Decimal,
) -> Result<String, WorkerError> {
    let base = WideDecimal::from(
        baskfy_core::py::decimal_from_float_str(config.sizing.risk_per_trade_pct)
            .map_err(dec_err)?,
    );
    let product = base.mul(&WideDecimal::from(multiplier));
    Ok(wide_str(
        &product
            .quantize(-3, baskfy_core::decimal::Rounding::HalfEven)
            .map_err(dec_err)?,
    ))
}

/// `_mean(values, window)`: the trailing mean of the last `window` closes, `None` before the
/// window is full.
fn mean(values: &[Decimal], window: i64) -> Result<Option<Decimal>, WorkerError> {
    let Ok(n) = usize::try_from(window) else {
        return Ok(None);
    };
    if n == 0 || values.len() < n {
        return Ok(None);
    }
    let mut total = Decimal::ZERO;
    for v in &values[values.len() - n..] {
        total = total
            .checked_add(*v)
            .ok_or_else(|| overflow("sum of closes"))?;
    }
    Ok(Some(
        total
            .checked_div(Decimal::from(window))
            .ok_or_else(|| overflow("mean of closes"))?,
    ))
}

/// `_sessions_between(start, end)`: trading days strictly after `start` and up to `end`.
async fn sessions_between(
    conn: &mut PgConnection,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<i64, WorkerError> {
    let (n,): (i64,) = sqlx::query_as(
        "SELECT count(*) FROM trading_day WHERE trading_day.exchange_id = $1 \
         AND trading_day.is_trading_day IS true AND trading_day.date > $2 \
         AND trading_day.date <= $3",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(start)
    .bind(end)
    .fetch_one(&mut *conn)
    .await?;
    Ok(n)
}

/// One open `sw_position` row, as `manage` and the account read it.
struct Position {
    id: i64,
    instrument_id: i64,
    symbol: String,
    entry_date: NaiveDate,
    entry_avg: Decimal,
    initial_stop: Decimal,
    stop: Decimal,
    quantity_open: i64,
    partial_done: bool,
    trail: String,
    setup: String,
    gtt_id: Option<String>,
}

async fn open_positions(
    conn: &mut PgConnection,
    user_id: i64,
    ordered: bool,
) -> Result<Vec<Position>, WorkerError> {
    let sql = format!(
        "SELECT sw_position.id, sw_position.instrument_id, instrument.symbol, \
         sw_position.entry_date, sw_position.entry_avg, sw_position.initial_stop, \
         sw_position.stop, sw_position.quantity_open, sw_position.partial_done, \
         sw_position.trail, sw_position.setup, sw_position.gtt_id::text AS gtt_id \
         FROM sw_position JOIN instrument ON instrument.id = sw_position.instrument_id \
         WHERE sw_position.user_id = $1 AND sw_position.state IN ('OPEN', 'PARTIAL') \
         AND sw_position.quantity_open > 0{}",
        if ordered {
            " ORDER BY instrument.symbol"
        } else {
            ""
        }
    );
    let rows = sqlx::query(&sql)
        .bind(user_id)
        .fetch_all(&mut *conn)
        .await?;
    let mut out = Vec::with_capacity(rows.len());
    for row in &rows {
        let quantity: i32 = row.try_get("quantity_open")?;
        out.push(Position {
            id: row.try_get("id")?,
            instrument_id: row.try_get("instrument_id")?,
            symbol: row.try_get("symbol")?,
            entry_date: row.try_get("entry_date")?,
            entry_avg: baskfy_db::row::decimal(row, "entry_avg")?,
            initial_stop: baskfy_db::row::decimal(row, "initial_stop")?,
            stop: baskfy_db::row::decimal(row, "stop")?,
            quantity_open: i64::from(quantity),
            partial_done: row.try_get("partial_done")?,
            trail: row.try_get("trail")?,
            setup: row.try_get("setup")?,
            gtt_id: row.try_get("gtt_id")?,
        });
    }
    Ok(out)
}

/// One bar: `(date, open, high, low, close)`.
type Bar = (NaiveDate, Decimal, Decimal, Decimal, Decimal);

/// `_bars_for`: `(date, open, high, low, close)` per instrument, oldest first.
async fn bars_for(
    conn: &mut PgConnection,
    instrument_ids: &[i64],
    start: NaiveDate,
    end: NaiveDate,
) -> Result<BTreeMap<i64, Vec<Bar>>, WorkerError> {
    let mut out: BTreeMap<i64, Vec<Bar>> = BTreeMap::new();
    if instrument_ids.is_empty() {
        return Ok(out);
    }
    let rows = sqlx::query(
        "SELECT instrument_id, date, open, high, low, close FROM ohlcv_daily \
         WHERE instrument_id = ANY($1) AND date >= $2 AND date <= $3 \
         ORDER BY instrument_id, date",
    )
    .bind(instrument_ids)
    .bind(start)
    .bind(end)
    .fetch_all(&mut *conn)
    .await?;
    for row in &rows {
        let id: i64 = row.try_get("instrument_id")?;
        out.entry(id).or_default().push((
            row.try_get("date")?,
            baskfy_db::row::decimal(row, "open")?,
            baskfy_db::row::decimal(row, "high")?,
            baskfy_db::row::decimal(row, "low")?,
            baskfy_db::row::decimal(row, "close")?,
        ));
    }
    Ok(out)
}

fn trail_of(text: &str) -> Result<TrailMa, WorkerError> {
    match text {
        "MA10" => Ok(TrailMa::Ma10),
        "MA20" => Ok(TrailMa::Ma20),
        other => Err(WorkerError::python(
            "ValueError",
            format!("'{other}' is not a valid TrailMa"),
        )),
    }
}

/// `manage_open_positions`: `stops.manage` over the book → `(exit lines, naked symbols,
/// positions managed)`. A position with no bar today is skipped, not managed.
pub(crate) async fn manage_open_positions(
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
    config: &SwingConfig,
) -> Result<(Vec<PlanLine>, Vec<String>, i64), WorkerError> {
    let positions = open_positions(conn, user_id, true).await?;
    if positions.is_empty() {
        return Ok((Vec::new(), Vec::new(), 0));
    }
    let ids: Vec<i64> = positions.iter().map(|p| p.instrument_id).collect();
    let start = lookback_start(conn, on, MA_LOOKBACK_SESSIONS).await?;
    let bars = bars_for(conn, &ids, start, on).await?;
    let mut lines = Vec::new();
    let mut naked = Vec::new();
    let mut managed = 0;
    for position in &positions {
        if position.gtt_id.is_none() {
            naked.push(position.symbol.clone());
        }
        let Some(series) = bars.get(&position.instrument_id) else {
            continue;
        };
        let Some(today) = series.last() else {
            continue;
        };
        if today.0 != on {
            continue;
        }
        let closes: Vec<Decimal> = series.iter().map(|b| b.4).collect();
        let bar = DailyBar {
            date: on,
            open: today.1,
            high: today.2,
            low: today.3,
            close: today.4,
            ma10: mean(&closes, config.ma_fast)?,
            ma20: mean(&closes, config.ma_slow)?,
            bars_since_entry: sessions_between(conn, position.entry_date, on).await?,
        };
        let open = OpenPosition {
            symbol: position.symbol.clone(),
            entry_date: position.entry_date,
            entry: position.entry_avg,
            initial_stop: position.initial_stop,
            stop: position.stop,
            quantity: position.quantity_open,
            partial_done: position.partial_done,
            trail: trail_of(&position.trail)?,
            is_ep_gap_day: position.setup == Setup::Ep.as_str(),
        };
        let actions = manage(&open, &bar, &config.stops).map_err(core_err)?;
        managed += 1;
        if actions.iter().all(|a| a.kind == ActionKind::Hold) {
            continue;
        }
        lines.extend(exit_lines(&position.symbol, &actions, Some(position.id))?);
    }
    Ok((lines, naked, managed))
}

/// `sleeve_account`: the sleeve's capital, its cash and what the open book carries at cost.
pub(crate) async fn sleeve_account(
    conn: &mut PgConnection,
    user_id: i64,
    capital: Option<Decimal>,
) -> Result<SwingAccount, WorkerError> {
    let capital = capital.unwrap_or(Decimal::ZERO);
    let rows = open_positions(conn, user_id, false).await?;
    let mut exposure = Decimal::ZERO;
    let mut symbols = BTreeSet::new();
    let mut counts: BTreeMap<String, i64> = BTreeMap::new();
    for row in &rows {
        let cost = row
            .entry_avg
            .checked_mul(Decimal::from(row.quantity_open))
            .ok_or_else(|| overflow("position cost"))?;
        exposure = exposure
            .checked_add(cost)
            .ok_or_else(|| overflow("open exposure"))?;
        symbols.insert(row.symbol.clone());
        *counts.entry(row.symbol.clone()).or_default() += 1;
    }
    let free = capital
        .checked_sub(exposure)
        .ok_or_else(|| overflow("cash available"))?;
    Ok(SwingAccount {
        equity: capital,
        cash_available: if free > Decimal::ZERO {
            free
        } else {
            Decimal::ZERO
        },
        open_symbols: symbols,
        open_exposure_inr: exposure,
        open_entry_counts: counts,
    })
}

/// `watch_items`: the watchlist as the plan builder wants it, plus symbol → instrument id. A row
/// with no trigger is dropped; the ADR, turnover, score and band come from the name's latest
/// detection row on or before `on`, the watch row's own when none exists.
pub(crate) async fn watch_items(
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
) -> Result<(Vec<WatchItem>, Vec<(String, i64)>), WorkerError> {
    let rows = list_watch(conn, user_id).await?;
    // `ORDER BY instrument_id, date DESC, score DESC` and the first row per instrument (score
    // DESC puts NULLs first in Postgres, as SQLAlchemy's `.desc()` leaves it).
    let latest_rows = sqlx::query(
        "SELECT DISTINCT ON (instrument_id) instrument_id, adr_pct, turnover_avg, score, \
         locked_upper_circuit FROM sw_setup_daily WHERE user_id = $1 AND date <= $2 \
         ORDER BY instrument_id, date DESC, score DESC",
    )
    .bind(user_id)
    .bind(on)
    .fetch_all(&mut *conn)
    .await?;
    struct Detected {
        adr_pct: Option<Decimal>,
        turnover_avg: Option<i64>,
        score: Option<Decimal>,
        locked: bool,
    }
    let mut latest: BTreeMap<i64, Detected> = BTreeMap::new();
    for row in &latest_rows {
        latest.insert(
            row.try_get("instrument_id")?,
            Detected {
                adr_pct: baskfy_db::row::opt_decimal(row, "adr_pct")?,
                turnover_avg: row.try_get("turnover_avg")?,
                score: baskfy_db::row::opt_decimal(row, "score")?,
                locked: row.try_get("locked_upper_circuit")?,
            },
        );
    }
    let mut items = Vec::new();
    let mut ids = Vec::new();
    for row in rows {
        let Some(trigger) = row.trigger else {
            continue;
        };
        let detected = latest.get(&row.instrument_id);
        let adr = match detected {
            Some(d) if d.adr_pct.is_some_and(|a| !a.is_zero()) => d.adr_pct,
            _ => row.adr_pct,
        };
        let score = match detected {
            Some(d) => d.score,
            None => row.score,
        };
        let setup = match row.setup.as_str() {
            "FLAG" => Setup::Flag,
            "EP" => Setup::Ep,
            "PARABOLIC_SHORT" => Setup::ParabolicShort,
            other => {
                return Err(WorkerError::python(
                    "ValueError",
                    format!("'{other}' is not a valid Setup"),
                ));
            }
        };
        items.push(WatchItem {
            symbol: row.symbol.clone(),
            setup,
            trigger,
            stop_ref: row.stop_ref,
            adr_pct: adr.filter(|a| !a.is_zero()).unwrap_or(Decimal::ZERO),
            avg_turnover_inr: detected
                .and_then(|d| d.turnover_avg)
                .filter(|t| *t != 0)
                .map(Decimal::from),
            score: score.unwrap_or(Decimal::ZERO),
            locked_upper_circuit: detected.is_some_and(|d| d.locked),
        });
        ids.push((row.symbol, row.instrument_id));
    }
    Ok((items, ids))
}

/// `held_instrument_ids`: symbol → instrument id for every position with shares open.
pub(crate) async fn held_instrument_ids(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Vec<(String, i64)>, WorkerError> {
    let rows: Vec<(String, i64)> = sqlx::query_as(
        "SELECT instrument.symbol, sw_position.instrument_id FROM sw_position \
         JOIN instrument ON instrument.id = sw_position.instrument_id \
         WHERE sw_position.user_id = $1 AND sw_position.quantity_open > 0",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows)
}

/// `sessions_logged`.
async fn sessions_logged(conn: &mut PgConnection, user_id: i64) -> Result<i64, WorkerError> {
    let (n,): (i64,) = sqlx::query_as("SELECT count(*) FROM sw_session WHERE user_id = $1")
        .bind(user_id)
        .fetch_one(&mut *conn)
        .await?;
    Ok(n)
}

/// `_record_session`: one `sw_session` row per session; a plan id is appended once. Returns how
/// many sessions exist. The row is updated only when its plan list changes (the ORM's flush).
async fn record_session(
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
    plan_id: Option<&str>,
    execution: bool,
) -> Result<i64, WorkerError> {
    let existing =
        sqlx::query("SELECT plan_ids FROM sw_session WHERE user_id = $1 AND session_date = $2")
            .bind(user_id)
            .bind(on)
            .fetch_optional(&mut *conn)
            .await?;
    let plans: Vec<String> = plan_id.map(str::to_owned).into_iter().collect();
    match existing {
        None => {
            let body = pyjson::dumps(&object([("plans", strings(plans))]))
                .map_err(|e| WorkerError::python("TypeError", e))?;
            sqlx::query(
                "INSERT INTO sw_session (user_id, session_date, mode, monitor_ran, plan_ids, \
                 notes) VALUES ($1, $2, $3, false, CAST($4 AS JSONB), 'swing-eod')",
            )
            .bind(user_id)
            .bind(on)
            .bind(if execution { "LIVE" } else { "DRY_RUN" })
            .bind(body)
            .execute(&mut *conn)
            .await?;
        }
        Some(row) => {
            let known = baskfy_db::row::opt_jsonb(&row, "plan_ids")?;
            let seen: Vec<String> = match known.as_ref().and_then(|k| match k {
                OrderedJson::Object(_) => k.get("plans"),
                _ => None,
            }) {
                Some(OrderedJson::Array(items)) => items.iter().map(py_str).collect(),
                _ => Vec::new(),
            };
            let mut merged = seen.clone();
            for p in &plans {
                if !seen.contains(p) {
                    merged.push(p.clone());
                }
            }
            let new_value = object([("plans", strings(merged))]);
            if known.as_ref() != Some(&new_value) {
                let body =
                    pyjson::dumps(&new_value).map_err(|e| WorkerError::python("TypeError", e))?;
                sqlx::query(
                    "UPDATE sw_session SET plan_ids = CAST($1 AS JSONB), updated_at = now() \
                     WHERE user_id = $2 AND session_date = $3",
                )
                .bind(body)
                .bind(user_id)
                .bind(on)
                .execute(&mut *conn)
                .await?;
            }
        }
    }
    sessions_logged(conn, user_id).await
}

/// `str(entry)` for a JSON value read back from `plan_ids`.
fn py_str(value: &OrderedJson) -> String {
    match value {
        OrderedJson::Str(text) => text.clone(),
        OrderedJson::Null => "None".to_owned(),
        OrderedJson::Bool(true) => "True".to_owned(),
        OrderedJson::Bool(false) => "False".to_owned(),
        OrderedJson::Int(i) => i.to_string(),
        OrderedJson::UInt(u) => u.to_string(),
        OrderedJson::BigInt(b) => b.clone(),
        OrderedJson::Float(f) => baskfy_core::py::float_repr(*f),
        other => pyjson::dumps(other).unwrap_or_default(),
    }
}

/// The `sw_config` columns `record_system_change` writes, as Python holds them.
#[derive(Debug, Clone, PartialEq)]
enum Field {
    Int(i64),
    Bool(bool),
    Dec(Option<Decimal>),
}

impl Field {
    /// `str(value)`.
    fn py_str(&self) -> Option<String> {
        match self {
            Self::Int(i) => Some(i.to_string()),
            Self::Bool(true) => Some("True".to_owned()),
            Self::Bool(false) => Some("False".to_owned()),
            Self::Dec(d) => d.map(|d| d.to_string()),
        }
    }

    /// `before != value` (numeric for Decimals: `Decimal("1.0") == Decimal("1.00")`).
    fn differs(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Int(a), Self::Int(b)) => a != b,
            (Self::Bool(a), Self::Bool(b)) => a != b,
            (Self::Dec(a), Self::Dec(b)) => a != b,
            _ => true,
        }
    }
}

/// `SwingConfigNotSeeded`.
struct NotSeeded;

/// `record_system_change(field, value, changed_by, now, note, audited)`: write a job-owned field
/// of `sw_config` when it changed, with its audit row (unless `audited=False`), `updated_by` and
/// the `onupdate` stamp. Nothing is written for an unchanged value.
#[allow(clippy::too_many_arguments)]
async fn record_system_change(
    conn: &mut PgConnection,
    user_id: i64,
    field: &str,
    value: &Field,
    now: DateTime<Utc>,
    note: Option<&str>,
    audited: bool,
) -> Result<Result<(), NotSeeded>, WorkerError> {
    let column = match field {
        "exposure_level"
        | "first_live_sessions_left"
        | "sleeve_peak_inr"
        | "drawdown_pct"
        | "drawdown_locked" => field,
        other => {
            return Err(WorkerError::python(
                "ValueError",
                format!("'{other}' is not a system-owned field"),
            ));
        }
    };
    let sql = format!(
        "SELECT {column}{} AS v FROM sw_config WHERE user_id = $1",
        match value {
            Field::Int(_) => "::int8",
            _ => "",
        }
    );
    let Some(row) = sqlx::query(&sql)
        .bind(user_id)
        .fetch_optional(&mut *conn)
        .await?
    else {
        return Ok(Err(NotSeeded));
    };
    let before = match value {
        Field::Int(_) => Field::Int(row.try_get("v")?),
        Field::Bool(_) => Field::Bool(row.try_get("v")?),
        Field::Dec(_) => Field::Dec(baskfy_db::row::opt_decimal(&row, "v")?),
    };
    if !before.differs(value) {
        return Ok(Ok(()));
    }
    let update = format!(
        "UPDATE sw_config SET {column} = $1, updated_by = $2, updated_at = now() \
         WHERE user_id = $3"
    );
    let query = sqlx::query(&update);
    let query = match value {
        Field::Int(i) => query.bind(
            i16::try_from(*i).map_err(|e| WorkerError::python("OverflowError", e.to_string()))?,
        ),
        Field::Bool(b) => query.bind(*b),
        Field::Dec(d) => query.bind(*d),
    };
    query
        .bind(LADDER_CHANGED_BY)
        .bind(user_id)
        .execute(&mut *conn)
        .await?;
    if audited {
        sqlx::query(
            "INSERT INTO sw_config_audit (user_id, key, old_value, new_value, changed_at, \
             changed_by, note) VALUES ($1, $2, $3, $4, $5, $6, $7)",
        )
        .bind(user_id)
        .bind(field)
        .bind(before.py_str())
        .bind(value.py_str().unwrap_or_else(|| "None".to_owned()))
        .bind(now)
        .bind(LADDER_CHANGED_BY)
        .bind(note)
        .execute(&mut *conn)
        .await?;
    }
    Ok(Ok(()))
}

/// `count_first_live_session`: the countdown, moved once per LIVE session that closes. Returns
/// `sw_config.first_live_sessions_left` after tonight.
async fn count_first_live_session(
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
    execution_enabled: bool,
    now: DateTime<Utc>,
) -> Result<i64, WorkerError> {
    let row: Option<(i16,)> =
        sqlx::query_as("SELECT first_live_sessions_left FROM sw_config WHERE user_id = $1")
            .bind(user_id)
            .fetch_optional(&mut *conn)
            .await?;
    let Some((left,)) = row else {
        return Ok(0);
    };
    let left = i64::from(left);
    if !execution_enabled || left <= 0 {
        return Ok(left);
    }
    let day: Option<(bool,)> = sqlx::query_as(
        "SELECT first_live_counted FROM sw_session WHERE user_id = $1 AND session_date = $2",
    )
    .bind(user_id)
    .bind(on)
    .fetch_optional(&mut *conn)
    .await?;
    match day {
        None | Some((true,)) => return Ok(left),
        Some((false,)) => {}
    }
    let note = format!(
        "{} closed LIVE: first live sessions {left} -> {}",
        iso(on),
        left - 1
    );
    // The row exists (read above), so `read_config` cannot refuse.
    let _ = record_system_change(
        conn,
        user_id,
        "first_live_sessions_left",
        &Field::Int(left - 1),
        now,
        Some(&note),
        true,
    )
    .await?;
    sqlx::query(
        "UPDATE sw_session SET first_live_counted = true, updated_at = now() \
         WHERE user_id = $1 AND session_date = $2",
    )
    .bind(user_id)
    .bind(on)
    .execute(&mut *conn)
    .await?;
    Ok(left - 1)
}

/// The day's `sw_market_daily` row, as the evening reads and rewrites it.
pub(crate) struct MarketRow {
    pub(crate) date: NaiveDate,
    pub(crate) gate: String,
    pub(crate) exposure_level: i64,
    pub(crate) detail: Option<OrderedJson>,
}

/// `_settled_rung(row, key)`: `detail.ladder.<key>` when it is an int (never a bool).
fn settled_rung(detail: Option<&OrderedJson>, key: &str) -> Option<i64> {
    let ladder = detail?.get("ladder")?;
    if !matches!(ladder, OrderedJson::Object(_)) {
        return None;
    }
    match ladder.get(key)? {
        OrderedJson::Int(v) => Some(*v),
        _ => None,
    }
}

/// `rung_in_force`: today's own settlement's `from`, else the previous session's `to`, else
/// `sw_config.exposure_level`.
async fn rung_in_force(
    conn: &mut PgConnection,
    user_id: i64,
    market: &MarketRow,
) -> Result<i64, WorkerError> {
    if let Some(own) = settled_rung(market.detail.as_ref(), "from") {
        return Ok(own);
    }
    let previous = sqlx::query(
        "SELECT detail FROM sw_market_daily WHERE user_id = $1 AND date < $2 \
         ORDER BY date DESC LIMIT 1",
    )
    .bind(user_id)
    .bind(market.date)
    .fetch_optional(&mut *conn)
    .await?;
    if let Some(row) = previous {
        let detail = baskfy_db::row::opt_jsonb(&row, "detail")?;
        if let Some(settled) = settled_rung(detail.as_ref(), "to") {
            return Ok(settled);
        }
    }
    let configured: Option<(i16,)> =
        sqlx::query_as("SELECT exposure_level FROM sw_config WHERE user_id = $1")
            .bind(user_id)
            .fetch_optional(&mut *conn)
            .await?;
    Ok(configured.map_or(0, |c| i64::from(c.0)))
}

/// `closed_r_multiples(on, count)`: the last `count` real closes' R on or before `on`, oldest
/// first.
async fn closed_r_multiples(
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
    n: i64,
) -> Result<Vec<Decimal>, WorkerError> {
    let rows = sqlx::query(
        "SELECT r_multiple FROM sw_position WHERE user_id = $1 AND state = 'CLOSED' \
         AND simulated IS false AND r_multiple IS NOT NULL AND closed_on IS NOT NULL \
         AND closed_on <= $2 ORDER BY closed_on DESC, id DESC LIMIT $3",
    )
    .bind(user_id)
    .bind(on)
    .bind(n)
    .fetch_all(&mut *conn)
    .await?;
    let mut values = Vec::with_capacity(rows.len());
    for row in &rows {
        if let Some(v) = baskfy_db::row::opt_decimal(row, "r_multiple")? {
            values.push(v);
        }
    }
    values.reverse();
    Ok(values)
}

/// What `settle_ladder` answered.
pub(crate) struct Settlement {
    rung_before: i64,
    tier: ExposureTier,
    closed_r: Vec<Decimal>,
    drawdown_pct: String,
}

fn gate_of(text: &str) -> Result<MarketGate, WorkerError> {
    match text {
        "GREEN" => Ok(MarketGate::Green),
        "AMBER" => Ok(MarketGate::Amber),
        "RED" => Ok(MarketGate::Red),
        other => Err(WorkerError::python(
            "ValueError",
            format!("'{other}' is not a valid MarketGate"),
        )),
    }
}

/// `settle_ladder`: `exposure_tier` over the rung in force, the last real closes, the day's gate
/// and the sleeve's drawdown; the answer written to `sw_config` (audited) and onto the day's
/// market row.
pub(crate) async fn settle_ladder(
    conn: &mut PgConnection,
    user_id: i64,
    market: &MarketRow,
    config: &SwingConfig,
    now: DateTime<Utc>,
) -> Result<Settlement, WorkerError> {
    let gate = gate_of(&market.gate)?;
    let rung_in = rung_in_force(conn, user_id, market).await?;
    let closes =
        closed_r_multiples(conn, user_id, market.date, config.market.lookback_trades).await?;
    let cfg = config_row(conn, user_id).await?;
    let drawdown = sleeve_drawdown(conn, user_id, market.date, cfg.as_ref()).await?;
    let tier = exposure_tier(
        rung_in,
        &closes,
        gate,
        &config.market,
        wide_float(&drawdown.pct)?,
        drawdown.was_locked,
    )
    .map_err(core_err)?;
    let pct_text = wide_str(&drawdown.pct);
    let nav_text = wide_str(&drawdown.nav);
    let peak_text = wide_str(&drawdown.peak);
    let gate_text = enum_value(&gate);
    let closes_text: Vec<String> = closes.iter().map(ToString::to_string).collect();
    let mut note = format!(
        "{} {gate_text}: rung {rung_in} -> {} on {} real closes [{}]",
        iso(market.date),
        tier.level,
        closes.len(),
        closes_text.join(", ")
    );
    if tier.drawdown_locked {
        note.push_str(&format!("; sleeve {pct_text}% below its peak, locked out"));
    }
    let seeded = record_system_change(
        conn,
        user_id,
        "exposure_level",
        &Field::Int(tier.level),
        now,
        Some(&note),
        true,
    )
    .await?;
    if seeded.is_ok() {
        let _ = record_system_change(
            conn,
            user_id,
            "sleeve_peak_inr",
            &Field::Dec(Some(wide_dec(&drawdown.peak)?)),
            now,
            None,
            false,
        )
        .await?;
        let _ = record_system_change(
            conn,
            user_id,
            "drawdown_pct",
            &Field::Dec(Some(wide_dec(&drawdown.pct)?)),
            now,
            None,
            false,
        )
        .await?;
        let lock_note = format!(
            "{}: NAV {nav_text} against peak {peak_text}, {pct_text}% below; lock-out at {}%, \
             release inside {}%",
            iso(market.date),
            baskfy_core::py::float_repr(config.market.max_drawdown_pct),
            baskfy_core::py::float_repr(config.market.resume_drawdown_pct)
        );
        let _ = record_system_change(
            conn,
            user_id,
            "drawdown_locked",
            &Field::Bool(tier.drawdown_locked),
            now,
            Some(&lock_note),
            true,
        )
        .await?;
    } else {
        log_warning(&format!(
            "no sw_config row for user {user_id}; the rung is kept on the market row only"
        ));
    }

    let mut detail = match &market.detail {
        Some(OrderedJson::Object(entries)) => entries.clone(),
        _ => Vec::new(),
    };
    pyjson::update(&mut detail, "closed_r_multiples", strings(closes_text));
    pyjson::update(&mut detail, "closed_trades_read", s("real"));
    pyjson::update(
        &mut detail,
        "ladder",
        object([
            ("from", int(rung_in)),
            ("to", int(tier.level)),
            ("settled_by", s(LADDER_CHANGED_BY)),
        ]),
    );
    pyjson::update(
        &mut detail,
        "drawdown",
        object([
            ("nav", s(nav_text)),
            ("peak", s(peak_text)),
            ("pct", s(pct_text.clone())),
            ("was_locked", OrderedJson::Bool(drawdown.was_locked)),
            ("locked", OrderedJson::Bool(tier.drawdown_locked)),
        ]),
    );
    let max_exposure = baskfy_core::py::decimal_from_float_str(tier.max_exposure_pct)
        .map_err(dec_err)?
        .round_dp_with_strategy(2, rust_decimal::RoundingStrategy::MidpointNearestEven);
    let body = pyjson::dumps(&OrderedJson::Object(detail))
        .map_err(|e| WorkerError::python("TypeError", e))?;
    sqlx::query(
        "UPDATE sw_market_daily SET exposure_level = $1, max_open_positions = $2, \
         max_exposure_pct = $3, new_entries_allowed = $4, drawdown_pct = $5, \
         drawdown_locked = $6, detail = CAST($7 AS JSONB) WHERE user_id = $8 AND date = $9",
    )
    .bind(i16::try_from(tier.level).map_err(|e| overflow(&e.to_string()))?)
    .bind(i16::try_from(tier.max_open_positions).map_err(|e| overflow(&e.to_string()))?)
    .bind(max_exposure)
    .bind(tier.new_entries_allowed)
    .bind(wide_dec(&drawdown.pct)?)
    .bind(tier.drawdown_locked)
    .bind(body)
    .bind(user_id)
    .bind(market.date)
    .execute(&mut *conn)
    .await?;
    Ok(Settlement {
        rung_before: rung_in,
        tier,
        closed_r: closes,
        drawdown_pct: pct_text,
    })
}

/// `_candidate(line)`.
fn candidate(line: &PlanLine) -> SwingCandidate {
    SwingCandidate {
        symbol: line.symbol.clone(),
        setup: line
            .setup
            .map_or_else(|| enum_value(&line.kind), |s| s.as_str().to_owned()),
        trigger: line.trigger.map_or_else(|| "—".to_owned(), money),
        stop: line.stop.map_or_else(|| "—".to_owned(), money),
        note: line.note.clone(),
    }
}

/// `send_eod_email`: the evening email to the user's address; `None` when there is no address.
/// Never raises: an undelivered message is logged by the mailer, and the address is still
/// returned (Python's `deliver` returns `False` rather than raising).
async fn send_eod_email(
    conn: &mut PgConnection,
    user_id: i64,
    report: &EodReport,
    plan: &SwingPlan,
    settings: &Settings,
) -> Result<Option<String>, WorkerError> {
    let address: Option<(String,)> = sqlx::query_as("SELECT email FROM app_user WHERE id = $1")
        .bind(user_id)
        .fetch_optional(&mut *conn)
        .await?;
    let Some((address,)) = address else {
        return Ok(None);
    };
    let watched = list_watch(conn, user_id).await?;
    let row_candidate = |row: &super::watch::WatchRow| SwingCandidate {
        symbol: row.symbol.clone(),
        setup: row.setup.clone(),
        trigger: row.trigger.map_or_else(|| "—".to_owned(), money),
        stop: row.stop_ref.map_or_else(|| "—".to_owned(), money),
        note: row.catalyst.clone().unwrap_or_default(),
    };
    let flags: Vec<SwingCandidate> = watched
        .iter()
        .filter(|r| r.setup == Setup::Flag.as_str())
        .take(MAX_FLAGS_IN_EMAIL)
        .map(row_candidate)
        .collect();
    let eps: Vec<SwingCandidate> = watched
        .iter()
        .filter(|r| r.setup == Setup::Ep.as_str())
        .map(row_candidate)
        .collect();
    let digest = SwingDigest {
        as_of: report.trade_date,
        gate: report.gate.clone(),
        exposure_level: report.exposure_level,
        max_open_positions: 0,
        naked: report.naked_positions.clone(),
        exits: plan
            .lines
            .iter()
            .filter(|l| matches!(l.kind, LineKind::SellAtOpen | LineKind::RaiseGttStop))
            .map(candidate)
            .collect(),
        entries: plan
            .lines
            .iter()
            .filter(|l| matches!(l.kind, LineKind::BuyOnTrigger | LineKind::PendingRange))
            .map(candidate)
            .collect(),
        skips: plan
            .skipped
            .iter()
            .map(|k| {
                (
                    k.symbol.clone(),
                    format!("{} {}", enum_value(&k.reason), k.detail)
                        .trim()
                        .to_owned(),
                )
            })
            .collect(),
        flags,
        eps,
        sessions_logged: report.sessions_logged,
        sessions_required: PAPER_SESSIONS_REQUIRED,
    };
    let message = swing_eod(
        &address,
        &digest,
        &format!("{}/swing", settings.public_api_base_url),
    );
    let _ = Mailer::from_settings(settings).deliver(&message).await;
    Ok(Some(address))
}

/// `run_swing_eod`: manage the book, settle the ladder, count the session, plan tomorrow, store
/// it, email it. Returns rather than raises when the detectors have not written the day's market
/// row.
pub(super) async fn run_swing_eod(
    conn: &mut PgConnection,
    outcome: &mut StepOutcome,
    trade_date: NaiveDate,
    user_id: i64,
    execution_enabled: bool,
    now: DateTime<Utc>,
    settings: &Settings,
) -> Result<EodReport, WorkerError> {
    let cfg = config_row(conn, user_id).await?;
    let capital = cfg.as_ref().map(|c| c.sleeve_capital_inr);
    let config = swing_config(cfg.as_ref());
    let market = sqlx::query(
        "SELECT date, gate, exposure_level, detail FROM sw_market_daily \
         WHERE user_id = $1 AND date = $2",
    )
    .bind(user_id)
    .bind(trade_date)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(market) = market else {
        outcome.status = StepStatus::Skipped;
        outcome.note("trade_date", s(iso(trade_date)));
        outcome.note(
            "skipped_reason",
            s("no sw_market_daily row; the detectors have not run for this date"),
        );
        return Ok(EodReport::new(trade_date, "UNKNOWN".to_owned(), 0));
    };
    let level: i16 = market.try_get("exposure_level")?;
    let market = MarketRow {
        date: market.try_get("date")?,
        gate: market.try_get("gate")?,
        exposure_level: i64::from(level),
        detail: baskfy_db::row::opt_jsonb(&market, "detail")?,
    };
    let gate = gate_of(&market.gate)?;
    let mut report = EodReport::new(trade_date, enum_value(&gate), market.exposure_level);

    let watched = auto_watch(conn, user_id, trade_date, &config).await?;
    report.watch_added = watched.added;
    report.watch_expired = watched.expired;

    let (exits, naked, managed) = manage_open_positions(conn, user_id, trade_date, &config).await?;
    report.positions_managed = managed;
    report.naked_positions = naked;
    report.exit_lines = count(exits.len());

    let settled = settle_ladder(conn, user_id, &market, &config, now).await?;
    let tier = settled.tier;
    report.rung_before = settled.rung_before;
    report.exposure_level = tier.level;
    report.closed_r = settled.closed_r.iter().map(ToString::to_string).collect();
    report.drawdown_pct = settled.drawdown_pct;
    report.drawdown_locked = tier.drawdown_locked;

    record_session(conn, user_id, trade_date, None, execution_enabled).await?;
    let left = count_first_live_session(conn, user_id, trade_date, execution_enabled, now).await?;
    let multiplier =
        first_live_multiplier(left, execution_enabled, &config.sizing).map_err(core_err)?;
    report.first_live_sessions_left = left;
    report.risk_multiplier = multiplier.to_string();
    report.risk_pct_in_force = risk_pct_in_force(&config, multiplier)?;

    let (items, mut instrument_ids) = watch_items(conn, user_id, trade_date).await?;
    report.watching = count(items.len());
    let account = sleeve_account(conn, user_id, capital).await?;
    let entries = build_entries(
        trade_date, &items, &account, gate, &tier, &config, 0, multiplier,
    )
    .map_err(core_err)?;
    report.entry_lines = count(
        entries
            .lines
            .iter()
            .filter(|l| l.kind == LineKind::BuyOnTrigger)
            .count(),
    );
    report.pending_lines = count(
        entries
            .lines
            .iter()
            .filter(|l| l.kind == LineKind::PendingRange)
            .count(),
    );
    report.skips = count(entries.skipped.len());

    let plan = assemble(
        trade_date,
        gate,
        tier,
        entries.lines,
        exits,
        entries.skipped,
    )?;
    instrument_ids.extend(held_instrument_ids(conn, user_id).await?);
    let plan_id = store_plan(conn, &plan, user_id, "EOD_PREVIEW", &instrument_ids, now).await?;
    report.plan_id = Some(plan_id.clone());
    report.sessions_logged =
        record_session(conn, user_id, trade_date, Some(&plan_id), execution_enabled).await?;

    let sent = send_eod_email(conn, user_id, &report, &plan, settings).await?;

    outcome.rows_out = count(plan.lines.len());
    if let OrderedJson::Object(entries) = report.as_detail() {
        for (k, v) in entries {
            outcome.note(&k, v);
        }
    }
    outcome.note("email_sent_to", pyjson::opt_s(sent));
    Ok(report)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn d(text: &str) -> Decimal {
        text.parse().unwrap()
    }

    #[test]
    fn the_header_speaks_only_while_the_countdown_runs() {
        assert_eq!(first_live_header(0, "0.250"), "");
        assert_eq!(
            first_live_header(3, "0.250"),
            "first live sessions: 3 left · risk 0.250%"
        );
    }

    #[test]
    fn risk_in_force_is_the_setting_times_the_multiplier_at_three_places() {
        let config = SwingConfig::default();
        assert_eq!(risk_pct_in_force(&config, Decimal::ONE).unwrap(), "0.500");
        assert_eq!(risk_pct_in_force(&config, d("0.5")).unwrap(), "0.250");
    }

    #[test]
    fn a_mean_needs_a_full_window() {
        let closes: Vec<Decimal> = (1..=20).map(Decimal::from).collect();
        assert_eq!(mean(&closes[..9], 10).unwrap(), None);
        assert_eq!(mean(&closes, 10).unwrap(), Some(d("15.5")));
        assert_eq!(mean(&closes, 20).unwrap(), Some(d("10.5")));
    }

    #[test]
    fn a_settled_rung_is_an_int_never_a_bool() {
        let detail = object([(
            "ladder",
            object([("from", int(2)), ("to", OrderedJson::Bool(true))]),
        )]);
        assert_eq!(settled_rung(Some(&detail), "from"), Some(2));
        assert_eq!(settled_rung(Some(&detail), "to"), None);
        assert_eq!(settled_rung(None, "from"), None);
        assert_eq!(
            settled_rung(Some(&object([("ladder", int(1))])), "from"),
            None
        );
    }

    #[test]
    fn a_config_field_compares_as_python_and_prints_as_str() {
        assert!(!Field::Dec(Some(d("1.0"))).differs(&Field::Dec(Some(d("1.00")))));
        assert!(Field::Dec(None).differs(&Field::Dec(Some(d("1")))));
        assert_eq!(Field::Bool(true).py_str().as_deref(), Some("True"));
        assert_eq!(Field::Int(2).py_str().as_deref(), Some("2"));
        assert_eq!(
            Field::Dec(Some(d("2500000.00"))).py_str().as_deref(),
            Some("2500000.00")
        );
    }

    #[test]
    fn a_line_reads_in_the_email_with_its_setup_or_its_kind() {
        let line = PlanLine {
            kind: LineKind::RaiseGttStop,
            symbol: "ABC".to_owned(),
            setup: None,
            quantity: 0,
            trigger: None,
            stop: Some(d("1234.5")),
            risk_inr: Decimal::ZERO,
            position_value: Decimal::ZERO,
            trail: None,
            note: "BREAKEVEN_AT_R".to_owned(),
            position_id: Some(1),
        };
        let c = candidate(&line);
        assert_eq!(c.setup, "RAISE_GTT_STOP");
        assert_eq!(c.trigger, "—");
        assert_eq!(c.stop, "1,234.50");
    }
}
