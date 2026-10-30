//! Port of `baskfy_core.options.directional`: O2, a with-trend opening-range break (`04` §4).

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime, TimeDelta};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::decimal::WideDecimal;
use crate::error::{CoreError, CoreResult};
use crate::options::bars::{
    Bar, add_seconds, bar_at, closed, five_minute_bars, high_low, ist, minute_after, pct, window,
    window_settled,
};
use crate::options::chain::is_liquid;
use crate::options::config::{DirectionalConfig, OptionType, OptionsCeilings, OptionsConfig};
use crate::options::costs::expected_gain_o2;
use crate::options::execution::{LegRole, drop_on_stale, first_by_precedence};
use crate::options::num::{self, from_float, py_str};
use crate::options::risk::trade_breached;
use crate::options::sizing::{gap_through_long, premium_cap_ok, risk_per_lot_long};
use crate::options::structures::{
    Candidate, ChainView, Direction, Known, Rejection, SleeveBook, Structure, cost_gate, leg,
    rejected, round_trip, size_for,
};

/// `04` §4.1's skip reasons, plus the two missing-data refusals.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum DayReason {
    /// Close equals its EMA.
    TrendFlat,
    /// Too few closes for the EMA.
    TrendUnknown,
    /// Gap too big.
    GapTooBig,
    /// Opening range too wide.
    RangeTooWide,
    /// India VIX too high.
    VixTooHigh,
    /// No India VIX close.
    VixUnknown,
    /// An event day.
    EventDay,
    /// The opening range is incomplete.
    IncompleteObservation,
}

impl DayReason {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            DayReason::TrendFlat => "TREND_FLAT",
            DayReason::TrendUnknown => "TREND_UNKNOWN",
            DayReason::GapTooBig => "GAP_TOO_BIG",
            DayReason::RangeTooWide => "RANGE_TOO_WIDE",
            DayReason::VixTooHigh => "VIX_TOO_HIGH",
            DayReason::VixUnknown => "VIX_UNKNOWN",
            DayReason::EventDay => "EVENT_DAY",
            DayReason::IncompleteObservation => "INCOMPLETE_OBSERVATION",
        }
    }
}

fn arith(e: crate::decimal::DecimalError) -> CoreError {
    CoreError::Arithmetic(e.to_string())
}

/// The `days`-day EMA of `closes` (oldest first) at the last close, seeded with the simple mean.
pub fn ema(closes: &[Decimal], days: i64) -> CoreResult<Option<Decimal>> {
    if days <= 0 {
        return Err(CoreError::InvalidInput(
            "an EMA needs a positive span".to_owned(),
        ));
    }
    let n = usize::try_from(days).unwrap_or(usize::MAX);
    if closes.len() < n {
        return Ok(None);
    }
    let w = |d: Decimal| WideDecimal::from(d);
    let alpha = WideDecimal::from(2_i64)
        .div(&WideDecimal::from(days + 1))
        .map_err(arith)?;
    let one_minus = WideDecimal::from(1_i64).sub(&alpha);
    let seed = closes[..n]
        .iter()
        .fold(WideDecimal::zero(), |acc, c| acc.add(&w(*c)));
    let mut value = seed.div(&WideDecimal::from(days)).map_err(arith)?;
    for close in &closes[n..] {
        value = alpha.mul(&w(*close)).add(&one_minus.mul(&value));
    }
    value.try_to_decimal().map(Some).map_err(arith)
}

/// `04` §4.1's trend: `UP` above the EMA, `DOWN` below, `None` when equal or unknown.
pub fn trend(
    daily_closes: &[Decimal],
    days: i64,
) -> CoreResult<(Option<Direction>, Option<Decimal>)> {
    let Some(average) = ema(daily_closes, days)? else {
        return Ok((None, None));
    };
    let Some(prev) = daily_closes.last().copied() else {
        return Ok((None, Some(average)));
    };
    Ok(if prev > average {
        (Some(Direction::Up), Some(average))
    } else if prev < average {
        (Some(Direction::Down), Some(average))
    } else {
        (None, Some(average))
    })
}

