//! `WideDecimal` — Python's `Decimal` in its default context, shared by every lane.
//!
//! Python's `Decimal` arithmetic runs in a context of **28 significant digits**, rounding
//! half-even, with the specification's ideal-exponent rules (an exact quotient keeps the
//! smallest exponent it can, `Decimal(0) / Decimal("365.25")` is `0E+2`). `rust_decimal` caps
//! the *scale* at 28 instead, so a 28-digit quotient below 0.1 (`year_fraction`'s
//! `-0.07939767282683093771389459274`, scale 29) cannot be held, and its division rounds to a
//! different digit count (`1 / 0.18` is `5.555555555555555555555555556` in Python, 28 digits).
//! DECISIONS-RS R1.3 (decided by the lead): such values are reproduced **exactly** by a wider
//! exact decimal. [`WideDecimal`] is that type: a sign, a decimal coefficient of any length and
//! an exponent, with `add`/`sub`/`mul`/`div`/`quantize` written step for step from CPython's
//! `_pydecimal` (which `libmpdec` agrees with), so every intermediate rounds where Python's
//! rounds.
//!
//! Written for `baskfy_core.backtest` at RS1 (it lived in `backtest::helpers`, which still
//! re-exports it); lifted here at the wave-3 housekeeping (DECISIONS-RS R3.4) because twt, vbt
//! and fno all compute with it.

use std::cmp::Ordering;
use std::fmt;
use std::str::FromStr;

use rust_decimal::Decimal;
use serde::{Deserialize, Deserializer, Serialize, Serializer};

/// A `WideDecimal` operation Python's `decimal` module would raise on (`DivisionByZero`,
/// `InvalidOperation`, `ConversionSyntax`), or a value too wide for `rust_decimal` on the way
/// out. The message is the whole display, as `BacktestError::Decimal`'s was.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct DecimalError(pub String);

// =============================================================================================
// WideDecimal — Python's Decimal in its default context (prec 28, ROUND_HALF_EVEN)
// =============================================================================================

/// Python's default context precision.
const PREC: i64 = 28;

/// A rounding mode of Python's `decimal` module.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Rounding {
    /// `ROUND_HALF_EVEN` (the context default).
    HalfEven,
    /// `ROUND_HALF_UP`.
    HalfUp,
    /// `ROUND_DOWN` (towards zero).
    Down,
}

/// An exact decimal with Python `Decimal` semantics: any number of coefficient digits, an
/// exponent, a signed zero, and arithmetic that rounds to 28 significant digits half-even
/// exactly where Python's default context does (DECISIONS-RS R1.3).
///
/// Equality and ordering are numeric, as in Python (`0.5 == 0.50`); [`fmt::Display`] is
/// `format(d, "f")`, the golden encoding; [`WideDecimal::to_sci_string`] is `str(d)`.
#[derive(Debug, Clone)]
pub struct WideDecimal {
    neg: bool,
    /// Coefficient digits, no leading zeros, `"0"` for zero.
    digits: String,
    exp: i64,
}

mod bigint {
    //! A minimal unsigned big integer (base 10^9 limbs, little-endian) — just the operations
    //! `WideDecimal` needs: parse/print decimal digits, add, subtract, multiply, divide.

    use std::cmp::Ordering;

    const BASE: u64 = 1_000_000_000;

    #[derive(Clone, Debug, PartialEq, Eq)]
    pub(super) struct Big {
        limbs: Vec<u32>,
    }

    impl Big {
        pub(super) fn zero() -> Self {
            Self { limbs: Vec::new() }
        }

        fn trimmed(mut self) -> Self {
            while self.limbs.last() == Some(&0) {
                self.limbs.pop();
            }
            self
        }

        pub(super) fn is_zero(&self) -> bool {
            self.limbs.is_empty()
        }

        /// From ASCII digits (the caller guarantees digits only).
        pub(super) fn parse(s: &str) -> Self {
            let bytes = s.as_bytes();
            let mut limbs = Vec::with_capacity(bytes.len() / 9 + 1);
            let mut end = bytes.len();
            while end > 0 {
                let start = end.saturating_sub(9);
                let mut v: u32 = 0;
                for &b in &bytes[start..end] {
                    v = v * 10 + u32::from(b.saturating_sub(b'0'));
                }
                limbs.push(v);
                end = start;
            }
            Self { limbs }.trimmed()
        }

