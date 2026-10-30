//! TW12 — the TWT page's **Scan now** and the publisher that picks the desk's rows up. Ports
//! `baskfy_worker.tasks.twt_scan` (`claim_run`, `unpublished_runs`, `run_twt_scan`) and the
//! one rule all three sleeves' scans share for which session a scan is of
//! (`swing_scan_now.decide_session`, `published_session.last_published_session`).
//!
//! **This module contains no detector**: a published session goes through
//! [`super::detect::detect_session`] with `force=True`, a session in progress through
//! [`super::live::run_twt_live`] — the same detector over one more bar. It writes signals,
//! states, breadth, the run row and (live) a `PROPOSED` plan; never an order.

use baskfy_db::OrderedJson;
use chrono::{DateTime, Duration, NaiveDate, NaiveDateTime, NaiveTime, Utc};
use sqlx::{PgConnection, Row as _};

use super::detect::detect_session;
use super::live::run_twt_live;
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};
use crate::steps::sleeves::for_twt_jobs::NSE_EXCHANGE_ID;
use crate::tasks::TaskContext;

/// `PUBLISH_LIMIT`: how many unpublished rows one publisher pass takes.
pub(crate) const PUBLISH_LIMIT: i64 = 10;

/// `REASON_MARKET_OPEN`.
pub(crate) const REASON_MARKET_OPEN: &str = "market open: today so far, from live quotes";
/// `REASON_PUBLISHED`.
pub(crate) const REASON_PUBLISHED: &str =
    "before the open: the last published session, from published bars";
/// `REASON_AFTER_CLOSE`.
pub(crate) const REASON_AFTER_CLOSE: &str =
    "after the close: today so far, from the day's final quotes";
/// `REASON_ALREADY_PUBLISHED`.
pub(crate) const REASON_ALREADY_PUBLISHED: &str =
    "today is already published: re-detected from published bars";

fn market_open() -> NaiveTime {
    NaiveTime::from_hms_opt(9, 15, 0).unwrap_or_default()
}

fn market_close() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 30, 0).unwrap_or_default()
}

/// `ScanDecision`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ScanDecision {
    pub(crate) session_date: NaiveDate,
    pub(crate) provisional: bool,
    /// The last published session — where a provisional scan's point-in-time reads look.
    pub(crate) published_as_of: NaiveDate,
    pub(crate) reason: &'static str,
}

/// `last_published_session(session)`: the newest run with a `data_version`, by version.
pub(crate) async fn last_published_session(
    conn: &mut PgConnection,
) -> Result<Option<NaiveDate>, WorkerError> {
    let row: Option<(NaiveDate,)> = sqlx::query_as(
        "SELECT trade_date FROM pipeline_run WHERE data_version IS NOT NULL \
         ORDER BY data_version DESC LIMIT 1",
    )
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.map(|r| r.0))
}

async fn is_trading_day(conn: &mut PgConnection, on: NaiveDate) -> Result<bool, WorkerError> {
    let row: Option<(Option<bool>,)> = sqlx::query_as(
        "SELECT is_trading_day FROM trading_day WHERE exchange_id = $1 AND date = $2",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(on)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.and_then(|r| r.0).unwrap_or(false))
}

/// The pure half of `decide_session`, given what the database answered.
pub(crate) fn decide(
    published: NaiveDate,
    now_ist: NaiveDateTime,
    today_is_trading_day: bool,
) -> ScanDecision {
    let today = now_ist.date();
    if published >= today {
        return ScanDecision {
            session_date: published,
            provisional: false,
            published_as_of: published,
            reason: REASON_ALREADY_PUBLISHED,
        };
    }
    let t = now_ist.time();
    // `session_has_started`: from 09:15 at any hour after (SW: a login after the close scans
    // today's final quotes); never before the open, when a quote is yesterday's.
    if t >= market_open() && today_is_trading_day {
        let reason = if t <= market_close() {
            REASON_MARKET_OPEN
        } else {
            REASON_AFTER_CLOSE
        };
        return ScanDecision {
            session_date: today,
            provisional: true,
            published_as_of: published,
            reason,
        };
    }
    ScanDecision {
        session_date: published,
        provisional: false,
        published_as_of: published,
        reason: REASON_PUBLISHED,
    }
}

