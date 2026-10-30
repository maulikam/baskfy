//! Port of `baskfy_core.options.condor`: O1, the hedged condor on an expiry day (`04` §3).

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::options::bars::{
    Bar, bar_at, closed, efficiency_ratio, er_points, high_low, pct, window, window_settled,
};
use crate::options::chain::is_liquid;
use crate::options::config::{OptionType, OptionsCeilings, OptionsConfig, Side};
use crate::options::costs::expected_gain_o1;
use crate::options::execution::{LegRole, drop_on_stale, first_by_precedence};
use crate::options::num::{self, from_float, py_str};
use crate::options::sizing::risk_per_lot_condor;
use crate::options::structures::{
    Candidate, CandidateLeg, ChainView, Known, PricedQuote, Rejection, SleeveBook, Structure,
    cost_gate, leg, rejected, round_trip, size_for,
};

pub use crate::options::config::{CondorConfig, CondorVariant, RiskConfig};
pub use crate::options::risk::trade_breached;

/// Condor `04` §2.1-§2.6, in the order they are reported.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum GateReason {
    /// The opening gap is too big.
    GapTooBig,
    /// The observation range is too wide.
    RangeTooWide,
    /// The last close left the opening range.
    NotContained,
    /// The efficiency ratio is too high.
    ErTooHigh,
    /// An event day.
    EventDay,
    /// The window is incomplete.
    IncompleteObservation,
}

impl GateReason {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            GateReason::GapTooBig => "GAP_TOO_BIG",
            GateReason::RangeTooWide => "RANGE_TOO_WIDE",
            GateReason::NotContained => "NOT_CONTAINED",
            GateReason::ErTooHigh => "ER_TOO_HIGH",
            GateReason::EventDay => "EVENT_DAY",
            GateReason::IncompleteObservation => "INCOMPLETE_OBSERVATION",
        }
    }
}

/// What the gate measured.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GateNumbers {
    /// Previous close.
    pub prev_close: Decimal,
    /// Bars in the observation window.
    pub bars: i64,
    /// The 09:15 open.
    pub open_0915: Option<Decimal>,
    /// The gap, percent.
    pub gap_pct: Option<Decimal>,
    /// Observation high.
    pub obs_high: Option<Decimal>,
    /// Observation low.
    pub obs_low: Option<Decimal>,
    /// Observation range, percent.
    pub range_pct: Option<Decimal>,
    /// Opening range high.
    pub or_high: Option<Decimal>,
    /// Opening range low.
    pub or_low: Option<Decimal>,
    /// The last close.
    pub last_close: Option<Decimal>,
    /// Inside the opening range.
    pub contained: Option<bool>,
    /// Kaufman's ER.
    pub er: Option<Decimal>,
}

/// Condor §2's verdict; `trade` iff final with no reason.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DayVerdict {
    /// The window is settled.
    pub final_: bool,
    /// Every reason.
    pub reasons: Vec<GateReason>,
    /// The numbers.
    pub numbers: GateNumbers,
}

impl DayVerdict {
    /// Final with no reason.
    pub fn trade(&self) -> bool {
        self.final_ && self.reasons.is_empty()
    }
}