        pub(super) fn digits(&self) -> String {
            let mut iter = self.limbs.iter().rev();
            let Some(top) = iter.next() else {
                return "0".to_owned();
            };
            let mut s = top.to_string();
            for limb in iter {
                s.push_str(&format!("{limb:09}"));
            }
            s
        }

        pub(super) fn cmp_big(&self, other: &Self) -> Ordering {
            self.limbs.len().cmp(&other.limbs.len()).then_with(|| {
                for (a, b) in self.limbs.iter().rev().zip(other.limbs.iter().rev()) {
                    match a.cmp(b) {
                        Ordering::Equal => {}
                        o => return o,
                    }
                }
                Ordering::Equal
            })
        }

        pub(super) fn add(&self, other: &Self) -> Self {
            let n = self.limbs.len().max(other.limbs.len());
            let mut out = Vec::with_capacity(n + 1);
            let mut carry = 0u64;
            for i in 0..n {
                let a = u64::from(self.limbs.get(i).copied().unwrap_or(0));
                let b = u64::from(other.limbs.get(i).copied().unwrap_or(0));
                let s = a + b + carry;
                out.push(low(s));
                carry = s / BASE;
            }
            if carry > 0 {
                out.push(low(carry));
            }
            Self { limbs: out }.trimmed()
        }

        /// `self - other`, for `self >= other` (the callers order the operands first).
        pub(super) fn sub(&self, other: &Self) -> Self {
            let mut out = Vec::with_capacity(self.limbs.len());
            let mut borrow = 0u64;
            for i in 0..self.limbs.len() {
                let a = u64::from(self.limbs[i]);
                let b = u64::from(other.limbs.get(i).copied().unwrap_or(0)) + borrow;
                if a >= b {
                    out.push(low(a - b));
                    borrow = 0;
                } else {
                    out.push(low(a + BASE - b));
                    borrow = 1;
                }
            }
            Self { limbs: out }.trimmed()
        }

        pub(super) fn mul(&self, other: &Self) -> Self {
            if self.is_zero() || other.is_zero() {
                return Self::zero();
            }
            let mut acc = vec![0u64; self.limbs.len() + other.limbs.len() + 1];
            for (i, &a) in self.limbs.iter().enumerate() {
                let mut carry = 0u64;
                for (j, &b) in other.limbs.iter().enumerate() {
                    let cur = acc[i + j] + u64::from(a) * u64::from(b) + carry;
                    acc[i + j] = cur % BASE;
                    carry = cur / BASE;
                }
                let mut k = i + other.limbs.len();
                while carry > 0 {
                    let cur = acc[k] + carry;
                    acc[k] = cur % BASE;
                    carry = cur / BASE;
                    k += 1;
                }
            }
            Self {
                limbs: acc.into_iter().map(low).collect(),
            }
            .trimmed()
        }

        fn mul_small_add(&self, m: u64, add: u64) -> Self {
            let mut out = Vec::with_capacity(self.limbs.len() + 1);
            let mut carry = add;
            for &a in &self.limbs {
                let cur = u64::from(a) * m + carry;
                out.push(low(cur % BASE));
                carry = cur / BASE;
            }
            while carry > 0 {
                out.push(low(carry % BASE));
                carry /= BASE;
            }
            Self { limbs: out }.trimmed()
        }

        /// `(self // d, self % d)`; `None` when `d` is zero.
        pub(super) fn divmod(&self, d: &Self) -> Option<(Self, Self)> {
            if d.is_zero() {
                return None;
            }
            if self.cmp_big(d) == Ordering::Less {
                return Some((Self::zero(), self.clone()));
            }
            let digits = self.digits();
            let mut q = String::with_capacity(digits.len());
            let mut r = Self::zero();
            for b in digits.bytes() {
                r = r.mul_small_add(10, u64::from(b.saturating_sub(b'0')));
                let mut qd = b'0';
                while r.cmp_big(d) != Ordering::Less {
                    r = r.sub(d);
                    qd += 1;
                }
                q.push(char::from(qd));
            }
            Some((Self::parse(&q), r))
        }

        /// Increment by one.
        pub(super) fn incremented(&self) -> Self {
            self.mul_small_add(1, 1)
        }
    }

    /// The low limb of a value already reduced below `BASE` (or a carry below it).
    fn low(v: u64) -> u32 {
        u32::try_from(v % BASE).unwrap_or(0)
    }
}

use bigint::Big;

fn zeros(n: i64) -> String {
    "0".repeat(usize::try_from(n).unwrap_or(0))
}

