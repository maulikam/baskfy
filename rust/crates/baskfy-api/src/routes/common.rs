//! What every router shares, held once (DECISIONS-RS R5.10's collapse, wave 4c's foundation).
//!
//! The five RS4b lanes each wrote a private copy of the same three things, because none of them
//! could edit a shared module. Every copy was graded EQUAL; they are now this one module, and the
//! lanes' names (`reference::scoped_sole_user_id`, `sleeves::common::lax_int`, ...) delegate here
//! so no call site moved. Where the copies differed, the most complete one won (R5.14 named the
//! sleeves copy for the tenant; R5.35's Unicode tables for Python's `int()`):
//!
//! * **pydantic-core's lax `str -> int`** ([`lax_int`]) and `str -> bool` ([`lax_bool`]) — the
//!   rule FastAPI applies to an `int`/`bool` query or path parameter;
//! * **Python's `int(text)`** ([`python_int`]) — what `resolve_sole_user_id` and the backtest
//!   cursor apply to a string, which (unlike pydantic) reads any script's decimal digits;
//! * **the sole tenant** ([`resolve_sole_tenant`], [`scoped_sole_user_id`]) — Python's
//!   `curated_seed.resolve_sole_user_id` and `curated_tenant.scoped_sole_user_id`.

use baskfy_db::{Db, OrderedJson};
use chrono::{DateTime, FixedOffset, NaiveDate};

use crate::problems::{Problem, ProblemType, internal_error, pipeline_degraded};

// ---------------------------------------------------------------------------------------------
// Python integers
// ---------------------------------------------------------------------------------------------

/// A Python `int` read from text: exact while it fits `i128`, and always its sign and the
/// canonical spelling `str(int(text))` would print (Python's ints are unbounded).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PyInt {
    /// The value, when it fits `i128`.
    pub(crate) value: Option<i128>,
    /// Its sign (a zero is non-negative).
    pub(crate) negative: bool,
    /// `str(int(text))`.
    pub(crate) canonical: String,
}

impl PyInt {
    fn from_digits(negative: bool, digits: &str) -> Self {
        let trimmed = digits.trim_start_matches('0');
        let magnitude = if trimmed.is_empty() { "0" } else { trimmed };
        let negative = negative && magnitude != "0";
        let canonical = if negative {
            format!("-{magnitude}")
        } else {
            magnitude.to_owned()
        };
        let value = canonical.parse::<i128>().ok();
        Self {
            value,
            negative,
            canonical,
        }
    }

    /// The value saturated into `i128` (beyond it, only the sign matters to any bound a route
    /// declares).
    #[must_use]
    pub(crate) fn saturated(&self) -> i128 {
        self.value
            .unwrap_or(if self.negative { i128::MIN } else { i128::MAX })
    }

    /// The value when a `bigint` can hold it.
    #[must_use]
    pub(crate) fn as_i64(&self) -> Option<i64> {
        self.value.and_then(|v| i64::try_from(v).ok())
    }
}

/// Digits with single `_` separators *between* them (Python's and pydantic's grouping rule),
/// mapped through `digit` and with the separators removed; `None` when the text is not that.
fn grouped_digits(body: &str, digit: impl Fn(char) -> Option<u32>) -> Option<String> {
    if body.is_empty() || body.starts_with('_') || body.ends_with('_') || body.contains("__") {
        return None;
    }
    let mut out = String::with_capacity(body.len());
    for c in body.chars() {
        if c == '_' {
            continue;
        }
        out.push(char::from_digit(digit(c)?, 10)?);
    }
    Some(out)
}

fn split_sign(s: &str) -> (bool, &str) {
    match s.as_bytes().first() {
        Some(b'-') => (true, &s[1..]),
        Some(b'+') => (false, &s[1..]),
        _ => (false, s),
    }
}

/// pydantic-core's lax `str -> int`, or `None` for `int_parsing`: `str::trim` (Unicode
/// `White_Space`), an optional sign, **ASCII** digits with single underscores between them, and
/// an optional `.` followed only by at least one `0` (`"12.0"` is 12; `"12."`, `"12.5"`,
/// `"1e2"`, `"0x10"` and full-width digits are refused). Probed against pydantic 2 by every lane.
#[must_use]
pub(crate) fn lax_int(raw: &str) -> Option<PyInt> {
    let (negative, body) = split_sign(raw.trim());
    let int_part = match body.split_once('.') {
        Some((i, f)) => {
            if f.is_empty() || !f.bytes().all(|b| b == b'0') {
                return None;
            }
            i
        }
        None => body,
    };
    let digits = grouped_digits(int_part, |c| c.is_ascii_digit().then(|| c.to_digit(10))?)?;
    Some(PyInt::from_digits(negative, &digits))
}

