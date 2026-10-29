//! The request models of the accounts lane's **write** routes, validated as pydantic 2 validates
//! them in Python mode (FastAPI's `ModelField.validate`): `ApiKeyCreate`, `ApiKeyRevokeIn`,
//! `ScreenAlertCreate`/`Update`, `UnsubscribeIn`, `WebhookEndpointCreate`/`Update`,
//! `CheckoutSessionIn`, `EntitlementOverrideIn` and `SupportMessageIn` (`baskfy_api.schemas`).
//!
//! [`crate::body`] carries the framework half (the body read, the content-type rule, CPython's
//! JSON scanner) and the two string kinds the session routes needed. These models also declare
//! `int` (with `ge`/`le`), `bool`, `datetime`, `Literal[...]` and lists, so the lane adds the
//! remaining pydantic-core lax rules here, privately (DECISIONS-RS R6.33):
//!
//! * `int`: a JSON int of any size; `true`/`false` as 1/0; a float only when integral
//!   (`int_from_float`), finite (`finite_number`) and within `i64` (`int_parsing_size`); a string
//!   by pydantic's lax rule ([`crate::routes::common::lax_int`]); then `ge`/`le`;
//! * `bool`: `true`/`false`, the ints 0/1, the floats 0.0/1.0, and pydantic's string spellings;
//! * `datetime`: speedate's RFC 3339 grammar, unix timestamps (seconds, or milliseconds beyond
//!   2e10) from ints, floats and numeric strings, and pydantic's fallback to a bare date — every
//!   failure on a string then reports the *date* parser's error (`datetime_from_date_parsing`),
//!   as pydantic-core does. The offset (or its absence) is kept, because Python echoes the parsed
//!   object, not the stored one ([`PyDateTime`]);
//! * `Literal[...]`: `literal_error` with pydantic's `'a', 'b' or 'c'` wording;
//! * lists: `list_type`, then each item's error with its index in the path (`scopes.0`).
//!
//! A JSON `null` is accepted only by an `X | None` field; a field with a non-`None` default
//! refuses it with its type's error, as pydantic does.

use axum::extract::Request;
use axum::http::header;
use baskfy_db::OrderedJson;
use chrono::{DateTime, Datelike, NaiveDate, NaiveDateTime, NaiveTime, Timelike, Utc};

use crate::body::Received;
use crate::email_address::validate_email_str;
use crate::problems::{ErrorEntry, Problem, framework, invalid_screen_definition};
use crate::routes::common::{PyInt, lax_bool, lax_int};

/// The largest body read (Starlette reads it whole; this only bounds memory).
const MAX_BODY_BYTES: usize = 1 << 20;

/// `await request.body()` and its `Content-Type`, then FastAPI's read of it as JSON.
///
/// # Errors
/// The `json_invalid` 400 and Starlette's parse 400 ([`crate::body::receive`]).
pub(crate) async fn read_body(request: Request) -> Result<Received, Problem> {
    let (content_type, bytes) = raw_body(request).await?;
    crate::body::receive(content_type.as_deref(), &bytes)
}

/// The raw bytes and `Content-Type` of a request (the webhook signs the exact bytes).
///
/// # Errors
/// Starlette's 400 when the body cannot be read.
pub(crate) async fn raw_body(request: Request) -> Result<(Option<String>, Vec<u8>), Problem> {
    let content_type = request
        .headers()
        .get(header::CONTENT_TYPE)
        .map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned());
    let bytes = axum::body::to_bytes(request.into_body(), MAX_BODY_BYTES)
        .await
        .map_err(|_| framework(400, "There was an error parsing the body"))?;
    Ok((content_type, bytes.to_vec()))
}

/// `datetime.now(tz=UTC)`: microsecond precision, as Python's clock and the column hold it.
pub(crate) fn now_micro() -> DateTime<Utc> {
    let t = crate::entitlements::utc_now();
    t.with_nanosecond(t.nanosecond() / 1000 * 1000).unwrap_or(t)
}

/// The write pool a POST/PUT/PATCH/DELETE handler is handed (`session.write_access`).
///
/// # Errors
/// The 500 catch-all when the deployment has none (never on a configured process).
pub(crate) fn write_pool(
    pool: Option<axum::extract::Extension<crate::session::WritePool>>,
) -> Result<crate::session::WritePool, Problem> {
    pool.map(|axum::extract::Extension(p)| p)
        .ok_or_else(crate::problems::internal_error)
}

/// A `sqlx` failure inside a write as the 500 catch-all.
pub(crate) fn dbw(e: sqlx::Error) -> Problem {
    crate::db_failure(&e.into())
}

/// One FastAPI validation error entry (`app._handle_validation`).
pub(crate) fn entry(field: &str, message: &str, kind: &str) -> ErrorEntry {
    vec![
        ("field".to_owned(), OrderedJson::Str(field.to_owned())),
        ("message".to_owned(), OrderedJson::Str(message.to_owned())),
        ("type".to_owned(), OrderedJson::Str(kind.to_owned())),
    ]
}

