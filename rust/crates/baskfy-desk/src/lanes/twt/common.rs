//! What the TWT and VBT sleeves share (lane twt-vbt): Python's row dicts ([`Rec`], [`Val`]),
//! the `_dec`/`_date`/`_stamp`/`_jsonable` helpers both desks carry a copy of, FastAPI's form
//! parsing and its two error shapes, `app.reconcile`'s entry refusal, `app.exit_rules`, and the
//! `last_price` read.
//!
//! **Rows are dicts, on purpose.** `PgTwtStore` and `PgVbtStore` hand dicts to the execute
//! modules, the page and `/data` (which dumps them whole, in key order), and the Python tests'
//! memory stores are dicts too. [`Rec`] is that dict — ordered, typed values, Python's lookups —
//! so the port reads line for line against the Python and the JSON keeps Python's key order.

use std::str::FromStr;

use axum::body::Body;
use axum::http::{HeaderValue, Response, StatusCode};
use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, TimeZone, Timelike, Utc};
use rust_decimal::Decimal;

use baskfy_core::decimal::{Rounding, WideDecimal};

use crate::app::AppState;
use crate::storage::{DbValue, Param, StorageError};
use crate::wire::{PyDict, PyValue, orjson_dumps};

// =============================================================================================
// Values and rows
// =============================================================================================

/// One Python value as the sleeves' stores and views hold it.
#[derive(Debug, Clone, PartialEq)]
pub enum Val {
    /// `None`.
    None,
    /// `bool`.
    Bool(bool),
    /// `int`.
    Int(i64),
    /// `float`.
    Float(f64),
    /// `decimal.Decimal`.
    Dec(Decimal),
    /// `str`.
    Str(String),
    /// `datetime.date`.
    Date(NaiveDate),
    /// An aware `datetime.datetime`.
    Stamp(DateTime<FixedOffset>),
    /// A naive `datetime.datetime` (a raw driver value the store did not normalise).
    Naive(NaiveDateTime),
    /// A value that came over the wire already Python-shaped (a gateway result, a JSON column).
    Py(PyValue),
    /// `list`.
    List(Vec<Val>),
    /// `dict`.
    Map(Rec),
}

impl Val {
    /// `None` → [`Val::None`], else the mapped value.
    pub fn opt<T>(v: Option<T>, f: impl FnOnce(T) -> Self) -> Self {
        v.map_or(Self::None, f)
    }

    /// `value is None`.
    #[must_use]
    pub const fn is_none(&self) -> bool {
        matches!(self, Self::None) || matches!(self, Self::Py(PyValue::None))
    }

    /// Python truthiness.
    #[must_use]
    pub fn truthy(&self) -> bool {
        match self {
            Self::None => false,
            Self::Bool(b) => *b,
            Self::Int(i) => *i != 0,
            Self::Float(f) => *f != 0.0,
            Self::Dec(d) => !d.is_zero(),
            Self::Str(s) => !s.is_empty(),
            Self::Date(_) | Self::Stamp(_) | Self::Naive(_) => true,
            Self::Py(p) => p.truthy(),
            Self::List(l) => !l.is_empty(),
            Self::Map(m) => !m.0.is_empty(),
        }
    }

    /// `str(value)`.
    #[must_use]
    pub fn py_str(&self) -> String {
        match self {
            Self::None => "None".to_owned(),
            Self::Bool(b) => if *b { "True" } else { "False" }.to_owned(),
            Self::Int(i) => i.to_string(),
            Self::Float(f) => baskfy_core::py::float_repr(*f),
            Self::Dec(d) => dstr(*d),
            Self::Str(s) => s.clone(),
            Self::Date(d) => d.format("%Y-%m-%d").to_string(),
            Self::Stamp(t) => py_isoformat(t, ' '),
            Self::Naive(t) => naive_isoformat(t, ' '),
            Self::Py(p) => p.py_str(),
            Self::List(_) | Self::Map(_) => format!("{:?}", self),
        }
    }

    /// `int(value)` for an int-like value.
    #[must_use]
    pub fn as_int(&self) -> Option<i64> {
        match self {
            Self::Int(i) => Some(*i),
            Self::Bool(b) => Some(i64::from(*b)),
            Self::Dec(d) => d.trunc().try_into().ok(),
            Self::Str(s) => s.trim().parse().ok(),
            Self::Py(PyValue::Int(i)) => Some(*i),
            Self::Py(PyValue::Str(s)) => s.trim().parse().ok(),
            _ => None,
        }
    }
}

