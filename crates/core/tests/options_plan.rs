//! Port of `tests/test_options_plan.py` (OP6 — the O1 plan builder).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset, NaiveTime, TimeDelta};
use rust_decimal::Decimal;

use baskfy_core::options::bars::{Bar, closed};
use baskfy_core::options::chain::Level;
use baskfy_core::options::config::{
    CostRates, Mode, OptionType, OptionsCeilings, OptionsConfig, Side, SizingMode, Sleeve,
    options_cost_rates_reviewed_on,
};
use baskfy_core::options::costs::rates_review_due;
use baskfy_core::options::execution::{LegRole, never_naked};
use baskfy_core::options::num;
use baskfy_core::options::plan::{
    Finalize, MARGIN_UNKNOWN, MarginQuote, O1Decision, O1Outcome, PlanState, STALE_CHAIN, Verdict,
    decide_o1, finalize_o1, plan_id_for,
};
use baskfy_core::options::scan::{
    DayContext, MarketDay, SleeveContext, Snapshot, SnapshotBook, Value, scan_all,
};
use baskfy_core::options::structures::SleeveBook;

use options_common::{
    LOT, Smile, at, bars_from_closes, carry_forward, d, dec, expiries_fixture, flat, ist_dt,
    market, quiet_monthly, quiet_monthly_bars, scan_snapshot, skew, to_minutes, trend_weekly,
    trend_weekly_bars, zigzag,
};

fn cfg() -> OptionsConfig {
    OptionsConfig::default()
}

fn ceil() -> OptionsCeilings {
    OptionsCeilings::default()
}

fn quiet() -> MarketDay {
    market(quiet_monthly(), quiet_monthly_bars())
}

fn now() -> DateTime<FixedOffset> {
    at(quiet_monthly(), 10, 0) + TimeDelta::seconds(40)
}

fn probe_margin() -> MarginQuote {
    MarginQuote {
        hedged_inr: dec("130903.05"),
        transient_inr: Some(dec("98250.00")),
    }
}

fn s(v: &str) -> String {
    v.to_owned()
}

fn strs(v: &[&str]) -> Vec<String> {
    v.iter().map(|x| (*x).to_owned()).collect()
}

/// What the collector stores at `minute`: the two nearest expiries, cost-of-carry forwards.
fn chain_with(bars: &[Bar], minute: DateTime<FixedOffset>, smile: &Smile) -> Snapshot {
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
    scan_snapshot(minute, spot, &expiries, smile, Some(&forwards))
}

fn chain(bars: &[Bar], minute: DateTime<FixedOffset>) -> Snapshot {
    chain_with(bars, minute, &skew(0.30, 0.33))
}

fn snap() -> Snapshot {
    chain(&quiet().bars, at(quiet_monthly(), 10, 0))
}

struct Knobs {
    sleeve: Sleeve,
    m: MarketDay,
    snap: Option<Snapshot>,
    now: DateTime<FixedOffset>,
    context: DayContext,
    options: OptionsConfig,
}

impl Default for Knobs {
    fn default() -> Self {
        Self {
            sleeve: Sleeve::O1m,
            m: quiet(),
            snap: Some(snap()),
            now: now(),
            context: DayContext::default(),
            options: cfg(),
        }
    }
}

fn decide_k(k: Knobs) -> O1Decision {
    decide_o1(
        k.sleeve,
        &k.m,
        &k.context,
        k.snap.as_ref(),
        &k.now,
        &k.options,
        &ceil(),
    )
    .unwrap()
}

fn decide() -> O1Decision {
    decide_k(Knobs::default())
}

fn finalize_full(
    decision: &O1Decision,
    margin: Option<MarginQuote>,
    mode: Mode,
    pool: Decimal,
    in_use: Decimal,
    options: &OptionsConfig,
) -> O1Outcome {
    finalize_o1(
        decision,
        margin.as_ref(),
        &Finalize {
            user_id: 7,
            mode,
            margin_pool_inr: pool,
            margin_in_use_inr: in_use,
        },
        options,
    )
    .unwrap()
}

fn finalize(decision: &O1Decision) -> O1Outcome {
    finalize_full(
        decision,
        Some(probe_margin()),
        Mode::Paper,
        Decimal::ZERO,
        Decimal::ZERO,
        &cfg(),
    )
}

