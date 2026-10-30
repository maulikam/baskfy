//! Port of `baskfy_core.options.chain`: quotes, ATM, strike step, the parity forward, liquidity, staleness.

use std::collections::{BTreeMap, BTreeSet};

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::decimal::WideDecimal;
use crate::error::{CoreError, CoreResult};
use crate::options::config::{ChainConfig, OiUnit, OptionType, Side};
use crate::options::greeks::{Bracket, Greeks, SolveLimits, solve, year_fraction};
use crate::options::num::{self, seconds_between};

/// One depth level: a price and the quantity resting there (units, not lots).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Level {
    /// Price at this level.
    pub price: Decimal,
    /// Units resting there.
    pub quantity: i64,
}

/// One contract's quote at one instant (`03` §5).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OptionQuote {
    /// The instrument token.
    pub instrument_token: i64,
    /// Expiry.
    pub expiry: NaiveDate,
    /// Strike.
    pub strike: Decimal,
    /// Call or put.
    pub option_type: OptionType,
    /// Best bid.
    pub bid: Option<Decimal>,
    /// Best ask.
    pub ask: Option<Decimal>,
    /// Bid ladder.
    #[serde(default)]
    pub bids: Vec<Level>,
    /// Ask ladder.
    #[serde(default)]
    pub asks: Vec<Level>,
    /// Open interest.
    pub oi: i64,
    /// When the quote was taken.
    pub ts: DateTime<FixedOffset>,
    /// Last traded price.
    #[serde(default)]
    pub last: Option<Decimal>,
    /// Volume.
    #[serde(default)]
    pub volume: i64,
}

impl OptionQuote {
    /// `(bid + ask) / 2` — `None` unless both sides are quoted and positive.
    pub fn mid(&self) -> CoreResult<Option<Decimal>> {
        match (self.bid, self.ask) {
            (Some(b), Some(a)) if b > Decimal::ZERO && a > Decimal::ZERO => {
                Ok(Some(num::div(num::add(b, a)?, Decimal::TWO)?))
            }
            _ => Ok(None),
        }
    }

    /// `(ask - bid) / mid * 100` (`04` §2.4).
    pub fn spread_pct(&self) -> CoreResult<Option<Decimal>> {
        let Some(mid) = self.mid()? else {
            return Ok(None);
        };
        match (self.bid, self.ask) {
            (Some(b), Some(a)) => Ok(Some(num::mul(
                num::div(num::sub(a, b)?, mid)?,
                Decimal::ONE_HUNDRED,
            )?)),
            _ => Ok(None),
        }
    }
}

/// The strike nearest `spot` on a `step` grid; a tie goes to the lower strike.
pub fn atm(spot: Decimal, step: Decimal) -> CoreResult<Decimal> {
    if step <= Decimal::ZERO {
        return Err(CoreError::InvalidInput(
            "strike step must be positive".to_owned(),
        ));
    }
    let below = num::mul(py_floordiv(spot, step)?, step)?;
    let above = num::add(below, step)?;
    if num::sub(spot, below)? <= num::sub(above, spot)? {
        Ok(below)
    } else {
        Ok(above)
    }
}

/// Python's `Decimal.__floordiv__`: the exact integer part of the quotient, toward zero.
fn py_floordiv(a: Decimal, b: Decimal) -> CoreResult<Decimal> {
    let overflow = || CoreError::Arithmetic(format!("{a} // {b} overflowed"));
    if b.is_zero() {
        return Err(CoreError::Arithmetic(format!("{a} // 0")));
    }
    let s = a.scale().max(b.scale());
    let lift = |m: i128, from: u32| -> CoreResult<i128> {
        10_i128
            .checked_pow(s - from)
            .and_then(|p| m.checked_mul(p))
            .ok_or_else(overflow)
    };
    let q = lift(a.mantissa(), a.scale())? / lift(b.mantissa(), b.scale())?;
    let mut out = Decimal::try_from_i128_with_scale(q, 0).map_err(|_| overflow())?;
    if out.is_zero() {
        out.set_sign_negative(a.is_sign_negative() != b.is_sign_negative());
    }
    Ok(out)
}

