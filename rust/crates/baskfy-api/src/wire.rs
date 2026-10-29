//! The response encoding every route shares: FastAPI's JSON, byte for byte.
//!
//! FastAPI renders a `response_model` with pydantic's `mode="json"` serialisation and then
//! `json.dumps(..., ensure_ascii=False, allow_nan=False, separators=(",", ":"))`. So:
//!
//! * **keys** in model field order (a Rust struct's field order; never a sorted map — use
//!   [`baskfy_db::OrderedJson`] for a dict built at runtime),
//! * **`Decimal`** as a JSON **string**, Python's `str(Decimal)` — `"1.5000"`, `"0.00"`,
//!   `"1E-7"` ([`py_decimal`]; serde helper [`ser::decimal`]),
//! * **`datetime`** as pydantic prints it: `2026-08-26T13:52:55.316805Z` — `Z` for UTC,
//!   `+05:30` otherwise, six fractional digits only when the microsecond is non-zero
//!   ([`py_datetime`], [`ser::datetime`]),
//! * **`date`** as `YYYY-MM-DD` ([`py_date`], [`ser::date`]),
//! * **`float`** as Python's `repr` (`1e-05`, `1e+16`, `100000.0`); NaN and the infinities become
//!   `null` (pydantic's `ser_json_inf_nan="null"`) ([`ser::float`]),
//! * non-ASCII text raw (UTF-8), no spaces.
//!
//! Every lane serialises through these helpers; none formats a number itself.

use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use chrono::{DateTime, Datelike, FixedOffset, NaiveDate, NaiveDateTime, Timelike, Utc};
use rust_decimal::Decimal;
use serde::Serialize;

/// `application/json` — FastAPI's `JSONResponse.media_type`.
pub const JSON: &str = "application/json";

/// `json.dumps` of `value` in FastAPI's spelling. A value that cannot be encoded (a non-finite
/// float outside [`ser::float`]) renders as an empty body — and is a bug in the route, logged.
#[must_use]
pub fn to_json_bytes<T: Serialize + ?Sized>(value: &T) -> Vec<u8> {
    match serde_json::to_vec(value) {
        Ok(bytes) => bytes,
        Err(e) => {
            crate::logging::error(
                "baskfy_api.wire",
                "response did not encode",
                &[("error", &e.to_string())],
            );
            Vec::new()
        }
    }
}

/// A `200 application/json` response of `value`.
#[must_use]
pub fn json<T: Serialize + ?Sized>(value: &T) -> Response {
    json_status(StatusCode::OK, value)
}

/// A `status` `application/json` response of `value`.
#[must_use]
pub fn json_status<T: Serialize + ?Sized>(status: StatusCode, value: &T) -> Response {
    let mut response = (status, to_json_bytes(value)).into_response();
    response
        .headers_mut()
        .insert(header::CONTENT_TYPE, HeaderValue::from_static(JSON));
    response
}

/// Python's `str(Decimal)` (to-scientific-string): plain notation when the exponent is `<= 0` and
/// the adjusted exponent is `>= -6`, scientific (`1E-7`, `1.5E-8`) otherwise.
#[must_use]
pub fn py_decimal(d: Decimal) -> String {
    let digits = d.mantissa().unsigned_abs().to_string();
    let exponent = -i64::from(d.scale());
    let sign = if d.is_sign_negative() { "-" } else { "" };
    let n = i64::try_from(digits.len()).unwrap_or(i64::MAX);
    let adjusted = exponent + n - 1;
    if exponent <= 0 && adjusted >= -6 {
        if exponent == 0 {
            return format!("{sign}{digits}");
        }
        let frac = usize::try_from(-exponent).unwrap_or(0);
        if digits.len() > frac {
            let split = digits.len() - frac;
            return format!("{sign}{}.{}", &digits[..split], &digits[split..]);
        }
        return format!("{sign}0.{}{digits}", "0".repeat(frac - digits.len()));
    }
    let mut out = String::from(sign);
    out.push_str(&digits[..1]);
    if digits.len() > 1 {
        out.push('.');
        out.push_str(&digits[1..]);
    }
    out.push('E');
    if adjusted >= 0 {
        out.push('+');
    }
    out.push_str(&adjusted.to_string());
    out
}

