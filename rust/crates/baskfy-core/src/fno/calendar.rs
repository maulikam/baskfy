//! Port of `baskfy_core.fno.calendar` (`decile-blueprint/packages/core/src/baskfy_core/fno/calendar.py`).
//!
//! Monthly expiries, entry and hard-exit sessions, the entry window (`04` §1, `03` §4). Two
//! sources and no arithmetic on weekdays: **expiries come from the NFO master** (the monthly is
//! the last listed expiry of its calendar month), **sessions come from `trading_day`**, passed in
//! as a sorted slice of exchange dates; "N sessions before" is an index step on that slice.
//!
//! Pure: the caller supplies the master's expiries, the calendar and `now`.
//!
//! Goldens: `L1/fno/calendar.<function>.case_NNN.json`.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

use chrono::{DateTime, Datelike, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, TimeDelta};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

use super::config::{CommonConfig, F1Config, FnoError, FnoResult};

/// A Python `datetime` as the calendar receives it: naive (exchange-local IST) or aware.
/// `plan_expires_at` keeps whichever it was given, as Python's `tzinfo=issued.tzinfo` does.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PyDateTime {
    /// A naive datetime, exchange-local IST.
    Naive(NaiveDateTime),
    /// An aware datetime.
    Aware(DateTime<FixedOffset>),
}

impl PyDateTime {
    /// The wall-clock date (`dt.date()`).
    #[must_use]
    pub fn date(&self) -> NaiveDate {
        match self {
            Self::Naive(n) => n.date(),
            Self::Aware(a) => a.date_naive(),
        }
    }

    /// The wall-clock time (`dt.time()`).
    #[must_use]
    pub fn time(&self) -> NaiveTime {
        match self {
            Self::Naive(n) => n.time(),
            Self::Aware(a) => a.time(),
        }
    }

    fn add_minutes(self, minutes: i64) -> FnoResult<Self> {
        let delta = TimeDelta::try_minutes(minutes)
            .ok_or_else(|| FnoError::Value(format!("{minutes} minutes is out of range")))?;
        let overflow = || FnoError::Value("date value out of range".to_owned());
        Ok(match self {
            Self::Naive(n) => Self::Naive(n.checked_add_signed(delta).ok_or_else(overflow)?),
            Self::Aware(a) => Self::Aware(a.checked_add_signed(delta).ok_or_else(overflow)?),
        })
    }

    /// `dt.datetime.combine(day, time, tzinfo=self.tzinfo)`.
    fn combine_like(self, day: NaiveDate, time: NaiveTime) -> FnoResult<Self> {
        let naive = NaiveDateTime::new(day, time);
        Ok(match self {
            Self::Naive(_) => Self::Naive(naive),
            Self::Aware(a) => Self::Aware(
                naive
                    .and_local_timezone(*a.offset())
                    .single()
                    .ok_or_else(|| FnoError::Value("ambiguous local time".to_owned()))?,
            ),
        })
    }

    /// Python's `isoformat()`: `2026-10-06T09:50:00`, microseconds only when non-zero, the
    /// offset as `+05:30` when aware.
    #[must_use]
    pub fn isoformat(&self) -> String {
        let base = |n: NaiveDateTime| {
            let micros = n.and_utc().timestamp_subsec_micros();
            if micros == 0 {
                n.format("%Y-%m-%dT%H:%M:%S").to_string()
            } else {
                format!("{}.{micros:06}", n.format("%Y-%m-%dT%H:%M:%S"))
            }
        };
        match self {
            Self::Naive(n) => base(*n),
            Self::Aware(a) => format!("{}{}", base(a.naive_local()), a.format("%:z")),
        }
    }

    /// Python's `datetime.fromisoformat` for the shapes the goldens hold.
    ///
    /// # Errors
    /// `ValueError` when the text is neither a naive nor an offset datetime.
    pub fn parse(text: &str) -> FnoResult<Self> {
        if let Ok(a) = DateTime::parse_from_rfc3339(text) {
            return Ok(Self::Aware(a));
        }
        if let Ok(a) = DateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%S%.f%:z") {
            return Ok(Self::Aware(a));
        }
        for fmt in [
            "%Y-%m-%dT%H:%M:%S%.f",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S%.f",
        ] {
            if let Ok(n) = NaiveDateTime::parse_from_str(text, fmt) {
                return Ok(Self::Naive(n));
            }
        }
        Err(FnoError::Value(format!(
            "Invalid isoformat string: {}",
            super::config::py::repr_str(text)
        )))
    }

    /// Python's ordering of two datetimes of the same kind (instants when aware).
    fn le(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Naive(a), Self::Naive(b)) => a <= b,
            (Self::Aware(a), Self::Aware(b)) => a <= b,
            // Python raises comparing naive with aware; the calendar never mixes them.
            (Self::Naive(a), Self::Aware(b)) => *a <= b.naive_local(),
            (Self::Aware(a), Self::Naive(b)) => a.naive_local() <= *b,
        }
    }
}

