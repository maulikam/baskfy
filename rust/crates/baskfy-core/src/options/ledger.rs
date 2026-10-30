//! Port of `baskfy_core.options.ledger` (`decile-blueprint/packages/core/src/baskfy_core/options/ledger.py`).
//!
//! A closed session's journal figures, pure (docs/options/04 §12), computed from the session's fills.
//!
//! Goldens: `L1/options/journal_figures.*` (`baskfy_core.options.ledger.journal_figures`).
//!
//! Everything is computed from the session's own **fills**, never from the plan's intentions:
//! **gross** is the signed cash of every fill (a sale brings premium in, a purchase pays it
//! out); **entry** and **exit** are the two halves' cash, unsigned; **costs** are `04` §6.1's
//! charges over every executed order; **net** = gross - costs; `r_multiple = net / R`; MFE and
//! MAE are the best and worst marked P&L, in R.

use chrono::{DateTime, FixedOffset};
use rust_decimal::{Decimal, RoundingStrategy};
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::options::costs::{
    ChargeBreakdown, CostFill, CostRates, Side, add, charges, mul, paise, py_div_quantize, sub,
};
use crate::options::structures::Structure;

/// Marked P&L per unit, a gain positive: a condor's credit less its close cost; otherwise value less paid.
pub fn pnl_points(
    structure: Structure,
    entry_points: Decimal,
    value_points: Decimal,
) -> CoreResult<Decimal> {
    if structure == Structure::IronCondor {
        sub(entry_points, value_points)
    } else {
        sub(value_points, entry_points)
    }
}

/// One executed fill: side, price per unit, units, and whether it closed the position.
///
/// Ports `baskfy_core.options.ledger.LegFill`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LegFill {
    /// Buy or sell.
    pub side: Side,
    /// Price per unit.
    pub price: Decimal,
    /// Units.
    pub quantity: i64,
    /// Whether the fill closed the position.
    pub closing: bool,
}

/// The session a journal row is computed for — Python's keyword arguments of
/// `journal_figures` other than the fills and the rates, as one value.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JournalSession {
    /// R, the risk the trade was taken at, ₹ (`04` §9.1). Must be positive.
    pub r_inr: Decimal,
    /// Units per leg, for MAE/MFE in R.
    pub quantity: i64,
    /// When the position opened.
    pub opened_at: DateTime<FixedOffset>,
    /// When it closed.
    pub closed_at: DateTime<FixedOffset>,
    /// Best marked P&L, points per unit, a gain positive.
    #[serde(default, deserialize_with = "signed_zero::deserialize")]
    pub peak_points: Option<Decimal>,
    /// Worst marked P&L, points per unit, a gain positive.
    #[serde(default, deserialize_with = "signed_zero::deserialize")]
    pub trough_points: Option<Decimal>,
}

/// An optional `Decimal` read from its string form keeping the sign of a zero.
///
/// `rust_decimal` parses `"-0.00"` as `+0.00`, but Python's `Decimal("-0.00") * 65 / R`
/// quantizes to `-0.00`, so an MAE/MFE of negative zero must reach [`journal_figures`] negative
/// for the row to print as Python's does.
mod signed_zero {
    use std::fmt;
    use std::str::FromStr;

    use rust_decimal::Decimal;
    use serde::Deserializer;
    use serde::de::{Error, Visitor};

    struct OptDecimal;

    impl<'de> Visitor<'de> for OptDecimal {
        type Value = Option<Decimal>;

        fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
            f.write_str("a decimal string, a number, or null")
        }

        fn visit_none<E: Error>(self) -> Result<Self::Value, E> {
            Ok(None)
        }

        fn visit_unit<E: Error>(self) -> Result<Self::Value, E> {
            Ok(None)
        }

