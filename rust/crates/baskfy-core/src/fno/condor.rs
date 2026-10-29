//! Port of `baskfy_core.fno.condor` (`decile-blueprint/packages/core/src/baskfy_core/fno/condor.py`).
//!
//! F1: the index monthly iron condor's strikes, credit, max loss, liquidity and sequences
//! (`04` §2), and its profit-take and loss-close predicates (`04` §1).
//!
//! Strikes (`sd = F·sigma·√T`, `T` = calendar days ÷ 365): the long call is the smallest listed
//! strike ≥ `F + (k + w)·sd`, the short call the smallest ≥ `F + k·sd`, the short put the
//! largest ≤ `F - k·sd`, the long put the largest ≤ `F - (k + w)·sd`. A wing equal to its short
//! (or no listed strike) is `REJECTED_STRUCTURE`. The credit `C = Σ short mids - Σ long mids`
//! must be positive; the max loss per unit is `max(call width, put width) - C`. **Entry** goes
//! long put → long call → short put → short call; **exit** goes short call → short put → long
//! call → long put, so the book is covered at every prefix.
//!
//! The cost to close is valued with each vertical clamped to `[0, width]`, so the loss close
//! **never** reads a loss beyond the wings' max loss.
//!
//! Every `Decimal` operation goes through Python's default context ([`super::config::py`]), so
//! scales and signed zeros print as Python prints them.
//!
//! Goldens: `L1/fno/condor.<function>.case_NNN.json`.

use std::collections::BTreeMap;

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize, Serializer, ser::SerializeStruct};

use super::config::py::{add, mul, sub};
use super::config::{F1Config, FnoError, FnoResult, PlanState, py};
use super::vol::OptionType;
use crate::backtest::helpers::{Rounding, WideDecimal};

/// Python numeric helpers the F&O decision ports share (float `repr`, `round`, `quantize`).
pub(crate) mod num {
    use rust_decimal::Decimal;

    use super::super::config::{FnoError, FnoResult};
    use crate::backtest::helpers::{BacktestError, Rounding, WideDecimal};

    /// A `WideDecimal` failure as the fno lane's error.
    pub(crate) fn wide_err(e: &BacktestError) -> FnoError {
        FnoError::Decimal(e.to_string())
    }

    /// A `WideDecimal` narrowed to `rust_decimal`.
    pub(crate) fn narrow(w: &WideDecimal) -> FnoResult<Decimal> {
        w.try_to_decimal().map_err(|e| wide_err(&e))
    }

    /// `d.quantize(Decimal(10) ** exp, rounding=mode)`.
    pub(crate) fn quantize(d: Decimal, exp: i64, mode: Rounding) -> FnoResult<Decimal> {
        let q = WideDecimal::from(d)
            .quantize(exp, mode)
            .map_err(|e| wide_err(&e))?;
        narrow(&q)
    }

    /// `_money`: `value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)`.
    pub(crate) fn money(d: Decimal) -> FnoResult<Decimal> {
        quantize(d, -2, Rounding::HalfUp)
    }

    /// `x.to_integral_value(rounding=ROUND_FLOOR)` (`up = false`) or `ROUND_CEILING`
    /// (`up = true`), on a value already in the default context.
    pub(crate) fn to_integral(x: &WideDecimal, up: bool) -> FnoResult<WideDecimal> {
        if x.exponent() >= 0 {
            return Ok(x.clone());
        }
        let down = x.quantize(0, Rounding::Down).map_err(|e| wide_err(&e))?;
        if down == *x {
            return Ok(down);
        }
        let one = WideDecimal::from(1_i64);
        let negative = x.is_sign_negative();
        Ok(match (up, negative) {
            // Floor of a negative inexact value and ceiling of a positive one step away from
            // zero; the other two are truncation (a ceiling of -0.5 is Python's `-0`).
            (false, true) => down.sub(&one),
            (true, false) => down.add(&one),
            _ => down,
        })
    }

