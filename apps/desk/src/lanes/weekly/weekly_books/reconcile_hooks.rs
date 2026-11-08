//! `app/reconcile.py`'s second half: the three sleeves' hooks (`twt_hooks`, `vbt_hooks`,
//! `swing_hooks`) and `run_pass_from_desk`, which the session supervisor runs every tick.
//!
//! Each hook is the sleeve's **own** idempotent `on_order_update` over its own store and its own
//! gateway — the code the sleeve lanes ported — so every broker mutation a pass causes (a GTT
//! re-sized after a partial fill, a stop armed for a naked position, a GTT cancelled after an
//! exit) goes through that gateway: guards → risk → rate limit → journal (law 2). This module
//! holds no Kite writer; its order book is the desk's `Kite` wrapper's three reads.
//!
//! **Payload → report.** Python hands the handler Kite's dict; the Rust handlers take typed
//! reports, so the conversion happens here, raising exactly where Python's handler would
//! (`int(filled_quantity or 0)`, `Decimal(str(average_price))`) and caught by the pass as that
//! handler's failure. Swing's raise sits after its line lookup (`_apply_update`), so a malformed
//! report for an order that is not a `SENT` swing buy is ignored, as in Python.

use std::sync::Arc;

use chrono::{DateTime, FixedOffset, NaiveDate};
use rust_decimal::Decimal;
use tokio::sync::Mutex;

use super::pyconv::{self, PyDecimal};
use super::reconcile::{
    HookError, OrderBook, PgIssueStore, ReconcileRun, SleeveHooks, reconcile_once,
};
use crate::app::AppState;
use crate::clock::ist;
use crate::gateway::{Gateway, GatewayError};
use crate::lanes::exec_core::desk::DeskGateway;
use crate::lanes::exec_core::kite_client::{ClientError, DeskKite};
use crate::lanes::exec_core::risk::RiskManager;
use crate::lanes::session::supervisor::{DepError, PassOutcome, ReconcilePass};
use crate::lanes::swing::store::{LineRow, PositionRow, SwingStore};
use crate::lanes::swing::swing_exec::execute as swing_x;
use crate::lanes::twt::common::{Rec, Val, dstr};
use crate::lanes::twt::execute as twt_x;
use crate::lanes::twt::store::TwtStore;
use crate::lanes::vbt::execute as vbt_x;
use crate::lanes::vbt::store::VbtStore;
use crate::settings::{Settings, SharedSettings};
use crate::wire::{PyDict, PyValue};

/// `now=`: the instant a hook hands its handler (one for the whole pass from the desk).
pub type NowFn = Arc<dyn Fn() -> DateTime<FixedOffset> + Send + Sync>;

/// `SleeveHooks.risk`: what the pass gives a dead order's remainder back to.
#[async_trait::async_trait]
pub trait RiskRelease: Send + Sync {
    /// `risk.release(symbol, value)`.
    async fn release(&self, symbol: &str, value: f64) -> Result<(), HookError>;
}

#[async_trait::async_trait]
impl RiskRelease for RiskManager {
    async fn release(&self, symbol: &str, value: f64) -> Result<(), HookError> {
        Self::release(self, symbol, value)
            .await
            .map_err(|e| format!("{}: {}", e.python_type, e.message))
    }
}

fn gateway_text(e: &GatewayError) -> HookError {
    format!("{}: {}", e.python_type, e.message)
}

fn client_text(e: &ClientError) -> HookError {
    format!("{}: {}", e.python_type, e.message)
}

fn snapshot(settings: &SharedSettings) -> Settings {
    match settings.read() {
        Ok(s) => s.clone(),
        Err(p) => p.into_inner().clone(),
    }
}

/// A store value as the reconciler reads it (`Decimal` as its `str`, which `Decimal(str(x))`
/// reads back unchanged).
fn val_py(v: &Val) -> PyValue {
    match v {
        Val::None => PyValue::None,
        Val::Bool(b) => PyValue::Bool(*b),
        Val::Int(i) => PyValue::Int(*i),
        Val::Float(f) => PyValue::Float(*f),
        Val::Date(d) => PyValue::Date(*d),
        Val::Py(p) => p.clone(),
        Val::Dec(_) | Val::Str(_) | Val::Stamp(_) | Val::Naive(_) | Val::List(_) | Val::Map(_) => {
            PyValue::Str(v.py_str())
        }
    }
}

