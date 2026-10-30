//! Port of `tests/test_options_sizing.py`: `04` §7 lots from a risk budget, paper-one-lot,
//! ceilings, margin a ceiling.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use rust_decimal::Decimal;

use baskfy_core::options::config::{Mode, OptionsCeilings, OptionsConfig, SizingMode, Sleeve};
use baskfy_core::options::sizing::{
    MarginCheck, MarginCode, SizeInputs, Sizing, SizingReject, gap_through_long, margin_check,
    premium_cap_ok, risk_per_lot_condor, risk_per_lot_debit_spread, risk_per_lot_long, size,
};
use options_common::{LOT, dec};

fn cfg() -> OptionsConfig {
    OptionsConfig::default()
}

fn condor_lot() -> Decimal {
    risk_per_lot_condor(
        Decimal::from(150),
        Decimal::from(40),
        LOT,
        Decimal::from(1000),
    )
    .unwrap()
}

struct Opts {
    mode: Mode,
    sleeve: Sleeve,
    real_rows: i64,
    lot_size: Option<i64>,
    risk_per_lot: Decimal,
    pct: Option<Decimal>,
    max_lots: Option<i64>,
}

impl Default for Opts {
    fn default() -> Self {
        Self {
            mode: Mode::Live,
            sleeve: Sleeve::O1m,
            real_rows: 5,
            lot_size: Some(LOT),
            risk_per_lot: condor_lot(),
            pct: None,
            max_lots: None,
        }
    }
}

fn try_size(capital: i64, o: Opts) -> baskfy_core::error::CoreResult<Sizing> {
    let c = cfg();
    size(
        &SizeInputs {
            mode: o.mode,
            sleeve_capital_inr: Decimal::from(capital),
            risk_per_trade_pct: o.pct.unwrap_or_else(|| c.risk_per_trade_pct(o.sleeve)),
            max_lots: o.max_lots.unwrap_or_else(|| c.max_lots(o.sleeve)),
            risk_per_lot_inr: o.risk_per_lot,
            lot_size: o.lot_size,
            real_journal_rows: o.real_rows,
        },
        &c.sizing,
        &OptionsCeilings::default(),
    )
}

fn sz(capital: i64, o: Opts) -> Sizing {
    try_size(capital, o).unwrap()
}

#[test]
fn risk_per_lot_condor_value() {
    assert_eq!(Decimal::from(8150), condor_lot());
}

#[test]
fn long_option_and_its_gap_through() {
    assert_eq!(
        risk_per_lot_long(dec("200.05"), dec("0.30"), LOT, Decimal::from(300)).unwrap(),
        dec("4200.975")
    );
    assert_eq!(
        gap_through_long(dec("200.05"), LOT).unwrap(),
        dec("13003.25")
    );
}

#[test]
fn debit_spread() {
    assert_eq!(
        risk_per_lot_debit_spread(Decimal::from(45), LOT, Decimal::from(500)).unwrap(),
        Decimal::from(3425)
    );
}

#[test]
fn capital_zero_in_paper_is_one_lot_and_r_is_one_lots_risk() {
    let got = sz(
        0,
        Opts {
            mode: Mode::Paper,
            ..Opts::default()
        },
    );
    assert_eq!(
        (got.lots, got.sizing_mode, got.r_inr, got.rejection),
        (1, SizingMode::PaperOneLot, condor_lot(), None)
    );
    assert!(!got.half_size);
}

#[test]
fn capital_zero_live_is_refused() {
    let got = sz(0, Opts::default());
    assert_eq!(got.rejection, Some(SizingReject::RejectedNoSleeveCapital));
    assert_eq!(got.lots, 0);
}

#[test]
fn no_lot_size_is_refused_first() {
    for lot_size in [None, Some(0)] {
        let got = sz(
            0,
            Opts {
                mode: Mode::Paper,
                lot_size,
                ..Opts::default()
            },
        );
        assert_eq!(got.rejection, Some(SizingReject::RejectedNoLotSize));
    }
}

#[test]
fn budget_too_small_names_the_arithmetic() {
    let got = sz(500_000, Opts::default());
    assert_eq!(got.rejection, Some(SizingReject::RejectedBudget));
    assert!(got.message.contains("5000.00"));
    assert!(got.message.contains("8150.00"));
}

#[test]
fn floor_of_budget_over_risk_per_lot() {
    let got = sz(2_000_000, Opts::default());
    assert_eq!(
        (got.lots, got.sizing_mode, got.r_inr),
        (2, SizingMode::Budget, Decimal::from(20000))
    );
}

