//! Port of `baskfy_core.options.expiry_setups`: O3, two expiry-day debit-spread setups (`04` §5).

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::options::bars::{
    Bar, add_seconds, bar_at, closed, efficiency_ratio, er_points, expected_minutes,
    five_minute_bars, high_low, ist, minute_after, pct, window, window_settled,
};
use crate::options::chain::is_liquid;
use crate::options::config::{ExpirySetupsConfig, OptionType, OptionsCeilings, OptionsConfig};
use crate::options::costs::expected_gain_o3;
use crate::options::execution::{LegRole, drop_on_stale, first_by_precedence};
use crate::options::num::{self, py_str};
use crate::options::risk::trade_breached;
use crate::options::sizing::risk_per_lot_debit_spread;
use crate::options::structures::{
    Candidate, ChainView, Direction, Known, Rejection, SleeveBook, Structure, cost_gate, leg,
    rejected, round_trip, size_for,
};

/// Why a setup does not arm today.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SetupReason {
    /// O3-A's range is too wide.
    RangeTooWide,
    /// O3-B's gap is too small.
    GapTooSmall,
    /// O3-B's gap is too big.
    GapTooBig,
    /// O3-B's hold broke.
    HoldBroken,
    /// An event day.
    EventDay,
    /// The window is incomplete.
    IncompleteObservation,
}

impl SetupReason {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            SetupReason::RangeTooWide => "RANGE_TOO_WIDE",
            SetupReason::GapTooSmall => "GAP_TOO_SMALL",
            SetupReason::GapTooBig => "GAP_TOO_BIG",
            SetupReason::HoldBroken => "HOLD_BROKEN",
            SetupReason::EventDay => "EVENT_DAY",
            SetupReason::IncompleteObservation => "INCOMPLETE_OBSERVATION",
        }
    }
}

/// A break the ER refused: `(close_time, close, er)`.
pub type LowErBreak = (NaiveTime, Decimal, Decimal);

/// O3-A at `now`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RangeBreakScan {
    /// The morning range is settled.
    pub final_: bool,
    /// Every reason.
    pub reasons: Vec<SetupReason>,
    /// Bars in the morning range.
    pub bars: i64,
    /// Range high.
    pub range_high: Option<Decimal>,
    /// Range low.
    pub range_low: Option<Decimal>,
    /// Range, percent.
    pub range_pct: Option<Decimal>,
    /// The up level.
    pub level_up: Option<Decimal>,
    /// The down level.
    pub level_down: Option<Decimal>,
    /// The trigger bar's close minute.
    pub trigger_time: Option<NaiveTime>,
    /// The trigger bar's close.
    pub trigger_close: Option<Decimal>,
    /// The trigger's ER.
    pub trigger_er: Option<Decimal>,
    /// The side broken.
    pub direction: Option<Direction>,
    /// Breaks the ER refused.
    pub low_er_breaks: Vec<LowErBreak>,
    /// The window closed with no trigger.
    pub window_closed: bool,
    /// The last closed minute's close.
    pub last_close: Option<Decimal>,
}

impl RangeBreakScan {
    /// Final with no reason.
    pub fn armed(&self) -> bool {
        self.final_ && self.reasons.is_empty()
    }
}

fn positive_close(prev_close: Decimal) -> CoreResult<()> {
    if prev_close <= Decimal::ZERO {
        return Err(CoreError::InvalidInput(
            "the previous close must be positive".to_owned(),
        ));
    }
    Ok(())
}

/// The clock and the session `range_break` and `gap_hold` read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SetupClock {
    /// Now.
    pub now: DateTime<FixedOffset>,
    /// An event day.
    pub event_day: bool,
    /// The session open.
    pub session_open: NaiveTime,
    /// Grace for a late minute, seconds.
    pub grace_seconds: i64,
}

