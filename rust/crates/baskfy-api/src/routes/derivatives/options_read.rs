//! Ports `baskfy_api.options_read` (OP5): the options tab's reads, and the response models of
//! `routers/options.py` in pydantic field order. Reads what the worker and the desk wrote —
//! `op_scan`, `op_expiry`, `op_event_day`, `op_chain_snapshot`, `op_index_minute`,
//! `op_session`, `op_position`, `op_journal`, `op_backtest_run` — and computes no signal.
//!
//! The clock (root CLAUDE.md's options row): scan states and the chain are stamped with the
//! collector's latest minute while the session is open and `stale` beyond two minutes; outside
//! the session they are the last scanned session's final minute. Nothing here reaches Kite.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_db::{Db, OrderedJson};
use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveDate, NaiveTime, Utc};
use rust_decimal::Decimal;
use serde::{Serialize, Serializer};
use sqlx::postgres::PgRow;

use super::options_core::{self as core, DISPLAY_ORDER, JournalRow, PAPER_PERIOD};
use super::shared::{col, db_err, ist, ist_midnight, ser_opt_wide, ser_wide, ser_wides};
use crate::problems::{Problem, internal_error};
use crate::wire::ser;

const UNDERLYING: &str = "NIFTY";
/// `NIFTY_50` / `INDIA_VIX` and their `index_snapshot_daily` slugs.
pub(crate) const NIFTY_50: (&str, &str) = ("NIFTY 50", "nifty-50");
/// `INDIA_VIX`.
pub(crate) const INDIA_VIX: (&str, &str) = ("INDIA VIX", "india-vix");
const HEADER_EXPIRIES: usize = 6;
const CHAIN_STRIKES_EACH_SIDE: usize = 10;
const OVER_STATES: [&str; 3] = ["CLOSED", "LAPSED", "SKIPPED"];
const EMPTY_COLLECTOR_OFF: &str = "collector_off";
const EMPTY_SCAN_OFF: &str = "scan_off";
const EMPTY_NO_SCAN_YET: &str = "no_scan_yet_today";
const EMPTY_NEVER: &str = "never_scanned";
const USER_SOURCE: &str = "USER";

/// A `datetime.time` as pydantic prints it: `HH:MM:SS`, `.ffffff` when the microsecond is set.
fn ser_time<S: Serializer>(t: &NaiveTime, s: S) -> Result<S::Ok, S::Error> {
    s.serialize_str(&py_time(*t))
}

/// `time.isoformat()`.
pub(crate) fn py_time(t: NaiveTime) -> String {
    let base = t.format("%H:%M:%S").to_string();
    let micros = chrono::Timelike::nanosecond(&t) / 1000 % 1_000_000;
    if micros == 0 {
        base
    } else {
        format!("{base}.{micros:06}")
    }
}

// --- response models -------------------------------------------------------------------------

/// `OptionsExpiryOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct ExpiryOut {
    #[serde(serialize_with = "ser::date")]
    expiry_date: NaiveDate,
    kind: String,
    lot_size: i64,
    event_day: bool,
    event_reason: Option<String>,
}

/// `OptionsRoleOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct RoleOut {
    sleeve: &'static str,
    today: bool,
    reason: &'static str,
    #[serde(serialize_with = "ser::opt_date")]
    next_date: Option<NaiveDate>,
}

/// `OptionsIndexOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct IndexOut {
    symbol: &'static str,
    #[serde(serialize_with = "ser::opt_decimal")]
    level: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_datetime")]
    at: Option<DateTime<Utc>>,
    #[serde(serialize_with = "ser::opt_date")]
    close_of: Option<NaiveDate>,
}

/// `OptionsPauseOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct PauseOut {
    scope: String,
    #[serde(serialize_with = "ser::opt_date")]
    paused_until: Option<NaiveDate>,
    paused_reason: Option<String>,
}

/// `OptionsScanOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct ScanOut {
    sleeve: String,
    #[serde(serialize_with = "ser::date")]
    trade_date: NaiveDate,
    #[serde(serialize_with = "ser::datetime")]
    ts: DateTime<Utc>,
    state: String,
    reasons: Vec<String>,
    numbers: OrderedJson,
    candidates: Vec<OrderedJson>,
    #[serde(serialize_with = "ser::opt_datetime")]
    as_of_minute: Option<DateTime<Utc>>,
    stale: bool,
}

/// `OptionsPositionOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct PositionOut {
    session_id: i64,
    sleeve: String,
    #[serde(serialize_with = "ser::date")]
    trade_date: NaiveDate,
    lots: i64,
    #[serde(serialize_with = "ser::decimal")]
    entry_points: Decimal,
    #[serde(serialize_with = "ser::decimal")]
    entry_inr: Decimal,
    #[serde(serialize_with = "ser::datetime")]
    opened_at: DateTime<Utc>,
    #[serde(serialize_with = "ser::datetime")]
    hard_exit_at: DateTime<Utc>,
    #[serde(serialize_with = "ser::opt_decimal")]
    last_mark_points: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_datetime")]
    last_mark_at: Option<DateTime<Utc>>,
    minutes_to_hard_exit: Option<i64>,
    simulated: bool,
}

/// `OptionsClosedOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct ClosedOut {
    session_id: i64,
    sleeve: String,
    #[serde(serialize_with = "ser::date")]
    trade_date: NaiveDate,
    structure: String,
    #[serde(serialize_with = "ser::decimal")]
    net_pnl_inr: Decimal,
    #[serde(serialize_with = "ser::decimal")]
    r_multiple: Decimal,
    closed_reason: String,
    minutes_held: i64,
    simulated: bool,
    sizing_mode: String,
}

/// `OptionsWeekROut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct WeekROut {
    sleeve: String,
    simulated: bool,
    #[serde(serialize_with = "ser_wide")]
    r: WideDecimal,
    trades: i64,
}

/// `OptionsGateOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct GateOut {
    pub(crate) group: &'static str,
    pub(crate) execution_enabled: bool,
    pub(crate) mode: &'static str,
}

/// `OptionsTodayOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct TodayOut {
    #[serde(serialize_with = "ser::date")]
    today: NaiveDate,
    session_day: bool,
    market_open: bool,
    #[serde(serialize_with = "ser::opt_date")]
    scan_date: Option<NaiveDate>,
    #[serde(serialize_with = "ser::opt_datetime")]
    as_of_minute: Option<DateTime<Utc>>,
    live: bool,
    stale: bool,
    empty_reason: Option<&'static str>,
    collect_enabled: bool,
    scan_enabled: bool,
    pub(crate) gates: Vec<GateOut>,
    roles: Vec<RoleOut>,
    expiries: Vec<ExpiryOut>,
    pauses: Vec<PauseOut>,
    nifty: IndexOut,
    vix: IndexOut,
    scans: Vec<ScanOut>,
    positions: Vec<PositionOut>,
    closed_today: Vec<ClosedOut>,
    week_r: Vec<WeekROut>,
}

