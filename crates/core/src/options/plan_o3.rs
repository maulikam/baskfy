//! Port of `baskfy_core.options.plan_o3`: the O3 plan builder, pure (`06` OP8).

use std::collections::BTreeSet;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime};
use rust_decimal::Decimal;
use serde::Serialize;

use crate::decimal::Rounding;
use crate::error::{CoreError, CoreResult};
use crate::options::bars::{ist, minute_after};
use crate::options::calendar::{RoleReason, expiry_for_o1_o3, lot_size_for, role};
use crate::options::chain::as_f64;
use crate::options::config::{
    ExpirySetupsConfig, Mode, OptionsCeilings, OptionsConfig, SizingMode, Sleeve,
};
use crate::options::costs::ChargeBreakdown;
use crate::options::expiry_setups::{self, SetupClock, SpreadBuild};
use crate::options::num::{self, at_ist, py_str};
use crate::options::plan::{
    Baskets, Decided, Finalize, MarginLeg, MarginQuote, PlanLeg, PlanState, STALE_CHAIN, Verdict,
    age_seconds, assert_never_naked, issued_ist, margin_gate, plan_costs, plan_id_for, plan_legs,
    q_even, strs,
};
use crate::options::scan::{
    DayContext, MarketDay, MinuteKey, Numbers, SnapshotBook, Value, priced_view, t,
};
use crate::options::session::{SessionState, plan_expires_at};
use crate::options::sizing::MarginCheck;
use crate::options::structures::{Candidate, Direction, Structure};

/// The O3 setups.
pub const O3_SLEEVES: [Sleeve; 2] = [Sleeve::O3a, Sleeve::O3b];
/// The setup is switched off.
pub const SETUP_DISABLED: &str = "SETUP_DISABLED";
/// No previous NIFTY 50 close.
pub const NO_PREV_CLOSE: &str = "NO_PREV_CLOSE";
/// Before the setup's first decision time.
pub const BEFORE_PLAN_TIME: &str = "BEFORE_PLAN_TIME";
/// The morning window is not judged yet.
pub const WINDOW_NOT_SETTLED: &str = "WINDOW_NOT_SETTLED";
/// O3-A's window closed with no qualifying break.
pub const NO_TRIGGER: &str = "NO_TRIGGER";
/// O3-A's window is open and nothing has broken yet.
pub const NO_TRIGGER_YET: &str = "NO_TRIGGER_YET";
/// The decision minute's chain is not stored yet.
pub const NO_DECISION_SNAPSHOT: &str = "NO_DECISION_SNAPSHOT";
/// First asked after the entry window with nothing decided.
pub const ENTRY_WINDOW_CLOSED: &str = "ENTRY_WINDOW_CLOSED";
/// O3-B planned or traded today, so O3-A may not.
pub const O3B_HOLDS: &str = "O3B_HOLDS";

const O3B_HOLDING: [SessionState; 4] = [
    SessionState::Planned,
    SessionState::Confirmed,
    SessionState::Open,
    SessionState::Closed,
];

/// The first phase's answer.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct O3Decision {
    /// O3-A or O3-B.
    pub sleeve: Sleeve,
    /// The trade date.
    pub trade_date: NaiveDate,
    /// When decided.
    pub now: DateTime<FixedOffset>,
    /// The state.
    pub state: PlanState,
    /// Every reason.
    pub reasons: Vec<String>,
    /// The setup's numbers.
    pub numbers: Numbers,
    /// The verdict.
    pub verdict: Option<Verdict>,
    /// Expiry.
    pub expiry: Option<NaiveDate>,
    /// Direction.
    pub direction: Option<Direction>,
    /// O3-A's range high.
    pub range_high: Option<Decimal>,
    /// O3-A's range low.
    pub range_low: Option<Decimal>,
    /// O3-B's half gap.
    pub half_gap: Option<Decimal>,
    /// The candidate.
    pub candidate: Option<Candidate>,
    /// Legs in send order.
    pub legs: Vec<PlanLeg>,
    /// The snapshot's minute.
    pub as_of_minute: Option<DateTime<FixedOffset>>,
    /// The itemised round trip.
    pub costs: Option<ChargeBreakdown>,
}

impl O3Decision {
    /// `PLANNED` or `SKIPPED` write a session.
    pub fn writes_session(&self) -> bool {
        matches!(self.state, PlanState::Planned | PlanState::Skipped)
    }

