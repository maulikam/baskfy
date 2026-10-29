//! Port of `baskfy_core.backtest_metrics`.
//!
//! Metrics over a backtest's curve and trades (docs/10 §Outputs). Goldens:
//! `go/testdata/golden/L1/backtest/{compute_metrics,downsample_curve,drawdown_series,
//! metrics_hash,monthly_returns,rolling_distribution,round_trips}.case_NNN.json`.
//!
//! Money stays [`Decimal`]; statistics are `f64`, exactly where the Python uses `Decimal` and
//! `float`. The float reductions reproduce numpy's own order of operations (pairwise summation
//! for `sum`/`mean`/`std`/`var`, `percentile`'s linear `_lerp`) and Python 3.12's compensated
//! `sum()`, so the published numbers — and the SHA-256 metrics hash taken over their
//! ten-decimal rounding — agree with the Python bit for bit wherever that is reachable.
//!
//! The engine's own `BacktestResult`, `BacktestConfig` and `Trade` live in
//! [`crate::backtest`]; this module takes the subset it reads as [`BacktestOutcome`],
//! [`MetricsConfig`] and [`Trade`] (DECISIONS-RS R2.3), so it does not depend on the engine port.

use std::collections::{BTreeMap, HashMap};
use std::fmt::Write as _;

use chrono::{Datelike, NaiveDate};
use rust_decimal::{Decimal, RoundingStrategy};
use serde::de::{Deserializer, IgnoredAny, SeqAccess, Visitor};
use serde::ser::{SerializeMap, SerializeSeq, Serializer};
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use numeric::{
    dec_to_f64, mean, neumaier_sum, percentile_linear, py_float_repr, py_round, std_ddof1,
    var_ddof1,
};

/// Statistics are rounded to this many decimal places before they are published or hashed.
pub const METRIC_ROUNDING: i32 = 10;

/// Roughly two points per trading week over fifteen years (docs/10's downsampled curve).
pub const EQUITY_CURVE_POINTS: usize = 1500;

/// A return series needs two observations before it is a series at all.
const MIN_OBSERVATIONS: usize = 2;
const PERCENTILES: [u32; 5] = [5, 25, 50, 75, 95];
/// Below this, an annualised standard deviation is rounding noise rather than a measurement.
const MIN_ANNUALISED_STD: f64 = 1e-6;

fn round_metric(value: Option<f64>) -> Option<f64> {
    value
        .filter(|v| v.is_finite())
        .map(|v| py_round(v, METRIC_ROUNDING))
}

fn arith(value: Option<Decimal>, what: &str) -> CoreResult<Decimal> {
    value.ok_or_else(|| CoreError::Arithmetic(what.to_owned()))
}

fn div(a: Decimal, b: Decimal, what: &str) -> CoreResult<Decimal> {
    arith(a.checked_div(b), what)
}

fn sub(a: Decimal, b: Decimal, what: &str) -> CoreResult<Decimal> {
    arith(a.checked_sub(b), what)
}

fn add(a: Decimal, b: Decimal, what: &str) -> CoreResult<Decimal> {
    arith(a.checked_add(b), what)
}

fn usize_f64(n: usize) -> f64 {
    // Counts here are far below 2^53, where the conversion is exact.
    u32::try_from(n).map_or(f64::MAX, f64::from)
}

fn check_lengths(a: usize, b: usize, what: &str) -> CoreResult<()> {
    if a == b {
        Ok(())
    } else {
        Err(CoreError::InvalidInput(format!(
            "{what}: zip(strict=True) over lengths {a} and {b}"
        )))
    }
}

/// `baskfy_core.backtest.year_fraction`: calendar years between two dates (days / 365.25).
fn year_fraction(start: NaiveDate, end: NaiveDate) -> CoreResult<Decimal> {
    let days = end.signed_duration_since(start).num_days();
    div(Decimal::from(days), Decimal::new(36525, 2), "year fraction")
}

// ---------------------------------------------------------------------------
// Inputs: the parts of the engine's result this module reads
// ---------------------------------------------------------------------------

/// `TradeSide`: a fill's direction.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TradeSide {
    /// A purchase.
    Buy,
    /// A sale.
    Sell,
}

/// `TradeReason`: docs/10 §Artefacts, `enter|exit|rebalance|delist`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TradeReason {
    /// A new position.
    Enter,
    /// A position closed by the screen.
    Exit,
    /// A resize of a held position.
    Rebalance,
    /// A position closed by a delisting.
    Delist,
}

/// One fill (`baskfy_core.backtest.Trade`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Trade {
    /// Fill day.
    pub date: NaiveDate,
    /// Instrument.
    pub instrument_id: i64,
    /// Ticker.
    pub symbol: String,
    /// Direction.
    pub side: TradeSide,
    /// Shares.
    pub quantity: i64,
    /// Fill price.
    pub price: Decimal,
    /// Price times quantity, rupees.
    pub notional: Decimal,
    /// Costs charged on this fill.
    pub cost: Decimal,
    /// Why the fill happened.
    pub reason: TradeReason,
    /// Realised profit on the shares a sell closed, net of its cost; `None` on a buy.
    #[serde(default)]
    pub realised_pnl: Option<Decimal>,
}

/// The fields of `BacktestConfig` the metrics read.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MetricsConfig {
    /// Opening capital.
    pub initial_capital: Decimal,
    /// Flat annual risk-free rate (fallback before the curve starts).
    #[serde(default)]
    pub risk_free_rate: Decimal,
    /// `(observed, annual rate)` points, forward-filled onto return days.
    #[serde(default)]
    pub risk_free_curve: Vec<(NaiveDate, Decimal)>,
}

/// The fields of `BacktestResult` [`compute_metrics`] reads.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct BacktestOutcome {
    /// The run's configuration (the parts the metrics need).
    pub config: MetricsConfig,
    /// Trading days of the run.
    pub dates: Vec<NaiveDate>,
    /// End-of-day equity, one per date.
    pub equity: Vec<Decimal>,
    /// End-of-day invested value, one per date.
    pub invested: Vec<Decimal>,
    /// Benchmark level per date, `None` where unobserved.
    pub benchmark: Vec<Option<Decimal>>,
    /// The fill log.
    pub trades: Vec<Trade>,
    /// How many delistings the run met.
    #[serde(rename = "delistings", deserialize_with = "count_seq")]
    pub delisting_count: usize,
    /// Total costs paid.
    pub total_costs: Decimal,
    /// Dividends credited.
    pub dividends_credited: Decimal,
}

impl BacktestOutcome {
    /// `BacktestResult.final_equity`: the last equity, or the opening capital.
    pub fn final_equity(&self) -> Decimal {
        self.equity
            .last()
            .copied()
            .unwrap_or(self.config.initial_capital)
    }
}

fn count_seq<'de, D: Deserializer<'de>>(deserializer: D) -> Result<usize, D::Error> {
    struct Count;
    impl<'de> Visitor<'de> for Count {
        type Value = usize;
        fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            f.write_str("a sequence")
        }
        fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<usize, A::Error> {
            let mut n = 0usize;
            while seq.next_element::<IgnoredAny>()?.is_some() {
                n = n.saturating_add(1);
            }
            Ok(n)
        }
        fn visit_u64<E: serde::de::Error>(self, v: u64) -> Result<usize, E> {
            usize::try_from(v).map_err(E::custom)
        }
    }
    deserializer.deserialize_any(Count)
}