/// What a declared field accepts.
#[derive(Debug, Clone, Copy)]
pub(crate) enum Kind {
    /// `str`, with `min_length`/`max_length`.
    Str {
        /// `min_length`.
        min: Option<usize>,
        /// `max_length`.
        max: Option<usize>,
    },
    /// `int`, with `ge`/`le`.
    Int {
        /// `ge`.
        ge: Option<i64>,
        /// `le`.
        le: Option<i64>,
    },
    /// `bool`.
    Bool,
    /// `datetime`.
    DateTime,
    /// `EmailStr` with `max_length=254` (`schemas.Email`).
    Email,
    /// `Literal[...]` of strings.
    Literal(&'static [&'static str]),
    /// `list[str]`.
    StrList,
    /// `list[Literal[...]]`.
    LiteralList(&'static [&'static str]),
}

/// Whether a field must be sent, may be `null`, or has a non-`None` default.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Presence {
    /// No default.
    Required,
    /// `X | None = None`: absent or `null` is [`Val::Absent`].
    Optional,
    /// A non-`None` default: absent is [`Val::Absent`] (the caller applies the default); `null` is
    /// refused.
    Defaulted,
}

/// One declared field, in declaration order.
#[derive(Debug, Clone, Copy)]
pub(crate) struct Field {
    /// The name.
    pub(crate) name: &'static str,
    /// What it accepts.
    pub(crate) kind: Kind,
    /// Whether it may be left out.
    pub(crate) presence: Presence,
}

/// Shorthand for a [`Field`].
pub(crate) const fn field(name: &'static str, kind: Kind, presence: Presence) -> Field {
    Field {
        name,
        kind,
        presence,
    }
}

/// A validated value.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Val {
    /// Left out (or `null` for an optional field).
    Absent,
    /// A string (an email in pydantic's normalised form).
    Str(String),
    /// An integer (Python's are unbounded).
    Int(PyInt),
    /// A boolean.
    Bool(bool),
    /// A datetime, as pydantic built it.
    DateTime(PyDateTime),
    /// A list of strings.
    List(Vec<String>),
}

impl Val {
    /// The string, if any.
    pub(crate) fn str(&self) -> Option<&str> {
        match self {
            Self::Str(s) => Some(s),
            _ => None,
        }
    }

    /// The integer, if any.
    pub(crate) fn int(&self) -> Option<&PyInt> {
        match self {
            Self::Int(i) => Some(i),
            _ => None,
        }
    }

    /// The boolean, if any.
    pub(crate) fn bool(&self) -> Option<bool> {
        match self {
            Self::Bool(b) => Some(*b),
            _ => None,
        }
    }

    /// The datetime, if any.
    pub(crate) fn datetime(&self) -> Option<&PyDateTime> {
        match self {
            Self::DateTime(d) => Some(d),
            _ => None,
        }
    }

    /// The list, if any.
    pub(crate) fn list(&self) -> Option<&[String]> {
        match self {
            Self::List(l) => Some(l),
            _ => None,
        }
    }
}

fn plural(n: usize) -> &'static str {
    if n == 1 { "" } else { "s" }
}

fn check_len(field: &str, s: &str, min: Option<usize>, max: Option<usize>) -> Option<ErrorEntry> {
    let n = s.chars().count();
    if let Some(min) = min
        && n < min
    {
        return Some(entry(
            field,
            &format!("String should have at least {min} character{}", plural(min)),
            "string_too_short",
        ));
    }
    if let Some(max) = max
        && n > max
    {
        return Some(entry(
            field,
            &format!("String should have at most {max} character{}", plural(max)),
            "string_too_long",
        ));
    }
    None
}

/// pydantic's `Literal` expectation: `'a'`, `'a' or 'b'`, `'a', 'b' or 'c'`.
pub(crate) fn literal_expected(options: &[&str]) -> String {
    let quoted: Vec<String> = options
        .iter()
        .map(|o| baskfy_providers::py::repr_str(o))
        .collect();
    match quoted.split_last() {
        None => String::new(),
        Some((last, [])) => last.clone(),
        Some((last, rest)) => format!("{} or {last}", rest.join(", ")),
    }
}

fn literal(path: &str, value: &OrderedJson, options: &[&str]) -> Result<String, ErrorEntry> {
    if let OrderedJson::Str(s) = value
        && options.contains(&s.as_str())
    {
        return Ok(s.clone());
    }
    Err(entry(
        path,
        &format!("Input should be {}", literal_expected(options)),
        "literal_error",
    ))
}

fn string(path: &str, value: &OrderedJson) -> Result<String, ErrorEntry> {
    match value {
        OrderedJson::Str(s) => Ok(s.clone()),
        _ => Err(entry(path, "Input should be a valid string", "string_type")),
    }
}

