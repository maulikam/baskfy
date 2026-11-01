//! Helpers shared by the accounts lane's routers (screens, curated_*, managers, watchlist, alerts,
//! api_keys, webhook_endpoints, admin, auth). Private to the lane: declared from `screens.rs`
//! with a `#[path]` so the shared wiring (`routes.rs`) stays untouched.
//!
//! What lives here is what several Python routers share: pydantic's lax `int`/`bool` query
//! parsing (FastAPI's validation of a declared parameter), the sole-tenant gate
//! (`baskfy_api.curated_tenant.scoped_sole_user_id`), the "docs/07's only 400" problem the
//! integrations routers reuse, and the database-error mapping.

pub mod csv_export;
#[cfg(test)]
mod http_tests;
pub mod investments;
pub mod trending;

use baskfy_db::OrderedJson;

use crate::problems::{Problem, ProblemType};
use crate::query::Validation;
use crate::state::AppState;

/// A `sqlx` failure as the 500 catch-all (Python's `SQLAlchemyError` has no handler).
pub fn db(e: sqlx::Error) -> Problem {
    crate::db_failure(&e.into())
}

/// A `baskfy_db` failure as the 500 catch-all.
pub fn dbe(e: &baskfy_db::DbError) -> Problem {
    crate::db_failure(e)
}

/// `Problem(ProblemType.INVALID_SCREEN_DEFINITION, detail, errors=[{"message": detail}])` —
/// the `_bad_request` of `routers/api_keys.py` and `routers/alerts.py`.
#[must_use]
pub fn invalid_with_message(detail: &str) -> Problem {
    Problem::new(ProblemType::InvalidScreenDefinition, detail).with(
        "errors",
        OrderedJson::Array(vec![OrderedJson::Object(vec![(
            "message".to_owned(),
            OrderedJson::Str(detail.to_owned()),
        )])]),
    )
}

/// The integer a pydantic-core lax `str -> int` parse yields, saturated to `i128` (Python's ints
/// are unbounded; anything beyond `i128` is far outside every bound a route declares). The rule
/// is [`crate::routes::common::lax_int`] (R5.10's collapse).
#[must_use]
pub fn pydantic_int(raw: &str) -> Option<i128> {
    crate::routes::common::lax_int(raw).map(|p| p.saturated())
}

/// Python's `int(text)`: [`crate::routes::common::python_int`], saturated to `i128`.
#[must_use]
pub fn python_int(raw: &str) -> Option<i128> {
    crate::routes::common::python_int(raw).map(|p| p.saturated())
}

const INT_PARSING: &str = "Input should be a valid integer, unable to parse string as an integer";

/// A declared `int` parameter with optional `ge`/`le` bounds, validated as FastAPI does: the
/// failure is recorded on `v` under `field` and `None` returned. `raw` `None` means "absent";
/// the caller supplies the default.
pub fn int_param(
    v: &mut Validation,
    field: &str,
    raw: Option<&str>,
    ge: Option<i64>,
    le: Option<i64>,
) -> Option<i128> {
    let raw = raw?;
    let Some(n) = pydantic_int(raw) else {
        v.error(field, INT_PARSING, "int_parsing");
        return None;
    };
    if let Some(ge) = ge
        && n < i128::from(ge)
    {
        v.error(
            field,
            &format!("Input should be greater than or equal to {ge}"),
            "greater_than_equal",
        );
        return None;
    }
    if let Some(le) = le
        && n > i128::from(le)
    {
        v.error(
            field,
            &format!("Input should be less than or equal to {le}"),
            "less_than_equal",
        );
        return None;
    }
    Some(n)
}

/// A declared `str` parameter with `min_length`/`max_length` (pydantic counts characters).
pub fn str_param<'a>(
    v: &mut Validation,
    field: &str,
    raw: Option<&'a str>,
    min_length: Option<usize>,
    max_length: Option<usize>,
) -> Option<&'a str> {
    let raw = raw?;
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

/// A declared `bool` parameter: pydantic-core's lax string set, ASCII case-insensitive, no
/// trimming.
pub fn bool_param(v: &mut Validation, field: &str, raw: Option<&str>) -> Option<bool> {
    let raw = raw?;
    match raw.to_ascii_lowercase().as_str() {
        "0" | "off" | "f" | "false" | "n" | "no" => Some(false),
        "1" | "on" | "t" | "true" | "y" | "yes" => Some(true),
        _ => {
            v.error(
                field,
                "Input should be a valid boolean, unable to interpret input",
                "bool_parsing",
            );
            None
        }
    }
}

