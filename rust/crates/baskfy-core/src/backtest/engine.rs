//! `baskfy_core.backtest.run_backtest` and `run_fragility`.
//!
//! Ported from `decile-blueprint/packages/core/src/baskfy_core/backtest.py` (docs/10
//! §"Execution model", steps 1-8, in order) and graded by `tests/golden_backtest_engine.rs`
//! against `go/testdata/golden/L1/backtest/run_{backtest,fragility}.*`.
//!
//! The schedules, weights, costs, price panel and the look-ahead guard are
//! [`super::helpers`]; this file is the day walk that uses them. Every read of dated data goes
//! through [`PointInTimeReader`] (house rule 5): the screen and the benchmark are checked
//! against the cursor, and a row dated after it is a [`BacktestError::LookAhead`].
//!
//! **Arithmetic.** Python carries every rupee as `Decimal` in its default context (28
//! significant digits, half-even), so the walk computes in [`WideDecimal`], which reproduces
//! that context exactly — every division (`equity * weight / price`, a sale's cost basis, the
//! benchmark's daily growth, a snapshot's average price and weight) rounds where Python's does
//! (DECISIONS-RS R1.3). Results leave as `rust_decimal::Decimal`, scale kept.
//!
//! **The rank-buffer rule** (`baskfy_core.rank_buffer.plan_rebalance`) is not ported as a
//! module yet; the private [`rank_buffer`] below reproduces exactly the part of it the engine
//! reads — the union of `entries`, `holds` and `inside_wrh`.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use chrono::NaiveDate;
use polars::prelude::{AnyValue, DataFrame, DataType};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use super::helpers::{
    AdjustmentCoverage, BacktestData, BacktestError, CostSpec, MONEY_EXPONENT, PRICE_EXPONENT,
    PointInTimeReader, PositionLimits, PricePanel, RebalanceSpec, Rounding, Selected,
    WEIGHT_EXPONENT, Weighting, WideDecimal, apply_position_limits, basis_points, raw_weights,
    rebalance_dates, shift_schedule,
};

// =============================================================================================
// Constants
// =============================================================================================

/// `RISK_OVERLAY_WINDOW`: docs/10's only overlay rule is `index_above_200dma`.
pub const RISK_OVERLAY_WINDOW: usize = 200;

/// `_PROGRESS_EVERY`: a progress event every 250 trading days (about a year).
const PROGRESS_EVERY: usize = 250;

/// `_SYMBOLS_IN_NOTE`: how many symbols an unfillable-day note names before summarising.
const SYMBOLS_IN_NOTE: usize = 6;

/// The rank a name absent from a decision's ranks sorts at (`10**9` in Python).
const UNRANKED: i64 = 1_000_000_000;

// =============================================================================================
// Configuration — docs/10 §Config, key for key
// =============================================================================================

fn default_top_n() -> i64 {
    20
}

fn default_hold_buffer() -> i64 {
    10
}

/// `SelectionSpec` (docs/10): `{"top_n": 20, "hold_buffer": 10}`; a buffer of 0 is strict top-N.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SelectionSpec {
    /// How many names the book targets, `1..=500`.
    #[serde(default = "default_top_n")]
    pub top_n: i64,
    /// How far past `top_n` a held name may fall and still be kept, `0..=500`.
    #[serde(default = "default_hold_buffer")]
    pub hold_buffer: i64,
}

impl Default for SelectionSpec {
    fn default() -> Self {
        Self {
            top_n: default_top_n(),
            hold_buffer: default_hold_buffer(),
        }
    }
}

/// `CashPolicy` (docs/10): `hold_cash | benchmark`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CashPolicy {
    /// Uninvested cash earns nothing (the default).
    #[default]
    HoldCash,
    /// Uninvested cash earns the benchmark's daily return.
    Benchmark,
}

/// `DividendPolicy` (docs/10 §7): the stored close is a price-return series, so only `ignore`
/// needs no dividend schedule.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DividendPolicy {
    /// Would need a total-return series; refused without a schedule.
    Reinvest,
    /// Credit each dividend to cash on its ex-date; refused without a schedule.
    Cash,
    /// A pure price return (the default).
    #[default]
    Ignore,
}

impl DividendPolicy {
    /// The enum's value (`"reinvest"`).
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::Reinvest => "reinvest",
            Self::Cash => "cash",
            Self::Ignore => "ignore",
        }
    }
}

/// `RiskRule` (docs/10 §Config).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
pub enum RiskRule {
    /// Invested only while the benchmark closes at or above its 200-day average.
    #[default]
    #[serde(rename = "index_above_200dma")]
    IndexAbove200dma,
}

/// `RiskOverlay` (docs/10): `{"enabled": false, "rule": "index_above_200dma"}`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RiskOverlay {
    /// Whether the overlay is on.
    #[serde(default)]
    pub enabled: bool,
    /// The rule it applies.
    #[serde(default)]
    pub rule: RiskRule,
}

/// A JSON value inside `BacktestConfig.screen_definition` (`dict[str, object]`), carried for
/// the record only — the engine never reads it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum ConfigValue {
    /// `None`.
    Null,
    /// A boolean.
    Bool(bool),
    /// An integer.
    Int(i64),
    /// A float.
    Float(f64),
    /// A string.
    Str(String),
    /// A list.
    List(Vec<ConfigValue>),
    /// A mapping.
    Map(BTreeMap<String, ConfigValue>),
}

fn default_initial_capital() -> Decimal {
    Decimal::from(1_000_000)
}

fn default_benchmark() -> String {
    "nifty-500".to_owned()
}

/// `BacktestConfig` (docs/10 §Config, key for key, plus `dividends` and the risk-free inputs).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BacktestConfig {
    /// The screen's public id, carried for the record.
    #[serde(default)]
    pub screen_public_id: Option<String>,
    /// An inline screen definition, carried for the record.
    #[serde(default)]
    pub screen_definition: Option<BTreeMap<String, ConfigValue>>,
    /// First day of the window.
    pub start: NaiveDate,
    /// Last day of the window; must be after `start`.
    pub end: NaiveDate,
    /// Starting cash, rupees, `> 0`.
    #[serde(default = "default_initial_capital")]
    pub initial_capital: Decimal,
    /// The rebalance calendar.
    #[serde(default)]
    pub rebalance: RebalanceSpec,
    /// Top-N and the hold buffer.
    #[serde(default)]
    pub selection: SelectionSpec,
    /// The pre-clip weighting scheme.
    #[serde(default)]
    pub weighting: Weighting,
    /// Per-name weight bounds.
    #[serde(default)]
    pub position_limits: PositionLimits,
    /// The cost stack, charged on both sides.
    #[serde(default)]
    pub costs: CostSpec,
    /// What uninvested cash earns.
    #[serde(default)]
    pub cash_policy: CashPolicy,
    /// The benchmark's slug.
    #[serde(default = "default_benchmark")]
    pub benchmark: String,
    /// The risk overlay.
    #[serde(default)]
    pub risk_overlay: RiskOverlay,
    /// The dividend policy.
    #[serde(default)]
    pub dividends: DividendPolicy,
    /// Annualised risk-free rate, `0..=1` (read by the metrics, not the engine).
    #[serde(default)]
    pub risk_free_rate: Decimal,
    /// Monthly `(date, annual rate)` observations (read by the metrics, not the engine).
    #[serde(default)]
    pub risk_free_curve: Vec<(NaiveDate, Decimal)>,
}

