//! `tests/test_regime_view_backtest.py`: the view built from the core engine's stored rows, and
//! P2's enriched action rows (`enrich_orders`) and per-lot tax review (`tax_detail`).
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

use std::collections::BTreeMap;

use baskfy_desk::lanes::analytics::regime::RegimeConfig;
use baskfy_desk::lanes::analytics::regime::alloc::{Candidate, Position};
use baskfy_desk::lanes::analytics::regime::engine::{
    self as e, BreadthReading, EvaluateArgs, ExposureSnapshot, IndexSignals, MaSignal,
    PreviousRegime, RegimeMode, RegimeState, RegimeTier, SignalState,
};
use baskfy_desk::lanes::analytics::regime::run::{self as rr, PlanKnobs};
use baskfy_desk::lanes::analytics::regime_store as rs;
use baskfy_desk::lanes::analytics::regime_view::{self as rv, DeskConstants};
use baskfy_desk::lanes::weekly::weekly_books::tradebook::{self as tb, Fill};
use baskfy_desk::storage::{Conn, Db};
use baskfy_desk::wire::{PyDict, PyValue, dict};
use chrono::{NaiveDate, NaiveDateTime};

const NOW: &str = "2026-06-05T18:00:00";

fn d(y: i32, m: u32, day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, day).unwrap()
}

fn today() -> NaiveDate {
    d(2026, 6, 5)
}

async fn sqlite() -> (tempfile::TempDir, Conn) {
    let dir = tempfile::tempdir().unwrap();
    let mut conn = Db::connect_sqlite_path(&dir.path().join("p.db"))
        .await
        .unwrap();
    conn.migrate().await.unwrap();
    (dir, conn)
}

