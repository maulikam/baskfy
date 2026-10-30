//! The volume-breakout desk's store, page and law, ported from `tests/test_vbt_desk.py`
//! (`TestTheStoreRoundTrips`, `TestTheView`, `TestTheLaw`, R8.VB1's lock and R8.VB2's read) and
//! `tests/test_vbt_rescan_desk.py` (`TestTheRowItWrites`, `TestTheTwoRefusals`,
//! `TestTheDeskAndTheWorkerAgree`, `TestThePage`).
//!
//! **The database.** As in Python: the `vb_` tables are built from the same DDL (it mirrors
//! `alembic/versions/0037_vbt.py` for the columns the store reads, in SQLite's spelling) on a
//! per-test file, and the real [`PgVbtStore`] runs the SQL it runs on Postgres with the schema
//! prefix `""`. Money is written as text: the SQLite binder refuses a `Decimal` (R8.2), as
//! Python's sqlite3 does without its adapter.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::{Path, PathBuf};
use std::str::FromStr;

use baskfy_desk::lanes::twt::common::{Rec, Val};
use baskfy_desk::lanes::vbt::store::{
    self as st, LineUpdate, PgVbtStore, SessionBump, StoreError, VbtStore,
};
use baskfy_desk::lanes::vbt::{self as v};
use baskfy_desk::settings::Settings;
use baskfy_desk::storage::{Db, DbValue, Param};
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, TimeZone, Utc};
use rust_decimal::Decimal;

/// `DDL` in `tests/test_vbt_desk.py`, statement for statement.
pub const DDL: &str = "
CREATE TABLE instrument (id INTEGER PRIMARY KEY, symbol TEXT);
CREATE TABLE vb_config (
  user_id INTEGER PRIMARY KEY, sleeve_capital_inr NUMERIC, max_open_positions INTEGER,
  max_position_pct NUMERIC, stop_pct NUMERIC, dry_run_sessions INTEGER,
  first_live_sessions_left INTEGER);
CREATE TABLE vb_breadth_daily (
  user_id INTEGER, date TEXT, measured_count INTEGER, above_count INTEGER,
  pct_above_dma NUMERIC, gate TEXT, thin_session INTEGER, PRIMARY KEY (user_id, date));
CREATE TABLE vb_plan (
  id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT, user_id INTEGER, session_date TEXT,
  source TEXT, built_at TEXT, expires_at TEXT, plan_hash TEXT, gate TEXT,
  sleeve_equity_inr NUMERIC, total_new_exposure_inr NUMERIC);
CREATE TABLE vb_plan_line (
  id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, user_id INTEGER, instrument_id INTEGER,
  kind TEXT, state TEXT, quantity INTEGER, limit_price NUMERIC, stop_price NUMERIC,
  value_inr NUMERIC, size_cap TEXT, reason TEXT, note TEXT, client_id TEXT, journal_ref TEXT,
  order_id INTEGER, position_id INTEGER);
CREATE TABLE vb_plan_skip (
  id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, user_id INTEGER, instrument_id INTEGER,
  reason TEXT, detail TEXT);
CREATE TABLE vb_position (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, broker_account_id INTEGER,
  instrument_id INTEGER, entry_date TEXT, entry_avg NUMERIC, quantity_entered INTEGER,
  quantity_open INTEGER, initial_stop NUMERIC, stop_price NUMERIC, gtt_id TEXT,
  gtt_trigger NUMERIC, gtt_armed_at TEXT, state TEXT, exit_queued_for TEXT,
  exit_reason_queued TEXT, partial_done INTEGER DEFAULT 0, trail TEXT, closed_on TEXT, exit_avg NUMERIC, close_reason TEXT,
  pnl_inr NUMERIC, return_pct NUMERIC, simulated INTEGER);
CREATE TABLE vb_order (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, broker_account_id INTEGER,
  instrument_id INTEGER, signal_date TEXT, limit_price NUMERIC, stop_price NUMERIC,
  quantity INTEGER, state TEXT, working_from TEXT, expires_after_session TEXT,
  sessions_worked INTEGER DEFAULT 0, broker_order_id TEXT, client_id TEXT,
  filled_quantity INTEGER DEFAULT 0, avg_fill_price NUMERIC, position_id INTEGER,
  cancelled_on TEXT, cancel_reason TEXT, simulated INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE vb_fill (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, position_id INTEGER, order_id INTEGER,
  side TEXT, quantity INTEGER, price NUMERIC, filled_at TEXT, journal_ref TEXT,
  simulated INTEGER);
CREATE TABLE vb_scan_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, requested_at TEXT, started_at TEXT,
  finished_at TEXT, session_date TEXT, status TEXT DEFAULT 'QUEUED', source TEXT DEFAULT 'desk',
  detail TEXT, error TEXT, task_id TEXT);
