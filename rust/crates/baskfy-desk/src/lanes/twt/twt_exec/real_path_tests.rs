//! `tests/test_twt_auto.py::TestTheRealPathStillRefuses` a second time, over the **real SQL
//! store** (`PgTwtStore` on SQLite, the same SQL the desk runs on Postgres with the schema prefix
//! `""`) where Python's runs over `AutoStore`: the runner's switch on, the sleeve's gates **dry**,
//! the real `execute_line` and the real `OrderGateway` over a replay Kite that answers nothing.
//! The in-memory twin is `auto_tests.rs`; this one proves the same four promises survive the SQL
//! (the cap counted by `created_at`, the line states, the GTT columns, the client ids).
//!
//! Python's own SQLite tests register `sqlite3.register_adapter(Decimal, str)`; here that is
//! `storage::register_sqlite_decimal_adapter` (R8.TD3) — test-only, process-wide, one-way. The
//! desk itself keeps sqlite3's refusal (R8.2).

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::str::FromStr;
use std::sync::Arc;

use async_trait::async_trait;
use chrono::{DateTime, Duration, FixedOffset, TimeZone};
use rust_decimal::Decimal;

use super::auto::{self as a, Collaborators};
use super::execute::{self as x, ExecOutcome, ExecResult};
use crate::clock::FixedClock;
use crate::fake_kite::ReplayKite;
use crate::gateway::{
    CancelOrder, DeleteGtt, Gateway, GatewayError, GttStop, ModifyGtt, PlaceOrder, ProductGates,
};
use crate::ids::RandomIds;
use crate::lanes::exec_core::order_gateway::{GatewayOptions, OrderGateway};
use crate::lanes::exec_core::risk::{RiskBacking, RiskConfig, RiskManager};
use crate::lanes::twt::common::db_int;
use crate::lanes::twt::store::{PgTwtStore, TwtStore};
use crate::settings::Settings;
use crate::storage::{Db, Param};
use crate::wire::{PyDict, PyValue};

const DDL: &[&str] = &[
    "CREATE TABLE instrument (id INTEGER PRIMARY KEY, symbol TEXT)",
    "CREATE TABLE ohlcv_daily (instrument_id INTEGER, date TEXT, close NUMERIC, adj_factor NUMERIC DEFAULT 1, PRIMARY KEY (instrument_id, date))",
    "CREATE TABLE tw_config (user_id INTEGER PRIMARY KEY, sleeve_capital_inr NUMERIC, max_open_positions INTEGER, max_position_pct NUMERIC, stop_pct NUMERIC, trail_pct NUMERIC, first_live_entries_left INTEGER, dry_run_sessions INTEGER, updated_at TEXT, updated_by TEXT)",
    "CREATE TABLE tw_config_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, key TEXT, old_value TEXT, new_value TEXT, changed_at TEXT, changed_by TEXT, note TEXT)",
    "CREATE TABLE tw_signal_daily (user_id INTEGER, date TEXT, instrument_id INTEGER, state TEXT, entry_reference_close NUMERIC, stop_preview NUMERIC, sessions_out_before INTEGER, rank_key INTEGER, turnover_avg_20 INTEGER, PRIMARY KEY (user_id, date, instrument_id))",
    "CREATE TABLE tw_plan (id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT, user_id INTEGER, session_date TEXT, source TEXT, built_at TEXT, expires_at TEXT, plan_hash TEXT, gate TEXT, sleeve_equity_inr NUMERIC, total_new_exposure_inr NUMERIC)",
    "CREATE TABLE tw_plan_line (id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, user_id INTEGER, instrument_id INTEGER, kind TEXT, state TEXT, quantity INTEGER, stop_price NUMERIC, value_inr NUMERIC, high_since NUMERIC, previous_trigger NUMERIC, note TEXT, client_id TEXT, journal_ref TEXT, order_id INTEGER, position_id INTEGER)",
    "CREATE TABLE tw_position (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, broker_account_id INTEGER, instrument_id INTEGER, order_id INTEGER, signal_date TEXT, entry_date TEXT, entry_avg NUMERIC, entry_adj_factor NUMERIC DEFAULT 1, quantity_entered INTEGER, quantity_open INTEGER, initial_stop NUMERIC, stop_price NUMERIC, high_since NUMERIC, high_since_date TEXT, gtt_id TEXT, gtt_trigger NUMERIC, gtt_armed_at TEXT, next_trigger NUMERIC, next_trigger_for TEXT, state TEXT, closed_on TEXT, exit_avg NUMERIC, close_reason TEXT, pnl_inr NUMERIC, return_pct NUMERIC, simulated INTEGER DEFAULT 1, half_size INTEGER DEFAULT 0, partial_done INTEGER DEFAULT 0, trail TEXT, partial_queued_for TEXT, partial_quantity INTEGER, exit_queued_for TEXT, exit_reason_queued TEXT)",
    "CREATE TABLE tw_order (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, broker_account_id INTEGER, instrument_id INTEGER, signal_date TEXT, side TEXT, quantity INTEGER, stop_price NUMERIC, state TEXT, broker_order_id TEXT, client_id TEXT, filled_quantity INTEGER DEFAULT 0, avg_fill_price NUMERIC, position_id INTEGER, simulated INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE tw_fill (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, position_id INTEGER, order_id INTEGER, side TEXT, quantity INTEGER, price NUMERIC, filled_at TEXT, journal_ref TEXT, simulated INTEGER DEFAULT 1)",
    "CREATE TABLE tw_session (user_id INTEGER, session_date TEXT, mode TEXT DEFAULT 'DRY_RUN', gate TEXT DEFAULT 'SHUT', states INTEGER DEFAULT 0, signals INTEGER DEFAULT 0, confirms INTEGER DEFAULT 0, fills INTEGER DEFAULT 0, ratchets INTEGER DEFAULT 0, exits INTEGER DEFAULT 0, naked_at_1515 INTEGER DEFAULT 0, first_live_entries_counted INTEGER DEFAULT 0, PRIMARY KEY (user_id, session_date))",
    "CREATE TABLE lv_protection_issue (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, sleeve TEXT NOT NULL, position_id INTEGER NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, seen_at TEXT NOT NULL, resolved_at TEXT)",
];

