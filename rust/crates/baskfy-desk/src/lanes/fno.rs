//! Lane **fno** — the F&O overnight book: page, confirm, monitor (F3 auto-exit M.5, flag
//! default false; exits only).
//!
//! **Ported here (sub-lane fno_desk):**
//! * `app/fno_gates.py` → [`gates`] (`fno_gates`, `product_gates`, `paper_gates`,
//!   `f3_auto_exit_enabled` — M.5's exit-only flag, default false; no unattended-entry input);
//! * `app/fno_clock.py` → [`clock`] (`run_day`);
//! * `app/fno_ledger.py` → [`ledger`] (the journal, `04` §7's pauses, `entry_block`, and
//!   [`ledger::DeskLedger`], the executor's `FoLedger`);
//! * `app/fno_desk.py` → [`desk`]: `GET /fno`, `GET /fno/data`, `POST /fno/execute` (the one
//!   route that moves anything: `confirm=true` and an `ISSUED` plan, through
//!   `fno_exec::execute`), and the nav's `fno_badge()` global.
//!
//! **Sibling sub-lane:** [`fno_exec`] (`app/fno_execute.py`, the monitor's order half).
//!
//! **Tests ported:** `test_fno_gates.py`, `test_fno_monitor_loop.py::TestTheDay`, the
//! structural/pure halves of `test_fno_desk.py` and `test_fno_ledger.py` (the DB-backed world is
//! graded by `tools/parity/desk_diff.py` on `desk_cases/fno_desk.json`).

pub mod clock;
pub mod desk;
pub mod gates;
pub mod ledger;

pub mod fno_exec;

use axum::Router;
use axum::routing::{get, post};

use crate::app::AppState;

/// The lane's routes.
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/fno", get(desk::page))
        .route("/fno/data", get(desk::data))
        .route("/fno/execute", post(desk::execute))
}

/// Hand the desk's state to the `fno_badge()` global (idempotent; every fno handler calls it,
/// and the integrator may call it right after `build_state`, DECISIONS-RS R8.F2).
pub fn install(state: &AppState) {
    crate::lanes::options::badge::install(state);
}

/// The lane's template globals: `fno_badge()` (`app.main`: `templates.env.globals["fno_badge"]
/// = fno_desk.nav_badge`), which every page's nav calls and which never fails.
pub fn register_templates(templates: &mut crate::templates::Templates) {
    templates.env_mut().add_function("fno_badge", || {
        let state = crate::lanes::options::badge::installed();
        let badge = desk::nav_badge(state.as_ref());
        crate::templates::py_value(&crate::wire::PyValue::Map(badge))
    });
}

/// Run one of the lane's background services by name; `None` if the name is not this lane's.
pub async fn run_service(name: &str, state: &AppState) -> Option<Result<(), String>> {
    let _ = state;
    match name {
        "fno-monitor" => Some(Err(format!("service {name} is not ported yet (lane fno)"))),
        _ => None,
    }
}
