//! `app/twt_desk.py::PgTwtStore` and the `TwtStore` protocol (`app/twt_execute.py`): one user's
//! `tw_` rows through the desk's connection, SQL text unchanged (SQLite dialect with `?`, run on
//! Postgres through `storage`'s translation, exactly as `analytics.pg` runs it).

use async_trait::async_trait;
use chrono::{DateTime, Duration, FixedOffset, NaiveDate};
use rust_decimal::Decimal;

use super::common::{
    Rec, Val, db_bool, db_date, db_dec, db_int, db_raw, db_stamp, dstr, ist_day, param,
};
use crate::storage::{Conn, DbValue, Param, Row, StorageError};

/// `KIND_ORDER`: `baskfy_core.twt.plan.LINE_ORDER`'s values — risk off before risk on.
pub const KIND_ORDER: [&str; 4] = ["SELL_AT_OPEN", "ARM_GTT", "RAISE_GTT_STOP", "BUY_AT_OPEN"];

/// `ENTERED_ORDER_STATES`: the `tw_order.state`s that have spoken for one of `04` §6.3's three.
pub const ENTERED_ORDER_STATES: [&str; 4] = ["CONFIRMED", "SENT", "PARTIAL", "FILLED"];

/// TW12's in-flight states.
pub const SCAN_IN_FLIGHT: [&str; 2] = ["QUEUED", "RUNNING"];

/// `ScanRefused`: `request_scan`'s two refusals, with the status the route answers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScanRefused {
    /// 409 or 429.
    pub status: u16,
    /// The words.
    pub reason: String,
    /// The run it names.
    pub run_id: i64,
    /// `Retry-After` seconds (429 only).
    pub retry_after: Option<i64>,
}

/// Why a store call failed: the database, or one of `request_scan`'s refusals.
#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    /// The database (a 500 at the route, as Python's driver error is).
    #[error(transparent)]
    Storage(#[from] StorageError),
    /// `ScanRefused`.
    #[error("{}", .0.reason)]
    Scan(ScanRefused),
    /// A row the code cannot read (Python's `KeyError`/`ValueError`).
    #[error("{0}")]
    Bad(String),
    /// `HTTPException(409, SESSION_BUSY)`: the session row is held by another confirm
    /// (`FOR UPDATE NOWAIT`, R8.TD1).
    #[error("{SESSION_BUSY}")]
    SessionBusy,
}

/// Postgres's SQLSTATE for `FOR UPDATE NOWAIT` finding the row held (`LOCK_NOT_AVAILABLE`).
pub const LOCK_NOT_AVAILABLE: &str = "55P03";

/// `SESSION_BUSY`: what a confirm that finds the session row held answers (409, R8.TD1).
pub const SESSION_BUSY: &str = "SESSION_BUSY: another confirm of this session is in flight; \
     nothing was sent \u{2014} confirm again";

/// What the store calls return.
pub type StoreResult<T> = Result<T, StoreError>;

/// `SleeveMoney`: `04` §9 as the confirm needs it, re-read under the lock.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SleeveMoney {
    /// `equity_inr`.
    pub equity_inr: Decimal,
    /// `cash_available_inr`.
    pub cash_available_inr: Decimal,
    /// `open_exposure_inr`.
    pub open_exposure_inr: Decimal,
    /// `first_live_entries_left`.
    pub first_live_entries_left: i64,
    /// `max_open_positions`.
    pub max_open_positions: i64,
    /// `max_position_pct`.
    pub max_position_pct: Decimal,
    /// `stop_pct`.
    pub stop_pct: Decimal,
    /// `trail_pct`.
    pub trail_pct: Decimal,
}

impl SleeveMoney {
    /// `dataclasses.asdict(money)`.
    #[must_use]
    pub fn as_rec(&self) -> Rec {
        Rec::of([
            ("equity_inr", Val::Dec(self.equity_inr)),
            ("cash_available_inr", Val::Dec(self.cash_available_inr)),
            ("open_exposure_inr", Val::Dec(self.open_exposure_inr)),
            (
                "first_live_entries_left",
                Val::Int(self.first_live_entries_left),
            ),
            ("max_open_positions", Val::Int(self.max_open_positions)),
            ("max_position_pct", Val::Dec(self.max_position_pct)),
            ("stop_pct", Val::Dec(self.stop_pct)),
            ("trail_pct", Val::Dec(self.trail_pct)),
        ])
    }
}

/// `twt_execute.sleeve_money_from_rows(config, positions, marks, realised)`: `04` §9.1–9.2 over
/// rows already read. The arithmetic is `baskfy_core::twt::sleeve::sleeve_value`.
///
/// # Errors
/// Arithmetic the core refuses.
pub fn sleeve_money_from_rows(
    config: &Rec,
    positions: &[Rec],
    marks: &[(i64, Decimal)],
    realised: Decimal,
) -> StoreResult<SleeveMoney> {
    use baskfy_core::twt::sleeve::{MarkSource, OpenPositionValue, sleeve_value};
    let mark_of = |id: i64| marks.iter().find(|(i, _)| *i == id).map(|(_, m)| *m);
    let mut valued = Vec::with_capacity(positions.len());
    for row in positions {
        let id = row.int("instrument_id");
        let entry = row
            .dec("entry_avg")
            .ok_or_else(|| StoreError::Bad("entry_avg".to_owned()))?;
        let mark = mark_of(id);
        valued.push(OpenPositionValue {
            instrument_id: id,
            quantity_open: row.int("quantity_open"),
            entry_avg: entry,
            mark: mark.unwrap_or(entry),
            mark_source: if mark.is_some() {
                MarkSource::SessionClose
            } else {
                MarkSource::Entry
            },
        });
    }
    let or_dec = |key: &str, default: &str| -> Decimal {
        if config.truthy(key) {
            config.dec(key).unwrap_or_default()
        } else {
            super::common::py_decimal(default).unwrap_or_default()
        }
    };
    let capital = if config.truthy("sleeve_capital_inr") {
        config.dec("sleeve_capital_inr").unwrap_or_default()
    } else {
        Decimal::ZERO
    };
    let bad = |e: baskfy_core::CoreError| StoreError::Bad(e.to_string());
    let value = sleeve_value(capital, realised, &valued)
        .and_then(|v| v.quantize())
        .map_err(bad)?;
    Ok(SleeveMoney {
        equity_inr: value.equity_inr().map_err(bad)?,
        cash_available_inr: value.cash_available_inr().map_err(bad)?,
        open_exposure_inr: value.open_exposure_inr(),
        first_live_entries_left: config.int("first_live_entries_left"),
        max_open_positions: if config.truthy("max_open_positions") {
            config.int("max_open_positions")
        } else {
            10
        },
        max_position_pct: or_dec("max_position_pct", "12.50"),
        stop_pct: or_dec("stop_pct", "20.00"),
        trail_pct: or_dec("trail_pct", "20.00"),
    })
}

