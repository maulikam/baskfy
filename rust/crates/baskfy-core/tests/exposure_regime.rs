#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]
//! Port of `kite-momentum-rebalancer/tests/test_regime.py` onto `baskfy_core::exposure::regime`.

use std::collections::BTreeMap;

use baskfy_core::exposure::StrMap;
use baskfy_core::exposure::regime::{
    AFFIRMATIVE_RISK_OFF, ALGORITHM_VERSION, BookWeights, BootstrapPolicy, BreadthReading, Candle,
    DISPLAY_REASONS, EvaluateArgs, ExecutionStatus, ExposureSnapshot, ForcedAction, IndexSignals,
    MaSignal, NewBuyMode, PreviousRegime, RegimeConfig, RegimeMode, RegimeState, RegimeTier,
    SignalState, TargetPosition, WeightSource, apply_recovery_override, apply_sentinel_floor,
    apply_transition, bearish_stack_weight, build_index_signals, classify_candidate,
    confirmed_state_series, describe, evaluate, make_evaluation_id, moving_average,
    next_evaluation_date, reason, resolve_book_weights, resolve_new_buys, weighted_health,
};
use chrono::{Datelike, Duration, NaiveDate};

use SignalState::{Above as ABOVE, Below as BELOW, Unknown as UNKNOWN};

fn d(y: i32, m: u32, day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, day).unwrap()
}

fn approx(a: f64, b: f64) -> bool {
    (a - b).abs() <= 1e-6 * b.abs().max(1.0)
}

fn cfg() -> RegimeConfig {
    RegimeConfig::default()
}

fn candles_from(closes: &[f64], start: NaiveDate, fin: bool) -> Vec<Candle> {
    closes
        .iter()
        .enumerate()
        .map(|(i, c)| Candle {
            date: start + Duration::days(i64::try_from(i).unwrap()),
            open: Some(*c),
            high: Some(c * 1.01),
            low: Some(c * 0.99),
            close: *c,
            is_final: fin,
        })
        .collect()
}

fn candles(closes: &[f64]) -> Vec<Candle> {
    candles_from(closes, d(2026, 1, 1), true)
}

fn signals_for(name: &str, closes: &[f64], c: &RegimeConfig) -> IndexSignals {
    build_index_signals(name, &candles(closes), c, None, None).unwrap()
}

fn forced_full(
    name: &str,
    states: &[(usize, SignalState)],
    close: f64,
    mas: &[(usize, f64)],
    stale: bool,
    has_data: bool,
) -> IndexSignals {
    let mut signals = BTreeMap::new();
    for (length, state) in states {
        let ma = mas
            .iter()
            .find(|(l, _)| l == length)
            .map_or(close, |(_, m)| *m);
        signals.insert(
            *length,
            MaSignal {
                ma_length: *length,
                close,
                ma: Some(ma),
                distance_pct: Some((close / ma - 1.0) * 100.0),
                state: *state,
                confirming_closes: 3,
            },
        );
    }
    IndexSignals {
        index_name: name.to_owned(),
        as_of_date: Some(d(2026, 6, 1)),
        close: Some(close),
        signals,
        is_stale: stale,
        has_data,
        instrument_token: None,
    }
}

fn forced(name: &str, states: &[(usize, SignalState)]) -> IndexSignals {
    forced_full(name, states, 100.0, &[], false, true)
}

fn st3(a: SignalState, b: SignalState, c: SignalState) -> [(usize, SignalState); 3] {
    [(20, a), (50, b), (200, c)]
}

fn book_w(small: f64, mid: f64, large: f64) -> BookWeights {
    BookWeights {
        weights: [("smallcap", small), ("midcap", mid), ("largecap", large)]
            .into_iter()
            .collect(),
        source: WeightSource::ConfigDefault,
        coverage_pct: 100.0,
        reason_codes: Vec::new(),
    }
}

fn book() -> BookWeights {
    book_w(0.5, 0.3, 0.2)
}

fn breadth_full(pct: f64, as_of: NaiveDate, coverage: f64) -> BreadthReading {
    BreadthReading {
        as_of_date: as_of,
        pct_above_20dma: pct,
        eligible_count: 500,
        observed_count: 500,
        coverage_pct: coverage,
        universe_id: "nifty500".into(),
        universe_hash: "hash1".into(),
        audit_run_id: "run1".into(),
        calculation_version: "v1".into(),
    }
}

fn breadth(pct: f64) -> BreadthReading {
    breadth_full(pct, d(2026, 6, 1), 100.0)
}

fn all_indices(
    c: &RegimeConfig,
    state_map: &[(&str, [(usize, SignalState); 3])],
) -> BTreeMap<String, IndexSignals> {
    state_map
        .iter()
        .map(|(cat, st)| {
            let name = c.structural_indices.get(cat).unwrap().clone();
            let sig = forced(&name, st);
            (name, sig)
        })
        .collect()
}

fn uniform(c: &RegimeConfig, st: [(usize, SignalState); 3]) -> BTreeMap<String, IndexSignals> {
    let cats: Vec<String> = c.structural_indices.keys().map(str::to_owned).collect();
    let pairs: Vec<(&str, [(usize, SignalState); 3])> =
        cats.iter().map(|k| (k.as_str(), st)).collect();
    all_indices(c, &pairs)
}

fn healthy(c: &RegimeConfig) -> BTreeMap<String, IndexSignals> {
    uniform(c, st3(ABOVE, ABOVE, ABOVE))
}

fn sent2(s50: SignalState, s200: SignalState) -> IndexSignals {
    forced("NIFTY 500 MOMENTUM 50", &st3(ABOVE, s50, s200))
}

fn sent() -> IndexSignals {
    sent2(ABOVE, ABOVE)
}

struct Inputs {
    indices: BTreeMap<String, IndexSignals>,
    sentinel: IndexSignals,
    brd: Option<BreadthReading>,
    bk: BookWeights,
    exposure: ExposureSnapshot,
    previous: Option<PreviousRegime>,
    as_of: NaiveDate,
    week_end: NaiveDate,
    session: NaiveDate,
}

fn inputs(c: &RegimeConfig) -> Inputs {
    Inputs {
        indices: healthy(c),
        sentinel: sent(),
        brd: Some(breadth(60.0)),
        bk: book(),
        exposure: ExposureSnapshot::new(50.0),
        previous: None,
        as_of: d(2026, 6, 5),
        week_end: d(2026, 6, 5),
        session: d(2026, 6, 5),
    }
}

