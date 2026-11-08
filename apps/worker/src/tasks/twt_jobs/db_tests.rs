//! The lane's Python database tests, ported (`test_twt_scan_task.py`, `test_twt_evening.py`,
//! `test_twt_sleeve_db.py` — the cases this lane's code answers; the fill-side countdown tests
//! belong to the desk's confirm path and are not this worker's).
//!
//! **Where they run.** On one connection to a *scratch* database, inside `TEMP` tables shaped
//! like the production ones (the columns these modules read and write, their numeric scales) —
//! never the bed's `baskfy`. Set `BASKFY_RS_SCRATCH_DATABASE_URL` (e.g. the bed container's
//! `postgres` database) and run `cargo test -p baskfy-worker -- --ignored twt_jobs`; without the
//! variable they are reported `ignored`. A URL naming the `baskfy` database is refused.
//! Task-level parity against Python on a real clone is `task_diff.py --cases
//! tools/parity/task_cases/twt_jobs.json`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_api::Settings;
use chrono::{DateTime, NaiveDate, Utc};
use rust_decimal::Decimal;
use sqlx::{Connection, PgConnection};

use super::evening::{
    EveningInputs, SOURCE_EVENING, SOURCE_MORNING, last_detected_session, run_twt_evening,
};
use super::scan::{last_published_session, unpublished_runs};
use super::sleeve::load_sleeve;
use crate::steps::{StepOutcome, StepStatus};

const IGNORE: &str =
    "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored";

const USER: i64 = 1;

fn d(day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 9, day).unwrap()
}

fn dec(s: &str) -> Decimal {
    s.parse().unwrap()
}

fn noon(day: u32) -> DateTime<Utc> {
    d(day).and_hms_opt(10, 0, 0).unwrap().and_utc()
}

