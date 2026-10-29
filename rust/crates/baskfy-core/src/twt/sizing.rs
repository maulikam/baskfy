//! Port of `baskfy_core.twt.sizing` — ten equal slots, and every cap named, in order
//! (`docs/twt/04` §6).
//!
//! Money is `Decimal` (house rule 9). The arithmetic runs in
//! [`WideDecimal`](crate::backtest::helpers::WideDecimal) — Python's `Decimal` in its default
//! context (28 significant digits, half-even) — so every intermediate rounds exactly where
//! Python's does; the published fields are quantized to the paise (or passed through) and leave
//! as `rust_decimal::Decimal`.

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::backtest::helpers::{Rounding, WideDecimal};
use crate::error::{CoreError, CoreResult};
use crate::twt::config::SizingConfig;

/// Ports `SizeCap`: which budget decided the quantity (`04` §6.2), in the order applied.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SizeCap {
    /// `equity / max_slots` — the ordinary answer.
    #[serde(rename = "SLOT")]
    Slot,
    /// `max_position_pct` of equity.
    #[serde(rename = "POSITION_PCT")]
    PositionPct,
    /// 1 % of the name's 20-session average turnover.
    #[serde(rename = "TURNOVER")]
    Turnover,
    /// The sleeve's cash.
    #[serde(rename = "CASH")]
    Cash,
}

/// Ports `SizeRefusal`: why no position at all.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SizeRefusal {
    /// Equity at or below zero.
    #[serde(rename = "NO_SLEEVE_CAPITAL")]
    NoSleeveCapital,
    /// A stop at or above the entry, or a non-positive price.
    #[serde(rename = "STOP_NOT_BELOW_ENTRY")]
    StopNotBelowEntry,
    /// The line is worth less than `min_trade_value_inr`.
    #[serde(rename = "BELOW_MIN_TRADE_VALUE")]
    BelowMinTradeValue,
}

/// Ports `SizedEntry`: one sized market order; `quantity == 0` always carries a `refusal`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SizedEntry {
    /// Shares.
    pub quantity: i64,
    /// The price the caller expects to pay.
    pub entry_price: Decimal,
    /// The stop passed in.
    pub stop_price: Decimal,
    /// `entry_price x quantity`, to the paise.
    pub value_inr: Decimal,
    /// The value as a percent of equity, to two places.
    pub position_pct: Decimal,
    /// `(entry - stop) x quantity`, to the paise.
    pub risk_inr: Decimal,
    /// The last cap that lowered the target.
    pub cap: Option<SizeCap>,
    /// Every cap that lowered it, in order.
    pub caps_applied: Vec<SizeCap>,
    /// Why nothing was sized.
    pub refusal: Option<SizeRefusal>,
}

impl SizedEntry {
    /// Ports `SizedEntry.placed`.
    #[must_use]
    pub fn placed(&self) -> bool {
        self.quantity > 0
    }

    /// Ports `SizedEntry.turnover_capped`.
    #[must_use]
    pub fn turnover_capped(&self) -> bool {
        self.caps_applied.contains(&SizeCap::Turnover)
    }
}

fn arith(e: impl std::fmt::Display) -> CoreError {
    CoreError::Arithmetic(e.to_string())
}

fn w(d: Decimal) -> WideDecimal {
    WideDecimal::from(d)
}

fn refuse(entry: Decimal, stop: Decimal, why: SizeRefusal) -> SizedEntry {
    SizedEntry {
        quantity: 0,
        entry_price: entry,
        stop_price: stop,
        value_inr: Decimal::ZERO,
        position_pct: Decimal::ZERO,
        risk_inr: Decimal::ZERO,
        cap: None,
        caps_applied: Vec::new(),
        refusal: Some(why),
    }
}

/// `x.quantize(Decimal("0.01"))` (half-even), out as a `Decimal`.
fn paise(x: &WideDecimal) -> CoreResult<Decimal> {
    x.quantize(-2, Rounding::HalfEven)
        .and_then(|q| q.try_to_decimal())
        .map_err(arith)
}

