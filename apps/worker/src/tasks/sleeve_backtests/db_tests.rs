//! `test_twt_backtest_job.py`'s database lifecycle tests, ported: the row written on the way in,
//! an empty plant failing loudly with `error` and `finished_at` on the row, and two runs being
//! two rows (append-only).
//!
//! **Where they run.** As the other lanes' (DECISIONS-RS R7.9): one connection to a *scratch*
//! database, inside `TEMP` tables shaped like the production ones — never the bed's `baskfy`.
//! Set `BASKFY_RS_SCRATCH_DATABASE_URL` and run `cargo test -p baskfy-worker -- --ignored
//! sleeve_backtests`; without it they are reported `ignored`. The planted-trade tests are graded
//! on the bed's real bars instead, by `tools/parity/twt_backtest_diff.py` and `task_diff.py`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use chrono::{DateTime, TimeZone, Utc};
use sqlx::{Connection as _, PgConnection};

use super::twt::{TwtBacktestArgs, run_twt_backtest};

const IGNORE: &str =
    "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored";

async fn scratch() -> PgConnection {
    let url =
        std::env::var("BASKFY_RS_SCRATCH_DATABASE_URL").unwrap_or_else(|_| panic!("{IGNORE}"));
    let database = url
        .rsplit('/')
        .next()
        .unwrap_or("")
        .split('?')
        .next()
        .unwrap_or("");
    assert!(
        database != "baskfy" && !database.is_empty(),
        "refusing the database {database:?}: these tests write temp tables"
    );
    let mut conn = PgConnection::connect(&url).await.unwrap();
    for ddl in [
        "CREATE TEMP TABLE ohlcv_daily (instrument_id bigint NOT NULL, date date NOT NULL, \
         PRIMARY KEY (instrument_id, date))",
        "CREATE TEMP TABLE tw_backtest_run (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, \
         user_id bigint NOT NULL, source varchar(24) NOT NULL, params jsonb NOT NULL, \
         started_at timestamptz NOT NULL, finished_at timestamptz, stats jsonb, drift jsonb, \
         error text, created_at timestamptz NOT NULL DEFAULT now(), \
         CHECK (finished_at IS NULL OR finished_at >= started_at))",
    ] {
        sqlx::query(ddl).execute(&mut conn).await.unwrap();
    }
    conn
}

fn at() -> DateTime<Utc> {
    Utc.with_ymd_and_hms(2026, 10, 30, 5, 30, 0)
        .single()
        .unwrap()
}

type Row = (
    i64,
    serde_json::Value,
    Option<DateTime<Utc>>,
    Option<String>,
    bool,
);

async fn rows(conn: &mut PgConnection) -> Vec<Row> {
    sqlx::query_as(
        "SELECT id, params, finished_at, error, stats IS NULL FROM tw_backtest_run ORDER BY id",
    )
    .fetch_all(conn)
    .await
    .unwrap()
}

/// `test_an_empty_plant_raises_rather_than_reporting_a_zero_return` and
/// `test_a_run_that_raises_records_its_error_...`: the row says what was asked, then why it failed.
#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn an_empty_plant_fails_loudly_and_the_row_says_why() {
    let mut conn = scratch().await;
    let clock = at;
    let err = run_twt_backtest(&mut conn, &clock, 7, &TwtBacktestArgs::default())
        .await
        .err()
        .unwrap();
    assert_eq!(err.python_type(), "ValueError");
    assert!(err.to_string().contains("no bars at all"));
    let stored = rows(&mut conn).await;
    assert_eq!(stored.len(), 1);
    let (_, params, finished_at, error, stats_null) = &stored[0];
    assert_eq!(params["start"], "2017-10-16");
    assert_eq!(params["sleeve_inr"], "1000000");
    assert_eq!(params["source"], "PLANT");
    assert_eq!(*finished_at, Some(at()));
    assert!(
        error
            .as_deref()
            .unwrap()
            .starts_with("ValueError: the plant holds no bars")
    );
    assert!(*stats_null, "a failed run carries no stats");
}

/// `test_two_runs_of_the_same_date_append_and_edit_nothing`: each run is its own row.
#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn two_runs_are_two_rows() {
    let mut conn = scratch().await;
    let clock = at;
    for sleeve in [None, Some(rust_decimal::Decimal::from(2_500_000_i64))] {
        let args = TwtBacktestArgs {
            sleeve_inr: sleeve,
            ..TwtBacktestArgs::default()
        };
        assert!(run_twt_backtest(&mut conn, &clock, 7, &args).await.is_err());
    }
    let stored = rows(&mut conn).await;
    assert_eq!(stored.len(), 2);
    assert_eq!(stored[0].1["sleeve_inr"], "1000000");
    assert_eq!(stored[1].1["sleeve_inr"], "2500000");
}