impl BacktestConfig {
    /// pydantic's field bounds and validators, which Python runs at construction.
    ///
    /// # Errors
    /// [`BacktestError::Validation`] naming the violated bound.
    pub fn validate(&self) -> Result<(), BacktestError> {
        if self.end <= self.start {
            return Err(BacktestError::Validation(format!(
                "end {} must be after start {}",
                self.end, self.start
            )));
        }
        if self.initial_capital <= Decimal::ZERO {
            return Err(BacktestError::Validation(format!(
                "initial_capital {} must be > 0",
                self.initial_capital
            )));
        }
        if !(1..=500).contains(&self.selection.top_n) {
            return Err(BacktestError::Validation(format!(
                "top_n {} must be >= 1 and <= 500",
                self.selection.top_n
            )));
        }
        if !(0..=500).contains(&self.selection.hold_buffer) {
            return Err(BacktestError::Validation(format!(
                "hold_buffer {} must be >= 0 and <= 500",
                self.selection.hold_buffer
            )));
        }
        if self.risk_free_rate < Decimal::ZERO || self.risk_free_rate > Decimal::ONE {
            return Err(BacktestError::Validation(format!(
                "risk_free_rate {} must be >= 0 and <= 1",
                self.risk_free_rate
            )));
        }
        self.position_limits.validate()?;
        self.costs.validate()
    }

    /// `with_costs(costs)`: the same configuration with another cost stack.
    #[must_use]
    pub fn with_costs(&self, costs: CostSpec) -> Self {
        Self {
            costs,
            ..self.clone()
        }
    }
}

// =============================================================================================
// Results
// =============================================================================================

/// `TradeSide`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TradeSide {
    /// A purchase.
    Buy,
    /// A sale.
    Sell,
}

/// `TradeReason` (docs/10 §Artefacts): `enter|exit|rebalance|delist`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TradeReason {
    /// A name the book did not hold.
    Enter,
    /// A held name the decision dropped.
    Exit,
    /// A held name resized.
    Rebalance,
    /// A forced sale of a dead instrument.
    Delist,
}

/// `Trade`: one fill (docs/10 §Artefacts).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Trade {
    /// The fill day.
    pub date: NaiveDate,
    /// `instrument.id`.
    pub instrument_id: i64,
    /// Trading symbol (or `#<id>`).
    pub symbol: String,
    /// Buy or sell.
    pub side: TradeSide,
    /// Whole shares.
    pub quantity: i64,
    /// The fill price, four places.
    pub price: Decimal,
    /// `price * quantity`, to the paisa.
    pub notional: Decimal,
    /// The cost charged on the notional.
    pub cost: Decimal,
    /// Why.
    pub reason: TradeReason,
    /// Realised profit on the shares this sale closed, net of its cost; `None` on a buy.
    pub realised_pnl: Option<Decimal>,
}

/// `HoldingSnapshot`: what was held after one rebalance was executed.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HoldingSnapshot {
    /// The decision day.
    pub rebalance_date: NaiveDate,
    /// The fill day.
    pub executed_on: NaiveDate,
    /// `instrument.id`.
    pub instrument_id: i64,
    /// Trading symbol.
    pub symbol: String,
    /// Display name.
    pub name: String,
    /// The screen rank the decision used; `None` for a name it did not rank.
    pub rank: Option<i64>,
    /// The weight the decision asked for.
    pub target_weight: Decimal,
    /// Shares held.
    pub quantity: i64,
    /// `value / quantity`, four places.
    pub price: Decimal,
    /// The position at the fill day's open (last close when it did not open), to the paisa.
    pub value: Decimal,
    /// `value / equity`, six places.
    pub actual_weight: Decimal,
}

/// `DelistingEvent`: a position closed because its instrument stopped trading.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DelistingEvent {
    /// The liquidation day.
    pub date: NaiveDate,
    /// `instrument.id`.
    pub instrument_id: i64,
    /// Trading symbol.
    pub symbol: String,
    /// Shares sold.
    pub quantity: i64,
    /// The last available close.
    pub price: Decimal,
    /// The day that close printed.
    pub priced_on: NaiveDate,
    /// `notional - cost`.
    pub proceeds: Decimal,
}

/// `BacktestProgress`: one SSE frame's worth of state.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BacktestProgress {
    /// The stage (`"simulating"`).
    pub stage: String,
    /// Steps done.
    pub completed: usize,
    /// Steps in all.
    pub total: usize,
    /// The day just simulated.
    pub as_of: Option<NaiveDate>,
}

impl BacktestProgress {
    /// `percent`: `round(100 * completed / total)` capped at 100, 0 for no steps (Python's
    /// `round` is half-even).
    #[must_use]
    pub fn percent(&self) -> usize {
        if self.total == 0 {
            return 0;
        }
        let scaled = 100 * self.completed;
        let (q, r) = (scaled / self.total, scaled % self.total);
        let rounded = match (2 * r).cmp(&self.total) {
            std::cmp::Ordering::Less => q,
            std::cmp::Ordering::Greater => q + 1,
            std::cmp::Ordering::Equal => q + (q % 2),
        };
        rounded.min(100)
    }
}

/// `BacktestResult`: everything docs/10 §Outputs asks for.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BacktestResult {
    /// The configuration that ran.
    pub config: BacktestConfig,
    /// Every simulated trading day.
    pub dates: Vec<NaiveDate>,
    /// Cash plus invested, per day.
    pub equity: Vec<Decimal>,
    /// Cash, per day.
    pub cash: Vec<Decimal>,
    /// The book marked to the close, per day.
    pub invested: Vec<Decimal>,
    /// The benchmark level, per day (`None` without one).
    pub benchmark: Vec<Option<Decimal>>,
    /// Every fill.
    pub trades: Vec<Trade>,
    /// Per-rebalance holdings, as filled.
    pub holdings: Vec<HoldingSnapshot>,
    /// Forced liquidations.
    pub delistings: Vec<DelistingEvent>,
    /// Sum of every trade's cost.
    pub total_costs: Decimal,
    /// Cash dividends credited.
    pub dividends_credited: Decimal,
    /// The schedule the run used.
    pub rebalance_dates: Vec<NaiveDate>,
    /// Rebalance dates whose screen returned no rows.
    pub blind_rebalances: Vec<NaiveDate>,
    /// What the price series can and cannot support, as the caller supplied it.
    pub adjustment_coverage: Option<AdjustmentCoverage>,
    /// Fill days on which a decision existed and nothing could be traded.
    pub unexecuted_fills: Vec<NaiveDate>,
    /// Notes, first occurrence order, deduplicated.
    pub notes: Vec<String>,
    /// The `pipeline_run.data_version` behind the panel.
    pub data_version: Option<i64>,
}

