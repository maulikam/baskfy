//! The lane's Python database tests, ported: `test_fno_ingest.py` (the ingest, the ban list, the
//! night), `test_fno_alerts.py::TestTodaysEvents`, `test_fno_spread_sample.py::TestTheRun`,
//! `test_fno_index_daily.py` and `test_fno_retest_run.py`'s guards.
//!
//! Each test runs inside one transaction that is rolled back, on a clone of the bed
//! (`BASKFY_RS_FNO_DB_URL`, an `rst_*`/`rsn_*` database). Readers are fakes; nothing leaves the box.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::too_many_lines
)]

use std::collections::BTreeSet;
use std::sync::Mutex;

use baskfy_api::Settings;
use baskfy_db::OrderedJson;
use baskfy_providers::archive::archive_key;
use baskfy_providers::errors::ProviderError;
use baskfy_providers::nse::KIND_FO_BHAVCOPY;
use baskfy_providers::records::{
    DepthLevelFields, DepthLevelRecord, OptionQuoteFields, OptionQuoteRecord,
};
use chrono::{DateTime, Datelike as _, FixedOffset, NaiveDate, TimeZone as _};
use polars::prelude::{
    DataFrame, Int128Chunked, IntoColumn as _, IntoSeries as _, NamedFrom as _,
    NewChunkedArray as _, Series,
};
use rust_decimal::Decimal;
use sqlx::{Connection as _, PgConnection};

use super::alerting::FnoName;
use super::events::{pending_events, run_fno_alerts};
use super::fixtures::{FileReader, ban_file, d, day, next};
use super::index_daily::{self, DailyBarReader};
use super::ingest::{
    contract_rows, ingest_day, partition_name, retain, store_ban_list, upsert_contracts,
};
use super::nightly::run_night;
use super::retest::run_retest;
use super::spreads::{QuoteSource, SampleContract, run_spread_sample};
use crate::error::WorkerError;

const IGNORE: &str = "needs BASKFY_RS_FNO_DB_URL (a clone of the bed, never baskfy)";

fn dec(t: &str) -> Decimal {
    t.parse().unwrap()
}

/// One test at a time: a partition created in one rolled-back transaction blocks the others.
static SERIAL: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

async fn clone() -> (tokio::sync::MutexGuard<'static, ()>, PgConnection) {
    let guard = SERIAL.lock().await;
    let url = std::env::var("BASKFY_RS_FNO_DB_URL").unwrap_or_else(|_| panic!("{IGNORE}"));
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
    (guard, conn)
}

async fn exec(conn: &mut PgConnection, sql: &str) {
    sqlx::query(sql).execute(&mut *conn).await.unwrap();
}

fn ist() -> FixedOffset {
    baskfy_providers::tokens::ist()
}

fn at(day: NaiveDate, h: u32, m: u32) -> DateTime<FixedOffset> {
    ist()
        .from_local_datetime(&day.and_hms_opt(h, m, 0).unwrap())
        .unwrap()
}

async fn calendar(conn: &mut PgConnection, days: &[NaiveDate], open: bool) {
    for day in days {
        sqlx::query(
            "INSERT INTO trading_day (exchange_id, date, is_trading_day, source) VALUES (1, $1, \
             $2, 'derived') ON CONFLICT (exchange_id, date) DO UPDATE SET \
             is_trading_day = excluded.is_trading_day",
        )
        .bind(day)
        .bind(open)
        .execute(&mut *conn)
        .await
        .unwrap();
    }
}

type ContractKey = (
    String,
    NaiveDate,
    Decimal,
    String,
    Option<Decimal>,
    Option<i64>,
    Option<String>,
);

async fn contracts(conn: &mut PgConnection, on: NaiveDate) -> Vec<ContractKey> {
    sqlx::query_as(
        "SELECT symbol, expiry, strike, option_type, settle, open_interest, source_key \
         FROM fo_contract_daily WHERE trade_date = $1 ORDER BY symbol, expiry, strike, option_type",
    )
    .bind(on)
    .fetch_all(&mut *conn)
    .await
    .unwrap()
}

async fn ingest_row(
    conn: &mut PgConnection,
    on: NaiveDate,
) -> Option<(String, i16, Option<String>)> {
    sqlx::query_as("SELECT status, attempts, source_key FROM fo_ingest_day WHERE trade_date = $1")
        .bind(on)
        .fetch_optional(&mut *conn)
        .await
        .unwrap()
}

