//! `test_ops_and_alerts.py`'s `TestTheReaper` and `TestThePublishDeadline`, on a rolled-back
//! transaction over a clone of the bed (`BASKFY_RS_OPS_DB_URL`, an `rst_*`/`rsn_*` database).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_api::Settings;
use baskfy_db::OrderedJson;
use chrono::{DateTime, Duration, NaiveDate, TimeZone as _, Utc};
use sqlx::{Connection as _, PgConnection};

use super::alerting::{Name, Severity};
use super::checks::{check_publish_deadline, reap_abandoned_runs};

const IGNORE: &str = "needs BASKFY_RS_OPS_DB_URL (a clone of the bed, never baskfy)";

/// A Tuesday beyond the bed's calendar, so no real run meets it.
fn trade_date() -> NaiveDate {
    NaiveDate::from_ymd_opt(2031, 8, 19).unwrap()
}

async fn clone() -> PgConnection {
    let url = std::env::var("BASKFY_RS_OPS_DB_URL").unwrap_or_else(|_| panic!("{IGNORE}"));
    let database = url
        .rsplit('/')
        .next()
        .unwrap_or("")
        .split('?')
        .next()
        .unwrap_or("");
    assert!(
        database.starts_with("rst_") || database.starts_with("rsn_"),
        "refusing {database:?}: these tests need a clone of the bed"
    );
    let mut conn = PgConnection::connect(&url).await.unwrap();
    sqlx::query("BEGIN").execute(&mut conn).await.unwrap();
    conn
}

fn utc(h: u32, m: u32) -> DateTime<Utc> {
    Utc.from_utc_datetime(&trade_date().and_hms_opt(h, m, 0).unwrap())
}

async fn running(conn: &mut PgConnection, started: DateTime<Utc>) -> i64 {
    let (id,): (i64,) = sqlx::query_as(
        "INSERT INTO pipeline_run (trade_date, status, started_at) VALUES ($1, 'running', $2) \
         RETURNING id",
    )
    .bind(trade_date())
    .bind(started)
    .fetch_one(&mut *conn)
    .await
    .unwrap();
    id
}

async fn ours(conn: &mut PgConnection, now: DateTime<Utc>) -> Vec<super::alerting::OpsAlert> {
    let day = trade_date().to_string();
    reap_abandoned_runs(conn, &Settings::default(), now)
        .await
        .unwrap()
        .into_iter()
        .filter(|a| a.labels.iter().any(|(k, v)| k == "trade_date" && *v == day))
        .collect()
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPS_DB_URL"]
async fn a_healthy_long_run_is_not_reaped() {
    let mut conn = clone().await;
    let now = utc(20, 0);
    running(&mut conn, now - Duration::minutes(40)).await;
    assert!(
        ours(&mut conn, now).await.is_empty(),
        "40 minutes is a slow night, not a death"
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPS_DB_URL"]
async fn an_abandoned_run_becomes_failed_and_alerts_once() {
    let mut conn = clone().await;
    let now = utc(22, 0);
    let id = running(&mut conn, now - Duration::minutes(120)).await;
    let alerts = ours(&mut conn, now).await;
    assert_eq!(alerts.len(), 1);
    let alert = &alerts[0];
    assert_eq!(
        (alert.name, alert.severity),
        (Name::PipelineAbandoned, Severity::Critical)
    );
    assert_eq!(
        alert.detail.get("age_minutes"),
        Some(&OrderedJson::Int(120))
    );
    assert_eq!(alert.name.runbook(), "docs/runbooks/pipeline-failed.md");
    let (status, finished, version): (String, Option<DateTime<Utc>>, Option<i64>) =
        sqlx::query_as("SELECT status, finished_at, data_version FROM pipeline_run WHERE id = $1")
            .bind(id)
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!(
        (status.as_str(), finished, version),
        ("failed", Some(now), None)
    );
    assert!(
        ours(&mut conn, now).await.is_empty(),
        "a second sweep finds nothing"
    );
}

async fn trading(conn: &mut PgConnection, day: NaiveDate, open: bool) {
    sqlx::query(
        "INSERT INTO trading_day (exchange_id, date, is_trading_day, source) VALUES (1, $1, $2, \
         'derived') ON CONFLICT (exchange_id, date) DO UPDATE SET is_trading_day = excluded.is_trading_day",
    )
    .bind(day)
    .bind(open)
    .execute(&mut *conn)
    .await
    .unwrap();
}

/// 20:15 IST on the trade date.
fn deadline_moment(day: NaiveDate) -> DateTime<Utc> {
    Utc.from_utc_datetime(&day.and_hms_opt(14, 45, 0).unwrap())
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_OPS_DB_URL"]
async fn the_publish_deadline() {
    let mut conn = clone().await;
    let settings = Settings::default();
    trading(&mut conn, trade_date(), true).await;
    let moment = deadline_moment(trade_date());
    let alert = check_publish_deadline(&mut conn, &settings, moment)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(alert.name, Name::PublishLate);
    assert_eq!(
        alert.labels,
        vec![("trade_date".to_owned(), trade_date().to_string())]
    );
    assert!(alert.summary.contains("nothing has published by 20:15 IST"));
    sqlx::query(
        "INSERT INTO pipeline_run (trade_date, status, started_at, finished_at, data_version) \
         VALUES ($1, 'succeeded', $2, $3, 1)",
    )
    .bind(trade_date())
    .bind(moment - Duration::hours(1))
    .bind(moment - Duration::minutes(5))
    .execute(&mut conn)
    .await
    .unwrap();
    assert!(
        check_publish_deadline(&mut conn, &settings, moment)
            .await
            .unwrap()
            .is_none()
    );
    let sunday = NaiveDate::from_ymd_opt(2031, 8, 17).unwrap();
    trading(&mut conn, sunday, false).await;
    assert!(
        check_publish_deadline(&mut conn, &settings, deadline_moment(sunday))
            .await
            .unwrap()
            .is_none()
    );
    let unknown = NaiveDate::from_ymd_opt(2031, 8, 20).unwrap();
    assert!(
        check_publish_deadline(&mut conn, &settings, deadline_moment(unknown))
            .await
            .unwrap()
            .is_none(),
        "an absent calendar row is not a trading day"
    );
}
