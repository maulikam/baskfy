//! What the four derivatives routers share: the IST clock, the sole-tenant guard
//! (`baskfy_api.curated_tenant` / `curated_seed.resolve_sole_user_id`), pydantic's lax query
//! parsing for `int` and `Literal` parameters, the Redis `GET` Python's `app.state.cache` makes,
//! and Python's `json.dumps` string escaping for the byte-compared bodies (SSE, NDJSON).

use std::sync::Arc;

use baskfy_core::decimal::WideDecimal;
use baskfy_db::{Db, DbError, OrderedJson};
use baskfy_providers::ratelimit::{RedisScriptBackend, ScriptBackend, ScriptValue};
use baskfy_providers::secret::Secret;
use chrono::{DateTime, FixedOffset, NaiveDate, Offset, TimeZone, Utc};
use serde::Serializer;
use tokio::sync::Mutex;

use crate::logging;
use crate::problems::{Problem, ProblemType, pipeline_degraded};
use crate::query::Validation;

/// `IST`'s offset: Asia/Kolkata has had no DST since 1945, so a fixed +05:30 is exact.
pub(crate) const IST_SECONDS: i32 = 19_800;

/// `baskfy_core.market_hours_cb.IST` as a fixed offset.
pub(crate) fn ist() -> FixedOffset {
    FixedOffset::east_opt(IST_SECONDS).unwrap_or_else(|| Utc.fix())
}

/// `dt.datetime.now(tz=IST)` — every router's `_now()` seam.
pub(crate) fn now_ist() -> DateTime<FixedOffset> {
    crate::entitlements::utc_now().with_timezone(&ist())
}

/// `datetime.combine(day, time(0, 0), tzinfo=IST)` as an instant.
pub(crate) fn ist_midnight(day: NaiveDate) -> DateTime<Utc> {
    let naive = day.and_hms_opt(0, 0, 0).unwrap_or_default();
    ist()
        .from_local_datetime(&naive)
        .single()
        .map_or_else(|| naive.and_utc(), |t| t.with_timezone(&Utc))
}

/// A database error as the 500 catch-all.
pub(crate) fn db_err(e: sqlx::Error) -> Problem {
    crate::db_failure(&e.into())
}

/// A row-decoding error as the 500 catch-all.
pub(crate) fn row_err(e: DbError) -> Problem {
    crate::db_failure(&e)
}

// --- typed column reads (every integer column is cast to int8, every enum to text, in SQL) ---

/// Column readers that turn a decode failure into the 500 catch-all.
pub(crate) mod col {
    use baskfy_db::{OrderedJson, row};
    use chrono::{DateTime, NaiveDate, NaiveTime, Utc};
    use rust_decimal::Decimal;
    use sqlx::Row;
    use sqlx::postgres::PgRow;

    use super::{db_err, row_err};
    use crate::problems::Problem;

