//! Port of `baskfy_core.options.plan_o2`: the O2 plan builder, pure (`06` OP7).

use std::collections::BTreeSet;

use chrono::{DateTime, FixedOffset, NaiveDate};
use rust_decimal::Decimal;
use serde::Serialize;

use crate::decimal::Rounding;
use crate::error::{CoreError, CoreResult};
use crate::options::bars::{ist, minute_after};
use crate::options::calendar::{RoleReason, expiry_for_o2, lot_size_for, role};
use crate::options::chain::as_f64;
use crate::options::config::{
    DirectionalConfig, Mode, OptionsCeilings, OptionsConfig, SizingMode, Sleeve,
};
use crate::options::costs::ChargeBreakdown;
use crate::options::directional::{self, Break, DayFilters, LongBuild, Range, TriggerScan};
use crate::options::num::{self, at_ist, py_str};
use crate::options::plan::{
    Decided, PlanLeg, PlanState, STALE_CHAIN, Verdict, age_seconds, assert_never_naked, issued_ist,
    plan_costs, plan_id_for, plan_legs, q_even, strs,
};
use crate::options::scan::{
    DayContext, MarketDay, MinuteKey, Numbers, SnapshotBook, Value, priced_view, t,
};
use crate::options::session::plan_expires_at;
use crate::options::sizing::gap_through_long;
use crate::options::structures::{Candidate, Direction, Structure};

/// The window closed with no with-trend break: a skipped session.
pub const NO_TRIGGER: &str = "NO_TRIGGER";
/// Nothing has broken yet.
pub const NO_TRIGGER_YET: &str = "NO_TRIGGER_YET";
/// The master lists no expiry after today.
pub const NO_EXPIRY: &str = "NO_EXPIRY";
/// Before 09:30.
pub const BEFORE_ENTRY_WINDOW: &str = "BEFORE_ENTRY_WINDOW";
/// The opening range is not judged yet.
pub const RANGE_NOT_SETTLED: &str = "RANGE_NOT_SETTLED";
/// The trigger minute's chain is not stored yet.
pub const NO_DECISION_SNAPSHOT: &str = "NO_DECISION_SNAPSHOT";
/// A trigger nobody can act on.
pub const ENTRY_WINDOW_CLOSED: &str = "ENTRY_WINDOW_CLOSED";

/// The first phase's answer.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct O2Decision {
    /// Always O2.
    pub sleeve: Sleeve,
    /// The trade date.
    pub trade_date: NaiveDate,
    /// When decided.
    pub now: DateTime<FixedOffset>,
    /// The state.
    pub state: PlanState,
    /// Every reason.
    pub reasons: Vec<String>,
    /// The day's numbers.
    pub numbers: Numbers,
    /// The verdict.
    pub verdict: Option<Verdict>,
    /// Expiry.
    pub expiry: Option<NaiveDate>,
    /// The trend.
    pub direction: Option<Direction>,
    /// The trigger.
    pub trigger: Option<Break>,
    /// Opening range high.
    pub or_high: Option<Decimal>,
    /// Opening range low.
    pub or_low: Option<Decimal>,
    /// The candidate.
    pub candidate: Option<Candidate>,
    /// The leg.
    pub legs: Vec<PlanLeg>,
    /// The snapshot's minute.
    pub as_of_minute: Option<DateTime<FixedOffset>>,
    /// The itemised round trip.
    pub costs: Option<ChargeBreakdown>,
}

impl O2Decision {
    /// `PLANNED` or `SKIPPED` write a session.
    pub fn writes_session(&self) -> bool {
        matches!(self.state, PlanState::Planned | PlanState::Skipped)
    }
}

impl Decided for O2Decision {
    fn sleeve(&self) -> Sleeve {
        self.sleeve
    }
    fn trade_date(&self) -> NaiveDate {
        self.trade_date
    }
    fn legs(&self) -> &[PlanLeg] {
        &self.legs
    }
}

/// `04` §4.5 written out at plan time, on the option's bid.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct O2Exits {
    /// Stop.
    pub stop_price: Decimal,
    /// Target.
    pub target_price: Decimal,
    /// The opening range edge.
    pub invalidation_level: Decimal,
    /// Minutes before the time stop.
    pub time_stop_minutes: i64,
    /// The price the time stop needs.
    pub time_stop_min_price: Decimal,
    /// The hard exit.
    pub hard_exit_at: DateTime<FixedOffset>,
}

