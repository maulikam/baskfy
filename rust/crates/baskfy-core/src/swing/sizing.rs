//! Port of `baskfy_core.swing.sizing` (`decile-blueprint/packages/core/src/baskfy_core/swing/sizing.py`).
//!
//! Risk per trade to share count (docs/swing/04 §5). Money is `Decimal` (house rule 9).
//!
//! Goldens: `L1/swing/size_position.*` (`baskfy_core.swing.sizing.size_position`).
//!
//! The share count is the floor of the risk-derived quantity, then the smallest of four caps,
//! and the cap that bound is *named* in the result so the plan can say "120 shares, capped by
//! the 20% position limit" rather than presenting a number as if it were the risk-derived one.

use std::str::FromStr;

use rust_decimal::{Decimal, RoundingStrategy};
use serde::{Deserialize, Deserializer, Serialize};

use crate::error::{CoreError, CoreResult};

/// Ports `baskfy_core.swing.config.SizingConfig` — risk per trade decides survival
/// (docs/swing/04 §5). It lives here until `swing::config` is ported (that file is outside this
/// port's ownership); missing keys take Python's defaults.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct SizingConfig {
    /// Percent of equity one full stop-out may lose.
    pub risk_per_trade_pct: f64,
    /// LV10: a fresh qualifying setup in a held name is a new entry.
    pub pyramiding: bool,
    /// At most this many open entries in one name.
    pub max_entries_per_name: i64,
    /// A position may not exceed this percent of equity.
    pub max_position_pct: f64,
    /// The most positions open at once.
    pub max_open_positions: i64,
    /// New entries per session.
    pub max_new_entries_per_session: i64,
    /// A position may not exceed this fraction of the name's average daily turnover.
    pub max_position_vs_turnover: f64,
    /// Below this the brokerage dominates the edge. Rupees, `Decimal` (house rule 9); a golden
    /// written while Python still declared it a float carries a JSON number, read exactly as
    /// Python's comparison reads it.
    #[serde(deserialize_with = "decimal_or_number")]
    pub min_trade_value_inr: Decimal,
    /// "Start small": the first live sessions plan at this multiple of the risk.
    pub risk_multiplier_first_live: f64,
    /// How many sessions "first live" lasts.
    pub first_live_sessions: i64,
}

impl Default for SizingConfig {
    fn default() -> Self {
        Self {
            risk_per_trade_pct: 0.5,
            pyramiding: true,
            max_entries_per_name: 2,
            max_position_pct: 20.0,
            max_open_positions: 10,
            max_new_entries_per_session: 3,
            max_position_vs_turnover: 0.01,
            min_trade_value_inr: Decimal::from(10_000),
            risk_multiplier_first_live: 0.5,
            first_live_sessions: 5,
        }
    }
}

/// A `Decimal` from a JSON string (`"10000"`) or number (`10000.0`, read as `Decimal(str(x))`).
fn decimal_or_number<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Decimal, D::Error> {
    struct Visitor;
    impl serde::de::Visitor<'_> for Visitor {
        type Value = Decimal;
        fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            f.write_str("a decimal string or a number")
        }
        fn visit_str<E: serde::de::Error>(self, s: &str) -> Result<Decimal, E> {
            Decimal::from_str(s).map_err(E::custom)
        }
        fn visit_f64<E: serde::de::Error>(self, x: f64) -> Result<Decimal, E> {
            decimal_from_py_float(x).map_err(E::custom)
        }
        fn visit_i64<E: serde::de::Error>(self, x: i64) -> Result<Decimal, E> {
            Ok(Decimal::from(x))
        }
        fn visit_u64<E: serde::de::Error>(self, x: u64) -> Result<Decimal, E> {
            Ok(Decimal::from(x))
        }
    }
    deserializer.deserialize_any(Visitor)
}

/// Which limit decided the quantity.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SizeCap {
    /// The risk budget over the stop distance.
    Risk,
    /// The position-percent cap.
    PositionPct,
    /// The share of the name's average daily turnover.
    Turnover,
    /// The cash on hand.
    Cash,
}

/// Why no position at all.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SizeRefusal {
    /// The stop is at or above the entry (or the entry is not positive).
    StopNotBelowEntry,
    /// The stop is further away than the method tolerates.
    StopTooWide,
    /// The sized position is worth less than `min_trade_value_inr`.
    BelowMinTradeValue,
    /// Equity is not positive.
    NoEquity,
}

