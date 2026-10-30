//! Port of `tests/test_options_greeks.py` — `04` §2: the chain, the parity forward, Black-76
//! greeks and the IV solver. Hypothesis properties run as seeded deterministic samples.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod options_common;

use baskfy_core::error::CoreError;
use baskfy_core::options::chain::{
    Illiquid, Level, OptionQuote, atm, depth_available, is_liquid, is_stale_index, is_stale_quote,
    is_stale_scan, parity_forward, quote_greeks, snapshot_strikes, strike_step,
};
use baskfy_core::options::config::{ChainConfig, OiUnit, OptionType, OptionsConfig, Side};
use baskfy_core::options::greeks::{
    Bracket, Greeks, SolveLimits, black76_greeks, black76_price, implied_vol, intrinsic, solve,
    year_fraction,
};
use baskfy_core::options::num::from_float;
use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime, TimeDelta};
use options_common::{
    LOT, QuoteOpts, Rng, d, dec, ist_dt, quote, quote_with, strikes_around, tick,
};
use rust_decimal::Decimal;

const CE: OptionType = OptionType::Ce;
const PE: OptionType = OptionType::Pe;

fn chain() -> ChainConfig {
    OptionsConfig::default().chain
}
fn settle() -> NaiveTime {
    OptionsConfig::default().calendar.market_close
}
fn rate() -> f64 {
    chain().rate
}
fn expiry() -> NaiveDate {
    d(2026, 10, 27)
}
fn now() -> DateTime<FixedOffset> {
    ist_dt(2026, 10, 20, 10, 0, 0)
}
fn t() -> f64 {
    year_fraction(&now(), expiry(), settle())
}

fn approx_rel(a: f64, b: f64, rel: f64) -> bool {
    (a - b).abs() <= rel * b.abs()
}
fn approx_abs(a: f64, b: f64, abs: f64) -> bool {
    (a - b).abs() <= abs
}
/// pytest.approx default: rel 1e-6 or abs 1e-12.
fn approx(a: f64, b: f64) -> bool {
    (a - b).abs() <= (1e-6 * b.abs()).max(1e-12)
}

fn bracket() -> Bracket {
    let c = chain();
    Bracket {
        lower: c.iv_lower,
        upper: c.iv_upper,
        tolerance: c.iv_tolerance,
    }
}

fn solve_iv(price: f64, forward: f64, strike: f64, years: f64, kind: OptionType) -> Option<f64> {
    implied_vol(price, forward, strike, years, rate(), kind, bracket())
}

fn invalid(r: Result<impl std::fmt::Debug, CoreError>, needle: &str) {
    match r {
        Err(CoreError::InvalidInput(m)) => assert!(m.contains(needle), "{m}"),
        other => panic!("expected InvalidInput({needle}), got {other:?}"),
    }
}

// --- TestTime ---

#[test]
fn time_calendar_minutes_to_1530_on_expiry() {
    let n = ist_dt(2026, 10, 27, 15, 0, 0);
    assert!(approx_rel(
        year_fraction(&n, expiry(), settle()),
        30.0 / 525_600.0,
        1e-12
    ));
}

#[test]
fn time_seven_days_is_seven_calendar_days() {
    let n = ist_dt(2026, 10, 20, 15, 30, 0);
    assert!(approx_rel(
        year_fraction(&n, expiry(), settle()),
        7.0 / 365.0,
        1e-12
    ));
}

#[test]
fn time_after_settlement_is_negative() {
    assert!(year_fraction(&ist_dt(2026, 10, 27, 15, 31, 0), expiry(), settle()) < 0.0);
}

// --- TestBlack76 ---

#[test]
fn black76_hull_worked_example() {
    let p = black76_price(20.0, 20.0, 4.0 / 12.0, 0.09, 0.25, PE);
    assert!(approx_abs(p, 1.1166, 5e-4), "{p}");
}

fn price_draw(r: &mut Rng) -> f64 {
    r.float(15000.0, 35000.0)
}
fn vol_draw(r: &mut Rng) -> f64 {
    r.float(0.05, 1.5)
}
fn year_draw(r: &mut Rng) -> f64 {
    r.float(1.0 / (365.0 * 24.0), 10.0 / 365.0)
}

