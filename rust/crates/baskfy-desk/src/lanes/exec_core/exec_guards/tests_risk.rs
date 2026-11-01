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

// --- wave D2: `_load` is strict where `_state_from` is tolerant (Python's own messages) -------

/// A same-day state file with `body` after the day, loaded the way Python's `_load` does.
async fn load_file(
    text: &str,
) -> (
    Result<RiskManager, crate::lanes::exec_core::risk::RiskError>,
    String,
) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("risk.json");
    std::fs::write(&path, text).unwrap();
    let m = RiskManager::new(
        RiskConfig::default(),
        clock(),
        RiskBacking::File(path.clone()),
    )
    .await;
    let after = std::fs::read_to_string(&path).unwrap();
    (m, after)
}

async fn state_of(text: &str) -> crate::lanes::exec_core::risk::RiskState {
    load_file(text).await.0.unwrap().state().await
}

async fn refusal_of(text: &str) -> (String, String) {
    let e = load_file(text).await.0.unwrap_err();
    (e.python_type, e.message)
}

#[tokio::test]
async fn a_same_day_file_converts_as_python_float_and_int_do() {
    // Expected values printed by Python 3.12 over `baskfy_execution.risk.RiskManager(state_path=)`.
    let s = state_of(
        r#"{"day":"2026-09-28","day_pnl":" -1_250.5 ","orders_today":"1_2","killed":1,"reasons":"ab","position_value":{"A":"1e3","B":true}}"#,
    )
    .await;
    assert_eq!(s.day_pnl, -1250.5);
    assert_eq!(s.orders_today, 12);
    assert!(s.killed);
    assert_eq!(
        s.reasons,
        ["a", "b"],
        "a string of reasons iterates its characters"
    );
    assert_eq!(
        s.position_value,
        [("A".to_owned(), 1000.0), ("B".to_owned(), 1.0)]
    );
    let s = state_of(r#"{"day":"2026-09-28","orders_today":7.9}"#).await;
    assert_eq!(s.orders_today, 7);
    let s = state_of(r#"{"day":"2026-09-28","reasons":{"k1":1,"k2":2}}"#).await;
    assert_eq!(s.reasons, ["k1", "k2"]);
    let s =
        state_of(r#"{"day":"2026-09-28","reasons":[1, 2.5, null, true, "x", [1,"a"], {"a":1}]}"#)
            .await;
    assert_eq!(
        s.reasons,
        ["1", "2.5", "None", "True", "x", "[1, 'a']", "{'a': 1}"]
    );
    let s = state_of(r#"{"day":"2026-09-28","position_value":0}"#).await;
    assert!(
        s.position_value.is_empty(),
        "a falsy position_value is {{}}"
    );
}

#[tokio::test]
async fn a_same_day_file_python_refuses_is_refused_with_its_message() {
    let float_none = (
        "TypeError".to_owned(),
        "float() argument must be a string or a real number, not 'NoneType'".to_owned(),
    );
    for (text, want) in [
        (r#"{"day":"2026-09-28","day_pnl":null}"#, float_none.clone()),
        (
            r#"{"day":"2026-09-28","day_pnl":[1]}"#,
            (
                "TypeError".to_owned(),
                "float() argument must be a string or a real number, not 'list'".to_owned(),
            ),
        ),
        (
            r#"{"day":"2026-09-28","orders_today":null}"#,
            (
                "TypeError".to_owned(),
                "int() argument must be a string, a bytes-like object or a real number, not \
                 'NoneType'"
                    .to_owned(),
            ),
        ),
        (
            r#"{"day":"2026-09-28","orders_today":"7.9"}"#,
            (
                "ValueError".to_owned(),
                "invalid literal for int() with base 10: '7.9'".to_owned(),
            ),
        ),
        (
            r#"{"day":"2026-09-28","orders_today":NaN}"#,
            (
                "ValueError".to_owned(),
                "cannot convert float NaN to integer".to_owned(),
            ),
        ),
        (
            r#"{"day":"2026-09-28","orders_today":Infinity}"#,
            (
                "OverflowError".to_owned(),
                "cannot convert float infinity to integer".to_owned(),
            ),
        ),
        (
            r#"{"day":"2026-09-28","reasons":5}"#,
            (
                "TypeError".to_owned(),
                "'int' object is not iterable".to_owned(),
            ),
        ),
        (
            r#"{"day":"2026-09-28","position_value":{"A":"x"}}"#,
            (
                "ValueError".to_owned(),
                "could not convert string to float: 'x'".to_owned(),
            ),
        ),
        (
            r#"{"day":"2026-09-28","position_value":{"A":null}}"#,
            float_none.clone(),
        ),
        // `day_pnl` is converted first.
        (
            r#"{"day":"2026-09-28","day_pnl":null,"orders_today":null}"#,
            float_none,
        ),
    ] {
        assert_eq!(refusal_of(text).await, want, "{text}");
    }
    let (kind, message) = refusal_of(r#"{"day":"2026-09-28","position_value":[1]}"#).await;
    assert_eq!(kind, "ValueError");
    assert!(
        message.starts_with("risk state position_value at ")
            && message.ends_with(" is not an object"),
        "{message}"
    );
}

#[tokio::test]
async fn non_finite_values_round_trip_as_json_dump_writes_them() {
    // `json.dump` writes NaN/-Infinity (allow_nan=True) and `json.load` reads them back.
    let s =
        state_of(r#"{"day":"2026-09-28","day_pnl":NaN,"position_value":{"X":-Infinity}}"#).await;
    assert!(s.day_pnl.is_nan());
    assert_eq!(s.position_value, [("X".to_owned(), f64::NEG_INFINITY)]);
    // A string that merely says NaN stays a string (only bare tokens are numbers).
    let s = state_of(r#"{"day":"2026-09-28","reasons":["NaN", "x Infinity"]}"#).await;
    assert_eq!(s.reasons, ["NaN", "x Infinity"]);
    // And the writer: a non-finite P&L is persisted, not refused.
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("risk.json");
    let m = RiskManager::new(
        RiskConfig::default(),
        clock(),
        RiskBacking::File(path.clone()),
    )
    .await
    .unwrap();
    m.on_pnl(f64::NAN).await.unwrap();
    let text = std::fs::read_to_string(&path).unwrap();
    assert!(text.contains("\"day_pnl\": NaN"), "{text}");
}

#[tokio::test]
async fn a_day_that_is_not_todays_string_starts_a_fresh_day_and_rewrites_the_file() {
    let (m, after) = load_file(r#"{"day":20260928,"orders_today":3}"#).await;
    assert_eq!(m.unwrap().state().await.orders_today, 0);
    // Byte for byte what Python's `_persist` wrote for the same file.
    assert_eq!(
        after,
        r#"{"day": "2026-09-28", "day_pnl": 0.0, "orders_today": 0, "killed": false, "reasons": [], "position_value": {}}"#
    );
}

#[test]
fn the_shared_rows_reading_stays_tolerant_but_int_still_raises_on_nan() {
    use crate::lanes::exec_core::risk::state_from;
    use crate::wire::PyValue;
    let raw = dict([("orders_today", PyValue::Float(f64::NAN))]);
    let e = state_from(&raw, "2026-09-28").unwrap_err();
    assert_eq!(
        (e.python_type.as_str(), e.message.as_str()),
        ("ValueError", "cannot convert float NaN to integer")
    );
    let raw = dict([
        ("orders_today", PyValue::Float(2.9)),
        ("day_pnl", PyValue::Str("5".to_owned())),
        ("reasons", PyValue::Str("ab".to_owned())),
        ("killed", PyValue::Str("x".to_owned())),
        ("day", PyValue::Int(0)),
    ]);
    let (day, s) = state_from(&raw, "2026-09-28").unwrap();
    assert_eq!(day, "2026-09-28");
    assert_eq!((s.day_pnl, s.orders_today, s.killed), (0.0, 2, true));
    assert!(s.reasons.is_empty());
}

#[test]
fn a_limit_override_is_read_as_python_float_reads_it() {
    let mut s = crate::settings::Settings::defaults().unwrap();
    s.risk_max_position_value = "1_500_000".to_owned();
    s.risk_max_gross_exposure = " 2e7 ".to_owned();
    let cfg = risk_config(0.0, &s).unwrap();
    assert_eq!(cfg.max_position_value, 1_500_000.0);
    assert_eq!(cfg.max_gross_exposure, 20_000_000.0);
    s.risk_max_position_value = " ".to_owned();
    let e = risk_config(0.0, &s).unwrap_err();
    assert_eq!(
        (e.python_type.as_str(), e.message.as_str()),
        ("ValueError", "could not convert string to float: ' '")
    );
    s.risk_max_position_value = "1__0".to_owned();
    assert!(risk_config(0.0, &s).is_err());
}

// --- test_risk_store.py::test_the_gateway_wires_the_row_only_on_postgres -----------------------

#[tokio::test]
async fn test_the_gateway_wires_the_row_only_on_postgres() {
    use crate::lanes::exec_core::desk::SharedRisk;
    let dir = tempfile::tempdir().unwrap();
    let settings = || {
        Arc::new(std::sync::RwLock::new(
            crate::settings::Settings::defaults().unwrap(),
        ))
    };
    // SQLite: the in-memory state it always had.
    let sqlite = Db::open(DbConfig {
        backend: "sqlite".to_owned(),
        schema: String::new(),
        database_url: String::new(),
        db_path: dir.path().join("desk.db"),
    })
    .await
    .unwrap();
    let risk = SharedRisk::new(settings(), clock(), sqlite);
    assert_eq!(risk.get().await.unwrap().backing_kind(), "memory");
    // Postgres: the shared `risk_ledger` row. The "server" is a loopback socket that answers
    // every connection with bytes that are not the Postgres protocol, so the store's lock is what
    // fails — proof that the row was wired (an in-memory manager would have been built). No
    // database is touched.
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        use tokio::io::AsyncWriteExt as _;
        while let Ok((mut socket, _)) = listener.accept().await {
            let _ = socket.write_all(b"not postgres\n").await;
        }
    });
    let pg = Db::open(DbConfig {
        backend: "postgres".to_owned(),
        schema: "desk".to_owned(),
        database_url: format!("postgresql://nobody:nothing@127.0.0.1:{port}/none"),
        db_path: dir.path().join("unused.db"),
    })
    .await
    .unwrap();
    let risk = SharedRisk::new(settings(), clock(), pg);
    let built = tokio::time::timeout(std::time::Duration::from_secs(60), risk.get())
        .await
        .expect("the store's connect did not give up");
    let e = built.unwrap_err();
    assert_eq!(e.python_type, "StorageError", "{e}");
}
