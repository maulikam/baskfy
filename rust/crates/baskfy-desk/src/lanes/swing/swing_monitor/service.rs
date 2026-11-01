//! `python -m app.swing_monitor` — `baskfy-desk service swing-monitor`: the morning process,
//! wired to the desk exactly as `swing_monitor.main()` wires it.
//!
//! * The flag first: with `BASKFY_SWING_MONITOR_ENABLED` off it exits 0 before touching a
//!   database, a broker or a bus.
//! * `BASKFY_SOLE_USER_ID` (default `"0"` in this module, unlike `app.config`'s `"1"`): unset is
//!   exit 2; a value `int()` refuses is Python's `ValueError`.
//! * `Kite()` refuses without `KITE_API_KEY`; the metrics port is read (a bad one is fatal); the
//!   late login is waited for until 15:20.
//! * The watch: the person's config, the watchlist with the morning's circuit bands, the gate
//!   (SW30's live re-read over `kite_level`), the notifier, [`PgSignalStore`], the strategy
//!   with **no gateway**, the quote fallback, the gated heartbeat, and `run_until_close` with
//!   the chores at their hours and SW25's queue drained every pass through
//!   [`DeskAutoConfirm`] — the same `execute_line` `POST /swing/execute` reaches, with
//!   `confirm="true"`, the plan just written and the price the tick carried. It sends only
//!   when `DRY_RUN=false`, `BASKFY_SWING_EXECUTION_ENABLED=true` **and**
//!   `BASKFY_SWING_AUTO_EXECUTE=true` (each defaults to the safe side; flipping them is
//!   Maulik's hand). With any one off the queue is emptied and nothing is confirmed.
//! * The backstop clock (`run_after_close`) afterwards, unconditionally.
//!
//! **The ticker.** This binary has no live Kite websocket (R8.0/R8.3): `start_ticker` refuses to
//! connect, so the bus stays quiet and the B10 quote fallback is the feed — what Python does
//! when its websocket cannot reach Kite. An oversize subscription is still SW20's refusal and
//! ends the run, as Python's `ValueError` does (DECISIONS-RS R8.SM1).

use std::collections::BTreeMap;
use std::sync::Arc;

use async_trait::async_trait;
use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime};
use rust_decimal::Decimal;

use super::clock::{ChoreSeams, PriceOf, SweepCounter, run_after_close, run_chore};
use super::runner::{
    AutoConfirm, Confirmed, GatedHeartbeat, LoopTimings, MonitorStore, PgSignalStore,
    QuoteFallback, QuoteReader, Quotes, SESSION_POLL_SECONDS, TickBus, WatchHooks, build_monitor,
    load_config, load_watchlist, monitor_metrics_port, record_monitor_ran, run_until_close,
    session_wait_until, wait_for_session, write_heartbeat,
};
use crate::app::AppState;
use crate::gateway::Gateway;
use crate::lanes::exec_core::kite_client::DeskKite;
use crate::lanes::swing::breakout::{CandleSource, WatchedName};
use crate::lanes::swing::context::load_context;
use crate::lanes::swing::desk::{last_price, open_store, swing_gateway};
use crate::lanes::swing::execute::{
    ExecContext, ExecError, OrderReport, OrderSource, TokioTimer, execute_line,
};
use crate::lanes::swing::live_gate::{KiteLevel, LiveLevel};
use crate::lanes::swing::notify::{Notifier, build_senders};
use crate::lanes::swing::rules::{Candle, dec_of_float};
use crate::lanes::swing::store::{SCHEMA, SwingStore};
use crate::lanes::swing::values::{col, text};
use crate::storage::{Conn, Param};
use crate::wire::{PyDict, PyValue};
use baskfy_providers::records::PyDateTime;

/// `Kite()`'s refusal without a key (`kite_client.Kite.__init__`).
pub const KITE_KEY_MISSING: &str =
    "RuntimeError: KITE_API_KEY missing — copy .env.example to .env and fill it.";

/// `app.main.kite()`: the desk's Kite wrapper, when the gateway is the desk's.
#[must_use]
pub fn desk_kite(state: &AppState) -> Option<Arc<DeskKite>> {
    state
        .gateway
        .as_any()
        .and_then(|a| a.downcast_ref::<crate::lanes::exec_core::desk::DeskGateway>())
        .map(crate::lanes::exec_core::desk::DeskGateway::kite)
}

