//! `tests/test_regime.py` (checkpoint 1: the pure engine) ported test for test, plus
//! `tests/test_exposure_moved_to_core.py`'s purity and naming guarantees. Every input is an
//! explicit fixture — no broker, no network, no clock, no database.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp,
    clippy::too_many_lines,
    clippy::cast_precision_loss,
    missing_docs
)]

use std::collections::BTreeMap;

use chrono::{Datelike, Duration, NaiveDate};

use super::*;
use crate::lanes::analytics::breadth::BreadthReading;
use crate::lanes::analytics::regime::{AFFIRMATIVE_RISK_OFF, DISPLAY_REASONS, RegimeConfig};

const ABOVE: SignalState = SignalState::Above;
const BELOW: SignalState = SignalState::Below;
const UNKNOWN: SignalState = SignalState::Unknown;

fn d(y: i32, m: u32, day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, day).unwrap()
}

fn approx(a: f64, b: f64) -> bool {
    (a - b).abs() <= 1e-6 * b.abs().max(1e-12)
}

fn cfg() -> RegimeConfig {
    RegimeConfig::default()
}

/// `candles(closes, start, final)`: daily candles with a synthetic OHLC around each close.
fn candles(closes: &[f64], start: NaiveDate, fin: bool) -> Vec<Candle> {
    closes
        .iter()
        .enumerate()
        .map(|(i, c)| Candle {
            date: start + Duration::days(i as i64),
            open: Some(*c),
            high: Some(c * 1.01),
            low: Some(c * 0.99),
            close: Some(*c),
            is_final: fin,
        })
        .collect()
}

fn signals_for(name: &str, closes: &[f64], cfg: &RegimeConfig) -> IndexSignals {
    build_index_signals(name, &candles(closes, d(2026, 1, 1), true), cfg, None, None).unwrap()
}

/// `forced_signals(name, states=, close=, mas=, stale=, has_data=)`: signals built directly, to
/// isolate the classifier from the MA maths.
fn forced(
    name: &str,
    states: &[(i64, SignalState)],
    close: f64,
    mas: &[(i64, f64)],
    stale: bool,
    has_data: bool,
) -> IndexSignals {
    let mut sig = BTreeMap::new();
    for (length, state) in states {
        let ma = mas
            .iter()
            .find(|(l, _)| l == length)
            .map_or(close, |(_, m)| *m);
        sig.insert(
            *length,
            MaSignal::new(
                *length,
                close,
                Some(ma),
                Some((close / ma - 1.0) * 100.0),
                *state,
                3,
            ),
        );
    }
    IndexSignals {
        index_name: name.to_owned(),
        as_of_date: Some(d(2026, 6, 1)),
        close: Some(close),
        signals: sig,
        is_stale: stale,
        has_data,
        instrument_token: None,
    }
}

fn st3(a: SignalState, b: SignalState, c: SignalState) -> [(i64, SignalState); 3] {
    [(20, a), (50, b), (200, c)]
}

fn fs(name: &str, s: &[(i64, SignalState)]) -> IndexSignals {
    forced(name, s, 100.0, &[], false, true)
}

fn book(small: f64, mid: f64, large: f64) -> BookWeights {
    BookWeights::new(
        &[("smallcap", small), ("midcap", mid), ("largecap", large)],
        WeightSource::ConfigDefault,
        100.0,
    )
}

fn breadth(pct: f64, as_of: NaiveDate, coverage: f64) -> BreadthReading {
    BreadthReading {
        as_of_date: as_of,
        pct_above_20dma: pct,
        eligible_count: 500,
        observed_count: 500,
        coverage_pct: coverage,
        universe_id: "nifty500".to_owned(),
        universe_hash: "hash1".to_owned(),
        audit_run_id: "run1".to_owned(),
        calculation_version: "v1".to_owned(),
    }
}

fn b60() -> BreadthReading {
    breadth(60.0, d(2026, 6, 1), 100.0)
}

fn bp(pct: f64) -> BreadthReading {
    breadth(pct, d(2026, 6, 1), 100.0)
}

/// `all_indices(cfg, state_map=)`: category → states.
fn all_indices(cfg: &RegimeConfig, map: &[(&str, [(i64, SignalState); 3])]) -> SignalMap {
    map.iter()
        .map(|(cat, st)| {
            let name = cfg
                .structural_indices
                .iter()
                .find(|(c, _)| c == cat)
                .map(|(_, n)| n.clone())
                .unwrap();
            (name.clone(), fs(&name, st))
        })
        .collect()
}

fn healthy(cfg: &RegimeConfig) -> SignalMap {
    let cats: Vec<String> = cfg.structural_indices.iter().map(|(c, _)| c.clone()).collect();
    let map: Vec<(&str, [(i64, SignalState); 3])> = cats
        .iter()
        .map(|c| (c.as_str(), st3(ABOVE, ABOVE, ABOVE)))
        .collect();
    all_indices(cfg, &map)
}

fn all_with(cfg: &RegimeConfig, st: [(i64, SignalState); 3]) -> SignalMap {
    let cats: Vec<String> = cfg.structural_indices.iter().map(|(c, _)| c.clone()).collect();
    let map: Vec<(&str, [(i64, SignalState); 3])> = cats.iter().map(|c| (c.as_str(), st)).collect();
    all_indices(cfg, &map)
}

fn sent(s50: SignalState, s200: SignalState) -> IndexSignals {
    fs("NIFTY 500 MOMENTUM 50", &st3(ABOVE, s50, s200))
}

/// The test file's `evaluate(...)` helper and its defaults.
struct Ev {
    indices: Option<SignalMap>,
    sentinel: Option<IndexSignals>,
    brd: Option<Option<BreadthReading>>,
    bk: Option<BookWeights>,
    exposure: Option<ExposureSnapshot>,
    previous: Option<PreviousRegime>,
    as_of: NaiveDate,
    week_end: NaiveDate,
    session: NaiveDate,
}

impl Default for Ev {
    fn default() -> Self {
        Self {
            indices: None,
            sentinel: None,
            brd: None,
            bk: None,
            exposure: None,
            previous: None,
            as_of: d(2026, 6, 5),
            week_end: d(2026, 6, 5),
            session: d(2026, 6, 5),
        }
    }
}

fn ev(cfg: &RegimeConfig, e: Ev) -> RegimeState {
    let indices = e.indices.unwrap_or_else(|| healthy(cfg));
    let sentinel = e.sentinel.unwrap_or_else(|| {
        fs(&cfg.momentum_sentinel, &st3(ABOVE, ABOVE, ABOVE))
    });
    let brd = e.brd.unwrap_or_else(|| Some(b60()));
    let bk = e.bk.unwrap_or_else(|| book(0.5, 0.3, 0.2));
    evaluate(&EvaluateInput {
        as_of_date: e.as_of,
        scheduled_week_end: e.week_end,
        signal_session_date: e.session,
        index_signals: &indices,
        sentinel: Some(&sentinel),
        breadth: brd.as_ref(),
        book: &bk,
        exposure: e.exposure.unwrap_or(ExposureSnapshot::actual(50.0)),
        cfg,
        previous: e.previous.as_ref(),
    })
}

fn prev(t: RegimeTier) -> PreviousRegime {
    PreviousRegime::new(t, d(2026, 5, 29))
}

fn has(st: &RegimeState, code: &str) -> bool {
    st.reason_codes.iter().any(|c| c == code)
}

