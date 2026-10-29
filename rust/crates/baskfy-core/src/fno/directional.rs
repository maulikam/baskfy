//! Port of `baskfy_core.fno.directional` (`decile-blueprint/packages/core/src/baskfy_core/fno/directional.py`).
//!
//! F3, the directional index credit spread — every rule of `04` §11, pure (Maulik, M.5). Bars
//! in, decisions out: the desk and the worker hand it daily bars, minute bars, the listed
//! expiries and strikes, and `now`.
//!
//! 1. **Levels** — pivot highs and lows over `f3_pivot_lookback` sessions; support is the
//!    highest pivot low below the close, resistance the lowest pivot high above it; the weekly
//!    range is the last `f3_weekly_sessions` sessions' high and low.
//! 2. **Direction** — UP above the trend average and support, DOWN below both, NONE otherwise;
//!    then the **75-minute confirm**. Disagreement is NONE.
//! 3. **The key level** — support for UP, resistance for DOWN, broken from a print beyond it by
//!    `f3_level_buffer_pct`.
//! 4. **The spread** — UP sells a put `f3_distance_pct` below the weekly low, DOWN a call that
//!    far above the weekly high, rounded away from the index; the wing `f3_wing_pct` further.
//! 5. **Expiry**, 6. **Size**, 7. **Add**, 8. **Exit** (`LEVEL_BREAK`, `LOSS_CUT`,
//!    `DECAY_TARGET`, `HARD_EXIT`, in precedence).
//!
//! Goldens: `L1/fno/directional.<function>.case_NNN.json`.

use std::collections::BTreeMap;

use chrono::{NaiveDate, NaiveTime, Timelike};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize, Serializer, ser::SerializeStruct};

use super::calendar::{PyDateTime, sessions_between};
use super::condor::num;
use super::config::py::{add, mul, sub};
use super::config::{CommonConfig, F3Config, FnoCeilings, FnoError, FnoResult, PlanState, py};
use super::sizing::{FoSizing, size};
use super::vol::OptionType;
use crate::backtest::helpers::WideDecimal;
use crate::options::gating::Mode;

const BAR_75_MINUTES: u32 = 75;
const SESSION_OPEN_MINUTE: u32 = 9 * 60 + 15;
const SESSION_CLOSE_MINUTE: u32 = 15 * 60 + 30;

/// The five 75-minute bars of an NSE session, by their opening minute (`04` §11).
///
/// Ports `baskfy_core.fno.directional.BAR_75_OPENS`.
#[must_use]
pub fn bar_75_opens() -> [NaiveTime; 5] {
    let t = |h, m| NaiveTime::from_hms_opt(h, m, 0).unwrap_or_default();
    [t(9, 15), t(10, 30), t(11, 45), t(13, 0), t(14, 15)]
}

fn hundred() -> Decimal {
    Decimal::ONE_HUNDRED
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

/// A daily read of the trend.
///
/// Ports `baskfy_core.fno.directional.Direction`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Direction {
    /// Above trend and support.
    Up,
    /// Below trend and resistance.
    Down,
    /// Neither.
    None,
}

impl Direction {
    /// The enum's value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::Up => "UP",
            Self::Down => "DOWN",
            Self::None => "NONE",
        }
    }
}

/// `fo_position.closed_reason` for F3 (`03` §9), in precedence order.
///
/// Ports `baskfy_core.fno.directional.F3ExitReason`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum F3ExitReason {
    /// The key level broke.
    LevelBreak,
    /// The mark reached the loss cut.
    LossCut,
    /// The mark decayed to the target.
    DecayTarget,
    /// Expiry day at the hard-exit time.
    HardExit,
}

// =============================================================================================
// Bars
// =============================================================================================

/// One row of `fo_index_daily`.
///
/// Ports `baskfy_core.fno.directional.DailyBar`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct DailyBar {
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

/// One row of `op_index_minute`; `ts` is the minute's start, **in IST**.
///
/// Ports `baskfy_core.fno.directional.MinuteBar`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct MinuteBar {
    /// The minute's start.
    pub ts: PyDateTime,
    /// Open.
    pub open: Decimal,
    /// High.
    pub high: Decimal,
    /// Low.
    pub low: Decimal,
    /// Close.
    pub close: Decimal,
}

/// A completed 75-minute bar (`04` §11): its opening minute and its OHLC.
///
/// Ports `baskfy_core.fno.directional.Bar75`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Bar75 {
    /// The bar's opening minute.
    pub start: PyDateTime,
    /// Open.
    pub open: Decimal,
    /// High.
    pub high: Decimal,
    /// Low.
    pub low: Decimal,
    /// Close.
    pub close: Decimal,
}

fn minute_of(ts: &PyDateTime) -> u32 {
    let t = ts.time();
    t.hour() * 60 + t.minute()
}

