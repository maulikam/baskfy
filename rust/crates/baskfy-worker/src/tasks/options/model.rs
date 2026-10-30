//! The pieces of `baskfy_core.options` this lane's tasks need and the Rust core does not carry
//! yet: the master's `Contract` and calendar (`calendar.expiries`/`kind`/`role`), the chain rules
//! the collector applies (`chain.atm`/`strike_step`/`snapshot_strikes`/`parity_forward`/
//! `quote_greeks`), Black-76's greeks (`greeks.year_fraction`/`black76_greeks`/`solve`), the
//! journal summary the weekly report prints (`journal.summarize`) and the config values they
//! read (`config.OptionsConfig`'s defaults).
//!
//! **Why here and not in `baskfy-core`** (DECISIONS-RS R8.op1): these modules have no goldens,
//! and a core module is never ported blind (R8.sb1's rule). They are pure — no clock, no I/O —
//! and are graded end to end, byte for byte, by `task_diff.py` through the rows and payloads the
//! tasks write. Moving them into `baskfy_core::options` is the options core's wave, goldens
//! first.
//!
//! What *is* reused from the core: `options::gating::Sleeve`/`group_of`, and the Black-76
//! premium, the bisection and `erf` that `fno::vol::greeks` already carries for the same Python
//! functions (`baskfy_core.options.greeks.black76_price`/`implied_vol`).
//!
//! Decimal rules reproduced: every `Decimal` operation runs in Python's default context (28
//! significant digits, half even) through [`WideDecimal`]; `Decimal.exp()` is correctly rounded
//! to 28 digits ([`exp28`]); `float(Decimal)` is the correctly rounded parse of `str(d)`.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_core::fno::vol::OptionType;
use baskfy_core::fno::vol::greeks::{Solver, black76_price, erf, implied_vol};
use baskfy_core::options::gating::{Sleeve, SleeveGroup, group_of};
use chrono::{DateTime, Datelike, FixedOffset, NaiveDate, NaiveTime};
use rust_decimal::Decimal;

use crate::error::WorkerError;

// ---------------------------------------------------------------------------------------------
// Config (`OptionsConfig()`'s defaults)
// ---------------------------------------------------------------------------------------------

/// `CalendarConfig.underlying`.
pub(crate) const UNDERLYING: &str = "NIFTY";

/// `CalendarConfig.market_open`.
pub(crate) fn market_open() -> NaiveTime {
    hm(9, 15)
}

/// `CalendarConfig.market_close` — also the settlement moment of `T`.
pub(crate) fn market_close() -> NaiveTime {
    hm(15, 30)
}

/// A wall-clock time (constant inputs; never out of range).
pub(crate) fn hm(hour: u32, minute: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(hour, minute, 0).unwrap_or(NaiveTime::MIN)
}

/// `ChainConfig.snapshot_strikes`.
pub(crate) const SNAPSHOT_STRIKES: usize = 15;
/// `ChainConfig.rate`.
pub(crate) const RATE: f64 = 0.065;
/// `ChainConfig.iv_lower` / `iv_upper` / `iv_tolerance`.
pub(crate) const SOLVER: Solver = Solver {
    lower: 0.01,
    upper: 5.0,
    tolerance: 1e-6,
};
/// `float(ChainConfig.min_premium)` (`Decimal("0.50")`).
pub(crate) const MIN_PREMIUM: f64 = 0.5;
/// `ChainConfig.greeks_model`.
pub(crate) const GREEKS_MODEL: &str = "black76-parity-v1";

/// `OptionsConfig().hard_exit_time(sleeve)`: O1-M and O1-W 14:30, O2 15:00, O3 14:45.
pub(crate) fn hard_exit_time(sleeve: Sleeve) -> NaiveTime {
    match group_of(sleeve) {
        SleeveGroup::O1m | SleeveGroup::O1w => hm(14, 30),
        SleeveGroup::O2 => hm(15, 0),
        SleeveGroup::O3 => hm(14, 45),
    }
}

/// `Sleeve` in declaration order (`for s in Sleeve`).
pub(crate) const SLEEVES: [Sleeve; 5] = [
    Sleeve::O1m,
    Sleeve::O1w,
    Sleeve::O2,
    Sleeve::O3a,
    Sleeve::O3b,
];

/// `sleeve.value`.
pub(crate) const fn sleeve_value(sleeve: Sleeve) -> &'static str {
    match sleeve {
        Sleeve::O1m => "O1M",
        Sleeve::O1w => "O1W",
        Sleeve::O2 => "O2",
        Sleeve::O3a => "O3A",
        Sleeve::O3b => "O3B",
    }
}

