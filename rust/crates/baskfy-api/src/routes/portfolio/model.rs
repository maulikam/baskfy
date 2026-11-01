//! pydantic v2's lax (Python-mode) validation of the JSON bodies this lane's write routes take —
//! the field types [`crate::body::validate_model`] does not cover: `int`, `Decimal`, `Literal`
//! enums, lists and nested models.
//!
//! FastAPI validates `json.loads(body)` against the route's model with
//! `model_validate(..., from_attributes=True)`, so the rules are Python mode's, checked against
//! pydantic-core 2.x in the decile-blueprint venv (`tests` below pin them):
//!
//! * `int`: an `int` (and a `bool`, which *is* an `int` in Python mode); a float with no fractional
//!   part (`int_from_float` otherwise, `finite_number` for NaN/inf, `int_parsing_size` beyond
//!   `i64`); a string by the lax rules of [`crate::routes::common::lax_int`]; anything else is
//!   `int_type`.
//! * `Decimal`: an `int`, a float (as `Decimal(repr(f))`), or a string (`Decimal(s)`: surrounding
//!   whitespace and `_` between digits allowed); NaN and infinities are `finite_number`; a `bool`,
//!   list or dict is `decimal_type`; an unreadable string is `decimal_parsing`.
//! * a nested model: a dict, else `model_attributes_type`; its fields in declaration order, then
//!   each extra key in arrival order (`extra="forbid"`).
//!
//! Locations are joined with `.` exactly as `app._field_path` joins them (`holdings.0.symbol`).

use baskfy_core::decimal::WideDecimal;
use baskfy_db::OrderedJson;

use crate::problems::{ErrorEntry, Problem, invalid_screen_definition};

/// The errors one validation collects, in pydantic's order.
#[derive(Debug, Default)]
pub(crate) struct Errors(pub(crate) Vec<ErrorEntry>);

impl Errors {
    /// Records one error at `loc`.
    pub(crate) fn push(&mut self, loc: &str, message: &str, kind: &str) {
        self.0.push(vec![
            ("field".to_owned(), OrderedJson::Str(loc.to_owned())),
            ("message".to_owned(), OrderedJson::Str(message.to_owned())),
            ("type".to_owned(), OrderedJson::Str(kind.to_owned())),
        ]);
    }

    /// The 400 FastAPI answers, or `Ok` when nothing failed.
    pub(crate) fn finish(self) -> Result<(), Problem> {
        if self.0.is_empty() {
            Ok(())
        } else {
            Err(invalid_screen_definition(self.0))
        }
    }
}

/// `loc` joined with one more part.
pub(crate) fn at(prefix: &str, part: &str) -> String {
    if prefix.is_empty() {
        part.to_owned()
    } else {
        format!("{prefix}.{part}")
    }
}

/// A JSON object's entries, or `model_attributes_type` at `loc` (`<root>` for the body itself).
pub(crate) fn object<'a>(
    value: &'a OrderedJson,
    loc: &str,
    errors: &mut Errors,
) -> Option<&'a [(String, OrderedJson)]> {
    if let OrderedJson::Object(entries) = value {
        Some(entries)
    } else {
        errors.push(
            if loc.is_empty() { "<root>" } else { loc },
            "Input should be a valid dictionary or object to extract fields from",
            "model_attributes_type",
        );
        None
    }
}

/// The value under `name` (the parser already kept the last of duplicate keys).
pub(crate) fn get<'a>(entries: &'a [(String, OrderedJson)], name: &str) -> Option<&'a OrderedJson> {
    entries.iter().find(|(k, _)| k == name).map(|(_, v)| v)
}

/// `extra="forbid"`: every key not in `declared`, in arrival order.
pub(crate) fn forbid_extra(
    entries: &[(String, OrderedJson)],
    declared: &[&str],
    prefix: &str,
    errors: &mut Errors,
) {
    for (key, _) in entries {
        if !declared.contains(&key.as_str()) {
            errors.push(
                &at(prefix, key),
                "Extra inputs are not permitted",
                "extra_forbidden",
            );
        }
    }
}