impl From<&str> for Val {
    fn from(v: &str) -> Self {
        Self::Str(v.to_owned())
    }
}
impl From<String> for Val {
    fn from(v: String) -> Self {
        Self::Str(v)
    }
}
impl From<i64> for Val {
    fn from(v: i64) -> Self {
        Self::Int(v)
    }
}
impl From<bool> for Val {
    fn from(v: bool) -> Self {
        Self::Bool(v)
    }
}
impl From<Decimal> for Val {
    fn from(v: Decimal) -> Self {
        Self::Dec(v)
    }
}
impl From<NaiveDate> for Val {
    fn from(v: NaiveDate) -> Self {
        Self::Date(v)
    }
}
impl From<DateTime<FixedOffset>> for Val {
    fn from(v: DateTime<FixedOffset>) -> Self {
        Self::Stamp(v)
    }
}
impl<T: Into<Self>> From<Option<T>> for Val {
    fn from(v: Option<T>) -> Self {
        v.map_or(Self::None, Into::into)
    }
}

/// A Python `dict`: ordered keys, Python's lookups.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Rec(pub Vec<(String, Val)>);

impl Rec {
    /// An empty dict.
    #[must_use]
    pub const fn new() -> Self {
        Self(Vec::new())
    }

    /// Build from pairs, in order.
    #[must_use]
    pub fn of<K: Into<String>>(pairs: impl IntoIterator<Item = (K, Val)>) -> Self {
        let mut r = Self::new();
        for (k, v) in pairs {
            r.set(k, v);
        }
        r
    }

    /// `d[key] = value` (an existing key keeps its position, as in Python).
    pub fn set(&mut self, key: impl Into<String>, value: Val) {
        let key = key.into();
        if let Some(slot) = self.0.iter_mut().find(|(k, _)| *k == key) {
            slot.1 = value;
        } else {
            self.0.push((key, value));
        }
    }

    /// `d.update(other)`.
    pub fn update(&mut self, other: &Self) {
        for (k, v) in &other.0 {
            self.set(k.clone(), v.clone());
        }
    }

    /// `d.get(key)`.
    #[must_use]
    pub fn get(&self, key: &str) -> Option<&Val> {
        self.0.iter().find(|(k, _)| k == key).map(|(_, v)| v)
    }

    /// `d.get(key)` with `None` for a missing key.
    #[must_use]
    pub fn val(&self, key: &str) -> Val {
        self.get(key).cloned().unwrap_or(Val::None)
    }

    /// `key in d`.
    #[must_use]
    pub fn contains(&self, key: &str) -> bool {
        self.get(key).is_some()
    }

    /// `int(d.get(key) or 0)`.
    #[must_use]
    pub fn int(&self, key: &str) -> i64 {
        self.get(key).and_then(Val::as_int).unwrap_or(0)
    }

    /// `d.get(key)` as an int, `None` when absent or `None`.
    #[must_use]
    pub fn opt_int(&self, key: &str) -> Option<i64> {
        self.get(key).filter(|v| !v.is_none()).and_then(Val::as_int)
    }

    /// `d.get(key)` as a Decimal (`_price`'s `Decimal(str(value))` for a float or a string).
    #[must_use]
    pub fn dec(&self, key: &str) -> Option<Decimal> {
        match self.get(key)? {
            Val::Dec(d) => Some(*d),
            Val::Int(i) => Some(Decimal::from(*i)),
            Val::Float(f) => baskfy_core::py::decimal_from_float_str(*f).ok(),
            Val::Str(s) => py_decimal(s),
            Val::Py(PyValue::Float(f)) => baskfy_core::py::decimal_from_float_str(*f).ok(),
            Val::Py(PyValue::Int(i)) => Some(Decimal::from(*i)),
            Val::Py(PyValue::Str(s)) => py_decimal(s),
            _ => None,
        }
    }

    /// `str(d.get(key))`, `"None"` for a missing key.
    #[must_use]
    pub fn s(&self, key: &str) -> String {
        self.val(key).py_str()
    }

    /// `d.get(key)` as text, `None` when absent or `None`.
    #[must_use]
    pub fn opt_str(&self, key: &str) -> Option<String> {
        match self.get(key)? {
            Val::None | Val::Py(PyValue::None) => None,
            other => Some(other.py_str()),
        }
    }

    /// `d.get(key)` as a date.
    #[must_use]
    pub fn date(&self, key: &str) -> Option<NaiveDate> {
        match self.get(key)? {
            Val::Date(d) => Some(*d),
            Val::Stamp(t) => Some(t.date_naive()),
            Val::Naive(t) => Some(t.date()),
            Val::Str(s) => NaiveDate::parse_from_str(s, "%Y-%m-%d").ok(),
            _ => None,
        }
    }

    /// `d.get(key)` as an aware datetime.
    #[must_use]
    pub fn stamp(&self, key: &str) -> Option<DateTime<FixedOffset>> {
        match self.get(key)? {
            Val::Stamp(t) => Some(*t),
            _ => None,
        }
    }

    /// `bool(d.get(key))`.
    #[must_use]
    pub fn truthy(&self, key: &str) -> bool {
        self.get(key).is_some_and(Val::truthy)
    }

    /// `d.get(key) is None` (missing counts).
    #[must_use]
    pub fn is_none(&self, key: &str) -> bool {
        self.get(key).is_none_or(Val::is_none)
    }
}

