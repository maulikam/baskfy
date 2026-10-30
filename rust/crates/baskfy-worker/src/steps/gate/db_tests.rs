//! The worker's Python database tests for this lane, ported: `test_quality_gate.py` (each of
//! docs/09's eight assertions against rows) and `test_session_catch_up.py`'s finder half
//! (`unlanded_sessions`, `gate_refused_sessions`, `demote_derived_after_missing_bhavcopy`).
//!
//! **Where they run.** As the index lane's (DECISIONS-RS R7.9): one connection to a *scratch*
//! database, inside `TEMP` tables shaped like the production ones (keys, `NOT NULL`s, numeric
//! scales) — never the bed's `baskfy`. Set `BASKFY_RS_SCRATCH_DATABASE_URL` (the bed container's
//! `postgres` database) and run `cargo test -p baskfy-worker -- --ignored steps::gate`; without
//! it they are reported `ignored`. Temporary tables vanish with the connection. The step-level
//! parity against Python on real clones is `nightly_diff.py --step data_quality_gate|publish`.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

use chrono::{Datelike as _, NaiveDate, NaiveDateTime, Weekday};
use sqlx::{Connection as _, PgConnection};

use super::quality::{
    CheckStatus, Thresholds, check_bar_count_against_baseline, check_factor_rows_match_bars,
    check_index_level_agreement, check_no_duplicate_bars, check_no_null_prices,
    check_no_unexplained_jumps, check_return_distribution, check_universe_sizes,
    run_data_quality_gate,
};
use crate::catch_up::{demote_derived_after_missing_bhavcopy, unlanded_sessions};
use crate::steps::StepOutcome;
use baskfy_db::OrderedJson;

const IGNORE: &str =
    "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored";

fn d(y: i32, m: u32, day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, day).unwrap()
}

/// `helpers.TRADE_DATE` / `PRIOR_DATE`.
fn trade_date() -> NaiveDate {
    d(2026, 8, 18)
}
fn prior_date() -> NaiveDate {
    d(2026, 8, 17)
}

fn thresholds() -> Thresholds {
    // `WorkerSettings(_env_file=None)`'s defaults.
    Thresholds {
        min_bar_ratio: 0.9,
        max_unexplained_return: 0.5,
        membership_tolerance: 0.05,
        ret_median_sigma: 3.0,
    }
}

