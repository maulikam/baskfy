//! What the six sleeve routers share (lane api-sleeves): the two JSON spellings Python's sleeve
//! routers answer in, pydantic's lax `int`/`bool` query parsing, the sole-tenant guard, and a
//! few row helpers.
//!
//! **Two encoders, one value tree.** The swing, VBT and most TWT routes answer with
//! `baskfy_core.screener.canonical_json(model.model_dump(mode="python"))`: a `Decimal` is an
//! **unquoted** number with its stored scale (`25.00`), a `datetime` is Python's `isoformat()`
//! (`...+00:00`), a float is `json.dumps`'s `repr` (`NaN` for NaN). `/twt/today`, `/twt/backtest`,
//! `/sleeves/state`, `/baskets*` and the portfolio sleeve routes answer with pydantic's own JSON:
//! a `Decimal` is a **quoted** `str(Decimal)`, a UTC `datetime` ends in `Z`, NaN is `null`. [`J`]
//! holds a response once and [`J::canonical`] / [`J::pydantic`] spell it either way.

use std::fmt::Write as _;
use std::str::FromStr;

use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use baskfy_db::OrderedJson;
use chrono::{DateTime, FixedOffset, NaiveDate, Timelike, Utc};
use rust_decimal::{Decimal, RoundingStrategy};
use sqlx::Row;
use sqlx::postgres::PgRow;

use crate::problems::{ErrorEntry, Problem};
use crate::state::AppState;
use crate::wire;

/// One response value, before it is spelled.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum J {
    /// `None`.
    Null,
    /// `bool`.
    Bool(bool),
    /// `int`.
    Int(i64),
    /// `float`.
    Float(f64),
    /// `str`.
    Str(String),
    /// `Decimal`.
    Dec(Decimal),
    /// A `Decimal` negative zero at this many places (`-0.00`). `rust_decimal` has no negative
    /// zero, and Python keeps the operand's sign when a negative value quantises to zero; see
    /// [`quantize_j`].
    NegZero(u32),
    /// `date`.
    Date(NaiveDate),
    /// A UTC `datetime` (what asyncpg returns for `timestamptz`).
    Dt(DateTime<Utc>),
    /// An aware `datetime` with its own offset (IST).
    DtFixed(DateTime<FixedOffset>),
    /// A stored JSON value echoed as Python's `json.loads` read it.
    Raw(OrderedJson),
    /// A `list`.
    Arr(Vec<J>),
    /// A `dict` / model, keys in field order.
    Obj(Vec<(String, J)>),
}

impl J {
    /// `Decimal | None`.
    pub(crate) fn opt_dec(d: Option<Decimal>) -> Self {
        d.map_or(Self::Null, Self::Dec)
    }

    /// `int | None`.
    pub(crate) fn opt_int(i: Option<i64>) -> Self {
        i.map_or(Self::Null, Self::Int)
    }

    /// `date | None`.
    pub(crate) fn opt_date(d: Option<NaiveDate>) -> Self {
        d.map_or(Self::Null, Self::Date)
    }

    /// `datetime | None`.
    pub(crate) fn opt_dt(t: Option<DateTime<Utc>>) -> Self {
        t.map_or(Self::Null, Self::Dt)
    }

    /// `str | None`.
    pub(crate) fn opt_str(s: Option<String>) -> Self {
        s.map_or(Self::Null, Self::Str)
    }

    /// `str`.
    pub(crate) fn s(s: &str) -> Self {
        Self::Str(s.to_owned())
    }

    /// `canonical_json(...)`.
    pub(crate) fn canonical(&self) -> String {
        let mut out = String::new();
        self.write(&mut out, false);
        out
    }

    /// pydantic's `model_dump_json()`.
    pub(crate) fn pydantic(&self) -> String {
        let mut out = String::new();
        self.write(&mut out, true);
        out
    }

