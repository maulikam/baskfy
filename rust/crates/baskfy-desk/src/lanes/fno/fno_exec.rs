//! Sub-lane **fno_exec** of lane `fno`: ports `app/fno_execute.py` and the order-facing half of
//! `app/fno_monitor.py` (DECISIONS-RS R8.0, R8.5).
//!
//! * [`store`] — `fno_execute.FoStore` and its rows over the `fo_` tables (schema `public`).
//! * the gates are the fno lane's [`super::gates`] (`paper_gates`, dry_run pinned true, FO7.1;
//!   M.5's `f3_auto_exit_enabled`); the tests below hold the two this sub-lane relies on.
//! * [`execute`] — the confirm (`execute_entry`), the click on an F3 exit
//!   (`execute_click_exit`), the exits and rolls (`execute_exit`, `execute_roll`,
//!   `run_pending`), F2's GTT stop (`place/move/delete_future_stop`) and `fo_gateway`.
//! * [`monitor`] — the whole of `app.fno_monitor`: the 09:20 plans, the watch, the exits and
//!   rolls (M.5's by-click rule), the tick's lapse-and-send pass and the night's marks.
//!
//! **Non-negotiables held here.** No route: `POST /fno/execute` is the fno desk's and calls
//! [`execute::execute_entry`] / [`execute::execute_click_exit`], which refuse without
//! `confirm=true` and an `ISSUED` plan inside its 30 minutes. LIVE is refused
//! (`LIVE_NOT_BUILT`). `BASKFY_FNO_F3_AUTO_EXIT` defaults false and decides one thing — whether
//! an F3 **exit** waits for the click; no entry or add is ever sent by a pass. The derived
//! gateway's gates are `paper_gates` read per order: `dry_run` is true in every row (FO7.1), and
//! `OPTIONS_ENABLED` (default off) is enforced in the gateway for a sleeve whose execution
//! switch is on without it. No auto-execute flag is added.

pub mod execute;
pub mod monitor;
pub mod store;

#[cfg(test)]
mod safety_tests;

#[cfg(test)]
mod tests {
    //! The two gate facts this sub-lane's order path relies on (the fno lane's own gate tests
    //! live beside `super::gates`).
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
    use baskfy_core::fno::config::FoSleeve;
    use baskfy_core::options::gating::Mode;

    use crate::lanes::fno::gates::{f3_auto_exit_enabled, mode_of, paper_gates};
    use crate::settings::Settings;

    fn with(pairs: &[(&str, &str)]) -> Settings {
        let owned: Vec<(String, String)> = pairs
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect();
        let lookup = move |k: &str| owned.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone());
        Settings::from_lookup(&lookup).unwrap_or_else(|e| panic!("settings: {e}"))
    }

    #[test]
    fn the_f3_auto_exit_flag_and_options_default_off() {
        let s = with(&[]);
        assert!(!f3_auto_exit_enabled(&s));
        assert!(!s.options_enabled);
        assert!(!s.fno_carry_enabled);
        assert!(s.dry_run);
        for sleeve in FoSleeve::ALL {
            assert_eq!(mode_of(&s, sleeve), Mode::Paper);
            let g = paper_gates(&s, sleeve);
            assert!(g.dry_run, "PAPER: every leg simulated");
            assert!(!g.intraday_enabled, "an FO order is NRML, never MIS");
        }
    }

    #[test]
    fn the_flag_follows_pythons_lower_equals_true() {
        // `os.getenv("BASKFY_FNO_F3_AUTO_EXIT", "false").lower() == "true"`.
        for v in ["true", "True", "TRUE"] {
            assert!(
                f3_auto_exit_enabled(&with(&[("BASKFY_FNO_F3_AUTO_EXIT", v)])),
                "{v:?}"
            );
        }
        for v in ["1", "yes", "on", " true", "true ", "", "false"] {
            assert!(
                !f3_auto_exit_enabled(&with(&[("BASKFY_FNO_F3_AUTO_EXIT", v)])),
                "{v:?}"
            );
        }
    }

    #[test]
    fn the_flag_changes_no_gate() {
        let on = with(&[("BASKFY_FNO_F3_AUTO_EXIT", "true")]);
        let off = with(&[]);
        for sleeve in FoSleeve::ALL {
            assert_eq!(mode_of(&on, sleeve), mode_of(&off, sleeve));
            assert_eq!(paper_gates(&on, sleeve), paper_gates(&off, sleeve));
        }
    }

    #[test]
    fn paper_is_pinned_and_options_enabled_is_the_gateways_when_half_flipped() {
        let all = with(&[
            ("DRY_RUN", "false"),
            ("OPTIONS_ENABLED", "true"),
            ("BASKFY_FNO_CARRY_ENABLED", "true"),
            ("BASKFY_FNO_F3_EXECUTION_ENABLED", "true"),
            ("INTRADAY_ENABLED", "true"),
        ]);
        assert_eq!(mode_of(&all, FoSleeve::F3N), Mode::Live);
        let g = paper_gates(&all, FoSleeve::F3N);
        assert!(
            g.dry_run,
            "FO7.1: the FO gateway always simulates, LIVE included"
        );
        assert!(!g.intraday_enabled);
        // The sleeve's execution switch on without OPTIONS_ENABLED: the real (off) switch passes
        // through, so the gateway refuses the NFO leg rather than quietly simulating it.
        let half = with(&[
            ("DRY_RUN", "false"),
            ("BASKFY_FNO_CARRY_ENABLED", "true"),
            ("BASKFY_FNO_F3_EXECUTION_ENABLED", "true"),
        ]);
        assert_eq!(mode_of(&half, FoSleeve::F3N), Mode::Paper);
        let h = paper_gates(&half, FoSleeve::F3N);
        assert!(h.dry_run && !h.options_enabled);
    }
}
