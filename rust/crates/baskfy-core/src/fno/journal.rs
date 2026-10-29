//! Port of `baskfy_core.fno.journal` (`decile-blueprint/packages/core/src/baskfy_core/fno/journal.py`).
//!
//! A closed FO structure's journal figures, pure (`docs/fno/03` §6, `06` FO10). One
//! `fo_journal` row per structure, in ₹ and R, computed from the position's own **fills**:
//!
//! * **gross** is the signed cash of every fill (entry, every roll and the exit together);
//! * **costs** are itemised per order group: F1's option legs pay `docs/options/04` §6.1's
//!   charges, F2's futures pay `04` §10's per-order charges **with the slippage line at zero**
//!   (a fill's price already carries its slippage). Each roll is journalled with its own costs;
//! * **net** = gross - costs; **R** is the planned maximum loss; `r_multiple = net / R`;
//! * **MAE / MFE** are the worst and best nightly `fo_mark` P&L, in R (0 when a mark never
//!   went the other way).
//!
//! Goldens: `L1/fno/journal.{r_multiple,option_costs,future_costs,journal_figures,one_pool}`.

use std::collections::BTreeSet;

use chrono::{NaiveDate, NaiveDateTime};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize, Serializer, ser::SerializeStruct};

use super::config::py::{add, mul, sub};
use super::config::{FnoError, FnoResult, FoSleeve, FutureCostRates, PlanKind, Structure, py};
use super::costs::future_charges;
use crate::backtest::helpers::Rounding;
use crate::options::costs::{CostFill, CostRates, Side, charges, paise};

/// Every way an FO structure is closed and journalled (`04` §1, §10; FO7).
///
/// Ports `baskfy_core.fno.journal.CloseKind`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CloseKind {
    /// F1's profit take.
    ProfitTake,
    /// F1's loss close.
    LossClose,
    /// The hard exit before expiry.
    HardExit,
    /// Exited after the hard-exit date.
    LateExit,
    /// F2's stop.
    Stop,
    /// F2's time exit.
    TimeExit,
    /// A future left naked.
    NakedFuture,
    /// A roll that did not complete.
    RollIncomplete,
    /// An entry that did not fill in full, whose filled legs were closed at once.
    AbandonedPartial,
}

impl CloseKind {
    /// The enum's value (`"ABANDONED_PARTIAL"`).
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::ProfitTake => "PROFIT_TAKE",
            Self::LossClose => "LOSS_CLOSE",
            Self::HardExit => "HARD_EXIT",
            Self::LateExit => "LATE_EXIT",
            Self::Stop => "STOP",
            Self::TimeExit => "TIME_EXIT",
            Self::NakedFuture => "NAKED_FUTURE",
            Self::RollIncomplete => "ROLL_INCOMPLETE",
            Self::AbandonedPartial => "ABANDONED_PARTIAL",
        }
    }
}

/// One executed fill of a position, tagged with the plan it rode on.
///
/// Ports `baskfy_core.fno.journal.FoFillRecord`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FoFillRecord {
    /// The plan.
    pub plan_id: String,
    /// Entry, exit, roll or add.
    pub kind: PlanKind,
    /// Buy or sell.
    pub side: Side,
    /// Price per unit.
    pub price: Decimal,
    /// Units.
    pub quantity: i64,
    /// When it filled (naive IST).
    pub filled_at: NaiveDateTime,
}

impl FoFillRecord {
    /// Signed: a sale brings money in, a purchase pays it out.
    ///
    /// Ports `baskfy_core.fno.journal.FoFillRecord.cash`.
    ///
    /// # Errors
    /// Decimal overflow.
    pub fn cash(&self) -> FnoResult<Decimal> {
        let amount = mul(self.price, Decimal::from(self.quantity))?;
        Ok(if self.side == Side::Sell {
            amount
        } else {
            neg(amount)
        })
    }
}

/// Python's `-x`: a zero comes back positive (`Decimal.__neg__` under the default context).
fn neg(x: Decimal) -> Decimal {
    if x.is_zero() {
        let mut z = x;
        z.set_sign_positive(true);
        z
    } else {
        -x
    }
}

/// Python's `abs(x)`.
fn abs(x: Decimal) -> Decimal {
    let mut a = x;
    a.set_sign_positive(true);
    a
}

/// `sum(values, Decimal(0))`, left to right.
fn sum(values: impl IntoIterator<Item = Decimal>) -> FnoResult<Decimal> {
    values.into_iter().try_fold(Decimal::ZERO, add)
}

