//! Port of `baskfy_core.options.structures`: the priced chain, candidate legs and the shared candidate.

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::decimal::Rounding;
use crate::error::{CoreError, CoreResult};
use crate::options::chain::{OptionQuote, atm, parity_forward, quote_greeks, strike_step};
use crate::options::config::{
    ChainConfig, CostRates, ExecutionConfig, Mode, OptionType, OptionsCeilings, Side, SizingConfig,
    SizingMode,
};
use crate::options::costs::{CostFill, CostVerdict, charges, cost_test};
use crate::options::execution::{LegRole, next_attempt};
use crate::options::greeks::{Greeks, year_fraction};
use crate::options::num::{self, from_float, py_str};
use crate::options::sizing::{SizeInputs, Sizing, SizingReject, size};

/// `op_plan.structure`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Structure {
    /// O1.
    IronCondor,
    /// O2.
    LongOption,
    /// O3.
    DebitSpread,
}

impl Structure {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            Structure::IronCondor => "IRON_CONDOR",
            Structure::LongOption => "LONG_OPTION",
            Structure::DebitSpread => "DEBIT_SPREAD",
        }
    }
}

/// A move's direction.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum Direction {
    /// Up.
    Up,
    /// Down.
    Down,
}

impl Direction {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            Direction::Up => "UP",
            Direction::Down => "DOWN",
        }
    }

    /// The other way.
    pub fn opposite(self) -> Self {
        match self {
            Direction::Up => Direction::Down,
            Direction::Down => Direction::Up,
        }
    }
}

/// Why a candidate is not a plan.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Rejection {
    /// No short call.
    RejectedNoShortCall,
    /// No short put.
    RejectedNoShortPut,
    /// No wing.
    RejectedNoWing,
    /// Credit below the floor.
    RejectedCredit,
    /// Delta out of band.
    RejectedDelta,
    /// Not liquid at the quantity.
    RejectedIlliquid,
    /// Debit out of range.
    RejectedDebit,
    /// Costs too much.
    RejectedCost,
    /// O2's premium cap.
    RejectedPremiumCap,
    /// No lot size.
    RejectedNoLotSize,
    /// No capital in a live plan.
    RejectedNoSleeveCapital,
    /// The budget buys no lot.
    RejectedBudget,
    /// Another sleeve holds the slot.
    RejectedSlotTaken,
    /// Paused.
    RejectedPaused,
    /// The contract is not quoted.
    RejectedNoContract,
    /// No chain for the expiry.
    RejectedNoChain,
}

impl Rejection {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            Rejection::RejectedNoShortCall => "REJECTED_NO_SHORT_CALL",
            Rejection::RejectedNoShortPut => "REJECTED_NO_SHORT_PUT",
            Rejection::RejectedNoWing => "REJECTED_NO_WING",
            Rejection::RejectedCredit => "REJECTED_CREDIT",
            Rejection::RejectedDelta => "REJECTED_DELTA",
            Rejection::RejectedIlliquid => "REJECTED_ILLIQUID",
            Rejection::RejectedDebit => "REJECTED_DEBIT",
            Rejection::RejectedCost => "REJECTED_COST",
            Rejection::RejectedPremiumCap => "REJECTED_PREMIUM_CAP",
            Rejection::RejectedNoLotSize => "REJECTED_NO_LOT_SIZE",
            Rejection::RejectedNoSleeveCapital => "REJECTED_NO_SLEEVE_CAPITAL",
            Rejection::RejectedBudget => "REJECTED_BUDGET",
            Rejection::RejectedSlotTaken => "REJECTED_SLOT_TAKEN",
            Rejection::RejectedPaused => "REJECTED_PAUSED",
            Rejection::RejectedNoContract => "REJECTED_NO_CONTRACT",
            Rejection::RejectedNoChain => "REJECTED_NO_CHAIN",
        }
    }
}

impl From<SizingReject> for Rejection {
    fn from(code: SizingReject) -> Self {
        match code {
            SizingReject::RejectedNoLotSize => Rejection::RejectedNoLotSize,
            SizingReject::RejectedNoSleeveCapital => Rejection::RejectedNoSleeveCapital,
            SizingReject::RejectedBudget => Rejection::RejectedBudget,
            SizingReject::RejectedPremiumCap => Rejection::RejectedPremiumCap,
        }
    }
}

