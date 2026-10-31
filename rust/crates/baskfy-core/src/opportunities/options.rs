//! Port of `baskfy_core.opportunities.options`: listed option structures compared on live quotes.

use std::collections::{BTreeMap, BTreeSet};

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::decimal::Rounding;
use crate::error::{CoreError, CoreResult};
use crate::opportunities::config::{
    BULLISH_STRATEGIES, Direction, OptionsParams, QuoteRules, StrategyKind,
};
use crate::opportunities::ranking::{DEFAULT_OPTION_WEIGHTS, ScoreBreakdown, score_option};
use crate::options::config::{CostRates, OptionType, Side};
use crate::options::costs::{CostFill, charges};
use crate::options::greeks::{Bracket, SolveLimits, solve, year_fraction};
use crate::options::num::{self, ist_offset, py_str, to_float};

/// The settlement clock on expiry day.
pub fn settle() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 30, 0).unwrap_or(NaiveTime::MIN)
}
const DAYS_PER_YEAR: f64 = 365.0;
/// How the legs' greeks are produced.
pub const GREEKS_SOURCE: &str = "MODEL_BLACK76_SPOT_FORWARD_R0";
/// The disclosure shown beside model greeks.
pub const MODEL_NOTE: &str = "IV and delta are model-derived: Black-76 solved on each leg's bid/ask mid with spot as the \
forward and a zero rate. They are estimates, not exchange data. No probability of profit \
or historical IV percentile is computed.";

fn expresses(kind: StrategyKind, direction: Direction) -> bool {
    BULLISH_STRATEGIES.contains(&kind) == (direction == Direction::Bullish)
}

// A negative Python slice bound here would be a misconfiguration; treat it as zero.
fn to_usize(n: i64) -> usize {
    usize::try_from(n).unwrap_or(0)
}

// ---- quotes ----

/// One listed contract's quote as fetched.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ContractQuote {
    /// Exchange symbol.
    pub tradingsymbol: String,
    /// Underlying symbol.
    pub underlying: String,
    /// Expiry date.
    pub expiry: NaiveDate,
    /// Strike.
    pub strike: Decimal,
    /// CE or PE.
    pub option_type: OptionType,
    /// Units per lot.
    pub lot_size: i64,
    /// Best bid.
    pub bid: Option<Decimal>,
    /// Best ask.
    pub ask: Option<Decimal>,
    /// Traded volume.
    pub volume: Option<i64>,
    /// Open interest.
    pub oi: Option<i64>,
    /// Exchange timestamp.
    pub quote_ts: Option<DateTime<FixedOffset>>,
}

/// A contract or structure refused, and why.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Rejection {
    /// The contract, or the legs joined by `/`.
    pub tradingsymbol: String,
    /// Why.
    pub reason: String,
}

fn reject(tradingsymbol: &str, reason: impl Into<String>) -> Rejection {
    Rejection {
        tradingsymbol: tradingsymbol.to_owned(),
        reason: reason.into(),
    }
}

/// A quote that passed every rule.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AcceptedQuote {
    /// The contract.
    pub contract: ContractQuote,
    /// Bid.
    pub bid: Decimal,
    /// Ask.
    pub ask: Decimal,
    /// Seconds since the exchange timestamp.
    pub quote_age_s: i64,
    /// Spread, percent of mid, to the paisa.
    pub spread_pct: Decimal,
}

impl AcceptedQuote {
    /// `(bid + ask) / 2`.
    pub fn mid(&self) -> CoreResult<Decimal> {
        num::div(num::add(self.bid, self.ask)?, Decimal::TWO)
    }
}

fn age(quote_ts: &DateTime<FixedOffset>, now: &DateTime<FixedOffset>) -> i64 {
    (*now - *quote_ts).num_seconds().max(0)
}

fn spread_pct(bid: Decimal, ask: Decimal) -> CoreResult<Decimal> {
    let mid = num::div(num::add(bid, ask)?, Decimal::TWO)?;
    let ratio = num::div(num::sub(ask, bid)?, mid)?;
    num::quantize(num::mul(ratio, Decimal::ONE_HUNDRED)?, 2, Rounding::HalfUp)
}