/// One sized entry. `quantity == 0` always carries a `refusal`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SizedPosition {
    /// Shares.
    pub quantity: i64,
    /// The entry price.
    pub entry: Decimal,
    /// The stop price.
    pub stop: Decimal,
    /// One R in rupees, two decimals.
    pub risk_inr: Decimal,
    /// `entry x quantity`, two decimals.
    pub position_value: Decimal,
    /// The position as a percent of equity, two decimals.
    pub position_pct: Decimal,
    /// The stop's distance below the entry, in percent, two decimals.
    pub stop_distance_pct: Decimal,
    /// The cap that bound, or `None` on a refusal.
    pub cap: Option<SizeCap>,
    /// Why there is no position, or `None` when there is one.
    pub refusal: Option<SizeRefusal>,
}

impl SizedPosition {
    /// One R in rupees: what a full stop-out loses.
    pub fn r_value(&self) -> Decimal {
        self.risk_inr
    }
}

/// Python's `Decimal(str(x))` for a config float: `repr(x)`'s digits and scale (`1.0` is
/// `Decimal("1.0")`, not `"1"` — DECISIONS-RS R3.2), through [`crate::py::decimal_from_float_str`].
pub(crate) fn decimal_from_py_float(x: f64) -> CoreResult<Decimal> {
    crate::py::decimal_from_float_str(x).map_err(|e| CoreError::InvalidInput(e.0))
}

/// A config float as Python's `Decimal(str(x))` sees it.
///
/// Python's decimal has no practical range limit and carries infinities; `rust_decimal` stops at
/// about 7.9e28. A finite float beyond that range, or an infinity, is kept as [`Self::Beyond`]
/// so a comparison against it can still answer as Python does (`x >= Decimal("Infinity")` is
/// simply false there), instead of failing the whole call.
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) enum ConfigNumber {
    /// Representable exactly as Python's `Decimal(str(x))`.
    Finite(Decimal),
    /// Beyond `Decimal`'s range: `infinite` for `±inf`, otherwise a finite but huge float.
    Beyond {
        /// The sign.
        positive: bool,
        /// True for `±inf` (Python raises on `0 x inf` and on `int(inf)`).
        infinite: bool,
    },
}

/// [`ConfigNumber`] for `x`. NaN is an error: every Python path that reads one raises
/// (`InvalidOperation` on an ordering comparison, `ValueError` on `int(NaN)`).
pub(crate) fn config_number(x: f64) -> CoreResult<ConfigNumber> {
    if x.is_nan() {
        return Err(CoreError::InvalidInput(
            "NaN is not a usable config number".to_owned(),
        ));
    }
    if x.is_infinite() {
        return Ok(ConfigNumber::Beyond {
            positive: x > 0.0,
            infinite: true,
        });
    }
    Ok(match decimal_from_py_float(x) {
        Ok(d) => ConfigNumber::Finite(d),
        // A finite float only fails to parse when it is too large (a tiny one rounds).
        Err(_) => ConfigNumber::Beyond {
            positive: x > 0.0,
            infinite: false,
        },
    })
}

/// Python's `d.quantize(Decimal("0.01"))` under the default context (ROUND_HALF_EVEN).
///
/// `quantize` keeps the operand's sign even when the result is zero: `Decimal("-0.001")`
/// quantizes to `Decimal("-0.00")`, which golden.py writes as `"-0.00"`. `round_dp` drops that
/// sign, so it is put back (a stop a paisa above the entry is refused with a stop distance of
/// `"-0.00"` in Python).
pub(crate) fn quantize_cents(value: Decimal) -> Decimal {
    let mut out = value.round_dp_with_strategy(2, RoundingStrategy::MidpointNearestEven);
    out.rescale(2);
    if value.is_sign_negative() {
        out.set_sign_negative(true);
    }
    out
}

pub(crate) fn checked(value: Option<Decimal>, what: &str) -> CoreResult<Decimal> {
    value.ok_or_else(|| CoreError::Arithmetic(format!("{what} overflowed or divided by zero")))
}

/// One size cap's share count. Python's is an unbounded `int`; one beyond `i64` either way
/// keeps its sign, which is all `min(caps)` and `quantity <= 0` need from it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum CapQuantity {
    /// A huge negative share count (a negative config beyond `Decimal`'s range).
    BelowRange,
    /// `int(d.to_integral_value(rounding=ROUND_DOWN))`.
    Shares(i64),
    /// A huge positive share count: this cap never binds while another is in range.
    AboveRange,
}

