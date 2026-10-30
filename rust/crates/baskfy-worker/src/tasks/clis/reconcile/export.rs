//! `baskfy_core.reference_export.read_export`: the export, typed as polars infers it.

use std::collections::HashMap;
use std::path::Path;

use baskfy_core::decimal::WideDecimal;
use baskfy_core::py::wide_from_float_str;

use crate::error::WorkerError;

pub(crate) const EXPORT_COLUMNS: [&str; 93] = [
    "name",
    "symbol",
    "series",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "marketcap",
    "volume_shares",
    "absolute_return_one_year",
    "absolute_return_nine_months",
    "absolute_return_six_months",
    "absolute_return_three_months",
    "absolute_return_one_month",
    "sharpe_return_one_year",
    "sharpe_return_nine_months",
    "sharpe_return_six_months",
    "sharpe_return_three_months",
    "sharpe_return_one_month",
    "rsi_one_year",
    "rsi_nine_months",
    "rsi_six_months",
    "rsi_three_months",
    "rsi_one_month",
    "volatility_one_year",
    "volatility_nine_months",
    "volatility_six_months",
    "volatility_three_months",
    "volatility_one_month",
    "beta",
    "circuits_one_year",
    "circuits_nine_months",
    "circuits_six_months",
    "circuits_three_months",
    "circuits_one_month",
    "positive_days_percent_one_year",
    "positive_days_percent_nine_months",
    "positive_days_percent_six_months",
    "positive_days_percent_three_months",
    "positive_days_percent_one_month",
    "high_one_year",
    "high_all_time",
    "away_from_high_one_year",
    "away_from_high_all_time",
    "ma_200",
    "ma_100",
    "ma_50",
    "ma_20",
    "median_volume_one_year",
    "is_nifty_50",
    "is_nifty_next_50",
    "is_nifty_100",
    "is_nifty_200",
    "is_nifty_500",
    "is_nifty_total_market",
    "is_nifty_large_mid_250",
    "is_nifty_midcap_150",
    "is_nifty_smallcap_250",
    "is_nifty_microcap_250",
    "is_nifty_mid_small_400",
    "is_nifty_allcap",
    "is_nifty_fno",
    "is_nifty_50_top_beta",
    "is_nifty_next_50_top_beta",
    "is_nifty_100_top_beta",
    "is_nifty_200_top_beta",
    "is_nifty_500_top_beta",
    "is_nifty_total_market_top_beta",
    "is_nifty_large_mid_250_top_beta",
    "is_nifty_midcap_150_top_beta",
    "is_nifty_smallcap_250_top_beta",
    "is_nifty_microcap_250_top_beta",
    "is_nifty_mid_small_400_top_beta",
    "is_nifty_allcap_top_beta",
    "is_nifty_fno_top_beta",
    "is_nifty_50_top_volatility",
    "is_nifty_next_50_top_volatility",
    "is_nifty_100_top_volatility",
    "is_nifty_200_top_volatility",
    "is_nifty_500_top_volatility",
    "is_nifty_total_market_top_volatility",
    "is_nifty_large_mid_250_top_volatility",
    "is_nifty_midcap_150_top_volatility",
    "is_nifty_smallcap_250_top_volatility",
    "is_nifty_microcap_250_top_volatility",
    "is_nifty_mid_small_400_top_volatility",
    "is_nifty_allcap_top_volatility",
    "is_nifty_fno_top_volatility",
    "is_etf",
    "is_etf_top_beta",
    "is_etf_top_volatility",
];

/// One cell, as polars types it.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Cell {
    Null,
    Int(i64),
    Float(f64),
    Str(String),
}

impl Cell {
    /// `Decimal(str(value))`.
    pub(crate) fn dec(&self) -> Result<WideDecimal, WorkerError> {
        match self {
            Self::Int(i) => Ok(WideDecimal::from(*i)),
            Self::Float(f) => wide_from_float_str(*f)
                .map_err(|e| WorkerError::python("InvalidOperation", e.to_string())),
            Self::Str(s) => s.parse().map_err(|_| {
                WorkerError::python(
                    "InvalidOperation",
                    "[<class 'decimal.ConversionSyntax'>]".to_owned(),
                )
            }),
            Self::Null => Err(WorkerError::python(
                "InvalidOperation",
                "[<class 'decimal.ConversionSyntax'>]".to_owned(),
            )),
        }
    }

    /// `value == 1`.
    pub(crate) fn is_one(&self) -> bool {
        match self {
            Self::Int(i) => *i == 1,
            Self::Float(f) => f.to_bits() == 1.0f64.to_bits(),
            _ => false,
        }
    }

    /// An integer cell (a circuit count); `None` for anything else.
    pub(crate) fn int(&self) -> Option<i64> {
        match self {
            Self::Int(i) => Some(*i),
            _ => None,
        }
    }

    /// Python truthiness of a numeric cell.
    pub(crate) fn truthy(&self) -> bool {
        match self {
            Self::Null => false,
            Self::Int(i) => *i != 0,
            Self::Float(f) => *f != 0.0,
            Self::Str(s) => !s.is_empty(),
        }
    }

    /// `str(value)` for a text cell.
    pub(crate) fn text(&self) -> String {
        match self {
            Self::Null => "None".to_owned(),
            Self::Int(i) => i.to_string(),
            Self::Float(f) => baskfy_core::py::float_repr(*f),
            Self::Str(s) => s.clone(),
        }
    }
}

