//! Values across the store boundary, as `app/swing_desk.py` moves them: `_dec` (a NUMERIC
//! column as `Decimal`, re-quantised to its storage scale), `_date`, `_stamp` (a timestamptz as
//! IST, a naive value read as IST), `_bind` (a Python value as both drivers accept it —
//! strings for the typed values, decimals quantised `ROUND_HALF_UP` to the column's scale) and
//! `_jsonable` (`Decimal` → `format(x, "f")`, dates → ISO).

use std::str::FromStr;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, TimeZone};
use rust_decimal::{Decimal, RoundingStrategy};

use super::rules::{dec_of_float, quantize};
use crate::clock::ist;
use crate::storage::{DbValue, Param, Row};
use crate::wire::{PyDict, PyValue};

/// `_COLUMN_SCALE`: storage scales from `0028_swing.py` (house rule 8).
#[must_use]
pub fn column_scale(column: &str) -> Option<u32> {
    Some(match column {
        "entry_avg" | "exit_avg" | "price" => 4,
        "initial_stop"
        | "stop"
        | "gtt_trigger"
        | "trigger"
        | "r_multiple"
        | "pnl_inr"
        | "risk_inr"
        | "position_value"
        | "range_high"
        | "range_low"
        | "low_of_day"
        | "last_price"
        | "entry"
        | "total_risk_inr"
        | "total_new_exposure_inr"
        | "sleeve_capital_inr"
        | "max_position_pct" => 2,
        "risk_per_trade_pct" => 3,
        _ => return None,
    })
}

/// `Decimal(str(value))` for whatever the driver returned.
#[must_use]
pub fn raw_dec(value: &DbValue) -> Option<Decimal> {
    match value {
        DbValue::Null => None,
        DbValue::Int(i) => Some(Decimal::from(*i)),
        DbValue::Bool(b) => Some(Decimal::from(i64::from(*b))),
        DbValue::Float(f) => Some(dec_of_float(*f)),
        DbValue::Text(s) | DbValue::Json(s) => super::rules::dec(s.trim()),
        _ => None,
    }
}

/// `_dec(value, column)`.
#[must_use]
pub fn dec(value: Option<&DbValue>, column: &str) -> Option<Decimal> {
    let number = raw_dec(value?)?;
    Some(match column_scale(column) {
        Some(scale) => quantize(number, scale, RoundingStrategy::MidpointNearestEven),
        None => number,
    })
}

/// `_date(value)`.
#[must_use]
pub fn date(value: Option<&DbValue>) -> Option<NaiveDate> {
    match value? {
        DbValue::Null => None,
        DbValue::Aware(t) => Some(t.date_naive()),
        DbValue::Naive(t) => Some(t.date()),
        DbValue::Text(s) => NaiveDate::parse_from_str(s.get(..10)?, "%Y-%m-%d").ok(),
        _ => None,
    }
}

/// `datetime.fromisoformat` on the forms the desk's stores hold.
#[must_use]
pub fn parse_iso(text: &str) -> Option<DateTime<FixedOffset>> {
    let t = text.trim();
    if let Ok(v) = DateTime::parse_from_rfc3339(t) {
        return Some(v);
    }
    for fmt in [
        "%Y-%m-%d %H:%M:%S%.f%:z",
        "%Y-%m-%dT%H:%M:%S%.f%:z",
        "%Y-%m-%d %H:%M:%S%.f%#z",
    ] {
        if let Ok(v) = DateTime::parse_from_str(t, fmt) {
            return Some(v);
        }
    }
    for fmt in [
        "%Y-%m-%d %H:%M:%S%.f",
        "%Y-%m-%dT%H:%M:%S%.f",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
    ] {
        if let Ok(v) = NaiveDateTime::parse_from_str(t, fmt) {
            return ist().from_local_datetime(&v).single();
        }
    }
    NaiveDate::parse_from_str(t, "%Y-%m-%d")
        .ok()
        .and_then(|d| d.and_hms_opt(0, 0, 0))
        .and_then(|v| ist().from_local_datetime(&v).single())
}

/// `_stamp(value)`: aware IST; a naive value is taken to be IST.
#[must_use]
pub fn stamp(value: Option<&DbValue>) -> Option<DateTime<FixedOffset>> {
    let moment = match value? {
        DbValue::Null => return None,
        DbValue::Aware(t) => *t,
        DbValue::Naive(t) => ist().from_local_datetime(t).single()?,
        DbValue::Text(s) => parse_iso(s)?,
        _ => return None,
    };
    Some(moment.with_timezone(&ist()))
}

/// `int(row[col])`.
#[must_use]
pub fn int(value: Option<&DbValue>) -> i64 {
    match value {
        Some(DbValue::Int(i)) => *i,
        Some(DbValue::Bool(b)) => i64::from(*b),
        #[allow(clippy::cast_possible_truncation)]
        Some(DbValue::Float(f)) => f.trunc() as i64,
        Some(DbValue::Text(s)) => s.trim().parse().unwrap_or(0),
        _ => 0,
    }
}

/// `int(row[col]) if row[col] is not None else None`.
#[must_use]
pub fn opt_int(value: Option<&DbValue>) -> Option<i64> {
    match value {
        None | Some(DbValue::Null) => None,
        v => Some(int(v)),
    }
}

