//! Ports `PgSwingStore` (`app/swing_desk.py`) and the `SwingStore` protocol of contract C1
//! (`app/swing_execute.py`): one user's `sw_` rows through the desk's connection, `?`
//! placeholders, tables qualified by schema (`public` on the desk's Postgres, `""` on the tests'
//! SQLite twin), every price a `Decimal` in and out.
//!
//! The SQL is Python's, statement for statement, so `pg.translate` turns both into the same
//! Postgres text.

use std::sync::Arc;

use async_trait::async_trait;
use chrono::{DateTime, FixedOffset, NaiveDate};
use rust_decimal::Decimal;

use super::context::{SignalContext, load_context};
use super::live_gate::LiveLevel;
use super::values::{
    Val, bind, boolean, col, date, dec, int, is_uuid, iso_date, iso_dt, json_column, opt_int,
    opt_text, stamp, text,
};
use crate::clock::Clock;
use crate::settings::Settings;
use crate::storage::{Conn, DbValue, Param, Row, StorageError};
use crate::wire::PyValue;

/// The screener's tables live in `public`; the desk's connection is on `search_path=desk`.
pub const SCHEMA: &str = "public";

/// `MANAGE_ACTIONS_SHOWN`.
pub const MANAGE_ACTIONS_SHOWN: i64 = 5;

/// A store failure (the driver's, or a caller's bug Python raises as `ValueError`).
#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    /// The database.
    #[error("{0}")]
    Storage(#[from] StorageError),
    /// A caller's bug.
    #[error("ValueError: {0}")]
    Value(String),
    /// `HTTPException(409, SESSION_BUSY)`: the session row is held by another confirm.
    #[error("{SESSION_BUSY}")]
    SessionBusy,
}

/// Postgres's SQLSTATE for `FOR UPDATE NOWAIT` finding the row held (`LOCK_NOT_AVAILABLE`).
pub const LOCK_NOT_AVAILABLE: &str = "55P03";

/// `SESSION_BUSY`: what a confirm that finds the session row held answers (409).
pub const SESSION_BUSY: &str = "SESSION_BUSY: another confirm of this session is in flight; \
                                nothing was sent — confirm again";

/// `sw_plan` as the store returns it.
#[derive(Debug, Clone, PartialEq)]
pub struct PlanRow {
    /// `id`.
    pub id: i64,
    /// `plan_id` (uuid text).
    pub plan_id: String,
    /// `as_of`.
    pub as_of: Option<NaiveDate>,
    /// `source`.
    pub source: String,
    /// `built_at`.
    pub built_at: Option<DateTime<FixedOffset>>,
    /// `expires_at`.
    pub expires_at: Option<DateTime<FixedOffset>>,
    /// `gate`.
    pub gate: String,
    /// `exposure_level`.
    pub exposure_level: i64,
    /// `total_risk_inr`.
    pub total_risk_inr: Option<Decimal>,
    /// `total_new_exposure_inr`.
    pub total_new_exposure_inr: Option<Decimal>,
}

/// `sw_plan_line` joined to its plan and instrument.
#[derive(Debug, Clone, PartialEq)]
pub struct LineRow {
    /// `id`.
    pub id: i64,
    /// `plan_pk`.
    pub plan_pk: i64,
    /// `plan_id` (uuid text).
    pub plan_id: String,
    /// `kind`.
    pub kind: String,
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `setup`.
    pub setup: Option<String>,
    /// `quantity`.
    pub quantity: i64,
    /// `trigger`.
    pub trigger: Option<Decimal>,
    /// `stop`.
    pub stop: Option<Decimal>,
    /// `risk_inr`.
    pub risk_inr: Option<Decimal>,
    /// `position_value`.
    pub position_value: Option<Decimal>,
    /// `trail`.
    pub trail: Option<String>,
    /// `note`.
    pub note: Option<String>,
    /// `state`.
    pub state: String,
    /// `client_id`.
    pub client_id: String,
    /// `journal_ref`.
    pub journal_ref: Option<String>,
    /// `position_id`.
    pub position_id: Option<i64>,
    /// `updated_at`.
    pub updated_at: Option<DateTime<FixedOffset>>,
    /// `as_of`.
    pub as_of: Option<NaiveDate>,
    /// `plan_source`.
    pub plan_source: String,
}

/// `sw_position` joined to its instrument.
#[derive(Debug, Clone, PartialEq)]
pub struct PositionRow {
    /// `id`.
    pub id: i64,
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `setup`.
    pub setup: String,
    /// `entry_date`.
    pub entry_date: Option<NaiveDate>,
    /// `entry_avg`.
    pub entry_avg: Option<Decimal>,
    /// `quantity_entered`.
    pub quantity_entered: i64,
    /// `quantity_open`.
    pub quantity_open: i64,
    /// `initial_stop`.
    pub initial_stop: Option<Decimal>,
    /// `stop`.
    pub stop: Option<Decimal>,
    /// `gtt_id`.
    pub gtt_id: Option<String>,
    /// `gtt_trigger`.
    pub gtt_trigger: Option<Decimal>,
    /// `gtt_armed_at`.
    pub gtt_armed_at: Option<DateTime<FixedOffset>>,
    /// `trail`.
    pub trail: String,
    /// `partial_done`.
    pub partial_done: bool,
    /// `partial_date`.
    pub partial_date: Option<NaiveDate>,
    /// `state`.
    pub state: String,
    /// `closed_on`.
    pub closed_on: Option<NaiveDate>,
    /// `exit_avg`.
    pub exit_avg: Option<Decimal>,
    /// `close_reason`.
    pub close_reason: Option<String>,
    /// `r_multiple`.
    pub r_multiple: Option<Decimal>,
    /// `pnl_inr`.
    pub pnl_inr: Option<Decimal>,
    /// `simulated`.
    pub simulated: bool,
    /// `half_risk`.
    pub half_risk: bool,
}

/// `sw_fill`.
#[derive(Debug, Clone, PartialEq)]
pub struct FillRow {
    /// `id`.
    pub id: i64,
    /// `side`.
    pub side: String,
    /// `quantity`.
    pub quantity: i64,
    /// `price`.
    pub price: Option<Decimal>,
    /// `filled_at`.
    pub filled_at: Option<DateTime<FixedOffset>>,
    /// `journal_ref`.
    pub journal_ref: Option<String>,
    /// `simulated`.
    pub simulated: bool,
}

/// `sw_session`.
#[derive(Debug, Clone, PartialEq)]
pub struct SessionRow {
    /// `session_date`.
    pub session_date: Option<NaiveDate>,
    /// `mode`.
    pub mode: String,
    /// `monitor_ran`.
    pub monitor_ran: bool,
    /// `signals`.
    pub signals: i64,
    /// `confirms`.
    pub confirms: i64,
    /// `fills`.
    pub fills: i64,
    /// `manage_actions`.
    pub manage_actions: i64,
    /// `notes`.
    pub notes: Option<String>,
}

