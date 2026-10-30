//! `baskfy_core.exposure.regime` (re-exported by the desk as `app/core/regime.py`) — the **pure**
//! regime engine: the typed values, the moving averages and their Schmitt-trigger hysteresis,
//! book-composition weights, weighted health, the R4 → R3 → R1 → R2 classifier, the sentinel
//! floor, the recovery override, the asymmetric transitions, new buys and forced action, and
//! the single entry point [`evaluate`].
//!
//! No I/O, no clock, no configuration read: everything arrives as an argument, so a live
//! evaluation and a historical replay given identical inputs produce identical decisions (the
//! live acceptance requirement the Python docstring states). The design invariants are
//! Python's: the market signal and the portfolio's exposure are separate facts; UNKNOWN, stale
//! or low-coverage inputs never permit R1 or new buys and never *independently* escalate a tier;
//! hysteresis compares each confirming close against **that day's** moving average.
//!
//! Floats follow CPython: `sum()` is 3.12's compensated sum ([`py_sum`]), `round()` is the
//! correctly rounded decimal, `+=` is plain IEEE addition.

use std::collections::BTreeMap;

use chrono::{Datelike, Duration, NaiveDate};
use sha2::Digest as _;

use super::{RegimeConfig, describe, is_affirmative_risk_off};
use crate::lanes::analytics::breadth::BreadthReading;
use crate::lanes::weekly::pandas::{Num, py_round};
use crate::wire::{PyDict, PyValue, dict};

pub use crate::lanes::analytics::index_cache::Candle;

/// `a == b` with Python's float semantics (`0.0 == -0.0`, NaN unequal to everything).
#[must_use]
pub fn feq(a: f64, b: f64) -> bool {
    matches!(a.partial_cmp(&b), Some(std::cmp::Ordering::Equal))
}

/// CPython 3.12's `sum()` over floats (start `0`): Neumaier-compensated.
#[must_use]
pub fn py_sum(values: impl IntoIterator<Item = f64>) -> f64 {
    match crate::lanes::weekly::pandas::py_sum(values.into_iter().map(Num::Float)) {
        Num::Float(f) => f,
        #[allow(clippy::cast_precision_loss)]
        Num::Int(i) => i as f64,
    }
}

// =============================================================================================
// typed values
// =============================================================================================

/// `RegimeTier`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum RegimeTier {
    /// Risk-on.
    R1,
    /// Caution.
    R2,
    /// Risk-off.
    R3,
    /// Crash.
    R4,
}

impl RegimeTier {
    /// `.ordinal`: 1 = most risk-on, 4 = most risk-off.
    #[must_use]
    pub const fn ordinal(self) -> i64 {
        match self {
            Self::R1 => 1,
            Self::R2 => 2,
            Self::R3 => 3,
            Self::R4 => 4,
        }
    }

    /// `from_ordinal(n)`, clamped to 1..=4.
    #[must_use]
    pub const fn from_ordinal(n: i64) -> Self {
        match n {
            i64::MIN..=1 => Self::R1,
            2 => Self::R2,
            3 => Self::R3,
            _ => Self::R4,
        }
    }

    /// `.value`.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::R1 => "R1",
            Self::R2 => "R2",
            Self::R3 => "R3",
            Self::R4 => "R4",
        }
    }

    /// `RegimeTier(value)`; `None` where Python raises `ValueError`.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "R1" => Some(Self::R1),
            "R2" => Some(Self::R2),
            "R3" => Some(Self::R3),
            "R4" => Some(Self::R4),
            _ => None,
        }
    }
}

/// `SignalState`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SignalState {
    /// Confirmed above.
    Above,
    /// Confirmed below.
    Below,
    /// Not yet confirmed either side.
    Unknown,
}

impl SignalState {
    /// `.value`.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Above => "above",
            Self::Below => "below",
            Self::Unknown => "unknown",
        }
    }
}

/// `NewBuyMode`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum NewBuyMode {
    /// Full-size entries.
    Full,
    /// Half-size entries.
    Half,
    /// No new entries.
    Blocked,
}

impl NewBuyMode {
    /// `.value`.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Full => "full",
            Self::Half => "half",
            Self::Blocked => "blocked",
        }
    }
}

/// `ForcedAction`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ForcedAction {
    /// Nothing forced.
    None,
    /// R2: trim to the cap.
    TrimToCap,
    /// R3: reduce to the cap.
    ReduceToCap,
    /// R4: crash reduction.
    CrashReduce,
    /// A person must decide.
    ManualReview,
}

impl ForcedAction {
    /// `.value`.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::TrimToCap => "trim_to_cap",
            Self::ReduceToCap => "reduce_to_cap",
            Self::CrashReduce => "crash_reduce",
            Self::ManualReview => "manual_review",
        }
    }
}

/// `ExecutionStatus`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ExecutionStatus {
    /// No plan.
    NotPlanned,
    /// Planned, not sent.
    Planned,
    /// Some legs failed.
    Partial,
    /// Every leg went.
    Completed,
    /// Every leg failed.
    Failed,
}

impl ExecutionStatus {
    /// `.value`.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::NotPlanned => "not_planned",
            Self::Planned => "planned",
            Self::Partial => "partial",
            Self::Completed => "completed",
            Self::Failed => "failed",
        }
    }
}

/// `WeightSource`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum WeightSource {
    /// The proposed full-risk target portfolio.
    FullRiskTarget,
    /// The last committed R1 snapshot.
    LastR1Snapshot,
    /// The configured defaults.
    ConfigDefault,
}

impl WeightSource {
    /// `.value`.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::FullRiskTarget => "full_risk_target",
            Self::LastR1Snapshot => "last_r1_snapshot",
            Self::ConfigDefault => "config_default",
        }
    }

    /// `WeightSource(value)`; `None` where Python raises.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "full_risk_target" => Some(Self::FullRiskTarget),
            "last_r1_snapshot" => Some(Self::LastR1Snapshot),
            "config_default" => Some(Self::ConfigDefault),
            _ => None,
        }
    }
}

/// `RegimeMode`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum RegimeMode {
    /// No executable plan.
    Observe,
    /// A plan that needs approval.
    Propose,
    /// A plan that may be executed.
    Enforce,
}

impl RegimeMode {
    /// `.value`.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Observe => "observe",
            Self::Propose => "propose",
            Self::Enforce => "enforce",
        }
    }

    /// `RegimeMode(value)`; `None` where Python raises.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "observe" => Some(Self::Observe),
            "propose" => Some(Self::Propose),
            "enforce" => Some(Self::Enforce),
            _ => None,
        }
    }
}

/// `BootstrapPolicy.OBSERVE_ONLY.value`.
pub const BOOTSTRAP_OBSERVE_ONLY: &str = "observe_only";
/// `BootstrapPolicy.ADOPT_CANDIDATE.value`.
pub const BOOTSTRAP_ADOPT_CANDIDATE: &str = "adopt_candidate";