// =============================================================================================
// The desk's collaborators: the order source, the auto-confirm, the chores
// =============================================================================================

/// `swing_desk.KiteOrders`: A8's order source over the desk's Kite session — a READ of the
/// history, whose last row is the order's state. No answer → OPEN, unfilled.
pub struct MonitorOrders {
    state: AppState,
}

/// `swing_desk.order_source()`: `None` (with Python's warning) when `Kite()` cannot be built.
#[must_use]
pub fn order_source(state: &AppState) -> Option<MonitorOrders> {
    if state.settings_snapshot().kite_api_key.is_empty() {
        log_line!("WARNING swing_desk no order source: {KITE_KEY_MISSING}");
        return None;
    }
    Some(MonitorOrders {
        state: state.clone(),
    })
}

fn py_float(v: &PyValue) -> Option<f64> {
    match v {
        PyValue::Float(f) => Some(*f),
        #[allow(clippy::cast_precision_loss)]
        PyValue::Int(i) => Some(*i as f64),
        _ => None,
    }
}

#[async_trait]
impl OrderSource for MonitorOrders {
    async fn order_status(&self, order_id: &str) -> OrderReport {
        let open = || OrderReport {
            status: "OPEN".to_owned(),
            filled_quantity: 0,
            average_price: Decimal::ZERO,
        };
        let history = match self.state.kite.order_history(order_id).await {
            Ok(PyValue::List(h)) => h,
            Ok(_) => Vec::new(),
            Err(e) => {
                log_line!("WARNING swing_desk no order history for {order_id}: {e}");
                return open();
            }
        };
        let Some(PyValue::Map(last)) = history.last() else {
            return open();
        };
        let none = PyValue::None;
        let status = last.get("status").unwrap_or(&none);
        let status = if status.truthy() {
            status.py_str()
        } else {
            "OPEN".to_owned()
        };
        let filled = last
            .get("filled_quantity")
            .filter(|v| v.truthy())
            .map_or(Ok(0), super::runner::py_int_value)
            .unwrap_or(0);
        let avg = last
            .get("average_price")
            .filter(|v| v.truthy())
            .and_then(py_float)
            .map_or(Decimal::ZERO, dec_of_float);
        OrderReport {
            status,
            filled_quantity: filled,
            average_price: avg,
        }
    }
}

/// SW25's confirm: `swing_execute.execute_line(open_store(), swing_gateway(), plan_id=…,
/// line_id=…, confirm="true", now=_now(), last_price=<the tick's>, orders=order_source())` —
/// the path `POST /swing/execute` takes, gateway and guards included. Only ever called by
/// `drain_auto_execute`, which calls it only when all three flags are on.
pub struct DeskAutoConfirm {
    state: AppState,
}

impl DeskAutoConfirm {
    /// Over the desk's state.
    #[must_use]
    pub const fn new(state: AppState) -> Self {
        Self { state }
    }
}

/// The words an `execute_line` failure is logged with (`type(exc).__name__: exc`).
#[must_use]
pub fn exec_error_words(e: &ExecError) -> String {
    match e {
        ExecError::Http(h) => format!("HTTPException: {}: {}", h.status, h.detail),
        ExecError::Store(s) => format!("StoreError: {s}"),
        ExecError::Gateway(g) => format!("{}: {}", g.python_type, g.message),
    }
}

#[async_trait]
impl AutoConfirm for DeskAutoConfirm {
    async fn confirm(
        &self,
        plan_id: &str,
        line_id: i64,
        last_price: Decimal,
    ) -> Result<Confirmed, String> {
        let state = &self.state;
        let mut store = open_store(state)
            .await
            .map_err(|e| format!("OperationalError: {e}"))?;
        let gw = swing_gateway(state)
            .await
            .ok_or_else(|| KITE_KEY_MISSING.to_owned())?;
        let now = state.clock.now_ist();
        let orders = order_source(state);
        let settings = state.settings_snapshot();
        let timer = TokioTimer;
        let cx = ExecContext {
            settings: &settings,
            orders: orders.as_ref().map(|o| o as &dyn OrderSource),
            timer: &timer,
        };
        let outcome = execute_line(
            &mut store,
            gw.as_ref(),
            &cx,
            plan_id,
            line_id,
            "true",
            now,
            Some(last_price),
        )
        .await
        .map_err(|e| exec_error_words(&e))?;
        Ok(Confirmed {
            status: outcome.status,
            reason: outcome.reason,
        })
    }
}

