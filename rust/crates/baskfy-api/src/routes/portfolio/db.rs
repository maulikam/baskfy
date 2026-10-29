//! Row reading for this lane: every failure is Python's unhandled `SQLAlchemyError` (a 500).

use baskfy_db::{DbError, OrderedJson, row};
use chrono::{DateTime, NaiveDate, Utc};
use sqlx::Row;
use sqlx::postgres::PgRow;

use baskfy_core::decimal::WideDecimal;

use crate::problems::Problem;

/// A database failure as the API answers it.
pub(crate) fn fail(e: impl Into<DbError>) -> Problem {
    crate::db_failure(&e.into())
}

/// A typed column.
pub(crate) fn get<'r, T>(r: &'r PgRow, column: &str) -> Result<T, Problem>
where
    T: sqlx::Decode<'r, sqlx::Postgres> + sqlx::Type<sqlx::Postgres>,
{
    r.try_get::<T, _>(column).map_err(fail)
}

/// A non-null `numeric`, scale kept, as Python's `Decimal`.
pub(crate) fn dec(r: &PgRow, column: &str) -> Result<WideDecimal, Problem> {
    row::decimal(r, column).map(WideDecimal::from).map_err(fail)
}

/// A nullable `numeric`.
pub(crate) fn opt_dec(r: &PgRow, column: &str) -> Result<Option<WideDecimal>, Problem> {
    row::opt_decimal(r, column)
        .map(|v| v.map(WideDecimal::from))
        .map_err(fail)
}

/// A non-null `date`.
pub(crate) fn date(r: &PgRow, column: &str) -> Result<NaiveDate, Problem> {
    row::date(r, column).map_err(fail)
}

/// A nullable `date`.
pub(crate) fn opt_date(r: &PgRow, column: &str) -> Result<Option<NaiveDate>, Problem> {
    row::opt_date(r, column).map_err(fail)
}

/// A non-null `timestamptz`.
pub(crate) fn ts(r: &PgRow, column: &str) -> Result<DateTime<Utc>, Problem> {
    row::timestamptz(r, column).map_err(fail)
}

/// A nullable `timestamptz`.
pub(crate) fn opt_ts(r: &PgRow, column: &str) -> Result<Option<DateTime<Utc>>, Problem> {
    row::opt_timestamptz(r, column).map_err(fail)
}

/// A `jsonb` column as SQLAlchemy reads it: SQL NULL and JSON `null` are both `None`.
pub(crate) fn opt_json(r: &PgRow, column: &str) -> Result<Option<OrderedJson>, Problem> {
    Ok(row::opt_jsonb(r, column)
        .map_err(fail)?
        .filter(|v| *v != OrderedJson::Null))
}

/// A non-null `jsonb` column.
pub(crate) fn json(r: &PgRow, column: &str) -> Result<OrderedJson, Problem> {
    row::jsonb(r, column).map_err(fail)
}

/// A non-null `numeric` as a [`super::pydec::Dec`].
pub(crate) fn pdec(r: &PgRow, column: &str) -> Result<super::pydec::Dec, Problem> {
    row::decimal(r, column)
        .map(super::pydec::Dec::from_decimal)
        .map_err(fail)
}

/// A nullable `numeric` as a [`super::pydec::Dec`].
pub(crate) fn opt_pdec(r: &PgRow, column: &str) -> Result<Option<super::pydec::Dec>, Problem> {
    row::opt_decimal(r, column)
        .map(|v| v.map(super::pydec::Dec::from_decimal))
        .map_err(fail)
}