/// `OptionsScanHistoryOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct ScanHistoryOut {
    pub(crate) sleeve: &'static str,
    #[serde(serialize_with = "ser::opt_date")]
    pub(crate) trade_date: Option<NaiveDate>,
    pub(crate) rows: Vec<ScanOut>,
}

/// `OptionsChainRowOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct ChainRowOut {
    #[serde(serialize_with = "ser::decimal")]
    strike: Decimal,
    option_type: String,
    #[serde(serialize_with = "ser::opt_decimal")]
    bid: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    ask: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    last: Option<Decimal>,
    oi: Option<i64>,
    oi_change: Option<i64>,
    #[serde(serialize_with = "ser::opt_decimal")]
    iv: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    delta: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    gamma: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    theta: Option<Decimal>,
}

/// `OptionsChainExpiryOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct ChainExpiryOut {
    #[serde(serialize_with = "ser::date")]
    expiry: NaiveDate,
    #[serde(serialize_with = "ser::datetime")]
    ts: DateTime<Utc>,
    #[serde(serialize_with = "ser::opt_decimal")]
    spot: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    forward: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    atm_strike: Option<Decimal>,
    #[serde(serialize_with = "ser_opt_wide")]
    atm_iv: Option<WideDecimal>,
    #[serde(serialize_with = "ser_opt_wide")]
    pcr_oi: Option<WideDecimal>,
    rows: Vec<ChainRowOut>,
}

/// `OptionsChainOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct ChainOut {
    pub(crate) expiries: Vec<ChainExpiryOut>,
}

/// `OptionsPositionsOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct PositionsOut {
    pub(crate) positions: Vec<PositionOut>,
    pub(crate) closed_today: Vec<ClosedOut>,
}

/// `OptionsSessionOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct SessionOut {
    id: i64,
    sleeve: String,
    #[serde(serialize_with = "ser::date")]
    trade_date: NaiveDate,
    #[serde(serialize_with = "ser::opt_date")]
    expiry_used: Option<NaiveDate>,
    mode: String,
    state: String,
    verdict: Option<String>,
    skip_reasons: Vec<String>,
    closed_reason: Option<String>,
    #[serde(serialize_with = "ser::opt_decimal")]
    pnl_inr: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    pnl_r: Option<Decimal>,
    slot_holder: Option<String>,
}

/// `OptionsSessionsOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct SessionsOut {
    pub(crate) sessions: Vec<SessionOut>,
}

/// `OptionsBucketOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct BucketOut {
    label: String,
    count: i64,
    #[serde(serialize_with = "ser_wide")]
    mean_r: WideDecimal,
}

/// `OptionsSummaryOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct SummaryOut {
    sleeve: String,
    simulated: bool,
    sizing_mode: String,
    count: i64,
    traded: i64,
    #[serde(serialize_with = "ser_opt_wide")]
    win_rate: Option<WideDecimal>,
    #[serde(serialize_with = "ser_opt_wide")]
    mean_r: Option<WideDecimal>,
    #[serde(serialize_with = "ser_opt_wide")]
    expectancy_inr: Option<WideDecimal>,
    #[serde(serialize_with = "ser_opt_wide")]
    expectancy_r: Option<WideDecimal>,
    #[serde(serialize_with = "ser_opt_wide")]
    worst_r: Option<WideDecimal>,
    #[serde(serialize_with = "ser_wide")]
    max_drawdown_r: WideDecimal,
    #[serde(serialize_with = "ser_wide")]
    max_drawdown_inr: WideDecimal,
    #[serde(serialize_with = "ser_wides")]
    r_values: Vec<WideDecimal>,
    by_closed_reason: Vec<BucketOut>,
}

/// `OptionsProgressOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct ProgressOut {
    group: &'static str,
    sessions_needed: i64,
    traded_needed: i64,
    sessions_done: i64,
    traded: i64,
}

/// `OptionsJournalOut`. `skips_by_reason` keeps the order the sessions were read in (Python's
/// `defaultdict(dict)`), so it is an [`OrderedJson`] object.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct JournalOut {
    summaries: Vec<SummaryOut>,
    skips_by_reason: OrderedJson,
    progress: Vec<ProgressOut>,
    min_sessions: OrderedJson,
    recent: Vec<ClosedOut>,
}

/// `OptionsBacktestRunOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct BacktestRunOut {
    id: i64,
    sleeve: String,
    tier: i64,
    #[serde(serialize_with = "ser::date")]
    date_from: NaiveDate,
    #[serde(serialize_with = "ser::date")]
    date_to: NaiveDate,
    sessions: i64,
    signals: i64,
    traded: i64,
    skipped_by_reason: OrderedJson,
    #[serde(serialize_with = "ser::opt_decimal")]
    win_rate: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    expectancy_r: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    net_pnl_inr: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    max_drawdown_r: Option<Decimal>,
    caveats: String,
    #[serde(serialize_with = "ser::datetime")]
    ran_at: DateTime<Utc>,
}

/// `OptionsBacktestOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct BacktestOut {
    pub(crate) runs: Vec<BacktestRunOut>,
    pub(crate) reason: Option<&'static str>,
}

/// `OptionsEventDayOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct EventDayOut {
    #[serde(serialize_with = "ser::date")]
    date: NaiveDate,
    reason: String,
    source: String,
    source_url: Option<String>,
    note: Option<String>,
    removable: bool,
}

/// `OptionsCalendarOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct CalendarOut {
    year: i64,
    expiries: Vec<ExpiryOut>,
    event_days: Vec<EventDayOut>,
}

/// `OptionsBookOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct BookOut {
    underlying: String,
    #[serde(serialize_with = "ser::decimal")]
    account_inr: Decimal,
    #[serde(serialize_with = "ser::decimal")]
    margin_pool_inr: Decimal,
    #[serde(serialize_with = "ser::decimal")]
    daily_loss_limit_inr: Decimal,
    #[serde(serialize_with = "ser::decimal")]
    monthly_pause_inr: Decimal,
    #[serde(serialize_with = "ser::opt_date")]
    paused_until: Option<NaiveDate>,
    paused_reason: Option<String>,
}

/// `OptionsSleeveConfigOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct SleeveConfigOut {
    sleeve: String,
    #[serde(serialize_with = "ser::decimal")]
    sleeve_capital_inr: Decimal,
    #[serde(serialize_with = "ser::decimal")]
    risk_per_trade_pct: Decimal,
    max_lots: i64,
    paper_enabled: bool,
    #[serde(serialize_with = "ser_time")]
    hard_exit_time: NaiveTime,
    #[serde(serialize_with = "ser::opt_date")]
    paused_until: Option<NaiveDate>,
    paused_reason: Option<String>,
}