/// `04` §5.1 over NIFTY 50's closed bars.
pub fn range_break(
    bars: &[Bar],
    prev_close: Decimal,
    clock: &SetupClock,
    config: &ExpirySetupsConfig,
) -> CoreResult<RangeBreakScan> {
    positive_close(prev_close)?;
    let now = &clock.now;
    let seen = closed(bars, now);
    let morning = window(&seen, config.o3a_range_start, config.o3a_range_end);
    let hl = high_low(&morning);
    let range_pct = hl
        .map(|(h, l)| num::sub(h, l).and_then(|r| pct(r, prev_close)))
        .transpose()?;
    let final_ = window_settled(&morning, config.o3a_range_end, now, clock.grace_seconds);
    let mut reasons = Vec::new();
    if range_pct.is_some_and(|r| r > config.o3a_range_max_pct) {
        reasons.push(SetupReason::RangeTooWide);
    }
    if clock.event_day {
        reasons.push(SetupReason::EventDay);
    }
    let n = i64::try_from(morning.len()).unwrap_or(i64::MAX);
    if final_ && n < config.o3a_range_bars {
        reasons.push(SetupReason::IncompleteObservation);
    }
    let buffer = num::div(config.o3a_buffer_pct, Decimal::ONE_HUNDRED)?;
    let up = hl
        .map(|(h, _)| num::add(Decimal::ONE, buffer).and_then(|f| num::mul(h, f)))
        .transpose()?;
    let down = hl
        .map(|(_, l)| num::sub(Decimal::ONE, buffer).and_then(|f| num::mul(l, f)))
        .transpose()?;
    let mut trigger: Option<(NaiveTime, Decimal, Decimal, Direction)> = None;
    let mut refused = Vec::new();
    if let (true, true, Some(up), Some(down)) = (final_, reasons.is_empty(), up, down) {
        let end_of_day = NaiveTime::from_hms_opt(23, 59, 0).unwrap_or(NaiveTime::MIN);
        let path_from = window(&seen, config.o3a_range_start, end_of_day);
        for bar in five_minute_bars(&seen, clock.session_open, config.bar_minutes)? {
            let Some(close) = bar.close else {
                continue;
            };
            if !(config.o3a_window_start <= bar.close_time
                && bar.close_time <= config.o3a_window_end)
            {
                continue;
            }
            let side = if close > up {
                Direction::Up
            } else if close < down {
                Direction::Down
            } else {
                continue;
            };
            let upto = window(&path_from, config.o3a_range_start, bar.close_time);
            let er = efficiency_ratio(&er_points(&upto))?;
            if er >= config.o3a_er_min {
                trigger = Some((bar.close_time, close, er, side));
                break;
            }
            refused.push((bar.close_time, close, er));
        }
    }
    let done =
        ist(now).time() >= add_seconds(minute_after(config.o3a_window_end), clock.grace_seconds);
    Ok(RangeBreakScan {
        final_,
        reasons,
        bars: n,
        range_high: hl.map(|x| x.0),
        range_low: hl.map(|x| x.1),
        range_pct,
        level_up: up,
        level_down: down,
        trigger_time: trigger.map(|t| t.0),
        trigger_close: trigger.map(|t| t.1),
        trigger_er: trigger.map(|t| t.2),
        direction: trigger.map(|t| t.3),
        low_er_breaks: refused,
        window_closed: trigger.is_none() && done,
        last_close: seen.last().map(|b| b.close),
    })
}

/// O3-B at `now`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GapHoldScan {
    /// The hold window is settled.
    pub final_: bool,
    /// Every reason.
    pub reasons: Vec<SetupReason>,
    /// Bars in the hold window.
    pub bars: i64,
    /// The 09:15 open.
    pub open_0915: Option<Decimal>,
    /// The gap, points.
    pub gap_points: Option<Decimal>,
    /// The gap, percent.
    pub gap_pct: Option<Decimal>,
    /// Halfway back to the previous close.
    pub half_gap: Option<Decimal>,
    /// The gap's direction.
    pub direction: Option<Direction>,
    /// Held so far.
    pub held_so_far: Option<bool>,
    /// Triggered at the plan time.
    pub triggered: bool,
    /// The window closed without a trigger.
    pub window_closed: bool,
}

