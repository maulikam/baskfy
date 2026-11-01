//! Ports `app.options_gates` (OP2) and `app.options_lab` (OP13).
//!
//! `options_gates(sleeve)` reads the desk's switches — `DRY_RUN`, `OPTIONS_ENABLED`,
//! `INTRADAY_ENABLED`, `BASKFY_OPTIONS_<SLEEVE>_EXECUTION_ENABLED` — **at the moment it is
//! called** and hands them to `baskfy_core::options::gating::options_gates`, the one function
//! that ANDs them. `LIVE` needs all four; every other combination is `PAPER`, and in PAPER the
//! gateway is handed `dry_run=true` (non-negotiable 1). There is no auto-execute input and none
//! may be added (PACK.3).

use std::path::Path;

use baskfy_core::options::gating::{Mode, OptionsFlags, OptionsGate, Sleeve};

use crate::gateway::ProductGates;
use crate::settings::Settings;

/// `options_flags()`: the desk's switches, read off the settings now.
#[must_use]
pub const fn options_flags(s: &Settings) -> OptionsFlags {
    OptionsFlags {
        dry_run: s.dry_run,
        options_enabled: s.options_enabled,
        intraday_enabled: s.intraday_enabled,
        o1m_execution_enabled: s.options_o1m_execution_enabled,
        o1w_execution_enabled: s.options_o1w_execution_enabled,
        o2_execution_enabled: s.options_o2_execution_enabled,
        o3_execution_enabled: s.options_o3_execution_enabled,
    }
}

/// `options_gates(sleeve)`: `PAPER` unless all four switches are on for `sleeve`.
#[must_use]
pub fn options_gates(s: &Settings, sleeve: Sleeve) -> OptionsGate {
    baskfy_core::options::gating::options_gates(sleeve, &options_flags(s))
}

/// `mode_of(sleeve)` (`options_desk.mode_of`).
#[must_use]
pub fn mode_of(s: &Settings, sleeve: Sleeve) -> Mode {
    options_gates(s, sleeve).mode
}

/// `product_gates(sleeve)`: the switches this sleeve's gateway is handed, read per order.
#[must_use]
pub fn product_gates(s: &Settings, sleeve: Sleeve) -> ProductGates {
    let gate = options_gates(s, sleeve);
    ProductGates {
        dry_run: gate.dry_run,
        intraday_enabled: gate.intraday_enabled,
        options_enabled: gate.options_enabled,
        fno_carry_enabled: false,
    }
}

/// `options_lab.lab_present(root)`: both halves of the frozen lab in the live tree — the
/// package `app/strategies/strangle/book.py` and its runner `scripts/strangle.py`. The lab is
/// frozen in `frozen/strangle/` (M6), so this is false in every shipped tree; thawing it is a
/// `git mv` and nothing else (OP13.1).
#[must_use]
pub fn lab_present(root: &Path) -> bool {
    let package = root.join("app").join("strategies").join("strangle");
    let module = package.join("book.py").is_file() || package.join("book").is_dir();
    module
        && package.join("__init__.py").is_file()
        && root.join("scripts").join("strangle.py").exists()
}

/// `options_lab.lab_enabled()`: `OPTIONS_ENABLED` **and** the lab thawed.
#[must_use]
pub fn lab_enabled(s: &Settings, root: &Path) -> bool {
    s.options_enabled && lab_present(root)
}
