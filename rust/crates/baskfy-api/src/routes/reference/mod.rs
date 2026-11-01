//! The reference-data lane's shared plumbing (RS5, lane `api-reference`): what `meta`,
//! `market_data`, `instruments`, `search`, `explore`, `kite` and `desk` all lean on, ported once.
//!
//! * the as-of resolution of `baskfy_api.screener` (`current_data_version`,
//!   `latest_published_date`, `resolve_as_of`) and the exceptions `app.py` maps onto problems;
//! * the canonical encoder those routers answer with (`baskfy_core.screener.canonical_json` over
//!   `model_dump(mode="python")`), with `snapshot_headers`;
//! * pydantic's lax query-parameter parsing for the types these routes declare (`int`, `bool`,
//!   `Decimal`, constrained `str`, `Literal`), with its exact error words;
//! * `curated_tenant.scoped_sole_user_id` and `invoices.today_ist`.
//!
//! Private to the lane: the route files reach it as `super::meta::reference`. Nothing here writes.

pub(crate) mod curated;
pub(crate) mod factsheet;
pub(crate) mod regime;
pub(crate) mod universes;

use axum::http::{HeaderName, HeaderValue, header};
use axum::response::{IntoResponse, Response};
use baskfy_core::screener::{Payload, PyDecimal};
use chrono::{DateTime, NaiveDate, Utc};
use rust_decimal::Decimal;

use crate::problems::{Problem, no_trading_day, pipeline_degraded};
use crate::query::{QueryParams, Validation};
use crate::state::AppState;

// ---------------------------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------------------------

/// A sqlx failure, answered as Python's 500 catch-all.
pub(crate) fn db(e: sqlx::Error) -> Problem {
    crate::db_failure(&e.into())
}

/// A `baskfy_db` decode failure, likewise.
pub(crate) fn dbe(e: &baskfy_db::DbError) -> Problem {
    crate::db_failure(e)
}

/// The catch-all for a state Python would raise on (an invariant broken in the data).
pub(crate) fn unexpected(what: &str) -> Problem {
    crate::logging::error("baskfy_api.app", "unhandled error", &[("error", what)]);
    crate::problems::internal_error()
}

// ---------------------------------------------------------------------------------------------
// As-of resolution (`baskfy_api.screener`)
// ---------------------------------------------------------------------------------------------

/// `DATA_START_DATE`: docs/01 §2.13's "from 1 Nov 2024".
pub(crate) fn data_start_date() -> NaiveDate {
    NaiveDate::from_ymd_opt(2024, 11, 1).unwrap_or_default()
}

/// `NSE_EXCHANGE_ID`.
pub(crate) const NSE_EXCHANGE_ID: i16 = 1;

/// `current_data_version`: the highest version ever published, `0` before the first run.
pub(crate) async fn current_data_version(state: &AppState) -> Result<i64, Problem> {
    let v: Option<i64> = sqlx::query_scalar(
        "SELECT max(data_version) FROM pipeline_run WHERE data_version IS NOT NULL",
    )
    .fetch_one(state.db.pool())
    .await
    .map_err(db)?;
    Ok(v.unwrap_or(0))
}

/// `latest_published_date`: the newest trade date of a published run
/// ([`crate::routes::common::latest_published_date`]).
pub(crate) async fn latest_published_date(state: &AppState) -> Result<Option<NaiveDate>, Problem> {
    crate::routes::common::latest_published_date(&state.db).await
}