/// One order group's costs, itemised and rounded to the paisa (house rule 8).
///
/// Ports `baskfy_core.fno.journal.CostLines`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CostLines {
    /// Executed orders.
    pub orders: i64,
    /// Brokerage.
    pub brokerage: Decimal,
    /// STT.
    pub stt: Decimal,
    /// Exchange transaction charge.
    pub exchange_txn: Decimal,
    /// SEBI fee.
    pub sebi: Decimal,
    /// IPFT.
    pub ipft: Decimal,
    /// Stamp.
    pub stamp: Decimal,
    /// GST.
    pub gst: Decimal,
}

impl CostLines {
    /// The sum of every line.
    ///
    /// Ports `baskfy_core.fno.journal.CostLines.total`.
    ///
    /// # Errors
    /// Decimal overflow.
    pub fn total(&self) -> FnoResult<Decimal> {
        let mut t = self.brokerage;
        for x in [
            self.stt,
            self.exchange_txn,
            self.sebi,
            self.ipft,
            self.stamp,
            self.gst,
        ] {
            t = add(t, x)?;
        }
        Ok(t)
    }

    fn zero() -> Self {
        let z = Decimal::new(0, 2);
        Self {
            orders: 0,
            brokerage: z,
            stt: z,
            exchange_txn: z,
            sebi: z,
            ipft: z,
            stamp: z,
            gst: z,
        }
    }
}

impl Serialize for CostLines {
    /// The fields plus `total` — also `as_json()`'s shape (every Decimal as its string).
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let total = self.total().map_err(serde::ser::Error::custom)?;
        let mut s = serializer.serialize_struct("CostLines", 9)?;
        s.serialize_field("orders", &self.orders)?;
        s.serialize_field("brokerage", &self.brokerage)?;
        s.serialize_field("stt", &self.stt)?;
        s.serialize_field("exchange_txn", &self.exchange_txn)?;
        s.serialize_field("sebi", &self.sebi)?;
        s.serialize_field("ipft", &self.ipft)?;
        s.serialize_field("stamp", &self.stamp)?;
        s.serialize_field("gst", &self.gst)?;
        s.serialize_field("total", &total)?;
        s.end()
    }
}

/// One F2 roll: the held month sold, the next bought, and what the two orders cost.
///
/// Ports `baskfy_core.fno.journal.RollRecord`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RollRecord {
    /// The roll's plan.
    pub plan_id: String,
    /// The day of its first fill.
    pub on: NaiveDate,
    /// What the sale brought in, ₹.
    pub sold_inr: Decimal,
    /// What the purchase paid, ₹.
    pub bought_inr: Decimal,
    /// The two orders' costs.
    pub costs: CostLines,
}

/// The figures one `fo_journal` row stores for a closed structure.
///
/// Ports `baskfy_core.fno.journal.FoJournalFigures`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct FoJournalFigures {
    /// The entry fills' cash, unsigned, ₹.
    pub entry_inr: Decimal,
    /// The exit fills' cash, unsigned, ₹.
    pub exit_inr: Decimal,
    /// The signed cash of every fill, ₹.
    pub gross_pnl_inr: Decimal,
    /// The entry group's costs.
    pub entry_costs: CostLines,
    /// The exit group's costs.
    pub exit_costs: CostLines,
    /// Each roll with its own costs.
    pub rolls: Vec<RollRecord>,
    /// Every group's costs, ₹.
    pub costs_inr: Decimal,
    /// Gross less costs, ₹.
    pub net_pnl_inr: Decimal,
    /// The planned maximum loss, ₹.
    pub r_inr: Decimal,
    /// Net ÷ R, to the cent of R.
    pub r_multiple: Decimal,
    /// The worst mark, in R (`None` with no marks).
    pub mae_r: Option<Decimal>,
    /// The best mark, in R (`None` with no marks).
    pub mfe_r: Option<Decimal>,
}

/// One roll in `cost_detail()`'s shape.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RollDetail {
    /// The roll's plan.
    pub plan_id: String,
    /// The roll's day.
    pub on: NaiveDate,
    /// What the sale brought in, ₹.
    pub sold_inr: Decimal,
    /// What the purchase paid, ₹.
    pub bought_inr: Decimal,
    /// The two orders' costs.
    pub costs: CostLines,
}

/// `fo_journal.detail['costs']`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CostDetail {
    /// The entry group's costs.
    pub entry: CostLines,
    /// The exit group's costs.
    pub exit: CostLines,
    /// Each roll.
    pub rolls: Vec<RollDetail>,
    /// Every group's costs, ₹.
    pub total: Decimal,
}

