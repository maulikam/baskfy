//! `test_ranking_worker.py` G4 (backfill-ranking) and an `explain` run, on an `rst_*` clone of
//! the bed (`BASKFY_RS_CLIS_DB_URL`), each inside one transaction that is rolled back.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeMap;
use std::future::Future;
use std::pin::Pin;

use chrono::NaiveDate;
use sqlx::{Connection as _, PgConnection};

use super::ranking::{
    COMPUTED, FAILED, PHASE2_COLUMNS, SKIPPED_COMPLETE, day_is_complete, desk_score,
    run_backfill_ranking,
};
use super::session::Flat;
use super::{ExplainError, Value, explain};
use crate::error::WorkerError;
use crate::steps::StepOutcome;

const IGNORE: &str = "needs BASKFY_RS_CLIS_DB_URL (an rst_* clone of the bed)";

static SERIAL: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

pub(crate) async fn clone_conn() -> PgConnection {
    let url = std::env::var("BASKFY_RS_CLIS_DB_URL").unwrap_or_else(|_| panic!("{IGNORE}"));
    let name = url
        .rsplit('/')
        .next()
        .unwrap_or("")
        .split('?')
        .next()
        .unwrap_or("")
        .to_owned();
    assert!(
        name.starts_with("rst_"),
        "refusing the database {name:?}: only rst_* clones"
    );
    PgConnection::connect(&url).await.unwrap()
}

async fn last_days(conn: &mut PgConnection, n: i64) -> Vec<NaiveDate> {
    let mut days: Vec<NaiveDate> = sqlx::query_scalar(
        "SELECT date FROM (SELECT DISTINCT date FROM factor_daily ORDER BY date DESC LIMIT $1) x",
    )
    .bind(n)
    .fetch_all(&mut *conn)
    .await
    .unwrap();
    days.sort();
    days
}

async fn nightly(conn: &mut PgConnection, day: NaiveDate) {
    crate::steps::compute::factors::run(conn, &mut StepOutcome::default(), day)
        .await
        .unwrap();
    desk_score(conn, day).await.unwrap();
}

type Key = (i64, NaiveDate);

async fn phase2_values(conn: &mut PgConnection, days: &[NaiveDate]) -> BTreeMap<Key, String> {
    let cols: Vec<String> = PHASE2_COLUMNS
        .iter()
        .map(|c| format!("{c}::text"))
        .collect();
    let rows: Vec<(i64, NaiveDate, String)> = sqlx::query_as(&format!(
        "SELECT instrument_id, date, concat_ws('|', {}) FROM factor_daily WHERE date = ANY($1)",
        cols.iter()
            .map(|c| format!("coalesce({c}, 'NULL')"))
            .collect::<Vec<_>>()
            .join(", ")
    ))
    .bind(days)
    .fetch_all(&mut *conn)
    .await
    .unwrap();
    rows.into_iter().map(|(i, d, v)| ((i, d), v)).collect()
}

async fn legacy_hashes(conn: &mut PgConnection, days: &[NaiveDate]) -> BTreeMap<Key, String> {
    let quoted: Vec<String> = PHASE2_COLUMNS.iter().map(|c| format!("'{c}'")).collect();
    let rows: Vec<(i64, NaiveDate, String)> = sqlx::query_as(&format!(
        "SELECT instrument_id, date, md5((to_jsonb(f) - ARRAY[{}]::text[])::text) \
         FROM factor_daily f WHERE date = ANY($1)",
        quoted.join(", ")
    ))
    .bind(days)
    .fetch_all(&mut *conn)
    .await
    .unwrap();
    rows.into_iter().map(|(i, d, v)| ((i, d), v)).collect()
}

async fn all_hashes(conn: &mut PgConnection, days: &[NaiveDate]) -> BTreeMap<Key, String> {
    let rows: Vec<(i64, NaiveDate, String)> = sqlx::query_as(
        "SELECT instrument_id, date, md5(to_jsonb(f)::text) FROM factor_daily f \
         WHERE date = ANY($1)",
    )
    .bind(days)
    .fetch_all(&mut *conn)
    .await
    .unwrap();
    rows.into_iter().map(|(i, d, v)| ((i, d), v)).collect()
}

async fn desk_all(conn: &mut PgConnection, days: &[NaiveDate]) -> BTreeMap<Key, String> {
    let rows: Vec<(i64, NaiveDate, String)> = sqlx::query_as(
        "SELECT instrument_id, date, md5(to_jsonb(d)::text) FROM desk_score_daily d \
         WHERE date = ANY($1)",
    )
    .bind(days)
    .fetch_all(&mut *conn)
    .await
    .unwrap();
    rows.into_iter().map(|(i, d, v)| ((i, d), v)).collect()
}

