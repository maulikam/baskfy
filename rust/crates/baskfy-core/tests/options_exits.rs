//! Ports `decile-blueprint/packages/core/tests/test_options_exits.py` (OP9): `exits.evaluate`,
//! one open position's exit per tick — the mark each rule is handed (longs at the bid, shorts at
//! the ask), the marked loss, staleness, the missing-mark case and feed loss.
//!
//! Python builds its times IST-aware; here they are IST wall-clock (`bars::ist` is the caller's).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::str::FromStr;

use baskfy_core::options::bars::Bar;
use baskfy_core::options::config::OptionsConfig;
use baskfy_core::options::execution::LegRole;
use baskfy_core::options::exits::{
    FEED_LOST, IndexState, LegMark, Marks, OpenLeg, OpenPosition, RULE, evaluate,
    latest_bar_after, latest_five_minute_close, mark, marked_loss_inr,
};
use baskfy_core::options::gating::Sleeve;
use baskfy_core::options::structures::{Direction, Structure};
use chrono::{Duration, NaiveDate, NaiveDateTime, NaiveTime};
use rust_decimal::Decimal;

const QTY: i64 = 65;

fn cfg() -> OptionsConfig {
    OptionsConfig::default()
}

fn d(s: &str) -> Decimal {
    Decimal::from_str(s).unwrap()
}

fn at3(hh: u32, mm: u32, ss: u32) -> NaiveDateTime {
    NaiveDate::from_ymd_opt(2026, 10, 27)
        .unwrap()
        .and_time(NaiveTime::from_hms_opt(hh, mm, ss).unwrap())
}

fn at(hh: u32, mm: u32) -> NaiveDateTime {
    at3(hh, mm, 0)
}

fn q(bid: &str, ask: &str, when: NaiveDateTime) -> LegMark {
    LegMark {
        bid: Some(d(bid)),
        ask: Some(d(ask)),
        at: when,
    }
}

fn bar_with(ts: NaiveDateTime, close: &str, high: Option<&str>, low: Option<&str>) -> Bar {
    let c = d(close);
    Bar {
        ts,
        open: c,
        high: high.map_or(c + Decimal::from(2), d),
        low: low.map_or(c - Decimal::from(2), d),
        close: c,
    }
}

fn flat_bars(start: (u32, u32), end: (u32, u32), close: &str) -> Vec<Bar> {
    let mut out = Vec::new();
    let mut t = at(start.0, start.1);
    while t <= at(end.0, end.1) {
        out.push(bar_with(t, close, None, None));
        t += Duration::minutes(1);
    }
    out
}

fn leg(role: LegRole, strike: &str, fill: &str, token: i64) -> OpenLeg {
    OpenLeg {
        role,
        strike: d(strike),
        quantity: QTY,
        fill_price: d(fill),
        instrument_token: token,
    }
}

fn condor(credit: &str, opened: Option<NaiveDateTime>) -> OpenPosition {
    OpenPosition {
        sleeve: Sleeve::O1m,
        structure: Structure::IronCondor,
        legs: vec![
            leg(LegRole::LongPut, "24700", "5.00", 1),
            leg(LegRole::LongCall, "25300", "5.00", 2),
            leg(LegRole::ShortPut, "24850", "25.00", 3),
            leg(LegRole::ShortCall, "25150", "25.00", 4),
        ],
        entry_points: d(credit),
        opened_at: opened.unwrap_or_else(|| at(10, 5)),
        risk_budget_inr: d("50000"),
        direction: None,
        range_high: None,
        range_low: None,
        half_gap: None,
        width_points: None,
    }
}

fn condor_marks(short: &str, wing: &str, when: NaiveDateTime) -> Marks {
    let (s, w, tick) = (d(short), d(wing), d("0.10"));
    let lm = |x: Decimal| LegMark {
        bid: Some(x),
        ask: Some(x + tick),
        at: when,
    };
    Marks::from([
        (LegRole::ShortCall, lm(s)),
        (LegRole::ShortPut, lm(s)),
        (LegRole::LongCall, lm(w)),
        (LegRole::LongPut, lm(w)),
    ])
}

fn long_call(entry: &str, opened: Option<NaiveDateTime>) -> OpenPosition {
    OpenPosition {
        sleeve: Sleeve::O2,
        structure: Structure::LongOption,
        legs: vec![leg(LegRole::LongCall, "25000", entry, 9)],
        entry_points: d(entry),
        opened_at: opened.unwrap_or_else(|| at(10, 6)),
        risk_budget_inr: d("50000"),
        direction: Some(Direction::Up),
        range_high: Some(d("25032")),
        range_low: Some(d("25008")),
        half_gap: None,
        width_points: None,
    }
}

