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
//! * `app/options_monitor.py`'s store half (`PgPositionStore`) → [`store`]; its runner's flag
//!   gate → [`run_service`];
//! * `app/options_clock.py` → [`clock`]; `app/strategies/intraday_skeleton.py` → [`intraday`].
//!
//! **Not ported (loud):** `app/strategies/nifty_options.py` (the monitor's strategy) needs
//! `baskfy_core.options.exits.evaluate`/`mark` and `bars`, which no crate has yet; with
//! `BASKFY_OPTIONS_MONITOR_ENABLED` off (its default) the service exits 0 exactly as Python's
//! does, and with it on the Rust service refuses (exit 2) rather than run half a monitor.
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
pub mod py;
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

/// `python -m app.options_monitor`: with `BASKFY_OPTIONS_MONITOR_ENABLED` false it logs and
/// returns 0 before touching the database, Kite or a ticker — the monitor is not even built.
///
/// # Errors
/// With the flag on: `BASKFY_SOLE_USER_ID` unset (Python's exit 2), or the strategy not ported.
pub async fn options_monitor(state: &AppState) -> Result<(), String> {
    badge::install(state);
    let settings = state.settings_snapshot();
    if !settings.options_monitor_enabled {
        return Ok(());
    }
    let user_id = std::env::var("BASKFY_SOLE_USER_ID")
        .ok()
        .and_then(|v| {
            if v.is_empty() {
                None
            } else {
                crate::settings::py_int(&v)
            }
        })
        .unwrap_or(0);
    if user_id == 0 {
        return Err("BASKFY_SOLE_USER_ID is not set; op_ rows are keyed by user".to_owned());
    }
    Err(
        "the options monitor's strategy (app/strategies/nifty_options.py) is not ported: \
         baskfy_core.options.exits is not in Rust yet (DECISIONS-RS R8.F5)"
            .to_owned(),
    )
}

/// Run one of the lane's background services by name; `None` if the name is not this lane's.
pub async fn run_service(name: &str, state: &AppState) -> Option<Result<(), String>> {
    match name {
        "options-monitor" => Some(options_monitor(state).await),
        _ => None,
    }
}