fn slot_of(ts: &PyDateTime) -> Option<u32> {
    let minute = minute_of(ts);
    (SESSION_OPEN_MINUTE..SESSION_CLOSE_MINUTE)
        .contains(&minute)
        .then(|| (minute - SESSION_OPEN_MINUTE) / BAR_75_MINUTES)
}

/// The ordering Python's `<` gives two datetimes: wall clock when naive, the instant when aware.
fn instant(ts: &PyDateTime) -> chrono::NaiveDateTime {
    match ts {
        PyDateTime::Naive(n) => *n,
        PyDateTime::Aware(a) => a.naive_utc(),
    }
}

/// `ts.replace(hour=h, minute=m, second=0, microsecond=0)`, keeping any offset.
fn at_minute(ts: &PyDateTime, minute: u32) -> FnoResult<PyDateTime> {
    let bad = || FnoError::Value("hour must be in 0..23".to_owned());
    let time = NaiveTime::from_hms_opt(minute / 60, minute % 60, 0).ok_or_else(bad)?;
    Ok(match ts {
        PyDateTime::Naive(n) => PyDateTime::Naive(n.date().and_time(time)),
        PyDateTime::Aware(a) => PyDateTime::Aware(
            a.date_naive()
                .and_time(time)
                .and_local_timezone(*a.offset())
                .single()
                .ok_or_else(bad)?,
        ),
    })
}

/// Aggregate minute bars into the session's five 75-minute bars, oldest first. A bar is
/// **complete** only when its closing minute (09:15 + 75 n - 1) has been seen; minutes outside
/// 09:15-15:30 are ignored.
///
/// Ports `baskfy_core.fno.directional.seventy_five_minute_bars`.
///
/// # Errors
/// Never in practice (a slot's opening minute is always a valid time).
pub fn seventy_five_minute_bars(minutes: &[MinuteBar]) -> FnoResult<Vec<Bar75>> {
    let mut grouped: BTreeMap<(NaiveDate, u32), Vec<MinuteBar>> = BTreeMap::new();
    for bar in minutes {
        if let Some(slot) = slot_of(&bar.ts) {
            grouped.entry((bar.ts.date(), slot)).or_default().push(*bar);
        }
    }
    let mut out = Vec::new();
    for ((_day, slot), mut rows) in grouped {
        rows.sort_by_key(|b| instant(&b.ts));
        let (Some(first), Some(last)) = (rows.first(), rows.last()) else {
            continue;
        };
        let start_minute = SESSION_OPEN_MINUTE + slot * BAR_75_MINUTES;
        if minute_of(&last.ts) != start_minute + BAR_75_MINUTES - 1 {
            continue;
        }
        let mut high = first.high;
        let mut low = first.low;
        for b in &rows[1..] {
            high = py::max(high, b.high);
            low = py::min(low, b.low);
        }
        out.push(Bar75 {
            start: at_minute(&first.ts, start_minute)?,
            open: first.open,
            high,
            low,
            close: last.close,
        });
    }
    Ok(out)
}

// =============================================================================================
// Levels and direction
// =============================================================================================

/// A turning point of the daily chart.
///
/// Ports `baskfy_core.fno.directional.Pivot`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Pivot {
    /// The session.
    pub date: NaiveDate,
    /// The price.
    pub price: Decimal,
}

/// Pivot highs and lows: a high with `width` lower highs on each side (strictly), a low with
/// `width` higher lows on each side. The last `width` bars cannot yet be pivots.
///
/// Ports `baskfy_core.fno.directional.pivots`.
#[must_use]
pub fn pivots(bars: &[DailyBar], width: i64) -> (Vec<Pivot>, Vec<Pivot>) {
    let mut highs = Vec::new();
    let mut lows = Vec::new();
    let n = i64::try_from(bars.len()).unwrap_or(i64::MAX);
    let mut i = width;
    while i < n - width {
        // Python slices clamp, so a negative width reads `bars[i - w : i]` as Python would.
        let around =
            py_slice(bars, i - width, i)
                .iter()
                .chain(py_slice(bars, i + 1, i + 1 + width));
        let Some(bar) = usize::try_from(i).ok().and_then(|k| bars.get(k)) else {
            i += 1;
            continue;
        };
        let around: Vec<&DailyBar> = around.collect();
        if around.iter().all(|b| bar.high > b.high) {
            highs.push(Pivot {
                date: bar.date,
                price: bar.high,
            });
        }
        if around.iter().all(|b| bar.low < b.low) {
            lows.push(Pivot {
                date: bar.date,
                price: bar.low,
            });
        }
        i += 1;
    }
    (highs, lows)
}