// ---------------------------------------------------------------------------
// A JSON value, canonically dumpable the way Python's json.dumps does it
// ---------------------------------------------------------------------------

/// A JSON value: the shape of `Metrics.as_dict()` and of `BacktestConfig.model_dump(mode="json")`.
///
/// [`Json::to_canonical`] reproduces `json.dumps(v, sort_keys=True, separators=(",", ":"))`
/// byte for byte (Python float `repr`, `ensure_ascii` escaping), which [`metrics_hash`] hashes.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(untagged)]
pub enum Json {
    /// `null` / `None`.
    Null,
    /// A boolean.
    Bool(bool),
    /// An integer.
    Int(i64),
    /// A float.
    Float(f64),
    /// A string.
    Str(String),
    /// An array.
    Array(Vec<Json>),
    /// An object; keys sorted.
    Object(BTreeMap<String, Json>),
}

impl Json {
    fn opt_float(value: Option<f64>) -> Self {
        value.map_or(Self::Null, Self::Float)
    }

    fn opt_str<T: ToString>(value: Option<T>) -> Self {
        value.map_or(Self::Null, |v| Self::Str(v.to_string()))
    }

    fn int(n: usize) -> Self {
        Self::Int(i64::try_from(n).unwrap_or(i64::MAX))
    }

    /// `json.dumps(self, sort_keys=True, separators=(",", ":"))`.
    pub fn to_canonical(&self) -> String {
        let mut out = String::new();
        self.write_canonical(&mut out);
        out
    }

    fn write_canonical(&self, out: &mut String) {
        match self {
            Self::Null => out.push_str("null"),
            Self::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Self::Int(n) => {
                let _ = write!(out, "{n}");
            }
            Self::Float(x) => {
                if x.is_nan() {
                    out.push_str("NaN");
                } else if x.is_infinite() {
                    out.push_str(if *x > 0.0 { "Infinity" } else { "-Infinity" });
                } else {
                    out.push_str(&py_float_repr(*x));
                }
            }
            Self::Str(s) => write_json_string(s, out),
            Self::Array(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    item.write_canonical(out);
                }
                out.push(']');
            }
            Self::Object(map) => {
                out.push('{');
                for (i, (key, value)) in map.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write_json_string(key, out);
                    out.push(':');
                    value.write_canonical(out);
                }
                out.push('}');
            }
        }
    }
}

/// Python's `json` string encoder with `ensure_ascii=True`.
fn write_json_string(s: &str, out: &mut String) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if c.is_ascii() && !c.is_ascii_control() => out.push(c),
            c => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
        }
    }
    out.push('"');
}

impl Serialize for Json {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            Self::Null => serializer.serialize_unit(),
            Self::Bool(b) => serializer.serialize_bool(*b),
            Self::Int(n) => serializer.serialize_i64(*n),
            Self::Float(x) => serializer.serialize_f64(*x),
            Self::Str(s) => serializer.serialize_str(s),
            Self::Array(items) => {
                let mut seq = serializer.serialize_seq(Some(items.len()))?;
                for item in items {
                    seq.serialize_element(item)?;
                }
                seq.end()
            }
            Self::Object(map) => {
                let mut m = serializer.serialize_map(Some(map.len()))?;
                for (k, v) in map {
                    m.serialize_entry(k, v)?;
                }
                m.end()
            }
        }
    }
}

fn object<const N: usize>(entries: [(&str, Json); N]) -> Json {
    Json::Object(
        entries
            .into_iter()
            .map(|(k, v)| (k.to_owned(), v))
            .collect(),
    )
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

/// One day of docs/10's "drawdown series".
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct DrawdownPoint {
    /// The day.
    pub date: NaiveDate,
    /// Equity that day.
    pub equity: Decimal,
    /// Running peak of equity (starting from zero).
    pub peak: Decimal,
    /// `(equity - peak) / peak`: negative or zero.
    pub drawdown: f64,
}

/// One cell of docs/10's "monthly return heatmap".
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct MonthlyReturn {
    /// Calendar year.
    pub year: i32,
    /// Calendar month, 1-12.
    pub month: u32,
    /// The month's return.
    pub ret: f64,
}

impl MonthlyReturn {
    /// `"YYYY-MM"`.
    pub fn key(&self) -> String {
        format!("{:04}-{:02}", self.year, self.month)
    }
}

/// One position, from the first share bought to the last share sold.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct RoundTrip {
    /// Instrument.
    pub instrument_id: i64,
    /// Ticker.
    pub symbol: String,
    /// Day of the first buy.
    pub opened_on: NaiveDate,
    /// Day of the sell that flattened it.
    pub closed_on: NaiveDate,
    /// Net of every cost charged on the way in and on the way out.
    pub pnl: Decimal,
    /// Notional plus cost of every buy.
    pub invested: Decimal,
    /// True when the position was closed by a delisting.
    pub delisted: bool,
}

impl RoundTrip {
    /// A win is a strictly positive P&L; break-even is a loss.
    pub fn is_win(&self) -> bool {
        self.pnl > Decimal::ZERO
    }
}

/// docs/10: "rolling 12-month return distribution".
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct RollingDistribution {
    /// Number of twelve-month windows.
    pub count: usize,
    /// Smallest window return.
    pub minimum: Option<f64>,
    /// 5th percentile.
    pub p05: Option<f64>,
    /// 25th percentile.
    pub p25: Option<f64>,
    /// Median.
    pub median: Option<f64>,
    /// 75th percentile.
    pub p75: Option<f64>,
    /// 95th percentile.
    pub p95: Option<f64>,
    /// Largest window return.
    pub maximum: Option<f64>,
    /// Share of windows that lost money.
    pub negative_share: Option<f64>,
}

impl RollingDistribution {
    fn empty() -> Self {
        Self {
            count: 0,
            minimum: None,
            p05: None,
            p25: None,
            median: None,
            p75: None,
            p95: None,
            maximum: None,
            negative_share: None,
        }
    }

    /// `RollingDistribution.as_dict()`: rounded, with the published key names.
    pub fn as_dict(&self) -> Json {
        let r = |v: Option<f64>| Json::opt_float(round_metric(v));
        object([
            ("count", Json::int(self.count)),
            ("min", r(self.minimum)),
            ("p05", r(self.p05)),
            ("p25", r(self.p25)),
            ("median", r(self.median)),
            ("p75", r(self.p75)),
            ("p95", r(self.p95)),
            ("max", r(self.maximum)),
            ("negative_share", r(self.negative_share)),
        ])
    }
}

