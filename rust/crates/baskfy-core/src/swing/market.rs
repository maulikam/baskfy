//! Port of `baskfy_core.swing.market` (`decile-blueprint/packages/core/src/baskfy_core/swing/market.py`).
//!
//! Breadth, the market gate, and progressive exposure (docs/swing/04 §8).
//!
//! Goldens: `L1/swing/exposure_tier.*` (`baskfy_core.swing.market.exposure_tier`).
//!
//! Ported so far: [`MarketGate`], [`ExposureTier`], [`drawdown_locked`] and
//! [`exposure_tier`] — the rung for tomorrow. The breadth snapshot, the gate itself, the index
//! EMAs and the intraday rung are not yet ported.

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};

/// Ports `baskfy_core.swing.config.MarketConfig` — breadth, the gate, and progressive
/// exposure (docs/swing/04 §8). It lives here until `swing::config` is ported (that file is
/// outside this port's ownership); missing keys take Python's defaults.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct MarketConfig {
    /// His gauge: share of the universe up at least this much over the last month.
    pub strong_move_pct: f64,
    /// Breadth at or above this is GREEN (with the index's blessing).
    pub green_min_pct_up: f64,
    /// Breadth at or below this is RED.
    pub red_max_pct_up: f64,
    /// The index's fast EMA length.
    pub index_ma_fast: i64,
    /// The index's slow EMA length.
    pub index_ma_slow: i64,
    /// Tiers as (max open positions, max sleeve exposure %), lowest first.
    pub tiers: Vec<(i64, f64)>,
    /// Step up after this many closed trades with positive net R.
    pub lookback_trades: i64,
    /// Step down on this loss streak.
    pub step_down_loss_streak: i64,
    /// Lock out new entries this far below the peak.
    pub max_drawdown_pct: f64,
    /// ...until the drawdown is back inside this.
    pub resume_drawdown_pct: f64,
}

impl Default for MarketConfig {
    fn default() -> Self {
        Self {
            strong_move_pct: 25.0,
            green_min_pct_up: 5.0,
            red_max_pct_up: 2.0,
            index_ma_fast: 10,
            index_ma_slow: 20,
            tiers: vec![(2, 25.0), (4, 50.0), (6, 75.0), (10, 100.0)],
            lookback_trades: 5,
            step_down_loss_streak: 3,
            max_drawdown_pct: 15.0,
            resume_drawdown_pct: 10.0,
        }
    }
}

/// GREEN: press. AMBER: new entries at reduced size. RED: no new entries, manage exits.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum MarketGate {
    /// Press.
    Green,
    /// New entries at reduced size.
    Amber,
    /// No new entries, manage exits.
    Red,
}

/// How much the book may carry. `level` is the rung, 0 lowest.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExposureTier {
    /// The rung, 0 lowest.
    pub level: i64,
    /// The most positions open at once on this rung.
    pub max_open_positions: i64,
    /// The most of the sleeve deployed on this rung, in percent.
    pub max_exposure_pct: f64,
    /// Whether the book may open anything new tomorrow.
    pub new_entries_allowed: bool,
    /// True while the sleeve's drawdown lock-out is in force (`max_drawdown_pct`).
    pub drawdown_locked: bool,
}

/// The lock-out state for today from the sleeve's drawdown and yesterday's state.
///
/// Locks at `max_drawdown_pct` below the peak; stays locked until the drawdown is back inside
/// `resume_drawdown_pct`. Hysteresis, so a sleeve oscillating around 15% does not flap.
pub fn drawdown_locked(drawdown_pct: f64, was_locked: bool, config: &MarketConfig) -> bool {
    if drawdown_pct >= config.max_drawdown_pct {
        return true;
    }
    was_locked && drawdown_pct > config.resume_drawdown_pct
}

/// Python's `len(trailing run of values < 0)`, read from the end.
fn loss_streak(recent: &[Decimal]) -> usize {
    recent
        .iter()
        .rev()
        .take_while(|value| **value < Decimal::ZERO)
        .count()
}

