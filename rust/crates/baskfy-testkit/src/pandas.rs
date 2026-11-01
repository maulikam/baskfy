//! Pandas frames and series in the golden encoding (the ranking goldens, DECISIONS-RS R1.5).
//!
//! Three frame shapes occur, all written by `tools/parity/golden_ranking.py`:
//!
//! | shape | where | dtypes |
//! |---|---|---|
//! | `{"columns", "rows"}` | inputs | inferred, as `pandas.DataFrame(rows, columns=…)` would |
//! | `{"columns", "date_columns", "rows"}` | inputs | inferred; the listed columns are `date`s |
//! | `{"columns", "dtypes", "index", "rows"}` | outputs | pandas names (`float64`, `int64`, `Int64`, `bool`, `str`, `object`) |
//!
//! and one series shape, `{"dtype", "index", "values"}`. Every pandas missing marker
//! (`None`, `pd.NA`, `NaN`, `NaT`) is written `null`, and decodes to a polars null.
//!
//! **Dtype mapping** (pandas → polars): `float64` → `Float64`; `int64` and the nullable
//! `Int64` → `Int64`; `bool` → `Boolean`; `str` → `String`; `object` and a column with no
//! stated dtype are inferred from their non-null values — all booleans → `Boolean`, all
//! strings → `String`, all integers → `Int64`, any float → `Float64`, nothing but nulls →
//! `Null`. In an *inferred input* column (no `dtypes`), integers beside a `null` become
//! `Float64`, as pandas turns them into floats with `NaN`. A `date_columns` column is `Date`.
//! A column mixing kinds (a bool beside a string) is refused rather than guessed.
//!
//! Pandas carries information a polars frame has no slot for — the index, and `object`
//! versus `bool`/`str` — so [`decode_pandas_frame`] returns them beside the frame in a
//! [`PandasFrame`], and [`encode_pandas_frame`] takes them back to rebuild the golden shape.

use polars::prelude::{DataFrame, DataType, IntoColumn, NamedFrom, Series};
use serde_json::{Map, Value};

use crate::error::TestkitError;
use crate::frame::{cell, series_from};

/// A pandas frame decoded from a golden: the data as polars, plus what pandas adds.
#[derive(Debug, Clone)]
pub struct PandasFrame {
    /// The columns, in the golden's order, with the mapped polars dtypes.
    pub frame: DataFrame,
    /// `(column, pandas dtype name)` in column order — the golden's `dtypes` when it has them,
    /// otherwise [`pandas_dtype_name`] of the inferred polars dtype.
    pub dtypes: Vec<(String, String)>,
    /// The golden's `index` (row labels), or `0..n` for an input shape that has none.
    pub index: Vec<Value>,
}

/// True for any of the three pandas frame shapes (and never for a polars frame, which is
/// exactly `{columns, dtypes, rows}`).
pub fn is_pandas_frame(v: &Value) -> bool {
    v.as_object().is_some_and(|m| {
        let has = |k: &str| m.contains_key(k);
        has("columns")
            && has("rows")
            && match m.len() {
                2 => true,
                3 => has("date_columns"),
                4 => has("dtypes") && has("index"),
                _ => false,
            }
    })
}

/// True for the pandas series shape `{dtype, index, values}`.
pub fn is_pandas_series(v: &Value) -> bool {
    v.as_object().is_some_and(|m| {
        m.len() == 3
            && m.contains_key("dtype")
            && m.contains_key("index")
            && m.contains_key("values")
    })
}

/// The pandas dtype name golden_ranking.py writes for a column of this polars dtype when
/// nothing more specific is known: `Float64` → `float64`, `Int64` → `int64`, `Boolean` →
/// `bool`, `String` → `str`, anything else (dates, nulls) → `object`.
pub fn pandas_dtype_name(dt: &DataType) -> &'static str {
    match dt {
        DataType::Float64 | DataType::Float32 => "float64",
        DataType::Int64 | DataType::Int32 | DataType::UInt32 | DataType::UInt64 => "int64",
        DataType::Boolean => "bool",
        DataType::String => "str",
        _ => "object",
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Bool,
    Str,
    Int,
    Float,
}

