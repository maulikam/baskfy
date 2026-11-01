//! Port of `baskfy_core.twt.exits` — the two stops, and there are exactly two (`docs/twt/04`
//! §7).
//!
//! Pure: a position and a bar in, an action out. Nothing here places, cancels or arms anything.
//!
//! Money and every level are `Decimal` (house rule 9). The public API speaks
//! `rust_decimal::Decimal`; the arithmetic runs in
//! [`WideDecimal`](crate::decimal::WideDecimal) — Python's `Decimal` in its default
//! context — so every product, quotient and tick floor rounds (and keeps its exponent) exactly
//! where Python's does. The backtest reuses the wide core directly (the `wide` items below), so
//! the study and the book walk the same arithmetic.

use std::fmt;
use std::str::FromStr;

use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::decimal::{Rounding, WideDecimal};
use crate::error::{CoreError, CoreResult};
use crate::twt::config::{ExitConfig, TICK_INR};

/// Ports `ExitReason`: why a position left, or is about to (`03` §5's `close_reason`). There is
/// no `EMA_EXIT` and no `TIME_EXIT`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum ExitReason {
    /// The session's low traded through the stop; the fill is the stop.
    #[serde(rename = "STOP_HIT")]
    StopHit,
    /// The session opened at or below the stop; the fill is the open.
    #[serde(rename = "STOP_GAP")]
    StopGap,
    /// `04` §7.4 — the entry session's own low reached the initial stop.
    #[serde(rename = "STOP_DAY0")]
    StopDay0,
    /// The name stopped printing; written off at its last close.
    #[serde(rename = "NO_BAR")]
    NoBar,
    /// The backtest liquidating what is still open on its last session.
    #[serde(rename = "END_OF_RUN")]
    EndOfRun,
    /// A person sold it.
    #[serde(rename = "MANUAL")]
    Manual,
}

impl ExitReason {
    /// Every member, in Python's declaration order (`for reason in ExitReason`).
    pub const ALL: [Self; 6] = [
        Self::StopHit,
        Self::StopGap,
        Self::StopDay0,
        Self::NoBar,
        Self::EndOfRun,
        Self::Manual,
    ];

    /// The enum's string value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::StopHit => "STOP_HIT",
            Self::StopGap => "STOP_GAP",
            Self::StopDay0 => "STOP_DAY0",
            Self::NoBar => "NO_BAR",
            Self::EndOfRun => "END_OF_RUN",
            Self::Manual => "MANUAL",
        }
    }
}

/// Ports `Action`: what one close says about one position. Three, and no fourth.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Action {
    /// Nothing to do.
    #[serde(rename = "HOLD")]
    Hold,
    /// The stop fired.
    #[serde(rename = "STOPPED_OUT")]
    StoppedOut,
    /// No bar for `no_bar_sessions`: written off at the last close.
    #[serde(rename = "WRITE_OFF")]
    WriteOff,
}

/// Ports `OpenPosition`: what `manage` and `ratchet` need to know.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OpenPosition {
    /// The instrument.
    pub instrument_id: i64,
    /// The session the position was filled in.
    pub entry_date: NaiveDate,
    /// The exchange's fill price, not the cost-inclusive book entry (`04` §5.3).
    pub fill_price: Decimal,
    /// Shares.
    pub quantity: i64,
    /// The stop resting at the exchange right now.
    pub stop_price: Decimal,
    /// What the stop was at entry.
    pub initial_stop: Decimal,
    /// The highest high since entry, an exchange print.
    pub high_since: Decimal,
}

/// Ports `Bar`: one session for one instrument. `close == None` means the name did not print.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Bar {
    /// The session.
    pub session: NaiveDate,
    /// The open.
    pub open: Option<Decimal>,
    /// The high.
    pub high: Option<Decimal>,
    /// The low.
    pub low: Option<Decimal>,
    /// The close.
    pub close: Option<Decimal>,
}

/// Ports `ManageAction`: what to do with one position after one close.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ManageAction {
    /// The action.
    pub action: Action,
    /// Why, for an exit.
    pub reason: Option<ExitReason>,
    /// The price the exit is expected at; `None` for a hold.
    pub price: Option<Decimal>,
    /// A human note.
    pub note: String,
}