const KITE_FIXTURE: &str =
    r#"{"session": {"authed": true}, "routes": {}, "quotes": {}, "historical": {}}"#;

/// The signal session (`SESSION`).
const SESSION: &str = "2026-09-10";

fn ist() -> FixedOffset {
    FixedOffset::east_opt(5 * 3600 + 1800).unwrap()
}

/// Monday 21 Sep 2026, ten seconds past the open.
fn open() -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(2026, 9, 21, 9, 15, 10).unwrap()
}

/// 09:05, when `twt-morning` builds the plan.
fn built() -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(2026, 9, 21, 9, 5, 0).unwrap()
}

fn stamp(t: DateTime<FixedOffset>) -> String {
    t.format("%Y-%m-%d %H:%M:%S%.f%:z").to_string()
}

fn txt(s: &str) -> Param {
    Param::Text(s.to_owned())
}

fn flags(dry_run: bool, execution: bool, auto: bool) -> Settings {
    let mut s = Settings::defaults().unwrap();
    s.dry_run = dry_run;
    s.twt_execution_enabled = execution;
    s.twt_auto_execute = auto;
    s
}

/// The real `OrderGateway`, as the sleeve's sibling exposes it (`ShimGateway`'s verbs).
struct Real(OrderGateway);

#[async_trait]
impl Gateway for Real {
    async fn place(&self, order: PlaceOrder) -> Result<PyDict, GatewayError> {
        self.0.place(&order).await
    }
    async fn place_gtt_stop(&self, stop: GttStop) -> Result<PyDict, GatewayError> {
        self.0.place_gtt_stop(&stop).await
    }
    async fn delete_gtt(&self, req: DeleteGtt) -> Result<PyDict, GatewayError> {
        let id = PyValue::Int(req.gtt_id);
        self.0.delete_gtt_value(&req, &id).await
    }
    async fn modify_gtt_quantity(&self, req: ModifyGtt) -> Result<PyDict, GatewayError> {
        let id = PyValue::Int(req.gtt_id);
        self.0.modify_gtt_value(&req, &id).await
    }
    async fn cancel_order(&self, req: CancelOrder) -> Result<PyDict, GatewayError> {
        let id = PyValue::Str(req.order_id.clone());
        self.0.cancel_order_value(&req, &id).await
    }
}

struct Rig {
    dir: tempfile::TempDir,
    kite: Arc<ReplayKite>,
    gateway: Arc<dyn Gateway>,
    plan_id: String,
}