/// The polars dtype name (as `series_from` reads it) of an `object` / unstated column.
fn infer(name: &str, values: &[Value], inputs_rule: bool) -> Result<&'static str, TestkitError> {
    let mut kind: Option<Kind> = None;
    let mut has_null = false;
    for (i, v) in values.iter().enumerate() {
        let k = match v {
            Value::Null => {
                has_null = true;
                continue;
            }
            Value::Bool(_) => Kind::Bool,
            Value::String(_) => Kind::Str,
            Value::Number(n) if n.is_i64() || n.is_u64() => Kind::Int,
            Value::Number(_) => Kind::Float,
            other => {
                return Err(TestkitError::decode(
                    format!("{name}[{i}]"),
                    format!("a pandas cell cannot be {other}"),
                ));
            }
        };
        kind = Some(match (kind, k) {
            (None, k) => k,
            (Some(a), b) if a == b => a,
            (Some(Kind::Int | Kind::Float), Kind::Int | Kind::Float) => Kind::Float,
            (Some(_), _) => {
                return Err(TestkitError::decode(
                    name,
                    "an object column mixes kinds (bool/str/number); decode it by hand",
                ));
            }
        });
    }
    Ok(match kind {
        None => "Null",
        Some(Kind::Bool) => "Boolean",
        Some(Kind::Str) => "String",
        Some(Kind::Int) if inputs_rule && has_null => "Float64",
        Some(Kind::Int) => "Int64",
        Some(Kind::Float) => "Float64",
    })
}

fn polars_name_for(pandas: &str) -> Option<&'static str> {
    match pandas {
        "float64" => Some("Float64"),
        "int64" | "Int64" => Some("Int64"),
        "bool" | "boolean" => Some("Boolean"),
        "str" | "string" => Some("String"),
        _ => None,
    }
}

fn columns_of(m: &Map<String, Value>) -> Result<Vec<String>, TestkitError> {
    m.get("columns")
        .and_then(Value::as_array)
        .ok_or_else(|| TestkitError::decode("columns", "expected a list"))?
        .iter()
        .map(|c| {
            c.as_str()
                .map(str::to_owned)
                .ok_or_else(|| TestkitError::decode("columns", format!("not a string: {c}")))
        })
        .collect()
}

fn by_column(
    m: &Map<String, Value>,
    width: usize,
) -> Result<(usize, Vec<Vec<Value>>), TestkitError> {
    let rows = m
        .get("rows")
        .and_then(Value::as_array)
        .ok_or_else(|| TestkitError::decode("rows", "expected a list"))?;
    let mut out: Vec<Vec<Value>> = vec![Vec::with_capacity(rows.len()); width];
    for (r, row) in rows.iter().enumerate() {
        let cells = row.as_array().filter(|c| c.len() == width).ok_or_else(|| {
            TestkitError::decode(format!("rows[{r}]"), format!("expected {width} cells"))
        })?;
        for (slot, cell) in out.iter_mut().zip(cells) {
            slot.push(cell.clone());
        }
    }
    Ok((rows.len(), out))
}

/// Decode any of the three pandas frame shapes (module docs) into a [`PandasFrame`].
pub fn decode_pandas_frame(v: &Value) -> Result<PandasFrame, TestkitError> {
    if !is_pandas_frame(v) {
        return Err(TestkitError::decode("frame", "not a pandas frame shape"));
    }
    let m = v
        .as_object()
        .ok_or_else(|| TestkitError::decode("frame", "expected an object"))?;
    let columns = columns_of(m)?;
    let (height, values) = by_column(m, columns.len())?;
    let stated = m.get("dtypes").and_then(Value::as_object);
    let dates: Vec<&str> = match m.get("date_columns") {
        None => Vec::new(),
        Some(d) => d
            .as_array()
            .ok_or_else(|| TestkitError::decode("date_columns", "expected a list"))?
            .iter()
            .map(|x| {
                x.as_str().ok_or_else(|| {
                    TestkitError::decode("date_columns", format!("not a string: {x}"))
                })
            })
            .collect::<Result<_, _>>()?,
    };
    let mut out = Vec::with_capacity(columns.len());
    let mut dtypes = Vec::with_capacity(columns.len());
    for (name, vals) in columns.iter().zip(&values) {
        let pandas = match stated {
            Some(s) => Some(
                s.get(name)
                    .and_then(Value::as_str)
                    .ok_or_else(|| TestkitError::decode(name.as_str(), "no dtype"))?
                    .to_owned(),
            ),
            None => None,
        };
        let polars_name = if dates.contains(&name.as_str()) {
            "Date"
        } else {
            match pandas.as_deref() {
                Some(p) => match polars_name_for(p) {
                    Some(n) => n,
                    None if p == "object" => infer(name, vals, false)?,
                    None => return Err(TestkitError::UnsupportedDtype(format!("pandas {p}"))),
                },
                None => infer(name, vals, true)?,
            }
        };
        let series = series_from(name, polars_name, vals)?;
        dtypes.push((
            name.clone(),
            pandas.unwrap_or_else(|| {
                if polars_name == "Date" {
                    "object".to_owned()
                } else {
                    pandas_dtype_name(series.dtype()).to_owned()
                }
            }),
        ));
        out.push(series.into_column());
    }
    let index = match m.get("index") {
        Some(i) => i
            .as_array()
            .ok_or_else(|| TestkitError::decode("index", "expected a list"))?
            .clone(),
        None => (0..height).map(Value::from).collect(),
    };
    if index.len() != height {
        return Err(TestkitError::decode(
            "index",
            format!("{} labels for {height} rows", index.len()),
        ));
    }
    Ok(PandasFrame {
        frame: DataFrame::new(height, out)?,
        dtypes,
        index,
    })
}