fn rep(v: f64, n: usize) -> Vec<f64> {
    vec![v; n]
}

fn cat(parts: &[Vec<f64>]) -> Vec<f64> {
    parts.concat()
}

// =============================================================================================
// hysteresis / signal state
// =============================================================================================

#[test]
fn test_two_confirming_closes_do_not_change_state() {
    let c = cfg();
    let closes = cat(&[rep(100.0, 60), rep(80.0, 30), vec![120.0, 121.0]]);
    let mas = moving_average(&closes, 20);
    let states = confirmed_state_series(&closes, &mas, c.buffer(), 3);
    assert_eq!(states.last().unwrap().0, BELOW);
}

#[test]
fn test_third_confirming_close_changes_state() {
    let c = cfg();
    let closes = cat(&[rep(100.0, 60), rep(80.0, 30), vec![120.0, 121.0, 122.0]]);
    let mas = moving_average(&closes, 20);
    let states = confirmed_state_series(&closes, &mas, c.buffer(), 3);
    assert_eq!(states.last().unwrap().0, ABOVE);
}

#[test]
fn test_buffer_band_preserves_previous_state() {
    let c = cfg();
    let closes = cat(&[rep(100.0, 60), rep(80.0, 30)]);
    let mas = moving_average(&closes, 20);
    let inside = mas.last().unwrap().unwrap() * 1.005;
    let closes2 = cat(&[closes, vec![inside, inside, inside]]);
    let mas2 = moving_average(&closes2, 20);
    let states = confirmed_state_series(&closes2, &mas2, c.buffer(), 3);
    assert_eq!(states.last().unwrap().0, BELOW);
}

#[test]
fn test_each_confirmation_day_uses_that_days_own_ma() {
    let c = cfg();
    let closes = cat(&[rep(10.0, 19), vec![10.0, 40.0, 40.0, 12.0, 12.0]]);
    let mas = moving_average(&closes, 20);
    let per_day: Vec<Option<bool>> = closes
        .iter()
        .zip(&mas)
        .map(|(c2, m)| m.map(|m| *c2 > m * (1.0 + c.buffer())))
        .collect();
    assert_eq!(
        per_day[per_day.len() - 3..].to_vec(),
        vec![Some(true), Some(false), Some(false)]
    );
    let states = confirmed_state_series(&closes, &mas, c.buffer(), 3);
    assert_eq!(states.last().unwrap().0, UNKNOWN);
}

#[test]
fn test_upward_and_downward_hysteresis_are_symmetric() {
    let c = cfg();
    let up = cat(&[rep(100.0, 60), vec![130.0, 131.0, 132.0]]);
    let dn = cat(&[rep(100.0, 60), vec![70.0, 69.0, 68.0]]);
    for (series, expected) in [(up, ABOVE), (dn, BELOW)] {
        let mas = moving_average(&series, 20);
        let st = confirmed_state_series(&series, &mas, c.buffer(), 3);
        assert_eq!(st.last().unwrap().0, expected);
    }
}

#[test]
fn test_state_starts_unknown_and_stays_until_first_confirmation() {
    let c = cfg();
    let closes = rep(100.0, 25);
    let mas = moving_average(&closes, 20);
    let states = confirmed_state_series(&closes, &mas, c.buffer(), 3);
    assert!(states.iter().all(|(s, _)| *s == UNKNOWN));
}

#[test]
fn test_confirming_close_counter() {
    let c = cfg();
    let closes = cat(&[rep(100.0, 60), rep(130.0, 5)]);
    let mas = moving_average(&closes, 20);
    let states = confirmed_state_series(&closes, &mas, c.buffer(), 3);
    assert_eq!(states.last().unwrap().1, 5);
}

#[test]
fn test_confirm_days_is_configurable() {
    let c = cfg();
    let closes = cat(&[rep(100.0, 60), rep(80.0, 30), vec![120.0, 121.0]]);
    let mas = moving_average(&closes, 20);
    let two = confirmed_state_series(&closes, &mas, c.buffer(), 2);
    assert_eq!(two.last().unwrap().0, ABOVE);
}

#[test]
fn test_buffer_is_stored_in_basis_points() {
    let c = cfg();
    assert_eq!(c.buffer_bps, 150);
    assert!(approx(c.buffer(), 0.015));
}

// =============================================================================================
// warm-up / unfinished candles
// =============================================================================================

#[test]
fn test_insufficient_warmup_leaves_long_ma_unknown() {
    let c = cfg();
    let closes: Vec<f64> = (0..120).map(|i| 100.0 + f64::from(i)).collect();
    let sig = signals_for("NIFTY 50", &closes, &c);
    assert_eq!(sig.state(20), ABOVE);
    assert_eq!(sig.state(50), ABOVE);
    assert_eq!(sig.state(200), UNKNOWN);
    assert!(sig.signals[&200].ma.is_none());
}

#[test]
fn test_unfinished_candle_is_never_consumed() {
    let c = cfg();
    let mut cs = candles(&rep(100.0, 30), d(2026, 1, 1), true);
    let last = cs.last().unwrap().date;
    cs.push(Candle {
        date: d(2026, 3, 1),
        open: Some(1.0),
        high: Some(1.0),
        low: Some(1.0),
        close: Some(9_999.0),
        is_final: false,
    });
    let sig = build_index_signals("NIFTY 50", &cs, &c, None, None).unwrap();
    assert_eq!(sig.close, Some(100.0));
    assert_eq!(sig.as_of_date, Some(last));
}

#[test]
fn test_no_final_candles_reports_no_data() {
    let c = cfg();
    let sig = build_index_signals(
        "NIFTY 50",
        &candles(&rep(100.0, 5), d(2026, 1, 1), false),
        &c,
        None,
        None,
    )
    .unwrap();
    assert!(!sig.has_data && sig.any_unknown());
}

#[test]
fn test_staleness_is_measured_against_as_of() {
    let c = cfg();
    let cs = candles(&rep(100.0, 30), d(2026, 1, 1), true);
    let last = cs.last().unwrap().date;
    let fresh = build_index_signals("NIFTY 50", &cs, &c, Some(last), None).unwrap();
    let stale =
        build_index_signals("NIFTY 50", &cs, &c, Some(last + Duration::days(30)), None).unwrap();
    assert!(!fresh.is_stale && stale.is_stale);
}

#[test]
fn a_null_close_raises_as_pythons_arithmetic_does() {
    let c = cfg();
    let mut cs = candles(&rep(100.0, 3), d(2026, 1, 1), true);
    cs[1].close = None;
    assert!(matches!(
        build_index_signals("X", &cs, &c, None, None),
        Err(EngineError::Type(_))
    ));
}

// =============================================================================================
// book-composition weights
// =============================================================================================

fn tp(sym: &str, cat: Option<&str>, v: f64) -> TargetPosition {
    TargetPosition::new(sym, cat, v)
}

#[test]
fn test_full_risk_target_is_preferred_source() {
    let c = cfg();
    let target = [
        tp("A", Some("smallcap"), 600_000.0),
        tp("B", Some("midcap"), 300_000.0),
        tp("C", Some("largecap"), 100_000.0),
    ];
    let bw = resolve_book_weights(&c, Some(&target), None).unwrap();
    assert_eq!(bw.source, WeightSource::FullRiskTarget);
    assert!(approx(bw.weight("smallcap"), 0.6));
    assert!(approx(py_sum(bw.weights.iter().map(|(_, v)| *v)), 1.0));
}