    /// `float(d)`: the double nearest the decimal (Python converts correctly rounded).
    pub(crate) fn to_f64(d: Decimal) -> FnoResult<f64> {
        let text = d.to_string();
        text.parse::<f64>()
            .map_err(|e| FnoError::Value(format!("float({text}): {e}")))
    }

    /// Python's `round(x, ndigits)`: the double nearest the correctly rounded decimal.
    pub(crate) fn py_round(x: f64, ndigits: usize) -> f64 {
        if !x.is_finite() {
            return x;
        }
        format!("{x:.ndigits$}").parse::<f64>().unwrap_or(x)
    }

    /// `repr(float)`: shortest round-trip digits, positional for decimal exponents in
    /// `[-4, 16)` (always with a fractional part), scientific otherwise (`1e-05`).
    pub(crate) fn py_float_repr(x: f64) -> String {
        if x.is_nan() {
            return "nan".to_owned();
        }
        if x.is_infinite() {
            return if x > 0.0 { "inf" } else { "-inf" }.to_owned();
        }
        let sci = format!("{x:e}");
        let (mantissa, exp) = sci.split_once('e').unwrap_or((sci.as_str(), "0"));
        let exp: i64 = exp.parse().unwrap_or(0);
        let (sign, mantissa) = match mantissa.strip_prefix('-') {
            Some(m) => ("-", m),
            None => ("", mantissa),
        };
        let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
        let n = i64::try_from(digits.len()).unwrap_or(i64::MAX);
        if (-4..16).contains(&exp) {
            if exp >= 0 {
                let int_len = exp + 1;
                if n <= int_len {
                    let pad = usize::try_from(int_len - n).unwrap_or(0);
                    format!("{sign}{digits}{}.0", "0".repeat(pad))
                } else {
                    let cut = usize::try_from(int_len).unwrap_or(0);
                    format!("{sign}{}.{}", &digits[..cut], &digits[cut..])
                }
            } else {
                let pad = usize::try_from(-exp - 1).unwrap_or(0);
                format!("{sign}0.{}{digits}", "0".repeat(pad))
            }
        } else {
            let (head, tail) = digits.split_at(1);
            let point = if tail.is_empty() {
                String::new()
            } else {
                format!(".{tail}")
            };
            let exp_sign = if exp < 0 { "-" } else { "+" };
            format!("{sign}{head}{point}e{exp_sign}{:02}", exp.abs())
        }
    }

    /// `Decimal(repr(x))`, kept wide (a tiny float prints at a scale `rust_decimal` lacks).
    pub(crate) fn wide_from_float(x: f64) -> FnoResult<WideDecimal> {
        let text = py_float_repr(x);
        text.parse::<WideDecimal>().map_err(|_| {
            FnoError::Decimal(format!(
                "[<class 'decimal.ConversionSyntax'>] Decimal({text:?})"
            ))
        })
    }

    /// `Decimal(repr(x))`.
    pub(crate) fn dec_from_float(x: f64) -> FnoResult<Decimal> {
        narrow(&wide_from_float(x)?)
    }

    #[cfg(test)]
    mod tests {
        #![allow(clippy::float_cmp)]
        use super::*;

        #[test]
        fn round_and_repr_match_python() {
            assert_eq!(py_round(0.25, 1), 0.2);
            assert_eq!(py_round(0.35, 1), 0.3);
            assert_eq!(py_round(922.25, 1), 922.2);
            assert_eq!(py_round(922.75, 1), 922.8);
            assert_eq!(py_round(2.675, 2), 2.67);
            assert_eq!(py_float_repr(1e-5), "1e-05");
            assert_eq!(py_float_repr(920.0), "920.0");
            assert_eq!(py_float_repr(1e16), "1e+16");
        }
    }
}

/// `Decimal(100)`.
fn hundred() -> Decimal {
    Decimal::ONE_HUNDRED
}

