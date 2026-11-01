//! Port of `tests/test_options_plan_o3.py` (OP8 — the O3 plan builder).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime, TimeDelta};
use rust_decimal::Decimal;

use baskfy_core::decimal::Rounding;
use baskfy_core::error::CoreError;
use baskfy_core::options::bars::{Bar, closed};
use baskfy_core::options::config::{
    Mode, OptionType, OptionsCeilings, OptionsConfig, Side, Sleeve,
};
use baskfy_core::options::execution::LegRole;
use baskfy_core::options::num;
use baskfy_core::options::plan::{Finalize, MarginQuote, PlanState, STALE_CHAIN, Verdict};
use baskfy_core::options::plan_o3::{
    BEFORE_PLAN_TIME, ENTRY_WINDOW_CLOSED, NO_DECISION_SNAPSHOT, NO_TRIGGER, NO_TRIGGER_YET,
    O3_SLEEVES, O3B_HOLDS, O3Decision, O3Outcome, O3Plan, SETUP_DISABLED, WINDOW_NOT_SETTLED,
    decide_o3, finalize_o3, wanted_minutes,
};
use baskfy_core::options::scan::{
    DayContext, MarketDay, ScanState, SleeveContext, Snapshot, SnapshotBook, Value, scan_all,
};
use baskfy_core::options::session::SessionState;
use baskfy_core::options::structures::{Direction, Rejection, Structure};

use options_common::{
    LOT, at, bars_from_closes, carry_forward, dec, expiries_fixture, flat, gap_hold, gap_hold_bars,
    market, o2_up_break, ramp, scan_snapshot, to_minutes, trend_weekly, trend_weekly_bars, zigzag,
};

fn cfg() -> OptionsConfig {
    OptionsConfig::default()
}

fn ceil() -> OptionsCeilings {
    OptionsCeilings::default()
}

fn width() -> Decimal {
    Decimal::from(100)
}

fn strs(v: &[&str]) -> Vec<String> {
    v.iter().map(|x| (*x).to_owned()).collect()
}

/// The two nearest expiries, cost-of-carry forwards moved by `forward_shift`.
fn chain_shift(bars: &[Bar], minute: DateTime<FixedOffset>, forward_shift: Decimal) -> Snapshot {
    let spot = closed(bars, &minute).last().unwrap().close;
    let expiries: Vec<_> = expiries_fixture()
        .into_iter()
        .filter(|e| *e >= minute.date_naive())
        .take(2)
        .collect();
    let forwards: BTreeMap<_, _> = expiries
        .iter()
        .map(|e| (*e, carry_forward(spot, minute, *e) + forward_shift))
        .collect();
    scan_snapshot(minute, spot, &expiries, &flat(0.14), Some(&forwards))
}

fn chain(bars: &[Bar], minute: DateTime<FixedOffset>) -> Snapshot {
    chain_shift(bars, minute, Decimal::ZERO)
}

fn plus20(day: NaiveDate, hh: u32, mm: u32) -> DateTime<FixedOffset> {
    at(day, hh, mm) + TimeDelta::seconds(20)
}

struct Knobs {
    context: DayContext,
    options: OptionsConfig,
    forward_shift: Decimal,
}

impl Default for Knobs {
    fn default() -> Self {
        Self {
            context: DayContext::default(),
            options: cfg(),
            forward_shift: Decimal::ZERO,
        }
    }
}

/// The worker's two phases: learn the minute wanted, load it from the fixture, decide.
fn decide_k(sleeve: Sleeve, m: &MarketDay, now: DateTime<FixedOffset>, k: Knobs) -> O3Decision {
    let mut loaded = BTreeMap::new();
    for minute in wanted_minutes(sleeve, m, &k.context, &now, &k.options, &ceil())
        .unwrap()
        .into_iter()
        .flatten()
    {
        loaded.insert(
            Some(minute),
            Some(chain_shift(&m.bars, minute, k.forward_shift)),
        );
    }
    let mut book = SnapshotBook::new(loaded);
    decide_o3(sleeve, m, &k.context, &mut book, &now, &k.options, &ceil()).unwrap()
}

