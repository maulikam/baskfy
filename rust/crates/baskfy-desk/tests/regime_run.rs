//! `tests/test_regime_plan.py` (planning, rollout modes, `DRY_RUN`, execution through the gateway,
//! reconciliation) and `test_regime_view_backtest.py`'s planner-output tests, over
//! `regime_run.py`'s port on the core engine; plus `scripts/daily.py`'s regime-preview step.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp,
    clippy::too_many_arguments
)]

use std::collections::BTreeSet;
use std::path::Path;
use std::sync::{Arc, RwLock};

use baskfy_desk::clock::{Clock, FixedClock, ist};
use baskfy_desk::fake_kite::ReplayKite;
use baskfy_desk::gateway::ProductGates;
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::lanes::analytics::breadth::{self, BreadthReading as DeskBreadth};
use baskfy_desk::lanes::analytics::index_cache::{self, Candle};
use baskfy_desk::lanes::analytics::regime::RegimeConfig;
use baskfy_desk::lanes::analytics::regime::alloc::{
    Candidate, OrderAction, Position, order_sales, solve_allocation,
};
use baskfy_desk::lanes::analytics::regime::engine::{
    ExecutionStatus, ForcedAction, NewBuyMode, RegimeMode, RegimeState, RegimeTier, StrMap,
    WeightSource,
};
use baskfy_desk::lanes::analytics::regime::run::{self as rr, PlanKnobs, RunError};
use baskfy_desk::lanes::analytics::regime_store as rs;
use baskfy_desk::lanes::exec_core::desk::ShimGateway;
use baskfy_desk::lanes::exec_core::order_gateway::{GatewayOptions, OrderGateway};
use baskfy_desk::lanes::exec_core::risk::{RiskBacking, RiskConfig, RiskLayer, RiskManager};
use baskfy_desk::lanes::weekly::weekly_books::tax_lots::{self as tl, TaxReview};
use baskfy_desk::storage::{Conn, Db, Param};
use baskfy_desk::wire::{PyDict, PyValue, dict};
use chrono::{Datelike, Duration, NaiveDate, TimeZone};

fn d(y: i32, m: u32, day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, day).unwrap()
}

fn cands_with(n: usize, start_rank: i64, base_score: f64) -> Vec<Candidate> {
    (0..n)
        .map(|i| {
            let k = i64::try_from(i).unwrap();
            Candidate {
                symbol: format!("S{i:02}"),
                rank: start_rank + k,
                score: base_score - k as f64,
                price: 100.0,
                cluster: format!("c{}", i % 4),
                liquidity: 1e8 - k as f64,
            }
        })
        .collect()
}

fn cands(n: usize) -> Vec<Candidate> {
    cands_with(n, 1, 80.0)
}

fn held(n: usize, qty: i64) -> Vec<Position> {
    (0..n)
        .map(|i| {
            let mut p = Position::new(
                &format!("S{i:02}"),
                qty,
                100.0,
                90.0,
                Some(i64::try_from(i).unwrap() + 1),
            );
            p.cluster = "c0".to_owned();
            p
        })
        .collect()
}

/// `state_for(cfg, tier, actual=, new_buys=)`.
fn state_for(tier: RegimeTier, actual: f64) -> RegimeState {
    let (cap, nb) = match tier {
        RegimeTier::R1 => (100.0, NewBuyMode::Full),
        RegimeTier::R2 => (70.0, NewBuyMode::Half),
        RegimeTier::R3 => (40.0, NewBuyMode::Blocked),
        RegimeTier::R4 => (10.0, NewBuyMode::Blocked),
    };
    RegimeState {
        raw_candidate_tier: tier,
        previous_policy_tier: None,
        policy_tier: tier,
        transition_limited: false,
        target_equity_cap_pct: cap,
        actual_equity_pct: actual,
        exposure_gap_pct: actual - cap,
        pending_sell_pct: 0.0,
        pending_buy_pct: 0.0,
        execution_status: ExecutionStatus::NotPlanned,
        new_buys: nb,
        forced_action: ForcedAction::None,
        reason_codes: Vec::new(),
        reasons: Vec::new(),
        as_of_date: d(2026, 6, 5),
        signal_session_date: d(2026, 6, 5),
        scheduled_week_end: d(2026, 6, 5),
        evaluation_id: "eval-test".to_owned(),
        breadth_pct: Some(60.0),
        breadth_coverage_pct: Some(100.0),
        override_active: false,
        book_category_weights: StrMap::new(),
        book_weight_source: WeightSource::ConfigDefault,
        index_diagnostics: StrMap::new(),
        last_transition_date: None,
        next_evaluation_date: d(2026, 6, 12),
        data_stale: false,
        manual_action_required: false,
        algorithm_version: "regime/1.0.0".to_owned(),
        config_hash: "hash".to_owned(),
        health: std::collections::BTreeMap::new(),
        bearish_stack_weight: 0.0,
    }
}

