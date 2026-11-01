//! The parts of `baskfy_core.swing.market` and `baskfy_core.swing.indicators` the nightly needs
//! that `baskfy_core::swing` has not ported yet (its `market` module says so): the breadth
//! snapshot, the EMA index reading, the gate, `drawdown_pct`, and `liquid_expr`. Pure; they live
//! here (R2.3) until `swing::market` takes them, and each names its Python origin.
//!
//! Float arithmetic is Python's, operation for operation: `round(x, 2)` is the correctly rounded
//! decimal's nearest double; `sum(values) / len(values)` adds left to right.

use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_core::pl;
use baskfy_core::swing::market::{MarketConfig, MarketGate};
use baskfy_core::swing::setups::LiquidityConfig;

use crate::error::WorkerError;

/// `INDEX_HISTORY_SESSIONS`.
pub(crate) const INDEX_HISTORY_SESSIONS: i64 = 250;

/// Python's `round(x, ndigits)`: the double nearest the correctly rounded decimal.
pub(crate) fn py_round(x: f64, ndigits: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    format!("{x:.ndigits$}").parse::<f64>().unwrap_or(x)
}

/// `BreadthSnapshot`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) struct BreadthSnapshot {
    pub constituent_count: i64,
    pub pct_up_strong_1m: f64,
    pub pct_new_52w_high: f64,
    pub pct_above_ma_slow: f64,
}

impl BreadthSnapshot {
    /// `BreadthSnapshot(0, 0.0, 0.0, 0.0)`.
    pub(crate) fn empty() -> Self {
        Self {
            constituent_count: 0,
            pct_up_strong_1m: 0.0,
            pct_new_52w_high: 0.0,
            pct_above_ma_slow: 0.0,
        }
    }
}

/// One row of the frame `breadth_snapshot` reads.
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) struct BreadthRow {
    pub ret_20: Option<f64>,
    pub close: Option<f64>,
    pub ma_slow: Option<f64>,
    pub high_1y: Option<f64>,
}

fn share(part: usize, count: usize) -> f64 {
    #[allow(clippy::cast_precision_loss)]
    let x = part as f64 / count as f64 * 100.0;
    py_round(x, 2)
}

/// `breadth_snapshot`: the shares of the frame up `strong_move_pct` over the month, at the
/// 52-week high (`close >= high_1y`) and above the slow MA. A null comparison is not counted
/// (`filter` drops it); floats compare in polars' total order.
pub(crate) fn breadth_snapshot(rows: &[BreadthRow], config: &MarketConfig) -> BreadthSnapshot {
    let count = rows.len();
    if count == 0 {
        return BreadthSnapshot::empty();
    }
    let strong = rows
        .iter()
        .filter(|r| pl::cmp(r.ret_20, Some(config.strong_move_pct), |o| o.is_ge()) == Some(true))
        .count();
    let highs = rows
        .iter()
        .filter(|r| pl::cmp(r.close, r.high_1y, |o| o.is_ge()) == Some(true))
        .count();
    let above = rows
        .iter()
        .filter(|r| pl::cmp(r.close, r.ma_slow, |o| o.is_gt()) == Some(true))
        .count();
    BreadthSnapshot {
        constituent_count: i64::try_from(count).unwrap_or(i64::MAX),
        pct_up_strong_1m: share(strong, count),
        pct_new_52w_high: share(highs, count),
        pct_above_ma_slow: share(above, count),
    }
}

/// `IndexReading`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) struct IndexReading {
    pub close: f64,
    pub ma_fast: f64,
    pub ma_slow: f64,
}

impl IndexReading {
    /// `long_bias`: the fast EMA above the slow one.
    pub(crate) fn long_bias(&self) -> bool {
        self.ma_fast > self.ma_slow
    }

    /// `bearish`.
    pub(crate) fn bearish(&self) -> bool {
        self.ma_fast < self.ma_slow
    }
}

/// CPython 3.12's built-in `sum()` over floats: Neumaier's compensated summation, the
/// compensation added at the end only when it is non-zero and finite (`builtin_sum_impl`).
pub(crate) fn py_sum(values: &[f64]) -> f64 {
    let mut total = 0.0_f64;
    let mut c = 0.0_f64;
    for &x in values {
        let t = total + x;
        if total.abs() >= x.abs() {
            c += (total - t) + x;
        } else {
            c += (x - t) + total;
        }
        total = t;
    }
    if c != 0.0 && c.is_finite() {
        total += c;
    }
    total
}

