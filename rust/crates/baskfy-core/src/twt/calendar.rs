//! Port of `baskfy_core.twt.calendar` — the run's own trading calendar, thin sessions removed
//! (`docs/twt/04` §2.1).
//!
//! **The arithmetic is VBT-1's and is called, not copied** in Python
//! (`baskfy_core.vbt.calendar`); TWT's own thresholds are spelled out field by field in
//! [`as_shared_config`]. Until the VBT lane's `vbt::calendar` lands, the shared arithmetic lives
//! here as [`shared`] (DECISIONS-RS R2.3's pattern) and the TWT functions call it exactly as
//! Python's do.
//!
//! A session is thin when its traded-name count is below `thin_session_min_share` of the
//! **centred** rolling median of that count over `thin_session_window_bars` sessions.

use std::collections::BTreeMap;

use chrono::NaiveDate;
use polars::prelude::DataFrame;
use serde::{Deserialize, Serialize};

use crate::error::CoreResult;
use crate::twt::config::vbt_shared::{DataConfig as VbtDataConfig, VbtConfig};
use crate::twt::config::{DataConfig, TwtConfig};

/// Ports `baskfy_core.vbt.calendar.SessionCalendar` (re-exported by `twt.calendar`): the
/// sessions a rolling window may count, with the thin ones named.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SessionCalendar {
    /// Every session kept, in order.
    pub sessions: Vec<NaiveDate>,
    /// The thin sessions removed, in order.
    pub dropped: Vec<NaiveDate>,
    /// `date -> traded-name count`, for the funnel and the audit.
    pub counts: BTreeMap<NaiveDate, i64>,
}

impl SessionCalendar {
    /// Ports `SessionCalendar.index_of`: the session's position, `-1` when it is not one.
    #[must_use]
    pub fn index_of(&self, session: NaiveDate) -> i64 {
        self.sessions
            .iter()
            .position(|d| *d == session)
            .and_then(|i| i64::try_from(i).ok())
            .unwrap_or(-1)
    }

    /// Ports `SessionCalendar.sessions_between`: calendar sessions in `(first, last]`.
    #[must_use]
    pub fn sessions_between(&self, first: NaiveDate, last: NaiveDate) -> i64 {
        let (start, end) = (self.index_of(first), self.index_of(last));
        if start < 0 || end < 0 {
            return 0;
        }
        end - start
    }

    /// Ports `SessionCalendar.advance`: the session `sessions` places after `session`, or
    /// `None` past the end (or when `session` is not a session).
    #[must_use]
    pub fn advance(&self, session: NaiveDate, sessions: i64) -> Option<NaiveDate> {
        let here = self.index_of(session);
        if here < 0 {
            return None;
        }
        let target = here.checked_add(sessions)?;
        let len = i64::try_from(self.sessions.len()).ok()?;
        if target >= len {
            return None;
        }
        // Python indexes a tuple with a negative int from the end.
        let at = if target < 0 { len + target } else { target };
        usize::try_from(at)
            .ok()
            .and_then(|i| self.sessions.get(i))
            .copied()
    }
}

/// Ports `as_shared_config`: TWT's calendar thresholds in the shape the shared implementation
/// takes, every field written out (the rest are VBT's defaults).
#[must_use]
pub fn as_shared_config(config: &DataConfig) -> VbtConfig {
    VbtConfig {
        data: VbtDataConfig {
            thin_session_min_share: config.thin_session_min_share,
            thin_session_window_bars: config.thin_session_window_bars,
            thin_session_min_periods: config.thin_session_min_periods,
            rolling_min_share: config.rolling_min_share,
            ..VbtDataConfig::default()
        },
        ..VbtConfig::default()
    }
}

/// Ports `session_counts`: `(date, traded)` — how many instruments printed on each session,
/// sorted, `traded` as `Int64`.
///
/// # Errors
/// [`crate::CoreError::InvalidInput`] without a `date` column (Python's `ValueError`).
pub fn session_counts(bars: &DataFrame) -> CoreResult<DataFrame> {
    shared::session_counts(bars)
}

/// Ports `thin_sessions`: the sessions `04` §2.1 removes, in order. `None` is TWT's default
/// `DataConfig`.
///
/// # Errors
/// As [`session_counts`], or a polars error from the median kernel.
pub fn thin_sessions(bars: &DataFrame, config: Option<&DataConfig>) -> CoreResult<Vec<NaiveDate>> {
    let default = DataConfig::default();
    let cfg = config.unwrap_or(&default);
    shared::thin_sessions(bars, &as_shared_config(cfg).data)
}

/// Ports `build_calendar`: every session the bars contain, minus the thin ones.
///
/// # Errors
/// As [`thin_sessions`].
pub fn build_calendar(bars: &DataFrame, config: &TwtConfig) -> CoreResult<SessionCalendar> {
    shared::build_calendar(bars, &as_shared_config(&config.data))
}

/// Ports `drop_thin_sessions`: `(bars without the thin sessions, the calendar)`; the bars come
/// back unchanged when nothing is thin.
///
/// # Errors
/// As [`thin_sessions`].
pub fn drop_thin_sessions(
    bars: &DataFrame,
    config: &TwtConfig,
) -> CoreResult<(DataFrame, SessionCalendar)> {
    shared::drop_thin_sessions(bars, &as_shared_config(&config.data))
}

/// The shared arithmetic of `baskfy_core.vbt.calendar`, ported as far as TWT calls it.
pub(crate) mod shared {
    use std::collections::{BTreeMap, BTreeSet};

