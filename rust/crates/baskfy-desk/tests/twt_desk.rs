//! `tests/test_twt_desk.py` (TestTheStore, TestTheView) and `tests/test_twt_scan_desk.py`'s
//! "the two copies agree", ported: the three-weeks-tight desk's store round-trips against SQLite
//! with `Decimal` out, the page's view puts exits before entries, an expired plan has no
//! confirmable line, the 15:15 strip names a naked line, and the flags read as their defaults.
//!
//! **The database.** As in Python, the `tw_` tables are built from a DDL mirroring
//! `alembic/versions/0041_twt.py` for the columns the store reads, in SQLite's spelling, and the
//! store runs the same SQL it runs on Postgres with the schema prefix `""`. Python's test
//! registers a `Decimal → str` adapter on sqlite3; the Rust SQLite binder refuses a `Decimal`
//! (R8.2), so the fixtures here write money as text, which is what that adapter does. Stamps are
//! written in the binder's own spelling so text comparison agrees with what the store binds
//! (Python's test registers `isoformat()` for the same reason).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::str::FromStr;

use baskfy_desk::Settings;
use baskfy_desk::lanes::twt::common::{Val, db_int};
use baskfy_desk::lanes::twt::store::{PgTwtStore, SessionBump, TwtStore};
use baskfy_desk::lanes::twt::{self as w};
use baskfy_desk::storage::{Db, Param};
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, TimeZone};
use rust_decimal::Decimal;

const DDL: &[&str] = &[
    "CREATE TABLE instrument (id INTEGER PRIMARY KEY, symbol TEXT)",
    "CREATE TABLE ohlcv_daily (instrument_id INTEGER, date TEXT, close NUMERIC, adj_factor NUMERIC DEFAULT 1, PRIMARY KEY (instrument_id, date))",
    "CREATE TABLE tw_config (user_id INTEGER PRIMARY KEY, sleeve_capital_inr NUMERIC, max_open_positions INTEGER, max_position_pct NUMERIC, stop_pct NUMERIC, trail_pct NUMERIC, first_live_entries_left INTEGER, dry_run_sessions INTEGER, updated_at TEXT, updated_by TEXT)",
    "CREATE TABLE tw_config_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, key TEXT, old_value TEXT, new_value TEXT, changed_at TEXT, changed_by TEXT, note TEXT)",
    "CREATE TABLE tw_breadth_daily (user_id INTEGER, date TEXT, universe_count INTEGER, measured_count INTEGER, above_count INTEGER, pct_above_dma NUMERIC, gate TEXT, dma_bars INTEGER, thin_session INTEGER DEFAULT 0, PRIMARY KEY (user_id, date))",
    "CREATE TABLE tw_signal_daily (user_id INTEGER, date TEXT, instrument_id INTEGER, state TEXT, entry_reference_close NUMERIC, stop_preview NUMERIC, sessions_out_before INTEGER, rank_key INTEGER, turnover_avg_20 INTEGER, PRIMARY KEY (user_id, date, instrument_id))",
    "CREATE TABLE tw_plan (id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT, user_id INTEGER, session_date TEXT, source TEXT, built_at TEXT, expires_at TEXT, plan_hash TEXT, gate TEXT, sleeve_equity_inr NUMERIC, total_new_exposure_inr NUMERIC)",
    "CREATE TABLE tw_plan_line (id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, user_id INTEGER, instrument_id INTEGER, kind TEXT, state TEXT, quantity INTEGER, stop_price NUMERIC, value_inr NUMERIC, high_since NUMERIC, previous_trigger NUMERIC, note TEXT, client_id TEXT, journal_ref TEXT, order_id INTEGER, position_id INTEGER)",
    "CREATE TABLE tw_plan_skip (id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, user_id INTEGER, instrument_id INTEGER, reason TEXT, detail TEXT)",
    "CREATE TABLE tw_position (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, broker_account_id INTEGER, instrument_id INTEGER, order_id INTEGER, signal_date TEXT, entry_date TEXT, entry_avg NUMERIC, entry_adj_factor NUMERIC DEFAULT 1, quantity_entered INTEGER, quantity_open INTEGER, initial_stop NUMERIC, stop_price NUMERIC, high_since NUMERIC, high_since_date TEXT, gtt_id TEXT, gtt_trigger NUMERIC, gtt_armed_at TEXT, next_trigger NUMERIC, next_trigger_for TEXT, state TEXT, closed_on TEXT, exit_avg NUMERIC, close_reason TEXT, pnl_inr NUMERIC, return_pct NUMERIC, simulated INTEGER DEFAULT 1, half_size INTEGER DEFAULT 0, partial_done INTEGER DEFAULT 0, trail TEXT, partial_queued_for TEXT, partial_quantity INTEGER, exit_queued_for TEXT, exit_reason_queued TEXT)",
    "CREATE TABLE tw_order (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, broker_account_id INTEGER, instrument_id INTEGER, signal_date TEXT, side TEXT, quantity INTEGER, stop_price NUMERIC, state TEXT, broker_order_id TEXT, client_id TEXT, filled_quantity INTEGER DEFAULT 0, avg_fill_price NUMERIC, position_id INTEGER, simulated INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE tw_fill (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, position_id INTEGER, order_id INTEGER, side TEXT, quantity INTEGER, price NUMERIC, filled_at TEXT, journal_ref TEXT, simulated INTEGER DEFAULT 1)",
    "CREATE TABLE tw_session (user_id INTEGER, session_date TEXT, mode TEXT DEFAULT 'DRY_RUN', gate TEXT DEFAULT 'SHUT', states INTEGER DEFAULT 0, signals INTEGER DEFAULT 0, confirms INTEGER DEFAULT 0, fills INTEGER DEFAULT 0, ratchets INTEGER DEFAULT 0, exits INTEGER DEFAULT 0, naked_at_1515 INTEGER DEFAULT 0, first_live_entries_counted INTEGER DEFAULT 0, PRIMARY KEY (user_id, session_date))",
    "CREATE TABLE lv_protection_issue (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, sleeve TEXT NOT NULL, position_id INTEGER NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, seen_at TEXT NOT NULL, resolved_at TEXT)",
];

fn ist() -> FixedOffset {
    FixedOffset::east_opt(5 * 3600 + 1800).unwrap()
}

fn now() -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(2026, 9, 11, 9, 20, 0).unwrap()
}

fn session() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 9, 10).unwrap()
}

fn d(text: &str) -> Decimal {
    Decimal::from_str(text).unwrap()
}

/// The binder's spelling of an aware stamp (`storage::Conn` on SQLite).
fn stamp(t: DateTime<FixedOffset>) -> String {
    t.format("%Y-%m-%d %H:%M:%S%.f%:z").to_string()
}

fn txt(s: &str) -> Param {
    Param::Text(s.to_owned())
}

struct Fixture {
    _dir: tempfile::TempDir,
    store: PgTwtStore,
    /// The SQLite file the desk's own pool opens for the route tests.
    path: std::path::PathBuf,
}

async fn store() -> Fixture {
    // `sqlite3.register_adapter(Decimal, str)`, as the Python module registers it at import.
    baskfy_desk::storage::register_sqlite_decimal_adapter();
    let dir = tempfile::tempdir().unwrap();
    let mut conn = Db::connect_sqlite_path(&dir.path().join("twt.db"))
        .await
        .unwrap();
    for stmt in DDL {
        conn.execute(stmt, &[]).await.unwrap();
    }
    let day = session().format("%Y-%m-%d").to_string();
    conn.execute(
        "INSERT INTO instrument (id, symbol) VALUES (42, 'TWTCO'), (43, 'HELDCO')",
        &[],
    )
    .await
    .unwrap();
    conn.execute(
        "INSERT INTO tw_config (user_id, sleeve_capital_inr, max_open_positions, \
         max_position_pct, stop_pct, trail_pct, first_live_entries_left, dry_run_sessions, \
         updated_at, updated_by) \
         VALUES (1, '2500000.00', 10, '12.50', '20.00', '20.00', 10, 0, ?, 'test')",
        &[txt(&stamp(now()))],
    )
    .await
    .unwrap();
    conn.execute(
        "INSERT INTO tw_breadth_daily (user_id, date, universe_count, measured_count, \
         above_count, pct_above_dma, gate, dma_bars, thin_session) \
         VALUES (1, ?, 4000, 1200, 700, '58.3333', 'OPEN', 200, 0)",
        &[txt(&day)],
    )
    .await
    .unwrap();
    conn.execute(
        "INSERT INTO ohlcv_daily (instrument_id, date, close, adj_factor) \
         VALUES (42, ?, '100.00', 1), (43, ?, '100.00', 1)",
        &[txt(&day), txt(&day)],
    )
    .await
    .unwrap();
    conn.execute(
        "INSERT INTO tw_signal_daily (user_id, date, instrument_id, state, \
         entry_reference_close, stop_preview, sessions_out_before, rank_key, turnover_avg_20) \
         VALUES (1, ?, 42, 'SIGNAL', '100.00', '80.00', 5, 500000000, 500000000)",
        &[txt(&day)],
    )
    .await
    .unwrap();
    let path = dir.path().join("twt.db");
    Fixture {
        _dir: dir,
        store: PgTwtStore::new(conn, 1, "", Some(1)),
        path,
    }
}

