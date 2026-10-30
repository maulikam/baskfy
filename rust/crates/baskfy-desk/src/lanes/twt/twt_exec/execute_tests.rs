//! `tests/test_twt_execute.py`, whole: TW6's execute logic driven through the REAL gateway
//! against a broker that explodes, over the in-memory store, asserting `docs/twt/02` (the law)
//! and `04` §§6, 7, 10 (the numbers) rather than any SQL. The sleeve's flag is false and the desk
//! is in dry run for every test here (`_flag_is_false`); **no test flips either**, and the spy
//! asserts on every call that the gateway answered a dry run — **0 orders reach a broker.**
//!
//! What is asserted here that nothing else asserts: the two failure halves of the ratchet
//! (`TestTheTwoFailureHalves`), driven through a gateway that fails on purpose.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use chrono::{DateTime, Duration, FixedOffset};
use rust_decimal::Decimal;

use super::execute::{self as x, ExecError, ExecOutcome, ExecResult, OrderReport};
use super::memory::{
    MemoryStore, Pos, a_position, a_store, a_store_with, d, now, session, twenty_five_lakh,
};
use super::rig::{Flaky, Hand, Kind, Spy, TAPE, dry, flags};
use crate::gateway::Gateway;
use crate::lanes::twt::common::{Rec, Val};

/// `confirm(store, plan_id, line_id, **kwargs)` over `gateway` (`gateway()` when `None`).
async fn confirm_on(
    store: &MemoryStore,
    gateway: Option<&dyn Gateway>,
    plan_id: &str,
    line_id: i64,
    confirm: &str,
    at: DateTime<FixedOffset>,
    last_price: Option<Decimal>,
) -> ExecResult<ExecOutcome> {
    let settings = dry();
    let mut handle = store.clone();
    match gateway {
        Some(g) => {
            x::execute_line(
                &mut handle,
                g,
                &settings,
                plan_id,
                line_id,
                confirm,
                at,
                last_price,
            )
            .await
        }
        None => {
            let spy = Spy::new(&settings).await;
            x::execute_line(
                &mut handle,
                &spy,
                &settings,
                plan_id,
                line_id,
                confirm,
                at,
                last_price,
            )
            .await
        }
    }
}

/// `confirm(store, plan_id, line_id, last_price=...)`.
async fn confirm(
    store: &MemoryStore,
    plan_id: &str,
    line_id: i64,
    last_price: Option<&str>,
) -> ExecResult<ExecOutcome> {
    confirm_on(
        store,
        None,
        plan_id,
        line_id,
        "true",
        now(),
        last_price.map(d),
    )
    .await
}

/// `confirm(..., gateway=g, last_price=...)`.
async fn confirm_via(
    store: &MemoryStore,
    g: &dyn Gateway,
    plan_id: &str,
    line_id: i64,
    last_price: Option<&str>,
) -> ExecResult<ExecOutcome> {
    confirm_on(
        store,
        Some(g),
        plan_id,
        line_id,
        "true",
        now(),
        last_price.map(d),
    )
    .await
}

fn status_of(e: &ExecError) -> u16 {
    match e {
        ExecError::Http(h) => h.status,
        other => panic!("expected an HTTPException, got {other:?}"),
    }
}

fn detail_of(e: &ExecError) -> String {
    match e {
        ExecError::Http(h) => h.detail.clone(),
        other => panic!("expected an HTTPException, got {other:?}"),
    }
}

fn dec(v: &Val) -> Decimal {
    match v {
        Val::Dec(x) => *x,
        other => panic!("not a Decimal: {other:?}"),
    }
}

// =============================================================================================
// G2 — nothing fires without the click
// =============================================================================================

#[tokio::test]
async fn test_a_missing_confirm_is_a_four_hundred() {
    let (store, plan_id, line_id) = a_store("BUY_AT_OPEN");
    let e = confirm_on(&store, None, &plan_id, line_id, "false", now(), None)
        .await
        .unwrap_err();
    assert_eq!(status_of(&e), 400);
    assert!(store.m().orders.is_empty());
}

#[tokio::test]
async fn test_an_unknown_plan_is_a_four_oh_four() {
    let (store, _plan_id, line_id) = a_store("BUY_AT_OPEN");
    let other = "99999999-8888-4777-8666-555555555555";
    let e = confirm(&store, other, line_id, None).await.unwrap_err();
    assert_eq!(status_of(&e), 404);
    assert!(store.m().orders.is_empty());
}

#[tokio::test]
async fn test_an_expired_plan_is_a_four_ten_and_nothing_is_sent() {
    let (store, plan_id, line_id) = a_store("BUY_AT_OPEN");
    let e = confirm_on(
        &store,
        None,
        &plan_id,
        line_id,
        "true",
        now() + Duration::minutes(31),
        None,
    )
    .await
    .unwrap_err();
    assert_eq!(status_of(&e), 410);
    assert!(detail_of(&e).contains("expired"));
    assert!(store.m().orders.is_empty());
}

#[tokio::test]
async fn test_a_plan_expired_by_one_second_is_still_expired() {
    let (store, plan_id, line_id) = a_store("BUY_AT_OPEN");
    let at = now() + Duration::minutes(25) + Duration::seconds(1);
    let e = confirm_on(&store, None, &plan_id, line_id, "true", at, None)
        .await
        .unwrap_err();
    assert_eq!(status_of(&e), 410);
}

#[tokio::test]
async fn test_a_sell_at_open_line_for_a_name_the_sleeve_does_not_hold_is_blocked() {
    let (store, plan_id, line_id) = a_store_with(
        "SELL_AT_OPEN",
        twenty_five_lakh(),
        &[("quantity", Val::Int(100))],
    );
    let outcome = confirm(&store, &plan_id, line_id, None).await.unwrap();
    assert_eq!(outcome.status, "BLOCKED");
    assert!(
        outcome.reason.contains("not in this sleeve's book"),
        "{}",
        outcome.reason
    );
}