/// `sw_config` as `config()` returns it.
#[derive(Debug, Clone, PartialEq)]
pub struct SleeveConfig {
    /// `sleeve_capital_inr`.
    pub sleeve_capital_inr: Decimal,
    /// `risk_per_trade_pct`.
    pub risk_per_trade_pct: Decimal,
    /// `max_position_pct`.
    pub max_position_pct: Decimal,
    /// `max_open_positions`.
    pub max_open_positions: i64,
    /// `first_live_sessions_left`.
    pub first_live_sessions_left: i64,
    /// `exposure_level`.
    pub exposure_level: i64,
    /// Whether the row exists.
    pub present: bool,
}

/// `sw_signal` with its watch's focus.
#[derive(Debug, Clone, PartialEq)]
pub struct SignalRow {
    /// `id`.
    pub id: i64,
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `setup`.
    pub setup: String,
    /// `session_date`.
    pub session_date: Option<NaiveDate>,
    /// `raised_at`.
    pub raised_at: Option<DateTime<FixedOffset>>,
    /// `state`.
    pub state: String,
    /// `or_window_minutes`.
    pub or_window_minutes: Option<i64>,
    /// `range_high`.
    pub range_high: Option<Decimal>,
    /// `range_low`.
    pub range_low: Option<Decimal>,
    /// `low_of_day`.
    pub low_of_day: Option<Decimal>,
    /// `last_price`.
    pub last_price: Option<Decimal>,
    /// `entry`.
    pub entry: Option<Decimal>,
    /// `stop`.
    pub stop: Option<Decimal>,
    /// `plan_line_id`.
    pub plan_line_id: Option<i64>,
    /// `focus`.
    pub focus: bool,
}

/// `sw_plan_skip`.
#[derive(Debug, Clone, PartialEq)]
pub struct SkipRow {
    /// `id`.
    pub id: i64,
    /// `instrument_id`.
    pub instrument_id: Option<i64>,
    /// `symbol`.
    pub symbol: String,
    /// `reason`.
    pub reason: String,
    /// `detail`.
    pub detail: Option<String>,
}

/// `signal_skip_for`'s answer.
#[derive(Debug, Clone, PartialEq)]
pub struct SignalSkip {
    /// `reason`.
    pub reason: String,
    /// `detail`.
    pub detail: Option<String>,
    /// `plan_id`.
    pub plan_id: String,
}

/// `lv_protection_issue` (LV2).
#[derive(Debug, Clone, PartialEq)]
pub struct ProtectionIssue {
    /// `symbol`.
    pub symbol: String,
    /// `kind`.
    pub kind: String,
    /// `detail`.
    pub detail: String,
    /// `position_id`.
    pub position_id: i64,
}

/// `sw_scan_run`.
#[derive(Debug, Clone, PartialEq)]
pub struct ScanRun {
    /// `id`.
    pub id: i64,
    /// `requested_at`.
    pub requested_at: Option<DateTime<FixedOffset>>,
    /// `started_at`.
    pub started_at: Option<DateTime<FixedOffset>>,
    /// `finished_at`.
    pub finished_at: Option<DateTime<FixedOffset>>,
    /// `session_date`.
    pub session_date: Option<NaiveDate>,
    /// `provisional`.
    pub provisional: bool,
    /// `status`.
    pub status: String,
    /// `detail.funnel` when it is a dict.
    pub funnel: Option<PyValue>,
    /// `detail` when it is a dict.
    pub detail: Option<PyValue>,
    /// `error`.
    pub error: Option<String>,
    /// `task_id`.
    pub task_id: Option<String>,
}

/// The newest `sw_market_daily` row's provenance.
#[derive(Debug, Clone, PartialEq)]
pub struct MarketScan {
    /// `date`.
    pub date: Option<NaiveDate>,
    /// `provisional`.
    pub provisional: bool,
    /// `detail.scan.scanned_at`.
    pub scanned_at: Option<DateTime<FixedOffset>>,
}

/// `catalysts_for`'s view of one name.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Catalyst {
    /// `headline`.
    pub headline: Option<String>,
    /// `published_at`.
    pub published_at: Option<DateTime<FixedOffset>>,
    /// `url`.
    pub url: Option<String>,
    /// `earnings_date`.
    pub earnings_date: Option<NaiveDate>,
}

/// `request_scan`'s refusals (`ScanRefused`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScanRefused {
    /// The HTTP status (409 / 429).
    pub status: u16,
    /// The sentence.
    pub reason: String,
    /// The newest run's id.
    pub run_id: i64,
    /// Seconds, for `Retry-After`.
    pub retry_after: Option<i64>,
}

