//! Port of `tests/test_options_edges.py`: the `04` boundaries at the exact flip values.
//! Not ported: `test_everything_the_core_hands_out_is_frozen` (Python dataclass introspection)
//! and `TestBacktestEdges` (backtest.py is not in Rust yet).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use std::collections::BTreeMap;

use chrono::NaiveDate;
use rust_decimal::Decimal;

use baskfy_core::options::chain::{Level, atm, depth_available, parity_forward, strike_step};
use baskfy_core::options::config::{
    ExecutionConfig, Mode, OptionType, OptionsCeilings, OptionsConfig, RiskConfig, Side,
    SizingMode, Sleeve,
};
use baskfy_core::options::costs::{CostFill, CostVerdict, charges, cost_test};
use baskfy_core::options::execution::{
    LegRole, entry_sequence, exit_sequence, never_naked, next_attempt, next_exit_leg, simulate_fill,
};
use baskfy_core::options::greeks::{
    Bracket, Greeks, SolveLimits, black76_price, intrinsic, solve, year_fraction,
};
use baskfy_core::options::journal::{JournalRow, r_multiple, summarize_one};
use baskfy_core::options::num;
use baskfy_core::options::risk::{
    BookLimits, Pause, PauseReason, RealisedTrade, SleeveDay, book_limits, evaluate_sleeve,
    last_trading_day_of_week, trade_breached,
};
use baskfy_core::options::sizing::{
    MarginCode, SizeInputs, Sizing, SizingReject, margin_check, premium_cap_ok, size,
};
use options_common::{LOT, QuoteOpts, d, dec, ist_dt, quote, quote_with, tick};

const CE: OptionType = OptionType::Ce;
const PE: OptionType = OptionType::Pe;
const LC: LegRole = LegRole::LongCall;
const LP: LegRole = LegRole::LongPut;
const SC: LegRole = LegRole::ShortCall;
const SP: LegRole = LegRole::ShortPut;

fn cfg() -> OptionsConfig {
    OptionsConfig::default()
}

fn rate() -> f64 {
    cfg().chain.rate
}

fn approx_abs(got: f64, want: f64, tol: f64) {
    assert!((got - want).abs() <= tol, "{got} != {want} ± {tol}");
}

fn approx_rel(got: f64, want: f64, rel: f64) {
    assert!(
        (got - want).abs() <= (rel * want.abs()).max(1e-12),
        "{got} != {want} (rel {rel})"
    );
}

// --- greeks domain ---

#[test]
fn greeks_small_numbers_are_priced_not_treated_as_degenerate() {
    approx_abs(
        black76_price(0.5, 0.5, 1.0, 0.0, 0.2, CE),
        0.039_827_8,
        1e-6,
    );
    approx_abs(
        black76_price(0.5, 0.5, 1.0, 0.0, 0.5, CE),
        0.098_706_4,
        1e-6,
    );
}

#[test]
fn greeks_degenerate_inputs_are_the_discounted_intrinsic() {
    for (forward, strike, years, vol, expected) in [
        (25100.0, 25000.0, 0.01, 0.0, 100.0), // no vol: discounted intrinsic
        (25100.0, 0.0, 0.01, 0.2, 25100.0),   // a zero strike is all intrinsic
        (0.0, 25000.0, 0.01, 0.2, 0.0),       // a zero forward is worth nothing as a call
    ] {
        let got = black76_price(forward, strike, years, rate(), vol, CE);
        approx_rel(got, expected, 1e-3);
    }
}

#[test]
fn greeks_an_otm_call_has_no_intrinsic() {
    assert!(intrinsic(24900.0, 25000.0, CE).abs() < f64::EPSILON);
}

fn solve_at(mid: f64, forward: f64, strike: f64, kind: OptionType) -> Option<Greeks> {
    let chain = cfg().chain;
    let t = year_fraction(
        &ist_dt(2026, 10, 20, 10, 0, 0),
        d(2026, 10, 27),
        chrono::NaiveTime::from_hms_opt(15, 30, 0).unwrap(),
    );
    solve(
        mid,
        forward,
        strike,
        t,
        chain.rate,
        kind,
        SolveLimits {
            min_premium: num::to_float(chain.min_premium),
            tick: num::to_float(tick()),
            bracket: Bracket {
                lower: chain.iv_lower,
                upper: chain.iv_upper,
                tolerance: chain.iv_tolerance,
            },
        },
    )
    .unwrap()
}

