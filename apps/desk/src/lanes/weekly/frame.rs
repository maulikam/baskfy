//! A scan as `pd.read_csv(path, encoding="utf-8-sig")` hands it to the scorer: named columns,
//! each `int64`, `float64`, `bool` or `object` by pandas' C-parser inference, missing cells by
//! pandas' default NA strings. Only what the weekly book reads is modelled; everything is graded
//! against `fixtures/weekly/scans.json` (dtype and every value, per column).

use std::collections::HashMap;

/// pandas' default `na_values` (`pandas._libs.parsers.STR_NA_VALUES`).
pub const NA_VALUES: [&str; 18] = [
    "", "#N/A", "#N/A N/A", "#NA", "-1.#IND", "-1.#QNAN", "-NaN", "-nan", "1.#IND", "1.#QNAN",
    "<NA>", "N/A", "NA", "NULL", "NaN", "None", "n/a", "nan",
];

fn is_na(s: &str) -> bool {
    s == "null" || NA_VALUES.contains(&s)
}

/// One column.
#[derive(Debug, Clone, PartialEq)]
pub enum Column {
    /// `int64` (no missing cells).
    Int(Vec<i64>),
    /// `float64` (missing = NaN).
    Float(Vec<f64>),
    /// `bool` (no missing cells).
    Bool(Vec<bool>),
    /// `object`: strings, missing = `None` (NaN in pandas).
    Str(Vec<Option<String>>),
}

impl Column {
    /// Number of rows.
    #[must_use]
    pub fn len(&self) -> usize {
        match self {
            Self::Int(v) => v.len(),
            Self::Float(v) => v.len(),
            Self::Bool(v) => v.len(),
            Self::Str(v) => v.len(),
        }
    }

    /// Is it empty?
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// The pandas dtype name.
    #[must_use]
    pub const fn dtype(&self) -> &'static str {
        match self {
            Self::Int(_) => "int64",
            Self::Float(_) => "float64",
            Self::Bool(_) => "bool",
            Self::Str(_) => "object",
        }
    }

    /// The values as float64 (`astype(float)` semantics for numbers and bools); `None` for an
    /// object column (pandas would compare strings with numbers and raise).
    #[must_use]
    #[allow(clippy::cast_precision_loss)]
    pub fn as_f64(&self) -> Option<Vec<f64>> {
        match self {
            Self::Int(v) => Some(v.iter().map(|x| *x as f64).collect()),
            Self::Float(v) => Some(v.clone()),
            Self::Bool(v) => Some(v.iter().map(|b| if *b { 1.0 } else { 0.0 }).collect()),
            Self::Str(v) if v.iter().all(Option::is_none) => Some(vec![f64::NAN; v.len()]),
            Self::Str(_) => None,
        }
    }

    /// Is cell `i` missing (`isna()`)?
    #[must_use]
    pub fn is_na(&self, i: usize) -> bool {
        match self {
            Self::Float(v) => v.get(i).is_some_and(|x| x.is_nan()),
            Self::Str(v) => v.get(i).is_some_and(Option::is_none),
            _ => false,
        }
    }

    /// `str(cell)` as pandas' `astype(str)` spells it (`nan` for a missing cell).
    #[must_use]
    pub fn py_str(&self, i: usize) -> String {
        match self {
            Self::Int(v) => v.get(i).map(ToString::to_string).unwrap_or_default(),
            Self::Float(v) => v
                .get(i)
                .map(|x| baskfy_core::py::float_repr(*x))
                .unwrap_or_default(),
            Self::Bool(v) => v
                .get(i)
                .map(|b| if *b { "True" } else { "False" }.to_owned())
                .unwrap_or_default(),
            Self::Str(v) => v
                .get(i)
                .and_then(Clone::clone)
                .unwrap_or_else(|| "nan".to_owned()),
        }
    }

    /// The cell as a string if it is one (for `symbol`, `series`, `date`).
    #[must_use]
    pub fn str_at(&self, i: usize) -> Option<String> {
        match self {
            Self::Str(v) => v.get(i).cloned().flatten(),
            _ => Some(self.py_str(i)),
        }
    }

    fn take(&self, rows: &[usize]) -> Self {
        match self {
            Self::Int(v) => Self::Int(rows.iter().map(|&i| v[i]).collect()),
            Self::Float(v) => Self::Float(rows.iter().map(|&i| v[i]).collect()),
            Self::Bool(v) => Self::Bool(rows.iter().map(|&i| v[i]).collect()),
            Self::Str(v) => Self::Str(rows.iter().map(|&i| v[i].clone()).collect()),
        }
    }
}