/// A connection with every table the gate and the sweep read, as `TEMP` tables, and the NSE
/// calendar seeded for Aug-Dec 2026 (weekdays are sessions, as `conftest.clean_db` seeds them).
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
        "CREATE TEMP TABLE trading_day (exchange_id smallint NOT NULL, date date NOT NULL, \
         is_trading_day boolean NOT NULL, holiday_name varchar, source varchar NOT NULL, \
         PRIMARY KEY (exchange_id, date))",
        "CREATE TEMP TABLE pipeline_run (id bigserial PRIMARY KEY, trade_date date NOT NULL, \
         status varchar NOT NULL, started_at timestamptz NOT NULL DEFAULT now(), \
         finished_at timestamptz, data_version bigint)",
        "CREATE TEMP TABLE pipeline_run_step (id bigserial PRIMARY KEY, run_id bigint NOT NULL \
         REFERENCES pipeline_run (id) ON DELETE CASCADE, step varchar NOT NULL, status varchar \
         NOT NULL, rows_in bigint NOT NULL DEFAULT 0, rows_out bigint NOT NULL DEFAULT 0, \
         duration_ms integer, error jsonb, UNIQUE (run_id, step))",
        "CREATE TEMP TABLE instrument (id bigint PRIMARY KEY, symbol varchar NOT NULL UNIQUE, \
         series varchar)",
        "CREATE TEMP TABLE ohlcv_daily (instrument_id bigint NOT NULL, date date NOT NULL, \
         close numeric(18,4) NOT NULL, close_raw numeric(18,4) NOT NULL, volume bigint NOT NULL, \
         upper_circuit numeric(18,4), lower_circuit numeric(18,4), \
         PRIMARY KEY (instrument_id, date))",
        "CREATE TEMP TABLE corporate_action (id bigserial PRIMARY KEY, instrument_id bigint NOT \
         NULL, action_type varchar NOT NULL, ex_date date NOT NULL)",
        "CREATE TEMP TABLE factor_daily (instrument_id bigint NOT NULL, date date NOT NULL, \
         ret_12m numeric(14,2), PRIMARY KEY (instrument_id, date))",
        "CREATE TEMP TABLE index_def (id smallint PRIMARY KEY, slug varchar NOT NULL UNIQUE)",
        "CREATE TEMP TABLE index_member_daily (index_id smallint NOT NULL, date date NOT NULL, \
         instrument_id bigint NOT NULL, weight numeric(10,6), \
         PRIMARY KEY (index_id, date, instrument_id))",
        "CREATE TEMP TABLE index_snapshot_daily (index_id smallint NOT NULL, date date NOT NULL, \
         level numeric(18,4), PRIMARY KEY (index_id, date))",
        "INSERT INTO index_def (id, slug) VALUES (1, 'nifty-50')",
    ] {
        sqlx::query(ddl).execute(&mut conn).await.unwrap();
    }
    let mut day = d(2026, 8, 1);
    while day <= d(2026, 12, 31) {
        let weekend = matches!(day.weekday(), Weekday::Sat | Weekday::Sun);
        sqlx::query(
            "INSERT INTO trading_day (exchange_id, date, is_trading_day, source) \
             VALUES (1, $1, $2, 'seed')",
        )
        .bind(day)
        .bind(!weekend)
        .execute(&mut conn)
        .await
        .unwrap();
        day = day.succ_opt().unwrap();
    }
    conn
}

async fn exec(conn: &mut PgConnection, sql: &str) {
    sqlx::query(sql).execute(&mut *conn).await.unwrap();
}

async fn make_instrument(conn: &mut PgConnection, symbol: &str, id: i64) -> i64 {
    sqlx::query("INSERT INTO instrument (id, symbol, series) VALUES ($1, $2, 'EQ')")
        .bind(id)
        .bind(symbol)
        .execute(&mut *conn)
        .await
        .unwrap();
    id
}

async fn add_bar(
    conn: &mut PgConnection,
    id: i64,
    on: NaiveDate,
    close: &str,
    circuits: (Option<&str>, Option<&str>),
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO ohlcv_daily (instrument_id, date, close, close_raw, volume, upper_circuit, \
         lower_circuit) VALUES ($1, $2, $3::numeric, $3::numeric, 1000, $4::numeric, $5::numeric)",
    )
    .bind(id)
    .bind(on)
    .bind(close)
    .bind(circuits.0)
    .bind(circuits.1)
    .execute(&mut *conn)
    .await
    .map(|_| ())
}

async fn bar(conn: &mut PgConnection, id: i64, on: NaiveDate, close: &str) {
    add_bar(conn, id, on, close, (None, None)).await.unwrap();
}

/// `run_compute_factors`'s row-count effect: one factor row per bar of the day (the factor
/// engine is the compute lane's; the gate reads only the count and `ret_12m`).
async fn factor_rows_for_bars(conn: &mut PgConnection, on: NaiveDate) {
    sqlx::query(
        "INSERT INTO factor_daily (instrument_id, date, ret_12m) \
         SELECT instrument_id, date, NULL FROM ohlcv_daily WHERE date = $1",
    )
    .bind(on)
    .execute(&mut *conn)
    .await
    .unwrap();
}

