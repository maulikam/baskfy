//! What the sleeves' backtest runners share: Python's argument reads, `isoformat()`, `str()` of a
//! `Decimal`, and the error text a failed run stores.

use baskfy_core::decimal::WideDecimal;
use baskfy_db::OrderedJson;
use chrono::{DateTime, NaiveDate, Timelike, Utc};
use rust_decimal::Decimal;

use crate::error::WorkerError;
use crate::tasks::args::fromisoformat_date;
use crate::tasks::{BoundArgs, TaskContext};

/// Python's truth of a JSON value (`if value:`): `None`, `False`, `0`, `0.0`, `""`, `[]` and
/// `{}` are false.
pub(super) fn truthy(value: &OrderedJson) -> bool {
    match value {
        OrderedJson::Null => false,
        OrderedJson::Bool(b) => *b,
        OrderedJson::Int(i) => *i != 0,
        OrderedJson::UInt(u) => *u != 0,
        OrderedJson::BigInt(_) => true,
        OrderedJson::Float(f) => *f != 0.0,
        OrderedJson::Str(s) => !s.is_empty(),
        OrderedJson::Array(a) => !a.is_empty(),
        OrderedJson::Object(o) => !o.is_empty(),
    }
}

/// `dt.date.fromisoformat(value) if value else None` for the task argument `name`.
///
/// # Errors
/// `TypeError("fromisoformat: argument must be str")` for a truthy non-string (CPython's
/// message), `ValueError` for a string that is not an ISO date.
pub(super) fn date_if_given(
    args: &BoundArgs,
    name: &str,
) -> Result<Option<NaiveDate>, WorkerError> {
    match args.value(name) {
        None => Ok(None),
        Some(value) if !truthy(value) => Ok(None),
        Some(OrderedJson::Str(text)) => fromisoformat_date(text).map(Some),
        Some(_) => Err(WorkerError::python(
            "TypeError",
            "fromisoformat: argument must be str",
        )),
    }
}

/// `Decimal(value) if value else None` for the task argument `name`: a string as `Decimal`
/// parses it (surrounding whitespace allowed), an integer exactly.
///
/// # Errors
/// `InvalidOperation("[<class 'decimal.ConversionSyntax'>]")` for a string `Decimal` refuses
/// (or one beyond 28 digits of scale), `TypeError` for another JSON type.
pub(super) fn decimal_if_given(
    args: &BoundArgs,
    name: &str,
) -> Result<Option<Decimal>, WorkerError> {
    let syntax = || WorkerError::python("InvalidOperation", "[<class 'decimal.ConversionSyntax'>]");
    match args.value(name) {
        None => Ok(None),
        Some(value) if !truthy(value) => Ok(None),
        Some(OrderedJson::Str(text)) => {
            let wide: WideDecimal = text.trim().parse().map_err(|_| syntax())?;
            wide.try_to_decimal().map(Some).map_err(|_| syntax())
        }
        Some(OrderedJson::Int(i)) => Ok(Some(Decimal::from(*i))),
        Some(other) => Err(WorkerError::python(
            "TypeError",
            format!(
                "conversion from {} to Decimal is not supported",
                crate::tasks::args::py_type_name(other)
            ),
        )),
    }
}

/// A count dict as Python built it: the keys of `order` first, in that order (a dict literal's
/// keys, or an enum's members), then any other key the core produced, sorted. The core keeps
/// these counts in a `BTreeMap`; Python's `dict` keeps insertion order, and the payload is
/// compared with key order.
pub(super) fn ordered_counts(
    map: &std::collections::BTreeMap<String, i64>,
    order: &[&str],
) -> OrderedJson {
    let mut pairs: Vec<(String, OrderedJson)> = order
        .iter()
        .filter_map(|k| map.get(*k).map(|v| ((*k).to_owned(), OrderedJson::Int(*v))))
        .collect();
    for (k, v) in map {
        if !order.contains(&k.as_str()) {
            pairs.push((k.clone(), OrderedJson::Int(*v)));
        }
    }
    OrderedJson::Object(pairs)
}

/// `datetime.isoformat()` of an aware UTC moment: `2026-10-30T07:45:00+00:00`, with
/// `.ffffff` only when the microseconds are not zero.
pub(super) fn iso_utc(moment: DateTime<Utc>) -> String {
    let micros = moment.nanosecond() / 1_000;
    if micros == 0 {
        moment.format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
    } else {
        format!("{}.{micros:06}+00:00", moment.format("%Y-%m-%dT%H:%M:%S"))
    }
}

/// `date.isoformat()`.
pub(super) fn iso_date(day: NaiveDate) -> String {
    day.format("%Y-%m-%d").to_string()
}

/// `str(d)` of a `Decimal`.
pub(super) fn dec_str(d: Decimal) -> String {
    WideDecimal::from(d).to_sci_string()
}

/// `str(d)` of a wide `Decimal`.
pub(super) fn wide_str(d: &WideDecimal) -> String {
    d.to_sci_string()
}