/// `OptionsCeilingsOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct CeilingsOut {
    #[serde(serialize_with = "ser::decimal")]
    pub(crate) risk_per_trade_inr_max: Decimal,
    #[serde(serialize_with = "ser::decimal")]
    pub(crate) risk_pct_max: Decimal,
    pub(crate) max_lots_max: i64,
    #[serde(serialize_with = "ser::decimal")]
    pub(crate) book_daily_loss_inr_max: Decimal,
    #[serde(serialize_with = "ser::decimal")]
    pub(crate) book_monthly_loss_inr_max: Decimal,
    #[serde(serialize_with = "ser_time")]
    pub(crate) hard_exit_latest: NaiveTime,
}

/// `OptionsThresholdOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct ThresholdOut {
    section: &'static str,
    key: &'static str,
    value: &'static str,
    anchor: &'static str,
}

/// `OptionsConfigOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct ConfigOut {
    pub(crate) seeded: bool,
    pub(crate) book: Option<BookOut>,
    pub(crate) sleeves: Vec<SleeveConfigOut>,
    pub(crate) ceilings: CeilingsOut,
    pub(crate) gates: Vec<GateOut>,
    pub(crate) thresholds: Vec<ThresholdOut>,
    pub(crate) audit: Vec<super::fno_read::AuditOut>,
}

/// `thresholds()` as the response's list.
pub(crate) fn thresholds() -> Vec<ThresholdOut> {
    core::THRESHOLDS
        .iter()
        .map(|&(section, key, value, anchor)| ThresholdOut {
            section,
            key,
            value,
            anchor,
        })
        .collect()
}

// --- reads -----------------------------------------------------------------------------------

fn wq(d: &WideDecimal, places: i64) -> Result<WideDecimal, Problem> {
    d.quantize(-places, Rounding::HalfEven)
        .map_err(|_| internal_error())
}

/// `is_session_day(session, day)`: the NSE calendar's answer; a day it does not carry is a
/// session.
pub(crate) async fn is_session_day(db: &Db, day: NaiveDate) -> Result<bool, Problem> {
    let known: Option<bool> = sqlx::query_scalar(
        "SELECT is_trading_day FROM trading_day WHERE exchange_id = 1 AND date = $1",
    )
    .bind(day)
    .fetch_optional(db.pool())
    .await
    .map_err(db_err)?;
    Ok(known.unwrap_or(true))
}

/// One `op_expiry` row the tab reads.
#[derive(Debug, Clone)]
struct ExpiryRow {
    expiry_date: NaiveDate,
    kind: String,
    lot_size: i64,
}

/// `_expiry_rows(session, start=, end=)`: NIFTY's live expiries from `start`, ascending.
async fn expiry_rows(
    db: &Db,
    start: NaiveDate,
    end: Option<NaiveDate>,
) -> Result<Vec<ExpiryRow>, Problem> {
    let rows = sqlx::query(
        "SELECT expiry_date, kind, lot_size::int8 AS lot_size, detail FROM op_expiry \
         WHERE underlying = $1 AND expiry_date >= $2 AND ($3::date IS NULL OR expiry_date <= $3) \
         ORDER BY expiry_date",
    )
    .bind(UNDERLYING)
    .bind(start)
    .bind(end)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    let mut out = Vec::new();
    for r in &rows {
        let withdrawn = matches!(
            col::opt_json(r, "detail")?,
            Some(OrderedJson::Object(ref o)) if o.iter().any(|(k, _)| k == "withdrawn_on")
        );
        if !withdrawn {
            out.push(ExpiryRow {
                expiry_date: col::date(r, "expiry_date")?,
                kind: col::text(r, "kind")?,
                lot_size: col::i64(r, "lot_size")?,
            });
        }
    }
    Ok(out)
}

/// One `op_event_day` row.
#[derive(Debug, Clone)]
struct EventRow {
    reason: String,
    source: String,
    source_url: Option<String>,
    note: Option<String>,
}

/// `_event_days(session, user_id, start=)`.
async fn event_days(
    db: &Db,
    user_id: i64,
    start: Option<NaiveDate>,
) -> Result<BTreeMap<NaiveDate, EventRow>, Problem> {
    let rows = sqlx::query(
        "SELECT date, reason, source, source_url, note FROM op_event_day \
         WHERE user_id = $1 AND ($2::date IS NULL OR date >= $2)",
    )
    .bind(user_id)
    .bind(start)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    let mut out = BTreeMap::new();
    for r in &rows {
        out.insert(
            col::date(r, "date")?,
            EventRow {
                reason: col::text(r, "reason")?,
                source: col::text(r, "source")?,
                source_url: col::opt_text(r, "source_url")?,
                note: col::opt_text(r, "note")?,
            },
        );
    }
    Ok(out)
}

fn expiry_out(row: &ExpiryRow, events: &BTreeMap<NaiveDate, EventRow>) -> ExpiryOut {
    let event = events.get(&row.expiry_date);
    ExpiryOut {
        expiry_date: row.expiry_date,
        kind: row.kind.clone(),
        lot_size: row.lot_size,
        event_day: event.is_some(),
        event_reason: event.map(|e| e.reason.clone()),
    }
}

/// `roles_for(day, session_day=, expiries=, event_days=)`.
fn roles_for(
    day: NaiveDate,
    session_day: bool,
    expiries: &[ExpiryRow],
    events: &BTreeMap<NaiveDate, EventRow>,
) -> Vec<RoleOut> {
    let listed: BTreeSet<NaiveDate> = expiries.iter().map(|e| e.expiry_date).collect();
    let event_set: BTreeSet<NaiveDate> = events.keys().copied().collect();
    DISPLAY_ORDER
        .iter()
        .map(|&sleeve| {
            let (today, reason) = core::role(day, sleeve, &listed, &event_set, session_day);
            let next_date = if sleeve == "O2" {
                None
            } else {
                core::next_session(day, sleeve, &listed, &event_set)
            };
            RoleOut {
                sleeve,
                today,
                reason,
                next_date,
            }
        })
        .collect()
}

/// `index_level(session, symbol, slug, day)`: the collector's last minute bar of `day`, else the
/// last daily close on or before it.
async fn index_level(
    db: &Db,
    (symbol, slug): (&'static str, &'static str),
    day: NaiveDate,
) -> Result<IndexOut, Problem> {
    let minute = sqlx::query(
        "SELECT m.close, m.ts FROM op_index_minute m JOIN instrument i ON i.id = m.instrument_id \
         WHERE i.symbol = $1 AND m.ts >= $2 ORDER BY m.ts DESC LIMIT 1",
    )
    .bind(symbol)
    .bind(ist_midnight(day))
    .fetch_optional(db.pool())
    .await
    .map_err(db_err)?;
    if let Some(m) = minute {
        return Ok(IndexOut {
            symbol,
            level: col::opt_dec(&m, "close")?,
            at: Some(col::ts(&m, "ts")?),
            close_of: None,
        });
    }
    let daily = sqlx::query(
        "SELECT s.level, s.date FROM index_snapshot_daily s JOIN index_def d ON d.id = s.index_id \
         WHERE d.slug = $1 AND s.date <= $2 AND s.level IS NOT NULL ORDER BY s.date DESC LIMIT 1",
    )
    .bind(slug)
    .bind(day)
    .fetch_optional(db.pool())
    .await
    .map_err(db_err)?;
    match daily {
        Some(r) => match col::opt_dec(&r, "level")? {
            Some(level) => Ok(IndexOut {
                symbol,
                level: Some(level),
                at: None,
                close_of: Some(col::date(&r, "date")?),
            }),
            None => Ok(IndexOut {
                symbol,
                level: None,
                at: None,
                close_of: None,
            }),
        },
        None => Ok(IndexOut {
            symbol,
            level: None,
            at: None,
            close_of: None,
        }),
    }
}