/// `items[start:stop]` with Python's slice rules (negative indexes count from the end).
fn py_slice<T>(items: &[T], start: i64, stop: i64) -> &[T] {
    let len = i64::try_from(items.len()).unwrap_or(i64::MAX);
    let clamp = |k: i64| {
        let k = if k < 0 { (len + k).max(0) } else { k.min(len) };
        usize::try_from(k).unwrap_or(0)
    };
    let (a, b) = (clamp(start), clamp(stop));
    if a >= b { &[] } else { &items[a..b] }
}

/// `items[-n:]` with Python's slice rules.
fn py_tail<T>(items: &[T], n: i64) -> &[T] {
    let len = i64::try_from(items.len()).unwrap_or(i64::MAX);
    py_slice(items, if n == 0 { 0 } else { -n }, len)
}

/// What the daily chart says at a close (`04` §11 "Levels").
///
/// Ports `baskfy_core.fno.directional.Levels`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Levels {
    /// The last bar's session.
    pub as_of: NaiveDate,
    /// The last close.
    pub close: Decimal,
    /// The highest pivot low below the close.
    pub support: Option<Decimal>,
    /// The lowest pivot high above the close.
    pub resistance: Option<Decimal>,
    /// The weekly window's high.
    pub weekly_high: Decimal,
    /// The weekly window's low.
    pub weekly_low: Decimal,
    /// The trend window's average close, to the paisa.
    pub trend_avg: Decimal,
    /// Pivot highs in the lookback.
    pub pivot_highs: Vec<Pivot>,
    /// Pivot lows in the lookback.
    pub pivot_lows: Vec<Pivot>,
}

/// The daily history the levels need: the pivot lookback, and at least the trend window.
///
/// Ports `baskfy_core.fno.directional.bars_needed`.
#[must_use]
pub fn bars_needed(f3: &F3Config) -> i64 {
    f3.pivot_lookback
        .max(f3.trend_sessions)
        .max(f3.weekly_sessions)
}

/// `_money(sum(values, Decimal(0)) / n)`: the 28-digit quotient, then the paisa half up.
fn average_money(values: impl IntoIterator<Item = Decimal>, n: usize) -> FnoResult<Decimal> {
    let mut total = Decimal::ZERO;
    for v in values {
        total = add(total, v)?;
    }
    let n = i64::try_from(n).map_err(|e| FnoError::Value(e.to_string()))?;
    py::div_quantize(
        total,
        Decimal::from(n),
        -2,
        crate::backtest::helpers::Rounding::HalfUp,
    )
}

/// The levels at the last bar's close, or `None` when the history is too short.
///
/// Ports `baskfy_core.fno.directional.levels`.
///
/// # Errors
/// A decimal the default context cannot hold (or Python's `ZeroDivisionError` for an empty
/// trend window, which the config's bounds refuse).
pub fn levels(bars: &[DailyBar], f3: &F3Config) -> FnoResult<Option<Levels>> {
    if i64::try_from(bars.len()).unwrap_or(i64::MAX) < bars_needed(f3) {
        return Ok(None);
    }
    let window = py_tail(bars, f3.pivot_lookback);
    let (highs, lows) = pivots(window, f3.pivot_width);
    let Some(last) = bars.last() else {
        return Ok(None);
    };
    let support = lows
        .iter()
        .map(|p| p.price)
        .filter(|p| *p < last.close)
        .reduce(py::max);
    let resistance = highs
        .iter()
        .map(|p| p.price)
        .filter(|p| *p > last.close)
        .reduce(py::min);
    let weekly = py_tail(bars, f3.weekly_sessions);
    let trend = py_tail(bars, f3.trend_sessions);
    let empty = || FnoError::Value("max() arg is an empty sequence".to_owned());
    let weekly_high = weekly
        .iter()
        .map(|b| b.high)
        .reduce(py::max)
        .ok_or_else(empty)?;
    let weekly_low = weekly
        .iter()
        .map(|b| b.low)
        .reduce(py::min)
        .ok_or_else(empty)?;
    Ok(Some(Levels {
        as_of: last.date,
        close: last.close,
        support,
        resistance,
        weekly_high,
        weekly_low,
        trend_avg: average_money(trend.iter().map(|b| b.close), trend.len())?,
        pivot_highs: highs,
        pivot_lows: lows,
    }))
}

/// UP above the trend average and above support; DOWN below both; otherwise NONE.
///
/// Ports `baskfy_core.fno.directional.daily_direction`.
#[must_use]
pub fn daily_direction(lv: &Levels) -> Direction {
    if lv
        .support
        .is_some_and(|s| lv.close > lv.trend_avg && lv.close > s)
    {
        return Direction::Up;
    }
    if lv
        .resistance
        .is_some_and(|r| lv.close < lv.trend_avg && lv.close < r)
    {
        return Direction::Down;
    }
    Direction::None
}