/// `desk_swing_sweeps_total{sweep, outcome}` through the desk's telemetry.
#[derive(Debug, Default, Clone, Copy)]
pub struct TelemetryCounter;

impl SweepCounter for TelemetryCounter {
    fn count(&self, sweep: &str, outcome: &str) {
        crate::lanes::session::telemetry::count(
            "swing_sweeps",
            &[("sweep", sweep), ("outcome", outcome)],
        );
    }
}

/// `swing_desk.last_price` as the sweep's `price_of`.
pub struct DeskPrices {
    state: AppState,
}

#[async_trait]
impl PriceOf for DeskPrices {
    async fn price_of(&self, symbol: &str) -> Result<Option<Decimal>, String> {
        Ok(last_price(&self.state, symbol).await)
    }
}

/// The chores' seams over the desk (`swing_desk.open_store`, `swing_gateway`, `order_source`,
/// `last_price`), each built when its chore starts.
pub struct DeskChoreSeams {
    state: AppState,
}

impl DeskChoreSeams {
    /// Over the desk's state.
    #[must_use]
    pub const fn new(state: AppState) -> Self {
        Self { state }
    }
}

#[async_trait]
impl ChoreSeams for DeskChoreSeams {
    async fn open_store(&self) -> Result<Box<dyn SwingStore>, String> {
        open_store(&self.state)
            .await
            .map(|s| Box::new(s) as Box<dyn SwingStore>)
            .map_err(|e| format!("OperationalError: {e}"))
    }

    async fn gateway(&self) -> Result<Arc<dyn Gateway>, String> {
        swing_gateway(&self.state)
            .await
            .ok_or_else(|| KITE_KEY_MISSING.to_owned())
    }

    async fn orders(&self) -> Option<Arc<dyn OrderSource>> {
        order_source(&self.state).map(|o| Arc::new(o) as Arc<dyn OrderSource>)
    }

    fn prices(&self) -> Arc<dyn PriceOf> {
        Arc::new(DeskPrices {
            state: self.state.clone(),
        })
    }
}

// =============================================================================================
// The broker's reads: circuit bands, candles, quotes, the session
// =============================================================================================

/// `circuit_bands(kite, symbols)`: the day's upper circuit per symbol, from one quote pass per
/// 400 names (the raw client's `quote`, as Python's `kite.kc.quote`). A failed chunk is a
/// missing band, not a halt.
pub async fn circuit_bands(state: &AppState, symbols: &[String]) -> BTreeMap<String, Decimal> {
    let mut out = BTreeMap::new();
    for chunk in symbols.chunks(400) {
        let keys: Vec<String> = chunk.iter().map(|s| format!("NSE:{s}")).collect();
        let data = match state.kite.quote(&keys).await {
            Ok(PyValue::Map(m)) => m,
            Ok(_) => continue,
            Err(e) => {
                log_line!("WARNING swing_monitor circuit quote failed: {e}");
                continue;
            }
        };
        for (key, row) in data.iter() {
            let PyValue::Map(row) = row else { continue };
            let Some(band) = row.get("upper_circuit_limit").filter(|b| b.truthy()) else {
                continue;
            };
            let Some(value) = super::runner::py_decimal_value(Some(band)) else {
                continue;
            };
            let symbol = key.split_once(':').map_or(key, |(_, s)| s);
            out.insert(symbol.to_owned(), value);
        }
    }
    out
}

/// `KiteCandles`: `historical_data(interval="minute")` from 09:15 to `until`.
pub struct KiteCandles {
    state: AppState,
}

