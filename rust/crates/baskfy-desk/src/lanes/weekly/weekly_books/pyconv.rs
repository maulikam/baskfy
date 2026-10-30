//! Python's own conversions where the reconciler lets them raise: `int(x)`, `Decimal(str(x))`,
//! `d[key]` and `obj.get` on a non-dict — each with the exception text Python's would carry
//! (`"<Type>: <message>"`), so a pass that fails fails with the same words (DECISIONS-RS R8.WB1).

use rust_decimal::Decimal;

use crate::wire::{PyDict, PyValue};

/// `type(x).__name__`.
#[must_use]
pub const fn type_name(v: &PyValue) -> &'static str {
    match v {
        PyValue::None => "NoneType",
        PyValue::Bool(_) => "bool",
        PyValue::Int(_) => "int",
        PyValue::Float(_) => "float",
        PyValue::Str(_) => "str",
        PyValue::Bytes(_) => "bytes",
        PyValue::Date(_) => "date",
        PyValue::DateTime(_) => "datetime",
        PyValue::List(_) => "list",
        PyValue::Map(_) => "dict",
    }
}

/// `AttributeError` for `x.get(...)` on something that is not a dict.
#[must_use]
pub fn no_get(v: &PyValue) -> String {
    format!(
        "AttributeError: '{}' object has no attribute 'get'",
        type_name(v)
    )
}

/// `KeyError: 'key'`.
#[must_use]
pub fn key_error(key: &str) -> String {
    format!("KeyError: {}", PyValue::Str(key.to_owned()).py_repr())
}

/// `d[key]`.
///
/// # Errors
/// `KeyError` when absent.
pub fn index<'a>(d: &'a PyDict, key: &str) -> Result<&'a PyValue, String> {
    d.get(key).ok_or_else(|| key_error(key))
}

/// The digits of `int(text)`'s grammar: surrounding whitespace, one sign, ASCII digits with
/// single `_` between digits.
fn int_text(text: &str) -> Option<i64> {
    let t = text.trim_matches(|c: char| c.is_whitespace());
    let (neg, body) = match t.as_bytes().first() {
        Some(b'-') => (true, &t[1..]),
        Some(b'+') => (false, &t[1..]),
        _ => (false, t),
    };
    if body.is_empty() || body.starts_with('_') || body.ends_with('_') || body.contains("__") {
        return None;
    }
    let digits: String = body.chars().filter(|c| *c != '_').collect();
    if !digits.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    let n: i64 = digits.parse().ok()?;
    Some(if neg { -n } else { n })
}

/// `int(x)`.
///
/// # Errors
/// Python's `ValueError` / `TypeError` / `OverflowError` text.
#[allow(clippy::cast_possible_truncation)]
pub fn int(v: &PyValue) -> Result<i64, String> {
    match v {
        PyValue::Int(i) => Ok(*i),
        PyValue::Bool(b) => Ok(i64::from(*b)),
        PyValue::Float(f) if f.is_nan() => {
            Err("ValueError: cannot convert float NaN to integer".to_owned())
        }
        PyValue::Float(f) if f.is_infinite() => {
            Err("OverflowError: cannot convert float infinity to integer".to_owned())
        }
        PyValue::Float(f) => {
            let t = f.trunc();
            // Beyond i64 Python still answers (a big int); no desk quantity is that large.
            if t.abs() >= 9.223_372_036_854_775e18 {
                return Err("OverflowError: int too large to convert".to_owned());
            }
            Ok(t as i64)
        }
        PyValue::Str(s) => int_text(s).ok_or_else(|| {
            format!(
                "ValueError: invalid literal for int() with base 10: {}",
                v.py_repr()
            )
        }),
        PyValue::Bytes(b) => std::str::from_utf8(b)
            .ok()
            .and_then(int_text)
            .ok_or_else(|| {
                format!(
                    "ValueError: invalid literal for int() with base 10: {}",
                    v.py_repr()
                )
            }),
        other => Err(format!(
            "TypeError: int() argument must be a string, a bytes-like object or a real number, \
             not '{}'",
            type_name(other)
        )),
    }
}

/// `int(x or 0)` (`None`, `0`, `""`, `[]`, … are `0`).
///
/// # Errors
/// As [`int`].
pub fn int_or_zero(v: Option<&PyValue>) -> Result<i64, String> {
    match v {
        Some(x) if x.truthy() => int(x),
        _ => Ok(0),
    }
}

/// `Decimal(str(x))`, as far as the desk can hold it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PyDecimal {
    /// A finite value.
    Finite(Decimal),
    /// `Decimal('NaN')` (quiet).
    NaN,
    /// `Decimal('sNaN')`: any arithmetic on it raises `InvalidOperation`.
    SNaN,
    /// `Decimal('Infinity')` (`true`) / `Decimal('-Infinity')` (`false`).
    Inf(bool),
}

/// `decimal.InvalidOperation` for a string the constructor refuses.
pub const CONVERSION_SYNTAX: &str = "InvalidOperation: [<class 'decimal.ConversionSyntax'>]";

