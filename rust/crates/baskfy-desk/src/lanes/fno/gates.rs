//! Ports `app.fno_gates` (FO6): the FO book's switches on the desk, read at the moment they are
//! asked. `LIVE` needs all four (`DRY_RUN` off, `OPTIONS_ENABLED`, `BASKFY_FNO_CARRY_ENABLED`,
//! the sleeve's `BASKFY_FNO_<F1|F2|F3>_EXECUTION_ENABLED`); every other combination is
//! `PAPER`, and in PAPER the gateway is handed `dry_run=true` whatever `DRY_RUN` says.
//! `INTRADAY_ENABLED` is not read: an FO order is NRML, never MIS.
//!
//! **There is no unattended-entry input and none may be added.** [`f3_auto_exit_enabled`] is
//! M.5's unattended-**exit** input, Maulik's (28 Sep 2026), read here and nowhere else; it
//! defaults false and gates nothing but the monitor's sending of an F3 **exit** plan.

use baskfy_core::fno::config::FoSleeve;
use baskfy_core::fno::gating::{FnoFlags, FnoGate};
use baskfy_core::options::gating::Mode;

use crate::gateway::ProductGates;
use crate::settings::Settings;

/// `fno_flags()`.
#[must_use]
pub const fn fno_flags(s: &Settings) -> FnoFlags {
    FnoFlags {
        dry_run: s.dry_run,
        options_enabled: s.options_enabled,
        fno_carry_enabled: s.fno_carry_enabled,
        f1_execution_enabled: s.fno_f1_execution_enabled,
        f2_execution_enabled: s.fno_f2_execution_enabled,
        f3_execution_enabled: s.fno_f3_execution_enabled,
    }
}

/// `fno_gates(sleeve)`: `PAPER` unless all four switches are on for `sleeve`.
#[must_use]
pub fn fno_gates(s: &Settings, sleeve: FoSleeve) -> FnoGate {
    baskfy_core::fno::gating::fno_gates(sleeve, &fno_flags(s))
}

/// `fno_execute.fno_mode(sleeve)` / `fno_desk.mode_of(sleeve)`.
#[must_use]
pub fn mode_of(s: &Settings, sleeve: FoSleeve) -> Mode {
    fno_gates(s, sleeve).mode
}

/// `f3_auto_exit_enabled()`: M.5's flag — may the monitor send an F3 **exit** itself? It gates
/// exits only; an entry or an add is a click whatever it says. Default false.
#[must_use]
pub const fn f3_auto_exit_enabled(s: &Settings) -> bool {
    s.fno_f3_auto_exit
}

/// `product_gates(sleeve)`: the switches this sleeve's gateway is handed, read per order.
/// `intraday_enabled` is false in every row.
#[must_use]
pub fn product_gates(s: &Settings, sleeve: FoSleeve) -> ProductGates {
    let gate = fno_gates(s, sleeve);
    ProductGates {
        dry_run: gate.dry_run,
        intraday_enabled: false,
        options_enabled: gate.options_enabled,
        fno_carry_enabled: gate.fno_carry_enabled,
    }
}

/// `fno_execute.paper_gates(sleeve)`: the sleeve's switches with `dry_run` pinned true (FO7.1).
#[must_use]
pub fn paper_gates(s: &Settings, sleeve: FoSleeve) -> ProductGates {
    ProductGates {
        dry_run: true,
        ..product_gates(s, sleeve)
    }
}

#[cfg(test)]
mod tests {
    //! `tests/test_fno_gates.py` (and `test_fno_safety.py::test_product_gates_never_carry_intraday`).
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use baskfy_core::fno::config::{FoSleeveGroup, group_of};

    use super::*;

    fn settings() -> Settings {
        Settings::defaults().expect("the shipped defaults parse")
    }

