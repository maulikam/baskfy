//! Port of `baskfy_core.vbt.exits` (`decile-blueprint/packages/core/src/baskfy_core/vbt/exits.py`)
//! — the stop and the exit (`docs/vbt/04` §6). **The EMA is the exit; the stop is insurance.**
//!
//! Pure: a position and a bar in, an action out. Nothing here places, cancels or arms anything;
//! the desk does that from a plan a person confirmed.
//!
//! Money and every level are `Decimal` (house rule 9). The public API speaks
//! `rust_decimal::Decimal`; the arithmetic runs in [`WideDecimal`] — Python's `Decimal` in its
//! default context (28 significant digits, half-even, the specification's ideal exponents) — so
//! every quotient, product and tick floor rounds, and keeps its exponent, exactly where Python's
//! does (DECISIONS-RS R1.3). The [`wide`] helpers are shared with `vbt::plan` and
//! `vbt::backtest`, which walk the same arithmetic.
//!
//! Goldens: `exits.tick_floor`, `exits.initial_stop`, `exits.apply_stop`, `exits.stop_fill`,
//! `exits.manage`, `exits.r_multiple`.

use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::backtest::helpers::{Rounding, WideDecimal};
use crate::error::CoreResult;
use crate::vbt::config::ExitConfig;

/// Ports `baskfy_core.vbt.exits.ExitReason` — why a position left, or is about to.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum ExitReason {
    /// The close fell below the 21-day EMA; the sell is at the **next** session's open.
    #[serde(rename = "EMA_EXIT")]
    EmaExit,
    /// The session's low traded through the stop; the fill is the stop.
    #[serde(rename = "STOP_HIT")]
    StopHit,
    /// The session opened at or below the stop; the fill is the open.
    #[serde(rename = "STOP_GAP")]
    StopGap,
    /// The name stopped printing; written off at its last close.
    #[serde(rename = "NO_BAR")]
    NoBar,
    /// The backtest liquidating what is still open on its last session.
    #[serde(rename = "END_OF_RUN")]
    EndOfRun,
    /// A person sold it.
    #[serde(rename = "MANUAL")]
    Manual,
    /// LV9: the remainder sold at the next open on a close below the trail MA.
    #[serde(rename = "MA_TRAIL")]
    MaTrail,
    /// LV9: a third sold into strength on bars 3-5. Never a close reason.
    #[serde(rename = "PARTIAL")]
    Partial,
}

impl ExitReason {
    /// Every member, in Python's declaration order (`for reason in ExitReason`).
    pub const ALL: [Self; 8] = [
        Self::EmaExit,
        Self::StopHit,
        Self::StopGap,
        Self::NoBar,
        Self::EndOfRun,
        Self::Manual,
        Self::MaTrail,
        Self::Partial,
    ];

    /// The enum's string value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::EmaExit => "EMA_EXIT",
            Self::StopHit => "STOP_HIT",
            Self::StopGap => "STOP_GAP",
            Self::NoBar => "NO_BAR",
            Self::EndOfRun => "END_OF_RUN",
            Self::Manual => "MANUAL",
            Self::MaTrail => "MA_TRAIL",
            Self::Partial => "PARTIAL",
        }
    }
}

/// Ports `baskfy_core.vbt.exits.Action` — what one close says about one position.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Action {
    /// Nothing to do.
    #[serde(rename = "HOLD")]
    Hold,
    /// Queue a sell for the next session's open.
    #[serde(rename = "QUEUE_SELL_AT_OPEN")]
    QueueSellAtOpen,
    /// LV9: queue a partial sale (`ManageAction.quantity` shares) for the next open.
    #[serde(rename = "QUEUE_PARTIAL_AT_OPEN")]
    QueuePartialAtOpen,
    /// LV9: raise the resting stop to `ManageAction.new_stop` (breakeven).
    #[serde(rename = "RAISE_STOP")]
    RaiseStop,
    /// The stop fired.
    #[serde(rename = "STOPPED_OUT")]
    StoppedOut,
    /// Written off: the name stopped printing.
    #[serde(rename = "WRITE_OFF")]
    WriteOff,
}