async fn a_plan(store: &mut PgTwtStore, expires_in_minutes: i64, n: u32) -> (String, i64) {
    let plan_id = format!("00000000-0000-4000-8000-{n:012}");
    store
        .conn
        .execute(
            "INSERT INTO tw_plan (plan_id, user_id, session_date, source, built_at, expires_at, \
             plan_hash, gate, sleeve_equity_inr, total_new_exposure_inr) \
             VALUES (?, 1, ?, 'MORNING', ?, ?, 'abc', 'OPEN', '2500000.00', '250000.00')",
            &[
                txt(&plan_id),
                txt(&session().format("%Y-%m-%d").to_string()),
                txt(&stamp(now() - Duration::minutes(5))),
                txt(&stamp(now() + Duration::minutes(expires_in_minutes))),
            ],
        )
        .await
        .unwrap();
    let cur = store
        .conn
        .execute("SELECT id FROM tw_plan WHERE plan_id = ?", &[txt(&plan_id)])
        .await
        .unwrap();
    let pk = db_int(cur.rows[0].get("id"));
    (plan_id, pk)
}

#[allow(clippy::too_many_arguments)]
async fn a_line(
    store: &mut PgTwtStore,
    plan_pk: i64,
    plan_id: &str,
    kind: &str,
    instrument_id: i64,
    symbol: &str,
    stop: &str,
    previous: Option<&str>,
) -> i64 {
    let suffix = if kind == "BUY_AT_OPEN" {
        String::new()
    } else {
        format!(":{kind}")
    };
    store
        .conn
        .execute(
            "INSERT INTO tw_plan_line (plan_id, user_id, instrument_id, kind, state, quantity, \
             stop_price, value_inr, high_since, previous_trigger, note, client_id) \
             VALUES (?, 1, ?, ?, 'PROPOSED', 2500, ?, '250000.00', NULL, ?, '', ?)",
            &[
                Param::Int(plan_pk),
                Param::Int(instrument_id),
                txt(kind),
                txt(stop),
                previous.map_or(Param::Null, txt),
                txt(&format!("{plan_id}:{symbol}{suffix}")),
            ],
        )
        .await
        .unwrap();
    let cur = store
        .conn
        .execute("SELECT MAX(id) AS id FROM tw_plan_line", &[])
        .await
        .unwrap();
    db_int(cur.rows[0].get("id"))
}

async fn a_position(store: &mut PgTwtStore, quantity: i64, gtt: Option<&str>) -> i64 {
    let day = session().format("%Y-%m-%d").to_string();
    store
        .conn
        .execute(
            "INSERT INTO tw_position (user_id, broker_account_id, instrument_id, signal_date, \
             entry_date, entry_avg, entry_adj_factor, quantity_entered, quantity_open, \
             initial_stop, stop_price, high_since, high_since_date, gtt_id, gtt_trigger, state, \
             simulated, half_size) VALUES (1, 1, 43, ?, ?, '100.00', 1, ?, ?, '80.00', '80.00', \
             '100.00', ?, ?, ?, 'OPEN', 1, 0)",
            &[
                txt(&day),
                txt(&day),
                Param::Int(quantity),
                Param::Int(quantity),
                txt(&day),
                gtt.map_or(Param::Null, txt),
                gtt.map_or(Param::Null, |_| txt("80.00")),
            ],
        )
        .await
        .unwrap();
    let cur = store
        .conn
        .execute("SELECT MAX(id) AS id FROM tw_position", &[])
        .await
        .unwrap();
    db_int(cur.rows[0].get("id"))
}

fn kinds(v: &Val) -> Vec<String> {
    match v {
        Val::List(items) => items
            .iter()
            .map(|i| match i {
                Val::Map(r) => r.s("kind"),
                _ => panic!("not a row"),
            })
            .collect(),
        _ => panic!("not a list"),
    }
}

// =============================================================================================
// TestTheStore
// =============================================================================================

#[tokio::test]
async fn a_plan_and_its_lines_round_trip() {
    let mut f = store().await;
    let (plan_id, pk) = a_plan(&mut f.store, 25, 1).await;
    let line_id = a_line(
        &mut f.store,
        pk,
        &plan_id,
        "BUY_AT_OPEN",
        42,
        "TWTCO",
        "80.00",
        None,
    )
    .await;
    let plan = f.store.plan(&plan_id).await.unwrap().unwrap();
    let line = f.store.line(line_id).await.unwrap().unwrap();
    assert_eq!(plan.date("session_date"), Some(session()));
    assert_eq!(plan.dec("sleeve_equity_inr"), Some(d("2500000.00")));
    assert_eq!(line.s("symbol"), "TWTCO");
    assert_eq!(line.dec("stop_price"), Some(d("80.00")));
    assert_eq!(line.s("client_id"), format!("{plan_id}:TWTCO"));
}

#[tokio::test]
async fn a_malformed_plan_id_is_none_and_not_a_five_hundred() {
    let mut f = store().await;
    assert!(f.store.plan("not-a-uuid").await.unwrap().is_none());
}

#[tokio::test]
async fn a_line_carries_its_signals_turnover_for_the_confirms_resize() {
    let mut f = store().await;
    let (plan_id, pk) = a_plan(&mut f.store, 25, 1).await;
    let line_id = a_line(
        &mut f.store,
        pk,
        &plan_id,
        "BUY_AT_OPEN",
        42,
        "TWTCO",
        "80.00",
        None,
    )
    .await;
    let line = f.store.line(line_id).await.unwrap().unwrap();
    assert_eq!(line.dec("turnover_avg_inr"), Some(d("500000000")));
}

#[tokio::test]
async fn the_book_is_only_what_the_sleeve_bought() {
    let mut f = store().await;
    a_position(&mut f.store, 1_000, Some("551234")).await;
    let book = f.store.open_positions().await.unwrap();
    let symbols: Vec<String> = book.iter().map(|r| r.s("symbol")).collect();
    assert_eq!(symbols, vec!["HELDCO".to_owned()]);
    assert_eq!(book[0].dec("stop_price"), Some(d("80.00")));
}

#[tokio::test]
async fn the_session_counters_accumulate_and_the_naked_count_is_set() {
    let mut f = store().await;
    let bump = |confirms, fills, ratchets| SessionBump {
        mode: "DRY_RUN".to_owned(),
        confirms,
        fills,
        ratchets,
        exits: 0,
    };
    f.store
        .bump_session(session(), bump(1, 0, 1))
        .await
        .unwrap();
    f.store
        .bump_session(session(), bump(1, 1, 0))
        .await
        .unwrap();
    f.store
        .set_naked_count(session(), "DRY_RUN", 2)
        .await
        .unwrap();
    f.store
        .set_naked_count(session(), "DRY_RUN", 0)
        .await
        .unwrap();
    let row = f.store.session(session()).await.unwrap().unwrap();
    assert_eq!(row.int("confirms"), 2);
    assert_eq!(row.int("fills"), 1);
    assert_eq!(row.int("ratchets"), 1);
    assert_eq!(row.int("naked_at_1515"), 0);
}

#[tokio::test]
async fn the_sleeves_money_is_its_own() {
    let mut f = store().await;
    a_position(&mut f.store, 100, Some("551234")).await;
    let money = f.store.sleeve_money(session()).await.unwrap();
    assert_eq!(money.equity_inr, d("2500000.00"));
    assert_eq!(money.open_exposure_inr, d("10000.00"));
    assert_eq!(money.first_live_entries_left, 10);
}

#[tokio::test]
async fn the_countdown_spends_once_and_never_on_a_simulated_fill() {
    let mut f = store().await;
    let position_id = a_position(&mut f.store, 1_000, Some("551234")).await;
    assert!(
        !f.store
            .count_first_live_entry(position_id, session(), now(), false)
            .await
            .unwrap()
    );
    f.store
        .conn
        .execute(
            "UPDATE tw_position SET simulated = 0 WHERE id = ?",
            &[Param::Int(position_id)],
        )
        .await
        .unwrap();
    assert!(
        f.store
            .count_first_live_entry(position_id, session(), now(), true)
            .await
            .unwrap()
    );
    assert_eq!(
        f.store
            .config()
            .await
            .unwrap()
            .int("first_live_entries_left"),
        9
    );
    assert!(
        !f.store
            .count_first_live_entry(position_id, session(), now(), true)
            .await
            .unwrap()
    );
    assert_eq!(
        f.store
            .config()
            .await
            .unwrap()
            .int("first_live_entries_left"),
        9
    );
    let audit = f
        .store
        .conn
        .execute("SELECT key FROM tw_config_audit", &[])
        .await
        .unwrap();
    let keys: Vec<&str> = audit
        .rows
        .iter()
        .map(|r| r.get("key").unwrap().as_str().unwrap())
        .collect();
    assert_eq!(keys, vec!["first_live_entries_left"]);
}

