//! `app/core/regime.py`: a re-export of `baskfy_core::exposure::regime`, plus the desk's adapters
//! from its own config, candle and breadth types onto the core's.

use chrono::NaiveDate;
use serde::Serialize;

pub use baskfy_core::exposure::regime::{
    AFFIRMATIVE_RISK_OFF, ALGORITHM_VERSION, BookWeights, BootstrapPolicy, BreadthReading,
    EvaluateArgs, ExecutionStatus, ExposureSnapshot, ForcedAction, IndexSignals, MaSignal,
    NewBuyMode, PreviousRegime, RegimeConfig as CoreConfig, RegimeMode, RegimeState, RegimeTier,
    SignalState, TargetPosition, WeightSource, apply_recovery_override, apply_sentinel_floor,
    apply_transition, bearish_stack_weight, classify_candidate, confirmed_state_series, evaluate,
    make_evaluation_id, moving_average, next_evaluation_date, optimistic_health,
    optimistic_sentinel, reason, resolve_book_weights, resolve_forced_action, resolve_new_buys,
    weighted_health,
};
pub use baskfy_core::exposure::{ExposureError, StrMap};

use crate::lanes::analytics::breadth::BreadthReading as DeskBreadth;
pub use crate::lanes::analytics::index_cache::Candle;
use crate::lanes::weekly::pandas::Num;
use crate::wire::{PyDict, PyValue};

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

/// What the desk's adapters refuse (a Python exception on the same input).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum EngineError {
    /// `ConfigError` and every other exception the core raises.
    #[error("{0}")]
    Config(String),
    /// A value Python's arithmetic raises on (a stored `NULL` close).
    #[error("TypeError: {0}")]
    Type(String),
}

impl From<super::ConfigError> for EngineError {
    fn from(e: super::ConfigError) -> Self {
        Self::Config(e.to_string())
    }
}

impl From<ExposureError> for EngineError {
    fn from(e: ExposureError) -> Self {
        Self::Config(e.to_string())
    }
}

/// The desk's candle (a stored close may be `NULL`) as the core's; Python's arithmetic raises on
/// a `None` close only for a **final** session, the only kind the engine reads.
///
/// # Errors
/// [`EngineError::Type`] for a final candle whose close is `NULL`.
pub fn core_candles(
    candles: &[Candle],
) -> Result<Vec<baskfy_core::exposure::regime::Candle>, EngineError> {
    candles
        .iter()
        .filter_map(|c| match (c.close, c.is_final) {
            (Some(close), _) => Some(Ok(baskfy_core::exposure::regime::Candle {
                date: c.date,
                open: c.open,
                high: c.high,
                low: c.low,
                close,
                is_final: c.is_final,
            })),
            (None, false) => None,
            (None, true) => Some(Err(EngineError::Type(
                "unsupported operand type(s) for +=: 'float' and 'NoneType'".to_owned(),
            ))),
        })
        .collect()
}

/// `build_index_signals(index_name, candles, cfg, as_of=, instrument_token=)` over the desk's
/// candles and config, through the core engine. Unfinished candles are dropped, never consumed.
///
/// # Errors
/// [`EngineError::Type`] for a final `NULL` close; [`EngineError::Config`] for a config the core
/// refuses.
pub fn build_index_signals(
    index_name: &str,
    candles: &[Candle],
    cfg: &super::RegimeConfig,
    as_of: Option<NaiveDate>,
    instrument_token: Option<i64>,
) -> Result<IndexSignals, EngineError> {
    let core = cfg.to_core()?;
    let candles = core_candles(candles)?;
    Ok(baskfy_core::exposure::regime::build_index_signals(
        index_name,
        &candles,
        &core,
        as_of,
        instrument_token,
    )?)
}

/// The breadth lane's reading as the core's (the same nine fields).
#[must_use]
pub fn core_breadth(b: &DeskBreadth) -> BreadthReading {
    BreadthReading {
        as_of_date: b.as_of_date,
        pct_above_20dma: b.pct_above_20dma,
        eligible_count: b.eligible_count,
        observed_count: b.observed_count,
        coverage_pct: b.coverage_pct,
        universe_id: b.universe_id.clone(),
        universe_hash: b.universe_hash.clone(),
        audit_run_id: b.audit_run_id.clone(),
        calculation_version: b.calculation_version.clone(),
    }
}

/// Any core value through its `as_dict()` serialisation, as the desk's wire value.
#[must_use]
pub fn to_py<T: Serialize>(value: &T) -> PyValue {
    serde_json::to_vec(value)
        .ok()
        .and_then(|b| PyValue::from_json(&b).ok())
        .unwrap_or(PyValue::None)
}

/// `dict(weights)` as a wire map, in insertion order.
#[must_use]
pub fn weights_map(weights: &StrMap<f64>) -> PyDict {
    let mut m = PyDict::new();
    for (k, v) in weights.iter() {
        m.insert(k.to_owned(), PyValue::Float(*v));
    }
    m
}

/// `RegimeState.as_dict()`.
#[must_use]
pub fn state_dict(state: &RegimeState) -> PyDict {
    match to_py(state) {
        PyValue::Map(m) => m,
        _ => PyDict::new(),
    }
}
