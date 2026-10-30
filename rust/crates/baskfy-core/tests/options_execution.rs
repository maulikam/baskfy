//! Port of `tests/test_options_execution.py`: `04` §8 sequences, never naked, attempts, depth
//! ladder, exit plumbing, feed loss and the expiry-day slot.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use std::collections::BTreeMap;

use chrono::TimeDelta;
use rust_decimal::Decimal;

use baskfy_core::error::CoreResult;
use baskfy_core::options::config::{ExecutionConfig, OptionsConfig, Side, Sleeve};
use baskfy_core::options::execution::{
    EntryStep, LegRole, Level, SlotVerdict, advance_entry, drop_on_stale, entry_sequence,
    exit_sequence, feed_lost, first_by_precedence, never_naked, next_attempt, next_exit_leg,
    simulate_fill, slot_after_confirm, slot_after_release, slot_verdict,
};
use options_common::{LOT, Rng, dec, ist_dt, tick};

const LC: LegRole = LegRole::LongCall;
const LP: LegRole = LegRole::LongPut;
const SC: LegRole = LegRole::ShortCall;
const SP: LegRole = LegRole::ShortPut;
const CONDOR: [LegRole; 4] = [SC, LP, SP, LC];
const CALL_SPREAD: [LegRole; 2] = [SC, LC];
const PUT_SPREAD: [LegRole; 2] = [LP, SP];
const STRUCTURES: [&[LegRole]; 3] = [&CONDOR, &CALL_SPREAD, &PUT_SPREAD];

fn exe() -> ExecutionConfig {
    OptionsConfig::default().execution
}

fn stale_index() -> i64 {
    OptionsConfig::default().chain.stale_index_seconds
}

fn err_has<T: std::fmt::Debug>(r: CoreResult<T>, needle: &str) {
    let e = r.expect_err("expected an error");
    assert!(e.to_string().contains(needle), "{e} lacks {needle:?}");
}

#[test]
fn condor_entry_is_wings_first_puts_first() {
    assert_eq!(entry_sequence(&CONDOR).unwrap(), vec![LP, LC, SP, SC]);
}

#[test]
fn condor_exit_is_shorts_first_calls_first() {
    assert_eq!(exit_sequence(&CONDOR), vec![SC, SP, LC, LP]);
}

#[test]
fn debit_spread_long_in_first_short_out_first() {
    assert_eq!(entry_sequence(&CALL_SPREAD).unwrap(), vec![LC, SC]);
    assert_eq!(exit_sequence(&CALL_SPREAD), vec![SC, LC]);
    assert_eq!(entry_sequence(&PUT_SPREAD).unwrap(), vec![LP, SP]);
}

#[test]
fn a_long_option_is_one_leg() {
    assert_eq!(entry_sequence(&[LC]).unwrap(), vec![LC]);
    assert_eq!(exit_sequence(&[LC]), vec![LC]);
}

#[test]
fn a_naked_short_is_refused() {
    err_has(entry_sequence(&[SC, LP, SP]), "naked");
}

#[test]
fn a_duplicate_role_is_refused() {
    err_has(entry_sequence(&[LC, LC]), "one leg per role");
}

#[test]
fn leg_role_sides() {
    assert_eq!(
        (
            LC.entry_side(),
            LC.exit_side(),
            SP.entry_side(),
            SP.exit_side()
        ),
        (Side::Buy, Side::Sell, Side::Sell, Side::Buy)
    );
    assert_eq!(
        (SC.protector(), SP.protector(), LC.protector()),
        (Some(LC), Some(LP), None)
    );
}

fn seq() -> Vec<LegRole> {
    entry_sequence(&CONDOR).unwrap()
}

#[test]
fn full_fills_send_each_leg_then_open() {
    let s = seq();
    let steps: Vec<_> = (0..5)
        .map(|n| advance_entry(&s, LOT, &vec![LOT; n]).unwrap())
        .collect();
    let legs: Vec<LegRole> = steps[..4].iter().map(|a| a.leg.unwrap()).collect();
    assert_eq!(legs, s);
    assert_eq!(steps[4].step, EntryStep::Open);
}

#[test]
fn a_partial_wing_abandons_before_any_short() {
    let action = advance_entry(&seq(), LOT, &[LOT, 30]).unwrap();
    assert_eq!(action.step, EntryStep::Abandon);
    assert_eq!(action.close, vec![(LC, 30), (LP, LOT)]);
}