fn plural(n: usize, unit: &str) -> String {
    if n == 1 {
        format!("{n} {unit}")
    } else {
        format!("{n} {unit}s")
    }
}

/// A `str` field with `min_length`/`max_length`; `None` after recording an error.
pub(crate) fn string(
    value: &OrderedJson,
    loc: &str,
    min: Option<usize>,
    max: Option<usize>,
    errors: &mut Errors,
) -> Option<String> {
    let OrderedJson::Str(s) = value else {
        errors.push(loc, "Input should be a valid string", "string_type");
        return None;
    };
    let n = s.chars().count();
    if let Some(min) = min
        && n < min
    {
        errors.push(
            loc,
            &format!("String should have at least {}", plural(min, "character")),
            "string_too_short",
        );
        return None;
    }
    if let Some(max) = max
        && n > max
    {
        errors.push(
            loc,
            &format!("String should have at most {}", plural(max, "character")),
            "string_too_long",
        );
        return None;
    }
    Some(s.clone())
}

/// A required `str` field.
pub(crate) fn required_string(
    entries: &[(String, OrderedJson)],
    name: &str,
    prefix: &str,
    bounds: (Option<usize>, Option<usize>),
    errors: &mut Errors,
) -> Option<String> {
    let loc = at(prefix, name);
    match get(entries, name) {
        None => {
            errors.push(&loc, "Field required", "missing");
            None
        }
        Some(v) => string(v, &loc, bounds.0, bounds.1, errors),
    }
}

/// An `int` as Python holds it, saturated into `i128` beyond any bound this lane checks.
pub(crate) type PyInt = i128;

/// Bounds on an `int` or `Decimal` field (`Field(gt=, ge=, le=)`), as integers.
#[derive(Debug, Clone, Copy, Default)]
pub(crate) struct Bounds {
    pub(crate) gt: Option<i64>,
    pub(crate) ge: Option<i64>,
    pub(crate) le: Option<i64>,
}

fn saturate(digits: &str, negative: bool) -> PyInt {
    let magnitude = digits.parse::<i128>().unwrap_or(i128::MAX);
    if negative { -magnitude } else { magnitude }
}

/// pydantic's lax `int` over a JSON value; `None` after recording an error.
pub(crate) fn int(
    value: &OrderedJson,
    loc: &str,
    bounds: Bounds,
    errors: &mut Errors,
) -> Option<PyInt> {
    let parsed: PyInt = match value {
        OrderedJson::Bool(b) => PyInt::from(*b),
        OrderedJson::Int(i) => PyInt::from(*i),
        OrderedJson::UInt(u) => PyInt::from(*u),
        OrderedJson::BigInt(s) => match s.strip_prefix('-') {
            Some(rest) => saturate(rest, true),
            None => saturate(s, false),
        },
        OrderedJson::Float(f) => {
            if !f.is_finite() {
                errors.push(loc, "Input should be a finite number", "finite_number");
                return None;
            }
            if f.fract() != 0.0 {
                errors.push(
                    loc,
                    "Input should be a valid integer, got a number with a fractional part",
                    "int_from_float",
                );
                return None;
            }
            // pydantic-core converts through an i64: a whole float beyond it is refused.
            #[allow(clippy::cast_precision_loss)]
            let limit = i64::MAX as f64;
            if *f >= limit || *f < -limit {
                errors.push(
                    loc,
                    "Unable to parse input string as an integer, exceeded maximum size",
                    "int_parsing_size",
                );
                return None;
            }
            #[allow(clippy::cast_possible_truncation)]
            let whole = *f as i64;
            PyInt::from(whole)
        }
        OrderedJson::Str(s) => match crate::routes::common::lax_int(s) {
            Some(p) => p.saturated(),
            None => {
                errors.push(
                    loc,
                    "Input should be a valid integer, unable to parse string as an integer",
                    "int_parsing",
                );
                return None;
            }
        },
        _ => {
            errors.push(loc, "Input should be a valid integer", "int_type");
            return None;
        }
    };
    if let Some(gt) = bounds.gt
        && parsed <= PyInt::from(gt)
    {
        errors.push(
            loc,
            &format!("Input should be greater than {gt}"),
            "greater_than",
        );
        return None;
    }
    if let Some(ge) = bounds.ge
        && parsed < PyInt::from(ge)
    {
        errors.push(
            loc,
            &format!("Input should be greater than or equal to {ge}"),
            "greater_than_equal",
        );
        return None;
    }
    if let Some(le) = bounds.le
        && parsed > PyInt::from(le)
    {
        errors.push(
            loc,
            &format!("Input should be less than or equal to {le}"),
            "less_than_equal",
        );
        return None;
    }
    Some(parsed)
}