/// `Decimal(text)`, `None` where Python raises `InvalidOperation`.
#[must_use]
pub fn py_decimal(text: &str) -> Option<Decimal> {
    let t = text.trim();
    Decimal::from_str(t)
        .ok()
        .or_else(|| Decimal::from_scientific(t).ok())
}

/// `str(Decimal)`.
#[must_use]
pub fn dstr(d: Decimal) -> String {
    WideDecimal::from(d).to_sci_string()
}

/// `a / b` in Python's default decimal context.
///
/// # Errors
/// Division by zero, or a quotient `rust_decimal` cannot hold.
pub fn ddiv(a: Decimal, b: Decimal) -> Result<Decimal, String> {
    WideDecimal::from(a)
        .div(&WideDecimal::from(b))
        .and_then(|q| q.try_to_decimal())
        .map_err(|e| e.to_string())
}

/// `a * b` exactly (rounded to 28 digits, as Python's context would).
#[must_use]
pub fn dmul(a: Decimal, b: Decimal) -> Decimal {
    WideDecimal::from(a)
        .mul(&WideDecimal::from(b))
        .try_to_decimal()
        .unwrap_or_else(|_| a * b)
}

/// `d.quantize(Decimal("1e<exp>"))`, half-even (Python's default context).
#[must_use]
pub fn quantize(d: Decimal, exp: i64) -> Decimal {
    WideDecimal::from(d)
        .quantize(exp, Rounding::HalfEven)
        .and_then(|q| q.try_to_decimal())
        .unwrap_or(d)
}

/// `float(d)`.
#[must_use]
pub fn dfloat(d: Decimal) -> f64 {
    dstr(d).parse().unwrap_or(0.0)
}

// =============================================================================================
// The driver's values — `_dec`, `_date`, `_stamp`
// =============================================================================================

/// IST, `timezone(timedelta(hours=5, minutes=30))`.
#[must_use]
pub fn ist() -> FixedOffset {
    crate::clock::ist()
}

/// `_dec(value)`: `None` stays `None`; a number becomes `Decimal(str(value))`; an unparseable
/// value is `None`.
#[must_use]
pub fn db_dec(v: Option<&DbValue>) -> Option<Decimal> {
    match v? {
        DbValue::Null => None,
        DbValue::Float(f) => baskfy_core::py::decimal_from_float_str(*f).ok(),
        DbValue::Int(i) => Some(Decimal::from(*i)),
        DbValue::Bool(b) => {
            let _ = b;
            None
        }
        DbValue::Text(s) | DbValue::Json(s) => py_decimal(s),
        _ => None,
    }
}

/// `_date(value)`.
///
/// # Errors
/// A string that is not an ISO date (Python's `ValueError`).
pub fn db_date(v: Option<&DbValue>) -> Result<Option<NaiveDate>, StorageError> {
    Ok(match v {
        None | Some(DbValue::Null) => None,
        Some(DbValue::Aware(t)) => Some(t.date_naive()),
        Some(DbValue::Naive(t)) => Some(t.date()),
        Some(other) => {
            let text = db_text(other);
            Some(py_fromisoformat_date(&text).ok_or_else(|| {
                StorageError::Other(format!("ValueError: Invalid isoformat string: '{text}'"))
            })?)
        }
    })
}

/// `_stamp(value)`: an aware datetime as it came; a naive one (or a string) read as IST.
///
/// # Errors
/// A string that is not an ISO datetime.
pub fn db_stamp(v: Option<&DbValue>) -> Result<Option<DateTime<FixedOffset>>, StorageError> {
    Ok(match v {
        None | Some(DbValue::Null) => None,
        Some(DbValue::Aware(t)) => Some(*t),
        Some(DbValue::Naive(t)) => ist().from_local_datetime(t).single(),
        Some(other) => {
            let text = db_text(other);
            let naive = py_fromisoformat_naive(&text).ok_or_else(|| {
                StorageError::Other(format!("ValueError: Invalid isoformat string: '{text}'"))
            })?;
            ist().from_local_datetime(&naive).single()
        }
    })
}

/// A driver value as Python would `str()` it (for the raw columns the stores pass through).
#[must_use]
pub fn db_text(v: &DbValue) -> String {
    match v {
        DbValue::Null => "None".to_owned(),
        DbValue::Int(i) => i.to_string(),
        DbValue::Float(f) => baskfy_core::py::float_repr(*f),
        DbValue::Text(s) | DbValue::Json(s) => s.clone(),
        DbValue::Bytes(b) => String::from_utf8_lossy(b).into_owned(),
        DbValue::Bool(b) => if *b { "True" } else { "False" }.to_owned(),
        DbValue::Aware(t) => py_isoformat(t, ' '),
        DbValue::Naive(t) => naive_isoformat(t, ' '),
    }
}

