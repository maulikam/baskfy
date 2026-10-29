//! Port of `baskfy_core.options.costs` (`decile-blueprint/packages/core/src/baskfy_core/options/costs.py`).
//!
//! What a round trip costs, itemised, in `Decimal` (docs/options/04 §6).
//!
//! Goldens: `L1/options/charges.*` (`baskfy_core.options.costs.charges`).
//!
//! This file also carries the two pieces of Python's `decimal` arithmetic the options ports
//! need to reproduce byte for byte: `Decimal.quantize` (with its rounding mode, and the sign of
//! a zero result) and `Decimal.__truediv__` under the default context (28 significant digits,
//! `ROUND_HALF_EVEN`, the ideal exponent on an exact quotient). `rust_decimal` alone keeps a
//! 29th digit and a different scale on exact quotients, which a byte-exact golden refuses.
//!
//! `Side` and `CostRates` belong to `baskfy_core.options.config`; they live here until
//! `options::config` is ported (that file is outside this port's ownership).

use chrono::NaiveDate;
use rust_decimal::{Decimal, RoundingStrategy};
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};

/// Ports `baskfy_core.options.config.Side`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum Side {
    /// `BUY`.
    Buy,
    /// `SELL`.
    Sell,
}

/// Ports `baskfy_core.options.config.CostRates` — every statutory and broker rate as a dated
/// field (`04` §6, verified by OP0, read 22 Sep 2026). Defaults are Python's.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct CostRates {
    /// Flat brokerage per executed F&O order, ₹.
    pub brokerage_per_order_inr: Decimal,
    /// STT on the sale of an option, percent of premium.
    pub stt_sell_premium_pct: Decimal,
    /// STT on an exercised option, percent of intrinsic.
    pub stt_exercise_intrinsic_pct: Decimal,
    /// NSE options transaction charge, percent of premium.
    pub exchange_txn_pct: Decimal,
    /// SEBI turnover fee, ₹ per crore.
    pub sebi_per_crore_inr: Decimal,
    /// IPFT, ₹ per crore.
    pub ipft_per_crore_inr: Decimal,
    /// Stamp duty on the buy side, percent.
    pub stamp_buy_pct: Decimal,
    /// GST, percent of brokerage + exchange + SEBI + IPFT.
    pub gst_pct: Decimal,
    /// MIS auto square-off charge per order, ₹.
    pub auto_squareoff_inr: Decimal,
    /// Tier 2's synthetic half-spread, percent of premium.
    pub synthetic_half_spread_pct: Decimal,
    /// Tier 2's synthetic half-spread floor, ₹ per unit.
    pub synthetic_half_spread_min_inr: Decimal,
    /// The day every rate was last checked against its primary source.
    pub reviewed_on: NaiveDate,
    /// Days after `reviewed_on` before the review warning fires.
    pub review_after_days: i64,
}

impl Default for CostRates {
    fn default() -> Self {
        Self {
            brokerage_per_order_inr: Decimal::new(20, 0),
            stt_sell_premium_pct: Decimal::new(15, 2),
            stt_exercise_intrinsic_pct: Decimal::new(15, 2),
            exchange_txn_pct: Decimal::new(3553, 5),
            sebi_per_crore_inr: Decimal::new(10, 0),
            ipft_per_crore_inr: Decimal::new(1, 2),
            stamp_buy_pct: Decimal::new(3, 3),
            gst_pct: Decimal::new(18, 0),
            auto_squareoff_inr: Decimal::new(50, 0),
            synthetic_half_spread_pct: Decimal::new(15, 1),
            synthetic_half_spread_min_inr: Decimal::new(10, 2),
            // OPTIONS_COST_RATES_REVIEWED_ON = 2026-09-22; a constant date is always valid.
            reviewed_on: NaiveDate::from_ymd_opt(2026, 9, 22).unwrap_or_default(),
            review_after_days: 90,
        }
    }
}

/// One executed (or planned) order: its side, its price per unit and its units.
///
/// Ports `baskfy_core.options.costs.CostFill`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CostFill {
    /// Buy or sell.
    pub side: Side,
    /// Price per unit.
    pub price: Decimal,
    /// Units.
    pub quantity: i64,
}

impl CostFill {
    /// `price * quantity`.
    pub fn turnover(&self) -> CoreResult<Decimal> {
        mul(self.price, Decimal::from(self.quantity))
    }
}

/// The statutory charges and brokerage over a set of orders, each rounded to the paisa.
///
/// Ports `baskfy_core.options.costs.ChargeBreakdown`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChargeBreakdown {
    /// Executed orders.
    pub orders: i64,
    /// Brokerage.
    pub brokerage: Decimal,
    /// STT on the sell side's premium.
    pub stt: Decimal,
    /// Exchange transaction charge.
    pub exchange_txn: Decimal,
    /// SEBI fee.
    pub sebi: Decimal,
    /// IPFT.
    pub ipft: Decimal,
    /// Stamp duty.
    pub stamp: Decimal,
    /// GST.
    pub gst: Decimal,
}