#[tokio::test]
async fn zero_capital_audits_the_previous_value() {
    let mut f = store().await;
    let before = f.store.zero_capital(now(), "twt-halt").await.unwrap();
    assert_eq!(before, Some(d("2500000.00")));
    assert_eq!(
        f.store.config().await.unwrap().dec("sleeve_capital_inr"),
        Some(Decimal::ZERO)
    );
    let cur = f
        .store
        .conn
        .execute(
            "SELECT old_value, new_value FROM tw_config_audit WHERE key = 'sleeve_capital_inr'",
            &[],
        )
        .await
        .unwrap();
    let row = &cur.rows[0];
    // sqlite's NUMERIC affinity drops the trailing paise the Postgres column keeps, so the value
    // is compared as a number (as Python's test does).
    assert_eq!(
        d(row.get("old_value").unwrap().as_str().unwrap()),
        d("2500000.00")
    );
    assert_eq!(
        d(row.get("new_value").unwrap().as_str().unwrap()),
        Decimal::ZERO
    );
}

#[tokio::test]
async fn expire_plans_moves_only_the_live_ones() {
    let mut f = store().await;
    a_plan(&mut f.store, 25, 1).await;
    a_plan(&mut f.store, -60, 2).await;
    assert_eq!(f.store.expire_plans(now()).await.unwrap(), 1);
}

// =============================================================================================
// TestTheView — `05` §2
// =============================================================================================

#[tokio::test]
async fn exits_come_before_entries_in_the_view() {
    let mut f = store().await;
    let (plan_id, pk) = a_plan(&mut f.store, 25, 1).await;
    a_line(
        &mut f.store,
        pk,
        &plan_id,
        "BUY_AT_OPEN",
        42,
        "TWTCO",
        "80.00",
        None,
    )
    .await;
    a_line(
        &mut f.store,
        pk,
        &plan_id,
        "RAISE_GTT_STOP",
        43,
        "HELDCO",
        "104.00",
        Some("80.00"),
    )
    .await;
    let s = Settings::defaults().unwrap();
    let view = w::build_view(&mut f.store, &s, now()).await.unwrap();
    assert_eq!(kinds(&view.val("lines"))[0], "RAISE_GTT_STOP");
    assert_eq!(kinds(&view.val("exits")), vec!["RAISE_GTT_STOP".to_owned()]);
    assert_eq!(kinds(&view.val("entries")), vec!["BUY_AT_OPEN".to_owned()]);
}

fn confirmables(view: &w::common::Rec) -> Vec<bool> {
    match view.val("lines") {
        Val::List(items) => items
            .iter()
            .map(|i| match i {
                Val::Map(r) => r.truthy("confirmable"),
                _ => panic!("not a row"),
            })
            .collect(),
        _ => panic!("not a list"),
    }
}

#[tokio::test]
async fn an_expired_plan_has_no_confirmable_line() {
    let mut f = store().await;
    let (plan_id, pk) = a_plan(&mut f.store, -1, 1).await;
    a_line(
        &mut f.store,
        pk,
        &plan_id,
        "BUY_AT_OPEN",
        42,
        "TWTCO",
        "80.00",
        None,
    )
    .await;
    let s = Settings::defaults().unwrap();
    let view = w::build_view(&mut f.store, &s, now()).await.unwrap();
    assert_eq!(view.val("expired"), Val::Bool(true));
    assert_eq!(confirmables(&view), vec![false]);
    assert!(view.s("countdown").contains("expired at"));
}

#[tokio::test]
async fn a_live_plan_says_how_long_it_has_left() {
    let mut f = store().await;
    let (plan_id, pk) = a_plan(&mut f.store, 25, 1).await;
    a_line(
        &mut f.store,
        pk,
        &plan_id,
        "BUY_AT_OPEN",
        42,
        "TWTCO",
        "80.00",
        None,
    )
    .await;
    let s = Settings::defaults().unwrap();
    let view = w::build_view(&mut f.store, &s, now()).await.unwrap();
    assert_eq!(view.val("expired"), Val::Bool(false));
    assert!(view.s("countdown").contains("min"));
    assert_eq!(confirmables(&view), vec![true]);
}

#[tokio::test]
async fn a_naked_line_is_named_on_the_view() {
    let mut f = store().await;
    a_position(&mut f.store, 1_000, None).await;
    let s = Settings::defaults().unwrap();
    let view = w::build_view(&mut f.store, &s, now()).await.unwrap();
    assert_eq!(
        view.val("naked"),
        Val::List(vec![Val::Str("HELDCO".to_owned())])
    );
}

#[tokio::test]
async fn the_view_carries_the_gate_the_flags_and_the_half_size_counter() {
    let mut f = store().await;
    a_plan(&mut f.store, 25, 1).await;
    let s = Settings::defaults().unwrap();
    let view = w::build_view(&mut f.store, &s, now()).await.unwrap();
    match view.val("breadth") {
        Val::Map(b) => assert_eq!(b.s("gate"), "OPEN"),
        other => panic!("no breadth: {other:?}"),
    }
    assert_eq!(view.val("dry_run"), Val::Bool(true));
    assert_eq!(view.val("execution_enabled"), Val::Bool(false));
    assert_eq!(view.val("first_live_entries_left").as_int(), Some(10));
    assert_eq!(view.val("max_new_entries_per_session").as_int(), Some(3));
}

#[tokio::test]
async fn a_desk_with_no_tw_tables_says_so_instead_of_five_hundreding() {
    let dir = tempfile::tempdir().unwrap();
    let conn = Db::connect_sqlite_path(&dir.path().join("empty.db"))
        .await
        .unwrap();
    let mut empty = PgTwtStore::new(conn, 1, "", None);
    let s = Settings::defaults().unwrap();
    assert!(w::build_view(&mut empty, &s, now()).await.is_err());
    let view = w::unavailable_view("no such table: tw_plan", &s, now());
    assert_eq!(view.val("available"), Val::Bool(false));
    assert_eq!(view.val("lines"), Val::List(Vec::new()));
    assert_eq!(view.s("reason"), "no such table: tw_plan");
}

// =============================================================================================
// The countdown and the constants
// =============================================================================================

#[test]
fn the_countdown_reads_minutes_and_seconds_and_names_the_expiry() {
    let mut plan = w::common::Rec::new();
    plan.set(
        "expires_at",
        Val::Stamp(now() + Duration::seconds(25 * 60 + 7)),
    );
    assert_eq!(w::countdown(Some(&plan), &now()), "25 min 7 s left");
    plan.set("expires_at", Val::Stamp(now() - Duration::minutes(1)));
    assert_eq!(
        w::countdown(Some(&plan), &now()),
        "expired at 09:19 \u{2014} rebuild it (make twt-plan SOURCE=MORNING) and read it again"
    );
    assert_eq!(w::countdown(None, &now()), "");
}

#[test]
fn the_plan_lives_thirty_minutes_and_the_sweep_is_at_1515() {
    // Non-negotiable 1: plans expire in 30 minutes. `04` §10.4 restates it on the page.
    assert_eq!(w::PLAN_TTL_MINUTES, 30);
    assert_eq!(w::sweep_at().to_string(), "15:15:00");
}

#[test]
fn the_two_copies_of_the_scan_windows_agree() {
    // `tests/test_twt_scan_desk.py::TestTheTwoCopiesAgree`: the desk's copy of
    // `baskfy_worker.tasks.twt_scan`'s two refusal windows.
    assert_eq!(w::SCAN_STALE_AFTER_SECONDS, 600);
    assert_eq!(w::SCAN_MIN_INTERVAL_SECONDS, 60);
}

#[test]
fn every_kind_has_its_label() {
    let labels: Vec<(&str, &str)> = w::KIND_LABEL.to_vec();
    assert_eq!(
        labels,
        vec![
            ("BUY_AT_OPEN", "TWT BUY AT OPEN"),
            ("ARM_GTT", "TWT ARM STOP"),
            ("RAISE_GTT_STOP", "TWT RAISE STOP"),
            ("SELL_AT_OPEN", "TWT SELL AT OPEN"),
        ]
    );
}

// =============================================================================================
// TestTheDeskModuleNamesNoBroker — law 2, restated for a page
// =============================================================================================

#[test]
fn the_desk_module_never_calls_a_placing_verb_itself() {
    // Every order goes through `execute`, which goes through the sleeve's gateway. The one broker
    // call in the page's module is a read (`get_gtts`), named on purpose.
    for (name, text) in [
        ("src/lanes/twt.rs", include_str!("../src/lanes/twt.rs")),
        (
            "src/lanes/twt/store.rs",
            include_str!("../src/lanes/twt/store.rs"),
        ),
    ] {
        for verb in [
            "KiteWrites",
            "kite.place",
            "kite.modify",
            "kite.cancel",
            "kite.delete",
        ] {
            assert!(!text.contains(verb), "{verb} is named in {name}");
        }
    }
    assert!(include_str!("../src/lanes/twt.rs").contains("state.kite.get_gtts()"));
}