/// `04` §4.1's numbers and verdict; `trade` iff final with no reason.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DayFilters {
    /// The opening range is settled.
    pub final_: bool,
    /// Every reason.
    pub reasons: Vec<DayReason>,
    /// The trend.
    pub trend: Option<Direction>,
    /// The EMA.
    pub ema: Option<Decimal>,
    /// Previous close.
    pub prev_close: Option<Decimal>,
    /// The 09:15 open.
    pub open_0915: Option<Decimal>,
    /// The gap, percent.
    pub gap_pct: Option<Decimal>,
    /// Opening range high.
    pub or_high: Option<Decimal>,
    /// Opening range low.
    pub or_low: Option<Decimal>,
    /// Opening range, percent.
    pub or_pct: Option<Decimal>,
    /// India VIX's previous close.
    pub vix: Option<Decimal>,
    /// Bars in the opening range.
    pub bars: i64,
}

impl DayFilters {
    /// Final with no reason.
    pub fn trade(&self) -> bool {
        self.final_ && self.reasons.is_empty()
    }
}

/// `04` §4.1 over NIFTY 50's closed bars and its daily closes through the previous session.
pub fn day_filters(
    bars: &[Bar],
    daily_closes: &[Decimal],
    vix_prev_close: Option<Decimal>,
    now: &DateTime<FixedOffset>,
    event_day: bool,
    config: &DirectionalConfig,
    grace_seconds: i64,
) -> CoreResult<DayFilters> {
    let seen = closed(bars, now);
    let opening = window(&seen, config.opening_range_start, config.opening_range_end);
    let (direction, average) = trend(daily_closes, config.trend_ema_days)?;
    let prev = daily_closes.last().copied();
    let usable_prev = prev.filter(|p| !p.is_zero());
    let open_0915 = bar_at(&opening, config.opening_range_start).map(|b| b.open);
    let gap = match (open_0915, usable_prev) {
        (Some(o), Some(p)) => Some(num::abs(pct(num::sub(o, p)?, p)?)?),
        _ => None,
    };
    let hl = high_low(&opening);
    let or_pct = match (hl, usable_prev) {
        (Some((h, l)), Some(p)) => Some(pct(num::sub(h, l)?, p)?),
        _ => None,
    };
    let final_ = window_settled(&opening, config.opening_range_end, now, grace_seconds);
    let mut reasons = Vec::new();
    if average.is_none() {
        reasons.push(DayReason::TrendUnknown);
    } else if direction.is_none() {
        reasons.push(DayReason::TrendFlat);
    }
    if gap.is_some_and(|g| g > config.gap_max_pct) {
        reasons.push(DayReason::GapTooBig);
    }
    if or_pct.is_some_and(|r| r > config.or_max_pct) {
        reasons.push(DayReason::RangeTooWide);
    }
    match vix_prev_close {
        None => reasons.push(DayReason::VixUnknown),
        Some(v) if v > config.vix_max => reasons.push(DayReason::VixTooHigh),
        Some(_) => {}
    }
    if event_day {
        reasons.push(DayReason::EventDay);
    }
    let n = i64::try_from(opening.len()).unwrap_or(i64::MAX);
    if final_ && n < config.opening_range_bars {
        reasons.push(DayReason::IncompleteObservation);
    }
    Ok(DayFilters {
        final_,
        reasons,
        trend: direction,
        ema: average,
        prev_close: prev,
        open_0915,
        gap_pct: gap,
        or_high: hl.map(|x| x.0),
        or_low: hl.map(|x| x.1),
        or_pct,
        vix: vix_prev_close,
        bars: n,
    })
}

/// A completed 5-minute bar that closed beyond a level.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Break {
    /// Its close minute.
    pub close_time: NaiveTime,
    /// Its close.
    pub close: Decimal,
    /// Which way it broke.
    pub direction: Direction,
}