#[test]
fn black76_put_call_parity_to_1e9() {
    let mut r = Rng(0x9E37_79B9_7F4A_7C15);
    for _ in 0..300 {
        let (forward, m, years, vol) = (
            price_draw(&mut r),
            r.float(0.8, 1.2),
            year_draw(&mut r),
            vol_draw(&mut r),
        );
        let strike = forward * m;
        let call = black76_price(forward, strike, years, rate(), vol, CE);
        let put = black76_price(forward, strike, years, rate(), vol, PE);
        let gap = (call - put - (-rate() * years).exp() * (forward - strike)).abs();
        assert!(
            gap <= 1e-9,
            "F={forward} K={strike} t={years} v={vol} gap={gap}"
        );
    }
}

#[test]
fn black76_delta_parity() {
    let mut r = Rng(0xD1B5_4A32_D192_ED03);
    for _ in 0..200 {
        let (forward, m, years, vol) = (
            price_draw(&mut r),
            r.float(0.9, 1.1),
            year_draw(&mut r),
            vol_draw(&mut r),
        );
        let strike = forward * m;
        let call = black76_greeks(forward, strike, years, rate(), vol, CE).unwrap();
        let put = black76_greeks(forward, strike, years, rate(), vol, PE).unwrap();
        assert!(approx_abs(
            call.delta - put.delta,
            (-rate() * years).exp(),
            1e-12
        ));
        assert!(approx_rel(call.gamma, put.gamma, 1e-12));
        assert!(approx_rel(call.vega, put.vega, 1e-12));
    }
}

#[test]
fn black76_greeks_match_finite_differences() {
    for kind in [CE, PE] {
        for strike in [24500.0, 25000.0, 25600.0] {
            let (f, years, vol) = (25010.0, t(), 0.14);
            let g = black76_greeks(f, strike, years, rate(), vol, kind).unwrap();
            let h = 0.5;
            let up = black76_price(f + h, strike, years, rate(), vol, kind);
            let down = black76_price(f - h, strike, years, rate(), vol, kind);
            assert!(approx_abs(g.delta, (up - down) / (2.0 * h), 1e-6));
            let mid = black76_price(f, strike, years, rate(), vol, kind);
            assert!(approx_rel(g.gamma, (up - 2.0 * mid + down) / (h * h), 1e-3));
            let dv = 1e-4;
            let vega = (black76_price(f, strike, years, rate(), vol + dv, kind)
                - black76_price(f, strike, years, rate(), vol - dv, kind))
                / (2.0 * dv);
            assert!(approx_rel(g.vega, vega / 100.0, 1e-6));
            let dt_ = 1e-6;
            let theta = -(black76_price(f, strike, years + dt_, rate(), vol, kind)
                - black76_price(f, strike, years - dt_, rate(), vol, kind))
                / (2.0 * dt_);
            assert!(
                approx_rel(g.theta, theta / 365.0, 1e-4),
                "{kind:?} {strike}"
            );
        }
    }
}

#[test]
fn black76_greeks_refuse_degenerate_inputs() {
    invalid(
        black76_greeks(25000.0, 25000.0, 0.0, rate(), 0.2, CE),
        "positive",
    );
}

#[test]
fn black76_degenerate_price_is_discounted_intrinsic() {
    assert!(approx(
        black76_price(25100.0, 25000.0, 0.0, rate(), 0.2, CE),
        100.0
    ));
    assert_eq!(
        black76_price(25100.0, 25000.0, 0.0, rate(), 0.2, PE).to_bits(),
        0.0_f64.to_bits()
    );
    assert_eq!(
        black76_price(25100.0, 25000.0, -0.01, rate(), 0.2, CE).to_bits(),
        100.0_f64.to_bits()
    );
}

