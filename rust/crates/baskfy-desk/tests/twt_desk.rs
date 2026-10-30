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
}

async fn store() -> Fixture {
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
    Fixture {
        _dir: dir,
        store: PgTwtStore::new(conn, 1, "", Some(1)),
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
