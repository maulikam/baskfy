//! `Dec` — Python's `Decimal` with an explicit context precision, for this lane.
//!
//! [`baskfy_core::decimal::WideDecimal`] reproduces Python's default context (28 digits,
//! half-even) exactly, but its precision is fixed. `baskfy_core.portfolio_nav` chain-links its
//! wealth index in `localcontext(prec=60)`, and the XIRR solver raises a `Decimal` to a
//! **non-integral** power (`(1 + r) ** (days / 365)`), which `WideDecimal` has no operation for.
//! `Dec` is `WideDecimal`'s algorithm (written step for step from CPython's `_pydecimal`) with the
//! precision as a parameter, plus [`Dec::pow`]: `exp(y · ln x)` evaluated with twenty guard digits
//! and rounded half-even to the context precision — what libmpdec's `mpd_qpow` computes ("correctly
//! rounded in almost all cases"). DECISIONS-RS, api-portfolio P2.

use std::cmp::Ordering;
use std::fmt;

use baskfy_core::decimal::Rounding;

/// Python's default context precision.
pub(crate) const PREC: i64 = 28;

/// Python's default context `Emax`.
const EMAX: i64 = 999_999;

mod big {
    //! A minimal unsigned big integer, base 10^9 limbs, little-endian.

    use std::cmp::Ordering;

    const BASE: u64 = 1_000_000_000;

    #[derive(Clone, Debug, PartialEq, Eq)]
    pub(super) struct Big {
        limbs: Vec<u32>,
    }

    fn low(v: u64) -> u32 {
        u32::try_from(v % BASE).unwrap_or(0)
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

        /// `self - other`, for `self >= other`.
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

        /// `(self // d, self % d)`; `None` when `d` is zero. Schoolbook, one decimal digit of
        /// quotient at a time, the digit found by a limb-estimate then corrected.
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
            let multiples: Vec<Self> = (0..=10u64).map(|k| d.mul_small_add(k, 0)).collect();
            for b in digits.bytes() {
                r = r.mul_small_add(10, u64::from(b.saturating_sub(b'0')));
                // Binary search the largest k with d*k <= r (k in 0..=9).
                let (mut lo, mut hi) = (0usize, 9usize);
                while lo < hi {
                    let mid = (lo + hi).div_ceil(2);
                    if multiples[mid].cmp_big(&r) == Ordering::Greater {
                        hi = mid - 1;
                    } else {
                        lo = mid;
                    }
                }
                r = r.sub(&multiples[lo]);
                q.push(char::from(b'0' + u8::try_from(lo).unwrap_or(0)));
            }
            Some((Self::parse(&q), r))
        }

        /// `(self // d, self % d)` for a divisor below one limb (`d < 10^9`): one pass over the
        /// limbs, most significant first.
        pub(super) fn divmod_small(&self, d: u64) -> Option<(Self, Self)> {
            if d == 0 {
                return None;
            }
            let mut out = vec![0u32; self.limbs.len()];
            let mut rem = 0u64;
            for (i, &limb) in self.limbs.iter().enumerate().rev() {
                let cur = rem * BASE + u64::from(limb);
                out[i] = low(cur / d);
                rem = cur % d;
            }
            Some((
                Self { limbs: out }.trimmed(),
                Self {
                    limbs: vec![low(rem)],
                }
                .trimmed(),
            ))
        }

        /// The value when it fits one limb.
        pub(super) fn small(&self) -> Option<u64> {
            match self.limbs.as_slice() {
                [] => Some(0),
                [one] => Some(u64::from(*one)),
                _ => None,
            }
        }

        pub(super) fn incremented(&self) -> Self {
            self.mul_small_add(1, 1)
        }
    }
}

use big::Big;

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

fn all_zeros(digits: &str, prec: usize) -> bool {
    digits
        .as_bytes()
        .get(prec..)
        .is_none_or(|r| r.iter().all(|&b| b == b'0'))
}

fn exact_half(digits: &str, prec: usize) -> bool {
    let b = digits.as_bytes();
    b.get(prec) == Some(&b'5') && all_zeros(digits, prec + 1)
}

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

/// An exact decimal with Python `Decimal` semantics; every operation takes the context precision.
#[derive(Debug, Clone)]
pub(crate) struct Dec {
    neg: bool,
    digits: String,
    exp: i64,
}

/// `ln(10)` to 90 digits (CPython `Decimal(10).ln()` at `prec=90`).
const LN10: &str =
    "2.30258509299404568401799145468436420760110148862877297603332790096757260967735248023599721";

impl Dec {
    fn triple(neg: bool, digits: &str, exp: i64) -> Self {
        Self {
            neg,
            digits: strip_leading_zeros(digits),
            exp,
        }
    }