#[test]
fn black76_intrinsic_on_the_forward() {
    assert_eq!(
        intrinsic(25100.0, 25000.0, CE).to_bits(),
        100.0_f64.to_bits()
    );
    assert_eq!(intrinsic(25100.0, 25000.0, PE).to_bits(), 0.0_f64.to_bits());
    assert_eq!(
        intrinsic(24900.0, 25000.0, PE).to_bits(),
        100.0_f64.to_bits()
    );
}

// --- TestImpliedVol ---

#[test]
fn implied_vol_round_trips() {
    let mut r = Rng(0xA076_1D64_78BD_642F);
    let min_premium: f64 = chain().min_premium.to_string().parse().unwrap();
    let mut checked = 0;
    for _ in 0..3000 {
        let (forward, m, years, vol) = (
            price_draw(&mut r),
            r.float(0.95, 1.05),
            year_draw(&mut r),
            vol_draw(&mut r),
        );
        let kind = if r.below(2) == 0 { CE } else { PE };
        let strike = forward * m;
        let price = black76_price(forward, strike, years, rate(), vol, kind);
        if price < min_premium || price - intrinsic(forward, strike, kind) <= 0.05 {
            continue;
        }
        let solved = solve_iv(price, forward, strike, years, kind).expect("solves");
        assert!(
            approx_abs(solved, vol, 1e-5),
            "F={forward} K={strike} t={years} v={vol} got {solved}"
        );
        checked += 1;
        if checked == 300 {
            break;
        }
    }
    assert!(checked >= 100);
}

#[test]
fn implied_vol_refuses_a_price_above_the_highest_vol() {
    let ceiling = black76_price(25000.0, 25000.0, t(), rate(), chain().iv_upper, CE);
    assert!(solve_iv(ceiling * 1.01, 25000.0, 25000.0, t(), CE).is_none());
}

#[test]
fn implied_vol_refuses_a_price_below_the_lowest_vol() {
    let floor = black76_price(25000.0, 25500.0, t(), rate(), chain().iv_lower, CE);
    assert!(solve_iv(floor / 2.0, 25000.0, 25500.0, t(), CE).is_none());
}

#[test]
fn implied_vol_refuses_degenerate_inputs() {
    assert!(solve_iv(0.0, 25000.0, 25000.0, t(), CE).is_none());
    assert!(solve_iv(100.0, 25000.0, 25000.0, 0.0, CE).is_none());
}

// --- TestSolveRefusals ---

fn solve_ce(mid: f64, forward: f64, strike: f64, years: f64) -> Option<Greeks> {
    solve(
        mid,
        forward,
        strike,
        years,
        rate(),
        CE,
        SolveLimits {
            min_premium: chain().min_premium.to_string().parse().unwrap(),
            tick: 0.05,
            bracket: bracket(),
        },
    )
    .unwrap()
}

#[test]
fn solve_below_min_premium() {
    assert!(solve_ce(0.45, 25000.0, 26500.0, t()).is_none());
}

#[test]
fn solve_below_intrinsic_plus_a_tick() {
    assert!(solve_ce(100.04, 25100.0, 25000.0, t()).is_none());
}

#[test]
fn solve_no_convergence() {
    assert!(solve_ce(20000.0, 25000.0, 25000.0, t()).is_none());
}

#[test]
fn solve_after_settlement() {
    assert!(solve_ce(100.0, 25000.0, 25000.0, -0.001).is_none());
}

#[test]
fn solve_a_good_quote_solves() {
    let price = black76_price(25000.0, 25000.0, t(), rate(), 0.13, CE);
    assert!(solve_ce(price, 25000.0, 25000.0, t()).is_some());
}

// --- TestParityForward ---

fn on_expiry() -> QuoteOpts {
    QuoteOpts {
        expiry: expiry(),
        ..QuoteOpts::default()
    }
}

fn parity_quotes(forward: f64, strikes: &[i64], vol: f64) -> Vec<OptionQuote> {
    let half = dec("0.5");
    let mut out = Vec::new();
    for &k in strikes {
        for kind in [CE, PE] {
            #[allow(clippy::cast_precision_loss)]
            let p = from_float(black76_price(forward, k as f64, t(), rate(), vol, kind)).unwrap();
            out.push(quote_with(
                Decimal::from(k),
                kind,
                Some(p - half),
                Some(p + half),
                on_expiry(),
            ));
        }
    }
    out
}