impl BacktestResult {
    /// `final_equity`: the last equity, or the initial capital for an empty run.
    #[must_use]
    pub fn final_equity(&self) -> Decimal {
        self.equity
            .last()
            .copied()
            .unwrap_or(self.config.initial_capital)
    }

    /// `blind_fraction`: blind rebalances over rebalances (28 significant digits), 0 without any.
    ///
    /// # Errors
    /// Never in practice (the divisor is a nonzero count).
    pub fn blind_fraction(&self) -> Result<WideDecimal, BacktestError> {
        if self.rebalance_dates.is_empty() {
            return Ok(WideDecimal::zero());
        }
        WideDecimal::from(self.blind_rebalances.len())
            .div(&WideDecimal::from(self.rebalance_dates.len()))
    }
}

/// `FragilityRun`: one perturbation and what it produced.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FragilityRun {
    /// `"base"`, `"costs_minus_25pct"`, ...
    pub label: String,
    /// What was perturbed.
    pub description: String,
    /// The run.
    pub result: BacktestResult,
}

/// `FragilityReport`: the base run and docs/10's four perturbations.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FragilityReport {
    /// The configuration as submitted.
    pub base: FragilityRun,
    /// Costs -25 %, +25 %; every rebalance -1, +1 trading day.
    pub variants: Vec<FragilityRun>,
}

/// A progress callback (`ProgressSink`).
pub type ProgressSink<'a> = &'a mut dyn FnMut(&BacktestProgress);

// =============================================================================================
// Python-value helpers
// =============================================================================================

/// Python's `repr(float)` (the shortest round-trip digits; scientific below 1e-4 and from
/// 1e16; an integral value keeps `.0`).
fn py_float_repr(v: f64) -> String {
    if v.is_nan() {
        return "nan".to_owned();
    }
    if v.is_infinite() {
        return if v > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    let sign = if v.is_sign_negative() { "-" } else { "" };
    if v.to_bits() << 1 == 0 {
        return format!("{sign}0.0");
    }
    let sci = format!("{:e}", v.abs());
    let (mantissa, exp) = sci.split_once('e').unwrap_or((sci.as_str(), "0"));
    let exp: i64 = exp.parse().unwrap_or(0);
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let len = i64::try_from(digits.len()).unwrap_or(i64::MAX);
    if (-4..16).contains(&exp) {
        if exp >= 0 {
            let int_len = exp + 1;
            if len <= int_len {
                let pad = usize::try_from(int_len - len).unwrap_or(0);
                format!("{sign}{digits}{}.0", "0".repeat(pad))
            } else {
                let cut = usize::try_from(int_len).unwrap_or(0);
                format!("{sign}{}.{}", &digits[..cut], &digits[cut..])
            }
        } else {
            let pad = usize::try_from(-exp - 1).unwrap_or(0);
            format!("{sign}0.{}{digits}", "0".repeat(pad))
        }
    } else {
        let (first, rest) = digits.split_at(1);
        let frac = if rest.is_empty() {
            String::new()
        } else {
            format!(".{rest}")
        };
        let esign = if exp < 0 { '-' } else { '+' };
        format!("{sign}{first}{frac}e{esign}{:02}", exp.abs())
    }
}

/// Python's `str(value)` of a polars cell as `to_list()`/`iter_rows()` hands it over; `None`
/// for a null.
fn py_str(value: &AnyValue<'_>) -> Option<String> {
    Some(match value {
        AnyValue::Null => return None,
        AnyValue::Boolean(b) => if *b { "True" } else { "False" }.to_owned(),
        AnyValue::Int8(v) => v.to_string(),
        AnyValue::Int16(v) => v.to_string(),
        AnyValue::Int32(v) => v.to_string(),
        AnyValue::Int64(v) => v.to_string(),
        AnyValue::UInt8(v) => v.to_string(),
        AnyValue::UInt16(v) => v.to_string(),
        AnyValue::UInt32(v) => v.to_string(),
        AnyValue::UInt64(v) => v.to_string(),
        AnyValue::Float32(v) => py_float_repr(f64::from(*v)),
        AnyValue::Float64(v) => py_float_repr(*v),
        AnyValue::String(s) => (*s).to_owned(),
        AnyValue::StringOwned(s) => s.to_string(),
        other => other.to_string(),
    })
}

/// Python's `int(value)` of a polars cell (a float truncates, a string parses).
fn py_int(value: &AnyValue<'_>, column: &str) -> Result<i64, BacktestError> {
    let bad = || BacktestError::Data(format!("{column} holds {value}, which is not an integer"));
    match value {
        AnyValue::Boolean(b) => Ok(i64::from(*b)),
        AnyValue::Int8(v) => Ok(i64::from(*v)),
        AnyValue::Int16(v) => Ok(i64::from(*v)),
        AnyValue::Int32(v) => Ok(i64::from(*v)),
        AnyValue::Int64(v) => Ok(*v),
        AnyValue::UInt8(v) => Ok(i64::from(*v)),
        AnyValue::UInt16(v) => Ok(i64::from(*v)),
        AnyValue::UInt32(v) => Ok(i64::from(*v)),
        AnyValue::UInt64(v) => i64::try_from(*v).map_err(|_| bad()),
        AnyValue::Float32(v) => float_to_int(f64::from(*v)).ok_or_else(bad),
        AnyValue::Float64(v) => float_to_int(*v).ok_or_else(bad),
        AnyValue::String(s) => s.trim().parse().map_err(|_| bad()),
        AnyValue::StringOwned(s) => s.trim().parse().map_err(|_| bad()),
        _ => Err(bad()),
    }
}

fn float_to_int(v: f64) -> Option<i64> {
    let text = py_float_repr(v.trunc());
    if !v.is_finite() {
        return None;
    }
    text.parse::<WideDecimal>()
        .ok()
        .and_then(|w| w.quantize(0, Rounding::Down).ok())
        .and_then(|w| w.to_string().parse().ok())
}

/// `_optional_decimal(value)`: `Decimal(str(value))` for a number, `None` for a null.
fn optional_decimal(value: &AnyValue<'_>) -> Result<Option<WideDecimal>, BacktestError> {
    match value {
        AnyValue::Null => Ok(None),
        AnyValue::Int8(_)
        | AnyValue::Int16(_)
        | AnyValue::Int32(_)
        | AnyValue::Int64(_)
        | AnyValue::UInt8(_)
        | AnyValue::UInt16(_)
        | AnyValue::UInt32(_)
        | AnyValue::UInt64(_)
        | AnyValue::Float32(_)
        | AnyValue::Float64(_)
        | AnyValue::String(_)
        | AnyValue::StringOwned(_) => py_str(value).unwrap_or_default().parse().map(Some),
        other => Err(BacktestError::Data(format!(
            "{other} is not a number the engine can weight by"
        ))),
    }
}

fn has_column(frame: &DataFrame, name: &str) -> bool {
    frame.get_column_names().iter().any(|c| c.as_str() == name)
}

fn cell<'a>(frame: &'a DataFrame, column: &str, row: usize) -> Result<AnyValue<'a>, BacktestError> {
    Ok(frame.column(column)?.get(row)?)
}

