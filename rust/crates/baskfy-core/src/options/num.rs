//! Python `Decimal` arithmetic (default context) on `rust_decimal` values, shared by the options ports.

use std::str::FromStr;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, Offset, TimeDelta, Utc};
use rust_decimal::Decimal;

use crate::decimal::{Rounding, WideDecimal};
use crate::error::{CoreError, CoreResult};
use crate::py;

fn wide(d: Decimal) -> WideDecimal {
    WideDecimal::from(d)
}

fn back(w: &WideDecimal) -> CoreResult<Decimal> {
    w.try_to_decimal()
        .map_err(|e| CoreError::Arithmetic(e.to_string()))
}

/// Python's `a + b`.
pub fn add(a: Decimal, b: Decimal) -> CoreResult<Decimal> {
    back(&wide(a).add(&wide(b)))
}

/// Python's `a - b`.
pub fn sub(a: Decimal, b: Decimal) -> CoreResult<Decimal> {
    back(&wide(a).sub(&wide(b)))
}

/// Python's `a * b`.
pub fn mul(a: Decimal, b: Decimal) -> CoreResult<Decimal> {
    back(&wide(a).mul(&wide(b)))
}

/// Python's `a * n` for an int `n`.
pub fn muli(a: Decimal, n: i64) -> CoreResult<Decimal> {
    mul(a, Decimal::from(n))
}

/// Python's `a / b`.
pub fn div(a: Decimal, b: Decimal) -> CoreResult<Decimal> {
    let q = wide(a)
        .div(&wide(b))
        .map_err(|e| CoreError::Arithmetic(e.to_string()))?;
    // A 28-digit quotient below 0.1 needs scale 29+; rust_decimal holds 28 places, half even.
    back(&q).or_else(|_| back(&q.rescale_exact(-28, Rounding::HalfEven)))
}

/// Python's `-a` (a zero comes back positive).
pub fn neg(a: Decimal) -> CoreResult<Decimal> {
    back(&wide(a).neg())
}

/// Python's `abs(a)`.
pub fn abs(a: Decimal) -> CoreResult<Decimal> {
    back(&wide(a).abs())
}

/// Python's `sum(values, Decimal(0))`.
pub fn sum(values: impl IntoIterator<Item = Decimal>) -> CoreResult<Decimal> {
    values.into_iter().try_fold(Decimal::ZERO, add)
}

/// Python's `max(a, b)` (the first on a tie).
pub fn max(a: Decimal, b: Decimal) -> Decimal {
    if b > a { b } else { a }
}

/// Python's `min(a, b)` (the first on a tie).
pub fn min(a: Decimal, b: Decimal) -> Decimal {
    if b < a { b } else { a }
}

/// Python's `d.quantize(Decimal(10) ** -places, rounding)`.
pub fn quantize(d: Decimal, places: i64, mode: Rounding) -> CoreResult<Decimal> {
    let q = wide(d)
        .quantize(-places, mode)
        .map_err(|e| CoreError::Arithmetic(e.to_string()))?;
    back(&q)
}

/// `quantize(Decimal("0.01"), ROUND_HALF_UP)` — `costs.paise`.
pub fn paise(d: Decimal) -> CoreResult<Decimal> {
    quantize(d, 2, Rounding::HalfUp)
}

/// Python's `str(d)`.
pub fn py_str(d: Decimal) -> String {
    wide(d).to_sci_string()
}

/// Python's `f"{d:.{places}f}"` for a `Decimal` (half even).
pub fn fixed(d: Decimal, places: i64) -> String {
    wide(d)
        .rescale_exact(-places, Rounding::HalfEven)
        .to_string()
}

/// Python's `str(d.quantize(Decimal(10) ** -places, rounding))`, `None` passing through.
pub fn qstr(d: Option<Decimal>, places: i64, mode: Rounding) -> CoreResult<Option<String>> {
    d.map(|v| quantize(v, places, mode).map(py_str)).transpose()
}

/// Python's `Decimal(repr(x))`.
pub fn from_float(x: f64) -> CoreResult<Decimal> {
    py::decimal_from_float_str(x).map_err(|e| CoreError::Arithmetic(e.to_string()))
}

/// Python's `float(d)` (correctly rounded).
pub fn to_float(d: Decimal) -> f64 {
    d.to_string().parse::<f64>().unwrap_or(f64::NAN)
}