/// `resolve_as_of(session, requested).as_of`, with `AsOfOutOfRange` and `NoPublishedData`
/// already rendered as `app.py` renders them (422 `no-trading-day`, 503 `pipeline-degraded`).
pub(crate) async fn resolve_as_of(
    state: &AppState,
    requested: Option<NaiveDate>,
) -> Result<NaiveDate, Problem> {
    let Some(latest) = latest_published_date(state).await? else {
        return Err(pipeline_degraded(
            "no pipeline_run has been published, so factor_daily has no trustworthy as-of date",
        ));
    };
    let Some(requested) = requested else {
        return Ok(latest);
    };
    let start = data_start_date();
    if requested > latest || requested < start {
        return Err(no_trading_day(requested, start, latest));
    }
    let snapped: Option<NaiveDate> = sqlx::query_scalar(
        "SELECT max(date) FROM trading_day WHERE exchange_id = $1 \
         AND is_trading_day IS true AND date <= $2",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(requested)
    .fetch_one(state.db.pool())
    .await
    .map_err(db)?;
    match snapped {
        Some(d) if d >= start => Ok(d),
        _ => Err(no_trading_day(requested, start, latest)),
    }
}

// ---------------------------------------------------------------------------------------------
// The canonical encoder (`canonical_json(model.model_dump(mode="python", by_alias=True))`)
// ---------------------------------------------------------------------------------------------

/// A `Decimal` cell, as `canonical_json` writes it (unquoted, its own digits).
pub(crate) fn p_dec(d: Decimal) -> Payload {
    PyDecimal::parse(&d.to_string())
        .map_or_else(|| Payload::Opaque("Decimal".to_owned()), Payload::Decimal)
}

/// `Decimal(str(x))` for a float that came out of Postgres as `double precision`.
pub(crate) fn p_dec_from_float(x: f64) -> Payload {
    if !x.is_finite() {
        return Payload::Opaque("Decimal".to_owned());
    }
    PyDecimal::parse(&baskfy_core::py::float_repr(x))
        .map_or_else(|| Payload::Opaque("Decimal".to_owned()), Payload::Decimal)
}

/// A nullable `Decimal` cell.
pub(crate) fn p_opt_dec(d: Option<Decimal>) -> Payload {
    d.map_or(Payload::Null, p_dec)
}

/// A `str`.
pub(crate) fn p_str(s: &str) -> Payload {
    Payload::Str(s.to_owned())
}

/// A nullable `str`.
pub(crate) fn p_opt_str(s: Option<&str>) -> Payload {
    s.map_or(Payload::Null, p_str)
}

/// A `date`.
pub(crate) fn p_date(d: NaiveDate) -> Payload {
    Payload::Date(d)
}

/// A nullable `date`.
pub(crate) fn p_opt_date(d: Option<NaiveDate>) -> Payload {
    d.map_or(Payload::Null, Payload::Date)
}

/// A nullable `int`.
pub(crate) fn p_opt_int(i: Option<i64>) -> Payload {
    i.map_or(Payload::Null, Payload::Int)
}

/// A mapping in the given (model field) order.
pub(crate) fn p_map(entries: Vec<(&str, Payload)>) -> Payload {
    Payload::Map(
        entries
            .into_iter()
            .map(|(k, v)| (k.to_owned(), v))
            .collect(),
    )
}

/// The routers' `_json(model, as_of=, data_version=)`: the canonical body, `application/json`,
/// and `snapshot_headers` (which the HTTP cache reads for its ETag).
pub(crate) fn canonical_response(
    payload: &Payload,
    as_of: Option<NaiveDate>,
    data_version: Option<i64>,
) -> Result<Response, Problem> {
    let body = baskfy_core::screener::canonical_json(payload)
        .map_err(|e| unexpected(&format!("canonical_json: {e}")))?;
    let mut response = body.into_response();
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static(crate::wire::JSON),
    );
    for (name, value) in crate::http_cache::snapshot_headers(as_of, data_version) {
        if let (Ok(n), Ok(v)) = (
            HeaderName::from_bytes(name.as_bytes()),
            HeaderValue::from_str(&value),
        ) {
            headers.insert(n, v);
        }
    }
    Ok(response)
}

// ---------------------------------------------------------------------------------------------
// Query parameters: pydantic-core's lax parsing, its error words
// ---------------------------------------------------------------------------------------------