/// docs/10 §Outputs, one field per named metric.
#[derive(Debug, Clone, PartialEq)]
pub struct Metrics {
    /// Compound annual growth rate.
    pub cagr: Option<f64>,
    /// Closing over opening, minus one.
    pub total_return: f64,
    /// Annualised standard deviation of daily returns.
    pub annualised_volatility: Option<f64>,
    /// Sharpe ratio against the risk-free series.
    pub sharpe: Option<f64>,
    /// Sortino ratio.
    pub sortino: Option<f64>,
    /// Worst drawdown, a non-positive fraction.
    pub max_drawdown: f64,
    /// The peak before the worst drawdown.
    pub max_drawdown_peak: Option<NaiveDate>,
    /// The trough of the worst drawdown.
    pub max_drawdown_trough: Option<NaiveDate>,
    /// The first day back at the peak.
    pub max_drawdown_recovered: Option<NaiveDate>,
    /// CAGR over |max drawdown|.
    pub calmar: Option<f64>,
    /// Share of round trips that won.
    pub hit_rate: Option<f64>,
    /// Mean winning P&L, to the paisa.
    pub average_win: Option<Decimal>,
    /// Mean losing P&L, to the paisa.
    pub average_loss: Option<Decimal>,
    /// One-way turnover per year.
    pub annual_turnover: Option<f64>,
    /// Total costs paid.
    pub total_costs: Decimal,
    /// Average invested share of equity.
    pub exposure: Option<f64>,
    /// Best month.
    pub best_month: Option<MonthlyReturn>,
    /// Worst month.
    pub worst_month: Option<MonthlyReturn>,
    /// Rolling twelve-month distribution.
    pub rolling_12m: RollingDistribution,
    /// Annualised OLS intercept against the benchmark.
    pub alpha: Option<f64>,
    /// OLS slope against the benchmark.
    pub beta: Option<f64>,
    /// Annualised spread of the active return, over observed benchmark days.
    pub tracking_error: Option<f64>,
    /// Active return over tracking error.
    pub information_ratio: Option<f64>,
    /// First day.
    pub start: NaiveDate,
    /// Last day.
    pub end: NaiveDate,
    /// Number of days.
    pub trading_days: usize,
    /// Observed periods per year.
    pub periods_per_year: Option<f64>,
    /// Opening capital.
    pub initial_capital: Decimal,
    /// Closing equity.
    pub final_equity: Decimal,
    /// Benchmark's own total return.
    pub benchmark_total_return: Option<f64>,
    /// Benchmark's own CAGR over the span it covers.
    pub benchmark_cagr: Option<f64>,
    /// Number of fills.
    pub trades: usize,
    /// Number of completed round trips.
    pub round_trips: usize,
    /// Number of delistings.
    pub delistings: usize,
    /// Dividends credited.
    pub dividends_credited: Decimal,
}

fn month_json(value: Option<&MonthlyReturn>) -> Json {
    value.map_or(Json::Null, |m| {
        object([
            ("month", Json::Str(m.key())),
            ("return", Json::opt_float(round_metric(Some(m.ret)))),
        ])
    })
}

impl Metrics {
    /// `Metrics.as_dict()`: the `backtest.metrics jsonb` payload and the metrics-hash input.
    /// Rupee amounts are strings; statistics are floats rounded to ten places, or null.
    pub fn as_dict(&self) -> Json {
        let r = |v: Option<f64>| Json::opt_float(round_metric(v));
        object([
            ("cagr", r(self.cagr)),
            ("total_return", r(Some(self.total_return))),
            ("annualised_volatility", r(self.annualised_volatility)),
            ("sharpe", r(self.sharpe)),
            ("sortino", r(self.sortino)),
            ("max_drawdown", r(Some(self.max_drawdown))),
            ("max_drawdown_peak", Json::opt_str(self.max_drawdown_peak)),
            (
                "max_drawdown_trough",
                Json::opt_str(self.max_drawdown_trough),
            ),
            (
                "max_drawdown_recovered",
                Json::opt_str(self.max_drawdown_recovered),
            ),
            ("calmar", r(self.calmar)),
            ("hit_rate", r(self.hit_rate)),
            ("average_win", Json::opt_str(self.average_win)),
            ("average_loss", Json::opt_str(self.average_loss)),
            ("annual_turnover", r(self.annual_turnover)),
            ("total_costs", Json::Str(self.total_costs.to_string())),
            ("exposure", r(self.exposure)),
            ("best_month", month_json(self.best_month.as_ref())),
            ("worst_month", month_json(self.worst_month.as_ref())),
            ("rolling_12m", self.rolling_12m.as_dict()),
            ("alpha", r(self.alpha)),
            ("beta", r(self.beta)),
            ("tracking_error", r(self.tracking_error)),
            ("information_ratio", r(self.information_ratio)),
            ("start", Json::Str(self.start.to_string())),
            ("end", Json::Str(self.end.to_string())),
            ("trading_days", Json::int(self.trading_days)),
            ("periods_per_year", r(self.periods_per_year)),
            (
                "initial_capital",
                Json::Str(self.initial_capital.to_string()),
            ),
            ("final_equity", Json::Str(self.final_equity.to_string())),
            ("benchmark_total_return", r(self.benchmark_total_return)),
            ("benchmark_cagr", r(self.benchmark_cagr)),
            ("trades", Json::int(self.trades)),
            ("round_trips", Json::int(self.round_trips)),
            ("delistings", Json::int(self.delistings)),
            (
                "dividends_credited",
                Json::Str(self.dividends_credited.to_string()),
            ),
        ])
    }
}

/// One point of the downsampled curve: `{"date", "equity", "benchmark"}`.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CurvePoint {
    /// The day.
    pub date: NaiveDate,
    /// Equity that day.
    pub equity: Decimal,
    /// Benchmark level that day, if known.
    pub benchmark: Option<Decimal>,
}

// ---------------------------------------------------------------------------
// Artefacts
// ---------------------------------------------------------------------------

/// docs/10 §Artefacts: "drawdown series". Drawdown is a fraction, negative or zero; a
/// non-positive running peak reads `0.0`.
pub fn drawdown_series(dates: &[NaiveDate], equity: &[Decimal]) -> CoreResult<Vec<DrawdownPoint>> {
    check_lengths(dates.len(), equity.len(), "drawdown_series")?;
    let mut points = Vec::with_capacity(dates.len());
    let mut peak = Decimal::ZERO;
    for (day, value) in dates.iter().zip(equity) {
        // Python's `max(peak, value)` keeps the first argument on a tie.
        if *value > peak {
            peak = *value;
        }
        let fraction = if peak <= Decimal::ZERO {
            0.0
        } else {
            dec_to_f64(div(sub(*value, peak, "drawdown")?, peak, "drawdown")?)?
        };
        points.push(DrawdownPoint {
            date: *day,
            equity: *value,
            peak,
            drawdown: fraction,
        });
    }
    Ok(points)
}

/// docs/10 §Artefacts: "monthly return heatmap". Each month runs from the previous month's
/// last equity to this month's; the first from the first equity.
pub fn monthly_returns(dates: &[NaiveDate], equity: &[Decimal]) -> CoreResult<Vec<MonthlyReturn>> {
    if dates.is_empty() {
        return Ok(Vec::new());
    }
    check_lengths(dates.len(), equity.len(), "monthly_returns")?;
    // Keyed like `month_key` ("YYYY-MM"), whose string order is (year, month) order.
    let mut closes: BTreeMap<(i32, u32), Decimal> = BTreeMap::new();
    for (day, value) in dates.iter().zip(equity) {
        closes.insert((day.year(), day.month()), *value);
    }
    let Some(mut previous) = equity.first().copied() else {
        return Ok(Vec::new());
    };
    let mut rows = Vec::with_capacity(closes.len());
    for ((year, month), value) in closes {
        let ret = if previous <= Decimal::ZERO {
            0.0
        } else {
            dec_to_f64(sub(
                div(value, previous, "monthly return")?,
                Decimal::ONE,
                "monthly return",
            )?)?
        };
        rows.push(MonthlyReturn { year, month, ret });
        previous = value;
    }
    Ok(rows)
}