#[async_trait]
impl CandleSource for KiteCandles {
    async fn minute_candles(
        &self,
        token: i64,
        day: NaiveDate,
        until: NaiveDateTime,
    ) -> Result<Vec<Candle>, String> {
        let start = day.and_hms_opt(9, 15, 0).unwrap_or_default();
        let raw = self
            .state
            .kite
            .historical_data(
                token,
                &start.format("%Y-%m-%d %H:%M:%S").to_string(),
                &until.format("%Y-%m-%d %H:%M:%S").to_string(),
                "minute",
                false,
                false,
            )
            .await
            .map_err(|e| e.to_string())?;
        let PyValue::List(rows) = raw else {
            return Ok(Vec::new());
        };
        let mut candles = Vec::new();
        for row in rows {
            let PyValue::Map(row) = row else { continue };
            let start = match row.get("date") {
                Some(PyValue::DateTime(PyDateTime::Naive(t))) => *t,
                Some(PyValue::DateTime(PyDateTime::Aware(t))) => t.naive_local(),
                _ => return Err("TypeError: a candle without a datetime".to_owned()),
            };
            let num = |k: &str| {
                super::runner::py_decimal_value(row.get(k))
                    .ok_or_else(|| format!("InvalidOperation: candle {k}"))
            };
            let none = PyValue::None;
            let zero = PyValue::Int(0);
            candles.push(Candle {
                start,
                open: num("open")?,
                high: num("high")?,
                low: num("low")?,
                close: num("close")?,
                volume: super::runner::py_int_value(row.get("volume").unwrap_or(&none).or(&zero))?,
            });
        }
        Ok(candles)
    }
}

/// `Kite.quote_raw` (one `quote` slot through the desk's limiter), else the raw client.
pub struct KiteQuotes {
    state: AppState,
}

#[async_trait]
impl QuoteReader for KiteQuotes {
    async fn quote_raw(&self, keys: &[String]) -> Result<PyValue, String> {
        if let Some(kite) = desk_kite(&self.state) {
            return kite
                .quote_raw(keys)
                .await
                .map_err(|e| format!("{}: {}", e.python_type, e.message));
        }
        match self.state.kite.quote(keys).await {
            Ok(PyValue::None) => Ok(PyValue::Map(PyDict::default())),
            Ok(v) => Ok(v),
            Err(e) => Err(e.to_string()),
        }
    }
}

/// `kite.is_authed()`: the desk wrapper re-reads a rotated token first.
pub async fn is_authed(state: &AppState) -> Result<bool, String> {
    if let Some(kite) = desk_kite(state) {
        return Ok(kite.is_authed().await);
    }
    Ok(state.kite.profile().await.is_ok())
}

// =============================================================================================
// The bus and the loop's hooks
// =============================================================================================

/// The session lane's `TickBus` as the loop reads it: one bounded queue per token, the newest
/// subscription replacing an older one (`queues[token] = bus.subscribe(token)`).
pub struct BusQueues {
    bus: Arc<crate::lanes::session::ticker::TickBus>,
    queues: BTreeMap<i64, tokio::sync::mpsc::Receiver<PyDict>>,
}

impl BusQueues {
    /// Over a bus.
    #[must_use]
    pub fn new(bus: Arc<crate::lanes::session::ticker::TickBus>) -> Self {
        Self {
            bus,
            queues: BTreeMap::new(),
        }
    }
}

impl TickBus for BusQueues {
    fn subscribe(&mut self, token: i64) {
        self.queues.insert(token, self.bus.subscribe(token));
    }

    fn drain(&mut self, token: i64) -> Vec<PyDict> {
        let mut out = Vec::new();
        if let Some(q) = self.queues.get_mut(&token) {
            while let Ok(t) = q.try_recv() {
                out.push(t);
            }
        }
        out
    }
}

/// `run_until_close`'s keyword collaborators as `main` builds them: the desk's clock, the real
/// chores, `record_monitor_ran` on the first tick, the reload with fresh circuit bands, the
/// gated heartbeat, all over the monitor's own connection.
pub struct DeskHooks {
    state: AppState,
    conn: Conn,
    user_id: i64,
    day: NaiveDate,
    started: std::time::Instant,
    gated: GatedHeartbeat,
}