fn quote_problem(
    quote: &ContractQuote,
    underlying: &str,
    now: &DateTime<FixedOffset>,
    rules: &QuoteRules,
) -> CoreResult<Option<String>> {
    if quote.underlying.to_uppercase() != underlying.to_uppercase() {
        return Ok(Some(format!(
            "underlying {} does not match {underlying}",
            quote.underlying
        )));
    }
    if quote.lot_size <= 0 {
        return Ok(Some("lot size missing".into()));
    }
    let (Some(bid), Some(ask)) = (quote.bid, quote.ask) else {
        return Ok(Some("incomplete quote: no two-sided market".into()));
    };
    if bid <= Decimal::ZERO || ask <= Decimal::ZERO {
        return Ok(Some("incomplete quote: no two-sided market".into()));
    }
    let Some(ts) = quote.quote_ts else {
        return Ok(Some("incomplete quote: no exchange timestamp".into()));
    };
    if bid > ask {
        return Ok(Some(format!(
            "crossed quote: bid {} above ask {}",
            py_str(bid),
            py_str(ask)
        )));
    }
    let quote_age = age(&ts, now);
    if quote_age > rules.max_quote_age_s {
        return Ok(Some(format!(
            "stale quote: {quote_age}s old (limit {}s)",
            rules.max_quote_age_s
        )));
    }
    let spread = spread_pct(bid, ask)?;
    if spread > rules.max_spread_pct {
        return Ok(Some(format!(
            "spread {}% exceeds {}%",
            py_str(spread),
            py_str(rules.max_spread_pct)
        )));
    }
    let oi = quote.oi.unwrap_or(0);
    if oi < rules.min_oi {
        return Ok(Some(format!("open interest {oi} below {}", rules.min_oi)));
    }
    let volume = quote.volume.unwrap_or(0);
    if volume < rules.min_volume {
        return Ok(Some(format!("volume {volume} below {}", rules.min_volume)));
    }
    Ok(None)
}

/// Accepts a quote or says why not.
pub fn validate_quote(
    quote: &ContractQuote,
    underlying: &str,
    now: &DateTime<FixedOffset>,
    rules: &QuoteRules,
) -> CoreResult<Result<AcceptedQuote, Rejection>> {
    let problem = quote_problem(quote, underlying, now, rules)?;
    let (Some(bid), Some(ask), Some(ts), None) = (quote.bid, quote.ask, quote.quote_ts, &problem)
    else {
        let reason = problem.unwrap_or_else(|| "incomplete quote".into());
        return Ok(Err(reject(&quote.tradingsymbol, reason)));
    };
    Ok(Ok(AcceptedQuote {
        contract: quote.clone(),
        bid,
        ask,
        quote_age_s: age(&ts, now),
        spread_pct: spread_pct(bid, ask)?,
    }))
}

// ---- structures ----

/// One leg of a structure.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Leg {
    /// Its accepted quote.
    pub quote: AcceptedQuote,
    /// Bought or sold.
    pub side: Side,
    /// Model IV, when it solved.
    pub iv: Option<f64>,
    /// Model delta, when it solved.
    pub delta: Option<f64>,
}

impl Leg {
    /// Ask to buy, bid to sell.
    pub fn exec_price(&self) -> Decimal {
        match self.side {
            Side::Buy => self.quote.ask,
            Side::Sell => self.quote.bid,
        }
    }

    /// The contract's strike.
    pub fn strike(&self) -> Decimal {
        self.quote.contract.strike
    }

    /// The contract's type.
    pub fn option_type(&self) -> OptionType {
        self.quote.contract.option_type
    }
}

/// P&L at one underlying level on expiry.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PayoffPoint {
    /// Underlying level.
    pub underlying: Decimal,
    /// P&L per lot.
    pub pnl_per_lot: Decimal,
}

