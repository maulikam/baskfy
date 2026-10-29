//! Port of `baskfy_core.fno.sizing` (`decile-blueprint/packages/core/src/baskfy_core/fno/sizing.py`).
//!
//! Lots from a risk budget; margin a ceiling, never a source (`04` §3, §10; Track C §6).
//!
//! `lots = floor(min(capital x risk %, RISK_PER_TRADE_INR_MAX) ÷ (risk per unit x lot_size))`,
//! capped at `fo_max_lots` (and its ceiling 10). **Zero lots is `REJECTED_SIZE`, never rounded
//! up to one.** A sleeve at ₹0 runs one lot on paper — recording what the live size would have
//! been at the per-trade ceiling — and live refuses `NO_SLEEVE_CAPITAL`. The broker's basket
//! margin is checked **after** sizing and can only reject (`REJECTED_MARGIN`).
//!
//! Goldens: `L1/fno/sizing.{risk_budget,size,margin_check}.case_NNN.json`.

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use super::config::py::mul;
use super::config::{CommonConfig, FnoCeilings, FnoError, FnoResult, PlanState, py};
use crate::backtest::helpers::Rounding;
use crate::options::gating::Mode;

/// `04` §7.3 / PACK.6 — the journal never pools the two.
///
/// Ports `baskfy_core.options.config.SizingMode` (the options config has no Rust home yet).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SizingMode {
    /// Lots from the risk budget.
    Budget,
    /// Paper at ₹0 capital: one lot.
    PaperOneLot,
}

/// The sizing's answer. `lots == 0` iff `state` is set.
///
/// Ports `baskfy_core.fno.sizing.FoSizing`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct FoSizing {
    /// Lots.
    pub lots: i64,
    /// Budget or paper-one-lot.
    pub sizing_mode: SizingMode,
    /// The risk budget, ₹.
    pub risk_budget_inr: Decimal,
    /// Risk per lot, ₹.
    pub risk_per_lot_inr: Decimal,
    /// The plan's max loss in ₹ (lots x risk per lot).
    pub max_loss_inr: Decimal,
    /// Paper at ₹0 only: the lots a live plan would get with the per-trade ceiling as budget.
    pub lots_at_ceiling: Option<i64>,
    /// The refusal, if any.
    pub state: Option<PlanState>,
    /// The sizing in words.
    pub message: String,
}

/// `int((budget / per_lot).to_integral_value(rounding=ROUND_FLOOR))` — the quotient rounded to
/// 28 significant digits first, exactly as Python divides.
fn floor_lots(budget: Decimal, per_lot: Decimal) -> FnoResult<i64> {
    let q = py::div_wide(budget, per_lot)?;
    // Both operands are positive here, so ROUND_DOWN is ROUND_FLOOR; a negative quotient (not
    // reachable from `size`) floors away from zero, as Python's would.
    let negative = q.is_sign_negative() && !q.is_zero();
    let down = q
        .quantize(0, Rounding::Down)
        .map_err(|e| FnoError::Decimal(e.to_string()))?;
    let whole = down
        .try_to_decimal()
        .map_err(|e| FnoError::Decimal(e.to_string()))?;
    let mut lots = i64::try_from(whole.mantissa())
        .map_err(|_| FnoError::Decimal(format!("{whole} lots is out of range")))?;
    if negative && down != q {
        lots -= 1;
    }
    Ok(lots)
}

/// `min(capital x min(pct, RISK_PCT_MAX) ÷ 100, RISK_PER_TRADE_INR_MAX)`.
///
/// Ports `baskfy_core.fno.sizing.risk_budget`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn risk_budget(
    capital_inr: Decimal,
    risk_pct: Decimal,
    ceilings: &FnoCeilings,
) -> FnoResult<Decimal> {
    let pct = py::min(risk_pct, ceilings.risk_pct_max);
    let budget = py::div(mul(capital_inr, pct)?, Decimal::ONE_HUNDRED)?;
    Ok(py::min(budget, ceilings.risk_per_trade_inr_max))
}

fn refused(
    risk_budget_inr: Decimal,
    risk_per_lot_inr: Decimal,
    state: PlanState,
    message: String,
) -> FoSizing {
    FoSizing {
        lots: 0,
        sizing_mode: SizingMode::Budget,
        risk_budget_inr,
        risk_per_lot_inr,
        max_loss_inr: Decimal::ZERO,
        lots_at_ceiling: None,
        state: Some(state),
        message,
    }
}

