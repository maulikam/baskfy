//! The worker's Python database tests for this lane, ported: `test_swing_eod.py` (the day-three
//! partial, the watchlist that fills and prunes itself, the plan with its skips, the session
//! counter, the first-live countdown), `test_swing_ladder.py` (the rung, its audit, idempotency,
//! the closes it reads), `test_swing_catalyst.py` (the feed's symbols, the upsert, the watch
//! fill) and `test_swing_ops.py`'s detect-freshness reads.
//!
//! **Where they run.** On a *clone* of the bed (an `rst_*`/`rsn_*` database — never `baskfy`),
//! each test inside one transaction that is **rolled back**: a fresh `app_user` and its
//! `sw_config`, fresh instruments and bars, then the function under test. Set
//! `BASKFY_RS_SWING_EOD_DB_URL` and run `cargo test -p baskfy-worker -- --ignored
//! tasks::swing_eod::db_tests`; without it they are reported `ignored`. The task-level parity
//! against Python on real clones is `task_diff.py --cases tools/parity/task_cases/swing_eod.json`.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::too_many_lines
)]

use baskfy_api::Settings;
use baskfy_core::swing::plan::LineKind;
use baskfy_core::swing::setups::SwingConfig;
use chrono::{NaiveDate, TimeZone as _, Utc};
use rust_decimal::Decimal;
use sqlx::{Connection as _, PgConnection};

use super::catalyst::run_swing_catalyst;
use super::eod::run_swing_eod;
use super::watch::{WATCHING, auto_watch};
use crate::steps::{StepOutcome, StepStatus};

const IGNORE: &str =
    "needs BASKFY_RS_SWING_EOD_DB_URL (a clone of the bed, never baskfy): run with --ignored";

fn d(text: &str) -> Decimal {
    text.parse().unwrap()
}

/// `AS_OF` of the Python tests.
fn as_of() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 18).unwrap()
}

/// A connection inside a transaction that the test never commits.
pub(super) async fn clone() -> PgConnection {
    let url = std::env::var("BASKFY_RS_SWING_EOD_DB_URL").unwrap_or_else(|_| panic!("{IGNORE}"));
    let database = url
        .rsplit('/')
        .next()
        .unwrap_or("")
        .split('?')
        .next()
        .unwrap_or("");
    assert!(
        database.starts_with("rst_") || database.starts_with("rsn_"),
        "refusing the database {database:?}: these tests need a clone of the bed"
    );
    let mut conn = PgConnection::connect(&url).await.unwrap();
    sqlx::query("BEGIN").execute(&mut conn).await.unwrap();
    conn
}

pub(super) async fn exec(conn: &mut PgConnection, sql: &str) {
    sqlx::query(sql).execute(&mut *conn).await.unwrap();
}

/// `_user(capital=)`: an `app_user` and its `sw_config` (the model's defaults: the countdown at
/// 5, rung 0).
pub(super) async fn user(conn: &mut PgConnection, capital: &str) -> i64 {
    let (id,): (i64,) = sqlx::query_as(
        "INSERT INTO app_user (public_id, email) VALUES ('sw5-user', 'sw5@example.com') RETURNING id",
    )
    .fetch_one(&mut *conn)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO sw_config (user_id, sleeve_capital_inr, updated_by) VALUES ($1, $2, 'test')",
    )
    .bind(id)
    .bind(d(capital))
    .execute(&mut *conn)
    .await
    .unwrap();
    id
}

/// `helpers.make_instrument(symbol)`.
pub(super) async fn instrument(conn: &mut PgConnection, symbol: &str) -> i64 {
    let (id,): (i64,) = sqlx::query_as(
        "INSERT INTO instrument (exchange_id, symbol, name, instrument_type, series, is_active) \
         VALUES (1, $1, $1, 'EQ', 'EQ', true) RETURNING id",
    )
    .bind(format!("ZZ{symbol}"))
    .fetch_one(&mut *conn)
    .await
    .unwrap();
    id
}

