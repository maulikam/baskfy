#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use baskfy_db::OrderedJson;
use baskfy_providers::records::{QuoteFields, QuoteRecord};
use chrono::{DateTime, Duration, NaiveDate, Utc};
use rust_decimal::Decimal;
use sqlx::{Connection, PgConnection, Row as _};

use super::live::{
    PlanBuild, build_live_plan_twt, last_factors, live_names, provisional_daily_bars,
    run_twt_live_on,
};
use super::scan::{REASON_MARKET_OPEN, ScanDecision, claim_run, decide_session};
use crate::steps::sleeves::for_twt_jobs::{load_twt_config, load_twt_universe};

const IGNORE: &str =
    "needs BASKFY_RS_TWT_CLONE_URL (an rst_* clone of the bed): run with --ignored";

const USER: i64 = 1;

static SERIAL: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

fn day(d: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 10, d).unwrap()
}

fn scan_now() -> DateTime<Utc> {
    day(30).and_hms_opt(7, 45, 20).unwrap().and_utc()
}

async fn clone() -> PgConnection {
    let url = std::env::var("BASKFY_RS_TWT_CLONE_URL").unwrap_or_else(|_| panic!("{IGNORE}"));
    let database = url
        .rsplit('/')
        .next()
        .unwrap_or("")
        .split('?')
        .next()
        .unwrap_or("");
    assert!(
        database.starts_with("rst_"),
        "refusing the database {database:?}: only an rst_* clone"
    );
    PgConnection::connect(&url).await.unwrap()
}

fn case_seed(name: &str) -> String {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../tools/parity/task_cases/twt_jobs.json");
    let text = std::fs::read_to_string(path).unwrap();
    let file: serde_json::Value = serde_json::from_str(&text).unwrap();
    file["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == name)
        .and_then(|c| c["seed_sql"].as_str())
        .unwrap_or_else(|| panic!("no case {name}"))
        .to_owned()
}

async fn exec(conn: &mut PgConnection, sql: &str) {
    sqlx::raw_sql(sql).execute(&mut *conn).await.unwrap();
}

async fn id_of(conn: &mut PgConnection, symbol: &str) -> i64 {
    sqlx::query_scalar("SELECT id FROM instrument WHERE symbol = $1")
        .bind(symbol)
        .fetch_one(&mut *conn)
        .await
        .unwrap()
}

async fn provisional_bars(
    conn: &mut PgConnection,
    overrides: &[(&str, Decimal)],
) -> super::live::BarBuild {
    let config = load_twt_config(conn, USER).await.unwrap();
    let universe = load_twt_universe(conn, &config).await.unwrap();
    let names = live_names(conn, &universe).await.unwrap();
    let ids: Vec<i64> = names.iter().map(|n| n.instrument_id).collect();
    let factors = last_factors(conn, day(29), &ids).await.unwrap();
    let rows = sqlx::query(
        "SELECT i.symbol, o.close_raw, o.volume FROM ohlcv_daily o \
         JOIN instrument i ON i.id = o.instrument_id \
         WHERE o.date = $1 AND o.instrument_id = ANY($2)",
    )
    .bind(day(29))
    .bind(&ids)
    .fetch_all(&mut *conn)
    .await
    .unwrap();
    let quotes: Vec<QuoteRecord> = rows
        .iter()
        .map(|row| {
            let symbol: String = row.get("symbol");
            let last = overrides
                .iter()
                .find(|(s, _)| *s == symbol)
                .map_or_else(|| row.get::<Decimal, _>("close_raw"), |(_, p)| *p);
            let mut fields = QuoteFields::new(&symbol, last);
            fields.volume = row.get::<Option<i64>, _>("volume").unwrap_or(0);
            QuoteRecord::new(fields).unwrap()
        })
        .collect();
    provisional_daily_bars(&names, &quotes, &factors, day(30)).unwrap()
}

fn decision() -> ScanDecision {
    ScanDecision {
        session_date: day(30),
        provisional: true,
        published_as_of: day(29),
        reason: REASON_MARKET_OPEN,
    }
}

fn field<'a>(value: &'a OrderedJson, key: &str) -> &'a OrderedJson {
    let OrderedJson::Object(entries) = value else {
        panic!("not an object: {value:?}");
    };
    entries
        .iter()
        .find(|(k, _)| k == key)
        .map(|(_, v)| v)
        .unwrap_or_else(|| panic!("no {key}"))
}

