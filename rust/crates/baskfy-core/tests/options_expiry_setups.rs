//! Port of `tests/test_options_expiry_setups.py` (OP4 — O3, the expiry-day setups).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset, NaiveTime};
use rust_decimal::Decimal;

use baskfy_core::options::bars::Bar;
use baskfy_core::options::config::{
    ExpirySetupsConfig, OptionType, OptionsCeilings, OptionsConfig, Side, SizingMode,
};
use baskfy_core::options::costs::{CostFill, charges};
use baskfy_core::options::execution::{LegRole, entry_sequence, exit_sequence};
use baskfy_core::options::expiry_setups::{
    self, GapHoldScan, RangeBreakScan, SetupClock, SetupReason, SpreadBuild, SpreadExit, SpreadTick,
};
use baskfy_core::options::num;
use baskfy_core::options::structures::{
    Candidate, ChainView, Direction, Rejection, SleeveBook, Structure, ViewInputs, chain_view,
};
use options_common::{
    LOT, Snapshot, at, bars_from_closes, carry_forward, d, dec, fill_lists, flat, gap_hold_bars,
    gap_hold_day, ist_dt, prev_close, ramp, settle, snapshot_with, tick, trend_weekly,
    trend_weekly_bars, walk_entry, walk_exit, zigzag,
};

fn cfg() -> OptionsConfig {
    OptionsConfig::default()
}

fn x() -> ExpirySetupsConfig {
    cfg().expiry_setups
}

fn hm(h: u32, m: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(h, m, 0).unwrap()
}

fn clock(now: DateTime<FixedOffset>, event: bool) -> SetupClock {
    let c = cfg();
    SetupClock {
        now,
        event_day: event,
        session_open: c.calendar.market_open,
        grace_seconds: c.chain.stale_scan_seconds,
    }
}

fn range_break(bars: &[Bar], now: DateTime<FixedOffset>, event: bool) -> RangeBreakScan {
    expiry_setups::range_break(bars, prev_close(), &clock(now, event), &x()).unwrap()
}

fn gap_hold(bars: &[Bar], now: DateTime<FixedOffset>, event: bool) -> GapHoldScan {
    expiry_setups::gap_hold(bars, prev_close(), &clock(now, event), &x()).unwrap()
}

// --- O3-A range break ---

#[test]
fn o3a_the_trend_weekly_triggers_at_1019_with_a_high_er() {
    let r = range_break(&trend_weekly_bars(), at(trend_weekly(), 10, 20), false);
    assert!(r.final_ && r.reasons.is_empty());
    assert_eq!(r.bars, x().o3a_range_bars);
    assert_eq!(
        (r.range_high, r.range_low),
        (Some(Decimal::from(25182)), Some(Decimal::from(24998)))
    );
    assert_eq!(r.range_pct, Some(dec("0.736")));
    assert_eq!(
        r.level_up,
        Some(num::mul(Decimal::from(25182), dec("1.0005")).unwrap())
    );
    assert_eq!(
        (r.trigger_time, r.trigger_close, r.direction),
        (
            Some(hm(10, 19)),
            Some(Decimal::from(25210)),
            Some(Direction::Up)
        )
    );
    assert_eq!(r.trigger_er, Some(Decimal::ONE));
}

#[test]
fn o3a_the_range_must_be_settled_first() {
    let r = range_break(
        &trend_weekly_bars(),
        ist_dt(2026, 10, 20, 10, 14, 30),
        false,
    );
    assert!(!r.final_ && r.trigger_time.is_none());
}

#[test]
fn o3a_a_wide_morning_range_skips() {
    let bars = bars_from_closes(
        trend_weekly(),
        Decimal::from(25000),
        &ramp(Decimal::from(25000), Decimal::from(6), 120),
    );
    let r = range_break(&bars, at(trend_weekly(), 10, 30), false);
    assert_eq!(r.reasons, vec![SetupReason::RangeTooWide]);
    assert!(r.trigger_time.is_none());
}

#[test]
fn o3a_a_break_with_a_low_er_is_recorded_and_the_watch_goes_on() {
    let mut closes = zigzag(Decimal::from(25000), Decimal::from(60), 60);
    let grind = ramp(*closes.last().unwrap(), Decimal::from(3), 60);
    closes.extend(grind);
    let r = range_break(
        &bars_from_closes(trend_weekly(), Decimal::from(25000), &closes),
        at(trend_weekly(), 11, 15),
        false,
    );
    assert!(r.trigger_time.is_none());
    assert!(!r.low_er_breaks.is_empty(), "the break must be seen");
    assert!(
        r.low_er_breaks
            .iter()
            .all(|(_, _, er)| *er < x().o3a_er_min)
    );
}

