//! Polars frames and series in the golden encoding.
//!
//! `{"columns": [...], "dtypes": {col: "Float64" | "Int64" | ...}, "rows": [[...], ...]}` —
//! Python polars dtype names, the frame's own column order, row-major values, `null` for a
//! polars null *and* for a float NaN (golden.py collapses both). Decoding therefore turns every
//! `null` into a polars null; a port whose Python produces NaN must still encode it as `null`,
//! which [`encode_frame`] does.

use chrono::NaiveDate;
use polars::prelude::{AnyValue, Column, DataFrame, DataType, IntoColumn, NamedFrom, Series};
use serde_json::{Map, Value};

use crate::error::TestkitError;
use crate::scalar;

/// True when `v` has exactly the frame shape `{columns, dtypes, rows}`.
pub(crate) fn is_frame(v: &Value) -> bool {
    v.as_object().is_some_and(|m| {
        m.len() == 3
            && m.contains_key("columns")
            && m.contains_key("dtypes")
            && m.contains_key("rows")
    })
}

/// True when `v` has exactly the series shape `{dtype, values}`.
pub(crate) fn is_series(v: &Value) -> bool {
    v.as_object()
        .is_some_and(|m| m.len() == 2 && m.contains_key("dtype") && m.contains_key("values"))
}

fn epoch() -> Result<NaiveDate, TestkitError> {
    NaiveDate::from_ymd_opt(1970, 1, 1).ok_or_else(|| TestkitError::decode("epoch", "1970-01-01"))
}

fn python_dtype_name(dt: &DataType) -> Result<&'static str, TestkitError> {
    Ok(match dt {
        DataType::Float64 => "Float64",
        DataType::Float32 => "Float32",
        DataType::Int64 => "Int64",
        DataType::Int32 => "Int32",
        DataType::UInt64 => "UInt64",
        DataType::UInt32 => "UInt32",
        DataType::Boolean => "Boolean",
        DataType::String => "String",
        DataType::Date => "Date",
        DataType::Null => "Null",
        other => return Err(TestkitError::UnsupportedDtype(other.to_string())),
    })
}

fn num<T>(
    col: &str,
    values: &[Value],
    get: impl Fn(&serde_json::Number) -> Option<T>,
) -> Result<Vec<Option<T>>, TestkitError> {
    values
        .iter()
        .enumerate()
        .map(|(i, v)| match v {
            Value::Null => Ok(None),
            Value::Number(n) => get(n).map(Some).ok_or_else(|| {
                TestkitError::decode(format!("{col}[{i}]"), format!("{n} out of range"))
            }),
            other => Err(TestkitError::decode(
                format!("{col}[{i}]"),
                format!("expected a number, got {other}"),
            )),
        })
        .collect()
}

/// Build one series named `name` from golden values of Python dtype `dtype`.
fn series_from(name: &str, dtype: &str, values: &[Value]) -> Result<Series, TestkitError> {
    let n = name.into();
    Ok(match dtype {
        "Float64" => Series::new(n, num(name, values, serde_json::Number::as_f64)?),
        // f64 -> f32 narrowing is the dtype's own precision, the same as Python's cast.
        #[allow(clippy::cast_possible_truncation)]
        "Float32" => Series::new(n, num(name, values, |x| x.as_f64().map(|f| f as f32))?),
        "Int64" => Series::new(n, num(name, values, serde_json::Number::as_i64)?),
        "Int32" => Series::new(
            n,
            num(name, values, |x| {
                x.as_i64().and_then(|i| i32::try_from(i).ok())
            })?,
        ),
        "UInt64" => Series::new(n, num(name, values, serde_json::Number::as_u64)?),
        "UInt32" => Series::new(
            n,
            num(name, values, |x| {
                x.as_u64().and_then(|i| u32::try_from(i).ok())
            })?,
        ),
        "Boolean" => Series::new(
            n,
            values
                .iter()
                .enumerate()
                .map(|(i, v)| match v {
                    Value::Null => Ok(None),
                    Value::Bool(b) => Ok(Some(*b)),
                    o => Err(TestkitError::decode(
                        format!("{name}[{i}]"),
                        format!("not a bool: {o}"),
                    )),
                })
                .collect::<Result<Vec<_>, _>>()?,
        ),
        "String" => Series::new(
            n,
            values
                .iter()
                .enumerate()
                .map(|(i, v)| match v {
                    Value::Null => Ok(None),
                    Value::String(s) => Ok(Some(s.clone())),
                    o => Err(TestkitError::decode(
                        format!("{name}[{i}]"),
                        format!("not a string: {o}"),
                    )),
                })
                .collect::<Result<Vec<_>, _>>()?,
        ),
        "Date" => {
            let epoch = epoch()?;
            let days = values
                .iter()
                .enumerate()
                .map(|(i, v)| match v {
                    Value::Null => Ok(None),
                    other => {
                        let d = scalar::date(other).map_err(|e| {
                            TestkitError::decode(format!("{name}[{i}]"), e.to_string())
                        })?;
                        i32::try_from(d.signed_duration_since(epoch).num_days())
                            .map(Some)
                            .map_err(|e| {
                                TestkitError::decode(format!("{name}[{i}]"), e.to_string())
                            })
                    }
                })
                .collect::<Result<Vec<Option<i32>>, _>>()?;
            Series::new(n, days).cast(&DataType::Date)?
        }
        "Null" => Series::new_null(n, values.len()),
        other => return Err(TestkitError::UnsupportedDtype(other.to_owned())),
    })
}

fn field<'a>(m: &'a Map<String, Value>, key: &str) -> Result<&'a Value, TestkitError> {
    m.get(key)
        .ok_or_else(|| TestkitError::decode(key, "missing from frame/series object"))
}