/// `Sleeve(value)`.
pub(crate) fn sleeve_of(value: &str) -> Result<Sleeve, WorkerError> {
    SLEEVES
        .into_iter()
        .find(|s| sleeve_value(*s) == value)
        .ok_or_else(|| {
            WorkerError::python(
                "ValueError",
                format!(
                    "{} is not a valid Sleeve",
                    baskfy_providers::py::repr_str(value)
                ),
            )
        })
}

/// `group_of(sleeve).value`.
pub(crate) fn group_value(sleeve: Sleeve) -> &'static str {
    match group_of(sleeve) {
        SleeveGroup::O1m => "O1M",
        SleeveGroup::O1w => "O1W",
        SleeveGroup::O2 => "O2",
        SleeveGroup::O3 => "O3",
    }
}

/// `OptionType.<X>.value`.
pub(crate) const fn option_type_value(kind: OptionType) -> &'static str {
    match kind {
        OptionType::Ce => "CE",
        OptionType::Pe => "PE",
    }
}

// ---------------------------------------------------------------------------------------------
// The calendar (`baskfy_core.options.calendar`)
// ---------------------------------------------------------------------------------------------

/// One row of `op_contract` — the master's facts (`calendar.Contract`).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Contract {
    pub(crate) instrument_token: i64,
    pub(crate) tradingsymbol: String,
    pub(crate) underlying: String,
    pub(crate) expiry: NaiveDate,
    pub(crate) strike: Decimal,
    pub(crate) option_type: OptionType,
    pub(crate) lot_size: i64,
    pub(crate) tick_size: Decimal,
}

/// `ExpiryKind`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ExpiryKind {
    Weekly,
    Monthly,
}

impl ExpiryKind {
    /// The stable string.
    pub(crate) const fn value(self) -> &'static str {
        match self {
            Self::Weekly => "WEEKLY",
            Self::Monthly => "MONTHLY",
        }
    }
}

/// `calendar.expiries(rows, underlying)`: the distinct expiries, ascending.
pub(crate) fn expiries(rows: &[Contract], underlying: &str) -> Vec<NaiveDate> {
    rows.iter()
        .filter(|r| r.underlying == underlying)
        .map(|r| r.expiry)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

/// `calendar.kind(rows, expiry, underlying)` over an already computed expiry list:
/// `MONTHLY` iff `expiry` is its month's last master expiry.
///
/// # Errors
/// `ValueError` when `expiry` is not listed.
pub(crate) fn kind_in(
    listed: &[NaiveDate],
    expiry: NaiveDate,
    underlying: &str,
) -> Result<ExpiryKind, WorkerError> {
    if !listed.contains(&expiry) {
        return Err(WorkerError::python(
            "ValueError",
            format!(
                "{} is not a {underlying} expiry in the master",
                expiry.format("%Y-%m-%d")
            ),
        ));
    }
    let last_of_month = listed
        .iter()
        .filter(|d| d.year() == expiry.year() && d.month() == expiry.month())
        .max()
        .copied();
    Ok(if last_of_month == Some(expiry) {
        ExpiryKind::Monthly
    } else {
        ExpiryKind::Weekly
    })
}

/// `calendar.role(day, sleeve, rows=..., event_days=..., trading_day=True).trades`.
///
/// # Errors
/// As [`kind_in`] (unreachable: the day is checked to be listed first).
pub(crate) fn trades_on(
    day: NaiveDate,
    sleeve: Sleeve,
    rows: &[Contract],
    event_days: &[NaiveDate],
    trading_day: bool,
) -> Result<bool, WorkerError> {
    if !trading_day || event_days.contains(&day) {
        return Ok(false);
    }
    if sleeve == Sleeve::O2 {
        return Ok(true);
    }
    let listed = expiries(rows, UNDERLYING);
    if !listed.contains(&day) {
        return Ok(false);
    }
    let day_kind = kind_in(&listed, day, UNDERLYING)?;
    if sleeve == Sleeve::O1m && day_kind != ExpiryKind::Monthly {
        return Ok(false);
    }
    if sleeve == Sleeve::O1w && day_kind == ExpiryKind::Monthly {
        return Ok(false);
    }
    Ok(true)
}

// ---------------------------------------------------------------------------------------------
// The chain (`baskfy_core.options.chain`)
// ---------------------------------------------------------------------------------------------

/// One contract's quote at one instant (`chain.OptionQuote`), the fields the collector reads.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct OptionQuote {
    pub(crate) expiry: NaiveDate,
    pub(crate) strike: Decimal,
    pub(crate) option_type: OptionType,
    pub(crate) bid: Option<Decimal>,
    pub(crate) ask: Option<Decimal>,
}

impl OptionQuote {
    /// `mid`: `(bid + ask) / 2`, `None` unless both sides are quoted and positive.
    pub(crate) fn mid(&self) -> Option<WideDecimal> {
        let (bid, ask) = (self.bid?, self.ask?);
        if bid <= Decimal::ZERO || ask <= Decimal::ZERO {
            return None;
        }
        WideDecimal::from(bid)
            .add(&WideDecimal::from(ask))
            .div(&WideDecimal::from(2_i64))
            .ok()
    }
}