/// Condor `04` §2 over the closed bars of the observation window.
pub fn observe(
    bars: &[Bar],
    prev_close: Decimal,
    now: &DateTime<FixedOffset>,
    event_day: bool,
    config: &CondorConfig,
    grace_seconds: i64,
) -> CoreResult<DayVerdict> {
    if prev_close <= Decimal::ZERO {
        return Err(CoreError::InvalidInput(
            "the previous close must be positive".to_owned(),
        ));
    }
    let seen = closed(bars, now);
    let obs = window(&seen, config.observation_start, config.observation_end);
    let opening = window(&seen, config.observation_start, config.opening_range_end);
    let open_0915 = bar_at(&obs, config.observation_start).map(|b| b.open);
    let gap = open_0915
        .map(|o| {
            num::sub(o, prev_close)
                .and_then(|g| pct(g, prev_close))
                .and_then(num::abs)
        })
        .transpose()?;
    let hl = high_low(&obs);
    let obs_range = hl
        .map(|(h, l)| num::sub(h, l).and_then(|r| pct(r, prev_close)))
        .transpose()?;
    let or_hl = high_low(&opening);
    let opening_done = bar_at(&opening, config.opening_range_end).is_some();
    let last = obs.last().map(|b| b.close);
    let contained = match (or_hl, last) {
        (Some((h, l)), Some(c)) if opening_done => Some(l <= c && c <= h),
        _ => None,
    };
    let er = if obs.len() > 1 {
        Some(efficiency_ratio(&er_points(&obs))?)
    } else {
        None
    };
    let n_obs = i64::try_from(obs.len()).unwrap_or(i64::MAX);
    let complete = n_obs >= config.min_bars && bar_at(&obs, config.observation_end).is_some();
    let final_ = window_settled(&obs, config.observation_end, now, grace_seconds);
    let mut reasons = Vec::new();
    if gap.is_some_and(|g| g > config.gap_max_pct) {
        reasons.push(GateReason::GapTooBig);
    }
    if obs_range.is_some_and(|r| r > config.range_max_pct) {
        reasons.push(GateReason::RangeTooWide);
    }
    if complete && contained == Some(false) {
        reasons.push(GateReason::NotContained);
    }
    if complete && er.is_some_and(|e| e > config.er_max) {
        reasons.push(GateReason::ErTooHigh);
    }
    if event_day {
        reasons.push(GateReason::EventDay);
    }
    if final_ && !complete {
        reasons.push(GateReason::IncompleteObservation);
    }
    Ok(DayVerdict {
        final_,
        reasons,
        numbers: GateNumbers {
            prev_close,
            bars: n_obs,
            open_0915,
            gap_pct: gap,
            obs_high: hl.map(|x| x.0),
            obs_low: hl.map(|x| x.1),
            range_pct: obs_range,
            or_high: or_hl.map(|x| x.0),
            or_low: or_hl.map(|x| x.1),
            last_close: last,
            contained,
            er,
        },
    })
}

fn abs_delta(priced: &PricedQuote) -> CoreResult<Option<Decimal>> {
    priced
        .delta()
        .map(|d| from_float(d).and_then(num::abs))
        .transpose()
}

/// Condor §4.1-§4.2: the liquid short in the delta band and beyond the opening range, nearest the target.
pub fn select_short<'a>(
    view: &'a ChainView,
    option_type: OptionType,
    bound: Decimal,
    quantity: i64,
    lot_size: i64,
    config: &CondorConfig,
    options: &OptionsConfig,
) -> CoreResult<Option<&'a PricedQuote>> {
    let mut best: Option<(Decimal, Decimal, &PricedQuote)> = None;
    for priced in view.of_type(option_type) {
        let Some(delta) = abs_delta(priced)? else {
            continue;
        };
        if !(config.delta_min <= delta && delta <= config.delta_max) {
            continue;
        }
        let beyond = match option_type {
            OptionType::Ce => priced.strike() > num::add(bound, config.strike_buffer_points)?,
            OptionType::Pe => priced.strike() < num::sub(bound, config.strike_buffer_points)?,
        };
        if !beyond {
            continue;
        }
        if !is_liquid(
            &priced.quote,
            quantity,
            Side::Sell,
            lot_size,
            &options.chain,
            false,
        )?
        .ok
        {
            continue;
        }
        let further = match option_type {
            OptionType::Ce => num::neg(priced.strike())?,
            OptionType::Pe => priced.strike(),
        };
        let key = (num::abs(num::sub(delta, config.delta_target)?)?, further);
        if best.as_ref().is_none_or(|(a, b, _)| key < (*a, *b)) {
            best = Some((key.0, key.1, priced));
        }
    }
    Ok(best.map(|(_, _, p)| p))
}