fn finalize_pool(decision: &O1Decision, pool: &str, in_use: &str) -> O1Outcome {
    finalize_full(
        decision,
        Some(probe_margin()),
        Mode::Paper,
        dec(pool),
        dec(in_use),
        &cfg(),
    )
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
    let shape: Vec<_> = plan
        .legs
        .iter()
        .map(|lg| (lg.seq, lg.role, lg.strike, lg.side))
        .collect();
    assert_eq!(
        shape,
        vec![
            (1, LegRole::LongPut, Decimal::from(24700), Side::Buy),
            (2, LegRole::LongCall, Decimal::from(25300), Side::Buy),
            (3, LegRole::ShortPut, Decimal::from(24850), Side::Sell),
            (4, LegRole::ShortCall, Decimal::from(25150), Side::Sell),
        ]
    );
    let symbols: Vec<_> = plan
        .legs
        .iter()
        .map(|lg| lg.tradingsymbol.clone())
        .collect();
    assert_eq!(
        symbols,
        strs(&[
            "NIFTY26102724700PE",
            "NIFTY26102725300CE",
            "NIFTY26102724850PE",
            "NIFTY26102725150CE",
        ])
    );
    let prices: Vec<_> = plan
        .legs
        .iter()
        .map(|lg| (lg.bid, lg.ask, lg.limit_price))
        .collect();
    assert_eq!(
        prices,
        vec![
            (Some(dec("5.30")), Some(dec("5.40")), dec("5.45")),
            (Some(dec("5.65")), Some(dec("5.80")), dec("5.85")),
            (Some(dec("23.80")), Some(dec("24.05")), dec("23.75")),
            (Some(dec("27.05")), Some(dec("27.35")), dec("27.00")),
        ]
    );
    assert_eq!(plan.credit_points, dec("39.65"));
    assert_eq!(plan.credit_inr, dec("39.65") * lot());
    assert_eq!(plan.credit_inr, dec("2577.25"));
    assert_eq!((plan.lots, plan.lot_size, plan.quantity()), (1, LOT, LOT));
    assert_eq!(plan.sizing_mode, SizingMode::PaperOneLot);
    assert_eq!(plan.mode, Mode::Paper);
    assert_eq!(plan.risk_per_lot_inr, dec("8172.75"));
    assert_eq!(plan.risk_per_lot_inr, plan.risk_budget_inr);
    assert_eq!(
        plan.max_loss_inr,
        (Decimal::from(150) - dec("39.65")) * lot()
    );
    assert_eq!(plan.max_loss_inr, dec("7172.75"));
    assert_eq!(plan.profit_target_inr, dec("1288.63"));
    assert_eq!(plan.stop_inr, dec("1288.63"));
    assert_eq!(plan.margin_required_inr, Some(dec("130903.05")));
    assert_eq!(plan.margin_transient_inr, Some(dec("98250.00")));
    assert_eq!(plan.warnings, strs(&["MARGIN_POOL_UNSET"]));
    assert_eq!(plan.expires_at, ist_dt(2026, 10, 27, 10, 15, 0));
    assert_eq!(plan.entry_window_end, plan.expires_at);
    assert_eq!(plan.as_of_minute, Some(at(quiet_monthly(), 10, 0)));
}

#[test]
fn the_costs_to_the_paisa_from_the_verified_rates() {
    let plan = finalize(&decide()).plan.unwrap();
    let entry_sells = (dec("23.75") + dec("27.00")) * lot();
    let exit_sells = (dec("5.25") + dec("5.60")) * lot();
    let entry_buys = (dec("5.45") + dec("5.85")) * lot();
    let exit_buys = (dec("24.10") + dec("27.40")) * lot();
    let (sells, buys) = (entry_sells + exit_sells, entry_buys + exit_buys);
    assert_eq!((sells, buys), (dec("4004.00"), dec("4082.00")));
    let c = &plan.costs;
    assert_eq!(c.orders, 8);
    assert_eq!(c.brokerage, dec("160.00"));
    assert_eq!(c.stt, dec("6.01"));
    assert_eq!(c.exchange_txn, dec("2.87"));
    assert_eq!(c.sebi, dec("0.01"));
    assert_eq!(c.ipft, dec("0.00"));
    assert_eq!(c.stamp, dec("0.12"));
    assert_eq!(c.gst, dec("29.32"));
    assert_eq!(c.total().unwrap(), plan.expected_cost_inr);
    assert_eq!(plan.expected_cost_inr, dec("198.33"));
    assert_eq!(plan.cost_share, dec("0.1539"));
    assert!(plan.cost_share <= cfg().condor_monthly.cost_share_max);
}

