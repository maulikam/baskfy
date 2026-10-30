//! Port of `tests/test_options_directional.py`: O2's filters, trigger, contract and exits.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime, TimeDelta};
use rust_decimal::Decimal;

use baskfy_core::options::bars::Bar;
use baskfy_core::options::config::{
    DirectionalConfig, OptionType, OptionsCeilings, OptionsConfig, Side, SizingMode,
};
use baskfy_core::options::costs::{CostFill, charges};
use baskfy_core::options::directional::{
    self, Break, DayFilters, DayReason, LongBuild, LongExit, LongTick, Range, TriggerScan,
};
use baskfy_core::options::execution::LegRole;
use baskfy_core::options::num;
use baskfy_core::options::structures::{
    Candidate, ChainView, Direction, Rejection, SleeveBook, Structure, ViewInputs, chain_view,
};

use options_common::{
    LOT, Snapshot, at, bars_from_closes, bars_from_closes_wick, carry_forward, d, dec,
    falling_closes, flat, ist_dt, o2_counter, o2_counter_bars, o2_up_break, o2_up_break_bars,
    prev_close, rising_closes, rising_closes_to, settle, snapshot_with, tick, zigzag,
};

fn cfg() -> OptionsConfig {
    OptionsConfig::default()
}

fn dc() -> DirectionalConfig {
    cfg().directional
}

fn grace() -> i64 {
    cfg().chain.stale_scan_seconds
}

fn tue() -> NaiveDate {
    d(2026, 10, 20)
}

struct FilterKnobs {
    bars: Option<Vec<Bar>>,
    now: Option<DateTime<FixedOffset>>,
    daily: Option<Vec<Decimal>>,
    vix: Option<Decimal>,
    event: bool,
}

impl Default for FilterKnobs {
    fn default() -> Self {
        Self {
            bars: None,
            now: None,
            daily: None,
            vix: Some(Decimal::from(14)),
            event: false,
        }
    }
}

fn filters(k: FilterKnobs) -> DayFilters {
    directional::day_filters(
        &k.bars.unwrap_or_else(o2_up_break_bars),
        &k.daily.unwrap_or_else(rising_closes),
        k.vix,
        &k.now.unwrap_or_else(|| at(o2_up_break(), 9, 30)),
        k.event,
        &dc(),
        grace(),
    )
    .unwrap()
}

// --- the trend ---

#[test]
fn trend_the_ema_is_sma_seeded_then_recursive() {
    let closes: Vec<Decimal> = (1..=5).map(Decimal::from).collect();
    assert_eq!(
        directional::ema(&closes, 3).unwrap(),
        Some(Decimal::from(4))
    );
    assert_eq!(directional::ema(&closes[..2], 3).unwrap(), None);
}

#[test]
fn trend_up_down_flat_unknown() {
    assert_eq!(
        directional::trend(&rising_closes(), 20).unwrap().0,
        Some(Direction::Up)
    );
    assert_eq!(
        directional::trend(&falling_closes(), 20).unwrap().0,
        Some(Direction::Down)
    );
    assert_eq!(
        directional::trend(&[prev_close(); 30], 20).unwrap(),
        (None, Some(prev_close()))
    );
    assert_eq!(
        directional::trend(&rising_closes_to(prev_close(), 19), 20).unwrap(),
        (None, None)
    );
}

// --- the day filters ---

#[test]
fn filters_the_up_break_day_passes_at_0930_with_its_numbers() {
    let f = filters(FilterKnobs::default());
    assert!(f.final_ && f.trade() && f.reasons.is_empty());
    assert_eq!(f.trend, Some(Direction::Up));
    assert_eq!(f.prev_close, Some(prev_close()));
    assert_eq!(f.gap_pct, Some(dec("0.08")));
    assert_eq!(
        (f.or_high, f.or_low),
        (Some(Decimal::from(25032)), Some(Decimal::from(25008)))
    );
    assert_eq!(f.or_pct, Some(dec("0.096")));
    assert_eq!(f.bars, dc().opening_range_bars);
}