/// A store row as a dict.
#[must_use]
pub fn rec_py(rec: &Rec) -> PyDict {
    let mut out = PyDict::new();
    for (k, v) in &rec.0 {
        out.insert(k.clone(), val_py(v));
    }
    out
}

/// `str(payload.get(key) or "")`.
fn text_or_empty(payload: &PyDict, key: &str) -> String {
    match payload.get(key) {
        Some(v) if v.truthy() => v.py_str(),
        _ => String::new(),
    }
}

/// `Decimal(str(x))` where the handler needs a finite price.
fn finite(v: &PyValue) -> Result<Decimal, HookError> {
    match pyconv::decimal(v)? {
        PyDecimal::Finite(d) => Ok(d),
        // Valid Decimals to Python whose later comparisons raise or mislead; Kite's JSON never
        // carries one (R8.WB1).
        PyDecimal::NaN | PyDecimal::SNaN | PyDecimal::Inf(_) => Err(format!(
            "InvalidOperation: [<class 'decimal.InvalidOperation'>] (average_price is {})",
            v.py_str()
        )),
    }
}

/// TWT's report, read as `twt_execute.on_order_update` reads its payload. `Ok(None)` when the
/// handler returns before reading anything (no order id).
///
/// # Errors
/// `int(filled_quantity)`'s or `Decimal(average_price)`'s raise.
pub fn twt_report(payload: &PyDict) -> Result<Option<twt_x::OrderReport>, HookError> {
    let order_id = text_or_empty(payload, "order_id");
    if order_id.is_empty() {
        return Ok(None);
    }
    let status = text_or_empty(payload, "status").to_uppercase();
    let filled = pyconv::int_or_zero(payload.get("filled_quantity"))?;
    let average_price = match payload.get("average_price") {
        None | Some(PyValue::None) => None,
        Some(v) => Some(finite(v)?),
    };
    let message = [
        payload.get("status_message"),
        payload.get("status_message_raw"),
    ]
    .into_iter()
    .flatten()
    .find(|v| v.truthy())
    .map(PyValue::py_str);
    Ok(Some(twt_x::OrderReport {
        order_id,
        status,
        filled_quantity: filled,
        average_price,
        status_message: message,
    }))
}

/// VBT reads the payload dict itself; this raises first where Python's handler would raise
/// (`int(filled_quantity or 0)`, `Decimal(str(average_price))`, after the order-id check).
///
/// # Errors
/// That raise.
pub fn vbt_precheck(payload: &PyDict) -> Result<(), HookError> {
    if text_or_empty(payload, "order_id").is_empty() {
        return Ok(());
    }
    pyconv::int_or_zero(payload.get("filled_quantity"))?;
    if let Some(v) = payload
        .get("average_price")
        .filter(|v| !matches!(v, PyValue::None))
    {
        finite(v)?;
    }
    Ok(())
}

/// Swing's update, as `swing_execute._apply_update` reads it.
///
/// # Errors
/// `int(filled_quantity or 0)`'s or `Decimal(str(average_price or 0))`'s raise.
pub fn swing_update(payload: &PyDict) -> Result<swing_x::OrderUpdate, HookError> {
    let zero = PyValue::Int(0);
    let average = match payload.get("average_price") {
        Some(v) if v.truthy() => v,
        _ => &zero,
    };
    Ok(swing_x::OrderUpdate {
        order_id: text_or_empty(payload, "order_id"),
        status: text_or_empty(payload, "status"),
        filled_quantity: pyconv::int_or_zero(payload.get("filled_quantity"))?,
        average_price: finite(average)?,
    })
}

fn outcome_value(status: &str) -> PyValue {
    PyValue::Str(status.to_owned())
}

/// The fields every hook shares.
struct Common {
    gateway: Arc<dyn Gateway>,
    settings: SharedSettings,
    now: NowFn,
}