/// The four legs of the condor.
///
/// Ports `baskfy_core.fno.condor.LegRole` (declaration order is `Ord`'s).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum LegRole {
    /// The put wing.
    LongPut,
    /// The call wing.
    LongCall,
    /// The short put.
    ShortPut,
    /// The short call.
    ShortCall,
}

impl LegRole {
    /// `list(LegRole)`, in declaration order.
    pub const ALL: [Self; 4] = [
        Self::LongPut,
        Self::LongCall,
        Self::ShortPut,
        Self::ShortCall,
    ];

    /// The enum's value (`"LONG_PUT"`, ...).
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::LongPut => "LONG_PUT",
            Self::LongCall => "LONG_CALL",
            Self::ShortPut => "SHORT_PUT",
            Self::ShortCall => "SHORT_CALL",
        }
    }

    /// `role.value.lower().replace("_", " ")` (`"short put"`).
    #[must_use]
    pub fn words(self) -> &'static str {
        match self {
            Self::LongPut => "long put",
            Self::LongCall => "long call",
            Self::ShortPut => "short put",
            Self::ShortCall => "short call",
        }
    }
}

/// `fo_leg.entry_seq` (`04` §2): longs first.
///
/// Ports `baskfy_core.fno.condor.ENTRY_SEQUENCE`.
pub const ENTRY_SEQUENCE: [LegRole; 4] = [
    LegRole::LongPut,
    LegRole::LongCall,
    LegRole::ShortPut,
    LegRole::ShortCall,
];

/// The exit order (`04` §2): shorts first.
///
/// Ports `baskfy_core.fno.condor.EXIT_SEQUENCE`.
pub const EXIT_SEQUENCE: [LegRole; 4] = [
    LegRole::ShortCall,
    LegRole::ShortPut,
    LegRole::LongCall,
    LegRole::LongPut,
];

/// A per-unit price for each leg, keyed by role.
pub type RoleMap = BTreeMap<LegRole, Decimal>;

/// The option type of a leg.
///
/// Ports `baskfy_core.fno.condor.option_type_of`.
#[must_use]
pub fn option_type_of(role: LegRole) -> OptionType {
    match role {
        LegRole::LongCall | LegRole::ShortCall => OptionType::Ce,
        LegRole::LongPut | LegRole::ShortPut => OptionType::Pe,
    }
}

/// +1 for a long leg, -1 for a short leg.
///
/// Ports `baskfy_core.fno.condor.sign_of`.
#[must_use]
pub fn sign_of(role: LegRole) -> i64 {
    match role {
        LegRole::LongCall | LegRole::LongPut => 1,
        LegRole::ShortCall | LegRole::ShortPut => -1,
    }
}

/// `mapping[role]`, or Python's `KeyError` naming the role.
pub(crate) fn leg(map: &RoleMap, role: LegRole) -> FnoResult<Decimal> {
    map.get(&role)
        .copied()
        .ok_or_else(|| FnoError::Key(role.value().to_owned()))
}

/// The four strikes.
///
/// Ports `baskfy_core.fno.condor.CondorStrikes` (serialised with its `call_width`,
/// `put_width` and `max_width` properties, as the goldens hold it).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
pub struct CondorStrikes {
    /// The call wing.
    pub long_call: Decimal,
    /// The short call.
    pub short_call: Decimal,
    /// The short put.
    pub short_put: Decimal,
    /// The put wing.
    pub long_put: Decimal,
}

impl CondorStrikes {
    /// The strike of one leg.
    ///
    /// Ports `baskfy_core.fno.condor.CondorStrikes.strike`.
    #[must_use]
    pub fn strike(&self, role: LegRole) -> Decimal {
        match role {
            LegRole::LongCall => self.long_call,
            LegRole::ShortCall => self.short_call,
            LegRole::ShortPut => self.short_put,
            LegRole::LongPut => self.long_put,
        }
    }