    /// An `int8` column.
    pub(crate) fn i64(r: &PgRow, c: &str) -> Result<i64, Problem> {
        r.try_get::<i64, _>(c).map_err(db_err)
    }
    /// A nullable `int8` column.
    pub(crate) fn opt_i64(r: &PgRow, c: &str) -> Result<Option<i64>, Problem> {
        r.try_get::<Option<i64>, _>(c).map_err(db_err)
    }
    /// A text column.
    pub(crate) fn text(r: &PgRow, c: &str) -> Result<String, Problem> {
        r.try_get::<String, _>(c).map_err(db_err)
    }
    /// A nullable text column.
    pub(crate) fn opt_text(r: &PgRow, c: &str) -> Result<Option<String>, Problem> {
        r.try_get::<Option<String>, _>(c).map_err(db_err)
    }
    /// A `text[]` column (`NULL` read as empty, Python's `row.x or []`).
    pub(crate) fn texts(r: &PgRow, c: &str) -> Result<Vec<String>, Problem> {
        Ok(r.try_get::<Option<Vec<String>>, _>(c)
            .map_err(db_err)?
            .unwrap_or_default())
    }
    /// A boolean column.
    pub(crate) fn boolean(r: &PgRow, c: &str) -> Result<bool, Problem> {
        r.try_get::<bool, _>(c).map_err(db_err)
    }
    /// A `date` column.
    pub(crate) fn date(r: &PgRow, c: &str) -> Result<NaiveDate, Problem> {
        row::date(r, c).map_err(row_err)
    }
    /// A nullable `date` column.
    pub(crate) fn opt_date(r: &PgRow, c: &str) -> Result<Option<NaiveDate>, Problem> {
        row::opt_date(r, c).map_err(row_err)
    }
    /// A `timestamptz` column, in UTC.
    pub(crate) fn ts(r: &PgRow, c: &str) -> Result<DateTime<Utc>, Problem> {
        row::timestamptz(r, c).map_err(row_err)
    }
    /// A nullable `timestamptz` column.
    pub(crate) fn opt_ts(r: &PgRow, c: &str) -> Result<Option<DateTime<Utc>>, Problem> {
        row::opt_timestamptz(r, c).map_err(row_err)
    }
    /// A `time` column.
    pub(crate) fn time(r: &PgRow, c: &str) -> Result<NaiveTime, Problem> {
        r.try_get::<NaiveTime, _>(c).map_err(db_err)
    }
    /// A `numeric` column, with its display scale.
    pub(crate) fn dec(r: &PgRow, c: &str) -> Result<Decimal, Problem> {
        row::decimal(r, c).map_err(row_err)
    }
    /// A nullable `numeric` column.
    pub(crate) fn opt_dec(r: &PgRow, c: &str) -> Result<Option<Decimal>, Problem> {
        row::opt_decimal(r, c).map_err(row_err)
    }
    /// A nullable `jsonb` column.
    pub(crate) fn opt_json(r: &PgRow, c: &str) -> Result<Option<OrderedJson>, Problem> {
        row::opt_jsonb(r, c).map_err(row_err)
    }
    /// `dict(row.x or {})`: a `jsonb` object column, `NULL` read as `{}`.
    pub(crate) fn object(r: &PgRow, c: &str) -> Result<OrderedJson, Problem> {
        match opt_json(r, c)? {
            None => Ok(OrderedJson::Object(Vec::new())),
            Some(v @ OrderedJson::Object(_)) => Ok(v),
            Some(_) => Err(crate::problems::internal_error()),
        }
    }
}

// --- the sole tenant -----------------------------------------------------------------------

/// `baskfy_core.curated_baskets.SOLE_USER_ENV`.
pub(crate) const SOLE_USER_ENV: &str = "BASKFY_SOLE_USER_ID";
/// `baskfy_api.seed.E2E_PUBLIC_ID`.
pub(crate) const E2E_PUBLIC_ID: &str = "e2e000000001";
/// `curated_tenant.SOLE_TENANT_REFUSED`.
pub(crate) const SOLE_TENANT_REFUSED: &str = "not-the-sole-tenant";

/// Python's `int(text)` for the decimal digits an env var holds: surrounding whitespace, a sign,
/// and single underscores between digits. `None` is Python's `ValueError`.
pub(crate) fn python_int(text: &str) -> Option<i64> {
    let s = text.trim();
    let (neg, body) = match s.as_bytes().first() {
        Some(b'-') => (true, &s[1..]),
        Some(b'+') => (false, &s[1..]),
        _ => (false, s),
    };
    let digits = underscored_digits(body)?;
    let value: i64 = digits.parse().ok()?;
    Some(if neg { -value } else { value })
}

/// Digits with single `_` separators between them (Python's and pydantic's grouping rule), the
/// separators removed; `None` when the text is not that.
fn underscored_digits(body: &str) -> Option<String> {
    if body.is_empty() || body.starts_with('_') || body.ends_with('_') || body.contains("__") {
        return None;
    }
    if !body.chars().all(|c| c.is_ascii_digit() || c == '_') {
        return None;
    }
    Some(body.replace('_', ""))
}