async fn exec(conn: &mut PgConnection, sql: &str) {
    sqlx::raw_sql(sql).execute(&mut *conn).await.unwrap();
}

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
    exec(
        &mut conn,
        "CREATE TEMP TABLE pipeline_run (id bigserial PRIMARY KEY, trade_date date NOT NULL, \
         data_version integer); \
         CREATE TEMP TABLE trading_day (exchange_id smallint NOT NULL, date date NOT NULL, \
         is_trading_day boolean NOT NULL, PRIMARY KEY (exchange_id, date)); \
         CREATE TEMP TABLE instrument (id bigint PRIMARY KEY, symbol varchar NOT NULL); \
         CREATE TEMP TABLE ohlcv_daily (instrument_id bigint NOT NULL, date date NOT NULL, \
         close numeric(18,4) NOT NULL, adj_factor numeric(18,10) NOT NULL DEFAULT 1, \
         PRIMARY KEY (instrument_id, date)); \
         CREATE TEMP TABLE tw_scan_run (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, \
         user_id bigint NOT NULL, requested_at timestamptz NOT NULL DEFAULT now(), \
         started_at timestamptz, finished_at timestamptz, session_date date, \
         status varchar(16) NOT NULL DEFAULT 'QUEUED', source varchar(8) NOT NULL DEFAULT 'desk', \
         detail jsonb, error text, task_id varchar(64), provisional boolean NOT NULL DEFAULT false); \
         CREATE TEMP TABLE tw_config (user_id bigint PRIMARY KEY, \
         sleeve_capital_inr numeric(12,2) NOT NULL DEFAULT 0, \
         max_open_positions smallint NOT NULL DEFAULT 10, \
         max_position_pct numeric(5,2) NOT NULL DEFAULT 12.50, \
         stop_pct numeric(5,2) NOT NULL DEFAULT 20.00, trail_pct numeric(5,2) NOT NULL DEFAULT 20.00, \
         first_live_entries_left smallint NOT NULL DEFAULT 10, \
         dry_run_sessions smallint NOT NULL DEFAULT 0); \
         CREATE TEMP TABLE tw_breadth_daily (user_id bigint NOT NULL, date date NOT NULL, \
         gate varchar(8) NOT NULL, thin_session boolean NOT NULL DEFAULT false, \
         PRIMARY KEY (user_id, date)); \
         CREATE TEMP TABLE tw_state_daily (user_id bigint NOT NULL, date date NOT NULL, \
         instrument_id bigint NOT NULL, PRIMARY KEY (user_id, date, instrument_id)); \
         CREATE TEMP TABLE tw_signal_daily (user_id bigint NOT NULL, date date NOT NULL, \
         instrument_id bigint NOT NULL, state varchar(16) NOT NULL, rank_key bigint NOT NULL, \
         turnover_avg_20 bigint, entry_reference_close numeric(18,2), \
         PRIMARY KEY (user_id, date, instrument_id)); \
         CREATE TEMP TABLE tw_order (id bigserial PRIMARY KEY, user_id bigint NOT NULL, \
         instrument_id bigint NOT NULL, signal_date date NOT NULL, side varchar(8) NOT NULL, \
         state varchar(16) NOT NULL, created_at timestamptz NOT NULL DEFAULT now()); \
         CREATE TEMP TABLE tw_position (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, \
         user_id bigint NOT NULL, instrument_id bigint NOT NULL, entry_avg numeric(18,4) NOT NULL, \
         entry_adj_factor numeric(18,10) NOT NULL DEFAULT 1, stop_price numeric(18,2) NOT NULL, \
         high_since numeric(18,4) NOT NULL, gtt_id varchar(64), gtt_trigger numeric(18,2), \
         next_trigger numeric(18,2), next_trigger_for date, quantity_open integer NOT NULL, \
         state varchar(16) NOT NULL DEFAULT 'OPEN', closed_on date, pnl_inr numeric(12,2), \
         trail varchar(4), partial_queued_for date, partial_quantity integer, \
         exit_queued_for date, exit_reason_queued varchar(24)); \
         CREATE TEMP TABLE tw_plan (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, \
         plan_id uuid NOT NULL UNIQUE, user_id bigint NOT NULL, session_date date NOT NULL, \
         source varchar(16) NOT NULL, built_at timestamptz NOT NULL, expires_at timestamptz NOT NULL, \
         plan_hash varchar(64) NOT NULL, gate varchar(8) NOT NULL, \
         sleeve_equity_inr numeric(12,2) NOT NULL, total_new_exposure_inr numeric(12,2) NOT NULL); \
         CREATE TEMP TABLE tw_plan_line (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, \
         plan_id bigint NOT NULL REFERENCES tw_plan(id) ON DELETE CASCADE, user_id bigint NOT NULL, \
         instrument_id bigint NOT NULL, kind varchar(16) NOT NULL, state varchar(16) NOT NULL, \
         quantity integer NOT NULL, stop_price numeric(18,2), value_inr numeric(12,2) NOT NULL, \
         high_since numeric(18,4), previous_trigger numeric(18,2), note text, client_id varchar(128), \
         position_id bigint); \
         CREATE TEMP TABLE tw_plan_skip (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, \
         plan_id bigint NOT NULL REFERENCES tw_plan(id) ON DELETE CASCADE, user_id bigint NOT NULL, \
         instrument_id bigint NOT NULL, reason varchar(32) NOT NULL, detail text); \
         CREATE TEMP TABLE tw_session (user_id bigint NOT NULL, session_date date NOT NULL, \
         mode varchar(8) NOT NULL DEFAULT 'DRY_RUN', gate varchar(8) NOT NULL DEFAULT 'SHUT', \
         states integer NOT NULL DEFAULT 0, signals integer NOT NULL DEFAULT 0, \
         confirms integer NOT NULL DEFAULT 0, \
         first_live_entries_counted smallint NOT NULL DEFAULT 0, plan_ids jsonb, \
         updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (user_id, session_date));",
    )
    .await;
    conn
}

fn settings() -> Settings {
    // No mail sink: the alerts log only.
    Settings {
        ops_alert_email: String::new(),
        ops_alert_webhook_url: String::new(),
        ..Settings::default()
    }
}

