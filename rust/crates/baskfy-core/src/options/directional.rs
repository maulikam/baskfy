//! Port of `baskfy_core.options.directional` (`decile-blueprint/packages/core/src/baskfy_core/options/directional.py`).
//!
//! O2 — the directional long (docs/options/04 §4).
//!
//! Ported so far: the exits (`04` §4.5) — `HARD_EXIT > STOP > INVALIDATED > TARGET > TIME_STOP`
//! (`MANUAL` last), the target never taken on a stale mark, and §9.2's budget breach as
//! `STOP`. The day filters, the trigger and the build are not yet ported.

use chrono::{Duration, NaiveDateTime};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::CoreResult;
use crate::options::condor::{RiskConfig, trade_breached};
use crate::options::config::DirectionalConfig;
use crate::options::costs::{add, mul, sub};
use crate::options::execution::{drop_on_stale, first_by_precedence};
use crate::options::structures::Direction;

/// Ports `baskfy_core.options.directional.LongExit`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum LongExit {
    /// The clock.
    HardExit,
    /// `bid <= E * (1 - stop_frac)`, or the budget breach.
    Stop,
    /// A 5-minute close back inside the opening range.
    Invalidated,
    /// `bid >= E * (1 + target_frac)`.
    Target,
    /// Held long enough without the gain.
    TimeStop,
    /// The operator's button.
    Manual,
}

impl LongExit {
    /// `.value`.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::HardExit => "HARD_EXIT",
            Self::Stop => "STOP",
            Self::Invalidated => "INVALIDATED",
            Self::Target => "TARGET",
            Self::TimeStop => "TIME_STOP",
            Self::Manual => "MANUAL",
        }
    }
}

/// `04` §4.5's precedence.
pub const PRECEDENCE: [LongExit; 6] = [
    LongExit::HardExit,
    LongExit::Stop,
    LongExit::Invalidated,
    LongExit::Target,
    LongExit::TimeStop,
    LongExit::Manual,
];

/// `invalidated(five_minute_close, direction, or_high, or_low)`: a completed 5-minute index
/// close back inside the opening range — `< or_high` for a call, `> or_low` for a put.
#[must_use]
pub fn invalidated(
    five_minute_close: Option<Decimal>,
    direction: Direction,
    or_high: Decimal,
    or_low: Decimal,
) -> bool {
    match five_minute_close {
        None => false,
        Some(close) => match direction {
            Direction::Up => close < or_high,
            Direction::Down => close > or_low,
        },
    }
}

/// One tick of the long — the keyword arguments of Python's `exit_decision` other than the
/// configs. Times are IST wall-clock.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LongTick {
    /// `E`, the entry fill.
    pub entry: Decimal,
    /// The option's bid (the mark).
    pub bid: Decimal,
    /// When it was entered.
    pub entered_at: NaiveDateTime,
    /// The tick's time.
    pub now: NaiveDateTime,
    /// The invalidation fired.
    pub is_invalidated: bool,
    /// The mark is stale.
    pub stale: bool,
    /// The marked loss, ₹, a loss positive.
    pub marked_loss_inr: Decimal,
    /// The trade's risk budget, ₹.
    pub risk_budget_inr: Decimal,
    /// The operator pressed close.
    pub manual: bool,
}

/// One tick's exit for the long, or `None` (`04` §4.5).
///
/// # Errors
/// Decimal overflow.
///
/// Ports `baskfy_core.options.directional.exit_decision` (`options` narrowed to its `risk`).
pub fn exit_decision(
    tick: &LongTick,
    config: &DirectionalConfig,
    risk: &RiskConfig,
) -> CoreResult<Option<LongExit>> {
    let mut fired: Vec<LongExit> = Vec::new();
    if tick.now.time() >= config.hard_exit_time {
        fired.push(LongExit::HardExit);
    }
    if tick.bid <= mul(tick.entry, sub(Decimal::ONE, config.stop_frac)?)?
        || trade_breached(tick.marked_loss_inr, tick.risk_budget_inr, risk)?
    {
        fired.push(LongExit::Stop);
    }
    if tick.is_invalidated {
        fired.push(LongExit::Invalidated);
    }
    if tick.bid >= mul(tick.entry, add(Decimal::ONE, config.target_frac)?)? {
        fired.push(LongExit::Target);
    }
    let held = tick.now - tick.entered_at;
    if held >= Duration::minutes(config.time_stop_minutes)
        && tick.bid < mul(tick.entry, add(Decimal::ONE, config.time_stop_min_gain)?)?
    {
        fired.push(LongExit::TimeStop);
    }
    if tick.manual {
        fired.push(LongExit::Manual);
    }
    let kept = drop_on_stale(&fired, &[LongExit::Target], tick.stale);
    first_by_precedence(&kept, &PRECEDENCE)
}
