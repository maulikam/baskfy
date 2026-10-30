//! Ports `baskfy_execution.risk` (the account-level risk manager and kill switch), the desk's
//! `app/core/risk_store.py` (`PgRiskStateStore`, one `risk_ledger` row per IST day, row-locked
//! for every decision — LV3), and `app.config.risk_config` / `risk_coherence` (limits derived
//! from NAV so they cannot contradict the strategy's own sizing).
//!
//! **One account, many processes.** Every decision is lock → reload → decide → save → unlock
//! against the store, so the desk, the swing monitor, `twt-auto` and the session supervisor
//! cannot each read the same headroom and each spend it. Without a store the state is in
//! memory (or the single-process JSON file), as Python's is.
//!
//! The day roll reads the injected clock's IST date (`datetime.now(tz=IST)`), never the host's.

use std::path::PathBuf;
use std::sync::{Arc, Mutex as StdMutex};

use async_trait::async_trait;
use tokio::sync::Mutex;

use crate::clock::Clock;
use crate::pyfmt::{PyArg, format_value};
use crate::storage::{Db, DbValue, Param};
use crate::wire::{PyDict, PyValue, dict, py_json_dumps};

/// `app.config.MAX_SINGLE_WEIGHT` — % of the equity sleeve (a constant, not an env var).
pub const MAX_SINGLE_WEIGHT: f64 = 15.0;
/// `app.config.TARGET_POSITIONS[0]`.
pub const TARGET_POSITIONS_MIN: i64 = 12;

/// A failure of the risk layer itself (Python raises; the gateway reports it).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{python_type}: {message}")]
pub struct RiskError {
    /// The Python exception class (`RuntimeError`, `ValueError`, …).
    pub python_type: String,
    /// `str(exc)`.
    pub message: String,
}

impl RiskError {
    fn new(python_type: &str, message: impl Into<String>) -> Self {
        Self {
            python_type: python_type.to_owned(),
            message: message.into(),
        }
    }
}

impl From<crate::storage::StorageError> for RiskError {
    fn from(e: crate::storage::StorageError) -> Self {
        Self::new("StorageError", e.to_string())
    }
}

/// `RiskConfig`.
#[derive(Debug, Clone, PartialEq)]
pub struct RiskConfig {
    /// ₹ realised + unrealised across engines.
    pub max_daily_loss: f64,
    /// Our own cap, well under Kite's 3000.
    pub max_orders_per_day: i64,
    /// Per symbol, accumulated.
    pub max_position_value: f64,
    /// Gross exposure cap.
    pub max_gross_exposure: f64,
    /// MIS positions force-closed after this.
    pub intraday_square_off: String,
}

impl Default for RiskConfig {
    fn default() -> Self {
        Self {
            max_daily_loss: 100_000.0,
            max_orders_per_day: 500,
            max_position_value: 1_500_000.0,
            max_gross_exposure: 12_000_000.0,
            intraday_square_off: "15:12".to_owned(),
        }
    }
}

/// `RiskState`. `position_value` keeps Python's dict insertion order.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct RiskState {
    /// `day_pnl`.
    pub day_pnl: f64,
    /// `orders_today`.
    pub orders_today: i64,
    /// `killed`.
    pub killed: bool,
    /// `reasons`.
    pub reasons: Vec<String>,
    /// Running per-symbol notional after accepted `pre_order` checks.
    pub position_value: Vec<(String, f64)>,
}

impl RiskState {
    /// `position_value.get(symbol, 0.0)`.
    #[must_use]
    pub fn position(&self, symbol: &str) -> f64 {
        self.position_value
            .iter()
            .find(|(k, _)| k == symbol)
            .map_or(0.0, |(_, v)| *v)
    }

    fn set_position(&mut self, symbol: &str, value: f64) {
        match self.position_value.iter_mut().find(|(k, _)| k == symbol) {
            Some((_, v)) => *v = value,
            None => self.position_value.push((symbol.to_owned(), value)),
        }
    }
}

fn fmt0(x: f64) -> String {
    format_value(&PyArg::Float(x), ",.0f").unwrap_or_else(|_| format!("{x:.0}"))
}