/// The `TwtStore` protocol plus the page's own reads (`PgTwtStore`'s). All prices `Decimal`.
#[async_trait]
pub trait TwtStore: Send {
    /// `plan(plan_id)`: `None` for an unknown plan, including a malformed id.
    async fn plan(&mut self, plan_id: &str) -> StoreResult<Option<Rec>>;
    /// `line(line_id)`.
    async fn line(&mut self, line_id: i64) -> StoreResult<Option<Rec>>;
    /// `set_line(line_id, state=..., journal_ref=..., order_id=..., position_id=..., note=...)`.
    async fn set_line(&mut self, line_id: i64, state: &str, extra: LineUpdate) -> StoreResult<()>;
    /// `open_position_for(instrument_id)`.
    async fn open_position_for(&mut self, instrument_id: i64) -> StoreResult<Option<Rec>>;
    /// `position(position_id)`.
    async fn position(&mut self, position_id: i64) -> StoreResult<Option<Rec>>;
    /// `open_positions()`: the sleeve's own book, and nothing else.
    async fn open_positions(&mut self) -> StoreResult<Vec<Rec>>;
    /// `create_position(fields)`.
    async fn create_position(&mut self, fields: Rec) -> StoreResult<i64>;
    /// `update_position(position_id, fields)`.
    async fn update_position(&mut self, position_id: i64, fields: Rec) -> StoreResult<()>;
    /// `add_fill(fields)`.
    async fn add_fill(&mut self, fields: Rec) -> StoreResult<i64>;
    /// `order(order_id)`.
    async fn order(&mut self, order_id: i64) -> StoreResult<Option<Rec>>;
    /// `order_by_broker_id(broker_order_id)`.
    async fn order_by_broker_id(&mut self, broker_order_id: &str) -> StoreResult<Option<Rec>>;
    /// `create_exit_order(fields)` (LV9).
    async fn create_exit_order(&mut self, fields: Rec) -> StoreResult<i64>;
    /// `exit_order_by_broker_id(broker_order_id)`.
    async fn exit_order_by_broker_id(&mut self, broker_order_id: &str) -> StoreResult<Option<Rec>>;
    /// `update_exit_order(exit_order_id, fields)`.
    async fn update_exit_order(&mut self, exit_order_id: i64, fields: Rec) -> StoreResult<()>;
    /// `open_orders()` (LV2).
    async fn open_orders(&mut self) -> StoreResult<Vec<Rec>>;
    /// `open_protection_issues()` (LV2).
    async fn open_protection_issues(&mut self) -> StoreResult<Vec<Rec>>;
    /// `line_by_client_id(client_id)`.
    async fn line_by_client_id(&mut self, client_id: &str) -> StoreResult<Option<Rec>>;
    /// `create_order(fields)`.
    async fn create_order(&mut self, fields: Rec) -> StoreResult<i64>;
    /// `update_order(order_id, fields)`.
    async fn update_order(&mut self, order_id: i64, fields: Rec) -> StoreResult<()>;
    /// `entries_taken(day)`: buys sent on the IST day, whatever plan they came from (LV10.2).
    async fn entries_taken(&mut self, day: NaiveDate) -> StoreResult<i64>;
    /// `bump_session(day, mode=..., confirms=..., fills=..., ratchets=..., exits=...)`.
    async fn bump_session(&mut self, day: NaiveDate, bump: SessionBump) -> StoreResult<()>;
    /// `set_naked_count(day, mode=..., naked=...)`.
    async fn set_naked_count(&mut self, day: NaiveDate, mode: &str, naked: i64) -> StoreResult<()>;
    /// `lock_session_for_update(day)`: the context manager's entry. Its exit is
    /// [`TwtStore::unlock_session`], which the confirm functions call once their body is done.
    async fn lock_session_for_update(&mut self, day: NaiveDate) -> StoreResult<()>;
    /// The exit of the outermost `with store.lock_session_for_update(day):` block: `COMMIT` of
    /// the transaction the lock opened, on success and on an exception alike (R8.TD1). A store
    /// with no transaction to close does nothing.
    async fn unlock_session(&mut self) -> StoreResult<()> {
        Ok(())
    }
    /// `sleeve_money(as_of)`.
    async fn sleeve_money(&mut self, as_of: NaiveDate) -> StoreResult<SleeveMoney>;
    /// `adj_factor(instrument_id, as_of)`.
    async fn adj_factor(&mut self, instrument_id: i64, as_of: NaiveDate) -> StoreResult<Decimal>;
    /// `count_first_live_entry(position_id, session_date=..., now=..., live=...)`.
    async fn count_first_live_entry(
        &mut self,
        position_id: i64,
        session_date: NaiveDate,
        now: DateTime<FixedOffset>,
        live: bool,
    ) -> StoreResult<bool>;
    /// `expire_plans(now)`.
    async fn expire_plans(&mut self, now: DateTime<FixedOffset>) -> StoreResult<i64>;
    /// `zero_capital(now=..., changed_by=...)`: the previous capital, or `None` with no row.
    async fn zero_capital(
        &mut self,
        now: DateTime<FixedOffset>,
        changed_by: &str,
    ) -> StoreResult<Option<Decimal>>;
    /// `todays_plan(on_or_before)`.
    async fn todays_plan(&mut self, on_or_before: NaiveDate) -> StoreResult<Option<Rec>>;
    /// `lines_for(plan_pk)`, in `KIND_ORDER` then symbol.
    async fn lines_for(&mut self, plan_pk: i64) -> StoreResult<Vec<Rec>>;
    /// The trading calendar's word on `day` (`twt_auto.session_closed`'s read): `Some(true)` is
    /// a session, `None` unknown.
    async fn is_trading_day(&mut self, day: NaiveDate) -> StoreResult<Option<bool>> {
        let _ = day;
        Ok(None)
    }
}

/// `set_line`'s keyword arguments (a `None` leaves the column as it is).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LineUpdate {
    /// `journal_ref`.
    pub journal_ref: Option<String>,
    /// `order_id`.
    pub order_id: Option<i64>,
    /// `position_id`.
    pub position_id: Option<i64>,
    /// `note`.
    pub note: Option<String>,
}

impl LineUpdate {
    /// Only a note.
    #[must_use]
    pub fn note(note: impl Into<String>) -> Self {
        Self {
            note: Some(note.into()),
            ..Self::default()
        }
    }

    /// Only a position.
    #[must_use]
    pub fn position(position_id: i64) -> Self {
        Self {
            position_id: Some(position_id),
            ..Self::default()
        }
    }
}

/// `bump_session`'s keyword arguments.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SessionBump {
    /// `mode` (`DRY_RUN` / `LIVE`).
    pub mode: String,
    /// `confirms`.
    pub confirms: i64,
    /// `fills`.
    pub fills: i64,
    /// `ratchets`.
    pub ratchets: i64,
    /// `exits`.
    pub exits: i64,
}

/// `"DRY_RUN" if simulated else "LIVE"`.
#[must_use]
pub fn mode(simulated: bool) -> String {
    if simulated { "DRY_RUN" } else { "LIVE" }.to_owned()
}

/// `uuid.UUID(str(plan_id))` accepts it.
#[must_use]
pub fn is_uuid(text: &str) -> bool {
    let t = text.trim();
    let t = t
        .strip_prefix("urn:uuid:")
        .unwrap_or(t)
        .trim_start_matches('{')
        .trim_end_matches('}');
    let hex: String = t.chars().filter(|c| *c != '-').collect();
    hex.len() == 32 && hex.chars().all(|c| c.is_ascii_hexdigit())
}

fn col<'a>(row: &'a Row, name: &str) -> Option<&'a DbValue> {
    row.get(name)
}

fn dec_val(row: &Row, name: &str) -> Val {
    Val::from(db_dec(col(row, name)))
}

fn date_val(row: &Row, name: &str) -> StoreResult<Val> {
    Ok(Val::from(db_date(col(row, name))?))
}

fn stamp_val(row: &Row, name: &str) -> StoreResult<Val> {
    Ok(Val::from(db_stamp(col(row, name))?))
}

fn str_val(row: &Row, name: &str) -> Val {
    match col(row, name) {
        None | Some(DbValue::Null) => Val::Str("None".to_owned()),
        Some(v) => Val::Str(super::common::db_text(v)),
    }
}

fn int_req(row: &Row, name: &str) -> StoreResult<Val> {
    match col(row, name) {
        Some(DbValue::Int(i)) => Ok(Val::Int(*i)),
        Some(DbValue::Bool(b)) => Ok(Val::Int(i64::from(*b))),
        #[allow(clippy::cast_possible_truncation)]
        Some(DbValue::Float(f)) => Ok(Val::Int(f.trunc() as i64)),
        Some(DbValue::Text(s)) => s
            .trim()
            .parse()
            .map(Val::Int)
            .map_err(|_| StoreError::Bad(format!("int({s:?})"))),
        _ => Err(StoreError::Bad(format!(
            "TypeError: int() argument must be a string, a bytes-like object or a real number, \
             not 'NoneType' ({name})"
        ))),
    }
}