#[test]
fn filters_not_final_before_the_0929_bar_closes() {
    let f = filters(FilterKnobs {
        now: Some(ist_dt(2026, 10, 19, 9, 29, 40)),
        ..FilterKnobs::default()
    });
    assert!(!f.final_ && f.bars == 14);
}

#[test]
fn filters_each_reason() {
    let cases = [
        (
            FilterKnobs {
                daily: Some(vec![prev_close(); 30]),
                ..FilterKnobs::default()
            },
            DayReason::TrendFlat,
        ),
        (
            FilterKnobs {
                daily: Some(rising_closes_to(prev_close(), 10)),
                ..FilterKnobs::default()
            },
            DayReason::TrendUnknown,
        ),
        (
            FilterKnobs {
                vix: Some(dec("22.01")),
                ..FilterKnobs::default()
            },
            DayReason::VixTooHigh,
        ),
        (
            FilterKnobs {
                vix: None,
                ..FilterKnobs::default()
            },
            DayReason::VixUnknown,
        ),
        (
            FilterKnobs {
                event: true,
                ..FilterKnobs::default()
            },
            DayReason::EventDay,
        ),
    ];
    for (knobs, reason) in cases {
        assert_eq!(filters(knobs).reasons, vec![reason], "{}", reason.value());
    }
}

#[test]
fn filters_vix_at_the_limit_is_allowed() {
    let f = filters(FilterKnobs {
        vix: Some(dc().vix_max),
        ..FilterKnobs::default()
    });
    assert!(f.reasons.is_empty());
}

#[test]
fn filters_gap_and_range_and_all_reasons_together() {
    let bars = bars_from_closes(
        o2_up_break(),
        Decimal::from(25300),
        &zigzag(Decimal::from(25300), Decimal::from(150), 30),
    );
    let f = filters(FilterKnobs {
        bars: Some(bars),
        daily: Some(vec![prev_close(); 30]),
        vix: Some(Decimal::from(30)),
        event: true,
        ..FilterKnobs::default()
    });
    assert_eq!(
        f.reasons,
        vec![
            DayReason::TrendFlat,
            DayReason::GapTooBig,
            DayReason::RangeTooWide,
            DayReason::VixTooHigh,
            DayReason::EventDay,
        ]
    );
}

#[test]
fn filters_a_missing_minute_is_incomplete_once_settled() {
    let skip = at(o2_up_break(), 9, 20);
    let bars: Vec<Bar> = o2_up_break_bars()
        .into_iter()
        .filter(|b| b.ts != skip)
        .collect();
    let f = filters(FilterKnobs {
        bars: Some(bars),
        now: Some(at(o2_up_break(), 9, 31)),
        ..FilterKnobs::default()
    });
    assert!(f.final_);
    assert_eq!(f.reasons, vec![DayReason::IncompleteObservation]);
}

// --- the trigger ---

fn trigger(bars: &[Bar], now: DateTime<FixedOffset>) -> TriggerScan {
    let c = cfg();
    directional::find_trigger(
        bars,
        Range {
            or_high: Decimal::from(25032),
            or_low: Decimal::from(25008),
            direction: Direction::Up,
        },
        &now,
        &c.directional,
        c.calendar.market_open,
        grace(),
    )
    .unwrap()
}

fn hm(h: u32, m: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(h, m, 0).unwrap()
}

#[test]
fn trigger_the_levels_carry_the_buffer() {
    let (up, down) =
        directional::levels(Decimal::from(25032), Decimal::from(25008), dc().buffer_pct).unwrap();
    assert_eq!(up, Decimal::from(25032) * dec("1.0005"));
    assert_eq!(up, dec("25044.516"));
    assert_eq!(down, Decimal::from(25008) * dec("0.9995"));
    assert_eq!(down, dec("24995.496"));
}

#[test]
fn trigger_the_first_completed_five_minute_close_beyond_the_level() {
    let t = trigger(&o2_up_break_bars(), at(o2_up_break(), 10, 5));
    assert_eq!(
        t.trigger,
        Some(Break {
            close_time: hm(10, 4),
            close: Decimal::from(25050),
            direction: Direction::Up,
        })
    );
    assert!(t.counter_breaks.is_empty());
}

