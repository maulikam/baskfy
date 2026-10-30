//! `baskfy_core.precision` as the three sleeve writers use it (docs/13 §4, house rule 8: round
//! at write time, half-up).
//!
//! `baskfy-core` holds a private copy inside `factors` (R2.3: a Python module with no Rust home
//! lives with the port that needed it); the detectors need the same table, plus `quantise`, so
//! this is the worker's copy. The table is Python's `COLUMN_PRECISION` and `INTEGER_COLUMNS` in
//! full, so a column the sleeves write is rounded exactly when Python's `apply_storage_precision`
//! rounds it: when it is in the table **and** its polars dtype is a float.

use rust_decimal::{Decimal, RoundingStrategy};

const PRICE_DP: i32 = 2;
const PERCENT_DP: i32 = 2;
const RSI_DP: i32 = 4;
const RATIO_DP: i32 = 10;

/// `COLUMN_PRECISION`, in full.
const COLUMN_PRECISION: &[(&str, i32)] = &[
    ("close", PRICE_DP),
    ("close_raw", PRICE_DP),
    ("ma_20", PRICE_DP),
    ("ma_50", PRICE_DP),
    ("ma_100", PRICE_DP),
    ("ma_200", PRICE_DP),
    ("high_1y", PRICE_DP),
    ("high_ath", PRICE_DP),
    ("ret_1m", PERCENT_DP),
    ("ret_3m", PERCENT_DP),
    ("ret_6m", PERCENT_DP),
    ("ret_9m", PERCENT_DP),
    ("ret_12m", PERCENT_DP),
    ("ret_12m_minus_1m", PERCENT_DP),
    ("ret_12m_minus_2m", PERCENT_DP),
    ("sharpe_1m", PERCENT_DP),
    ("sharpe_3m", PERCENT_DP),
    ("sharpe_6m", PERCENT_DP),
    ("sharpe_9m", PERCENT_DP),
    ("sharpe_12m", PERCENT_DP),
    ("away_high_1y", PERCENT_DP),
    ("away_high_ath", PERCENT_DP),
    ("pos_days_1m", PERCENT_DP),
    ("pos_days_3m", PERCENT_DP),
    ("pos_days_6m", PERCENT_DP),
    ("pos_days_9m", PERCENT_DP),
    ("pos_days_12m", PERCENT_DP),
    ("rsi_1m", RSI_DP),
    ("rsi_3m", RSI_DP),
    ("rsi_6m", RSI_DP),
    ("rsi_9m", RSI_DP),
    ("rsi_12m", RSI_DP),
    ("vol_1m", RATIO_DP),
    ("vol_3m", RATIO_DP),
    ("vol_6m", RATIO_DP),
    ("vol_9m", RATIO_DP),
    ("vol_12m", RATIO_DP),
    ("beta_12m", RATIO_DP),
    ("pe", RSI_DP),
    ("atr_14", RSI_DP),
    ("atr_ext_20", RSI_DP),
    ("ma50_slope_20", PERCENT_DP),
    ("eff_ratio_63", RSI_DP),
    ("max_dd_6m", PERCENT_DP),
    ("max_dd_12m", PERCENT_DP),
    ("downside_vol_6m", RATIO_DP),
    ("downside_vol_12m", RATIO_DP),
    ("sortino_6m", PERCENT_DP),
    ("sortino_12m", PERCENT_DP),
    ("underwater_12m", PERCENT_DP),
    ("ret_ex_top3_12m", PERCENT_DP),
    ("accel_21_105", RATIO_DP),
    ("accel_21_105_vs", RSI_DP),
    ("vol_exp_21_126", RSI_DP),
    ("excess_ret_3m", PERCENT_DP),
    ("excess_ret_6m", PERCENT_DP),
    ("excess_ret_12m", PERCENT_DP),
    ("resid_ret_12m", PERCENT_DP),
    ("rs_persist_126", PERCENT_DP),
    ("mom_pctile", PERCENT_DP),
    ("rank_persist_20", PERCENT_DP),
    ("nse_mr6", RATIO_DP),
    ("nse_mr12", RATIO_DP),
    ("trigger", PRICE_DP),
    ("stop_ref", PRICE_DP),
    ("pivot_high", PRICE_DP),
    ("score", PERCENT_DP),
    ("adr_pct", PERCENT_DP),
    ("prior_move_pct", PERCENT_DP),
    ("base_depth_pct", PERCENT_DP),
    ("tightness_adr", PERCENT_DP),
    ("dryup_ratio", PERCENT_DP),
    ("dist_ma_fast_pct", PERCENT_DP),
    ("dist_ma_slow_pct", PERCENT_DP),
    ("rvol", PERCENT_DP),
    ("gap_pct", PERCENT_DP),
    ("adj_factor", RATIO_DP),
    ("limit_price", PRICE_DP),
    ("stop_price", PRICE_DP),
    ("sma_200", PRICE_DP),
    ("ema_21", PRICE_DP),
    ("high_20_prior", PRICE_DP),
    ("change_pct", PERCENT_DP),
    ("ret_20_pct", PERCENT_DP),
    ("close_position", RSI_DP),
    ("pct_above_dma", RSI_DP),
    ("week_close_0", PRICE_DP),
    ("week_close_1", PRICE_DP),
    ("week_close_2", PRICE_DP),
    ("week_range_pct", RSI_DP),
    ("month_low_3", PRICE_DP),
    ("month_low_ratio", RSI_DP),
    ("sma_dma", PRICE_DP),
    ("entry_reference_close", PRICE_DP),
    ("stop_preview", PRICE_DP),
    ("high_since", PRICE_DP),
    ("next_trigger", PRICE_DP),
];