#[test]
fn the_plan_is_the_scans_candidate() {
    let decision = decide();
    let mut book = SnapshotBook::new(BTreeMap::from([
        (Some(at(quiet_monthly(), 10, 0)), Some(snap())),
        (None, Some(snap())),
    ]));
    let rows = scan_all(
        &quiet(),
        &DayContext::default(),
        &now(),
        &mut book,
        &cfg(),
        &ceil(),
    )
    .unwrap();
    let row = rows.iter().find(|r| r.sleeve == Sleeve::O1m).unwrap();
    let [candidate] = row.candidates.as_slice() else {
        panic!("one candidate");
    };
    assert_eq!(decision.candidate.as_ref(), Some(candidate));
    let costs = decision.costs.as_ref().unwrap();
    assert_eq!(Some(costs.total().unwrap()), candidate.round_trip_inr);
}

#[test]
fn never_naked_at_every_prefix_of_the_send_order() {
    let plan = finalize(&decide()).plan.unwrap();
    let mut held: BTreeMap<LegRole, i64> = BTreeMap::new();
    for lg in &plan.legs {
        *held.entry(lg.role).or_insert(0) += lg.quantity;
        assert!(never_naked(&held));
    }
    let first_two: std::collections::BTreeSet<_> = plan.legs[..2].iter().map(|l| l.role).collect();
    assert_eq!(
        first_two,
        std::collections::BTreeSet::from([LegRole::LongPut, LegRole::LongCall])
    );
}

#[test]
fn the_transient_basket_is_the_wings_and_the_first_short() {
    let (hedged, transient) = decide().margin_baskets();
    let last = hedged.last().unwrap();
    assert_eq!(
        (last.tradingsymbol.as_str(), last.side),
        ("NIFTY26102725150CE", Side::Sell)
    );
    assert_eq!(transient, hedged[..3].to_vec());
    assert!(hedged.iter().all(|m| m.quantity == LOT));
}

#[test]
fn plan_id_is_deterministic_and_a_valid_client_id_half() {
    let a = finalize(&decide()).plan.unwrap();
    let b = finalize(&decide()).plan.unwrap();
    assert_eq!(a.plan_id, b.plan_id);
    assert!(a.plan_id.starts_with("O1M-20261027-"));
    assert!(!a.plan_id.contains(':') && !a.plan_id.chars().any(char::is_whitespace));
    assert_ne!(plan_id_for(8, &decide()), a.plan_id);
}

#[test]
fn expires_at_is_min_of_thirty_minutes_and_the_window() {
    let mut wide = cfg();
    wide.condor_monthly.entry_window_end = NaiveTime::from_hms_opt(11, 0, 0).unwrap();
    let decision = decide_k(Knobs {
        options: wide.clone(),
        ..Knobs::default()
    });
    let plan = finalize_full(
        &decision,
        Some(probe_margin()),
        Mode::Paper,
        Decimal::ZERO,
        Decimal::ZERO,
        &wide,
    )
    .plan
    .unwrap();
    assert_eq!(plan.expires_at, now() + TimeDelta::minutes(30));
    assert!(plan.expires_at < plan.entry_window_end);
}

// --- refusals ---

fn widen(snap: &Snapshot, strike: i64, kind: OptionType, bid: &str) -> Snapshot {
    let bid = dec(bid);
    let mut out = snap.clone();
    for q in &mut out.quotes {
        if q.expiry == quiet_monthly() && q.strike == Decimal::from(strike) && q.option_type == kind
        {
            q.bid = Some(bid);
            q.bids = vec![
                Level {
                    price: bid,
                    quantity: 1300
                };
                3
            ];
        }
    }
    out
}