#[test]
fn trigger_not_before_the_bar_has_closed() {
    let t = trigger(&o2_up_break_bars(), ist_dt(2026, 10, 19, 10, 4, 50));
    assert!(t.trigger.is_none() && !t.window_closed);
}

#[test]
fn trigger_a_counter_trend_break_is_recorded_never_traded() {
    let t = trigger(&o2_counter_bars(), at(o2_counter(), 10, 30));
    assert!(t.trigger.is_none());
    assert_eq!(
        t.counter_breaks[0],
        Break {
            close_time: hm(10, 9),
            close: Decimal::from(24990),
            direction: Direction::Down,
        }
    );
}

#[test]
fn trigger_the_window_closes_after_1330_plus_the_grace() {
    let bars = o2_counter_bars();
    assert!(!trigger(&bars, at(o2_counter(), 13, 32)).window_closed);
    assert!(trigger(&bars, at(o2_counter(), 13, 33)).window_closed);
}

#[test]
fn trigger_a_break_after_the_window_is_not_a_trigger() {
    let mut closes = vec![Decimal::from(25020); 4 * 60 + 15];
    closes.extend(vec![Decimal::from(25100); 30]);
    let bars = bars_from_closes_wick(o2_up_break(), Decimal::from(25020), &closes, Decimal::ZERO);
    let c = cfg();
    let t = directional::find_trigger(
        &bars,
        Range {
            or_high: Decimal::from(25020),
            or_low: Decimal::from(25020),
            direction: Direction::Up,
        },
        &at(o2_up_break(), 14, 0),
        &c.directional,
        c.calendar.market_open,
        grace(),
    )
    .unwrap();
    assert!(t.trigger.is_none() && t.window_closed);
}

// --- the contract (04 §4.3-§4.6) ---

fn now() -> DateTime<FixedOffset> {
    at(o2_up_break(), 10, 5)
}

fn spot() -> Decimal {
    Decimal::from(25050)
}

fn o2_snapshot_with(depth: i64, vol: f64) -> Snapshot {
    let expiries = [tue(), d(2026, 10, 27)];
    let fwd: BTreeMap<NaiveDate, Decimal> = expiries
        .iter()
        .map(|e| (*e, carry_forward(spot(), now(), *e)))
        .collect();
    snapshot_with(now(), spot(), &expiries, &flat(vol), Some(&fwd), depth)
}

fn o2_snapshot() -> Snapshot {
    o2_snapshot_with(1300, 0.13)
}

