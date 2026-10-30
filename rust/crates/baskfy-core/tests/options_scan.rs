//! Port of `tests/test_options_scan.py` (OP4, `04` §10).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use std::collections::{BTreeMap, BTreeSet};

use chrono::{DateTime, FixedOffset, NaiveDate, TimeDelta, Timelike};
use rust_decimal::{Decimal, RoundingStrategy};

use baskfy_core::options::bars::{Bar, closed};
use baskfy_core::options::calendar::expiry_for_o2;
use baskfy_core::options::condor::{self, CondorBuild};
use baskfy_core::options::config::{OptionsCeilings, OptionsConfig, Sleeve};
use baskfy_core::options::directional::{self, LongBuild};
use baskfy_core::options::expiry_setups::{self, SpreadBuild};
use baskfy_core::options::num::at_ist;
use baskfy_core::options::scan::{
    DayContext, MarketDay, MinuteKey, ScanResult, ScanState, SleeveContext, Snapshot, SnapshotBook,
    Value, scan_all, wanted_minutes,
};
use baskfy_core::options::session::SessionState;
use baskfy_core::options::structures::{
    Direction, Rejection, SleeveBook, ViewInputs, chain_view, to_json,
};
use options_common::{
    LOT, Smile, at, carry_forward, d, dec, flat, gap_hold, gap_hold_bars, market, market_with,
    nifty_master, o2_counter, o2_counter_bars, o2_tuesday, o2_up_break, o2_up_break_bars,
    quiet_monthly, quiet_monthly_bars, scan_snapshot, settle, skew, tick, trend_weekly,
    trend_weekly_bars,
};

fn cfg() -> OptionsConfig {
    OptionsConfig::default()
}

fn ceil() -> OptionsCeilings {
    OptionsCeilings::default()
}

fn two_nearest(day: NaiveDate) -> Vec<NaiveDate> {
    let set: BTreeSet<NaiveDate> = nifty_master()
        .iter()
        .map(|c| c.expiry)
        .filter(|e| *e >= day)
        .collect();
    set.into_iter().take(2).collect()
}

/// What the collector would have stored at `minute`: the chain around the last close.
fn snapshot_at(bars: &[Bar], minute: DateTime<FixedOffset>, smile: &Smile) -> Option<Snapshot> {
    let seen = closed(bars, &minute);
    let spot = seen.last()?.close;
    let exps = two_nearest(minute.date_naive());
    let fwd: BTreeMap<NaiveDate, Decimal> = exps
        .iter()
        .map(|e| (*e, carry_forward(spot, minute, *e)))
        .collect();
    Some(scan_snapshot(minute, spot, &exps, smile, Some(&fwd)))
}

fn floor_minute(now: DateTime<FixedOffset>) -> DateTime<FixedOffset> {
    now.with_second(0).unwrap().with_nanosecond(0).unwrap()
}

fn run_full(
    m: &MarketDay,
    now: DateTime<FixedOffset>,
    smile: &Smile,
    ctx: &DayContext,
    latest_lag_minutes: i64,
) -> BTreeMap<Sleeve, ScanResult> {
    let options = cfg();
    let minute = floor_minute(now);
    let mut loaded: BTreeMap<MinuteKey, Option<Snapshot>> = BTreeMap::new();
    for wanted in wanted_minutes(m, ctx, &now, &options, &ceil()).unwrap() {
        let at_minute = wanted.unwrap_or(minute - TimeDelta::minutes(latest_lag_minutes));
        loaded.insert(wanted, snapshot_at(&m.bars, at_minute, smile));
    }
    let mut book = SnapshotBook::new(loaded);
    scan_all(m, ctx, &now, &mut book, &options, &ceil())
        .unwrap()
        .into_iter()
        .map(|r| (r.sleeve, r))
        .collect()
}

fn run(m: &MarketDay, now: DateTime<FixedOffset>, smile: &Smile) -> BTreeMap<Sleeve, ScanResult> {
    run_full(m, now, smile, &DayContext::default(), 0)
}

fn run_ctx(
    m: &MarketDay,
    now: DateTime<FixedOffset>,
    smile: &Smile,
    ctx: &DayContext,
) -> BTreeMap<Sleeve, ScanResult> {
    run_full(m, now, smile, ctx, 0)
}