/// `int_parsing`.
const INT_PARSING: &str = "Input should be a valid integer, unable to parse string as an integer";

/// An integer pydantic accepted: exact while it fits `i64`, otherwise only its sign is kept
/// (every bound these routes declare is far inside `i64`, so the sign decides the comparison).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum PyInt {
    /// Fits `i64`.
    Small(i64),
    /// Above `i64::MAX`.
    Huge,
    /// Below `i64::MIN`.
    HugeNegative,
}

/// pydantic-core's `str` → `int` (lax): [`crate::routes::common::lax_int`] in this lane's shape
/// (R5.10's collapse).
pub(crate) fn parse_py_int(raw: &str) -> Option<PyInt> {
    let p = crate::routes::common::lax_int(raw)?;
    Some(match p.as_i64() {
        Some(v) => PyInt::Small(v),
        None if p.negative => PyInt::HugeNegative,
        None => PyInt::Huge,
    })
}

/// pydantic-core's `str` → `bool` (lax).
pub(crate) fn parse_py_bool(raw: &str) -> Option<bool> {
    crate::routes::common::lax_bool(raw)
}

/// The outcome of pydantic's `str` → `Decimal` (lax).
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum PyDec {
    /// A finite value: `format(d, "f")`, and its sign and zero-ness.
    Finite {
        /// `format(d, "f")`.
        text: String,
        /// `str(d)`: the exact value in scientific form.
        sci: String,
        /// The sign.
        negative: bool,
        /// Whether it is a zero.
        zero: bool,
    },
    /// A finite value whose exponent has five or more digits: Python holds it exactly and its
    /// fixed-point spelling could be gigabytes, so only `str(d)` (scientific form, exact) is kept
    /// (DECISIONS-RS R5.12).
    Extreme {
        /// `str(d)`: the exact value in scientific form.
        sci: String,
        /// The sign.
        negative: bool,
        /// Whether it is a zero.
        zero: bool,
    },
    /// `nan` / `inf`: `finite_number`.
    NonFinite,
}

impl PyDec {
    /// The value as Python's default-context decimal, when it can be held.
    pub(crate) fn wide(&self) -> Option<baskfy_core::decimal::WideDecimal> {
        match self {
            Self::Finite { sci, .. } | Self::Extreme { sci, .. } => sci.parse().ok(),
            Self::NonFinite => None,
        }
    }
}

/// pydantic-core's `str` → `Decimal`: Python's `Decimal(str)` (whitespace stripped, every `_`
/// dropped, Unicode digits read as ASCII).
pub(crate) fn parse_py_decimal(raw: &str) -> Option<PyDec> {
    let parsed = PyDecimal::parse(raw)?;
    let sci = match &parsed {
        PyDecimal::NonFinite(_) => return Some(PyDec::NonFinite),
        PyDecimal::Finite(d) => d.py_str(),
    };
    let cleaned: String = py_strip(raw).chars().filter(|c| *c != '_').collect();
    let (mantissa, exponent) = match cleaned.find(['e', 'E']) {
        Some(i) => (&cleaned[..i], &cleaned[i + 1..]),
        None => (cleaned.as_str(), ""),
    };
    let negative = mantissa.starts_with('-');
    let zero = PyDecimal::parse(mantissa).is_some_and(|m| parsed_is_zero(&m));
    if exponent.trim_start_matches(['+', '-']).chars().count() > 4 {
        return Some(PyDec::Extreme {
            sci,
            negative,
            zero,
        });
    }
    let text = parsed.format_f();
    let zero = text
        .trim_start_matches('-')
        .bytes()
        .all(|b| b == b'0' || b == b'.');
    Some(PyDec::Finite {
        negative: text.starts_with('-'),
        zero,
        text,
        sci,
    })
}