fn as_float(v: &PyValue) -> Option<f64> {
    #[allow(clippy::cast_precision_loss)]
    match v {
        PyValue::Float(f) => Some(*f),
        PyValue::Int(i) => Some(*i as f64),
        PyValue::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        _ => None,
    }
}

fn py_float(v: &PyValue) -> Result<f64, RiskError> {
    match v {
        PyValue::Str(s) => s.trim().parse::<f64>().map_err(|_| {
            RiskError::new(
                "ValueError",
                format!(
                    "could not convert string to float: {}",
                    baskfy_providers::py::repr_str(s)
                ),
            )
        }),
        other => as_float(other).ok_or_else(|| {
            RiskError::new(
                "TypeError",
                "float() argument must be a string or a real number",
            )
        }),
    }
}

fn py_str(v: &PyValue) -> String {
    match v {
        PyValue::Str(s) => s.clone(),
        PyValue::Int(i) => i.to_string(),
        PyValue::Float(f) => baskfy_core::py::float_repr(*f),
        PyValue::Bool(b) => if *b { "True" } else { "False" }.to_owned(),
        PyValue::None => "None".to_owned(),
        other => format!("{other:?}"),
    }
}

/// The trading day's calendar date in IST, from the clock.
#[must_use]
pub fn ist_day(clock: &dyn Clock) -> String {
    clock.now_ist().format("%Y-%m-%d").to_string()
}

/// `_state_from(raw)`: the payload's day and state, tolerant of an older writer.
///
/// # Errors
/// A `position_value` entry `float()` refuses.
pub fn state_from(raw: &PyDict, today: &str) -> Result<(String, RiskState), RiskError> {
    let day = match raw.get("day") {
        Some(v) if v.truthy() => py_str(v),
        _ => today.to_owned(),
    };
    let mut position_value = Vec::new();
    if let Some(PyValue::Map(m)) = raw.get("position_value") {
        for (k, v) in m.iter() {
            position_value.push((k.to_owned(), py_float(v)?));
        }
    }
    let reasons = match raw.get("reasons") {
        Some(PyValue::List(items)) => items.iter().map(py_str).collect(),
        _ => Vec::new(),
    };
    let day_pnl = raw.get("day_pnl").and_then(as_float).unwrap_or(0.0);
    #[allow(clippy::cast_possible_truncation)]
    let orders_today = match raw.get("orders_today") {
        Some(PyValue::Int(i)) => *i,
        Some(v) => as_float(v).map_or(0, |f| f.trunc() as i64),
        None => 0,
    };
    let killed = raw.get("killed").is_some_and(PyValue::truthy);
    Ok((
        day,
        RiskState {
            day_pnl,
            orders_today,
            killed,
            reasons,
            position_value,
        },
    ))
}

/// `_payload()`: the JSON the store and the file both hold.
#[must_use]
pub fn payload(day: &str, s: &RiskState) -> PyDict {
    dict([
        ("day", PyValue::Str(day.to_owned())),
        ("day_pnl", PyValue::Float(s.day_pnl)),
        ("orders_today", PyValue::Int(s.orders_today)),
        ("killed", PyValue::Bool(s.killed)),
        (
            "reasons",
            PyValue::List(s.reasons.iter().cloned().map(PyValue::Str).collect()),
        ),
        (
            "position_value",
            PyValue::Map(dict(
                s.position_value
                    .iter()
                    .map(|(k, v)| (k.clone(), PyValue::Float(*v))),
            )),
        ),
    ])
}

/// One held lock on the shared row: `load`/`save` run inside it, then `commit` (or
/// `rollback` on a failure).
#[async_trait]
pub trait RiskTxn: Send {
    /// `store.load()`.
    async fn load(&mut self) -> Result<Option<PyDict>, RiskError>;
    /// `store.save(payload)`.
    async fn save(&mut self, payload: &PyDict) -> Result<(), RiskError>;
    /// Leave the `with store.lock()` block normally.
    async fn commit(self: Box<Self>) -> Result<(), RiskError>;
    /// Leave it on an exception.
    async fn rollback(self: Box<Self>);
}