/// A sized, scored, actionable structure.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OptionCandidate {
    /// The structure.
    pub strategy: StrategyKind,
    /// Expiry.
    pub expiry: NaiveDate,
    /// Days to expiry.
    pub dte: i64,
    /// Units per lot.
    pub lot_size: i64,
    /// Lots within the loss limit.
    pub lots: i64,
    /// The legs.
    pub legs: Vec<Leg>,
    /// Net debit per unit.
    pub net_debit_per_unit: Decimal,
    /// Max loss per unit.
    pub max_loss_per_unit: Decimal,
    /// Max profit per unit; `None` when unlimited.
    pub max_profit_per_unit: Option<Decimal>,
    /// Breakevens, to the paisa.
    pub breakevens: Vec<Decimal>,
    /// Entry charges, one lot.
    pub entry_cost_inr: Decimal,
    /// Entry plus exit charges, one lot.
    pub round_trip_cost_inr: Decimal,
    /// Expiry payoff grid.
    pub payoff: Vec<PayoffPoint>,
    /// Score.
    pub score: ScoreBreakdown,
    /// Caveats.
    pub limitations: Vec<String>,
    /// Always true for a built candidate.
    pub actionable: bool,
}

impl OptionCandidate {
    /// `STRATEGY|SYM1|SYM2`.
    pub fn id(&self) -> String {
        std::iter::once(self.strategy.value())
            .chain(self.legs.iter().map(|l| l.quote.contract.tradingsymbol.as_str()))
            .collect::<Vec<_>>()
            .join("|")
    }

    /// Net debit per lot.
    pub fn net_debit_per_lot(&self) -> CoreResult<Decimal> {
        num::muli(self.net_debit_per_unit, self.lot_size)
    }

    /// Max loss per lot.
    pub fn max_loss_per_lot(&self) -> CoreResult<Decimal> {
        num::muli(self.max_loss_per_unit, self.lot_size)
    }

    /// Max profit per lot; `None` when unlimited.
    pub fn max_profit_per_lot(&self) -> CoreResult<Option<Decimal>> {
        self.max_profit_per_unit
            .map(|p| num::muli(p, self.lot_size))
            .transpose()
    }

    /// Max loss per lot plus round-trip charges.
    pub fn max_loss_after_costs_per_lot(&self) -> CoreResult<Decimal> {
        num::add(self.max_loss_per_lot()?, self.round_trip_cost_inr)
    }
}

/// One leg's intrinsic value at `underlying`, signed by side.
pub fn leg_payoff_per_unit(leg: &Leg, underlying: Decimal) -> CoreResult<Decimal> {
    let value = match leg.option_type() {
        OptionType::Ce => num::max(num::sub(underlying, leg.strike())?, Decimal::ZERO),
        OptionType::Pe => num::max(num::sub(leg.strike(), underlying)?, Decimal::ZERO),
    };
    match leg.side {
        Side::Buy => Ok(value),
        Side::Sell => num::neg(value),
    }
}

/// The structure's P&L per unit at expiry.
pub fn expiry_pnl_per_unit(legs: &[Leg], underlying: Decimal) -> CoreResult<Decimal> {
    let parts = legs
        .iter()
        .map(|l| leg_payoff_per_unit(l, underlying))
        .collect::<CoreResult<Vec<_>>>()?;
    num::sub(num::sum(parts)?, net_debit(legs)?)
}

/// Bought premium less sold premium, at executable prices.
pub fn net_debit(legs: &[Leg]) -> CoreResult<Decimal> {
    let parts = legs
        .iter()
        .map(|l| match l.side {
            Side::Buy => Ok(l.exec_price()),
            Side::Sell => num::neg(l.exec_price()),
        })
        .collect::<CoreResult<Vec<_>>>()?;
    num::sum(parts)
}

/// Debit, risk, reward and breakevens per unit.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StructureMetrics {
    /// Net debit.
    pub debit: Decimal,
    /// Max loss.
    pub max_loss: Decimal,
    /// Max profit; `None` when unlimited.
    pub max_profit: Option<Decimal>,
    /// Breakevens.
    pub breakevens: Vec<Decimal>,
}