fn wide(value: Decimal) -> WideDecimal {
    WideDecimal::from(value)
}

/// `_money(value)`, kept in Python's context.
fn money(value: &WideDecimal) -> Result<WideDecimal, BacktestError> {
    value.quantize(MONEY_EXPONENT, Rounding::HalfUp)
}

/// `int(value.to_integral_value(rounding=ROUND_DOWN))`.
fn int_down(value: &WideDecimal) -> Result<i64, BacktestError> {
    let whole = value.quantize(0, Rounding::Down)?;
    whole
        .to_string()
        .parse()
        .map_err(|_| BacktestError::Decimal(format!("{whole} does not fit a 64-bit integer")))
}

fn out(value: &WideDecimal) -> Result<Decimal, BacktestError> {
    value.try_to_decimal()
}

fn days_to_date(days: i32) -> Option<NaiveDate> {
    NaiveDate::default().checked_add_signed(chrono::TimeDelta::days(i64::from(days)))
}

/// Python's `list.__repr__` of strings, for the missing-column message.
fn py_list_repr(items: &[&str]) -> String {
    let inner: Vec<String> = items.iter().map(|s| format!("'{s}'")).collect();
    format!("[{}]", inner.join(", "))
}

// =============================================================================================
// The rank-buffer rule (the part of `baskfy_core.rank_buffer.plan_rebalance` the engine reads)
// =============================================================================================

mod rank_buffer {
    //! `plan_rebalance(...)`'s `entries ∪ holds ∪ inside_wrh`, by instrument id.

    use std::collections::{BTreeMap, BTreeSet};

    use super::Selected;

    /// The ids the plan keeps: every screened name ranked `<= top_n` that is not held
    /// (`entries`), and every held name the screen ranks `<= top_n + hold_buffer` (`holds` and
    /// `inside_wrh`). The screen is read as Python's `by_id` dict: a repeated id keeps its
    /// last row.
    pub(super) fn kept(
        by_id: &BTreeMap<i64, &Selected>,
        held: &BTreeSet<i64>,
        top_n: i64,
        hold_buffer: i64,
    ) -> BTreeSet<i64> {
        let limit = top_n + hold_buffer;
        let mut keep = BTreeSet::new();
        for id in held {
            if let Some(row) = by_id.get(id)
                && row.rank <= limit
            {
                keep.insert(*id);
            }
        }
        for (id, row) in by_id {
            if row.rank <= top_n && !held.contains(id) {
                keep.insert(*id);
            }
        }
        keep
    }
}

// =============================================================================================
// The engine
// =============================================================================================

/// `_Position`.
#[derive(Debug, Clone)]
struct Position {
    quantity: i64,
    /// Rupees paid for the shares held, cost included.
    book_cost: WideDecimal,
}

/// `_Decision`: what one rebalance date decided, waiting for the next open.
#[derive(Debug, Clone)]
struct Decision {
    decided_on: NaiveDate,
    targets: BTreeMap<i64, Decimal>,
    ranks: BTreeMap<i64, i64>,
    reasons: BTreeMap<i64, TradeReason>,
}

impl Decision {
    fn rank_of(&self, id: i64) -> i64 {
        self.ranks.get(&id).copied().unwrap_or(UNRANKED)
    }

    /// Every name the fill touches, best-ranked first.
    fn instruments(&self) -> Vec<i64> {
        let set: BTreeSet<i64> = self
            .targets
            .keys()
            .chain(self.reasons.keys())
            .copied()
            .collect();
        let mut ids: Vec<i64> = set.into_iter().collect();
        ids.sort_by_key(|id| (self.rank_of(*id), *id));
        ids
    }
}

type Book = BTreeMap<i64, Position>;