/// pydantic-core's lax `str -> bool`.
#[must_use]
pub(crate) fn lax_bool(raw: &str) -> Option<bool> {
    match raw.to_lowercase().as_str() {
        "0" | "off" | "f" | "false" | "n" | "no" => Some(false),
        "1" | "on" | "t" | "true" | "y" | "yes" => Some(true),
        _ => None,
    }
}

/// The first code point of every run of ten Unicode decimal digits (`Nd`, 0-9 in order) —
/// exactly the characters CPython 3.12's `int()` reads (`unicodedata.decimal`, Unicode 15.0.0).
/// Generated from the live Python (R5.35); regenerate when its Unicode version changes.
const PY_DECIMAL_ZEROS: [u32; 68] = [
    0x30, 0x660, 0x6F0, 0x7C0, 0x966, 0x9E6, 0xA66, 0xAE6, 0xB66, 0xBE6, 0xC66, 0xCE6, 0xD66,
    0xDE6, 0xE50, 0xED0, 0xF20, 0x1040, 0x1090, 0x17E0, 0x1810, 0x1946, 0x19D0, 0x1A80, 0x1A90,
    0x1B50, 0x1BB0, 0x1C40, 0x1C50, 0xA620, 0xA8D0, 0xA900, 0xA9D0, 0xA9F0, 0xAA50, 0xABF0, 0xFF10,
    0x104A0, 0x10D30, 0x11066, 0x110F0, 0x11136, 0x111D0, 0x112F0, 0x11450, 0x114D0, 0x11650,
    0x116C0, 0x11730, 0x118E0, 0x11950, 0x11C50, 0x11D50, 0x11DA0, 0x11F50, 0x16A60, 0x16AC0,
    0x16B50, 0x1D7CE, 0x1D7D8, 0x1D7E2, 0x1D7EC, 0x1D7F6, 0x1E140, 0x1E2F0, 0x1E4F0, 0x1E950,
    0x1FBF0,
];

/// The characters `str.isdigit()` accepts that are **not** decimal digits (superscripts,
/// circled digits, ...): `isdigit()` passes them and `int()` then raises. Same provenance.
pub(crate) const PY_DIGIT_ONLY: [(u32, u32); 20] = [
    (0xB2, 0xB3),
    (0xB9, 0xB9),
    (0x1369, 0x1371),
    (0x19DA, 0x19DA),
    (0x2070, 0x2070),
    (0x2074, 0x2079),
    (0x2080, 0x2089),
    (0x2460, 0x2468),
    (0x2474, 0x247C),
    (0x2488, 0x2490),
    (0x24EA, 0x24EA),
    (0x24F5, 0x24FD),
    (0x24FF, 0x24FF),
    (0x2776, 0x277E),
    (0x2780, 0x2788),
    (0x278A, 0x2792),
    (0x10A40, 0x10A43),
    (0x10E60, 0x10E68),
    (0x11052, 0x1105A),
    (0x1F100, 0x1F10A),
];

/// `unicodedata.decimal(ch, None)`.
#[must_use]
pub(crate) fn py_decimal(ch: char) -> Option<u32> {
    let c = u32::from(ch);
    PY_DECIMAL_ZEROS
        .iter()
        .find(|&&zero| (zero..zero + 10).contains(&c))
        .map(|zero| c - zero)
}

/// `str.isspace()` for one character: Unicode `White_Space` plus the four ASCII separators
/// (`\x1c`-`\x1f`) Python also counts.
#[must_use]
pub(crate) fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Python's `int(text)` (base 10): surrounding whitespace, an optional sign, any script's decimal
/// digits with single underscores between them; no fraction. `None` is Python's `ValueError`.
#[must_use]
pub(crate) fn python_int(text: &str) -> Option<PyInt> {
    let (negative, body) = split_sign(text.trim_matches(py_space));
    let digits = grouped_digits(body, py_decimal)?;
    Some(PyInt::from_digits(negative, &digits))
}

