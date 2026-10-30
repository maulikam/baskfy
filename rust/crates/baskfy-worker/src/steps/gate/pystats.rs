//! Python's `statistics.median`, `statistics.fmean` and `statistics.pstdev` (CPython 3.12), and
//! the two float formats the gate's messages use, reproduced **bit for bit**.
//!
//! The gate's step row stores `mean`, `sigma` and `band` as JSON numbers and the harness compares
//! them byte for byte, so "close enough" is not enough:
//!
//! * `fmean` is `math.fsum(data) / len(data)`: the exactly rounded sum, then one division.
//! * `pstdev` is the **correctly rounded** square root of the exact population variance
//!   (`_ss` sums with `Fraction`s; `_float_sqrt_of_frac` takes an integer square root with
//!   round-to-odd and converts once). A naive `sqrt(Σ(x-μ)²/n)` in `f64` differs in the last
//!   bits.
//!
//! Both need integers wider than 128 bits, so a minimal unsigned big integer lives here (the
//! workspace has none of its own, and one private type is cheaper than a new dependency —
//! DECISIONS-RS R-gate.3).

use std::cmp::Ordering;

/// `statistics.median(data)` over integers: the middle value (an `int`) for an odd count, the
/// mean of the two middle values (a `float`) for an even one.
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) enum Median {
    /// An odd count: Python's `int`.
    Int(i64),
    /// An even count: `(a + b) / 2`, a `float`.
    Float(f64),
}

impl Median {
    /// The value as Python's `float(median)`.
    pub(crate) fn as_f64(self) -> f64 {
        match self {
            // Bar counts are far below 2**53, so the conversion is exact.
            #[allow(clippy::cast_precision_loss)]
            Self::Int(i) => i as f64,
            Self::Float(f) => f,
        }
    }
}

/// `statistics.median(values)`; `None` for no values (Python raises; the caller never asks).
pub(crate) fn median_ints(values: &[i64]) -> Option<Median> {
    let mut sorted = values.to_vec();
    sorted.sort_unstable();
    let n = sorted.len();
    if n == 0 {
        return None;
    }
    let i = n / 2;
    if n % 2 == 1 {
        return sorted.get(i).copied().map(Median::Int);
    }
    let a = *sorted.get(i - 1)?;
    let b = *sorted.get(i)?;
    // `(a + b) / 2`: int + int is exact, then true division rounds once.
    Some(Median::Float(int_ratio_to_f64(
        i128::from(a) + i128::from(b),
        2,
    )))
}

/// `p / q` as Python's `int / int` (correctly rounded), for a small positive `q`.
fn int_ratio_to_f64(p: i128, q: u32) -> f64 {
    let negative = p < 0;
    let magnitude = Big::from_u128(p.unsigned_abs());
    // p / q = (p * 2^k / q) * 2^-k with enough bits that the quotient's rounding is exact.
    let k = 128;
    let scaled = magnitude.shl(k);
    let (quotient, remainder) = scaled.divmod(&Big::from_u128(u128::from(q)));
    let mut value = to_f64_scaled(&quotient, -i64::from(k), !remainder.is_zero());
    if negative {
        value = -value;
    }
    value
}

/// `statistics.fmean(data)`: `math.fsum(data) / len(data)`. `None` for no data.
pub(crate) fn fmean(data: &[f64]) -> Option<f64> {
    if data.is_empty() {
        return None;
    }
    let exact = ExactSum::of(data)?;
    let total = exact.to_f64();
    #[allow(clippy::cast_precision_loss)]
    let n = data.len() as f64;
    Some(total / n)
}