/// `PgTwtStore`: one user's `tw_` rows through the desk's connection.
pub struct PgTwtStore {
    /// The connection.
    pub conn: Conn,
    /// `user_id`.
    pub user_id: i64,
    /// `"public"` on Postgres, `""` on SQLite.
    pub schema: String,
    /// `broker_account_id`.
    pub broker_account_id: Option<i64>,
    /// `_lock_depth > 0` with a transaction this store opened (R8.TD1).
    lock_opened: bool,
}

impl std::fmt::Debug for PgTwtStore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PgTwtStore")
            .field("user_id", &self.user_id)
            .field("schema", &self.schema)
            .finish_non_exhaustive()
    }
}

const LINE_SELECT: &str = "SELECT l.id, l.plan_id AS plan_pk, p.plan_id, l.kind, l.instrument_id, i.symbol, \
     l.quantity, l.stop_price, l.value_inr, l.high_since, l.previous_trigger, l.note, \
     l.state, l.client_id, l.journal_ref, l.order_id, l.position_id, \
     p.session_date, p.source AS plan_source, p.expires_at, s.turnover_avg_20 ";

const POSITION_SELECT: &str = "SELECT p.id, p.instrument_id, i.symbol, p.signal_date, p.entry_date, p.entry_avg, \
     p.entry_adj_factor, p.quantity_entered, p.quantity_open, p.initial_stop, p.stop_price, \
     p.high_since, p.high_since_date, p.gtt_id, p.gtt_trigger, p.gtt_armed_at, \
     p.next_trigger, p.next_trigger_for, p.state, p.closed_on, p.exit_avg, p.close_reason, \
     p.pnl_inr, p.return_pct, p.simulated, p.half_size, p.partial_done, p.trail, \
     p.partial_queued_for, p.partial_quantity, p.exit_queued_for, p.exit_reason_queued ";

const ORDER_SELECT: &str = "SELECT o.id, o.instrument_id, i.symbol, o.signal_date, o.side, o.quantity, \
     o.stop_price, o.state, o.broker_order_id, o.client_id, o.filled_quantity, \
     o.avg_fill_price, o.position_id, o.simulated ";

const EXIT_SELECT: &str = "SELECT id, sleeve, position_id, line_id, symbol, broker_order_id, client_id, quantity, \
     reference_price, state, filled_quantity, avg_fill_price, reason, simulated ";

const SCAN_SELECT: &str = "SELECT id, requested_at, started_at, finished_at, session_date, status, source, \
     detail, error, task_id FROM ";

/// `_plan_row`.
fn plan_row(row: &Row) -> StoreResult<Rec> {
    Ok(Rec::of([
        ("id", int_req(row, "id")?),
        ("plan_id", str_val(row, "plan_id")),
        ("session_date", date_val(row, "session_date")?),
        ("source", str_val(row, "source")),
        ("built_at", stamp_val(row, "built_at")?),
        ("expires_at", stamp_val(row, "expires_at")?),
        ("gate", str_val(row, "gate")),
        ("sleeve_equity_inr", dec_val(row, "sleeve_equity_inr")),
        (
            "total_new_exposure_inr",
            dec_val(row, "total_new_exposure_inr"),
        ),
    ]))
}

/// `_line_row`.
fn line_row(row: &Row) -> StoreResult<Rec> {
    Ok(Rec::of([
        ("id", int_req(row, "id")?),
        ("plan_pk", int_req(row, "plan_pk")?),
        ("plan_id", str_val(row, "plan_id")),
        ("kind", str_val(row, "kind")),
        ("instrument_id", int_req(row, "instrument_id")?),
        ("symbol", str_val(row, "symbol")),
        ("quantity", Val::Int(db_int(col(row, "quantity")))),
        ("stop_price", dec_val(row, "stop_price")),
        ("value_inr", dec_val(row, "value_inr")),
        ("high_since", dec_val(row, "high_since")),
        ("previous_trigger", dec_val(row, "previous_trigger")),
        ("note", db_raw(col(row, "note"))),
        ("state", str_val(row, "state")),
        ("client_id", db_raw(col(row, "client_id"))),
        ("journal_ref", db_raw(col(row, "journal_ref"))),
        ("order_id", db_raw(col(row, "order_id"))),
        ("position_id", db_raw(col(row, "position_id"))),
        ("session_date", date_val(row, "session_date")?),
        ("plan_source", str_val(row, "plan_source")),
        ("expires_at", stamp_val(row, "expires_at")?),
        ("turnover_avg_inr", dec_val(row, "turnover_avg_20")),
    ]))
}

/// `_position_row`.
fn position_row(row: &Row) -> StoreResult<Rec> {
    let opt_int = |name: &str| match col(row, name) {
        None | Some(DbValue::Null) => Val::None,
        other => Val::Int(db_int(other)),
    };
    Ok(Rec::of([
        ("id", int_req(row, "id")?),
        ("instrument_id", int_req(row, "instrument_id")?),
        ("symbol", str_val(row, "symbol")),
        ("signal_date", date_val(row, "signal_date")?),
        ("entry_date", date_val(row, "entry_date")?),
        ("entry_avg", dec_val(row, "entry_avg")),
        ("entry_adj_factor", dec_val(row, "entry_adj_factor")),
        ("quantity_entered", int_req(row, "quantity_entered")?),
        ("quantity_open", int_req(row, "quantity_open")?),
        ("initial_stop", dec_val(row, "initial_stop")),
        ("stop_price", dec_val(row, "stop_price")),
        ("high_since", dec_val(row, "high_since")),
        ("high_since_date", date_val(row, "high_since_date")?),
        ("gtt_id", db_raw(col(row, "gtt_id"))),
        ("gtt_trigger", dec_val(row, "gtt_trigger")),
        ("gtt_armed_at", stamp_val(row, "gtt_armed_at")?),
        ("next_trigger", dec_val(row, "next_trigger")),
        ("next_trigger_for", date_val(row, "next_trigger_for")?),
        ("state", str_val(row, "state")),
        ("closed_on", date_val(row, "closed_on")?),
        ("exit_avg", dec_val(row, "exit_avg")),
        ("close_reason", db_raw(col(row, "close_reason"))),
        ("pnl_inr", dec_val(row, "pnl_inr")),
        ("return_pct", dec_val(row, "return_pct")),
        ("simulated", Val::Bool(db_bool(col(row, "simulated")))),
        ("half_size", Val::Bool(db_bool(col(row, "half_size")))),
        ("partial_done", Val::Bool(db_bool(col(row, "partial_done")))),
        ("trail", db_raw(col(row, "trail"))),
        ("partial_queued_for", date_val(row, "partial_queued_for")?),
        ("partial_quantity", opt_int("partial_quantity")),
        ("exit_queued_for", date_val(row, "exit_queued_for")?),
        ("exit_reason_queued", db_raw(col(row, "exit_reason_queued"))),
    ]))
}

/// `_order_row`.
fn order_row(row: &Row) -> StoreResult<Rec> {
    Ok(Rec::of([
        ("id", int_req(row, "id")?),
        ("instrument_id", int_req(row, "instrument_id")?),
        ("symbol", str_val(row, "symbol")),
        ("signal_date", date_val(row, "signal_date")?),
        ("side", str_val(row, "side")),
        ("quantity", int_req(row, "quantity")?),
        ("stop_price", dec_val(row, "stop_price")),
        ("state", str_val(row, "state")),
        ("broker_order_id", db_raw(col(row, "broker_order_id"))),
        ("client_id", db_raw(col(row, "client_id"))),
        (
            "filled_quantity",
            Val::Int(db_int(col(row, "filled_quantity"))),
        ),
        ("avg_fill_price", dec_val(row, "avg_fill_price")),
        ("position_id", db_raw(col(row, "position_id"))),
        ("simulated", Val::Bool(db_bool(col(row, "simulated")))),
    ]))
}