/// Ports `baskfy_core.vbt.exits.OpenPosition` — what `manage` needs to know about a position.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OpenPosition {
    /// The instrument.
    pub instrument_id: i64,
    /// The fill session.
    pub entry_date: NaiveDate,
    /// The fill price.
    pub entry_price: Decimal,
    /// Shares open.
    pub quantity: i64,
    /// The stop in force.
    pub stop_price: Decimal,
    /// The stop at entry (the R denominator).
    pub initial_stop: Decimal,
    /// The session a queued exit sells at, if any.
    #[serde(default)]
    pub exit_queued_for: Option<NaiveDate>,
}

/// Ports `baskfy_core.exits.qulla.OhlcBar` as far as `vbt.exits.Bar.history` carries it: one
/// session of one name, exchange prices. `vbt.exits.manage` never reads it (R2.3).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OhlcBar {
    /// The session.
    pub date: NaiveDate,
    /// Open.
    pub open: Decimal,
    /// High.
    pub high: Decimal,
    /// Low.
    pub low: Decimal,
    /// Close.
    pub close: Decimal,
}

/// Ports `baskfy_core.vbt.exits.Bar` — one session for one instrument. `close == None` means
/// the name did not print.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Bar {
    /// The session.
    pub session: NaiveDate,
    /// Open.
    pub open: Option<Decimal>,
    /// High.
    pub high: Option<Decimal>,
    /// Low.
    pub low: Option<Decimal>,
    /// Close.
    pub close: Option<Decimal>,
    /// The exit average (the 21-day EMA).
    pub ema_exit: Option<Decimal>,
    /// LV9: the exchange-price history ending on this session; empty for the EMA exit.
    #[serde(default)]
    pub history: Vec<OhlcBar>,
}

/// Ports `baskfy_core.vbt.exits.ManageAction` — what to do with one position after one close.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ManageAction {
    /// The action.
    pub action: Action,
    /// Why, when it is an exit.
    pub reason: Option<ExitReason>,
    /// The price the exit is expected at; `None` for a hold and a queued sell.
    #[serde(default)]
    pub price: Option<Decimal>,
    /// A sentence for the plan.
    #[serde(default)]
    pub note: String,
    /// LV9: the shares a partial sells.
    #[serde(default)]
    pub quantity: Option<i64>,
    /// LV9: the stop a `RAISE_STOP` raises to.
    #[serde(default)]
    pub new_stop: Option<Decimal>,
}

impl ManageAction {
    fn new(
        action: Action,
        reason: Option<ExitReason>,
        price: Option<Decimal>,
        note: String,
    ) -> Self {
        Self {
            action,
            reason,
            price,
            note,
            quantity: None,
            new_stop: None,
        }
    }
}

/// Python's `Decimal` arithmetic shared by the VBT ports (`exits`, `plan`, `backtest`).
pub(crate) mod wide {
    use std::str::FromStr;

    use rust_decimal::Decimal;

    use crate::backtest::helpers::{Rounding, WideDecimal};
    use crate::error::{CoreError, CoreResult};

    /// A decimal failure as the crate's arithmetic error.
    pub(crate) fn arith(e: impl std::fmt::Display) -> CoreError {
        CoreError::Arithmetic(e.to_string())
    }

    /// `Decimal(n)`.
    pub(crate) fn int(n: i64) -> WideDecimal {
        WideDecimal::from(n)
    }

    /// The same value as a `rust_decimal::Decimal` (scale kept).
    pub(crate) fn to_dec(x: &WideDecimal) -> CoreResult<Decimal> {
        x.try_to_decimal().map_err(arith)
    }

    /// `x.to_integral_value(rounding=mode)`: an integral exponent is left as it is.
    pub(crate) fn to_integral(x: &WideDecimal, mode: Rounding) -> CoreResult<WideDecimal> {
        if x.exponent() >= 0 {
            return Ok(x.clone());
        }
        x.quantize(0, mode).map_err(arith)
    }

