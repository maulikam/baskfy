//! The lane's Python database tests, ported (`test_vbt_evening.py`, `test_vbt_sleeve_db.py`,
//! `test_vbt_ops.py`, `test_vbt_rescan.py` — the cases this lane's code answers; the detector's
//! own tests are wave 5's, and the qulla/live paths are graded end to end by `task_diff.py`).
//!
//! **Where they run.** On one connection to a *scratch* database, inside `TEMP` tables shaped
//! like the production ones (the columns these modules read and write, their numeric scales) —
//! never the bed's `baskfy`. Set `BASKFY_RS_SCRATCH_DATABASE_URL` (e.g. the bed container's
//! `postgres` database) and run `cargo test -p baskfy-worker -- --ignored vbt_jobs`; without the
//! variable they are reported `ignored`. A URL naming the `baskfy` database is refused.
//! Task-level parity against Python on a real clone is `task_diff.py --cases
//! tools/parity/task_cases/vbt_jobs.json`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_api::Settings;
use baskfy_db::OrderedJson;
use chrono::{DateTime, NaiveDate, Utc};
use rust_decimal::Decimal;
use sqlx::{Connection, PgConnection};

use super::evening::{
    EveningInputs, EveningReport, SOURCE_EVENING, SOURCE_MORNING, last_detected_session,
    run_vbt_evening,
};
use super::live::build_live_plan_vbt;
use super::ops::{
    CheckInputs, check_detect_fresh, check_naked_positions, check_orders_past_expiry,
    check_positions_without_bars,
};
use super::rescan::{
    REASON_PUBLISHED, claim_run, last_published_session, run_vbt_rescan, unpublished_runs,
};
use super::sleeve::load_sleeve;
use crate::settings::WorkerSettings;
use crate::steps::{StepOutcome, StepStatus};
use crate::tasks::TaskContext;
use crate::tasks::context::TaskClock;

const IGNORE: &str =
    "needs BASKFY_RS_SCRATCH_DATABASE_URL (a scratch database, never baskfy): run with --ignored";

const USER: i64 = 1;

fn d(day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 9, day).unwrap()
}

fn dec(s: &str) -> Decimal {
    s.parse().unwrap()
}

/// 21:15 IST on `day`.
fn evening_at(day: u32) -> DateTime<Utc> {
    d(day).and_hms_opt(15, 45, 0).unwrap().and_utc()
}

async fn exec(conn: &mut PgConnection, sql: &str) {
    sqlx::raw_sql(sql).execute(&mut *conn).await.unwrap();
}