/// `_exit_row` (TWT's: `reason` raw).
fn exit_row(row: &Row) -> StoreResult<Rec> {
    let line_id = match col(row, "line_id") {
        None | Some(DbValue::Null) => Val::None,
        other => Val::Int(db_int(other)),
    };
    Ok(Rec::of([
        ("id", int_req(row, "id")?),
        ("sleeve", str_val(row, "sleeve")),
        ("position_id", int_req(row, "position_id")?),
        ("line_id", line_id),
        ("symbol", str_val(row, "symbol")),
        ("broker_order_id", db_raw(col(row, "broker_order_id"))),
        ("client_id", db_raw(col(row, "client_id"))),
        ("quantity", int_req(row, "quantity")?),
        ("reference_price", dec_val(row, "reference_price")),
        ("state", str_val(row, "state")),
        (
            "filled_quantity",
            Val::Int(db_int(col(row, "filled_quantity"))),
        ),
        ("avg_fill_price", dec_val(row, "avg_fill_price")),
        ("reason", db_raw(col(row, "reason"))),
        ("simulated", Val::Bool(db_bool(col(row, "simulated")))),
        ("side", Val::Str("SELL".to_owned())),
    ]))
}

/// `_scan_run_row` (TWT's: timestamps through `_stamp`, the date through `_date`).
fn scan_run_row(row: &Row) -> StoreResult<Rec> {
    let detail = match col(row, "detail") {
        Some(DbValue::Text(s)) => {
            crate::wire::PyValue::from_json(s.as_bytes()).map_or(Val::Str(s.clone()), Val::Py)
        }
        other => db_raw(other),
    };
    Ok(Rec::of([
        ("id", int_req(row, "id")?),
        ("requested_at", stamp_val(row, "requested_at")?),
        ("started_at", stamp_val(row, "started_at")?),
        ("finished_at", stamp_val(row, "finished_at")?),
        ("session_date", date_val(row, "session_date")?),
        ("status", str_val(row, "status")),
        ("source", str_val(row, "source")),
        ("detail", detail),
        ("error", db_raw(col(row, "error"))),
        ("task_id", db_raw(col(row, "task_id"))),
    ]))
}

impl PgTwtStore {
    /// `PgTwtStore(conn, user_id, schema, broker_account_id=...)`.
    #[must_use]
    pub fn new(conn: Conn, user_id: i64, schema: &str, broker_account_id: Option<i64>) -> Self {
        Self {
            conn,
            user_id,
            schema: schema.to_owned(),
            broker_account_id,
            lock_opened: false,
        }
    }

    /// Whether a `lock_session_for_update` transaction is open on this store.
    #[must_use]
    pub const fn session_locked(&self) -> bool {
        self.lock_opened
    }

    /// `t(table)`.
    #[must_use]
    pub fn t(&self, table: &str) -> String {
        if self.schema.is_empty() {
            table.to_owned()
        } else {
            format!("{}.{table}", self.schema)
        }
    }

    async fn rows(&mut self, sql: &str, params: &[Param]) -> StoreResult<Vec<Row>> {
        Ok(self.conn.execute(sql, params).await?.rows)
    }

