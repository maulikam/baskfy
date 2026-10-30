//! `services/worker/tests/test_swing_ladder.py`, ported beyond `db_tests`' climb: the step back,
//! the paper book, the five-trade window, and the drawdown containment. Same clone and rollback
//! discipline as `db_tests`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_api::Settings;
use chrono::{Days, NaiveDate, TimeZone as _, Utc};
use rust_decimal::Decimal;
use serde_json::{Value, json};
use sqlx::PgConnection;

use super::db_tests::{clone, instrument};
use super::eod::run_swing_eod;
use crate::steps::StepOutcome;

const FIVE_GOOD: [&str; 5] = ["2.00", "-1.00", "1.50", "-0.50", "2.50"];
const THREE_LOSSES: [&str; 5] = ["2.00", "1.50", "-1.00", "-0.80", "-1.00"];

fn as_of() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 18).unwrap()
}

fn yesterday() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 17).unwrap()
}

fn d(text: &str) -> Decimal {
    text.parse().unwrap()
}

/// `_user(rung=, capital=, peak=, locked=)`.
async fn user(
    conn: &mut PgConnection,
    rung: i16,
    capital: &str,
    peak: Option<&str>,
    locked: bool,
) -> i64 {
    let (id,): (i64,) = sqlx::query_as(
        "INSERT INTO app_user (public_id, email) VALUES ('sw8-user', 'sw8@example.com') \
         RETURNING id",
    )
    .fetch_one(&mut *conn)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO sw_config (user_id, sleeve_capital_inr, exposure_level, sleeve_peak_inr, \
         drawdown_locked, updated_by) VALUES ($1, $2, $3, $4, $5, 'test')",
    )
    .bind(id)
    .bind(d(capital))
    .bind(rung)
    .bind(peak.map(d))
    .bind(locked)
    .execute(&mut *conn)
    .await
    .unwrap();
    id
}

/// `_market(gate=, on=, level=, settled=)`.
async fn market(
    conn: &mut PgConnection,
    user_id: i64,
    gate: &str,
    on: NaiveDate,
    level: i16,
    settled: Option<(i16, i16)>,
) {
    let mut detail = json!({"sectors": [], "closed_r_multiples": [], "closed_trades_read": "real"});
    if let Some((from, to)) = settled {
        detail["ladder"] = json!({"from": from, "to": to, "settled_by": "swing-eod"});
    }
    sqlx::query(
        "INSERT INTO sw_market_daily (user_id, date, constituent_count, pct_up_strong_1m, gate, \
         exposure_level, max_open_positions, max_exposure_pct, new_entries_allowed, \
         parabolic_count, detail) VALUES ($1, $2, 40, 8.0000, $3, $4, 2, 40.00, $5, 0, $6)",
    )
    .bind(user_id)
    .bind(on)
    .bind(gate)
    .bind(level)
    .bind(gate != "RED")
    .bind(detail)
    .execute(&mut *conn)
    .await
    .unwrap();
}

/// `_closed(symbol=, r=, closed_on=, simulated=, pnl=)`: entry 100, one R = 4.
async fn closed(
    conn: &mut PgConnection,
    user_id: i64,
    symbol: &str,
    r: Option<&str>,
    closed_on: NaiveDate,
    simulated: bool,
    pnl: Option<&str>,
) {
    let id = instrument(conn, symbol).await;
    let r_multiple = r.map(d);
    let exit_avg = r_multiple.map(|r| Decimal::from(100) + r * Decimal::from(4));
    let pnl_inr = match pnl {
        Some(p) => Some(d(p)),
        None => r_multiple.map(|r| r * Decimal::from(400)),
    };
    sqlx::query(
        "INSERT INTO sw_position (user_id, instrument_id, setup, entry_date, entry_avg, \
         quantity_entered, initial_stop, stop, trail, partial_done, quantity_open, state, \
         closed_on, exit_avg, close_reason, r_multiple, pnl_inr, simulated) \
         VALUES ($1, $2, 'FLAG', $3, 100.0000, 100, 96.00, 96.00, 'MA20', false, 0, 'CLOSED', \
         $4, $5, 'CLOSE_BELOW_TRAIL_MA', $6, $7, $8)",
    )
    .bind(user_id)
    .bind(id)
    .bind(closed_on.checked_sub_days(Days::new(7)).unwrap())
    .bind(closed_on)
    .bind(exit_avg)
    .bind(r_multiple)
    .bind(pnl_inr)
    .bind(simulated)
    .execute(&mut *conn)
    .await
    .unwrap();
}