#[test]
fn parity_recovers_the_forward_from_the_atm_pair() {
    let quotes = parity_quotes(25012.5, &[24950, 25000, 25050], 0.13);
    let found = parity_forward(&quotes, dec("25010"), Decimal::from(50), t(), rate())
        .unwrap()
        .unwrap();
    let f: f64 = found.to_string().parse().unwrap();
    assert!(approx_abs(f, 25012.5, 1e-6), "{found}");
}

#[test]
fn parity_falls_back_to_the_next_nearest_pair() {
    let quotes = parity_quotes(25012.5, &[24950, 25000, 25050], 0.13);
    let broken: Vec<OptionQuote> = quotes
        .into_iter()
        .map(|q| {
            if q.strike == Decimal::from(25000) && q.option_type == PE {
                quote_with(Decimal::from(25000), PE, None, Some(dec("90")), on_expiry())
            } else {
                q
            }
        })
        .collect();
    let found = parity_forward(&broken, dec("25010"), Decimal::from(50), t(), rate())
        .unwrap()
        .unwrap();
    let f: f64 = found.to_string().parse().unwrap();
    assert!(approx_abs(f, 25012.5, 1e-6), "{found}");
}

#[test]
fn parity_no_pair_no_forward() {
    let only_calls = vec![quote_with(
        Decimal::from(25000),
        CE,
        Some(dec("100")),
        Some(dec("101")),
        on_expiry(),
    )];
    assert!(
        parity_forward(
            &only_calls,
            Decimal::from(25000),
            Decimal::from(50),
            t(),
            rate()
        )
        .unwrap()
        .is_none()
    );
}

#[test]
fn parity_one_expiry_only() {
    let mixed = vec![
        quote_with(
            Decimal::from(25000),
            CE,
            Some(dec("1")),
            Some(dec("2")),
            on_expiry(),
        ),
        quote_with(
            Decimal::from(25000),
            PE,
            Some(dec("1")),
            Some(dec("2")),
            QuoteOpts {
                expiry: d(2026, 11, 3),
                ..QuoteOpts::default()
            },
        ),
    ];
    invalid(
        parity_forward(&mixed, Decimal::from(25000), Decimal::from(50), t(), rate()),
        "one expiry",
    );
}

#[test]
fn parity_quote_greeks_on_the_forward() {
    let price = from_float(black76_price(25012.5, 25100.0, t(), rate(), 0.13, CE)).unwrap();
    let half = dec("0.5");
    let q = quote_with(
        Decimal::from(25100),
        CE,
        Some(price - half),
        Some(price + half),
        on_expiry(),
    );
    let g = quote_greeks(&q, dec("25012.5"), &now(), tick(), &chain(), settle())
        .unwrap()
        .unwrap();
    assert!(approx_abs(g.iv, 0.13, 1e-5));
    assert!(0.0 < g.delta && g.delta < 0.5);
}

#[test]
fn parity_quote_greeks_refuse_without_a_mid() {
    let q = quote_with(Decimal::from(25100), CE, None, Some(dec("10")), on_expiry());
    assert!(
        quote_greeks(&q, Decimal::from(25000), &now(), tick(), &chain(), settle())
            .unwrap()
            .is_none()
    );
}

// --- TestStrikes ---

#[test]
fn strikes_atm_ties_go_to_the_lower_strike() {
    for (spot, expected) in [
        ("25024", 25000),
        ("25025", 25000),
        ("25026", 25050),
        ("24975", 24950),
    ] {
        assert_eq!(
            atm(dec(spot), Decimal::from(50)).unwrap(),
            Decimal::from(expected),
            "{spot}"
        );
    }
}

#[test]
fn strikes_atm_refuses_a_zero_step() {
    invalid(atm(Decimal::from(25000), Decimal::ZERO), "positive");
}

