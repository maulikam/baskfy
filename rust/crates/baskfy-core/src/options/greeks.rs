//! Port of `baskfy_core.options.greeks`: Black-76 on the put-call-parity forward (`04` §2.3).

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime};
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::fno::vol::greeks::erf;
use crate::options::config::OptionType;
use crate::options::num::{combine, seconds_between};

const MINUTES_PER_YEAR: f64 = 365.0 * 24.0 * 60.0;
const DAYS_PER_YEAR: f64 = 365.0;
const VOL_POINT: f64 = 100.0;

/// `T` in calendar years from `now` to `settle` on `expiry`; the settlement takes `now`'s offset.
pub fn year_fraction(now: &DateTime<FixedOffset>, expiry: NaiveDate, settle: NaiveTime) -> f64 {
    let close = combine(expiry, settle, *now.offset());
    seconds_between(&close, now) / 60.0 / MINUTES_PER_YEAR
}

fn norm_cdf(x: f64) -> f64 {
    0.5 * (1.0 + erf(x / 2.0_f64.sqrt()))
}

fn norm_pdf(x: f64) -> f64 {
    (-0.5 * x * x).exp() / (2.0 * std::f64::consts::PI).sqrt()
}

fn d1_d2(forward: f64, strike: f64, years: f64, vol: f64) -> (f64, f64) {
    let root = vol * years.sqrt();
    let d1 = ((forward / strike).ln() + 0.5 * vol * vol * years) / root;
    (d1, d1 - root)
}

fn valid(forward: f64, strike: f64, years: f64, vol: f64) -> bool {
    forward > 0.0 && strike > 0.0 && years > 0.0 && vol > 0.0
}

/// The Black-76 premium; the discounted intrinsic when the inputs are degenerate.
pub fn black76_price(
    forward: f64,
    strike: f64,
    years: f64,
    rate: f64,
    vol: f64,
    kind: OptionType,
) -> f64 {
    let discount = (-rate * years.max(0.0)).exp();
    if !valid(forward, strike, years, vol) {
        let intrinsic = match kind {
            OptionType::Ce => forward - strike,
            OptionType::Pe => strike - forward,
        };
        return discount * intrinsic.max(0.0);
    }
    let (d1, d2) = d1_d2(forward, strike, years, vol);
    match kind {
        OptionType::Ce => discount * (forward * norm_cdf(d1) - strike * norm_cdf(d2)),
        OptionType::Pe => discount * (strike * norm_cdf(-d2) - forward * norm_cdf(-d1)),
    }
}

/// Intrinsic value on the forward, undiscounted.
pub fn intrinsic(forward: f64, strike: f64, kind: OptionType) -> f64 {
    match kind {
        OptionType::Ce => (forward - strike).max(0.0),
        OptionType::Pe => (strike - forward).max(0.0),
    }
}

/// The bisection's bounds and tolerance.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Bracket {
    /// The lowest vol.
    pub lower: f64,
    /// The highest vol.
    pub upper: f64,
    /// Stop once the bracket is this narrow.
    pub tolerance: f64,
}

/// The Black-76 vol that reprices `price`, by bisection; `None` when not bracketed or degenerate.
pub fn implied_vol(
    price: f64,
    forward: f64,
    strike: f64,
    years: f64,
    rate: f64,
    kind: OptionType,
    bracket: Bracket,
) -> Option<f64> {
    if price <= 0.0 || years <= 0.0 || forward <= 0.0 || strike <= 0.0 {
        return None;
    }
    let low_price = black76_price(forward, strike, years, rate, bracket.lower, kind);
    let high_price = black76_price(forward, strike, years, rate, bracket.upper, kind);
    if price < low_price || price > high_price {
        return None;
    }
    let (mut lo, mut hi) = (bracket.lower, bracket.upper);
    while hi - lo > bracket.tolerance {
        let mid = (lo + hi) / 2.0;
        if black76_price(forward, strike, years, rate, mid, kind) < price {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    Some((lo + hi) / 2.0)
}

/// Black-76 on `F`: delta, gamma, theta per calendar day, vega per vol point.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Greeks {
    /// The vol.
    pub iv: f64,
    /// Delta per point of `F`.
    pub delta: f64,
    /// Gamma.
    pub gamma: f64,
    /// Theta per calendar day.
    pub theta: f64,
    /// Vega per vol point.
    pub vega: f64,
}

/// Analytic greeks at `vol`; an error on degenerate inputs.
pub fn black76_greeks(
    forward: f64,
    strike: f64,
    years: f64,
    rate: f64,
    vol: f64,
    kind: OptionType,
) -> CoreResult<Greeks> {
    if !valid(forward, strike, years, vol) {
        return Err(CoreError::InvalidInput(
            "greeks need a positive forward, strike, time and vol".to_owned(),
        ));
    }
    let discount = (-rate * years).exp();
    let (d1, _) = d1_d2(forward, strike, years, vol);
    let density = norm_pdf(d1);
    let root_t = years.sqrt();
    let price = black76_price(forward, strike, years, rate, vol, kind);
    let delta = match kind {
        OptionType::Ce => discount * norm_cdf(d1),
        OptionType::Pe => -discount * norm_cdf(-d1),
    };
    let gamma = discount * density / (forward * vol * root_t);
    let decay = discount * forward * density * vol / (2.0 * root_t);
    let theta_year = rate * price - decay;
    let vega = discount * forward * density * root_t / VOL_POINT;
    Ok(Greeks {
        iv: vol,
        delta,
        gamma,
        theta: theta_year / DAYS_PER_YEAR,
        vega,
    })
}

/// The quote-level refusals and the solver's bracket.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct SolveLimits {
    /// Smallest premium solved for.
    pub min_premium: f64,
    /// The contract's tick.
    pub tick: f64,
    /// The bisection bracket.
    pub bracket: Bracket,
}

/// IV and greeks for one quote, or `None` where `04` §2.3 refuses.
pub fn solve(
    mid: f64,
    forward: f64,
    strike: f64,
    years: f64,
    rate: f64,
    kind: OptionType,
    limits: SolveLimits,
) -> CoreResult<Option<Greeks>> {
    if years <= 0.0 || mid < limits.min_premium {
        return Ok(None);
    }
    if mid < intrinsic(forward, strike, kind) + limits.tick {
        return Ok(None);
    }
    match implied_vol(mid, forward, strike, years, rate, kind, limits.bracket) {
        None => Ok(None),
        Some(vol) => black76_greeks(forward, strike, years, rate, vol, kind).map(Some),
    }
}