#[test]
fn greeks_a_mid_exactly_at_min_premium_is_solved() {
    let floor = num::to_float(cfg().chain.min_premium);
    assert!(solve_at(floor, 25000.0, 25800.0, CE).is_some());
}

#[test]
fn greeks_a_mid_exactly_at_intrinsic_plus_a_tick_is_solved() {
    let mid = intrinsic(25100.0, 25000.0, CE) + num::to_float(tick());
    assert!(solve_at(mid, 25100.0, 25000.0, CE).is_some());
}

// --- chain edges ---

#[test]
fn chain_two_strikes_have_a_step() {
    assert_eq!(
        strike_step(
            &[Decimal::from(25000), Decimal::from(25050)],
            Decimal::from(25000),
            15
        )
        .unwrap(),
        Some(Decimal::from(50))
    );
}

#[test]
fn chain_the_window_is_exactly_two_window_plus_one_strikes() {
    let strikes: Vec<Decimal> = [0, 10, 110, 210, 310].map(Decimal::from).to_vec();
    // {0, 10, 110}: gaps 10, 100 -> finer
    assert_eq!(
        strike_step(&strikes, Decimal::from(5), 1).unwrap(),
        Some(Decimal::from(10))
    );
    assert_eq!(
        strike_step(&strikes, Decimal::from(300), 1).unwrap(),
        Some(Decimal::from(100))
    );
}

#[test]
fn chain_a_one_point_grid() {
    assert_eq!(
        atm(dec("25000.4"), Decimal::ONE).unwrap(),
        Decimal::from(25000)
    );
}

#[test]
fn chain_a_zero_side_has_no_mid() {
    for (bid, ask) in [("0", "1"), ("1", "0")] {
        assert_eq!(
            quote(25000, CE, Some(bid), Some(ask)).mid().unwrap(),
            None,
            "{bid}/{ask}"
        );
    }
}

#[test]
fn chain_a_sub_rupee_quote_has_a_mid() {
    assert_eq!(
        quote(26500, CE, Some("0.50"), Some("0.60")).mid().unwrap(),
        Some(dec("0.55"))
    );
}

#[test]
fn chain_one_unit_of_depth_counts() {
    let q = quote_with(
        Decimal::from(25000),
        CE,
        Some(Decimal::from(99)),
        Some(Decimal::from(101)),
        QuoteOpts {
            asks: Some(vec![Level {
                price: Decimal::from(101),
                quantity: 1,
            }]),
            ..QuoteOpts::default()
        },
    );
    assert_eq!(depth_available(&q, Side::Buy, 3), 1);
}

#[test]
fn chain_the_fallback_takes_the_nearest_pair_first() {
    // ATM 25,000 has no put; 25,050 (nearest) and 24,900 disagree, and 25,050 must win.
    let quotes = [
        quote(25000, CE, Some("100"), Some("101")),
        quote(25050, CE, Some("80"), Some("81")),
        quote(25050, PE, Some("70"), Some("71")),
        quote(24900, CE, Some("200"), Some("201")),
        quote(24900, PE, Some("50"), Some("51")),
    ];
    let found = parity_forward(
        &quotes,
        Decimal::from(25000),
        Decimal::from(50),
        0.0,
        rate(),
    );
    assert_eq!(found.unwrap(), Some(Decimal::from(25060)));
}

// --- cost edges ---

#[test]
fn costs_the_crore_is_ten_million() {
    let big = CostFill {
        side: Side::Buy,
        price: Decimal::from(100_000),
        quantity: 1_000_000,
    };
    assert_eq!(
        charges(&[big], &cfg().costs).unwrap().sebi,
        dec("100000.00")
    );
}

#[test]
fn costs_gst_includes_ipft_and_the_total_includes_everything() {
    let fill = CostFill {
        side: Side::Buy,
        price: Decimal::from(1000),
        quantity: 100_000,
    };
    let got = charges(&[fill], &cfg().costs).unwrap();
    assert_eq!(got.ipft, dec("0.10"));
    assert_eq!(got.gst, dec("6417.02")); // 18 % of (20 + 35,530 + 100 + 0.10)
    assert_eq!(got.total().unwrap(), dec("45067.12"));
}

#[test]
fn costs_a_one_rupee_gain_can_pay_ten_paise() {
    let got = cost_test(dec("0.10"), Decimal::ONE, dec("0.20")).unwrap();
    assert_eq!(
        (got.verdict, got.cost_share),
        (CostVerdict::Ok, Some(dec("0.1")))
    );
}

// --- sizing edges ---