    /// `long_call - short_call`.
    ///
    /// # Errors
    /// A difference the default context cannot hold.
    pub fn call_width(&self) -> FnoResult<Decimal> {
        sub(self.long_call, self.short_call)
    }

    /// `short_put - long_put`.
    ///
    /// # Errors
    /// A difference the default context cannot hold.
    pub fn put_width(&self) -> FnoResult<Decimal> {
        sub(self.short_put, self.long_put)
    }

    /// `max(call_width, put_width)`.
    ///
    /// # Errors
    /// A difference the default context cannot hold.
    pub fn max_width(&self) -> FnoResult<Decimal> {
        Ok(py::max(self.call_width()?, self.put_width()?))
    }
}

impl Serialize for CondorStrikes {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let err = serde::ser::Error::custom;
        let mut s = serializer.serialize_struct("CondorStrikes", 7)?;
        s.serialize_field("long_call", &self.long_call)?;
        s.serialize_field("short_call", &self.short_call)?;
        s.serialize_field("short_put", &self.short_put)?;
        s.serialize_field("long_put", &self.long_put)?;
        s.serialize_field("call_width", &self.call_width().map_err(err)?)?;
        s.serialize_field("put_width", &self.put_width().map_err(err)?)?;
        s.serialize_field("max_width", &self.max_width().map_err(err)?)?;
        s.end()
    }
}

/// The strikes, or the reason there are none (`state` is then `REJECTED_STRUCTURE`).
///
/// Ports `baskfy_core.fno.condor.StrikeChoice`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StrikeChoice {
    /// The strikes, when every leg found one.
    pub strikes: Option<CondorStrikes>,
    /// One standard deviation in points.
    pub sd: Decimal,
    /// Why there are no strikes, in words.
    pub reasons: Vec<String>,
}

impl StrikeChoice {
    /// `None` with strikes, else `REJECTED_STRUCTURE`.
    ///
    /// Ports `baskfy_core.fno.condor.StrikeChoice.state`.
    #[must_use]
    pub fn state(&self) -> Option<PlanState> {
        if self.strikes.is_some() {
            None
        } else {
            Some(PlanState::RejectedStructure)
        }
    }
}

impl Serialize for StrikeChoice {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut s = serializer.serialize_struct("StrikeChoice", 4)?;
        s.serialize_field("strikes", &self.strikes)?;
        s.serialize_field("sd", &self.sd)?;
        s.serialize_field("reasons", &self.reasons)?;
        s.serialize_field("state", &self.state())?;
        s.end()
    }
}

/// `sd = F · sigma · √T` with `T` in calendar years (`04` §2).
///
/// Ports `baskfy_core.fno.condor.one_sd`.
///
/// # Errors
/// `ValueError` without a positive IV and days to expiry.
pub fn one_sd(forward: Decimal, iv: f64, days_to_expiry: i64) -> FnoResult<Decimal> {
    // `iv <= 0` is False for NaN in Python, so a NaN IV passes on to `repr` (and fails there).
    if days_to_expiry <= 0 || iv <= 0.0 {
        return Err(FnoError::Value(
            "sd needs a positive IV and days to expiry".to_owned(),
        ));
    }
    #[allow(clippy::cast_precision_loss)] // Python's int / int true division
    let years = days_to_expiry as f64 / 365.0;
    let root = num::wide_from_float(iv * years.sqrt())?;
    num::narrow(&WideDecimal::from(forward).mul(&root))
}

/// `min(k for k in listed if k >= target)` — the first of equal minima, as Python keeps it.
fn smallest_at_or_above(listed: &[Decimal], target: Decimal) -> Option<Decimal> {
    listed
        .iter()
        .filter(|k| **k >= target)
        .fold(None, |best: Option<Decimal>, k| match best {
            Some(b) if *k >= b => Some(b),
            _ => Some(*k),
        })
}

