//! Port of `baskfy_core.options.sizing`: lots from a risk budget, margin as a ceiling (`04` §7).

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::options::config::{Mode, OptionsCeilings, SizingConfig, SizingMode};
use crate::options::num::{self, fixed};

/// Why sizing refused.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SizingReject {
    /// No lot size in the master.
    RejectedNoLotSize,
    /// A live plan with no capital.
    RejectedNoSleeveCapital,
    /// The budget buys no lot.
    RejectedBudget,
    /// O2's premium cap.
    RejectedPremiumCap,
}

/// O1: `(wing_width - credit) * lot_size + reserve`.
pub fn risk_per_lot_condor(
    wing_width_points: Decimal,
    credit_points: Decimal,
    lot_size: i64,
    reserve_per_lot_inr: Decimal,
) -> CoreResult<Decimal> {
    num::add(
        num::muli(num::sub(wing_width_points, credit_points)?, lot_size)?,
        reserve_per_lot_inr,
    )
}

/// O2: `E_planned * stop_frac * lot_size + reserve`.
pub fn risk_per_lot_long(
    planned_entry: Decimal,
    stop_frac: Decimal,
    lot_size: i64,
    reserve_per_lot_inr: Decimal,
) -> CoreResult<Decimal> {
    num::add(
        num::muli(num::mul(planned_entry, stop_frac)?, lot_size)?,
        reserve_per_lot_inr,
    )
}

/// O2's true worst case per lot: the whole premium.
pub fn gap_through_long(entry: Decimal, lot_size: i64) -> CoreResult<Decimal> {
    num::muli(entry, lot_size)
}

/// O3: `debit * lot_size + reserve`.
pub fn risk_per_lot_debit_spread(
    debit: Decimal,
    lot_size: i64,
    reserve_per_lot_inr: Decimal,
) -> CoreResult<Decimal> {
    num::add(num::muli(debit, lot_size)?, reserve_per_lot_inr)
}

/// The sizing's answer; `lots == 0` iff `rejection` is set.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Sizing {
    /// Lots.
    pub lots: i64,
    /// How it was sized.
    pub sizing_mode: SizingMode,
    /// The budget in force.
    pub risk_budget_inr: Decimal,
    /// The R the journal divides by.
    pub r_inr: Decimal,
    /// Risk per lot.
    pub risk_per_lot_inr: Decimal,
    /// First-live half size.
    pub half_size: bool,
    /// Why it refused.
    pub rejection: Option<SizingReject>,
    /// The arithmetic, in words.
    pub message: String,
}

fn reject(code: SizingReject, message: String, risk_per_lot: Decimal, budget: Decimal) -> Sizing {
    Sizing {
        lots: 0,
        sizing_mode: SizingMode::Budget,
        risk_budget_inr: budget,
        r_inr: budget,
        risk_per_lot_inr: risk_per_lot,
        half_size: false,
        rejection: Some(code),
        message,
    }
}

/// `04` §7.5: fewer than `first_live_trades` real rows.
pub fn first_live(real_journal_rows: i64, config: &SizingConfig) -> bool {
    real_journal_rows < config.first_live_trades
}

/// Every input `04` §7 names.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SizeInputs {
    /// Paper or live.
    pub mode: Mode,
    /// The sleeve's capital.
    pub sleeve_capital_inr: Decimal,
    /// Risk per trade, percent.
    pub risk_per_trade_pct: Decimal,
    /// The sleeve's lot cap.
    pub max_lots: i64,
    /// One lot's risk.
    pub risk_per_lot_inr: Decimal,
    /// The master's lot size.
    pub lot_size: Option<i64>,
    /// Real journal rows so far.
    pub real_journal_rows: i64,
}

