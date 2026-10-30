//! Port of `tests/test_options_condor.py`: O1's gate, structure and exits.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use chrono::{DateTime, FixedOffset, NaiveDate, TimeDelta};
use rust_decimal::Decimal;

use baskfy_core::options::bars::{Bar, efficiency_ratio, er_points};
use baskfy_core::options::condor::{
    self, CondorBuild, CondorExit, CondorTick, DayVerdict, GateReason,
};
use baskfy_core::options::config::{
    CondorConfig, CondorVariant, Mode, OptionType, OptionsCeilings, OptionsConfig, Side, SizingMode,
};
use baskfy_core::options::costs::{CostFill, charges};
use baskfy_core::options::execution::{LegRole, entry_sequence, exit_sequence};
use baskfy_core::options::num;
use baskfy_core::options::structures::{
    Candidate, ChainView, Rejection, SleeveBook, Structure, ViewInputs, chain_view,
};

use options_common::{
    LOT, Snapshot, at, bars_from_closes, d, dec, fill_lists, ist_dt, prev_close, quiet_monthly,
    quiet_monthly_bars, ramp, settle, skew, snapshot_with, tick, trend_weekly, trend_weekly_bars,
    walk_entry, walk_exit, zigzag,
};

fn cfg() -> OptionsConfig {
    OptionsConfig::default()
}

fn m() -> CondorConfig {
    cfg().condor_monthly
}

fn w() -> CondorConfig {
    cfg().condor_weekly
}

fn grace() -> i64 {
    cfg().chain.stale_scan_seconds
}

fn nov3() -> NaiveDate {
    d(2026, 11, 3)
}

fn observe_with(
    bars: &[Bar],
    now: DateTime<FixedOffset>,
    event: bool,
    config: &CondorConfig,
) -> DayVerdict {
    condor::observe(bars, prev_close(), &now, event, config, grace()).unwrap()
}

fn observe(bars: &[Bar], now: DateTime<FixedOffset>) -> DayVerdict {
    observe_with(bars, now, false, &m())
}

// --- Kaufman's ER (condor 04 §2.4, §2.8) ---

#[test]
fn er_a_straight_line_of_45_rising_closes_is_one() {
    let bars = bars_from_closes(
        quiet_monthly(),
        Decimal::from(25000),
        &ramp(Decimal::from(25000), Decimal::TWO, 45),
    );
    assert_eq!(efficiency_ratio(&er_points(&bars)).unwrap(), Decimal::ONE);
}

#[test]
fn er_a_perfect_zigzag_is_near_zero() {
    let closes = zigzag(Decimal::from(25000), Decimal::TEN, 45);
    let bars = bars_from_closes(quiet_monthly(), Decimal::from(25000), &closes);
    // Net move 10 over a path of 10 + 43 * 20 = 870.
    assert_eq!(
        efficiency_ratio(&er_points(&bars)).unwrap(),
        num::div(Decimal::TEN, Decimal::from(870)).unwrap()
    );
}

#[test]
fn er_no_path_is_zero_not_a_division() {
    assert_eq!(
        efficiency_ratio(&[Decimal::ONE; 45]).unwrap(),
        Decimal::ZERO
    );
    assert_eq!(efficiency_ratio(&[]).unwrap(), Decimal::ZERO);
}

#[test]
fn er_c0_is_the_first_bars_open_not_its_close() {
    let bar = |hh, mm, o, h, l, c| Bar {
        ts: at(quiet_monthly(), hh, mm),
        open: Decimal::from(o),
        high: Decimal::from(h),
        low: Decimal::from(l),
        close: Decimal::from(c),
    };
    let bars = vec![
        bar(9, 15, 100, 111, 99, 110),
        bar(9, 16, 110, 111, 104, 105),
    ];
    assert_eq!(
        er_points(&bars),
        vec![Decimal::from(100), Decimal::from(105)]
    );
    assert_eq!(efficiency_ratio(&er_points(&bars)).unwrap(), Decimal::ONE);
}

// --- the gate (condor 04 §2) ---