/// `max(k for k in listed if k <= target)` — the first of equal maxima.
fn largest_at_or_below(listed: &[Decimal], target: Decimal) -> Option<Decimal> {
    listed
        .iter()
        .filter(|k| **k <= target)
        .fold(None, |best: Option<Decimal>, k| match best {
            Some(b) if *k <= b => Some(b),
            _ => Some(*k),
        })
}

/// `04` §2's table on the listed strikes of each type.
///
/// Ports `baskfy_core.fno.condor.select_strikes`.
///
/// # Errors
/// `ValueError` from [`one_sd`].
pub fn select_strikes(
    forward: Decimal,
    iv: f64,
    days_to_expiry: i64,
    call_strikes: &[Decimal],
    put_strikes: &[Decimal],
    config: &F1Config,
) -> FnoResult<StrikeChoice> {
    let sd = one_sd(forward, iv, days_to_expiry)?;
    let (k, w) = (config.short_sigma, config.wing_sigma);
    let far = mul(add(k, w)?, sd)?;
    let near = mul(k, sd)?;
    let long_call = smallest_at_or_above(call_strikes, add(forward, far)?);
    let short_call = smallest_at_or_above(call_strikes, add(forward, near)?);
    let short_put = largest_at_or_below(put_strikes, sub(forward, near)?);
    let long_put = largest_at_or_below(put_strikes, sub(forward, far)?);
    let mut reasons: Vec<String> = [
        ("long call", long_call),
        ("short call", short_call),
        ("short put", short_put),
        ("long put", long_put),
    ]
    .iter()
    .filter(|(_, v)| v.is_none())
    .map(|(name, _)| format!("no listed strike for the {name}"))
    .collect();
    if let (Some(lc), Some(sc)) = (long_call, short_call)
        && lc == sc
    {
        reasons.push(format!(
            "the call wing equals the short call ({})",
            py::str(sc)
        ));
    }
    if let (Some(lp), Some(sp)) = (long_put, short_put)
        && lp == sp
    {
        reasons.push(format!(
            "the put wing equals the short put ({})",
            py::str(sp)
        ));
    }
    match (long_call, short_call, short_put, long_put) {
        (Some(long_call), Some(short_call), Some(short_put), Some(long_put))
            if reasons.is_empty() =>
        {
            Ok(StrikeChoice {
                strikes: Some(CondorStrikes {
                    long_call,
                    short_call,
                    short_put,
                    long_put,
                }),
                sd,
                reasons: Vec::new(),
            })
        }
        _ => Ok(StrikeChoice {
            strikes: None,
            sd,
            reasons,
        }),
    }
}

/// `C = Σ short mids - Σ long mids` per unit (`04` §2).
///
/// Ports `baskfy_core.fno.condor.credit`.
///
/// # Errors
/// `KeyError` naming the first role (in `LegRole` order) the mapping lacks.
pub fn credit(mids: &RoleMap) -> FnoResult<Decimal> {
    let mut total = Decimal::ZERO;
    for role in LegRole::ALL {
        total = add(total, mul(Decimal::from(-sign_of(role)), leg(mids, role)?)?)?;
    }
    Ok(total)
}

/// `max(call width, put width) - C` (`04` §2).
///
/// Ports `baskfy_core.fno.condor.max_loss_per_unit`.
///
/// # Errors
/// A difference the default context cannot hold.
pub fn max_loss_per_unit(strikes: &CondorStrikes, credit_per_unit: Decimal) -> FnoResult<Decimal> {
    sub(strikes.max_width()?, credit_per_unit)
}

/// A priced condor, or its rejection (`state` set, `reasons` in words).
///
/// Ports `baskfy_core.fno.condor.Structure`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Structure {
    /// The strikes priced.
    pub strikes: Option<CondorStrikes>,
    /// The credit per unit.
    pub credit: Decimal,
    /// The max loss per unit.
    pub max_loss_per_unit: Decimal,
    /// `REJECTED_STRUCTURE`, or `None` when the structure prices.
    pub state: Option<PlanState>,
    /// The refusal in words.
    pub reasons: Vec<String>,
}