#[test]
fn test_weights_use_market_value_not_position_count() {
    let c = cfg();
    let target = [
        tp("A", Some("smallcap"), 900_000.0),
        tp("B", Some("largecap"), 50_000.0),
        tp("C", Some("largecap"), 50_000.0),
    ];
    let bw = resolve_book_weights(&c, Some(&target), None).unwrap();
    assert!(approx(bw.weight("smallcap"), 0.9));
}

#[test]
fn test_falls_back_to_last_r1_snapshot() {
    let c = cfg();
    let snap = BookWeights::new(
        &[("smallcap", 0.4), ("midcap", 0.4), ("largecap", 0.2)],
        WeightSource::FullRiskTarget,
        100.0,
    );
    let bw = resolve_book_weights(&c, None, Some(&snap)).unwrap();
    assert_eq!(bw.source, WeightSource::LastR1Snapshot);
    assert!(approx(bw.weight("midcap"), 0.4));
}

#[test]
fn test_falls_back_to_config_defaults() {
    let c = cfg();
    let bw = resolve_book_weights(&c, None, None).unwrap();
    assert_eq!(bw.source, WeightSource::ConfigDefault);
    assert_eq!(
        bw.weights,
        vec![
            ("smallcap".to_owned(), 0.5),
            ("midcap".to_owned(), 0.3),
            ("largecap".to_owned(), 0.2)
        ]
    );
    assert!(bw.reason_codes.contains(&reason::BOOK_WEIGHTS_CONFIG_DEFAULT));
}

#[test]
fn test_empty_holdings_fall_back_to_defaults() {
    let c = cfg();
    let bw = resolve_book_weights(&c, Some(&[]), None).unwrap();
    assert_eq!(bw.source, WeightSource::ConfigDefault);
}

#[test]
fn test_low_classification_coverage_falls_back_and_flags() {
    let c = cfg();
    let target = [tp("A", Some("smallcap"), 100_000.0), tp("B", None, 900_000.0)];
    let bw = resolve_book_weights(&c, Some(&target), None).unwrap();
    assert_eq!(bw.source, WeightSource::ConfigDefault);
    assert!(bw.reason_codes.contains(&reason::BOOK_CLASSIFICATION_COVERAGE_LOW));
}

#[test]
fn test_unknown_category_is_never_silently_reassigned() {
    let c = cfg();
    let target = [
        tp("A", Some("smallcap"), 850_000.0),
        tp("B", Some("midcap"), 100_000.0),
        tp("C", Some("mystery"), 50_000.0),
    ];
    let bw = resolve_book_weights(&c, Some(&target), None).unwrap();
    assert!(bw.get("mystery").is_none());
    assert!(approx(bw.coverage_pct, 95.0));
    assert!(bw.weight("largecap").abs() < 1e-12);
}

#[test]
fn test_regime_reduced_holdings_are_never_reused_as_strategic_weights() {
    let c = cfg();
    let full = [
        tp("S", Some("smallcap"), 500_000.0),
        tp("M", Some("midcap"), 300_000.0),
        tp("L", Some("largecap"), 200_000.0),
    ];
    let reduced = [tp("L", Some("largecap"), 200_000.0), tp("M", Some("midcap"), 50_000.0)];
    let strategic = resolve_book_weights(&c, Some(&full), None).unwrap();
    let residual = resolve_book_weights(&c, Some(&reduced), None).unwrap();
    assert!(approx(strategic.weight("smallcap"), 0.5));
    assert!(approx(residual.weight("largecap"), 0.8));
    assert_ne!(strategic.weight("largecap"), residual.weight("largecap"));
}

#[test]
fn test_book_weights_are_frozen_for_the_evaluation() {
    let c = cfg();
    let st = ev(
        &c,
        Ev {
            bk: Some(book(0.7, 0.2, 0.1)),
            ..Ev::default()
        },
    );
    assert_eq!(
        st.book_category_weights,
        vec![
            ("smallcap".to_owned(), 0.7),
            ("midcap".to_owned(), 0.2),
            ("largecap".to_owned(), 0.1)
        ]
    );
    assert_eq!(st.book_weight_source, WeightSource::ConfigDefault);
}

// =============================================================================================
// weighted health
// =============================================================================================

#[test]
fn test_weighted_health_per_ma_length() {
    let c = cfg();
    let bk = book(0.5, 0.3, 0.2);
    let idx = all_indices(
        &c,
        &[
            ("largecap", st3(ABOVE, ABOVE, ABOVE)),
            ("midcap", st3(ABOVE, BELOW, ABOVE)),
            ("smallcap", st3(BELOW, BELOW, BELOW)),
        ],
    );
    assert!(approx(weighted_health(&bk, &idx, &c, 20), 0.5));
    assert!(approx(weighted_health(&bk, &idx, &c, 50), 0.2));
    assert!(approx(weighted_health(&bk, &idx, &c, 200), 0.5));
}

#[test]
fn test_unknown_contributes_zero_to_health() {
    let c = cfg();
    let bk = book(0.5, 0.3, 0.2);
    let idx = all_indices(
        &c,
        &[
            ("largecap", st3(UNKNOWN, UNKNOWN, UNKNOWN)),
            ("midcap", st3(ABOVE, ABOVE, ABOVE)),
            ("smallcap", st3(ABOVE, ABOVE, ABOVE)),
        ],
    );
    assert!(approx(weighted_health(&bk, &idx, &c, 200), 0.8));
}

#[test]
fn test_bearish_stack_weight() {
    let c = cfg();
    let bk = book(0.5, 0.3, 0.2);
    let stacked = forced(
        "NIFTY SMLCAP 250",
        &st3(BELOW, BELOW, BELOW),
        80.0,
        &[(20, 90.0), (50, 100.0), (200, 110.0)],
        false,
        true,
    );
    let ok = forced(
        "NIFTY 50",
        &st3(ABOVE, ABOVE, ABOVE),
        120.0,
        &[(20, 110.0), (50, 100.0), (200, 90.0)],
        false,
        true,
    );
    let mid = forced(
        "NIFTY MIDCAP 150",
        &st3(BELOW, BELOW, BELOW),
        80.0,
        &[(20, 90.0), (50, 100.0), (200, 110.0)],
        false,
        true,
    );
    let idx: SignalMap = vec![
        ("NIFTY SMLCAP 250".to_owned(), stacked),
        ("NIFTY 50".to_owned(), ok),
        ("NIFTY MIDCAP 150".to_owned(), mid),
    ];
    assert!(approx(bearish_stack_weight(&bk, &idx, &c), 0.8));
}

#[test]
fn test_bearish_stack_requires_both_ordering_and_confirmed_below() {
    let sig = forced(
        "NIFTY 50",
        &st3(ABOVE, BELOW, BELOW),
        100.0,
        &[(20, 90.0), (50, 100.0), (200, 110.0)],
        false,
        true,
    );
    assert!(!sig.bearish_stack(&[20, 50, 200]));
}

// =============================================================================================
// classifier boundaries
// =============================================================================================

fn hp(h20: f64, h50: f64, h200: f64) -> Vec<(i64, f64)> {
    vec![(20, h20), (50, h50), (200, h200)]
}