/// `statistics.pstdev(data)`: the correctly rounded square root of the exact population variance.
/// `None` for no data or a non-finite value.
pub(crate) fn pstdev(data: &[f64]) -> Option<f64> {
    if data.is_empty() {
        return None;
    }
    let terms: Vec<(bool, Big, i64)> = data.iter().map(|x| decompose(*x)).collect::<Option<_>>()?;
    // Every value is X_i * 2^e with a common (minimum) exponent e.
    let e = terms.iter().map(|(_, _, exp)| *exp).min().unwrap_or(0);
    let mut sum = Signed::zero();
    let mut sum_sq = Big::zero();
    for (neg, mantissa, exp) in &terms {
        let shift = u32::try_from(exp - e).ok()?;
        let x = mantissa.shl(shift);
        sum_sq = sum_sq.add(&x.mul(&x));
        sum = sum.add(&Signed { neg: *neg, mag: x });
    }
    let n = Big::from_u128(u128::try_from(data.len()).ok()?);
    // mss = (n * ΣX² - (ΣX)²) / (n² * 2^(-2e))   [times 2^(2e) folded into the right side]
    let numerator = n.mul(&sum_sq).sub(&sum.mag.mul(&sum.mag))?;
    let mut denominator = n.mul(&n);
    let mut numerator = numerator;
    if e < 0 {
        denominator = denominator.shl(u32::try_from(-2 * e).ok()?);
    } else {
        numerator = numerator.shl(u32::try_from(2 * e).ok()?);
    }
    Some(float_sqrt_of_frac(&numerator, &denominator))
}

/// `_float_sqrt_of_frac(n, m)` (CPython 3.12): the square root of `n/m`, correctly rounded.
fn float_sqrt_of_frac(n: &Big, m: &Big) -> f64 {
    const SQRT_BIT_WIDTH: i64 = 109; // 2 * 53 + 3
    let q = (i64::from(n.bit_length()) - i64::from(m.bit_length()) - SQRT_BIT_WIDTH).div_euclid(2);
    if q >= 0 {
        let q32 = u32::try_from(q).unwrap_or(u32::MAX);
        let root = integer_sqrt_of_frac_rto(n, &m.shl(2 * q32)).shl(q32);
        to_f64_scaled(&root, 0, false)
    } else {
        let q32 = u32::try_from(-q).unwrap_or(u32::MAX);
        let root = integer_sqrt_of_frac_rto(&n.shl(2 * q32), m);
        to_f64_scaled(&root, -i64::from(q32), false)
    }
}

/// `_integer_sqrt_of_frac_rto(n, m)`: `isqrt(n // m) | (a*a*m != n)`.
fn integer_sqrt_of_frac_rto(n: &Big, m: &Big) -> Big {
    let (quotient, _) = n.divmod(m);
    let a = quotient.isqrt();
    let exact = a.mul(&a).mul(m).cmp(n) == Ordering::Equal;
    if exact { a } else { a.or_one() }
}

/// A finite float as `(negative, mantissa, exponent)` with `x = ±mantissa * 2^exponent`.
fn decompose(x: f64) -> Option<(bool, Big, i64)> {
    if !x.is_finite() {
        return None;
    }
    let bits = x.to_bits();
    let negative = bits >> 63 == 1;
    let exponent_bits = i64::try_from((bits >> 52) & 0x7ff).ok()?;
    let fraction = bits & ((1_u64 << 52) - 1);
    let (mantissa, exponent) = if exponent_bits == 0 {
        (fraction, -1074)
    } else {
        (fraction | (1_u64 << 52), exponent_bits - 1075)
    };
    Some((negative, Big::from_u128(u128::from(mantissa)), exponent))
}

/// `math.fsum`'s exact sum, kept as a signed big integer times a power of two.
struct ExactSum {
    value: Signed,
    exponent: i64,
}

impl ExactSum {
    fn of(data: &[f64]) -> Option<Self> {
        let terms: Vec<(bool, Big, i64)> =
            data.iter().map(|x| decompose(*x)).collect::<Option<_>>()?;
        let e = terms.iter().map(|(_, _, exp)| *exp).min().unwrap_or(0);
        let mut sum = Signed::zero();
        for (neg, mantissa, exp) in terms {
            let shift = u32::try_from(exp - e).ok()?;
            sum = sum.add(&Signed {
                neg,
                mag: mantissa.shl(shift),
            });
        }
        Some(Self {
            value: sum,
            exponent: e,
        })
    }

    /// The exact sum rounded once, half to even (what `fsum` returns).
    fn to_f64(&self) -> f64 {
        let magnitude = to_f64_scaled(&self.value.mag, self.exponent, false);
        if self.value.neg {
            -magnitude
        } else {
            magnitude
        }
    }
}