/// `04` §5.2: the gap's size, then no bar through `half_gap` to the hold end.
pub fn gap_hold(
    bars: &[Bar],
    prev_close: Decimal,
    clock: &SetupClock,
    config: &ExpirySetupsConfig,
) -> CoreResult<GapHoldScan> {
    positive_close(prev_close)?;
    let now = &clock.now;
    let seen = closed(bars, now);
    let hold = window(&seen, clock.session_open, config.o3b_hold_end);
    let open_0915 = bar_at(&hold, clock.session_open).map(|b| b.open);
    let gap = open_0915.map(|o| num::sub(o, prev_close)).transpose()?;
    let gap_pct = gap
        .map(|g| pct(g, prev_close).and_then(num::abs))
        .transpose()?;
    let half = gap
        .map(|g| num::div(g, Decimal::TWO).and_then(|h| num::add(prev_close, h)))
        .transpose()?;
    let direction = match gap {
        Some(g) if !g.is_zero() => Some(if g > Decimal::ZERO {
            Direction::Up
        } else {
            Direction::Down
        }),
        _ => None,
    };
    let held = match (half, direction) {
        (Some(h), Some(Direction::Up)) if !hold.is_empty() => Some(hold.iter().all(|b| b.low > h)),
        (Some(h), Some(Direction::Down)) if !hold.is_empty() => {
            Some(hold.iter().all(|b| b.high < h))
        }
        _ => None,
    };
    let expected = expected_minutes(clock.session_open, config.o3b_hold_end);
    let final_ = window_settled(&hold, config.o3b_hold_end, now, clock.grace_seconds);
    let mut reasons = Vec::new();
    if gap_pct.is_some_and(|g| g < config.o3b_gap_min_pct) {
        reasons.push(SetupReason::GapTooSmall);
    }
    if gap_pct.is_some_and(|g| g > config.o3b_gap_max_pct) {
        reasons.push(SetupReason::GapTooBig);
    }
    if held == Some(false) {
        reasons.push(SetupReason::HoldBroken);
    }
    if clock.event_day {
        reasons.push(SetupReason::EventDay);
    }
    let n = i64::try_from(hold.len()).unwrap_or(i64::MAX);
    if final_ && n < expected {
        reasons.push(SetupReason::IncompleteObservation);
    }
    let wall = ist(now).time();
    let triggered = final_ && reasons.is_empty() && wall >= config.o3b_plan_time;
    Ok(GapHoldScan {
        final_,
        reasons,
        bars: n,
        open_0915,
        gap_points: gap,
        gap_pct,
        half_gap: half,
        direction,
        held_so_far: held,
        triggered,
        window_closed: !triggered && wall > config.o3b_window_end,
    })
}

/// `04` §5: long ATM, short `width` further in the move's direction.
pub fn strikes_for(
    view: &ChainView,
    direction: Direction,
    width: Decimal,
) -> CoreResult<(Decimal, Decimal, OptionType)> {
    Ok(match direction {
        Direction::Up => (view.atm, num::add(view.atm, width)?, OptionType::Ce),
        Direction::Down => (view.atm, num::sub(view.atm, width)?, OptionType::Pe),
    })
}

/// Every input of `build` besides the view, the direction and the configs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SpreadBuild {
    /// The master's lot size.
    pub lot_size: Option<i64>,
    /// The sleeve's book.
    pub book: SleeveBook,
    /// Expiry.
    pub expiry: NaiveDate,
    /// The slot is free.
    pub slot_free: bool,
    /// Paused.
    pub paused: bool,
}

