//! `baskfy_core.risk_free` — the India short-rate series a backtest's Sharpe subtracts.
//!
//! Python reads `baskfy_core/data/india_tbill.csv` (OECD MEI IR3TIB, monthly, percent per annum)
//! as a bundled seed file and divides each observation by 100 in its default decimal context.
//! The same bytes are compiled in here (`india_tbill.csv`, a copy; `tests::the_series_is_pythons`
//! fails the moment the two differ), so the worker reads no file at run time.

use std::str::FromStr as _;

use baskfy_core::backtest::engine::BacktestConfig;
use baskfy_core::backtest::helpers::WideDecimal;
use chrono::NaiveDate;
use rust_decimal::Decimal;

use crate::error::WorkerError;

/// The bundled series, byte for byte Python's `baskfy_core/data/india_tbill.csv`.
pub(super) const SERIES_CSV: &str = include_str!("india_tbill.csv");

/// `load_tbill_series()`: `(month-start, annual rate as a decimal fraction)`, sorted ascending.
///
/// # Errors
/// A row that does not parse (Python would raise `ValueError`/`InvalidOperation` too).
pub(super) fn load_tbill_series() -> Result<Vec<(NaiveDate, Decimal)>, WorkerError> {
    let hundred = WideDecimal::from(100_i64);
    let mut points = Vec::new();
    let mut lines = SERIES_CSV.lines();
    let header = lines.next().unwrap_or_default();
    let columns: Vec<&str> = header.split(',').collect();
    let date_at = columns.iter().position(|c| *c == "date");
    let pct_at = columns.iter().position(|c| *c == "annual_pct");
    let (Some(date_at), Some(pct_at)) = (date_at, pct_at) else {
        return Err(WorkerError::python("KeyError", "'date'"));
    };
    for line in lines {
        if line.is_empty() {
            continue;
        }
        let cells: Vec<&str> = line.split(',').collect();
        let bad = || WorkerError::python("ValueError", format!("bad T-bill row {line:?}"));
        let day = cells
            .get(date_at)
            .and_then(|d| NaiveDate::parse_from_str(d, "%Y-%m-%d").ok())
            .ok_or_else(bad)?;
        let pct = cells
            .get(pct_at)
            .and_then(|p| WideDecimal::from_str(p).ok())
            .ok_or_else(bad)?;
        let rate = pct
            .div(&hundred)
            .and_then(|r| r.try_to_decimal())
            .map_err(|e| WorkerError::python("InvalidOperation", e.0))?;
        points.push((day, rate));
    }
    points.sort_by_key(|(day, _)| *day);
    Ok(points)
}

/// `attach_tbill_curve(config)`: the bundled series up to `config.end`, unless the caller
/// already supplied a curve (or no observation overlaps).
///
/// # Errors
/// As [`load_tbill_series`].
pub(super) fn attach_tbill_curve(config: &BacktestConfig) -> Result<BacktestConfig, WorkerError> {
    if !config.risk_free_curve.is_empty() {
        return Ok(config.clone());
    }
    let overlapping: Vec<(NaiveDate, Decimal)> = load_tbill_series()?
        .into_iter()
        .filter(|(day, _)| *day <= config.end)
        .collect();
    if overlapping.is_empty() {
        return Ok(config.clone());
    }
    let mut attached = config.clone();
    attached.risk_free_curve = overlapping;
    Ok(attached)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    #[test]
    fn the_series_is_pythons() {
        let python = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../decile-blueprint/packages/core/src/baskfy_core/data/india_tbill.csv");
        let text = std::fs::read_to_string(&python).expect("the Python series");
        assert_eq!(
            text, SERIES_CSV,
            "india_tbill.csv drifted from Python's copy"
        );
    }

    #[test]
    fn rates_are_fractions_divided_in_pythons_context() {
        let series = load_tbill_series().unwrap();
        assert_eq!(series.len(), 176);
        let (first_day, first) = series[0];
        assert_eq!(first_day, NaiveDate::from_ymd_opt(2011, 11, 1).unwrap());
        // Decimal("8.868967") / Decimal("100")
        assert_eq!(first.to_string(), "0.08868967");
        let (_, last) = series[series.len() - 1];
        // Decimal("5.340000") / 100: the ideal exponent (-6) keeps two trailing zeros.
        assert_eq!(
            WideDecimal::from(last).to_sci_string(),
            "0.053400",
            "Python: str(Decimal('5.340000') / Decimal('100'))"
        );
        assert!(series.windows(2).all(|w| w[0].0 < w[1].0));
    }
}
