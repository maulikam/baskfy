//! Python's value semantics as the options and FO desks use them over the desk's Postgres
//! adapter (`app.analytics.pg`): `numeric` arrives as a `float`, a `date` as its ISO string, a
//! `timestamptz` aware, `jsonb` parsed, an array as a list; the modules then read every figure
//! as `Decimal(str(value))` and quantize it to its storage precision (house rule 8).
//!
//! Shared by the `options` and `fno` lanes (one owner; both are this file's).

use std::str::FromStr;

use baskfy_core::decimal::{Rounding, WideDecimal};
use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, TimeZone};
use rust_decimal::Decimal;

use crate::storage::{DbValue, Param, Row};
use crate::wire::{PyDict, PyValue};

/// Asia/Kolkata, fixed +05:30.
#[must_use]
pub fn ist() -> FixedOffset {
    crate::clock::ist()
}

/// `str(d)`: Python's `Decimal.__str__` (scientific for small exponents, `"0.00"` kept).
#[must_use]
pub fn dstr(d: Decimal) -> String {
    WideDecimal::from(d).to_sci_string()
}

/// `d.quantize(Decimal(10) ** exp)` under the default context (`ROUND_HALF_EVEN`), the scale
/// kept as Python keeps it (`Decimal("12.3").quantize(Decimal("0.01"))` is `12.30`).
#[must_use]
pub fn quantize(d: Decimal, exp: i64) -> Decimal {
    WideDecimal::from(d)
        .quantize(exp, Rounding::HalfEven)
        .ok()
        .and_then(|q| q.to_decimal())
        .unwrap_or(d)
}

/// `d.quantize(Decimal("0.01"))`.
#[must_use]
pub fn q2(d: Decimal) -> Decimal {
    quantize(d, -2)
}

/// `a / b` in Python's default context (28 significant digits, half-even).
///
/// # Errors
/// Division by zero (`decimal.DivisionByZero` / `InvalidOperation`).
pub fn div(a: Decimal, b: Decimal) -> Result<Decimal, String> {
    baskfy_core::fno::config::py::div(a, b).map_err(|e| e.message())
}

/// `Decimal(str(float))`.
#[must_use]
pub fn dec_from_float(x: f64) -> Option<Decimal> {
    baskfy_core::py::decimal_from_float_str(x).ok()
}

/// `Decimal(str(text))`, Python's parser (`"1E+2"`, `" 1.5 "` refused as Python refuses
/// surrounding junk except whitespace).
#[must_use]
pub fn dec_from_str(text: &str) -> Option<Decimal> {
    let t = text.trim();
    WideDecimal::from_str(t)
        .ok()
        .and_then(|w| w.to_decimal())
        .or_else(|| Decimal::from_str(t).ok())
        .or_else(|| Decimal::from_scientific(t).ok())
}

/// `_dec(value)`: `None` for `None`/`""`, else `Decimal(str(value))`.
#[must_use]
pub fn dec(value: &DbValue) -> Option<Decimal> {
    match value {
        DbValue::Null => None,
        DbValue::Int(i) => Some(Decimal::from(*i)),
        DbValue::Float(f) => dec_from_float(*f),
        DbValue::Bool(b) => Some(Decimal::from(i64::from(*b))),
        DbValue::Text(s) | DbValue::Json(s) => {
            if s.is_empty() {
                None
            } else {
                dec_from_str(s)
            }
        }
        _ => None,
    }
}

/// `_dec(value)` for a parsed-JSON value (a `detail` field).
#[must_use]
pub fn dec_py(value: Option<&PyValue>) -> Option<Decimal> {
    match value? {
        PyValue::None => None,
        PyValue::Int(i) => Some(Decimal::from(*i)),
        PyValue::Float(f) => dec_from_float(*f),
        PyValue::Bool(b) => Some(Decimal::from(i64::from(*b))),
        PyValue::Str(s) => {
            if s.is_empty() {
                None
            } else {
                dec_from_str(s)
            }
        }
        _ => None,
    }
}

/// `_px(value)`: `_dec` quantized to the cent.
#[must_use]
pub fn px(value: &DbValue) -> Option<Decimal> {
    dec(value).map(q2)
}

/// `_px` for a JSON value.
#[must_use]
pub fn px_py(value: Option<&PyValue>) -> Option<Decimal> {
    dec_py(value).map(q2)
}

