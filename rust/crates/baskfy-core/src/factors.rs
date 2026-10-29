//! Port of `baskfy_core.factors` — the factor engine (`compute_factors`).
//!
//! Goldens: `go/testdata/golden/L1/ranking/factors.compute_factors{,_unrounded}.case_NNN`.
//!
//! docs/05 is the numerical contract and the Python module follows it section by section; this
//! port follows the Python, line for line, including the modules it calls and that have no Rust
//! home yet, which live here as private submodules: `windows` (`baskfy_core.windows`),
//! `circuits`, `momentum` (the skip-month return), `precision` (`apply_storage_precision`),
//! `regime` (`baskfy_core.instrument_regime`) and `ranking` (`baskfy_core.factors_ranking`'s
//! two entry points).
//!
//! **How the floats are kept faithful.** Python evaluates every factor as a polars window
//! expression `.over("instrument_id")`; the rolling statistics here call the *same* polars
//! kernels (`rolling_mean/sum/std/max/quantile`, `ewm_mean`) on each instrument's slice, which
//! is what `.over` does, so their floats are the Python floats. Element-wise arithmetic is
//! IEEE and identical by construction; comparisons use polars' total order (NaN above
//! everything). The parts Python does in NumPy (Cutler's RSI over a date x instrument matrix,
//! the path statistics, the Wasserstein regime) reproduce NumPy's operation order: cumulative
//! sums, pairwise summation, `np.quantile`'s linear method and `np.linspace`.
//!
//! **Known bug, reproduced (DECISIONS-RS R1.4):** the regime columns are classified over each
//! instrument's whole history and labelled only on the frame's latest date, so bars after
//! `as_of` leave the `as_of` row's regime NULL (house rule 5). Fixing it is a strategy change
//! and Maulik's call; it is a Python change first, then a golden regeneration.

use std::cmp::Ordering;
use std::ops::Range;

use chrono::NaiveDate;
use polars::prelude::{
    AnyValue, DataFrame, DataType, IdxCa, IdxSize, IntoColumn, NamedFrom, Series,
    SortMultipleOptions,
};
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};

pub use self::windows::{FactorWindow, resolve_window, resolve_windows, subtract_months};

// ---------------------------------------------------------------------------
// Constants (factors.py, windows.py)
// ---------------------------------------------------------------------------

/// docs/05 §2 — "x sqrt(252)". The annualisation constant, in trading days.
pub const TRADING_DAYS_PER_YEAR: usize = 252;

/// docs/05 §3's guard: a volatility below half the last stored decimal place is zero.
pub const ZERO_VOLATILITY_EPSILON: f64 = 0.5e-10;

/// docs/06 §"Step 4" — the top-risk flag percentile (unused by the engine itself).
pub const TOP_RISK_FLAG_PERCENTILE: f64 = 0.10;

/// Turnover source label: the exchange's traded value was used (docs/05 §13).
pub const TURNOVER_EXCHANGE: &str = "exchange";
/// Turnover source label: `close_raw x volume_raw` was used.
pub const TURNOVER_DERIVED: &str = "close_x_volume";

/// Columns `compute_factors` requires on the input frame.
pub const REQUIRED_COLUMNS: [&str; 7] = [
    "instrument_id",
    "date",
    "close",
    "close_raw",
    "high",
    "low",
    "volume_raw",
];

/// Columns it uses if present, and adds as NULL if absent.
pub const OPTIONAL_COLUMNS: [&str; 6] = [
    "turnover",
    "upper_circuit",
    "lower_circuit",
    "series",
    "marketcap_cr",
    "pe",
];

/// The five windows every factor family is computed over (docs/01 §3, docs/05).
pub const WINDOW_MONTHS: [i64; 5] = [1, 3, 6, 9, 12];
/// docs/05 §6 — beta needs at least 200 overlapping observations.
pub const MIN_BETA_OBSERVATIONS: usize = 200;
/// docs/05 §9 — simple moving-average lengths, in bars.
pub const MA_LENGTHS: [usize; 4] = [20, 50, 100, 200];
/// docs/05 §10 — the 1-year high is over the last 252 bars.
pub const HIGH_1Y_BARS: usize = 252;
/// docs/05 §13 — the rolling traded-value windows, in bars.
pub const VOL_AVG_BARS: [(&str, usize); 6] = [
    ("1w", 5),
    ("1m", 21),
    ("3m", 63),
    ("6m", 126),
    ("9m", 189),
    ("12m", 252),
];
/// docs/05 §13 — the 1-year median traded value, in bars.
pub const MEDIAN_VOL_BARS: usize = 252;

/// The polars `ddof` of `vol_Nm` (docs/05 §2, amended 2026-08-31: population stdev).
const VOL_DDOF: u8 = 0;

fn sqrt_year() -> f64 {
    // `math.sqrt(TRADING_DAYS_PER_YEAR)`; 252 is exact in f64.
    252.0_f64.sqrt()
}

// ---------------------------------------------------------------------------
// Configuration (FactorConfig, CircuitConfig, SkipMonthConfig, RegimeConfig)
// ---------------------------------------------------------------------------

/// Ports `baskfy_core.circuits.CircuitConfig` — the INFERRED circuit-hit detection's knobs.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CircuitConfig {
    /// docs/05 §12 — the NSE price bands the heuristic recognises, as fractions.
    pub bands: Vec<f64>,
    /// "with a 0.25% tolerance".
    pub band_tolerance: f64,
    /// How close to a published band counts as "at" it.
    pub tick: f64,
    /// When false, only published bands count.
    pub allow_band_heuristic: bool,
}

impl Default for CircuitConfig {
    fn default() -> Self {
        Self {
            bands: vec![0.02, 0.05, 0.10, 0.20],
            band_tolerance: 0.0025,
            tick: 0.05,
            allow_band_heuristic: true,
        }
    }
}

impl CircuitConfig {
    /// `CircuitConfig.__post_init__`'s checks.
    fn validate(&self) -> CoreResult<()> {
        if self.bands.is_empty() {
            return Err(CoreError::InvalidInput(
                "at least one price band is required".to_owned(),
            ));
        }
        if self.bands.iter().any(|b| *b <= 0.0) {
            return Err(CoreError::InvalidInput(format!(
                "price bands must be positive; got ({})",
                self.bands
                    .iter()
                    .map(|b| py_float(*b))
                    .collect::<Vec<_>>()
                    .join(", ")
            )));
        }
        if self.band_tolerance < 0.0 {
            return Err(CoreError::InvalidInput(format!(
                "band_tolerance must not be negative; got {}",
                py_float(self.band_tolerance)
            )));
        }
        Ok(())
    }
}

/// Ports `baskfy_core.momentum.SkipMonthDefinition` — the two readings of docs/05 §8.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum SkipMonthDefinition {
    /// `P_{t-21} / P_{t-252} - 1` — docs/05's primary.
    #[default]
    SkipEnd,
    /// `P_{t-21} / P_{t-273} - 1` — the alternative.
    FullYearEndingEarlier,
}

/// Ports `baskfy_core.momentum.SkipMonthConfig`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SkipMonthConfig {
    /// Which reading of docs/05 §8.
    pub definition: SkipMonthDefinition,
    /// Bars skipped for `ret_12m_minus_1m`.
    pub skip_1m_bars: usize,
    /// Bars skipped for `ret_12m_minus_2m`.
    pub skip_2m_bars: usize,
    /// The lookback in bars.
    pub lookback_bars: usize,
}

impl Default for SkipMonthConfig {
    fn default() -> Self {
        Self {
            definition: SkipMonthDefinition::SkipEnd,
            skip_1m_bars: 21,
            skip_2m_bars: 42,
            lookback_bars: 252,
        }
    }
}

impl SkipMonthConfig {
    fn validate(&self) -> CoreResult<()> {
        if self.skip_1m_bars < 1 || self.skip_2m_bars < 1 {
            return Err(CoreError::InvalidInput(
                "skip offsets must be at least one bar".to_owned(),
            ));
        }
        if self.lookback_bars <= self.skip_2m_bars {
            return Err(CoreError::InvalidInput(
                "the lookback must be longer than the skip".to_owned(),
            ));
        }
        Ok(())
    }

    /// Ports `SkipMonthConfig.numerator_denominator`: `(bars back for the numerator, for the
    /// denominator)`.
    pub fn numerator_denominator(&self, skip_bars: usize) -> (usize, usize) {
        match self.definition {
            SkipMonthDefinition::SkipEnd => (skip_bars, self.lookback_bars),
            SkipMonthDefinition::FullYearEndingEarlier => {
                (skip_bars, skip_bars + self.lookback_bars)
            }
        }
    }
}

/// Ports `baskfy_core.instrument_regime.RegimeConfig` (docs/05 §15).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RegimeConfig {
    /// Step 1's sample length.
    pub sample_bars: usize,
    /// Step 2's tail share.
    pub tail_fraction: f64,
    /// Step 4's margin.
    pub margin: f64,
    /// Rolling windows needed before the references mean anything.
    pub min_windows: usize,
}

impl Default for RegimeConfig {
    fn default() -> Self {
        Self {
            sample_bars: 63,
            tail_fraction: 0.20,
            margin: 0.10,
            min_windows: 10,
        }
    }
}

impl RegimeConfig {
    fn validate(&self) -> CoreResult<()> {
        if self.sample_bars < 2 {
            return Err(CoreError::InvalidInput(format!(
                "sample_bars must be at least 2; got {}",
                self.sample_bars
            )));
        }
        if !(self.tail_fraction > 0.0 && self.tail_fraction < 1.0) {
            return Err(CoreError::InvalidInput(format!(
                "tail_fraction must be in (0, 1); got {}",
                py_float(self.tail_fraction)
            )));
        }
        if !(self.margin >= 0.0 && self.margin < 1.0) {
            return Err(CoreError::InvalidInput(format!(
                "margin must be in [0, 1); got {}",
                py_float(self.margin)
            )));
        }
        Ok(())
    }
}

/// Ports `baskfy_core.factors.FactorConfig` — everything the engine can be recalibrated on.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FactorConfig {
    /// docs/05 §12.
    pub circuits: CircuitConfig,
    /// docs/05 §8.
    pub skip_month: SkipMonthConfig,
    /// docs/05 §15.
    pub regime: RegimeConfig,
    /// The calendar windows of the legacy families, in months.
    pub window_months: Vec<i64>,
    /// docs/05 §6's overlap requirement.
    pub min_beta_observations: usize,
}

impl Default for FactorConfig {
    fn default() -> Self {
        Self {
            circuits: CircuitConfig::default(),
            skip_month: SkipMonthConfig::default(),
            regime: RegimeConfig::default(),
            window_months: WINDOW_MONTHS.to_vec(),
            min_beta_observations: MIN_BETA_OBSERVATIONS,
        }
    }
}

impl FactorConfig {
    /// The dataclasses' `__post_init__` checks (Python cannot construct an invalid config).
    pub fn validate(&self) -> CoreResult<()> {
        self.circuits.validate()?;
        self.skip_month.validate()?;
        self.regime.validate()
    }
}

/// Ports `baskfy_core.factors.FactorResult`: one row per instrument at `as_of`, and the
/// windows that produced it, in `config.window_months` order (Python's dict).
#[derive(Debug, Clone)]
pub struct FactorResult {
    /// The as-of rows.
    pub frame: DataFrame,
    /// `{months: window}` in insertion order.
    pub windows: Vec<(i64, FactorWindow)>,
}

impl FactorResult {
    /// Ports `FactorResult.window_lengths`.
    pub fn window_lengths(&self) -> Vec<(i64, usize)> {
        self.windows.iter().map(|(m, w)| (*m, w.length())).collect()
    }
}

// ---------------------------------------------------------------------------
// Public entry points
// ---------------------------------------------------------------------------

/// Ports `baskfy_core.factors.compute_factors`: every factor in docs/05 for one as-of date,
/// rounded for storage (`apply_storage_precision`, house rule 8).
///
/// `bars` is the long history of every instrument; `benchmark` is NIFTY 50 (`date`, `level` or
/// `close`) for beta and `resid_ret_12m`; `market_benchmark` is NIFTY 500 for `excess_ret_*` and
/// `rs_persist_126`. Either absent makes its columns NULL.
///
/// # Errors
///
/// [`CoreError::InvalidInput`] with Python's `ValueError` message: a missing required column,
/// an `as_of` absent from the calendar, an empty calendar, a malformed benchmark.
/// [`CoreError::Polars`] when a column cannot be cast.
pub fn compute_factors(
    bars: &DataFrame,
    as_of: NaiveDate,
    trading_days: &[NaiveDate],
    benchmark: Option<&DataFrame>,
    config: &FactorConfig,
    market_benchmark: Option<&DataFrame>,
) -> CoreResult<FactorResult> {
    let unrounded = compute_factors_unrounded(
        bars,
        as_of,
        trading_days,
        benchmark,
        config,
        market_benchmark,
    )?;
    Ok(FactorResult {
        frame: precision::apply_storage_precision(&unrounded.frame)?,
        windows: resolve_windows(as_of, trading_days, &config.window_months)?,
    })
}

