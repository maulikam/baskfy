//! A Celery message's `args` and `kwargs`, bound to a task's parameters as CPython binds a call.
//!
//! Celery calls the body as `task.run(*args, **kwargs)` with the message's JSON values — no
//! coercion, no validation. So the binding errors are CPython 3.12's own, in its order
//! (`initialize_locals`): an unexpected or repeated keyword first (in the message's keyword
//! order), then too many positionals, then the missing required ones — each a `TypeError` whose
//! message names the function (`nightly_pipeline() missing 1 required positional argument: 'x'`).
//! A bound task (`bind=True`) counts its `self` in the positional numbers, as Python does.
//!
//! The typed accessors ([`BoundArgs::opt_str`], [`BoundArgs::int`], ...) refuse a JSON value of
//! the wrong type with a `TypeError`. Python would let the value through and fail wherever the
//! body first uses it, with a message particular to that use; a lane whose task is graded on a
//! wrongly-typed argument reproduces that message itself through [`BoundArgs::value`]
//! (DECISIONS-RS R8.1).

use baskfy_db::OrderedJson;
use chrono::NaiveDate;

use super::{PyDefault, TaskSpec};
use crate::error::WorkerError;

/// The arguments of one call, every parameter resolved (given or defaulted), in `run`'s order.
#[derive(Debug, Clone, PartialEq)]
pub struct BoundArgs {
    values: Vec<(&'static str, OrderedJson)>,
}

/// `type(value).__name__` for a JSON value as Python decodes it.
#[must_use]
pub fn py_type_name(value: &OrderedJson) -> &'static str {
    match value {
        OrderedJson::Null => "NoneType",
        OrderedJson::Bool(_) => "bool",
        OrderedJson::Int(_) | OrderedJson::UInt(_) | OrderedJson::BigInt(_) => "int",
        OrderedJson::Float(_) => "float",
        OrderedJson::Str(_) => "str",
        OrderedJson::Array(_) => "list",
        OrderedJson::Object(_) => "dict",
    }
}

fn type_error(message: String) -> WorkerError {
    WorkerError::python("TypeError", message)
}

fn default_value(default: PyDefault) -> Option<OrderedJson> {
    match default {
        PyDefault::Required => None,
        PyDefault::None => Some(OrderedJson::Null),
        PyDefault::Str(s) => Some(OrderedJson::Str(s.to_owned())),
        PyDefault::Int(i) => Some(OrderedJson::Int(i)),
        PyDefault::Bool(b) => Some(OrderedJson::Bool(b)),
    }
}

/// CPython's `'a'`, `'a' and 'b'`, `'a', 'b', and 'c'`.
fn name_list(names: &[&str]) -> String {
    let quoted: Vec<String> = names.iter().map(|n| format!("'{n}'")).collect();
    match quoted.as_slice() {
        [] => String::new(),
        [one] => one.clone(),
        [a, b] => format!("{a} and {b}"),
        [head @ .., last] => format!("{}, and {last}", head.join(", ")),
    }
}

/// The function's name as CPython prints it in a binding error (a bound task's `run` is a method
/// of its task class, and CPython still prints the bare function name).
fn function_name(spec: &TaskSpec) -> &'static str {
    spec.python.rsplit('.').next().unwrap_or(spec.python)
}

