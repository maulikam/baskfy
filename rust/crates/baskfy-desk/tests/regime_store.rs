//! `tests/test_regime_data.py`'s store tests (previews vs canonical commit, exposure
//! reconciliation, book snapshots) and the desk config's carriage onto the core engine.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

use std::collections::BTreeMap;

use baskfy_desk::lanes::analytics::index_cache::Candle;
use baskfy_desk::lanes::analytics::regime::RegimeConfig;
use baskfy_desk::lanes::analytics::regime::engine::{
    self as e, BookWeights, BreadthReading, CoreConfig, EvaluateArgs, ExecutionStatus,
    ExposureSnapshot, IndexSignals, MaSignal, PreviousRegime, RegimeMode, RegimeState, RegimeTier,
    SignalState, StrMap, WeightSource,
};
use baskfy_desk::lanes::analytics::regime_store::{self as rs, StoreError};
use baskfy_desk::storage::{Conn, Db, DbConfig, DbValue};
use baskfy_desk::wire::{PyDict, PyValue};
use chrono::{Duration, NaiveDate};

const NOW: &str = "2026-06-05T18:00:00";
const HEX: &str = "0123456789abcdef0123456789abcdef";

fn d(y: i32, m: u32, day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, day).unwrap()
}

fn core() -> CoreConfig {
    RegimeConfig::default().to_core().unwrap()
}

async fn sqlite() -> (tempfile::TempDir, Conn) {
    let dir = tempfile::tempdir().unwrap();
    let mut conn = Db::connect_sqlite_path(&dir.path().join("p.db"))
        .await
        .unwrap();
    conn.migrate().await.unwrap();
    (dir, conn)
}

fn signals(name: &str, week: NaiveDate, cfg: &CoreConfig) -> IndexSignals {
    IndexSignals {
        index_name: name.to_owned(),
        as_of_date: Some(week),
        close: Some(100.0),
        signals: cfg
            .ma_lengths
            .iter()
            .map(|m| {
                (
                    *m,
                    MaSignal {
                        ma_length: *m,
                        close: 100.0,
                        ma: Some(95.0),
                        distance_pct: Some(5.0),
                        state: SignalState::Above,
                        confirming_closes: 3,
                    },
                )
            })
            .collect(),
        is_stale: false,
        has_data: true,
        instrument_token: None,
    }
}

/// `make_state(cfg, week_end=, previous=, tier_actual=)`.
pub fn make_state(week: NaiveDate, previous: Option<&PreviousRegime>, actual: f64) -> RegimeState {
    let cfg = core();
    let idx: BTreeMap<String, IndexSignals> = cfg
        .structural_indices
        .values()
        .map(|n| (n.clone(), signals(n, week, &cfg)))
        .collect();
    let sentinel = signals(&cfg.momentum_sentinel, week, &cfg);
    let brd = BreadthReading {
        as_of_date: week,
        pct_above_20dma: 70.0,
        eligible_count: 500,
        observed_count: 500,
        coverage_pct: 100.0,
        universe_id: "scan".to_owned(),
        universe_hash: "h".to_owned(),
        audit_run_id: "run".to_owned(),
        calculation_version: "v1".to_owned(),
    };
    let book = e::resolve_book_weights(&cfg, None, None).unwrap();
    let exposure = ExposureSnapshot::new(actual);
    e::evaluate(&EvaluateArgs {
        as_of_date: week,
        scheduled_week_end: week,
        signal_session_date: week,
        index_signals: &idx,
        sentinel: Some(&sentinel),
        breadth: Some(&brd),
        book: &book,
        exposure: &exposure,
        cfg: &cfg,
        previous,
    })
    .unwrap()
}

fn st() -> RegimeState {
    make_state(d(2026, 6, 5), None, 50.0)
}

fn text(r: &PyDict, k: &str) -> String {
    match r.get(k) {
        Some(PyValue::Str(s)) => s.clone(),
        other => panic!("{k}: {other:?}"),
    }
}

fn int(r: &PyDict, k: &str) -> i64 {
    match r.get(k) {
        Some(PyValue::Int(i)) => *i,
        Some(PyValue::Bool(b)) => i64::from(*b),
        other => panic!("{k}: {other:?}"),
    }
}

fn float(r: &PyDict, k: &str) -> f64 {
    match r.get(k) {
        Some(PyValue::Float(f)) => *f,
        Some(PyValue::Int(i)) => *i as f64,
        other => panic!("{k}: {other:?}"),
    }
}

