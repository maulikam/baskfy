//! Python's float printing, written once for every lane (DECISIONS-RS R3.2, R3.4).
//!
//! Python reads a config float into money as `Decimal(str(x))`, and `str(float)` is
//! `repr(float)`: the **shortest** digits that round-trip, and among those the one closest to
//! the exact binary value; positional for a decimal exponent in `[-4, 16)` and always with a
//! fractional part (`1.0`, `20.0`), scientific outside it (`1e-05`, `1.5e+16`). Rust's `{x}`
//! prints `1` for `1.0` and `10000000000000000` for `1e16`, so a `Decimal` built from it has the
//! wrong scale (`"1"` where Python prints `"1.0"`) — the class of bug R3.2 recorded. Every port
//! goes through [`float_repr`]; before the wave-3 housekeeping seven private copies existed.

use std::str::FromStr;

use rust_decimal::Decimal;

use crate::decimal::{DecimalError, WideDecimal};

/// Python's `repr(float)` (= `str(float)`): `nan`, `inf`, `-inf`; otherwise the shortest
/// round-trip digits (the correctly rounded one among equals), positional for decimal exponents
/// in `[-4, 16)` with `.0` on an integral value, scientific (`1e+16`, `1.5e-07`) outside it.
#[must_use]
pub fn float_repr(x: f64) -> String {
    if x.is_nan() {
        return "nan".to_owned();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    // Rust's shortest form gives the shortest round-trip *length*; among candidates of that
    // length Python takes the one closest to the exact value, which is the correctly rounded
    // print at that precision. At a power of two the rounding interval is lopsided and the
    // correctly rounded print may not round-trip; then the shortest form is the only candidate.
    let shortest = format!("{x:e}");
    let length = shortest
        .split('e')
        .next()
        .unwrap_or("")
        .chars()
        .filter(char::is_ascii_digit)
        .count();
    let rounded = format!("{x:.*e}", length.saturating_sub(1));
    let sci = if rounded
        .parse::<f64>()
        .is_ok_and(|y| y.to_bits() == x.to_bits())
    {
        rounded
    } else {
        shortest
    };
    let (mantissa, exp) = sci.split_once('e').unwrap_or((sci.as_str(), "0"));
    let exp: i64 = exp.parse().unwrap_or(0);
    let (sign, mantissa) = match mantissa.strip_prefix('-') {
        Some(m) => ("-", m),
        None => ("", mantissa),
    };
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let n = i64::try_from(digits.len()).unwrap_or(1);
    if (-4..16).contains(&exp) {
        if exp < 0 {
            let zeros = "0".repeat(usize::try_from(-exp - 1).unwrap_or(0));
            return format!("{sign}0.{zeros}{digits}");
        }
        let int_len = exp + 1;
        if int_len >= n {
            let zeros = "0".repeat(usize::try_from(int_len - n).unwrap_or(0));
            return format!("{sign}{digits}{zeros}.0");
        }
        let cut = usize::try_from(int_len).unwrap_or(0);
        return format!("{sign}{}.{}", &digits[..cut], &digits[cut..]);
    }
    let (head, rest) = digits.split_at(1.min(digits.len()));
    let body = if rest.is_empty() {
        head.to_owned()
    } else {
        format!("{head}.{rest}")
    };
    let esign = if exp < 0 { "-" } else { "+" };
    format!("{sign}{body}e{esign}{:02}", exp.unsigned_abs())
}

/// `Decimal(str(x))` for a finite float, exactly and kept wide (`1e-30` prints at a scale
/// `rust_decimal` lacks; `20.0` is `Decimal('20.0')`, scale 1).
///
/// # Errors
/// [`DecimalError`] for a NaN or an infinity (Python would carry `Decimal('NaN')` /
/// `Decimal('Infinity')`, which [`WideDecimal`] cannot hold); each caller words its own error.
pub fn wide_from_float_str(x: f64) -> Result<WideDecimal, DecimalError> {
    if !x.is_finite() {
        return Err(DecimalError(format!(
            "Decimal(str({})) is not finite",
            float_repr(x)
        )));
    }
    WideDecimal::from_str(&float_repr(x))
}

/// `Decimal(str(x))` for a finite float as a `rust_decimal::Decimal`, scale included (`1.0` is
/// `1.0`, `1e-05` is `0.00001`). A value with more than 28 decimal places rounds half-even to
/// 28, and a positive-exponent print (`1e+16`) is held at exponent 0 (`rust_decimal` has no
/// positive exponent; R3.8's limit) — both beyond any config value.
///
/// # Errors
/// [`DecimalError`] for a non-finite float or a magnitude beyond `rust_decimal`'s ~7.9e28.
pub fn decimal_from_float_str(x: f64) -> Result<Decimal, DecimalError> {
    if !x.is_finite() {
        return Err(DecimalError(format!(
            "{} is not a finite config number",
            float_repr(x)
        )));
    }
    let text = float_repr(x);
    let mut d = Decimal::from_str(&text)
        .or_else(|_| Decimal::from_scientific(&text))
        .map_err(|e| DecimalError(format!("{text} does not fit a Decimal: {e}")))?;
    // `rust_decimal`'s parser drops the sign of a zero; Python keeps it (`Decimal('-0.0')`).
    if x.is_sign_negative() {
        d.set_sign_negative(true);
    }
    Ok(d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repr_matches_python() {
        for (x, s) in [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (20.0, "20.0"),
            (20.92, "20.92"),
            (0.1, "0.1"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1.5e-7, "1.5e-07"),
            (1e16, "1e+16"),
            (1.5e16, "1.5e+16"),
            (123_456_789_012_345.6, "123456789012345.6"),
            (5e-324, "5e-324"),
            (f64::MAX, "1.7976931348623157e+308"),
            (f64::NAN, "nan"),
            (f64::NEG_INFINITY, "-inf"),
        ] {
            assert_eq!(float_repr(x), s, "{x:e}");
        }
    }

    #[test]
    fn decimal_from_float_str_keeps_pythons_scale() {
        for (x, s) in [
            (1.0, "1.0"),
            (0.5, "0.5"),
            (26.0, "26.0"),
            (12.5, "12.5"),
            (0.00001, "0.00001"),
            (1e16, "10000000000000000"),
            (-0.0, "-0.0"),
            (-1.5, "-1.5"),
        ] {
            assert_eq!(
                decimal_from_float_str(x).map(|d| d.to_string()),
                Ok(s.to_owned())
            );
        }
        assert!(decimal_from_float_str(f64::NAN).is_err());
        assert!(decimal_from_float_str(1e300).is_err());
        assert_eq!(
            wide_from_float_str(1e-30).map(|d| d.to_string()),
            Ok("0.000000000000000000000000000001".to_owned())
        );
    }
}