/// A raw driver value as the row dict carries it (`row["note"]`).
#[must_use]
pub fn db_raw(v: Option<&DbValue>) -> Val {
    match v {
        None | Some(DbValue::Null) => Val::None,
        Some(DbValue::Int(i)) => Val::Int(*i),
        Some(DbValue::Float(f)) => Val::Float(*f),
        Some(DbValue::Text(s)) => Val::Str(s.clone()),
        Some(DbValue::Json(s)) => {
            PyValue::from_json(s.as_bytes()).map_or_else(|_| Val::Str(s.clone()), Val::Py)
        }
        Some(DbValue::Bytes(b)) => Val::Py(PyValue::Bytes(b.clone())),
        Some(DbValue::Bool(b)) => Val::Bool(*b),
        Some(DbValue::Aware(t)) => Val::Stamp(*t),
        Some(DbValue::Naive(t)) => Val::Naive(*t),
    }
}

/// `bool(row[col])` on a driver value (SQLite's 0/1, Postgres's bool).
#[must_use]
pub fn db_bool(v: Option<&DbValue>) -> bool {
    match v {
        None | Some(DbValue::Null) => false,
        Some(DbValue::Bool(b)) => *b,
        Some(DbValue::Int(i)) => *i != 0,
        Some(DbValue::Float(f)) => *f != 0.0,
        Some(DbValue::Text(s) | DbValue::Json(s)) => !s.is_empty(),
        Some(DbValue::Bytes(b)) => !b.is_empty(),
        Some(_) => true,
    }
}

/// `int(row[col] or 0)`.
#[must_use]
pub fn db_int(v: Option<&DbValue>) -> i64 {
    match v {
        Some(DbValue::Int(i)) => *i,
        Some(DbValue::Bool(b)) => i64::from(*b),
        #[allow(clippy::cast_possible_truncation)]
        Some(DbValue::Float(f)) => f.trunc() as i64,
        Some(DbValue::Text(s)) => s.trim().parse().unwrap_or(0),
        _ => 0,
    }
}

/// `date.fromisoformat` for the plain `YYYY-MM-DD` form (and a datetime string's date part,
/// which Python refuses — so does this).
#[must_use]
pub fn py_fromisoformat_date(text: &str) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(text, "%Y-%m-%d").ok()
}

/// `datetime.fromisoformat(text)` with any offset dropped (the stores `.replace(tzinfo=IST)`).
#[must_use]
pub fn py_fromisoformat_naive(text: &str) -> Option<NaiveDateTime> {
    if let Ok(t) = DateTime::parse_from_rfc3339(text) {
        return Some(t.naive_local());
    }
    for fmt in [
        "%Y-%m-%d %H:%M:%S%.f%:z",
        "%Y-%m-%dT%H:%M:%S%.f%:z",
        "%Y-%m-%d %H:%M:%S%.f%#z",
    ] {
        if let Ok(t) = DateTime::parse_from_str(text, fmt) {
            return Some(t.naive_local());
        }
    }
    for fmt in [
        "%Y-%m-%d %H:%M:%S%.f",
        "%Y-%m-%dT%H:%M:%S%.f",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
    ] {
        if let Ok(t) = NaiveDateTime::parse_from_str(text, fmt) {
            return Some(t);
        }
    }
    NaiveDate::parse_from_str(text, "%Y-%m-%d")
        .ok()
        .and_then(|d| d.and_hms_opt(0, 0, 0))
}

/// `datetime.isoformat(sep)` for an aware datetime.
#[must_use]
pub fn py_isoformat(t: &DateTime<FixedOffset>, sep: char) -> String {
    let base = naive_isoformat(&t.naive_local(), sep);
    let secs = t.offset().local_minus_utc();
    let sign = if secs < 0 { '-' } else { '+' };
    let a = secs.unsigned_abs();
    let (h, m, s) = (a / 3600, (a % 3600) / 60, a % 60);
    if s == 0 {
        format!("{base}{sign}{h:02}:{m:02}")
    } else {
        format!("{base}{sign}{h:02}:{m:02}:{s:02}")
    }
}

/// `datetime.isoformat(sep)` for a naive datetime.
#[must_use]
pub fn naive_isoformat(t: &NaiveDateTime, sep: char) -> String {
    let micros = t.nanosecond() / 1000;
    let head = format!("{}{sep}{}", t.format("%Y-%m-%d"), t.format("%H:%M:%S"));
    if micros == 0 {
        head
    } else {
        format!("{head}.{micros:06}")
    }
}

/// `stamp.astimezone(IST)`.
#[must_use]
pub fn to_ist(t: &DateTime<FixedOffset>) -> DateTime<FixedOffset> {
    t.with_timezone(&ist())
}

/// `stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)` — the IST day of a stored stamp.
#[must_use]
pub fn ist_day(t: &DateTime<FixedOffset>) -> NaiveDate {
    to_ist(t).date_naive()
}

/// A datetime in UTC as an aware value (for a clock reading).
#[must_use]
pub fn aware_utc(t: DateTime<Utc>) -> DateTime<FixedOffset> {
    t.fixed_offset()
}