/// `latest_scan_date(session, user_id, on_or_before)`.
pub(crate) async fn latest_scan_date(
    db: &Db,
    user_id: i64,
    on_or_before: NaiveDate,
) -> Result<Option<NaiveDate>, Problem> {
    sqlx::query_scalar(
        "SELECT max(trade_date) FROM op_scan WHERE user_id = $1 AND trade_date <= $2",
    )
    .bind(user_id)
    .bind(on_or_before)
    .fetch_one(db.pool())
    .await
    .map_err(db_err)
}

const SCAN_COLUMNS: &str = "s.sleeve::text AS sleeve, s.trade_date, s.ts, s.state, \
     s.reasons::text[] AS reasons, s.numbers, s.candidates, s.as_of_minute, s.stale";

fn scan_view(r: &PgRow) -> Result<ScanOut, Problem> {
    let candidates = match col::opt_json(r, "candidates")? {
        Some(OrderedJson::Array(items)) => items,
        _ => Vec::new(),
    };
    Ok(ScanOut {
        sleeve: col::text(r, "sleeve")?,
        trade_date: col::date(r, "trade_date")?,
        ts: col::ts(r, "ts")?,
        state: col::text(r, "state")?,
        reasons: col::texts(r, "reasons")?,
        numbers: col::object(r, "numbers")?,
        candidates,
        as_of_minute: col::opt_ts(r, "as_of_minute")?,
        stale: col::boolean(r, "stale")?,
    })
}

/// `latest_scans(session, user_id, day)`: the newest row per sleeve, in display order.
async fn latest_scans(db: &Db, user_id: i64, day: NaiveDate) -> Result<Vec<ScanOut>, Problem> {
    let rows = sqlx::query(&format!(
        "SELECT {SCAN_COLUMNS} FROM op_scan s JOIN (SELECT sleeve, max(ts) AS ts FROM op_scan \
         WHERE user_id = $1 AND trade_date = $2 GROUP BY sleeve) newest \
         ON s.sleeve = newest.sleeve AND s.ts = newest.ts \
         WHERE s.user_id = $1 AND s.trade_date = $2"
    ))
    .bind(user_id)
    .bind(day)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    let mut by_sleeve: BTreeMap<String, ScanOut> = BTreeMap::new();
    for r in &rows {
        let view = scan_view(r)?;
        by_sleeve.insert(view.sleeve.clone(), view);
    }
    Ok(DISPLAY_ORDER
        .iter()
        .filter_map(|s| by_sleeve.remove(*s))
        .collect())
}

/// `scan_history(session, user_id, sleeve, day)`: one sleeve's rows through a session.
pub(crate) async fn scan_history(
    db: &Db,
    user_id: i64,
    sleeve: &str,
    day: NaiveDate,
) -> Result<Vec<ScanOut>, Problem> {
    let rows = sqlx::query(&format!(
        "SELECT {SCAN_COLUMNS} FROM op_scan s WHERE s.user_id = $1 AND s.sleeve::text = $2 \
         AND s.trade_date = $3 ORDER BY s.ts"
    ))
    .bind(user_id)
    .bind(sleeve)
    .bind(day)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    rows.iter().map(scan_view).collect()
}

/// `pauses(session, user_id)`: the book's, then each sleeve's (by sleeve).
async fn pauses(db: &Db, user_id: i64) -> Result<Vec<PauseOut>, Problem> {
    let mut out = Vec::new();
    let book =
        sqlx::query("SELECT paused_until, paused_reason FROM op_book_config WHERE user_id = $1")
            .bind(user_id)
            .fetch_optional(db.pool())
            .await
            .map_err(db_err)?;
    if let Some(b) = book {
        out.push(PauseOut {
            scope: "BOOK".to_owned(),
            paused_until: col::opt_date(&b, "paused_until")?,
            paused_reason: col::opt_text(&b, "paused_reason")?,
        });
    }
    let sleeves = sqlx::query(
        "SELECT sleeve, paused_until, paused_reason FROM op_sleeve_config WHERE user_id = $1 \
         ORDER BY sleeve",
    )
    .bind(user_id)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    for s in &sleeves {
        out.push(PauseOut {
            scope: col::text(s, "sleeve")?,
            paused_until: col::opt_date(s, "paused_until")?,
            paused_reason: col::opt_text(s, "paused_reason")?,
        });
    }
    Ok(out)
}

/// `open_positions(session, user_id, now)`.
pub(crate) async fn open_positions(
    db: &Db,
    user_id: i64,
    now: DateTime<FixedOffset>,
) -> Result<Vec<PositionOut>, Problem> {
    let rows = sqlx::query(
        "SELECT p.session_id, s.sleeve::text AS sleeve, s.trade_date, p.lots::int8 AS lots, \
         p.entry_points, p.entry_inr, p.opened_at, p.hard_exit_at, p.last_mark_points, \
         p.last_mark_at, p.simulated FROM op_position p JOIN op_session s ON s.id = p.session_id \
         WHERE p.user_id = $1 AND p.closed_at IS NULL ORDER BY p.opened_at",
    )
    .bind(user_id)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    let now_utc = now.with_timezone(&Utc);
    rows.iter()
        .map(|r| {
            let hard_exit_at = col::ts(r, "hard_exit_at")?;
            // `int((hard_exit_at - now).total_seconds() // 60)`: floor, in microseconds.
            let micros = (hard_exit_at - now_utc)
                .num_microseconds()
                .unwrap_or(i64::MIN);
            let left = micros.div_euclid(60_000_000);
            Ok(PositionOut {
                session_id: col::i64(r, "session_id")?,
                sleeve: col::text(r, "sleeve")?,
                trade_date: col::date(r, "trade_date")?,
                lots: col::i64(r, "lots")?,
                entry_points: col::dec(r, "entry_points")?,
                entry_inr: col::dec(r, "entry_inr")?,
                opened_at: col::ts(r, "opened_at")?,
                hard_exit_at,
                last_mark_points: col::opt_dec(r, "last_mark_points")?,
                last_mark_at: col::opt_ts(r, "last_mark_at")?,
                minutes_to_hard_exit: Some(left.max(0)),
                simulated: col::boolean(r, "simulated")?,
            })
        })
        .collect()
}

const JOURNAL_COLUMNS: &str = "session_id, sleeve::text AS sleeve, trade_date, structure, \
     net_pnl_inr, r_multiple, closed_reason, minutes_held::int8 AS minutes_held, simulated, \
     sizing_mode, risk_budget_inr";