fn parsed_is_zero(parsed: &PyDecimal) -> bool {
    match parsed {
        PyDecimal::Finite(d) => {
            let t = d.format_f();
            t.trim_start_matches('-')
                .bytes()
                .all(|b| b == b'0' || b == b'.')
        }
        PyDecimal::NonFinite(_) => false,
    }
}

/// Pydantic's `"at least 1 character"` / `"at least 2 characters"`.
fn characters(n: usize) -> String {
    if n == 1 {
        "1 character".to_owned()
    } else {
        format!("{n} characters")
    }
}

/// One request's query parameters and FastAPI's error accumulator together.
pub(crate) struct Params {
    /// The parsed query string.
    pub q: QueryParams,
    /// The accumulated errors.
    pub v: Validation,
}

impl Params {
    /// `request.query_params`, no errors yet.
    pub(crate) fn new(raw: Option<&str>) -> Self {
        Self {
            q: QueryParams::parse(raw),
            v: Validation::new(),
        }
    }

    /// A raw value, or `None` when absent.
    pub(crate) fn raw(&self, name: &str) -> Option<String> {
        self.q.get(name).map(str::to_owned)
    }

    /// An `int` with optional `ge`/`le`, defaulting when absent; `None` after an error.
    pub(crate) fn int(
        &mut self,
        name: &str,
        default: Option<i64>,
        ge: Option<i64>,
        le: Option<i64>,
    ) -> Option<Option<i64>> {
        let Some(raw) = self.raw(name) else {
            return Some(default);
        };
        let Some(value) = parse_py_int(&raw) else {
            self.v.error(name, INT_PARSING, "int_parsing");
            return None;
        };
        if let Some(ge) = ge {
            let below = match value {
                PyInt::Small(x) => x < ge,
                PyInt::HugeNegative => true,
                PyInt::Huge => false,
            };
            if below {
                self.v.error(
                    name,
                    &format!("Input should be greater than or equal to {ge}"),
                    "greater_than_equal",
                );
                return None;
            }
        }
        if let Some(le) = le {
            let above = match value {
                PyInt::Small(x) => x > le,
                PyInt::Huge => true,
                PyInt::HugeNegative => false,
            };
            if above {
                self.v.error(
                    name,
                    &format!("Input should be less than or equal to {le}"),
                    "less_than_equal",
                );
                return None;
            }
        }
        Some(Some(match value {
            PyInt::Small(x) => x,
            PyInt::Huge => i64::MAX,
            PyInt::HugeNegative => i64::MIN,
        }))
    }

    /// A constrained `str` (`min_length`, `max_length` in characters); absent is `None`.
    pub(crate) fn string(
        &mut self,
        name: &str,
        min_length: Option<usize>,
        max_length: Option<usize>,
    ) -> Result<Option<String>, ()> {
        let Some(raw) = self.raw(name) else {
            return Ok(None);
        };
        let n = raw.chars().count();
        if let Some(min) = min_length
            && n < min
        {
            self.v.error(
                name,
                &format!("String should have at least {}", characters(min)),
                "string_too_short",
            );
            return Err(());
        }
        if let Some(max) = max_length
            && n > max
        {
            self.v.error(
                name,
                &format!("String should have at most {}", characters(max)),
                "string_too_long",
            );
            return Err(());
        }
        Ok(Some(raw))
    }

    /// A required, constrained `str`.
    pub(crate) fn required_string(
        &mut self,
        name: &str,
        min_length: Option<usize>,
        max_length: Option<usize>,
    ) -> Option<String> {
        if self.q.get(name).is_none() {
            self.v.error(name, "Field required", "missing");
            return None;
        }
        self.string(name, min_length, max_length).ok().flatten()
    }

