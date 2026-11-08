//! Port of `tests/test_options_session.py` — `04` §11: seven edges, nothing else, random walks.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use std::collections::BTreeSet;

use baskfy_core::error::CoreError;
use baskfy_core::options::config::Sleeve;
use baskfy_core::options::session::{
    EDGES, NEVER_OPENED, Session, SessionState as S, TERMINAL, new_session, plan_expires_at,
    plan_is_live, transition,
};
use chrono::{NaiveDate, NaiveTime, TimeDelta};
use options_common::{Rng, d, ist_dt};

const ALL: [S; 7] = [
    S::Observing,
    S::Skipped,
    S::Planned,
    S::Lapsed,
    S::Confirmed,
    S::Open,
    S::Closed,
];

fn day() -> NaiveDate {
    d(2026, 10, 27)
}

/// The spec's edges, written out from `04` §11 / condor §9 rather than read from the module.
fn spec_edges() -> BTreeSet<(S, S)> {
    [
        (S::Observing, S::Skipped),
        (S::Observing, S::Planned),
        (S::Planned, S::Lapsed),
        (S::Planned, S::Confirmed),
        (S::Confirmed, S::Open),
        (S::Confirmed, S::Closed),
        (S::Open, S::Closed),
    ]
    .into_iter()
    .collect()
}

fn reason(frm: S, to: S) -> Option<&'static str> {
    match to {
        S::Skipped => Some("GAP_TOO_BIG"),
        S::Closed if frm == S::Confirmed => Some(NEVER_OPENED),
        S::Closed => Some("PROFIT"),
        _ => None,
    }
}

fn session(sleeve: Sleeve, state: S) -> Session {
    Session {
        sleeve,
        trade_date: day(),
        state,
        reason: None,
    }
}

fn illegal<T: std::fmt::Debug>(r: Result<T, CoreError>, needle: &str) {
    match r {
        Err(CoreError::InvalidInput(m)) => assert!(m.contains(needle), "{m}"),
        other => panic!("expected an illegal transition ({needle}), got {other:?}"),
    }
}

#[test]
fn the_module_has_exactly_the_specs_edges() {
    assert_eq!(EDGES.iter().copied().collect::<BTreeSet<_>>(), spec_edges());
}

#[test]
fn every_pair() {
    for frm in ALL {
        for to in ALL {
            let s = session(Sleeve::O1m, frm);
            if spec_edges().contains(&(frm, to)) {
                assert_eq!(transition(&s, to, reason(frm, to)).unwrap().state, to);
            } else {
                illegal(transition(&s, to, reason(frm, to)), "");
            }
        }
    }
}

#[test]
fn terminal_states_have_no_way_out() {
    assert_eq!(
        TERMINAL.iter().copied().collect::<BTreeSet<_>>(),
        [S::Skipped, S::Lapsed, S::Closed].into_iter().collect()
    );
    for (frm, _) in spec_edges() {
        assert!(!TERMINAL.contains(&frm));
    }
}

#[test]
fn random_edge_walks() {
    let mut r = Rng(0x2545_F491_4F6C_DD1D);
    for _ in 0..500 {
        let len = r.below(9);
        let targets: Vec<S> = (0..len).map(|_| ALL[r.below(ALL.len())]).collect();
        let mut s = new_session(Sleeve::O2, day(), &[]).unwrap();
        for to in targets {
            let before = s.clone();
            match transition(&s, to, reason(s.state, to)) {
                Err(CoreError::InvalidInput(_)) => {
                    assert!(!spec_edges().contains(&(before.state, to)));
                    s = before;
                }
                Err(e) => panic!("{e}"),
                Ok(next) => {
                    assert!(spec_edges().contains(&(before.state, next.state)));
                    s = next;
                }
            }
        }
    }
}

// --- TestReasons ---

#[test]
fn a_skip_carries_its_reason() {
    illegal(
        transition(&session(Sleeve::O1m, S::Observing), S::Skipped, None),
        "reason",
    );
}

#[test]
fn confirmed_never_opened() {
    illegal(
        transition(
            &session(Sleeve::O1m, S::Confirmed),
            S::Closed,
            Some("PROFIT"),
        ),
        "NEVER_OPENED",
    );
    let closed = transition(
        &session(Sleeve::O1m, S::Confirmed),
        S::Closed,
        Some(NEVER_OPENED),
    )
    .unwrap();
    assert_eq!(closed.reason.as_deref(), Some(NEVER_OPENED));
}

#[test]
fn open_closes_with_an_exit_code() {
    for bad in [None, Some(NEVER_OPENED)] {
        illegal(
            transition(&session(Sleeve::O1m, S::Open), S::Closed, bad),
            "",
        );
    }
    assert_eq!(
        transition(&session(Sleeve::O1m, S::Open), S::Closed, Some("HARD_EXIT"))
            .unwrap()
            .reason
            .as_deref(),
        Some("HARD_EXIT")
    );
}

// --- TestOnePerSleevePerDate ---

#[test]
fn a_second_session_is_refused() {
    let first = new_session(Sleeve::O1m, day(), &[]).unwrap();
    illegal(
        new_session(Sleeve::O1m, day(), &[first]),
        "already has a session",
    );
}

#[test]
fn other_sleeves_and_dates_are_fine() {
    let first = new_session(Sleeve::O1m, day(), &[]).unwrap();
    assert_eq!(
        new_session(Sleeve::O3a, day(), std::slice::from_ref(&first))
            .unwrap()
            .sleeve,
        Sleeve::O3a
    );
    assert_eq!(
        new_session(Sleeve::O1m, day() + TimeDelta::days(7), &[first])
            .unwrap()
            .state,
        S::Observing
    );
}

// --- TestPlanExpiry ---

fn hm(h: u32, m: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(h, m, 0).unwrap()
}

#[test]
fn the_window_end_binds_o1() {
    let issued = ist_dt(2026, 10, 27, 10, 0, 0);
    assert_eq!(
        plan_expires_at(&issued, hm(10, 15), 30),
        ist_dt(2026, 10, 27, 10, 15, 0)
    );
}

#[test]
fn thirty_minutes_bind_o2() {
    let issued = ist_dt(2026, 10, 21, 11, 20, 0);
    assert_eq!(
        plan_expires_at(&issued, hm(13, 30), 30),
        ist_dt(2026, 10, 21, 11, 50, 0)
    );
}

#[test]
fn live_strictly_before_expiry() {
    let expires = ist_dt(2026, 10, 27, 10, 15, 0);
    assert!(plan_is_live(&expires, &(expires - TimeDelta::seconds(1))));
    assert!(!plan_is_live(&expires, &expires));
}
