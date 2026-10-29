//! `baskfy_api.csv_export` — the docs/13 93-column reference export, byte for byte: UTF-8 with
//! a BOM, an unquoted header, only `name` quoted (always), `\n` line endings, every value at its
//! stored precision (never through a float).
//!
//! The rows come from `baskfy_core::screener::build_export_query` — the statement Python
//! executes, compiled for asyncpg. Its expanding `IN` lists (`__[POSTCOMPILE_name]`) are expanded
//! here exactly as SQLAlchemy expands them at execution (one typed `$n` per element, later
//! positions renumbered), so the database runs the same query Python runs.

use std::sync::LazyLock;

use baskfy_core::screener::{BindValue, CompiledStatement, universe_by_slug};
use baskfy_db::row;
use chrono::NaiveDate;
use regex::Regex;
use sqlx::postgres::{PgArguments, PgRow};
use sqlx::{Arguments, Column, Row, TypeInfo, ValueRef};

use super::{db, dbe};
use crate::problems::{Problem, internal_error};

/// `BOM`.
pub const BOM: &str = "\u{feff}";
/// `QUOTED_COLUMN`.
pub const QUOTED_COLUMN: &str = "name";
/// `LINE_ENDING`.
pub const LINE_ENDING: &str = "\n";

/// `baskfy_core.reference_export.EXPORT_COLUMNS`: docs/13 §1's 93 columns, in order (asserted
/// against the committed reference file's header by this module's tests).
pub const EXPORT_COLUMNS: [&str; 93] = [
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

/// `baskfy_core.reference_export.FACTOR_COLUMN_MAP`: export column -> `factor_daily` column.
pub const FACTOR_COLUMN_MAP: [(&str, &str); 44] = [
    ("close", "close"),
    ("marketcap", "marketcap_cr"),
    ("volume", "vol_day_val"),
    ("median_volume_one_year", "median_vol_12m"),
    ("beta", "beta_12m"),
    ("high_one_year", "high_1y"),
    ("high_all_time", "high_ath"),
    ("away_from_high_one_year", "away_high_1y"),
    ("away_from_high_all_time", "away_high_ath"),
    ("ma_200", "ma_200"),
    ("ma_100", "ma_100"),
    ("ma_50", "ma_50"),
    ("ma_20", "ma_20"),
    ("series", "series"),
    ("absolute_return_one_year", "ret_12m"),
    ("absolute_return_nine_months", "ret_9m"),
    ("absolute_return_six_months", "ret_6m"),
    ("absolute_return_three_months", "ret_3m"),
    ("absolute_return_one_month", "ret_1m"),
    ("sharpe_return_one_year", "sharpe_12m"),
    ("sharpe_return_nine_months", "sharpe_9m"),
    ("sharpe_return_six_months", "sharpe_6m"),
    ("sharpe_return_three_months", "sharpe_3m"),
    ("sharpe_return_one_month", "sharpe_1m"),
    ("rsi_one_year", "rsi_12m"),
    ("rsi_nine_months", "rsi_9m"),
    ("rsi_six_months", "rsi_6m"),
    ("rsi_three_months", "rsi_3m"),
    ("rsi_one_month", "rsi_1m"),
    ("volatility_one_year", "vol_12m"),
    ("volatility_nine_months", "vol_9m"),
    ("volatility_six_months", "vol_6m"),
    ("volatility_three_months", "vol_3m"),
    ("volatility_one_month", "vol_1m"),
    ("circuits_one_year", "circuits_12m"),
    ("circuits_nine_months", "circuits_9m"),
    ("circuits_six_months", "circuits_6m"),
    ("circuits_three_months", "circuits_3m"),
    ("circuits_one_month", "circuits_1m"),
    ("positive_days_percent_one_year", "pos_days_12m"),
    ("positive_days_percent_nine_months", "pos_days_9m"),
    ("positive_days_percent_six_months", "pos_days_6m"),
    ("positive_days_percent_three_months", "pos_days_3m"),
    ("positive_days_percent_one_month", "pos_days_1m"),
];

/// `_DIRECT_SOURCES` beyond the factor map.
const DIRECT_EXTRA: [&str; 6] = ["name", "symbol", "open", "high", "low", "volume_shares"];

/// `_FLAG_SOURCES[column]`: the mask column and the universe's bit, for an `is_*` column.
fn flag_source(column: &str) -> Option<(&'static str, i64)> {
    let base = column.strip_prefix("is_")?;
    let (slug_part, mask) = if let Some(b) = base.strip_suffix("_top_beta") {
        (b, "top_beta_mask")
    } else if let Some(b) = base.strip_suffix("_top_volatility") {
        (b, "top_volatility_mask")
    } else {
        (base, "universe_mask")
    };
    let universe = universe_by_slug(&slug_part.replace('_', "-")).ok()?;
    Some((mask, universe.mask_value()))
}

fn direct_source(column: &str) -> Option<&'static str> {
    FACTOR_COLUMN_MAP
        .iter()
        .find(|(k, _)| *k == column)
        .map(|(_, v)| *v)
        .or_else(|| DIRECT_EXTRA.iter().find(|k| **k == column).copied())
}

