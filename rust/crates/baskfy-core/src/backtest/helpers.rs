//! `baskfy_core.backtest` helpers: `month_key`, `month_end`, `year_fraction`, `rebalance_dates`,
//! `shift_schedule`, `raw_weights`, `apply_position_limits`, `CostSpec`, `PricePanel`,
//! `PointInTimeReader`, `AdjustmentCoverage`.
//!
//! Ported from `decile-blueprint/packages/core/src/baskfy_core/backtest.py` and graded by
//! `tests/golden_backtest_helpers.rs` against `go/testdata/golden/L1/backtest`.
//!
//! ## Python's decimal context, reproduced ([`WideDecimal`])
//!
//! Python's `Decimal` arithmetic runs in a context of **28 significant digits**, rounding
//! half-even, with the specification's ideal-exponent rules (an exact quotient keeps the
//! smallest exponent it can, `Decimal(0) / Decimal("365.25")` is `0E+2`). `rust_decimal` caps
//! the *scale* at 28 instead, so a 28-digit quotient below 0.1 (`year_fraction`'s
//! `-0.07939767282683093771389459274`, scale 29) cannot be held, and its division rounds to a
//! different digit count (`1 / 0.18` is `5.555555555555555555555555556` in Python, 28 digits).
//! DECISIONS-RS R1.3 (decided by the lead): such values are reproduced **exactly** by a wider
//! exact decimal scoped to the functions that need it. [`WideDecimal`] is that type: a sign, a
//! decimal coefficient of any length and an exponent, with `add`/`sub`/`mul`/`div`/`quantize`
//! written step for step from CPython's `_pydecimal` (which `libmpdec` agrees with), so every
//! intermediate rounds where Python's rounds. Money that leaves these helpers already quantized
//! (`CostSpec::charge`, the six-place weights) is handed back as `rust_decimal::Decimal`.

use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt;
use std::str::FromStr;

use chrono::{Datelike, NaiveDate, TimeDelta};
use polars::prelude::{DataFrame, DataType, IntoLazy, PolarsError, col, lit};
use rust_decimal::Decimal;
use serde::{Deserialize, Deserializer, Serialize, Serializer};

// =============================================================================================
// Errors
// =============================================================================================

/// What the backtest helpers refuse to do — Python's `BacktestError` family, one variant per
/// exception class, each carrying Python's message verbatim.
#[derive(Debug, thiserror::Error)]
pub enum BacktestError {
    /// `BacktestError`: anything the engine refuses (an unset cursor, a cursor moving back).
    #[error("{0}")]
    Backtest(String),
    /// `BacktestDataError`: the panel cannot answer something asked of it.
    #[error("{0}")]
    Data(String),
    /// `BacktestConfigError`: a configuration the engine cannot execute.
    #[error("{0}")]
    Config(String),
    /// `LookAheadError`: a read carried a date later than the point-in-time cursor.
    #[error(
        "{what} returned a row dated {offending}, which is after the point-in-time cursor {as_of}. This is look-ahead."
    )]
    LookAhead {
        /// What was being read (`"screen result for 2024-01-31"`, `"open price"`).
        what: String,
        /// The offending date.
        offending: NaiveDate,
        /// The cursor it exceeded.
        as_of: NaiveDate,
    },
    /// pydantic's `ValidationError`: a spec outside its declared bounds.
    #[error("{0}")]
    Validation(String),
    /// `decimal.DivisionByZero` / `decimal.InvalidOperation`, or a decimal that does not fit
    /// `rust_decimal` on the way out.
    #[error("{0}")]
    Decimal(String),
    /// An error polars raised while reading a frame (Python would raise polars' own exception).
    #[error(transparent)]
    Polars(#[from] PolarsError),
}

