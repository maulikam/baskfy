//! Path and query parameters as FastAPI + pydantic v2 validate them, for this lane's routes.
//!
//! pydantic's lax `int` from a string (checked against pydantic-core in the decile-blueprint
//! venv): surrounding whitespace is stripped (Unicode, as Rust's `trim`), then an optional sign,
//! ASCII digits with single `_` separators between digits, and an optional `.` followed by zeros
//! only (`"12.0"` is 12, `"12.5"` and `"12."` are refused). The value is unbounded in Python; it
//! is held here as an `i128`, saturating far beyond any constraint or `bigint` it meets.

use crate::query::{QueryParams, Validation};

/// The parsed value of a pydantic `int`: saturated `i128` (see the module docs).
pub(crate) type PyInt = i128;

/// pydantic's lax `str -> int`, or `None` for `int_parsing`: [`crate::routes::common::lax_int`],
/// saturated into `i128` (R5.10's collapse).
pub(crate) fn parse_py_int(raw: &str) -> Option<PyInt> {
    crate::routes::common::lax_int(raw).map(|p| p.saturated())
}

/// Python's `int(text)`: [`crate::routes::common::python_int`], saturated as [`parse_py_int`].
pub(crate) fn python_int(text: &str) -> Option<PyInt> {
    crate::routes::common::python_int(text).map(|p| p.saturated())
}

/// Bounds on an `int` parameter (`Field(ge=, le=, gt=)`).
#[derive(Debug, Clone, Copy, Default)]
pub(crate) struct IntBounds {
    /// `ge`.
    pub(crate) ge: Option<i128>,
    /// `le`.
    pub(crate) le: Option<i128>,
    /// `gt`.
    pub(crate) gt: Option<i128>,
}

impl IntBounds {
    /// No bounds.
    pub(crate) const NONE: Self = Self {
        ge: None,
        le: None,
        gt: None,
    };
}

/// Validates one `int` value, recording pydantic's error under `field`.
pub(crate) fn int_value(
    v: &mut Validation,
    field: &str,
    raw: &str,
    bounds: IntBounds,
) -> Option<PyInt> {
    let Some(value) = parse_py_int(raw) else {
        v.error(
            field,
            "Input should be a valid integer, unable to parse string as an integer",
            "int_parsing",
        );
        return None;
    };
    if let Some(gt) = bounds.gt
        && value <= gt
    {
        v.error(
            field,
            &format!("Input should be greater than {gt}"),
            "greater_than",
        );
        return None;
    }
    if let Some(ge) = bounds.ge
        && value < ge
    {
        v.error(
            field,
            &format!("Input should be greater than or equal to {ge}"),
            "greater_than_equal",
        );
        return None;
    }
    if let Some(le) = bounds.le
        && value > le
    {
        v.error(
            field,
            &format!("Input should be less than or equal to {le}"),
            "less_than_equal",
        );
        return None;
    }
    Some(value)
}

/// An optional `int` query parameter with a default (`limit: int = 100`).
pub(crate) fn query_int(
    v: &mut Validation,
    q: &QueryParams,
    name: &str,
    default: i128,
    bounds: IntBounds,
) -> Option<PyInt> {
    match q.get(name) {
        None => Some(default),
        Some(raw) => int_value(v, name, raw, bounds),
    }
}

/// A `str` parameter's `min_length` / `max_length` (counted in characters, as Python counts).
pub(crate) fn str_value<'a>(
    v: &mut Validation,
    field: &str,
    raw: &'a str,
    min_length: Option<usize>,
    max_length: Option<usize>,
) -> Option<&'a str> {
    let n = raw.chars().count();
    if let Some(min) = min_length
        && n < min
    {
        let unit = if min == 1 { "character" } else { "characters" };
        v.error(
            field,
            &format!("String should have at least {min} {unit}"),
            "string_too_short",
        );
        return None;
    }
    if let Some(max) = max_length
        && n > max
    {
        let unit = if max == 1 { "character" } else { "characters" };
        v.error(
            field,
            &format!("String should have at most {max} {unit}"),
            "string_too_long",
        );
        return None;
    }
    Some(raw)
}

/// A value that must fit Postgres's `bigint` when it reaches a query. asyncpg refuses a wider
/// `int` with a `DataError`, which nothing in the API handles: the 500 catch-all.
///
/// # Errors
/// `internal_error()` for a value outside `i64`.
pub(crate) fn bigint(value: PyInt) -> Result<i64, crate::problems::Problem> {
    i64::try_from(value).map_err(|_| {
        crate::logging::error(
            "baskfy_api.app",
            "unhandled error",
            &[("error", "value out of int64 range")],
        );
        crate::problems::internal_error()
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pydantic_lax_int() {
        // Each checked against pydantic-core in the decile-blueprint venv.
        for (raw, want) in [
            ("12", Some(12)),
            (" 12 ", Some(12)),
            ("+12", Some(12)),
            ("-3", Some(-3)),
            ("1_000", Some(1000)),
            ("12.0", Some(12)),
            ("12.000", Some(12)),
            ("1_0.0", Some(10)),
            ("  -0012_3  ", Some(-123)),
            ("\t5\n", Some(5)),
            ("\u{a0}5", Some(5)),
            ("00012", Some(12)),
            ("+0", Some(0)),
            ("12.5", None),
            ("1e3", None),
            ("abc", None),
            ("", None),
            ("  ", None),
            ("0x10", None),
            ("１２", None),
            ("1_", None),
            ("_1", None),
            ("1__0", None),
            ("12.", None),
            (".0", None),
            ("+-1", None),
            ("12.0_0", None),
            ("12.0.0", None),
            ("1 2", None),
            ("12\u{0}", None),
        ] {
            assert_eq!(parse_py_int(raw), want, "{raw:?}");
        }
        assert_eq!(
            parse_py_int("99999999999999999999"),
            Some(99_999_999_999_999_999_999)
        );
    }

    #[test]
    fn python_int_is_pythons_int() {
        assert_eq!(python_int(" 7 "), Some(7));
        assert_eq!(python_int("1_0"), Some(10));
        assert_eq!(python_int("+3"), Some(3));
        assert_eq!(python_int("x"), None);
        assert_eq!(python_int("1__0"), None);
        assert_eq!(python_int("2.0"), None);
    }

    #[test]
    fn bounds_speak_pydantics_words() {
        let mut v = Validation::new();
        assert!(
            int_value(
                &mut v,
                "limit",
                "0",
                IntBounds {
                    ge: Some(1),
                    le: Some(500),
                    gt: None
                }
            )
            .is_none()
        );
        assert!(
            int_value(
                &mut v,
                "limit",
                "501",
                IntBounds {
                    ge: Some(1),
                    le: Some(500),
                    gt: None
                }
            )
            .is_none()
        );
        assert!(str_value(&mut v, "symbol", "abcde", None, Some(4)).is_none());
        let Err(problem) = v.finish() else {
            unreachable!("three errors were recorded")
        };
        let body = problem
            .body(Some("/x"))
            .to_python_json()
            .unwrap_or_default();
        assert!(body.contains(r#"{"field":"limit","message":"Input should be greater than or equal to 1","type":"greater_than_equal"}"#), "{body}");
        assert!(body.contains(r#"{"field":"limit","message":"Input should be less than or equal to 500","type":"less_than_equal"}"#), "{body}");
        assert!(body.contains(r#"{"field":"symbol","message":"String should have at most 4 characters","type":"string_too_long"}"#), "{body}");
    }
}