fn seeded_quotes() -> [(&'static str, Decimal); 2] {
    [
        ("DCMSRIND", Decimal::new(6060, 2)),
        ("NILKAMAL", Decimal::new(191_960, 2)),
    ]
}

async fn live_scan(conn: &mut PgConnection, case: &str) -> OrderedJson {
    exec(conn, &case_seed(case)).await;
    let build = provisional_bars(conn, &seeded_quotes()).await;
    assert!(build.frame.height() > 1000, "a session, not a thin one");
    run_twt_live_on(conn, USER, &decision(), &build, scan_now(), false)
        .await
        .unwrap()
}

async fn signal_state(conn: &mut PgConnection, symbol: &str) -> Option<(String, bool)> {
    let id = id_of(conn, symbol).await;
    sqlx::query_as(
        "SELECT state, provisional FROM tw_signal_daily \
         WHERE user_id = $1 AND date = $2 AND instrument_id = $3",
    )
    .bind(USER)
    .bind(day(30))
    .bind(id)
    .fetch_optional(&mut *conn)
    .await
    .unwrap()
}

async fn count(conn: &mut PgConnection, sql: &str) -> i64 {
    sqlx::query_scalar(sql).fetch_one(&mut *conn).await.unwrap()
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_TWT_CLONE_URL"]
async fn the_seeded_provisional_bar_signals_twice_and_a_shut_gate_plans_nothing() {
    let _serial = SERIAL.lock().await;
    let mut conn = clone().await;
    let mut tx = conn.begin().await.unwrap();
    let positions_before = count(
        &mut tx,
        "SELECT count(*) FROM tw_position WHERE updated_at > now() - interval '1 hour'",
    )
    .await;
    let report = live_scan(&mut tx, "scan-live-session-signals").await;
    assert_eq!(field(&report, "provisional"), &OrderedJson::Bool(true));
    assert_eq!(field(&report, "plan_id"), &OrderedJson::Null);
    assert_eq!(field(&report, "plan_kept"), &OrderedJson::Bool(false));
    let OrderedJson::Int(signals) = field(&report, "signals") else {
        panic!("signals is an int");
    };
    assert!(*signals >= 2, "the two seeded names signal: {signals}");
    for symbol in ["DCMSRIND", "NILKAMAL"] {
        assert_eq!(
            signal_state(&mut tx, symbol).await,
            Some(("SIGNAL".to_owned(), true)),
            "{symbol}"
        );
    }
    let (gate, provisional): (String, bool) = sqlx::query_as(
        "SELECT gate, provisional FROM tw_breadth_daily WHERE user_id = $1 AND date = $2",
    )
    .bind(USER)
    .bind(day(30))
    .fetch_one(&mut *tx)
    .await
    .unwrap();
    assert_eq!((gate.as_str(), provisional), ("SHUT", true));
    assert_eq!(
        count(
            &mut tx,
            "SELECT count(*) FROM tw_plan WHERE session_date = '2026-10-30'"
        )
        .await,
        0
    );
    assert_eq!(
        count(
            &mut tx,
            "SELECT count(*) FROM tw_position WHERE updated_at > now() - interval '1 hour'",
        )
        .await,
        positions_before
    );
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_TWT_CLONE_URL"]
async fn the_bed_bar_alone_does_not_signal_the_seeded_names() {
    let _serial = SERIAL.lock().await;
    let mut conn = clone().await;
    let mut tx = conn.begin().await.unwrap();
    exec(&mut tx, &case_seed("scan-live-session-quotes")).await;
    let build = provisional_bars(&mut tx, &seeded_quotes()).await;
    run_twt_live_on(&mut tx, USER, &decision(), &build, scan_now(), false)
        .await
        .unwrap();
    for symbol in ["DCMSRIND", "NILKAMAL"] {
        assert_eq!(signal_state(&mut tx, symbol).await, None, "{symbol}");
    }
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_TWT_CLONE_URL"]
async fn an_open_gate_stores_a_live_plan_of_proposed_buys_expiring_in_thirty_minutes() {
    let _serial = SERIAL.lock().await;
    let mut conn = clone().await;
    let mut tx = conn.begin().await.unwrap();
    let report = live_scan(&mut tx, "scan-live-session-signals-gate-open").await;
    let OrderedJson::Str(plan_id) = field(&report, "plan_id").clone() else {
        panic!("a plan: {report:?}");
    };
    let (source, built_at, expires_at, gate): (String, DateTime<Utc>, DateTime<Utc>, String) =
        sqlx::query_as(
            "SELECT source, built_at, expires_at, gate FROM tw_plan WHERE plan_id::text = $1",
        )
        .bind(&plan_id)
        .fetch_one(&mut *tx)
        .await
        .unwrap();
    assert_eq!((source.as_str(), gate.as_str()), ("LIVE", "OPEN"));
    assert_eq!(built_at, scan_now());
    assert_eq!(expires_at, scan_now() + Duration::minutes(30));
    let lines: Vec<(String, String, Option<String>, i32)> = sqlx::query_as(
        "SELECT l.kind, l.state, l.note, l.quantity FROM tw_plan_line l \
         JOIN tw_plan p ON p.id = l.plan_id WHERE p.plan_id::text = $1",
    )
    .bind(&plan_id)
    .fetch_all(&mut *tx)
    .await
    .unwrap();
    assert!(!lines.is_empty());
    for (kind, state, note, quantity) in &lines {
        assert_eq!((kind.as_str(), state.as_str()), ("BUY_AT_OPEN", "PROPOSED"));
        assert!(
            note.as_deref()
                .is_some_and(|n| n.starts_with("live scan 13:15: buy now, at market; ")),
            "{note:?}"
        );
        assert!(*quantity > 0);
    }
    let OrderedJson::Int(entries) = field(&report, "entries") else {
        panic!("entries is an int");
    };
    assert!(*entries >= 1);
    assert_eq!(
        count(
            &mut tx,
            "SELECT count(*) FROM tw_order WHERE signal_date = '2026-10-30'"
        )
        .await,
        0,
        "a live scan sends nothing"
    );
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_TWT_CLONE_URL"]
async fn a_rescan_keeps_a_live_plan_whose_line_is_at_the_broker() {
    let _serial = SERIAL.lock().await;
    let mut conn = clone().await;
    let mut tx = conn.begin().await.unwrap();
    let report = live_scan(&mut tx, "scan-live-session-signals-gate-open").await;
    let OrderedJson::Str(first) = field(&report, "plan_id").clone() else {
        panic!("a plan");
    };
    exec(
        &mut tx,
        &format!(
            "UPDATE tw_plan_line SET state = 'SENT' WHERE id = (SELECT min(l.id) FROM \
             tw_plan_line l JOIN tw_plan p ON p.id = l.plan_id WHERE p.plan_id::text = '{first}')"
        ),
    )
    .await;
    let later = scan_now() + Duration::minutes(5);
    let again = build_live_plan_twt(&mut tx, USER, day(30), false, later)
        .await
        .unwrap();
    assert_eq!(
        again,
        PlanBuild {
            plan_id: Some(first),
            entries: 0,
            kept: true,
            already_ordered: 0
        }
    );
    assert_eq!(
        count(
            &mut tx,
            "SELECT count(*) FROM tw_plan WHERE session_date = '2026-10-30' AND source = 'LIVE'"
        )
        .await,
        1
    );
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_TWT_CLONE_URL"]
async fn a_name_with_an_order_today_is_not_offered_again() {
    let _serial = SERIAL.lock().await;
    let mut conn = clone().await;
    let mut tx = conn.begin().await.unwrap();
    exec(&mut tx, &case_seed("scan-live-session-signals-gate-open")).await;
    let build = provisional_bars(&mut tx, &seeded_quotes()).await;
    let mut detect = crate::steps::StepOutcome::default();
    crate::steps::sleeves::for_twt_jobs::run_detect_twt_for(
        &mut tx,
        &mut detect,
        day(30),
        USER,
        None,
        Some(&build.frame),
        true,
    )
    .await
    .unwrap();
    let candidates = count(
        &mut tx,
        "SELECT count(*) FROM tw_signal_daily WHERE user_id = 1 AND date = '2026-10-30' \
         AND state = 'SIGNAL'",
    )
    .await;
    exec(
        &mut tx,
        "INSERT INTO tw_order (user_id, instrument_id, signal_date, side, quantity, stop_price, \
         state, filled_quantity, simulated, client_id) \
         SELECT 1, instrument_id, date, 'BUY', 10, 1.00, 'SENT', 0, false, 'earlier:' || \
         instrument_id FROM tw_signal_daily WHERE user_id = 1 AND date = '2026-10-30' \
         AND state = 'SIGNAL'",
    )
    .await;
    let built = build_live_plan_twt(&mut tx, USER, day(30), false, scan_now())
        .await
        .unwrap();
    assert_eq!(
        (built.plan_id, built.entries, built.already_ordered),
        (None, 0, candidates)
    );
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_TWT_CLONE_URL"]
async fn no_signal_means_no_live_plan() {
    let _serial = SERIAL.lock().await;
    let mut conn = clone().await;
    let mut tx = conn.begin().await.unwrap();
    let built = build_live_plan_twt(&mut tx, USER, day(31), false, scan_now())
        .await
        .unwrap();
    assert_eq!(built, PlanBuild::default());
    assert_eq!(
        count(
            &mut tx,
            "SELECT count(*) FROM tw_plan WHERE session_date = '2026-10-31'"
        )
        .await,
        0
    );
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_TWT_CLONE_URL"]
async fn a_queued_row_is_claimed_once_and_an_unknown_id_answers_none() {
    let _serial = SERIAL.lock().await;
    let mut conn = clone().await;
    let mut tx = conn.begin().await.unwrap();
    let id: i64 = sqlx::query_scalar(
        "INSERT INTO tw_scan_run (user_id, requested_at, status, source) \
         VALUES (1, now(), 'QUEUED', 'desk') RETURNING id",
    )
    .fetch_one(&mut *tx)
    .await
    .unwrap();
    assert!(claim_run(&mut tx, id, scan_now()).await.unwrap().is_some());
    let (status, started_at): (String, Option<DateTime<Utc>>) =
        sqlx::query_as("SELECT status, started_at FROM tw_scan_run WHERE id = $1")
            .bind(id)
            .fetch_one(&mut *tx)
            .await
            .unwrap();
    assert_eq!((status.as_str(), started_at), ("RUNNING", Some(scan_now())));
    assert!(claim_run(&mut tx, id, scan_now()).await.unwrap().is_none());
    assert!(
        claim_run(&mut tx, 999_999_999, scan_now())
            .await
            .unwrap()
            .is_none()
    );
    tx.rollback().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_TWT_CLONE_URL"]
async fn a_box_that_has_never_published_is_not_runnable_and_in_session_today_is_provisional() {
    let _serial = SERIAL.lock().await;
    let mut conn = clone().await;
    let mut tx = conn.begin().await.unwrap();
    let at = |d: u32, h: u32, m: u32| day(d).and_hms_opt(h, m, 0).unwrap();
    let live = decide_session(&mut tx, at(30, 13, 15))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(live, decision());
    let before = decide_session(&mut tx, at(30, 8, 30))
        .await
        .unwrap()
        .unwrap();
    assert_eq!((before.session_date, before.provisional), (day(29), false));
    exec(&mut tx, "UPDATE pipeline_run SET data_version = NULL").await;
    let refused = decide_session(&mut tx, at(30, 13, 15)).await.unwrap();
    assert!(refused.is_err_and(|reason| reason.contains("no published bars")));
    tx.rollback().await.unwrap();
}