/// `filename_for(screen_name, as_of)`: `Investing 001` + 2026-08-18 ->
/// `investing-001-2026-08-18.csv`.
#[must_use]
pub fn filename_for(screen_name: &str, as_of: NaiveDate) -> String {
    let mut slug = String::new();
    for c in screen_name.chars() {
        if c.is_alphanumeric() {
            slug.extend(c.to_lowercase());
        } else {
            slug.push('-');
        }
    }
    let mut slug = slug.trim_matches('-').to_owned();
    while slug.contains("--") {
        slug = slug.replace("--", "-");
    }
    let slug = if slug.is_empty() {
        "screen".to_owned()
    } else {
        slug
    };
    format!("{slug}-{}.csv", crate::wire::py_date(as_of))
}

/// `quote(value)`: wrap, double any embedded quote.
#[must_use]
pub fn quote(value: &str) -> String {
    format!("\"{}\"", value.replace('"', "\"\""))
}

/// `header_line()`.
#[must_use]
pub fn header_line() -> String {
    EXPORT_COLUMNS.join(",")
}

/// One cell of a result row: `None` for SQL `NULL`.
#[derive(Debug, Clone, PartialEq)]
pub enum Cell {
    /// `None`.
    Null,
    /// An integer column.
    Int(i64),
    /// A `numeric` (kept as its stored text, `format(d, "f")`).
    Numeric(String),
    /// A float column (`str(float)`).
    Float(f64),
    /// Text.
    Text(String),
    /// A date.
    Date(NaiveDate),
    /// A bool.
    Bool(bool),
}

/// `format_cell(value)`.
#[must_use]
pub fn format_cell(cell: &Cell) -> String {
    match cell {
        Cell::Null => String::new(),
        Cell::Int(i) => i.to_string(),
        Cell::Numeric(s) | Cell::Text(s) => s.clone(),
        Cell::Float(f) => baskfy_core::py::float_repr(*f),
        Cell::Date(d) => crate::wire::py_date(*d),
        Cell::Bool(b) => if *b { "1" } else { "0" }.to_owned(),
    }
}

fn cell(r: &PgRow, column: &str) -> Result<Cell, Problem> {
    let Some(col) = r.columns().iter().find(|c| c.name() == column) else {
        return Ok(Cell::Null);
    };
    let raw = r.try_get_raw(col.ordinal()).map_err(db)?;
    if raw.is_null() {
        return Ok(Cell::Null);
    }
    let kind = col.type_info().name().to_owned();
    Ok(match kind.as_str() {
        "INT2" => Cell::Int(i64::from(r.try_get::<i16, _>(col.ordinal()).map_err(db)?)),
        "INT4" => Cell::Int(i64::from(r.try_get::<i32, _>(col.ordinal()).map_err(db)?)),
        "INT8" => Cell::Int(r.try_get::<i64, _>(col.ordinal()).map_err(db)?),
        "NUMERIC" => Cell::Numeric(row::decimal(r, column).map_err(|e| dbe(&e))?.to_string()),
        "FLOAT4" => Cell::Float(f64::from(r.try_get::<f32, _>(col.ordinal()).map_err(db)?)),
        "FLOAT8" => Cell::Float(r.try_get::<f64, _>(col.ordinal()).map_err(db)?),
        "DATE" => Cell::Date(r.try_get::<NaiveDate, _>(col.ordinal()).map_err(db)?),
        "BOOL" => Cell::Bool(r.try_get::<bool, _>(col.ordinal()).map_err(db)?),
        _ => Cell::Text(r.try_get::<String, _>(col.ordinal()).map_err(db)?),
    })
}

/// `export_row(row, as_of)`: one data line, in docs/13 §1's column order.
///
/// # Errors
/// 500 for a column that does not decode.
pub fn export_row(r: &PgRow, as_of: NaiveDate) -> Result<String, Problem> {
    let mut cells = Vec::with_capacity(EXPORT_COLUMNS.len());
    for column in EXPORT_COLUMNS {
        if column == "date" {
            cells.push(crate::wire::py_date(as_of));
            continue;
        }
        if let Some((mask_column, bit)) = flag_source(column) {
            // `isinstance(mask, int) and mask & bit`.
            let set = matches!(cell(r, mask_column)?, Cell::Int(m) if m & bit != 0);
            cells.push(if set { "1" } else { "0" }.to_owned());
            continue;
        }
        let text = match direct_source(column) {
            Some(source) => format_cell(&cell(r, source)?),
            None => String::new(),
        };
        cells.push(if column == QUOTED_COLUMN {
            quote(&text)
        } else {
            text
        });
    }
    Ok(cells.join(","))
}

