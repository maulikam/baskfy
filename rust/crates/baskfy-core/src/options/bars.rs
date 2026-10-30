//! Port of `baskfy_core.options.bars` (`decile-blueprint/packages/core/src/baskfy_core/options/bars.py`).
//!
//! Index bars as every sleeve reads them: windows, 5-minute bars, Kaufman's ER (`04` preamble).
//!
//! * **A window** is the one-minute bars whose *start* lies in `[start, end]` inclusive.
//! * **A 5-minute bar** is built from the one-minute bars, aligned to 09:15 and named by its last
//!   minute (`close_time`); it is **complete** once that last minute is stored (OP4.2).
//! * **Kaufman's efficiency ratio**: `|c_n - c_0| / Σ|c_i - c_{i-1}|`, `0` with no path.
//!
//! Only **closed** minutes exist for a decision made at `now`: a bar counts once
//! `ts + 1 min <= now`.
//!
//! **Times.** Python accepts aware or naive datetimes and reads both as IST wall-clock through
//! `ist()`. Here every time is already that wall-clock ([`NaiveDateTime`]); [`ist`] is the one
//! conversion, applied by the caller at the boundary (an aware database `ts`, a tick's zone).

use std::collections::BTreeMap;

use chrono::{
    DateTime, Duration, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, Offset as _, Timelike, Utc,
};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};

/// One index minute: its start (IST wall-clock) and OHLC, as `op_index_minute` stores it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Bar {
    /// The minute's start, IST wall-clock.
    pub ts: NaiveDateTime,
    /// Open.
    pub open: Decimal,
    /// High.
    pub high: Decimal,
    /// Low.
    pub low: Decimal,
    /// Close.
    pub close: Decimal,
}

/// IST, the fixed `+05:30` offset (India has no daylight saving). A constant, not a clock.
#[must_use]
pub fn ist_offset() -> FixedOffset {
    // 19,800 seconds is always a valid offset; the UTC fallback is unreachable.
    FixedOffset::east_opt(5 * 3600 + 30 * 60).unwrap_or_else(|| Utc.fix())
}

/// `ist(ts)` for an aware value: converted to IST and the zone dropped.
#[must_use]
pub fn ist(ts: &DateTime<FixedOffset>) -> NaiveDateTime {
    ts.with_timezone(&ist_offset()).naive_local()
}

fn floor_minute(ts: NaiveDateTime) -> NaiveDateTime {
    ts.with_second(0)
        .and_then(|t| t.with_nanosecond(0))
        .unwrap_or(ts)
}

fn time_floor_minute(t: NaiveTime) -> NaiveTime {
    t.with_second(0).and_then(|x| x.with_nanosecond(0)).unwrap_or(t)
}

/// `minute(ts)`: the IST minute a bar starts at.
#[must_use]
pub fn minute(ts: NaiveDateTime) -> NaiveTime {
    time_floor_minute(ts.time())
}

/// `closed(bars, now)`: the bars finished by `now` (`ts + 1 min <= now`), in time order, one
/// per minute; a duplicate minute keeps its last reading (the upsert's semantics, OP3.7).
#[must_use]
pub fn closed<'a>(bars: impl IntoIterator<Item = &'a Bar>, now: NaiveDateTime) -> Vec<Bar> {
    let mut by_minute: BTreeMap<NaiveDateTime, &Bar> = BTreeMap::new();
    for bar in bars {
        let start = floor_minute(bar.ts);
        if start + Duration::minutes(1) <= now {
            by_minute.insert(start, bar);
        }
    }
    by_minute.into_values().cloned().collect()
}

/// `window(bars, start, end)`: the bars whose start minute lies in `[start, end]`, in time
/// order (a stable sort, as Python's `sorted`).
#[must_use]
pub fn window<'a>(
    bars: impl IntoIterator<Item = &'a Bar>,
    start: NaiveTime,
    end: NaiveTime,
) -> Vec<Bar> {
    let mut out: Vec<Bar> = bars
        .into_iter()
        .filter(|b| start <= minute(b.ts) && minute(b.ts) <= end)
        .cloned()
        .collect();
    out.sort_by_key(|b| b.ts);
    out
}

fn on_2000(t: NaiveTime) -> NaiveDateTime {
    NaiveDate::from_ymd_opt(2000, 1, 1)
        .unwrap_or(NaiveDate::MIN)
        .and_time(t)
}

/// `expected_minutes(start, end)`: how many one-minute bars `[start, end]` holds.
#[must_use]
pub fn expected_minutes(start: NaiveTime, end: NaiveTime) -> i64 {
    let span = on_2000(end) - on_2000(start);
    span.num_seconds().div_euclid(60) + 1
}

/// `bar_at(bars, at)`: the bar starting at minute `at`, if stored (the first such).
#[must_use]
pub fn bar_at<'a>(bars: impl IntoIterator<Item = &'a Bar>, at: NaiveTime) -> Option<&'a Bar> {
    bars.into_iter().find(|b| minute(b.ts) == at)
}

/// `high_low(bars)`: the window's high and low; `None` for an empty window.
#[must_use]
pub fn high_low(bars: &[Bar]) -> Option<(Decimal, Decimal)> {
    let high = bars.iter().map(|b| b.high).max()?;
    let low = bars.iter().map(|b| b.low).min()?;
    Some((high, low))
}

/// `minute_after(at)`: when a bar starting at `at` has closed (wraps past midnight as Python's
/// `.time()` does).
#[must_use]
pub fn minute_after(at: NaiveTime) -> NaiveTime {
    (on_2000(at) + Duration::minutes(1)).time()
}