fn decide(sleeve: Sleeve, m: &MarketDay, now: DateTime<FixedOffset>) -> O3Decision {
    decide_k(sleeve, m, now, Knobs::default())
}

fn decide_ctx(
    sleeve: Sleeve,
    m: &MarketDay,
    now: DateTime<FixedOffset>,
    context: DayContext,
) -> O3Decision {
    decide_k(
        sleeve,
        m,
        now,
        Knobs {
            context,
            ..Knobs::default()
        },
    )
}

fn finalize_full(
    decision: &O3Decision,
    margin: Option<MarginQuote>,
    mode: Mode,
    pool: Decimal,
    in_use: Decimal,
) -> O3Outcome {
    finalize_o3(
        decision,
        margin.as_ref(),
        &Finalize {
            user_id: 7,
            mode,
            margin_pool_inr: pool,
            margin_in_use_inr: in_use,
        },
        &cfg(),
    )
    .unwrap()
}

fn finalize(decision: &O3Decision) -> O3Outcome {
    finalize_full(decision, None, Mode::Paper, Decimal::ZERO, Decimal::ZERO)
}

fn gap_down_bars() -> Vec<Bar> {
    let closes = zigzag(dec("24800"), dec("10"), to_minutes(15, 30));
    bars_from_closes(gap_hold(), dec("24800"), &closes)
}

fn trend_down_bars() -> Vec<Bar> {
    let mut first = ramp(dec("25000"), dec("-3"), to_minutes(10, 15));
    let rest = ramp(
        *first.last().unwrap(),
        dec("-6"),
        to_minutes(15, 30) - first.len(),
    );
    first.extend(rest);
    bars_from_closes(trend_weekly(), dec("25000"), &first)
}

fn gap_then_break_bars() -> Vec<Bar> {
    let mut first = ramp(dec("25200"), dec("0.5"), to_minutes(10, 15));
    let rest = ramp(
        *first.last().unwrap(),
        dec("6"),
        to_minutes(15, 30) - first.len(),
    );
    first.extend(rest);
    bars_from_closes(gap_hold(), dec("25200"), &first)
}

fn gap_up() -> MarketDay {
    market(gap_hold(), gap_hold_bars())
}

fn gap_down() -> MarketDay {
    market(gap_hold(), gap_down_bars())
}

fn trend_up() -> MarketDay {
    market(trend_weekly(), trend_weekly_bars())
}

fn trend_down() -> MarketDay {
    market(trend_weekly(), trend_down_bars())
}

fn both() -> MarketDay {
    market(gap_hold(), gap_then_break_bars())
}

fn o3b_now() -> DateTime<FixedOffset> {
    plus20(gap_hold(), 9, 46)
}

fn o3a_now() -> DateTime<FixedOffset> {
    plus20(trend_weekly(), 10, 21)
}

fn planned(outcome: O3Outcome) -> O3Plan {
    assert_eq!(outcome.state, PlanState::Planned, "{:?}", outcome.reasons);
    outcome.plan.unwrap()
}

fn plan_of(sleeve: Sleeve, m: &MarketDay, now: DateTime<FixedOffset>) -> O3Plan {
    planned(finalize(&decide(sleeve, m, now)))
}

// --- each setup and each direction ---