    /// The hedged basket (both legs) and no transient one.
    pub fn margin_baskets(&self) -> Baskets {
        if self.state != PlanState::Planned || self.legs.is_empty() {
            return (Vec::new(), Vec::new());
        }
        let all = self
            .legs
            .iter()
            .map(|lg| MarginLeg {
                tradingsymbol: lg.tradingsymbol.clone(),
                side: lg.side,
                quantity: lg.quantity,
            })
            .collect();
        (all, Vec::new())
    }
}

impl Decided for O3Decision {
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

/// `04` §5.3 written out at plan time, on `V = long.bid - short.ask`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct O3Exits {
    /// Target value.
    pub target_value: Decimal,
    /// Stop value.
    pub stop_value: Decimal,
    /// The invalidation rule, in words.
    pub invalidation: String,
    /// Its level.
    pub invalidation_level: Decimal,
    /// The hard exit.
    pub hard_exit_at: DateTime<FixedOffset>,
}

/// One `op_plan` row and its two legs.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct O3Plan {
    /// Deterministic id.
    pub plan_id: String,
    /// O3-A or O3-B.
    pub sleeve: Sleeve,
    /// The trade date.
    pub trade_date: NaiveDate,
    /// Expiry.
    pub expiry: NaiveDate,
    /// Paper or live.
    pub mode: Mode,
    /// The structure.
    pub structure: Structure,
    /// Direction.
    pub direction: Direction,
    /// When issued.
    pub issued_at: DateTime<FixedOffset>,
    /// The entry window's end.
    pub entry_window_end: DateTime<FixedOffset>,
    /// When it lapses.
    pub expires_at: DateTime<FixedOffset>,
    /// Debit per unit.
    pub debit_points: Decimal,
    /// Width.
    pub width_points: Decimal,
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
    /// Debit in rupees.
    pub debit_inr: Decimal,
    /// Max loss.
    pub max_loss_inr: Decimal,
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
    /// Hedged margin.
    pub margin_required_inr: Option<Decimal>,
    /// The margin check.
    pub margin: Option<MarginCheck>,
    /// The exits.
    pub exits: O3Exits,
    /// Warnings.
    pub warnings: Vec<String>,
    /// Legs.
    pub legs: Vec<PlanLeg>,
    /// The snapshot's minute.
    pub as_of_minute: Option<DateTime<FixedOffset>>,
    /// The numbers.
    pub numbers: Numbers,
}

impl O3Plan {
    /// Units per leg.
    pub fn quantity(&self) -> i64 {
        self.lots * self.lot_size
    }
}

/// The second phase's answer.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct O3Outcome {
    /// The decision.
    pub decision: O3Decision,
    /// The plan.
    pub plan: Option<O3Plan>,
    /// The state.
    pub state: PlanState,
    /// Every reason.
    pub reasons: Vec<String>,
}

/// O3-B: `o3b_window_end`; O3-A: `o3a_entry_window_end`.
pub fn entry_window_end(sleeve: Sleeve, config: &ExpirySetupsConfig) -> NaiveTime {
    if sleeve == Sleeve::O3b {
        config.o3b_window_end
    } else {
        config.o3a_entry_window_end
    }
}

/// O3-B's plan time, O3-A's first trigger bar.
pub fn first_decision_time(sleeve: Sleeve, config: &ExpirySetupsConfig) -> NaiveTime {
    if sleeve == Sleeve::O3b {
        config.o3b_plan_time
    } else {
        config.o3a_window_start
    }
}

/// `04` §5.5 on the user's rows: O3-B has a plan today that has not lapsed.
pub fn o3b_holds(context: &DayContext) -> bool {
    context
        .of(Sleeve::O3b)
        .session_state
        .is_some_and(|s| O3B_HOLDING.contains(&s))
}

#[derive(Debug, Clone, Default)]
struct Rest {
    numbers: Numbers,
    expiry: Option<NaiveDate>,
    verdict: Option<Verdict>,
    direction: Option<Direction>,
    range_high: Option<Decimal>,
    range_low: Option<Decimal>,
    half_gap: Option<Decimal>,
    candidate: Option<Candidate>,
    legs: Vec<PlanLeg>,
    as_of_minute: Option<DateTime<FixedOffset>>,
    costs: Option<ChargeBreakdown>,
}