#[test]
fn beyond_max_spread_the_put_is_not_a_short() {
    let dd = decide_k(Knobs {
        snap: Some(widen(&snap(), 24850, OptionType::Pe, "23.30")),
        ..Knobs::default()
    });
    assert_eq!(dd.state, PlanState::Skipped);
    assert_eq!(dd.reasons, strs(&["REJECTED_NO_SHORT_PUT"]));
    assert_eq!(dd.verdict, Some(Verdict::Trade));
}

#[test]
fn inside_max_spread_it_only_lowers_the_credit() {
    let dd = decide_k(Knobs {
        snap: Some(widen(&snap(), 24850, OptionType::Pe, "23.40")),
        ..Knobs::default()
    });
    assert_eq!(dd.state, PlanState::Planned);
    let c = dd.candidate.unwrap();
    assert_eq!(c.points, Some(dec("39.25")));
    assert!(c.cost_share.unwrap() < dec("0.16"));
}

#[test]
fn a_credit_the_costs_eat_is_rejected_cost() {
    let mut low = cfg();
    low.condor_monthly.credit_floor_frac = dec("0.10");
    let snap = chain_with(&quiet().bars, at(quiet_monthly(), 10, 0), &flat(0.16));
    let dd = decide_k(Knobs {
        snap: Some(snap),
        options: low.clone(),
        ..Knobs::default()
    });
    assert_eq!(dd.state, PlanState::Skipped);
    assert_eq!(dd.reasons, strs(&["REJECTED_COST"]));
    let c = dd.candidate.unwrap();
    assert_eq!(c.points, Some(dec("25.35")));
    let share = c.cost_share.unwrap();
    assert!(share > low.condor_monthly.cost_share_max);
    let rt = c.round_trip_inr.unwrap();
    let gain = num::muli(num::mul(dec("0.5"), dec("25.35")).unwrap(), LOT).unwrap();
    assert_eq!(share, num::div(rt, gain).unwrap());
}

#[test]
fn at_the_default_floor_the_cost_test_cannot_bind_for_one_lot() {
    let c = cfg().condor_monthly;
    let gain =
        (Decimal::ONE - c.profit_take_frac) * (c.credit_floor_frac * c.wing_width_points) * lot();
    assert_eq!(gain, dec("1218.75"));
    assert!(dec("198.33") / gain < c.cost_share_max);
}

fn spike_bars() -> Vec<Bar> {
    let mut closes = zigzag(Decimal::from(25010), Decimal::from(8), to_minutes(15, 30));
    closes[5] = Decimal::from(25150);
    bars_from_closes(quiet_monthly(), Decimal::from(25010), &closes)
}

#[test]
fn only_in_band_call_inside_the_or_is_rejected_no_short_call() {
    let m = market(quiet_monthly(), spike_bars());
    let snap = chain(&m.bars, at(quiet_monthly(), 10, 0));
    let dd = decide_k(Knobs {
        m,
        snap: Some(snap),
        ..Knobs::default()
    });
    assert_eq!(dd.numbers.get("or_high"), Some(&Value::Str(s("25152.00"))));
    assert_eq!(dd.numbers.get("contained"), Some(&Value::Bool(true)));
    assert_eq!(dd.verdict, Some(Verdict::Trade));
    assert_eq!(dd.state, PlanState::Skipped);
    assert_eq!(dd.reasons, strs(&["REJECTED_NO_SHORT_CALL"]));
}

// --- which days have a session ---

#[test]
fn o1w_on_the_monthly_tuesday_has_no_session() {
    let dd = decide_k(Knobs {
        sleeve: Sleeve::O1w,
        ..Knobs::default()
    });
    assert_eq!(dd.state, PlanState::NoSession);
    assert!(!dd.writes_session());
    assert_eq!(dd.reasons, strs(&["MONTHLY_EXPIRY"]));
    assert!(finalize(&dd).plan.is_none());
}

#[test]
fn o1m_on_a_weekly_has_no_session() {
    let dd = decide_k(Knobs {
        m: market(trend_weekly(), trend_weekly_bars()),
        ..Knobs::default()
    });
    assert_eq!(dd.state, PlanState::NoSession);
}