    async fn one(&mut self, sql: &str, params: &[Param]) -> StoreResult<Option<Row>> {
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
            "FROM {} l JOIN {} p ON p.id = l.plan_id JOIN {} i ON i.id = l.instrument_id \
             LEFT JOIN {} s ON s.user_id = l.user_id \
             AND s.instrument_id = l.instrument_id AND s.date = p.session_date ",
            self.t("tw_plan_line"),
            self.t("tw_plan"),
            self.t("instrument"),
            self.t("tw_signal_daily")
        )
    }

    fn position_from(&self) -> String {
        format!(
            "FROM {} p JOIN {} i ON i.id = p.instrument_id ",
            self.t("tw_position"),
            self.t("instrument")
        )
    }

    fn order_from(&self) -> String {
        format!(
            "FROM {} o JOIN {} i ON i.id = o.instrument_id ",
            self.t("tw_order"),
            self.t("instrument")
        )
    }

    async fn insert_returning(
        &mut self,
        table: &str,
        lead: &[(&str, Val)],
        fields: &Rec,
    ) -> StoreResult<i64> {
        let mut columns: Vec<String> = lead.iter().map(|(k, _)| (*k).to_owned()).collect();
        let mut values: Vec<Param> = lead.iter().map(|(_, v)| param(v)).collect();
        for (k, v) in &fields.0 {
            columns.push(k.clone());
            values.push(param(v));
        }
        let marks = vec!["?"; columns.len()].join(", ");
        let sql = format!(
            "INSERT INTO {} ({}) VALUES ({marks}) RETURNING id",
            self.t(table),
            columns.join(", ")
        );
        let row = self
            .one(&sql, &values)
            .await?
            .ok_or_else(|| StoreError::Bad("INSERT ... RETURNING gave no row".to_owned()))?;
        Ok(db_int(row.get("id")))
    }

    async fn update_set(
        &mut self,
        table: &str,
        id: i64,
        fields: &Rec,
        tail: &str,
    ) -> StoreResult<()> {
        let sets: Vec<String> = fields.0.iter().map(|(k, _)| format!("{k} = ?")).collect();
        let mut values: Vec<Param> = fields.0.iter().map(|(_, v)| param(v)).collect();
        values.push(Param::Int(self.user_id));
        values.push(Param::Int(id));
        let sql = format!(
            "UPDATE {} SET {}{tail} WHERE user_id = ? AND id = ?",
            self.t(table),
            sets.join(", ")
        );
        self.conn.execute(&sql, &values).await?;
        Ok(())
    }

    /// `config()`.
    ///
    /// # Errors
    /// The database.
    pub async fn config(&mut self) -> StoreResult<Rec> {
        let sql = format!(
            "SELECT sleeve_capital_inr, max_open_positions, max_position_pct, stop_pct, \
             trail_pct, first_live_entries_left, dry_run_sessions, updated_at, updated_by \
             FROM {} WHERE user_id = ?",
            self.t("tw_config")
        );
        let Some(row) = self.one(&sql, &[Param::Int(self.user_id)]).await? else {
            return Ok(Rec::of([
                ("sleeve_capital_inr", Val::Dec(Decimal::ZERO)),
                ("first_live_entries_left", Val::Int(0)),
            ]));
        };
        let capital = db_dec(col(&row, "sleeve_capital_inr"))
            .filter(|d| !d.is_zero())
            .unwrap_or(Decimal::ZERO);
        let max_open = match db_int(col(&row, "max_open_positions")) {
            0 => 10,
            n => n,
        };
        Ok(Rec::of([
            ("sleeve_capital_inr", Val::Dec(capital)),
            ("max_open_positions", Val::Int(max_open)),
            ("max_position_pct", dec_val(&row, "max_position_pct")),
            ("stop_pct", dec_val(&row, "stop_pct")),
            ("trail_pct", dec_val(&row, "trail_pct")),
            (
                "first_live_entries_left",
                Val::Int(db_int(col(&row, "first_live_entries_left"))),
            ),
            (
                "dry_run_sessions",
                Val::Int(db_int(col(&row, "dry_run_sessions"))),
            ),
            ("updated_at", stamp_val(&row, "updated_at")?),
            ("updated_by", db_raw(col(&row, "updated_by"))),
        ]))
    }

    /// `realised_pnl(as_of)`.
    ///
    /// # Errors
    /// The database.
    pub async fn realised_pnl(&mut self, as_of: NaiveDate) -> StoreResult<Decimal> {
        let sql = format!(
            "SELECT COALESCE(SUM(pnl_inr), 0) AS total FROM {} \
             WHERE user_id = ? AND state = 'CLOSED' AND closed_on <= ?",
            self.t("tw_position")
        );
        let row = self
            .one(&sql, &[Param::Int(self.user_id), Param::Date(as_of)])
            .await?;
        Ok(row
            .and_then(|r| db_dec(col(&r, "total")))
            .filter(|d| !d.is_zero())
            .unwrap_or(Decimal::ZERO))
    }

    /// `marks(instrument_ids, as_of)`: the latest close on or before `as_of`, per instrument.
    ///
    /// # Errors
    /// The database.
    pub async fn marks(
        &mut self,
        ids: &[i64],
        as_of: NaiveDate,
    ) -> StoreResult<Vec<(i64, Decimal)>> {
        if ids.is_empty() {
            return Ok(Vec::new());
        }
        let marks = vec!["?"; ids.len()].join(", ");
        let t = self.t("ohlcv_daily");
        let sql = format!(
            "SELECT o.instrument_id, o.close FROM {t} o \
             JOIN (SELECT instrument_id, MAX(date) AS date FROM {t} \
             WHERE instrument_id IN ({marks}) AND date <= ? GROUP BY instrument_id) latest \
             ON latest.instrument_id = o.instrument_id AND latest.date = o.date"
        );
        let mut params: Vec<Param> = ids.iter().map(|i| Param::Int(*i)).collect();
        params.push(Param::Date(as_of));
        let rows = self.rows(&sql, &params).await?;
        let mut out: Vec<(i64, Decimal)> = Vec::new();
        for row in rows {
            let id = db_int(col(&row, "instrument_id"));
            let close = db_dec(col(&row, "close"))
                .filter(|d| !d.is_zero())
                .unwrap_or(Decimal::ZERO);
            if let Some(slot) = out.iter_mut().find(|(i, _)| *i == id) {
                slot.1 = close;
            } else {
                out.push((id, close));
            }
        }
        Ok(out)
    }

    /// `session(day)`.
    ///
    /// # Errors
    /// The database.
    pub async fn session(&mut self, day: NaiveDate) -> StoreResult<Option<Rec>> {
        let sql = format!(
            "SELECT session_date, mode, gate, states, signals, confirms, fills, ratchets, \
             exits, naked_at_1515, first_live_entries_counted FROM {} \
             WHERE user_id = ? AND session_date = ?",
            self.t("tw_session")
        );
        let Some(row) = self
            .one(&sql, &[Param::Int(self.user_id), Param::Date(day)])
            .await?
        else {
            return Ok(None);
        };
        let n = |name: &str| Val::Int(db_int(col(&row, name)));
        Ok(Some(Rec::of([
            ("session_date", date_val(&row, "session_date")?),
            ("mode", str_val(&row, "mode")),
            ("gate", str_val(&row, "gate")),
            ("states", n("states")),
            ("signals", n("signals")),
            ("confirms", n("confirms")),
            ("fills", n("fills")),
            ("ratchets", n("ratchets")),
            ("exits", n("exits")),
            ("naked_at_1515", n("naked_at_1515")),
            (
                "first_live_entries_counted",
                n("first_live_entries_counted"),
            ),
        ])))
    }

    async fn audit(
        &mut self,
        key: &str,
        old: Option<String>,
        new: Option<String>,
        now: DateTime<FixedOffset>,
        changed_by: &str,
        note: Option<&str>,
    ) -> StoreResult<()> {
        let sql = format!(
            "INSERT INTO {} (user_id, key, old_value, new_value, changed_at, changed_by, note) \
             VALUES (?, ?, ?, ?, ?, ?, ?)",
            self.t("tw_config_audit")
        );
        self.conn
            .execute(
                &sql,
                &[
                    Param::Int(self.user_id),
                    Param::from(key),
                    Param::from(old),
                    Param::from(new),
                    Param::Aware(now),
                    Param::from(changed_by),
                    Param::from(note),
                ],
            )
            .await?;
        Ok(())
    }

    /// `skips_for(plan_pk)`.
    ///
    /// # Errors
    /// The database.
    pub async fn skips_for(&mut self, plan_pk: i64) -> StoreResult<Vec<Rec>> {
        let sql = format!(
            "SELECT s.reason, s.detail, i.symbol FROM {} s JOIN {} i ON i.id = s.instrument_id \
             WHERE s.user_id = ? AND s.plan_id = ? ORDER BY i.symbol",
            self.t("tw_plan_skip"),
            self.t("instrument")
        );
        let rows = self
            .rows(&sql, &[Param::Int(self.user_id), Param::Int(plan_pk)])
            .await?;
        Ok(rows
            .iter()
            .map(|row| {
                Rec::of([
                    ("reason", str_val(row, "reason")),
                    ("detail", db_raw(col(row, "detail"))),
                    ("symbol", str_val(row, "symbol")),
                ])
            })
            .collect())
    }

    /// `newest_scan()`.
    ///
    /// # Errors
    /// The database.
    pub async fn newest_scan(&mut self) -> StoreResult<Option<Rec>> {
        let sql = format!(
            "{SCAN_SELECT}{} WHERE user_id = ? ORDER BY requested_at DESC, id DESC LIMIT 1",
            self.t("tw_scan_run")
        );
        match self.one(&sql, &[Param::Int(self.user_id)]).await? {
            Some(row) => Ok(Some(scan_run_row(&row)?)),
            None => Ok(None),
        }
    }

    /// `scan_run(run_id)`.
    ///
    /// # Errors
    /// The database.
    pub async fn scan_run(&mut self, run_id: i64) -> StoreResult<Option<Rec>> {
        let sql = format!(
            "{SCAN_SELECT}{} WHERE user_id = ? AND id = ?",
            self.t("tw_scan_run")
        );
        match self
            .one(&sql, &[Param::Int(self.user_id), Param::Int(run_id)])
            .await?
        {
            Some(row) => Ok(Some(scan_run_row(&row)?)),
            None => Ok(None),
        }
    }

    /// `request_scan(now=..., min_interval=..., stale_after=..., source="desk")`.
    ///
    /// # Errors
    /// [`StoreError::Scan`] with 409 or 429, or the database.
    pub async fn request_scan(
        &mut self,
        now: DateTime<FixedOffset>,
        min_interval: Duration,
        stale_after: Duration,
        source: &str,
    ) -> StoreResult<i64> {
        if let Some(newest) = self.newest_scan().await?
            && let Some(requested_at) = newest.stamp("requested_at")
        {
            let status = newest.s("status");
            let in_flight = SCAN_IN_FLIGHT.contains(&status.as_str());
            let id = newest.int("id");
            if in_flight && requested_at > now - stale_after {
                return Err(StoreError::Scan(ScanRefused {
                    status: 409,
                    reason: format!(
                        "Scan {id} is {}; its result is on its way.",
                        status.to_lowercase()
                    ),
                    run_id: id,
                    retry_after: None,
                }));
            }
            if requested_at > now - min_interval {
                let wait = min_interval - (now - requested_at);
                let seconds = (micros_to_secs(wait) + 0.999).trunc().max(1.0);
                let ago = micros_to_secs(now - requested_at).trunc();
                #[allow(clippy::cast_possible_truncation)]
                let (seconds, ago) = (seconds as i64, ago as i64);
                return Err(StoreError::Scan(ScanRefused {
                    status: 429,
                    reason: format!(
                        "A scan was requested {ago} s ago; one a minute is the limit. \
                         Try again in {seconds} s."
                    ),
                    run_id: id,
                    retry_after: Some(seconds),
                }));
            }
        }
        let sql = format!(
            "INSERT INTO {} (user_id, requested_at, status, source) \
             VALUES (?, ?, 'QUEUED', ?) RETURNING id",
            self.t("tw_scan_run")
        );
        let row = self
            .one(
                &sql,
                &[
                    Param::Int(self.user_id),
                    Param::Aware(now),
                    Param::from(source),
                ],
            )
            .await?
            .ok_or_else(|| StoreError::Bad("INSERT ... RETURNING gave no row".to_owned()))?;
        Ok(db_int(row.get("id")))
    }

    /// `breadth(on)`.
    ///
    /// # Errors
    /// The database.
    pub async fn breadth(&mut self, on: NaiveDate) -> StoreResult<Option<Rec>> {
        let sql = format!(
            "SELECT date, measured_count, above_count, pct_above_dma, gate, thin_session \
             FROM {} WHERE user_id = ? AND date = ?",
            self.t("tw_breadth_daily")
        );
        let Some(row) = self
            .one(&sql, &[Param::Int(self.user_id), Param::Date(on)])
            .await?
        else {
            return Ok(None);
        };
        Ok(Some(Rec::of([
            ("date", date_val(&row, "date")?),
            (
                "measured_count",
                Val::Int(db_int(col(&row, "measured_count"))),
            ),
            ("above_count", Val::Int(db_int(col(&row, "above_count")))),
            ("pct_above_dma", dec_val(&row, "pct_above_dma")),
            ("gate", str_val(&row, "gate")),
            (
                "thin_session",
                Val::Bool(db_bool(col(&row, "thin_session"))),
            ),
        ])))
    }
}