/// An `int | None = None` field: `Ok(None)` absent or `null`, `Err(())` after an error.
pub(crate) fn optional_int(
    entries: &[(String, OrderedJson)],
    name: &str,
    prefix: &str,
    bounds: Bounds,
    errors: &mut Errors,
) -> Result<Option<PyInt>, ()> {
    match get(entries, name) {
        None | Some(OrderedJson::Null) => Ok(None),
        Some(v) => int(v, &at(prefix, name), bounds, errors)
            .map(Some)
            .ok_or(()),
    }
}

/// Python's `str.isspace()` for one character (Rust's `White_Space` plus `\x1c`–`\x1f`).
pub(crate) fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Python's `str.strip()`.
pub(crate) fn py_strip(s: &str) -> &str {
    s.trim_matches(py_isspace)
}

/// What `Decimal(text)` made of a string.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum PyDecimal {
    /// A finite value.
    Finite(WideDecimal),
    /// `NaN`, `sNaN`, `Infinity` (any case, any sign).
    Special,
    /// `InvalidOperation` (`ConversionSyntax`).
    Invalid,
}

/// `decimal.Decimal(text)` for the ASCII grammar: surrounding whitespace, a sign, digits with
/// single `_` between digits, an optional point, an optional exponent; or a special value.
/// Non-ASCII digits (which CPython also accepts) are read as invalid (DECISIONS-RS R6.21).
pub(crate) fn py_decimal(text: &str) -> PyDecimal {
    let t = py_strip(text);
    let (_, body) = match t.as_bytes().first() {
        Some(b'-' | b'+') => t.split_at(1),
        _ => ("", t),
    };
    let lower = body.to_ascii_lowercase();
    if ["inf", "infinity", "nan", "snan"].contains(&lower.as_str())
        || ((lower.starts_with("nan") || lower.starts_with("snan"))
            && lower
                .trim_start_matches('s')
                .trim_start_matches("nan")
                .bytes()
                .all(|b| b.is_ascii_digit()))
    {
        return PyDecimal::Special;
    }
    // Underscores only between two digits.
    let bytes = t.as_bytes();
    let mut cleaned = String::with_capacity(t.len());
    for (i, &b) in bytes.iter().enumerate() {
        if b == b'_' {
            let before = i > 0 && bytes[i - 1].is_ascii_digit();
            let after = bytes.get(i + 1).is_some_and(u8::is_ascii_digit);
            if !(before && after) {
                return PyDecimal::Invalid;
            }
            continue;
        }
        cleaned.push(char::from(b));
    }
    if !t.is_ascii() {
        return PyDecimal::Invalid;
    }
    // The exponent may not be empty, nor carry its own whitespace.
    if let Some(i) = cleaned.find(['e', 'E']) {
        let exp = &cleaned[i + 1..];
        let digits = exp.strip_prefix(['+', '-']).unwrap_or(exp);
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return PyDecimal::Invalid;
        }
    }
    match cleaned.parse::<WideDecimal>() {
        Ok(d) => PyDecimal::Finite(d),
        Err(_) => PyDecimal::Invalid,
    }
}