/// `chain.atm(spot, step)`: the strike nearest `spot` on a `step` grid; a tie goes lower.
///
/// # Errors
/// `ValueError` for a step that is not positive.
pub(crate) fn atm(spot: Decimal, step: Decimal) -> Result<Decimal, WorkerError> {
    if step <= Decimal::ZERO {
        return Err(WorkerError::python(
            "ValueError",
            "strike step must be positive",
        ));
    }
    let below = (spot / step).trunc() * step;
    let above = below + step;
    Ok(if spot - below <= above - spot {
        below
    } else {
        above
    })
}

/// `chain.strike_step(strikes, near, window)`: the modal adjacent gap among the `2 * window + 1`
/// listed strikes nearest `near` (a tie goes to the smaller gap); `None` below two strikes.
pub(crate) fn strike_step(
    strikes: impl IntoIterator<Item = Decimal>,
    near: Decimal,
    window: usize,
) -> Option<Decimal> {
    let distinct: Vec<Decimal> = strikes
        .into_iter()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    if distinct.len() < 2 {
        return None;
    }
    let mut nearest = distinct;
    nearest.sort_by_key(|k| ((*k - near).abs(), *k));
    nearest.truncate(2 * window + 1);
    nearest.sort();
    let mut gaps: BTreeMap<Decimal, usize> = BTreeMap::new();
    for pair in nearest.windows(2) {
        if let [a, b] = pair {
            *gaps.entry(*b - *a).or_insert(0) += 1;
        }
    }
    let top = gaps.values().copied().max()?;
    gaps.into_iter()
        .filter(|(_, n)| *n == top)
        .map(|(gap, _)| gap)
        .min()
}

/// `chain.snapshot_strikes(listed, spot, step, count)`: the listed strikes within `count` steps
/// of ATM, ascending.
///
/// # Errors
/// As [`atm`].
pub(crate) fn snapshot_strikes(
    listed: impl IntoIterator<Item = Decimal>,
    spot: Decimal,
    step: Decimal,
    count: usize,
) -> Result<Vec<Decimal>, WorkerError> {
    let centre = atm(spot, step)?;
    let width = step * Decimal::from(count);
    let (low, high) = (centre - width, centre + width);
    Ok(listed
        .into_iter()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .filter(|k| low <= *k && *k <= high)
        .collect())
}

/// `chain.parity_forward(quotes, spot, step, years, rate)`: `F = K + (C_mid - P_mid) * e^{rT}`
/// from the ATM pair, falling back pair by pair nearest ATM first; `None` if no strike has both
/// mids. The quotes are one expiry's (the caller groups them).
///
/// # Errors
/// As [`atm`]; a `Decimal.exp` argument the port does not handle.
pub(crate) fn parity_forward(
    quotes: &[&OptionQuote],
    spot: Decimal,
    step: Decimal,
    years: f64,
    rate: f64,
) -> Result<Option<WideDecimal>, WorkerError> {
    // `{q.strike: q.mid ...}`: a later quote at the same strike replaces the value, the key
    // keeps its first spelling.
    let mut calls: Vec<(Decimal, Option<WideDecimal>)> = Vec::new();
    let mut puts: Vec<(Decimal, Option<WideDecimal>)> = Vec::new();
    for q in quotes {
        let book = if q.option_type == OptionType::Ce {
            &mut calls
        } else {
            &mut puts
        };
        let mid = q.mid();
        if let Some(slot) = book.iter_mut().find(|(k, _)| *k == q.strike) {
            slot.1 = mid;
        } else {
            book.push((q.strike, mid));
        }
    }
    let centre = atm(spot, step)?;
    let growth = exp28(&rate_times_years(rate, years)?)?;
    let mut both: Vec<Decimal> = calls
        .iter()
        .map(|(k, _)| *k)
        .filter(|k| puts.iter().any(|(p, _)| p == k))
        .collect();
    both.sort_by_key(|k| ((*k - centre).abs(), *k));
    for strike in both {
        let call = calls
            .iter()
            .find(|(k, _)| *k == strike)
            .and_then(|(_, m)| m.clone());
        let put = puts
            .iter()
            .find(|(k, _)| *k == strike)
            .and_then(|(_, m)| m.clone());
        if let (Some(c), Some(p)) = (call, put) {
            return Ok(Some(WideDecimal::from(strike).add(&c.sub(&p).mul(&growth))));
        }
    }
    Ok(None)
}

/// `Decimal(repr(rate)) * Decimal(repr(years))` — exact: two short coefficients.
fn rate_times_years(rate: f64, years: f64) -> Result<WideDecimal, WorkerError> {
    let parse = |x: f64| -> Result<WideDecimal, WorkerError> {
        baskfy_core::py::float_repr(x)
            .parse::<WideDecimal>()
            .map_err(|e| WorkerError::python("InvalidOperation", e.to_string()))
    };
    Ok(parse(rate)?.mul(&parse(years)?))
}