/// `RiskStateStore`: durable, shared risk state.
#[async_trait]
pub trait RiskStateStore: Send + Sync {
    /// `store.lock()`.
    async fn lock(&self) -> Result<Box<dyn RiskTxn>, RiskError>;
    /// `store.save(payload)` called OUTSIDE `lock()` — which Python's `_roll` does from
    /// `refresh()`. `PgRiskStateStore` refuses it; an in-memory store accepts it.
    async fn save_unlocked(&self, payload: &PyDict) -> Result<(), RiskError>;
}

/// The tests' shared row: a dict behind a lock, counting how often it was held and saved
/// (`tests/test_risk_store.py`'s `MemoryStore`).
#[derive(Debug, Default)]
pub struct MemoryStore {
    row: StdMutex<Option<PyDict>>,
    gate: Arc<Mutex<()>>,
    counts: StdMutex<(u64, u64)>,
}

impl MemoryStore {
    /// The stored row.
    #[must_use]
    pub fn row(&self) -> Option<PyDict> {
        self.row.lock().map(|r| r.clone()).unwrap_or_default()
    }

    /// `(locks, saves)`.
    #[must_use]
    pub fn counts(&self) -> (u64, u64) {
        self.counts.lock().map(|c| *c).unwrap_or_default()
    }

    fn put(&self, payload: &PyDict) {
        if let Ok(mut r) = self.row.lock() {
            *r = Some(payload.clone());
        }
        if let Ok(mut c) = self.counts.lock() {
            c.1 += 1;
        }
    }
}

struct MemoryTxn {
    store: Arc<MemoryStore>,
    _guard: tokio::sync::OwnedMutexGuard<()>,
}

#[async_trait]
impl RiskTxn for MemoryTxn {
    async fn load(&mut self) -> Result<Option<PyDict>, RiskError> {
        Ok(self.store.row())
    }
    async fn save(&mut self, payload: &PyDict) -> Result<(), RiskError> {
        self.store.put(payload);
        Ok(())
    }
    async fn commit(self: Box<Self>) -> Result<(), RiskError> {
        Ok(())
    }
    async fn rollback(self: Box<Self>) {}
}

/// A [`MemoryStore`] shared by several managers.
#[derive(Debug, Clone, Default)]
pub struct SharedMemoryStore(pub Arc<MemoryStore>);

#[async_trait]
impl RiskStateStore for SharedMemoryStore {
    async fn lock(&self) -> Result<Box<dyn RiskTxn>, RiskError> {
        let guard = self.0.gate.clone().lock_owned().await;
        if let Ok(mut c) = self.0.counts.lock() {
            c.0 += 1;
        }
        Ok(Box::new(MemoryTxn {
            store: self.0.clone(),
            _guard: guard,
        }))
    }
    async fn save_unlocked(&self, payload: &PyDict) -> Result<(), RiskError> {
        self.0.put(payload);
        Ok(())
    }
}

/// `PgRiskStateStore`: `risk_ledger` through the desk's SQLite-shaped connection. `lock()`
/// opens a transaction and takes today's row `FOR UPDATE` (inserting it first if the day has
/// none); on SQLite `BEGIN IMMEDIATE` already takes the write lock.
#[derive(Clone)]
pub struct PgRiskStateStore {
    db: Db,
    /// `user_id`.
    pub user_id: i64,
    /// `schema` (`"public"` on the desk; `""` on a bare SQLite file).
    pub schema: String,
    today: Arc<dyn Fn() -> chrono::NaiveDate + Send + Sync>,
}

impl std::fmt::Debug for PgRiskStateStore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PgRiskStateStore")
            .field("user_id", &self.user_id)
            .field("schema", &self.schema)
            .finish_non_exhaustive()
    }
}