#[test]
fn the_inr_ceiling_caps_the_budget() {
    let got = sz(10_000_000, Opts::default());
    assert_eq!(
        got.risk_budget_inr,
        OptionsCeilings::default().risk_per_trade_inr_max
    );
    assert_eq!(got.lots, 3);
}

#[test]
fn the_sleeves_max_lots_caps() {
    let got = sz(
        10_000_000,
        Opts {
            sleeve: Sleeve::O1w,
            pct: Some(dec("1.0")),
            ..Opts::default()
        },
    );
    assert_eq!(got.lots, 2);
}

#[test]
fn the_system_max_lots_caps() {
    let got = sz(
        10_000_000,
        Opts {
            risk_per_lot: Decimal::from(1000),
            max_lots: Some(50),
            ..Opts::default()
        },
    );
    assert_eq!(got.lots, OptionsCeilings::default().max_lots_max);
}

#[test]
fn a_pct_above_the_ceiling_is_clamped() {
    let got = sz(
        1_000_000,
        Opts {
            pct: Some(dec("3.0")),
            risk_per_lot: Decimal::from(1000),
            max_lots: Some(50),
            ..Opts::default()
        },
    );
    assert_eq!(got.risk_budget_inr, Decimal::from(10000));
}

#[test]
fn zero_risk_per_lot_is_a_bug() {
    let e = try_size(
        1_000_000,
        Opts {
            risk_per_lot: Decimal::ZERO,
            ..Opts::default()
        },
    )
    .unwrap_err();
    assert!(e.to_string().contains("positive"));
}

#[test]
fn four_real_rows_halve_the_budget() {
    let got = sz(
        2_000_000,
        Opts {
            real_rows: 4,
            ..Opts::default()
        },
    );
    assert!(got.half_size);
    assert_eq!(got.risk_budget_inr, Decimal::from(10000));
    assert_eq!(got.lots, 1);
}

#[test]
fn five_real_rows_lift_it() {
    let got = sz(2_000_000, Opts::default());
    assert!(!got.half_size);
    assert_eq!(got.risk_budget_inr, Decimal::from(20000));
}

#[test]
fn premium_cap_boundary() {
    let cap = cfg().directional.premium_cap_pct;
    let capital = Decimal::from(100_000);
    assert!(premium_cap_ok(1, Decimal::from(150), LOT, capital, cap).unwrap());
    assert!(premium_cap_ok(1, dec("153.84"), LOT, capital, cap).unwrap());
    assert!(!premium_cap_ok(1, Decimal::from(200), LOT, capital, cap).unwrap());
}

#[test]
fn no_capital_nothing_to_cap() {
    assert!(
        premium_cap_ok(
            1,
            Decimal::from(500),
            LOT,
            Decimal::ZERO,
            cfg().directional.premium_cap_pct
        )
        .unwrap()
    );
}

fn margin(
    mode: Mode,
    hedged: Decimal,
    transient: Option<Decimal>,
    pool: i64,
    used: i64,
) -> MarginCheck {
    margin_check(
        mode,
        hedged,
        transient,
        Decimal::from(pool),
        Decimal::from(used),
    )
    .unwrap()
}

#[test]
fn pool_unset_in_paper_warns() {
    let got = margin(Mode::Paper, Decimal::from(90000), None, 0, 0);
    assert_eq!(got.code, MarginCode::MarginPoolUnset);
    assert!(!got.rejected());
    assert!(got.message.contains("90000.00"));
}

#[test]
fn pool_unset_live_rejects() {
    assert!(margin(Mode::Live, Decimal::from(90000), None, 0, 0).rejected());
}

#[test]
fn fits_under_the_free_pool() {
    let got = margin(Mode::Live, Decimal::from(150_000), None, 200_000, 50_000);
    assert_eq!(got.code, MarginCode::Ok);
    assert_eq!(got.available_inr, Decimal::from(150_000));
}

#[test]
fn hedged_above_the_free_pool() {
    let got = margin(Mode::Live, dec("150000.01"), None, 200_000, 50_000);
    assert!(got.rejected());
    assert!(got.message.contains("hedged"));
}

#[test]
fn the_transient_figure_counts() {
    let got = margin(
        Mode::Paper,
        Decimal::from(100_000),
        Some(Decimal::from(180_000)),
        200_000,
        50_000,
    );
    assert!(got.rejected());
    assert!(got.message.contains("transient"));
}
