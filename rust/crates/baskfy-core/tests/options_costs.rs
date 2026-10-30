//! Port of `tests/test_options_costs.py`: `04` §6 costs, every rate a field, the expiry-day STT trap.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use chrono::{NaiveDate, NaiveTime};
use rust_decimal::Decimal;

use baskfy_core::options::config::{
    CostRates, OptionsCeilings, OptionsConfig, Side, Sleeve, check_hard_exits,
};
use baskfy_core::options::costs::{
    ChargeBreakdown, CostFill, CostVerdict, HeldLong, charges, cost_test, exercise_stt,
    expected_gain_o1, expected_gain_o2, expected_gain_o3, rates_review_due, settlement_stt,
    synthetic_half_spread,
};
use options_common::{LOT, dec};

fn rates() -> CostRates {
    OptionsConfig::default().costs
}

fn settle() -> NaiveTime {
    OptionsConfig::default().calendar.market_close
}

fn buy() -> CostFill {
    CostFill {
        side: Side::Buy,
        price: dec("200.05"),
        quantity: LOT,
    }
}

fn sell() -> CostFill {
    CostFill {
        side: Side::Sell,
        price: dec("260.00"),
        quantity: LOT,
    }
}

fn hm(h: u32, m: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(h, m, 0).unwrap()
}

#[test]
fn each_component_to_the_paisa() {
    let got = charges(&[buy(), sell()], &rates()).unwrap();
    assert_eq!(got.orders, 2);
    assert_eq!(got.brokerage, dec("40.00"));
    assert_eq!(got.stt, dec("25.35"));
    assert_eq!(got.exchange_txn, dec("10.62"));
    assert_eq!(got.sebi, dec("0.03"));
    assert_eq!(got.ipft, dec("0.00"));
    assert_eq!(got.stamp, dec("0.39"));
    assert_eq!(got.gst, dec("9.12"));
    assert_eq!(got.total().unwrap(), dec("85.51"));
}

#[test]
fn a_condor_pays_eight_brokerages() {
    let mut legs = vec![
        CostFill {
            side: Side::Buy,
            price: Decimal::from(5),
            quantity: LOT
        };
        4
    ];
    legs.extend(vec![
        CostFill {
            side: Side::Sell,
            price: Decimal::from(5),
            quantity: LOT
        };
        4
    ]);
    assert_eq!(charges(&legs, &rates()).unwrap().brokerage, dec("160.00"));
}

#[test]
fn stt_is_sell_side_only_and_stamp_buy_side_only() {
    assert_eq!(charges(&[buy()], &rates()).unwrap().stt, Decimal::ZERO);
    assert_eq!(charges(&[sell()], &rates()).unwrap().stamp, Decimal::ZERO);
}

type Field = fn(&mut CostRates) -> &mut Decimal;
type Component = fn(&ChargeBreakdown) -> Decimal;

#[test]
fn each_rate_is_a_field_that_moves_its_component() {
    let cases: [(Field, Component, Vec<CostFill>); 5] = [
        (
            |r| &mut r.brokerage_per_order_inr,
            |c| c.brokerage,
            vec![buy(), sell()],
        ),
        (|r| &mut r.stt_sell_premium_pct, |c| c.stt, vec![sell()]),
        (
            |r| &mut r.exchange_txn_pct,
            |c| c.exchange_txn,
            vec![buy(), sell()],
        ),
        (
            |r| &mut r.sebi_per_crore_inr,
            |c| c.sebi,
            vec![buy(), sell()],
        ),
        (|r| &mut r.stamp_buy_pct, |c| c.stamp, vec![buy()]),
    ];
    for (field, component, fills) in cases {
        let base = component(&charges(&fills, &rates()).unwrap());
        let mut doubled_rates = rates();
        let f = field(&mut doubled_rates);
        *f *= Decimal::TWO;
        let doubled = component(&charges(&fills, &doubled_rates).unwrap());
        assert!((doubled - Decimal::TWO * base).abs() <= dec("0.01"));
        assert!(doubled > base);
    }
}

#[test]
fn ipft_and_gst_are_fields() {
    let big = [CostFill {
        side: Side::Buy,
        price: Decimal::from(1000),
        quantity: 100_000,
    }];
    assert_eq!(charges(&big, &rates()).unwrap().ipft, dec("0.10"));
    let mut r2 = rates();
    r2.ipft_per_crore_inr = dec("0.02");
    assert_eq!(charges(&big, &r2).unwrap().ipft, dec("0.20"));
    let mut no_gst = rates();
    no_gst.gst_pct = Decimal::ZERO;
    assert_eq!(
        charges(&[buy(), sell()], &no_gst).unwrap().gst,
        Decimal::ZERO
    );
}