async fn scalar_i64(conn: &mut PgConnection, sql: &str) -> i64 {
    let (v,): (i64,) = sqlx::query_as(sql).fetch_one(&mut *conn).await.unwrap();
    v
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
         CREATE TEMP TABLE instrument (id bigint PRIMARY KEY, symbol varchar NOT NULL, \
         name varchar, instrument_type varchar NOT NULL DEFAULT 'EQ', \
         series varchar DEFAULT 'EQ'); \
         CREATE TEMP TABLE index_def (id bigint PRIMARY KEY, slug varchar NOT NULL); \
         CREATE TEMP TABLE index_member_daily (index_id bigint NOT NULL, \
         instrument_id bigint NOT NULL, date date NOT NULL); \
         CREATE TEMP TABLE ohlcv_daily (instrument_id bigint NOT NULL, date date NOT NULL, \
         open numeric(18,4) NOT NULL, high numeric(18,4) NOT NULL, low numeric(18,4) NOT NULL, \
         close numeric(18,4) NOT NULL, close_raw numeric(18,4) NOT NULL, \
         volume bigint NOT NULL DEFAULT 0, upper_circuit numeric(18,4), \
         adj_factor numeric(18,10) NOT NULL DEFAULT 1, PRIMARY KEY (instrument_id, date)); \
         CREATE TEMP TABLE vb_config (user_id bigint PRIMARY KEY, \
         sleeve_capital_inr numeric(14,2) NOT NULL DEFAULT 0, \
         max_open_positions smallint NOT NULL DEFAULT 10, \
         max_position_pct numeric(5,2) NOT NULL DEFAULT 12.50, \
         stop_pct numeric(5,2) NOT NULL DEFAULT 12.00, \
         first_live_sessions_left smallint NOT NULL DEFAULT 5, \
         dry_run_sessions integer NOT NULL DEFAULT 0, \
         updated_at timestamptz NOT NULL DEFAULT now(), updated_by varchar(64)); \
         CREATE TEMP TABLE vb_config_audit (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, \
         user_id bigint NOT NULL, key varchar(64) NOT NULL, old_value text, new_value text, \
         changed_at timestamptz NOT NULL DEFAULT now(), changed_by varchar(64) NOT NULL, \
         note text); \
         CREATE TEMP TABLE vb_breadth_daily (user_id bigint NOT NULL, date date NOT NULL, \
         gate varchar(8) NOT NULL, thin_session boolean NOT NULL DEFAULT false, \
         PRIMARY KEY (user_id, date)); \
         CREATE TEMP TABLE vb_signal_daily (user_id bigint NOT NULL, date date NOT NULL, \
         instrument_id bigint NOT NULL, state varchar(16) NOT NULL, \
         limit_price numeric(18,2), turnover_avg_20 bigint, rank_key bigint NOT NULL DEFAULT 0, \
         locked_upper_circuit boolean NOT NULL DEFAULT false, \
         PRIMARY KEY (user_id, date, instrument_id)); \
         CREATE TEMP TABLE vb_order (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, \
         user_id bigint NOT NULL, instrument_id bigint NOT NULL, signal_date date NOT NULL, \
         limit_price numeric(18,2) NOT NULL, stop_price numeric(18,2) NOT NULL, \
         quantity integer NOT NULL, state varchar(16) NOT NULL, \
         expires_after_session date, sessions_worked smallint NOT NULL DEFAULT 0, \
         filled_quantity integer NOT NULL DEFAULT 0, cancelled_on date, \
         cancel_reason varchar(16), created_at timestamptz NOT NULL DEFAULT now(), \
         updated_at timestamptz NOT NULL DEFAULT now()); \
         CREATE TEMP TABLE vb_position (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, \
         user_id bigint NOT NULL, instrument_id bigint NOT NULL, entry_date date NOT NULL, \
         entry_avg numeric(18,4) NOT NULL, quantity_open integer NOT NULL, \
         initial_stop numeric(18,2) NOT NULL, stop_price numeric(18,2) NOT NULL, \
         gtt_id varchar(64), gtt_trigger numeric(18,2), \
         state varchar(16) NOT NULL DEFAULT 'OPEN', exit_queued_for date, \
         exit_reason_queued varchar(24), closed_on date, pnl_inr numeric(12,2), \
         partial_done boolean NOT NULL DEFAULT false, trail varchar(4), \
         updated_at timestamptz NOT NULL DEFAULT now()); \
         CREATE TEMP TABLE vb_plan (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, \
         plan_id uuid NOT NULL UNIQUE, user_id bigint NOT NULL, session_date date NOT NULL, \
         source varchar(16) NOT NULL, built_at timestamptz NOT NULL, \
         expires_at timestamptz NOT NULL, plan_hash varchar(64) NOT NULL, \
         gate varchar(8) NOT NULL, sleeve_equity_inr numeric(20,2) NOT NULL, \
         total_new_exposure_inr numeric(20,2) NOT NULL); \
         CREATE TEMP TABLE vb_plan_line (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, \
         plan_id bigint NOT NULL REFERENCES vb_plan(id) ON DELETE CASCADE, \
         user_id bigint NOT NULL, instrument_id bigint NOT NULL, kind varchar(16) NOT NULL, \
         state varchar(16) NOT NULL, quantity integer NOT NULL, limit_price numeric(18,2), \
         stop_price numeric(18,2), value_inr numeric(20,2) NOT NULL, size_cap varchar(16), \
         reason varchar(24), note text, client_id varchar(128), position_id bigint); \
         CREATE TEMP TABLE vb_plan_skip (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, \
         plan_id bigint NOT NULL REFERENCES vb_plan(id) ON DELETE CASCADE, \
         user_id bigint NOT NULL, instrument_id bigint NOT NULL, reason varchar(32) NOT NULL, \
         detail text); \
         CREATE TEMP TABLE vb_session (user_id bigint NOT NULL, session_date date NOT NULL, \
         mode varchar(8) NOT NULL DEFAULT 'DRY_RUN', gate varchar(8) NOT NULL DEFAULT 'SHUT', \
         signals integer NOT NULL DEFAULT 0, confirms integer NOT NULL DEFAULT 0, \
         first_live_counted boolean NOT NULL DEFAULT false, \
         counted_for_dry_run_gate boolean NOT NULL DEFAULT false, plan_ids jsonb, notes text, \
         updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (user_id, session_date)); \
         CREATE TEMP TABLE vb_scan_run (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, \
         user_id bigint NOT NULL, requested_at timestamptz NOT NULL DEFAULT now(), \
         status varchar(16) NOT NULL DEFAULT 'QUEUED', task_id varchar(64), \
         session_date date, provisional boolean NOT NULL DEFAULT false, detail jsonb, \
         error text, started_at timestamptz, finished_at timestamptz);",
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

/// ₹25 lakh, sessions 1–4 Sep on the calendar and in the sleeve's breadth (gate OPEN on the 3rd),
/// the 3rd's signals: three SIGNAL rows and a SCAN_ONLY one.
async fn seeded() -> PgConnection {
    let mut conn = scratch().await;
    exec(
        &mut conn,
        "INSERT INTO trading_day VALUES (1, '2026-09-01', true), (1, '2026-09-02', true), \
           (1, '2026-09-03', true), (1, '2026-09-04', true), (1, '2026-09-05', false), \
           (1, '2026-09-07', true), (1, '2026-09-08', true); \
         INSERT INTO instrument (id, symbol) VALUES (1, 'ACME'), (2, 'BOLT'), (3, 'CORE'), \
           (4, 'DUD'), (5, 'EDGE'); \
         INSERT INTO vb_config (user_id, sleeve_capital_inr, dry_run_sessions) \
           VALUES (1, 2500000.00, 3); \
         INSERT INTO vb_breadth_daily VALUES (1, '2026-09-01', 'SHUT', false), \
           (1, '2026-09-02', 'SHUT', false), (1, '2026-09-03', 'OPEN', false), \
           (1, '2026-09-04', 'SHUT', false); \
         INSERT INTO vb_signal_daily VALUES \
           (1, '2026-09-03', 1, 'SIGNAL', 500.00, 900000000, 900000000, false), \
           (1, '2026-09-03', 2, 'SIGNAL', 120.00, 800000000, 800000000, false), \
           (1, '2026-09-03', 3, 'SIGNAL', 250.00, 700000000, 700000000, false), \
           (1, '2026-09-03', 4, 'SCAN_ONLY', 40.00, 990000000, 990000000, false);",
    )
    .await;
    conn
}

async fn evening(
    conn: &mut PgConnection,
    day: NaiveDate,
    source: &str,
    execution_enabled: bool,
    now: DateTime<Utc>,
) -> (StepOutcome, Option<EveningReport>) {
    let mut outcome = StepOutcome::default();
    let inputs = EveningInputs {
        trade_date: day,
        user_id: USER,
        execution_enabled,
        source,
        now,
    };
    let report = run_vbt_evening(conn, &mut outcome, &inputs).await.unwrap();
    (outcome, report)
}

// --- test_vbt_evening.py -------------------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_signal_becomes_a_limit_at_its_own_close_and_scan_only_is_not_a_candidate() {
    let mut conn = seeded().await;
    let (_, report) = evening(&mut conn, d(3), SOURCE_EVENING, false, evening_at(3)).await;
    let report = report.unwrap();
    assert_eq!(report.entries, 3);
    assert_eq!(report.gate, "OPEN");
    let rows: Vec<(i64, String, Decimal, String)> = sqlx::query_as(
        "SELECT instrument_id, kind, limit_price, state FROM vb_plan_line ORDER BY instrument_id",
    )
    .fetch_all(&mut conn)
    .await
    .unwrap();
    assert_eq!(
        rows.iter().map(|r| r.0).collect::<Vec<_>>(),
        [1, 2, 3],
        "the SCAN_ONLY row is no candidate"
    );
    assert_eq!(rows[0].2, dec("500.00"));
    assert!(
        rows.iter()
            .all(|r| r.1 == "PLACE_LIMIT" && r.3 == "PROPOSED")
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn at_most_three_a_session_and_the_rest_are_skips() {
    let mut conn = seeded().await;
    exec(
        &mut conn,
        "INSERT INTO vb_signal_daily VALUES \
         (1, '2026-09-03', 5, 'SIGNAL', 90.00, 600000000, 600000000, false)",
    )
    .await;
    let (_, report) = evening(&mut conn, d(3), SOURCE_EVENING, false, evening_at(3)).await;
    let report = report.unwrap();
    assert_eq!((report.entries, report.skips), (3, 1));
    let (reason,): (String,) = sqlx::query_as("SELECT reason FROM vb_plan_skip")
        .fetch_one(&mut conn)
        .await
        .unwrap();
    assert_eq!(reason, "SESSION_CAP");
    // The highest turnover is taken first: EDGE (rank 600m) is the one left out.
    let (skipped,): (i64,) = sqlx::query_as("SELECT instrument_id FROM vb_plan_skip")
        .fetch_one(&mut conn)
        .await
        .unwrap();
    assert_eq!(skipped, 5);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_shut_gate_lines_nothing_and_says_why() {
    let mut conn = seeded().await;
    exec(
        &mut conn,
        "UPDATE vb_breadth_daily SET gate = 'SHUT' WHERE date = '2026-09-03'",
    )
    .await;
    let (_, report) = evening(&mut conn, d(3), SOURCE_EVENING, false, evening_at(3)).await;
    let report = report.unwrap();
    assert_eq!((report.entries, report.skips), (0, 3));
    assert_eq!(
        scalar_i64(
            &mut conn,
            "SELECT count(*) FROM vb_plan_skip WHERE reason = 'GATE_SHUT'"
        )
        .await,
        3
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_sleeve_with_no_capital_lines_nothing() {
    let mut conn = seeded().await;
    exec(&mut conn, "UPDATE vb_config SET sleeve_capital_inr = 0").await;
    let (_, report) = evening(&mut conn, d(3), SOURCE_EVENING, false, evening_at(3)).await;
    assert_eq!(report.unwrap().entries, 0);
    assert_eq!(
        scalar_i64(
            &mut conn,
            "SELECT count(*) FROM vb_plan_skip WHERE reason = 'NO_SLEEVE_CAPITAL'"
        )
        .await,
        3
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_locked_bar_is_skipped_with_its_own_reason() {
    let mut conn = seeded().await;
    exec(
        &mut conn,
        "UPDATE vb_signal_daily SET locked_upper_circuit = true WHERE instrument_id = 1",
    )
    .await;
    evening(&mut conn, d(3), SOURCE_EVENING, false, evening_at(3)).await;
    assert_eq!(
        scalar_i64(
            &mut conn,
            "SELECT instrument_id FROM vb_plan_skip WHERE reason = 'LOCKED_UPPER_CIRCUIT'"
        )
        .await,
        1
    );
}

async fn seed_orders(conn: &mut PgConnection) {
    exec(
        conn,
        "INSERT INTO vb_order (user_id, instrument_id, signal_date, limit_price, stop_price, \
         quantity, state) VALUES \
         (1, 1, '2026-09-01', 500.00, 440.00, 10, 'SENT'), \
         (1, 2, '2026-09-01', 120.00, 105.60, 10, 'PROPOSED'), \
         (1, 3, '2026-09-03', 250.00, 220.00, 10, 'CONFIRMED')",
    )
    .await;
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn an_order_at_the_end_of_its_third_session_is_a_cancel_line_and_never_cancelled_here() {
    let mut conn = seeded().await;
    seed_orders(&mut conn).await;
    let (outcome, report) = evening(&mut conn, d(4), SOURCE_EVENING, false, evening_at(4)).await;
    let report = report.unwrap();
    // Sessions 2, 3, 4 have passed for the 1 Sep orders: the SENT one becomes a CANCEL_LIMIT
    // line and stays SENT (a broker holds it); the PROPOSED one is EXPIRED here.
    assert_eq!(report.cancels, 1);
    let states: Vec<(i64, String, i64, Option<NaiveDate>)> = sqlx::query_as(
        "SELECT instrument_id, state, sessions_worked::bigint, expires_after_session \
         FROM vb_order ORDER BY instrument_id",
    )
    .fetch_all(&mut conn)
    .await
    .unwrap();
    assert_eq!(states[0], (1, "SENT".to_owned(), 3, Some(d(4))));
    assert_eq!(states[1], (2, "EXPIRED".to_owned(), 3, Some(d(4))));
    // The 3 Sep order is inside its window: counted, not touched; its expiry is past the calendar.
    assert_eq!(states[2], (3, "CONFIRMED".to_owned(), 1, None));
    assert_eq!(
        outcome
            .detail
            .iter()
            .find(|(k, _)| k == "orders_refreshed")
            .map(|(_, v)| v.clone()),
        Some(OrderedJson::Int(3))
    );
    // A re-run cannot inflate the derived count (house rule 7).
    let (outcome, _) = evening(&mut conn, d(4), SOURCE_EVENING, false, evening_at(4)).await;
    assert_eq!(
        scalar_i64(
            &mut conn,
            "SELECT sessions_worked::bigint FROM vb_order WHERE instrument_id = 1"
        )
        .await,
        3
    );
    assert!(
        outcome
            .detail
            .iter()
            .any(|(k, v)| k == "orders_refreshed" && *v == OrderedJson::Int(0))
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_naked_position_gets_an_arm_gtt_line_and_a_working_order_holds_a_slot() {
    let mut conn = seeded().await;
    exec(
        &mut conn,
        "INSERT INTO vb_position (user_id, instrument_id, entry_date, entry_avg, quantity_open, \
         initial_stop, stop_price) VALUES (1, 5, '2026-09-02', 90.0000, 100, 79.20, 79.20); \
         INSERT INTO vb_order (user_id, instrument_id, signal_date, limit_price, stop_price, \
         quantity, state) VALUES (1, 2, '2026-09-03', 120.00, 105.60, 10, 'SENT')",
    )
    .await;
    let (_, report) = evening(&mut conn, d(3), SOURCE_EVENING, false, evening_at(3)).await;
    let report = report.unwrap();
    assert_eq!(report.arms, 1);
    let (kind, stop): (String, Decimal) =
        sqlx::query_as("SELECT kind, stop_price FROM vb_plan_line WHERE instrument_id = 5")
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!((kind.as_str(), stop), ("ARM_GTT", dec("79.20")));
    assert_eq!(
        scalar_i64(
            &mut conn,
            "SELECT instrument_id FROM vb_plan_skip WHERE reason = 'ALREADY_WORKING'"
        )
        .await,
        2
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn the_plan_expires_in_thirty_minutes_and_a_re_run_replaces_it() {
    let mut conn = seeded().await;
    let now = evening_at(3);
    evening(&mut conn, d(3), SOURCE_EVENING, false, now).await;
    let (built, expires): (DateTime<Utc>, DateTime<Utc>) =
        sqlx::query_as("SELECT built_at, expires_at FROM vb_plan")
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!(built, now);
    assert_eq!(expires - built, chrono::Duration::minutes(30));
    evening(&mut conn, d(3), SOURCE_EVENING, false, now).await;
    assert_eq!(
        scalar_i64(&mut conn, "SELECT count(*) FROM vb_plan").await,
        1
    );
    assert_eq!(
        scalar_i64(&mut conn, "SELECT count(*) FROM vb_plan_line").await,
        3
    );
    // The morning plan is a second row, not a replacement.
    evening(&mut conn, d(3), SOURCE_MORNING, false, now).await;
    assert_eq!(
        scalar_i64(&mut conn, "SELECT count(*) FROM vb_plan").await,
        2
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn the_dry_run_counter_moves_once_and_only_when_rehearsed() {
    let mut conn = seeded().await;
    // 3 Sep has executable lines and no confirm: not rehearsed, not counted.
    let (_, report) = evening(&mut conn, d(3), SOURCE_EVENING, false, evening_at(3)).await;
    assert!(!report.unwrap().counted_dry_run);
    let (mode, gate, signals): (String, String, i32) = sqlx::query_as(
        "SELECT mode, gate, signals FROM vb_session WHERE session_date = '2026-09-03'",
    )
    .fetch_one(&mut conn)
    .await
    .unwrap();
    assert_eq!(
        (mode.as_str(), gate.as_str(), signals),
        ("DRY_RUN", "OPEN", 3)
    );
    assert_eq!(
        scalar_i64(&mut conn, "SELECT dry_run_sessions::bigint FROM vb_config").await,
        3
    );
    // A confirm on the session rehearses it: counted once, audited, and never again.
    exec(
        &mut conn,
        "UPDATE vb_session SET confirms = 1 WHERE session_date = '2026-09-03'",
    )
    .await;
    let (_, report) = evening(&mut conn, d(3), SOURCE_EVENING, false, evening_at(3)).await;
    assert!(report.unwrap().counted_dry_run);
    let (_, report) = evening(&mut conn, d(3), SOURCE_EVENING, false, evening_at(3)).await;
    assert!(!report.unwrap().counted_dry_run);
    assert_eq!(
        scalar_i64(&mut conn, "SELECT dry_run_sessions::bigint FROM vb_config").await,
        4
    );
    let (key, old, new, by, note): (String, String, String, String, String) =
        sqlx::query_as("SELECT key, old_value, new_value, changed_by, note FROM vb_config_audit")
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!(
        (
            key.as_str(),
            old.as_str(),
            new.as_str(),
            by.as_str(),
            note.as_str()
        ),
        (
            "dry_run_sessions",
            "3",
            "4",
            "vbt-evening",
            "2026-09-03 closed in DRY_RUN"
        )
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_session_with_nothing_to_confirm_counts_and_a_live_one_moves_the_countdown() {
    let mut conn = seeded().await;
    // 4 Sep: gate SHUT, no candidates, no book — nothing executable, so it is rehearsed.
    let (_, report) = evening(&mut conn, d(4), SOURCE_EVENING, false, evening_at(4)).await;
    assert!(report.unwrap().counted_dry_run);
    // A LIVE session moves first_live_sessions_left once.
    let (_, report) = evening(&mut conn, d(2), SOURCE_EVENING, true, evening_at(2)).await;
    let report = report.unwrap();
    assert!(report.counted_first_live && !report.counted_dry_run);
    let (_, report) = evening(&mut conn, d(2), SOURCE_EVENING, true, evening_at(2)).await;
    assert!(!report.unwrap().counted_first_live);
    assert_eq!(
        scalar_i64(
            &mut conn,
            "SELECT first_live_sessions_left::bigint FROM vb_config"
        )
        .await,
        4
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_thin_session_is_not_one_the_sleeve_traded() {
    let mut conn = seeded().await;
    exec(
        &mut conn,
        "UPDATE vb_breadth_daily SET thin_session = true WHERE date = '2026-09-03'",
    )
    .await;
    let (outcome, report) = evening(&mut conn, d(3), SOURCE_EVENING, false, evening_at(3)).await;
    assert!(report.is_none());
    assert_eq!(outcome.status, StepStatus::Skipped);
    assert_eq!(
        scalar_i64(&mut conn, "SELECT count(*) FROM vb_plan").await,
        0
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn the_morning_reads_the_last_session_the_sleeve_saw() {
    let mut conn = seeded().await;
    exec(
        &mut conn,
        "UPDATE vb_breadth_daily SET thin_session = true WHERE date = '2026-09-04'",
    )
    .await;
    assert_eq!(
        last_detected_session(&mut conn, USER, d(7)).await.unwrap(),
        Some(d(3))
    );
    assert_eq!(
        last_detected_session(&mut conn, USER, d(2)).await.unwrap(),
        Some(d(2))
    );
    exec(&mut conn, "DELETE FROM vb_breadth_daily").await;
    assert_eq!(
        last_detected_session(&mut conn, USER, d(7)).await.unwrap(),
        None
    );
}

// --- test_vbt_sleeve_db.py -----------------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn the_sleeve_is_its_capital_its_profit_and_its_own_book() {
    let mut conn = seeded().await;
    let sleeve = load_sleeve(&mut conn, USER, d(4)).await.unwrap();
    assert_eq!(sleeve.equity_inr().unwrap(), dec("2500000.00"));
    exec(
        &mut conn,
        "INSERT INTO ohlcv_daily (instrument_id, date, open, high, low, close, close_raw) VALUES \
           (1, '2026-09-02', 100, 100, 100, 110.0000, 110.0000), \
           (1, '2026-09-04', 100, 100, 100, 120.0000, 120.0000), \
           (1, '2026-09-07', 100, 100, 100, 999.0000, 999.0000); \
         INSERT INTO vb_position (user_id, instrument_id, entry_date, entry_avg, quantity_open, \
           initial_stop, stop_price) VALUES (1, 1, '2026-09-01', 100.0000, 10, 88, 88), \
           (1, 2, '2026-09-01', 50.0000, 4, 44, 44); \
         INSERT INTO vb_position (user_id, instrument_id, entry_date, entry_avg, quantity_open, \
           initial_stop, stop_price, state, closed_on, pnl_inr) VALUES \
           (1, 3, '2026-09-01', 10, 0, 8, 8, 'CLOSED', '2026-09-03', 1500.50), \
           (1, 3, '2026-09-01', 10, 0, 8, 8, 'CLOSED', '2026-09-07', 99.00); \
         INSERT INTO vb_order (user_id, instrument_id, signal_date, limit_price, stop_price, \
           quantity, filled_quantity, state) VALUES \
           (1, 4, '2026-09-03', 40.00, 35.00, 10, 4, 'PARTIAL'), \
           (1, 5, '2026-09-03', 90.00, 80.00, 10, 0, 'CANCELLED');",
    )
    .await;
    let sleeve = load_sleeve(&mut conn, USER, d(4)).await.unwrap();
    // Marked at the latest close on or before 4 Sep (120, never the 7th's 999); BOLT has not
    // printed and is marked at its entry; realised profit up to 4 Sep is cash; the partly
    // filled order commits only its remainder; the cancelled one nothing.
    assert_eq!(sleeve.value_of_open_inr, dec("1400.0000"));
    assert_eq!(sleeve.cost_of_open_inr, dec("1200.0000"));
    assert_eq!(sleeve.realised_inr, dec("1500.50"));
    assert_eq!(sleeve.committed_inr, dec("240.00"));
    exec(&mut conn, "DELETE FROM vb_config").await;
    let sleeve = load_sleeve(&mut conn, USER, d(4)).await.unwrap();
    assert_eq!(sleeve.capital_inr, Decimal::ZERO);
}

// --- test_vbt_rescan.py --------------------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn the_sweep_takes_queued_rows_without_a_task_oldest_first() {
    let mut conn = scratch().await;
    exec(
        &mut conn,
        "INSERT INTO vb_scan_run (user_id, requested_at, status) VALUES \
           (1, '2026-09-01 10:02+00', 'QUEUED'), (1, '2026-09-01 10:01+00', 'QUEUED'), \
           (1, '2026-09-01 10:00+00', 'RUNNING'), (1, '2026-09-01 09:00+00', 'DONE'), \
           (1, '2026-09-01 08:00+00', 'FAILED'); \
         INSERT INTO vb_scan_run (user_id, requested_at, status, task_id) VALUES \
           (1, '2026-09-01 07:00+00', 'QUEUED', 'already');",
    )
    .await;
    assert_eq!(unpublished_runs(&mut conn, 10).await.unwrap(), [2, 1]);
    assert_eq!(unpublished_runs(&mut conn, 1).await.unwrap(), [2]);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn the_published_session_is_the_newest_run_with_a_data_version() {
    let mut conn = scratch().await;
    assert_eq!(last_published_session(&mut conn).await.unwrap(), None);
    exec(
        &mut conn,
        "INSERT INTO pipeline_run (trade_date, data_version) VALUES \
           ('2026-09-03', 7), ('2026-09-04', 8), ('2026-09-07', NULL)",
    )
    .await;
    assert_eq!(last_published_session(&mut conn).await.unwrap(), Some(d(4)));
}

// --- test_vbt_ops.py -----------------------------------------------------------------------------

fn at_ist(day: u32, h: u32, m: u32) -> DateTime<Utc> {
    d(day).and_hms_opt(h, m, 0).unwrap().and_utc() - chrono::Duration::minutes(330)
}

fn field<'a>(json: &'a OrderedJson, key: &str) -> &'a OrderedJson {
    match json {
        OrderedJson::Object(entries) => entries
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v)
            .unwrap_or_else(|| panic!("no {key}")),
        _ => panic!("not an object"),
    }
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_limit_past_its_third_session_raises_and_nothing_else_is_late() {
    let mut conn = scratch().await;
    exec(
        &mut conn,
        "INSERT INTO vb_order (user_id, instrument_id, signal_date, limit_price, stop_price, \
         quantity, state, expires_after_session) VALUES \
         (1, 1, '2026-09-01', 10, 9, 1, 'SENT', '2026-09-03'), \
         (1, 2, '2026-09-01', 10, 9, 1, 'PARTIAL', '2026-09-04'), \
         (1, 3, '2026-09-01', 10, 9, 1, 'FILLED', '2026-09-01'), \
         (1, 4, '2026-09-01', 10, 9, 1, 'CONFIRMED', NULL), \
         (2, 5, '2026-09-01', 10, 9, 1, 'SENT', '2026-09-01');",
    )
    .await;
    let settings = settings();
    let inputs = CheckInputs {
        now: at_ist(4, 21, 40),
        user_id: USER,
        settings: &settings,
    };
    let out = check_orders_past_expiry(&mut conn, &inputs).await.unwrap();
    // The SENT limit expired on the 3rd; the PARTIAL one's window ends today and is not late; a
    // terminal state, an order with no window and another user's order are never this sleeve's.
    assert_eq!(field(&out, "past_expiry"), &OrderedJson::Int(1));
    assert_eq!(
        field(field(&out, "alert"), "alert"),
        &OrderedJson::Str("VBT_ORDER_PAST_EXPIRY".to_owned())
    );
    // On a Saturday it stays quiet, and the check writes nothing either way.
    let inputs = CheckInputs {
        now: at_ist(5, 21, 40),
        user_id: USER,
        settings: &settings,
    };
    let out = check_orders_past_expiry(&mut conn, &inputs).await.unwrap();
    assert_eq!(field(&out, "checked"), &OrderedJson::Bool(false));
    assert_eq!(
        scalar_i64(
            &mut conn,
            "SELECT count(*) FROM vb_order WHERE state = 'SENT'"
        )
        .await,
        2
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn naked_and_silent_positions_raise_and_a_covered_printing_book_is_silent() {
    let mut conn = scratch().await;
    let settings = settings();
    let inputs = CheckInputs {
        now: at_ist(8, 21, 40),
        user_id: USER,
        settings: &settings,
    };
    // An empty book asks no questions.
    let out = check_positions_without_bars(&mut conn, &inputs)
        .await
        .unwrap();
    assert_eq!(field(&out, "silent"), &OrderedJson::Int(0));
    exec(
        &mut conn,
        "INSERT INTO ohlcv_daily (instrument_id, date, open, high, low, close, close_raw) VALUES \
           (1, '2026-09-07', 1, 1, 1, 1, 1), (2, '2026-08-31', 1, 1, 1, 1, 1); \
         INSERT INTO vb_position (user_id, instrument_id, entry_date, entry_avg, quantity_open, \
           initial_stop, stop_price, gtt_id) VALUES \
           (1, 1, '2026-09-01', 1, 1, 1, 1, 'g'), (1, 2, '2026-09-01', 1, 1, 1, 1, NULL), \
           (1, 3, '2026-09-01', 1, 1, 1, 1, 'g');",
    )
    .await;
    let out = check_naked_positions(&mut conn, &inputs).await.unwrap();
    assert_eq!(field(&out, "naked"), &OrderedJson::Int(1));
    // Instrument 2 last printed eight days ago, instrument 3 never; instrument 1 is printing.
    let out = check_positions_without_bars(&mut conn, &inputs)
        .await
        .unwrap();
    assert_eq!(field(&out, "silent"), &OrderedJson::Int(2));
    exec(
        &mut conn,
        "UPDATE vb_position SET gtt_id = 'g'; DELETE FROM vb_position WHERE instrument_id <> 1",
    )
    .await;
    let out = check_naked_positions(&mut conn, &inputs).await.unwrap();
    assert_eq!(field(&out, "alert"), &OrderedJson::Null);
    let out = check_positions_without_bars(&mut conn, &inputs)
        .await
        .unwrap();
    assert_eq!(field(&out, "alert"), &OrderedJson::Null);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_detector_that_has_written_nothing_raises_and_a_long_weekend_is_not_staleness() {
    let mut conn = scratch().await;
    let settings = settings();
    let at = |day: u32| CheckInputs {
        now: at_ist(day, 21, 30),
        user_id: USER,
        settings: &settings,
    };
    let out = check_detect_fresh(&mut conn, &at(4)).await.unwrap();
    assert_eq!(
        field(field(&out, "alert"), "detail"),
        &crate::pyjson::object([("latest", OrderedJson::Null)])
    );
    exec(
        &mut conn,
        "INSERT INTO vb_breadth_daily VALUES (1, '2026-09-04', 'SHUT', false)",
    )
    .await;
    // Friday's row on Tuesday is four days old: a long weekend, fresh.
    let out = check_detect_fresh(&mut conn, &at(8)).await.unwrap();
    assert_eq!(field(&out, "alert"), &OrderedJson::Null);
    // A week of silence is not.
    let out = check_detect_fresh(&mut conn, &at(10)).await.unwrap();
    assert_eq!(
        field(field(&out, "alert"), "alert"),
        &OrderedJson::Str("VBT_DETECT_STALE".to_owned())
    );
}

fn ctx_at(day: u32, h: u32, m: u32) -> TaskContext {
    let base = Settings::default();
    let db = baskfy_db::WriteDb::connect_lazy(&base.db_settings()).unwrap();
    TaskContext::new(
        db,
        settings(),
        WorkerSettings::default(),
        TaskClock::starting_at_ist(d(day).and_hms_opt(h, m, 0).unwrap()),
        None,
        None,
    )
}

fn text(json: &OrderedJson) -> &str {
    match json {
        OrderedJson::Str(s) => s.as_str(),
        other => panic!("not a string: {other:?}"),
    }
}

async fn queued(conn: &mut PgConnection, user_id: i64) -> i64 {
    scalar_i64(
        conn,
        &format!(
            "INSERT INTO vb_scan_run (user_id, requested_at, status) \
             VALUES ({user_id}, '2026-09-07 02:59+00', 'QUEUED') RETURNING id"
        ),
    )
    .await
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_queued_row_is_claimed_once_and_an_unknown_one_never() {
    let mut conn = scratch().await;
    let id = queued(&mut conn, 2).await;
    let claimed = claim_run(&mut conn, id, at_ist(7, 8, 30)).await.unwrap();
    assert_eq!(claimed.map(|c| c.user_id), Some(2), "the row's own user");
    let (status, started): (String, Option<DateTime<Utc>>) =
        sqlx::query_as("SELECT status, started_at FROM vb_scan_run WHERE id = $1")
            .bind(id)
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!(
        (status.as_str(), started),
        ("RUNNING", Some(at_ist(7, 8, 30)))
    );
    assert!(
        claim_run(&mut conn, id, at_ist(7, 8, 31))
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        claim_run(&mut conn, 999_999, at_ist(7, 8, 31))
            .await
            .unwrap()
            .is_none()
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_run_with_nothing_published_fails_on_the_row_rather_than_raising() {
    let mut conn = scratch().await;
    let id = queued(&mut conn, USER).await;
    let out = run_vbt_rescan(&ctx_at(7, 8, 30), &mut conn, id, false)
        .await
        .unwrap();
    assert_eq!(
        field(&out, "status"),
        &OrderedJson::Str("FAILED".to_owned())
    );
    assert!(
        text(field(&out, "error")).starts_with(
            "ValueError: the pipeline has published no session on or before 2026-09-07"
        )
    );
    let (status, error, finished): (String, Option<String>, Option<DateTime<Utc>>) =
        sqlx::query_as("SELECT status, error, finished_at FROM vb_scan_run WHERE id = $1")
            .bind(id)
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!(status, "FAILED");
    assert_eq!(error.as_deref(), Some(text(field(&out, "error"))));
    assert!(finished.is_some());
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_quiet_session_finishes_done_and_a_finished_row_is_skipped() {
    let mut conn = seeded().await;
    exec(
        &mut conn,
        "INSERT INTO pipeline_run (trade_date, data_version) VALUES ('2026-09-04', 9)",
    )
    .await;
    let id = queued(&mut conn, USER).await;
    let out = run_vbt_rescan(&ctx_at(7, 8, 30), &mut conn, id, false)
        .await
        .unwrap();
    assert_eq!(field(&out, "status"), &OrderedJson::Str("DONE".to_owned()));
    assert_eq!(
        field(&out, "session_date"),
        &OrderedJson::Str("2026-09-04".to_owned())
    );
    assert_eq!(field(&out, "provisional"), &OrderedJson::Bool(false));
    let detail = field(&out, "detail");
    assert_eq!(field(detail, "signals"), &OrderedJson::Int(0));
    assert_eq!(
        field(detail, "reason"),
        &OrderedJson::Str(REASON_PUBLISHED.to_owned())
    );
    assert_eq!(field(&out, "error"), &OrderedJson::Null);
    assert_eq!(
        scalar_i64(
            &mut conn,
            &format!(
                "SELECT count(*) FROM vb_scan_run WHERE id = {id} AND status = 'DONE' \
                      AND session_date = '2026-09-04' AND detail IS NOT NULL"
            )
        )
        .await,
        1
    );
    // Before the open the published session is detected plain: no plan, no order.
    assert_eq!(
        scalar_i64(&mut conn, "SELECT count(*) FROM vb_plan").await,
        0
    );
    let again = run_vbt_rescan(&ctx_at(7, 8, 31), &mut conn, id, false)
        .await
        .unwrap();
    assert_eq!(
        field(&again, "skipped"),
        &OrderedJson::Str("not QUEUED — already claimed or finished".to_owned())
    );
}

// --- test_live_scan.py (VBT's live plan) ---------------------------------------------------------

async fn live_plan(conn: &mut PgConnection) -> super::live::PlanBuild {
    build_live_plan_vbt(conn, USER, d(3), false, at_ist(3, 13, 15))
        .await
        .unwrap()
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_vbt_signal_becomes_a_live_plan_of_buy_at_market_lines() {
    let mut conn = seeded().await;
    let built = live_plan(&mut conn).await;
    assert_eq!(built.entries, 3);
    assert!(!built.kept);
    let (plan_id, source, built_at, expires): (String, String, DateTime<Utc>, DateTime<Utc>) =
        sqlx::query_as("SELECT plan_id::text, source, built_at, expires_at FROM vb_plan")
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!(Some(plan_id), built.plan_id);
    assert_eq!(source, "LIVE");
    assert_eq!(expires - built_at, chrono::Duration::minutes(30));
    let lines: Vec<(String, String, String)> =
        sqlx::query_as("SELECT kind, state, note FROM vb_plan_line ORDER BY instrument_id")
            .fetch_all(&mut conn)
            .await
            .unwrap();
    assert_eq!(lines.len(), 3);
    for (kind, state, note) in &lines {
        assert_eq!(
            (kind.as_str(), state.as_str()),
            ("BUY_AT_MARKET", "PROPOSED")
        );
        assert!(
            note.starts_with("live scan 13:15: buy now, at market; "),
            "{note}"
        );
    }
    // No settle, no exits and no order from a live scan.
    assert_eq!(
        scalar_i64(&mut conn, "SELECT count(*) FROM vb_session").await,
        0
    );
    assert_eq!(
        scalar_i64(&mut conn, "SELECT count(*) FROM vb_order").await,
        0
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn no_vbt_signal_means_no_live_plan() {
    let mut conn = seeded().await;
    exec(
        &mut conn,
        "DELETE FROM vb_signal_daily WHERE state = 'SIGNAL'",
    )
    .await;
    let built = live_plan(&mut conn).await;
    assert_eq!((built.plan_id, built.entries, built.kept), (None, 0, false));
    assert_eq!(
        scalar_i64(&mut conn, "SELECT count(*) FROM vb_plan").await,
        0
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_rescan_keeps_a_live_plan_whose_line_is_at_the_broker() {
    let mut conn = seeded().await;
    let first = live_plan(&mut conn).await;
    exec(
        &mut conn,
        "UPDATE vb_plan_line SET state = 'SENT' WHERE instrument_id = 1",
    )
    .await;
    let second = live_plan(&mut conn).await;
    assert!(second.kept);
    assert_eq!(second.plan_id, first.plan_id);
    assert_eq!(second.entries, 0);
    assert_eq!(
        scalar_i64(&mut conn, "SELECT count(*) FROM vb_plan").await,
        1
    );
    assert_eq!(
        scalar_i64(&mut conn, "SELECT count(*) FROM vb_plan_line").await,
        3
    );
    // A plan with nothing in flight is rebuilt under a new id instead.
    exec(&mut conn, "UPDATE vb_plan_line SET state = 'PROPOSED'").await;
    let third = live_plan(&mut conn).await;
    assert!(!third.kept);
    assert_ne!(third.plan_id, first.plan_id);
    assert_eq!(
        scalar_i64(&mut conn, "SELECT count(*) FROM vb_plan").await,
        1
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_name_with_a_working_order_is_not_offered_again() {
    let mut conn = seeded().await;
    exec(
        &mut conn,
        "INSERT INTO vb_order (user_id, instrument_id, signal_date, limit_price, stop_price, \
         quantity, state) VALUES (1, 1, '2026-09-03', 500.00, 440.00, 10, 'SENT')",
    )
    .await;
    let built = live_plan(&mut conn).await;
    assert_eq!(built.entries, 2);
    assert_eq!(
        scalar_i64(
            &mut conn,
            "SELECT count(*) FROM vb_plan_line WHERE instrument_id = 1"
        )
        .await,
        0
    );
    assert_eq!(
        scalar_i64(
            &mut conn,
            "SELECT instrument_id FROM vb_plan_skip WHERE reason = 'ALREADY_WORKING'"
        )
        .await,
        1
    );
}

// --- test_vbt_evening.py, test_vbt_tenancy.py ----------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn every_line_is_proposed_and_the_morning_is_a_second_row_with_its_own_lines() {
    let mut conn = seeded().await;
    exec(
        &mut conn,
        "INSERT INTO vb_position (user_id, instrument_id, entry_date, entry_avg, quantity_open, \
         initial_stop, stop_price) VALUES (1, 5, '2026-09-02', 90.0000, 100, 79.20, 79.20)",
    )
    .await;
    evening(&mut conn, d(3), SOURCE_EVENING, false, evening_at(3)).await;
    evening(&mut conn, d(3), SOURCE_MORNING, false, evening_at(3)).await;
    assert_eq!(
        scalar_i64(
            &mut conn,
            "SELECT count(*) FROM vb_plan_line WHERE state <> 'PROPOSED'"
        )
        .await,
        0
    );
    let per_source: Vec<(String, i64)> = sqlx::query_as(
        "SELECT p.source, count(l.id) FROM vb_plan p JOIN vb_plan_line l ON l.plan_id = p.id \
         GROUP BY p.source ORDER BY p.source",
    )
    .fetch_all(&mut conn)
    .await
    .unwrap();
    assert_eq!(
        per_source,
        [("EVENING".to_owned(), 4), ("MORNING".to_owned(), 4)]
    );
    assert_eq!(
        scalar_i64(&mut conn, "SELECT count(*) FROM vb_order").await,
        0
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn an_evening_writes_only_its_users_rows_and_leaves_another_book_alone() {
    let mut conn = seeded().await;
    exec(
        &mut conn,
        "INSERT INTO vb_config (user_id, sleeve_capital_inr) VALUES (2, 1000000.00); \
         INSERT INTO vb_breadth_daily VALUES (2, '2026-09-03', 'OPEN', false); \
         INSERT INTO vb_signal_daily VALUES \
           (2, '2026-09-03', 4, 'SIGNAL', 40.00, 990000000, 990000000, false); \
         INSERT INTO vb_order (user_id, instrument_id, signal_date, limit_price, stop_price, \
           quantity, state) VALUES (2, 1, '2026-08-01', 500.00, 440.00, 10, 'PROPOSED'); \
         INSERT INTO vb_position (user_id, instrument_id, entry_date, entry_avg, quantity_open, \
           initial_stop, stop_price) VALUES (2, 3, '2026-09-01', 250.0000, 10, 220, 220)",
    )
    .await;
    let (_, report) = evening(&mut conn, d(3), SOURCE_EVENING, false, evening_at(3)).await;
    let report = report.unwrap();
    // User 2's open position in CORE is not user 1's: CORE is still bought, nothing is armed.
    assert_eq!((report.entries, report.arms), (3, 0));
    for table in ["vb_plan", "vb_plan_line", "vb_plan_skip", "vb_session"] {
        assert_eq!(
            scalar_i64(
                &mut conn,
                &format!("SELECT count(*) FROM {table} WHERE user_id <> 1")
            )
            .await,
            0,
            "{table}"
        );
    }
    assert_eq!(
        scalar_i64(
            &mut conn,
            "SELECT count(*) FROM vb_order WHERE user_id = 2 AND state = 'PROPOSED' \
             AND expires_after_session IS NULL AND sessions_worked = 0"
        )
        .await,
        1
    );
    assert_eq!(
        scalar_i64(
            &mut conn,
            "SELECT count(*) FROM vb_config_audit WHERE user_id = 2"
        )
        .await,
        0
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn a_close_below_the_trail_ma_becomes_tomorrows_sell() {
    let mut conn = seeded().await;
    // Sixty sessions ending on 4 Sep: flat at 100, then a close of 80 on the last one.
    exec(
        &mut conn,
        "DELETE FROM trading_day; \
         INSERT INTO trading_day \
           SELECT 1, g::date, true FROM generate_series(DATE '2026-05-01', DATE '2026-09-30', \
           INTERVAL '1 day') g WHERE extract(isodow FROM g) < 6; \
         INSERT INTO ohlcv_daily (instrument_id, date, open, high, low, close, close_raw, volume) \
           SELECT 5, t.date, 100, 101, 99, 100, 100, 100000 FROM trading_day t \
           WHERE t.date < '2026-09-04' AND t.date >= '2026-06-10'; \
         INSERT INTO ohlcv_daily (instrument_id, date, open, high, low, close, close_raw, volume) \
           VALUES (5, '2026-09-04', 100, 100, 79, 80, 80, 100000); \
         INSERT INTO vb_position (user_id, instrument_id, entry_date, entry_avg, quantity_open, \
           initial_stop, stop_price, gtt_id) VALUES (1, 5, '2026-07-01', 100.0000, 500, 70.00, \
           70.00, 'gtt-1')",
    )
    .await;
    let (_, report) = evening(&mut conn, d(4), SOURCE_EVENING, false, evening_at(4)).await;
    assert_eq!(report.unwrap().sells, 1);
    let (quantity, reason, position_id): (i32, Option<String>, Option<i64>) = sqlx::query_as(
        "SELECT quantity, reason, position_id FROM vb_plan_line WHERE kind = 'SELL_AT_OPEN'",
    )
    .fetch_one(&mut conn)
    .await
    .unwrap();
    let held = scalar_i64(
        &mut conn,
        "SELECT id FROM vb_position WHERE instrument_id = 5",
    )
    .await;
    assert_eq!(
        (quantity, reason.as_deref(), position_id),
        (500, Some("MA_TRAIL"), Some(held))
    );
    let (queued_for, why): (Option<NaiveDate>, Option<String>) = sqlx::query_as(
        "SELECT exit_queued_for, exit_reason_queued FROM vb_position WHERE instrument_id = 5",
    )
    .fetch_one(&mut conn)
    .await
    .unwrap();
    assert_eq!(queued_for, Some(d(7)));
    assert_eq!(why.as_deref(), Some("MA_TRAIL"));
}

// --- test_vbt_ops.py -----------------------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
async fn every_live_state_can_be_late_and_no_terminal_one_or_unadopted_one_is() {
    let mut conn = scratch().await;
    let settings = settings();
    let inputs = CheckInputs {
        now: at_ist(8, 21, 40),
        user_id: USER,
        settings: &settings,
    };
    for state in ["PROPOSED", "CONFIRMED", "SENT", "PARTIAL"] {
        exec(&mut conn, "DELETE FROM vb_order").await;
        exec(
            &mut conn,
            &format!(
                "INSERT INTO vb_order (user_id, instrument_id, signal_date, limit_price, \
                 stop_price, quantity, state, expires_after_session) VALUES \
                 (1, 1, '2026-09-01', 10, 9, 1, '{state}', '2026-09-07')"
            ),
        )
        .await;
        let out = check_orders_past_expiry(&mut conn, &inputs).await.unwrap();
        assert_eq!(field(&out, "past_expiry"), &OrderedJson::Int(1), "{state}");
        let orders = field(field(field(&out, "alert"), "detail"), "orders");
        let OrderedJson::Array(orders) = orders else {
            panic!("a list")
        };
        assert_eq!(
            field(&orders[0], "state"),
            &OrderedJson::Str(state.to_owned())
        );
        assert_eq!(
            field(&orders[0], "expires_after_session"),
            &OrderedJson::Str("2026-09-07".to_owned())
        );
    }
    exec(&mut conn, "DELETE FROM vb_order").await;
    exec(
        &mut conn,
        "INSERT INTO vb_order (user_id, instrument_id, signal_date, limit_price, stop_price, \
         quantity, state, expires_after_session) VALUES \
         (1, 1, '2026-07-01', 10, 9, 1, 'FILLED', '2026-07-03'), \
         (1, 2, '2026-07-01', 10, 9, 1, 'CANCELLED', '2026-07-03'), \
         (1, 3, '2026-07-01', 10, 9, 1, 'EXPIRED', '2026-07-03'), \
         (1, 4, '2026-07-01', 10, 9, 1, 'REJECTED', '2026-07-03'), \
         (1, 5, '2026-07-01', 10, 9, 1, 'SENT', NULL), \
         (2, 6, '2026-07-01', 10, 9, 1, 'SENT', '2026-07-03')",
    )
    .await;
    let out = check_orders_past_expiry(&mut conn, &inputs).await.unwrap();
    assert_eq!(field(&out, "past_expiry"), &OrderedJson::Int(0));
    assert_eq!(field(&out, "alert"), &OrderedJson::Null);
}