/// `resolve_sole_user_id(session)`: `BASKFY_SOLE_USER_ID` (stripped; blank or not a number is
/// "unset", with a warning), else the seeded account's id, else 503. Reads only.
///
/// # Errors
/// 503 `pipeline-degraded` when neither names an account; 500 on a database failure.
pub(crate) async fn resolve_sole_user_id(db: &Db) -> Result<i64, Problem> {
    let raw = std::env::var(SOLE_USER_ENV).unwrap_or_default();
    let raw = raw.trim();
    if !raw.is_empty() {
        if let Some(id) = python_int(raw) {
            return Ok(id);
        }
        logging::warning(
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
        .map_err(db_err)?;
    found.ok_or_else(|| {
        pipeline_degraded(&format!(
            "The sole-tenant account is not configured on this deployment. Set \
             {SOLE_USER_ENV}, or run `make seed` to create the development account."
        ))
    })
}

/// `scoped_sole_user_id(session, principal_user_id, surface=)`: the sole tenant's id, or a 404
/// carrying `reason: not-the-sole-tenant` for any other principal.
///
/// # Errors
/// As described, plus [`resolve_sole_user_id`]'s.
pub(crate) async fn scoped_sole_user_id(
    db: &Db,
    principal_user_id: Option<i64>,
    surface: Option<&str>,
) -> Result<i64, Problem> {
    let sole = resolve_sole_user_id(db).await?;
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
                "This deployment serves a single account and this caller (user {caller}) \
                     is not it{tail}"
            ),
        )
        .with("reason", OrderedJson::Str(SOLE_TENANT_REFUSED.to_owned())));
    }
    Ok(sole)
}

// --- pydantic's lax query parsing -----------------------------------------------------------

/// A Python `int` parsed from a query string: small enough for `i128`, or a sign and the fact
/// that it is beyond any bound this API states.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PyInt {
    /// The value, when it fits `i128`.
    pub(crate) value: Option<i128>,
    /// Its sign (a zero is non-negative).
    pub(crate) negative: bool,
    /// `str(int(text))`: the canonical spelling Python would print.
    pub(crate) canonical: String,
}

/// pydantic-core's lax `str -> int`: surrounding (Unicode) whitespace, a sign, digits with single
/// underscores between them, and an optional fractional part of zeros only (`"2026.00"`).
pub(crate) fn lax_int(raw: &str) -> Option<PyInt> {
    let s = raw.trim();
    let (neg, body) = match s.as_bytes().first() {
        Some(b'-') => (true, &s[1..]),
        Some(b'+') => (false, &s[1..]),
        _ => (false, s),
    };
    let (int_part, frac) = match body.split_once('.') {
        Some((i, f)) => (i, Some(f)),
        None => (body, None),
    };
    if let Some(f) = frac
        && (f.is_empty() || !f.chars().all(|c| c == '0'))
    {
        return None;
    }
    let digits = underscored_digits(int_part)?;
    let trimmed = digits.trim_start_matches('0');
    let magnitude = if trimmed.is_empty() { "0" } else { trimmed };
    let negative = neg && magnitude != "0";
    let canonical = if negative {
        format!("-{magnitude}")
    } else {
        magnitude.to_owned()
    };
    let value = canonical.parse::<i128>().ok();
    Some(PyInt {
        value,
        negative,
        canonical,
    })
}

/// An `int` query parameter with optional `ge`/`le` bounds, validated as FastAPI does; a failure
/// is recorded on `v` with pydantic's type and message.
pub(crate) fn validate_int(
    v: &mut Validation,
    name: &str,
    raw: Option<&str>,
    ge: Option<i64>,
    le: Option<i64>,
) -> Option<PyInt> {
    let raw = raw?;
    let Some(parsed) = lax_int(raw) else {
        v.error(
            name,
            "Input should be a valid integer, unable to parse string as an integer",
            "int_parsing",
        );
        return None;
    };
    let below = |bound: i64| match parsed.value {
        Some(x) => x < i128::from(bound),
        None => parsed.negative,
    };
    let above = |bound: i64| match parsed.value {
        Some(x) => x > i128::from(bound),
        None => !parsed.negative,
    };
    if let Some(g) = ge
        && below(g)
    {
        v.error(
            name,
            &format!("Input should be greater than or equal to {g}"),
            "greater_than_equal",
        );
        return None;
    }
    if let Some(l) = le
        && above(l)
    {
        v.error(
            name,
            &format!("Input should be less than or equal to {l}"),
            "less_than_equal",
        );
        return None;
    }
    Some(parsed)
}

