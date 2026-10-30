//! Port of `baskfy_core.options.bars`: windows, 5-minute bars and Kaufman's ER over index minutes.

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, TimeDelta, Timelike};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::options::num;

pub use crate::options::num::{ist, ist_offset};

/// One index minute: its start and OHLC.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Bar {
    /// The minute's start.
    pub ts: DateTime<FixedOffset>,
    /// Open.
    pub open: Decimal,
    /// High.
    pub high: Decimal,
    /// Low.
    pub low: Decimal,
    /// Close.
    pub close: Decimal,
}

fn floor_minute(t: NaiveDateTime) -> NaiveDateTime {
    t.with_second(0)
        .and_then(|x| x.with_nanosecond(0))
        .unwrap_or(t)
}

/// The IST minute a bar starts at.
pub fn minute(ts: &DateTime<FixedOffset>) -> NaiveTime {
    floor_minute(ist(ts)).time()
}

/// The bars finished by `now` (`ts + 1 min <= now`), in time order, one per minute (last wins).
pub fn closed(bars: &[Bar], now: &DateTime<FixedOffset>) -> Vec<Bar> {
    let cutoff = ist(now);
    let mut by_minute: BTreeMap<NaiveDateTime, &Bar> = BTreeMap::new();
    for bar in bars {
        let start = floor_minute(ist(&bar.ts));
        if start + TimeDelta::minutes(1) <= cutoff {
            by_minute.insert(start, bar);
        }
    }
    by_minute.into_values().cloned().collect()
}

/// The bars whose start minute lies in `[start, end]`, in time order.
pub fn window(bars: &[Bar], start: NaiveTime, end: NaiveTime) -> Vec<Bar> {
    let mut out: Vec<Bar> = bars
        .iter()
        .filter(|b| {
            let m = minute(&b.ts);
            start <= m && m <= end
        })
        .cloned()
        .collect();
    out.sort_by_key(|b| ist(&b.ts));
    out
}

fn base_day() -> NaiveDate {
    NaiveDate::from_ymd_opt(2000, 1, 1).unwrap_or_default()
}

/// How many one-minute bars `[start, end]` holds when nothing is missing.
pub fn expected_minutes(start: NaiveTime, end: NaiveTime) -> i64 {
    let span = NaiveDateTime::new(base_day(), end) - NaiveDateTime::new(base_day(), start);
    span.num_seconds().div_euclid(60) + 1
}

/// The bar starting at minute `at`, if stored.
pub fn bar_at(bars: &[Bar], at: NaiveTime) -> Option<&Bar> {
    bars.iter().find(|b| minute(&b.ts) == at)
}

/// The window's high and low; `None` for an empty window.
pub fn high_low(bars: &[Bar]) -> Option<(Decimal, Decimal)> {
    let first = bars.first()?;
    let high = bars
        .iter()
        .skip(1)
        .fold(first.high, |m, b| num::max(m, b.high));
    let low = bars
        .iter()
        .skip(1)
        .fold(first.low, |m, b| num::min(m, b.low));
    Some((high, low))
}

/// The minute after `at`.
pub fn minute_after(at: NaiveTime) -> NaiveTime {
    (NaiveDateTime::new(base_day(), at) + TimeDelta::minutes(1)).time()
}

/// `at` plus `seconds`, clamped to the same day.
pub fn add_seconds(at: NaiveTime, seconds: i64) -> NaiveTime {
    let moment = NaiveDateTime::new(base_day(), at) + TimeDelta::seconds(seconds);
    if moment.date() == base_day() {
        moment.time()
    } else {
        NaiveTime::from_hms_micro_opt(23, 59, 59, 999_999).unwrap_or(NaiveTime::MIN)
    }
}

/// Whether a window ending at minute `end` can be judged at `now`.
pub fn window_settled(
    bars: &[Bar],
    end: NaiveTime,
    now: &DateTime<FixedOffset>,
    grace_seconds: i64,
) -> bool {
    if bar_at(bars, end).is_some() {
        return true;
    }
    ist(now).time() >= add_seconds(minute_after(end), grace_seconds)
}