    /// A `str` matching `pattern` (an anchored alternation, `^(A|B)$`), checked as pydantic's
    /// Rust regex does: the whole value is one of `choices`.
    pub(crate) fn pattern(
        &mut self,
        name: &str,
        pattern: &str,
        choices: &[&str],
    ) -> Result<Option<String>, ()> {
        let Some(raw) = self.raw(name) else {
            return Ok(None);
        };
        if choices.contains(&raw.as_str()) {
            Ok(Some(raw))
        } else {
            self.v.error(
                name,
                &format!("String should match pattern '{pattern}'"),
                "string_pattern_mismatch",
            );
            Err(())
        }
    }

    /// A `bool` with a default.
    pub(crate) fn boolean(&mut self, name: &str, default: bool) -> Option<bool> {
        let Some(raw) = self.raw(name) else {
            return Some(default);
        };
        let parsed = parse_py_bool(&raw);
        if parsed.is_none() {
            self.v.error(
                name,
                "Input should be a valid boolean, unable to interpret input",
                "bool_parsing",
            );
        }
        parsed
    }

    /// A `Literal[...]` with a default.
    pub(crate) fn literal(
        &mut self,
        name: &str,
        choices: &[&str],
        default: &str,
    ) -> Option<String> {
        let Some(raw) = self.raw(name) else {
            return Some(default.to_owned());
        };
        if choices.contains(&raw.as_str()) {
            return Some(raw);
        }
        let quoted: Vec<String> = choices.iter().map(|c| format!("'{c}'")).collect();
        let message = match quoted.split_last() {
            Some((last, rest)) if !rest.is_empty() => {
                format!("Input should be {} or {last}", rest.join(", "))
            }
            Some((last, _)) => format!("Input should be {last}"),
            None => "Input should be".to_owned(),
        };
        self.v.error(name, &message, "literal_error");
        None
    }

    /// A `Decimal` with an optional exclusive lower bound of zero (`gt=0`); absent is `Ok(None)`.
    pub(crate) fn decimal(&mut self, name: &str, gt_zero: bool) -> Result<Option<PyDec>, ()> {
        let Some(raw) = self.raw(name) else {
            return Ok(None);
        };
        let Some(value) = parse_py_decimal(&raw) else {
            self.v
                .error(name, "Input should be a valid decimal", "decimal_parsing");
            return Err(());
        };
        if value == PyDec::NonFinite {
            self.v
                .error(name, "Input should be a finite number", "finite_number");
            return Err(());
        }
        if gt_zero {
            let positive = match &value {
                PyDec::Finite { negative, zero, .. } | PyDec::Extreme { negative, zero, .. } => {
                    !*negative && !*zero
                }
                PyDec::NonFinite => false,
            };
            if !positive {
                self.v
                    .error(name, "Input should be greater than 0", "greater_than");
                return Err(());
            }
        }
        Ok(Some(value))
    }

    /// A `datetime.date | None`; absent is `None`.
    pub(crate) fn date(&mut self, name: &str) -> Result<Option<NaiveDate>, ()> {
        let Some(raw) = self.raw(name) else {
            return Ok(None);
        };
        self.v.date(name, Some(&raw)).map(Some).ok_or(())
    }

    /// The 400 problem when anything failed.
    pub(crate) fn finish(self) -> Result<(), Problem> {
        self.v.finish()
    }
}

/// A path parameter's `max_length` (FastAPI validates path parameters before the query).
pub(crate) fn path_max_length(v: &mut Validation, name: &str, value: &str, max: usize) -> bool {
    if value.chars().count() > max {
        v.error(
            name,
            &format!("String should have at most {}", characters(max)),
            "string_too_long",
        );
        return false;
    }
    true
}

// ---------------------------------------------------------------------------------------------
// Clocks and tenants
// ---------------------------------------------------------------------------------------------

pub(crate) use crate::routes::common::{now_ist, today_ist};

