//! Port of `baskfy_core.options.plan`: the O1 plan builder, pure (`06` OP6).

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset, NaiveDate};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::decimal::Rounding;
use crate::error::{CoreError, CoreResult};
use crate::options::bars::ist;
use crate::options::calendar::{Contract, RoleReason, expiry_for_o1_o3, lot_size_for, role};
use crate::options::chain::{Level, as_f64};
use crate::options::condor::{self, CondorBuild, DayVerdict};
use crate::options::config::{
    CondorConfig, Mode, OptionType, OptionsCeilings, OptionsConfig, Side, SizingMode, Sleeve,
};
use crate::options::costs::{ChargeBreakdown, charges};
use crate::options::execution::{LegRole, entry_sequence, never_naked};
use crate::options::num::{self, at_ist, ist_offset, py_str};
use crate::options::scan::{DayContext, MarketDay, Numbers, Snapshot, Value, priced_view};
use crate::options::session::plan_expires_at;
use crate::options::sizing::{MarginCheck, MarginCode, margin_check};
use crate::options::structures::{Candidate, CandidateLeg, Structure, round_trip_fills};

/// The O1 sleeves.
pub const O1_SLEEVES: [Sleeve; 2] = [Sleeve::O1m, Sleeve::O1w];

/// What one build attempt concluded.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum PlanState {
    /// A plan, issued.
    Planned,
    /// A day with no plan.
    Skipped,
    /// Not this sleeve's day: nothing is written.
    NoSession,
    /// Too early to decide.
    NotReady,
    /// First asked after the entry window with nothing decided.
    WindowClosed,
}

impl PlanState {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            PlanState::Planned => "PLANNED",
            PlanState::Skipped => "SKIPPED",
            PlanState::NoSession => "NO_SESSION",
            PlanState::NotReady => "NOT_READY",
            PlanState::WindowClosed => "WINDOW_CLOSED",
        }
    }
}

/// `op_session.verdict`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum Verdict {
    /// The gate passed.
    Trade,
    /// The gate refused.
    Skip,
}

/// No plan from stale quotes.
pub const STALE_CHAIN: &str = "STALE_CHAIN";
/// The broker's calculator did not answer (a paper warning).
pub const MARGIN_UNKNOWN: &str = "MARGIN_UNKNOWN";

/// One `op_leg` row.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PlanLeg {
    /// 1-based send order.
    pub seq: i64,
    /// Role.
    pub role: LegRole,
    /// The master's symbol.
    pub tradingsymbol: String,
    /// The instrument token.
    pub instrument_token: i64,
    /// Strike.
    pub strike: Decimal,
    /// Call or put.
    pub option_type: OptionType,
    /// Side.
    pub side: Side,
    /// Units.
    pub quantity: i64,
    /// The attempt-1 limit.
    pub limit_price: Decimal,
    /// Bid.
    pub bid: Option<Decimal>,
    /// Ask.
    pub ask: Option<Decimal>,
    /// IV.
    pub iv: Option<f64>,
    /// Delta.
    pub delta: Option<f64>,
    /// Bid depth.
    #[serde(default)]
    pub bids: Vec<Level>,
    /// Ask depth.
    #[serde(default)]
    pub asks: Vec<Level>,
}

/// One leg of a margin-calculator basket — never an order.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MarginLeg {
    /// Symbol.
    pub tradingsymbol: String,
    /// Side.
    pub side: Side,
    /// Units.
    pub quantity: i64,
}

/// The calculator's answers.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MarginQuote {
    /// The whole basket.
    pub hedged_inr: Decimal,
    /// The worst entry prefix.
    pub transient_inr: Option<Decimal>,
}