/// A bar built from one-minute bars, aligned to the session open.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FiveMinuteBar {
    /// Its first minute.
    pub start: NaiveTime,
    /// Its last minute.
    pub close_time: NaiveTime,
    /// Open.
    pub open: Decimal,
    /// High.
    pub high: Decimal,
    /// Low.
    pub low: Decimal,
    /// Close; `None` until its last minute is stored.
    pub close: Option<Decimal>,
    /// Minutes it holds.
    pub minutes: i64,
}

impl FiveMinuteBar {
    /// Its last minute is stored, so its close is known.
    pub fn complete(&self) -> bool {
        self.close.is_some()
    }
}

/// `width`-minute bars aligned to `session_open`.
pub fn five_minute_bars(
    bars: &[Bar],
    session_open: NaiveTime,
    width: i64,
) -> CoreResult<Vec<FiveMinuteBar>> {
    if width <= 0 {
        return Err(CoreError::InvalidInput(
            "a bar is at least one minute wide".to_owned(),
        ));
    }
    let origin = NaiveDateTime::new(base_day(), session_open);
    let mut buckets: BTreeMap<i64, Vec<&Bar>> = BTreeMap::new();
    for bar in bars {
        let offset = (NaiveDateTime::new(base_day(), minute(&bar.ts)) - origin)
            .num_seconds()
            .div_euclid(60);
        if offset < 0 {
            continue;
        }
        buckets.entry(offset / width).or_default().push(bar);
    }
    let mut out = Vec::with_capacity(buckets.len());
    for (index, mut members) in buckets {
        members.sort_by_key(|b| ist(&b.ts));
        let start = (origin + TimeDelta::minutes(index * width)).time();
        let last = (origin + TimeDelta::minutes(index * width + width - 1)).time();
        let closing = members.iter().find(|b| minute(&b.ts) == last);
        let Some(first) = members.first() else {
            continue;
        };
        out.push(FiveMinuteBar {
            start,
            close_time: last,
            open: first.open,
            high: members
                .iter()
                .skip(1)
                .fold(first.high, |m, b| num::max(m, b.high)),
            low: members
                .iter()
                .skip(1)
                .fold(first.low, |m, b| num::min(m, b.low)),
            close: closing.map(|b| b.close),
            minutes: i64::try_from(members.len()).unwrap_or(i64::MAX),
        });
    }
    Ok(out)
}

/// Kaufman's ER: net move over path length; `0` when the path has no length.
pub fn efficiency_ratio(points: &[Decimal]) -> CoreResult<Decimal> {
    if points.len() < 2 {
        return Ok(Decimal::ZERO);
    }
    let mut path = Decimal::ZERO;
    for pair in points.windows(2) {
        path = num::add(path, num::abs(num::sub(pair[1], pair[0])?)?)?;
    }
    if path.is_zero() {
        return Ok(Decimal::ZERO);
    }
    let (first, last) = (points[0], points[points.len() - 1]);
    num::div(num::abs(num::sub(last, first)?)?, path)
}

/// Condor `04` §2.4's path: the first bar's open, then each later bar's close.
pub fn er_points(bars: &[Bar]) -> Vec<Decimal> {
    let Some(first) = bars.first() else {
        return Vec::new();
    };
    std::iter::once(first.open)
        .chain(bars.iter().skip(1).map(|b| b.close))
        .collect()
}

/// `numerator / denominator * 100`.
pub fn pct(numerator: Decimal, denominator: Decimal) -> CoreResult<Decimal> {
    if denominator.is_zero() {
        return Err(CoreError::InvalidInput("a percent of zero".to_owned()));
    }
    num::mul(num::div(numerator, denominator)?, Decimal::ONE_HUNDRED)
}