/// `_sessions_before(as_of, count)`, oldest first.
async fn sessions_before(conn: &mut PgConnection, on: NaiveDate, count: i64) -> Vec<NaiveDate> {
    super::recent_trading_days(conn, on, count).await.unwrap()
}

/// `_bars`: open = close, high +1 %, low -1 %.
async fn bars(conn: &mut PgConnection, instrument_id: i64, dates: &[NaiveDate], closes: &[f64]) {
    for (on, close) in dates.iter().zip(closes) {
        let c = d(&baskfy_core::py::float_repr(*close));
        let high = d(&baskfy_core::py::float_repr(
            (close * 1.01 * 1e4).round() / 1e4,
        ));
        let low = d(&baskfy_core::py::float_repr(
            (close * 0.99 * 1e4).round() / 1e4,
        ));
        sqlx::query(
            "INSERT INTO ohlcv_daily (instrument_id, date, open, high, low, close, volume, \
             close_raw, volume_raw, adj_factor, source) \
             VALUES ($1, $2, $3, $4, $5, $3, 1000000, $3, 1000000, 1, 'nse')",
        )
        .bind(instrument_id)
        .bind(on)
        .bind(c)
        .bind(high)
        .bind(low)
        .execute(&mut *conn)
        .await
        .unwrap();
    }
}

/// `_market(gate=)`.
async fn market(conn: &mut PgConnection, user_id: i64, gate: &str, on: NaiveDate) {
    sqlx::query(
        "INSERT INTO sw_market_daily (user_id, date, constituent_count, pct_up_strong_1m, gate, \
         exposure_level, max_open_positions, max_exposure_pct, new_entries_allowed, \
         parabolic_count, detail) VALUES ($1, $2, 40, 8.0000, $3, 3, 8, 100.00, $4, 0, '{}')",
    )
    .bind(user_id)
    .bind(on)
    .bind(gate)
    .bind(gate != "RED")
    .execute(&mut *conn)
    .await
    .unwrap();
}

/// `_position(...)` (simulated, MA20 trail, as the Python fixture).
#[allow(clippy::too_many_arguments)]
async fn position(
    conn: &mut PgConnection,
    user_id: i64,
    instrument_id: i64,
    entry_date: NaiveDate,
    gtt_id: Option<&str>,
) -> i64 {
    let (id,): (i64,) = sqlx::query_as(
        "INSERT INTO sw_position (user_id, instrument_id, setup, entry_date, entry_avg, \
         quantity_entered, initial_stop, stop, gtt_id, trail, partial_done, quantity_open, \
         state, simulated) VALUES ($1, $2, 'FLAG', $3, 100, 300, 96, 96, $4, 'MA20', false, \
         300, 'OPEN', true) RETURNING id",
    )
    .bind(user_id)
    .bind(instrument_id)
    .bind(entry_date)
    .bind(gtt_id)
    .fetch_one(&mut *conn)
    .await
    .unwrap();
    id
}

/// A detection row (`_setup` of the Python tests).
#[allow(clippy::too_many_arguments)]
async fn setup_row(
    conn: &mut PgConnection,
    user_id: i64,
    instrument_id: i64,
    on: NaiveDate,
    setup: &str,
    status: &str,
    score: &str,
) {
    sqlx::query(
        "INSERT INTO sw_setup_daily (user_id, date, instrument_id, setup, status, score, \
         trigger, stop_ref, adr_pct, turnover_avg) \
         VALUES ($1, $2, $3, $4, $5, $6, 105.00, 98.00, 6.00, 200000000)",
    )
    .bind(user_id)
    .bind(on)
    .bind(instrument_id)
    .bind(setup)
    .bind(status)
    .bind(d(score))
    .execute(&mut *conn)
    .await
    .unwrap();
}