impl FoJournalFigures {
    /// The itemised costs as `fo_journal.detail['costs']` stores them.
    ///
    /// Ports `baskfy_core.fno.journal.FoJournalFigures.cost_detail`.
    #[must_use]
    pub fn cost_detail(&self) -> CostDetail {
        CostDetail {
            entry: self.entry_costs.clone(),
            exit: self.exit_costs.clone(),
            rolls: self
                .rolls
                .iter()
                .map(|r| RollDetail {
                    plan_id: r.plan_id.clone(),
                    on: r.on,
                    sold_inr: r.sold_inr,
                    bought_inr: r.bought_inr,
                    costs: r.costs.clone(),
                })
                .collect(),
            total: self.costs_inr,
        }
    }
}

/// `net ÷ R`, half up to the cent (`fo_journal.r_multiple` is `numeric(8, 2)`). R must be
/// positive: a zero R is a sizing bug, not a zero trade.
///
/// Ports `baskfy_core.fno.journal.r_multiple`.
///
/// # Errors
/// `ValueError` when `r_inr <= 0`.
pub fn r_multiple(net_pnl_inr: Decimal, r_inr: Decimal) -> FnoResult<Decimal> {
    if r_inr <= Decimal::ZERO {
        return Err(FnoError::Value("R must be positive".to_owned()));
    }
    py::div_quantize(net_pnl_inr, r_inr, -2, Rounding::HalfUp)
}

/// `docs/options/04` §6.1 over a group of option orders (one fill is one order).
///
/// Ports `baskfy_core.fno.journal.option_costs`.
///
/// # Errors
/// Decimal overflow.
pub fn option_costs(fills: &[FoFillRecord], rates: &CostRates) -> FnoResult<CostLines> {
    if fills.is_empty() {
        return Ok(CostLines::zero());
    }
    let orders: Vec<CostFill> = fills
        .iter()
        .map(|f| CostFill {
            side: f.side,
            price: f.price,
            quantity: f.quantity,
        })
        .collect();
    let c = charges(&orders, rates)?;
    Ok(CostLines {
        orders: c.orders,
        brokerage: c.brokerage,
        stt: c.stt,
        exchange_txn: c.exchange_txn,
        sebi: c.sebi,
        ipft: c.ipft,
        stamp: c.stamp,
        gst: c.gst,
    })
}

/// `04` §10's futures charges over a group of orders, slippage excluded (it is in the fill
/// price already).
///
/// Ports `baskfy_core.fno.journal.future_costs`.
///
/// # Errors
/// Decimal overflow.
pub fn future_costs(fills: &[FoFillRecord], rates: &FutureCostRates) -> FnoResult<CostLines> {
    if fills.is_empty() {
        return Ok(CostLines::zero());
    }
    let turnover = |side: Side| -> FnoResult<Decimal> {
        let mut parts = Vec::new();
        for f in fills.iter().filter(|f| f.side == side) {
            parts.push(mul(f.price, Decimal::from(f.quantity))?);
        }
        sum(parts)
    };
    let buys = turnover(Side::Buy)?;
    let sells = turnover(Side::Sell)?;
    let no_slippage = FutureCostRates {
        slippage_pct: Decimal::ZERO,
        ..rates.clone()
    };
    let orders =
        i64::try_from(fills.len()).map_err(|_| FnoError::Value("too many orders".to_owned()))?;
    let c = future_charges(buys, sells, orders, &no_slippage)?;
    let zero = Decimal::new(0, 2);
    Ok(CostLines {
        orders: c.orders,
        brokerage: c.brokerage,
        stt: c.stt,
        exchange_txn: c.exchange_txn,
        sebi: zero,
        ipft: zero,
        stamp: c.stamp,
        gst: c.gst,
    })
}