/// The 75-minute verdict: the bar it read, its close and the average it was judged against.
///
/// Ports `baskfy_core.fno.directional.Confirm`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Confirm {
    /// The confirm agrees with the daily read.
    pub agrees: bool,
    /// The last completed bar's opening minute.
    pub bar_start: Option<PyDateTime>,
    /// Its close.
    pub last_close: Option<Decimal>,
    /// The average of the last `f3_confirm_bars` closes, to the paisa.
    pub average: Option<Decimal>,
    /// The verdict in words.
    pub message: String,
}

/// `04` §11: the last completed 75-minute close above (UP) or below (DOWN) the average of the
/// last `f3_confirm_bars` closes. Too few bars, or a NONE daily read, never agrees.
///
/// Ports `baskfy_core.fno.directional.confirm_75`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn confirm_75(bars: &[Bar75], daily: Direction, f3: &F3Config) -> FnoResult<Confirm> {
    let no = |message: String| Confirm {
        agrees: false,
        bar_start: None,
        last_close: None,
        average: None,
        message,
    };
    if daily == Direction::None {
        return Ok(no("the daily chart gives no direction".to_owned()));
    }
    if i64::try_from(bars.len()).unwrap_or(i64::MAX) < f3.confirm_bars {
        return Ok(no(format!(
            "{} 75-minute bars < {} needed",
            bars.len(),
            f3.confirm_bars
        )));
    }
    let recent = py_tail(bars, f3.confirm_bars);
    let average = average_money(recent.iter().map(|b| b.close), recent.len())?;
    let Some(last) = recent.last() else {
        return Err(FnoError::Value("list index out of range".to_owned()));
    };
    let agrees = if daily == Direction::Up {
        last.close > average
    } else {
        last.close < average
    };
    let verb = if daily == Direction::Up {
        "above"
    } else {
        "below"
    };
    Ok(Confirm {
        agrees,
        bar_start: Some(last.start),
        last_close: Some(last.close),
        average: Some(average),
        message: format!(
            "75-minute close {} {} {verb} its {}-bar average {}",
            py::str(last.close),
            if agrees { "is" } else { "is not" },
            f3.confirm_bars,
            py::str(average)
        ),
    })
}

/// The daily read, confirmed; NONE when the two disagree.
///
/// Ports `baskfy_core.fno.directional.direction_for`.
///
/// # Errors
/// As [`confirm_75`].
pub fn direction_for(
    lv: &Levels,
    bars: &[Bar75],
    f3: &F3Config,
) -> FnoResult<(Direction, Confirm)> {
    let daily = daily_direction(lv);
    let verdict = confirm_75(bars, daily, f3)?;
    let direction = if verdict.agrees {
        daily
    } else {
        Direction::None
    };
    Ok((direction, verdict))
}

/// Support for UP, resistance for DOWN, nothing for NONE.
///
/// Ports `baskfy_core.fno.directional.key_level`.
#[must_use]
pub fn key_level(direction: Direction, lv: &Levels) -> Option<Decimal> {
    match direction {
        Direction::Up => lv.support,
        Direction::Down => lv.resistance,
        Direction::None => None,
    }
}

/// The intraday check at plan time: the index above the session's open for UP, below for
/// DOWN. A print at the open is neither.
///
/// Ports `baskfy_core.fno.directional.intraday_aligned`.
#[must_use]
pub fn intraday_aligned(direction: Direction, last_price: Decimal, session_open: Decimal) -> bool {
    match direction {
        Direction::Up => last_price > session_open,
        Direction::Down => last_price < session_open,
        Direction::None => false,
    }
}

/// The index beyond the level by `f3_level_buffer_pct` — from a print, not a close.
///
/// Ports `baskfy_core.fno.directional.level_broken`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn level_broken(
    direction: Direction,
    level: Decimal,
    last_price: Decimal,
    f3: &F3Config,
) -> FnoResult<bool> {
    let buffer = py::div(mul(level, f3.level_buffer_pct)?, hundred())?;
    Ok(match direction {
        Direction::Up => last_price < sub(level, buffer)?,
        Direction::Down => last_price > add(level, buffer)?,
        Direction::None => false,
    })
}

// =============================================================================================
// The spread
// =============================================================================================

/// The two strikes of the spread.
///
/// Ports `baskfy_core.fno.directional.SpreadStrikes` (serialised with its `width`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
pub struct SpreadStrikes {
    /// `PE` for UP, `CE` for DOWN.
    pub option_type: OptionType,
    /// The short strike.
    pub short: Decimal,
    /// The wing strike.
    pub wing: Decimal,
}