/// Ports `Ratchet`: one session's trailing-stop arithmetic (`04` §7.2), and whether it is a
/// line.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Ratchet {
    /// `max(high_since, session high)`.
    pub high_since: Decimal,
    /// `max(stop in force, trail)`.
    pub raw_trigger: Decimal,
    /// The clamped, tick-floored trigger.
    pub next_trigger: Decimal,
    /// `max(resting stop, next_trigger)`: a stop never falls.
    pub stop_in_force: Decimal,
    /// `next_trigger` strictly above the resting stop.
    pub raises: bool,
    /// The clamp produced a trigger below the resting stop.
    pub clamped_below_stop: bool,
}

/// Ports `AdjustmentOutcome`: what a corporate action does to a resting stop (`04` §7.3).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AdjustmentOutcome {
    /// The re-derived high.
    pub high_since: Decimal,
    /// The re-derived trigger (or the resting one when there is no close).
    pub next_trigger: Decimal,
    /// Emit a `RAISE_GTT_STOP` carrying the adjustment as its reason.
    pub emit_raise: bool,
    /// Raise `TWT_ADJUSTMENT_RESET`.
    pub alert: bool,
}

pub(crate) fn arith(e: impl fmt::Display) -> CoreError {
    CoreError::Arithmetic(e.to_string())
}

fn w(d: Decimal) -> WideDecimal {
    WideDecimal::from(d)
}

fn ow(d: Option<Decimal>) -> Option<WideDecimal> {
    d.map(WideDecimal::from)
}

fn out(x: &WideDecimal) -> CoreResult<Decimal> {
    x.try_to_decimal().map_err(arith)
}

/// The wide (Python-`Decimal`) core every public function and the backtest share.
pub(crate) mod wide {
    use chrono::NaiveDate;

    use super::{Action, ExitReason, arith};
    use crate::decimal::{Rounding, WideDecimal};
    use crate::error::CoreResult;
    use crate::twt::config::ExitConfig;

    /// `OpenPosition`, in wide decimals.
    #[derive(Debug, Clone)]
    pub(crate) struct Position {
        pub(crate) entry_date: NaiveDate,
        pub(crate) stop_price: WideDecimal,
        pub(crate) high_since: WideDecimal,
    }

    /// `Bar`, in wide decimals.
    #[derive(Debug, Clone)]
    pub(crate) struct WBar {
        pub(crate) session: NaiveDate,
        pub(crate) open: Option<WideDecimal>,
        pub(crate) high: Option<WideDecimal>,
        pub(crate) low: Option<WideDecimal>,
        pub(crate) close: Option<WideDecimal>,
    }

    /// `Ratchet`, in wide decimals.
    #[derive(Debug, Clone)]
    pub(crate) struct WRatchet {
        pub(crate) high_since: WideDecimal,
        pub(crate) raw_trigger: WideDecimal,
        pub(crate) next_trigger: WideDecimal,
        pub(crate) stop_in_force: WideDecimal,
        pub(crate) raises: bool,
        pub(crate) clamped_below_stop: bool,
    }

    /// `ManageAction`, in wide decimals.
    #[derive(Debug, Clone)]
    pub(crate) struct WManage {
        pub(crate) action: Action,
        pub(crate) reason: Option<ExitReason>,
        pub(crate) price: Option<WideDecimal>,
        pub(crate) note: String,
    }

    /// Python's `max(a, b)`: `a` unless `b` is strictly greater.
    pub(crate) fn py_max(a: &WideDecimal, b: &WideDecimal) -> WideDecimal {
        if b > a { b.clone() } else { a.clone() }
    }

    /// Python's `min(a, b)`: `a` unless `b` is strictly smaller.
    pub(crate) fn py_min(a: &WideDecimal, b: &WideDecimal) -> WideDecimal {
        if b < a { b.clone() } else { a.clone() }
    }

    /// `x.to_integral_value(rounding=mode)`: an integral exponent is left as it is.
    pub(crate) fn to_integral(x: &WideDecimal, mode: Rounding) -> CoreResult<WideDecimal> {
        if x.exponent() >= 0 {
            return Ok(x.clone());
        }
        x.quantize(0, mode).map_err(arith)
    }

