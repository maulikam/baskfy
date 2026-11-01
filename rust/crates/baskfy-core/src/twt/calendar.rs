//! Port of `baskfy_core.twt.calendar` — the run's own trading calendar, thin sessions removed
//! (`docs/twt/04` §2.1).
//!
//! **The arithmetic is VBT-1's and is called, not copied**, in Python and here: every function
//! hands TWT's own thresholds, spelled out field by field in [`as_shared_config`], to
//! [`crate::vbt::calendar`]. [`SessionCalendar`] is VBT's type, re-exported as Python's
//! `twt.calendar` re-exports it (until the wave-3 housekeeping the shared arithmetic was a
//! private copy here; DECISIONS-RS R3.4).
//!
//! A session is thin when its traded-name count is below `thin_session_min_share` of the
//! **centred** rolling median of that count over `thin_session_window_bars` sessions.

use chrono::NaiveDate;
use polars::prelude::DataFrame;

use crate::error::CoreResult;
use crate::twt::config::{DataConfig, TwtConfig};
use crate::vbt::calendar as shared;
use crate::vbt::config::{DataConfig as VbtDataConfig, VbtConfig};

/// Ports `baskfy_core.vbt.calendar.SessionCalendar`, which `twt.calendar` re-exports.
pub use crate::vbt::calendar::SessionCalendar;

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
    shared::thin_sessions(bars, Some(&as_shared_config(cfg).data))
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
