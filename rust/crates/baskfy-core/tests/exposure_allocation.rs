#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]
//! Port of the pure allocation tests of `kite-momentum-rebalancer/tests/test_regime_plan.py`.

use std::collections::BTreeSet;

use baskfy_core::exposure::ExposureError;
use baskfy_core::exposure::allocation::{
    Allocation, Candidate, OrderAction, Position, RegimePlan, SurvivorParams, build_orders,
    feasible_position_range, order_sales, projected_equity_pct, select_survivors, solve_allocation,
    validate_allocation_request,
};
use baskfy_core::exposure::regime::{NewBuyMode, RegimeTier};

const SLEEVE_MIN: f64 = 6.0;
const SLEEVE_MAX: f64 = 15.0;

fn near(a: f64, b: f64, abs: f64) -> bool {
    (a - b).abs() <= abs.max(1e-6 * b.abs())
}

fn cands_full(n: usize, start_rank: i64, price: f64, base_score: f64) -> Vec<Candidate> {
    (0..n)
        .map(|i| {
            let ii = i64::try_from(i).unwrap();
            Candidate {
                symbol: format!("S{i:02}"),
                rank: start_rank + ii,
                score: base_score - f64::from(u32::try_from(i).unwrap()),
                price,
                cluster: format!("c{}", i % 4),
                liquidity: 1e8 - f64::from(u32::try_from(i).unwrap()),
            }
        })
        .collect()
}

fn cands(n: usize) -> Vec<Candidate> {
    cands_full(n, 1, 100.0, 80.0)
}

fn none() -> BTreeSet<String> {
    BTreeSet::new()
}

fn set(xs: &[&str]) -> BTreeSet<String> {
    xs.iter().map(|s| (*s).to_owned()).collect()
}

fn solve(c: &[Candidate], sleeve: f64) -> Allocation {
    solve_allocation(c, sleeve, SLEEVE_MIN, SLEEVE_MAX, 25.0, &none(), 0.5).unwrap()
}

fn alloc_err(r: Result<Allocation, ExposureError>) -> String {
    match r {
        Err(ExposureError::Allocation(m)) => m,
        other => panic!("expected AllocationError, got {other:?}"),
    }
}

fn sum(m: impl Iterator<Item = f64>) -> f64 {
    m.sum()
}

fn positions(symbols: &[String], qty: i64, price: f64, avg: f64) -> Vec<Position> {
    symbols
        .iter()
        .enumerate()
        .map(|(i, s)| {
            let mut p = Position::new(s, qty, price, avg, Some(i64::try_from(i).unwrap() + 1));
            p.cluster = "c0".into();
            p
        })
        .collect()
}

fn names(n: usize) -> Vec<String> {
    (0..n).map(|i| format!("S{i:02}")).collect()
}

#[test]
fn sleeve_weights_and_nav_weights_differ_by_the_sleeve() {
    let a = solve(&cands(10), 40.0);
    assert!(near(sum(a.weights_sleeve.values().copied()), 100.0, 1e-6));
    assert!(near(sum(a.weights_nav.values().copied()), 40.0, 1e-6));
    for (s, w) in a.weights_sleeve.iter() {
        assert!(near(*a.weights_nav.get(s).unwrap(), w * 0.40, 1e-6));
    }
}

#[test]
fn the_specs_worked_example_holds() {
    let a = solve(&cands_full(10, 1, 100.0, 50.0), 40.0);
    let (s, w) = a
        .weights_sleeve
        .iter()
        .find(|(_, v)| (**v - 10.0).abs() < 1.5)
        .unwrap();
    assert!(near(*a.weights_nav.get(s).unwrap(), w * 0.4, 1e-6));
}

#[test]
fn min_and_max_are_enforced_on_the_sleeve_not_nav() {
    let a = solve(&cands(12), 40.0);
    for w in a.weights_sleeve.values() {
        assert!(*w >= SLEEVE_MIN - 1e-6 && *w <= SLEEVE_MAX + 1e-6);
    }
    let max_nav = a.weights_nav.values().copied().fold(f64::MIN, f64::max);
    assert!(max_nav <= SLEEVE_MAX * 0.4 + 1e-6);
}

#[test]
fn full_sleeve_reduces_to_the_old_nav_semantics() {
    let a = solve(&cands(12), 100.0);
    assert_eq!(a.weights_sleeve, a.weights_nav);
}

#[test]
fn feasible_position_range_for_the_configured_weights() {
    assert_eq!(feasible_position_range(6.0, 15.0).unwrap(), (7, 16));
}

#[test]
fn too_many_full_size_positions_is_an_error() {
    let m = alloc_err(solve_allocation(
        &cands(17),
        100.0,
        SLEEVE_MIN,
        SLEEVE_MAX,
        25.0,
        &none(),
        0.5,
    ));
    assert!(m.contains("only 100%"));
}

#[test]
fn too_few_positions_to_absorb_the_sleeve_is_an_error() {
    let m = alloc_err(solve_allocation(
        &cands(3),
        100.0,
        SLEEVE_MIN,
        SLEEVE_MAX,
        25.0,
        &none(),
        0.5,
    ));
    assert!(m.contains("at least"));
}