/// `04` §4.2 at `now`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TriggerScan {
    /// The up level.
    pub level_up: Decimal,
    /// The down level.
    pub level_down: Decimal,
    /// The trigger, if any.
    pub trigger: Option<Break>,
    /// Counter-trend breaks seen, never traded.
    pub counter_breaks: Vec<Break>,
    /// The window closed with no trigger.
    pub window_closed: bool,
    /// The last closed minute's close.
    pub last_close: Option<Decimal>,
}

/// The break levels: `or_high * (1 + buffer)` and `or_low * (1 - buffer)`.
pub fn levels(
    or_high: Decimal,
    or_low: Decimal,
    buffer_pct: Decimal,
) -> CoreResult<(Decimal, Decimal)> {
    let b = num::div(buffer_pct, Decimal::ONE_HUNDRED)?;
    Ok((
        num::mul(or_high, num::add(Decimal::ONE, b)?)?,
        num::mul(or_low, num::sub(Decimal::ONE, b)?)?,
    ))
}

/// The opening range and trend `find_trigger` reads.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Range {
    /// Opening range high.
    pub or_high: Decimal,
    /// Opening range low.
    pub or_low: Decimal,
    /// The trend.
    pub direction: Direction,
}

/// The first completed 5-minute bar in the entry window beyond the with-trend level.
pub fn find_trigger(
    bars: &[Bar],
    range: Range,
    now: &DateTime<FixedOffset>,
    config: &DirectionalConfig,
    session_open: NaiveTime,
    grace_seconds: i64,
) -> CoreResult<TriggerScan> {
    let seen = closed(bars, now);
    let (up, down) = levels(range.or_high, range.or_low, config.buffer_pct)?;
    let direction = range.direction;
    let mut counter = Vec::new();
    let mut trigger = None;
    for bar in five_minute_bars(&seen, session_open, config.bar_minutes)? {
        let Some(close) = bar.close else {
            continue;
        };
        if !(config.entry_window_start <= bar.close_time
            && bar.close_time <= config.entry_window_end)
        {
            continue;
        }
        let (broke_up, broke_down) = (close > up, close < down);
        let (with_trend, against) = match direction {
            Direction::Up => (broke_up, broke_down),
            Direction::Down => (broke_down, broke_up),
        };
        if with_trend {
            trigger = Some(Break {
                close_time: bar.close_time,
                close,
                direction,
            });
            break;
        }
        if against {
            counter.push(Break {
                close_time: bar.close_time,
                close,
                direction: direction.opposite(),
            });
        }
    }
    let done = ist(now).time() >= add_seconds(minute_after(config.entry_window_end), grace_seconds);
    Ok(TriggerScan {
        level_up: up,
        level_down: down,
        window_closed: trigger.is_none() && done,
        trigger,
        counter_breaks: counter,
        last_close: seen.last().map(|b| b.close),
    })
}

/// `04` §4.3: a CE `itm_steps` below ATM on an up-trend, a PE above it on a down-trend.
pub fn contract_for(
    view: &ChainView,
    direction: Direction,
    itm_steps: i64,
) -> CoreResult<(Decimal, OptionType)> {
    let offset = num::muli(view.step, itm_steps)?;
    Ok(match direction {
        Direction::Up => (num::sub(view.atm, offset)?, OptionType::Ce),
        Direction::Down => (num::add(view.atm, offset)?, OptionType::Pe),
    })
}

/// Every input of `build` besides the view, the direction and the configs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LongBuild {
    /// The master's lot size.
    pub lot_size: Option<i64>,
    /// The sleeve's book.
    pub book: SleeveBook,
    /// Expiry.
    pub expiry: NaiveDate,
    /// Paused.
    pub paused: bool,
}