fn plan_with(
    st: &RegimeState,
    c: &[Candidate],
    p: &[Position],
    capital: f64,
    reviews: &[(String, TaxReview)],
    mode: RegimeMode,
    dry_run: bool,
    knobs: &PlanKnobs,
) -> baskfy_desk::lanes::analytics::regime::alloc::RegimePlan {
    rr::plan_regime_rebalance(st, c, p, capital, reviews, mode, dry_run, knobs).unwrap()
}

fn plan(
    st: &RegimeState,
    c: &[Candidate],
    p: &[Position],
    capital: f64,
    mode: RegimeMode,
) -> baskfy_desk::lanes::analytics::regime::alloc::RegimePlan {
    plan_with(st, c, p, capital, &[], mode, true, &PlanKnobs::default())
}

fn lots(qty: i64, on: &str, price: f64) -> Vec<tl::Lot> {
    tl::lots_from_rows(
        "X",
        &[dict([
            ("quantity", PyValue::Int(qty)),
            ("acquired_on", PyValue::Str(on.to_owned())),
            ("price", PyValue::Float(price)),
        ])],
    )
    .unwrap()
}

fn review(sym: &str, qty: i64, l: &[tl::Lot]) -> TaxReview {
    tl::review_sale(sym, qty, 100.0, l, d(2026, 6, 5), tl::DEFAULT_REVIEW_DAYS)
}

// =================================================================================================
// planning
// =================================================================================================

#[test]
fn the_config_knobs_are_the_desks_defaults() {
    let k = PlanKnobs::from_settings(&baskfy_desk::Settings::defaults().unwrap());
    assert_eq!(k, PlanKnobs::default());
}

#[test]
fn half_sized_entries_count_toward_the_position_ceiling() {
    let p = plan(
        &state_for(RegimeTier::R2, 100.0),
        &cands(30),
        &[],
        10_000_000.0,
        RegimeMode::Propose,
    );
    assert!(p.allocation.n_positions() <= 15);
}

#[test]
fn r2_new_entries_are_half_sized() {
    let p = plan(
        &state_for(RegimeTier::R2, 0.0),
        &cands(12),
        &[],
        10_000_000.0,
        RegimeMode::Propose,
    );
    let buys: BTreeSet<String> = p.buys().iter().map(|o| o.symbol.clone()).collect();
    assert_eq!(p.allocation.half_sized, buys);
    assert!(p.buys().iter().all(|o| o.half_sized));
}

#[test]
fn blocked_tier_admits_no_new_names() {
    let p = plan(
        &state_for(RegimeTier::R3, 100.0),
        &cands(20),
        &held(12, 100),
        10_000_000.0,
        RegimeMode::Propose,
    );
    assert!(p.buys().iter().all(|o| o.qty_now != 0));
}

#[test]
fn r2_trims_to_the_70_percent_cap() {
    let p = plan(
        &state_for(RegimeTier::R2, 100.0),
        &cands(12),
        &held(12, 1000),
        1_200_000.0,
        RegimeMode::Propose,
    );
    assert!((p.projected_equity_pct - 70.0).abs() <= 1.0);
    assert!(p.exposure_gap_after_plan_pct <= 1e-6);
}

#[test]
fn r3_reduction_reaches_40_percent_not_merely_half_the_names() {
    let p = plan(
        &state_for(RegimeTier::R3, 100.0),
        &cands(12),
        &held(12, 1000),
        1_200_000.0,
        RegimeMode::Propose,
    );
    assert!((p.projected_equity_pct - 40.0).abs() <= 1.0);
    assert!(p.projected_equity_pct < 45.0);
}

#[test]
fn r4_residual_respects_the_configured_cap_and_name_limit() {
    let knobs = PlanKnobs {
        residual_max_names: 2,
        min_sleeve_w: 1.0,
        ..PlanKnobs::default()
    };
    let p = plan_with(
        &state_for(RegimeTier::R4, 100.0),
        &cands(12),
        &held(12, 1000),
        1_200_000.0,
        &[],
        RegimeMode::Propose,
        true,
        &knobs,
    );
    assert!(p.projected_equity_pct <= 11.0);
    assert!(p.allocation.n_positions() <= 2);
}

#[test]
fn r4_with_no_residual_policy_is_full_cash() {
    let knobs = PlanKnobs {
        residual_max_names: 0,
        ..PlanKnobs::default()
    };
    let p = plan_with(
        &state_for(RegimeTier::R4, 100.0),
        &cands(12),
        &held(12, 1000),
        1_200_000.0,
        &[],
        RegimeMode::Propose,
        true,
        &knobs,
    );
    assert!(p.projected_equity_pct.abs() <= 1e-6);
    assert!(p.orders.iter().all(|o| o.qty_final == 0));
}

#[test]
fn r1_allows_the_full_book() {
    let p = plan(
        &state_for(RegimeTier::R1, 0.0),
        &cands(12),
        &[],
        10_000_000.0,
        RegimeMode::Propose,
    );
    assert!((p.projected_equity_pct - 100.0).abs() <= 1.0);
    assert!(p.allocation.half_sized.is_empty());
}