/// `Reason`: the stable reason codes — the audit contract. Never renamed; only added to.
pub mod reason {
    /// Data quality.
    pub const DATA_STALE: &str = "DATA_STALE";
    /// Data quality.
    pub const DATA_INSUFFICIENT_WARMUP: &str = "DATA_INSUFFICIENT_WARMUP";
    /// Data quality.
    pub const DATA_INDEX_MISSING: &str = "DATA_INDEX_MISSING";
    /// Data quality.
    pub const SIGNAL_UNKNOWN: &str = "SIGNAL_UNKNOWN";
    /// Data quality.
    pub const BREADTH_MISSING: &str = "BREADTH_MISSING";
    /// Data quality.
    pub const BREADTH_STALE: &str = "BREADTH_STALE";
    /// Data quality.
    pub const BREADTH_LOW_COVERAGE: &str = "BREADTH_LOW_COVERAGE";
    /// Data quality.
    pub const BREADTH_NOT_USED: &str = "BREADTH_NOT_USED";
    /// Classifier.
    pub const R4_BEARISH_STACK_AND_WEAK_BREADTH: &str = "R4_BEARISH_STACK_AND_WEAK_BREADTH";
    /// Classifier.
    pub const R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH: &str = "R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH";
    /// Classifier.
    pub const R3_H200_WEAK: &str = "R3_H200_WEAK";
    /// Classifier.
    pub const R3_BREADTH_WEAK: &str = "R3_BREADTH_WEAK";
    /// Classifier.
    pub const R3_SENTINEL_BELOW_200DMA: &str = "R3_SENTINEL_BELOW_200DMA";
    /// Classifier.
    pub const R1_ALL_CONDITIONS_MET: &str = "R1_ALL_CONDITIONS_MET";
    /// Classifier.
    pub const R2_DEFAULT: &str = "R2_DEFAULT";
    /// Sentinel veto.
    pub const SENTINEL_BELOW_50DMA_VETO: &str = "SENTINEL_BELOW_50DMA_VETO";
    /// Sentinel veto.
    pub const SENTINEL_FLOOR_R2: &str = "SENTINEL_FLOOR_R2";
    /// Sentinel veto.
    pub const SENTINEL_FLOOR_R3: &str = "SENTINEL_FLOOR_R3";
    /// Sentinel veto.
    pub const SENTINEL_UNKNOWN: &str = "SENTINEL_UNKNOWN";
    /// Recovery override.
    pub const RECOVERY_OVERRIDE_APPLIED: &str = "RECOVERY_OVERRIDE_APPLIED";
    /// Recovery override.
    pub const RECOVERY_OVERRIDE_UNCONFIRMED: &str = "RECOVERY_OVERRIDE_UNCONFIRMED";
    /// Recovery override.
    pub const RECOVERY_OVERRIDE_H20_TOO_WEAK: &str = "RECOVERY_OVERRIDE_H20_TOO_WEAK";
    /// Recovery override.
    pub const RECOVERY_OVERRIDE_BREADTH_UNUSABLE: &str = "RECOVERY_OVERRIDE_BREADTH_UNUSABLE";
    /// Transitions.
    pub const TRANSITION_RISK_OFF_DIRECT: &str = "TRANSITION_RISK_OFF_DIRECT";
    /// Transitions.
    pub const TRANSITION_RERISK_CAPPED: &str = "TRANSITION_RERISK_CAPPED";
    /// Transitions.
    pub const TRANSITION_NONE: &str = "TRANSITION_NONE";
    /// Transitions.
    pub const TRANSITION_HELD_STALE_DATA: &str = "TRANSITION_HELD_STALE_DATA";
    /// Transitions.
    pub const BOOTSTRAP_NO_PRIOR_STATE: &str = "BOOTSTRAP_NO_PRIOR_STATE";
    /// Transitions.
    pub const BOOTSTRAP_OBSERVE_ONLY: &str = "BOOTSTRAP_OBSERVE_ONLY";
    /// Book weights.
    pub const BOOK_WEIGHTS_FULL_RISK_TARGET: &str = "BOOK_WEIGHTS_FULL_RISK_TARGET";
    /// Book weights.
    pub const BOOK_WEIGHTS_LAST_R1_SNAPSHOT: &str = "BOOK_WEIGHTS_LAST_R1_SNAPSHOT";
    /// Book weights.
    pub const BOOK_WEIGHTS_CONFIG_DEFAULT: &str = "BOOK_WEIGHTS_CONFIG_DEFAULT";
    /// Book weights.
    pub const BOOK_CLASSIFICATION_COVERAGE_LOW: &str = "BOOK_CLASSIFICATION_COVERAGE_LOW";
    /// Buys / exposure.
    pub const NEW_BUYS_FULL: &str = "NEW_BUYS_FULL";
    /// Buys / exposure.
    pub const NEW_BUYS_HALF: &str = "NEW_BUYS_HALF";
    /// Buys / exposure.
    pub const NEW_BUYS_BLOCKED_TIER: &str = "NEW_BUYS_BLOCKED_TIER";
    /// Buys / exposure.
    pub const NEW_BUYS_BLOCKED_SENTINEL: &str = "NEW_BUYS_BLOCKED_SENTINEL";
    /// Buys / exposure.
    pub const NEW_BUYS_BLOCKED_DATA: &str = "NEW_BUYS_BLOCKED_DATA";
    /// Buys / exposure.
    pub const EXPOSURE_WITHIN_CAP: &str = "EXPOSURE_WITHIN_CAP";
    /// Buys / exposure.
    pub const EXPOSURE_GAP_PENDING: &str = "EXPOSURE_GAP_PENDING";
    /// Buys / exposure.
    pub const EXPOSURE_RECONCILE_RETRY: &str = "EXPOSURE_RECONCILE_RETRY";
    /// Buys / exposure.
    pub const FORCED_SELL_SUPPRESSED_BOOTSTRAP: &str = "FORCED_SELL_SUPPRESSED_BOOTSTRAP";

    /// `vars(Reason)`'s values, in declaration order.
    pub const ALL: [&str; 42] = [
        DATA_STALE,
        DATA_INSUFFICIENT_WARMUP,
        DATA_INDEX_MISSING,
        SIGNAL_UNKNOWN,
        BREADTH_MISSING,
        BREADTH_STALE,
        BREADTH_LOW_COVERAGE,
        BREADTH_NOT_USED,
        R4_BEARISH_STACK_AND_WEAK_BREADTH,
        R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH,
        R3_H200_WEAK,
        R3_BREADTH_WEAK,
        R3_SENTINEL_BELOW_200DMA,
        R1_ALL_CONDITIONS_MET,
        R2_DEFAULT,
        SENTINEL_BELOW_50DMA_VETO,
        SENTINEL_FLOOR_R2,
        SENTINEL_FLOOR_R3,
        SENTINEL_UNKNOWN,
        RECOVERY_OVERRIDE_APPLIED,
        RECOVERY_OVERRIDE_UNCONFIRMED,
        RECOVERY_OVERRIDE_H20_TOO_WEAK,
        RECOVERY_OVERRIDE_BREADTH_UNUSABLE,
        TRANSITION_RISK_OFF_DIRECT,
        TRANSITION_RERISK_CAPPED,
        TRANSITION_NONE,
        TRANSITION_HELD_STALE_DATA,
        BOOTSTRAP_NO_PRIOR_STATE,
        BOOTSTRAP_OBSERVE_ONLY,
        BOOK_WEIGHTS_FULL_RISK_TARGET,
        BOOK_WEIGHTS_LAST_R1_SNAPSHOT,
        BOOK_WEIGHTS_CONFIG_DEFAULT,
        BOOK_CLASSIFICATION_COVERAGE_LOW,
        NEW_BUYS_FULL,
        NEW_BUYS_HALF,
        NEW_BUYS_BLOCKED_TIER,
        NEW_BUYS_BLOCKED_SENTINEL,
        NEW_BUYS_BLOCKED_DATA,
        EXPOSURE_WITHIN_CAP,
        EXPOSURE_GAP_PENDING,
        EXPOSURE_RECONCILE_RETRY,
        FORCED_SELL_SUPPRESSED_BOOTSTRAP,
    ];
}

