//! Lane **options** — the NIFTY options book (`/nifty-options`) and the frozen lab's hook (no
//! auto-execute flag, ever).
//!
//! **Ported** (DECISIONS-RS R8.F1–R8.F6):
//! * `app/options_gates.py`, `app/options_lab.py` → [`gates`];
//! * `app/options_execute.py` → [`execute`] (+ [`store`], the `op_` tables) over the pure
//!   executor in [`core`] (`baskfy_core.options.executor`/`execution`/`risk`, which
//!   `baskfy-core` has not ported yet);
//! * `app/options_ledger.py` → [`ledger`];
//! * `app/options_desk.py` → [`desk`] and the four routes below;
//! * `app/options_monitor.py`'s store half (`PgPositionStore`) → [`store`]; its runner →
//!   [`options_monitor`] / [`run_service`];
//! * `app/strategies/nifty_options.py` → [`monitor`] (over `baskfy_core::options::exits`,
//!   `bars`, wave D2, R8.20.2);
//! * `app/options_clock.py` → [`clock`]; `app/strategies/intraday_skeleton.py` → [`intraday`];
//! * `scripts/options_monitor_loop.py` (the container's clock) → [`monitor_loop`];
//! * `tools/options/replay.py` (a recorded day through the monitor) → [`replay`].
//!
//! **The ticker.** This binary has no live Kite websocket (R8.0/R8.3), so the session's bus is
//! quiet and the B10 quote fallback (`LegQuotes`) is the feed — what Python does when its
//! websocket cannot reach Kite; `follow(tokens)` has no ticker to subscribe (R8.20.2).
//!
//! **Routes:** `GET /nifty-options`, `GET /nifty-options/data`, `POST /nifty-options/execute`,
//! `POST /nifty-options/close`.
//!
//! **Non-negotiables held here:** nothing in this lane confirms without a request carrying
//! `confirm=true` and an `ISSUED` plan inside its window; LIVE is refused (`LIVE_NOT_BUILT`);
//! `OPTIONS_ENABLED` defaults off and NFO is enforced in the gateway (the derived gateway's
//! gates are [`gates::product_gates`], read per order); there is no options auto-execute flag.

pub mod badge;
pub mod clock;
pub mod core;
pub mod desk;
pub mod execute;
pub mod gates;
pub mod intraday;
pub mod ledger;
pub mod monitor;
pub mod monitor_loop;
pub mod py;
pub mod replay;
pub mod store;
pub mod web;

use axum::Router;
use axum::routing::{get, post};

use crate::app::AppState;

/// `app.options_lab.lab_enabled()`: the frozen lab's nav entry and routes appear only with
/// `OPTIONS_ENABLED` **and** a thawed lab (OP13.1). The Rust desk ships no lab (it is frozen in
/// `frozen/strangle/` and is not ported, M6), so the lab is never present here and this is the
/// switch ANDed with `false`; [`gates::lab_enabled`] is the probe over a desk root.
#[must_use]
pub const fn lab_enabled(settings: &crate::settings::Settings) -> bool {
    let _ = settings.options_enabled;
    false
}

/// The lane's routes.
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/nifty-options", get(desk::page))
        .route("/nifty-options/data", get(desk::data))
        .route("/nifty-options/execute", post(desk::execute))
        .route("/nifty-options/close", post(desk::close))
}

/// The lane's template globals (none; `options_enabled` is the app's).
pub fn register_templates(templates: &mut crate::templates::Templates) {
    let _ = templates;
}

/// `Kite()`'s refusal without a key (`kite_client.Kite.__init__`).
pub const KITE_KEY_MISSING: &str =
    "RuntimeError: KITE_API_KEY missing — copy .env.example to .env and fill it.";

/// `int(os.environ.get("BASKFY_SOLE_USER_ID", "0") or 0)`.
///
/// # Errors
/// A value `int()` refuses (Python's `ValueError`).
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

/// The quote fallback's endpoint: the desk's `Kite().quote_raw(keys)` (the slot-taking read).
struct DeskQuoteRaw(std::sync::Arc<crate::lanes::exec_core::kite_client::DeskKite>);

#[async_trait::async_trait]
impl clock::QuoteRaw for DeskQuoteRaw {
    async fn quote_raw(&self, keys: &[String]) -> Result<crate::wire::PyDict, String> {
        match self.0.quote_raw(keys).await {
            Ok(crate::wire::PyValue::Map(m)) => Ok(m),
            Ok(_) => Ok(crate::wire::PyDict::new()),
            Err(e) => Err(e.python_type),
        }
    }
}

/// The session's bus: no websocket in this binary, so nothing is ever queued.
struct QuietBus;

impl clock::Bus for QuietBus {
    fn subscribe(&mut self, token: i64) {
        let _ = token;
    }

    fn drain(&mut self, token: i64) -> Vec<crate::wire::PyDict> {
        let _ = token;
        Vec::new()
    }
}

/// `ist_now`, `time.monotonic`, `asyncio.sleep` over the desk's clock.
struct DeskSeams(std::sync::Arc<dyn crate::clock::Clock>);

#[async_trait::async_trait]
impl clock::Seams for DeskSeams {
    fn now(&mut self) -> chrono::NaiveDateTime {
        self.0.now_ist().naive_local()
    }

    fn clock(&mut self) -> f64 {
        self.0.monotonic()
    }

    async fn sleep(&mut self, seconds: f64) {
        tokio::time::sleep(std::time::Duration::from_secs_f64(seconds.max(0.0))).await;
    }
}