/// The first phase's answer.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct O1Decision {
    /// The sleeve.
    pub sleeve: Sleeve,
    /// The trade date.
    pub trade_date: NaiveDate,
    /// When decided.
    pub now: DateTime<FixedOffset>,
    /// The state.
    pub state: PlanState,
    /// Every reason.
    pub reasons: Vec<String>,
    /// The gate's numbers.
    pub numbers: Numbers,
    /// The gate's verdict.
    pub verdict: Option<Verdict>,
    /// Expiry.
    pub expiry: Option<NaiveDate>,
    /// The candidate.
    pub candidate: Option<Candidate>,
    /// The legs in send order.
    pub legs: Vec<PlanLeg>,
    /// The snapshot's minute.
    pub as_of_minute: Option<DateTime<FixedOffset>>,
    /// The itemised round trip.
    pub costs: Option<ChargeBreakdown>,
}

/// A basket pair: hedged and transient.
pub type Baskets = (Vec<MarginLeg>, Vec<MarginLeg>);

fn basket(legs: &[PlanLeg]) -> Vec<MarginLeg> {
    legs.iter()
        .map(|lg| MarginLeg {
            tradingsymbol: lg.tradingsymbol.clone(),
            side: lg.side,
            quantity: lg.quantity,
        })
        .collect()
}

impl O1Decision {
    /// `PLANNED` or `SKIPPED` write a session.
    pub fn writes_session(&self) -> bool {
        matches!(self.state, PlanState::Planned | PlanState::Skipped)
    }

    /// The hedged basket and the transient one (every leg but the last).
    pub fn margin_baskets(&self) -> Baskets {
        if self.state != PlanState::Planned || self.legs.is_empty() {
            return (Vec::new(), Vec::new());
        }
        let all = basket(&self.legs);
        let transient = all[..all.len() - 1].to_vec();
        (all, transient)
    }
}

/// One `op_plan` row and its legs.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct O1Plan {
    /// Deterministic id.
    pub plan_id: String,
    /// The sleeve.
    pub sleeve: Sleeve,
    /// The trade date.
    pub trade_date: NaiveDate,
    /// Expiry.
    pub expiry: NaiveDate,
    /// Paper or live.
    pub mode: Mode,
    /// The structure.
    pub structure: Structure,
    /// When issued.
    pub issued_at: DateTime<FixedOffset>,
    /// The entry window's end.
    pub entry_window_end: DateTime<FixedOffset>,
    /// When it lapses.
    pub expires_at: DateTime<FixedOffset>,
    /// Credit per unit.
    pub credit_points: Decimal,
    /// Wing width.
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
    /// Credit in rupees.
    pub credit_inr: Decimal,
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
    /// Transient margin.
    pub margin_transient_inr: Option<Decimal>,
    /// The margin check.
    pub margin: Option<MarginCheck>,
    /// Warnings.
    pub warnings: Vec<String>,
    /// Legs.
    pub legs: Vec<PlanLeg>,
    /// The snapshot's minute.
    pub as_of_minute: Option<DateTime<FixedOffset>>,
    /// The gate's numbers.
    pub numbers: Numbers,
}

impl O1Plan {
    /// Units per leg.
    pub fn quantity(&self) -> i64 {
        self.lots * self.lot_size
    }
}

/// The second phase's answer.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct O1Outcome {
    /// The decision.
    pub decision: O1Decision,
    /// The plan.
    pub plan: Option<O1Plan>,
    /// The state.
    pub state: PlanState,
    /// Every reason.
    pub reasons: Vec<String>,
}

/// `condor_monthly` for O1-M, `condor_weekly` for O1-W.
pub fn condor_config_for(sleeve: Sleeve, options: &OptionsConfig) -> CoreResult<&CondorConfig> {
    match sleeve {
        Sleeve::O1m => Ok(&options.condor_monthly),
        Sleeve::O1w => Ok(&options.condor_weekly),
        _ => Err(CoreError::InvalidInput(format!(
            "{} is not an O1 sleeve",
            sleeve.value()
        ))),
    }
}

