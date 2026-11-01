//! Which trading sessions never landed, and the standing instruction to run them again (M84).
//! Ports `baskfy_worker.catch_up` — **owned by the gate lane**.
//!
//! "Landed" means one thing: a `pipeline_run` row for the date carrying a `data_version`. Only
//! `publish` sets that column and only a run that passed the quality gate reaches `publish`. A
//! run that failed, was abandoned or is still going has none, so it is not landed, whatever its
//! status says.
//!
//! This is **not** a retry loop around a failed gate: a day the gate refused is *known bad*
//! ([`gate_refused_sessions`]) and is never proposed again on a timer (5 Sep 2026: four full
//! chains for 2026-09-04, all refused for the same uningested split). Nor does it ever propose a
//! session still in progress: today is excluded until [`SESSION_DATA_READY_IST`], so the
//! published series never claims today while today is still running.
//!
//! Not ported here: the Celery task `session_catch_up` itself, which runs `nightly_pipeline` for
//! each day and then `swing_eod_task` — the swing desk's half is a later wave (DECISIONS-RS
//! R-gate.4). The Rust `catch-up` command lists what the sweep would run.

use std::collections::BTreeSet;

use chrono::{Days, NaiveDate, NaiveDateTime, NaiveTime};
use sqlx::PgConnection;

use crate::calendar::NSE_EXCHANGE_ID;
use crate::error::WorkerError;

/// `QUALITY_GATE_STEP`: the step whose failure means "the chain reached a verdict and the verdict
/// was no".
pub const QUALITY_GATE_STEP: &str = "data_quality_gate";

/// `STEP_FAILED`.
pub const STEP_FAILED: &str = "failed";

/// `SESSION_DATA_READY_IST = dt.time(18, 0)`: NSE publishes the day's bhavcopy well after the
/// 15:30 close, so before 18:00 IST there is nothing to ingest for today and nothing to gate.
/// **18:00 is the bhavcopy's hour and it stays that way** (M88 moved it to 15:45 on the evidence
/// of three liquid symbols and wrote a partial day the gate refused three times — 3,056 bars
/// against a 10-day median of 3,524 — reverted 8 Sep 2026). The published series never claims
/// today while today is still running.
pub const SESSION_DATA_READY_IST: NaiveTime = match NaiveTime::from_hms_opt(18, 0, 0) {
    Some(t) => t,
    None => NaiveTime::MIN,
};

/// `DEFAULT_LOOKBACK_DAYS`: a week covers a long weekend plus the two days it takes anybody to
/// notice.
pub const DEFAULT_LOOKBACK_DAYS: i64 = 7;

/// `DEFAULT_MAX_SESSIONS`: how many sessions one sweep runs.
pub const DEFAULT_MAX_SESSIONS: usize = 2;

/// `baskfy_core.models.reference.INFERRED_HOLIDAY_NAME`.
pub const INFERRED_HOLIDAY_NAME: &str = "inferred: no instrument traded";

/// `_has_had_time_to_publish(day, now)`: false for today before the data can exist (`now` is an
/// IST wall-clock moment); any past session has had its chance; a future one has not.
#[must_use]
pub fn has_had_time_to_publish(day: NaiveDate, now: Option<NaiveDateTime>) -> bool {
    let Some(now) = now else {
        return true;
    };
    if day < now.date() {
        return true;
    }
    if day > now.date() {
        return false;
    }
    now.time() >= SESSION_DATA_READY_IST
}

/// `landed_sessions`: the trade dates in `[start, end]` that have a published run.
///
/// # Errors
/// The database's.
pub async fn landed_sessions(
    conn: &mut PgConnection,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<BTreeSet<NaiveDate>, WorkerError> {
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT DISTINCT pipeline_run.trade_date \nFROM pipeline_run \nWHERE \
         pipeline_run.trade_date >= $1::DATE AND pipeline_run.trade_date <= $2::DATE AND \
         pipeline_run.data_version IS NOT NULL",
    )
    .bind(start)
    .bind(end)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().map(|(d,)| d).collect())
}

/// `gate_refused_sessions`: trade dates whose chain ran to a verdict and the verdict was **no**
/// (an unpublished run with a failed `data_quality_gate` step).
///
/// # Errors
/// The database's.
pub async fn gate_refused_sessions(
    conn: &mut PgConnection,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<BTreeSet<NaiveDate>, WorkerError> {
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT DISTINCT pipeline_run.trade_date \nFROM pipeline_run JOIN pipeline_run_step ON \
         pipeline_run_step.run_id = pipeline_run.id \nWHERE pipeline_run.trade_date >= $1::DATE \
         AND pipeline_run.trade_date <= $2::DATE AND pipeline_run.data_version IS NULL AND \
         pipeline_run_step.step = $3::VARCHAR AND pipeline_run_step.status = $4::VARCHAR",
    )
    .bind(start)
    .bind(end)
    .bind(QUALITY_GATE_STEP)
    .bind(STEP_FAILED)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().map(|(d,)| d).collect())
}