#[test]
fn test_r1_requires_all_four_conditions() {
    let c = cfg();
    let s = sent(ABOVE, ABOVE);
    let (t, _) = classify_candidate(&hp(1.0, 0.8, 0.8), 0.0, Some(55.0), true, Some(&s), &c);
    assert_eq!(t, RegimeTier::R1);
}

#[test]
fn test_r1_boundaries_are_inclusive() {
    let c = cfg();
    let s = sent(ABOVE, ABOVE);
    for (h50, h200, brd, expected) in [
        (0.80, 0.80, 55.0, RegimeTier::R1),
        (0.7999, 0.80, 55.0, RegimeTier::R2),
        (0.80, 0.7999, 55.0, RegimeTier::R2),
        (0.80, 0.80, 54.99, RegimeTier::R2),
    ] {
        let (t, _) =
            classify_candidate(&hp(1.0, h50, h200), 0.0, Some(brd), true, Some(&s), &c);
        assert_eq!(t, expected, "{h50} {h200} {brd}");
    }
}

#[test]
fn test_r3_h200_boundary_is_inclusive() {
    let c = cfg();
    let s = sent(ABOVE, ABOVE);
    for (h200, expected) in [(0.50, RegimeTier::R3), (0.5001, RegimeTier::R2)] {
        let (t, _) = classify_candidate(&hp(1.0, 0.6, h200), 0.0, Some(60.0), true, Some(&s), &c);
        assert_eq!(t, expected);
    }
}

#[test]
fn test_r3_breadth_boundary_is_exclusive() {
    let c = cfg();
    let s = sent(ABOVE, ABOVE);
    for (brd, expected) in [(39.99, RegimeTier::R3), (40.0, RegimeTier::R2)] {
        let (t, _) = classify_candidate(&hp(1.0, 0.6, 0.6), 0.0, Some(brd), true, Some(&s), &c);
        assert_eq!(t, expected);
    }
}

#[test]
fn test_r4_stack_and_breadth_boundaries() {
    let c = cfg();
    let s = sent(ABOVE, ABOVE);
    for (stack, brd, expected) in [
        (0.60, 29.99, RegimeTier::R4),
        (0.5999, 29.99, RegimeTier::R3),
        (0.60, 30.0, RegimeTier::R3),
    ] {
        let (t, _) =
            classify_candidate(&hp(0.0, 0.0, 0.4), stack, Some(brd), true, Some(&s), &c);
        assert_eq!(t, expected);
    }
}

#[test]
fn test_r4_via_sentinel_health_and_breadth() {
    let c = cfg();
    let s = sent(ABOVE, BELOW);
    let (t, codes) = classify_candidate(&hp(0.0, 0.0, 0.29), 0.0, Some(29.0), true, Some(&s), &c);
    assert_eq!(t, RegimeTier::R4);
    assert!(codes.contains(&reason::R4_SENTINEL_BEAR_WEAK_HEALTH_BREADTH));
}

#[test]
fn test_r2_is_the_default() {
    let c = cfg();
    let s = sent(ABOVE, ABOVE);
    let (t, codes) = classify_candidate(&hp(0.7, 0.7, 0.7), 0.0, Some(50.0), true, Some(&s), &c);
    assert!(t == RegimeTier::R2 && codes.contains(&reason::R2_DEFAULT));
}

#[test]
fn test_unusable_breadth_cannot_permit_r1() {
    let c = cfg();
    let s = sent(ABOVE, ABOVE);
    let (t, _) = classify_candidate(&hp(1.0, 1.0, 1.0), 0.0, Some(90.0), false, Some(&s), &c);
    assert_eq!(t, RegimeTier::R2);
}

#[test]
fn test_unusable_breadth_cannot_independently_force_r4() {
    let c = cfg();
    let s = sent(ABOVE, ABOVE);
    let (t, _) = classify_candidate(&hp(0.0, 0.0, 0.6), 1.0, Some(5.0), false, Some(&s), &c);
    assert_ne!(t, RegimeTier::R4);
}

// =============================================================================================
// momentum sentinel veto
// =============================================================================================

#[test]
fn test_sentinel_below_50_floors_at_r2_and_blocks_buys() {
    let c = cfg();
    let st = ev(
        &c,
        Ev {
            sentinel: Some(sent(BELOW, ABOVE)),
            ..Ev::default()
        },
    );
    assert!(st.policy_tier.ordinal() >= RegimeTier::R2.ordinal());
    assert_eq!(st.new_buys, NewBuyMode::Blocked);
    assert!(has(&st, reason::SENTINEL_BELOW_50DMA_VETO));
}

#[test]
fn test_sentinel_below_50_does_not_cause_binary_full_exit() {
    let c = cfg();
    let st = ev(
        &c,
        Ev {
            sentinel: Some(sent(BELOW, ABOVE)),
            exposure: Some(ExposureSnapshot::actual(100.0)),
            previous: Some(prev(RegimeTier::R1)),
            ..Ev::default()
        },
    );
    assert_eq!(st.policy_tier, RegimeTier::R2);
    assert_eq!(st.target_equity_cap_pct, 70.0);
    assert_eq!(st.forced_action, ForcedAction::TrimToCap);
}

#[test]
fn test_sentinel_below_200_forces_at_least_r3() {
    let s = sent(ABOVE, BELOW);
    let (tier, codes) = apply_sentinel_floor(RegimeTier::R1, Some(&s));
    assert_eq!(tier, RegimeTier::R3);
    assert!(codes.contains(&reason::SENTINEL_FLOOR_R3));
}

#[test]
fn test_sentinel_floor_never_makes_the_tier_less_defensive() {
    let s = sent(BELOW, BELOW);
    let (tier, _) = apply_sentinel_floor(RegimeTier::R4, Some(&s));
    assert_eq!(tier, RegimeTier::R4);
}

#[test]
fn test_veto_removal_does_not_by_itself_produce_r1() {
    let c = cfg();
    let idx = all_indices(
        &c,
        &[
            ("largecap", st3(ABOVE, ABOVE, ABOVE)),
            ("midcap", st3(ABOVE, BELOW, ABOVE)),
            ("smallcap", st3(BELOW, BELOW, BELOW)),
        ],
    );
    let st = ev(
        &c,
        Ev {
            indices: Some(idx),
            sentinel: Some(sent(ABOVE, ABOVE)),
            previous: Some(prev(RegimeTier::R2)),
            ..Ev::default()
        },
    );
    assert_ne!(st.policy_tier, RegimeTier::R1);
}

// =============================================================================================
// recovery override
// =============================================================================================

#[test]
fn test_recovery_override_lifts_r4_to_r2_only() {
    let c = cfg();
    let (tier, active, codes) =
        apply_recovery_override(RegimeTier::R4, &[(20, 0.8)], Some(60.0), true, 2, &c);
    assert!(tier == RegimeTier::R2 && active);
    assert!(codes.contains(&reason::RECOVERY_OVERRIDE_APPLIED));
}

#[test]
fn test_recovery_override_never_reaches_r1() {
    let c = cfg();
    for start in [RegimeTier::R3, RegimeTier::R4] {
        let (tier, _, _) =
            apply_recovery_override(start, &[(20, 1.0)], Some(99.0), true, 99, &c);
        assert_eq!(tier, RegimeTier::R2);
    }
}