    fn write(&self, out: &mut String, pydantic: bool) {
        match self {
            Self::Null => out.push_str("null"),
            Self::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Self::Int(i) => {
                let _ = write!(out, "{i}");
            }
            Self::Float(f) => out.push_str(&float_token(*f, pydantic)),
            Self::Str(s) => json_str(s, out),
            Self::Dec(d) => {
                if pydantic {
                    json_str(&wire::py_decimal(*d), out);
                } else {
                    out.push_str(&d.to_string());
                }
            }
            Self::NegZero(dp) => {
                let text = if *dp == 0 {
                    "-0".to_owned()
                } else {
                    format!("-0.{}", "0".repeat(*dp as usize))
                };
                if pydantic {
                    json_str(&text, out);
                } else {
                    out.push_str(&text);
                }
            }
            Self::Date(d) => json_str(&wire::py_date(*d), out),
            Self::Dt(t) => {
                let text = if pydantic {
                    wire::py_datetime(*t)
                } else {
                    py_isoformat(&t.fixed_offset())
                };
                json_str(&text, out);
            }
            Self::DtFixed(t) => {
                let text = if pydantic {
                    wire::py_datetime_fixed(*t)
                } else {
                    py_isoformat(t)
                };
                json_str(&text, out);
            }
            Self::Raw(v) => write_raw(v, out, pydantic),
            Self::Arr(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    item.write(out, pydantic);
                }
                out.push(']');
            }
            Self::Obj(entries) => {
                out.push('{');
                for (i, (k, v)) in entries.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    json_str(k, out);
                    out.push(':');
                    v.write(out, pydantic);
                }
                out.push('}');
            }
        }
    }
}

/// Builds a [`J::Obj`] in field order.
#[derive(Debug, Default)]
pub(crate) struct Obj(Vec<(String, J)>);

impl Obj {
    /// An empty model.
    pub(crate) fn new() -> Self {
        Self::default()
    }

    /// Adds `key: value`.
    pub(crate) fn put(mut self, key: &str, value: J) -> Self {
        self.0.push((key.to_owned(), value));
        self
    }

    /// The finished value.
    pub(crate) fn done(self) -> J {
        J::Obj(self.0)
    }
}

fn float_token(f: f64, pydantic: bool) -> String {
    if f.is_nan() {
        if pydantic { "null" } else { "NaN" }.to_owned()
    } else if f.is_infinite() {
        if pydantic {
            "null".to_owned()
        } else if f > 0.0 {
            "Infinity".to_owned()
        } else {
            "-Infinity".to_owned()
        }
    } else {
        baskfy_core::py::float_repr(f)
    }
}

fn write_raw(v: &OrderedJson, out: &mut String, pydantic: bool) {
    match v {
        OrderedJson::Null => out.push_str("null"),
        OrderedJson::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        OrderedJson::Int(i) => {
            let _ = write!(out, "{i}");
        }
        OrderedJson::UInt(u) => {
            let _ = write!(out, "{u}");
        }
        OrderedJson::BigInt(big) => out.push_str(big),
        OrderedJson::Float(f) => out.push_str(&float_token(*f, pydantic)),
        OrderedJson::Str(s) => json_str(s, out),
        OrderedJson::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_raw(item, out, pydantic);
            }
            out.push(']');
        }
        OrderedJson::Object(entries) => {
            out.push('{');
            for (i, (k, v)) in entries.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                json_str(k, out);
                out.push(':');
                write_raw(v, out, pydantic);
            }
            out.push('}');
        }
    }
}

/// `json.dumps(s, ensure_ascii=False)`.
fn json_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Python's `datetime.isoformat()` of an aware datetime: `+00:00`, never `Z`; microseconds only
/// when non-zero; an offset with seconds keeps them.
pub(crate) fn py_isoformat(t: &DateTime<FixedOffset>) -> String {
    let micros = t.nanosecond() / 1000 % 1_000_000;
    let mut out = format!(
        "{}T{:02}:{:02}:{:02}",
        wire::py_date(t.date_naive()),
        t.hour(),
        t.minute(),
        t.second()
    );
    if micros != 0 {
        let _ = write!(out, ".{micros:06}");
    }
    let offset = t.offset().local_minus_utc();
    let sign = if offset < 0 { '-' } else { '+' };
    let abs = offset.unsigned_abs();
    let _ = write!(out, "{sign}{:02}:{:02}", abs / 3600, abs % 3600 / 60);
    if !abs.is_multiple_of(60) {
        let _ = write!(out, ":{:02}", abs % 60);
    }
    out
}