#[test]
fn strikes_the_step_is_read_near_the_money() {
    let step = strike_step(
        &strikes_around(25000),
        Decimal::from(25000),
        chain().snapshot_strikes,
    )
    .unwrap();
    assert_eq!(step, Some(Decimal::from(50)));
}

#[test]
fn strikes_far_from_the_money_the_grid_is_wider() {
    let step = strike_step(&strikes_around(25000), Decimal::from(26900), 3).unwrap();
    assert_eq!(step, Some(Decimal::from(100)));
}

#[test]
fn strikes_a_modal_tie_goes_to_the_finer_grid() {
    let strikes: Vec<Decimal> = [100, 150, 200, 300, 400]
        .into_iter()
        .map(Decimal::from)
        .collect();
    assert_eq!(
        strike_step(&strikes, Decimal::from(250), 2).unwrap(),
        Some(Decimal::from(50))
    );
}

#[test]
fn strikes_one_strike_has_no_step() {
    assert_eq!(
        strike_step(&[Decimal::from(25000)], Decimal::from(25000), 15).unwrap(),
        None
    );
}

#[test]
fn strikes_snapshot_strikes_either_side_of_atm() {
    let n = chain().snapshot_strikes;
    let chosen =
        snapshot_strikes(&strikes_around(25000), dec("25010"), Decimal::from(50), n).unwrap();
    assert_eq!(chosen.len(), usize::try_from(2 * n + 1).unwrap());
    assert_eq!(chosen[0], Decimal::from(24250));
    assert_eq!(chosen[chosen.len() - 1], Decimal::from(25750));
}

// --- TestLiquidity ---

const QTY: i64 = LOT * 2;

fn ok(q: &OptionQuote, qty: i64, side: Side, cfg: &ChainConfig, wing: bool) -> bool {
    is_liquid(q, qty, side, LOT, cfg, wing).unwrap().ok
}
fn reasons(q: &OptionQuote, qty: i64, side: Side, cfg: &ChainConfig, wing: bool) -> Vec<Illiquid> {
    is_liquid(q, qty, side, LOT, cfg, wing).unwrap().reasons
}

#[test]
fn liquidity_mid_and_spread() {
    let q = quote(25000, CE, Some("99"), Some("101"));
    assert_eq!(q.mid().unwrap(), Some(Decimal::from(100)));
    assert_eq!(q.spread_pct().unwrap(), Some(Decimal::from(2)));
}

#[test]
fn liquidity_a_liquid_contract() {
    assert!(ok(
        &quote(25000, CE, Some("99"), Some("101")),
        QTY,
        Side::Sell,
        &chain(),
        false
    ));
}

#[test]
fn liquidity_spread_boundary() {
    let at_limit = quote(25000, CE, Some("98.5"), Some("101.5"));
    assert!(ok(&at_limit, QTY, Side::Buy, &chain(), false));
    let wide = quote(25000, CE, Some("98.4"), Some("101.6"));
    assert_eq!(
        reasons(&wide, QTY, Side::Buy, &chain(), false),
        vec![Illiquid::SpreadTooWide]
    );
}

#[test]
fn liquidity_depth_is_read_on_the_side_the_order_takes() {
    let thin_asks = quote_with(
        Decimal::from(25000),
        CE,
        Some(dec("99")),
        Some(dec("101")),
        QuoteOpts {
            asks: Some(vec![Level {
                price: Decimal::from(101),
                quantity: 10,
            }]),
            ..QuoteOpts::default()
        },
    );
    assert!(ok(&thin_asks, QTY, Side::Sell, &chain(), false));
    assert_eq!(
        reasons(&thin_asks, QTY, Side::Buy, &chain(), false),
        vec![Illiquid::DepthTooThin]
    );
}

