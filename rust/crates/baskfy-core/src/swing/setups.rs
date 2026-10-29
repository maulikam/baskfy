//! Port of `baskfy_core.swing.setups` (`decile-blueprint/packages/core/src/baskfy_core/swing/setups.py`).
//!
//! The three detectors (docs/swing/04 §2-§4): one as-of date in, one row per candidate out.
//!
//! Goldens: `L1/swing/detect_setups.*` (`baskfy_core.swing.setups.detect_setups`).
//!
//! # Shape of the computation
//!
//! Python evaluates each detector as `group_by(instrument_id).agg(...)` polars expressions over
//! the last `window` bars of each instrument. This port walks the same groups explicitly and
//! reproduces the expression semantics value for value:
//!
//! * a polars **null** is `None`; arithmetic on a null is null; a comparison with a null is a
//!   null; `&` / `|` follow Kleene logic; `filter` keeps only `true`;
//! * float comparisons use polars' total order (NaN equals NaN and sorts above every number);
//! * `min` / `max` / `arg_max` / `mean` skip nulls, and an empty or all-null slice is null;
//! * `Expr.slice(offset, length)` clamps to the group, as polars does.
//!
//! All three detectors return the same columns ([`CANDIDATE_COLUMNS`]), nulls where a column is
//! not meaningful for the setup. `trigger` and `stop_ref` are **adjusted** prices; `adj_factor`
//! rides along so the plan builder can produce the exchange price (`adjusted / adj_factor`).

use std::cmp::Ordering;
use std::collections::HashMap;

use chrono::NaiveDate;
use polars::prelude::{Column, DataFrame, DataType, IdxCa, IdxSize, IntoColumn, NamedFrom, Series};
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::swing::market::MarketConfig;
use crate::swing::opening_range::OpeningRangeConfig;
use crate::swing::sizing::SizingConfig;
use crate::swing::stops::StopConfig;

// ---------------------------------------------------------------------------
// `baskfy_core.swing.config` — the parts this module and the plan read
// ---------------------------------------------------------------------------
//
// `swing::config` is declared but outside this port's ownership and still empty, so — like
// `SizingConfig` (sizing.rs), `StopConfig` (stops.rs), `MarketConfig` (market.rs) and
// `OpeningRangeConfig` (opening_range.rs) — the dataclasses the detectors need live here until
// it is ported. Missing keys take Python's defaults; an unknown key is an error.

/// The three setups. The value is the wire form (`sw_setup_daily.setup`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Setup {
    /// The flag / continuation breakout.
    Flag,
    /// The episodic pivot.
    Ep,
    /// The parabolic runner — detected for the record only.
    ParabolicShort,
}

impl Setup {
    /// The wire form, `Setup.value` in Python.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Flag => "FLAG",
            Self::Ep => "EP",
            Self::ParabolicShort => "PARABOLIC_SHORT",
        }
    }
}

/// Setups the plan builder may turn into a BUY line. `PARABOLIC_SHORT` is never here: NSE cash
/// equities cannot be shorted for delivery (docs/swing/02 Track C).
pub const TRADEABLE_SETUPS: [Setup; 2] = [Setup::Flag, Setup::Ep];

/// The universe a setup may come from (docs/swing/04 §1).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct LiquidityConfig {
    /// ADR% is the mean of the bar's range over this many bars.
    pub adr_bars: i64,
    /// The ADR% floor.
    pub adr_min_pct: f64,
    /// Average rupee turnover is taken over this many bars.
    pub turnover_bars: i64,
    /// The turnover floor, rupees.
    pub turnover_min_inr: f64,
    /// The price floor.
    pub price_min: f64,
}

impl Default for LiquidityConfig {
    fn default() -> Self {
        Self {
            adr_bars: 20,
            adr_min_pct: 4.0,
            turnover_bars: 20,
            turnover_min_inr: 5e7,
            price_min: 20.0,
        }
    }
}

/// Setup 1 — the flag / continuation breakout (docs/swing/04 §2).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct FlagConfig {
    /// How far back the flagpole may start.
    pub lookback_bars: i64,
    /// The prior move's minimum, percent.
    pub flagpole_min_gain_pct: f64,
    /// The base's shortest length, bars since the pole's high.
    pub base_min_bars: i64,
    /// The base's longest length.
    pub base_max_bars: i64,
    /// The deepest retrace from the pole high, percent.
    pub base_max_depth_pct: f64,
    /// Tightness is measured over this many bars...
    pub tight_bars: i64,
    /// ...as a multiple of ADR% no larger than this.
    pub tight_max_adr_multiple: f64,
    /// The second half of the base may not undercut the first half's low.
    pub require_higher_lows: bool,
    /// The close sits within this many ADRs of the fast MA.
    pub max_extension_adr: f64,
    /// A close this far below the slow MA is still "on" it, percent.
    pub ma_tolerance_pct: f64,
    /// The slow MA must be no lower than it was this many bars ago.
    pub ma_rising_bars: i64,
    /// Volume dry-up window.
    pub dryup_bars: i64,
    /// Volume dry-up ratio ceiling.
    pub dryup_max_ratio: f64,
    /// The pivot is the highest high of this many bars.
    pub pivot_bars: i64,
    /// A breakout needs at least this relative volume.
    pub breakout_min_rvol: f64,
}

impl Default for FlagConfig {
    fn default() -> Self {
        Self {
            lookback_bars: 65,
            flagpole_min_gain_pct: 30.0,
            base_min_bars: 10,
            base_max_bars: 60,
            base_max_depth_pct: 30.0,
            tight_bars: 10,
            tight_max_adr_multiple: 3.0,
            require_higher_lows: true,
            max_extension_adr: 2.0,
            ma_tolerance_pct: 2.0,
            ma_rising_bars: 5,
            dryup_bars: 10,
            dryup_max_ratio: 0.85,
            pivot_bars: 20,
            breakout_min_rvol: 1.5,
        }
    }
}

/// Setup 2 — the episodic pivot (docs/swing/04 §3).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct EpConfig {
    /// The gap's minimum, percent.
    pub min_gap_pct: f64,
    /// Relative volume is today over the mean of this many prior bars.
    pub rvol_bars: i64,
    /// The relative volume's minimum.
    pub min_rvol: f64,
    /// Where the close must sit in the day's range, 0 to 1.
    pub min_close_position: f64,
    /// Neglect is measured over this many bars before the gap...
    pub prior_bars: i64,
    /// ...and the return over them may not exceed this, percent.
    pub max_prior_gain_pct: f64,
    /// An EP stays enterable for this many bars after the gap day.
    pub valid_bars: i64,
}

impl Default for EpConfig {
    fn default() -> Self {
        Self {
            min_gap_pct: 10.0,
            rvol_bars: 50,
            min_rvol: 3.0,
            min_close_position: 0.5,
            prior_bars: 60,
            max_prior_gain_pct: 30.0,
            valid_bars: 3,
        }
    }
}

/// Setup 3 — the parabolic runner, detected for the record only (docs/swing/04 §4).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ParabolicConfig {
    /// The 5-bar gain's minimum, percent.
    pub min_gain_5_bars_pct: f64,
    /// The 10-bar gain's minimum, percent.
    pub min_gain_10_bars_pct: f64,
    /// Consecutive up closes.
    pub min_up_streak: i64,
    /// Extension above the fast MA, in ADRs.
    pub min_extension_adr: f64,
}

