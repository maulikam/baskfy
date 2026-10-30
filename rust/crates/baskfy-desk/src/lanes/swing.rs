//! Lane **swing** — the swing book: page, confirm, monitor (SW25/SW26 auto-execute flag, default false), notifier.
//!
//! **STATUS (wave D, split into three sibling lanes).** `swing_desk` — [`desk`] (the page, its
//! JSON and the seven routes below, over [`store`]'s `PgSwingStore`), [`breakout`]
//! (`strategies/swing_breakout.py`) and [`breadth`] (`app/breadth_source.py`); `swing_exec` —
//! [`swing_exec`] (`swing_execute.py`, `swing_live_gate.py`; [`execute`] and [`live_gate`]
//! re-export it); `swing_monitor` — [`swing_monitor`] (`swing_monitor.py`, `swing_clock.py`,
//! the notifier). The lane never edits `lib.rs`, `app.rs` or `lanes.rs`. A new dependency is a
//! DECISIONS-RS entry the integrator applies.
//!
//! **Python it ports:**
//! * `app/{swing_desk,swing_execute,swing_monitor,swing_clock,swing_live_gate,swing_notify}.py`
//! * `app/strategies/swing_breakout.py`
//!
//! **Routes:**
//! * `GET /swing`
//! * `GET /swing/data`
//! * `POST /swing/execute`
//! * `POST /swing/rearm`
//! * `POST /swing/reconcile`
//! * `POST /swing/cutoff`
//! * `POST /swing/scan`
//!
//! **Background services** (`baskfy-desk service <name>`, via [`run_service`]):
//! * swing-monitor (python -m app.swing_monitor)
//!
//! **Tests to port first** (`kite-momentum-rebalancer/tests/`, test functions per file):
//! * `test_swing_desk.py` (114)
//! * `test_swing_execute.py` (122)
//! * `test_swing_monitor.py` (84)
//! * `test_swing_monitor_schedule.py` (10)
//! * `test_swing_clock.py` (15)
//! * `test_swing_notify.py` (12)
//! * `test_swing_track_c.py` (22)
//! * `test_swing_budgets.py` (2)
//! * `test_swing_catalyst_desk.py` (5)
//!
//! 386 test functions in all (before parametrisation).

pub mod breadth;
pub mod breakout;
pub mod context;
pub mod desk;
pub mod execute;
pub mod gateway;
pub mod live_gate;
pub mod notify;
pub mod rules;
pub mod store;
pub mod swing_exec;
pub mod swing_monitor;
pub mod values;

use axum::Router;

use crate::app::AppState;

/// The lane's routes: the swing desk's seven (`app.swing_desk.router`, lane swing_desk), and
/// lane swing_monitor's harness-only `/__harness/swing-signal` (Starlette's 404 without
/// `--harness`).
pub fn routes() -> Router<AppState> {
    Router::new()
        .merge(desk::routes())
        .merge(swing_monitor::harness::routes())
}

/// The lane's template globals and filters (e.g. the fno lane's `fno_badge`).
pub fn register_templates(templates: &mut crate::templates::Templates) {
    let _ = templates;
}

/// Run one of the lane's background services by name; `None` if the name is not this lane's.
pub async fn run_service(name: &str, state: &AppState) -> Option<Result<(), String>> {
    match name {
        // `python -m app.swing_monitor` (lane swing_monitor): exits 0 with the flag off; SW25's
        // auto-execute sends only with all three flags on (each defaults false).
        // Boxed: the watch's future is deep, and the binary's `main` lays out every service's.
        "swing-monitor" => Some(Box::pin(swing_monitor::service::run(state)).await),
        _ => None,
    }
}