/// pydantic's lax `Decimal` over a JSON value; `None` after recording an error.
pub(crate) fn decimal(
    value: &OrderedJson,
    loc: &str,
    gt_zero: bool,
    errors: &mut Errors,
) -> Option<WideDecimal> {
    let parsed = match value {
        OrderedJson::Int(i) => WideDecimal::from(*i),
        OrderedJson::UInt(u) => u.to_string().parse().unwrap_or_default(),
        OrderedJson::BigInt(s) => s.parse().unwrap_or_default(),
        OrderedJson::Float(f) => {
            if !f.is_finite() {
                errors.push(loc, "Input should be a finite number", "finite_number");
                return None;
            }
            match py_decimal(&baskfy_core::py::float_repr(*f)) {
                PyDecimal::Finite(d) => d,
                _ => {
                    errors.push(loc, "Input should be a valid decimal", "decimal_parsing");
                    return None;
                }
            }
        }
        OrderedJson::Str(s) => match py_decimal(s) {
            PyDecimal::Finite(d) => d,
            PyDecimal::Special => {
                errors.push(loc, "Input should be a finite number", "finite_number");
                return None;
            }
            PyDecimal::Invalid => {
                errors.push(loc, "Input should be a valid decimal", "decimal_parsing");
                return None;
            }
        },
        _ => {
            errors.push(
                loc,
                "Decimal input should be an integer, float, string or Decimal object",
                "decimal_type",
            );
            return None;
        }
    };
    if gt_zero && parsed <= WideDecimal::zero() {
        errors.push(loc, "Input should be greater than 0", "greater_than");
        return None;
    }
    Some(parsed)
}

/// A `Decimal | None = None` field: `Ok(None)` absent or `null`, `Err(())` after an error.
pub(crate) fn optional_decimal(
    entries: &[(String, OrderedJson)],
    name: &str,
    prefix: &str,
    gt_zero: bool,
    errors: &mut Errors,
) -> Result<Option<WideDecimal>, ()> {
    match get(entries, name) {
        None | Some(OrderedJson::Null) => Ok(None),
        Some(v) => decimal(v, &at(prefix, name), gt_zero, errors)
            .map(Some)
            .ok_or(()),
    }
}

/// A `Literal`/`StrEnum` field: one of `values`, else pydantic's `enum` error.
pub(crate) fn choice(
    entries: &[(String, OrderedJson)],
    name: &str,
    prefix: &str,
    values: &[&'static str],
    errors: &mut Errors,
) -> Option<&'static str> {
    let loc = at(prefix, name);
    let Some(value) = get(entries, name) else {
        errors.push(&loc, "Field required", "missing");
        return None;
    };
    if let OrderedJson::Str(s) = value
        && let Some(v) = values.iter().find(|v| **v == s.as_str())
    {
        return Some(v);
    }
    let quoted: Vec<String> = values.iter().map(|v| format!("'{v}'")).collect();
    let listed = match quoted.split_last() {
        Some((last, rest)) if !rest.is_empty() => format!("{} or {last}", rest.join(", ")),
        _ => quoted.join(""),
    };
    errors.push(&loc, &format!("Input should be {listed}"), "enum");
    None
}