impl DeskHooks {
    /// Over the desk's state and the monitor's own connection, for `user_id` on `day`.
    #[must_use]
    pub fn new(state: AppState, conn: Conn, user_id: i64, day: NaiveDate) -> Self {
        Self {
            state,
            conn,
            user_id,
            day,
            started: std::time::Instant::now(),
            gated: GatedHeartbeat::new(60.0),
        }
    }

    fn settings(&self) -> crate::settings::Settings {
        self.state.settings_snapshot()
    }
}

/// `watched_symbols()`: every WATCHING symbol, unordered as Python's query leaves it.
async fn watched_symbols(conn: &mut Conn, user_id: i64) -> Result<Vec<String>, String> {
    let rows = conn
        .execute(
            &format!(
                "SELECT i.symbol FROM {SCHEMA}.sw_watch w JOIN {SCHEMA}.instrument i \
                 ON i.id = w.instrument_id WHERE w.user_id = ? AND w.state = 'WATCHING'"
            ),
            &[Param::Int(user_id)],
        )
        .await
        .map_err(|e| e.to_string())?
        .rows;
    Ok(rows.iter().map(|r| text(col(r, "symbol"))).collect())
}

/// `reload_watchlist()`: the list with this moment's circuit bands.
async fn reload_watchlist(
    state: &AppState,
    conn: &mut Conn,
    user_id: i64,
) -> Result<Vec<WatchedName>, String> {
    let symbols = watched_symbols(conn, user_id).await?;
    let bands = circuit_bands(state, &symbols).await;
    load_watchlist(conn, user_id, &bands).await
}

#[async_trait]
impl WatchHooks for DeskHooks {
    fn now(&mut self) -> NaiveDateTime {
        self.state.clock.now_ist().naive_local()
    }

    fn monotonic(&mut self) -> f64 {
        self.started.elapsed().as_secs_f64()
    }

    async fn sleep(&mut self, seconds: f64) {
        if seconds > 0.0 {
            tokio::time::sleep(std::time::Duration::from_secs_f64(seconds)).await;
        }
    }

    async fn run_chore(&mut self, name: &str, _day: NaiveDate) -> Result<(), String> {
        let seams = DeskChoreSeams::new(self.state.clone());
        run_chore(
            name,
            &seams,
            &self.settings(),
            self.state.clock.now_ist(),
            &TelemetryCounter,
        )
        .await;
        Ok(())
    }

    async fn on_first_tick(&mut self) -> Result<(), String> {
        let settings = self.settings();
        record_monitor_ran(&mut self.conn, self.user_id, self.day, 0, &settings)
            .await
            .map_err(|e| e.to_string())
    }

    fn has_reload(&self) -> bool {
        true
    }

    async fn reload(&mut self) -> Result<Vec<WatchedName>, String> {
        reload_watchlist(&self.state, &mut self.conn, self.user_id).await
    }

    fn has_heartbeat(&self) -> bool {
        true
    }

    async fn heartbeat(
        &mut self,
        store: &mut dyn MonitorStore,
        detail: &str,
    ) -> Result<(), String> {
        let mono = self.started.elapsed().as_secs_f64();
        let line = self.gated.line(store, detail, mono).await?;
        write_heartbeat(
            &mut self.conn,
            self.user_id,
            &line,
            self.state.clock.now_ist(),
        )
        .await
        .map_err(|e| e.to_string())
    }
}

// =============================================================================================
// main()
// =============================================================================================

/// `BASKFY_SOLE_USER_ID` as this module reads it: `int(os.environ.get(K, "0") or 0)`.
///
/// # Errors
/// Python's `ValueError` for a value `int()` refuses.
pub fn sole_user_id(raw: Option<&str>) -> Result<i64, String> {
    match raw.filter(|v| !v.is_empty()) {
        None => Ok(0),
        Some(v) => crate::settings::py_int(v).ok_or_else(|| {
            format!(
                "ValueError: invalid literal for int() with base 10: {}",
                baskfy_providers::py::repr_str(v)
            )
        }),
    }
}

/// `swing_monitor.main()`: the exit code (0, or 2 without a user).
///
/// # Errors
/// What Python's `main` lets escape: a bad user id or metrics port, `Kite()` without a key, a
/// database that cannot be read, an oversize subscription, a malformed tick.
#[allow(clippy::too_many_lines)]
pub async fn main(state: &AppState) -> Result<i32, String> {
    main_with(state, &|k| std::env::var(k).ok()).await
}