/// `str(value.quantize(places))` (half even), `None` passing through.
pub(crate) fn q_even(value: Option<Decimal>, places: i64) -> CoreResult<Value> {
    num::qstr(value, places, Rounding::HalfEven).map(Value::from)
}

fn gate_numbers(verdict: &DayVerdict, config: &CondorConfig) -> CoreResult<Numbers> {
    let n = &verdict.numbers;
    Ok(Numbers::of(vec![
        ("variant", config.variant.value().into()),
        ("prev_close", q_even(Some(n.prev_close), 2)?),
        ("bars", n.bars.into()),
        ("open_0915", q_even(n.open_0915, 2)?),
        ("gap_pct", q_even(n.gap_pct, 4)?),
        ("range_pct", q_even(n.range_pct, 4)?),
        ("or_high", q_even(n.or_high, 2)?),
        ("or_low", q_even(n.or_low, 2)?),
        ("last_close", q_even(n.last_close, 2)?),
        ("contained", n.contained.into()),
        ("er", q_even(n.er, 4)?),
    ]))
}

/// The candidate's legs as `op_leg` rows, in entry order, with symbols and depth.
pub fn plan_legs(
    candidate: &Candidate,
    contracts: &[Contract],
    snapshot: Option<&Snapshot>,
) -> CoreResult<Vec<PlanLeg>> {
    let by_role: BTreeMap<LegRole, &CandidateLeg> =
        candidate.legs.iter().map(|lg| (lg.role, lg)).collect();
    let roles: Vec<LegRole> = candidate.legs.iter().map(|lg| lg.role).collect();
    let mut unique: Vec<LegRole> = Vec::new();
    for r in roles {
        if !unique.contains(&r) {
            unique.push(r);
        }
    }
    let order = entry_sequence(&unique)?;
    let quantity = candidate.lots * candidate.lot_size.unwrap_or(0);
    let mut out = Vec::with_capacity(order.len());
    for (i, leg_role) in order.into_iter().enumerate() {
        let Some(lg) = by_role.get(&leg_role) else {
            continue;
        };
        let symbol = contracts
            .iter()
            .rev()
            .find(|c| c.instrument_token == lg.instrument_token)
            .map(|c| c.tradingsymbol.clone())
            .ok_or_else(|| {
                CoreError::InvalidInput(format!(
                    "token {} is not in the master",
                    lg.instrument_token
                ))
            })?;
        let quote = snapshot.and_then(|s| {
            s.quotes
                .iter()
                .rev()
                .find(|q| q.instrument_token == lg.instrument_token)
        });
        out.push(PlanLeg {
            seq: i64::try_from(i + 1).unwrap_or(i64::MAX),
            role: lg.role,
            tradingsymbol: symbol,
            instrument_token: lg.instrument_token,
            strike: lg.strike,
            option_type: lg.option_type,
            side: lg.entry_side(),
            quantity,
            limit_price: lg.limit_price,
            bid: lg.bid,
            ask: lg.ask,
            iv: lg.iv,
            delta: lg.delta,
            bids: quote.map(|q| q.bids.clone()).unwrap_or_default(),
            asks: quote.map(|q| q.asks.clone()).unwrap_or_default(),
        });
    }
    Ok(out)
}

/// `04` §6.1 itemised over the plan's orders; its total is the candidate's round trip.
pub fn plan_costs(
    candidate: &Candidate,
    tick: Decimal,
    options: &OptionsConfig,
) -> CoreResult<ChargeBreakdown> {
    let quantity = candidate.lots * candidate.lot_size.unwrap_or(0);
    let fills = round_trip_fills(&candidate.legs, quantity, tick, &options.execution)?;
    charges(&fills, &options.costs)
}