#[test]
fn planning_nets_trades_and_creates_no_round_trips() {
    let p = plan(
        &state_for(RegimeTier::R2, 100.0),
        &cands(12),
        &held(12, 1000),
        1_200_000.0,
        RegimeMode::Propose,
    );
    assert!(p.round_trips().is_empty());
    let names: BTreeSet<&str> = p.orders.iter().map(|o| o.symbol.as_str()).collect();
    assert_eq!(names.len(), p.orders.len());
}

#[test]
fn a_name_kept_at_the_same_weight_produces_no_order() {
    let cs = cands_with(10, 1, 50.0);
    let alloc = solve_allocation(&cs, 100.0, 6.0, 15.0, 25.0, &BTreeSet::new(), 0.5).unwrap();
    let capital = 10_000_000.0;
    let ps: Vec<Position> = cs
        .iter()
        .map(|c| {
            let w = alloc.weights_nav.get(&c.symbol).unwrap();
            #[allow(clippy::cast_possible_truncation)]
            let q = (capital * w / 100.0 / c.price).round() as i64;
            Position::new(&c.symbol, q, c.price, c.price, Some(c.rank))
        })
        .collect();
    let p = plan(
        &state_for(RegimeTier::R1, 100.0),
        &cs,
        &ps,
        capital,
        RegimeMode::Propose,
    );
    assert!(p.orders.iter().all(|o| o.delta == 0));
}

#[test]
fn guard_exits_sort_first_and_are_never_reinstated() {
    let mut ps: Vec<Position> = ["A", "B", "C"]
        .iter()
        .enumerate()
        .map(|(i, s)| {
            let mut p = Position::new(s, 100, 100.0, 90.0, Some(i64::try_from(i).unwrap() + 1));
            p.cluster = "c0".to_owned();
            p
        })
        .collect();
    ps[0].guard_exit = true;
    assert_eq!(order_sales(&ps, 3, &BTreeSet::new())[0].symbol, "A");
    let mut cs = vec![
        Candidate::new("A", 1, 90.0, 100.0),
        Candidate::new("B", 2, 80.0, 100.0),
        Candidate::new("C", 3, 70.0, 100.0),
    ];
    cs.extend(cands_with(9, 10, 80.0));
    let knobs = PlanKnobs {
        min_sleeve_w: 1.0,
        ..PlanKnobs::default()
    };
    let p = plan_with(
        &state_for(RegimeTier::R1, 100.0),
        &cs,
        &ps,
        1_000_000.0,
        &[],
        RegimeMode::Propose,
        true,
        &knobs,
    );
    let a = p.orders.iter().find(|o| o.symbol == "A").unwrap();
    assert_eq!(a.qty_final, 0);
    assert_eq!(a.note, "guard exit");
    assert!(
        p.reason_codes
            .iter()
            .any(|c| c == rr::PLAN_GUARD_EXITS_PRESERVED)
    );
}

#[test]
fn tax_flag_does_not_leave_the_cap_silently_breached() {
    let l = lots(1000, "2025-07-01", 50.0);
    let reviews: Vec<(String, TaxReview)> = (0..12)
        .map(|i| {
            let s = format!("S{i:02}");
            (s.clone(), review(&s, 1000, &l))
        })
        .collect();
    let p = plan_with(
        &state_for(RegimeTier::R3, 100.0),
        &cands(12),
        &held(12, 1000),
        1_200_000.0,
        &reviews,
        RegimeMode::Propose,
        true,
        &PlanKnobs::default(),
    );
    assert!((p.projected_equity_pct - 40.0).abs() <= 1.0);
    assert!(p.manual_action_required);
    assert!(!p.tax_flagged.is_empty());
    assert!(p.reason_codes.iter().any(|c| c == rr::PLAN_TAX_SOLD_ANYWAY));
}

#[test]
fn tax_review_marks_the_affected_sell_lines() {
    let l = lots(1000, "2025-07-01", 50.0);
    let reviews = vec![("S11".to_owned(), review("S11", 1000, &l))];
    let p = plan_with(
        &state_for(RegimeTier::R3, 100.0),
        &cands(12),
        &held(12, 1000),
        1_200_000.0,
        &reviews,
        RegimeMode::Propose,
        true,
        &PlanKnobs::default(),
    );
    let s11 = p.orders.iter().find(|o| o.symbol == "S11").unwrap();
    assert!(s11.delta >= 0 || s11.tax_review);
}

#[test]
fn observe_mode_produces_no_executable_plan() {
    let p = plan(
        &state_for(RegimeTier::R2, 100.0),
        &cands(12),
        &held(12, 100),
        1_200_000.0,
        RegimeMode::Observe,
    );
    assert!(!p.executable);
    assert!(
        p.reason_codes
            .iter()
            .any(|c| c == rr::PLAN_NOT_EXECUTABLE_OBSERVE)
    );
}