/// A real closed trade.
async fn closed(conn: &mut PgConnection, user_id: i64, instrument_id: i64, on: NaiveDate, r: &str) {
    sqlx::query(
        "INSERT INTO sw_position (user_id, instrument_id, setup, entry_date, entry_avg, \
         quantity_entered, initial_stop, stop, trail, quantity_open, state, closed_on, \
         r_multiple, pnl_inr, simulated) VALUES ($1, $2, 'FLAG', $3, 100, 10, 96, 96, 'MA20', 0, \
         'CLOSED', $3, $4, 0, false)",
    )
    .bind(user_id)
    .bind(instrument_id)
    .bind(on)
    .bind(d(r))
    .execute(&mut *conn)
    .await
    .unwrap();
}

fn settings() -> Settings {
    Settings::default()
}

fn now() -> chrono::DateTime<Utc> {
    Utc.with_ymd_and_hms(2026, 8, 18, 15, 45, 0).unwrap()
}

async fn eod(conn: &mut PgConnection, user_id: i64, execution: bool) -> (StepOutcome, String) {
    let mut outcome = StepOutcome::default();
    let report = run_swing_eod(
        conn,
        &mut outcome,
        as_of(),
        user_id,
        execution,
        now(),
        &settings(),
    )
    .await
    .unwrap();
    let text = crate::pyjson::dumps(&report.as_detail()).unwrap();
    (outcome, text)
}