// --- the gate itself ---------------------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn every_assertion_number_appears_exactly_once_and_the_payload_records_them() {
    let mut c = scratch().await;
    let mut outcome = StepOutcome::default();
    let results = run_data_quality_gate(&mut c, &mut outcome, trade_date(), thresholds())
        .await
        .unwrap();
    let mut numbers: Vec<i64> = results.iter().map(|r| r.assertion).collect();
    numbers.sort_unstable();
    assert_eq!(numbers, (1..=8).collect::<Vec<_>>());
    assert!(results.iter().any(|r| r.status == CheckStatus::Skipped));
    let Some(OrderedJson::Array(checks)) = outcome.detail_json().get("checks").cloned() else {
        panic!("checks is a list");
    };
    assert_eq!(checks.len(), 8);
    assert_eq!(outcome.rows_in, 8);
}

// --- assertion 1 -------------------------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn bar_count_skips_passes_and_fails() {
    let mut c = scratch().await;
    let r = check_bar_count_against_baseline(&mut c, trade_date(), thresholds())
        .await
        .unwrap();
    assert_eq!(r.status, CheckStatus::Skipped);

    let i = make_instrument(&mut c, "SBIN", 1).await;
    for day in [d(2026, 8, 13), d(2026, 8, 14), prior_date(), trade_date()] {
        bar(&mut c, i, day, "100").await;
    }
    let r = check_bar_count_against_baseline(&mut c, trade_date(), thresholds())
        .await
        .unwrap();
    assert_eq!(r.status, CheckStatus::Passed);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn bar_count_fails_when_most_of_the_day_is_missing() {
    let mut c = scratch().await;
    for index in 0..10 {
        let i = make_instrument(&mut c, &format!("SYM{index}"), index + 1).await;
        for day in [d(2026, 8, 13), d(2026, 8, 14), prior_date()] {
            bar(&mut c, i, day, "100").await;
        }
    }
    let lone = make_instrument(&mut c, "LONE", 99).await;
    bar(&mut c, lone, trade_date(), "100").await;
    let r = check_bar_count_against_baseline(&mut c, trade_date(), thresholds())
        .await
        .unwrap();
    assert_eq!(r.status, CheckStatus::Failed);
    assert_eq!(r.observed.get("today"), Some(&OrderedJson::Int(1)));
}

// --- assertion 2 -------------------------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn a_calm_day_passes_and_an_unadjusted_split_is_caught() {
    let mut c = scratch().await;
    let calm = make_instrument(&mut c, "CALM", 1).await;
    bar(&mut c, calm, prior_date(), "100").await;
    bar(&mut c, calm, trade_date(), "102").await;
    let r = check_no_unexplained_jumps(&mut c, trade_date(), thresholds())
        .await
        .unwrap();
    assert_eq!(r.status, CheckStatus::Passed);
    assert_eq!(
        r.message,
        "0 of 1 instruments moved more than 50% with no corporate action or circuit to explain it"
    );

    let split = make_instrument(&mut c, "SBIN", 2).await;
    bar(&mut c, split, prior_date(), "1000").await;
    bar(&mut c, split, trade_date(), "100").await;
    let r = check_no_unexplained_jumps(&mut c, trade_date(), thresholds())
        .await
        .unwrap();
    assert_eq!(r.status, CheckStatus::Failed);
    let Some(OrderedJson::Array(offenders)) = r.observed.get("offenders") else {
        panic!("offenders is a list");
    };
    assert_eq!(offenders.len(), 1);
    assert_eq!(offenders[0].get("move"), Some(&OrderedJson::Float(-0.9)));
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn a_corporate_action_or_a_circuit_explains_the_jump() {
    let mut c = scratch().await;
    let i = make_instrument(&mut c, "SBIN", 1).await;
    bar(&mut c, i, prior_date(), "1000").await;
    bar(&mut c, i, trade_date(), "100").await;
    exec(
        &mut c,
        "INSERT INTO corporate_action (instrument_id, action_type, ex_date) \
         VALUES (1, 'split', DATE '2026-08-18')",
    )
    .await;
    let r = check_no_unexplained_jumps(&mut c, trade_date(), thresholds())
        .await
        .unwrap();
    assert_eq!(r.status, CheckStatus::Passed);

    let locked = make_instrument(&mut c, "LOCKED", 2).await;
    bar(&mut c, locked, prior_date(), "1000").await;
    add_bar(
        &mut c,
        locked,
        trade_date(),
        "100",
        (Some("120"), Some("100")),
    )
    .await
    .unwrap();
    let r = check_no_unexplained_jumps(&mut c, trade_date(), thresholds())
        .await
        .unwrap();
    assert_eq!(r.status, CheckStatus::Passed, "{}", r.message);
}