/// Encode a polars frame in the pandas *output* shape `{columns, dtypes, index, rows}`.
///
/// `dtypes` gives the pandas name of a column (`object`, `Int64`, …) when it is not what
/// [`pandas_dtype_name`] would say; columns it does not mention use that default. Dates are
/// written ISO, NaN as `null` — exactly golden_ranking.py's `_pd_frame`.
pub fn encode_pandas_frame(
    df: &DataFrame,
    index: &[Value],
    dtypes: &[(String, String)],
) -> Result<Value, TestkitError> {
    if index.len() != df.height() {
        return Err(TestkitError::Encode(format!(
            "{} index labels for {} rows",
            index.len(),
            df.height()
        )));
    }
    let cols = df.columns();
    let mut names = Vec::with_capacity(cols.len());
    let mut types = Map::new();
    for c in cols {
        let name = c.name().to_string();
        let pandas = dtypes.iter().find(|(n, _)| *n == name).map_or_else(
            || pandas_dtype_name(c.dtype()).to_owned(),
            |(_, p)| p.clone(),
        );
        types.insert(name.clone(), Value::String(pandas));
        names.push(Value::String(name));
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
    m.insert("dtypes".into(), Value::Object(types));
    m.insert("index".into(), Value::Array(index.to_vec()));
    m.insert("rows".into(), Value::Array(rows));
    Ok(Value::Object(m))
}

/// Decode a pandas series `{dtype, index, values}` into a polars `Series` named `name` and its
/// index labels. The dtype maps as for frames.
pub fn decode_pandas_series(name: &str, v: &Value) -> Result<(Series, Vec<Value>), TestkitError> {
    if !is_pandas_series(v) {
        return Err(TestkitError::decode("series", "not a pandas series shape"));
    }
    let m = v
        .as_object()
        .ok_or_else(|| TestkitError::decode("series", "expected an object"))?;
    let pandas = m
        .get("dtype")
        .and_then(Value::as_str)
        .ok_or_else(|| TestkitError::decode("dtype", "expected a string"))?;
    let values = m
        .get("values")
        .and_then(Value::as_array)
        .ok_or_else(|| TestkitError::decode("values", "expected a list"))?;
    let index = m
        .get("index")
        .and_then(Value::as_array)
        .ok_or_else(|| TestkitError::decode("index", "expected a list"))?
        .clone();
    if index.len() != values.len() {
        return Err(TestkitError::decode("index", "length differs from values"));
    }
    let polars_name = match polars_name_for(pandas) {
        Some(n) => n,
        None if pandas == "object" => infer(name, values, false)?,
        None => return Err(TestkitError::UnsupportedDtype(format!("pandas {pandas}"))),
    };
    Ok((series_from(name, polars_name, values)?, index))
}

/// Encode a polars `Series` in the pandas series shape `{dtype, index, values}`; `dtype` is
/// the pandas name to write (`None`: [`pandas_dtype_name`] of the series' dtype).
pub fn encode_pandas_series(
    s: &Series,
    index: &[Value],
    dtype: Option<&str>,
) -> Result<Value, TestkitError> {
    if index.len() != s.len() {
        return Err(TestkitError::Encode(format!(
            "{} index labels for {} values",
            index.len(),
            s.len()
        )));
    }
    let mut values = Vec::with_capacity(s.len());
    for i in 0..s.len() {
        values.push(cell(s.get(i)?, s.name())?);
    }
    let mut m = Map::new();
    m.insert(
        "dtype".into(),
        Value::String(
            dtype
                .unwrap_or_else(|| pandas_dtype_name(s.dtype()))
                .to_owned(),
        ),
    );
    m.insert("index".into(), Value::Array(index.to_vec()));
    m.insert("values".into(), Value::Array(values));
    Ok(Value::Object(m))
}

/// A float series from pandas-encoded values (`null` → null), the shape golden_ranking.py's
/// `_float_series` builds for `percentile`/`transform` inputs.
pub fn float_series(name: &str, values: &[Value]) -> Result<Series, TestkitError> {
    let floats = values
        .iter()
        .enumerate()
        .map(|(i, v)| match v {
            Value::Null => Ok(None),
            Value::Number(n) => n
                .as_f64()
                .map(Some)
                .ok_or_else(|| TestkitError::decode(format!("{name}[{i}]"), "not a float")),
            other => Err(TestkitError::decode(
                format!("{name}[{i}]"),
                format!("expected a number, got {other}"),
            )),
        })
        .collect::<Result<Vec<Option<f64>>, _>>()?;
    Ok(Series::new(name.into(), floats))
}
