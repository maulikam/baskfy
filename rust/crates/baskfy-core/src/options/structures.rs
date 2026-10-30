//! Port of `baskfy_core.options.structures`' two enums (`decile-blueprint/packages/core/src/baskfy_core/options/structures.py`).
//!
//! Ported so far: `Structure` (`03` §9's `op_plan.structure`) and `Direction`, which the exits
//! read. The candidates, the rejections and the builders are not yet ported.

use serde::{Deserialize, Serialize};

/// Ports `baskfy_core.options.structures.Structure`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Structure {
    /// `IRON_CONDOR` (O1).
    IronCondor,
    /// `LONG_OPTION` (O2).
    LongOption,
    /// `DEBIT_SPREAD` (O3).
    DebitSpread,
}

impl Structure {
    /// `Structure(text)`; `None` where Python raises `ValueError`.
    #[must_use]
    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "IRON_CONDOR" => Some(Self::IronCondor),
            "LONG_OPTION" => Some(Self::LongOption),
            "DEBIT_SPREAD" => Some(Self::DebitSpread),
            _ => None,
        }
    }

    /// `.value`.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::IronCondor => "IRON_CONDOR",
            Self::LongOption => "LONG_OPTION",
            Self::DebitSpread => "DEBIT_SPREAD",
        }
    }
}

/// Ports `baskfy_core.options.structures.Direction`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum Direction {
    /// `UP`.
    Up,
    /// `DOWN`.
    Down,
}

impl Direction {
    /// `Direction(text)`; `None` where Python raises `ValueError`.
    #[must_use]
    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "UP" => Some(Self::Up),
            "DOWN" => Some(Self::Down),
            _ => None,
        }
    }

    /// `.value`.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Up => "UP",
            Self::Down => "DOWN",
        }
    }
}
