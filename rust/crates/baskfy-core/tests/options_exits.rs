//! Port of `tests/test_options_exits.py` (OP9, `exits.evaluate`).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime, TimeDelta, Timelike};
use rust_decimal::Decimal;

use baskfy_core::options::bars::Bar;
use baskfy_core::options::config::{OptionsConfig, Sleeve};
use baskfy_core::options::execution::LegRole;
use baskfy_core::options::exits::{
    ExitVerdict, FEED_LOST, IndexState, LegMark, Mark, OpenLeg, OpenPosition, RULE, evaluate,
    latest_bar_after, latest_five_minute_close, mark, marked_loss_inr,
};
use baskfy_core::options::num::at_ist;
use baskfy_core::options::structures::{Direction, Structure};

const QTY: i64 = 65;

fn cfg() -> OptionsConfig {
    OptionsConfig::default()
}

fn day() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 10, 27).unwrap()
}

fn dec(s: &str) -> Decimal {
    s.parse().unwrap()
}

fn at(hh: u32, mm: u32) -> DateTime<FixedOffset> {
    at_s(hh, mm, 0)
}

fn at_s(hh: u32, mm: u32, ss: u32) -> DateTime<FixedOffset> {
    at_ist(day(), NaiveTime::from_hms_opt(hh, mm, ss).unwrap())
}

fn q(bid: &str, ask: &str, when: DateTime<FixedOffset>) -> LegMark {
    LegMark {
        bid: Some(dec(bid)),
        ask: Some(dec(ask)),
        at: when,
    }
}

fn bar_hl(hh: u32, mm: u32, close: &str, high: Option<&str>, low: Option<&str>) -> Bar {
    let c = dec(close);
    Bar {
        ts: at(hh, mm),
        open: c,
        high: high.map_or(c + Decimal::TWO, dec),
        low: low.map_or(c - Decimal::TWO, dec),
        close: c,
    }
}

fn flat_bars(start: (u32, u32), end: (u32, u32), close: &str) -> Vec<Bar> {
    let mut out = Vec::new();
    let mut t = at(start.0, start.1);
    while t <= at(end.0, end.1) {
        out.push(bar_hl(t.hour(), t.minute(), close, None, None));
        t += TimeDelta::minutes(1);
    }
    out
}

fn leg(role: LegRole, strike: &str, fill: &str, token: i64) -> OpenLeg {
    OpenLeg {
        role,
        strike: dec(strike),
        quantity: QTY,
        fill_price: dec(fill),
        instrument_token: token,
    }
}

fn condor_at(credit: &str, opened: DateTime<FixedOffset>) -> OpenPosition {
    OpenPosition {
        sleeve: Sleeve::O1m,
        structure: Structure::IronCondor,
        legs: vec![
            leg(LegRole::LongPut, "24700", "5.00", 1),
            leg(LegRole::LongCall, "25300", "5.00", 2),
            leg(LegRole::ShortPut, "24850", "25.00", 3),
            leg(LegRole::ShortCall, "25150", "25.00", 4),
        ],
        entry_points: dec(credit),
        opened_at: opened,
        risk_budget_inr: dec("50000"),
        direction: None,
        range_high: None,
        range_low: None,
        half_gap: None,
        width_points: None,
    }
}

fn condor(credit: &str) -> OpenPosition {
    condor_at(credit, at(10, 5))
}

/// Both shorts quoted `short` (bid) / +0.10 (ask), both wings `wing` / +0.10.
fn condor_marks(
    short: &str,
    wing: &str,
    when: DateTime<FixedOffset>,
) -> BTreeMap<LegRole, LegMark> {
    let (s, w, t) = (dec(short), dec(wing), dec("0.10"));
    let m = |p: Decimal| LegMark {
        bid: Some(p),
        ask: Some(p + t),
        at: when,
    };
    BTreeMap::from([
        (LegRole::ShortCall, m(s)),
        (LegRole::ShortPut, m(s)),
        (LegRole::LongCall, m(w)),
        (LegRole::LongPut, m(w)),
    ])
}

fn long_call(entry: &str, opened: DateTime<FixedOffset>) -> OpenPosition {
    OpenPosition {
        sleeve: Sleeve::O2,
        structure: Structure::LongOption,
        legs: vec![leg(LegRole::LongCall, "25000", entry, 9)],
        entry_points: dec(entry),
        opened_at: opened,
        risk_budget_inr: dec("50000"),
        direction: Some(Direction::Up),
        range_high: Some(dec("25032")),
        range_low: Some(dec("25008")),
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
        entry_points: dec(debit),
        opened_at: at(10, 22),
        risk_budget_inr: dec("50000"),
        direction: Some(Direction::Up),
        range_high: Some(dec("25182")),
        range_low: Some(dec("25003")),
        half_gap: Some(dec("25100")),
        width_points: Some(dec("100")),
    }
}