CREATE TABLE vb_session (
  user_id INTEGER, session_date TEXT, mode TEXT DEFAULT 'DRY_RUN', gate TEXT DEFAULT 'SHUT',
  signals INTEGER DEFAULT 0, confirms INTEGER DEFAULT 0, fills INTEGER DEFAULT 0,
  exits INTEGER DEFAULT 0, PRIMARY KEY (user_id, session_date));
CREATE TABLE lv_protection_issue (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, sleeve TEXT NOT NULL,
  position_id INTEGER NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL,
  seen_at TEXT NOT NULL, resolved_at TEXT);
CREATE TABLE lv_exit_order (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, sleeve TEXT NOT NULL,
  position_id INTEGER NOT NULL, line_id INTEGER, symbol TEXT NOT NULL, broker_order_id TEXT,
  client_id TEXT, quantity INTEGER NOT NULL, reference_price NUMERIC NOT NULL,
  state TEXT NOT NULL DEFAULT 'SENT', filled_quantity INTEGER NOT NULL DEFAULT 0,
  avg_fill_price NUMERIC, reason TEXT NOT NULL, simulated INTEGER NOT NULL DEFAULT 1,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
";

fn ist() -> FixedOffset {
    FixedOffset::east_opt(5 * 3600 + 1800).unwrap()
}

/// `NOW`.
fn now() -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(2026, 9, 9, 21, 20, 0).unwrap()
}

/// `SESSION`.
fn session() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 9, 9).unwrap()
}

fn d(text: &str) -> Decimal {
    Decimal::from_str(text).unwrap()
}

fn txt(s: &str) -> Param {
    Param::Text(s.to_owned())
}

/// `datetime.isoformat()` of an aware stamp, as the Python test writes it into a TEXT column.
fn iso(t: DateTime<FixedOffset>) -> String {
    t.format("%Y-%m-%dT%H:%M:%S%:z").to_string()
}

fn desk_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../kite-momentum-rebalancer")
}

fn lane_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lanes")
}

/// A fresh file with the DDL, as the `store` fixture builds it (`seed` adds the fixture's
/// instruments, config and breadth rows; `test_vbt_rescan_desk.py`'s fixture has none).
async fn fresh(seed: bool) -> (tempfile::TempDir, PgVbtStore) {
    let dir = tempfile::tempdir().unwrap();
    let mut conn = Db::connect_sqlite_path(&dir.path().join("vbt.db"))
        .await
        .unwrap();
    for stmt in DDL.split(';').map(str::trim).filter(|s| !s.is_empty()) {
        conn.execute(stmt, &[]).await.unwrap();
    }
    if seed {
        conn.execute(
            "INSERT INTO instrument (id, symbol) VALUES (42, 'VBTCO'), (43, 'OTHERCO')",
            &[],
        )
        .await
        .unwrap();
        conn.execute(
            "INSERT INTO vb_config VALUES (1, '1000000.00', 10, '12.50', '12.00', 7, 5)",
            &[],
        )
        .await
        .unwrap();
        conn.execute(
            "INSERT INTO vb_breadth_daily VALUES (1, ?, 1200, 700, '58.3333', 'OPEN', 0)",
            &[txt(&session().to_string())],
        )
        .await
        .unwrap();
    }
    (dir, PgVbtStore::new(conn, 1, "", Some(1)))
}

async fn store() -> (tempfile::TempDir, PgVbtStore) {
    fresh(true).await
}

async fn count(store: &mut PgVbtStore, sql: &str) -> i64 {
    let rows = store.conn.execute(sql, &[]).await.unwrap().rows;
    match rows[0].get("n") {
        Some(DbValue::Int(n)) => *n,
        other => panic!("{other:?}"),
    }
}