/// `datetime.fromtimestamp(seconds, tz=UTC)` for an epoch-seconds float: CPython splits with
/// `modf` and rounds the fraction's microseconds half-even (`_PyTime_DoubleToDenominator`).
pub(crate) fn utc_from_seconds(seconds: f64) -> Option<DateTime<Utc>> {
    if !seconds.is_finite() {
        return None;
    }
    let mut int_part = seconds.trunc();
    let mut frac = ((seconds - int_part) * 1e6).round_ties_even();
    if frac >= 1e6 {
        frac -= 1e6;
        int_part += 1.0;
    } else if frac < 0.0 {
        frac += 1e6;
        int_part -= 1.0;
    }
    if int_part.abs() > 1e15 {
        return None;
    }
    #[allow(clippy::cast_possible_truncation)]
    let (secs, micros) = (int_part as i64, frac as u32);
    DateTime::from_timestamp(secs, micros * 1000)
}

/// `datetime.isoformat()` of an aware UTC datetime: `+00:00`, microseconds only when non-zero.
pub(crate) fn py_isoformat_utc(t: DateTime<Utc>) -> String {
    let z = crate::wire::py_datetime(t);
    format!("{}+00:00", z.trim_end_matches('Z'))
}

/// CPython 3.12's `sum(floats)` (start `0`): the first float, then Neumaier's compensated sum.
pub(crate) fn py_sum(values: &[f64]) -> f64 {
    let Some((first, rest)) = values.split_first() else {
        return 0.0;
    };
    let mut f_result = 0.0 + first;
    let mut c = 0.0_f64;
    for &x in rest {
        let t = f_result + x;
        if f_result.abs() >= x.abs() {
            c += (f_result - t) + x;
        } else {
            c += (x - t) + f_result;
        }
        f_result = t;
    }
    if c != 0.0 && c.is_finite() {
        f_result += c;
    }
    f_result
}

/// `curated_tenant.scoped_sole_user_id(session, principal_user_id, surface=)`, as
/// [`crate::routes::common::scoped_sole_user_id`].
pub(crate) async fn scoped_sole_user_id(
    state: &AppState,
    principal_user_id: Option<i64>,
    surface: Option<&str>,
) -> Result<i64, Problem> {
    crate::routes::common::scoped_sole_user_id(&state.db, principal_user_id, surface).await
}

// ---------------------------------------------------------------------------------------------
// Python strings and the live-quote switch
// ---------------------------------------------------------------------------------------------

use crate::routes::common::py_space;

/// `str.strip()`.
pub(crate) fn py_strip(s: &str) -> &str {
    s.trim_matches(py_space)
}

/// `str.upper()`.
pub(crate) fn py_upper(s: &str) -> String {
    s.to_uppercase()
}

/// `live_prices.quotes_permitted()`; a token store that cannot be read is Python's 500.
pub(crate) fn quotes_permitted() -> Result<bool, Problem> {
    crate::live_prices::quotes_permitted().map_err(|e| {
        crate::logging::error(
            "baskfy_api.app",
            "unhandled error",
            &[("error", e.python_type())],
        );
        crate::problems::internal_error()
    })
}

// ---------------------------------------------------------------------------------------------
// Python numerics
// ---------------------------------------------------------------------------------------------

/// Python's `round(x, ndigits)` for a float: the double nearest the correctly rounded decimal.
pub(crate) fn py_round(x: f64, ndigits: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    format!("{x:.ndigits$}").parse::<f64>().unwrap_or(x)
}

