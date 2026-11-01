//! `app/vbt_desk.py::PgVbtStore` and the `VbtStore` protocol (`app/vbt_execute.py`): one user's
//! `vb_` rows through the desk's connection, SQL text unchanged (SQLite dialect with `?`, run on
//! Postgres through `storage`'s translation, exactly as `analytics.pg` runs it).
//!
//! One deliberate difference, recorded in DECISIONS-RS (the vbt lane's entry): on SQLite,
//! `lock_session_for_update` does not issue `BEGIN IMMEDIATE`. (Corrected in adversarial
//! review: the desk's sqlite3 connection is `isolation_level=None`, so `in_transaction` is
//! `False` after the `INSERT … ON CONFLICT DO NOTHING` and Python **does** send `BEGIN
//! IMMEDIATE` — and nothing ever commits it, so the confirm's writes are rolled back when the
//! connection closes. That path is unreachable in production: a SQLite desk has no `vb_`
//! tables, and the SQLite binder refuses the execute path's `Decimal`s (R8.2). Not copying a
//! transaction nobody commits is the safer half.)
//!
//! **The lock is one transaction per confirm (R8.VB1, Python first).** The desk's connections
//! are autocommit, so a bare `SELECT … FOR UPDATE` released its row lock when the statement
//! ended and two tabs confirming together passed the session cap (`04` §5.3/§9.4) between them.
//! Now the outermost `lock_session_for_update` opens `BEGIN`, takes the sleeve's per-user
//! transaction lock (`pg_try_advisory_xact_lock(hashtext('vb_session:<user>'))` — the cap counts
//! the IST day the orders were sent, while a confirm locks its *plan's* session row, so a MORNING
//! and a LIVE line would otherwise lock different rows) and the row `FOR UPDATE NOWAIT`; a held
//! lock is a 409 `SESSION_BUSY` before anything is read or sent; [`VbtStore::unlock_session`]
//! commits on the way out, on an error too. On SQLite the lock is `BEGIN IMMEDIATE` … `COMMIT`,
//! as Python's now is (the earlier deliberate difference — Python began a transaction nobody
//! committed — is gone with the fix).

use async_trait::async_trait;
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, TimeZone, Utc};
use rust_decimal::Decimal;

use super::super::twt::common::{
    Rec, Val, db_bool, db_date, db_dec, db_int, db_raw, db_stamp, db_text, ist_day, param,
    py_fromisoformat_naive,
};
use crate::storage::{Conn, DbValue, Param, Row, StorageError};

/// `SCHEMA`.
pub const SCHEMA: &str = "public";

/// `KIND_ORDER`: the order `04` §9.3 renders a plan in — risk coming off before risk going on.
pub const KIND_ORDER: [&str; 6] = [
    "SELL_AT_OPEN",
    "CANCEL_LIMIT",
    "ARM_GTT",
    "RAISE_GTT_STOP",
    "PLACE_LIMIT",
    "BUY_AT_MARKET",
];

/// `RESCAN_IN_FLIGHT` (mirrored from `baskfy_worker.tasks.vbt_rescan`).
pub const RESCAN_IN_FLIGHT: [&str; 2] = ["QUEUED", "RUNNING"];
/// `RESCAN_STALE_AFTER_SECONDS`.
pub const RESCAN_STALE_AFTER_SECONDS: i64 = 600;
/// `RESCAN_MIN_INTERVAL_SECONDS`.
pub const RESCAN_MIN_INTERVAL_SECONDS: i64 = 60;

/// `ScanRefused`: the two refusals, carrying the HTTP status the contract answers with.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScanRefused {
    /// 409 or 429.
    pub status: u16,
    /// The words.
    pub reason: String,
    /// The run it names.
    pub run_id: i64,
    /// That run's status.
    pub run_status: String,
    /// `Retry-After` seconds (429 only).
    pub retry_after: Option<i64>,
}

/// Why a store call failed.
#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    /// The database.
    #[error(transparent)]
    Storage(#[from] StorageError),
    /// `ScanRefused`.
    #[error("{}", .0.reason)]
    Scan(ScanRefused),
    /// A row the code cannot read (Python's `KeyError`/`ValueError`/`TypeError`).
    #[error("{0}")]
    Bad(String),
    /// `HTTPException(409, SESSION_BUSY)`: the session lock is held by another confirm
    /// (`NOWAIT`, R8.VB1).
    #[error("{SESSION_BUSY}")]
    SessionBusy,
}

/// Postgres's SQLSTATE for `FOR UPDATE NOWAIT` finding the row held (`LOCK_NOT_AVAILABLE`).
pub const LOCK_NOT_AVAILABLE: &str = "55P03";

/// `SESSION_LOCK_KEY`: the text hashed into the sleeve's per-user transaction lock.
pub const SESSION_LOCK_KEY: &str = "vb_session";

/// `SESSION_BUSY`: what a confirm that finds the session lock held answers (409, R8.VB1).
pub const SESSION_BUSY: &str = "SESSION_BUSY: another confirm of this session is in flight; \
     nothing was sent \u{2014} confirm again";

/// What the store calls return.
pub type StoreResult<T> = Result<T, StoreError>;

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
    /// `mode`.
    pub mode: String,
    /// `confirms`.
    pub confirms: i64,
    /// `fills`.
    pub fills: i64,
    /// `exits`.
    pub exits: i64,
}