impl PgRiskStateStore {
    /// The store over `db`, keyed by `user_id`, the day from `today`.
    #[must_use]
    pub fn new(
        db: Db,
        user_id: i64,
        schema: &str,
        today: Arc<dyn Fn() -> chrono::NaiveDate + Send + Sync>,
    ) -> Self {
        Self {
            db,
            user_id,
            schema: schema.to_owned(),
            today,
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
}

struct PgTxn {
    conn: crate::storage::Conn,
    table: String,
    user_id: i64,
    day: chrono::NaiveDate,
}

fn dumps(p: &PyDict) -> Result<String, RiskError> {
    py_json_dumps(&PyValue::Map(p.clone())).map_err(|e| RiskError::new("ValueError", e.to_string()))
}

#[async_trait]
impl RiskTxn for PgTxn {
    async fn load(&mut self) -> Result<Option<PyDict>, RiskError> {
        let cur = self
            .conn
            .execute(
                &format!(
                    "SELECT payload FROM {} WHERE user_id = ? AND day = ?",
                    self.table
                ),
                &[Param::Int(self.user_id), Param::Date(self.day)],
            )
            .await?;
        let Some(row) = cur.fetchone() else {
            return Ok(None);
        };
        let text = match row.get("payload") {
            Some(DbValue::Text(s) | DbValue::Json(s)) => s.clone(),
            Some(DbValue::Bytes(b)) => String::from_utf8_lossy(b).into_owned(),
            other => {
                return Err(RiskError::new(
                    "TypeError",
                    format!("risk_ledger payload is not JSON: {other:?}"),
                ));
            }
        };
        let parsed = PyValue::from_json(text.as_bytes())
            .map_err(|e| RiskError::new("JSONDecodeError", e))?;
        let PyValue::Map(m) = parsed else {
            return Err(RiskError::new(
                "TypeError",
                "risk_ledger payload is not an object",
            ));
        };
        // A row lock() inserted for a day nobody has decided on yet carries only the day.
        Ok(if m.len() > 1 { Some(m) } else { None })
    }

    async fn save(&mut self, payload: &PyDict) -> Result<(), RiskError> {
        self.conn
            .execute(
                &format!(
                    "INSERT INTO {} (user_id, day, payload, updated_at) VALUES (?, ?, ?, \
                     CURRENT_TIMESTAMP) ON CONFLICT (user_id, day) DO UPDATE SET payload = \
                     EXCLUDED.payload, updated_at = CURRENT_TIMESTAMP",
                    self.table
                ),
                &[
                    Param::Int(self.user_id),
                    Param::Date(self.day),
                    Param::Text(dumps(payload)?),
                ],
            )
            .await?;
        Ok(())
    }

    async fn commit(mut self: Box<Self>) -> Result<(), RiskError> {
        self.conn.execute("COMMIT", &[]).await?;
        Ok(())
    }

    async fn rollback(mut self: Box<Self>) {
        let _ = self.conn.execute("ROLLBACK", &[]).await;
    }
}

#[async_trait]
impl RiskStateStore for PgRiskStateStore {
    async fn lock(&self) -> Result<Box<dyn RiskTxn>, RiskError> {
        let mut conn = self.db.connect().await?;
        let postgres = conn.is_postgres();
        conn.execute(if postgres { "BEGIN" } else { "BEGIN IMMEDIATE" }, &[])
            .await?;
        let day = (self.today)();
        let table = self.t("risk_ledger");
        let opened = async {
            let first = dict([("day", PyValue::Str(day.format("%Y-%m-%d").to_string()))]);
            conn.execute(
                &format!(
                    "INSERT INTO {table} (user_id, day, payload) VALUES (?, ?, ?) ON CONFLICT \
                     (user_id, day) DO NOTHING"
                ),
                &[
                    Param::Int(self.user_id),
                    Param::Date(day),
                    Param::Text(dumps(&first)?),
                ],
            )
            .await?;
            conn.execute(
                &format!(
                    "SELECT payload FROM {table} WHERE user_id = ? AND day = ?{}",
                    if postgres { " FOR UPDATE" } else { "" }
                ),
                &[Param::Int(self.user_id), Param::Date(day)],
            )
            .await?;
            Ok::<(), RiskError>(())
        }
        .await;
        if let Err(e) = opened {
            let _ = conn.execute("ROLLBACK", &[]).await;
            return Err(e);
        }
        Ok(Box::new(PgTxn {
            conn,
            table,
            user_id: self.user_id,
            day,
        }))
    }

    async fn save_unlocked(&self, _payload: &PyDict) -> Result<(), RiskError> {
        Err(RiskError::new(
            "RuntimeError",
            "risk_ledger is read and written only under lock()",
        ))
    }
}

/// Where a [`RiskManager`]'s state lives.
#[derive(Clone)]
pub enum RiskBacking {
    /// In memory, for this process only.
    Memory,
    /// The single-process JSON file (`state_path=`).
    File(PathBuf),
    /// The shared row (`store=`).
    Store(Arc<dyn RiskStateStore>),
}

struct Inner {
    day: String,
    state: RiskState,
}

/// `RiskManager`: the account's caps and kill switch. Every method takes the store's lock
/// and reloads first.
pub struct RiskManager {
    cfg: RiskConfig,
    clock: Arc<dyn Clock>,
    backing: RiskBacking,
    inner: Mutex<Inner>,
}

impl std::fmt::Debug for RiskManager {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RiskManager")
            .field("cfg", &self.cfg)
            .finish_non_exhaustive()
    }
}

/// What the gateway consults: `pre_order` (layer 2) and the kill switch read for stops.
#[async_trait]
pub trait RiskLayer: Send + Sync {
    /// `risk.pre_order(symbol, value, gross, side=)`.
    async fn pre_order(
        &self,
        symbol: &str,
        value: f64,
        gross: f64,
        side: &str,
    ) -> Result<(bool, String), RiskError>;