/// `a_plan(store, expires_in_minutes=25)`.
async fn a_plan(store: &mut PgVbtStore, expires_in_minutes: i64) -> (String, i64) {
    let plan_id = "8a6b5c4d-3e2f-4a1b-9c8d-7e6f5a4b3c2d".to_owned();
    store
        .conn
        .execute(
            "INSERT INTO vb_plan (plan_id, user_id, session_date, source, built_at, expires_at, \
             plan_hash, gate, sleeve_equity_inr, total_new_exposure_inr) \
             VALUES (?, 1, ?, 'EVENING', ?, ?, 'abc', 'OPEN', '1000000.00', '96000.00')",
            &[
                txt(&plan_id),
                txt(&session().to_string()),
                txt(&iso(now() - Duration::minutes(5))),
                txt(&iso(now() + Duration::minutes(expires_in_minutes))),
            ],
        )
        .await
        .unwrap();
    let rows = store
        .conn
        .execute("SELECT id FROM vb_plan", &[])
        .await
        .unwrap()
        .rows;
    let pk = match rows[0].get("id") {
        Some(DbValue::Int(n)) => *n,
        other => panic!("{other:?}"),
    };
    (plan_id, pk)
}

/// `a_line(store, plan_pk, kind="PLACE_LIMIT", **overrides)`.
async fn a_line(store: &mut PgVbtStore, plan_pk: i64, kind: &str) -> i64 {
    store
        .conn
        .execute(
            "INSERT INTO vb_plan_line (plan_id, user_id, instrument_id, kind, state, quantity, \
             limit_price, stop_price, value_inr, size_cap, note, client_id) \
             VALUES (?, 1, 42, ?, 'PROPOSED', 1000, '96.00', '84.45', '96000.00', 'SLOT', \
             'limit at the signal close; slot bound the size', ?)",
            &[
                Param::Int(plan_pk),
                txt(kind),
                txt(&format!("plan:VBTCO:{kind}")),
            ],
        )
        .await
        .unwrap();
    let rows = store
        .conn
        .execute("SELECT MAX(id) AS id FROM vb_plan_line", &[])
        .await
        .unwrap()
        .rows;
    match rows[0].get("id") {
        Some(DbValue::Int(n)) => *n,
        other => panic!("{other:?}"),
    }
}

fn position_fields(quantity: i64) -> Rec {
    Rec::of([
        ("instrument_id", Val::Int(42)),
        ("entry_date", Val::Str(session().to_string())),
        ("entry_avg", Val::from("96.00")),
        ("quantity_entered", Val::Int(quantity)),
        ("quantity_open", Val::Int(quantity)),
        ("initial_stop", Val::from("84.45")),
        ("stop_price", Val::from("84.45")),
        ("state", Val::from("OPEN")),
        ("simulated", Val::Int(1)),
    ])
}

fn order_fields(instrument_id: i64, quantity: i64, state: &str) -> Rec {
    Rec::of([
        ("instrument_id", Val::Int(instrument_id)),
        ("signal_date", Val::Str(session().to_string())),
        ("limit_price", Val::from("96.00")),
        ("stop_price", Val::from("84.45")),
        ("quantity", Val::Int(quantity)),
        ("state", Val::from(state)),
        ("simulated", Val::Int(1)),
    ])
}

fn list(view: &Rec, key: &str) -> Vec<Rec> {
    match view.val(key) {
        Val::List(items) => items
            .into_iter()
            .map(|i| match i {
                Val::Map(r) => r,
                other => panic!("{other:?}"),
            })
            .collect(),
        other => panic!("{key}: {other:?}"),
    }
}

fn map(view: &Rec, key: &str) -> Rec {
    match view.val(key) {
        Val::Map(r) => r,
        other => panic!("{key}: {other:?}"),
    }
}

fn settings() -> Settings {
    Settings::defaults().unwrap()
}

// =============================================================================================
// TestTheStoreRoundTrips
// =============================================================================================

#[tokio::test]
async fn a_plan_comes_back_with_decimals_and_aware_stamps() {
    let (_d, mut store) = store().await;
    let (plan_id, _) = a_plan(&mut store, 25).await;
    let row = store.plan(&plan_id).await.unwrap().unwrap();
    assert_eq!(row.dec("sleeve_equity_inr"), Some(d("1000000.00")));
    assert_eq!(row.date("session_date"), Some(session()));
    assert!(matches!(row.val("expires_at"), Val::Stamp(_)));
}

#[tokio::test]
async fn a_malformed_plan_id_is_none_rather_than_a_driver_error() {
    let (_d, mut store) = store().await;
    assert!(store.plan("not-a-uuid").await.unwrap().is_none());
}