#[test]
fn a_cancelled_wing_closes_the_filled_one() {
    assert_eq!(
        advance_entry(&seq(), LOT, &[LOT, 0]).unwrap().close,
        vec![(LP, LOT)]
    );
}

#[test]
fn a_partial_short_does_not_send_the_other_short() {
    let action = advance_entry(&seq(), LOT, &[LOT, LOT, 20]).unwrap();
    assert_eq!(action.step, EntryStep::Abandon);
    assert_eq!(action.close, vec![(SP, 20), (LC, LOT), (LP, LOT)]);
}

#[test]
fn bad_outcomes_raise() {
    err_has(advance_entry(&[LC], LOT, &[LOT, LOT]), "more outcomes");
    err_has(advance_entry(&[LC], LOT, &[LOT + 1]), "filled");
}

fn walk_entry(roles: &[LegRole], qty: i64, fills: &[i64]) {
    let sequence = entry_sequence(roles).unwrap();
    let mut position: BTreeMap<LegRole, i64> = sequence.iter().map(|r| (*r, 0)).collect();
    let mut outcomes: Vec<i64> = Vec::new();
    let mut draws = fills.iter().copied();
    loop {
        let action = advance_entry(&sequence, qty, &outcomes).unwrap();
        match action.step {
            EntryStep::Open => {
                assert!(sequence.iter().all(|r| position[r] == qty));
                return;
            }
            EntryStep::Abandon => {
                for (role, filled) in &action.close {
                    assert_eq!(position[role], *filled);
                    position.insert(*role, 0);
                    assert!(never_naked(&position));
                }
                assert!(position.values().all(|v| *v == 0));
                return;
            }
            EntryStep::Send => {}
        }
        let leg = action.leg.expect("a SEND names its leg");
        if !leg.is_long() {
            assert!(
                sequence
                    .iter()
                    .filter(|r| r.is_long())
                    .all(|r| position[r] == qty),
                "a short before every long filled"
            );
        }
        let filled = draws.next().unwrap_or(qty).min(qty);
        position.insert(leg, filled);
        outcomes.push(filled);
        assert!(never_naked(&position));
    }
}

fn walk_exit(roles: &[LegRole], qty: i64, fills: &[i64]) {
    let mut position: Vec<(LegRole, i64)> = roles.iter().map(|r| (*r, qty)).collect();
    let get = |p: &[(LegRole, i64)], r: LegRole| p.iter().find(|(x, _)| *x == r).map(|(_, q)| *q);
    let mut draws = fills.iter().copied();
    for _ in 0..200 {
        let Some(leg) = next_exit_leg(&position) else {
            assert!(position.iter().all(|(_, v)| *v == 0));
            return;
        };
        if leg.is_long() {
            for s in [SC, SP] {
                if s.protector() == Some(leg)
                    && let Some(q) = get(&position, s)
                {
                    assert_eq!(q, 0);
                }
            }
        }
        let slot = position.iter_mut().find(|(r, _)| *r == leg).unwrap();
        slot.1 -= draws.next().unwrap_or(qty).min(slot.1);
        let map: BTreeMap<LegRole, i64> = position.iter().copied().collect();
        assert!(never_naked(&map));
    }
    panic!("exit did not reach flat");
}

fn draw_list(rng: &mut Rng, max_len: usize, lo: i64, hi: i64) -> Vec<i64> {
    let len = rng.below(max_len + 1);
    let span = usize::try_from(hi - lo + 1).unwrap();
    (0..len)
        .map(|_| {
            // Half the draws are a full fill, so walks reach every leg as Hypothesis's do.
            if rng.below(2) == 0 {
                hi
            } else {
                lo + i64::try_from(rng.below(span)).unwrap()
            }
        })
        .collect()
}

#[test]
fn never_naked_through_entry() {
    for (i, roles) in STRUCTURES.iter().enumerate() {
        let mut rng = Rng(0x9E37_79B9_7F4A_7C15 ^ (i as u64 + 1));
        walk_entry(roles, LOT, &[]);
        for _ in 0..500 {
            let fills = draw_list(&mut rng, 12, 0, LOT);
            walk_entry(roles, LOT, &fills);
        }
    }
}

#[test]
fn never_naked_through_exit() {
    for (i, roles) in STRUCTURES.iter().enumerate() {
        let mut rng = Rng(0xD1B5_4A32_D192_ED03 ^ (i as u64 + 1));
        walk_exit(roles, LOT, &[]);
        walk_exit(roles, LOT, &[1; 40]);
        for _ in 0..500 {
            let fills = draw_list(&mut rng, 40, 1, LOT);
            walk_exit(roles, LOT, &fills);
        }
    }
}