// --- assertion 3 -------------------------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn duplicates_pass_on_a_healthy_table_and_the_key_forbids_them() {
    let mut c = scratch().await;
    let i = make_instrument(&mut c, "SBIN", 1).await;
    bar(&mut c, i, trade_date(), "100").await;
    let r = check_no_duplicate_bars(&mut c).await.unwrap();
    assert_eq!(r.status, CheckStatus::Passed);
    let err = add_bar(&mut c, i, trade_date(), "101", (None, None))
        .await
        .unwrap_err();
    assert!(err.to_string().contains("duplicate key"), "{err}");
}

// --- assertion 4 -------------------------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn factor_rows_must_match_bars() {
    let mut c = scratch().await;
    for index in 0..3 {
        let i = make_instrument(&mut c, &format!("SYM{index}"), index + 1).await;
        bar(&mut c, i, trade_date(), "100").await;
    }
    let r = check_factor_rows_match_bars(&mut c, trade_date())
        .await
        .unwrap();
    assert_eq!(
        r.status,
        CheckStatus::Failed,
        "factors have not been computed"
    );
    factor_rows_for_bars(&mut c, trade_date()).await;
    let r = check_factor_rows_match_bars(&mut c, trade_date())
        .await
        .unwrap();
    assert_eq!(r.status, CheckStatus::Passed);
    exec(&mut c, "DELETE FROM ohlcv_daily WHERE instrument_id = 1").await;
    let r = check_factor_rows_match_bars(&mut c, trade_date())
        .await
        .unwrap();
    assert_eq!(r.status, CheckStatus::Failed, "a dropped row is detected");
}

// --- assertion 5 -------------------------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn universes_empty_fail_and_a_sized_nifty_50_passes_its_own_check() {
    let mut c = scratch().await;
    let r = check_universe_sizes(&mut c, trade_date(), thresholds())
        .await
        .unwrap();
    assert_eq!(r.status, CheckStatus::Failed);
    for index in 0..50 {
        let i = make_instrument(&mut c, &format!("N{index}"), index + 1).await;
        sqlx::query(
            "INSERT INTO index_member_daily (index_id, date, instrument_id) VALUES (1, $1, $2)",
        )
        .bind(trade_date())
        .bind(i)
        .execute(&mut c)
        .await
        .unwrap();
    }
    let r = check_universe_sizes(&mut c, trade_date(), thresholds())
        .await
        .unwrap();
    let counts = r.observed.get("counts").unwrap();
    assert_eq!(counts.get("nifty-50"), Some(&OrderedJson::Int(50)));
    assert!(!r.message.contains("nifty-50:"));
}

// --- assertion 6 -------------------------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn index_level_skips_then_skips_loudly_then_evaluates() {
    let mut c = scratch().await;
    let r = check_index_level_agreement(&mut c, trade_date())
        .await
        .unwrap();
    assert_eq!(r.status, CheckStatus::Skipped);
    assert!(r.message.contains("nothing to reconcile"));
    exec(
        &mut c,
        "INSERT INTO index_snapshot_daily (index_id, date, level) \
         VALUES (1, DATE '2026-08-18', 24500.35)",
    )
    .await;
    let r = check_index_level_agreement(&mut c, trade_date())
        .await
        .unwrap();
    assert_eq!(r.status, CheckStatus::Skipped);
    assert!(r.message.contains("weight is unpopulated"));
    assert_eq!(
        r.observed.get("published_level"),
        Some(&OrderedJson::Float(24500.35))
    );
    let i = make_instrument(&mut c, "SBIN", 1).await;
    sqlx::query(
        "INSERT INTO index_member_daily (index_id, date, instrument_id, weight) \
         VALUES (1, $1, $2, 0.05)",
    )
    .bind(trade_date())
    .bind(i)
    .execute(&mut c)
    .await
    .unwrap();
    let r = check_index_level_agreement(&mut c, trade_date())
        .await
        .unwrap();
    assert_eq!(r.status, CheckStatus::Passed);
}