async fn plan_lines(
    conn: &mut PgConnection,
    user_id: i64,
) -> Vec<(String, i32, Option<Decimal>, Option<String>)> {
    sqlx::query_as(
        "SELECT l.kind, l.quantity, l.stop, l.note FROM sw_plan_line l \
         WHERE l.user_id = $1 ORDER BY l.id",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await
    .unwrap()
}

// --- TestTheDayThreePartial ----------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn it_produces_exactly_a_third_sold_and_a_stop_at_breakeven() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    market(&mut conn, user_id, "GREEN", as_of()).await;
    let dates = sessions_before(&mut conn, as_of(), 40).await;
    let id = instrument(&mut conn, "FLAGCO").await;
    let mut closes = vec![100.0; dates.len() - 4];
    closes.extend([100.0, 104.0, 107.0, 110.0]);
    bars(&mut conn, id, &dates, &closes).await;
    position(
        &mut conn,
        user_id,
        id,
        dates[dates.len() - 4],
        Some("GTT-1"),
    )
    .await;
    let (_, report) = eod(&mut conn, user_id, false).await;
    assert!(report.contains("\"positions_managed\": 1"), "{report}");
    assert!(report.contains("\"naked_positions\": []"), "{report}");
    let lines = plan_lines(&mut conn, user_id).await;
    assert_eq!(
        lines,
        vec![
            (
                "SELL_AT_OPEN".to_owned(),
                100,
                None,
                Some("PARTIAL_INTO_STRENGTH".to_owned())
            ),
            (
                "RAISE_GTT_STOP".to_owned(),
                0,
                Some(d("100.00")),
                Some("BREAKEVEN_AFTER_PARTIAL".to_owned())
            ),
        ]
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn day_two_produces_no_partial_and_one_full_r_moves_the_stop() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    market(&mut conn, user_id, "GREEN", as_of()).await;
    let dates = sessions_before(&mut conn, as_of(), 40).await;
    let quiet = instrument(&mut conn, "DAYTWO").await;
    let mut closes = vec![100.0; dates.len() - 3];
    closes.extend([100.0, 101.0, 102.0]);
    bars(&mut conn, quiet, &dates, &closes).await;
    position(
        &mut conn,
        user_id,
        quiet,
        dates[dates.len() - 3],
        Some("GTT-1"),
    )
    .await;
    let one_r = instrument(&mut conn, "ONER").await;
    let mut closes = vec![100.0; dates.len() - 3];
    closes.extend([100.0, 102.0, 104.0]);
    bars(&mut conn, one_r, &dates, &closes).await;
    position(
        &mut conn,
        user_id,
        one_r,
        dates[dates.len() - 3],
        Some("GTT-2"),
    )
    .await;
    let (_, report) = eod(&mut conn, user_id, false).await;
    assert!(report.contains("\"positions_managed\": 2"), "{report}");
    assert_eq!(
        plan_lines(&mut conn, user_id).await,
        vec![(
            "RAISE_GTT_STOP".to_owned(),
            0,
            Some(d("100.00")),
            Some("BREAKEVEN_AT_R".to_owned())
        )]
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn a_position_with_no_bar_today_is_not_managed_and_a_naked_one_is_reported() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    market(&mut conn, user_id, "GREEN", as_of()).await;
    let dates = sessions_before(&mut conn, as_of(), 40).await;
    let halted = instrument(&mut conn, "HALTED").await;
    bars(
        &mut conn,
        halted,
        &dates[..dates.len() - 1],
        &vec![100.0; dates.len() - 1],
    )
    .await;
    position(
        &mut conn,
        user_id,
        halted,
        dates[dates.len() - 5],
        Some("GTT-1"),
    )
    .await;
    let naked = instrument(&mut conn, "NAKEDCO").await;
    bars(&mut conn, naked, &dates, &vec![100.0; dates.len()]).await;
    position(&mut conn, user_id, naked, dates[dates.len() - 5], None).await;
    let (_, report) = eod(&mut conn, user_id, false).await;
    assert!(report.contains("\"positions_managed\": 1"), "{report}");
    assert!(
        report.contains("\"naked_positions\": [\"ZZNAKEDCO\"]"),
        "{report}"
    );
}

// --- TestTheWatchlistFillsAndPrunesItself ------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn a_high_scoring_flag_and_every_ep_are_watched_and_nothing_else_twice() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let config = SwingConfig::default();
    let flag = instrument(&mut conn, "HIGHFLAG").await;
    let low = instrument(&mut conn, "LOWFLAG").await;
    let ep = instrument(&mut conn, "LOWEP").await;
    let breakout = instrument(&mut conn, "BROKE").await;
    let parabolic = instrument(&mut conn, "PARA").await;
    setup_row(
        &mut conn,
        user_id,
        flag,
        as_of(),
        "FLAG",
        "SETTING_UP",
        "80",
    )
    .await;
    setup_row(&mut conn, user_id, low, as_of(), "FLAG", "SETTING_UP", "40").await;
    setup_row(&mut conn, user_id, ep, as_of(), "EP", "GAP_DAY", "20").await;
    setup_row(
        &mut conn,
        user_id,
        breakout,
        as_of(),
        "FLAG",
        "BREAKOUT_TODAY",
        "95",
    )
    .await;
    setup_row(
        &mut conn,
        user_id,
        parabolic,
        as_of(),
        "PARABOLIC_SHORT",
        "RUNNING",
        "99",
    )
    .await;
    let first = auto_watch(&mut conn, user_id, as_of(), &config)
        .await
        .unwrap();
    assert_eq!(first.added, 2);
    let second = auto_watch(&mut conn, user_id, as_of(), &config)
        .await
        .unwrap();
    assert_eq!(second.added, 0);
    let watched: Vec<(i64, String, Option<NaiveDate>, bool)> = sqlx::query_as(
        "SELECT instrument_id, source, expires_on, focus FROM sw_watch WHERE user_id = $1 \
         AND state = $2 ORDER BY instrument_id",
    )
    .bind(user_id)
    .bind(WATCHING)
    .fetch_all(&mut conn)
    .await
    .unwrap();
    assert_eq!(watched.len(), 2);
    assert_eq!(watched[0].0, flag);
    assert_eq!(watched[1].0, ep);
    assert!(
        watched
            .iter()
            .all(|w| w.1 == "DETECTOR" && w.2.is_some() && w.3)
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn a_row_past_its_expiry_leaves_the_list_without_being_deleted() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let id = instrument(&mut conn, "OLDFLAG").await;
    sqlx::query(
        "INSERT INTO sw_watch (user_id, instrument_id, setup, source, added_on, expires_on, \
         trigger, state) VALUES ($1, $2, 'FLAG', 'DETECTOR', DATE '2026-08-01', \
         DATE '2026-08-14', 105, 'WATCHING')",
    )
    .bind(user_id)
    .bind(id)
    .execute(&mut conn)
    .await
    .unwrap();
    let result = auto_watch(&mut conn, user_id, as_of(), &SwingConfig::default())
        .await
        .unwrap();
    assert_eq!(result.expired, 1);
    let (state,): (String,) = sqlx::query_as("SELECT state FROM sw_watch WHERE user_id = $1")
        .bind(user_id)
        .fetch_one(&mut conn)
        .await
        .unwrap();
    assert_eq!(state, "EXPIRED");
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn a_manual_row_written_before_the_rule_is_given_its_clock_once() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let id = instrument(&mut conn, "TYPED").await;
    exec(
        &mut conn,
        &format!(
            "INSERT INTO sw_watch (user_id, instrument_id, setup, source, added_on, trigger, \
             state) VALUES ({user_id}, {id}, 'FLAG', 'MANUAL', DATE '2026-08-17', 105, 'WATCHING')"
        ),
    )
    .await;
    let result = auto_watch(&mut conn, user_id, as_of(), &SwingConfig::default())
        .await
        .unwrap();
    assert_eq!(result.expired, 0);
    let expected =
        super::recent_trading_days(&mut conn, NaiveDate::from_ymd_opt(2026, 9, 30).unwrap(), 60)
            .await
            .unwrap()
            .into_iter()
            .filter(|day| *day > NaiveDate::from_ymd_opt(2026, 8, 17).unwrap())
            .nth(9);
    let (expires,): (Option<NaiveDate>,) =
        sqlx::query_as("SELECT expires_on FROM sw_watch WHERE user_id = $1")
            .bind(user_id)
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!(expires, expected);
}

// --- The plan, its skips, the session counter -------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn a_red_gate_plans_nothing_and_says_why_for_every_name() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    market(&mut conn, user_id, "RED", as_of()).await;
    let id = instrument(&mut conn, "REDFLAG").await;
    setup_row(&mut conn, user_id, id, as_of(), "FLAG", "SETTING_UP", "80").await;
    let (_, report) = eod(&mut conn, user_id, false).await;
    assert!(report.contains("\"entries\": 0"), "{report}");
    let skips: Vec<(String,)> =
        sqlx::query_as("SELECT reason FROM sw_plan_skip WHERE user_id = $1")
            .bind(user_id)
            .fetch_all(&mut conn)
            .await
            .unwrap();
    assert_eq!(skips, vec![("GATE_RED".to_owned(),)]);
    let (source, gate): (String, String) =
        sqlx::query_as("SELECT source, gate FROM sw_plan WHERE user_id = $1")
            .bind(user_id)
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!((source.as_str(), gate.as_str()), ("EOD_PREVIEW", "RED"));
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn the_client_id_carries_the_kind_and_the_plan_expires_in_thirty_minutes() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    market(&mut conn, user_id, "GREEN", as_of()).await;
    let dates = sessions_before(&mut conn, as_of(), 40).await;
    let id = instrument(&mut conn, "FLAGCO").await;
    let mut closes = vec![100.0; dates.len() - 4];
    closes.extend([100.0, 104.0, 107.0, 110.0]);
    bars(&mut conn, id, &dates, &closes).await;
    position(
        &mut conn,
        user_id,
        id,
        dates[dates.len() - 4],
        Some("GTT-1"),
    )
    .await;
    eod(&mut conn, user_id, false).await;
    let ids: Vec<(String,)> = sqlx::query_as(
        "SELECT l.client_id FROM sw_plan_line l JOIN sw_plan p ON p.id = l.plan_id \
         WHERE l.user_id = $1 AND l.client_id = p.plan_id::text || ':ZZFLAGCO:' || l.kind \
         AND p.expires_at - p.built_at = interval '30 minutes'",
    )
    .bind(user_id)
    .fetch_all(&mut conn)
    .await
    .unwrap();
    assert_eq!(ids.len(), 2);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn a_date_the_detectors_never_ran_is_skipped() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let mut outcome = StepOutcome::default();
    let report = run_swing_eod(
        &mut conn,
        &mut outcome,
        as_of(),
        user_id,
        false,
        now(),
        &settings(),
    )
    .await
    .unwrap();
    assert_eq!(outcome.status, StepStatus::Skipped);
    let text = crate::pyjson::dumps(&report.as_detail()).unwrap();
    assert!(text.contains("\"gate\": \"UNKNOWN\""), "{text}");
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn one_row_per_session_whether_or_not_anything_happened() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    market(&mut conn, user_id, "GREEN", as_of()).await;
    let (_, first) = eod(&mut conn, user_id, false).await;
    let (_, second) = eod(&mut conn, user_id, false).await;
    assert!(first.contains("\"sessions_logged\": 1"), "{first}");
    assert!(second.contains("\"sessions_logged\": 1"), "{second}");
    let (mode, plans): (String, i32) = sqlx::query_as(
        "SELECT mode, jsonb_array_length(plan_ids->'plans') FROM sw_session WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_one(&mut conn)
    .await
    .unwrap();
    assert_eq!((mode.as_str(), plans), ("DRY_RUN", 2));
}

// --- The first-live countdown (A9) -------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn a_live_session_decrements_once_and_a_dry_run_one_never() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    market(&mut conn, user_id, "GREEN", as_of()).await;
    let (_, paper) = eod(&mut conn, user_id, false).await;
    assert!(paper.contains("\"sessions_left\": 5"), "{paper}");
    assert!(paper.contains("\"risk_multiplier\": \"1\""), "{paper}");
    exec(
        &mut conn,
        &format!("DELETE FROM sw_plan WHERE user_id = {user_id}"),
    )
    .await;
    exec(
        &mut conn,
        &format!("DELETE FROM sw_session WHERE user_id = {user_id}"),
    )
    .await;
    let (_, live) = eod(&mut conn, user_id, true).await;
    assert!(live.contains("\"sessions_left\": 4"), "{live}");
    assert!(live.contains("\"risk_multiplier\": \"0.5\""), "{live}");
    assert!(
        live.contains("first live sessions: 4 left \\u00b7 risk 0.250%"),
        "{live}"
    );
    let (_, again) = eod(&mut conn, user_id, true).await;
    assert!(again.contains("\"sessions_left\": 4"), "{again}");
    let audits: Vec<(String, Option<String>, String)> = sqlx::query_as(
        "SELECT key, old_value, new_value FROM sw_config_audit WHERE user_id = $1 \
         AND key = 'first_live_sessions_left'",
    )
    .bind(user_id)
    .fetch_all(&mut conn)
    .await
    .unwrap();
    assert_eq!(
        audits,
        vec![(
            "first_live_sessions_left".to_owned(),
            Some("5".to_owned()),
            "4".to_owned()
        )]
    );
}

// --- The ladder (SW8) --------------------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn five_good_closes_in_a_green_tape_climb_once_with_an_audit_row() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    market(&mut conn, user_id, "GREEN", as_of()).await;
    let id = instrument(&mut conn, "CLOSER").await;
    for (i, r) in ["1.00", "2.00", "0.50", "1.50", "0.80"].iter().enumerate() {
        let on = NaiveDate::from_ymd_opt(2026, 8, 3 + u32::try_from(i).unwrap()).unwrap();
        closed(&mut conn, user_id, id, on, r).await;
    }
    // A close after the session is not read (house rule 5).
    closed(
        &mut conn,
        user_id,
        id,
        NaiveDate::from_ymd_opt(2026, 8, 20).unwrap(),
        "-1.00",
    )
    .await;
    let (_, first) = eod(&mut conn, user_id, false).await;
    assert!(first.contains("\"from\": 0, \"to\": 1"), "{first}");
    assert!(
        first
            .contains("\"closed_r_multiples\": [\"1.00\", \"2.00\", \"0.50\", \"1.50\", \"0.80\"]"),
        "{first}"
    );
    let (_, second) = eod(&mut conn, user_id, false).await;
    assert!(second.contains("\"from\": 0, \"to\": 1"), "{second}");
    let (level,): (i16,) =
        sqlx::query_as("SELECT exposure_level FROM sw_config WHERE user_id = $1")
            .bind(user_id)
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!(level, 1);
    let audits: Vec<(Option<String>, String, String)> = sqlx::query_as(
        "SELECT old_value, new_value, changed_by FROM sw_config_audit WHERE user_id = $1 \
         AND key = 'exposure_level'",
    )
    .bind(user_id)
    .fetch_all(&mut conn)
    .await
    .unwrap();
    assert_eq!(
        audits,
        vec![(Some("0".to_owned()), "1".to_owned(), "swing-eod".to_owned())]
    );
    let (detail,): (serde_json::Value,) =
        sqlx::query_as("SELECT detail FROM sw_market_daily WHERE user_id = $1 AND date = $2")
            .bind(user_id)
            .bind(as_of())
            .fetch_one(&mut conn)
            .await
            .unwrap();
    assert_eq!(detail["ladder"]["settled_by"], "swing-eod");
    assert_eq!(detail["closed_trades_read"], "real");
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn a_sleeve_that_was_never_seeded_still_settles_the_market_row() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    exec(
        &mut conn,
        &format!("DELETE FROM sw_config WHERE user_id = {user_id}"),
    )
    .await;
    market(&mut conn, user_id, "RED", as_of()).await;
    let (_, report) = eod(&mut conn, user_id, false).await;
    assert!(report.contains("\"drawdown_pct\": \"0\""), "{report}");
    let (level, locked): (i16, bool) = sqlx::query_as(
        "SELECT exposure_level, drawdown_locked FROM sw_market_daily WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_one(&mut conn)
    .await
    .unwrap();
    assert_eq!((level, locked), (0, false));
}

// --- The catalyst feed (SW11B) -----------------------------------------------------------

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn nothing_watched_is_a_noted_no_op_and_no_provider_is_a_noted_skip() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, "1000000").await;
    let mut outcome = StepOutcome::default();
    let report = run_swing_catalyst::<baskfy_providers::nse::NseProvider>(
        &mut conn,
        &mut outcome,
        as_of(),
        user_id,
        None,
        None,
        None,
    )
    .await
    .unwrap();
    let text = crate::pyjson::dumps(&report.as_detail()).unwrap();
    assert!(text.contains("\"symbols\": 0"), "{text}");
    assert!(text.contains("nothing watched"), "{text}");
    let id = instrument(&mut conn, "WATCHED").await;
    exec(
        &mut conn,
        &format!(
            "INSERT INTO sw_watch (user_id, instrument_id, setup, source, added_on, trigger, \
             state) VALUES ({user_id}, {id}, 'FLAG', 'MANUAL', DATE '2026-08-17', 105, 'WATCHING')"
        ),
    )
    .await;
    let mut outcome = StepOutcome::default();
    let report = run_swing_catalyst::<baskfy_providers::nse::NseProvider>(
        &mut conn,
        &mut outcome,
        as_of(),
        user_id,
        None,
        None,
        None,
    )
    .await
    .unwrap();
    let text = crate::pyjson::dumps(&report.as_detail()).unwrap();
    assert!(text.contains("\"symbols\": 1"), "{text}");
    assert!(text.contains("no catalyst provider configured"), "{text}");
}

#[test]
fn every_plan_kind_the_evening_writes_is_one_the_table_knows() {
    for kind in [
        LineKind::BuyOnTrigger,
        LineKind::SellAtOpen,
        LineKind::RaiseGttStop,
        LineKind::PendingRange,
    ] {
        let value = super::plan::enum_value(&kind);
        assert!(
            [
                "BUY_ON_TRIGGER",
                "SELL_AT_OPEN",
                "RAISE_GTT_STOP",
                "PENDING_RANGE"
            ]
            .contains(&value.as_str())
        );
    }
}