static PLACEHOLDER: LazyLock<Option<Regex>> = LazyLock::new(|| Regex::new(r"\$(\d+)").ok());

/// The compiled statement as asyncpg executes it: expanding lists rendered one typed `$n` per
/// element (SQLAlchemy's `IN (__[POSTCOMPILE_x])` expansion), every later position renumbered,
/// and the arguments bound in that order.
///
/// # Errors
/// 500 when the statement cannot be rendered (an unknown bind name).
pub fn executable(compiled: &CompiledStatement) -> Result<(String, PgArguments), Problem> {
    let Some(placeholder) = PLACEHOLDER.as_ref() else {
        return Err(internal_error());
    };
    let value_of = |name: &str| {
        compiled
            .params
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, v)| v.clone())
    };
    let mut args = PgArguments::default();
    let mut renumber: Vec<usize> = Vec::new(); // old position (1-based) -> new position
    let mut expansions: Vec<(String, String)> = Vec::new();
    let mut next = 1usize;
    let bind_err = |e: sqlx::error::BoxDynError| {
        crate::logging::error(
            "baskfy_api.app",
            "unhandled error",
            &[("error", &e.to_string())],
        );
        internal_error()
    };
    for name in &compiled.param_order {
        let value = value_of(name).ok_or_else(internal_error)?;
        match value {
            BindValue::StrList(items) => {
                let rendered = if items.is_empty() {
                    "SELECT CAST(NULL AS VARCHAR) WHERE 1!=1".to_owned()
                } else {
                    let mut parts = Vec::with_capacity(items.len());
                    for item in items {
                        parts.push(format!("${next}::VARCHAR"));
                        next += 1;
                        args.add(item).map_err(bind_err)?;
                    }
                    parts.join(", ")
                };
                expansions.push((format!("__[POSTCOMPILE_{name}]"), rendered));
            }
            other => {
                renumber.push(next);
                next += 1;
                match other {
                    BindValue::Int(i) => args.add(i).map_err(bind_err)?,
                    BindValue::Float(f) => args.add(f).map_err(bind_err)?,
                    BindValue::Decimal(d) => args.add(d.py_str()).map_err(bind_err)?,
                    BindValue::Date(d) => args.add(d).map_err(bind_err)?,
                    BindValue::StrList(_) => {}
                }
            }
        }
    }
    let mut failed = false;
    let mut sql = placeholder
        .replace_all(&compiled.sql, |caps: &regex::Captures<'_>| {
            let old: usize = caps[1].parse().unwrap_or(0);
            match old.checked_sub(1).and_then(|i| renumber.get(i)) {
                Some(new) => format!("${new}"),
                None => {
                    failed = true;
                    caps[0].to_owned()
                }
            }
        })
        .into_owned();
    if failed {
        return Err(internal_error());
    }
    for (token, rendered) in expansions {
        sql = sql.replace(&token, &rendered);
    }
    Ok((sql, args))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn the_header_is_the_committed_reference_files() {
        // docs/13 §5 step 6: the export reproduces the committed file's column names and order.
        let bytes = baskfy_providers::reference_export_io::reference_export_bytes().unwrap();
        let text = String::from_utf8(bytes).unwrap();
        let first = text.lines().next().unwrap();
        assert_eq!(first, format!("{BOM}{}", header_line()));
    }

    #[test]
    fn filenames_and_quoting_are_pythons() {
        let d = NaiveDate::from_ymd_opt(2026, 8, 18).unwrap();
        assert_eq!(
            filename_for("Investing 001", d),
            "investing-001-2026-08-18.csv"
        );
        assert_eq!(filename_for("  --  ", d), "screen-2026-08-18.csv");
        assert_eq!(
            filename_for("Momentum, Low Volatility", d),
            "momentum-low-volatility-2026-08-18.csv"
        );
        assert_eq!(quote(r#"A "B" C"#), r#""A ""B"" C""#);
        assert_eq!(flag_source("is_nifty_50"), Some(("universe_mask", 1)));
        assert_eq!(
            flag_source("is_etf_top_volatility"),
            Some(("top_volatility_mask", 8192))
        );
        assert_eq!(
            flag_source("is_nifty_fno_top_beta"),
            Some(("top_beta_mask", 4096))
        );
        assert_eq!(format_cell(&Cell::Bool(true)), "1");
        assert_eq!(format_cell(&Cell::Null), "");
    }
}