fn ev(c: &RegimeConfig, f: impl FnOnce(&mut Inputs)) -> RegimeState {
    let mut i = inputs(c);
    f(&mut i);
    evaluate(&EvaluateArgs {
        as_of_date: i.as_of,
        scheduled_week_end: i.week_end,
        signal_session_date: i.session,
        index_signals: &i.indices,
        sentinel: Some(&i.sentinel),
        breadth: i.brd.as_ref(),
        book: &i.bk,
        exposure: &i.exposure,
        cfg: c,
        previous: i.previous.as_ref(),
    })
    .unwrap()
}

fn prev(t: RegimeTier) -> Option<PreviousRegime> {
    Some(PreviousRegime::new(t, d(2026, 5, 29)))
}

fn has(st: &RegimeState, code: &str) -> bool {
    st.reason_codes.iter().any(|c| c == code)
}

fn hmap(pairs: &[(usize, f64)]) -> BTreeMap<usize, f64> {
    pairs.iter().copied().collect()
}

fn repeat(v: f64, n: usize) -> Vec<f64> {
    vec![v; n]
}

fn cat(parts: &[&[f64]]) -> Vec<f64> {
    parts.concat()
}

fn states_of(closes: &[f64], confirm_days: i64) -> Vec<(SignalState, i64)> {
    let mas = moving_average(closes, 20).unwrap();
    confirmed_state_series(closes, &mas, cfg().buffer(), confirm_days)
}

#[test]
fn two_confirming_closes_do_not_change_state() {
    let closes = cat(&[&repeat(100.0, 60), &repeat(80.0, 30), &[120.0, 121.0]]);
    assert_eq!(states_of(&closes, 3).last().unwrap().0, BELOW);
}

#[test]
fn third_confirming_close_changes_state() {
    let closes = cat(&[
        &repeat(100.0, 60),
        &repeat(80.0, 30),
        &[120.0, 121.0, 122.0],
    ]);
    assert_eq!(states_of(&closes, 3).last().unwrap().0, ABOVE);
}

#[test]
fn buffer_band_preserves_previous_state() {
    let closes = cat(&[&repeat(100.0, 60), &repeat(80.0, 30)]);
    let ma_now = moving_average(&closes, 20)
        .unwrap()
        .last()
        .unwrap()
        .unwrap();
    let inside = ma_now * 1.005;
    let closes2 = cat(&[&closes, &[inside, inside, inside]]);
    assert_eq!(states_of(&closes2, 3).last().unwrap().0, BELOW);
}

#[test]
fn each_confirmation_day_uses_that_days_own_ma() {
    let closes = cat(&[&repeat(10.0, 19), &[10.0, 40.0, 40.0, 12.0, 12.0]]);
    let mas = moving_average(&closes, 20).unwrap();
    let b = cfg().buffer();
    let per_day: Vec<bool> = closes
        .iter()
        .zip(&mas)
        .map(|(c, m)| m.is_some_and(|m| *c > m * (1.0 + b)))
        .collect();
    assert_eq!(&per_day[per_day.len() - 3..], &[true, false, false]);
    assert_eq!(states_of(&closes, 3).last().unwrap().0, UNKNOWN);
}

#[test]
fn upward_and_downward_hysteresis_are_symmetric() {
    let up = cat(&[&repeat(100.0, 60), &[130.0, 131.0, 132.0]]);
    let dn = cat(&[&repeat(100.0, 60), &[70.0, 69.0, 68.0]]);
    assert_eq!(states_of(&up, 3).last().unwrap().0, ABOVE);
    assert_eq!(states_of(&dn, 3).last().unwrap().0, BELOW);
}

#[test]
fn state_starts_unknown_and_stays_until_first_confirmation() {
    assert!(
        states_of(&repeat(100.0, 25), 3)
            .iter()
            .all(|(s, _)| *s == UNKNOWN)
    );
}

#[test]
fn confirming_close_counter() {
    let closes = cat(&[&repeat(100.0, 60), &repeat(130.0, 5)]);
    assert_eq!(states_of(&closes, 3).last().unwrap().1, 5);
}

#[test]
fn confirm_days_is_configurable() {
    let closes = cat(&[&repeat(100.0, 60), &repeat(80.0, 30), &[120.0, 121.0]]);
    assert_eq!(states_of(&closes, 2).last().unwrap().0, ABOVE);
}

#[test]
fn buffer_is_stored_in_basis_points() {
    assert_eq!(cfg().buffer_bps, 150);
    assert!(approx(cfg().buffer(), 0.015));
}

#[test]
fn insufficient_warmup_leaves_long_ma_unknown() {
    let closes: Vec<f64> = (0..120).map(|i| 100.0 + f64::from(i)).collect();
    let sig = signals_for("NIFTY 50", &closes, &cfg());
    assert_eq!(sig.state(20), ABOVE);
    assert_eq!(sig.state(50), ABOVE);
    assert_eq!(sig.state(200), UNKNOWN);
    assert!(sig.signals[&200].ma.is_none());
}

#[test]
fn unfinished_candle_is_never_consumed() {
    let fin = candles(&repeat(100.0, 30));
    let mut all = fin.clone();
    all.push(Candle {
        date: d(2026, 3, 1),
        open: Some(1.0),
        high: Some(1.0),
        low: Some(1.0),
        close: 9_999.0,
        is_final: false,
    });
    let sig = build_index_signals("NIFTY 50", &all, &cfg(), None, None).unwrap();
    assert_eq!(sig.close, Some(100.0));
    assert_eq!(sig.as_of_date, Some(fin.last().unwrap().date));
}

#[test]
fn no_final_candles_reports_no_data() {
    let cs = candles_from(&repeat(100.0, 5), d(2026, 1, 1), false);
    let sig = build_index_signals("NIFTY 50", &cs, &cfg(), None, None).unwrap();
    assert!(!sig.has_data && sig.any_unknown());
}

#[test]
fn staleness_is_measured_against_as_of() {
    let cs = candles(&repeat(100.0, 30));
    let last = cs.last().unwrap().date;
    let fresh = build_index_signals("NIFTY 50", &cs, &cfg(), Some(last), None).unwrap();
    let stale = build_index_signals(
        "NIFTY 50",
        &cs,
        &cfg(),
        Some(last + Duration::days(30)),
        None,
    )
    .unwrap();
    assert!(!fresh.is_stale && stale.is_stale);
}