impl Rig {
    async fn store(&self) -> PgTwtStore {
        let conn = Db::connect_sqlite_path(&self.dir.path().join("twt.db"))
            .await
            .unwrap();
        PgTwtStore::new(conn, 1, "", Some(1))
    }

    async fn scalar(&self, sql: &str) -> i64 {
        let mut store = self.store().await;
        let cur = store.conn.execute(sql, &[]).await.unwrap();
        db_int(cur.rows[0].get("n"))
    }
}

/// `_four_buys()`: ₹25 lakh, one MORNING plan built today, four `BUY_AT_OPEN` lines.
async fn four_buys() -> Rig {
    crate::storage::register_sqlite_decimal_adapter();
    let dir = tempfile::tempdir().unwrap();
    let mut conn = Db::connect_sqlite_path(&dir.path().join("twt.db"))
        .await
        .unwrap();
    for stmt in DDL {
        conn.execute(stmt, &[]).await.unwrap();
    }
    conn.execute(
        "INSERT INTO tw_config (user_id, sleeve_capital_inr, max_open_positions, \
         max_position_pct, stop_pct, trail_pct, first_live_entries_left, dry_run_sessions, \
         updated_at, updated_by) \
         VALUES (1, '2500000.00', 10, '12.50', '20.00', '20.00', 10, 0, ?, 'test')",
        &[txt(&stamp(built()))],
    )
    .await
    .unwrap();
    let plan_id = "00000000-0000-4000-8000-000000000001".to_owned();
    conn.execute(
        "INSERT INTO tw_plan (plan_id, user_id, session_date, source, built_at, expires_at, \
         plan_hash, gate, sleeve_equity_inr, total_new_exposure_inr) \
         VALUES (?, 1, ?, 'MORNING', ?, ?, 'abc', 'OPEN', '2500000.00', '1000000.00')",
        &[
            txt(&plan_id),
            txt(SESSION),
            txt(&stamp(built())),
            txt(&stamp(built() + Duration::minutes(30))),
        ],
    )
    .await
    .unwrap();
    for (index, symbol) in ["AAA", "BBB", "CCC", "DDD"].iter().enumerate() {
        let instrument = 1001 + i64::try_from(index).unwrap();
        conn.execute(
            "INSERT INTO instrument (id, symbol) VALUES (?, ?)",
            &[Param::Int(instrument), txt(symbol)],
        )
        .await
        .unwrap();
        conn.execute(
            "INSERT INTO ohlcv_daily (instrument_id, date, close, adj_factor) \
             VALUES (?, ?, '100.00', 1)",
            &[Param::Int(instrument), txt(SESSION)],
        )
        .await
        .unwrap();
        conn.execute(
            "INSERT INTO tw_signal_daily (user_id, date, instrument_id, state, \
             entry_reference_close, stop_preview, sessions_out_before, rank_key, \
             turnover_avg_20) VALUES (1, ?, ?, 'SIGNAL', '100.00', '80.00', 5, 500000000, \
             500000000)",
            &[txt(SESSION), Param::Int(instrument)],
        )
        .await
        .unwrap();
        conn.execute(
            "INSERT INTO tw_plan_line (plan_id, user_id, instrument_id, kind, state, quantity, \
             stop_price, value_inr, high_since, previous_trigger, note, client_id) \
             VALUES (1, 1, ?, 'BUY_AT_OPEN', 'PROPOSED', 2500, '80.00', '250000.00', NULL, \
             NULL, '', ?)",
            &[Param::Int(instrument), txt(&format!("{plan_id}:{symbol}"))],
        )
        .await
        .unwrap();
    }
    drop(conn);

    std::fs::write(dir.path().join("kite.json"), KITE_FIXTURE).unwrap();
    std::fs::create_dir(dir.path().join("instruments")).unwrap();
    let kite = Arc::new(ReplayKite::load(dir.path(), None, "test-api-key").unwrap());
    let clock = Arc::new(FixedClock::new(open()));
    let risk = RiskManager::new(RiskConfig::default(), clock.clone(), RiskBacking::Memory)
        .await
        .unwrap();
    // The sleeve's own gates, fixed dry: `twt_gates()` with DRY_RUN true and the flag off.
    let gates = x::twt_gates(&flags(true, false, false));
    assert!(gates.dry_run);
    let core = OrderGateway::new(
        kite.clone(),
        Arc::new(risk),
        GatewayOptions {
            gates: Arc::new(move || -> ProductGates { gates }),
            journal_path: dir.path().join("twt_orders_journal.jsonl"),
            stop_band: Some(x::twt_stop_band().unwrap()),
            coverage: None,
            clock,
            ids: Arc::new(RandomIds),
            limits: None,
        },
    )
    .unwrap();
    Rig {
        dir,
        kite,
        gateway: Arc::new(Real(core)),
        plan_id,
    }
}

