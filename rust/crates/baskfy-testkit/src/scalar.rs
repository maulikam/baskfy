//! Scalar decoders (Decimal, date, datetime, time) and the generic [`decode_value`] walk.

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime};
use polars::prelude::{DataFrame, Series};
use rust_decimal::Decimal;
use serde_json::Value;

use crate::error::TestkitError;
use crate::frame::{decode_frame, decode_series, is_frame, is_series};

fn as_str<'a>(v: &'a Value, what: &str) -> Result<&'a str, TestkitError> {
    v.as_str()
        .ok_or_else(|| TestkitError::decode(what, format!("expected a string, got {v}")))
}

/// A golden Decimal string (`"5.10"`) as a `rust_decimal::Decimal`, scale preserved.
pub fn decimal(v: &Value) -> Result<Decimal, TestkitError> {
    let s = as_str(v, "decimal")?;
    Decimal::from_str_exact(s).map_err(|e| TestkitError::decode("decimal", format!("{s:?}: {e}")))
}

/// A golden ISO day (`"2026-09-02"`).
pub fn date(v: &Value) -> Result<NaiveDate, TestkitError> {
    let s = as_str(v, "date")?;
    NaiveDate::parse_from_str(s, "%Y-%m-%d")
        .map_err(|e| TestkitError::decode("date", format!("{s:?}: {e}")))
}

/// A golden aware datetime (`"2026-10-27T12:41:00+05:30"`).
pub fn datetime(v: &Value) -> Result<DateTime<FixedOffset>, TestkitError> {
    let s = as_str(v, "datetime")?;
    DateTime::parse_from_rfc3339(s)
        .map_err(|e| TestkitError::decode("datetime", format!("{s:?}: {e}")))
}

/// A golden naive datetime (`"2026-09-02T09:31:00"`), exchange-local IST by convention.
pub fn naive_datetime(v: &Value) -> Result<NaiveDateTime, TestkitError> {
    let s = as_str(v, "naive_datetime")?;
    NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S%.f")
        .map_err(|e| TestkitError::decode("naive_datetime", format!("{s:?}: {e}")))
}

/// A golden time of day (`"14:00:00"`).
pub fn time(v: &Value) -> Result<NaiveTime, TestkitError> {
    let s = as_str(v, "time")?;
    NaiveTime::parse_from_str(s, "%H:%M:%S%.f")
        .map_err(|e| TestkitError::decode("time", format!("{s:?}: {e}")))
}

/// A golden value decoded without a schema. Strings are classified by shape, so this is a
/// *structural* decode for harness checks and debugging; a port reads its inputs with the
/// typed accessors on [`crate::Golden`] instead.
#[derive(Debug, Clone)]
pub enum Decoded {
    /// JSON `null` (Python `None`, NaN or inf).
    Null,
    /// A boolean.
    Bool(bool),
    /// An integer.
    Int(i64),
    /// An unsigned integer above `i64::MAX`.
    UInt(u64),
    /// A float.
    Float(f64),
    /// A string that parses as a plain decimal (`-?digits[.digits]`).
    Decimal(Decimal),
    /// A decimal-shaped string Python's `Decimal` printed but `rust_decimal` cannot hold
    /// (scale above 28, or a mantissa above 96 bits) — e.g. a 28-significant-digit quotient
    /// below 0.1. Kept as its exact text; grading is byte-exact on the string, so a port must
    /// still print it. Listed by [`wide_decimals`] so none goes unnoticed (DECISIONS-RS R1.3).
    WideDecimal(String),
    /// A string shaped `YYYY-MM-DD`.
    Date(NaiveDate),
    /// A string shaped like an RFC 3339 datetime with an offset.
    DateTime(DateTime<FixedOffset>),
    /// A string shaped like a datetime without an offset.
    NaiveDateTime(NaiveDateTime),
    /// A string shaped `HH:MM:SS`.
    Time(NaiveTime),
    /// Any other string.
    Str(String),
    /// A polars frame object.
    Frame(DataFrame),
    /// A polars series object.
    Series(Series),
    /// A list.
    List(Vec<Decoded>),
    /// An object, keys in golden (sorted) order.
    Map(Vec<(String, Decoded)>),
}

fn looks_decimal(s: &str) -> bool {
    let body = s.strip_prefix('-').unwrap_or(s);
    let mut parts = body.splitn(2, '.');
    let int = parts.next().unwrap_or("");
    let frac = parts.next();
    !int.is_empty()
        && int.bytes().all(|b| b.is_ascii_digit())
        && frac.is_none_or(|f| !f.is_empty() && f.bytes().all(|b| b.is_ascii_digit()))
}