    use chrono::NaiveDate;
    use polars::prelude::{BooleanChunked, DataFrame, IntoColumn, NamedFrom, Series};

    use super::SessionCalendar;
    use crate::error::{CoreError, CoreResult};
    use crate::twt::config::vbt_shared::{DataConfig, VbtConfig};
    use crate::twt::indicators::frame::{
        F, cmp_nulls_first, column_names, date_of, date_series, dates, has, py_list, rolling,
    };

    /// `(session, traded)` pairs, sorted by date (a null date sorts first).
    fn counts(bars: &DataFrame) -> CoreResult<Vec<(Option<i32>, i64)>> {
        if !has(bars, "date") {
            return Err(CoreError::InvalidInput(format!(
                "bars need a 'date' column; got {}",
                py_list(&column_names(bars))
            )));
        }
        let mut tally: BTreeMap<Option<i32>, i64> = BTreeMap::new();
        for day in dates(bars, "date")? {
            *tally.entry(day).or_insert(0) += 1;
        }
        let mut out: Vec<(Option<i32>, i64)> = tally.into_iter().collect();
        out.sort_by(|a, b| cmp_nulls_first(&a.0, &b.0));
        Ok(out)
    }

    /// Ports `vbt.calendar.session_counts`.
    pub(crate) fn session_counts(bars: &DataFrame) -> CoreResult<DataFrame> {
        let rows = counts(bars)?;
        let height = rows.len();
        let day = date_series("date", rows.iter().map(|r| r.0).collect())?;
        let traded = Series::new(
            "traded".into(),
            rows.iter().map(|r| r.1).collect::<Vec<_>>(),
        );
        Ok(DataFrame::new(
            height,
            vec![day.into_column(), traded.into_column()],
        )?)
    }

    /// The thin verdict over the counts: `(session, is_thin)` for every counted session.
    fn verdicts(
        rows: &[(Option<i32>, i64)],
        cfg: &DataConfig,
    ) -> CoreResult<Vec<(Option<i32>, bool)>> {
        if rows.is_empty() {
            return Ok(Vec::new());
        }
        let window = usize::try_from(cfg.thin_session_window_bars).map_err(|_| {
            CoreError::InvalidInput(format!(
                "window_size must be >= 0, got {}",
                cfg.thin_session_window_bars
            ))
        })?;
        let min = usize::try_from(cfg.thin_session_min_periods).map_err(|_| {
            CoreError::InvalidInput(format!(
                "min_samples must be >= 0, got {}",
                cfg.thin_session_min_periods
            ))
        })?;
        #[allow(clippy::cast_precision_loss)]
        let traded: Vec<F> = rows.iter().map(|r| Some(r.1 as f64)).collect();
        let median = rolling(&traded, window, min, true, true)?;
        Ok(rows
            .iter()
            .zip(median)
            .map(|(row, med)| {
                // Python: `int < float * float`, an exact comparison (counts are small ints).
                #[allow(clippy::cast_precision_loss)]
                let thin = med.is_some_and(|m| (row.1 as f64) < cfg.thin_session_min_share * m);
                (row.0, thin)
            })
            .collect())
    }

    /// Ports `vbt.calendar.thin_sessions`.
    pub(crate) fn thin_sessions(bars: &DataFrame, cfg: &DataConfig) -> CoreResult<Vec<NaiveDate>> {
        let rows = counts(bars)?;
        verdicts(&rows, cfg)?
            .into_iter()
            .filter(|(_, thin)| *thin)
            .filter_map(|(day, _)| day)
            .map(date_of)
            .collect()
    }

    /// Ports `vbt.calendar.build_calendar`.
    pub(crate) fn build_calendar(
        bars: &DataFrame,
        config: &VbtConfig,
    ) -> CoreResult<SessionCalendar> {
        let rows = counts(bars)?;
        let dropped: BTreeSet<i32> = verdicts(&rows, &config.data)?
            .into_iter()
            .filter(|(_, thin)| *thin)
            .filter_map(|(day, _)| day)
            .collect();
        let mut sessions = Vec::new();
        let mut gone = Vec::new();
        let mut tally = BTreeMap::new();
        for (day, traded) in &rows {
            // A null date cannot be a calendar session; Python would carry a `None` here.
            let Some(day) = day else { continue };
            let date = date_of(*day)?;
            if dropped.contains(day) {
                gone.push(date);
            } else {
                sessions.push(date);
            }
            tally.insert(date, *traded);
        }
        Ok(SessionCalendar {
            sessions,
            dropped: gone,
            counts: tally,
        })
    }

    /// Ports `vbt.calendar.drop_thin_sessions`.
    pub(crate) fn drop_thin_sessions(
        bars: &DataFrame,
        config: &VbtConfig,
    ) -> CoreResult<(DataFrame, SessionCalendar)> {
        let calendar = build_calendar(bars, config)?;
        if calendar.dropped.is_empty() {
            return Ok((bars.clone(), calendar));
        }
        let gone: BTreeSet<NaiveDate> = calendar.dropped.iter().copied().collect();
        let keep: Vec<bool> = dates(bars, "date")?
            .into_iter()
            .map(|d| match d {
                // `~is_in(dropped)`: a null date is neither in nor out, and `filter` drops null.
                None => Ok(false),
                Some(days) => Ok(!gone.contains(&date_of(days)?)),
            })
            .collect::<CoreResult<Vec<bool>>>()?;
        let mask = BooleanChunked::new("keep".into(), keep);
        Ok((bars.filter(&mask)?, calendar))
    }
}