/// `run_backtest(config, data, progress=..., schedule=...)`: docs/10 §"Execution model",
/// steps 1-8, in order. `schedule` overrides the computed rebalance calendar (the fragility
/// probe's shifted dates).
///
/// # Errors
/// Python's `BacktestError` family: a dividend policy without a schedule
/// ([`BacktestError::Config`]), an empty window, no rebalance dates, a schedule date outside
/// the calendar, a missing screen ([`BacktestError::Data`]), a screen or benchmark row dated
/// after the cursor ([`BacktestError::LookAhead`]), an unusable weighting input; and
/// [`BacktestError::Validation`] for a configuration pydantic would have refused.
pub fn run_backtest(
    config: &BacktestConfig,
    data: &BacktestData,
    progress: Option<ProgressSink<'_>>,
    schedule: Option<&[NaiveDate]>,
) -> Result<BacktestResult, BacktestError> {
    let mut progress = progress;
    config.validate()?;
    if config.dividends != DividendPolicy::Ignore && data.dividends.is_none() {
        return Err(BacktestError::Config(format!(
            "the '{}' dividend policy needs a dividend schedule and none was supplied. The \
             stored close is a PRICE-return series — M28 applied splits and bonuses and \
             deliberately not the 38 dividend-shaped actions — so dividends cannot be assumed \
             to be inside it. See DividendPolicy.",
            config.dividends.value()
        )));
    }

    let mut blind: Vec<NaiveDate> = Vec::new();
    let mut unexecuted: Vec<NaiveDate> = Vec::new();
    let mut reader = PointInTimeReader::new(data);
    let panel = &data.prices;
    let days: Vec<NaiveDate> = data
        .calendar
        .iter()
        .copied()
        .filter(|d| config.start <= *d && *d <= config.end)
        .collect();
    if days.is_empty() {
        return Err(BacktestError::Data(format!(
            "the trading calendar holds no day between {} and {}",
            config.start, config.end
        )));
    }

    let dates: Vec<NaiveDate> = match schedule {
        Some(s) => s.to_vec(),
        None => rebalance_dates(&data.calendar, config.start, config.end, &config.rebalance),
    };
    if dates.is_empty() {
        return Err(BacktestError::Data(
            "the configuration produces no rebalance dates".to_owned(),
        ));
    }

    // Step 4: an order decided on `d` is filled on the next trading day inside the window; a
    // decision with no such day is never taken.
    let mut calendar_index: HashMap<NaiveDate, usize> = HashMap::new();
    for (position, day) in data.calendar.iter().enumerate() {
        calendar_index.insert(*day, position);
    }
    let mut fill_for: HashMap<NaiveDate, NaiveDate> = HashMap::new();
    for decided in &dates {
        let position = calendar_index.get(decided).copied().ok_or_else(|| {
            BacktestError::Data(format!(
                "rebalance date {decided} is not in the trading calendar"
            ))
        })?;
        if let Some(next) = data.calendar.get(position + 1)
            && *next <= config.end
        {
            fill_for.insert(*decided, *next);
        }
    }
    let fills_due: BTreeSet<NaiveDate> = fill_for.values().copied().collect();

    let mut positions: Book = BTreeMap::new();
    let mut cash = money(&wide(config.initial_capital))?;
    let mut trades: Vec<Trade> = Vec::new();
    let mut holdings: Vec<HoldingSnapshot> = Vec::new();
    let mut delistings: Vec<DelistingEvent> = Vec::new();
    let mut notes: Vec<String> = Vec::new();
    let mut total_costs = WideDecimal::zero();
    let mut dividends_credited = WideDecimal::zero();

    let mut equity_series: Vec<Decimal> = Vec::with_capacity(days.len());
    let mut cash_series: Vec<Decimal> = Vec::with_capacity(days.len());
    let mut invested_series: Vec<Decimal> = Vec::with_capacity(days.len());
    let mut benchmark_series: Vec<Option<Decimal>> = Vec::with_capacity(days.len());

    let mut pending: Option<Decision> = None;
    let mut liquidation: HashMap<i64, Option<usize>> = HashMap::new();
    let benchmark_levels = benchmark_levels(data)?;
    let total_steps = days.len();

    for (step, &day) in days.iter().enumerate() {
        reader.advance_to(day)?;
        let position_index = panel.index_of(day)?;

        // --- Step 4: fill yesterday's decision at today's open ----------------------------
        if fills_due.contains(&day)
            && let Some(decision) = pending.take()
        {
            let (new_cash, filled, unfillable) = execute(
                &decision,
                day,
                position_index,
                panel,
                &mut positions,
                cash,
                config,
                data,
            )?;
            cash = new_cash;
            for trade in &filled {
                total_costs = total_costs.add(&wide(trade.cost));
            }
            let nothing_filled = filled.is_empty();
            trades.extend(filled);
            if !unfillable.is_empty() {
                let mut shown: Vec<String> =
                    unfillable.iter().map(|id| data.symbol_for(*id)).collect();
                shown.sort();
                let names = shown
                    .iter()
                    .take(SYMBOLS_IN_NOTE)
                    .cloned()
                    .collect::<Vec<_>>()
                    .join(", ");
                let more = if shown.len() > SYMBOLS_IN_NOTE {
                    format!(" and {} more", shown.len() - SYMBOLS_IN_NOTE)
                } else {
                    String::new()
                };
                let tail = if nothing_filled {
                    "NOTHING was filled that day; the book is unchanged."
                } else {
                    "The rest were filled."
                };
                notes.push(format!(
                    "{} of the {} names decided on {} had no opening price on {day} and could \
                     not be filled ({names}{more}). {tail}",
                    unfillable.len(),
                    decision.instruments().len(),
                    decision.decided_on,
                ));
                if nothing_filled {
                    unexecuted.push(day);
                }
            }
            holdings.extend(snapshot(
                &decision,
                day,
                &positions,
                panel,
                position_index,
                data,
                &cash,
            )?);
        }

        // --- Step 8: delisting --------------------------------------------------------------
        let held_ids: Vec<i64> = positions.keys().copied().collect();
        for instrument_id in held_ids {
            let Some(held) = positions.get(&instrument_id) else {
                continue;
            };
            if held.quantity <= 0 {
                continue;
            }
            let trigger = *liquidation.entry(instrument_id).or_insert_with(|| {
                panel.liquidation_day(instrument_id, data.delistings.get(&instrument_id).copied())
            });
            let Some(trigger) = trigger else {
                continue;
            };
            if position_index < trigger {
                continue;
            }
            match liquidate(
                &reader,
                instrument_id,
                day,
                &mut positions,
                &cash,
                config,
                data,
            )? {
                None => {
                    notes.push(format!(
                        "{} stopped trading before {day} and never printed a close; the \
                         position was written off.",
                        data.symbol_for(instrument_id)
                    ));
                    positions.remove(&instrument_id);
                }
                Some((event, new_cash, trade)) => {
                    cash = new_cash;
                    delistings.push(event);
                    total_costs = total_costs.add(&wide(trade.cost));
                    trades.push(trade);
                }
            }
        }

        // --- Step 7: cash dividends, when the policy credits them ----------------------------
        if config.dividends == DividendPolicy::Cash {
            let credited = credit_dividends(&reader, day, &positions)?;
            cash = money(&cash.add(&credited))?;
            dividends_credited = dividends_credited.add(&credited);
        }

        // --- Uninvested cash, under `cash_policy` ---------------------------------------------
        if config.cash_policy == CashPolicy::Benchmark && step > 0 {
            let growth = benchmark_growth(&benchmark_levels, days[step - 1], day)?;
            cash = money(&cash.mul(&growth))?;
        }

        // --- Steps 1-3: the decision, taken on today's close ----------------------------------
        if fill_for.contains_key(&day) {
            let (decision, decision_notes, screened) =
                decide(&reader, day, &positions, config, &benchmark_levels)?;
            pending = Some(decision);
            notes.extend(decision_notes);
            if screened == 0 {
                blind.push(day);
            }
        }

        // --- Step 6: mark to market -------------------------------------------------------
        let invested = market_value(&positions, panel, position_index)?;
        equity_series.push(out(&money(&cash.add(&invested))?)?);
        cash_series.push(out(&cash)?);
        invested_series.push(out(&invested)?);
        benchmark_series.push(match benchmark_levels.get(&day) {
            Some(level) => Some(out(level)?),
            None => None,
        });

        if let Some(sink) = progress.as_mut()
            && (step % PROGRESS_EVERY == 0 || step == total_steps - 1)
        {
            sink(&BacktestProgress {
                stage: "simulating".to_owned(),
                completed: step + 1,
                total: total_steps,
                as_of: Some(day),
            });
        }
    }

    // `tuple(dict.fromkeys(notes))`: first occurrence order, duplicates dropped.
    let mut seen: BTreeSet<String> = BTreeSet::new();
    notes.retain(|n| seen.insert(n.clone()));

    Ok(BacktestResult {
        config: config.clone(),
        dates: days,
        equity: equity_series,
        cash: cash_series,
        invested: invested_series,
        benchmark: benchmark_series,
        trades,
        holdings,
        delistings,
        total_costs: out(&total_costs)?,
        dividends_credited: out(&dividends_credited)?,
        rebalance_dates: dates,
        blind_rebalances: blind,
        adjustment_coverage: data.adjustment_coverage,
        unexecuted_fills: unexecuted,
        notes,
        data_version: data.data_version,
    })
}