/// `timedelta.total_seconds()`.
#[must_use]
pub fn micros_to_secs(d: Duration) -> f64 {
    #[allow(clippy::cast_precision_loss)]
    let micros = d.num_microseconds().unwrap_or(i64::MAX) as f64;
    micros / 1e6
}

#[async_trait]
impl TwtStore for PgTwtStore {
    async fn plan(&mut self, plan_id: &str) -> StoreResult<Option<Rec>> {
        if !is_uuid(plan_id) {
            return Ok(None);
        }
        let sql = format!(
            "SELECT id, plan_id, session_date, source, built_at, expires_at, gate, \
             sleeve_equity_inr, total_new_exposure_inr FROM {} \
             WHERE user_id = ? AND plan_id = ?",
            self.t("tw_plan")
        );
        match self
            .one(&sql, &[Param::Int(self.user_id), Param::from(plan_id)])
            .await?
        {
            Some(row) => Ok(Some(plan_row(&row)?)),
            None => Ok(None),
        }
    }

    async fn line(&mut self, line_id: i64) -> StoreResult<Option<Rec>> {
        let sql = format!(
            "{LINE_SELECT}{}WHERE l.user_id = ? AND l.id = ?",
            self.line_from()
        );
        match self
            .one(&sql, &[Param::Int(self.user_id), Param::Int(line_id)])
            .await?
        {
            Some(row) => Ok(Some(line_row(&row)?)),
            None => Ok(None),
        }
    }

    async fn set_line(&mut self, line_id: i64, state: &str, extra: LineUpdate) -> StoreResult<()> {
        let mut sets = vec!["state = ?".to_owned()];
        let mut values = vec![Param::from(state)];
        if let Some(v) = extra.journal_ref {
            sets.push("journal_ref = ?".to_owned());
            values.push(Param::Text(v));
        }
        if let Some(v) = extra.order_id {
            sets.push("order_id = ?".to_owned());
            values.push(Param::Int(v));
        }
        if let Some(v) = extra.position_id {
            sets.push("position_id = ?".to_owned());
            values.push(Param::Int(v));
        }
        if let Some(v) = extra.note {
            sets.push("note = ?".to_owned());
            values.push(Param::Text(v));
        }
        values.push(Param::Int(self.user_id));
        values.push(Param::Int(line_id));
        let sql = format!(
            "UPDATE {} SET {} WHERE user_id = ? AND id = ?",
            self.t("tw_plan_line"),
            sets.join(", ")
        );
        self.conn.execute(&sql, &values).await?;
        Ok(())
    }

    async fn open_position_for(&mut self, instrument_id: i64) -> StoreResult<Option<Rec>> {
        let sql = format!(
            "{POSITION_SELECT}{}WHERE p.user_id = ? AND p.instrument_id = ? AND p.state = 'OPEN' \
             AND p.quantity_open > 0 ORDER BY p.id LIMIT 1",
            self.position_from()
        );
        match self
            .one(&sql, &[Param::Int(self.user_id), Param::Int(instrument_id)])
            .await?
        {
            Some(row) => Ok(Some(position_row(&row)?)),
            None => Ok(None),
        }
    }

    async fn position(&mut self, position_id: i64) -> StoreResult<Option<Rec>> {
        let sql = format!(
            "{POSITION_SELECT}{}WHERE p.user_id = ? AND p.id = ?",
            self.position_from()
        );
        match self
            .one(&sql, &[Param::Int(self.user_id), Param::Int(position_id)])
            .await?
        {
            Some(row) => Ok(Some(position_row(&row)?)),
            None => Ok(None),
        }
    }

    async fn open_positions(&mut self) -> StoreResult<Vec<Rec>> {
        let sql = format!(
            "{POSITION_SELECT}{}WHERE p.user_id = ? AND p.state = 'OPEN' AND p.quantity_open > 0 \
             ORDER BY p.entry_date, i.symbol",
            self.position_from()
        );
        let rows = self.rows(&sql, &[Param::Int(self.user_id)]).await?;
        rows.iter().map(position_row).collect()
    }

    async fn create_position(&mut self, fields: Rec) -> StoreResult<i64> {
        let lead = [
            ("user_id", Val::Int(self.user_id)),
            ("broker_account_id", Val::from(self.broker_account_id)),
        ];
        self.insert_returning("tw_position", &lead, &fields).await
    }

    async fn update_position(&mut self, position_id: i64, fields: Rec) -> StoreResult<()> {
        self.update_set("tw_position", position_id, &fields, "")
            .await
    }

    async fn add_fill(&mut self, fields: Rec) -> StoreResult<i64> {
        let lead = [("user_id", Val::Int(self.user_id))];
        self.insert_returning("tw_fill", &lead, &fields).await
    }

    async fn order(&mut self, order_id: i64) -> StoreResult<Option<Rec>> {
        let sql = format!(
            "{ORDER_SELECT}{}WHERE o.user_id = ? AND o.id = ?",
            self.order_from()
        );
        match self
            .one(&sql, &[Param::Int(self.user_id), Param::Int(order_id)])
            .await?
        {
            Some(row) => Ok(Some(order_row(&row)?)),
            None => Ok(None),
        }
    }

    async fn order_by_broker_id(&mut self, broker_order_id: &str) -> StoreResult<Option<Rec>> {
        let sql = format!(
            "{ORDER_SELECT}{}WHERE o.user_id = ? AND o.broker_order_id = ? ORDER BY o.id DESC LIMIT 1",
            self.order_from()
        );
        match self
            .one(
                &sql,
                &[Param::Int(self.user_id), Param::from(broker_order_id)],
            )
            .await?
        {
            Some(row) => Ok(Some(order_row(&row)?)),
            None => Ok(None),
        }
    }

    async fn create_exit_order(&mut self, fields: Rec) -> StoreResult<i64> {
        let lead = [("user_id", Val::Int(self.user_id))];
        self.insert_returning("lv_exit_order", &lead, &fields).await
    }

    async fn exit_order_by_broker_id(&mut self, broker_order_id: &str) -> StoreResult<Option<Rec>> {
        let sql = format!(
            "{EXIT_SELECT}FROM {} WHERE user_id = ? AND broker_order_id = ? ORDER BY id DESC LIMIT 1",
            self.t("lv_exit_order")
        );
        match self
            .one(
                &sql,
                &[Param::Int(self.user_id), Param::from(broker_order_id)],
            )
            .await?
        {
            Some(row) => Ok(Some(exit_row(&row)?)),
            None => Ok(None),
        }
    }