// =============================================================================================
// TWT
// =============================================================================================

/// `twt_hooks(store, gateway, now=)`.
pub struct TwtHooks<S> {
    store: Mutex<S>,
    common: Common,
    /// `risk` (`getattr(gateway, "risk", None)`; a test may set it).
    pub risk: Option<Arc<dyn RiskRelease>>,
}

impl<S> TwtHooks<S> {
    /// The store, for a caller that reads it back.
    pub const fn store(&self) -> &Mutex<S> {
        &self.store
    }
}

/// `twt_hooks(store, gateway, now=)`.
pub fn twt_hooks<S: TwtStore>(
    store: S,
    gateway: Arc<dyn Gateway>,
    settings: SharedSettings,
    now: NowFn,
    risk: Option<Arc<dyn RiskRelease>>,
) -> TwtHooks<S> {
    TwtHooks {
        store: Mutex::new(store),
        common: Common {
            gateway,
            settings,
            now,
        },
        risk,
    }
}

fn twt_exec_text(e: &twt_x::ExecError) -> HookError {
    match e {
        twt_x::ExecError::Gateway(g) => gateway_text(g),
        twt_x::ExecError::Http(h) => format!("HTTPException: {}: {}", h.status, h.detail),
        twt_x::ExecError::Store(s) => s.to_string(),
    }
}

#[async_trait::async_trait]
impl<S: TwtStore + Sync> SleeveHooks for TwtHooks<S> {
    fn name(&self) -> &str {
        "twt"
    }

    async fn open_orders(&self) -> Result<Vec<PyDict>, HookError> {
        let rows = self
            .store
            .lock()
            .await
            .open_orders()
            .await
            .map_err(|e| e.to_string())?;
        Ok(rows.iter().map(rec_py).collect())
    }

    async fn on_order_update(&self, payload: &PyDict) -> Result<Option<PyValue>, HookError> {
        let Some(report) = twt_report(payload)? else {
            return Ok(None);
        };
        let settings = snapshot(&self.common.settings);
        let mut store = self.store.lock().await;
        let outcome = twt_x::on_order_update(
            &mut *store,
            self.common.gateway.as_ref(),
            &settings,
            &report,
            (self.common.now)(),
        )
        .await
        .map_err(|e| twt_exec_text(&e))?;
        Ok(outcome.map(|o| outcome_value(&o.status)))
    }

    async fn open_positions(&self) -> Result<Vec<PyDict>, HookError> {
        let rows = self
            .store
            .lock()
            .await
            .open_positions()
            .await
            .map_err(|e| e.to_string())?;
        Ok(rows.iter().map(rec_py).collect())
    }

    fn has_risk(&self) -> bool {
        self.risk.is_some()
    }

    async fn release(&self, symbol: &str, value: f64) -> Result<(), HookError> {
        match &self.risk {
            Some(r) => r.release(symbol, value).await,
            None => Ok(()),
        }
    }
}

// =============================================================================================
// VBT
// =============================================================================================

/// `vbt_hooks(store, gateway, now=)`.
pub struct VbtHooks<S> {
    store: Mutex<S>,
    common: Common,
    /// `risk`.
    pub risk: Option<Arc<dyn RiskRelease>>,
}

impl<S> VbtHooks<S> {
    /// The store, for a caller that reads it back.
    pub const fn store(&self) -> &Mutex<S> {
        &self.store
    }
}

/// `vbt_hooks(store, gateway, now=)`.
pub fn vbt_hooks<S: VbtStore>(
    store: S,
    gateway: Arc<dyn Gateway>,
    settings: SharedSettings,
    now: NowFn,
    risk: Option<Arc<dyn RiskRelease>>,
) -> VbtHooks<S> {
    VbtHooks {
        store: Mutex::new(store),
        common: Common {
            gateway,
            settings,
            now,
        },
        risk,
    }
}

fn vbt_exec_text(e: &vbt_x::ExecError) -> HookError {
    match e {
        vbt_x::ExecError::Gateway(g) => gateway_text(g),
        vbt_x::ExecError::Http(h) => format!("HTTPException: {}: {}", h.status, h.detail),
        vbt_x::ExecError::Store(s) => s.to_string(),
    }
}