/// `Response(content=canonical_json(...), media_type="application/json")`.
pub(crate) fn canonical_response(value: &J) -> Response {
    text_response(value.canonical())
}

/// FastAPI's `response_model` rendering (pydantic JSON).
pub(crate) fn pydantic_response(value: &J) -> Response {
    text_response(value.pydantic())
}

fn text_response(body: String) -> Response {
    let mut response = (StatusCode::OK, body).into_response();
    response
        .headers_mut()
        .insert(header::CONTENT_TYPE, HeaderValue::from_static(wire::JSON));
    response
}

// ---------------------------------------------------------------------------------------------
// Decimal arithmetic in Python's context
// ---------------------------------------------------------------------------------------------

/// `value.quantize(Decimal("1e-{dp}"))` under Python's default `ROUND_HALF_EVEN`.
pub(crate) fn quantize(value: Decimal, dp: u32) -> Decimal {
    let mut out = value.round_dp_with_strategy(dp, RoundingStrategy::MidpointNearestEven);
    out.rescale(dp);
    out
}

/// `value.quantize(Decimal("1e-{dp}"))` as a response value. Python keeps the operand's sign
/// on a result that rounds to zero (`Decimal("-0.004").quantize(Decimal("0.01"))` is `-0.00`,
/// which both encoders spell with its minus sign); `rust_decimal` normalises it away, so the
/// sign is carried in [`J::NegZero`].
pub(crate) fn quantize_j(value: Decimal, dp: u32) -> J {
    let q = quantize(value, dp);
    if q.is_zero() && !value.is_zero() && value.is_sign_negative() {
        J::NegZero(dp)
    } else {
        J::Dec(q)
    }
}

/// `baskfy_core.precision.quantise(value, dp)`: `ROUND_HALF_UP`.
pub(crate) fn quantise_half_up(value: Decimal, dp: u32) -> Decimal {
    let mut out = value.round_dp_with_strategy(dp, RoundingStrategy::MidpointAwayFromZero);
    out.rescale(dp);
    out
}

/// `Decimal(str(x))` for a Python float `x`: repr's digits (`40.0`, `83.8`).
pub(crate) fn decimal_of_float(x: f64) -> Decimal {
    Decimal::from_str(&baskfy_core::py::float_repr(x)).unwrap_or_default()
}

/// A division that fails like a database error would: as the 500 catch-all.
pub(crate) fn div(a: Decimal, b: Decimal) -> Result<Decimal, Problem> {
    a.checked_div(b).ok_or_else(crate::problems::internal_error)
}

/// A multiplication beyond `Decimal`'s range answers the 500 catch-all.
pub(crate) fn mul(a: Decimal, b: Decimal) -> Result<Decimal, Problem> {
    a.checked_mul(b).ok_or_else(crate::problems::internal_error)
}

// ---------------------------------------------------------------------------------------------
// pydantic's lax query parsing
// ---------------------------------------------------------------------------------------------

/// What pydantic makes of an `int` query value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum LaxInt {
    /// A valid integer that fits `i64`.
    Small(i64),
    /// A valid integer beyond `i64` (pydantic keeps it; asyncpg then refuses it: a 500).
    Big {
        /// The sign of the value.
        negative: bool,
    },
    /// `int_parsing`.
    Invalid,
}

/// pydantic-core's `str` → `int` (lax), in this lane's shape. The rule is
/// [`crate::routes::common::lax_int`] (R5.10's collapse).
pub(crate) fn lax_int(raw: &str) -> LaxInt {
    match crate::routes::common::lax_int(raw) {
        None => LaxInt::Invalid,
        Some(p) => p.as_i64().map_or(
            LaxInt::Big {
                negative: p.negative,
            },
            LaxInt::Small,
        ),
    }
}