fn view_of(snap: &Snapshot) -> ChainView {
    let c = cfg();
    chain_view(
        &snap.quotes,
        &ViewInputs {
            expiry: tue(),
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

struct Knobs {
    direction: Direction,
    book: SleeveBook,
    config: DirectionalConfig,
    paused: bool,
}

impl Default for Knobs {
    fn default() -> Self {
        Self {
            direction: Direction::Up,
            book: SleeveBook::default(),
            config: dc(),
            paused: false,
        }
    }
}

fn build_with(view: Option<&ChainView>, k: Knobs) -> Candidate {
    directional::build(
        view,
        k.direction,
        &LongBuild {
            lot_size: Some(LOT),
            book: k.book,
            expiry: tue(),
            paused: k.paused,
        },
        &k.config,
        &cfg(),
        &OptionsCeilings::default(),
    )
    .unwrap()
}

fn build(view: &ChainView) -> Candidate {
    build_with(Some(view), Knobs::default())
}

#[test]
fn contract_one_step_itm_call_on_the_up_break_to_the_rupee() {
    let view = view_of(&o2_snapshot());
    let c = build(&view);
    assert!(c.viable() && c.structure == Structure::LongOption);
    assert_eq!(c.legs.len(), 1);
    let lg = &c.legs[0];
    assert_eq!(view.atm, Decimal::from(25050));
    assert_eq!(
        (lg.role, lg.strike, lg.option_type),
        (LegRole::LongCall, Decimal::from(25000), OptionType::Ce)
    );
    let cfg_d = dc();
    let delta = num::from_float(lg.delta.unwrap().abs()).unwrap();
    assert!(cfg_d.delta_min <= delta && delta <= cfg_d.delta_max);
    let entry = lg.ask.unwrap_or_default() + tick();
    assert_eq!(c.points, Some(lg.limit_price));
    assert_eq!(lg.limit_price, entry);
    assert_eq!(entry, dec("106.75"));
    let lot = Decimal::from(LOT);
    assert_eq!(
        c.risk_per_lot_inr,
        Some(entry * cfg_d.stop_frac * lot + cfg_d.reserve_per_lot_inr)
    );
    assert_eq!(c.risk_per_lot_inr, Some(dec("2381.625")));
    assert_eq!((c.lots, c.sizing_mode), (1, Some(SizingMode::PaperOneLot)));
    assert_eq!(c.extra["gap_through_inr"], num::py_str(entry * lot));
    let fills = [
        CostFill {
            side: Side::Buy,
            price: entry,
            quantity: LOT,
        },
        CostFill {
            side: Side::Sell,
            price: lg.bid.unwrap_or_default() - tick(),
            quantity: LOT,
        },
    ];
    assert_eq!(
        c.round_trip_inr,
        Some(charges(&fills, &cfg().costs).unwrap().total().unwrap())
    );
    let gain = cfg_d.target_frac * entry * lot;
    assert_eq!(c.expected_gain_inr, Some(gain));
    assert!(c.cost_share.is_some_and(|s| s <= cfg_d.cost_share_max));
}

#[test]
fn contract_one_step_itm_put_on_a_down_break() {
    let c = build_with(
        Some(&view_of(&o2_snapshot())),
        Knobs {
            direction: Direction::Down,
            ..Knobs::default()
        },
    );
    assert_eq!(c.legs.len(), 1);
    let lg = &c.legs[0];
    assert_eq!(
        (lg.role, lg.strike, lg.option_type),
        (LegRole::LongPut, Decimal::from(25100), OptionType::Pe)
    );
}

#[test]
fn contract_delta_outside_the_band_rejects() {
    let deep = DirectionalConfig {
        itm_steps: 8,
        ..dc()
    };
    let c = build_with(
        Some(&view_of(&o2_snapshot())),
        Knobs {
            config: deep,
            ..Knobs::default()
        },
    );
    assert_eq!(c.rejection, Some(Rejection::RejectedDelta));
}

#[test]
fn contract_the_premium_cap() {
    let book = || SleeveBook {
        sleeve_capital_inr: Decimal::from(2_000_000),
        ..SleeveBook::default()
    };
    let rich = build_with(
        Some(&view_of(&o2_snapshot())),
        Knobs {
            book: book(),
            ..Knobs::default()
        },
    );
    assert!(rich.lots == 2 && rich.half_size);
    let capped = DirectionalConfig {
        premium_cap_pct: dec("0.5"),
        ..dc()
    };
    let c = build_with(
        Some(&view_of(&o2_snapshot())),
        Knobs {
            book: book(),
            config: capped,
            ..Knobs::default()
        },
    );
    assert_eq!(c.rejection, Some(Rejection::RejectedPremiumCap));
}

#[test]
fn contract_illiquid_at_the_quantity_rejects() {
    let c = build(&view_of(&o2_snapshot_with(10, 0.13)));
    assert_eq!(c.rejection, Some(Rejection::RejectedIlliquid));
}

#[test]
fn contract_paused_and_no_chain() {
    let paused = build_with(
        Some(&view_of(&o2_snapshot())),
        Knobs {
            paused: true,
            ..Knobs::default()
        },
    );
    assert_eq!(paused.rejection, Some(Rejection::RejectedPaused));
    assert_eq!(
        build_with(None, Knobs::default()).rejection,
        Some(Rejection::RejectedNoChain)
    );
}

// --- exits (04 §4.5) ---

fn entered() -> DateTime<FixedOffset> {
    at(o2_up_break(), 10, 6)
}

struct Tick {
    bid: &'static str,
    minutes: i64,
    clock: Option<DateTime<FixedOffset>>,
    invalidated: bool,
    stale: bool,
    loss: &'static str,
}

fn bid(bid: &'static str) -> Tick {
    Tick {
        bid,
        minutes: 5,
        clock: None,
        invalidated: false,
        stale: false,
        loss: "0",
    }
}

fn decide(t: Tick) -> Option<LongExit> {
    let c = cfg();
    directional::exit_decision(
        &LongTick {
            entry: Decimal::from(100),
            bid: dec(t.bid),
            entered_at: entered(),
            now: t
                .clock
                .unwrap_or_else(|| entered() + TimeDelta::minutes(t.minutes)),
            is_invalidated: t.invalidated,
            stale: t.stale,
            marked_loss_inr: dec(t.loss),
            risk_budget_inr: Decimal::from(2000),
            manual: false,
        },
        &c.directional,
        &c,
    )
    .unwrap()
}

#[test]
fn exits_stop_at_minus_30_target_at_plus_60() {
    let cases = [
        ("70", Some(LongExit::Stop)),
        ("70.05", None),
        ("159.95", None),
        ("160", Some(LongExit::Target)),
    ];
    for (b, expected) in cases {
        assert_eq!(decide(bid(b)), expected, "bid={b}");
    }
}

#[test]
fn exits_the_time_stop_at_45_minutes_below_plus_10() {
    assert_eq!(
        decide(Tick {
            minutes: 45,
            ..bid("109.95")
        }),
        Some(LongExit::TimeStop)
    );
    assert_eq!(
        decide(Tick {
            minutes: 45,
            ..bid("110")
        }),
        None
    );
    assert_eq!(
        decide(Tick {
            minutes: 44,
            ..bid("100")
        }),
        None
    );
}

#[test]
fn exits_hard_exit_at_1500() {
    let t = Tick {
        clock: Some(at(o2_up_break(), 15, 0)),
        ..bid("100")
    };
    assert_eq!(decide(t), Some(LongExit::HardExit));
}

#[test]
fn exits_invalidation_by_a_close_back_inside_the_range() {
    let (h, l) = (Decimal::from(25032), Decimal::from(25008));
    assert!(directional::invalidated(
        Some(dec("25031.95")),
        Direction::Up,
        h,
        l
    ));
    assert!(!directional::invalidated(
        Some(Decimal::from(25032)),
        Direction::Up,
        h,
        l
    ));
    assert!(directional::invalidated(
        Some(dec("25008.05")),
        Direction::Down,
        h,
        l
    ));
    let t = Tick {
        invalidated: true,
        ..bid("120")
    };
    assert_eq!(decide(t), Some(LongExit::Invalidated));
}

#[test]
fn exits_precedence() {
    let t = Tick {
        clock: Some(at(o2_up_break(), 15, 0)),
        ..bid("60")
    };
    assert_eq!(decide(t), Some(LongExit::HardExit));
    let t = Tick {
        invalidated: true,
        ..bid("60")
    };
    assert_eq!(decide(t), Some(LongExit::Stop));
    let t = Tick {
        invalidated: true,
        ..bid("170")
    };
    assert_eq!(decide(t), Some(LongExit::Invalidated));
    let t = Tick {
        minutes: 50,
        ..bid("170")
    };
    assert_eq!(decide(t), Some(LongExit::Target));
}

#[test]
fn exits_no_target_on_a_stale_mark_but_the_stop_holds() {
    let t = Tick {
        stale: true,
        ..bid("170")
    };
    assert_eq!(decide(t), None);
    let t = Tick {
        stale: true,
        ..bid("60")
    };
    assert_eq!(decide(t), Some(LongExit::Stop));
}

#[test]
fn exits_a_budget_breach_is_a_stop() {
    let t = Tick {
        loss: "2000.01",
        ..bid("95")
    };
    assert_eq!(decide(t), Some(LongExit::Stop));
}