// =============================================================================================
// `_jsonable` and the template's values
// =============================================================================================

/// `_jsonable(value)`: Decimals as strings, dates and datetimes as ISO text, containers
/// recursed, everything else as it is.
#[must_use]
pub fn jsonable(v: &Val) -> PyValue {
    match v {
        Val::None => PyValue::None,
        Val::Bool(b) => PyValue::Bool(*b),
        Val::Int(i) => PyValue::Int(*i),
        Val::Float(f) => PyValue::Float(*f),
        Val::Dec(d) => PyValue::Str(dstr(*d)),
        Val::Str(s) => PyValue::Str(s.clone()),
        Val::Date(d) => PyValue::Str(d.format("%Y-%m-%d").to_string()),
        Val::Stamp(t) => PyValue::Str(py_isoformat(t, 'T')),
        Val::Naive(t) => PyValue::Str(naive_isoformat(t, 'T')),
        Val::Py(p) => jsonable_py(p),
        Val::List(items) => PyValue::List(items.iter().map(jsonable).collect()),
        Val::Map(rec) => PyValue::Map(jsonable_rec(rec)),
    }
}

fn jsonable_py(p: &PyValue) -> PyValue {
    use baskfy_providers::records::PyDateTime;
    match p {
        PyValue::Date(d) => PyValue::Str(d.format("%Y-%m-%d").to_string()),
        PyValue::DateTime(PyDateTime::Aware(t)) => PyValue::Str(py_isoformat(t, 'T')),
        PyValue::DateTime(PyDateTime::Naive(t)) => PyValue::Str(naive_isoformat(t, 'T')),
        PyValue::List(items) => PyValue::List(items.iter().map(jsonable_py).collect()),
        PyValue::Map(m) => {
            let mut out = PyDict::new();
            for (k, v) in m.iter() {
                out.insert(k, jsonable_py(v));
            }
            PyValue::Map(out)
        }
        other => other.clone(),
    }
}

/// `_jsonable(dict)`.
#[must_use]
pub fn jsonable_rec(rec: &Rec) -> PyDict {
    let mut out = PyDict::new();
    for (k, v) in &rec.0 {
        out.insert(k.clone(), jsonable(v));
    }
    out
}

/// A Decimal for the page: a float, except at a two-place rounding tie, where the float would
/// round the other way from Python's `'{:.2f}'.format(Decimal)` (half-even on the exact value) —
/// there the already-rounded value is handed over. DECISIONS-RS R8.TV1.
#[must_use]
pub fn template_decimal(d: Decimal) -> f64 {
    let f = dfloat(d);
    let exact = quantize(d, -2);
    let float_text = format!("{f:.2}");
    if float_text == dstr(exact) || d.scale() <= 2 {
        f
    } else {
        dfloat(exact)
    }
}

/// A [`Val`] as a template value (`v` in `twt.html`/`vbt.html`).
#[must_use]
pub fn template_value(v: &Val) -> minijinja::Value {
    use minijinja::Value;
    match v {
        Val::None => Value::from(()),
        Val::Bool(b) => Value::from(*b),
        Val::Int(i) => Value::from(*i),
        Val::Float(f) => Value::from(*f),
        Val::Dec(d) => Value::from(template_decimal(*d)),
        Val::Str(s) => Value::from(s.clone()),
        Val::Date(d) => crate::templates::py_value(&PyValue::Date(*d)),
        Val::Stamp(t) => crate::templates::py_value(&PyValue::DateTime(
            baskfy_providers::records::PyDateTime::Aware(*t),
        )),
        Val::Naive(t) => crate::templates::py_value(&PyValue::DateTime(
            baskfy_providers::records::PyDateTime::Naive(*t),
        )),
        Val::Py(p) => crate::templates::py_value(p),
        Val::List(items) => Value::from(items.iter().map(template_value).collect::<Vec<_>>()),
        Val::Map(rec) => Value::from_pairs(
            rec.0
                .iter()
                .map(|(k, v)| (k.clone(), template_value(v)))
                .collect::<Vec<_>>(),
        ),
    }
}

/// A [`Val`] as a bound SQL parameter, by the Python type the call site passes.
#[must_use]
pub fn param(v: &Val) -> Param {
    match v {
        Val::None | Val::Py(PyValue::None) => Param::Null,
        Val::Bool(b) => Param::Bool(*b),
        Val::Int(i) | Val::Py(PyValue::Int(i)) => Param::Int(*i),
        Val::Float(f) | Val::Py(PyValue::Float(f)) => Param::Float(*f),
        Val::Dec(d) => Param::Decimal(*d),
        Val::Str(s) | Val::Py(PyValue::Str(s)) => Param::Text(s.clone()),
        Val::Date(d) => Param::Date(*d),
        Val::Stamp(t) => Param::Aware(*t),
        Val::Naive(t) => Param::Naive(*t),
        Val::Py(p) => Param::Text(p.py_str()),
        Val::List(_) | Val::Map(_) => Param::Text(v.py_str()),
    }
}