/// A column by name (`row["name"]`), `Null` when absent.
#[must_use]
pub fn col<'a>(row: &'a Row, name: &str) -> &'a DbValue {
    static NULL: DbValue = DbValue::Null;
    row.get(name).unwrap_or(&NULL)
}

/// `int(row[name])`.
#[must_use]
pub fn int(value: &DbValue) -> i64 {
    match value {
        DbValue::Int(i) => *i,
        DbValue::Bool(b) => i64::from(*b),
        // Python's int() of a float truncates.
        #[allow(clippy::cast_possible_truncation)]
        DbValue::Float(f) => *f as i64,
        DbValue::Text(s) => s.trim().parse().unwrap_or(0),
        _ => 0,
    }
}

/// `str(value)` for a text-like column (`None` → `"None"`, as Python prints it).
#[must_use]
pub fn text(value: &DbValue) -> String {
    match value {
        DbValue::Null => "None".to_owned(),
        DbValue::Text(s) | DbValue::Json(s) => s.clone(),
        DbValue::Int(i) => i.to_string(),
        DbValue::Float(f) => baskfy_core::py::float_repr(*f),
        DbValue::Bool(b) => if *b { "True" } else { "False" }.to_owned(),
        DbValue::Aware(t) => iso_aware(t),
        DbValue::Naive(t) => iso_naive(t, 'T'),
        DbValue::Bytes(b) => format!("b'{}'", String::from_utf8_lossy(b)),
    }
}

/// `str` or `None`.
#[must_use]
pub fn opt_text(value: &DbValue) -> Option<String> {
    if value.is_null() {
        None
    } else {
        Some(text(value))
    }
}

/// `bool(value)`.
#[must_use]
pub fn truthy(value: &DbValue) -> bool {
    match value {
        DbValue::Null => false,
        DbValue::Bool(b) => *b,
        DbValue::Int(i) => *i != 0,
        DbValue::Float(f) => *f != 0.0,
        DbValue::Text(s) | DbValue::Json(s) => !s.is_empty(),
        DbValue::Bytes(b) => !b.is_empty(),
        DbValue::Aware(_) | DbValue::Naive(_) => true,
    }
}

/// `datetime.isoformat()` of an aware value (microseconds only when non-zero).
#[must_use]
pub fn iso_aware(t: &DateTime<FixedOffset>) -> String {
    if t.timestamp_subsec_micros() == 0 {
        t.format("%Y-%m-%dT%H:%M:%S%:z").to_string()
    } else {
        t.format("%Y-%m-%dT%H:%M:%S%.6f%:z").to_string()
    }
}

/// `datetime.isoformat(sep)` of a naive value.
#[must_use]
pub fn iso_naive(t: &NaiveDateTime, sep: char) -> String {
    let fmt = if t.and_utc().timestamp_subsec_micros() == 0 {
        format!("%Y-%m-%d{sep}%H:%M:%S")
    } else {
        format!("%Y-%m-%d{sep}%H:%M:%S%.6f")
    };
    t.format(&fmt).to_string()
}

/// `dt.datetime.fromisoformat(text)` (Python 3.11+: `T` or space, offsets `+05:30`/`Z`).
#[must_use]
pub fn parse_iso(text: &str) -> Option<(NaiveDateTime, Option<FixedOffset>)> {
    let t = text.trim();
    if let Ok(a) = DateTime::parse_from_rfc3339(t) {
        return Some((a.naive_local(), Some(*a.offset())));
    }
    for fmt in [
        "%Y-%m-%d %H:%M:%S%.f%:z",
        "%Y-%m-%dT%H:%M:%S%.f%:z",
        "%Y-%m-%d %H:%M:%S%.f%#z",
        "%Y-%m-%dT%H:%M:%S%.f%#z",
    ] {
        if let Ok(a) = DateTime::parse_from_str(t, fmt) {
            return Some((a.naive_local(), Some(*a.offset())));
        }
    }
    for fmt in [
        "%Y-%m-%d %H:%M:%S%.f",
        "%Y-%m-%dT%H:%M:%S%.f",
        "%Y-%m-%dT%H:%M",
    ] {
        if let Ok(n) = NaiveDateTime::parse_from_str(t, fmt) {
            return Some((n, None));
        }
    }
    NaiveDate::parse_from_str(t, "%Y-%m-%d")
        .ok()
        .map(|d| (d.and_time(NaiveTime::MIN), None))
}