/// `float(d)`: the correctly rounded parse of `str(d)`.
pub(crate) fn to_f64(d: &WideDecimal) -> f64 {
    d.to_sci_string().parse::<f64>().unwrap_or(f64::NAN)
}

/// `float(d)` for a `rust_decimal`.
pub(crate) fn dec_f64(d: Decimal) -> f64 {
    to_f64(&WideDecimal::from(d))
}

// ---------------------------------------------------------------------------------------------
// `Decimal.exp()` — correctly rounded to 28 digits, half even
// ---------------------------------------------------------------------------------------------

/// A small unsigned fixed-point big integer (base 10^9 limbs, little-endian) for [`exp28`].
#[derive(Clone, Debug, PartialEq, Eq)]
struct Fixed {
    limbs: Vec<u64>,
}

const LIMB: u64 = 1_000_000_000;

impl Fixed {
    /// `10^power`.
    fn pow10(power: usize) -> Self {
        let mut limbs = vec![0_u64; power / 9];
        limbs.push(10_u64.pow(u32::try_from(power % 9).unwrap_or(0)));
        Self { limbs }
    }

    fn is_zero(&self) -> bool {
        self.limbs.iter().all(|l| *l == 0)
    }

    fn mul_small(&self, factor: u128) -> Self {
        let mut out = Vec::with_capacity(self.limbs.len() + 3);
        let mut carry: u128 = 0;
        for limb in &self.limbs {
            let v = u128::from(*limb) * factor + carry;
            out.push(u64::try_from(v % u128::from(LIMB)).unwrap_or(0));
            carry = v / u128::from(LIMB);
        }
        while carry > 0 {
            out.push(u64::try_from(carry % u128::from(LIMB)).unwrap_or(0));
            carry /= u128::from(LIMB);
        }
        Self { limbs: out }
    }

    /// Truncating division by a divisor below `2^64`.
    fn div_small(&self, divisor: u64) -> Self {
        let mut out = vec![0_u64; self.limbs.len()];
        let mut rem: u128 = 0;
        for (i, limb) in self.limbs.iter().enumerate().rev() {
            let cur = rem * u128::from(LIMB) + u128::from(*limb);
            if let Some(slot) = out.get_mut(i) {
                *slot = u64::try_from(cur / u128::from(divisor)).unwrap_or(0);
            }
            rem = cur % u128::from(divisor);
        }
        Self { limbs: out }
    }

    /// Truncating division by `10^power`.
    fn div_pow10(&self, power: usize) -> Self {
        let mut out = self.clone();
        let mut left = power;
        while left > 0 {
            let step = left.min(9);
            out = out.div_small(10_u64.pow(u32::try_from(step).unwrap_or(0)));
            left -= step;
        }
        out
    }

    fn add(&self, other: &Self) -> Self {
        let n = self.limbs.len().max(other.limbs.len());
        let mut out = Vec::with_capacity(n + 1);
        let mut carry = 0_u64;
        for i in 0..n {
            let v = self.limbs.get(i).copied().unwrap_or(0)
                + other.limbs.get(i).copied().unwrap_or(0)
                + carry;
            out.push(v % LIMB);
            carry = v / LIMB;
        }
        if carry > 0 {
            out.push(carry);
        }
        Self { limbs: out }
    }

    fn digits(&self) -> String {
        let mut limbs = self.limbs.clone();
        while limbs.len() > 1 && limbs.last() == Some(&0) {
            limbs.pop();
        }
        let mut out = String::new();
        for (i, limb) in limbs.iter().rev().enumerate() {
            if i == 0 {
                out.push_str(&limb.to_string());
            } else {
                out.push_str(&format!("{limb:09}"));
            }
        }
        out
    }
}

/// Fixed-point digits carried beyond the result's 28 (the Taylor sum truncates at ~1 unit in
/// the last of these per term; a result within that of a rounding boundary is the only way to
/// mis-round, ~10^-50 relative).
const GUARD_DIGITS: usize = 80;