impl Default for ParabolicConfig {
    fn default() -> Self {
        Self {
            min_gain_5_bars_pct: 50.0,
            min_gain_10_bars_pct: 100.0,
            min_up_streak: 3,
            min_extension_adr: 4.0,
        }
    }
}

/// The watchlist's own rules (docs/swing/04 §9.5).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct WatchConfig {
    /// A detected flag is watched automatically from this score up.
    pub auto_watch_min_score: f64,
    /// A flag stays on the list this many sessions without triggering.
    pub flag_valid_bars: i64,
    /// The weekly focus list's length.
    pub auto_watch_top_n: i64,
    /// The daily focus's length.
    pub focus_top_n: i64,
    /// A MANUAL row expires after this many sessions.
    pub manual_valid_bars: i64,
}

impl Default for WatchConfig {
    fn default() -> Self {
        Self {
            auto_watch_min_score: 60.0,
            flag_valid_bars: 10,
            auto_watch_top_n: 20,
            focus_top_n: 5,
            manual_valid_bars: 10,
        }
    }
}

/// Everything the engine can be recalibrated on without an edit (`SwingConfig`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct SwingConfig {
    /// The universe.
    pub liquidity: LiquidityConfig,
    /// Setup 1.
    pub flag: FlagConfig,
    /// Setup 2.
    pub ep: EpConfig,
    /// Setup 3.
    pub parabolic: ParabolicConfig,
    /// Risk per trade.
    pub sizing: SizingConfig,
    /// Stops and exits.
    pub stops: StopConfig,
    /// The watchlist.
    pub watch: WatchConfig,
    /// The live monitor.
    pub opening_range: OpeningRangeConfig,
    /// Breadth, the gate and progressive exposure.
    pub market: MarketConfig,
    /// The fast moving average's length.
    pub ma_fast: i64,
    /// The slow moving average's length.
    pub ma_slow: i64,
    /// The trend moving average's length.
    pub ma_trend: i64,
}

impl Default for SwingConfig {
    fn default() -> Self {
        Self {
            liquidity: LiquidityConfig::default(),
            flag: FlagConfig::default(),
            ep: EpConfig::default(),
            parabolic: ParabolicConfig::default(),
            sizing: SizingConfig::default(),
            stops: StopConfig::default(),
            watch: WatchConfig::default(),
            opening_range: OpeningRangeConfig::default(),
            market: MarketConfig::default(),
            ma_fast: 10,
            ma_slow: 20,
            ma_trend: 50,
        }
    }
}

const PCT: f64 = 100.0;
const HALF: i64 = 2;
/// A reading at this multiple of its threshold earns full marks in a score component.
const FULL_MARKS: f64 = 2.0;
/// Integer twin of [`FULL_MARKS`], for `_FULL_MARKS * para.min_up_streak` (an int in Python).
const FULL_MARKS_INT: i64 = 2;

/// Where the candidate is in its life, the day it was detected.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CandidateStatus {
    /// A flag whose conditions hold and whose close is still below the pivot: watch the pivot.
    SettingUp,
    /// A flag that closed above yesterday's pivot on volume today: the breakout happened.
    BreakoutToday,
    /// The EP's gap day itself.
    GapDay,
    /// A parabolic name still making green closes.
    Running,
    /// A parabolic name that printed its first red close after the streak.
    Exhaustion,
}

impl CandidateStatus {
    /// The wire form (`sw_setup_daily.status`).
    pub fn as_str(self) -> &'static str {
        match self {
            Self::SettingUp => "SETTING_UP",
            Self::BreakoutToday => "BREAKOUT_TODAY",
            Self::GapDay => "GAP_DAY",
            Self::Running => "RUNNING",
            Self::Exhaustion => "EXHAUSTION",
        }
    }
}

const SETUP_FLAG: &str = Setup::Flag.as_str();
const SETUP_EP: &str = Setup::Ep.as_str();
const SETUP_PARABOLIC: &str = Setup::ParabolicShort.as_str();

/// The uniform output schema, in order.
pub const CANDIDATE_COLUMNS: [&str; 24] = [
    "instrument_id",
    "symbol",
    "date",
    "setup",
    "status",
    "score",
    "close",
    "adj_factor",
    "adr_pct",
    "turnover_avg",
    "trigger",
    "stop_ref",
    "pivot_high",
    "prior_move_pct",
    "base_bars",
    "base_depth_pct",
    "tightness_adr",
    "dryup_ratio",
    "dist_ma_fast_pct",
    "dist_ma_slow_pct",
    "rvol",
    "gap_pct",
    "up_streak",
    "locked_upper_circuit",
];

// ---------------------------------------------------------------------------
// Polars value semantics: null as None, Kleene booleans, total-order floats
// ---------------------------------------------------------------------------

type F = Option<f64>;
type B = Option<bool>;

fn and(a: B, b: B) -> B {
    match (a, b) {
        (Some(false), _) | (_, Some(false)) => Some(false),
        (Some(true), Some(true)) => Some(true),
        _ => None,
    }
}

fn or(a: B, b: B) -> B {
    match (a, b) {
        (Some(true), _) | (_, Some(true)) => Some(true),
        (Some(false), Some(false)) => Some(false),
        _ => None,
    }
}

/// Polars' float order: NaN equals NaN and is greater than every number.
fn total_cmp(a: f64, b: f64) -> Ordering {
    match (a.is_nan(), b.is_nan()) {
        (true, true) => Ordering::Equal,
        (true, false) => Ordering::Greater,
        (false, true) => Ordering::Less,
        (false, false) => a.partial_cmp(&b).unwrap_or(Ordering::Equal),
    }
}

fn cmp_with(a: F, b: F, test: fn(Ordering) -> bool) -> B {
    Some(test(total_cmp(a?, b?)))
}

fn ge(a: F, b: F) -> B {
    cmp_with(a, b, Ordering::is_ge)
}

fn le(a: F, b: F) -> B {
    cmp_with(a, b, Ordering::is_le)
}

fn gt(a: F, b: F) -> B {
    cmp_with(a, b, Ordering::is_gt)
}

fn add(a: F, b: F) -> F {
    Some(a? + b?)
}

fn sub(a: F, b: F) -> F {
    Some(a? - b?)
}

fn mul(a: F, b: F) -> F {
    Some(a? * b?)
}

fn div(a: F, b: F) -> F {
    Some(a? / b?)
}

fn lit(x: f64) -> F {
    Some(x)
}

/// `Expr.clip(0.0, 1.0)`; a NaN stays NaN.
fn clamp01(x: F) -> F {
    x.map(|v| if v.is_nan() { v } else { v.clamp(0.0, 1.0) })
}

/// `((a / b) - 1.0) * 100.0`, the percent-change shape every detector uses.
fn pct_change(a: F, b: F) -> F {
    mul(sub(div(a, b), lit(1.0)), lit(PCT))
}

/// Integer `n` as a polars float (exact for any count this module produces).
#[allow(clippy::cast_precision_loss)]
fn int_as_float(n: i64) -> f64 {
    n as f64
}

/// A config integer as `i64`, whatever integer type the config port chose.
fn int<T: TryInto<i64>>(value: T, what: &str) -> CoreResult<i64> {
    value
        .try_into()
        .map_err(|_| CoreError::InvalidInput(format!("config {what} does not fit an i64")))
}