fn plus20(day: NaiveDate, hh: u32, mm: u32) -> DateTime<FixedOffset> {
    at(day, hh, mm) + TimeDelta::seconds(20)
}

fn quiet() -> MarketDay {
    market(quiet_monthly(), quiet_monthly_bars())
}
fn trend() -> MarketDay {
    market(trend_weekly(), trend_weekly_bars())
}
fn gap() -> MarketDay {
    market(gap_hold(), gap_hold_bars())
}
fn up() -> MarketDay {
    market(o2_up_break(), o2_up_break_bars())
}
fn counter() -> MarketDay {
    market(o2_counter(), o2_counter_bars())
}
fn monthly_smile() -> Box<Smile> {
    Box::new(skew(0.30, 0.33))
}
fn fl(v: f64) -> Box<Smile> {
    Box::new(flat(v))
}

fn s(v: &str) -> Value {
    Value::Str(v.to_owned())
}

fn num<'a>(r: &'a ScanResult, key: &str) -> &'a Value {
    r.numbers
        .get(key)
        .unwrap_or_else(|| panic!("no {key} in numbers"))
}

fn num_str(r: &ScanResult, key: &str) -> String {
    match num(r, key) {
        Value::Str(v) => v.clone(),
        other => panic!("{key} is {other:?}"),
    }
}

fn reasons(r: &ScanResult) -> Vec<&str> {
    r.reasons.iter().map(String::as_str).collect()
}

// --- TestEachFixtureDay ---

#[test]
fn quiet_monthly_o1m_would_trade() {
    let r = run(&quiet(), plus20(quiet_monthly(), 10, 1), &*monthly_smile())
        .remove(&Sleeve::O1m)
        .unwrap();
    assert!(r.state == ScanState::WouldTrade && r.reasons.is_empty());
    assert_eq!(r.candidates.len(), 1);
    let c = &r.candidates[0];
    // The collector's chain carries a cost-of-carry forward, so the credit is 39.65 here.
    assert!(c.viable() && c.points == Some(dec("39.65")) && c.lots == 1);
    assert_eq!(r.as_of_minute, Some(at(quiet_monthly(), 10, 0)));
    assert_eq!(num(&r, "gap_pct"), &s("0.0400"));
    assert_eq!(num(&r, "contained"), &Value::Bool(true));
}

#[test]
fn quiet_monthly_observes_before_and_is_done_after_the_window() {
    let before = run(&quiet(), plus20(quiet_monthly(), 9, 50), &*monthly_smile());
    assert_eq!(before[&Sleeve::O1m].state, ScanState::Observing);
    let after = run(&quiet(), plus20(quiet_monthly(), 10, 15), &*monthly_smile())
        .remove(&Sleeve::O1m)
        .unwrap();
    assert_eq!(after.state, ScanState::Done);
    assert_eq!(num(&after, "verdict"), &s(ScanState::WouldTrade.value()));
}

#[test]
fn on_the_monthly_o1w_is_not_today() {
    let r = run(&quiet(), plus20(quiet_monthly(), 10, 1), &*monthly_smile())
        .remove(&Sleeve::O1w)
        .unwrap();
    assert!(r.state == ScanState::NotToday && reasons(&r) == ["MONTHLY_EXPIRY"]);
    assert_eq!(num(&r, "next_date"), &s("2026-11-03"));
}

#[test]
fn trend_weekly_o1w_skips_with_er_and_containment() {
    let r = run(&trend(), plus20(trend_weekly(), 10, 5), &*fl(0.14))
        .remove(&Sleeve::O1w)
        .unwrap();
    assert_eq!(r.state, ScanState::WouldSkip);
    assert_eq!(reasons(&r), ["NOT_CONTAINED", "ER_TOO_HIGH"]);
    assert!(r.candidates.is_empty());
}

#[test]
fn trend_weekly_o3a_triggers() {
    let r = run(&trend(), plus20(trend_weekly(), 10, 21), &*fl(0.14))
        .remove(&Sleeve::O3a)
        .unwrap();
    assert!(r.state == ScanState::Triggered && r.reasons.is_empty());
    assert_eq!(num(&r, "trigger_time"), &s("10:19"));
    assert_eq!(num(&r, "direction"), &s("UP"));
    assert_eq!(r.candidates.len(), 1);
    let c = &r.candidates[0];
    assert!(c.viable() && c.direction == Some(Direction::Up) && c.expiry == trend_weekly());
    assert_eq!(r.as_of_minute, Some(at(trend_weekly(), 10, 20)));
}