#[test]
fn propose_mode_needs_approval() {
    let p = plan_with(
        &state_for(RegimeTier::R2, 100.0),
        &cands(12),
        &[],
        1_200_000.0,
        &[],
        RegimeMode::Propose,
        false,
        &PlanKnobs::default(),
    );
    assert!(!p.executable);
    assert!(p.reason_codes.iter().any(|c| c == rr::PLAN_NEEDS_APPROVAL));
}

#[test]
fn enforce_mode_is_executable_only_outside_dry_run() {
    let st = state_for(RegimeTier::R2, 100.0);
    let dry = plan_with(
        &st,
        &cands(12),
        &[],
        1_200_000.0,
        &[],
        RegimeMode::Enforce,
        true,
        &PlanKnobs::default(),
    );
    let live = plan_with(
        &st,
        &cands(12),
        &[],
        1_200_000.0,
        &[],
        RegimeMode::Enforce,
        false,
        &PlanKnobs::default(),
    );
    assert!(!dry.executable && dry.reason_codes.iter().any(|c| c == rr::PLAN_DRY_RUN));
    assert!(live.executable);
}

#[test]
fn infeasible_allocation_becomes_a_manual_review_state() {
    let knobs = PlanKnobs {
        min_sleeve_w: 40.0,
        max_sleeve_w: 45.0,
        target_positions: (2, 2),
        ..PlanKnobs::default()
    };
    let p = plan_with(
        &state_for(RegimeTier::R1, 0.0),
        &cands(2),
        &[],
        1_000_000.0,
        &[],
        RegimeMode::Propose,
        true,
        &knobs,
    );
    assert!(p.infeasible_reason.is_some());
    assert!(p.manual_action_required);
    assert!(!p.executable);
    assert!(p.orders.is_empty());
    assert!(p.reason_codes.iter().any(|c| c == rr::PLAN_INFEASIBLE));
}

#[test]
fn reason_codes_keep_first_seen_order_without_duplicates() {
    let p = plan(
        &state_for(RegimeTier::R2, 100.0),
        &cands(12),
        &[],
        1_200_000.0,
        RegimeMode::Observe,
    );
    assert_eq!(
        p.reason_codes,
        [rr::PLAN_NOT_EXECUTABLE_OBSERVE, rr::PLAN_DRY_RUN]
    );
}

#[test]
fn summary_is_json_friendly_and_names_the_plan() {
    let p = plan(
        &state_for(RegimeTier::R2, 100.0),
        &cands(12),
        &[],
        1_200_000.0,
        RegimeMode::Propose,
    );
    let s = rr::summarise(&p);
    let text = baskfy_desk::wire::orjson_dumps(&PyValue::Map(s.clone())).unwrap();
    let v: serde_json::Value = serde_json::from_str(&text).unwrap();
    assert_eq!(v["tier"], "R2");
    assert_eq!(v["sleeve_pct"], serde_json::json!(70.0));
    assert_eq!(v["buys"], serde_json::json!(p.buys().len()));
    assert_eq!(v["executable"], serde_json::json!(false));
    assert!(v["largest_position"]["symbol"].is_string());
}

#[test]
fn snapshot_carries_proposed_orders_and_review_items() {
    let st = state_for(RegimeTier::R2, 100.0);
    let cs: Vec<Candidate> = (0..12)
        .map(|i| Candidate::new(&format!("S{i:02}"), i + 1, 85.0 - i as f64, 100.0))
        .collect();
    let hs: Vec<Position> = (0..12)
        .map(|i| Position::new(&format!("S{i:02}"), 1000, 100.0, 90.0, Some(i + 1)))
        .collect();
    let acq = (d(2026, 6, 5) - Duration::days(329))
        .format("%Y-%m-%d")
        .to_string();
    let rev = vec![(
        "S11".to_owned(),
        tl::review_sale(
            "S11",
            1000,
            100.0,
            &lots(1000, &acq, 50.0),
            d(2026, 6, 5),
            tl::DEFAULT_REVIEW_DAYS,
        ),
    )];
    let p = plan_with(
        &st,
        &cs,
        &hs,
        1_200_000.0,
        &rev,
        RegimeMode::Observe,
        true,
        &PlanKnobs::default(),
    );
    let snap = rr::evaluation_snapshot(Some(&p), Some(&rev));
    let PyValue::List(orders) = snap.get("proposed_orders").unwrap() else {
        panic!()
    };
    assert!(!orders.is_empty());
    assert_eq!(
        orders.len(),
        p.orders.iter().filter(|o| o.delta != 0).count()
    );
    let PyValue::List(items) = snap.get("manual_review_items").unwrap() else {
        panic!()
    };
    assert_eq!(items.len(), 1);
    let PyValue::Map(summary) = snap.get("plan").unwrap() else {
        panic!()
    };
    assert_eq!(summary.get("tier"), Some(&PyValue::Str("R2".to_owned())));
    let PyValue::Map(first) = &orders[0] else {
        panic!()
    };
    let keys: Vec<&str> = first.iter().map(|(k, _)| k).collect();
    assert_eq!(
        keys,
        [
            "symbol",
            "action",
            "qty_now",
            "delta",
            "qty_final",
            "ref_price",
            "weight_nav",
            "weight_sleeve",
            "value",
            "rank",
            "half_sized",
            "tax_review",
            "note"
        ]
    );
    assert!(rr::evaluation_snapshot(None, None).is_empty());
}

