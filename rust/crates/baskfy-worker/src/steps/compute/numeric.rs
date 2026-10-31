//! How a Python value becomes a stored number, exactly — the conversions the compute lane's
//! writers share (house rule 8: storage precision is the contract).
//!
//! * [`float_to_numeric`]: asyncpg binds a Python `float` to a `numeric` parameter as
//!   `Decimal(float)` — the **exact** binary value (`asyncpg/pgproto/codecs/numeric.pyx`,
//!   `numeric_encode_binary`) — and PostgreSQL then rounds it to the column's scale, half away
//!   from zero. `rust_decimal` cannot hold an exact double (up to ~770 digits), so the rounding
//!   is done here on the integer `m * 2^e` instead, which gives the same digits.
//! * [`float_to_int`]: asyncpg's `int8_encode` calls `int(obj)` on a float — truncation.
//! * [`quantize_half_up_repr`]: `Decimal(str(x)).quantize(10**-places, ROUND_HALF_UP)` — the
//!   desk score's `_stored`, which rounds Python's **shortest repr**, not the exact binary.
//! * [`polars_round`] / [`round_half_up`]: polars' `round(places)` and
//!   `baskfy_core.precision.round_expr` (core keeps a private copy inside `factors`).

use rust_decimal::Decimal;

use crate::error::WorkerError;

/// The largest scale [`float_to_numeric`] handles (`10^20 * 2^53 < 2^128`). `factor_daily`'s
/// widest is 10.
const MAX_SCALE: u32 = 20;

fn overflow(x: f64) -> WorkerError {
    WorkerError::python(
        "DataError",
        format!("numeric field overflow: {x} does not fit the column"),
    )
}

/// `Decimal(x)` bound into a `numeric(p, scale)` column: the exact double rounded to `scale`
/// places, ties away from zero (PostgreSQL's `round_var`). `None` for NaN and infinity, which the
/// Python callers turn into NULL before binding (`_clean`, `_finite`).
///
/// # Errors
/// A magnitude beyond what `rust_decimal` holds (PostgreSQL would refuse it as an overflow of
/// any column this lane writes).
pub(crate) fn float_to_numeric(x: f64, scale: u32) -> Result<Option<Decimal>, WorkerError> {
    if !x.is_finite() {
        return Ok(None);
    }
    if scale > MAX_SCALE {
        return Err(overflow(x));
    }
    let bits = x.to_bits();
    let negative = (bits >> 63) == 1;
    let exponent_bits = i32::try_from((bits >> 52) & 0x7ff).map_err(|_| overflow(x))?;
    let fraction = bits & ((1_u64 << 52) - 1);
    let (mantissa, exponent) = if exponent_bits == 0 {
        (fraction, -1074_i32)
    } else {
        (fraction | (1_u64 << 52), exponent_bits - 1075)
    };
    let ten_s = 10_u128.pow(scale);
    let scaled = u128::from(mantissa)
        .checked_mul(ten_s)
        .ok_or_else(|| overflow(x))?;
    let magnitude: u128 = if exponent >= 0 {
        let shift = u32::try_from(exponent).map_err(|_| overflow(x))?;
        if shift >= 127 || scaled.leading_zeros() <= shift {
            return Err(overflow(x));
        }
        scaled << shift
    } else {
        let k = exponent.unsigned_abs();
        if k >= 128 {
            // scaled < 2^120, so the value is below 2^-8 of one unit in the last place: 0.
            0
        } else {
            let q = scaled >> k;
            let rem = scaled & ((1_u128 << k) - 1);
            let half = 1_u128 << (k - 1);
            if rem >= half { q + 1 } else { q }
        }
    };
    let signed = i128::try_from(magnitude).map_err(|_| overflow(x))?;
    let signed = if negative { -signed } else { signed };
    Decimal::try_from_i128_with_scale(signed, scale)
        .map(Some)
        .map_err(|_| overflow(x))
}

/// `int(x)` for a float bound to an integer column: truncation; `None` for NaN and infinity
/// (cleaned to NULL before binding).
///
/// # Errors
/// A float beyond `i64` (asyncpg's `OverflowError`).
pub(crate) fn float_to_int(x: f64) -> Result<Option<i64>, WorkerError> {
    if !x.is_finite() {
        return Ok(None);
    }
    let t = x.trunc();
    // 2^63 is exactly representable; anything at or above it does not fit.
    if !(-9_223_372_036_854_775_808.0..9_223_372_036_854_775_808.0).contains(&t) {
        return Err(WorkerError::python(
            "OverflowError",
            format!("value out of int64 range: {x}"),
        ));
    }
    #[allow(clippy::cast_possible_truncation)]
    let i = t as i64;
    Ok(Some(i))
}