#[test]
fn o3a_a_break_after_1300_is_not_a_trigger() {
    let mut closes = vec![Decimal::from(25000); 13 * 60 + 5 - (9 * 60 + 15)];
    closes.extend(vec![Decimal::from(25300); 10]);
    let bars = bars_from_closes(trend_weekly(), Decimal::from(25000), &closes);
    let r = range_break(&bars, at(trend_weekly(), 13, 30), false);
    assert!(r.trigger_time.is_none() && r.window_closed);
}

#[test]
fn o3a_event_day() {
    let r = range_break(&trend_weekly_bars(), at(trend_weekly(), 10, 20), true);
    assert_eq!(r.reasons, vec![SetupReason::EventDay]);
}

// --- O3-B gap hold ---

#[test]
fn o3b_the_gap_holds_and_triggers_at_0945() {
    let g = gap_hold(&gap_hold_bars(), at(gap_hold_day(), 9, 45), false);
    assert!(g.final_ && g.reasons.is_empty() && g.triggered);
    assert_eq!(g.gap_points, Some(Decimal::from(200)));
    assert_eq!(g.gap_pct, Some(dec("0.8")));
    assert_eq!(g.half_gap, Some(Decimal::from(25100)));
    assert_eq!(g.direction, Some(Direction::Up));
    assert_eq!(g.held_so_far, Some(true));
    assert_eq!(g.bars, 30);
}

#[test]
fn o3b_not_before_the_plan_time() {
    let g = gap_hold(&gap_hold_bars(), ist_dt(2026, 10, 13, 9, 44, 50), false);
    assert!(!g.triggered);
}

#[test]
fn o3b_the_gap_band() {
    for (open, reason) in [
        ("25120", SetupReason::GapTooSmall),
        ("25400", SetupReason::GapTooBig),
    ] {
        let o = dec(open);
        let bars = bars_from_closes(gap_hold_day(), o, &zigzag(o, Decimal::from(5), 40));
        let g = gap_hold(&bars, at(gap_hold_day(), 9, 45), false);
        assert!(g.reasons.contains(&reason) && !g.triggered, "{open}");
    }
}

#[test]
fn o3b_the_band_edges_are_inclusive() {
    for pct in [x().o3b_gap_min_pct, x().o3b_gap_max_pct] {
        let factor = num::add(Decimal::ONE, num::div(pct, Decimal::ONE_HUNDRED).unwrap()).unwrap();
        let o = num::mul(prev_close(), factor).unwrap();
        let bars = bars_from_closes(gap_hold_day(), o, &zigzag(o, Decimal::ONE, 40));
        let g = gap_hold(&bars, at(gap_hold_day(), 9, 45), false);
        assert!(g.reasons.is_empty() && g.triggered, "{pct}");
    }
}

#[test]
fn o3b_a_bar_through_half_the_gap_breaks_the_hold() {
    let mut closes = zigzag(Decimal::from(25200), Decimal::from(10), 40);
    closes[20] = Decimal::from(25101); // its low wicks to 25,099 <= 25,100
    let bars = bars_from_closes(gap_hold_day(), Decimal::from(25200), &closes);
    let g = gap_hold(&bars, at(gap_hold_day(), 9, 45), false);
    assert_eq!(g.reasons, vec![SetupReason::HoldBroken]);
    assert!(!g.triggered);
}

#[test]
fn o3b_a_down_gap_is_symmetric() {
    let o = Decimal::from(24800);
    let bars = bars_from_closes(gap_hold_day(), o, &zigzag(o, Decimal::from(10), 40));
    let g = gap_hold(&bars, at(gap_hold_day(), 9, 45), false);
    assert_eq!(g.direction, Some(Direction::Down));
    assert_eq!(g.half_gap, Some(Decimal::from(24900)));
    assert!(g.triggered);
}

// --- the debit spread (04 §5, §5.5, §6.4) ---

fn now() -> DateTime<FixedOffset> {
    at(gap_hold_day(), 9, 45)
}

fn spot() -> Decimal {
    Decimal::from(25190) // the 09:44 close
}

fn gap_snapshot(vol: f64, depth: i64) -> Snapshot {
    let exps = [gap_hold_day(), d(2026, 10, 20)];
    let fwd: BTreeMap<_, _> = exps
        .iter()
        .map(|e| (*e, carry_forward(spot(), now(), *e)))
        .collect();
    snapshot_with(now(), spot(), &exps, &flat(vol), Some(&fwd), depth)
}

fn default_snapshot() -> Snapshot {
    gap_snapshot(0.14, 1300)
}