struct Setup {
    state: Option<PlanState>,
    reasons: Vec<String>,
    fields: Rest,
    direction: Option<Direction>,
    decision_minute: Option<DateTime<FixedOffset>>,
}

fn clock_for(now: &DateTime<FixedOffset>, options: &OptionsConfig) -> SetupClock {
    SetupClock {
        now: *now,
        event_day: false,
        session_open: options.calendar.market_open,
        grace_seconds: options.chain.stale_scan_seconds,
    }
}

fn gap_hold(
    market: &MarketDay,
    prev_close: Decimal,
    now: &DateTime<FixedOffset>,
    options: &OptionsConfig,
) -> CoreResult<Setup> {
    let cfg = &options.expiry_setups;
    let gh = expiry_setups::gap_hold(&market.bars, prev_close, &clock_for(now, options), cfg)?;
    let numbers = Numbers::of(vec![
        ("setup", "GAP_HOLD".into()),
        ("prev_close", q_even(Some(prev_close), 2)?),
        ("open_0915", q_even(gh.open_0915, 2)?),
        ("gap_points", q_even(gh.gap_points, 2)?),
        ("gap_pct", q_even(gh.gap_pct, 4)?),
        ("gap_min_pct", py_str(cfg.o3b_gap_min_pct).into()),
        ("gap_max_pct", py_str(cfg.o3b_gap_max_pct).into()),
        ("half_gap", q_even(gh.half_gap, 2)?),
        (
            "direction",
            gh.direction.map(|d| d.value().to_owned()).into(),
        ),
        ("held", gh.held_so_far.into()),
        ("bars", gh.bars.into()),
        ("plan_time", t(Some(cfg.o3b_plan_time))),
        ("window_end", t(Some(cfg.o3b_window_end))),
    ]);
    let fields = Rest {
        numbers,
        direction: gh.direction,
        half_gap: gh.half_gap,
        ..Rest::default()
    };
    let terminal = |state, reasons: Vec<String>, fields| Setup {
        state: Some(state),
        reasons,
        fields,
        direction: None,
        decision_minute: None,
    };
    if !gh.final_ {
        return Ok(terminal(
            PlanState::NotReady,
            strs(&[WINDOW_NOT_SETTLED]),
            fields,
        ));
    }
    if !gh.reasons.is_empty() {
        let reasons = gh.reasons.iter().map(|r| r.value().to_owned()).collect();
        return Ok(terminal(
            PlanState::Skipped,
            reasons,
            Rest {
                verdict: Some(Verdict::Skip),
                ..fields
            },
        ));
    }
    let (true, Some(direction)) = (gh.triggered, gh.direction) else {
        return Ok(terminal(
            PlanState::NotReady,
            strs(&[BEFORE_PLAN_TIME]),
            fields,
        ));
    };
    Ok(Setup {
        state: None,
        reasons: Vec::new(),
        fields,
        direction: Some(direction),
        decision_minute: Some(at_ist(market.trade_date, cfg.o3b_plan_time)),
    })
}