/// The `VbtStore` protocol. All prices `Decimal`.
#[async_trait]
pub trait VbtStore: Send {
    /// `plan(plan_id)`: `None` for an unknown plan, including a malformed id.
    async fn plan(&mut self, plan_id: &str) -> StoreResult<Option<Rec>>;
    /// `line(line_id)`.
    async fn line(&mut self, line_id: i64) -> StoreResult<Option<Rec>>;
    /// `set_line(line_id, state=..., ...)`.
    async fn set_line(&mut self, line_id: i64, state: &str, extra: LineUpdate) -> StoreResult<()>;
    /// `open_position_for(instrument_id)`.
    async fn open_position_for(&mut self, instrument_id: i64) -> StoreResult<Option<Rec>>;
    /// `position(position_id)`.
    async fn position(&mut self, position_id: i64) -> StoreResult<Option<Rec>>;
    /// `create_position(fields)`.
    async fn create_position(&mut self, fields: Rec) -> StoreResult<i64>;
    /// `update_position(position_id, fields)`.
    async fn update_position(&mut self, position_id: i64, fields: Rec) -> StoreResult<()>;
    /// `add_fill(fields)`.
    async fn add_fill(&mut self, fields: Rec) -> StoreResult<i64>;
    /// `order(order_id)`.
    async fn order(&mut self, order_id: i64) -> StoreResult<Option<Rec>>;
    /// `create_order(fields)`.
    async fn create_order(&mut self, fields: Rec) -> StoreResult<i64>;
    /// `update_order(order_id, fields)`.
    async fn update_order(&mut self, order_id: i64, fields: Rec) -> StoreResult<()>;
    /// `working_order_for(instrument_id)`.
    async fn working_order_for(&mut self, instrument_id: i64) -> StoreResult<Option<Rec>>;
    /// `order_by_broker_id(broker_order_id)` (LV2).
    async fn order_by_broker_id(&mut self, broker_order_id: &str) -> StoreResult<Option<Rec>>;
    /// `open_orders()` (LV2).
    async fn open_orders(&mut self) -> StoreResult<Vec<Rec>>;
    /// `open_positions()`: this sleeve's `OPEN` positions with shares.
    async fn open_positions(&mut self) -> StoreResult<Vec<Rec>>;
    /// `open_protection_issues()` (LV2).
    async fn open_protection_issues(&mut self) -> StoreResult<Vec<Rec>>;
    /// `create_exit_order(fields)`.
    async fn create_exit_order(&mut self, fields: Rec) -> StoreResult<i64>;
    /// `exit_order_by_broker_id(broker_order_id)`.
    async fn exit_order_by_broker_id(&mut self, broker_order_id: &str) -> StoreResult<Option<Rec>>;
    /// `update_exit_order(exit_order_id, fields)`.
    async fn update_exit_order(&mut self, exit_order_id: i64, fields: Rec) -> StoreResult<()>;
    /// `line_by_client_id(client_id)`.
    async fn line_by_client_id(&mut self, client_id: &str) -> StoreResult<Option<Rec>>;
    /// `bump_session(day, mode=..., confirms=..., fills=..., exits=...)`.
    async fn bump_session(&mut self, day: NaiveDate, bump: SessionBump) -> StoreResult<()>;
    /// `lock_session_for_update(day)`: the context manager's entry. Its exit is
    /// [`VbtStore::unlock_session`], which the confirm functions call once their body is done.
    async fn lock_session_for_update(&mut self, day: NaiveDate) -> StoreResult<()>;
    /// The exit of the outermost `with store.lock_session_for_update(day):` block: `COMMIT` of
    /// the transaction the lock opened, on success and on an error alike (R8.VB1). A store with
    /// no transaction to close does nothing.
    async fn unlock_session(&mut self) -> StoreResult<()> {
        Ok(())
    }
    /// `order_for_signal(instrument_id, signal_date)`: the `vb_order` already written for this
    /// name on this signal, in any state (`uq_vb_order_one_per_signal`, R8.VB2). A store that
    /// does not implement it **refuses** (fail closed): an entry is never sent unasked.
    async fn order_for_signal(
        &mut self,
        instrument_id: i64,
        signal_date: NaiveDate,
    ) -> StoreResult<Option<i64>> {
        let _ = (instrument_id, signal_date);
        Err(StoreError::Bad(
            "order_for_signal is not implemented by this store".to_owned(),
        ))
    }
    /// `entries_taken(day)`: orders sent on the IST day (LV10.2).
    async fn entries_taken(&mut self, day: NaiveDate) -> StoreResult<i64>;
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
        Some(v) => Val::Str(db_text(v)),
    }
}

fn int_req(row: &Row, name: &str) -> StoreResult<Val> {
    match col(row, name) {
        Some(DbValue::Int(i)) => Ok(Val::Int(*i)),
        Some(DbValue::Bool(b)) => Ok(Val::Int(i64::from(*b))),
        #[allow(clippy::cast_possible_truncation)]
        Some(DbValue::Float(f)) => Ok(Val::Int(f.trunc() as i64)),
        Some(DbValue::Text(s)) => {
            s.trim().parse().map(Val::Int).map_err(|_| {
                StoreError::Bad(format!("ValueError: invalid literal for int(): {s:?}"))
            })
        }
        _ => Err(StoreError::Bad(format!(
            "TypeError: int() argument must be a string, a bytes-like object or a real number, \
             not 'NoneType' ({name})"
        ))),
    }
}