#[test]
fn trend_weekly_o3b_has_no_gap() {
    let r = run(&trend(), plus20(trend_weekly(), 9, 46), &*fl(0.14))
        .remove(&Sleeve::O3b)
        .unwrap();
    assert!(r.state == ScanState::DaySkipped && reasons(&r) == ["GAP_TOO_SMALL"]);
}

#[test]
fn gap_hold_o3b_triggers_at_0945() {
    let r = run(&gap(), plus20(gap_hold(), 9, 46), &*fl(0.14))
        .remove(&Sleeve::O3b)
        .unwrap();
    assert_eq!(r.state, ScanState::Triggered);
    assert_eq!(r.candidates.len(), 1);
    let c = &r.candidates[0];
    assert!(c.viable() && c.points == Some(dec("26.95")));
    let strikes: Vec<Decimal> = c.legs.iter().map(|l| l.strike).collect();
    assert_eq!(strikes, [Decimal::from(25200), Decimal::from(25300)]);
    assert_eq!(r.as_of_minute, Some(at(gap_hold(), 9, 45)));
}

#[test]
fn gap_hold_o3b_builds_before_the_plan_time() {
    let r = run(&gap(), plus20(gap_hold(), 9, 30), &*fl(0.14))
        .remove(&Sleeve::O3b)
        .unwrap();
    assert_eq!(r.state, ScanState::BuildingRange);
    assert_eq!(num(&r, "held_so_far"), &Value::Bool(true));
    assert_eq!(num(&r, "direction"), &s("UP"));
}

#[test]
fn o2_up_break_monday_uses_tuesdays_contract() {
    let r = run(&up(), plus20(o2_up_break(), 10, 6), &*fl(0.13))
        .remove(&Sleeve::O2)
        .unwrap();
    assert_eq!(r.state, ScanState::Triggered);
    assert_eq!(num(&r, "trigger_time"), &s("10:04"));
    assert_eq!(num(&r, "index"), &s("NIFTY 50"));
    assert_eq!(num(&r, "expiry"), &s("2026-10-20"));
    assert_eq!(r.candidates.len(), 1);
    let c = &r.candidates[0];
    assert!(c.viable() && c.expiry == d(2026, 10, 20));
    assert!(c.legs[0].strike == Decimal::from(25000) && c.points == Some(dec("106.75")));
}

#[test]
fn o2_on_a_tuesday_expiry_uses_next_weeks_contract() {
    assert_eq!(
        expiry_for_o2(o2_tuesday(), &nifty_master(), "NIFTY"),
        Some(d(2026, 10, 27))
    );
    let r = run(&trend(), plus20(trend_weekly(), 9, 40), &*fl(0.13))
        .remove(&Sleeve::O2)
        .unwrap();
    assert_eq!(num(&r, "expiry"), &s("2026-10-27"));
    assert!(r.candidates.iter().all(|c| c.expiry == d(2026, 10, 27)));
}

#[test]
fn o2_counter_trend_break_is_seen_not_traded() {
    let r = run(&counter(), plus20(o2_counter(), 10, 30), &*fl(0.13))
        .remove(&Sleeve::O2)
        .unwrap();
    assert_eq!(r.state, ScanState::Armed);
    let Value::List(breaks) = num(&r, "counter_trend_breaks") else {
        panic!("counter_trend_breaks is not a list");
    };
    let first = &breaks[0];
    assert_eq!(first.0.len(), 3);
    assert_eq!(first.get("close_time"), Some(&s("10:09")));
    assert_eq!(first.get("close"), Some(&s("24990.00")));
    assert_eq!(first.get("direction"), Some(&s("DOWN")));
    let closing = run(&counter(), plus20(o2_counter(), 13, 33), &*fl(0.13));
    assert_eq!(closing[&Sleeve::O2].state, ScanState::WindowClosed);
}