/// `lock_session_for_update` on SQLite opens `BEGIN IMMEDIATE` only when the connection is not
/// already in a transaction (`_in_transaction(conn) is False`), so a second confirm — or the fill
/// path's lock after the confirm's — on the same store is not a "within a transaction" error.
#[tokio::test]
async fn the_session_lock_twice_on_one_connection_is_not_an_error() {
    let mut f = store().await;
    f.store.lock_session_for_update(session()).await.unwrap();
    f.store.lock_session_for_update(session()).await.unwrap();
    assert!(f.store.session(session()).await.unwrap().is_some());
}

#[tokio::test]
async fn entries_taken_counts_this_sessions_buys_from_any_plan() {
    use baskfy_desk::lanes::twt::common::Rec;
    let mut f = store().await;
    let utc = FixedOffset::east_opt(0).unwrap();
    f.store
        .create_order(Rec::of([
            ("instrument_id", Val::Int(42)),
            ("signal_date", Val::from(session())),
            ("side", Val::from("BUY")),
            ("quantity", Val::Int(10)),
            ("stop_price", Val::from(d("80.00"))),
            ("state", Val::from("CONFIRMED")),
            ("client_id", Val::from("a")),
            ("simulated", Val::Bool(true)),
            // LV10.2: the cap counts by the IST day the order was sent
            (
                "created_at",
                Val::from(
                    utc.from_local_datetime(&session().and_hms_opt(4, 0, 0).unwrap())
                        .unwrap(),
                ),
            ),
        ]))
        .await
        .unwrap();
    assert_eq!(f.store.entries_taken(session()).await.unwrap(), 1);
    assert_eq!(
        f.store
            .entries_taken(session() + Duration::days(1))
            .await
            .unwrap(),
        0
    );
}

// =============================================================================================
// R8.TD1 — the session lock is one transaction, closed by the confirm's exit
// =============================================================================================

/// A block that opened the transaction commits it on the way out: a second connection sees
/// what was written inside, and the store holds no transaction afterwards.
#[tokio::test]
async fn sqlite_commits_on_the_way_out() {
    let mut f = store().await;
    f.store.lock_session_for_update(session()).await.unwrap();
    assert!(f.store.session_locked());
    f.store
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
    f.store.unlock_session().await.unwrap();
    assert!(!f.store.session_locked());
    let mut other = Db::connect_sqlite_path(&f.path).await.unwrap();
    let rows = other
        .execute(
            "SELECT confirms FROM tw_session WHERE user_id = 1 AND session_date = ?",
            &[txt(&session().format("%Y-%m-%d").to_string())],
        )
        .await
        .unwrap()
        .rows;
    assert_eq!(db_int(rows[0].get("confirms")), 1);
    // A store with nothing open closes nothing.
    f.store.unlock_session().await.unwrap();
}

// =============================================================================================
// The routes — TW6a's six, plus the page (`client` in test_twt_desk.py)
// =============================================================================================

mod routes {
    use std::sync::Arc;

    use axum::Router;
    use axum::body::{Body, to_bytes};
    use axum::http::{Request, StatusCode, header};
    use baskfy_desk::app::{AppState, DeskPaths, StateParts};
    use baskfy_desk::clock::FixedClock;
    use baskfy_desk::fake_kite::ReplayKite;
    use baskfy_desk::gateway::{
        CancelOrder, DeleteGtt, Gateway, GatewayError, GttStop, ModifyGtt, PlaceOrder,
        SleeveGatewaySpec,
    };
    use baskfy_desk::ids::HarnessIds;
    use baskfy_desk::storage::{Db, DbConfig};
    use baskfy_desk::wire::{PyDict, PyValue, dict};
    use baskfy_desk::{Settings, build_app, build_state};
    use tower::ServiceExt as _;

    use super::now;

    /// `LAST_PRICES`: what the broker would quote (the replay Kite's `quotes`).
    pub const LAST_PRICES: [(&str, i64, f64); 2] =
        [("TWTCO", 1042, 100.0), ("HELDCO", 1043, 130.0)];

    /// What the test's gateway does on `place` / `place_gtt_stop`.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub enum Gw {
        /// `_Gateway`: a gateway that simulates, and a broker that is never reached.
        Simulates,
        /// `_Guarded.place`: raises `UntouchableInstrumentError`.
        GuardedPlace,
        /// `_Guarded.place_gtt_stop`: raises `UntouchableInstrumentError`.
        GuardedGtt,
        /// The scan fixture's `_no_gateway`: asking for it at all fails the test.
        Explodes,
    }

    #[derive(Debug)]
    struct TestGateway(Gw);

    fn untouchable() -> GatewayError {
        GatewayError {
            python_type: "UntouchableInstrumentError".to_owned(),
            message: "SGBMAY29: sovereign gold bonds are untouchable".to_owned(),
        }
    }

    fn absent(what: &str) -> GatewayError {
        GatewayError {
            python_type: "AttributeError".to_owned(),
            message: format!("'_Gateway' object has no attribute '{what}'"),
        }
    }

    #[async_trait::async_trait]
    impl Gateway for TestGateway {
        async fn place(&self, _order: PlaceOrder) -> Result<PyDict, GatewayError> {
            if self.0 == Gw::GuardedPlace {
                return Err(untouchable());
            }
            Ok(dict([
                ("status", PyValue::Str("DRY_RUN".to_owned())),
                ("order_id", PyValue::Str("DRY-1".to_owned())),
            ]))
        }
        async fn place_gtt_stop(&self, stop: GttStop) -> Result<PyDict, GatewayError> {
            if self.0 == Gw::GuardedGtt {
                return Err(untouchable());
            }
            Ok(dict([
                ("status", PyValue::Str("DRY_RUN_GTT".to_owned())),
                ("gtt_id", PyValue::None),
                (
                    "client_id",
                    stop.client_id.map_or(PyValue::None, PyValue::Str),
                ),
                ("trigger", PyValue::Float(stop.trigger)),
            ]))
        }
        async fn delete_gtt(&self, req: DeleteGtt) -> Result<PyDict, GatewayError> {
            Ok(dict([
                ("status", PyValue::Str("DRY_RUN_GTT_DELETE".to_owned())),
                ("gtt_id", PyValue::Int(req.gtt_id)),
            ]))
        }
        async fn modify_gtt_quantity(&self, _req: ModifyGtt) -> Result<PyDict, GatewayError> {
            Err(absent("modify_gtt_quantity"))
        }
        async fn cancel_order(&self, _req: CancelOrder) -> Result<PyDict, GatewayError> {
            Err(absent("cancel_order"))
        }
        fn derive_sleeve(&self, _spec: SleeveGatewaySpec) -> Option<Arc<dyn Gateway>> {
            assert!(
                self.0 != Gw::Explodes,
                "a scan route asked for a broker gateway"
            );
            Some(Arc::new(Self(self.0)))
        }
    }

    pub struct Client {
        pub app: Router,
        pub clock: Arc<FixedClock>,
        pub kite: Arc<ReplayKite>,
        _kite_dir: tempfile::TempDir,
    }

    pub struct Reply {
        pub status: StatusCode,
        pub headers: axum::http::HeaderMap,
        pub text: String,
    }

    impl Reply {
        pub fn json(&self) -> serde_json::Value {
            serde_json::from_str(&self.text).unwrap()
        }
    }

    impl Client {
        /// The mounted app over the scenario's SQLite file, DRY_RUN, the twt flag off, no
        /// token on disk, the replay Kite quoting `LAST_PRICES` (and `gtts`, when given).
        pub async fn new(
            db_path: &std::path::Path,
            gw: Gw,
            gtts: Option<serde_json::Value>,
        ) -> Self {
            let mut settings = Settings::defaults().unwrap();
            settings.dry_run = true;
            settings.twt_execution_enabled = false;
            settings.twt_auto_execute = false;
            settings.sole_user_id = 1;
            settings.sole_broker_account_id = 1;
            settings.kite_api_key = "replay-test-key".to_owned();
            settings.db_backend = "sqlite".to_owned();
            settings.db_path = db_path.display().to_string();
            let kite_dir = tempfile::tempdir().unwrap();
            settings.token_file = kite_dir
                .path()
                .join("no-such-token.json")
                .display()
                .to_string();
            let prices: &[(&str, i64, f64)] = if gw == Gw::Explodes {
                &[]
            } else {
                &LAST_PRICES
            };
            let mut quotes = serde_json::Map::new();
            for &(symbol, token, price) in prices {
                quotes.insert(
                    format!("NSE:{symbol}"),
                    serde_json::json!({"instrument_token": token, "last_price": price}),
                );
            }
            let mut routes = serde_json::Map::new();
            if let Some(g) = gtts {
                routes.insert("gtt".to_owned(), serde_json::json!({"data": g}));
            }
            let fixture = serde_json::json!({
                "session": {"authed": true},
                "routes": routes,
                "quotes": quotes,
            });
            std::fs::write(
                kite_dir.path().join("kite.json"),
                serde_json::to_vec_pretty(&fixture).unwrap(),
            )
            .unwrap();
            let kite =
                Arc::new(ReplayKite::load(kite_dir.path(), None, &settings.kite_api_key).unwrap());
            let clock = Arc::new(FixedClock::new(now()));
            let db = Db::open(DbConfig::from_settings(&settings, None))
                .await
                .unwrap();
            let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../../kite-momentum-rebalancer");
            let paths = DeskPaths {
                root: kite_dir.path().to_path_buf(),
                templates: root.join("app").join("templates"),
                static_dir: root.join("app").join("static"),
            };
            let state = build_state(StateParts {
                settings,
                clock: clock.clone(),
                ids: Arc::new(HarnessIds::new("twt-desk-tests")),
                db,
                kite: kite.clone(),
                paths,
                harness: None,
            });
            // `monkeypatch.setattr(W, "twt_gateway", lambda: _Gateway())`: the sleeve's gateway
            // is derived from this one.
            let state = AppState {
                gateway: Arc::new(TestGateway(gw)),
                ..state
            };
            Self {
                app: build_app(state),
                clock,
                kite,
                _kite_dir: kite_dir,
            }
        }

        async fn send(&self, request: Request<Body>) -> Reply {
            let r = self.app.clone().oneshot(request).await.unwrap();
            let status = r.status();
            let headers = r.headers().clone();
            let bytes = to_bytes(r.into_body(), usize::MAX).await.unwrap();
            Reply {
                status,
                headers,
                text: String::from_utf8(bytes.to_vec()).unwrap(),
            }
        }

        pub async fn get(&self, path: &str) -> Reply {
            self.send(
                Request::get(path)
                    .header(header::HOST, "127.0.0.1")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
        }

        /// `client.post(path, data=form)` from the desk's own origin.
        pub async fn post(&self, path: &str, form: &[(&str, &str)]) -> Reply {
            self.post_from(path, form, Some("http://127.0.0.1")).await
        }

        pub async fn post_from(
            &self,
            path: &str,
            form: &[(&str, &str)],
            origin: Option<&str>,
        ) -> Reply {
            let body: String = form
                .iter()
                .map(|(k, v)| format!("{k}={}", v.replace(':', "%3A")))
                .collect::<Vec<_>>()
                .join("&");
            let mut b = Request::post(path)
                .header(header::HOST, "127.0.0.1")
                .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded");
            if let Some(o) = origin {
                b = b.header(header::ORIGIN, o);
            }
            self.send(b.body(Body::from(body)).unwrap()).await
        }
    }

    /// `_forms(html, action)`.
    pub fn forms(html: &str, action: &str) -> Vec<String> {
        let re = regex::Regex::new(&format!(
            r#"(?s)<form[^>]*action="{}"[^>]*>.*?</form>"#,
            regex::escape(action)
        ))
        .unwrap();
        re.find_iter(html).map(|m| m.as_str().to_owned()).collect()
    }
}