/// One `op_plan` row and its single leg.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct O2Plan {
    /// Deterministic id.
    pub plan_id: String,
    /// Always O2.
    pub sleeve: Sleeve,
    /// The trade date.
    pub trade_date: NaiveDate,
    /// Expiry.
    pub expiry: NaiveDate,
    /// Paper or live.
    pub mode: Mode,
    /// The structure.
    pub structure: Structure,
    /// The trend.
    pub direction: Direction,
    /// When issued.
    pub issued_at: DateTime<FixedOffset>,
    /// The entry window's end.
    pub entry_window_end: DateTime<FixedOffset>,
    /// When it lapses.
    pub expires_at: DateTime<FixedOffset>,
    /// Entry per unit.
    pub debit_points: Decimal,
    /// Lot size.
    pub lot_size: i64,
    /// Lots.
    pub lots: i64,
    /// How it was sized.
    pub sizing_mode: SizingMode,
    /// First-live half size.
    pub half_size: bool,
    /// Risk per lot.
    pub risk_per_lot_inr: Decimal,
    /// R.
    pub risk_budget_inr: Decimal,
    /// Premium paid.
    pub premium_inr: Decimal,
    /// Max loss at the stop.
    pub max_loss_inr: Decimal,
    /// The whole premium.
    pub gap_through_inr: Decimal,
    /// Profit target.
    pub profit_target_inr: Decimal,
    /// Stop.
    pub stop_inr: Decimal,
    /// Expected costs.
    pub expected_cost_inr: Decimal,
    /// Cost share.
    pub cost_share: Decimal,
    /// Itemised costs.
    pub costs: ChargeBreakdown,
    /// The exits.
    pub exits: O2Exits,
    /// Warnings.
    pub warnings: Vec<String>,
    /// The leg.
    pub legs: Vec<PlanLeg>,
    /// The snapshot's minute.
    pub as_of_minute: Option<DateTime<FixedOffset>>,
    /// The numbers.
    pub numbers: Numbers,
}

impl O2Plan {
    /// Units.
    pub fn quantity(&self) -> i64 {
        self.lots * self.lot_size
    }
}

/// The second phase's answer.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct O2Outcome {
    /// The decision.
    pub decision: O2Decision,
    /// The plan.
    pub plan: Option<O2Plan>,
    /// The state.
    pub state: PlanState,
    /// Every reason.
    pub reasons: Vec<String>,
}

/// `04` §4.1's numbers beside their thresholds.
pub fn day_numbers(
    filters: &DayFilters,
    config: &DirectionalConfig,
    expiry: Option<NaiveDate>,
) -> CoreResult<Numbers> {
    Ok(Numbers::of(vec![
        ("trend", filters.trend.map(|d| d.value().to_owned()).into()),
        ("ema", q_even(filters.ema, 2)?),
        ("trend_ema_days", config.trend_ema_days.into()),
        ("index", "NIFTY 50".into()),
        ("prev_close", q_even(filters.prev_close, 2)?),
        ("open_0915", q_even(filters.open_0915, 2)?),
        ("gap_pct", q_even(filters.gap_pct, 4)?),
        ("gap_max_pct", py_str(config.gap_max_pct).into()),
        ("or_high", q_even(filters.or_high, 2)?),
        ("or_low", q_even(filters.or_low, 2)?),
        ("or_pct", q_even(filters.or_pct, 4)?),
        ("or_max_pct", py_str(config.or_max_pct).into()),
        ("vix", q_even(filters.vix, 2)?),
        ("vix_max", py_str(config.vix_max).into()),
        ("bars", filters.bars.into()),
        ("expiry", expiry.map(|e| e.to_string()).into()),
    ]))
}

