//! Port of `baskfy_core.options.execution` (`decile-blueprint/packages/core/src/baskfy_core/options/execution.py`).
//!
//! The execution mechanics the core decides and the desk sends (docs/options/04 §8). Pure decisions only — law 2: nothing here sends an order.
//!
//! Goldens: `L1/options/never_naked.*` and `L1/options/simulate_fill.*` (`baskfy_core.options.execution.never_naked`, `.simulate_fill`).
//!
//! Ported so far: the leg roles, `never_naked` (Track C §2), the depth-ladder paper fill
//! (§8.4), and the exit plumbing the condor's exits use (`first_by_precedence`,
//! `drop_on_stale`, §8.5). The sequencing reducers, attempts, feed loss and the expiry-day slot
//! are not yet ported.
//!
//! `ExecutionConfig` belongs to `baskfy_core.options.config` and `Level` to
//! `baskfy_core.options.chain`; they live here until those modules are ported.

use std::collections::BTreeMap;

use chrono::NaiveTime;
use rust_decimal::{Decimal, RoundingStrategy};
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::options::costs::{Side, add, mul, py_div, py_div_quantize};

/// Ports `baskfy_core.options.config.ExecutionConfig` — `04` §8's shared mechanics.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct ExecutionConfig {
    /// Ticks a first attempt improves on the quoted side.
    pub limit_improve_ticks: i64,
    /// Seconds each attempt waits for a fill.
    pub fill_wait_seconds: i64,
    /// Whether a risk-reducing close gets a third, marketable LIMIT at the touch.
    pub exit_final_marketable: bool,
    /// Ticks worse each level past the first fills at in a paper fill (OP1.5).
    pub latency_ticks: i64,
    /// Minutes a plan lives.
    pub plan_ttl_minutes: i64,
    /// `04` §8.5: O1/O3 lose the feed only after this time.
    pub feed_watch_from: NaiveTime,
    /// `04` §8.5: O2 loses the feed only once a position is this old.
    pub feed_grace_seconds: i64,
}

impl Default for ExecutionConfig {
    fn default() -> Self {
        Self {
            limit_improve_ticks: 1,
            fill_wait_seconds: 20,
            exit_final_marketable: true,
            latency_ticks: 1,
            plan_ttl_minutes: 30,
            feed_watch_from: NaiveTime::from_hms_opt(14, 0, 0).unwrap_or_default(),
            feed_grace_seconds: 60,
        }
    }
}

/// One depth level: a price and the quantity resting there (units, not lots).
///
/// Ports `baskfy_core.options.chain.Level`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Level {
    /// Price at this level.
    pub price: Decimal,
    /// Units resting there.
    pub quantity: i64,
}

/// Ports `baskfy_core.options.execution.LegRole`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum LegRole {
    /// `LONG_CALL`.
    LongCall,
    /// `LONG_PUT`.
    LongPut,
    /// `SHORT_CALL`.
    ShortCall,
    /// `SHORT_PUT`.
    ShortPut,
}

impl LegRole {
    /// Whether the leg is bought to open.
    pub fn is_long(self) -> bool {
        matches!(self, LegRole::LongCall | LegRole::LongPut)
    }

    /// The side that opens the leg.
    pub fn entry_side(self) -> Side {
        if self.is_long() {
            Side::Buy
        } else {
            Side::Sell
        }
    }

    /// The side that closes the leg.
    pub fn exit_side(self) -> Side {
        if self.is_long() {
            Side::Sell
        } else {
            Side::Buy
        }
    }

    /// The long that protects a short (same option type); `None` for a long.
    pub fn protector(self) -> Option<LegRole> {
        match self {
            LegRole::ShortCall => Some(LegRole::LongCall),
            LegRole::ShortPut => Some(LegRole::LongPut),
            LegRole::LongCall | LegRole::LongPut => None,
        }
    }
}

/// Short quantity ≤ long quantity on each side, the property Track C §2 demands.
///
/// Ports `baskfy_core.options.execution.never_naked`.
pub fn never_naked(position: &BTreeMap<LegRole, i64>) -> bool {
    let qty = |role| position.get(&role).copied().unwrap_or(0);
    qty(LegRole::ShortCall) <= qty(LegRole::LongCall)
        && qty(LegRole::ShortPut) <= qty(LegRole::LongPut)
}

/// Ports `baskfy_core.options.execution.SimMethod`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SimMethod {
    /// The depth-ladder walk of §8.4.
    DepthLadder,
}

/// A paper fill: what the ladder gave at or inside the limit. `filled < requested` is a
/// partial; `filled == 0` means nothing was available at the limit.
///
/// Ports `baskfy_core.options.execution.SimFill`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SimFill {
    /// The order's side.
    pub side: Side,
    /// Units asked for.
    pub requested: i64,
    /// Units filled.
    pub filled: i64,
    /// Average fill price; `None` when nothing filled.
    pub avg_price: Option<Decimal>,
    /// Depth levels the walk took from.
    pub levels_consumed: i64,
    /// How the fill was simulated.
    pub method: SimMethod,
}

impl SimFill {
    /// Whether the whole order filled.
    pub fn complete(&self) -> bool {
        self.filled == self.requested
    }
}