    /// `gtt.kill_switch_reason(risk)`: refresh, then the kill switch's words or `""`.
    async fn kill_switch_reason(&self) -> Result<String, RiskError>;
}

async fn write_file(path: &PathBuf, p: &PyDict) -> Result<(), RiskError> {
    let io = |e: std::io::Error| RiskError::new("OSError", e.to_string());
    if let Some(parent) = path.parent()
        && !parent.as_os_str().is_empty()
    {
        std::fs::create_dir_all(parent).map_err(io)?;
    }
    let tmp = PathBuf::from(format!("{}.tmp", path.display()));
    std::fs::write(&tmp, dumps(p)?).map_err(io)?;
    std::fs::rename(&tmp, path).map_err(io)?;
    Ok(())
}

impl RiskManager {
    /// `RiskManager(cfg, state_path=..., store=...)`. With a store, the row is locked and
    /// reloaded once now (Python's constructor does the same, so the day's row exists from
    /// here on); with a file, a stale day starts fresh and is written back.
    ///
    /// # Errors
    /// The store's, or a file that is not a JSON object.
    pub async fn new(
        cfg: RiskConfig,
        clock: Arc<dyn Clock>,
        backing: RiskBacking,
    ) -> Result<Self, RiskError> {
        let today = ist_day(clock.as_ref());
        let manager = Self {
            cfg,
            clock,
            backing,
            inner: Mutex::new(Inner {
                day: today,
                state: RiskState::default(),
            }),
        };
        match &manager.backing {
            RiskBacking::Store(store) => {
                let mut inner = manager.inner.lock().await;
                let mut txn = store.lock().await?;
                let loaded = manager.reload(txn.as_mut(), &mut inner).await;
                match loaded {
                    Ok(()) => txn.commit().await?,
                    Err(e) => {
                        txn.rollback().await;
                        return Err(e);
                    }
                }
            }
            RiskBacking::File(path) => manager.load_file(path).await?,
            RiskBacking::Memory => {}
        }
        Ok(manager)
    }

    /// The configuration.
    #[must_use]
    pub const fn cfg(&self) -> &RiskConfig {
        &self.cfg
    }

    /// A copy of the state as this process last saw it.
    pub async fn state(&self) -> RiskState {
        self.inner.lock().await.state.clone()
    }

    async fn reload(&self, txn: &mut dyn RiskTxn, inner: &mut Inner) -> Result<(), RiskError> {
        let Some(raw) = txn.load().await? else {
            return Ok(());
        };
        let (day, state) = state_from(&raw, &ist_day(self.clock.as_ref()))?;
        inner.day = day;
        inner.state = state;
        Ok(())
    }