#[tokio::test]
async fn a_line_carries_its_plan_and_its_levels() {
    let (_d, mut store) = store().await;
    let (plan_id, plan_pk) = a_plan(&mut store, 25).await;
    let line_id = a_line(&mut store, plan_pk, "PLACE_LIMIT").await;
    let line = store.line(line_id).await.unwrap().unwrap();
    assert_eq!(line.s("plan_id"), plan_id);
    assert_eq!(line.dec("limit_price"), Some(d("96.00")));
    assert_eq!(line.dec("stop_price"), Some(d("84.45")));
    assert_eq!(line.s("symbol"), "VBTCO");
}

#[tokio::test]
async fn setting_a_line_writes_only_what_it_was_given() {
    let (_d, mut store) = store().await;
    let (_plan_id, plan_pk) = a_plan(&mut store, 25).await;
    let line_id = a_line(&mut store, plan_pk, "PLACE_LIMIT").await;
    store
        .set_line(
            line_id,
            "SENT",
            LineUpdate {
                journal_ref: Some("ORD-1".to_owned()),
                ..LineUpdate::default()
            },
        )
        .await
        .unwrap();
    let line = store.line(line_id).await.unwrap().unwrap();
    assert_eq!(line.s("state"), "SENT");
    assert_eq!(line.s("journal_ref"), "ORD-1");
    assert!(line.s("note").starts_with("limit at the signal close"));
}

#[tokio::test]
async fn a_position_round_trips() {
    let (_d, mut store) = store().await;
    let position_id = store.create_position(position_fields(500)).await.unwrap();
    let row = store.position(position_id).await.unwrap().unwrap();
    assert_eq!(row.dec("entry_avg"), Some(d("96.00")));
    assert_eq!(row.val("simulated"), Val::Bool(true));
    assert_eq!(
        store
            .open_position_for(42)
            .await
            .unwrap()
            .unwrap()
            .int("id"),
        position_id
    );
}

#[tokio::test]
async fn an_order_round_trips_and_is_found_while_it_works() {
    let (_d, mut store) = store().await;
    let order_id = store
        .create_order(order_fields(42, 1_000, "SENT"))
        .await
        .unwrap();
    assert_eq!(
        store
            .working_order_for(42)
            .await
            .unwrap()
            .unwrap()
            .int("id"),
        order_id
    );
    store
        .update_order(order_id, Rec::of([("state", Val::from("CANCELLED"))]))
        .await
        .unwrap();
    assert!(store.working_order_for(42).await.unwrap().is_none());
}

#[tokio::test]
async fn the_session_counters_accumulate() {
    let (_d, mut store) = store().await;
    let bump = |confirms, fills| SessionBump {
        mode: "DRY_RUN".to_owned(),
        confirms,
        fills,
        exits: 0,
    };
    store.bump_session(session(), bump(1, 0)).await.unwrap();
    store.bump_session(session(), bump(1, 1)).await.unwrap();
    let row = store.session(session()).await.unwrap().unwrap();
    assert_eq!(
        (row.int("confirms"), row.int("fills"), row.s("mode")),
        (2, 1, "DRY_RUN".to_owned())
    );
}

#[tokio::test]
async fn entries_taken_counts_only_what_the_session_committed() {
    let (_d, mut store) = store().await;
    let at = Utc
        .from_utc_datetime(&session().and_hms_opt(4, 0, 0).unwrap())
        .fixed_offset();
    let mut sent = order_fields(42, 10, "SENT");
    sent.set("created_at", Val::Stamp(at));
    store.create_order(sent).await.unwrap();
    let mut expired = order_fields(43, 10, "EXPIRED");
    expired.set("limit_price", Val::from("50.00"));
    expired.set("stop_price", Val::from("44.00"));
    expired.set("created_at", Val::Stamp(at));
    store.create_order(expired).await.unwrap();
    assert_eq!(store.entries_taken(session()).await.unwrap(), 1);
}

// =============================================================================================
// TestTheView
// =============================================================================================

#[tokio::test]
async fn the_lines_are_ordered_risk_off_before_risk_on() {
    let (_d, mut store) = store().await;
    let (_p, plan_pk) = a_plan(&mut store, 25).await;
    for kind in ["PLACE_LIMIT", "ARM_GTT", "SELL_AT_OPEN", "CANCEL_LIMIT"] {
        a_line(&mut store, plan_pk, kind).await;
    }
    let view = v::build_view(&mut store, &settings(), now()).await.unwrap();
    let kinds: Vec<String> = list(&view, "lines").iter().map(|l| l.s("kind")).collect();
    assert_eq!(
        kinds,
        vec!["SELL_AT_OPEN", "CANCEL_LIMIT", "ARM_GTT", "PLACE_LIMIT"]
    );
}