/// What the engine refuses (a Python exception on the same input).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum EngineError {
    /// `ConfigError`.
    #[error("{0}")]
    Config(String),
    /// A value Python's arithmetic raises on (a stored `NULL` close).
    #[error("TypeError: {0}")]
    Type(String),
}

fn opt_f(x: Option<f64>) -> PyValue {
    x.map_or(PyValue::None, PyValue::Float)
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

// =============================================================================================
// market data value objects
// =============================================================================================

/// `MaSignal`.
#[derive(Debug, Clone, PartialEq)]
pub struct MaSignal {
    /// `ma_length`.
    pub ma_length: i64,
    /// `close`.
    pub close: f64,
    /// `ma` (`None` until the window fills).
    pub ma: Option<f64>,
    /// `distance_pct`.
    pub distance_pct: Option<f64>,
    /// `state`.
    pub state: SignalState,
    /// `confirming_closes`.
    pub confirming_closes: i64,
}

impl MaSignal {
    /// `MaSignal(ma_length, close, ma, distance_pct, state, confirming_closes)`.
    #[must_use]
    pub const fn new(
        ma_length: i64,
        close: f64,
        ma: Option<f64>,
        distance_pct: Option<f64>,
        state: SignalState,
        confirming_closes: i64,
    ) -> Self {
        Self {
            ma_length,
            close,
            ma,
            distance_pct,
            state,
            confirming_closes,
        }
    }

    /// `as_dict()`.
    #[must_use]
    pub fn as_dict(&self) -> PyDict {
        dict([
            ("ma_length", PyValue::Int(self.ma_length)),
            ("close", PyValue::Float(self.close)),
            ("ma", opt_f(self.ma)),
            ("distance_pct", opt_f(self.distance_pct)),
            ("state", PyValue::Str(self.state.value().to_owned())),
            ("confirming_closes", PyValue::Int(self.confirming_closes)),
        ])
    }
}

/// `IndexSignals`.
#[derive(Debug, Clone, PartialEq)]
pub struct IndexSignals {
    /// `index_name`.
    pub index_name: String,
    /// `as_of_date`.
    pub as_of_date: Option<NaiveDate>,
    /// `close`.
    pub close: Option<f64>,
    /// `signals`, by MA length.
    pub signals: BTreeMap<i64, MaSignal>,
    /// `is_stale`.
    pub is_stale: bool,
    /// `has_data`.
    pub has_data: bool,
    /// `instrument_token`.
    pub instrument_token: Option<i64>,
}

impl IndexSignals {
    /// `IndexSignals(index_name, as_of_date, close, signals)` with the dataclass defaults.
    #[must_use]
    pub fn new(
        index_name: &str,
        as_of_date: Option<NaiveDate>,
        close: Option<f64>,
        signals: BTreeMap<i64, MaSignal>,
    ) -> Self {
        Self {
            index_name: index_name.to_owned(),
            as_of_date,
            close,
            signals,
            is_stale: false,
            has_data: true,
            instrument_token: None,
        }
    }

    /// `state(ma_length)`: UNKNOWN when there is no such signal.
    #[must_use]
    pub fn state(&self, ma_length: i64) -> SignalState {
        self.signals
            .get(&ma_length)
            .map_or(SignalState::Unknown, |s| s.state)
    }

    /// `is_above(ma_length)`.
    #[must_use]
    pub fn is_above(&self, ma_length: i64) -> bool {
        self.state(ma_length) == SignalState::Above
    }

    /// `is_below(ma_length)`.
    #[must_use]
    pub fn is_below(&self, ma_length: i64) -> bool {
        self.state(ma_length) == SignalState::Below
    }

    /// `any_unknown`.
    #[must_use]
    pub fn any_unknown(&self) -> bool {
        !self.has_data
            || self
                .signals
                .values()
                .any(|s| s.state == SignalState::Unknown)
    }

    /// `bearish_stack(lengths)`: MA20 < MA50 < MA200 **and** the close confirmed below MA20.
    #[must_use]
    pub fn bearish_stack(&self, lengths: &[i64]) -> bool {
        let mut mas = Vec::with_capacity(lengths.len());
        for n in lengths {
            match self.signals.get(n) {
                None => return false,
                Some(s) => mas.push(s.ma),
            }
        }
        let Some(mas): Option<Vec<f64>> = mas.into_iter().collect() else {
            return false;
        };
        let ordered = mas.windows(2).all(|w| w[0] < w[1]);
        ordered && lengths.first().is_some_and(|l| self.is_below(*l))
    }

    /// `as_dict()`.
    #[must_use]
    pub fn as_dict(&self) -> PyDict {
        let mut signals = PyDict::new();
        for (k, v) in &self.signals {
            signals.insert(k.to_string(), PyValue::Map(v.as_dict()));
        }
        dict([
            ("index_name", PyValue::Str(self.index_name.clone())),
            (
                "as_of_date",
                self.as_of_date
                    .map_or(PyValue::None, |d| PyValue::Str(iso(d))),
            ),
            ("close", opt_f(self.close)),
            ("is_stale", PyValue::Bool(self.is_stale)),
            ("has_data", PyValue::Bool(self.has_data)),
            (
                "instrument_token",
                self.instrument_token.map_or(PyValue::None, PyValue::Int),
            ),
            ("signals", PyValue::Map(signals)),
            ("bearish_stack", PyValue::Bool(self.bearish_stack(&[20, 50, 200]))),
        ])
    }
}

impl BreadthReading {
    /// `is_usable(cfg, as_of)`: usable, and the codes that say why not.
    #[must_use]
    pub fn is_usable(&self, cfg: &RegimeConfig, as_of: NaiveDate) -> (bool, Vec<&'static str>) {
        let mut codes = Vec::new();
        if self.coverage_pct < cfg.min_breadth_coverage_pct {
            codes.push(reason::BREADTH_LOW_COVERAGE);
        }
        if (as_of - self.as_of_date).num_days() > cfg.breadth_stale_days {
            codes.push(reason::BREADTH_STALE);
        }
        (codes.is_empty(), codes)
    }

    /// `as_dict()`.
    #[must_use]
    pub fn as_dict(&self) -> PyDict {
        dict([
            ("as_of_date", PyValue::Str(iso(self.as_of_date))),
            ("pct_above_20dma", PyValue::Float(self.pct_above_20dma)),
            ("eligible_count", PyValue::Int(self.eligible_count)),
            ("observed_count", PyValue::Int(self.observed_count)),
            ("coverage_pct", PyValue::Float(self.coverage_pct)),
            ("universe_id", PyValue::Str(self.universe_id.clone())),
            ("universe_hash", PyValue::Str(self.universe_hash.clone())),
            ("audit_run_id", PyValue::Str(self.audit_run_id.clone())),
            (
                "calculation_version",
                PyValue::Str(self.calculation_version.clone()),
            ),
        ])
    }
}

/// `BookWeights`.
#[derive(Debug, Clone, PartialEq)]
pub struct BookWeights {
    /// `weights`, in insertion order.
    pub weights: Vec<(String, f64)>,
    /// `source`.
    pub source: WeightSource,
    /// `coverage_pct`.
    pub coverage_pct: f64,
    /// `reason_codes`.
    pub reason_codes: Vec<&'static str>,
}

impl BookWeights {
    /// `BookWeights(weights, source, coverage_pct)`.
    #[must_use]
    pub fn new(weights: &[(&str, f64)], source: WeightSource, coverage_pct: f64) -> Self {
        Self {
            weights: weights.iter().map(|(k, v)| ((*k).to_owned(), *v)).collect(),
            source,
            coverage_pct,
            reason_codes: Vec::new(),
        }
    }

    /// `weights.get(category, 0.0)`.
    #[must_use]
    pub fn weight(&self, category: &str) -> f64 {
        self.get(category).unwrap_or(0.0)
    }

    /// `weights.get(category)`.
    #[must_use]
    pub fn get(&self, category: &str) -> Option<f64> {
        self.weights
            .iter()
            .find(|(k, _)| k == category)
            .map(|(_, v)| *v)
    }

    /// `as_dict()`.
    #[must_use]
    pub fn as_dict(&self) -> PyDict {
        let mut sorted = self.weights.clone();
        sorted.sort_by(|a, b| a.0.cmp(&b.0));
        dict([
            ("weights", PyValue::Map(weights_map(&sorted))),
            ("source", PyValue::Str(self.source.value().to_owned())),
            ("coverage_pct", PyValue::Float(self.coverage_pct)),
            (
                "reason_codes",
                PyValue::List(
                    self.reason_codes
                        .iter()
                        .map(|c| PyValue::Str((*c).to_owned()))
                        .collect(),
                ),
            ),
        ])
    }
}

/// A weights list as a Python dict (insertion order kept).
#[must_use]
pub fn weights_map(weights: &[(String, f64)]) -> PyDict {
    let mut m = PyDict::new();
    for (k, v) in weights {
        m.insert(k.clone(), PyValue::Float(*v));
    }
    m
}

/// `TargetPosition`: a line of the hypothetical full-risk target book.
#[derive(Debug, Clone, PartialEq)]
pub struct TargetPosition {
    /// `symbol`.
    pub symbol: String,
    /// `category` (`None` if unclassified).
    pub category: Option<String>,
    /// `market_value`.
    pub market_value: f64,
}

impl TargetPosition {
    /// `TargetPosition(symbol, category, market_value)`.
    #[must_use]
    pub fn new(symbol: &str, category: Option<&str>, market_value: f64) -> Self {
        Self {
            symbol: symbol.to_owned(),
            category: category.map(str::to_owned),
            market_value,
        }
    }
}

/// `ExposureSnapshot`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ExposureSnapshot {
    /// `actual_equity_pct`.
    pub actual_equity_pct: f64,
    /// `pending_buy_pct`.
    pub pending_buy_pct: f64,
    /// `pending_sell_pct`.
    pub pending_sell_pct: f64,
    /// `execution_status`.
    pub execution_status: ExecutionStatus,
}