async fn evening(
    conn: &mut PgConnection,
    day: NaiveDate,
    source: &str,
    now: DateTime<Utc>,
) -> (StepOutcome, Option<super::evening::EveningReport>) {
    let settings = settings();
    let mut outcome = StepOutcome::default();
    let inputs = EveningInputs {
        trade_date: day,
        user_id: USER,
        execution_enabled: false,
        source,
        now,
        settings: &settings,
    };
    let report = run_twt_evening(conn, &mut outcome, &inputs).await.unwrap();
    (outcome, report)
}

async fn scalar_i64(conn: &mut PgConnection, sql: &str) -> i64 {
    let (v,): (i64,) = sqlx::query_as(sql).fetch_one(&mut *conn).await.unwrap();
    v
}

/// A sleeve with ₹25 lakh, an open gate on 1 Sep, one SIGNAL and one SCAN_ONLY row.
async fn seeded() -> PgConnection {
    let mut conn = scratch().await;
    exec(
        &mut conn,
        "INSERT INTO trading_day VALUES (1, '2026-09-01', true), (1, '2026-09-02', true); \
         INSERT INTO instrument VALUES (1, 'ACME'), (2, 'THINCO'); \
         INSERT INTO tw_config (user_id, sleeve_capital_inr) VALUES (1, 2500000.00); \
         INSERT INTO tw_breadth_daily VALUES (1, '2026-09-01', 'OPEN', false); \
         INSERT INTO tw_state_daily VALUES (1, '2026-09-01', 1), (1, '2026-09-01', 2); \
         INSERT INTO tw_signal_daily VALUES \
           (1, '2026-09-01', 1, 'SIGNAL', 900000000, 500000000, 500.00), \
           (1, '2026-09-01', 2, 'SCAN_ONLY', 100, 1000000, 40.00);",
    )
    .await;
    conn
}

// --- test_twt_scan_task.py ---------------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn the_publisher_takes_queued_rows_without_a_task_oldest_first() {
    let mut conn = scratch().await;
    exec(
        &mut conn,
        "INSERT INTO tw_scan_run (user_id, requested_at, status) VALUES \
           (1, '2026-09-01 10:02+00', 'QUEUED'), (1, '2026-09-01 10:01+00', 'QUEUED'), \
           (1, '2026-09-01 10:00+00', 'RUNNING'), (1, '2026-09-01 09:00+00', 'DONE'), \
           (1, '2026-09-01 08:00+00', 'FAILED'); \
         INSERT INTO tw_scan_run (user_id, requested_at, status, task_id) VALUES \
           (1, '2026-09-01 07:00+00', 'QUEUED', 'already');",
    )
    .await;
    // Rows 2 then 1: oldest first; RUNNING/DONE/FAILED and the row with a task id are left alone.
    assert_eq!(unpublished_runs(&mut conn, 10).await.unwrap(), [2, 1]);
    assert_eq!(unpublished_runs(&mut conn, 1).await.unwrap(), [2]);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn the_published_session_is_the_newest_by_version_and_none_before_the_first() {
    let mut conn = scratch().await;
    assert_eq!(last_published_session(&mut conn).await.unwrap(), None);
    exec(
        &mut conn,
        "INSERT INTO pipeline_run (trade_date, data_version) VALUES \
           ('2026-09-01', 7), ('2026-08-31', 8), ('2026-09-02', NULL);",
    )
    .await;
    // A late re-publish of an older day is the latest; a day the gate refused is not published.
    assert_eq!(
        last_published_session(&mut conn).await.unwrap(),
        NaiveDate::from_ymd_opt(2026, 8, 31)
    );
}