#[async_trait::async_trait]
impl<S: VbtStore + Sync> SleeveHooks for VbtHooks<S> {
    fn name(&self) -> &str {
        "vbt"
    }

    async fn open_orders(&self) -> Result<Vec<PyDict>, HookError> {
        let rows = self
            .store
            .lock()
            .await
            .open_orders()
            .await
            .map_err(|e| e.to_string())?;
        Ok(rows.iter().map(rec_py).collect())
    }

    async fn on_order_update(&self, payload: &PyDict) -> Result<Option<PyValue>, HookError> {
        vbt_precheck(payload)?;
        let settings = snapshot(&self.common.settings);
        let mut store = self.store.lock().await;
        let outcome = vbt_x::on_order_update(
            &mut *store,
            self.common.gateway.as_ref(),
            &settings,
            payload,
            (self.common.now)(),
        )
        .await
        .map_err(|e| vbt_exec_text(&e))?;
        Ok(outcome.map(|o| outcome_value(&o.status)))
    }

    async fn open_positions(&self) -> Result<Vec<PyDict>, HookError> {
        let rows = self
            .store
            .lock()
            .await
            .open_positions()
            .await
            .map_err(|e| e.to_string())?;
        Ok(rows.iter().map(rec_py).collect())
    }

    fn has_risk(&self) -> bool {
        self.risk.is_some()
    }

    async fn release(&self, symbol: &str, value: f64) -> Result<(), HookError> {
        match &self.risk {
            Some(r) => r.release(symbol, value).await,
            None => Ok(()),
        }
    }
}

// =============================================================================================
// Swing
// =============================================================================================

/// `swing_hooks(store, gateway, now=)`.
pub struct SwingHooks<S> {
    store: Mutex<S>,
    common: Common,
    /// `risk`.
    pub risk: Option<Arc<dyn RiskRelease>>,
}

impl<S> SwingHooks<S> {
    /// The store, for a caller that reads it back.
    pub const fn store(&self) -> &Mutex<S> {
        &self.store
    }
}

/// `swing_hooks(store, gateway, now=)`.
pub fn swing_hooks<S: SwingStore>(
    store: S,
    gateway: Arc<dyn Gateway>,
    settings: SharedSettings,
    now: NowFn,
    risk: Option<Arc<dyn RiskRelease>>,
) -> SwingHooks<S> {
    SwingHooks {
        store: Mutex::new(store),
        common: Common {
            gateway,
            settings,
            now,
        },
        risk,
    }
}

/// `swing_execute._session_day(now)`.
fn swing_session_day(now: DateTime<FixedOffset>) -> NaiveDate {
    now.with_timezone(&ist()).date_naive()
}

/// One `sent_buy_lines` row as the reconciler's open order.
fn swing_open_order(line: &LineRow) -> PyDict {
    let mut d = PyDict::new();
    d.insert(
        "broker_order_id",
        line.journal_ref.clone().map_or(PyValue::None, PyValue::Str),
    );
    d.insert("symbol", PyValue::Str(line.symbol.clone()));
    d.insert("quantity", PyValue::Int(line.quantity));
    d.insert("filled_quantity", PyValue::Int(0));
    d.insert(
        "reference_price",
        line.trigger
            .map_or(PyValue::None, |t| PyValue::Str(dstr(t))),
    );
    d
}

/// One open position as the reconciler reads it.
fn swing_position(p: &PositionRow) -> PyDict {
    let mut d = PyDict::new();
    d.insert("id", PyValue::Int(p.id));
    d.insert("instrument_id", PyValue::Int(p.instrument_id));
    d.insert("symbol", PyValue::Str(p.symbol.clone()));
    d.insert("quantity_entered", PyValue::Int(p.quantity_entered));
    d.insert("quantity_open", PyValue::Int(p.quantity_open));
    d.insert(
        "gtt_id",
        p.gtt_id.clone().map_or(PyValue::None, PyValue::Str),
    );
    d.insert("state", PyValue::Str(p.state.clone()));
    d.insert("simulated", PyValue::Bool(p.simulated));
    d
}

