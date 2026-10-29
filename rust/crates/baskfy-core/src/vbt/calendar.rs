//! Port of `baskfy_core.vbt.calendar` (`decile-blueprint/packages/core/src/baskfy_core/vbt/calendar.py`).
//!
//! The run's own trading calendar: thin sessions removed (`docs/vbt/04` §2.1). A muhurat
//! session, or a special Saturday the exchange runs as a test, is a session to the exchange and
//! is **not a trading day for a daily strategy**: ~200 names print against ~1,900, and a single
//! such column poisons every 50- and 200-session rolling window that spans it.
//!
//! The rule is the contract: a session is thin when its traded-name count is below
//! `thin_session_min_share` of the **centred** rolling median of that count. Pure: a frame in, a
//! frame out.
//!
//! Goldens: `calendar.session_counts`, `calendar.thin_sessions`, `calendar.build_calendar`,
//! `calendar.drop_thin_sessions`, `calendar.SessionCalendar.{index_of,sessions_between,advance}`.
//!
//! **Dates.** The `date` column must be a polars `Date` (every caller and golden passes one); a
//! null date is refused by the functions that turn counts into a calendar (DECISIONS-RS R3.x),
//! where Python would carry a `None` "session" into the calendar.

use std::collections::{BTreeMap, BTreeSet};

use chrono::NaiveDate;
use polars::prelude::{BooleanChunked, DataFrame, DataType, IntoColumn, NamedFrom, Series};
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::vbt::config::{DataConfig, VbtConfig};

const DATE: &str = "date";

/// Shared frame helpers for the VBT ports (dates as polars day numbers, Python reprs, polars'
/// total float order).
pub(crate) mod frames {
    use std::cmp::Ordering;
    use std::ops::Range;

    use chrono::{NaiveDate, TimeDelta};
    use polars::prelude::{AnyValue, DataFrame, DataType, IdxCa, IdxSize, Series};

    use crate::error::{CoreError, CoreResult};
    use crate::factor_registry::py_repr_str;

    /// polars' `Date` epoch.
    pub(crate) fn epoch() -> CoreResult<NaiveDate> {
        NaiveDate::from_ymd_opt(1970, 1, 1)
            .ok_or_else(|| CoreError::InvalidInput("1970-01-01".to_owned()))
    }

    /// A date as polars' physical day number.
    pub(crate) fn day_number(d: NaiveDate) -> CoreResult<i32> {
        i32::try_from(d.signed_duration_since(epoch()?).num_days())
            .map_err(|e| CoreError::InvalidInput(e.to_string()))
    }

    /// A polars day number as a date.
    pub(crate) fn from_day_number(days: i32) -> CoreResult<NaiveDate> {
        epoch()?
            .checked_add_signed(TimeDelta::days(i64::from(days)))
            .ok_or_else(|| CoreError::InvalidInput(format!("day {days} out of range")))
    }

    /// Python's `repr(list_of_str)`.
    pub(crate) fn py_list(items: &[String]) -> String {
        let inner: Vec<String> = items.iter().map(|s| py_repr_str(s)).collect();
        format!("[{}]", inner.join(", "))
    }

    /// The frame's column names, in order.
    pub(crate) fn column_names(df: &DataFrame) -> Vec<String> {
        df.get_column_names()
            .iter()
            .map(|c| c.to_string())
            .collect()
    }

    /// True when the frame has a column `name`.
    pub(crate) fn has(df: &DataFrame, name: &str) -> bool {
        df.get_column_index(name).is_some()
    }

    /// A `Date` column as day numbers (`None` for null). Refuses any other dtype.
    pub(crate) fn dates(df: &DataFrame, name: &str) -> CoreResult<Vec<Option<i32>>> {
        let col = df.column(name)?;
        if col.dtype() != &DataType::Date {
            return Err(CoreError::InvalidInput(format!(
                "column '{name}' must be a polars Date, got {}",
                col.dtype()
            )));
        }
        Ok(col
            .as_materialized_series()
            .cast(&DataType::Int32)?
            .i32()?
            .iter()
            .collect())
    }