/// Credit and max loss; a non-positive credit (or max loss) is `REJECTED_STRUCTURE`.
///
/// Ports `baskfy_core.fno.condor.price_structure`.
///
/// # Errors
/// `KeyError` for a missing leg.
pub fn price_structure(strikes: &CondorStrikes, mids: &RoleMap) -> FnoResult<Structure> {
    let c = credit(mids)?;
    let loss = max_loss_per_unit(strikes, c)?;
    let rejected = |reason: String| Structure {
        strikes: Some(*strikes),
        credit: c,
        max_loss_per_unit: loss,
        state: Some(PlanState::RejectedStructure),
        reasons: vec![reason],
    };
    if c <= Decimal::ZERO {
        return Ok(rejected(format!("credit {} is not positive", py::str(c))));
    }
    if loss <= Decimal::ZERO {
        return Ok(rejected(format!(
            "max loss {} is not positive",
            py::str(loss)
        )));
    }
    Ok(Structure {
        strikes: Some(*strikes),
        credit: c,
        max_loss_per_unit: loss,
        state: None,
        reasons: Vec::new(),
    })
}

/// A live quote for one leg: best bid, best ask and open interest in lots.
///
/// Ports `baskfy_core.fno.condor.LegQuote` (serialised with `two_sided` and `mid`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
pub struct LegQuote {
    /// Best bid.
    pub bid: Option<Decimal>,
    /// Best ask.
    pub ask: Option<Decimal>,
    /// Open interest, lots.
    pub oi_lots: i64,
}

impl LegQuote {
    /// Both sides present and positive.
    ///
    /// Ports `baskfy_core.fno.condor.LegQuote.two_sided`.
    #[must_use]
    pub fn two_sided(&self) -> bool {
        matches!((self.bid, self.ask), (Some(b), Some(a)) if b > Decimal::ZERO && a > Decimal::ZERO)
    }

    /// `(bid + ask) / 2` when two-sided.
    ///
    /// Ports `baskfy_core.fno.condor.LegQuote.mid`.
    ///
    /// # Errors
    /// A sum the default context cannot hold.
    pub fn mid(&self) -> FnoResult<Option<Decimal>> {
        match (self.bid, self.ask) {
            (Some(b), Some(a)) if self.two_sided() => Ok(Some(py::div(add(b, a)?, Decimal::TWO)?)),
            _ => Ok(None),
        }
    }
}

impl Serialize for LegQuote {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mid = self.mid().map_err(serde::ser::Error::custom)?;
        let mut s = serializer.serialize_struct("LegQuote", 5)?;
        s.serialize_field("bid", &self.bid)?;
        s.serialize_field("ask", &self.ask)?;
        s.serialize_field("oi_lots", &self.oi_lots)?;
        s.serialize_field("two_sided", &self.two_sided())?;
        s.serialize_field("mid", &mid)?;
        s.end()
    }
}