fn sig(name: &str, week: NaiveDate, lengths: &[usize]) -> IndexSignals {
    IndexSignals {
        index_name: name.to_owned(),
        as_of_date: Some(week),
        close: Some(100.0),
        signals: lengths
            .iter()
            .map(|m| {
                (
                    *m,
                    MaSignal {
                        ma_length: *m,
                        close: 100.0,
                        ma: Some(95.0),
                        distance_pct: Some(5.26),
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

/// `make_state(cfg, tier=R2, week=2026-06-05)`.
fn make_state(tier: RegimeTier) -> RegimeState {
    let cfg = RegimeConfig::default().to_core().unwrap();
    let week = today();
    let idx: BTreeMap<String, IndexSignals> = cfg
        .structural_indices
        .values()
        .map(|n| (n.clone(), sig(n, week, &cfg.ma_lengths)))
        .collect();
    let sent = sig(&cfg.momentum_sentinel, week, &cfg.ma_lengths);
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
    let prev = PreviousRegime::new(tier, d(2026, 5, 29));
    e::evaluate(&EvaluateArgs {
        as_of_date: week,
        scheduled_week_end: week,
        signal_session_date: week,
        index_signals: &idx,
        sentinel: Some(&sent),
        breadth: Some(&brd),
        book: &book,
        exposure: &ExposureSnapshot::new(85.0),
        cfg: &cfg,
        previous: Some(&prev),
    })
    .unwrap()
}

async fn view(c: &mut Conn) -> PyDict {
    let s = baskfy_desk::Settings::defaults().unwrap();
    let k = DeskConstants::resolve(&s, &BTreeMap::new());
    rv::build(c, &s, &k, "desk", today()).await.unwrap()
}

fn get<'a>(v: &'a PyDict, k: &str) -> &'a PyValue {
    v.get(k).unwrap_or_else(|| panic!("{k} missing"))
}

fn s(x: &str) -> PyValue {
    PyValue::Str(x.to_owned())
}

// =================================================================================================
// the view reads what the core engine's writers stored
// =================================================================================================

#[tokio::test]
async fn empty_database_reports_unavailable() {
    let (_d, mut c) = sqlite().await;
    let v = view(&mut c).await;
    assert_eq!(get(&v, "available"), &PyValue::Bool(false));
    assert!(v.get("message").is_some());
}

#[tokio::test]
async fn view_builds_from_a_stored_preview() {
    let (_d, mut c) = sqlite().await;
    let st = make_state(RegimeTier::R2);
    rs::save_preview(
        &mut c,
        &st,
        RegimeMode::Observe,
        true,
        None,
        None,
        "aa",
        NOW,
    )
    .await
    .unwrap();
    let v = view(&mut c).await;
    assert_eq!(get(&v, "available"), &PyValue::Bool(true));
    assert_eq!(get(&v, "committed"), &PyValue::Bool(false));
    assert_eq!(get(&v, "policy_tier"), &s(st.policy_tier.as_str()));
    assert_eq!(get(&v, "algorithm_version"), &s(e::ALGORITHM_VERSION));
    assert_eq!(
        get(&v, "config_hash"),
        &s(&RegimeConfig::default().config_hash())
    );
    let PyValue::List(cards) = get(&v, "structural_cards") else {
        panic!()
    };
    assert_eq!(cards.len(), 3);
    for card in cards {
        let PyValue::Map(card) = card else { panic!() };
        let PyValue::List(mas) = get(card, "mas") else {
            panic!()
        };
        let lengths: Vec<&PyValue> = mas
            .iter()
            .map(|m| match m {
                PyValue::Map(m) => get(m, "length"),
                _ => panic!(),
            })
            .collect();
        assert_eq!(
            lengths,
            [&PyValue::Int(20), &PyValue::Int(50), &PyValue::Int(200)]
        );
    }
}

#[tokio::test]
async fn committed_row_is_preferred_over_previews() {
    let (_d, mut c) = sqlite().await;
    let st = make_state(RegimeTier::R2);
    rs::save_preview(
        &mut c,
        &st,
        RegimeMode::Observe,
        true,
        None,
        None,
        "aa",
        NOW,
    )
    .await
    .unwrap();
    rs::commit_evaluation(&mut c, &st, RegimeMode::Enforce, false, None, NOW)
        .await
        .unwrap();
    let v = view(&mut c).await;
    assert_eq!(get(&v, "committed"), &PyValue::Bool(true));
    assert_eq!(get(&v, "run_id"), &s("canonical"));
}

#[tokio::test]
async fn view_renders_stored_proposed_orders() {
    let (_d, mut c) = sqlite().await;
    let st = make_state(RegimeTier::R2);
    let cands: Vec<Candidate> = (0..12)
        .map(|i| Candidate::new(&format!("S{i:02}"), i + 1, 85.0 - i as f64, 100.0))
        .collect();
    let held: Vec<Position> = (0..12)
        .map(|i| Position::new(&format!("S{i:02}"), 1000, 100.0, 90.0, Some(i + 1)))
        .collect();
    let plan = rr::plan_regime_rebalance(
        &st,
        &cands,
        &held,
        1_200_000.0,
        &[],
        RegimeMode::Observe,
        true,
        &PlanKnobs::default(),
    )
    .unwrap();
    let snap = rr::evaluation_snapshot(Some(&plan), None);
    rs::save_preview(
        &mut c,
        &st,
        RegimeMode::Observe,
        true,
        None,
        Some(&snap),
        "aa",
        NOW,
    )
    .await
    .unwrap();
    let v = view(&mut c).await;
    let n = |k: &str| match get(&v, k) {
        PyValue::List(l) => l.len(),
        other => panic!("{k}: {other:?}"),
    };
    assert_eq!(
        n("proposed_orders"),
        plan.orders.iter().filter(|o| o.delta != 0).count()
    );
    assert_eq!(
        n("proposed_sells") + n("proposed_buys"),
        n("proposed_orders")
    );
    let PyValue::Map(summary) = get(&v, "plan_summary") else {
        panic!()
    };
    assert_eq!(get(summary, "sleeve_pct"), &PyValue::Float(plan.sleeve_pct));
}

#[tokio::test]
async fn view_is_read_only() {
    let (_d, mut c) = sqlite().await;
    rs::save_preview(
        &mut c,
        &make_state(RegimeTier::R2),
        RegimeMode::Observe,
        true,
        None,
        None,
        "aa",
        NOW,
    )
    .await
    .unwrap();
    let tables = [
        "regime_evaluations",
        "regime_exposure",
        "breadth_readings",
        "regime_book_snapshots",
        "index_series",
        "snapshots",
    ];
    let mut counts = Vec::new();
    for round in 0..2 {
        if round == 1 {
            for _ in 0..3 {
                view(&mut c).await;
            }
        }
        let mut now = Vec::new();
        for t in tables {
            let cur = c
                .execute(&format!("SELECT COUNT(*) AS n FROM {t}"), &[])
                .await
                .unwrap();
            now.push(
                cur.rows[0]
                    .get("n")
                    .and_then(baskfy_desk::storage::DbValue::as_i64),
            );
        }
        counts.push(now);
    }
    assert_eq!(counts[0], counts[1]);
}

// =================================================================================================
// P2 — enriched action rows and per-lot tax review
// =================================================================================================

fn order(sym: &str, delta: i64, px: f64, extra: &[(&str, PyValue)]) -> PyDict {
    let mut o = dict([
        ("symbol", s(sym)),
        ("action", s("EXIT")),
        ("qty_now", PyValue::Int(100)),
        ("delta", PyValue::Int(delta)),
        ("qty_final", PyValue::Int(0)),
        ("ref_price", PyValue::Float(px)),
        ("weight_nav", PyValue::Float(0.0)),
        ("weight_sleeve", PyValue::Float(0.0)),
        ("value", PyValue::Float(delta.abs() as f64 * px)),
    ]);
    for (k, v) in extra {
        o.insert(*k, v.clone());
    }
    o
}

fn sale() -> PyDict {
    order("AAA", -100, 100.0, &[])
}

async fn stock(c: &mut Conn, sym: &str, qty: i64, price: f64, when: &str) {
    let at = NaiveDateTime::parse_from_str(when, "%Y-%m-%dT%H:%M:%S").unwrap();
    let mut fill = Fill::new(sym, at, "BUY", qty, price);
    fill.trade_id = format!("{sym}{when}");
    tb::store_fills(c, &[fill], "console_csv", at)
        .await
        .unwrap();
    tb::rebuild_symbols(c, &[sym.to_owned()]).await.unwrap();
}

fn empty_portfolio() -> PyDict {
    dict([("tradeable", PyValue::List(Vec::new()))])
}

async fn enrich(c: &mut Conn, orders: &[PyDict], port: &PyDict) -> Vec<PyDict> {
    rv::enrich_orders(c, orders, port, 0.01, 45, today(), None)
        .await
        .unwrap()
}

async fn detail(c: &mut Conn, orders: &[PyDict]) -> Vec<PyDict> {
    rv::tax_detail(c, orders, 45, today()).await.unwrap()
}

#[tokio::test]
async fn an_order_carries_the_weight_it_moves_from_and_to() {
    let (_d, mut c) = sqlite().await;
    let port = dict([(
        "tradeable",
        PyValue::List(vec![PyValue::Map(dict([
            ("symbol", s("AAA")),
            ("weight_pct", PyValue::Float(6.4)),
            ("pnl_pct", PyValue::Float(12.5)),
            ("pnl_value", PyValue::Float(5000.0)),
            ("value", PyValue::Float(1e5)),
            ("excluded", PyValue::Bool(false)),
        ]))]),
    )]);
    let got = enrich(
        &mut c,
        &[order(
            "AAA",
            -100,
            100.0,
            &[("weight_nav", PyValue::Float(2.0))],
        )],
        &port,
    )
    .await;
    let g = &got[0];
    assert_eq!(get(g, "weight_now"), &PyValue::Float(6.4));
    assert_eq!(get(g, "weight_target"), &PyValue::Float(2.0));
    assert_eq!(get(g, "pnl_pct"), &PyValue::Float(12.5));
    assert_eq!(get(g, "pnl_value"), &PyValue::Float(5000.0));
    let keys: Vec<&str> = g.iter().map(|(k, _)| k).collect();
    assert_eq!(
        &keys[9..],
        [
            "weight_now",
            "weight_target",
            "pnl_pct",
            "pnl_value",
            "days_to_ltcg",
            "tax_flagged",
            "tax_codes",
            "tax_gain",
            "liquidity_warning",
            "order_status",
            "filled_qty"
        ]
    );
}

#[tokio::test]
async fn an_order_for_a_position_not_in_the_portfolio_keeps_its_row() {
    let (_d, mut c) = sqlite().await;
    let got = enrich(&mut c, &[order("AAA", 50, 100.0, &[])], &empty_portfolio()).await;
    assert_eq!(got.len(), 1);
    assert_eq!(get(&got[0], "weight_now"), &PyValue::None);
}

#[tokio::test]
async fn a_sale_reports_the_soonest_lot_to_reach_long_term() {
    let (_d, mut c) = sqlite().await;
    stock(&mut c, "AAA", 100, 90.0, "2026-01-05T10:00:00").await;
    let got = enrich(&mut c, &[sale()], &empty_portfolio()).await;
    let PyValue::Int(days) = get(&got[0], "days_to_ltcg") else {
        panic!("{:?}", got[0])
    };
    assert!(*days > 0);
    assert_eq!(get(&got[0], "tax_flagged"), &PyValue::Bool(false));
    assert_eq!(get(&got[0], "tax_gain"), &PyValue::Float(1000.0));
}

#[tokio::test]
async fn a_purchase_is_not_tax_reviewed() {
    let (_d, mut c) = sqlite().await;
    let got = enrich(&mut c, &[order("AAA", 50, 100.0, &[])], &empty_portfolio()).await;
    assert_eq!(get(&got[0], "days_to_ltcg"), &PyValue::None);
    assert_eq!(get(&got[0], "tax_flagged"), &PyValue::None);
    assert_eq!(get(&got[0], "tax_codes"), &PyValue::List(Vec::new()));
}

#[tokio::test]
async fn a_failed_tax_review_does_not_look_tax_clear() {
    let (_d, mut c) = sqlite().await;
    c.execute("DROP TABLE trades", &[]).await.unwrap();
    let got = enrich(&mut c, &[sale()], &empty_portfolio()).await;
    assert_eq!(get(&got[0], "tax_flagged"), &PyValue::None);
    assert_eq!(get(&got[0], "tax_gain"), &PyValue::None);
}

#[tokio::test]
async fn a_trade_past_the_liquidity_cap_is_warned_and_a_missing_median_is_not() {
    let (_d, mut c) = sqlite().await;
    let port = |median: f64| {
        dict([(
            "tradeable",
            PyValue::List(vec![PyValue::Map(dict([
                ("symbol", s("AAA")),
                ("median_day_value", PyValue::Float(median)),
            ]))]),
        )])
    };
    let warned = enrich(&mut c, &[sale()], &port(500_000.0)).await;
    assert_eq!(get(&warned[0], "liquidity_warning"), &PyValue::Bool(true));
    let fine = enrich(&mut c, &[sale()], &port(5_000_000.0)).await;
    assert_eq!(get(&fine[0], "liquidity_warning"), &PyValue::Bool(false));
    let unknown = enrich(&mut c, &[sale()], &port(0.0)).await;
    assert_eq!(get(&unknown[0], "liquidity_warning"), &PyValue::Bool(false));
}

#[tokio::test]
async fn every_proposed_sale_gets_its_lots_not_only_flagged_ones() {
    let (_d, mut c) = sqlite().await;
    stock(&mut c, "AAA", 100, 90.0, "2026-01-05T10:00:00").await;
    let out = detail(&mut c, &[sale()]).await;
    assert_eq!(out.len(), 1);
    let PyValue::List(lots) = get(&out[0], "lots") else {
        panic!()
    };
    assert!(!lots.is_empty());
    assert_eq!(get(&out[0], "flagged"), &PyValue::Bool(false));
}

#[tokio::test]
async fn lot_detail_carries_the_acquisition_price() {
    let (_d, mut c) = sqlite().await;
    stock(&mut c, "AAA", 100, 90.0, "2026-01-05T10:00:00").await;
    let out = detail(&mut c, &[sale()]).await;
    let PyValue::List(lots) = get(&out[0], "lots") else {
        panic!()
    };
    let PyValue::Map(lot) = &lots[0] else {
        panic!()
    };
    assert_eq!(get(lot, "acquired_at"), &PyValue::Float(90.0));
    assert_eq!(get(lot, "acquired_on"), &s("2026-01-05"));
    assert_eq!(get(lot, "long_term"), &PyValue::Bool(false));
    assert_eq!(get(lot, "gain"), &PyValue::Float(1000.0));
}

#[tokio::test]
async fn uncovered_quantity_is_reported() {
    let (_d, mut c) = sqlite().await;
    stock(&mut c, "AAA", 40, 90.0, "2026-01-05T10:00:00").await;
    let out = detail(&mut c, &[sale()]).await;
    assert_eq!(get(&out[0], "covered_qty"), &PyValue::Int(40));
    assert_eq!(get(&out[0], "uncovered_qty"), &PyValue::Int(60));
    assert_eq!(get(&out[0], "data_unknown"), &PyValue::Bool(true));
    assert_eq!(get(&out[0], "manual_review"), &PyValue::Bool(true));
}

#[tokio::test]
async fn short_and_long_term_quantities_are_split() {
    let (_d, mut c) = sqlite().await;
    stock(&mut c, "AAA", 100, 90.0, "2020-01-05T10:00:00").await;
    let out = detail(&mut c, &[sale()]).await;
    assert_eq!(get(&out[0], "long_term_qty"), &PyValue::Int(100));
    assert_eq!(get(&out[0], "short_term_qty"), &PyValue::Int(0));
}

#[tokio::test]
async fn a_purchase_produces_no_tax_detail() {
    let (_d, mut c) = sqlite().await;
    assert!(
        detail(&mut c, &[order("AAA", 10, 100.0, &[])])
            .await
            .is_empty()
    );
}

#[tokio::test]
async fn a_sale_whose_lots_cannot_be_read_is_skipped_not_fatal() {
    let (_d, mut c) = sqlite().await;
    c.execute("DROP TABLE trades", &[]).await.unwrap();
    assert!(detail(&mut c, &[sale()]).await.is_empty());
}

#[tokio::test]
async fn a_malformed_delta_raises_where_python_raises() {
    let (_d, mut c) = sqlite().await;
    let bad = order("AAA", 0, 100.0, &[("delta", s("lots"))]);
    assert!(
        rv::enrich_orders(
            &mut c,
            &[bad.clone()],
            &empty_portfolio(),
            0.01,
            45,
            today(),
            None
        )
        .await
        .is_err()
    );
    assert!(rv::tax_detail(&mut c, &[bad], 45, today()).await.is_err());
}