pub(crate) use crate::routes::common::lax_bool;

/// Validates an `int` parameter into the accumulator: `int_parsing` on failure.
pub(crate) fn int_param(
    v: &mut crate::query::Validation,
    field: &str,
    raw: Option<&str>,
) -> Option<LaxInt> {
    let raw = raw?;
    match lax_int(raw) {
        LaxInt::Invalid => {
            v.error(
                field,
                "Input should be a valid integer, unable to parse string as an integer",
                "int_parsing",
            );
            None
        }
        ok => Some(ok),
    }
}

/// An id that pydantic accepted but no `bigint` column can hold: asyncpg refuses to encode it,
/// which Python answers with the 500 catch-all.
pub(crate) fn id_or_500(id: LaxInt) -> Result<i64, Problem> {
    match id {
        LaxInt::Small(v) => Ok(v),
        LaxInt::Big { .. } | LaxInt::Invalid => Err(crate::problems::internal_error()),
    }
}

/// A one-error `errors[]` entry without a `type` (`{"field", "message"}`), as the routers raise
/// with `Problem(BAD_REQUEST, ..., errors=[...])`.
pub(crate) fn field_error(field: &str, message: &str) -> ErrorEntry {
    vec![
        ("field".to_owned(), OrderedJson::Str(field.to_owned())),
        ("message".to_owned(), OrderedJson::Str(message.to_owned())),
    ]
}

/// Python's `repr(list_of_str)`: `['EP', 'FLAG']`.
pub(crate) fn py_list_repr(items: &[&str]) -> String {
    let inner: Vec<String> = items
        .iter()
        .map(|s| baskfy_providers::py::repr_str(s))
        .collect();
    format!("[{}]", inner.join(", "))
}

// ---------------------------------------------------------------------------------------------
// The sole tenant (`baskfy_api.curated_tenant`, `curated_seed.resolve_sole_user_id`)
// ---------------------------------------------------------------------------------------------

/// `scoped_sole_user_id(session, principal_user_id, surface=None)`: the sole tenant, or a 404
/// carrying `reason: not-the-sole-tenant` for any other caller. The rule is
/// [`crate::routes::common::scoped_sole_user_id`] (R5.10's collapse, this lane's copy the model).
///
/// # Errors
/// As the shared function.
pub(crate) async fn scoped_sole_user_id(
    state: &AppState,
    principal_user_id: Option<i64>,
    surface: Option<&str>,
) -> Result<i64, Problem> {
    crate::routes::common::scoped_sole_user_id(&state.db, principal_user_id, surface).await
}

// ---------------------------------------------------------------------------------------------
// Rows
// ---------------------------------------------------------------------------------------------

/// A database error as Python answers it (the 500 catch-all, logged).
pub(crate) fn db(e: sqlx::Error) -> Problem {
    crate::db_failure(&e.into())
}

/// A row decode failure, likewise.
pub(crate) fn dbe(e: baskfy_db::DbError) -> Problem {
    crate::db_failure(&e)
}

/// `row[col]` typed, a decode failure as the 500 catch-all.
pub(crate) fn get<'r, T>(row: &'r PgRow, col: &str) -> Result<T, Problem>
where
    T: sqlx::Decode<'r, sqlx::Postgres> + sqlx::Type<sqlx::Postgres>,
{
    row.try_get::<T, _>(col).map_err(db)
}

/// A non-null `numeric` with its scale.
pub(crate) fn dec(row: &PgRow, col: &str) -> Result<Decimal, Problem> {
    baskfy_db::row::decimal(row, col).map_err(dbe)
}

/// A nullable `numeric` with its scale.
pub(crate) fn odec(row: &PgRow, col: &str) -> Result<Option<Decimal>, Problem> {
    baskfy_db::row::opt_decimal(row, col).map_err(dbe)
}