#[test]
fn contradictory_weights_are_rejected() {
    let e = validate_allocation_request(10, 20.0, 10.0, 0).unwrap_err();
    assert!(e.to_string().contains("contradict"));
}

#[test]
fn no_candidates_is_an_error() {
    let m = alloc_err(solve_allocation(&[], 100.0, 6.0, 15.0, 25.0, &none(), 0.5));
    assert!(m.contains("no eligible"));
}

#[test]
fn weights_are_never_silently_distorted() {
    let a = solve(&cands(14), 70.0);
    assert!(a.weights_sleeve.values().all(|w| *w >= SLEEVE_MIN - 1e-6));
    assert!(near(sum(a.weights_sleeve.values().copied()), 100.0, 1e-6));
}

#[test]
fn cluster_cap_is_applied_on_the_sleeve() {
    let mut cs: Vec<Candidate> = (0..6)
        .map(|i| {
            let mut c = Candidate::new(
                &format!("H{i}"),
                i + 1,
                80.0 - f64::from(u8::try_from(i).unwrap()),
                100.0,
            );
            c.cluster = "pharma".into();
            c
        })
        .collect();
    cs.extend((0..6).map(|i| {
        let mut c = Candidate::new(
            &format!("R{i}"),
            10 + i,
            60.0 - f64::from(u8::try_from(i).unwrap()),
            100.0,
        );
        c.cluster = format!("c{i}");
        c
    }));
    let a = solve_allocation(&cs, 100.0, 1.0, 30.0, 25.0, &none(), 0.5).unwrap();
    assert!(*a.cluster_weights.get("pharma").unwrap() <= 25.0 + 1e-6);
    assert!(a.notes.iter().any(|n| n == "cluster cap applied: pharma"));
}

#[test]
fn cluster_cap_that_contradicts_the_floor_is_reported() {
    let mut cs = cands(8);
    for c in &mut cs {
        c.cluster = "it".into();
    }
    let m = alloc_err(solve_allocation(&cs, 100.0, 6.0, 15.0, 25.0, &none(), 0.5));
    assert!(m.starts_with("cluster 'it' holds 8 names needing 48.0%"));
}

#[test]
fn half_sized_entry_is_half_the_solved_weight() {
    let full = solve(&cands(10), 100.0);
    let half = solve_allocation(
        &cands(10),
        100.0,
        SLEEVE_MIN,
        SLEEVE_MAX,
        25.0,
        &set(&["S00"]),
        0.5,
    )
    .unwrap();
    let h = *half.weights_sleeve.get("S00").unwrap();
    assert!(h < *full.weights_sleeve.get("S00").unwrap());
    assert!(h < SLEEVE_MIN);
    assert!(half.half_sized.contains("S00"));
}

#[test]
fn blocked_tier_admits_no_new_names() {
    let held = positions(&names(12), 100, 100.0, 90.0);
    let (survivors, _) = select_survivors(
        &cands(20),
        &held,
        &SurvivorParams::new(15, NewBuyMode::Blocked),
    );
    let held_names: BTreeSet<&str> = held.iter().map(|p| p.symbol.as_str()).collect();
    assert!(
        survivors
            .iter()
            .all(|c| held_names.contains(c.symbol.as_str()))
    );
}

#[test]
fn half_sized_entries_count_toward_the_position_ceiling() {
    let (survivors, _) =
        select_survivors(&cands(30), &[], &SurvivorParams::new(15, NewBuyMode::Half));
    assert_eq!(survivors.len(), 15);
}

#[test]
fn r4_residual_respects_the_name_limit() {
    let held = positions(&names(12), 1000, 100.0, 90.0);
    let mut p = SurvivorParams::new(15, NewBuyMode::Blocked);
    p.residual_max_names = Some(2);
    let (survivors, dropped) = select_survivors(&cands(12), &held, &p);
    assert_eq!(
        survivors
            .iter()
            .map(|c| c.symbol.as_str())
            .collect::<Vec<_>>(),
        ["S00", "S01"]
    );
    assert_eq!(dropped.len(), 10);
    p.residual_max_names = Some(0);
    assert!(select_survivors(&cands(12), &held, &p).0.is_empty());
}

fn plan_like(
    sleeve: f64,
    held: &[Position],
    cs: &[Candidate],
    capital: f64,
) -> Vec<baskfy_core::exposure::allocation::OrderLine> {
    let (survivors, _) = select_survivors(cs, held, &SurvivorParams::new(15, NewBuyMode::Blocked));
    let a = solve(&survivors, sleeve);
    build_orders(&a, held, cs, capital, &none(), &none())
}

#[test]
fn r3_reduction_reaches_40_percent_not_merely_half_the_names() {
    let held = positions(&names(12), 1000, 100.0, 90.0);
    let lines = plan_like(40.0, &held, &cands(12), 1_200_000.0);
    let proj = projected_equity_pct(&lines, 1_200_000.0);
    assert!(near(proj, 40.0, 1.0) && proj < 45.0);
}

