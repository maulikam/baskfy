//! Port of `tests/test_options_plan_o2.py` (OP7 — the O2 plan builder).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use std::collections::{BTreeMap, BTreeSet};

use chrono::{DateTime, Datelike, FixedOffset, NaiveTime, TimeDelta, Weekday};
use rust_decimal::Decimal;

use baskfy_core::options::bars::{Bar, closed};
use baskfy_core::options::config::{
    Mode, OptionType, OptionsCeilings, OptionsConfig, Side, SizingMode, Sleeve,
};
use baskfy_core::options::execution::LegRole;
use baskfy_core::options::num;
use baskfy_core::options::plan::{PlanState, STALE_CHAIN, Verdict, plan_id_for};
use baskfy_core::options::plan_o2::{
    BEFORE_ENTRY_WINDOW, ENTRY_WINDOW_CLOSED, NO_DECISION_SNAPSHOT, NO_TRIGGER, NO_TRIGGER_YET,
    O2Decision, O2Outcome, RANGE_NOT_SETTLED, decide_o2, decision_minute_for, finalize_o2,
    wanted_minutes,
};
use baskfy_core::options::scan::{
    DayContext, MarketDay, Numbers, ScanState, SleeveContext, Snapshot, SnapshotBook, Value,
    scan_all,
};
use baskfy_core::options::structures::{Direction, SleeveBook, Structure};

use options_common::{
    LOT, at, bars_from_closes, carry_forward, d, dec, expiries_fixture, falling_closes, flat,
    ist_dt, market, market_with, o2_counter, o2_counter_bars, o2_tuesday, o2_up_break,
    o2_up_break_bars, prev_close, scan_snapshot, skew, to_minutes, trend_weekly_bars, zigzag,
};

fn cfg() -> OptionsConfig {
    OptionsConfig::default()
}

fn ceil() -> OptionsCeilings {
    OptionsCeilings::default()
}

fn day() -> chrono::NaiveDate {
    o2_up_break()
}

fn up() -> MarketDay {
    market(day(), o2_up_break_bars())
}

/// The trigger bar closes at 10:04; the plan prices from the 10:05 chain.
fn trigger_minute() -> DateTime<FixedOffset> {
    at(day(), 10, 5)
}

fn now() -> DateTime<FixedOffset> {
    trigger_minute() + TimeDelta::seconds(45)
}

fn s(v: &str) -> String {
    v.to_owned()
}

fn strs(v: &[&str]) -> Vec<String> {
    v.iter().map(|x| (*x).to_owned()).collect()
}

fn vstr(v: &str) -> Value {
    Value::Str(s(v))
}

fn chain(bars: &[Bar], minute: DateTime<FixedOffset>) -> Snapshot {
    let spot = closed(bars, &minute).last().unwrap().close;
    let expiries: Vec<_> = expiries_fixture()
        .into_iter()
        .filter(|e| *e >= minute.date_naive())
        .take(2)
        .collect();
    let forwards: BTreeMap<_, _> = expiries
        .iter()
        .map(|e| (*e, carry_forward(spot, minute, *e)))
        .collect();
    scan_snapshot(minute, spot, &expiries, &skew(0.30, 0.33), Some(&forwards))
}

fn snap() -> Snapshot {
    chain(&o2_up_break_bars(), trigger_minute())
}

struct Knobs {
    m: MarketDay,
    snap: Option<Snapshot>,
    minute: Option<DateTime<FixedOffset>>,
    now: DateTime<FixedOffset>,
    context: DayContext,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
}

impl Default for Knobs {
    fn default() -> Self {
        Self {
            m: up(),
            snap: Some(snap()),
            minute: Some(trigger_minute()),
            now: now(),
            context: DayContext::default(),
            options: cfg(),
            ceilings: ceil(),
        }
    }
}

fn decide_k(k: Knobs) -> O2Decision {
    let loaded = match k.minute {
        Some(minute) => BTreeMap::from([(Some(minute), k.snap)]),
        None => BTreeMap::new(),
    };
    let mut book = SnapshotBook::new(loaded);
    decide_o2(&k.m, &k.context, &mut book, &k.now, &k.options, &k.ceilings).unwrap()
}

fn decide() -> O2Decision {
    decide_k(Knobs::default())
}