/// The optional fields a decision may carry.
#[derive(Debug, Clone, Default, PartialEq)]
pub(crate) struct Rest {
    pub(crate) expiry: Option<NaiveDate>,
    pub(crate) numbers: Numbers,
    pub(crate) verdict: Option<Verdict>,
    pub(crate) candidate: Option<Candidate>,
    pub(crate) legs: Vec<PlanLeg>,
    pub(crate) as_of_minute: Option<DateTime<FixedOffset>>,
    pub(crate) costs: Option<ChargeBreakdown>,
}

fn decision(
    sleeve: Sleeve,
    day: NaiveDate,
    now: &DateTime<FixedOffset>,
    state: PlanState,
    reasons: Vec<String>,
    rest: Rest,
) -> O1Decision {
    O1Decision {
        sleeve,
        trade_date: day,
        now: *now,
        state,
        reasons,
        numbers: rest.numbers,
        verdict: rest.verdict,
        expiry: rest.expiry,
        candidate: rest.candidate,
        legs: rest.legs,
        as_of_minute: rest.as_of_minute,
        costs: rest.costs,
    }
}

pub(crate) fn strs(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| (*s).to_owned()).collect()
}

/// Seconds from `then` to `now`, IST wall clock.
pub(crate) fn age_seconds(now: &DateTime<FixedOffset>, then: &DateTime<FixedOffset>) -> f64 {
    as_f64(
        (ist(now) - ist(then))
            .num_microseconds()
            .unwrap_or(i64::MAX),
    ) / 1_000_000.0
}