/// Decode a golden `{"columns", "dtypes", "rows"}` object into a `DataFrame` with the dumped
/// dtypes (Float64, Float32, Int64, Int32, UInt64, UInt32, Boolean, String, Date, Null).
pub fn decode_frame(v: &Value) -> Result<DataFrame, TestkitError> {
    let m = v
        .as_object()
        .ok_or_else(|| TestkitError::decode("frame", "expected an object"))?;
    let columns = field(m, "columns")?
        .as_array()
        .ok_or_else(|| TestkitError::decode("columns", "expected a list"))?;
    let dtypes = field(m, "dtypes")?
        .as_object()
        .ok_or_else(|| TestkitError::decode("dtypes", "expected an object"))?;
    let rows = field(m, "rows")?
        .as_array()
        .ok_or_else(|| TestkitError::decode("rows", "expected a list"))?;
    let width = columns.len();
    let mut by_col: Vec<Vec<Value>> = vec![Vec::with_capacity(rows.len()); width];
    for (r, row) in rows.iter().enumerate() {
        let cells = row.as_array().filter(|c| c.len() == width).ok_or_else(|| {
            TestkitError::decode(format!("rows[{r}]"), format!("expected {width} cells"))
        })?;
        for (slot, cell) in by_col.iter_mut().zip(cells) {
            slot.push(cell.clone());
        }
    }
    let mut out: Vec<Column> = Vec::with_capacity(width);
    for (c, values) in columns.iter().zip(&by_col) {
        let name = c
            .as_str()
            .ok_or_else(|| TestkitError::decode("columns", format!("not a string: {c}")))?;
        let dtype = dtypes
            .get(name)
            .and_then(Value::as_str)
            .ok_or_else(|| TestkitError::decode(name, "no dtype"))?;
        out.push(series_from(name, dtype, values)?.into_column());
    }
    Ok(DataFrame::new(rows.len(), out)?)
}

/// Decode a golden `{"dtype", "values"}` object into a `Series` named `name`.
pub fn decode_series(name: &str, v: &Value) -> Result<Series, TestkitError> {
    let m = v
        .as_object()
        .ok_or_else(|| TestkitError::decode("series", "expected an object"))?;
    let dtype = field(m, "dtype")?
        .as_str()
        .ok_or_else(|| TestkitError::decode("dtype", "expected a string"))?;
    let values = field(m, "values")?
        .as_array()
        .ok_or_else(|| TestkitError::decode("values", "expected a list"))?;
    series_from(name, dtype, values)
}

fn float(f: f64) -> Value {
    serde_json::Number::from_f64(f).map_or(Value::Null, Value::Number)
}

fn cell(av: AnyValue<'_>, at: &str) -> Result<Value, TestkitError> {
    Ok(match av {
        AnyValue::Null => Value::Null,
        AnyValue::Boolean(b) => Value::Bool(b),
        AnyValue::String(s) => Value::String(s.to_owned()),
        AnyValue::StringOwned(s) => Value::String(s.to_string()),
        AnyValue::Int8(i) => Value::from(i),
        AnyValue::Int16(i) => Value::from(i),
        AnyValue::Int32(i) => Value::from(i),
        AnyValue::Int64(i) => Value::from(i),
        AnyValue::UInt8(i) => Value::from(i),
        AnyValue::UInt16(i) => Value::from(i),
        AnyValue::UInt32(i) => Value::from(i),
        AnyValue::UInt64(i) => Value::from(i),
        AnyValue::Float32(f) => float(f64::from(f)),
        AnyValue::Float64(f) => float(f),
        AnyValue::Date(days) => {
            let d = epoch()?
                .checked_add_signed(chrono::TimeDelta::days(i64::from(days)))
                .ok_or_else(|| TestkitError::Encode(format!("{at}: date out of range")))?;
            Value::String(d.format("%Y-%m-%d").to_string())
        }
        other => {
            return Err(TestkitError::Encode(format!(
                "{at}: unsupported value {other:?}"
            )));
        }
    })
}

/// Encode a `DataFrame` exactly as golden.py encodes a polars frame, so it can be compared
/// with [`crate::compare`] (dtype names, column order, NaN as `null`, dates ISO).
pub fn encode_frame(df: &DataFrame) -> Result<Value, TestkitError> {
    let cols = df.columns();
    let mut dtypes = Map::new();
    let mut names = Vec::with_capacity(cols.len());
    for c in cols {
        dtypes.insert(
            c.name().to_string(),
            Value::String(python_dtype_name(c.dtype())?.to_owned()),
        );
        names.push(Value::String(c.name().to_string()));
    }
    let mut rows = Vec::with_capacity(df.height());
    for r in 0..df.height() {
        let mut row = Vec::with_capacity(cols.len());
        for c in cols {
            row.push(cell(c.get(r)?, c.name())?);
        }
        rows.push(Value::Array(row));
    }
    let mut m = Map::new();
    m.insert("columns".into(), Value::Array(names));
    m.insert("dtypes".into(), Value::Object(dtypes));
    m.insert("rows".into(), Value::Array(rows));
    Ok(Value::Object(m))
}

/// Encode a `Series` as golden.py encodes a polars series: `{"dtype", "values"}`.
pub fn encode_series(s: &Series) -> Result<Value, TestkitError> {
    let mut values = Vec::with_capacity(s.len());
    for i in 0..s.len() {
        values.push(cell(s.get(i)?, s.name())?);
    }
    let mut m = Map::new();
    m.insert(
        "dtype".into(),
        Value::String(python_dtype_name(s.dtype())?.to_owned()),
    );
    m.insert("values".into(), Value::Array(values));
    Ok(Value::Object(m))
}