/// `_closes(rs=, simulated=)`: oldest first, on successive days ending yesterday.
async fn closes(conn: &mut PgConnection, user_id: i64, rs: &[&str], simulated: bool) {
    let n = u64::try_from(rs.len()).unwrap();
    let first = yesterday().checked_sub_days(Days::new(n - 1)).unwrap();
    for (i, r) in (0_u64..).zip(rs) {
        let on = first.checked_add_days(Days::new(i)).unwrap();
        closed(
            conn,
            user_id,
            &format!("DONE{i}"),
            Some(r),
            on,
            simulated,
            None,
        )
        .await;
    }
}

/// The evening's report, parsed.
async fn eod(conn: &mut PgConnection, user_id: i64, execution: bool) -> Value {
    let mut outcome = StepOutcome::default();
    let report = run_swing_eod(
        conn,
        &mut outcome,
        as_of(),
        user_id,
        execution,
        Utc.with_ymd_and_hms(2026, 8, 18, 15, 45, 0).unwrap(),
        &Settings::default(),
    )
    .await
    .unwrap();
    serde_json::from_str(&crate::pyjson::dumps(&report.as_detail()).unwrap()).unwrap()
}

fn rungs(report: &Value) -> (i64, i64) {
    (
        report["ladder"]["from"].as_i64().unwrap(),
        report["ladder"]["to"].as_i64().unwrap(),
    )
}

fn closed_r(report: &Value) -> Vec<String> {
    report["ladder"]["closed_r_multiples"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_owned())
        .collect()
}

fn drawdown(report: &Value) -> (String, bool) {
    (
        report["ladder"]["drawdown_pct"]
            .as_str()
            .unwrap()
            .to_owned(),
        report["ladder"]["drawdown_locked"].as_bool().unwrap(),
    )
}

async fn audit(conn: &mut PgConnection, user_id: i64, key: &str) -> Vec<(Option<String>, String)> {
    sqlx::query_as(
        "SELECT old_value, new_value FROM sw_config_audit WHERE user_id = $1 AND key = $2 \
         ORDER BY id",
    )
    .bind(user_id)
    .bind(key)
    .fetch_all(&mut *conn)
    .await
    .unwrap()
}