#[tokio::test]
async fn test_the_whole_confirm_runs_under_the_session_lock() {
    let (store, plan_id, line_id) = a_store("BUY_AT_OPEN");
    confirm(&store, &plan_id, line_id, Some("100.00"))
        .await
        .unwrap();
    assert_eq!(store.m().locked, vec![session()]);
    // R8.TD1: the lock's exit runs once the body is done.
    assert_eq!(store.m().unlocked, 1);
}

// =============================================================================================
// G6 — a line is confirmed once, and the key is `plan_id:symbol`
// =============================================================================================

#[tokio::test]
async fn test_a_second_confirm_of_the_same_line_is_refused() {
    let (store, plan_id, line_id) = a_store("BUY_AT_OPEN");
    confirm(&store, &plan_id, line_id, Some("100.00"))
        .await
        .unwrap();
    let e = confirm(&store, &plan_id, line_id, Some("100.00"))
        .await
        .unwrap_err();
    assert_eq!(status_of(&e), 409);
    assert_eq!(store.m().orders.len(), 1);
}

#[tokio::test]
async fn test_the_orders_client_id_is_plan_id_colon_symbol() {
    let (store, plan_id, line_id) = a_store("BUY_AT_OPEN");
    confirm(&store, &plan_id, line_id, Some("100.00"))
        .await
        .unwrap();
    let m = store.m();
    assert_eq!(m.orders[0].s("client_id"), format!("{plan_id}:TWTCO"));
}

#[tokio::test]
async fn test_a_gtt_legs_client_id_carries_its_kind() {
    let (store, plan_id, line_id) = a_store("ARM_GTT");
    a_position(&store, &Pos::naked());
    confirm(&store, &plan_id, line_id, Some("100.00"))
        .await
        .unwrap();
    let mut m = store.m();
    let line = m.line(line_id);
    assert_eq!(line.s("client_id"), format!("{plan_id}:TWTCO:ARM_GTT"));
    assert_eq!(line.s("state"), "FILLED");
}

// =============================================================================================
// G5 — every fill arms a GTT in the same request, and the fourth entry is refused
// =============================================================================================

