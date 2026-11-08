//! What the four derivatives routers share: the IST clock, the sole-tenant guard
//! (`baskfy_api.curated_tenant` / `curated_seed.resolve_sole_user_id`), pydantic's lax query
//! parsing for `int` and `Literal` parameters, the Redis `GET` Python's `app.state.cache` makes,
//! and Python's `json.dumps` string escaping for the byte-compared bodies (SSE, NDJSON).

use baskfy_core::decimal::WideDecimal;
use baskfy_db::{Db, DbError};
use chrono::{DateTime, FixedOffset, NaiveDate, Offset, TimeZone, Utc};
use serde::Serializer;

use crate::problems::Problem;
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

/// `scoped_sole_user_id(session, principal_user_id, surface=)`, as
/// [`crate::routes::common::scoped_sole_user_id`].
///
/// # Errors
/// As described, plus [`crate::routes::common::resolve_sole_tenant`]'s.
pub(crate) async fn scoped_sole_user_id(
    db: &Db,
    principal_user_id: Option<i64>,
    surface: Option<&str>,
) -> Result<i64, Problem> {
    crate::routes::common::scoped_sole_user_id(db, principal_user_id, surface).await
}

// --- pydantic's lax query parsing -----------------------------------------------------------

pub(crate) use crate::routes::common::{PyInt, lax_int};

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

// --- the process-wide Redis client: `crate::cache` (held once since wave 4c's foundation) ---

pub(crate) use crate::cache::cache_get;

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

    // `python_int_reads_the_env_var_as_python_does` moved with the function to
    // `routes::common` (R5.10's collapse).
}
