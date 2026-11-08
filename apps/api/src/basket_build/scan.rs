//! `momentum_scan.build`'s frame from the desk's bar columns.

use std::collections::HashMap;

use baskfy_core::factors::{FactorConfig, compute_factors};
use chrono::NaiveDate;
use polars::prelude::{Column, DataFrame, DataType, IntoColumn, NamedFrom, Series};

use super::BuildError;
use super::score::ScanRow;

/// The desk's bar columns (`DESK_BAR_SCHEMA`), accumulated row by row.
#[derive(Default)]
pub struct DeskBars {
    /// `symbol`.
    pub symbol: Vec<String>,
    /// `instrument_id`.
    pub instrument_id: Vec<i64>,
    /// `date`.
    pub date: Vec<NaiveDate>,
    /// open, high, low, close, volume, close_raw, volume_raw.
    pub numbers: [Vec<f64>; 7],
}

impl DeskBars {
    /// The number of bars.
    #[must_use]
    pub fn len(&self) -> usize {
        self.instrument_id.len()
    }

    /// No bars.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.instrument_id.is_empty()
    }
}

/// 2B.2's carried columns, by symbol.
pub struct Carried {
    /// `symbol`.
    pub symbol: String,
    /// `series`.
    pub series: Option<String>,
    /// `marketcap`.
    pub marketcap: Option<f64>,
    /// `beta`.
    pub beta: Option<f64>,
    /// `circuits_three_months`.
    pub circuits_three_months: Option<i64>,
    /// `circuits_one_year`.
    pub circuits_one_year: Option<i64>,
    /// `is_nifty_fno`.
    pub is_nifty_fno: i64,
}

/// A polars error as Python's polars class.
pub(crate) fn pl_err(e: &polars::prelude::PolarsError) -> BuildError {
    BuildError::python("ComputeError", e.to_string())
}

/// A core error as the Python exception it ports.
pub(crate) fn core_err(e: baskfy_core::error::CoreError) -> BuildError {
    use baskfy_core::error::CoreError;
    match e {
        CoreError::Polars(p) => pl_err(&p),
        CoreError::Arithmetic(m) => BuildError::python("InvalidOperation", m),
        CoreError::InvalidInput(m) | CoreError::MissingColumn(m) => {
            BuildError::python("ValueError", m)
        }
    }
}

fn day_number(d: NaiveDate) -> i32 {
    let epoch = NaiveDate::from_ymd_opt(1970, 1, 1).unwrap_or_default();
    i32::try_from((d - epoch).num_days()).unwrap_or(i32::MAX)
}

fn date_column(name: &str, days: Vec<i32>) -> Result<Column, BuildError> {
    Ok(Series::new(name.into(), days)
        .cast(&DataType::Date)
        .map_err(|e| pl_err(&e))?
        .into_column())
}

fn floats(df: &DataFrame, name: &str) -> Result<Vec<Option<f64>>, BuildError> {
    let s = df
        .column(name)
        .map_err(|e| pl_err(&e))?
        .as_materialized_series()
        .cast(&DataType::Float64)
        .map_err(|e| pl_err(&e))?;
    Ok(s.f64().map_err(|e| pl_err(&e))?.iter().collect())
}

fn strings(df: &DataFrame, name: &str) -> Result<Vec<Option<String>>, BuildError> {
    let s = df
        .column(name)
        .map_err(|e| pl_err(&e))?
        .as_materialized_series()
        .cast(&DataType::String)
        .map_err(|e| pl_err(&e))?;
    Ok(s.str()
        .map_err(|e| pl_err(&e))?
        .iter()
        .map(|v| v.map(str::to_owned))
        .collect())
}

