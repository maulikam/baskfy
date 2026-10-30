//! Port of `tests/test_options_executor.py` (OP10, `04` §8.2-§8.4): sending a plan's legs.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use std::collections::{BTreeMap, BTreeSet};
use std::str::FromStr;

use baskfy_core::error::CoreResult;
use baskfy_core::options::config::{ExecutionConfig, Side, Sleeve};
use baskfy_core::options::execution::{Attempt, LegRole, never_naked};
use baskfy_core::options::executor::{
    Book, ExecResult, Fill, Outcome, Rules, Venue, run_entry, run_exit,
};
use options_common::Rng;
use rust_decimal::Decimal;

const QTY: i64 = 65;
const CONDOR: [LegRole; 4] = [
    LegRole::ShortCall,
    LegRole::ShortPut,
    LegRole::LongCall,
    LegRole::LongPut,
];
const SPREAD: [LegRole; 2] = [LegRole::LongCall, LegRole::ShortCall];
const LONG: [LegRole; 1] = [LegRole::LongCall];

fn dec(s: &str) -> Decimal {
    Decimal::from_str(s).unwrap()
}

type FillFn = Box<dyn FnMut(LegRole, &Attempt, i64) -> i64>;

/// `(role, side, attempt, filled, closing, marketable)`.
type Sent = (LegRole, Side, i64, i64, bool, bool);

/// Fills each attempt with `fill`, records every order and asserts the book never goes naked.
struct TestVenue {
    fill: FillFn,
    sent: Vec<Sent>,
    book: BTreeMap<LegRole, i64>,
}

impl TestVenue {
    fn new(fill: impl FnMut(LegRole, &Attempt, i64) -> i64 + 'static) -> Self {
        Self {
            fill: Box::new(fill),
            sent: Vec::new(),
            book: BTreeMap::new(),
        }
    }

    fn holding(mut self, roles: &[LegRole], qty: i64) -> Self {
        self.book = roles.iter().map(|r| (*r, qty)).collect();
        self
    }
}

impl Venue for TestVenue {
    fn quote(&mut self, _role: LegRole) -> CoreResult<Book> {
        Ok(Book {
            bid: dec("10.00"),
            ask: dec("10.20"),
        })
    }

    fn send(
        &mut self,
        role: LegRole,
        attempt: &Attempt,
        quantity: i64,
        closing: bool,
    ) -> CoreResult<Fill> {
        let filled = (self.fill)(role, attempt, quantity);
        self.sent.push((
            role,
            attempt.side,
            attempt.number,
            filled,
            closing,
            attempt.marketable,
        ));
        *self.book.entry(role).or_insert(0) += if closing { -filled } else { filled };
        assert!(never_naked(&self.book), "{role:?} {:?}", self.book);
        Ok(Fill {
            quantity: filled,
            avg_price: (filled != 0).then_some(attempt.price),
        })
    }
}

fn full(_: LegRole, _: &Attempt, q: i64) -> i64 {
    q
}

fn cfg() -> ExecutionConfig {
    ExecutionConfig::default()
}

fn entry(venue: &mut TestVenue, roles: &[LegRole], sleeve: Sleeve) -> ExecResult {
    let config = cfg();
    let rules = Rules {
        sleeve,
        tick: dec("0.05"),
        config: &config,
    };
    run_entry(venue, roles, QTY, rules).unwrap()
}

fn exit(venue: &mut TestVenue, position: BTreeMap<LegRole, i64>, sleeve: Sleeve) -> ExecResult {
    let config = cfg();
    let rules = Rules {
        sleeve,
        tick: dec("0.05"),
        config: &config,
    };
    run_exit(venue, position, rules).unwrap()
}

fn of(roles: &[LegRole], qty: i64) -> BTreeMap<LegRole, i64> {
    roles.iter().map(|r| (*r, qty)).collect()
}

#[test]
fn a_condor_opens_wings_first() {
    let mut venue = TestVenue::new(full);
    let result = entry(&mut venue, &CONDOR, Sleeve::O1m);
    assert_eq!(result.outcome, Outcome::Open);
    let sent: Vec<LegRole> = venue.sent.iter().map(|s| s.0).collect();
    let set = |a: LegRole, b: LegRole| BTreeSet::from([a, b]);
    assert_eq!(
        set(sent[0], sent[1]),
        set(LegRole::LongCall, LegRole::LongPut)
    );
    assert_eq!(
        set(sent[2], sent[3]),
        set(LegRole::ShortCall, LegRole::ShortPut)
    );
    assert_eq!(result.position, of(&CONDOR, QTY));
}

#[test]
fn attempt_one_is_the_touch_plus_a_tick() {
    let mut venue = TestVenue::new(full);
    let result = entry(&mut venue, &SPREAD, Sleeve::O3a);
    let [long_event, short_event] = &result.events[..] else {
        panic!("{:?}", result.events)
    };
    assert_eq!(long_event.price, dec("10.25"));
    assert_eq!(short_event.price, dec("9.95"));
    assert_eq!(
        result.avg_price(LegRole::LongCall).unwrap(),
        Some(dec("10.25"))
    );
}