/// `float(Decimal)`: the double nearest the decimal.
pub(crate) fn dec_to_f64(d: Decimal) -> f64 {
    d.to_string().parse::<f64>().unwrap_or(f64::NAN)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn ints_parse_as_pydantic_does() {
        // Each checked against pydantic 2 (TypeAdapter(int).validate_python(str)).
        for (raw, want) in [
            ("5", Some(5)),
            ("+5", Some(5)),
            ("-5", Some(-5)),
            (" 5 ", Some(5)),
            ("5_0", Some(50)),
            ("05", Some(5)),
            ("5.0", Some(5)),
            ("5.00", Some(5)),
            ("5\t", Some(5)),
            ("\n5", Some(5)),
            ("-0", Some(0)),
            ("0_0", Some(0)),
            ("1_000.0", Some(1000)),
            ("+5.0", Some(5)),
            (" -5.0", Some(-5)),
        ] {
            assert_eq!(parse_py_int(raw), want.map(PyInt::Small), "{raw:?}");
        }
        for raw in [
            "_5", "5_", "5__0", "0x5", "5.", ".5", "5.5", "1e1", "", "  ", "٥", "5.0_0", "inf",
            "nan", "5e0",
        ] {
            assert_eq!(parse_py_int(raw), None, "{raw:?}");
        }
        assert_eq!(parse_py_int("99999999999999999999"), Some(PyInt::Huge));
        assert_eq!(
            parse_py_int("-99999999999999999999"),
            Some(PyInt::HugeNegative)
        );
    }

    #[test]
    fn bools_parse_as_pydantic_does() {
        for t in ["true", "True", "TRUE", "1", "yes", "on", "t", "y"] {
            assert_eq!(parse_py_bool(t), Some(true), "{t}");
        }
        for f in ["false", "0", "no", "off", "f", "n"] {
            assert_eq!(parse_py_bool(f), Some(false), "{f}");
        }
        for bad in ["", "x", " true", "false ", "2"] {
            assert_eq!(parse_py_bool(bad), None, "{bad:?}");
        }
    }

    #[test]
    fn decimals_parse_as_pydantic_does() {
        let v = |s: &str| match parse_py_decimal(s) {
            Some(PyDec::Finite { text, .. }) => Some(text),
            _ => None,
        };
        // Each checked against pydantic 2 (TypeAdapter(Decimal)), printed with format(d, "f").
        assert_eq!(v("1.50").as_deref(), Some("1.50"));
        assert_eq!(v(" 2 ").as_deref(), Some("2"));
        assert_eq!(v("1_000").as_deref(), Some("1000"));
        assert_eq!(v("_5").as_deref(), Some("5"));
        assert_eq!(v("+3").as_deref(), Some("3"));
        assert_eq!(v(".5").as_deref(), Some("0.5"));
        assert_eq!(v("5.").as_deref(), Some("5"));
        assert_eq!(v("1E-2").as_deref(), Some("0.01"));
        assert_eq!(v("1e3").as_deref(), Some("1000"));
        for bad in ["abc", "", "0x1", "1.2.3"] {
            assert_eq!(parse_py_decimal(bad), None, "{bad:?}");
        }
        assert_eq!(parse_py_decimal("nan"), Some(PyDec::NonFinite));
        assert_eq!(parse_py_decimal("inf"), Some(PyDec::NonFinite));
        assert_eq!(
            parse_py_decimal("1e100000"),
            Some(PyDec::Extreme {
                sci: "1E+100000".to_owned(),
                negative: false,
                zero: false
            })
        );
        // `str(Decimal(...))`, exact, whatever the spelling.
        let sci = |s: &str| match parse_py_decimal(s) {
            Some(PyDec::Finite { sci, .. } | PyDec::Extreme { sci, .. }) => Some(sci),
            _ => None,
        };
        assert_eq!(sci("1_0.50").as_deref(), Some("10.50"));
        assert_eq!(sci("1.5e-16384").as_deref(), Some("1.5E-16384"));
        assert_eq!(sci("-0e-20000").as_deref(), Some("-0E-20000"));
        assert_eq!(sci("1e30").as_deref(), Some("1E+30"));
    }

    #[test]
    fn python_round_is_correctly_rounded() {
        assert_eq!(py_round(2.675, 2).to_bits(), 2.67_f64.to_bits());
        assert_eq!(py_round(0.125, 2).to_bits(), 0.12_f64.to_bits());
        assert_eq!(py_round(-1.005, 2).to_bits(), (-1.0_f64).to_bits());
    }
}