#[test]
fn o2_armed_shows_the_level_and_the_distance() {
    let r = run(&up(), plus20(o2_up_break(), 9, 45), &*fl(0.13))
        .remove(&Sleeve::O2)
        .unwrap();
    assert!(r.state == ScanState::Armed && num(&r, "direction") == &s("UP"));
    let last = dec(&num_str(&r, "last_close"));
    let level = Decimal::from(25032) * dec("1.0005");
    assert_eq!(
        num_str(&r, "trigger_level"),
        level
            .round_dp_with_strategy(2, RoundingStrategy::MidpointNearestEven)
            .to_string()
    );
    assert_eq!(
        dec(&num_str(&r, "distance_points")),
        (level - last).round_dp_with_strategy(2, RoundingStrategy::MidpointNearestEven)
    );
    assert!(!r.candidates.is_empty() && r.candidates[0].viable());
}

#[test]
fn o2_builds_its_range_before_0930() {
    let r = run(&up(), plus20(o2_up_break(), 9, 20), &*fl(0.13));
    assert_eq!(r[&Sleeve::O2].state, ScanState::BuildingRange);
}

#[test]
fn o2_day_skipped_on_high_vix() {
    let m = market_with(
        o2_up_break(),
        o2_up_break_bars(),
        None,
        Some(Decimal::from(25)),
        true,
    );
    let r = run(&m, plus20(o2_up_break(), 9, 40), &*fl(0.13))
        .remove(&Sleeve::O2)
        .unwrap();
    assert!(r.state == ScanState::DaySkipped && reasons(&r) == ["VIX_TOO_HIGH"]);
}

#[test]
fn o3_on_a_non_expiry_is_not_today() {
    let r = run(&up(), plus20(o2_up_break(), 10, 6), &*fl(0.13));
    assert_eq!(r[&Sleeve::O3a].state, ScanState::NotToday);
    assert_eq!(num(&r[&Sleeve::O3a], "next_date"), &s("2026-10-20"));
}

#[test]
fn an_event_day_is_not_today_for_every_sleeve() {
    let ctx = DayContext {
        event_days: vec![quiet_monthly()],
        ..DayContext::default()
    };
    let r = run_ctx(
        &quiet(),
        plus20(quiet_monthly(), 10, 1),
        &*monthly_smile(),
        &ctx,
    );
    let states: BTreeMap<Sleeve, ScanState> = r.iter().map(|(k, x)| (*k, x.state)).collect();
    let expected: BTreeMap<Sleeve, ScanState> = Sleeve::ALL
        .iter()
        .map(|s| (*s, ScanState::NotToday))
        .collect();
    assert_eq!(states, expected);
    assert!(r.values().all(|x| reasons(x) == ["EVENT_DAY"]));
}

#[test]
fn a_holiday_is_not_today() {
    let m = market_with(
        quiet_monthly(),
        quiet_monthly_bars(),
        None,
        Some(Decimal::from(14)),
        false,
    );
    let r = run(&m, plus20(quiet_monthly(), 10, 1), &*monthly_smile());
    assert_eq!(r.len(), 5);
    assert!(r.values().all(|x| reasons(x) == ["NOT_TRADING_DAY"]));
}

// --- TestTheCandidateIsThePlan ---

fn listed(expiry: NaiveDate) -> Vec<Decimal> {
    nifty_master()
        .iter()
        .filter(|c| c.expiry == expiry)
        .map(|c| c.strike)
        .collect()
}

#[test]
fn the_candidate_is_the_plan_o1() {
    let options = cfg();
    let r = run(&quiet(), plus20(quiet_monthly(), 10, 1), &*monthly_smile())
        .remove(&Sleeve::O1m)
        .unwrap();
    let snap = snapshot_at(&quiet().bars, at(quiet_monthly(), 10, 0), &*monthly_smile()).unwrap();
    let strikes = listed(quiet_monthly());
    let view = chain_view(
        &snap.quotes,
        &ViewInputs {
            expiry: quiet_monthly(),
            spot: snap.spot.unwrap(),
            now: snap.ts,
            tick: tick(),
            config: &options.chain,
            settle: settle(),
            listed_strikes: Some(&strikes),
        },
    )
    .unwrap();
    let plan = condor::build(
        view.as_ref(),
        &CondorBuild {
            or_high: dec(&num_str(&r, "or_high")),
            or_low: dec(&num_str(&r, "or_low")),
            lot_size: Some(LOT),
            book: SleeveBook::default(),
            slot_free: true,
            paused: false,
            expiry: quiet_monthly(),
        },
        &options.condor_monthly,
        &options,
        &ceil(),
    )
    .unwrap();
    assert_eq!(r.candidates, vec![plan]);
}