#[tokio::test]
async fn test_a_fill_arms_gtt_in_the_same_request() {
    let (store, plan_id, line_id) = a_store("BUY_AT_OPEN");
    let outcome = confirm(&store, &plan_id, line_id, Some("100.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "SIMULATED", "{}", outcome.reason);
    let position_id = outcome.position_id.unwrap();
    let mut m = store.m();
    let fills: Vec<String> = m.fills.iter().map(|f| f.s("side")).collect();
    let position = m.position(position_id);
    assert!(!position.is_none("gtt_id"));
    assert_eq!(dec(&position.val("gtt_trigger")), d("80.00")); // 20 % under the fill, on the tick
    assert_eq!(dec(&position.val("initial_stop")), d("80.00"));
    assert_eq!(dec(&position.val("high_since")), d("100.00"));
    assert_eq!(fills, vec!["BUY"]);
}

#[tokio::test]
async fn test_the_stop_comes_from_the_fill_and_not_from_the_plans_preview() {
    let (store, plan_id, line_id) = a_store("BUY_AT_OPEN");
    let outcome = confirm(&store, &plan_id, line_id, Some("120.00"))
        .await
        .unwrap();
    let mut m = store.m();
    let position = m.position(outcome.position_id.unwrap());
    assert_eq!(dec(&position.val("entry_avg")), d("120.00"));
    assert_eq!(dec(&position.val("initial_stop")), d("96.00"));
}

#[tokio::test]
async fn test_a_fourth_entry_in_one_session_is_blocked_by_the_session_cap() {
    let (store, plan_id, line_id) = a_store("BUY_AT_OPEN");
    for index in 0..3 {
        store.m().orders.push(Rec::of([
            ("id", Val::Int(900 + index)),
            ("side", Val::from("BUY")),
            ("signal_date", Val::Date(session())),
            ("state", Val::from("FILLED")),
            ("instrument_id", Val::Int(500 + index)),
            // LV10.2: sent this morning — the cap counts the day of sending, not the signal
            ("created_at", Val::Stamp(now())),
        ]));
    }
    let outcome = confirm(&store, &plan_id, line_id, Some("100.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "BLOCKED");
    assert!(
        outcome.reason.starts_with("SESSION_CAP"),
        "{}",
        outcome.reason
    );
    assert_eq!(store.m().line(line_id).s("state"), "REJECTED");
    assert_eq!(store.m().orders.len(), 3);
}

#[tokio::test]
async fn test_the_session_cap_counts_orders_from_any_plan() {
    let (store, plan_id, line_id) = a_store("BUY_AT_OPEN");
    store.m().orders.push(Rec::of([
        ("id", Val::Int(900)),
        ("side", Val::from("BUY")),
        ("signal_date", Val::Date(session())),
        ("state", Val::from("CONFIRMED")),
        ("instrument_id", Val::Int(77)),
    ]));
    assert_eq!(store.m().entries_taken(session()), 1);
    let outcome = confirm(&store, &plan_id, line_id, Some("100.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "SIMULATED", "{}", outcome.reason);
}

#[tokio::test]
async fn test_a_second_entry_in_a_held_name_is_allowed_and_a_third_is_refused() {
    let (store, plan_id, line_id) = a_store("BUY_AT_OPEN");
    a_position(&store, &Pos::default());
    let outcome = confirm(&store, &plan_id, line_id, Some("100.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "SIMULATED", "{}", outcome.reason);
    assert_eq!(store.m().open_positions().len(), 2);

    let (store2, plan_id2, line_id2) = a_store("BUY_AT_OPEN");
    a_position(&store2, &Pos::default());
    a_position(&store2, &Pos::default());
    let outcome = confirm(&store2, &plan_id2, line_id2, Some("100.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "BLOCKED");
    assert!(
        outcome.reason.starts_with("ALREADY_HELD") && outcome.reason.contains("2 open entries"),
        "{}",
        outcome.reason
    );
}

#[tokio::test]
async fn test_a_sleeve_at_zero_blocks_the_confirm_too() {
    let (store, plan_id, line_id) = a_store_with("BUY_AT_OPEN", d("0.00"), &[]);
    let outcome = confirm(&store, &plan_id, line_id, Some("100.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "BLOCKED");
    assert!(
        outcome.reason.starts_with("NO_SLEEVE_CAPITAL"),
        "{}",
        outcome.reason
    );
    assert!(store.m().orders.is_empty());
}

#[tokio::test]
async fn test_a_line_is_never_grown_past_what_the_page_showed() {
    let (store, plan_id, line_id) = a_store_with(
        "BUY_AT_OPEN",
        twenty_five_lakh(),
        &[
            ("quantity", Val::Int(100)),
            ("value_inr", Val::Dec(d("10000.00"))),
        ],
    );
    let outcome = confirm(&store, &plan_id, line_id, Some("100.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "SIMULATED", "{}", outcome.reason);
    assert_eq!(
        store
            .m()
            .position(outcome.position_id.unwrap())
            .int("quantity_open"),
        100
    );
}

#[tokio::test]
async fn test_the_countdown_does_not_move_on_a_simulated_fill() {
    let (store, plan_id, line_id) = a_store("BUY_AT_OPEN");
    confirm(&store, &plan_id, line_id, Some("100.00"))
        .await
        .unwrap();
    assert_eq!(store.m().config.int("first_live_entries_left"), 10);
    assert!(store.m().counted.is_empty());
}

// =============================================================================================
// G3 — a stop never falls
// =============================================================================================

fn raise(stop: &str) -> (MemoryStore, String, i64) {
    a_store_with(
        "RAISE_GTT_STOP",
        twenty_five_lakh(),
        &[("stop_price", Val::Dec(d(stop)))],
    )
}

#[tokio::test]
async fn test_a_stop_never_falls_a_raise_at_the_resting_trigger_is_blocked() {
    let (store, plan_id, line_id) = raise("80.00");
    a_position(&store, &Pos::default());
    let flaky = Flaky::ok();
    let outcome = confirm_via(&store, &flaky, &plan_id, line_id, Some("130.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "BLOCKED");
    assert!(
        outcome.reason.contains("a stop never falls"),
        "{}",
        outcome.reason
    );
    assert_eq!((flaky.cancelled(), flaky.armed()), (0, 0));
}

#[tokio::test]
async fn test_a_new_stop_below_the_resting_one_is_blocked() {
    let (store, plan_id, line_id) = raise("70.00");
    a_position(&store, &Pos::default());
    let flaky = Flaky::ok();
    let outcome = confirm_via(&store, &flaky, &plan_id, line_id, Some("130.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "BLOCKED");
    assert!(
        outcome.reason.contains("a stop never falls"),
        "{}",
        outcome.reason
    );
    assert_eq!(flaky.cancelled(), 0);
}

#[tokio::test]
async fn test_a_stop_never_falls_below_the_higher_of_the_stop_and_the_gtt() {
    let (store, plan_id, line_id) = raise("95.00");
    a_position(
        &store,
        &Pos {
            stop: "96.00",
            ..Pos::default()
        },
    );
    let flaky = Flaky::ok();
    let outcome = confirm_via(&store, &flaky, &plan_id, line_id, Some("130.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "BLOCKED");
    assert!(outcome.reason.contains("96.00"), "{}", outcome.reason);
}

#[tokio::test]
async fn test_a_trigger_at_or_above_last_price_is_blocked() {
    let (store, plan_id, line_id) = raise("130.00");
    a_position(&store, &Pos::default());
    let flaky = Flaky::ok();
    let outcome = confirm_via(&store, &flaky, &plan_id, line_id, Some("130.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "BLOCKED");
    assert!(
        outcome.reason.contains("would fire at once"),
        "{}",
        outcome.reason
    );
    assert_eq!((flaky.cancelled(), flaky.armed()), (0, 0));
}

#[tokio::test]
async fn test_a_trigger_with_no_last_price_to_check_it_above_last_is_blocked() {
    let (store, plan_id, line_id) = raise("104.00");
    a_position(&store, &Pos::default());
    let flaky = Flaky::ok();
    let outcome = confirm_via(&store, &flaky, &plan_id, line_id, None)
        .await
        .unwrap();
    assert_eq!(outcome.status, "BLOCKED");
    assert!(
        outcome.reason.contains("no last price"),
        "{}",
        outcome.reason
    );
    assert_eq!(flaky.cancelled(), 0);
}

#[tokio::test]
async fn test_a_good_raise_moves_the_stop_and_clears_the_spent_trigger() {
    let (store, plan_id, line_id) = raise("104.00");
    let position_id = a_position(&store, &Pos::default());
    {
        let mut m = store.m();
        let p = m.position(position_id);
        p.set("next_trigger", Val::Dec(d("104.00")));
        p.set("next_trigger_for", Val::Date(session()));
    }
    let flaky = Flaky::ok();
    let outcome = confirm_via(&store, &flaky, &plan_id, line_id, Some("130.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "SIMULATED", "{}", outcome.reason);
    let mut m = store.m();
    let ratchets = m.session(session()).ratchets;
    let position = m.position(position_id);
    assert_eq!(dec(&position.val("stop_price")), d("104.00"));
    assert_eq!(dec(&position.val("gtt_trigger")), d("104.00"));
    assert_eq!(position.val("gtt_id"), Val::from("998877"));
    assert!(position.is_none("next_trigger"));
    assert_eq!(ratchets, 1);
    // Delete first, then arm. Never two triggers on one position at the same time.
    assert_eq!((flaky.cancelled(), flaky.armed()), (1, 1));
}

// =============================================================================================
// G4 — the delete-and-replace path's two failure halves
// =============================================================================================

#[tokio::test]
async fn test_when_the_cancel_fails_the_old_stop_is_still_resting() {
    let (store, plan_id, line_id) = raise("104.00");
    let position_id = a_position(&store, &Pos::default());
    let flaky = Flaky::new(false, true);
    let outcome = confirm_via(&store, &flaky, &plan_id, line_id, Some("130.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "BLOCKED");
    assert!(!outcome.naked);
    assert!(
        outcome.reason.contains("still resting"),
        "{}",
        outcome.reason
    );
    let mut m = store.m();
    let position = m.position(position_id);
    assert_eq!(position.val("gtt_id"), Val::from("551234"));
    assert_eq!(dec(&position.val("gtt_trigger")), d("80.00"));
    assert_eq!(dec(&position.val("stop_price")), d("80.00"));
    assert_eq!(flaky.armed(), 0); // nothing was placed
}

#[tokio::test]
async fn test_when_the_arm_fails_after_a_good_cancel_the_line_is_named_naked() {
    let (store, plan_id, line_id) = raise("104.00");
    let position_id = a_position(&store, &Pos::default());
    let flaky = Flaky::new(true, false);
    let outcome = confirm_via(&store, &flaky, &plan_id, line_id, Some("130.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "BLOCKED");
    assert!(outcome.naked);
    assert!(outcome.reason.contains("NAKED"), "{}", outcome.reason);
    assert!(
        outcome.reason.contains(&format!("position {position_id}")),
        "{}",
        outcome.reason
    );
    let mut m = store.m();
    let position = m.position(position_id);
    assert!(position.is_none("gtt_id"));
    assert!(position.is_none("gtt_trigger"));
    // The INTENT is recorded, so the next re-arm rests the stop at the right level.
    assert_eq!(dec(&position.val("stop_price")), d("104.00"));
}

#[tokio::test]
async fn test_a_naked_line_is_what_the_sweep_then_finds() {
    let (store, plan_id, line_id) = raise("104.00");
    a_position(&store, &Pos::default());
    confirm_via(
        &store,
        &Flaky::new(true, false),
        &plan_id,
        line_id,
        Some("130.00"),
    )
    .await
    .unwrap();
    let mut handle = store.clone();
    let naked: Vec<String> = x::naked_positions(&mut handle)
        .await
        .unwrap()
        .iter()
        .map(|r| r.s("symbol"))
        .collect();
    assert_eq!(naked, vec!["TWTCO"]);
}

#[tokio::test]
async fn test_a_fill_whose_arm_fails_is_reported_naked_too() {
    let (store, plan_id, line_id) = a_store("BUY_AT_OPEN");
    let gw = Hand::new(Kind::BuyOkArmFails);
    let outcome = confirm_via(&store, &gw, &plan_id, line_id, Some("100.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "BLOCKED");
    assert!(outcome.naked);
    assert!(outcome.reason.contains("NAKED"), "{}", outcome.reason);
    assert!(
        store
            .m()
            .position(outcome.position_id.unwrap())
            .is_none("gtt_id")
    );
}

// =============================================================================================
// G8 (the module half) — the four chores: rearm, sweep, reconcile, halt
// =============================================================================================

async fn rearm(
    store: &MemoryStore,
    gw: &dyn Gateway,
    position_id: i64,
    confirm: &str,
    last_price: Option<&str>,
) -> ExecResult<ExecOutcome> {
    let mut handle = store.clone();
    x::rearm_gtt(
        &mut handle,
        gw,
        &dry(),
        position_id,
        confirm,
        now(),
        last_price.map(d),
    )
    .await
}

#[tokio::test]
async fn test_rearm_arms_a_naked_position() {
    let store = MemoryStore::default_store();
    let position_id = a_position(&store, &Pos::naked());
    let outcome = rearm(&store, &Flaky::ok(), position_id, "true", Some("130.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "SIMULATED", "{}", outcome.reason);
    assert_eq!(
        store.m().position(position_id).val("gtt_id"),
        Val::from("998877")
    );
}

#[tokio::test]
async fn test_rearm_refuses_a_position_that_already_has_a_trigger() {
    let store = MemoryStore::default_store();
    let position_id = a_position(&store, &Pos::default());
    let outcome = rearm(&store, &Flaky::ok(), position_id, "true", None)
        .await
        .unwrap();
    assert_eq!(outcome.status, "BLOCKED");
    assert!(
        outcome.reason.contains("already resting"),
        "{}",
        outcome.reason
    );
}

#[tokio::test]
async fn test_rearm_without_a_confirm_is_a_four_hundred() {
    let store = MemoryStore::default_store();
    let position_id = a_position(&store, &Pos::naked());
    let e = rearm(&store, &Flaky::ok(), position_id, "no", None)
        .await
        .unwrap_err();
    assert_eq!(status_of(&e), 400);
}

#[tokio::test]
async fn test_rearm_refuses_a_stop_at_or_above_the_last_price() {
    let store = MemoryStore::default_store();
    let position_id = a_position(&store, &Pos::naked());
    let outcome = rearm(&store, &Flaky::ok(), position_id, "true", Some("79.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "BLOCKED");
    assert!(
        outcome.reason.contains("is not protection"),
        "{}",
        outcome.reason
    );
}

async fn sweep(store: &MemoryStore, gw: &dyn Gateway, prices: &[(&str, &str)]) -> x::SweepReport {
    let prices: Vec<(String, Decimal)> = prices
        .iter()
        .map(|(s, p)| ((*s).to_owned(), d(p)))
        .collect();
    let mut handle = store.clone();
    x::sweep_naked(&mut handle, gw, &dry(), now(), &prices)
        .await
        .unwrap()
}

#[tokio::test]
async fn test_the_sweep_rearms_what_is_naked_and_counts_what_is_left() {
    let store = MemoryStore::default_store();
    a_position(
        &store,
        &Pos {
            instrument_id: 42,
            symbol: "GOODCO",
            ..Pos::naked()
        },
    );
    a_position(
        &store,
        &Pos {
            instrument_id: 43,
            symbol: "BADCO",
            ..Pos::naked()
        },
    );
    // BADCO has no quote: armed against its entry
    let report = sweep(&store, &Flaky::ok(), &[("GOODCO", "130.00")]).await;
    assert_eq!(report.naked_before, 2);
    assert_eq!(report.naked, 0);
    assert_eq!(store.m().session(now().date_naive()).naked, 0);
}

#[tokio::test]
async fn test_the_sweep_is_idempotent_on_the_same_day() {
    let store = MemoryStore::default_store();
    a_position(&store, &Pos::naked());
    let first = sweep(&store, &Flaky::ok(), &[("TWTCO", "130")]).await;
    let second = sweep(&store, &Flaky::ok(), &[("TWTCO", "130")]).await;
    assert_eq!(first.rearmed, 1);
    assert_eq!(
        (second.naked_before, second.rearmed, second.naked),
        (0, 0, 0)
    );
}

#[tokio::test]
async fn test_the_sweep_records_what_it_could_not_fix() {
    let store = MemoryStore::default_store();
    a_position(&store, &Pos::naked());
    let report = sweep(&store, &Flaky::new(true, false), &[("TWTCO", "130.00")]).await;
    assert_eq!(report.naked, 1);
    assert_eq!(store.m().session(now().date_naive()).naked, 1);
}

// --- `tools/twt/sweep.py`'s `Rearm` — one position in, one outcome out ------------------------

/// `rearm_callable(store, gateway, now=NOW, price_for=lambda _symbol: price)(position_id)`.
async fn rearm_callable(
    store: &MemoryStore,
    gw: &dyn Gateway,
    position_id: i64,
    price: &str,
) -> Rec {
    let mut handle = store.clone();
    x::rearm_one(&mut handle, gw, &dry(), position_id, now(), Some(d(price)))
        .await
        .unwrap()
}

#[tokio::test]
async fn test_the_callable_answers_the_shape_rearm_outcome_is_built_from() {
    let store = MemoryStore::default_store();
    let position_id = a_position(&store, &Pos::naked());
    let answer = rearm_callable(&store, &Flaky::ok(), position_id, "130.00").await;
    let keys: Vec<&str> = answer.0.iter().map(|(k, _)| k.as_str()).collect();
    assert_eq!(keys, vec!["position_id", "armed", "gtt_id", "reason"]);
    assert_eq!(answer.val("position_id"), Val::Int(position_id));
    assert_eq!(answer.val("armed"), Val::Bool(true));
    assert_eq!(answer.val("gtt_id"), Val::from("998877"));
    assert_eq!(answer.val("reason"), Val::from(""));
}

#[tokio::test]
async fn test_a_refusal_comes_back_armed_false_with_the_reason_to_read() {
    let store = MemoryStore::default_store();
    let position_id = a_position(&store, &Pos::naked());
    let answer = rearm_callable(&store, &Flaky::ok(), position_id, "79.00").await;
    assert_eq!(answer.val("armed"), Val::Bool(false));
    assert!(answer.is_none("gtt_id"));
    assert!(answer.s("reason").contains("is not protection"));
}

#[tokio::test]
async fn test_the_callable_never_places_a_buy() {
    let store = MemoryStore::default_store();
    let position_id = a_position(&store, &Pos::naked());
    // `Flaky.place` panics if anything asks it to buy.
    rearm_callable(&store, &Flaky::ok(), position_id, "130.00").await;
    assert!(store.m().orders.is_empty());
}

// --- reconcile -------------------------------------------------------------------------------

fn gtt(id: i64, symbol: &str, trigger: f64, status: Option<&str>) -> Rec {
    let mut r = Rec::of([
        ("gtt_id", Val::Int(id)),
        ("symbol", Val::from(symbol)),
        ("trigger", Val::Float(trigger)),
    ]);
    if let Some(s) = status {
        r.set("status", Val::from(s));
    }
    r
}

async fn reconcile(store: &MemoryStore, source: Option<Vec<Rec>>) -> x::ReconcileReport {
    let mut handle = store.clone();
    x::reconcile_gtts(&mut handle, source, now()).await.unwrap()
}

async fn naked(store: &MemoryStore) -> usize {
    let mut handle = store.clone();
    x::naked_positions(&mut handle).await.unwrap().len()
}

#[tokio::test]
async fn test_reconcile_attaches_a_hand_armed_gtt_to_its_position() {
    let store = MemoryStore::default_store();
    let position_id = a_position(&store, &Pos::naked());
    let report = reconcile(
        &store,
        Some(vec![gtt(771_122, "TWTCO", 81.5, Some("active"))]),
    )
    .await;
    assert_eq!(report.attached, 1);
    {
        let mut m = store.m();
        let position = m.position(position_id);
        assert_eq!(position.val("gtt_id"), Val::from("771122"));
        assert_eq!(dec(&position.val("gtt_trigger")), d("81.5"));
    }
    assert_eq!(naked(&store).await, 0);
}

#[tokio::test]
async fn test_reconcile_leaves_a_trigger_it_cannot_match_alone_and_counts_it() {
    let store = MemoryStore::default_store();
    let position_id = a_position(&store, &Pos::default());
    let report = reconcile(&store, Some(vec![gtt(771_122, "TWTCO", 79.0, None)])).await;
    assert_eq!(report.attached, 0);
    assert_eq!(report.unmatched, 1);
    assert_eq!(
        store.m().position(position_id).val("gtt_id"),
        Val::from("551234")
    );
}

#[tokio::test]
async fn test_another_books_resting_gtt_is_not_a_discrepancy() {
    let store = MemoryStore::default_store();
    a_position(&store, &Pos::default());
    let report = reconcile(
        &store,
        Some(vec![gtt(771_122, "SOMEONEELSECO", 10.0, None)]),
    )
    .await;
    assert_eq!(report.attached, 0);
    assert_eq!(report.unmatched, 0);
}

#[tokio::test]
async fn test_reconcile_without_a_broker_changes_nothing() {
    let store = MemoryStore::default_store();
    a_position(&store, &Pos::naked());
    assert_eq!(reconcile(&store, None).await.attached, 0);
    assert_eq!(naked(&store).await, 1);
}

// --- the halt: FIRST-LIVE-MORNING §7's four behaviours -----------------------------------------

async fn halt(store: &MemoryStore, confirm: &str) -> ExecResult<x::HaltReport> {
    let mut handle = store.clone();
    x::halt_sleeve(&mut handle, confirm, now()).await
}

#[tokio::test]
async fn test_halt_zeroes_the_capital_and_audits_the_previous_value() {
    let store = MemoryStore::default_store();
    let report = halt(&store, "true").await.unwrap();
    assert!(report.halted);
    assert_eq!(report.sleeve_capital_inr_before, Some(twenty_five_lakh()));
    let m = store.m();
    assert_eq!(dec(&m.config.val("sleeve_capital_inr")), Decimal::ZERO);
    let audit: Vec<&Rec> = m
        .audit
        .iter()
        .filter(|r| r.s("key") == "sleeve_capital_inr")
        .collect();
    assert_eq!(audit.len(), 1);
    assert_eq!(audit[0].s("old_value"), "2500000.00");
    assert_eq!(audit[0].s("new_value"), "0");
}

#[tokio::test]
async fn test_halt_expires_every_live_plan() {
    let (store, plan_id, line_id) = a_store("BUY_AT_OPEN");
    let report = halt(&store, "true").await.unwrap();
    assert_eq!(report.plans_expired, 1);
    let e = confirm(&store, &plan_id, line_id, Some("100.00"))
        .await
        .unwrap_err();
    assert_eq!(status_of(&e), 410);
}

#[tokio::test]
async fn test_halt_never_touches_protection() {
    let (store, plan_id, line_id) = raise("104.00");
    let position_id = a_position(&store, &Pos::default());
    let before = store.m().position(position_id).clone();

    halt(&store, "true").await.unwrap();

    {
        let mut m = store.m();
        let after = m.position(position_id).clone();
        assert_eq!(after.val("gtt_id"), before.val("gtt_id"));
        assert_eq!(after.val("gtt_id"), Val::from("551234"));
        assert_eq!(after.val("gtt_trigger"), before.val("gtt_trigger"));
        assert_eq!(dec(&after.val("gtt_trigger")), d("80.00"));
        assert_eq!(after.int("quantity_open"), before.int("quantity_open"));
        assert_eq!(after.s("state"), "OPEN");
        // And the ratchet still works — on a plan built after the halt.
        m.plan(&plan_id)
            .set("expires_at", Val::Stamp(now() + Duration::minutes(25)));
    }
    let flaky = Flaky::ok();
    let outcome = confirm_via(&store, &flaky, &plan_id, line_id, Some("130.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "SIMULATED", "{}", outcome.reason);
    assert_eq!(
        dec(&store.m().position(position_id).val("gtt_trigger")),
        d("104.00")
    );
}

#[tokio::test]
async fn test_a_halted_sleeve_cannot_buy() {
    let (store, plan_id, line_id) = a_store("BUY_AT_OPEN");
    halt(&store, "true").await.unwrap();
    store
        .m()
        .plan(&plan_id)
        .set("expires_at", Val::Stamp(now() + Duration::minutes(25)));
    let outcome = confirm(&store, &plan_id, line_id, Some("100.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "BLOCKED");
    assert!(
        outcome.reason.starts_with("NO_SLEEVE_CAPITAL"),
        "{}",
        outcome.reason
    );
    assert!(store.m().orders.is_empty());
}

#[tokio::test]
async fn test_halt_says_what_it_did_in_one_line() {
    let store = MemoryStore::default_store();
    let report = halt(&store, "true").await.unwrap();
    let line = report.line();
    assert_eq!(line.matches('\n').count(), 0);
    assert!(
        line.contains("\u{20b9}2500000.00") && line.contains("\u{20b9}0"),
        "{line}"
    );
    assert!(line.contains("untouched"), "{line}");
}

#[tokio::test]
async fn test_halt_without_a_confirm_is_a_four_hundred() {
    let store = MemoryStore::default_store();
    let e = halt(&store, "").await.unwrap_err();
    assert_eq!(status_of(&e), 400);
    assert_eq!(
        dec(&store.m().config.val("sleeve_capital_inr")),
        twenty_five_lakh()
    );
}

// =============================================================================================
// Fills that arrive later
// =============================================================================================

fn a_sent_buy(store: &MemoryStore, broker_order_id: &str, client_id: &str) -> i64 {
    let mut m = store.m();
    let id = m.id();
    m.orders.push(Rec::of([
        ("id", Val::Int(id)),
        ("filled_quantity", Val::Int(0)),
        ("position_id", Val::None),
        ("symbol", Val::from("TWTCO")),
        ("instrument_id", Val::Int(42)),
        ("signal_date", Val::Date(session())),
        ("side", Val::from("BUY")),
        ("quantity", Val::Int(100)),
        ("stop_price", Val::Dec(d("80.00"))),
        ("state", Val::from("SENT")),
        ("broker_order_id", Val::from(broker_order_id)),
        ("client_id", Val::from(client_id)),
        ("simulated", Val::Bool(false)),
    ]));
    id
}

async fn update(
    store: &MemoryStore,
    gw: &dyn Gateway,
    report: OrderReport,
) -> ExecResult<Option<ExecOutcome>> {
    let mut handle = store.clone();
    x::on_order_update(&mut handle, gw, &dry(), &report, now()).await
}

#[tokio::test]
async fn test_a_completed_buy_becomes_the_position_the_fill_and_the_gtt() {
    let store = MemoryStore::default_store();
    let order_row = a_sent_buy(&store, "250911000001", "x:TWTCO");
    let outcome = update(
        &store,
        &Flaky::ok(),
        OrderReport {
            order_id: "250911000001".to_owned(),
            status: "COMPLETE".to_owned(),
            filled_quantity: 100,
            average_price: Some(d("101.00")),
            status_message: None,
        },
    )
    .await
    .unwrap()
    .unwrap();
    let mut m = store.m();
    let position = m.position(outcome.position_id.unwrap()).clone();
    assert_eq!(position.int("quantity_open"), 100);
    assert_eq!(dec(&position.val("entry_avg")), d("101.00"));
    assert_eq!(dec(&position.val("initial_stop")), d("80.80"));
    assert_eq!(position.val("gtt_id"), Val::from("998877"));
    assert_eq!(m.order(order_row).s("state"), "FILLED");
}

#[tokio::test]
async fn test_an_order_this_sleeve_does_not_own_is_ignored() {
    let store = MemoryStore::default_store();
    let out = update(
        &store,
        &Flaky::ok(),
        OrderReport {
            order_id: "nope".to_owned(),
            status: "COMPLETE".to_owned(),
            ..OrderReport::default()
        },
    )
    .await
    .unwrap();
    assert!(out.is_none());
}

#[tokio::test]
async fn test_an_open_order_is_not_a_fill() {
    let store = MemoryStore::default_store();
    a_sent_buy(&store, "1", "x");
    let out = update(
        &store,
        &Flaky::ok(),
        OrderReport {
            order_id: "1".to_owned(),
            status: "OPEN".to_owned(),
            ..OrderReport::default()
        },
    )
    .await
    .unwrap();
    assert!(out.is_none());
}

// =============================================================================================
// G7 — the spy: 0 orders reach a broker in the whole suite
// =============================================================================================

#[test]
fn test_the_gates_are_cnc_only_and_dry_run_while_the_flag_is_false() {
    let gates = x::twt_gates(&dry());
    assert!(gates.dry_run);
    assert!(!gates.intraday_enabled);
    assert!(!gates.options_enabled);
}

#[test]
fn test_both_flags_are_false_by_default_and_auto_execute_is_the_one_named_setting() {
    let s = crate::settings::Settings::defaults().unwrap();
    assert!(!s.twt_execution_enabled);
    assert!(!s.twt_auto_execute);
    // `[name for name in dir(config) if name.startswith("TWT_AUTO")] == ["TWT_AUTO_EXECUTE"]`:
    // the settings' own source names exactly one such field.
    let source = include_str!("../../../settings.rs");
    let mut named: Vec<&str> = source
        .split(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
        .filter(|w| w.starts_with("twt_auto"))
        .collect();
    named.sort_unstable();
    named.dedup();
    assert_eq!(named, vec!["twt_auto_execute"]);
}

#[tokio::test]
async fn test_no_broker_client_method_is_ever_invoked_by_a_confirm() {
    let (store, plan_id, line_id) = a_store("BUY_AT_OPEN");
    let spy = Spy::new(&dry()).await;
    let outcome = confirm_via(&store, &spy, &plan_id, line_id, Some("100.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "SIMULATED", "{}", outcome.reason);
    // `_CountingKC`: not one of place_order, place_gtt, delete_gtt, cancel_order was touched —
    // and no read either (`instruments` is ExplodingKC's fifth trap).
    assert!(spy.kite.write_calls().is_empty());
    assert!(spy.kite.calls().is_empty(), "{:?}", spy.kite.calls());
}

#[tokio::test]
async fn test_the_buy_is_cnc_market_and_never_intraday() {
    let (store, plan_id, line_id) = a_store("BUY_AT_OPEN");
    let gw = Hand::new(Kind::Recorder);
    confirm_via(&store, &gw, &plan_id, line_id, Some("100.00"))
        .await
        .unwrap();
    let seen = gw.seen();
    assert_eq!(seen[0].product, "CNC");
    assert_eq!(seen[0].order_type, "MARKET");
    assert_eq!(seen[0].side, "BUY");
}

#[tokio::test]
async fn test_the_spy_saw_no_place_answer_anything_but_dry_run() {
    let (store, plan_id, line_id) = a_store("BUY_AT_OPEN");
    let outcome = confirm(&store, &plan_id, line_id, Some("100.00"))
        .await
        .unwrap();
    assert!(outcome.simulated);
    let tape = TAPE.lock().unwrap().clone();
    assert!(tape.contains(&("place".to_owned(), "DRY_RUN".to_owned())));
    assert!(
        tape.iter().all(|(_, s)| s.starts_with("DRY_RUN")),
        "{tape:?}"
    );
    assert!(!tape.iter().any(|(_, s)| s == "PLACED" || s == "GTT_PLACED"));
}

// =============================================================================================
// LV9 — Qullamaggie's sale at the open (DECISIONS-TW TW20)
// =============================================================================================

/// `TestTheSellAtOpen._held(store, partial_quantity=..., exit_reason=...)`.
fn held(store: &MemoryStore, partial_quantity: Option<i64>, exit_reason: Option<&str>) -> i64 {
    let position_id = a_position(
        store,
        &Pos {
            quantity: 300,
            entry: "100.00",
            stop: "80.00",
            gtt_id: Some("DRY-1"),
            gtt_trigger: Some("80.00"),
            ..Pos::default()
        },
    );
    let mut m = store.m();
    let p = m.position(position_id);
    p.update(&Rec::of([
        ("partial_done", Val::Bool(false)),
        ("trail", Val::from("MA20")),
        (
            "partial_queued_for",
            if partial_quantity.is_some_and(|q| q != 0) {
                Val::Date(session())
            } else {
                Val::None
            },
        ),
        ("partial_quantity", Val::from(partial_quantity)),
        (
            "exit_queued_for",
            if exit_reason.is_some_and(|r| !r.is_empty()) {
                Val::Date(session())
            } else {
                Val::None
            },
        ),
        ("exit_reason_queued", Val::from(exit_reason)),
        ("quantity_entered", Val::Int(300)),
    ]));
    position_id
}

fn sell(quantity: i64) -> (MemoryStore, String, i64) {
    a_store_with(
        "SELL_AT_OPEN",
        twenty_five_lakh(),
        &[("quantity", Val::Int(quantity))],
    )
}

#[tokio::test]
async fn test_the_partial_sells_a_third_at_market_with_protection_and_keeps_the_position() {
    let (store, plan_id, line_id) = sell(100);
    let position_id = held(&store, Some(100), None);
    store
        .m()
        .line(line_id)
        .set("position_id", Val::Int(position_id));

    let spy = Spy::new(&dry()).await;
    let tape_before = TAPE.lock().unwrap().len();
    let outcome = confirm_via(&store, &spy, &plan_id, line_id, Some("110.00"))
        .await
        .unwrap();

    assert_eq!(outcome.status, "SIMULATED", "{}", outcome.reason);
    assert_eq!(outcome.position_id, Some(position_id));
    let mut m = store.m();
    let fills = m.fills.clone();
    let position = m.position(position_id).clone();
    assert_eq!(position.int("quantity_open"), 200);
    assert_eq!(position.s("state"), "OPEN");
    assert_eq!(position.val("partial_done"), Val::Bool(true));
    assert!(position.is_none("partial_quantity"));
    let sides: Vec<String> = fills.iter().map(|f| f.s("side")).collect();
    assert_eq!(sides, vec!["SELL"]);
    assert_eq!(fills[0].int("quantity"), 100);
    assert_eq!(dec(&fills[0].val("price")), d("110.00"));
    assert_eq!(m.line(line_id).s("state"), "FILLED");
    drop(m);
    let tape = TAPE.lock().unwrap().clone();
    assert!(
        tape.len() > tape_before && tape.iter().any(|(c, _)| c == "place"),
        "the sell went through the gateway"
    );
}

#[tokio::test]
async fn test_the_gateway_is_asked_for_a_market_sell_with_kite_protection() {
    let (store, plan_id, line_id) = sell(100);
    held(&store, Some(100), None);
    let gw = Hand::new(Kind::Recording);
    confirm_via(&store, &gw, &plan_id, line_id, Some("110.00"))
        .await
        .unwrap();
    let seen = gw.seen();
    assert_eq!(seen[0].side, "SELL");
    assert_eq!(seen[0].order_type, "MARKET");
    assert_eq!(seen[0].market_protection, Some(x::TWT_MARKET_PROTECTION));
    assert_eq!(seen[0].qty, 100);
}

#[tokio::test]
async fn test_the_remainder_on_a_trail_exit_closes_the_position_with_the_reason() {
    let (store, plan_id, line_id) = sell(300);
    let position_id = held(&store, None, Some("MA_TRAIL"));
    let outcome = confirm(&store, &plan_id, line_id, Some("95.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "SIMULATED", "{}", outcome.reason);
    let mut m = store.m();
    let exits = m.session(session()).exits;
    let position = m.position(position_id);
    assert_eq!(position.s("state"), "CLOSED");
    assert_eq!(position.s("close_reason"), "MA_TRAIL");
    assert_eq!(position.int("quantity_open"), 0);
    assert_eq!(dec(&position.val("exit_avg")), d("95.00"));
    assert_eq!(dec(&position.val("pnl_inr")), d("-1500.00"));
    assert_eq!(exits, 1);
}

#[tokio::test]
async fn test_never_more_than_the_sleeve_holds() {
    let (store, plan_id, line_id) = sell(301);
    held(&store, None, Some("MA_TRAIL"));
    let outcome = confirm(&store, &plan_id, line_id, Some("95.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "BLOCKED");
    assert!(outcome.reason.contains("holds 300"), "{}", outcome.reason);
}

#[tokio::test]
async fn test_a_live_sell_is_recorded_and_booked_only_from_the_brokers_fill() {
    let (store, plan_id, line_id) = sell(100);
    let position_id = held(&store, Some(100), None);
    let gw = Hand::new(Kind::Accepting);
    let outcome = confirm_via(&store, &gw, &plan_id, line_id, Some("110.00"))
        .await
        .unwrap();
    assert_eq!(outcome.status, "SENT", "{}", outcome.reason);
    {
        let mut m = store.m();
        assert_eq!(
            m.position(position_id).int("quantity_open"),
            300,
            "nothing booked yet"
        );
        assert_eq!(m.line(line_id).s("state"), "SENT");
        let exit_order = &m.exit_orders[0];
        assert_eq!(
            (
                exit_order.s("reason"),
                exit_order.int("quantity"),
                exit_order.s("sleeve")
            ),
            ("PARTIAL".to_owned(), 100, "twt".to_owned())
        );
    }

    let result = update(
        &store,
        &gw,
        OrderReport {
            order_id: "BRK-SELL-1".to_owned(),
            status: "COMPLETE".to_owned(),
            filled_quantity: 100,
            // `"average_price": 111.5` — a float on the wire.
            average_price: Some(d("111.5")),
            status_message: None,
        },
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(result.status, "FILLED", "{}", result.reason);
    let mut m = store.m();
    let last_fill = m.fills.last().unwrap().clone();
    let exit_state = m.exit_orders[0].s("state");
    let position = m.position(position_id).clone();
    assert_eq!(position.int("quantity_open"), 200);
    assert_eq!(position.val("partial_done"), Val::Bool(true));
    assert_eq!(last_fill.int("quantity"), 100);
    let price = dec(&last_fill.val("price"));
    assert_eq!(price, d("111.5000"));
    assert_eq!(price.to_string(), "111.5000");
    assert_eq!(exit_state, "FILLED");
    assert_eq!(m.line(line_id).s("state"), "FILLED");
}

#[tokio::test]
async fn test_the_line_names_its_position_when_a_name_holds_two() {
    let (store, plan_id, line_id) = sell(100);
    let first = held(&store, None, None);
    let second = held(&store, Some(100), None);
    store.m().line(line_id).set("position_id", Val::Int(second));
    confirm(&store, &plan_id, line_id, Some("110.00"))
        .await
        .unwrap();
    let mut m = store.m();
    assert_eq!(m.position(second).int("quantity_open"), 200);
    assert_eq!(m.position(first).int("quantity_open"), 300);
}

/// The suite placed nothing live even with the settings the auto runner holds (`flags` is the
/// same helper the runner's tests use; this pins that the confirm itself reads its gates from
/// the settings it is handed, and `DRY_RUN=true` alone keeps it dry).
#[test]
fn test_dry_run_alone_keeps_the_gates_dry_whatever_the_sleeve_flag_says() {
    assert!(x::twt_gates(&flags(true, true, true)).dry_run);
    assert!(x::twt_gates(&flags(false, false, true)).dry_run);
    assert!(!x::twt_gates(&flags(false, true, false)).dry_run);
}