/// Fields for a new position (`create_position`'s dict, in Python's key order).
pub type Fields = Vec<(&'static str, Val)>;

/// The columns a caller may write on `sw_position`.
pub const POSITION_COLUMNS: [&str; 23] = [
    "broker_account_id",
    "instrument_id",
    "setup",
    "entry_date",
    "entry_avg",
    "quantity_entered",
    "initial_stop",
    "stop",
    "gtt_id",
    "gtt_trigger",
    "gtt_armed_at",
    "trail",
    "partial_done",
    "partial_date",
    "quantity_open",
    "state",
    "closed_on",
    "exit_avg",
    "close_reason",
    "r_multiple",
    "pnl_inr",
    "simulated",
    "half_risk",
];

/// The columns a caller may write on `sw_fill`.
pub const FILL_COLUMNS: [&str; 7] = [
    "position_id",
    "side",
    "quantity",
    "price",
    "filled_at",
    "journal_ref",
    "simulated",
];

/// Contract C1's `SwingStore`: what `execute_line` and its siblings need of the book.
#[async_trait]
pub trait SwingStore: Send {
    /// `plan(plan_id)`.
    async fn plan(&mut self, plan_id: &str) -> Result<Option<PlanRow>, StoreError>;
    /// `line(line_id)`.
    async fn line(&mut self, line_id: i64) -> Result<Option<LineRow>, StoreError>;
    /// `set_line(...)`.
    async fn set_line(
        &mut self,
        line_id: i64,
        state: &str,
        journal_ref: Option<&str>,
        position_id: Option<i64>,
    ) -> Result<(), StoreError>;
    /// `open_position_for(instrument_id)`.
    async fn open_position_for(
        &mut self,
        instrument_id: i64,
    ) -> Result<Option<PositionRow>, StoreError>;
    /// `position(position_id)`.
    async fn position(&mut self, position_id: i64) -> Result<Option<PositionRow>, StoreError>;
    /// `create_position(fields)`.
    async fn create_position(&mut self, fields: Fields) -> Result<i64, StoreError>;
    /// `update_position(position_id, fields)`.
    async fn update_position(&mut self, position_id: i64, fields: Fields)
    -> Result<(), StoreError>;
    /// `add_fill(fields)`.
    async fn add_fill(&mut self, fields: Fields) -> Result<i64, StoreError>;
    /// `bump_session(...)`.
    async fn bump_session(
        &mut self,
        day: NaiveDate,
        mode: &str,
        confirms: i64,
        fills: i64,
        manage_actions: i64,
    ) -> Result<(), StoreError>;
    /// `note_session(day, note)` (SW11).
    async fn note_session(&mut self, day: NaiveDate, note: &str) -> Result<(), StoreError>;
    /// `config()`.
    async fn config(&mut self) -> Result<SleeveConfig, StoreError>;
    /// `open_positions()`.
    async fn open_positions(&mut self) -> Result<Vec<PositionRow>, StoreError>;
    /// `open_protection_issues()` (LV2).
    async fn open_protection_issues(&mut self) -> Result<Vec<ProtectionIssue>, StoreError>;
    /// The lock's `BEGIN` half: the day's session row inserted and held.
    async fn lock_begin(&mut self, day: NaiveDate) -> Result<(), StoreError>;
    /// The lock's `COMMIT`.
    async fn lock_commit(&mut self) -> Result<(), StoreError>;
    /// The lock's `ROLLBACK`.
    async fn lock_rollback(&mut self) -> Result<(), StoreError>;
    /// `session_context(day)`.
    async fn session_context(&mut self, day: NaiveDate) -> Result<SignalContext, StoreError>;
    /// `resize_line(...)`.
    async fn resize_line(
        &mut self,
        line_id: i64,
        quantity: i64,
        risk_inr: Decimal,
        position_value: Decimal,
        note: &str,
    ) -> Result<(), StoreError>;
    /// `range_high_for(line_id)`.
    async fn range_high_for(&mut self, line_id: i64) -> Result<Option<Decimal>, StoreError>;
    /// `line_by_order(order_id)`.
    async fn line_by_order(&mut self, order_id: &str) -> Result<Option<LineRow>, StoreError>;
    /// `sent_buy_lines(day)`.
    async fn sent_buy_lines(&mut self, day: NaiveDate) -> Result<Vec<LineRow>, StoreError>;
    /// `note_line(line_id, note)`.
    async fn note_line(&mut self, line_id: i64, note: &str) -> Result<(), StoreError>;
    /// `expire_pending(day)`.
    async fn expire_pending(&mut self, day: NaiveDate) -> Result<i64, StoreError>;
}

/// `PgSwingStore`.
pub struct PgSwingStore {
    /// The connection.
    pub conn: Conn,
    /// The user.
    pub user_id: i64,
    /// `public`, or `""` for a SQLite file.
    pub schema: String,
    /// The broker account a new position is stamped with.
    pub broker_account_id: Option<i64>,
    /// SW30: today's index level; `None` keeps the last close's gate.
    pub live_level: Option<Arc<dyn LiveLevel>>,
    /// The settings at the moment the store was opened (the lock's mode, the gate's index).
    pub settings: Settings,
    /// The clock (the live gate's "now").
    pub clock: Arc<dyn Clock>,
}

const LINE_SELECT: &str = "SELECT l.id, l.plan_id AS plan_pk, p.plan_id, l.kind, l.instrument_id, i.symbol, \
     l.setup, l.quantity, l.trigger, l.stop, l.risk_inr, l.position_value, l.trail, \
     l.note, l.state, l.client_id, l.journal_ref, l.position_id, l.updated_at, \
     p.as_of, p.source AS plan_source ";

const POSITION_SELECT: &str = "SELECT p.id, p.instrument_id, i.symbol, p.setup, p.entry_date, p.entry_avg, \
     p.quantity_entered, p.quantity_open, p.initial_stop, p.stop, p.gtt_id, p.gtt_trigger, \
     p.gtt_armed_at, p.trail, p.partial_done, p.partial_date, p.state, p.closed_on, \
     p.exit_avg, p.close_reason, p.r_multiple, p.pnl_inr, p.simulated, p.half_risk ";

fn plan_row(r: &Row) -> PlanRow {
    PlanRow {
        id: int(col(r, "id")),
        plan_id: text(col(r, "plan_id")),
        as_of: date(col(r, "as_of")),
        source: text(col(r, "source")),
        built_at: stamp(col(r, "built_at")),
        expires_at: stamp(col(r, "expires_at")),
        gate: text(col(r, "gate")),
        exposure_level: int(col(r, "exposure_level")),
        total_risk_inr: dec(col(r, "total_risk_inr"), "total_risk_inr"),
        total_new_exposure_inr: dec(col(r, "total_new_exposure_inr"), "total_new_exposure_inr"),
    }
}

fn line_row(r: &Row) -> LineRow {
    LineRow {
        id: int(col(r, "id")),
        plan_pk: int(col(r, "plan_pk")),
        plan_id: text(col(r, "plan_id")),
        kind: text(col(r, "kind")),
        instrument_id: int(col(r, "instrument_id")),
        symbol: text(col(r, "symbol")),
        setup: opt_text(col(r, "setup")),
        quantity: int(col(r, "quantity")),
        trigger: dec(col(r, "trigger"), "trigger"),
        stop: dec(col(r, "stop"), "stop"),
        risk_inr: dec(col(r, "risk_inr"), "risk_inr"),
        position_value: dec(col(r, "position_value"), "position_value"),
        trail: opt_text(col(r, "trail")),
        note: opt_text(col(r, "note")),
        state: text(col(r, "state")),
        client_id: text(col(r, "client_id")),
        journal_ref: opt_text(col(r, "journal_ref")),
        position_id: opt_int(col(r, "position_id")),
        updated_at: stamp(col(r, "updated_at")),
        as_of: date(col(r, "as_of")),
        plan_source: text(col(r, "plan_source")),
    }
}

fn position_row(r: &Row) -> PositionRow {
    PositionRow {
        id: int(col(r, "id")),
        instrument_id: int(col(r, "instrument_id")),
        symbol: text(col(r, "symbol")),
        setup: text(col(r, "setup")),
        entry_date: date(col(r, "entry_date")),
        entry_avg: dec(col(r, "entry_avg"), "entry_avg"),
        quantity_entered: int(col(r, "quantity_entered")),
        quantity_open: int(col(r, "quantity_open")),
        initial_stop: dec(col(r, "initial_stop"), "initial_stop"),
        stop: dec(col(r, "stop"), "stop"),
        gtt_id: opt_text(col(r, "gtt_id")),
        gtt_trigger: dec(col(r, "gtt_trigger"), "gtt_trigger"),
        gtt_armed_at: stamp(col(r, "gtt_armed_at")),
        trail: text(col(r, "trail")),
        partial_done: boolean(col(r, "partial_done")),
        partial_date: date(col(r, "partial_date")),
        state: text(col(r, "state")),
        closed_on: date(col(r, "closed_on")),
        exit_avg: dec(col(r, "exit_avg"), "exit_avg"),
        close_reason: opt_text(col(r, "close_reason")),
        r_multiple: dec(col(r, "r_multiple"), "r_multiple"),
        pnl_inr: dec(col(r, "pnl_inr"), "pnl_inr"),
        simulated: boolean(col(r, "simulated")),
        half_risk: boolean(col(r, "half_risk")),
    }
}

fn scan_run_row(r: &Row) -> ScanRun {
    let detail = json_column(col(r, "detail"));
    let detail = detail.filter(|d| matches!(d, PyValue::Map(_)));
    let funnel = match &detail {
        Some(PyValue::Map(m)) => m
            .get("funnel")
            .cloned()
            .filter(|f| matches!(f, PyValue::Map(_))),
        _ => None,
    };
    ScanRun {
        id: int(col(r, "id")),
        requested_at: stamp(col(r, "requested_at")),
        started_at: stamp(col(r, "started_at")),
        finished_at: stamp(col(r, "finished_at")),
        session_date: date(col(r, "session_date")),
        provisional: boolean(col(r, "provisional")),
        status: text(col(r, "status")),
        funnel,
        detail,
        error: opt_text(col(r, "error")),
        task_id: opt_text(col(r, "task_id")),
    }
}

fn day_param(day: NaiveDate) -> Param {
    Param::Text(iso_date(day))
}

fn unknown_columns(fields: &Fields, allowed: &[&str]) -> Option<String> {
    let mut unknown: Vec<&str> = fields
        .iter()
        .map(|(k, _)| *k)
        .filter(|k| !allowed.contains(k))
        .collect();
    if unknown.is_empty() {
        return None;
    }
    unknown.sort_unstable();
    unknown.dedup();
    let quoted: Vec<String> = unknown.iter().map(|k| format!("'{k}'")).collect();
    Some(format!("[{}]", quoted.join(", ")))
}

impl PgSwingStore {
    /// `schema.table`, or the bare name when the schema is empty.
    #[must_use]
    pub fn t(&self, table: &str) -> String {
        if self.schema.is_empty() {
            table.to_owned()
        } else {
            format!("{}.{table}", self.schema)
        }
    }

    async fn rows(&mut self, sql: &str, params: &[Param]) -> Result<Vec<Row>, StoreError> {
        Ok(self.conn.execute(sql, params).await?.rows)
    }

    async fn one(&mut self, sql: &str, params: &[Param]) -> Result<Option<Row>, StoreError> {
        Ok(self
            .conn
            .execute(sql, params)
            .await?
            .rows
            .into_iter()
            .next())
    }

    fn line_from(&self) -> String {
        format!(
            "FROM {} l JOIN {} p ON p.id = l.plan_id JOIN {} i ON i.id = l.instrument_id ",
            self.t("sw_plan_line"),
            self.t("sw_plan"),
            self.t("instrument")
        )
    }

    fn position_from(&self) -> String {
        format!(
            "FROM {} p JOIN {} i ON i.id = p.instrument_id ",
            self.t("sw_position"),
            self.t("instrument")
        )
    }

    fn plan_select(&self) -> String {
        format!(
            "SELECT id, plan_id, as_of, source, built_at, expires_at, gate, exposure_level, \
             total_risk_inr, total_new_exposure_inr FROM {} ",
            self.t("sw_plan")
        )
    }

    /// `fills_for(position_id)`.
    ///
    /// # Errors
    /// The driver's.
    pub async fn fills_for(&mut self, position_id: i64) -> Result<Vec<FillRow>, StoreError> {
        let sql = format!(
            "SELECT id, side, quantity, price, filled_at, journal_ref, simulated \
             FROM {} WHERE user_id = ? AND position_id = ? ORDER BY id",
            self.t("sw_fill")
        );
        let rows = self
            .rows(&sql, &[Param::Int(self.user_id), Param::Int(position_id)])
            .await?;
        Ok(rows
            .iter()
            .map(|r| FillRow {
                id: int(col(r, "id")),
                side: text(col(r, "side")),
                quantity: int(col(r, "quantity")),
                price: dec(col(r, "price"), "price"),
                filled_at: stamp(col(r, "filled_at")),
                journal_ref: opt_text(col(r, "journal_ref")),
                simulated: boolean(col(r, "simulated")),
            })
            .collect())
    }

    /// `session(day)`.
    ///
    /// # Errors
    /// The driver's.
    pub async fn session(&mut self, day: NaiveDate) -> Result<Option<SessionRow>, StoreError> {
        let sql = format!(
            "SELECT session_date, mode, monitor_ran, signals, confirms, fills, manage_actions, \
             notes FROM {} WHERE user_id = ? AND session_date = ?",
            self.t("sw_session")
        );
        let row = self
            .one(&sql, &[Param::Int(self.user_id), day_param(day)])
            .await?;
        Ok(row.map(|r| SessionRow {
            session_date: date(col(&r, "session_date")),
            mode: text(col(&r, "mode")),
            monitor_ran: boolean(col(&r, "monitor_ran")),
            signals: int(col(&r, "signals")),
            confirms: int(col(&r, "confirms")),
            fills: int(col(&r, "fills")),
            manage_actions: int(col(&r, "manage_actions")),
            notes: opt_text(col(&r, "notes")),
        }))
    }

    /// `set_first_live_sessions_left(value)` (C1's write; nothing in execute calls it).
    ///
    /// # Errors
    /// The driver's.
    pub async fn set_first_live_sessions_left(&mut self, value: i64) -> Result<(), StoreError> {
        let sql = format!(
            "UPDATE {} SET first_live_sessions_left = ?, \
             updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            self.t("sw_config")
        );
        self.conn
            .execute(&sql, &[Param::Int(value.max(0)), Param::Int(self.user_id)])
            .await?;
        Ok(())
    }

    /// `signals_for(day)`: today's signals, newest first, with the watch's focus.
    ///
    /// # Errors
    /// The driver's.
    pub async fn signals_for(&mut self, day: NaiveDate) -> Result<Vec<SignalRow>, StoreError> {
        let sql = format!(
            "SELECT s.id, s.instrument_id, i.symbol, s.setup, s.session_date, s.raised_at, \
             s.state, s.or_window_minutes, s.range_high, s.range_low, s.low_of_day, \
             s.last_price, s.entry, s.stop, s.plan_line_id, \
             (SELECT w.focus FROM {} w WHERE w.id = s.watch_id) AS focus \
             FROM {} s JOIN {} i ON i.id = s.instrument_id \
             WHERE s.user_id = ? AND s.session_date = ? ORDER BY s.raised_at DESC, s.id DESC",
            self.t("sw_watch"),
            self.t("sw_signal"),
            self.t("instrument")
        );
        let rows = self
            .rows(&sql, &[Param::Int(self.user_id), day_param(day)])
            .await?;
        Ok(rows
            .iter()
            .map(|r| SignalRow {
                id: int(col(r, "id")),
                instrument_id: int(col(r, "instrument_id")),
                symbol: text(col(r, "symbol")),
                setup: text(col(r, "setup")),
                session_date: date(col(r, "session_date")),
                raised_at: stamp(col(r, "raised_at")),
                state: text(col(r, "state")),
                or_window_minutes: opt_int(col(r, "or_window_minutes")),
                range_high: dec(col(r, "range_high"), "range_high"),
                range_low: dec(col(r, "range_low"), "range_low"),
                low_of_day: dec(col(r, "low_of_day"), "low_of_day"),
                last_price: dec(col(r, "last_price"), "last_price"),
                entry: dec(col(r, "entry"), "entry"),
                stop: dec(col(r, "stop"), "stop"),
                plan_line_id: opt_int(col(r, "plan_line_id")),
                focus: boolean(col(r, "focus")),
            })
            .collect())
    }

    /// `signal_skip_for(instrument_id, day)`.
    ///
    /// # Errors
    /// The driver's.
    pub async fn signal_skip_for(
        &mut self,
        instrument_id: i64,
        day: NaiveDate,
    ) -> Result<Option<SignalSkip>, StoreError> {
        let sql = format!(
            "SELECT k.reason, k.detail, p.plan_id FROM {} k \
             JOIN {} p ON p.id = k.plan_id \
             WHERE k.user_id = ? AND k.instrument_id = ? AND p.as_of = ? AND p.source = 'SIGNAL' \
             ORDER BY p.built_at DESC, k.id DESC LIMIT 1",
            self.t("sw_plan_skip"),
            self.t("sw_plan")
        );
        let row = self
            .one(
                &sql,
                &[
                    Param::Int(self.user_id),
                    Param::Int(instrument_id),
                    day_param(day),
                ],
            )
            .await?;
        Ok(row.map(|r| SignalSkip {
            reason: text(col(&r, "reason")),
            detail: opt_text(col(&r, "detail")),
            plan_id: text(col(&r, "plan_id")),
        }))
    }

    /// `latest_plan(source)`.
    ///
    /// # Errors
    /// The driver's.
    pub async fn latest_plan(&mut self, source: &str) -> Result<Option<PlanRow>, StoreError> {
        let sql = format!(
            "{}WHERE user_id = ? AND source = ? ORDER BY built_at DESC, id DESC LIMIT 1",
            self.plan_select()
        );
        let row = self
            .one(
                &sql,
                &[Param::Int(self.user_id), Param::Text(source.to_owned())],
            )
            .await?;
        Ok(row.as_ref().map(plan_row))
    }

    /// `lines_for(plan_pk)`.
    ///
    /// # Errors
    /// The driver's.
    pub async fn lines_for(&mut self, plan_pk: i64) -> Result<Vec<LineRow>, StoreError> {
        let sql = format!(
            "{LINE_SELECT}{}WHERE l.user_id = ? AND l.plan_id = ? ORDER BY l.id",
            self.line_from()
        );
        let rows = self
            .rows(&sql, &[Param::Int(self.user_id), Param::Int(plan_pk)])
            .await?;
        Ok(rows.iter().map(line_row).collect())
    }

    /// `skips_for(plan_pk)`.
    ///
    /// # Errors
    /// The driver's.
    pub async fn skips_for(&mut self, plan_pk: i64) -> Result<Vec<SkipRow>, StoreError> {
        let sql = format!(
            "SELECT id, instrument_id, symbol, reason, detail FROM {} \
             WHERE user_id = ? AND plan_id = ? ORDER BY id",
            self.t("sw_plan_skip")
        );
        let rows = self
            .rows(&sql, &[Param::Int(self.user_id), Param::Int(plan_pk)])
            .await?;
        Ok(rows
            .iter()
            .map(|r| SkipRow {
                id: int(col(r, "id")),
                instrument_id: opt_int(col(r, "instrument_id")),
                symbol: text(col(r, "symbol")),
                reason: text(col(r, "reason")),
                detail: opt_text(col(r, "detail")),
            })
            .collect())
    }

    /// `latest_scan_run()`.
    ///
    /// # Errors
    /// The driver's.
    pub async fn latest_scan_run(&mut self) -> Result<Option<ScanRun>, StoreError> {
        let sql = format!(
            "SELECT id, requested_at, started_at, finished_at, session_date, provisional, \
             status, detail, error, task_id FROM {} \
             WHERE user_id = ? ORDER BY requested_at DESC, id DESC LIMIT 1",
            self.t("sw_scan_run")
        );
        let row = self.one(&sql, &[Param::Int(self.user_id)]).await?;
        Ok(row.as_ref().map(scan_run_row))
    }

    /// `latest_market_scan()`.
    ///
    /// # Errors
    /// The driver's.
    pub async fn latest_market_scan(&mut self) -> Result<Option<MarketScan>, StoreError> {
        let sql = format!(
            "SELECT date, provisional, detail FROM {} \
             WHERE user_id = ? ORDER BY date DESC LIMIT 1",
            self.t("sw_market_daily")
        );
        let Some(row) = self.one(&sql, &[Param::Int(self.user_id)]).await? else {
            return Ok(None);
        };
        let detail = json_column(col(&row, "detail"));
        let scanned_at = match &detail {
            Some(PyValue::Map(m)) => match m.get("scan") {
                Some(PyValue::Map(scan)) => match scan.get("scanned_at") {
                    Some(PyValue::Str(s)) => {
                        super::values::parse_iso(s).map(|t| t.with_timezone(&crate::clock::ist()))
                    }
                    _ => None,
                },
                _ => None,
            },
            _ => None,
        };
        Ok(Some(MarketScan {
            date: date(col(&row, "date")),
            provisional: boolean(col(&row, "provisional")),
            scanned_at,
        }))
    }

    /// `request_scan(...)`: one `QUEUED` run, or the API's 409 / 429.
    ///
    /// # Errors
    /// The refusal, or the driver's.
    pub async fn request_scan(
        &mut self,
        now: DateTime<FixedOffset>,
        min_interval: chrono::Duration,
        stale_after: chrono::Duration,
        source: &str,
    ) -> Result<Result<i64, ScanRefused>, StoreError> {
        if let Some(newest) = self.latest_scan_run().await?
            && let Some(requested) = newest.requested_at
        {
            let in_flight = newest.status == "QUEUED" || newest.status == "RUNNING";
            if in_flight && requested > now - stale_after {
                return Ok(Err(ScanRefused {
                    status: 409,
                    reason: format!(
                        "Scan {} is {}; its result is on its way.",
                        newest.id,
                        newest.status.to_lowercase()
                    ),
                    run_id: newest.id,
                    retry_after: None,
                }));
            }
            if requested > now - min_interval {
                let wait = min_interval - (now - requested);
                #[allow(clippy::cast_possible_truncation)]
                let seconds =
                    1_i64.max((wait.num_microseconds().unwrap_or(0) as f64 / 1e6 + 0.999) as i64);
                #[allow(clippy::cast_possible_truncation)]
                let ago = ((now - requested).num_microseconds().unwrap_or(0) as f64 / 1e6) as i64;
                return Ok(Err(ScanRefused {
                    status: 429,
                    reason: format!(
                        "A scan was requested {ago} s ago; one a minute is the limit. Try again \
                         in {seconds} s."
                    ),
                    run_id: newest.id,
                    retry_after: Some(seconds),
                }));
            }
        }
        let sql = format!(
            "INSERT INTO {} (user_id, requested_at, status, detail) \
             VALUES (?, ?, 'QUEUED', ?) RETURNING id",
            self.t("sw_scan_run")
        );
        let detail = format!("{{\"source\": {}}}", baskfy_providers::py::json_str(source));
        let row = self
            .one(
                &sql,
                &[
                    Param::Int(self.user_id),
                    Param::Text(iso_dt(&now)),
                    Param::Text(detail),
                ],
            )
            .await?;
        Ok(Ok(row.map_or(0, |r| int(col(&r, "id")))))
    }

    /// `catalysts_for(instrument_ids)` (SW11B).
    ///
    /// # Errors
    /// The driver's.
    pub async fn catalysts_for(
        &mut self,
        instrument_ids: &[i64],
    ) -> Result<std::collections::BTreeMap<i64, Catalyst>, StoreError> {
        let mut wanted: Vec<i64> = instrument_ids.to_vec();
        wanted.sort_unstable();
        wanted.dedup();
        let mut out = std::collections::BTreeMap::new();
        if wanted.is_empty() {
            return Ok(out);
        }
        let marks = vec!["?"; wanted.len()].join(", ");
        let sql = format!(
            "SELECT instrument_id, headline, published_at, url, source, earnings_date \
             FROM {} WHERE user_id = ? AND instrument_id IN ({marks}) \
             ORDER BY instrument_id, (published_at IS NULL), published_at DESC, id DESC",
            self.t("sw_catalyst")
        );
        let mut params = vec![Param::Int(self.user_id)];
        params.extend(wanted.iter().map(|i| Param::Int(*i)));
        for r in self.rows(&sql, &params).await? {
            let view: &mut Catalyst = out.entry(int(col(&r, "instrument_id"))).or_default();
            if text(col(&r, "source")) == "NSE_EVENT_CALENDAR" {
                if view.earnings_date.is_none() {
                    view.earnings_date = date(col(&r, "earnings_date"));
                }
            } else if view.url.is_none() {
                view.headline = Some(text(col(&r, "headline")));
                view.published_at = stamp(col(&r, "published_at"));
                view.url = Some(text(col(&r, "url")));
            }
        }
        Ok(out)
    }

    /// `recent_manage_actions(limit)`.
    ///
    /// # Errors
    /// The driver's.
    pub async fn recent_manage_actions(&mut self, limit: i64) -> Result<Vec<LineRow>, StoreError> {
        let sql = format!(
            "{LINE_SELECT}{}WHERE l.user_id = ? AND l.kind IN ('SELL_AT_OPEN', 'RAISE_GTT_STOP') \
             ORDER BY l.updated_at DESC, l.id DESC LIMIT ?",
            self.line_from()
        );
        let rows = self
            .rows(&sql, &[Param::Int(self.user_id), Param::Int(limit)])
            .await?;
        Ok(rows.iter().map(line_row).collect())
    }
}

#[async_trait]
impl SwingStore for PgSwingStore {
    async fn plan(&mut self, plan_id: &str) -> Result<Option<PlanRow>, StoreError> {
        if !is_uuid(plan_id) {
            return Ok(None);
        }
        let sql = format!("{}WHERE user_id = ? AND plan_id = ?", self.plan_select());
        let row = self
            .one(
                &sql,
                &[Param::Int(self.user_id), Param::Text(plan_id.to_owned())],
            )
            .await?;
        Ok(row.as_ref().map(plan_row))
    }

    async fn line(&mut self, line_id: i64) -> Result<Option<LineRow>, StoreError> {
        let sql = format!(
            "{LINE_SELECT}{}WHERE l.user_id = ? AND l.id = ?",
            self.line_from()
        );
        let row = self
            .one(&sql, &[Param::Int(self.user_id), Param::Int(line_id)])
            .await?;
        Ok(row.as_ref().map(line_row))
    }

    async fn set_line(
        &mut self,
        line_id: i64,
        state: &str,
        journal_ref: Option<&str>,
        position_id: Option<i64>,
    ) -> Result<(), StoreError> {
        let mut sets = vec!["state = ?"];
        let mut params = vec![Param::Text(state.to_owned())];
        if let Some(r) = journal_ref {
            sets.push("journal_ref = ?");
            params.push(Param::Text(r.to_owned()));
        }
        if let Some(p) = position_id {
            sets.push("position_id = ?");
            params.push(Param::Int(p));
        }
        sets.push("updated_at = CURRENT_TIMESTAMP");
        params.push(Param::Int(self.user_id));
        params.push(Param::Int(line_id));
        let sql = format!(
            "UPDATE {} SET {} WHERE user_id = ? AND id = ?",
            self.t("sw_plan_line"),
            sets.join(", ")
        );
        self.conn.execute(&sql, &params).await?;
        Ok(())
    }

    async fn open_position_for(
        &mut self,
        instrument_id: i64,
    ) -> Result<Option<PositionRow>, StoreError> {
        let sql = format!(
            "{POSITION_SELECT}{}WHERE p.user_id = ? AND p.instrument_id = ? AND p.state IN ('OPEN', 'PARTIAL') \
             AND p.quantity_open > 0 ORDER BY p.id DESC LIMIT 1",
            self.position_from()
        );
        let row = self
            .one(&sql, &[Param::Int(self.user_id), Param::Int(instrument_id)])
            .await?;
        Ok(row.as_ref().map(position_row))
    }

    async fn position(&mut self, position_id: i64) -> Result<Option<PositionRow>, StoreError> {
        let sql = format!(
            "{POSITION_SELECT}{}WHERE p.user_id = ? AND p.id = ?",
            self.position_from()
        );
        let row = self
            .one(&sql, &[Param::Int(self.user_id), Param::Int(position_id)])
            .await?;
        Ok(row.as_ref().map(position_row))
    }

    async fn create_position(&mut self, fields: Fields) -> Result<i64, StoreError> {
        let mut values: Fields = fields
            .into_iter()
            .filter(|(k, _)| *k != "symbol" && *k != "id")
            .collect();
        if let Some(bad) = unknown_columns(&values, &POSITION_COLUMNS) {
            return Err(StoreError::Value(format!(
                "sw_position has no column {bad}"
            )));
        }
        let has = |values: &Fields, k: &str| values.iter().any(|(c, _)| *c == k);
        if !has(&values, "broker_account_id") {
            values.push((
                "broker_account_id",
                self.broker_account_id.map_or(Val::Null, Val::Int),
            ));
        }
        if !has(&values, "quantity_open") {
            let entered = values
                .iter()
                .find(|(c, _)| *c == "quantity_entered")
                .map_or(Val::Null, |(_, v)| v.clone());
            values.push(("quantity_open", entered));
        }
        if !has(&values, "state") {
            values.push(("state", Val::Text("OPEN".to_owned())));
        }
        if !has(&values, "simulated") {
            values.push(("simulated", Val::Bool(true)));
        }
        if !has(&values, "partial_done") {
            values.push(("partial_done", Val::Bool(false)));
        }
        let mut columns = vec!["user_id"];
        columns.extend(values.iter().map(|(k, _)| *k));
        let mut params = vec![Param::Int(self.user_id)];
        params.extend(values.iter().map(|(k, v)| bind(v, k)));
        let sql = format!(
            "INSERT INTO {} ({}) VALUES ({}) RETURNING id",
            self.t("sw_position"),
            columns.join(", "),
            vec!["?"; columns.len()].join(", ")
        );
        let row = self.one(&sql, &params).await?;
        Ok(row.map_or(0, |r| int(col(&r, "id"))))
    }

    async fn update_position(
        &mut self,
        position_id: i64,
        fields: Fields,
    ) -> Result<(), StoreError> {
        if let Some(bad) = unknown_columns(&fields, &POSITION_COLUMNS) {
            return Err(StoreError::Value(format!(
                "sw_position has no column {bad}"
            )));
        }
        if fields.is_empty() {
            return Ok(());
        }
        let mut sets: Vec<String> = fields.iter().map(|(k, _)| format!("{k} = ?")).collect();
        sets.push("updated_at = CURRENT_TIMESTAMP".to_owned());
        let mut params: Vec<Param> = fields.iter().map(|(k, v)| bind(v, k)).collect();
        params.push(Param::Int(self.user_id));
        params.push(Param::Int(position_id));
        let sql = format!(
            "UPDATE {} SET {} WHERE user_id = ? AND id = ?",
            self.t("sw_position"),
            sets.join(", ")
        );
        self.conn.execute(&sql, &params).await?;
        Ok(())
    }

    async fn add_fill(&mut self, fields: Fields) -> Result<i64, StoreError> {
        if let Some(bad) = unknown_columns(&fields, &FILL_COLUMNS) {
            return Err(StoreError::Value(format!("sw_fill has no column {bad}")));
        }
        let mut values = fields;
        if !values.iter().any(|(k, _)| *k == "simulated") {
            values.push(("simulated", Val::Bool(true)));
        }
        let mut columns = vec!["user_id"];
        columns.extend(values.iter().map(|(k, _)| *k));
        let mut params = vec![Param::Int(self.user_id)];
        params.extend(values.iter().map(|(k, v)| bind(v, k)));
        let sql = format!(
            "INSERT INTO {} ({}) VALUES ({}) RETURNING id",
            self.t("sw_fill"),
            columns.join(", "),
            vec!["?"; columns.len()].join(", ")
        );
        let row = self.one(&sql, &params).await?;
        Ok(row.map_or(0, |r| int(col(&r, "id"))))
    }

    async fn bump_session(
        &mut self,
        day: NaiveDate,
        mode: &str,
        confirms: i64,
        fills: i64,
        manage_actions: i64,
    ) -> Result<(), StoreError> {
        let sql = format!(
            "INSERT INTO {} AS s (user_id, session_date, mode, monitor_ran, \
             signals, confirms, fills, manage_actions, notes) \
             VALUES (?, ?, ?, false, 0, ?, ?, ?, 'swing-desk') \
             ON CONFLICT (user_id, session_date) DO UPDATE SET \
             confirms = s.confirms + EXCLUDED.confirms, fills = s.fills + EXCLUDED.fills, \
             manage_actions = s.manage_actions + EXCLUDED.manage_actions, mode = EXCLUDED.mode",
            self.t("sw_session")
        );
        self.conn
            .execute(
                &sql,
                &[
                    Param::Int(self.user_id),
                    day_param(day),
                    Param::Text(mode.to_owned()),
                    Param::Int(confirms),
                    Param::Int(fills),
                    Param::Int(manage_actions),
                ],
            )
            .await?;
        Ok(())
    }

    async fn note_session(&mut self, day: NaiveDate, note: &str) -> Result<(), StoreError> {
        let sql = format!(
            "INSERT INTO {} AS s (user_id, session_date, mode, monitor_ran, \
             signals, confirms, fills, manage_actions, notes) \
             VALUES (?, ?, ?, false, 0, 0, 0, 0, ?) \
             ON CONFLICT (user_id, session_date) DO UPDATE SET \
             notes = COALESCE(s.notes, '') || ?",
            self.t("sw_session")
        );
        self.conn
            .execute(
                &sql,
                &[
                    Param::Int(self.user_id),
                    day_param(day),
                    Param::Text("DRY_RUN".to_owned()),
                    Param::Text(note.to_owned()),
                    Param::Text(format!("\n{note}")),
                ],
            )
            .await?;
        Ok(())
    }

    async fn config(&mut self) -> Result<SleeveConfig, StoreError> {
        let sql = format!(
            "SELECT sleeve_capital_inr, risk_per_trade_pct, max_position_pct, \
             max_open_positions, first_live_sessions_left, exposure_level \
             FROM {} WHERE user_id = ?",
            self.t("sw_config")
        );
        let Some(r) = self.one(&sql, &[Param::Int(self.user_id)]).await? else {
            let sizing = baskfy_core::swing::sizing::SizingConfig::default();
            return Ok(SleeveConfig {
                sleeve_capital_inr: Decimal::ZERO,
                risk_per_trade_pct: super::rules::dec_of_float(sizing.risk_per_trade_pct),
                max_position_pct: super::rules::dec_of_float(sizing.max_position_pct),
                max_open_positions: sizing.max_open_positions,
                first_live_sessions_left: 5,
                exposure_level: 0,
                present: false,
            });
        };
        Ok(SleeveConfig {
            sleeve_capital_inr: dec(col(&r, "sleeve_capital_inr"), "sleeve_capital_inr")
                .unwrap_or_default(),
            risk_per_trade_pct: dec(col(&r, "risk_per_trade_pct"), "risk_per_trade_pct")
                .unwrap_or_default(),
            max_position_pct: dec(col(&r, "max_position_pct"), "max_position_pct")
                .unwrap_or_default(),
            max_open_positions: int(col(&r, "max_open_positions")),
            first_live_sessions_left: int(col(&r, "first_live_sessions_left")),
            exposure_level: int(col(&r, "exposure_level")),
            present: true,
        })
    }

    async fn open_positions(&mut self) -> Result<Vec<PositionRow>, StoreError> {
        let sql = format!(
            "{POSITION_SELECT}{}WHERE p.user_id = ? AND p.state IN ('OPEN', 'PARTIAL') AND p.quantity_open > 0 \
             ORDER BY (p.gtt_id IS NULL) DESC, p.entry_date, p.id",
            self.position_from()
        );
        let rows = self.rows(&sql, &[Param::Int(self.user_id)]).await?;
        Ok(rows.iter().map(position_row).collect())
    }

    async fn open_protection_issues(&mut self) -> Result<Vec<ProtectionIssue>, StoreError> {
        let sql = format!(
            "SELECT symbol, kind, detail, position_id FROM {} \
             WHERE user_id = ? AND sleeve = ? AND resolved_at IS NULL ORDER BY seen_at, id",
            self.t("lv_protection_issue")
        );
        let rows = self
            .rows(
                &sql,
                &[Param::Int(self.user_id), Param::Text("swing".to_owned())],
            )
            .await?;
        Ok(rows
            .iter()
            .map(|r| ProtectionIssue {
                symbol: text(col(r, "symbol")),
                kind: text(col(r, "kind")),
                detail: text(col(r, "detail")),
                position_id: int(col(r, "position_id")),
            })
            .collect())
    }

    async fn lock_begin(&mut self, day: NaiveDate) -> Result<(), StoreError> {
        let sqlite = !self.conn.is_postgres();
        let mode = if self.settings.dry_run || !self.settings.swing_execution_enabled {
            "DRY_RUN"
        } else {
            "LIVE"
        };
        let table = self.t("sw_session");
        let params = [
            Param::Int(self.user_id),
            day_param(day),
            Param::Text(mode.to_owned()),
        ];
        if sqlite {
            self.conn.execute("BEGIN IMMEDIATE", &[]).await?;
            let insert = format!(
                "INSERT INTO {table} (user_id, session_date, mode, monitor_ran, \
                 signals, confirms, fills, manage_actions, notes) \
                 VALUES (?, ?, ?, false, 0, 0, 0, 0, 'swing-desk') \
                 ON CONFLICT (user_id, session_date) DO NOTHING"
            );
            if let Err(e) = self.conn.execute(&insert, &params).await {
                let _ = self.conn.execute("ROLLBACK", &[]).await;
                return Err(e.into());
            }
            return Ok(());
        }
        // Outside the transaction, only when absent: a racing confirm never waits on the holder.
        let insert = format!(
            "INSERT INTO {table} (user_id, session_date, mode, monitor_ran, \
             signals, confirms, fills, manage_actions, notes) \
             SELECT ?, ?, ?, false, 0, 0, 0, 0, 'swing-desk' WHERE NOT EXISTS (\
             SELECT 1 FROM {table} WHERE user_id = ? AND session_date = ?) \
             ON CONFLICT (user_id, session_date) DO NOTHING"
        );
        let mut insert_params = params.to_vec();
        insert_params.extend([Param::Int(self.user_id), day_param(day)]);
        self.conn.execute(&insert, &insert_params).await?;
        self.conn.execute("BEGIN", &[]).await?;
        let lock = format!(
            "SELECT session_date FROM {table} \
             WHERE user_id = ? AND session_date = ? FOR UPDATE NOWAIT"
        );
        if let Err(e) = self
            .conn
            .execute(&lock, &[Param::Int(self.user_id), day_param(day)])
            .await
        {
            let _ = self.conn.execute("ROLLBACK", &[]).await;
            if let StorageError::Driver(sqlx::Error::Database(db)) = &e
                && db.code().as_deref() == Some(LOCK_NOT_AVAILABLE)
            {
                return Err(StoreError::SessionBusy);
            }
            return Err(e.into());
        }
        Ok(())
    }

    async fn lock_commit(&mut self) -> Result<(), StoreError> {
        self.conn.execute("COMMIT", &[]).await?;
        Ok(())
    }

    async fn lock_rollback(&mut self) -> Result<(), StoreError> {
        self.conn.execute("ROLLBACK", &[]).await?;
        Ok(())
    }

    async fn session_context(&mut self, day: NaiveDate) -> Result<SignalContext, StoreError> {
        let now = self.clock.now_ist();
        let slug = self.settings.swing_index_slug.clone();
        let live = self.live_level.clone();
        Ok(load_context(
            &mut self.conn,
            self.user_id,
            day,
            &self.schema.clone(),
            live.as_deref(),
            Some(now),
            &slug,
        )
        .await?)
    }

    async fn resize_line(
        &mut self,
        line_id: i64,
        quantity: i64,
        risk_inr: Decimal,
        position_value: Decimal,
        note: &str,
    ) -> Result<(), StoreError> {
        let sql = format!(
            "UPDATE {} SET quantity = ?, risk_inr = ?, position_value = ?, \
             note = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND id = ?",
            self.t("sw_plan_line")
        );
        self.conn
            .execute(
                &sql,
                &[
                    Param::Int(quantity),
                    bind(&Val::Dec(risk_inr), "risk_inr"),
                    bind(&Val::Dec(position_value), "position_value"),
                    Param::Text(note.to_owned()),
                    Param::Int(self.user_id),
                    Param::Int(line_id),
                ],
            )
            .await?;
        Ok(())
    }

    async fn range_high_for(&mut self, line_id: i64) -> Result<Option<Decimal>, StoreError> {
        let sql = format!(
            "SELECT range_high FROM {} \
             WHERE user_id = ? AND plan_line_id = ? ORDER BY id DESC LIMIT 1",
            self.t("sw_signal")
        );
        let row = self
            .one(&sql, &[Param::Int(self.user_id), Param::Int(line_id)])
            .await?;
        Ok(row.and_then(|r| dec(col(&r, "range_high"), "range_high")))
    }

    async fn line_by_order(&mut self, order_id: &str) -> Result<Option<LineRow>, StoreError> {
        let sql = format!(
            "{LINE_SELECT}{}WHERE l.user_id = ? AND l.kind = 'BUY_ON_TRIGGER' AND l.state = 'SENT' \
             AND l.journal_ref = ? ORDER BY l.id DESC LIMIT 1",
            self.line_from()
        );
        let row = self
            .one(
                &sql,
                &[Param::Int(self.user_id), Param::Text(order_id.to_owned())],
            )
            .await?;
        Ok(row.as_ref().map(line_row))
    }

    async fn sent_buy_lines(&mut self, day: NaiveDate) -> Result<Vec<LineRow>, StoreError> {
        let sql = format!(
            "{LINE_SELECT}{}WHERE l.user_id = ? AND p.as_of = ? AND l.kind = 'BUY_ON_TRIGGER' \
             AND l.state = 'SENT' ORDER BY l.id",
            self.line_from()
        );
        let rows = self
            .rows(&sql, &[Param::Int(self.user_id), day_param(day)])
            .await?;
        Ok(rows.iter().map(line_row).collect())
    }

    async fn note_line(&mut self, line_id: i64, note: &str) -> Result<(), StoreError> {
        let sql = format!(
            "UPDATE {} SET note = COALESCE(note || '; ', '') || ?, \
             updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND id = ?",
            self.t("sw_plan_line")
        );
        self.conn
            .execute(
                &sql,
                &[
                    Param::Text(note.to_owned()),
                    Param::Int(self.user_id),
                    Param::Int(line_id),
                ],
            )
            .await?;
        Ok(())
    }

    async fn expire_pending(&mut self, day: NaiveDate) -> Result<i64, StoreError> {
        let sql = format!(
            "UPDATE {} SET state = 'EXPIRED', \
             note = COALESCE(note || '; ', '') || 'slot freed at 10:45', \
             updated_at = CURRENT_TIMESTAMP \
             WHERE user_id = ? AND kind = 'PENDING_RANGE' AND state = 'PROPOSED' AND plan_id IN \
             (SELECT id FROM {} WHERE user_id = ? AND as_of = ?) RETURNING id",
            self.t("sw_plan_line"),
            self.t("sw_plan")
        );
        let rows = self
            .rows(
                &sql,
                &[
                    Param::Int(self.user_id),
                    Param::Int(self.user_id),
                    day_param(day),
                ],
            )
            .await?;
        Ok(i64::try_from(rows.len()).unwrap_or(i64::MAX))
    }
}

/// A `DbValue` holder so callers outside the store can read a column.
#[must_use]
pub fn value_text(v: Option<&DbValue>) -> String {
    text(v)
}