#[test]
fn thin_long_call_depth_abandons_and_no_short_is_ever_sent() {
    let mut venue = TestVenue::new(|role, _, q| if role == LegRole::LongCall { 0 } else { q });
    let result = entry(&mut venue, &CONDOR, Sleeve::O1m);
    assert_eq!(result.outcome, Outcome::AbandonedEntry);
    assert!(
        !venue
            .sent
            .iter()
            .any(|s| matches!(s.0, LegRole::ShortCall | LegRole::ShortPut))
    );
    assert_eq!(
        result.position.get(&LegRole::LongPut).copied().unwrap_or(0),
        0
    );
}

#[test]
fn an_o3_partial_long_is_abandoned_and_its_fill_closed() {
    let mut venue = TestVenue::new(|role, attempt, q| {
        if role == LegRole::LongCall && attempt.side == Side::Buy {
            q.min(25)
        } else {
            q
        }
    });
    let result = entry(&mut venue, &SPREAD, Sleeve::O3a);
    assert_eq!(result.outcome, Outcome::AbandonedEntry);
    assert!(!venue.sent.iter().any(|s| s.0 == LegRole::ShortCall));
    let total = |closing: bool| -> i64 {
        venue
            .sent
            .iter()
            .filter(|s| s.0 == LegRole::LongCall && s.4 == closing)
            .map(|s| s.3)
            .sum()
    };
    assert_eq!((total(false), total(true)), (50, 50));
    assert_eq!(result.position[&LegRole::LongCall], 0);
}

#[test]
fn closes_run_shorts_first_and_the_third_attempt_is_marketable() {
    let mut venue = TestVenue::new(|role, attempt, q| {
        if role == LegRole::ShortCall && attempt.number < 3 {
            0
        } else {
            q
        }
    })
    .holding(&SPREAD, QTY);
    let result = exit(&mut venue, of(&SPREAD, QTY), Sleeve::O3a);
    assert_eq!(result.outcome, Outcome::Flat);
    let order: Vec<(LegRole, i64, bool)> = venue.sent.iter().map(|s| (s.0, s.2, s.5)).collect();
    assert_eq!(
        order,
        vec![
            (LegRole::ShortCall, 1, false),
            (LegRole::ShortCall, 2, false),
            (LegRole::ShortCall, 3, true),
            (LegRole::LongCall, 1, false),
        ]
    );
}

#[test]
fn a_wing_gets_no_marketable_attempt_and_the_exit_is_partial() {
    let mut venue = TestVenue::new(|role, _, q| if role == LegRole::LongCall { 0 } else { q })
        .holding(&SPREAD, QTY);
    let result = exit(&mut venue, of(&SPREAD, QTY), Sleeve::O3a);
    assert_eq!(result.outcome, Outcome::PartialExit);
    assert_eq!(
        result.position,
        BTreeMap::from([(LegRole::ShortCall, 0), (LegRole::LongCall, QTY)])
    );
    let attempts: Vec<i64> = venue
        .sent
        .iter()
        .filter(|s| s.0 == LegRole::LongCall)
        .map(|s| s.2)
        .collect();
    assert_eq!(attempts, vec![1, 2]);
}

#[test]
fn o2s_sell_is_risk_reducing() {
    let mut venue =
        TestVenue::new(|_, attempt, q| if attempt.number == 3 { q } else { 0 }).holding(&LONG, QTY);
    let result = exit(&mut venue, of(&LONG, QTY), Sleeve::O2);
    assert_eq!(result.outcome, Outcome::Flat);
    assert!(venue.sent.last().unwrap().5);
}

#[test]
fn an_overfilling_venue_is_an_error() {
    let mut venue = TestVenue::new(|_, _, q| q + 1);
    let config = cfg();
    let rules = Rules {
        sleeve: Sleeve::O2,
        tick: dec("0.05"),
        config: &config,
    };
    assert!(run_entry(&mut venue, &LONG, QTY, rules).is_err());
}

#[test]
fn a_naked_structure_is_refused_before_any_send() {
    let mut venue = TestVenue::new(full);
    let config = cfg();
    let rules = Rules {
        sleeve: Sleeve::O3a,
        tick: dec("0.05"),
        config: &config,
    };
    assert!(run_entry(&mut venue, &[LegRole::ShortCall], QTY, rules).is_err());
    assert!(venue.sent.is_empty());
}

fn never_naked_over_500_seeds(roles: &[LegRole], sleeve: Sleeve) {
    let mut outcomes = BTreeSet::new();
    for seed in 0..500_u64 {
        let mut rng = Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1);
        let mut venue = TestVenue::new(move |_, _, q| {
            let span = usize::try_from(q + 1).unwrap();
            match rng.below(3) {
                0 => 0,
                1 => q,
                _ => i64::try_from(rng.below(span)).unwrap(),
            }
        });
        let result = entry(&mut venue, roles, sleeve);
        outcomes.insert(result.outcome.value());
        if result.outcome == Outcome::Open {
            let closed = exit(&mut venue, result.position.clone(), sleeve);
            outcomes.insert(closed.outcome.value());
        }
        assert!(never_naked(&result.position));
    }
    assert!(outcomes.contains("OPEN") && outcomes.contains("ABANDONED_ENTRY"));
}

#[test]
fn never_naked_condor() {
    never_naked_over_500_seeds(&CONDOR, Sleeve::O1m);
}

#[test]
fn never_naked_spread() {
    never_naked_over_500_seeds(&SPREAD, Sleeve::O3a);
}

#[test]
fn never_naked_long() {
    never_naked_over_500_seeds(&LONG, Sleeve::O2);
}