/// Ports `baskfy_core.factors.compute_factors_unrounded`: [`compute_factors`] without the
/// storage rounding. **Never write this** (it exists for the cross-validation harness).
///
/// # Errors
///
/// As [`compute_factors`].
pub fn compute_factors_unrounded(
    bars: &DataFrame,
    as_of: NaiveDate,
    trading_days: &[NaiveDate],
    benchmark: Option<&DataFrame>,
    config: &FactorConfig,
    market_benchmark: Option<&DataFrame>,
) -> CoreResult<FactorResult> {
    config.validate()?;
    require_columns(bars)?;
    let windows = resolve_windows(as_of, trading_days, &config.window_months)?;
    let ranking_windows = ranking::resolve_ranking_windows(as_of, trading_days)?;

    let mut long = prepare(bars, config)?;
    with_returns(&mut long)?;
    with_window_factors(&mut long, &windows)?;
    with_price_levels(&mut long)?;
    with_skip_month(&mut long, config)?;
    with_liquidity(&mut long)?;
    with_rsi(&mut long, &windows)?;
    with_beta(&mut long, benchmark, config)?;
    ranking::with_ranking_series(&mut long, &ranking_windows, market_benchmark)?;
    // KNOWN BUG (DECISIONS-RS R1.4): classified over the whole history, labelled on the
    // frame's max date only — bars after `as_of` leave the `as_of` regime NULL.
    regime::classify_regimes(&mut long, &config.regime)?;

    let as_of_days = days(as_of)?;
    let rows: Vec<usize> = (0..long.df.height())
        .filter(|&r| long.dates[r] == Some(as_of_days))
        .collect();
    let mut at = long.df.take(&idx(&rows)?)?;
    null_short_windows(&mut at, &windows)?;
    let at = ranking::with_ranking_at_as_of(
        at,
        &long,
        &rows,
        as_of,
        trading_days,
        &ranking_windows,
        benchmark,
        market_benchmark,
    )?;
    Ok(FactorResult { frame: at, windows })
}

// ---------------------------------------------------------------------------
// The long frame and its per-instrument groups
// ---------------------------------------------------------------------------

type F = Option<f64>;
type B = Option<bool>;

/// The sorted long frame, its `instrument_id` runs (`.over("instrument_id")`'s groups) and its
/// dates as days since the epoch.
struct Long {
    df: DataFrame,
    groups: Vec<Range<usize>>,
    /// Group index of each row.
    group_of: Vec<usize>,
    /// True for the group whose `instrument_id` is NULL (a join on it never matches).
    group_null: Vec<bool>,
    dates: Vec<Option<i32>>,
}

fn epoch() -> CoreResult<NaiveDate> {
    NaiveDate::from_ymd_opt(1970, 1, 1)
        .ok_or_else(|| CoreError::InvalidInput("epoch out of range".to_owned()))
}

fn days(d: NaiveDate) -> CoreResult<i32> {
    i32::try_from(d.signed_duration_since(epoch()?).num_days())
        .map_err(|e| CoreError::InvalidInput(e.to_string()))
}

fn idx(rows: &[usize]) -> CoreResult<IdxCa> {
    let v = rows
        .iter()
        .map(|&r| IdxSize::try_from(r).map_err(|e| CoreError::InvalidInput(e.to_string())))
        .collect::<CoreResult<Vec<IdxSize>>>()?;
    Ok(IdxCa::from_vec("".into(), v))
}

fn as_f64(n: usize) -> f64 {
    // Window lengths and counts are far below 2^53: the conversion is exact.
    #[allow(clippy::cast_precision_loss)]
    let x = n as f64;
    x
}

fn floats(df: &DataFrame, name: &str) -> CoreResult<Vec<F>> {
    let s = df
        .column(name)?
        .as_materialized_series()
        .cast(&DataType::Float64)?;
    Ok(s.f64()?.iter().collect())
}

fn put(df: &mut DataFrame, name: &str, values: Vec<F>) -> CoreResult<()> {
    df.with_column(Series::new(name.into(), values).into_column())?;
    Ok(())
}

fn put_null(df: &mut DataFrame, name: &str, dtype: &DataType) -> CoreResult<()> {
    let h = df.height();
    df.with_column(Series::full_null(name.into(), h, dtype).into_column())?;
    Ok(())
}

/// A left join's new column: `name`, or `name_right` when the frame already has `name`.
fn join_put(df: &mut DataFrame, name: &str, column: Series) -> CoreResult<()> {
    let target = if df.get_column_index(name).is_some() {
        format!("{name}_right")
    } else {
        name.to_owned()
    };
    df.with_column(column.with_name(target.as_str().into()).into_column())?;
    Ok(())
}

fn drop_if_present(df: &DataFrame, name: &str) -> CoreResult<DataFrame> {
    if df.get_column_index(name).is_some() {
        Ok(df.drop(name)?)
    } else {
        Ok(df.clone())
    }
}

fn has(df: &DataFrame, name: &str) -> bool {
    df.get_column_index(name).is_some()
}

/// Python's `repr` of a list of strings: `['a', 'b']`.
fn py_list(items: &[String]) -> String {
    let inner: Vec<String> = items.iter().map(|s| format!("'{s}'")).collect();
    format!("[{}]", inner.join(", "))
}

/// Python's `repr` of a float, close enough for error messages (`0.1`, `2.0`).
fn py_float(x: f64) -> String {
    if x.is_finite() && x.fract().abs() <= 0.0 && x.abs() < 1e16 {
        format!("{x:.1}")
    } else {
        format!("{x}")
    }
}

fn require_columns(bars: &DataFrame) -> CoreResult<()> {
    let missing: Vec<String> = REQUIRED_COLUMNS
        .iter()
        .filter(|c| !has(bars, c))
        .map(|c| (*c).to_owned())
        .collect();
    if missing.is_empty() {
        return Ok(());
    }
    let got: Vec<String> = bars
        .get_column_names()
        .iter()
        .map(|c| c.to_string())
        .collect();
    Err(CoreError::InvalidInput(format!(
        "bars are missing required columns {}; got {}",
        py_list(&missing),
        py_list(&got)
    )))
}

// --- polars semantics for scalars -------------------------------------------------------------

/// polars' total order on floats: NaN equals NaN and sorts above every number.
fn tot_cmp(a: f64, b: f64) -> Ordering {
    match (a.is_nan(), b.is_nan()) {
        (true, true) => Ordering::Equal,
        (true, false) => Ordering::Greater,
        (false, true) => Ordering::Less,
        (false, false) => a.partial_cmp(&b).unwrap_or(Ordering::Equal),
    }
}

fn cmp_f(a: F, b: f64, test: fn(Ordering) -> bool) -> B {
    a.map(|x| test(tot_cmp(x, b)))
}

fn gt(a: F, b: f64) -> B {
    cmp_f(a, b, |o| o == Ordering::Greater)
}

fn lt(a: F, b: f64) -> B {
    cmp_f(a, b, |o| o == Ordering::Less)
}

fn le(a: F, b: f64) -> B {
    cmp_f(a, b, |o| o != Ordering::Greater)
}

fn ge(a: F, b: f64) -> B {
    cmp_f(a, b, |o| o != Ordering::Less)
}

fn eq(a: F, b: f64) -> B {
    cmp_f(a, b, |o| o == Ordering::Equal)
}

fn gt2(a: F, b: F) -> B {
    match (a, b) {
        (Some(x), Some(y)) => Some(tot_cmp(x, y) == Ordering::Greater),
        _ => None,
    }
}

fn ge2(a: F, b: F) -> B {
    match (a, b) {
        (Some(x), Some(y)) => Some(tot_cmp(x, y) != Ordering::Less),
        _ => None,
    }
}

fn le2(a: F, b: F) -> B {
    match (a, b) {
        (Some(x), Some(y)) => Some(tot_cmp(x, y) != Ordering::Greater),
        _ => None,
    }
}

fn eq2(a: F, b: F) -> B {
    match (a, b) {
        (Some(x), Some(y)) => Some(tot_cmp(x, y) == Ordering::Equal),
        _ => None,
    }
}

/// Kleene AND.
fn and(a: B, b: B) -> B {
    match (a, b) {
        (Some(false), _) | (_, Some(false)) => Some(false),
        (Some(true), Some(true)) => Some(true),
        _ => None,
    }
}

/// Kleene OR.
fn or(a: B, b: B) -> B {
    match (a, b) {
        (Some(true), _) | (_, Some(true)) => Some(true),
        (Some(false), Some(false)) => Some(false),
        _ => None,
    }
}

/// `pl.when(cond)`: a NULL condition is not taken.
fn taken(cond: B) -> bool {
    cond == Some(true)
}

fn null(x: F) -> B {
    Some(x.is_none())
}

fn bin(a: F, b: F, f: fn(f64, f64) -> f64) -> F {
    match (a, b) {
        (Some(x), Some(y)) => Some(f(x, y)),
        _ => None,
    }
}

fn div(a: F, b: F) -> F {
    bin(a, b, |x, y| x / y)
}

fn sub(a: F, b: F) -> F {
    bin(a, b, |x, y| x - y)
}

fn mul(a: F, b: F) -> F {
    bin(a, b, |x, y| x * y)
}

fn bool_f(b: B) -> F {
    b.map(|x| if x { 1.0 } else { 0.0 })
}

// --- per-group polars kernels --------------------------------------------------------------

#[derive(Clone, Copy)]
enum Roll {
    Mean,
    Sum,
    Max,
    Std(u8),
    Median,
}

fn series_values(s: &Series) -> CoreResult<Vec<F>> {
    Ok(s.cast(&DataType::Float64)?.f64()?.iter().collect())
}

/// One instrument's `rolling_*(window_size, min_samples)` — the polars kernel itself.
fn rolling_slice(values: &[F], kind: Roll, window: usize, min: usize) -> CoreResult<Vec<F>> {
    use polars::polars_compute::rolling::RollingQuantileParams;
    use polars::prelude::{
        QuantileMethod, RollingFnParams, RollingOptionsFixedWindow, RollingVarParams, SeriesOpsTime,
    };
    if values.is_empty() {
        return Ok(Vec::new());
    }
    let s = Series::new("".into(), values.to_vec());
    let mut options = RollingOptionsFixedWindow {
        window_size: window,
        min_periods: min,
        weights: None,
        center: false,
        fn_params: None,
    };
    let out = match kind {
        Roll::Mean => s.rolling_mean(options)?,
        Roll::Sum => s.rolling_sum(options)?,
        Roll::Max => s.rolling_max(options)?,
        Roll::Std(ddof) => {
            options.fn_params = Some(RollingFnParams::Var(RollingVarParams { ddof }));
            s.rolling_std(options)?
        }
        Roll::Median => {
            options.fn_params = Some(RollingFnParams::Quantile(RollingQuantileParams {
                prob: 0.5,
                method: QuantileMethod::Linear,
            }));
            s.rolling_quantile(options)?
        }
    };
    series_values(&out)
}

fn rolling(
    values: &[F],
    groups: &[Range<usize>],
    kind: Roll,
    window: usize,
    min: usize,
) -> CoreResult<Vec<F>> {
    let mut out = Vec::with_capacity(values.len());
    for g in groups {
        out.extend(rolling_slice(&values[g.clone()], kind, window, min)?);
    }
    Ok(out)
}

/// `expr.ewm_mean(alpha, adjust=False, ignore_nulls=True).over(...)` — polars' kernel.
fn ewm_mean(values: &[F], groups: &[Range<usize>], alpha: f64) -> CoreResult<Vec<F>> {
    use polars::prelude::EWMOptions;
    let mut out = Vec::with_capacity(values.len());
    for g in groups {
        if g.is_empty() {
            continue;
        }
        let s = Series::new("".into(), values[g.clone()].to_vec());
        let r = polars::prelude::ewm_mean(
            &s,
            EWMOptions {
                alpha,
                adjust: false,
                bias: false,
                min_periods: 1,
                ignore_nulls: true,
            },
        )?;
        out.extend(series_values(&r)?);
    }
    Ok(out)
}