// =================================================================================================
// execution — DRY_RUN, approval, and the gateway as the only path
// =================================================================================================

struct Bed {
    _dir: tempfile::TempDir,
    kite: Arc<ReplayKite>,
    gateway: ShimGateway,
}

async fn bed() -> Bed {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(
        dir.path().join("kite.json"),
        r#"{"session": {"authed": true}, "routes": {}, "quotes": {}, "historical": {}}"#,
    )
    .unwrap();
    std::fs::create_dir(dir.path().join("instruments")).unwrap();
    let mut csv = "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n".to_owned();
    for i in 0..20 {
        csv.push_str(&format!(
            "{},{},S{i:02},S{i:02},0,,0,0.05,1,EQ,NSE,NSE\n",
            1000 + i,
            10 + i
        ));
    }
    std::fs::write(dir.path().join("instruments/NSE.csv"), csv).unwrap();
    let kite = Arc::new(ReplayKite::load(dir.path(), None, "test-api-key").unwrap());
    let clock: Arc<dyn Clock> = Arc::new(FixedClock::new(
        ist().with_ymd_and_hms(2026, 6, 8, 10, 0, 0).unwrap(),
    ));
    let risk: Arc<dyn RiskLayer> = Arc::new(
        RiskManager::new(
            RiskConfig {
                max_position_value: 1e12,
                max_gross_exposure: 1e13,
                ..RiskConfig::default()
            },
            clock.clone(),
            RiskBacking::Memory,
        )
        .await
        .unwrap(),
    );
    let gates = ProductGates::default();
    assert!(gates.dry_run, "DRY_RUN is the gateway's default");
    let core = OrderGateway::new(
        kite.clone(),
        risk,
        GatewayOptions {
            gates: Arc::new(move || gates),
            journal_path: dir.path().join("journal.jsonl"),
            stop_band: None,
            coverage: None,
            clock,
            ids: Arc::new(HarnessIds::new("regime-run-test")),
            limits: None,
        },
    )
    .unwrap();
    let settings = Arc::new(RwLock::new(baskfy_desk::Settings::defaults().unwrap()));
    Bed {
        _dir: dir,
        kite,
        gateway: ShimGateway::new(Arc::new(core), settings),
    }
}

fn status(r: &PyDict) -> String {
    match r.get("status") {
        Some(PyValue::Str(s)) => s.clone(),
        other => panic!("{other:?}"),
    }
}

#[tokio::test]
async fn non_enforce_modes_refuse_to_execute() {
    let b = bed().await;
    for mode in [RegimeMode::Observe, RegimeMode::Propose] {
        let p = plan(
            &state_for(RegimeTier::R2, 100.0),
            &cands(12),
            &[],
            1_200_000.0,
            mode,
        );
        let err = rr::execute_regime_plan(&b.gateway, &p, true, mode, false, true)
            .await
            .unwrap_err();
        assert!(
            matches!(&err, RunError::PlanNotExecutable(m) if m.contains("enforce")),
            "{err}"
        );
    }
    assert!(b.kite.write_calls().is_empty());
}

#[tokio::test]
async fn dry_run_submits_no_orders_even_in_enforce_mode() {
    let b = bed().await;
    let p = plan_with(
        &state_for(RegimeTier::R2, 100.0),
        &cands(12),
        &[],
        1_200_000.0,
        &[],
        RegimeMode::Enforce,
        true,
        &PlanKnobs::default(),
    );
    let err = rr::execute_regime_plan(&b.gateway, &p, true, RegimeMode::Enforce, true, true)
        .await
        .unwrap_err();
    assert!(matches!(&err, RunError::PlanNotExecutable(m) if m.contains("DRY_RUN")));
    assert!(b.kite.write_calls().is_empty());
}

#[tokio::test]
async fn execution_requires_explicit_approval() {
    let b = bed().await;
    let p = plan_with(
        &state_for(RegimeTier::R2, 100.0),
        &cands(12),
        &[],
        1_200_000.0,
        &[],
        RegimeMode::Enforce,
        false,
        &PlanKnobs::default(),
    );
    let err = rr::execute_regime_plan(&b.gateway, &p, true, RegimeMode::Enforce, false, false)
        .await
        .unwrap_err();
    assert!(matches!(&err, RunError::PlanNotExecutable(m) if m.contains("approval")));
}

#[tokio::test]
async fn disabled_feature_refuses_to_execute() {
    let b = bed().await;
    let p = plan_with(
        &state_for(RegimeTier::R2, 100.0),
        &cands(12),
        &[],
        1_200_000.0,
        &[],
        RegimeMode::Enforce,
        false,
        &PlanKnobs::default(),
    );
    let err = rr::execute_regime_plan(&b.gateway, &p, false, RegimeMode::Enforce, false, true)
        .await
        .unwrap_err();
    assert!(matches!(err, RunError::RegimeDisabled(_)));
}