impl ChargeBreakdown {
    /// The sum of every component (Python's `total` property).
    pub fn total(&self) -> CoreResult<Decimal> {
        [
            self.stt,
            self.exchange_txn,
            self.sebi,
            self.ipft,
            self.stamp,
            self.gst,
        ]
        .into_iter()
        .try_fold(self.brokerage, add)
    }
}

const HUNDRED: Decimal = Decimal::ONE_HUNDRED;

fn crore() -> Decimal {
    Decimal::new(10_000_000, 0)
}

/// `04` §6.1: brokerage per executed order, STT on the sell side's premium, exchange
/// transaction and SEBI and IPFT on premium turnover both sides, stamp on the buy side, GST on
/// brokerage + exchange + SEBI + IPFT.
///
/// Ports `baskfy_core.options.costs.charges`.
pub fn charges(fills: &[CostFill], rates: &CostRates) -> CoreResult<ChargeBreakdown> {
    let mut sells = Decimal::ZERO;
    let mut buys = Decimal::ZERO;
    for fill in fills {
        let turnover = fill.turnover()?;
        match fill.side {
            Side::Sell => sells = add(sells, turnover)?,
            Side::Buy => buys = add(buys, turnover)?,
        }
    }
    let turnover = add(sells, buys)?;
    let orders = i64::try_from(fills.len())
        .map_err(|_| CoreError::Arithmetic("too many orders".to_string()))?;
    let brokerage = paise(mul(rates.brokerage_per_order_inr, Decimal::from(orders))?)?;
    // `paise(x / d)`: Python's 28-digit quotient, then the paisa — without materialising the
    // quotient, whose scale can exceed rust_decimal's 28 (a tiny turnover over a crore).
    let paise_of =
        |x: Decimal, d: Decimal| py_div_quantize(x, d, 2, RoundingStrategy::MidpointAwayFromZero);
    let stt = paise_of(mul(sells, rates.stt_sell_premium_pct)?, HUNDRED)?;
    let exchange = paise_of(mul(turnover, rates.exchange_txn_pct)?, HUNDRED)?;
    let sebi = paise_of(mul(turnover, rates.sebi_per_crore_inr)?, crore())?;
    let ipft = paise_of(mul(turnover, rates.ipft_per_crore_inr)?, crore())?;
    let stamp = paise_of(mul(buys, rates.stamp_buy_pct)?, HUNDRED)?;
    let gst_base = add(add(add(brokerage, exchange)?, sebi)?, ipft)?;
    let gst = paise_of(mul(gst_base, rates.gst_pct)?, HUNDRED)?;
    Ok(ChargeBreakdown {
        orders,
        brokerage,
        stt,
        exchange_txn: exchange,
        sebi,
        ipft,
        stamp,
        gst,
    })
}

// --- Python `decimal` emulation, shared by the options ports ---------------------------------

/// Python's default context precision (`decimal.getcontext().prec`).
const PY_PREC: u32 = 28;

/// Round a rupee amount to the paisa, half up.
///
/// Ports `baskfy_core.options.costs.paise` (`quantize(Decimal("0.01"), ROUND_HALF_UP)`).
pub fn paise(value: Decimal) -> CoreResult<Decimal> {
    quantize(value, 2, RoundingStrategy::MidpointAwayFromZero)
}

/// Python's `Decimal.quantize(Decimal(10) ** -places, rounding)`: exactly `places` decimals,
/// and a zero result keeps the sign of the input (`Decimal("-0.001").quantize(...)` is `-0.00`).
pub(crate) fn quantize(
    value: Decimal,
    places: u32,
    strategy: RoundingStrategy,
) -> CoreResult<Decimal> {
    let mut out = value.round_dp_with_strategy(places, strategy);
    if out.scale() < places {
        // Widening the scale is exact; it fails only past rust_decimal's 28 places or 96 bits.
        let widened = out
            .mantissa()
            .checked_mul(10_i128.pow(places - out.scale()))
            .ok_or_else(|| CoreError::Arithmetic(format!("cannot quantize {value}")))?;
        out = Decimal::try_from_i128_with_scale(widened, places)
            .map_err(|e| CoreError::Arithmetic(format!("cannot quantize {value}: {e}")))?;
    }
    if out.is_zero() {
        out.set_sign_negative(value.is_sign_negative());
    }
    Ok(out)
}

fn digits(n: u128) -> u32 {
    if n == 0 { 0 } else { n.ilog10() + 1 }
}

/// Python's `a / b` under the default context: an exact quotient at the ideal exponent
/// (`exp(a) - exp(b)`, trailing zeros removed only down to it), an inexact one rounded to 28
/// significant digits, half even.
pub(crate) fn py_div(a: Decimal, b: Decimal) -> CoreResult<Decimal> {
    let (negative, coef, scale) = py_div_parts(a, b)?;
    build(negative, coef, scale, a, b)
}