/// Ports `size_entry`: `quantity = floor(target / entry_price)` where `target` starts at
/// `equity / max_slots x slot_multiplier` and is lowered, in order, by the per-position
/// ceiling, the name's liquidity (when known) and the sleeve's cash.
///
/// # Errors
/// [`CoreError::Arithmetic`] where Python's `Decimal` would raise (a quantity or value beyond
/// 28 digits), or a quantity beyond `i64`.
pub fn size_entry(
    equity: Decimal,
    cash_available: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    turnover_avg_inr: Option<Decimal>,
    config: &SizingConfig,
    slot_multiplier: Decimal,
) -> CoreResult<SizedEntry> {
    if equity <= Decimal::ZERO {
        return Ok(refuse(
            entry_price,
            stop_price,
            SizeRefusal::NoSleeveCapital,
        ));
    }
    if entry_price <= Decimal::ZERO || stop_price >= entry_price || stop_price <= Decimal::ZERO {
        return Ok(refuse(
            entry_price,
            stop_price,
            SizeRefusal::StopNotBelowEntry,
        ));
    }
    let hundred = WideDecimal::from(100_i64);
    let mut target = w(equity)
        .div(&WideDecimal::from(config.max_slots))
        .map_err(arith)?
        .mul(&w(slot_multiplier));
    let mut cap = SizeCap::Slot;
    let mut applied: Vec<SizeCap> = Vec::new();
    let mut ceilings: Vec<(SizeCap, WideDecimal)> = vec![(
        SizeCap::PositionPct,
        w(equity)
            .mul(&w(config.max_position_pct))
            .div(&hundred)
            .map_err(arith)?,
    )];
    if let Some(turnover) = turnover_avg_inr {
        ceilings.push((
            SizeCap::Turnover,
            w(turnover).mul(&w(config.max_position_vs_turnover)),
        ));
    }
    // `max(cash_available, Decimal(0))`: the first argument unless zero is greater.
    let cash = if Decimal::ZERO > cash_available {
        WideDecimal::zero()
    } else {
        w(cash_available)
    };
    ceilings.push((SizeCap::Cash, cash));
    for (name, ceiling) in ceilings {
        if ceiling < target {
            target = ceiling;
            cap = name;
            applied.push(name);
        }
    }

    // `int((budget / price).to_integral_value(rounding=ROUND_DOWN))`.
    let shares = target
        .div(&w(entry_price))
        .and_then(|q| q.quantize(0, Rounding::Down))
        .map_err(arith)?;
    let quantity: i64 = shares
        .try_to_decimal()
        .map_err(arith)?
        .try_into()
        .map_err(|_| CoreError::Arithmetic(format!("quantity {shares} does not fit i64")))?;
    let value = w(entry_price).mul(&WideDecimal::from(quantity));
    if value < w(config.min_trade_value_inr) {
        return Ok(refuse(
            entry_price,
            stop_price,
            SizeRefusal::BelowMinTradeValue,
        ));
    }
    // `_pct_of(value, equity)`: equity > 0 here, so never the zero branch.
    let position_pct = value.div(&w(equity)).map_err(arith)?.mul(&hundred);
    let risk = w(entry_price)
        .sub(&w(stop_price))
        .mul(&WideDecimal::from(quantity));
    Ok(SizedEntry {
        quantity,
        entry_price,
        stop_price,
        value_inr: paise(&value)?,
        position_pct: paise(&position_pct)?,
        risk_inr: paise(&risk)?,
        cap: Some(cap),
        caps_applied: applied,
        refusal: None,
    })
}

/// Ports `first_live_multiplier`: half a slot while the first live **entries** are taken; a
/// `DRY_RUN` plan (`execution_enabled == false`) is full size.
#[must_use]
pub fn first_live_multiplier(
    entries_left: i64,
    execution_enabled: bool,
    config: &SizingConfig,
) -> Decimal {
    if execution_enabled && entries_left > 0 {
        return config.risk_multiplier_first_live;
    }
    Decimal::ONE
}