/// `_aware(at)`: an ISO string or a datetime, as an aware datetime (naive → IST).
#[must_use]
pub fn aware(value: &DbValue) -> Option<DateTime<FixedOffset>> {
    match value {
        DbValue::Aware(t) => Some(*t),
        DbValue::Naive(n) => ist().from_local_datetime(n).single(),
        DbValue::Text(s) => {
            let (n, off) = parse_iso(s)?;
            match off {
                Some(o) => o.from_local_datetime(&n).single(),
                None => ist().from_local_datetime(&n).single(),
            }
        }
        _ => None,
    }
}

/// `_aware` for a JSON value.
#[must_use]
pub fn aware_py(value: Option<&PyValue>) -> Option<DateTime<FixedOffset>> {
    match value? {
        PyValue::Str(s) => aware(&DbValue::Text(s.clone())),
        PyValue::DateTime(baskfy_providers::records::PyDateTime::Aware(t)) => Some(*t),
        PyValue::DateTime(baskfy_providers::records::PyDateTime::Naive(n)) => {
            ist().from_local_datetime(n).single()
        }
        _ => None,
    }
}

/// `_opt_day(value)`: `None`/`""` → `None`, a datetime's date, else the ISO prefix.
#[must_use]
pub fn opt_day(value: &DbValue) -> Option<NaiveDate> {
    match value {
        DbValue::Null => None,
        DbValue::Aware(t) => Some(t.date_naive()),
        DbValue::Naive(t) => Some(t.date()),
        DbValue::Text(s) | DbValue::Json(s) => {
            if s.is_empty() {
                None
            } else {
                NaiveDate::parse_from_str(s.get(..10).unwrap_or(s), "%Y-%m-%d").ok()
            }
        }
        _ => None,
    }
}

/// `_opt_day` for a JSON value.
#[must_use]
pub fn opt_day_py(value: Option<&PyValue>) -> Option<NaiveDate> {
    match value? {
        PyValue::None => None,
        PyValue::Date(d) => Some(*d),
        PyValue::DateTime(baskfy_providers::records::PyDateTime::Aware(t)) => Some(t.date_naive()),
        PyValue::DateTime(baskfy_providers::records::PyDateTime::Naive(t)) => Some(t.date()),
        PyValue::Str(s) if s.is_empty() => None,
        PyValue::Str(s) => NaiveDate::parse_from_str(s.get(..10).unwrap_or(s), "%Y-%m-%d").ok(),
        other => {
            let s = other.py_str();
            NaiveDate::parse_from_str(s.get(..10).unwrap_or(&s), "%Y-%m-%d").ok()
        }
    }
}

/// `_json(value)`: a dict, or `{}`.
#[must_use]
pub fn json_obj(value: &DbValue) -> PyDict {
    match value {
        DbValue::Json(s) | DbValue::Text(s) if !s.is_empty() => {
            match PyValue::from_json(s.as_bytes()) {
                Ok(PyValue::Map(m)) => m,
                _ => PyDict::new(),
            }
        }
        _ => PyDict::new(),
    }
}

/// A Postgres array column read back as Python's list of ints (`{1,2,3}` → `[1, 2, 3]`).
#[must_use]
pub fn int_array(value: &DbValue) -> Vec<i64> {
    match value {
        DbValue::Text(s) | DbValue::Json(s) => s
            .trim_matches(|c| c == '{' || c == '}' || c == '[' || c == ']')
            .split(',')
            .filter_map(|p| p.trim().parse().ok())
            .collect(),
        _ => Vec::new(),
    }
}

/// psycopg's adaptation of a Python `list` of ints (`ARRAY[1,2,3]`), inlined into the SQL —
/// ints only, so no quoting is involved; `'{}'` for an empty list, as psycopg sends it.
#[must_use]
pub fn int_array_sql(ids: &[i64]) -> String {
    if ids.is_empty() {
        return "'{}'::int8[]".to_owned();
    }
    let inner: Vec<String> = ids.iter().map(ToString::to_string).collect();
    format!("ARRAY[{}]", inner.join(","))
}