impl ExposureSnapshot {
    /// `ExposureSnapshot(actual_equity_pct=...)` with the defaults.
    #[must_use]
    pub const fn actual(actual_equity_pct: f64) -> Self {
        Self {
            actual_equity_pct,
            pending_buy_pct: 0.0,
            pending_sell_pct: 0.0,
            execution_status: ExecutionStatus::NotPlanned,
        }
    }
}

/// `PreviousRegime`.
#[derive(Debug, Clone, PartialEq)]
pub struct PreviousRegime {
    /// `policy_tier`.
    pub policy_tier: RegimeTier,
    /// `scheduled_week_end`.
    pub scheduled_week_end: NaiveDate,
    /// `last_transition_date`.
    pub last_transition_date: Option<NaiveDate>,
    /// `recovery_confirmations`.
    pub recovery_confirmations: i64,
    /// `book_weights`: the frozen snapshot for the week.
    pub book_weights: Option<BookWeights>,
}

impl PreviousRegime {
    /// `PreviousRegime(policy_tier, scheduled_week_end)` with the defaults.
    #[must_use]
    pub const fn new(policy_tier: RegimeTier, scheduled_week_end: NaiveDate) -> Self {
        Self {
            policy_tier,
            scheduled_week_end,
            last_transition_date: None,
            recovery_confirmations: 0,
            book_weights: None,
        }
    }
}

// =============================================================================================
// the state this engine produces
// =============================================================================================

/// `RegimeState`.
#[derive(Debug, Clone, PartialEq)]
#[allow(missing_docs)]
pub struct RegimeState {
    pub raw_candidate_tier: RegimeTier,
    pub previous_policy_tier: Option<RegimeTier>,
    pub policy_tier: RegimeTier,
    pub transition_limited: bool,
    pub target_equity_cap_pct: f64,
    pub actual_equity_pct: f64,
    pub exposure_gap_pct: f64,
    pub pending_sell_pct: f64,
    pub pending_buy_pct: f64,
    pub execution_status: ExecutionStatus,
    pub new_buys: NewBuyMode,
    pub forced_action: ForcedAction,
    pub reason_codes: Vec<String>,
    pub reasons: Vec<String>,
    pub as_of_date: NaiveDate,
    pub signal_session_date: NaiveDate,
    pub scheduled_week_end: NaiveDate,
    pub evaluation_id: String,
    pub breadth_pct: Option<f64>,
    pub breadth_coverage_pct: Option<f64>,
    pub override_active: bool,
    /// `book_category_weights`, in insertion order.
    pub book_category_weights: Vec<(String, f64)>,
    pub book_weight_source: WeightSource,
    /// `index_diagnostics` (name → `IndexSignals.as_dict()`), in insertion order.
    pub index_diagnostics: PyDict,
    pub last_transition_date: Option<NaiveDate>,
    pub next_evaluation_date: NaiveDate,
    pub data_stale: bool,
    pub manual_action_required: bool,
    pub algorithm_version: String,
    pub config_hash: String,
    /// `health`, in `ma_lengths` order.
    pub health: Vec<(i64, f64)>,
    pub bearish_stack_weight: f64,
}

impl RegimeState {
    /// `as_dict()`.
    #[must_use]
    pub fn as_dict(&self) -> PyDict {
        let date = |d: Option<NaiveDate>| d.map_or(PyValue::None, |d| PyValue::Str(iso(d)));
        let strs = |v: &[String]| PyValue::List(v.iter().cloned().map(PyValue::Str).collect());
        let mut weights = self.book_category_weights.clone();
        weights.sort_by(|a, b| a.0.cmp(&b.0));
        let mut health = self.health.clone();
        health.sort_by_key(|(k, _)| *k);
        let mut h = PyDict::new();
        for (k, v) in &health {
            h.insert(k.to_string(), PyValue::Float(*v));
        }
        dict([
            (
                "raw_candidate_tier",
                PyValue::Str(self.raw_candidate_tier.value().to_owned()),
            ),
            (
                "previous_policy_tier",
                self.previous_policy_tier
                    .map_or(PyValue::None, |t| PyValue::Str(t.value().to_owned())),
            ),
            (
                "policy_tier",
                PyValue::Str(self.policy_tier.value().to_owned()),
            ),
            ("transition_limited", PyValue::Bool(self.transition_limited)),
            (
                "target_equity_cap_pct",
                PyValue::Float(self.target_equity_cap_pct),
            ),
            ("actual_equity_pct", PyValue::Float(self.actual_equity_pct)),
            ("exposure_gap_pct", PyValue::Float(self.exposure_gap_pct)),
            ("pending_sell_pct", PyValue::Float(self.pending_sell_pct)),
            ("pending_buy_pct", PyValue::Float(self.pending_buy_pct)),
            (
                "execution_status",
                PyValue::Str(self.execution_status.value().to_owned()),
            ),
            ("new_buys", PyValue::Str(self.new_buys.value().to_owned())),
            (
                "forced_action",
                PyValue::Str(self.forced_action.value().to_owned()),
            ),
            ("reason_codes", strs(&self.reason_codes)),
            ("reasons", strs(&self.reasons)),
            ("as_of_date", PyValue::Str(iso(self.as_of_date))),
            (
                "signal_session_date",
                PyValue::Str(iso(self.signal_session_date)),
            ),
            (
                "scheduled_week_end",
                PyValue::Str(iso(self.scheduled_week_end)),
            ),
            ("evaluation_id", PyValue::Str(self.evaluation_id.clone())),
            ("breadth_pct", opt_f(self.breadth_pct)),
            ("breadth_coverage_pct", opt_f(self.breadth_coverage_pct)),
            ("override_active", PyValue::Bool(self.override_active)),
            ("book_category_weights", PyValue::Map(weights_map(&weights))),
            (
                "book_weight_source",
                PyValue::Str(self.book_weight_source.value().to_owned()),
            ),
            (
                "index_diagnostics",
                PyValue::Map(self.index_diagnostics.clone()),
            ),
            ("last_transition_date", date(self.last_transition_date)),
            (
                "next_evaluation_date",
                PyValue::Str(iso(self.next_evaluation_date)),
            ),
            ("data_stale", PyValue::Bool(self.data_stale)),
            (
                "manual_action_required",
                PyValue::Bool(self.manual_action_required),
            ),
            (
                "algorithm_version",
                PyValue::Str(self.algorithm_version.clone()),
            ),
            ("config_hash", PyValue::Str(self.config_hash.clone())),
            ("health", PyValue::Map(h)),
            (
                "bearish_stack_weight",
                PyValue::Float(self.bearish_stack_weight),
            ),
        ])
    }
}