/// `momentum_scan.build(bars, as_of, trading_days, cfg=DESK_CONFIG, carried=carried)`'s frame,
/// as the rows `score.score` reads, sorted by symbol (and `drop_duplicates(subset="symbol")`).
///
/// # Errors
/// Python's, as polars and the factor engine raise them.
pub fn build_scan(
    bars: &DeskBars,
    as_of: NaiveDate,
    trading_days: &[NaiveDate],
    carried: &[Carried],
) -> Result<Vec<ScanRow>, BuildError> {
    let n = bars.len();
    let [open, high, low, close, volume, close_raw, volume_raw] = &bars.numbers;
    let frame = DataFrame::new(
        n,
        vec![
            Series::new("symbol".into(), bars.symbol.clone()).into_column(),
            Series::new("instrument_id".into(), bars.instrument_id.clone()).into_column(),
            date_column("date", bars.date.iter().map(|d| day_number(*d)).collect())?,
            Series::new("open".into(), open.clone()).into_column(),
            Series::new("high".into(), high.clone()).into_column(),
            Series::new("low".into(), low.clone()).into_column(),
            Series::new("close".into(), close.clone()).into_column(),
            Series::new("volume".into(), volume.clone()).into_column(),
            Series::new("close_raw".into(), close_raw.clone()).into_column(),
            Series::new("volume_raw".into(), volume_raw.clone()).into_column(),
        ],
    )
    .map_err(|e| pl_err(&e))?;
    let computed = compute_factors(
        &frame,
        as_of,
        trading_days,
        None,
        &FactorConfig::default(),
        None,
    )
    .map_err(core_err)?
    .frame;

    let symbols = strings(&computed, "symbol")?;
    let col = |name: &str| -> Result<Vec<f64>, BuildError> {
        Ok(floats(&computed, name)?
            .into_iter()
            .map(|v| v.unwrap_or(f64::NAN))
            .collect())
    };
    let close_c = col("close")?;
    let median = col("median_vol_12m")?;
    let rsi = col("rsi_1m")?;
    let vol = col("vol_12m")?;
    let pos3 = col("pos_days_3m")?;
    let pos6 = col("pos_days_6m")?;
    let ma20 = col("ma_20")?;
    let ma50 = col("ma_50")?;
    let ma100 = col("ma_100")?;
    let ma200 = col("ma_200")?;
    let away = col("away_high_1y")?;
    let rets: Vec<Vec<f64>> = ["ret_1m", "ret_3m", "ret_6m", "ret_9m", "ret_12m"]
        .iter()
        .map(|c| col(c))
        .collect::<Result<_, _>>()?;
    let sharpes: Vec<Vec<f64>> = [
        "sharpe_1m",
        "sharpe_3m",
        "sharpe_6m",
        "sharpe_9m",
        "sharpe_12m",
    ]
    .iter()
    .map(|c| col(c))
    .collect::<Result<_, _>>()?;

    // The inner join on symbol: carried wins every column both sides have.
    let mut by_symbol: HashMap<&str, Vec<&Carried>> = HashMap::new();
    for c in carried {
        by_symbol.entry(c.symbol.as_str()).or_default().push(c);
    }
    let nan = |v: Option<f64>| v.unwrap_or(f64::NAN);
    #[allow(clippy::cast_precision_loss)]
    let nan_i = |v: Option<i64>| v.map_or(f64::NAN, |x| x as f64);
    let mut rows: Vec<ScanRow> = Vec::new();
    for (i, symbol) in symbols.iter().enumerate() {
        let Some(symbol) = symbol else { continue };
        let Some(matches) = by_symbol.get(symbol.as_str()) else {
            continue;
        };
        for c in matches {
            #[allow(clippy::cast_precision_loss)]
            let fno = c.is_nifty_fno as f64;
            rows.push(ScanRow {
                symbol: symbol.clone(),
                series: c.series.clone(),
                close: close_c[i],
                marketcap: nan(c.marketcap),
                median_volume_one_year: median[i],
                rsi_one_month: rsi[i],
                volatility_one_year: vol[i],
                beta: nan(c.beta),
                circuits_three_months: nan_i(c.circuits_three_months),
                circuits_one_year: nan_i(c.circuits_one_year),
                positive_days_3m: pos3[i],
                positive_days_6m: pos6[i],
                ma_20: ma20[i],
                ma_50: ma50[i],
                ma_100: ma100[i],
                ma_200: ma200[i],
                away_from_high_one_year: away[i],
                is_nifty_fno: fno,
                returns: [rets[0][i], rets[1][i], rets[2][i], rets[3][i], rets[4][i]],
                sharpe: [
                    sharpes[0][i],
                    sharpes[1][i],
                    sharpes[2][i],
                    sharpes[3][i],
                    sharpes[4][i],
                ],
            });
        }
    }
    // `.sort("symbol")` (symbols are unique here: `_instrument_ids` refused a shared one), then
    // `drop_duplicates(subset="symbol")` keeping the first.
    rows.sort_by(|a, b| a.symbol.cmp(&b.symbol));
    rows.dedup_by(|b, a| a.symbol == b.symbol);
    Ok(rows)
}