    async fn load_file(&self, path: &PathBuf) -> Result<(), RiskError> {
        if !path.is_file() {
            return Ok(());
        }
        let text =
            std::fs::read_to_string(path).map_err(|e| RiskError::new("OSError", e.to_string()))?;
        let raw = PyValue::from_json(text.as_bytes())
            .map_err(|e| RiskError::new("JSONDecodeError", e))?;
        let PyValue::Map(raw) = raw else {
            return Err(RiskError::new(
                "ValueError",
                format!("risk state at {} is not an object", path.display()),
            ));
        };
        let today = ist_day(self.clock.as_ref());
        let mut inner = self.inner.lock().await;
        if raw.get("day").map(py_str).as_deref() != Some(today.as_str()) {
            inner.day = today;
            inner.state = RiskState::default();
            return write_file(path, &payload(&inner.day, &inner.state)).await;
        }
        if let Some(pv) = raw.get("position_value")
            && pv.truthy()
            && !matches!(pv, PyValue::Map(_))
        {
            return Err(RiskError::new(
                "ValueError",
                format!(
                    "risk state position_value at {} is not an object",
                    path.display()
                ),
            ));
        }
        let (_, state) = state_from(&raw, &today)?;
        inner.day = today;
        inner.state = state;
        Ok(())
    }

    /// Persist inside a held lock (`txn`), or to the file, or nowhere.
    async fn persist(&self, txn: Option<&mut dyn RiskTxn>, inner: &Inner) -> Result<(), RiskError> {
        let p = payload(&inner.day, &inner.state);
        match (&self.backing, txn) {
            (RiskBacking::Store(_), Some(t)) => t.save(&p).await,
            (RiskBacking::Store(store), None) => store.save_unlocked(&p).await,
            (RiskBacking::File(path), _) => write_file(path, &p).await,
            (RiskBacking::Memory, _) => Ok(()),
        }
    }

    async fn roll(
        &self,
        txn: Option<&mut dyn RiskTxn>,
        inner: &mut Inner,
    ) -> Result<(), RiskError> {
        let d = ist_day(self.clock.as_ref());
        if d != inner.day {
            inner.day = d;
            inner.state = RiskState::default();
            self.persist(txn, inner).await?;
        }
        Ok(())
    }

    /// Run `body` under the store's lock with a fresh reload (`_locked`), or directly.
    async fn locked<T, F>(&self, body: F) -> Result<T, RiskError>
    where
        T: Send,
        F: for<'a> FnOnce(
                &'a Self,
                Option<&'a mut dyn RiskTxn>,
                &'a mut Inner,
            ) -> std::pin::Pin<
                Box<dyn std::future::Future<Output = Result<T, RiskError>> + Send + 'a>,
            > + Send,
    {
        let mut inner = self.inner.lock().await;
        if let RiskBacking::Store(store) = &self.backing {
            let mut txn = store.lock().await?;
            let outcome = match self.reload(txn.as_mut(), &mut inner).await {
                Ok(()) => body(self, Some(txn.as_mut()), &mut inner).await,
                Err(e) => Err(e),
            };
            return match outcome {
                Ok(v) => {
                    txn.commit().await?;
                    Ok(v)
                }
                Err(e) => {
                    txn.rollback().await;
                    Err(e)
                }
            };
        }
        body(self, None, &mut inner).await
    }

    /// `refresh()`: re-read the shared row without deciding (a kill from another process),
    /// then roll the day — outside the lock, as Python does.
    ///
    /// # Errors
    /// The store's.
    pub async fn refresh(&self) -> Result<(), RiskError> {
        let RiskBacking::Store(store) = &self.backing else {
            return Ok(());
        };
        let mut inner = self.inner.lock().await;
        let mut txn = store.lock().await?;
        match self.reload(txn.as_mut(), &mut inner).await {
            Ok(()) => txn.commit().await?,
            Err(e) => {
                txn.rollback().await;
                return Err(e);
            }
        }
        self.roll(None, &mut inner).await
    }

