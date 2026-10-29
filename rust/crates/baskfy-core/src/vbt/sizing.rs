//! Port of `baskfy_core.vbt.sizing` (`decile-blueprint/packages/core/src/baskfy_core/vbt/sizing.py`).
//!
//! Ten equal slots, and the cap that bound is named (`docs/vbt/04` §5). Money is `Decimal`
//! (house rule 9). The share count is the floor of the smallest of four budgets, and **which one
//! bound is part of the answer**.
//!
//! The budgets are computed in [`WideDecimal`] — Python's `Decimal` in its default context
//! (28 significant digits, half-even) — so a slot of `987654.32 / 10` and its floor division by
//! the limit round exactly where Python's do (DECISIONS-RS R1.3). A config float enters as
//! Python's `Decimal(str(x))`; an infinite one behaves as `Decimal('Infinity')` does.
//!
//! Goldens: `sizing.size_entry`, `sizing.first_live_multiplier`.

use std::cmp::Ordering;
use std::str::FromStr;

use rust_decimal::{Decimal, RoundingStrategy};
use serde::{Deserialize, Serialize};

use crate::backtest::helpers::{Rounding, WideDecimal};
use crate::error::{CoreError, CoreResult};
use crate::options::costs::{mul, quantize, sub};
use crate::vbt::config::SizingConfig;
use crate::vbt::exits::wide;

/// Ports `baskfy_core.vbt.sizing.SizeCap` — which budget decided the quantity.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SizeCap {
    /// Equity / `max_slots` — the ordinary answer.
    #[serde(rename = "SLOT")]
    Slot,
    /// `max_position_pct` of equity.
    #[serde(rename = "POSITION_PCT")]
    PositionPct,
    /// The cash the sleeve actually has.
    #[serde(rename = "CASH")]
    Cash,
    /// 1% of the name's 20-day average turnover.
    #[serde(rename = "TURNOVER")]
    Turnover,
}

/// Ports `baskfy_core.vbt.sizing.SizeRefusal` — why no position at all.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SizeRefusal {
    /// Equity is zero or negative.
    #[serde(rename = "NO_SLEEVE_CAPITAL")]
    NoSleeveCapital,
    /// The limit is not positive, or the stop is not below it.
    #[serde(rename = "STOP_NOT_BELOW_ENTRY")]
    StopNotBelowEntry,
    /// The binding budget buys less than the minimum trade value.
    #[serde(rename = "BELOW_MIN_TRADE_VALUE")]
    BelowMinTradeValue,
    /// The turnover ceiling cannot fund the minimum trade value.
    #[serde(rename = "TURNOVER_CAP")]
    TurnoverCap,
}

/// Ports `baskfy_core.vbt.sizing.SizedEntry` — one sized limit order. `quantity == 0` always
/// carries a `refusal`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SizedEntry {
    /// Shares.
    pub quantity: i64,
    /// The limit (the signal bar's close).
    pub limit_price: Decimal,
    /// The stop.
    pub stop_price: Decimal,
    /// `limit x quantity`, to the paisa.
    pub value_inr: Decimal,
    /// `value / equity x 100`, two places.
    pub position_pct: Decimal,
    /// `(limit - stop) x quantity`, to the paisa.
    pub risk_inr: Decimal,
    /// The budget that bound.
    pub cap: Option<SizeCap>,
    /// Why nothing was sized.
    pub refusal: Option<SizeRefusal>,
}

impl SizedEntry {
    /// Ports the `placed` property.
    #[must_use]
    pub fn placed(&self) -> bool {
        self.quantity > 0
    }
}

fn refuse(limit: Decimal, stop: Decimal, why: SizeRefusal) -> SizedEntry {
    SizedEntry {
        quantity: 0,
        limit_price: limit,
        stop_price: stop,
        value_inr: Decimal::ZERO,
        position_pct: Decimal::ZERO,
        risk_inr: Decimal::ZERO,
        cap: None,
        refusal: Some(why),
    }
}

/// A Python `Decimal` that may be infinite (`Decimal(str(inf))`).
#[derive(Debug, Clone)]
enum PyDec {
    Finite(WideDecimal),
    Inf { negative: bool },
}