    fn set(s: &mut Settings, sleeve: FoSleeve, row: (bool, bool, bool, bool)) {
        let (dry_off, options, carry, execution) = row;
        s.dry_run = !dry_off;
        s.options_enabled = options;
        s.fno_carry_enabled = carry;
        s.fno_f1_execution_enabled = false;
        s.fno_f2_execution_enabled = false;
        s.fno_f3_execution_enabled = false;
        match group_of(sleeve) {
            FoSleeveGroup::F1 => s.fno_f1_execution_enabled = execution,
            FoSleeveGroup::F2 => s.fno_f2_execution_enabled = execution,
            FoSleeveGroup::F3 => s.fno_f3_execution_enabled = execution,
        }
    }

    #[test]
    fn paper_for_every_row_but_all_four() {
        for intraday in [false, true] {
            for sleeve in FoSleeve::ALL {
                for bits in 0..16u8 {
                    let row = (bits & 1 != 0, bits & 2 != 0, bits & 4 != 0, bits & 8 != 0);
                    let mut s = settings();
                    set(&mut s, sleeve, row);
                    s.intraday_enabled = intraday; // must make no difference
                    let all_four = bits == 15;
                    let want = if all_four { Mode::Live } else { Mode::Paper };
                    assert_eq!(mode_of(&s, sleeve), want, "{sleeve:?} {row:?}");
                    let gates = product_gates(&s, sleeve);
                    assert_eq!(gates.dry_run, !all_four, "{sleeve:?} {row:?}");
                    assert!(!gates.intraday_enabled);
                    // FO7.1: the paper gateway is dry-run in every row, LIVE included.
                    assert!(paper_gates(&s, sleeve).dry_run);
                    assert!(!paper_gates(&s, sleeve).intraday_enabled);
                }
            }
        }
    }

    #[test]
    fn every_fo_flag_defaults_false_and_the_shipped_mode_is_paper() {
        let s = settings();
        assert!(!s.fno_carry_enabled);
        assert!(!s.fno_f1_execution_enabled);
        assert!(!s.fno_f2_execution_enabled);
        assert!(!s.fno_f3_execution_enabled);
        assert!(!s.fno_f3_auto_exit);
        assert!(!s.fno_monitor_enabled);
        for sleeve in FoSleeve::ALL {
            assert_eq!(mode_of(&s, sleeve), Mode::Paper);
            assert!(product_gates(&s, sleeve).dry_run);
        }
    }

    #[test]
    fn the_only_fo_auto_setting_is_f3s_exit_flag() {
        // M.5 (Maulik, 28 Sep 2026): one admitted spelling, an exit only. This source is held
        // to the same scan Python's test runs over `fno_gates.py`.
        let text = include_str!("gates.rs").to_uppercase();
        let code = text.split("#[CFG(TEST)]").next().unwrap_or("");
        let stripped = code
            .replace("BASKFY_FNO_F3_AUTO_EXIT", "")
            .replace("FNO_F3_AUTO_EXIT", "")
            .replace("F3_AUTO_EXIT_ENABLED", "")
            .replace("FNO_F3_AUTO_EXIT", "");
        let re = regex::Regex::new(r"BASKFY_FNO_\w*AUTO|\bFNO_\w*AUTO").unwrap();
        assert!(!re.is_match(&stripped));
        assert!(!f3_auto_exit_enabled(&settings()));
        let mut s = settings();
        s.fno_f3_auto_exit = true;
        assert!(f3_auto_exit_enabled(&s));
        // The exit flag changes no gate: still PAPER, still dry-run.
        for sleeve in FoSleeve::ALL {
            assert_eq!(mode_of(&s, sleeve), Mode::Paper);
            assert!(product_gates(&s, sleeve).dry_run);
        }
    }

    #[test]
    fn product_gates_follow_the_settings_read_now() {
        let mut s = settings();
        set(&mut s, FoSleeve::F2, (true, true, true, true));
        let g = product_gates(&s, FoSleeve::F2);
        assert!(!g.dry_run && g.options_enabled && g.fno_carry_enabled && !g.intraday_enabled);
        // Another sleeve's execution switch is not this sleeve's.
        assert!(product_gates(&s, FoSleeve::F1N).dry_run);
        assert!(product_gates(&s, FoSleeve::F3N).dry_run);
    }
}