/// `04` §4.2's levels, the trigger and every counter-trend break.
pub fn trigger_numbers(trig: &TriggerScan, direction: Direction) -> CoreResult<Numbers> {
    let level = match direction {
        Direction::Up => trig.level_up,
        Direction::Down => trig.level_down,
    };
    let breaks = trig
        .counter_breaks
        .iter()
        .map(|b| {
            Ok(Numbers::of(vec![
                ("close_time", t(Some(b.close_time))),
                ("close", q_even(Some(b.close), 2)?),
                ("direction", b.direction.value().into()),
            ]))
        })
        .collect::<CoreResult<Vec<_>>>()?;
    let mut out = Numbers::of(vec![
        ("direction", direction.value().into()),
        ("trigger_level", q_even(Some(level), 2)?),
        ("level_up", q_even(Some(trig.level_up), 2)?),
        ("level_down", q_even(Some(trig.level_down), 2)?),
        ("last_close", q_even(trig.last_close, 2)?),
        ("counter_trend_breaks", Value::List(breaks)),
    ]);
    if let Some(trigger) = &trig.trigger {
        out.set("trigger_time", t(Some(trigger.close_time)));
        out.set("trigger_close", q_even(Some(trigger.close), 2)?);
    }
    Ok(out)
}

/// The minute a plan prices from: the one after the trigger bar's close minute.
pub fn decision_minute_for(trigger: &Break, day: NaiveDate) -> DateTime<FixedOffset> {
    at_ist(day, minute_after(trigger.close_time))
}

#[derive(Debug, Clone, Default)]
struct Rest {
    numbers: Numbers,
    expiry: Option<NaiveDate>,
    verdict: Option<Verdict>,
    direction: Option<Direction>,
    trigger: Option<Break>,
    or_high: Option<Decimal>,
    or_low: Option<Decimal>,
    candidate: Option<Candidate>,
    legs: Vec<PlanLeg>,
    as_of_minute: Option<DateTime<FixedOffset>>,
    costs: Option<ChargeBreakdown>,
}

fn decision(
    day: NaiveDate,
    now: &DateTime<FixedOffset>,
    state: PlanState,
    reasons: Vec<String>,
    rest: Rest,
) -> O2Decision {
    O2Decision {
        sleeve: Sleeve::O2,
        trade_date: day,
        now: *now,
        state,
        reasons,
        numbers: rest.numbers,
        verdict: rest.verdict,
        expiry: rest.expiry,
        direction: rest.direction,
        trigger: rest.trigger,
        or_high: rest.or_high,
        or_low: rest.or_low,
        candidate: rest.candidate,
        legs: rest.legs,
        as_of_minute: rest.as_of_minute,
        costs: rest.costs,
    }
}

