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
//! `Sleeve`, `SleeveGroup`, `group_of` and `Mode` belong to `baskfy_core.options.config`; they
//! live here until `options::config` is ported (that file is outside this port's ownership).

use serde::{Deserialize, Serialize};

/// Ports `baskfy_core.options.config.Sleeve` — the five sleeve codes (the `op_sleeve` enum).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum Sleeve {
    /// `O1M` — the monthly condor.
    #[serde(rename = "O1M")]
    O1m,
    /// `O1W` — the weekly condor.
    #[serde(rename = "O1W")]
    O1w,
    /// `O2` — the directional long.
    #[serde(rename = "O2")]
    O2,
    /// `O3A` — expiry setup A.
    #[serde(rename = "O3A")]
    O3a,
    /// `O3B` — expiry setup B.
    #[serde(rename = "O3B")]
    O3b,
}

/// Ports `baskfy_core.options.config.SleeveGroup`: `O3A`/`O3B` are one group, `O3`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum SleeveGroup {
    /// `O1M`.
    #[serde(rename = "O1M")]
    O1m,
    /// `O1W`.
    #[serde(rename = "O1W")]
    O1w,
    /// `O2`.
    #[serde(rename = "O2")]
    O2,
    /// `O3`.
    #[serde(rename = "O3")]
    O3,
}

/// The config/flag group a sleeve code belongs to.
///
/// Ports `baskfy_core.options.config.group_of`.
pub fn group_of(sleeve: Sleeve) -> SleeveGroup {
    match sleeve {
        Sleeve::O1m => SleeveGroup::O1m,
        Sleeve::O1w => SleeveGroup::O1w,
        Sleeve::O2 => SleeveGroup::O2,
        Sleeve::O3a | Sleeve::O3b => SleeveGroup::O3,
    }
}

/// Ports `baskfy_core.options.config.Mode` — fixed by `options_gates()` at 09:15.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum Mode {
    /// Simulated: journalled `simulated=true`, no broker call.
    Paper,
    /// Real orders.
    Live,
}

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