fn finalize_with(decision: &O2Decision, mode: Mode, options: &OptionsConfig) -> O2Outcome {
    finalize_o2(decision, 7, mode, options).unwrap()
}

fn finalize(decision: &O2Decision) -> O2Outcome {
    finalize_with(decision, Mode::Paper, &cfg())
}

fn booked(book: SleeveBook, paused: bool) -> DayContext {
    DayContext {
        sleeves: BTreeMap::from([(
            Sleeve::O2,
            SleeveContext {
                book,
                paused,
                ..SleeveContext::default()
            },
        )]),
        ..DayContext::default()
    }
}

fn lot() -> Decimal {
    Decimal::from(LOT)
}

// --- the fixture morning, to the rupee ---

#[test]
fn the_plan_to_the_rupee() {
    let outcome = finalize(&decide());
    assert_eq!(outcome.state, PlanState::Planned);
    let plan = outcome.plan.unwrap();
    let [leg] = plan.legs.as_slice() else {
        panic!("one leg");
    };
    assert_eq!(
        (leg.seq, leg.role, leg.side),
        (1, LegRole::LongCall, Side::Buy)
    );
    assert_eq!(
        (leg.strike, leg.option_type),
        (Decimal::from(25000), OptionType::Ce)
    );
    assert_eq!(leg.tradingsymbol, "NIFTY26102025000CE");
    assert_eq!(plan.expiry, d(2026, 10, 20));
    assert_eq!(plan.structure, Structure::LongOption);
    assert_eq!(plan.direction, Direction::Up);
    assert_eq!(
        (leg.bid, leg.ask, leg.limit_price),
        (Some(dec("201.65")), Some(dec("203.70")), dec("203.75"))
    );
    let delta = num::from_float(leg.delta.unwrap()).unwrap();
    assert!(cfg().directional.delta_min <= delta && delta <= cfg().directional.delta_max);
    assert_eq!(plan.debit_points, dec("203.75"));
    assert_eq!((plan.lots, plan.lot_size, plan.quantity()), (1, LOT, LOT));
    assert_eq!(plan.sizing_mode, SizingMode::PaperOneLot);
    assert_eq!(plan.mode, Mode::Paper);
    assert_eq!(plan.max_loss_inr, dec("3973.13"));
    assert_eq!(plan.risk_per_lot_inr, dec("4273.13"));
    assert_eq!(plan.risk_per_lot_inr, plan.risk_budget_inr);
    assert_eq!(plan.premium_inr, dec("203.75") * lot());
    assert_eq!(plan.premium_inr, dec("13243.75"));
    assert_eq!(plan.gap_through_inr, plan.premium_inr);
    assert_eq!(plan.profit_target_inr, dec("7946.25"));
    assert_eq!(plan.stop_inr, plan.max_loss_inr);
    assert_eq!(plan.expires_at, now() + TimeDelta::minutes(30));
    assert_eq!(plan.entry_window_end, ist_dt(2026, 10, 19, 13, 30, 0));
    assert_eq!(plan.as_of_minute, Some(trigger_minute()));
    assert!(plan.warnings.is_empty());
}

#[test]
fn the_costs_to_the_paisa_from_the_verified_rates() {
    let plan = finalize(&decide()).plan.unwrap();
    let buys = dec("203.75") * lot();
    let sells = dec("201.60") * lot();
    assert_eq!((buys, sells), (dec("13243.75"), dec("13104.00")));
    let c = &plan.costs;
    assert_eq!(c.orders, 2);
    assert_eq!(c.brokerage, dec("40.00"));
    assert_eq!(c.stt, dec("19.66"));
    assert_eq!(c.exchange_txn, dec("9.36"));
    assert_eq!(c.sebi, dec("0.03"));
    assert_eq!(c.ipft, dec("0.00"));
    assert_eq!(c.stamp, dec("0.40"));
    assert_eq!(c.gst, dec("8.89"));
    assert_eq!(c.total().unwrap(), plan.expected_cost_inr);
    assert_eq!(plan.expected_cost_inr, dec("78.34"));
    assert_eq!(plan.cost_share, dec("0.0099"));
    assert!(plan.cost_share <= cfg().directional.cost_share_max);
}

