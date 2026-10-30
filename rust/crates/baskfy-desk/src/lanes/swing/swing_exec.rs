//! Lane **swing_exec** (parent: swing) — ports `app/swing_execute.py` (SW7, SW10.4, SW10.5:
//! one confirmed plan line becomes orders, a stop and rows, through the gateway it is handed)
//! and `app/swing_live_gate.py` (SW30/SW31: the gate re-read on the live index between 09:15
//! and 15:30).
//!
//! Neither Python module mounts a route: `POST /swing/execute`, `/swing/rearm`,
//! `/swing/cutoff` and the postback live in `app/swing_desk.py` and call in here, so this lane
//! registers no routes and no template globals. `BASKFY_SWING_AUTO_EXECUTE` (SW25/SW26) is read
//! by the monitor (`app/swing_monitor.py`), never here: everything in this module runs only
//! when a caller hands it `confirm == "true"` and a `plan_id` issued in the last thirty
//! minutes. Every clock is injected (`now`, [`execute::PollTimer`]).
//!
//! The non-negotiables are ported as tests first (`swing_exec/tests.rs`, from
//! `tests/test_swing_execute.py`): confirm/plan/expiry refusals, `DRY_RUN` and the execution
//! flag, the GTT stop on every buy in the same call, CNC-only gates, `client_id =
//! plan_id:symbol…`, the three-entries-a-session cap, A9's first-live half risk.

pub mod execute;
pub mod live_gate;

#[cfg(test)]
mod tests;
