//! Port of `baskfy_core.options.execution` (`decile-blueprint/packages/core/src/baskfy_core/options/execution.py`).
//!
//! The execution mechanics the core decides and the desk sends (docs/options/04 §8). Pure decisions only — law 2: nothing here sends an order.
//!
//! Goldens: `L1/options/never_naked.*` and `L1/options/simulate_fill.*` (`baskfy_core.options.execution.never_naked`, `.simulate_fill`).
//!

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset};
use rust_decimal::{Decimal, RoundingStrategy};
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::options::chain::as_f64;
use crate::options::config::{OptionType, SLOT_SLEEVES, Sleeve, SleeveGroup, group_of};
use crate::options::costs::{Side, add, mul, py_div, py_div_quantize};
use crate::options::num::{self, seconds_between};

pub use crate::options::chain::Level;
pub use crate::options::config::ExecutionConfig;

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
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            LegRole::LongCall => "LONG_CALL",
            LegRole::LongPut => "LONG_PUT",
            LegRole::ShortCall => "SHORT_CALL",
            LegRole::ShortPut => "SHORT_PUT",
        }
    }

    /// The leg's option type.
    pub fn option_type(self) -> OptionType {
        match self {
            LegRole::LongCall | LegRole::ShortCall => OptionType::Ce,
            LegRole::LongPut | LegRole::ShortPut => OptionType::Pe,
        }
    }

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

fn entry_rank(role: LegRole) -> u8 {
    match role {
        LegRole::LongPut => 0,
        LegRole::LongCall => 1,
        LegRole::ShortPut => 2,
        LegRole::ShortCall => 3,
    }
}

fn exit_rank(role: LegRole) -> u8 {
    match role {
        LegRole::ShortCall => 0,
        LegRole::ShortPut => 1,
        LegRole::LongCall => 2,
        LegRole::LongPut => 3,
    }
}

/// Every long before any short (§8.3); refuses a duplicate role or a short with no protecting long.
pub fn entry_sequence(roles: &[LegRole]) -> CoreResult<Vec<LegRole>> {
    let mut seen: Vec<LegRole> = Vec::new();
    for r in roles {
        if seen.contains(r) {
            return Err(CoreError::InvalidInput(
                "a structure has at most one leg per role".to_owned(),
            ));
        }
        seen.push(*r);
    }
    for r in roles {
        if let Some(p) = r.protector()
            && !roles.contains(&p)
        {
            return Err(CoreError::InvalidInput(format!(
                "{} has no {}: a naked short is Track C",
                r.value(),
                p.value()
            )));
        }
    }
    let mut out = roles.to_vec();
    out.sort_by_key(|r| entry_rank(*r));
    Ok(out)
}

/// Every short before any long (§8.3).
pub fn exit_sequence(roles: &[LegRole]) -> Vec<LegRole> {
    let mut out: Vec<LegRole> = Vec::new();
    for r in roles {
        if !out.contains(r) {
            out.push(*r);
        }
    }
    out.sort_by_key(|r| exit_rank(*r));
    out
}

/// The entry reducer's step.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum EntryStep {
    /// Send the named leg.
    Send,
    /// Every leg filled.
    Open,
    /// Close what filled.
    Abandon,
}

/// What the desk does next.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EntryAction {
    /// The step.
    pub step: EntryStep,
    /// The leg to send.
    pub leg: Option<LegRole>,
    /// What to close, in exit order, with each filled quantity.
    pub close: Vec<(LegRole, i64)>,
}

/// The entry reducer: `outcomes[i]` is the final filled quantity of `sequence[i]`.
pub fn advance_entry(
    sequence: &[LegRole],
    quantity: i64,
    outcomes: &[i64],
) -> CoreResult<EntryAction> {
    if outcomes.len() > sequence.len() {
        return Err(CoreError::InvalidInput(
            "more outcomes than legs".to_owned(),
        ));
    }
    for (role, filled) in sequence.iter().zip(outcomes) {
        if !(0..=quantity).contains(filled) {
            return Err(CoreError::InvalidInput(format!(
                "{} filled {filled} of {quantity}",
                role.value()
            )));
        }
    }
    if outcomes.iter().any(|f| *f < quantity) {
        let filled: Vec<(LegRole, i64)> = sequence
            .iter()
            .zip(outcomes)
            .filter(|(_, f)| **f > 0)
            .map(|(r, f)| (*r, *f))
            .collect();
        let roles: Vec<LegRole> = filled.iter().map(|(r, _)| *r).collect();
        let close = exit_sequence(&roles)
            .into_iter()
            .filter_map(|r| filled.iter().find(|(x, _)| *x == r).copied())
            .collect();
        return Ok(EntryAction {
            step: EntryStep::Abandon,
            leg: None,
            close,
        });
    }
    if outcomes.len() == sequence.len() {
        return Ok(EntryAction {
            step: EntryStep::Open,
            leg: None,
            close: Vec::new(),
        });
    }
    Ok(EntryAction {
        step: EntryStep::Send,
        leg: Some(sequence[outcomes.len()]),
        close: Vec::new(),
    })
}