    /// `kill(reason)`.
    ///
    /// # Errors
    /// The store's.
    pub async fn kill(&self, reason: &str) -> Result<(), RiskError> {
        let reason = reason.to_owned();
        self.locked(move |me, txn, inner| {
            Box::pin(async move {
                inner.state.killed = true;
                inner.state.reasons.push(reason);
                me.persist(txn, inner).await
            })
        })
        .await
    }

    /// `on_pnl(pnl)`: the day's P&L; at or below `-max_daily_loss` the kill switch trips.
    ///
    /// # Errors
    /// The store's.
    pub async fn on_pnl(&self, pnl: f64) -> Result<(), RiskError> {
        self.locked(move |me, mut txn, inner| {
            Box::pin(async move {
                me.roll(reborrow(&mut txn), inner).await?;
                inner.state.day_pnl = pnl;
                if pnl <= -me.cfg.max_daily_loss.abs() {
                    inner.state.killed = true;
                    inner
                        .state
                        .reasons
                        .push(format!("daily loss cap hit ({})", fmt0(pnl)));
                }
                me.persist(txn, inner).await
            })
        })
        .await
    }

    /// `release(symbol, value)`: give back a reservation for shares that never filled.
    ///
    /// # Errors
    /// The store's.
    pub async fn release(&self, symbol: &str, value: f64) -> Result<(), RiskError> {
        let symbol = symbol.to_owned();
        self.locked(move |me, mut txn, inner| {
            Box::pin(async move {
                me.roll(reborrow(&mut txn), inner).await?;
                let current = inner.state.position(&symbol);
                inner
                    .state
                    .set_position(&symbol, (current - value.abs()).max(0.0));
                me.persist(txn, inner).await
            })
        })
        .await
    }

    /// `seed_positions({symbol: notional})`: raise (never lower) the per-symbol map to what
    /// the account already holds.
    ///
    /// # Errors
    /// The store's.
    pub async fn seed_positions(&self, values: &[(String, f64)]) -> Result<(), RiskError> {
        let values = values.to_vec();
        self.locked(move |me, mut txn, inner| {
            Box::pin(async move {
                me.roll(reborrow(&mut txn), inner).await?;
                for (symbol, notional) in values {
                    if notional <= 0.0 {
                        continue;
                    }
                    let current = inner.state.position(&symbol);
                    inner.state.set_position(&symbol, current.max(notional));
                }
                me.persist(txn, inner).await
            })
        })
        .await
    }

    fn decide(
        &self,
        inner: &mut Inner,
        symbol: &str,
        value: f64,
        gross: f64,
        side: &str,
    ) -> (bool, String) {
        let (s, c) = (&mut inner.state, &self.cfg);
        if s.killed {
            return (false, format!("KILL SWITCH: {}", s.reasons.join("; ")));
        }
        if s.orders_today >= c.max_orders_per_day {
            return (false, "own daily order cap reached".to_owned());
        }
        let current = s.position(symbol);
        let side_u = if side.is_empty() {
            "BUY".to_owned()
        } else {
            side.to_uppercase()
        };
        let projected = if side_u == "BUY" {
            current + value.abs()
        } else {
            value.abs()
        };
        if projected > c.max_position_value {
            return (
                false,
                format!("{symbol} position value {} > cap", fmt0(projected)),
            );
        }
        if gross > c.max_gross_exposure {
            return (false, "gross exposure cap".to_owned());
        }
        s.orders_today += 1;
        if side_u == "BUY" {
            s.set_position(symbol, current + value.abs());
        } else {
            s.set_position(symbol, (current - value.abs()).max(0.0));
        }
        (true, "ok".to_owned())
    }

    /// `pre_order(symbol, value, gross, side=)`.
    ///
    /// # Errors
    /// The store's.
    pub async fn pre_order(
        &self,
        symbol: &str,
        value: f64,
        gross: f64,
        side: &str,
    ) -> Result<(bool, String), RiskError> {
        let (symbol, side) = (symbol.to_owned(), side.to_owned());
        self.locked(move |me, mut txn, inner| {
            Box::pin(async move {
                me.roll(reborrow(&mut txn), inner).await?;
                let verdict = me.decide(inner, &symbol, value, gross, &side);
                if verdict.0 {
                    me.persist(txn, inner).await?;
                }
                Ok(verdict)
            })
        })
        .await
    }
}