/// `expr.shift(k).over(...)`.
fn shift<T: Clone>(values: &[Option<T>], groups: &[Range<usize>], k: usize) -> Vec<Option<T>> {
    let mut out = vec![None; values.len()];
    for g in groups {
        for i in g.clone() {
            if i >= g.start + k {
                out[i] = values[i - k].clone();
            }
        }
    }
    out
}

/// `expr.diff().over(...)`.
fn diff(values: &[F], groups: &[Range<usize>]) -> Vec<F> {
    let lag = shift(values, groups, 1);
    values.iter().zip(&lag).map(|(a, b)| sub(*a, *b)).collect()
}

/// `expr.cum_max().over(...)`: NULL stays NULL, the running maximum skips it.
fn cum_max(values: &[F], groups: &[Range<usize>]) -> Vec<F> {
    let mut out = vec![None; values.len()];
    for g in groups {
        let mut best: F = None;
        for i in g.clone() {
            if let Some(v) = values[i] {
                best = Some(match best {
                    Some(b) if tot_cmp(b, v) != Ordering::Less => b,
                    _ => v,
                });
                out[i] = best;
            }
        }
    }
    out
}

// ---------------------------------------------------------------------------
// windows.py
// ---------------------------------------------------------------------------

mod windows {
    //! Port of `baskfy_core.windows`: calendar-offset factor windows (docs/05 §Notation as
    //! corrected by M11 — the window is every trading day in `(anniversary, as_of]`).

    use chrono::{Datelike, NaiveDate};
    use serde::Serialize;

    use crate::error::{CoreError, CoreResult};

    /// Ports `baskfy_core.windows.subtract_months` — `relativedelta(months=K)`, clamped to the
    /// target month's last day.
    ///
    /// # Errors
    ///
    /// [`CoreError::InvalidInput`] for negative `months` or a date out of range.
    pub fn subtract_months(day: NaiveDate, months: i64) -> CoreResult<NaiveDate> {
        if months < 0 {
            return Err(CoreError::InvalidInput(format!(
                "months must not be negative; got {months}"
            )));
        }
        let total = i64::from(day.year()) * 12 + i64::from(day.month()) - 1 - months;
        let year = total.div_euclid(12);
        let month = total.rem_euclid(12) + 1;
        let year = i32::try_from(year).map_err(|e| CoreError::InvalidInput(e.to_string()))?;
        let month = u32::try_from(month).map_err(|e| CoreError::InvalidInput(e.to_string()))?;
        let last = days_in_month(year, month)?;
        NaiveDate::from_ymd_opt(year, month, day.day().min(last))
            .ok_or_else(|| CoreError::InvalidInput(format!("date out of range: {year}-{month}")))
    }

    pub(super) fn days_in_month(year: i32, month: u32) -> CoreResult<u32> {
        if month == 12 {
            return Ok(31);
        }
        let next = NaiveDate::from_ymd_opt(year, month + 1, 1)
            .ok_or_else(|| CoreError::InvalidInput(format!("date out of range: {year}-{month}")))?;
        next.pred_opt()
            .map(|d| d.day())
            .ok_or_else(|| CoreError::InvalidInput("date out of range".to_owned()))
    }

    /// Ports `baskfy_core.windows.FactorWindow`: one resolved window.
    #[derive(Debug, Clone, PartialEq, Eq, Serialize)]
    pub struct FactorWindow {
        /// K.
        pub months: i64,
        /// The as-of date.
        pub as_of: NaiveDate,
        /// `as_of - relativedelta(months=K)` — the anniversary, excluded from the window.
        pub calendar_start: NaiveDate,
        /// The first trading day strictly after `calendar_start`.
        pub start: NaiveDate,
        /// Every trading day in `(calendar_start, as_of]`, in calendar order.
        pub trading_days: Vec<NaiveDate>,
        /// The first day of the calendar the window was resolved against.
        pub calendar_first: NaiveDate,
    }

    impl FactorWindow {
        /// Ports `spans_full_window`: did the calendar reach back to the offset?
        pub fn spans_full_window(&self) -> bool {
            self.calendar_first <= self.calendar_start
        }

        /// Ports `length`: N, the shared denominator.
        pub fn length(&self) -> usize {
            self.trading_days.len()
        }

        /// Ports `key`: `"12m"`, `"6m"`, …
        pub fn key(&self) -> String {
            format!("{}m", self.months)
        }
    }

    /// Python's `bisect.bisect_right`, literally (so an unsorted calendar behaves the same).
    fn bisect_right(items: &[NaiveDate], x: NaiveDate) -> usize {
        let (mut lo, mut hi) = (0, items.len());
        while lo < hi {
            let mid = (lo + hi) / 2;
            if x < items[mid] {
                hi = mid;
            } else {
                lo = mid + 1;
            }
        }
        lo
    }

    /// Ports `baskfy_core.windows.resolve_window`.
    ///
    /// # Errors
    ///
    /// [`CoreError::InvalidInput`] with Python's message: an empty calendar, an `as_of` not in
    /// it, or no trading day after the anniversary.
    pub fn resolve_window(
        as_of: NaiveDate,
        months: i64,
        trading_days: &[NaiveDate],
    ) -> CoreResult<FactorWindow> {
        let Some(first) = trading_days.first() else {
            return Err(CoreError::InvalidInput(
                "the trading calendar is empty".to_owned(),
            ));
        };
        if !trading_days.contains(&as_of) {
            return Err(CoreError::InvalidInput(format!(
                "{} is not a trading day in the supplied calendar",
                as_of.format("%Y-%m-%d")
            )));
        }
        let calendar_start = subtract_months(as_of, months)?;
        let index = bisect_right(trading_days, calendar_start);
        let Some(start) = trading_days.get(index) else {
            return Err(CoreError::InvalidInput(format!(
                "no trading day after {} in the supplied calendar",
                calendar_start.format("%Y-%m-%d")
            )));
        };
        let end_index = bisect_right(trading_days, as_of);
        let span = trading_days
            .get(index..end_index)
            .map(<[NaiveDate]>::to_vec)
            .unwrap_or_default();
        Ok(FactorWindow {
            months,
            as_of,
            calendar_start,
            start: *start,
            trading_days: span,
            calendar_first: *first,
        })
    }

    /// Ports `baskfy_core.windows.resolve_windows`: `{K: window}` in `months` order (a repeated
    /// K keeps its first position, as a Python dict does).
    ///
    /// # Errors
    ///
    /// As [`resolve_window`].
    pub fn resolve_windows(
        as_of: NaiveDate,
        trading_days: &[NaiveDate],
        months: &[i64],
    ) -> CoreResult<Vec<(i64, FactorWindow)>> {
        let mut out: Vec<(i64, FactorWindow)> = Vec::with_capacity(months.len());
        for &k in months {
            let w = resolve_window(as_of, k, trading_days)?;
            if let Some(slot) = out.iter_mut().find(|(m, _)| *m == k) {
                slot.1 = w;
            } else {
                out.push((k, w));
            }
        }
        Ok(out)
    }
}

// ---------------------------------------------------------------------------
// _prepare and the per-row factors
// ---------------------------------------------------------------------------

fn prepare(bars: &DataFrame, config: &FactorConfig) -> CoreResult<Long> {
    let mut df = bars.sort(
        ["instrument_id", "date"],
        SortMultipleOptions::default().with_maintain_order(true),
    )?;
    for column in OPTIONAL_COLUMNS {
        if !has(&df, column) {
            let dtype = if column == "series" {
                DataType::String
            } else {
                DataType::Float64
            };
            put_null(&mut df, column, &dtype)?;
        }
    }
    for column in [
        "close",
        "close_raw",
        "high",
        "low",
        "volume_raw",
        "turnover",
        "upper_circuit",
        "lower_circuit",
    ] {
        let cast = df.column(column)?.cast(&DataType::Float64)?;
        df.with_column(cast)?;
    }

    // The date column as days; a Date column is required for the as-of filter and the joins.
    let date_col = df.column("date")?.cast(&DataType::Date)?;
    df.with_column(date_col.clone())?;
    let dates: Vec<Option<i32>> = date_col
        .as_materialized_series()
        .cast(&DataType::Int32)?
        .i32()?
        .iter()
        .collect();

    // `.over("instrument_id")`'s groups: the runs of equal ids in the sorted frame.
    let ids = df.column("instrument_id")?.as_materialized_series().clone();
    let mut groups: Vec<Range<usize>> = Vec::new();
    let mut group_null: Vec<bool> = Vec::new();
    let mut group_of = Vec::with_capacity(df.height());
    let mut previous: Option<AnyValue<'_>> = None;
    for r in 0..ids.len() {
        let id = ids.get(r)?;
        let same = previous.as_ref().is_some_and(|p| *p == id);
        if same {
            if let Some(g) = groups.last_mut() {
                g.end = r + 1;
            }
        } else {
            groups.push(r..r + 1);
            group_null.push(matches!(id, AnyValue::Null));
        }
        group_of.push(groups.len().saturating_sub(1));
        previous = Some(id);
    }
    let mut long = Long {
        df,
        groups,
        group_of,
        group_null,
        dates,
    };

    let close_raw = floats(&long.df, "close_raw")?;
    let prev_close_raw = shift(&close_raw, &long.groups, 1);
    put(&mut long.df, "prev_close_raw", prev_close_raw.clone())?;

    // docs/05 §13: exchange turnover when present and positive, else close_raw x volume_raw.
    let turnover = floats(&long.df, "turnover")?;
    let volume = floats(&long.df, "volume_raw")?;
    let exchange: Vec<bool> = turnover
        .iter()
        .map(|t| taken(and(Some(t.is_some()), gt(*t, 0.0))))
        .collect();
    let vol_day_val: Vec<F> = (0..turnover.len())
        .map(|i| {
            if exchange[i] {
                turnover[i]
            } else {
                mul(close_raw[i], volume[i])
            }
        })
        .collect();
    put(&mut long.df, "vol_day_val", vol_day_val)?;
    let source: Vec<&str> = exchange
        .iter()
        .map(|e| {
            if *e {
                TURNOVER_EXCHANGE
            } else {
                TURNOVER_DERIVED
            }
        })
        .collect();
    long.df
        .with_column(Series::new("turnover_source".into(), source).into_column())?;

    // docs/05 §12 — INFERRED circuit detection and the method used, per row.
    let (hit, method) = circuits::hit_and_method(&long.df, &prev_close_raw, &config.circuits)?;
    long.df
        .with_column(Series::new("circuit_hit".into(), hit).into_column())?;
    long.df
        .with_column(Series::new("circuit_method".into(), method).into_column())?;
    Ok(long)
}

mod circuits {
    //! Port of `baskfy_core.circuits` (docs/05 §12, INFERRED).

    use polars::prelude::DataFrame;

    use super::{B, CircuitConfig, F, and, floats, ge2, le2, null, or, sub, taken};
    use crate::error::CoreResult;

    /// `circuit_hit_expr` and `circuit_method_expr`, row by row.
    pub(super) fn hit_and_method(
        df: &DataFrame,
        prev: &[F],
        config: &CircuitConfig,
    ) -> CoreResult<(Vec<bool>, Vec<&'static str>)> {
        let close_raw = floats(df, "close_raw")?;
        let high = floats(df, "high")?;
        let low = floats(df, "low")?;
        let upper = floats(df, "upper_circuit")?;
        let lower = floats(df, "lower_circuit")?;
        let tick = Some(config.tick);
        let mut hits = Vec::with_capacity(close_raw.len());
        let mut methods = Vec::with_capacity(close_raw.len());
        for i in 0..close_raw.len() {
            let at_upper = and(
                Some(upper[i].is_some()),
                ge2(close_raw[i], sub(upper[i], tick)),
            );
            let at_lower = and(
                Some(lower[i].is_some()),
                le2(close_raw[i], upper_plus(lower[i], config.tick)),
            );
            let published = or(at_upper, at_lower);
            let hit: B = if config.allow_band_heuristic {
                let flat_day = and(
                    super::eq2(high[i], low[i]),
                    super::eq2(low[i], close_raw[i]),
                );
                let moved = sub(super::div(close_raw[i], prev[i]), Some(1.0)).map(f64::abs);
                let mut on_a_band: B = Some(false);
                for band in &config.bands {
                    on_a_band = or(on_a_band, super::ge(moved, band - config.band_tolerance));
                }
                let heuristic = and(and(flat_day, on_a_band), Some(prev[i].is_some()));
                or(published, heuristic)
            } else {
                published
            };
            hits.push(taken(hit));
            let has_bands = upper[i].is_some() || lower[i].is_some();
            methods.push(if has_bands {
                "bhavcopy_bands"
            } else if !taken(null(prev[i])) {
                if config.allow_band_heuristic {
                    "band_heuristic"
                } else {
                    "undetermined"
                }
            } else {
                "undetermined"
            });
        }
        Ok((hits, methods))
    }