/// `(exposure_level, sleeve_peak_inr, drawdown_pct, drawdown_locked)` of `sw_config`.
async fn config(conn: &mut PgConnection, user_id: i64) -> (i16, Option<Decimal>, Decimal, bool) {
    sqlx::query_as(
        "SELECT exposure_level, sleeve_peak_inr, drawdown_pct, drawdown_locked FROM sw_config \
         WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_one(&mut *conn)
    .await
    .unwrap()
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn three_losses_in_a_row_move_it_back() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, 1, "1000000", None, false).await;
    market(&mut conn, user_id, "GREEN", yesterday(), 1, Some((0, 1))).await;
    market(&mut conn, user_id, "GREEN", as_of(), 1, None).await;
    closes(&mut conn, user_id, &THREE_LOSSES, false).await;
    let report = eod(&mut conn, user_id, false).await;
    assert_eq!(rungs(&report), (1, 0));
    assert_eq!(config(&mut conn, user_id).await.0, 0);
    let (level,): (i16,) = sqlx::query_as(
        "SELECT exposure_level FROM sw_market_daily WHERE user_id = $1 AND date = $2",
    )
    .bind(user_id)
    .bind(as_of())
    .fetch_one(&mut conn)
    .await
    .unwrap();
    assert_eq!(level, 0);
    assert_eq!(
        audit(&mut conn, user_id, "exposure_level").await,
        [(Some("1".to_owned()), "0".to_owned())]
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn an_unchanged_rung_leaves_no_audit_row() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, 0, "1000000", None, false).await;
    market(&mut conn, user_id, "AMBER", as_of(), 0, None).await;
    closes(&mut conn, user_id, &FIVE_GOOD, false).await;
    let report = eod(&mut conn, user_id, false).await;
    assert_eq!(rungs(&report), (0, 0));
    assert!(audit(&mut conn, user_id, "exposure_level").await.is_empty());
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn the_rung_carries_over_and_a_rewritten_previous_row_does_not_move_it() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, 1, "1000000", None, false).await;
    market(&mut conn, user_id, "GREEN", yesterday(), 1, Some((0, 1))).await;
    market(&mut conn, user_id, "GREEN", as_of(), 0, None).await;
    closes(&mut conn, user_id, &FIVE_GOOD, false).await;
    assert_eq!(rungs(&eod(&mut conn, user_id, false).await), (1, 2));

    drop(conn);
    let mut conn = clone().await;
    let user_id = user(&mut conn, 1, "1000000", None, false).await;
    market(&mut conn, user_id, "GREEN", yesterday(), 2, None).await;
    market(&mut conn, user_id, "AMBER", as_of(), 2, None).await;
    assert_eq!(rungs(&eod(&mut conn, user_id, false).await), (1, 1));
    assert_eq!(config(&mut conn, user_id).await.0, 1);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn simulated_closes_never_move_the_rung_whatever_the_flag_says() {
    for execution in [false, true] {
        let mut conn = clone().await;
        let user_id = user(&mut conn, 0, "1000000", None, false).await;
        market(&mut conn, user_id, "GREEN", as_of(), 0, None).await;
        closes(&mut conn, user_id, &FIVE_GOOD, true).await;
        let report = eod(&mut conn, user_id, execution).await;
        assert_eq!(rungs(&report).1, 0, "execution {execution}");
        assert!(closed_r(&report).is_empty(), "execution {execution}");
    }
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn only_the_last_five_count_and_a_close_without_r_or_after_the_date_is_not_read() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, 0, "1000000", None, false).await;
    market(&mut conn, user_id, "GREEN", as_of(), 0, None).await;
    let seven = ["-3.00", "-3.00", "2.00", "-1.00", "1.50", "-0.50", "2.50"];
    closes(&mut conn, user_id, &seven, false).await;
    let report = eod(&mut conn, user_id, false).await;
    assert_eq!(closed_r(&report), FIVE_GOOD);
    assert_eq!(rungs(&report).1, 1);

    drop(conn);
    let mut conn = clone().await;
    let user_id = user(&mut conn, 0, "1000000", None, false).await;
    market(&mut conn, user_id, "GREEN", as_of(), 0, None).await;
    closes(&mut conn, user_id, &FIVE_GOOD[..4], false).await;
    closed(
        &mut conn,
        user_id,
        "HALFDONE",
        None,
        yesterday(),
        false,
        None,
    )
    .await;
    let later = as_of().checked_add_days(Days::new(1)).unwrap();
    closed(
        &mut conn,
        user_id,
        "LATER",
        Some("3.00"),
        later,
        false,
        None,
    )
    .await;
    let report = eod(&mut conn, user_id, false).await;
    assert_eq!(closed_r(&report), FIVE_GOOD[..4]);
    assert_eq!(rungs(&report).1, 0, "four closes are not five");
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn fifteen_percent_under_its_peak_settles_rung_zero_locked() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, 2, "1000000", Some("1000000"), false).await;
    market(&mut conn, user_id, "GREEN", yesterday(), 2, Some((1, 2))).await;
    market(&mut conn, user_id, "GREEN", as_of(), 2, None).await;
    closes(&mut conn, user_id, &FIVE_GOOD, false).await;
    closed(
        &mut conn,
        user_id,
        "BIGLOSS",
        Some("-1.00"),
        yesterday(),
        false,
        Some("-151800"),
    )
    .await;
    let report = eod(&mut conn, user_id, false).await;
    assert_eq!(rungs(&report), (2, 0));
    assert_eq!(drawdown(&report), ("15.00".to_owned(), true));
    assert_eq!(
        config(&mut conn, user_id).await,
        (0, Some(d("1000000.00")), d("15.00"), true)
    );
    let (allowed, locked, detail): (bool, bool, Value) = sqlx::query_as(
        "SELECT new_entries_allowed, drawdown_locked, detail FROM sw_market_daily \
         WHERE user_id = $1 AND date = $2",
    )
    .bind(user_id)
    .bind(as_of())
    .fetch_one(&mut conn)
    .await
    .unwrap();
    assert_eq!((allowed, locked), (false, true));
    assert_eq!(
        detail["drawdown"],
        json!({"nav": "850000.00", "peak": "1000000.00", "pct": "15.00", "was_locked": false, "locked": true})
    );
    assert_eq!(
        audit(&mut conn, user_id, "drawdown_locked").await,
        [(Some("False".to_owned()), "True".to_owned())]
    );
    let again = eod(&mut conn, user_id, false).await;
    assert_eq!(drawdown(&again), ("15.00".to_owned(), true));
    assert_eq!(audit(&mut conn, user_id, "drawdown_locked").await.len(), 1);
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn the_lock_has_hysteresis_and_lifts_inside_ten_percent() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, 0, "1000000", Some("1000000"), true).await;
    market(&mut conn, user_id, "GREEN", as_of(), 0, None).await;
    closed(
        &mut conn,
        user_id,
        "LOSS",
        Some("-1.00"),
        yesterday(),
        false,
        Some("-120000"),
    )
    .await;
    assert_eq!(
        drawdown(&eod(&mut conn, user_id, false).await),
        ("12.00".to_owned(), true)
    );
    assert!(
        audit(&mut conn, user_id, "drawdown_locked")
            .await
            .is_empty()
    );

    drop(conn);
    let mut conn = clone().await;
    let user_id = user(&mut conn, 0, "1000000", Some("1000000"), false).await;
    market(&mut conn, user_id, "GREEN", as_of(), 0, None).await;
    closed(
        &mut conn,
        user_id,
        "LOSS",
        Some("-1.00"),
        yesterday(),
        false,
        Some("-120000"),
    )
    .await;
    assert_eq!(
        drawdown(&eod(&mut conn, user_id, false).await),
        ("12.00".to_owned(), false)
    );

    drop(conn);
    let mut conn = clone().await;
    let user_id = user(&mut conn, 0, "1000000", Some("1000000"), true).await;
    market(&mut conn, user_id, "GREEN", yesterday(), 0, Some((0, 0))).await;
    market(&mut conn, user_id, "GREEN", as_of(), 0, None).await;
    closed(
        &mut conn,
        user_id,
        "LOSS",
        Some("-1.00"),
        yesterday(),
        false,
        Some("-99000"),
    )
    .await;
    let report = eod(&mut conn, user_id, false).await;
    assert_eq!(drawdown(&report), ("9.90".to_owned(), false));
    assert_eq!(
        audit(&mut conn, user_id, "drawdown_locked").await,
        [(Some("True".to_owned()), "False".to_owned())]
    );
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_SWING_EOD_DB_URL"]
async fn the_first_evening_sets_the_peak_and_the_peak_only_rises() {
    let mut conn = clone().await;
    let user_id = user(&mut conn, 0, "1000000", None, false).await;
    market(&mut conn, user_id, "GREEN", as_of(), 0, None).await;
    closed(
        &mut conn,
        user_id,
        "EARLY",
        Some("-1.00"),
        yesterday(),
        false,
        Some("-200000"),
    )
    .await;
    let report = eod(&mut conn, user_id, false).await;
    assert_eq!(drawdown(&report), ("0.00".to_owned(), false));
    assert_eq!(
        config(&mut conn, user_id).await,
        (0, Some(d("800000.00")), d("0.00"), false)
    );

    drop(conn);
    let mut conn = clone().await;
    let user_id = user(&mut conn, 0, "1000000", Some("900000"), false).await;
    market(&mut conn, user_id, "GREEN", as_of(), 0, None).await;
    closes(&mut conn, user_id, &FIVE_GOOD, false).await;
    eod(&mut conn, user_id, false).await;
    let (_, peak, pct, _) = config(&mut conn, user_id).await;
    assert_eq!((peak, pct), (Some(d("1001800.00")), d("0.00")));

    drop(conn);
    let mut conn = clone().await;
    let user_id = user(&mut conn, 0, "0", None, false).await;
    market(&mut conn, user_id, "GREEN", as_of(), 0, None).await;
    closed(
        &mut conn,
        user_id,
        "EARLY",
        Some("-1.00"),
        yesterday(),
        false,
        Some("-400"),
    )
    .await;
    let report = eod(&mut conn, user_id, false).await;
    assert_eq!(drawdown(&report), ("0.00".to_owned(), false));
    assert_eq!(config(&mut conn, user_id).await.1, Some(d("-400.00")));
}