fn int_from_json(path: &str, value: &OrderedJson) -> Result<PyInt, ErrorEntry> {
    let parsing = || {
        entry(
            path,
            "Input should be a valid integer, unable to parse string as an integer",
            "int_parsing",
        )
    };
    match value {
        OrderedJson::Bool(b) => lax_int(if *b { "1" } else { "0" }).ok_or_else(parsing),
        OrderedJson::Int(i) => lax_int(&i.to_string()).ok_or_else(parsing),
        OrderedJson::UInt(u) => lax_int(&u.to_string()).ok_or_else(parsing),
        OrderedJson::BigInt(s) => lax_int(s).ok_or_else(parsing),
        OrderedJson::Float(f) => {
            if !f.is_finite() {
                return Err(entry(
                    path,
                    "Input should be a finite number",
                    "finite_number",
                ));
            }
            if f.fract() != 0.0 {
                return Err(entry(
                    path,
                    "Input should be a valid integer, got a number with a fractional part",
                    "int_from_float",
                ));
            }
            // pydantic-core casts through i64: beyond it is `int_parsing_size`.
            if *f >= 9_223_372_036_854_775_808.0 || *f < -9_223_372_036_854_775_808.0 {
                return Err(entry(
                    path,
                    "Unable to parse input string as an integer, exceeded maximum size",
                    "int_parsing_size",
                ));
            }
            #[allow(clippy::cast_possible_truncation)]
            let whole = *f as i64;
            lax_int(&whole.to_string()).ok_or_else(parsing)
        }
        OrderedJson::Str(s) => lax_int(s).ok_or_else(parsing),
        _ => Err(entry(path, "Input should be a valid integer", "int_type")),
    }
}

fn int(
    path: &str,
    value: &OrderedJson,
    ge: Option<i64>,
    le: Option<i64>,
) -> Result<PyInt, ErrorEntry> {
    let n = int_from_json(path, value)?;
    let v = n.saturated();
    if let Some(ge) = ge
        && v < i128::from(ge)
    {
        return Err(entry(
            path,
            &format!("Input should be greater than or equal to {ge}"),
            "greater_than_equal",
        ));
    }
    if let Some(le) = le
        && v > i128::from(le)
    {
        return Err(entry(
            path,
            &format!("Input should be less than or equal to {le}"),
            "less_than_equal",
        ));
    }
    Ok(n)
}

fn boolean(path: &str, value: &OrderedJson) -> Result<bool, ErrorEntry> {
    let parsing = || {
        entry(
            path,
            "Input should be a valid boolean, unable to interpret input",
            "bool_parsing",
        )
    };
    match value {
        OrderedJson::Bool(b) => Ok(*b),
        OrderedJson::Int(0) => Ok(false),
        OrderedJson::Int(1) => Ok(true),
        OrderedJson::Int(_) | OrderedJson::UInt(_) | OrderedJson::BigInt(_) => Err(parsing()),
        OrderedJson::Float(f) if *f == 0.0 => Ok(false),
        OrderedJson::Float(f) if f.to_bits() == 1.0_f64.to_bits() => Ok(true),
        OrderedJson::Float(_) => Err(parsing()),
        OrderedJson::Str(s) => lax_bool(s).ok_or_else(parsing),
        _ => Err(entry(path, "Input should be a valid boolean", "bool_type")),
    }
}

fn email(path: &str, value: &OrderedJson) -> Result<String, ErrorEntry> {
    let s = string(path, value)?;
    let normalised = validate_email_str(&s).map_err(|m| entry(path, &m, "value_error"))?;
    match check_len(path, &normalised, None, Some(254)) {
        Some(e) => Err(e),
        None => Ok(normalised),
    }
}

fn list<T>(
    path: &str,
    value: &OrderedJson,
    item: impl Fn(&str, &OrderedJson) -> Result<T, ErrorEntry>,
) -> Result<Vec<T>, Vec<ErrorEntry>> {
    let OrderedJson::Array(items) = value else {
        return Err(vec![entry(
            path,
            "Input should be a valid list",
            "list_type",
        )]);
    };
    let mut out = Vec::with_capacity(items.len());
    let mut errors = Vec::new();
    for (i, v) in items.iter().enumerate() {
        match item(&format!("{path}.{i}"), v) {
            Ok(x) => out.push(x),
            Err(e) => errors.push(e),
        }
    }
    if errors.is_empty() {
        Ok(out)
    } else {
        Err(errors)
    }
}

fn one(kind: Kind, path: &str, value: &OrderedJson) -> Result<Val, Vec<ErrorEntry>> {
    let single = |r: Result<Val, ErrorEntry>| r.map_err(|e| vec![e]);
    match kind {
        Kind::Str { min, max } => {
            single(
                string(path, value).and_then(|s| match check_len(path, &s, min, max) {
                    Some(e) => Err(e),
                    None => Ok(Val::Str(s)),
                }),
            )
        }
        Kind::Int { ge, le } => single(int(path, value, ge, le).map(Val::Int)),
        Kind::Bool => single(boolean(path, value).map(Val::Bool)),
        Kind::DateTime => single(datetime(path, value).map(Val::DateTime)),
        Kind::Email => single(email(path, value).map(Val::Str)),
        Kind::Literal(options) => single(literal(path, value, options).map(Val::Str)),
        Kind::StrList => list(path, value, string).map(Val::List),
        Kind::LiteralList(options) => {
            list(path, value, |p, v| literal(p, v, options)).map(Val::List)
        }
    }
}