#[test]
fn exercise_stt_is_on_intrinsic() {
    assert_eq!(
        exercise_stt(Decimal::from(300), LOT, &rates()).unwrap(),
        dec("29.25")
    );
    assert_eq!(
        exercise_stt(Decimal::from(-50), LOT, &rates()).unwrap(),
        Decimal::ZERO
    );
}

fn deep_itm() -> Vec<HeldLong> {
    vec![HeldLong {
        strike: Decimal::from(24500),
        is_call: true,
        quantity: LOT,
    }]
}

#[test]
fn every_hard_exit_makes_the_trap_unreachable() {
    for sleeve in Sleeve::ALL {
        let hard_exit = OptionsConfig::default().hard_exit_time(sleeve);
        assert!(hard_exit < settle());
        assert_eq!(
            settlement_stt(
                &deep_itm(),
                Decimal::from(25500),
                hard_exit,
                settle(),
                &rates()
            )
            .unwrap(),
            Decimal::ZERO
        );
    }
}

#[test]
fn a_hard_exit_past_1530_would_put_the_charge_in_the_journal() {
    let charge = settlement_stt(
        &deep_itm(),
        Decimal::from(25500),
        hm(15, 31),
        settle(),
        &rates(),
    )
    .unwrap();
    assert_eq!(charge, dec("97.50"));
}

#[test]
fn a_put_leg_and_an_otm_leg() {
    let held = [
        HeldLong {
            strike: Decimal::from(26000),
            is_call: false,
            quantity: LOT,
        },
        HeldLong {
            strike: Decimal::from(26000),
            is_call: true,
            quantity: LOT,
        },
    ];
    assert_eq!(
        settlement_stt(&held, Decimal::from(25500), hm(15, 30), settle(), &rates()).unwrap(),
        dec("48.75")
    );
}

#[test]
fn the_defaults_respect_the_hard_exit_ceiling() {
    let ceilings = OptionsCeilings::default();
    assert!(check_hard_exits(&OptionsConfig::default(), &ceilings).is_empty());
    let mut late = OptionsConfig::default();
    late.directional.hard_exit_time = hm(15, 5);
    assert_eq!(check_hard_exits(&late, &ceilings), vec!["O2".to_owned()]);
}

#[test]
fn synthetic_half_spread_floor_and_pct() {
    assert_eq!(
        synthetic_half_spread(Decimal::from(4), &rates()).unwrap(),
        dec("0.10")
    );
    assert_eq!(
        synthetic_half_spread(Decimal::from(100), &rates()).unwrap(),
        dec("1.5")
    );
}

#[test]
fn expected_gains() {
    assert_eq!(
        expected_gain_o1(Decimal::from(2600), dec("0.50")).unwrap(),
        Decimal::from(1300)
    );
    assert_eq!(
        expected_gain_o2(Decimal::from(200), LOT, dec("0.60")).unwrap(),
        Decimal::from(7800)
    );
    assert_eq!(
        expected_gain_o3(Decimal::from(100), Decimal::from(45), LOT, dec("0.80")).unwrap(),
        Decimal::from(2275)
    );
}

#[test]
fn the_share_boundary() {
    let ok = cost_test(Decimal::from(260), Decimal::from(1300), dec("0.20")).unwrap();
    assert_eq!(ok.verdict, CostVerdict::Ok);
    assert_eq!(ok.cost_share, Some(dec("0.2")));
    assert_eq!(
        cost_test(dec("260.01"), Decimal::from(1300), dec("0.20"))
            .unwrap()
            .verdict,
        CostVerdict::RejectedCost
    );
}

#[test]
fn no_gain_cannot_pay_a_cost() {
    let test = cost_test(Decimal::from(10), Decimal::ZERO, dec("0.20")).unwrap();
    assert_eq!(test.verdict, CostVerdict::RejectedCost);
    assert_eq!(test.cost_share, None);
}

#[test]
fn the_rates_review_warning() {
    let r = rates();
    let d = |m, day| NaiveDate::from_ymd_opt(2026, m, day).unwrap();
    assert_eq!(r.reviewed_on, d(9, 22));
    assert!(!rates_review_due(&r, d(12, 21)));
    assert!(rates_review_due(&r, d(12, 22)));
}

#[test]
fn the_verified_rates() {
    let r = CostRates::default();
    assert_eq!(r.stt_sell_premium_pct, dec("0.15"));
    assert_eq!(r.stt_exercise_intrinsic_pct, dec("0.15"));
    assert_eq!(r.exchange_txn_pct, dec("0.03553"));
    assert_eq!(r.ipft_per_crore_inr, dec("0.01"));
}