#[test]
fn liquidity_depth_counts_only_the_first_levels() {
    let ladder: Vec<Level> = (0..5)
        .map(|i| Level {
            price: Decimal::from(101 + i),
            quantity: 40,
        })
        .collect();
    let q = quote_with(
        Decimal::from(25000),
        CE,
        Some(dec("99")),
        Some(dec("101")),
        QuoteOpts {
            asks: Some(ladder),
            ..QuoteOpts::default()
        },
    );
    assert_eq!(depth_available(&q, Side::Buy, chain().depth_levels), 120);
    assert!(!ok(&q, 130, Side::Buy, &chain(), false));
    assert!(ok(&q, 120, Side::Buy, &chain(), false));
}

fn with_oi(oi: i64) -> OptionQuote {
    quote_with(
        Decimal::from(25000),
        CE,
        Some(dec("99")),
        Some(dec("101")),
        QuoteOpts {
            oi,
            ..QuoteOpts::default()
        },
    )
}

#[test]
fn liquidity_open_interest_floor() {
    let floor = chain().min_oi_lots * LOT;
    assert!(ok(&with_oi(floor), QTY, Side::Buy, &chain(), false));
    assert_eq!(
        reasons(&with_oi(floor - 1), QTY, Side::Buy, &chain(), false),
        vec![Illiquid::OiTooLow]
    );
}

#[test]
fn liquidity_open_interest_in_lots_is_converted() {
    let lots = ChainConfig {
        oi_unit: OiUnit::Lots,
        ..ChainConfig::default()
    };
    let q = with_oi(chain().min_oi_lots);
    assert!(ok(&q, QTY, Side::Buy, &lots, false));
    assert!(!ok(&q, QTY, Side::Buy, &chain(), false));
}

#[test]
fn liquidity_a_wing_needs_only_depth() {
    let wide_thin_oi = quote_with(
        Decimal::from(25000),
        CE,
        Some(dec("1")),
        Some(dec("2")),
        QuoteOpts {
            oi: 0,
            ..QuoteOpts::default()
        },
    );
    assert!(ok(&wide_thin_oi, QTY, Side::Buy, &chain(), true));
    let no_depth = quote_with(
        Decimal::from(25000),
        CE,
        Some(dec("1")),
        Some(dec("2")),
        QuoteOpts {
            asks: Some(Vec::new()),
            oi: 0,
            ..QuoteOpts::default()
        },
    );
    assert_eq!(
        reasons(&no_depth, QTY, Side::Buy, &chain(), true),
        vec![Illiquid::DepthTooThin]
    );
}

#[test]
fn liquidity_every_reason_is_reported() {
    let q = quote_with(
        Decimal::from(25000),
        CE,
        None,
        Some(dec("101")),
        QuoteOpts {
            asks: Some(Vec::new()),
            oi: 0,
            ..QuoteOpts::default()
        },
    );
    assert_eq!(
        reasons(&q, QTY, Side::Buy, &chain(), false),
        vec![
            Illiquid::NoQuote,
            Illiquid::DepthTooThin,
            Illiquid::OiTooLow
        ]
    );
}

// --- TestStaleness ---

#[test]
fn staleness_quote() {
    let ts = ist_dt(2026, 10, 27, 10, 0, 0);
    assert!(!is_stale_quote(
        &ts,
        &(ts + TimeDelta::seconds(15)),
        &chain()
    ));
    assert!(is_stale_quote(
        &ts,
        &(ts + TimeDelta::seconds(16)),
        &chain()
    ));
}

#[test]
fn staleness_index() {
    let ts = ist_dt(2026, 10, 27, 14, 0, 0);
    assert!(!is_stale_index(
        Some(&ts),
        &(ts + TimeDelta::seconds(30)),
        &chain()
    ));
    assert!(is_stale_index(
        Some(&ts),
        &(ts + TimeDelta::seconds(31)),
        &chain()
    ));
    assert!(is_stale_index(None, &ts, &chain()));
}

#[test]
fn staleness_scan_after_two_minutes() {
    let ts = ist_dt(2026, 10, 27, 11, 0, 0);
    assert!(!is_stale_scan(&ts, &(ts + TimeDelta::minutes(2)), &chain()));
    assert!(is_stale_scan(
        &ts,
        &(ts + TimeDelta::minutes(2) + TimeDelta::seconds(1)),
        &chain()
    ));
}