fn swing_exec_text(e: &swing_x::ExecError) -> HookError {
    match e {
        swing_x::ExecError::Gateway(g) => gateway_text(g),
        swing_x::ExecError::Http(h) => format!("HTTPException: {h}"),
        swing_x::ExecError::Store(s) => s.to_string(),
    }
}

#[async_trait::async_trait]
impl<S: SwingStore + Sync> SleeveHooks for SwingHooks<S> {
    fn name(&self) -> &str {
        "swing"
    }

    async fn open_orders(&self) -> Result<Vec<PyDict>, HookError> {
        let day = swing_session_day((self.common.now)());
        let lines = self
            .store
            .lock()
            .await
            .sent_buy_lines(day)
            .await
            .map_err(|e| e.to_string())?;
        Ok(lines
            .iter()
            .filter(|l| l.journal_ref.as_ref().is_some_and(|j| !j.is_empty()))
            .map(swing_open_order)
            .collect())
    }

    async fn on_order_update(&self, payload: &PyDict) -> Result<Option<PyValue>, HookError> {
        let settings = snapshot(&self.common.settings);
        let now = (self.common.now)();
        let mut store = self.store.lock().await;
        let update = match swing_update(payload) {
            Ok(u) => u,
            Err(raise) => {
                // Python reads the payload's numbers only once it holds a SENT swing line.
                let order_id = text_or_empty(payload, "order_id");
                if order_id.is_empty() {
                    return Ok(None);
                }
                let Some(line) = store
                    .line_by_order(&order_id)
                    .await
                    .map_err(|e| e.to_string())?
                else {
                    return Ok(None);
                };
                let current = store.line(line.id).await.map_err(|e| e.to_string())?;
                if current.is_none_or(|c| c.state != "SENT") {
                    return Ok(None);
                }
                return Err(raise);
            }
        };
        let outcome = swing_x::on_order_update(
            &mut *store,
            self.common.gateway.as_ref(),
            &settings,
            &update,
            now,
        )
        .await
        .map_err(|e| swing_exec_text(&e))?;
        Ok(outcome.map(|o| outcome_value(&o.status)))
    }

    async fn open_positions(&self) -> Result<Vec<PyDict>, HookError> {
        let rows = self
            .store
            .lock()
            .await
            .open_positions()
            .await
            .map_err(|e| e.to_string())?;
        Ok(rows.iter().map(swing_position).collect())
    }

    fn has_risk(&self) -> bool {
        self.risk.is_some()
    }

    async fn release(&self, symbol: &str, value: f64) -> Result<(), HookError> {
        match &self.risk {
            Some(r) => r.release(symbol, value).await,
            None => Ok(()),
        }
    }
}

// =============================================================================================
// The order book, and one pass from the desk
// =============================================================================================

/// The desk's `Kite` wrapper is the order book: three reads, one limiter slot each.
pub struct KiteBook(pub Arc<DeskKite>);

#[async_trait::async_trait]
impl OrderBook for KiteBook {
    async fn orders(&self) -> Result<Vec<PyValue>, HookError> {
        self.0.orders().await.map_err(|e| client_text(&e))
    }

    async fn get_gtts(&self) -> Result<Vec<PyValue>, HookError> {
        self.0.get_gtts().await.map_err(|e| client_text(&e))
    }

    async fn holdings(&self) -> Result<Vec<PyDict>, HookError> {
        self.0.holdings().await.map_err(|e| client_text(&e))
    }
}

/// `Kite.__init__`'s refusal, which `_main.kite()` raises first.
pub const NO_KITE_KEY: &str =
    "RuntimeError: KITE_API_KEY missing — copy .env.example to .env and fill it.";