/// Validates `received` against a closed (`extra="forbid"`) model with these fields: the values
/// in declaration order, or FastAPI's 400 with every error (fields in order, then each extra key).
///
/// # Errors
/// `invalid_screen_definition(errors)`, as `app._handle_validation` renders it.
pub(crate) fn validate(received: &Received, fields: &[Field]) -> Result<Vec<Val>, Problem> {
    let entries = match received {
        Received::Missing | Received::Json(OrderedJson::Null) => {
            return Err(invalid_screen_definition(vec![entry(
                "<root>",
                "Field required",
                "missing",
            )]));
        }
        Received::Json(OrderedJson::Object(entries)) => entries,
        Received::Raw | Received::Json(_) => {
            return Err(invalid_screen_definition(vec![entry(
                "<root>",
                "Input should be a valid dictionary or object to extract fields from",
                "model_attributes_type",
            )]));
        }
    };
    let mut errors: Vec<ErrorEntry> = Vec::new();
    let mut values = Vec::with_capacity(fields.len());
    for f in fields {
        let given = entries.iter().find(|(k, _)| k == f.name).map(|(_, v)| v);
        let outcome = match (f.presence, given) {
            (Presence::Required, None) => Err(vec![entry(f.name, "Field required", "missing")]),
            (Presence::Optional | Presence::Defaulted, None)
            | (Presence::Optional, Some(OrderedJson::Null)) => Ok(Val::Absent),
            (_, Some(v)) => one(f.kind, f.name, v),
        };
        match outcome {
            Ok(v) => values.push(v),
            Err(mut e) => {
                errors.append(&mut e);
                values.push(Val::Absent);
            }
        }
    }
    for (key, _) in entries {
        if !fields.iter().any(|f| f.name == key) {
            errors.push(entry(
                key,
                "Extra inputs are not permitted",
                "extra_forbidden",
            ));
        }
    }
    if errors.is_empty() {
        Ok(values)
    } else {
        Err(invalid_screen_definition(errors))
    }
}

// ---------------------------------------------------------------------------------------------
// datetime
// ---------------------------------------------------------------------------------------------

/// A `datetime` as pydantic builds it from input: the wall-clock fields and the offset in
/// seconds, or `None` for a naive value. Python keeps this object on the ORM row and echoes it in
/// the response unchanged, so the offset matters even after the value is stored.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct PyDateTime {
    /// The wall clock.
    pub(crate) local: NaiveDateTime,
    /// `utcoffset()` in seconds, `None` when naive.
    pub(crate) offset: Option<i32>,
}

impl PyDateTime {
    /// A UTC instant as asyncpg returns it (offset zero).
    pub(crate) fn from_utc(t: DateTime<Utc>) -> Self {
        Self {
            local: t.naive_utc(),
            offset: Some(0),
        }
    }

    /// The instant, as asyncpg stores it: `astimezone(utc)`. A **naive** value is read as the
    /// process's local time by Python; this port reads it as UTC, which is what Python does in a
    /// process running with `TZ=UTC` (every container; the harness sets it — R6.34).
    pub(crate) fn instant(&self) -> DateTime<Utc> {
        let shift = chrono::Duration::seconds(i64::from(self.offset.unwrap_or(0)));
        DateTime::from_naive_utc_and_offset(self.local - shift, Utc)
    }

    /// The instant `is_active` compares: a naive value is read as UTC (`replace(tzinfo=UTC)`).
    pub(crate) fn aware(&self) -> DateTime<Utc> {
        self.instant()
    }

    /// pydantic's JSON: `Z` for a zero offset, `±HH:MM` otherwise, nothing when naive.
    pub(crate) fn json(&self) -> String {
        let clock = clock(self.local.time());
        let date = date_str(self.local.date());
        match self.offset {
            None => format!("{date}T{clock}"),
            Some(0) => format!("{date}T{clock}Z"),
            Some(o) => format!("{date}T{clock}{}", offset_str(o)),
        }
    }

    /// `datetime.isoformat()`: `+00:00` for a zero offset.
    pub(crate) fn isoformat(&self) -> String {
        let clock = clock(self.local.time());
        let date = date_str(self.local.date());
        match self.offset {
            None => format!("{date}T{clock}"),
            Some(o) => format!("{date}T{clock}{}", offset_str(o)),
        }
    }
}

/// Serialises an optional [`PyDateTime`] as pydantic does (`null` when absent).
pub(crate) fn ser_opt_datetime<S: serde::Serializer>(
    t: &Option<PyDateTime>,
    s: S,
) -> Result<S::Ok, S::Error> {
    match t {
        Some(t) => s.serialize_str(&t.json()),
        None => s.serialize_none(),
    }
}

fn date_str(d: NaiveDate) -> String {
    format!("{:04}-{:02}-{:02}", d.year(), d.month(), d.day())
}

fn clock(t: NaiveTime) -> String {
    let micros = t.nanosecond() / 1000;
    let base = format!("{:02}:{:02}:{:02}", t.hour(), t.minute(), t.second());
    if micros == 0 {
        base
    } else {
        format!("{base}.{micros:06}")
    }
}

fn offset_str(o: i32) -> String {
    let sign = if o < 0 { '-' } else { '+' };
    let abs = o.unsigned_abs();
    format!("{sign}{:02}:{:02}", abs / 3600, abs % 3600 / 60)
}

