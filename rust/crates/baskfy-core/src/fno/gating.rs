//! Port of `baskfy_core.fno.gating` (`decile-blueprint/packages/core/src/baskfy_core/fno/gating.py`).
//!
//! `fno_gates(sleeve)` — the one function that ANDs the FO run's four switches (`02` Track B).
//! A real FO order needs **four** flags at once for its sleeve: `DRY_RUN=false`,
//! `OPTIONS_ENABLED`, `BASKFY_FNO_CARRY_ENABLED` and `BASKFY_FNO_<F1|F2|F3>_EXECUTION_ENABLED`.
//! Every other combination — fifteen of sixteen, per sleeve — is `PAPER`. `INTRADAY_ENABLED` is
//! not one of them and must not become one: the gate carries `intraday_enabled = false` in every
//! row. Non-negotiable #1's third exception (F3's auto-exit, M.5) reads this gate, so it is
//! reproduced exactly and no default here opens it: every [`FnoFlags`] default is the safe one.
//!
//! There is no unattended-entry input here and none may be added (`02` Track B and Track C §3).
//!
//! Goldens: `L1/fno/gating.{fno_gates,FnoFlags.execution_enabled,constants}.case_NNN.json`.

use serde::{Deserialize, Serialize, Serializer, ser::SerializeStruct};

use super::config::{FoSleeve, FoSleeveGroup, group_of};
use crate::options::gating::Mode;

/// The env var behind each sleeve group's execution flag (`02` Track B), system-only.
///
/// Ports `baskfy_core.fno.gating.EXECUTION_FLAG_ENV`.
pub const EXECUTION_FLAG_ENV: [(FoSleeveGroup, &str); 3] = [
    (FoSleeveGroup::F1, "BASKFY_FNO_F1_EXECUTION_ENABLED"),
    (FoSleeveGroup::F2, "BASKFY_FNO_F2_EXECUTION_ENABLED"),
    (FoSleeveGroup::F3, "BASKFY_FNO_F3_EXECUTION_ENABLED"),
];

/// The carry switch: NRML on NFO for `fo_plan` orders, and F2's GTT (`02` §1). A LOCKED_KEY.
///
/// Ports `baskfy_core.fno.gating.CARRY_FLAG_ENV`.
pub const CARRY_FLAG_ENV: &str = "BASKFY_FNO_CARRY_ENABLED";

/// The operational flags — they move no money, and stay false by default.
///
/// Ports `baskfy_core.fno.gating.OPERATIONAL_FLAG_ENV`.
pub const OPERATIONAL_FLAG_ENV: [&str; 2] =
    ["BASKFY_FNO_SCAN_ENABLED", "BASKFY_FNO_MONITOR_ENABLED"];

/// The desk's existing switch the FO book shares. `INTRADAY_ENABLED` is deliberately absent.
///
/// Ports `baskfy_core.fno.gating.DESK_SWITCH_ENV`.
pub const DESK_SWITCH_ENV: [&str; 1] = ["OPTIONS_ENABLED"];

/// The switches as one process read them. Every default is the safe one (`dry_run` true,
/// everything else false); a flag missing from the input takes its default.
///
/// Ports `baskfy_core.fno.gating.FnoFlags`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct FnoFlags {
    /// `DRY_RUN` (default true).
    pub dry_run: bool,
    /// `OPTIONS_ENABLED`.
    pub options_enabled: bool,
    /// `BASKFY_FNO_CARRY_ENABLED`.
    pub fno_carry_enabled: bool,
    /// `BASKFY_FNO_F1_EXECUTION_ENABLED`.
    pub f1_execution_enabled: bool,
    /// `BASKFY_FNO_F2_EXECUTION_ENABLED`.
    pub f2_execution_enabled: bool,
    /// `BASKFY_FNO_F3_EXECUTION_ENABLED`.
    pub f3_execution_enabled: bool,
}