#[test]
fn the_strikes_and_the_debit_are_04_5() {
    let cases = [
        (
            Sleeve::O3b,
            gap_up(),
            o3b_now(),
            Direction::Up,
            at(gap_hold(), 9, 45),
        ),
        (
            Sleeve::O3b,
            gap_down(),
            o3b_now(),
            Direction::Down,
            at(gap_hold(), 9, 45),
        ),
        (
            Sleeve::O3a,
            trend_up(),
            o3a_now(),
            Direction::Up,
            at(trend_weekly(), 10, 20),
        ),
        (
            Sleeve::O3a,
            trend_down(),
            o3a_now(),
            Direction::Down,
            at(trend_weekly(), 10, 20),
        ),
    ];
    for (sleeve, m, now, direction, decision_minute) in cases {
        let plan = plan_of(sleeve, &m, now);
        let snap = chain(&m.bars, decision_minute);
        let spot = snap.spot.unwrap();
        let fifty = Decimal::from(50);
        let atm = num::quantize(spot / fifty, 0, Rounding::HalfEven).unwrap() * fifty;
        let right = if direction == Direction::Up {
            OptionType::Ce
        } else {
            OptionType::Pe
        };
        let k_short = if direction == Direction::Up {
            atm + width()
        } else {
            atm - width()
        };
        let [long_leg, short_leg] = plan.legs.as_slice() else {
            panic!("two legs");
        };
        assert_eq!((long_leg.strike, short_leg.strike), (atm, k_short));
        assert_eq!(long_leg.option_type, right);
        assert_eq!(short_leg.option_type, right);
        assert_eq!(plan.expiry, m.trade_date);
        assert_eq!(plan.direction, direction);
        assert_eq!(plan.structure, Structure::DebitSpread);
        let (ask, bid) = (long_leg.ask.unwrap(), short_leg.bid.unwrap());
        assert_eq!(plan.debit_points, ask - bid);
        assert_eq!(plan.width_points, width());
        assert_eq!(plan.as_of_minute, Some(decision_minute));
    }
}

#[test]
fn o3b_up_to_the_rupee() {
    let plan = plan_of(Sleeve::O3b, &gap_up(), o3b_now());
    let [long_leg, short_leg] = plan.legs.as_slice() else {
        panic!("two legs");
    };
    assert_eq!(
        (
            long_leg.tradingsymbol.as_str(),
            short_leg.tradingsymbol.as_str()
        ),
        ("NIFTY26101325200CE", "NIFTY26101325300CE")
    );
    assert_eq!(
        (long_leg.bid, long_leg.ask, short_leg.bid, short_leg.ask),
        (
            Some(dec("31.60")),
            Some(dec("31.95")),
            Some(dec("5.00")),
            Some(dec("5.10"))
        )
    );
    assert_eq!(plan.debit_points, dec("26.95"));
    assert_eq!((plan.lots, plan.quantity()), (1, LOT));
    assert_eq!(plan.debit_inr, plan.max_loss_inr);
    assert_eq!(plan.max_loss_inr, dec("1751.75"));
    assert_eq!(plan.risk_per_lot_inr, dec("2251.75"));
    assert_eq!(plan.profit_target_inr, dec("3448.25"));
    assert_eq!(plan.stop_inr, dec("875.88"));
}

#[test]
fn o3a_up_to_the_rupee() {
    let decision = decide(Sleeve::O3a, &trend_up(), o3a_now());
    let plan = planned(finalize(&decision));
    assert_eq!(
        decision.numbers.get("trigger_time"),
        Some(&Value::Str("10:19".to_owned()))
    );
    let [long_leg, short_leg] = plan.legs.as_slice() else {
        panic!("two legs");
    };
    assert_eq!(
        (long_leg.strike, short_leg.strike),
        (dec("25200"), dec("25300"))
    );
    assert_eq!(plan.debit_points, dec("33.55"));
    assert_eq!(plan.exits.invalidation_level, dec("25182"));
}

#[test]
fn the_plan_is_the_scans_candidate() {
    for (sleeve, m, now, minute) in [
        (Sleeve::O3b, gap_up(), o3b_now(), at(gap_hold(), 9, 45)),
        (
            Sleeve::O3a,
            trend_up(),
            o3a_now(),
            at(trend_weekly(), 10, 20),
        ),
    ] {
        let decision = decide(sleeve, &m, now);
        let mut book = SnapshotBook::new(BTreeMap::from([
            (Some(minute), Some(chain(&m.bars, minute))),
            (None, Some(chain(&m.bars, minute))),
        ]));
        let rows = scan_all(&m, &DayContext::default(), &now, &mut book, &cfg(), &ceil()).unwrap();
        let row = rows.into_iter().find(|r| r.sleeve == sleeve).unwrap();
        assert_eq!(row.state, ScanState::Triggered);
        assert_eq!(row.candidates, vec![decision.candidate.unwrap()]);
    }
}