/// `x.exp()` in Python's default context: correctly rounded to 28 significant digits, half
/// even (`Decimal.exp` is documented so). `x` must be non-negative and below 1 — the forward's
/// `rT` always is (a positive rate times a positive `T` under a year); anything else is refused
/// rather than approximated.
///
/// # Errors
/// `InvalidOperation` for an argument outside `[0, 1)`.
pub(crate) fn exp28(x: &WideDecimal) -> Result<WideDecimal, WorkerError> {
    if x.is_zero() {
        return Ok(WideDecimal::from(1_i64));
    }
    let refuse = || {
        WorkerError::python(
            "InvalidOperation",
            format!(
                "Decimal.exp of {} is outside the port's [0, 1)",
                x.to_sci_string()
            ),
        )
    };
    if x.is_sign_negative() || x >= &WideDecimal::from(1_i64) {
        return Err(refuse());
    }
    // x = coefficient * 10^-places (two short coefficients' product: 65 times a 17-digit repr, below 2^64).
    let places = usize::try_from(-x.exponent()).map_err(|_| refuse())?;
    let coeff_text: String = x.to_string().chars().filter(char::is_ascii_digit).collect();
    let coefficient: u128 = coeff_text
        .trim_start_matches('0')
        .parse()
        .map_err(|_| refuse())?;
    if coefficient >= 1 << 64 {
        return Err(refuse());
    }
    // sum_k x^k / k!, each term truncated at 10^-GUARD_DIGITS.
    let mut term = Fixed::pow10(GUARD_DIGITS);
    let mut total = term.clone();
    let mut k: u64 = 1;
    loop {
        term = term.mul_small(coefficient).div_pow10(places).div_small(k);
        if term.is_zero() {
            break;
        }
        total = total.add(&term);
        k += 1;
    }
    // total ≈ e^x · 10^GUARD_DIGITS; round to 28 significant digits, half even.
    let digits = total.digits();
    let len = digits.len();
    let exp = i64::try_from(len).map_err(|_| refuse())?
        - 28
        - i64::try_from(GUARD_DIGITS).map_err(|_| refuse())?;
    let exact: WideDecimal = format!(
        "{}E{}",
        digits,
        -i64::try_from(GUARD_DIGITS).map_err(|_| refuse())?
    )
    .parse()
    .map_err(|_| refuse())?;
    let rounded = exact.rescale_exact(exp, Rounding::HalfEven);
    if rounded.coefficient_len() > 28 {
        return Ok(exact.rescale_exact(exp + 1, Rounding::HalfEven));
    }
    Ok(rounded)
}

// ---------------------------------------------------------------------------------------------
// Black-76 (`baskfy_core.options.greeks`)
// ---------------------------------------------------------------------------------------------

const MINUTES_PER_YEAR: f64 = 365.0 * 24.0 * 60.0;

/// `greeks.year_fraction(now, expiry, settle)`: calendar years from `now` to `settle` on
/// `expiry`, the settlement moment in `now`'s own zone.
pub(crate) fn year_fraction(
    now: DateTime<FixedOffset>,
    expiry: NaiveDate,
    settle: NaiveTime,
) -> f64 {
    let close = expiry.and_time(settle) - now.naive_local();
    let micros = close.num_microseconds().unwrap_or(i64::MAX);
    // `timedelta.total_seconds()`: microseconds / 10**6, correctly rounded.
    #[allow(clippy::cast_precision_loss)]
    let seconds = micros as f64 / 1e6;
    seconds / 60.0 / MINUTES_PER_YEAR
}

/// `greeks.Greeks`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) struct Greeks {
    pub(crate) iv: f64,
    pub(crate) delta: f64,
    pub(crate) gamma: f64,
    pub(crate) theta: f64,
    pub(crate) vega: f64,
}

fn norm_cdf(x: f64) -> f64 {
    0.5 * (1.0 + erf(x / 2.0_f64.sqrt()))
}

fn norm_pdf(x: f64) -> f64 {
    (-0.5 * x * x).exp() / (2.0 * std::f64::consts::PI).sqrt()
}

/// `greeks.black76_greeks` at `vol` (the caller has refused degenerate inputs).
fn black76_greeks(
    forward: f64,
    strike: f64,
    years: f64,
    rate: f64,
    vol: f64,
    kind: OptionType,
) -> Greeks {
    let discount = (-rate * years).exp();
    let root = vol * years.sqrt();
    let d1 = ((forward / strike).ln() + 0.5 * vol * vol * years) / root;
    let density = norm_pdf(d1);
    let root_t = years.sqrt();
    let price = black76_price(forward, strike, years, rate, vol, kind);
    let delta = if kind == OptionType::Ce {
        discount * norm_cdf(d1)
    } else {
        -discount * norm_cdf(-d1)
    };
    let gamma = discount * density / (forward * vol * root_t);
    let decay = discount * forward * density * vol / (2.0 * root_t);
    let theta_year = rate * price - decay;
    let vega = discount * forward * density * root_t / 100.0;
    Greeks {
        iv: vol,
        delta,
        gamma,
        theta: theta_year / 365.0,
        vega,
    }
}

/// `greeks.intrinsic`: on the forward, undiscounted (Python's `max(x, 0.0)` keeps `x` unless
/// `0.0` is greater).
fn intrinsic(forward: f64, strike: f64, kind: OptionType) -> f64 {
    let x = if kind == OptionType::Ce {
        forward - strike
    } else {
        strike - forward
    };
    if 0.0 > x { 0.0 } else { x }
}