fn tp(symbol: &str, category: Option<&str>, value: f64) -> TargetPosition {
    TargetPosition {
        symbol: symbol.into(),
        category: category.map(str::to_owned),
        market_value: value,
    }
}

fn w(b: &BookWeights, k: &str) -> f64 {
    *b.weights.get(k).unwrap()
}

#[test]
fn full_risk_target_is_preferred_source() {
    let t = [
        tp("A", Some("smallcap"), 600_000.0),
        tp("B", Some("midcap"), 300_000.0),
        tp("C", Some("largecap"), 100_000.0),
    ];
    let bw = resolve_book_weights(&cfg(), Some(&t), None).unwrap();
    assert_eq!(bw.source, WeightSource::FullRiskTarget);
    assert!(approx(w(&bw, "smallcap"), 0.6));
    assert!(approx(bw.weights.values().sum::<f64>(), 1.0));
}

#[test]
fn weights_use_market_value_not_position_count() {
    let t = [
        tp("A", Some("smallcap"), 900_000.0),
        tp("B", Some("largecap"), 50_000.0),
        tp("C", Some("largecap"), 50_000.0),
    ];
    let bw = resolve_book_weights(&cfg(), Some(&t), None).unwrap();
    assert!(approx(w(&bw, "smallcap"), 0.9));
}

#[test]
fn falls_back_to_last_r1_snapshot() {
    let mut snap = book_w(0.4, 0.4, 0.2);
    snap.source = WeightSource::FullRiskTarget;
    let bw = resolve_book_weights(&cfg(), None, Some(&snap)).unwrap();
    assert_eq!(bw.source, WeightSource::LastR1Snapshot);
    assert!(approx(w(&bw, "midcap"), 0.4));
}

#[test]
fn falls_back_to_config_defaults() {
    let bw = resolve_book_weights(&cfg(), None, None).unwrap();
    assert_eq!(bw.source, WeightSource::ConfigDefault);
    let expected: StrMap<f64> = [("smallcap", 0.5), ("midcap", 0.3), ("largecap", 0.2)]
        .into_iter()
        .collect();
    assert_eq!(bw.weights, expected);
    assert!(
        bw.reason_codes
            .iter()
            .any(|c| c == reason::BOOK_WEIGHTS_CONFIG_DEFAULT)
    );
}

#[test]
fn empty_holdings_fall_back_to_defaults() {
    let bw = resolve_book_weights(&cfg(), Some(&[]), None).unwrap();
    assert_eq!(bw.source, WeightSource::ConfigDefault);
}

#[test]
fn low_classification_coverage_falls_back_and_flags() {
    let t = [
        tp("A", Some("smallcap"), 100_000.0),
        tp("B", None, 900_000.0),
    ];
    let bw = resolve_book_weights(&cfg(), Some(&t), None).unwrap();
    assert_eq!(bw.source, WeightSource::ConfigDefault);
    assert!(
        bw.reason_codes
            .iter()
            .any(|c| c == reason::BOOK_CLASSIFICATION_COVERAGE_LOW)
    );
}

#[test]
fn unknown_category_is_never_silently_reassigned() {
    let t = [
        tp("A", Some("smallcap"), 850_000.0),
        tp("B", Some("midcap"), 100_000.0),
        tp("C", Some("mystery"), 50_000.0),
    ];
    let bw = resolve_book_weights(&cfg(), Some(&t), None).unwrap();
    assert!(!bw.weights.contains_key("mystery"));
    assert!(approx(bw.coverage_pct, 95.0));
    assert!(approx(w(&bw, "largecap"), 0.0));
}

#[test]
fn regime_reduced_holdings_are_never_reused_as_strategic_weights() {
    let full = [
        tp("S", Some("smallcap"), 500_000.0),
        tp("M", Some("midcap"), 300_000.0),
        tp("L", Some("largecap"), 200_000.0),
    ];
    let reduced = [
        tp("L", Some("largecap"), 200_000.0),
        tp("M", Some("midcap"), 50_000.0),
    ];
    let strategic = resolve_book_weights(&cfg(), Some(&full), None).unwrap();
    let residual = resolve_book_weights(&cfg(), Some(&reduced), None).unwrap();
    assert!(approx(w(&strategic, "smallcap"), 0.5));
    assert!(approx(w(&residual, "largecap"), 0.8));
    assert!(w(&strategic, "largecap") != w(&residual, "largecap"));
}

#[test]
fn book_weights_are_frozen_for_the_evaluation() {
    let c = cfg();
    let st = ev(&c, |i| i.bk = book_w(0.7, 0.2, 0.1));
    let expected: StrMap<f64> = [("smallcap", 0.7), ("midcap", 0.2), ("largecap", 0.1)]
        .into_iter()
        .collect();
    assert_eq!(st.book_category_weights, expected);
    assert_eq!(st.book_weight_source, WeightSource::ConfigDefault);
}

fn mixed(c: &RegimeConfig) -> BTreeMap<String, IndexSignals> {
    all_indices(
        c,
        &[
            ("largecap", st3(ABOVE, ABOVE, ABOVE)),
            ("midcap", st3(ABOVE, BELOW, ABOVE)),
            ("smallcap", st3(BELOW, BELOW, BELOW)),
        ],
    )
}

#[test]
fn weighted_health_per_ma_length() {
    let c = cfg();
    let idx = mixed(&c);
    assert!(approx(weighted_health(&book(), &idx, &c, 20), 0.5));
    assert!(approx(weighted_health(&book(), &idx, &c, 50), 0.2));
    assert!(approx(weighted_health(&book(), &idx, &c, 200), 0.5));
}

#[test]
fn unknown_contributes_zero_to_health() {
    let c = cfg();
    let idx = all_indices(
        &c,
        &[
            ("largecap", st3(UNKNOWN, UNKNOWN, UNKNOWN)),
            ("midcap", st3(ABOVE, ABOVE, ABOVE)),
            ("smallcap", st3(ABOVE, ABOVE, ABOVE)),
        ],
    );
    assert!(approx(weighted_health(&book(), &idx, &c, 200), 0.8));
}

