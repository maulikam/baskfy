//! `tests/test_autorun.py`, the equity half (the strangle cases need the frozen lab and are
//! skipped in Python too unless it is thawed).

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

use chrono::NaiveDate;

use super::*;

fn today() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 19).unwrap()
}

fn at(h: u32, m: u32) -> NaiveDateTime {
    today().and_hms_opt(h, m, 0).unwrap()
}

async fn conn() -> (tempfile::TempDir, Conn) {
    let dir = tempfile::tempdir().unwrap();
    let mut c = crate::storage::Db::connect_sqlite_path(&dir.path().join("p.db"))
        .await
        .unwrap();
    c.migrate().await.unwrap();
    (dir, c)
}

const FULL: &[[&str; 3]] = &[
    ["index history", "ok", ""],
    ["benchmark PRI", "ok", ""],
    ["EOD snapshot", "ok", ""],
    ["trade capture", "ok", ""],
    ["breadth", "skipped", "no --scan given"],
    ["regime preview", "ok", ""],
];

fn pre_close() -> Vec<[String; 3]> {
    FULL.iter()
        .map(|s| {
            if s[0] == "EOD snapshot" {
                [
                    s[0].to_owned(),
                    "skipped".to_owned(),
                    "it is 09:30, before the 15:30 close".to_owned(),
                ]
            } else {
                [s[0].to_owned(), s[1].to_owned(), s[2].to_owned()]
            }
        })
        .collect()
}

fn full() -> Vec<[String; 3]> {
    FULL.iter().map(|s| s.map(str::to_owned)).collect()
}

fn only_run() -> Vec<[String; 3]> {
    let mut out = vec![FULL[0].map(str::to_owned)];
    for s in &FULL[1..] {
        out.push([
            s[0].to_owned(),
            "skipped".to_owned(),
            "not selected by --only".to_owned(),
        ]);
    }
    out
}

async fn record_daily(c: &mut Conn, steps: &[[String; 3]], outcome: &str, day: NaiveDate) {
    let iso = day.format("%Y-%m-%d").to_string();
    c.execute(
        "INSERT INTO daily_runs(ran_at, session_date, trigger, outcome, failed_steps, steps_json, duration_s) VALUES(?,?,?,?,?,?,?)",
        &[
            Param::Text(format!("{iso}T09:30:00")),
            Param::Text(iso),
            Param::Text("page".to_owned()),
            Param::Text(outcome.to_owned()),
            Param::Int(0),
            Param::Text(serde_json::to_string(steps).unwrap()),
            Param::Float(1.0),
        ],
    )
    .await
    .unwrap();
}

fn ops(items: &[PyDict]) -> Vec<String> {
    items
        .iter()
        .map(|i| i.get("op").map(PyValue::py_str).unwrap_or_default())
        .collect()
}

async fn ops_at(c: &mut Conn, h: u32) -> Vec<String> {
    ops(&needed(c, at(h, 0), true, false).await.unwrap())
}

#[tokio::test]
async fn nothing_is_due_on_a_non_trading_day() {
    let (_d, mut c) = conn().await;
    assert!(
        needed(&mut c, at(10, 0), false, false)
            .await
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn before_the_options_open_the_straddle_is_not_due() {
    let (_d, mut c) = conn().await;
    record_daily(&mut c, &pre_close(), "ok", today()).await;
    assert!(
        !ops(&needed(&mut c, at(8, 45), true, true).await.unwrap())
            .contains(&"strangle_collect".to_owned())
    );
}

#[tokio::test]
async fn a_day_with_no_run_needs_one() {
    let (_d, mut c) = conn().await;
    assert!(ops_at(&mut c, 10).await.contains(&"daily".to_owned()));
}

#[tokio::test]
async fn a_partial_only_run_does_not_count_as_collected() {
    let (_d, mut c) = conn().await;
    record_daily(&mut c, &only_run(), "ok", today()).await;
    assert!(ops_at(&mut c, 10).await.contains(&"daily".to_owned()));
}

#[tokio::test]
async fn before_the_close_a_skipped_snapshot_still_counts() {
    let (_d, mut c) = conn().await;
    record_daily(&mut c, &pre_close(), "ok", today()).await;
    assert!(!ops_at(&mut c, 11).await.contains(&"daily".to_owned()));
}

#[tokio::test]
async fn after_the_close_a_missing_snapshot_is_outstanding() {
    let (_d, mut c) = conn().await;
    record_daily(&mut c, &pre_close(), "ok", today()).await;
    let items = needed(&mut c, at(16, 0), true, false).await.unwrap();
    assert_eq!(ops(&items), vec!["daily"]);
    assert!(
        items[0]
            .get("why")
            .is_some_and(|w| w.py_str().contains("cannot be backfilled"))
    );
    assert_eq!(items[0].get("note"), Some(&PyValue::Str(String::new())));
}

#[tokio::test]
async fn after_the_close_a_landed_snapshot_settles_the_day() {
    let (_d, mut c) = conn().await;
    record_daily(&mut c, &full(), "ok", today()).await;
    assert!(!ops_at(&mut c, 16).await.contains(&"daily".to_owned()));
}

#[tokio::test]
async fn a_failed_run_does_not_count() {
    let (_d, mut c) = conn().await;
    record_daily(&mut c, &full(), "auth", today()).await;
    assert!(ops_at(&mut c, 16).await.contains(&"daily".to_owned()));
}

#[tokio::test]
async fn yesterdays_run_does_not_settle_today() {
    let (_d, mut c) = conn().await;
    record_daily(
        &mut c,
        &full(),
        "ok",
        NaiveDate::from_ymd_opt(2026, 8, 18).unwrap(),
    )
    .await;
    assert!(ops_at(&mut c, 16).await.contains(&"daily".to_owned()));
}

#[tokio::test]
async fn the_frozen_lab_offers_no_options_items() {
    let (_d, mut c) = conn().await;
    record_daily(&mut c, &full(), "ok", today()).await;
    assert!(
        needed(&mut c, at(10, 0), true, true)
            .await
            .unwrap()
            .is_empty()
    );
}

#[test]
fn the_summary_names_what_is_outstanding() {
    assert_eq!(
        summary(&[]),
        "Nothing outstanding — today is already collected."
    );
    let item = dict([("label", PyValue::Str("Daily collection".to_owned()))]);
    assert_eq!(summary(&[item]), "1 outstanding: Daily collection");
}

#[test]
fn the_decision_module_runs_nothing() {
    let src = include_str!("../autorun.rs");
    for token in ["Command", "spawn", "gateway", "place_order"] {
        assert!(!src.contains(token), "{token}");
    }
}