async fn strip_phase2(conn: &mut PgConnection, days: &[NaiveDate]) {
    let sets: Vec<String> = PHASE2_COLUMNS
        .iter()
        .map(|c| format!("{c} = NULL"))
        .collect();
    sqlx::query(&format!(
        "UPDATE factor_daily SET {} WHERE date = ANY($1)",
        sets.join(", ")
    ))
    .bind(days)
    .execute(&mut *conn)
    .await
    .unwrap();
    sqlx::query("DELETE FROM desk_score_daily WHERE date = ANY($1)")
        .bind(days)
        .execute(&mut *conn)
        .await
        .unwrap();
}

fn refuse<'c>(
    conn: &'c mut PgConnection,
    _on: NaiveDate,
) -> Pin<Box<dyn Future<Output = Result<i64, WorkerError>> + 'c>> {
    Box::pin(async move {
        sqlx::query("SELECT 1 / 0").execute(&mut *conn).await?;
        Ok(0)
    })
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
async fn backfill_ranking_rebuilds_phase2_and_leaves_legacy_columns_byte_identical() {
    let _guard = SERIAL.lock().await;
    let mut conn = clone_conn().await;
    let mut tx = conn.begin().await.unwrap();
    let days = last_days(&mut tx, 3).await;
    for day in &days {
        nightly(&mut tx, *day).await;
    }
    let phase2_expected = phase2_values(&mut tx, &days).await;
    let desk_expected = desk_all(&mut tx, &days).await;
    assert!(
        phase2_expected
            .values()
            .any(|v| v.split('|').any(|x| x != "NULL"))
    );
    assert!(!desk_expected.is_empty());

    strip_phase2(&mut tx, &days).await;
    let tampered: i64 =
        sqlx::query_scalar("SELECT min(instrument_id) FROM factor_daily WHERE date = $1")
            .bind(days[0])
            .fetch_one(&mut *tx)
            .await
            .unwrap();
    sqlx::query(
        "UPDATE factor_daily SET ret_1m = 12345.67, close = 1.00 \
         WHERE instrument_id = $1 AND date = $2",
    )
    .bind(tampered)
    .bind(days[0])
    .execute(&mut *tx)
    .await
    .unwrap();
    let legacy_before = legacy_hashes(&mut tx, &days).await;
    assert!(!day_is_complete(&mut tx, days[0]).await.unwrap());

    let report = run_backfill_ranking(&mut Flat(&mut tx), days[0], days[2], false, desk_score)
        .await
        .unwrap();
    let computed: Vec<NaiveDate> = report
        .days
        .iter()
        .filter(|d| d.status == COMPUTED)
        .map(|d| d.day)
        .collect();
    assert_eq!(computed, days);
    assert!(report.errors().is_empty());
    assert_eq!(legacy_hashes(&mut tx, &days).await, legacy_before);
    let ret: String = sqlx::query_scalar(
        "SELECT ret_1m::text FROM factor_daily WHERE instrument_id = $1 AND date = $2",
    )
    .bind(tampered)
    .bind(days[0])
    .fetch_one(&mut *tx)
    .await
    .unwrap();
    assert_eq!(ret.trim_end_matches('0'), "12345.67");
    assert_eq!(phase2_values(&mut tx, &days).await, phase2_expected);
    assert_eq!(desk_all(&mut tx, &days).await, desk_expected);
    for day in &days {
        assert!(day_is_complete(&mut tx, *day).await.unwrap());
    }
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
async fn backfill_ranking_skips_complete_days_unless_forced() {
    let _guard = SERIAL.lock().await;
    let mut conn = clone_conn().await;
    let mut tx = conn.begin().await.unwrap();
    let days = last_days(&mut tx, 3).await;
    for day in &days {
        nightly(&mut tx, *day).await;
    }
    strip_phase2(&mut tx, &days).await;

    let first = run_backfill_ranking(&mut Flat(&mut tx), days[0], days[1], false, desk_score)
        .await
        .unwrap();
    assert!(
        first
            .days
            .iter()
            .filter(|d| d.day == days[0] || d.day == days[1])
            .all(|d| d.status == COMPUTED)
    );
    let after_first = all_hashes(&mut tx, &days).await;

    let resumed = run_backfill_ranking(&mut Flat(&mut tx), days[0], days[2], false, desk_score)
        .await
        .unwrap();
    let statuses: Vec<(NaiveDate, &str)> = resumed
        .days
        .iter()
        .filter(|d| days.contains(&d.day))
        .map(|d| (d.day, d.status))
        .collect();
    assert_eq!(
        statuses,
        vec![
            (days[0], SKIPPED_COMPLETE),
            (days[1], SKIPPED_COMPLETE),
            (days[2], COMPUTED)
        ]
    );
    let settled = all_hashes(&mut tx, &days).await;
    let settled_desk = desk_all(&mut tx, &days).await;
    let not_last = |m: &BTreeMap<Key, String>| -> BTreeMap<Key, String> {
        m.iter()
            .filter(|(k, _)| k.1 != days[2])
            .map(|(k, v)| (*k, v.clone()))
            .collect()
    };
    assert_eq!(not_last(&settled), not_last(&after_first));

    let forced = run_backfill_ranking(&mut Flat(&mut tx), days[0], days[2], true, desk_score)
        .await
        .unwrap();
    assert!(
        forced
            .days
            .iter()
            .filter(|d| days.contains(&d.day))
            .all(|d| d.status == COMPUTED)
    );
    assert_eq!(all_hashes(&mut tx, &days).await, settled);
    assert_eq!(desk_all(&mut tx, &days).await, settled_desk);
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
async fn backfill_ranking_keeps_factor_columns_when_desk_score_fails_and_reports_it() {
    let _guard = SERIAL.lock().await;
    let mut conn = clone_conn().await;
    let mut tx = conn.begin().await.unwrap();
    let days = last_days(&mut tx, 1).await;
    let day = days[0];
    nightly(&mut tx, day).await;
    let phase2_expected = phase2_values(&mut tx, &days).await;
    strip_phase2(&mut tx, &days).await;

    let report = run_backfill_ranking(&mut Flat(&mut tx), day, day, false, refuse)
        .await
        .unwrap();
    assert_eq!(
        report.days.iter().map(|d| d.status).collect::<Vec<_>>(),
        vec![FAILED]
    );
    assert!(report.errors()[0].contains("desk_score"));
    assert!(report.errors()[0].contains("division by zero"));
    assert_eq!(phase2_values(&mut tx, &days).await, phase2_expected);
    assert!(!day_is_complete(&mut tx, day).await.unwrap());
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_CLIS_DB_URL"]
async fn explain_recomputes_a_stored_factor_and_names_its_errors() {
    let _guard = SERIAL.lock().await;
    let mut conn = clone_conn().await;
    let mut tx = conn.begin().await.unwrap();
    let day = last_days(&mut tx, 1).await[0];
    let (symbol, stored): (String, Option<String>) = sqlx::query_as(
        "SELECT i.symbol, f.sharpe_12m::text FROM factor_daily f JOIN instrument i \
         ON i.id = f.instrument_id WHERE f.date = $1 AND f.sharpe_12m IS NOT NULL \
         ORDER BY i.symbol LIMIT 1",
    )
    .bind(day)
    .fetch_one(&mut *tx)
    .await
    .unwrap();
    let e = explain(&mut tx, &symbol, day, "sharpe_12m").await.unwrap();
    let Value::Float(v) = e.value else {
        panic!("sharpe_12m must be a number, got {:?}", e.value);
    };
    let stored: f64 = stored.unwrap().parse().unwrap();
    assert!((v - stored).abs() < 1e-9, "{v} vs stored {stored}");
    assert_eq!(e.inputs.len(), 3);
    assert!(!e.series_preview.is_empty() && e.series_preview.len() <= 10);
    assert!(!e.window_lengths.is_empty());

    let all = explain(&mut tx, &symbol, day, "all").await.unwrap();
    assert_eq!(all.value, Value::None);
    assert!(all.inputs.iter().all(|(k, _)| !k.starts_with('_')));

    match explain(&mut tx, &symbol, day, "no_such_factor").await {
        Err(ExplainError::Key(m)) => assert!(m.starts_with("unknown factor 'no_such_factor'")),
        other => panic!("expected a KeyError, got {other:?}"),
    }
    match explain(&mut tx, "NO-SUCH-SYMBOL", day, "sharpe_12m").await {
        Err(e @ ExplainError::Lookup(_)) => {
            assert_eq!(e.printed(), "no instrument with symbol 'NO-SUCH-SYMBOL");
        }
        other => panic!("expected a LookupError, got {other:?}"),
    }
    tx.rollback().await.unwrap();
}