    /// A day-number vector as a polars `Date` series.
    pub(crate) fn date_series(name: &str, days: Vec<Option<i32>>) -> CoreResult<Series> {
        use polars::prelude::NamedFrom;
        Ok(Series::new(name.into(), days).cast(&DataType::Date)?)
    }

    /// A column cast to `Float64`, as values.
    pub(crate) fn floats(df: &DataFrame, name: &str) -> CoreResult<Vec<Option<f64>>> {
        let s = df
            .column(name)?
            .as_materialized_series()
            .cast(&DataType::Float64)?;
        Ok(s.f64()?.iter().collect())
    }

    /// Row positions as a polars gather index.
    pub(crate) fn idx(rows: &[usize]) -> CoreResult<IdxCa> {
        let v = rows
            .iter()
            .map(|&r| IdxSize::try_from(r).map_err(|e| CoreError::InvalidInput(e.to_string())))
            .collect::<CoreResult<Vec<IdxSize>>>()?;
        Ok(IdxCa::from_vec("".into(), v))
    }

    /// polars' total order on floats: NaN equals NaN and sorts above every number.
    pub(crate) fn tot_cmp(a: f64, b: f64) -> Ordering {
        match (a.is_nan(), b.is_nan()) {
            (true, true) => Ordering::Equal,
            (true, false) => Ordering::Greater,
            (false, true) => Ordering::Less,
            (false, false) => a.partial_cmp(&b).unwrap_or(Ordering::Equal),
        }
    }

    /// A polars float comparison under Kleene logic: null when either side is null.
    pub(crate) fn cmp_opt(
        a: Option<f64>,
        b: Option<f64>,
        accept: fn(Ordering) -> bool,
    ) -> Option<bool> {
        Some(accept(tot_cmp(a?, b?)))
    }

    /// `.over(key)`'s groups: the runs of equal values of `key` in a frame sorted by it.
    pub(crate) fn runs(df: &DataFrame, key: &str) -> CoreResult<Vec<Range<usize>>> {
        let s = df.column(key)?.as_materialized_series().clone();
        let mut groups: Vec<Range<usize>> = Vec::new();
        let mut previous: Option<AnyValue<'_>> = None;
        for r in 0..s.len() {
            let v = s.get(r)?;
            let same = previous.as_ref().is_some_and(|p| *p == v);
            if same {
                if let Some(g) = groups.last_mut() {
                    g.end = r + 1;
                }
            } else {
                groups.push(r..r + 1);
            }
            previous = Some(v);
        }
        Ok(groups)
    }
}

/// Ports `baskfy_core.vbt.calendar.SessionCalendar` — the sessions a rolling window is allowed
/// to count, with the thin ones named. Encoded as `{sessions, dropped, counts}`.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct SessionCalendar {
    /// The sessions kept, in date order.
    pub sessions: Vec<NaiveDate>,
    /// The thin sessions removed.
    pub dropped: Vec<NaiveDate>,
    /// `date -> traded-name count`, every session including the dropped ones.
    pub counts: BTreeMap<NaiveDate, i64>,
}

impl SessionCalendar {
    /// Ports `SessionCalendar.index_of`: the position of `session`, `-1` when it is not one.
    #[must_use]
    pub fn index_of(&self, session: NaiveDate) -> i64 {
        self.sessions
            .iter()
            .position(|d| *d == session)
            .and_then(|i| i64::try_from(i).ok())
            .unwrap_or(-1)
    }

    /// Ports `SessionCalendar.sessions_between`: how many sessions lie in `(first, last]`; `0`
    /// when either is not a session, negative when `last` precedes `first`.
    #[must_use]
    pub fn sessions_between(&self, first: NaiveDate, last: NaiveDate) -> i64 {
        let (start, end) = (self.index_of(first), self.index_of(last));
        if start < 0 || end < 0 {
            return 0;
        }
        end - start
    }