    /// `Decimal(0)`.
    pub(crate) fn zero() -> Self {
        Self::triple(false, "0", 0)
    }

    /// `Decimal(n)`.
    pub(crate) fn int(n: i64) -> Self {
        Self::triple(n < 0, &n.unsigned_abs().to_string(), 0)
    }

    /// `Decimal(text)` for a literal known to be valid (zero otherwise).
    pub(crate) fn lit(text: &str) -> Self {
        Self::parse(text).unwrap_or_else(Self::zero)
    }

    /// Python's `Decimal(str)` for finite numerals.
    pub(crate) fn parse(s: &str) -> Option<Self> {
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
            return None;
        }
        let mut exp = match exp_part {
            Some(e) => e.parse::<i64>().ok()?,
            None => 0,
        };
        exp -= len_i64(frac);
        Some(Self::triple(neg, &format!("{int}{frac}"), exp))
    }

    /// From a stored `numeric` (scale kept).
    pub(crate) fn from_decimal(d: rust_decimal::Decimal) -> Self {
        Self::triple(
            d.is_sign_negative(),
            &d.mantissa().unsigned_abs().to_string(),
            -i64::from(d.scale()),
        )
    }

    /// True for either signed zero.
    pub(crate) fn is_zero(&self) -> bool {
        self.digits == "0"
    }

    fn adjusted(&self) -> i64 {
        self.exp + len_i64(&self.digits) - 1
    }

    fn fix(self, prec: i64) -> Self {
        if self.is_zero() {
            return self;
        }
        let len = len_i64(&self.digits);
        if len <= prec {
            return self;
        }
        let mut exp_min = len + self.exp - prec;
        let kept = usize::try_from(prec).unwrap_or(28);
        let changed = round_flag(&self.digits, kept, Rounding::HalfEven);
        let mut coeff = self.digits[..kept].to_owned();
        if changed > 0 {
            coeff = Big::parse(&coeff).incremented().digits();
            if len_i64(&coeff) > prec {
                coeff.pop();
                exp_min += 1;
            }
        }
        Self::triple(self.neg, &coeff, exp_min)
    }

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

    /// `self.quantize(Decimal("1E{exp}"), rounding=mode)` in the default context. Python raises
    /// `InvalidOperation` beyond 28 digits; `None` here.
    pub(crate) fn quantize(&self, exp: i64, mode: Rounding) -> Option<Self> {
        let out = self.rescale(exp, mode);
        if len_i64(&out.digits) > PREC {
            return None;
        }
        Some(out.fix(PREC))
    }

    /// `-self`.
    pub(crate) fn neg(&self) -> Self {
        let neg = if self.is_zero() { false } else { !self.neg };
        Self::triple(neg, &self.digits, self.exp).fix(PREC)
    }

    /// `abs(self)`.
    pub(crate) fn abs(&self) -> Self {
        Self::triple(false, &self.digits, self.exp).fix(PREC)
    }

    fn copy_negate(&self) -> Self {
        Self::triple(!self.neg, &self.digits, self.exp)
    }

    /// `self + other` at precision `prec`.
    pub(crate) fn add_p(&self, other: &Self, prec: i64) -> Self {
        let exp = self.exp.min(other.exp);
        if self.is_zero() && other.is_zero() {
            return Self::triple(self.neg && other.neg, "0", exp).fix(prec);
        }
        if self.is_zero() {
            let e = exp.max(other.exp - prec - 1);
            return other.rescale(e, Rounding::HalfEven).fix(prec);
        }
        if other.is_zero() {
            let e = exp.max(self.exp - prec - 1);
            return self.rescale(e, Rounding::HalfEven).fix(prec);
        }
        let (mut tmp, mut oth) = if self.exp < other.exp {
            (other.clone(), self.clone())
        } else {
            (self.clone(), other.clone())
        };
        let tmp_len = len_i64(&tmp.digits);
        let oth_len = len_i64(&oth.digits);
        let floor = tmp.exp + (-1i64).min(tmp_len - prec - 2);
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
                Ordering::Equal => return Self::triple(false, "0", exp).fix(prec),
                Ordering::Greater => (tmp.neg, a.sub(&b)),
                Ordering::Less => (oth.neg, b.sub(&a)),
            }
        };
        Self::triple(neg, &coeff.digits(), common).fix(prec)
    }

    /// `self - other` at precision `prec`.
    pub(crate) fn sub_p(&self, other: &Self, prec: i64) -> Self {
        self.add_p(&other.copy_negate(), prec)
    }

    /// `self * other` at precision `prec`.
    pub(crate) fn mul_p(&self, other: &Self, prec: i64) -> Self {
        let neg = self.neg ^ other.neg;
        let exp = self.exp + other.exp;
        if self.is_zero() || other.is_zero() {
            return Self::triple(neg, "0", exp).fix(prec);
        }
        let product = Big::parse(&self.digits).mul(&Big::parse(&other.digits));
        Self::triple(neg, &product.digits(), exp).fix(prec)
    }

    /// `self / other` at precision `prec`; `None` for a zero divisor (Python raises).
    pub(crate) fn div_p(&self, other: &Self, prec: i64) -> Option<Self> {
        if other.is_zero() {
            return None;
        }
        let neg = self.neg ^ other.neg;
        if self.is_zero() {
            return Some(Self::triple(neg, "0", self.exp - other.exp).fix(prec));
        }
        let shift = len_i64(&other.digits) - len_i64(&self.digits) + prec + 1;
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
        let (coeff, rem) = match den.small() {
            Some(d) => num.divmod_small(d)?,
            None => num.divmod(&den)?,
        };
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
        Some(Self::triple(neg, &digits, exp).fix(prec))
    }

    /// `self + other` in the default context.
    pub(crate) fn add(&self, other: &Self) -> Self {
        self.add_p(other, PREC)
    }

    /// `self - other` in the default context.
    pub(crate) fn sub(&self, other: &Self) -> Self {
        self.sub_p(other, PREC)
    }

    /// `self * other` in the default context.
    pub(crate) fn mul(&self, other: &Self) -> Self {
        self.mul_p(other, PREC)
    }

    /// `self / other` in the default context.
    pub(crate) fn div(&self, other: &Self) -> Option<Self> {
        self.div_p(other, PREC)
    }

    /// `str(self)`.
    pub(crate) fn to_sci_string(&self) -> String {
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

    /// An `f64` near the value (a starting guess only; never an answer).
    fn approx(&self) -> f64 {
        let mantissa: f64 = self
            .digits
            .get(..17.min(self.digits.len()))
            .unwrap_or("0")
            .parse()
            .unwrap_or(0.0);
        let shift = len_i64(&self.digits) - 17.min(len_i64(&self.digits)) + self.exp;
        let v = mantissa * 10f64.powi(i32::try_from(shift).unwrap_or(0));
        if self.neg { -v } else { v }
    }

    /// `exp(self)` at precision `w` (working digits; not correctly rounded by itself).
    fn exp_w(&self, w: i64) -> Self {
        if self.is_zero() {
            return Self::int(1);
        }
        let ln10 = Self::lit(LN10);
        // self = n·ln10 + r, |r| <= ln10/2.
        let n = (self.approx() / std::f64::consts::LN_10).round();
        let n_i = if n.is_finite() { n as i64 } else { 0 };
        let r = self.sub_p(&Self::int(n_i).mul_p(&ln10, w + 10), w + 10);
        // exp(r) = exp(r / 2^k)^(2^k), with |r / 2^k| < 2^-10.
        let mut k = 0u32;
        let mut scale = 1.0f64;
        while r.approx().abs() / scale >= 1.0 / 1024.0 && k < 64 {
            scale *= 2.0;
            k += 1;
        }
        let two_k = Self::int(1i64 << k.min(62));
        let small = r.div_p(&two_k, w).unwrap_or_else(Self::zero);
        // Taylor: 1 + x + x²/2! + ...
        let mut sum = Self::int(1);
        let mut term = Self::int(1);
        for i in 1..200 {
            term = term
                .mul_p(&small, w)
                .div_p(&Self::int(i), w)
                .unwrap_or_else(Self::zero);
            if term.is_zero() || term.adjusted() < sum.adjusted() - w - 2 {
                break;
            }
            sum = sum.add_p(&term, w);
        }
        for _ in 0..k {
            sum = sum.mul_p(&sum, w);
        }
        Self {
            neg: sum.neg,
            digits: sum.digits,
            exp: sum.exp + n_i,
        }
    }

    /// `ln(self)` for `self > 0`, at precision `w`.
    fn ln_w(&self, w: i64) -> Option<Self> {
        if self.neg || self.is_zero() {
            return None;
        }
        // self = m · 10^e with m in [1, 10).
        let e = self.adjusted();
        let m = Self {
            neg: false,
            digits: self.digits.clone(),
            exp: self.exp - e,
        };
        let ln10 = Self::lit(LN10);
        let guess = m.approx().ln();
        let mut y = Self::parse(&format!("{guess:.17}"))?;
        // Halley: y += 2·(m − e^y)/(m + e^y); cubic, so 17 → 51 → 150 digits.
        for _ in 0..4 {
            let ey = y.exp_w(w + 5);
            let step = m
                .sub_p(&ey, w + 5)
                .mul_p(&Self::int(2), w + 5)
                .div_p(&m.add_p(&ey, w + 5), w + 5)?;
            y = y.add_p(&step, w + 5);
            if step.is_zero() || step.adjusted() < y.adjusted() - w - 2 {
                break;
            }
        }
        Some(y.add_p(&Self::int(e).mul_p(&ln10, w + 5), w))
    }

    /// `self ** other` in the default context, for `self > 0`. An integral `other` of zero is
    /// `Decimal(1)`; everything else is `exp(other · ln self)` with twenty guard digits, rounded
    /// half-even to 28 significant digits. `None` for a non-positive base (Python raises or
    /// returns a complex-domain error; the solver never asks).
    #[cfg(test)]
    pub(crate) fn pow(&self, other: &Self) -> Option<Self> {
        Self::pow_from_ln(&self.pow_ln()?, other)
    }

    /// `ln(self)` at the working precision [`Dec::pow`] uses — computed once and handed to
    /// [`Dec::pow_from_ln`] when one base is raised to many exponents (the XIRR solver's
    /// `(1 + r) ** t` over every flow). The same value, so the same correctly rounded powers.
    pub(crate) fn pow_ln(&self) -> Option<Self> {
        self.ln_w(PREC + 20)
    }

    /// `x ** other` given `ln = x.pow_ln()`.
    pub(crate) fn pow_from_ln(ln: &Self, other: &Self) -> Option<Self> {
        if other.is_zero() {
            return Some(Self::int(1));
        }
        let w = PREC + 20;
        let z = other.mul_p(ln, w);
        // The default context's Emax: beyond it Python raises `Overflow` (a trapped signal).
        if z.approx() > 2_302_585.0 {
            return None;
        }
        let out = z.exp_w(w).fix(PREC);
        if out.adjusted() > EMAX {
            return None;
        }
        Some(out)
    }
}