/// `run_pass_from_desk(now)`: the three sleeves' hooks over the desk's own stores and
/// gateways, one pass.
///
/// # Errors
/// What Python's raises: no Kite key, a store or connection that will not open, VBT's stop band
/// out of range, or the pass itself ([`reconcile_once`]).
pub async fn run_pass_from_desk(
    state: &AppState,
    now: DateTime<FixedOffset>,
) -> Result<ReconcileRun, HookError> {
    let settings = state.settings_snapshot();
    // `kite = _main.kite()`: the constructor refuses without a key.
    if settings.kite_api_key.is_empty() {
        return Err(NO_KITE_KEY.to_owned());
    }
    let desk = state
        .gateway
        .as_any()
        .and_then(|a| a.downcast_ref::<DeskGateway>())
        .ok_or_else(|| "RuntimeError: the desk's gateway is not wired".to_owned())?;
    let book = KiteBook(desk.kite());
    let db_err = |e: crate::storage::StorageError| e.to_string();
    let swing_store = crate::lanes::swing::desk::open_store(state)
        .await
        .map_err(db_err)?;
    let twt_store = crate::lanes::twt::open_store(state).await.map_err(db_err)?;
    let vbt_store = crate::lanes::vbt::open_store(state).await.map_err(db_err)?;
    let mut conn = state.db.connect().await.map_err(db_err)?;
    let schema = if state.db.config().is_postgres() {
        "public"
    } else {
        ""
    };
    let mut issues = PgIssueStore::new(&mut conn, settings.sole_user_id, schema);
    let clock: NowFn = Arc::new(move || now);
    // `getattr(gateway, "risk", None)`: every sleeve gateway shares `app.main`'s one manager,
    // built by `_main.gateway()` before the first of them.
    let risk: Arc<dyn RiskRelease> = desk
        .risk()
        .await
        .map_err(|e| format!("{}: {}", e.python_type, e.message))?;
    let swing_gw = crate::lanes::swing::desk::swing_gateway(state)
        .await
        .ok_or_else(|| NO_KITE_KEY.to_owned())?;
    let twt_gw = crate::lanes::twt::twt_gateway(state);
    let vbt_gw = crate::lanes::vbt::vbt_gateway(state).ok_or_else(|| {
        "ValueError: VBT_STOP_BAND_MIN/MAX do not form a stop band; the VBT gateway cannot be \
         built"
            .to_owned()
    })?;
    let swing = swing_hooks(
        swing_store,
        swing_gw,
        state.settings.clone(),
        clock.clone(),
        Some(risk.clone()),
    );
    let twt = twt_hooks(
        twt_store,
        twt_gw,
        state.settings.clone(),
        clock.clone(),
        Some(risk.clone()),
    );
    let vbt = vbt_hooks(vbt_store, vbt_gw, state.settings.clone(), clock, Some(risk));
    reconcile_once(&book, &[&swing, &twt, &vbt], now, &mut issues).await
}

/// The session supervisor's [`ReconcilePass`] over [`run_pass_from_desk`].
pub struct DeskReconcilePass {
    state: AppState,
}

impl DeskReconcilePass {
    /// Over the desk's state.
    #[must_use]
    pub const fn new(state: AppState) -> Self {
        Self { state }
    }
}

impl std::fmt::Debug for DeskReconcilePass {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DeskReconcilePass").finish_non_exhaustive()
    }
}

/// `"<Type>: <message>"` back into the supervisor's error.
#[must_use]
pub fn dep_error(text: &str) -> DepError {
    match text.split_once(": ") {
        Some((kind, message))
            if !kind.is_empty() && kind.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') =>
        {
            DepError {
                kind: kind.to_owned(),
                message: message.to_owned(),
            }
        }
        _ => DepError::runtime(text),
    }
}

#[async_trait::async_trait]
impl ReconcilePass for DeskReconcilePass {
    async fn run(&self, now: DateTime<FixedOffset>) -> Result<PassOutcome, DepError> {
        let run = run_pass_from_desk(&self.state, now)
            .await
            .map_err(|e| dep_error(&e))?;
        Ok(PassOutcome {
            ok: run.ok(),
            line: run.line(),
        })
    }
}