/// Python's `seq[-n:]` for any integer `n` (including `0`, which is the whole sequence, and a
/// negative `n`, which is `seq[|n|:]`).
fn python_tail(values: &[Decimal], n: i64) -> CoreResult<&[Decimal]> {
    let len = i64::try_from(values.len())
        .map_err(|_| CoreError::InvalidInput("too many closed trades".to_owned()))?;
    let start = n
        .checked_neg()
        .ok_or_else(|| CoreError::InvalidInput(format!("lookback_trades {n} out of range")))?;
    let start = if start < 0 {
        (len + start).max(0)
    } else {
        start.min(len)
    };
    let start = usize::try_from(start)
        .map_err(|_| CoreError::InvalidInput(format!("lookback_trades {n} out of range")))?;
    Ok(values.get(start..).unwrap_or(&[]))
}

fn as_i64(value: usize, what: &str) -> CoreResult<i64> {
    i64::try_from(value).map_err(|_| CoreError::InvalidInput(format!("{what} out of range")))
}

/// `config.tiers[level]` as `(max_open_positions, max_exposure_pct)`.
fn tier(config: &MarketConfig, level: i64) -> CoreResult<(i64, f64)> {
    let index = usize::try_from(level)
        .map_err(|_| CoreError::InvalidInput(format!("no exposure tier {level}")))?;
    let (positions, exposure) = config
        .tiers
        .get(index)
        .ok_or_else(|| CoreError::InvalidInput(format!("no exposure tier {level}")))?;
    Ok((*positions, *exposure))
}

/// The rung for tomorrow, from today's rung, the last closed trades, the gate and the sleeve's
/// drawdown.
///
/// Rules, in precedence order:
///
/// 1. Drawdown lock-out ([`drawdown_locked`]): rung 0, no new entries, until recovered.
/// 2. RED: drop to rung 0 and allow no new entries, whatever the results say.
/// 3. A loss streak of `step_down_loss_streak` closed trades: one rung down.
/// 4. The last `lookback_trades` closed trades net positive R, and the gate GREEN: one rung
///    up. AMBER holds the rung.
/// 5. Otherwise hold.
///
/// `drawdown_pct` and `was_drawdown_locked` default to `0.0` and `false` in Python.
///
/// # Errors
///
/// [`CoreError::InvalidInput`] when `config.tiers` is empty (Python's `IndexError`) or a
/// config integer does not fit.
pub fn exposure_tier(
    current_level: i64,
    closed_r_multiples: &[Decimal],
    gate: MarketGate,
    config: &MarketConfig,
    drawdown_pct: f64,
    was_drawdown_locked: bool,
) -> CoreResult<ExposureTier> {
    let count = as_i64(config.tiers.len(), "tier count")?;
    if count == 0 {
        return Err(CoreError::InvalidInput(
            "config.tiers is empty: no exposure rung".to_owned(),
        ));
    }
    let top = count - 1;
    let mut level = current_level.max(0).min(top);
    let locked = drawdown_locked(drawdown_pct, was_drawdown_locked, config);
    if locked || gate == MarketGate::Red {
        let (positions, exposure) = tier(config, 0)?;
        return Ok(ExposureTier {
            level: 0,
            max_open_positions: positions,
            max_exposure_pct: exposure,
            new_entries_allowed: false,
            drawdown_locked: locked,
        });
    }

    let lookback = config.lookback_trades;
    let streak_needed = config.step_down_loss_streak;
    let recent = python_tail(closed_r_multiples, lookback)?;
    let streak = as_i64(loss_streak(recent), "loss streak")?;
    if streak >= streak_needed {
        level = (level - 1).max(0);
    } else if gate == MarketGate::Green && as_i64(recent.len(), "recent trades")? >= lookback {
        let mut total = Decimal::ZERO;
        for value in recent {
            total = total
                .checked_add(*value)
                .ok_or_else(|| CoreError::Arithmetic("sum of R multiples overflowed".into()))?;
        }
        if total > Decimal::ZERO {
            level = (level + 1).min(top);
        }
    }
    let (positions, exposure) = tier(config, level)?;
    Ok(ExposureTier {
        level,
        max_open_positions: positions,
        max_exposure_pct: exposure,
        new_entries_allowed: true,
        drawdown_locked: false,
    })
}