#[test]
fn gate_the_quiet_monthly_trades_with_its_numbers() {
    let v = observe(&quiet_monthly_bars(), at(quiet_monthly(), 10, 0));
    assert!(v.final_ && v.trade() && v.reasons.is_empty());
    let n = &v.numbers;
    assert_eq!(n.bars, 45);
    assert_eq!(n.open_0915, Some(Decimal::from(25010)));
    assert_eq!(n.gap_pct, Some(dec("0.04")));
    assert_eq!(
        (n.obs_high, n.obs_low),
        (Some(Decimal::from(25020)), Some(Decimal::from(25000)))
    );
    assert_eq!(n.range_pct, Some(dec("0.08")));
    assert_eq!(n.contained, Some(true));
    assert!(n.er.is_some_and(|e| e <= m().er_max));
}

#[test]
fn gate_not_final_while_the_window_fills_and_no_incomplete_reason() {
    let v = observe(&quiet_monthly_bars(), at(quiet_monthly(), 9, 50));
    assert!(!v.final_ && !v.trade());
    assert!(!v.reasons.contains(&GateReason::IncompleteObservation));
    assert_eq!(v.numbers.bars, 35);
}

#[test]
fn gate_the_trend_weekly_skips_for_containment_and_er_every_reason_reported() {
    let v = observe_with(&trend_weekly_bars(), at(trend_weekly(), 10, 0), false, &w());
    assert!(v.final_);
    assert_eq!(
        v.reasons,
        vec![GateReason::NotContained, GateReason::ErTooHigh]
    );
    assert_eq!(v.numbers.er, Some(Decimal::ONE));
    assert_eq!(v.numbers.gap_pct, Some(Decimal::ZERO));
}

#[test]
fn gate_every_reason_in_condor_order() {
    let bars = bars_from_closes(
        quiet_monthly(),
        Decimal::from(25250),
        &ramp(Decimal::from(25250), Decimal::from(6), 45),
    );
    let v = observe_with(&bars, at(quiet_monthly(), 10, 0), true, &m());
    assert_eq!(
        v.reasons,
        vec![
            GateReason::GapTooBig,
            GateReason::RangeTooWide,
            GateReason::NotContained,
            GateReason::ErTooHigh,
            GateReason::EventDay,
        ]
    );
}

#[test]
fn gate_gap_exactly_at_the_limit_is_not_a_reason() {
    let frac = num::div(m().gap_max_pct, Decimal::ONE_HUNDRED).unwrap();
    let open = num::mul(prev_close(), num::add(Decimal::ONE, frac).unwrap()).unwrap();
    let bars = bars_from_closes(quiet_monthly(), open, &zigzag(open, Decimal::from(5), 45));
    let v = observe(&bars, at(quiet_monthly(), 10, 0));
    assert_eq!(v.numbers.gap_pct, Some(m().gap_max_pct));
    assert!(!v.reasons.contains(&GateReason::GapTooBig));
}

#[test]
fn gate_a_missing_minute_waits_for_the_grace_then_is_incomplete() {
    let skip = at(quiet_monthly(), 9, 59);
    let bars: Vec<Bar> = quiet_monthly_bars()
        .into_iter()
        .filter(|b| b.ts != skip)
        .collect();
    let early = observe(&bars, ist_dt(2026, 10, 27, 10, 1, 0));
    assert!(!early.final_);
    let late = observe(
        &bars,
        at(quiet_monthly(), 10, 0) + TimeDelta::seconds(60 + grace()),
    );
    assert!(late.final_);
    assert_eq!(late.reasons, vec![GateReason::IncompleteObservation]);
}

#[test]
fn gate_a_forming_minute_is_not_read() {
    let v = observe(&quiet_monthly_bars(), ist_dt(2026, 10, 27, 9, 59, 30));
    assert!(v.numbers.bars == 44 && !v.final_);
}

#[test]
fn gate_the_two_variants_are_distinct_objects_that_start_equal_but_for_risk() {
    let (mo, we) = (m(), w());
    assert_ne!(mo, we);
    assert_eq!(
        (mo.variant, we.variant),
        (CondorVariant::Monthly, CondorVariant::Weekly)
    );
    let aligned = CondorConfig {
        variant: mo.variant,
        risk_per_trade_pct: mo.risk_per_trade_pct,
        max_lots: mo.max_lots,
        tier3_min_sessions: mo.tier3_min_sessions,
        ..we
    };
    assert_eq!(aligned, mo);
}

// --- the structure (condor 04 §4, 04 §3, §6, §7) ---