fn json(r: &PyDict, k: &str) -> serde_json::Value {
    serde_json::from_str(&text(r, k)).unwrap()
}

async fn commit(
    conn: &mut Conn,
    s: &RegimeState,
    snap: Option<&PyDict>,
) -> Result<&'static str, StoreError> {
    rs::commit_evaluation(conn, s, RegimeMode::Enforce, false, snap, NOW).await
}

async fn preview(conn: &mut Conn, s: &RegimeState) -> String {
    rs::save_preview(conn, s, RegimeMode::Observe, true, None, None, HEX, NOW)
        .await
        .unwrap()
}

async fn exposure(conn: &mut Conn, id: &str, actual: f64, status: ExecutionStatus, when: &str) {
    rs::record_exposure_status(conn, id, actual, 40.0, 0.0, 0.0, status, when, None)
        .await
        .unwrap();
}

fn weights(pairs: &[(&str, f64)], source: WeightSource, cov: f64) -> BookWeights {
    BookWeights {
        weights: pairs.iter().map(|(k, v)| (*k, *v)).collect(),
        source,
        coverage_pct: cov,
        reason_codes: Vec::new(),
    }
}

// --- config carriage ----------------------------------------------------------------------------

#[test]
fn the_desk_config_is_the_cores_config() {
    let desk = RegimeConfig::default();
    let core = desk.to_core().unwrap();
    assert_eq!(core, CoreConfig::default());
    assert_eq!(core.config_hash(), desk.config_hash());
    assert_eq!(core.canonical_json(), desk.canonical_json());
    let tuned = RegimeConfig {
        buffer_bps: 175,
        tier_exposure_pct: [100.0, 70.0, 40.0, 0.0],
        bootstrap_policy: "adopt_candidate".to_owned(),
        mode: "enforce".to_owned(),
        ..RegimeConfig::default()
    };
    let c = tuned.to_core().unwrap();
    assert_eq!(c.config_hash(), tuned.config_hash());
    assert_eq!(c.mode, RegimeMode::Enforce);
    assert_eq!(c.cap_for(RegimeTier::R4).unwrap(), 0.0);
}

#[test]
fn a_value_the_core_cannot_hold_is_refused_not_coerced() {
    let bad = RegimeConfig {
        mode: "autopilot".to_owned(),
        ..RegimeConfig::default()
    };
    assert!(bad.to_core().is_err());
    let neg = RegimeConfig {
        ma_lengths: vec![-20, 50],
        ..RegimeConfig::default()
    };
    assert!(neg.to_core().is_err());
}

#[test]
fn the_default_mode_is_observe_and_never_enforce() {
    let s = baskfy_desk::Settings::defaults().unwrap();
    let cfg = RegimeConfig::from_settings(&s).unwrap().to_core().unwrap();
    cfg.validate().unwrap();
    assert_eq!(cfg.mode, RegimeMode::Observe);
    assert_eq!(cfg.buffer_bps, 150);
    assert!(!s.regime_enabled);
    assert!(s.dry_run);
}

fn candles(closes: &[Option<f64>], start: NaiveDate, fin: bool) -> Vec<Candle> {
    closes
        .iter()
        .enumerate()
        .map(|(i, c)| Candle {
            date: start + Duration::days(i64::try_from(i).unwrap()),
            open: *c,
            high: c.map(|x| x * 1.01),
            low: c.map(|x| x * 0.99),
            close: *c,
            is_final: fin,
        })
        .collect()
}

#[test]
fn the_desk_candles_reach_the_core_engine_unchanged() {
    let cfg = RegimeConfig::default();
    let closes: Vec<Option<f64>> = (0..260).map(|i| Some(100.0 + f64::from(i) * 0.5)).collect();
    let start = d(2025, 6, 1);
    let ours = e::build_index_signals(
        "NIFTY 50",
        &candles(&closes, start, true),
        &cfg,
        None,
        Some(7),
    )
    .unwrap();
    let plain: Vec<f64> = closes.iter().map(|c| c.unwrap()).collect();
    let core_candles: Vec<_> = plain
        .iter()
        .enumerate()
        .map(|(i, c)| baskfy_core::exposure::regime::Candle {
            date: start + Duration::days(i64::try_from(i).unwrap()),
            open: Some(*c),
            high: Some(c * 1.01),
            low: Some(c * 0.99),
            close: *c,
            is_final: true,
        })
        .collect();
    let theirs = baskfy_core::exposure::regime::build_index_signals(
        "NIFTY 50",
        &core_candles,
        &core(),
        None,
        Some(7),
    )
    .unwrap();
    assert_eq!(ours, theirs);
    assert_eq!(ours.state(20), SignalState::Above);
    assert_eq!(ours.state(200), SignalState::Above);
}