#[test]
fn the_candidate_is_the_plan_o2() {
    let options = cfg();
    let r = run(&up(), plus20(o2_up_break(), 10, 6), &*fl(0.13))
        .remove(&Sleeve::O2)
        .unwrap();
    let snap = snapshot_at(&up().bars, at(o2_up_break(), 10, 5), &*fl(0.13)).unwrap();
    let tue = d(2026, 10, 20);
    let strikes = listed(tue);
    let view = chain_view(
        &snap.quotes,
        &ViewInputs {
            expiry: tue,
            spot: snap.spot.unwrap(),
            now: snap.ts,
            tick: tick(),
            config: &options.chain,
            settle: settle(),
            listed_strikes: Some(&strikes),
        },
    )
    .unwrap();
    let plan = directional::build(
        view.as_ref(),
        Direction::Up,
        &LongBuild {
            lot_size: Some(LOT),
            book: SleeveBook::default(),
            expiry: tue,
            paused: false,
        },
        &options.directional,
        &options,
        &ceil(),
    )
    .unwrap();
    assert_eq!(r.candidates, vec![plan]);
}

#[test]
fn the_candidate_is_the_plan_o3() {
    let options = cfg();
    let r = run(&gap(), plus20(gap_hold(), 9, 46), &*fl(0.14))
        .remove(&Sleeve::O3b)
        .unwrap();
    let snap = snapshot_at(&gap().bars, at(gap_hold(), 9, 45), &*fl(0.14)).unwrap();
    let strikes = listed(gap_hold());
    let view = chain_view(
        &snap.quotes,
        &ViewInputs {
            expiry: gap_hold(),
            spot: snap.spot.unwrap(),
            now: snap.ts,
            tick: tick(),
            config: &options.chain,
            settle: settle(),
            listed_strikes: Some(&strikes),
        },
    )
    .unwrap();
    let plan = expiry_setups::build(
        view.as_ref(),
        Direction::Up,
        &SpreadBuild {
            lot_size: Some(LOT),
            book: SleeveBook::default(),
            expiry: gap_hold(),
            slot_free: true,
            paused: false,
        },
        &options.expiry_setups,
        &options,
        &ceil(),
    )
    .unwrap();
    assert_eq!(r.candidates, vec![plan]);
}

// --- TestTheSlot ---

#[test]
fn o3_holding_the_slot_makes_o1_slot_taken_and_leaves_o2_alone() {
    let ctx = DayContext {
        slot_holder: Some(Sleeve::O3b),
        ..DayContext::default()
    };
    let r = run_ctx(
        &quiet(),
        plus20(quiet_monthly(), 10, 1),
        &*monthly_smile(),
        &ctx,
    );
    assert_eq!(r[&Sleeve::O1m].state, ScanState::SlotTaken);
    assert_eq!(
        reasons(&r[&Sleeve::O1m]),
        [Rejection::RejectedSlotTaken.value()]
    );
    assert_ne!(r[&Sleeve::O2].state, ScanState::SlotTaken);
}

#[test]
fn a_sleeve_holding_the_slot_itself_is_not_refused() {
    let ctx = DayContext {
        slot_holder: Some(Sleeve::O1m),
        ..DayContext::default()
    };
    let r = run_ctx(
        &quiet(),
        plus20(quiet_monthly(), 10, 1),
        &*monthly_smile(),
        &ctx,
    );
    assert_eq!(r[&Sleeve::O1m].state, ScanState::WouldTrade);
}

#[test]
fn o3b_holds_the_day_so_o3a_cannot() {
    let r = run(&gap(), plus20(gap_hold(), 10, 30), &*fl(0.14));
    assert_eq!(r[&Sleeve::O3b].state, ScanState::Triggered);
    assert_eq!(r[&Sleeve::O3a].state, ScanState::SlotTaken);
    assert!(r[&Sleeve::O3a].reasons.iter().any(|x| x == "O3B_HOLDS"));
}