/// Bind a message's arguments to `spec`'s parameters.
///
/// # Errors
/// Python's `TypeError` for a call that does not bind.
pub fn bind(
    spec: &TaskSpec,
    positional: Vec<OrderedJson>,
    keywords: Vec<(String, OrderedJson)>,
) -> Result<BoundArgs, WorkerError> {
    let func = function_name(spec);
    let offset = usize::from(spec.bound);
    let params = spec.params;
    let mut slots: Vec<Option<OrderedJson>> = vec![None; params.len()];
    let given_positional = positional.len();
    for (slot, value) in slots.iter_mut().zip(positional) {
        *slot = Some(value);
    }
    for (key, value) in keywords {
        let Some(index) = params.iter().position(|p| p.name == key) else {
            return Err(type_error(format!(
                "{func}() got an unexpected keyword argument '{key}'"
            )));
        };
        if slots.get(index).is_some_and(Option::is_some) {
            return Err(type_error(format!(
                "{func}() got multiple values for argument '{key}'"
            )));
        }
        if let Some(slot) = slots.get_mut(index) {
            *slot = Some(value);
        }
    }
    if given_positional > params.len() {
        let total = params.len() + offset;
        let required = params
            .iter()
            .filter(|p| p.default == PyDefault::Required)
            .count()
            + offset;
        let takes = if required == total {
            format!(
                "{total} positional argument{}",
                if total == 1 { "" } else { "s" }
            )
        } else {
            format!("from {required} to {total} positional arguments")
        };
        let given = given_positional + offset;
        return Err(type_error(format!(
            "{func}() takes {takes} but {given} {} given",
            if given == 1 { "was" } else { "were" }
        )));
    }
    let mut missing = Vec::new();
    let mut values = Vec::with_capacity(params.len());
    for (param, slot) in params.iter().zip(slots) {
        match slot.or_else(|| default_value(param.default)) {
            Some(v) => values.push((param.name, v)),
            None => missing.push(param.name),
        }
    }
    if !missing.is_empty() {
        let n = missing.len();
        return Err(type_error(format!(
            "{func}() missing {n} required positional argument{}: {}",
            if n == 1 { "" } else { "s" },
            name_list(&missing)
        )));
    }
    Ok(BoundArgs { values })
}