// --- the debit cap ---

#[test]
fn a_debit_above_55_percent_of_the_width_is_rejected() {
    let decision = decide_k(
        Sleeve::O3b,
        &gap_up(),
        o3b_now(),
        Knobs {
            forward_shift: dec("80"),
            ..Knobs::default()
        },
    );
    assert_eq!(decision.state, PlanState::Skipped);
    assert_eq!(decision.reasons, strs(&[Rejection::RejectedDebit.value()]));
    let candidate = decision.candidate.as_ref().unwrap();
    assert!(candidate.points.unwrap() > dec("55"));
    assert!(finalize(&decision).plan.is_none());
}

#[test]
fn the_cap_is_the_config_fraction() {
    let mut tight = cfg();
    tight.expiry_setups.max_debit_frac = dec("0.20");
    let decision = decide_k(
        Sleeve::O3b,
        &gap_up(),
        o3b_now(),
        Knobs {
            options: tight,
            ..Knobs::default()
        },
    );
    assert_eq!(decision.reasons, strs(&[Rejection::RejectedDebit.value()]));
}

// --- one O3 a day ---

fn sleeve_ctx(sleeve: Sleeve, state: SessionState) -> DayContext {
    DayContext {
        sleeves: BTreeMap::from([(
            sleeve,
            SleeveContext {
                session_state: Some(state),
                ..SleeveContext::default()
            },
        )]),
        ..DayContext::default()
    }
}

#[test]
fn both_setups_fire_on_this_day() {
    assert_eq!(
        decide(Sleeve::O3b, &both(), o3b_now()).state,
        PlanState::Planned
    );
    let free = decide(Sleeve::O3a, &both(), plus20(gap_hold(), 10, 21));
    assert_eq!(free.state, PlanState::Planned);
}

#[test]
fn o3b_holds_the_day_so_o3a_is_slot_taken() {
    for o3b_state in [
        SessionState::Planned,
        SessionState::Confirmed,
        SessionState::Open,
        SessionState::Closed,
    ] {
        let ctx = sleeve_ctx(Sleeve::O3b, o3b_state);
        let decision = decide_ctx(Sleeve::O3a, &both(), plus20(gap_hold(), 10, 21), ctx);
        assert_eq!(decision.state, PlanState::Skipped, "{o3b_state:?}");
        assert_eq!(
            decision.reasons,
            strs(&[Rejection::RejectedSlotTaken.value(), O3B_HOLDS])
        );
    }
}

#[test]
fn a_lapsed_or_skipped_o3b_frees_o3a() {
    for o3b_state in [SessionState::Lapsed, SessionState::Skipped] {
        let ctx = sleeve_ctx(Sleeve::O3b, o3b_state);
        assert_eq!(
            decide_ctx(Sleeve::O3a, &both(), plus20(gap_hold(), 10, 21), ctx).state,
            PlanState::Planned,
            "{o3b_state:?}"
        );
    }
}

#[test]
fn o3a_never_holds_o3b() {
    let ctx = sleeve_ctx(Sleeve::O3a, SessionState::Planned);
    assert_eq!(
        decide_ctx(Sleeve::O3b, &gap_up(), o3b_now(), ctx).state,
        PlanState::Planned
    );
}

// --- the expiry-day slot ---

#[test]
fn a_confirmed_o1_makes_o3_slot_taken() {
    for holder in [Sleeve::O1w, Sleeve::O1m] {
        for (sleeve, m, now) in [
            (Sleeve::O3b, gap_up(), o3b_now()),
            (Sleeve::O3a, trend_up(), o3a_now()),
        ] {
            let ctx = DayContext {
                slot_holder: Some(holder),
                ..DayContext::default()
            };
            let decision = decide_ctx(sleeve, &m, now, ctx);
            assert_eq!(decision.state, PlanState::Skipped);
            assert_eq!(
                decision.reasons,
                strs(&[Rejection::RejectedSlotTaken.value()])
            );
            assert!(finalize(&decision).plan.is_none());
        }
    }
}