fn wing<'a>(
    view: &'a ChainView,
    short: &PricedQuote,
    width: Decimal,
    quantity: i64,
    lot_size: i64,
    options: &OptionsConfig,
) -> CoreResult<Option<&'a PricedQuote>> {
    let strike = match short.option_type() {
        OptionType::Ce => num::add(short.strike(), width)?,
        OptionType::Pe => num::sub(short.strike(), width)?,
    };
    let Some(w) = view.get(strike, short.option_type()) else {
        return Ok(None);
    };
    if w.quote.bid.is_none() || w.quote.ask.is_none() {
        return Ok(None);
    }
    let ok = is_liquid(
        &w.quote,
        quantity,
        Side::Buy,
        lot_size,
        &options.chain,
        true,
    )?
    .ok;
    Ok(if ok { Some(w) } else { None })
}

/// Condor §4.4: shorts at bid, wings at ask.
pub fn credit_points(
    short_call: &PricedQuote,
    short_put: &PricedQuote,
    long_call: &PricedQuote,
    long_put: &PricedQuote,
) -> CoreResult<Decimal> {
    for q in [short_call, short_put, long_call, long_put] {
        if q.quote.bid.is_none() || q.quote.ask.is_none() {
            return Err(CoreError::InvalidInput(
                "a condor leg needs a two-sided quote".to_owned(),
            ));
        }
    }
    let z = Decimal::ZERO;
    let total = num::add(
        short_call.quote.bid.unwrap_or(z),
        short_put.quote.bid.unwrap_or(z),
    )?;
    let total = num::sub(total, long_call.quote.ask.unwrap_or(z))?;
    num::sub(total, long_put.quote.ask.unwrap_or(z))
}

/// Every input of `build` besides the view and the configs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CondorBuild {
    /// Opening range high.
    pub or_high: Decimal,
    /// Opening range low.
    pub or_low: Decimal,
    /// The master's lot size.
    pub lot_size: Option<i64>,
    /// The sleeve's book.
    pub book: SleeveBook,
    /// The slot is free.
    pub slot_free: bool,
    /// The sleeve or book is paused.
    pub paused: bool,
    /// Expiry.
    pub expiry: NaiveDate,
}