/// A nullable `jsonb`: SQL NULL and JSON `null` both `None`, as SQLAlchemy reads them.
pub(crate) fn ojson(row: &PgRow, col: &str) -> Result<Option<OrderedJson>, Problem> {
    Ok(baskfy_db::row::opt_jsonb(row, col)
        .map_err(dbe)?
        .filter(|v| *v != OrderedJson::Null))
}

/// `value if isinstance(value, dict) else None`.
pub(crate) fn as_dict(v: Option<OrderedJson>) -> Option<OrderedJson> {
    v.filter(|v| matches!(v, OrderedJson::Object(_)))
}

/// `int(v) if isinstance(v, int) else None` — Python's `bool` is an `int` too.
pub(crate) fn json_int(v: Option<&OrderedJson>) -> Option<i64> {
    match v? {
        OrderedJson::Int(i) => Some(*i),
        OrderedJson::Bool(b) => Some(i64::from(*b)),
        _ => None,
    }
}

/// `int(v) if isinstance(v, int) and not isinstance(v, bool) else None`.
pub(crate) fn json_strict_int(v: Option<&OrderedJson>) -> Option<i64> {
    match v? {
        OrderedJson::Int(i) => Some(*i),
        _ => None,
    }
}

/// Python's `str(value)` of a value `json.loads` produced.
pub(crate) fn py_str_json(v: &OrderedJson) -> String {
    match v {
        OrderedJson::Str(s) => s.clone(),
        other => py_repr_json(other),
    }
}