#[test]
fn holding_the_slot_itself_is_not_a_refusal() {
    let ctx = DayContext {
        slot_holder: Some(Sleeve::O3b),
        ..DayContext::default()
    };
    assert_eq!(
        decide_ctx(Sleeve::O3b, &gap_up(), o3b_now(), ctx).state,
        PlanState::Planned
    );
}

#[test]
fn an_o1_plan_that_is_only_planned_does_not_hold_the_slot() {
    let ctx = sleeve_ctx(Sleeve::O1w, SessionState::Planned);
    assert_eq!(
        decide_ctx(Sleeve::O3b, &gap_up(), o3b_now(), ctx).state,
        PlanState::Planned
    );
}

// --- the plan's shape ---

#[test]
fn long_first_then_short_never_naked() {
    for (sleeve, m, now) in [
        (Sleeve::O3b, gap_down(), o3b_now()),
        (Sleeve::O3a, trend_up(), o3a_now()),
    ] {
        let plan = plan_of(sleeve, &m, now);
        let shape: Vec<_> = plan.legs.iter().map(|lg| (lg.seq, lg.side)).collect();
        assert_eq!(shape, vec![(1, Side::Buy), (2, Side::Sell)]);
        assert!(matches!(
            plan.legs[0].role,
            LegRole::LongCall | LegRole::LongPut
        ));
        assert!(plan.legs.iter().all(|lg| lg.quantity == plan.quantity()));
    }
}

#[test]
fn o3b_lapses_at_its_window_end() {
    let plan = plan_of(Sleeve::O3b, &gap_up(), o3b_now());
    assert_eq!(plan.expires_at, at(gap_hold(), 10, 0));
    assert_eq!(plan.entry_window_end, at(gap_hold(), 10, 0));
}

#[test]
fn o3a_gets_thirty_minutes() {
    let plan = plan_of(Sleeve::O3a, &trend_up(), o3a_now());
    assert_eq!(plan.expires_at, o3a_now() + TimeDelta::minutes(30));
    assert_eq!(plan.entry_window_end, at(trend_weekly(), 13, 30));
}

#[test]
fn the_exits_are_04_5_3() {
    let up = plan_of(Sleeve::O3b, &gap_up(), o3b_now());
    let down = plan_of(Sleeve::O3a, &trend_down(), o3a_now());
    assert_eq!(up.exits.target_value, dec("80.00"));
    assert_eq!(up.exits.stop_value, dec("13.48"));
    assert_eq!(up.exits.invalidation_level, dec("25100"));
    assert_eq!(up.exits.hard_exit_at, at(gap_hold(), 14, 45));
    let Some(Value::Str(low)) = down.numbers.get("range_low") else {
        panic!("range_low is a string");
    };
    assert_eq!(down.exits.invalidation_level, dec(low));
}

#[test]
fn the_plan_id_is_deterministic_and_per_setup() {
    let a = plan_of(Sleeve::O3b, &gap_up(), o3b_now());
    let b = plan_of(Sleeve::O3b, &gap_up(), o3b_now());
    assert_eq!(a.plan_id, b.plan_id);
    assert!(a.plan_id.starts_with("O3B-20261013-") && !a.plan_id.contains(':'));
}

#[test]
fn costs_are_itemised_over_four_orders() {
    let plan = plan_of(Sleeve::O3b, &gap_up(), o3b_now());
    assert_eq!(plan.costs.brokerage, dec("80.00"));
    assert_eq!(plan.expected_cost_inr, plan.costs.total().unwrap());
    assert!(plan.cost_share < cfg().expiry_setups.cost_share_max);
}

// --- margin ---

