//! Lane **sleeves** — the four post-publish steps (`refresh_basket` ported by the RS5
//! integrator, `sleeves/basket.rs`). The signatures are the orchestrator's contract. The orchestrator's wrappers make steps 12-14 unable to raise
//! (an error becomes `skipped` with `skipped_reason = "{type}: {message}"`); `refresh_basket`
//! records its own failure and returns, as Python's does.
//!
//! | Python | here |
//! |---|---|
//! | `tasks/basket.run_refresh_basket` → names (step 11; 0 names → `skipped`) | [`refresh_basket`] |
//! | `tasks/swing.run_detect_swing` (step 12) | [`detect_swing`] |
//! | `tasks/vbt.run_detect_vbt` (step 13) | [`detect_vbt`] |
//! | `tasks/twt.run_detect_twt` (step 14; never reads `twt_execution_enabled`) | [`detect_twt`] |
//!
//! Law 2 binds hardest here: detection writes signal rows and places, arms or sizes nothing.

mod basket;
mod common;
mod precision;
mod qulla;
mod swing;
mod swing_market;
mod twt;
mod vbt;

use chrono::NaiveDate;
use sqlx::PgConnection;

use crate::error::WorkerError;
use crate::steps::StepOutcome;

/// Step 11: the basket cache; returns the number of names (0 → the orchestrator marks the step
/// `skipped`).
///
/// # Errors
/// Python's, as the task raises them (it records its own failures and returns).
pub async fn refresh_basket(
    conn: &mut PgConnection,
    step: &mut StepOutcome,
    on: NaiveDate,
) -> Result<i64, WorkerError> {
    basket::run_refresh_basket(conn, step, on).await
}

/// Step 12's detector: `run_detect_swing(session, step, trade_date, user_id=, index_slug=,
/// execution_enabled=, pipeline_run_id=)`.
///
/// # Errors
/// Python's; the orchestrator turns any of them into `skipped`.
pub async fn detect_swing(
    conn: &mut PgConnection,
    step: &mut StepOutcome,
    on: NaiveDate,
    user_id: i64,
    index_slug: &str,
    _execution_enabled: bool,
    pipeline_run_id: i64,
) -> Result<(), WorkerError> {
    // `execution_enabled` is read by nothing since A10 (SW10.5): the ladder reads real closes
    // whatever the flag says. It stays in the signature because the orchestrator passes it.
    swing::run_detect_swing(conn, step, on, user_id, index_slug, pipeline_run_id)
        .await
        .map(|_| ())
}

/// Step 13's detector: `run_detect_vbt(session, step, trade_date, user_id=, pipeline_run_id=)`.
///
/// # Errors
/// Python's; the orchestrator turns any of them into `skipped`.
pub async fn detect_vbt(
    conn: &mut PgConnection,
    step: &mut StepOutcome,
    on: NaiveDate,
    user_id: i64,
    pipeline_run_id: i64,
) -> Result<(), WorkerError> {
    vbt::run_detect_vbt(conn, step, on, user_id, pipeline_run_id)
        .await
        .map(|_| ())
}

/// Step 14's detector: `run_detect_twt(session, step, trade_date, user_id=, pipeline_run_id=)`.
///
/// # Errors
/// Python's; the orchestrator turns any of them into `skipped`.
pub async fn detect_twt(
    conn: &mut PgConnection,
    step: &mut StepOutcome,
    on: NaiveDate,
    user_id: i64,
    pipeline_run_id: i64,
) -> Result<(), WorkerError> {
    twt::run_detect_twt(conn, step, on, user_id, pipeline_run_id)
        .await
        .map(|_| ())
}

/// Wave 6, lane `sleeve_backtests`: the loaders the three sleeves' backtests share with their
/// detectors, re-exported so the backtests reuse them rather than fork them (added; nothing
/// above changed).
pub(crate) mod for_backtests {
    pub(crate) use super::common::{
        NSE_EXCHANGE_ID, UniverseRule, core_err, load_raw_bars, load_universe, lookback_start,
        pl_err, value_err,
    };
    pub(crate) use super::swing::{config_row, swing_config};
    pub(crate) use super::swing_market::{
        BreadthRow, BreadthSnapshot, INDEX_HISTORY_SESSIONS, IndexReading, breadth_snapshot, ema,
        is_liquid, market_gate,
    };
}

/// Wave 6, lane `swing_eod`: the swing detector with Python's optional run id (the tasks
/// `baskfy.swing.detect` and `baskfy.swing.weekend` pass `None`), and the readers the evening
/// shares with it (`load_swing_config`, `lookback_start`, `sleeve_drawdown`, the bars' calendar),
/// re-exported so the evening reuses them rather than forks them (added; nothing above changed).
pub(crate) mod for_swing_eod {
    pub(crate) use super::common::{NSE_EXCHANGE_ID, lookback_start};
    pub(crate) use super::swing::{
        config_row, run_detect_swing_for, sleeve_drawdown, swing_config,
    };
}

/// Wave 6, lane `swing_live`: "Scan now"'s detector (the nightly's, widened by
/// [`ScanExtras`](swing::ScanExtras) rather than forked) and the readers the morning and the
/// scan share with it, re-exported so the lane reuses them (added; nothing above changed).
pub(crate) mod for_swing_live {
    pub(crate) use super::common::{
        date_column, dates, day_number, floats, ints, lookback_start, pl_err, strings,
    };
    pub(crate) use super::swing::{
        BAR_COLUMNS, ScanExtras, config_row, load_swing_bars, run_detect_swing_scan, swing_config,
    };
    pub(crate) use super::swing_market::{is_liquid, py_round};
}

/// Wave 6, lane `twt_jobs`: the TWT detector with Python's optional run id, LV8's provisional
/// bar and flag (`baskfy.twt.detect`/`baskfy.twt.scan` reach it through `detect_session` and
/// `live_scan.run_twt_live`), its config and universe readers, and the bar loader's helpers,
/// re-exported so the tasks reuse them rather than fork them (added; nothing above changed).
pub(crate) mod for_twt_jobs {
    pub(crate) use super::common::{NSE_EXCHANGE_ID, core_err, date_column, day_number, pl_err};
    pub(crate) use super::twt::{load_twt_config, load_twt_universe, run_detect_twt_for};
}

/// Wave 6, lane `vbt_jobs`: the VBT detector with Python's optional run id, LV8's provisional
/// bar and flag (`baskfy.vbt.detect` and `baskfy.vbt.rescan` reach it), its config and universe
/// readers, the bar loader the evening's held-name EMA reuses, and Qullamaggie's adapter pieces
/// (the trailing mean, the trail choice), re-exported so the tasks reuse them rather than fork
/// them (added; nothing above changed).
pub(crate) mod for_vbt_jobs {
    pub(crate) use super::common::{
        NSE_EXCHANGE_ID, core_err, date_column, dates, day_number, floats, ints, load_raw_bars,
        lookback_start, pl_err, value_err,
    };
    pub(crate) use super::qulla::{MA_TRAIL, OhlcBar, PARTIAL, mean, trail_for, trail_value};
    pub(crate) use super::vbt::{
        LOOKBACK_SESSIONS, load_vbt_config, load_vbt_universe, run_detect_vbt_for,
    };
}