fn view_of(snap: &Snapshot) -> ChainView {
    let c = cfg();
    chain_view(
        &snap.quotes,
        &ViewInputs {
            expiry: gap_hold_day(),
            spot: spot(),
            now: now(),
            tick: tick(),
            config: &c.chain,
            settle: settle(),
            listed_strikes: None,
        },
    )
    .unwrap()
    .expect("a view")
}

fn build_with(
    view: Option<&ChainView>,
    direction: Direction,
    config: &ExpirySetupsConfig,
    slot_free: bool,
) -> Candidate {
    expiry_setups::build(
        view,
        direction,
        &SpreadBuild {
            lot_size: Some(LOT),
            book: SleeveBook::default(),
            expiry: gap_hold_day(),
            slot_free,
            paused: false,
        },
        config,
        &cfg(),
        &OptionsCeilings::default(),
    )
    .unwrap()
}

fn build(view: &ChainView) -> Candidate {
    build_with(Some(view), Direction::Up, &x(), true)
}

#[test]
fn spread_the_call_spread_to_the_rupee() {
    let view = view_of(&default_snapshot());
    let c = build(&view);
    let x = x();
    assert!(c.viable() && c.structure == Structure::DebitSpread);
    let (long_leg, short_leg) = (&c.legs[0], &c.legs[1]);
    assert_eq!(c.legs.len(), 2);
    assert_eq!(view.atm, Decimal::from(25200));
    assert_eq!(
        (long_leg.role, long_leg.strike),
        (LegRole::LongCall, Decimal::from(25200))
    );
    assert_eq!(
        (short_leg.role, short_leg.strike),
        (LegRole::ShortCall, Decimal::from(25300))
    );
    let points = c.points.unwrap();
    assert_eq!(
        points,
        long_leg.ask.unwrap_or_default() - short_leg.bid.unwrap_or_default()
    );
    assert_eq!(points, dec("26.95"));
    assert!(points <= x.max_debit_frac * x.width_points);
    assert_eq!(
        c.risk_per_lot_inr,
        Some(dec("26.95") * Decimal::from(LOT) + x.reserve_per_lot_inr)
    );
    assert_eq!(c.max_loss_inr, Some(dec("26.95") * Decimal::from(LOT)));
    assert_eq!((c.lots, c.sizing_mode), (1, Some(SizingMode::PaperOneLot)));
    let t = tick();
    let fills = [
        CostFill {
            side: Side::Buy,
            price: long_leg.ask.unwrap_or_default() + t,
            quantity: LOT,
        },
        CostFill {
            side: Side::Sell,
            price: long_leg.bid.unwrap_or_default() - t,
            quantity: LOT,
        },
        CostFill {
            side: Side::Sell,
            price: short_leg.bid.unwrap_or_default() - t,
            quantity: LOT,
        },
        CostFill {
            side: Side::Buy,
            price: short_leg.ask.unwrap_or_default() + t,
            quantity: LOT,
        },
    ];
    assert_eq!(
        c.round_trip_inr,
        Some(charges(&fills, &cfg().costs).unwrap().total().unwrap())
    );
    assert_eq!(
        c.expected_gain_inr,
        Some((x.target_frac_of_width * x.width_points - points) * Decimal::from(LOT))
    );
    assert!(c.cost_share.is_some_and(|s| s <= x.cost_share_max));
}

#[test]
fn spread_the_put_spread_goes_down_from_atm() {
    let view = view_of(&default_snapshot());
    let c = build_with(Some(&view), Direction::Down, &x(), true);
    let (long_leg, short_leg) = (&c.legs[0], &c.legs[1]);
    assert_eq!(
        (long_leg.role, long_leg.strike, long_leg.option_type),
        (LegRole::LongPut, Decimal::from(25200), OptionType::Pe)
    );
    assert_eq!(
        (short_leg.role, short_leg.strike),
        (LegRole::ShortPut, Decimal::from(25100))
    );
}

#[test]
fn spread_a_debit_above_the_cap_rejects() {
    let tight = ExpirySetupsConfig {
        max_debit_frac: dec("0.25"), // 25 points < 26.95
        ..x()
    };
    let view = view_of(&default_snapshot());
    let c = build_with(Some(&view), Direction::Up, &tight, true);
    assert_eq!(c.rejection, Some(Rejection::RejectedDebit));
}

#[test]
fn spread_the_slot_must_be_free() {
    let view = view_of(&default_snapshot());
    let c = build_with(Some(&view), Direction::Up, &x(), false);
    assert_eq!(c.rejection, Some(Rejection::RejectedSlotTaken));
}

#[test]
fn spread_illiquid_at_the_quantity() {
    let view = view_of(&gap_snapshot(0.14, 10));
    assert_eq!(build(&view).rejection, Some(Rejection::RejectedIlliquid));
}