/// The next leg to close: the first in exit order still open, never a long while its short is open.
pub fn next_exit_leg(open_quantity: &[(LegRole, i64)]) -> Option<LegRole> {
    let qty = |r: LegRole| {
        open_quantity
            .iter()
            .find(|(x, _)| *x == r)
            .map_or(0, |(_, q)| *q)
    };
    let roles: Vec<LegRole> = open_quantity.iter().map(|(r, _)| *r).collect();
    for role in exit_sequence(&roles) {
        if qty(role) <= 0 {
            continue;
        }
        let protected_open = [LegRole::ShortCall, LegRole::ShortPut]
            .into_iter()
            .any(|s| s.protector() == Some(role) && qty(s) > 0);
        if protected_open {
            continue;
        }
        return Some(role);
    }
    None
}

/// One LIMIT order attempt.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Attempt {
    /// 1, 2 or 3.
    pub number: i64,
    /// Buy or sell.
    pub side: Side,
    /// The limit.
    pub price: Decimal,
    /// The third, risk-reducing close at the touch.
    pub marketable: bool,
}

/// §8.2: attempt 1 one tick through the touch, attempt 2 one more, attempt 3 only for a risk-reducing close.
pub fn next_attempt(
    number: i64,
    side: Side,
    bid: Decimal,
    ask: Decimal,
    tick: Decimal,
    closing_reduces_risk: bool,
    config: &ExecutionConfig,
) -> CoreResult<Option<Attempt>> {
    if number < 1 {
        return Err(CoreError::InvalidInput("attempts count from 1".to_owned()));
    }
    let (direction, touch) = match side {
        Side::Buy => (Decimal::ONE, ask),
        Side::Sell => (Decimal::NEGATIVE_ONE, bid),
    };
    if number <= 2 {
        let ticks = config.limit_improve_ticks + (number - 1);
        let price = num::add(touch, num::muli(num::mul(direction, tick)?, ticks)?)?;
        return Ok(Some(Attempt {
            number,
            side,
            price: num::max(price, tick),
            marketable: false,
        }));
    }
    if number == 3 && closing_reduces_risk && config.exit_final_marketable {
        return Ok(Some(Attempt {
            number,
            side,
            price: num::max(touch, tick),
            marketable: true,
        }));
    }
    Ok(None)
}

/// §8.5 — `HARD_EXIT` with reason `FEED_LOST`.
pub fn feed_lost(
    sleeve: Sleeve,
    now: &DateTime<FixedOffset>,
    last_index_tick: Option<&DateTime<FixedOffset>>,
    position_opened_at: Option<&DateTime<FixedOffset>>,
    stale_index_seconds: i64,
    config: &ExecutionConfig,
) -> bool {
    let Some(opened) = position_opened_at else {
        return false;
    };
    let silent =
        last_index_tick.is_none_or(|t| seconds_between(now, t) > as_f64(stale_index_seconds));
    if !silent {
        return false;
    }
    if group_of(sleeve) == SleeveGroup::O2 {
        return seconds_between(now, opened) > as_f64(config.feed_grace_seconds);
    }
    now.time() >= config.feed_watch_from
}

/// The expiry-day slot's verdict for a sleeve.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SlotVerdict {
    /// Nobody holds it.
    Free,
    /// This sleeve holds it.
    HeldBySelf,
    /// Another sleeve holds it.
    SlotTaken,
    /// O2: outside the slot.
    OutsideSlot,
}

/// §8.6: one O1-or-O3 position per expiry day; O2 is outside it.
pub fn slot_verdict(sleeve: Sleeve, holder: Option<Sleeve>) -> SlotVerdict {
    if !SLOT_SLEEVES.contains(&sleeve) {
        return SlotVerdict::OutsideSlot;
    }
    match holder {
        None => SlotVerdict::Free,
        Some(h) if h == sleeve => SlotVerdict::HeldBySelf,
        Some(_) => SlotVerdict::SlotTaken,
    }
}

/// The holder after `sleeve` confirms; a taken slot refuses.
pub fn slot_after_confirm(sleeve: Sleeve, holder: Option<Sleeve>) -> CoreResult<Option<Sleeve>> {
    match slot_verdict(sleeve, holder) {
        SlotVerdict::SlotTaken => Err(CoreError::InvalidInput(format!(
            "the expiry-day slot is held by {}",
            holder.map_or("None", Sleeve::value)
        ))),
        SlotVerdict::OutsideSlot => Ok(holder),
        SlotVerdict::Free | SlotVerdict::HeldBySelf => Ok(Some(sleeve)),
    }
}

/// A lapsed or rejected plan frees the slot only if its sleeve held it.
pub fn slot_after_release(sleeve: Sleeve, holder: Option<Sleeve>) -> Option<Sleeve> {
    if holder == Some(sleeve) { None } else { holder }
}