/// `greeks.solve`: IV and greeks for one quote, or `None` where `04` §2.3 refuses.
#[allow(clippy::too_many_arguments)]
pub(crate) fn solve(
    mid: f64,
    forward: f64,
    strike: f64,
    years: f64,
    rate: f64,
    kind: OptionType,
    tick: f64,
) -> Option<Greeks> {
    if years <= 0.0 || mid < MIN_PREMIUM {
        return None;
    }
    if mid < intrinsic(forward, strike, kind) + tick {
        return None;
    }
    let vol = implied_vol(mid, forward, strike, years, rate, kind, SOLVER)?;
    let valid = forward > 0.0 && strike > 0.0 && years > 0.0 && vol > 0.0;
    valid.then(|| black76_greeks(forward, strike, years, rate, vol, kind))
}

/// `chain.quote_greeks(quote, forward, now=..., tick=..., config=..., settle=...)`.
pub(crate) fn quote_greeks(
    quote: &OptionQuote,
    forward: &WideDecimal,
    now: DateTime<FixedOffset>,
    tick: Decimal,
) -> Option<Greeks> {
    let mid = quote.mid()?;
    solve(
        to_f64(&mid),
        to_f64(forward),
        dec_f64(quote.strike),
        year_fraction(now, quote.expiry, market_close()),
        RATE,
        quote.option_type,
        dec_f64(tick),
    )
}

// ---------------------------------------------------------------------------------------------
// The journal (`baskfy_core.options.journal`) — what the weekly report reads
// ---------------------------------------------------------------------------------------------

/// `SizingMode`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) enum SizingMode {
    Budget,
    PaperOneLot,
}

impl SizingMode {
    /// The stable string.
    pub(crate) const fn value(self) -> &'static str {
        match self {
            Self::Budget => "BUDGET",
            Self::PaperOneLot => "PAPER_ONE_LOT",
        }
    }

    /// `SizingMode(value)`.
    pub(crate) fn of(value: &str) -> Result<Self, WorkerError> {
        match value {
            "BUDGET" => Ok(Self::Budget),
            "PAPER_ONE_LOT" => Ok(Self::PaperOneLot),
            other => Err(WorkerError::python(
                "ValueError",
                format!(
                    "{} is not a valid SizingMode",
                    baskfy_providers::py::repr_str(other)
                ),
            )),
        }
    }
}

/// `journal.JournalRow` (the fields `summarize` reads for the weekly lines).
#[derive(Debug, Clone)]
pub(crate) struct JournalRow {
    pub(crate) sleeve: Sleeve,
    pub(crate) trade_date: NaiveDate,
    pub(crate) simulated: bool,
    pub(crate) sizing_mode: SizingMode,
    pub(crate) traded: bool,
    pub(crate) skip_reason: Option<String>,
    pub(crate) net_pnl_inr: WideDecimal,
    pub(crate) r_inr: WideDecimal,
}

impl JournalRow {
    /// `row.r`: `net_pnl_inr / R`, R positive.
    fn r(&self) -> Result<WideDecimal, WorkerError> {
        if self.r_inr <= WideDecimal::zero() {
            return Err(WorkerError::python("ValueError", "R must be positive"));
        }
        self.net_pnl_inr
            .div(&self.r_inr)
            .map_err(|e| WorkerError::python("InvalidOperation", e.to_string()))
    }
}

/// `journal.PoolKey`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct PoolKey {
    pub(crate) sleeve: Sleeve,
    pub(crate) simulated: bool,
    pub(crate) sizing_mode: SizingMode,
}

/// `journal.Summary` — the fields the weekly report prints.
#[derive(Debug, Clone)]
pub(crate) struct Summary {
    pub(crate) key: PoolKey,
    pub(crate) traded: usize,
    pub(crate) skipped_by_reason: Vec<(String, i64)>,
    pub(crate) win_rate: Option<WideDecimal>,
    pub(crate) mean_r: Option<WideDecimal>,
    pub(crate) worst_r: Option<WideDecimal>,
    pub(crate) max_drawdown_r: WideDecimal,
}

/// `journal.max_drawdown`: the largest peak-to-trough fall of the running sum (peak from 0).
fn max_drawdown(values: &[WideDecimal]) -> WideDecimal {
    let mut peak = WideDecimal::zero();
    let mut running = WideDecimal::zero();
    let mut worst = WideDecimal::zero();
    for value in values {
        running = running.add(value);
        // Python's max keeps the first of equals.
        if running > peak {
            peak = running.clone();
        }
        let fall = peak.sub(&running);
        if fall > worst {
            worst = fall;
        }
    }
    worst
}