impl BoundArgs {
    /// Arguments built directly (a task calling another task's body, as Python's
    /// `session_catch_up` calls `swing_eod_task(day)`).
    #[must_use]
    pub fn from_pairs(values: Vec<(&'static str, OrderedJson)>) -> Self {
        Self { values }
    }

    /// Every parameter and its value, in order.
    #[must_use]
    pub fn pairs(&self) -> &[(&'static str, OrderedJson)] {
        &self.values
    }

    /// The raw value of `name` (`None` for a name the task does not have).
    #[must_use]
    pub fn value(&self, name: &str) -> Option<&OrderedJson> {
        self.values.iter().find(|(k, _)| *k == name).map(|(_, v)| v)
    }

    fn require(&self, name: &str) -> Result<&OrderedJson, WorkerError> {
        self.value(name).ok_or_else(|| {
            WorkerError::python(
                "KeyError",
                format!("'{name}' is not a parameter of this task"),
            )
        })
    }

    fn wrong(name: &str, wanted: &str, value: &OrderedJson) -> WorkerError {
        type_error(format!(
            "argument '{name}' must be {wanted}, not {}",
            py_type_name(value)
        ))
    }

    /// A `str | None` parameter.
    ///
    /// # Errors
    /// `TypeError` for any other JSON type.
    pub fn opt_str(&self, name: &str) -> Result<Option<String>, WorkerError> {
        match self.require(name)? {
            OrderedJson::Null => Ok(None),
            OrderedJson::Str(s) => Ok(Some(s.clone())),
            other => Err(Self::wrong(name, "str or None", other)),
        }
    }

    /// A `str` parameter.
    ///
    /// # Errors
    /// `TypeError` for any other JSON type.
    pub fn str(&self, name: &str) -> Result<String, WorkerError> {
        match self.require(name)? {
            OrderedJson::Str(s) => Ok(s.clone()),
            other => Err(Self::wrong(name, "str", other)),
        }
    }

    /// An `int` parameter (a JSON `true`/`false` is Python's `1`/`0`, as `bool` is an `int`).
    ///
    /// # Errors
    /// `TypeError` for any other JSON type, or an integer beyond `i64`.
    pub fn int(&self, name: &str) -> Result<i64, WorkerError> {
        match self.require(name)? {
            OrderedJson::Int(i) => Ok(*i),
            OrderedJson::Bool(b) => Ok(i64::from(*b)),
            other => Err(Self::wrong(name, "int", other)),
        }
    }

    /// A `bool` parameter (Python reads any value's truth; only JSON booleans are accepted here).
    ///
    /// # Errors
    /// `TypeError` for any other JSON type.
    pub fn bool(&self, name: &str) -> Result<bool, WorkerError> {
        match self.require(name)? {
            OrderedJson::Bool(b) => Ok(*b),
            other => Err(Self::wrong(name, "bool", other)),
        }
    }

    /// A `list[str] | None` parameter.
    ///
    /// # Errors
    /// `TypeError` for any other JSON type or a non-string item.
    pub fn opt_str_list(&self, name: &str) -> Result<Option<Vec<String>>, WorkerError> {
        match self.require(name)? {
            OrderedJson::Null => Ok(None),
            OrderedJson::Array(items) => items
                .iter()
                .map(|item| match item {
                    OrderedJson::Str(s) => Ok(s.clone()),
                    other => Err(Self::wrong(name, "a list of str", other)),
                })
                .collect::<Result<Vec<_>, _>>()
                .map(Some),
            other => Err(Self::wrong(name, "list[str] or None", other)),
        }
    }

    /// A `str | None` date parameter read as Python's `dt.date.fromisoformat(value)` when given.
    ///
    /// # Errors
    /// As [`BoundArgs::opt_str`]; `ValueError` for a string that is not an ISO date.
    pub fn opt_date(&self, name: &str) -> Result<Option<NaiveDate>, WorkerError> {
        self.opt_str(name)?
            .map(|text| fromisoformat_date(&text))
            .transpose()
    }
}

/// `datetime.date.fromisoformat(text)` (Python 3.12) for the forms a task argument takes:
/// `YYYY-MM-DD` and `YYYYMMDD`. The ISO week forms (`2026-W44-4`) are refused with Python's
/// message rather than read (no caller sends one).
///
/// # Errors
/// `ValueError("Invalid isoformat string: '...'")`, Python's message.
pub fn fromisoformat_date(text: &str) -> Result<NaiveDate, WorkerError> {
    let invalid = || {
        WorkerError::python(
            "ValueError",
            format!(
                "Invalid isoformat string: {}",
                baskfy_providers::py::repr_str(text)
            ),
        )
    };
    let bytes = text.as_bytes();
    let digits = |s: &[u8]| s.iter().all(u8::is_ascii_digit);
    let (y, m, d) = match bytes.len() {
        10 if bytes.get(4) == Some(&b'-') && bytes.get(7) == Some(&b'-') => {
            (text.get(0..4), text.get(5..7), text.get(8..10))
        }
        8 if digits(bytes) => (text.get(0..4), text.get(4..6), text.get(6..8)),
        _ => return Err(invalid()),
    };
    let (Some(y), Some(m), Some(d)) = (y, m, d) else {
        return Err(invalid());
    };
    if !(digits(y.as_bytes()) && digits(m.as_bytes()) && digits(d.as_bytes())) {
        return Err(invalid());
    }
    let (Ok(year), Ok(month), Ok(day)) = (y.parse::<i32>(), m.parse::<u32>(), d.parse::<u32>())
    else {
        return Err(invalid());
    };
    if year < 1 {
        return Err(WorkerError::python(
            "ValueError",
            format!("year {year} is out of range"),
        ));
    }
    if !(1..=12).contains(&month) {
        return Err(WorkerError::python(
            "ValueError",
            "month must be in 1..12".to_owned(),
        ));
    }
    NaiveDate::from_ymd_opt(year, month, day).ok_or_else(|| {
        WorkerError::python("ValueError", "day is out of range for month".to_owned())
    })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::tasks::find;

    fn s(v: &str) -> OrderedJson {
        OrderedJson::Str(v.to_owned())
    }

    fn err(r: Result<BoundArgs, WorkerError>) -> String {
        let e = r.expect_err("should not bind");
        format!("{}: {e}", e.python_type())
    }

    #[test]
    fn binds_positional_keyword_and_defaults() {
        let spec = find("baskfy.pipeline.session_catch_up").unwrap();
        let b = bind(spec, vec![OrderedJson::Int(3)], vec![]).unwrap();
        assert_eq!(b.int("lookback_days").unwrap(), 3);
        assert_eq!(b.int("max_sessions").unwrap(), 2);
        let b = bind(
            spec,
            vec![],
            vec![("max_sessions".into(), OrderedJson::Int(1))],
        )
        .unwrap();
        assert_eq!(b.int("lookback_days").unwrap(), 7);
    }

    // The messages below are CPython 3.12's, checked against
    // `nightly_pipeline(...)`, `resync_task(...)` and `session_catch_up(...)`.
    #[test]
    fn binding_errors_are_cpythons() {
        let nightly = find("baskfy.pipeline.nightly").unwrap();
        assert_eq!(
            err(bind(nightly, vec![s("a"), s("b")], vec![])),
            "TypeError: nightly_pipeline() takes from 0 to 1 positional arguments but 2 were given"
        );
        assert_eq!(
            err(bind(nightly, vec![], vec![("day".into(), s("x"))])),
            "TypeError: nightly_pipeline() got an unexpected keyword argument 'day'"
        );
        assert_eq!(
            err(bind(
                nightly,
                vec![s("a")],
                vec![("trade_date".into(), s("x"))]
            )),
            "TypeError: nightly_pipeline() got multiple values for argument 'trade_date'"
        );
        let resync = find("baskfy.ops.resync").unwrap();
        assert_eq!(
            err(bind(resync, vec![], vec![])),
            "TypeError: resync_task() missing 2 required positional arguments: 'actor_user_id' and 'days'"
        );
        assert_eq!(
            err(bind(resync, vec![OrderedJson::Int(1)], vec![])),
            "TypeError: resync_task() missing 1 required positional argument: 'days'"
        );
        assert_eq!(
            err(bind(
                resync,
                vec![
                    OrderedJson::Int(1),
                    OrderedJson::Int(2),
                    OrderedJson::Int(3)
                ],
                vec![]
            )),
            "TypeError: resync_task() takes 2 positional arguments but 3 were given"
        );
        let purge = find("baskfy.accounts.purge").unwrap();
        assert_eq!(
            err(bind(purge, vec![OrderedJson::Int(1)], vec![])),
            "TypeError: purge_accounts_task() takes 0 positional arguments but 1 was given"
        );
    }

    #[test]
    fn a_bound_task_counts_self() {
        let scan = find("baskfy.swing.scan_now").unwrap();
        assert_eq!(
            err(bind(
                scan,
                vec![OrderedJson::Int(1), OrderedJson::Int(2)],
                vec![]
            )),
            "TypeError: swing_scan_now_task() takes 2 positional arguments but 3 were given"
        );
        assert_eq!(
            err(bind(scan, vec![], vec![])),
            "TypeError: swing_scan_now_task() missing 1 required positional argument: 'run_id'"
        );
    }

    #[test]
    fn fromisoformat_reads_the_two_forms() {
        let d = NaiveDate::from_ymd_opt(2026, 10, 29).unwrap();
        assert_eq!(fromisoformat_date("2026-10-29").unwrap(), d);
        assert_eq!(fromisoformat_date("20261029").unwrap(), d);
        let e = fromisoformat_date("29-10-2026").unwrap_err();
        assert_eq!(e.to_string(), "Invalid isoformat string: '29-10-2026'");
        let e = fromisoformat_date("2026-02-30").unwrap_err();
        assert_eq!(e.to_string(), "day is out of range for month");
    }

    #[test]
    fn three_names_take_an_oxford_comma() {
        assert_eq!(name_list(&["a", "b", "c"]), "'a', 'b', and 'c'");
    }
}
