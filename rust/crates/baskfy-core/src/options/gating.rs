//! Port of `baskfy_core.options.gating` (`decile-blueprint/packages/core/src/baskfy_core/options/gating.py`).
//!
//! `options_gates(sleeve)` — the one function that ANDs the four switches (docs/options/02 Track B). The flags are passed in as values; this module never reads the environment (law 1).
//!
//! Goldens: `L1/options/options_gates.*` (`baskfy_core.options.gating.options_gates`).
//!
//! A real options order needs **four** flags at once for its sleeve: `DRY_RUN=false`,
//! `OPTIONS_ENABLED`, `INTRADAY_ENABLED` and `BASKFY_OPTIONS_<SLEEVE>_EXECUTION_ENABLED`. Every
//! other combination is `PAPER`. With the sleeve's own execution flag off, the paper gates are
//! handed to the gateway (`dry_run`, `options_enabled` and `intraday_enabled` all true —
//! DECISIONS-OP OP2.3); with it on, the desk's real product switches pass through so a
//! half-flipped configuration is refused by the gateway itself. There is no auto-execute input
//! here and none may be added (PACK.3).
//!

use serde::{Deserialize, Serialize};

pub use crate::options::config::{Mode, Sleeve, SleeveGroup, group_of};

/// Each sleeve group's execution flag env name.
pub const EXECUTION_FLAG_ENV: [(SleeveGroup, &str); 4] = [
    (SleeveGroup::O1m, "BASKFY_OPTIONS_O1M_EXECUTION_ENABLED"),
    (SleeveGroup::O1w, "BASKFY_OPTIONS_O1W_EXECUTION_ENABLED"),
    (SleeveGroup::O2, "BASKFY_OPTIONS_O2_EXECUTION_ENABLED"),
    (SleeveGroup::O3, "BASKFY_OPTIONS_O3_EXECUTION_ENABLED"),
];

/// The three operational flags; they move no money (PACK.11) and default false.
pub const OPERATIONAL_FLAG_ENV: [&str; 3] = [
    "BASKFY_OPTIONS_MONITOR_ENABLED",
    "BASKFY_OPTIONS_COLLECT_ENABLED",
    "BASKFY_OPTIONS_SCAN_ENABLED",
];

/// The desk's two product switches the options book shares.
pub const DESK_SWITCH_ENV: [&str; 2] = ["OPTIONS_ENABLED", "INTRADAY_ENABLED"];

/// The six system-only ceilings, by `OptionsCeilings` field.
pub const CEILING_ENV: [(&str, &str); 6] = [
    (
        "risk_per_trade_inr_max",
        "BASKFY_OPTIONS_RISK_PER_TRADE_INR_MAX",
    ),
    ("risk_pct_max", "BASKFY_OPTIONS_RISK_PCT_MAX"),
    ("max_lots_max", "BASKFY_OPTIONS_MAX_LOTS_MAX"),
    (
        "book_daily_loss_inr_max",
        "BASKFY_OPTIONS_BOOK_DAILY_LOSS_INR_MAX",
    ),
    (
        "book_monthly_loss_inr_max",
        "BASKFY_OPTIONS_BOOK_MONTHLY_LOSS_INR_MAX",
    ),
    ("hard_exit_latest", "BASKFY_OPTIONS_HARD_EXIT_LATEST"),
];

/// The switches as one process read them at startup. Every default is the safe one.
///
/// Ports `baskfy_core.options.gating.OptionsFlags`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct OptionsFlags {
    /// `DRY_RUN` (default true).
    pub dry_run: bool,
    /// `OPTIONS_ENABLED`.
    pub options_enabled: bool,
    /// `INTRADAY_ENABLED`.
    pub intraday_enabled: bool,
    /// `BASKFY_OPTIONS_O1M_EXECUTION_ENABLED`.
    pub o1m_execution_enabled: bool,
    /// `BASKFY_OPTIONS_O1W_EXECUTION_ENABLED`.
    pub o1w_execution_enabled: bool,
    /// `BASKFY_OPTIONS_O2_EXECUTION_ENABLED`.
    pub o2_execution_enabled: bool,
    /// `BASKFY_OPTIONS_O3_EXECUTION_ENABLED` (shared by O3-A and O3-B).
    pub o3_execution_enabled: bool,
}

impl Default for OptionsFlags {
    fn default() -> Self {
        Self {
            dry_run: true,
            options_enabled: false,
            intraday_enabled: false,
            o1m_execution_enabled: false,
            o1w_execution_enabled: false,
            o2_execution_enabled: false,
            o3_execution_enabled: false,
        }
    }
}

impl OptionsFlags {
    /// The sleeve's own execution flag; O3-A and O3-B share `O3`'s.
    pub fn execution_enabled(&self, sleeve: Sleeve) -> bool {
        match group_of(sleeve) {
            SleeveGroup::O1m => self.o1m_execution_enabled,
            SleeveGroup::O1w => self.o1w_execution_enabled,
            SleeveGroup::O2 => self.o2_execution_enabled,
            SleeveGroup::O3 => self.o3_execution_enabled,
        }
    }
}

/// What a sleeve may do right now: its mode and the switches its gateway is handed.
///
/// Ports `baskfy_core.options.gating.OptionsGate`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct OptionsGate {
    /// The sleeve asked about.
    pub sleeve: Sleeve,
    /// `LIVE` iff all four switches are on.
    pub mode: Mode,
    /// The gateway's `dry_run`.
    pub dry_run: bool,
    /// The gateway's `options_enabled`.
    pub options_enabled: bool,
    /// The gateway's `intraday_enabled`.
    pub intraday_enabled: bool,
}

impl OptionsGate {
    /// Whether a confirm now would be journalled `simulated=true`.
    pub fn simulated(&self) -> bool {
        self.mode == Mode::Paper
    }
}

/// `LIVE` iff all four switches are on for `sleeve`; `PAPER` otherwise (`02` §B).
///
/// Ports `baskfy_core.options.gating.options_gates`.
pub fn options_gates(sleeve: Sleeve, flags: &OptionsFlags) -> OptionsGate {
    let execution = flags.execution_enabled(sleeve);
    let live = !flags.dry_run && flags.options_enabled && flags.intraday_enabled && execution;
    let gate = |mode, dry_run, options_enabled, intraday_enabled| OptionsGate {
        sleeve,
        mode,
        dry_run,
        options_enabled,
        intraday_enabled,
    };
    if live {
        return gate(Mode::Live, false, true, true);
    }
    if !execution {
        // The paper gates: dry_run forces the gateway's simulated branch (OP2.3).
        return gate(Mode::Paper, true, true, true);
    }
    gate(
        Mode::Paper,
        true,
        flags.options_enabled,
        flags.intraday_enabled,
    )
}