fn mean(values: &[WideDecimal]) -> Result<Option<WideDecimal>, WorkerError> {
    if values.is_empty() {
        return Ok(None);
    }
    WideDecimal::sum(values)
        .div(&WideDecimal::from(values.len()))
        .map(Some)
        .map_err(|e| WorkerError::python("InvalidOperation", e.to_string()))
}

/// `journal.summarize_one` for rows of one key.
fn summarize_one(key: PoolKey, rows: &[&JournalRow]) -> Result<Summary, WorkerError> {
    let mut traded: Vec<&JournalRow> = rows.iter().copied().filter(|r| r.traded).collect();
    // `sorted(..., key=trade_date)` is stable.
    traded.sort_by_key(|r| r.trade_date);
    let mut skipped: BTreeMap<String, i64> = BTreeMap::new();
    for row in rows.iter().filter(|r| !r.traded) {
        let reason = row
            .skip_reason
            .clone()
            .unwrap_or_else(|| "UNSPECIFIED".to_owned());
        *skipped.entry(reason).or_insert(0) += 1;
    }
    let rs = traded
        .iter()
        .map(|r| r.r())
        .collect::<Result<Vec<_>, _>>()?;
    let wins = rs.iter().filter(|r| **r > WideDecimal::zero()).count();
    let win_rate = if rs.is_empty() {
        None
    } else {
        Some(
            WideDecimal::from(wins)
                .div(&WideDecimal::from(rs.len()))
                .map_err(|e| WorkerError::python("InvalidOperation", e.to_string()))?,
        )
    };
    // `min(rs)` keeps the first of equals.
    let mut worst: Option<WideDecimal> = None;
    for r in &rs {
        if worst.as_ref().is_none_or(|w| r < w) {
            worst = Some(r.clone());
        }
    }
    Ok(Summary {
        key,
        traded: traded.len(),
        skipped_by_reason: skipped.into_iter().collect(),
        win_rate,
        mean_r: mean(&rs)?,
        worst_r: worst,
        max_drawdown_r: max_drawdown(&rs),
    })
}