/// `_benchmark_levels(data)`: `date -> Decimal(str(level))`, nulls skipped.
fn benchmark_levels(data: &BacktestData) -> Result<HashMap<NaiveDate, WideDecimal>, BacktestError> {
    let mut levels = HashMap::new();
    let Some(frame) = data.benchmark.as_ref() else {
        return Ok(levels);
    };
    if !has_column(frame, "level") {
        return Err(BacktestError::Data(
            "the benchmark frame needs a 'level' column".to_owned(),
        ));
    }
    let dates = frame.column("date")?;
    if dates.dtype() != &DataType::Date {
        // Python keys the map by whatever `to_list()` gives; anything but a `date` is never
        // found by a lookup on a trading day.
        return Ok(levels);
    }
    let days: Vec<Option<i32>> = dates.date()?.physical().iter().collect();
    for (row, day) in days.iter().enumerate() {
        let Some(text) = py_str(&cell(frame, "level", row)?) else {
            continue;
        };
        let Some(day) = day.and_then(days_to_date) else {
            continue;
        };
        levels.insert(day, text.parse::<WideDecimal>()?);
    }
    Ok(levels)
}

/// `_benchmark_growth(levels, previous, day)`: `after / before`, or 1 when either is missing
/// or `before <= 0`.
fn benchmark_growth(
    levels: &HashMap<NaiveDate, WideDecimal>,
    previous: NaiveDate,
    day: NaiveDate,
) -> Result<WideDecimal, BacktestError> {
    match (levels.get(&previous), levels.get(&day)) {
        (Some(before), Some(after)) if *before > WideDecimal::zero() => after.div(before),
        _ => Ok(WideDecimal::from(1_i64)),
    }
}

/// The price a held name is carried at: `primary` when it printed, else its last close.
fn carried_price(
    panel: &PricePanel,
    instrument_id: i64,
    position_index: usize,
    primary: Option<Decimal>,
) -> Result<Option<Decimal>, BacktestError> {
    if primary.is_some() {
        return Ok(primary);
    }
    Ok(panel
        .last_close_at_or_before(instrument_id, position_index)?
        .map(|(price, _)| price))
}

/// `_market_value`: the book at today's close; a quiet name at its last close.
fn market_value(
    positions: &Book,
    panel: &PricePanel,
    position_index: usize,
) -> Result<WideDecimal, BacktestError> {
    let mut total = WideDecimal::zero();
    for (id, position) in positions {
        if position.quantity <= 0 {
            continue;
        }
        let close = panel.close_on(*id, position_index)?;
        let Some(price) = carried_price(panel, *id, position_index, close)? else {
            continue;
        };
        total = total.add(&wide(price).mul(&WideDecimal::from(position.quantity)));
    }
    money(&total)
}

/// `_open_value`: the book at today's open; a name with no opening print at its last close.
fn open_value(
    positions: &Book,
    panel: &PricePanel,
    position_index: usize,
) -> Result<WideDecimal, BacktestError> {
    let mut total = WideDecimal::zero();
    for (id, position) in positions {
        if position.quantity <= 0 {
            continue;
        }
        let open = panel.open_on(*id, position_index)?;
        let Some(price) = carried_price(panel, *id, position_index, open)? else {
            continue;
        };
        total = total.add(&wide(price).mul(&WideDecimal::from(position.quantity)));
    }
    money(&total)
}

/// `_selected_rows(frame)`: the screen's rows with what weighting needs.
fn selected_rows(frame: &DataFrame) -> Result<Vec<Selected>, BacktestError> {
    let missing: Vec<&str> = ["instrument_id", "rank"]
        .into_iter()
        .filter(|c| !has_column(frame, c))
        .collect();
    if !missing.is_empty() {
        return Err(BacktestError::Data(format!(
            "a screen result is missing column(s) {}",
            py_list_repr(&missing)
        )));
    }
    let has_symbol = has_column(frame, "symbol");
    let has_name = has_column(frame, "name");
    let has_cap = has_column(frame, "marketcap_cr");
    let has_vol = has_column(frame, "vol_12m");
    let mut rows = Vec::with_capacity(frame.height());
    for row in 0..frame.height() {
        let instrument_id = py_int(&cell(frame, "instrument_id", row)?, "instrument_id")?;
        let as_text = |column: &str| -> Result<String, BacktestError> {
            Ok(py_str(&cell(frame, column, row)?).unwrap_or_else(|| "None".to_owned()))
        };
        let symbol = if has_symbol {
            as_text("symbol")?
        } else {
            format!("#{instrument_id}")
        };
        let name = if has_name {
            as_text("name")?
        } else {
            symbol.clone()
        };
        let rank = py_int(&cell(frame, "rank", row)?, "rank")?;
        let marketcap_cr = if has_cap {
            optional_decimal(&cell(frame, "marketcap_cr", row)?)?
        } else {
            None
        };
        let volatility = if has_vol {
            optional_decimal(&cell(frame, "vol_12m", row)?)?
        } else {
            None
        };
        rows.push(Selected {
            instrument_id,
            symbol,
            name,
            rank,
            marketcap_cr,
            volatility,
        });
    }
    Ok(rows)
}

/// `_risk_on`: is the benchmark at or above its 200-day moving average? Fewer than 200
/// observations is noted and stays invested.
fn risk_on(
    reader: &PointInTimeReader<'_>,
    day: NaiveDate,
    levels: &HashMap<NaiveDate, WideDecimal>,
    notes: &mut Vec<String>,
) -> Result<bool, BacktestError> {
    let window = reader.benchmark_to(day)?;
    let mut series: Vec<String> = Vec::new();
    for row in 0..window.height() {
        if let Some(text) = py_str(&cell(&window, "level", row)?) {
            series.push(text);
        }
    }
    if series.len() < RISK_OVERLAY_WINDOW {
        notes.push(format!(
            "the risk overlay had only {} benchmark observations on {day}, fewer than the 200 \
             its moving average needs; it stayed invested.",
            series.len()
        ));
        return Ok(true);
    }
    let tail: Vec<WideDecimal> = series[series.len() - RISK_OVERLAY_WINDOW..]
        .iter()
        .map(|t| t.parse::<WideDecimal>())
        .collect::<Result<_, _>>()?;
    let average = WideDecimal::sum(tail.iter()).div(&WideDecimal::from(RISK_OVERLAY_WINDOW))?;
    let current = match levels.get(&day) {
        Some(level) => level.clone(),
        None => tail.last().cloned().unwrap_or_default(),
    };
    Ok(current >= average)
}

/// `_reasons(targets, positions)`: `enter`, `exit` or `rebalance` per name.
fn reasons(targets: &BTreeMap<i64, Decimal>, positions: &Book) -> BTreeMap<i64, TradeReason> {
    let ids: BTreeSet<i64> = targets.keys().chain(positions.keys()).copied().collect();
    let mut out = BTreeMap::new();
    for id in ids {
        let held = positions.get(&id).map_or(0, |p| p.quantity);
        if !targets.contains_key(&id) {
            if held > 0 {
                out.insert(id, TradeReason::Exit);
            }
            continue;
        }
        out.insert(
            id,
            if held == 0 {
                TradeReason::Enter
            } else {
                TradeReason::Rebalance
            },
        );
    }
    out
}