/// A `Literal[...]` query parameter: one of `allowed`, else pydantic's `literal_error`. Kept for
/// `GET /overlap`'s `scope` (not ported yet, DECISIONS-RS R5.21); its behaviour is tested here.
#[allow(dead_code)]
pub(crate) fn validate_literal<'a>(
    v: &mut Validation,
    name: &str,
    raw: Option<&str>,
    allowed: &[&'a str],
) -> Option<&'a str> {
    let raw = raw?;
    if let Some(hit) = allowed.iter().find(|a| **a == raw) {
        return Some(hit);
    }
    let quoted: Vec<String> = allowed.iter().map(|a| format!("'{a}'")).collect();
    let expected = match quoted.split_last() {
        Some((last, rest)) if !rest.is_empty() => format!("{} or {last}", rest.join(", ")),
        Some((last, _)) => last.clone(),
        None => String::new(),
    };
    v.error(
        name,
        &format!("Input should be {expected}"),
        "literal_error",
    );
    None
}

// --- Python's json.dumps string escaping -----------------------------------------------------

/// `json.dumps(text)` (`ensure_ascii=True`): every non-ASCII code point as `\uXXXX` (surrogate
/// pairs above the BMP), the short escapes for `"`, `\`, `\n`, `\r`, `\t`, `\b`, `\f`, and
/// `\u00XX` for the other control characters.
pub(crate) fn py_json_str_ascii(text: &str) -> String {
    py_json_str(text, true)
}

/// `json.dumps(text, ensure_ascii=False)`.
pub(crate) fn py_json_str_utf8(text: &str) -> String {
    py_json_str(text, false)
}