#[test]
fn the_calculator_is_asked_about_the_hedged_basket_only() {
    let decision = decide(Sleeve::O3b, &gap_up(), o3b_now());
    let (hedged, transient) = decision.margin_baskets();
    let shape: Vec<_> = hedged
        .iter()
        .map(|lg| (lg.tradingsymbol.as_str(), lg.side))
        .collect();
    assert_eq!(
        shape,
        vec![
            ("NIFTY26101325200CE", Side::Buy),
            ("NIFTY26101325300CE", Side::Sell),
        ]
    );
    assert!(transient.is_empty());
}

#[test]
fn no_answer_is_a_warning_on_paper_and_a_rejection_live() {
    let decision = decide(Sleeve::O3b, &gap_up(), o3b_now());
    let paper = planned(finalize(&decision));
    assert!(paper.warnings.contains(&"MARGIN_UNKNOWN".to_owned()));
    let live = finalize_full(&decision, None, Mode::Live, Decimal::ZERO, Decimal::ZERO);
    assert_eq!(live.state, PlanState::Skipped);
    assert_eq!(live.reasons, strs(&["REJECTED_MARGIN"]));
}

#[test]
fn a_basket_above_the_free_pool_is_rejected_live() {
    let decision = decide(Sleeve::O3b, &gap_up(), o3b_now());
    let quote = MarginQuote {
        hedged_inr: dec("30000"),
        transient_inr: None,
    };
    let live = finalize_full(
        &decision,
        Some(quote.clone()),
        Mode::Live,
        dec("50000"),
        dec("25000"),
    );
    assert_eq!(live.state, PlanState::Skipped);
    assert_eq!(live.reasons, strs(&["REJECTED_MARGIN"]));
    let fits = finalize_full(
        &decision,
        Some(quote),
        Mode::Live,
        dec("60000"),
        Decimal::ZERO,
    );
    assert_eq!(
        fits.plan.unwrap().margin_required_inr,
        Some(dec("30000.00"))
    );
}

// --- the clock and the day ---

#[test]
fn o3b_before_its_plan_time_is_not_ready() {
    let dd = decide(Sleeve::O3b, &gap_up(), plus20(gap_hold(), 9, 40));
    assert_eq!(
        (dd.state, dd.reasons.clone()),
        (PlanState::NotReady, strs(&[BEFORE_PLAN_TIME]))
    );
    assert!(!dd.writes_session());
}

#[test]
fn o3a_before_its_first_trigger_bar_is_not_ready() {
    let dd = decide(Sleeve::O3a, &trend_up(), plus20(trend_weekly(), 10, 10));
    assert_eq!(
        (dd.state, dd.reasons),
        (PlanState::NotReady, strs(&[BEFORE_PLAN_TIME]))
    );
}

#[test]
fn o3a_armed_is_not_ready_and_a_quiet_day_is_skipped() {
    let armed = decide(Sleeve::O3a, &gap_up(), plus20(gap_hold(), 11, 0));
    assert_eq!(
        (armed.state, armed.reasons),
        (PlanState::NotReady, strs(&[NO_TRIGGER_YET]))
    );
    let waiting = decide(Sleeve::O3a, &gap_up(), plus20(gap_hold(), 13, 2));
    assert_eq!(waiting.reasons, strs(&[NO_TRIGGER_YET]));
    let done = decide(Sleeve::O3a, &gap_up(), plus20(gap_hold(), 13, 3));
    assert_eq!(
        (done.state, done.reasons.clone()),
        (PlanState::Skipped, strs(&[NO_TRIGGER]))
    );
    assert_eq!(done.verdict, Some(Verdict::Skip));
    assert!(done.writes_session());
}

#[test]
fn o3b_on_a_day_with_no_gap_is_skipped_with_the_reason() {
    let dd = decide(Sleeve::O3b, &trend_up(), plus20(trend_weekly(), 9, 46));
    assert_eq!(dd.state, PlanState::Skipped);
    assert_eq!(dd.reasons, strs(&["GAP_TOO_SMALL"]));
}