#[test]
fn bearish_stack_weight_sums_stacked_categories() {
    let c = cfg();
    let stacked_mas = [(20, 90.0), (50, 100.0), (200, 110.0)];
    let ok_mas = [(20, 110.0), (50, 100.0), (200, 90.0)];
    let idx: BTreeMap<String, IndexSignals> = [
        forced_full(
            "NIFTY SMLCAP 250",
            &st3(BELOW, BELOW, BELOW),
            80.0,
            &stacked_mas,
            false,
            true,
        ),
        forced_full(
            "NIFTY 50",
            &st3(ABOVE, ABOVE, ABOVE),
            120.0,
            &ok_mas,
            false,
            true,
        ),
        forced_full(
            "NIFTY MIDCAP 150",
            &st3(BELOW, BELOW, BELOW),
            80.0,
            &stacked_mas,
            false,
            true,
        ),
    ]
    .into_iter()
    .map(|s| (s.index_name.clone(), s))
    .collect();
    assert!(approx(bearish_stack_weight(&book(), &idx, &c), 0.8));
}

#[test]
fn bearish_stack_requires_both_ordering_and_confirmed_below() {
    let sig = forced_full(
        "NIFTY 50",
        &st3(ABOVE, BELOW, BELOW),
        100.0,
        &[(20, 90.0), (50, 100.0), (200, 110.0)],
        false,
        true,
    );
    assert!(!sig.bearish_stack(&[20, 50, 200]));
}

fn classify(h50: f64, h200: f64, h20: f64, stack: f64, b: f64, s: &IndexSignals) -> RegimeTier {
    classify_candidate(
        &hmap(&[(20, h20), (50, h50), (200, h200)]),
        stack,
        Some(b),
        true,
        Some(s),
        &cfg(),
    )
    .0
}

#[test]
fn r1_requires_all_four_conditions() {
    assert_eq!(classify(0.8, 0.8, 1.0, 0.0, 55.0, &sent()), RegimeTier::R1);
}

#[test]
fn r1_boundaries_are_inclusive() {
    for (h50, h200, b, expected) in [
        (0.80, 0.80, 55.0, RegimeTier::R1),
        (0.7999, 0.80, 55.0, RegimeTier::R2),
        (0.80, 0.7999, 55.0, RegimeTier::R2),
        (0.80, 0.80, 54.99, RegimeTier::R2),
    ] {
        assert_eq!(classify(h50, h200, 1.0, 0.0, b, &sent()), expected);
    }
}

#[test]
fn r3_h200_boundary_is_inclusive() {
    assert_eq!(classify(0.6, 0.50, 1.0, 0.0, 60.0, &sent()), RegimeTier::R3);
    assert_eq!(
        classify(0.6, 0.5001, 1.0, 0.0, 60.0, &sent()),
        RegimeTier::R2
    );
}

#[test]
fn r3_breadth_boundary_is_exclusive() {
    assert_eq!(classify(0.6, 0.6, 1.0, 0.0, 39.99, &sent()), RegimeTier::R3);
    assert_eq!(classify(0.6, 0.6, 1.0, 0.0, 40.0, &sent()), RegimeTier::R2);
}

#[test]
fn r4_stack_and_breadth_boundaries() {
    for (stack, b, expected) in [
        (0.60, 29.99, RegimeTier::R4),
        (0.5999, 29.99, RegimeTier::R3),
        (0.60, 30.0, RegimeTier::R3),
    ] {
        assert_eq!(classify(0.0, 0.4, 0.0, stack, b, &sent()), expected);
    }
}

#[test]
fn r4_via_sentinel_health_and_breadth() {
    let (t, codes) = classify_candidate(
        &hmap(&[(20, 0.0), (50, 0.0), (200, 0.29)]),
        0.0,
        Some(29.0),
        true,
        Some(&sent2(ABOVE, BELOW)),
        &cfg(),
    );
    assert_eq!(t, RegimeTier::R4);
    assert!(codes.contains(&reason::R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH));
}

#[test]
fn r2_is_the_default() {
    let (t, codes) = classify_candidate(
        &hmap(&[(20, 0.7), (50, 0.7), (200, 0.7)]),
        0.0,
        Some(50.0),
        true,
        Some(&sent()),
        &cfg(),
    );
    assert!(t == RegimeTier::R2 && codes.contains(&reason::R2_DEFAULT));
}

#[test]
fn unusable_breadth_cannot_permit_r1() {
    let (t, _) = classify_candidate(
        &hmap(&[(20, 1.0), (50, 1.0), (200, 1.0)]),
        0.0,
        Some(90.0),
        false,
        Some(&sent()),
        &cfg(),
    );
    assert_eq!(t, RegimeTier::R2);
}

#[test]
fn unusable_breadth_cannot_independently_force_r4() {
    let (t, _) = classify_candidate(
        &hmap(&[(20, 0.0), (50, 0.0), (200, 0.6)]),
        1.0,
        Some(5.0),
        false,
        Some(&sent()),
        &cfg(),
    );
    assert_ne!(t, RegimeTier::R4);
}

#[test]
fn sentinel_below_50_floors_at_r2_and_blocks_buys() {
    let st = ev(&cfg(), |i| i.sentinel = sent2(BELOW, ABOVE));
    assert!(st.policy_tier.ordinal() >= RegimeTier::R2.ordinal());
    assert_eq!(st.new_buys, NewBuyMode::Blocked);
    assert!(has(&st, reason::SENTINEL_BELOW_50DMA_VETO));
}

#[test]
fn sentinel_below_50_does_not_cause_binary_full_exit() {
    let st = ev(&cfg(), |i| {
        i.sentinel = sent2(BELOW, ABOVE);
        i.exposure = ExposureSnapshot::new(100.0);
        i.previous = prev(RegimeTier::R1);
    });
    assert_eq!(st.policy_tier, RegimeTier::R2);
    assert_eq!(st.target_equity_cap_pct, 70.0);
    assert_eq!(st.forced_action, ForcedAction::TrimToCap);
}

#[test]
fn sentinel_below_200_forces_at_least_r3() {
    let (t, codes) = apply_sentinel_floor(RegimeTier::R1, Some(&sent2(ABOVE, BELOW)));
    assert_eq!(t, RegimeTier::R3);
    assert!(codes.contains(&reason::SENTINEL_FLOOR_R3));
}

#[test]
fn sentinel_floor_never_makes_the_tier_less_defensive() {
    let (t, _) = apply_sentinel_floor(RegimeTier::R4, Some(&sent2(BELOW, BELOW)));
    assert_eq!(t, RegimeTier::R4);
}

#[test]
fn veto_removal_does_not_by_itself_produce_r1() {
    let c = cfg();
    let st = ev(&c, |i| {
        i.indices = mixed(&c);
        i.previous = prev(RegimeTier::R2);
    });
    assert_ne!(st.policy_tier, RegimeTier::R1);
}

