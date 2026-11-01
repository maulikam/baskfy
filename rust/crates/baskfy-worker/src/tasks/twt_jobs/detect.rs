//! TW4's one-session rule, shared by the 21:00 retry (`baskfy.twt.detect`) and **Scan now**
//! (`baskfy.twt.scan`, `force=True`). Ports `tasks/twt.detect_session` and
//! `published_session_count` over wave 5's detector (`steps::sleeves::twt`, reused through
//! `for_twt_jobs`, never forked).
//!
//! The retry asks the **breadth** table, not the signal table (TW4.3): this sleeve signals about
//! eighteen times a year, so "no signal rows" is the ordinary state of a session that ran.

use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use sqlx::PgConnection;

use crate::error::WorkerError;
use crate::pyjson::{int, object, s};
use crate::steps::StepOutcome;
use crate::steps::sleeves::for_twt_jobs::run_detect_twt_for;

/// `published_session_count`: `tw_breadth_daily` rows for the session (one per session seen,
/// thin ones included).
pub(crate) async fn published_session_count(
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
) -> Result<i64, WorkerError> {
    let (count,): (i64,) =
        sqlx::query_as("SELECT count(*) FROM tw_breadth_daily WHERE user_id = $1 AND date = $2")
            .bind(user_id)
            .bind(on)
            .fetch_one(&mut *conn)
            .await?;
    Ok(count)
}

/// `detect_session(session, day, user_id=, force=)`: detect unless the session already has a
/// breadth row; `force` re-detects (still idempotent: every write underneath is an upsert).
pub(crate) async fn detect_session(
    conn: &mut PgConnection,
    day: NaiveDate,
    user_id: i64,
    force: bool,
) -> Result<OrderedJson, WorkerError> {
    let date = day.format("%Y-%m-%d").to_string();
    if !force {
        let already = published_session_count(conn, user_id, day).await?;
        if already != 0 {
            return Ok(object([
                ("date", s(date)),
                ("skipped", s("already detected")),
                ("rows", int(already)),
            ]));
        }
    }
    let mut outcome = StepOutcome::default();
    let signals = run_detect_twt_for(conn, &mut outcome, day, user_id, None, None, false).await?;
    Ok(object([
        ("date", s(date)),
        ("signals", int(signals)),
        ("status", s(outcome.status.value())),
        ("detail", outcome.detail_json()),
    ]))
}