    /// Ports `SessionCalendar.advance`: the session `sessions` places after `session`, or `None`
    /// past the end (or when `session` is not one).
    ///
    /// A negative step that lands before the first session indexes from the end, as Python's
    /// tuple indexing does.
    ///
    /// # Errors
    /// [`CoreError::InvalidInput`] (Python's `IndexError`) when a negative step reaches past the
    /// start of the tuple even from the end.
    pub fn advance(&self, session: NaiveDate, sessions: i64) -> CoreResult<Option<NaiveDate>> {
        let here = self.index_of(session);
        let len = i64::try_from(self.sessions.len())
            .map_err(|e| CoreError::InvalidInput(e.to_string()))?;
        let target = here.saturating_add(sessions);
        if here < 0 || target >= len {
            return Ok(None);
        }
        let at = if target < 0 { target + len } else { target };
        let at = usize::try_from(at)
            .map_err(|_| CoreError::InvalidInput("tuple index out of range".to_owned()))?;
        self.sessions
            .get(at)
            .copied()
            .map(Some)
            .ok_or_else(|| CoreError::InvalidInput("tuple index out of range".to_owned()))
    }
}

/// `(date day number, traded)` per session, sorted (a null date first).
fn count_rows(bars: &DataFrame) -> CoreResult<Vec<(Option<i32>, i64)>> {
    if !frames::has(bars, DATE) {
        return Err(CoreError::InvalidInput(format!(
            "bars need a '{DATE}' column; got {}",
            frames::py_list(&frames::column_names(bars))
        )));
    }
    let mut counts: BTreeMap<Option<i32>, i64> = BTreeMap::new();
    for d in frames::dates(bars, DATE)? {
        *counts.entry(d).or_insert(0) += 1;
    }
    Ok(counts.into_iter().collect())
}

/// Ports `baskfy_core.vbt.calendar.session_counts`: `(date, traded)` — how many instruments
/// printed a bar on each session, sorted by date; `traded` is `Int64`.
///
/// # Errors
/// [`CoreError::InvalidInput`] (Python's `ValueError`, same message) without a `date` column;
/// a `date` column that is not a polars `Date` is refused too.
pub fn session_counts(bars: &DataFrame) -> CoreResult<DataFrame> {
    let rows = count_rows(bars)?;
    let days: Vec<Option<i32>> = rows.iter().map(|r| r.0).collect();
    let traded: Vec<i64> = rows.iter().map(|r| r.1).collect();
    let height = rows.len();
    Ok(DataFrame::new(
        height,
        vec![
            frames::date_series(DATE, days)?.into_column(),
            Series::new("traded".into(), traded).into_column(),
        ],
    )?)
}

/// The counts as dated rows; a null date is refused (it cannot be a session).
fn dated_counts(bars: &DataFrame) -> CoreResult<Vec<(NaiveDate, i64)>> {
    count_rows(bars)?
        .into_iter()
        .map(|(d, n)| {
            let d = d.ok_or_else(|| {
                CoreError::InvalidInput("a null date cannot be a session".to_owned())
            })?;
            Ok((frames::from_day_number(d)?, n))
        })
        .collect()
}

/// The centred rolling median of the counts (polars' kernel), `None` where it has no value.
fn centred_median(counts: &[i64], cfg: &DataConfig) -> CoreResult<Vec<Option<f64>>> {
    use polars::polars_compute::rolling::RollingQuantileParams;
    use polars::prelude::{
        QuantileMethod, RollingFnParams, RollingOptionsFixedWindow, SeriesOpsTime,
    };
    let window = usize::try_from(cfg.thin_session_window_bars)
        .map_err(|e| CoreError::InvalidInput(format!("thin_session_window_bars: {e}")))?;
    let min = usize::try_from(cfg.thin_session_min_periods)
        .map_err(|e| CoreError::InvalidInput(format!("thin_session_min_periods: {e}")))?;
    #[allow(clippy::cast_precision_loss)] // counts are far below 2^53
    let values: Vec<f64> = counts.iter().map(|&n| n as f64).collect();
    let s = Series::new("traded".into(), values);
    let options = RollingOptionsFixedWindow {
        window_size: window,
        min_periods: min,
        weights: None,
        center: true,
        fn_params: Some(RollingFnParams::Quantile(RollingQuantileParams {
            prob: 0.5,
            method: QuantileMethod::Linear,
        })),
    };
    let out = s.rolling_quantile(options)?;
    Ok(out.cast(&DataType::Float64)?.f64()?.iter().collect())
}