/// A frame: column names in file order, and the columns.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Frame {
    /// Column names, in order.
    pub names: Vec<String>,
    /// Columns by name.
    pub columns: HashMap<String, Column>,
    /// Row count.
    pub nrows: usize,
}

impl Frame {
    /// `name in df.columns`.
    #[must_use]
    pub fn has(&self, name: &str) -> bool {
        self.columns.contains_key(name)
    }

    /// `df[name]`.
    #[must_use]
    pub fn col(&self, name: &str) -> Option<&Column> {
        self.columns.get(name)
    }

    /// Add or replace a column (a new name goes last, as pandas' `df[name] = ...`).
    pub fn set(&mut self, name: &str, column: Column) {
        if !self.columns.contains_key(name) {
            self.names.push(name.to_owned());
        }
        self.nrows = column.len();
        self.columns.insert(name.to_owned(), column);
    }

    /// `df.iloc[rows]` (with a fresh index).
    #[must_use]
    pub fn take(&self, rows: &[usize]) -> Self {
        Self {
            names: self.names.clone(),
            columns: self
                .columns
                .iter()
                .map(|(k, c)| (k.clone(), c.take(rows)))
                .collect(),
            nrows: rows.len(),
        }
    }

    /// Numeric column as float64; `None` when absent or not numeric.
    #[must_use]
    pub fn f64s(&self, name: &str) -> Option<Vec<f64>> {
        self.col(name).and_then(Column::as_f64)
    }

    /// String column (`str(cell)`, `None` for a missing object cell).
    #[must_use]
    pub fn strs(&self, name: &str) -> Option<Vec<Option<String>>> {
        self.col(name)
            .map(|c| (0..self.nrows).map(|i| c.str_at(i)).collect())
    }
}

/// Why a CSV could not be read.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct CsvError(pub String);

/// Split CSV text into records (RFC 4180 quoting with doubled quotes; `\n`, `\r\n` or `\r`
/// row ends; blank lines skipped, as pandas' `skip_blank_lines=True`).
fn records(text: &str) -> Vec<Vec<String>> {
    let mut rows: Vec<Vec<String>> = Vec::new();
    let mut row: Vec<String> = Vec::new();
    let mut field = String::new();
    let mut chars = text.chars().peekable();
    let mut in_quotes = false;
    let mut field_started = false;
    let mut row_has_content = false;
    while let Some(c) = chars.next() {
        if in_quotes {
            if c == '"' {
                if chars.peek() == Some(&'"') {
                    field.push('"');
                    chars.next();
                } else {
                    in_quotes = false;
                }
            } else {
                field.push(c);
            }
            continue;
        }
        match c {
            '"' if !field_started || field.is_empty() => {
                in_quotes = true;
                field_started = true;
                row_has_content = true;
            }
            ',' => {
                row.push(std::mem::take(&mut field));
                field_started = false;
                row_has_content = true;
            }
            '\n' | '\r' => {
                if c == '\r' && chars.peek() == Some(&'\n') {
                    chars.next();
                }
                if row_has_content || !field.is_empty() {
                    row.push(std::mem::take(&mut field));
                    rows.push(std::mem::take(&mut row));
                }
                field_started = false;
                row_has_content = false;
            }
            _ => {
                field.push(c);
                field_started = true;
                row_has_content = true;
            }
        }
    }
    if row_has_content || !field.is_empty() {
        row.push(field);
        rows.push(row);
    }
    rows
}