impl fmt::Display for PyDateTime {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.isoformat())
    }
}

impl Serialize for PyDateTime {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.isoformat())
    }
}

impl<'de> Deserialize<'de> for PyDateTime {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let text = String::deserialize(deserializer)?;
        Self::parse(&text).map_err(serde::de::Error::custom)
    }
}

/// The last listed expiry of each calendar month, ascending (`04` §1: "never a weekday rule").
///
/// Ports `baskfy_core.fno.calendar.monthly_expiries`.
#[must_use]
pub fn monthly_expiries(expiries: &[NaiveDate]) -> Vec<NaiveDate> {
    let mut last: BTreeMap<(i32, u32), NaiveDate> = BTreeMap::new();
    for day in expiries.iter().copied().collect::<BTreeSet<_>>() {
        last.insert((day.year(), day.month()), day);
    }
    let mut out: Vec<NaiveDate> = last.into_values().collect();
    out.sort();
    out
}

/// The monthly of `year`-`month`, or `None` if the master lists nothing that month.
///
/// Ports `baskfy_core.fno.calendar.monthly_expiry`.
#[must_use]
pub fn monthly_expiry(expiries: &[NaiveDate], year: i32, month: u32) -> Option<NaiveDate> {
    expiries
        .iter()
        .copied()
        .filter(|d| d.year() == year && d.month() == month)
        .max()
}

