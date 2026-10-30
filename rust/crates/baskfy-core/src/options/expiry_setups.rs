//! Port of `baskfy_core.options.expiry_setups` (`decile-blueprint/packages/core/src/baskfy_core/options/expiry_setups.py`).
//!
//! O3 — the expiry-day debit spreads, O3-A (range break) and O3-B (gap hold) (docs/options/04 §5).
//!
//! Ported so far: the exits (`04` §5.3) — `HARD_EXIT > STOP > INVALIDATED > TARGET` (`MANUAL`
//! last), the target never taken on a stale mark, §9.2's budget breach as `STOP` — and the two
//! invalidations. The scans and the build are not yet ported.

use chrono::NaiveDateTime;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::CoreResult;
use crate::options::bars::Bar;
use crate::options::condor::{RiskConfig, trade_breached};
use crate::options::config::ExpirySetupsConfig;
use crate::options::costs::mul;
use crate::options::execution::{drop_on_stale, first_by_precedence};
use crate::options::structures::Direction;

/// Ports `baskfy_core.options.expiry_setups.SpreadExit`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SpreadExit {
    /// The clock.
    HardExit,
    /// `V <= stop_frac_of_debit * D`, or the budget breach.
    Stop,
    /// The setup's invalidation.
    Invalidated,
    /// `V >= target_frac_of_width * width`.
    Target,
    /// The operator's button.
    Manual,
}

impl SpreadExit {
    /// `.value`.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::HardExit => "HARD_EXIT",
            Self::Stop => "STOP",
            Self::Invalidated => "INVALIDATED",
            Self::Target => "TARGET",
            Self::Manual => "MANUAL",
        }
    }
}

/// `04` §5.3's precedence.
pub const PRECEDENCE: [SpreadExit; 5] = [
    SpreadExit::HardExit,
    SpreadExit::Stop,
    SpreadExit::Invalidated,
    SpreadExit::Target,
    SpreadExit::Manual,
];

/// `close_value(long_bid, short_ask)`: `V = long.bid - short.ask`.
#[must_use]
pub fn close_value(long_bid: Decimal, short_ask: Decimal) -> Decimal {
    long_bid - short_ask
}

/// O3-A: a completed 5-minute close back inside the morning range.
#[must_use]
pub fn range_break_invalidated(
    five_minute_close: Option<Decimal>,
    direction: Direction,
    high: Decimal,
    low: Decimal,
) -> bool {
    match five_minute_close {
        None => false,
        Some(close) => match direction {
            Direction::Up => close < high,
            Direction::Down => close > low,
        },
    }
}

/// O3-B: any bar through `half_gap` (a low at or below it on an up-gap, a high at or above it
/// on a down-gap).
#[must_use]
pub fn gap_hold_invalidated(bar: Option<&Bar>, direction: Direction, half_gap: Decimal) -> bool {
    match bar {
        None => false,
        Some(b) => match direction {
            Direction::Up => b.low <= half_gap,
            Direction::Down => b.high >= half_gap,
        },
    }
}

/// One tick of the spread — the keyword arguments of Python's `exit_decision` other than the
/// configs. `now` is IST wall-clock.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SpreadTick {
    /// `V`, what closing receives now, points.
    pub value: Decimal,
    /// `D`, the entry debit from fills.
    pub entry_debit: Decimal,
    /// The spread's width.
    pub width: Decimal,
    /// The invalidation fired.
    pub is_invalidated: bool,
    /// The tick's time.
    pub now: NaiveDateTime,
    /// The mark is stale.
    pub stale: bool,
    /// The marked loss, ₹, a loss positive.
    pub marked_loss_inr: Decimal,
    /// The trade's risk budget, ₹.
    pub risk_budget_inr: Decimal,
    /// The operator pressed close.
    pub manual: bool,
}

/// One tick's exit for the spread, or `None` (`04` §5.3).
///
/// # Errors
/// Decimal overflow.
///
/// Ports `baskfy_core.options.expiry_setups.exit_decision` (`options` narrowed to its `risk`).
pub fn exit_decision(
    tick: &SpreadTick,
    config: &ExpirySetupsConfig,
    risk: &RiskConfig,
) -> CoreResult<Option<SpreadExit>> {
    let mut fired: Vec<SpreadExit> = Vec::new();
    if tick.now.time() >= config.hard_exit_time {
        fired.push(SpreadExit::HardExit);
    }
    if tick.value <= mul(config.stop_frac_of_debit, tick.entry_debit)?
        || trade_breached(tick.marked_loss_inr, tick.risk_budget_inr, risk)?
    {
        fired.push(SpreadExit::Stop);
    }
    if tick.is_invalidated {
        fired.push(SpreadExit::Invalidated);
    }
    if tick.value >= mul(config.target_frac_of_width, tick.width)? {
        fired.push(SpreadExit::Target);
    }
    if tick.manual {
        fired.push(SpreadExit::Manual);
    }
    let kept = drop_on_stale(&fired, &[SpreadExit::Target], tick.stale);
    first_by_precedence(&kept, &PRECEDENCE)
}