/// The long a plan would carry, or the first refusal (`04` §4/§6/§7).
pub fn build(
    view: Option<&ChainView>,
    direction: Direction,
    inputs: &LongBuild,
    config: &DirectionalConfig,
    options: &OptionsConfig,
    ceilings: &OptionsCeilings,
) -> CoreResult<Candidate> {
    let structure = Structure::LongOption;
    let expiry = inputs.expiry;
    let refuse = |code: Rejection, message: String, known: Known| {
        Ok(rejected(structure, expiry, code, message, known))
    };
    let bare = Known {
        direction: Some(direction),
        ..Known::default()
    };
    if inputs.paused {
        return refuse(
            Rejection::RejectedPaused,
            "the sleeve or book is paused".to_owned(),
            bare,
        );
    }
    let Some(view) = view else {
        return refuse(
            Rejection::RejectedNoChain,
            "no chain for the expiry".to_owned(),
            bare,
        );
    };
    let lot_size = match inputs.lot_size {
        Some(l) if l > 0 => l,
        _ => {
            return refuse(
                Rejection::RejectedNoLotSize,
                "the master has no lot size".to_owned(),
                bare,
            );
        }
    };
    let (strike, option_type) = contract_for(view, direction, config.itm_steps)?;
    let priced = match view.get(strike, option_type) {
        Some(p) if p.quote.bid.is_some() && p.quote.ask.is_some() => p,
        _ => {
            return refuse(
                Rejection::RejectedNoContract,
                format!(
                    "{} {} is not quoted two-sided",
                    py_str(strike),
                    option_type.value()
                ),
                Known {
                    direction: Some(direction),
                    lot_size: Some(lot_size),
                    ..Known::default()
                },
            );
        }
    };
    let role = match option_type {
        OptionType::Ce => LegRole::LongCall,
        OptionType::Pe => LegRole::LongPut,
    };
    let the_leg = leg(role, priced, view.tick, &options.execution)?;
    let entry = the_leg.limit_price;
    let legs = vec![the_leg.clone()];
    let known = |extra: BTreeMap<String, String>| Known {
        direction: Some(direction),
        legs: legs.clone(),
        points: Some(entry),
        lot_size: Some(lot_size),
        extra,
    };
    let delta = priced.delta();
    let in_band = match delta {
        None => false,
        Some(d) => {
            let a = num::abs(from_float(d)?)?;
            config.delta_min <= a && a <= config.delta_max
        }
    };
    if !in_band {
        let shown = delta.map_or_else(|| "unknown".to_owned(), |d| format!("{:.4}", d.abs()));
        return refuse(
            Rejection::RejectedDelta,
            format!(
                "|delta| {shown} outside [{}, {}]",
                py_str(config.delta_min),
                py_str(config.delta_max)
            ),
            known(BTreeMap::new()),
        );
    }
    let per_lot = risk_per_lot_long(
        entry,
        config.stop_frac,
        lot_size,
        config.reserve_per_lot_inr,
    )?;
    let sizing = size_for(
        &inputs.book,
        config.risk_per_trade_pct,
        config.max_lots,
        per_lot,
        Some(lot_size),
        &options.sizing,
        ceilings,
    )?;
    let mut extra = BTreeMap::new();
    extra.insert(
        "gap_through_per_lot_inr".to_owned(),
        py_str(gap_through_long(entry, lot_size)?),
    );
    if let Some(code) = sizing.rejection {
        return refuse(code.into(), sizing.message, known(extra));
    }
    if !premium_cap_ok(
        sizing.lots,
        entry,
        lot_size,
        inputs.book.sleeve_capital_inr,
        config.premium_cap_pct,
    )? {
        return refuse(
            Rejection::RejectedPremiumCap,
            format!(
                "{} lots * ₹{} * {lot_size} > {} % of capital",
                sizing.lots,
                py_str(entry),
                py_str(config.premium_cap_pct)
            ),
            known(extra),
        );
    }
    let quantity = sizing.lots * lot_size;
    if !is_liquid(
        &priced.quote,
        quantity,
        the_leg.entry_side(),
        lot_size,
        &options.chain,
        false,
    )?
    .ok
    {
        return refuse(
            Rejection::RejectedIlliquid,
            format!(
                "{} {} is not liquid for {quantity} units",
                py_str(strike),
                option_type.value()
            ),
            known(extra),
        );
    }
    let trip = round_trip(
        &legs,
        quantity,
        view.tick,
        &options.costs,
        &options.execution,
    )?;
    let gain = expected_gain_o2(entry, quantity, config.target_frac)?;
    let (share, passes) = cost_gate(trip, gain, config.cost_share_max)?;
    extra.insert(
        "gap_through_inr".to_owned(),
        py_str(num::muli(gap_through_long(entry, lot_size)?, sizing.lots)?),
    );
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
        points: Some(entry),
        lot_size: Some(lot_size),
        lots: if passes { sizing.lots } else { 0 },
        sizing_mode: Some(sizing.sizing_mode),
        risk_per_lot_inr: Some(per_lot),
        r_inr: Some(sizing.r_inr),
        max_loss_inr: Some(num::muli(num::mul(entry, config.stop_frac)?, quantity)?),
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

/// O2's exit codes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum LongExit {
    /// The clock.
    HardExit,
    /// The stop.
    Stop,
    /// A close back inside the range.
    Invalidated,
    /// The target.
    Target,
    /// The time stop.
    TimeStop,
    /// The operator's button.
    Manual,
}