fn beyond(positive: bool) -> CapQuantity {
    if positive {
        CapQuantity::AboveRange
    } else {
        CapQuantity::BelowRange
    }
}

/// `int((base x factor / d1 / d2 ...).to_integral_value(rounding=ROUND_DOWN))`, where every
/// divisor is positive (the callers guarantee it) and an intermediate beyond `Decimal`'s range
/// keeps only its sign. An infinite factor is Python's `OverflowError` from `int(Infinity)`.
fn cap_quantity(
    base: Decimal,
    factor: Option<ConfigNumber>,
    divisors: &[Decimal],
) -> CoreResult<CapQuantity> {
    let mut value = base;
    match factor {
        None => {}
        Some(ConfigNumber::Finite(f)) => match value.checked_mul(f) {
            Some(v) => value = v,
            None => {
                let positive = value.is_sign_positive() == f.is_sign_positive();
                return Ok(beyond(positive));
            }
        },
        Some(ConfigNumber::Beyond { infinite: true, .. }) => {
            return Err(CoreError::Arithmetic(
                "an infinite size cap cannot be a share count".to_owned(),
            ));
        }
        Some(ConfigNumber::Beyond { positive, .. }) => {
            if value.is_zero() {
                // Python: 0 x 1e30 is 0.
                value = Decimal::ZERO;
            } else {
                return Ok(beyond(positive == value.is_sign_positive()));
            }
        }
    }
    for divisor in divisors {
        match value.checked_div(*divisor) {
            Some(v) => value = v,
            None if divisor.is_zero() => {
                return Err(CoreError::Arithmetic("size cap divided by zero".to_owned()));
            }
            None => {
                return Ok(beyond(
                    value.is_sign_positive() == divisor.is_sign_positive(),
                ));
            }
        }
    }
    Ok(match i64::try_from(value.trunc()) {
        Ok(shares) => CapQuantity::Shares(shares),
        Err(_) => beyond(value.is_sign_positive()),
    })
}

const PCT: Decimal = Decimal::ONE_HUNDRED;

fn pct(numerator: Decimal, denominator: Decimal) -> CoreResult<Decimal> {
    if denominator <= Decimal::ZERO {
        return Ok(Decimal::ZERO);
    }
    let ratio = checked(numerator.checked_div(denominator), "percent")?;
    Ok(quantize_cents(checked(ratio.checked_mul(PCT), "percent")?))
}

fn refuse(entry: Decimal, stop: Decimal, why: SizeRefusal) -> CoreResult<SizedPosition> {
    let stop_distance_pct = if entry > Decimal::ZERO {
        pct(checked(entry.checked_sub(stop), "stop distance")?, entry)?
    } else {
        Decimal::ZERO
    };
    Ok(SizedPosition {
        quantity: 0,
        entry,
        stop,
        risk_inr: Decimal::ZERO,
        position_value: Decimal::ZERO,
        position_pct: Decimal::ZERO,
        stop_distance_pct,
        cap: None,
        refusal: Some(why),
    })
}