fn spread(sleeve: Sleeve, debit: &str) -> OpenPosition {
    OpenPosition {
        sleeve,
        structure: Structure::DebitSpread,
        legs: vec![
            leg(LegRole::LongCall, "25200", "35.00", 11),
            leg(LegRole::ShortCall, "25300", "5.00", 12),
        ],
        entry_points: d(debit),
        opened_at: at(10, 22),
        risk_budget_inr: d("50000"),
        direction: Some(Direction::Up),
        range_high: Some(d("25182")),
        range_low: Some(d("25003")),
        half_gap: Some(d("25100")),
        width_points: Some(d("100")),
    }
}

fn spread_marks(long: &str, short: &str, when: NaiveDateTime) -> Marks {
    let tick = d("0.10");
    Marks::from([
        (
            LegRole::LongCall,
            LegMark {
                bid: Some(d(long)),
                ask: Some(d(long) + tick),
                at: when,
            },
        ),
        (
            LegRole::ShortCall,
            LegMark {
                bid: Some(d(short)),
                ask: Some(d(short) + tick),
                at: when,
            },
        ),
    ])
}

fn live(spot: &str, when: NaiveDateTime, bars: Vec<Bar>) -> IndexState {
    IndexState {
        spot: Some(d(spot)),
        last_tick_at: Some(when),
        bars,
    }
}

fn ev(
    p: &OpenPosition,
    m: &Marks,
    i: &IndexState,
    now: NaiveDateTime,
) -> Option<baskfy_core::options::exits::ExitVerdict> {
    evaluate(p, m, i, now, &cfg(), false).unwrap()
}

// --- the mark and the loss --------------------------------------------------------------------

#[test]
fn a_condor_is_marked_at_the_cost_to_close() {
    let now = at(11, 0);
    let m = mark(&condor("40.00", None), &condor_marks("10.00", "2.00", now), now, &cfg()).unwrap();
    assert_eq!(m.value_points, Some(d("16.20")));
    assert!(!m.stale);
}

#[test]
fn a_spread_is_marked_at_what_closing_receives() {
    let now = at(11, 0);
    let m = mark(
        &spread(Sleeve::O3a, "30.00"),
        &spread_marks("50.00", "10.00", now),
        now,
        &cfg(),
    )
    .unwrap();
    assert_eq!(m.value_points, Some(d("39.90")));
    assert!(!m.stale);
}

#[test]
fn an_old_quote_makes_the_mark_stale() {
    let now = at(11, 0);
    let old = now - Duration::seconds(cfg().chain.stale_quote_seconds + 1);
    let mut marks = spread_marks("50.00", "10.00", now);
    marks.insert(LegRole::ShortCall, q("10.00", "10.10", old));
    assert!(mark(&spread(Sleeve::O3a, "30.00"), &marks, now, &cfg()).unwrap().stale);
}

#[test]
fn a_one_sided_leg_has_no_mark() {
    let now = at(11, 0);
    let mut marks = spread_marks("50.00", "10.00", now);
    marks.insert(
        LegRole::ShortCall,
        LegMark {
            bid: Some(d("10.00")),
            ask: None,
            at: now,
        },
    );
    let m = mark(&spread(Sleeve::O3a, "30.00"), &marks, now, &cfg()).unwrap();
    assert!(m.value_points.is_none() && m.stale);
}

#[test]
fn the_marked_loss_is_signed_by_structure() {
    assert_eq!(
        marked_loss_inr(&condor("40.00", None), d("50.00")).unwrap(),
        d("650.00")
    );
    assert_eq!(
        marked_loss_inr(&spread(Sleeve::O3a, "30.00"), d("20.00")).unwrap(),
        d("650.00")
    );
    assert_eq!(
        marked_loss_inr(&long_call("200.00", None), d("210.00")).unwrap(),
        d("-650.00")
    );
}

// --- each structure's rules, through one function ---------------------------------------------

#[test]
fn condor_profit_at_half_the_credit() {
    let now = at(11, 0);
    let v = ev(
        &condor("40.00", None),
        &condor_marks("10.00", "2.00", now),
        &live("25000", now, vec![]),
        now,
    )
    .unwrap();
    assert_eq!((v.code.as_str(), v.reason.as_str()), ("PROFIT", RULE));
    assert_eq!(v.value_points, Some(d("16.20")));
}

#[test]
fn condor_a_stale_mark_drops_the_profit_but_not_the_strike_touch() {
    let now = at(11, 0);
    let old = now - Duration::seconds(60);
    let marks = condor_marks("10.00", "2.00", old);
    assert!(ev(&condor("40.00", None), &marks, &live("25000", now, vec![]), now).is_none());
    let touched = ev(&condor("40.00", None), &marks, &live("25150", now, vec![]), now).unwrap();
    assert_eq!(touched.code, "STRIKE_TOUCH");
}