    fn one() -> WideDecimal {
        WideDecimal::from(1_i64)
    }

    fn pct() -> WideDecimal {
        WideDecimal::from(100_i64)
    }

    /// `tick_floor`.
    pub(crate) fn tick_floor(price: &WideDecimal, tick: &WideDecimal) -> CoreResult<WideDecimal> {
        let q = price.div(tick).map_err(arith)?;
        Ok(to_integral(&q, Rounding::Down)?.mul(tick))
    }

    /// `initial_stop`.
    pub(crate) fn initial_stop(
        fill: &WideDecimal,
        config: &ExitConfig,
        tick: &WideDecimal,
    ) -> CoreResult<WideDecimal> {
        let fraction = WideDecimal::from(config.stop_pct)
            .div(&pct())
            .map_err(arith)?;
        tick_floor(&fill.mul(&one().sub(&fraction)), tick)
    }

    /// `trail_from`.
    pub(crate) fn trail_from(high: &WideDecimal, config: &ExitConfig) -> CoreResult<WideDecimal> {
        let fraction = WideDecimal::from(config.trail_pct)
            .div(&pct())
            .map_err(arith)?;
        Ok(high.mul(&one().sub(&fraction)))
    }

    /// `clamp_under_close`.
    pub(crate) fn clamp_under_close(
        raw: &WideDecimal,
        close: &WideDecimal,
        config: &ExitConfig,
        tick: &WideDecimal,
    ) -> CoreResult<WideDecimal> {
        if raw < close {
            let ceiling = close.mul(&WideDecimal::from(config.close_clamp_fraction));
            return tick_floor(&py_min(raw, &ceiling), tick);
        }
        tick_floor(
            &close.mul(&WideDecimal::from(config.close_clamp_fallback)),
            tick,
        )
    }

    /// `ratchet`.
    pub(crate) fn ratchet(
        position: &Position,
        bar: &WBar,
        config: &ExitConfig,
        tick: &WideDecimal,
    ) -> CoreResult<Option<WRatchet>> {
        let Some(close) = &bar.close else {
            return Ok(None);
        };
        let high_since = match &bar.high {
            None => position.high_since.clone(),
            Some(high) => py_max(&position.high_since, high),
        };
        let trailed = trail_from(&high_since, config)?;
        let raw_trigger = py_max(&position.stop_price, &trailed);
        let next_trigger = clamp_under_close(&raw_trigger, close, config, tick)?;
        Ok(Some(WRatchet {
            stop_in_force: py_max(&position.stop_price, &next_trigger),
            raises: next_trigger > position.stop_price,
            clamped_below_stop: next_trigger < position.stop_price,
            high_since,
            raw_trigger,
            next_trigger,
        }))
    }

    /// `stop_fill`: the open first, then the low.
    pub(crate) fn stop_fill(bar: &WBar, stop: &WideDecimal) -> Option<(ExitReason, WideDecimal)> {
        if let Some(open) = &bar.open
            && open <= stop
        {
            return Some((ExitReason::StopGap, open.clone()));
        }
        if let Some(low) = &bar.low
            && low <= stop
        {
            return Some((ExitReason::StopHit, stop.clone()));
        }
        None
    }

    /// `fill_day_stop`.
    pub(crate) fn fill_day_stop(position: &Position, bar: &WBar) -> Option<WManage> {
        if bar.session != position.entry_date {
            return None;
        }
        let low = bar.low.as_ref()?;
        if low > &position.stop_price {
            return None;
        }
        let price = match &bar.open {
            Some(open) if open <= &position.stop_price => open.clone(),
            _ => position.stop_price.clone(),
        };
        Some(WManage {
            action: Action::StoppedOut,
            reason: Some(ExitReason::StopDay0),
            price: Some(price),
            note: format!(
                "the entry session traded to {} against a stop of {}",
                low.to_sci_string(),
                position.stop_price.to_sci_string()
            ),
        })
    }