impl PyDec {
    fn from_float(x: f64) -> CoreResult<Self> {
        if x.is_nan() {
            return Err(CoreError::InvalidInput(
                "NaN is not a usable config number".to_owned(),
            ));
        }
        if x.is_infinite() {
            return Ok(Self::Inf { negative: x < 0.0 });
        }
        WideDecimal::from_str(&format!("{x}"))
            .map(Self::Finite)
            .map_err(|e| CoreError::InvalidInput(e.to_string()))
    }

    /// `self * other` (finite `other`): `0 x inf` is Python's `InvalidOperation`.
    fn mul(&self, other: &WideDecimal) -> CoreResult<Self> {
        match self {
            Self::Finite(a) => Ok(Self::Finite(a.mul(other))),
            Self::Inf { negative } => {
                if other.is_zero() {
                    return Err(CoreError::Arithmetic(
                        "[<class 'decimal.InvalidOperation'>] 0 * Infinity".to_owned(),
                    ));
                }
                Ok(Self::Inf {
                    negative: *negative != other.is_sign_negative(),
                })
            }
        }
    }

    /// `self / other` (finite, non-zero `other`).
    fn div(&self, other: &WideDecimal) -> CoreResult<Self> {
        match self {
            Self::Finite(a) => a
                .div(other)
                .map(Self::Finite)
                .map_err(|e| CoreError::Arithmetic(e.to_string())),
            Self::Inf { negative } => Ok(Self::Inf {
                negative: *negative != other.is_sign_negative(),
            }),
        }
    }

    fn cmp(&self, other: &Self) -> Ordering {
        match (self, other) {
            (Self::Finite(a), Self::Finite(b)) => a.cmp(b),
            (Self::Inf { negative: a }, Self::Inf { negative: b }) => b.cmp(a),
            (Self::Inf { negative }, Self::Finite(_)) => {
                if *negative {
                    Ordering::Less
                } else {
                    Ordering::Greater
                }
            }
            (Self::Finite(_), Self::Inf { negative }) => {
                if *negative {
                    Ordering::Greater
                } else {
                    Ordering::Less
                }
            }
        }
    }
}

fn wide_err(e: impl std::fmt::Display) -> CoreError {
    CoreError::Arithmetic(e.to_string())
}

/// `int((budget / price).to_integral_value(rounding=ROUND_DOWN))`.
fn floor_div(budget: &PyDec, price: Decimal) -> CoreResult<i64> {
    let q = budget.div(&WideDecimal::from(price))?;
    let PyDec::Finite(q) = q else {
        return Err(CoreError::Arithmetic(
            "cannot convert Infinity to integer".to_owned(),
        ));
    };
    let whole = q.quantize(0, Rounding::Down).map_err(wide_err)?;
    let whole = whole.try_to_decimal().map_err(wide_err)?;
    let whole = whole.trunc();
    i64::try_from(whole)
        .map_err(|e| CoreError::Arithmetic(format!("share count {whole} out of range: {e}")))
}

/// `_pct_of`: `(numerator / denominator * 100).quantize(Decimal("0.01"))`, 0 when the
/// denominator is not positive.
fn pct_of(numerator: Decimal, denominator: Decimal) -> CoreResult<Decimal> {
    if denominator <= Decimal::ZERO {
        return Ok(Decimal::ZERO);
    }
    WideDecimal::from(numerator)
        .div(&WideDecimal::from(denominator))
        .map_err(wide_err)?
        .mul(&WideDecimal::from(100_i64))
        .quantize(-2, Rounding::HalfEven)
        .and_then(|q| q.try_to_decimal())
        .map_err(wide_err)
}