/// The sweep the runner hands `act`: `run_pending_exits(book, gateway_for, quotes=, mode_of=)`
/// on the desk's gateway — every raised exit closed under its entry's confirm (PACK.2).
struct Sweep {
    state: AppState,
    book: crate::storage::Conn,
    user_id: i64,
}

#[async_trait::async_trait]
impl clock::Act for Sweep {
    async fn act(&mut self, _now: chrono::NaiveDateTime) -> Result<(), String> {
        let settings = self.state.settings_snapshot();
        let state = self.state.clone();
        let gateway_for = move |sleeve| execute::options_gateway(&state, sleeve);
        let quotes = execute::KiteQuotes {
            kite: std::sync::Arc::clone(&self.state.kite),
        };
        let modes = |s| gates::mode_of(&settings, s);
        let clock = std::sync::Arc::clone(&self.state.clock);
        let now = move || clock.now_ist();
        let mut book = store::PgOptionsStore::new(&mut self.book, self.user_id);
        execute::run_pending_exits(&mut book, &gateway_for, &quotes, &modes, &now)
            .await
            .map(|_| ())
            .map_err(|e| match e {
                execute::ExecError::Refused(r) => r.message,
                execute::ExecError::Other(m) => m,
            })
    }
}

/// `options_monitor.main()`: the exit code — 0 (the flag off, or a session done), 2 without a
/// user.
///
/// # Errors
/// What Python's `main` lets escape: a bad user id, `Kite()` without a key, a database that
/// cannot be opened, the morning ledger's database error, the strategy's exception.
pub async fn options_monitor_main(
    state: &AppState,
    env: &(dyn Fn(&str) -> Option<String> + Send + Sync),
) -> Result<i32, String> {
    badge::install(state);
    let settings = state.settings_snapshot();
    if !settings.options_monitor_enabled {
        monitor::emit(
            "INFO desk.options_monitor BASKFY_OPTIONS_MONITOR_ENABLED is false; exiting without \
             building the monitor",
        );
        return Ok(0);
    }
    let user_id = sole_user_id(env("BASKFY_SOLE_USER_ID").as_deref())?;
    if user_id == 0 {
        monitor::emit(
            "ERROR desk.options_monitor BASKFY_SOLE_USER_ID is not set; op_ rows are keyed by user",
        );
        return Ok(2);
    }
    let day = state.clock.now_ist().date_naive();
    if settings.kite_api_key.is_empty() {
        return Err(KITE_KEY_MISSING.to_owned());
    }
    let desk = state
        .gateway
        .as_any()
        .and_then(|a| a.downcast_ref::<crate::lanes::exec_core::desk::DeskGateway>())
        .ok_or_else(|| "RuntimeError: the desk's Kite client is not available".to_owned())?;
    let kite = desk.kite();
    let conn = state.db.connect().await.map_err(|e| e.to_string())?;
    let store = monitor::PgPositionStore { conn, user_id };
    // No gateway at all: the monitor decides exits, the executor sends them.
    let Some(mut strategy) = monitor::build_monitor(settings.options_monitor_enabled, store, day)
    else {
        return Ok(0);
    };
    let mut book_conn = state.db.connect().await.map_err(|e| e.to_string())?;
    {
        // 09:00 (OP11): the ledger's rules before the open.
        let mut book = store::PgOptionsStore::new(&mut book_conn, user_id);
        let modes = |s| gates::mode_of(&settings, s);
        ledger::morning_ledger(&mut book, state.clock.now_ist(), &modes)
            .await
            .map_err(|e| e.py_text())?;
    }
    // `_main.gateway()` builds the shared risk manager before any sleeve's gateway.
    let _ = desk.risk().await;
    let mut sweep = Sweep {
        state: state.clone(),
        book: book_conn,
        user_id,
    };
    let reader = DeskQuoteRaw(kite);
    let mut quotes = clock::LegQuotes::new(&reader, 5.0);
    let mut follow = |_: &[i64]| -> Result<(), String> { Ok(()) };
    let mut seams = DeskSeams(std::sync::Arc::clone(&state.clock));
    let raised = clock::run_session(
        &mut strategy,
        &mut QuietBus,
        Some(&mut follow),
        Some(&mut quotes),
        Some(&mut sweep),
        &mut seams,
        1.0,
    )
    .await?;
    monitor::emit(&format!(
        "INFO desk.options_monitor options monitor done: {raised} exit(s) raised"
    ));
    Ok(0)
}

/// `baskfy-desk service options-monitor` — `python -m app.options_monitor`: with
/// `BASKFY_OPTIONS_MONITOR_ENABLED` false it logs and returns 0 before touching the database,
/// Kite or a ticker — the monitor is not even built. Python's exit code 2 is kept.
///
/// # Errors
/// As [`options_monitor_main`].
pub async fn options_monitor(state: &AppState) -> Result<(), String> {
    match options_monitor_main(state, &|k| std::env::var(k).ok()).await? {
        0 => Ok(()),
        code => {
            monitor::emit(&format!("ERROR desk.options_monitor exiting {code}"));
            std::process::exit(code)
        }
    }
}

/// Run one of the lane's background services by name; `None` if the name is not this lane's.
pub async fn run_service(name: &str, state: &AppState) -> Option<Result<(), String>> {
    match name {
        "options-monitor" => Some(options_monitor(state).await),
        "options-monitor-loop" => {
            let program = std::env::current_exe()
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or_else(|_| "baskfy-desk".to_owned());
            let mut seams = monitor_loop::ProcessSeams {
                clock: std::sync::Arc::clone(&state.clock),
            };
            Some(
                tokio::task::spawn_blocking(move || {
                    monitor_loop::run_forever(&mut seams, &program, None);
                })
                .await
                .map_err(|e| e.to_string()),
            )
        }
        _ => None,
    }
}