/// `decide_session(session, now)`; `Err(message)` is `ScanNotRunnable`.
pub(super) async fn decide_session(
    conn: &mut PgConnection,
    now_ist: NaiveDateTime,
) -> Result<Result<ScanDecision, String>, WorkerError> {
    let Some(published) = last_published_session(conn).await? else {
        return Ok(Err(
            "no published bars: the pipeline has not run yet".to_owned()
        ));
    };
    let today = now_ist.date();
    let trading = if published < today && now_ist.time() >= market_open() {
        is_trading_day(conn, today).await?
    } else {
        false
    };
    Ok(Ok(decide(published, now_ist, trading)))
}

/// `unpublished_runs`: `QUEUED` rows with no `task_id`, oldest first.
pub(crate) async fn unpublished_runs(
    conn: &mut PgConnection,
    limit: i64,
) -> Result<Vec<i64>, WorkerError> {
    let rows: Vec<(i64,)> = sqlx::query_as(
        "SELECT id FROM tw_scan_run WHERE status = 'QUEUED' AND task_id IS NULL \
         ORDER BY requested_at LIMIT $1",
    )
    .bind(limit)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().map(|r| r.0).collect())
}

/// The claimed row's user and what it already says (a failure leaves those as they were).
pub(super) struct Claimed {
    user_id: i64,
    provisional: bool,
    session_date: Option<NaiveDate>,
    detail: Option<OrderedJson>,
}

/// `claim_run`: `RUNNING` and `started_at`, or `None` for a row that is not `QUEUED`.
pub(super) async fn claim_run(
    conn: &mut PgConnection,
    run_id: i64,
    started_at: DateTime<Utc>,
) -> Result<Option<Claimed>, WorkerError> {
    let row = sqlx::query(
        "SELECT user_id, status, provisional, session_date, detail::text AS detail \
         FROM tw_scan_run WHERE id = $1",
    )
    .bind(run_id)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(row) = row else {
        return Ok(None);
    };
    let status: String = row.try_get("status")?;
    if status != "QUEUED" {
        return Ok(None);
    }
    sqlx::query("UPDATE tw_scan_run SET status = 'RUNNING', started_at = $2 WHERE id = $1")
        .bind(run_id)
        .bind(started_at)
        .execute(&mut *conn)
        .await?;
    let detail = row
        .try_get::<Option<String>, _>("detail")?
        .map(|text| OrderedJson::parse(&text))
        .transpose()
        .map_err(|e| WorkerError::python("ValueError", e.to_string()))?;
    Ok(Some(Claimed {
        user_id: row.try_get("user_id")?,
        provisional: row.try_get("provisional")?,
        session_date: row.try_get("session_date")?,
        detail,
    }))
}

/// What the run came to.
struct Finished {
    session_date: Option<NaiveDate>,
    provisional: bool,
    detail: Option<OrderedJson>,
}

/// `run_twt_scan(session, run_id, quote_source=..., live=run_twt_live(execution_enabled=))`:
/// detect for the row's user and record what it saw. **Never raises into the worker** for a
/// failure of the work: the row is `FAILED` with `"{type}: {message}"`.
pub(crate) async fn run_twt_scan(
    ctx: &TaskContext,
    conn: &mut PgConnection,
    run_id: i64,
    execution_enabled: bool,
) -> Result<OrderedJson, WorkerError> {
    let stamp = ctx.now_utc();
    let Some(claimed) = claim_run(conn, run_id, ctx.now_utc()).await? else {
        return Ok(object([
            ("run_id", int(run_id)),
            ("skipped", s("not QUEUED — already claimed or finished")),
        ]));
    };
    let work = scan_body(ctx, conn, claimed.user_id, stamp, execution_enabled).await;
    let user_id = claimed.user_id;
    let (status, finished, error) = match work {
        Ok(done) => ("DONE", done, None),
        Err(e) => {
            crate::steps::log_error(&format!(
                "twt scan failed run_id={run_id} user_id={user_id}: {}: {e}",
                e.python_type()
            ));
            (
                "FAILED",
                Finished {
                    session_date: claimed.session_date,
                    provisional: claimed.provisional,
                    detail: claimed.detail,
                },
                Some(format!("{}: {e}", e.python_type())),
            )
        }
    };
    let detail_text = finished
        .detail
        .as_ref()
        .map(OrderedJson::to_python_json)
        .transpose()
        .map_err(|e| WorkerError::python("ValueError", e.to_string()))?;
    if status == "DONE" {
        sqlx::query(
            "UPDATE tw_scan_run SET status = $2, session_date = $3, provisional = $4, \
             detail = CAST($5 AS JSONB), finished_at = $6 WHERE id = $1",
        )
        .bind(run_id)
        .bind(status)
        .bind(finished.session_date)
        .bind(finished.provisional)
        .bind(detail_text)
        .bind(ctx.now_utc())
        .execute(&mut *conn)
        .await?;
    } else {
        sqlx::query(
            "UPDATE tw_scan_run SET status = $2, error = $3, finished_at = $4 WHERE id = $1",
        )
        .bind(run_id)
        .bind(status)
        .bind(&error)
        .bind(ctx.now_utc())
        .execute(&mut *conn)
        .await?;
    }
    Ok(object([
        ("run_id", int(run_id)),
        ("status", s(status)),
        (
            "session_date",
            finished
                .session_date
                .map_or(OrderedJson::Null, |d| s(d.format("%Y-%m-%d").to_string())),
        ),
        ("provisional", OrderedJson::Bool(finished.provisional)),
        ("detail", finished.detail.unwrap_or(OrderedJson::Null)),
        ("error", error.map_or(OrderedJson::Null, s)),
    ]))
}