// ---------------------------------------------------------------------------------------------
// The IST clock and the published date
// ---------------------------------------------------------------------------------------------

/// IST, `+05:30` (`baskfy_providers::tokens::ist`).
#[must_use]
pub(crate) fn ist() -> FixedOffset {
    baskfy_providers::tokens::ist()
}

/// `datetime.now(tz=IST)`, read through the API's one clock (`entitlements::utc_now`).
#[must_use]
pub(crate) fn now_ist() -> DateTime<FixedOffset> {
    crate::entitlements::utc_now().with_timezone(&ist())
}

/// `today_ist()` (`invoices.today_ist`, `portfolio_overview._today_ist`).
#[must_use]
pub(crate) fn today_ist() -> NaiveDate {
    now_ist().date_naive()
}

/// `screener.latest_published_date(session)`: the newest trade date of a published run.
///
/// # Errors
/// The 500 catch-all on a database failure.
pub(crate) async fn latest_published_date(db: &Db) -> Result<Option<NaiveDate>, Problem> {
    sqlx::query_scalar("SELECT max(trade_date) FROM pipeline_run WHERE data_version IS NOT NULL")
        .fetch_one(db.pool())
        .await
        .map_err(|e| crate::db_failure(&e.into()))
}

// ---------------------------------------------------------------------------------------------
// The sole tenant (`baskfy_api.curated_seed`, `baskfy_api.curated_tenant`)
// ---------------------------------------------------------------------------------------------

/// `baskfy_core.curated_baskets.SOLE_USER_ENV`.
pub(crate) const SOLE_USER_ENV: &str = "BASKFY_SOLE_USER_ID";
/// `baskfy_api.seed.E2E_PUBLIC_ID`: the development account the fallback looks up.
pub(crate) const E2E_PUBLIC_ID: &str = "e2e000000001";
/// `curated_tenant.SOLE_TENANT_REFUSED`.
pub(crate) const SOLE_TENANT_REFUSED: &str = "not-the-sole-tenant";

/// What `resolve_sole_user_id` returned.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum SoleId {
    /// An id a `bigint` can hold.
    Id(i64),
    /// A valid integer beyond `i64`: Python keeps it, so every caller is "not it", and a query
    /// bound with it fails in asyncpg (the 500 catch-all).
    Huge,
}

/// `resolve_sole_user_id(session)`: `BASKFY_SOLE_USER_ID` from the **process environment** on
/// every call (R5.14), stripped, read with Python's `int()`; blank or not a number is "unset",
/// with Python's warning; unset falls back to the seeded development account, else a 503 naming
/// the variable. Reads only — it never creates an account.
///
/// # Errors
/// 503 `pipeline-degraded` when nothing names an account; the 500 catch-all on a database failure.
pub(crate) async fn resolve_sole_tenant(db: &Db) -> Result<SoleId, Problem> {
    let raw = std::env::var(SOLE_USER_ENV).unwrap_or_default();
    let raw = raw.trim_matches(py_space);
    if !raw.is_empty() {
        if let Some(id) = python_int(raw) {
            return Ok(id.as_i64().map_or(SoleId::Huge, SoleId::Id));
        }
        crate::logging::warning(
            "baskfy_api.curated_seed",
            &format!(
                "{SOLE_USER_ENV} is not a number ({}); reading it as 'no sole tenant configured', \
                 which is what baskfy_worker.providers._sole_user_id does with the same value",
                baskfy_providers::py::repr_str(raw)
            ),
            &[],
        );
    }
    let found: Option<i64> = sqlx::query_scalar("SELECT id FROM app_user WHERE public_id = $1")
        .bind(E2E_PUBLIC_ID)
        .fetch_optional(db.pool())
        .await
        .map_err(|e| crate::db_failure(&e.into()))?;
    found.map(SoleId::Id).ok_or_else(|| {
        pipeline_degraded(&format!(
            "The sole-tenant account is not configured on this deployment. Set \
             {SOLE_USER_ENV}, or run `make seed` to create the development account."
        ))
    })
}

