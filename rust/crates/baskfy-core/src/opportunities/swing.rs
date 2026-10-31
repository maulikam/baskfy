//! Port of `baskfy_core.opportunities.swing`: breakout, pullback and relative-strength
//! evaluation of one completed daily bar, the opportunity it becomes, and the scan.

use std::collections::{BTreeMap, HashMap, HashSet};

use chrono::NaiveDate;
use polars::prelude::DataFrame;
use rust_decimal::{Decimal, RoundingStrategy};
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::opportunities::config::{SetupType, SwingFilters, SwingParams, TriggerStatus};
use crate::opportunities::indicators::{IndicatorRow, rows_on};
use crate::opportunities::ranking::{ScoreBreakdown, score_swing};
use crate::py::{decimal_from_float_str, float_repr};

const PCT: f64 = 100.0;
const CRORE: f64 = 10_000_000.0;

/// Ports `Evidence`: one visible check behind a setup.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Evidence {
    /// Stable key.
    pub key: String,
    /// Human label.
    pub label: String,
    /// Observed value.
    pub value: Option<f64>,
    /// Threshold it is compared with.
    pub threshold: Option<f64>,
    /// Unit of value and threshold.
    pub unit: String,
    /// Whether the check passed.
    pub passed: bool,
}

impl Evidence {
    fn new(
        key: &str,
        label: impl Into<String>,
        value: Option<f64>,
        threshold: Option<f64>,
        unit: &str,
        passed: bool,
    ) -> Self {
        Self {
            key: key.to_owned(),
            label: label.into(),
            value,
            threshold,
            unit: unit.to_owned(),
            passed,
        }
    }
}

/// Ports `Target`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Target {
    /// `T1 structural`, `T1 ATR reach` or `T2 extension`.
    pub label: String,
    /// Price, adjusted terms.
    pub price: f64,
    /// Distance from entry in R.
    pub r_multiple: f64,
}

/// Ports `Evaluation`: one setup read off one bar.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Evaluation {
    /// The setup.
    pub setup: SetupType,
    /// Pending or triggered.
    pub status: TriggerStatus,
    /// What would trigger it, in words.
    pub trigger_condition: String,
    /// The trigger price.
    pub trigger_level: f64,
    /// `CLOSE_OF_TRIGGER_BAR` or `TRIGGER_LEVEL`.
    pub entry_basis: String,
    /// Assumed entry.
    pub entry: f64,
    /// Invalidation (stop).
    pub invalidation: f64,
    /// One or two targets; never empty.
    pub targets: Vec<Target>,
    /// The checks behind it.
    pub evidence: Vec<Evidence>,
    /// Holding horizon in sessions.
    pub horizon: (i64, i64),
    /// Extension past the trigger in ATR (triggered only).
    pub extension_atr: Option<f64>,
    /// Distance to the trigger in % (pending only).
    pub distance_pct: Option<f64>,
}

impl Evaluation {
    /// Ports `risk`: entry minus invalidation.
    #[must_use]
    pub fn risk(&self) -> f64 {
        self.entry - self.invalidation
    }

    /// Ports `reward`: first target minus entry.
    #[must_use]
    pub fn reward(&self) -> f64 {
        self.targets.first().map_or(f64::NAN, |t| t.price) - self.entry
    }

    /// Ports `risk_reward`: reward over risk, 0 when risk is not positive.
    #[must_use]
    pub fn risk_reward(&self) -> f64 {
        if self.risk() > 0.0 {
            self.reward() / self.risk()
        } else {
            0.0
        }
    }
}

/// Ports `InstrumentContext`: per-name facts the bars do not carry.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct InstrumentContext {
    /// Company name.
    pub name: String,
    /// Sector, if known.
    pub sector: Option<String>,
    /// Sessions missing from the recent window.
    pub missing_sessions: i64,
    /// Corporate actions in the lookback, `(ex_date, kind)`.
    pub recent_actions: Vec<(NaiveDate, String)>,
    /// Corporate actions inside the horizon, `(ex_date, kind)`.
    pub upcoming_actions: Vec<(NaiveDate, String)>,
    /// In the F&O ban list for the next session.
    pub in_fno_ban: bool,
}