/// Day filters, trigger, expiry, strike, delta, sizing and the cost test at `now`.
pub fn decide_o2(
    market: &MarketDay,
    context: &DayContext,
    snapshots: &mut SnapshotBook,
    now: &DateTime<FixedOffset>,
    options: &OptionsConfig,
    ceilings: &OptionsCeilings,
) -> CoreResult<O2Decision> {
    let cfg = &options.directional;
    let day = market.trade_date;
    let decided = |state, reasons, rest| Ok(decision(day, now, state, reasons, rest));
    let day_role = role(
        day,
        Sleeve::O2,
        &market.contracts,
        &context.event_days,
        market.trading_day,
        "NIFTY",
    )?;
    if !day_role.trades && day_role.reason != RoleReason::EventDay {
        return decided(
            PlanState::NoSession,
            strs(&[day_role.reason.value()]),
            Rest::default(),
        );
    }
    let expiry = expiry_for_o2(day, &market.contracts, &options.calendar.underlying);
    let with_expiry = || Rest {
        expiry,
        ..Rest::default()
    };
    if ist(now).time() < cfg.entry_window_start {
        return decided(
            PlanState::NotReady,
            strs(&[BEFORE_ENTRY_WINDOW]),
            with_expiry(),
        );
    }
    if !day_role.trades {
        return decided(
            PlanState::Skipped,
            strs(&[RoleReason::EventDay.value()]),
            Rest {
                verdict: Some(Verdict::Skip),
                ..with_expiry()
            },
        );
    }
    let filters = directional::day_filters(
        &market.bars,
        &market.daily_closes,
        market.vix_prev_close,
        now,
        false,
        cfg,
        options.chain.stale_scan_seconds,
    )?;
    let mut numbers = day_numbers(&filters, cfg, expiry)?;
    if !filters.final_ {
        return decided(
            PlanState::NotReady,
            strs(&[RANGE_NOT_SETTLED]),
            Rest {
                numbers,
                ..with_expiry()
            },
        );
    }
    let Some(direction) = filters.trend.filter(|_| filters.reasons.is_empty()) else {
        return decided(
            PlanState::Skipped,
            filters
                .reasons
                .iter()
                .map(|r| r.value().to_owned())
                .collect(),
            Rest {
                numbers,
                verdict: Some(Verdict::Skip),
                ..with_expiry()
            },
        );
    };
    let or_high = filters.or_high.unwrap_or(Decimal::ZERO);
    let or_low = filters.or_low.unwrap_or(Decimal::ZERO);
    let trig = directional::find_trigger(
        &market.bars,
        Range {
            or_high,
            or_low,
            direction,
        },
        now,
        cfg,
        options.calendar.market_open,
        options.chain.stale_scan_seconds,
    )?;
    numbers.extend(trigger_numbers(&trig, direction)?);
    let common = Rest {
        expiry,
        numbers,
        direction: Some(direction),
        or_high: Some(or_high),
        or_low: Some(or_low),
        ..Rest::default()
    };
    let Some(trigger) = trig.trigger.clone() else {
        if trig.window_closed {
            return decided(
                PlanState::Skipped,
                strs(&[NO_TRIGGER]),
                Rest {
                    verdict: Some(Verdict::Skip),
                    ..common
                },
            );
        }
        return decided(PlanState::NotReady, strs(&[NO_TRIGGER_YET]), common);
    };
    let common = Rest {
        trigger: Some(trigger.clone()),
        ..common
    };
    if ist(now).time() >= cfg.entry_window_end {
        return decided(
            PlanState::WindowClosed,
            strs(&[ENTRY_WINDOW_CLOSED]),
            common,
        );
    }
    let Some(expiry) = expiry else {
        return decided(PlanState::NotReady, strs(&[NO_EXPIRY]), common);
    };
    let minute = decision_minute_for(&trigger, day);
    let Some(snapshot) = snapshots.get(Some(minute)) else {
        return decided(PlanState::NotReady, strs(&[NO_DECISION_SNAPSHOT]), common);
    };
    let view = priced_view(Some(&snapshot), Some(expiry), market, options)?;
    let mine = context.of(Sleeve::O2);
    let candidate = directional::build(
        view.as_ref(),
        direction,
        &LongBuild {
            lot_size: lot_size_for(&market.contracts, expiry, &options.calendar.underlying),
            book: mine.book.clone(),
            expiry,
            paused: mine.paused,
        },
        cfg,
        options,
        ceilings,
    )?;
    let common = Rest {
        verdict: Some(Verdict::Trade),
        candidate: Some(candidate.clone()),
        as_of_minute: Some(snapshot.ts),
        ..common
    };
    if let Some(code) = candidate.rejection {
        return decided(PlanState::Skipped, strs(&[code.value()]), common);
    }
    if age_seconds(now, &snapshot.ts) > as_f64(options.chain.stale_scan_seconds) {
        return decided(PlanState::Skipped, strs(&[STALE_CHAIN]), common);
    }
    let Some(view) = view else {
        return Err(CoreError::InvalidInput(
            "a viable candidate without a chain".to_owned(),
        ));
    };
    let legs = plan_legs(&candidate, &market.contracts, Some(&snapshot))?;
    let costs = plan_costs(&candidate, view.tick, options)?;
    decided(
        PlanState::Planned,
        Vec::new(),
        Rest {
            legs,
            costs: Some(costs),
            ..common
        },
    )
}

/// The snapshot minutes [`decide_o2`] will ask for at `now`.
pub fn wanted_minutes(
    market: &MarketDay,
    context: &DayContext,
    now: &DateTime<FixedOffset>,
    options: &OptionsConfig,
    ceilings: &OptionsCeilings,
) -> CoreResult<BTreeSet<MinuteKey>> {
    let mut book = SnapshotBook::default();
    decide_o2(market, context, &mut book, now, options, ceilings)?;
    Ok(book.asked)
}

/// `04` §4.5's levels for a long bought at `entry`.
pub fn exits_for(
    entry: Decimal,
    day: NaiveDate,
    invalidation_level: Decimal,
    config: &DirectionalConfig,
) -> CoreResult<O2Exits> {
    let at = |frac: Decimal, sign_up: bool| -> CoreResult<Decimal> {
        let factor = if sign_up {
            num::add(Decimal::ONE, frac)?
        } else {
            num::sub(Decimal::ONE, frac)?
        };
        num::paise(num::mul(entry, factor)?)
    };
    Ok(O2Exits {
        stop_price: at(config.stop_frac, false)?,
        target_price: at(config.target_frac, true)?,
        invalidation_level,
        time_stop_minutes: config.time_stop_minutes,
        time_stop_min_price: at(config.time_stop_min_gain, true)?,
        hard_exit_at: at_ist(day, config.hard_exit_time),
    })
}