/// `mag * 2^exponent` rounded half to even to the nearest `f64`. `sticky` says the true value is
/// a little above `mag * 2^exponent` (a discarded non-zero remainder), which breaks a tie upward.
/// Results here are normal numbers; a subnormal result is still rounded, through the multiply.
fn to_f64_scaled(mag: &Big, exponent: i64, sticky: bool) -> f64 {
    let bits = mag.bit_length();
    if bits == 0 {
        return 0.0;
    }
    let (mut top, mut exp) = if bits <= 53 {
        (mag.low_u64(), exponent)
    } else {
        let shift = bits - 53;
        let top = mag.shr(shift);
        let dropped = mag.sub(&top.shl(shift)).unwrap_or_else(Big::zero);
        let half = Big::from_u128(1).shl(shift - 1);
        let mut rounded = top.low_u64();
        match dropped.cmp(&half) {
            Ordering::Greater => rounded += 1,
            Ordering::Equal if sticky || rounded & 1 == 1 => rounded += 1,
            _ => {}
        }
        (rounded, exponent + i64::from(shift))
    };
    if top == 1_u64 << 53 {
        top >>= 1;
        exp += 1;
    }
    #[allow(clippy::cast_precision_loss)]
    let base = top as f64; // at most 53 bits: exact
    scale_by_power_of_two(base, exp)
}

/// `x * 2^exp`, exact while the result stays normal.
fn scale_by_power_of_two(mut x: f64, mut exp: i64) -> f64 {
    while exp > 0 {
        let step = exp.min(1000);
        x *= f64::powi(2.0, i32::try_from(step).unwrap_or(1000));
        exp -= step;
    }
    while exp < 0 {
        let step = (-exp).min(1000);
        x *= f64::powi(2.0, -i32::try_from(step).unwrap_or(1000));
        exp += step;
    }
    x
}