/// The condor a plan would carry, or the first refusal (`04` §3/§6/§7).
pub fn build(
    view: Option<&ChainView>,
    inputs: &CondorBuild,
    config: &CondorConfig,
    options: &OptionsConfig,
    ceilings: &OptionsCeilings,
) -> CoreResult<Candidate> {
    let structure = Structure::IronCondor;
    let expiry = inputs.expiry;
    let refuse = |code: Rejection, message: String, known: Known| {
        Ok(rejected(structure, expiry, code, message, known))
    };
    if inputs.paused {
        return refuse(
            Rejection::RejectedPaused,
            "the sleeve or book is paused".to_owned(),
            Known::default(),
        );
    }
    if !inputs.slot_free {
        return refuse(
            Rejection::RejectedSlotTaken,
            "another sleeve holds today's slot".to_owned(),
            Known::default(),
        );
    }
    let Some(view) = view else {
        return refuse(
            Rejection::RejectedNoChain,
            "no chain for the expiry".to_owned(),
            Known::default(),
        );
    };
    let lot_size = match inputs.lot_size {
        Some(l) if l > 0 => l,
        _ => {
            return refuse(
                Rejection::RejectedNoLotSize,
                "the master has no lot size".to_owned(),
                Known::default(),
            );
        }
    };
    let sized =
        |legs: Vec<CandidateLeg>, points: Option<Decimal>, extra: BTreeMap<String, String>| Known {
            direction: None,
            legs,
            points,
            lot_size: Some(lot_size),
            extra,
        };
    let band = format!(
        "[{}, {}]",
        py_str(config.delta_min),
        py_str(config.delta_max)
    );
    let Some(short_call) = select_short(
        view,
        OptionType::Ce,
        inputs.or_high,
        lot_size,
        lot_size,
        config,
        options,
    )?
    else {
        return refuse(
            Rejection::RejectedNoShortCall,
            format!(
                "no liquid CE with |delta| in {band} above {}",
                py_str(num::add(inputs.or_high, config.strike_buffer_points)?)
            ),
            sized(Vec::new(), None, BTreeMap::new()),
        );
    };
    let Some(short_put) = select_short(
        view,
        OptionType::Pe,
        inputs.or_low,
        lot_size,
        lot_size,
        config,
        options,
    )?
    else {
        return refuse(
            Rejection::RejectedNoShortPut,
            format!(
                "no liquid PE with |delta| in {band} below {}",
                py_str(num::sub(inputs.or_low, config.strike_buffer_points)?)
            ),
            sized(Vec::new(), None, BTreeMap::new()),
        );
    };
    let width = config.wing_width_points;
    let long_call = wing(view, short_call, width, lot_size, lot_size, options)?;
    let long_put = wing(view, short_put, width, lot_size, lot_size, options)?;
    let (Some(long_call), Some(long_put)) = (long_call, long_put) else {
        return refuse(
            Rejection::RejectedNoWing,
            format!(
                "a wing {} points out is missing, one-sided or too thin",
                py_str(width)
            ),
            sized(Vec::new(), None, BTreeMap::new()),
        );
    };
    let tick = view.tick;
    let ex = &options.execution;
    let legs = vec![
        leg(LegRole::LongPut, long_put, tick, ex)?,
        leg(LegRole::LongCall, long_call, tick, ex)?,
        leg(LegRole::ShortPut, short_put, tick, ex)?,
        leg(LegRole::ShortCall, short_call, tick, ex)?,
    ];
    let quotes = [long_put, long_call, short_put, short_call];
    let credit = credit_points(short_call, short_put, long_call, long_put)?;
    let floor = num::mul(config.credit_floor_frac, width)?;
    let mut extra = BTreeMap::new();
    extra.insert("credit_floor_points".to_owned(), py_str(floor));
    extra.insert(
        "credit_target_points".to_owned(),
        py_str(num::mul(config.credit_target_frac, width)?),
    );
    extra.insert("wing_width_points".to_owned(), py_str(width));
    if credit < floor {
        return refuse(
            Rejection::RejectedCredit,
            format!(
                "credit {} < floor {} ({} * {})",
                py_str(credit),
                py_str(floor),
                py_str(config.credit_floor_frac),
                py_str(width)
            ),
            sized(legs, Some(credit), extra),
        );
    }
    let per_lot = risk_per_lot_condor(width, credit, lot_size, config.reserve_per_lot_inr)?;
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
        return refuse(
            code.into(),
            sizing.message,
            sized(legs, Some(credit), extra),
        );
    }
    let quantity = sizing.lots * lot_size;
    let mut thin = Vec::new();
    for (lg, q) in legs.iter().zip(quotes) {
        let wing_leg = lg.role.is_long();
        if !is_liquid(
            &q.quote,
            quantity,
            lg.entry_side(),
            lot_size,
            &options.chain,
            wing_leg,
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
            sized(legs, Some(credit), extra),
        );
    }
    let trip = round_trip(&legs, quantity, tick, &options.costs, ex)?;
    let credit_inr = num::muli(credit, quantity)?;
    let gain = expected_gain_o1(credit_inr, config.profit_take_frac)?;
    let (share, passes) = cost_gate(trip, gain, config.cost_share_max)?;
    let mut warnings = Vec::new();
    if num::div(trip, Decimal::from(sizing.lots))? > config.reserve_per_lot_inr {
        warnings.push("RESERVE_EXCEEDED".to_owned());
    }
    extra.insert("credit_inr".to_owned(), py_str(num::paise(credit_inr)?));
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
        direction: None,
        legs,
        points: Some(credit),
        lot_size: Some(lot_size),
        lots: if passes { sizing.lots } else { 0 },
        sizing_mode: Some(sizing.sizing_mode),
        risk_per_lot_inr: Some(per_lot),
        r_inr: Some(sizing.r_inr),
        max_loss_inr: Some(num::muli(num::sub(width, credit)?, quantity)?),
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
        warnings,
        extra,
    })
}