// --- assertion 7 -------------------------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn a_complete_day_passes_and_the_schema_forbids_a_null_close() {
    let mut c = scratch().await;
    let i = make_instrument(&mut c, "SBIN", 1).await;
    bar(&mut c, i, trade_date(), "100").await;
    let r = check_no_null_prices(&mut c, trade_date()).await.unwrap();
    assert_eq!(r.status, CheckStatus::Passed);
    assert_eq!(r.observed.get("nulls"), Some(&OrderedJson::Int(0)));
    let err = sqlx::query("UPDATE ohlcv_daily SET close = NULL WHERE instrument_id = 1")
        .execute(&mut c)
        .await
        .unwrap_err();
    assert!(err.to_string().contains("not-null"), "{err}");
}

// --- assertion 8 -------------------------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn return_distribution_skips_while_ret_12m_is_unpopulated() {
    let mut c = scratch().await;
    let i = make_instrument(&mut c, "SBIN", 1).await;
    bar(&mut c, i, trade_date(), "100").await;
    factor_rows_for_bars(&mut c, trade_date()).await;
    let r = check_return_distribution(&mut c, trade_date(), thresholds())
        .await
        .unwrap();
    assert_eq!(r.status, CheckStatus::Skipped);
    assert!(r.message.contains("factor engine has not run"));
}

// --- catch-up: the finder ------------------------------------------------------------------------

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

async fn run(conn: &mut PgConnection, day: NaiveDate, status: &str, data_version: Option<i64>) {
    sqlx::query("INSERT INTO pipeline_run (trade_date, status, data_version) VALUES ($1, $2, $3)")
        .bind(day)
        .bind(status)
        .bind(data_version)
        .execute(&mut *conn)
        .await
        .unwrap();
}

async fn gate_verdict(conn: &mut PgConnection, day: NaiveDate, passed: bool) {
    sqlx::query(
        "INSERT INTO pipeline_run_step (run_id, step, status) SELECT id, 'data_quality_gate', $2 \
         FROM pipeline_run WHERE trade_date = $1 ORDER BY id DESC LIMIT 1",
    )
    .bind(day)
    .bind(if passed { "succeeded" } else { "failed" })
    .execute(&mut *conn)
    .await
    .unwrap();
}