    /// `manage`.
    pub(crate) fn manage(
        position: &Position,
        bar: &WBar,
        blank_sessions: i64,
        config: &ExitConfig,
    ) -> WManage {
        if bar.close.is_none() {
            if blank_sessions >= config.no_bar_sessions {
                return WManage {
                    action: Action::WriteOff,
                    reason: Some(ExitReason::NoBar),
                    price: None,
                    note: format!("no bar for {blank_sessions} sessions"),
                };
            }
            return WManage {
                action: Action::Hold,
                reason: None,
                price: None,
                note: "no bar today".to_owned(),
            };
        }
        if let Some(day_zero) = fill_day_stop(position, bar) {
            return day_zero;
        }
        if let Some((reason, price)) = stop_fill(bar, &position.stop_price) {
            return WManage {
                action: Action::StoppedOut,
                reason: Some(reason),
                price: Some(price),
                note: format!("stop {}", position.stop_price.to_sci_string()),
            };
        }
        WManage {
            action: Action::Hold,
            reason: None,
            price: None,
            note: "above the stop; the trail ratchets tonight".to_owned(),
        }
    }
}

fn wide_position(p: &OpenPosition) -> wide::Position {
    wide::Position {
        entry_date: p.entry_date,
        stop_price: w(p.stop_price),
        high_since: w(p.high_since),
    }
}

fn wide_bar(b: &Bar) -> wide::WBar {
    wide::WBar {
        session: b.session,
        open: ow(b.open),
        high: ow(b.high),
        low: ow(b.low),
        close: ow(b.close),
    }
}

fn manage_out(m: wide::WManage) -> CoreResult<ManageAction> {
    Ok(ManageAction {
        action: m.action,
        reason: m.reason,
        price: m.price.as_ref().map(out).transpose()?,
        note: m.note,
    })
}

/// Ports `tick`: the exchange's tick, as a `Decimal`; `None` means NSE cash equities' own.
///
/// # Errors
/// [`CoreError::Arithmetic`] for a string Python's `Decimal` would refuse.
pub fn tick(tick_inr: Option<&str>) -> CoreResult<Decimal> {
    let text = tick_inr.unwrap_or(TICK_INR);
    out(&WideDecimal::from_str(text).map_err(arith)?)
}

/// Ports `tick_floor`: round **down** (toward zero) to the tick.
///
/// # Errors
/// [`CoreError::Arithmetic`] on a zero tick (Python's `DivisionByZero`).
pub fn tick_floor(price: Decimal, tick_size: Decimal) -> CoreResult<Decimal> {
    out(&wide::tick_floor(&w(price), &w(tick_size))?)
}

/// Ports `initial_stop`: `stop_pct` below the fill, floored to the tick (`04` §7.1).
///
/// # Errors
/// [`CoreError::Arithmetic`] on a zero tick.
pub fn initial_stop(
    fill_price: Decimal,
    config: &ExitConfig,
    tick_size: Decimal,
) -> CoreResult<Decimal> {
    out(&wide::initial_stop(&w(fill_price), config, &w(tick_size))?)
}

/// Ports `stop_is_below_entry`: `0 < stop < fill_price`.
#[must_use]
pub fn stop_is_below_entry(fill_price: Decimal, stop: Decimal) -> bool {
    Decimal::ZERO < stop && stop < fill_price
}

/// Ports `trail_from`: `high_since x (1 - trail_pct / 100)`.
///
/// # Errors
/// [`CoreError::Arithmetic`] when the result does not fit a `Decimal`.
pub fn trail_from(high_since: Decimal, config: &ExitConfig) -> CoreResult<Decimal> {
    out(&wide::trail_from(&w(high_since), config)?)
}

/// Ports `clamp_under_close` (`04` §7.2's clamp, floored to the tick).
///
/// # Errors
/// [`CoreError::Arithmetic`] on a zero tick.
pub fn clamp_under_close(
    raw_trigger: Decimal,
    close: Decimal,
    config: &ExitConfig,
    tick_size: Decimal,
) -> CoreResult<Decimal> {
    out(&wide::clamp_under_close(
        &w(raw_trigger),
        &w(close),
        config,
        &w(tick_size),
    )?)
}