// ---------------------------------------------------------------------------
// The indicated frame, read once
// ---------------------------------------------------------------------------

/// The columns the detectors read, as plain vectors in frame order.
struct Indicated {
    instrument_id: Vec<Option<i64>>,
    date: Vec<Option<i32>>,
    symbol: Vec<Option<String>>,
    open: Vec<F>,
    high: Vec<F>,
    low: Vec<F>,
    close: Vec<F>,
    volume: Vec<F>,
    adj_factor: Vec<F>,
    upper_circuit: Vec<F>,
    prev_close: Vec<F>,
    gap_pct: Vec<F>,
    adr_pct: Vec<F>,
    turnover_avg: Vec<F>,
    ma_fast: Vec<F>,
    ma_slow: Vec<F>,
    ma_trend: Vec<F>,
    vol_avg_fast: Vec<F>,
    rvol: Vec<F>,
    ret_5: Vec<F>,
    ret_10: Vec<F>,
    up_streak: Vec<Option<i64>>,
    close_position: Vec<F>,
}

fn column<'a>(frame: &'a DataFrame, name: &str) -> CoreResult<&'a Column> {
    frame
        .column(name)
        .map_err(|_| CoreError::MissingColumn(name.to_owned()))
}

fn floats(frame: &DataFrame, name: &str) -> CoreResult<Vec<F>> {
    let cast = column(frame, name)?.cast(&DataType::Float64)?;
    Ok(cast.f64()?.iter().collect())
}

fn ints(frame: &DataFrame, name: &str) -> CoreResult<Vec<Option<i64>>> {
    let cast = column(frame, name)?.cast(&DataType::Int64)?;
    Ok(cast.i64()?.iter().collect())
}

impl Indicated {
    fn read(frame: &DataFrame) -> CoreResult<Self> {
        let date = column(frame, "date")?.cast(&DataType::Date)?;
        let date = date.date()?.physical().iter().collect();
        let symbol = column(frame, "symbol")?.cast(&DataType::String)?;
        let symbol = symbol.str()?.iter().map(|s| s.map(str::to_owned)).collect();
        Ok(Self {
            instrument_id: ints(frame, "instrument_id")?,
            date,
            symbol,
            open: floats(frame, "open")?,
            high: floats(frame, "high")?,
            low: floats(frame, "low")?,
            close: floats(frame, "close")?,
            volume: floats(frame, "volume")?,
            adj_factor: floats(frame, "adj_factor")?,
            upper_circuit: floats(frame, "upper_circuit")?,
            prev_close: floats(frame, "prev_close")?,
            gap_pct: floats(frame, "gap_pct")?,
            adr_pct: floats(frame, "adr_pct")?,
            turnover_avg: floats(frame, "turnover_avg")?,
            ma_fast: floats(frame, "ma_fast")?,
            ma_slow: floats(frame, "ma_slow")?,
            ma_trend: floats(frame, "ma_trend")?,
            vol_avg_fast: floats(frame, "vol_avg_fast")?,
            rvol: floats(frame, "rvol")?,
            ret_5: floats(frame, "ret_5")?,
            ret_10: floats(frame, "ret_10")?,
            up_streak: ints(frame, "up_streak")?,
            close_position: floats(frame, "close_position")?,
        })
    }
}

/// One instrument's window: its key and the frame rows, oldest first.
struct Group {
    id: Option<i64>,
    rows: Vec<usize>,
}

impl Group {
    fn len(&self) -> usize {
        self.rows.len()
    }

    /// The values of `values` over this group's rows.
    fn of<T: Clone>(&self, values: &[T]) -> Vec<T> {
        self.rows.iter().map(|&r| values[r].clone()).collect()
    }

    /// `pl.col(c).last()`.
    fn last<T: Clone>(&self, values: &[Option<T>]) -> Option<T> {
        self.rows.last().and_then(|&r| values[r].clone())
    }

    /// `pl.col(c).first()`.
    fn first<T: Clone>(&self, values: &[Option<T>]) -> Option<T> {
        self.rows.first().and_then(|&r| values[r].clone())
    }

    /// `pl.col(c).shift(k).last()`: the value `k` bars before the last, null when out of range.
    fn shifted_last<T: Clone>(&self, values: &[Option<T>], k: i64) -> Option<T> {
        let n = i64::try_from(self.len()).ok()?;
        let at = usize::try_from(n - 1 - k).ok()?;
        if k < 0 || at >= self.len() {
            return None;
        }
        values[self.rows[at]].clone()
    }
}

/// `upto.group_by(instrument_id, maintain_order=True)`: groups in order of first appearance.
fn groups_upto(ind: &Indicated, as_of: i32) -> Vec<Group> {
    let mut index: HashMap<Option<i64>, usize> = HashMap::new();
    let mut groups: Vec<Group> = Vec::new();
    for (row, date) in ind.date.iter().enumerate() {
        // `filter(date <= as_of)`: a null date is a null predicate, dropped.
        if !date.is_some_and(|d| d <= as_of) {
            continue;
        }
        let id = ind.instrument_id[row];
        let slot = *index.entry(id).or_insert_with(|| {
            groups.push(Group {
                id,
                rows: Vec::new(),
            });
            groups.len() - 1
        });
        groups[slot].rows.push(row);
    }
    groups
}

/// `_window`: the last `bars` rows per instrument ending exactly at `as_of`, dropping
/// instruments with fewer than `min_bars` rows up to `as_of`.
///
/// Rows with a null `instrument_id` never survive: Python keeps the instruments through
/// `upto.join(present, on="instrument_id", how="semi")`, and a polars join does not match a
/// null key to a null key (`nulls_equal=False`), so the null group is dropped there even
/// though `group_by` formed it.
fn window(ind: &Indicated, as_of: i32, bars: i64, min_bars: usize) -> Vec<Group> {
    groups_upto(ind, as_of)
        .into_iter()
        .filter(|g| {
            let last = g.rows.iter().filter_map(|&r| ind.date[r]).max();
            g.id.is_some() && last == Some(as_of) && g.len() >= min_bars
        })
        .map(|g| {
            let keep = usize::try_from(bars.max(0)).unwrap_or(usize::MAX);
            let skip = g.len().saturating_sub(keep);
            Group {
                id: g.id,
                rows: g.rows[skip..].to_vec(),
            }
        })
        .collect()
}

// ---------------------------------------------------------------------------
// Aggregations over a slice, with polars' null handling
// ---------------------------------------------------------------------------

/// `Expr.slice(offset, length)` on a group of `n`: the clamped index range.
fn slice_range(n: usize, offset: i64, length: Option<i64>) -> std::ops::Range<usize> {
    let n_i = i64::try_from(n).unwrap_or(i64::MAX);
    let start = if offset < 0 {
        (n_i + offset).max(0)
    } else {
        offset.min(n_i)
    };
    let end = match length {
        Some(len) => start.saturating_add(len.max(0)).min(n_i),
        None => n_i,
    };
    let start = usize::try_from(start).unwrap_or(n);
    let end = usize::try_from(end).unwrap_or(n);
    start..end.max(start)
}

fn tail_range(n: usize, k: i64) -> std::ops::Range<usize> {
    let keep = usize::try_from(k.max(0)).unwrap_or(usize::MAX);
    n.saturating_sub(keep)..n
}