fn closed_view(r: &PgRow) -> Result<ClosedOut, Problem> {
    Ok(ClosedOut {
        session_id: col::i64(r, "session_id")?,
        sleeve: col::text(r, "sleeve")?,
        trade_date: col::date(r, "trade_date")?,
        structure: col::text(r, "structure")?,
        net_pnl_inr: col::dec(r, "net_pnl_inr")?,
        r_multiple: col::dec(r, "r_multiple")?,
        closed_reason: col::text(r, "closed_reason")?,
        minutes_held: col::i64(r, "minutes_held")?,
        simulated: col::boolean(r, "simulated")?,
        sizing_mode: col::text(r, "sizing_mode")?,
    })
}

/// `closed_on(session, user_id, day)`, by sleeve.
pub(crate) async fn closed_on(
    db: &Db,
    user_id: i64,
    day: NaiveDate,
) -> Result<Vec<ClosedOut>, Problem> {
    let rows = sqlx::query(&format!(
        "SELECT {JOURNAL_COLUMNS} FROM op_journal WHERE user_id = $1 AND trade_date = $2 \
         ORDER BY op_journal.sleeve"
    ))
    .bind(user_id)
    .bind(day)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    rows.iter().map(closed_view).collect()
}

/// `week_r(session, user_id, day)`: each sleeve's running R for the ISO week, real and
/// simulated never pooled.
async fn week_r(db: &Db, user_id: i64, day: NaiveDate) -> Result<Vec<WeekROut>, Problem> {
    let monday = day - Duration::days(i64::from(day.weekday().num_days_from_monday()));
    let rows = sqlx::query(
        "SELECT sleeve::text AS sleeve, simulated, sum(r_multiple) AS total, count(*) AS n \
         FROM op_journal WHERE user_id = $1 AND trade_date >= $2 AND trade_date <= $3 \
         GROUP BY op_journal.sleeve, op_journal.simulated",
    )
    .bind(user_id)
    .bind(monday)
    .bind(day)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    let mut out = Vec::new();
    for r in &rows {
        // `Decimal(total or 0)`: a zero total (falsy) is `Decimal(0)`, which prints `0`.
        let total = col::opt_dec(r, "total")?.filter(|t| !t.is_zero());
        out.push(WeekROut {
            sleeve: col::text(r, "sleeve")?,
            simulated: col::boolean(r, "simulated")?,
            r: total.map_or_else(|| WideDecimal::from(0_i64), WideDecimal::from),
            trades: col::i64(r, "n")?,
        });
    }
    out.sort_by(|a, b| (&a.sleeve, a.simulated).cmp(&(&b.sleeve, b.simulated)));
    Ok(out)
}

/// `empty_reason(...)`: why there is no scan for today to show.
fn empty_reason(
    has_scans: bool,
    scan_date: Option<NaiveDate>,
    today: NaiveDate,
    session_day: bool,
    collect_enabled: bool,
    scan_enabled: bool,
) -> Option<&'static str> {
    if has_scans && scan_date == Some(today) {
        return None;
    }
    if !collect_enabled {
        return Some(EMPTY_COLLECTOR_OFF);
    }
    if !scan_enabled {
        return Some(EMPTY_SCAN_OFF);
    }
    if session_day {
        return Some(EMPTY_NO_SCAN_YET);
    }
    if has_scans { None } else { Some(EMPTY_NEVER) }
}

/// The switches and the clock `today_view` takes.
pub(crate) struct TodayInputs {
    pub(crate) user_id: i64,
    pub(crate) now: DateTime<FixedOffset>,
    pub(crate) session_day: bool,
    pub(crate) market_open: bool,
    pub(crate) collect_enabled: bool,
    pub(crate) scan_enabled: bool,
    pub(crate) gates: Vec<GateOut>,
}

/// `today_view(...)`, rendered as `OptionsTodayOut`.
pub(crate) async fn today_view(db: &Db, i: TodayInputs) -> Result<TodayOut, Problem> {
    let today = i.now.with_timezone(&ist()).date_naive();
    let user_id = i.user_id;
    let scan_date = latest_scan_date(db, user_id, today).await?;
    let scans = match scan_date {
        Some(d) => latest_scans(db, user_id, d).await?,
        None => Vec::new(),
    };
    let minutes: Vec<DateTime<Utc>> = scans.iter().filter_map(|s| s.as_of_minute).collect();
    let as_of = minutes
        .iter()
        .max()
        .copied()
        .or_else(|| scans.iter().map(|s| s.ts).max());
    let live = i.market_open && scan_date == Some(today) && !scans.is_empty();
    let stale = live && as_of.is_some_and(|a| i.now.with_timezone(&Utc) - a > Duration::minutes(2));
    let events = event_days(db, user_id, None).await?;
    let upcoming = expiry_rows(db, today, None).await?;
    let closed_day = scan_date.unwrap_or(today);
    Ok(TodayOut {
        today,
        session_day: i.session_day,
        market_open: i.market_open,
        scan_date,
        as_of_minute: as_of,
        live,
        stale,
        empty_reason: empty_reason(
            !scans.is_empty(),
            scan_date,
            today,
            i.session_day,
            i.collect_enabled,
            i.scan_enabled,
        ),
        collect_enabled: i.collect_enabled,
        scan_enabled: i.scan_enabled,
        gates: i.gates,
        roles: roles_for(today, i.session_day, &upcoming, &events),
        expiries: upcoming
            .iter()
            .take(HEADER_EXPIRIES)
            .map(|e| expiry_out(e, &events))
            .collect(),
        pauses: pauses(db, user_id).await?,
        nifty: index_level(db, NIFTY_50, today).await?,
        vix: index_level(db, INDIA_VIX, today).await?,
        scans,
        positions: open_positions(db, user_id, i.now).await?,
        closed_today: closed_on(db, user_id, closed_day).await?,
        week_r: week_r(db, user_id, today).await?,
    })
}

// --- the chain -------------------------------------------------------------------------------

/// One `op_chain_snapshot` row.
struct ChainRow {
    token: i64,
    strike: Decimal,
    option_type: String,
    spot: Option<Decimal>,
    forward: Option<Decimal>,
    bid: Option<Decimal>,
    ask: Option<Decimal>,
    last: Option<Decimal>,
    oi: Option<i64>,
    iv: Option<Decimal>,
    delta: Option<Decimal>,
    gamma: Option<Decimal>,
    theta: Option<Decimal>,
}