#[test]
fn next_exit_leg_holds_a_wing_while_its_short_is_open() {
    assert_eq!(
        next_exit_leg(&[(SC, 0), (SP, 10), (LC, LOT), (LP, LOT)]),
        Some(SP)
    );
    assert_eq!(
        next_exit_leg(&[(SC, 0), (SP, 0), (LC, LOT), (LP, LOT)]),
        Some(LC)
    );
    assert_eq!(next_exit_leg(&[(LC, 0)]), None);
}

fn attempt_at(n: i64, side: Side, reduces: bool, cfg: &ExecutionConfig) -> Option<Decimal> {
    next_attempt(n, side, dec("100.00"), dec("100.50"), tick(), reduces, cfg)
        .unwrap()
        .map(|a| a.price)
}

#[test]
fn entry_buy_improves_then_reprices_then_cancels() {
    assert_eq!(attempt_at(1, Side::Buy, false, &exe()), Some(dec("100.55")));
    assert_eq!(attempt_at(2, Side::Buy, false, &exe()), Some(dec("100.60")));
    assert_eq!(attempt_at(3, Side::Buy, false, &exe()), None);
}

#[test]
fn entry_sell() {
    assert_eq!(attempt_at(1, Side::Sell, false, &exe()), Some(dec("99.95")));
    assert_eq!(attempt_at(2, Side::Sell, false, &exe()), Some(dec("99.90")));
}

#[test]
fn a_risk_reducing_close_gets_a_marketable_limit_at_the_touch() {
    let attempt = next_attempt(
        3,
        Side::Buy,
        dec("100.00"),
        dec("100.50"),
        tick(),
        true,
        &exe(),
    )
    .unwrap()
    .unwrap();
    assert_eq!((attempt.price, attempt.marketable), (dec("100.50"), true));
    assert_eq!(attempt_at(4, Side::Buy, true, &exe()), None);
}

#[test]
fn the_flag_turns_the_third_attempt_off() {
    let off = ExecutionConfig {
        exit_final_marketable: false,
        ..ExecutionConfig::default()
    };
    assert_eq!(attempt_at(3, Side::Buy, true, &off), None);
}

#[test]
fn a_sell_never_prices_below_a_tick() {
    let attempt = next_attempt(
        2,
        Side::Sell,
        dec("0.05"),
        dec("0.10"),
        tick(),
        false,
        &exe(),
    )
    .unwrap()
    .unwrap();
    assert_eq!(attempt.price, tick());
}

#[test]
fn attempts_count_from_one() {
    err_has(
        next_attempt(
            0,
            Side::Buy,
            dec("100.00"),
            dec("100.50"),
            tick(),
            false,
            &exe(),
        ),
        "from 1",
    );
}

fn lv(p: &str, q: i64) -> Level {
    Level {
        price: dec(p),
        quantity: q,
    }
}

fn asks() -> Vec<Level> {
    vec![lv("100.00", 65), lv("100.05", 65), lv("100.10", 100)]
}

#[test]
fn two_levels_fill_in_full() {
    let got = simulate_fill(Side::Buy, &asks(), 130, dec("100.20"), tick(), &exe()).unwrap();
    assert_eq!(
        (
            got.filled,
            got.avg_price,
            got.levels_consumed,
            got.complete()
        ),
        (130, Some(dec("100.05")), 2, true)
    );
}

#[test]
fn the_limit_stops_the_walk_and_the_rest_is_partial() {
    let got = simulate_fill(Side::Buy, &asks(), 130, dec("100.05"), tick(), &exe()).unwrap();
    assert_eq!(
        (got.filled, got.avg_price, got.complete()),
        (65, Some(dec("100.00")), false)
    );
}

#[test]
fn a_sell_walks_the_bids_downward() {
    let bids = [lv("99.95", 65), lv("99.90", 65)];
    let got = simulate_fill(Side::Sell, &bids, 130, dec("99.80"), tick(), &exe()).unwrap();
    assert_eq!(got.avg_price, Some(dec("99.90")));
}

#[test]
fn no_depth_no_fill_and_no_ltp_fallback() {
    let got = simulate_fill(Side::Buy, &[], 65, dec("999"), tick(), &exe()).unwrap();
    assert_eq!((got.filled, got.avg_price), (0, None));
}

#[test]
fn empty_levels_are_skipped() {
    let ladder = [lv("100", 0), lv("100.05", 65)];
    let got = simulate_fill(Side::Buy, &ladder, 65, dec("100.05"), tick(), &exe()).unwrap();
    assert_eq!(got.avg_price, Some(dec("100.05")));
}