/// `date.isoformat()`: `YYYY-MM-DD`, the year zero-padded to four digits.
#[must_use]
pub fn py_date(d: NaiveDate) -> String {
    format!("{:04}-{:02}-{:02}", d.year(), d.month(), d.day())
}

fn clock(t: &impl Timelike) -> String {
    let micros = t.nanosecond() / 1000 % 1_000_000;
    let base = format!("{:02}:{:02}:{:02}", t.hour(), t.minute(), t.second());
    if micros == 0 {
        base
    } else {
        format!("{base}.{micros:06}")
    }
}

/// An aware `datetime` as pydantic serialises it: `Z` for a zero offset, `±HH:MM` otherwise.
#[must_use]
pub fn py_datetime_fixed(t: DateTime<FixedOffset>) -> String {
    let offset = t.offset().local_minus_utc();
    let zone = if offset == 0 {
        "Z".to_owned()
    } else {
        let sign = if offset < 0 { '-' } else { '+' };
        let abs = offset.unsigned_abs();
        format!("{sign}{:02}:{:02}", abs / 3600, abs % 3600 / 60)
    };
    format!("{}T{}{zone}", py_date(t.date_naive()), clock(&t.time()))
}

/// A UTC `datetime` (what asyncpg returns for every `timestamptz`): `...Z`.
#[must_use]
pub fn py_datetime(t: DateTime<Utc>) -> String {
    format!("{}T{}Z", py_date(t.date_naive()), clock(&t.time()))
}

/// A naive `datetime`: no zone suffix.
#[must_use]
pub fn py_naive_datetime(t: NaiveDateTime) -> String {
    format!("{}T{}", py_date(t.date()), clock(&t.time()))
}

/// Python's `repr(float)` as a raw JSON number, or `null` for NaN and the infinities.
#[must_use]
pub fn py_float_json(x: f64) -> String {
    if x.is_finite() {
        baskfy_core::py::float_repr(x)
    } else {
        "null".to_owned()
    }
}

/// `serialize_with` helpers for response structs.
pub mod ser {
    use chrono::{DateTime, FixedOffset, NaiveDate, Utc};
    use rust_decimal::Decimal;
    use serde::Serializer;
    use serde_json::value::RawValue;

