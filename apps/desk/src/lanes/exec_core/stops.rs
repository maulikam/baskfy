//! Non-negotiable 4's arithmetic and non-negotiable 2's sum, as the desk computes them:
//!
//! * `baskfy_core.score.stop_from_vol` — the weekly book's vol-scaled stop, clamped to
//!   `STOP_MIN`..`STOP_MAX` (8–12%) and rounded to one decimal. Computed at plan time; the
//!   gateway only enforces that what arrives is a stop.
//! * `baskfy_execution.broker_ports.normalize_holding` / `total_quantity` — the read half of
//!   the broker surface: total = `quantity + t1_quantity + collateral_quantity`, in `Decimal`.

use rust_decimal::Decimal;

use super::gtt::py_round;

/// `app.config.STOP_MIN`.
pub const STOP_MIN: f64 = 0.08;
/// `app.config.STOP_MAX`.
pub const STOP_MAX: f64 = 0.12;
/// `app.config.STOP_VOL_MULT`.
pub const STOP_VOL_MULT: f64 = 2.2;

/// The three constants `stop_from_vol` reads (`ScoringConfig`'s stop half).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct StopConfig {
    /// `STOP_MIN`.
    pub stop_min: f64,
    /// `STOP_MAX`.
    pub stop_max: f64,
    /// `STOP_VOL_MULT`.
    pub stop_vol_mult: f64,
}

impl Default for StopConfig {
    fn default() -> Self {
        Self {
            stop_min: STOP_MIN,
            stop_max: STOP_MAX,
            stop_vol_mult: STOP_VOL_MULT,
        }
    }
}

/// `stop_from_vol(price, ann_vol, cfg)`:
/// `round(price * (1 - clip(ann_vol / sqrt(52) * mult, min, max)), 1)`.
#[must_use]
pub fn stop_from_vol(price: f64, ann_vol: f64, cfg: StopConfig) -> f64 {
    let raw = ann_vol / 52.0_f64.sqrt() * cfg.stop_vol_mult;
    // np.clip(a, lo, hi) == minimum(maximum(a, lo), hi); NaN propagates.
    let pct = if raw.is_nan() {
        raw
    } else {
        raw.max(cfg.stop_min).min(cfg.stop_max)
    };
    py_round(price * (1.0 - pct), 1)
}

/// `broker_ports.HoldingRow`: one position in Baskfy's shape.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HoldingRow {
    /// `symbol` (upper-cased).
    pub symbol: String,
    /// `exchange`.
    pub exchange: String,
    /// `quantity`.
    pub quantity: Decimal,
    /// `t1_quantity`.
    pub t1_quantity: Decimal,
    /// `collateral_quantity` (pledged).
    pub collateral_quantity: Decimal,
    /// `average_price`.
    pub average_price: Decimal,
    /// `last_price`.
    pub last_price: Option<Decimal>,
    /// `product`.
    pub product: String,
}

/// A number as `Decimal(str(x))` reads it.
#[derive(Debug, Clone, PartialEq)]
pub enum Num {
    /// An `int`.
    Int(i64),
    /// A `str`.
    Str(String),
    /// A `Decimal`.
    Dec(Decimal),
}

fn dec(n: &Num) -> Result<Decimal, String> {
    match n {
        Num::Int(i) => Ok(Decimal::from(*i)),
        Num::Dec(d) => Ok(*d),
        Num::Str(s) => s
            .trim()
            .parse::<Decimal>()
            .map_err(|_| format!("[<class 'decimal.ConversionSyntax'>] {s:?}")),
    }
}

/// `normalize_holding(...)`.
///
/// # Errors
/// A quantity or price `Decimal` refuses.
#[allow(clippy::too_many_arguments)]
pub fn normalize_holding(
    symbol: &str,
    exchange: &str,
    quantity: &Num,
    t1_quantity: &Num,
    collateral_quantity: &Num,
    average_price: &Num,
    last_price: Option<&Num>,
    product: &str,
) -> Result<HoldingRow, String> {
    Ok(HoldingRow {
        symbol: symbol.trim().to_uppercase(),
        exchange: exchange.trim().to_uppercase(),
        quantity: dec(quantity)?,
        t1_quantity: dec(t1_quantity)?,
        collateral_quantity: dec(collateral_quantity)?,
        average_price: dec(average_price)?,
        last_price: last_price.map(dec).transpose()?,
        product: product.to_owned(),
    })
}

/// `total_quantity(row)`: non-negotiable 2.
#[must_use]
pub fn total_quantity(row: &HoldingRow) -> Decimal {
    row.quantity + row.t1_quantity + row.collateral_quantity
}