        fn visit_some<D: Deserializer<'de>>(self, d: D) -> Result<Self::Value, D::Error> {
            d.deserialize_any(self)
        }

        fn visit_str<E: Error>(self, v: &str) -> Result<Self::Value, E> {
            let text = v.trim();
            let mut out = Decimal::from_str(text)
                .or_else(|_| Decimal::from_scientific(text))
                .map_err(|e| E::custom(format!("{v:?} is not a decimal: {e}")))?;
            if out.is_zero() && text.starts_with('-') {
                out.set_sign_negative(true);
            }
            Ok(Some(out))
        }

        fn visit_i64<E: Error>(self, v: i64) -> Result<Self::Value, E> {
            Ok(Some(Decimal::from(v)))
        }

        fn visit_u64<E: Error>(self, v: u64) -> Result<Self::Value, E> {
            Ok(Some(Decimal::from(v)))
        }

        fn visit_f64<E: Error>(self, v: f64) -> Result<Self::Value, E> {
            Decimal::from_str(&v.to_string())
                .map(Some)
                .map_err(|e| E::custom(format!("{v} is not a decimal: {e}")))
        }
    }

    pub(super) fn deserialize<'de, D: Deserializer<'de>>(
        d: D,
    ) -> Result<Option<Decimal>, D::Error> {
        d.deserialize_option(OptDecimal)
    }
}

/// The row `op_journal` stores for one closed session.
///
/// Ports `baskfy_core.options.ledger.JournalFigures`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JournalFigures {
    /// Cash of the opening fills, unsigned.
    pub entry_inr: Decimal,
    /// Cash of the closing fills, unsigned.
    pub exit_inr: Decimal,
    /// Signed cash of every fill.
    pub gross_pnl_inr: Decimal,
    /// `04` §6.1's charges over every executed order.
    pub costs: ChargeBreakdown,
    /// Gross less costs.
    pub net_pnl_inr: Decimal,
    /// R, to the paisa.
    pub r_inr: Decimal,
    /// Net over R, two places.
    pub r_multiple: Decimal,
    /// Whole minutes between open and close, never negative.
    pub minutes_held: i64,
    /// Trough in R, two places.
    pub mae_r: Option<Decimal>,
    /// Peak in R, two places.
    pub mfe_r: Option<Decimal>,
}

fn cash<'a>(fills: impl IntoIterator<Item = &'a LegFill>) -> CoreResult<Decimal> {
    fills.into_iter().try_fold(Decimal::ZERO, |acc, f| {
        let sign = match f.side {
            Side::Sell => Decimal::ONE,
            Side::Buy => Decimal::NEGATIVE_ONE,
        };
        add(acc, mul(mul(f.price, Decimal::from(f.quantity))?, sign)?)
    })
}

/// `(a / b).quantize(Decimal("0.01"))` under the default context: half even.
fn r_places(a: Decimal, b: Decimal) -> CoreResult<Decimal> {
    py_div_quantize(a, b, 2, RoundingStrategy::MidpointNearestEven)
}

/// The row `op_journal` stores for one closed session.
///
/// Ports `baskfy_core.options.ledger.journal_figures`.
pub fn journal_figures(
    fills: &[LegFill],
    session: &JournalSession,
    rates: &CostRates,
) -> CoreResult<JournalFigures> {
    let r_inr = session.r_inr;
    if r_inr <= Decimal::ZERO {
        return Err(CoreError::InvalidInput("R must be positive".to_string()));
    }
    let gross = paise(cash(fills)?)?;
    let cost_fills: Vec<CostFill> = fills
        .iter()
        .map(|f| CostFill {
            side: f.side,
            price: f.price,
            quantity: f.quantity,
        })
        .collect();
    let cost = charges(&cost_fills, rates)?;
    let net = paise(sub(gross, cost.total()?)?)?;
    let quantity = Decimal::from(session.quantity);
    let in_r = |points: Option<Decimal>| -> CoreResult<Option<Decimal>> {
        points
            .map(|p| r_places(mul(p, quantity)?, r_inr))
            .transpose()
    };
    let seconds = (session.closed_at - session.opened_at).num_seconds();
    Ok(JournalFigures {
        entry_inr: paise(cash(fills.iter().filter(|f| !f.closing))?.abs())?,
        exit_inr: paise(cash(fills.iter().filter(|f| f.closing))?.abs())?,
        gross_pnl_inr: gross,
        costs: cost,
        net_pnl_inr: net,
        r_inr: paise(r_inr)?,
        // journal.r_multiple: net / R, R already checked positive.
        r_multiple: r_places(net, r_inr)?,
        minutes_held: (seconds / 60).max(0),
        mae_r: in_r(session.trough_points)?,
        mfe_r: in_r(session.peak_points)?,
    })
}