#[test]
fn recovery_override_lifts_r4_to_r2_only() {
    let (t, active, codes) = apply_recovery_override(
        RegimeTier::R4,
        &hmap(&[(20, 0.8)]),
        Some(60.0),
        true,
        2,
        &cfg(),
    );
    assert!(t == RegimeTier::R2 && active);
    assert!(codes.contains(&reason::RECOVERY_OVERRIDE_APPLIED));
}

#[test]
fn recovery_override_never_reaches_r1() {
    for start in [RegimeTier::R3, RegimeTier::R4] {
        let (t, _, _) =
            apply_recovery_override(start, &hmap(&[(20, 1.0)]), Some(99.0), true, 99, &cfg());
        assert_eq!(t, RegimeTier::R2);
    }
}

#[test]
fn recovery_override_requires_confirmation() {
    let (t, active, codes) = apply_recovery_override(
        RegimeTier::R4,
        &hmap(&[(20, 0.8)]),
        Some(60.0),
        true,
        1,
        &cfg(),
    );
    assert!(t == RegimeTier::R4 && !active);
    assert!(codes.contains(&reason::RECOVERY_OVERRIDE_UNCONFIRMED));
}

#[test]
fn recovery_override_requires_h20() {
    let (_, active, codes) = apply_recovery_override(
        RegimeTier::R4,
        &hmap(&[(20, 0.2)]),
        Some(60.0),
        true,
        5,
        &cfg(),
    );
    assert!(!active && codes.contains(&reason::RECOVERY_OVERRIDE_H20_TOO_WEAK));
}

#[test]
fn recovery_override_requires_usable_breadth() {
    let (_, active, codes) = apply_recovery_override(
        RegimeTier::R4,
        &hmap(&[(20, 0.9)]),
        Some(90.0),
        false,
        5,
        &cfg(),
    );
    assert!(!active);
    assert!(codes.contains(&reason::RECOVERY_OVERRIDE_BREADTH_UNUSABLE));
}

#[test]
fn recovery_override_does_not_bypass_the_rerisk_cap() {
    let (t, limited, _) = apply_transition(Some(RegimeTier::R4), RegimeTier::R2);
    assert!(t == RegimeTier::R3 && limited);
}

#[test]
fn recovery_override_keeps_buys_blocked_under_sentinel_veto() {
    let c = cfg();
    let st = ev(&c, |i| {
        i.indices = uniform(&c, st3(ABOVE, BELOW, BELOW));
        i.sentinel = sent2(BELOW, BELOW);
        i.brd = Some(breadth(60.0));
        let mut p = PreviousRegime::new(RegimeTier::R3, d(2026, 5, 29));
        p.recovery_confirmations = 5;
        i.previous = Some(p);
    });
    assert_eq!(st.new_buys, NewBuyMode::Blocked);
}

#[test]
fn risk_off_is_direct_and_skips_tiers() {
    for target in [RegimeTier::R3, RegimeTier::R4] {
        let (t, limited, codes) = apply_transition(Some(RegimeTier::R1), target);
        assert!(t == target && !limited);
        assert!(codes.contains(&reason::TRANSITION_RISK_OFF_DIRECT));
    }
}

#[test]
fn rerisking_is_capped_to_one_tier_per_week() {
    let (t, limited, codes) = apply_transition(Some(RegimeTier::R4), RegimeTier::R1);
    assert!(t == RegimeTier::R3 && limited);
    assert!(codes.contains(&reason::TRANSITION_RERISK_CAPPED));
}

#[test]
fn single_step_rerisk_is_not_flagged_as_limited() {
    let (t, limited, _) = apply_transition(Some(RegimeTier::R3), RegimeTier::R2);
    assert!(t == RegimeTier::R2 && !limited);
}

#[test]
fn rerisk_ladder_takes_three_weeks_from_r4_to_r1() {
    let mut tier = RegimeTier::R4;
    let mut seen = vec![tier];
    for _ in 0..3 {
        tier = apply_transition(Some(tier), RegimeTier::R1).0;
        seen.push(tier);
    }
    assert_eq!(
        seen,
        [
            RegimeTier::R4,
            RegimeTier::R3,
            RegimeTier::R2,
            RegimeTier::R1
        ]
    );
}

#[test]
fn unchanged_tier_reports_no_transition() {
    let (t, _, codes) = apply_transition(Some(RegimeTier::R2), RegimeTier::R2);
    assert!(t == RegimeTier::R2 && codes.contains(&reason::TRANSITION_NONE));
}

#[test]
fn same_week_rerun_is_idempotent() {
    let run = || {
        ev(&cfg(), |i| {
            i.sentinel = sent2(ABOVE, BELOW);
            i.previous = prev(RegimeTier::R1);
        })
    };
    let (a, b) = (run(), run());
    assert_eq!(
        serde_json::to_string(&a).unwrap(),
        serde_json::to_string(&b).unwrap()
    );
    assert_eq!(a.evaluation_id, b.evaluation_id);
}

#[test]
fn evaluation_id_is_deterministic_per_week_and_session() {
    let c = cfg();
    let x = make_evaluation_id(d(2026, 6, 5), d(2026, 6, 5), &c);
    let y = make_evaluation_id(d(2026, 6, 5), d(2026, 6, 5), &c);
    let z = make_evaluation_id(d(2026, 6, 12), d(2026, 6, 12), &c);
    assert!(x == y && x != z);
    assert_eq!(x.len(), 24);
}

#[test]
fn evaluation_id_changes_with_config() {
    let other = RegimeConfig {
        confirm_days: 5,
        ..cfg()
    };
    assert_ne!(
        make_evaluation_id(d(2026, 6, 5), d(2026, 6, 5), &cfg()),
        make_evaluation_id(d(2026, 6, 5), d(2026, 6, 5), &other)
    );
}

#[test]
fn next_evaluation_lands_on_the_configured_weekday() {
    let c = cfg();
    let nxt = next_evaluation_date(d(2026, 6, 5), &c);
    assert_eq!(
        i64::from(nxt.weekday().num_days_from_monday()),
        c.weekly_evaluation_weekday
    );
    assert_eq!(nxt, d(2026, 6, 12));
}

#[test]
fn holiday_week_uses_the_last_completed_session() {
    let st = ev(&cfg(), |i| {
        i.week_end = d(2026, 6, 5);
        i.session = d(2026, 6, 4);
    });
    assert_eq!(st.scheduled_week_end, d(2026, 6, 5));
    assert_eq!(st.signal_session_date, d(2026, 6, 4));
    assert_eq!(st.next_evaluation_date, d(2026, 6, 12));
}