use axum::http::StatusCode;
use routes::{Client, Gw, forms};

async fn a_position_on(
    store: &mut PgTwtStore,
    instrument_id: i64,
    quantity: i64,
    gtt: Option<&str>,
) -> i64 {
    let id = a_position(store, quantity, gtt).await;
    store
        .conn
        .execute(
            "UPDATE tw_position SET instrument_id = ? WHERE id = ?",
            &[Param::Int(instrument_id), Param::Int(id)],
        )
        .await
        .unwrap();
    id
}

async fn client(f: &Fixture) -> Client {
    Client::new(&f.path, Gw::Simulates, None).await
}

async fn buy_line(f: &mut Fixture, expires_in_minutes: i64) -> (String, i64) {
    let (plan_id, pk) = a_plan(&mut f.store, expires_in_minutes, 1).await;
    let line_id = a_line(
        &mut f.store,
        pk,
        &plan_id,
        "BUY_AT_OPEN",
        42,
        "TWTCO",
        "80.00",
        None,
    )
    .await;
    (plan_id, line_id)
}

fn confirm_form<'a>(
    plan_id: &'a str,
    line_id: &'a str,
    confirm: &'a str,
) -> [(&'a str, &'a str); 3] {
    [
        ("plan_id", plan_id),
        ("line_id", line_id),
        ("confirm", confirm),
    ]
}

fn capital(v: &Val) -> Decimal {
    match v {
        Val::Map(r) => r.dec("sleeve_capital_inr").unwrap(),
        _ => panic!("not a row"),
    }
}

async fn sleeve_capital(f: &mut Fixture) -> Decimal {
    capital(&Val::Map(f.store.config().await.unwrap()))
}

// --- TestThePageRenders ---------------------------------------------------------------------

#[tokio::test]
async fn the_page_renders_with_exits_entries_and_the_book() {
    let mut f = store().await;
    buy_line(&mut f, 25).await;
    a_position(&mut f.store, 1_000, Some("551234")).await;
    let c = client(&f).await;
    let html = c.get("/twt").await.text;
    assert!(html.contains("Three weeks tight"));
    assert!(html.contains("Exits") && html.contains("Entries") && html.contains("The book"));
    assert!(html.contains("SIMULATED"));
}