/// The `plan_id`, the 30-minute expiry and `04` §4.5's exits — no broker read.
pub fn finalize_o2(
    decision: &O2Decision,
    user_id: i64,
    mode: Mode,
    options: &OptionsConfig,
) -> CoreResult<O2Outcome> {
    if decision.state != PlanState::Planned {
        return Ok(O2Outcome {
            decision: decision.clone(),
            plan: None,
            state: decision.state,
            reasons: decision.reasons.clone(),
        });
    }
    let (Some(candidate), Some(expiry), Some(costs), Some(direction)) = (
        &decision.candidate,
        decision.expiry,
        &decision.costs,
        decision.direction,
    ) else {
        return Err(CoreError::InvalidInput(
            "a PLANNED decision carries its candidate, expiry, direction and costs".to_owned(),
        ));
    };
    assert_never_naked(&decision.legs)?;
    let cfg = &options.directional;
    let issued = issued_ist(&decision.now);
    let window_end = at_ist(decision.trade_date, cfg.entry_window_end);
    let expires = plan_expires_at(
        &issued,
        cfg.entry_window_end,
        options.execution.plan_ttl_minutes,
    );
    let lot_size = candidate.lot_size.unwrap_or(0);
    let quantity = candidate.lots * lot_size;
    let zero = Decimal::ZERO;
    let entry = candidate.points.unwrap_or(zero);
    let invalidation = match direction {
        Direction::Up => decision.or_high,
        Direction::Down => decision.or_low,
    }
    .filter(|v| !v.is_zero())
    .unwrap_or(zero);
    let plan = O2Plan {
        plan_id: plan_id_for(user_id, decision),
        sleeve: Sleeve::O2,
        trade_date: decision.trade_date,
        expiry,
        mode,
        structure: candidate.structure,
        direction,
        issued_at: issued,
        entry_window_end: window_end,
        expires_at: expires,
        debit_points: entry,
        lot_size,
        lots: candidate.lots,
        sizing_mode: candidate.sizing_mode.unwrap_or(SizingMode::PaperOneLot),
        half_size: candidate.half_size,
        risk_per_lot_inr: num::paise(candidate.risk_per_lot_inr.unwrap_or(zero))?,
        risk_budget_inr: num::paise(candidate.r_inr.unwrap_or(zero))?,
        premium_inr: num::paise(num::muli(entry, quantity)?)?,
        max_loss_inr: num::paise(candidate.max_loss_inr.unwrap_or(zero))?,
        gap_through_inr: num::paise(num::muli(
            gap_through_long(entry, lot_size)?,
            candidate.lots,
        )?)?,
        profit_target_inr: num::paise(num::muli(num::mul(cfg.target_frac, entry)?, quantity)?)?,
        stop_inr: num::paise(num::muli(num::mul(cfg.stop_frac, entry)?, quantity)?)?,
        expected_cost_inr: costs.total()?,
        cost_share: num::quantize(candidate.cost_share.unwrap_or(zero), 4, Rounding::HalfEven)?,
        costs: costs.clone(),
        exits: exits_for(entry, decision.trade_date, invalidation, cfg)?,
        warnings: candidate.warnings.clone(),
        legs: decision.legs.clone(),
        as_of_minute: decision.as_of_minute,
        numbers: decision.numbers.clone(),
    };
    Ok(O2Outcome {
        decision: decision.clone(),
        plan: Some(plan),
        state: PlanState::Planned,
        reasons: Vec::new(),
    })
}

/// The plan's one leg.
pub fn leg_of(plan: &O2Plan) -> CoreResult<&PlanLeg> {
    match plan.legs.as_slice() {
        [leg] => Ok(leg),
        _ => Err(CoreError::InvalidInput(
            "an O2 plan has exactly one leg".to_owned(),
        )),
    }
}

/// `25000 CE` — the strike and right.
pub fn contract_label(leg: &PlanLeg) -> CoreResult<String> {
    Ok(format!(
        "{} {}",
        py_str(num::quantize(leg.strike, 0, Rounding::HalfEven)?),
        leg.option_type.value()
    ))
}