/// speedate's `ParseError`, as `get_documentation()` words each one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DateError {
    TooShort,
    ExtraCharacters,
    InvalidCharYear,
    InvalidCharDateSep,
    InvalidCharMonth,
    InvalidCharDay,
    OutOfRangeMonth,
    OutOfRangeDay,
    InvalidCharDateTimeSep,
    InvalidCharHour,
    InvalidCharMinute,
    InvalidCharSecond,
    InvalidCharTimeSep,
    InvalidCharTzHour,
    InvalidCharTzMinute,
    OutOfRangeHour,
    OutOfRangeMinute,
    OutOfRangeSecond,
    OutOfRangeTz,
    OutOfRangeTzMinute,
    InvalidCharTzSign,
    SecondFractionMissing,
    DateTooLarge,
    DateTooSmall,
    /// Not speedate's: the date parsed, but Python's `datetime` refuses year 0.
    YearZero,
}

impl DateError {
    fn message(self) -> &'static str {
        match self {
            Self::TooShort => "input is too short",
            Self::ExtraCharacters => "unexpected extra characters at the end of the input",
            Self::InvalidCharYear => "invalid character in year",
            Self::InvalidCharDateSep => "invalid date separator, expected `-`",
            Self::InvalidCharMonth => "invalid character in month",
            Self::InvalidCharDay => "invalid character in day",
            Self::OutOfRangeMonth => "month value is outside expected range of 1-12",
            Self::OutOfRangeDay => "day value is outside expected range",
            Self::InvalidCharDateTimeSep => {
                "invalid datetime separator, expected `T`, `t`, `_` or space"
            }
            Self::InvalidCharHour => "invalid character in hour",
            Self::InvalidCharMinute => "invalid character in minute",
            Self::InvalidCharSecond => "invalid character in second",
            Self::InvalidCharTimeSep => "invalid time separator, expected `:`",
            Self::InvalidCharTzHour => "invalid timezone hour",
            Self::InvalidCharTzMinute => "invalid timezone minute",
            Self::OutOfRangeHour => "hour value is outside expected range of 0-23",
            Self::OutOfRangeMinute => "minute value is outside expected range of 0-59",
            Self::OutOfRangeSecond => "second value is outside expected range of 0-59",
            Self::OutOfRangeTz => "timezone offset must be less than 24 hours",
            Self::OutOfRangeTzMinute => "timezone minute value is outside expected range of 0-59",
            Self::InvalidCharTzSign => "invalid timezone sign",
            Self::SecondFractionMissing => "second fraction digits missing after `.`",
            Self::DateTooLarge => "dates after 9999 are not supported as unix timestamps",
            Self::DateTooSmall => "dates before 0000 are not supported as unix timestamps",
            Self::YearZero => "year 0 is out of range",
        }
    }
}

/// `MS_WATERSHED`: beyond ±2e10 an integer timestamp is milliseconds.
const MS_WATERSHED: i64 = 20_000_000_000;
/// The first second of year 10000.
const UNIX_10000: i64 = 253_402_300_800;
/// The first second of year 0.
const UNIX_0000: i64 = -62_167_219_200;

fn digits2(b: &[u8], at: usize) -> Option<u32> {
    match (b.get(at), b.get(at + 1)) {
        (Some(x @ b'0'..=b'9'), Some(y @ b'0'..=b'9')) => {
            Some(u32::from(x - b'0') * 10 + u32::from(y - b'0'))
        }
        _ => None,
    }
}

/// `Date::parse_bytes_partial`: the first ten bytes as `YYYY-MM-DD`.
fn date_partial(b: &[u8]) -> Result<(i32, u32, u32), DateError> {
    if b.len() < 10 {
        return Err(DateError::TooShort);
    }
    let mut year: i32 = 0;
    for &c in &b[..4] {
        if !c.is_ascii_digit() {
            return Err(DateError::InvalidCharYear);
        }
        year = year * 10 + i32::from(c - b'0');
    }
    if b[4] != b'-' {
        return Err(DateError::InvalidCharDateSep);
    }
    let month = digits2(b, 5).ok_or(DateError::InvalidCharMonth)?;
    if b[7] != b'-' {
        return Err(DateError::InvalidCharDateSep);
    }
    let day = digits2(b, 8).ok_or(DateError::InvalidCharDay)?;
    let leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
    let max = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if leap {
                29
            } else {
                28
            }
        }
        _ => return Err(DateError::OutOfRangeMonth),
    };
    if day < 1 || day > max {
        return Err(DateError::OutOfRangeDay);
    }
    Ok((year, month, day))
}