fn crash(i: &mut Inputs, c: &RegimeConfig) {
    i.indices = uniform(c, st3(BELOW, BELOW, BELOW));
    i.sentinel = sent2(BELOW, BELOW);
    i.brd = Some(breadth(20.0));
}

#[test]
fn bootstrap_never_forces_sells() {
    let c = cfg();
    let st = ev(&c, |i| {
        crash(i, &c);
        i.exposure = ExposureSnapshot::new(95.0);
    });
    assert!(st.previous_policy_tier.is_none());
    assert_eq!(st.forced_action, ForcedAction::None);
    assert!(st.manual_action_required);
    assert!(has(&st, reason::BOOTSTRAP_OBSERVE_ONLY));
}

#[test]
fn bootstrap_adopt_candidate_policy_allows_reconciliation() {
    let c = RegimeConfig {
        bootstrap_policy: BootstrapPolicy::AdoptCandidate,
        ..cfg()
    };
    let st = ev(&c, |i| {
        crash(i, &c);
        i.exposure = ExposureSnapshot::new(95.0);
    });
    assert_ne!(st.forced_action, ForcedAction::None);
}

fn stale_up(c: &RegimeConfig) -> BTreeMap<String, IndexSignals> {
    c.structural_indices
        .values()
        .map(|n| {
            (
                n.clone(),
                forced_full(n, &st3(ABOVE, ABOVE, ABOVE), 100.0, &[], true, true),
            )
        })
        .collect()
}

#[test]
fn stale_index_retains_tier_blocks_buys_and_forces_no_sell() {
    let c = cfg();
    let st = ev(&c, |i| {
        i.indices = stale_up(&c);
        i.exposure = ExposureSnapshot::new(95.0);
        i.previous = prev(RegimeTier::R1);
    });
    assert_eq!(st.policy_tier, RegimeTier::R1);
    assert_eq!(st.new_buys, NewBuyMode::Blocked);
    assert_eq!(st.forced_action, ForcedAction::None);
    assert!(st.data_stale);
    assert!(has(&st, reason::TRANSITION_HELD_STALE_DATA));
}

#[test]
fn missing_index_retains_tier_and_blocks_buys() {
    let c = cfg();
    let st = ev(&c, |i| {
        i.indices.remove("NIFTY SMLCAP 250");
        i.previous = prev(RegimeTier::R2);
    });
    assert_eq!(st.policy_tier, RegimeTier::R2);
    assert_eq!(st.new_buys, NewBuyMode::Blocked);
    assert!(has(&st, reason::DATA_INDEX_MISSING));
}

#[test]
fn stale_breadth_blocks_buys_but_causes_no_forced_sell() {
    let st = ev(&cfg(), |i| {
        i.brd = Some(breadth_full(70.0, d(2026, 1, 1), 100.0));
        i.exposure = ExposureSnapshot::new(95.0);
        i.previous = prev(RegimeTier::R1);
    });
    assert_eq!(st.new_buys, NewBuyMode::Blocked);
    assert_eq!(st.forced_action, ForcedAction::None);
    assert!(has(&st, reason::BREADTH_STALE));
}

#[test]
fn genuine_risk_off_still_escalates_through_degraded_data() {
    let c = cfg();
    let st = ev(&c, |i| {
        let mut idx = all_indices(
            &c,
            &[
                ("largecap", st3(BELOW, BELOW, BELOW)),
                ("smallcap", st3(BELOW, BELOW, BELOW)),
            ],
        );
        idx.insert(
            "NIFTY MIDCAP 150".into(),
            forced_full(
                "NIFTY MIDCAP 150",
                &st3(ABOVE, ABOVE, ABOVE),
                100.0,
                &[],
                true,
                true,
            ),
        );
        i.indices = idx;
        i.brd = Some(breadth(35.0));
        i.exposure = ExposureSnapshot::new(90.0);
        i.previous = prev(RegimeTier::R1);
    });
    assert_eq!(st.policy_tier, RegimeTier::R3);
    assert_eq!(st.forced_action, ForcedAction::ReduceToCap);
    assert!(st.data_stale);
}

#[test]
fn degraded_data_can_never_rerisk() {
    let c = cfg();
    let st = ev(&c, |i| {
        i.indices = stale_up(&c);
        i.brd = Some(breadth(95.0));
        i.previous = prev(RegimeTier::R3);
    });
    assert_eq!(st.policy_tier, RegimeTier::R3);
    assert_eq!(st.new_buys, NewBuyMode::Blocked);
    assert!(has(&st, reason::TRANSITION_HELD_STALE_DATA));
}

#[test]
fn r2_default_is_not_an_affirmative_risk_off_signal() {
    assert!(!AFFIRMATIVE_RISK_OFF.contains(&reason::R2_DEFAULT));
    assert!(AFFIRMATIVE_RISK_OFF.contains(&reason::R3_H200_WEAK));
    assert!(AFFIRMATIVE_RISK_OFF.contains(&reason::SENTINEL_FLOOR_R3));
}

#[test]
fn low_coverage_breadth_is_rejected() {
    let st = ev(&cfg(), |i| {
        i.brd = Some(breadth_full(70.0, d(2026, 6, 1), 50.0));
        i.previous = prev(RegimeTier::R2);
    });
    assert!(has(&st, reason::BREADTH_LOW_COVERAGE));
    assert_eq!(st.new_buys, NewBuyMode::Blocked);
    assert_ne!(st.policy_tier, RegimeTier::R1);
}

#[test]
fn missing_breadth_is_flagged() {
    let st = ev(&cfg(), |i| {
        i.brd = None;
        i.previous = prev(RegimeTier::R2);
    });
    assert!(has(&st, reason::BREADTH_MISSING));
    assert_eq!(st.new_buys, NewBuyMode::Blocked);
}

#[test]
fn unknown_signal_cannot_permit_r1_or_new_buys() {
    let c = cfg();
    let st = ev(&c, |i| {
        i.indices = all_indices(
            &c,
            &[
                ("largecap", st3(ABOVE, ABOVE, ABOVE)),
                ("midcap", st3(ABOVE, ABOVE, ABOVE)),
                ("smallcap", st3(ABOVE, ABOVE, UNKNOWN)),
            ],
        );
        i.brd = Some(breadth(80.0));
        i.previous = prev(RegimeTier::R2);
    });
    assert_ne!(st.raw_candidate_tier, RegimeTier::R1);
    assert_ne!(st.policy_tier, RegimeTier::R1);
    assert_eq!(st.new_buys, NewBuyMode::Blocked);
    assert!(has(&st, reason::SIGNAL_UNKNOWN));
}