/// Python's `format(x, ".{digits}f")` for a float: the exact binary value rounded half to even.
pub(crate) fn format_fixed(x: f64, digits: usize) -> String {
    if x.is_nan() {
        return "nan".to_owned();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    // Rust's `{:.N}` formats the exact binary expansion and rounds a tie to even, as CPython's
    // `PyOS_double_to_string(x, 'f', N)` does (both are correctly rounded); `-0.0` keeps its sign
    // in both.
    format!("{x:.digits$}")
}

/// Python's `format(x, ".0%")`: `x * 100` in floating point, then `.0f`, then `%`.
pub(crate) fn format_percent0(x: f64) -> String {
    format!("{}%", format_fixed(x * 100.0, 0))
}

// ---------------------------------------------------------------------------------------------
// A minimal unsigned big integer: base 2^32 limbs, little-endian, no leading zero limbs.
// ---------------------------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
struct Big {
    limbs: Vec<u32>,
}

#[derive(Debug, Clone)]
struct Signed {
    neg: bool,
    mag: Big,
}

impl Signed {
    fn zero() -> Self {
        Self {
            neg: false,
            mag: Big::zero(),
        }
    }

    fn add(&self, other: &Self) -> Self {
        if self.neg == other.neg {
            return Self {
                neg: self.neg,
                mag: self.mag.add(&other.mag),
            };
        }
        match self.mag.cmp(&other.mag) {
            Ordering::Less => Self {
                neg: other.neg,
                mag: other.mag.sub(&self.mag).unwrap_or_else(Big::zero),
            },
            _ => {
                let mag = self.mag.sub(&other.mag).unwrap_or_else(Big::zero);
                let neg = self.neg && !mag.is_zero();
                Self { neg, mag }
            }
        }
    }
}

impl Big {
    fn zero() -> Self {
        Self { limbs: Vec::new() }
    }

    fn from_u128(mut v: u128) -> Self {
        let mut limbs = Vec::new();
        while v > 0 {
            limbs.push(u32::try_from(v & 0xffff_ffff).unwrap_or(0));
            v >>= 32;
        }
        Self { limbs }
    }

    fn trim(mut self) -> Self {
        while self.limbs.last() == Some(&0) {
            self.limbs.pop();
        }
        self
    }

    fn is_zero(&self) -> bool {
        self.limbs.is_empty()
    }

    fn low_u64(&self) -> u64 {
        let lo = u64::from(self.limbs.first().copied().unwrap_or(0));
        let hi = u64::from(self.limbs.get(1).copied().unwrap_or(0));
        lo | (hi << 32)
    }

    fn bit_length(&self) -> u32 {
        match self.limbs.last() {
            None => 0,
            Some(top) => {
                let whole = u32::try_from(self.limbs.len() - 1).unwrap_or(u32::MAX);
                whole * 32 + (32 - top.leading_zeros())
            }
        }
    }

    fn bit(&self, i: u32) -> bool {
        let limb = (i / 32) as usize;
        self.limbs
            .get(limb)
            .is_some_and(|l| (l >> (i % 32)) & 1 == 1)
    }

    fn or_one(&self) -> Self {
        let mut out = self.clone();
        match out.limbs.first_mut() {
            Some(l) => *l |= 1,
            None => out.limbs.push(1),
        }
        out
    }

    fn add(&self, other: &Self) -> Self {
        let n = self.limbs.len().max(other.limbs.len());
        let mut limbs = Vec::with_capacity(n + 1);
        let mut carry = 0_u64;
        for i in 0..n {
            let a = u64::from(self.limbs.get(i).copied().unwrap_or(0));
            let b = u64::from(other.limbs.get(i).copied().unwrap_or(0));
            let s = a + b + carry;
            limbs.push(u32::try_from(s & 0xffff_ffff).unwrap_or(0));
            carry = s >> 32;
        }
        if carry > 0 {
            limbs.push(u32::try_from(carry).unwrap_or(0));
        }
        Self { limbs }.trim()
    }

    /// `self - other`, or `None` when it would be negative.
    fn sub(&self, other: &Self) -> Option<Self> {
        if self.cmp(other) == Ordering::Less {
            return None;
        }
        let mut limbs = Vec::with_capacity(self.limbs.len());
        let mut borrow = 0_i64;
        for i in 0..self.limbs.len() {
            let a = i64::from(self.limbs.get(i).copied().unwrap_or(0));
            let b = i64::from(other.limbs.get(i).copied().unwrap_or(0));
            let mut d = a - b - borrow;
            if d < 0 {
                d += 1 << 32;
                borrow = 1;
            } else {
                borrow = 0;
            }
            limbs.push(u32::try_from(d).unwrap_or(0));
        }
        Some(Self { limbs }.trim())
    }

    fn mul(&self, other: &Self) -> Self {
        if self.is_zero() || other.is_zero() {
            return Self::zero();
        }
        let mut out = vec![0_u64; self.limbs.len() + other.limbs.len() + 1];
        for (i, a) in self.limbs.iter().enumerate() {
            let mut carry = 0_u64;
            for (j, b) in other.limbs.iter().enumerate() {
                let slot = out.get(i + j).copied().unwrap_or(0);
                let t = u64::from(*a) * u64::from(*b) + slot + carry;
                if let Some(s) = out.get_mut(i + j) {
                    *s = t & 0xffff_ffff;
                }
                carry = t >> 32;
            }
            let mut k = i + other.limbs.len();
            while carry > 0 {
                let slot = out.get(k).copied().unwrap_or(0);
                let t = slot + carry;
                if let Some(s) = out.get_mut(k) {
                    *s = t & 0xffff_ffff;
                }
                carry = t >> 32;
                k += 1;
            }
        }
        Self {
            limbs: out
                .into_iter()
                .map(|v| u32::try_from(v).unwrap_or(0))
                .collect(),
        }
        .trim()
    }

    fn shl(&self, bits: u32) -> Self {
        if self.is_zero() {
            return Self::zero();
        }
        let whole = (bits / 32) as usize;
        let part = bits % 32;
        let mut limbs = vec![0_u32; whole];
        let mut carry = 0_u32;
        for l in &self.limbs {
            if part == 0 {
                limbs.push(*l);
            } else {
                limbs.push((l << part) | carry);
                carry = l >> (32 - part);
            }
        }
        if carry > 0 {
            limbs.push(carry);
        }
        Self { limbs }.trim()
    }

    fn shr(&self, bits: u32) -> Self {
        let whole = (bits / 32) as usize;
        let part = bits % 32;
        if whole >= self.limbs.len() {
            return Self::zero();
        }
        let src = self.limbs.get(whole..).unwrap_or(&[]);
        let mut limbs = Vec::with_capacity(src.len());
        for (i, l) in src.iter().enumerate() {
            if part == 0 {
                limbs.push(*l);
            } else {
                let next = src.get(i + 1).copied().unwrap_or(0);
                limbs.push((l >> part) | (next << (32 - part)));
            }
        }
        Self { limbs }.trim()
    }

    /// Floor division and remainder (binary long division; the operands here are a few hundred
    /// bits). Division by zero answers `(0, self)`; no caller divides by zero.
    fn divmod(&self, divisor: &Self) -> (Self, Self) {
        if divisor.is_zero() || self.cmp(divisor) == Ordering::Less {
            return (Self::zero(), self.clone());
        }
        let mut quotient = vec![0_u32; self.limbs.len()];
        let mut remainder = Self::zero();
        let mut i = self.bit_length();
        while i > 0 {
            i -= 1;
            remainder = remainder.shl(1);
            if self.bit(i) {
                remainder = remainder.or_one();
            }
            if remainder.cmp(divisor) != Ordering::Less {
                remainder = remainder.sub(divisor).unwrap_or_else(Self::zero);
                if let Some(limb) = quotient.get_mut((i / 32) as usize) {
                    *limb |= 1 << (i % 32);
                }
            }
        }
        (Self { limbs: quotient }.trim(), remainder)
    }

    /// `math.isqrt(self)`: the floor of the square root (Newton's iteration from above).
    fn isqrt(&self) -> Self {
        if self.is_zero() {
            return Self::zero();
        }
        // Start at 2^ceil(bits/2) >= sqrt(self).
        let mut x = Self::from_u128(1).shl(self.bit_length().div_ceil(2));
        loop {
            let (q, _) = self.divmod(&x);
            let (y, _) = x.add(&q).divmod(&Self::from_u128(2));
            if y.cmp(&x) != Ordering::Less {
                return x;
            }
            x = y;
        }
    }
}

impl PartialOrd for Big {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Big {
    fn cmp(&self, other: &Self) -> Ordering {
        match self.limbs.len().cmp(&other.limbs.len()) {
            Ordering::Equal => {
                for (a, b) in self.limbs.iter().rev().zip(other.limbs.iter().rev()) {
                    match a.cmp(b) {
                        Ordering::Equal => {}
                        o => return o,
                    }
                }
                Ordering::Equal
            }
            o => o,
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::float_cmp)]
    use super::*;

    #[test]
    fn median_is_pythons() {
        // statistics.median([...]): an odd count gives the int; an even count the float mean.
        assert_eq!(median_ints(&[3, 1, 2]), Some(Median::Int(2)));
        assert_eq!(median_ints(&[4216, 4217]), Some(Median::Float(4216.5)));
        assert_eq!(median_ints(&[1, 2]), Some(Median::Float(1.5)));
        assert_eq!(median_ints(&[]), None);
        assert_eq!(median_ints(&[5]), Some(Median::Int(5)));
    }

    #[test]
    fn fixed_formatting_rounds_half_to_even_like_python() {
        // f"{x:.0f}" in CPython 3.12.
        assert_eq!(format_fixed(4216.5, 0), "4216");
        assert_eq!(format_fixed(4217.5, 0), "4218");
        assert_eq!(format_fixed(0.5, 0), "0");
        assert_eq!(format_fixed(1.5, 0), "2");
        assert_eq!(format_fixed(2.5, 0), "2");
        assert_eq!(format_fixed(3794.85, 0), "3795");
        // f"{x:.2f}"
        assert_eq!(format_fixed(-18.01, 2), "-18.01");
        assert_eq!(format_fixed(-12.339_830_508_474_575, 2), "-12.34");
        assert_eq!(format_fixed(8.850_934_150_397_57, 2), "8.85");
        assert_eq!(format_fixed(2.675, 2), "2.67"); // 2.675 is below the tie in binary
        assert_eq!(format_fixed(22936.75, 2), "22936.75");
        assert_eq!(format_percent0(0.5), "50%");
        assert_eq!(format_percent0(0.335), "34%");
    }

    #[test]
    fn the_beds_return_history_reproduces_pythons_mean_and_sigma() {
        // ret_12m medians of the 60 sessions before 2026-10-29 on the bed (one is NULL, so 59),
        // as the gate reads them (percentile_cont through SQLAlchemy's Numeric(14, 2): "%.2f");
        // CPython 3.12: fmean -12.339830508474575, pstdev 2.9503113834658565, band
        // 8.85093415039757 — run 103's own stored numbers.
        let medians = super::tests_support::bed_medians();
        assert_eq!(medians.len(), 59);
        let mean = fmean(&medians).unwrap();
        let sigma = pstdev(&medians).unwrap();
        assert_eq!(mean, -12.339_830_508_474_575);
        assert_eq!(sigma, 2.950_311_383_465_856_5);
        assert_eq!(3.0 * sigma, 8.850_934_150_397_57);
    }

    #[test]
    fn small_cases_match_python() {
        // statistics.pstdev([1.5, 2.5, 2.5, 2.75, 3.25, 4.75]) == 0.986893273527251
        assert_eq!(
            pstdev(&[1.5, 2.5, 2.5, 2.75, 3.25, 4.75]).unwrap(),
            0.986_893_273_527_251
        );
        // statistics.fmean([3.5, 4.0, 5.25]) == 4.25
        assert_eq!(fmean(&[3.5, 4.0, 5.25]).unwrap(), 4.25);
        // fsum is exact where a running sum is not: fmean([0.1] * 10) == 0.1
        assert_eq!(fmean(&[0.1; 10]).unwrap(), 0.1);
        // fmean([1e16, 1.0, -1e16]) == 1/3
        assert_eq!(fmean(&[1e16, 1.0, -1e16]).unwrap(), 1.0 / 3.0);
        assert_eq!(pstdev(&[2.0, 2.0]).unwrap(), 0.0);
        // statistics.pstdev([0.1, 0.2, 0.3]) == 0.0816496580927726
        assert_eq!(pstdev(&[0.1, 0.2, 0.3]).unwrap(), 0.081_649_658_092_772_6);
        // statistics.pstdev([-18.01, 7.5, 1e-3]) == 10.705093398315901
        assert_eq!(
            pstdev(&[-18.01, 7.5, 1e-3]).unwrap(),
            10.705_093_398_315_901
        );
    }

    #[test]
    fn sixty_random_lists_match_cpython_bit_for_bit() {
        // `pystats_cases.txt`: values | fmean pstdev, written by CPython 3.12's `statistics`
        // (seed 20261030; two-decimal medians, wide floats, and tiny/huge mixes).
        let mut seen = 0;
        for line in include_str!("pystats_cases.txt").lines() {
            let (values, expected) = line.split_once(" | ").unwrap();
            let values: Vec<f64> = values.split(' ').map(|t| t.parse().unwrap()).collect();
            let expected: Vec<f64> = expected.split(' ').map(|t| t.parse().unwrap()).collect();
            assert_eq!(fmean(&values).unwrap(), expected[0], "fmean {line}");
            assert_eq!(pstdev(&values).unwrap(), expected[1], "pstdev {line}");
            seen += 1;
        }
        assert_eq!(seen, 60);
    }

    #[test]
    fn big_integer_basics() {
        let a = Big::from_u128(u128::MAX);
        let b = a.mul(&a);
        let (q, r) = b.divmod(&a);
        assert_eq!(q, a);
        assert!(r.is_zero());
        assert_eq!(b.isqrt(), a);
        assert_eq!(Big::from_u128(99).isqrt(), Big::from_u128(9));
        assert_eq!(Big::from_u128(100).isqrt(), Big::from_u128(10));
        assert_eq!(a.shl(70).shr(70), a);
    }
}

/// Fixtures shared with the gate's tests.
#[cfg(test)]
pub(crate) mod tests_support {
    /// The bed's 59 `ret_12m` medians before 2026-10-29 (the 60th session's is NULL), as the
    /// gate reads them (`%.2f`).
    pub(crate) fn bed_medians() -> Vec<f64> {
        include_str!("pystats_bed_medians.txt")
            .split_whitespace()
            .filter_map(|t| t.parse().ok())
            .collect()
    }
}