/// `Time::parse_bytes_partial` plus the offset: `(time, offset, bytes consumed)`.
fn time_partial(b: &[u8]) -> Result<(NaiveTime, Option<i32>, usize), DateError> {
    if b.len() < 5 {
        return Err(DateError::TooShort);
    }
    let hour = digits2(b, 0).ok_or(DateError::InvalidCharHour)?;
    if hour > 23 {
        return Err(DateError::OutOfRangeHour);
    }
    if b.get(2) != Some(&b':') {
        return Err(DateError::InvalidCharTimeSep);
    }
    let minute = digits2(b, 3).ok_or(DateError::InvalidCharMinute)?;
    if minute > 59 {
        return Err(DateError::OutOfRangeMinute);
    }
    let mut len = 5;
    let mut second = 0;
    let mut micro = 0u32;
    if b.get(5) == Some(&b':') {
        second = digits2(b, 6).ok_or(DateError::InvalidCharSecond)?;
        if second > 59 {
            return Err(DateError::OutOfRangeSecond);
        }
        len = 8;
        if matches!(b.get(8), Some(b'.' | b',')) {
            len = 9;
            let mut i = 0;
            while let Some(c @ b'0'..=b'9') = b.get(len + i) {
                if i < 6 {
                    micro = micro * 10 + u32::from(c - b'0');
                }
                i += 1;
            }
            if i == 0 {
                return Err(DateError::SecondFractionMissing);
            }
            if i < 6 {
                micro *= 10u32.pow(u32::try_from(6 - i).unwrap_or(0));
            }
            len += i;
        }
    }
    let offset = match b.get(len) {
        Some(b'Z' | b'z') => {
            len += 1;
            Some(0)
        }
        Some(sign @ (b'+' | b'-')) => {
            let h = digits2(b, len + 1).ok_or(DateError::InvalidCharTzHour)?;
            let mut at = len + 3;
            if b.get(at) == Some(&b':') {
                at += 1;
            }
            let m = digits2(b, at).ok_or(DateError::InvalidCharTzMinute)?;
            if m > 59 {
                return Err(DateError::OutOfRangeTzMinute);
            }
            len = at + 2;
            let total = i32::try_from(h * 3600 + m * 60).unwrap_or(i32::MAX);
            if total >= 24 * 3600 {
                return Err(DateError::OutOfRangeTz);
            }
            Some(if *sign == b'-' { -total } else { total })
        }
        Some(_) => return Err(DateError::InvalidCharTzSign),
        None => None,
    };
    let time = NaiveTime::from_hms_micro_opt(hour, minute, second, micro)
        .ok_or(DateError::OutOfRangeSecond)?;
    Ok((time, offset, len))
}

/// `DateTime::parse_bytes_rfc3339`, with speedate's error.
fn rfc3339(b: &[u8]) -> Result<PyDateTime, DateError> {
    let (y, m, d) = date_partial(b)?;
    if !matches!(b.get(10), Some(b'T' | b't' | b' ' | b'_')) {
        return Err(DateError::InvalidCharDateTimeSep);
    }
    let (time, offset, used) = time_partial(&b[11..])?;
    if 11 + used != b.len() {
        return Err(DateError::ExtraCharacters);
    }
    let date = NaiveDate::from_ymd_opt(y, m, d).ok_or(DateError::OutOfRangeDay)?;
    Ok(PyDateTime {
        local: date.and_time(time),
        offset,
    })
}

/// `DateTime::parse_bytes`: RFC 3339, else a numeric timestamp; the error is the timestamp's when
/// the input is a number, else the grammar's.
fn datetime_bytes(b: &[u8]) -> Result<PyDateTime, DateError> {
    let grammar = match rfc3339(b) {
        Ok(d) => return Ok(d),
        Err(e) => e,
    };
    match number_parse(b) {
        Some(Number::Int(i)) => from_int(i),
        Some(Number::Float(f)) => from_float(f),
        None => Err(grammar),
    }
}

/// pydantic's lax `str -> date`: a `YYYY-MM-DD` date, else a datetime whose time is exactly
/// midnight (its wall-clock date; the offset is ignored).
///
/// # Errors
/// `(type, message)`: `date_from_datetime_inexact`, `date_from_datetime_parsing` with the
/// datetime parser's error, or `date_parsing` for year 0.
pub(crate) fn parse_date_str(s: &str) -> Result<NaiveDate, (&'static str, String)> {
    let b = s.as_bytes();
    if b.len() == 10
        && let Ok((y, m, d)) = date_partial(b)
    {
        if y == 0 {
            return Err((
                "date_parsing",
                format!(
                    "Input should be a valid date in the format YYYY-MM-DD, {}",
                    DateError::YearZero.message()
                ),
            ));
        }
        if let Some(date) = NaiveDate::from_ymd_opt(y, m, d) {
            return Ok(date);
        }
    }
    match datetime_bytes(b) {
        Ok(dt) => {
            if dt.local.time() == NaiveTime::MIN {
                Ok(dt.local.date())
            } else {
                Err((
                    "date_from_datetime_inexact",
                    "Datetimes provided to dates should have zero time - e.g. be exact dates"
                        .to_owned(),
                ))
            }
        }
        Err(e) => Err((
            "date_from_datetime_parsing",
            format!("Input should be a valid date or datetime, {}", e.message()),
        )),
    }
}

/// speedate's `int_parse_bytes`: an optional `-`, then ASCII digits, within `i64`.
fn int_parse(b: &[u8]) -> Option<i64> {
    let (neg, digits) = match b {
        [b'-', rest @ ..] => (true, rest),
        _ => (false, b),
    };
    if digits.is_empty() {
        return None;
    }
    let mut v: i64 = 0;
    for &c in digits {
        if !c.is_ascii_digit() {
            return None;
        }
        v = v.checked_mul(10)?.checked_sub(i64::from(c - b'0'))?;
    }
    if neg { Some(v) } else { v.checked_neg() }
}