#[test]
fn an_unfinished_session_is_dropped_and_a_null_final_close_raises() {
    let cfg = RegimeConfig::default();
    let start = d(2026, 1, 1);
    let mut c = candles(&[Some(100.0); 30], start, true);
    c.push(Candle {
        date: start + Duration::days(30),
        open: None,
        high: None,
        low: None,
        close: None,
        is_final: false,
    });
    let s = e::build_index_signals("X", &c, &cfg, None, None).unwrap();
    assert_eq!(s.as_of_date, Some(start + Duration::days(29)));
    let mut bad = candles(&[Some(100.0); 5], start, true);
    bad[2].close = None;
    let err = e::build_index_signals("X", &bad, &cfg, None, None).unwrap_err();
    assert!(err.to_string().starts_with("TypeError"), "{err}");
    let none = e::build_index_signals("X", &[], &cfg, None, None).unwrap();
    assert!(!none.has_data && none.is_stale);
}

// --- previews vs canonical commit ---------------------------------------------------------------

#[tokio::test]
async fn dry_run_cannot_commit_a_policy_tier() {
    let (_d, mut c) = sqlite().await;
    let s = st();
    let err = rs::commit_evaluation(&mut c, &s, RegimeMode::Enforce, true, None, NOW)
        .await
        .unwrap_err();
    assert!(matches!(&err, StoreError::DryRunCommit(m) if m.contains("DRY_RUN is active")));
    assert!(
        rs::get_canonical(&mut c, &s.evaluation_id)
            .await
            .unwrap()
            .is_none()
    );
}

#[tokio::test]
async fn observe_mode_cannot_commit() {
    let (_d, mut c) = sqlite().await;
    let err = rs::commit_evaluation(&mut c, &st(), RegimeMode::Observe, false, None, NOW)
        .await
        .unwrap_err();
    assert!(matches!(&err, StoreError::DryRunCommit(m) if m.contains("does not commit")));
}

