//! Typed column reads, each matching what asyncpg + SQLAlchemy hand Python.
//!
//! | Postgres | Python (asyncpg) | here |
//! |---|---|---|
//! | `numeric` | `Decimal` with the stored scale (`numeric(18,4)` 1.5 → `Decimal('1.5000')`) | [`decimal`]: `rust_decimal::Decimal`, scale kept |
//! | `timestamptz` | aware `datetime` in UTC | [`timestamptz`]: `DateTime<Utc>` |
//! | `timestamp` | naive `datetime` | [`timestamp`]: `NaiveDateTime` |
//! | `date` | `date` | [`date`]: `NaiveDate` |
//! | `jsonb` | `json.loads(text)` → `dict` in stored key order | [`jsonb`]: [`OrderedJson`] |
//!
//! A `numeric` wider than `rust_decimal`'s 28 digits of scale/precision is a decode error, never
//! a silently rounded value (the R1.3 class, DECISIONS-RS R5.3).

use chrono::{DateTime, NaiveDate, NaiveDateTime, Utc};
use rust_decimal::Decimal;
use sqlx::postgres::{PgRow, PgValueFormat};
use sqlx::types::Json;
use sqlx::{Row, TypeInfo, ValueRef};

use crate::error::DbError;
use crate::json::OrderedJson;

fn get<'r, T>(row: &'r PgRow, column: &str) -> Result<T, DbError>
where
    T: sqlx::Decode<'r, sqlx::Postgres> + sqlx::Type<sqlx::Postgres>,
{
    row.try_get::<T, _>(column)
        .map_err(|e| match DbError::from(e) {
            DbError::Decode { message, .. } => DbError::Decode {
                column: column.to_owned(),
                message,
            },
            other => other,
        })
}

/// A non-null `numeric`.
///
/// # Errors
/// [`DbError::Decode`] for a NULL, a non-numeric column, or a value beyond 28 digits.
pub fn decimal(row: &PgRow, column: &str) -> Result<Decimal, DbError> {
    opt_decimal(row, column)?.ok_or_else(|| decode_error(column, "unexpected NULL"))
}

/// A nullable `numeric`.
///
/// Decoded here from Postgres's wire form rather than by sqlx's `Decimal` decoder, which drops
/// the display scale of a zero (`0::numeric(10,2)` arrives as `0`, where asyncpg gives
/// `Decimal('0.00')` and the API prints `"0.00"`). See [`numeric_from_binary`].
///
/// # Errors
/// [`DbError::Decode`] as [`decimal`].
pub fn opt_decimal(row: &PgRow, column: &str) -> Result<Option<Decimal>, DbError> {
    let raw = row.try_get_raw(column).map_err(DbError::from)?;
    if raw.is_null() {
        return Ok(None);
    }
    let type_name = raw.type_info().name().to_owned();
    if type_name != "NUMERIC" {
        return Err(decode_error(
            column,
            &format!("expected NUMERIC, found {type_name}"),
        ));
    }
    let parsed = match raw.format() {
        PgValueFormat::Binary => numeric_from_binary(
            raw.as_bytes()
                .map_err(|e| decode_error(column, &e.to_string()))?,
        ),
        PgValueFormat::Text => {
            let text = raw
                .as_str()
                .map_err(|e| decode_error(column, &e.to_string()))?;
            Decimal::from_str_exact(text).map_err(|e| e.to_string())
        }
    };
    parsed.map(Some).map_err(|m| decode_error(column, &m))
}

fn decode_error(column: &str, message: &str) -> DbError {
    DbError::Decode {
        column: column.to_owned(),
        message: message.to_owned(),
    }
}

const NUMERIC_NEG: u16 = 0x4000;
const NUMERIC_POS: u16 = 0x0000;

/// Postgres's binary `numeric` (`int16 ndigits, int16 weight, uint16 sign, uint16 dscale`, then
/// `ndigits` base-10000 digits) to a `Decimal` with exactly `dscale` fractional digits — what
/// asyncpg builds. NaN and the infinities (Postgres 14+) have no `Decimal` and are errors, as is
/// anything beyond 28 digits (never rounded).
///
/// # Errors
/// A message saying why the value cannot be a `Decimal`.
pub fn numeric_from_binary(bytes: &[u8]) -> Result<Decimal, String> {
    let word = |i: usize| -> Result<u16, String> {
        let hi = bytes.get(i).copied().ok_or("truncated numeric")?;
        let lo = bytes.get(i + 1).copied().ok_or("truncated numeric")?;
        Ok(u16::from_be_bytes([hi, lo]))
    };
    let ndigits = usize::from(word(0)?);
    let weight = i64::from(i16::from_be_bytes(word(2)?.to_be_bytes()));
    let sign = word(4)?;
    let dscale = usize::from(word(6)?);
    if sign != NUMERIC_POS && sign != NUMERIC_NEG {
        return Err("NaN or infinite numeric has no Decimal".to_owned());
    }
    let mut digits = Vec::with_capacity(ndigits);
    for k in 0..ndigits {
        digits.push(word(8 + 2 * k)?);
    }
    let digit_at = |position: i64| -> u16 {
        // The group whose place value is 10000^position.
        let index = weight - position;
        usize::try_from(index)
            .ok()
            .and_then(|i| digits.get(i).copied())
            .unwrap_or(0)
    };
    let mut integer = String::new();
    let mut position = weight;
    while position >= 0 {
        let group = digit_at(position);
        if integer.is_empty() {
            if group != 0 {
                integer.push_str(&group.to_string());
            }
        } else {
            integer.push_str(&format!("{group:04}"));
        }
        position -= 1;
    }
    if integer.is_empty() {
        integer.push('0');
    }
    let mut fraction = String::new();
    let mut position = -1_i64;
    while fraction.len() < dscale {
        fraction.push_str(&format!("{:04}", digit_at(position)));
        position -= 1;
    }
    fraction.truncate(dscale);
    let mut text = String::new();
    if sign == NUMERIC_NEG {
        text.push('-');
    }
    text.push_str(&integer);
    if dscale > 0 {
        text.push('.');
        text.push_str(&fraction);
    }
    Decimal::from_str_exact(&text).map_err(|e| format!("{e} (numeric beyond Decimal's 28 digits)"))
}