fn len_i64(s: &str) -> i64 {
    i64::try_from(s.len()).unwrap_or(i64::MAX)
}

fn strip_leading_zeros(s: &str) -> String {
    let t = s.trim_start_matches('0');
    if t.is_empty() {
        "0".to_owned()
    } else {
        t.to_owned()
    }
}

/// `_all_zeros(digits, prec)`: every digit from `prec` on is zero.
fn all_zeros(digits: &str, prec: usize) -> bool {
    digits
        .as_bytes()
        .get(prec..)
        .is_none_or(|r| r.iter().all(|&b| b == b'0'))
}

/// `_exact_half(digits, prec)`: the digits from `prec` on are exactly `50…0`.
fn exact_half(digits: &str, prec: usize) -> bool {
    let b = digits.as_bytes();
    b.get(prec) == Some(&b'5') && all_zeros(digits, prec + 1)
}

/// Python's `_round_*` helpers: `1` round up, `0` exact, `-1` truncated.
fn round_flag(digits: &str, prec: usize, mode: Rounding) -> i8 {
    let half_up = || {
        if digits
            .as_bytes()
            .get(prec)
            .is_some_and(|b| (b'5'..=b'9').contains(b))
        {
            1
        } else if all_zeros(digits, prec) {
            0
        } else {
            -1
        }
    };
    match mode {
        Rounding::Down => {
            if all_zeros(digits, prec) {
                0
            } else {
                -1
            }
        }
        Rounding::HalfUp => half_up(),
        Rounding::HalfEven => {
            let even_before = prec == 0
                || digits
                    .as_bytes()
                    .get(prec - 1)
                    .is_some_and(|b| b"02468".contains(b));
            if exact_half(digits, prec) && even_before {
                -1
            } else {
                half_up()
            }
        }
    }
}

impl WideDecimal {
    pub(crate) fn triple(neg: bool, digits: &str, exp: i64) -> Self {
        Self {
            neg,
            digits: strip_leading_zeros(digits),
            exp,
        }
    }

    /// Zero (`Decimal(0)`).
    #[must_use]
    pub fn zero() -> Self {
        Self::triple(false, "0", 0)
    }

    /// True for either signed zero.
    #[must_use]
    pub fn is_zero(&self) -> bool {
        self.digits == "0"
    }

    /// True when the sign bit is set (including `-0`).
    #[must_use]
    pub fn is_sign_negative(&self) -> bool {
        self.neg
    }

    /// The exponent (`Decimal("0.10")` has exponent -2).
    #[must_use]
    pub fn exponent(&self) -> i64 {
        self.exp
    }

    /// `self.adjusted()`: the exponent of the most significant digit.
    fn adjusted(&self) -> i64 {
        self.exp + len_i64(&self.digits) - 1
    }

    /// `_fix`: round to the context's 28 significant digits, half-even.
    fn fix(self) -> Self {
        if self.is_zero() {
            return self;
        }
        let len = len_i64(&self.digits);
        if len <= PREC {
            return self;
        }
        let mut exp_min = len + self.exp - PREC;
        let kept = usize::try_from(PREC).unwrap_or(28);
        let changed = round_flag(&self.digits, kept, Rounding::HalfEven);
        let mut coeff = self.digits[..kept].to_owned();
        if changed > 0 {
            coeff = Big::parse(&coeff).incremented().digits();
            if len_i64(&coeff) > PREC {
                coeff.pop();
                exp_min += 1;
            }
        }
        Self::triple(self.neg, &coeff, exp_min)
    }

    /// `_rescale(exp, rounding)`: the same value at exponent `exp`, rounded by `mode`.
    fn rescale(&self, exp: i64, mode: Rounding) -> Self {
        if self.is_zero() {
            return Self::triple(self.neg, "0", exp);
        }
        if self.exp >= exp {
            let mut digits = self.digits.clone();
            digits.push_str(&zeros(self.exp - exp));
            return Self::triple(self.neg, &digits, exp);
        }
        let mut source = self.clone();
        let mut kept = len_i64(&self.digits) + self.exp - exp;
        if kept < 0 {
            source = Self::triple(self.neg, "1", exp - 1);
            kept = 0;
        }
        let kept = usize::try_from(kept).unwrap_or(0);
        let changed = round_flag(&source.digits, kept, mode);
        let mut coeff = source.digits.get(..kept).unwrap_or("").to_owned();
        if coeff.is_empty() {
            coeff = "0".to_owned();
        }
        if changed == 1 {
            coeff = Big::parse(&coeff).incremented().digits();
        }
        Self::triple(self.neg, &coeff, exp)
    }