#[tokio::test]
async fn preview_is_persisted_without_committing() {
    let (_d, mut c) = sqlite().await;
    let s = st();
    let rid = preview(&mut c, &s).await;
    assert_eq!(rid, "preview-0123456789ab");
    assert!(
        rs::get_canonical(&mut c, &s.evaluation_id)
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(
        rs::previews_for(&mut c, &s.evaluation_id)
            .await
            .unwrap()
            .len(),
        1
    );
}

#[tokio::test]
async fn many_previews_do_not_block_the_later_canonical_commit() {
    let (_d, mut c) = sqlite().await;
    let s = st();
    for i in 0..3 {
        let hex = format!("{i:012x}");
        rs::save_preview(&mut c, &s, RegimeMode::Observe, true, None, None, &hex, NOW)
            .await
            .unwrap();
    }
    commit(&mut c, &s, None).await.unwrap();
    let row = rs::get_canonical(&mut c, &s.evaluation_id)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(int(&row, "committed"), 1);
    assert_eq!(int(&row, "dry_run"), 0);
    assert_eq!(
        rs::previews_for(&mut c, &s.evaluation_id)
            .await
            .unwrap()
            .len(),
        3
    );
}

#[tokio::test]
async fn a_week_commits_exactly_once() {
    let (_d, mut c) = sqlite().await;
    let (a, b) = (st(), st());
    assert_eq!(a.evaluation_id, b.evaluation_id);
    commit(&mut c, &a, None).await.unwrap();
    let err = commit(&mut c, &b, None).await.unwrap_err();
    assert!(matches!(&err, StoreError::AlreadyCommitted(m) if m.contains("already committed")));
}

#[tokio::test]
async fn preview_run_id_canonical_is_rejected() {
    let (_d, mut c) = sqlite().await;
    let err = rs::save_preview(
        &mut c,
        &st(),
        RegimeMode::Observe,
        true,
        Some("canonical"),
        None,
        HEX,
        NOW,
    )
    .await
    .unwrap_err();
    assert!(matches!(&err, StoreError::Value(m) if m.contains("reserved")));
}

#[tokio::test]
async fn previews_never_move_the_policy_baseline() {
    let (_d, mut c) = sqlite().await;
    preview(&mut c, &st()).await;
    assert!(rs::previous_regime(&mut c, None).await.unwrap().is_none());
}

#[tokio::test]
async fn previous_regime_is_rebuilt_from_the_last_commit() {
    let (_d, mut c) = sqlite().await;
    let s = st();
    commit(&mut c, &s, None).await.unwrap();
    let prev = rs::previous_regime(&mut c, None).await.unwrap().unwrap();
    assert_eq!(prev.policy_tier, s.policy_tier);
    assert_eq!(prev.scheduled_week_end, d(2026, 6, 5));
    assert_eq!(
        prev.book_weights.unwrap().weights.sorted(),
        s.book_category_weights.sorted()
    );
}

#[tokio::test]
async fn committed_decision_round_trips_with_full_provenance() {
    let (_d, mut c) = sqlite().await;
    let s = st();
    let mut snap = PyDict::new();
    snap.insert("note", PyValue::Str("checkpoint2".to_owned()));
    commit(&mut c, &s, Some(&snap)).await.unwrap();
    let row = rs::get_canonical(&mut c, &s.evaluation_id)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(text(&row, "algorithm_version"), e::ALGORITHM_VERSION);
    assert_eq!(text(&row, "config_hash"), core().config_hash());
    assert_eq!(
        json(&row, "reason_codes_json"),
        serde_json::json!(s.reason_codes)
    );
    assert_eq!(json(&row, "reasons_json"), serde_json::json!(s.reasons));
    let stored = json(&row, "input_snapshot_json");
    assert_eq!(stored["note"], "checkpoint2");
    assert_eq!(
        stored["index_diagnostics"]["NIFTY 50"]["signals"]["200"]["state"],
        "above"
    );
    assert_eq!(stored["health"]["20"], serde_json::json!(1.0));
    assert_eq!(text(&row, "policy_tier"), s.policy_tier.as_str());
    assert_eq!(text(&row, "new_buys"), s.new_buys.as_str());
    assert_eq!(text(&row, "book_weight_source"), "config_default");
}

#[tokio::test]
async fn the_stored_text_is_pythons_sorted_compact_json() {
    let (_d, mut c) = sqlite().await;
    let s = st();
    commit(&mut c, &s, None).await.unwrap();
    let row = rs::get_canonical(&mut c, &s.evaluation_id)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        text(&row, "book_weights_json"),
        r#"{"largecap":0.2,"midcap":0.3,"smallcap":0.5}"#
    );
    let snap = text(&row, "input_snapshot_json");
    assert!(snap.starts_with(r#"{"bearish_stack_weight":0.0,"breadth_coverage_pct":100.0,"breadth_pct":70.0,"health":{"#), "{snap}");
}

// --- exposure reconciliation ---------------------------------------------------------------------

#[tokio::test]
async fn exposure_is_recorded_separately_from_the_decision() {
    let (_d, mut c) = sqlite().await;
    let s = make_state(d(2026, 6, 5), None, 90.0);
    commit(&mut c, &s, None).await.unwrap();
    rs::record_exposure_status(
        &mut c,
        &s.evaluation_id,
        90.0,
        40.0,
        0.0,
        0.0,
        ExecutionStatus::Planned,
        "2026-06-08T10:00:00",
        None,
    )
    .await
    .unwrap();
    let row = rs::latest_exposure(&mut c, Some(&s.evaluation_id))
        .await
        .unwrap()
        .unwrap();
    assert!((float(&row, "exposure_gap_pct") - 50.0).abs() < 1e-9);
    assert_eq!(text(&row, "execution_status"), "planned");
}

#[tokio::test]
async fn partial_fills_append_observations_without_a_new_evaluation() {
    let (_d, mut c) = sqlite().await;
    let s = make_state(d(2026, 6, 5), None, 90.0);
    commit(&mut c, &s, None).await.unwrap();
    for (when, actual, status) in [
        ("2026-06-08T10:00:00", 90.0, ExecutionStatus::Planned),
        ("2026-06-08T10:05:00", 65.0, ExecutionStatus::Partial),
        ("2026-06-09T10:00:00", 40.0, ExecutionStatus::Completed),
    ] {
        exposure(&mut c, &s.evaluation_id, actual, status, when).await;
    }
    let hist = rs::exposure_history(&mut c, &s.evaluation_id)
        .await
        .unwrap();
    let statuses: Vec<String> = hist.iter().map(|h| text(h, "execution_status")).collect();
    assert_eq!(statuses, ["planned", "partial", "completed"]);
    assert_eq!(rs::committed_history(&mut c).await.unwrap().len(), 1);
}

#[tokio::test]
async fn open_gap_is_detectable_for_retry() {
    let (_d, mut c) = sqlite().await;
    let s = make_state(d(2026, 6, 5), None, 90.0);
    commit(&mut c, &s, None).await.unwrap();
    exposure(
        &mut c,
        &s.evaluation_id,
        65.0,
        ExecutionStatus::Partial,
        "2026-06-08T10:00:00",
    )
    .await;
    assert!(rs::open_exposure_gap(&mut c, 1.0).await.unwrap().is_some());
    exposure(
        &mut c,
        &s.evaluation_id,
        40.2,
        ExecutionStatus::Completed,
        "2026-06-09T10:00:00",
    )
    .await;
    assert!(rs::open_exposure_gap(&mut c, 1.0).await.unwrap().is_none());
}

#[tokio::test]
async fn exposure_observation_is_idempotent_per_timestamp() {
    let (_d, mut c) = sqlite().await;
    let s = st();
    for _ in 0..3 {
        exposure(
            &mut c,
            &s.evaluation_id,
            50.0,
            ExecutionStatus::NotPlanned,
            "2026-06-08T10:00:00",
        )
        .await;
    }
    assert_eq!(
        rs::exposure_history(&mut c, &s.evaluation_id)
            .await
            .unwrap()
            .len(),
        1
    );
}

// --- book snapshots ------------------------------------------------------------------------------

#[tokio::test]
async fn only_r1_snapshots_are_offered_as_the_fallback_weight_source() {
    let (_d, mut c) = sqlite().await;
    let r1 = weights(
        &[("smallcap", 0.55), ("midcap", 0.30), ("largecap", 0.15)],
        WeightSource::FullRiskTarget,
        100.0,
    );
    let r3 = weights(
        &[("smallcap", 0.10), ("midcap", 0.20), ("largecap", 0.70)],
        WeightSource::FullRiskTarget,
        100.0,
    );
    rs::save_book_snapshot(&mut c, "e1", d(2026, 5, 1), RegimeTier::R1, &r1, NOW)
        .await
        .unwrap();
    rs::save_book_snapshot(&mut c, "e2", d(2026, 6, 5), RegimeTier::R3, &r3, NOW)
        .await
        .unwrap();
    let snap = rs::last_r1_book_weights(&mut c).await.unwrap().unwrap();
    assert_eq!(snap.weights.get("smallcap"), Some(&0.55));
    assert_eq!(snap.weights.get("largecap"), Some(&0.15));
}

#[tokio::test]
async fn no_r1_snapshot_yields_none() {
    let (_d, mut c) = sqlite().await;
    assert!(rs::last_r1_book_weights(&mut c).await.unwrap().is_none());
}

#[tokio::test]
async fn book_snapshot_upsert_is_idempotent() {
    let (_d, mut c) = sqlite().await;
    let w = weights(
        &[("smallcap", 0.5), ("midcap", 0.3), ("largecap", 0.2)],
        WeightSource::ConfigDefault,
        0.0,
    );
    for _ in 0..2 {
        rs::save_book_snapshot(&mut c, "e1", d(2026, 6, 5), RegimeTier::R1, &w, NOW)
            .await
            .unwrap();
    }
    let n = c
        .execute("SELECT COUNT(*) AS n FROM regime_book_snapshots", &[])
        .await
        .unwrap()
        .rows[0]
        .get("n")
        .and_then(DbValue::as_i64);
    assert_eq!(n, Some(1));
}

#[tokio::test]
async fn snapshot_feeds_the_engines_weight_precedence() {
    let (_d, mut c) = sqlite().await;
    let w = weights(
        &[("smallcap", 0.6), ("midcap", 0.25), ("largecap", 0.15)],
        WeightSource::FullRiskTarget,
        100.0,
    );
    rs::save_book_snapshot(&mut c, "e1", d(2026, 5, 1), RegimeTier::R1, &w, NOW)
        .await
        .unwrap();
    let snap = rs::last_r1_book_weights(&mut c).await.unwrap();
    let bw = e::resolve_book_weights(&core(), None, snap.as_ref()).unwrap();
    assert_eq!(bw.source, WeightSource::LastR1Snapshot);
    assert!((bw.weights.get("smallcap").unwrap() - 0.6).abs() < 1e-9);
}

#[tokio::test]
async fn a_committed_week_is_next_weeks_previous_and_previews_still_do_not_count() {
    let (_d, mut c) = sqlite().await;
    let first = st();
    commit(&mut c, &first, None).await.unwrap();
    let prev = rs::previous_regime(&mut c, None).await.unwrap().unwrap();
    let second = make_state(d(2026, 6, 12), Some(&prev), 50.0);
    assert_eq!(second.previous_policy_tier, Some(first.policy_tier));
    preview(&mut c, &second).await;
    let again = rs::previous_regime(&mut c, None).await.unwrap().unwrap();
    assert_eq!(again.scheduled_week_end, d(2026, 6, 5));
    assert_ne!(first.evaluation_id, second.evaluation_id);
}

#[test]
fn the_store_weights_are_the_cores_type() {
    let w: StrMap<f64> = [("a", 1.0)].into_iter().collect();
    assert_eq!(e::weights_map(&w).get("a"), Some(&PyValue::Float(1.0)));
}

// --- the same writers on Postgres (a bed clone) --------------------------------------------------

#[tokio::test]
#[ignore = "needs the bed: BASKFY_RS_REGIME_TEST_PG naming an rsd_* clone of rsd_tpl_desk, then --ignored"]
async fn on_postgres_the_writers_round_trip() {
    let url = std::env::var("BASKFY_RS_REGIME_TEST_PG").unwrap();
    let name = url.rsplit('/').next().unwrap().split('?').next().unwrap();
    assert!(
        name.starts_with("rsd_"),
        "{name}: only an rsd_* clone is ever written"
    );
    let db = Db::open(DbConfig {
        backend: "postgres".to_owned(),
        schema: "desk".to_owned(),
        database_url: url.clone(),
        db_path: std::path::PathBuf::new(),
    })
    .await
    .unwrap();
    let mut c = db.connect().await.unwrap();
    for t in [
        "regime_evaluations",
        "regime_exposure",
        "regime_book_snapshots",
    ] {
        c.execute(&format!("DELETE FROM {t}"), &[]).await.unwrap();
    }
    let s = st();
    assert!(
        rs::commit_evaluation(&mut c, &s, RegimeMode::Enforce, true, None, NOW)
            .await
            .is_err()
    );
    preview(&mut c, &s).await;
    assert!(rs::previous_regime(&mut c, None).await.unwrap().is_none());
    commit(&mut c, &s, None).await.unwrap();
    assert!(matches!(
        commit(&mut c, &s, None).await,
        Err(StoreError::AlreadyCommitted(_))
    ));
    let prev = rs::previous_regime(&mut c, None).await.unwrap().unwrap();
    assert_eq!(prev.policy_tier, s.policy_tier);
    let row = rs::get_canonical(&mut c, &s.evaluation_id)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(text(&row, "config_hash"), core().config_hash());
    exposure(
        &mut c,
        &s.evaluation_id,
        65.0,
        ExecutionStatus::Partial,
        "2026-06-08T10:00:00",
    )
    .await;
    exposure(
        &mut c,
        &s.evaluation_id,
        65.0,
        ExecutionStatus::Partial,
        "2026-06-08T10:00:00",
    )
    .await;
    assert_eq!(
        rs::exposure_history(&mut c, &s.evaluation_id)
            .await
            .unwrap()
            .len(),
        1
    );
    assert!(rs::open_exposure_gap(&mut c, 1.0).await.unwrap().is_some());
    let w = weights(
        &[("smallcap", 0.6), ("midcap", 0.25), ("largecap", 0.15)],
        WeightSource::FullRiskTarget,
        100.0,
    );
    rs::save_book_snapshot(&mut c, "e1", d(2026, 5, 1), RegimeTier::R1, &w, NOW)
        .await
        .unwrap();
    rs::save_book_snapshot(&mut c, "e1", d(2026, 5, 1), RegimeTier::R1, &w, NOW)
        .await
        .unwrap();
    let snap = rs::last_r1_book_weights(&mut c).await.unwrap().unwrap();
    assert_eq!(snap.weights.get("smallcap"), Some(&0.6));
}