/// `sum(values) / len(values)`.
fn mean(values: &[f64]) -> f64 {
    let total = py_sum(values);
    #[allow(clippy::cast_precision_loss)]
    let n = values.len() as f64;
    total / n
}

/// `ema(values, length)`: TradingView's `ta.ema`, seeded with the simple average of the first
/// `length` values; with no more than `length` values, their simple average.
pub(crate) fn ema(values: &[f64], length: usize) -> Result<f64, WorkerError> {
    if values.is_empty() {
        return Err(WorkerError::python("ValueError", "an EMA of no values"));
    }
    if values.len() <= length {
        return Ok(mean(values));
    }
    #[allow(clippy::cast_precision_loss)]
    let alpha = 2.0 / (length as f64 + 1.0);
    let mut value = mean(values.get(..length).unwrap_or(&[]));
    for x in values.get(length..).unwrap_or(&[]) {
        value = alpha * x + (1.0 - alpha) * value;
    }
    Ok(value)
}

/// `index_reading`: the newest level and its two EMAs; `None` with fewer than `index_ma_slow`
/// levels.
pub(crate) fn index_reading(
    levels: &[f64],
    config: &MarketConfig,
) -> Result<Option<IndexReading>, WorkerError> {
    let slow = usize::try_from(config.index_ma_slow).unwrap_or(usize::MAX);
    let fast = usize::try_from(config.index_ma_fast).unwrap_or(usize::MAX);
    if levels.len() < slow {
        return Ok(None);
    }
    let Some(&close) = levels.last() else {
        return Ok(None);
    };
    Ok(Some(IndexReading {
        close,
        ma_fast: ema(levels, fast)?,
        ma_slow: ema(levels, slow)?,
    }))
}

/// `market_gate`: breadth decides; the index can only make it worse.
pub(crate) fn market_gate(
    breadth: &BreadthSnapshot,
    index: Option<&IndexReading>,
    config: &MarketConfig,
) -> MarketGate {
    if breadth.constituent_count == 0 {
        return MarketGate::Red;
    }
    if index.is_some_and(IndexReading::bearish) {
        return MarketGate::Red;
    }
    if breadth.pct_up_strong_1m <= config.red_max_pct_up {
        return MarketGate::Red;
    }
    if breadth.pct_up_strong_1m >= config.green_min_pct_up
        && index.is_none_or(IndexReading::long_bias)
    {
        return MarketGate::Green;
    }
    MarketGate::Amber
}

/// `MarketGate.value`.
pub(crate) fn gate_value(gate: MarketGate) -> &'static str {
    match gate {
        MarketGate::Green => "GREEN",
        MarketGate::Amber => "AMBER",
        MarketGate::Red => "RED",
    }
}

/// `drawdown_pct(peak=, equity=)`: how far below the peak, in percent, quantized half-up to two
/// places in the decimal context and returned as a float; zero at or above the peak and when
/// the peak is not positive.
pub(crate) fn drawdown_pct(peak: &WideDecimal, equity: &WideDecimal) -> Result<f64, WorkerError> {
    if *peak <= WideDecimal::zero() || equity >= peak {
        return Ok(0.0);
    }
    let err = |e: baskfy_core::decimal::DecimalError| {
        WorkerError::python("InvalidOperation", e.to_string())
    };
    let ratio = peak.sub(equity).div(peak).map_err(err)?;
    let pct = ratio
        .mul(&WideDecimal::from(100_i64))
        .quantize(-2, Rounding::HalfUp)
        .map_err(err)?;
    pct.to_string()
        .parse::<f64>()
        .map_err(|e| WorkerError::python("ValueError", e.to_string()))
}

