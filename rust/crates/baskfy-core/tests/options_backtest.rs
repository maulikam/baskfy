//! Port of `tests/test_options_backtest.py` (`04` §13 / `07` §4): one sleeve, one tier, verbatim caveats.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::path::PathBuf;
use std::str::FromStr;

use baskfy_core::decimal::WideDecimal;
use baskfy_core::options::backtest::{
    DayOutcome, TIER1_CAVEAT, TIER2_CAVEAT, TIER2_O2_CAVEAT, Tier, caveats, run, sample_banner,
    tier2_quote,
};
use baskfy_core::options::config::{OptionType, OptionsConfig, Sleeve};
use baskfy_core::options::greeks::black76_price;
use baskfy_core::options::num;
use chrono::{NaiveDate, NaiveTime};
use rust_decimal::Decimal;

fn docs() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../docs")
}

fn flat(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn read(rel: &str) -> String {
    std::fs::read_to_string(docs().join(rel)).unwrap()
}

fn dec(s: &str) -> Decimal {
    Decimal::from_str(s).unwrap()
}

fn d(y: i32, m: u32, day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, day).unwrap()
}

fn strs(v: &[&str]) -> Vec<String> {
    v.iter().map(|s| (*s).to_owned()).collect()
}

#[test]
fn caveat_tier1_is_verbatim() {
    assert!(flat(&read("options/07-data-reality.md")).contains(TIER1_CAVEAT));
}

#[test]
fn caveat_tier2_is_condor_07s_sentence() {
    assert!(flat(&read("condor/07-data-reality.md")).contains(TIER2_CAVEAT));
}

#[test]
fn caveat_tier2_o2_is_04s_sentence() {
    assert!(flat(&read("options/04-business-rules.md")).contains(TIER2_O2_CAVEAT));
}

#[test]
fn o2_carries_both_and_the_others_one() {
    assert_eq!(
        caveats(Sleeve::O2, Tier::Modelled, 0, 60),
        strs(&[TIER2_CAVEAT, TIER2_O2_CAVEAT])
    );
    assert_eq!(
        caveats(Sleeve::O1m, Tier::Modelled, 0, 12),
        strs(&[TIER2_CAVEAT])
    );
    assert_eq!(
        caveats(Sleeve::O3a, Tier::Signals, 0, 20),
        strs(&[TIER1_CAVEAT])
    );
}

#[test]
fn tier3_sample_and_banner() {
    assert_eq!(
        sample_banner(11, 12).as_deref(),
        Some("Insufficient sample: 11 of 12 observed sessions.")
    );
    assert_eq!(sample_banner(12, 12), None);
    assert_eq!(
        caveats(Sleeve::O1m, Tier::Observed, 12, 12),
        strs(&["Observed sample: 12 sessions."])
    );
    assert_eq!(caveats(Sleeve::O2, Tier::Observed, 5, 60).len(), 2);
}

fn priced(day: NaiveDate, net: &str, r: &str) -> DayOutcome {
    DayOutcome {
        net_pnl_inr: Some(dec(net)),
        r_multiple: Some(dec(r)),
        ..DayOutcome::bare(day, true)
    }
}

fn days() -> Vec<DayOutcome> {
    vec![
        priced(d(2025, 12, 30), "500", "0.5"),
        DayOutcome::skipped(d(2026, 1, 6), "ER_TOO_HIGH"),
        priced(d(2026, 1, 13), "-1000", "-1"),
        priced(d(2026, 1, 20), "1500", "1.5"),
        DayOutcome::skipped(d(2026, 1, 27), "ER_TOO_HIGH"),
    ]
}