fn with_sleeve(sleeve: Sleeve, sc: SleeveContext) -> DayContext {
    DayContext {
        sleeves: BTreeMap::from([(sleeve, sc)]),
        ..DayContext::default()
    }
}

#[test]
fn a_lapsed_o3b_frees_o3a() {
    let ctx = with_sleeve(
        Sleeve::O3b,
        SleeveContext {
            session_state: Some(SessionState::Lapsed),
            ..SleeveContext::default()
        },
    );
    let r = run_ctx(&gap(), plus20(gap_hold(), 10, 30), &*fl(0.14), &ctx);
    assert_eq!(r[&Sleeve::O3a].state, ScanState::Armed);
}

// --- TestOverrides ---

#[test]
fn paused_overrides_and_keeps_the_numbers() {
    let ctx = with_sleeve(
        Sleeve::O1m,
        SleeveContext {
            paused: true,
            ..SleeveContext::default()
        },
    );
    let r = run_ctx(
        &quiet(),
        plus20(quiet_monthly(), 10, 1),
        &*monthly_smile(),
        &ctx,
    )
    .remove(&Sleeve::O1m)
    .unwrap();
    assert_eq!(r.state, ScanState::Paused);
    assert!(
        r.reasons
            .iter()
            .any(|x| x == Rejection::RejectedPaused.value())
    );
    assert_ne!(num(&r, "er"), &Value::Null);
    assert_eq!(r.candidates[0].rejection, Some(Rejection::RejectedPaused));
}

#[test]
fn a_planned_session_shows_planned() {
    let ctx = with_sleeve(
        Sleeve::O1m,
        SleeveContext {
            session_state: Some(SessionState::Confirmed),
            ..SleeveContext::default()
        },
    );
    let r = run_ctx(
        &quiet(),
        plus20(quiet_monthly(), 10, 5),
        &*monthly_smile(),
        &ctx,
    );
    assert_eq!(r[&Sleeve::O1m].state, ScanState::Planned);
}

#[test]
fn no_chain_is_a_refusal_not_a_guess() {
    let now = plus20(quiet_monthly(), 10, 1);
    let mut book = SnapshotBook::default();
    let results = scan_all(
        &quiet(),
        &DayContext::default(),
        &now,
        &mut book,
        &cfg(),
        &ceil(),
    )
    .unwrap();
    let r = results.iter().find(|x| x.sleeve == Sleeve::O1m).unwrap();
    assert!(r.state == ScanState::WouldSkip && reasons(r) == ["REJECTED_NO_CHAIN"]);
    assert!(r.stale && r.as_of_minute.is_none());
}

// --- TestDecisionSnapshots ---

#[test]
fn wanted_minutes_names_each_decision_and_the_latest() {
    let wanted = wanted_minutes(
        &gap(),
        &DayContext::default(),
        &plus20(gap_hold(), 10, 30),
        &cfg(),
        &ceil(),
    )
    .unwrap();
    assert!(wanted.contains(&Some(at(gap_hold(), 9, 45))));
    assert!(wanted.contains(&None));
    // Before any sleeve prices anything, no snapshot is read at all.
    let early = wanted_minutes(
        &quiet(),
        &DayContext::default(),
        &plus20(quiet_monthly(), 9, 20),
        &cfg(),
        &ceil(),
    )
    .unwrap();
    assert!(early.is_empty());
}

#[test]
fn a_decided_candidate_does_not_move_with_later_premiums() {
    let now = plus20(quiet_monthly(), 10, 10);
    let a = run(&quiet(), now, &*monthly_smile())
        .remove(&Sleeve::O1m)
        .unwrap();
    let q = quiet();
    let mut book = SnapshotBook::new(BTreeMap::from([
        (
            Some(at(quiet_monthly(), 10, 0)),
            snapshot_at(&q.bars, at(quiet_monthly(), 10, 0), &*monthly_smile()),
        ),
        (
            None,
            snapshot_at(&q.bars, at(quiet_monthly(), 10, 10), &skew(0.5, 0.5)),
        ),
    ]));
    let rows = scan_all(&q, &DayContext::default(), &now, &mut book, &cfg(), &ceil()).unwrap();
    let b = rows.into_iter().find(|x| x.sleeve == Sleeve::O1m).unwrap();
    assert_eq!(a.candidates, b.candidates);
}