/// `max` skipping nulls (and NaN, as polars' `max` does); null when nothing is left.
fn max_of(values: &[F]) -> F {
    extreme(values, Ordering::Greater)
}

/// `min` skipping nulls and NaN; null when nothing is left.
fn min_of(values: &[F]) -> F {
    extreme(values, Ordering::Less)
}

fn extreme(values: &[F], want: Ordering) -> F {
    let mut best: F = None;
    let mut saw_nan = false;
    for v in values.iter().flatten() {
        if v.is_nan() {
            saw_nan = true;
            continue;
        }
        best = match best {
            Some(b) if total_cmp(*v, b) != want => Some(b),
            _ => Some(*v),
        };
    }
    if best.is_none() && saw_nan {
        return Some(f64::NAN);
    }
    best
}

/// `arg_max`: the index of the first maximum among non-null values, null when there is none.
fn arg_max_of(values: &[F]) -> Option<i64> {
    let mut best: Option<(usize, f64)> = None;
    for (i, v) in values.iter().enumerate() {
        let Some(v) = *v else { continue };
        if v.is_nan() {
            continue;
        }
        if best.is_none_or(|(_, b)| v > b) {
            best = Some((i, v));
        }
    }
    best.and_then(|(i, _)| i64::try_from(i).ok())
}

/// `mean` skipping nulls; null when there is nothing to average.
fn mean_of(values: &[F]) -> F {
    let present: Vec<f64> = values.iter().flatten().copied().collect();
    if present.is_empty() {
        return None;
    }
    let count = i64::try_from(present.len()).ok()?;
    Some(present.iter().sum::<f64>() / int_as_float(count))
}

// ---------------------------------------------------------------------------
// One candidate row
// ---------------------------------------------------------------------------

/// One output row, every column of [`CANDIDATE_COLUMNS`] (null where not meaningful).
#[derive(Debug, Clone, Default)]
struct Candidate {
    instrument_id: Option<i64>,
    symbol: Option<String>,
    date: Option<i32>,
    setup: &'static str,
    status: &'static str,
    score: F,
    close: F,
    adj_factor: F,
    adr_pct: F,
    turnover_avg: F,
    trigger: F,
    stop_ref: F,
    pivot_high: F,
    prior_move_pct: F,
    base_bars: Option<i64>,
    base_depth_pct: F,
    tightness_adr: F,
    dryup_ratio: F,
    dist_ma_fast_pct: F,
    dist_ma_slow_pct: F,
    rvol: F,
    gap_pct: F,
    up_streak: Option<i32>,
    locked_upper_circuit: B,
}

/// The universe predicate of docs/swing/04 §1 (`liquid_expr(config).last()`).
fn liquid(adr_pct: F, turnover_avg: F, close: F, config: &SwingConfig) -> B {
    let liquidity = &config.liquidity;
    and(
        and(
            ge(adr_pct, lit(liquidity.adr_min_pct)),
            ge(turnover_avg, lit(liquidity.turnover_min_inr)),
        ),
        ge(close, lit(liquidity.price_min)),
    )
}

/// `_locked_expr`: today's high touched the upper price band, so a fill cannot be assumed.
fn locked(upper_circuit: F, high_today: F) -> B {
    and(
        and(Some(upper_circuit.is_some()), gt(upper_circuit, lit(0.0))),
        ge(high_today, upper_circuit),
    )
}

fn is_true(b: B) -> bool {
    b == Some(true)
}

// ---------------------------------------------------------------------------
// Setup 1 — FLAG
// ---------------------------------------------------------------------------