/// The figures one `fo_journal` row stores for a closed structure. `fills` are every fill of
/// the position (entry, rolls, exit); `marks_inr` are its nightly `fo_mark.pnl_inr` values.
///
/// Ports `baskfy_core.fno.journal.journal_figures`.
///
/// # Errors
/// `ValueError` when `r_inr <= 0`; decimal overflow.
pub fn journal_figures(
    structure: Structure,
    fills: &[FoFillRecord],
    r_inr: Decimal,
    marks_inr: &[Decimal],
    option_rates: &CostRates,
    future_rates: &FutureCostRates,
) -> FnoResult<FoJournalFigures> {
    if r_inr <= Decimal::ZERO {
        return Err(FnoError::Value("R must be positive".to_owned()));
    }
    let of_kind = |kind: PlanKind| -> Vec<FoFillRecord> {
        fills.iter().filter(|f| f.kind == kind).cloned().collect()
    };
    let entry = of_kind(PlanKind::Entry);
    let exits = of_kind(PlanKind::Exit);
    let roll_ids: BTreeSet<&str> = fills
        .iter()
        .filter(|f| f.kind == PlanKind::Roll)
        .map(|f| f.plan_id.as_str())
        .collect();
    let (entry_costs, exit_costs) = if structure == Structure::IronCondor {
        (
            option_costs(&entry, option_rates)?,
            option_costs(&exits, option_rates)?,
        )
    } else {
        (
            future_costs(&entry, future_rates)?,
            future_costs(&exits, future_rates)?,
        )
    };
    let mut rolls = Vec::with_capacity(roll_ids.len());
    for plan_id in roll_ids {
        let group: Vec<FoFillRecord> = fills
            .iter()
            .filter(|f| f.plan_id == plan_id)
            .cloned()
            .collect();
        let Some(first) = group.iter().map(|f| f.filled_at).min() else {
            continue;
        };
        let cash_of = |side: Side| -> FnoResult<Decimal> {
            let mut parts = Vec::new();
            for f in group.iter().filter(|f| f.side == side) {
                parts.push(f.cash()?);
            }
            sum(parts)
        };
        rolls.push(RollRecord {
            plan_id: plan_id.to_owned(),
            on: first.date(),
            sold_inr: paise(cash_of(Side::Sell)?)?,
            bought_inr: paise(neg(cash_of(Side::Buy)?))?,
            costs: future_costs(&group, future_rates)?,
        });
    }
    let all_cash: Vec<Decimal> = fills
        .iter()
        .map(FoFillRecord::cash)
        .collect::<FnoResult<_>>()?;
    let gross = paise(sum(all_cash)?)?;
    let mut totals = vec![entry_costs.total()?, exit_costs.total()?];
    for r in &rolls {
        totals.push(r.costs.total()?);
    }
    let costs = paise(sum(totals)?)?;
    let net = paise(sub(gross, costs)?)?;
    // Python's min/max keep the first of equals.
    let worst = marks_inr
        .iter()
        .copied()
        .reduce(|a, b| if b < a { b } else { a });
    let best = marks_inr
        .iter()
        .copied()
        .reduce(|a, b| if b > a { b } else { a });
    let entry_cash: Vec<Decimal> = entry
        .iter()
        .map(FoFillRecord::cash)
        .collect::<FnoResult<_>>()?;
    let exit_cash: Vec<Decimal> = exits
        .iter()
        .map(FoFillRecord::cash)
        .collect::<FnoResult<_>>()?;
    Ok(FoJournalFigures {
        entry_inr: paise(abs(sum(entry_cash)?))?,
        exit_inr: paise(abs(sum(exit_cash)?))?,
        gross_pnl_inr: gross,
        entry_costs,
        exit_costs,
        rolls,
        costs_inr: costs,
        net_pnl_inr: net,
        r_inr: paise(r_inr)?,
        r_multiple: r_multiple(net, r_inr)?,
        mae_r: worst
            .map(|w| r_multiple(py::min(w, Decimal::ZERO), r_inr))
            .transpose()?,
        mfe_r: best
            .map(|b| r_multiple(py::max(b, Decimal::ZERO), r_inr))
            .transpose()?,
    })
}

/// `(sleeve, simulated)` — the two things `03` §6 forbids pooling.
///
/// Ports `baskfy_core.fno.journal.PoolKey`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub struct PoolKey {
    /// The sleeve.
    pub sleeve: FoSleeve,
    /// Paper or live.
    pub simulated: bool,
}

/// The single key of a set of rows, `None` when empty.
///
/// Ports `baskfy_core.fno.journal.one_pool`.
///
/// # Errors
/// `PooledRows` when the keys are mixed.
pub fn one_pool(keys: &[PoolKey]) -> FnoResult<Option<PoolKey>> {
    let found: BTreeSet<PoolKey> = keys.iter().copied().collect();
    if found.len() > 1 {
        return Err(FnoError::PooledRows(format!(
            "one figure per (sleeve, simulated); got {} keys",
            found.len()
        )));
    }
    Ok(found.into_iter().next())
}