/// docs/10: the downsampled equity curve. Stride sampling with the last point always kept.
/// An empty `benchmark` means every point's benchmark is `None`.
pub fn downsample_curve(
    dates: &[NaiveDate],
    equity: &[Decimal],
    benchmark: &[Option<Decimal>],
    limit: usize,
) -> CoreResult<Vec<CurvePoint>> {
    let total = dates.len();
    if total == 0 {
        return Ok(Vec::new());
    }
    if limit == 0 {
        return Err(CoreError::InvalidInput(
            "downsample_curve: limit must be positive".to_owned(),
        ));
    }
    let stride = total.div_ceil(limit).max(1);
    let mut keep: Vec<usize> = (0..total).step_by(stride).collect();
    let last = total - 1;
    if keep.last() != Some(&last) {
        keep.push(last);
    }
    keep.into_iter()
        .map(|index| {
            let missing = || CoreError::InvalidInput(format!("downsample_curve: index {index}"));
            let level = if benchmark.is_empty() {
                None
            } else {
                *benchmark.get(index).ok_or_else(missing)?
            };
            Ok(CurvePoint {
                date: *dates.get(index).ok_or_else(missing)?,
                equity: *equity.get(index).ok_or_else(missing)?,
                benchmark: level,
            })
        })
        .collect()
}

struct OpenPosition {
    opened_on: NaiveDate,
    quantity: i64,
    pnl: Decimal,
    invested: Decimal,
    symbol: String,
    delisted: bool,
}

/// Reconstruct closed positions from the fill log: a round trip ends when the instrument's
/// running quantity returns to exactly zero.
pub fn round_trips(trades: &[Trade]) -> CoreResult<Vec<RoundTrip>> {
    let mut open: HashMap<i64, OpenPosition> = HashMap::new();
    let mut finished = Vec::new();
    for trade in trades {
        let state = open
            .entry(trade.instrument_id)
            .or_insert_with(|| OpenPosition {
                opened_on: trade.date,
                quantity: 0,
                pnl: Decimal::ZERO,
                invested: Decimal::ZERO,
                symbol: trade.symbol.clone(),
                delisted: false,
            });
        match trade.side {
            TradeSide::Buy => {
                state.quantity = state.quantity.saturating_add(trade.quantity);
                state.invested = add(
                    add(state.invested, trade.notional, "invested")?,
                    trade.cost,
                    "invested",
                )?;
            }
            TradeSide::Sell => {
                state.quantity = state.quantity.saturating_sub(trade.quantity);
                // `trade.realised_pnl or Decimal(0)`: a zero Decimal is falsy in Python, so a
                // `0.00` P&L adds the scale-0 `Decimal(0)` and keeps the running total's scale.
                let realised = trade
                    .realised_pnl
                    .filter(|p| !p.is_zero())
                    .unwrap_or(Decimal::ZERO);
                state.pnl = add(state.pnl, realised, "round-trip pnl")?;
                if trade.reason == TradeReason::Delist {
                    state.delisted = true;
                }
            }
        }
        if state.quantity == 0
            && let Some(done) = open.remove(&trade.instrument_id)
        {
            finished.push(RoundTrip {
                instrument_id: trade.instrument_id,
                symbol: done.symbol,
                opened_on: done.opened_on,
                closed_on: trade.date,
                pnl: done.pnl,
                invested: done.invested,
                delisted: done.delisted,
            });
        }
    }
    Ok(finished)
}

/// docs/10: "rolling 12-month return distribution", over calendar-year windows (29 February
/// maps to 28 February of the next year).
pub fn rolling_distribution(
    dates: &[NaiveDate],
    equity: &[Decimal],
) -> CoreResult<RollingDistribution> {
    if dates.len() < MIN_OBSERVATIONS {
        return Ok(RollingDistribution::empty());
    }
    let mut samples: Vec<f64> = Vec::new();
    for (index, day) in dates.iter().enumerate() {
        let next_year = day.year().saturating_add(1);
        let target = day
            .with_year(next_year)
            .or_else(|| NaiveDate::from_ymd_opt(next_year, day.month(), 28))
            .ok_or_else(|| CoreError::InvalidInput(format!("no anniversary for {day}")))?;
        let position = bisect_left(dates, target, index.saturating_add(1));
        if position >= dates.len() {
            break;
        }
        let missing = || CoreError::InvalidInput(format!("rolling_distribution: index {index}"));
        let opening = *equity.get(index).ok_or_else(missing)?;
        if opening <= Decimal::ZERO {
            continue;
        }
        let closing = *equity.get(position).ok_or_else(missing)?;
        samples.push(dec_to_f64(sub(
            div(closing, opening, "rolling return")?,
            Decimal::ONE,
            "rolling return",
        )?)?);
    }
    if samples.is_empty() {
        return Ok(RollingDistribution::empty());
    }
    let mut sorted = samples.clone();
    sorted.sort_by(f64::total_cmp);
    let pct = |q: u32| percentile_linear(&sorted, f64::from(q) / 100.0);
    let negatives = samples.iter().filter(|x| **x < 0.0).count();
    Ok(RollingDistribution {
        count: samples.len(),
        minimum: sorted.first().copied(),
        p05: pct(PERCENTILES[0]),
        p25: pct(PERCENTILES[1]),
        median: pct(PERCENTILES[2]),
        p75: pct(PERCENTILES[3]),
        p95: pct(PERCENTILES[4]),
        maximum: sorted.last().copied(),
        negative_share: Some(usize_f64(negatives) / usize_f64(samples.len())),
    })
}

// ---------------------------------------------------------------------------
// Metrics
// ---------------------------------------------------------------------------

fn returns(values: &[Decimal]) -> CoreResult<Vec<f64>> {
    if values.len() < MIN_OBSERVATIONS {
        return Ok(Vec::new());
    }
    let series = values
        .iter()
        .map(|v| dec_to_f64(*v))
        .collect::<CoreResult<Vec<f64>>>()?;
    Ok(series
        .windows(2)
        .map(|w| if w[0] > 0.0 { w[1] / w[0] - 1.0 } else { 0.0 })
        .collect())
}

/// Daily benchmark returns and the mask of days actually observed (M43).
fn benchmark_returns(levels: &[Option<Decimal>]) -> CoreResult<(Vec<f64>, Vec<bool>)> {
    if levels.len() < MIN_OBSERVATIONS {
        return Ok((Vec::new(), Vec::new()));
    }
    let mut out = vec![0.0; levels.len() - 1];
    let mut observed = vec![false; levels.len() - 1];
    for (index, pair) in levels.windows(2).enumerate() {
        let (Some(before), Some(after)) = (pair[0], pair[1]) else {
            continue;
        };
        if before <= Decimal::ZERO {
            continue;
        }
        out[index] = dec_to_f64(sub(
            div(after, before, "benchmark return")?,
            Decimal::ONE,
            "benchmark return",
        )?)?;
        observed[index] = true;
    }
    Ok((out, observed))
}