/// What speedate's `float_parse_bytes` makes of a numeric string.
enum Number {
    Int(i64),
    Float(f64),
}

fn number_parse(b: &[u8]) -> Option<Number> {
    let body = match b {
        [b'+' | b'-', rest @ ..] => rest,
        _ => b,
    };
    let dots = body.iter().filter(|&&c| c == b'.').count();
    let digits = body.iter().filter(|c| c.is_ascii_digit()).count();
    if digits == 0 || dots > 1 || digits + dots != body.len() {
        return None;
    }
    if dots == 0 {
        let unsigned = b.strip_prefix(b"+").unwrap_or(b);
        return int_parse(unsigned).map(Number::Int);
    }
    std::str::from_utf8(b)
        .ok()?
        .parse::<f64>()
        .ok()
        .map(Number::Float)
}

/// `DateTime::from_timestamp(seconds, micros)` after the millisecond watershed.
fn from_seconds(mut second: i64, mut micro: i64) -> Result<PyDateTime, DateError> {
    if micro >= 1_000_000 {
        second += micro / 1_000_000;
        micro %= 1_000_000;
    }
    if second >= UNIX_10000 {
        return Err(DateError::DateTooLarge);
    }
    if second < UNIX_0000 {
        return Err(DateError::DateTooSmall);
    }
    let t = DateTime::from_timestamp(second, u32::try_from(micro * 1000).unwrap_or(0))
        .ok_or(DateError::DateTooLarge)?;
    if t.year() == 0 {
        return Err(DateError::YearZero);
    }
    Ok(PyDateTime::from_utc(t))
}

fn from_int(ts: i64) -> Result<PyDateTime, DateError> {
    from_int_micro(ts, 0)
}

/// speedate's `from_timestamp(ts, micro)`: the millisecond watershed applies to the integer part.
fn from_int_micro(ts: i64, micro: i64) -> Result<PyDateTime, DateError> {
    let Some(abs) = ts.checked_abs() else {
        return Err(DateError::DateTooSmall);
    };
    if abs <= MS_WATERSHED {
        return from_seconds(ts, micro);
    }
    let mut seconds = ts / 1000;
    let mut extra = (ts % 1000) * 1000;
    if extra < 0 {
        seconds -= 1;
        extra += 1_000_000;
    }
    from_seconds(seconds, extra + micro)
}

/// pydantic-core's `float_as_datetime`: the floor as the integer timestamp, and the fraction's
/// **magnitude** rounded to microseconds (to milliseconds' microseconds beyond the watershed) (so `-1.25` reads as `-2 + 0.25`, pydantic's own
/// arithmetic, reproduced).
fn from_float(ts: f64) -> Result<PyDateTime, DateError> {
    let floor = ts.floor();
    if floor >= 9.2e18 {
        return Err(DateError::DateTooLarge);
    }
    if floor <= -9.2e18 {
        return Err(DateError::DateTooSmall);
    }
    #[allow(clippy::cast_possible_truncation)]
    let seconds = floor as i64;
    // Beyond the watershed the input is milliseconds, so its fraction is of a millisecond.
    #[allow(clippy::cast_precision_loss)]
    let scale = if ts.abs() > MS_WATERSHED as f64 {
        1_000.0
    } else {
        1_000_000.0
    };
    #[allow(clippy::cast_possible_truncation)]
    let micro = (ts.fract().abs() * scale).round() as i64;
    from_int_micro(seconds, micro)
}

fn datetime_error(path: &str, kind: &str, prefix: &str, e: DateError) -> ErrorEntry {
    entry(path, &format!("{prefix}, {}", e.message()), kind)
}

/// A string: the datetime grammar, then a numeric timestamp, then a bare date (midnight, naive);
/// a failure reports the date parser's error.
pub(crate) fn parse_datetime_str(s: &str) -> Result<PyDateTime, (&'static str, String)> {
    let b = s.as_bytes();
    if let Ok(d) = rfc3339(b) {
        if d.local.year() == 0 {
            return Err((
                "datetime_parsing",
                format!(
                    "Input should be a valid datetime, {}",
                    DateError::YearZero.message()
                ),
            ));
        }
        return Ok(d);
    }
    let numeric = match number_parse(b) {
        Some(Number::Int(i)) => Some(from_int(i)),
        Some(Number::Float(f)) => Some(from_float(f)),
        None => None,
    };
    if let Some(Ok(d)) = numeric {
        return Ok(d);
    }
    // pydantic-core's lax fallback: the input as a date (rfc3339, then an integer timestamp).
    let date_error = match date_partial(b) {
        Ok((y, m, d)) if b.len() == 10 => {
            if y == 0 {
                return Err((
                    "datetime_parsing",
                    format!(
                        "Input should be a valid datetime, {}",
                        DateError::YearZero.message()
                    ),
                ));
            }
            match NaiveDate::from_ymd_opt(y, m, d) {
                Some(date) => {
                    return Ok(PyDateTime {
                        local: date.and_time(NaiveTime::MIN),
                        offset: None,
                    });
                }
                None => DateError::OutOfRangeDay,
            }
        }
        Ok(_) => DateError::ExtraCharacters,
        Err(e) => match int_parse(b) {
            Some(i) => match from_int(i) {
                Ok(_) => e,
                Err(te) => te,
            },
            None => e,
        },
    };
    Err((
        "datetime_from_date_parsing",
        format!(
            "Input should be a valid datetime or date, {}",
            date_error.message()
        ),
    ))
}