    async fn update_exit_order(&mut self, exit_order_id: i64, fields: Rec) -> StoreResult<()> {
        self.update_set(
            "lv_exit_order",
            exit_order_id,
            &fields,
            ", updated_at = CURRENT_TIMESTAMP",
        )
        .await
    }

    async fn open_orders(&mut self) -> StoreResult<Vec<Rec>> {
        let sql = format!(
            "{ORDER_SELECT}{}WHERE o.user_id = ? AND o.state IN ('SENT', 'PARTIAL') \
             AND o.broker_order_id IS NOT NULL ORDER BY o.id",
            self.order_from()
        );
        let rows = self.rows(&sql, &[Param::Int(self.user_id)]).await?;
        let mut out = Vec::with_capacity(rows.len());
        for row in &rows {
            let mut order = order_row(row)?;
            let day = order.date("signal_date").unwrap_or_default();
            let stop_pct = self.sleeve_money(day).await?.stop_pct;
            let reference = order.dec("stop_price").and_then(|stop| {
                let one = Decimal::ONE;
                let frac = one - super::common::ddiv(stop_pct, Decimal::from(100)).ok()?;
                (frac > Decimal::ZERO)
                    .then(|| super::common::ddiv(stop, frac).ok())
                    .flatten()
                    .map(|r| super::common::quantize(r, -2))
            });
            order.set("reference_price", Val::from(reference));
            out.push(order);
        }
        let sql = format!(
            "{EXIT_SELECT}FROM {} WHERE user_id = ? AND sleeve = 'twt' AND state IN ('SENT', 'PARTIAL') \
             AND broker_order_id IS NOT NULL ORDER BY id",
            self.t("lv_exit_order")
        );
        let exits = self.rows(&sql, &[Param::Int(self.user_id)]).await?;
        for row in &exits {
            out.push(exit_row(row)?);
        }
        Ok(out)
    }

    async fn open_protection_issues(&mut self) -> StoreResult<Vec<Rec>> {
        let sql = format!(
            "SELECT symbol, kind, detail, position_id FROM {} \
             WHERE user_id = ? AND sleeve = ? AND resolved_at IS NULL ORDER BY seen_at, id",
            self.t("lv_protection_issue")
        );
        let rows = self
            .rows(&sql, &[Param::Int(self.user_id), Param::from("twt")])
            .await?;
        rows.iter()
            .map(|r| {
                Ok(Rec::of([
                    ("symbol", str_val(r, "symbol")),
                    ("kind", str_val(r, "kind")),
                    ("detail", str_val(r, "detail")),
                    ("position_id", int_req(r, "position_id")?),
                ]))
            })
            .collect()
    }

    async fn line_by_client_id(&mut self, client_id: &str) -> StoreResult<Option<Rec>> {
        let sql = format!(
            "{LINE_SELECT}{}WHERE l.user_id = ? AND l.client_id = ? ORDER BY l.id DESC LIMIT 1",
            self.line_from()
        );
        match self
            .one(&sql, &[Param::Int(self.user_id), Param::from(client_id)])
            .await?
        {
            Some(row) => Ok(Some(line_row(&row)?)),
            None => Ok(None),
        }
    }

    async fn create_order(&mut self, fields: Rec) -> StoreResult<i64> {
        let lead = [
            ("user_id", Val::Int(self.user_id)),
            ("broker_account_id", Val::from(self.broker_account_id)),
        ];
        self.insert_returning("tw_order", &lead, &fields).await
    }

    async fn update_order(&mut self, order_id: i64, fields: Rec) -> StoreResult<()> {
        self.update_set("tw_order", order_id, &fields, "").await
    }

    async fn entries_taken(&mut self, day: NaiveDate) -> StoreResult<i64> {
        let marks = vec!["?"; ENTERED_ORDER_STATES.len()].join(", ");
        let sql = format!(
            "SELECT created_at FROM {} WHERE user_id = ? AND side = 'BUY' AND state IN ({marks})",
            self.t("tw_order")
        );
        let mut params = vec![Param::Int(self.user_id)];
        params.extend(ENTERED_ORDER_STATES.iter().map(|s| Param::from(*s)));
        let rows = self.rows(&sql, &params).await?;
        let mut n = 0;
        for r in &rows {
            if let Some(t) = db_stamp(col(r, "created_at"))?
                && ist_day(&t) == day
            {
                n += 1;
            }
        }
        Ok(n)
    }

    async fn bump_session(&mut self, day: NaiveDate, bump: SessionBump) -> StoreResult<()> {
        let sql = format!(
            "INSERT INTO {} (user_id, session_date, mode, confirms, fills, ratchets, exits) \
             VALUES (?, ?, ?, ?, ?, ?, ?) \
             ON CONFLICT (user_id, session_date) DO UPDATE SET mode = EXCLUDED.mode, \
             confirms = tw_session.confirms + EXCLUDED.confirms, \
             fills = tw_session.fills + EXCLUDED.fills, \
             ratchets = tw_session.ratchets + EXCLUDED.ratchets, \
             exits = tw_session.exits + EXCLUDED.exits",
            self.t("tw_session")
        );
        self.conn
            .execute(
                &sql,
                &[
                    Param::Int(self.user_id),
                    Param::Date(day),
                    Param::Text(bump.mode),
                    Param::Int(bump.confirms),
                    Param::Int(bump.fills),
                    Param::Int(bump.ratchets),
                    Param::Int(bump.exits),
                ],
            )
            .await?;
        Ok(())
    }

    async fn set_naked_count(&mut self, day: NaiveDate, mode: &str, naked: i64) -> StoreResult<()> {
        let sql = format!(
            "INSERT INTO {} (user_id, session_date, mode, naked_at_1515) VALUES (?, ?, ?, ?) \
             ON CONFLICT (user_id, session_date) DO UPDATE SET \
             naked_at_1515 = EXCLUDED.naked_at_1515",
            self.t("tw_session")
        );
        self.conn
            .execute(
                &sql,
                &[
                    Param::Int(self.user_id),
                    Param::Date(day),
                    Param::from(mode),
                    Param::Int(naked),
                ],
            )
            .await?;
        Ok(())
    }

    async fn lock_session_for_update(&mut self, day: NaiveDate) -> StoreResult<()> {
        let sql = format!(
            "INSERT INTO {} (user_id, session_date) VALUES (?, ?) \
             ON CONFLICT (user_id, session_date) DO NOTHING",
            self.t("tw_session")
        );
        let params = [Param::Int(self.user_id), Param::Date(day)];
        self.conn.execute(&sql, &params).await?;
        if !self.schema.is_empty() {
            // R8.TD1: the outermost block opens the transaction the `FOR UPDATE` holds, and
            // `NOWAIT` refuses a held row (409) rather than waiting on it.
            let opened = !self.lock_opened;
            if opened {
                self.conn.execute("BEGIN", &[]).await?;
            }
            let sql = format!(
                "SELECT 1 FROM {} WHERE user_id = ? AND session_date = ? FOR UPDATE NOWAIT",
                self.t("tw_session")
            );
            match self.conn.execute(&sql, &params).await {
                Ok(_) => {
                    if opened {
                        self.lock_opened = true;
                    }
                }
                Err(e) => {
                    if opened {
                        self.conn.execute("ROLLBACK", &[]).await?;
                    }
                    if let StorageError::Driver(sqlx::Error::Database(db)) = &e
                        && db.code().as_deref() == Some(LOCK_NOT_AVAILABLE)
                    {
                        return Err(StoreError::SessionBusy);
                    }
                    return Err(e.into());
                }
            }
        } else {
            // `elif _in_transaction(self.conn) is False: BEGIN IMMEDIATE`. sqlite refuses a
            // second BEGIN on a connection that already holds one (a nested block, or a
            // transaction the caller opened), and Python skips the statement in exactly that
            // case — so that one refusal is the skip, nothing else is. Only the block that
            // opened the transaction closes it (R8.TD1).
            match self.conn.execute("BEGIN IMMEDIATE", &[]).await {
                Ok(_) => self.lock_opened = true,
                Err(StorageError::Driver(sqlx::Error::Database(db)))
                    if db.message().contains("within a transaction") => {}
                Err(e) => return Err(e.into()),
            }
        }
        Ok(())
    }