#[test]
fn quantity_must_be_positive() {
    err_has(
        simulate_fill(Side::Buy, &asks(), 0, Decimal::ONE, tick(), &exe()),
        "positive",
    );
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum O2 {
    HardExit,
    Stop,
    Invalidated,
    Target,
    TimeStop,
}

const O2_PRECEDENCE: [O2; 5] = [
    O2::HardExit,
    O2::Stop,
    O2::Invalidated,
    O2::Target,
    O2::TimeStop,
];

#[test]
fn precedence() {
    assert_eq!(
        first_by_precedence(&[O2::Target, O2::Stop], &O2_PRECEDENCE).unwrap(),
        Some(O2::Stop)
    );
    assert_eq!(
        first_by_precedence(&[O2::TimeStop, O2::HardExit], &O2_PRECEDENCE).unwrap(),
        Some(O2::HardExit)
    );
    assert_eq!(first_by_precedence(&[], &O2_PRECEDENCE).unwrap(), None);
}

#[test]
fn an_unranked_code_raises() {
    err_has(
        first_by_precedence(&[O2::Stop], &[O2::HardExit]),
        "precedence",
    );
}

#[test]
fn a_stale_quote_never_takes_profit() {
    let fired = [O2::Target, O2::Stop];
    assert_eq!(drop_on_stale(&fired, &[O2::Target], true), vec![O2::Stop]);
    assert_eq!(
        drop_on_stale(&fired, &[O2::Target], false),
        vec![O2::Target, O2::Stop]
    );
}

fn lost(
    sleeve: Sleeve,
    now: (u32, u32, u32),
    tick_ago: Option<i64>,
    opened_ago: Option<i64>,
) -> bool {
    let at = ist_dt(2026, 10, 27, now.0, now.1, now.2);
    let tick_at = tick_ago.map(|s| at - TimeDelta::seconds(s));
    let opened_at = opened_ago.map(|s| at - TimeDelta::seconds(s));
    feed_lost(
        sleeve,
        &at,
        tick_at.as_ref(),
        opened_at.as_ref(),
        stale_index(),
        &exe(),
    )
}

#[test]
fn o1_before_1400_is_not_lost() {
    assert!(!lost(Sleeve::O1m, (13, 59, 59), Some(31), Some(3600)));
}

#[test]
fn o1_after_1400_silent_is_lost() {
    assert!(lost(Sleeve::O1m, (14, 0, 0), Some(31), Some(3600)));
    assert!(lost(Sleeve::O3a, (14, 10, 0), None, Some(3600)));
}

#[test]
fn a_recent_tick_is_fine() {
    assert!(!lost(Sleeve::O1w, (14, 10, 0), Some(30), Some(3600)));
}

#[test]
fn no_position_no_exit() {
    assert!(!lost(Sleeve::O1m, (14, 10, 0), None, None));
}

#[test]
fn o2_any_time_after_the_grace() {
    assert!(lost(Sleeve::O2, (11, 0, 0), Some(31), Some(61)));
    assert!(!lost(Sleeve::O2, (11, 0, 0), Some(31), Some(60)));
}

#[test]
fn o2_is_outside() {
    assert_eq!(
        slot_verdict(Sleeve::O2, Some(Sleeve::O1m)),
        SlotVerdict::OutsideSlot
    );
    assert_eq!(
        slot_after_confirm(Sleeve::O2, Some(Sleeve::O1m)).unwrap(),
        Some(Sleeve::O1m)
    );
}

#[test]
fn first_confirmed_holds_it() {
    assert_eq!(slot_verdict(Sleeve::O3b, None), SlotVerdict::Free);
    let holder = slot_after_confirm(Sleeve::O3b, None).unwrap();
    assert_eq!(holder, Some(Sleeve::O3b));
    assert_eq!(slot_verdict(Sleeve::O1w, holder), SlotVerdict::SlotTaken);
    assert_eq!(slot_verdict(Sleeve::O3b, holder), SlotVerdict::HeldBySelf);
    err_has(slot_after_confirm(Sleeve::O1w, holder), "held by");
}

#[test]
fn release_frees_only_its_own() {
    assert_eq!(slot_after_release(Sleeve::O1m, Some(Sleeve::O1m)), None);
    assert_eq!(
        slot_after_release(Sleeve::O3a, Some(Sleeve::O1m)),
        Some(Sleeve::O1m)
    );
}