/// `Decimal(text)`: whitespace stripped, every `_` dropped, `Inf`/`Infinity`/`NaN`/`sNaN` in
/// any case.
///
/// # Errors
/// [`CONVERSION_SYNTAX`].
pub fn decimal_text(text: &str) -> Result<PyDecimal, String> {
    let stripped = text
        .trim_matches(|c: char| c.is_whitespace())
        .replace('_', "");
    let t = stripped.as_str();
    let (neg, body) = match t.as_bytes().first() {
        Some(b'-') => (true, &t[1..]),
        Some(b'+') => (false, &t[1..]),
        _ => (false, t),
    };
    let lower = body.to_ascii_lowercase();
    match lower.as_str() {
        "inf" | "infinity" => return Ok(PyDecimal::Inf(!neg)),
        _ => {}
    }
    let digits_only = |s: &str| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit());
    if let Some(payload) = lower.strip_prefix("snan") {
        if payload.is_empty() || digits_only(payload) {
            return Ok(PyDecimal::SNaN);
        }
        return Err(CONVERSION_SYNTAX.to_owned());
    }
    if let Some(payload) = lower.strip_prefix("nan") {
        if payload.is_empty() || digits_only(payload) {
            return Ok(PyDecimal::NaN);
        }
        return Err(CONVERSION_SYNTAX.to_owned());
    }
    // `Decimal.__new__` strips and then drops every `_` (`value.strip().replace("_", "")`).
    let clean: String = body.chars().filter(|c| *c != '_').collect();
    let (mantissa, exponent) = match clean.find(['e', 'E']) {
        Some(at) => (&clean[..at], Some(&clean[at + 1..])),
        None => (clean.as_str(), None),
    };
    let (int_part, frac_part) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let all_digits = |s: &str| s.bytes().all(|b| b.is_ascii_digit());
    if (int_part.is_empty() && frac_part.is_empty())
        || !all_digits(int_part)
        || !all_digits(frac_part)
    {
        return Err(CONVERSION_SYNTAX.to_owned());
    }
    if let Some(e) = exponent {
        let e = e.strip_prefix(['+', '-']).unwrap_or(e);
        if e.is_empty() || !all_digits(e) {
            return Err(CONVERSION_SYNTAX.to_owned());
        }
    }
    let mut signed = String::from(if neg { "-" } else { "" });
    signed.push_str(if int_part.is_empty() { "0" } else { int_part });
    if !frac_part.is_empty() {
        signed.push('.');
        signed.push_str(frac_part);
    }
    if let Some(e) = exponent {
        signed.push('e');
        signed.push_str(e);
    }
    signed
        .parse::<Decimal>()
        .or_else(|_| Decimal::from_scientific(&signed))
        .map(PyDecimal::Finite)
        // Valid to Python but beyond 28 digits or rust_decimal's exponent range: no desk price
        // is; refuse rather than round silently.
        .map_err(|_| format!("OverflowError: Decimal {signed} is outside the desk's range"))
}

/// `Decimal(str(x))`.
///
/// # Errors
/// As [`decimal_text`].
pub fn decimal(v: &PyValue) -> Result<PyDecimal, String> {
    let text = match v {
        PyValue::Float(f) => baskfy_providers::py::repr_float(*f),
        other => other.py_str(),
    };
    decimal_text(&text)
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn int_matches_python() {
        assert_eq!(int(&PyValue::Str(" 1_000 ".into())), Ok(1000));
        assert_eq!(int(&PyValue::Float(-2.9)), Ok(-2));
        assert_eq!(
            int(&PyValue::Str("abc".into())),
            Err("ValueError: invalid literal for int() with base 10: 'abc'".into())
        );
        assert_eq!(
            int(&PyValue::Str("1.5".into())).unwrap_err(),
            "ValueError: invalid literal for int() with base 10: '1.5'"
        );
        assert_eq!(
            int(&PyValue::None).unwrap_err(),
            "TypeError: int() argument must be a string, a bytes-like object or a real number, \
             not 'NoneType'"
        );
        assert_eq!(int_or_zero(Some(&PyValue::Str(String::new()))), Ok(0));
    }

    #[test]
    fn decimal_matches_python() {
        assert_eq!(
            decimal(&PyValue::Str("100.00".into())),
            Ok(PyDecimal::Finite(Decimal::new(10000, 2)))
        );
        assert_eq!(
            decimal(&PyValue::Str(" 1_0.5 ".into())),
            Ok(PyDecimal::Finite(Decimal::new(105, 1)))
        );
        assert_eq!(
            decimal(&PyValue::Str("-Infinity".into())),
            Ok(PyDecimal::Inf(false))
        );
        assert_eq!(decimal(&PyValue::Str("nan".into())), Ok(PyDecimal::NaN));
        assert_eq!(
            decimal(&PyValue::Str("abc".into())).unwrap_err(),
            CONVERSION_SYNTAX
        );
        assert_eq!(
            decimal(&PyValue::Bool(true)).unwrap_err(),
            CONVERSION_SYNTAX
        );
        assert_eq!(
            decimal(&PyValue::Float(1e-7)),
            Ok(PyDecimal::Finite(Decimal::new(1, 7)))
        );
        assert_eq!(
            decimal(&PyValue::Str("_1__0".into())),
            Ok(PyDecimal::Finite(Decimal::new(10, 0)))
        );
        assert_eq!(
            decimal(&PyValue::Str("1e".into())).unwrap_err(),
            CONVERSION_SYNTAX
        );
        assert_eq!(
            decimal(&PyValue::Str("+.5e+3".into())),
            Ok(PyDecimal::Finite(Decimal::new(500, 0)))
        );
    }
}