fn size_of(capital: &str, pct: &str, lot_size: i64, risk_per_lot: &str) -> Sizing {
    size(
        &SizeInputs {
            mode: Mode::Live,
            sleeve_capital_inr: dec(capital),
            risk_per_trade_pct: dec(pct),
            max_lots: 50,
            risk_per_lot_inr: dec(risk_per_lot),
            lot_size: Some(lot_size),
            real_journal_rows: 5,
        },
        &cfg().sizing,
        &OptionsCeilings::default(),
    )
    .unwrap()
}

fn size_capital(capital: &str) -> Sizing {
    size_of(capital, "1.0", LOT, "1000")
}

#[test]
fn sizing_a_rejection_carries_no_budget_and_no_half_size() {
    let got = size_capital("0");
    assert_eq!(got.rejection, Some(SizingReject::RejectedNoSleeveCapital));
    assert_eq!(
        (got.risk_budget_inr, got.r_inr, got.half_size),
        (Decimal::ZERO, Decimal::ZERO, false)
    );
}

#[test]
fn sizing_a_lot_size_of_one_and_a_rupee_of_risk_are_sizeable() {
    let got = size_of("100000", "1.0", 1, "1");
    assert_eq!(got.lots, OptionsCeilings::default().max_lots_max);
}

#[test]
fn sizing_one_rupee_of_capital_is_capital() {
    assert_eq!(
        size_capital("1").rejection,
        Some(SizingReject::RejectedBudget)
    );
}

#[test]
fn sizing_the_budget_multiplies_capital_by_the_pct() {
    let got = size_of("2000000", "0.5", LOT, "1000");
    assert_eq!(got.risk_budget_inr, Decimal::from(10000));
}

#[test]
fn sizing_the_premium_cap_at_one_rupee_and_at_the_boundary() {
    let cap = cfg().directional.premium_cap_pct;
    assert!(!premium_cap_ok(1, Decimal::from(150), LOT, Decimal::ONE, cap).unwrap());
    // 9,750 = 10 % exactly
    assert!(premium_cap_ok(1, Decimal::from(150), LOT, Decimal::from(97_500), cap).unwrap());
}

#[test]
fn sizing_a_one_rupee_pool_is_a_pool() {
    let got = margin_check(
        Mode::Paper,
        Decimal::from(50000),
        None,
        Decimal::ONE,
        Decimal::ZERO,
    )
    .unwrap();
    assert_eq!(got.code, MarginCode::RejectedMargin);
}

// --- risk edges ---

fn wed() -> NaiveDate {
    d(2026, 10, 21)
}

fn week() -> Vec<NaiveDate> {
    vec![d(2026, 10, 19), d(2026, 10, 20), wed(), d(2026, 10, 22)]
}

fn trade(day: NaiveDate, r: &str) -> RealisedTrade {
    RealisedTrade {
        sleeve: Sleeve::O2,
        trade_date: day,
        net_pnl_inr: dec(r) * Decimal::from(1000),
        r_multiple: dec(r),
    }
}

fn eval(history: Vec<RealisedTrade>, r: &str) -> Option<Pause> {
    evaluate_sleeve(
        &SleeveDay {
            sleeve: Sleeve::O2,
            today: wed(),
            history,
            today_marked_inr: Decimal::ZERO,
            r_today_inr: dec(r),
            trading_days: week(),
        },
        &cfg().risk,
    )
    .unwrap()
}

#[test]
fn risk_a_monday_row_is_in_its_week() {
    let got = eval(vec![trade(d(2026, 10, 19), "-4")], "1000").expect("a pause");
    assert_eq!(got.reasons, vec![PauseReason::WeeklyR]);
}

#[test]
fn risk_the_week_ends_on_its_sunday() {
    let mut days = week();
    days.push(d(2026, 10, 26));
    assert_eq!(last_trading_day_of_week(wed(), &days), d(2026, 10, 22));
}

#[test]
fn risk_last_months_losses_are_not_this_months() {
    assert_eq!(eval(vec![trade(d(2026, 9, 29), "-8")], "1000"), None);
}

#[test]
fn risk_an_r_of_one_rupee_is_an_r() {
    assert_eq!(eval(Vec::new(), "1"), None);
}

#[test]
fn risk_the_breach_fraction_multiplies() {
    let half = RiskConfig {
        budget_breach_frac: dec("0.5"),
        ..RiskConfig::default()
    };
    assert!(trade_breached(Decimal::from(600), Decimal::from(1000), &half).unwrap());
}