impl SpreadStrikes {
    /// `abs(short - wing)`.
    ///
    /// Ports `baskfy_core.fno.directional.SpreadStrikes.width`.
    ///
    /// # Errors
    /// A difference the default context cannot hold.
    pub fn width(&self) -> FnoResult<Decimal> {
        num::narrow(&WideDecimal::from(sub(self.short, self.wing)?).abs())
    }
}

impl Serialize for SpreadStrikes {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let width = self.width().map_err(serde::ser::Error::custom)?;
        let mut s = serializer.serialize_struct("SpreadStrikes", 4)?;
        s.serialize_field("option_type", &self.option_type)?;
        s.serialize_field("short", &self.short)?;
        s.serialize_field("wing", &self.wing)?;
        s.serialize_field("width", &width)?;
        s.end()
    }
}

/// `(value / step).to_integral_value(ROUND_CEILING if up else ROUND_FLOOR) * step`.
fn to_step(value: Decimal, step: Decimal, up: bool) -> FnoResult<Decimal> {
    let q = py::div_wide(value, step)?;
    let whole = num::to_integral(&q, up)?;
    num::narrow(&whole.mul(&WideDecimal::from(step)))
}

fn option_type_value(kind: OptionType) -> &'static str {
    match kind {
        OptionType::Ce => "CE",
        OptionType::Pe => "PE",
    }
}

/// UP: a put `f3_distance_pct` below the weekly low, rounded down; DOWN: a call that far above
/// the weekly high, rounded up. The wing `f3_wing_pct` of the close further out, rounded away
/// too, and never on the short's strike.
///
/// Ports `baskfy_core.fno.directional.spread_strikes`.
///
/// # Errors
/// `ValueError` with no direction or a non-positive step.
pub fn spread_strikes(
    direction: Direction,
    lv: &Levels,
    step: Decimal,
    f3: &F3Config,
) -> FnoResult<SpreadStrikes> {
    if direction == Direction::None {
        return Err(FnoError::Value("no spread without a direction".to_owned()));
    }
    if step <= Decimal::ZERO {
        return Err(FnoError::Value("strike step must be positive".to_owned()));
    }
    let distance = py::div(f3.distance_pct, hundred())?;
    let wing_gap = py::div(mul(lv.close, f3.wing_pct)?, hundred())?;
    if direction == Direction::Up {
        let short = to_step(
            mul(lv.weekly_low, sub(Decimal::ONE, distance)?)?,
            step,
            false,
        )?;
        let wing = py::min(
            to_step(sub(short, wing_gap)?, step, false)?,
            sub(short, step)?,
        );
        return Ok(SpreadStrikes {
            option_type: OptionType::Pe,
            short,
            wing,
        });
    }
    let short = to_step(
        mul(lv.weekly_high, add(Decimal::ONE, distance)?)?,
        step,
        true,
    )?;
    let wing = py::max(
        to_step(add(short, wing_gap)?, step, true)?,
        add(short, step)?,
    );
    Ok(SpreadStrikes {
        option_type: OptionType::Ce,
        short,
        wing,
    })
}

/// The nearest listed expiry with at least `min_sessions` sessions after `entry`; `None` when
/// nothing listed qualifies.
///
/// Ports `baskfy_core.fno.directional.choose_expiry`.
#[must_use]
pub fn choose_expiry(
    expiries: &[NaiveDate],
    sessions: &[NaiveDate],
    entry: NaiveDate,
    min_sessions: i64,
) -> Option<NaiveDate> {
    let mut listed = expiries.to_vec();
    listed.sort_unstable();
    listed.dedup();
    listed.into_iter().filter(|e| *e >= entry).find(|expiry| {
        sessions_between(sessions, entry, *expiry).is_some_and(|left| left >= min_sessions)
    })
}

/// What the spread collects per unit: the short's price less the wing's.
///
/// Ports `baskfy_core.fno.directional.credit_per_unit`.
///
/// # Errors
/// A difference the default context cannot hold.
pub fn credit_per_unit(short_price: Decimal, wing_price: Decimal) -> FnoResult<Decimal> {
    sub(short_price, wing_price)
}

/// The width less the credit; the exchange's spread margin is the same number.
///
/// Ports `baskfy_core.fno.directional.max_loss_per_unit`.
///
/// # Errors
/// A difference the default context cannot hold.
pub fn max_loss_per_unit(strikes: &SpreadStrikes, credit: Decimal) -> FnoResult<Decimal> {
    sub(strikes.width()?, credit)
}