/// Ports `SwingOpportunity`: an evaluation priced in today's terms, scored.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SwingOpportunity {
    /// Instrument id.
    pub instrument_id: i64,
    /// Trading symbol.
    pub symbol: String,
    /// Company name.
    pub name: String,
    /// Sector.
    pub sector: Option<String>,
    /// The evaluation (adjusted float levels).
    pub evaluation: Evaluation,
    /// Close.
    pub close: Decimal,
    /// Entry.
    pub entry: Decimal,
    /// Trigger level.
    pub trigger_level: Decimal,
    /// Invalidation.
    pub invalidation: Decimal,
    /// `(label, price, r_multiple)` per target.
    pub targets: Vec<(String, Decimal, Decimal)>,
    /// Risk per share.
    pub risk_per_share: Decimal,
    /// Reward per share at T1.
    pub reward_per_share: Decimal,
    /// Reward-to-risk at T1.
    pub risk_reward: Decimal,
    /// ATR, price terms.
    pub atr: Decimal,
    /// ATR as % of close.
    pub atr_pct: Decimal,
    /// 20-session historical volatility, annualised %.
    pub hv_pct: Option<Decimal>,
    /// 20-session average turnover, crore.
    pub avg_turnover_cr: Decimal,
    /// 20-session average volume.
    pub avg_volume: i64,
    /// Excess return over the benchmark, pp.
    pub rs_excess_pct: Option<Decimal>,
    /// Lookback of the excess return.
    pub rs_lookback: i64,
    /// The score.
    pub score: ScoreBreakdown,
    /// Session the bar closed.
    pub data_as_of: NaiveDate,
    /// Data caveats, in words.
    pub limitations: Vec<String>,
    /// False when a caveat blocks acting on it.
    pub actionable: bool,
}

impl SwingOpportunity {
    /// Ports `id`: `SETUP:instrument_id:YYYY-MM-DD`.
    #[must_use]
    pub fn id(&self) -> String {
        format!(
            "{}:{}:{}",
            self.evaluation.setup.value(),
            self.instrument_id,
            self.data_as_of
        )
    }
}

/// Ports `ScanCounts`.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScanCounts {
    /// Names with a bar on the scan date.
    pub universe: i64,
    /// Names that passed the filters and were evaluated.
    pub evaluated: i64,
    /// Names with too little history.
    pub insufficient_history: i64,
    /// Opportunities found (before the limit).
    pub qualified: i64,
    /// Of those, pending.
    pub pending: i64,
    /// Of those, triggered.
    pub triggered: i64,
}

/// Ports `SwingScanResult`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct SwingScanResult {
    /// Scan date; `None` when no name had a bar on it.
    pub as_of: Option<NaiveDate>,
    /// Counts.
    pub counts: ScanCounts,
    /// Ranked opportunities, limited.
    pub opportunities: Vec<SwingOpportunity>,
    /// Scan-level warnings.
    pub warnings: Vec<String>,
}

/// Ports `_num`: a finite value, else `None`.
fn num(value: Option<f64>) -> Option<f64> {
    value.filter(|v| v.is_finite())
}

/// Python's `x or default` on an optional float (0.0 is falsy).
fn or_f(value: Option<f64>, default: f64) -> f64 {
    match value {
        Some(v) if v != 0.0 => v,
        _ => default,
    }
}