/// The debit spread a plan would carry, or the first refusal (`04` §5/§6/§7).
pub fn build(
    view: Option<&ChainView>,
    direction: Direction,
    inputs: &SpreadBuild,
    config: &ExpirySetupsConfig,
    options: &OptionsConfig,
    ceilings: &OptionsCeilings,
) -> CoreResult<Candidate> {
    let structure = Structure::DebitSpread;
    let expiry = inputs.expiry;
    let refuse = |code: Rejection, message: String, known: Known| {
        Ok(rejected(structure, expiry, code, message, known))
    };
    let bare = || Known {
        direction: Some(direction),
        ..Known::default()
    };
    if inputs.paused {
        return refuse(
            Rejection::RejectedPaused,
            "the sleeve or book is paused".to_owned(),
            bare(),
        );
    }
    if !inputs.slot_free {
        return refuse(
            Rejection::RejectedSlotTaken,
            "another sleeve holds today's slot".to_owned(),
            bare(),
        );
    }
    let Some(view) = view else {
        return refuse(
            Rejection::RejectedNoChain,
            "no chain for the expiry".to_owned(),
            bare(),
        );
    };
    let lot_size = match inputs.lot_size {
        Some(l) if l > 0 => l,
        _ => {
            return refuse(
                Rejection::RejectedNoLotSize,
                "the master has no lot size".to_owned(),
                bare(),
            );
        }
    };
    let width = config.width_points;
    let (k_long, k_short, option_type) = strikes_for(view, direction, width)?;
    let two_sided = |k| {
        view.get(k, option_type)
            .filter(|q| q.quote.bid.is_some() && q.quote.ask.is_some())
    };
    let (Some(long_q), Some(short_q)) = (two_sided(k_long), two_sided(k_short)) else {
        return refuse(
            Rejection::RejectedNoContract,
            format!(
                "{}/{} {} is not quoted two-sided",
                py_str(k_long),
                py_str(k_short),
                option_type.value()
            ),
            Known {
                direction: Some(direction),
                lot_size: Some(lot_size),
                ..Known::default()
            },
        );
    };
    let (long_role, short_role) = match option_type {
        OptionType::Ce => (LegRole::LongCall, LegRole::ShortCall),
        OptionType::Pe => (LegRole::LongPut, LegRole::ShortPut),
    };
    let legs = vec![
        leg(long_role, long_q, view.tick, &options.execution)?,
        leg(short_role, short_q, view.tick, &options.execution)?,
    ];
    let z = Decimal::ZERO;
    let debit = num::sub(
        long_q.quote.ask.unwrap_or(z),
        short_q.quote.bid.unwrap_or(z),
    )?;
    let cap = num::mul(config.max_debit_frac, width)?;
    let mut extra = BTreeMap::new();
    extra.insert("width_points".to_owned(), py_str(width));
    extra.insert("max_debit_points".to_owned(), py_str(cap));
    let known = |extra: &BTreeMap<String, String>| Known {
        direction: Some(direction),
        legs: legs.clone(),
        points: Some(debit),
        lot_size: Some(lot_size),
        extra: extra.clone(),
    };
    if debit <= Decimal::ZERO || debit > cap {
        return refuse(
            Rejection::RejectedDebit,
            format!(
                "debit {} outside (0, {}] ({} * {})",
                py_str(debit),
                py_str(cap),
                py_str(config.max_debit_frac),
                py_str(width)
            ),
            known(&extra),
        );
    }
    let per_lot = risk_per_lot_debit_spread(debit, lot_size, config.reserve_per_lot_inr)?;
    let sizing = size_for(
        &inputs.book,
        config.risk_per_trade_pct,
        config.max_lots,
        per_lot,
        Some(lot_size),
        &options.sizing,
        ceilings,
    )?;
    if let Some(code) = sizing.rejection {
        return refuse(code.into(), sizing.message, known(&extra));
    }
    let quantity = sizing.lots * lot_size;
    let mut thin = Vec::new();
    for (lg, q) in legs.iter().zip([long_q, short_q]) {
        if !is_liquid(
            &q.quote,
            quantity,
            lg.entry_side(),
            lot_size,
            &options.chain,
            false,
        )?
        .ok
        {
            thin.push(lg.role.value());
        }
    }
    if !thin.is_empty() {
        return refuse(
            Rejection::RejectedIlliquid,
            format!("not liquid for {quantity} units: {}", thin.join(", ")),
            known(&extra),
        );
    }
    let trip = round_trip(
        &legs,
        quantity,
        view.tick,
        &options.costs,
        &options.execution,
    )?;
    let gain = expected_gain_o3(width, debit, quantity, config.target_frac_of_width)?;
    let (share, passes) = cost_gate(trip, gain, config.cost_share_max)?;
    let message = if passes {
        sizing.message.clone()
    } else {
        format!(
            "cost share {} > {} (round trip ₹{})",
            share.map_or_else(|| "None".to_owned(), py_str),
            py_str(config.cost_share_max),
            py_str(trip)
        )
    };
    Ok(Candidate {
        structure,
        expiry,
        direction: Some(direction),
        legs,
        points: Some(debit),
        lot_size: Some(lot_size),
        lots: if passes { sizing.lots } else { 0 },
        sizing_mode: Some(sizing.sizing_mode),
        risk_per_lot_inr: Some(per_lot),
        r_inr: Some(sizing.r_inr),
        max_loss_inr: Some(num::muli(debit, quantity)?),
        round_trip_inr: Some(trip),
        expected_gain_inr: Some(gain),
        cost_share: share,
        rejection: if passes {
            None
        } else {
            Some(Rejection::RejectedCost)
        },
        message,
        half_size: sizing.half_size,
        warnings: Vec::new(),
        extra,
    })
}