impl BacktestError {
    /// The Python exception class name (`"BacktestDataError"`, `"LookAheadError"`, ...).
    #[must_use]
    pub fn type_name(&self) -> &'static str {
        match self {
            Self::Backtest(_) => "BacktestError",
            Self::Data(_) => "BacktestDataError",
            Self::Config(_) => "BacktestConfigError",
            Self::LookAhead { .. } => "LookAheadError",
            Self::Validation(_) => "ValidationError",
            Self::Decimal(_) => "InvalidOperation",
            Self::Polars(_) => "PolarsError",
        }
    }
}

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
    fn triple(neg: bool, digits: &str, exp: i64) -> Self {
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
    /// [`BacktestError::Decimal`] when the result needs more than 28 digits (Python's
    /// `InvalidOperation`).
    pub fn quantize(&self, exp: i64, mode: Rounding) -> Result<Self, BacktestError> {
        let out = self.rescale(exp, mode);
        if len_i64(&out.digits) > PREC {
            return Err(BacktestError::Decimal(format!(
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
    /// [`BacktestError::Decimal`] on division by zero (Python's `DivisionByZero` /
    /// `InvalidOperation` for `0 / 0`).
    pub fn div(&self, other: &Self) -> Result<Self, BacktestError> {
        if other.is_zero() {
            return Err(BacktestError::Decimal(if self.is_zero() {
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
            .ok_or_else(|| BacktestError::Decimal("division by zero".to_owned()))?;
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

    /// The same value as a `rust_decimal::Decimal`, or a [`BacktestError::Decimal`] naming it.
    ///
    /// # Errors
    /// When the value does not fit `rust_decimal`.
    pub fn try_to_decimal(&self) -> Result<Decimal, BacktestError> {
        self.to_decimal().ok_or_else(|| {
            BacktestError::Decimal(format!("{self} does not fit a 96-bit, scale-28 decimal"))
        })
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
    type Err = BacktestError;

    /// Python's `Decimal(str)` for finite numerals: `[sign] digits [. digits] [e exp]`, scale
    /// and signed zero kept exactly.
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let bad = || BacktestError::Decimal(format!("invalid decimal literal: {s:?}"));
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

// =============================================================================================
// Constants
// =============================================================================================

/// `PRICE_EXPONENT = Decimal("0.0001")`: the exponent every price sits on.
pub const PRICE_EXPONENT: i64 = -4;
/// `MONEY_EXPONENT = Decimal("0.01")`: rupees to the paisa.
pub const MONEY_EXPONENT: i64 = -2;
/// `WEIGHT_EXPONENT = Decimal("0.000001")`: weights to six places.
pub const WEIGHT_EXPONENT: i64 = -6;
/// `MISSING_BAR_TOLERANCE_DAYS`: consecutive bar-less trading days before a name is dead.
pub const MISSING_BAR_TOLERANCE_DAYS: usize = 5;

/// `BASIS_POINTS = Decimal(10_000)`.
#[must_use]
pub fn basis_points() -> WideDecimal {
    WideDecimal::from(10_000_i64)
}

/// `_DAYS_PER_YEAR = Decimal("365.25")`.
fn days_per_year() -> WideDecimal {
    WideDecimal::triple(false, "36525", -2)
}

/// `_money(value)`: quantize to the paisa, half-up.
///
/// # Errors
/// Python's `InvalidOperation` when the value needs more than 28 digits.
pub fn money(value: &WideDecimal) -> Result<Decimal, BacktestError> {
    value
        .quantize(MONEY_EXPONENT, Rounding::HalfUp)?
        .try_to_decimal()
}

// =============================================================================================
// Configuration blocks
// =============================================================================================

/// `RebalanceFrequency` (docs/10): `weekly|fortnightly|monthly|quarterly`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RebalanceFrequency {
    /// One rebalance per ISO week.
    Weekly,
    /// One per pair of ISO weeks (`iso.week // 2`).
    Fortnightly,
    /// One per calendar month (the default).
    #[default]
    Monthly,
    /// One per calendar quarter.
    Quarterly,
}

/// `RebalanceDay`: which trading day of each period.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RebalanceDay {
    /// The period's last trading day (the default).
    #[default]
    LastTradingDay,
    /// The period's first trading day.
    FirstTradingDay,
}

/// `RebalanceSpec`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RebalanceSpec {
    /// How often.
    #[serde(default)]
    pub frequency: RebalanceFrequency,
    /// Which day of the period.
    #[serde(default)]
    pub day: RebalanceDay,
}

/// `Weighting` (docs/10): `equal | inverse_volatility | rank | marketcap`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Weighting {
    /// One each (the default).
    #[default]
    Equal,
    /// `1 / vol_12m`.
    InverseVolatility,
    /// `n .. 1` by rank within the selected set.
    Rank,
    /// The market cap itself.
    Marketcap,
}

/// `ImpactModel`: only the one docs/10 names.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ImpactModel {
    /// `"fixed"`.
    #[default]
    Fixed,
}

fn default_max_weight() -> Decimal {
    Decimal::new(10, 2)
}

fn default_min_weight() -> Decimal {
    Decimal::new(1, 2)
}

/// `PositionLimits` (docs/10): `{"max_weight": 0.10, "min_weight": 0.01}`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PositionLimits {
    /// Ceiling per name, `0 < max_weight <= 1`.
    #[serde(default = "default_max_weight")]
    pub max_weight: Decimal,
    /// Floor per name, `0 <= min_weight <= 1`, not above `max_weight`.
    #[serde(default = "default_min_weight")]
    pub min_weight: Decimal,
}

impl Default for PositionLimits {
    fn default() -> Self {
        Self {
            max_weight: default_max_weight(),
            min_weight: default_min_weight(),
        }
    }
}

impl PositionLimits {
    /// pydantic's field bounds and `_ordered` validator.
    ///
    /// # Errors
    /// [`BacktestError::Validation`] naming the violated bound.
    pub fn validate(&self) -> Result<(), BacktestError> {
        if self.max_weight <= Decimal::ZERO || self.max_weight > Decimal::ONE {
            return Err(BacktestError::Validation(format!(
                "max_weight {} must be > 0 and <= 1",
                self.max_weight
            )));
        }
        if self.min_weight < Decimal::ZERO || self.min_weight > Decimal::ONE {
            return Err(BacktestError::Validation(format!(
                "min_weight {} must be >= 0 and <= 1",
                self.min_weight
            )));
        }
        if self.min_weight > self.max_weight {
            return Err(BacktestError::Validation(format!(
                "min_weight {} is above max_weight {}",
                self.min_weight, self.max_weight
            )));
        }
        Ok(())
    }
}

fn default_brokerage_bps() -> Decimal {
    Decimal::from(3)
}

fn default_stt_bps() -> Decimal {
    Decimal::from(10)
}

fn default_slippage_bps() -> Decimal {
    Decimal::from(15)
}

/// `CostSpec` (docs/10): brokerage 3 bps, STT 10 bps, slippage 15 bps, charged on both sides.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CostSpec {
    /// Brokerage, basis points, `0..=1000`.
    #[serde(default = "default_brokerage_bps")]
    pub brokerage_bps: Decimal,
    /// STT, basis points, `0..=1000`.
    #[serde(default = "default_stt_bps")]
    pub stt_bps: Decimal,
    /// Slippage, basis points, `0..=1000`.
    #[serde(default = "default_slippage_bps")]
    pub slippage_bps: Decimal,
    /// Always `fixed`.
    #[serde(default)]
    pub impact_model: ImpactModel,
}

impl Default for CostSpec {
    fn default() -> Self {
        Self {
            brokerage_bps: default_brokerage_bps(),
            stt_bps: default_stt_bps(),
            slippage_bps: default_slippage_bps(),
            impact_model: ImpactModel::Fixed,
        }
    }
}

impl CostSpec {
    /// pydantic's `ge=0, le=1000` on each rate.
    ///
    /// # Errors
    /// [`BacktestError::Validation`] naming the field out of bounds.
    pub fn validate(&self) -> Result<(), BacktestError> {
        let thousand = Decimal::from(1000);
        for (name, v) in [
            ("brokerage_bps", self.brokerage_bps),
            ("stt_bps", self.stt_bps),
            ("slippage_bps", self.slippage_bps),
        ] {
            if v < Decimal::ZERO || v > thousand {
                return Err(BacktestError::Validation(format!(
                    "{name} {v} must be >= 0 and <= 1000"
                )));
            }
        }
        Ok(())
    }

    /// `total_bps`: brokerage + STT + slippage, added in Python's context.
    #[must_use]
    pub fn total_bps(&self) -> WideDecimal {
        WideDecimal::from(self.brokerage_bps)
            .add(&WideDecimal::from(self.stt_bps))
            .add(&WideDecimal::from(self.slippage_bps))
    }

    /// `scaled(factor)`: every rate multiplied — docs/10's ±25 % cost probe. Validated like
    /// Python's constructor, so a rate pushed past 1000 bps is refused.
    ///
    /// # Errors
    /// [`BacktestError::Validation`] for a rate out of bounds; [`BacktestError::Decimal`] if a
    /// product does not fit `rust_decimal`.
    pub fn scaled(&self, factor: Decimal) -> Result<Self, BacktestError> {
        let f = WideDecimal::from(factor);
        let times = |v: Decimal| WideDecimal::from(v).mul(&f).try_to_decimal();
        let out = Self {
            brokerage_bps: times(self.brokerage_bps)?,
            stt_bps: times(self.stt_bps)?,
            slippage_bps: times(self.slippage_bps)?,
            impact_model: self.impact_model,
        };
        out.validate()?;
        Ok(out)
    }

    /// `charge(notional)`: `abs(notional) * total_bps / 10_000`, half-up to the paisa.
    ///
    /// # Errors
    /// Python's `InvalidOperation` when the cost needs more than 28 digits.
    pub fn charge(&self, notional: Decimal) -> Result<Decimal, BacktestError> {
        let gross = WideDecimal::from(notional).abs().mul(&self.total_bps());
        money(&gross.div(&basis_points())?)
    }
}

// =============================================================================================
// Calendar helpers
// =============================================================================================

/// `year_fraction(start, end)`: calendar years between two dates, `days / 365.25`, in Python's
/// 28-digit context (so a span under 0.1 years prints at scale 29, R1.3).
#[must_use]
pub fn year_fraction(start: NaiveDate, end: NaiveDate) -> WideDecimal {
    let days = (end - start).num_days();
    // The divisor is the nonzero constant 365.25, so `div` cannot return its zero-divisor error.
    WideDecimal::from(days)
        .div(&days_per_year())
        .unwrap_or_default()
}

/// `month_key(day)`: `"YYYY-MM"`.
#[must_use]
pub fn month_key(day: NaiveDate) -> String {
    format!("{:04}-{:02}", day.year(), day.month())
}

fn is_leap(year: i32) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

/// `month_end(day)`: the last calendar day of `day`'s month.
#[must_use]
pub fn month_end(day: NaiveDate) -> NaiveDate {
    let last = match day.month() {
        4 | 6 | 9 | 11 => 30,
        2 if is_leap(day.year()) => 29,
        2 => 28,
        _ => 31,
    };
    // `last` is that month's length, so `with_day` always succeeds.
    day.with_day(last).unwrap_or(day)
}

fn period_key(day: NaiveDate, frequency: RebalanceFrequency) -> (i32, u32) {
    match frequency {
        RebalanceFrequency::Monthly => (day.year(), day.month()),
        RebalanceFrequency::Quarterly => (day.year(), (day.month() - 1) / 3),
        RebalanceFrequency::Weekly => {
            let iso = day.iso_week();
            (iso.year(), iso.week())
        }
        RebalanceFrequency::Fortnightly => {
            let iso = day.iso_week();
            (iso.year(), iso.week() / 2)
        }
    }
}

/// `rebalance_dates(calendar, start, end, spec)`: the screen dates inside `[start, end]`, the
/// first trading day on or after `start` always among them; sorted, unique.
#[must_use]
pub fn rebalance_dates(
    calendar: &[NaiveDate],
    start: NaiveDate,
    end: NaiveDate,
    spec: &RebalanceSpec,
) -> Vec<NaiveDate> {
    let window: Vec<NaiveDate> = calendar
        .iter()
        .copied()
        .filter(|d| start <= *d && *d <= end)
        .collect();
    let Some(&first) = window.first() else {
        return Vec::new();
    };
    let mut chosen: HashMap<(i32, u32), NaiveDate> = HashMap::new();
    for &day in &window {
        let key = period_key(day, spec.frequency);
        match spec.day {
            RebalanceDay::LastTradingDay => {
                chosen.insert(key, day);
            }
            RebalanceDay::FirstTradingDay => {
                chosen.entry(key).or_insert(day);
            }
        }
    }
    let mut dates: BTreeSet<NaiveDate> = chosen.into_values().collect();
    dates.insert(first);
    dates.into_iter().collect()
}

/// `shift_schedule(calendar, dates, offset)`: each date moved `offset` trading days; dates not
/// in the calendar or pushed off either end are dropped; sorted, unique.
#[must_use]
pub fn shift_schedule(calendar: &[NaiveDate], dates: &[NaiveDate], offset: i64) -> Vec<NaiveDate> {
    let mut index: HashMap<NaiveDate, usize> = HashMap::new();
    for (position, day) in calendar.iter().enumerate() {
        index.insert(*day, position);
    }
    let mut moved: BTreeSet<NaiveDate> = BTreeSet::new();
    for day in dates {
        let Some(&position) = index.get(day) else {
            continue;
        };
        let target = i64::try_from(position)
            .unwrap_or(i64::MAX)
            .saturating_add(offset);
        if let Some(d) = usize::try_from(target).ok().and_then(|t| calendar.get(t)) {
            moved.insert(*d);
        }
    }
    moved.into_iter().collect()
}

// =============================================================================================
// Weighting
// =============================================================================================

/// `_Selected`: one name the screen selected at a rebalance date, with what weighting needs.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Selected {
    /// `instrument.id`.
    pub instrument_id: i64,
    /// Trading symbol.
    pub symbol: String,
    /// Display name.
    pub name: String,
    /// The screen's rank (1 = best).
    pub rank: i64,
    /// `marketcap_cr`, when the screen projected it.
    #[serde(default)]
    pub marketcap_cr: Option<WideDecimal>,
    /// `vol_12m`, when the screen projected it.
    #[serde(default)]
    pub volatility: Option<WideDecimal>,
}

/// Insert into an insertion-ordered map the way a Python dict assignment does: an existing key
/// keeps its position and takes the new value.
fn dict_set<K: PartialEq + Copy, V>(map: &mut Vec<(K, V)>, key: K, value: V) {
    if let Some(slot) = map.iter_mut().find(|(k, _)| *k == key) {
        slot.1 = value;
    } else {
        map.push((key, value));
    }
}

fn positive(
    value: Option<&WideDecimal>,
    row: &Selected,
    column: &str,
) -> Result<WideDecimal, BacktestError> {
    match value {
        Some(v) if *v > WideDecimal::zero() => Ok(v.clone()),
        other => {
            let shown = other.map_or_else(|| "None".to_owned(), WideDecimal::repr);
            Err(BacktestError::Data(format!(
                "{column} is {shown} for instrument {} on rank {}; this weighting scheme cannot \
                 use it. Choose 'equal' or fix the screen's projection.",
                row.instrument_id, row.rank
            )))
        }
    }
}

/// `raw_weights(scheme, rows)`: the pre-clip weights, in Python's dict order (first insertion
/// position of each id).
///
/// # Errors
/// [`BacktestError::Data`] for a missing or non-positive market cap / volatility.
pub fn raw_weights(
    scheme: Weighting,
    rows: &[Selected],
) -> Result<Vec<(i64, WideDecimal)>, BacktestError> {
    let mut out: Vec<(i64, WideDecimal)> = Vec::with_capacity(rows.len());
    match scheme {
        Weighting::Equal => {
            for row in rows {
                dict_set(&mut out, row.instrument_id, WideDecimal::from(1_i64));
            }
        }
        Weighting::Rank => {
            let mut order: Vec<&Selected> = rows.iter().collect();
            order.sort_by(|a, b| {
                a.rank
                    .cmp(&b.rank)
                    .then_with(|| a.instrument_id.cmp(&b.instrument_id))
            });
            let count = order.len();
            for (position, row) in order.iter().enumerate() {
                dict_set(
                    &mut out,
                    row.instrument_id,
                    WideDecimal::from(count - position),
                );
            }
        }
        Weighting::Marketcap => {
            for row in rows {
                let v = positive(row.marketcap_cr.as_ref(), row, "marketcap_cr")?;
                dict_set(&mut out, row.instrument_id, v);
            }
        }
        Weighting::InverseVolatility => {
            let mut vols: Vec<(i64, WideDecimal)> = Vec::with_capacity(rows.len());
            for row in rows {
                let v = positive(row.volatility.as_ref(), row, "vol_12m")?;
                dict_set(&mut vols, row.instrument_id, v);
            }
            let one = WideDecimal::from(1_i64);
            for (key, vol) in vols {
                out.push((key, one.div(&vol)?));
            }
        }
    }
    Ok(out)
}

/// `apply_position_limits(weights, limits)`: clip to `[min_weight, max_weight]` and renormalise
/// to exactly 1 by water-filling, ceilings before floors; an infeasible bound is relaxed to
/// `1/n`; quantized down to six places with the remainder on the largest name that has room
/// under the ceiling (lowest id on ties).
///
/// Python returns a dict whose order comes partly from a `set`; the result here is keyed in
/// ascending id order. `weights` is read as a Python mapping: a repeated id keeps its first
/// position and its last value.
///
/// # Errors
/// [`BacktestError::Decimal`] only if a weight cannot be quantized (Python's
/// `InvalidOperation`), which needs a weight of 10^22 or more.
pub fn apply_position_limits(
    weights: &[(i64, WideDecimal)],
    limits: &PositionLimits,
) -> Result<BTreeMap<i64, Decimal>, BacktestError> {
    let mut free: Vec<(i64, WideDecimal)> = Vec::with_capacity(weights.len());
    for (k, v) in weights {
        dict_set(&mut free, *k, v.clone());
    }
    if free.is_empty() {
        return Ok(BTreeMap::new());
    }
    let count = free.len();
    let one = WideDecimal::from(1_i64);
    let even = one.div(&WideDecimal::from(count))?;
    let max_w = WideDecimal::from(limits.max_weight);
    let min_w = WideDecimal::from(limits.min_weight);
    // Python's max(a, b) / min(a, b) keep the first argument on a tie.
    let ceiling = if even > max_w { even.clone() } else { max_w };
    let floor = if even < min_w { even } else { min_w };

    let mut fixed: Vec<(i64, WideDecimal)> = Vec::new();
    for _ in 0..=count {
        if free.is_empty() {
            return normalised(&fixed, &ceiling);
        }
        let remaining = one.sub(&WideDecimal::sum(fixed.iter().map(|(_, v)| v)));
        let total = WideDecimal::sum(free.iter().map(|(_, v)| v));
        let scaled: Vec<(i64, WideDecimal)> = if total <= WideDecimal::zero() {
            let share = remaining.div(&WideDecimal::from(free.len()))?;
            free.iter().map(|(k, _)| (*k, share.clone())).collect()
        } else {
            free.iter()
                .map(|(k, v)| Ok((*k, v.div(&total)?.mul(&remaining))))
                .collect::<Result<_, BacktestError>>()?
        };

        let above: Vec<i64> = scaled
            .iter()
            .filter(|(_, v)| *v > ceiling)
            .map(|(k, _)| *k)
            .collect();
        if !above.is_empty() {
            for k in &above {
                dict_set(&mut fixed, *k, ceiling.clone());
            }
            free.retain(|(k, _)| !above.contains(k));
            continue;
        }
        let below: Vec<i64> = scaled
            .iter()
            .filter(|(_, v)| *v < floor)
            .map(|(k, _)| *k)
            .collect();
        if !below.is_empty() {
            for k in &below {
                dict_set(&mut fixed, *k, floor.clone());
            }
            free.retain(|(k, _)| !below.contains(k));
            continue;
        }
        let mut merged = fixed;
        merged.extend(scaled);
        return normalised(&merged, &ceiling);
    }
    // The loop converges (every pass fixes at least one name); kept for parity with Python.
    let mut merged = fixed;
    merged.extend(free);
    normalised(&merged, &ceiling)
}

/// `_normalised`: quantize to six places (down) and place the remainder.
fn normalised(
    weights: &[(i64, WideDecimal)],
    ceiling: &WideDecimal,
) -> Result<BTreeMap<i64, Decimal>, BacktestError> {
    if weights.is_empty() {
        return Ok(BTreeMap::new());
    }
    let mut rounded: Vec<(i64, WideDecimal)> = Vec::with_capacity(weights.len());
    for (k, v) in weights {
        dict_set(
            &mut rounded,
            *k,
            v.quantize(WEIGHT_EXPONENT, Rounding::Down)?,
        );
    }
    let remainder = WideDecimal::from(1_i64).sub(&WideDecimal::sum(rounded.iter().map(|(_, v)| v)));
    if !remainder.is_zero() {
        // max(..., key=(rounded[key], -key)): the largest weight, the lowest id on a tie.
        let better = |a: &(i64, WideDecimal), b: &(i64, WideDecimal)| {
            a.1.cmp(&b.1).then_with(|| b.0.cmp(&a.0))
        };
        let room: Vec<&(i64, WideDecimal)> = rounded
            .iter()
            .filter(|(_, v)| v.add(&remainder) <= *ceiling)
            .collect();
        let anchor = if room.is_empty() {
            rounded.iter().max_by(|a, b| better(a, b)).map(|e| e.0)
        } else {
            room.into_iter().max_by(|a, b| better(a, b)).map(|e| e.0)
        };
        if let Some(anchor) = anchor
            && let Some(slot) = rounded.iter_mut().find(|(k, _)| *k == anchor)
        {
            slot.1 = slot.1.add(&remainder);
        }
    }
    rounded
        .into_iter()
        .map(|(k, v)| Ok((k, v.try_to_decimal()?)))
        .collect()
}

// =============================================================================================
// The price panel
// =============================================================================================

/// `REQUIRED_PRICE_COLUMNS`.
pub const REQUIRED_PRICE_COLUMNS: [&str; 4] = ["date", "instrument_id", "open", "close"];

/// Python's `repr()` of a `str` (single quotes unless the text holds one and no double quote).
fn py_str_repr(s: &str) -> String {
    if s.contains('\'') && !s.contains('"') {
        format!("\"{}\"", s.replace('\\', "\\\\"))
    } else {
        format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}

fn py_list_repr(items: &[&str]) -> String {
    let inner: Vec<String> = items.iter().map(|s| py_str_repr(s)).collect();
    format!("[{}]", inner.join(", "))
}

/// `_price(value)`: snap a float64 lookup back onto the four-decimal grid,
/// `Decimal(f"{value:.4f}")`.
///
/// # Errors
/// [`BacktestError::Decimal`] for a non-finite value or one beyond `rust_decimal`'s range
/// (Python would hold `Decimal("inf")`; no stored price is either).
pub fn price_from_float(value: f64) -> Result<Decimal, BacktestError> {
    let text = format!("{value:.4}");
    Decimal::from_str_exact(&text)
        .map_err(|e| BacktestError::Decimal(format!("price {text} is not representable: {e}")))
}

fn has_column(frame: &DataFrame, name: &str) -> bool {
    frame.get_column_names().iter().any(|c| c.as_str() == name)
}

fn days_to_date(days: i32) -> Option<NaiveDate> {
    NaiveDate::default().checked_add_signed(TimeDelta::days(i64::from(days)))
}

/// `PricePanel`: adjusted daily bars pivoted into dense `(day, instrument)` arrays. Missing
/// bars are NaN — never zero, never forward-filled.
#[derive(Debug, Clone)]
pub struct PricePanel {
    is_price_only: bool,
    calendar: Vec<NaiveDate>,
    day_index: HashMap<NaiveDate, usize>,
    instrument_ids: Vec<i64>,
    column: HashMap<i64, usize>,
    /// Row-major `[day][instrument]`.
    open: Vec<f64>,
    close: Vec<f64>,
    /// The last day each series prints a close, `None` when it never does.
    last_row: Vec<Option<usize>>,
}

impl PricePanel {
    /// `PricePanel(frame, calendar, price_only=...)`. Reads `open`/`close`, or
    /// `price_open`/`price_close` when `price_only`.
    ///
    /// # Errors
    /// [`BacktestError::Data`] for a missing required column, a null `instrument_id`, or bars
    /// on dates outside the calendar; [`BacktestError::Polars`] for an absent or uncastable
    /// price column (Python raises polars' own error there).
    pub fn new(
        frame: &DataFrame,
        calendar: &[NaiveDate],
        price_only: bool,
    ) -> Result<Self, BacktestError> {
        let missing: Vec<&str> = REQUIRED_PRICE_COLUMNS
            .iter()
            .copied()
            .filter(|c| !has_column(frame, c))
            .collect();
        if !missing.is_empty() {
            return Err(BacktestError::Data(format!(
                "the price panel is missing column(s) {}",
                py_list_repr(&missing)
            )));
        }
        let calendar = calendar.to_vec();
        let mut day_index = HashMap::with_capacity(calendar.len());
        for (position, day) in calendar.iter().enumerate() {
            day_index.insert(*day, position);
        }

        let ids_col = frame
            .column("instrument_id")?
            .strict_cast(&DataType::Int64)?;
        let ids: Vec<Option<i64>> = ids_col.i64()?.iter().collect();
        if ids.iter().any(Option::is_none) {
            // Python: int(None) raises TypeError while listing the instruments.
            return Err(BacktestError::Data(
                "instrument_id holds a null, which is not an instrument".to_owned(),
            ));
        }
        let unique: BTreeSet<i64> = ids.iter().flatten().copied().collect();
        let instrument_ids: Vec<i64> = unique.into_iter().collect();
        let column: HashMap<i64, usize> = instrument_ids
            .iter()
            .enumerate()
            .map(|(p, id)| (*id, p))
            .collect();

        let (open_name, close_name) = if price_only {
            ("price_open", "price_close")
        } else {
            ("open", "close")
        };
        let opens: Vec<Option<f64>> = frame
            .column(open_name)?
            .strict_cast(&DataType::Float64)?
            .f64()?
            .iter()
            .collect();
        let closes: Vec<Option<f64>> = frame
            .column(close_name)?
            .strict_cast(&DataType::Float64)?
            .f64()?
            .iter()
            .collect();

        let width = instrument_ids.len();
        let cells = calendar.len() * width;
        let mut open = vec![f64::NAN; cells];
        let mut close = vec![f64::NAN; cells];
        if frame.height() > 0 {
            let date_col = frame.column("date")?;
            let positions: Vec<Option<usize>> = if date_col.dtype() == &DataType::Date {
                date_col
                    .date()?
                    .physical()
                    .iter()
                    .map(|d| {
                        d.and_then(days_to_date)
                            .and_then(|day| day_index.get(&day).copied())
                    })
                    .collect()
            } else {
                // Python looks each value up in a dict keyed by `date`; a datetime, a string or a
                // null is never found.
                vec![None; frame.height()]
            };
            let unknown = positions.iter().filter(|p| p.is_none()).count();
            if unknown > 0 {
                return Err(BacktestError::Data(format!(
                    "{unknown} bar(s) fall on dates that are not in the trading calendar"
                )));
            }
            for (row, day) in positions.iter().enumerate() {
                let (Some(day), Some(Some(id))) = (day, ids.get(row)) else {
                    continue;
                };
                let Some(&c) = column.get(id) else {
                    continue;
                };
                let cell = day * width + c;
                open[cell] = opens.get(row).copied().flatten().unwrap_or(f64::NAN);
                close[cell] = closes.get(row).copied().flatten().unwrap_or(f64::NAN);
            }
        }

        let last_row = (0..width)
            .map(|c| {
                (0..calendar.len())
                    .rev()
                    .find(|d| !close[d * width + c].is_nan())
            })
            .collect();

        Ok(Self {
            is_price_only: price_only,
            calendar,
            day_index,
            instrument_ids,
            column,
            open,
            close,
            last_row,
        })
    }

    /// Whether the panel read the raw `price_*` pair (kept for callers; gates nothing).
    #[must_use]
    pub fn is_price_only(&self) -> bool {
        self.is_price_only
    }

    /// The trading calendar the panel is indexed by.
    #[must_use]
    pub fn calendar(&self) -> &[NaiveDate] {
        &self.calendar
    }

    /// Every instrument with at least one bar, ascending.
    #[must_use]
    pub fn instrument_ids(&self) -> &[i64] {
        &self.instrument_ids
    }

    /// `index_of(day)`: the day's position in the calendar.
    ///
    /// # Errors
    /// [`BacktestError::Data`] when `day` is not a trading day of this calendar.
    pub fn index_of(&self, day: NaiveDate) -> Result<usize, BacktestError> {
        self.day_index.get(&day).copied().ok_or_else(|| {
            BacktestError::Data(format!("{day} is not a trading day in this calendar"))
        })
    }

    fn cell(
        &self,
        values: &[f64],
        instrument_id: i64,
        day_position: usize,
    ) -> Result<Option<Decimal>, BacktestError> {
        let Some(&c) = self.column.get(&instrument_id) else {
            return Ok(None);
        };
        if day_position >= self.calendar.len() {
            // Python: numpy's IndexError.
            return Err(BacktestError::Data(format!(
                "day position {day_position} is outside a calendar of {} days",
                self.calendar.len()
            )));
        }
        let v = values[day_position * self.instrument_ids.len() + c];
        if v.is_nan() {
            Ok(None)
        } else {
            price_from_float(v).map(Some)
        }
    }

    /// `close_on(instrument_id, day_position)`: the close, `None` for a missing bar or an
    /// unknown instrument.
    ///
    /// # Errors
    /// A position past the calendar (Python's `IndexError`), or an unrepresentable price.
    pub fn close_on(
        &self,
        instrument_id: i64,
        day_position: usize,
    ) -> Result<Option<Decimal>, BacktestError> {
        self.cell(&self.close, instrument_id, day_position)
    }

    /// `open_on(instrument_id, day_position)`: the open, `None` for a missing bar or an
    /// unknown instrument.
    ///
    /// # Errors
    /// A position past the calendar (Python's `IndexError`), or an unrepresentable price.
    pub fn open_on(
        &self,
        instrument_id: i64,
        day_position: usize,
    ) -> Result<Option<Decimal>, BacktestError> {
        self.cell(&self.open, instrument_id, day_position)
    }

    /// `last_close_at_or_before(instrument_id, day_position)`: the most recent printed close
    /// on or before the position, and its position (a position past the end reads the whole
    /// series, as numpy's slice does).
    ///
    /// # Errors
    /// An unrepresentable price.
    pub fn last_close_at_or_before(
        &self,
        instrument_id: i64,
        day_position: usize,
    ) -> Result<Option<(Decimal, usize)>, BacktestError> {
        let Some(&c) = self.column.get(&instrument_id) else {
            return Ok(None);
        };
        let width = self.instrument_ids.len();
        let upto = day_position.min(self.calendar.len().saturating_sub(1));
        if self.calendar.is_empty() {
            return Ok(None);
        }
        for d in (0..=upto).rev() {
            let v = self.close[d * width + c];
            if !v.is_nan() {
                return Ok(Some((price_from_float(v)?, d)));
            }
        }
        Ok(None)
    }

    /// `liquidation_day(instrument_id, delisted_on)`: the day index a held position must be
    /// closed — the first trading day on or after `delisted_on`, or the day after
    /// [`MISSING_BAR_TOLERANCE_DAYS`] bar-less days, whichever is first (it may be one past
    /// the calendar).
    #[must_use]
    pub fn liquidation_day(
        &self,
        instrument_id: i64,
        delisted_on: Option<NaiveDate>,
    ) -> Option<usize> {
        let mut candidates: Vec<usize> = Vec::new();
        if let Some(delisted) = delisted_on
            && let Some(p) = self.calendar.iter().position(|d| *d >= delisted)
        {
            candidates.push(p);
        }
        if let Some(&c) = self.column.get(&instrument_id)
            && let Some(last) = self.last_row.get(c).copied().flatten()
            && last + MISSING_BAR_TOLERANCE_DAYS < self.calendar.len()
        {
            candidates.push(last + MISSING_BAR_TOLERANCE_DAYS + 1);
        }
        candidates.into_iter().min()
    }
}

// =============================================================================================
// BacktestData and the point-in-time guard
// =============================================================================================

/// `AdjustmentCoverage`: what the adjusted price series a run read can and cannot support.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct AdjustmentCoverage {
    /// Earliest corporate action on record, `None` when there are none.
    pub actions_from: Option<NaiveDate>,
    /// Latest corporate action on record.
    pub actions_to: Option<NaiveDate>,
    /// First date of the segment we adjust ourselves (`source='nse'`).
    #[serde(default)]
    pub self_adjusted_from: Option<NaiveDate>,
    /// The date the two adjustment conventions meet.
    #[serde(default)]
    pub splice_on: Option<NaiveDate>,
    /// The stored series carries every action known at load time.
    #[serde(default = "default_true")]
    pub as_of_today: bool,
}

fn default_true() -> bool {
    true
}

impl AdjustmentCoverage {
    /// `caveats(start, end)`: the sentences a run over `[start, end]` has to carry.
    #[must_use]
    pub fn caveats(&self, start: NaiveDate, end: NaiveDate) -> Vec<String> {
        let mut notes = Vec::new();
        let exposed_from = self.self_adjusted_from.map_or(start, |s| start.max(s));
        let exposed = self.self_adjusted_from.is_none_or(|s| s <= end);
        if exposed {
            match self.actions_from {
                None => notes.push(
                    "No corporate actions are on record, so the prices adjusted here are \
                     unadjusted in fact. Any split or bonus reads as a price move."
                        .to_owned(),
                ),
                Some(actions_from) if exposed_from < actions_from => {
                    let days = (actions_from - exposed_from).num_days();
                    let plural = if days == 1 { "" } else { "s" };
                    notes.push(format!(
                        "Corporate actions are on record only from {actions_from}, and prices \
                         adjusted here begin {exposed_from} — a gap of {days} day{plural}. \
                         Splits and bonuses inside it are not applied."
                    ));
                }
                Some(_) => {}
            }
        }
        if let Some(splice) = self.splice_on
            && start < splice
        {
            notes.push(format!(
                "Prices before {splice} come from a different source and are adjusted for \
                 dividends as well as splits, while prices after it are not. Returns measured \
                 before that date are higher than the later convention would give, by roughly \
                 the dividend yield."
            ));
        }
        if self.as_of_today {
            notes.push(
                "Prices are adjusted as of the day this ran, not as of each simulated day, so a \
                 corporate action is reflected in bars that predate it."
                    .to_owned(),
            );
        }
        notes
    }
}

/// `BacktestData`: everything the engine reads, assembled by the caller.
#[derive(Debug, Clone)]
pub struct BacktestData {
    /// The trading calendar.
    pub calendar: Vec<NaiveDate>,
    /// The price panel.
    pub prices: PricePanel,
    /// Each rebalance date's screen output; every frame must carry a `date` column.
    pub screens: BTreeMap<NaiveDate, DataFrame>,
    /// `date`, `level` — the benchmark, if any.
    pub benchmark: Option<DataFrame>,
    /// `instrument_id` -> `instrument.delisted_on`.
    pub delistings: HashMap<i64, NaiveDate>,
    /// `instrument_id` -> symbol.
    pub symbols: HashMap<i64, String>,
    /// `instrument_id` -> name.
    pub names: HashMap<i64, String>,
    /// `date`, `instrument_id`, `amount` — per-share cash dividends, if any.
    pub dividends: Option<DataFrame>,
    /// The published `pipeline_run.data_version` behind the panel.
    pub data_version: Option<i64>,
    /// What the adjusted price panel can and cannot support.
    pub adjustment_coverage: Option<AdjustmentCoverage>,
}

impl BacktestData {
    /// `symbol_for(id)`: the symbol, or `#<id>`.
    #[must_use]
    pub fn symbol_for(&self, instrument_id: i64) -> String {
        self.symbols
            .get(&instrument_id)
            .cloned()
            .unwrap_or_else(|| format!("#{instrument_id}"))
    }

    /// `name_for(id)`: the name, or the symbol.
    #[must_use]
    pub fn name_for(&self, instrument_id: i64) -> String {
        self.names
            .get(&instrument_id)
            .cloned()
            .unwrap_or_else(|| self.symbol_for(instrument_id))
    }
}

/// `PointInTimeReader`: the look-ahead guard. Every read of dated data goes through it, and
/// it checks the *data* (the latest `date` in what comes back), not only the request.
#[derive(Debug)]
pub struct PointInTimeReader<'a> {
    data: &'a BacktestData,
    as_of: Option<NaiveDate>,
}

impl<'a> PointInTimeReader<'a> {
    /// A reader with its cursor unset.
    #[must_use]
    pub fn new(data: &'a BacktestData) -> Self {
        Self { data, as_of: None }
    }

    /// `as_of`: the cursor.
    ///
    /// # Errors
    /// [`BacktestError::Backtest`] while the cursor is unset.
    pub fn as_of(&self) -> Result<NaiveDate, BacktestError> {
        self.as_of.ok_or_else(|| {
            BacktestError::Backtest("the point-in-time cursor has not been set".to_owned())
        })
    }

    /// `advance_to(day)`: move the clock forward; it never moves back.
    ///
    /// # Errors
    /// [`BacktestError::Backtest`] when `day` is before the cursor.
    pub fn advance_to(&mut self, day: NaiveDate) -> Result<(), BacktestError> {
        if let Some(current) = self.as_of
            && day < current
        {
            return Err(BacktestError::Backtest(format!(
                "the point-in-time cursor cannot move back from {current} to {day}"
            )));
        }
        self.as_of = Some(day);
        Ok(())
    }

    fn guard(&self, frame: DataFrame, what: &str) -> Result<DataFrame, BacktestError> {
        if !has_column(&frame, "date") {
            return Err(BacktestError::Data(format!(
                "{what} has no 'date' column, so the look-ahead guard cannot check it"
            )));
        }
        if frame.height() > 0 {
            let column = frame.column("date")?;
            let latest = match column.dtype() {
                DataType::Date => column.date()?.physical().iter().flatten().max(),
                DataType::Datetime(_, _) => {
                    let as_date = column.cast(&DataType::Date)?;
                    as_date.date()?.physical().iter().flatten().max()
                }
                // Python's isinstance(latest, date) is false for any other dtype: no check.
                _ => None,
            };
            if let Some(latest) = latest.and_then(days_to_date) {
                let as_of = self.as_of()?;
                if latest > as_of {
                    return Err(BacktestError::LookAhead {
                        what: what.to_owned(),
                        offending: latest,
                        as_of,
                    });
                }
            }
        }
        Ok(frame)
    }

    /// `screen_on(day)`: the screen's output as of `day`, guarded.
    ///
    /// # Errors
    /// No screen for `day`, a frame without `date`, a row after the cursor, an unset cursor.
    pub fn screen_on(&self, day: NaiveDate) -> Result<DataFrame, BacktestError> {
        let frame = self.data.screens.get(&day).ok_or_else(|| {
            BacktestError::Data(format!(
                "no screen result was precomputed for rebalance date {day}"
            ))
        })?;
        self.guard(frame.clone(), &format!("screen result for {day}"))
    }

    /// `benchmark_to(day)`: the benchmark's rows dated on or before `day`, guarded.
    ///
    /// # Errors
    /// No benchmark, a frame without `date`, a row after the cursor, an unset cursor.
    pub fn benchmark_to(&self, day: NaiveDate) -> Result<DataFrame, BacktestError> {
        let frame = self.data.benchmark.as_ref().ok_or_else(|| {
            BacktestError::Data("this run needs a benchmark series and none was supplied".into())
        })?;
        let window = frame
            .clone()
            .lazy()
            .filter(col("date").lt_eq(lit(day)))
            .collect()?;
        self.guard(window, "benchmark series")
    }

    /// `dividends_on(day)`: the dividend schedule's rows dated `day`, guarded.
    ///
    /// # Errors
    /// No schedule, a frame without `date`, a row after the cursor, an unset cursor.
    pub fn dividends_on(&self, day: NaiveDate) -> Result<DataFrame, BacktestError> {
        let frame = self.data.dividends.as_ref().ok_or_else(|| {
            BacktestError::Data(
                "the 'cash' dividend policy needs a dividend schedule and none was supplied".into(),
            )
        })?;
        let window = frame
            .clone()
            .lazy()
            .filter(col("date").eq(lit(day)))
            .collect()?;
        self.guard(window, "dividend schedule")
    }

    fn require_not_future(&self, day: NaiveDate, what: &str) -> Result<(), BacktestError> {
        let as_of = self.as_of()?;
        if day > as_of {
            return Err(BacktestError::LookAhead {
                what: format!("{what} price"),
                offending: day,
                as_of,
            });
        }
        Ok(())
    }

    /// `price_close(instrument_id, day)`.
    ///
    /// # Errors
    /// `day` after the cursor (look-ahead), an unset cursor, a non-trading day.
    pub fn price_close(
        &self,
        instrument_id: i64,
        day: NaiveDate,
    ) -> Result<Option<Decimal>, BacktestError> {
        self.require_not_future(day, "close")?;
        let prices = &self.data.prices;
        prices.close_on(instrument_id, prices.index_of(day)?)
    }

    /// `price_open(instrument_id, day)`.
    ///
    /// # Errors
    /// `day` after the cursor (look-ahead), an unset cursor, a non-trading day.
    pub fn price_open(
        &self,
        instrument_id: i64,
        day: NaiveDate,
    ) -> Result<Option<Decimal>, BacktestError> {
        self.require_not_future(day, "open")?;
        let prices = &self.data.prices;
        prices.open_on(instrument_id, prices.index_of(day)?)
    }

    /// `last_close(instrument_id, day)`: the most recent close on or before `day`, and its date.
    ///
    /// # Errors
    /// `day` after the cursor (look-ahead), an unset cursor, a non-trading day.
    pub fn last_close(
        &self,
        instrument_id: i64,
        day: NaiveDate,
    ) -> Result<Option<(Decimal, NaiveDate)>, BacktestError> {
        self.require_not_future(day, "last close")?;
        let prices = &self.data.prices;
        let found = prices.last_close_at_or_before(instrument_id, prices.index_of(day)?)?;
        Ok(
            found
                .and_then(|(price, position)| prices.calendar().get(position).map(|d| (price, *d))),
        )
    }
}