fn range_break(
    market: &MarketDay,
    prev_close: Decimal,
    now: &DateTime<FixedOffset>,
    options: &OptionsConfig,
) -> CoreResult<Setup> {
    let cfg = &options.expiry_setups;
    let rb = expiry_setups::range_break(&market.bars, prev_close, &clock_for(now, options), cfg)?;
    let low_er = rb
        .low_er_breaks
        .iter()
        .map(|(time, close, er)| {
            Ok(Numbers::of(vec![
                ("close_time", t(Some(*time))),
                ("close", q_even(Some(*close), 2)?),
                ("er", q_even(Some(*er), 4)?),
            ]))
        })
        .collect::<CoreResult<Vec<_>>>()?;
    let mut numbers = Numbers::of(vec![
        ("setup", "RANGE_BREAK".into()),
        ("prev_close", q_even(Some(prev_close), 2)?),
        ("range_high", q_even(rb.range_high, 2)?),
        ("range_low", q_even(rb.range_low, 2)?),
        ("range_pct", q_even(rb.range_pct, 4)?),
        ("range_max_pct", py_str(cfg.o3a_range_max_pct).into()),
        ("bars", rb.bars.into()),
        ("level_up", q_even(rb.level_up, 2)?),
        ("level_down", q_even(rb.level_down, 2)?),
        ("er_min", py_str(cfg.o3a_er_min).into()),
        ("window_start", t(Some(cfg.o3a_window_start))),
        ("window_end", t(Some(cfg.o3a_window_end))),
        ("low_er_breaks", Value::List(low_er)),
    ]);
    let base = |numbers: Numbers| Rest {
        numbers,
        range_high: rb.range_high,
        range_low: rb.range_low,
        ..Rest::default()
    };
    let terminal = |state, reasons: Vec<String>, fields| Setup {
        state: Some(state),
        reasons,
        fields,
        direction: None,
        decision_minute: None,
    };
    if !rb.final_ {
        return Ok(terminal(
            PlanState::NotReady,
            strs(&[WINDOW_NOT_SETTLED]),
            base(numbers),
        ));
    }
    if !rb.reasons.is_empty() {
        let reasons = rb.reasons.iter().map(|r| r.value().to_owned()).collect();
        return Ok(terminal(
            PlanState::Skipped,
            reasons,
            Rest {
                verdict: Some(Verdict::Skip),
                ..base(numbers)
            },
        ));
    }
    let (Some(trigger_time), Some(direction)) = (rb.trigger_time, rb.direction) else {
        if rb.window_closed {
            return Ok(terminal(
                PlanState::Skipped,
                strs(&[NO_TRIGGER]),
                Rest {
                    verdict: Some(Verdict::Skip),
                    ..base(numbers)
                },
            ));
        }
        return Ok(terminal(
            PlanState::NotReady,
            strs(&[NO_TRIGGER_YET]),
            base(numbers),
        ));
    };
    numbers.extend(Numbers::of(vec![
        ("trigger_time", t(Some(trigger_time))),
        ("trigger_close", q_even(rb.trigger_close, 2)?),
        ("trigger_er", q_even(rb.trigger_er, 4)?),
        ("direction", direction.value().into()),
    ]));
    Ok(Setup {
        state: None,
        reasons: Vec::new(),
        fields: Rest {
            direction: Some(direction),
            ..base(numbers)
        },
        direction: Some(direction),
        decision_minute: Some(at_ist(market.trade_date, minute_after(trigger_time))),
    })
}

fn decision(
    sleeve: Sleeve,
    day: NaiveDate,
    now: &DateTime<FixedOffset>,
    state: PlanState,
    reasons: Vec<String>,
    rest: Rest,
) -> O3Decision {
    O3Decision {
        sleeve,
        trade_date: day,
        now: *now,
        state,
        reasons,
        numbers: rest.numbers,
        verdict: rest.verdict,
        expiry: rest.expiry,
        direction: rest.direction,
        range_high: rest.range_high,
        range_low: rest.range_low,
        half_gap: rest.half_gap,
        candidate: rest.candidate,
        legs: rest.legs,
        as_of_minute: rest.as_of_minute,
        costs: rest.costs,
    }
}