fn spread_o3a() -> OpenPosition {
    spread(Sleeve::O3a, "30.00")
}

fn spread_marks(
    long: &str,
    short: &str,
    when: DateTime<FixedOffset>,
) -> BTreeMap<LegRole, LegMark> {
    let t = dec("0.10");
    BTreeMap::from([
        (
            LegRole::LongCall,
            LegMark {
                bid: Some(dec(long)),
                ask: Some(dec(long) + t),
                at: when,
            },
        ),
        (
            LegRole::ShortCall,
            LegMark {
                bid: Some(dec(short)),
                ask: Some(dec(short) + t),
                at: when,
            },
        ),
    ])
}

fn live(spot: &str, when: DateTime<FixedOffset>, bars: Vec<Bar>) -> IndexState {
    IndexState {
        spot: Some(dec(spot)),
        last_tick_at: Some(when),
        bars,
    }
}

fn eval(
    position: &OpenPosition,
    marks: &BTreeMap<LegRole, LegMark>,
    index: &IndexState,
    now: DateTime<FixedOffset>,
) -> Option<ExitVerdict> {
    evaluate(position, marks, index, &now, &cfg(), false).unwrap()
}

fn code(v: Option<&ExitVerdict>) -> Option<&str> {
    v.map(|x| x.code.as_str())
}

// --- the mark and the loss ---

#[test]
fn a_condor_is_marked_at_the_cost_to_close() {
    // Shorts bought back at the ask, wings sold at the bid: 2 x 10.10 - 2 x 2.00 = 16.20.
    let now = at(11, 0);
    let m = mark(
        &condor("40.00"),
        &condor_marks("10.00", "2.00", now),
        &now,
        &cfg(),
    )
    .unwrap();
    assert_eq!(
        m,
        Mark {
            value_points: Some(dec("16.20")),
            stale: false
        }
    );
}

#[test]
fn a_spread_is_marked_at_what_closing_receives() {
    let now = at(11, 0);
    let m = mark(
        &spread_o3a(),
        &spread_marks("50.00", "10.00", now),
        &now,
        &cfg(),
    )
    .unwrap();
    assert!(m.value_points == Some(dec("39.90")) && !m.stale);
}

#[test]
fn an_old_quote_makes_the_mark_stale() {
    let now = at(11, 0);
    let old = now - TimeDelta::seconds(cfg().chain.stale_quote_seconds + 1);
    let mut marks = spread_marks("50.00", "10.00", now);
    marks.insert(LegRole::ShortCall, q("10.00", "10.10", old));
    assert!(mark(&spread_o3a(), &marks, &now, &cfg()).unwrap().stale);
}

#[test]
fn a_one_sided_leg_has_no_mark() {
    let now = at(11, 0);
    let mut marks = spread_marks("50.00", "10.00", now);
    marks.insert(
        LegRole::ShortCall,
        LegMark {
            bid: Some(dec("10.00")),
            ask: None,
            at: now,
        },
    );
    let m = mark(&spread_o3a(), &marks, &now, &cfg()).unwrap();
    assert!(m.value_points.is_none() && m.stale);
}

#[test]
fn the_marked_loss_is_signed_by_structure() {
    assert_eq!(
        marked_loss_inr(&condor("40.00"), dec("50.00")).unwrap(),
        dec("650.00")
    );
    assert_eq!(
        marked_loss_inr(&spread(Sleeve::O3a, "30.00"), dec("20.00")).unwrap(),
        dec("650.00")
    );
    assert_eq!(
        marked_loss_inr(&long_call("200.00", at(10, 6)), dec("210.00")).unwrap(),
        dec("-650.00")
    );
}

// --- the condor ---

#[test]
fn condor_profit_at_half_the_credit() {
    let now = at(11, 0);
    let v = eval(
        &condor("40.00"),
        &condor_marks("10.00", "2.00", now),
        &live("25000", now, vec![]),
        now,
    )
    .unwrap();
    assert_eq!((v.code.as_str(), v.reason.as_str()), ("PROFIT", RULE));
    assert_eq!(v.value_points, Some(dec("16.20")));
}

#[test]
fn a_stale_mark_drops_the_profit_but_not_the_strike_touch() {
    let now = at(11, 0);
    let old = now - TimeDelta::seconds(60);
    let marks = condor_marks("10.00", "2.00", old);
    assert!(eval(&condor("40.00"), &marks, &live("25000", now, vec![]), now).is_none());
    let touched = eval(&condor("40.00"), &marks, &live("25150", now, vec![]), now);
    assert_eq!(code(touched.as_ref()), Some("STRIKE_TOUCH"));
}