    fn upper_plus(x: F, tick: f64) -> F {
        x.map(|v| v + tick)
    }
}

/// docs/05 §Notation: `r_t = P_t / P_{t-1} - 1` on the adjusted close.
fn with_returns(long: &mut Long) -> CoreResult<()> {
    let close = floats(&long.df, "close")?;
    let previous = shift(&close, &long.groups, 1);
    let daily: Vec<F> = close
        .iter()
        .zip(&previous)
        .map(|(c, p)| {
            if taken(or(null(*p), eq(*p, 0.0))) {
                None
            } else {
                sub(div(*c, *p), Some(1.0))
            }
        })
        .collect();
    put(&mut long.df, "daily_return", daily)
}

/// docs/05 §1 returns, §2 volatility, §3 sharpe, §11 positive days, §12 circuits.
fn with_window_factors(long: &mut Long, windows: &[(i64, FactorWindow)]) -> CoreResult<()> {
    let close = floats(&long.df, "close")?;
    let daily = floats(&long.df, "daily_return")?;
    let hit = floats(&long.df, "circuit_hit")?;
    let positive: Vec<F> = daily.iter().map(|r| bool_f(gt(*r, 0.0))).collect();
    let groups = long.groups.clone();
    // All expressions of one `with_columns` read the frame as it was; computed first, put after.
    let mut columns: Vec<(String, Option<Vec<F>>)> = Vec::new();
    for (_, window) in windows {
        let n = window.length();
        let key = window.key();
        if !window.spans_full_window() {
            for prefix in ["ret", "vol", "sharpe", "rsi", "pos_days", "circuits"] {
                columns.push((format!("{prefix}_{key}"), None));
            }
            continue;
        }
        let base = shift(&close, &groups, n.saturating_sub(1));
        let ret: Vec<F> = close
            .iter()
            .zip(&base)
            .map(|(c, b)| {
                if taken(or(null(*b), eq(*b, 0.0))) {
                    None
                } else {
                    mul(sub(div(*c, *b), Some(1.0)), Some(100.0))
                }
            })
            .collect();
        columns.push((format!("ret_{key}"), Some(ret)));
        let vol = rolling(&daily, &groups, Roll::Std(VOL_DDOF), n, n)?
            .into_iter()
            .map(|v| v.map(|x| x * sqrt_year()))
            .collect();
        columns.push((format!("vol_{key}"), Some(vol)));
        // `rolling_sum(n) / n * 100`: polars divides a float column by a scalar as a
        // multiplication by the scalar's reciprocal (`prim_true_div_scalar`), so `x / n` here
        // is `x * (1 / n)`, one ulp away from a true division (10/22*100 is 45.45454545454546
        // in polars, 45.45454545454545 in IEEE division).
        let inv_n = 1.0 / as_f64(n);
        let pos = rolling(&positive, &groups, Roll::Sum, n, n)?
            .into_iter()
            .map(|v| v.map(|x| x * inv_n * 100.0))
            .collect();
        columns.push((format!("pos_days_{key}"), Some(pos)));
        let circuits = rolling(&hit, &groups, Roll::Sum, n, n)?;
        columns.push((format!("circuits_{key}"), Some(circuits)));
    }
    for (name, values) in columns {
        match values {
            Some(v) => put(&mut long.df, &name, v)?,
            None => put_null(&mut long.df, &name, &DataType::Float64)?,
        }
    }

    // §3: sharpe_N = ret_N_pct / (vol_N_fraction x 100), NULL on a zero or NULL volatility.
    let mut sharpes = Vec::new();
    for (months, _) in windows {
        let vol = floats(&long.df, &format!("vol_{months}m"))?;
        let ret = floats(&long.df, &format!("ret_{months}m"))?;
        let sharpe: Vec<F> = vol
            .iter()
            .zip(&ret)
            .map(|(v, r)| {
                let guard = or(
                    or(null(*v), lt(v.map(f64::abs), ZERO_VOLATILITY_EPSILON)),
                    null(*r),
                );
                if taken(guard) {
                    None
                } else {
                    div(*r, v.map(|x| x * 100.0))
                }
            })
            .collect();
        sharpes.push((format!("sharpe_{months}m"), sharpe));
    }
    for (name, v) in sharpes {
        put(&mut long.df, &name, v)?;
    }
    Ok(())
}

/// docs/05 §9 moving averages, §10 highs and away-from-high.
fn with_price_levels(long: &mut Long) -> CoreResult<()> {
    let close = floats(&long.df, "close")?;
    let high = floats(&long.df, "high")?;
    let groups = long.groups.clone();
    for k in MA_LENGTHS {
        let ma = rolling(&close, &groups, Roll::Mean, k, k)?;
        put(&mut long.df, &format!("ma_{k}"), ma)?;
    }
    let high_1y = rolling(&high, &groups, Roll::Max, HIGH_1Y_BARS, HIGH_1Y_BARS)?;
    let high_ath = cum_max(&high, &groups);
    put(&mut long.df, "high_1y", high_1y.clone())?;
    put(&mut long.df, "high_ath", high_ath.clone())?;
    for (h, name) in [(high_1y, "away_high_1y"), (high_ath, "away_high_ath")] {
        let away: Vec<F> = close
            .iter()
            .zip(&h)
            .map(|(c, x)| {
                if taken(or(null(*x), eq(*x, 0.0))) {
                    None
                } else {
                    mul(sub(div(*c, *x), Some(1.0)), Some(100.0))
                }
            })
            .collect();
        put(&mut long.df, name, away)?;
    }
    Ok(())
}

/// docs/05 §8 — INFERRED (`baskfy_core.momentum.skip_month_return_expr`).
fn with_skip_month(long: &mut Long, config: &FactorConfig) -> CoreResult<()> {
    let close = floats(&long.df, "close")?;
    let groups = long.groups.clone();
    let mut out = Vec::new();
    for (skip, name) in [
        (config.skip_month.skip_1m_bars, "ret_12m_minus_1m"),
        (config.skip_month.skip_2m_bars, "ret_12m_minus_2m"),
    ] {
        let (num_back, den_back) = config.skip_month.numerator_denominator(skip);
        let numerator = shift(&close, &groups, num_back);
        let denominator = shift(&close, &groups, den_back);
        let values: Vec<F> = numerator
            .iter()
            .zip(&denominator)
            .map(|(n, d)| {
                if taken(or(or(null(*d), eq(*d, 0.0)), null(*n))) {
                    None
                } else {
                    mul(sub(div(*n, *d), Some(1.0)), Some(100.0))
                }
            })
            .collect();
        out.push((name, values));
    }
    for (name, values) in out {
        put(&mut long.df, name, values)?;
    }
    Ok(())
}

/// docs/05 §13 — rolling means of daily traded value, and the 1-year median.
fn with_liquidity(long: &mut Long) -> CoreResult<()> {
    let value = floats(&long.df, "vol_day_val")?;
    let groups = long.groups.clone();
    for (key, bars) in VOL_AVG_BARS {
        let v = rolling(&value, &groups, Roll::Mean, bars, bars)?;
        put(&mut long.df, &format!("vol_avg_{key}"), v)?;
    }
    let median = rolling(
        &value,
        &groups,
        Roll::Median,
        MEDIAN_VOL_BARS,
        MEDIAN_VOL_BARS,
    )?;
    put(&mut long.df, "median_vol_12m", median)
}

/// docs/05 §5 — Cutler's RSI with period `N - 1`, over a date x instrument matrix exactly as
/// the Python pivots it (dates in order of discovery, a gap step skipping its windows).
fn with_rsi(long: &mut Long, windows: &[(i64, FactorWindow)]) -> CoreResult<()> {
    let set_empty = |df: &mut DataFrame| -> CoreResult<()> {
        for (months, _) in windows {
            put_null(df, &format!("rsi_{months}m"), &DataType::Float64)?;
        }
        Ok(())
    };
    if long.df.height() == 0 {
        return set_empty(&mut long.df);
    }
    // pivot(on=instrument_id, index=date, values=close): index rows in order of discovery.
    let mut date_keys: Vec<Option<i32>> = Vec::new();
    let mut row_date = Vec::with_capacity(long.df.height());
    for d in &long.dates {
        let k = match date_keys.iter().position(|x| x == d) {
            Some(k) => k,
            None => {
                date_keys.push(*d);
                date_keys.len() - 1
            }
        };
        row_date.push(k);
    }
    if date_keys.len() < 2 {
        return set_empty(&mut long.df);
    }
    let close = floats(&long.df, "close")?;
    let steps_n = date_keys.len();
    let inst_n = long.groups.len();
    let mut prices = vec![vec![f64::NAN; inst_n]; steps_n];
    let mut filled = vec![vec![false; inst_n]; steps_n];
    for r in 0..long.df.height() {
        let (d, g) = (row_date[r], long.group_of[r]);
        if filled[d][g] {
            return Err(CoreError::InvalidInput(
                "found multiple elements in the same group, please specify an aggregation \
                 function"
                    .to_owned(),
            ));
        }
        filled[d][g] = true;
        prices[d][g] = close[r].unwrap_or(f64::NAN);
    }
    let steps = steps_n - 1;
    let mut gains = vec![vec![0.0; inst_n]; steps];
    let mut losses = vec![vec![0.0; inst_n]; steps];
    for t in 0..steps {
        for i in 0..inst_n {
            let (a, b) = (prices[t][i], prices[t + 1][i]);
            if a.is_finite() && b.is_finite() {
                let change = b - a;
                gains[t][i] = if change > 0.0 { change } else { 0.0 };
                losses[t][i] = if change < 0.0 { -change } else { 0.0 };
            } else {
                gains[t][i] = f64::NAN;
                losses[t][i] = f64::NAN;
            }
        }
    }
    let mut additions = Vec::new();
    for (months, window) in windows {
        let period = window.length().saturating_sub(1);
        let rsi = numpy::cutler_rsi(&gains, &losses, period, window.length() >= 1);
        let values: Vec<F> = (0..long.df.height())
            .map(|r| {
                let d = row_date[r];
                if d == 0 {
                    None
                } else {
                    Some(rsi[d - 1][long.group_of[r]])
                }
            })
            .collect();
        additions.push((format!("rsi_{months}m"), values));
    }
    for (name, values) in additions {
        join_put(
            &mut long.df,
            &name,
            Series::new(name.as_str().into(), values),
        )?;
    }
    Ok(())
}

/// docs/05 §6 — `Cov(r_stock, r_bench)_252 / Var(r_bench)_252` against NIFTY 50.
fn with_beta(
    long: &mut Long,
    benchmark: Option<&DataFrame>,
    config: &FactorConfig,
) -> CoreResult<()> {
    let Some(levels) = ranking::benchmark_levels(benchmark)? else {
        return put_null(&mut long.df, "beta_12m", &DataType::Float64);
    };
    // bench_return = level / level.shift(1) - 1 over the sorted series, joined on date.
    let mut returns: Vec<(Option<i32>, F)> = Vec::with_capacity(levels.len());
    for (i, (d, l)) in levels.iter().enumerate() {
        let previous = if i == 0 { None } else { levels[i - 1].1 };
        returns.push((*d, sub(div(*l, previous), Some(1.0))));
    }
    let bench: Vec<F> = long
        .dates
        .iter()
        .map(|d| {
            d.and_then(|day| {
                returns
                    .iter()
                    .find(|(x, _)| *x == Some(day))
                    .and_then(|(_, v)| *v)
            })
        })
        .collect();
    join_put(
        &mut long.df,
        "bench_return",
        Series::new("bench_return".into(), bench.clone()),
    )?;

    let daily = floats(&long.df, "daily_return")?;
    let n = TRADING_DAYS_PER_YEAR;
    let min = config.min_beta_observations;
    let mut stock = Vec::with_capacity(daily.len());
    let mut market = Vec::with_capacity(daily.len());
    let mut product = Vec::with_capacity(daily.len());
    let mut market_sq = Vec::with_capacity(daily.len());
    for (s, m) in daily.iter().zip(&bench) {
        let paired = s.is_some() && m.is_some();
        let (s, m) = if paired { (*s, *m) } else { (None, None) };
        stock.push(s);
        market.push(m);
        product.push(mul(s, m));
        market_sq.push(m.map(|x| x.powi(2)));
    }
    let groups = long.groups.clone();
    let mean_stock = rolling(&stock, &groups, Roll::Mean, n, min)?;
    let mean_bench = rolling(&market, &groups, Roll::Mean, n, min)?;
    let mean_product = rolling(&product, &groups, Roll::Mean, n, min)?;
    let mean_bench_sq = rolling(&market_sq, &groups, Roll::Mean, n, min)?;
    let beta: Vec<F> = (0..daily.len())
        .map(|i| {
            let covariance = sub(mean_product[i], mul(mean_stock[i], mean_bench[i]));
            let variance = sub(mean_bench_sq[i], mean_bench[i].map(|x| x.powi(2)));
            if taken(or(null(variance), le(variance, 0.0))) {
                None
            } else {
                div(covariance, variance)
            }
        })
        .collect();
    put(&mut long.df, "beta_12m", beta)
}

