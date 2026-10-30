//! Ports `packages/execution/tests/test_risk_store.py` (one account, many processes: every
//! decision reloads the shared row under its lock) and the desk's `tests/test_risk_store.py`
//! (`PgRiskStateStore` over a SQLite file, `BEGIN IMMEDIATE` standing in for `FOR UPDATE`),
//! plus `risk_config`/`risk_coherence`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::Arc;

use chrono::{NaiveDate, TimeZone as _};

use super::super::risk::{
    PgRiskStateStore, RiskBacking, RiskConfig, RiskLayer, RiskManager, RiskStateStore,
    SharedMemoryStore, risk_coherence, risk_config,
};
use crate::clock::{Clock, FixedClock, ist};
use crate::storage::{Db, DbConfig};
use crate::wire::{PyValue, dict};

fn clock() -> Arc<dyn Clock> {
    Arc::new(FixedClock::new(
        ist().with_ymd_and_hms(2026, 9, 28, 10, 0, 0).unwrap(),
    ))
}

async fn manager(cfg: RiskConfig, store: &SharedMemoryStore) -> RiskManager {
    RiskManager::new(cfg, clock(), RiskBacking::Store(Arc::new(store.clone())))
        .await
        .unwrap()
}

fn cap(max_position_value: f64) -> RiskConfig {
    RiskConfig {
        max_position_value,
        ..RiskConfig::default()
    }
}

fn positions(store: &SharedMemoryStore) -> PyValue {
    store
        .0
        .row()
        .and_then(|r| r.get("position_value").cloned())
        .unwrap_or(PyValue::None)
}

fn pv(pairs: &[(&str, f64)]) -> PyValue {
    PyValue::Map(dict(pairs.iter().map(|(k, v)| (*k, PyValue::Float(*v)))))
}

#[tokio::test]
async fn test_pre_order_reloads_under_the_lock_and_saves_after() {
    let store = SharedMemoryStore::default();
    let m = manager(RiskConfig::default(), &store).await;
    let locks_before = store.0.counts().0;
    let verdict = m.pre_order("A", 1_000.0, 1_000.0, "BUY").await.unwrap();
    assert_eq!(verdict, (true, "ok".to_owned()));
    assert_eq!(store.0.counts().0, locks_before + 1);
    assert_eq!(
        store.0.row().unwrap().get("orders_today"),
        Some(&PyValue::Int(1))
    );
    assert_eq!(positions(&store), pv(&[("A", 1_000.0)]));
}

#[tokio::test]
async fn test_two_processes_cannot_spend_the_same_cap_twice() {
    let store = SharedMemoryStore::default();
    let swing = manager(cap(10_000.0), &store).await;
    let twt = manager(cap(10_000.0), &store).await;
    assert!(
        swing
            .pre_order("SAME", 6_000.0, 6_000.0, "BUY")
            .await
            .unwrap()
            .0
    );
    let (ok, why) = twt
        .pre_order("SAME", 6_000.0, 6_000.0, "BUY")
        .await
        .unwrap();
    assert!(!ok);
    assert!(why.contains("SAME position value 12,000 > cap"), "{why}");
    let alone = RiskManager::new(cap(10_000.0), clock(), RiskBacking::Memory)
        .await
        .unwrap();
    assert!(
        alone
            .pre_order("SAME", 6_000.0, 6_000.0, "BUY")
            .await
            .unwrap()
            .0
    );
}

#[tokio::test]
async fn test_the_order_counter_is_account_wide() {
    let store = SharedMemoryStore::default();
    let cfg = RiskConfig {
        max_orders_per_day: 3,
        ..RiskConfig::default()
    };
    let swing = manager(cfg.clone(), &store).await;
    let twt = manager(cfg, &store).await;
    assert!(swing.pre_order("A", 1.0, 1.0, "BUY").await.unwrap().0);
    assert!(twt.pre_order("B", 1.0, 1.0, "BUY").await.unwrap().0);
    assert!(swing.pre_order("C", 1.0, 1.0, "BUY").await.unwrap().0);
    let (ok, why) = twt.pre_order("D", 1.0, 1.0, "BUY").await.unwrap();
    assert!(!ok && why == "own daily order cap reached");
}

#[tokio::test]
async fn test_release_gives_an_unfilled_reservation_back_to_every_process() {
    let store = SharedMemoryStore::default();
    let swing = manager(cap(10_000.0), &store).await;
    let twt = manager(cap(10_000.0), &store).await;
    assert!(
        swing
            .pre_order("X", 8_000.0, 8_000.0, "BUY")
            .await
            .unwrap()
            .0
    );
    assert!(!twt.pre_order("X", 4_000.0, 4_000.0, "BUY").await.unwrap().0);
    swing.release("X", 6_000.0).await.unwrap();
    assert!(twt.pre_order("X", 4_000.0, 4_000.0, "BUY").await.unwrap().0);
    assert_eq!(positions(&store), pv(&[("X", 6_000.0)]));
}