    /// `a / b` in the default context.
    pub(crate) fn div(a: &WideDecimal, b: &WideDecimal) -> CoreResult<WideDecimal> {
        a.div(b).map_err(arith)
    }

    /// Python's `min(a, b)`: `a` unless `b` is strictly smaller.
    pub(crate) fn py_min(a: &WideDecimal, b: &WideDecimal) -> WideDecimal {
        if b < a { b.clone() } else { a.clone() }
    }

    /// `str(d)` for a `rust_decimal::Decimal` — Python's to-scientific-string (`"1E-7"`).
    pub(crate) fn py_str(d: Decimal) -> String {
        WideDecimal::from(d).to_sci_string()
    }

    /// `float(d)`: the correctly rounded double of the decimal's digits.
    pub(crate) fn to_f64(x: &WideDecimal) -> CoreResult<f64> {
        let text = x.to_sci_string();
        text.parse::<f64>()
            .map_err(|e| CoreError::InvalidInput(format!("float({text}): {e}")))
    }

    /// Python's `repr(float)`: the shortest round-tripping digits, fixed notation for decimal
    /// exponents in `[-4, 16)` (always with a fractional part, `"12.0"`), scientific otherwise
    /// (`"1e-05"`, `"1.5e+16"`).
    pub(crate) fn py_float_repr(x: f64) -> String {
        if x.is_nan() {
            return "nan".to_owned();
        }
        if x.is_infinite() {
            return if x > 0.0 { "inf" } else { "-inf" }.to_owned();
        }
        let sci = format!("{x:e}");
        let (mantissa, exp) = sci.split_once('e').unwrap_or((sci.as_str(), "0"));
        let exp: i64 = exp.parse().unwrap_or(0);
        let (sign, mantissa) = match mantissa.strip_prefix('-') {
            Some(m) => ("-", m),
            None => ("", mantissa),
        };
        let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
        let n = i64::try_from(digits.len()).unwrap_or(i64::MAX);
        if (-4..16).contains(&exp) {
            if exp >= 0 {
                let int_len = exp + 1;
                if n <= int_len {
                    let pad = usize::try_from(int_len - n).unwrap_or(0);
                    format!("{sign}{digits}{}.0", "0".repeat(pad))
                } else {
                    let cut = usize::try_from(int_len).unwrap_or(0);
                    format!("{sign}{}.{}", &digits[..cut], &digits[cut..])
                }
            } else {
                let pad = usize::try_from(-exp - 1).unwrap_or(0);
                format!("{sign}0.{}{digits}", "0".repeat(pad))
            }
        } else {
            let (head, tail) = digits.split_at(1);
            let point = if tail.is_empty() {
                String::new()
            } else {
                format!(".{tail}")
            };
            let exp_sign = if exp < 0 { "-" } else { "+" };
            format!("{sign}{head}{point}e{exp_sign}{:02}", exp.abs())
        }
    }

    /// `Decimal(str(x))` for a config float. A non-finite float is refused (Python would carry
    /// `Decimal('Infinity')`/`NaN` on, which no golden or real config does).
    pub(crate) fn from_py_float_str(x: f64) -> CoreResult<WideDecimal> {
        if !x.is_finite() {
            return Err(CoreError::InvalidInput(format!(
                "Decimal(str({})) is not finite",
                py_float_repr(x)
            )));
        }
        WideDecimal::from_str(&py_float_repr(x)).map_err(arith)
    }