/// `04` §2 liquidity, **by name**: each short has OI ≥ `f1_min_short_oi_lots` and a spread ≤
/// `f1_max_spread_pct` of its mid; each wing has a two-sided quote. Empty is OK; anything else
/// is `REJECTED_LIQUIDITY`.
///
/// Ports `baskfy_core.fno.condor.liquidity_refusals`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn liquidity_refusals(
    strikes: &CondorStrikes,
    quotes: &BTreeMap<LegRole, LegQuote>,
    config: &F1Config,
) -> FnoResult<Vec<String>> {
    let mut found = Vec::new();
    for role in ENTRY_SEQUENCE {
        let name = format!("{} {}", role.words(), py::str(strikes.strike(role)));
        let Some(quote) = quotes.get(&role).filter(|q| q.two_sided()) else {
            found.push(format!("{name}: no two-sided quote"));
            continue;
        };
        if sign_of(role) > 0 {
            continue;
        }
        let mid = quote.mid()?;
        if quote.oi_lots < config.min_short_oi_lots {
            found.push(format!(
                "{name}: OI {} lots < {}",
                quote.oi_lots, config.min_short_oi_lots
            ));
        }
        if let (Some(mid), Some(bid), Some(ask)) = (mid, quote.bid, quote.ask) {
            // `(ask - bid) / mid * 100` stays wide: the quotient prints at scale 29+ (R1.3).
            let spread_pct = py::div_wide(sub(ask, bid)?, mid)?.mul(&WideDecimal::from(hundred()));
            if spread_pct > WideDecimal::from(config.max_spread_pct) {
                let shown = spread_pct
                    .quantize(-2, Rounding::HalfEven)
                    .map_err(|e| num::wide_err(&e))?;
                found.push(format!(
                    "{name}: spread {shown} % of mid > {} %",
                    py::str(config.max_spread_pct)
                ));
            }
        }
    }
    Ok(found)
}

/// What buying the structure back costs per unit, each vertical clamped to `[0, width]` — so
/// it is never more than the wider width (the wings' max loss + C).
///
/// Ports `baskfy_core.fno.condor.cost_to_close`.
///
/// # Errors
/// `KeyError` for a missing mark (in the order Python reads them).
pub fn cost_to_close(strikes: &CondorStrikes, marks: &RoleMap) -> FnoResult<Decimal> {
    let calls = sub(
        leg(marks, LegRole::ShortCall)?,
        leg(marks, LegRole::LongCall)?,
    )?;
    let puts = sub(
        leg(marks, LegRole::ShortPut)?,
        leg(marks, LegRole::LongPut)?,
    )?;
    let zero = Decimal::ZERO;
    add(
        py::min(py::max(calls, zero), strikes.call_width()?),
        py::min(py::max(puts, zero), strikes.put_width()?),
    )
}

/// `(1 - pct) x C`: close when the cost to close is at or below it (`04` §1).
///
/// Ports `baskfy_core.fno.condor.profit_take_level`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn profit_take_level(entry_credit: Decimal, config: &F1Config) -> FnoResult<Decimal> {
    let keep = sub(Decimal::ONE, py::div(config.profit_take_pct, hundred())?)?;
    mul(keep, entry_credit)
}

/// `(1 + mult) x C`, never above the wider width: the loss close never widens past the wings'
/// max loss (`04` §1).
///
/// Ports `baskfy_core.fno.condor.loss_close_level`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn loss_close_level(
    entry_credit: Decimal,
    strikes: &CondorStrikes,
    config: &F1Config,
) -> FnoResult<Decimal> {
    let level = mul(add(Decimal::ONE, config.loss_close_mult)?, entry_credit)?;
    Ok(py::min(level, strikes.max_width()?))
}

/// `close_cost <= profit_take_level`.
///
/// Ports `baskfy_core.fno.condor.profit_take_hit`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn profit_take_hit(
    close_cost: Decimal,
    entry_credit: Decimal,
    config: &F1Config,
) -> FnoResult<bool> {
    Ok(close_cost <= profit_take_level(entry_credit, config)?)
}

/// `close_cost >= loss_close_level`.
///
/// Ports `baskfy_core.fno.condor.loss_close_hit`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn loss_close_hit(
    close_cost: Decimal,
    entry_credit: Decimal,
    strikes: &CondorStrikes,
    config: &F1Config,
) -> FnoResult<bool> {
    Ok(close_cost >= loss_close_level(entry_credit, strikes, config)?)
}

/// A [`RoleMap`] as `fno::costs` takes it (keyed by the role's value).
#[must_use]
pub fn role_prices(map: &RoleMap) -> super::costs::RolePrices {
    map.iter()
        .map(|(role, price)| (role.value().to_owned(), *price))
        .collect()
}