/// numpy's `a - b` over two 1-D float arrays: elementwise when the lengths agree, a length-1
/// side broadcast against the other, and numpy's "operands could not be broadcast" error
/// otherwise. A benchmark whose length differs from the run's dates reaches the size-1 arms.
fn broadcast_sub(a: &[f64], b: &[f64]) -> CoreResult<Vec<f64>> {
    match (a, b) {
        _ if a.len() == b.len() => Ok(a.iter().zip(b).map(|(p, q)| p - q).collect()),
        ([p], _) => Ok(b.iter().map(|q| p - q).collect()),
        (_, [q]) => Ok(a.iter().map(|p| p - q).collect()),
        _ => Err(CoreError::InvalidInput(format!(
            "compute_metrics: operands could not be broadcast together with shapes ({},) ({},)",
            a.len(),
            b.len()
        ))),
    }
}

/// `bisect.bisect_left(a, x, lo)`: CPython's loop exactly, so an unsorted input lands where the
/// Python lands too.
fn bisect_left(a: &[NaiveDate], x: NaiveDate, lo: usize) -> usize {
    let mut lo = lo;
    let mut hi = a.len();
    while lo < hi {
        let mid = lo + (hi - lo) / 2;
        if a.get(mid).is_some_and(|d| *d < x) {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    lo
}

/// Python's `float(base ** exponent)` for two floats, raising where Python raises: a negative
/// base to a non-integer power is a `complex` in Python 3, which `float()` refuses
/// (`TypeError`), and a finite pair whose power overflows is an `OverflowError`. `powf` would
/// return NaN or infinity instead, which the metric rounding would quietly publish as `null`.
fn py_pow(base: f64, exponent: f64) -> CoreResult<f64> {
    if base < 0.0 && exponent.is_finite() && exponent.fract() != 0.0 {
        return Err(CoreError::Arithmetic(format!(
            "({base}) ** {exponent} is complex: float() argument must be a real number"
        )));
    }
    if base == 0.0 && exponent < 0.0 {
        return Err(CoreError::Arithmetic(
            "0.0 cannot be raised to a negative power".to_owned(),
        ));
    }
    let value = base.powf(exponent);
    if value.is_infinite() && base.is_finite() && exponent.is_finite() {
        return Err(CoreError::Arithmetic(format!(
            "({base}) ** {exponent}: numerical result out of range"
        )));
    }
    Ok(value)
}

/// Every metric docs/10 §Outputs names, from one pass over the run.
pub fn compute_metrics(result: &BacktestOutcome) -> CoreResult<Metrics> {
    let dates = &result.dates;
    let equity = &result.equity;
    let config = &result.config;
    let (Some(&start), Some(&end)) = (dates.first(), dates.last()) else {
        return Err(CoreError::InvalidInput(
            "compute_metrics: a result with no dates".to_owned(),
        ));
    };
    let years = year_fraction(start, end)?;
    let years_f = dec_to_f64(years)?;
    let portfolio = returns(equity)?;
    let periods = if years > Decimal::ZERO && !portfolio.is_empty() {
        Some(usize_f64(portfolio.len()) / years_f)
    } else {
        None
    };

    let opening = dec_to_f64(config.initial_capital)?;
    let final_equity = result.final_equity();
    let closing = dec_to_f64(final_equity)?;
    let total_return = if opening > 0.0 {
        closing / opening - 1.0
    } else {
        0.0
    };
    let cagr = cagr_of(opening, closing, years)?;

    let volatility = annualised_std(Some(&portfolio), periods);
    let rf_daily = risk_free_daily(dates, config, periods, portfolio.len())?;
    let excess: Vec<f64> = if portfolio.is_empty() {
        portfolio.clone()
    } else {
        check_lengths(portfolio.len(), rf_daily.len(), "excess return")?;
        portfolio
            .iter()
            .zip(&rf_daily)
            .map(|(p, r)| p - r)
            .collect()
    };
    let sharpe = ratio(
        Some(&excess),
        annualised_std(Some(&excess), periods),
        periods,
    );
    let sortino = ratio(Some(&excess), downside_deviation(&excess, periods), periods);

    let drawdowns = drawdown_series(dates, equity)?;
    // Python's `min` keeps the first of equal minima.
    let worst = drawdowns
        .iter()
        .fold(None::<&DrawdownPoint>, |best, p| match best {
            Some(b) if b.drawdown <= p.drawdown => Some(b),
            _ => Some(p),
        });
    let max_drawdown = worst.map_or(0.0, |w| w.drawdown);
    let (peak_date, recovered) = drawdown_dates(&drawdowns, worst);
    let calmar = match cagr {
        Some(c) if max_drawdown < 0.0 => Some(c / max_drawdown.abs()),
        _ => None,
    };

    let months = monthly_returns(dates, equity)?;
    let completed = round_trips(&result.trades)?;
    let wins: Vec<Decimal> = completed
        .iter()
        .filter(|t| t.is_win())
        .map(|t| t.pnl)
        .collect();
    let losses: Vec<Decimal> = completed
        .iter()
        .filter(|t| !t.is_win())
        .map(|t| t.pnl)
        .collect();

    let (bench_returns, bench_observed) = benchmark_returns(&result.benchmark)?;
    let bench_excess = broadcast_sub(&bench_returns, &rf_daily)?;
    let (alpha, beta) = alpha_beta(&excess, &bench_excess, periods)?;
    let active: Option<Vec<f64>> =
        if bench_returns.len() == portfolio.len() && bench_observed.iter().any(|o| *o) {
            Some(
                portfolio
                    .iter()
                    .zip(&bench_returns)
                    .zip(&bench_observed)
                    .filter(|(_, seen)| **seen)
                    .map(|((p, b), _)| p - b)
                    .collect(),
            )
        } else {
            None
        };
    let tracking_error = active
        .as_ref()
        .and_then(|a| annualised_std(Some(a), periods));
    let information_ratio = active
        .as_ref()
        .and_then(|a| ratio(Some(a), tracking_error, periods));

    // Python's `max`/`min` keep the first of equal extremes.
    let best_month = months
        .iter()
        .fold(None::<&MonthlyReturn>, |best, m| match best {
            Some(b) if b.ret >= m.ret => Some(b),
            _ => Some(m),
        })
        .cloned();
    let worst_month = months
        .iter()
        .fold(None::<&MonthlyReturn>, |best, m| match best {
            Some(b) if b.ret <= m.ret => Some(b),
            _ => Some(m),
        })
        .cloned();

    Ok(Metrics {
        cagr,
        total_return,
        annualised_volatility: volatility,
        sharpe,
        sortino,
        max_drawdown,
        max_drawdown_peak: peak_date,
        max_drawdown_trough: worst.filter(|_| max_drawdown < 0.0).map(|w| w.date),
        max_drawdown_recovered: recovered,
        calmar,
        hit_rate: if completed.is_empty() {
            None
        } else {
            Some(usize_f64(wins.len()) / usize_f64(completed.len()))
        },
        average_win: mean_money(&wins)?,
        average_loss: mean_money(&losses)?,
        annual_turnover: turnover(result, years)?,
        total_costs: result.total_costs,
        exposure: exposure(result)?,
        best_month,
        worst_month,
        rolling_12m: rolling_distribution(dates, equity)?,
        alpha,
        beta,
        tracking_error,
        information_ratio,
        start,
        end,
        trading_days: dates.len(),
        periods_per_year: periods,
        initial_capital: config.initial_capital,
        final_equity,
        benchmark_total_return: benchmark_total(&result.benchmark)?,
        benchmark_cagr: benchmark_cagr(dates, &result.benchmark, years)?,
        trades: result.trades.len(),
        round_trips: completed.len(),
        delistings: result.delisting_count,
        dividends_credited: result.dividends_credited,
    })
}

/// The compound annual rate; `-1.0` for a book that went to exactly zero; `None` where
/// undefined (non-positive opening, negative closing, zero-length window).
fn cagr_of(opening: f64, closing: f64, years: Decimal) -> CoreResult<Option<f64>> {
    if opening <= 0.0 || closing < 0.0 || years <= Decimal::ZERO {
        return Ok(None);
    }
    if closing <= 0.0 {
        return Ok(Some(-1.0));
    }
    let y = dec_to_f64(years)?;
    Ok(Some(py_pow(closing / opening, 1.0 / y)? - 1.0))
}

fn daily_rate(annual: Decimal, periods: Option<f64>) -> CoreResult<f64> {
    match periods {
        Some(p) if p > 0.0 && annual > Decimal::ZERO => {
            Ok(py_pow(1.0 + dec_to_f64(annual)?, 1.0 / p)? - 1.0)
        }
        _ => Ok(0.0),
    }
}

/// `baskfy_core.risk_free.annual_rate_on`: last observation on or before `day`.
fn annual_rate_on(day: NaiveDate, curve: &[(NaiveDate, Decimal)], fallback: Decimal) -> Decimal {
    let mut chosen = fallback;
    for (observed, rate) in curve {
        if *observed > day {
            break;
        }
        chosen = *rate;
    }
    chosen
}

fn risk_free_daily(
    dates: &[NaiveDate],
    config: &MetricsConfig,
    periods: Option<f64>,
    n: usize,
) -> CoreResult<Vec<f64>> {
    if n == 0 {
        return Ok(Vec::new());
    }
    if config.risk_free_curve.is_empty() {
        return Ok(vec![daily_rate(config.risk_free_rate, periods)?; n]);
    }
    let return_dates = if dates.len() == n + 1 {
        &dates[1..]
    } else {
        &dates[dates.len().saturating_sub(n)..]
    };
    return_dates
        .iter()
        .map(|day| {
            daily_rate(
                annual_rate_on(*day, &config.risk_free_curve, config.risk_free_rate),
                periods,
            )
        })
        .collect()
}

fn annualised_std(series: Option<&[f64]>, periods: Option<f64>) -> Option<f64> {
    let series = series?;
    let p = periods?;
    if series.len() < MIN_OBSERVATIONS || p <= 0.0 {
        return None;
    }
    Some(std_ddof1(series) * p.sqrt())
}

fn downside_deviation(series: &[f64], periods: Option<f64>) -> Option<f64> {
    let p = periods?;
    if series.len() < MIN_OBSERVATIONS || p <= 0.0 {
        return None;
    }
    let squares: Vec<f64> = series
        .iter()
        .map(|x| {
            let d = x.min(0.0);
            d * d
        })
        .collect();
    let value = mean(&squares).sqrt();
    if value > 0.0 {
        Some(value * p.sqrt())
    } else {
        None
    }
}

fn ratio(excess: Option<&[f64]>, denominator: Option<f64>, periods: Option<f64>) -> Option<f64> {
    let excess = excess?;
    let p = periods?;
    if excess.is_empty() {
        return None;
    }
    let d = denominator?;
    if d.abs() < MIN_ANNUALISED_STD {
        return None;
    }
    Some(mean(excess) * p / d)
}

fn drawdown_dates(
    points: &[DrawdownPoint],
    worst: Option<&DrawdownPoint>,
) -> (Option<NaiveDate>, Option<NaiveDate>) {
    let Some(worst) = worst.filter(|w| w.drawdown < 0.0) else {
        return (None, None);
    };
    let mut peak_date = None;
    for point in points {
        if point.date > worst.date {
            break;
        }
        if point.equity >= worst.peak {
            peak_date = Some(point.date);
        }
    }
    let recovered = points
        .iter()
        .find(|p| p.date > worst.date && p.equity >= worst.peak)
        .map(|p| p.date);
    (peak_date, recovered)
}

fn mean_money(values: &[Decimal]) -> CoreResult<Option<Decimal>> {
    if values.is_empty() {
        return Ok(None);
    }
    let mut total = Decimal::ZERO;
    for v in values {
        total = add(total, *v, "mean money")?;
    }
    let mean = div(total, Decimal::from(values.len()), "mean money")?;
    let mut rounded = mean
        .round_dp_with_strategy(2, RoundingStrategy::MidpointAwayFromZero)
        .rescaled(2);
    // `quantize` keeps the operand's sign, so a mean in (-0.005, 0) becomes `Decimal("-0.00")`
    // and publishes as "-0.00" (a break-even trip beside a one-paisa loss does it). rust_decimal
    // drops the sign of a rounded zero; put it back. A zero mean is `+0` in Python (its sum
    // starts from `Decimal(0)`), so only a strictly negative mean carries the sign.
    if rounded.is_zero() {
        rounded.set_sign_negative(mean < Decimal::ZERO);
    }
    Ok(Some(rounded))
}

trait Rescaled {
    fn rescaled(self, scale: u32) -> Self;
}

impl Rescaled for Decimal {
    /// `quantize` always writes exactly `scale` places (`Decimal("5").quantize(0.01)` is `5.00`).
    fn rescaled(mut self, scale: u32) -> Self {
        self.rescale(scale);
        self
    }
}

/// One-way traded notional over average equity, per year (buys and sells added and halved).
fn turnover(result: &BacktestOutcome, years: Decimal) -> CoreResult<Option<f64>> {
    if years <= Decimal::ZERO || result.equity.is_empty() {
        return Ok(None);
    }
    let mut traded = Decimal::ZERO;
    for trade in &result.trades {
        traded = add(traded, trade.notional, "turnover")?;
    }
    let mut total = Decimal::ZERO;
    for v in &result.equity {
        total = add(total, *v, "turnover")?;
    }
    let average = div(total, Decimal::from(result.equity.len()), "turnover")?;
    if average <= Decimal::ZERO {
        return Ok(None);
    }
    let value = div(
        div(div(traded, Decimal::TWO, "turnover")?, average, "turnover")?,
        years,
        "turnover",
    )?;
    Ok(Some(dec_to_f64(value)?))
}

/// docs/10: "exposure %" — the average share of the book in the market, over days with
/// positive equity.
fn exposure(result: &BacktestOutcome) -> CoreResult<Option<f64>> {
    check_lengths(result.invested.len(), result.equity.len(), "exposure")?;
    let mut pairs = Vec::new();
    for (invested, equity) in result.invested.iter().zip(&result.equity) {
        if *equity > Decimal::ZERO {
            pairs.push(dec_to_f64(div(*invested, *equity, "exposure")?)?);
        }
    }
    if pairs.is_empty() {
        return Ok(None);
    }
    Ok(Some(neumaier_sum(&pairs) / usize_f64(pairs.len())))
}

/// OLS of the portfolio's excess return on the benchmark's; alpha compounded to a year.
fn alpha_beta(
    excess: &[f64],
    bench: &[f64],
    periods: Option<f64>,
) -> CoreResult<(Option<f64>, Option<f64>)> {
    let Some(p) = periods else {
        return Ok((None, None));
    };
    if excess.len() < MIN_OBSERVATIONS || excess.len() != bench.len() {
        return Ok((None, None));
    }
    let variance = var_ddof1(bench);
    if variance <= 0.0 {
        return Ok((None, None));
    }
    let covariance = numeric::cov_ddof1(excess, bench);
    let beta = covariance / variance;
    let intercept = mean(excess) - beta * mean(bench);
    Ok((Some(py_pow(1.0 + intercept, p)? - 1.0), Some(beta)))
}

fn benchmark_total(levels: &[Option<Decimal>]) -> CoreResult<Option<f64>> {
    let known: Vec<Decimal> = levels
        .iter()
        .flatten()
        .filter(|v| **v > Decimal::ZERO)
        .copied()
        .collect();
    let (Some(first), Some(last)) = (known.first(), known.last()) else {
        return Ok(None);
    };
    if known.len() < MIN_OBSERVATIONS {
        return Ok(None);
    }
    Ok(Some(dec_to_f64(sub(
        div(*last, *first, "benchmark total")?,
        Decimal::ONE,
        "benchmark total",
    )?)?))
}

/// The benchmark's own compound rate over the span it actually covers (M43).
fn benchmark_cagr(
    dates: &[NaiveDate],
    levels: &[Option<Decimal>],
    years: Decimal,
) -> CoreResult<Option<f64>> {
    let covered: Vec<(NaiveDate, Decimal)> = dates
        .iter()
        .zip(levels)
        .filter_map(|(day, value)| value.filter(|v| *v > Decimal::ZERO).map(|v| (*day, v)))
        .collect();
    let (Some(first), Some(last)) = (covered.first(), covered.last()) else {
        return Ok(None);
    };
    if covered.len() < MIN_OBSERVATIONS {
        return Ok(None);
    }
    let span = year_fraction(first.0, last.0)?;
    cagr_of(
        dec_to_f64(first.1)?,
        dec_to_f64(last.1)?,
        if span > Decimal::ZERO { span } else { years },
    )
}

// ---------------------------------------------------------------------------
// Determinism (docs/10 §"Correctness harness" test 4)
// ---------------------------------------------------------------------------

/// docs/10: "same config + same `data_version` -> identical metrics hash".
///
/// SHA-256 over canonical JSON (sorted keys, compact separators) of
/// `{"metrics": metrics.as_dict(), "config": config, "data_version": data_version}`.
/// `config` is the run's `BacktestConfig` as JSON (`model_dump(mode="json")`, which is what
/// the Python's `json.loads(config.model_dump_json())` yields).
pub fn metrics_hash(metrics: &Metrics, config: &Json, data_version: Option<i64>) -> String {
    let payload = object([
        ("metrics", metrics.as_dict()),
        ("config", config.clone()),
        ("data_version", data_version.map_or(Json::Null, Json::Int)),
    ]);
    sha256::hex_digest(payload.to_canonical().as_bytes())
}

/// Float kernels that reproduce numpy's and CPython's order of operations.
mod numeric {
    use rust_decimal::Decimal;

    use crate::error::{CoreError, CoreResult};

    /// numpy's `PW_BLOCKSIZE`.
    const PW_BLOCKSIZE: usize = 128;

    /// `float(Decimal)`: correctly rounded, as parsing the decimal's digits is.
    pub(super) fn dec_to_f64(value: Decimal) -> CoreResult<f64> {
        value
            .to_string()
            .parse::<f64>()
            .map_err(|e| CoreError::InvalidInput(format!("float({value}): {e}")))
    }

    /// numpy's `pairwise_sum` for a contiguous float64 reduction (`np.sum`, and through it
    /// `mean`, `var`, `std`): eight accumulators per 128-element block, halves above that.
    pub(super) fn pairwise_sum(a: &[f64]) -> f64 {
        let n = a.len();
        if n < 8 {
            let mut res = 0.0;
            for x in a {
                res += x;
            }
            res
        } else if n <= PW_BLOCKSIZE {
            let mut r = [0.0f64; 8];
            r.copy_from_slice(&a[..8]);
            let mut i = 8;
            while i < n - (n % 8) {
                for (j, acc) in r.iter_mut().enumerate() {
                    *acc += a[i + j];
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
            pairwise_sum(&a[..n2]) + pairwise_sum(&a[n2..])
        }
    }

    fn len_f64(n: usize) -> f64 {
        u32::try_from(n).map_or(f64::MAX, f64::from)
    }

    /// `np.mean`.
    pub(super) fn mean(a: &[f64]) -> f64 {
        pairwise_sum(a) / len_f64(a.len())
    }

    /// `np.var(ddof=1)`.
    pub(super) fn var_ddof1(a: &[f64]) -> f64 {
        let m = mean(a);
        let squares: Vec<f64> = a
            .iter()
            .map(|x| {
                let d = x - m;
                d * d
            })
            .collect();
        pairwise_sum(&squares) / len_f64(a.len().saturating_sub(1))
    }

    /// `np.std(ddof=1)`.
    pub(super) fn std_ddof1(a: &[f64]) -> f64 {
        var_ddof1(a).sqrt()
    }

    /// `np.cov(x, y, ddof=1)[0][1]`: centred cross-products summed, times `1 / (n - 1)`.
    /// numpy sums through BLAS `dot`, whose order is the library's; pairwise is used here.
    pub(super) fn cov_ddof1(x: &[f64], y: &[f64]) -> f64 {
        let mx = mean(x);
        let my = mean(y);
        let products: Vec<f64> = x.iter().zip(y).map(|(a, b)| (a - mx) * (b - my)).collect();
        pairwise_sum(&products) * (1.0 / len_f64(x.len().saturating_sub(1)))
    }

    /// CPython 3.12's `sum()` over floats: Neumaier-compensated.
    pub(super) fn neumaier_sum(a: &[f64]) -> f64 {
        let mut total = 0.0f64;
        let mut c = 0.0f64;
        for x in a {
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

    /// `np.percentile(sorted, q * 100)` with the default `linear` method, including numpy's
    /// `_lerp` (which interpolates from the upper bound when the weight is at least one half).
    pub(super) fn percentile_linear(sorted: &[f64], quantile: f64) -> Option<f64> {
        let n = sorted.len();
        let last = n.checked_sub(1)?;
        let virtual_index = len_f64(last) * quantile;
        let (previous, next, previous_f) = if virtual_index >= len_f64(last) {
            (last, last, -1.0)
        } else if virtual_index < 0.0 {
            (0, 0, 0.0)
        } else {
            let floor = virtual_index.floor();
            // floor of a value in [0, n-1) fits usize exactly.
            let idx = sorted
                .iter()
                .enumerate()
                .map(|(i, _)| i)
                .take_while(|i| len_f64(*i) <= floor)
                .last()
                .unwrap_or(0);
            (idx, idx.saturating_add(1).min(last), floor)
        };
        let gamma = virtual_index - previous_f;
        let a = *sorted.get(previous)?;
        let b = *sorted.get(next)?;
        let diff = b - a;
        let mut value = a + diff * gamma;
        if gamma >= 0.5 {
            value = b - diff * (1.0 - gamma);
        }
        Some(value)
    }

    /// Python's `round(x, ndigits)`: the double nearest the correctly rounded decimal.
    pub(super) fn py_round(x: f64, ndigits: i32) -> f64 {
        let places = usize::try_from(ndigits).unwrap_or(0);
        format!("{x:.places$}").parse::<f64>().unwrap_or(x)
    }

    /// `repr(float)`: shortest round-trip digits, positional for exponents -4..16 (always
    /// with a decimal point), scientific otherwise.
    pub(super) fn py_float_repr(x: f64) -> String {
        let sci = format!("{x:e}");
        let Some((mantissa, exp)) = sci.split_once('e') else {
            return sci;
        };
        let Ok(exp) = exp.parse::<i64>() else {
            return sci;
        };
        let sign = if mantissa.starts_with('-') { "-" } else { "" };
        let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
        let n = i64::try_from(digits.len()).unwrap_or(i64::MAX);
        if (-4..16).contains(&exp) {
            if exp < 0 {
                let zeros = "0".repeat(usize::try_from(-exp - 1).unwrap_or(0));
                return format!("{sign}0.{zeros}{digits}");
            }
            let int_len = exp + 1;
            if int_len >= n {
                let zeros = "0".repeat(usize::try_from(int_len - n).unwrap_or(0));
                return format!("{sign}{digits}{zeros}.0");
            }
            let cut = usize::try_from(int_len).unwrap_or(0);
            return format!("{sign}{}.{}", &digits[..cut], &digits[cut..]);
        }
        let (head, rest) = digits.split_at(1);
        let body = if rest.is_empty() {
            head.to_owned()
        } else {
            format!("{head}.{rest}")
        };
        let esign = if exp < 0 { "-" } else { "+" };
        format!("{sign}{body}e{esign}{:02}", exp.unsigned_abs())
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn repr_matches_python() {
            for (x, s) in [
                (0.0, "0.0"),
                (-0.0, "-0.0"),
                (1.0, "1.0"),
                (0.1, "0.1"),
                (1e-5, "1e-05"),
                (0.0001, "0.0001"),
                (255.675, "255.675"),
                (1e16, "1e+16"),
                (1.5e16, "1.5e+16"),
                (123_456_789_012_345.6, "123456789012345.6"),
                (-0.2145838601, "-0.2145838601"),
            ] {
                assert_eq!(py_float_repr(x), s);
            }
        }

        #[test]
        fn percentile_matches_numpy() {
            let s = [1.0, 2.0, 3.0, 4.0, 10.0];
            assert_eq!(percentile_linear(&s, 0.5), Some(3.0));
            assert_eq!(percentile_linear(&s, 0.95), Some(8.799_999_999_999_999));
            assert_eq!(percentile_linear(&s, 0.05), Some(1.2));
            let t = [0.1, 0.25, 0.3, 0.7];
            assert_eq!(percentile_linear(&t, 0.75), Some(0.399_999_999_999_999_97));
        }
    }
}

/// SHA-256 (FIPS 180-4), for the metrics hash. Pure computation; no dependency taken.
mod sha256 {
    const K: [u32; 64] = [
        0x428a_2f98,
        0x7137_4491,
        0xb5c0_fbcf,
        0xe9b5_dba5,
        0x3956_c25b,
        0x59f1_11f1,
        0x923f_82a4,
        0xab1c_5ed5,
        0xd807_aa98,
        0x1283_5b01,
        0x2431_85be,
        0x550c_7dc3,
        0x72be_5d74,
        0x80de_b1fe,
        0x9bdc_06a7,
        0xc19b_f174,
        0xe49b_69c1,
        0xefbe_4786,
        0x0fc1_9dc6,
        0x240c_a1cc,
        0x2de9_2c6f,
        0x4a74_84aa,
        0x5cb0_a9dc,
        0x76f9_88da,
        0x983e_5152,
        0xa831_c66d,
        0xb003_27c8,
        0xbf59_7fc7,
        0xc6e0_0bf3,
        0xd5a7_9147,
        0x06ca_6351,
        0x1429_2967,
        0x27b7_0a85,
        0x2e1b_2138,
        0x4d2c_6dfc,
        0x5338_0d13,
        0x650a_7354,
        0x766a_0abb,
        0x81c2_c92e,
        0x9272_2c85,
        0xa2bf_e8a1,
        0xa81a_664b,
        0xc24b_8b70,
        0xc76c_51a3,
        0xd192_e819,
        0xd699_0624,
        0xf40e_3585,
        0x106a_a070,
        0x19a4_c116,
        0x1e37_6c08,
        0x2748_774c,
        0x34b0_bcb5,
        0x391c_0cb3,
        0x4ed8_aa4a,
        0x5b9c_ca4f,
        0x682e_6ff3,
        0x748f_82ee,
        0x78a5_636f,
        0x84c8_7814,
        0x8cc7_0208,
        0x90be_fffa,
        0xa450_6ceb,
        0xbef9_a3f7,
        0xc671_78f2,
    ];

    const H0: [u32; 8] = [
        0x6a09_e667,
        0xbb67_ae85,
        0x3c6e_f372,
        0xa54f_f53a,
        0x510e_527f,
        0x9b05_688c,
        0x1f83_d9ab,
        0x5be0_cd19,
    ];

    /// Lower-case hex digest, as `hashlib.sha256(data).hexdigest()`.
    pub(super) fn hex_digest(data: &[u8]) -> String {
        let mut message = data.to_vec();
        let bit_len = u64::try_from(data.len())
            .unwrap_or(u64::MAX)
            .wrapping_mul(8);
        message.push(0x80);
        while message.len() % 64 != 56 {
            message.push(0);
        }
        message.extend_from_slice(&bit_len.to_be_bytes());
        let mut h = H0;
        for block in message.as_chunks::<64>().0 {
            let mut w = [0u32; 64];
            for (i, word) in block.as_chunks::<4>().0.iter().enumerate() {
                w[i] = u32::from_be_bytes([word[0], word[1], word[2], word[3]]);
            }
            for i in 16..64 {
                let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
                let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
                w[i] = w[i - 16]
                    .wrapping_add(s0)
                    .wrapping_add(w[i - 7])
                    .wrapping_add(s1);
            }
            let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh] = h;
            for i in 0..64 {
                let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
                let ch = (e & f) ^ (!e & g);
                let t1 = hh
                    .wrapping_add(s1)
                    .wrapping_add(ch)
                    .wrapping_add(K[i])
                    .wrapping_add(w[i]);
                let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
                let maj = (a & b) ^ (a & c) ^ (b & c);
                let t2 = s0.wrapping_add(maj);
                hh = g;
                g = f;
                f = e;
                e = d.wrapping_add(t1);
                d = c;
                c = b;
                b = a;
                a = t1.wrapping_add(t2);
            }
            for (slot, v) in h.iter_mut().zip([a, b, c, d, e, f, g, hh]) {
                *slot = slot.wrapping_add(v);
            }
        }
        h.iter().map(|v| format!("{v:08x}")).collect()
    }

    #[cfg(test)]
    mod tests {
        #[test]
        fn known_digests() {
            assert_eq!(
                super::hex_digest(b""),
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            );
            assert_eq!(
                super::hex_digest(b"abc"),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
            );
        }
    }
}