// --- the ingest -------------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs BASKFY_RS_FNO_DB_URL"]
async fn a_day_lands_by_its_key_with_its_source_and_a_rerun_is_identical() {
    let (_serial, mut conn) = clone().await;
    let reader = FileReader::new(Some(day()), None);
    let report = ingest_day(&mut conn, &reader, day(), false).await.unwrap();
    assert_eq!(report.status, "INGESTED");
    assert_eq!(report.rows_in_file, Some(17));
    let first = contracts(&mut conn, day()).await;
    assert_eq!(Some(i64::try_from(first.len()).unwrap()), report.rows_kept);
    let key = archive_key(KIND_FO_BHAVCOPY, day(), "zip").unwrap();
    assert!(first.iter().all(|r| r.6.as_deref() == Some(key.as_str())));
    let (regclass,): (Option<String>,) = sqlx::query_as("SELECT to_regclass($1)::text")
        .bind(partition_name(day()))
        .fetch_one(&mut conn)
        .await
        .unwrap();
    assert_eq!(
        regclass,
        Some(partition_name(day())),
        "the ingest created the month's partition"
    );
    let asked = reader.reads();
    let again = ingest_day(&mut conn, &reader, day(), false).await.unwrap();
    assert_eq!(again.skipped.as_deref(), Some("already ingested"));
    assert_eq!(reader.reads(), asked, "a finished day asks NSE nothing");
    let rows = super::fixtures::day_rows();
    upsert_contracts(&mut conn, &contract_rows(&retain(&rows)), &key)
        .await
        .unwrap();
    assert_eq!(contracts(&mut conn, day()).await, first, "house rule 7");
    let row = ingest_row(&mut conn, day()).await.unwrap();
    assert_eq!(
        (row.0.as_str(), row.2.as_deref()),
        ("INGESTED", Some(key.as_str()))
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs BASKFY_RS_FNO_DB_URL"]
async fn no_file_is_pending_then_missing_at_2330_and_never_interpolated() {
    let (_serial, mut conn) = clone().await;
    let missing = FileReader::new(None, None);
    let early = ingest_day(&mut conn, &missing, day(), false).await.unwrap();
    assert_eq!(early.status, "PENDING");
    assert!(early.error.as_deref().unwrap_or("").contains("404"));
    let late = ingest_day(&mut conn, &missing, day(), true).await.unwrap();
    assert_eq!(late.status, "MISSING");
    let row = ingest_row(&mut conn, day()).await.unwrap();
    assert_eq!((row.0.as_str(), row.1), ("MISSING", 2));
    assert!(
        contracts(&mut conn, day()).await.is_empty(),
        "a missing day has no rows at all"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs BASKFY_RS_FNO_DB_URL"]
async fn a_file_stamped_with_another_session_writes_nothing() {
    let (_serial, mut conn) = clone().await;
    let report = ingest_day(
        &mut conn,
        &FileReader::new(Some(next()), None),
        day(),
        false,
    )
    .await
    .unwrap();
    assert_eq!(report.status, "PENDING");
    assert!(report.error.is_some());
    assert!(contracts(&mut conn, day()).await.is_empty());
}

async fn ban_flags(conn: &mut PgConnection, on: NaiveDate) -> Vec<(String, bool)> {
    sqlx::query_as(
        "SELECT symbol, in_ban FROM fo_underlying_daily WHERE trade_date = $1 ORDER BY symbol",
    )
    .bind(on)
    .fetch_all(&mut *conn)
    .await
    .unwrap()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs BASKFY_RS_FNO_DB_URL"]
async fn the_next_sessions_ban_list_lands_on_tonights_rows() {
    let (_serial, mut conn) = clone().await;
    exec(
        &mut conn,
        "INSERT INTO fo_underlying_daily (trade_date, symbol, in_ban) VALUES \
         ('2030-01-02', 'SBIN', true), ('2030-01-02', 'IDEA', false)",
    )
    .await;
    let reader = FileReader::new(None, Some(ban_file("03-JAN-2030", &["IDEA", "SAIL"])));
    let report = store_ban_list(&mut conn, &reader, day(), next())
        .await
        .unwrap();
    assert_eq!(
        report.symbols,
        Some(vec!["IDEA".to_owned(), "SAIL".to_owned()])
    );
    assert_eq!(
        ban_flags(&mut conn, day()).await,
        vec![
            ("IDEA".to_owned(), true),
            ("SAIL".to_owned(), true),
            ("SBIN".to_owned(), false)
        ]
    );
    let (session, symbols): (Option<NaiveDate>, Option<Vec<String>>) = sqlx::query_as(
        "SELECT ban_for_session, ban_symbols FROM fo_ingest_day WHERE trade_date = $1",
    )
    .bind(day())
    .fetch_one(&mut conn)
    .await
    .unwrap();
    assert_eq!(
        (session, symbols),
        (
            Some(next()),
            Some(vec!["IDEA".to_owned(), "SAIL".to_owned()])
        )
    );
    let asked = reader.reads();
    let again = store_ban_list(&mut conn, &reader, day(), next())
        .await
        .unwrap();
    assert_eq!(again.skipped.as_deref(), Some("already stored"));
    assert_eq!(reader.reads(), asked);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs BASKFY_RS_FNO_DB_URL"]
async fn todays_ban_list_served_for_tomorrow_writes_nothing() {
    let (_serial, mut conn) = clone().await;
    let reader = FileReader::new(None, Some(ban_file("02-JAN-2030", &["IDEA"])));
    let report = store_ban_list(&mut conn, &reader, day(), next())
        .await
        .unwrap();
    assert!(report.symbols.is_none());
    assert!(report.error.as_deref().unwrap_or("").contains("wrong file"));
    assert!(ingest_row(&mut conn, day()).await.is_none());
    assert!(ban_flags(&mut conn, day()).await.is_empty());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs BASKFY_RS_FNO_DB_URL"]
async fn one_night_ingests_and_stores_the_next_sessions_ban_list() {
    let (_serial, mut conn) = clone().await;
    calendar(&mut conn, &[day(), next()], true).await;
    let reader = FileReader::new(Some(day()), Some(ban_file("03-JAN-2030", &["KAYNES"])));
    let now = day().and_hms_opt(18, 30, 0).unwrap();
    let out = run_night(&mut conn, &reader, day(), now).await.unwrap();
    assert_eq!(
        out.get("ingest").and_then(|i| i.get("status")),
        Some(&OrderedJson::Str("INGESTED".to_owned()))
    );
    assert_eq!(
        out.get("ban_list").and_then(|b| b.get("symbols")),
        Some(&OrderedJson::Array(vec![OrderedJson::Str(
            "KAYNES".to_owned()
        )]))
    );
    assert_eq!(
        out.get("underlying_rows"),
        Some(&OrderedJson::Int(0)),
        "no previous session's expiry"
    );
    let asked = reader.reads();
    let second = run_night(&mut conn, &reader, day(), now).await.unwrap();
    assert_eq!(reader.reads(), asked, "a finished night asks NSE nothing");
    assert_eq!(
        second.get("ban_list").and_then(|b| b.get("skipped")),
        Some(&OrderedJson::Str("already stored".to_owned()))
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs BASKFY_RS_FNO_DB_URL"]
async fn a_holiday_is_skipped_before_any_read() {
    let (_serial, mut conn) = clone().await;
    calendar(&mut conn, &[day()], false).await;
    let reader = FileReader::new(None, None);
    let out = run_night(
        &mut conn,
        &reader,
        day(),
        day().and_hms_opt(23, 30, 0).unwrap(),
    )
    .await
    .unwrap();
    assert_eq!(
        out,
        crate::pyjson::object([
            ("trade_date", crate::pyjson::s("2030-01-02")),
            ("skipped", crate::pyjson::s("not an NSE trading day")),
        ])
    );
    assert_eq!(reader.reads(), 0);
    assert!(ingest_row(&mut conn, day()).await.is_none());
}

// --- today's events ---------------------------------------------------------------------------

fn thu() -> NaiveDate {
    d(2031, 7, 24)
}

fn fri() -> NaiveDate {
    d(2031, 7, 25)
}

fn mon() -> NaiveDate {
    d(2031, 7, 28)
}

async fn user(conn: &mut PgConnection, tag: &str) -> i64 {
    let (id,): (i64,) = sqlx::query_as(
        "INSERT INTO app_user (public_id, email, name) VALUES ($1, $2, 'FO') RETURNING id",
    )
    .bind(format!("rsfo{tag}"))
    .bind(format!("rs-fo-{tag}@example.com"))
    .fetch_one(&mut *conn)
    .await
    .unwrap();
    id
}

struct Plan<'a> {
    plan_id: &'a str,
    trade_date: NaiveDate,
    kind: &'a str,
    status: &'a str,
    detail: serde_json::Value,
}

async fn plan(conn: &mut PgConnection, uid: i64, p: Plan<'_>) {
    sqlx::query(
        "INSERT INTO fo_plan (user_id, plan_id, sleeve, symbol, trade_date, structure, kind, \
         sizing_mode, issued_at, expires_at, lots, lot_size, credit_points, max_loss_inr, \
         hard_exit_date, status, detail) VALUES ($1, $2, 'F1N', 'NIFTY', $3, 'IRON_CONDOR', $4, \
         'BUDGET', $5, $6, 2, 75, 112.40, 24500.00, '2031-08-21', $7, $8)",
    )
    .bind(uid)
    .bind(p.plan_id)
    .bind(p.trade_date)
    .bind(p.kind)
    .bind(at(p.trade_date, 9, 20))
    .bind(at(p.trade_date, 9, 50))
    .bind(p.status)
    .bind(p.detail)
    .execute(&mut *conn)
    .await
    .unwrap();
}

async fn position(
    conn: &mut PgConnection,
    uid: i64,
    sleeve: &str,
    entry: &str,
    hard_exit: Option<NaiveDate>,
    legs: serde_json::Value,
    closed: bool,
) -> i64 {
    let (id,): (i64,) = sqlx::query_as(&format!(
        "INSERT INTO fo_position (user_id, sleeve, symbol, structure, entry_plan_id, legs, lots, \
         lot_size, hard_exit_date, opened_at, closed_at, closed_reason, simulated) VALUES ($1, \
         '{sleeve}', 'BANKNIFTY', 'IRON_CONDOR', $2, $3, 1, 35, $4, $5, $6, $7, true) RETURNING id"
    ))
    .bind(uid)
    .bind(entry)
    .bind(legs)
    .bind(hard_exit)
    .bind(at(thu(), 9, 30))
    .bind(closed.then(|| at(fri(), 15, 1)))
    .bind(closed.then_some("STOP"))
    .fetch_one(&mut *conn)
    .await
    .unwrap();
    id
}

async fn journal(conn: &mut PgConnection, uid: i64, position_id: i64, closed_on: NaiveDate) {
    sqlx::query(
        "INSERT INTO fo_journal (position_id, user_id, sleeve, symbol, structure, opened_on, \
         closed_on, entry_inr, exit_inr, gross_pnl_inr, costs_inr, net_pnl_inr, risk_budget_inr, \
         r_multiple, closed_reason, sessions_held, rolls, simulated, sizing_mode) VALUES ($1, $2, \
         'F2', 'ZQA', 'FUTURE', $3, $4, 0, 0, 3200, 180.50, 3019.50, 10000, 0.30, 'STOP', 1, 0, \
         true, 'BUDGET')",
    )
    .bind(position_id)
    .bind(uid)
    .bind(thu())
    .bind(closed_on)
    .execute(&mut *conn)
    .await
    .unwrap();
}

/// `_seed`: the plans, positions and journal rows of `TestTodaysEvents`.
async fn seed_events(conn: &mut PgConnection) -> i64 {
    let uid = user(conn, "fo12alerts").await;
    calendar(conn, &[thu(), fri(), mon()], true).await;
    let late = serde_json::json!({"violations": [{"code": "LATE_EXIT", "message": "open past 15:00", "at": "2031-07-25T15:05:00+05:30"}]});
    let old_late = serde_json::json!({"violations": [{"code": "LATE_EXIT", "message": "old", "at": "2031-07-24T15:05:00+05:30"}]});
    let none = serde_json::json!({});
    for p in [
        Plan {
            plan_id: "fo12-issued",
            trade_date: fri(),
            kind: "ENTRY",
            status: "ISSUED",
            detail: none.clone(),
        },
        Plan {
            plan_id: "fo12-refused",
            trade_date: fri(),
            kind: "ENTRY",
            status: "REJECTED_MARGIN",
            detail: none.clone(),
        },
        Plan {
            plan_id: "fo12-roll",
            trade_date: fri(),
            kind: "ROLL",
            status: "ISSUED",
            detail: none.clone(),
        },
        Plan {
            plan_id: "fo12-yesterday",
            trade_date: thu(),
            kind: "ENTRY",
            status: "ISSUED",
            detail: none.clone(),
        },
        Plan {
            plan_id: "fo12-late",
            trade_date: thu(),
            kind: "ENTRY",
            status: "CLOSED",
            detail: late,
        },
        Plan {
            plan_id: "fo12-old-late",
            trade_date: thu(),
            kind: "ENTRY",
            status: "CLOSED",
            detail: old_late,
        },
    ] {
        plan(conn, uid, p).await;
    }
    let legs = serde_json::json!({"legs": []});
    position(
        conn,
        uid,
        "F1B",
        "F1B-20310701-1",
        Some(mon()),
        legs.clone(),
        false,
    )
    .await;
    position(
        conn,
        uid,
        "F2",
        "fo12-f2",
        None,
        serde_json::json!({"legs": [], "carry": {"time_exit_date": "2031-07-28"}}),
        false,
    )
    .await;
    let later = position(
        conn,
        uid,
        "F1B",
        "fo12-later",
        Some(d(2031, 8, 21)),
        legs.clone(),
        false,
    )
    .await;
    let closed = position(conn, uid, "F1B", "fo12-closed", Some(mon()), legs, true).await;
    journal(conn, uid, closed, fri()).await;
    journal(conn, uid, later, thu()).await;
    uid
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs BASKFY_RS_FNO_DB_URL"]
async fn the_morning_raises_the_plan_the_exit_and_the_late_exit() {
    let (_serial, mut conn) = clone().await;
    let uid = seed_events(&mut conn).await;
    let events = pending_events(&mut conn, uid, at(fri(), 9, 25))
        .await
        .unwrap();
    let mut names: Vec<&str> = events.iter().map(|e| e.alert.name.value()).collect();
    names.sort_unstable();
    assert_eq!(names, ["FNO_EXIT", "FNO_LATE_EXIT", "FNO_PLAN"]);
    let plan = events
        .iter()
        .find(|e| e.alert.name == FnoName::Plan)
        .unwrap();
    assert_eq!(plan.marker, format!("fno:alert:plan:{uid}:fo12-issued"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs BASKFY_RS_FNO_DB_URL"]
async fn the_evening_names_every_open_structure_exiting_next_session() {
    let (_serial, mut conn) = clone().await;
    let uid = seed_events(&mut conn).await;
    let events = pending_events(&mut conn, uid, at(fri(), 18, 5))
        .await
        .unwrap();
    let hard: Vec<_> = events
        .iter()
        .filter(|e| e.alert.name == FnoName::HardExitTomorrow)
        .map(|e| &e.alert)
        .collect();
    let mut which: Vec<String> = hard
        .iter()
        .map(|a| match a.detail.get("which") {
            Some(OrderedJson::Str(w)) => w.clone(),
            other => panic!("{other:?}"),
        })
        .collect();
    which.sort();
    assert_eq!(which, ["hard exit", "time exit"]);
    assert!(
        hard.iter()
            .all(|a| a.summary.contains("tomorrow, 2031-07-28"))
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs BASKFY_RS_FNO_DB_URL"]
async fn redis_down_delivers_rather_than_silences() {
    let (_serial, mut conn) = clone().await;
    let uid = seed_events(&mut conn).await;
    let down = redis::Client::open("redis://127.0.0.1:1/0").unwrap();
    let out = run_fno_alerts(
        &mut conn,
        &[uid],
        Some(&down),
        at(fri(), 9, 25),
        &Settings::default(),
        chrono::Utc::now,
    )
    .await
    .unwrap();
    let Some(OrderedJson::Array(sent)) = out.get("sent") else {
        panic!("{out:?}")
    };
    assert_eq!(sent.len(), 3);
    assert_eq!(out.get("events"), Some(&OrderedJson::Int(3)));
}

// --- the spread sample ------------------------------------------------------------------------

const STOCKS: [(&str, [&str; 3], &str); 3] = [
    ("ZZSTEADY", ["500", "500", "500"], "808.00"),
    ("ZZSPIKE", ["1", "1", "90000"], "150.00"),
    ("ZZMID", ["200", "200", "200"], "95.00"),
];

fn sessions() -> [NaiveDate; 3] {
    [d(2027, 11, 1), d(2027, 11, 2), d(2027, 11, 3)]
}

fn today() -> NaiveDate {
    d(2027, 11, 4)
}

fn nov() -> NaiveDate {
    d(2027, 11, 25)
}

fn contract(symbol: &str, expiry: NaiveDate, strike: &str, kind: &str) -> SampleContract {
    SampleContract {
        symbol: symbol.to_owned(),
        tradingsymbol: format!("{symbol}{}{strike}{kind}", expiry.format("%y%b%d")).to_uppercase(),
        expiry,
        strike: dec(strike),
        option_type: kind.to_owned(),
    }
}

/// `_seed` of `TestTheRun`, on a calendar cleared of the bed's own F&O rows.
async fn seed_spreads(conn: &mut PgConnection) {
    exec(conn, "TRUNCATE fo_contract_daily").await;
    exec(
        conn,
        "UPDATE op_contract SET expired = true WHERE expiry >= '2027-11-04'",
    )
    .await;
    for day in sessions() {
        super::ingest::ensure_contract_partition(conn, day)
            .await
            .unwrap();
    }
    for (i, day) in sessions().into_iter().enumerate() {
        let mut rows: Vec<(&str, &str, &str, &str)> = STOCKS
            .iter()
            .map(|(s, t, settle)| ("FUTSTK", *s, *settle, t[i]))
            .collect();
        rows.push(("FUTIDX", "NIFTY", "24310.00", "99999999"));
        rows.push(("FUTIDX", "BANKNIFTY", "55020.00", "99999999"));
        for (instrument, symbol, settle, turnover) in rows {
            sqlx::query(
                "INSERT INTO fo_contract_daily (trade_date, instrument, symbol, expiry, strike, \
                 option_type, close, settle, turnover, source_key) VALUES ($1, $2, $3, $4, 0, 'XX', \
                 $5, $5, $6, 'test/fo3')",
            )
            .bind(day)
            .bind(instrument)
            .bind(symbol)
            .bind(nov())
            .bind(dec(settle))
            .bind(dec(turnover))
            .execute(&mut *conn)
            .await
            .unwrap();
        }
    }
    let ladders: [(&str, Vec<String>); 5] = [
        (
            "ZZSTEADY",
            (700..=920).step_by(20).map(|k| k.to_string()).collect(),
        ),
        (
            "ZZSPIKE",
            (120..=180).step_by(5).map(|k| k.to_string()).collect(),
        ),
        (
            "ZZMID",
            (80..=110).step_by(2).map(|k| k.to_string()).collect(),
        ),
        (
            "NIFTY",
            (24000..=24600).step_by(50).map(|k| k.to_string()).collect(),
        ),
        (
            "BANKNIFTY",
            (54500..=55500)
                .step_by(100)
                .map(|k| k.to_string())
                .collect(),
        ),
    ];
    let mut token = 9_870_000_000_i64;
    for (symbol, ladder) in &ladders {
        let mut expiries = vec![nov(), d(2027, 12, 30)];
        if *symbol == "NIFTY" {
            expiries.push(d(2027, 11, 9));
        }
        for expiry in expiries {
            for strike in ladder {
                for kind in ["CE", "PE"] {
                    token += 1;
                    let c = contract(symbol, expiry, strike, kind);
                    sqlx::query(
                        "INSERT INTO op_contract (instrument_token, tradingsymbol, underlying, \
                         expiry, strike, option_type, lot_size, tick_size, first_seen, last_seen) \
                         VALUES ($1, $2, $3, $4, $5, $6, 75, 0.05, '2027-11-01', '2027-11-03')",
                    )
                    .bind(token)
                    .bind(&c.tradingsymbol)
                    .bind(*symbol)
                    .bind(expiry)
                    .bind(c.strike)
                    .bind(kind)
                    .execute(&mut *conn)
                    .await
                    .unwrap();
                }
            }
        }
    }
}

/// Answers every key two-sided (10.05 / 10.10) except those named one-sided; records the calls.
struct RecordingQuotes {
    kite: bool,
    one_sided: BTreeSet<String>,
    calls: Mutex<Vec<Vec<String>>>,
}

impl RecordingQuotes {
    fn new(kite: bool, one_sided: BTreeSet<String>) -> Self {
        Self {
            kite,
            one_sided,
            calls: Mutex::new(Vec::new()),
        }
    }
}

fn level(p: &str) -> Vec<DepthLevelRecord> {
    vec![
        DepthLevelRecord::new(DepthLevelFields {
            price: dec(p),
            quantity: 50,
            orders: 1,
        })
        .unwrap(),
    ]
}

impl QuoteSource for RecordingQuotes {
    fn kite_ok(&self) -> bool {
        self.kite
    }

    async fn option_quotes(&self, keys: &[String]) -> Result<Vec<OptionQuoteRecord>, WorkerError> {
        assert!(self.kite, "a Kite read with no Kite session");
        self.calls.lock().unwrap().push(keys.to_vec());
        Ok(keys
            .iter()
            .map(|k| {
                OptionQuoteRecord::new(OptionQuoteFields {
                    symbol: k.trim_start_matches("NFO:").to_owned(),
                    exchange: "NFO".to_owned(),
                    instrument_token: None,
                    last_price: None,
                    volume: 0,
                    oi: Some(1200),
                    oi_day_high: None,
                    oi_day_low: None,
                    bids: level("10.05"),
                    asks: if self.one_sided.contains(k) {
                        Vec::new()
                    } else {
                        level("10.10")
                    },
                    as_of: None,
                    last_trade_time: None,
                })
                .unwrap()
            })
            .collect())
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs BASKFY_RS_FNO_DB_URL"]
async fn one_call_over_the_near_monthly_atm_window_and_append_only() {
    let (_serial, mut conn) = clone().await;
    seed_spreads(&mut conn).await;
    let at_1500 = at(today(), 15, 0);
    let reader = RecordingQuotes::new(true, BTreeSet::new());
    let report = run_spread_sample(&mut conn, at_1500, &reader, None)
        .await
        .unwrap();
    assert_eq!(report.skipped, None);
    assert_eq!(report.reference_session, Some(sessions()[2]));
    assert_eq!(
        report.underlyings,
        ["ZZSTEADY", "ZZMID", "ZZSPIKE", "NIFTY", "BANKNIFTY"],
        "median turnover: one 90,000 day does not lift ZZSPIKE"
    );
    let calls = reader.calls.lock().unwrap().clone();
    assert_eq!((calls.len(), report.calls), (1, 1));
    assert_eq!(calls[0].len(), 5 * 14);
    assert!(calls[0].iter().all(|k| k.starts_with("NFO:")));
    let stored: Vec<(String, NaiveDate, Decimal, Option<Decimal>, NaiveDate)> = sqlx::query_as(
        "SELECT symbol, expiry, strike, mid, trade_date FROM fo_spread_sample WHERE taken_at = $1",
    )
    .bind(at_1500)
    .fetch_all(&mut conn)
    .await
    .unwrap();
    assert_eq!(stored.len(), 70);
    assert_eq!(report.inserted, 70);
    assert!(
        stored.iter().all(|r| r.1 == nov()),
        "the near monthly, never a weekly"
    );
    let steady: BTreeSet<Decimal> = stored
        .iter()
        .filter(|r| r.0 == "ZZSTEADY")
        .map(|r| r.2.normalize())
        .collect();
    let want: BTreeSet<Decimal> = [740, 760, 780, 800, 820, 840, 860]
        .into_iter()
        .map(Decimal::from)
        .collect();
    assert_eq!(steady, want);
    let nifty: Vec<Decimal> = stored
        .iter()
        .filter(|r| r.0 == "NIFTY")
        .map(|r| r.2.normalize())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    assert_eq!(nifty[3], Decimal::from(24300));
    assert!(
        stored
            .iter()
            .all(|r| r.3 == Some(dec("10.08")) && r.4 == today())
    );
    let again = run_spread_sample(
        &mut conn,
        at_1500,
        &RecordingQuotes::new(true, BTreeSet::new()),
        None,
    )
    .await
    .unwrap();
    assert_eq!(
        (again.rows, again.inserted),
        (70, 0),
        "append-only (house rule 7)"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs BASKFY_RS_FNO_DB_URL"]
async fn no_kite_session_makes_no_call_and_one_sided_rows_are_kept() {
    let (_serial, mut conn) = clone().await;
    seed_spreads(&mut conn).await;
    let at_1500 = at(today(), 15, 0);
    let report = run_spread_sample(
        &mut conn,
        at_1500,
        &RecordingQuotes::new(false, BTreeSet::new()),
        None,
    )
    .await
    .unwrap();
    assert_eq!(report.skipped.as_deref(), Some("no usable Kite session"));
    assert_eq!((report.calls, report.inserted), (0, 0));
    let one_sided: BTreeSet<String> = [740, 760]
        .iter()
        .map(|k| contract("ZZSTEADY", nov(), &k.to_string(), "CE").key())
        .collect();
    let report = run_spread_sample(
        &mut conn,
        at_1500,
        &RecordingQuotes::new(true, one_sided),
        None,
    )
    .await
    .unwrap();
    assert_eq!(report.one_sided, 2);
    let nulls: (i64,) = sqlx::query_as(
        "SELECT count(*) FROM fo_spread_sample WHERE taken_at = $1 AND ask IS NULL AND mid IS NULL \
         AND bid IS NOT NULL",
    )
    .bind(at_1500)
    .fetch_one(&mut conn)
    .await
    .unwrap();
    assert_eq!(nulls.0, 2);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs BASKFY_RS_FNO_DB_URL"]
async fn before_any_ingest_it_says_so_and_calls_nothing() {
    let (_serial, mut conn) = clone().await;
    exec(&mut conn, "TRUNCATE fo_contract_daily").await;
    let reader = RecordingQuotes::new(true, BTreeSet::new());
    let report = run_spread_sample(&mut conn, at(today(), 15, 0), &reader, None)
        .await
        .unwrap();
    assert!(
        report
            .skipped
            .as_deref()
            .unwrap_or("")
            .contains("FO2 has not ingested")
    );
    assert!(reader.calls.lock().unwrap().is_empty());
}

// --- the index series -------------------------------------------------------------------------

/// `FakeDaily`: one candle per weekday in the window, prices keyed on the token.
struct FakeDaily {
    revise: i64,
    calls: Mutex<Vec<(i64, NaiveDate, NaiveDate)>>,
}

impl FakeDaily {
    fn new(revise: i64) -> Self {
        Self {
            revise,
            calls: Mutex::new(Vec::new()),
        }
    }
}

fn dec_col(name: &str, values: &[Decimal]) -> polars::prelude::Column {
    Int128Chunked::from_iter_options(
        name.into(),
        values.iter().map(|v| {
            let mut x = *v;
            x.rescale(4);
            Some(x.mantissa())
        }),
    )
    .into_decimal_unchecked(18, 4)
    .into_series()
    .into_column()
}

impl DailyBarReader for FakeDaily {
    async fn daily_bars(
        &self,
        token: i64,
        start: NaiveDate,
        end: NaiveDate,
    ) -> Result<DataFrame, ProviderError> {
        self.calls.lock().unwrap().push((token, start, end));
        let days: Vec<NaiveDate> = start
            .iter_days()
            .take_while(|x| *x <= end)
            .filter(|x| x.weekday().num_days_from_monday() < 5)
            .collect();
        let base = Decimal::from(token % 1000);
        let n = days.len();
        let each = |f: &dyn Fn() -> Decimal| vec![f(); n];
        Ok(DataFrame::new(
            n,
            vec![
                Series::new("symbol".into(), vec![token.to_string(); n]).into_column(),
                Series::new("date".into(), days).into_column(),
                dec_col("open", &each(&|| base + Decimal::ONE)),
                dec_col("high", &each(&|| base + dec("2.125"))),
                dec_col("low", &each(&|| base - Decimal::ONE)),
                dec_col(
                    "close",
                    &each(&|| base + dec("0.5") + Decimal::from(self.revise)),
                ),
                Series::new("volume".into(), vec![0_i64; n]).into_column(),
                Series::new("source".into(), vec!["kite"; n]).into_column(),
            ],
        )
        .unwrap())
    }
}

async fn index_rows(
    conn: &mut PgConnection,
    underlying: &str,
) -> Vec<(NaiveDate, Decimal, Decimal, String)> {
    sqlx::query_as(
        "SELECT trade_date, high, close, source FROM fo_index_daily WHERE underlying = $1 \
         ORDER BY trade_date",
    )
    .bind(underlying)
    .fetch_all(&mut *conn)
    .await
    .unwrap()
}

/// The bed's two index instruments carry Kite's tokens; its own series is cleared.
async fn indices(conn: &mut PgConnection, bank: bool) {
    exec(conn, "DELETE FROM fo_index_daily").await;
    exec(conn, "UPDATE instrument SET kite_token = 256265, instrument_type = 'INDEX' WHERE symbol = 'NIFTY 50'").await;
    if bank {
        exec(conn, "UPDATE instrument SET kite_token = 260105, instrument_type = 'INDEX' WHERE symbol = 'NIFTY BANK'").await;
    } else {
        exec(
            conn,
            "UPDATE instrument SET kite_token = NULL WHERE symbol = 'NIFTY BANK'",
        )
        .await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs BASKFY_RS_FNO_DB_URL"]
async fn daily_backfill_writes_both_indices_rounded_and_idempotent() {
    let (_serial, mut conn) = clone().await;
    indices(&mut conn, true).await;
    let reader = FakeDaily::new(0);
    let (start, end) = (d(2026, 9, 1), d(2026, 9, 25));
    let first = index_daily::backfill(&mut conn, &reader, start, end)
        .await
        .unwrap();
    let again = index_daily::backfill(&mut conn, &reader, start, end)
        .await
        .unwrap();
    assert_eq!(first.calls, 2);
    assert!(first.skipped.is_empty());
    let written: BTreeSet<&str> = first.written.iter().map(|(k, _)| k.as_str()).collect();
    assert_eq!(written, BTreeSet::from(["NIFTY", "BANKNIFTY"]));
    let rows = index_rows(&mut conn, "NIFTY").await;
    assert_eq!(rows.len(), 19);
    assert_eq!(rows[0].1, dec("267.13"), "rounded to the paisa at write");
    assert_eq!(rows[18].0, end);
    assert!(first.latest.contains(&("NIFTY".to_owned(), Some(end))));
    assert_eq!(again.written, first.written);
    assert_eq!(index_rows(&mut conn, "NIFTY").await.len(), 19);
    assert!(rows.iter().all(|r| r.3 == "KITE_HIST"));
    let bars = index_daily::daily_bars(&mut conn, "NIFTY", d(2026, 9, 23), 5)
        .await
        .unwrap();
    assert_eq!(
        bars.iter().map(|b| b.date).collect::<Vec<_>>(),
        [
            d(2026, 9, 17),
            d(2026, 9, 18),
            d(2026, 9, 21),
            d(2026, 9, 22),
            d(2026, 9, 23)
        ]
    );
    assert_eq!(bars[4].close, dec("265.50"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs BASKFY_RS_FNO_DB_URL"]
async fn daily_backfill_reports_a_missing_index_row_instead_of_guessing() {
    let (_serial, mut conn) = clone().await;
    indices(&mut conn, false).await;
    let report = index_daily::backfill(
        &mut conn,
        &FakeDaily::new(0),
        d(2026, 9, 21),
        d(2026, 9, 25),
    )
    .await
    .unwrap();
    assert_eq!(
        (report.skipped, report.calls),
        (vec!["BANKNIFTY".to_owned()], 1)
    );
    assert!(index_rows(&mut conn, "BANKNIFTY").await.is_empty());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs BASKFY_RS_FNO_DB_URL"]
async fn daily_extend_re_reads_a_week_and_overwrites_a_revised_candle() {
    let (_serial, mut conn) = clone().await;
    indices(&mut conn, true).await;
    index_daily::backfill(&mut conn, &FakeDaily::new(0), d(2026, 9, 1), d(2026, 9, 18))
        .await
        .unwrap();
    let revised = FakeDaily::new(10);
    let report = index_daily::extend(&mut conn, &revised, d(2026, 9, 25))
        .await
        .unwrap();
    let starts: Vec<NaiveDate> = revised.calls.lock().unwrap().iter().map(|c| c.1).collect();
    assert_eq!(starts, [d(2026, 9, 11); 2], "a week back");
    let rows = index_rows(&mut conn, "NIFTY").await;
    assert_eq!(rows.last().unwrap().0, d(2026, 9, 25));
    let close = |on: NaiveDate| rows.iter().find(|r| r.0 == on).unwrap().2;
    assert_eq!(
        close(d(2026, 9, 10)),
        dec("265.50"),
        "before the overlap: untouched"
    );
    assert_eq!(
        close(d(2026, 9, 14)),
        dec("275.50"),
        "inside the overlap: overwritten"
    );
    assert!(
        report
            .latest
            .contains(&("NIFTY".to_owned(), Some(d(2026, 9, 25))))
    );
    exec(&mut conn, "DELETE FROM fo_index_daily").await;
    let fresh = FakeDaily::new(0);
    index_daily::extend(&mut conn, &fresh, d(2026, 9, 25))
        .await
        .unwrap();
    assert!(
        fresh
            .calls
            .lock()
            .unwrap()
            .iter()
            .all(|c| c.1 == index_daily::default_backfill_start())
    );
}

// --- the quarterly re-test's guards -----------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs BASKFY_RS_FNO_DB_URL"]
async fn the_retest_refuses_outside_its_months_and_unknown_families() {
    let (_serial, mut conn) = clone().await;
    exec(&mut conn, "DELETE FROM fo_backtest_run").await;
    let out = run_retest(&mut conn, d(2031, 8, 2), false, None)
        .await
        .unwrap();
    let Some(OrderedJson::Str(skipped)) = out.get("skipped") else {
        panic!("{out:?}")
    };
    assert!(skipped.contains("not due"), "{skipped}");
    let out = run_retest(&mut conn, d(2031, 7, 1), true, Some(vec!["B9".to_owned()]))
        .await
        .unwrap();
    let Some(OrderedJson::Str(refused)) = out.get("refused") else {
        panic!("{out:?}")
    };
    assert!(refused.contains("B9"), "{refused}");
}

// --- the night scan ---------------------------------------------------------------------------

type ScanRow = (String, String, String, Vec<String>);

async fn scan_rows(conn: &mut PgConnection, uid: i64, on: NaiveDate) -> Vec<ScanRow> {
    sqlx::query_as(
        "SELECT sleeve::text, symbol, state, reasons FROM fo_scan WHERE user_id = $1 \
         AND trade_date = $2 ORDER BY sleeve, symbol",
    )
    .bind(uid)
    .bind(on)
    .fetch_all(&mut *conn)
    .await
    .unwrap()
}

/// `test_a_day_not_ingested_writes_one_sleeve_row_with_its_reason`, and a re-run that replaces
/// a stale row and leaves the same rows (`test_a_rerun_leaves_the_same_rows_and_drops_stale_ones`).
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs BASKFY_RS_FNO_DB_URL"]
async fn a_day_not_ingested_writes_one_sleeve_row_and_a_rerun_is_identical() {
    let (_serial, mut conn) = clone().await;
    let t = d(2030, 9, 4);
    calendar(&mut conn, &[t, d(2030, 9, 5)], true).await;
    let uid = user(&mut conn, "fo4scan").await;
    sqlx::query(
        "INSERT INTO fo_sleeve_config (user_id, sleeve, capital_inr, updated_by) VALUES \
         ($1, 'F1', 2500000, 'test'), ($1, 'F2', 0, 'test')",
    )
    .bind(uid)
    .execute(&mut conn)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO fo_ingest_day (trade_date, status, attempts) VALUES ($1, 'PENDING', 1)",
    )
    .bind(t)
    .execute(&mut conn)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO fo_scan (user_id, sleeve, trade_date, symbol, state, reasons) VALUES \
         ($1, 'F2', $2, 'STALE', 'CANDIDATE', ARRAY['from an earlier run'])",
    )
    .bind(uid)
    .bind(t)
    .execute(&mut conn)
    .await
    .unwrap();
    super::scan::run_scan(&mut conn, t, Some(vec![uid]))
        .await
        .unwrap();
    let first = scan_rows(&mut conn, uid, t).await;
    let f2: Vec<&ScanRow> = first.iter().filter(|r| r.0 == "F2").collect();
    assert_eq!(f2.len(), 1, "{first:?}");
    assert_eq!(
        (f2[0].1.as_str(), f2[0].2.as_str()),
        (super::scan::SLEEVE_ROW, "NO_DATA")
    );
    assert!(
        f2[0].3[0].contains("not ingested (PENDING)"),
        "{:?}",
        f2[0].3
    );
    let nifty = first
        .iter()
        .find(|r| r.0 == "F1N" && r.1 == "NIFTY")
        .unwrap();
    assert_eq!(nifty.2, "NO_DATA");
    assert!(
        nifty.3[0].contains("does not reach the entry session"),
        "{nifty:?}"
    );
    super::scan::run_scan(&mut conn, t, Some(vec![uid]))
        .await
        .unwrap();
    assert_eq!(scan_rows(&mut conn, uid, t).await, first);
}