/// The modal difference between adjacent listed strikes near `near` (`04` §2.1).
pub fn strike_step(strikes: &[Decimal], near: Decimal, window: i64) -> CoreResult<Option<Decimal>> {
    let distinct: BTreeSet<Decimal> = strikes.iter().copied().collect();
    if distinct.len() < 2 {
        return Ok(None);
    }
    let mut keyed: Vec<(Decimal, Decimal)> = Vec::with_capacity(distinct.len());
    for k in &distinct {
        keyed.push((num::abs(num::sub(*k, near)?)?, *k));
    }
    keyed.sort();
    let take = usize::try_from((2 * window + 1).max(0)).unwrap_or(0);
    let mut nearest: Vec<Decimal> = keyed.into_iter().take(take).map(|(_, k)| k).collect();
    nearest.sort();
    let mut gaps: Vec<(Decimal, usize)> = Vec::new();
    for pair in nearest.windows(2) {
        let gap = num::sub(pair[1], pair[0])?;
        match gaps.iter_mut().find(|(g, _)| *g == gap) {
            Some(entry) => entry.1 += 1,
            None => gaps.push((gap, 1)),
        }
    }
    let Some(top) = gaps.iter().map(|(_, c)| *c).max() else {
        return Ok(None);
    };
    Ok(gaps
        .iter()
        .filter(|(_, c)| *c == top)
        .map(|(g, _)| *g)
        .reduce(num::min))
}

/// The listed strikes within `count` steps either side of ATM (`04` §2.1).
pub fn snapshot_strikes(
    listed: &[Decimal],
    spot: Decimal,
    step: Decimal,
    count: i64,
) -> CoreResult<Vec<Decimal>> {
    let centre = atm(spot, step)?;
    let span = num::muli(step, count)?;
    let (low, high) = (num::sub(centre, span)?, num::add(centre, span)?);
    let set: BTreeSet<Decimal> = listed.iter().copied().collect();
    Ok(set
        .into_iter()
        .filter(|k| low <= *k && *k <= high)
        .collect())
}

/// `F = K + (C_mid - P_mid) * e^{rT}` from the ATM pair (`04` §2.2), nearest strike first.
pub fn parity_forward(
    quotes: &[OptionQuote],
    spot: Decimal,
    step: Decimal,
    years: f64,
    rate: f64,
) -> CoreResult<Option<Decimal>> {
    let expiries: BTreeSet<NaiveDate> = quotes.iter().map(|q| q.expiry).collect();
    if expiries.len() > 1 {
        return Err(CoreError::InvalidInput(
            "parity_forward takes one expiry's quotes".to_owned(),
        ));
    }
    let mut calls: BTreeMap<Decimal, Option<Decimal>> = BTreeMap::new();
    let mut puts: BTreeMap<Decimal, Option<Decimal>> = BTreeMap::new();
    for q in quotes {
        let target = match q.option_type {
            OptionType::Ce => &mut calls,
            OptionType::Pe => &mut puts,
        };
        target.insert(q.strike, q.mid()?);
    }
    let centre = atm(spot, step)?;
    let growth = num::exp_of_product(rate, years)?;
    let mut both: Vec<(Decimal, Decimal)> = Vec::new();
    for k in calls.keys() {
        if puts.contains_key(k) {
            both.push((num::abs(num::sub(*k, centre)?)?, *k));
        }
    }
    both.sort();
    for (_, strike) in both {
        if let (Some(Some(c)), Some(Some(p))) = (calls.get(&strike), puts.get(&strike)) {
            let diff = WideDecimal::from(*c).sub(&WideDecimal::from(*p));
            let forward = WideDecimal::from(strike).add(&diff.mul(&WideDecimal::from(growth)));
            return forward
                .try_to_decimal()
                .map(Some)
                .map_err(|e| CoreError::Arithmetic(e.to_string()));
        }
    }
    Ok(None)
}