/// The `try:` body of `run_twt_scan`.
async fn scan_body(
    ctx: &TaskContext,
    conn: &mut PgConnection,
    user_id: i64,
    stamp: DateTime<Utc>,
    execution_enabled: bool,
) -> Result<Finished, WorkerError> {
    let now_ist = (stamp + Duration::minutes(330)).naive_utc();
    let decision = match decide_session(conn, now_ist).await? {
        Ok(decision) => decision,
        Err(reason) => {
            return Err(WorkerError::python(
                "ValueError",
                format!(
                    "the pipeline has published no session on or before {}; there is nothing \
                     to detect until the chain runs ({reason})",
                    stamp.date_naive().format("%Y-%m-%d")
                ),
            ));
        }
    };
    if decision.provisional {
        // `quote_source()` is built here and only here: outside a session nothing calls Kite.
        let quotes = super::quotes::interactive_kite(ctx).await?;
        let report =
            run_twt_live(conn, user_id, &decision, &quotes, stamp, execution_enabled).await?;
        let mut detail = match report {
            OrderedJson::Object(entries) => entries,
            _ => Vec::new(),
        };
        crate::pyjson::update(&mut detail, "reason", s(decision.reason));
        return Ok(Finished {
            session_date: Some(decision.session_date),
            provisional: true,
            detail: Some(OrderedJson::Object(detail)),
        });
    }
    let day = decision.published_as_of;
    let found = detect_session(conn, day, user_id, true).await?;
    let mut detail = match found {
        OrderedJson::Object(entries) => entries,
        _ => Vec::new(),
    };
    crate::pyjson::update(&mut detail, "provisional", OrderedJson::Bool(false));
    crate::pyjson::update(&mut detail, "reason", s(decision.reason));
    Ok(Finished {
        session_date: Some(day),
        provisional: false,
        detail: Some(OrderedJson::Object(detail)),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(day: u32, h: u32, m: u32) -> NaiveDateTime {
        NaiveDate::from_ymd_opt(2026, 10, day)
            .and_then(|d| d.and_hms_opt(h, m, 0))
            .unwrap_or_default()
    }

    fn d(day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(2026, 10, day).unwrap_or_default()
    }

    #[test]
    fn before_the_open_the_scan_is_of_the_last_published_session() {
        let got = decide(d(29), at(30, 9, 14), true);
        assert_eq!(
            got,
            ScanDecision {
                session_date: d(29),
                provisional: false,
                published_as_of: d(29),
                reason: REASON_PUBLISHED
            }
        );
    }

    #[test]
    fn from_the_open_it_is_today_provisional_and_after_the_close_still_today() {
        let open = decide(d(29), at(30, 9, 15), true);
        assert!(open.provisional);
        assert_eq!(open.session_date, d(30));
        assert_eq!(open.published_as_of, d(29));
        assert_eq!(open.reason, REASON_MARKET_OPEN);
        assert_eq!(
            decide(d(29), at(30, 15, 30), true).reason,
            REASON_MARKET_OPEN
        );
        let late = decide(d(29), at(30, 15, 31), true);
        assert!(late.provisional);
        assert_eq!(late.reason, REASON_AFTER_CLOSE);
    }

    #[test]
    fn a_holiday_or_a_published_today_is_never_provisional() {
        assert!(!decide(d(30), at(31, 11, 0), false).provisional);
        let published = decide(d(29), at(29, 21, 30), true);
        assert!(!published.provisional);
        assert_eq!(published.reason, REASON_ALREADY_PUBLISHED);
    }
}