#[test]
fn an_event_day_on_the_monthly_is_skipped_not_shifted() {
    let dd = decide_k(Knobs {
        context: DayContext {
            event_days: vec![quiet_monthly()],
            ..DayContext::default()
        },
        ..Knobs::default()
    });
    assert_eq!(
        (dd.state, dd.reasons, dd.verdict),
        (
            PlanState::Skipped,
            strs(&["EVENT_DAY"]),
            Some(Verdict::Skip)
        )
    );
}

#[test]
fn the_gate_skips_with_every_reason() {
    let m = market(trend_weekly(), trend_weekly_bars());
    let snap = chain(&m.bars, at(trend_weekly(), 10, 0));
    let dd = decide_k(Knobs {
        sleeve: Sleeve::O1w,
        m,
        snap: Some(snap),
        now: at(trend_weekly(), 10, 1),
        ..Knobs::default()
    });
    assert_eq!(dd.state, PlanState::Skipped);
    assert_eq!(dd.verdict, Some(Verdict::Skip));
    assert!(dd.reasons.contains(&s("NOT_CONTAINED")) && dd.reasons.contains(&s("ER_TOO_HIGH")));
    assert!(dd.candidate.is_none());
}

// --- the slot and the pause ---

#[test]
fn slot_held_by_o3_is_rejected_slot_taken() {
    let dd = decide_k(Knobs {
        context: DayContext {
            slot_holder: Some(Sleeve::O3b),
            ..DayContext::default()
        },
        ..Knobs::default()
    });
    assert_eq!(dd.state, PlanState::Skipped);
    assert_eq!(dd.reasons, strs(&["REJECTED_SLOT_TAKEN"]));
}

#[test]
fn a_slot_held_by_itself_is_not_refused() {
    let dd = decide_k(Knobs {
        context: DayContext {
            slot_holder: Some(Sleeve::O1m),
            ..DayContext::default()
        },
        ..Knobs::default()
    });
    assert_eq!(dd.state, PlanState::Planned);
}

fn with_sleeve(ctx: SleeveContext) -> DayContext {
    DayContext {
        sleeves: BTreeMap::from([(Sleeve::O1m, ctx)]),
        ..DayContext::default()
    }
}

#[test]
fn a_paused_sleeve_is_rejected_paused() {
    let dd = decide_k(Knobs {
        context: with_sleeve(SleeveContext {
            paused: true,
            ..SleeveContext::default()
        }),
        ..Knobs::default()
    });
    assert_eq!(dd.reasons, strs(&["REJECTED_PAUSED"]));
}

#[test]
fn live_with_capital_zero_is_rejected_no_sleeve_capital() {
    let dd = decide_k(Knobs {
        context: with_sleeve(SleeveContext {
            book: SleeveBook {
                mode: Mode::Live,
                ..SleeveBook::default()
            },
            ..SleeveContext::default()
        }),
        ..Knobs::default()
    });
    assert_eq!(dd.reasons, strs(&["REJECTED_NO_SLEEVE_CAPITAL"]));
}

// --- the clock ---

#[test]
fn outside_the_window_nothing_is_decided() {
    for ((h, m, sec), state, reason) in [
        ((9, 59, 50), PlanState::NotReady, "BEFORE_PLAN_TIME"),
        ((10, 15, 0), PlanState::WindowClosed, "ENTRY_WINDOW_CLOSED"),
    ] {
        let dd = decide_k(Knobs {
            now: ist_dt(2026, 10, 27, h, m, sec),
            ..Knobs::default()
        });
        assert_eq!((dd.state, dd.reasons.clone()), (state, strs(&[reason])));
        assert!(!dd.writes_session());
    }
}

#[test]
fn no_decision_snapshot_yet_waits() {
    let dd = decide_k(Knobs {
        snap: None,
        ..Knobs::default()
    });
    assert_eq!(
        (dd.state, dd.reasons),
        (PlanState::NotReady, strs(&["NO_DECISION_SNAPSHOT"]))
    );
}