// =============================================================================================
// moving averages + Schmitt-trigger hysteresis (the heart of the engine)
// =============================================================================================

/// `moving_average(closes, length)`: a running-sum simple MA aligned to `closes`, `None` until
/// `length` observations exist (the running sum's rounding included, as in Python).
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn moving_average(closes: &[f64], length: usize) -> Vec<Option<f64>> {
    let mut out = Vec::with_capacity(closes.len());
    let mut running = 0.0;
    for (i, c) in closes.iter().enumerate() {
        running += c;
        if i >= length {
            running -= closes[i - length];
        }
        out.push(if i + 1 >= length && length > 0 {
            Some(running / length as f64)
        } else {
            None
        });
    }
    out
}

/// `confirmed_state_series(closes, mas, buffer=, confirm_days=)`: per day, the confirmed state
/// and the consecutive closes on today's side. Each close is judged against its own day's MA;
/// inside the buffer the previous state holds; before any confirmation the state is UNKNOWN.
#[must_use]
pub fn confirmed_state_series(
    closes: &[f64],
    mas: &[Option<f64>],
    buffer: f64,
    confirm_days: i64,
) -> Vec<(SignalState, i64)> {
    let n = closes.len();
    let mut above = vec![false; n];
    let mut below = vec![false; n];
    for i in 0..n {
        let Some(ma) = mas.get(i).copied().flatten() else {
            continue;
        };
        above[i] = closes[i] > ma * (1.0 + buffer);
        below[i] = closes[i] < ma * (1.0 - buffer);
    }
    let mut out = Vec::with_capacity(n);
    let mut state = SignalState::Unknown;
    for i in 0..n {
        let idx = i64::try_from(i).unwrap_or(i64::MAX);
        if idx + 1 >= confirm_days {
            let start = usize::try_from(idx - confirm_days + 1).unwrap_or(0);
            let window = start..=i;
            if window.clone().all(|k| above[k]) {
                state = SignalState::Above;
            } else if window.clone().all(|k| below[k]) {
                state = SignalState::Below;
            }
        }
        let flags = if above[i] {
            Some(&above)
        } else if below[i] {
            Some(&below)
        } else {
            None
        };
        let run = flags.map_or(0, |f| {
            let mut run = 0;
            let mut k = i;
            loop {
                if !f[k] {
                    break;
                }
                run += 1;
                if k == 0 {
                    break;
                }
                k -= 1;
            }
            run
        });
        out.push((state, run));
    }
    out
}

/// `build_index_signals(index_name, candles, cfg, as_of=, instrument_token=)`: candles →
/// confirmed signals. Unfinished candles are dropped, never consumed.
///
/// # Errors
/// [`EngineError::Type`] for a final candle whose close is `NULL` (Python's arithmetic raises).
pub fn build_index_signals(
    index_name: &str,
    candles: &[Candle],
    cfg: &RegimeConfig,
    as_of: Option<NaiveDate>,
    instrument_token: Option<i64>,
) -> Result<IndexSignals, EngineError> {
    let mut sorted: Vec<&Candle> = candles.iter().collect();
    sorted.sort_by_key(|c| c.date);
    let finals: Vec<&Candle> = sorted.into_iter().filter(|c| c.is_final).collect();
    let Some(last) = finals.last() else {
        return Ok(IndexSignals {
            index_name: index_name.to_owned(),
            as_of_date: None,
            close: None,
            signals: BTreeMap::new(),
            is_stale: true,
            has_data: false,
            instrument_token,
        });
    };
    let closes: Vec<f64> = finals
        .iter()
        .map(|c| {
            c.close.ok_or_else(|| {
                EngineError::Type(
                    "unsupported operand type(s) for +=: 'float' and 'NoneType'".to_owned(),
                )
            })
        })
        .collect::<Result<_, _>>()?;
    let last_close = closes.last().copied().unwrap_or(0.0);
    let mut signals = BTreeMap::new();
    for &length in &cfg.ma_lengths {
        let mas = moving_average(&closes, usize::try_from(length).unwrap_or(0));
        let states = confirmed_state_series(&closes, &mas, cfg.buffer(), cfg.confirm_days);
        let ma_last = mas.last().copied().flatten();
        let (state, run) = states
            .last()
            .copied()
            .unwrap_or((SignalState::Unknown, 0));
        let distance_pct = match ma_last {
            Some(m) if !feq(m, 0.0) => Some((last_close / m - 1.0) * 100.0),
            _ => None,
        };
        signals.insert(
            length,
            MaSignal::new(length, last_close, ma_last, distance_pct, state, run),
        );
    }
    let reference = as_of.unwrap_or(last.date);
    let stale = (reference - last.date).num_days() > cfg.index_stale_days;
    Ok(IndexSignals {
        index_name: index_name.to_owned(),
        as_of_date: Some(last.date),
        close: Some(last_close),
        signals,
        is_stale: stale,
        has_data: true,
        instrument_token,
    })
}

// =============================================================================================
// book-composition weights
// =============================================================================================

fn normalise(weights: &[(String, f64)]) -> Result<Vec<(String, f64)>, EngineError> {
    let total = py_sum(weights.iter().map(|(_, v)| *v).filter(|v| *v > 0.0));
    if total <= 0.0 {
        let body: Vec<String> = weights
            .iter()
            .map(|(k, v)| format!("'{k}': {}", baskfy_core::py::float_repr(*v)))
            .collect();
        return Err(EngineError::Config(format!(
            "book weights are not normalizable: {{{}}}",
            body.join(", ")
        )));
    }
    Ok(weights
        .iter()
        .map(|(k, v)| (k.clone(), if *v > 0.0 { v / total } else { 0.0 }))
        .collect())
}