/// `INTEGER_COLUMNS`: stored as whole numbers.
const INTEGER_COLUMNS: [&str; 20] = [
    "marketcap_cr",
    "vol_day_val",
    "vol_avg_1w",
    "vol_avg_1m",
    "vol_avg_3m",
    "vol_avg_6m",
    "vol_avg_9m",
    "vol_avg_12m",
    "median_vol_12m",
    "circuits_1m",
    "circuits_3m",
    "circuits_6m",
    "circuits_9m",
    "circuits_12m",
    "vol_persist_20",
    "turnover_avg",
    "vol_sma_50",
    "turnover_inr",
    "turnover_avg_20",
    "rank_key",
];

/// How `apply_storage_precision` treats a float column of this name.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Rule {
    /// `round_expr(name, places)`.
    Places(i32),
    /// `integer_expr(name)`: half-up to a whole number, cast to `Int64`.
    Whole,
    /// Not in the table: left alone.
    Keep,
}

/// The rule for `name`. `COLUMN_PRECISION` is applied before `INTEGER_COLUMNS`, and no name is
/// in both, so the order does not matter.
pub(crate) fn rule(name: &str) -> Rule {
    if let Some(&(_, places)) = COLUMN_PRECISION.iter().find(|(n, _)| *n == name) {
        return Rule::Places(places);
    }
    if INTEGER_COLUMNS.contains(&name) {
        return Rule::Whole;
    }
    Rule::Keep
}

/// The places `COLUMN_PRECISION` gives `name` (`quantise(value, COLUMN_PRECISION[name])`).
pub(crate) fn places_of(name: &str) -> Option<i32> {
    COLUMN_PRECISION
        .iter()
        .find(|(n, _)| *n == name)
        .map(|&(_, p)| p)
}

/// polars' `sign` on a float: -1, 1, or the value itself (0, -0, NaN).
fn sign(x: f64) -> f64 {
    if x < 0.0 {
        -1.0
    } else if x > 0.0 {
        1.0
    } else {
        x
    }
}

/// polars' `round(places)` (half to even), as its float kernel computes it.
fn polars_round(val: f64, places: i32) -> f64 {
    let multiplier = 10.0_f64.powi(places);
    let ret = (val * multiplier).round_ties_even() / multiplier;
    if ret.is_finite() { ret } else { val }
}

/// `round_expr`: `((|x| 10^p + 0.5).floor() / 10^p).round(p) * sign(x)`; NULL for null, inf
/// and NaN.
pub(crate) fn round_half_up(value: Option<f64>, places: i32) -> Option<f64> {
    let scale = 10.0_f64.powf(f64::from(places));
    match value {
        Some(x) if x.is_finite() => {
            Some(polars_round((x.abs() * scale + 0.5).floor() / scale, places) * sign(x))
        }
        _ => None,
    }
}

/// `integer_expr`: half-up to a whole number, as `Int64`; NULL for null, inf and NaN.
pub(crate) fn whole(value: Option<f64>) -> Option<i64> {
    match value {
        Some(x) if x.is_finite() => {
            let v = (x.abs() + 0.5).floor() * sign(x);
            // polars' strict cast: in range for every stored rupee figure.
            #[allow(clippy::cast_possible_truncation)]
            let i = v as i64;
            Some(i)
        }
        _ => None,
    }
}

/// `quantise(value, places)`: `Decimal(str(value)).quantize(10^-places, ROUND_HALF_UP)`;
/// `None` passes through.
pub(crate) fn quantise(value: Option<Decimal>, places: u32) -> Option<Decimal> {
    value.map(|v| v.round_dp_with_strategy(places, RoundingStrategy::MidpointAwayFromZero))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::float_cmp)]
    use super::*;

    #[test]
    fn half_up_not_half_even() {
        assert_eq!(round_half_up(Some(2.5), 0), Some(3.0));
        assert_eq!(round_half_up(Some(-2.5), 0), Some(-3.0));
        assert_eq!(round_half_up(Some(0.125), 2), Some(0.13));
        assert_eq!(round_half_up(Some(f64::NAN), 2), None);
        assert_eq!(round_half_up(Some(f64::INFINITY), 2), None);
        assert_eq!(round_half_up(None, 2), None);
        assert_eq!(whole(Some(2.5)), Some(3));
        assert_eq!(whole(Some(-2.5)), Some(-3));
        assert_eq!(whole(Some(f64::NAN)), None);
    }

    #[test]
    fn the_sleeves_columns_have_their_documented_precision() {
        assert_eq!(rule("limit_price"), Rule::Places(2));
        assert_eq!(rule("close_position"), Rule::Places(4));
        assert_eq!(rule("pct_above_dma"), Rule::Places(4));
        assert_eq!(rule("week_range_pct"), Rule::Places(4));
        assert_eq!(rule("month_low_ratio"), Rule::Places(4));
        assert_eq!(rule("adj_factor"), Rule::Places(10));
        assert_eq!(rule("turnover_avg"), Rule::Whole);
        assert_eq!(rule("rank_key"), Rule::Whole);
        assert_eq!(rule("open"), Rule::Keep);
        assert_eq!(rule("volume"), Rule::Keep);
        assert_eq!(places_of("next_trigger"), Some(2));
    }

    #[test]
    fn quantise_is_round_half_up() {
        let d = |s: &str| s.parse::<Decimal>().ok();
        assert_eq!(quantise(d("1.005"), 2), d("1.01"));
        assert_eq!(quantise(d("-1.005"), 2), d("-1.01"));
        assert_eq!(quantise(None, 2), None);
    }
}