#[test]
fn test_recovery_override_requires_confirmation() {
    let c = cfg();
    let (tier, active, codes) =
        apply_recovery_override(RegimeTier::R4, &[(20, 0.8)], Some(60.0), true, 1, &c);
    assert!(tier == RegimeTier::R4 && !active);
    assert!(codes.contains(&reason::RECOVERY_OVERRIDE_UNCONFIRMED));
}

#[test]
fn test_recovery_override_requires_h20() {
    let c = cfg();
    let (_, active, codes) =
        apply_recovery_override(RegimeTier::R4, &[(20, 0.2)], Some(60.0), true, 5, &c);
    assert!(!active && codes.contains(&reason::RECOVERY_OVERRIDE_H20_TOO_WEAK));
}

#[test]
fn test_recovery_override_requires_usable_breadth() {
    let c = cfg();
    let (_, active, codes) =
        apply_recovery_override(RegimeTier::R4, &[(20, 0.9)], Some(90.0), false, 5, &c);
    assert!(!active);
    assert!(codes.contains(&reason::RECOVERY_OVERRIDE_BREADTH_UNUSABLE));
}

#[test]
fn test_recovery_override_does_not_bypass_the_rerisk_cap() {
    let (policy, limited, _) = apply_transition(Some(RegimeTier::R4), RegimeTier::R2);
    assert!(policy == RegimeTier::R3 && limited);
}

#[test]
fn test_recovery_override_keeps_buys_blocked_under_sentinel_veto() {
    let c = cfg();
    let st = ev(
        &c,
        Ev {
            indices: Some(all_with(&c, st3(ABOVE, BELOW, BELOW))),
            sentinel: Some(sent(BELOW, BELOW)),
            brd: Some(Some(bp(60.0))),
            previous: Some(PreviousRegime {
                recovery_confirmations: 5,
                ..prev(RegimeTier::R3)
            }),
            ..Ev::default()
        },
    );
    assert_eq!(st.new_buys, NewBuyMode::Blocked);
}

// =============================================================================================
// transitions
// =============================================================================================

#[test]
fn test_risk_off_is_direct_and_skips_tiers() {
    for target in [RegimeTier::R3, RegimeTier::R4] {
        let (policy, limited, codes) = apply_transition(Some(RegimeTier::R1), target);
        assert!(policy == target && !limited);
        assert!(codes.contains(&reason::TRANSITION_RISK_OFF_DIRECT));
    }
}

#[test]
fn test_rerisking_is_capped_to_one_tier_per_week() {
    let (policy, limited, codes) = apply_transition(Some(RegimeTier::R4), RegimeTier::R1);
    assert!(policy == RegimeTier::R3 && limited);
    assert!(codes.contains(&reason::TRANSITION_RERISK_CAPPED));
}

#[test]
fn test_single_step_rerisk_is_not_flagged_as_limited() {
    let (policy, limited, _) = apply_transition(Some(RegimeTier::R3), RegimeTier::R2);
    assert!(policy == RegimeTier::R2 && !limited);
}

#[test]
fn test_rerisk_ladder_takes_three_weeks_from_r4_to_r1() {
    let mut tier = RegimeTier::R4;
    let mut seen = vec![tier];
    for _ in 0..3 {
        tier = apply_transition(Some(tier), RegimeTier::R1).0;
        seen.push(tier);
    }
    assert_eq!(
        seen,
        vec![RegimeTier::R4, RegimeTier::R3, RegimeTier::R2, RegimeTier::R1]
    );
}

#[test]
fn test_unchanged_tier_reports_no_transition() {
    let (policy, _, codes) = apply_transition(Some(RegimeTier::R2), RegimeTier::R2);
    assert!(policy == RegimeTier::R2 && codes.contains(&reason::TRANSITION_NONE));
}

#[test]
fn test_same_week_rerun_is_idempotent() {
    let c = cfg();
    let mk = || {
        ev(
            &c,
            Ev {
                sentinel: Some(sent(ABOVE, BELOW)),
                previous: Some(prev(RegimeTier::R1)),
                ..Ev::default()
            },
        )
    };
    let (a, b) = (mk(), mk());
    assert_eq!(a.as_dict(), b.as_dict());
    assert_eq!(a.evaluation_id, b.evaluation_id);
}

#[test]
fn test_evaluation_id_is_deterministic_per_week_and_session() {
    let c = cfg();
    let x = make_evaluation_id(d(2026, 6, 5), d(2026, 6, 5), &c);
    let y = make_evaluation_id(d(2026, 6, 5), d(2026, 6, 5), &c);
    let z = make_evaluation_id(d(2026, 6, 12), d(2026, 6, 12), &c);
    assert!(x == y && x != z);
    assert_eq!(x.len(), 24);
}

#[test]
fn test_evaluation_id_changes_with_config() {
    let c = cfg();
    let other = RegimeConfig {
        confirm_days: 5,
        ..cfg()
    };
    assert_ne!(
        make_evaluation_id(d(2026, 6, 5), d(2026, 6, 5), &c),
        make_evaluation_id(d(2026, 6, 5), d(2026, 6, 5), &other)
    );
}

#[test]
fn test_next_evaluation_lands_on_the_configured_weekday() {
    let c = cfg();
    let nxt = next_evaluation_date(d(2026, 6, 5), &c);
    assert_eq!(
        i64::from(nxt.weekday().num_days_from_monday()),
        c.weekly_evaluation_weekday
    );
    assert_eq!(nxt, d(2026, 6, 12));
}

#[test]
fn test_holiday_week_uses_the_last_completed_session() {
    let c = cfg();
    let st = ev(
        &c,
        Ev {
            week_end: d(2026, 6, 5),
            session: d(2026, 6, 4),
            ..Ev::default()
        },
    );
    assert_eq!(st.scheduled_week_end, d(2026, 6, 5));
    assert_eq!(st.signal_session_date, d(2026, 6, 4));
    assert_eq!(st.next_evaluation_date, d(2026, 6, 12));
}

// =============================================================================================
// bootstrap
// =============================================================================================

#[test]
fn test_bootstrap_never_forces_sells() {
    let c = cfg();
    let st = ev(
        &c,
        Ev {
            indices: Some(all_with(&c, st3(BELOW, BELOW, BELOW))),
            sentinel: Some(sent(BELOW, BELOW)),
            brd: Some(Some(bp(20.0))),
            exposure: Some(ExposureSnapshot::actual(95.0)),
            ..Ev::default()
        },
    );
    assert!(st.previous_policy_tier.is_none());
    assert_eq!(st.forced_action, ForcedAction::None);
    assert!(st.manual_action_required);
    assert!(has(&st, reason::BOOTSTRAP_OBSERVE_ONLY));
}

#[test]
fn test_bootstrap_adopt_candidate_policy_allows_reconciliation() {
    let c = RegimeConfig {
        bootstrap_policy: BOOTSTRAP_ADOPT_CANDIDATE.to_owned(),
        ..cfg()
    };
    let st = ev(
        &c,
        Ev {
            indices: Some(all_with(&c, st3(BELOW, BELOW, BELOW))),
            sentinel: Some(sent(BELOW, BELOW)),
            brd: Some(Some(bp(20.0))),
            exposure: Some(ExposureSnapshot::actual(95.0)),
            ..Ev::default()
        },
    );
    assert_ne!(st.forced_action, ForcedAction::None);
}

// =============================================================================================
// data quality: asymmetric effects
// =============================================================================================