/// Python's `int(d.to_integral_value(rounding=ROUND_FLOOR))` for a value that fits an `i64`.
pub fn floor_int(d: Decimal) -> CoreResult<i64> {
    let f = d.floor();
    i64::try_from(f.trunc().mantissa() / 10_i128.pow(f.scale()))
        .map_err(|_| CoreError::Arithmetic(format!("{d} does not fit an int")))
}

/// Python's `Decimal(repr(a)) * Decimal(repr(b))` then `.exp()` — `chain.parity_forward`'s growth.
pub fn exp_of_product(a: f64, b: f64) -> CoreResult<Decimal> {
    let x = py::wide_from_float_str(a)
        .and_then(|wa| py::wide_from_float_str(b).map(|wb| wa.mul(&wb)))
        .map_err(|e| CoreError::Arithmetic(e.to_string()))?;
    back(&exp(&x)?)
}

/// Python's `Decimal.exp()` in the default context: correctly rounded to 28 digits, half even.
pub fn exp(x: &WideDecimal) -> CoreResult<WideDecimal> {
    if x.is_zero() {
        return Ok(WideDecimal::from(1_i64));
    }
    let text = x.abs().to_string();
    let (int_part, frac_part) = text.split_once('.').unwrap_or((text.as_str(), ""));
    if int_part.len() > 3 {
        return Err(CoreError::Arithmetic(format!("exp({x}) out of range")));
    }
    let digits = format!("{int_part}{frac_part}");
    let p = Big::parse(&digits);
    let s = frac_part.len();
    const PRECISION: usize = 80;
    let one = Big::pow10(PRECISION);
    let mut term = one.clone();
    let mut plus = one;
    let mut minus = Big::zero();
    let negative = x.is_sign_negative();
    let mut k: u32 = 1;
    while !term.is_zero() {
        term = term.mul(&p).shift_down(s).div_small(k);
        if negative && k % 2 == 1 {
            minus = minus.add(&term);
        } else {
            plus = plus.add(&term);
        }
        k += 1;
        if k > 2000 {
            return Err(CoreError::Arithmetic(format!("exp({x}) did not converge")));
        }
    }
    let total = plus.sub(&minus);
    let mut all = total.digits();
    let int_len = i64::try_from(all.len()).unwrap_or(0) - i64::try_from(PRECISION).unwrap_or(0);
    // Round to 28 significant digits, half even; the tail is never an exact tie (inexact exp).
    let keep = 28_usize;
    if all.len() <= keep {
        all.push_str(&"0".repeat(keep + 1 - all.len()));
    }
    let head = &all[..keep];
    let next = all.as_bytes()[keep];
    let rest_nonzero = all[keep + 1..].bytes().any(|b| b != b'0');
    let last_odd = (head.as_bytes()[keep - 1] - b'0') % 2 == 1;
    let up = next > b'5' || (next == b'5' && (rest_nonzero || last_odd));
    let mut coef = Big::parse(head);
    if up {
        coef = coef.add(&Big::parse("1"));
    }
    let mut coef_digits = coef.digits();
    let mut exponent = int_len - i64::try_from(keep).unwrap_or(28);
    if coef_digits.len() > keep {
        coef_digits.pop();
        exponent += 1;
    }
    WideDecimal::from_str(&format!("{coef_digits}E{exponent}"))
        .map_err(|e| CoreError::Arithmetic(e.to_string()))
}

/// A minimal unsigned big integer (base 10^9, little endian) for [`exp`].
#[derive(Clone, Debug)]
struct Big(Vec<u64>);

const BASE: u64 = 1_000_000_000;