/// `unlanded_sessions(session, through=, lookback_days=, now=)`: trading days in the window that
/// were never published and were not refused by the gate, oldest first. The calendar decides
/// "was there a session", never the weekday; a date it does not carry is not proposed.
///
/// # Errors
/// `ValueError` for a negative lookback; the database's.
pub async fn unlanded_sessions(
    conn: &mut PgConnection,
    through: NaiveDate,
    lookback_days: i64,
    now: Option<NaiveDateTime>,
) -> Result<Vec<NaiveDate>, WorkerError> {
    if lookback_days < 0 {
        return Err(WorkerError::python(
            "ValueError",
            format!("lookback_days must not be negative, got {lookback_days}"),
        ));
    }
    let start = through
        .checked_sub_days(Days::new(lookback_days.unsigned_abs()))
        .ok_or_else(|| {
            WorkerError::python("OverflowError", "date value out of range".to_owned())
        })?;
    // Python reads the calendar without an exchange filter; so does this.
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT trading_day.date \nFROM trading_day \nWHERE trading_day.date >= $1::DATE AND \
         trading_day.date <= $2::DATE AND trading_day.is_trading_day IS true ORDER BY \
         trading_day.date ASC",
    )
    .bind(start)
    .bind(through)
    .fetch_all(&mut *conn)
    .await?;
    let landed = landed_sessions(conn, start, through).await?;
    let refused = gate_refused_sessions(conn, start, through).await?;
    Ok(select_unlanded(
        rows.into_iter().map(|(d,)| d),
        &landed,
        &refused,
        now,
    ))
}

/// The sweep's filter, pure: a session not landed, not refused, and old enough to have data.
pub fn select_unlanded(
    sessions: impl IntoIterator<Item = NaiveDate>,
    landed: &BTreeSet<NaiveDate>,
    refused: &BTreeSet<NaiveDate>,
    now: Option<NaiveDateTime>,
) -> Vec<NaiveDate> {
    sessions
        .into_iter()
        .filter(|day| {
            !landed.contains(day) && !refused.contains(day) && has_had_time_to_publish(*day, now)
        })
        .collect()
}