#[tokio::test]
async fn an_infeasible_plan_is_refused() {
    let b = bed().await;
    let knobs = PlanKnobs {
        min_sleeve_w: 40.0,
        max_sleeve_w: 45.0,
        target_positions: (2, 2),
        ..PlanKnobs::default()
    };
    let p = plan_with(
        &state_for(RegimeTier::R1, 0.0),
        &cands(2),
        &[],
        1_000_000.0,
        &[],
        RegimeMode::Enforce,
        false,
        &knobs,
    );
    let err = rr::execute_regime_plan(&b.gateway, &p, true, RegimeMode::Enforce, false, true)
        .await
        .unwrap_err();
    assert!(matches!(&err, RunError::PlanNotExecutable(m) if m.starts_with("plan is infeasible")));
}

#[tokio::test]
async fn execution_routes_through_the_gateway() {
    let b = bed().await;
    let p = plan_with(
        &state_for(RegimeTier::R2, 0.0),
        &cands(12),
        &[],
        1_200_000.0,
        &[],
        RegimeMode::Enforce,
        false,
        &PlanKnobs::default(),
    );
    let rep = rr::execute_regime_plan(&b.gateway, &p, true, RegimeMode::Enforce, false, true)
        .await
        .unwrap();
    assert!(!rep.submitted.is_empty());
    assert!(rep.submitted.iter().all(|r| status(r) == "DRY_RUN"));
    assert!(rep.skipped.is_empty());
    assert_eq!(rep.status, ExecutionStatus::Completed);
    assert_eq!(rep.filled_pct(), 100.0);
    assert_eq!(rep.mode, "enforce");
    assert!(
        b.kite.write_calls().is_empty(),
        "DRY_RUN inside the gateway reached the broker"
    );
}

#[tokio::test]
async fn execution_sells_before_buys() {
    let b = bed().await;
    let p = plan_with(
        &state_for(RegimeTier::R2, 100.0),
        &cands(12),
        &held(6, 2000),
        1_200_000.0,
        &[],
        RegimeMode::Enforce,
        false,
        &PlanKnobs::default(),
    );
    let rep = rr::execute_regime_plan(&b.gateway, &p, true, RegimeMode::Enforce, false, true)
        .await
        .unwrap();
    let sides: Vec<String> = rep
        .submitted
        .iter()
        .map(|r| match r.get("regime_action") {
            Some(PyValue::Str(s)) => s.clone(),
            other => panic!("{other:?}"),
        })
        .collect();
    let sells: Vec<usize> = sides
        .iter()
        .enumerate()
        .filter(|(_, a)| *a == "TRIM" || *a == "EXIT")
        .map(|(i, _)| i)
        .collect();
    let buys: Vec<usize> = sides
        .iter()
        .enumerate()
        .filter(|(_, a)| *a == "BUY" || *a == "ADD")
        .map(|(i, _)| i)
        .collect();
    assert!(!sells.is_empty() && !buys.is_empty());
    assert!(sells.iter().max() < buys.iter().min());
    assert!(b.kite.write_calls().is_empty());
}

#[tokio::test]
async fn nothing_to_do_is_not_planned() {
    let b = bed().await;
    let p = plan_with(
        &state_for(RegimeTier::R4, 0.0),
        &[],
        &[],
        1_200_000.0,
        &[],
        RegimeMode::Enforce,
        false,
        &PlanKnobs::default(),
    );
    let rep = rr::execute_regime_plan(&b.gateway, &p, true, RegimeMode::Enforce, false, true)
        .await
        .unwrap();
    assert_eq!(rep.status, ExecutionStatus::NotPlanned);
    assert_eq!(rep.filled_pct(), 0.0);
}

#[test]
fn the_regime_modules_never_call_the_broker_directly() {
    let lane = Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lanes/analytics/regime");
    for f in ["run.rs", "alloc.rs", "engine.rs"] {
        let src = std::fs::read_to_string(lane.join(f)).unwrap();
        let code: String = src
            .lines()
            .filter(|l| !l.trim_start().starts_with("//"))
            .collect::<Vec<_>>()
            .join("\n");
        for verb in [
            "place_order(",
            "place_gtt(",
            "place_gtt_stop(",
            "Kite",
            "kite::",
        ] {
            assert!(!code.contains(verb), "{f} names {verb}");
        }
    }
    let run = std::fs::read_to_string(lane.join("run.rs")).unwrap();
    assert_eq!(
        run.matches(".place(").count(),
        1,
        "one call site, on the Gateway trait"
    );
    assert!(run.contains("gateway: &dyn Gateway"));
}

#[test]
fn an_order_line_action_is_pythons_string() {
    assert_eq!(OrderAction::Trim.as_str(), "TRIM");
    assert_eq!(OrderAction::Exit.as_str(), "EXIT");
}