/// `scoped_sole_user_id(session, principal_user_id, surface=None)`: the sole tenant, or a 404
/// carrying `reason: not-the-sole-tenant` for any signed-in caller who is not it.
///
/// # Errors
/// As described, plus [`resolve_sole_tenant`]'s.
pub(crate) async fn scoped_sole_user_id(
    db: &Db,
    principal_user_id: Option<i64>,
    surface: Option<&str>,
) -> Result<i64, Problem> {
    let sole = resolve_sole_tenant(db).await?;
    if let Some(caller) = principal_user_id
        && SoleId::Id(caller) != sole
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
    match sole {
        SoleId::Id(id) => Ok(id),
        SoleId::Huge => Err(internal_error()),
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn lax(s: &str) -> Option<String> {
        lax_int(s).map(|p| p.canonical)
    }

    #[test]
    fn lax_int_is_pydantic_cores_string_rule() {
        // Each was run through `TypeAdapter(int).validate_python(s)` (pydantic 2, the bed's venv)
        // by the RS4b lanes; the union of their tables.
        for (s, want) in [
            ("5", Some("5")),
            (" 5", Some("5")),
            ("5 ", Some("5")),
            ("+5", Some("5")),
            ("05", Some("5")),
            ("-0", Some("0")),
            ("1_000", Some("1000")),
            ("12.0", Some("12")),
            ("1_0.00", Some("10")),
            ("999999999999999999999999", Some("999999999999999999999999")),
            ("12.", None),
            (".0", None),
            ("12.5", None),
            ("5e0", None),
            ("0x5", None),
            ("5_", None),
            ("_5", None),
            ("5__0", None),
            ("true", None),
            ("", None),
            ("١٠", None),
            ("５", None),
        ] {
            assert_eq!(lax(s).as_deref(), want, "{s:?}");
        }
        let big = lax_int("-99999999999999999999999999999999999999999").unwrap();
        assert!(big.negative && big.value.is_none());
        assert_eq!(big.saturated(), i128::MIN);
    }

    #[test]
    fn python_int_reads_every_scripts_decimal_digits() {
        // `int(s)` in CPython 3.12.
        for (s, want) in [
            ("1", Some("1")),
            (" +6 ", Some("6")),
            ("1_0", Some("10")),
            ("١", Some("1")),
            ("٤٢", Some("42")),
            ("\u{1c}7\u{1f}", Some("7")),
            ("1_0_", None),
            ("abc", None),
            ("1.0", None),
            ("²", None),
            ("", None),
        ] {
            assert_eq!(python_int(s).map(|p| p.canonical).as_deref(), want, "{s:?}");
        }
        assert_eq!(python_int("99999999999999999999").unwrap().as_i64(), None);
    }

    fn sole(s: &str) -> Option<SoleId> {
        python_int(s).map(|p| p.as_i64().map_or(SoleId::Huge, SoleId::Id))
    }

    #[test]
    fn the_sole_tenant_variable_is_read_as_pythons_int() {
        // Moved from the sleeves lane's copy (R5.10's collapse), unchanged.
        assert_eq!(sole("6"), Some(SoleId::Id(6)));
        assert_eq!(sole("+6"), Some(SoleId::Id(6)));
        assert_eq!(sole("-6"), Some(SoleId::Id(-6)));
        assert_eq!(sole("1_0"), Some(SoleId::Id(10)));
        assert_eq!(sole("-9223372036854775808"), Some(SoleId::Id(i64::MIN)));
        assert_eq!(sole("99999999999999999999"), Some(SoleId::Huge));
        assert_eq!(sole("abc"), None);
        assert_eq!(sole("1_0_"), None);
        assert_eq!(sole("_1"), None);
        assert_eq!(sole("1__0"), None);
        assert_eq!(sole("+-1"), None);
        // Moved from the derivatives lane's copy.
        assert_eq!(sole(" 42 "), Some(SoleId::Id(42)));
        assert_eq!(sole("1_000"), Some(SoleId::Id(1000)));
        assert_eq!(sole("4.0"), None);
        assert_eq!(sole(""), None);
    }

    #[test]
    fn lax_bool_is_pydantics() {
        for (s, want) in [
            ("true", Some(true)),
            ("YES", Some(true)),
            ("1", Some(true)),
            ("off", Some(false)),
            ("F", Some(false)),
            ("2", None),
            ("", None),
        ] {
            assert_eq!(lax_bool(s), want, "{s:?}");
        }
    }
}