/// Ports `baskfy_core.vbt.sizing.size_entry`: the share count for one entry,
/// `floor(min(slot, position cap, cash, turnover cap) / limit_price)`.
///
/// `slot_multiplier` is `04` §5.4's first-live halving, applied here so every cap and refusal
/// sees the real size. Ties between budgets go to the earlier one (`SLOT`, `POSITION_PCT`,
/// `CASH`, `TURNOVER`), as Python's `min` does.
///
/// # Errors
/// Python's decimal failures, reproduced as [`CoreError::Arithmetic`]: `max_slots == 0`
/// (`DivisionByZero`), an infinite budget that binds (`OverflowError`), `0 x Infinity`; and a
/// NaN config float.
#[allow(clippy::too_many_arguments)] // one keyword per input the size depends on, as Python
pub fn size_entry(
    equity: Decimal,
    cash_available: Decimal,
    limit_price: Decimal,
    stop_price: Decimal,
    turnover_avg_inr: Option<Decimal>,
    config: &SizingConfig,
    slot_multiplier: Decimal,
) -> CoreResult<SizedEntry> {
    if equity <= Decimal::ZERO {
        return Ok(refuse(
            limit_price,
            stop_price,
            SizeRefusal::NoSleeveCapital,
        ));
    }
    if limit_price <= Decimal::ZERO || stop_price >= limit_price {
        return Ok(refuse(
            limit_price,
            stop_price,
            SizeRefusal::StopNotBelowEntry,
        ));
    }
    let wide_equity = WideDecimal::from(equity);
    let by_slot = wide_equity
        .div(&WideDecimal::from(config.max_slots))
        .map_err(wide_err)?
        .mul(&WideDecimal::from(slot_multiplier));
    let by_position = PyDec::from_float(config.max_position_pct)?
        .mul(&wide_equity)?
        .div(&WideDecimal::from(100_i64))?;
    let cash = if cash_available < Decimal::ZERO {
        Decimal::ZERO
    } else {
        cash_available
    };
    let mut budgets: Vec<(PyDec, SizeCap)> = vec![
        (PyDec::Finite(by_slot), SizeCap::Slot),
        (by_position, SizeCap::PositionPct),
        (PyDec::Finite(WideDecimal::from(cash)), SizeCap::Cash),
    ];
    if let Some(turnover) = turnover_avg_inr {
        let cap = PyDec::from_float(config.max_position_vs_turnover)?
            .mul(&WideDecimal::from(turnover))?;
        budgets.push((cap, SizeCap::Turnover));
    }
    // `min(budgets, key=...)`: the first of the smallest.
    let mut best = 0;
    for i in 1..budgets.len() {
        if budgets[i].0.cmp(&budgets[best].0) == Ordering::Less {
            best = i;
        }
    }
    let (budget, cap) = (&budgets[best].0, budgets[best].1);
    let quantity = floor_div(budget, limit_price)?;
    let value = mul(limit_price, Decimal::from(quantity))?;
    if value < config.min_trade_value_inr {
        let why = if cap == SizeCap::Turnover {
            SizeRefusal::TurnoverCap
        } else {
            SizeRefusal::BelowMinTradeValue
        };
        return Ok(refuse(limit_price, stop_price, why));
    }
    let risk = mul(sub(limit_price, stop_price)?, Decimal::from(quantity))?;
    Ok(SizedEntry {
        quantity,
        limit_price,
        stop_price,
        value_inr: quantize(value, 2, RoundingStrategy::MidpointNearestEven)?,
        position_pct: pct_of(value, equity)?,
        risk_inr: quantize(risk, 2, RoundingStrategy::MidpointNearestEven)?,
        cap: Some(cap),
        refusal: None,
    })
}

/// Ports `baskfy_core.vbt.sizing.first_live_multiplier`: half a slot (the configured share)
/// while the countdown runs **and** a confirm would be real; `1` otherwise.
///
/// # Errors
/// A non-finite `risk_multiplier_first_live` (Python returns `Decimal('Infinity')`, which
/// `rust_decimal` cannot hold).
pub fn first_live_multiplier(
    sessions_left: i64,
    execution_enabled: bool,
    config: &SizingConfig,
) -> CoreResult<Decimal> {
    if execution_enabled && sessions_left > 0 {
        // `Decimal(str(x))` keeps repr's ".0" on a whole float: `1.0` is `Decimal("1.0")`.
        return wide::to_dec(&wide::from_py_float_str(config.risk_multiplier_first_live)?);
    }
    Ok(Decimal::ONE)
}