/// `bisect_left` then an equality check: the index of `day` in `sessions`, if it is one.
/// CPython's `bisect_left` loop, step for step (so an unsorted slice probes the same indices).
fn index_of(sessions: &[NaiveDate], day: NaiveDate) -> Option<usize> {
    let (mut lo, mut hi) = (0, sessions.len());
    while lo < hi {
        let mid = (lo + hi) / 2;
        if sessions[mid] < day {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    (lo < sessions.len() && sessions[lo] == day).then_some(lo)
}

/// The exchange session `n` sessions before `anchor` (`anchor` itself is 0); `None` when
/// `anchor` is not a session of the calendar or the calendar does not reach back that far.
///
/// Ports `baskfy_core.fno.calendar.sessions_before`.
///
/// # Errors
/// `ValueError` when `n` is negative.
pub fn sessions_before(
    sessions: &[NaiveDate],
    anchor: NaiveDate,
    n: i64,
) -> FnoResult<Option<NaiveDate>> {
    if n < 0 {
        return Err(FnoError::Value(
            "n counts sessions backwards and cannot be negative".to_owned(),
        ));
    }
    let Some(i) = index_of(sessions, anchor) else {
        return Ok(None);
    };
    let back = usize::try_from(n).ok();
    Ok(back.and_then(|b| i.checked_sub(b)).map(|j| sessions[j]))
}

/// How many sessions `end` lies after `start` (both must be sessions; negative backwards).
///
/// Ports `baskfy_core.fno.calendar.sessions_between`.
#[must_use]
pub fn sessions_between(sessions: &[NaiveDate], start: NaiveDate, end: NaiveDate) -> Option<i64> {
    let i = index_of(sessions, start)?;
    let j = index_of(sessions, end)?;
    let (i, j) = (i64::try_from(i).ok()?, i64::try_from(j).ok()?);
    Some(j - i)
}

/// F1's entry day: `f1_entry_sessions_before` sessions before the monthly (`04` §1).
///
/// Ports `baskfy_core.fno.calendar.entry_session`.
///
/// # Errors
/// `ValueError` when `n` is negative.
pub fn entry_session(
    sessions: &[NaiveDate],
    expiry: NaiveDate,
    n: i64,
) -> FnoResult<Option<NaiveDate>> {
    sessions_before(sessions, expiry, n)
}

/// `fo_plan.hard_exit_date = E - n` in sessions (`03` §4). `n` is never zero.
///
/// Ports `baskfy_core.fno.calendar.hard_exit_session`.
///
/// # Errors
/// `ValueError` when `n < 1` (never into expiry day, `02` §2.2).
pub fn hard_exit_session(
    sessions: &[NaiveDate],
    expiry: NaiveDate,
    n: i64,
) -> FnoResult<Option<NaiveDate>> {
    if n < 1 {
        return Err(FnoError::Value(
            "fo_hard_exit_before_expiry is never zero (02 §2.2)".to_owned(),
        ));
    }
    sessions_before(sessions, expiry, n)
}

/// 15:00 IST (naive) on `E - fo_hard_exit_before_expiry`.
///
/// Ports `baskfy_core.fno.calendar.hard_exit_at`.
///
/// # Errors
/// `ValueError` when the config's `hard_exit_before_expiry` is below one.
pub fn hard_exit_at(
    sessions: &[NaiveDate],
    expiry: NaiveDate,
    common: &CommonConfig,
) -> FnoResult<Option<NaiveDateTime>> {
    let day = hard_exit_session(sessions, expiry, common.hard_exit_before_expiry)?;
    Ok(day.map(|d| NaiveDateTime::new(d, common.hard_exit_time)))
}

/// A position still open after its hard-exit date is exited at the next open as `LATE_EXIT`
/// (`04` §1).
///
/// Ports `baskfy_core.fno.calendar.is_late_exit`.
#[must_use]
pub fn is_late_exit(hard_exit_date: NaiveDate, today: NaiveDate) -> bool {
    today > hard_exit_date
}

/// The first `(entry session, monthly expiry)` with entry on or after `today`; `None` when the
/// master or the calendar does not reach far enough.
///
/// Ports `baskfy_core.fno.calendar.next_entry`.
///
/// # Errors
/// `ValueError` when `n` is negative.
pub fn next_entry(
    today: NaiveDate,
    expiries: &[NaiveDate],
    sessions: &[NaiveDate],
    n: i64,
) -> FnoResult<Option<(NaiveDate, NaiveDate)>> {
    for expiry in monthly_expiries(expiries) {
        if let Some(entry) = entry_session(sessions, expiry, n)?
            && entry >= today
        {
            return Ok(Some((entry, expiry)));
        }
    }
    Ok(None)
}

/// The monthly expiry whose entry day is `today`, or `None`.
///
/// Ports `baskfy_core.fno.calendar.is_entry_day`.
///
/// # Errors
/// `ValueError` when `n` is negative.
pub fn is_entry_day(
    today: NaiveDate,
    expiries: &[NaiveDate],
    sessions: &[NaiveDate],
    n: i64,
) -> FnoResult<Option<NaiveDate>> {
    Ok(next_entry(today, expiries, sessions, n)?
        .and_then(|(entry, expiry)| (entry == today).then_some(expiry)))
}

/// `expires_at = min(issued + plan_ttl_minutes, entry_window_end)` on the issue date (`04` §1).
///
/// Ports `baskfy_core.fno.calendar.plan_expires_at`.
///
/// # Errors
/// `ValueError` when the arithmetic leaves chrono's range.
pub fn plan_expires_at(
    issued: PyDateTime,
    f1: &F1Config,
    common: &CommonConfig,
) -> FnoResult<PyDateTime> {
    let ttl = issued.add_minutes(common.plan_ttl_minutes)?;
    let window_end = issued.combine_like(issued.date(), f1.entry_window_end)?;
    // Python's min keeps the first of equals.
    Ok(if window_end.le(&ttl) && !ttl.le(&window_end) {
        window_end
    } else {
        ttl
    })
}

/// `plan_time <= now.time() < entry_window_end` (`04` §1). A missed window is `LAPSED`.
///
/// Ports `baskfy_core.fno.calendar.in_entry_window`.
#[must_use]
pub fn in_entry_window(now: PyDateTime, f1: &F1Config) -> bool {
    let t = now.time();
    f1.plan_time <= t && t < f1.entry_window_end
}

/// The nearest expiry **strictly after** the previous session (`04` §4).
///
/// Ports `baskfy_core.fno.calendar.held_expiry`.
#[must_use]
pub fn held_expiry(previous_session: NaiveDate, expiries: &[NaiveDate]) -> Option<NaiveDate> {
    expiries
        .iter()
        .copied()
        .filter(|d| *d > previous_session)
        .min()
}

/// F2 buys the near month, or the next month when the near one expires within
/// `min_sessions` sessions of `day` (`01` §1b).
///
/// Ports `baskfy_core.fno.calendar.f2_contract_expiry`.
#[must_use]
pub fn f2_contract_expiry(
    day: NaiveDate,
    expiries: &[NaiveDate],
    sessions: &[NaiveDate],
    min_sessions: i64,
) -> Option<NaiveDate> {
    monthly_expiries(expiries)
        .into_iter()
        .filter(|d| *d > day)
        .find(|expiry| sessions_between(sessions, day, *expiry).is_some_and(|g| g > min_sessions))
}

/// F2's roll day, `E - f2_roll_before_expiry` at 15:00 (`04` §10).
///
/// Ports `baskfy_core.fno.calendar.roll_session`.
///
/// # Errors
/// `ValueError` when `n < 1`.
pub fn roll_session(
    sessions: &[NaiveDate],
    expiry: NaiveDate,
    n: i64,
) -> FnoResult<Option<NaiveDate>> {
    if n < 1 {
        return Err(FnoError::Value(
            "f2_roll_before_expiry is at least one session".to_owned(),
        ));
    }
    sessions_before(sessions, expiry, n)
}