#[test]
fn a_decision_first_asked_after_the_window_writes_nothing() {
    let dd = decide(Sleeve::O3b, &gap_up(), plus20(gap_hold(), 10, 1));
    assert_eq!(
        (dd.state, dd.reasons.clone()),
        (PlanState::WindowClosed, strs(&[ENTRY_WINDOW_CLOSED]))
    );
    assert!(!dd.writes_session());
}

#[test]
fn no_decision_snapshot_waits() {
    let dd = decide_o3(
        Sleeve::O3b,
        &gap_up(),
        &DayContext::default(),
        &mut SnapshotBook::default(),
        &o3b_now(),
        &cfg(),
        &ceil(),
    )
    .unwrap();
    assert_eq!(
        (dd.state, dd.reasons),
        (PlanState::NotReady, strs(&[NO_DECISION_SNAPSHOT]))
    );
}

#[test]
fn a_stale_decision_chain_is_skipped() {
    let dd = decide(Sleeve::O3b, &gap_up(), plus20(gap_hold(), 9, 50));
    assert_eq!(dd.state, PlanState::Skipped);
    assert_eq!(dd.reasons, strs(&[STALE_CHAIN]));
}

#[test]
fn the_hold_not_yet_settled_waits() {
    let mut c = cfg();
    c.expiry_setups.o3b_plan_time = NaiveTime::from_hms_opt(9, 30, 0).unwrap();
    let dd = decide_k(
        Sleeve::O3b,
        &gap_up(),
        plus20(gap_hold(), 9, 31),
        Knobs {
            options: c,
            ..Knobs::default()
        },
    );
    assert_eq!(
        (dd.state, dd.reasons),
        (PlanState::NotReady, strs(&[WINDOW_NOT_SETTLED]))
    );
}

#[test]
fn a_non_expiry_day_has_no_session() {
    let m = market(o2_up_break(), gap_hold_bars());
    let dd = decide(Sleeve::O3b, &m, plus20(o2_up_break(), 9, 46));
    assert_eq!(dd.state, PlanState::NoSession);
    assert!(!dd.writes_session());
}

#[test]
fn an_event_expiry_is_skipped_never_shifted() {
    let ctx = DayContext {
        event_days: vec![gap_hold()],
        ..DayContext::default()
    };
    let dd = decide_ctx(Sleeve::O3b, &gap_up(), o3b_now(), ctx);
    assert_eq!(
        (dd.state, dd.reasons),
        (PlanState::Skipped, strs(&["EVENT_DAY"]))
    );
}

#[test]
fn a_disabled_setup_has_no_session() {
    for sleeve in [Sleeve::O3a, Sleeve::O3b] {
        let mut off = cfg();
        off.expiry_setups.o3a_enabled = false;
        off.expiry_setups.o3b_enabled = false;
        let dd = decide_k(
            sleeve,
            &gap_up(),
            o3b_now(),
            Knobs {
                options: off,
                ..Knobs::default()
            },
        );
        assert_eq!(
            (dd.state, dd.reasons),
            (PlanState::NoSession, strs(&[SETUP_DISABLED]))
        );
    }
}

#[test]
fn a_paused_setup_is_skipped_with_the_code() {
    let ctx = DayContext {
        sleeves: BTreeMap::from([(
            Sleeve::O3b,
            SleeveContext {
                paused: true,
                ..SleeveContext::default()
            },
        )]),
        ..DayContext::default()
    };
    let dd = decide_ctx(Sleeve::O3b, &gap_up(), o3b_now(), ctx);
    assert_eq!(dd.reasons, strs(&[Rejection::RejectedPaused.value()]));
}

#[test]
fn only_o3_setups_are_accepted() {
    assert!(!O3_SLEEVES.contains(&Sleeve::O2));
    let err = decide_o3(
        Sleeve::O2,
        &gap_up(),
        &DayContext::default(),
        &mut SnapshotBook::default(),
        &o3b_now(),
        &cfg(),
        &ceil(),
    )
    .unwrap_err();
    match err {
        CoreError::InvalidInput(msg) => assert!(msg.contains("not an O3 setup"), "{msg}"),
        other => panic!("{other:?}"),
    }
}