#[test]
fn the_0959_bar_not_stored_yet_waits() {
    let mut m = quiet();
    m.bars.retain(|b| b.ts < at(quiet_monthly(), 9, 59));
    let dd = decide_k(Knobs {
        m,
        ..Knobs::default()
    });
    assert_eq!(
        (dd.state, dd.reasons),
        (PlanState::NotReady, strs(&["WINDOW_NOT_SETTLED"]))
    );
}

#[test]
fn a_decision_snapshot_older_than_the_stale_limit_is_not_planned() {
    let late = at(quiet_monthly(), 10, 2) + TimeDelta::seconds(1);
    let dd = decide_k(Knobs {
        now: late,
        ..Knobs::default()
    });
    assert_eq!(
        (dd.state, dd.reasons),
        (PlanState::Skipped, strs(&[STALE_CHAIN]))
    );
}

// --- the margin ---

fn margin_message(out: &O1Outcome) -> String {
    match out.decision.numbers.get("margin_message") {
        Some(Value::Str(m)) => m.clone(),
        other => panic!("margin_message: {other:?}"),
    }
}

#[test]
fn a_pool_too_small_is_rejected_margin() {
    let out = finalize_pool(&decide(), "150000", "20000");
    assert!(out.plan.is_none());
    assert_eq!(out.state, PlanState::Skipped);
    assert_eq!(out.reasons, strs(&["REJECTED_MARGIN"]));
    assert!(margin_message(&out).contains("130903.05"));
}

#[test]
fn the_transient_figure_is_a_ceiling_too() {
    let big = MarginQuote {
        hedged_inr: dec("100000"),
        transient_inr: Some(dec("160000")),
    };
    let out = finalize_full(
        &decide(),
        Some(big),
        Mode::Paper,
        dec("150000"),
        Decimal::ZERO,
        &cfg(),
    );
    assert_eq!(out.reasons, strs(&["REJECTED_MARGIN"]));
    assert!(margin_message(&out).contains("transient"));
}

#[test]
fn a_pool_that_fits_is_ok_with_no_warning() {
    let plan = finalize_pool(&decide(), "200000", "0").plan.unwrap();
    assert!(plan.warnings.is_empty());
    assert_eq!(plan.margin.unwrap().code.value(), "OK");
}

#[test]
fn no_answer_is_a_warning_in_paper_and_a_refusal_live() {
    let zero = Decimal::ZERO;
    let paper = finalize_full(&decide(), None, Mode::Paper, zero, zero, &cfg())
        .plan
        .unwrap();
    assert!(paper.warnings.contains(&s(MARGIN_UNKNOWN)));
    assert!(paper.margin_required_inr.is_none());
    let live = finalize_full(&decide(), None, Mode::Live, zero, zero, &cfg());
    assert!(live.plan.is_none());
    assert_eq!(live.reasons, strs(&["REJECTED_MARGIN"]));
}

#[test]
fn margin_never_sizes() {
    let a = finalize_pool(&decide(), "10000000", "0").plan.unwrap();
    let b = finalize_pool(&decide(), "200000", "0").plan.unwrap();
    assert_eq!((a.lots, b.lots), (1, 1));
}

// --- the rates are pinned ---

#[test]
fn each_rate_is_opzeros_verified_value() {
    let r = CostRates::default();
    assert_eq!(
        (
            r.brokerage_per_order_inr,
            r.stt_sell_premium_pct,
            r.stt_exercise_intrinsic_pct,
            r.exchange_txn_pct,
            r.sebi_per_crore_inr,
            r.ipft_per_crore_inr,
            r.stamp_buy_pct,
            r.gst_pct,
            r.auto_squareoff_inr,
        ),
        (
            dec("20"),
            dec("0.15"),
            dec("0.15"),
            dec("0.03553"),
            dec("10"),
            dec("0.01"),
            dec("0.003"),
            dec("18"),
            dec("50"),
        )
    );
}

#[test]
fn reviewed_on_is_the_constant_and_warns_after_90_days() {
    assert_eq!(d(2026, 9, 22), options_cost_rates_reviewed_on());
    assert_eq!(
        CostRates::default().reviewed_on,
        options_cost_rates_reviewed_on()
    );
    assert!(!rates_review_due(&CostRates::default(), d(2026, 12, 21)));
    assert!(rates_review_due(&CostRates::default(), d(2026, 12, 22)));
}