/// A quote and its greeks on the expiry's forward.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PricedQuote {
    /// The quote.
    pub quote: OptionQuote,
    /// Greeks; `None` where `04` §2.3 refuses.
    pub greeks: Option<Greeks>,
}

impl PricedQuote {
    /// The strike.
    pub fn strike(&self) -> Decimal {
        self.quote.strike
    }

    /// Call or put.
    pub fn option_type(&self) -> OptionType {
        self.quote.option_type
    }

    /// Delta, when solved.
    pub fn delta(&self) -> Option<f64> {
        self.greeks.map(|g| g.delta)
    }
}

/// One expiry's chain at one instant, priced.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ChainView {
    /// Expiry.
    pub expiry: NaiveDate,
    /// The pricing instant.
    pub now: DateTime<FixedOffset>,
    /// Spot.
    pub spot: Decimal,
    /// Strike step.
    pub step: Decimal,
    /// ATM strike.
    pub atm: Decimal,
    /// The parity forward.
    pub forward: Option<Decimal>,
    /// Tick size.
    pub tick: Decimal,
    /// Every quote, by strike then type.
    pub quotes: Vec<PricedQuote>,
}

impl ChainView {
    /// The quote at `strike` and `option_type`.
    pub fn get(&self, strike: Decimal, option_type: OptionType) -> Option<&PricedQuote> {
        self.quotes
            .iter()
            .find(|q| q.strike() == strike && q.option_type() == option_type)
    }

    /// Every quote of one type.
    pub fn of_type(&self, option_type: OptionType) -> Vec<&PricedQuote> {
        self.quotes
            .iter()
            .filter(|q| q.option_type() == option_type)
            .collect()
    }
}

/// What `chain_view` reads besides the quotes.
#[derive(Debug, Clone, PartialEq)]
pub struct ViewInputs<'a> {
    /// The expiry.
    pub expiry: NaiveDate,
    /// Spot.
    pub spot: Decimal,
    /// The pricing instant.
    pub now: DateTime<FixedOffset>,
    /// Tick size.
    pub tick: Decimal,
    /// The chain config.
    pub config: &'a ChainConfig,
    /// The settlement time.
    pub settle: NaiveTime,
    /// The master's listed strikes, when given.
    pub listed_strikes: Option<&'a [Decimal]>,
}

/// Price `expiry`'s quotes at `now`; `None` without a quote or a strike step.
pub fn chain_view(
    quotes: &[OptionQuote],
    inputs: &ViewInputs<'_>,
) -> CoreResult<Option<ChainView>> {
    let mut same: Vec<&OptionQuote> = quotes
        .iter()
        .filter(|q| q.expiry == inputs.expiry)
        .collect();
    if same.is_empty() {
        return Ok(None);
    }
    let strikes: Vec<Decimal> = match inputs.listed_strikes {
        Some(listed) => listed.to_vec(),
        None => same.iter().map(|q| q.strike).collect(),
    };
    let Some(step) = strike_step(&strikes, inputs.spot, inputs.config.snapshot_strikes)? else {
        return Ok(None);
    };
    let years = year_fraction(&inputs.now, inputs.expiry, inputs.settle);
    let owned: Vec<OptionQuote> = same.iter().map(|q| (*q).clone()).collect();
    let forward = if years > 0.0 {
        parity_forward(&owned, inputs.spot, step, years, inputs.config.rate)?
    } else {
        None
    };
    same.sort_by(|a, b| {
        a.strike
            .cmp(&b.strike)
            .then_with(|| a.option_type.value().cmp(b.option_type.value()))
    });
    let mut priced = Vec::with_capacity(same.len());
    for q in same {
        let greeks = match forward {
            Some(f) => quote_greeks(q, f, &inputs.now, inputs.tick, inputs.config, inputs.settle)?,
            None => None,
        };
        priced.push(PricedQuote {
            quote: q.clone(),
            greeks,
        });
    }
    Ok(Some(ChainView {
        expiry: inputs.expiry,
        now: inputs.now,
        spot: inputs.spot,
        step,
        atm: atm(inputs.spot, step)?,
        forward,
        tick: inputs.tick,
        quotes: priced,
    }))
}

