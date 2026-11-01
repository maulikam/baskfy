//! Lane **swing_monitor** (parent `swing`): `app/swing_monitor.py`, `app/swing_clock.py` and
//! `app/swing_notify.py`.
//!
//! * [`runner`] — the opening-range monitor's runner: the flag (nothing is built with
//!   `BASKFY_SWING_MONITOR_ENABLED` off), the signal store, the notice to the daily focus, the
//!   quote fallback's cap, the late-login wait, the chores at their own hours, the reload, the
//!   heartbeat, `monitor_ran`, and SW25's auto-execute queue — **drained every pass, sent only
//!   when `DRY_RUN=false`, `BASKFY_SWING_EXECUTION_ENABLED=true` and
//!   `BASKFY_SWING_AUTO_EXECUTE=true` all hold** (each defaults to the safe side; flipping them
//!   is Maulik's hand). The monitor confirms through an [`runner::AutoConfirm`] the desk wires
//!   to the same `execute_line` a click reaches; nothing here names a placing verb.
//! * [`clock`] — the 10:45 cutoff and the 15:15 GTT sweep, through the swing book's gateway
//!   (`execute::cutoff_open_orders` / `eod_gtt_sweep`), `run_chore` and the backstop
//!   `run_after_close`. Clocks and sleeps are injected.
//! * [`service`] — `swing_monitor.main()`: `baskfy-desk service swing-monitor`, wired to the
//!   desk (its store, the swing gateway, `execute_line` as [`service::DeskAutoConfirm`], the
//!   chores' seams, the quote fallback, the gated heartbeat), and the container scheduler's
//!   `next_run`.
//! * [`harness`] — `POST /__harness/swing-signal`, harness-only, so `desk_diff.py --plans`
//!   grades the SIGNAL plan kind against Python's.
//! * The notifier is [`super::notify`] (declared by the parent before the split; this lane owns
//!   its content and tests it here): SMTP (STARTTLS, AUTH) and Telegram's `sendMessage`.

/// One log line on stderr, Python's `logging` format reduced to `LEVEL logger message`.
macro_rules! log_line {
    ($($arg:tt)*) => {
        $crate::lanes::swing::swing_monitor::emit(&::std::format!($($arg)*))
    };
}

/// Where [`log_line!`] writes: stderr, the desk process's log (never a secret — every line is
/// composed here from symbols, counts and error words).
#[allow(clippy::print_stderr)]
pub fn emit(line: &str) {
    eprintln!("{line}");
}

pub mod clock;
pub mod harness;
pub mod runner;
pub mod service;

pub use super::notify;

#[cfg(test)]
mod tests;
#[cfg(test)]
mod tests_clock;
#[cfg(test)]
mod tests_notify;
#[cfg(test)]
mod tests_service;
#[cfg(test)]
mod tests_store;
#[cfg(test)]
mod tests_strategy;