// =============================================================================================
// HTTP: FastAPI's answers
// =============================================================================================

/// An `HTTPException(status, detail, headers=...)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpError {
    /// The status.
    pub status: u16,
    /// `detail`.
    pub detail: String,
    /// Extra headers (`Retry-After`).
    pub headers: Vec<(String, String)>,
}

impl HttpError {
    /// `HTTPException(status, detail)`.
    #[must_use]
    pub fn new(status: u16, detail: impl Into<String>) -> Self {
        Self {
            status,
            detail: detail.into(),
            headers: Vec::new(),
        }
    }

    /// FastAPI's answer: `{"detail": "..."}`.
    #[must_use]
    pub fn response(&self) -> Response<Body> {
        let mut body = PyDict::new();
        body.insert("detail", PyValue::Str(self.detail.clone()));
        let mut r = json_response(self.status, &PyValue::Map(body));
        for (k, v) in &self.headers {
            if let (Ok(name), Ok(value)) = (
                axum::http::HeaderName::from_bytes(k.as_bytes()),
                HeaderValue::from_str(v),
            ) {
                r.headers_mut().insert(name, value);
            }
        }
        r
    }
}

/// `ORJSONResponse(value, status_code=status)`.
#[must_use]
pub fn json_response(status: u16, value: &PyValue) -> Response<Body> {
    match orjson_dumps(value) {
        Ok(text) => {
            let mut r = Response::new(Body::from(text));
            *r.status_mut() = StatusCode::from_u16(status).unwrap_or(StatusCode::OK);
            r.headers_mut().insert(
                axum::http::header::CONTENT_TYPE,
                HeaderValue::from_static("application/json"),
            );
            r
        }
        Err(_) => server_error(),
    }
}

/// `HTMLResponse(text)`.
#[must_use]
pub fn html_response(text: String) -> Response<Body> {
    let mut r = Response::new(Body::from(text));
    r.headers_mut().insert(
        axum::http::header::CONTENT_TYPE,
        HeaderValue::from_static("text/html; charset=utf-8"),
    );
    r
}

/// Starlette's unhandled-exception answer.
#[must_use]
pub fn server_error() -> Response<Body> {
    let mut r = Response::new(Body::from("Internal Server Error"));
    *r.status_mut() = StatusCode::INTERNAL_SERVER_ERROR;
    r.headers_mut().insert(
        axum::http::header::CONTENT_TYPE,
        HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    r
}

/// An urlencoded form body, as Starlette parses it (`+` is a space, `%XX` a byte, blank values
/// kept).
#[must_use]
pub fn parse_form(body: &[u8]) -> Vec<(String, String)> {
    fn decode(s: &[u8]) -> String {
        let mut out = Vec::with_capacity(s.len());
        let mut i = 0;
        while i < s.len() {
            match s[i] {
                b'+' => {
                    out.push(b' ');
                    i += 1;
                }
                b'%' if i + 2 < s.len() => {
                    let hex = std::str::from_utf8(&s[i + 1..i + 3])
                        .ok()
                        .and_then(|h| u8::from_str_radix(h, 16).ok());
                    if let Some(b) = hex {
                        out.push(b);
                        i += 3;
                    } else {
                        out.push(b'%');
                        i += 1;
                    }
                }
                c => {
                    out.push(c);
                    i += 1;
                }
            }
        }
        String::from_utf8_lossy(&out).into_owned()
    }
    body.split(|b| *b == b'&' || *b == b';')
        .filter(|part| !part.is_empty())
        .map(|part| {
            let mut kv = part.splitn(2, |b| *b == b'=');
            let k = kv.next().unwrap_or_default();
            let v = kv.next().unwrap_or_default();
            (decode(k), decode(v))
        })
        .collect()
}

/// A route's `Form(...)` fields, validated as FastAPI does; the 422 when one is missing or
/// unparseable.
#[derive(Debug, Default)]
pub struct FormFields {
    pairs: Vec<(String, String)>,
    errors: Vec<PyValue>,
}

/// pydantic-core's lax `str -> int`: trimmed, an optional sign, ASCII digits with single
/// underscores between them, and an optional `.` followed only by zeros.
#[must_use]
pub fn lax_int(raw: &str) -> Option<i64> {
    let t = raw.trim();
    let (neg, body) = match t.as_bytes().first() {
        Some(b'-') => (true, &t[1..]),
        Some(b'+') => (false, &t[1..]),
        _ => (false, t),
    };
    let int_part = match body.split_once('.') {
        Some((i, f)) => {
            if f.is_empty() || !f.bytes().all(|b| b == b'0') {
                return None;
            }
            i
        }
        None => body,
    };
    if int_part.is_empty()
        || int_part.starts_with('_')
        || int_part.ends_with('_')
        || int_part.contains("__")
        || !int_part.bytes().all(|b| b.is_ascii_digit() || b == b'_')
    {
        return None;
    }
    let digits: String = int_part.chars().filter(char::is_ascii_digit).collect();
    let n: i64 = digits.parse().ok()?;
    Some(if neg { -n } else { n })
}

impl FormFields {
    /// Parse a request body.
    #[must_use]
    pub fn parse(body: &[u8]) -> Self {
        Self {
            pairs: parse_form(body),
            errors: Vec::new(),
        }
    }

    fn raw(&self, name: &str) -> Option<String> {
        self.pairs
            .iter()
            .rev()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.clone())
            .filter(|v| !v.is_empty())
    }

    fn error(&mut self, kind: &str, name: &str, msg: &str, input: PyValue) {
        let mut e = PyDict::new();
        e.insert("type", PyValue::Str(kind.to_owned()));
        e.insert(
            "loc",
            PyValue::List(vec![
                PyValue::Str("body".to_owned()),
                PyValue::Str(name.to_owned()),
            ]),
        );
        e.insert("msg", PyValue::Str(msg.to_owned()));
        e.insert("input", input);
        self.errors.push(PyValue::Map(e));
    }

    /// `name: str = Form(...)`.
    pub fn str(&mut self, name: &str) -> String {
        if let Some(v) = self.raw(name) {
            v
        } else {
            self.error("missing", name, "Field required", PyValue::None);
            String::new()
        }
    }

    /// `name: int = Form(...)`.
    pub fn int(&mut self, name: &str) -> i64 {
        match self.raw(name) {
            None => {
                self.error("missing", name, "Field required", PyValue::None);
                0
            }
            Some(v) => lax_int(&v).unwrap_or_else(|| {
                self.error(
                    "int_parsing",
                    name,
                    "Input should be a valid integer, unable to parse string as an integer",
                    PyValue::Str(v.clone()),
                );
                0
            }),
        }
    }

    /// FastAPI's 422, if any field failed.
    #[must_use]
    pub fn finish(self) -> Option<Response<Body>> {
        if self.errors.is_empty() {
            return None;
        }
        let mut body = PyDict::new();
        body.insert("detail", PyValue::List(self.errors));
        Some(json_response(422, &PyValue::Map(body)))
    }
}