/// Role, gate, chain, structure, sizing and the cost test for one O1 sleeve at `now`.
pub fn decide_o1(
    sleeve: Sleeve,
    market: &MarketDay,
    context: &DayContext,
    snapshot: Option<&Snapshot>,
    now: &DateTime<FixedOffset>,
    options: &OptionsConfig,
    ceilings: &OptionsCeilings,
) -> CoreResult<O1Decision> {
    let cfg = condor_config_for(sleeve, options)?;
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
    let expiry = expiry_for_o1_o3(day);
    let with_expiry = || Rest {
        expiry: Some(expiry),
        ..Rest::default()
    };
    let clock = ist(now).time();
    if clock < cfg.plan_time {
        return decided(
            PlanState::NotReady,
            strs(&["BEFORE_PLAN_TIME"]),
            with_expiry(),
        );
    }
    if clock >= cfg.entry_window_end {
        return decided(
            PlanState::WindowClosed,
            strs(&["ENTRY_WINDOW_CLOSED"]),
            with_expiry(),
        );
    }
    let skip = |reasons: Vec<String>, numbers: Numbers| {
        decided(
            PlanState::Skipped,
            reasons,
            Rest {
                expiry: Some(expiry),
                numbers,
                verdict: Some(Verdict::Skip),
                ..Rest::default()
            },
        )
    };
    if !day_role.trades {
        return skip(strs(&[RoleReason::EventDay.value()]), Numbers::default());
    }
    let Some(prev_close) = market.daily_closes.last().copied() else {
        return skip(strs(&["NO_PREV_CLOSE"]), Numbers::default());
    };
    let verdict = condor::observe(
        &market.bars,
        prev_close,
        now,
        false,
        cfg,
        options.chain.stale_scan_seconds,
    )?;
    let numbers = gate_numbers(&verdict, cfg)?;
    let not_ready = |reason: &str, numbers: Numbers| {
        decided(
            PlanState::NotReady,
            strs(&[reason]),
            Rest {
                expiry: Some(expiry),
                numbers,
                ..Rest::default()
            },
        )
    };
    if !verdict.final_ {
        return not_ready("WINDOW_NOT_SETTLED", numbers);
    }
    if !verdict.reasons.is_empty() {
        return skip(
            verdict
                .reasons
                .iter()
                .map(|r| r.value().to_owned())
                .collect(),
            numbers,
        );
    }
    let Some(snapshot) = snapshot else {
        return not_ready("NO_DECISION_SNAPSHOT", numbers);
    };
    let mine = context.of(sleeve);
    let view = priced_view(Some(snapshot), Some(expiry), market, options)?;
    let candidate = condor::build(
        view.as_ref(),
        &CondorBuild {
            or_high: verdict.numbers.or_high.unwrap_or(Decimal::ZERO),
            or_low: verdict.numbers.or_low.unwrap_or(Decimal::ZERO),
            lot_size: lot_size_for(&market.contracts, expiry, &options.calendar.underlying),
            book: mine.book.clone(),
            slot_free: context.slot_holder.is_none_or(|h| h == sleeve),
            paused: mine.paused,
            expiry,
        },
        cfg,
        options,
        ceilings,
    )?;
    let common = Rest {
        expiry: Some(expiry),
        numbers,
        verdict: Some(Verdict::Trade),
        candidate: Some(candidate.clone()),
        as_of_minute: Some(snapshot.ts),
        ..Rest::default()
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
    let legs = plan_legs(&candidate, &market.contracts, Some(snapshot))?;
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

/// What `plan_id_for` and `assert_never_naked` need of a decision.
pub trait Decided {
    /// The sleeve.
    fn sleeve(&self) -> Sleeve;
    /// The trade date.
    fn trade_date(&self) -> NaiveDate;
    /// The legs.
    fn legs(&self) -> &[PlanLeg];
}

impl Decided for O1Decision {
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

/// Refuse a leg order that would leave the book short more than long at any prefix.
pub fn assert_never_naked(legs: &[PlanLeg]) -> CoreResult<()> {
    for prefix in 1..=legs.len() {
        let mut held: BTreeMap<LegRole, i64> = BTreeMap::new();
        for lg in &legs[..prefix] {
            *held.entry(lg.role).or_insert(0) += lg.quantity;
        }
        if !never_naked(&held) {
            return Err(CoreError::InvalidInput(
                "the legs would leave the book short more than long".to_owned(),
            ));
        }
    }
    Ok(())
}

/// Deterministic: the same user, sleeve, date and legs mint the same id.
pub fn plan_id_for(user_id: i64, decision: &impl Decided) -> String {
    let mut parts = vec![
        user_id.to_string(),
        decision.sleeve().value().to_owned(),
        decision.trade_date().to_string(),
    ];
    for lg in decision.legs() {
        parts.push(format!(
            "{},{},{},{},{}",
            lg.seq,
            lg.instrument_token,
            lg.side.value(),
            lg.quantity,
            py_str(lg.limit_price)
        ));
    }
    let digest = sha256_hex(parts.join("|").as_bytes());
    format!(
        "{}-{}-{}",
        decision.sleeve().value(),
        decision.trade_date().format("%Y%m%d"),
        &digest[..12]
    )
}

/// The broker-side inputs of a finalize.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Finalize {
    /// The user.
    pub user_id: i64,
    /// Paper or live.
    pub mode: Mode,
    /// The margin pool.
    pub margin_pool_inr: Decimal,
    /// Margin in use.
    pub margin_in_use_inr: Decimal,
}

/// The margin outcome shared by O1 and O3: warnings so far, the check, or a rejection message.
pub(crate) fn margin_gate(
    margin: Option<&MarginQuote>,
    inputs: &Finalize,
    warnings: &mut Vec<String>,
) -> CoreResult<Result<Option<MarginCheck>, String>> {
    let Some(margin) = margin else {
        if inputs.mode == Mode::Live {
            return Ok(Err(
                "the broker's margin calculator did not answer".to_owned()
            ));
        }
        warnings.push(MARGIN_UNKNOWN.to_owned());
        return Ok(Ok(None));
    };
    let check = margin_check(
        inputs.mode,
        margin.hedged_inr,
        margin.transient_inr,
        inputs.margin_pool_inr,
        inputs.margin_in_use_inr,
    )?;
    if check.rejected() {
        return Ok(Err(check.message));
    }
    if check.code == MarginCode::MarginPoolUnset {
        warnings.push(MarginCode::MarginPoolUnset.value().to_owned());
    }
    Ok(Ok(Some(check)))
}

/// `now` as aware IST.
pub(crate) fn issued_ist(now: &DateTime<FixedOffset>) -> DateTime<FixedOffset> {
    now.with_timezone(&ist_offset())
}

/// The margin ceiling, the `plan_id` and the 30-minute expiry, or `REJECTED_MARGIN`.
pub fn finalize_o1(
    decision: &O1Decision,
    margin: Option<&MarginQuote>,
    inputs: &Finalize,
    options: &OptionsConfig,
) -> CoreResult<O1Outcome> {
    if decision.state != PlanState::Planned {
        return Ok(O1Outcome {
            decision: decision.clone(),
            plan: None,
            state: decision.state,
            reasons: decision.reasons.clone(),
        });
    }
    let (Some(candidate), Some(expiry), Some(costs)) =
        (&decision.candidate, decision.expiry, &decision.costs)
    else {
        return Err(CoreError::InvalidInput(
            "a PLANNED decision carries its candidate, expiry and costs".to_owned(),
        ));
    };
    assert_never_naked(&decision.legs)?;
    let mut warnings = candidate.warnings.clone();
    let check = match margin_gate(margin, inputs, &mut warnings)? {
        Ok(check) => check,
        Err(message) => return Ok(margin_rejected(decision, &message)),
    };
    let cfg = condor_config_for(decision.sleeve, options)?;
    let issued = issued_ist(&decision.now);
    let window_end = at_ist(decision.trade_date, cfg.entry_window_end);
    let expires = plan_expires_at(
        &issued,
        cfg.entry_window_end,
        options.execution.plan_ttl_minutes,
    );
    let lot_size = candidate.lot_size.unwrap_or(0);
    let quantity = candidate.lots * lot_size;
    let credit = candidate.points.unwrap_or(Decimal::ZERO);
    let credit_inr = num::muli(credit, quantity)?;
    let width = cfg.wing_width_points;
    let zero = Decimal::ZERO;
    let plan = O1Plan {
        plan_id: plan_id_for(inputs.user_id, decision),
        sleeve: decision.sleeve,
        trade_date: decision.trade_date,
        expiry,
        mode: inputs.mode,
        structure: candidate.structure,
        issued_at: issued,
        entry_window_end: window_end,
        expires_at: expires,
        credit_points: credit,
        width_points: width,
        lot_size,
        lots: candidate.lots,
        sizing_mode: candidate.sizing_mode.unwrap_or(SizingMode::PaperOneLot),
        half_size: candidate.half_size,
        risk_per_lot_inr: num::paise(candidate.risk_per_lot_inr.unwrap_or(zero))?,
        risk_budget_inr: num::paise(candidate.r_inr.unwrap_or(zero))?,
        credit_inr: num::paise(credit_inr)?,
        max_loss_inr: num::paise(candidate.max_loss_inr.unwrap_or(zero))?,
        profit_target_inr: num::paise(num::mul(
            num::sub(Decimal::ONE, cfg.profit_take_frac)?,
            credit_inr,
        )?)?,
        stop_inr: num::paise(num::mul(
            num::sub(cfg.stop_frac, Decimal::ONE)?,
            credit_inr,
        )?)?,
        expected_cost_inr: costs.total()?,
        cost_share: num::quantize(candidate.cost_share.unwrap_or(zero), 4, Rounding::HalfEven)?,
        costs: costs.clone(),
        margin_required_inr: margin.map(|m| num::paise(m.hedged_inr)).transpose()?,
        margin_transient_inr: margin
            .and_then(|m| m.transient_inr)
            .map(num::paise)
            .transpose()?,
        margin: check,
        warnings,
        legs: decision.legs.clone(),
        as_of_minute: decision.as_of_minute,
        numbers: decision.numbers.clone(),
    };
    Ok(O1Outcome {
        decision: decision.clone(),
        plan: Some(plan),
        state: PlanState::Planned,
        reasons: Vec::new(),
    })
}

fn margin_rejected(decision: &O1Decision, message: &str) -> O1Outcome {
    let code = "REJECTED_MARGIN".to_owned();
    let mut numbers = decision.numbers.clone();
    numbers.set("margin_message", message);
    let skipped = O1Decision {
        state: PlanState::Skipped,
        reasons: vec![code.clone()],
        numbers,
        ..decision.clone()
    };
    O1Outcome {
        decision: skipped,
        plan: None,
        state: PlanState::Skipped,
        reasons: vec![code],
    }
}

/// SHA-256 of `data`, lowercase hex (Python's `hashlib.sha256(data).hexdigest()`).
pub fn sha256_hex(data: &[u8]) -> String {
    const K: [u32; 64] = [
        0x428a_2f98,
        0x7137_4491,
        0xb5c0_fbcf,
        0xe9b5_dba5,
        0x3956_c25b,
        0x59f1_11f1,
        0x923f_82a4,
        0xab1c_5ed5,
        0xd807_aa98,
        0x1283_5b01,
        0x2431_85be,
        0x550c_7dc3,
        0x72be_5d74,
        0x80de_b1fe,
        0x9bdc_06a7,
        0xc19b_f174,
        0xe49b_69c1,
        0xefbe_4786,
        0x0fc1_9dc6,
        0x240c_a1cc,
        0x2de9_2c6f,
        0x4a74_84aa,
        0x5cb0_a9dc,
        0x76f9_88da,
        0x983e_5152,
        0xa831_c66d,
        0xb003_27c8,
        0xbf59_7fc7,
        0xc6e0_0bf3,
        0xd5a7_9147,
        0x06ca_6351,
        0x1429_2967,
        0x27b7_0a85,
        0x2e1b_2138,
        0x4d2c_6dfc,
        0x5338_0d13,
        0x650a_7354,
        0x766a_0abb,
        0x81c2_c92e,
        0x9272_2c85,
        0xa2bf_e8a1,
        0xa81a_664b,
        0xc24b_8b70,
        0xc76c_51a3,
        0xd192_e819,
        0xd699_0624,
        0xf40e_3585,
        0x106a_a070,
        0x19a4_c116,
        0x1e37_6c08,
        0x2748_774c,
        0x34b0_bcb5,
        0x391c_0cb3,
        0x4ed8_aa4a,
        0x5b9c_ca4f,
        0x682e_6ff3,
        0x748f_82ee,
        0x78a5_636f,
        0x84c8_7814,
        0x8cc7_0208,
        0x90be_fffa,
        0xa450_6ceb,
        0xbef9_a3f7,
        0xc671_78f2,
    ];
    let mut h: [u32; 8] = [
        0x6a09_e667,
        0xbb67_ae85,
        0x3c6e_f372,
        0xa54f_f53a,
        0x510e_527f,
        0x9b05_688c,
        0x1f83_d9ab,
        0x5be0_cd19,
    ];
    let bit_len = (data.len() as u64).wrapping_mul(8);
    let mut msg = data.to_vec();
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());
    for chunk in msg.chunks(64) {
        let mut w = [0_u32; 64];
        for (i, word) in chunk.chunks(4).enumerate().take(16) {
            w[i] = u32::from_be_bytes([word[0], word[1], word[2], word[3]]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh] = h;
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ (!e & g);
            let t1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = s0.wrapping_add(maj);
            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(t1);
            d = c;
            c = b;
            b = a;
            a = t1.wrapping_add(t2);
        }
        for (slot, v) in h.iter_mut().zip([a, b, c, d, e, f, g, hh]) {
            *slot = slot.wrapping_add(v);
        }
    }
    h.iter().map(|v| format!("{v:08x}")).collect()
}