/// The structure's metrics, or why it cannot be held (`Err` inside).
pub fn structure_metrics(
    kind: StrategyKind,
    legs: &[Leg],
) -> CoreResult<Result<StructureMetrics, String>> {
    let debit = net_debit(legs)?;
    if debit <= Decimal::ZERO {
        return Ok(Err("non-positive net debit from quotes".into()));
    }
    let Some(first) = legs.first() else {
        return Err(CoreError::InvalidInput("no legs".into()));
    };
    let metrics = |max_profit: Option<Decimal>, breakeven: Decimal| StructureMetrics {
        debit,
        max_loss: debit,
        max_profit,
        breakevens: vec![breakeven],
    };
    match kind {
        StrategyKind::LongCall => {
            return Ok(Ok(metrics(None, num::add(first.strike(), debit)?)));
        }
        StrategyKind::LongPut => {
            let be = num::sub(first.strike(), debit)?;
            return Ok(Ok(metrics(Some(be), be)));
        }
        StrategyKind::BullCallSpread | StrategyKind::BearPutSpread => {}
    }
    let Some(second) = legs.get(1) else {
        return Err(CoreError::InvalidInput("spread needs two legs".into()));
    };
    let width = num::abs(num::sub(first.strike(), second.strike())?)?;
    if debit >= width {
        return Ok(Err(format!(
            "debit {} not below spread width {}",
            py_str(debit),
            py_str(width)
        )));
    }
    let profit = num::sub(width, debit)?;
    let be = if kind == StrategyKind::BullCallSpread {
        num::add(first.strike(), debit)?
    } else {
        num::sub(first.strike(), debit)?
    };
    Ok(Ok(metrics(Some(profit), be)))
}

fn shape(kind: StrategyKind) -> &'static [(OptionType, Side)] {
    match kind {
        StrategyKind::LongCall => &[(OptionType::Ce, Side::Buy)],
        StrategyKind::LongPut => &[(OptionType::Pe, Side::Buy)],
        StrategyKind::BullCallSpread => &[(OptionType::Ce, Side::Buy), (OptionType::Ce, Side::Sell)],
        StrategyKind::BearPutSpread => &[(OptionType::Pe, Side::Buy), (OptionType::Pe, Side::Sell)],
    }
}

fn distinct<T: Ord>(items: impl Iterator<Item = T>) -> usize {
    items.collect::<BTreeSet<_>>().len()
}

/// Why the legs do not form `kind`, or `None`.
pub fn validate_legs(kind: StrategyKind, legs: &[Leg]) -> Option<String> {
    let contracts = || legs.iter().map(|l| &l.quote.contract);
    let strike_order = |ok: fn(Decimal, Decimal) -> bool| match (legs.first(), legs.get(1)) {
        (Some(a), Some(b)) => ok(a.strike(), b.strike()),
        _ => false,
    };
    let checks: [(&dyn Fn() -> bool, &str); 6] = [
        (
            &|| distinct(contracts().map(|c| c.underlying.as_str())) == 1,
            "legs on different underlyings",
        ),
        (
            &|| distinct(contracts().map(|c| c.expiry)) == 1,
            "legs on different expiries",
        ),
        (
            &|| distinct(contracts().map(|c| c.lot_size)) == 1,
            "legs with different lot sizes",
        ),
        (
            &|| {
                legs.iter()
                    .map(|l| (l.option_type(), l.side))
                    .eq(shape(kind).iter().copied())
            },
            "leg types do not match the strategy",
        ),
        (
            &|| kind != StrategyKind::BullCallSpread || strike_order(|a, b| a < b),
            "bull call spread needs the bought strike below the sold strike",
        ),
        (
            &|| kind != StrategyKind::BearPutSpread || strike_order(|a, b| a > b),
            "bear put spread needs the bought strike above the sold strike",
        ),
    ];
    checks
        .iter()
        .find(|(ok, _)| !ok())
        .map(|(_, message)| (*message).to_owned())
}