#[test]
fn condor_the_hard_exit_outranks_everything() {
    let now = at(14, 30);
    let v = ev(
        &condor("40.00", None),
        &condor_marks("10.00", "2.00", now),
        &live("25150", now, vec![]),
        now,
    )
    .unwrap();
    assert_eq!(v.code, "HARD_EXIT");
}

#[test]
fn condor_holding_between_the_rules() {
    let now = at(11, 0);
    let marks = condor_marks("20.00", "1.00", now);
    assert!(ev(&condor("40.00", None), &marks, &live("25000", now, vec![]), now).is_none());
}

#[test]
fn condor_an_unknown_index_holds_the_touch_rule_only() {
    let now = at(11, 0);
    let blind = IndexState::default();
    assert!(ev(&condor("40.00", None), &condor_marks("20.00", "1.00", now), &blind, now).is_none());
    let stop = ev(&condor("40.00", None), &condor_marks("35.00", "1.00", now), &blind, now).unwrap();
    assert_eq!(stop.code, "STOP");
    let late = at(14, 30);
    let hard =
        ev(&condor("40.00", None), &condor_marks("20.00", "1.00", late), &blind, late).unwrap();
    assert_eq!(hard.code, "HARD_EXIT");
}

#[test]
fn long_the_time_stop_after_45_minutes_without_the_gain() {
    let opened = at(10, 6);
    let now = opened + Duration::minutes(cfg().directional.time_stop_minutes);
    let bars = flat_bars((10, 0), (10, 50), "25060");
    let marks = Marks::from([(LegRole::LongCall, q("201.00", "201.50", now))]);
    let v = ev(
        &long_call("200.00", Some(opened)),
        &marks,
        &live("25060", now, bars),
        now,
    )
    .unwrap();
    assert_eq!(v.code, "TIME_STOP");
}

#[test]
fn long_before_45_minutes_it_holds() {
    let opened = at(10, 6);
    let now = opened + Duration::minutes(cfg().directional.time_stop_minutes - 1);
    let bars = flat_bars((10, 0), (10, 50), "25060");
    let marks = Marks::from([(LegRole::LongCall, q("201.00", "201.50", now))]);
    assert!(
        ev(
            &long_call("200.00", Some(opened)),
            &marks,
            &live("25060", now, bars),
            now
        )
        .is_none()
    );
}

#[test]
fn long_a_five_minute_close_back_inside_the_range_invalidates() {
    let now = at(10, 21);
    let bars = flat_bars((10, 5), (10, 19), "25020");
    let marks = Marks::from([(LegRole::LongCall, q("199.00", "199.50", now))]);
    let v = ev(&long_call("200.00", None), &marks, &live("25020", now, bars), now).unwrap();
    assert_eq!(v.code, "INVALIDATED");
}

#[test]
fn long_target_on_the_bid() {
    let now = at(10, 30);
    let target = d("200.00") * (Decimal::ONE + cfg().directional.target_frac);
    let marks = Marks::from([(
        LegRole::LongCall,
        LegMark {
            bid: Some(target),
            ask: Some(target + Decimal::ONE),
            at: now,
        },
    )]);
    let v = ev(
        &long_call("200.00", None),
        &marks,
        &live("25100", now, flat_bars((10, 5), (10, 29), "25100")),
        now,
    )
    .unwrap();
    assert_eq!(v.code, "TARGET");
}

#[test]
fn spread_o3_target_at_80_percent_of_the_width() {
    let now = at(11, 0);
    let bars = flat_bars((10, 20), (10, 59), "25300");
    let v = ev(
        &spread(Sleeve::O3a, "30.00"),
        &spread_marks("85.00", "4.90", now),
        &live("25300", now, bars),
        now,
    )
    .unwrap();
    assert_eq!(v.code, "TARGET");
}

#[test]
fn spread_o3_stop_at_half_the_debit() {
    let now = at(11, 0);
    let bars = flat_bars((10, 20), (10, 59), "25250");
    let v = ev(
        &spread(Sleeve::O3a, "30.00"),
        &spread_marks("20.00", "5.90", now),
        &live("25250", now, bars),
        now,
    )
    .unwrap();
    assert_eq!(v.code, "STOP");
}

#[test]
fn spread_o3a_invalidated_by_a_close_back_inside_the_morning_range() {
    let now = at(10, 31);
    let bars = flat_bars((10, 20), (10, 29), "25150");
    let v = ev(
        &spread(Sleeve::O3a, "30.00"),
        &spread_marks("35.00", "5.00", now),
        &live("25150", now, bars),
        now,
    )
    .unwrap();
    assert_eq!(v.code, "INVALIDATED");
}