/// A list field: the items, or `list_type` (a JSON `null` too: the default only covers absence).
pub(crate) fn list<'a>(
    value: &'a OrderedJson,
    loc: &str,
    min_length: Option<usize>,
    errors: &mut Errors,
) -> Option<&'a [OrderedJson]> {
    let OrderedJson::Array(items) = value else {
        errors.push(loc, "Input should be a valid list", "list_type");
        return None;
    };
    if let Some(min) = min_length
        && items.len() < min
    {
        errors.push(
            loc,
            &format!(
                "List should have at least {} after validation, not {}",
                plural(min, "item"),
                items.len()
            ),
            "too_short",
        );
        return None;
    }
    Some(items)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn d(s: &str) -> WideDecimal {
        s.parse().unwrap()
    }

    #[test]
    fn decimal_strings_read_as_pythons_decimal() {
        // Each checked with `Decimal(s)` in CPython 3.12.
        for (s, want) in [
            ("1.5", PyDecimal::Finite(d("1.5"))),
            ("  2.5 ", PyDecimal::Finite(d("2.5"))),
            ("1_000", PyDecimal::Finite(d("1000"))),
            ("1e3", PyDecimal::Finite(d("1E+3"))),
            ("-0", PyDecimal::Finite(d("-0"))),
            ("+.5", PyDecimal::Finite(d("0.5"))),
            ("5.", PyDecimal::Finite(d("5"))),
            ("NaN", PyDecimal::Special),
            ("-Infinity", PyDecimal::Special),
            ("inf", PyDecimal::Special),
            ("sNaN12", PyDecimal::Special),
            ("1__0", PyDecimal::Invalid),
            ("_1", PyDecimal::Invalid),
            ("1e", PyDecimal::Invalid),
            ("abc", PyDecimal::Invalid),
            ("", PyDecimal::Invalid),
            (".", PyDecimal::Invalid),
            ("1 2", PyDecimal::Invalid),
        ] {
            assert_eq!(py_decimal(s), want, "{s:?}");
        }
        assert_eq!(
            match py_decimal("5.") {
                PyDecimal::Finite(v) => v.to_sci_string(),
                _ => String::new(),
            },
            "5"
        );
    }

    #[test]
    fn pydantic_errors_for_ints_decimals_and_enums() {
        // Pinned from pydantic 2.13 in the decile-blueprint venv.
        let mut e = Errors::default();
        assert_eq!(
            int(&OrderedJson::Bool(true), "a", Bounds::default(), &mut e),
            Some(1)
        );
        assert_eq!(
            int(&OrderedJson::Float(2.0), "a", Bounds::default(), &mut e),
            Some(2)
        );
        assert!(e.0.is_empty());
        assert_eq!(
            int(&OrderedJson::Float(1.5), "p", Bounds::default(), &mut e),
            None
        );
        assert_eq!(
            int(&OrderedJson::Float(1e20), "q", Bounds::default(), &mut e),
            None
        );
        assert_eq!(
            int(
                &OrderedJson::Int(0),
                "g",
                Bounds {
                    gt: Some(0),
                    ..Bounds::default()
                },
                &mut e
            ),
            None
        );
        assert_eq!(decimal(&OrderedJson::Bool(true), "b", false, &mut e), None);
        assert_eq!(
            decimal(&OrderedJson::Float(0.1 + 0.2), "f", false, &mut e).map(|v| v.to_sci_string()),
            Some("0.30000000000000004".to_owned())
        );
        assert_eq!(
            decimal(&OrderedJson::Float(1e20), "f", false, &mut e).map(|v| v.to_sci_string()),
            Some("1E+20".to_owned())
        );
        let entries = vec![("kind".to_owned(), OrderedJson::Str("capital".to_owned()))];
        assert_eq!(
            choice(&entries, "kind", "", &["CAPITAL", "MONITORING"], &mut e),
            None
        );
        let kinds: Vec<String> =
            e.0.iter()
                .map(|entry| match &entry[2].1 {
                    OrderedJson::Str(s) => s.clone(),
                    _ => String::new(),
                })
                .collect();
        assert_eq!(
            kinds,
            [
                "int_from_float",
                "int_parsing_size",
                "greater_than",
                "decimal_type",
                "enum"
            ]
        );
        assert_eq!(
            e.0[4][1].1,
            OrderedJson::Str("Input should be 'CAPITAL' or 'MONITORING'".to_owned())
        );
    }
}