fn classify(s: &str, at: &str) -> Result<Decoded, TestkitError> {
    let v = Value::String(s.to_owned());
    let b = s.as_bytes();
    let digit = |i: usize| b.get(i).is_some_and(u8::is_ascii_digit);
    let date_prefix = b.len() >= 10
        && (0..4).all(digit)
        && b[4] == b'-'
        && digit(5)
        && digit(6)
        && b[7] == b'-'
        && digit(8)
        && digit(9);
    if date_prefix && b.len() == 10 {
        return date(&v)
            .map(Decoded::Date)
            .map_err(|e| TestkitError::decode(at, e.to_string()));
    }
    if date_prefix && b.get(10) == Some(&b'T') {
        if let Ok(dt) = datetime(&v) {
            return Ok(Decoded::DateTime(dt));
        }
        return naive_datetime(&v)
            .map(Decoded::NaiveDateTime)
            .map_err(|e| TestkitError::decode(at, e.to_string()));
    }
    let time_shape = b.len() >= 8
        && digit(0)
        && digit(1)
        && b[2] == b':'
        && digit(3)
        && digit(4)
        && b[5] == b':';
    if time_shape && let Ok(t) = time(&v) {
        return Ok(Decoded::Time(t));
    }
    if looks_decimal(s) {
        return match decimal(&v) {
            Ok(d) => Ok(Decoded::Decimal(d)),
            Err(_) if beyond_rust_decimal(s) => Ok(Decoded::WideDecimal(s.to_owned())),
            Err(e) => Err(TestkitError::decode(at, e.to_string())),
        };
    }
    Ok(Decoded::Str(s.to_owned()))
}

/// A well-formed decimal string whose scale exceeds 28 or whose digits exceed 96 bits:
/// valid for Python's `Decimal`, unrepresentable in `rust_decimal`.
fn beyond_rust_decimal(s: &str) -> bool {
    let body = s.strip_prefix('-').unwrap_or(s);
    let (int, frac) = body.split_once('.').unwrap_or((body, ""));
    let digits = format!("{int}{frac}");
    let significant = digits.trim_start_matches('0');
    frac.len() > 28
        || significant.len() > 29
        || significant
            .parse::<u128>()
            .is_ok_and(|m| m > (1u128 << 96) - 1)
}

/// Every decimal-shaped string in a golden subtree that `rust_decimal` cannot hold, with its
/// JSON path. These are the values a `rust_decimal` port cannot reproduce byte-exactly.
pub fn wide_decimals(v: &Value) -> Vec<(String, String)> {
    fn go(v: &Value, at: &str, out: &mut Vec<(String, String)>) {
        match v {
            Value::String(s)
                if looks_decimal(s) && decimal(v).is_err() && beyond_rust_decimal(s) =>
            {
                out.push((at.to_owned(), s.clone()));
            }
            Value::Array(xs) => xs
                .iter()
                .enumerate()
                .for_each(|(i, x)| go(x, &format!("{at}[{i}]"), out)),
            Value::Object(m) => m.iter().for_each(|(k, x)| go(x, &format!("{at}.{k}"), out)),
            _ => {}
        }
    }
    let mut out = Vec::new();
    go(v, "$", &mut out);
    out
}

/// Decode a whole golden subtree: frames and series become polars objects, date/time/decimal
/// shaped strings are parsed (an unparseable one is an error), everything else is kept.
pub fn decode_value(v: &Value) -> Result<Decoded, TestkitError> {
    walk(v, "$")
}

fn walk(v: &Value, at: &str) -> Result<Decoded, TestkitError> {
    Ok(match v {
        Value::Null => Decoded::Null,
        Value::Bool(b) => Decoded::Bool(*b),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Decoded::Int(i)
            } else if let Some(u) = n.as_u64() {
                Decoded::UInt(u)
            } else if let Some(f) = n.as_f64() {
                Decoded::Float(f)
            } else {
                return Err(TestkitError::decode(
                    at,
                    format!("unrepresentable number {n}"),
                ));
            }
        }
        Value::String(s) => classify(s, at)?,
        Value::Array(items) => Decoded::List(
            items
                .iter()
                .enumerate()
                .map(|(i, x)| walk(x, &format!("{at}[{i}]")))
                .collect::<Result<_, _>>()?,
        ),
        Value::Object(_) if is_frame(v) => {
            Decoded::Frame(decode_frame(v).map_err(|e| TestkitError::decode(at, e.to_string()))?)
        }
        Value::Object(_) if is_series(v) => Decoded::Series(
            decode_series("values", v).map_err(|e| TestkitError::decode(at, e.to_string()))?,
        ),
        Value::Object(map) => Decoded::Map(
            map.iter()
                .map(|(k, x)| Ok((k.clone(), walk(x, &format!("{at}.{k}"))?)))
                .collect::<Result<_, TestkitError>>()?,
        ),
    })
}