/// `bool(row[col])`.
#[must_use]
pub fn boolean(value: Option<&DbValue>) -> bool {
    match value {
        Some(DbValue::Bool(b)) => *b,
        Some(DbValue::Int(i)) => *i != 0,
        Some(DbValue::Float(f)) => *f != 0.0,
        Some(DbValue::Text(s)) => !s.is_empty(),
        _ => false,
    }
}

/// `str(row[col])`.
#[must_use]
pub fn text(value: Option<&DbValue>) -> String {
    match value {
        Some(DbValue::Text(s) | DbValue::Json(s)) => s.clone(),
        Some(DbValue::Int(i)) => i.to_string(),
        Some(DbValue::Float(f)) => baskfy_core::py::float_repr(*f),
        Some(DbValue::Bool(b)) => if *b { "True" } else { "False" }.to_owned(),
        Some(DbValue::Aware(t)) => t.format("%Y-%m-%d %H:%M:%S%:z").to_string(),
        Some(DbValue::Naive(t)) => t.format("%Y-%m-%d %H:%M:%S").to_string(),
        Some(DbValue::Null) | None => "None".to_owned(),
        Some(DbValue::Bytes(_)) => String::new(),
    }
}

/// `str(row[col]) if row[col] is not None else None`.
#[must_use]
pub fn opt_text(value: Option<&DbValue>) -> Option<String> {
    match value {
        None | Some(DbValue::Null) => None,
        v => Some(text(v)),
    }
}

/// A row's column.
#[must_use]
pub fn col<'a>(row: &'a Row, name: &str) -> Option<&'a DbValue> {
    row.get(name)
}

/// A Python value as the store binds it (`_bind`).
#[derive(Debug, Clone, PartialEq)]
pub enum Val {
    /// `None`.
    Null,
    /// `bool`.
    Bool(bool),
    /// `int`.
    Int(i64),
    /// `Decimal`.
    Dec(Decimal),
    /// `str`.
    Text(String),
    /// `date`.
    Date(NaiveDate),
    /// An aware `datetime`.
    Stamp(DateTime<FixedOffset>),
}

/// `datetime.isoformat()`.
#[must_use]
pub fn iso_dt(t: &DateTime<FixedOffset>) -> String {
    super::notify::iso(t)
}

/// `date.isoformat()`.
#[must_use]
pub fn iso_date(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

/// `_bind(value, column)`.
#[must_use]
pub fn bind(value: &Val, column: &str) -> Param {
    match value {
        Val::Null => Param::Null,
        Val::Bool(b) => Param::Bool(*b),
        Val::Int(i) => Param::Int(*i),
        Val::Dec(d) => {
            let d = column_scale(column).map_or(*d, |s| {
                quantize(*d, s, RoundingStrategy::MidpointAwayFromZero)
            });
            Param::Text(d.to_string())
        }
        Val::Text(s) => Param::Text(s.clone()),
        Val::Date(d) => Param::Text(iso_date(*d)),
        Val::Stamp(t) => Param::Text(iso_dt(t)),
    }
}

/// `_jsonable(Decimal)`: `format(x, "f")`.
#[must_use]
pub fn pv_dec(d: Option<Decimal>) -> PyValue {
    d.map_or(PyValue::None, |v| PyValue::Str(v.to_string()))
}

/// `_jsonable(date)`.
#[must_use]
pub fn pv_date(d: Option<NaiveDate>) -> PyValue {
    d.map_or(PyValue::None, |v| PyValue::Str(iso_date(v)))
}

/// `_jsonable(datetime)`.
#[must_use]
pub fn pv_stamp(t: Option<&DateTime<FixedOffset>>) -> PyValue {
    t.map_or(PyValue::None, |v| PyValue::Str(iso_dt(v)))
}

/// A string or `None`.
#[must_use]
pub fn pv_str(s: Option<&str>) -> PyValue {
    s.map_or(PyValue::None, |v| PyValue::Str(v.to_owned()))
}

/// An int or `None`.
#[must_use]
pub fn pv_int(i: Option<i64>) -> PyValue {
    i.map_or(PyValue::None, PyValue::Int)
}

/// A JSON column (`_json_column`) as the view's value, in its own key order.
#[must_use]
pub fn json_column(value: Option<&DbValue>) -> Option<PyValue> {
    match value? {
        DbValue::Text(s) | DbValue::Json(s) => PyValue::from_json(s.as_bytes()).ok(),
        _ => None,
    }
}

/// `uuid.UUID(str(text))` accepts it.
#[must_use]
pub fn is_uuid(text: &str) -> bool {
    let hex = text.replace("urn:", "").replace("uuid:", "");
    let hex = hex.trim_matches(|c| c == '{' || c == '}').replace('-', "");
    if hex.chars().count() != 32 {
        return false;
    }
    // int(hex, 16): surrounding whitespace, a sign, and single underscores between digits.
    let body = hex.trim();
    let body = body.strip_prefix(['+', '-']).unwrap_or(body);
    if body.is_empty() || body.starts_with('_') || body.ends_with('_') || body.contains("__") {
        return false;
    }
    body.chars().all(|c| c.is_ascii_hexdigit() || c == '_')
}

/// Decimal from text for tests and fixtures.
#[must_use]
pub fn d(text: &str) -> Decimal {
    Decimal::from_str(text).unwrap_or_default()
}

/// An ordered dict from pairs.
#[must_use]
pub fn map(pairs: Vec<(&str, PyValue)>) -> PyValue {
    let mut m = PyDict::new();
    for (k, v) in pairs {
        m.insert(k, v);
    }
    PyValue::Map(m)
}