/// `resolve_book_weights(cfg, full_risk_target=, last_r1_snapshot=)`: the strategic book
/// composition in the spec's precedence order. `full_risk_target` must be the book the strategy
/// wants at **full** risk, never the regime-reduced holdings.
///
/// # Errors
/// [`EngineError::Config`] when the chosen weights cannot be normalised.
pub fn resolve_book_weights(
    cfg: &RegimeConfig,
    full_risk_target: Option<&[TargetPosition]>,
    last_r1_snapshot: Option<&BookWeights>,
) -> Result<BookWeights, EngineError> {
    let mut codes: Vec<&'static str> = Vec::new();
    if let Some(target) = full_risk_target.filter(|t| !t.is_empty()) {
        let total = py_sum(target.iter().map(|p| p.market_value.max(0.0)));
        if total > 0.0 {
            let mut known: Vec<(String, f64)> = cfg
                .structural_indices
                .iter()
                .map(|(c, _)| (c.clone(), 0.0))
                .collect();
            let mut classified = 0.0;
            for p in target {
                let v = p.market_value.max(0.0);
                if let Some(cat) = &p.category
                    && let Some(slot) = known.iter_mut().find(|(k, _)| k == cat)
                {
                    slot.1 += v;
                    classified += v;
                }
            }
            let coverage = classified / total * 100.0;
            if coverage >= cfg.min_classification_coverage_pct && classified > 0.0 {
                return Ok(BookWeights {
                    weights: normalise(&known)?,
                    source: WeightSource::FullRiskTarget,
                    coverage_pct: py_round(coverage, 2),
                    reason_codes: vec![reason::BOOK_WEIGHTS_FULL_RISK_TARGET],
                });
            }
            codes.push(reason::BOOK_CLASSIFICATION_COVERAGE_LOW);
        }
    }
    if let Some(snap) = last_r1_snapshot.filter(|s| !s.weights.is_empty()) {
        let mut rc = codes.clone();
        rc.push(reason::BOOK_WEIGHTS_LAST_R1_SNAPSHOT);
        return Ok(BookWeights {
            weights: normalise(&snap.weights)?,
            source: WeightSource::LastR1Snapshot,
            coverage_pct: snap.coverage_pct,
            reason_codes: rc,
        });
    }
    codes.push(reason::BOOK_WEIGHTS_CONFIG_DEFAULT);
    Ok(BookWeights {
        weights: normalise(&cfg.default_book_weights)?,
        source: WeightSource::ConfigDefault,
        coverage_pct: 0.0,
        reason_codes: codes,
    })
}

// =============================================================================================
// weighted health
// =============================================================================================

/// The index signals by name, in insertion order (Python's `Mapping[str, IndexSignals]`).
pub type SignalMap = Vec<(String, IndexSignals)>;

fn signal<'a>(map: &'a [(String, IndexSignals)], name: &str) -> Option<&'a IndexSignals> {
    map.iter().find(|(k, _)| k == name).map(|(_, v)| v)
}

/// `weighted_health(book, index_signals, cfg, ma_length)`: the book weight of every category
/// whose index is confirmed above the MA. UNKNOWN contributes zero, never bullish.
#[must_use]
pub fn weighted_health(
    book: &BookWeights,
    index_signals: &[(String, IndexSignals)],
    cfg: &RegimeConfig,
    ma_length: i64,
) -> f64 {
    let mut total = 0.0;
    for (category, index_name) in &cfg.structural_indices {
        let w = book.weight(category);
        if signal(index_signals, index_name).is_some_and(|s| s.is_above(ma_length)) {
            total += w;
        }
    }
    py_round(total, 10)
}

/// `optimistic_health(...)`: every degraded signal charitably read as ABOVE — used only to test
/// whether a risk-off escalation survives bad data.
#[must_use]
pub fn optimistic_health(
    book: &BookWeights,
    index_signals: &[(String, IndexSignals)],
    cfg: &RegimeConfig,
    ma_length: i64,
) -> f64 {
    let mut total = 0.0;
    for (category, index_name) in &cfg.structural_indices {
        let w = book.weight(category);
        match signal(index_signals, index_name) {
            None => total += w,
            Some(s) if !s.has_data || s.is_stale => total += w,
            Some(s)
                if matches!(
                    s.state(ma_length),
                    SignalState::Above | SignalState::Unknown
                ) =>
            {
                total += w;
            }
            Some(_) => {}
        }
    }
    py_round(total, 10)
}

/// `optimistic_sentinel(sentinel)`: a degraded sentinel imposes no floor.
#[must_use]
pub fn optimistic_sentinel(sentinel: Option<&IndexSignals>) -> Option<&IndexSignals> {
    sentinel.filter(|s| s.has_data && !s.is_stale)
}

/// `bearish_stack_weight(book, index_signals, cfg)`.
#[must_use]
pub fn bearish_stack_weight(
    book: &BookWeights,
    index_signals: &[(String, IndexSignals)],
    cfg: &RegimeConfig,
) -> f64 {
    let mut total = 0.0;
    for (category, index_name) in &cfg.structural_indices {
        if signal(index_signals, index_name).is_some_and(|s| s.bearish_stack(&cfg.ma_lengths)) {
            total += book.weight(category);
        }
    }
    py_round(total, 10)
}

fn h(health: &[(i64, f64)], m: i64) -> f64 {
    health
        .iter()
        .find(|(k, _)| *k == m)
        .map_or(0.0, |(_, v)| *v)
}

// =============================================================================================
// candidate tier classifier
// =============================================================================================

/// `classify_candidate(...)`: mutually exclusive rules, R4 → R3 → R1 → else R2. Breadth fires
/// only when usable, so an unusable reading can neither trigger risk-off nor satisfy R1.
#[must_use]
pub fn classify_candidate(
    health: &[(i64, f64)],
    stack_weight: f64,
    breadth_pct: Option<f64>,
    breadth_usable: bool,
    sentinel: Option<&IndexSignals>,
    cfg: &RegimeConfig,
) -> (RegimeTier, Vec<&'static str>) {
    let mut codes = Vec::new();
    let h50 = h(health, 50);
    let h200 = h(health, 200);
    let b = if breadth_usable { breadth_pct } else { None };
    let sentinel_below_200 = sentinel.is_some_and(|s| s.is_below(200));
    let sentinel_above_50 = sentinel.is_some_and(|s| s.is_above(50));

    if let Some(b) = b {
        if stack_weight >= cfg.r4_bearish_stack_min && b < cfg.r4_breadth_max {
            codes.push(reason::R4_BEARISH_STACK_AND_WEAK_BREADTH);
            return (RegimeTier::R4, codes);
        }
        if sentinel_below_200 && h200 < cfg.r4_h200_max && b < cfg.r4_breadth_max {
            codes.push(reason::R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH);
            return (RegimeTier::R4, codes);
        }
    }

    let mut r3 = false;
    if h200 <= cfg.r3_h200_max {
        codes.push(reason::R3_H200_WEAK);
        r3 = true;
    }
    if b.is_some_and(|b| b < cfg.r3_breadth_max) {
        codes.push(reason::R3_BREADTH_WEAK);
        r3 = true;
    }
    if sentinel_below_200 {
        codes.push(reason::R3_SENTINEL_BELOW_200DMA);
        r3 = true;
    }
    if r3 {
        return (RegimeTier::R3, codes);
    }

    let breadth_ok_for_r1 = if cfg.breadth_required {
        b.is_some_and(|b| b >= cfg.r1_breadth_min)
    } else {
        true
    };
    if h50 >= cfg.r1_h50_min && h200 >= cfg.r1_h200_min && breadth_ok_for_r1 && sentinel_above_50
    {
        codes.push(reason::R1_ALL_CONDITIONS_MET);
        return (RegimeTier::R1, codes);
    }
    codes.push(reason::R2_DEFAULT);
    (RegimeTier::R2, codes)
}