/// The share count for one entry.
///
/// `quantity = floor(equity x risk% / (entry - stop))`, then the minimum of that and the
/// position-percent cap, the turnover cap (a position no larger than
/// `max_position_vs_turnover` of the name's average daily turnover) and the cash on hand.
/// `max_stop_distance_pct` is the widest stop the method tolerates: a stop 14% away is not a
/// swing-trade stop, and shrinking the size to fit it would only disguise that.
///
/// On a tie between caps the first in Python's order wins: RISK, POSITION_PCT, CASH, TURNOVER.
///
/// # Errors
///
/// [`CoreError::Arithmetic`] on a decimal overflow; [`CoreError::InvalidInput`] for a
/// non-finite config float.
pub fn size_position(
    equity: Decimal,
    cash_available: Decimal,
    entry: Decimal,
    stop: Decimal,
    avg_turnover_inr: Option<Decimal>,
    config: &SizingConfig,
    max_stop_distance_pct: Decimal,
) -> CoreResult<SizedPosition> {
    if equity <= Decimal::ZERO {
        return refuse(entry, stop, SizeRefusal::NoEquity);
    }
    if stop >= entry || entry <= Decimal::ZERO {
        return refuse(entry, stop, SizeRefusal::StopNotBelowEntry);
    }
    let distance = checked(entry.checked_sub(stop), "stop distance")?;
    let distance_pct = pct(distance, entry)?;
    if distance_pct > max_stop_distance_pct {
        return refuse(entry, stop, SizeRefusal::StopTooWide);
    }

    let by_risk = cap_quantity(
        equity,
        Some(config_number(config.risk_per_trade_pct)?),
        &[PCT, distance],
    )?;
    let by_position = cap_quantity(
        equity,
        Some(config_number(config.max_position_pct)?),
        &[PCT, entry],
    )?;
    let by_cash = cap_quantity(cash_available, None, &[entry])?;
    let mut caps = vec![
        (by_risk, SizeCap::Risk),
        (by_position, SizeCap::PositionPct),
        (by_cash, SizeCap::Cash),
    ];
    if let Some(turnover) = avg_turnover_inr.filter(|t| *t > Decimal::ZERO) {
        let by_turnover = cap_quantity(
            turnover,
            Some(config_number(config.max_position_vs_turnover)?),
            &[entry],
        )?;
        caps.push((by_turnover, SizeCap::Turnover));
    }

    // Python's `min(caps, key=...)`: the first of the smallest wins.
    let mut chosen = (by_risk, SizeCap::Risk);
    for candidate in caps {
        if candidate.0 < chosen.0 {
            chosen = candidate;
        }
    }
    let (quantity, cap) = match chosen {
        (CapQuantity::Shares(q), cap) => (q, cap),
        // Python's quantity is a huge negative int: `quantity <= 0` refuses before anything else.
        (CapQuantity::BelowRange, _) => {
            return refuse(entry, stop, SizeRefusal::BelowMinTradeValue);
        }
        // Every cap is beyond i64 shares; Python would size a position no account can hold.
        (CapQuantity::AboveRange, _) => {
            return Err(CoreError::Arithmetic(
                "every size cap is beyond a representable share count".to_owned(),
            ));
        }
    };
    let value = checked(entry.checked_mul(Decimal::from(quantity)), "position value")?;
    if quantity <= 0 || value < config.min_trade_value_inr {
        return refuse(entry, stop, SizeRefusal::BelowMinTradeValue);
    }
    Ok(SizedPosition {
        quantity,
        entry,
        stop,
        risk_inr: quantize_cents(checked(
            distance.checked_mul(Decimal::from(quantity)),
            "risk",
        )?),
        position_value: quantize_cents(value),
        position_pct: pct(value, equity)?,
        stop_distance_pct: distance_pct,
        cap: Some(cap),
        refusal: None,
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use std::str::FromStr;

    use super::*;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).unwrap()
    }

    fn sized(config: &SizingConfig, stop: &str) -> CoreResult<SizedPosition> {
        size_position(
            d("1000000"),
            d("1000000"),
            d("100"),
            d(stop),
            Some(d("500000000")),
            config,
            d("10"),
        )
    }

    /// Python: `Decimal("-0.001").quantize(Decimal("0.01"))` is `Decimal("-0.00")`.
    #[test]
    fn a_stop_a_paisa_above_the_entry_reports_negative_zero_distance() {
        let out = sized(&SizingConfig::default(), "100.001").unwrap();
        assert_eq!(out.refusal, Some(SizeRefusal::StopNotBelowEntry));
        assert_eq!(out.stop_distance_pct.to_string(), "-0.00");
    }

    /// Values checked against the Python function: a cap config beyond `Decimal`'s range never
    /// binds, and an infinite one raises.
    #[test]
    fn a_cap_beyond_decimal_range_never_binds() {
        let risk = SizingConfig {
            risk_per_trade_pct: 1e30,
            ..SizingConfig::default()
        };
        let out = sized(&risk, "96").unwrap();
        assert_eq!((out.quantity, out.cap), (2000, Some(SizeCap::PositionPct)));
        let position = SizingConfig {
            max_position_pct: 1e30,
            ..SizingConfig::default()
        };
        let out = sized(&position, "96").unwrap();
        assert_eq!((out.quantity, out.cap), (1250, Some(SizeCap::Risk)));
        let below = SizingConfig {
            max_position_pct: -1e30,
            ..SizingConfig::default()
        };
        let out = sized(&below, "96").unwrap();
        assert_eq!(out.refusal, Some(SizeRefusal::BelowMinTradeValue));
        for bad in [f64::INFINITY, f64::NAN] {
            let config = SizingConfig {
                max_position_vs_turnover: bad,
                ..SizingConfig::default()
            };
            assert!(sized(&config, "96").is_err());
        }
    }
}