/// Python's `repr(value)` of a value `json.loads` produced.
pub(crate) fn py_repr_json(v: &OrderedJson) -> String {
    match v {
        OrderedJson::Null => "None".to_owned(),
        OrderedJson::Bool(b) => if *b { "True" } else { "False" }.to_owned(),
        OrderedJson::Int(i) => i.to_string(),
        OrderedJson::UInt(u) => u.to_string(),
        OrderedJson::BigInt(big) => big.clone(),
        OrderedJson::Float(f) => {
            if f.is_nan() {
                "nan".to_owned()
            } else if f.is_infinite() {
                if *f > 0.0 { "inf" } else { "-inf" }.to_owned()
            } else {
                baskfy_core::py::float_repr(*f)
            }
        }
        OrderedJson::Str(s) => baskfy_providers::py::repr_str(s),
        OrderedJson::Array(items) => format!(
            "[{}]",
            items
                .iter()
                .map(py_repr_json)
                .collect::<Vec<_>>()
                .join(", ")
        ),
        OrderedJson::Object(entries) => format!(
            "{{{}}}",
            entries
                .iter()
                .map(|(k, v)| format!("{}: {}", baskfy_providers::py::repr_str(k), py_repr_json(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

/// The current UTC instant.
pub(crate) fn now_utc() -> DateTime<Utc> {
    crate::entitlements::utc_now()
}

/// India Standard Time.
pub(crate) fn ist() -> FixedOffset {
    baskfy_providers::tokens::ist()
}

/// Python's `datetime.fromisoformat(stamp)` for the shapes the workers write
/// (`YYYY-MM-DD[T ]HH:MM[:SS[.ffffff]][Z|±HH:MM]`). `None` where Python raises — and where it is
/// naive, which no writer stores (recorded in DECISIONS-RS).
pub(crate) fn from_isoformat(stamp: &str) -> Option<DateTime<FixedOffset>> {
    let normalised = if let Some(head) = stamp.strip_suffix('Z') {
        format!("{head}+00:00")
    } else {
        stamp.to_owned()
    };
    for fmt in [
        "%Y-%m-%dT%H:%M:%S%.f%:z",
        "%Y-%m-%d %H:%M:%S%.f%:z",
        "%Y-%m-%dT%H:%M%:z",
        "%Y-%m-%d %H:%M%:z",
    ] {
        if let Ok(t) = DateTime::parse_from_str(&normalised, fmt) {
            return Some(t);
        }
    }
    None
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn lax_int_is_pydantics() {
        // Each outcome checked against pydantic 2.13 `TypeAdapter(int).validate_python(s)`.
        for (s, want) in [
            (" 5 ", LaxInt::Small(5)),
            ("+5", LaxInt::Small(5)),
            ("-5", LaxInt::Small(-5)),
            ("1_000", LaxInt::Small(1000)),
            ("05", LaxInt::Small(5)),
            ("1.0", LaxInt::Small(1)),
            ("1.00", LaxInt::Small(1)),
            ("1_000.0", LaxInt::Small(1000)),
            ("-0.00", LaxInt::Small(0)),
            ("\t7\n", LaxInt::Small(7)),
            ("\u{a0}5", LaxInt::Small(5)),
            ("99999999999999999999", LaxInt::Big { negative: false }),
            ("-9999999999999999999", LaxInt::Big { negative: true }),
            ("1.", LaxInt::Invalid),
            ("1e3", LaxInt::Invalid),
            ("0x10", LaxInt::Invalid),
            ("", LaxInt::Invalid),
            (" ", LaxInt::Invalid),
            ("5a", LaxInt::Invalid),
            ("1.50", LaxInt::Invalid),
            (".0", LaxInt::Invalid),
            ("1__0", LaxInt::Invalid),
            ("_1", LaxInt::Invalid),
            ("1_", LaxInt::Invalid),
            ("+-1", LaxInt::Invalid),
            ("1._0", LaxInt::Invalid),
            ("\u{663}", LaxInt::Invalid),
        ] {
            assert_eq!(lax_int(s), want, "{s:?}");
        }
    }

    #[test]
    fn lax_bool_is_pydantics() {
        for (s, want) in [
            ("true", Some(true)),
            ("TRUE", Some(true)),
            ("1", Some(true)),
            ("yes", Some(true)),
            ("on", Some(true)),
            ("t", Some(true)),
            ("y", Some(true)),
            ("0", Some(false)),
            ("off", Some(false)),
            ("No", Some(false)),
            (" true", None),
            ("", None),
            ("maybe", None),
        ] {
            assert_eq!(lax_bool(s), want, "{s:?}");
        }
    }

    #[test]
    fn the_two_spellings() {
        let t = DateTime::parse_from_rfc3339("2026-10-29T04:45:14.478474Z")
            .unwrap()
            .with_timezone(&Utc);
        let v = Obj::new()
            .put("d", J::Dec(Decimal::from_str("25.00").unwrap()))
            .put("t", J::Dt(t))
            .put("f", J::Float(f64::NAN))
            .put("s", J::s("a\"—"))
            .done();
        assert_eq!(
            v.canonical(),
            r#"{"d":25.00,"t":"2026-10-29T04:45:14.478474+00:00","f":NaN,"s":"a\"—"}"#
        );
        assert_eq!(
            v.pydantic(),
            r#"{"d":"25.00","t":"2026-10-29T04:45:14.478474Z","f":null,"s":"a\"—"}"#
        );
    }

    #[test]
    fn isoformat_round_trips_what_the_workers_write() {
        for s in [
            "2026-10-29T04:45:14.676147+00:00",
            "2026-10-29T10:15:14+05:30",
            "2026-10-29T10:15:00+05:30",
        ] {
            assert_eq!(py_isoformat(&from_isoformat(s).unwrap()), s);
        }
        assert_eq!(
            py_isoformat(&from_isoformat("2026-10-29T04:45:14Z").unwrap()),
            "2026-10-29T04:45:14+00:00"
        );
        assert!(from_isoformat("not a time").is_none());
    }

    // `the_sole_tenant_variable_is_read_as_pythons_int` moved with the function to
    // `routes::common` (R5.10's collapse).

    #[test]
    fn a_negative_value_that_rounds_to_zero_keeps_its_sign() {
        let d = |v: &str| Decimal::from_str(v).unwrap();
        assert_eq!(quantize_j(d("-0.004"), 2), J::NegZero(2));
        assert_eq!(quantize_j(d("-0.004"), 2).canonical(), "-0.00");
        assert_eq!(quantize_j(d("-0.004"), 2).pydantic(), "\"-0.00\"");
        assert_eq!(quantize_j(d("-0.4"), 0).canonical(), "-0");
        assert_eq!(quantize_j(d("-0.005"), 2).canonical(), "-0.00");
        assert_eq!(quantize_j(d("-0.006"), 2).canonical(), "-0.01");
        assert_eq!(quantize_j(d("0.004"), 2).canonical(), "0.00");
        assert_eq!(quantize_j(d("0"), 2).canonical(), "0.00");
        assert_eq!(quantize_j(d("-0.00003"), 4).pydantic(), "\"-0.0000\"");
    }

    #[test]
    fn quantize_is_half_even_and_keeps_the_scale() {
        let d = |s: &str| Decimal::from_str(s).unwrap();
        assert_eq!(quantize(d("2.345"), 2).to_string(), "2.34");
        assert_eq!(quantize(d("2.355"), 2).to_string(), "2.36");
        assert_eq!(quantize(d("5"), 2).to_string(), "5.00");
        assert_eq!(decimal_of_float(40.0).to_string(), "40.0");
    }

    /// Ports the structural halves of `test_swing_readonly.py`, `test_twt_readonly.py`,
    /// `test_vbt_readonly.py`, `test_baskets_readonly.py` and `test_sleeves_are_not_orders.py`:
    /// the lane's routers register **only** the money-free writes Python allows (wave 4c, lane
    /// writes-sleeves) — `sleeve_state` and `baskets` none at all — and name no execution path.
    #[test]
    fn the_lanes_routers_write_only_what_python_allows_and_reach_no_order() {
        for (name, source, writes) in [
            (
                "swing",
                include_str!("../swing.rs"),
                &[
                    "post(writes::post_scan",
                    "patch(writes::patch_watch",
                    ".delete(writes::delete_watch",
                    ".post(writes::post_watch",
                    ".patch(writes::patch_config",
                ][..],
            ),
            (
                "twt",
                include_str!("../twt.rs"),
                &["post(writes::post_twt_scan"][..],
            ),
            (
                "vbt",
                include_str!("../vbt.rs"),
                &[
                    "post(writes::post_vbt_scan",
                    ".patch(writes::patch_vbt_config",
                ][..],
            ),
            (
                "sleeves",
                include_str!("../sleeves.rs"),
                &[".put(replace::replace_sleeves"][..],
            ),
            ("sleeve_state", include_str!("../sleeve_state.rs"), &[][..]),
            ("baskets", include_str!("../baskets.rs"), &[][..]),
            ("common", include_str!("common.rs"), &[][..]),
        ] {
            let code = source.split("#[cfg(test)]").next().unwrap_or(source);
            let mut stripped = code.split_whitespace().collect::<String>();
            for allowed in writes {
                let allowed: String = allowed.split_whitespace().collect();
                assert!(stripped.contains(&allowed), "{name} lost {allowed}");
                stripped = stripped.replacen(&allowed, "", 1);
            }
            for verb in [
                ".post(", ".patch(", ".delete(", "(post(", "(put(", "(patch(", "(delete(",
            ] {
                assert!(
                    !stripped.contains(verb),
                    "{name} registers another write: {verb}"
                );
            }
            // `.put("key", value)` is the response builder; a `.put(` routing a handler is not.
            assert!(
                !stripped
                    .match_indices(".put(")
                    .any(|(i, _)| !stripped[i + 5..].starts_with('"')),
                "{name} registers another write: put"
            );
            for sql in ["INSERT ", "UPDATE ", "DELETE FROM", "TRUNCATE", "begin()"] {
                assert!(!code.contains(sql), "{name} contains {sql}");
            }
            for order in ["baskfy_execution", "place_order", "place_gtt", "kite::"] {
                assert!(!code.contains(order), "{name} names {order}");
            }
        }
    }
}