/// Entry charges and entry-plus-exit charges for `lots` lots (exit at the far side).
pub fn round_trip_costs(legs: &[Leg], lots: i64, rates: &CostRates) -> CoreResult<(Decimal, Decimal)> {
    let Some(first) = legs.first() else {
        return Err(CoreError::InvalidInput("no legs".into()));
    };
    let lot = first.quote.contract.lot_size * lots;
    let entry: Vec<CostFill> = legs
        .iter()
        .map(|l| CostFill {
            side: l.side,
            price: l.exec_price(),
            quantity: lot,
        })
        .collect();
    let exit: Vec<CostFill> = legs
        .iter()
        .map(|l| match l.side {
            Side::Buy => CostFill {
                side: Side::Sell,
                price: l.quote.bid,
                quantity: lot,
            },
            Side::Sell => CostFill {
                side: Side::Buy,
                price: l.quote.ask,
                quantity: lot,
            },
        })
        .collect();
    let entry_total = charges(&entry, rates)?.total()?;
    Ok((
        entry_total,
        num::add(entry_total, charges(&exit, rates)?.total()?)?,
    ))
}

/// Expiry P&L per lot over spot ± the range, plus strikes, breakevens and spot.
pub fn payoff_grid(
    legs: &[Leg],
    spot: Decimal,
    breakevens: &[Decimal],
    params: &OptionsParams,
) -> CoreResult<Vec<PayoffPoint>> {
    let Some(first) = legs.first() else {
        return Err(CoreError::InvalidInput("no legs".into()));
    };
    let span = num::div(num::mul(spot, params.payoff_range_pct)?, Decimal::ONE_HUNDRED)?;
    let (low, high) = (num::sub(spot, span)?, num::add(spot, span)?);
    let steps = (params.payoff_points - 1).max(1);
    let width = num::sub(high, low)?;
    let mut xs = Vec::new();
    for i in 0..=steps {
        let step = num::div(num::muli(width, i)?, Decimal::from(steps))?;
        xs.push(num::add(low, step)?);
    }
    xs.extend(legs.iter().map(Leg::strike).filter(|k| low <= *k && *k <= high));
    xs.extend(breakevens.iter().copied().filter(|b| low <= *b && *b <= high));
    xs.push(spot);
    // Python's set: numerically equal values collapse before rounding.
    xs.sort();
    xs.dedup();
    let lot = first.quote.contract.lot_size;
    xs.into_iter()
        .filter(|x| *x > Decimal::ZERO)
        .map(|x| {
            Ok(PayoffPoint {
                underlying: num::paise(x)?,
                pnl_per_lot: num::paise(num::muli(expiry_pnl_per_unit(legs, x)?, lot)?)?,
            })
        })
        .collect()
}

fn model_leg(
    quote: &AcceptedQuote,
    side: Side,
    spot: Decimal,
    now: &DateTime<FixedOffset>,
    params: &OptionsParams,
) -> CoreResult<Leg> {
    let contract = &quote.contract;
    let years = year_fraction(&now.with_timezone(&ist_offset()), contract.expiry, settle());
    let limits = SolveLimits {
        min_premium: params.min_model_premium,
        tick: 0.05,
        bracket: Bracket {
            lower: params.iv_lower,
            upper: params.iv_upper,
            tolerance: params.iv_tolerance,
        },
    };
    let greeks = solve(
        to_float(quote.mid()?),
        to_float(spot),
        to_float(contract.strike),
        years,
        params.model_rate,
        contract.option_type,
        limits,
    )?;
    Ok(Leg {
        quote: quote.clone(),
        side,
        iv: greeks.as_ref().map(|g| g.iv),
        delta: greeks.as_ref().map(|g| g.delta),
    })
}

// Python's `int(a // b)` on Decimals: the quotient truncated toward zero, exactly.
fn int_div(a: Decimal, b: Decimal) -> CoreResult<i64> {
    let overflow = || CoreError::Arithmetic(format!("{a} // {b} overflowed"));
    let mut q = a.checked_div(b).ok_or_else(overflow)?.trunc();
    let (abs_a, abs_b) = (a.abs(), b.abs());
    let step = if (a < Decimal::ZERO) == (b < Decimal::ZERO) {
        Decimal::ONE
    } else {
        -Decimal::ONE
    };
    while q.abs().checked_mul(abs_b).ok_or_else(overflow)? > abs_a {
        q -= step;
    }
    while (q.abs() + Decimal::ONE).checked_mul(abs_b).ok_or_else(overflow)? <= abs_a {
        q += step;
    }
    num::floor_int(q)
}