/// §8.4: walk the side the order takes (`ladder` = the asks for a buy, the bids for a sell),
/// level by level, up to `limit_price`.
///
/// Each level beyond the first fills `latency_ticks` worse than its quoted price (OP1.5); a
/// level whose effective price is beyond the limit stops the walk, and what the ladder could
/// not fill at the limit is a partial. There is no LTP or mid path: an empty ladder fills
/// nothing. Levels with a non-positive price or quantity are skipped and do not count as "the
/// first".
///
/// Ports `baskfy_core.options.execution.simulate_fill`.
pub fn simulate_fill(
    side: Side,
    ladder: &[Level],
    quantity: i64,
    limit_price: Decimal,
    tick: Decimal,
    config: &ExecutionConfig,
) -> CoreResult<SimFill> {
    if quantity <= 0 {
        return Err(CoreError::InvalidInput(
            "quantity must be positive".to_string(),
        ));
    }
    let sign = match side {
        Side::Buy => Decimal::ONE,
        Side::Sell => Decimal::NEGATIVE_ONE,
    };
    let adverse = mul(mul(tick, Decimal::from(config.latency_ticks))?, sign)?;
    let mut remaining = quantity;
    let mut cost = Decimal::ZERO;
    let mut consumed: i64 = 0;
    let usable = ladder
        .iter()
        .filter(|lv| lv.price > Decimal::ZERO && lv.quantity > 0);
    for (index, level) in usable.enumerate() {
        let price = if index > 0 {
            add(level.price, adverse)?
        } else {
            level.price
        };
        let beyond = match side {
            Side::Buy => price > limit_price,
            Side::Sell => price < limit_price,
        };
        if beyond {
            break;
        }
        let take = remaining.min(level.quantity);
        cost = add(cost, mul(price, Decimal::from(take))?)?;
        remaining -= take;
        consumed += 1;
        if remaining == 0 {
            break;
        }
    }
    let filled = quantity - remaining;
    let avg_price = if filled != 0 {
        Some(avg(cost, Decimal::from(filled))?)
    } else {
        None
    };
    Ok(SimFill {
        side,
        requested: quantity,
        filled,
        avg_price,
        levels_consumed: consumed,
        method: SimMethod::DepthLadder,
    })
}

/// Python's `cost / filled`. Its 28-significant-digit quotient needs a scale above 28 when the
/// average is below 0.1 and inexact (two levels at 0.05 and 0.10 over three units is
/// `0.08333…`, scale 29), which rust_decimal cannot hold. Rather than refuse a paper fill, that
/// one case is rounded half-even to 28 places — the same value to within 1e-28, printed with one
/// digit fewer than Python's. Every other quotient is Python's exactly.
fn avg(cost: Decimal, filled: Decimal) -> CoreResult<Decimal> {
    py_div(cost, filled)
        .or_else(|_| py_div_quantize(cost, filled, 28, RoundingStrategy::MidpointNearestEven))
}

/// The one exit that wins when several fire on one tick — the first in `precedence`.
///
/// O1 `HARD_EXIT > STRIKE_TOUCH > STOP > PROFIT` (condor §7.6). A fired code missing from
/// `precedence` is a caller bug and is an error.
///
/// Ports `baskfy_core.options.execution.first_by_precedence`.
pub fn first_by_precedence<C>(fired: &[C], precedence: &[C]) -> CoreResult<Option<C>>
where
    C: Copy + PartialEq + std::fmt::Debug,
{
    let unknown: Vec<&C> = fired.iter().filter(|c| !precedence.contains(c)).collect();
    if !unknown.is_empty() {
        return Err(CoreError::InvalidInput(format!(
            "no precedence for {unknown:?}"
        )));
    }
    Ok(precedence.iter().copied().find(|code| fired.contains(code)))
}

/// §8.5: on a stale quote the profit-taking / target rules are not evaluated; stops,
/// invalidations and hard exits still are, on the last known mark.
///
/// Ports `baskfy_core.options.execution.drop_on_stale`.
pub fn drop_on_stale<C>(fired: &[C], profit_codes: &[C], stale: bool) -> Vec<C>
where
    C: Copy + PartialEq,
{
    fired
        .iter()
        .copied()
        .filter(|code| !(stale && profit_codes.contains(code)))
        .collect()
}

/// §8.5 — `HARD_EXIT` with reason `FEED_LOST`.
///
/// With a position open: O1 and O3 lose the feed after `feed_watch_from` (14:00) once no index
/// tick has come for `stale_index_seconds`; O2 at any time once the position is older than
/// `feed_grace_seconds` and no tick has come for `stale_index_seconds`. No position, no exit.
/// Times are IST wall-clock (`bars::ist` applied by the caller).
///
/// Ports `baskfy_core.options.execution.feed_lost`.
#[must_use]
pub fn feed_lost(
    sleeve: crate::options::gating::Sleeve,
    now: chrono::NaiveDateTime,
    last_index_tick: Option<chrono::NaiveDateTime>,
    position_opened_at: Option<chrono::NaiveDateTime>,
    stale_index_seconds: i64,
    config: &ExecutionConfig,
) -> bool {
    use crate::options::gating::{SleeveGroup, group_of};
    let Some(opened) = position_opened_at else {
        return false;
    };
    // `(a - b).total_seconds() > n` on microseconds, exactly.
    let older_than = |a: chrono::NaiveDateTime, b: chrono::NaiveDateTime, secs: i64| {
        (a - b).num_microseconds().map_or(a > b, |us| {
            i128::from(us) > i128::from(secs) * 1_000_000
        })
    };
    let silent = last_index_tick.is_none_or(|t| older_than(now, t, stale_index_seconds));
    if !silent {
        return false;
    }
    if group_of(sleeve) == SleeveGroup::O2 {
        return older_than(now, opened, config.feed_grace_seconds);
    }
    now.time() >= config.feed_watch_from
}