/// The largest `datetime.time` (`time.max`, 23:59:59.999999).
#[must_use]
pub fn time_max() -> NaiveTime {
    NaiveTime::from_hms_micro_opt(23, 59, 59, 999_999).unwrap_or(NaiveTime::MIN)
}

/// `add_seconds(at, seconds)`: `at` plus `seconds`, clamped to the same day (`time.max`).
#[must_use]
pub fn add_seconds(at: NaiveTime, seconds: i64) -> NaiveTime {
    let base = on_2000(at);
    let moment = base + Duration::seconds(seconds);
    if moment.date() == base.date() {
        moment.time()
    } else {
        time_max()
    }
}

/// `window_settled(bars, end, now, grace_seconds)`: the window ending at minute `end` has its
/// last minute, or `grace_seconds` have passed after that minute closed (OP4.3).
#[must_use]
pub fn window_settled<'a>(
    bars: impl IntoIterator<Item = &'a Bar>,
    end: NaiveTime,
    now: NaiveDateTime,
    grace_seconds: i64,
) -> bool {
    if bar_at(bars, end).is_some() {
        return true;
    }
    now.time() >= add_seconds(minute_after(end), grace_seconds)
}

/// A bar built from one-minute bars, aligned to the session open.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FiveMinuteBar {
    /// Its first minute.
    pub start: NaiveTime,
    /// Its last minute (the bar "closes at" it).
    pub close_time: NaiveTime,
    /// The first member's open.
    pub open: Decimal,
    /// The members' high.
    pub high: Decimal,
    /// The members' low.
    pub low: Decimal,
    /// The last minute's close; `None` while that minute is not stored.
    pub close: Option<Decimal>,
    /// How many one-minute bars it holds.
    pub minutes: i64,
}

impl FiveMinuteBar {
    /// Its last minute is stored, so its close is known (OP4.2).
    #[must_use]
    pub const fn complete(&self) -> bool {
        self.close.is_some()
    }
}

/// `five_minute_bars(bars, session_open, width)`: `width`-minute bars aligned to
/// `session_open`. Bars before the open are ignored.
///
/// # Errors
/// `width <= 0` (Python's `ValueError("a bar is at least one minute wide")`).
pub fn five_minute_bars<'a>(
    bars: impl IntoIterator<Item = &'a Bar>,
    session_open: NaiveTime,
    width: i64,
) -> CoreResult<Vec<FiveMinuteBar>> {
    if width <= 0 {
        return Err(CoreError::InvalidInput(
            "a bar is at least one minute wide".to_owned(),
        ));
    }
    let origin = on_2000(session_open);
    let mut buckets: BTreeMap<i64, Vec<&Bar>> = BTreeMap::new();
    for bar in bars {
        let offset = (on_2000(minute(bar.ts)) - origin)
            .num_seconds()
            .div_euclid(60);
        if offset < 0 {
            continue;
        }
        buckets.entry(offset.div_euclid(width)).or_default().push(bar);
    }
    let mut out = Vec::with_capacity(buckets.len());
    for (index, mut members) in buckets {
        members.sort_by_key(|b| b.ts);
        let start = (origin + Duration::minutes(index * width)).time();
        let last = (origin + Duration::minutes(index * width + width - 1)).time();
        let closing = members.iter().find(|b| minute(b.ts) == last);
        let Some(first) = members.first() else {
            continue;
        };
        let high = members.iter().map(|b| b.high).max().unwrap_or(first.high);
        let low = members.iter().map(|b| b.low).min().unwrap_or(first.low);
        out.push(FiveMinuteBar {
            start,
            close_time: last,
            open: first.open,
            high,
            low,
            close: closing.map(|b| b.close),
            minutes: i64::try_from(members.len()).unwrap_or(i64::MAX),
        });
    }
    Ok(out)
}

/// `efficiency_ratio(points)`: Kaufman's ER, net move over path length; `0` when the path has
/// no length.
///
/// # Errors
/// Decimal overflow.
pub fn efficiency_ratio(points: &[Decimal]) -> CoreResult<Decimal> {
    if points.len() < 2 {
        return Ok(Decimal::ZERO);
    }
    let mut path = Decimal::ZERO;
    for pair in points.windows(2) {
        if let [a, b] = pair {
            path = crate::options::costs::add(path, (*b - *a).abs())?;
        }
    }
    if path.is_zero() {
        return Ok(Decimal::ZERO);
    }
    let (Some(first), Some(last)) = (points.first(), points.last()) else {
        return Ok(Decimal::ZERO);
    };
    crate::options::costs::py_div((*last - *first).abs(), path)
}

/// `er_points(bars)`: condor `04` §2.4's path — the first bar's **open**, then each later
/// bar's close.
#[must_use]
pub fn er_points(bars: &[Bar]) -> Vec<Decimal> {
    let Some(first) = bars.first() else {
        return Vec::new();
    };
    std::iter::once(first.open)
        .chain(bars.iter().skip(1).map(|b| b.close))
        .collect()
}

/// `pct(numerator, denominator)`: `numerator / denominator * 100`.
///
/// # Errors
/// A zero denominator (Python's `ValueError("a percent of zero")`).
pub fn pct(numerator: Decimal, denominator: Decimal) -> CoreResult<Decimal> {
    if denominator.is_zero() {
        return Err(CoreError::InvalidInput("a percent of zero".to_owned()));
    }
    let q = crate::options::costs::py_div(numerator, denominator)?;
    crate::options::costs::mul(q, Decimal::ONE_HUNDRED)
}