fn sizing(max_loss_after_costs_per_lot: Decimal, max_loss_inr: Option<Decimal>) -> CoreResult<i64> {
    let Some(limit) = max_loss_inr else {
        return Ok(1);
    };
    if max_loss_after_costs_per_lot <= Decimal::ZERO {
        return Ok(0);
    }
    int_div(limit, max_loss_after_costs_per_lot)
}

/// What the user asked to compare.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OptionsRequest {
    /// Underlying symbol.
    pub underlying: String,
    /// The view.
    pub direction: Direction,
    /// Structures asked for.
    pub strategies: BTreeSet<StrategyKind>,
    /// Loss limit after costs; `None` sizes one lot.
    pub max_loss_inr: Option<Decimal>,
    /// DTE window low.
    pub min_dte: i64,
    /// DTE window high.
    pub max_dte: i64,
    /// Quote-quality limits.
    pub rules: QuoteRules,
}

/// The ranked candidates and everything refused.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OptionsResult {
    /// Best first.
    pub candidates: Vec<OptionCandidate>,
    /// Quotes and structures refused.
    pub rejected: Vec<Rejection>,
    /// Expiries considered.
    pub expiries: Vec<NaiveDate>,
    /// Caveats.
    pub warnings: Vec<String>,
}

fn strike_window(strikes: &[Decimal], spot: Decimal, each_side: usize) -> CoreResult<Vec<Decimal>> {
    let ordered: Vec<Decimal> = strikes.iter().copied().collect::<BTreeSet<_>>().into_iter().collect();
    let mut atm: Option<(Decimal, Decimal, usize)> = None;
    for (i, k) in ordered.iter().enumerate() {
        let key = (num::abs(num::sub(*k, spot)?)?, *k);
        if atm.is_none_or(|(d, s, _)| key < (d, s)) {
            atm = Some((key.0, key.1, i));
        }
    }
    let Some((_, _, index)) = atm else {
        return Ok(Vec::new());
    };
    let start = index.saturating_sub(each_side);
    let end = (index + each_side + 1).min(ordered.len());
    Ok(ordered[start..end].to_vec())
}

fn dte(expiry: NaiveDate, now: &DateTime<FixedOffset>) -> i64 {
    (expiry - now.with_timezone(&ist_offset()).date_naive()).num_days()
}

/// Every leg combination of `kind` inside the strike window.
pub fn candidate_structures(
    kind: StrategyKind,
    by_strike: &BTreeMap<(Decimal, OptionType), AcceptedQuote>,
    window: &[Decimal],
    params: &OptionsParams,
) -> Vec<Vec<(AcceptedQuote, Side)>> {
    let option_type = if matches!(kind, StrategyKind::LongCall | StrategyKind::BullCallSpread) {
        OptionType::Ce
    } else {
        OptionType::Pe
    };
    let available: Vec<&AcceptedQuote> = window
        .iter()
        .filter_map(|k| by_strike.get(&(*k, option_type)))
        .collect();
    if matches!(kind, StrategyKind::LongCall | StrategyKind::LongPut) {
        return available
            .into_iter()
            .map(|q| vec![(q.clone(), Side::Buy)])
            .collect();
    }
    let steps = to_usize(params.max_spread_width_steps);
    let mut out = Vec::new();
    for (i, long) in available.iter().enumerate() {
        let shorts: Vec<&AcceptedQuote> = if kind == StrategyKind::BullCallSpread {
            available.iter().skip(i + 1).take(steps).copied().collect()
        } else {
            available[i.saturating_sub(steps)..i].iter().rev().copied().collect()
        };
        out.extend(
            shorts
                .into_iter()
                .map(|short| vec![((*long).clone(), Side::Buy), (short.clone(), Side::Sell)]),
        );
    }
    out
}