/// docs/swing/04 §2 — the flag / continuation setup, one row per qualifying instrument.
fn flag_candidates(
    ind: &Indicated,
    as_of: i32,
    config: &SwingConfig,
) -> CoreResult<Vec<Candidate>> {
    let flag = &config.flag;
    let lookback = int(flag.lookback_bars, "flag.lookback_bars")?;
    let base_min = int(flag.base_min_bars, "flag.base_min_bars")?;
    let base_max = int(flag.base_max_bars, "flag.base_max_bars")?;
    let tight_bars = int(flag.tight_bars, "flag.tight_bars")?;
    let pivot_bars = int(flag.pivot_bars, "flag.pivot_bars")?;
    let ma_rising = int(flag.ma_rising_bars, "flag.ma_rising_bars")?;

    // At least one bar before today: the pole is found among the bars before `as_of`.
    let groups = window(ind, as_of, lookback + base_max, 2);
    let mut out = Vec::new();
    for g in &groups {
        let n_usize = g.len();
        let n = i64::try_from(n_usize)
            .map_err(|_| CoreError::InvalidInput("window too long".to_owned()))?;
        let high = g.of(&ind.high);
        let low = g.of(&ind.low);
        let volume = g.of(&ind.volume);

        // The pole is found among the bars BEFORE today.
        let before_today = slice_range(n_usize, 0, Some(n - 1));
        let pole_idx = arg_max_of(&high[before_today.clone()]);
        let base_bars = pole_idx.map(|p| n - 1 - p);

        let close = g.last(&ind.close);
        let adr_pct = g.last(&ind.adr_pct);
        let turnover_avg = g.last(&ind.turnover_avg);
        let ma_fast = g.last(&ind.ma_fast);
        let ma_slow = g.last(&ind.ma_slow);
        let ma_trend = g.last(&ind.ma_trend);
        let ma_slow_before = g.shifted_last(&ind.ma_slow, ma_rising);
        let vol_avg_fast = g.last(&ind.vol_avg_fast);
        let rvol = g.last(&ind.rvol);
        let low_today = g.last(&ind.low);
        let high_today = g.last(&ind.high);
        let upper_circuit = g.last(&ind.upper_circuit);
        let pole_high = max_of(&high[before_today]);

        let (pole_low, base_low, base_low_first_half, base_low_second_half, base_vol_avg) =
            match (pole_idx, base_bars) {
                (Some(p), Some(bb)) => {
                    let pole_start = (p - lookback).max(0);
                    let base_start = p + 1;
                    let half = bb.div_euclid(HALF);
                    (
                        min_of(&low[slice_range(n_usize, pole_start, Some(p - pole_start))]),
                        min_of(&low[slice_range(n_usize, base_start, None)]),
                        min_of(&low[slice_range(n_usize, base_start, Some(half))]),
                        min_of(&low[slice_range(n_usize, base_start + half, None)]),
                        mean_of(&volume[slice_range(n_usize, base_start, None)]),
                    )
                }
                _ => (None, None, None, None, None),
            };
        let tight_high = max_of(&high[tail_range(n_usize, tight_bars)]);
        let tight_low = min_of(&low[tail_range(n_usize, tight_bars)]);
        let pivot_incl_today = max_of(&high[tail_range(n_usize, pivot_bars)]);
        let head = &high[slice_range(n_usize, 0, Some(n - 1))];
        let pivot_prev = max_of(&head[tail_range(head.len(), pivot_bars)]);
        let is_liquid = liquid(adr_pct, turnover_avg, close, config);

        let prior_move_pct = pct_change(pole_high, pole_low);
        let base_depth_pct = mul(sub(lit(1.0), div(base_low, pole_high)), lit(PCT));
        let tightness_adr = div(pct_change(tight_high, tight_low), adr_pct);
        let dryup_ratio = div(vol_avg_fast, base_vol_avg);
        let dist_ma_fast_pct = pct_change(close, ma_fast);
        let dist_ma_slow_pct = pct_change(close, ma_slow);
        let base_bars_f = base_bars.map(int_as_float);

        let base_ok = and(
            and(
                ge(base_bars_f, lit(int_as_float(base_min))),
                le(base_bars_f, lit(int_as_float(base_max))),
            ),
            and(
                ge(prior_move_pct, lit(flag.flagpole_min_gain_pct)),
                le(base_depth_pct, lit(flag.base_max_depth_pct)),
            ),
        );
        let mut structure = is_liquid;
        for term in [
            ge(base_bars_f, lit(int_as_float(base_min))),
            le(base_bars_f, lit(int_as_float(base_max))),
            ge(prior_move_pct, lit(flag.flagpole_min_gain_pct)),
            le(base_depth_pct, lit(flag.base_max_depth_pct)),
            le(tightness_adr, lit(flag.tight_max_adr_multiple)),
            le(dist_ma_fast_pct, mul(lit(flag.max_extension_adr), adr_pct)),
            ge(dist_ma_slow_pct, lit(-flag.ma_tolerance_pct)),
            ge(close, ma_trend),
            ge(ma_slow, ma_slow_before),
            le(dryup_ratio, lit(flag.dryup_max_ratio)),
        ] {
            structure = and(structure, term);
        }
        if flag.require_higher_lows {
            structure = and(structure, ge(base_low_second_half, base_low_first_half));
        }

        let breakout = and(gt(close, pivot_prev), ge(rvol, lit(flag.breakout_min_rvol)));
        let keep = or(structure, and(and(is_liquid, breakout), base_ok));
        if !is_true(keep) {
            continue;
        }

        let liquidity = &config.liquidity;
        let score = [
            mul(
                lit(30.0),
                clamp01(sub(
                    lit(1.0),
                    div(tightness_adr, lit(flag.tight_max_adr_multiple)),
                )),
            ),
            mul(
                lit(25.0),
                clamp01(div(
                    prior_move_pct,
                    lit(FULL_MARKS * flag.flagpole_min_gain_pct),
                )),
            ),
            mul(
                lit(20.0),
                clamp01(div(adr_pct, lit(FULL_MARKS * liquidity.adr_min_pct))),
            ),
            mul(
                lit(15.0),
                clamp01(sub(
                    lit(1.0),
                    div(base_depth_pct, lit(flag.base_max_depth_pct)),
                )),
            ),
            mul(lit(10.0), clamp01(sub(lit(1.0), dryup_ratio))),
        ]
        .into_iter()
        .reduce(add)
        .flatten();

        let broke_out = is_true(breakout);
        out.push(Candidate {
            instrument_id: g.id,
            symbol: g.last(&ind.symbol),
            date: g.last(&ind.date),
            setup: SETUP_FLAG,
            status: if broke_out {
                CandidateStatus::BreakoutToday.as_str()
            } else {
                CandidateStatus::SettingUp.as_str()
            },
            score,
            close,
            adj_factor: g.last(&ind.adj_factor),
            adr_pct,
            turnover_avg,
            trigger: if broke_out {
                high_today
            } else {
                pivot_incl_today
            },
            stop_ref: low_today,
            pivot_high: pivot_incl_today,
            prior_move_pct,
            base_bars,
            base_depth_pct,
            tightness_adr,
            dryup_ratio,
            dist_ma_fast_pct,
            dist_ma_slow_pct,
            rvol,
            gap_pct: None,
            up_streak: None,
            locked_upper_circuit: locked(upper_circuit, high_today),
        });
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Setup 2 — EP
// ---------------------------------------------------------------------------

/// docs/swing/04 §3 — the episodic pivot, detected on the gap day itself.
fn ep_candidates(ind: &Indicated, as_of: i32, config: &SwingConfig) -> CoreResult<Vec<Candidate>> {
    let ep = &config.ep;
    let prior_bars = int(ep.prior_bars, "ep.prior_bars")?;
    let groups = window(ind, as_of, prior_bars + 1, 1);
    let mut out = Vec::new();
    for g in &groups {
        let n = i64::try_from(g.len())
            .map_err(|_| CoreError::InvalidInput("window too long".to_owned()))?;
        let close = g.last(&ind.close);
        let adr_pct = g.last(&ind.adr_pct);
        let turnover_avg = g.last(&ind.turnover_avg);
        let gap_pct = g.last(&ind.gap_pct);
        let rvol = g.last(&ind.rvol);
        let close_position = g.last(&ind.close_position);
        let open_today = g.last(&ind.open);
        let high_today = g.last(&ind.high);
        let low_today = g.last(&ind.low);
        let upper_circuit = g.last(&ind.upper_circuit);
        let prev_close = g.last(&ind.prev_close);
        let close_prior_start = g.first(&ind.close);
        let ma_fast = g.last(&ind.ma_fast);
        let ma_slow = g.last(&ind.ma_slow);
        let is_liquid = liquid(adr_pct, turnover_avg, close, config);

        let prior_move_pct = pct_change(prev_close, close_prior_start);
        let dist_ma_fast_pct = pct_change(close, ma_fast);
        let dist_ma_slow_pct = pct_change(close, ma_slow);

        let mut conditions = and(is_liquid, Some(n > prior_bars));
        for term in [
            ge(gap_pct, lit(ep.min_gap_pct)),
            ge(rvol, lit(ep.min_rvol)),
            ge(close, open_today),
            ge(close_position, lit(ep.min_close_position)),
            le(prior_move_pct, lit(ep.max_prior_gain_pct)),
        ] {
            conditions = and(conditions, term);
        }
        if !is_true(conditions) {
            continue;
        }
        let score = [
            mul(
                lit(35.0),
                clamp01(div(gap_pct, lit(FULL_MARKS * ep.min_gap_pct))),
            ),
            mul(lit(35.0), clamp01(div(rvol, lit(FULL_MARKS * ep.min_rvol)))),
            mul(lit(15.0), clamp01(close_position)),
            mul(
                lit(15.0),
                clamp01(sub(
                    lit(1.0),
                    div(prior_move_pct, lit(ep.max_prior_gain_pct)),
                )),
            ),
        ]
        .into_iter()
        .reduce(add)
        .flatten();
        out.push(Candidate {
            instrument_id: g.id,
            symbol: g.last(&ind.symbol),
            date: g.last(&ind.date),
            setup: SETUP_EP,
            status: CandidateStatus::GapDay.as_str(),
            score,
            close,
            adj_factor: g.last(&ind.adj_factor),
            adr_pct,
            turnover_avg,
            trigger: high_today,
            stop_ref: low_today,
            prior_move_pct,
            dist_ma_fast_pct,
            dist_ma_slow_pct,
            rvol,
            gap_pct,
            locked_upper_circuit: locked(upper_circuit, high_today),
            ..Candidate::default()
        });
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Setup 3 — PARABOLIC_SHORT (detect only)
// ---------------------------------------------------------------------------

/// docs/swing/04 §4 — the exhausted runner. Never a BUY line; see `TRADEABLE_SETUPS`.
fn parabolic_candidates(
    ind: &Indicated,
    as_of: i32,
    config: &SwingConfig,
) -> CoreResult<Vec<Candidate>> {
    let para = &config.parabolic;
    let min_streak = int(para.min_up_streak, "parabolic.min_up_streak")?;
    let groups = window(ind, as_of, min_streak + 1, 1);
    let mut out = Vec::new();
    for g in &groups {
        let close = g.last(&ind.close);
        let adr_pct = g.last(&ind.adr_pct);
        let turnover_avg = g.last(&ind.turnover_avg);
        let ret_5 = g.last(&ind.ret_5);
        let ret_10 = g.last(&ind.ret_10);
        let up_streak = g.last(&ind.up_streak);
        let streak_yesterday = g.shifted_last(&ind.up_streak, 1);
        let high_today = g.last(&ind.high);
        let low_today = g.last(&ind.low);
        let upper_circuit = g.last(&ind.upper_circuit);
        let ma_fast = g.last(&ind.ma_fast);
        let ma_slow = g.last(&ind.ma_slow);
        let is_liquid = liquid(adr_pct, turnover_avg, close, config);

        let dist_ma_fast_pct = pct_change(close, ma_fast);
        let dist_ma_slow_pct = pct_change(close, ma_slow);
        let ran = or(
            ge(ret_5, lit(para.min_gain_5_bars_pct)),
            ge(ret_10, lit(para.min_gain_10_bars_pct)),
        );
        let extended = ge(dist_ma_fast_pct, mul(lit(para.min_extension_adr), adr_pct));
        let running = up_streak.map(|s| s >= min_streak);
        let exhausted = and(
            up_streak.map(|s| s == 0),
            streak_yesterday.map(|s| s >= min_streak),
        );
        let conditions = and(and(and(is_liquid, ran), extended), or(running, exhausted));
        if !is_true(conditions) {
            continue;
        }
        let streak_f = up_streak.map(int_as_float);
        let score = [
            mul(
                lit(40.0),
                clamp01(div(ret_5, lit(FULL_MARKS * para.min_gain_5_bars_pct))),
            ),
            mul(
                lit(30.0),
                clamp01(div(
                    dist_ma_fast_pct,
                    mul(lit(FULL_MARKS * para.min_extension_adr), adr_pct),
                )),
            ),
            mul(
                lit(30.0),
                clamp01(div(
                    streak_f,
                    lit(int_as_float(FULL_MARKS_INT * min_streak)),
                )),
            ),
        ]
        .into_iter()
        .reduce(add)
        .flatten();
        let up_streak_out = match up_streak {
            Some(s) => Some(i32::try_from(s).map_err(|_| {
                CoreError::InvalidInput(format!("up_streak {s} does not fit an Int32"))
            })?),
            None => None,
        };
        out.push(Candidate {
            instrument_id: g.id,
            symbol: g.last(&ind.symbol),
            date: g.last(&ind.date),
            setup: SETUP_PARABOLIC,
            status: if is_true(exhausted) {
                CandidateStatus::Exhaustion.as_str()
            } else {
                CandidateStatus::Running.as_str()
            },
            score,
            close,
            adj_factor: g.last(&ind.adj_factor),
            adr_pct,
            turnover_avg,
            trigger: low_today,
            stop_ref: high_today,
            dist_ma_fast_pct,
            dist_ma_slow_pct,
            up_streak: up_streak_out,
            locked_upper_circuit: locked(upper_circuit, high_today),
            ..Candidate::default()
        });
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Frames out
// ---------------------------------------------------------------------------

fn days_since_epoch(date: NaiveDate) -> CoreResult<i32> {
    let epoch = NaiveDate::from_ymd_opt(1970, 1, 1)
        .ok_or_else(|| CoreError::InvalidInput("epoch".to_owned()))?;
    i32::try_from(date.signed_duration_since(epoch).num_days())
        .map_err(|_| CoreError::InvalidInput(format!("as_of {date} out of range")))
}

/// The candidates as a frame with [`CANDIDATE_COLUMNS`], typed exactly as Python's `_SCHEMA`.
fn to_frame(rows: &[Candidate]) -> CoreResult<DataFrame> {
    fn f64s(name: &str, rows: &[Candidate], get: fn(&Candidate) -> F) -> Column {
        Series::new(name.into(), rows.iter().map(get).collect::<Vec<F>>()).into_column()
    }
    fn strs(name: &str, values: Vec<Option<String>>) -> Column {
        Series::new(name.into(), values).into_column()
    }
    let dates = Series::new(
        "date".into(),
        rows.iter().map(|r| r.date).collect::<Vec<Option<i32>>>(),
    )
    .cast(&DataType::Date)?
    .into_column();
    let columns = vec![
        Series::new(
            "instrument_id".into(),
            rows.iter().map(|r| r.instrument_id).collect::<Vec<_>>(),
        )
        .into_column(),
        strs("symbol", rows.iter().map(|r| r.symbol.clone()).collect()),
        dates,
        strs(
            "setup",
            rows.iter().map(|r| Some(r.setup.to_owned())).collect(),
        ),
        strs(
            "status",
            rows.iter().map(|r| Some(r.status.to_owned())).collect(),
        ),
        f64s("score", rows, |r| r.score),
        f64s("close", rows, |r| r.close),
        f64s("adj_factor", rows, |r| r.adj_factor),
        f64s("adr_pct", rows, |r| r.adr_pct),
        f64s("turnover_avg", rows, |r| r.turnover_avg),
        f64s("trigger", rows, |r| r.trigger),
        f64s("stop_ref", rows, |r| r.stop_ref),
        f64s("pivot_high", rows, |r| r.pivot_high),
        f64s("prior_move_pct", rows, |r| r.prior_move_pct),
        Series::new(
            "base_bars".into(),
            rows.iter().map(|r| r.base_bars).collect::<Vec<_>>(),
        )
        .into_column(),
        f64s("base_depth_pct", rows, |r| r.base_depth_pct),
        f64s("tightness_adr", rows, |r| r.tightness_adr),
        f64s("dryup_ratio", rows, |r| r.dryup_ratio),
        f64s("dist_ma_fast_pct", rows, |r| r.dist_ma_fast_pct),
        f64s("dist_ma_slow_pct", rows, |r| r.dist_ma_slow_pct),
        f64s("rvol", rows, |r| r.rvol),
        f64s("gap_pct", rows, |r| r.gap_pct),
        Series::new(
            "up_streak".into(),
            rows.iter().map(|r| r.up_streak).collect::<Vec<_>>(),
        )
        .into_column(),
        Series::new(
            "locked_upper_circuit".into(),
            rows.iter()
                .map(|r| r.locked_upper_circuit)
                .collect::<Vec<_>>(),
        )
        .into_column(),
    ];
    Ok(DataFrame::new(rows.len(), columns)?)
}

/// The empty candidate frame: every column of [`CANDIDATE_COLUMNS`], typed, no rows.
///
/// # Errors
///
/// [`CoreError::Polars`] if polars cannot build the frame.
pub fn empty_candidates() -> CoreResult<DataFrame> {
    to_frame(&[])
}

/// docs/swing/04 §2 — the flag / continuation setup, one row per qualifying instrument.
///
/// # Errors
///
/// [`CoreError::MissingColumn`] when `indicated` lacks an indicator column;
/// [`CoreError::InvalidInput`] for an unrepresentable config or date.
pub fn detect_flags(
    indicated: &DataFrame,
    as_of: NaiveDate,
    config: &SwingConfig,
) -> CoreResult<DataFrame> {
    let ind = Indicated::read(indicated)?;
    to_frame(&flag_candidates(&ind, days_since_epoch(as_of)?, config)?)
}

/// docs/swing/04 §3 — the episodic pivot, detected on the gap day itself.
///
/// # Errors
///
/// As [`detect_flags`].
pub fn detect_eps(
    indicated: &DataFrame,
    as_of: NaiveDate,
    config: &SwingConfig,
) -> CoreResult<DataFrame> {
    let ind = Indicated::read(indicated)?;
    to_frame(&ep_candidates(&ind, days_since_epoch(as_of)?, config)?)
}

/// docs/swing/04 §4 — the exhausted runner. Never a BUY line.
///
/// # Errors
///
/// As [`detect_flags`].
pub fn detect_parabolic(
    indicated: &DataFrame,
    as_of: NaiveDate,
    config: &SwingConfig,
) -> CoreResult<DataFrame> {
    let ind = Indicated::read(indicated)?;
    to_frame(&parabolic_candidates(
        &ind,
        days_since_epoch(as_of)?,
        config,
    )?)
}

/// Polars' default sort order for a nullable float key, descending: nulls first, then NaN
/// (the largest value), then numbers from high to low.
fn desc_nulls_first(a: F, b: F) -> Ordering {
    match (a, b) {
        (None, None) => Ordering::Equal,
        (None, Some(_)) => Ordering::Less,
        (Some(_), None) => Ordering::Greater,
        (Some(x), Some(y)) => total_cmp(y, x),
    }
}

/// Ascending with nulls first (polars' default `nulls_last=False`).
fn asc_nulls_first(a: Option<i64>, b: Option<i64>) -> Ordering {
    match (a, b) {
        (None, None) => Ordering::Equal,
        (None, Some(_)) => Ordering::Less,
        (Some(_), None) => Ordering::Greater,
        (Some(x), Some(y)) => x.cmp(&y),
    }
}

/// Every setup for one as-of date, best score first within each setup.
///
/// `indicated` is the output of `with_swing_indicators` (the Python caller runs
/// `detect_setups(with_swing_indicators(bars, config), as_of, config)`). The rows are the
/// flags, the EPs and the parabolic runners, sorted by `setup` ascending, `score` descending
/// and `instrument_id` ascending.
///
/// # Errors
///
/// As [`detect_flags`].
pub fn detect_setups(
    indicated: &DataFrame,
    as_of: NaiveDate,
    config: &SwingConfig,
) -> CoreResult<DataFrame> {
    let ind = Indicated::read(indicated)?;
    let day = days_since_epoch(as_of)?;
    let mut rows = flag_candidates(&ind, day, config)?;
    rows.extend(ep_candidates(&ind, day, config)?);
    rows.extend(parabolic_candidates(&ind, day, config)?);
    rows.sort_by(|a, b| {
        a.setup
            .cmp(b.setup)
            .then_with(|| desc_nulls_first(a.score, b.score))
            .then_with(|| asc_nulls_first(a.instrument_id, b.instrument_id))
    });
    to_frame(&rows)
}

// ---------------------------------------------------------------------------
// `baskfy_core.swing.indicators.with_swing_indicators`
// ---------------------------------------------------------------------------
//
// `swing::indicators` is declared but outside this port's ownership and still empty; the
// detectors' goldens grade `detect_setups(with_swing_indicators(bars, config), as_of, config)`,
// so the indicator pass lives here until that module is ported.

/// Columns [`with_swing_indicators`] expects on the input frame.
pub const REQUIRED_COLUMNS: [&str; 7] = [
    "instrument_id",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
];

/// Return-lookback columns the detectors read, as `ret_{bars}`.
pub const RETURN_BARS: [i64; 4] = [5, 10, 20, 60];

/// `rolling_mean(window)` with polars' default `min_periods = window`: null until `window`
/// non-null values are in the window (a null inside the window makes it null).
fn rolling_mean(values: &[F], window: i64) -> Vec<F> {
    let Ok(w) = usize::try_from(window) else {
        return vec![None; values.len()];
    };
    if w == 0 {
        return vec![None; values.len()];
    }
    (0..values.len())
        .map(|i| {
            if i + 1 < w {
                return None;
            }
            let slice = &values[i + 1 - w..=i];
            let mut sum = 0.0;
            for v in slice {
                sum += (*v)?;
            }
            let count = i64::try_from(w).ok()?;
            Some(sum / int_as_float(count))
        })
        .collect()
}

/// `shift(k)` within one instrument.
fn shift(values: &[F], k: usize) -> Vec<F> {
    (0..values.len())
        .map(|i| if i >= k { values[i - k] } else { None })
        .collect()
}

/// Add every indicator column the detectors read, per instrument, in one pass.
///
/// Port of `with_swing_indicators`: the frame sorted by `(instrument_id, date)`, the optional
/// columns (`turnover`, `upper_circuit`, `adj_factor`, `symbol`) added as nulls when absent,
/// `adj_factor` null filled with 1.0, then `prev_close`, `range_pct`, `turnover_inr`,
/// `gap_pct`, `adr_pct`, `turnover_avg`, `ma_fast`, `ma_slow`, `ma_trend`, `vol_avg_fast`,
/// `vol_avg_rvol` (excluding today), `ret_5/10/20/60`, `close_position`, `rvol` and
/// `up_streak` (`Int32`). Nothing is rounded: these are inputs to a decision.
///
/// # Errors
///
/// [`CoreError::InvalidInput`] when a required column is missing (Python's `ValueError`);
/// [`CoreError::Polars`] when a column cannot be cast.
pub fn with_swing_indicators(bars: &DataFrame, config: &SwingConfig) -> CoreResult<DataFrame> {
    let present: Vec<String> = bars
        .columns()
        .iter()
        .map(|c| c.name().to_string())
        .collect();
    let missing: Vec<&str> = REQUIRED_COLUMNS
        .iter()
        .copied()
        .filter(|c| !present.iter().any(|p| p == c))
        .collect();
    if !missing.is_empty() {
        return Err(CoreError::InvalidInput(format!(
            "bars are missing required columns {missing:?}; got {present:?}"
        )));
    }
    let height = bars.height();
    let ids = ints(bars, "instrument_id")?;
    let dates_col = column(bars, "date")?.cast(&DataType::Date)?;
    let dates: Vec<Option<i32>> = dates_col.date()?.physical().iter().collect();

    // `bars.sort(["instrument_id", "date"])`: nulls first, a stable order for ties.
    let mut order: Vec<usize> = (0..height).collect();
    order.sort_by(|&a, &b| {
        asc_nulls_first(ids[a], ids[b])
            .then_with(|| asc_nulls_first(dates[a].map(i64::from), dates[b].map(i64::from)))
    });
    let take = |values: &[F]| -> Vec<F> { order.iter().map(|&i| values[i]).collect() };

    let optional_floats = |name: &str| -> CoreResult<Vec<F>> {
        if present.iter().any(|p| p == name) {
            floats(bars, name)
        } else {
            Ok(vec![None; height])
        }
    };
    let open = take(&floats(bars, "open")?);
    let high = take(&floats(bars, "high")?);
    let low = take(&floats(bars, "low")?);
    let close = take(&floats(bars, "close")?);
    let volume = take(&floats(bars, "volume")?);
    let turnover = take(&optional_floats("turnover")?);
    let upper_circuit = take(&optional_floats("upper_circuit")?);
    let adj_factor: Vec<F> = take(&optional_floats("adj_factor")?)
        .into_iter()
        .map(|v| v.or(Some(1.0)))
        .collect();
    let sorted_ids: Vec<Option<i64>> = order.iter().map(|&i| ids[i]).collect();

    // Contiguous runs of one instrument (the frame is sorted by it): `.over("instrument_id")`.
    let mut runs: Vec<std::ops::Range<usize>> = Vec::new();
    let mut start = 0;
    for i in 1..=height {
        if i == height || sorted_ids[i] != sorted_ids[start] {
            if i > start {
                runs.push(start..i);
            }
            start = i;
        }
    }

    let range_pct: Vec<F> = high
        .iter()
        .zip(&low)
        .map(|(&h, &l)| pct_change(h, l))
        .collect();
    let turnover_inr: Vec<F> = (0..height)
        .map(|i| {
            if is_true(and(Some(turnover[i].is_some()), gt(turnover[i], lit(0.0)))) {
                turnover[i]
            } else {
                mul(close[i], volume[i])
            }
        })
        .collect();
    let close_position: Vec<F> = (0..height)
        .map(|i| {
            if is_true(gt(high[i], low[i])) {
                div(sub(close[i], low[i]), sub(high[i], low[i]))
            } else {
                Some(0.5)
            }
        })
        .collect();

    let liquidity = &config.liquidity;
    let mut prev_close = vec![None; height];
    let mut adr_pct = vec![None; height];
    let mut turnover_avg = vec![None; height];
    let mut ma_fast = vec![None; height];
    let mut ma_slow = vec![None; height];
    let mut ma_trend = vec![None; height];
    let mut vol_avg_fast = vec![None; height];
    let mut vol_avg_rvol = vec![None; height];
    let mut rets: Vec<Vec<F>> = vec![vec![None; height]; RETURN_BARS.len()];
    let mut up_streak: Vec<Option<i32>> = vec![None; height];
    for run in &runs {
        let r = run.clone();
        let write = |dst: &mut Vec<F>, src: Vec<F>| {
            for (slot, v) in dst[r.clone()].iter_mut().zip(src) {
                *slot = v;
            }
        };
        let c = &close[r.clone()];
        write(&mut prev_close, shift(c, 1));
        write(
            &mut adr_pct,
            rolling_mean(&range_pct[r.clone()], liquidity.adr_bars),
        );
        write(
            &mut turnover_avg,
            rolling_mean(&turnover_inr[r.clone()], liquidity.turnover_bars),
        );
        write(&mut ma_fast, rolling_mean(c, config.ma_fast));
        write(&mut ma_slow, rolling_mean(c, config.ma_slow));
        write(&mut ma_trend, rolling_mean(c, config.ma_trend));
        write(
            &mut vol_avg_fast,
            rolling_mean(&volume[r.clone()], config.flag.dryup_bars),
        );
        write(
            &mut vol_avg_rvol,
            rolling_mean(&shift(&volume[r.clone()], 1), config.ep.rvol_bars),
        );
        for (slot, &n) in rets.iter_mut().zip(RETURN_BARS.iter()) {
            let k = usize::try_from(n).unwrap_or(usize::MAX);
            let ret: Vec<F> = c
                .iter()
                .zip(shift(c, k))
                .map(|(&now, then)| pct_change(now, then))
                .collect();
            write(slot, ret);
        }
        // A run-length count: every down (or first) bar starts a new group, and the streak is
        // the cumulative count of up bars inside the group.
        let mut streak: i32 = 0;
        for i in r.clone() {
            let up = is_true(gt(close[i], prev_close[i]));
            streak = if up { streak.saturating_add(1) } else { 0 };
            up_streak[i] = Some(streak);
        }
    }
    let gap_pct: Vec<F> = (0..height)
        .map(|i| pct_change(open[i], prev_close[i]))
        .collect();
    let rvol: Vec<F> = (0..height)
        .map(|i| {
            if is_true(gt(vol_avg_rvol[i], lit(0.0))) {
                div(volume[i], vol_avg_rvol[i])
            } else {
                None
            }
        })
        .collect();

    // The input's own columns (sorted, the five prices and turnover/upper_circuit/adj_factor
    // cast to Float64), the absent optional columns appended, then the indicators.
    let mut columns: Vec<Column> = Vec::new();
    for name in &present {
        // `instrument_id`, `date` and a present `symbol` keep their input dtype, as Python's
        // `_prepare` leaves them uncast; only their row order changes.
        let col = match name.as_str() {
            "open" => Some(fseries(name, open.clone())),
            "high" => Some(fseries(name, high.clone())),
            "low" => Some(fseries(name, low.clone())),
            "close" => Some(fseries(name, close.clone())),
            "volume" => Some(fseries(name, volume.clone())),
            "turnover" => Some(fseries(name, turnover.clone())),
            "upper_circuit" => Some(fseries(name, upper_circuit.clone())),
            "adj_factor" => Some(fseries(name, adj_factor.clone())),
            _ => None,
        };
        let col = match col {
            Some(c) => c,
            None => {
                let raw = column(bars, name)?.as_materialized_series();
                let idx: Vec<Option<IdxSize>> =
                    order.iter().map(|&i| IdxSize::try_from(i).ok()).collect();
                if idx.iter().any(Option::is_none) {
                    return Err(CoreError::InvalidInput(format!(
                        "bars has more rows than polars can index ({height})"
                    )));
                }
                let idx = IdxCa::new("idx".into(), idx);
                raw.take(&idx)?.into_column()
            }
        };
        columns.push(col);
    }
    for name in ["turnover", "upper_circuit", "adj_factor"] {
        if !present.iter().any(|p| p == name) {
            let values = match name {
                "turnover" => turnover.clone(),
                "upper_circuit" => upper_circuit.clone(),
                _ => adj_factor.clone(),
            };
            columns.push(fseries(name, values));
        }
    }
    if !present.iter().any(|p| p == "symbol") {
        columns.push(Series::new("symbol".into(), vec![None::<String>; height]).into_column());
    }
    columns.push(fseries("prev_close", prev_close));
    columns.push(fseries("range_pct", range_pct));
    columns.push(fseries("turnover_inr", turnover_inr));
    columns.push(fseries("gap_pct", gap_pct));
    columns.push(fseries("adr_pct", adr_pct));
    columns.push(fseries("turnover_avg", turnover_avg));
    columns.push(fseries("ma_fast", ma_fast));
    columns.push(fseries("ma_slow", ma_slow));
    columns.push(fseries("ma_trend", ma_trend));
    columns.push(fseries("vol_avg_fast", vol_avg_fast));
    columns.push(fseries("vol_avg_rvol", vol_avg_rvol));
    for (values, n) in rets.into_iter().zip(RETURN_BARS) {
        columns.push(fseries(&format!("ret_{n}"), values));
    }
    columns.push(fseries("close_position", close_position));
    columns.push(fseries("rvol", rvol));
    columns.push(Series::new("up_streak".into(), up_streak).into_column());
    Ok(DataFrame::new(height, columns)?)
}

fn fseries(name: &str, values: Vec<F>) -> Column {
    Series::new(name.into(), values).into_column()
}