#[async_trait]
impl RiskLayer for RiskManager {
    async fn pre_order(
        &self,
        symbol: &str,
        value: f64,
        gross: f64,
        side: &str,
    ) -> Result<(bool, String), RiskError> {
        Self::pre_order(self, symbol, value, gross, side).await
    }

    async fn kill_switch_reason(&self) -> Result<String, RiskError> {
        self.refresh().await?;
        let inner = self.inner.lock().await;
        if !inner.state.killed {
            return Ok(String::new());
        }
        Ok(format!("KILL SWITCH: {}", inner.state.reasons.join("; ")))
    }
}

/// `app.config.risk_config(nav)`: limits derived from NAV unless overridden in the settings.
///
/// # Errors
/// An override that `float()` refuses.
pub fn risk_config(nav: f64, s: &crate::settings::Settings) -> Result<RiskConfig, RiskError> {
    // `base = float(nav) if nav else 0.0`; every branch below asks `base > 0`.
    let base = nav;
    let parse = |raw: &str| {
        raw.trim().parse::<f64>().map_err(|_| {
            RiskError::new(
                "ValueError",
                format!(
                    "could not convert string to float: {}",
                    baskfy_providers::py::repr_str(raw)
                ),
            )
        })
    };
    let pos_cap = if !s.risk_max_position_value.is_empty() {
        parse(&s.risk_max_position_value)?
    } else if base > 0.0 {
        base * MAX_SINGLE_WEIGHT / 100.0 * s.risk_position_headroom
    } else {
        1_500_000.0
    };
    let gross_cap = if !s.risk_max_gross_exposure.is_empty() {
        parse(&s.risk_max_gross_exposure)?
    } else if base > 0.0 {
        base * s.risk_gross_multiple
    } else {
        12_000_000.0
    };
    let daily_loss = if base > 0.0 {
        base * s.risk_max_daily_loss_pct / 100.0
    } else {
        100_000.0
    };
    Ok(RiskConfig {
        max_daily_loss: super::gtt::py_round(daily_loss, 2),
        max_orders_per_day: s.risk_max_orders_per_day,
        max_position_value: super::gtt::py_round(pos_cap, 2),
        max_gross_exposure: super::gtt::py_round(gross_cap, 2),
        intraday_square_off: "15:12".to_owned(),
    })
}

/// `app.config.risk_coherence(cfg, nav)`: contradictions between the limits and the
/// strategy's own sizing. Anything reported would block a legal action.
#[must_use]
pub fn risk_coherence(cfg: &RiskConfig, nav: f64) -> Vec<String> {
    let mut problems = Vec::new();
    if nav > 0.0 {
        let legal_max = nav * MAX_SINGLE_WEIGHT / 100.0;
        if cfg.max_position_value < legal_max {
            problems.push(format!(
                "max_position_value Rs {} is below the largest position the strategy permits \
                 ({}% of NAV = Rs {}) — it would block a legal position",
                fmt0(cfg.max_position_value),
                baskfy_core::py::float_repr(MAX_SINGLE_WEIGHT),
                fmt0(legal_max)
            ));
        }
        if cfg.max_gross_exposure < nav {
            problems.push(format!(
                "max_gross_exposure Rs {} is below current NAV Rs {} — every order would be \
                 refused",
                fmt0(cfg.max_gross_exposure),
                fmt0(nav)
            ));
        }
        if cfg.max_orders_per_day < TARGET_POSITIONS_MIN * 2 {
            problems.push(format!(
                "max_orders_per_day {} cannot cover a {TARGET_POSITIONS_MIN}-position rebalance",
                cfg.max_orders_per_day
            ));
        }
    }
    problems
}

/// Reborrow a held lock for one call without tying it to the closure's whole lifetime (a
/// trait object behind `&mut` only shortens its lifetime through an explicit coercion).
fn reborrow<'s>(txn: &'s mut Option<&mut dyn RiskTxn>) -> Option<&'s mut dyn RiskTxn> {
    match txn {
        Some(t) => Some(&mut **t),
        None => None,
    }
}