/// `_chain_for(session, expiry, day_start=)`.
async fn chain_for(
    db: &Db,
    expiry: NaiveDate,
    day_start: DateTime<Utc>,
) -> Result<Option<ChainExpiryOut>, Problem> {
    let mut latest: Option<DateTime<Utc>> =
        sqlx::query_scalar("SELECT max(ts) FROM op_chain_snapshot WHERE expiry = $1 AND ts >= $2")
            .bind(expiry)
            .bind(day_start)
            .fetch_one(db.pool())
            .await
            .map_err(db_err)?;
    if latest.is_none() {
        latest = sqlx::query_scalar("SELECT max(ts) FROM op_chain_snapshot WHERE expiry = $1")
            .bind(expiry)
            .fetch_one(db.pool())
            .await
            .map_err(db_err)?;
    }
    let Some(latest) = latest else {
        return Ok(None);
    };
    let raw = sqlx::query("SELECT * FROM op_chain_snapshot WHERE expiry = $1 AND ts = $2")
        .bind(expiry)
        .bind(latest)
        .fetch_all(db.pool())
        .await
        .map_err(db_err)?;
    let mut rows = Vec::with_capacity(raw.len());
    for r in &raw {
        rows.push(ChainRow {
            token: col::i64(r, "instrument_token")?,
            strike: col::dec(r, "strike")?,
            option_type: col::text(r, "option_type")?,
            spot: col::opt_dec(r, "spot")?,
            forward: col::opt_dec(r, "forward")?,
            bid: col::opt_dec(r, "bid")?,
            ask: col::opt_dec(r, "ask")?,
            last: col::opt_dec(r, "last")?,
            oi: col::opt_i64(r, "oi")?,
            iv: col::opt_dec(r, "iv")?,
            delta: col::opt_dec(r, "delta")?,
            gamma: col::opt_dec(r, "gamma")?,
            theta: col::opt_dec(r, "theta")?,
        });
    }
    let session_start = ist_midnight(latest.with_timezone(&ist()).date_naive());
    let first_ts: Option<DateTime<Utc>> =
        sqlx::query_scalar("SELECT min(ts) FROM op_chain_snapshot WHERE expiry = $1 AND ts >= $2")
            .bind(expiry)
            .bind(session_start)
            .fetch_one(db.pool())
            .await
            .map_err(db_err)?;
    let mut opening: BTreeMap<i64, Option<i64>> = BTreeMap::new();
    if let Some(first) = first_ts.filter(|f| *f != latest) {
        let open_rows = sqlx::query(
            "SELECT instrument_token, oi FROM op_chain_snapshot WHERE expiry = $1 AND ts = $2",
        )
        .bind(expiry)
        .bind(first)
        .fetch_all(db.pool())
        .await
        .map_err(db_err)?;
        for r in &open_rows {
            opening.insert(col::i64(r, "instrument_token")?, col::opt_i64(r, "oi")?);
        }
    }
    let spot = rows.iter().find_map(|r| r.spot);
    let forward = rows.iter().find_map(|r| r.forward);
    let strikes: Vec<Decimal> = rows
        .iter()
        .map(|r| r.strike)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    // `_atm`: the strike nearest the forward (else spot), the lower one on a tie.
    let atm = forward.or(spot).and_then(|reference| {
        strikes
            .iter()
            .copied()
            .min_by(|a, b| ((*a - reference).abs(), *a).cmp(&((*b - reference).abs(), *b)))
    });
    if let Some(atm) = atm
        && let Some(index) = strikes.iter().position(|k| *k == atm)
    {
        let lo = index.saturating_sub(CHAIN_STRIKES_EACH_SIDE);
        let hi = (index + CHAIN_STRIKES_EACH_SIDE + 1).min(strikes.len());
        let keep: BTreeSet<Decimal> = strikes[lo..hi].iter().copied().collect();
        rows.retain(|r| keep.contains(&r.strike));
    }
    rows.sort_by(|a, b| (a.strike, &a.option_type).cmp(&(b.strike, &b.option_type)));
    let atm_ivs: Vec<WideDecimal> = rows
        .iter()
        .filter(|r| Some(r.strike) == atm)
        .filter_map(|r| r.iv.map(WideDecimal::from))
        .collect();
    let atm_iv = if atm_ivs.is_empty() {
        None
    } else {
        let sum = atm_ivs
            .iter()
            .fold(WideDecimal::from(0_i64), |acc, v| acc.add(v));
        let mean = sum
            .div(&WideDecimal::from(atm_ivs.len()))
            .map_err(|_| internal_error())?;
        Some(wq(&mean, 6)?)
    };
    let put_oi: i64 = rows
        .iter()
        .filter(|r| r.option_type == "PE")
        .map(|r| r.oi.unwrap_or(0))
        .sum();
    let call_oi: i64 = rows
        .iter()
        .filter(|r| r.option_type == "CE")
        .map(|r| r.oi.unwrap_or(0))
        .sum();
    let pcr_oi = if call_oi == 0 {
        None
    } else {
        let ratio = WideDecimal::from(put_oi)
            .div(&WideDecimal::from(call_oi))
            .map_err(|_| internal_error())?;
        Some(wq(&ratio, 4)?)
    };
    Ok(Some(ChainExpiryOut {
        expiry,
        ts: latest,
        spot,
        forward,
        atm_strike: atm,
        atm_iv,
        pcr_oi,
        rows: rows
            .iter()
            .map(|r| ChainRowOut {
                strike: r.strike,
                option_type: r.option_type.clone(),
                bid: r.bid,
                ask: r.ask,
                last: r.last,
                oi: r.oi,
                oi_change: match (r.oi, opening.get(&r.token).copied().flatten()) {
                    (Some(oi), Some(open)) => Some(oi - open),
                    _ => None,
                },
                iv: r.iv,
                delta: r.delta,
                gamma: r.gamma,
                theta: r.theta,
            })
            .collect(),
    }))
}

/// `chain(session, now=, expiry=)`: the named expiry, or the nearest two live ones.
pub(crate) async fn chain(
    db: &Db,
    now: DateTime<FixedOffset>,
    expiry: Option<NaiveDate>,
) -> Result<ChainOut, Problem> {
    let today = now.with_timezone(&ist()).date_naive();
    let day_start = ist_midnight(today);
    let wanted: Vec<NaiveDate> = match expiry {
        Some(e) => vec![e],
        None => expiry_rows(db, today, None)
            .await?
            .iter()
            .take(2)
            .map(|e| e.expiry_date)
            .collect(),
    };
    let mut expiries = Vec::new();
    for one in wanted {
        if let Some(view) = chain_for(db, one, day_start).await? {
            expiries.push(view);
        }
    }
    Ok(ChainOut { expiries })
}

// --- sessions, journal, backtest -------------------------------------------------------------