/// The scan's or the monitor's proposal for one underlying. `state` is `None` when clean, else
/// the first refusal in the order structure → cost → size.
///
/// Ports `baskfy_core.fno.directional.SpreadProposal`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SpreadProposal {
    /// The first refusal, or `None`.
    pub state: Option<PlanState>,
    /// The refusals in words.
    pub reasons: Vec<String>,
    /// The strikes.
    pub strikes: SpreadStrikes,
    /// The short's price (0 without one).
    pub short_price: Decimal,
    /// The wing's price (0 without one).
    pub wing_price: Decimal,
    /// The credit per unit.
    pub credit: Option<Decimal>,
    /// The max loss per unit.
    pub max_loss_per_unit: Option<Decimal>,
    /// The max loss per lot.
    pub max_loss_per_lot_inr: Option<Decimal>,
    /// The sizing.
    pub sizing: Option<FoSizing>,
}

/// `04` §11 "Sizing": the budget is the smaller of the entry share and §3's risk budget (so the
/// ₹ ceiling and `fo_max_lots` still bind), spent on max loss per lot.
///
/// Ports `baskfy_core.fno.directional.size_entry`.
///
/// # Errors
/// `ValueError` from sizing when the max loss is not positive.
pub fn size_entry(
    mode: Mode,
    capital_inr: Decimal,
    max_loss_per_unit: Decimal,
    lot_size: Option<i64>,
    f3: &F3Config,
    common: &CommonConfig,
    ceilings: &FnoCeilings,
) -> FnoResult<FoSizing> {
    size(
        mode,
        capital_inr,
        py::min(f3.entry_share_pct, f3.risk_per_trade_pct),
        max_loss_per_unit,
        lot_size,
        common,
        ceilings,
    )
}

/// The listed prices keyed by `(strike, option type)`: Python's dict, compared by value.
pub type StrikePrices = Vec<((Decimal, OptionType), Decimal)>;

fn price_at(prices: &StrikePrices, strike: Decimal, kind: OptionType) -> Option<Decimal> {
    prices
        .iter()
        .rev()
        .find(|((k, t), _)| *k == strike && *t == kind)
        .map(|(_, p)| *p)
}

/// Strikes, credit, max loss and lots from the listed prices. A leg without a price is
/// `REJECTED_LIQUIDITY`; a credit that is not positive or a short below
/// `f3_short_premium_min_inr` is `REJECTED_COST`; no lots is `REJECTED_SIZE`.
///
/// Ports `baskfy_core.fno.directional.propose_spread`. `prices` holds the last price written
/// for a key (a Python dict built from rows keeps the last value).
///
/// # Errors
/// `ValueError` from [`spread_strikes`] or from sizing.
#[allow(clippy::too_many_arguments)] // every input 04 §11 names, as in Python
pub fn propose_spread(
    mode: Mode,
    direction: Direction,
    lv: &Levels,
    step: Decimal,
    prices: &StrikePrices,
    lot_size: Option<i64>,
    capital_inr: Decimal,
    f3: &F3Config,
    common: &CommonConfig,
    ceilings: &FnoCeilings,
) -> FnoResult<SpreadProposal> {
    let strikes = spread_strikes(direction, lv, step, f3)?;
    let short_price = price_at(prices, strikes.short, strikes.option_type);
    let wing_price = price_at(prices, strikes.wing, strikes.option_type);
    let kind = option_type_value(strikes.option_type);
    let mut reasons = Vec::new();
    if short_price.is_none_or(|p| p <= Decimal::ZERO) {
        reasons.push(format!("short {kind} {}: no price", py::str(strikes.short)));
    }
    if wing_price.is_none_or(|p| p <= Decimal::ZERO) {
        reasons.push(format!("wing {kind} {}: no price", py::str(strikes.wing)));
    }
    let (Some(short_price), Some(wing_price)) = (short_price, wing_price) else {
        return Ok(unpriced(strikes, reasons, short_price, wing_price));
    };
    if !reasons.is_empty() {
        return Ok(unpriced(
            strikes,
            reasons,
            Some(short_price),
            Some(wing_price),
        ));
    }
    let credit = credit_per_unit(short_price, wing_price)?;
    if short_price < f3.short_premium_min_inr {
        reasons.push(format!(
            "short premium {} < ₹{}: not worth the round trip",
            py::str(short_price),
            py::str(f3.short_premium_min_inr)
        ));
    }
    if credit <= Decimal::ZERO {
        reasons.push(format!("credit {} is not positive", py::str(credit)));
    }
    if !reasons.is_empty() {
        return Ok(SpreadProposal {
            state: Some(PlanState::RejectedCost),
            reasons,
            strikes,
            short_price,
            wing_price,
            credit: Some(credit),
            max_loss_per_unit: None,
            max_loss_per_lot_inr: None,
            sizing: None,
        });
    }
    let loss_unit = max_loss_per_unit(&strikes, credit)?;
    let sizing = size_entry(mode, capital_inr, loss_unit, lot_size, f3, common, ceilings)?;
    let per_lot = match lot_size {
        Some(n) if n != 0 => Some(mul(loss_unit, Decimal::from(n))?),
        _ => None,
    };
    Ok(SpreadProposal {
        state: sizing.state,
        reasons: if sizing.state.is_some() {
            vec![sizing.message.clone()]
        } else {
            Vec::new()
        },
        strikes,
        short_price,
        wing_price,
        credit: Some(credit),
        max_loss_per_unit: Some(loss_unit),
        max_loss_per_lot_inr: per_lot,
        sizing: Some(sizing),
    })
}