#[test]
fn the_exits_are_04_4_5() {
    let plan = finalize(&decide()).plan.unwrap();
    let e = &plan.exits;
    assert_eq!(e.stop_price, dec("142.63"));
    assert_eq!(e.target_price, dec("326.00"));
    assert_eq!(e.time_stop_minutes, 45);
    assert_eq!(e.time_stop_min_price, dec("224.13"));
    assert_eq!(e.invalidation_level, dec("25032"));
    assert_eq!(e.hard_exit_at, ist_dt(2026, 10, 19, 15, 0, 0));
}

#[test]
fn the_plan_is_the_scans_candidate() {
    let decision = decide();
    let mut book = SnapshotBook::new(BTreeMap::from([
        (Some(trigger_minute()), Some(snap())),
        (None, Some(snap())),
    ]));
    let rows = scan_all(
        &up(),
        &DayContext::default(),
        &now(),
        &mut book,
        &cfg(),
        &ceil(),
    )
    .unwrap();
    let row = rows.iter().find(|r| r.sleeve == Sleeve::O2).unwrap();
    assert_eq!(row.state, ScanState::Triggered);
    let [candidate] = row.candidates.as_slice() else {
        panic!("one candidate");
    };
    assert_eq!(decision.candidate.as_ref(), Some(candidate));
    let costs = decision.costs.as_ref().unwrap();
    assert_eq!(Some(costs.total().unwrap()), candidate.round_trip_inr);
}

#[test]
fn the_only_leg_is_long_and_the_book_is_never_naked() {
    let plan = finalize(&decide()).plan.unwrap();
    let roles: Vec<_> = plan.legs.iter().map(|lg| lg.role).collect();
    assert_eq!(roles, vec![LegRole::LongCall]);
    assert!(plan.legs.iter().all(|lg| lg.side == Side::Buy));
}

#[test]
fn plan_id_is_deterministic_and_a_valid_client_id_half() {
    let a = finalize(&decide()).plan.unwrap();
    let b = finalize(&decide()).plan.unwrap();
    assert_eq!(a.plan_id, b.plan_id);
    assert!(a.plan_id.starts_with("O2-20261019-"));
    assert!(!a.plan_id.contains(':') && !a.plan_id.chars().any(char::is_whitespace));
    assert_ne!(plan_id_for(8, &decide()), a.plan_id);
}

#[test]
fn expires_at_is_min_of_thirty_minutes_and_the_window() {
    let mut narrow = cfg();
    narrow.directional.entry_window_end = NaiveTime::from_hms_opt(10, 20, 0).unwrap();
    let decision = decide_k(Knobs {
        options: narrow.clone(),
        ..Knobs::default()
    });
    let plan = finalize_with(&decision, Mode::Paper, &narrow).plan.unwrap();
    assert_eq!(plan.expires_at, ist_dt(2026, 10, 19, 10, 20, 0));
    assert_eq!(plan.expires_at, plan.entry_window_end);
    assert!(plan.entry_window_end < now() + TimeDelta::minutes(30));
}

#[test]
fn the_builder_asks_for_exactly_the_trigger_minute() {
    assert_eq!(
        wanted_minutes(&up(), &DayContext::default(), &now(), &cfg(), &ceil()).unwrap(),
        BTreeSet::from([Some(trigger_minute())])
    );
    let decision = decide();
    let trigger = decision.trigger.unwrap();
    assert_eq!(decision_minute_for(&trigger, day()), trigger_minute());
}

// --- the contract ---

#[test]
fn the_monday_before_an_expiry_uses_tuesdays_contract() {
    let decision = decide();
    assert_eq!(day().weekday(), Weekday::Mon);
    assert_eq!(decision.expiry, Some(d(2026, 10, 20)));
}

#[test]
fn an_expiry_tuesday_uses_next_weeks_contract() {
    let bars = trend_weekly_bars();
    let m = market(o2_tuesday(), bars.clone());
    let minute = at(o2_tuesday(), 9, 35);
    let plan = finalize(&decide_k(Knobs {
        m,
        snap: Some(chain(&bars, minute)),
        minute: Some(minute),
        now: minute + TimeDelta::seconds(45),
        ..Knobs::default()
    }))
    .plan
    .unwrap();
    assert_eq!(d(2026, 10, 20), o2_tuesday());
    assert_eq!(plan.expiry, d(2026, 10, 27));
    assert_eq!(plan.legs[0].tradingsymbol, "NIFTY26102725000CE");
    assert_eq!(plan.debit_points, dec("471.95"));
}