/// `demote_derived_after_missing_bhavcopy(session, day, published=)`: a weekday the seed only
/// *guessed* was a session (`source='derived'`) becomes an inferred holiday after one clear
/// "NSE published nothing", so the sweep stops re-proposing a 404 (AF 3.11). Returns whether it
/// demoted.
///
/// # Errors
/// The database's.
pub async fn demote_derived_after_missing_bhavcopy(
    conn: &mut PgConnection,
    day: NaiveDate,
    published: bool,
) -> Result<bool, WorkerError> {
    if published {
        return Ok(false);
    }
    let rows: Vec<(bool, String)> = sqlx::query_as(
        "SELECT trading_day.is_trading_day, trading_day.source \nFROM trading_day \nWHERE \
         trading_day.exchange_id = $1::SMALLINT AND trading_day.date = $2::DATE",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(day)
    .fetch_all(&mut *conn)
    .await?;
    if rows.len() > 1 {
        return Err(WorkerError::python(
            "MultipleResultsFound",
            "Multiple rows were found when one or none was required",
        ));
    }
    let Some((is_trading_day, source)) = rows.into_iter().next() else {
        return Ok(false);
    };
    if source != "derived" || !is_trading_day {
        return Ok(false);
    }
    mark_inferred_holiday(conn, day).await?;
    Ok(true)
}

/// `calendar.mark_inferred_holiday`: the calendar upsert AF 3.11's catch-up and the reconcile
/// share (`is_trading_day=False, source='bhavcopy', holiday_name=INFERRED_HOLIDAY_NAME`).
///
/// # Errors
/// The database's.
pub async fn mark_inferred_holiday(
    conn: &mut PgConnection,
    day: NaiveDate,
) -> Result<(), WorkerError> {
    sqlx::query(
        "INSERT INTO trading_day (exchange_id, date, is_trading_day, holiday_name, source) \
         VALUES ($1::SMALLINT, $2::DATE, $3, $4::VARCHAR, $5::VARCHAR) ON CONFLICT \
         (exchange_id, date) DO UPDATE SET is_trading_day = excluded.is_trading_day, \
         holiday_name = excluded.holiday_name, source = excluded.source",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(day)
    .bind(false)
    .bind(INFERRED_HOLIDAY_NAME)
    .bind("bhavcopy")
    .execute(&mut *conn)
    .await?;
    Ok(())
}

/// What one sweep would do with `missing`: run the oldest `max_sessions`, report the rest as
/// `remaining` for the next trigger (`session_catch_up`'s bound).
#[must_use]
pub fn split_sweep(missing: &[NaiveDate], max_sessions: usize) -> (Vec<NaiveDate>, Vec<NaiveDate>) {
    let cut = missing.len().min(max_sessions);
    let (run, remaining) = missing.split_at(cut);
    (run.to_vec(), remaining.to_vec())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(y: i32, m: u32, day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, day).unwrap_or_default()
    }

    fn at(dd: &str, t: &str) -> Option<NaiveDateTime> {
        NaiveDateTime::parse_from_str(&format!("{dd} {t}"), "%Y-%m-%d %H:%M").ok()
    }

    // The real week of the incident (5 Sep 2026 is the Saturday).
    fn mon() -> NaiveDate {
        d(2026, 8, 31)
    }
    fn tue() -> NaiveDate {
        d(2026, 9, 1)
    }
    fn wed() -> NaiveDate {
        d(2026, 9, 2)
    }
    fn thu() -> NaiveDate {
        d(2026, 9, 3)
    }
    fn fri() -> NaiveDate {
        d(2026, 9, 4)
    }

    fn week() -> Vec<NaiveDate> {
        vec![mon(), tue(), wed(), thu(), fri()]
    }

    fn set(days: &[NaiveDate]) -> BTreeSet<NaiveDate> {
        days.iter().copied().collect()
    }

    #[test]
    fn today_waits_for_eighteen_hundred_ist() {
        let day = d(2026, 10, 29);
        assert_eq!(SESSION_DATA_READY_IST.to_string(), "18:00:00");
        assert!(!has_had_time_to_publish(day, at("2026-10-29", "15:45")));
        assert!(!has_had_time_to_publish(day, at("2026-10-29", "17:59")));
        assert!(has_had_time_to_publish(day, at("2026-10-29", "18:00")));
        assert!(has_had_time_to_publish(day, at("2026-10-30", "06:45")));
        assert!(!has_had_time_to_publish(day, at("2026-10-28", "23:00")));
        assert!(has_had_time_to_publish(day, None));
    }

    #[test]
    fn the_cutoff_is_18_00_and_there_is_no_earlier_kite_cutoff() {
        // M88 reverted: 15:45 is not a cutoff for anything.
        assert_eq!(
            SESSION_DATA_READY_IST,
            NaiveTime::from_hms_opt(18, 0, 0).unwrap_or_default()
        );
        assert!(!has_had_time_to_publish(fri(), at("2026-09-04", "15:46")));
    }

    #[test]
    fn a_gate_refusal_is_not_proposed_again_but_a_killed_run_is() {
        let refused = select_unlanded([thu()], &set(&[]), &set(&[thu()]), None);
        assert!(refused.is_empty(), "the gate already judged the day wrong");
        let killed = select_unlanded([thu()], &set(&[]), &set(&[]), None);
        assert_eq!(killed, [thu()]);
    }

    #[test]
    fn the_3_sep_shape_is_unlanded_and_published_days_never_are() {
        let missing = select_unlanded(
            [wed(), thu(), fri()],
            &set(&[wed(), fri()]),
            &set(&[]),
            None,
        );
        assert_eq!(missing, [thu()]);
        let none = select_unlanded(
            [wed(), thu(), fri()],
            &set(&[wed(), thu(), fri()]),
            &set(&[]),
            None,
        );
        assert!(none.is_empty());
    }

    #[test]
    fn today_is_not_missing_until_its_data_can_exist_but_yesterday_is() {
        let morning = at("2026-09-04", "06:45");
        assert!(select_unlanded([fri()], &set(&[]), &set(&[]), morning).is_empty());
        assert_eq!(
            select_unlanded([fri()], &set(&[]), &set(&[]), at("2026-09-04", "18:30")),
            [fri()]
        );
        assert_eq!(
            select_unlanded([thu(), fri()], &set(&[]), &set(&[]), morning),
            [thu()],
            "the 06:45 sweep would have walked past last night's hole"
        );
    }

    #[test]
    fn oldest_first_and_bounded() {
        let missing = select_unlanded(week(), &set(&[]), &set(&[]), None);
        assert_eq!(missing, week());
        let (run, remaining) = split_sweep(&missing, DEFAULT_MAX_SESSIONS);
        assert_eq!(run, [mon(), tue()]);
        assert_eq!(remaining, [wed(), thu(), fri()]);
        let (run, remaining) = split_sweep(&[], DEFAULT_MAX_SESSIONS);
        assert!(run.is_empty() && remaining.is_empty());
    }
}