/// `apply_sentinel_floor(tier, sentinel)`: the sentinel only ever makes the tier more defensive.
#[must_use]
pub fn apply_sentinel_floor(
    tier: RegimeTier,
    sentinel: Option<&IndexSignals>,
) -> (RegimeTier, Vec<&'static str>) {
    let mut codes = Vec::new();
    let Some(s) = sentinel.filter(|s| s.has_data) else {
        codes.push(reason::SENTINEL_UNKNOWN);
        return (tier, codes);
    };
    let mut out = tier;
    if s.is_below(50) && out.ordinal() < RegimeTier::R2.ordinal() {
        out = RegimeTier::R2;
        codes.push(reason::SENTINEL_FLOOR_R2);
    }
    if s.is_below(200) && out.ordinal() < RegimeTier::R3.ordinal() {
        out = RegimeTier::R3;
        codes.push(reason::SENTINEL_FLOOR_R3);
    }
    if s.is_below(50) {
        codes.push(reason::SENTINEL_BELOW_50DMA_VETO);
    }
    (out, codes)
}

/// `apply_recovery_override(...)`: lift an R3/R4 candidate to no better than R2.
#[must_use]
pub fn apply_recovery_override(
    tier: RegimeTier,
    health: &[(i64, f64)],
    breadth_pct: Option<f64>,
    breadth_usable: bool,
    confirmations: i64,
    cfg: &RegimeConfig,
) -> (RegimeTier, bool, Vec<&'static str>) {
    let mut codes = Vec::new();
    if tier.ordinal() < RegimeTier::R3.ordinal() {
        return (tier, false, codes);
    }
    let Some(b) = breadth_pct.filter(|_| breadth_usable) else {
        codes.push(reason::RECOVERY_OVERRIDE_BREADTH_UNUSABLE);
        return (tier, false, codes);
    };
    if b < cfg.recovery_breadth_min {
        return (tier, false, codes);
    }
    if h(health, 20) < cfg.recovery_h20_min {
        codes.push(reason::RECOVERY_OVERRIDE_H20_TOO_WEAK);
        return (tier, false, codes);
    }
    if confirmations < cfg.recovery_confirm_evaluations {
        codes.push(reason::RECOVERY_OVERRIDE_UNCONFIRMED);
        return (tier, false, codes);
    }
    codes.push(reason::RECOVERY_OVERRIDE_APPLIED);
    (RegimeTier::R2, true, codes)
}

// =============================================================================================
// transitions
// =============================================================================================

/// `apply_transition(previous, candidate, cfg)`: risk-off is immediate, re-risking is one
/// ordinal tier per weekly evaluation.
#[must_use]
pub fn apply_transition(
    previous: Option<RegimeTier>,
    candidate: RegimeTier,
) -> (RegimeTier, bool, Vec<&'static str>) {
    let Some(previous) = previous else {
        return (candidate, false, vec![reason::BOOTSTRAP_NO_PRIOR_STATE]);
    };
    if candidate.ordinal() > previous.ordinal() {
        return (candidate, false, vec![reason::TRANSITION_RISK_OFF_DIRECT]);
    }
    if candidate.ordinal() < previous.ordinal() {
        let stepped = RegimeTier::from_ordinal(previous.ordinal() - 1);
        let limited = stepped != candidate;
        return (
            stepped,
            limited,
            if limited {
                vec![reason::TRANSITION_RERISK_CAPPED]
            } else {
                Vec::new()
            },
        );
    }
    (previous, false, vec![reason::TRANSITION_NONE])
}

/// `next_evaluation_date(after, cfg)`: the next date strictly after `after` on the evaluation
/// weekday.
#[must_use]
pub fn next_evaluation_date(after: NaiveDate, cfg: &RegimeConfig) -> NaiveDate {
    let weekday = i64::from(after.weekday().num_days_from_monday());
    let delta = (cfg.weekly_evaluation_weekday - weekday).rem_euclid(7);
    after + Duration::days(if delta == 0 { 7 } else { delta })
}

/// `make_evaluation_id(scheduled_week_end, signal_session_date, cfg)`: deterministic — the same
/// week, session and configuration always address the same decision.
#[must_use]
pub fn make_evaluation_id(
    scheduled_week_end: NaiveDate,
    signal_session_date: NaiveDate,
    cfg: &RegimeConfig,
) -> String {
    let blob = [
        iso(scheduled_week_end),
        iso(signal_session_date),
        cfg.algorithm_version.clone(),
        cfg.config_hash(),
    ]
    .join("|");
    let digest = sha2::Sha256::digest(blob.as_bytes());
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    hex[..24].to_owned()
}

// =============================================================================================
// new buys + forced action
// =============================================================================================

/// `resolve_new_buys(tier, sentinel=, data_ok=)`.
#[must_use]
pub fn resolve_new_buys(
    tier: RegimeTier,
    sentinel: Option<&IndexSignals>,
    data_ok: bool,
) -> (NewBuyMode, Vec<&'static str>) {
    if !data_ok {
        return (NewBuyMode::Blocked, vec![reason::NEW_BUYS_BLOCKED_DATA]);
    }
    if sentinel.is_some_and(|s| s.is_below(50)) {
        return (NewBuyMode::Blocked, vec![reason::NEW_BUYS_BLOCKED_SENTINEL]);
    }
    match tier {
        RegimeTier::R1 => (NewBuyMode::Full, vec![reason::NEW_BUYS_FULL]),
        RegimeTier::R2 => (NewBuyMode::Half, vec![reason::NEW_BUYS_HALF]),
        _ => (NewBuyMode::Blocked, vec![reason::NEW_BUYS_BLOCKED_TIER]),
    }
}

/// `resolve_forced_action(tier, gap_pct, cfg, bootstrap_suppressed=)`: driven by the exposure
/// **gap**, not the transition date, so a failed or partial plan is retried without a new
/// transition.
#[must_use]
pub fn resolve_forced_action(
    tier: RegimeTier,
    gap_pct: f64,
    cfg: &RegimeConfig,
    bootstrap_suppressed: bool,
) -> (ForcedAction, Vec<&'static str>) {
    if gap_pct <= cfg.exposure_tolerance_pct {
        return (ForcedAction::None, vec![reason::EXPOSURE_WITHIN_CAP]);
    }
    if bootstrap_suppressed {
        return (
            ForcedAction::None,
            vec![reason::FORCED_SELL_SUPPRESSED_BOOTSTRAP],
        );
    }
    let codes = vec![reason::EXPOSURE_GAP_PENDING];
    match tier {
        RegimeTier::R4 => (ForcedAction::CrashReduce, codes),
        RegimeTier::R3 => (ForcedAction::ReduceToCap, codes),
        RegimeTier::R2 => (ForcedAction::TrimToCap, codes),
        RegimeTier::R1 => (ForcedAction::None, codes),
    }
}

// =============================================================================================
// the single entry point
// =============================================================================================

/// `evaluate(...)`'s inputs.
#[derive(Debug, Clone)]
pub struct EvaluateInput<'a> {
    /// `as_of_date`.
    pub as_of_date: NaiveDate,
    /// `scheduled_week_end`.
    pub scheduled_week_end: NaiveDate,
    /// `signal_session_date`.
    pub signal_session_date: NaiveDate,
    /// `index_signals`.
    pub index_signals: &'a [(String, IndexSignals)],
    /// `sentinel`.
    pub sentinel: Option<&'a IndexSignals>,
    /// `breadth`.
    pub breadth: Option<&'a BreadthReading>,
    /// `book`.
    pub book: &'a BookWeights,
    /// `exposure`.
    pub exposure: ExposureSnapshot,
    /// `cfg`.
    pub cfg: &'a RegimeConfig,
    /// `previous`.
    pub previous: Option<&'a PreviousRegime>,
}