/// Python's `min(a, b)` on floats: `a` unless `b < a`.
fn py_min(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// Python's `max(a, b)` on floats: `a` unless `b > a`.
fn py_max(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// Ports `effective_horizon`: the setup's horizon clipped to the request, `None` when disjoint.
#[must_use]
pub fn effective_horizon(
    setup: SetupType,
    params: &SwingParams,
    filters: &SwingFilters,
) -> Option<(i64, i64)> {
    let (low, high) = params.horizon_for(setup);
    let start = low.max(filters.horizon_min);
    let end = high.min(filters.horizon_max);
    (start <= end).then_some((start, end))
}

fn targets(
    entry: f64,
    stop: f64,
    structural: Option<f64>,
    reach: f64,
    extension_r: f64,
) -> Vec<Target> {
    let risk = entry - stop;
    let first = match structural {
        Some(s) if s > entry => py_min(s, reach),
        _ => reach,
    };
    let label = match structural {
        Some(s) if s > entry && s <= reach => "T1 structural",
        _ => "T1 ATR reach",
    };
    let r_of = |price: f64| if risk > 0.0 { (price - entry) / risk } else { 0.0 };
    let mut out = vec![Target {
        label: label.to_owned(),
        price: first,
        r_multiple: r_of(first),
    }];
    let mut second = entry + extension_r * risk;
    if let Some(s) = structural.filter(|s| *s > first) {
        second = py_max(second, s);
    }
    if second > first {
        out.push(Target {
            label: "T2 extension".to_owned(),
            price: second,
            r_multiple: r_of(second),
        });
    }
    out
}

fn reach(entry: f64, atr: f64, horizon: (i64, i64), params: &SwingParams) -> f64 {
    entry + params.risk.reach_atr_per_sqrt_session * atr * (horizon.1 as f64).sqrt()
}

const fn entry_basis(triggered: bool) -> &'static str {
    if triggered {
        "CLOSE_OF_TRIGGER_BAR"
    } else {
        "TRIGGER_LEVEL"
    }
}

const fn status_of(triggered: bool) -> TriggerStatus {
    if triggered {
        TriggerStatus::Triggered
    } else {
        TriggerStatus::Pending
    }
}

/// Ports `evaluate_breakout`.
#[must_use]
pub fn evaluate_breakout(
    row: &IndicatorRow,
    params: &SwingParams,
    horizon: (i64, i64),
) -> Option<Evaluation> {
    let p = &params.breakout;
    let (close, pivot, base_low) = (num(row.close)?, num(row.pivot)?, num(row.base_low)?);
    let atr = num(row.atr).filter(|a| *a > 0.0)?;
    let (sma_mid, rvol) = (num(row.sma_mid), num(row.rvol));
    let depth = (pivot - base_low) / pivot * PCT;
    let trend = sma_mid.is_some_and(|m| close > m);
    let depth_ok = depth <= p.max_base_depth_pct;
    let extension = (close - pivot) / atr;
    let volume_ok = rvol.is_some_and(|r| r >= p.volume_multiple);
    let distance = (pivot - close) / close * PCT;
    let triggered = close > pivot && volume_ok && extension <= p.max_extension_atr;
    let pending = close <= pivot && distance <= p.near_pivot_pct;
    if !(trend && depth_ok && (triggered || pending)) {
        return None;
    }
    let entry = if triggered { close } else { pivot };
    let stop = entry - params.risk.stop_atr * atr;
    let evidence = vec![
        Evidence::new(
            "trend",
            "Close above 50-session average",
            Some(close),
            sma_mid,
            "₹",
            trend,
        ),
        Evidence::new(
            "base_depth",
            "Base depth",
            Some(depth),
            Some(p.max_base_depth_pct),
            "%",
            depth_ok,
        ),
        Evidence::new(
            "close_vs_pivot",
            format!("Close vs {}-session pivot", p.pivot_lookback),
            Some(close),
            Some(pivot),
            "₹",
            close > pivot,
        ),
        Evidence::new(
            "rvol",
            "Volume vs 20-session average",
            rvol,
            Some(p.volume_multiple),
            "x",
            volume_ok,
        ),
        Evidence::new(
            "extension",
            "Extension above pivot",
            triggered.then_some(extension),
            Some(p.max_extension_atr),
            "ATR",
            triggered,
        ),
    ];
    Some(Evaluation {
        setup: SetupType::Breakout,
        status: status_of(triggered),
        trigger_condition: format!(
            "Daily close above {pivot:.2} with volume at least {}x the 20-session average",
            float_repr(p.volume_multiple)
        ),
        trigger_level: pivot,
        entry_basis: entry_basis(triggered).to_owned(),
        entry,
        invalidation: stop,
        targets: targets(
            entry,
            stop,
            Some(entry + (pivot - base_low)),
            reach(entry, atr, horizon, params),
            params.risk.extension_r,
        ),
        evidence,
        horizon,
        extension_atr: triggered.then_some(extension),
        distance_pct: (!triggered).then_some(distance),
    })
}

/// Ports `evaluate_pullback`.
#[must_use]
pub fn evaluate_pullback(
    row: &IndicatorRow,
    params: &SwingParams,
    horizon: (i64, i64),
) -> Option<Evaluation> {
    let p = &params.pullback;
    let (close, high, prev_high) = (num(row.close)?, num(row.high)?, num(row.prev_high)?);
    let atr = num(row.atr).filter(|a| *a > 0.0)?;
    let (swing_high, recent_low) = (num(row.swing_high)?, num(row.recent_low)?);
    let (sma_mid, sma_prev) = (num(row.sma_mid)?, num(row.sma_mid_prev)?);
    let sma_long = num(row.sma_long);
    let touched = row.touched_fast.unwrap_or(false);
    let above = close > sma_mid;
    let rising = sma_mid > sma_prev;
    let long_ok = sma_long.is_none_or(|l| sma_mid > l);
    let depth = (swing_high - close) / swing_high * PCT;
    let depth_ok = p.min_depth_pct <= depth && depth <= p.max_depth_pct;
    if !(above && rising && long_ok && depth_ok && touched) {
        return None;
    }
    let triggered = close > prev_high;
    let trigger_level = if triggered { prev_high } else { high };
    let entry = if triggered { close } else { high };
    let stop = recent_low - params.risk.stop_buffer_atr * atr;
    if stop >= entry {
        return None;
    }
    let evidence = vec![
        Evidence::new(
            "trend",
            "Close above 50-session average",
            Some(close),
            Some(sma_mid),
            "₹",
            above,
        ),
        Evidence::new(
            "slope",
            "50-session average rising",
            Some(sma_mid),
            Some(sma_prev),
            "₹",
            rising,
        ),
        Evidence::new(
            "long_trend",
            "50 above 200-session average",
            Some(sma_mid),
            sma_long,
            "₹",
            long_ok,
        ),
        Evidence::new(
            "depth",
            format!("Pullback from {}-session high", p.swing_lookback),
            Some(depth),
            Some(p.max_depth_pct),
            "%",
            depth_ok,
        ),
        Evidence::new(
            "touch",
            "Tested 20-session average",
            None,
            Some(p.touch_atr),
            "ATR",
            touched,
        ),
        Evidence::new(
            "reversal",
            "Close above prior bar high",
            Some(close),
            Some(prev_high),
            "₹",
            triggered,
        ),
    ];
    Some(Evaluation {
        setup: SetupType::Pullback,
        status: status_of(triggered),
        trigger_condition: format!("Price trades above {trigger_level:.2} (reversal bar high)"),
        trigger_level,
        entry_basis: entry_basis(triggered).to_owned(),
        entry,
        invalidation: stop,
        targets: targets(
            entry,
            stop,
            Some(swing_high),
            reach(entry, atr, horizon, params),
            params.risk.extension_r,
        ),
        evidence,
        horizon,
        extension_atr: triggered.then(|| (close - prev_high) / atr),
        distance_pct: (!triggered).then(|| (high - close) / close * PCT),
    })
}

/// Ports `evaluate_relative_strength`.
#[must_use]
pub fn evaluate_relative_strength(
    row: &IndicatorRow,
    params: &SwingParams,
    horizon: (i64, i64),
) -> Option<Evaluation> {
    let p = &params.relative_strength;
    let close = num(row.close)?;
    let atr = num(row.atr).filter(|a| *a > 0.0)?;
    let (excess, rs_line) = (num(row.rs_excess_pct)?, num(row.rs_line)?);
    let (rs_high, trigger) = (num(row.rs_line_high)?, num(row.rs_trigger)?);
    let sma_mid = num(row.sma_mid)?;
    let excess_ok = excess >= p.min_excess_pct;
    let near_high = rs_line >= rs_high * (1.0 - p.rs_near_high_pct / PCT);
    let above = close > sma_mid;
    if !(excess_ok && near_high && above) {
        return None;
    }
    let triggered = close > trigger;
    let entry = if triggered { close } else { trigger };
    let stop = entry - params.risk.stop_atr * atr;
    let evidence = vec![
        Evidence::new(
            "excess",
            format!("{}-session return above benchmark", p.lookback),
            Some(excess),
            Some(p.min_excess_pct),
            "pp",
            excess_ok,
        ),
        Evidence::new(
            "rs_high",
            "Relative-strength line near its high",
            Some((rs_line / rs_high - 1.0) * PCT),
            Some(-p.rs_near_high_pct),
            "%",
            near_high,
        ),
        Evidence::new(
            "trend",
            "Close above 50-session average",
            Some(close),
            Some(sma_mid),
            "₹",
            above,
        ),
        Evidence::new(
            "trigger",
            format!("Close above {}-session high", p.trigger_lookback),
            Some(close),
            Some(trigger),
            "₹",
            triggered,
        ),
    ];
    Some(Evaluation {
        setup: SetupType::RelativeStrength,
        status: status_of(triggered),
        trigger_condition: format!(
            "Daily close above {trigger:.2} ({}-session high)",
            p.trigger_lookback
        ),
        trigger_level: trigger,
        entry_basis: entry_basis(triggered).to_owned(),
        entry,
        invalidation: stop,
        targets: targets(
            entry,
            stop,
            None,
            reach(entry, atr, horizon, params),
            params.risk.extension_r,
        ),
        evidence,
        horizon,
        extension_atr: triggered.then(|| (close - trigger) / atr),
        distance_pct: (!triggered).then(|| (trigger - close) / close * PCT),
    })
}

/// Ports `evaluate`: dispatch on the setup.
#[must_use]
pub fn evaluate(
    row: &IndicatorRow,
    setup: SetupType,
    params: &SwingParams,
    horizon: (i64, i64),
) -> Option<Evaluation> {
    match setup {
        SetupType::Breakout => evaluate_breakout(row, params, horizon),
        SetupType::Pullback => evaluate_pullback(row, params, horizon),
        SetupType::RelativeStrength => evaluate_relative_strength(row, params, horizon),
    }
}

/// `Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)`.
fn q2(x: f64) -> CoreResult<Decimal> {
    let d = decimal_from_float_str(x).map_err(|e| CoreError::Arithmetic(e.0))?;
    let mut out = d.round_dp_with_strategy(2, RoundingStrategy::MidpointAwayFromZero);
    out.rescale(2);
    Ok(out)
}

/// Ports `_price`: an adjusted price back in today's terms, to the paisa.
fn price(value: f64, factor: f64) -> CoreResult<Decimal> {
    q2(value / factor)
}

/// Ports `_ratio`.
fn ratio(value: f64) -> CoreResult<Decimal> {
    q2(value)
}

/// Ports `limitations_for`: the data caveats and whether any blocks acting.
#[must_use]
pub fn limitations_for(
    row: &IndicatorRow,
    context: &InstrumentContext,
    params: &SwingParams,
) -> (Vec<String>, bool) {
    let mut notes = Vec::new();
    let mut blocking = false;
    let bars = row.bars.unwrap_or(0);
    if bars < params.data.long_trend_bars {
        notes.push(format!(
            "Only {bars} sessions of history; the {}-session trend check was skipped.",
            params.data.long_trend_bars
        ));
    }
    if context.missing_sessions > 0 {
        notes.push(format!(
            "{} trading session(s) missing in the last {}; indicators span the gap.",
            context.missing_sessions, params.data.window_sessions
        ));
        if context.missing_sessions > params.data.max_missing_sessions {
            blocking = true;
        }
    }
    for (day, kind) in &context.recent_actions {
        notes.push(format!(
            "Corporate action ({kind}) on {day}; levels use adjusted prices and are shown in \
             today's price terms."
        ));
    }
    for (day, kind) in &context.upcoming_actions {
        notes.push(format!(
            "Upcoming corporate action ({kind}) on {day} inside the horizon."
        ));
    }
    if num(row.bench_level).is_none() {
        notes.push(
            "Benchmark level missing for this session; relative strength unavailable.".to_owned(),
        );
    }
    if context.in_fno_ban {
        notes.push("Stock is in the F&O ban list for the next session.".to_owned());
    }
    (notes, blocking)
}

/// Ports `build_opportunity`.
///
/// # Errors
/// [`CoreError::InvalidInput`] when the row has no date (Python's `ValueError`);
/// [`CoreError::Arithmetic`] for a non-finite level that cannot become a `Decimal`.
pub fn build_opportunity(
    row: &IndicatorRow,
    evaluation: Evaluation,
    context: &InstrumentContext,
    params: &SwingParams,
) -> CoreResult<SwingOpportunity> {
    let factor = or_f(num(row.adj_factor), 1.0);
    let atr = or_f(num(row.atr), 0.0);
    let close = or_f(num(row.close), 0.0);
    let excess = num(row.rs_excess_pct);
    let turnover = or_f(num(row.turnover_avg), 0.0) / CRORE;
    let (mut notes, blocking) = limitations_for(row, context, params);
    if evaluation.status == TriggerStatus::Triggered {
        notes.push(
            "Triggered on the completed bar; the next session may open away from the entry \
             assumption."
                .to_owned(),
        );
    }
    let score = score_swing(
        &evaluation,
        num(row.rvol),
        close,
        num(row.sma_mid),
        num(row.sma_mid_prev),
        num(row.sma_long),
        excess,
        turnover,
        params,
    );
    let date = row
        .date
        .ok_or_else(|| CoreError::InvalidInput("indicator row has no date".to_owned()))?;
    let targets = evaluation
        .targets
        .iter()
        .map(|t| Ok((t.label.clone(), price(t.price, factor)?, ratio(t.r_multiple)?)))
        .collect::<CoreResult<Vec<_>>>()?;
    let hv_pct = num(row.hv_pct).map(ratio).transpose()?;
    Ok(SwingOpportunity {
        instrument_id: row.instrument_id.unwrap_or(0),
        symbol: row.symbol.clone().unwrap_or_default(),
        name: context.name.clone(),
        sector: context.sector.clone(),
        close: price(close, factor)?,
        entry: price(evaluation.entry, factor)?,
        trigger_level: price(evaluation.trigger_level, factor)?,
        invalidation: price(evaluation.invalidation, factor)?,
        targets,
        risk_per_share: price(evaluation.risk(), factor)?,
        reward_per_share: price(evaluation.reward(), factor)?,
        risk_reward: ratio(evaluation.risk_reward())?,
        atr: price(atr, factor)?,
        atr_pct: ratio(or_f(num(row.atr_pct), 0.0))?,
        hv_pct,
        avg_turnover_cr: ratio(turnover)?,
        avg_volume: or_f(num(row.volume_avg), 0.0).round_ties_even() as i64,
        rs_excess_pct: excess.map(ratio).transpose()?,
        rs_lookback: params.relative_strength.lookback,
        score,
        data_as_of: date,
        limitations: notes,
        actionable: !blocking,
        evaluation,
    })
}

/// Ports `passes_filters`: sector, unadjusted price, turnover and ATR % gates.
#[must_use]
pub fn passes_filters(
    row: &IndicatorRow,
    context: &InstrumentContext,
    filters: &SwingFilters,
) -> bool {
    let factor = or_f(num(row.adj_factor), 1.0);
    let close = or_f(num(row.close), 0.0) / factor;
    let atr_pct = num(row.atr_pct);
    let turnover = or_f(num(row.turnover_avg), 0.0) / CRORE;
    if !filters.sectors.is_empty()
        && !context
            .sector
            .as_ref()
            .is_some_and(|s| filters.sectors.contains(s))
    {
        return false;
    }
    if close < filters.min_price || turnover < filters.min_turnover_cr {
        return false;
    }
    atr_pct.is_some_and(|a| filters.atr_pct_min <= a && a <= filters.atr_pct_max)
}

/// Python's `seq[:n]` bound for a length.
fn head_len(len: usize, n: i64) -> usize {
    let len_i = i64::try_from(len).unwrap_or(i64::MAX);
    let end = if n < 0 { (len_i + n).max(0) } else { n.min(len_i) };
    usize::try_from(end).unwrap_or(0)
}

/// Ports `scan`: evaluate every name with a bar on `as_of`, rank, limit.
///
/// # Errors
/// A polars error reading the frame; [`CoreError::InvalidInput`] for a row without an
/// instrument id; [`CoreError::Arithmetic`] for a non-finite level.
pub fn scan(
    indicated: &DataFrame,
    as_of: NaiveDate,
    contexts: &HashMap<i64, InstrumentContext>,
    filters: &SwingFilters,
    params: &SwingParams,
) -> CoreResult<SwingScanResult> {
    let today = rows_on(indicated, as_of)?;
    let universe = today.len();
    let horizons: BTreeMap<SetupType, (i64, i64)> = filters
        .setups
        .iter()
        .filter_map(|s| effective_horizon(*s, params, filters).map(|h| (*s, h)))
        .collect();
    let mut warnings = Vec::new();
    let skipped: Vec<&str> = filters
        .setups
        .iter()
        .filter(|s| !horizons.contains_key(s))
        .map(|s| s.value())
        .collect();
    if !skipped.is_empty() {
        warnings.push(format!(
            "Setups {} were skipped: their holding horizon does not overlap {}-{} sessions.",
            skipped.join(", "),
            filters.horizon_min,
            filters.horizon_max
        ));
    }
    let default_context = InstrumentContext::default();
    let mut insufficient = 0_i64;
    let mut evaluated = 0_i64;
    let mut found = Vec::new();
    for row in &today {
        if row.bars.unwrap_or(0) < params.data.min_bars {
            insufficient += 1;
            continue;
        }
        let id = row
            .instrument_id
            .ok_or_else(|| CoreError::InvalidInput("row has no instrument_id".to_owned()))?;
        let context = contexts.get(&id).unwrap_or(&default_context);
        if !passes_filters(row, context, filters) {
            continue;
        }
        evaluated += 1;
        for (setup, horizon) in &horizons {
            let Some(evaluation) = evaluate(row, *setup, params, *horizon) else {
                continue;
            };
            if evaluation.risk() <= 0.0 || evaluation.risk_reward() < filters.min_rr {
                continue;
            }
            found.push(build_opportunity(row, evaluation, context, params)?);
        }
    }
    if found.iter().any(|o| o.rs_excess_pct.is_none()) {
        warnings.push("Benchmark series incomplete; relative-strength readings are partial.".to_owned());
    }
    let mut ranked = rank(found);
    let count = |status| {
        ranked
            .iter()
            .filter(|o| o.evaluation.status == status)
            .count() as i64
    };
    let counts = ScanCounts {
        universe: universe as i64,
        evaluated,
        insufficient_history: insufficient,
        qualified: ranked.len() as i64,
        pending: count(TriggerStatus::Pending),
        triggered: count(TriggerStatus::Triggered),
    };
    ranked.truncate(head_len(ranked.len(), filters.limit));
    Ok(SwingScanResult {
        as_of: (universe > 0).then_some(as_of),
        counts,
        opportunities: ranked,
        warnings,
    })
}

/// Ports `rank`: score descending, then symbol, then setup (stable).
#[must_use]
pub fn rank(opportunities: Vec<SwingOpportunity>) -> Vec<SwingOpportunity> {
    let mut out = opportunities;
    out.sort_by(|a, b| {
        (-a.score.total)
            .partial_cmp(&-b.score.total)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.symbol.cmp(&b.symbol))
            .then_with(|| a.evaluation.setup.value().cmp(b.evaluation.setup.value()))
    });
    out
}

/// Ports `missing_sessions`: calendar sessions in the last `window` (from the first bar on)
/// with no bar.
#[must_use]
pub fn missing_sessions(present: &[NaiveDate], calendar: &[NaiveDate], window: i64) -> i64 {
    let Some(first) = present.iter().min() else {
        return 0;
    };
    let have: HashSet<&NaiveDate> = present.iter().collect();
    let mut sorted = calendar.to_vec();
    sorted.sort_unstable();
    let len = i64::try_from(sorted.len()).unwrap_or(i64::MAX);
    // Python's `[-window:]`: `[-0:]` is the whole list.
    let start = if window > 0 { (len - window).max(0) } else { (-window).min(len) };
    let start = usize::try_from(start).unwrap_or(0);
    sorted[start..]
        .iter()
        .filter(|d| *d >= first && !have.contains(d))
        .count() as i64
}