#[tokio::test]
async fn every_line_is_labelled_vbt() {
    let (_d, mut store) = store().await;
    let (_p, plan_pk) = a_plan(&mut store, 25).await;
    a_line(&mut store, plan_pk, "PLACE_LIMIT").await;
    let view = v::build_view(&mut store, &settings(), now()).await.unwrap();
    assert!(list(&view, "lines")[0].s("label").starts_with("VBT"));
}

#[tokio::test]
async fn a_proposed_line_on_a_live_plan_is_confirmable() {
    let (_d, mut store) = store().await;
    let (_p, plan_pk) = a_plan(&mut store, 25).await;
    a_line(&mut store, plan_pk, "PLACE_LIMIT").await;
    let view = v::build_view(&mut store, &settings(), now()).await.unwrap();
    assert_eq!(list(&view, "lines")[0].val("confirmable"), Val::Bool(true));
}

#[tokio::test]
async fn a_line_on_an_expired_plan_offers_no_button() {
    let (_d, mut store) = store().await;
    let (_p, plan_pk) = a_plan(&mut store, -1).await;
    a_line(&mut store, plan_pk, "PLACE_LIMIT").await;
    let view = v::build_view(&mut store, &settings(), now()).await.unwrap();
    let line = list(&view, "lines")[0].clone();
    assert_eq!(line.val("confirmable"), Val::Bool(false));
    assert_eq!(line.val("expired"), Val::Bool(true));
}

#[tokio::test]
async fn the_skips_are_on_the_page() {
    let (_d, mut store) = store().await;
    let (_p, plan_pk) = a_plan(&mut store, 25).await;
    store
        .conn
        .execute(
            "INSERT INTO vb_plan_skip (plan_id, user_id, instrument_id, reason, detail) \
             VALUES (?, 1, 43, 'SESSION_CAP', '3 new entries a session')",
            &[Param::Int(plan_pk)],
        )
        .await
        .unwrap();
    let view = v::build_view(&mut store, &settings(), now()).await.unwrap();
    let skips = list(&view, "skips");
    assert_eq!(skips.len(), 1);
    // `== [{"reason": ..., "detail": ..., "symbol": ...}]`: a dict compares by keys, not order.
    assert_eq!(skips[0].0.len(), 3, "{:?}", skips[0]);
    assert_eq!(skips[0].s("reason"), "SESSION_CAP");
    assert_eq!(skips[0].s("detail"), "3 new entries a session");
    assert_eq!(skips[0].s("symbol"), "OTHERCO");
}

#[tokio::test]
async fn a_limit_in_its_third_session_says_it_cancels_tonight() {
    let (_d, mut store) = store().await;
    let mut fields = order_fields(42, 100, "SENT");
    fields.set("sessions_worked", Val::Int(2));
    store.create_order(fields).await.unwrap();
    let view = v::build_view(&mut store, &settings(), now()).await.unwrap();
    let order = list(&view, "working")[0].clone();
    assert_eq!(order.s("sessions_text"), "2 of 3");
    assert_eq!(order.val("expires_tonight"), Val::Bool(true));
}

#[tokio::test]
async fn a_position_without_a_gtt_is_marked_naked() {
    let (_d, mut store) = store().await;
    store.create_position(position_fields(100)).await.unwrap();
    let view = v::build_view(&mut store, &settings(), now()).await.unwrap();
    assert_eq!(list(&view, "book")[0].val("naked"), Val::Bool(true));
}

#[tokio::test]
async fn the_status_bar_carries_the_gate_and_the_dry_run_count() {
    let (_d, mut store) = store().await;
    let view = v::build_view(&mut store, &settings(), now()).await.unwrap();
    assert_eq!(map(&view, "breadth").s("gate"), "OPEN");
    assert_eq!(view.int("dry_run_sessions"), 7);
    // 0 since 11 Sep 2026: the paper gate was withdrawn (DECISIONS-VB VB11.4).
    assert_eq!(view.int("dry_run_sessions_required"), 0);
    assert_eq!(view.val("execution_enabled"), Val::Bool(false));
    assert_eq!(view.val("dry_run"), Val::Bool(true));
}

#[test]
fn a_desk_with_no_tables_says_so_rather_than_five_hundred() {
    let view = v::unavailable_view("no vb_ tables on this desk", &settings(), now());
    assert_eq!(view.val("available"), Val::Bool(false));
    assert!(view.s("reason").contains("no vb_ tables"));
    assert!(list(&view, "lines").is_empty());
}