/// A JSON float (`repr(float)` when serialised).
pub(super) fn jf(x: f64) -> OrderedJson {
    OrderedJson::Float(x)
}

/// A JSON float or `null`.
pub(super) fn opt_jf(x: Option<f64>) -> OrderedJson {
    x.map_or(OrderedJson::Null, OrderedJson::Float)
}

/// `date.today()` under the worker's process clock. The containers and the harness run with
/// `TZ=UTC`, so the local date is the UTC date of the (injected) clock.
pub(super) fn today(ctx: &TaskContext) -> NaiveDate {
    ctx.now_utc().date_naive()
}

/// `f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"`. Rust has no Python traceback:
/// the third part names the Rust function the error left (the harness compares the column by
/// presence, as the nightly's `pipeline_run_step.error.traceback`; DECISIONS-RS R7.4).
pub(super) fn error_text(error: &WorkerError, separator: &str, origin: &str) -> String {
    format!(
        "{}: {}{separator}Traceback (Rust): raised in {origin}\n",
        error.python_type(),
        error
    )
}

/// The first non-finite float in `value`, in serialisation order, as Python's `json.dumps`
/// writes it (`NaN`, `Infinity`, `-Infinity`).
fn non_finite_token(value: &OrderedJson) -> Option<&'static str> {
    match value {
        OrderedJson::Float(f) if f.is_nan() => Some("NaN"),
        OrderedJson::Float(f) if f.is_infinite() => {
            Some(if *f > 0.0 { "Infinity" } else { "-Infinity" })
        }
        OrderedJson::Array(items) => items.iter().find_map(non_finite_token),
        OrderedJson::Object(pairs) => pairs.iter().find_map(|(_, v)| non_finite_token(v)),
        _ => None,
    }
}

/// The text a JSONB column is bound to: `json.dumps(value)`. Python's encoder writes a NaN or an
/// infinity as a bare `NaN`/`Infinity` token, which Postgres refuses (`invalid input syntax for
/// type json`) and SQLAlchemy raises as `DBAPIError`; the same class here, Postgres's own words
/// as the message (Python's also carries the SQL and its parameters — the harness compares the
/// class; DECISIONS-RS, lane `sleeve_backtests`).
pub(super) fn jsonb_text(value: &OrderedJson) -> Result<String, WorkerError> {
    if let Some(token) = non_finite_token(value) {
        return Err(WorkerError::python(
            "DBAPIError",
            format!("invalid input syntax for type json\nDETAIL:  Token \"{token}\" is invalid."),
        ));
    }
    crate::pyjson::dumps(value).map_err(|e| WorkerError::python("ValueError", e))
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn isoformat_drops_zero_microseconds_as_python_does() {
        let at = Utc.with_ymd_and_hms(2026, 10, 30, 7, 45, 0).single();
        assert_eq!(
            at.map(iso_utc).as_deref(),
            Some("2026-10-30T07:45:00+00:00")
        );
        let with = at.and_then(|a| a.with_nanosecond(5_000));
        assert_eq!(
            with.map(iso_utc).as_deref(),
            Some("2026-10-30T07:45:00.000005+00:00")
        );
    }

    #[test]
    fn truth_is_pythons() {
        assert!(!truthy(&OrderedJson::Str(String::new())));
        assert!(!truthy(&OrderedJson::Int(0)));
        assert!(!truthy(&OrderedJson::Null));
        assert!(truthy(&OrderedJson::Str("2026-01-01".into())));
        assert!(truthy(&OrderedJson::Int(5)));
    }

    #[test]
    fn a_truthy_non_string_date_is_cpythons_type_error() {
        let args = BoundArgs::from_pairs(vec![("start", OrderedJson::Int(5))]);
        let err = date_if_given(&args, "start").err();
        assert_eq!(
            err.map(|e| (e.python_type(), e.to_string())),
            Some((
                "TypeError".to_owned(),
                "fromisoformat: argument must be str".to_owned()
            ))
        );
        let empty = BoundArgs::from_pairs(vec![("start", OrderedJson::Str(String::new()))]);
        assert_eq!(date_if_given(&empty, "start").ok(), Some(None));
    }

    #[test]
    fn a_nan_reaches_the_database_as_python_sends_it() {
        let v = OrderedJson::Object(vec![(
            "x".into(),
            OrderedJson::Array(vec![OrderedJson::Float(1.0), OrderedJson::Float(f64::NAN)]),
        )]);
        let err = jsonb_text(&v).err();
        assert_eq!(err.map(|e| e.python_type()).as_deref(), Some("DBAPIError"));
        assert_eq!(
            jsonb_text(&OrderedJson::Float(0.5)).ok().as_deref(),
            Some("0.5")
        );
    }

    #[test]
    fn decimal_str_keeps_the_scale() {
        assert_eq!(dec_str(Decimal::new(300, 1)), "30.0");
        assert_eq!(dec_str(Decimal::new(1_000_000, 0)), "1000000");
    }
}