#[test]
fn a_down_trend_buys_a_put_one_step_above_the_money() {
    let bars = o2_counter_bars();
    let m = market_with(
        o2_counter(),
        bars.clone(),
        Some(falling_closes()),
        Some(Decimal::from(14)),
        true,
    );
    let minute = at(o2_counter(), 10, 10);
    let decision = decide_k(Knobs {
        m,
        snap: Some(chain(&bars, minute)),
        minute: Some(minute),
        now: minute + TimeDelta::seconds(45),
        ..Knobs::default()
    });
    let plan = finalize(&decision).plan.unwrap();
    assert_eq!(plan.direction, Direction::Down);
    let [leg] = plan.legs.as_slice() else {
        panic!("one leg");
    };
    assert_eq!(
        (leg.role, leg.option_type, leg.strike),
        (LegRole::LongPut, OptionType::Pe, Decimal::from(25050))
    );
    assert_eq!(plan.exits.invalidation_level, dec("25008"));
}

// --- the day filters ---

fn skips(decision: &O2Decision, reason: &str) {
    assert_eq!(decision.state, PlanState::Skipped);
    assert_eq!(decision.reasons, strs(&[reason]));
    assert_eq!(decision.verdict, Some(Verdict::Skip));
    assert!(finalize(decision).plan.is_none());
}

fn no_minute(m: MarketDay) -> O2Decision {
    decide_k(Knobs {
        m,
        minute: None,
        ..Knobs::default()
    })
}

#[test]
fn a_gap_beyond_gap_max_plans_nothing_with_the_reason() {
    let opened = dec("25300");
    let bars = bars_from_closes(
        day(),
        opened,
        &zigzag(opened, dec("10"), to_minutes(15, 30)),
    );
    let decision = no_minute(market(day(), bars));
    skips(&decision, "GAP_TOO_BIG");
    assert_eq!(decision.numbers.get("gap_pct"), Some(&vstr("1.2000")));
}

#[test]
fn a_vix_above_vix_max_plans_nothing_with_the_reason() {
    let decision = no_minute(market_with(
        day(),
        o2_up_break_bars(),
        None,
        Some(dec("23")),
        true,
    ));
    skips(&decision, "VIX_TOO_HIGH");
    assert_eq!(decision.numbers.get("vix"), Some(&vstr("23.00")));
    assert_eq!(decision.numbers.get("vix_max"), Some(&vstr("22")));
}

#[test]
fn an_opening_range_wider_than_or_max_is_refused() {
    let bars = bars_from_closes(
        day(),
        dec("25020"),
        &zigzag(dec("25150"), dec("120"), to_minutes(15, 30)),
    );
    let decision = no_minute(market(day(), bars));
    skips(&decision, "RANGE_TOO_WIDE");
    assert_eq!(decision.numbers.get("or_pct"), Some(&vstr("1.0160")));
}

#[test]
fn a_flat_trend_is_refused() {
    let flat_closes = vec![prev_close(); 60];
    let decision = no_minute(market_with(
        day(),
        o2_up_break_bars(),
        Some(flat_closes),
        Some(Decimal::from(14)),
        true,
    ));
    skips(&decision, "TREND_FLAT");
}

#[test]
fn no_vix_is_refused_rather_than_assumed() {
    skips(
        &no_minute(market_with(day(), o2_up_break_bars(), None, None, true)),
        "VIX_UNKNOWN",
    );
}

#[test]
fn an_event_day_is_skipped_never_shifted() {
    let decision = decide_k(Knobs {
        context: DayContext {
            event_days: vec![day()],
            ..DayContext::default()
        },
        ..Knobs::default()
    });
    skips(&decision, "EVENT_DAY");
}

#[test]
fn a_holiday_has_no_session_at_all() {
    let decision = no_minute(market_with(
        day(),
        o2_up_break_bars(),
        None,
        Some(Decimal::from(14)),
        false,
    ));
    assert_eq!(decision.state, PlanState::NoSession);
    assert!(!decision.writes_session());
    assert_eq!(decision.reasons, strs(&["NOT_TRADING_DAY"]));
}