impl LongExit {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            LongExit::HardExit => "HARD_EXIT",
            LongExit::Stop => "STOP",
            LongExit::Invalidated => "INVALIDATED",
            LongExit::Target => "TARGET",
            LongExit::TimeStop => "TIME_STOP",
            LongExit::Manual => "MANUAL",
        }
    }
}

/// `HARD_EXIT > STOP > INVALIDATED > TARGET > TIME_STOP > MANUAL`.
pub const PRECEDENCE: [LongExit; 6] = [
    LongExit::HardExit,
    LongExit::Stop,
    LongExit::Invalidated,
    LongExit::Target,
    LongExit::TimeStop,
    LongExit::Manual,
];

/// A completed 5-minute close back inside the opening range.
pub fn invalidated(
    five_minute_close: Option<Decimal>,
    direction: Direction,
    or_high: Decimal,
    or_low: Decimal,
) -> bool {
    match (five_minute_close, direction) {
        (None, _) => false,
        (Some(c), Direction::Up) => c < or_high,
        (Some(c), Direction::Down) => c > or_low,
    }
}

/// One tick's marks for the long.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LongTick {
    /// E, the entry fill.
    pub entry: Decimal,
    /// The option's bid.
    pub bid: Decimal,
    /// When it was entered.
    pub entered_at: DateTime<FixedOffset>,
    /// Now.
    pub now: DateTime<FixedOffset>,
    /// Invalidated.
    pub is_invalidated: bool,
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

/// One tick's exit for the long, or `None`.
pub fn exit_decision(
    tick: &LongTick,
    config: &DirectionalConfig,
    options: &OptionsConfig,
) -> CoreResult<Option<LongExit>> {
    let mut fired = Vec::new();
    if ist(&tick.now).time() >= config.hard_exit_time {
        fired.push(LongExit::HardExit);
    }
    let stop = num::mul(tick.entry, num::sub(Decimal::ONE, config.stop_frac)?)?;
    if tick.bid <= stop
        || trade_breached(tick.marked_loss_inr, tick.risk_budget_inr, &options.risk)?
    {
        fired.push(LongExit::Stop);
    }
    if tick.is_invalidated {
        fired.push(LongExit::Invalidated);
    }
    if tick.bid >= num::mul(tick.entry, num::add(Decimal::ONE, config.target_frac)?)? {
        fired.push(LongExit::Target);
    }
    let held = ist(&tick.now) - ist(&tick.entered_at);
    let min_price = num::mul(
        tick.entry,
        num::add(Decimal::ONE, config.time_stop_min_gain)?,
    )?;
    if held >= TimeDelta::minutes(config.time_stop_minutes) && tick.bid < min_price {
        fired.push(LongExit::TimeStop);
    }
    if tick.manual {
        fired.push(LongExit::Manual);
    }
    let kept = drop_on_stale(&fired, &[LongExit::Target], tick.stale);
    first_by_precedence(&kept, &PRECEDENCE)
}