/// A path parameter `name: int` (`/twt/scan/{run_id}`), or FastAPI's 422.
///
/// # Errors
/// The 422 response.
pub fn path_int(name: &str, raw: &str) -> Result<i64, Box<Response<Body>>> {
    lax_int(raw).ok_or_else(|| {
        let mut e = PyDict::new();
        e.insert("type", PyValue::Str("int_parsing".to_owned()));
        e.insert(
            "loc",
            PyValue::List(vec![
                PyValue::Str("path".to_owned()),
                PyValue::Str(name.to_owned()),
            ]),
        );
        e.insert(
            "msg",
            PyValue::Str(
                "Input should be a valid integer, unable to parse string as an integer".to_owned(),
            ),
        );
        e.insert("input", PyValue::Str(raw.to_owned()));
        let mut body = PyDict::new();
        body.insert("detail", PyValue::List(vec![PyValue::Map(e)]));
        Box::new(json_response(422, &PyValue::Map(body)))
    })
}

// =============================================================================================
// `app.reconcile`: protection first, entries second (LV2)
// =============================================================================================

/// `reconcile.protection_unresolved(store, naked=...)`: the naked positions and the open issues.
#[must_use]
pub fn protection_unresolved(naked: &[Rec], issues: &[Rec]) -> Vec<String> {
    let mut reasons: Vec<String> = naked
        .iter()
        .map(|row| {
            format!(
                "{}: position {} holds {} with no resting stop",
                row.s("symbol"),
                row.s("id"),
                row.s("quantity_open")
            )
        })
        .collect();
    for issue in issues {
        reasons.push(format!(
            "{}: {} \u{2014} {}",
            issue.s("symbol"),
            issue.s("kind"),
            issue.s("detail")
        ));
    }
    reasons
}

/// `reconcile.refusal(reasons)`.
#[must_use]
pub fn refusal(reasons: &[String]) -> String {
    format!("PROTECTION_UNRESOLVED: {}", reasons.join("; "))
}

// =============================================================================================
// `app.exit_rules`
// =============================================================================================

/// `_pct(value)`: `f"{Decimal(str(value)).normalize():f}"` — `20.0` reads `20`, `12.5` stays.
#[must_use]
pub fn pct_text(text: &str) -> String {
    py_decimal(text).map_or_else(|| text.to_owned(), |d| d.normalize().to_string())
}