/// `_decide`: steps 1-3 — the screen as of `day`, the hold buffer, the target weights. The
/// third value is how many rows the screen returned.
fn decide(
    reader: &PointInTimeReader<'_>,
    day: NaiveDate,
    positions: &Book,
    config: &BacktestConfig,
    levels: &HashMap<NaiveDate, WideDecimal>,
) -> Result<(Decision, Vec<String>, usize), BacktestError> {
    let mut notes = Vec::new();
    let rows = selected_rows(&reader.screen_on(day)?)?;

    let mut targets: BTreeMap<i64, Decimal> = BTreeMap::new();
    let mut ranks: BTreeMap<i64, i64> = BTreeMap::new();
    if config.risk_overlay.enabled && !risk_on(reader, day, levels, &mut notes)? {
        notes.push(format!(
            "the risk overlay was triggered on {day}: the benchmark closed below its 200-day \
             moving average, so the book went to cash."
        ));
    } else {
        let held: BTreeSet<i64> = positions
            .iter()
            .filter(|(_, p)| p.quantity > 0)
            .map(|(id, _)| *id)
            .collect();
        let mut by_id: BTreeMap<i64, &Selected> = BTreeMap::new();
        for row in &rows {
            by_id.insert(row.instrument_id, row);
        }
        let keep = rank_buffer::kept(
            &by_id,
            &held,
            config.selection.top_n,
            config.selection.hold_buffer,
        );
        let selected: Vec<Selected> = keep
            .iter()
            .filter_map(|id| by_id.get(id).map(|row| (*row).clone()))
            .collect();
        if selected.is_empty() {
            notes.push(format!(
                "the screen returned nothing on {day}; the book went to cash."
            ));
        }
        targets = apply_position_limits(
            &raw_weights(config.weighting, &selected)?,
            &config.position_limits,
        )?;
        ranks = selected.iter().map(|r| (r.instrument_id, r.rank)).collect();
    }

    let reasons = reasons(&targets, positions);
    Ok((
        Decision {
            decided_on: day,
            targets,
            ranks,
            reasons,
        },
        notes,
        rows.len(),
    ))
}

/// `_target_quantity`: `equity * weight / price`, rounded down to whole shares.
fn target_quantity(
    weight: Option<Decimal>,
    equity: &WideDecimal,
    price: Decimal,
) -> Result<i64, BacktestError> {
    match weight {
        Some(w) if w > Decimal::ZERO => int_down(&equity.mul(&wide(w)).div(&wide(price))?),
        _ => Ok(0),
    }
}

/// `_affordable`: trim a buy to what the cash on hand pays for, cost included.
fn affordable(
    delta: i64,
    price: Decimal,
    cash: &WideDecimal,
    config: &BacktestConfig,
) -> Result<i64, BacktestError> {
    let rate = config.costs.total_bps().div(&basis_points())?;
    let unit = wide(price).mul(&WideDecimal::from(1_i64).add(&rate));
    if unit <= WideDecimal::zero() {
        return Ok(0);
    }
    let ceiling = int_down(&cash.div(&unit)?)?;
    Ok(delta.min(ceiling).max(0))
}

/// `_fill`: one fill — cash, the book and the trade record.
#[allow(clippy::too_many_arguments)]
fn fill(
    instrument_id: i64,
    day: NaiveDate,
    quantity: i64,
    price: Decimal,
    side: TradeSide,
    reason: TradeReason,
    positions: &mut Book,
    cash: &WideDecimal,
    config: &BacktestConfig,
    data: &BacktestData,
) -> Result<(WideDecimal, Trade), BacktestError> {
    let notional = money(&wide(price).mul(&WideDecimal::from(quantity)))?;
    let cost = wide(config.costs.charge(out(&notional)?)?);
    let position = positions.entry(instrument_id).or_insert(Position {
        quantity: 0,
        book_cost: WideDecimal::zero(),
    });
    let mut realised = None;
    let new_cash;
    match side {
        TradeSide::Buy => {
            new_cash = money(&cash.sub(&notional).sub(&cost))?;
            position.quantity += quantity;
            position.book_cost = position.book_cost.add(&notional.add(&cost));
        }
        TradeSide::Sell => {
            let basis = if position.quantity > 0 {
                money(
                    &position
                        .book_cost
                        .mul(&WideDecimal::from(quantity))
                        .div(&WideDecimal::from(position.quantity))?,
                )?
            } else {
                WideDecimal::zero()
            };
            new_cash = money(&cash.add(&notional).sub(&cost))?;
            position.quantity -= quantity;
            position.book_cost = position.book_cost.sub(&basis);
            realised = Some(out(&money(&notional.sub(&cost).sub(&basis))?)?);
            if position.quantity <= 0 {
                positions.remove(&instrument_id);
            }
        }
    }
    Ok((
        new_cash,
        Trade {
            date: day,
            instrument_id,
            symbol: data.symbol_for(instrument_id),
            side,
            quantity,
            price,
            notional: out(&notional)?,
            cost: out(&cost)?,
            reason,
            realised_pnl: realised,
        },
    ))
}

/// `_execute`: steps 4-5 — fill at today's open, whole shares, sells before buys, sized on the
/// book valued at today's open. Returns the cash, the fills, and every name that had no
/// opening print (so could not be filled).
#[allow(clippy::too_many_arguments)]
fn execute(
    decision: &Decision,
    day: NaiveDate,
    position_index: usize,
    panel: &PricePanel,
    positions: &mut Book,
    cash: WideDecimal,
    config: &BacktestConfig,
    data: &BacktestData,
) -> Result<(WideDecimal, Vec<Trade>, Vec<i64>), BacktestError> {
    let mut cash = cash;
    let mut fills = Vec::new();
    let equity = cash.add(&open_value(positions, panel, position_index)?);
    let instruments = decision.instruments();
    let mut wanted: HashMap<i64, i64> = HashMap::new();
    let mut unfillable = Vec::new();
    for &id in &instruments {
        match panel.open_on(id, position_index)? {
            Some(price) if price > Decimal::ZERO => {
                let target = target_quantity(decision.targets.get(&id).copied(), &equity, price)?;
                wanted.insert(id, target);
            }
            _ => unfillable.push(id),
        }
    }

    for side in [TradeSide::Sell, TradeSide::Buy] {
        for &id in &instruments {
            let Some(&target) = wanted.get(&id) else {
                continue;
            };
            let Some(price) = panel.open_on(id, position_index)? else {
                continue;
            };
            if price <= Decimal::ZERO {
                continue;
            }
            let held = positions.get(&id).map_or(0, |p| p.quantity);
            let delta = target - held;
            if delta == 0 || (delta < 0) != (side == TradeSide::Sell) {
                continue;
            }
            let quantity = match side {
                TradeSide::Sell => -delta,
                TradeSide::Buy => affordable(delta, price, &cash, config)?,
            };
            if quantity <= 0 {
                continue;
            }
            let reason = decision
                .reasons
                .get(&id)
                .copied()
                .unwrap_or(TradeReason::Rebalance);
            let (new_cash, trade) = fill(
                id, day, quantity, price, side, reason, positions, &cash, config, data,
            )?;
            cash = new_cash;
            fills.push(trade);
        }
    }
    Ok((cash, fills, unfillable))
}