    /// `self.quantize(Decimal(f"1E{exp}"), rounding=mode)`.
    ///
    /// # Errors
    /// [`DecimalError`] when the result needs more than 28 digits (Python's
    /// `InvalidOperation`).
    pub fn quantize(&self, exp: i64, mode: Rounding) -> Result<Self, DecimalError> {
        let out = self.rescale(exp, mode);
        if len_i64(&out.digits) > PREC {
            return Err(DecimalError(format!(
                "quantize result has more than {PREC} digits: {self}"
            )));
        }
        Ok(out.fix())
    }

    /// `-self` (Python's `__neg__`: a zero comes back positive).
    #[must_use]
    pub fn neg(&self) -> Self {
        let neg = if self.is_zero() { false } else { !self.neg };
        Self::triple(neg, &self.digits, self.exp).fix()
    }

    /// `abs(self)` (Python's `__abs__`: a zero comes back positive).
    #[must_use]
    pub fn abs(&self) -> Self {
        Self::triple(false, &self.digits, self.exp).fix()
    }

    fn copy_negate(&self) -> Self {
        Self::triple(!self.neg, &self.digits, self.exp)
    }

    /// `self + other` in the default context.
    #[must_use]
    pub fn add(&self, other: &Self) -> Self {
        let exp = self.exp.min(other.exp);
        if self.is_zero() && other.is_zero() {
            return Self::triple(self.neg && other.neg, "0", exp).fix();
        }
        if self.is_zero() {
            let e = exp.max(other.exp - PREC - 1);
            return other.rescale(e, Rounding::HalfEven).fix();
        }
        if other.is_zero() {
            let e = exp.max(self.exp - PREC - 1);
            return self.rescale(e, Rounding::HalfEven).fix();
        }
        // _normalize: bring both to one exponent, replacing a negligible operand by 10**exp.
        let (mut tmp, mut oth) = if self.exp < other.exp {
            (other.clone(), self.clone())
        } else {
            (self.clone(), other.clone())
        };
        let tmp_len = len_i64(&tmp.digits);
        let oth_len = len_i64(&oth.digits);
        let floor = tmp.exp + (-1i64).min(tmp_len - PREC - 2);
        if oth_len + oth.exp - 1 < floor {
            oth.digits = "1".to_owned();
            oth.exp = floor;
        }
        tmp.digits.push_str(&zeros(tmp.exp - oth.exp));
        tmp.exp = oth.exp;
        let a = Big::parse(&tmp.digits);
        let b = Big::parse(&oth.digits);
        let common = oth.exp;
        let (neg, coeff) = if tmp.neg == oth.neg {
            (tmp.neg, a.add(&b))
        } else {
            match a.cmp_big(&b) {
                Ordering::Equal => return Self::triple(false, "0", exp).fix(),
                Ordering::Greater => (tmp.neg, a.sub(&b)),
                Ordering::Less => (oth.neg, b.sub(&a)),
            }
        };
        Self::triple(neg, &coeff.digits(), common).fix()
    }

    /// `self - other` in the default context.
    #[must_use]
    pub fn sub(&self, other: &Self) -> Self {
        self.add(&other.copy_negate())
    }

    /// `self * other` in the default context.
    #[must_use]
    pub fn mul(&self, other: &Self) -> Self {
        let neg = self.neg ^ other.neg;
        let exp = self.exp + other.exp;
        if self.is_zero() || other.is_zero() {
            return Self::triple(neg, "0", exp).fix();
        }
        let product = Big::parse(&self.digits).mul(&Big::parse(&other.digits));
        Self::triple(neg, &product.digits(), exp).fix()
    }