/// Python's `(a / b).quantize(Decimal(10) ** -places, rounding)`: the quotient is first
/// rounded to 28 significant digits exactly as Python's division does, then quantized, without
/// ever materialising the intermediate (whose scale may exceed rust_decimal's 28).
pub(crate) fn py_div_quantize(
    a: Decimal,
    b: Decimal,
    places: u32,
    strategy: RoundingStrategy,
) -> CoreResult<Decimal> {
    let (negative, mut coef, mut scale) = py_div_parts(a, b)?;
    let target = i64::from(places);
    if scale > target {
        let drop = scale - target;
        let (q, round_up) = if drop > 30 {
            // coef < 10^28 < half of 10^drop: everything rounds away to zero.
            (0, false)
        } else {
            let p = 10_u128.pow(u32::try_from(drop).unwrap_or(30));
            let (q, low, half) = (coef / p, coef % p, p / 2);
            let up = match strategy {
                RoundingStrategy::MidpointNearestEven => low > half || (low == half && q % 2 == 1),
                RoundingStrategy::MidpointAwayFromZero => low >= half,
                _ => {
                    return Err(CoreError::Arithmetic(format!(
                        "unsupported rounding {strategy:?}"
                    )));
                }
            };
            (q, up)
        };
        coef = if round_up { q + 1 } else { q };
        scale = target;
    }
    // Widen to exactly `places` (exact) via quantize, which also keeps a zero's sign.
    let mut out = build(negative, coef, scale, a, b)?;
    if out.is_zero() {
        out.set_sign_negative(negative);
    }
    quantize(out, places, strategy)
}

/// The sign, coefficient and scale of Python's `a / b` (see [`py_div`]).
fn py_div_parts(a: Decimal, b: Decimal) -> CoreResult<(bool, u128, i64)> {
    if b.is_zero() {
        return Err(CoreError::Arithmetic(format!("division of {a} by zero")));
    }
    let negative = a.is_sign_negative() != b.is_sign_negative();
    let ua = a.mantissa().unsigned_abs();
    let ub = b.mantissa().unsigned_abs();
    let mut coef = ua / ub;
    let mut rem = ua % ub;
    let mut frac: i64 = 0;
    // One digit beyond the precision, then the remainder as the sticky bit.
    while rem != 0 && digits(coef) <= PY_PREC {
        rem *= 10;
        coef = coef * 10 + rem / ub;
        rem %= ub;
        frac += 1;
    }
    let n = digits(coef);
    if n > PY_PREC {
        let drop = n - PY_PREC;
        let p = 10_u128.pow(drop);
        let low = coef % p;
        coef /= p;
        frac -= i64::from(drop);
        let half = p / 2;
        let round_up = low > half || (low == half && (rem != 0 || coef % 2 == 1));
        if round_up {
            coef += 1;
            if digits(coef) > PY_PREC {
                coef /= 10;
                frac -= 1;
            }
        }
    }
    let scale = frac + i64::from(a.scale()) - i64::from(b.scale());
    Ok((negative, coef, scale))
}

/// A `Decimal` from Python-division parts; errors where rust_decimal cannot hold the value.
fn build(negative: bool, coef: u128, scale: i64, a: Decimal, b: Decimal) -> CoreResult<Decimal> {
    let range = || CoreError::Arithmetic(format!("{a} / {b} out of range"));
    let (coef, scale) = if scale < 0 {
        // Python would print a positive exponent ("1E+2"); the value is kept, at scale 0.
        let up = u32::try_from(-scale).map_err(|_| range())?;
        let widened = 10_u128
            .checked_pow(up)
            .and_then(|m| coef.checked_mul(m))
            .ok_or_else(range)?;
        (widened, 0)
    } else {
        (coef, scale)
    };
    let scale = u32::try_from(scale).map_err(|_| range())?;
    let signed = i128::try_from(coef).map_err(|_| range())?;
    let mut out = Decimal::try_from_i128_with_scale(signed, scale)
        .map_err(|e| CoreError::Arithmetic(format!("{a} / {b} not representable: {e}")))?;
    out.set_sign_negative(negative);
    Ok(out)
}

/// Checked `a * b`. A zero product carries Python's sign (the XOR of the operands' signs, so
/// `Decimal("-0.00") * 65` is `-0.00`); rust_decimal would drop it.
pub(crate) fn mul(a: Decimal, b: Decimal) -> CoreResult<Decimal> {
    let mut out = a
        .checked_mul(b)
        .ok_or_else(|| CoreError::Arithmetic(format!("{a} * {b} overflowed")))?;
    if out.is_zero() {
        out.set_sign_negative(a.is_sign_negative() != b.is_sign_negative());
    }
    Ok(out)
}

/// Checked `a + b`. A zero sum carries Python's sign under the default context: negative
/// only when both operands are negative (`-0 + -0`), positive otherwise.
pub(crate) fn add(a: Decimal, b: Decimal) -> CoreResult<Decimal> {
    let mut out = a
        .checked_add(b)
        .ok_or_else(|| CoreError::Arithmetic(format!("{a} + {b} overflowed")))?;
    if out.is_zero() {
        out.set_sign_negative(a.is_sign_negative() && b.is_sign_negative());
    }
    Ok(out)
}

/// Checked `a - b`, i.e. Python's `a + (-b)`, with [`add`]'s zero sign.
pub(crate) fn sub(a: Decimal, b: Decimal) -> CoreResult<Decimal> {
    add(a, -b)
}