// =================================================================================================
// reconciliation
// =================================================================================================

async fn sqlite() -> (tempfile::TempDir, Conn) {
    let dir = tempfile::tempdir().unwrap();
    let mut conn = Db::connect_sqlite_path(&dir.path().join("p.db"))
        .await
        .unwrap();
    conn.migrate().await.unwrap();
    (dir, conn)
}

fn f(r: &PyDict, k: &str) -> f64 {
    match r.get(k) {
        Some(PyValue::Float(x)) => *x,
        Some(PyValue::Int(i)) => *i as f64,
        other => panic!("{k}: {other:?}"),
    }
}

#[tokio::test]
async fn partial_execution_preserves_the_gap_and_can_be_retried() {
    let (_d, mut c) = sqlite().await;
    let st = state_for(RegimeTier::R3, 100.0);
    let p = plan(
        &st,
        &cands(12),
        &held(12, 1000),
        1_200_000.0,
        RegimeMode::Propose,
    );
    rr::reconcile(
        &mut c,
        &st,
        &p,
        100.0,
        None,
        ExecutionStatus::Planned,
        "2026-06-08T10:00:00",
    )
    .await
    .unwrap();
    rr::reconcile(
        &mut c,
        &st,
        &p,
        65.0,
        None,
        ExecutionStatus::Partial,
        "2026-06-08T10:05:00",
    )
    .await
    .unwrap();
    let row = rs::latest_exposure(&mut c, Some(&st.evaluation_id))
        .await
        .unwrap()
        .unwrap();
    assert!((f(&row, "exposure_gap_pct") - 25.0).abs() < 1e-9);
    assert!(rs::open_exposure_gap(&mut c, 1.0).await.unwrap().is_some());
    assert_eq!(
        rs::exposure_history(&mut c, &st.evaluation_id)
            .await
            .unwrap()
            .len(),
        2
    );
    assert!(rs::committed_history(&mut c).await.unwrap().is_empty());
}

#[tokio::test]
async fn reconciliation_closes_when_the_cap_is_reached() {
    let (_d, mut c) = sqlite().await;
    let st = state_for(RegimeTier::R3, 100.0);
    let p = plan(&st, &cands(12), &[], 1_200_000.0, RegimeMode::Propose);
    rr::reconcile(
        &mut c,
        &st,
        &p,
        40.0,
        None,
        ExecutionStatus::Completed,
        "2026-06-09T10:00:00",
    )
    .await
    .unwrap();
    assert!(rs::open_exposure_gap(&mut c, 1.0).await.unwrap().is_none());
}

#[tokio::test]
async fn pending_percentages_are_measured_against_rupee_capital() {
    let (_d, mut c) = sqlite().await;
    let st = state_for(RegimeTier::R2, 85.0);
    let hs: Vec<Position> = (0..12)
        .map(|i| Position::new(&format!("S{i:02}"), 1000, 100.0, 90.0, Some(i + 1)))
        .collect();
    let cs: Vec<Candidate> = (0..12)
        .map(|i| Candidate::new(&format!("S{i:02}"), i + 1, 85.0 - i as f64, 100.0))
        .collect();
    let capital = 1_200_000.0;
    let p = plan(&st, &cs, &hs, capital, RegimeMode::Observe);
    rr::reconcile(
        &mut c,
        &st,
        &p,
        85.0,
        Some(capital),
        ExecutionStatus::NotPlanned,
        "2026-06-08T10:00:00",
    )
    .await
    .unwrap();
    let row = rs::latest_exposure(&mut c, Some(&st.evaluation_id))
        .await
        .unwrap()
        .unwrap();
    let expected: f64 = p
        .sells()
        .iter()
        .map(|o| o.delta.abs() as f64 * o.ref_price)
        .sum::<f64>()
        / capital
        * 100.0;
    assert!(expected > 0.0);
    assert!((f(&row, "pending_sell_pct") - expected).abs() <= 1e-6);
    assert!((0.0..=100.0).contains(&f(&row, "pending_sell_pct")));
    let note = match row.get("note") {
        Some(PyValue::Str(s)) => s.clone(),
        other => panic!("{other:?}"),
    };
    assert_eq!(
        note,
        format!("tier=R2 projected={:.2}%", p.projected_equity_pct)
    );
}

#[tokio::test]
async fn missing_capital_does_not_produce_a_nonsense_percentage() {
    let (_d, mut c) = sqlite().await;
    let st = state_for(RegimeTier::R2, 85.0);
    let p = plan(
        &st,
        &cands(12),
        &held(12, 1000),
        1_200_000.0,
        RegimeMode::Observe,
    );
    rr::reconcile(
        &mut c,
        &st,
        &p,
        85.0,
        None,
        ExecutionStatus::NotPlanned,
        "2026-06-08T11:00:00",
    )
    .await
    .unwrap();
    let row = rs::latest_exposure(&mut c, Some(&st.evaluation_id))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(f(&row, "pending_sell_pct"), 0.0);
}