/// `price or Decimal(0)`: a missing or zero price prints as `0`.
fn or_zero(price: Option<Decimal>) -> Decimal {
    price.filter(|p| !p.is_zero()).unwrap_or(Decimal::ZERO)
}

fn unpriced(
    strikes: SpreadStrikes,
    reasons: Vec<String>,
    short_price: Option<Decimal>,
    wing_price: Option<Decimal>,
) -> SpreadProposal {
    SpreadProposal {
        state: Some(PlanState::RejectedLiquidity),
        reasons,
        strikes,
        short_price: or_zero(short_price),
        wing_price: or_zero(wing_price),
        credit: None,
        max_loss_per_unit: None,
        max_loss_per_lot_inr: None,
        sizing: None,
    }
}

// =============================================================================================
// Add and exit
// =============================================================================================

/// What the monitor knows of an open F3 position.
///
/// Ports `baskfy_core.fno.directional.OpenSpread`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OpenSpread {
    /// The direction traded.
    pub direction: Direction,
    /// The key level.
    pub level: Decimal,
    /// The strikes.
    pub strikes: SpreadStrikes,
    /// The contract's expiry.
    pub expiry: NaiveDate,
    /// The entry session.
    pub entry_session: NaiveDate,
    /// The entry credit per unit.
    pub entry_credit: Decimal,
    /// Lots open.
    pub lots: i64,
    /// Units per lot.
    pub lot_size: i64,
    /// Max loss per lot, ₹.
    pub max_loss_per_lot_inr: Decimal,
}

/// An add, or why there is none.
///
/// Ports `baskfy_core.fno.directional.AddDecision` (serialised with `allowed`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AddDecision {
    /// Lots to add (0: none).
    pub lots: i64,
    /// The decision in words.
    pub message: String,
}

impl AddDecision {
    /// `lots > 0`.
    ///
    /// Ports `baskfy_core.fno.directional.AddDecision.allowed`.
    #[must_use]
    pub fn allowed(&self) -> bool {
        self.lots > 0
    }
}

impl Serialize for AddDecision {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut s = serializer.serialize_struct("AddDecision", 3)?;
        s.serialize_field("lots", &self.lots)?;
        s.serialize_field("message", &self.message)?;
        s.serialize_field("allowed", &self.allowed())?;
        s.end()
    }
}

fn no_add(message: impl Into<String>) -> AddDecision {
    AddDecision {
        lots: 0,
        message: message.into(),
    }
}

/// `04` §11 "Add": the next session or later; the mark decayed `f3_add_working_pct` of the
/// credit; the direction and level unchanged; the same lots again, stopping at the full share.
/// Paper at ₹0 allows one add of one lot.
///
/// Ports `baskfy_core.fno.directional.add_decision`.
///
/// # Errors
/// A decimal the default context cannot hold (a zero max loss per lot divides by zero, as in
/// Python).
#[allow(clippy::too_many_arguments)] // every input 04 §11 names, as in Python
pub fn add_decision(
    position: &OpenSpread,
    today: NaiveDate,
    direction_now: Direction,
    level_intact: bool,
    mark: Option<Decimal>,
    capital_inr: Decimal,
    f3: &F3Config,
    common: &CommonConfig,
) -> FnoResult<AddDecision> {
    if today <= position.entry_session {
        return Ok(no_add("an add waits for the next session"));
    }
    if direction_now != position.direction {
        return Ok(no_add(format!(
            "the direction now reads {}, not {}",
            direction_now.value(),
            position.direction.value()
        )));
    }
    if !level_intact {
        return Ok(no_add("the level is broken; this is an exit, not an add"));
    }
    let Some(mark) = mark else {
        return Ok(no_add("no mark for the spread"));
    };
    let working_at = mul(
        position.entry_credit,
        sub(Decimal::ONE, py::div(f3.add_working_pct, hundred())?)?,
    )?;
    if mark > working_at {
        return Ok(no_add(format!(
            "mark {} has not decayed to {} ({} % of the credit)",
            py::str(mark),
            py::str(num::money(working_at)?),
            py::str(f3.add_working_pct)
        )));
    }
    let cap = common.max_lots.min(common.max_lots_ceiling);
    if capital_inr <= Decimal::ZERO {
        let wanted = i64::from(position.lots < 2);
        if wanted == 0 || position.lots + wanted > cap {
            return Ok(no_add("paper at ₹0: one add of one lot, already taken"));
        }
        return Ok(AddDecision {
            lots: 1,
            message: "paper at ₹0: one add of one lot".to_owned(),
        });
    }
    let full = py::div(mul(capital_inr, f3.full_share_pct)?, hundred())?;
    let room = sub(
        full,
        mul(Decimal::from(position.lots), position.max_loss_per_lot_inr)?,
    )?;
    let ratio = py::div_wide(room, position.max_loss_per_lot_inr)?;
    let floored = num::narrow(&num::to_integral(&ratio, false)?)?;
    let affordable = i64::try_from(floored.trunc().mantissa())
        .map_err(|_| FnoError::Value(format!("{floored} lots is out of range")))?;
    let lots = position.lots.min(affordable).min(cap - position.lots);
    if lots <= 0 {
        return Ok(no_add(format!(
            "the full share ₹{} is reached with {} lots open",
            py::str(num::money(full)?),
            position.lots
        )));
    }
    Ok(AddDecision {
        lots,
        message: format!(
            "add {lots} lots; room ₹{} under the full share",
            py::str(num::money(room)?)
        ),
    })
}