#[test]
fn spread_a_missing_short_strike() {
    let snap = default_snapshot();
    let gone = Snapshot {
        ts: snap.ts,
        spot: snap.spot,
        quotes: snap
            .quotes
            .iter()
            .filter(|q| q.strike != Decimal::from(25300))
            .cloned()
            .collect(),
    };
    assert_eq!(
        build(&view_of(&gone)).rejection,
        Some(Rejection::RejectedNoContract)
    );
}

#[test]
fn spread_never_naked_sequences() {
    let roles: Vec<LegRole> = build(&view_of(&default_snapshot()))
        .legs
        .iter()
        .map(|lg| lg.role)
        .collect();
    assert_eq!(
        entry_sequence(&roles).unwrap(),
        vec![LegRole::LongCall, LegRole::ShortCall]
    );
    assert_eq!(
        exit_sequence(&roles),
        vec![LegRole::ShortCall, LegRole::LongCall]
    );
}

#[test]
fn spread_never_naked_through_entry() {
    for fills in fill_lists(0, LOT, 6, 300, 0x03E5_0001) {
        walk_entry(&[LegRole::LongPut, LegRole::ShortPut], LOT, &fills);
    }
}

#[test]
fn spread_never_naked_through_exit() {
    for fills in fill_lists(1, LOT, 20, 300, 0x03E5_0002) {
        walk_exit(&[LegRole::LongCall, LegRole::ShortCall], LOT, &fills);
    }
}

// --- exits (04 §5.3) ---

fn decide(value: &str, clock: (u32, u32), invalid: bool, stale: bool) -> Option<SpreadExit> {
    let x = x();
    expiry_setups::exit_decision(
        &SpreadTick {
            value: dec(value),
            entry_debit: Decimal::from(30),
            width: x.width_points,
            is_invalidated: invalid,
            now: at(gap_hold_day(), clock.0, clock.1),
            stale,
            marked_loss_inr: Decimal::ZERO,
            risk_budget_inr: Decimal::from(2450),
            manual: false,
        },
        &x,
        &cfg(),
    )
    .unwrap()
}

fn plain(value: &str) -> Option<SpreadExit> {
    decide(value, (11, 0), false, false)
}

#[test]
fn exits_target_at_80_pct_of_width_stop_at_half_the_debit() {
    for (value, expected) in [
        ("15", Some(SpreadExit::Stop)),
        ("15.05", None),
        ("79.95", None),
        ("80", Some(SpreadExit::Target)),
    ] {
        assert_eq!(plain(value), expected, "{value}");
    }
}

#[test]
fn exits_hard_exit_at_1445_first() {
    assert_eq!(
        decide("10", (14, 45), false, false),
        Some(SpreadExit::HardExit)
    );
    assert_eq!(decide("40", (14, 44), false, false), None);
}

#[test]
fn exits_precedence_and_staleness() {
    assert_eq!(decide("10", (11, 0), true, false), Some(SpreadExit::Stop));
    assert_eq!(
        decide("90", (11, 0), true, false),
        Some(SpreadExit::Invalidated)
    );
    assert_eq!(decide("90", (11, 0), false, true), None);
    assert_eq!(decide("10", (11, 0), false, true), Some(SpreadExit::Stop));
}

#[test]
fn exits_the_close_value_is_long_bid_minus_short_ask() {
    assert_eq!(
        expiry_setups::close_value(dec("50.10"), dec("12.40")).unwrap(),
        dec("37.70")
    );
}

#[test]
fn exits_invalidation_rules() {
    let (hi, lo) = (Decimal::from(25182), Decimal::from(24998));
    assert!(expiry_setups::range_break_invalidated(
        Some(dec("25181.95")),
        Direction::Up,
        hi,
        lo
    ));
    assert!(!expiry_setups::range_break_invalidated(
        Some(Decimal::from(25182)),
        Direction::Up,
        hi,
        lo
    ));
    assert!(expiry_setups::range_break_invalidated(
        Some(dec("24998.05")),
        Direction::Down,
        hi,
        lo
    ));
    let bar = Bar {
        ts: at(gap_hold_day(), 10, 0),
        open: Decimal::from(25110),
        high: Decimal::from(25112),
        low: Decimal::from(25100),
        close: Decimal::from(25105),
    };
    assert!(expiry_setups::gap_hold_invalidated(
        Some(&bar),
        Direction::Up,
        Decimal::from(25100)
    ));
    assert!(!expiry_setups::gap_hold_invalidated(
        Some(&bar),
        Direction::Up,
        dec("25099.95")
    ));
    assert!(expiry_setups::gap_hold_invalidated(
        Some(&bar),
        Direction::Down,
        Decimal::from(25112)
    ));
}