#[tokio::test]
async fn dry_run_does_not_commit_a_policy_tier() {
    let (_d, mut c) = sqlite().await;
    let st = state_for(RegimeTier::R2, 100.0);
    assert!(matches!(
        rs::commit_evaluation(
            &mut c,
            &st,
            RegimeMode::Enforce,
            true,
            None,
            "2026-06-08T10:00:00"
        )
        .await,
        Err(rs::StoreError::DryRunCommit(_))
    ));
    assert!(
        rs::get_canonical(&mut c, &st.evaluation_id)
            .await
            .unwrap()
            .is_none()
    );
}

// =================================================================================================
// the daily regime preview (scripts/daily.py step 5)
// =================================================================================================

fn weekdays(n: usize, end: NaiveDate) -> Vec<NaiveDate> {
    let mut out = Vec::new();
    let mut day = end;
    while out.len() < n {
        if day.weekday().num_days_from_monday() < 5 {
            out.push(day);
        }
        day -= Duration::days(1);
    }
    out.reverse();
    out
}

async fn seed_indices(c: &mut Conn, cfg: &RegimeConfig, end: NaiveDate) {
    for name in index_cache::required_index_names(cfg) {
        let candles: Vec<Candle> = weekdays(260, end)
            .into_iter()
            .enumerate()
            .map(|(i, day)| {
                let close = 100.0 + i as f64 * 0.5;
                Candle {
                    date: day,
                    open: Some(close),
                    high: Some(close),
                    low: Some(close),
                    close: Some(close),
                    is_final: true,
                }
            })
            .collect();
        index_cache::upsert(c, &name, &candles, None, "2026-06-05T18:00:00")
            .await
            .unwrap();
    }
}

#[tokio::test]
async fn the_daily_preview_stores_a_preview_and_never_commits() {
    let (_d, mut c) = sqlite().await;
    let cfg = RegimeConfig::default();
    let friday = d(2026, 6, 5);
    seed_indices(&mut c, &cfg, friday).await;
    breadth::save_reading(
        &mut c,
        &DeskBreadth {
            as_of_date: friday,
            pct_above_20dma: 70.0,
            eligible_count: 500,
            observed_count: 500,
            coverage_pct: 100.0,
            universe_id: "scan".to_owned(),
            universe_hash: "h".to_owned(),
            audit_run_id: "run".to_owned(),
            calculation_version: breadth::CALCULATION_VERSION.to_owned(),
        },
        breadth::MISSING_POLICY,
        "2026-06-05T18:00:00",
    )
    .await
    .unwrap();
    c.execute(
        "INSERT INTO snapshots(date, nav, invested, cash, holdings_json) VALUES(?,?,?,?,?)",
        &[
            Param::from("2026-06-05"),
            Param::from(1_000_000.0),
            Param::from(850_000.0),
            Param::from(150_000.0),
            Param::from("[]"),
        ],
    )
    .await
    .unwrap();
    let out = rr::regime_preview(
        &mut c,
        &cfg,
        true,
        d(2026, 6, 6),
        "2026-06-06T09:00:00",
        "abcdef0123456789",
    )
    .await
    .unwrap();
    assert_eq!(out.run_id, "preview-abcdef012345");
    assert_eq!(out.actual_equity_pct, 85.0);
    assert_eq!(out.state.signal_session_date, friday);
    assert_eq!(out.state.breadth_pct, Some(70.0));
    assert_eq!(out.state.book_weight_source, WeightSource::ConfigDefault);
    assert!(!out.state.data_stale);
    assert!(out.summary.starts_with(out.state.policy_tier.as_str()));
    assert!(out.summary.contains("actual 85.0%"), "{}", out.summary);
    assert!(
        rs::get_canonical(&mut c, &out.state.evaluation_id)
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(
        rs::previews_for(&mut c, &out.state.evaluation_id)
            .await
            .unwrap()
            .len(),
        1
    );
    let row = rs::latest_exposure(&mut c, Some(&out.state.evaluation_id))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(f(&row, "actual_equity_pct"), 85.0);
    assert_eq!(
        f(&row, "target_equity_cap_pct"),
        out.state.target_equity_cap_pct
    );
    assert!(rs::previous_regime(&mut c, None).await.unwrap().is_none());
}

#[tokio::test]
async fn the_daily_preview_refuses_without_an_aligned_session() {
    let (_d, mut c) = sqlite().await;
    let err = rr::regime_preview(
        &mut c,
        &RegimeConfig::default(),
        true,
        d(2026, 6, 6),
        "2026-06-06T09:00:00",
        "ab",
    )
    .await
    .unwrap_err();
    assert!(
        err.to_string()
            .contains("no session where every required index")
    );
    let n = c
        .execute("SELECT COUNT(*) AS n FROM regime_evaluations", &[])
        .await
        .unwrap()
        .rows[0]
        .get("n")
        .and_then(baskfy_desk::storage::DbValue::as_i64);
    assert_eq!(n, Some(0));
}