#[test]
fn the_hard_exit_outranks_everything() {
    let now = at(14, 30);
    let v = eval(
        &condor("40.00"),
        &condor_marks("10.00", "2.00", now),
        &live("25150", now, vec![]),
        now,
    );
    assert_eq!(code(v.as_ref()), Some("HARD_EXIT"));
}

#[test]
fn condor_holding_between_the_rules() {
    let now = at(11, 0);
    let marks = condor_marks("20.00", "1.00", now); // D = 38.20
    assert!(eval(&condor("40.00"), &marks, &live("25000", now, vec![]), now).is_none());
}

#[test]
fn an_unknown_index_holds_the_touch_rule_only() {
    let now = at(11, 0);
    let blind = IndexState {
        spot: None,
        last_tick_at: None,
        bars: vec![],
    };
    assert!(
        eval(
            &condor("40.00"),
            &condor_marks("20.00", "1.00", now),
            &blind,
            now
        )
        .is_none()
    );
    let stop = eval(
        &condor("40.00"),
        &condor_marks("35.00", "1.00", now),
        &blind,
        now,
    );
    assert_eq!(code(stop.as_ref()), Some("STOP")); // D = 68.20 >= 1.5 x 40
    let late = at(14, 30);
    let hard = eval(
        &condor("40.00"),
        &condor_marks("20.00", "1.00", late),
        &blind,
        late,
    );
    assert_eq!(code(hard.as_ref()), Some("HARD_EXIT"));
}

// --- the long ---

#[test]
fn the_time_stop_after_45_minutes_without_the_gain() {
    let opened = at(10, 6);
    let now = opened + TimeDelta::minutes(cfg().directional.time_stop_minutes);
    let bars = flat_bars((10, 0), (10, 50), "25060");
    let marks = BTreeMap::from([(LegRole::LongCall, q("201.00", "201.50", now))]);
    let v = eval(
        &long_call("200.00", opened),
        &marks,
        &live("25060", now, bars),
        now,
    );
    assert_eq!(code(v.as_ref()), Some("TIME_STOP"));
}

#[test]
fn before_45_minutes_it_holds() {
    let opened = at(10, 6);
    let now = opened + TimeDelta::minutes(cfg().directional.time_stop_minutes - 1);
    let bars = flat_bars((10, 0), (10, 50), "25060");
    let marks = BTreeMap::from([(LegRole::LongCall, q("201.00", "201.50", now))]);
    assert!(
        eval(
            &long_call("200.00", opened),
            &marks,
            &live("25060", now, bars),
            now
        )
        .is_none()
    );
}

#[test]
fn a_five_minute_close_back_inside_the_range_invalidates() {
    let now = at(10, 21);
    let bars = flat_bars((10, 5), (10, 19), "25020");
    let marks = BTreeMap::from([(LegRole::LongCall, q("199.00", "199.50", now))]);
    let v = eval(
        &long_call("200.00", at(10, 6)),
        &marks,
        &live("25020", now, bars),
        now,
    );
    assert_eq!(code(v.as_ref()), Some("INVALIDATED"));
}

#[test]
fn target_on_the_bid() {
    let now = at(10, 30);
    let target = dec("200.00") * (Decimal::ONE + cfg().directional.target_frac);
    let marks = BTreeMap::from([(
        LegRole::LongCall,
        LegMark {
            bid: Some(target),
            ask: Some(target + Decimal::ONE),
            at: now,
        },
    )]);
    let v = eval(
        &long_call("200.00", at(10, 6)),
        &marks,
        &live("25100", now, flat_bars((10, 5), (10, 29), "25100")),
        now,
    );
    assert_eq!(code(v.as_ref()), Some("TARGET"));
}

// --- the spread ---

#[test]
fn o3_target_at_80_percent_of_the_width() {
    let now = at(11, 0);
    let bars = flat_bars((10, 20), (10, 59), "25300");
    let v = eval(
        &spread_o3a(),
        &spread_marks("85.00", "4.90", now),
        &live("25300", now, bars),
        now,
    );
    assert_eq!(code(v.as_ref()), Some("TARGET")); // V = 85.00 - 5.00 = 80.00
}

#[test]
fn o3_stop_at_half_the_debit() {
    let now = at(11, 0);
    let bars = flat_bars((10, 20), (10, 59), "25250");
    let v = eval(
        &spread_o3a(),
        &spread_marks("20.00", "5.90", now),
        &live("25250", now, bars),
        now,
    );
    assert_eq!(code(v.as_ref()), Some("STOP")); // V = 14.00 <= 15.00
}