// --- the trigger ---

#[test]
fn a_counter_trend_break_plans_nothing_and_is_recorded() {
    let m = market(o2_counter(), o2_counter_bars());
    let armed = decide_k(Knobs {
        m,
        minute: None,
        now: at(o2_counter(), 10, 15),
        ..Knobs::default()
    });
    assert_eq!(armed.state, PlanState::NotReady);
    assert_eq!(armed.reasons, strs(&[NO_TRIGGER_YET]));
    let Some(Value::List(breaks)) = armed.numbers.get("counter_trend_breaks") else {
        panic!("counter_trend_breaks is a list");
    };
    assert!(!breaks.is_empty());
    assert_eq!(
        breaks[0],
        Numbers::of(vec![
            ("close_time", vstr("10:09")),
            ("close", vstr("24990.00")),
            ("direction", vstr("DOWN")),
        ])
    );
    assert_eq!(armed.numbers.get("direction"), Some(&vstr("UP")));
    assert!(finalize(&armed).plan.is_none());
}

#[test]
fn the_window_closing_with_no_trigger_is_a_skipped_session() {
    let decision = decide_k(Knobs {
        m: market(o2_counter(), o2_counter_bars()),
        minute: None,
        now: at(o2_counter(), 13, 33),
        ..Knobs::default()
    });
    assert_eq!(decision.state, PlanState::Skipped);
    assert_eq!(decision.reasons, strs(&[NO_TRIGGER]));
    assert_eq!(decision.verdict, Some(Verdict::Skip));
    assert!(decision.writes_session());
    assert!(finalize(&decision).plan.is_none());
}

#[test]
fn one_trigger_a_day_whatever_the_tape_does_later() {
    let late = decide_k(Knobs {
        now: at(day(), 11, 0),
        ..Knobs::default()
    });
    let trigger = late.trigger.as_ref().unwrap();
    assert_eq!(
        trigger.close_time,
        NaiveTime::from_hms_opt(10, 4, 0).unwrap()
    );
    assert_eq!(late.as_of_minute, Some(trigger_minute()));
}

#[test]
fn a_trigger_after_the_entry_window_is_never_planned() {
    let decision = decide_k(Knobs {
        minute: None,
        now: at(day(), 13, 30) + TimeDelta::seconds(45),
        ..Knobs::default()
    });
    assert_eq!(decision.state, PlanState::WindowClosed);
    assert_eq!(decision.reasons, strs(&[ENTRY_WINDOW_CLOSED]));
    assert!(!decision.writes_session());
}

// --- the delta ---

fn fast_chain() -> Snapshot {
    let expiries: Vec<_> = expiries_fixture()
        .into_iter()
        .filter(|e| *e >= day())
        .take(2)
        .collect();
    let forwards: BTreeMap<_, _> = expiries.iter().map(|e| (*e, dec("25000"))).collect();
    scan_snapshot(
        trigger_minute(),
        dec("25150"),
        &expiries,
        &flat(0.50),
        Some(&forwards),
    )
}

#[test]
fn an_out_of_band_delta_is_rejected_with_the_number() {
    let decision = decide_k(Knobs {
        snap: Some(fast_chain()),
        ..Knobs::default()
    });
    assert_eq!(decision.state, PlanState::Skipped);
    assert_eq!(decision.reasons, strs(&["REJECTED_DELTA"]));
    assert_eq!(decision.verdict, Some(Verdict::Trade));
    let candidate = decision.candidate.as_ref().unwrap();
    assert_eq!(candidate.legs[0].strike, Decimal::from(25100));
    let delta = candidate.legs[0].delta.unwrap();
    assert_eq!(format!("{delta:.4}"), "0.4508");
    assert!(candidate.message.contains("outside [0.50, 0.75]"));
    assert!(finalize(&decision).plan.is_none());
}

// --- sizing and its caps ---

#[test]
fn a_paused_sleeve_is_rejected_paused() {
    let decision = decide_k(Knobs {
        context: booked(SleeveBook::default(), true),
        ..Knobs::default()
    });
    assert_eq!(decision.reasons, strs(&["REJECTED_PAUSED"]));
    assert_eq!(decision.state, PlanState::Skipped);
}