#[test]
fn r2_trims_to_the_70_percent_cap_and_nets_trades() {
    let held = positions(&names(12), 1000, 100.0, 90.0);
    let lines = plan_like(70.0, &held, &cands(12), 1_200_000.0);
    assert!(near(projected_equity_pct(&lines, 1_200_000.0), 70.0, 1.0));
    let syms: BTreeSet<&str> = lines.iter().map(|o| o.symbol.as_str()).collect();
    assert_eq!(syms.len(), lines.len());
    let plan = RegimePlan {
        tier: RegimeTier::R2,
        sleeve_pct: 70.0,
        allocation: Allocation::empty(70.0),
        orders: lines,
        projected_equity_pct: 70.0,
        exposure_gap_after_plan_pct: 0.0,
        manual_action_required: false,
        executable: false,
        reason_codes: Vec::new(),
        tax_flagged: Vec::new(),
        infeasible_reason: None,
    };
    assert!(plan.round_trips().is_empty());
    assert!(plan.sells().iter().all(|o| o.action == OrderAction::Trim));
}

#[test]
fn a_name_kept_at_the_same_weight_produces_no_order() {
    let cs = cands_full(10, 1, 100.0, 50.0);
    let a = solve(&cs, 100.0);
    let capital = 10_000_000.0;
    let held: Vec<Position> = cs
        .iter()
        .map(|c| {
            let q = (capital * a.weights_nav.get(&c.symbol).unwrap() / 100.0 / c.price).round();
            Position::new(&c.symbol, q as i64, c.price, c.price, Some(c.rank))
        })
        .collect();
    let lines = build_orders(&a, &held, &cs, capital, &none(), &none());
    assert!(
        lines
            .iter()
            .all(|o| o.delta == 0 && o.action == OrderAction::Hold)
    );
}

#[test]
fn guard_exits_sort_first_and_are_never_reinstated() {
    let mut ps = positions(&["A".into(), "B".into(), "C".into()], 100, 100.0, 90.0);
    ps[0].guard_exit = true;
    assert_eq!(order_sales(&ps, 3, &none())[0].symbol, "A");

    let mut cs = vec![
        Candidate::new("A", 1, 90.0, 100.0),
        Candidate::new("B", 2, 80.0, 100.0),
        Candidate::new("C", 3, 70.0, 100.0),
    ];
    cs.extend(cands_full(9, 10, 100.0, 80.0));
    let a = solve_allocation(&cs, 100.0, 1.0, SLEEVE_MAX, 25.0, &none(), 0.5).unwrap();
    let lines = build_orders(&a, &ps, &cs, 1_000_000.0, &none(), &set(&["A"]));
    let line = lines.iter().find(|o| o.symbol == "A").unwrap();
    assert!(line.qty_final == 0 && line.note == "guard exit");
    assert_eq!(line.action, OrderAction::Exit);
}

#[test]
fn worst_ranked_positions_sell_first() {
    let mut ps = positions(&["A".into(), "B".into(), "C".into()], 100, 100.0, 90.0);
    ps[1].rank = Some(5);
    ps[2].rank = Some(20);
    assert_eq!(order_sales(&ps, 1, &none())[0].symbol, "C");
}

#[test]
fn within_a_nearby_band_losers_sell_before_winners() {
    let ps = [
        Position::new("WIN", 100, 120.0, 100.0, Some(10)),
        Position::new("LOSE", 100, 80.0, 100.0, Some(11)),
    ];
    assert_eq!(order_sales(&ps, 5, &none())[0].symbol, "LOSE");
}

#[test]
fn tax_flagged_winners_sell_last_within_their_band() {
    let ps = [
        Position::new("CLEAR", 100, 120.0, 100.0, Some(10)),
        Position::new("NEARLTCG", 100, 120.0, 100.0, Some(11)),
    ];
    let order = order_sales(&ps, 5, &set(&["NEARLTCG"]));
    assert_eq!(order.last().unwrap().symbol, "NEARLTCG");
}

#[test]
fn unranked_positions_sell_early() {
    let ps = [
        Position::new("RANKED", 100, 100.0, 90.0, Some(1)),
        Position::new("GONE", 100, 100.0, 90.0, None),
    ];
    assert_eq!(order_sales(&ps, 100, &none())[0].symbol, "GONE");
}

#[test]
fn tax_review_marks_only_sell_lines() {
    let held = positions(&names(12), 1000, 100.0, 90.0);
    let (survivors, _) = select_survivors(
        &cands(12),
        &held,
        &SurvivorParams::new(15, NewBuyMode::Blocked),
    );
    let a = solve(&survivors, 40.0);
    let lines = build_orders(
        &a,
        &held,
        &cands(12),
        1_200_000.0,
        &set(&["S00", "S01"]),
        &none(),
    );
    for o in &lines {
        assert_eq!(
            o.tax_review,
            (o.symbol == "S00" || o.symbol == "S01") && o.delta < 0
        );
    }
}

#[test]
fn projected_equity_of_zero_capital_is_zero() {
    assert_eq!(projected_equity_pct(&[], 0.0), 0.0);
}