/// psycopg's adaptation of a Python `list` of `str` as an inline `ARRAY['a','b']` literal
/// (standard-conforming quoting, as `storage::pg_literal` does).
#[must_use]
pub fn text_array_sql(items: &[String]) -> String {
    if items.is_empty() {
        return "'{}'::text[]".to_owned();
    }
    let inner: Vec<String> = items
        .iter()
        .map(|s| format!("'{}'", s.replace('\0', "").replace('\'', "''")))
        .collect();
    format!("ARRAY[{}]", inner.join(","))
}

/// A `Decimal` parameter (psycopg sends `numeric`).
#[must_use]
pub fn pdec(d: Option<Decimal>) -> Param {
    d.map_or(Param::Null, Param::Decimal)
}

/// `_jsonable(value)` then `json.dumps` (Decimal → str, date/datetime → isoformat).
#[must_use]
pub fn dumps(value: &PyValue) -> String {
    crate::wire::py_json_dumps(&jsonable(value)).unwrap_or_else(|_| "null".to_owned())
}

/// `_jsonable(value)`: dates and datetimes to ISO strings, recursively (a `Decimal` is already
/// a `Str` here: lanes put `str(d)` in, as `_jsonable` would).
#[must_use]
pub fn jsonable(value: &PyValue) -> PyValue {
    use baskfy_providers::records::PyDateTime;
    match value {
        PyValue::Date(d) => PyValue::Str(d.format("%Y-%m-%d").to_string()),
        PyValue::DateTime(PyDateTime::Aware(t)) => PyValue::Str(iso_aware(t)),
        PyValue::DateTime(PyDateTime::Naive(t)) => PyValue::Str(iso_naive(t, 'T')),
        PyValue::List(items) => PyValue::List(items.iter().map(jsonable).collect()),
        PyValue::Map(m) => {
            let mut out = PyDict::new();
            for (k, v) in m.iter() {
                out.insert(k, jsonable(v));
            }
            PyValue::Map(out)
        }
        other => other.clone(),
    }
}

/// A `Decimal` as the JSON-ready value `_jsonable` makes of it.
#[must_use]
pub fn vdec(d: Option<Decimal>) -> PyValue {
    d.map_or(PyValue::None, |d| PyValue::Str(dstr(d)))
}

/// `_s(value)` for an optional string.
#[must_use]
pub fn vstr(s: Option<String>) -> PyValue {
    s.map_or(PyValue::None, PyValue::Str)
}

/// The JSON value a stored column becomes in a view dict (`dict(row)` then `_jsonable`).
#[must_use]
pub fn db_to_py(value: &DbValue) -> PyValue {
    match value {
        DbValue::Null => PyValue::None,
        DbValue::Int(i) => PyValue::Int(*i),
        DbValue::Float(f) => PyValue::Float(*f),
        DbValue::Text(s) => PyValue::Str(s.clone()),
        DbValue::Bool(b) => PyValue::Bool(*b),
        DbValue::Aware(t) => PyValue::Str(iso_aware(t)),
        DbValue::Naive(t) => PyValue::Str(iso_naive(t, 'T')),
        DbValue::Json(s) => PyValue::from_json(s.as_bytes()).unwrap_or(PyValue::None),
        DbValue::Bytes(b) => PyValue::Bytes(b.clone()),
    }
}

/// `int(value)` of a JSON value (`int(d.get("lot_size") or 1)` callers pass the fallback).
#[must_use]
pub fn py_int(value: Option<&PyValue>, fallback: i64) -> i64 {
    match value {
        Some(PyValue::Int(i)) if *i != 0 => *i,
        #[allow(clippy::cast_possible_truncation)]
        Some(PyValue::Float(f)) if *f != 0.0 => *f as i64,
        Some(PyValue::Str(s)) if !s.is_empty() => s.trim().parse().unwrap_or(fallback),
        Some(PyValue::Bool(true)) => 1,
        _ => fallback,
    }
}

/// `str(value)` of a JSON value (`None` → `"None"`).
#[must_use]
pub fn py_text(value: Option<&PyValue>) -> String {
    value.map_or_else(|| "None".to_owned(), PyValue::py_str)
}

/// A naive datetime at `time` on `day`, in IST.
#[must_use]
pub fn ist_at(day: NaiveDate, time: NaiveTime) -> Option<DateTime<FixedOffset>> {
    ist().from_local_datetime(&day.and_time(time)).single()
}