    /// `Decimal(x)` for a float: its exact binary value (`n * 5**k` at exponent `-k`, as
    /// `Decimal.from_float` builds it). A non-finite float is refused.
    pub(crate) fn from_f64_exact(x: f64) -> CoreResult<WideDecimal> {
        if !x.is_finite() {
            return Err(CoreError::InvalidInput(format!(
                "Decimal({}) is not finite",
                py_float_repr(x)
            )));
        }
        let sign = if x.is_sign_negative() { "-" } else { "" };
        if x.to_bits() << 1 == 0 {
            return WideDecimal::from_str(&format!("{sign}0")).map_err(arith);
        }
        let bits = x.to_bits();
        let biased = i64::try_from((bits >> 52) & 0x7ff).unwrap_or(0);
        let fraction = bits & ((1_u64 << 52) - 1);
        let (mut mantissa, mut exp2) = if biased == 0 {
            (fraction, -1074_i64)
        } else {
            (fraction | (1_u64 << 52), biased - 1075)
        };
        while mantissa & 1 == 0 && exp2 < 0 {
            mantissa >>= 1;
            exp2 += 1;
        }
        let mut limbs = Limbs::from_u64(mantissa);
        let text = if exp2 >= 0 {
            let mut left = exp2;
            while left > 0 {
                let step = left.min(30);
                limbs.mul_small(1_u64 << step);
                left -= step;
            }
            format!("{sign}{}", limbs.digits())
        } else {
            let k = -exp2;
            let mut left = k;
            while left > 0 {
                let step = left.min(13);
                limbs.mul_small(5_u64.pow(u32::try_from(step).unwrap_or(0)));
                left -= step;
            }
            format!("{sign}{}E-{k}", limbs.digits())
        };
        WideDecimal::from_str(&text).map_err(arith)
    }

    /// A tiny unsigned big integer (base 10^9 limbs, little-endian) for `from_f64_exact`.
    struct Limbs(Vec<u64>);

    const BASE: u64 = 1_000_000_000;

    impl Limbs {
        fn from_u64(mut v: u64) -> Self {
            let mut limbs = Vec::new();
            while v > 0 {
                limbs.push(v % BASE);
                v /= BASE;
            }
            Self(limbs)
        }

        /// `self *= m` for `m < 2^31`.
        fn mul_small(&mut self, m: u64) {
            let mut carry = 0_u64;
            for limb in &mut self.0 {
                let v = *limb * m + carry;
                *limb = v % BASE;
                carry = v / BASE;
            }
            while carry > 0 {
                self.0.push(carry % BASE);
                carry /= BASE;
            }
        }

        fn digits(&self) -> String {
            let mut out = String::new();
            for (i, limb) in self.0.iter().rev().enumerate() {
                if i == 0 {
                    out.push_str(&limb.to_string());
                } else {
                    out.push_str(&format!("{limb:09}"));
                }
            }
            if out.is_empty() {
                out.push('0');
            }
            out
        }
    }

    /// `tick_floor` in wide decimals: `(price / tick).to_integral_value(ROUND_DOWN) * tick`.
    pub(crate) fn tick_floor(price: &WideDecimal, tick: &WideDecimal) -> CoreResult<WideDecimal> {
        Ok(to_integral(&div(price, tick)?, Rounding::Down)?.mul(tick))
    }
}

/// Ports `baskfy_core.vbt.exits.tick_floor`: round **down** (towards zero) to the tick.
///
/// # Errors
/// A zero tick (Python's `DivisionByZero`), or a result `rust_decimal` cannot hold.
pub fn tick_floor(price: Decimal, tick: Decimal) -> CoreResult<Decimal> {
    wide::to_dec(&wide::tick_floor(
        &WideDecimal::from(price),
        &WideDecimal::from(tick),
    )?)
}

/// `initial_stop` in wide decimals: `fill x (1 - Decimal(str(stop_pct)) / 100)`, floored.
pub(crate) fn initial_stop_wide(
    fill: &WideDecimal,
    config: &ExitConfig,
    tick: &WideDecimal,
) -> CoreResult<WideDecimal> {
    let fraction = wide::div(&wide::from_py_float_str(config.stop_pct)?, &wide::int(100))?;
    let raw = fill.mul(&wide::int(1).sub(&fraction));
    wide::tick_floor(&raw, tick)
}