fn opt_int(row: &Row, name: &str) -> Val {
    match col(row, name) {
        None | Some(DbValue::Null) => Val::None,
        other => Val::Int(db_int(other)),
    }
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

/// `_as_datetime(value)`: a stored timestamp, always aware — a naive one is **UTC** here (not
/// IST, as `_stamp` reads it).
#[must_use]
pub fn as_datetime(v: &Val) -> Option<DateTime<FixedOffset>> {
    match v {
        Val::Stamp(t) => Some(*t),
        Val::Naive(t) => Some(Utc.from_utc_datetime(t).fixed_offset()),
        Val::Str(s) => {
            if let Ok(t) = DateTime::parse_from_rfc3339(&s.replace(' ', "T")) {
                return Some(t);
            }
            py_fromisoformat_naive(s).map(|t| Utc.from_utc_datetime(&t).fixed_offset())
        }
        _ => None,
    }
}

/// `_stamp(value)` on a raw value the store passed through.
#[must_use]
pub fn stamp_of(v: &Val) -> Val {
    let ist = super::super::twt::common::ist();
    match v {
        Val::Stamp(t) => Val::Stamp(*t),
        Val::Naive(t) => ist
            .from_local_datetime(t)
            .single()
            .map_or(Val::None, Val::Stamp),
        Val::None => Val::None,
        other => {
            let text = other.py_str();
            let naive = py_fromisoformat_naive(&text).or_else(|| {
                DateTime::parse_from_rfc3339(&text.replace(' ', "T"))
                    .ok()
                    .map(|t| t.naive_local())
            });
            naive
                .and_then(|n| ist.from_local_datetime(&n).single())
                .map_or(Val::None, Val::Stamp)
        }
    }
}

/// `_date(value)` on a raw value the store passed through.
#[must_use]
pub fn date_of(v: &Val) -> Val {
    match v {
        Val::None => Val::None,
        Val::Date(d) => Val::Date(*d),
        Val::Stamp(t) => Val::Date(t.date_naive()),
        Val::Naive(t) => Val::Date(t.date()),
        other => {
            NaiveDate::parse_from_str(&other.py_str(), "%Y-%m-%d").map_or(Val::None, Val::Date)
        }
    }
}

const LINE_SELECT: &str = "SELECT l.id, l.plan_id AS plan_pk, p.plan_id, l.kind, l.instrument_id, i.symbol, \
     l.quantity, l.limit_price, l.stop_price, l.value_inr, l.size_cap, l.reason, l.note, \
     l.state, l.client_id, l.journal_ref, l.order_id, l.position_id, \
     p.session_date, p.source AS plan_source, p.expires_at ";

const POSITION_SELECT: &str = "SELECT p.id, p.instrument_id, i.symbol, p.entry_date, p.entry_avg, \
     p.quantity_entered, p.quantity_open, p.initial_stop, p.stop_price, p.gtt_id, \
     p.gtt_trigger, p.gtt_armed_at, p.state, p.exit_queued_for, p.exit_reason_queued, \
     p.closed_on, p.exit_avg, p.close_reason, p.pnl_inr, p.return_pct, p.simulated, \
     p.partial_done, p.trail ";

const ORDER_SELECT: &str = "SELECT o.id, o.instrument_id, i.symbol, o.signal_date, o.limit_price, o.stop_price, \
     o.quantity, o.state, o.working_from, o.expires_after_session, o.sessions_worked, \
     o.broker_order_id, o.client_id, o.filled_quantity, o.avg_fill_price, o.position_id, \
     o.cancelled_on, o.cancel_reason, o.simulated ";

const EXIT_SELECT: &str = "SELECT id, sleeve, position_id, line_id, symbol, broker_order_id, client_id, quantity, \
     reference_price, state, filled_quantity, avg_fill_price, reason, simulated ";

const SCAN_SELECT: &str = "SELECT id, requested_at, started_at, finished_at, session_date, status, source, \
     detail, error, task_id FROM ";

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

fn line_row(row: &Row) -> StoreResult<Rec> {
    Ok(Rec::of([
        ("id", int_req(row, "id")?),
        ("plan_pk", int_req(row, "plan_pk")?),
        ("plan_id", str_val(row, "plan_id")),
        ("kind", str_val(row, "kind")),
        ("instrument_id", int_req(row, "instrument_id")?),
        ("symbol", str_val(row, "symbol")),
        ("quantity", Val::Int(db_int(col(row, "quantity")))),
        ("limit_price", dec_val(row, "limit_price")),
        ("stop_price", dec_val(row, "stop_price")),
        ("value_inr", dec_val(row, "value_inr")),
        ("size_cap", db_raw(col(row, "size_cap"))),
        ("reason", db_raw(col(row, "reason"))),
        ("note", db_raw(col(row, "note"))),
        ("state", str_val(row, "state")),
        ("client_id", db_raw(col(row, "client_id"))),
        ("journal_ref", db_raw(col(row, "journal_ref"))),
        ("order_id", db_raw(col(row, "order_id"))),
        ("position_id", db_raw(col(row, "position_id"))),
        ("session_date", date_val(row, "session_date")?),
        ("plan_source", str_val(row, "plan_source")),
        ("expires_at", stamp_val(row, "expires_at")?),
    ]))
}

fn position_row(row: &Row) -> StoreResult<Rec> {
    Ok(Rec::of([
        ("id", int_req(row, "id")?),
        ("instrument_id", int_req(row, "instrument_id")?),
        ("symbol", str_val(row, "symbol")),
        ("entry_date", date_val(row, "entry_date")?),
        ("entry_avg", dec_val(row, "entry_avg")),
        ("quantity_entered", int_req(row, "quantity_entered")?),
        ("quantity_open", int_req(row, "quantity_open")?),
        ("initial_stop", dec_val(row, "initial_stop")),
        ("stop_price", dec_val(row, "stop_price")),
        ("gtt_id", db_raw(col(row, "gtt_id"))),
        ("gtt_trigger", dec_val(row, "gtt_trigger")),
        ("gtt_armed_at", stamp_val(row, "gtt_armed_at")?),
        ("state", str_val(row, "state")),
        ("exit_queued_for", date_val(row, "exit_queued_for")?),
        ("exit_reason_queued", db_raw(col(row, "exit_reason_queued"))),
        ("closed_on", date_val(row, "closed_on")?),
        ("exit_avg", dec_val(row, "exit_avg")),
        ("close_reason", db_raw(col(row, "close_reason"))),
        ("pnl_inr", dec_val(row, "pnl_inr")),
        ("return_pct", dec_val(row, "return_pct")),
        ("simulated", Val::Bool(db_bool(col(row, "simulated")))),
        ("partial_done", Val::Bool(db_bool(col(row, "partial_done")))),
        ("trail", db_raw(col(row, "trail"))),
    ]))
}

fn order_row(row: &Row) -> StoreResult<Rec> {
    Ok(Rec::of([
        ("id", int_req(row, "id")?),
        ("instrument_id", int_req(row, "instrument_id")?),
        ("symbol", str_val(row, "symbol")),
        ("signal_date", date_val(row, "signal_date")?),
        ("limit_price", dec_val(row, "limit_price")),
        ("stop_price", dec_val(row, "stop_price")),
        ("quantity", int_req(row, "quantity")?),
        ("state", str_val(row, "state")),
        ("working_from", date_val(row, "working_from")?),
        (
            "expires_after_session",
            date_val(row, "expires_after_session")?,
        ),
        (
            "sessions_worked",
            Val::Int(db_int(col(row, "sessions_worked"))),
        ),
        ("broker_order_id", db_raw(col(row, "broker_order_id"))),
        ("client_id", db_raw(col(row, "client_id"))),
        (
            "filled_quantity",
            Val::Int(db_int(col(row, "filled_quantity"))),
        ),
        ("avg_fill_price", dec_val(row, "avg_fill_price")),
        ("position_id", db_raw(col(row, "position_id"))),
        ("cancelled_on", date_val(row, "cancelled_on")?),
        ("cancel_reason", db_raw(col(row, "cancel_reason"))),
        ("simulated", Val::Bool(db_bool(col(row, "simulated")))),
    ]))
}

fn exit_row(row: &Row) -> StoreResult<Rec> {
    Ok(Rec::of([
        ("id", int_req(row, "id")?),
        ("sleeve", str_val(row, "sleeve")),
        ("position_id", int_req(row, "position_id")?),
        ("line_id", opt_int(row, "line_id")),
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
        ("reason", str_val(row, "reason")),
        ("simulated", Val::Bool(db_bool(col(row, "simulated")))),
    ]))
}

/// `_scan_run_row`: timestamps left as the driver handed them over; `detail` parsed when text.
fn scan_run_row(row: &Row) -> StoreResult<Rec> {
    let detail = match col(row, "detail") {
        Some(DbValue::Text(s)) => crate::wire::PyValue::from_json(s.as_bytes())
            .map_err(|e| StoreError::Bad(format!("json.decoder.JSONDecodeError: {e}")))
            .map(Val::Py)?,
        other => db_raw(other),
    };
    Ok(Rec::of([
        ("id", int_req(row, "id")?),
        ("requested_at", db_raw(col(row, "requested_at"))),
        ("started_at", db_raw(col(row, "started_at"))),
        ("finished_at", db_raw(col(row, "finished_at"))),
        ("session_date", db_raw(col(row, "session_date"))),
        ("status", db_raw(col(row, "status"))),
        ("source", db_raw(col(row, "source"))),
        ("detail", detail),
        ("error", db_raw(col(row, "error"))),
        ("task_id", db_raw(col(row, "task_id"))),
    ]))
}

/// `timedelta.total_seconds()`.
#[must_use]
pub fn total_seconds(d: Duration) -> f64 {
    #[allow(clippy::cast_precision_loss)]
    let micros = d.num_microseconds().unwrap_or(i64::MAX) as f64;
    micros / 1e6
}

/// `PgVbtStore`: one user's `vb_` rows through the desk's connection.
pub struct PgVbtStore {
    /// The connection.
    pub conn: Conn,
    /// `user_id`.
    pub user_id: i64,
    /// `"public"` on Postgres, `""` on SQLite.
    pub schema: String,
    /// `broker_account_id`.
    pub broker_account_id: Option<i64>,
    /// `_lock_depth > 0` with a transaction this store opened (R8.VB1).
    lock_opened: bool,
    /// `_lock_depth`: open `lock_session_for_update` blocks; only the outermost commits.
    lock_depth: u32,
}

impl std::fmt::Debug for PgVbtStore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PgVbtStore")
            .field("user_id", &self.user_id)
            .field("schema", &self.schema)
            .finish_non_exhaustive()
    }
}