/// `scoped_sole_user_id(session, principal_user_id, surface=)`, as
/// [`crate::routes::common::scoped_sole_user_id`].
///
/// # Errors
/// As [`crate::routes::common::resolve_sole_tenant`], plus the 404 refusal.
pub async fn scoped_sole_user_id(
    state: &AppState,
    principal_user_id: Option<i64>,
    surface: Option<&str>,
) -> Result<i64, Problem> {
    crate::routes::common::scoped_sole_user_id(&state.db, principal_user_id, surface).await
}

/// `timedelta.total_seconds()` of `later - earlier`: integer microseconds over 10**6, exactly
/// rounded (Python divides two ints).
#[must_use]
pub fn total_seconds(delta: chrono::TimeDelta) -> f64 {
    let micros = delta.num_microseconds().unwrap_or(i64::MAX);
    #[allow(clippy::cast_precision_loss)]
    let m = micros as f64;
    m / 1_000_000.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pydantic_int_is_pydantic_cores_lax_string_rule() {
        // Each pair was run through `TypeAdapter(int).validate_python` on the bed's venv.
        for (s, want) in [
            ("5", Some(5)),
            (" 7 ", Some(7)),
            ("+3", Some(3)),
            ("-0", Some(0)),
            ("1.0", Some(1)),
            ("1.00000", Some(1)),
            ("1_000", Some(1000)),
            ("1_0.0", Some(10)),
            ("007", Some(7)),
            ("\t8\n", Some(8)),
            ("\u{a0}5", Some(5)),
            ("\u{b}5", Some(5)),
            ("-1.0", Some(-1)),
            ("1.5", None),
            ("1e2", None),
            ("0x10", None),
            ("", None),
            ("abc", None),
            ("1.", None),
            ("  ", None),
            ("1__0", None),
            ("_1", None),
            ("1_", None),
            ("+_1", None),
            ("1.0_0", None),
            ("1.0.0", None),
            ("+-1", None),
            ("- 1", None),
            ("1 0", None),
            ("\u{1c}5", None),
            ("5\0", None),
            ("-", None),
            (".0", None),
            ("0.", None),
            ("\u{ff11}", None),
            ("\u{663}", None),
        ] {
            assert_eq!(pydantic_int(s), want, "{s:?}");
        }
        assert_eq!(
            pydantic_int("9999999999999999999999"),
            Some(9_999_999_999_999_999_999_999)
        );
    }

    #[test]
    fn bounds_and_bools_fail_with_pydantics_words() {
        let mut v = Validation::new();
        assert_eq!(
            int_param(&mut v, "limit", Some("0"), Some(1), Some(365)),
            None
        );
        assert_eq!(
            int_param(&mut v, "limit", Some("366"), Some(1), Some(365)),
            None
        );
        assert_eq!(
            int_param(&mut v, "limit", Some("x"), Some(1), Some(365)),
            None
        );
        assert_eq!(
            int_param(&mut v, "limit", Some("365"), Some(1), Some(365)),
            Some(365)
        );
        assert_eq!(bool_param(&mut v, "flag", Some("TrUe")), Some(true));
        assert_eq!(bool_param(&mut v, "flag", Some("off")), Some(false));
        assert_eq!(bool_param(&mut v, "flag", Some(" true")), None);
        let Err(p) = v.finish() else {
            unreachable!("four errors were recorded")
        };
        let body = p.body(None).to_python_json().unwrap_or_default();
        assert!(body.contains(r#"{"field":"limit","message":"Input should be greater than or equal to 1","type":"greater_than_equal"}"#), "{body}");
        assert!(body.contains(r#"{"field":"limit","message":"Input should be less than or equal to 365","type":"less_than_equal"}"#), "{body}");
        assert!(body.contains(r#""type":"int_parsing""#), "{body}");
        assert!(body.contains(r#"{"field":"flag","message":"Input should be a valid boolean, unable to interpret input","type":"bool_parsing"}"#), "{body}");
    }

    #[test]
    fn total_seconds_is_exact() {
        assert!(
            (total_seconds(chrono::TimeDelta::microseconds(1_500_001)) - 1.500_001).abs() < 1e-12
        );
    }
}