#[test]
fn live_with_capital_zero_is_rejected_no_sleeve_capital() {
    let decision = decide_k(Knobs {
        context: booked(
            SleeveBook {
                mode: Mode::Live,
                ..SleeveBook::default()
            },
            false,
        ),
        ..Knobs::default()
    });
    assert_eq!(decision.reasons, strs(&["REJECTED_NO_SLEEVE_CAPITAL"]));
}

#[test]
fn a_budget_too_small_for_one_lot_is_rejected_budget() {
    let book = SleeveBook {
        mode: Mode::Paper,
        sleeve_capital_inr: dec("100000"),
        ..SleeveBook::default()
    };
    let decision = decide_k(Knobs {
        context: booked(book, false),
        ..Knobs::default()
    });
    assert_eq!(decision.reasons, strs(&["REJECTED_BUDGET"]));
    let candidate = decision.candidate.unwrap();
    assert!(
        candidate
            .message
            .contains("₹250.00 / risk per lot ₹4273.12"),
        "{}",
        candidate.message
    );
}

#[test]
fn the_premium_cap_cannot_bind_under_04s_ceilings() {
    let c = cfg().directional;
    let most = ceil().risk_pct_max / c.stop_frac;
    assert_eq!(most, dec("1.0") / dec("0.30"));
    assert!(most < c.premium_cap_pct);
}

#[test]
fn the_premium_cap_refuses_when_a_ceiling_allows_it_to_bind() {
    let ceilings = OptionsCeilings {
        risk_pct_max: dec("5"),
        ..ceil()
    };
    let book = SleeveBook {
        mode: Mode::Paper,
        sleeve_capital_inr: dec("100000"),
        risk_per_trade_pct: Some(dec("5")),
        real_journal_rows: 5,
        ..SleeveBook::default()
    };
    let decision = decide_k(Knobs {
        context: booked(book, false),
        ceilings,
        ..Knobs::default()
    });
    assert_eq!(decision.reasons, strs(&["REJECTED_PREMIUM_CAP"]));
    let candidate = decision.candidate.unwrap();
    assert_eq!(candidate.message, "1 lots * ₹203.75 * 65 > 10 % of capital");
}

// --- the clock ---

#[test]
fn outside_the_window_nothing_is_written() {
    for ((h, m, sec), state, reason) in [
        ((9, 29, 50), PlanState::NotReady, BEFORE_ENTRY_WINDOW),
        ((13, 30, 45), PlanState::WindowClosed, ENTRY_WINDOW_CLOSED),
    ] {
        let decision = decide_k(Knobs {
            minute: None,
            now: ist_dt(2026, 10, 19, h, m, sec),
            ..Knobs::default()
        });
        assert_eq!(
            (decision.state, decision.reasons.clone()),
            (state, strs(&[reason]))
        );
        assert!(!decision.writes_session());
    }
}

#[test]
fn an_unsettled_opening_range_waits() {
    let mut partial = up();
    partial.bars.retain(|b| b.ts < at(day(), 9, 29));
    let decision = decide_k(Knobs {
        m: partial,
        minute: None,
        now: at(day(), 9, 31),
        ..Knobs::default()
    });
    assert_eq!(
        (decision.state, decision.reasons),
        (PlanState::NotReady, strs(&[RANGE_NOT_SETTLED]))
    );
}

#[test]
fn no_trigger_snapshot_yet_waits() {
    let decision = decide_k(Knobs {
        snap: None,
        ..Knobs::default()
    });
    assert_eq!(
        (decision.state, decision.reasons),
        (PlanState::NotReady, strs(&[NO_DECISION_SNAPSHOT]))
    );
}

#[test]
fn a_trigger_snapshot_older_than_the_stale_limit_is_not_planned() {
    let late = at(day(), 10, 7) + TimeDelta::seconds(1);
    let decision = decide_k(Knobs {
        now: late,
        ..Knobs::default()
    });
    assert_eq!(
        (decision.state, decision.reasons.clone()),
        (PlanState::Skipped, strs(&[STALE_CHAIN]))
    );
    assert!(finalize(&decision).plan.is_none());
}