/// `sessions(session, user_id=, start=, end=, sleeve=)`.
pub(crate) async fn sessions(
    db: &Db,
    user_id: i64,
    start: NaiveDate,
    end: NaiveDate,
    sleeve: Option<&str>,
) -> Result<Vec<SessionOut>, Problem> {
    let rows = sqlx::query(
        "SELECT id, sleeve::text AS sleeve, trade_date, expiry_used, mode, state, verdict, \
         skip_reasons::text[] AS skip_reasons, closed_reason, pnl_inr, pnl_r, slot_holder \
         FROM op_session WHERE user_id = $1 AND trade_date >= $2 AND trade_date <= $3 \
         AND ($4::text IS NULL OR sleeve::text = $4) ORDER BY op_session.trade_date, op_session.sleeve",
    )
    .bind(user_id)
    .bind(start)
    .bind(end)
    .bind(sleeve)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    rows.iter()
        .map(|r| {
            Ok(SessionOut {
                id: col::i64(r, "id")?,
                sleeve: col::text(r, "sleeve")?,
                trade_date: col::date(r, "trade_date")?,
                expiry_used: col::opt_date(r, "expiry_used")?,
                mode: col::text(r, "mode")?,
                state: col::text(r, "state")?,
                verdict: col::opt_text(r, "verdict")?,
                skip_reasons: col::texts(r, "skip_reasons")?,
                closed_reason: col::opt_text(r, "closed_reason")?,
                pnl_inr: col::opt_dec(r, "pnl_inr")?,
                pnl_r: col::opt_dec(r, "pnl_r")?,
                slot_holder: col::opt_text(r, "slot_holder")?,
            })
        })
        .collect()
}

/// `journal(session, user_id=, sleeve=)` rendered as `OptionsJournalOut`: `04` §12's summary per
/// (sleeve, simulated, sizing mode), never pooled.
pub(crate) async fn journal(
    db: &Db,
    user_id: i64,
    sleeve: Option<&str>,
) -> Result<JournalOut, Problem> {
    let rows = sqlx::query(&format!(
        "SELECT {JOURNAL_COLUMNS} FROM op_journal WHERE user_id = $1 \
         AND ($2::text IS NULL OR sleeve::text = $2) \
         ORDER BY op_journal.trade_date, op_journal.sleeve"
    ))
    .bind(user_id)
    .bind(sleeve)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    // `select(OpSession)` with no ORDER BY: every column, so the plan (and the row order the
    // skip buckets are built in) is the one SQLAlchemy's statement gets.
    let over = sqlx::query(
        "SELECT op_session.*, op_session.sleeve::text AS sleeve_text, \
         op_session.skip_reasons::text[] AS skip_text FROM op_session \
         WHERE op_session.user_id = $1 AND op_session.state = ANY($2) \
         AND ($3::text IS NULL OR op_session.sleeve::text = $3)",
    )
    .bind(user_id)
    .bind(
        OVER_STATES
            .iter()
            .map(|s| (*s).to_owned())
            .collect::<Vec<_>>(),
    )
    .bind(sleeve)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    let mut skips: Vec<(String, Vec<(String, i64)>)> = Vec::new();
    let mut done: BTreeMap<String, i64> = BTreeMap::new();
    for ended in &over {
        let ended_sleeve = col::text(ended, "sleeve_text")?;
        let group = core::group_of(&ended_sleeve).to_owned();
        if col::text(ended, "mode")? == "PAPER" {
            *done.entry(group).or_insert(0) += 1;
        }
        if col::text(ended, "state")? == "SKIPPED" {
            let reasons = col::texts(ended, "skip_text")?;
            let reason = reasons
                .first()
                .cloned()
                .unwrap_or_else(|| "UNSPECIFIED".to_owned());
            let slot = match skips.iter().position(|(s, _)| *s == ended_sleeve) {
                Some(i) => i,
                None => {
                    skips.push((ended_sleeve.clone(), Vec::new()));
                    skips.len() - 1
                }
            };
            let bucket = &mut skips[slot].1;
            match bucket.iter_mut().find(|(r, _)| *r == reason) {
                Some(entry) => entry.1 += 1,
                None => bucket.push((reason, 1)),
            }
        }
    }
    let mut traded: BTreeMap<String, i64> = BTreeMap::new();
    let mut journal_rows = Vec::with_capacity(rows.len());
    let mut closed = Vec::with_capacity(rows.len());
    for r in &rows {
        let view = closed_view(r)?;
        if view.simulated {
            *traded
                .entry(core::group_of(&view.sleeve).to_owned())
                .or_insert(0) += 1;
        }
        let budget = col::dec(r, "risk_budget_inr")?;
        journal_rows.push(JournalRow {
            sleeve: view.sleeve.clone(),
            trade_date: view.trade_date,
            simulated: view.simulated,
            sizing_mode: view.sizing_mode.clone(),
            net_pnl_inr: WideDecimal::from(view.net_pnl_inr),
            r_inr: if budget > Decimal::ZERO {
                WideDecimal::from(budget)
            } else {
                WideDecimal::from(1_i64)
            },
            // `row.closed_reason or "UNSPECIFIED"`: an empty reason is falsy too.
            closed_reason: Some(view.closed_reason.clone()).filter(|r| !r.is_empty()),
        });
        closed.push(view);
    }
    let groups: Vec<(&'static str, i64, i64)> = match sleeve {
        Some(s) => PAPER_PERIOD
            .iter()
            .copied()
            .filter(|(g, _, _)| *g == core::group_of(s))
            .collect(),
        None => PAPER_PERIOD.to_vec(),
    };
    let progress = groups
        .into_iter()
        .map(|(group, sessions_needed, traded_needed)| ProgressOut {
            group,
            sessions_needed,
            traded_needed,
            sessions_done: done.get(group).copied().unwrap_or(0),
            traded: traded.get(group).copied().unwrap_or(0),
        })
        .collect();
    let mut r_values: BTreeMap<core::PoolKey, Vec<WideDecimal>> = BTreeMap::new();
    for row in &journal_rows {
        r_values
            .entry(core::key_of(row))
            .or_default()
            .push(row.r().ok_or_else(internal_error)?);
    }
    let summaries = core::summarize(&journal_rows)
        .ok_or_else(internal_error)?
        .into_iter()
        .map(|s| SummaryOut {
            r_values: r_values.get(&s.key).cloned().unwrap_or_default(),
            sleeve: s.key.0,
            simulated: s.key.1,
            sizing_mode: s.key.2,
            count: s.count,
            traded: s.traded,
            win_rate: s.win_rate,
            mean_r: s.mean_r,
            expectancy_inr: s.expectancy_inr,
            expectancy_r: s.expectancy_r,
            worst_r: s.worst_r,
            max_drawdown_r: s.max_drawdown_r,
            max_drawdown_inr: s.max_drawdown_inr,
            by_closed_reason: s
                .by_closed_reason
                .into_iter()
                .map(|(label, b)| BucketOut {
                    label,
                    count: b.count,
                    mean_r: b.mean_r,
                })
                .collect(),
        })
        .collect();
    let skips_by_reason = OrderedJson::Object(
        skips
            .into_iter()
            .map(|(s, bucket)| {
                (
                    s,
                    OrderedJson::Object(
                        bucket
                            .into_iter()
                            .map(|(r, n)| (r, OrderedJson::Int(n)))
                            .collect(),
                    ),
                )
            })
            .collect(),
    );
    let min_sessions = OrderedJson::Object(
        DISPLAY_ORDER
            .iter()
            .map(|s| {
                (
                    (*s).to_owned(),
                    OrderedJson::Int(core::tier3_min_sessions(s)),
                )
            })
            .collect(),
    );
    let recent = closed.iter().rev().take(20).cloned().collect();
    Ok(JournalOut {
        summaries,
        skips_by_reason,
        progress,
        min_sessions,
        recent,
    })
}