// =============================================================================================
// R8.VB1 and R8.VB2 on the store
// =============================================================================================

#[tokio::test]
async fn sqlite_commits_on_the_way_out() {
    let (dir, mut store) = store().await;
    store.lock_session_for_update(session()).await.unwrap();
    assert!(store.session_locked());
    store
        .bump_session(
            session(),
            SessionBump {
                mode: "DRY_RUN".to_owned(),
                confirms: 1,
                ..SessionBump::default()
            },
        )
        .await
        .unwrap();
    store.unlock_session().await.unwrap();
    assert!(!store.session_locked());
    let mut other = Db::connect_sqlite_path(&dir.path().join("vbt.db"))
        .await
        .unwrap();
    let rows = other
        .execute(
            "SELECT confirms FROM vb_session WHERE user_id = 1 AND session_date = ?",
            &[txt(&session().to_string())],
        )
        .await
        .unwrap()
        .rows;
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].get("confirms"), Some(&DbValue::Int(1)));
}

#[tokio::test]
async fn a_nested_lock_on_sqlite_opens_one_transaction() {
    let (_d, mut store) = store().await;
    store.lock_session_for_update(session()).await.unwrap();
    store.lock_session_for_update(session()).await.unwrap();
    store.unlock_session().await.unwrap();
    assert!(!store.session_locked());
    // A second exit has nothing to close.
    store.unlock_session().await.unwrap();
}

#[tokio::test]
async fn it_finds_an_order_in_any_state_and_only_on_its_signal() {
    let (_d, mut store) = store().await;
    let order_id = store
        .create_order(order_fields(42, 10, "CANCELLED"))
        .await
        .unwrap();
    assert_eq!(
        store.order_for_signal(42, session()).await.unwrap(),
        Some(order_id)
    );
    assert_eq!(
        store
            .order_for_signal(42, session() - Duration::days(1))
            .await
            .unwrap(),
        None
    );
    assert_eq!(store.order_for_signal(43, session()).await.unwrap(), None);
}

#[test]
fn the_busy_answer_is_pythons_word_for_word() {
    assert_eq!(
        st::SESSION_BUSY,
        "SESSION_BUSY: another confirm of this session is in flight; nothing was sent \u{2014} \
         confirm again"
    );
    assert_eq!(st::LOCK_NOT_AVAILABLE, "55P03");
    assert_eq!(st::SESSION_LOCK_KEY, "vb_session");
    let py = std::fs::read_to_string(desk_root().join("app/vbt_desk.py")).unwrap();
    assert!(py.contains("SESSION_LOCK_KEY: Final = \"vb_session\""));
    assert!(py.contains(
        "\"SESSION_BUSY: another confirm of this session is in flight; nothing was sent — \""
    ));
    assert!(py.contains("pg_try_advisory_xact_lock(hashtext(?)) AS got"));
    let rs = std::fs::read_to_string(lane_dir().join("vbt/store.rs")).unwrap();
    assert!(rs.contains("pg_try_advisory_xact_lock(hashtext(?)) AS got"));
    assert!(rs.contains("FOR UPDATE NOWAIT"));
}

// =============================================================================================
// test_vbt_rescan_desk.py — TestTheRowItWrites
// =============================================================================================

async fn scan_rows(store: &mut PgVbtStore) -> Vec<baskfy_desk::storage::Row> {
    store
        .conn
        .execute("SELECT * FROM vb_scan_run ORDER BY id", &[])
        .await
        .unwrap()
        .rows
}

fn int_of(row: &baskfy_desk::storage::Row, key: &str) -> i64 {
    match row.get(key) {
        Some(DbValue::Int(n)) => *n,
        other => panic!("{key}: {other:?}"),
    }
}

#[tokio::test]
async fn a_request_inserts_one_queued_row_for_this_user() {
    let (_d, mut store) = fresh(false).await;
    let run_id = store.request_scan(now(), None, None).await.unwrap();
    let rows = scan_rows(&mut store).await;
    assert_eq!(rows.len(), 1);
    assert_eq!(int_of(&rows[0], "id"), run_id);
    assert_eq!(
        rows[0].get("status").and_then(|v| v.as_str()),
        Some("QUEUED")
    );
    assert_eq!(rows[0].get("source").and_then(|v| v.as_str()), Some("desk"));
    assert_eq!(int_of(&rows[0], "user_id"), 1);
}