#[test]
fn committed_tier_is_not_evidence_the_cap_was_achieved() {
    let st = ev(&cfg(), |i| {
        i.sentinel = sent2(ABOVE, BELOW);
        i.exposure = ExposureSnapshot::new(90.0);
        i.previous = prev(RegimeTier::R1);
    });
    assert_eq!(st.policy_tier, RegimeTier::R3);
    assert_eq!(st.target_equity_cap_pct, 40.0);
    assert_eq!(st.actual_equity_pct, 90.0);
    assert!(approx(st.exposure_gap_pct, 50.0));
    assert_eq!(st.forced_action, ForcedAction::ReduceToCap);
}

#[test]
fn partial_execution_preserves_the_gap_without_a_new_transition() {
    let st = ev(&cfg(), |i| {
        i.sentinel = sent2(ABOVE, BELOW);
        i.exposure = ExposureSnapshot {
            actual_equity_pct: 65.0,
            pending_buy_pct: 0.0,
            pending_sell_pct: 25.0,
            execution_status: ExecutionStatus::Partial,
        };
        let mut p = PreviousRegime::new(RegimeTier::R3, d(2026, 6, 5));
        p.last_transition_date = Some(d(2026, 6, 5));
        i.previous = Some(p);
        i.week_end = d(2026, 6, 12);
        i.session = d(2026, 6, 12);
    });
    assert_eq!(st.policy_tier, RegimeTier::R3);
    assert!(!st.transition_limited);
    assert_eq!(st.last_transition_date, Some(d(2026, 6, 5)));
    assert!(approx(st.exposure_gap_pct, 25.0));
    assert_eq!(st.forced_action, ForcedAction::ReduceToCap);
    assert_eq!(st.execution_status, ExecutionStatus::Partial);
    assert!(has(&st, reason::EXPOSURE_RECONCILE_RETRY));
}

#[test]
fn exposure_within_tolerance_needs_no_action() {
    let st = ev(&cfg(), |i| {
        i.sentinel = sent2(ABOVE, BELOW);
        i.exposure = ExposureSnapshot::new(40.5);
        i.previous = prev(RegimeTier::R3);
    });
    assert_eq!(st.forced_action, ForcedAction::None);
    assert!(has(&st, reason::EXPOSURE_WITHIN_CAP));
}

#[test]
fn r4_is_not_a_full_exit_at_the_default_cap() {
    let c = cfg();
    assert_eq!(c.cap_for(RegimeTier::R4).unwrap(), 10.0);
    let st = ev(&c, |i| {
        crash(i, &c);
        i.exposure = ExposureSnapshot::new(80.0);
        i.previous = prev(RegimeTier::R1);
    });
    assert_eq!(st.policy_tier, RegimeTier::R4);
    assert_eq!(st.target_equity_cap_pct, 10.0);
    assert_eq!(st.forced_action, ForcedAction::CrashReduce);
}

#[test]
fn r4_can_be_configured_to_full_cash() {
    let c = RegimeConfig {
        tier_exposure_pct: [("R1", 100.0), ("R2", 70.0), ("R3", 40.0), ("R4", 0.0)]
            .into_iter()
            .collect(),
        ..cfg()
    };
    c.validate().unwrap();
    assert_eq!(c.cap_for(RegimeTier::R4).unwrap(), 0.0);
}

#[test]
fn r1_permits_full_size_entries() {
    let st = ev(&cfg(), |i| {
        i.brd = Some(breadth(80.0));
        i.previous = prev(RegimeTier::R1);
    });
    assert_eq!(st.policy_tier, RegimeTier::R1);
    assert_eq!(st.new_buys, NewBuyMode::Full);
}

#[test]
fn r2_permits_half_size_entries() {
    let c = cfg();
    let st = ev(&c, |i| {
        i.indices = all_indices(
            &c,
            &[
                ("largecap", st3(ABOVE, ABOVE, ABOVE)),
                ("midcap", st3(ABOVE, BELOW, ABOVE)),
                ("smallcap", st3(ABOVE, ABOVE, ABOVE)),
            ],
        );
        i.brd = Some(breadth(50.0));
        i.previous = prev(RegimeTier::R2);
    });
    assert_eq!(st.policy_tier, RegimeTier::R2);
    assert_eq!(st.new_buys, NewBuyMode::Half);
}

#[test]
fn r3_and_r4_block_new_entries() {
    for t in [RegimeTier::R3, RegimeTier::R4] {
        let (m, codes) = resolve_new_buys(t, Some(&sent()), true);
        assert_eq!(m, NewBuyMode::Blocked);
        assert!(codes.contains(&reason::NEW_BUYS_BLOCKED_TIER));
    }
}

fn config_err(c: &RegimeConfig) -> String {
    c.validate().unwrap_err().to_string()
}

#[test]
fn default_config_is_valid() {
    cfg().validate().unwrap();
}

#[test]
fn ma_lengths_must_be_strictly_increasing() {
    for lengths in [vec![50, 20, 200], vec![20, 20, 200]] {
        let c = RegimeConfig {
            ma_lengths: lengths,
            ..cfg()
        };
        assert!(config_err(&c).contains("strictly increasing"));
    }
}

#[test]
fn tier_caps_must_be_non_increasing() {
    let c = RegimeConfig {
        tier_exposure_pct: [("R1", 100.0), ("R2", 40.0), ("R3", 70.0), ("R4", 10.0)]
            .into_iter()
            .collect(),
        ..cfg()
    };
    assert!(config_err(&c).contains("non-increasing"));
}

#[test]
fn confirm_days_must_be_at_least_one() {
    let c = RegimeConfig {
        confirm_days: 0,
        ..cfg()
    };
    assert!(config_err(&c).contains("confirm_days"));
}

#[test]
fn buffer_bps_must_be_in_a_safe_range() {
    let c = RegimeConfig {
        buffer_bps: 5000,
        ..cfg()
    };
    assert!(config_err(&c).contains("buffer_bps"));
}

#[test]
fn thresholds_must_be_in_range() {
    let a = RegimeConfig {
        r1_breadth_min: 155.0,
        ..cfg()
    };
    assert!(config_err(&a).contains("percentage"));
    let b = RegimeConfig {
        r1_h200_min: 8.0,
        ..cfg()
    };
    assert!(config_err(&b).contains("weighted-health fraction"));
}