/// Ports `baskfy_core.vbt.exits.initial_stop`: `stop_pct` below **the fill**, floored to the
/// tick (`04` §6.1).
///
/// # Errors
/// A zero tick, a non-finite `stop_pct`, or a result `rust_decimal` cannot hold.
pub fn initial_stop(
    fill_price: Decimal,
    config: &ExitConfig,
    tick: Decimal,
) -> CoreResult<Decimal> {
    wide::to_dec(&initial_stop_wide(
        &WideDecimal::from(fill_price),
        config,
        &WideDecimal::from(tick),
    )?)
}

/// Ports `baskfy_core.vbt.exits.apply_stop`: a stop never falls — Python's `max(current,
/// proposed)`, which keeps `current` on a tie (so its scale).
#[must_use]
pub fn apply_stop(current: Decimal, proposed: Decimal) -> Decimal {
    if proposed > current {
        proposed
    } else {
        current
    }
}

/// Ports `baskfy_core.vbt.exits.stop_fill`: the open first, then the low (`04` §6.4); `None`
/// when the stop was not reached.
#[must_use]
pub fn stop_fill(bar: &Bar, stop: Decimal) -> Option<(ExitReason, Decimal)> {
    if let Some(open) = bar.open
        && open <= stop
    {
        return Some((ExitReason::StopGap, open));
    }
    if let Some(low) = bar.low
        && low <= stop
    {
        return Some((ExitReason::StopHit, stop));
    }
    None
}

/// Ports `baskfy_core.vbt.exits.manage`: what one close says about one position (`04` §6.6's
/// precedence, top-down) — no bar for `no_bar_tolerance_sessions` → write off; the stop; a
/// close below the exit EMA → queue a sell for the next open; otherwise hold. The EMA is never
/// evaluated on a null.
#[must_use]
pub fn manage(
    position: &OpenPosition,
    bar: &Bar,
    blank_sessions: i64,
    config: &ExitConfig,
) -> ManageAction {
    let Some(close) = bar.close else {
        if blank_sessions >= config.no_bar_tolerance_sessions {
            return ManageAction::new(
                Action::WriteOff,
                Some(ExitReason::NoBar),
                None,
                format!("no bar for {blank_sessions} sessions"),
            );
        }
        return ManageAction::new(Action::Hold, None, None, "no bar today".to_owned());
    };

    if let Some((reason, price)) = stop_fill(bar, position.stop_price) {
        return ManageAction::new(
            Action::StoppedOut,
            Some(reason),
            Some(price),
            format!("stop {}", wide::py_str(position.stop_price)),
        );
    }

    if let Some(ema) = bar.ema_exit
        && close < ema
    {
        return ManageAction::new(
            Action::QueueSellAtOpen,
            Some(ExitReason::EmaExit),
            None,
            format!(
                "close {} below the {}-day EMA {}",
                wide::py_str(close),
                config.trail_ema_bars,
                wide::py_str(ema)
            ),
        );
    }
    ManageAction::new(
        Action::Hold,
        None,
        None,
        "above the EMA, stop intact".to_owned(),
    )
}

/// Ports `baskfy_core.vbt.exits.r_multiple`: `(exit - entry) / (entry - initial stop)`, two
/// places (half-even); `None` when the risk was not positive.
///
/// # Errors
/// A quotient Python's `quantize` would refuse (more than 28 digits), or one `rust_decimal`
/// cannot hold.
pub fn r_multiple(
    entry: Decimal,
    initial: Decimal,
    exit_price: Decimal,
) -> CoreResult<Option<Decimal>> {
    let (entry, initial, exit_price) = (
        WideDecimal::from(entry),
        WideDecimal::from(initial),
        WideDecimal::from(exit_price),
    );
    let risk = entry.sub(&initial);
    if risk <= WideDecimal::zero() {
        return Ok(None);
    }
    let r = wide::div(&exit_price.sub(&entry), &risk)?
        .quantize(-2, Rounding::HalfEven)
        .map_err(wide::arith)?;
    wide::to_dec(&r).map(Some)
}