/// `04` §7.1-7.3 and §7.5.
pub fn size(
    inputs: &SizeInputs,
    config: &SizingConfig,
    ceilings: &OptionsCeilings,
) -> CoreResult<Sizing> {
    let per_lot = inputs.risk_per_lot_inr;
    if inputs.lot_size.is_none_or(|l| l <= 0) {
        return Ok(reject(
            SizingReject::RejectedNoLotSize,
            "the master has no lot size for this expiry; nothing is sized from a guess".to_owned(),
            per_lot,
            Decimal::ZERO,
        ));
    }
    if per_lot <= Decimal::ZERO {
        return Err(CoreError::InvalidInput(
            "risk per lot must be positive — a structure with no risk is a bug".to_owned(),
        ));
    }
    if inputs.sleeve_capital_inr <= Decimal::ZERO {
        if inputs.mode == Mode::Live {
            return Ok(reject(
                SizingReject::RejectedNoSleeveCapital,
                "sleeve capital is ₹0; a live plan needs capital Maulik has set".to_owned(),
                per_lot,
                Decimal::ZERO,
            ));
        }
        return Ok(Sizing {
            lots: 1,
            sizing_mode: SizingMode::PaperOneLot,
            risk_budget_inr: per_lot,
            r_inr: per_lot,
            risk_per_lot_inr: per_lot,
            half_size: false,
            rejection: None,
            message: "paper, sleeve capital ₹0: one lot, R = one lot's risk".to_owned(),
        });
    }
    let pct = num::min(inputs.risk_per_trade_pct, ceilings.risk_pct_max);
    let mut budget = num::min(
        num::div(
            num::mul(inputs.sleeve_capital_inr, pct)?,
            Decimal::ONE_HUNDRED,
        )?,
        ceilings.risk_per_trade_inr_max,
    );
    let half = first_live(inputs.real_journal_rows, config);
    if half {
        budget = num::mul(budget, config.first_live_risk_multiplier)?;
    }
    let raw = num::floor_int(num::div(budget, per_lot)?)?;
    let lots = raw.min(inputs.max_lots).min(ceilings.max_lots_max);
    if lots <= 0 {
        return Ok(reject(
            SizingReject::RejectedBudget,
            format!(
                "risk budget ₹{} / risk per lot ₹{} = {raw} lots",
                fixed(budget, 2),
                fixed(per_lot, 2)
            ),
            per_lot,
            budget,
        ));
    }
    Ok(Sizing {
        lots,
        sizing_mode: SizingMode::Budget,
        risk_budget_inr: budget,
        r_inr: budget,
        risk_per_lot_inr: per_lot,
        half_size: half,
        rejection: None,
        message: format!(
            "₹{} / ₹{} per lot → {lots} lots",
            fixed(budget, 2),
            fixed(per_lot, 2)
        ),
    })
}

/// O2's premium cap: `lots * E * lot_size <= premium_cap_pct % of capital` when capital > 0.
pub fn premium_cap_ok(
    lots: i64,
    entry: Decimal,
    lot_size: i64,
    sleeve_capital_inr: Decimal,
    premium_cap_pct: Decimal,
) -> CoreResult<bool> {
    if sleeve_capital_inr <= Decimal::ZERO {
        return Ok(true);
    }
    let premium = num::muli(num::mul(Decimal::from(lots), entry)?, lot_size)?;
    let cap = num::div(
        num::mul(sleeve_capital_inr, premium_cap_pct)?,
        Decimal::ONE_HUNDRED,
    )?;
    Ok(premium <= cap)
}

/// The margin check's code.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum MarginCode {
    /// Fits.
    Ok,
    /// No pool set (a paper warning).
    MarginPoolUnset,
    /// Does not fit.
    RejectedMargin,
}

impl MarginCode {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            MarginCode::Ok => "OK",
            MarginCode::MarginPoolUnset => "MARGIN_POOL_UNSET",
            MarginCode::RejectedMargin => "REJECTED_MARGIN",
        }
    }
}

/// The margin check's answer.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MarginCheck {
    /// The code.
    pub code: MarginCode,
    /// Pool less what is in use.
    pub available_inr: Decimal,
    /// The arithmetic, in words.
    pub message: String,
}

impl MarginCheck {
    /// `REJECTED_MARGIN`.
    pub fn rejected(&self) -> bool {
        self.code == MarginCode::RejectedMargin
    }
}

/// `04` §7.4 — the broker's basket estimate must fit under the free pool.
pub fn margin_check(
    mode: Mode,
    hedged_estimate_inr: Decimal,
    transient_estimate_inr: Option<Decimal>,
    margin_pool_inr: Decimal,
    margin_in_use_inr: Decimal,
) -> CoreResult<MarginCheck> {
    let available = num::sub(margin_pool_inr, margin_in_use_inr)?;
    let transient = match transient_estimate_inr {
        Some(t) if !t.is_zero() => t,
        _ => Decimal::ZERO,
    };
    let worst = num::max(hedged_estimate_inr, transient);
    let check = |code, message| MarginCheck {
        code,
        available_inr: available,
        message,
    };
    if margin_pool_inr <= Decimal::ZERO {
        if mode == Mode::Paper {
            return Ok(check(
                MarginCode::MarginPoolUnset,
                format!("margin pool unset; this plan needs ₹{}", fixed(worst, 2)),
            ));
        }
        return Ok(check(
            MarginCode::RejectedMargin,
            "margin pool unset (live)".to_owned(),
        ));
    }
    if worst > available {
        let which = if worst == hedged_estimate_inr {
            "hedged"
        } else {
            "transient"
        };
        return Ok(check(
            MarginCode::RejectedMargin,
            format!(
                "{which} margin ₹{} > free pool ₹{}",
                fixed(worst, 2),
                fixed(available, 2)
            ),
        ));
    }
    Ok(check(
        MarginCode::Ok,
        format!("₹{} within free ₹{}", fixed(worst, 2), fixed(available, 2)),
    ))
}
