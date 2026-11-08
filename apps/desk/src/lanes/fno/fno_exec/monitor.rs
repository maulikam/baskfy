//! Ports `app.fno_monitor` (FO7, M.5): the flag that builds it, the per-user entry plan id, the
//! 09:20 plans (F1, F2, F3 and F3's add — raised, never confirmed), the live watch that raises
//! EXIT/ROLL plans **under the entry's confirm**, the tick's lapse-and-send pass, the night's
//! marks and F2's trail, and the Postgres and Kite seams ([`seams`]).
//!
//! **THE FLAG.** `BASKFY_FNO_MONITOR_ENABLED` is false by default; [`service::run`] then
//! returns `Ok` without touching the database, Kite or a gateway — Python's `main()` returning
//! 0.
//!
//! **M.5, Maulik's (28 Sep 2026).** An F3 (`CREDIT_SPREAD`) exit is sent by the monitor itself
//! only when `exit_by_monitor(sleeve)` says so — wired to
//! [`crate::lanes::fno::gates::f3_auto_exit_enabled`], `BASKFY_FNO_F3_AUTO_EXIT`, **default false**;
//! otherwise it is written `ISSUED` with `awaits_click=true` and waits for `POST /fno/execute`.
//! The flag gates EXITS only: the monitor never confirms an ENTRY or an ADD (it raises an ADD
//! `ISSUED` for the click), and [`super::execute::run_pending`] sends only EXIT and ROLL plans.
//! Every exit it sends goes through the FO gateway, whose product gates are
//! `fno_gates(sleeve)` with `dry_run` pinned true and read per order (FO7.1): it is simulated,
//! never sent to a broker; `OPTIONS_ENABLED` is the gateway's to enforce.

pub mod engine;
pub mod harness;
pub mod night;
pub mod seams;
pub mod service;

#[cfg(test)]
mod tests_seams;

pub use engine::{FnoMonitor, NightReport, TickReport};
pub use service::run;

use chrono::{NaiveDate, NaiveTime};

use baskfy_core::fno::config::FoSleeve;
use baskfy_core::fno::exits::ExitReason;

use super::execute::{state, structure};

/// `SESSION_OPEN`.
#[must_use]
pub fn session_open() -> NaiveTime {
    NaiveTime::from_hms_opt(9, 15, 0).unwrap_or(NaiveTime::MIN)
}

/// `SESSION_CLOSE`.
#[must_use]
pub fn session_close() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 30, 0).unwrap_or(NaiveTime::MIN)
}

/// `entry_plan_id(sleeve, day, symbol, user_id)`: deterministic and per user (FO7.6).
#[must_use]
pub fn entry_plan_id(sleeve: FoSleeve, day: NaiveDate, symbol: &str, user_id: i64) -> String {
    format!(
        "{}-{}-{symbol}-u{user_id}",
        sleeve.value(),
        day.format("%Y%m%d")
    )
}

/// `ExitReason.value`.
#[must_use]
pub const fn reason_value(reason: ExitReason) -> &'static str {
    match reason {
        ExitReason::LateExit => "LATE_EXIT",
        ExitReason::LossClose => "LOSS_CLOSE",
        ExitReason::ProfitTake => "PROFIT_TAKE",
        ExitReason::HardExit => "HARD_EXIT",
        ExitReason::Stop => "STOP",
        ExitReason::TimeExit => "TIME_EXIT",
        ExitReason::Roll => "ROLL",
        ExitReason::NakedFuture => "NAKED_FUTURE",
        ExitReason::LevelBreak => "LEVEL_BREAK",
        ExitReason::LossCut => "LOSS_CUT",
        ExitReason::DecayTarget => "DECAY_TARGET",
    }
}

/// M.5's rule, the one place it is decided: does this exit wait for the click? Only an F3
/// spread's exit can; it waits unless the monitor may send it (`BASKFY_FNO_F3_AUTO_EXIT`).
/// F1's and F2's exits run under their entry's confirm, as they always have.
#[must_use]
pub fn exit_awaits_click(structure_value: &str, exit_by_monitor: bool) -> bool {
    structure_value == structure::CREDIT_SPREAD && !exit_by_monitor
}

/// The status an exit plan is written in: `ISSUED` for the click, `CONFIRMED` under the entry.
#[must_use]
pub const fn exit_status(by_click: bool) -> &'static str {
    if by_click {
        state::ISSUED
    } else {
        state::CONFIRMED
    }
}

/// `exit_by_monitor`: per sleeve, may the monitor send an F3 exit itself? `main()` wires it to
/// `f3_auto_exit_enabled()` (M.5); the default answers no.
pub type ExitByMonitor = dyn Fn(FoSleeve) -> bool + Send + Sync;

/// `_no_monitor_exit`.
#[must_use]
pub const fn no_monitor_exit(_sleeve: FoSleeve) -> bool {
    false
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
    use super::*;

    #[test]
    fn entry_plan_ids_are_deterministic_and_per_user() {
        let day = NaiveDate::from_ymd_opt(2026, 11, 12).unwrap_or_default();
        assert_eq!(
            entry_plan_id(FoSleeve::F3N, day, "NIFTY", 7),
            "F3N-20261112-NIFTY-u7"
        );
        assert_ne!(
            entry_plan_id(FoSleeve::F1N, day, "NIFTY", 1),
            entry_plan_id(FoSleeve::F1N, day, "NIFTY", 2)
        );
    }

    #[test]
    fn only_an_f3_exit_can_wait_for_the_click() {
        // Flag off (the default): the F3 exit waits; F1's and F2's run under the entry.
        assert!(exit_awaits_click(structure::CREDIT_SPREAD, false));
        assert!(!exit_awaits_click(structure::IRON_CONDOR, false));
        assert!(!exit_awaits_click(structure::FUTURE, false));
        // Flag on: the monitor sends the F3 exit itself.
        assert!(!exit_awaits_click(structure::CREDIT_SPREAD, true));
        assert_eq!(exit_status(true), "ISSUED");
        assert_eq!(exit_status(false), "CONFIRMED");
    }

    #[test]
    fn the_default_seam_never_lets_the_monitor_exit() {
        for s in FoSleeve::ALL {
            assert!(!no_monitor_exit(s));
        }
    }
}