/// `_run(store, gateway=gateway)` with the runner forced on and the sleeve dry.
struct Runner<'a> {
    rig: &'a Rig,
}

#[async_trait]
impl Collaborators for Runner<'_> {
    fn settings(&self) -> Settings {
        flags(false, true, true)
    }
    fn now(&self) -> DateTime<FixedOffset> {
        open()
    }
    async fn open_store(&self) -> Result<Box<dyn TwtStore>, String> {
        Ok(Box::new(self.rig.store().await))
    }
    async fn gateway(&self) -> Arc<dyn Gateway> {
        self.rig.gateway.clone()
    }
    async fn price_for(&self, _symbol: &str) -> Option<Decimal> {
        Some(Decimal::from_str("100.00").unwrap())
    }
    async fn authed(&self) -> bool {
        true
    }
    async fn sleep(&self, _seconds: f64) {}
    async fn execute(
        &self,
        store: &mut dyn TwtStore,
        gateway: &dyn Gateway,
        _settings: &Settings,
        plan_id: &str,
        line_id: i64,
        now: DateTime<FixedOffset>,
        last_price: Option<Decimal>,
    ) -> ExecResult<ExecOutcome> {
        // The real `execute_line` with the sleeve's gates dry (`twt_gates().dry_run is True`).
        let dry = flags(true, false, false);
        x::execute_line(
            store, gateway, &dry, plan_id, line_id, "true", now, last_price,
        )
        .await
    }
}

async fn run(rig: &Rig) -> a::RunReport {
    a::run_once(&Runner { rig }, 0.0, 0.0).await.unwrap()
}

#[tokio::test]
async fn a_halted_sleeve_sends_nothing() {
    let rig = four_buys().await;
    {
        let mut store = rig.store().await;
        x::halt_sleeve(&mut store, "true", open() - Duration::seconds(5))
            .await
            .unwrap();
    }
    let report = run(&rig).await;
    assert!(!report.ran, "{report:?}");
    assert!(report.reason.starts_with("EXPIRED"), "{report:?}");
    assert!(report.reason.contains(&rig.plan_id), "{report:?}");
    assert_eq!(rig.scalar("SELECT COUNT(*) AS n FROM tw_order").await, 0);
    assert!(rig.kite.write_calls().is_empty());
}

#[tokio::test]
async fn the_per_session_cap_still_refuses_a_fourth_entry() {
    let rig = four_buys().await;
    let report = run(&rig).await;
    let statuses: Vec<&str> = report.attempts.iter().map(|(_, _, s)| s.as_str()).collect();
    assert_eq!(
        statuses,
        ["SIMULATED", "SIMULATED", "SIMULATED", "BLOCKED"],
        "{report:?}"
    );
    let mut store = rig.store().await;
    assert!(
        store
            .line(4)
            .await
            .unwrap()
            .unwrap()
            .s("note")
            .starts_with("SESSION_CAP")
    );
    // LV10.2: counted by the day the orders were sent — this morning, not the signal's date
    assert_eq!(store.entries_taken(open().date_naive()).await.unwrap(), 3);
    assert!(rig.kite.write_calls().is_empty());
}

#[tokio::test]
async fn re_running_does_not_double_send() {
    let rig = four_buys().await;
    run(&rig).await;
    let orders = rig.scalar("SELECT COUNT(*) AS n FROM tw_order").await;
    let fills = rig.scalar("SELECT COUNT(*) AS n FROM tw_fill").await;
    let again = run(&rig).await;
    assert!(again.attempts.is_empty(), "{again:?}");
    assert_eq!(
        rig.scalar("SELECT COUNT(*) AS n FROM tw_order").await,
        orders
    );
    assert_eq!(rig.scalar("SELECT COUNT(*) AS n FROM tw_fill").await, fills);
    assert_eq!((orders, fills), (3, 3));
    assert!(rig.kite.write_calls().is_empty());
}