/// Validates the quotes, builds every allowed structure, and ranks them.
pub fn analyze(
    request: &OptionsRequest,
    quotes: &[ContractQuote],
    spot: Decimal,
    now: &DateTime<FixedOffset>,
    rates: &CostRates,
    params: &OptionsParams,
) -> CoreResult<OptionsResult> {
    let mut kinds: Vec<StrategyKind> = request
        .strategies
        .iter()
        .copied()
        .filter(|k| expresses(*k, request.direction))
        .collect();
    kinds.sort_by_key(|k| k.value());
    let mut warnings = Vec::new();
    let mut dropped: Vec<&str> = request
        .strategies
        .iter()
        .filter(|k| !expresses(**k, request.direction))
        .map(|k| k.value())
        .collect();
    dropped.sort_unstable();
    if !dropped.is_empty() {
        warnings.push(format!(
            "{} skipped: they do not express a {} view.",
            dropped.join(", "),
            request.direction.value()
        ));
    }
    let underlying = request.underlying.to_uppercase();
    let in_range: Vec<NaiveDate> = quotes
        .iter()
        .filter(|q| {
            let d = dte(q.expiry, now);
            request.min_dte <= d && d <= request.max_dte && q.underlying.to_uppercase() == underlying
        })
        .map(|q| q.expiry)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .take(to_usize(params.max_expiries))
        .collect();
    if in_range.is_empty() {
        warnings.push(format!(
            "No listed expiry within {}-{} days to expiry.",
            request.min_dte, request.max_dte
        ));
    }
    let mut rejected = Vec::new();
    let mut accepted: BTreeMap<NaiveDate, Vec<AcceptedQuote>> = BTreeMap::new();
    for quote in quotes {
        if !in_range.contains(&quote.expiry) {
            continue;
        }
        match validate_quote(quote, &request.underlying, now, &request.rules)? {
            Err(r) => rejected.push(r),
            Ok(a) => accepted.entry(quote.expiry).or_default().push(a),
        }
    }
    let mut candidates = Vec::new();
    for expiry in &in_range {
        let pool = accepted.get(expiry).map(Vec::as_slice).unwrap_or_default();
        let lot_sizes: BTreeSet<i64> = pool.iter().map(|q| q.contract.lot_size).collect();
        if lot_sizes.len() > 1 {
            let sizes: Vec<String> = lot_sizes.iter().map(i64::to_string).collect();
            warnings.push(format!(
                "Expiry {expiry} lists mixed lot sizes [{}].",
                sizes.join(", ")
            ));
        }
        let by_strike: BTreeMap<(Decimal, OptionType), AcceptedQuote> = pool
            .iter()
            .map(|q| ((q.contract.strike, q.contract.option_type), q.clone()))
            .collect();
        let strikes: Vec<Decimal> = pool.iter().map(|q| q.contract.strike).collect();
        let window = strike_window(&strikes, spot, to_usize(params.strikes_each_side))?;
        for kind in &kinds {
            for structure in candidate_structures(*kind, &by_strike, &window, params) {
                let legs = structure
                    .iter()
                    .map(|(q, side)| model_leg(q, *side, spot, now, params))
                    .collect::<CoreResult<Vec<_>>>()?;
                match build_candidate(*kind, legs, spot, now, request, rates, params)? {
                    Err(r) => rejected.push(r),
                    Ok(c) => candidates.push(c),
                }
            }
        }
    }
    let mut keyed: Vec<(f64, String, OptionCandidate)> = candidates
        .into_iter()
        .map(|c| (c.score.total, c.id(), c))
        .collect();
    keyed.sort_by(|a, b| b.0.total_cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    Ok(OptionsResult {
        candidates: keyed
            .into_iter()
            .take(to_usize(params.max_candidates))
            .map(|(_, _, c)| c)
            .collect(),
        rejected,
        expiries: in_range,
        warnings,
    })
}

/// Validates, prices, sizes and scores one structure.
pub fn build_candidate(
    kind: StrategyKind,
    legs: Vec<Leg>,
    spot: Decimal,
    now: &DateTime<FixedOffset>,
    request: &OptionsRequest,
    rates: &CostRates,
    params: &OptionsParams,
) -> CoreResult<Result<OptionCandidate, Rejection>> {
    let label = legs
        .iter()
        .map(|l| l.quote.contract.tradingsymbol.as_str())
        .collect::<Vec<_>>()
        .join("/");
    if let Some(problem) = validate_legs(kind, &legs) {
        return Ok(Err(reject(&label, problem)));
    }
    let metrics = match structure_metrics(kind, &legs)? {
        Ok(m) => m,
        Err(problem) => return Ok(Err(reject(&label, problem))),
    };
    let Some(first) = legs.first() else {
        return Err(CoreError::InvalidInput("no legs".into()));
    };
    let lot_size = first.quote.contract.lot_size;
    let expiry = first.quote.contract.expiry;
    let (entry_cost, round_trip) = round_trip_costs(&legs, 1, rates)?;
    let max_loss_lot = num::muli(metrics.max_loss, lot_size)?;
    let max_loss_after_costs = num::add(max_loss_lot, round_trip)?;
    let lots = sizing(max_loss_after_costs, request.max_loss_inr)?;
    if lots < 1 {
        let limit = request.max_loss_inr.map(py_str).unwrap_or_else(|| "None".into());
        return Ok(Err(reject(
            &label,
            format!(
                "one lot risks ₹{} after costs, above the ₹{limit} limit",
                py_str(max_loss_after_costs)
            ),
        )));
    }
    let dte = dte(expiry, now);
    let first_iv = legs
        .iter()
        .find_map(|l| if l.side == Side::Buy { l.iv } else { None });
    #[allow(clippy::cast_precision_loss)]
    let years = dte.max(0) as f64 / DAYS_PER_YEAR;
    let expected_move_pct = first_iv.filter(|_| years > 0.0).map(|iv| iv * years.sqrt() * 100.0);
    let first_be = metrics
        .breakevens
        .first()
        .copied()
        .ok_or_else(|| CoreError::InvalidInput("no breakeven".into()))?;
    let breakeven_move = (to_float(num::sub(first_be, spot)?) / to_float(spot) * 100.0).abs();
    let payoff_ratio = metrics
        .max_profit
        .map(|p| num::div(p, metrics.max_loss).map(to_float))
        .transpose()?;
    let mut limitations = Vec::new();
    if legs.iter().any(|l| l.iv.is_none()) {
        limitations.push("Model IV did not solve for at least one leg; Greeks omitted there.".to_owned());
    }
    if metrics.max_profit.is_none() {
        limitations.push("Maximum profit is unlimited; payoff score is neutral.".to_owned());
    }
    if dte <= 1 {
        limitations.push("Expires within a day; exercise/settlement STT may apply if held.".to_owned());
    }
    let worst_spread = legs
        .iter()
        .map(|l| l.quote.spread_pct)
        .max()
        .unwrap_or(Decimal::ZERO);
    let cost_share = if max_loss_lot > Decimal::ZERO {
        to_float(num::div(round_trip, max_loss_lot)?)
    } else {
        1.0
    };
    let score = score_option(
        payoff_ratio,
        breakeven_move,
        expected_move_pct,
        to_float(worst_spread),
        to_float(request.rules.max_spread_pct),
        cost_share,
        dte,
        (params.preferred_dte_min, params.preferred_dte_max),
        &DEFAULT_OPTION_WEIGHTS,
    );
    let breakevens = metrics
        .breakevens
        .iter()
        .map(|b| num::paise(*b))
        .collect::<CoreResult<Vec<_>>>()?;
    let payoff = payoff_grid(&legs, spot, &metrics.breakevens, params)?;
    Ok(Ok(OptionCandidate {
        strategy: kind,
        expiry,
        dte,
        lot_size,
        lots,
        legs,
        net_debit_per_unit: metrics.debit,
        max_loss_per_unit: metrics.max_loss,
        max_profit_per_unit: metrics.max_profit,
        breakevens,
        entry_cost_inr: entry_cost,
        round_trip_cost_inr: round_trip,
        payoff,
        score,
        limitations,
        actionable: true,
    }))
}