fn stale_all(c: &RegimeConfig) -> SignalMap {
    c.structural_indices
        .iter()
        .map(|(_, n)| {
            (
                n.clone(),
                forced(n, &st3(ABOVE, ABOVE, ABOVE), 100.0, &[], true, true),
            )
        })
        .collect()
}

#[test]
fn test_stale_index_retains_tier_blocks_buys_and_forces_no_sell() {
    let c = cfg();
    let st = ev(
        &c,
        Ev {
            indices: Some(stale_all(&c)),
            exposure: Some(ExposureSnapshot::actual(95.0)),
            previous: Some(prev(RegimeTier::R1)),
            ..Ev::default()
        },
    );
    assert_eq!(st.policy_tier, RegimeTier::R1);
    assert_eq!(st.new_buys, NewBuyMode::Blocked);
    assert_eq!(st.forced_action, ForcedAction::None);
    assert!(st.data_stale);
    assert!(has(&st, reason::TRANSITION_HELD_STALE_DATA));
}

#[test]
fn test_missing_index_retains_tier_and_blocks_buys() {
    let c = cfg();
    let mut idx = healthy(&c);
    idx.retain(|(n, _)| n != "NIFTY SMLCAP 250");
    let st = ev(
        &c,
        Ev {
            indices: Some(idx),
            previous: Some(prev(RegimeTier::R2)),
            ..Ev::default()
        },
    );
    assert_eq!(st.policy_tier, RegimeTier::R2);
    assert_eq!(st.new_buys, NewBuyMode::Blocked);
    assert!(has(&st, reason::DATA_INDEX_MISSING));
}

#[test]
fn test_stale_breadth_blocks_buys_but_causes_no_forced_sell() {
    let c = cfg();
    let st = ev(
        &c,
        Ev {
            brd: Some(Some(breadth(70.0, d(2026, 1, 1), 100.0))),
            exposure: Some(ExposureSnapshot::actual(95.0)),
            previous: Some(prev(RegimeTier::R1)),
            ..Ev::default()
        },
    );
    assert_eq!(st.new_buys, NewBuyMode::Blocked);
    assert_eq!(st.forced_action, ForcedAction::None);
    assert!(has(&st, reason::BREADTH_STALE));
}

#[test]
fn test_genuine_risk_off_still_escalates_through_degraded_data() {
    let c = cfg();
    let mut idx = all_indices(
        &c,
        &[
            ("largecap", st3(BELOW, BELOW, BELOW)),
            ("smallcap", st3(BELOW, BELOW, BELOW)),
        ],
    );
    idx.push((
        "NIFTY MIDCAP 150".to_owned(),
        forced(
            "NIFTY MIDCAP 150",
            &st3(ABOVE, ABOVE, ABOVE),
            100.0,
            &[],
            true,
            true,
        ),
    ));
    let st = ev(
        &c,
        Ev {
            indices: Some(idx),
            brd: Some(Some(bp(35.0))),
            exposure: Some(ExposureSnapshot::actual(90.0)),
            previous: Some(prev(RegimeTier::R1)),
            ..Ev::default()
        },
    );
    assert_eq!(st.policy_tier, RegimeTier::R3);
    assert_eq!(st.forced_action, ForcedAction::ReduceToCap);
    assert!(st.data_stale);
}

#[test]
fn test_degraded_data_can_never_rerisk() {
    let c = cfg();
    let st = ev(
        &c,
        Ev {
            indices: Some(stale_all(&c)),
            brd: Some(Some(bp(95.0))),
            previous: Some(prev(RegimeTier::R3)),
            ..Ev::default()
        },
    );
    assert_eq!(st.policy_tier, RegimeTier::R3);
    assert_eq!(st.new_buys, NewBuyMode::Blocked);
    assert!(has(&st, reason::TRANSITION_HELD_STALE_DATA));
}

#[test]
fn test_r2_default_is_not_an_affirmative_risk_off_signal() {
    assert!(!AFFIRMATIVE_RISK_OFF.contains(&reason::R2_DEFAULT));
    assert!(AFFIRMATIVE_RISK_OFF.contains(&reason::R3_H200_WEAK));
    assert!(AFFIRMATIVE_RISK_OFF.contains(&reason::SENTINEL_FLOOR_R3));
}

#[test]
fn test_low_coverage_breadth_is_rejected() {
    let c = cfg();
    let st = ev(
        &c,
        Ev {
            brd: Some(Some(breadth(70.0, d(2026, 6, 1), 50.0))),
            previous: Some(prev(RegimeTier::R2)),
            ..Ev::default()
        },
    );
    assert!(has(&st, reason::BREADTH_LOW_COVERAGE));
    assert_eq!(st.new_buys, NewBuyMode::Blocked);
    assert_ne!(st.policy_tier, RegimeTier::R1);
}

#[test]
fn test_missing_breadth_is_flagged() {
    let c = cfg();
    let st = ev(
        &c,
        Ev {
            brd: Some(None),
            previous: Some(prev(RegimeTier::R2)),
            ..Ev::default()
        },
    );
    assert!(has(&st, reason::BREADTH_MISSING));
    assert_eq!(st.new_buys, NewBuyMode::Blocked);
}

#[test]
fn test_unknown_signal_cannot_permit_r1_or_new_buys() {
    let c = cfg();
    let idx = all_indices(
        &c,
        &[
            ("largecap", st3(ABOVE, ABOVE, ABOVE)),
            ("midcap", st3(ABOVE, ABOVE, ABOVE)),
            ("smallcap", st3(ABOVE, ABOVE, UNKNOWN)),
        ],
    );
    let st = ev(
        &c,
        Ev {
            indices: Some(idx),
            brd: Some(Some(bp(80.0))),
            previous: Some(prev(RegimeTier::R2)),
            ..Ev::default()
        },
    );
    assert_ne!(st.raw_candidate_tier, RegimeTier::R1);
    assert_ne!(st.policy_tier, RegimeTier::R1);
    assert_eq!(st.new_buys, NewBuyMode::Blocked);
    assert!(has(&st, reason::SIGNAL_UNKNOWN));
}

// =============================================================================================
// exposure: signal and execution are separate facts
// =============================================================================================

#[test]
fn test_committed_tier_is_not_evidence_the_cap_was_achieved() {
    let c = cfg();
    let st = ev(
        &c,
        Ev {
            sentinel: Some(sent(ABOVE, BELOW)),
            exposure: Some(ExposureSnapshot::actual(90.0)),
            previous: Some(prev(RegimeTier::R1)),
            ..Ev::default()
        },
    );
    assert_eq!(st.policy_tier, RegimeTier::R3);
    assert_eq!(st.target_equity_cap_pct, 40.0);
    assert_eq!(st.actual_equity_pct, 90.0);
    assert!(approx(st.exposure_gap_pct, 50.0));
    assert_eq!(st.forced_action, ForcedAction::ReduceToCap);
}