/// `python -m app.reconcile`: one pass, its line printed; exit 0 when the run is clean, else 1.
/// Python lets a raise escape (a traceback and exit 1); here it is the error text and 1.
pub async fn main(state: &AppState) -> (i32, String) {
    let now = state.clock.now_ist();
    match run_pass_from_desk(state, now).await {
        Ok(run) => (i32::from(!run.ok()), run.line()),
        Err(e) => (1, e),
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;
    use crate::wire::dict;

    fn payload(pairs: &[(&str, PyValue)]) -> PyDict {
        dict(pairs.iter().map(|(k, v)| (*k, v.clone())))
    }

    #[test]
    fn twt_reads_the_payload_as_its_handler_does() {
        assert_eq!(twt_report(&payload(&[])), Ok(None));
        let r = twt_report(&payload(&[
            ("order_id", PyValue::Int(42)),
            ("status", PyValue::Str("rejected".into())),
            ("filled_quantity", PyValue::Str(" 7 ".into())),
            ("average_price", PyValue::Float(101.5)),
            ("status_message", PyValue::Str(String::new())),
            ("status_message_raw", PyValue::Str("RMS: no margin".into())),
        ]))
        .unwrap()
        .unwrap();
        assert_eq!(r.order_id, "42");
        assert_eq!(r.status, "REJECTED");
        assert_eq!(r.filled_quantity, 7);
        assert_eq!(r.average_price, Some(Decimal::new(1015, 1)));
        assert_eq!(r.status_message.as_deref(), Some("RMS: no margin"));
        assert_eq!(
            twt_report(&payload(&[
                ("order_id", PyValue::Str("B".into())),
                ("filled_quantity", PyValue::Str("forty".into())),
            ]))
            .unwrap_err(),
            "ValueError: invalid literal for int() with base 10: 'forty'"
        );
    }

    #[test]
    fn vbt_raises_only_after_the_order_id_check() {
        assert_eq!(
            vbt_precheck(&payload(&[("filled_quantity", PyValue::Str("x".into()))])),
            Ok(())
        );
        assert!(
            vbt_precheck(&payload(&[
                ("order_id", PyValue::Str("V1".into())),
                ("average_price", PyValue::Str("n/a".into())),
            ]))
            .unwrap_err()
            .starts_with("InvalidOperation")
        );
    }

    #[test]
    fn swing_reads_status_as_given_and_a_missing_average_as_zero() {
        let u = swing_update(&payload(&[
            ("order_id", PyValue::Str("S1".into())),
            ("status", PyValue::Str("complete".into())),
            ("filled_quantity", PyValue::Int(5)),
        ]))
        .unwrap();
        assert_eq!(u.status, "complete");
        assert_eq!(u.average_price, Decimal::ZERO);
        assert_eq!(u.filled_quantity, 5);
    }

    #[test]
    fn an_exception_text_becomes_the_supervisors_error() {
        let e = dep_error("KeyError: 'id'");
        assert_eq!((e.kind.as_str(), e.message.as_str()), ("KeyError", "'id'"));
        let e = dep_error("no type here");
        assert_eq!(e.kind, "RuntimeError");
        let e = dep_error(NO_KITE_KEY);
        assert_eq!(e.kind, "RuntimeError");
        assert!(e.message.starts_with("KITE_API_KEY missing"));
    }

    #[test]
    fn a_store_row_reads_decimal_as_its_text() {
        let rec = Rec::of([
            ("id", Val::Int(3)),
            ("reference_price", Val::Dec(Decimal::new(10000, 2))),
            ("gtt_id", Val::None),
            ("simulated", Val::Bool(true)),
        ]);
        let d = rec_py(&rec);
        assert_eq!(
            d.get("reference_price"),
            Some(&PyValue::Str("100.00".into()))
        );
        assert_eq!(d.get("gtt_id"), Some(&PyValue::None));
        assert_eq!(d.get("simulated"), Some(&PyValue::Bool(true)));
    }

    #[test]
    fn the_module_holds_no_writer() {
        let src = include_str!("reconcile_hooks.rs");
        let body = src.split("#[cfg(test)]").next().unwrap();
        for token in [
            concat!("Kite", "Writes"),
            concat!("place", "_order"),
            concat!(".place", "_gtt"),
            concat!("kc.", "place"),
        ] {
            assert!(!body.contains(token), "{token}");
        }
    }
}