#[tokio::test]
async fn test_seed_positions_counts_a_manual_holding_toward_exposure() {
    let store = SharedMemoryStore::default();
    let swing = manager(cap(10_000.0), &store).await;
    let twt = manager(cap(10_000.0), &store).await;
    swing
        .seed_positions(&[("HELD".to_owned(), 9_500.0), ("ZERO".to_owned(), 0.0)])
        .await
        .unwrap();
    let (ok, why) = twt
        .pre_order("HELD", 1_000.0, 1_000.0, "BUY")
        .await
        .unwrap();
    assert!(
        !ok && why.contains("HELD position value 10,500 > cap"),
        "{why}"
    );
    assert!(
        twt.pre_order("NEW", 7_000.0, 7_000.0, "BUY")
            .await
            .unwrap()
            .0
    );
    swing
        .seed_positions(&[("NEW".to_owned(), 100.0)])
        .await
        .unwrap();
    let PyValue::Map(p) = positions(&store) else {
        panic!("no positions")
    };
    assert_eq!(
        p.get("NEW"),
        Some(&PyValue::Float(7_000.0)),
        "a seed never lowers"
    );
}

#[tokio::test]
async fn test_kill_in_one_process_is_seen_by_the_other() {
    let store = SharedMemoryStore::default();
    let swing = manager(RiskConfig::default(), &store).await;
    let twt = manager(RiskConfig::default(), &store).await;
    assert!(twt.pre_order("A", 1.0, 1.0, "BUY").await.unwrap().0);
    swing.kill("operator").await.unwrap();
    let (ok, why) = twt.pre_order("B", 1.0, 1.0, "BUY").await.unwrap();
    assert!(!ok && why.starts_with("KILL SWITCH: operator"), "{why}");
    assert_eq!(
        RiskLayer::kill_switch_reason(&twt).await.unwrap(),
        "KILL SWITCH: operator"
    );
}

#[tokio::test]
async fn test_restart_preserves_the_day() {
    let store = SharedMemoryStore::default();
    let first = manager(RiskConfig::default(), &store).await;
    assert!(first.pre_order("A", 5.0, 5.0, "BUY").await.unwrap().0);
    first.on_pnl(-12.5).await.unwrap();
    drop(first);
    let again = manager(RiskConfig::default(), &store).await;
    let s = again.state().await;
    assert_eq!(s.orders_today, 1);
    assert!((s.day_pnl + 12.5).abs() < f64::EPSILON);
    assert_eq!(s.position_value, vec![("A".to_owned(), 5.0)]);
}

#[tokio::test]
async fn test_the_json_file_path_still_works_without_a_store() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("risk.json");
    let m = RiskManager::new(
        RiskConfig::default(),
        clock(),
        RiskBacking::File(path.clone()),
    )
    .await
    .unwrap();
    assert!(m.pre_order("A", 10.0, 10.0, "BUY").await.unwrap().0);
    let saved: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
    assert_eq!(saved["orders_today"], 1);
    assert_eq!(saved["position_value"], serde_json::json!({"A": 10.0}));
    let again = RiskManager::new(RiskConfig::default(), clock(), RiskBacking::File(path))
        .await
        .unwrap();
    assert_eq!(again.state().await.orders_today, 1);
}

#[tokio::test]
async fn test_without_a_store_or_a_path_the_state_is_in_memory_as_before() {
    let m = RiskManager::new(RiskConfig::default(), clock(), RiskBacking::Memory)
        .await
        .unwrap();
    assert!(m.pre_order("A", 10.0, 10.0, "BUY").await.unwrap().0);
    m.refresh().await.unwrap();
    assert_eq!(m.state().await.orders_today, 1);
    assert_eq!(RiskLayer::kill_switch_reason(&m).await.unwrap(), "");
}

#[tokio::test]
async fn the_daily_loss_cap_trips_the_kill_switch() {
    let m = RiskManager::new(RiskConfig::default(), clock(), RiskBacking::Memory)
        .await
        .unwrap();
    m.on_pnl(-100_000.0).await.unwrap();
    let (ok, why) = m.pre_order("A", 1.0, 1.0, "BUY").await.unwrap();
    assert!(!ok);
    assert_eq!(why, "KILL SWITCH: daily loss cap hit (-100,000)");
}