#[tokio::test]
async fn there_is_one_confirm_form_per_line_and_no_confirm_all() {
    let mut f = store().await;
    let (plan_id, pk) = a_plan(&mut f.store, 25, 1).await;
    a_line(
        &mut f.store,
        pk,
        &plan_id,
        "BUY_AT_OPEN",
        42,
        "TWTCO",
        "80.00",
        None,
    )
    .await;
    a_line(
        &mut f.store,
        pk,
        &plan_id,
        "ARM_GTT",
        43,
        "HELDCO",
        "80.00",
        None,
    )
    .await;
    let c = client(&f).await;
    let html = c.get("/twt").await.text;
    let found = forms(&html, "/twt/execute");
    assert_eq!(found.len(), 2);
    assert!(found.iter().all(|form| form.contains(r#"name="line_id""#)));
    assert!(!html.to_lowercase().contains("confirm all"));
}

#[tokio::test]
async fn an_expired_plans_buttons_are_absent_not_disabled() {
    let mut f = store().await;
    buy_line(&mut f, -1).await;
    let c = client(&f).await;
    let html = c.get("/twt").await.text;
    assert!(forms(&html, "/twt/execute").is_empty());
    assert!(!html.contains("disabled"));
    assert!(html.contains("plan expired"));
}

#[tokio::test]
async fn the_rearm_form_exists_only_for_a_naked_line() {
    let mut f = store().await;
    a_position_on(&mut f.store, 42, 1_000, Some("551234")).await;
    a_position_on(&mut f.store, 43, 1_000, None).await;
    let c = client(&f).await;
    let html = c.get("/twt").await.text;
    assert_eq!(forms(&html, "/twt/rearm").len(), 1);
    assert!(html.contains("NAKED"));
}

#[tokio::test]
async fn the_data_route_answers_json() {
    let mut f = store().await;
    let (plan_id, _) = buy_line(&mut f, 25).await;
    let c = client(&f).await;
    let payload = c.get("/twt/data").await.json();
    assert_eq!(payload["available"], serde_json::json!(true));
    assert_eq!(payload["plan"]["plan_id"], serde_json::json!(plan_id));
    let value = payload["lines"][0]["value_inr"].as_str().unwrap();
    assert_eq!(d(value), d("250000.00"));
}

// --- TestTheExecuteRoute --------------------------------------------------------------------

#[tokio::test]
async fn a_confirm_fills_and_arms_through_the_route() {
    let mut f = store().await;
    let (plan_id, line_id) = buy_line(&mut f, 25).await;
    let c = client(&f).await;
    let id = line_id.to_string();
    let answer = c
        .post("/twt/execute", &confirm_form(&plan_id, &id, "true"))
        .await;
    assert_eq!(answer.status, StatusCode::OK, "{}", answer.text);
    let body = answer.json();
    assert_eq!(body["status"], serde_json::json!("SIMULATED"));
    assert_eq!(body["line_id"], serde_json::json!(line_id));
    let position_id = body["position_id"].as_i64().unwrap();
    let position = f.store.position(position_id).await.unwrap().unwrap();
    assert!(!matches!(position.get("gtt_id"), None | Some(Val::None)));
    assert_eq!(position.dec("initial_stop"), Some(d("80.00")));
    // R8.TD1: the confirm's transaction is closed — this connection writes without waiting.
    assert!(f.store.lock_session_for_update(session()).await.is_ok());
    f.store.unlock_session().await.unwrap();
}

#[tokio::test]
async fn a_missing_confirm_is_a_four_hundred_through_the_route() {
    let mut f = store().await;
    let (plan_id, line_id) = buy_line(&mut f, 25).await;
    let c = client(&f).await;
    let id = line_id.to_string();
    let answer = c
        .post("/twt/execute", &confirm_form(&plan_id, &id, "false"))
        .await;
    assert_eq!(answer.status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn an_expired_plan_is_a_four_ten_through_the_route() {
    let mut f = store().await;
    let (plan_id, line_id) = buy_line(&mut f, -1).await;
    let c = client(&f).await;
    let id = line_id.to_string();
    let answer = c
        .post("/twt/execute", &confirm_form(&plan_id, &id, "true"))
        .await;
    assert_eq!(answer.status, StatusCode::GONE);
}

// --- TestTheUntouchableGuard ----------------------------------------------------------------

#[tokio::test]
async fn an_untouchable_instrument_comes_back_blocked_with_the_guards_words() {
    let mut f = store().await;
    let (plan_id, line_id) = buy_line(&mut f, 25).await;
    let c = Client::new(&f.path, Gw::GuardedPlace, None).await;
    let id = line_id.to_string();
    let answer = c
        .post("/twt/execute", &confirm_form(&plan_id, &id, "true"))
        .await;
    assert_eq!(answer.status, StatusCode::OK, "{}", answer.text);
    let body = answer.json();
    assert_eq!(body["status"], serde_json::json!("BLOCKED"));
    assert!(body["reason"].as_str().unwrap().contains("untouchable"));
    assert_eq!(body["simulated"], serde_json::json!(true));
}

#[tokio::test]
async fn a_rearm_of_an_untouchable_line_is_blocked_not_a_five_hundred() {
    let mut f = store().await;
    let position_id = a_position(&mut f.store, 1_000, None).await.to_string();
    let c = Client::new(&f.path, Gw::GuardedGtt, None).await;
    let body = c
        .post(
            "/twt/rearm",
            &[("position_id", position_id.as_str()), ("confirm", "true")],
        )
        .await
        .json();
    assert_eq!(body["status"], serde_json::json!("BLOCKED"));
    assert!(body["reason"].as_str().unwrap().contains("untouchable"));
}

// --- TestTheHaltRoute -----------------------------------------------------------------------

#[tokio::test]
async fn halt_zeroes_the_capital_expires_the_plans_and_answers_in_one_line() {
    let mut f = store().await;
    a_plan(&mut f.store, 25, 1).await;
    let c = client(&f).await;
    let answer = c.post("/twt/halt", &[("confirm", "true")]).await;
    assert_eq!(answer.status, StatusCode::OK, "{}", answer.text);
    let body = answer.json();
    assert_eq!(body["halted"], serde_json::json!(true));
    assert_eq!(
        d(body["sleeve_capital_inr_before"].as_str().unwrap()),
        d("2500000.00")
    );
    assert_eq!(body["plans_expired"], serde_json::json!(1));
    assert_eq!(body["message"].as_str().unwrap().matches('\n').count(), 0);
    assert_eq!(sleeve_capital(&mut f).await, d("0"));
}

#[tokio::test]
async fn halt_never_touches_protection() {
    let mut f = store().await;
    let position_id = a_position_on(&mut f.store, 43, 1_000, Some("551234")).await;
    let c = client(&f).await;
    c.post("/twt/halt", &[("confirm", "true")]).await;

    let after = f.store.position(position_id).await.unwrap().unwrap();
    assert_eq!(after.s("gtt_id"), "551234");
    assert_eq!(after.dec("gtt_trigger"), Some(d("80.00")));
    assert_eq!(after.int("quantity_open"), 1_000);
    assert_eq!(after.s("state"), "OPEN");

    let (plan_id, pk) = a_plan(&mut f.store, 25, 2).await;
    let line_id = a_line(
        &mut f.store,
        pk,
        &plan_id,
        "RAISE_GTT_STOP",
        43,
        "HELDCO",
        "104.00",
        Some("80.00"),
    )
    .await
    .to_string();
    let answer = c
        .post("/twt/execute", &confirm_form(&plan_id, &line_id, "true"))
        .await;
    assert_eq!(
        answer.json()["status"],
        serde_json::json!("SIMULATED"),
        "{}",
        answer.text
    );
    let after = f.store.position(position_id).await.unwrap().unwrap();
    assert_eq!(after.dec("gtt_trigger"), Some(d("104.00")));
}

#[tokio::test]
async fn a_halted_sleeve_cannot_buy() {
    let mut f = store().await;
    let c = client(&f).await;
    c.post("/twt/halt", &[("confirm", "true")]).await;
    let (plan_id, line_id) = buy_line(&mut f, 25).await;
    let id = line_id.to_string();
    let body = c
        .post("/twt/execute", &confirm_form(&plan_id, &id, "true"))
        .await
        .json();
    assert_eq!(body["status"], serde_json::json!("BLOCKED"));
    assert!(
        body["reason"]
            .as_str()
            .unwrap()
            .starts_with("NO_SLEEVE_CAPITAL")
    );
}

#[tokio::test]
async fn halt_without_a_confirm_is_a_four_hundred() {
    let mut f = store().await;
    let c = client(&f).await;
    assert_eq!(
        c.post("/twt/halt", &[("confirm", "no")]).await.status,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(sleeve_capital(&mut f).await, d("2500000.00"));
}

// --- TestTheRearmSweepAndReconcileRoutes ----------------------------------------------------

#[tokio::test]
async fn rearm_arms_one_naked_position() {
    let mut f = store().await;
    let position_id = a_position(&mut f.store, 1_000, None).await;
    let c = client(&f).await;
    let id = position_id.to_string();
    let body = c
        .post(
            "/twt/rearm",
            &[("position_id", id.as_str()), ("confirm", "true")],
        )
        .await
        .json();
    assert_eq!(body["status"], serde_json::json!("SIMULATED"));
    let after = f.store.position(position_id).await.unwrap().unwrap();
    assert!(!matches!(after.get("gtt_id"), None | Some(Val::None)));
}

#[tokio::test]
async fn rearm_without_a_confirm_is_a_four_hundred() {
    let mut f = store().await;
    let position_id = a_position(&mut f.store, 1_000, None).await.to_string();
    let c = client(&f).await;
    let answer = c
        .post(
            "/twt/rearm",
            &[("position_id", position_id.as_str()), ("confirm", "no")],
        )
        .await;
    assert_eq!(answer.status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn the_sweep_route_reports_zero_naked_when_it_is_clean() {
    let mut f = store().await;
    a_position(&mut f.store, 1_000, None).await;
    let c = client(&f).await;
    let body = c.post("/twt/sweep", &[("confirm", "true")]).await.json();
    assert_eq!(body["naked_before"], serde_json::json!(1));
    assert_eq!(body["naked"], serde_json::json!(0));
    let row = f.store.session(now().date_naive()).await.unwrap().unwrap();
    assert_eq!(row.int("naked_at_1515"), 0);
}

#[tokio::test]
async fn the_sweep_route_needs_a_confirm() {
    let f = store().await;
    let c = client(&f).await;
    assert_eq!(
        c.post("/twt/sweep", &[("confirm", "no")]).await.status,
        StatusCode::BAD_REQUEST
    );
}

#[tokio::test]
async fn the_reconcile_route_attaches_a_hand_armed_gtt() {
    let mut f = store().await;
    let position_id = a_position(&mut f.store, 1_000, None).await;
    // `_Gtts([{"gtt_id": 771122, "symbol": "HELDCO", "trigger": 81.5}])`, as Kite lists it.
    let gtts = serde_json::json!([{
        "id": 771_122,
        "status": "active",
        "condition": {"tradingsymbol": "HELDCO", "trigger_values": [81.5]},
    }]);
    let c = Client::new(&f.path, Gw::Simulates, Some(gtts)).await;
    let body = c
        .post("/twt/reconcile", &[("confirm", "true")])
        .await
        .json();
    assert_eq!(body["attached"], serde_json::json!(1));
    let after = f.store.position(position_id).await.unwrap().unwrap();
    assert_eq!(after.s("gtt_id"), "771122");
}

#[tokio::test]
async fn the_reconcile_route_needs_a_confirm() {
    let f = store().await;
    let c = client(&f).await;
    assert_eq!(
        c.post("/twt/reconcile", &[("confirm", "no")]).await.status,
        StatusCode::BAD_REQUEST
    );
}

// --- TestEveryPostNeedsAnOrigin -------------------------------------------------------------

#[tokio::test]
async fn a_post_without_an_origin_header_is_refused() {
    for (path, payload) in [
        (
            "/twt/execute",
            vec![
                ("plan_id", "00000000-0000-4000-8000-000000000009"),
                ("line_id", "1"),
                ("confirm", "true"),
            ],
        ),
        ("/twt/halt", vec![("confirm", "true")]),
        (
            "/twt/rearm",
            vec![("position_id", "1"), ("confirm", "true")],
        ),
        ("/twt/sweep", vec![("confirm", "true")]),
        ("/twt/reconcile", vec![("confirm", "true")]),
    ] {
        let mut f = store().await;
        let c = client(&f).await;
        let answer = c.post_from(path, &payload, None).await;
        assert_eq!(answer.status, StatusCode::FORBIDDEN, "{path}");
        assert!(
            answer.json()["detail"]
                .as_str()
                .unwrap()
                .contains("did not come from the desk"),
            "{path}"
        );
        // And nothing happened: the halt in particular did not zero anything.
        assert_eq!(sleeve_capital(&mut f).await, d("2500000.00"), "{path}");
        assert!(c.kite.calls().is_empty(), "{path}");
    }
}

#[tokio::test]
async fn a_post_from_another_sites_origin_is_refused() {
    let mut f = store().await;
    let c = client(&f).await;
    let answer = c
        .post_from(
            "/twt/halt",
            &[("confirm", "true")],
            Some("http://evil.example"),
        )
        .await;
    assert_eq!(answer.status, StatusCode::FORBIDDEN);
    assert_eq!(sleeve_capital(&mut f).await, d("2500000.00"));
}

#[tokio::test]
async fn the_page_itself_is_a_get_and_needs_no_origin() {
    let f = store().await;
    let c = client(&f).await;
    assert_eq!(c.get("/twt").await.status, StatusCode::OK);
    assert_eq!(c.get("/twt/data").await.status, StatusCode::OK);
}

// =============================================================================================
// tests/test_twt_scan_desk.py — TW12's Scan now button: two routes and one table
// =============================================================================================

const SCAN_DDL: &str = "CREATE TABLE tw_scan_run (\
  id INTEGER PRIMARY KEY AUTOINCREMENT, \
  user_id INTEGER NOT NULL, \
  requested_at TEXT NOT NULL, \
  started_at TEXT, \
  finished_at TEXT, \
  session_date TEXT, \
  status TEXT NOT NULL DEFAULT 'QUEUED', \
  source TEXT NOT NULL DEFAULT 'desk', \
  detail TEXT, \
  error TEXT, \
  task_id TEXT, \
  CHECK (status IN ('QUEUED', 'RUNNING', 'DONE', 'FAILED')), \
  CHECK (source IN ('desk', 'web', 'cli')))";

fn scan_now() -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(2026, 9, 11, 14, 20, 0).unwrap()
}

struct ScanFixture {
    dir: tempfile::TempDir,
    store: PgTwtStore,
    client: Client,
}

/// `store` + `clock` + `client`: a database holding **only** `tw_scan_run`, a gateway that
/// fails the test if it is asked for, and a broker that quotes nothing.
async fn scan_fixture() -> ScanFixture {
    baskfy_desk::storage::register_sqlite_decimal_adapter();
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("twt-scan.db");
    let mut conn = Db::connect_sqlite_path(&path).await.unwrap();
    conn.execute(SCAN_DDL, &[]).await.unwrap();
    let client = Client::new(&path, Gw::Explodes, None).await;
    client.clock.set(scan_now());
    ScanFixture {
        dir,
        store: PgTwtStore::new(conn, 1, "", None),
        client,
    }
}

async fn run_id_of(c: &Client) -> i64 {
    c.post("/twt/scan", &[]).await.json()["run_id"]
        .as_i64()
        .unwrap()
}

async fn set_status(store: &mut PgTwtStore, run_id: i64, status: &str) {
    store
        .conn
        .execute(
            "UPDATE tw_scan_run SET status = ? WHERE id = ?",
            &[txt(status), Param::Int(run_id)],
        )
        .await
        .unwrap();
}

#[tokio::test]
async fn a_scan_is_accepted_with_202_and_the_run_to_poll() {
    let mut s = scan_fixture().await;
    let answer = s.client.post("/twt/scan", &[]).await;
    assert_eq!(answer.status.as_u16(), 202);
    let body = answer.json();
    assert_eq!(body["status"], serde_json::json!("QUEUED"));
    let run_id = body["run_id"].as_i64().unwrap();
    assert!(run_id >= 1);
    assert_eq!(
        body["requested_at"],
        serde_json::json!(scan_now().to_rfc3339())
    );
    let run = s.store.scan_run(run_id).await.unwrap().unwrap();
    assert_eq!(run.s("status"), "QUEUED");
    assert_eq!(run.s("source"), "desk");
    assert!(matches!(run.get("task_id"), None | Some(Val::None)));
}

#[tokio::test]
async fn a_second_press_while_one_is_in_flight_is_409_naming_the_run() {
    let s = scan_fixture().await;
    let first = run_id_of(&s.client).await;
    let answer = s.client.post("/twt/scan", &[]).await;
    assert_eq!(answer.status, StatusCode::CONFLICT);
    assert!(
        answer.json()["detail"]
            .as_str()
            .unwrap()
            .contains(&format!("Scan {first} is queued"))
    );
}

#[tokio::test]
async fn a_press_a_second_after_a_finished_run_is_429_with_retry_after() {
    let mut s = scan_fixture().await;
    let run_id = run_id_of(&s.client).await;
    set_status(&mut s.store, run_id, "DONE").await;
    s.client.clock.advance(1.0);
    let answer = s.client.post("/twt/scan", &[]).await;
    assert_eq!(answer.status, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(answer.headers["Retry-After"], "59");
    assert!(
        answer.json()["detail"]
            .as_str()
            .unwrap()
            .contains("one a minute is the limit")
    );
}

#[tokio::test]
async fn a_press_after_the_minute_is_accepted_again() {
    let mut s = scan_fixture().await;
    let run_id = run_id_of(&s.client).await;
    set_status(&mut s.store, run_id, "DONE").await;
    s.client.clock.advance(61.0);
    let answer = s.client.post("/twt/scan", &[]).await;
    assert_eq!(answer.status.as_u16(), 202);
    assert_ne!(answer.json()["run_id"].as_i64().unwrap(), run_id);
}

#[tokio::test]
async fn a_stale_in_flight_row_does_not_lock_the_button_forever() {
    let s = scan_fixture().await;
    let first = run_id_of(&s.client).await;
    #[allow(clippy::cast_precision_loss)]
    s.client
        .clock
        .advance((w::SCAN_STALE_AFTER_SECONDS + 1) as f64);
    let answer = s.client.post("/twt/scan", &[]).await;
    assert_eq!(answer.status.as_u16(), 202);
    assert_ne!(answer.json()["run_id"].as_i64().unwrap(), first);
    assert_eq!(
        s.client.get(&format!("/twt/scan/{first}")).await.json()["status"],
        serde_json::json!("QUEUED")
    );
}

#[tokio::test]
async fn reading_a_run_gives_the_swing_vocabulary() {
    let mut s = scan_fixture().await;
    let run_id = run_id_of(&s.client).await;
    for status in ["QUEUED", "RUNNING", "DONE", "FAILED"] {
        set_status(&mut s.store, run_id, status).await;
        let answer = s.client.get(&format!("/twt/scan/{run_id}")).await;
        assert_eq!(answer.status, StatusCode::OK);
        assert_eq!(answer.json()["status"], serde_json::json!(status));
    }
}

#[tokio::test]
async fn a_finished_run_carries_its_session_and_its_funnel() {
    let mut s = scan_fixture().await;
    let run_id = run_id_of(&s.client).await;
    s.store
        .conn
        .execute(
            "UPDATE tw_scan_run SET status = 'DONE', session_date = '2026-09-10', \
             detail = '{\"signals\": 2, \"status\": \"OK\"}' WHERE id = ?",
            &[Param::Int(run_id)],
        )
        .await
        .unwrap();
    let body = s.client.get(&format!("/twt/scan/{run_id}")).await.json();
    assert_eq!(body["session_date"], serde_json::json!("2026-09-10"));
    assert_eq!(
        body["detail"],
        serde_json::json!({"signals": 2, "status": "OK"})
    );
    assert_eq!(body["error"], serde_json::Value::Null);
}

#[tokio::test]
async fn a_failed_run_carries_its_reason() {
    let mut s = scan_fixture().await;
    let run_id = run_id_of(&s.client).await;
    s.store
        .conn
        .execute(
            "UPDATE tw_scan_run SET status = 'FAILED', error = 'ValueError: nothing published' \
             WHERE id = ?",
            &[Param::Int(run_id)],
        )
        .await
        .unwrap();
    let body = s.client.get(&format!("/twt/scan/{run_id}")).await.json();
    assert_eq!(body["status"], serde_json::json!("FAILED"));
    assert_eq!(
        body["error"],
        serde_json::json!("ValueError: nothing published")
    );
}

#[tokio::test]
async fn an_unknown_run_is_404() {
    let s = scan_fixture().await;
    assert_eq!(
        s.client.get("/twt/scan/99999").await.status,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn another_users_run_is_404_and_not_somebody_elses_row() {
    let mut s = scan_fixture().await;
    s.store
        .conn
        .execute(
            "INSERT INTO tw_scan_run (user_id, requested_at, status) VALUES (2, ?, 'DONE')",
            &[txt(&scan_now().to_rfc3339())],
        )
        .await
        .unwrap();
    let rows = s
        .store
        .conn
        .execute("SELECT id FROM tw_scan_run WHERE user_id = 2", &[])
        .await
        .unwrap()
        .rows;
    let other = db_int(rows[0].get("id"));
    assert_eq!(
        s.client.get(&format!("/twt/scan/{other}")).await.status,
        StatusCode::NOT_FOUND
    );
}

// --- TestTheScanIsMoneyFree -----------------------------------------------------------------

#[tokio::test]
async fn the_scan_never_asks_for_a_gateway_or_a_quote() {
    // The fixture's gateway panics if a sleeve gateway is derived from it, and its broker quotes
    // nothing. The 202 is the proof; the empty call log says no quote was even asked for.
    let s = scan_fixture().await;
    assert_eq!(s.client.post("/twt/scan", &[]).await.status.as_u16(), 202);
    assert!(s.client.kite.calls().is_empty());
}

async fn scan_rows(store: &mut PgTwtStore) -> i64 {
    let rows = store
        .conn
        .execute("SELECT count(*) AS n FROM tw_scan_run", &[])
        .await
        .unwrap()
        .rows;
    db_int(rows[0].get("n"))
}

#[tokio::test]
async fn the_scan_writes_one_row_and_nothing_else() {
    // The scenario database contains **only** `tw_scan_run`, so a route that reached for a
    // config row, a position or a plan would raise `no such table` rather than pass quietly.
    let mut s = scan_fixture().await;
    s.client.post("/twt/scan", &[]).await;
    assert_eq!(scan_rows(&mut s.store).await, 1);
}

#[tokio::test]
async fn a_scan_post_without_an_origin_header_is_refused() {
    let mut s = scan_fixture().await;
    let answer = s.client.post_from("/twt/scan", &[], None).await;
    assert_eq!(answer.status, StatusCode::FORBIDDEN);
    assert_eq!(scan_rows(&mut s.store).await, 0);
}

#[tokio::test]
async fn reading_a_run_is_a_get_and_needs_no_origin() {
    let s = scan_fixture().await;
    assert_eq!(
        s.client.get("/twt/scan/1").await.status,
        StatusCode::NOT_FOUND
    ); // not 403
    drop(s.dir);
}

// --- TestTheTwoCopiesAgree: the copied constants, read back from the worker -----------------

fn worker_source() -> String {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../decile-blueprint/services/worker/src/baskfy_worker/tasks/twt_scan.py");
    std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

fn worker_constant(source: &str, name: &str) -> i64 {
    let re = regex::Regex::new(&format!(r"(?m)^{name}: Final = (\d+)$")).unwrap();
    let found = re
        .captures(source)
        .unwrap_or_else(|| panic!("{name} is not a module constant in that file any more"));
    found[1].parse().unwrap()
}

#[test]
fn the_stale_window_is_the_same_number_on_both_sides() {
    assert_eq!(
        w::SCAN_STALE_AFTER_SECONDS,
        worker_constant(&worker_source(), "STALE_AFTER_SECONDS")
    );
}

#[test]
fn the_minimum_interval_is_the_same_number_on_both_sides() {
    assert_eq!(
        w::SCAN_MIN_INTERVAL_SECONDS,
        worker_constant(&worker_source(), "MIN_INTERVAL_SECONDS")
    );
}

#[test]
fn the_in_flight_states_are_the_same_pair_on_both_sides() {
    assert_eq!(
        baskfy_desk::lanes::twt::store::SCAN_IN_FLIGHT,
        ["QUEUED", "RUNNING"]
    );
    assert!(
        worker_source().contains(r#"IN_FLIGHT: Final[tuple[str, ...]] = ("QUEUED", "RUNNING")"#)
    );
}

// =============================================================================================
// R8.TD1 on Postgres — the bed (a `rsd_*` clone; never the template)
// =============================================================================================

/// A broker that is never reached: the confirm below is refused before it.
#[derive(Debug)]
struct NeverGateway;

#[async_trait::async_trait]
impl baskfy_desk::gateway::Gateway for NeverGateway {
    async fn place(
        &self,
        _o: baskfy_desk::gateway::PlaceOrder,
    ) -> Result<baskfy_desk::wire::PyDict, baskfy_desk::gateway::GatewayError> {
        panic!("a refused confirm reached the gateway")
    }
    async fn place_gtt_stop(
        &self,
        _s: baskfy_desk::gateway::GttStop,
    ) -> Result<baskfy_desk::wire::PyDict, baskfy_desk::gateway::GatewayError> {
        panic!("a refused confirm reached the gateway")
    }
    async fn delete_gtt(
        &self,
        _r: baskfy_desk::gateway::DeleteGtt,
    ) -> Result<baskfy_desk::wire::PyDict, baskfy_desk::gateway::GatewayError> {
        panic!("a refused confirm reached the gateway")
    }
    async fn modify_gtt_quantity(
        &self,
        _r: baskfy_desk::gateway::ModifyGtt,
    ) -> Result<baskfy_desk::wire::PyDict, baskfy_desk::gateway::GatewayError> {
        panic!("a refused confirm reached the gateway")
    }
    async fn cancel_order(
        &self,
        _r: baskfy_desk::gateway::CancelOrder,
    ) -> Result<baskfy_desk::wire::PyDict, baskfy_desk::gateway::GatewayError> {
        panic!("a refused confirm reached the gateway")
    }
}

/// Two confirms of one session on two connections: the second finds the row held, is refused
/// 409 `SESSION_BUSY` before anything is sent, and leaves its connection outside any
/// transaction; what the first wrote inside its block is invisible until its exit commits it.
#[tokio::test]
#[ignore = "needs the bed: BASKFY_RS_TEST_DATABASE_URL naming an rsd_* clone of it, then --ignored"]
async fn on_postgres_the_lock_is_one_transaction_and_a_held_row_is_busy() {
    use baskfy_desk::lanes::twt::execute::{ExecError, execute_line};
    use baskfy_desk::lanes::twt::store::{SESSION_BUSY, StoreError};
    use baskfy_desk::storage::DbConfig;

    let url = std::env::var("BASKFY_RS_TEST_DATABASE_URL").unwrap();
    let name = url.rsplit('/').next().unwrap().split('?').next().unwrap();
    assert!(
        name.starts_with("rsd_"),
        "{name}: only an rsd_* clone is ever written"
    );
    let db = Db::open(DbConfig {
        backend: "postgres".to_owned(),
        schema: "public".to_owned(),
        database_url: url.clone(),
        db_path: std::path::PathBuf::new(),
    })
    .await
    .unwrap();
    let mut probe = db.connect().await.unwrap();
    let rows = probe
        .execute(
            "SELECT p.plan_id::text AS plan_id, p.user_id, l.id AS line_id \
             FROM tw_plan p JOIN tw_plan_line l ON l.plan_id = p.id \
             WHERE l.state = 'PROPOSED' ORDER BY l.id LIMIT 1",
            &[],
        )
        .await
        .unwrap()
        .rows;
    let plan_id = match rows[0].get("plan_id") {
        Some(baskfy_desk::storage::DbValue::Text(s)) => s.clone(),
        other => panic!("{other:?}"),
    };
    let user_id = db_int(rows[0].get("user_id"));
    let line_id = db_int(rows[0].get("line_id"));
    probe
        .execute(
            "UPDATE tw_plan SET expires_at = '2099-01-01 00:00:00+05:30' WHERE plan_id = ?",
            &[txt(&plan_id)],
        )
        .await
        .unwrap();
    let mut a = PgTwtStore::new(db.connect().await.unwrap(), user_id, "public", Some(1));
    let mut b = PgTwtStore::new(db.connect().await.unwrap(), user_id, "public", Some(1));
    let day = a
        .plan(&plan_id)
        .await
        .unwrap()
        .unwrap()
        .date("session_date")
        .unwrap();
    let before = b
        .session(day)
        .await
        .unwrap()
        .map_or(0, |r| r.int("confirms"));

    a.lock_session_for_update(day).await.unwrap();
    assert!(a.session_locked());
    assert!(matches!(
        b.lock_session_for_update(day).await,
        Err(StoreError::SessionBusy)
    ));
    assert!(!b.session_locked());

    // The same refusal through the confirm: 409, before the gateway, nothing written.
    let settings = Settings::defaults().unwrap();
    let refused = execute_line(
        &mut b,
        &NeverGateway,
        &settings,
        &plan_id,
        line_id,
        "true",
        now(),
        None,
    )
    .await;
    match refused {
        Err(ExecError::Http(h)) => {
            assert_eq!(h.status, 409);
            assert_eq!(h.detail, SESSION_BUSY);
        }
        other => panic!("{other:?}"),
    }
    assert_eq!(
        b.line(line_id).await.unwrap().unwrap().s("state"),
        "PROPOSED"
    );

    // A's writes are inside its transaction until the exit commits them.
    a.bump_session(
        day,
        SessionBump {
            mode: "DRY_RUN".to_owned(),
            confirms: 1,
            ..SessionBump::default()
        },
    )
    .await
    .unwrap();
    assert_eq!(
        b.session(day).await.unwrap().unwrap().int("confirms"),
        before
    );
    a.unlock_session().await.unwrap();
    assert!(!a.session_locked());
    assert_eq!(
        b.session(day).await.unwrap().unwrap().int("confirms"),
        before + 1
    );

    // And the row is free again.
    b.lock_session_for_update(day).await.unwrap();
    b.unlock_session().await.unwrap();
}