#[test]
fn o3a_invalidated_by_a_close_back_inside_the_morning_range() {
    let now = at(10, 31);
    let bars = flat_bars((10, 20), (10, 29), "25150");
    let v = eval(
        &spread_o3a(),
        &spread_marks("35.00", "5.00", now),
        &live("25150", now, bars),
        now,
    );
    assert_eq!(code(v.as_ref()), Some("INVALIDATED"));
}

#[test]
fn o3b_invalidated_by_any_bar_through_half_gap() {
    let now = at(10, 31);
    let bars = vec![bar_hl(10, 29, "25120", None, Some("25099"))];
    let v = eval(
        &spread(Sleeve::O3b, "30.00"),
        &spread_marks("35.00", "5.00", now),
        &live("25120", now, bars),
        now,
    );
    assert_eq!(code(v.as_ref()), Some("INVALIDATED"));
}

// --- no mark, the budget, the feed ---

#[test]
fn with_no_mark_only_the_clock_can_close() {
    let (early, late) = (at(11, 0), at(14, 45));
    let none = BTreeMap::new();
    assert!(eval(&spread_o3a(), &none, &live("25000", early, vec![]), early).is_none());
    let v = eval(&spread_o3a(), &none, &live("25000", late, vec![]), late).unwrap();
    assert!(v.code == "HARD_EXIT" && v.value_points.is_none());
}

#[test]
fn the_budget_breach_closes_as_stop() {
    let now = at(11, 0);
    let small = OpenPosition {
        risk_budget_inr: dec("100"),
        half_gap: None,
        ..spread_o3a()
    };
    let bars = flat_bars((10, 20), (10, 59), "25250");
    let v = eval(
        &small,
        &spread_marks("33.00", "5.90", now),
        &live("25250", now, bars),
        now,
    )
    .unwrap();
    // V = 27.00: the spread rule holds, but the loss 3.00 x 65 = 195 > 100.
    assert!(v.code == "STOP" && v.marked_loss_inr == Some(dec("195.00")));
}

fn o1_loses_the_feed_only_after_14_00(now: DateTime<FixedOffset>, lost: bool) {
    let silent_since = now - TimeDelta::seconds(cfg().chain.stale_index_seconds + 5);
    let index = IndexState {
        spot: Some(dec("25000")),
        last_tick_at: Some(silent_since),
        bars: vec![],
    };
    let v = eval(
        &condor("40.00"),
        &condor_marks("20.00", "1.00", now),
        &index,
        now,
    );
    if lost {
        let v = v.unwrap();
        assert_eq!(
            (v.code.as_str(), v.reason.as_str()),
            ("HARD_EXIT", FEED_LOST)
        );
    } else {
        assert!(v.is_none());
    }
}

#[test]
fn o1_loses_the_feed_only_after_14_00_at_1359() {
    o1_loses_the_feed_only_after_14_00(at_s(13, 59, 0), false);
}

#[test]
fn o1_loses_the_feed_only_after_14_00_at_1405() {
    o1_loses_the_feed_only_after_14_00(at_s(14, 5, 0), true);
}

#[test]
fn o2_loses_the_feed_at_any_time_after_the_grace() {
    let opened = at(10, 6);
    let now = opened + TimeDelta::seconds(cfg().execution.feed_grace_seconds + 30);
    let index = IndexState {
        spot: Some(dec("25060")),
        last_tick_at: Some(now - TimeDelta::minutes(2)),
        bars: vec![],
    };
    let marks = BTreeMap::from([(LegRole::LongCall, q("205.00", "205.50", now))]);
    let v = eval(&long_call("200.00", opened), &marks, &index, now).unwrap();
    assert_eq!(
        (v.code.as_str(), v.reason.as_str()),
        ("HARD_EXIT", FEED_LOST)
    );
}

// --- the bars the rules read ---

#[test]
fn the_five_minute_close_is_a_completed_bar_after_entry() {
    let bars = flat_bars((10, 0), (10, 13), "25050");
    let open = NaiveTime::from_hms_opt(9, 15, 0).unwrap();
    // At 10:14:30 the 10:10-10:14 bar is not complete.
    assert_eq!(
        latest_five_minute_close(&bars, &at_s(10, 14, 30), &at(10, 6), open, 5).unwrap(),
        Some(dec("25050"))
    );
    assert_eq!(
        latest_five_minute_close(&bars, &at_s(10, 14, 30), &at(10, 10), open, 5).unwrap(),
        None
    );
}

#[test]
fn the_bar_after_entry() {
    let bars = flat_bars((10, 0), (10, 5), "25050");
    let found = latest_bar_after(&bars, &at(10, 6), &at(10, 3)).unwrap();
    assert_eq!(found.ts, at(10, 5));
    assert!(latest_bar_after(&bars, &at(10, 6), &at(10, 7)).is_none());
}