/// IV and greeks for `quote` on `forward` at `now`, or `None` where `04` §2.3 refuses.
pub fn quote_greeks(
    quote: &OptionQuote,
    forward: Decimal,
    now: &DateTime<FixedOffset>,
    tick: Decimal,
    config: &ChainConfig,
    settle: NaiveTime,
) -> CoreResult<Option<Greeks>> {
    let Some(mid) = quote.mid()? else {
        return Ok(None);
    };
    solve(
        num::to_float(mid),
        num::to_float(forward),
        num::to_float(quote.strike),
        year_fraction(now, quote.expiry, settle),
        config.rate,
        quote.option_type,
        SolveLimits {
            min_premium: num::to_float(config.min_premium),
            tick: num::to_float(tick),
            bracket: Bracket {
                lower: config.iv_lower,
                upper: config.iv_upper,
                tolerance: config.iv_tolerance,
            },
        },
    )
}

/// Why a contract failed `is_liquid` — every reason, not only the first.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Illiquid {
    /// No two-sided quote.
    NoQuote,
    /// Spread beyond `max_spread_pct`.
    SpreadTooWide,
    /// Not enough depth on the side taken.
    DepthTooThin,
    /// Open interest too low.
    OiTooLow,
}

/// `is_liquid`'s answer.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Liquidity {
    /// Every test passed.
    pub ok: bool,
    /// Every failed test.
    pub reasons: Vec<Illiquid>,
}

/// Units resting on the side an order takes, within the first `levels` levels.
pub fn depth_available(quote: &OptionQuote, side: Side, levels: i64) -> i64 {
    let ladder = match side {
        Side::Buy => &quote.asks,
        Side::Sell => &quote.bids,
    };
    let n = usize::try_from(levels.max(0)).unwrap_or(0);
    ladder
        .iter()
        .take(n)
        .filter(|l| l.quantity > 0)
        .map(|l| l.quantity)
        .sum()
}

/// Kite's `oi` in units whatever it counts.
pub fn oi_in_units(quote: &OptionQuote, lot_size: i64, unit: OiUnit) -> i64 {
    match unit {
        OiUnit::Lots => quote.oi * lot_size,
        OiUnit::Units => quote.oi,
    }
}

/// `04` §2.4: spread, depth on the side taken, and OI — or, for an O1 wing, depth only.
pub fn is_liquid(
    quote: &OptionQuote,
    qty: i64,
    side: Side,
    lot_size: i64,
    config: &ChainConfig,
    wing: bool,
) -> CoreResult<Liquidity> {
    let mut reasons = Vec::new();
    match quote.spread_pct()? {
        None => reasons.push(Illiquid::NoQuote),
        Some(spread) if !wing && spread > config.max_spread_pct => {
            reasons.push(Illiquid::SpreadTooWide);
        }
        Some(_) => {}
    }
    if depth_available(quote, side, config.depth_levels) < qty {
        reasons.push(Illiquid::DepthTooThin);
    }
    if !wing && oi_in_units(quote, lot_size, config.oi_unit) < config.min_oi_lots * lot_size {
        reasons.push(Illiquid::OiTooLow);
    }
    Ok(Liquidity {
        ok: reasons.is_empty(),
        reasons,
    })
}

/// Older than `stale_quote_seconds` (`04` §2.5).
pub fn is_stale_quote(
    quote_ts: &DateTime<FixedOffset>,
    now: &DateTime<FixedOffset>,
    config: &ChainConfig,
) -> bool {
    seconds_between(now, quote_ts) > as_f64(config.stale_quote_seconds)
}

/// No index tick for `stale_index_seconds`, or none at all (`04` §2.5).
pub fn is_stale_index(
    last_tick: Option<&DateTime<FixedOffset>>,
    now: &DateTime<FixedOffset>,
    config: &ChainConfig,
) -> bool {
    match last_tick {
        None => true,
        Some(t) => seconds_between(now, t) > as_f64(config.stale_index_seconds),
    }
}

/// A scan from a snapshot older than `stale_scan_seconds` is stale (`04` §2.5).
pub fn is_stale_scan(
    snapshot_ts: &DateTime<FixedOffset>,
    now: &DateTime<FixedOffset>,
    config: &ChainConfig,
) -> bool {
    seconds_between(now, snapshot_ts) > as_f64(config.stale_scan_seconds)
}

#[allow(clippy::cast_precision_loss)]
pub(crate) fn as_f64(n: i64) -> f64 {
    n as f64
}