    /// `self / other` in the default context (correctly rounded, ideal exponent when exact).
    ///
    /// # Errors
    /// [`DecimalError`] on division by zero (Python's `DivisionByZero` /
    /// `InvalidOperation` for `0 / 0`).
    pub fn div(&self, other: &Self) -> Result<Self, DecimalError> {
        if other.is_zero() {
            return Err(DecimalError(if self.is_zero() {
                "[<class 'decimal.DivisionUndefined'>]".to_owned()
            } else {
                "[<class 'decimal.DivisionByZero'>]".to_owned()
            }));
        }
        let neg = self.neg ^ other.neg;
        if self.is_zero() {
            return Ok(Self::triple(neg, "0", self.exp - other.exp).fix());
        }
        let shift = len_i64(&other.digits) - len_i64(&self.digits) + PREC + 1;
        let mut exp = self.exp - other.exp - shift;
        let (num, den) = if shift >= 0 {
            let mut n = self.digits.clone();
            n.push_str(&zeros(shift));
            (Big::parse(&n), Big::parse(&other.digits))
        } else {
            let mut d = other.digits.clone();
            d.push_str(&zeros(-shift));
            (Big::parse(&self.digits), Big::parse(&d))
        };
        let (coeff, rem) = num
            .divmod(&den)
            .ok_or_else(|| DecimalError("division by zero".to_owned()))?;
        let mut digits = coeff.digits();
        if rem.is_zero() {
            let ideal = self.exp - other.exp;
            while exp < ideal && digits.len() > 1 && digits.ends_with('0') {
                digits.pop();
                exp += 1;
            }
        } else {
            let last = digits.as_bytes().last().copied().unwrap_or(b'0');
            if last == b'0' || last == b'5' {
                digits = Big::parse(&digits).incremented().digits();
            }
        }
        Ok(Self::triple(neg, &digits, exp).fix())
    }

    /// `sum(values, Decimal(0))`, added left to right.
    pub fn sum<'a>(values: impl IntoIterator<Item = &'a Self>) -> Self {
        values.into_iter().fold(Self::zero(), |acc, v| acc.add(v))
    }

    /// `str(d)`: Python's to-scientific-string (`"1E-7"`, `"0E+2"`, `"0.25"`).
    #[must_use]
    pub fn to_sci_string(&self) -> String {
        let len = len_i64(&self.digits);
        let leftdigits = self.exp + len;
        let dotplace = if self.exp <= 0 && leftdigits > -6 {
            leftdigits
        } else {
            1
        };
        let (intpart, fracpart) = if dotplace <= 0 {
            (
                "0".to_owned(),
                format!(".{}{}", zeros(-dotplace), self.digits),
            )
        } else if dotplace >= len {
            (
                format!("{}{}", self.digits, zeros(dotplace - len)),
                String::new(),
            )
        } else {
            let cut = usize::try_from(dotplace).unwrap_or(0);
            (
                self.digits[..cut].to_owned(),
                format!(".{}", &self.digits[cut..]),
            )
        };
        let exp = if leftdigits == dotplace {
            String::new()
        } else {
            format!("E{:+}", leftdigits - dotplace)
        };
        let sign = if self.neg { "-" } else { "" };
        format!("{sign}{intpart}{fracpart}{exp}")
    }

    /// `repr(d)`: `Decimal('0.18')`.
    #[must_use]
    pub fn repr(&self) -> String {
        format!("Decimal('{}')", self.to_sci_string())
    }

    /// The same value as a `rust_decimal::Decimal`, scale kept; `None` when it cannot be held
    /// (scale above 28 or a coefficient above 96 bits).
    #[must_use]
    pub fn to_decimal(&self) -> Option<Decimal> {
        let (digits, scale) = if self.exp >= 0 {
            let mut d = self.digits.clone();
            d.push_str(&zeros(self.exp));
            (d, 0u32)
        } else {
            (self.digits.clone(), u32::try_from(-self.exp).ok()?)
        };
        let mantissa: i128 = digits.parse().ok()?;
        let mut out = Decimal::try_from_i128_with_scale(mantissa, scale).ok()?;
        out.set_sign_negative(self.neg);
        Some(out)
    }

    /// The same value as a `rust_decimal::Decimal`, or a [`DecimalError`] naming it.
    ///
    /// # Errors
    /// When the value does not fit `rust_decimal`.
    pub fn try_to_decimal(&self) -> Result<Decimal, DecimalError> {
        self.to_decimal()
            .ok_or_else(|| DecimalError(format!("{self} does not fit a 96-bit, scale-28 decimal")))
    }
}

impl Default for WideDecimal {
    fn default() -> Self {
        Self::zero()
    }
}

impl From<Decimal> for WideDecimal {
    fn from(d: Decimal) -> Self {
        let mantissa = d.mantissa().unsigned_abs();
        Self::triple(
            d.is_sign_negative(),
            &mantissa.to_string(),
            -i64::from(d.scale()),
        )
    }
}

impl From<i64> for WideDecimal {
    fn from(v: i64) -> Self {
        Self::triple(v < 0, &v.unsigned_abs().to_string(), 0)
    }
}