impl PgVbtStore {
    /// `PgVbtStore(conn, user_id, schema, broker_account_id=...)`.
    #[must_use]
    pub fn new(conn: Conn, user_id: i64, schema: &str, broker_account_id: Option<i64>) -> Self {
        Self {
            conn,
            user_id,
            schema: schema.to_owned(),
            broker_account_id,
            lock_opened: false,
            lock_depth: 0,
        }
    }

    /// Whether a `lock_session_for_update` transaction is open on this store.
    #[must_use]
    pub const fn session_locked(&self) -> bool {
        self.lock_opened
    }

    /// The Postgres half of `lock_session_for_update` inside the transaction: the sleeve's
    /// per-user lock, then the day's row, both without waiting.
    async fn take_locks(&mut self, params: &[Param]) -> StoreResult<()> {
        let key = [Param::Text(format!("{SESSION_LOCK_KEY}:{}", self.user_id))];
        let got = self
            .one("SELECT pg_try_advisory_xact_lock(hashtext(?)) AS got", &key)
            .await?;
        if !got.as_ref().is_some_and(|r| db_bool(col(r, "got"))) {
            return Err(StoreError::SessionBusy);
        }
        let sql = format!(
            "SELECT 1 FROM {} WHERE user_id = ? AND session_date = ? FOR UPDATE NOWAIT",
            self.t("vb_session")
        );
        match self.conn.execute(&sql, params).await {
            Ok(_) => Ok(()),
            Err(StorageError::Driver(sqlx::Error::Database(db)))
                if db.code().as_deref() == Some(LOCK_NOT_AVAILABLE) =>
            {
                Err(StoreError::SessionBusy)
            }
            Err(e) => Err(e.into()),
        }
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
            "FROM {} l JOIN {} p ON p.id = l.plan_id JOIN {} i ON i.id = l.instrument_id ",
            self.t("vb_plan_line"),
            self.t("vb_plan"),
            self.t("instrument")
        )
    }

    fn position_from(&self) -> String {
        format!(
            "FROM {} p JOIN {} i ON i.id = p.instrument_id ",
            self.t("vb_position"),
            self.t("instrument")
        )
    }

    fn order_from(&self) -> String {
        format!(
            "FROM {} o JOIN {} i ON i.id = o.instrument_id ",
            self.t("vb_order"),
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

    fn broker(&self) -> Val {
        Val::from(self.broker_account_id)
    }

    /// `book()`: every `OPEN` position with shares, by entry date then symbol.
    ///
    /// # Errors
    /// The database.
    pub async fn book(&mut self) -> StoreResult<Vec<Rec>> {
        let sql = format!(
            "{POSITION_SELECT}{}WHERE p.user_id = ? AND p.state = 'OPEN' AND p.quantity_open > 0 \
             ORDER BY p.entry_date, i.symbol",
            self.position_from()
        );
        let rows = self.rows(&sql, &[Param::Int(self.user_id)]).await?;
        rows.iter().map(position_row).collect()
    }

    /// `todays_plan(on_or_before)`: the newest plan at or before the date.
    ///
    /// # Errors
    /// The database.
    pub async fn todays_plan(&mut self, on_or_before: NaiveDate) -> StoreResult<Option<Rec>> {
        let sql = format!(
            "SELECT id, plan_id, session_date, source, built_at, expires_at, gate, \
             sleeve_equity_inr, total_new_exposure_inr FROM {} \
             WHERE user_id = ? AND session_date <= ? ORDER BY session_date DESC, built_at DESC \
             LIMIT 1",
            self.t("vb_plan")
        );
        match self
            .one(&sql, &[Param::Int(self.user_id), Param::Date(on_or_before)])
            .await?
        {
            Some(row) => Ok(Some(plan_row(&row)?)),
            None => Ok(None),
        }
    }

    /// `lines_for(plan_pk)`, in `KIND_ORDER` then symbol.
    ///
    /// # Errors
    /// The database, or a kind outside `KIND_ORDER` (Python's `ValueError`).
    pub async fn lines_for(&mut self, plan_pk: i64) -> StoreResult<Vec<Rec>> {
        let sql = format!(
            "{LINE_SELECT}{}WHERE l.user_id = ? AND l.plan_id = ?",
            self.line_from()
        );
        let rows = self
            .rows(&sql, &[Param::Int(self.user_id), Param::Int(plan_pk)])
            .await?;
        let mut lines = rows.iter().map(line_row).collect::<StoreResult<Vec<_>>>()?;
        for line in &lines {
            let kind = line.s("kind");
            if !KIND_ORDER.contains(&kind.as_str()) {
                return Err(StoreError::Bad(format!(
                    "ValueError: tuple.index(x): x not in tuple ({kind})"
                )));
            }
        }
        lines.sort_by(|a, b| {
            let pos = |r: &Rec| KIND_ORDER.iter().position(|k| *k == r.s("kind"));
            pos(a)
                .cmp(&pos(b))
                .then_with(|| a.s("symbol").cmp(&b.s("symbol")))
        });
        Ok(lines)
    }

    /// `skips_for(plan_pk)`.
    ///
    /// # Errors
    /// The database.
    pub async fn skips_for(&mut self, plan_pk: i64) -> StoreResult<Vec<Rec>> {
        let sql = format!(
            "SELECT s.reason, s.detail, i.symbol FROM {} s JOIN {} i ON i.id = s.instrument_id \
             WHERE s.user_id = ? AND s.plan_id = ? ORDER BY i.symbol",
            self.t("vb_plan_skip"),
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

    /// `working()`: the resting orders, by signal date then symbol.
    ///
    /// # Errors
    /// The database.
    pub async fn working(&mut self) -> StoreResult<Vec<Rec>> {
        let sql = format!(
            "{ORDER_SELECT}{}WHERE o.user_id = ? AND o.state IN ('PROPOSED', 'CONFIRMED', 'SENT', 'PARTIAL') \
             ORDER BY o.signal_date, i.symbol",
            self.order_from()
        );
        let rows = self.rows(&sql, &[Param::Int(self.user_id)]).await?;
        rows.iter().map(order_row).collect()
    }

    /// `newest_scan()`: this user's most recent re-detect request (VB12).
    ///
    /// # Errors
    /// The database.
    pub async fn newest_scan(&mut self) -> StoreResult<Option<Rec>> {
        let sql = format!(
            "{SCAN_SELECT}{} WHERE user_id = ? ORDER BY requested_at DESC, id DESC LIMIT 1",
            self.t("vb_scan_run")
        );
        match self.one(&sql, &[Param::Int(self.user_id)]).await? {
            Some(row) => Ok(Some(scan_run_row(&row)?)),
            None => Ok(None),
        }
    }

    /// `scan_run(run_id)`: one run of **this user's**.
    ///
    /// # Errors
    /// The database.
    pub async fn scan_run(&mut self, run_id: i64) -> StoreResult<Option<Rec>> {
        let sql = format!(
            "{SCAN_SELECT}{} WHERE user_id = ? AND id = ?",
            self.t("vb_scan_run")
        );
        match self
            .one(&sql, &[Param::Int(self.user_id), Param::Int(run_id)])
            .await?
        {
            Some(row) => Ok(Some(scan_run_row(&row)?)),
            None => Ok(None),
        }
    }

    /// `request_scan(now=...)`: insert one `QUEUED` row, or one of the two refusals.
    ///
    /// # Errors
    /// [`StoreError::Scan`] (409, 429), or the database.
    pub async fn request_scan(
        &mut self,
        now: DateTime<FixedOffset>,
        min_interval: Option<Duration>,
        stale_after: Option<Duration>,
    ) -> StoreResult<i64> {
        let window = min_interval.unwrap_or_else(|| Duration::seconds(RESCAN_MIN_INTERVAL_SECONDS));
        let stale = stale_after.unwrap_or_else(|| Duration::seconds(RESCAN_STALE_AFTER_SECONDS));
        if let Some(newest) = self.newest_scan().await? {
            let requested = as_datetime(&newest.val("requested_at")).ok_or_else(|| {
                StoreError::Bad(format!(
                    "TypeError: unsupported requested_at {}",
                    newest.s("requested_at")
                ))
            })?;
            let age = now - requested;
            let status = newest.s("status");
            let id = newest.int("id");
            if RESCAN_IN_FLIGHT.contains(&status.as_str()) && age < stale {
                return Err(StoreError::Scan(ScanRefused {
                    status: 409,
                    reason: format!(
                        "Scan {id} is {}; its result is on its way.",
                        status.to_lowercase()
                    ),
                    run_id: id,
                    run_status: status,
                    retry_after: None,
                }));
            }
            if age < window {
                let wait = window - age;
                #[allow(clippy::cast_possible_truncation)]
                let seconds = ((total_seconds(wait) + 0.999).trunc() as i64).max(1);
                #[allow(clippy::cast_possible_truncation)]
                let ago = total_seconds(age).trunc() as i64;
                return Err(StoreError::Scan(ScanRefused {
                    status: 429,
                    reason: format!(
                        "A scan was requested {ago} s ago; one a minute is the limit. Try again \
                         in {seconds} s."
                    ),
                    run_id: id,
                    run_status: status,
                    retry_after: Some(seconds),
                }));
            }
        }
        let sql = format!(
            "INSERT INTO {} (user_id, requested_at, status, source) \
             VALUES (?, ?, 'QUEUED', 'desk') RETURNING id",
            self.t("vb_scan_run")
        );
        let row = self
            .one(&sql, &[Param::Int(self.user_id), Param::Aware(now)])
            .await?
            .ok_or_else(|| StoreError::Bad("INSERT ... RETURNING gave no row".to_owned()))?;
        Ok(db_int(row.get("id")))
    }

    /// `session(day)`.
    ///
    /// # Errors
    /// The database.
    pub async fn session(&mut self, day: NaiveDate) -> StoreResult<Option<Rec>> {
        let sql = format!(
            "SELECT session_date, mode, gate, signals, confirms, fills, exits \
             FROM {} WHERE user_id = ? AND session_date = ?",
            self.t("vb_session")
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
            ("signals", n("signals")),
            ("confirms", n("confirms")),
            ("fills", n("fills")),
            ("exits", n("exits")),
        ])))
    }

    /// `config()`.
    ///
    /// # Errors
    /// The database.
    pub async fn config(&mut self) -> StoreResult<Rec> {
        let sql = format!(
            "SELECT sleeve_capital_inr, max_open_positions, max_position_pct, stop_pct, \
             dry_run_sessions, first_live_sessions_left FROM {} WHERE user_id = ?",
            self.t("vb_config")
        );
        let Some(row) = self.one(&sql, &[Param::Int(self.user_id)]).await? else {
            return Ok(Rec::of([
                ("sleeve_capital_inr", Val::Dec(Decimal::ZERO)),
                ("dry_run_sessions", Val::Int(0)),
            ]));
        };
        Ok(Rec::of([
            ("sleeve_capital_inr", dec_val(&row, "sleeve_capital_inr")),
            ("max_open_positions", int_req(&row, "max_open_positions")?),
            ("max_position_pct", dec_val(&row, "max_position_pct")),
            ("stop_pct", dec_val(&row, "stop_pct")),
            (
                "dry_run_sessions",
                Val::Int(db_int(col(&row, "dry_run_sessions"))),
            ),
            (
                "first_live_sessions_left",
                Val::Int(db_int(col(&row, "first_live_sessions_left"))),
            ),
        ]))
    }

    /// `breadth(on_or_before)`.
    ///
    /// # Errors
    /// The database.
    pub async fn breadth(&mut self, on_or_before: NaiveDate) -> StoreResult<Option<Rec>> {
        let sql = format!(
            "SELECT date, measured_count, above_count, pct_above_dma, gate, thin_session \
             FROM {} WHERE user_id = ? AND date <= ? ORDER BY date DESC LIMIT 1",
            self.t("vb_breadth_daily")
        );
        let Some(row) = self
            .one(&sql, &[Param::Int(self.user_id), Param::Date(on_or_before)])
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

    async fn one_line(&mut self, tail: &str, params: &[Param]) -> StoreResult<Option<Rec>> {
        let sql = format!("{LINE_SELECT}{}{tail}", self.line_from());
        match self.one(&sql, params).await? {
            Some(row) => Ok(Some(line_row(&row)?)),
            None => Ok(None),
        }
    }

    async fn one_position(&mut self, tail: &str, params: &[Param]) -> StoreResult<Option<Rec>> {
        let sql = format!("{POSITION_SELECT}{}{tail}", self.position_from());
        match self.one(&sql, params).await? {
            Some(row) => Ok(Some(position_row(&row)?)),
            None => Ok(None),
        }
    }

    async fn one_order(&mut self, tail: &str, params: &[Param]) -> StoreResult<Option<Rec>> {
        let sql = format!("{ORDER_SELECT}{}{tail}", self.order_from());
        match self.one(&sql, params).await? {
            Some(row) => Ok(Some(order_row(&row)?)),
            None => Ok(None),
        }
    }
}

#[async_trait]
impl VbtStore for PgVbtStore {
    async fn plan(&mut self, plan_id: &str) -> StoreResult<Option<Rec>> {
        if !is_uuid(plan_id) {
            return Ok(None);
        }
        let sql = format!(
            "SELECT id, plan_id, session_date, source, built_at, expires_at, gate, \
             sleeve_equity_inr, total_new_exposure_inr FROM {} \
             WHERE user_id = ? AND plan_id = ?",
            self.t("vb_plan")
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
        let uid = self.user_id;
        self.one_line(
            "WHERE l.user_id = ? AND l.id = ?",
            &[Param::Int(uid), Param::Int(line_id)],
        )
        .await
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
            self.t("vb_plan_line"),
            sets.join(", ")
        );
        self.conn.execute(&sql, &values).await?;
        Ok(())
    }

    async fn open_position_for(&mut self, instrument_id: i64) -> StoreResult<Option<Rec>> {
        let uid = self.user_id;
        self.one_position(
            "WHERE p.user_id = ? AND p.instrument_id = ? AND p.state = 'OPEN' \
             AND p.quantity_open > 0 ORDER BY p.id LIMIT 1",
            &[Param::Int(uid), Param::Int(instrument_id)],
        )
        .await
    }

    async fn position(&mut self, position_id: i64) -> StoreResult<Option<Rec>> {
        let uid = self.user_id;
        self.one_position(
            "WHERE p.user_id = ? AND p.id = ?",
            &[Param::Int(uid), Param::Int(position_id)],
        )
        .await
    }

    async fn create_position(&mut self, fields: Rec) -> StoreResult<i64> {
        let lead = [
            ("user_id", Val::Int(self.user_id)),
            ("broker_account_id", self.broker()),
        ];
        self.insert_returning("vb_position", &lead, &fields).await
    }

    async fn update_position(&mut self, position_id: i64, fields: Rec) -> StoreResult<()> {
        self.update_set("vb_position", position_id, &fields, "")
            .await
    }

    async fn add_fill(&mut self, fields: Rec) -> StoreResult<i64> {
        let lead = [("user_id", Val::Int(self.user_id))];
        self.insert_returning("vb_fill", &lead, &fields).await
    }

    async fn order(&mut self, order_id: i64) -> StoreResult<Option<Rec>> {
        let uid = self.user_id;
        self.one_order(
            "WHERE o.user_id = ? AND o.id = ?",
            &[Param::Int(uid), Param::Int(order_id)],
        )
        .await
    }

    async fn create_order(&mut self, fields: Rec) -> StoreResult<i64> {
        let lead = [
            ("user_id", Val::Int(self.user_id)),
            ("broker_account_id", self.broker()),
        ];
        self.insert_returning("vb_order", &lead, &fields).await
    }

    async fn update_order(&mut self, order_id: i64, fields: Rec) -> StoreResult<()> {
        self.update_set("vb_order", order_id, &fields, "").await
    }

    async fn working_order_for(&mut self, instrument_id: i64) -> StoreResult<Option<Rec>> {
        let uid = self.user_id;
        self.one_order(
            "WHERE o.user_id = ? AND o.instrument_id = ? AND o.state IN \
             ('PROPOSED', 'CONFIRMED', 'SENT', 'PARTIAL') ORDER BY o.id LIMIT 1",
            &[Param::Int(uid), Param::Int(instrument_id)],
        )
        .await
    }

    async fn order_by_broker_id(&mut self, broker_order_id: &str) -> StoreResult<Option<Rec>> {
        let uid = self.user_id;
        self.one_order(
            "WHERE o.user_id = ? AND o.broker_order_id = ? ORDER BY o.id DESC LIMIT 1",
            &[Param::Int(uid), Param::from(broker_order_id)],
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
            let limit = order.val("limit_price");
            order.set("reference_price", limit);
            out.push(order);
        }
        let sql = format!(
            "{EXIT_SELECT}FROM {} WHERE user_id = ? AND sleeve = 'vbt' AND state IN ('SENT', 'PARTIAL') \
             AND broker_order_id IS NOT NULL ORDER BY id",
            self.t("lv_exit_order")
        );
        let rows = self.rows(&sql, &[Param::Int(self.user_id)]).await?;
        for row in &rows {
            out.push(exit_row(row)?);
        }
        Ok(out)
    }

    async fn open_positions(&mut self) -> StoreResult<Vec<Rec>> {
        self.book().await
    }

    async fn open_protection_issues(&mut self) -> StoreResult<Vec<Rec>> {
        let sql = format!(
            "SELECT symbol, kind, detail, position_id FROM {} \
             WHERE user_id = ? AND sleeve = ? AND resolved_at IS NULL ORDER BY seen_at, id",
            self.t("lv_protection_issue")
        );
        let rows = self
            .rows(&sql, &[Param::Int(self.user_id), Param::from("vbt")])
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

    async fn line_by_client_id(&mut self, client_id: &str) -> StoreResult<Option<Rec>> {
        let uid = self.user_id;
        self.one_line(
            "WHERE l.user_id = ? AND l.client_id = ? ORDER BY l.id DESC LIMIT 1",
            &[Param::Int(uid), Param::from(client_id)],
        )
        .await
    }

    async fn bump_session(&mut self, day: NaiveDate, bump: SessionBump) -> StoreResult<()> {
        let sql = format!(
            "INSERT INTO {} (user_id, session_date, mode, confirms, fills, exits) \
             VALUES (?, ?, ?, ?, ?, ?) \
             ON CONFLICT (user_id, session_date) DO UPDATE SET mode = EXCLUDED.mode, \
             confirms = vb_session.confirms + EXCLUDED.confirms, \
             fills = vb_session.fills + EXCLUDED.fills, \
             exits = vb_session.exits + EXCLUDED.exits",
            self.t("vb_session")
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
                    Param::Int(bump.exits),
                ],
            )
            .await?;
        Ok(())
    }

    async fn lock_session_for_update(&mut self, day: NaiveDate) -> StoreResult<()> {
        // NOT EXISTS first: a bare ON CONFLICT waits on a row another confirm's open
        // transaction has updated (R8.TD1/R8.VB1).
        let table = self.t("vb_session");
        let sql = format!(
            "INSERT INTO {table} (user_id, session_date) SELECT ?, ? \
             WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE user_id = ? AND session_date = ?) \
             ON CONFLICT (user_id, session_date) DO NOTHING"
        );
        let params = [Param::Int(self.user_id), Param::Date(day)];
        let insert = [
            Param::Int(self.user_id),
            Param::Date(day),
            Param::Int(self.user_id),
            Param::Date(day),
        ];
        self.conn.execute(&sql, &insert).await?;
        if !self.schema.is_empty() {
            // R8.VB1: the outermost block opens the transaction the locks are held by, and a
            // held lock is refused (409) rather than waited on.
            let opened = self.lock_depth == 0 && !self.lock_opened;
            if opened {
                self.conn.execute("BEGIN", &[]).await?;
            }
            match self.take_locks(&params).await {
                Ok(()) => {
                    if opened {
                        self.lock_opened = true;
                    }
                }
                Err(e) => {
                    if opened {
                        self.conn.execute("ROLLBACK", &[]).await?;
                    }
                    return Err(e);
                }
            }
        } else {
            // `elif _in_transaction(self.conn) is False: BEGIN IMMEDIATE`. sqlite refuses a
            // second BEGIN on a connection that already holds one (a nested block, or a
            // transaction the caller opened), and Python skips the statement in exactly that
            // case — so that one refusal is the skip, nothing else is. Only the block that
            // opened the transaction closes it (R8.VB1).
            match self.conn.execute("BEGIN IMMEDIATE", &[]).await {
                Ok(_) => self.lock_opened = true,
                Err(StorageError::Driver(sqlx::Error::Database(db)))
                    if db.message().contains("within a transaction") => {}
                Err(e) => return Err(e.into()),
            }
        }
        self.lock_depth += 1;
        Ok(())
    }

    async fn unlock_session(&mut self) -> StoreResult<()> {
        if self.lock_depth == 0 {
            return Ok(());
        }
        self.lock_depth -= 1;
        if self.lock_depth == 0 && self.lock_opened {
            self.lock_opened = false;
            self.conn.execute("COMMIT", &[]).await?;
        }
        Ok(())
    }

    async fn order_for_signal(
        &mut self,
        instrument_id: i64,
        signal_date: NaiveDate,
    ) -> StoreResult<Option<i64>> {
        let sql = format!(
            "SELECT id FROM {} WHERE user_id = ? AND signal_date = ? \
             AND instrument_id = ? ORDER BY id LIMIT 1",
            self.t("vb_order")
        );
        let row = self
            .one(
                &sql,
                &[
                    Param::Int(self.user_id),
                    Param::Date(signal_date),
                    Param::Int(instrument_id),
                ],
            )
            .await?;
        Ok(row.map(|r| db_int(col(&r, "id"))))
    }

    async fn entries_taken(&mut self, day: NaiveDate) -> StoreResult<i64> {
        let sql = format!(
            "SELECT created_at FROM {} WHERE user_id = ? \
             AND state IN ('CONFIRMED', 'SENT', 'FILLED', 'PARTIAL')",
            self.t("vb_order")
        );
        let rows = self.rows(&sql, &[Param::Int(self.user_id)]).await?;
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
}