/// `04` §3 and §10, for either sleeve (`risk_per_unit` is what differs: F1's condor max loss
/// per unit, F2's `entry - stop`).
///
/// Ports `baskfy_core.fno.sizing.size`.
///
/// # Errors
/// `ValueError` when `risk_per_unit` is not positive (a structure with no risk is a bug).
pub fn size(
    mode: Mode,
    capital_inr: Decimal,
    risk_pct: Decimal,
    risk_per_unit: Decimal,
    lot_size: Option<i64>,
    common: &CommonConfig,
    ceilings: &FnoCeilings,
) -> FnoResult<FoSizing> {
    let lot_size = match lot_size {
        Some(n) if n > 0 => n,
        _ => {
            return Ok(refused(
                Decimal::ZERO,
                Decimal::ZERO,
                PlanState::RejectedSize,
                "the master has no lot size; nothing is sized from a guess".to_owned(),
            ));
        }
    };
    if risk_per_unit <= Decimal::ZERO {
        return Err(FnoError::Value(
            "risk per unit must be positive: a structure with no risk is a bug".to_owned(),
        ));
    }
    let per_lot = mul(risk_per_unit, Decimal::from(lot_size))?;
    let cap = common.max_lots.min(common.max_lots_ceiling);
    if capital_inr <= Decimal::ZERO {
        if mode == Mode::Live {
            return Ok(refused(
                Decimal::ZERO,
                per_lot,
                PlanState::NoSleeveCapital,
                "sleeve capital is ₹0; a live plan needs capital Maulik has set".to_owned(),
            ));
        }
        let at_ceiling = floor_lots(ceilings.risk_per_trade_inr_max, per_lot)?.min(cap);
        return Ok(FoSizing {
            lots: 1,
            sizing_mode: SizingMode::PaperOneLot,
            risk_budget_inr: per_lot,
            risk_per_lot_inr: per_lot,
            max_loss_inr: per_lot,
            lots_at_ceiling: Some(at_ceiling),
            state: None,
            message: format!(
                "paper, capital ₹0: one lot; live at the ₹{} ceiling would be {at_ceiling} lots",
                py::str(ceilings.risk_per_trade_inr_max)
            ),
        });
    }
    let budget = risk_budget(capital_inr, risk_pct, ceilings)?;
    let raw = floor_lots(budget, per_lot)?;
    let lots = raw.min(cap);
    if lots <= 0 {
        return Ok(refused(
            budget,
            per_lot,
            PlanState::RejectedSize,
            format!(
                "risk budget ₹{} / risk per lot ₹{} = {raw} lots",
                py::fmt_2f(budget),
                py::fmt_2f(per_lot)
            ),
        ));
    }
    Ok(FoSizing {
        lots,
        sizing_mode: SizingMode::Budget,
        risk_budget_inr: budget,
        risk_per_lot_inr: per_lot,
        max_loss_inr: mul(per_lot, Decimal::from(lots))?,
        lots_at_ceiling: None,
        state: None,
        message: format!(
            "₹{} / ₹{} per lot → {lots} lots",
            py::fmt_2f(budget),
            py::fmt_2f(per_lot)
        ),
    })
}

/// Ports `baskfy_core.fno.sizing.MarginVerdict`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum MarginVerdict {
    /// Within free margin.
    Ok,
    /// Above free margin.
    RejectedMargin,
}

/// Ports `baskfy_core.fno.sizing.MarginCheck`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct MarginCheck {
    /// The verdict.
    pub verdict: MarginVerdict,
    /// The check in words.
    pub message: String,
}

/// `basket_order_margins` for the plan's legs ≤ free margin, or `REJECTED_MARGIN`. It never
/// returns a lot count: margin never sizes (Track C §6).
///
/// Ports `baskfy_core.fno.sizing.margin_check`.
#[must_use]
pub fn margin_check(required_inr: Decimal, free_inr: Decimal) -> MarginCheck {
    if required_inr <= free_inr {
        return MarginCheck {
            verdict: MarginVerdict::Ok,
            message: format!(
                "₹{} within free ₹{}",
                py::fmt_2f(required_inr),
                py::fmt_2f(free_inr)
            ),
        };
    }
    MarginCheck {
        verdict: MarginVerdict::RejectedMargin,
        message: format!(
            "margin ₹{} > free ₹{}",
            py::fmt_2f(required_inr),
            py::fmt_2f(free_inr)
        ),
    }
}