impl From<usize> for WideDecimal {
    fn from(v: usize) -> Self {
        Self::triple(false, &v.to_string(), 0)
    }
}

impl FromStr for WideDecimal {
    type Err = DecimalError;

    /// Python's `Decimal(str)` for finite numerals: `[sign] digits [. digits] [e exp]`, scale
    /// and signed zero kept exactly.
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let bad = || DecimalError(format!("invalid decimal literal: {s:?}"));
        let t = s.trim();
        let (neg, body) = match t.as_bytes().first() {
            Some(b'-') => (true, &t[1..]),
            Some(b'+') => (false, &t[1..]),
            _ => (false, t),
        };
        let (mantissa, exp_part) = match body.find(['e', 'E']) {
            Some(i) => (&body[..i], Some(&body[i + 1..])),
            None => (body, None),
        };
        let (int, frac) = mantissa.split_once('.').unwrap_or((mantissa, ""));
        if (int.is_empty() && frac.is_empty())
            || !int.bytes().all(|b| b.is_ascii_digit())
            || !frac.bytes().all(|b| b.is_ascii_digit())
        {
            return Err(bad());
        }
        let mut exp = match exp_part {
            Some(e) => e.parse::<i64>().map_err(|_| bad())?,
            None => 0,
        };
        exp -= len_i64(frac);
        Ok(Self::triple(neg, &format!("{int}{frac}"), exp))
    }
}

impl fmt::Display for WideDecimal {
    /// `format(d, "f")` — the golden encoding (`"0.10"`, `"-0.00"`, `"0"` for `0E+2`).
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let d = if self.is_zero() && self.exp > 0 {
            self.rescale(0, Rounding::HalfEven)
        } else {
            self.clone()
        };
        let len = len_i64(&d.digits);
        let dotplace = d.exp + len;
        let (intpart, fracpart) = if dotplace < 0 {
            ("0".to_owned(), format!("{}{}", zeros(-dotplace), d.digits))
        } else if dotplace > len {
            (
                format!("{}{}", d.digits, zeros(dotplace - len)),
                String::new(),
            )
        } else {
            let cut = usize::try_from(dotplace).unwrap_or(0);
            let int = &d.digits[..cut];
            (
                if int.is_empty() {
                    "0".to_owned()
                } else {
                    int.to_owned()
                },
                d.digits[cut..].to_owned(),
            )
        };
        let sign = if d.neg { "-" } else { "" };
        if fracpart.is_empty() {
            write!(f, "{sign}{intpart}")
        } else {
            write!(f, "{sign}{intpart}.{fracpart}")
        }
    }
}

impl PartialEq for WideDecimal {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}

impl Eq for WideDecimal {}

impl PartialOrd for WideDecimal {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for WideDecimal {
    /// Python's `_cmp`: numeric, `0 == -0`.
    fn cmp(&self, other: &Self) -> Ordering {
        let sign = |neg: bool| {
            if neg {
                Ordering::Less
            } else {
                Ordering::Greater
            }
        };
        match (self.is_zero(), other.is_zero()) {
            (true, true) => return Ordering::Equal,
            (true, false) => return sign(other.neg).reverse(),
            (false, true) => return sign(self.neg),
            (false, false) => {}
        }
        if self.neg != other.neg {
            return if self.neg {
                Ordering::Less
            } else {
                Ordering::Greater
            };
        }
        let magnitude = self.adjusted().cmp(&other.adjusted()).then_with(|| {
            let mut a = self.digits.clone();
            a.push_str(&zeros(self.exp - other.exp));
            let mut b = other.digits.clone();
            b.push_str(&zeros(other.exp - self.exp));
            a.cmp(&b)
        });
        if self.neg {
            magnitude.reverse()
        } else {
            magnitude
        }
    }
}

impl Serialize for WideDecimal {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_str(self)
    }
}

impl<'de> Deserialize<'de> for WideDecimal {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct V;
        impl serde::de::Visitor<'_> for V {
            type Value = WideDecimal;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("a decimal string or an integer")
            }
            fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<WideDecimal, E> {
                v.parse().map_err(E::custom)
            }
            fn visit_i64<E: serde::de::Error>(self, v: i64) -> Result<WideDecimal, E> {
                Ok(WideDecimal::from(v))
            }
            fn visit_u64<E: serde::de::Error>(self, v: u64) -> Result<WideDecimal, E> {
                Ok(WideDecimal::triple(false, &v.to_string(), 0))
            }
        }
        deserializer.deserialize_any(V)
    }
}
