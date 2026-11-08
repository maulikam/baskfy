//! Reading a provider frame row by row — Python's `frame.iter_rows(named=True)` over the
//! fixed schemas (`DAILY_BARS_SCHEMA`, `BHAVCOPY_SCHEMA`).

use chrono::{Days, NaiveDate};
use polars::prelude::{AnyValue, Column, DataFrame};
use rust_decimal::Decimal;

use crate::error::WorkerError;

fn frame_error(message: String) -> WorkerError {
    WorkerError::python("ColumnNotFoundError", message)
}

/// One column of `frame`, by name.
pub(super) fn column<'a>(frame: &'a DataFrame, name: &str) -> Result<&'a Column, WorkerError> {
    frame
        .column(name)
        .map_err(|e| frame_error(format!("{name}: {e}")))
}

fn value_at<'a>(col: &'a Column, row: usize) -> Result<AnyValue<'a>, WorkerError> {
    col.get(row)
        .map_err(|e| WorkerError::python("OutOfBoundsError", e.to_string()))
}

/// A decimal cell (`Decimal | None`), scale kept.
pub(super) fn decimal(col: &Column, row: usize) -> Result<Option<Decimal>, WorkerError> {
    match value_at(col, row)? {
        AnyValue::Null => Ok(None),
        AnyValue::Decimal(mantissa, _, scale) => {
            let scale = u32::try_from(scale)
                .map_err(|_| WorkerError::python("ValueError", "negative decimal scale"))?;
            Decimal::try_from_i128_with_scale(mantissa, scale)
                .map(Some)
                .map_err(|e| WorkerError::python("ValueError", e.to_string()))
        }
        other => Err(WorkerError::python(
            "TypeError",
            format!("expected a decimal cell, got {other:?}"),
        )),
    }
}

/// A string cell.
pub(super) fn string(col: &Column, row: usize) -> Result<Option<String>, WorkerError> {
    match value_at(col, row)? {
        AnyValue::Null => Ok(None),
        AnyValue::String(s) => Ok(Some(s.to_owned())),
        AnyValue::StringOwned(s) => Ok(Some(s.to_string())),
        other => Err(WorkerError::python(
            "TypeError",
            format!("expected a string cell, got {other:?}"),
        )),
    }
}

/// An integer cell.
pub(super) fn int(col: &Column, row: usize) -> Result<Option<i64>, WorkerError> {
    match value_at(col, row)? {
        AnyValue::Null => Ok(None),
        AnyValue::Int64(v) => Ok(Some(v)),
        AnyValue::Int32(v) => Ok(Some(i64::from(v))),
        other => Err(WorkerError::python(
            "TypeError",
            format!("expected an integer cell, got {other:?}"),
        )),
    }
}

/// A date cell (days since the Unix epoch).
pub(super) fn date(col: &Column, row: usize) -> Result<Option<NaiveDate>, WorkerError> {
    match value_at(col, row)? {
        AnyValue::Null => Ok(None),
        AnyValue::Date(days) => {
            let epoch = NaiveDate::from_ymd_opt(1970, 1, 1)
                .ok_or_else(|| WorkerError::python("ValueError", "epoch"))?;
            let shifted = if days >= 0 {
                epoch.checked_add_days(Days::new(u64::from(days.unsigned_abs())))
            } else {
                epoch.checked_sub_days(Days::new(u64::from(days.unsigned_abs())))
            };
            shifted
                .map(Some)
                .ok_or_else(|| WorkerError::python("OverflowError", "date out of range"))
        }
        other => Err(WorkerError::python(
            "TypeError",
            format!("expected a date cell, got {other:?}"),
        )),
    }
}