/// docs/05 §Notation: an instrument without a full window gets NULL for that window.
fn null_short_windows(frame: &mut DataFrame, windows: &[(i64, FactorWindow)]) -> CoreResult<()> {
    let mut out = Vec::new();
    for (months, _) in windows {
        let key = format!("{months}m");
        let vol = floats(frame, &format!("vol_{key}"))?;
        let ret = floats(frame, &format!("ret_{key}"))?;
        for prefix in ["sharpe", "pos_days", "circuits"] {
            let column = format!("{prefix}_{key}");
            if !has(frame, &column) {
                continue;
            }
            let values = floats(frame, &column)?;
            let masked: Vec<F> = (0..values.len())
                .map(|i| {
                    if vol[i].is_none() || ret[i].is_none() {
                        None
                    } else {
                        values[i]
                    }
                })
                .collect();
            out.push((column, masked));
        }
    }
    for (name, values) in out {
        put(frame, &name, values)?;
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// NumPy, reproduced
// ---------------------------------------------------------------------------

mod numpy {
    //! The NumPy operations the Python calls, in NumPy's own operation order.

    /// `np.add.reduce` over a contiguous row: the identity (0.0) plus NumPy's pairwise sum
    /// (blocks of 8 accumulators below 128 elements, halves above).
    pub(super) fn sum(a: &[f64]) -> f64 {
        0.0 + pairwise(a)
    }

    fn pairwise(a: &[f64]) -> f64 {
        let n = a.len();
        if n < 8 {
            let mut res = 0.0;
            for x in a {
                res += x;
            }
            res
        } else if n <= 128 {
            let mut r = [0.0; 8];
            r.copy_from_slice(&a[..8]);
            let mut i = 8;
            while i < n - (n % 8) {
                for j in 0..8 {
                    r[j] += a[i + j];
                }
                i += 8;
            }
            let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
            while i < n {
                res += a[i];
                i += 1;
            }
            res
        } else {
            let mut n2 = n / 2;
            n2 -= n2 % 8;
            pairwise(&a[..n2]) + pairwise(&a[n2..])
        }
    }

    /// `np.mean`.
    pub(super) fn mean(a: &[f64]) -> f64 {
        sum(a) / super::as_f64(a.len())
    }

    /// `np.linspace(0.0, 1.0, num)`.
    pub(super) fn linspace01(num: usize) -> Vec<f64> {
        if num == 0 {
            return Vec::new();
        }
        if num == 1 {
            return vec![0.0];
        }
        let step = 1.0 / super::as_f64(num - 1);
        let mut y: Vec<f64> = (0..num).map(|i| super::as_f64(i) * step + 0.0).collect();
        if let Some(last) = y.last_mut() {
            *last = 1.0;
        }
        y
    }

    /// `np.quantile(a, q, method="linear")` for one `q`, `sorted` ascending and non-empty.
    pub(super) fn quantile_linear(sorted: &[f64], q: f64) -> f64 {
        let n = sorted.len();
        let count = super::as_f64(n);
        // NumPy's `_QuantileMethods["linear"]["get_virtual_index"]` is `(n - 1) * quantiles`
        // (numpy 2.x), not the generic `_compute_virtual_index` (`n*q + (1 - q) - 1`), which
        // differs in the last bits and moved `regime_distance_*` by up to ~3e-14 relative.
        let virtual_index = (count - 1.0) * q;
        let (previous, next): (usize, usize);
        let previous_f: f64;
        if virtual_index >= count - 1.0 {
            previous = n - 1;
            next = n - 1;
            previous_f = -1.0;
        } else if virtual_index < 0.0 {
            previous = 0;
            next = 0;
            previous_f = 0.0;
        } else {
            let floor = virtual_index.floor();
            // `floor` is in [0, n - 1): the conversion is exact.
            #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
            let p = floor as usize;
            previous = p;
            next = (p + 1).min(n - 1);
            previous_f = floor;
        }
        let gamma = virtual_index - previous_f;
        lerp(sorted[previous], sorted[next], gamma)
    }

    /// NumPy's `_lerp`: `a + (b - a) t`, or `b - (b - a)(1 - t)` when `t >= 0.5`.
    fn lerp(a: f64, b: f64, t: f64) -> f64 {
        let diff = b - a;
        if t >= 0.5 {
            b - diff * (1.0 - t)
        } else {
            a + diff * t
        }
    }

    /// `_cutler_rsi` over a `(time, instrument)` matrix: a rolling simple mean written as a
    /// difference of cumulative sums, NaN where a window holds a gap step.
    pub(super) fn cutler_rsi(
        gains: &[Vec<f64>],
        losses: &[Vec<f64>],
        period: usize,
        has_returns: bool,
    ) -> Vec<Vec<f64>> {
        let steps = gains.len();
        let instruments = gains.first().map_or(0, Vec::len);
        let mut out = vec![vec![f64::NAN; instruments]; steps];
        if !has_returns || period < 1 || steps < period {
            return out;
        }
        let p = super::as_f64(period);
        for i in 0..instruments {
            let mut cg = Vec::with_capacity(steps + 1);
            let mut cl = Vec::with_capacity(steps + 1);
            let mut cgap = Vec::with_capacity(steps + 1);
            let (mut g, mut l, mut gap) = (0.0_f64, 0.0_f64, 0.0_f64);
            cg.push(g);
            cl.push(l);
            cgap.push(gap);
            for t in 0..steps {
                let is_gap = gains[t][i].is_nan() || losses[t][i].is_nan();
                g += if is_gap { 0.0 } else { gains[t][i] };
                l += if is_gap { 0.0 } else { losses[t][i] };
                gap += if is_gap { 1.0 } else { 0.0 };
                cg.push(g);
                cl.push(l);
                cgap.push(gap);
            }
            for k in 0..=(steps - period) {
                let avg_gain = (cg[period + k] - cg[k]) / p;
                let avg_loss = (cl[period + k] - cl[k]) / p;
                let gaps = cgap[period + k] - cgap[k];
                let value = rsi_from(avg_gain, avg_loss);
                out[period - 1 + k][i] = if gaps > 0.0 { f64::NAN } else { value };
            }
        }
        out
    }

    /// `_rsi_from`: `100 - 100/(1 + RS)`; no losses is 100 (50 when nothing moved).
    fn rsi_from(avg_gain: f64, avg_loss: f64) -> f64 {
        if avg_loss.abs() <= 0.0 {
            if avg_gain.abs() <= 0.0 { 50.0 } else { 100.0 }
        } else {
            let rs = avg_gain / avg_loss;
            100.0 - 100.0 / (1.0 + rs)
        }
    }
}

// ---------------------------------------------------------------------------
// instrument_regime.py
// ---------------------------------------------------------------------------

mod regime {
    //! Port of `baskfy_core.instrument_regime` — the per-instrument Wasserstein regime
    //! (docs/05 §15, INFERRED design).

    use polars::prelude::{DataType, NamedFrom, Series};

    use super::{Long, RegimeConfig, floats, join_put, numpy, put_null, tot_cmp};
    use crate::error::CoreResult;

    /// `wasserstein_1d`: mean absolute difference of the two samples' quantiles on a common
    /// grid of `max(len)` points.
    fn wasserstein_1d(left: &[f64], right: &[f64]) -> f64 {
        if left.is_empty() || right.is_empty() {
            return f64::NAN;
        }
        let mut l = left.to_vec();
        let mut r = right.to_vec();
        l.sort_by(|a, b| tot_cmp(*a, *b));
        r.sort_by(|a, b| tot_cmp(*a, *b));
        let grid = numpy::linspace01(l.len().max(r.len()));
        let diffs: Vec<f64> = grid
            .iter()
            .map(|q| (numpy::quantile_linear(&l, *q) - numpy::quantile_linear(&r, *q)).abs())
            .collect();
        numpy::mean(&diffs)
    }

    /// `classify_series`: `(regime, distance_to_bull, distance_to_bear)`.
    pub(super) fn classify_series(
        returns: &[f64],
        config: &RegimeConfig,
    ) -> (&'static str, f64, f64) {
        let clean: Vec<f64> = returns.iter().copied().filter(|x| !x.is_nan()).collect();
        let n = config.sample_bars;
        if clean.len() < n {
            return ("NEUTRAL", f64::NAN, f64::NAN);
        }
        let current = &clean[clean.len() - n..];
        let starts = clean.len() - n + 1;
        if starts < config.min_windows {
            return ("NEUTRAL", f64::NAN, f64::NAN);
        }
        let windows: Vec<&[f64]> = (0..starts).map(|s| &clean[s..s + n]).collect();
        let performance: Vec<f64> = windows.iter().map(|w| numpy::sum(w)).collect();
        let mut order: Vec<usize> = (0..starts).collect();
        order.sort_by(|a, b| tot_cmp(performance[*a], performance[*b]));
        // `max(1, round(starts * tail_fraction))` — Python's round is half to even.
        let scaled = (super::as_f64(starts) * config.tail_fraction).round_ties_even();
        #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
        let tail = (scaled.max(0.0) as usize).max(1).min(starts);
        let bear: Vec<f64> = order[..tail]
            .iter()
            .flat_map(|&i| windows[i].iter().copied())
            .collect();
        let bull: Vec<f64> = order[starts - tail..]
            .iter()
            .flat_map(|&i| windows[i].iter().copied())
            .collect();
        let to_bull = wasserstein_1d(current, &bull);
        let to_bear = wasserstein_1d(current, &bear);
        if !to_bull.is_finite() || !to_bear.is_finite() {
            return ("NEUTRAL", to_bull, to_bear);
        }
        let threshold = 1.0 - config.margin;
        if to_bull < to_bear * threshold {
            return ("BULL", to_bull, to_bear);
        }
        if to_bear < to_bull * threshold {
            return ("BEAR", to_bull, to_bear);
        }
        ("NEUTRAL", to_bull, to_bear)
    }

    /// `classify_regimes`: label each instrument once over its whole history and keep the label
    /// only on the frame's latest date.
    ///
    /// KNOWN BUG (DECISIONS-RS R1.4), reproduced: "latest date" is the frame's, not `as_of`.
    pub(super) fn classify_regimes(long: &mut Long, config: &RegimeConfig) -> CoreResult<()> {
        let h = long.df.height();
        if h == 0 {
            put_null(&mut long.df, "regime", &DataType::String)?;
            put_null(&mut long.df, "regime_distance_bull", &DataType::Float64)?;
            put_null(&mut long.df, "regime_distance_bear", &DataType::Float64)?;
            return Ok(());
        }
        let daily = floats(&long.df, "daily_return")?;
        let mut labels: Vec<(&'static str, Option<f64>, Option<f64>)> = Vec::new();
        for g in &long.groups {
            let returns: Vec<f64> = daily[g.clone()]
                .iter()
                .map(|x| x.unwrap_or(f64::NAN))
                .collect();
            let (regime, bull, bear) = classify_series(&returns, config);
            let finite = |x: f64| if x.is_nan() { None } else { Some(x) };
            labels.push((regime, finite(bull), finite(bear)));
        }
        let last_date = long.dates.iter().flatten().max().copied();
        let keep = |r: usize| last_date.is_some() && long.dates[r] == last_date;
        let mut regime: Vec<Option<&str>> = Vec::with_capacity(h);
        let mut bull: Vec<Option<f64>> = Vec::with_capacity(h);
        let mut bear: Vec<Option<f64>> = Vec::with_capacity(h);
        for r in 0..h {
            let g = long.group_of[r];
            // A join on a NULL instrument_id never matches.
            if keep(r) && !long.group_null[g] {
                regime.push(Some(labels[g].0));
                bull.push(labels[g].1);
                bear.push(labels[g].2);
            } else {
                regime.push(None);
                bull.push(None);
                bear.push(None);
            }
        }
        // `pl.DataFrame(labels, strict=False)` types a distance column Null when every
        // instrument's distance is None.
        let typed = |name: &str, values: Vec<Option<f64>>, any: bool| {
            if any {
                Series::new(name.into(), values)
            } else {
                Series::new_null(name.into(), values.len())
            }
        };
        let any_bull = labels.iter().any(|l| l.1.is_some());
        let any_bear = labels.iter().any(|l| l.2.is_some());
        join_put(&mut long.df, "regime", Series::new("regime".into(), regime))?;
        join_put(
            &mut long.df,
            "regime_distance_bull",
            typed("regime_distance_bull", bull, any_bull),
        )?;
        join_put(
            &mut long.df,
            "regime_distance_bear",
            typed("regime_distance_bear", bear, any_bear),
        )?;
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// factors_ranking.py — the C1 ranking factors
// ---------------------------------------------------------------------------

mod ranking {
    //! Port of `baskfy_core.factors_ranking`'s two entry points, `with_ranking_series` and
    //! `with_ranking_at_as_of`, and the helpers they call (docs/ranking/PLAN.md, contract C1).

    use std::ops::Range;

    use chrono::{Datelike, NaiveDate};
    use polars::prelude::{DataFrame, DataType, NamedFrom, Series};

    use super::{
        B, F, FactorWindow, Long, Roll, as_f64, bool_f, days, diff, div, drop_if_present, ewm_mean,
        floats, gt2, has, join_put, le, lt, mul, null, numpy, or, put, put_null, resolve_window,
        rolling, shift, sqrt_year, sub, taken, tot_cmp,
    };
    use crate::error::{CoreError, CoreResult};

    /// A benchmark as `(date as days, level)`, ascending.
    type Levels = Vec<(Option<i32>, F)>;

    const ATR_PERIOD: usize = 14;
    const MA_SLOPE_LOOKBACK: usize = 20;
    const EFFICIENCY_CHANGES: usize = 63;
    const ACCEL_RECENT: usize = 21;
    const ACCEL_PRIOR: usize = 105;
    const VOLUME_RECENT: usize = 21;
    const VOLUME_BASE: usize = 126;
    const VOLUME_PERSIST_SESSIONS: usize = 20;
    const RS_PERSIST_SESSIONS: usize = 126;
    const RS_RETURN_SESSIONS: usize = 20;
    const TOP_DAYS_EXCLUDED: usize = 3;
    const NSE_SIGMA_SESSIONS: usize = 252;
    const NSE_MR6_BASE_MONTHS: i64 = 7;
    const NSE_MR12_BASE_MONTHS: i64 = 13;
    const NSE_RETURN_END_MONTHS: i64 = 1;
    const RANKING_WINDOW_MONTHS: [i64; 3] = [3, 6, 12];
    const ZERO_DENOMINATOR_EPSILON: f64 = 0.5e-10;
    const ZERO_ATR_EPSILON: f64 = 0.5e-4;

    const LOG_RETURN: &str = "_rk_log_return";
    const TRUE_RANGE: &str = "_rk_true_range";
    const TR_SEED: &str = "_rk_tr_seed";
    const TR_SEEDS_SEEN: &str = "_rk_tr_seeds_seen";
    const VOLUME_BASELINE: &str = "_rk_volume_baseline";
    const BENCH_RETURN_20: &str = "_rk_bench_return_20";
    const NSE_SIGMA: &str = "_rk_nse_sigma";
    const INTERNAL_COLUMNS: [&str; 7] = [
        LOG_RETURN,
        TRUE_RANGE,
        TR_SEED,
        TR_SEEDS_SEEN,
        VOLUME_BASELINE,
        BENCH_RETURN_20,
        NSE_SIGMA,
    ];

    /// `RANKING_NUMERIC_TYPES`: `numeric(precision, scale)` of every decimal C1 column.
    const RANKING_NUMERIC_TYPES: [(&str, i32, i32); 24] = [
        ("atr_14", 18, 4),
        ("atr_ext_20", 10, 4),
        ("ma50_slope_20", 14, 2),
        ("eff_ratio_63", 10, 4),
        ("max_dd_6m", 14, 2),
        ("max_dd_12m", 14, 2),
        ("downside_vol_6m", 18, 10),
        ("downside_vol_12m", 18, 10),
        ("sortino_6m", 14, 2),
        ("sortino_12m", 14, 2),
        ("underwater_12m", 7, 2),
        ("ret_ex_top3_12m", 14, 2),
        ("accel_21_105", 18, 10),
        ("accel_21_105_vs", 10, 4),
        ("vol_exp_21_126", 10, 4),
        ("excess_ret_3m", 14, 2),
        ("excess_ret_6m", 14, 2),
        ("excess_ret_12m", 14, 2),
        ("resid_ret_12m", 14, 2),
        ("rs_persist_126", 7, 2),
        ("mom_pctile", 7, 2),
        ("rank_persist_20", 7, 2),
        ("nse_mr6", 18, 10),
        ("nse_mr12", 18, 10),
    ];

    fn window(windows: &[(i64, FactorWindow)], months: i64) -> CoreResult<&FactorWindow> {
        windows
            .iter()
            .find(|(m, _)| *m == months)
            .map(|(_, w)| w)
            .ok_or_else(|| CoreError::InvalidInput(format!("no {months}m ranking window")))
    }

    /// `resolve_ranking_windows`: the 3M, 6M and 12M windows, resolved as `ret_Nm`'s are.
    pub(super) fn resolve_ranking_windows(
        as_of: NaiveDate,
        trading_days: &[NaiveDate],
    ) -> CoreResult<Vec<(i64, FactorWindow)>> {
        RANKING_WINDOW_MONTHS
            .iter()
            .map(|&m| Ok((m, resolve_window(as_of, m, trading_days)?)))
            .collect()
    }

    /// `benchmark_levels`: `(date, level)` ascending, NULL levels kept; `None` when absent.
    pub(super) fn benchmark_levels(frame: Option<&DataFrame>) -> CoreResult<Option<Levels>> {
        let Some(frame) = frame else {
            return Ok(None);
        };
        if frame.height() == 0 {
            return Ok(None);
        }
        let column = if has(frame, "level") {
            "level"
        } else if has(frame, "close") {
            "close"
        } else {
            let got: Vec<String> = frame
                .get_column_names()
                .iter()
                .map(|c| c.to_string())
                .collect();
            return Err(CoreError::InvalidInput(format!(
                "a benchmark needs a 'level' (or 'close') column; got {}",
                super::py_list(&got)
            )));
        };
        let dates: Vec<Option<i32>> = frame
            .column("date")?
            .cast(&DataType::Date)?
            .as_materialized_series()
            .cast(&DataType::Int32)?
            .i32()?
            .iter()
            .collect();
        let levels = floats(frame, column)?;
        let mut pairs: Vec<(Option<i32>, F)> = dates.into_iter().zip(levels).collect();
        // `.sort("date")`: stable, nulls first.
        pairs.sort_by_key(|p| p.0);
        let mut seen: Vec<Option<i32>> = pairs.iter().map(|p| p.0).collect();
        seen.dedup();
        if seen.len() != pairs.len() {
            return Err(CoreError::InvalidInput(
                "a benchmark series carries the same date twice".to_owned(),
            ));
        }
        Ok(Some(pairs))
    }

    fn level_on(levels: Option<&Levels>, day: NaiveDate) -> CoreResult<F> {
        let Some(levels) = levels else {
            return Ok(None);
        };
        let d = Some(days(day)?);
        Ok(levels
            .iter()
            .find(|(x, _)| *x == d)
            .and_then(|(_, v)| *v)
            .filter(|v| v.is_finite()))
    }

    /// `benchmark_window_return`: the index's return over `ret_Nm`'s window, in percent.
    fn benchmark_window_return(
        levels: Option<&Levels>,
        as_of: NaiveDate,
        window: &FactorWindow,
    ) -> CoreResult<F> {
        if !window.spans_full_window() {
            return Ok(None);
        }
        let end = level_on(levels, as_of)?;
        let start = level_on(levels, window.start)?;
        match (end, start) {
            (Some(e), Some(s)) if s > 0.0 => Ok(Some((e / s - 1.0) * 100.0)),
            _ => Ok(None),
        }
    }

    /// `month_end_trading_day`: the last calendar trading day in the month `months_back`
    /// before `as_of`'s.
    fn month_end_trading_day(
        trading_days: &[NaiveDate],
        as_of: NaiveDate,
        months_back: i64,
    ) -> CoreResult<Option<NaiveDate>> {
        if months_back < 1 {
            return Err(CoreError::InvalidInput(format!(
                "months_back must be at least 1; got {months_back}"
            )));
        }
        let total = i64::from(as_of.year()) * 12 + i64::from(as_of.month()) - 1 - months_back;
        let year = i32::try_from(total.div_euclid(12))
            .map_err(|e| CoreError::InvalidInput(e.to_string()))?;
        let month = u32::try_from(total.rem_euclid(12) + 1)
            .map_err(|e| CoreError::InvalidInput(e.to_string()))?;
        let first = NaiveDate::from_ymd_opt(year, month, 1)
            .ok_or_else(|| CoreError::InvalidInput("date out of range".to_owned()))?;
        let last =
            NaiveDate::from_ymd_opt(year, month, super::windows::days_in_month(year, month)?)
                .ok_or_else(|| CoreError::InvalidInput("date out of range".to_owned()))?;
        Ok(trading_days
            .iter()
            .filter(|d| first <= **d && **d <= last)
            .max()
            .copied())
    }

    // --- with_ranking_series ------------------------------------------------------------

    /// `with_ranking_series`: every C1 column that is a rolling expression over history.
    pub(super) fn with_ranking_series(
        long: &mut Long,
        windows: &[(i64, FactorWindow)],
        market_benchmark: Option<&DataFrame>,
    ) -> CoreResult<()> {
        let groups = long.groups.clone();
        let close = floats(&long.df, "close")?;
        let high = floats(&long.df, "high")?;
        let low = floats(&long.df, "low")?;
        let previous = shift(&close, &groups, 1);
        let mut log_return = Vec::with_capacity(close.len());
        let mut true_range = Vec::with_capacity(close.len());
        for i in 0..close.len() {
            let (c, p) = (close[i], previous[i]);
            let guard = or(or(or(null(p), le(p, 0.0)), null(c)), le(c, 0.0));
            log_return.push(if taken(guard) {
                None
            } else {
                div(c, p).map(f64::ln)
            });
            let tr_guard = or(or(null(high[i]), null(low[i])), null(p));
            true_range.push(if taken(tr_guard) {
                None
            } else {
                let a = sub(high[i], low[i]);
                let b = sub(high[i], p).map(f64::abs);
                let c2 = sub(low[i], p).map(f64::abs);
                max_horizontal(&[a, b, c2])
            });
        }
        put(&mut long.df, LOG_RETURN, log_return.clone())?;
        put(&mut long.df, TRUE_RANGE, true_range.clone())?;

        with_atr(long, &groups, &true_range, &close)?;
        with_trend(long, &groups, &close)?;
        with_downside_volatility(long, &groups, windows)?;
        with_acceleration(long, &groups, &log_return)?;
        with_participation(long, &groups)?;
        with_relative_strength(long, &groups, &close, market_benchmark)?;
        let sigma = rolling(
            &log_return,
            &groups,
            Roll::Std(1),
            NSE_SIGMA_SESSIONS,
            NSE_SIGMA_SESSIONS,
        )?
        .into_iter()
        .map(|v| v.map(|x| x * sqrt_year()))
        .collect();
        put(&mut long.df, NSE_SIGMA, sigma)
    }

    /// `pl.max_horizontal`: the largest non-null value.
    fn max_horizontal(values: &[F]) -> F {
        let mut best: F = None;
        for v in values.iter().flatten() {
            best = Some(match best {
                Some(b) if tot_cmp(b, *v) != std::cmp::Ordering::Less => b,
                _ => *v,
            });
        }
        best
    }

    /// `_with_atr`: Wilder's ATR(14) seeded by the 14-TR mean, and `atr_ext_20`.
    fn with_atr(long: &mut Long, groups: &[Range<usize>], tr: &[F], close: &[F]) -> CoreResult<()> {
        let seed = rolling(tr, groups, Roll::Mean, ATR_PERIOD, ATR_PERIOD)?;
        put(&mut long.df, TR_SEED, seed.clone())?;
        let mut seen: Vec<Option<i64>> = vec![None; tr.len()];
        for g in groups {
            let mut count = 0_i64;
            for i in g.clone() {
                count += i64::from(seed[i].is_some());
                seen[i] = Some(count);
            }
        }
        long.df
            .with_column(polars::prelude::IntoColumn::into_column(Series::new(
                TR_SEEDS_SEEN.into(),
                seen.clone(),
            )))?;
        let wilder: Vec<F> = (0..tr.len())
            .map(|i| {
                if seen[i] == Some(0) {
                    None
                } else if seen[i] == Some(1) && seed[i].is_some() {
                    seed[i]
                } else {
                    tr[i]
                }
            })
            .collect();
        let atr = ewm_mean(&wilder, groups, 1.0 / super::as_f64(ATR_PERIOD))?;
        put(&mut long.df, "atr_14", atr.clone())?;
        let ma_20 = floats(&long.df, "ma_20")?;
        let ext: Vec<F> = (0..tr.len())
            .map(|i| {
                let guard = or(
                    or(
                        or(null(atr[i]), lt(atr[i], ZERO_ATR_EPSILON)),
                        null(ma_20[i]),
                    ),
                    null(close[i]),
                );
                if taken(guard) {
                    None
                } else {
                    div(sub(close[i], ma_20[i]), atr[i])
                }
            })
            .collect();
        put(&mut long.df, "atr_ext_20", ext)
    }

    /// `_with_trend`: `ma50_slope_20` and `eff_ratio_63`.
    fn with_trend(long: &mut Long, groups: &[Range<usize>], close: &[F]) -> CoreResult<()> {
        let ma_50 = floats(&long.df, "ma_50")?;
        let ma_base = shift(&ma_50, groups, MA_SLOPE_LOOKBACK);
        let lagged = shift(close, groups, EFFICIENCY_CHANGES);
        let net: Vec<F> = close
            .iter()
            .zip(&lagged)
            .map(|(c, l)| sub(*c, *l).map(f64::abs))
            .collect();
        let moves: Vec<F> = diff(close, groups)
            .into_iter()
            .map(|v| v.map(f64::abs))
            .collect();
        let path = rolling(
            &moves,
            groups,
            Roll::Sum,
            EFFICIENCY_CHANGES,
            EFFICIENCY_CHANGES,
        )?;
        let mut slope = Vec::with_capacity(close.len());
        let mut eff = Vec::with_capacity(close.len());
        for i in 0..close.len() {
            let guard = or(or(null(ma_50[i]), null(ma_base[i])), le(ma_base[i], 0.0));
            slope.push(if taken(guard) {
                None
            } else {
                mul(sub(div(ma_50[i], ma_base[i]), Some(1.0)), Some(100.0))
            });
            let guard = or(
                or(null(net[i]), null(path[i])),
                lt(path[i], ZERO_DENOMINATOR_EPSILON),
            );
            eff.push(if taken(guard) {
                None
            } else {
                div(net[i], path[i])
            });
        }
        put(&mut long.df, "ma50_slope_20", slope)?;
        put(&mut long.df, "eff_ratio_63", eff)
    }

    /// `_with_downside_volatility`: sqrt(mean of min(r, 0)^2 over N returns) x sqrt(252).
    fn with_downside_volatility(
        long: &mut Long,
        groups: &[Range<usize>],
        windows: &[(i64, FactorWindow)],
    ) -> CoreResult<()> {
        let returns = floats(&long.df, "daily_return")?;
        let squared: Vec<F> = returns
            .iter()
            .map(|r| match r {
                None => None,
                Some(x) if taken(lt(Some(*x), 0.0)) => Some(x * x),
                Some(_) => Some(0.0),
            })
            .collect();
        let mut out = Vec::new();
        for months in [6, 12] {
            let w = window(windows, months)?;
            let name = format!("downside_vol_{months}m");
            if !w.spans_full_window() {
                out.push((name, None));
                continue;
            }
            let n = w.length();
            let values: Vec<F> = rolling(&squared, groups, Roll::Mean, n, n)?
                .into_iter()
                .map(|v| {
                    v.map(|x| {
                        // `.clip(lower_bound=0.0)` then `.sqrt() * sqrt(252)`.
                        let clipped = if taken(lt(Some(x), 0.0)) { 0.0 } else { x };
                        clipped.sqrt() * sqrt_year()
                    })
                })
                .collect();
            out.push((name, Some(values)));
        }
        for (name, values) in out {
            match values {
                Some(v) => put(&mut long.df, &name, v)?,
                None => put_null(&mut long.df, &name, &DataType::Float64)?,
            }
        }
        Ok(())
    }

    /// `_with_acceleration`: `accel_21_105` and its volatility-scaled form.
    fn with_acceleration(long: &mut Long, groups: &[Range<usize>], logs: &[F]) -> CoreResult<()> {
        let recent = rolling(logs, groups, Roll::Mean, ACCEL_RECENT, ACCEL_RECENT)?;
        let prior_mean = rolling(logs, groups, Roll::Mean, ACCEL_PRIOR, ACCEL_PRIOR)?;
        let prior = shift(&prior_mean, groups, ACCEL_RECENT);
        let span = ACCEL_RECENT + ACCEL_PRIOR;
        let spread = rolling(logs, groups, Roll::Std(1), span, span)?;
        let accel: Vec<F> = recent
            .iter()
            .zip(&prior)
            .map(|(r, p)| sub(*r, *p))
            .collect();
        let scaled: Vec<F> = (0..accel.len())
            .map(|i| {
                let guard = or(
                    or(null(accel[i]), null(spread[i])),
                    lt(spread[i], ZERO_DENOMINATOR_EPSILON),
                );
                if taken(guard) {
                    None
                } else {
                    div(accel[i], spread[i])
                }
            })
            .collect();
        put(&mut long.df, "accel_21_105", accel)?;
        put(&mut long.df, "accel_21_105_vs", scaled)
    }

    /// `_with_participation`: `vol_exp_21_126` and `vol_persist_20` over `vol_day_val`.
    fn with_participation(long: &mut Long, groups: &[Range<usize>]) -> CoreResult<()> {
        let value = floats(&long.df, "vol_day_val")?;
        let recent = rolling(&value, groups, Roll::Mean, VOLUME_RECENT, VOLUME_RECENT)?;
        let base_mean = rolling(&value, groups, Roll::Mean, VOLUME_BASE, VOLUME_BASE)?;
        let base = shift(&base_mean, groups, VOLUME_RECENT);
        let expansion: Vec<F> = (0..value.len())
            .map(|i| {
                let guard = or(or(null(recent[i]), null(base[i])), le(base[i], 0.0));
                if taken(guard) {
                    None
                } else {
                    div(recent[i], base[i])
                }
            })
            .collect();
        let baseline = shift(&base_mean, groups, VOLUME_PERSIST_SESSIONS);
        put(&mut long.df, "vol_exp_21_126", expansion)?;
        put(&mut long.df, VOLUME_BASELINE, baseline.clone())?;

        let lags: Vec<Vec<F>> = (0..VOLUME_PERSIST_SESSIONS)
            .map(|lag| shift(&value, groups, lag))
            .collect();
        let present: Vec<F> = value
            .iter()
            .map(|v| Some(if v.is_some() { 1.0 } else { 0.0 }))
            .collect();
        let present_count = rolling(
            &present,
            groups,
            Roll::Sum,
            VOLUME_PERSIST_SESSIONS,
            VOLUME_PERSIST_SESSIONS,
        )?;
        let wanted = super::as_f64(VOLUME_PERSIST_SESSIONS);
        let persist: Vec<F> = (0..value.len())
            .map(|i| {
                let complete: B = super::eq(present_count[i], wanted);
                let guard = or(
                    or(null(baseline[i]), Some(complete.is_none())),
                    complete.map(|c| !c),
                );
                if taken(guard) {
                    return None;
                }
                // `pl.sum_horizontal` skips NULLs.
                let mut total = 0.0;
                for lag in &lags {
                    if let Some(x) = bool_f(gt2(lag[i], baseline[i])) {
                        total += x;
                    }
                }
                Some(total)
            })
            .collect();
        put(&mut long.df, "vol_persist_20", persist)
    }

    /// `_with_relative_strength`: % of the last 126 sessions whose 20-session return beat
    /// NIFTY 500's.
    fn with_relative_strength(
        long: &mut Long,
        groups: &[Range<usize>],
        close: &[F],
        market_benchmark: Option<&DataFrame>,
    ) -> CoreResult<()> {
        let Some(levels) = benchmark_levels(market_benchmark)? else {
            put_null(&mut long.df, BENCH_RETURN_20, &DataType::Float64)?;
            return put_null(&mut long.df, "rs_persist_126", &DataType::Float64);
        };
        let bench: Vec<(Option<i32>, F)> = levels
            .iter()
            .enumerate()
            .map(|(i, (d, l))| {
                let base = if i >= RS_RETURN_SESSIONS {
                    levels[i - RS_RETURN_SESSIONS].1
                } else {
                    None
                };
                let r = if taken(or(null(base), le(base, 0.0))) {
                    None
                } else {
                    sub(div(*l, base), Some(1.0))
                };
                (*d, r)
            })
            .collect();
        long.df = drop_if_present(&long.df, BENCH_RETURN_20)?;
        let joined: Vec<F> = long
            .dates
            .iter()
            .map(|d| {
                d.and_then(|day| {
                    bench
                        .iter()
                        .find(|(x, _)| *x == Some(day))
                        .and_then(|(_, v)| *v)
                })
            })
            .collect();
        join_put(
            &mut long.df,
            BENCH_RETURN_20,
            Series::new(BENCH_RETURN_20.into(), joined.clone()),
        )?;

        let stock_base = shift(close, groups, RS_RETURN_SESSIONS);
        let beat: Vec<F> = (0..close.len())
            .map(|i| {
                let guard = or(null(stock_base[i]), le(stock_base[i], 0.0));
                let stock_return = if taken(guard) {
                    None
                } else {
                    sub(div(close[i], stock_base[i]), Some(1.0))
                };
                if taken(or(null(stock_return), null(joined[i]))) {
                    None
                } else {
                    bool_f(gt2(stock_return, joined[i]))
                }
            })
            .collect();
        let persist: Vec<F> = rolling(
            &beat,
            groups,
            Roll::Mean,
            RS_PERSIST_SESSIONS,
            RS_PERSIST_SESSIONS,
        )?
        .into_iter()
        .map(|v| v.map(|x| x * 100.0))
        .collect();
        put(&mut long.df, "rs_persist_126", persist)
    }

    // --- with_ranking_at_as_of ----------------------------------------------------------

    /// `with_ranking_at_as_of`: the as-of-only C1 columns, the internal columns dropped, and
    /// the unrepresentable NULLed. `rows` are the long frame's indices of `at`'s rows.
    #[allow(clippy::too_many_arguments)]
    pub(super) fn with_ranking_at_as_of(
        mut at: DataFrame,
        long: &Long,
        rows: &[usize],
        as_of: NaiveDate,
        trading_days: &[NaiveDate],
        windows: &[(i64, FactorWindow)],
        benchmark: Option<&DataFrame>,
        market_benchmark: Option<&DataFrame>,
    ) -> CoreResult<DataFrame> {
        let as_of_day = days(as_of)?;
        // `past`: each group's rows dated on or before as_of, in order.
        let past: Vec<Vec<usize>> = long
            .groups
            .iter()
            .map(|g| {
                g.clone()
                    .filter(|&r| long.dates[r].is_some_and(|d| d <= as_of_day))
                    .collect()
            })
            .collect();
        let past_height: usize = past.iter().map(Vec::len).sum();
        let at_groups: Vec<usize> = rows.iter().map(|&r| long.group_of[r]).collect();

        with_sortino(&mut at)?;
        with_path_statistics(&mut at, long, &past, past_height, &at_groups, windows)?;
        with_benchmark_relative(&mut at, as_of, windows, benchmark, market_benchmark)?;
        with_nse_ratios(
            &mut at,
            long,
            &past,
            past_height,
            &at_groups,
            as_of,
            trading_days,
        )?;
        put_null(&mut at, "mom_pctile", &DataType::Float64)?;
        put_null(&mut at, "rank_persist_20", &DataType::Float64)?;
        for c in INTERNAL_COLUMNS {
            at = drop_if_present(&at, c)?;
        }
        null_unrepresentable(&mut at)?;
        Ok(at)
    }

    /// `_with_sortino`: `ret_Nm / (downside_vol_Nm x 100)` with `sharpe_Nm`'s guard.
    fn with_sortino(frame: &mut DataFrame) -> CoreResult<()> {
        let mut out = Vec::new();
        for months in [6, 12] {
            let name = format!("sortino_{months}m");
            let ret = format!("ret_{months}m");
            if !has(frame, &ret) {
                out.push((name, None));
                continue;
            }
            let r = floats(frame, &ret)?;
            let d = floats(frame, &format!("downside_vol_{months}m"))?;
            let values: Vec<F> = (0..r.len())
                .map(|i| {
                    let guard = or(
                        or(null(d[i]), lt(d[i], ZERO_DENOMINATOR_EPSILON)),
                        null(r[i]),
                    );
                    if taken(guard) {
                        None
                    } else {
                        div(r[i], d[i].map(|x| x * 100.0))
                    }
                })
                .collect();
            out.push((name, Some(values)));
        }
        for (name, values) in out {
            match values {
                Some(v) => put(frame, &name, v)?,
                None => put_null(frame, &name, &DataType::Float64)?,
            }
        }
        Ok(())
    }

    /// `_with_path_statistics`: `max_dd_6m/12m`, `underwater_12m`, `ret_ex_top3_12m` over each
    /// instrument's last N closes (NumPy over a right-aligned, NaN-padded matrix).
    fn with_path_statistics(
        frame: &mut DataFrame,
        long: &Long,
        past: &[Vec<usize>],
        past_height: usize,
        at_groups: &[usize],
        windows: &[(i64, FactorWindow)],
    ) -> CoreResult<()> {
        let names = [
            "max_dd_6m",
            "max_dd_12m",
            "underwater_12m",
            "ret_ex_top3_12m",
        ];
        if frame.height() == 0 || past_height == 0 {
            for n in names {
                put_null(frame, n, &DataType::Float64)?;
            }
            return Ok(());
        }
        let w6 = window(windows, 6)?;
        let w12 = window(windows, 12)?;
        let longest = w6.length().max(w12.length());
        let close = floats(&long.df, "close")?;
        // Per group: the last `longest` closes, oldest first, right-aligned, NaN-padded.
        let tails: Vec<Vec<f64>> = past
            .iter()
            .map(|rows| {
                let mut tail = vec![f64::NAN; longest];
                let take = rows.len().min(longest);
                for (k, &r) in rows[rows.len() - take..].iter().enumerate() {
                    tail[longest - take + k] = close[r].unwrap_or(f64::NAN);
                }
                tail
            })
            .collect();
        // results[group] = [max_dd_6m, max_dd_12m, underwater_12m, ret_ex_top3_12m]
        let mut results: Vec<[f64; 4]> = vec![[f64::NAN; 4]; past.len()];
        for (slot, w) in [(0_usize, w6), (1, w12)] {
            let n = w.length();
            if !w.spans_full_window() || n < 2 {
                continue;
            }
            for (g, tail) in tails.iter().enumerate() {
                let block = &tail[longest - n..];
                let valid = block.iter().all(|x| x.is_finite() && *x > 0.0);
                if !valid {
                    continue;
                }
                let mut running = Vec::with_capacity(n);
                let mut best = f64::NEG_INFINITY;
                for x in block {
                    best = if *x > best { *x } else { best };
                    running.push(best);
                }
                let mut drawdown = f64::NEG_INFINITY;
                let mut under = 0_usize;
                for (x, m) in block.iter().zip(&running) {
                    let d = 1.0 - x / m;
                    drawdown = if d > drawdown { d } else { drawdown };
                    under += usize::from(*x < *m);
                }
                results[g][slot] = drawdown * 100.0;
                if slot == 1 {
                    results[g][2] = as_f64(under) / as_f64(n) * 100.0;
                    if n > TOP_DAYS_EXCLUDED {
                        results[g][3] = return_ex_top_days(block);
                    }
                }
            }
        }
        for (k, name) in names.iter().enumerate() {
            let values: Vec<F> = at_groups
                .iter()
                .map(|&g| {
                    if long.group_null[g] {
                        return None;
                    }
                    let v = results[g][k];
                    if v.is_nan() { None } else { Some(v) }
                })
                .collect();
            join_put(frame, name, Series::new((*name).into(), values))?;
        }
        Ok(())
    }

    /// `_return_ex_top_days`: `(exp(sum l - sum of the 3 largest l) - 1) x 100`.
    fn return_ex_top_days(closes: &[f64]) -> f64 {
        let logs: Vec<f64> = closes.windows(2).map(|p| p[1].ln() - p[0].ln()).collect();
        if logs.len() < TOP_DAYS_EXCLUDED {
            return f64::NAN;
        }
        let mut sorted = logs.clone();
        sorted.sort_by(|a, b| tot_cmp(*a, *b));
        let largest = numpy::sum(&sorted[sorted.len() - TOP_DAYS_EXCLUDED..]);
        (numpy::sum(&logs) - largest).exp_m1() * 100.0
    }

    /// `_with_benchmark_relative`: `excess_ret_3m/6m/12m` (NIFTY 500) and `resid_ret_12m`.
    fn with_benchmark_relative(
        frame: &mut DataFrame,
        as_of: NaiveDate,
        windows: &[(i64, FactorWindow)],
        benchmark: Option<&DataFrame>,
        market_benchmark: Option<&DataFrame>,
    ) -> CoreResult<()> {
        let market = benchmark_levels(market_benchmark)?;
        let mut out: Vec<(String, Option<Vec<F>>)> = Vec::new();
        for months in RANKING_WINDOW_MONTHS {
            let name = format!("excess_ret_{months}m");
            let ret = format!("ret_{months}m");
            let index_return =
                benchmark_window_return(market.as_ref(), as_of, window(windows, months)?)?;
            match index_return {
                Some(x) if has(frame, &ret) => {
                    let r = floats(frame, &ret)?;
                    out.push((name, Some(r.iter().map(|v| v.map(|y| y - x)).collect())));
                }
                _ => out.push((name, None)),
            }
        }
        let nifty = benchmark_levels(benchmark)?;
        let nifty_50_return = benchmark_window_return(nifty.as_ref(), as_of, window(windows, 12)?)?;
        match nifty_50_return {
            Some(x) if has(frame, "ret_12m") && has(frame, "beta_12m") => {
                let r = floats(frame, "ret_12m")?;
                let b = floats(frame, "beta_12m")?;
                let values: Vec<F> = r
                    .iter()
                    .zip(&b)
                    .map(|(r, b)| {
                        if r.is_none() || b.is_none() {
                            None
                        } else {
                            sub(*r, mul(*b, Some(x)))
                        }
                    })
                    .collect();
                out.push(("resid_ret_12m".to_owned(), Some(values)));
            }
            _ => out.push(("resid_ret_12m".to_owned(), None)),
        }
        for (name, values) in out {
            match values {
                Some(v) => put(frame, &name, v)?,
                None => put_null(frame, &name, &DataType::Float64)?,
            }
        }
        Ok(())
    }

    /// `_with_nse_ratios`: `nse_mr6`/`nse_mr12` for the rebalance month containing `as_of`.
    fn with_nse_ratios(
        frame: &mut DataFrame,
        long: &Long,
        past: &[Vec<usize>],
        past_height: usize,
        at_groups: &[usize],
        as_of: NaiveDate,
        trading_days: &[NaiveDate],
    ) -> CoreResult<()> {
        let end = month_end_trading_day(trading_days, as_of, NSE_RETURN_END_MONTHS)?;
        let bases = [
            (
                "nse_mr6",
                month_end_trading_day(trading_days, as_of, NSE_MR6_BASE_MONTHS)?,
            ),
            (
                "nse_mr12",
                month_end_trading_day(trading_days, as_of, NSE_MR12_BASE_MONTHS)?,
            ),
        ];
        let Some(end) = end.filter(|_| past_height > 0) else {
            for (name, _) in bases {
                put_null(frame, name, &DataType::Float64)?;
            }
            return Ok(());
        };
        let close = floats(&long.df, "close")?;
        let sigma = floats(&long.df, NSE_SIGMA)?;
        // A left join on instrument_id with the past rows dated exactly `day`.
        let on = |day: NaiveDate, values: &[F]| -> CoreResult<Vec<F>> {
            let d = days(day)?;
            Ok(at_groups
                .iter()
                .map(|&g| {
                    if long.group_null[g] {
                        return None;
                    }
                    past[g]
                        .iter()
                        .find(|&&r| long.dates[r] == Some(d))
                        .and_then(|&r| values[r])
                })
                .collect())
        };
        let p_end = on(end, &close)?;
        let sigma_end = on(end, &sigma)?;
        let mut out = Vec::new();
        for (name, base_day) in bases {
            let Some(base_day) = base_day else {
                out.push((name, None));
                continue;
            };
            let base = on(base_day, &close)?;
            let values: Vec<F> = (0..p_end.len())
                .map(|i| {
                    let guard = or(
                        or(
                            or(or(null(p_end[i]), null(base[i])), le(base[i], 0.0)),
                            null(sigma_end[i]),
                        ),
                        lt(sigma_end[i], ZERO_DENOMINATOR_EPSILON),
                    );
                    if taken(guard) {
                        None
                    } else {
                        div(sub(div(p_end[i], base[i]), Some(1.0)), sigma_end[i])
                    }
                })
                .collect();
            out.push((name, Some(values)));
        }
        for (name, values) in out {
            match values {
                Some(v) => put(frame, name, v)?,
                None => put_null(frame, name, &DataType::Float64)?,
            }
        }
        Ok(())
    }

    /// `null_unrepresentable`: NULL any C1 value `numeric(p, s)` cannot hold, NaN or infinite.
    fn null_unrepresentable(frame: &mut DataFrame) -> CoreResult<()> {
        let mut out = Vec::new();
        for (name, precision, scale) in RANKING_NUMERIC_TYPES {
            if !has(frame, name) {
                continue;
            }
            let limit = 10.0_f64.powf(f64::from(precision - scale))
                - 0.5 * 10.0_f64.powf(f64::from(-scale));
            let values = floats(frame, name)?;
            let kept: Vec<F> = values
                .iter()
                .map(|v| match v {
                    Some(x) if x.is_finite() && !taken(super::ge(Some(x.abs()), limit)) => Some(*x),
                    _ => None,
                })
                .collect();
            out.push((name, kept));
        }
        for (name, values) in out {
            put(frame, name, values)?;
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// precision.py
// ---------------------------------------------------------------------------

mod precision {
    //! Port of `baskfy_core.precision.apply_storage_precision` (docs/13 §4: round half-up at
    //! write time).

    use polars::prelude::{DataFrame, DataType, IntoColumn, NamedFrom, Series};

    use super::{F, floats};
    use crate::error::CoreResult;

    const PRICE_DP: i32 = 2;
    const PERCENT_DP: i32 = 2;
    const RSI_DP: i32 = 4;
    const RATIO_DP: i32 = 10;

    /// `COLUMN_PRECISION`, in full (it is shared with the swing, VBT and TWT writers).
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

    /// `round_expr`: `((|x| 10^p + 0.5).floor() / 10^p).round(p) * sign(x)`, NULL for
    /// null/inf/NaN.
    pub(super) fn round_half_up(value: F, places: i32) -> F {
        let scale = 10.0_f64.powf(f64::from(places));
        match value {
            Some(x) if x.is_finite() => {
                Some(polars_round((x.abs() * scale + 0.5).floor() / scale, places) * sign(x))
            }
            _ => None,
        }
    }

    /// `integer_expr`: half-up to a whole number, as Int64.
    fn whole(value: F) -> Option<i64> {
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

    fn is_float(df: &DataFrame, name: &str) -> bool {
        df.column(name)
            .is_ok_and(|c| matches!(c.dtype(), DataType::Float64 | DataType::Float32))
    }

    /// `apply_storage_precision`.
    pub(super) fn apply_storage_precision(frame: &DataFrame) -> CoreResult<DataFrame> {
        let mut df = frame.clone();
        for &(name, places) in COLUMN_PRECISION {
            if !is_float(&df, name) {
                continue;
            }
            let values: Vec<F> = floats(&df, name)?
                .into_iter()
                .map(|v| round_half_up(v, places))
                .collect();
            df.with_column(Series::new(name.into(), values).into_column())?;
        }
        for name in INTEGER_COLUMNS {
            if !is_float(&df, name) {
                continue;
            }
            let values: Vec<Option<i64>> = floats(&df, name)?.into_iter().map(whole).collect();
            df.with_column(Series::new(name.into(), values).into_column())?;
        }
        Ok(df)
    }
}