/// `evaluate(...)`: the pure regime evaluation. Identical inputs always yield an identical state.
#[must_use]
#[allow(clippy::too_many_lines)]
pub fn evaluate(input: &EvaluateInput<'_>) -> RegimeState {
    let cfg = input.cfg;
    let sentinel = input.sentinel;
    let mut codes: Vec<&'static str> = Vec::new();

    let required: Vec<&String> = cfg.structural_indices.iter().map(|(_, v)| v).collect();
    let present = |n: &str| signal(input.index_signals, n);
    let missing = required
        .iter()
        .any(|n| present(n).is_none_or(|s| !s.has_data));
    let stale = required
        .iter()
        .any(|n| present(n).is_some_and(|s| s.is_stale));
    let unknown = required
        .iter()
        .any(|n| present(n).is_some_and(IndexSignals::any_unknown));

    if missing {
        codes.push(reason::DATA_INDEX_MISSING);
    }
    if stale || sentinel.is_some_and(|s| s.is_stale) {
        codes.push(reason::DATA_STALE);
    }
    if unknown || sentinel.is_some_and(IndexSignals::any_unknown) {
        codes.push(reason::SIGNAL_UNKNOWN);
    }
    let data_stale = missing || stale;
    let mut data_ok = !(missing || stale || unknown);

    let mut breadth_usable = false;
    let breadth_pct = input.breadth.map(|b| b.pct_above_20dma);
    let breadth_cov = input.breadth.map(|b| b.coverage_pct);
    if !cfg.breadth_required {
        codes.push(reason::BREADTH_NOT_USED);
    } else if let Some(b) = input.breadth {
        let (usable, bcodes) = b.is_usable(cfg, input.as_of_date);
        breadth_usable = usable;
        codes.extend(bcodes);
    } else {
        codes.push(reason::BREADTH_MISSING);
    }
    if cfg.breadth_required && !breadth_usable {
        data_ok = false;
    }

    codes.extend(input.book.reason_codes.iter().copied());

    let health: Vec<(i64, f64)> = cfg
        .ma_lengths
        .iter()
        .map(|m| (*m, weighted_health(input.book, input.index_signals, cfg, *m)))
        .collect();
    let stack_w = bearish_stack_weight(input.book, input.index_signals, cfg);

    let (candidate, ccodes) = classify_candidate(
        &health,
        stack_w,
        breadth_pct,
        breadth_usable,
        sentinel,
        cfg,
    );
    codes.extend(ccodes);
    let (candidate, scodes) = apply_sentinel_floor(candidate, sentinel);
    codes.extend(scodes);
    let confirmations = input.previous.map_or(0, |p| p.recovery_confirmations);
    let (candidate, override_active, rcodes) = apply_recovery_override(
        candidate,
        &health,
        breadth_pct,
        breadth_usable,
        confirmations,
        cfg,
    );
    codes.extend(rcodes);

    let mut raw_candidate = candidate;
    if unknown && raw_candidate == RegimeTier::R1 {
        raw_candidate = RegimeTier::R2;
    }

    let prev_tier = input.previous.map(|p| p.policy_tier);
    let degraded = missing || stale || unknown || (cfg.breadth_required && !breadth_usable);

    let (policy_tier, transition_limited) = match prev_tier {
        Some(prev) if degraded => {
            let health_opt: Vec<(i64, f64)> = cfg
                .ma_lengths
                .iter()
                .map(|m| {
                    (
                        *m,
                        optimistic_health(input.book, input.index_signals, cfg, *m),
                    )
                })
                .collect();
            let sent_opt = optimistic_sentinel(sentinel);
            let (cand_opt, codes_opt) = classify_candidate(
                &health_opt,
                stack_w,
                breadth_pct,
                breadth_usable,
                sent_opt,
                cfg,
            );
            let (cand_opt, floor_opt) = apply_sentinel_floor(cand_opt, sent_opt);
            let justified = codes_opt
                .iter()
                .chain(floor_opt.iter())
                .any(|c| is_affirmative_risk_off(c))
                && cand_opt.ordinal() > prev.ordinal();
            if justified {
                codes.push(reason::TRANSITION_RISK_OFF_DIRECT);
                (cand_opt, false)
            } else {
                codes.push(reason::TRANSITION_HELD_STALE_DATA);
                (prev, false)
            }
        }
        _ => {
            let (tier, limited, tcodes) = apply_transition(prev_tier, raw_candidate);
            codes.extend(tcodes);
            (tier, limited)
        }
    };

    let bootstrap_suppressed =
        prev_tier.is_none() && cfg.bootstrap_policy == BOOTSTRAP_OBSERVE_ONLY;
    if bootstrap_suppressed {
        codes.push(reason::BOOTSTRAP_OBSERVE_ONLY);
    }

    let cap = cfg.cap_for(policy_tier.value()).unwrap_or(0.0);
    let gap = py_round(input.exposure.actual_equity_pct - cap, 6);
    if matches!(
        input.exposure.execution_status,
        ExecutionStatus::Partial | ExecutionStatus::Failed
    ) {
        codes.push(reason::EXPOSURE_RECONCILE_RETRY);
    }
    let (new_buys, nbcodes) = resolve_new_buys(policy_tier, sentinel, data_ok);
    codes.extend(nbcodes);
    let (forced, fcodes) = resolve_forced_action(policy_tier, gap, cfg, bootstrap_suppressed);
    codes.extend(fcodes);
    let manual = bootstrap_suppressed && gap > cfg.exposure_tolerance_pct;

    let transitioned = prev_tier.is_some_and(|p| p != policy_tier);
    let last_transition = if transitioned {
        Some(input.signal_session_date)
    } else {
        input.previous.and_then(|p| p.last_transition_date)
    };

    let mut ordered: Vec<String> = Vec::with_capacity(codes.len());
    for c in codes {
        if !ordered.iter().any(|x| x == c) {
            ordered.push(c.to_owned());
        }
    }

    let mut sorted_signals: Vec<&(String, IndexSignals)> = input.index_signals.iter().collect();
    sorted_signals.sort_by(|a, b| a.0.cmp(&b.0));
    let mut diagnostics = PyDict::new();
    for (n, s) in sorted_signals {
        diagnostics.insert(n.clone(), PyValue::Map(s.as_dict()));
    }
    if let Some(s) = sentinel {
        diagnostics.insert(s.index_name.clone(), PyValue::Map(s.as_dict()));
    }

    RegimeState {
        raw_candidate_tier: raw_candidate,
        previous_policy_tier: prev_tier,
        policy_tier,
        transition_limited,
        target_equity_cap_pct: cap,
        actual_equity_pct: input.exposure.actual_equity_pct,
        exposure_gap_pct: gap,
        pending_sell_pct: input.exposure.pending_sell_pct,
        pending_buy_pct: input.exposure.pending_buy_pct,
        execution_status: input.exposure.execution_status,
        new_buys,
        forced_action: forced,
        reasons: describe(&ordered),
        reason_codes: ordered,
        as_of_date: input.as_of_date,
        signal_session_date: input.signal_session_date,
        scheduled_week_end: input.scheduled_week_end,
        evaluation_id: make_evaluation_id(
            input.scheduled_week_end,
            input.signal_session_date,
            cfg,
        ),
        breadth_pct,
        breadth_coverage_pct: breadth_cov,
        override_active,
        book_category_weights: input.book.weights.clone(),
        book_weight_source: input.book.source,
        index_diagnostics: diagnostics,
        last_transition_date: last_transition,
        next_evaluation_date: next_evaluation_date(input.scheduled_week_end, cfg),
        data_stale,
        manual_action_required: manual,
        algorithm_version: cfg.algorithm_version.clone(),
        config_hash: cfg.config_hash(),
        health,
        bearish_stack_weight: stack_w,
    }
}