#[test]
fn one_row_of_one_sleeve_and_one_tier() {
    let mut reversed = days();
    reversed.reverse();
    let got = run(Sleeve::O1w, Tier::Modelled, reversed, |o| o, 20).unwrap();
    assert_eq!(
        (got.sleeve, got.tier, got.sessions, got.traded),
        (Sleeve::O1w, Tier::Modelled, 5, 3)
    );
    assert_eq!(
        (got.date_from, got.date_to),
        (Some(d(2025, 12, 30)), Some(d(2026, 1, 27)))
    );
    assert_eq!(got.skipped_by_reason, vec![("ER_TOO_HIGH".to_owned(), 2)]);
    assert_eq!(got.by_year, vec![(2025, 1, 1), (2026, 4, 2)]);
    assert_eq!(
        got.win_rate,
        Some(num::div(Decimal::from(2), Decimal::from(3)).unwrap())
    );
    assert_eq!(
        got.expectancy_r,
        Some(num::div(Decimal::ONE, Decimal::from(3)).unwrap())
    );
    assert_eq!(got.net_pnl_inr, Some(Decimal::from(1000)));
    assert_eq!(got.max_drawdown_r, Some(Decimal::ONE));
    assert_eq!(got.caveats, strs(&[TIER2_CAVEAT]));
}

#[test]
fn tier1_has_no_pnl() {
    let e = run(Sleeve::O2, Tier::Signals, days(), |o| o, 60).unwrap_err();
    assert!(e.to_string().contains("no P&L"), "{e}");
    let signals = vec![DayOutcome {
        trigger_time: NaiveTime::from_hms_opt(10, 5, 0),
        mfe_points: Some(Decimal::from(80)),
        ..DayOutcome::bare(d(2026, 1, 6), true)
    }];
    let got = run(Sleeve::O2, Tier::Signals, signals, |o| o, 60).unwrap();
    assert_eq!(got.traded, 1);
    assert_eq!(
        (
            got.win_rate,
            got.expectancy_r,
            got.net_pnl_inr,
            got.max_drawdown_r
        ),
        (None, None, None, None)
    );
}

#[test]
fn a_priced_tier_needs_r_on_every_trade() {
    let e = run(
        Sleeve::O3a,
        Tier::Observed,
        vec![DayOutcome::bare(d(2026, 1, 6), true)],
        |o| o,
        20,
    )
    .unwrap_err();
    assert!(e.to_string().contains("needs its R"), "{e}");
}

#[test]
fn empty() {
    let got = run(
        Sleeve::O1m,
        Tier::Observed,
        Vec::<DayOutcome>::new(),
        |o| o,
        12,
    )
    .unwrap();
    assert_eq!(
        (got.sessions, got.date_from, got.expectancy_r),
        (0, None, None)
    );
}

#[test]
fn a_skip_without_a_reason_is_unspecified() {
    let outcomes = vec![
        DayOutcome::bare(d(2026, 1, 6), false),
        DayOutcome::skipped(d(2026, 1, 7), ""),
    ];
    let got = run(Sleeve::O2, Tier::Signals, outcomes, |o| o, 60).unwrap();
    assert_eq!(got.skipped_by_reason, vec![("UNSPECIFIED".to_owned(), 2)]);
}

fn wide(d: Decimal) -> WideDecimal {
    WideDecimal::from(d)
}

#[test]
fn tier2_flat_vix_forward_equals_spot() {
    let t = 7.0 / 365.0;
    let rates = OptionsConfig::default().costs;
    let got = tier2_quote(
        Decimal::from(25000),
        Decimal::from(25100),
        t,
        Decimal::from(14),
        OptionType::Ce,
        0.065,
        &rates,
    )
    .unwrap();
    let model: f64 = got.model.to_string().parse().unwrap();
    let expected = black76_price(25000.0, 25100.0, t, 0.065, 0.14, OptionType::Ce);
    assert!((model - expected).abs() <= 1e-9 * expected);
    let half = got.model.mul(&wide(dec("0.015")));
    assert_eq!(got.ask.sub(&got.model), half);
    assert_eq!(got.model.sub(&got.bid), half);
}

#[test]
fn tier2_the_bid_is_floored_at_zero() {
    let rates = OptionsConfig::default().costs;
    let got = tier2_quote(
        Decimal::from(25000),
        Decimal::from(27000),
        1.0 / 365.0,
        Decimal::from(10),
        OptionType::Ce,
        0.065,
        &rates,
    )
    .unwrap();
    assert!(got.bid.is_zero());
    assert_eq!(got.ask, got.model.add(&wide(dec("0.10"))));
}