/// `Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)`: Python's
/// shortest repr of `x` rounded half away from zero on its first dropped digit. `None` for NaN
/// and infinity (`_stored`'s `NA`).
///
/// # Errors
/// A value too large for `rust_decimal` (never a desk component, which are below 100).
pub(crate) fn quantize_half_up_repr(x: f64, places: u32) -> Result<Option<Decimal>, WorkerError> {
    if !x.is_finite() {
        return Ok(None);
    }
    // Rust's `Display` for f64 is the shortest round-trip digits, positional: the same digits
    // as Python's repr (which may print them in exponent form; the value is the same).
    let text = format!("{}", x.abs());
    let (int_part, frac_part) = text.split_once('.').unwrap_or((text.as_str(), ""));
    let places_usize = usize::try_from(places).map_err(|_| overflow(x))?;
    let mut digits = String::with_capacity(int_part.len() + places_usize);
    digits.push_str(int_part);
    let kept: String = frac_part.chars().take(places_usize).collect();
    digits.push_str(&kept);
    for _ in kept.len()..places_usize {
        digits.push('0');
    }
    let round_up = frac_part
        .chars()
        .nth(places_usize)
        .is_some_and(|c| c >= '5');
    let mut magnitude: i128 = digits.parse().map_err(|_| overflow(x))?;
    if round_up {
        magnitude += 1;
    }
    let signed = if x.is_sign_negative() {
        -magnitude
    } else {
        magnitude
    };
    Decimal::try_from_i128_with_scale(signed, places)
        .map(Some)
        .map_err(|_| overflow(x))
}

/// polars' `round(places)` on a float (half to even), as its kernel computes it.
pub(crate) fn polars_round(val: f64, places: i32) -> f64 {
    let multiplier = 10.0_f64.powi(places);
    let ret = (val * multiplier).round_ties_even() / multiplier;
    if ret.is_finite() { ret } else { val }
}

/// polars' `sign` on a float: -1, 1, or the value itself (0, -0, NaN).
fn sign(x: f64) -> f64 {
    if x < 0.0 {
        -1.0
    } else if x > 0.0 {
        1.0
    } else {
        x
    }
}

/// `baskfy_core.precision.round_expr`: `((|x| 10^p + 0.5).floor() / 10^p).round(p) * sign(x)`,
/// `None` for null, NaN and infinity.
pub(crate) fn round_half_up(value: Option<f64>, places: i32) -> Option<f64> {
    let scale = 10.0_f64.powf(f64::from(places));
    match value {
        Some(x) if x.is_finite() => {
            Some(polars_round((x.abs() * scale + 0.5).floor() / scale, places) * sign(x))
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use std::str::FromStr;

    use super::*;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).unwrap()
    }

    #[test]
    fn exact_binary_rounding_matches_postgres_on_decimal_of_float() {
        // Decimal(2.675) = 2.67499999999999982236431605997495353221893310546875 -> 2.67
        assert_eq!(float_to_numeric(2.675, 2).unwrap(), Some(d("2.67")));
        // Decimal(0.125) is exact, a tie: away from zero.
        assert_eq!(float_to_numeric(0.125, 2).unwrap(), Some(d("0.13")));
        assert_eq!(float_to_numeric(-0.125, 2).unwrap(), Some(d("-0.13")));
        assert_eq!(float_to_numeric(12.35, 2).unwrap(), Some(d("12.35")));
        assert_eq!(float_to_numeric(0.0, 2).unwrap(), Some(d("0.00")));
        assert_eq!(float_to_numeric(-0.0, 4).unwrap(), Some(d("0.0000")));
        assert_eq!(float_to_numeric(1e-300, 10).unwrap(), Some(d("0E-10")));
        assert_eq!(
            float_to_numeric(0.123_456_789_05, 10).unwrap(),
            // Decimal(0.12345678905) = 0.123456789050000001473... -> up.
            Some(d("0.1234567891"))
        );
        assert_eq!(
            float_to_numeric(123_456_789.0, 0).unwrap(),
            Some(d("123456789"))
        );
        assert_eq!(float_to_numeric(f64::NAN, 2).unwrap(), None);
        assert_eq!(float_to_numeric(f64::INFINITY, 2).unwrap(), None);
    }

    #[test]
    fn int_columns_truncate_like_python_int() {
        assert_eq!(float_to_int(12.9).unwrap(), Some(12));
        assert_eq!(float_to_int(-12.9).unwrap(), Some(-12));
        assert_eq!(float_to_int(f64::NAN).unwrap(), None);
        assert!(float_to_int(1e19).is_err());
    }

    #[test]
    fn repr_half_up_rounds_the_printed_digits() {
        // repr(2.675) is '2.675': half-up on the digits gives 2.68 (the exact binary would not).
        assert_eq!(quantize_half_up_repr(2.675, 2).unwrap(), Some(d("2.68")));
        assert_eq!(quantize_half_up_repr(-2.675, 2).unwrap(), Some(d("-2.68")));
        assert_eq!(quantize_half_up_repr(21.0, 4).unwrap(), Some(d("21.0000")));
        assert_eq!(
            quantize_half_up_repr(3.552_713_678_800_501e-15, 4).unwrap(),
            Some(d("0.0000"))
        );
        assert_eq!(quantize_half_up_repr(57.25, 1).unwrap(), Some(d("57.3")));
        assert_eq!(quantize_half_up_repr(f64::NAN, 1).unwrap(), None);
    }

    #[test]
    fn round_expr_is_half_up() {
        assert_eq!(round_half_up(Some(2.5), 0), Some(3.0));
        assert_eq!(round_half_up(Some(-2.5), 0), Some(-3.0));
        assert_eq!(round_half_up(Some(f64::NAN), 2), None);
    }
}