fn now() -> DateTime<FixedOffset> {
    at(quiet_monthly(), 10, 0)
}

fn spot() -> Decimal {
    Decimal::from(25018)
}

fn or_high() -> Decimal {
    Decimal::from(25020)
}

fn or_low() -> Decimal {
    Decimal::from(25000)
}

fn monthly_snapshot_with(call_vol: f64, put_vol: f64, depth: i64) -> Snapshot {
    snapshot_with(
        now(),
        spot(),
        &[quiet_monthly(), nov3()],
        &skew(call_vol, put_vol),
        None,
        depth,
    )
}

fn monthly_snapshot() -> Snapshot {
    monthly_snapshot_with(0.30, 0.33, 1300)
}

fn view_of(snap: &Snapshot) -> ChainView {
    let c = cfg();
    chain_view(
        &snap.quotes,
        &ViewInputs {
            expiry: quiet_monthly(),
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
    or_high: Decimal,
    or_low: Decimal,
    book: SleeveBook,
    config: CondorConfig,
    slot_free: bool,
    paused: bool,
    lot_size: Option<i64>,
}

impl Default for Knobs {
    fn default() -> Self {
        Self {
            or_high: or_high(),
            or_low: or_low(),
            book: SleeveBook::default(),
            config: m(),
            slot_free: true,
            paused: false,
            lot_size: Some(LOT),
        }
    }
}

fn build_with(view: Option<&ChainView>, k: Knobs) -> Candidate {
    condor::build(
        view,
        &CondorBuild {
            or_high: k.or_high,
            or_low: k.or_low,
            lot_size: k.lot_size,
            book: k.book,
            slot_free: k.slot_free,
            paused: k.paused,
            expiry: quiet_monthly(),
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

/// Condor §4.1 read literally: |delta| in band AND beyond the range, nearest the target.
fn expected_short(view: &ChainView, kind: OptionType, bound: Decimal) -> Decimal {
    let mo = m();
    let mut best: Option<(Decimal, Decimal)> = None;
    for q in view.of_type(kind) {
        let Some(delta) = q.delta() else { continue };
        let a = num::abs(num::from_float(delta).unwrap()).unwrap();
        let beyond = match kind {
            OptionType::Ce => q.strike() > bound,
            OptionType::Pe => q.strike() < bound,
        };
        if !(mo.delta_min <= a && a <= mo.delta_max && beyond) {
            continue;
        }
        let key = num::abs(a - mo.delta_target).unwrap();
        if best.is_none_or(|(k, _)| key < k) {
            best = Some((key, q.strike()));
        }
    }
    best.unwrap().1
}

fn role_of(c: &Candidate, role: LegRole) -> &baskfy_core::options::structures::CandidateLeg {
    c.legs.iter().find(|lg| lg.role == role).unwrap()
}

#[test]
fn structure_the_quiet_monthly_condor_to_the_rupee() {
    let view = view_of(&monthly_snapshot());
    let c = build(&view);
    assert!(c.viable() && c.structure == Structure::IronCondor);
    let sc = role_of(&c, LegRole::ShortCall);
    let sp = role_of(&c, LegRole::ShortPut);
    let lc = role_of(&c, LegRole::LongCall);
    let lp = role_of(&c, LegRole::LongPut);
    assert_eq!(sc.strike, expected_short(&view, OptionType::Ce, or_high()));
    assert_eq!(sc.strike, Decimal::from(25150));
    assert_eq!(sp.strike, expected_short(&view, OptionType::Pe, or_low()));
    assert_eq!(sp.strike, Decimal::from(24850));
    assert_eq!(
        (lc.strike, lp.strike),
        (Decimal::from(25300), Decimal::from(24700))
    );
    let z = Decimal::ZERO;
    let points = c.points.unwrap();
    assert_eq!(
        points,
        sc.bid.unwrap_or(z) + sp.bid.unwrap_or(z) - lc.ask.unwrap_or(z) - lp.ask.unwrap_or(z)
    );
    assert_eq!(points, dec("39.55"));
    let mo = m();
    assert!(points >= mo.credit_floor_frac * mo.wing_width_points);
    assert_eq!(lc.limit_price, lc.ask.unwrap_or(z) + tick());
    assert_eq!(sc.limit_price, sc.bid.unwrap_or(z) - tick());
    assert_eq!(
        (c.lots, c.sizing_mode, c.lot_size),
        (1, Some(SizingMode::PaperOneLot), Some(LOT))
    );
    let lot = Decimal::from(LOT);
    let per_lot = c.risk_per_lot_inr.unwrap();
    assert_eq!(
        per_lot,
        (Decimal::from(150) - dec("39.55")) * lot + Decimal::from(1000)
    );
    assert_eq!(per_lot, dec("8179.25"));
    assert_eq!(c.r_inr, c.risk_per_lot_inr);
    let max_loss = c.max_loss_inr.unwrap();
    assert_eq!(max_loss, (Decimal::from(150) - dec("39.55")) * lot);
    assert_eq!(max_loss, dec("7179.25"));
}

#[test]
fn structure_the_round_trip_is_eight_orders_at_the_plans_prices() {
    let c = build(&view_of(&monthly_snapshot()));
    let mut fills = Vec::new();
    for lg in &c.legs {
        let (bid, ask) = (lg.bid.unwrap_or_default(), lg.ask.unwrap_or_default());
        let entry_side = if lg.role.is_long() {
            Side::Buy
        } else {
            Side::Sell
        };
        let exit_side = if lg.role.is_long() {
            Side::Sell
        } else {
            Side::Buy
        };
        let entry = if entry_side == Side::Buy {
            ask + tick()
        } else {
            bid - tick()
        };
        let close = if exit_side == Side::Sell {
            bid - tick()
        } else {
            ask + tick()
        };
        fills.push(CostFill {
            side: entry_side,
            price: entry,
            quantity: LOT,
        });
        fills.push(CostFill {
            side: exit_side,
            price: close,
            quantity: LOT,
        });
    }
    let expected = charges(&fills, &cfg().costs).unwrap();
    assert_eq!(expected.orders, 8);
    let total = expected.total().unwrap();
    assert_eq!(c.round_trip_inr, Some(total));
    assert_eq!(total, dec("198.32"));
    let mo = m();
    let gain = (Decimal::ONE - mo.profit_take_frac) * dec("39.55") * Decimal::from(LOT);
    assert_eq!(c.expected_gain_inr, Some(gain));
    assert_eq!(c.cost_share, Some(num::div(dec("198.32"), gain).unwrap()));
    assert!(c.cost_share.is_some_and(|s| s <= mo.cost_share_max));
    assert!(c.warnings.is_empty());
}

#[test]
fn structure_delta_and_range_neither_relaxed_for_the_call() {
    let view = view_of(&monthly_snapshot());
    let c = build_with(
        Some(&view),
        Knobs {
            or_high: Decimal::from(25150),
            ..Knobs::default()
        },
    );
    assert_eq!(c.rejection, Some(Rejection::RejectedNoShortCall));
    let tight = CondorConfig {
        delta_min: dec("0.245"),
        delta_max: dec("0.246"),
        ..m()
    };
    let c = build_with(
        Some(&view),
        Knobs {
            config: tight,
            ..Knobs::default()
        },
    );
    assert_eq!(c.rejection, Some(Rejection::RejectedNoShortCall));
}

#[test]
fn structure_delta_and_range_for_the_put() {
    let view = view_of(&monthly_snapshot());
    let c = build_with(
        Some(&view),
        Knobs {
            or_low: Decimal::from(24850),
            ..Knobs::default()
        },
    );
    assert_eq!(c.rejection, Some(Rejection::RejectedNoShortPut));
}

#[test]
fn structure_a_missing_wing_rejects() {
    let snap = monthly_snapshot();
    let gone = Snapshot {
        ts: snap.ts,
        spot: snap.spot,
        quotes: snap
            .quotes
            .into_iter()
            .filter(|q| !(q.strike == Decimal::from(25300) && q.option_type == OptionType::Ce))
            .collect(),
    };
    assert_eq!(
        build(&view_of(&gone)).rejection,
        Some(Rejection::RejectedNoWing)
    );
}

#[test]
fn structure_a_credit_below_the_floor_rejects() {
    let c = build_with(
        Some(&view_of(&monthly_snapshot())),
        Knobs {
            config: CondorConfig {
                credit_floor_frac: dec("0.30"),
                ..m()
            },
            ..Knobs::default()
        },
    );
    assert_eq!(c.rejection, Some(Rejection::RejectedCredit));
    assert_eq!(c.points, Some(dec("39.55")));
}

#[test]
fn structure_the_budget_sizes_with_the_first_live_half() {
    let view = view_of(&monthly_snapshot());
    let book = |capital: i64| SleeveBook {
        sleeve_capital_inr: Decimal::from(capital),
        ..SleeveBook::default()
    };
    let small = build_with(
        Some(&view),
        Knobs {
            book: book(1_000_000),
            ..Knobs::default()
        },
    );
    assert_eq!(small.rejection, Some(Rejection::RejectedBudget));
    let big = build_with(
        Some(&view),
        Knobs {
            book: book(5_000_000),
            ..Knobs::default()
        },
    );
    assert_eq!(
        (big.lots, big.sizing_mode, big.half_size),
        (1, Some(SizingMode::Budget), true)
    );
    assert_eq!(big.r_inr, Some(Decimal::from(12500)));
}

#[test]
fn structure_live_with_no_capital_is_refused() {
    let c = build_with(
        Some(&view_of(&monthly_snapshot())),
        Knobs {
            book: SleeveBook {
                mode: Mode::Live,
                ..SleeveBook::default()
            },
            ..Knobs::default()
        },
    );
    assert_eq!(c.rejection, Some(Rejection::RejectedNoSleeveCapital));
}

#[test]
fn structure_liquidity_is_checked_at_the_sized_quantity() {
    let thin = view_of(&monthly_snapshot_with(0.30, 0.33, 50));
    assert!(build(&thin).viable());
    let sized = || SleeveBook {
        sleeve_capital_inr: Decimal::from(5_000_000),
        real_journal_rows: 5,
        ..SleeveBook::default()
    };
    let full = build_with(
        Some(&view_of(&monthly_snapshot())),
        Knobs {
            book: sized(),
            ..Knobs::default()
        },
    );
    assert_eq!(full.lots, 3);
    let c = build_with(
        Some(&thin),
        Knobs {
            book: sized(),
            ..Knobs::default()
        },
    );
    assert_eq!(c.rejection, Some(Rejection::RejectedIlliquid));
}

#[test]
fn structure_refusals_before_pricing() {
    let view = view_of(&monthly_snapshot());
    let paused = build_with(
        Some(&view),
        Knobs {
            paused: true,
            ..Knobs::default()
        },
    );
    assert_eq!(paused.rejection, Some(Rejection::RejectedPaused));
    let taken = build_with(
        Some(&view),
        Knobs {
            slot_free: false,
            ..Knobs::default()
        },
    );
    assert_eq!(taken.rejection, Some(Rejection::RejectedSlotTaken));
    assert_eq!(
        build_with(None, Knobs::default()).rejection,
        Some(Rejection::RejectedNoChain)
    );
    let no_lot = build_with(
        Some(&view),
        Knobs {
            lot_size: None,
            ..Knobs::default()
        },
    );
    assert_eq!(no_lot.rejection, Some(Rejection::RejectedNoLotSize));
}

// --- never naked (Track C §2) ---

fn condor_roles() -> Vec<LegRole> {
    build(&view_of(&monthly_snapshot()))
        .legs
        .iter()
        .map(|lg| lg.role)
        .collect()
}

#[test]
fn never_naked_the_candidate_has_both_wings_and_the_sequences_protect() {
    let roles = condor_roles();
    assert_eq!(
        entry_sequence(&roles).unwrap(),
        vec![
            LegRole::LongPut,
            LegRole::LongCall,
            LegRole::ShortPut,
            LegRole::ShortCall
        ]
    );
    assert_eq!(
        exit_sequence(&roles),
        vec![
            LegRole::ShortCall,
            LegRole::ShortPut,
            LegRole::LongCall,
            LegRole::LongPut
        ]
    );
}

#[test]
fn never_naked_entry() {
    let roles = condor_roles();
    for fills in fill_lists(0, LOT, 12, 300, 0x0c0d_0e01) {
        walk_entry(&roles, LOT, &fills);
    }
}

#[test]
fn never_naked_exit() {
    let roles = condor_roles();
    for fills in fill_lists(1, LOT, 40, 300, 0x0c0d_0e02) {
        walk_exit(&roles, LOT, &fills);
    }
}

// --- exits (condor 04 §7) ---

struct Tick {
    d: &'static str,
    c: &'static str,
    spot: &'static str,
    clock: (u32, u32),
    stale: bool,
    loss: &'static str,
    manual: bool,
}

impl Default for Tick {
    fn default() -> Self {
        Self {
            d: "0",
            c: "40",
            spot: "25010",
            clock: (11, 0),
            stale: false,
            loss: "0",
            manual: false,
        }
    }
}

fn decide(t: Tick) -> Option<CondorExit> {
    let c = cfg();
    condor::exit_decision(
        &CondorTick {
            entry_credit: dec(t.c),
            cost_now: dec(t.d),
            spot: Some(dec(t.spot)),
            short_call_strike: Decimal::from(25150),
            short_put_strike: Decimal::from(24850),
            now: at(quiet_monthly(), t.clock.0, t.clock.1),
            stale: t.stale,
            marked_loss_inr: dec(t.loss),
            risk_budget_inr: Decimal::from(8000),
            manual: t.manual,
        },
        &c.condor_monthly,
        &c.risk,
    )
    .unwrap()
}

fn d_(d: &'static str) -> Tick {
    Tick {
        d,
        ..Tick::default()
    }
}

#[test]
fn exits_profit_at_half_c_and_stop_at_one_and_a_half_c() {
    let cases = [
        ("20", Some(CondorExit::Profit)),
        ("20.05", None),
        ("59.95", None),
        ("60", Some(CondorExit::Stop)),
    ];
    for (d, expected) in cases {
        assert_eq!(decide(d_(d)), expected, "d={d}");
    }
}

#[test]
fn exits_a_strike_touch_on_either_side() {
    for spot in ["25150", "24850"] {
        let t = Tick { spot, ..d_("30") };
        assert_eq!(decide(t), Some(CondorExit::StrikeTouch));
    }
}

#[test]
fn exits_hard_exit_at_1430_outranks_everything() {
    let t = Tick {
        spot: "25200",
        clock: (14, 30),
        ..d_("70")
    };
    assert_eq!(decide(t), Some(CondorExit::HardExit));
    let t = Tick {
        clock: (14, 29),
        ..d_("30")
    };
    assert_eq!(decide(t), None);
}

#[test]
fn exits_precedence_touch_over_stop_over_profit() {
    let t = Tick {
        spot: "25200",
        ..d_("70")
    };
    assert_eq!(decide(t), Some(CondorExit::StrikeTouch));
}

#[test]
fn exits_profit_is_never_taken_on_a_stale_mark_but_stops_are() {
    let t = Tick {
        stale: true,
        ..d_("10")
    };
    assert_eq!(decide(t), None);
    let t = Tick {
        stale: true,
        ..d_("70")
    };
    assert_eq!(decide(t), Some(CondorExit::Stop));
}

#[test]
fn exits_a_budget_breach_stops_whatever_d_over_c_says() {
    let t = Tick {
        loss: "8000.01",
        ..d_("30")
    };
    assert_eq!(decide(t), Some(CondorExit::Stop));
    let t = Tick {
        loss: "8000",
        ..d_("30")
    };
    assert_eq!(decide(t), None);
}

#[test]
fn exits_manual_is_the_button_and_loses_to_automatic_rules() {
    let t = Tick {
        manual: true,
        ..d_("30")
    };
    assert_eq!(decide(t), Some(CondorExit::Manual));
    let t = Tick {
        manual: true,
        ..d_("10")
    };
    assert_eq!(decide(t), Some(CondorExit::Profit));
}

#[test]
fn exits_the_cost_to_close_reads_shorts_at_ask_and_wings_at_bid() {
    let c = build(&view_of(&monthly_snapshot()));
    let z = Decimal::ZERO;
    let expected = role_of(&c, LegRole::ShortCall).ask.unwrap_or(z)
        + role_of(&c, LegRole::ShortPut).ask.unwrap_or(z)
        - role_of(&c, LegRole::LongCall).bid.unwrap_or(z)
        - role_of(&c, LegRole::LongPut).bid.unwrap_or(z);
    assert_eq!(condor::cost_to_close(&c.legs).unwrap(), expected);
}