#[test]
fn a_snapshot_minutes_old_is_stale() {
    let r = run_full(
        &up(),
        plus20(o2_up_break(), 9, 45),
        &*fl(0.13),
        &DayContext::default(),
        3,
    );
    assert!(r[&Sleeve::O2].stale);
    let fresh = run(&up(), plus20(o2_up_break(), 9, 45), &*fl(0.13));
    assert!(!fresh[&Sleeve::O2].stale);
}

// --- TestIdempotenceAndShape ---

#[test]
fn the_same_minute_gives_the_same_rows() {
    let now = plus20(trend_weekly(), 10, 21);
    assert_eq!(
        run(&trend(), now, &*fl(0.14)),
        run(&trend(), now, &*fl(0.14))
    );
}

#[test]
fn the_row_minute_is_floored_ist() {
    let r = run(&trend(), plus20(trend_weekly(), 10, 21), &*fl(0.14));
    assert_eq!(
        r[&Sleeve::O2].ts,
        at_ist(
            d(2026, 10, 20),
            chrono::NaiveTime::from_hms_opt(10, 21, 0).unwrap()
        )
    );
}

fn walk(value: &serde_json::Value) {
    match value {
        serde_json::Value::Number(n) => assert!(!n.is_f64(), "{n}"),
        serde_json::Value::Object(m) => m.values().for_each(walk),
        serde_json::Value::Array(a) => a.iter().for_each(walk),
        _ => {}
    }
}

#[test]
fn candidates_json_has_no_floats() {
    let cases: [(MarketDay, DateTime<FixedOffset>, Box<Smile>); 2] = [
        (quiet(), plus20(quiet_monthly(), 10, 1), monthly_smile()),
        (gap(), plus20(gap_hold(), 10, 30), fl(0.14)),
    ];
    for (day, now, smile) in &cases {
        for r in run(day, *now, &**smile).values() {
            walk(&serde_json::to_value(&r.numbers).unwrap());
            for c in &r.candidates {
                walk(&serde_json::to_value(to_json(c).unwrap()).unwrap());
            }
        }
    }
}

/// `04` §10's one-way order per sleeve: each state's rank may only rise within a day.
fn order(sleeve: Sleeve, state: ScanState) -> i32 {
    match sleeve {
        Sleeve::O1m | Sleeve::O1w => match state {
            ScanState::Observing => 0,
            ScanState::WouldTrade | ScanState::WouldSkip => 1,
            ScanState::SlotTaken | ScanState::Planned => 2,
            ScanState::Done => 3,
            other => panic!("{sleeve:?} has no rank for {other:?}"),
        },
        Sleeve::O2 | Sleeve::O3a | Sleeve::O3b => match state {
            ScanState::BuildingRange => 0,
            ScanState::DaySkipped | ScanState::Armed => 1,
            ScanState::SlotTaken | ScanState::Triggered | ScanState::WindowClosed => 2,
            other => panic!("{sleeve:?} has no rank for {other:?}"),
        },
    }
}

fn states_are_one_way_through_the_day(m: &MarketDay, smile: &Smile) {
    let mut ranks: BTreeMap<Sleeve, i32> = BTreeMap::new();
    let mut minute = at(m.trade_date, 9, 15);
    let end = at(m.trade_date, 15, 30);
    while minute <= end {
        for (sleeve, r) in run(m, minute + TimeDelta::seconds(20), smile) {
            if r.state == ScanState::NotToday {
                continue;
            }
            let rank = order(sleeve, r.state);
            let prev = ranks.get(&sleeve).copied().unwrap_or(0);
            assert!(rank >= prev, "{sleeve:?} {} {:?}", minute.time(), r.state);
            ranks.insert(sleeve, rank);
        }
        minute += TimeDelta::minutes(5);
    }
}

#[test]
fn states_are_one_way_quiet_monthly() {
    states_are_one_way_through_the_day(&quiet(), &*monthly_smile());
}

#[test]
fn states_are_one_way_trend_weekly() {
    states_are_one_way_through_the_day(&trend(), &*fl(0.14));
}

#[test]
fn states_are_one_way_gap_hold() {
    states_are_one_way_through_the_day(&gap(), &*fl(0.14));
}

#[test]
fn states_are_one_way_o2_up_break() {
    states_are_one_way_through_the_day(&up(), &*fl(0.13));
}