#[tokio::test]
async fn the_row_has_no_task_id_which_is_what_the_sweep_looks_for() {
    let (_d, mut store) = fresh(false).await;
    store.request_scan(now(), None, None).await.unwrap();
    assert_eq!(
        scan_rows(&mut store).await[0].get("task_id"),
        Some(&DbValue::Null)
    );
}

#[tokio::test]
async fn it_decides_no_session_because_only_the_worker_can() {
    let (_d, mut store) = fresh(false).await;
    store.request_scan(now(), None, None).await.unwrap();
    assert_eq!(
        scan_rows(&mut store).await[0].get("session_date"),
        Some(&DbValue::Null)
    );
}

#[tokio::test]
async fn newest_scan_reads_it_back() {
    let (_d, mut store) = fresh(false).await;
    let run_id = store.request_scan(now(), None, None).await.unwrap();
    let newest = store.newest_scan().await.unwrap().unwrap();
    assert_eq!(newest.int("id"), run_id);
    assert_eq!(newest.s("status"), "QUEUED");
}

#[tokio::test]
async fn newest_scan_is_none_before_the_button_has_ever_been_pressed() {
    let (_d, mut store) = fresh(false).await;
    assert!(store.newest_scan().await.unwrap().is_none());
}

#[tokio::test]
async fn it_writes_nothing_but_the_scan_row() {
    let (_d, mut store) = fresh(false).await;
    store.request_scan(now(), None, None).await.unwrap();
    for table in [
        "vb_plan",
        "vb_plan_line",
        "vb_order",
        "vb_position",
        "vb_fill",
    ] {
        let n = count(&mut store, &format!("SELECT COUNT(*) AS n FROM {table}")).await;
        assert_eq!(n, 0, "a re-detect wrote to {table}");
    }
}

#[tokio::test]
async fn the_newest_of_several_is_the_one_returned() {
    let (_d, mut store) = fresh(false).await;
    store
        .request_scan(now() - Duration::hours(2), None, None)
        .await
        .unwrap();
    let latest = store.request_scan(now(), None, None).await.unwrap();
    assert_eq!(
        store.newest_scan().await.unwrap().unwrap().int("id"),
        latest
    );
}

// =============================================================================================
// TestTheTwoRefusals — the route's own rule, over the store (`request_scan` carries it here)
// =============================================================================================

/// `_decide(store, now)`: `{"accepted": False, "reason": ...}` or `{"accepted": True}`, the
/// reasons in the words `/vbt/rescan` answers with.
async fn decide(store: &mut PgVbtStore, at: DateTime<FixedOffset>) -> Result<i64, &'static str> {
    match store.request_scan(at, None, None).await {
        Ok(id) => Ok(id),
        Err(StoreError::Scan(r)) if r.status == 409 => Err("a re-detect is already in flight"),
        Err(StoreError::Scan(r)) if r.status == 429 => Err("too soon"),
        Err(other) => panic!("{other}"),
    }
}

async fn set_status(store: &mut PgVbtStore, status: &str) {
    store
        .conn
        .execute("UPDATE vb_scan_run SET status = ?", &[txt(status)])
        .await
        .unwrap();
}

#[tokio::test]
async fn a_second_press_while_one_is_in_flight_is_refused() {
    let (_d, mut store) = fresh(false).await;
    store.request_scan(now(), None, None).await.unwrap();
    assert_eq!(
        decide(&mut store, now() + Duration::seconds(5)).await,
        Err("a re-detect is already in flight")
    );
    assert_eq!(scan_rows(&mut store).await.len(), 1);
}

#[tokio::test]
async fn both_in_flight_states_refuse() {
    for status in ["QUEUED", "RUNNING"] {
        let (_d, mut store) = fresh(false).await;
        store.request_scan(now(), None, None).await.unwrap();
        set_status(&mut store, status).await;
        assert!(
            decide(&mut store, now() + Duration::seconds(5))
                .await
                .is_err(),
            "{status}"
        );
    }
}

#[tokio::test]
async fn a_finished_scan_inside_the_minute_is_too_soon_rather_than_in_flight() {
    let (_d, mut store) = fresh(false).await;
    store.request_scan(now(), None, None).await.unwrap();
    set_status(&mut store, "DONE").await;
    assert_eq!(
        decide(&mut store, now() + Duration::seconds(30)).await,
        Err("too soon")
    );
}