fn qulla_rule(prefix: &str, s: &baskfy_core::swing::stops::StopConfig) -> String {
    format!(
        "{prefix}; {}/{} sold into strength between bar {} and bar {} after entry if green, then \
         the stop to breakeven (or after {}R); the rest trails the 10-day MA (ADR at or above \
         {}%) or the 20-day MA and is sold at the next open on a close below it; the GTT is the \
         hard stop (Qullamaggie's rule)",
        s.partial_numerator,
        s.partial_denominator,
        s.partial_earliest_bar,
        s.partial_latest_bar,
        pct_text(&baskfy_core::py::float_repr(s.breakeven_after_r)),
        pct_text(&baskfy_core::py::float_repr(s.fast_trail_min_adr_pct)),
    )
}

/// `EXIT_RULES["twt"]`.
#[must_use]
pub fn twt_exit_rule() -> String {
    let e = baskfy_core::twt::config::TwtConfig::default().exits;
    if e.qulla_exits {
        return qulla_rule(
            &format!(
                "{}% initial stop under the fill (GTT)",
                pct_text(&e.stop_pct.to_string())
            ),
            &e.qulla,
        );
    }
    format!(
        "{}% initial stop under the fill; a {}% high-water trail ratchets the GTT each evening; \
         no target, no time stop \u{2014} the GTT is the only exit",
        pct_text(&e.stop_pct.to_string()),
        pct_text(&e.trail_pct.to_string())
    )
}

/// `EXIT_RULES["vbt"]`.
#[must_use]
pub fn vbt_exit_rule() -> String {
    let e = baskfy_core::vbt::config::VbtConfig::default().exits;
    let stop = pct_text(&baskfy_core::py::float_repr(e.stop_pct));
    if e.qulla_exits {
        return qulla_rule(
            &format!("{stop}% initial stop under the signal close (GTT)"),
            &e.qulla,
        );
    }
    format!(
        "{stop}% initial stop under the signal close (GTT); the working exit is a close below \
         the 21-day EMA, sold at the next open; no target"
    )
}

// =============================================================================================
// `last_price`: a read, not an order
// =============================================================================================

/// `twt_desk.last_price(symbol)` / `vbt_desk.last_price(symbol)`: `kite().ltp([symbol])`, or
/// `None` without a session (or for a zero price). Logged, never raised.
///
/// `app.main.kite()` is the desk's **wrapper**, so the read goes through its `LTP_CACHE_SECONDS`
/// cache, its quote-family limiter and `refresh_token_if_changed` — the exec_core lane's
/// [`crate::lanes::exec_core::kite_client::DeskKite::ltp`]. Only a process without the desk's
/// gateway (a test state) falls back to the bare SDK read.
pub async fn last_price(state: &AppState, symbol: &str) -> Option<Decimal> {
    let desk = state
        .gateway
        .as_any()
        .and_then(|a| a.downcast_ref::<crate::lanes::exec_core::desk::DeskGateway>());
    let price = if let Some(gw) = desk {
        let prices = gw.kite().ltp(&[symbol.to_owned()], "NSE").await.ok()?;
        match prices.get(symbol)? {
            PyValue::Float(f) => *f,
            #[allow(clippy::cast_precision_loss)]
            PyValue::Int(i) => *i as f64,
            _ => return None,
        }
    } else {
        let key = format!("NSE:{symbol}");
        let answer = state.kite.ltp(std::slice::from_ref(&key)).await.ok()?;
        let row = answer.get(&key).ok()?;
        match row.get("last_price").ok()? {
            PyValue::Float(f) => *f,
            #[allow(clippy::cast_precision_loss)]
            PyValue::Int(i) => *i as f64,
            PyValue::Str(s) => s.trim().parse().ok()?,
            _ => return None,
        }
    };
    if price == 0.0 {
        return None;
    }
    baskfy_core::py::decimal_from_float_str(price).ok()
}

/// The sole tenant (`TenantIds(user_id=int(C.SOLE_USER_ID), ...)`), or the `ValueError`.
///
/// # Errors
/// A non-positive id.
pub fn tenant(
    settings: &crate::settings::Settings,
) -> Result<crate::gateway::TenantIds, crate::gateway::ClientIdError> {
    crate::gateway::TenantIds::new(settings.sole_user_id, settings.sole_broker_account_id)
}

/// The gateway's result as an outcome field.
#[must_use]
pub fn py_map(d: &PyDict) -> Val {
    Val::Py(PyValue::Map(d.clone()))
}

/// `result.get(key)` on a gateway result.
#[must_use]
pub fn res_get(d: &PyDict, key: &str) -> Val {
    d.get(key).cloned().map_or(Val::None, Val::Py)
}

/// `str(result.get(key) or "")` on a gateway result.
#[must_use]
pub fn res_str(d: &PyDict, key: &str) -> String {
    match d.get(key) {
        Some(v) if v.truthy() => v.py_str(),
        _ => String::new(),
    }
}

/// `str(result.get(key))` (Python prints `None` for a missing key).
#[must_use]
pub fn res_str_none(d: &PyDict, key: &str) -> String {
    d.get(key)
        .map_or_else(|| "None".to_owned(), PyValue::py_str)
}