/// The export, every column typed, in [`EXPORT_COLUMNS`] order.
#[derive(Debug, Clone)]
pub(crate) struct Frame {
    index: HashMap<&'static str, usize>,
    rows: Vec<Vec<Cell>>,
}

/// One row of a [`Frame`].
#[derive(Debug, Clone, Copy)]
pub(crate) struct Row<'a> {
    frame: &'a Frame,
    cells: &'a [Cell],
}

impl Row<'_> {
    /// `row[column]`.
    pub(crate) fn get(&self, column: &str) -> &Cell {
        self.frame
            .index
            .get(column)
            .and_then(|i| self.cells.get(*i))
            .unwrap_or(&Cell::Null)
    }

    /// `row["symbol"]`.
    pub(crate) fn symbol(&self) -> String {
        self.get("symbol").text()
    }
}

impl Frame {
    /// `frame.height`.
    pub(crate) fn height(&self) -> usize {
        self.rows.len()
    }

    /// `frame.iter_rows(named=True)`.
    pub(crate) fn rows(&self) -> impl Iterator<Item = Row<'_>> {
        self.rows.iter().map(|cells| Row { frame: self, cells })
    }

    /// `frame.filter(pl.col("symbol").is_in(wanted))`.
    pub(crate) fn only_symbols(&self, wanted: &[String]) -> Self {
        let rows = self
            .rows()
            .filter(|r| matches!(r.get("symbol"), Cell::Str(s) if wanted.contains(s)))
            .map(|r| r.cells.to_vec())
            .collect();
        Self {
            index: self.index.clone(),
            rows,
        }
    }
}

fn is_int(text: &str) -> bool {
    let body = text.strip_prefix(['+', '-']).unwrap_or(text);
    !body.is_empty() && body.bytes().all(|b| b.is_ascii_digit()) && text.parse::<i64>().is_ok()
}

fn python_list(items: &[String]) -> String {
    let quoted: Vec<String> = items.iter().map(|s| format!("'{s}'")).collect();
    format!("[{}]", quoted.join(", "))
}

/// `read_export(path)`.
pub(crate) fn read_export(path: &Path) -> Result<Frame, WorkerError> {
    let bytes = std::fs::read(path).map_err(|e| {
        WorkerError::python(
            "FileNotFoundError",
            format!(
                "[Errno {}] No such file or directory: '{}'",
                e.raw_os_error().unwrap_or(2),
                path.display()
            ),
        )
    })?;
    parse_export(&bytes)
}

/// `read_export` over the file's bytes (UTF-8 with BOM).
pub(crate) fn parse_export(bytes: &[u8]) -> Result<Frame, WorkerError> {
    let body = bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(bytes);
    let mut reader = csv::ReaderBuilder::new()
        .has_headers(true)
        .from_reader(body);
    let csv_error = |e: csv::Error| WorkerError::python("ComputeError", e.to_string());
    let headers: Vec<String> = reader
        .headers()
        .map_err(csv_error)?
        .iter()
        .map(str::to_owned)
        .collect();
    let mut raw: Vec<Vec<String>> = Vec::new();
    for record in reader.records() {
        raw.push(
            record
                .map_err(csv_error)?
                .iter()
                .map(str::to_owned)
                .collect(),
        );
    }
    let missing: Vec<String> = EXPORT_COLUMNS
        .iter()
        .filter(|c| !headers.iter().any(|h| h == *c))
        .map(|c| (*c).to_owned())
        .collect();
    let unexpected: Vec<String> = headers
        .iter()
        .filter(|h| !EXPORT_COLUMNS.contains(&h.as_str()))
        .cloned()
        .collect();
    if !missing.is_empty() || !unexpected.is_empty() {
        return Err(WorkerError::python(
            "ValueError",
            format!(
                "reference export schema drift: missing={} unexpected={}",
                python_list(&missing),
                python_list(&unexpected)
            ),
        ));
    }
    let positions: Vec<usize> = EXPORT_COLUMNS
        .iter()
        .map(|c| headers.iter().position(|h| h == c).unwrap_or(0))
        .collect();
    let kinds: Vec<u8> = positions
        .iter()
        .map(|&p| {
            let values = raw
                .iter()
                .filter_map(|r| r.get(p))
                .filter(|v| !v.is_empty());
            let all: Vec<&String> = values.collect();
            if !all.is_empty() && all.iter().all(|v| is_int(v)) {
                b'i'
            } else if !all.is_empty() && all.iter().all(|v| v.parse::<f64>().is_ok()) {
                b'f'
            } else {
                b's'
            }
        })
        .collect();
    let rows = raw
        .iter()
        .map(|record| {
            positions
                .iter()
                .zip(&kinds)
                .map(|(&p, kind)| match record.get(p).map(String::as_str) {
                    None | Some("") => Cell::Null,
                    Some(v) => match kind {
                        b'i' => v.parse().map_or(Cell::Null, Cell::Int),
                        b'f' => v.parse().map_or(Cell::Null, Cell::Float),
                        _ => Cell::Str(v.to_owned()),
                    },
                })
                .collect()
        })
        .collect();
    let index = EXPORT_COLUMNS
        .iter()
        .enumerate()
        .map(|(i, c)| (*c, i))
        .collect();
    Ok(Frame { index, rows })
}