    /// A `Decimal` field: Python's `str(Decimal)`, as a JSON string.
    ///
    /// # Errors
    /// The serializer's.
    pub fn decimal<S: Serializer>(d: &Decimal, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&super::py_decimal(*d))
    }

    /// A `Decimal | None` field.
    ///
    /// # Errors
    /// The serializer's.
    pub fn opt_decimal<S: Serializer>(d: &Option<Decimal>, s: S) -> Result<S::Ok, S::Error> {
        match d {
            Some(d) => decimal(d, s),
            None => s.serialize_none(),
        }
    }

    /// A UTC `datetime` field.
    ///
    /// # Errors
    /// The serializer's.
    pub fn datetime<S: Serializer>(t: &DateTime<Utc>, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&super::py_datetime(*t))
    }

    /// A UTC `datetime | None` field.
    ///
    /// # Errors
    /// The serializer's.
    pub fn opt_datetime<S: Serializer>(t: &Option<DateTime<Utc>>, s: S) -> Result<S::Ok, S::Error> {
        match t {
            Some(t) => datetime(t, s),
            None => s.serialize_none(),
        }
    }

    /// An aware `datetime` with its own offset (an IST clock, say).
    ///
    /// # Errors
    /// The serializer's.
    pub fn datetime_fixed<S: Serializer>(
        t: &DateTime<FixedOffset>,
        s: S,
    ) -> Result<S::Ok, S::Error> {
        s.serialize_str(&super::py_datetime_fixed(*t))
    }

    /// A `date` field.
    ///
    /// # Errors
    /// The serializer's.
    pub fn date<S: Serializer>(d: &NaiveDate, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&super::py_date(*d))
    }

    /// A `date | None` field.
    ///
    /// # Errors
    /// The serializer's.
    pub fn opt_date<S: Serializer>(d: &Option<NaiveDate>, s: S) -> Result<S::Ok, S::Error> {
        match d {
            Some(d) => date(d, s),
            None => s.serialize_none(),
        }
    }

    /// A `list[date]` field.
    ///
    /// # Errors
    /// The serializer's.
    pub fn dates<S: Serializer>(ds: &[NaiveDate], s: S) -> Result<S::Ok, S::Error> {
        s.collect_seq(ds.iter().map(|d| super::py_date(*d)))
    }

    /// A `float` field: `repr(float)`; NaN and the infinities as `null`.
    ///
    /// # Errors
    /// The serializer's.
    pub fn float<S: Serializer>(x: &f64, s: S) -> Result<S::Ok, S::Error> {
        let raw =
            RawValue::from_string(super::py_float_json(*x)).map_err(serde::ser::Error::custom)?;
        serde::Serialize::serialize(&raw, s)
    }

    /// A `float | None` field.
    ///
    /// # Errors
    /// The serializer's.
    pub fn opt_float<S: Serializer>(x: &Option<f64>, s: S) -> Result<S::Ok, S::Error> {
        match x {
            Some(x) => float(x, s),
            None => s.serialize_none(),
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;
    use std::str::FromStr;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).unwrap()
    }

    #[test]
    fn decimal_is_pythons_str() {
        // str(Decimal(x)) for each, checked against CPython 3.12.
        for (x, py) in [
            ("1.5000", "1.5000"),
            ("0.00", "0.00"),
            ("0.0000001", "1E-7"),
            ("0.000001", "0.000001"),
            ("0.00000123", "0.00000123"),
            ("0.000000123", "1.23E-7"),
            ("-12.30", "-12.30"),
            ("123456789", "123456789"),
            ("0", "0"),
            ("-0.5", "-0.5"),
        ] {
            assert_eq!(py_decimal(d(x)), py, "{x}");
        }
    }

    #[test]
    fn datetimes_are_pydantics() {
        let t = DateTime::parse_from_rfc3339("2026-01-02T03:04:05.316800Z").unwrap();
        assert_eq!(
            py_datetime(t.with_timezone(&Utc)),
            "2026-01-02T03:04:05.316800Z"
        );
        let t = DateTime::parse_from_rfc3339("2026-01-02T03:04:05Z").unwrap();
        assert_eq!(py_datetime(t.with_timezone(&Utc)), "2026-01-02T03:04:05Z");
        let t = DateTime::parse_from_rfc3339("2026-01-02T03:04:05.000010+05:30").unwrap();
        assert_eq!(py_datetime_fixed(t), "2026-01-02T03:04:05.000010+05:30");
        let n = NaiveDate::from_ymd_opt(2026, 1, 2)
            .unwrap()
            .and_hms_opt(3, 4, 5)
            .unwrap();
        assert_eq!(py_naive_datetime(n), "2026-01-02T03:04:05");
        assert_eq!(
            py_date(NaiveDate::from_ymd_opt(33, 1, 2).unwrap()),
            "0033-01-02"
        );
    }

    #[derive(serde::Serialize)]
    struct F {
        #[serde(serialize_with = "ser::float")]
        a: f64,
        #[serde(serialize_with = "ser::opt_float")]
        b: Option<f64>,
    }

    #[test]
    fn floats_are_pythons_repr() {
        let row = |a, b| String::from_utf8(to_json_bytes(&F { a, b })).unwrap();
        assert_eq!(row(1e16, Some(1e-5)), r#"{"a":1e+16,"b":1e-05}"#);
        assert_eq!(row(100_000.0, None), r#"{"a":100000.0,"b":null}"#);
        assert_eq!(row(f64::NAN, Some(0.1)), r#"{"a":null,"b":0.1}"#);
    }
}