/// An F3 exit and its reason in words.
///
/// Ports `baskfy_core.fno.directional.F3Exit`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct F3Exit {
    /// The reason.
    pub reason: F3ExitReason,
    /// The exit in words.
    pub message: String,
}

/// `_money(credit x (1 - decay_target_pct / 100))`.
///
/// Ports `baskfy_core.fno.directional.decay_target_mark`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn decay_target_mark(entry_credit: Decimal, f3: &F3Config) -> FnoResult<Decimal> {
    num::money(mul(
        entry_credit,
        sub(Decimal::ONE, py::div(f3.decay_target_pct, hundred())?)?,
    )?)
}

/// `_money(credit x loss_cut_mult)`.
///
/// Ports `baskfy_core.fno.directional.loss_cut_mark`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn loss_cut_mark(entry_credit: Decimal, f3: &F3Config) -> FnoResult<Decimal> {
    num::money(mul(entry_credit, f3.loss_cut_mult)?)
}

/// The exit due at `now`, in `04` §11's precedence, or `None` to hold. `last_price` is the
/// index's latest print, `mark` the spread's cost to close per unit; either may be `None`. A
/// position found past its expiry is a hard exit too.
///
/// Ports `baskfy_core.fno.directional.f3_exit`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn f3_exit(
    now: PyDateTime,
    position: &OpenSpread,
    last_price: Option<Decimal>,
    mark: Option<Decimal>,
    f3: &F3Config,
    common: &CommonConfig,
) -> FnoResult<Option<F3Exit>> {
    if let Some(price) = last_price
        && level_broken(position.direction, position.level, price, f3)?
    {
        return Ok(Some(F3Exit {
            reason: F3ExitReason::LevelBreak,
            message: format!(
                "index {} beyond the level {} by more than {} %",
                py::str(price),
                py::str(position.level),
                py::str(f3.level_buffer_pct)
            ),
        }));
    }
    if let Some(mark) = mark {
        let cut = loss_cut_mark(position.entry_credit, f3)?;
        if mark >= cut {
            return Ok(Some(F3Exit {
                reason: F3ExitReason::LossCut,
                message: format!(
                    "mark {} ≥ {} x credit {} ({})",
                    py::str(mark),
                    py::str(f3.loss_cut_mult),
                    py::str(position.entry_credit),
                    py::str(cut)
                ),
            }));
        }
        let target = decay_target_mark(position.entry_credit, f3)?;
        if mark <= target {
            return Ok(Some(F3Exit {
                reason: F3ExitReason::DecayTarget,
                message: format!(
                    "mark {} ≤ {}: {} % of the credit decayed",
                    py::str(mark),
                    py::str(target),
                    py::str(f3.decay_target_pct)
                ),
            }));
        }
    }
    let today = now.date();
    if today > position.expiry || (today == position.expiry && now.time() >= common.hard_exit_time)
    {
        return Ok(Some(F3Exit {
            reason: F3ExitReason::HardExit,
            message: format!(
                "expiry {} at {}",
                iso(position.expiry),
                common.hard_exit_time.format("%H:%M")
            ),
        }));
    }
    Ok(None)
}

/// Credit received less the cost to close, times the units, to the paisa.
///
/// Ports `baskfy_core.fno.directional.pnl_inr`.
///
/// # Errors
/// A decimal the default context cannot hold.
pub fn pnl_inr(entry_credit: Decimal, mark: Decimal, quantity: i64) -> FnoResult<Decimal> {
    num::money(mul(sub(entry_credit, mark)?, Decimal::from(quantity))?)
}