fn py_json_str(text: &str, ascii: bool) -> String {
    let mut out = String::with_capacity(text.len() + 2);
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c if ascii && !c.is_ascii() => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

// --- serde helpers for computed decimals -----------------------------------------------------

/// A `WideDecimal | None` field: `str(Decimal)` as a JSON string, or `null`.
///
/// # Errors
/// The serializer's.
pub(crate) fn ser_opt_wide<S: Serializer>(
    d: &Option<WideDecimal>,
    s: S,
) -> Result<S::Ok, S::Error> {
    match d {
        Some(d) => s.serialize_str(&d.to_sci_string()),
        None => s.serialize_none(),
    }
}

/// A `WideDecimal` field: `str(Decimal)` as a JSON string.
///
/// # Errors
/// The serializer's.
pub(crate) fn ser_wide<S: Serializer>(d: &WideDecimal, s: S) -> Result<S::Ok, S::Error> {
    s.serialize_str(&d.to_sci_string())
}

/// A list of `WideDecimal`s, each `str(Decimal)`.
///
/// # Errors
/// The serializer's.
pub(crate) fn ser_wides<S: Serializer>(ds: &[WideDecimal], s: S) -> Result<S::Ok, S::Error> {
    s.collect_seq(ds.iter().map(WideDecimal::to_sci_string))
}

// --- the process-wide Redis client (`app.state.cache`) ---------------------------------------

/// `redis.call('GET', key)` — the one read the cache answers for these routers.
const GET_SCRIPT: &str = "return redis.call('GET', KEYS[1])";

static CACHE: Mutex<Option<Arc<RedisScriptBackend>>> = Mutex::const_new(None);

async fn cache_client(url: &Secret) -> Option<Arc<RedisScriptBackend>> {
    let mut slot = CACHE.lock().await;
    if let Some(client) = slot.as_ref() {
        return Some(Arc::clone(client));
    }
    match tokio::time::timeout(
        std::time::Duration::from_secs(5),
        RedisScriptBackend::connect(url),
    )
    .await
    {
        Ok(Ok(client)) => {
            let client = Arc::new(client);
            *slot = Some(Arc::clone(&client));
            Some(client)
        }
        Ok(Err(e)) => {
            logging::warning(
                "baskfy_api.app",
                "redis unavailable",
                &[("error", &e.to_string())],
            );
            None
        }
        Err(_) => None,
    }
}

/// `await cache.get(key)` as these routers use it: the stored text, or `None` when the key is
/// absent **or** Redis failed (every caller treats a `RedisError` as "nothing stored").
pub(crate) async fn cache_get(url: &Secret, key: &str) -> Option<Vec<u8>> {
    let client = cache_client(url).await?;
    match client.eval(GET_SCRIPT, &[key.to_owned()], &[]).await {
        Ok(ScriptValue::Bytes(b)) => Some(b),
        Ok(_) => None,
        Err(e) => {
            logging::warning(
                "baskfy_api.app",
                "redis read failed",
                &[("error", &e.to_string())],
            );
            None
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn lax_int_is_pydantics() {
        // Each answer checked against pydantic 2.13's TypeAdapter(int) in lax mode.
        for (raw, want) in [
            ("2026", Some("2026")),
            (" 2026 ", Some("2026")),
            ("+2026", Some("2026")),
            ("2026.0", Some("2026")),
            ("2026.00", Some("2026")),
            ("-2026.0", Some("-2026")),
            ("2_026.0", Some("2026")),
            ("0002026", Some("2026")),
            ("1_0.0", Some("10")),
            ("0.0", Some("0")),
            ("-0", Some("0")),
            ("\t7\u{b}", Some("7")),
            ("7\u{a0}", Some("7")),
            ("2026.", None),
            (".0", None),
            ("2026.5", None),
            ("1e3", None),
            ("abc", None),
            ("", None),
            ("1__0", None),
            ("_10", None),
            ("10_", None),
            ("+-1", None),
            ("- 1", None),
            ("1.0_0", None),
            ("0x10", None),
        ] {
            assert_eq!(
                lax_int(raw).map(|p| p.canonical),
                want.map(str::to_owned),
                "{raw:?}"
            );
        }
        let huge = lax_int("99999999999999999999999999999999999999999").unwrap();
        assert!(huge.value.is_none() && !huge.negative);
    }

    #[test]
    fn bounds_and_literals_say_what_pydantic_says() {
        let mut v = Validation::new();
        assert!(validate_int(&mut v, "year", Some("2019"), Some(2020), Some(2100)).is_none());
        assert!(
            validate_int(
                &mut v,
                "year",
                Some("99999999999999999999999"),
                Some(2020),
                Some(2100)
            )
            .is_none()
        );
        assert!(validate_literal(&mut v, "scope", Some("x"), &["actionable", "all"]).is_none());
        let body = v
            .finish()
            .err()
            .unwrap()
            .body(None)
            .to_python_json()
            .unwrap();
        assert!(body.contains(r#""message":"Input should be greater than or equal to 2020","type":"greater_than_equal""#), "{body}");
        assert!(
            body.contains(
                r#""message":"Input should be less than or equal to 2100","type":"less_than_equal""#
            ),
            "{body}"
        );
        assert!(
            body.contains(
                r#""message":"Input should be 'actionable' or 'all'","type":"literal_error""#
            ),
            "{body}"
        );
    }

    #[test]
    fn json_strings_escape_as_python_does() {
        assert_eq!(
            py_json_str_ascii("a\"b\\c\nd\u{1}é😀"),
            "\"a\\\"b\\\\c\\nd\\u0001\\u00e9\\ud83d\\ude00\""
        );
        assert_eq!(py_json_str_utf8("é\u{7f}"), "\"é\u{7f}\"");
    }

    #[test]
    fn python_int_reads_the_env_var_as_python_does() {
        assert_eq!(python_int(" 42 "), Some(42));
        assert_eq!(python_int("1_000"), Some(1000));
        assert_eq!(python_int("4.0"), None);
        assert_eq!(python_int(""), None);
    }
}