fn thin_of(counts: &[(NaiveDate, i64)], cfg: &DataConfig) -> CoreResult<Vec<NaiveDate>> {
    if counts.is_empty() {
        return Ok(Vec::new());
    }
    let traded: Vec<i64> = counts.iter().map(|c| c.1).collect();
    let median = centred_median(&traded, cfg)?;
    let mut verdict = Vec::new();
    for ((day, n), med) in counts.iter().zip(median) {
        let Some(med) = med else { continue };
        // Python compares an int with a float exactly; counts are exact in f64.
        #[allow(clippy::cast_precision_loss)]
        let n = *n as f64;
        if n < cfg.thin_session_min_share * med {
            verdict.push(*day);
        }
    }
    Ok(verdict)
}

/// Ports `baskfy_core.vbt.calendar.thin_sessions`: the sessions `04` §2.1 removes, in order.
/// `None` reads the default `DataConfig`.
///
/// # Errors
/// As [`session_counts`]; a null date, or a negative window, is refused.
pub fn thin_sessions(bars: &DataFrame, config: Option<&DataConfig>) -> CoreResult<Vec<NaiveDate>> {
    let default = DataConfig::default();
    let cfg = config.unwrap_or(&default);
    let counts = dated_counts(bars)?;
    thin_of(&counts, cfg)
}

/// Ports `baskfy_core.vbt.calendar.build_calendar`: every session the bars contain, minus the
/// thin ones.
///
/// # Errors
/// As [`thin_sessions`].
pub fn build_calendar(bars: &DataFrame, config: &VbtConfig) -> CoreResult<SessionCalendar> {
    let counts = dated_counts(bars)?;
    let dropped: BTreeSet<NaiveDate> = thin_of(&counts, &config.data)?.into_iter().collect();
    Ok(SessionCalendar {
        sessions: counts
            .iter()
            .map(|c| c.0)
            .filter(|d| !dropped.contains(d))
            .collect(),
        dropped: counts
            .iter()
            .map(|c| c.0)
            .filter(|d| dropped.contains(d))
            .collect(),
        counts: counts.into_iter().collect(),
    })
}

/// Ports `baskfy_core.vbt.calendar.drop_thin_sessions`: `(bars without the thin sessions, the
/// calendar)`. Nothing thin: the bars come back unchanged.
///
/// # Errors
/// As [`build_calendar`].
pub fn drop_thin_sessions(
    bars: &DataFrame,
    config: &VbtConfig,
) -> CoreResult<(DataFrame, SessionCalendar)> {
    let calendar = build_calendar(bars, config)?;
    if calendar.dropped.is_empty() {
        return Ok((bars.clone(), calendar));
    }
    let dropped: BTreeSet<i32> = calendar
        .dropped
        .iter()
        .map(|d| frames::day_number(*d))
        .collect::<CoreResult<_>>()?;
    // `~is_in(dropped)`: a null date is null, and a filter drops it (unreachable: refused above).
    let keep: Vec<bool> = frames::dates(bars, DATE)?
        .into_iter()
        .map(|d| d.is_some_and(|d| !dropped.contains(&d)))
        .collect();
    let mask = BooleanChunked::new("keep".into(), keep);
    Ok((bars.filter(&mask)?, calendar))
}