#[tokio::test]
async fn a_sell_projects_its_own_size_and_frees_the_reservation() {
    let m = RiskManager::new(cap(10_000.0), clock(), RiskBacking::Memory)
        .await
        .unwrap();
    assert!(m.pre_order("A", 9_000.0, 9_000.0, "BUY").await.unwrap().0);
    assert!(m.pre_order("A", 4_000.0, 0.0, "SELL").await.unwrap().0);
    assert_eq!(m.state().await.position("A"), 5_000.0);
    let (ok, why) = m.pre_order("B", 1.0, 12_000_001.0, "BUY").await.unwrap();
    assert!(!ok && why == "gross exposure cap");
}

// --- the desk's PgRiskStateStore over a SQLite file (tests/test_risk_store.py) ---------------

async fn sqlite_store(dir: &std::path::Path) -> PgRiskStateStore {
    let path = dir.join("desk.db");
    let db = Db::open(DbConfig {
        backend: "sqlite".to_owned(),
        schema: String::new(),
        database_url: String::new(),
        db_path: path.clone(),
    })
    .await
    .unwrap();
    let mut conn = db.connect().await.unwrap();
    conn.execute(
        "CREATE TABLE risk_ledger (user_id INTEGER NOT NULL, day DATE NOT NULL, \
         payload TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, \
         PRIMARY KEY (user_id, day))",
        &[],
    )
    .await
    .unwrap();
    drop(conn);
    PgRiskStateStore::new(
        db,
        1,
        "",
        Arc::new(|| NaiveDate::from_ymd_opt(2026, 9, 28).unwrap_or_default()),
    )
}

#[tokio::test]
async fn test_the_row_is_read_and_written_only_under_the_lock() {
    let dir = tempfile::tempdir().unwrap();
    let store = sqlite_store(dir.path()).await;
    let e = store
        .save_unlocked(&dict([("day", PyValue::Str("x".to_owned()))]))
        .await;
    assert_eq!(e.unwrap_err().python_type, "RuntimeError");
    let mut txn = store.lock().await.unwrap();
    assert!(
        txn.load().await.unwrap().is_none(),
        "a bare day row reads as no state"
    );
    let payload = dict([
        ("day", PyValue::Str("2026-09-28".to_owned())),
        ("orders_today", PyValue::Int(2)),
        ("position_value", pv(&[("A", 5.0)])),
    ]);
    txn.save(&payload).await.unwrap();
    txn.commit().await.unwrap();
    let mut txn = store.lock().await.unwrap();
    assert_eq!(txn.load().await.unwrap(), Some(payload));
    txn.commit().await.unwrap();
}

#[tokio::test]
async fn test_two_managers_over_the_desk_row_share_one_cap() {
    let dir = tempfile::tempdir().unwrap();
    let store: Arc<dyn RiskStateStore> = Arc::new(sqlite_store(dir.path()).await);
    let desk = RiskManager::new(cap(10_000.0), clock(), RiskBacking::Store(store.clone()))
        .await
        .unwrap();
    let twt_auto = RiskManager::new(cap(10_000.0), clock(), RiskBacking::Store(store.clone()))
        .await
        .unwrap();
    assert!(
        desk.pre_order("SAME", 6_000.0, 6_000.0, "BUY")
            .await
            .unwrap()
            .0
    );
    let (ok, why) = twt_auto
        .pre_order("SAME", 6_000.0, 6_000.0, "BUY")
        .await
        .unwrap();
    assert!(!ok && why.contains("12,000 > cap"), "{why}");
    desk.kill("operator").await.unwrap();
    let (_, why) = twt_auto.pre_order("OTHER", 1.0, 1.0, "BUY").await.unwrap();
    assert!(why.starts_with("KILL SWITCH"), "{why}");
    let mut txn = store.lock().await.unwrap();
    let row = txn.load().await.unwrap().unwrap();
    txn.commit().await.unwrap();
    assert_eq!(row.get("killed"), Some(&PyValue::Bool(true)));
    assert_eq!(row.get("orders_today"), Some(&PyValue::Int(1)));
}

// --- risk_config / risk_coherence -----------------------------------------------------------

#[test]
fn the_limits_derive_from_nav_and_never_contradict_the_strategy() {
    let s = crate::settings::Settings::defaults().unwrap();
    let cfg = risk_config(0.0, &s).unwrap();
    assert_eq!(cfg.max_daily_loss, 100_000.0);
    assert_eq!(cfg.max_position_value, 1_500_000.0);
    assert_eq!(cfg.max_gross_exposure, 12_000_000.0);
    let nav = 10_000_000.0;
    let cfg = risk_config(nav, &s).unwrap();
    assert!(
        risk_coherence(&cfg, nav).is_empty(),
        "{:?}",
        risk_coherence(&cfg, nav)
    );
    let tight = RiskConfig {
        max_position_value: 1.0,
        max_gross_exposure: 1.0,
        max_orders_per_day: 1,
        ..cfg
    };
    assert_eq!(risk_coherence(&tight, nav).len(), 3);
}