/// A non-null `timestamptz`, in UTC.
///
/// # Errors
/// [`DbError::Decode`].
pub fn timestamptz(row: &PgRow, column: &str) -> Result<DateTime<Utc>, DbError> {
    get(row, column)
}

/// A nullable `timestamptz`, in UTC.
///
/// # Errors
/// [`DbError::Decode`].
pub fn opt_timestamptz(row: &PgRow, column: &str) -> Result<Option<DateTime<Utc>>, DbError> {
    get(row, column)
}

/// A non-null `timestamp` (without time zone).
///
/// # Errors
/// [`DbError::Decode`].
pub fn timestamp(row: &PgRow, column: &str) -> Result<NaiveDateTime, DbError> {
    get(row, column)
}

/// A non-null `date`.
///
/// # Errors
/// [`DbError::Decode`].
pub fn date(row: &PgRow, column: &str) -> Result<NaiveDate, DbError> {
    get(row, column)
}

/// A nullable `date`.
///
/// # Errors
/// [`DbError::Decode`].
pub fn opt_date(row: &PgRow, column: &str) -> Result<Option<NaiveDate>, DbError> {
    get(row, column)
}

/// A non-null `jsonb` (or `json`), keys in stored order.
///
/// # Errors
/// [`DbError::Decode`].
pub fn jsonb(row: &PgRow, column: &str) -> Result<OrderedJson, DbError> {
    get::<Json<OrderedJson>>(row, column).map(|j| j.0)
}

/// A nullable `jsonb`. SQL NULL is `None`; a JSON `null` stored in the column is
/// `Some(OrderedJson::Null)` — SQLAlchemy's `JSONB` reads both as `None`, so a port that echoes
/// the column maps both to `null`.
///
/// # Errors
/// [`DbError::Decode`].
pub fn opt_jsonb(row: &PgRow, column: &str) -> Result<Option<OrderedJson>, DbError> {
    get::<Option<Json<OrderedJson>>>(row, column).map(|j| j.map(|j| j.0))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn wire(ndigits: u16, weight: i16, sign: u16, dscale: u16, digits: &[u16]) -> Vec<u8> {
        let mut out = Vec::new();
        for w in [
            ndigits,
            u16::from_be_bytes(weight.to_be_bytes()),
            sign,
            dscale,
        ] {
            out.extend_from_slice(&w.to_be_bytes());
        }
        for d in digits {
            out.extend_from_slice(&d.to_be_bytes());
        }
        out
    }

    #[test]
    fn the_scale_is_the_display_scale() {
        // 0::numeric(10,2): no digits at all, dscale 2.
        assert_eq!(
            numeric_from_binary(&wire(0, 0, 0, 2, &[]))
                .unwrap()
                .to_string(),
            "0.00"
        );
        // 12345.678: digits [1, 2345, 6780], weight 1, dscale 3.
        assert_eq!(
            numeric_from_binary(&wire(3, 1, 0, 3, &[1, 2345, 6780]))
                .unwrap()
                .to_string(),
            "12345.678"
        );
        // -0.0000001: digits [10], weight -2, dscale 7.
        assert_eq!(
            numeric_from_binary(&wire(1, -2, NUMERIC_NEG, 7, &[10]))
                .unwrap()
                .to_string(),
            "-0.0000001"
        );
        // 10000 (one group at weight 1, trailing zero group stripped by Postgres).
        assert_eq!(
            numeric_from_binary(&wire(1, 1, 0, 0, &[1]))
                .unwrap()
                .to_string(),
            "10000"
        );
    }

    #[test]
    fn nan_and_overflow_are_errors() {
        assert!(numeric_from_binary(&wire(0, 0, 0xC000, 0, &[])).is_err());
        assert!(numeric_from_binary(&wire(1, 10, 0, 0, &[1])).is_err());
        assert!(numeric_from_binary(&[0, 1]).is_err());
    }
}