fn parse_int(s: &str) -> Option<i64> {
    let t = s.trim();
    let digits = t.strip_prefix(['+', '-']).unwrap_or(t);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    t.strip_prefix('+').unwrap_or(t).parse().ok()
}

fn parse_float(s: &str) -> Option<f64> {
    let t = s.trim();
    if t.is_empty() {
        return None;
    }
    let lower = t.to_ascii_lowercase();
    match lower.trim_start_matches(['+', '-']) {
        "inf" | "infinity" => {
            return Some(if lower.starts_with('-') {
                f64::NEG_INFINITY
            } else {
                f64::INFINITY
            });
        }
        "nan" => return Some(f64::NAN),
        _ => {}
    }
    let ok = t
        .bytes()
        .all(|b| b.is_ascii_digit() || matches!(b, b'+' | b'-' | b'.' | b'e' | b'E'));
    if !ok {
        return None;
    }
    t.parse::<f64>().ok()
}

fn parse_bool(s: &str) -> Option<bool> {
    match s {
        "True" | "TRUE" | "true" => Some(true),
        "False" | "FALSE" | "false" => Some(false),
        _ => None,
    }
}

fn infer(cells: &[Option<&str>]) -> Column {
    let present: Vec<&str> = cells.iter().filter_map(|c| *c).collect();
    let any_na = present.len() != cells.len();
    if present.is_empty() {
        return Column::Float(vec![f64::NAN; cells.len()]);
    }
    if present.iter().all(|s| parse_int(s).is_some()) {
        if any_na {
            return Column::Float(
                cells
                    .iter()
                    .map(|c| c.and_then(parse_float).unwrap_or(f64::NAN))
                    .collect(),
            );
        }
        return Column::Int(
            cells
                .iter()
                .map(|c| c.and_then(parse_int).unwrap_or(0))
                .collect(),
        );
    }
    if present.iter().all(|s| parse_float(s).is_some()) {
        return Column::Float(
            cells
                .iter()
                .map(|c| c.and_then(parse_float).unwrap_or(f64::NAN))
                .collect(),
        );
    }
    if !any_na && present.iter().all(|s| parse_bool(s).is_some()) {
        return Column::Bool(
            cells
                .iter()
                .map(|c| c.and_then(parse_bool).unwrap_or(false))
                .collect(),
        );
    }
    Column::Str(cells.iter().map(|c| c.map(str::to_owned)).collect())
}

/// `pd.read_csv(io.BytesIO(raw), encoding="utf-8-sig")` for the scan's shape: a header row,
/// then rows with as many fields.
///
/// # Errors
/// Bytes that are not UTF-8, no header, or a row with more fields than the header (pandas'
/// "Error tokenizing data").
pub fn read_csv(raw: &[u8]) -> Result<Frame, CsvError> {
    let text = std::str::from_utf8(raw).map_err(|e| {
        CsvError(format!(
            "'utf-8' codec can't decode byte at position {}",
            e.valid_up_to()
        ))
    })?;
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    let mut rows = records(text);
    if rows.is_empty() {
        return Err(CsvError("No columns to parse from file".to_owned()));
    }
    let header = rows.remove(0);
    let width = header.len();
    for (i, r) in rows.iter().enumerate() {
        if r.len() > width {
            return Err(CsvError(format!(
                "Error tokenizing data. C error: Expected {width} fields in line {}, saw {}",
                i + 2,
                r.len()
            )));
        }
    }
    let mut frame = Frame {
        nrows: rows.len(),
        ..Frame::default()
    };
    let mut seen: HashMap<String, usize> = HashMap::new();
    for (j, name) in header.iter().enumerate() {
        let cells: Vec<Option<&str>> = rows
            .iter()
            .map(|r| r.get(j).map(String::as_str).filter(|s| !is_na(s)))
            .collect();
        // pandas mangles a repeated header: "x", "x.1", ...
        let count = seen.entry(name.clone()).or_insert(0);
        let name = if *count == 0 {
            name.clone()
        } else {
            format!("{name}.{count}")
        };
        *count += 1;
        frame.names.push(name.clone());
        frame.columns.insert(name, infer(&cells));
    }
    Ok(frame)
}