/// O3's exit codes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SpreadExit {
    /// The clock.
    HardExit,
    /// The stop.
    Stop,
    /// Invalidated.
    Invalidated,
    /// The target.
    Target,
    /// The operator's button.
    Manual,
}

impl SpreadExit {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            SpreadExit::HardExit => "HARD_EXIT",
            SpreadExit::Stop => "STOP",
            SpreadExit::Invalidated => "INVALIDATED",
            SpreadExit::Target => "TARGET",
            SpreadExit::Manual => "MANUAL",
        }
    }
}

/// `HARD_EXIT > STOP > INVALIDATED > TARGET > MANUAL`.
pub const PRECEDENCE: [SpreadExit; 5] = [
    SpreadExit::HardExit,
    SpreadExit::Stop,
    SpreadExit::Invalidated,
    SpreadExit::Target,
    SpreadExit::Manual,
];

/// `V = long.bid - short.ask`.
pub fn close_value(long_bid: Decimal, short_ask: Decimal) -> CoreResult<Decimal> {
    num::sub(long_bid, short_ask)
}

/// O3-A: a completed 5-minute close back inside the morning range.
pub fn range_break_invalidated(
    five_minute_close: Option<Decimal>,
    direction: Direction,
    high: Decimal,
    low: Decimal,
) -> bool {
    match (five_minute_close, direction) {
        (None, _) => false,
        (Some(c), Direction::Up) => c < high,
        (Some(c), Direction::Down) => c > low,
    }
}

/// O3-B: any bar through `half_gap`.
pub fn gap_hold_invalidated(bar: Option<&Bar>, direction: Direction, half_gap: Decimal) -> bool {
    match (bar, direction) {
        (None, _) => false,
        (Some(b), Direction::Up) => b.low <= half_gap,
        (Some(b), Direction::Down) => b.high >= half_gap,
    }
}

/// One tick's marks for the spread.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SpreadTick {
    /// V, the close value.
    pub value: Decimal,
    /// D, the entry debit.
    pub entry_debit: Decimal,
    /// The width.
    pub width: Decimal,
    /// Invalidated.
    pub is_invalidated: bool,
    /// Now.
    pub now: DateTime<FixedOffset>,
    /// The mark is stale.
    pub stale: bool,
    /// Marked loss, positive.
    pub marked_loss_inr: Decimal,
    /// Risk budget.
    pub risk_budget_inr: Decimal,
    /// The operator pressed close.
    #[serde(default)]
    pub manual: bool,
}

/// One tick's exit for the spread, or `None`.
pub fn exit_decision(
    tick: &SpreadTick,
    config: &ExpirySetupsConfig,
    options: &OptionsConfig,
) -> CoreResult<Option<SpreadExit>> {
    let mut fired = Vec::new();
    if ist(&tick.now).time() >= config.hard_exit_time {
        fired.push(SpreadExit::HardExit);
    }
    if tick.value <= num::mul(config.stop_frac_of_debit, tick.entry_debit)?
        || trade_breached(tick.marked_loss_inr, tick.risk_budget_inr, &options.risk)?
    {
        fired.push(SpreadExit::Stop);
    }
    if tick.is_invalidated {
        fired.push(SpreadExit::Invalidated);
    }
    if tick.value >= num::mul(config.target_frac_of_width, tick.width)? {
        fired.push(SpreadExit::Target);
    }
    if tick.manual {
        fired.push(SpreadExit::Manual);
    }
    let kept = drop_on_stale(&fired, &[SpreadExit::Target], tick.stale);
    first_by_precedence(&kept, &PRECEDENCE)
}