/// `backtests(session, user_id=, sleeve=)`: the newest run per (sleeve, tier).
pub(crate) async fn backtests(
    db: &Db,
    user_id: i64,
    sleeve: Option<&str>,
) -> Result<Vec<BacktestRunOut>, Problem> {
    let rows = sqlx::query(
        "SELECT b.id, b.sleeve::text AS sleeve, b.tier::int8 AS tier, b.date_from, b.date_to, \
         b.sessions::int8 AS sessions, b.signals::int8 AS signals, b.traded::int8 AS traded, \
         b.skipped_by_reason_json, b.win_rate, b.expectancy_r, b.net_pnl_inr, b.max_drawdown_r, \
         b.caveats, b.ran_at FROM op_backtest_run b JOIN (SELECT sleeve, tier, max(id) AS id \
         FROM op_backtest_run WHERE user_id = $1 GROUP BY sleeve, tier) newest \
         ON b.id = newest.id WHERE ($2::text IS NULL OR b.sleeve::text = $2) \
         ORDER BY b.sleeve, b.tier",
    )
    .bind(user_id)
    .bind(sleeve)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    rows.iter()
        .map(|r| {
            Ok(BacktestRunOut {
                id: col::i64(r, "id")?,
                sleeve: col::text(r, "sleeve")?,
                tier: col::i64(r, "tier")?,
                date_from: col::date(r, "date_from")?,
                date_to: col::date(r, "date_to")?,
                sessions: col::i64(r, "sessions")?,
                signals: col::i64(r, "signals")?,
                traded: col::i64(r, "traded")?,
                skipped_by_reason: col::object(r, "skipped_by_reason_json")?,
                win_rate: col::opt_dec(r, "win_rate")?,
                expectancy_r: col::opt_dec(r, "expectancy_r")?,
                net_pnl_inr: col::opt_dec(r, "net_pnl_inr")?,
                max_drawdown_r: col::opt_dec(r, "max_drawdown_r")?,
                caveats: col::text(r, "caveats")?,
                ran_at: col::ts(r, "ran_at")?,
            })
        })
        .collect()
}

/// `calendar(session, user_id=, year=)`: the year's live expiries and event days.
pub(crate) async fn calendar(db: &Db, user_id: i64, year: i32) -> Result<CalendarOut, Problem> {
    let start = NaiveDate::from_ymd_opt(year, 1, 1).ok_or_else(internal_error)?;
    let end = NaiveDate::from_ymd_opt(year, 12, 31).ok_or_else(internal_error)?;
    let events: BTreeMap<NaiveDate, EventRow> = event_days(db, user_id, Some(start))
        .await?
        .into_iter()
        .filter(|(d, _)| *d <= end)
        .collect();
    let expiries = expiry_rows(db, start, Some(end)).await?;
    Ok(CalendarOut {
        year: i64::from(year),
        expiries: expiries.iter().map(|e| expiry_out(e, &events)).collect(),
        event_days: events
            .into_iter()
            .map(|(date, e)| EventDayOut {
                date,
                removable: e.source == USER_SOURCE,
                reason: e.reason,
                source: e.source,
                source_url: e.source_url,
                note: e.note,
            })
            .collect(),
    })
}

/// `read_book` + `read_sleeve` for every `SleeveGroup`: `None` is `OptionsConfigNotSeeded`.
pub(crate) async fn read_config(
    db: &Db,
    user_id: i64,
) -> Result<Option<(BookOut, Vec<SleeveConfigOut>)>, Problem> {
    let book = sqlx::query(
        "SELECT underlying, account_inr, margin_pool_inr, daily_loss_limit_inr, \
         monthly_pause_inr, paused_until, paused_reason FROM op_book_config WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_optional(db.pool())
    .await
    .map_err(db_err)?;
    let Some(b) = book else { return Ok(None) };
    let book = BookOut {
        underlying: col::text(&b, "underlying")?,
        account_inr: col::dec(&b, "account_inr")?,
        margin_pool_inr: col::dec(&b, "margin_pool_inr")?,
        daily_loss_limit_inr: col::dec(&b, "daily_loss_limit_inr")?,
        monthly_pause_inr: col::dec(&b, "monthly_pause_inr")?,
        paused_until: col::opt_date(&b, "paused_until")?,
        paused_reason: col::opt_text(&b, "paused_reason")?,
    };
    let mut sleeves = Vec::new();
    for (group, _, _) in PAPER_PERIOD {
        let row = sqlx::query(
            "SELECT sleeve, sleeve_capital_inr, risk_per_trade_pct, max_lots::int8 AS max_lots, \
             paper_enabled, hard_exit_time, paused_until, paused_reason FROM op_sleeve_config \
             WHERE user_id = $1 AND sleeve = $2",
        )
        .bind(user_id)
        .bind(group)
        .fetch_optional(db.pool())
        .await
        .map_err(db_err)?;
        let Some(s) = row else { return Ok(None) };
        sleeves.push(SleeveConfigOut {
            sleeve: col::text(&s, "sleeve")?,
            sleeve_capital_inr: col::dec(&s, "sleeve_capital_inr")?,
            risk_per_trade_pct: col::dec(&s, "risk_per_trade_pct")?,
            max_lots: col::i64(&s, "max_lots")?,
            paper_enabled: col::boolean(&s, "paper_enabled")?,
            hard_exit_time: col::time(&s, "hard_exit_time")?,
            paused_until: col::opt_date(&s, "paused_until")?,
            paused_reason: col::opt_text(&s, "paused_reason")?,
        });
    }
    Ok(Some((book, sleeves)))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn empty_reason_names_the_switch_first() {
        let d = NaiveDate::from_ymd_opt(2026, 10, 30).unwrap();
        let y = NaiveDate::from_ymd_opt(2026, 10, 29).unwrap();
        assert_eq!(empty_reason(true, Some(d), d, true, false, false), None);
        assert_eq!(
            empty_reason(true, Some(y), d, true, false, true),
            Some("collector_off")
        );
        assert_eq!(
            empty_reason(true, Some(y), d, true, true, false),
            Some("scan_off")
        );
        assert_eq!(
            empty_reason(true, Some(y), d, true, true, true),
            Some("no_scan_yet_today")
        );
        assert_eq!(empty_reason(true, Some(y), d, false, true, true), None);
        assert_eq!(
            empty_reason(false, None, d, false, true, true),
            Some("never_scanned")
        );
    }

    #[test]
    fn a_time_prints_as_isoformat() {
        assert_eq!(
            py_time(NaiveTime::from_hms_opt(14, 30, 0).unwrap()),
            "14:30:00"
        );
        assert_eq!(
            py_time(NaiveTime::from_hms_micro_opt(9, 5, 1, 250).unwrap()),
            "09:05:01.000250"
        );
    }
}