#[test]
fn test_partial_execution_preserves_the_gap_without_a_new_transition() {
    let c = cfg();
    let p = PreviousRegime {
        last_transition_date: Some(d(2026, 6, 5)),
        ..PreviousRegime::new(RegimeTier::R3, d(2026, 6, 5))
    };
    let st = ev(
        &c,
        Ev {
            sentinel: Some(sent(ABOVE, BELOW)),
            exposure: Some(ExposureSnapshot {
                actual_equity_pct: 65.0,
                pending_buy_pct: 0.0,
                pending_sell_pct: 25.0,
                execution_status: ExecutionStatus::Partial,
            }),
            previous: Some(p),
            week_end: d(2026, 6, 12),
            session: d(2026, 6, 12),
            ..Ev::default()
        },
    );
    assert_eq!(st.policy_tier, RegimeTier::R3);
    assert!(!st.transition_limited);
    assert_eq!(st.last_transition_date, Some(d(2026, 6, 5)));
    assert!(approx(st.exposure_gap_pct, 25.0));
    assert_eq!(st.forced_action, ForcedAction::ReduceToCap);
    assert_eq!(st.execution_status, ExecutionStatus::Partial);
    assert!(has(&st, reason::EXPOSURE_RECONCILE_RETRY));
}

#[test]
fn test_exposure_within_tolerance_needs_no_action() {
    let c = cfg();
    let st = ev(
        &c,
        Ev {
            sentinel: Some(sent(ABOVE, BELOW)),
            exposure: Some(ExposureSnapshot::actual(40.5)),
            previous: Some(prev(RegimeTier::R3)),
            ..Ev::default()
        },
    );
    assert_eq!(st.forced_action, ForcedAction::None);
    assert!(has(&st, reason::EXPOSURE_WITHIN_CAP));
}

#[test]
fn test_r4_is_not_a_full_exit_at_the_default_cap() {
    let c = cfg();
    assert_eq!(c.cap_for("R4"), Some(10.0));
    let st = ev(
        &c,
        Ev {
            indices: Some(all_with(&c, st3(BELOW, BELOW, BELOW))),
            sentinel: Some(sent(BELOW, BELOW)),
            brd: Some(Some(bp(20.0))),
            exposure: Some(ExposureSnapshot::actual(80.0)),
            previous: Some(prev(RegimeTier::R1)),
            ..Ev::default()
        },
    );
    assert_eq!(st.policy_tier, RegimeTier::R4);
    assert_eq!(st.target_equity_cap_pct, 10.0);
    assert_eq!(st.forced_action, ForcedAction::CrashReduce);
}

#[test]
fn test_r4_can_be_configured_to_full_cash() {
    let c = RegimeConfig {
        tier_exposure_pct: [100.0, 70.0, 40.0, 0.0],
        ..cfg()
    };
    c.validate().unwrap();
    assert_eq!(c.cap_for("R4"), Some(0.0));
}

// =============================================================================================
// new-buy modes
// =============================================================================================

#[test]
fn test_r1_permits_full_size_entries() {
    let c = cfg();
    let st = ev(
        &c,
        Ev {
            brd: Some(Some(bp(80.0))),
            previous: Some(prev(RegimeTier::R1)),
            ..Ev::default()
        },
    );
    assert_eq!(st.policy_tier, RegimeTier::R1);
    assert_eq!(st.new_buys, NewBuyMode::Full);
}

#[test]
fn test_r2_permits_half_size_entries() {
    let c = cfg();
    let idx = all_indices(
        &c,
        &[
            ("largecap", st3(ABOVE, ABOVE, ABOVE)),
            ("midcap", st3(ABOVE, BELOW, ABOVE)),
            ("smallcap", st3(ABOVE, ABOVE, ABOVE)),
        ],
    );
    let st = ev(
        &c,
        Ev {
            indices: Some(idx),
            brd: Some(Some(bp(50.0))),
            previous: Some(prev(RegimeTier::R2)),
            ..Ev::default()
        },
    );
    assert_eq!(st.policy_tier, RegimeTier::R2);
    assert_eq!(st.new_buys, NewBuyMode::Half);
}

#[test]
fn test_r3_and_r4_block_new_entries() {
    for tier in [RegimeTier::R3, RegimeTier::R4] {
        let s = sent(ABOVE, ABOVE);
        let (mode, codes) = resolve_new_buys(tier, Some(&s), true);
        assert_eq!(mode, NewBuyMode::Blocked);
        assert!(codes.contains(&reason::NEW_BUYS_BLOCKED_TIER));
    }
}

// =============================================================================================
// configuration validation
// =============================================================================================

fn err_of(c: &RegimeConfig) -> String {
    c.validate().err().map(|e| e.to_string()).unwrap_or_default()
}

#[test]
fn test_default_config_is_valid() {
    cfg().validate().unwrap();
}

#[test]
fn test_ma_lengths_must_be_strictly_increasing() {
    let a = RegimeConfig {
        ma_lengths: vec![50, 20, 200],
        ..cfg()
    };
    assert!(err_of(&a).contains("strictly increasing"));
    let b = RegimeConfig {
        ma_lengths: vec![20, 20, 200],
        ..cfg()
    };
    assert!(err_of(&b).contains("strictly increasing"));
}

#[test]
fn test_tier_caps_must_be_non_increasing() {
    let c = RegimeConfig {
        tier_exposure_pct: [100.0, 40.0, 70.0, 10.0],
        ..cfg()
    };
    assert!(err_of(&c).contains("non-increasing"));
}

#[test]
fn test_confirm_days_must_be_at_least_one() {
    let c = RegimeConfig {
        confirm_days: 0,
        ..cfg()
    };
    assert!(err_of(&c).contains("confirm_days"));
}

#[test]
fn test_buffer_bps_must_be_in_a_safe_range() {
    let c = RegimeConfig {
        buffer_bps: 5000,
        ..cfg()
    };
    assert!(err_of(&c).contains("buffer_bps"));
}

#[test]
fn test_thresholds_must_be_in_range() {
    let a = RegimeConfig {
        r1_breadth_min: 155.0,
        ..cfg()
    };
    assert!(err_of(&a).contains("percentage"));
    let b = RegimeConfig {
        r1_h200_min: 8.0,
        ..cfg()
    };
    assert!(err_of(&b).contains("weighted-health fraction"));
}

#[test]
fn test_weights_must_be_non_negative_and_normalizable() {
    let c = RegimeConfig {
        default_book_weights: vec![
            ("smallcap".to_owned(), -1.0),
            ("midcap".to_owned(), 0.5),
            ("largecap".to_owned(), 0.5),
        ],
        ..cfg()
    };
    let e = err_of(&c);
    assert!(e.contains("non-negative"), "{e}");
    assert!(e.contains("{'smallcap': -1.0, 'midcap': 0.5, 'largecap': 0.5}"), "{e}");
}

#[test]
fn test_index_identifiers_must_be_non_empty() {
    let c = RegimeConfig {
        momentum_sentinel: "  ".to_owned(),
        ..cfg()
    };
    assert!(err_of(&c).contains("non-empty"));
}

#[test]
fn test_validation_reports_every_problem_at_once() {
    let c = RegimeConfig {
        confirm_days: 0,
        buffer_bps: 9999,
        ..cfg()
    };
    let e = err_of(&c);
    assert!(e.contains("confirm_days") && e.contains("buffer_bps"));
}

#[test]
fn missing_weight_categories_and_an_empty_index_table_are_named() {
    let c = RegimeConfig {
        default_book_weights: vec![("smallcap".to_owned(), 1.0)],
        ..cfg()
    };
    assert!(err_of(&c).contains("default_book_weights missing categories: ['largecap', 'midcap']"));
    let z = RegimeConfig {
        default_book_weights: vec![("smallcap".to_owned(), 0.0)],
        structural_indices: Vec::new(),
        ..cfg()
    };
    let e = err_of(&z);
    assert!(e.contains("must be normalizable (sum > 0)"));
    assert!(e.contains("structural_indices must not be empty"));
}