/// Role, setup, slot, chain, spread, sizing and the cost test for one O3 setup at `now`.
pub fn decide_o3(
    sleeve: Sleeve,
    market: &MarketDay,
    context: &DayContext,
    snapshots: &mut SnapshotBook,
    now: &DateTime<FixedOffset>,
    options: &OptionsConfig,
    ceilings: &OptionsCeilings,
) -> CoreResult<O3Decision> {
    if !O3_SLEEVES.contains(&sleeve) {
        return Err(CoreError::InvalidInput(format!(
            "{} is not an O3 setup",
            sleeve.value()
        )));
    }
    let cfg = &options.expiry_setups;
    let day = market.trade_date;
    let decided = |state, reasons, rest| Ok(decision(sleeve, day, now, state, reasons, rest));
    let day_role = role(
        day,
        sleeve,
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
    let enabled = if sleeve == Sleeve::O3a {
        cfg.o3a_enabled
    } else {
        cfg.o3b_enabled
    };
    if !enabled {
        return decided(
            PlanState::NoSession,
            strs(&[SETUP_DISABLED]),
            Rest::default(),
        );
    }
    let expiry = expiry_for_o1_o3(day);
    let with_expiry = || Rest {
        expiry: Some(expiry),
        ..Rest::default()
    };
    let clock = ist(now).time();
    if clock < first_decision_time(sleeve, cfg) {
        return decided(
            PlanState::NotReady,
            strs(&[BEFORE_PLAN_TIME]),
            with_expiry(),
        );
    }
    let skipped = |reason: &str| {
        Rest {
            verdict: Some(Verdict::Skip),
            ..with_expiry()
        }
        .with_reason(reason)
    };
    if !day_role.trades {
        let (reasons, rest) = skipped(RoleReason::EventDay.value());
        return decided(PlanState::Skipped, reasons, rest);
    }
    let Some(prev_close) = market.daily_closes.last().copied() else {
        let (reasons, rest) = skipped(NO_PREV_CLOSE);
        return decided(PlanState::Skipped, reasons, rest);
    };
    let setup = if sleeve == Sleeve::O3b {
        gap_hold(market, prev_close, now, options)?
    } else {
        range_break(market, prev_close, now, options)?
    };
    let common = Rest {
        expiry: Some(expiry),
        ..setup.fields
    };
    if let Some(state) = setup.state {
        return decided(state, setup.reasons, common);
    }
    if clock >= entry_window_end(sleeve, cfg) {
        return decided(
            PlanState::WindowClosed,
            strs(&[ENTRY_WINDOW_CLOSED]),
            common,
        );
    }
    let Some(snapshot) = snapshots.get(setup.decision_minute) else {
        return decided(PlanState::NotReady, strs(&[NO_DECISION_SNAPSHOT]), common);
    };
    let holds = sleeve == Sleeve::O3a && o3b_holds(context);
    let mine = context.of(sleeve);
    let view = priced_view(Some(&snapshot), Some(expiry), market, options)?;
    let Some(direction) = setup.direction else {
        return Err(CoreError::InvalidInput(
            "a triggered setup without a direction".to_owned(),
        ));
    };
    let candidate = expiry_setups::build(
        view.as_ref(),
        direction,
        &SpreadBuild {
            lot_size: lot_size_for(&market.contracts, expiry, &options.calendar.underlying),
            book: mine.book.clone(),
            expiry,
            slot_free: context.slot_holder.is_none_or(|h| h == sleeve) && !holds,
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
        let mut reasons = strs(&[code.value()]);
        if holds {
            reasons.push(O3B_HOLDS.to_owned());
        }
        return decided(PlanState::Skipped, reasons, common);
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

impl Rest {
    fn with_reason(self, reason: &str) -> (Vec<String>, Rest) {
        (strs(&[reason]), self)
    }
}

/// The snapshot minutes [`decide_o3`] will ask for at `now` (at most one).
pub fn wanted_minutes(
    sleeve: Sleeve,
    market: &MarketDay,
    context: &DayContext,
    now: &DateTime<FixedOffset>,
    options: &OptionsConfig,
    ceilings: &OptionsCeilings,
) -> CoreResult<BTreeSet<MinuteKey>> {
    let mut book = SnapshotBook::default();
    decide_o3(sleeve, market, context, &mut book, now, options, ceilings)?;
    Ok(book.asked)
}

/// `04` §5.3's levels for a spread bought at `debit` per unit.
pub fn exits_for(
    decision: &O3Decision,
    debit: Decimal,
    config: &ExpirySetupsConfig,
) -> CoreResult<O3Exits> {
    let Some(direction) = decision.direction else {
        return Err(CoreError::InvalidInput(
            "a planned spread has a direction".to_owned(),
        ));
    };
    let (rule, level) = if decision.sleeve == Sleeve::O3b {
        ("O3B: any 1-minute bar through half_gap", decision.half_gap)
    } else {
        (
            "O3A: a completed 5-minute close back inside the morning range",
            match direction {
                Direction::Up => decision.range_high,
                Direction::Down => decision.range_low,
            },
        )
    };
    let Some(level) = level else {
        return Err(CoreError::InvalidInput(
            "a planned spread knows its invalidation level".to_owned(),
        ));
    };
    Ok(O3Exits {
        target_value: num::paise(num::mul(config.target_frac_of_width, config.width_points)?)?,
        stop_value: num::paise(num::mul(config.stop_frac_of_debit, debit)?)?,
        invalidation: rule.to_owned(),
        invalidation_level: num::paise(level)?,
        hard_exit_at: at_ist(decision.trade_date, config.hard_exit_time),
    })
}

/// The margin ceiling, the `plan_id`, the 30-minute expiry and the exits, or `REJECTED_MARGIN`.
pub fn finalize_o3(
    decision: &O3Decision,
    margin: Option<&MarginQuote>,
    inputs: &Finalize,
    options: &OptionsConfig,
) -> CoreResult<O3Outcome> {
    if decision.state != PlanState::Planned {
        return Ok(O3Outcome {
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
    let mut warnings = candidate.warnings.clone();
    let check = match margin_gate(margin, inputs, &mut warnings)? {
        Ok(check) => check,
        Err(message) => return Ok(margin_rejected(decision, &message)),
    };
    let cfg = &options.expiry_setups;
    let issued = issued_ist(&decision.now);
    let window = entry_window_end(decision.sleeve, cfg);
    let expires = plan_expires_at(&issued, window, options.execution.plan_ttl_minutes);
    let lot_size = candidate.lot_size.unwrap_or(0);
    let quantity = candidate.lots * lot_size;
    let zero = Decimal::ZERO;
    let debit = candidate.points.unwrap_or(zero);
    let width = cfg.width_points;
    let profit = num::muli(
        num::sub(num::mul(cfg.target_frac_of_width, width)?, debit)?,
        quantity,
    )?;
    let stop = num::muli(
        num::mul(num::sub(Decimal::ONE, cfg.stop_frac_of_debit)?, debit)?,
        quantity,
    )?;
    let plan = O3Plan {
        plan_id: plan_id_for(inputs.user_id, decision),
        sleeve: decision.sleeve,
        trade_date: decision.trade_date,
        expiry,
        mode: inputs.mode,
        structure: candidate.structure,
        direction,
        issued_at: issued,
        entry_window_end: at_ist(decision.trade_date, window),
        expires_at: expires,
        debit_points: debit,
        width_points: width,
        lot_size,
        lots: candidate.lots,
        sizing_mode: candidate.sizing_mode.unwrap_or(SizingMode::PaperOneLot),
        half_size: candidate.half_size,
        risk_per_lot_inr: num::paise(candidate.risk_per_lot_inr.unwrap_or(zero))?,
        risk_budget_inr: num::paise(candidate.r_inr.unwrap_or(zero))?,
        debit_inr: num::paise(num::muli(debit, quantity)?)?,
        max_loss_inr: num::paise(candidate.max_loss_inr.unwrap_or(zero))?,
        profit_target_inr: num::paise(profit)?,
        stop_inr: num::paise(stop)?,
        expected_cost_inr: costs.total()?,
        cost_share: num::quantize(candidate.cost_share.unwrap_or(zero), 4, Rounding::HalfEven)?,
        costs: costs.clone(),
        margin_required_inr: margin.map(|m| num::paise(m.hedged_inr)).transpose()?,
        margin: check,
        exits: exits_for(decision, debit, cfg)?,
        warnings,
        legs: decision.legs.clone(),
        as_of_minute: decision.as_of_minute,
        numbers: decision.numbers.clone(),
    };
    Ok(O3Outcome {
        decision: decision.clone(),
        plan: Some(plan),
        state: PlanState::Planned,
        reasons: Vec::new(),
    })
}

fn margin_rejected(decision: &O3Decision, message: &str) -> O3Outcome {
    let code = "REJECTED_MARGIN".to_owned();
    let mut numbers = decision.numbers.clone();
    numbers.set("margin_message", message);
    O3Outcome {
        decision: O3Decision {
            state: PlanState::Skipped,
            reasons: vec![code.clone()],
            numbers,
            ..decision.clone()
        },
        plan: None,
        state: PlanState::Skipped,
        reasons: vec![code],
    }
}

/// `25000/25100 CE` — long strike, short strike and right.
pub fn contract_label(plan: &O3Plan) -> CoreResult<String> {
    let [long_leg, short_leg] = plan.legs.as_slice() else {
        return Err(CoreError::InvalidInput(
            "an O3 plan has exactly two legs".to_owned(),
        ));
    };
    Ok(format!(
        "{}/{} {}",
        py_str(num::quantize(long_leg.strike, 0, Rounding::HalfEven)?),
        py_str(num::quantize(short_leg.strike, 0, Rounding::HalfEven)?),
        long_leg.option_type.value()
    ))
}