// --- test_twt_evening.py -----------------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_session_the_detector_never_saw_plans_nothing_and_says_so() {
    let mut conn = seeded().await;
    let (outcome, report) = evening(&mut conn, d(2), SOURCE_EVENING, noon(2)).await;
    assert!(report.is_none());
    assert_eq!(outcome.status, StepStatus::Skipped);
    assert_eq!(
        scalar_i64(&mut conn, "SELECT count(*) FROM tw_plan").await,
        0
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_signal_becomes_a_buy_line_and_a_scan_only_row_a_liquidity_skip() {
    let mut conn = seeded().await;
    let (_, report) = evening(&mut conn, d(1), SOURCE_EVENING, noon(1)).await;
    let report = report.unwrap();
    assert_eq!((report.entries, report.skips), (1, 1));
    let (kind, state, client): (String, String, String) =
        sqlx::query_as("SELECT kind, state, client_id FROM tw_plan_line")
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!((kind.as_str(), state.as_str()), ("BUY_AT_OPEN", "PROPOSED"));
    // non-negotiable 6: `plan_id:symbol` for an order.
    assert_eq!(client, format!("{}:ACME", report.plan_id));
    let (reason,): (String,) = sqlx::query_as("SELECT reason FROM tw_plan_skip")
        .fetch_one(&mut conn)
        .await
        .unwrap();
    assert_eq!(reason, "BELOW_LIQUIDITY_FLOOR");
    // The plan expires thirty minutes after it is built.
    let minutes = scalar_i64(
        &mut conn,
        "SELECT (extract(epoch FROM expires_at - built_at) / 60)::bigint FROM tw_plan",
    )
    .await;
    assert_eq!(minutes, 30);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_shut_gate_lines_nothing_and_the_plan_says_why() {
    let mut conn = seeded().await;
    exec(&mut conn, "UPDATE tw_breadth_daily SET gate = 'SHUT'").await;
    let (_, report) = evening(&mut conn, d(1), SOURCE_EVENING, noon(1)).await;
    assert_eq!(report.unwrap().entries, 0);
    let (reason,): (String,) =
        sqlx::query_as("SELECT reason FROM tw_plan_skip WHERE instrument_id = 1")
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!(reason, "GATE_SHUT");
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_sleeve_with_no_capital_plans_nothing() {
    let mut conn = seeded().await;
    exec(&mut conn, "UPDATE tw_config SET sleeve_capital_inr = 0").await;
    let (_, report) = evening(&mut conn, d(1), SOURCE_EVENING, noon(1)).await;
    assert_eq!(report.unwrap().entries, 0);
    let (reason,): (String,) =
        sqlx::query_as("SELECT reason FROM tw_plan_skip WHERE instrument_id = 1")
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!(reason, "NO_SLEEVE_CAPITAL");
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn rebuilding_a_source_replaces_and_the_morning_is_a_second_row() {
    let mut conn = seeded().await;
    let (_, first) = evening(&mut conn, d(1), SOURCE_EVENING, noon(1)).await;
    let (_, again) = evening(&mut conn, d(1), SOURCE_EVENING, noon(1)).await;
    assert_ne!(first.unwrap().plan_id, again.unwrap().plan_id);
    assert_eq!(
        scalar_i64(&mut conn, "SELECT count(*) FROM tw_plan").await,
        1
    );
    assert_eq!(
        scalar_i64(&mut conn, "SELECT count(*) FROM tw_plan_line").await,
        1
    );
    let (_, morning) = evening(&mut conn, d(1), SOURCE_MORNING, noon(2)).await;
    let morning = morning.unwrap();
    assert_eq!(
        scalar_i64(&mut conn, "SELECT count(*) FROM tw_plan").await,
        2
    );
    // The session row carries both plan ids, the gate and the detector's counts.
    let (mode, gate, states, signals, ids): (String, String, i32, i32, String) =
        sqlx::query_as("SELECT mode, gate, states, signals, plan_ids->>'morning' FROM tw_session")
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!(
        (mode.as_str(), gate.as_str(), states, signals),
        ("DRY_RUN", "OPEN", 2, 1)
    );
    assert_eq!(ids, morning.plan_id);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn the_evening_never_moves_the_countdown_or_the_desks_counters() {
    let mut conn = seeded().await;
    exec(
        &mut conn,
        "UPDATE tw_config SET first_live_entries_left = 4, dry_run_sessions = 3; \
         INSERT INTO tw_session (user_id, session_date, confirms, first_live_entries_counted) \
         VALUES (1, '2026-09-01', 2, 1);",
    )
    .await;
    evening(&mut conn, d(1), SOURCE_MORNING, noon(2)).await;
    let (left, dry): (i16, i16) =
        sqlx::query_as("SELECT first_live_entries_left, dry_run_sessions FROM tw_config")
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!((left, dry), (4, 3));
    let (confirms, counted): (i32, i16) =
        sqlx::query_as("SELECT confirms, first_live_entries_counted FROM tw_session")
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!((confirms, counted), (2, 1));
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn the_morning_plans_from_the_last_tradeable_session() {
    let mut conn = seeded().await;
    exec(
        &mut conn,
        "INSERT INTO tw_breadth_daily VALUES (1, '2026-09-02', 'SHUT', true);",
    )
    .await;
    // A thin session was not traded (`04` §2.1): the morning reads the one before it.
    assert_eq!(
        last_detected_session(&mut conn, USER, d(3)).await.unwrap(),
        Some(d(1))
    );
    assert_eq!(
        last_detected_session(&mut conn, USER, d(1) - chrono::Days::new(1))
            .await
            .unwrap(),
        None
    );
}

// --- test_twt_sleeve_db.py ---------------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn equity_is_capital_plus_realised_plus_the_marked_open_and_a_stale_mark_falls_back() {
    let mut conn = seeded().await;
    let untouched = load_sleeve(&mut conn, USER, d(1)).await.unwrap();
    assert_eq!(untouched.quantized_equity().unwrap(), dec("2500000.00"));
    exec(
        &mut conn,
        "INSERT INTO ohlcv_daily (instrument_id, date, close) VALUES \
           (1, '2026-08-31', 110.0000), (1, '2026-09-01', 120.0000), (1, '2026-09-02', 999.0000); \
         INSERT INTO tw_position (user_id, instrument_id, entry_avg, stop_price, high_since, \
           quantity_open) VALUES (1, 1, 100.0000, 80.00, 120.0000, 10), \
           (1, 2, 50.0000, 40.00, 50.0000, 10); \
         INSERT INTO tw_position (user_id, instrument_id, entry_avg, stop_price, high_since, \
           quantity_open, state, closed_on, pnl_inr) VALUES \
           (1, 2, 50.0000, 40.00, 50.0000, 0, 'CLOSED', '2026-08-31', 1234.50), \
           (1, 2, 50.0000, 40.00, 50.0000, 0, 'CLOSED', '2026-09-02', 99999.00);",
    )
    .await;
    let sleeve = load_sleeve(&mut conn, USER, d(1)).await.unwrap();
    // 25,00,000 capital + 1,234.50 realised (the 2 Sep close is after the session: no
    // look-ahead) + the open book's gain at its marks: 10 x (120 - 100) on the session's close
    // (never the 2 Sep bar) and 10 x (50 - 50) for a name never printed since the fill (marked
    // at its entry). The capital already paid for the shares (`baskfy_core::twt::sleeve`).
    assert_eq!(sleeve.quantized_equity().unwrap(), dec("2501434.50"));
    assert_eq!(sleeve.stale_marks(), 1);
}

async fn sleeve() -> PgConnection {
    let mut conn = scratch().await;
    exec(
        &mut conn,
        "INSERT INTO trading_day VALUES (1, '2026-09-01', true), (1, '2026-09-02', true); \
         INSERT INTO tw_config (user_id, sleeve_capital_inr) VALUES (1, 2500000.00); \
         INSERT INTO tw_breadth_daily VALUES (1, '2026-09-01', 'OPEN', false);",
    )
    .await;
    conn
}

async fn name(conn: &mut PgConnection, id: i64, symbol: &str, close: &str) {
    exec(
        conn,
        &format!(
            "INSERT INTO instrument VALUES ({id}, '{symbol}'); \
             INSERT INTO ohlcv_daily (instrument_id, date, close) VALUES ({id}, '2026-09-01', {close});"
        ),
    )
    .await;
}

async fn signal(conn: &mut PgConnection, id: i64, symbol: &str, rank: i64) {
    name(conn, id, symbol, "100.0000").await;
    exec(
        conn,
        &format!(
            "INSERT INTO tw_state_daily VALUES (1, '2026-09-01', {id}); \
             INSERT INTO tw_signal_daily VALUES (1, '2026-09-01', {id}, 'SIGNAL', {rank}, \
             500000000, 100.00);"
        ),
    )
    .await;
}

async fn position(conn: &mut PgConnection, id: i64, gtt: &str, next: &str) {
    exec(
        conn,
        &format!(
            "INSERT INTO tw_position (user_id, instrument_id, entry_avg, stop_price, high_since, \
             gtt_id, gtt_trigger, next_trigger, next_trigger_for, quantity_open) \
             VALUES (1, {id}, 100.0000, 80.00, 100.0000, {gtt}, {next}, 100);"
        ),
    )
    .await;
}

async fn skip_reasons(conn: &mut PgConnection) -> Vec<String> {
    sqlx::query_scalar("SELECT reason FROM tw_plan_skip ORDER BY id")
        .fetch_all(&mut *conn)
        .await
        .unwrap()
}

async fn line_kinds(conn: &mut PgConnection) -> Vec<String> {
    sqlx::query_scalar("SELECT kind FROM tw_plan_line ORDER BY id")
        .fetch_all(&mut *conn)
        .await
        .unwrap()
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn at_most_three_entries_a_session_and_the_rest_are_skips() {
    let mut conn = sleeve().await;
    for index in 0..5 {
        signal(
            &mut conn,
            index + 1,
            &format!("NAME{index}"),
            1_000_000 - index,
        )
        .await;
    }
    let (_, report) = evening(&mut conn, d(1), SOURCE_EVENING, noon(1)).await;
    assert_eq!(report.unwrap().entries, 3);
    assert_eq!(
        skip_reasons(&mut conn).await,
        ["SESSION_CAP", "SESSION_CAP"]
    );
    let bought: Vec<i64> =
        sqlx::query_scalar("SELECT instrument_id FROM tw_plan_line ORDER BY instrument_id")
            .fetch_all(&mut conn)
            .await
            .unwrap();
    assert_eq!(bought, [1, 2, 3]);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_buy_line_is_a_tenth_of_the_sleeve_with_a_twenty_percent_stop() {
    let mut conn = sleeve().await;
    signal(&mut conn, 1, "TWTCO", 1).await;
    let (_, report) = evening(&mut conn, d(1), SOURCE_EVENING, noon(1)).await;
    let report = report.unwrap();
    let (quantity, value, stop, client): (i32, Decimal, Decimal, String) =
        sqlx::query_as("SELECT quantity, value_inr, stop_price, client_id FROM tw_plan_line")
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!(
        (quantity, value, stop),
        (2_500, dec("250000.00"), dec("80.00"))
    );
    assert_eq!(client, format!("{}:TWTCO", report.plan_id));
    let (equity, exposure, hash): (Decimal, Decimal, String) =
        sqlx::query_as("SELECT sleeve_equity_inr, total_new_exposure_inr, plan_hash FROM tw_plan")
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!((equity, exposure), (dec("2500000.00"), dec("250000.00")));
    assert_eq!(hash.len(), 64);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_name_at_the_per_name_cap_is_not_bought_again() {
    let mut conn = sleeve().await;
    signal(&mut conn, 1, "TWTCO", 1).await;
    position(&mut conn, 1, "'GTT-1', 80.00", "NULL, NULL").await;
    position(&mut conn, 1, "'GTT-2', 80.00", "NULL, NULL").await;
    let (_, report) = evening(&mut conn, d(1), SOURCE_EVENING, noon(1)).await;
    assert_eq!(report.unwrap().entries, 0);
    assert_eq!(skip_reasons(&mut conn).await, ["ALREADY_HELD"]);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn the_session_cap_counts_orders_sent_on_the_day_the_plan_executes() {
    let mut conn = sleeve().await;
    for (id, symbol, rank) in [(1, "AAA", 3), (2, "BBB", 2), (3, "CCC", 1)] {
        signal(&mut conn, id, symbol, rank).await;
    }
    exec(
        &mut conn,
        "INSERT INTO tw_order (user_id, instrument_id, signal_date, side, state, created_at) \
         VALUES (1, 1, '2026-09-01', 'BUY', 'FILLED', '2026-09-01 04:00:00+00');",
    )
    .await;
    let (_, tonight) = evening(&mut conn, d(1), SOURCE_EVENING, noon(1)).await;
    assert_eq!(tonight.unwrap().entries, 3);
    exec(
        &mut conn,
        "INSERT INTO tw_order (user_id, instrument_id, signal_date, side, state, created_at) \
         VALUES (1, 1, '2026-09-01', 'BUY', 'CONFIRMED', '2026-09-02 10:00:00+00');",
    )
    .await;
    let (_, morning) = evening(&mut conn, d(1), SOURCE_MORNING, noon(2)).await;
    assert_eq!(morning.unwrap().entries, 2);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_naked_position_is_an_arm_line_stored_before_the_entries() {
    let mut conn = sleeve().await;
    name(&mut conn, 7, "NAKEDCO", "100.0000").await;
    position(&mut conn, 7, "NULL, NULL", "NULL, NULL").await;
    signal(&mut conn, 1, "TWTCO", 1).await;
    let (_, report) = evening(&mut conn, d(1), SOURCE_EVENING, noon(1)).await;
    let report = report.unwrap();
    assert_eq!((report.arms, report.naked, report.entries), (1, 1, 1));
    assert_eq!(line_kinds(&mut conn).await, ["ARM_GTT", "BUY_AT_OPEN"]);
    let (quantity, stop, client): (i32, Decimal, String) = sqlx::query_as(
        "SELECT quantity, stop_price, client_id FROM tw_plan_line WHERE kind = 'ARM_GTT'",
    )
    .fetch_one(&mut conn)
    .await
    .unwrap();
    assert_eq!((quantity, stop), (100, dec("80.00")));
    assert_eq!(client, format!("{}:NAKEDCO:ARM_GTT", report.plan_id));
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn the_ratchet_carries_both_triggers_and_only_for_its_own_session() {
    let mut conn = sleeve().await;
    name(&mut conn, 8, "RATCHETCO", "130.0000").await;
    position(&mut conn, 8, "'GTT-1', 80.00", "104.00, '2026-09-01'").await;
    let (_, report) = evening(&mut conn, d(1), SOURCE_EVENING, noon(1)).await;
    let report = report.unwrap();
    assert_eq!(report.ratchets, 1);
    let (stop, previous, high, client): (Decimal, Decimal, Decimal, String) = sqlx::query_as(
        "SELECT stop_price, previous_trigger, high_since, client_id FROM tw_plan_line \
         WHERE kind = 'RAISE_GTT_STOP'",
    )
    .fetch_one(&mut conn)
    .await
    .unwrap();
    assert_eq!(
        (stop, previous, high),
        (dec("104.00"), dec("80.00"), dec("100.0000"))
    );
    assert_eq!(
        client,
        format!("{}:RATCHETCO:RAISE_GTT_STOP", report.plan_id)
    );
    exec(
        &mut conn,
        "UPDATE tw_position SET next_trigger_for = '2026-08-31'",
    )
    .await;
    let (_, stale) = evening(&mut conn, d(1), SOURCE_EVENING, noon(1)).await;
    assert_eq!(stale.unwrap().ratchets, 0);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_close_far_under_the_stop_is_never_a_sell_at_open() {
    let mut conn = sleeve().await;
    name(&mut conn, 9, "HOLDCO", "40.0000").await;
    position(&mut conn, 9, "'GTT-1', 80.00", "NULL, NULL").await;
    signal(&mut conn, 1, "TWTCO", 1).await;
    evening(&mut conn, d(1), SOURCE_EVENING, noon(1)).await;
    assert!(
        !line_kinds(&mut conn)
            .await
            .iter()
            .any(|kind| kind == "SELL_AT_OPEN")
    );
    let states: Vec<String> = sqlx::query_scalar("SELECT DISTINCT state FROM tw_plan_line")
        .fetch_all(&mut conn)
        .await
        .unwrap();
    assert_eq!(states, ["PROPOSED"]);
}