/// One leg as a plan would send it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CandidateLeg {
    /// Its role.
    pub role: LegRole,
    /// Strike.
    pub strike: Decimal,
    /// Call or put.
    pub option_type: OptionType,
    /// Expiry.
    pub expiry: NaiveDate,
    /// The instrument token.
    pub instrument_token: i64,
    /// Bid.
    pub bid: Option<Decimal>,
    /// Ask.
    pub ask: Option<Decimal>,
    /// IV.
    pub iv: Option<f64>,
    /// Delta.
    pub delta: Option<f64>,
    /// The attempt-1 limit.
    pub limit_price: Decimal,
}

impl CandidateLeg {
    /// The side that opens it.
    pub fn entry_side(&self) -> Side {
        self.role.entry_side()
    }
}

fn two_sided(bid: Option<Decimal>, ask: Option<Decimal>) -> CoreResult<(Decimal, Decimal)> {
    match (bid, ask) {
        (Some(b), Some(a)) => Ok((b, a)),
        _ => Err(CoreError::InvalidInput(
            "a leg needs a two-sided quote".to_owned(),
        )),
    }
}

fn first_attempt(
    side: Side,
    bid: Decimal,
    ask: Decimal,
    tick: Decimal,
    config: &ExecutionConfig,
) -> CoreResult<Decimal> {
    next_attempt(1, side, bid, ask, tick, false, config)?
        .map(|a| a.price)
        .ok_or_else(|| CoreError::InvalidInput("attempt 1 always exists".to_owned()))
}

/// A leg at `04` §8.2's attempt-1 limit.
pub fn leg(
    role: LegRole,
    priced: &PricedQuote,
    tick: Decimal,
    config: &ExecutionConfig,
) -> CoreResult<CandidateLeg> {
    let q = &priced.quote;
    let (bid, ask) = two_sided(q.bid, q.ask)?;
    Ok(CandidateLeg {
        role,
        strike: q.strike,
        option_type: q.option_type,
        expiry: q.expiry,
        instrument_token: q.instrument_token,
        bid: q.bid,
        ask: q.ask,
        iv: priced.greeks.map(|g| g.iv),
        delta: priced.delta(),
        limit_price: first_attempt(role.entry_side(), bid, ask, tick, config)?,
    })
}

/// The same quotes' attempt-1 close.
pub fn exit_price(
    candidate_leg: &CandidateLeg,
    tick: Decimal,
    config: &ExecutionConfig,
) -> CoreResult<Decimal> {
    let (bid, ask) = two_sided(candidate_leg.bid, candidate_leg.ask)?;
    first_attempt(candidate_leg.role.exit_side(), bid, ask, tick, config)
}

/// `04` §6.1 over every leg in and out at the same quotes.
pub fn round_trip(
    legs: &[CandidateLeg],
    quantity: i64,
    tick: Decimal,
    rates: &CostRates,
    execution: &ExecutionConfig,
) -> CoreResult<Decimal> {
    charges(&round_trip_fills(legs, quantity, tick, execution)?, rates)?.total()
}

/// The round trip's orders: every leg in at its limit, then out at the opposite-side limit.
pub fn round_trip_fills(
    legs: &[CandidateLeg],
    quantity: i64,
    tick: Decimal,
    execution: &ExecutionConfig,
) -> CoreResult<Vec<CostFill>> {
    let mut fills: Vec<CostFill> = legs
        .iter()
        .map(|lg| CostFill {
            side: lg.entry_side(),
            price: lg.limit_price,
            quantity,
        })
        .collect();
    for lg in legs {
        fills.push(CostFill {
            side: lg.role.exit_side(),
            price: exit_price(lg, tick, execution)?,
            quantity,
        });
    }
    Ok(fills)
}