/// Condor §7's `D`: shorts at ask, wings at bid, per unit.
pub fn cost_to_close(legs: &[CandidateLeg]) -> CoreResult<Decimal> {
    let mut total = Decimal::ZERO;
    for lg in legs {
        let (Some(bid), Some(ask)) = (lg.bid, lg.ask) else {
            return Err(CoreError::InvalidInput(
                "a mark needs a two-sided quote".to_owned(),
            ));
        };
        total = if lg.role.is_long() {
            num::add(total, num::neg(bid)?)?
        } else {
            num::add(total, ask)?
        };
    }
    Ok(total)
}

/// Ports `baskfy_core.options.condor.CondorExit`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CondorExit {
    /// The clock.
    HardExit,
    /// Spot touched a short strike.
    StrikeTouch,
    /// `D ≥ stop_frac * C`, or the budget breach.
    Stop,
    /// `D ≤ profit_take_frac * C`.
    Profit,
    /// The operator's button.
    Manual,
}

/// Condor §7.6; `MANUAL` is the operator's button and outranks nothing automatic.
pub const PRECEDENCE: [CondorExit; 5] = [
    CondorExit::HardExit,
    CondorExit::StrikeTouch,
    CondorExit::Stop,
    CondorExit::Profit,
    CondorExit::Manual,
];

/// One tick's marks — the keyword arguments of Python's `exit_decision` other than the two
/// configs, as one value.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CondorTick {
    /// C, the entry credit from fills (§4.8), points.
    pub entry_credit: Decimal,
    /// D, the cost to close now, points.
    pub cost_now: Decimal,
    /// The index level; `None` when no index tick has come yet (OP13.2).
    pub spot: Option<Decimal>,
    /// The short call's strike.
    pub short_call_strike: Decimal,
    /// The short put's strike.
    pub short_put_strike: Decimal,
    /// The tick's time; converted to IST wall clock before comparing.
    pub now: DateTime<FixedOffset>,
    /// Whether the mark is stale (§7.7).
    pub stale: bool,
    /// The marked loss, ₹, a loss positive.
    pub marked_loss_inr: Decimal,
    /// The trade's risk budget, ₹.
    pub risk_budget_inr: Decimal,
    /// The operator pressed close.
    #[serde(default)]
    pub manual: bool,
}

/// `now` as naive IST wall-clock (Python's `bars.ist`).
fn ist_time(now: &DateTime<FixedOffset>) -> CoreResult<NaiveTime> {
    let ist = FixedOffset::east_opt(5 * 3600 + 30 * 60)
        .ok_or_else(|| CoreError::InvalidInput("IST offset".to_string()))?;
    Ok(now.with_timezone(&ist).time())
}

/// One tick's exit, or `None`. `entry_credit` is C from fills (§4.8), `cost_now` is D.
///
/// `04` §9.2's budget breach closes as `STOP` whatever D/C says; a stale mark drops only
/// `PROFIT` (§7.7). An unknown `spot` cannot touch a strike, so only `STRIKE_TOUCH` is not
/// judged; every premium rule and the clock still are (DECISIONS-OP OP13.2).
///
/// Ports `baskfy_core.options.condor.exit_decision` (`options` narrowed to the `RiskConfig`
/// it reads).
pub fn exit_decision(
    tick: &CondorTick,
    config: &CondorConfig,
    risk: &RiskConfig,
) -> CoreResult<Option<CondorExit>> {
    let mut fired: Vec<CondorExit> = Vec::new();
    if ist_time(&tick.now)? >= config.hard_exit_time {
        fired.push(CondorExit::HardExit);
    }
    if let Some(spot) = tick.spot
        && (spot >= tick.short_call_strike || spot <= tick.short_put_strike)
    {
        fired.push(CondorExit::StrikeTouch);
    }
    if tick.cost_now >= num::mul(config.stop_frac, tick.entry_credit)?
        || trade_breached(tick.marked_loss_inr, tick.risk_budget_inr, risk)?
    {
        fired.push(CondorExit::Stop);
    }
    if tick.cost_now <= num::mul(config.profit_take_frac, tick.entry_credit)? {
        fired.push(CondorExit::Profit);
    }
    if tick.manual {
        fired.push(CondorExit::Manual);
    }
    let kept = drop_on_stale(&fired, &[CondorExit::Profit], tick.stale);
    first_by_precedence(&kept, &PRECEDENCE)
}