/// `_liquidate`: docs/10 §8 — sell at the last available close, log the event. `None` when
/// the name never printed a close (the caller writes the position off).
fn liquidate(
    reader: &PointInTimeReader<'_>,
    instrument_id: i64,
    day: NaiveDate,
    positions: &mut Book,
    cash: &WideDecimal,
    config: &BacktestConfig,
    data: &BacktestData,
) -> Result<Option<(DelistingEvent, WideDecimal, Trade)>, BacktestError> {
    let Some((price, priced_on)) = reader.last_close(instrument_id, day)? else {
        return Ok(None);
    };
    let quantity = positions.get(&instrument_id).map_or(0, |p| p.quantity);
    let (cash, trade) = fill(
        instrument_id,
        day,
        quantity,
        price,
        TradeSide::Sell,
        TradeReason::Delist,
        positions,
        cash,
        config,
        data,
    )?;
    let proceeds = out(&wide(trade.notional).sub(&wide(trade.cost)))?;
    let event = DelistingEvent {
        date: day,
        instrument_id,
        symbol: data.symbol_for(instrument_id),
        quantity,
        price,
        priced_on,
        proceeds,
    };
    Ok(Some((event, cash, trade)))
}

/// `_credit_dividends`: per-share amounts on `day` times shares held, to the paisa; `0` (not
/// `0.00`) when the schedule has no row that day.
fn credit_dividends(
    reader: &PointInTimeReader<'_>,
    day: NaiveDate,
    positions: &Book,
) -> Result<WideDecimal, BacktestError> {
    let frame = reader.dividends_on(day)?;
    if frame.height() == 0 {
        return Ok(WideDecimal::zero());
    }
    let mut total = WideDecimal::zero();
    for row in 0..frame.height() {
        let id = py_int(&cell(&frame, "instrument_id", row)?, "instrument_id")?;
        let Some(position) = positions.get(&id) else {
            continue;
        };
        if position.quantity <= 0 {
            continue;
        }
        let Some(amount) = optional_decimal(&cell(&frame, "amount", row)?)? else {
            continue;
        };
        total = total.add(&amount.mul(&WideDecimal::from(position.quantity)));
    }
    money(&total)
}

/// `_snapshot`: per-rebalance holdings as filled — target weight beside actual weight.
fn snapshot(
    decision: &Decision,
    executed_on: NaiveDate,
    positions: &Book,
    panel: &PricePanel,
    position_index: usize,
    data: &BacktestData,
    cash: &WideDecimal,
) -> Result<Vec<HoldingSnapshot>, BacktestError> {
    let mut values: BTreeMap<i64, WideDecimal> = BTreeMap::new();
    for (id, position) in positions {
        if position.quantity <= 0 {
            continue;
        }
        let open = panel.open_on(*id, position_index)?;
        let Some(price) = carried_price(panel, *id, position_index, open)? else {
            continue;
        };
        values.insert(
            *id,
            money(&wide(price).mul(&WideDecimal::from(position.quantity)))?,
        );
    }
    let equity = WideDecimal::sum(values.values()).add(cash);
    let mut ids: Vec<i64> = values.keys().copied().collect();
    ids.sort_by_key(|id| (decision.rank_of(*id), *id));
    let mut rows = Vec::with_capacity(ids.len());
    for id in ids {
        let (Some(value), Some(position)) = (values.get(&id), positions.get(&id)) else {
            continue;
        };
        let quantity = WideDecimal::from(position.quantity);
        let price = value
            .div(&quantity)?
            .quantize(PRICE_EXPONENT, Rounding::HalfEven)?;
        let actual_weight = if equity > WideDecimal::zero() {
            out(&value
                .div(&equity)?
                .quantize(WEIGHT_EXPONENT, Rounding::HalfEven)?)?
        } else {
            Decimal::ZERO
        };
        rows.push(HoldingSnapshot {
            rebalance_date: decision.decided_on,
            executed_on,
            instrument_id: id,
            symbol: data.symbol_for(id),
            name: data.name_for(id),
            rank: decision.ranks.get(&id).copied(),
            target_weight: decision.targets.get(&id).copied().unwrap_or(Decimal::ZERO),
            quantity: position.quantity,
            price: out(&price)?,
            value: out(value)?,
            actual_weight,
        });
    }
    Ok(rows)
}

// =============================================================================================
// Fragility (deliverable 7)
// =============================================================================================

/// `run_fragility(config, data, base=..., progress=...)`: the configuration as asked, then
/// costs -25 % and +25 %, then every rebalance shifted -1 and +1 trading day. A shifted date
/// with no precomputed screen raises rather than reusing a neighbour.
///
/// # Errors
/// Any error a constituent [`run_backtest`] raises, or [`BacktestError::Validation`] for a
/// scaled cost stack out of bounds.
pub fn run_fragility(
    config: &BacktestConfig,
    data: &BacktestData,
    base: Option<BacktestResult>,
    progress: Option<ProgressSink<'_>>,
) -> Result<FragilityReport, BacktestError> {
    let baseline = match base {
        Some(b) => b,
        None => run_backtest(config, data, progress, None)?,
    };
    let mut variants = Vec::with_capacity(4);
    for (label, factor, percent) in [
        ("costs_minus_25pct", Decimal::new(75, 2), "-25"),
        ("costs_plus_25pct", Decimal::new(125, 2), "+25"),
    ] {
        let scaled = config.with_costs(config.costs.scaled(factor)?);
        variants.push(FragilityRun {
            label: label.to_owned(),
            description: format!("every cost component {percent}%"),
            result: run_backtest(&scaled, data, None, None)?,
        });
    }
    for (label, offset) in [("rebalance_minus_1d", -1_i64), ("rebalance_plus_1d", 1)] {
        let schedule = shift_schedule(&data.calendar, &baseline.rebalance_dates, offset);
        variants.push(FragilityRun {
            label: label.to_owned(),
            description: format!("every rebalance {offset:+} trading day"),
            result: run_backtest(config, data, None, Some(&schedule))?,
        });
    }
    Ok(FragilityReport {
        base: FragilityRun {
            label: "base".to_owned(),
            description: "the configuration as submitted".to_owned(),
            result: baseline,
        },
        variants,
    })
}