/// A sleeve's priced structure — or the one code that stopped it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Candidate {
    /// The structure.
    pub structure: Structure,
    /// Expiry.
    pub expiry: NaiveDate,
    /// Direction (O2/O3).
    pub direction: Option<Direction>,
    /// Legs.
    pub legs: Vec<CandidateLeg>,
    /// Premium per unit.
    pub points: Option<Decimal>,
    /// Lot size.
    pub lot_size: Option<i64>,
    /// Lots; 0 iff rejected.
    pub lots: i64,
    /// How it was sized.
    pub sizing_mode: Option<SizingMode>,
    /// Risk per lot.
    pub risk_per_lot_inr: Option<Decimal>,
    /// R.
    pub r_inr: Option<Decimal>,
    /// Max loss.
    pub max_loss_inr: Option<Decimal>,
    /// Round-trip costs.
    pub round_trip_inr: Option<Decimal>,
    /// Expected gain.
    pub expected_gain_inr: Option<Decimal>,
    /// Cost share.
    pub cost_share: Option<Decimal>,
    /// The refusal.
    pub rejection: Option<Rejection>,
    /// In words.
    pub message: String,
    /// First-live half size.
    pub half_size: bool,
    /// Warnings.
    pub warnings: Vec<String>,
    /// Extra string fields.
    pub extra: BTreeMap<String, String>,
}

impl Candidate {
    /// No rejection.
    pub fn viable(&self) -> bool {
        self.rejection.is_none()
    }
}

/// What was known when a rule refused.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Known {
    /// Direction.
    pub direction: Option<Direction>,
    /// Legs.
    pub legs: Vec<CandidateLeg>,
    /// Premium per unit.
    pub points: Option<Decimal>,
    /// Lot size.
    pub lot_size: Option<i64>,
    /// Extra fields.
    pub extra: BTreeMap<String, String>,
}

/// A rejected candidate.
pub fn rejected(
    structure: Structure,
    expiry: NaiveDate,
    code: Rejection,
    message: String,
    known: Known,
) -> Candidate {
    Candidate {
        structure,
        expiry,
        direction: known.direction,
        legs: known.legs,
        points: known.points,
        lot_size: known.lot_size,
        lots: 0,
        sizing_mode: None,
        risk_per_lot_inr: None,
        r_inr: None,
        max_loss_inr: None,
        round_trip_inr: None,
        expected_gain_inr: None,
        cost_share: None,
        rejection: Some(code),
        message,
        half_size: false,
        warnings: Vec::new(),
        extra: known.extra,
    }
}

/// What sizing needs to know about the sleeve.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct SleeveBook {
    /// Paper or live.
    pub mode: Mode,
    /// Capital.
    pub sleeve_capital_inr: Decimal,
    /// The sleeve's own risk percent.
    pub risk_per_trade_pct: Option<Decimal>,
    /// The sleeve's own lot cap.
    pub max_lots: Option<i64>,
    /// Real journal rows.
    pub real_journal_rows: i64,
}

impl Default for SleeveBook {
    fn default() -> Self {
        Self {
            mode: Mode::Paper,
            sleeve_capital_inr: Decimal::ZERO,
            risk_per_trade_pct: None,
            max_lots: None,
            real_journal_rows: 0,
        }
    }
}

/// `sizing.size` with the sleeve's own settings, falling back to `04`'s defaults.
pub fn size_for(
    book: &SleeveBook,
    default_risk_pct: Decimal,
    default_max_lots: i64,
    risk_per_lot_inr: Decimal,
    lot_size: Option<i64>,
    config: &SizingConfig,
    ceilings: &OptionsCeilings,
) -> CoreResult<Sizing> {
    size(
        &SizeInputs {
            mode: book.mode,
            sleeve_capital_inr: book.sleeve_capital_inr,
            risk_per_trade_pct: book.risk_per_trade_pct.unwrap_or(default_risk_pct),
            max_lots: book.max_lots.unwrap_or(default_max_lots),
            risk_per_lot_inr,
            lot_size,
            real_journal_rows: book.real_journal_rows,
        },
        config,
        ceilings,
    )
}

/// `04` §6.4: the share (`None` when the gain cannot pay) and whether it passes.
pub fn cost_gate(
    round_trip_inr: Decimal,
    expected_gain_inr: Decimal,
    share_max: Decimal,
) -> CoreResult<(Option<Decimal>, bool)> {
    let verdict = cost_test(round_trip_inr, expected_gain_inr, share_max)?;
    let passes = verdict.cost_share.is_some_and(|s| s <= share_max);
    debug_assert!(passes == (verdict.verdict == CostVerdict::Ok));
    Ok((verdict.cost_share, passes))
}

