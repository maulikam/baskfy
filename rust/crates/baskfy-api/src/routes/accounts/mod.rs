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

use crate::problems::{Problem, ProblemType, pipeline_degraded};
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
/// are unbounded; anything beyond `i128` is far outside every bound a route declares).
///
/// pydantic-core's rule, probed against pydantic 2 on the bed's venv: `str::trim` (Unicode
/// `White_Space`), an optional sign, ASCII digits with single underscores *between* digits
/// (`int()`'s rule), and an optional `.` followed by at least one `0` and nothing else (`"1.0"`,
/// `"1_0.00"`). `"1."`, `".0"`, `"1e2"`, `"0x10"`, `"1.5"`, full-width digits: refused.
#[must_use]
pub fn pydantic_int(raw: &str) -> Option<i128> {
    let s = raw.trim();
    let s = match s.split_once('.') {
        Some((int_part, frac)) => {
            if frac.is_empty() || !frac.bytes().all(|b| b == b'0') {
                return None;
            }
            int_part
        }
        None => s,
    };
    python_int_literal(s)
}

/// Python's `int(text)` for base 10 over ASCII digits: whitespace stripped, an optional sign,
/// digits with single underscores between them. (Python also accepts non-ASCII decimal digits;
/// that is not reproduced — recorded in DECISIONS-RS.)
#[must_use]
pub fn python_int(raw: &str) -> Option<i128> {
    python_int_literal(raw.trim())
}

fn python_int_literal(s: &str) -> Option<i128> {
    let (negative, digits) = match s.as_bytes().first() {
        Some(b'-') => (true, &s[1..]),
        Some(b'+') => (false, &s[1..]),
        _ => (false, s),
    };
    if digits.is_empty() {
        return None;
    }
    let bytes = digits.as_bytes();
    let mut value: i128 = 0;
    let mut previous_underscore = true; // a leading underscore is refused
    for &b in bytes {
        if b == b'_' {
            if previous_underscore {
                return None;
            }
            previous_underscore = true;
            continue;
        }
        if !b.is_ascii_digit() {
            return None;
        }
        previous_underscore = false;
        value = value
            .saturating_mul(10)
            .saturating_add(i128::from(b - b'0'));
    }
    if previous_underscore {
        return None; // a trailing underscore
    }
    Some(if negative { -value } else { value })
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

/// `curated_seed.SOLE_USER_ENV`.
pub const SOLE_USER_ENV: &str = "BASKFY_SOLE_USER_ID";

/// `seed.E2E_PUBLIC_ID`: the development account the fallback looks up.
pub const E2E_PUBLIC_ID: &str = "e2e000000001";

/// `curated_tenant.SOLE_TENANT_REFUSED`.
pub const SOLE_TENANT_REFUSED: &str = "not-the-sole-tenant";

/// `resolve_sole_user_id`: `BASKFY_SOLE_USER_ID` read from the process environment on every
/// call (stripped; blank or non-numeric = unset), else the seeded development account by public
/// id, else a 503 naming the variable. Reads only.
///
/// # Errors
/// 503 `pipeline-degraded` when no tenant is configured; 500 for a database failure.
pub async fn resolve_sole_user_id(state: &AppState) -> Result<i64, Problem> {
    let raw = std::env::var(SOLE_USER_ENV).unwrap_or_default();
    let raw = raw.trim();
    if !raw.is_empty() {
        match python_int(raw).and_then(|n| i64::try_from(n).ok()) {
            Some(n) => return Ok(n),
            None => crate::logging::warning(
                "baskfy_api.curated_seed",
                "BASKFY_SOLE_USER_ID is not a number; reading it as 'no sole tenant configured'",
                &[],
            ),
        }
    }
    let found: Option<i64> = sqlx::query_scalar("SELECT id FROM app_user WHERE public_id = $1")
        .bind(E2E_PUBLIC_ID)
        .fetch_optional(state.db.pool())
        .await
        .map_err(db)?;
    found.ok_or_else(|| {
        pipeline_degraded(&format!(
            "The sole-tenant account is not configured on this deployment. Set {SOLE_USER_ENV}, \
             or run `make seed` to create the development account."
        ))
    })
}

/// `scoped_sole_user_id(session, principal_user_id, surface=)`: the sole tenant, or a 404 with
/// `reason: not-the-sole-tenant` for any other principal.
///
/// # Errors
/// As [`resolve_sole_user_id`], plus the 404 refusal.
pub async fn scoped_sole_user_id(
    state: &AppState,
    principal_user_id: Option<i64>,
    surface: Option<&str>,
) -> Result<i64, Problem> {
    let sole = resolve_sole_user_id(state).await?;
    if let Some(caller) = principal_user_id
        && caller != sole
    {
        let tail = surface.map_or_else(
            || ".".to_owned(),
            |s| format!(", so {s} is not readable here."),
        );
        return Err(Problem::new(
            ProblemType::NotFound,
            format!(
                "This deployment serves a single account and this caller (user {caller}) is not \
                 it{tail}"
            ),
        )
        .with("reason", OrderedJson::Str(SOLE_TENANT_REFUSED.to_owned())));
    }
    Ok(sole)
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