#[tokio::test]
async fn a_minute_later_it_is_allowed() {
    let (_d, mut store) = fresh(false).await;
    store.request_scan(now(), None, None).await.unwrap();
    set_status(&mut store, "DONE").await;
    assert!(
        decide(&mut store, now() + Duration::seconds(61))
            .await
            .is_ok()
    );
    assert_eq!(scan_rows(&mut store).await.len(), 2);
}

#[tokio::test]
async fn a_dead_worker_does_not_wedge_the_button() {
    let (_d, mut store) = fresh(false).await;
    store
        .request_scan(
            now() - Duration::seconds(st::RESCAN_STALE_AFTER_SECONDS + 1),
            None,
            None,
        )
        .await
        .unwrap();
    set_status(&mut store, "RUNNING").await;
    assert!(decide(&mut store, now()).await.is_ok());
    assert_eq!(scan_rows(&mut store).await.len(), 2);
}

#[tokio::test]
async fn a_failed_scan_can_be_retried_after_the_interval() {
    let (_d, mut store) = fresh(false).await;
    store.request_scan(now(), None, None).await.unwrap();
    store
        .conn
        .execute(
            "UPDATE vb_scan_run SET status = 'FAILED', error = 'boom'",
            &[],
        )
        .await
        .unwrap();
    assert!(
        decide(&mut store, now() + Duration::seconds(61))
            .await
            .is_ok()
    );
}

// =============================================================================================
// TestTheDeskAndTheWorkerAgree — read off the worker's source as text
// =============================================================================================

fn worker_source() -> String {
    let path = desk_root()
        .join("../decile-blueprint/services/worker/src/baskfy_worker/tasks/vbt_rescan.py");
    assert!(
        path.is_file(),
        "{}: moved; this test cannot check the copies agree",
        path.display()
    );
    std::fs::read_to_string(path).unwrap()
}

#[test]
fn the_worker_module_is_where_this_says_it_is() {
    assert!(!worker_source().is_empty());
}

#[test]
fn the_numbers_match() {
    let source = worker_source();
    for (desk_value, worker_name) in [
        (st::RESCAN_STALE_AFTER_SECONDS, "STALE_AFTER_SECONDS"),
        (st::RESCAN_MIN_INTERVAL_SECONDS, "MIN_INTERVAL_SECONDS"),
    ] {
        let line = source
            .lines()
            .find(|l| l.starts_with(&format!("{worker_name}: Final")))
            .unwrap();
        let value: i64 = line.split('=').nth(1).unwrap().trim().parse().unwrap();
        assert_eq!(value, desk_value, "{worker_name}");
    }
}

#[test]
fn the_in_flight_states_match() {
    assert!(
        worker_source().contains(r#"IN_FLIGHT: Final[tuple[str, ...]] = ("QUEUED", "RUNNING")"#)
    );
    assert_eq!(st::RESCAN_IN_FLIGHT, ["QUEUED", "RUNNING"]);
}

// =============================================================================================
// TestThePage (rescan) and TestTheLaw (desk) — the one template both desks render
// =============================================================================================

fn template() -> String {
    std::fs::read_to_string(desk_root().join("app/templates/vbt.html")).unwrap()
}

#[test]
fn the_button_posts_to_the_rescan_route_and_nothing_else() {
    let body = template();
    assert!(body.contains(r#"action="/vbt/rescan""#));
    assert_eq!(body.matches(r#"class="vb-rescan""#).count(), 1);
}

#[test]
fn the_button_says_what_it_does_rather_than_scan_now() {
    let body = template();
    assert!(body.contains("Re-detect the last session"));
    assert!(!body.contains("Scan now"));
}

#[test]
fn the_template_has_no_confirm_all_control() {
    let body = template();
    assert_eq!(body.matches(r#"action="/vbt/execute""#).count(), 1);
    assert!(!body.contains("confirm_all"));
    assert!(!body.contains("confirm-all"));
}

/// The page module's own source (`vbt.rs`): Python's `vbt_desk.py`.
fn page_source() -> String {
    std::fs::read_to_string(lane_dir().join("vbt.rs")).unwrap()
}

#[test]
fn the_page_module_names_no_broker_method() {
    let source = page_source();
    for forbidden in ["place_order", "place_gtt(", "kc.", "kite.kc", "KiteWrites"] {
        assert!(!source.contains(forbidden), "{forbidden}");
    }
}

#[test]
fn no_auto_execute_flag_is_referenced() {
    assert!(!page_source().to_uppercase().contains("AUTO_EXECUTE"));
}