/// `journal.summarize`: one summary per `(sleeve, simulated, sizing_mode)` present, ordered by
/// `(sleeve.value, simulated, sizing_mode.value)`.
pub(crate) fn summarize(rows: &[JournalRow]) -> Result<Vec<Summary>, WorkerError> {
    let mut keys: Vec<PoolKey> = Vec::new();
    for row in rows {
        let key = PoolKey {
            sleeve: row.sleeve,
            simulated: row.simulated,
            sizing_mode: row.sizing_mode,
        };
        if !keys.contains(&key) {
            keys.push(key);
        }
    }
    keys.sort_by(|a, b| {
        (sleeve_value(a.sleeve), a.simulated, a.sizing_mode.value()).cmp(&(
            sleeve_value(b.sleeve),
            b.simulated,
            b.sizing_mode.value(),
        ))
    });
    keys.into_iter()
        .map(|key| {
            let mine: Vec<&JournalRow> = rows
                .iter()
                .filter(|r| {
                    r.sleeve == key.sleeve
                        && r.simulated == key.simulated
                        && r.sizing_mode == key.sizing_mode
                })
                .collect();
            summarize_one(key, &mine)
        })
        .collect()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn d(text: &str) -> Decimal {
        text.parse().unwrap()
    }

    fn w(text: &str) -> WideDecimal {
        text.parse().unwrap()
    }

    #[test]
    fn atm_ties_go_to_the_lower_strike() {
        assert_eq!(atm(d("25025"), d("50")).unwrap(), d("25000"));
        assert_eq!(atm(d("25025.01"), d("50")).unwrap(), d("25050"));
        assert_eq!(atm(d("25012.35"), d("50")).unwrap(), d("25000"));
        assert!(atm(d("1"), Decimal::ZERO).is_err());
    }

    #[test]
    fn the_step_is_the_modal_gap_near_the_spot() {
        let near: Vec<Decimal> = (24000..=26000).step_by(50).map(Decimal::from).collect();
        let far: Vec<Decimal> = (22000..24000).step_by(100).map(Decimal::from).collect();
        let all: Vec<Decimal> = near.iter().chain(far.iter()).copied().collect();
        assert_eq!(strike_step(all.clone(), d("25012.35"), 15), Some(d("50")));
        let coarse: Vec<Decimal> = all
            .iter()
            .copied()
            .filter(|k| (*k % d("100")).is_zero())
            .collect();
        assert_eq!(strike_step(coarse, d("25012.35"), 15), Some(d("100")));
        assert_eq!(strike_step([d("100")], d("100"), 15), None);
    }

    #[test]
    fn fifteen_strikes_a_side_of_atm() {
        let near: Vec<Decimal> = (24000..=26000).step_by(50).map(Decimal::from).collect();
        let picked = snapshot_strikes(near, d("25012.35"), d("50"), 15).unwrap();
        assert_eq!(picked.len(), 31);
        assert_eq!(picked.first().copied(), Some(d("24250")));
        assert_eq!(picked.last().copied(), Some(d("25750")));
    }

    #[test]
    fn exp_is_correctly_rounded_to_28_digits() {
        // Python: Decimal("0.0007995").exp() == Decimal("1.000799819685315460074990096")
        assert_eq!(
            exp28(&w("0.0007995")).unwrap().to_sci_string(),
            "1.000799819685315460074990096"
        );
        // Python: Decimal("0.065").exp() == Decimal("1.067159024384192625478894245")
        assert_eq!(
            exp28(&w("0.065")).unwrap().to_sci_string(),
            "1.067159024384192625478894245"
        );
        assert_eq!(exp28(&w("0")).unwrap().to_sci_string(), "1");
        assert!(exp28(&w("-0.1")).is_err());
    }

    #[test]
    fn year_fraction_is_calendar_minutes() {
        let ist = FixedOffset::east_opt(19_800).unwrap();
        let now = NaiveDate::from_ymd_opt(2026, 9, 22)
            .unwrap()
            .and_hms_opt(15, 30, 0)
            .unwrap()
            .and_local_timezone(ist)
            .unwrap();
        let next = NaiveDate::from_ymd_opt(2026, 9, 23).unwrap();
        let t = year_fraction(now, next, market_close());
        assert!((t - 1.0 / 365.0).abs() < 1e-15);
        assert!(
            year_fraction(
                now,
                NaiveDate::from_ymd_opt(2026, 9, 22).unwrap(),
                market_close()
            ) == 0.0
        );
    }

    #[test]
    fn the_greeks_refuse_below_intrinsic_and_the_floor() {
        assert!(solve(0.4, 25000.0, 25000.0, 0.02, RATE, OptionType::Ce, 0.05).is_none());
        assert!(solve(10.0, 25100.0, 25000.0, 0.02, RATE, OptionType::Ce, 0.05).is_none());
        let g = solve(200.0, 25000.0, 25000.0, 0.02, RATE, OptionType::Ce, 0.05).unwrap();
        assert!(g.delta > 0.3 && g.delta < 0.7);
        assert!(g.theta < 0.0 && g.gamma > 0.0 && g.vega > 0.0);
        let p = solve(200.0, 25000.0, 25000.0, 0.02, RATE, OptionType::Pe, 0.05).unwrap();
        assert!(p.delta < 0.0);
    }

    #[test]
    fn a_monthly_is_the_last_expiry_of_its_month() {
        let listed = [
            NaiveDate::from_ymd_opt(2026, 9, 22).unwrap(),
            NaiveDate::from_ymd_opt(2026, 9, 29).unwrap(),
            NaiveDate::from_ymd_opt(2026, 10, 6).unwrap(),
        ];
        assert_eq!(
            kind_in(&listed, listed[0], "NIFTY").unwrap(),
            ExpiryKind::Weekly
        );
        assert_eq!(
            kind_in(&listed, listed[1], "NIFTY").unwrap(),
            ExpiryKind::Monthly
        );
        assert_eq!(
            kind_in(&listed, listed[2], "NIFTY").unwrap(),
            ExpiryKind::Monthly
        );
        assert!(
            kind_in(
                &listed,
                NaiveDate::from_ymd_opt(2026, 9, 23).unwrap(),
                "NIFTY"
            )
            .is_err()
        );
    }

    #[test]
    fn a_summary_never_pools_and_reads_r() {
        let row = |sleeve, simulated, pnl: &str, traded| JournalRow {
            sleeve,
            trade_date: NaiveDate::from_ymd_opt(2026, 10, 27).unwrap(),
            simulated,
            sizing_mode: SizingMode::Budget,
            traded,
            skip_reason: (!traded).then(|| "NO_TRIGGER".to_owned()),
            net_pnl_inr: w(pnl),
            r_inr: w("1000"),
        };
        let rows = [
            row(Sleeve::O2, true, "500", true),
            row(Sleeve::O2, true, "-1500", true),
            row(Sleeve::O1m, true, "0", false),
            row(Sleeve::O2, false, "250", true),
        ];
        let out = summarize(&rows).unwrap();
        assert_eq!(out.len(), 3);
        assert_eq!(sleeve_value(out[0].key.sleeve), "O1M");
        assert_eq!(out[0].skipped_by_reason, vec![("NO_TRIGGER".to_owned(), 1)]);
        let o2 = &out[2];
        assert!(o2.key.simulated);
        assert_eq!(o2.traded, 2);
        assert_eq!(o2.mean_r.as_ref().unwrap().to_sci_string(), "-0.5");
        assert_eq!(o2.worst_r.as_ref().unwrap().to_sci_string(), "-1.5");
        assert_eq!(o2.max_drawdown_r.to_sci_string(), "1.5");
    }
}