impl Default for FnoFlags {
    fn default() -> Self {
        Self {
            dry_run: true,
            options_enabled: false,
            fno_carry_enabled: false,
            f1_execution_enabled: false,
            f2_execution_enabled: false,
            f3_execution_enabled: false,
        }
    }
}

impl FnoFlags {
    /// The sleeve's own execution flag; `F1N`/`F1B` share `F1`'s, `F3N`/`F3B` `F3`'s.
    ///
    /// Ports `baskfy_core.fno.gating.FnoFlags.execution_enabled`.
    #[must_use]
    pub fn execution_enabled(&self, sleeve: FoSleeve) -> bool {
        match group_of(sleeve) {
            FoSleeveGroup::F2 => self.f2_execution_enabled,
            FoSleeveGroup::F3 => self.f3_execution_enabled,
            FoSleeveGroup::F1 => self.f1_execution_enabled,
        }
    }
}

/// What a sleeve may do right now: its mode and the switches its gateway is handed.
///
/// Ports `baskfy_core.fno.gating.FnoGate`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FnoGate {
    /// The sleeve.
    pub sleeve: FoSleeve,
    /// `LIVE` iff all four switches are on.
    pub mode: Mode,
    /// The gateway's `dry_run`.
    pub dry_run: bool,
    /// The gateway's `options_enabled`.
    pub options_enabled: bool,
    /// The gateway's `fno_carry_enabled`.
    pub fno_carry_enabled: bool,
    /// Always `false`: an FO order is NRML, never MIS (`02` Track B).
    pub intraday_enabled: bool,
}

impl FnoGate {
    /// Whether a confirm now would be journalled `simulated=true`.
    ///
    /// Ports `baskfy_core.fno.gating.FnoGate.simulated`.
    #[must_use]
    pub fn simulated(&self) -> bool {
        self.mode == Mode::Paper
    }
}

impl Serialize for FnoGate {
    /// The fields plus the `simulated` property, as the golden encodes the dataclass.
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut s = serializer.serialize_struct("FnoGate", 7)?;
        s.serialize_field("sleeve", &self.sleeve)?;
        s.serialize_field("mode", &self.mode)?;
        s.serialize_field("dry_run", &self.dry_run)?;
        s.serialize_field("options_enabled", &self.options_enabled)?;
        s.serialize_field("fno_carry_enabled", &self.fno_carry_enabled)?;
        s.serialize_field("intraday_enabled", &self.intraday_enabled)?;
        s.serialize_field("simulated", &self.simulated())?;
        s.end()
    }
}

/// `LIVE` iff all four switches are on for `sleeve`; `PAPER` otherwise (`02` §B).
///
/// * all four on: `LIVE`, `dry_run=false`, both derivative switches on;
/// * the sleeve's execution flag off: the paper gates — `dry_run=true` with both derivative
///   switches on, so a paper confirm runs the real gateway path and is simulated whatever
///   `DRY_RUN` says;
/// * the execution flag on but not all four: `dry_run=true` and the real `OPTIONS_ENABLED` /
///   `BASKFY_FNO_CARRY_ENABLED` pass through, so a half-flipped configuration is refused by the
///   gateway rather than quietly simulated.
///
/// Ports `baskfy_core.fno.gating.fno_gates`.
#[must_use]
pub fn fno_gates(sleeve: FoSleeve, flags: &FnoFlags) -> FnoGate {
    let execution = flags.execution_enabled(sleeve);
    let live = !flags.dry_run && flags.options_enabled && flags.fno_carry_enabled && execution;
    let gate = |mode, dry_run, options_enabled, fno_carry_enabled| FnoGate {
        sleeve,
        mode,
        dry_run,
        options_enabled,
        fno_carry_enabled,
        intraday_enabled: false,
    };
    if live {
        return gate(Mode::Live, false, true, true);
    }
    if !execution {
        // The paper gates: dry_run forces the gateway's simulated branch whatever DRY_RUN says.
        return gate(Mode::Paper, true, true, true);
    }
    gate(
        Mode::Paper,
        true,
        flags.options_enabled,
        flags.fno_carry_enabled,
    )
}