#[test]
fn spread_o3b_invalidated_by_any_bar_through_half_gap() {
    let now = at(10, 31);
    let bars = vec![bar_with(at(10, 29), "25120", None, Some("25099"))];
    let v = ev(
        &spread(Sleeve::O3b, "30.00"),
        &spread_marks("35.00", "5.00", now),
        &live("25120", now, bars),
        now,
    )
    .unwrap();
    assert_eq!(v.code, "INVALIDATED");
}

// --- no mark, the budget, the feed ------------------------------------------------------------

#[test]
fn with_no_mark_only_the_clock_can_close() {
    let (early, late) = (at(11, 0), at(14, 45));
    let empty = Marks::new();
    assert!(ev(&spread(Sleeve::O3a, "30.00"), &empty, &live("25000", early, vec![]), early).is_none());
    let v = ev(&spread(Sleeve::O3a, "30.00"), &empty, &live("25000", late, vec![]), late).unwrap();
    assert!(v.code == "HARD_EXIT" && v.value_points.is_none());
}

#[test]
fn the_budget_breach_closes_as_stop() {
    let now = at(11, 0);
    let mut small = spread(Sleeve::O3a, "30.00");
    small.risk_budget_inr = d("100");
    small.half_gap = None;
    let bars = flat_bars((10, 20), (10, 59), "25250");
    let v = ev(&small, &spread_marks("33.00", "5.90", now), &live("25250", now, bars), now).unwrap();
    assert_eq!(v.code, "STOP");
    assert_eq!(v.marked_loss_inr, Some(d("195.00")));
}

#[test]
fn o1_loses_the_feed_only_after_14_00() {
    for (now, lost) in [(at3(13, 59, 0), false), (at3(14, 5, 0), true)] {
        let silent_since = now - Duration::seconds(cfg().chain.stale_index_seconds + 5);
        let index = IndexState {
            spot: Some(d("25000")),
            last_tick_at: Some(silent_since),
            bars: vec![],
        };
        let v = ev(&condor("40.00", None), &condor_marks("20.00", "1.00", now), &index, now);
        if lost {
            let v = v.unwrap();
            assert_eq!((v.code.as_str(), v.reason.as_str()), ("HARD_EXIT", FEED_LOST));
        } else {
            assert!(v.is_none());
        }
    }
}

#[test]
fn o2_loses_the_feed_at_any_time_after_the_grace() {
    let opened = at(10, 6);
    let now = opened + Duration::seconds(cfg().execution.feed_grace_seconds + 30);
    let index = IndexState {
        spot: Some(d("25060")),
        last_tick_at: Some(now - Duration::minutes(2)),
        bars: vec![],
    };
    let marks = Marks::from([(LegRole::LongCall, q("205.00", "205.50", now))]);
    let v = ev(&long_call("200.00", Some(opened)), &marks, &index, now).unwrap();
    assert_eq!((v.code.as_str(), v.reason.as_str()), ("HARD_EXIT", FEED_LOST));
}

#[test]
fn the_five_minute_close_is_a_completed_bar_after_entry() {
    let bars = flat_bars((10, 0), (10, 13), "25050");
    let open = NaiveTime::from_hms_opt(9, 15, 0).unwrap();
    assert_eq!(
        latest_five_minute_close(&bars, at3(10, 14, 30), at(10, 6), open, 5).unwrap(),
        Some(d("25050"))
    );
    assert_eq!(
        latest_five_minute_close(&bars, at3(10, 14, 30), at(10, 10), open, 5).unwrap(),
        None
    );
}

#[test]
fn the_bar_after_entry() {
    let bars = flat_bars((10, 0), (10, 5), "25050");
    let found = latest_bar_after(&bars, at(10, 6), at(10, 3)).unwrap();
    assert_eq!(found.ts, at(10, 5));
    assert!(latest_bar_after(&bars, at(10, 6), at(10, 7)).is_none());
}

#[test]
fn a_condor_outside_o1_is_refused_as_python_does() {
    // Python's `ValueError(f"{sleeve} does not trade a condor")` — the composition's guard.
    let now = at(11, 0);
    let mut wrong = condor("40.00", None);
    wrong.sleeve = Sleeve::O2;
    let err = evaluate(
        &wrong,
        &condor_marks("10.00", "2.00", now),
        &live("25000", now, vec![]),
        now,
        &cfg(),
        false,
    )
    .unwrap_err();
    assert!(err.to_string().contains("O2 does not trade a condor"));
}