#[test]
fn weights_must_be_non_negative_and_normalizable() {
    let c = RegimeConfig {
        default_book_weights: [("smallcap", -1.0), ("midcap", 0.5), ("largecap", 0.5)]
            .into_iter()
            .collect(),
        ..cfg()
    };
    assert!(config_err(&c).contains("non-negative"));
}

#[test]
fn index_identifiers_must_be_non_empty() {
    let c = RegimeConfig {
        momentum_sentinel: "  ".into(),
        ..cfg()
    };
    assert!(config_err(&c).contains("non-empty"));
}

#[test]
fn validation_reports_every_problem_at_once() {
    let c = RegimeConfig {
        confirm_days: 0,
        buffer_bps: 9999,
        ..cfg()
    };
    let e = config_err(&c);
    assert!(e.contains("confirm_days") && e.contains("buffer_bps"));
}

#[test]
fn state_persists_algorithm_version_and_config_hash() {
    let c = cfg();
    let st = ev(&c, |_| {});
    assert_eq!(st.algorithm_version, ALGORITHM_VERSION);
    assert_eq!(st.config_hash.len(), 16);
    assert_eq!(st.config_hash, c.config_hash());
}

#[test]
fn config_hash_is_stable_across_instances() {
    assert_eq!(cfg().config_hash(), cfg().config_hash());
}

#[test]
fn config_hash_changes_when_a_threshold_changes() {
    let other = RegimeConfig {
        r1_breadth_min: 60.0,
        ..cfg()
    };
    assert_ne!(cfg().config_hash(), other.config_hash());
}

#[test]
fn config_hash_ignores_rollout_mode() {
    let a = RegimeConfig {
        mode: RegimeMode::Observe,
        ..cfg()
    };
    let b = RegimeConfig {
        mode: RegimeMode::Enforce,
        ..cfg()
    };
    assert_eq!(a.config_hash(), b.config_hash());
}

#[test]
fn every_reason_code_has_display_text() {
    for (code, text) in DISPLAY_REASONS {
        assert!(!code.is_empty() && !text.is_empty());
    }
    assert_eq!(DISPLAY_REASONS.len(), 42);
}

#[test]
fn state_reasons_align_with_codes() {
    let st = ev(&cfg(), |_| {});
    assert_eq!(st.reasons.len(), st.reason_codes.len());
    assert_eq!(st.reasons, describe(&st.reason_codes));
}

#[test]
fn reason_codes_are_deduplicated_and_ordered() {
    let st = ev(&cfg(), |i| i.sentinel = sent2(BELOW, BELOW));
    let uniq: std::collections::BTreeSet<&String> = st.reason_codes.iter().collect();
    assert_eq!(uniq.len(), st.reason_codes.len());
}

#[test]
fn state_serialises_to_json_safe_primitives() {
    let blob = serde_json::to_string(&ev(&cfg(), |_| {})).unwrap();
    assert!(blob.contains("\"policy_tier\""));
}

#[test]
fn live_and_replay_paths_are_identical() {
    let run = || {
        ev(&cfg(), |i| {
            i.brd = Some(breadth(80.0));
            i.exposure = ExposureSnapshot::new(60.0);
            i.previous = prev(RegimeTier::R1);
        })
    };
    assert_eq!(
        serde_json::to_string(&run()).unwrap(),
        serde_json::to_string(&run()).unwrap()
    );
}

#[test]
fn build_index_signals_is_deterministic() {
    let cs: Vec<f64> = (0..260).map(|i| 100.0 + f64::from(i % 7)).collect();
    let a = signals_for("NIFTY 50", &cs, &cfg());
    let b = signals_for("NIFTY 50", &cs, &cfg());
    assert_eq!(
        serde_json::to_string(&a).unwrap(),
        serde_json::to_string(&b).unwrap()
    );
}

#[test]
fn default_config_hash_is_pinned() {
    let c = cfg();
    assert_eq!(
        c.canonical_json(),
        concat!(
            r#"{"algorithm_version":"regime/1.0.0","bootstrap_policy":"observe_only","#,
            r#""breadth_required":true,"breadth_stale_days":10,"buffer_bps":150,"confirm_days":3,"#,
            r#""default_book_weights":{"largecap":0.2,"midcap":0.3,"smallcap":0.5},"#,
            r#""exchange":"NSE","exposure_tolerance_pct":1.0,"#,
            r#""index_aliases":{"NIFTY 500 MOMENTUM 50":["NIFTY500MOMENTM50"],"#,
            r#""NIFTY SMLCAP 250":["NIFTY SMALLCAP 250"]},"index_stale_days":5,"#,
            r#""ma_lengths":[20,50,200],"min_breadth_coverage_pct":90.0,"#,
            r#""min_classification_coverage_pct":80.0,"momentum_sentinel":"NIFTY 500 MOMENTUM 50","#,
            r#""r1_breadth_min":55.0,"r1_h200_min":0.8,"r1_h50_min":0.8,"r3_breadth_max":40.0,"#,
            r#""r3_h200_max":0.5,"r4_bearish_stack_min":0.6,"r4_breadth_max":30.0,"#,
            r#""r4_h200_max":0.3,"recovery_breadth_min":55.0,"recovery_confirm_evaluations":2,"#,
            r#""recovery_h20_min":0.5,"segment":"INDICES","#,
            r#""structural_indices":{"largecap":"NIFTY 50","midcap":"NIFTY MIDCAP 150","#,
            r#""smallcap":"NIFTY SMLCAP 250"},"#,
            r#""tier_exposure_pct":{"R1":100.0,"R2":70.0,"R3":40.0,"R4":10.0},"#,
            r#""weekly_evaluation_weekday":4}"#
        )
    );
}

#[test]
fn regime_source_stays_pure() {
    let src = include_str!("../src/exposure/regime.rs");
    for forbidden in [
        "place_order",
        "place_gtt",
        "OrderGateway",
        "kiteconnect",
        "sqlite",
        "reqwest",
        "::now(",
        "today()",
    ] {
        assert!(
            !src.contains(forbidden),
            "regime.rs must stay pure: {forbidden}"
        );
    }
}

#[test]
fn signal_states_round_trip_their_values() {
    for s in SignalState::ALL {
        assert_eq!(SignalState::parse(s.as_str()), Some(*s));
    }
    assert_eq!(RegimeTier::from_ordinal(0), RegimeTier::R1);
    assert_eq!(RegimeTier::from_ordinal(9), RegimeTier::R4);
}