#[test]
fn risk_one_rupee_of_capital_and_one_rupee_settings() {
    let limits = book_limits(
        &[Decimal::ONE],
        Decimal::ONE,
        Decimal::ONE,
        &cfg().risk,
        &OptionsCeilings::default(),
    )
    .unwrap();
    assert_eq!(
        limits,
        Some(BookLimits {
            daily_loss_inr: Decimal::ONE,
            monthly_pause_inr: Decimal::ONE,
        })
    );
}

// --- journal edges ---

#[test]
fn journal_an_r_of_one_rupee() {
    assert_eq!(
        r_multiple(Decimal::from(5), Decimal::ONE).unwrap(),
        Decimal::from(5)
    );
}

#[test]
fn journal_a_scratch_is_not_a_win() {
    let row = |pnl: i64| JournalRow {
        net_pnl_inr: Decimal::from(pnl),
        ..JournalRow::new(Sleeve::O2, wed(), true, SizingMode::PaperOneLot, true)
    };
    let rows = [row(0), row(1)];
    assert_eq!(summarize_one(&rows).unwrap().win_rate, Some(dec("0.5")));
}

// --- execution edges ---

#[test]
fn execution_the_input_order_never_changes_the_leg_order() {
    for roles in [[LC, LP, SC, SP], [SP, SC, LP, LC], [LC, SC, LP, SP]] {
        assert_eq!(entry_sequence(&roles).unwrap(), vec![LP, LC, SP, SC]);
        assert_eq!(exit_sequence(&roles), vec![SC, SP, LC, LP]);
    }
}

#[test]
fn execution_never_naked_checks_each_side_and_treats_a_missing_leg_as_zero() {
    let book = |pairs: &[(LegRole, i64)]| pairs.iter().copied().collect::<BTreeMap<_, _>>();
    assert!(!never_naked(&book(&[(SC, 1), (LC, 0), (SP, 0), (LP, 5)])));
    assert!(!never_naked(&book(&[(SP, 1), (LP, 0), (SC, 0), (LC, 5)])));
    assert!(!never_naked(&book(&[(SC, 1)])));
    assert!(!never_naked(&book(&[(SP, 1)])));
}

#[test]
fn execution_a_lone_long_is_closable() {
    assert_eq!(next_exit_leg(&[(LC, 5)]), Some(LC));
}

#[test]
fn execution_the_first_two_attempts_are_not_marketable() {
    for number in [1, 2] {
        let attempt = next_attempt(
            number,
            Side::Buy,
            Decimal::from(100),
            dec("100.5"),
            tick(),
            true,
            &cfg().execution,
        )
        .unwrap()
        .expect("an attempt");
        assert!(!attempt.marketable);
    }
}

fn level(price: Decimal, quantity: i64) -> Level {
    Level { price, quantity }
}

#[test]
fn execution_one_unit_is_a_quantity() {
    let got = simulate_fill(
        Side::Buy,
        &[level(Decimal::from(10), 5)],
        1,
        Decimal::from(10),
        tick(),
        &cfg().execution,
    )
    .unwrap();
    assert_eq!(got.filled, 1);
}

#[test]
fn execution_latency_is_ticks_times_latency_ticks() {
    let ladder = [level(Decimal::from(100), 10), level(Decimal::from(100), 10)];
    let config = ExecutionConfig {
        latency_ticks: 2,
        ..ExecutionConfig::default()
    };
    let got = simulate_fill(Side::Buy, &ladder, 20, Decimal::from(101), tick(), &config).unwrap();
    assert_eq!(got.avg_price, Some(dec("100.05"))); // 10 @ 100.00 + 10 @ 100.10
}

#[test]
fn execution_zero_price_and_zero_quantity_levels_are_ignored_and_cheap_levels_are_not() {
    let ladder = [
        level(Decimal::ZERO, 65),
        level(dec("0.50"), 1),
        level(dec("0.55"), 64),
    ];
    let got = simulate_fill(
        Side::Buy,
        &ladder,
        65,
        Decimal::ONE,
        tick(),
        &cfg().execution,
    )
    .unwrap();
    assert_eq!(got.filled, 65);
    let cost = num::add(dec("0.50"), num::muli(dec("0.60"), 64).unwrap()).unwrap();
    assert_eq!(
        got.avg_price,
        Some(num::div(cost, Decimal::from(65)).unwrap())
    );
}

#[test]
fn execution_a_sell_at_exactly_its_limit_fills() {
    let got = simulate_fill(
        Side::Sell,
        &[level(dec("99.95"), 65)],
        65,
        dec("99.95"),
        tick(),
        &cfg().execution,
    )
    .unwrap();
    assert!(got.complete());
}