#[tokio::test]
async fn every_buy_gets_its_stop_in_the_same_request() {
    let rig = four_buys().await;
    run(&rig).await;
    let mut store = rig.store().await;
    let positions = store.open_positions().await.unwrap();
    assert_eq!(positions.len(), 3);
    assert!(
        positions.iter().all(|p| !p.is_none("gtt_id")),
        "a buy was left without a stop"
    );
    assert!(
        positions
            .iter()
            .all(|p| p.s("gtt_id").starts_with(x::SIMULATED_GTT_PREFIX))
    );
}

#[tokio::test]
async fn every_order_carries_plan_id_colon_symbol() {
    let rig = four_buys().await;
    run(&rig).await;
    let mut store = rig.store().await;
    let cur = store
        .conn
        .execute("SELECT client_id FROM tw_order ORDER BY id", &[])
        .await
        .unwrap();
    let ids: Vec<String> = cur
        .rows
        .iter()
        .map(|r| crate::lanes::twt::common::db_text(r.get("client_id").unwrap()))
        .collect();
    let want: Vec<String> = ["AAA", "BBB", "CCC"]
        .iter()
        .map(|s| format!("{}:{s}", rig.plan_id))
        .collect();
    assert_eq!(ids, want);
}

#[tokio::test]
async fn a_naked_position_refuses_every_buy_before_the_gateway() {
    let rig = four_buys().await;
    {
        let mut store = rig.store().await;
        store
            .conn
            .execute(
                "INSERT INTO instrument (id, symbol) VALUES (2001, 'NAKED')",
                &[],
            )
            .await
            .unwrap();
        store
            .conn
            .execute(
                "INSERT INTO tw_position (user_id, broker_account_id, instrument_id, \
                 signal_date, entry_date, entry_avg, quantity_entered, quantity_open, \
                 initial_stop, stop_price, state, simulated) \
                 VALUES (1, 1, 2001, '2026-09-01', '2026-09-02', '100.00', 10, 10, '80.00', \
                 '80.00', 'OPEN', 1)",
                &[],
            )
            .await
            .unwrap();
    }
    let report = run(&rig).await;
    assert_eq!(report.attempts.len(), 4, "{report:?}");
    assert!(
        report.attempts.iter().all(|(_, _, s)| s == "BLOCKED"),
        "{report:?}"
    );
    let mut store = rig.store().await;
    for id in 1..=4 {
        let line = store.line(id).await.unwrap().unwrap();
        assert_eq!(line.s("state"), "REJECTED");
        assert!(
            line.s("note").starts_with("PROTECTION_UNRESOLVED"),
            "{line:?}"
        );
    }
    assert_eq!(rig.scalar("SELECT COUNT(*) AS n FROM tw_order").await, 0);
    assert!(rig.kite.write_calls().is_empty());
}

#[tokio::test]
async fn an_untouchable_instrument_is_refused_by_the_guards_and_the_rest_go() {
    let rig = four_buys().await;
    {
        let mut store = rig.store().await;
        store
            .conn
            .execute(
                "UPDATE instrument SET symbol = 'SGBMAR29' WHERE id = 1001",
                &[],
            )
            .await
            .unwrap();
    }
    {
        let mut store = rig.store().await;
        store
            .conn
            .execute("DELETE FROM tw_plan_line WHERE id = 4", &[])
            .await
            .unwrap();
    }
    let report = run(&rig).await;
    let got: Vec<(&str, &str)> = report
        .attempts
        .iter()
        .map(|(sym, _, s)| (sym.as_str(), s.as_str()))
        .collect();
    assert_eq!(
        got[..2],
        [("BBB", "SIMULATED"), ("CCC", "SIMULATED")],
        "{report:?}"
    );
    assert_eq!(got[2].0, "SGBMAR29", "{report:?}");
    assert_ne!(got[2].1, "SIMULATED", "{report:?}");
    let mut store = rig.store().await;
    let note = store.line(1).await.unwrap().unwrap().s("note");
    assert!(
        !note.starts_with("SESSION_CAP"),
        "the guard refused it, not the cap: {note}"
    );
    assert_eq!(
        rig.scalar(
            "SELECT COUNT(*) AS n FROM tw_order WHERE instrument_id = 1001 AND state <> 'REJECTED'"
        )
        .await,
        0
    );
    assert!(rig.kite.write_calls().is_empty());
}