/// [`main`] over a given environment (`os.environ.get`), for the tests.
///
/// # Errors
/// As [`main`].
pub async fn main_with(
    state: &AppState,
    env: &(dyn Fn(&str) -> Option<String> + Send + Sync),
) -> Result<i32, String> {
    let settings = state.settings_snapshot();
    if !settings.swing_monitor_enabled {
        log_line!(
            "INFO swing_monitor BASKFY_SWING_MONITOR_ENABLED is false; exiting without building \
             the monitor"
        );
        return Ok(0);
    }
    let user_id = sole_user_id(env("BASKFY_SOLE_USER_ID").as_deref())?;
    if user_id == 0 {
        log_line!(
            "ERROR swing_monitor BASKFY_SOLE_USER_ID is not set; the watchlist is keyed by user"
        );
        return Ok(2);
    }
    let day = state.clock.now_ist().date_naive();
    if settings.kite_api_key.is_empty() {
        return Err(KITE_KEY_MISSING.to_owned());
    }
    let port = monitor_metrics_port(
        env("DESK_MONITOR_METRICS_PORT").as_deref(),
        &settings.metrics_port,
    )?;
    let telemetry = crate::lanes::session::telemetry::TelemetryConfig::from_settings(&settings);
    let _ = crate::lanes::session::telemetry::install(
        &telemetry,
        port.and_then(|p| u16::try_from(p).ok()),
    );
    let session = wait_for_session(
        || is_authed(state),
        || state.clock.now_ist(),
        |s| tokio::time::sleep(std::time::Duration::from_secs_f64(s.max(0.0))),
        session_wait_until(),
        SESSION_POLL_SECONDS,
    )
    .await;
    if session {
        watch_today(state, user_id, day).await?;
    } else {
        log_line!(
            "ERROR swing_monitor no Kite session arrived before {}; the monitor did not run",
            session_wait_until().format("%H:%M:%S")
        );
    }
    // The backstop clock (SW26): both chores normally already ran inside the watch; this runs
    // unconditionally because an empty or early-dead watch never reached their marks.
    let seams = DeskChoreSeams::new(state.clone());
    let code = run_after_close(
        day,
        &seams,
        &state.settings_snapshot(),
        || state.clock.now_ist(),
        |s| tokio::time::sleep(std::time::Duration::from_secs_f64(s.max(0.0))),
        &TelemetryCounter,
    )
    .await;
    Ok(code)
}