    async fn unlock_session(&mut self) -> StoreResult<()> {
        if self.lock_opened {
            self.lock_opened = false;
            self.conn.execute("COMMIT", &[]).await?;
        }
        Ok(())
    }

    async fn sleeve_money(&mut self, as_of: NaiveDate) -> StoreResult<SleeveMoney> {
        let positions = self.open_positions().await?;
        let ids: Vec<i64> = positions.iter().map(|r| r.int("instrument_id")).collect();
        let config = self.config().await?;
        let marks = self.marks(&ids, as_of).await?;
        let realised = self.realised_pnl(as_of).await?;
        sleeve_money_from_rows(&config, &positions, &marks, realised)
    }

    async fn adj_factor(&mut self, instrument_id: i64, as_of: NaiveDate) -> StoreResult<Decimal> {
        let sql = format!(
            "SELECT adj_factor FROM {} WHERE instrument_id = ? AND date <= ? \
             ORDER BY date DESC LIMIT 1",
            self.t("ohlcv_daily")
        );
        let row = self
            .one(&sql, &[Param::Int(instrument_id), Param::Date(as_of)])
            .await?;
        Ok(row
            .and_then(|r| db_dec(col(&r, "adj_factor")))
            .filter(|d| !d.is_zero())
            .unwrap_or(Decimal::ONE))
    }

    async fn count_first_live_entry(
        &mut self,
        position_id: i64,
        session_date: NaiveDate,
        now: DateTime<FixedOffset>,
        live: bool,
    ) -> StoreResult<bool> {
        let Some(position) = self.position(position_id).await? else {
            return Ok(false);
        };
        if position.truthy("half_size") {
            return Ok(false);
        }
        if !live || position.truthy("simulated") {
            return Ok(false);
        }
        let config = self.config().await?;
        let left = config.int("first_live_entries_left");
        if left <= 0 {
            return Ok(false);
        }
        self.audit(
            "first_live_entries_left",
            Some(left.to_string()),
            Some((left - 1).to_string()),
            now,
            "twt-fill",
            None,
        )
        .await?;
        let sql = format!(
            "UPDATE {} SET first_live_entries_left = ?, updated_by = ? WHERE user_id = ?",
            self.t("tw_config")
        );
        self.conn
            .execute(
                &sql,
                &[
                    Param::Int(left - 1),
                    Param::from("twt-fill"),
                    Param::Int(self.user_id),
                ],
            )
            .await?;
        let sql = format!(
            "UPDATE {} SET half_size = ? WHERE user_id = ? AND id = ?",
            self.t("tw_position")
        );
        self.conn
            .execute(
                &sql,
                &[
                    Param::Bool(true),
                    Param::Int(self.user_id),
                    Param::Int(position_id),
                ],
            )
            .await?;
        let sql = format!(
            "INSERT INTO {} (user_id, session_date, mode, first_live_entries_counted) \
             VALUES (?, ?, 'LIVE', 1) ON CONFLICT (user_id, session_date) DO UPDATE SET \
             first_live_entries_counted = tw_session.first_live_entries_counted + 1",
            self.t("tw_session")
        );
        self.conn
            .execute(&sql, &[Param::Int(self.user_id), Param::Date(session_date)])
            .await?;
        Ok(true)
    }

    async fn expire_plans(&mut self, now: DateTime<FixedOffset>) -> StoreResult<i64> {
        let sql = format!(
            "UPDATE {} SET expires_at = ? WHERE user_id = ? AND expires_at > ?",
            self.t("tw_plan")
        );
        let cursor = self
            .conn
            .execute(
                &sql,
                &[
                    Param::Aware(now),
                    Param::Int(self.user_id),
                    Param::Aware(now),
                ],
            )
            .await?;
        Ok(cursor.rowcount.max(0))
    }

    async fn zero_capital(
        &mut self,
        now: DateTime<FixedOffset>,
        changed_by: &str,
    ) -> StoreResult<Option<Decimal>> {
        let config = self.config().await?;
        let before = config.dec("sleeve_capital_inr");
        let sql = format!(
            "SELECT 1 AS present FROM {} WHERE user_id = ?",
            self.t("tw_config")
        );
        if self.one(&sql, &[Param::Int(self.user_id)]).await?.is_none() {
            return Ok(None);
        }
        self.audit(
            "sleeve_capital_inr",
            before.map(dstr),
            Some("0".to_owned()),
            now,
            changed_by,
            Some("halted from the desk: the sleeve cannot size a line at zero"),
        )
        .await?;
        let sql = format!(
            "UPDATE {} SET sleeve_capital_inr = 0, updated_by = ? WHERE user_id = ?",
            self.t("tw_config")
        );
        self.conn
            .execute(&sql, &[Param::from(changed_by), Param::Int(self.user_id)])
            .await?;
        Ok(before)
    }

    async fn todays_plan(&mut self, on_or_before: NaiveDate) -> StoreResult<Option<Rec>> {
        let sql = format!(
            "SELECT id, plan_id, session_date, source, built_at, expires_at, gate, \
             sleeve_equity_inr, total_new_exposure_inr FROM {} \
             WHERE user_id = ? AND session_date <= ? ORDER BY session_date DESC, built_at DESC \
             LIMIT 1",
            self.t("tw_plan")
        );
        match self
            .one(&sql, &[Param::Int(self.user_id), Param::Date(on_or_before)])
            .await?
        {
            Some(row) => Ok(Some(plan_row(&row)?)),
            None => Ok(None),
        }
    }

    async fn lines_for(&mut self, plan_pk: i64) -> StoreResult<Vec<Rec>> {
        let sql = format!(
            "{LINE_SELECT}{}WHERE l.user_id = ? AND l.plan_id = ?",
            self.line_from()
        );
        let rows = self
            .rows(&sql, &[Param::Int(self.user_id), Param::Int(plan_pk)])
            .await?;
        let mut lines: Vec<Rec> = rows.iter().map(line_row).collect::<StoreResult<_>>()?;
        sort_lines(&mut lines, &KIND_ORDER)?;
        Ok(lines)
    }

    async fn is_trading_day(&mut self, day: NaiveDate) -> StoreResult<Option<bool>> {
        let sql = format!(
            "SELECT is_trading_day FROM {} WHERE date = ?",
            self.t("trading_day")
        );
        let row = self.one(&sql, &[Param::Date(day)]).await?;
        Ok(row.map(|r| db_bool(r.at(0))))
    }
}

/// `sorted(lines, key=lambda l: (KIND_ORDER.index(l["kind"]), l["symbol"]))` — a kind not in the
/// order is Python's `ValueError`.
///
/// # Errors
/// An unknown kind.
pub fn sort_lines(lines: &mut [Rec], order: &[&str]) -> StoreResult<()> {
    for line in lines.iter() {
        let kind = line.s("kind");
        if !order.contains(&kind.as_str()) {
            return Err(StoreError::Bad(format!(
                "ValueError: '{kind}' is not in tuple"
            )));
        }
    }
    lines.sort_by(|a, b| {
        let ka = order.iter().position(|k| *k == a.s("kind")).unwrap_or(0);
        let kb = order.iter().position(|k| *k == b.s("kind")).unwrap_or(0);
        ka.cmp(&kb).then_with(|| a.s("symbol").cmp(&b.s("symbol")))
    });
    Ok(())
}