impl PartialEq for Dec {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}

impl Eq for Dec {}

impl PartialOrd for Dec {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Dec {
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

impl fmt::Display for Dec {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.to_sci_string())
    }
}

impl serde::Serialize for Dec {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&self.to_sci_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(s: &str) -> Dec {
        Dec::lit(s)
    }

    #[test]
    fn default_context_matches_cpython() {
        // Each right-hand side is CPython 3.12's `str()` of the same expression.
        assert_eq!(
            d("1").div(&d("3")).map(|x| x.to_sci_string()),
            Some("0.3333333333333333333333333333".to_owned())
        );
        assert_eq!(
            d("1").div(&d("0.18")).map(|x| x.to_sci_string()),
            Some("5.555555555555555555555555556".to_owned())
        );
        assert_eq!(
            d("0").div(&d("365.25")).map(|x| x.to_sci_string()),
            Some("0E+2".to_owned())
        );
        assert_eq!(d("0.1").add(&d("0.20")).to_sci_string(), "0.30");
        assert_eq!(d("1.10").mul(&d("1.10")).to_sci_string(), "1.2100");
        assert_eq!(
            d("2.675")
                .quantize(-2, Rounding::HalfUp)
                .map(|x| x.to_sci_string()),
            Some("2.68".to_owned())
        );
        assert_eq!(d("-0.0000001").to_sci_string(), "-1E-7");
        // localcontext(prec=60): 1/3.
        assert_eq!(
            d("1").div_p(&d("3"), 60).map(|x| x.to_sci_string()),
            Some(format!("0.{}", "3".repeat(60)))
        );
    }

    #[test]
    fn non_integral_powers_are_cpythons() {
        // CPython 3.12: str(Decimal(a) ** Decimal(b)).
        for (a, b, want) in [
            ("1.1", "0.5", "1.048808848170151546991453514"),
            (
                "1.2040",
                "2.493150684931506849315068493",
                "1.588597888959461265480112505",
            ),
            (
                "0.95",
                "0.002739726027397260273972602740",
                "0.9998594803001534885951741318",
            ),
            ("2", "10", "1024"),
            ("1.5", "2", "2.25"),
        ] {
            let got = d(a).pow(&d(b)).map(|x| x.to_sci_string());
            let ok = got.as_deref().map(|g| Dec::lit(g) == Dec::lit(want));
            assert_eq!(ok, Some(true), "{a} ** {b}: got {got:?}, want {want}");
            // The digits too, for the inexact ones (28 significant).
            if want.len() > 10 {
                assert_eq!(got.as_deref(), Some(want), "{a} ** {b}");
            }
        }
        assert_eq!(
            d("1.3").pow(&d("0")).map(|x| x.to_sci_string()),
            Some("1".to_owned())
        );
    }
}