fn money(v: Option<Decimal>) -> CoreResult<Option<String>> {
    num::qstr(v, 2, Rounding::HalfUp)
}

fn greek(v: Option<f64>) -> CoreResult<Option<String>> {
    v.map(|x| num::quantize(from_float(x)?, 6, Rounding::HalfUp).map(py_str))
        .transpose()
}

/// `op_scan.candidates`' leg element.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct LegJson {
    /// Role.
    pub role: String,
    /// Strike.
    pub strike: Option<String>,
    /// Type.
    pub option_type: String,
    /// Expiry.
    pub expiry: String,
    /// Token.
    pub instrument_token: i64,
    /// Side.
    pub side: String,
    /// Bid.
    pub bid: Option<String>,
    /// Ask.
    pub ask: Option<String>,
    /// IV.
    pub iv: Option<String>,
    /// Delta.
    pub delta: Option<String>,
    /// Limit.
    pub limit_price: Option<String>,
}

/// A leg as `op_scan.candidates` stores it.
pub fn leg_json(lg: &CandidateLeg) -> CoreResult<LegJson> {
    Ok(LegJson {
        role: lg.role.value().to_owned(),
        strike: money(Some(lg.strike))?,
        option_type: lg.option_type.value().to_owned(),
        expiry: lg.expiry.to_string(),
        instrument_token: lg.instrument_token,
        side: lg.entry_side().value().to_owned(),
        bid: money(lg.bid)?,
        ask: money(lg.ask)?,
        iv: greek(lg.iv)?,
        delta: greek(lg.delta)?,
        limit_price: money(Some(lg.limit_price))?,
    })
}

/// `op_scan.candidates`' element: strings for money and prices, never floats.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CandidateJson {
    /// Structure.
    pub structure: String,
    /// Expiry.
    pub expiry: String,
    /// Direction.
    pub direction: Option<String>,
    /// Legs.
    pub legs: Vec<LegJson>,
    /// Points.
    pub points: Option<String>,
    /// Lot size.
    pub lot_size: Option<i64>,
    /// Lots.
    pub lots: i64,
    /// Sizing mode.
    pub sizing_mode: Option<String>,
    /// Half size.
    pub half_size: bool,
    /// Risk per lot.
    pub risk_per_lot_inr: Option<String>,
    /// R.
    pub r_inr: Option<String>,
    /// Max loss.
    pub max_loss_inr: Option<String>,
    /// Round trip.
    pub round_trip_inr: Option<String>,
    /// Expected gain.
    pub expected_gain_inr: Option<String>,
    /// Cost share.
    pub cost_share: Option<String>,
    /// Rejection.
    pub rejection: Option<String>,
    /// Message.
    pub message: String,
    /// Warnings.
    pub warnings: Vec<String>,
    /// Extra fields, sorted by key.
    #[serde(flatten)]
    pub extra: BTreeMap<String, String>,
}

/// A candidate as `op_scan.candidates` stores it.
pub fn to_json(c: &Candidate) -> CoreResult<CandidateJson> {
    Ok(CandidateJson {
        structure: c.structure.value().to_owned(),
        expiry: c.expiry.to_string(),
        direction: c.direction.map(|d| d.value().to_owned()),
        legs: c.legs.iter().map(leg_json).collect::<CoreResult<_>>()?,
        points: money(c.points)?,
        lot_size: c.lot_size,
        lots: c.lots,
        sizing_mode: c.sizing_mode.map(|m| m.value().to_owned()),
        half_size: c.half_size,
        risk_per_lot_inr: money(c.risk_per_lot_inr)?,
        r_inr: money(c.r_inr)?,
        max_loss_inr: money(c.max_loss_inr)?,
        round_trip_inr: money(c.round_trip_inr)?,
        expected_gain_inr: money(c.expected_gain_inr)?,
        cost_share: num::qstr(c.cost_share, 4, Rounding::HalfUp)?,
        rejection: c.rejection.map(|r| r.value().to_owned()),
        message: c.message.clone(),
        warnings: c.warnings.clone(),
        extra: c.extra.clone(),
    })
}