/// The `with connect() as conn:` block of `main`: build the watch and run it to the close.
async fn watch_today(state: &AppState, user_id: i64, day: NaiveDate) -> Result<(), String> {
    let settings = state.settings_snapshot();
    let mut conn = state.db.connect().await.map_err(|e| e.to_string())?;
    let config = load_config(&mut conn, user_id, SCHEMA).await?;
    let watchlist = reload_watchlist(state, &mut conn, user_id).await?;
    let live_level: Arc<dyn LiveLevel> = Arc::new(KiteLevel::new(
        state.kite.clone(),
        &settings.swing_index_slug,
    ));
    let context = load_context(
        &mut conn,
        user_id,
        day,
        SCHEMA,
        Some(live_level.as_ref()),
        Some(state.clock.now_ist()),
        &settings.swing_index_slug,
    )
    .await
    .map_err(|e| e.to_string())?;
    let notifier = Notifier::new(
        build_senders(&settings),
        Some(Box::new(|channel: &str, outcome: &str| {
            crate::lanes::session::telemetry::count(
                "swing_notifications",
                &[("channel", channel), ("outcome", outcome)],
            );
            Ok(())
        })),
    );
    let clock = state.clock.clone();
    let mut store = PgSignalStore {
        conn,
        user_id,
        day,
        config: config.clone(),
        live_level: Some(live_level),
        context: Some(context),
        lines_written: Vec::new(),
        pending_confirms: Vec::new(),
        notifier: Some(notifier),
        notices: Vec::new(),
        settings: settings.clone(),
        ids: state.ids.clone(),
        now: Arc::new(move || clock.now_ist()),
    };
    // No gateway at all: the strategy takes none.
    let strategy = build_monitor(
        settings.swing_monitor_enabled,
        watchlist.clone(),
        day,
        config,
        None,
    )
    .map_err(|e| format!("ValueError: {e}"))?;
    let mut hooks = DeskHooks::new(
        state.clone(),
        state.db.connect().await.map_err(|e| e.to_string())?,
        user_id,
        day,
    );
    if watchlist.is_empty() {
        log_line!(
            "INFO swing_monitor nothing to watch today (0 names) yet; watching for the list to \
             change"
        );
        record_monitor_ran(&mut hooks.conn, user_id, day, 0, &settings)
            .await
            .map_err(|e| e.to_string())?;
    }
    let Some(mut strategy) = strategy else {
        log_line!("INFO swing_monitor the monitor was not built");
        return Ok(());
    };
    strategy.observe_with(Some(Box::new(|seconds: f64| {
        crate::lanes::session::telemetry::observe("swing_verdict_seconds", seconds);
        Ok(())
    })));
    let quotes = KiteQuotes {
        state: state.clone(),
    };
    let fallback = QuoteFallback::ordered(
        watchlist
            .iter()
            .map(|w| (w.token, w.symbol.clone()))
            .collect(),
        None,
    );
    let ticker = Arc::new(crate::lanes::session::ticker::TickBus::new());
    if let Err(e) = crate::lanes::session::ticker::start_ticker(&strategy.tokens(), &ticker) {
        if strategy.tokens().len() > crate::lanes::session::ticker::MAX_INSTRUMENTS_PER_CONNECTION {
            return Err(format!("ValueError: {e}"));
        }
        log_line!(
            "WARNING swing_monitor the ticker is not connected ({e}); the quote fallback is the feed"
        );
    }
    let mut bus = BusQueues::new(ticker);
    let candles = KiteCandles {
        state: state.clone(),
    };
    let confirm = DeskAutoConfirm::new(state.clone());
    let raised = run_until_close(
        &mut strategy,
        &mut store,
        &candles,
        &mut bus,
        &mut hooks,
        Some(Quotes {
            fallback,
            reader: &quotes,
        }),
        &settings,
        &confirm,
        LoopTimings::default(),
    )
    .await?;
    record_monitor_ran(
        &mut hooks.conn,
        user_id,
        day,
        i64::try_from(raised).unwrap_or(i64::MAX),
        &settings,
    )
    .await
    .map_err(|e| e.to_string())
}

/// `baskfy-desk service swing-monitor`: [`main`], with Python's exit code 2 kept.
///
/// # Errors
/// As [`main`].
pub async fn run(state: &AppState) -> Result<(), String> {
    match main(state).await? {
        0 => Ok(()),
        code => {
            log_line!("ERROR swing_monitor exiting {code}");
            std::process::exit(code)
        }
    }
}

/// The scheduler the desk's container runs around the service (`Dockerfile.desk`'s
/// `swing-monitor-loop`, `next_run`): 09:14 IST on a weekday, or **now** when a trading
/// session is under way and the flag is on — a restart at 13:09 joins the afternoon instead of
/// waiting for Monday. `close` is `monitor_close`.
#[must_use]
pub fn next_run(
    now: DateTime<FixedOffset>,
    enabled: bool,
    close: chrono::NaiveTime,
) -> DateTime<FixedOffset> {
    use chrono::{Datelike, TimeZone};
    let ist = crate::clock::ist();
    let local = now.with_timezone(&ist);
    let at = chrono::NaiveTime::from_hms_opt(9, 14, 0).unwrap_or_default();
    let at_on = |d: NaiveDate| {
        ist.from_local_datetime(&d.and_time(at))
            .single()
            .unwrap_or(local)
    };
    let mut run = at_on(local.date_naive());
    if run <= local {
        if enabled && local.weekday().num_days_from_monday() <= 4 && local.time() < close {
            return local;
        }
        run += chrono::Duration::days(1);
    }
    while run.weekday().num_days_from_monday() > 4 {
        run += chrono::Duration::days(1);
    }
    run
}