// =============================================================================================
// reproducibility contract
// =============================================================================================

#[test]
fn test_state_persists_algorithm_version_and_config_hash() {
    let c = cfg();
    let st = ev(&c, Ev::default());
    assert_eq!(st.algorithm_version, crate::lanes::analytics::regime::ALGORITHM_VERSION);
    assert_eq!(st.config_hash.len(), 16);
    assert_eq!(st.config_hash, c.config_hash());
}

#[test]
fn test_config_hash_is_stable_across_instances() {
    assert_eq!(cfg().config_hash(), cfg().config_hash());
}

#[test]
fn test_config_hash_changes_when_a_threshold_changes() {
    let other = RegimeConfig {
        r1_breadth_min: 60.0,
        ..cfg()
    };
    assert_ne!(cfg().config_hash(), other.config_hash());
}

#[test]
fn test_config_hash_ignores_rollout_mode() {
    let a = RegimeConfig {
        mode: "observe".to_owned(),
        ..cfg()
    };
    let b = RegimeConfig {
        mode: "enforce".to_owned(),
        ..cfg()
    };
    assert_eq!(a.config_hash(), b.config_hash());
}

#[test]
fn test_every_reason_code_has_display_text() {
    let missing: Vec<&str> = reason::ALL
        .iter()
        .copied()
        .filter(|c| !DISPLAY_REASONS.iter().any(|(k, _)| k == c))
        .collect();
    assert!(missing.is_empty(), "reason codes without display text: {missing:?}");
    assert_eq!(reason::ALL.len(), DISPLAY_REASONS.len());
}

#[test]
fn test_state_reasons_align_with_codes() {
    let c = cfg();
    let st = ev(&c, Ev::default());
    assert_eq!(st.reasons.len(), st.reason_codes.len());
    assert_eq!(st.reasons, crate::lanes::analytics::regime::describe(&st.reason_codes));
}

#[test]
fn test_reason_codes_are_deduplicated_and_ordered() {
    let c = cfg();
    let st = ev(
        &c,
        Ev {
            sentinel: Some(sent(BELOW, BELOW)),
            ..Ev::default()
        },
    );
    let mut uniq = st.reason_codes.clone();
    uniq.sort();
    uniq.dedup();
    assert_eq!(uniq.len(), st.reason_codes.len());
}

#[test]
fn test_state_serialises_to_json_safe_primitives() {
    let c = cfg();
    let st = ev(&c, Ev::default());
    let blob = crate::wire::orjson_dumps(&PyValue::Map(st.as_dict())).unwrap();
    assert!(blob.contains("\"policy_tier\""));
}

#[test]
fn test_live_and_replay_paths_are_identical() {
    let c = cfg();
    let mk = || {
        ev(
            &c,
            Ev {
                indices: Some(healthy(&c)),
                sentinel: Some(sent(ABOVE, ABOVE)),
                brd: Some(Some(bp(80.0))),
                bk: Some(book(0.5, 0.3, 0.2)),
                exposure: Some(ExposureSnapshot::actual(60.0)),
                previous: Some(prev(RegimeTier::R1)),
                ..Ev::default()
            },
        )
    };
    assert_eq!(mk().as_dict(), mk().as_dict());
}

#[test]
fn test_build_index_signals_is_deterministic() {
    let c = cfg();
    let cs: Vec<f64> = (0..260).map(|i| 100.0 + f64::from(i % 7)).collect();
    let a = signals_for("NIFTY 50", &cs, &c);
    let b = signals_for("NIFTY 50", &cs, &c);
    assert_eq!(a.as_dict(), b.as_dict());
}

// =============================================================================================
// guardrails: this module must stay pure
// =============================================================================================

const ENGINE_SRC: &str = include_str!("engine.rs");
const ALLOC_SRC: &str = include_str!("alloc.rs");

fn code_only(src: &str) -> String {
    src.lines()
        .filter(|l| {
            let t = l.trim_start();
            !t.starts_with("//")
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn test_regime_module_imports_nothing_from_the_order_path() {
    let src = code_only(ENGINE_SRC);
    for forbidden in [
        "place_order",
        "place_gtt",
        "Gateway",
        "KiteWrites",
        "crate::storage",
        "reqwest",
        "std::fs",
        "std::net",
    ] {
        assert!(!src.contains(forbidden), "engine.rs must stay pure — found {forbidden:?}");
    }
}

#[test]
fn test_regime_module_does_not_read_the_clock() {
    for src in [code_only(ENGINE_SRC), code_only(ALLOC_SRC)] {
        assert!(!src.contains("today_ist") && !src.contains("Local::now"));
        assert!(!src.contains("SystemTime") && !src.contains("Utc::now"));
        assert!(!src.contains("crate::clock"));
    }
}

// --- tests/test_exposure_moved_to_core.py ------------------------------------------------------

/// `test_the_exposure_modules_touch_nothing[regime|allocation]`: no I/O imports in either.
#[test]
fn test_the_exposure_modules_touch_nothing() {
    for (name, src) in [("engine", ENGINE_SRC), ("alloc", ALLOC_SRC)] {
        let src = code_only(src);
        for forbidden in [
            "std::fs",
            "std::net",
            "std::path",
            "crate::storage",
            "reqwest",
            "tokio",
            "crate::kite",
            "sqlx",
        ] {
            assert!(!src.contains(forbidden), "{name} imports {forbidden}");
        }
    }
}

/// `test_the_desk_still_reaches_them_by_their_old_names`: `regime.RegimeTier.R1.value == "R1"`
/// and the allocation module carries `Allocation`.
#[test]
fn test_the_desk_still_reaches_them_by_their_old_names() {
    assert_eq!(RegimeTier::R1.value(), "R1");
    let a = super::super::alloc::Allocation::empty(40.0);
    assert_eq!(a.n_positions(), 0);
}

/// `test_the_algorithm_version_still_matches_every_stored_evaluation` and
/// `test_the_config_fingerprint_reproduces_against_every_stored_evaluation`: Python reads
/// `data/portfolio.db` (skipping when absent). This port never opens that file (it is
/// unrebuildable evidence); it reads the stored evaluations of the test bed when
/// `BASKFY_RS_REGIME_TEST_PG` names a database, and is skipped (ignored) otherwise.
#[tokio::test]
#[ignore = "needs BASKFY_RS_REGIME_TEST_PG (a bed clone); run explicitly"]
async fn test_the_stored_evaluations_reproduce_version_and_fingerprint() {
    let Ok(url) = std::env::var("BASKFY_RS_REGIME_TEST_PG") else {
        return;
    };
    let db = crate::storage::Db::postgres(&url, "desk").await.unwrap();
    let mut conn = db.connect().await.unwrap();
    let rows = conn
        .execute(
            "SELECT DISTINCT algorithm_version, config_hash FROM regime_evaluations",
            &[],
        )
        .await
        .unwrap()
        .rows;
    let c = cfg();
    for r in rows {
        let v = r
            .get("algorithm_version")
            .and_then(crate::storage::DbValue::as_str)
            .unwrap_or_default()
            .to_owned();
        let h = r
            .get("config_hash")
            .and_then(crate::storage::DbValue::as_str)
            .unwrap_or_default()
            .to_owned();
        assert_eq!(c.algorithm_version, v);
        assert_eq!(c.config_hash(), h);
    }
}