fn ist(s: &str) -> Option<NaiveDateTime> {
    NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M").ok()
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn a_day_the_gate_refused_is_not_re_run_forever() {
    let mut c = scratch().await;
    run(&mut c, thu(), "failed", None).await;
    assert_eq!(
        unlanded_sessions(&mut c, thu(), 0, None).await.unwrap(),
        [thu()],
        "a killed run (no verdict) is still proposed"
    );
    gate_verdict(&mut c, thu(), false).await;
    assert!(
        unlanded_sessions(&mut c, thu(), 0, None)
            .await
            .unwrap()
            .is_empty(),
        "the sweep proposed a day whose data the gate has already judged wrong"
    );
    run(&mut c, thu(), "succeeded", Some(11)).await;
    gate_verdict(&mut c, thu(), true).await;
    assert!(
        unlanded_sessions(&mut c, thu(), 0, None)
            .await
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn what_counts_as_landed_is_a_data_version() {
    let mut c = scratch().await;
    run(&mut c, wed(), "succeeded", Some(8)).await;
    run(&mut c, thu(), "failed", None).await;
    run(&mut c, fri(), "succeeded", Some(9)).await;
    assert_eq!(
        unlanded_sessions(&mut c, fri(), 2, None).await.unwrap(),
        [thu()],
        "the killed run was treated as if the day had landed"
    );
    run(&mut c, thu(), "running", None).await;
    assert_eq!(
        unlanded_sessions(&mut c, thu(), 0, None).await.unwrap(),
        [thu()],
        "`running` is not `published`"
    );
    run(&mut c, thu(), "succeeded", Some(10)).await;
    assert!(
        unlanded_sessions(&mut c, fri(), 2, None)
            .await
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn a_day_with_no_run_is_proposed_but_never_a_holiday_or_an_uncharted_date() {
    let mut c = scratch().await;
    run(&mut c, wed(), "succeeded", Some(8)).await;
    assert!(
        unlanded_sessions(&mut c, wed(), 0, None)
            .await
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        unlanded_sessions(&mut c, thu(), 1, None).await.unwrap(),
        [thu()]
    );
    exec(
        &mut c,
        "UPDATE trading_day SET is_trading_day = false WHERE date = DATE '2026-09-04'",
    )
    .await;
    let missing = unlanded_sessions(&mut c, fri(), 1, None).await.unwrap();
    assert_eq!(missing, [thu()]);
    assert!(
        unlanded_sessions(&mut c, d(2027, 1, 5), 3, None)
            .await
            .unwrap()
            .is_empty(),
        "absent from the calendar means not proposed"
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn a_derived_day_is_demoted_after_one_missing_bhavcopy() {
    let mut c = scratch().await;
    exec(
        &mut c,
        "UPDATE trading_day SET is_trading_day = true, source = 'derived', holiday_name = NULL \
         WHERE date = DATE '2026-09-04'",
    )
    .await;
    assert!(
        !demote_derived_after_missing_bhavcopy(&mut c, fri(), true)
            .await
            .unwrap()
    );
    assert!(
        demote_derived_after_missing_bhavcopy(&mut c, fri(), false)
            .await
            .unwrap()
    );
    let (on, source, name): (bool, String, Option<String>) = sqlx::query_as(
        "SELECT is_trading_day, source, holiday_name FROM trading_day WHERE date = $1",
    )
    .bind(fri())
    .fetch_one(&mut c)
    .await
    .unwrap();
    assert!(!on);
    assert_eq!(source, "bhavcopy");
    assert_eq!(name.as_deref(), Some("inferred: no instrument traded"));
    assert!(
        unlanded_sessions(&mut c, fri(), 0, None)
            .await
            .unwrap()
            .is_empty()
    );
    // A seeded (not derived) day is never demoted.
    assert!(
        !demote_derived_after_missing_bhavcopy(&mut c, thu(), false)
            .await
            .unwrap()
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored"]
async fn today_waits_for_its_data_and_the_window_is_oldest_first_and_bounded() {
    let mut c = scratch().await;
    let morning = ist("2026-09-04 06:45");
    assert!(
        unlanded_sessions(&mut c, fri(), 0, morning)
            .await
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        unlanded_sessions(&mut c, fri(), 0, ist("2026-09-04 18:30"))
            .await
            .unwrap(),
        [fri()]
    );
    assert_eq!(
        unlanded_sessions(&mut c, fri(), 1, morning).await.unwrap(),
        [thu()],
        "the 06:45 sweep would have walked past last night's hole"
    );
    assert_eq!(
        unlanded_sessions(&mut c, fri(), 4, None).await.unwrap(),
        [mon(), tue(), wed(), thu(), fri()]
    );
    assert_eq!(
        unlanded_sessions(&mut c, fri(), 1, None).await.unwrap(),
        [thu(), fri()]
    );
    let err = unlanded_sessions(&mut c, fri(), -1, None)
        .await
        .unwrap_err();
    assert_eq!(err.python_type(), "ValueError");
    assert!(err.to_string().contains("lookback_days"));
}