impl Big {
    fn zero() -> Self {
        Big(Vec::new())
    }
    fn trim(mut self) -> Self {
        while self.0.last() == Some(&0) {
            self.0.pop();
        }
        self
    }
    fn is_zero(&self) -> bool {
        self.0.iter().all(|&l| l == 0)
    }
    fn parse(s: &str) -> Self {
        let bytes = s.as_bytes();
        let mut limbs = Vec::new();
        let mut end = bytes.len();
        while end > 0 {
            let start = end.saturating_sub(9);
            let chunk = std::str::from_utf8(&bytes[start..end]).unwrap_or("0");
            limbs.push(chunk.parse::<u64>().unwrap_or(0));
            end = start;
        }
        Big(limbs).trim()
    }
    fn pow10(n: usize) -> Self {
        Big::parse(&format!("1{}", "0".repeat(n)))
    }
    fn digits(&self) -> String {
        let t = self.clone().trim();
        let Some((last, rest)) = t.0.split_last() else {
            return "0".to_owned();
        };
        let mut out = last.to_string();
        for limb in rest.iter().rev() {
            out.push_str(&format!("{limb:09}"));
        }
        out
    }
    fn add(&self, other: &Self) -> Self {
        let n = self.0.len().max(other.0.len());
        let mut out = Vec::with_capacity(n + 1);
        let mut carry = 0;
        for i in 0..n {
            let v =
                self.0.get(i).copied().unwrap_or(0) + other.0.get(i).copied().unwrap_or(0) + carry;
            out.push(v % BASE);
            carry = v / BASE;
        }
        if carry > 0 {
            out.push(carry);
        }
        Big(out).trim()
    }
    /// `self - other`, `self >= other`.
    fn sub(&self, other: &Self) -> Self {
        let mut out = Vec::with_capacity(self.0.len());
        let mut borrow = 0_u64;
        for i in 0..self.0.len() {
            let a = self.0[i];
            let b = other.0.get(i).copied().unwrap_or(0) + borrow;
            if a >= b {
                out.push(a - b);
                borrow = 0;
            } else {
                out.push(a + BASE - b);
                borrow = 1;
            }
        }
        Big(out).trim()
    }
    fn mul(&self, other: &Self) -> Self {
        if self.is_zero() || other.is_zero() {
            return Big::zero();
        }
        let mut out = vec![0_u64; self.0.len() + other.0.len() + 1];
        for (i, &a) in self.0.iter().enumerate() {
            let mut carry = 0_u64;
            for (j, &b) in other.0.iter().enumerate() {
                let v = out[i + j] + a * b + carry;
                out[i + j] = v % BASE;
                carry = v / BASE;
            }
            let mut k = i + other.0.len();
            while carry > 0 {
                let v = out[k] + carry;
                out[k] = v % BASE;
                carry = v / BASE;
                k += 1;
            }
        }
        Big(out).trim()
    }
    fn div_small(&self, d: u32) -> Self {
        let d = u64::from(d.max(1));
        let mut out = vec![0_u64; self.0.len()];
        let mut rem = 0_u64;
        for i in (0..self.0.len()).rev() {
            let cur = rem * BASE + self.0[i];
            out[i] = cur / d;
            rem = cur % d;
        }
        Big(out).trim()
    }
    /// Floor division by `10^n`.
    fn shift_down(&self, n: usize) -> Self {
        let d = self.digits();
        if d.len() <= n {
            return Big::zero();
        }
        Big::parse(&d[..d.len() - n])
    }
}

/// India Standard Time, a fixed offset (no daylight saving).
pub fn ist_offset() -> FixedOffset {
    FixedOffset::east_opt(5 * 3600 + 30 * 60).unwrap_or_else(|| Utc.fix())
}

/// `dt.datetime.combine(day, clock, tzinfo=tz)`.
pub fn combine(day: NaiveDate, clock: NaiveTime, tz: FixedOffset) -> DateTime<FixedOffset> {
    let naive = NaiveDateTime::new(day, clock);
    let utc = naive - TimeDelta::seconds(i64::from(tz.local_minus_utc()));
    DateTime::from_naive_utc_and_offset(utc, tz)
}

/// `dt.datetime.combine(day, clock, tzinfo=IST)`.
pub fn at_ist(day: NaiveDate, clock: NaiveTime) -> DateTime<FixedOffset> {
    combine(day, clock, ist_offset())
}

/// `ts` as naive IST wall clock (`bars.ist`).
pub fn ist(ts: &DateTime<FixedOffset>) -> NaiveDateTime {
    ts.with_timezone(&ist_offset()).naive_local()
}

/// `(a - b).total_seconds()` as a float.
pub fn seconds_between(a: &DateTime<FixedOffset>, b: &DateTime<FixedOffset>) -> f64 {
    let d = *a - *b;
    let micros = d.num_microseconds().unwrap_or(i64::MAX);
    #[allow(clippy::cast_precision_loss)]
    let out = micros as f64 / 1_000_000.0;
    out
}

/// `t.strftime("%H:%M")`.
pub fn hhmm(t: NaiveTime) -> String {
    t.format("%H:%M").to_string()
}