/// Ports `ratchet`: `04` §7.2 after one close; `None` when the name did not print.
///
/// # Errors
/// [`CoreError::Arithmetic`] on a zero tick.
pub fn ratchet(
    position: &OpenPosition,
    bar: &Bar,
    config: &ExitConfig,
    tick_size: Decimal,
) -> CoreResult<Option<Ratchet>> {
    let Some(r) = wide::ratchet(
        &wide_position(position),
        &wide_bar(bar),
        config,
        &w(tick_size),
    )?
    else {
        return Ok(None);
    };
    Ok(Some(Ratchet {
        high_since: out(&r.high_since)?,
        raw_trigger: out(&r.raw_trigger)?,
        next_trigger: out(&r.next_trigger)?,
        stop_in_force: out(&r.stop_in_force)?,
        raises: r.raises,
        clamped_below_stop: r.clamped_below_stop,
    }))
}

/// Ports `on_adjustment` (`04` §7.3, TW0.7): a re-derived trigger above the resting one is a
/// line; one below it is an alert and nothing else.
///
/// # Errors
/// [`CoreError::Arithmetic`] on a zero tick.
pub fn on_adjustment(
    position: &OpenPosition,
    rederived_high_since: Decimal,
    bar: &Bar,
    config: &ExitConfig,
    tick_size: Decimal,
) -> CoreResult<AdjustmentOutcome> {
    let Some(close) = bar.close else {
        return Ok(AdjustmentOutcome {
            high_since: rederived_high_since,
            next_trigger: position.stop_price,
            emit_raise: false,
            alert: true,
        });
    };
    let trailed = wide::trail_from(&w(rederived_high_since), config)?;
    let trigger = wide::clamp_under_close(&trailed, &w(close), config, &w(tick_size))?;
    let raises = trigger > w(position.stop_price);
    Ok(AdjustmentOutcome {
        high_since: rederived_high_since,
        next_trigger: out(&trigger)?,
        emit_raise: raises,
        alert: !raises,
    })
}

/// Ports `stop_fill`: the open first, then the low; `None` when the stop was not reached.
///
/// # Errors
/// Never in practice (the price is one of the inputs); typed for the shared core.
pub fn stop_fill(bar: &Bar, stop: Decimal) -> CoreResult<Option<(ExitReason, Decimal)>> {
    wide::stop_fill(&wide_bar(bar), &w(stop))
        .map(|(reason, price)| out(&price).map(|p| (reason, p)))
        .transpose()
}

/// Ports `fill_day_stop` (`04` §7.4); `None` when the entry session did not reach the stop.
///
/// # Errors
/// Never in practice (the price is one of the inputs); typed for the shared core.
pub fn fill_day_stop(position: &OpenPosition, bar: &Bar) -> CoreResult<Option<ManageAction>> {
    wide::fill_day_stop(&wide_position(position), &wide_bar(bar))
        .map(manage_out)
        .transpose()
}

/// Ports `manage`: no bar for `no_bar_sessions` → write off; the fill-day rule; the stop;
/// otherwise hold. There is no fifth branch.
///
/// # Errors
/// Never in practice (every price is one of the inputs); typed for the shared core.
pub fn manage(
    position: &OpenPosition,
    bar: &Bar,
    blank_sessions: i64,
    config: &ExitConfig,
) -> CoreResult<ManageAction> {
    manage_out(wide::manage(
        &wide_position(position),
        &wide_bar(bar),
        blank_sessions,
        config,
    ))
}

/// Ports `r_multiple`: `(exit - entry) / (entry - initial stop)`, 2 dp (half-even); `None` when
/// the risk was not positive.
///
/// # Errors
/// [`CoreError::Arithmetic`] where Python's `quantize` raises (more than 28 digits).
pub fn r_multiple(
    entry: Decimal,
    initial: Decimal,
    exit_price: Decimal,
) -> CoreResult<Option<Decimal>> {
    let risk = w(entry).sub(&w(initial));
    if risk <= WideDecimal::zero() {
        return Ok(None);
    }
    let r = w(exit_price)
        .sub(&w(entry))
        .div(&risk)
        .map_err(arith)?
        .quantize(-2, Rounding::HalfEven)
        .map_err(arith)?;
    out(&r).map(Some)
}