/// `liquid_expr`: `adr_pct >= adr_min_pct & turnover_avg >= turnover_min_inr & close >=
/// price_min`, a null read as false (a filter), floats in polars' total order.
pub(crate) fn is_liquid(
    adr_pct: Option<f64>,
    turnover_avg: Option<f64>,
    close: Option<f64>,
    liquidity: &LiquidityConfig,
) -> bool {
    let ge = |a: Option<f64>, b: f64| pl::cmp(a, Some(b), |o| o.is_ge());
    // Kleene `&`: false wins over null; a filter keeps only true.
    let parts = [
        ge(adr_pct, liquidity.adr_min_pct),
        ge(turnover_avg, liquidity.turnover_min_inr),
        ge(close, liquidity.price_min),
    ];
    parts.iter().all(|p| *p == Some(true))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::float_cmp)]
    use super::*;

    #[test]
    fn python_round_is_correctly_rounded() {
        assert_eq!(py_round(2.675, 2), 2.67);
        assert_eq!(py_round(0.125, 2), 0.12);
        assert_eq!(py_round(1.0 / 3.0 * 100.0, 2), 33.33);
    }

    #[test]
    fn sum_is_compensated_as_cpython_312s() {
        assert_eq!(py_sum(&[0.1, 0.2, 0.3]), 0.6);
        assert_eq!(py_sum(&[1e16, 1.0, -1e16]), 1.0);
    }

    #[test]
    fn ema_is_seeded_with_the_simple_average() {
        assert_eq!(ema(&[1.0, 2.0, 3.0], 5).ok(), Some(2.0));
        let v = [1.0, 2.0, 3.0, 4.0];
        // seed (1+2+3)/3 = 2; alpha = 0.5; 0.5*4 + 0.5*2 = 3.
        assert_eq!(ema(&v, 3).ok(), Some(3.0));
        assert!(ema(&[], 3).is_err());
    }

    #[test]
    fn the_gate_is_breadth_first_and_the_index_only_worsens_it() {
        let config = MarketConfig::default();
        let mut b = BreadthSnapshot {
            constituent_count: 100,
            pct_up_strong_1m: 6.0,
            pct_new_52w_high: 0.0,
            pct_above_ma_slow: 0.0,
        };
        let bull = IndexReading {
            close: 1.0,
            ma_fast: 2.0,
            ma_slow: 1.0,
        };
        let bear = IndexReading {
            close: 1.0,
            ma_fast: 1.0,
            ma_slow: 2.0,
        };
        assert_eq!(market_gate(&b, None, &config), MarketGate::Green);
        assert_eq!(market_gate(&b, Some(&bull), &config), MarketGate::Green);
        assert_eq!(market_gate(&b, Some(&bear), &config), MarketGate::Red);
        b.pct_up_strong_1m = 3.0;
        assert_eq!(market_gate(&b, None, &config), MarketGate::Amber);
        b.pct_up_strong_1m = 2.0;
        assert_eq!(market_gate(&b, None, &config), MarketGate::Red);
        assert_eq!(
            market_gate(&BreadthSnapshot::empty(), None, &config),
            MarketGate::Red
        );
    }

    #[test]
    fn breadth_counts_only_true_comparisons() {
        let rows = [
            BreadthRow {
                ret_20: Some(30.0),
                close: Some(10.0),
                ma_slow: Some(9.0),
                high_1y: Some(10.0),
            },
            BreadthRow {
                ret_20: None,
                close: Some(10.0),
                ma_slow: None,
                high_1y: Some(11.0),
            },
            BreadthRow {
                ret_20: Some(1.0),
                close: Some(10.0),
                ma_slow: Some(10.0),
                high_1y: None,
            },
        ];
        let s = breadth_snapshot(&rows, &MarketConfig::default());
        assert_eq!(s.constituent_count, 3);
        assert_eq!(s.pct_up_strong_1m, 33.33);
        assert_eq!(s.pct_new_52w_high, 33.33);
        assert_eq!(s.pct_above_ma_slow, 33.33);
    }

    #[test]
    fn drawdown_is_zero_at_the_peak_and_half_up_below_it() {
        let d = |s: &str| s.parse::<WideDecimal>().unwrap_or_default();
        assert_eq!(drawdown_pct(&d("100"), &d("100")).ok(), Some(0.0));
        assert_eq!(drawdown_pct(&d("0"), &d("-5")).ok(), Some(0.0));
        assert_eq!(drawdown_pct(&d("200"), &d("199.99")).ok(), Some(0.01));
        assert_eq!(drawdown_pct(&d("3"), &d("2")).ok(), Some(33.33));
    }

    #[test]
    fn liquidity_needs_all_three_floors() {
        let l = LiquidityConfig::default();
        assert!(is_liquid(Some(4.0), Some(5e7), Some(20.0), &l));
        assert!(!is_liquid(Some(3.9), Some(5e7), Some(20.0), &l));
        assert!(!is_liquid(None, Some(5e7), Some(20.0), &l));
        assert!(is_liquid(Some(f64::NAN), Some(5e7), Some(20.0), &l));
    }
}