fn datetime(path: &str, value: &OrderedJson) -> Result<PyDateTime, ErrorEntry> {
    const PARSING: &str = "Input should be a valid datetime";
    match value {
        OrderedJson::Str(s) => parse_datetime_str(s).map_err(|(kind, msg)| entry(path, &msg, kind)),
        OrderedJson::Int(i) => {
            from_int(*i).map_err(|e| datetime_error(path, "datetime_parsing", PARSING, e))
        }
        OrderedJson::UInt(_) => Err(datetime_error(
            path,
            "datetime_parsing",
            PARSING,
            DateError::DateTooLarge,
        )),
        OrderedJson::BigInt(s) => Err(datetime_error(
            path,
            "datetime_parsing",
            PARSING,
            if s.starts_with('-') {
                DateError::DateTooSmall
            } else {
                DateError::DateTooLarge
            },
        )),
        OrderedJson::Float(f) => {
            if f.is_nan() {
                return Err(entry(
                    path,
                    "Input should be a valid datetime, NaN values not permitted",
                    "datetime_parsing",
                ));
            }
            from_float(*f).map_err(|e| datetime_error(path, "datetime_parsing", PARSING, e))
        }
        _ => Err(entry(path, PARSING, "datetime_type")),
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic)]
    use super::*;

    /// `[input JSON, "OK", pydantic JSON, isoformat]` or `[input JSON, "ERR", type, msg]`, dumped
    /// from pydantic 2.13 (`TypeAdapter(datetime)`).
    const VECTORS: &str = include_str!("datetime_vectors.json");

    #[test]
    fn datetimes_parse_as_pydantic_parses_them() {
        let cases: Vec<Vec<String>> = serde_json::from_str(VECTORS).unwrap();
        assert!(cases.len() > 100);
        let mut failures = Vec::new();
        for c in &cases {
            let Ok(Received::Json(input)) =
                crate::body::receive(Some("application/json"), c[0].as_bytes())
            else {
                panic!("not JSON: {}", c[0]);
            };
            let got = match datetime("x", &input) {
                Ok(d) => vec!["OK".to_owned(), format!("\"{}\"", d.json()), d.isoformat()],
                Err(e) => {
                    let get = |k: &str| match e.iter().find(|(n, _)| n == k) {
                        Some((_, OrderedJson::Str(s))) => s.clone(),
                        _ => String::new(),
                    };
                    vec!["ERR".to_owned(), get("type"), get("message")]
                }
            };
            if got[..] != c[1..] {
                failures.push(format!("{} -> {got:?}, want {:?}", c[0], &c[1..]));
            }
        }
        assert!(failures.is_empty(), "{}", failures.join("\n"));
    }

    /// `[input, "OK", isoformat]` or `[input, type, msg]`, dumped from pydantic 2.13
    /// (`TypeAdapter(date)` on strings: the `{trade_date}` path parameter).
    const DATE_VECTORS: &str = include_str!("date_vectors.json");

    #[test]
    fn dates_parse_as_pydantic_parses_them() {
        let cases: Vec<Vec<String>> = serde_json::from_str(DATE_VECTORS).unwrap();
        let mut failures = Vec::new();
        for c in &cases {
            let got = match parse_date_str(&c[0]) {
                Ok(d) => vec!["OK".to_owned(), crate::wire::py_date(d)],
                Err((kind, msg)) => vec![kind.to_owned(), msg],
            };
            if got[..] != c[1..] {
                failures.push(format!("{:?} -> {got:?}, want {:?}", c[0], &c[1..]));
            }
        }
        assert!(failures.is_empty(), "{}", failures.join("\n"));
    }

    #[test]
    fn literals_are_worded_as_pydantic_words_them() {
        assert_eq!(literal_expected(&["a"]), "'a'");
        assert_eq!(literal_expected(&["a", "b"]), "'a' or 'b'");
        assert_eq!(literal_expected(&["a", "b", "c"]), "'a', 'b' or 'c'");
    }

    #[test]
    fn ints_and_bools_follow_the_lax_rules() {
        let i = |v: OrderedJson| int("f", &v, Some(1), None);
        assert_eq!(i(OrderedJson::Float(5.0)).unwrap().value, Some(5));
        assert!(i(OrderedJson::Float(5.5)).is_err());
        assert_eq!(i(OrderedJson::Bool(true)).unwrap().value, Some(1));
        assert!(i(OrderedJson::Bool(false)).is_err());
        assert_eq!(i(OrderedJson::Str(" 1_0 ".into())).unwrap().value, Some(10));
        assert!(boolean("f", &OrderedJson::Int(2)).is_err());
        assert!(boolean("f", &OrderedJson::Str("Yes".into())).unwrap());
        assert!(boolean("f", &OrderedJson::Null).is_err());
    }
}
