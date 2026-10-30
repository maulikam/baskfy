//! Port of `tests/test_options_ledger.py`: OP11, a closed session's journal figures from fills.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use rust_decimal::{Decimal, RoundingStrategy};

use baskfy_core::options::config::{CostRates, Side};
use baskfy_core::options::costs::{CostFill, charges};
use baskfy_core::options::ledger::{
    JournalFigures, JournalSession, LegFill, journal_figures, pnl_points,
};
use baskfy_core::options::structures::Structure;
use options_common::{dec, ist_dt};

fn fill(side: Side, price: &str, closing: bool) -> LegFill {
    LegFill {
        side,
        price: dec(price),
        quantity: 65,
        closing,
    }
}

/// Wings bought at 5.10, shorts sold at 20.00, closed at 5.20 and 0.50: 20.40 a unit, 65 units.
fn condor() -> Vec<LegFill> {
    vec![
        fill(Side::Buy, "5.10", false),
        fill(Side::Buy, "5.10", false),
        fill(Side::Sell, "20.00", false),
        fill(Side::Sell, "20.00", false),
        fill(Side::Buy, "5.20", true),
        fill(Side::Buy, "5.20", true),
        fill(Side::Sell, "0.50", true),
        fill(Side::Sell, "0.50", true),
    ]
}

fn session(r_inr: Decimal, peak: Option<Decimal>, trough: Option<Decimal>) -> JournalSession {
    JournalSession {
        r_inr,
        quantity: 65,
        opened_at: ist_dt(2026, 10, 27, 10, 5, 50),
        closed_at: ist_dt(2026, 10, 27, 11, 14, 50),
        peak_points: peak,
        trough_points: trough,
    }
}

fn figures(peak: Option<Decimal>, trough: Option<Decimal>) -> JournalFigures {
    journal_figures(
        &condor(),
        &session(dec("2500"), peak, trough),
        &CostRates::default(),
    )
    .unwrap()
}

#[test]
fn gross_is_the_signed_cash_of_every_fill() {
    let f = figures(None, None);
    assert_eq!(f.entry_inr, dec("1937.00"));
    assert_eq!(f.exit_inr, dec("611.00"));
    assert_eq!(f.gross_pnl_inr, dec("1326.00"));
}

#[test]
fn costs_are_04_6_over_all_eight_orders() {
    let f = figures(None, None);
    let cost_fills: Vec<CostFill> = condor()
        .iter()
        .map(|x| CostFill {
            side: x.side,
            price: x.price,
            quantity: x.quantity,
        })
        .collect();
    let expected = charges(&cost_fills, &CostRates::default()).unwrap();
    assert_eq!(f.costs, expected);
    assert_eq!(f.costs.orders, 8);
    assert_eq!(f.net_pnl_inr, f.gross_pnl_inr - expected.total().unwrap());
}

#[test]
fn r_and_the_minutes() {
    let f = figures(Some(dec("20.40")), Some(dec("-3.00")));
    let want = (f.net_pnl_inr / dec("2500"))
        .round_dp_with_strategy(2, RoundingStrategy::MidpointNearestEven);
    assert_eq!(f.r_multiple, want);
    assert_eq!(f.minutes_held, 69);
    assert_eq!(f.mfe_r, Some(dec("0.53")));
    assert_eq!(f.mae_r, Some(dec("-0.08")));
}

#[test]
fn a_zero_r_is_a_sizing_bug() {
    let e = journal_figures(
        &condor(),
        &session(Decimal::ZERO, None, None),
        &CostRates::default(),
    )
    .unwrap_err();
    assert!(e.to_string().contains("R must be positive"));
}

#[test]
fn a_gain_is_positive_for_every_structure() {
    assert_eq!(
        pnl_points(Structure::IronCondor, dec("29.80"), dec("9.40")).unwrap(),
        dec("20.40")
    );
    assert_eq!(
        pnl_points(Structure::LongOption, dec("200"), dec("260")).unwrap(),
        dec("60")
    );
    assert_eq!(
        pnl_points(Structure::DebitSpread, dec("30"), dec("15")).unwrap(),
        dec("-15")
    );
}
