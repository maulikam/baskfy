//! `tests/test_metrics.py`, `test_drawdown.py`, `test_xirr.py` and `test_twr_chaining.py`.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp,
    clippy::cast_precision_loss,
    clippy::cast_possible_truncation,
    clippy::cast_sign_loss,
    missing_docs
)]

use std::collections::HashMap;

use baskfy_desk::lanes::analytics::analytics_views::metrics::{
    self as m, Capital, Order, Series, Trade,
};
use chrono::{Duration, NaiveDate};

fn d(s: &str) -> NaiveDate {
    NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap()
}

fn days(n: usize, start: &str) -> Vec<NaiveDate> {
    (0..n)
        .map(|i| d(start) + Duration::days(i as i64))
        .collect()
}

fn series(values: &[f64], start: &str) -> Series {
    days(values.len(), start)
        .into_iter()
        .zip(values.iter().copied())
        .collect()
}

fn ts(date: &str) -> f64 {
    d(date).and_hms_opt(0, 0, 0).unwrap().and_utc().timestamp() as f64
}

fn approx(a: f64, b: f64, tol: f64) -> bool {
    (a - b).abs() <= tol.max(1e-6 * b.abs())
}

fn linspace(a: f64, b: f64, n: usize) -> Vec<f64> {
    m::linspace(a, b, n)
}

struct Rng(u64);

impl Rng {
    fn uniform(&mut self) -> f64 {
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        ((self.0 >> 11) as f64 + 0.5) / (1u64 << 53) as f64
    }
    fn normal(&mut self, mu: f64, sigma: f64, n: usize) -> Vec<f64> {
        (0..n)
            .map(|_| {
                let (u, v) = (self.uniform(), self.uniform());
                mu + sigma * (-2.0 * u.ln()).sqrt() * (2.0 * std::f64::consts::PI * v).cos()
            })
            .collect()
    }
}

fn vals(s: &Series) -> Vec<f64> {
    s.iter().map(|p| p.1).collect()
}

fn std1(v: &[f64]) -> f64 {
    m::nanstd(v, 1)
}

// =============================================================================================
// returns
// =============================================================================================

#[test]
fn absolute_and_cagr() {
    let s = vec![(d("2026-01-01"), 100.0), (d("2028-01-01"), 121.0)];
    assert!(approx(m::absolute(&s), 0.21, 1e-12));
    assert!(approx(m::cagr(&s), 0.0998, 1e-3));
}

#[test]
fn cagr_of_flat_series_is_zero() {
    let s = vec![(d("2026-01-01"), 100.0), (d("2027-01-01"), 100.0)];
    assert!(approx(m::cagr(&s), 0.0, 1e-12));
}

#[test]
fn rolling_returns_windows() {
    let s = series(&linspace(100.0, 200.0, 300), "2026-01-01");
    let rr = m::rolling_returns(&s, &[("5d", 5), ("20d", 20)]);
    assert_eq!(
        rr.iter().map(|c| c.0.as_str()).collect::<Vec<_>>(),
        ["5d", "20d"]
    );
    assert_eq!(rr[0].1.iter().filter(|p| p.1.is_nan()).count(), 5);
    assert!(approx(rr[0].1[299].1, s[299].1 / s[294].1 - 1.0, 1e-12));
}

#[test]
fn monthly_returns_measure_the_first_partial_month() {
    let n = (d("2026-03-31") - d("2026-01-15")).num_days() as usize + 1;
    let s = series(&linspace(100.0, 130.0, n), "2026-01-15");
    let mr = m::monthly_returns(&s);
    assert_eq!(mr.len(), 3);
    let prod: f64 = mr.iter().map(|p| 1.0 + p.1).product();
    assert!(approx(prod, s[n - 1].1 / s[0].1, 1e-9));
}

#[test]
fn monthly_return_matrix_shape() {
    let n = (d("2027-06-30") - d("2026-01-01")).num_days() as usize + 1;
    let s = series(&linspace(100.0, 150.0, n), "2026-01-01");
    let mat = m::monthly_return_matrix(&s, true);
    assert_eq!(mat.iter().map(|r| r.0).collect::<Vec<_>>(), [2026, 2027]);
    assert!(mat[1].1[11].is_nan());
    assert!(!mat[1].1[5].is_nan());
}

// =============================================================================================
// risk
// =============================================================================================

#[test]
fn ann_volatility_scales_by_sqrt_252() {
    let v: Vec<f64> = (0..100)
        .map(|i| if i % 2 == 0 { 0.01 } else { -0.01 })
        .collect();
    let r = series(&v, "2026-01-01");
    assert!(approx(
        m::ann_volatility(&r),
        std1(&v) * 252f64.sqrt(),
        1e-12
    ));
}

#[test]
fn downside_deviation_ignores_upside() {
    let up = series(&[0.01; 50], "2026-01-01");
    assert!(approx(m::downside_deviation(&up, 0.0, 252), 0.0, 1e-12));
    let v: Vec<f64> = (0..50)
        .map(|i| if i % 2 == 0 { 0.02 } else { -0.02 })
        .collect();
    assert!(m::downside_deviation(&series(&v, "2026-01-01"), 0.0, 252) > 0.0);
}

#[test]
fn downside_deviation_is_below_total_volatility() {
    let v: Vec<f64> = [0.03, -0.01, 0.02, -0.005, 0.01].repeat(10);
    let r = series(&v, "2026-01-01");
    assert!(m::downside_deviation(&r, 0.0, 252) < m::ann_volatility(&r));
}

#[test]
fn sharpe_of_a_riskless_gain_is_undefined_not_zero() {
    assert!(m::sharpe(&series(&[0.001; 30], "2026-01-01"), 0.0).is_nan());
}

#[test]
fn sharpe_of_a_flat_zero_series_is_zero() {
    assert_eq!(m::sharpe(&series(&[0.0; 30], "2026-01-01"), 0.0), 0.0);
}

#[test]
fn sharpe_falls_when_risk_free_rises() {
    let r = series(&Rng(1).normal(0.001, 0.01, 250), "2026-01-01");
    assert!(m::sharpe(&r, 0.0) > m::sharpe(&r, 0.06));
}

#[test]
fn sortino_exceeds_sharpe_for_upward_skew() {
    let mut v = vec![0.05; 20];
    v.extend(vec![-0.005; 80]);
    let r = series(&v, "2026-01-01");
    assert!(m::sortino(&r, 0.0) > m::sharpe(&r, 0.0));
}

#[test]
fn beta_of_a_series_against_itself_is_one() {
    let r = series(&Rng(2).normal(0.0, 0.01, 200), "2026-01-01");
    assert!(approx(m::beta(&r, &r), 1.0, 1e-12));
    assert!(m::alpha_jensen(&r, &r, 0.0).abs() <= 1e-12);
    assert!(approx(m::tracking_error(&r, &r), 0.0, 1e-12));
}

#[test]
fn beta_of_a_doubled_series_is_two() {
    let b = series(&Rng(3).normal(0.0, 0.01, 200), "2026-01-01");
    let p: Series = b.iter().map(|(t, v)| (*t, v * 2.0)).collect();
    assert!(approx(m::beta(&p, &b), 2.0, 1e-12));
}

#[test]
fn beta_is_zero_against_a_constant_benchmark() {
    let r = series(&Rng(4).normal(0.0, 0.01, 50), "2026-01-01");
    assert_eq!(m::beta(&r, &series(&[0.0; 50], "2026-01-01")), 0.0);
}

#[test]
fn information_ratio_sign_follows_excess_return() {
    let mut rng = Rng(5);
    let bv = rng.normal(0.0005, 0.01, 250);
    let noise = rng.normal(0.0, 0.002, 250);
    let b = series(&bv, "2026-01-01");
    let up: Vec<f64> = bv.iter().zip(&noise).map(|(x, n)| x + 0.0004 + n).collect();
    let down: Vec<f64> = bv.iter().zip(&noise).map(|(x, n)| x - 0.0004 + n).collect();
    assert!(m::information_ratio(&series(&up, "2026-01-01"), &b) > 0.0);
    assert!(m::information_ratio(&series(&down, "2026-01-01"), &b) < 0.0);
}

#[test]
fn information_ratio_is_undefined_with_zero_tracking_error() {
    let bv = Rng(9).normal(0.0005, 0.01, 100);
    let b = series(&bv, "2026-01-01");
    let p: Vec<f64> = bv.iter().map(|x| x + 0.0004).collect();
    assert!(m::information_ratio(&series(&p, "2026-01-01"), &b).is_nan());
    assert_eq!(m::information_ratio(&b, &b), 0.0);
}

#[test]
fn parametric_var_matches_the_formula() {
    let v = Rng(6).normal(0.0, 0.01, 500);
    let r = series(&v, "2026-01-01");
    assert!(approx(
        m::var_parametric(&r, Some(1.645)),
        -(m::nanmean(&v) - 1.645 * std1(&v)),
        1e-15
    ));
}

#[test]
fn alignment_handles_mismatched_dates() {
    let a = series(&[0.01, 0.02, 0.03], "2026-01-01");
    let b = series(&[0.01, 0.02], "2026-01-02");
    let beta = m::beta(&a, &b);
    assert!(beta.is_finite());
    assert!(approx(beta, 1.0, 1e-12));
    assert!(m::tracking_error(&a, &b) >= 0.0);
}

// =============================================================================================
// trades
// =============================================================================================

#[allow(clippy::too_many_arguments)]
fn trade(
    symbol: &str,
    pnl: f64,
    costs: f64,
    score: f64,
    rank: f64,
    qty: f64,
    px: f64,
    reason: &str,
    exit: &str,
    fwd: f64,
) -> Trade {
    Trade {
        symbol: symbol.into(),
        pnl,
        costs: Some(costs),
        entry_score: Some(score),
        entry_rank: Some(rank),
        qty: Some(qty),
        entry_price: Some(px),
        exit_reason: Some(reason.into()),
        entry_ts: Some(ts("2026-01-01")),
        exit_ts: Some(ts(exit)),
        fwd_return_20d: Some(fwd),
        ..Trade::default()
    }
}

fn trades() -> Vec<Trade> {
    vec![
        trade(
            "DIXON",
            50_000.0,
            500.0,
            84.0,
            1.0,
            100.0,
            600.0,
            "rank_drop",
            "2026-03-01",
            -0.08,
        ),
        trade(
            "KAYNES",
            -20_000.0,
            400.0,
            82.0,
            2.0,
            50.0,
            3_000.0,
            "stop_loss",
            "2026-02-01",
            -0.15,
        ),
        trade(
            "POLYCAB",
            30_000.0,
            300.0,
            65.0,
            18.0,
            40.0,
            2_200.0,
            "rank_drop",
            "2026-04-01",
            0.05,
        ),
        trade(
            "SUZLON",
            -10_000.0,
            200.0,
            55.0,
            22.0,
            200.0,
            500.0,
            "stop_loss",
            "2026-02-15",
            0.10,
        ),
    ]
}

fn closed(symbol: &str, pnl: f64, exit: Option<f64>) -> Trade {
    Trade {
        symbol: symbol.into(),
        pnl,
        exit_ts: exit,
        ..Trade::default()
    }
}

#[test]
fn win_rate_and_averages() {
    let t = trades();
    assert!(approx(m::win_rate(&t), 0.5, 1e-12));
    assert!(approx(m::avg_win(&t), 40_000.0, 1e-9));
    assert!(approx(m::avg_loss(&t), -15_000.0, 1e-9));
    assert!(approx(m::payoff_ratio(&t), 40_000.0 / 15_000.0, 1e-12));
}

#[test]
fn profit_factor_and_expectancy() {
    let t = trades();
    assert!(approx(m::profit_factor(&t), 80_000.0 / 30_000.0, 1e-12));
    assert!(approx(
        m::expectancy(&t),
        0.5 * 40_000.0 + 0.5 * -15_000.0,
        1e-9
    ));
}

#[test]
fn profit_factor_is_infinite_without_losses() {
    assert_eq!(
        m::profit_factor(&[closed("A", 100.0, Some(ts("2026-01-01")))]),
        f64::INFINITY
    );
}

#[test]
fn empty_trades_do_not_raise() {
    assert_eq!(m::win_rate(&[]), 0.0);
    assert_eq!(m::expectancy(&[]), 0.0);
    assert!(m::per_symbol_contribution(&[]).is_empty());
}

#[test]
fn open_trades_are_excluded() {
    let t = [
        closed("A", 100.0, Some(ts("2026-01-01"))),
        closed("B", -999.0, None),
    ];
    assert_eq!(m::win_rate(&t), 1.0);
}

#[test]
fn avg_holding_period() {
    assert!(approx(
        m::avg_holding_period(&trades()),
        (59.0 + 31.0 + 90.0 + 45.0) / 4.0,
        0.1
    ));
}

#[test]
fn hit_rate_by_exit_reason() {
    let out = m::hit_rate_by_exit_reason(&trades());
    let get = |k: &str| out.iter().find(|g| g.key == k).unwrap();
    assert!(approx(get("rank_drop").hit_rate, 1.0, 1e-12));
    assert!(approx(get("stop_loss").hit_rate, 0.0, 1e-12));
    assert_eq!(get("rank_drop").trades, 2);
    assert_eq!(out[0].key, "rank_drop");
}

#[test]
fn turnover_and_cost_drag() {
    assert!(approx(
        m::turnover_pct(2_500_000.0, 10_000_000.0),
        25.0,
        1e-12
    ));
    assert_eq!(m::turnover_pct(100.0, 0.0), 0.0);
    assert!(approx(m::cost_drag(50_000.0, 10_000_000.0), 0.5, 1e-12));
}

fn order(side: &str, r: f64, fill: Option<f64>, qty: f64) -> Order {
    Order {
        side: side.into(),
        planned_ref_price: Some(r),
        avg_fill_price: fill,
        filled_qty: Some(qty),
        ..Order::default()
    }
}

#[test]
fn slippage_signs_so_positive_always_means_worse() {
    let s = m::slippage(&[
        order("BUY", 100.0, Some(101.0), 10.0),
        order("SELL", 100.0, Some(99.0), 10.0),
    ]);
    assert_eq!(s.orders, 2);
    assert!(approx(s.mean_bps, 100.0, 1e-9));
    assert!(approx(s.total_cost, 20.0, 1e-9));
}

#[test]
fn favourable_fills_are_negative_slippage() {
    assert!(approx(
        m::slippage(&[order("SELL", 100.0, Some(101.0), 5.0)]).mean_bps,
        -100.0,
        1e-9
    ));
}

#[test]
fn unfilled_orders_are_ignored_by_slippage() {
    assert_eq!(m::slippage(&[order("BUY", 100.0, None, 0.0)]).orders, 0);
}

fn taxed(sym: &str, pnl: f64, costs: f64, entry: &str, exit: &str) -> Trade {
    Trade {
        symbol: sym.into(),
        pnl,
        costs: Some(costs),
        entry_ts: Some(ts(entry)),
        exit_ts: Some(ts(exit)),
        ..Trade::default()
    }
}

#[test]
fn tax_drag_splits_stcg_and_ltcg_with_the_exemption() {
    let out = m::tax_drag(
        &[
            taxed("A", 100_000.0, 500.0, "2026-01-01", "2026-06-01"),
            taxed("B", 300_000.0, 1_000.0, "2025-01-01", "2026-06-01"),
        ],
        None,
        false,
    );
    assert!(approx(out.stcg_tax, 100_500.0 * 0.20, 1e-9));
    assert!(approx(out.ltcg_tax, (301_000.0 - 125_000.0) * 0.125, 1e-9));
    assert!(approx(out.total_tax, 20_100.0 + 22_000.0, 1e-9));
}

#[test]
fn ltcg_exemption_can_absorb_the_whole_gain() {
    let out = m::tax_drag(
        &[taxed("A", 100_000.0, 0.0, "2024-01-01", "2026-06-01")],
        None,
        false,
    );
    assert!(approx(out.ltcg_tax, 0.0, 1e-12));
}

#[test]
fn losses_never_produce_negative_tax() {
    let out = m::tax_drag(
        &[taxed("A", -500_000.0, 0.0, "2026-01-01", "2026-06-01")],
        None,
        false,
    );
    assert!(approx(out.total_tax, 0.0, 1e-12));
}

#[test]
fn exemption_applies_per_financial_year() {
    let out = m::tax_drag(
        &[
            taxed("A", 200_000.0, 0.0, "2024-01-01", "2026-03-31"),
            taxed("B", 200_000.0, 0.0, "2024-01-01", "2026-04-01"),
        ],
        None,
        false,
    );
    assert_eq!(
        out.by_fy.iter().map(|r| r.fy.as_str()).collect::<Vec<_>>(),
        ["2025-26", "2026-27"]
    );
    assert!(approx(
        out.total_tax,
        2.0 * (200_000.0 - 125_000.0) * 0.125,
        1e-9
    ));
}

#[test]
fn costs_deductible_flag_lowers_the_bill() {
    let t = [taxed("A", 100_000.0, 10_000.0, "2026-01-01", "2026-06-01")];
    assert!(m::tax_drag(&t, None, true).total_tax < m::tax_drag(&t, None, false).total_tax);
}

#[test]
fn tax_drag_pct_uses_capital() {
    let t = [taxed("A", 100_000.0, 0.0, "2026-01-01", "2026-06-01")];
    assert!(approx(
        m::tax_drag(&t, Some(10_000_000.0), false).drag_pct,
        0.2,
        1e-12
    ));
    assert_eq!(m::tax_drag(&t, Some(0.0), false).drag_pct, 0.0);
}

#[test]
fn per_symbol_contribution() {
    let out = m::per_symbol_contribution(&trades());
    assert_eq!(out[0].key, "DIXON");
    assert!(approx(out[0].pnl, 50_000.0, 1e-9));
    let total: f64 = out.iter().map(|c| c.share_pct.abs()).sum();
    assert!(approx(total, 100.0, 1e-9));
}

#[test]
fn per_sector_contribution_uses_supplied_mapping() {
    let sectors: HashMap<String, String> =
        [("DIXON", "EMS"), ("KAYNES", "EMS"), ("POLYCAB", "cables")]
            .iter()
            .map(|(a, b)| ((*a).to_owned(), (*b).to_owned()))
            .collect();
    let out = m::per_sector_contribution(&trades(), &sectors);
    let get = |k: &str| out.iter().find(|c| c.key == k).unwrap();
    assert!(approx(get("EMS").pnl, 30_000.0, 1e-9));
    assert!(approx(get("other").pnl, -10_000.0, 1e-9));
}

#[test]
fn sectors_load_from_the_csv_and_an_absent_file_is_empty() {
    let dir = tempfile::tempdir().unwrap();
    let p = dir.path().join("sectors.csv");
    std::fs::write(&p, "symbol,cluster\nDIXON,EMS\nPOLYCAB,cables\n").unwrap();
    let s = m::load_sectors(&p);
    assert_eq!(s.get("DIXON").map(String::as_str), Some("EMS"));
    assert!(m::load_sectors(&dir.path().join("nope.csv")).is_empty());
}

// =============================================================================================
// momentum diagnostics
// =============================================================================================

#[test]
fn perf_by_score_bucket() {
    let out = m::perf_by_score_bucket(&trades(), None);
    let get = |k: &str| out.iter().find(|g| g.key == k).unwrap();
    assert_eq!(
        out.iter().map(|g| g.key.as_str()).collect::<Vec<_>>(),
        ["0-50", "50-60", "60-70", "70-80", "80-101"]
    );
    assert_eq!(get("80-101").trades, 2);
    assert_eq!(get("60-70").trades, 1);
    assert_eq!(get("50-60").trades, 1);
    assert!(get("0-50").avg_pnl.is_nan());
    assert_eq!(get("0-50").total_pnl, 0.0);
}

#[test]
fn perf_by_rank_bucket() {
    let out = m::perf_by_rank_bucket(&trades(), &m::DEFAULT_RANK_BUCKETS);
    let get = |k: &str| out.iter().find(|g| g.key == k).unwrap();
    assert_eq!(get("1-5").trades, 2);
    assert_eq!(get("15-25").trades, 2);
    assert!(approx(get("1-5").total_pnl, 30_000.0, 1e-9));
}

fn scored(sym: &str, score: f64, pnl: f64, qty: f64, px: f64) -> Trade {
    Trade {
        symbol: sym.into(),
        entry_score: Some(score),
        pnl,
        qty: Some(qty),
        entry_price: Some(px),
        exit_ts: Some(ts("2026-01-01")),
        ..Trade::default()
    }
}

#[test]
fn score_decay_correlation_is_positive_when_score_predicts() {
    let t: Vec<Trade> = [
        (50.0, -1_000.0),
        (60.0, 0.0),
        (70.0, 1_000.0),
        (80.0, 2_000.0),
        (90.0, 3_000.0),
    ]
    .iter()
    .enumerate()
    .map(|(i, (s, p))| scored(&i.to_string(), *s, *p, 10.0, 100.0))
    .collect();
    let out = m::score_decay_vs_return(&t, None);
    assert_eq!(out.n, 5);
    assert!(approx(out.pearson, 1.0, 1e-9));
    assert!(approx(out.spearman, 1.0, 1e-9));
}

#[test]
fn score_decay_handles_too_few_rows() {
    assert!(
        m::score_decay_vs_return(&[scored("A", 70.0, 1.0, 1.0, 1.0)], None)
            .pearson
            .is_nan()
    );
}

#[test]
fn exit_efficiency_rewards_selling_before_a_fall() {
    let out = m::exit_efficiency(&trades());
    assert_eq!(out.n, 4);
    assert!(approx(
        out.efficiency,
        -m::nanmean(&[-0.08, -0.15, 0.05, 0.10]),
        1e-12
    ));
    assert!(approx(out.pct_good_exits, 50.0, 1e-12));
}

#[test]
fn exit_efficiency_without_forward_data() {
    assert_eq!(m::exit_efficiency(&[closed("A", 1.0, Some(1.0))]).n, 0);
}

#[test]
fn stop_loss_effectiveness_isolates_stop_outs() {
    let out = m::stop_loss_effectiveness(&trades(), "stop");
    assert_eq!(out.n, 2);
    assert!(approx(out.saved_by_stop_pct, 50.0, 1e-12));
    assert!(approx(out.shaken_out_pct, 50.0, 1e-12));
}

#[test]
fn cash_drag_reports_average_weight() {
    let nav = series(&linspace(1_000_000.0, 1_200_000.0, 100), "2026-01-01");
    let cash = series(&[200_000.0; 100], "2026-01-01");
    let out = m::cash_drag(&nav, &cash, None, None);
    assert!(out.avg_cash_weight_pct > 15.0 && out.avg_cash_weight_pct < 21.0);
    assert!(out.estimated_drag_pct > 0.0);
}

#[test]
fn cash_drag_is_zero_without_cash() {
    let nav = series(&linspace(1_000_000.0, 1_100_000.0, 50), "2026-01-01");
    let out = m::cash_drag(&nav, &series(&[0.0; 50], "2026-01-01"), None, None);
    assert!(approx(out.avg_cash_weight_pct, 0.0, 1e-12));
    assert!(approx(out.estimated_drag_pct, 0.0, 1e-12));
}

#[test]
fn weekly_turnover_trend() {
    let o = |t: &str, q: f64, px: f64| Order {
        created_ts: Some(ts(t)),
        filled_qty: Some(q),
        avg_fill_price: Some(px),
        ..Order::default()
    };
    let out = m::weekly_turnover_trend(
        &[
            o("2026-01-05", 100.0, 1_000.0),
            o("2026-01-06", 50.0, 2_000.0),
            o("2026-01-13", 10.0, 1_000.0),
        ],
        &Capital::Scalar(10_000_000.0),
    );
    assert!(out.len() >= 2);
    let total: f64 = out.iter().map(|w| w.traded_value).sum();
    assert!(approx(
        total,
        100.0 * 1000.0 + 50.0 * 2000.0 + 10.0 * 1000.0,
        1e-9
    ));
    assert!(approx(out[0].turnover_pct, 2.0, 1e-12));
    assert_eq!(out[0].week, d("2026-01-11"));
}

#[test]
fn weekly_turnover_trend_handles_empty() {
    assert!(m::weekly_turnover_trend(&[], &Capital::Scalar(1_000_000.0)).is_empty());
}

// =============================================================================================
// benchmarking
// =============================================================================================

fn month_ends(values: &[f64]) -> Series {
    ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"]
        .iter()
        .zip(values)
        .map(|(t, v)| (d(t), *v))
        .collect()
}

#[test]
fn up_and_down_capture_of_identical_series() {
    let b = month_ends(&[0.05, -0.03, 0.02, -0.01]);
    assert!(approx(m::up_capture(&b, &b), 1.0, 1e-12));
    assert!(approx(m::down_capture(&b, &b), 1.0, 1e-12));
}

#[test]
fn defensive_portfolio_has_low_down_capture() {
    let b = month_ends(&[0.10, -0.10, 0.10, -0.10]);
    let p = month_ends(&[0.10, -0.05, 0.10, -0.05]);
    assert!(m::down_capture(&p, &b) < 1.0);
    assert!(approx(m::up_capture(&p, &b), 1.0, 1e-12));
}

#[test]
fn capture_is_nan_without_qualifying_months() {
    let b = month_ends(&[0.05, 0.02]);
    assert!(m::down_capture(&b, &b).is_nan());
}

#[test]
fn equity_curve_rebases_everything_to_100_and_labels_the_series() {
    let port = series(&linspace(100.0, 130.0, 30), "2026-01-01");
    let n500 = series(&linspace(20_000.0, 21_000.0, 30), "2026-01-01");
    let mom30 = series(&linspace(30_000.0, 33_000.0, 30), "2026-01-01");
    let types: HashMap<String, String> = [("NIFTY 500", "TRI"), ("NIFTY200 MOMENTM 30", "TRI")]
        .iter()
        .map(|(a, b)| ((*a).to_owned(), (*b).to_owned()))
        .collect();
    let out = m::equity_curve_vs_benchmark(
        &port,
        &[
            ("NIFTY 500".into(), n500),
            ("NIFTY200 MOMENTM 30".into(), mom30),
        ],
        100.0,
        &types,
    );
    let c = &out.curve;
    for col in &c.columns {
        assert!(approx(col.1[0], 100.0, 1e-12));
    }
    assert!(approx(
        *c.col("portfolio").unwrap().last().unwrap(),
        130.0,
        1e-9
    ));
    assert!(approx(
        *c.col("NIFTY 500").unwrap().last().unwrap(),
        105.0,
        1e-9
    ));
    let used: Vec<(&str, &str)> = out
        .series_used
        .iter()
        .map(|(a, b)| (a.as_str(), b.as_str()))
        .collect();
    assert_eq!(
        used,
        [
            ("portfolio", "NAV"),
            ("NIFTY 500", "TRI"),
            ("NIFTY200 MOMENTM 30", "TRI")
        ]
    );
    assert_eq!(out.inception.as_deref(), Some("2026-01-01"));
}

#[test]
fn equity_curve_marks_unlabelled_benchmarks_unknown() {
    let out = m::equity_curve_vs_benchmark(
        &series(&linspace(100.0, 110.0, 10), "2026-01-01"),
        &[("X".into(), series(&linspace(1.0, 2.0, 10), "2026-01-01"))],
        100.0,
        &HashMap::new(),
    );
    assert_eq!(out.series_used[1], ("X".to_owned(), "UNKNOWN".to_owned()));
}

#[test]
fn equity_curve_clips_benchmarks_to_the_portfolio_inception() {
    let port = series(&[100.0, 110.0], "2026-06-01");
    let bench = series(&linspace(1_000.0, 1_200.0, 120), "2026-04-01");
    let out = m::equity_curve_vs_benchmark(&port, &[("B".into(), bench)], 100.0, &HashMap::new());
    assert_eq!(out.curve.index[0], d("2026-06-01"));
    assert!(approx(out.curve.col("B").unwrap()[0], 100.0, 1e-12));
    assert!(out.dropped.is_empty());
}

#[test]
fn non_overlapping_benchmark_is_reported_not_silently_dropped() {
    let port = series(&[100.0, 110.0], "2026-06-01");
    let stale = series(&linspace(1_000.0, 1_100.0, 30), "2026-01-01");
    let out =
        m::equity_curve_vs_benchmark(&port, &[("STALE".into(), stale)], 100.0, &HashMap::new());
    assert_eq!(out.dropped, ["STALE"]);
    assert!(out.curve.col("STALE").is_none());
}

// =============================================================================================
// summary bundle
// =============================================================================================

#[test]
fn summary_bundles_headline_metrics() {
    use baskfy_desk::wire::PyValue;
    let nav = series(&linspace(1_000_000.0, 1_300_000.0, 300), "2026-01-01");
    let out = m::summary(&nav, None, None, 0.0, "UNKNOWN");
    assert_eq!(
        out.get("inception"),
        Some(&PyValue::Str("2026-01-01".into()))
    );
    let PyValue::Float(iv) = out.get("index_value").unwrap() else {
        panic!()
    };
    assert!(approx(*iv, 130.0, 1e-9));
    let PyValue::Float(c) = out.get("cagr").unwrap() else {
        panic!()
    };
    assert!(*c > 0.0);
    let PyValue::Float(mdd) = out.get("max_drawdown").unwrap() else {
        panic!()
    };
    assert!(approx(*mdd, 0.0, 1e-12));
    assert_eq!(out.get("benchmark_series"), Some(&PyValue::None));
}

#[test]
fn summary_adds_relative_metrics_and_records_the_series_used() {
    use baskfy_desk::wire::PyValue;
    let nav = series(&linspace(1_000_000.0, 1_300_000.0, 300), "2026-01-01");
    let bench = series(&linspace(20_000.0, 24_000.0, 300), "2026-01-01");
    let out = m::summary(&nav, None, Some(&bench), 0.0, "TRI");
    assert_eq!(
        out.get("benchmark_series"),
        Some(&PyValue::Str("TRI".into()))
    );
    for k in [
        "beta",
        "alpha_jensen",
        "tracking_error",
        "information_ratio",
        "up_capture",
        "down_capture",
    ] {
        assert!(out.get(k).is_some(), "{k}");
    }
}

#[test]
fn numpy_reductions_match_python() {
    let v: Vec<f64> = (0..300)
        .map(|i| (f64::from(i) * 0.37 - 11.0) / 7.0)
        .collect();
    assert_eq!(m::np_sum(&v), 1_899.214_285_714_285_6);
    assert_eq!(m::nanmean(&v), 6.330_714_285_714_285_5);
    assert_eq!(m::nanstd(&v, 1), 4.585_185_772_837_189);
    assert_eq!(m::quantile(&[1.0, 2.0, 3.0, 4.0], 0.05), 1.15);
    assert_eq!(m::rank(&[3.0, 1.0, 3.0, f64::NAN])[..3], [2.5, 1.0, 2.5]);
}

// =============================================================================================
// test_drawdown.py
// =============================================================================================

fn curve(values: &[f64]) -> Series {
    series(values, "2026-01-01")
}

#[test]
fn known_curve_depth_and_duration() {
    let dd = m::max_drawdown(&curve(&[
        100.0, 110.0, 120.0, 110.0, 100.0, 90.0, 100.0, 110.0, 120.0, 130.0,
    ]));
    assert!(approx(dd.depth, -0.25, 1e-12));
    assert!(approx(dd.depth_pct, -25.0, 1e-12));
    assert_eq!(dd.peak_date, Some(d("2026-01-03")));
    assert_eq!(dd.trough_date, Some(d("2026-01-06")));
    assert_eq!(dd.recovery_date, Some(d("2026-01-09")));
    assert_eq!(dd.duration_days, 6);
    assert!(dd.recovered);
    assert!(approx(dd.current_dd, 0.0, 1e-12));
}

#[test]
fn unrecovered_drawdown_runs_to_the_last_observation() {
    let dd = m::max_drawdown(&curve(&[100.0, 150.0, 120.0, 110.0, 105.0]));
    assert!(approx(dd.depth, 105.0 / 150.0 - 1.0, 1e-12));
    assert_eq!(dd.peak_date, Some(d("2026-01-02")));
    assert_eq!(dd.trough_date, Some(d("2026-01-05")));
    assert_eq!(dd.recovery_date, None);
    assert!(!dd.recovered);
    assert_eq!(dd.duration_days, 3);
    assert!(approx(dd.current_dd, dd.depth, 1e-12));
}

#[test]
fn deepest_of_several_drawdowns_wins() {
    let dd = m::max_drawdown(&curve(&[100.0, 110.0, 90.0, 105.0, 130.0, 90.0, 100.0]));
    assert!(approx(dd.depth, 90.0 / 130.0 - 1.0, 1e-12));
    assert_eq!(dd.peak_date, Some(d("2026-01-05")));
    assert_eq!(dd.trough_date, Some(d("2026-01-06")));
}

#[test]
fn monotonic_rise_has_no_drawdown() {
    let dd = m::max_drawdown(&curve(&[100.0, 101.0, 102.0, 103.0]));
    assert!(approx(dd.depth, 0.0, 1e-12));
    assert!(approx(dd.current_dd, 0.0, 1e-12));
    assert!(dd.recovered);
}

#[test]
fn flat_curve_has_no_drawdown() {
    assert!(approx(
        m::max_drawdown(&curve(&[100.0, 100.0, 100.0])).depth,
        0.0,
        1e-12
    ));
}

#[test]
fn peak_is_the_first_date_reaching_that_high() {
    let dd = m::max_drawdown(&curve(&[100.0, 120.0, 120.0, 120.0, 60.0, 120.0]));
    assert_eq!(dd.peak_date, Some(d("2026-01-02")));
    assert!(approx(dd.depth, -0.5, 1e-12));
    assert_eq!(dd.recovery_date, Some(d("2026-01-06")));
    assert_eq!(dd.duration_days, 4);
}

#[test]
fn single_point_and_empty_curves() {
    assert!(approx(m::max_drawdown(&curve(&[100.0])).depth, 0.0, 1e-12));
    let empty = m::max_drawdown(&[]);
    assert_eq!(empty.depth, 0.0);
    assert_eq!(empty.peak_date, None);
}

#[test]
fn recovery_requires_reaching_the_peak_not_merely_approaching_it() {
    let dd = m::max_drawdown(&curve(&[100.0, 200.0, 100.0, 199.99]));
    assert_eq!(dd.recovery_date, None);
    assert!(!dd.recovered);
}

#[test]
fn drawdown_series_matches_max_drawdown() {
    let s = curve(&[100.0, 110.0, 120.0, 90.0, 120.0]);
    let ds = vals(&m::drawdown_series(&s));
    let min = ds.iter().copied().fold(f64::INFINITY, f64::min);
    assert!(approx(min, m::max_drawdown(&s).depth, 1e-12));
    assert!(approx(ds[0], 0.0, 1e-12));
    assert!(ds.iter().all(|x| *x <= 1e-12));
}

#[test]
fn drawdown_series_length_matches_input() {
    assert_eq!(
        m::drawdown_series(&curve(&[100.0, 90.0, 95.0, 80.0])).len(),
        4
    );
}

#[test]
fn calmar_is_cagr_over_abs_max_drawdown() {
    let mut v = vec![100.0; 181];
    v.push(75.0);
    v.extend(vec![150.0; 184]);
    let s = curve(&v);
    assert!(approx(
        m::calmar(&s),
        m::cagr(&s) / m::max_drawdown(&s).depth.abs(),
        1e-12
    ));
}

#[test]
fn calmar_without_a_drawdown_is_undefined_not_zero() {
    assert!(m::calmar(&curve(&[100.0, 110.0, 120.0])).is_nan());
    assert_eq!(m::calmar(&curve(&[100.0, 100.0, 100.0])), 0.0);
}

#[test]
fn var_is_reported_as_a_positive_loss() {
    let r = curve(&[-0.03, -0.01, 0.00, 0.01, 0.02, 0.015, -0.025, 0.005]);
    assert!(m::var_parametric(&r, None) > 0.0);
    assert!(m::var_historical(&r, 0.95) > 0.0);
}

#[test]
fn historical_var_is_the_5th_percentile_loss() {
    let v: Vec<f64> = [-0.10, -0.05, 0.0, 0.05, 0.10].repeat(4);
    assert!(approx(
        m::var_historical(&curve(&v), 0.95),
        -m::quantile(&v, 0.05),
        1e-15
    ));
    assert!(approx(m::var_historical(&curve(&v), 0.95), 0.1, 1e-12));
}

// =============================================================================================
// test_xirr.py
// =============================================================================================

fn x(amounts: &[f64], dates: &[&str]) -> f64 {
    m::xirr(amounts, &dates.iter().map(|t| d(t)).collect::<Vec<_>>())
}

fn npv(amounts: &[f64], dates: &[&str], rate: f64) -> f64 {
    amounts
        .iter()
        .zip(dates)
        .map(|(a, t)| a / (1.0 + rate).powf((d(t) - d(dates[0])).num_days() as f64 / 365.0))
        .sum()
}

#[test]
fn two_cashflow_example_to_four_decimals() {
    let r = x(&[-100_000.0, 115_000.0], &["2026-01-01", "2027-01-01"]);
    assert!((r * 10_000.0).round() / 10_000.0 == 0.15);
}

#[test]
fn two_cashflow_example_is_exact_beyond_four_decimals() {
    assert!((x(&[-100_000.0, 115_000.0], &["2026-01-01", "2027-01-01"]) - 0.15).abs() <= 1e-9);
}

#[test]
fn four_cashflow_sip() {
    let a = [-25_000.0, -25_000.0, -25_000.0, 79_000.0];
    let t = ["2026-01-01", "2026-02-01", "2026-03-01", "2026-06-01"];
    let r = x(&a, &t);
    assert!(r.is_finite());
    assert!(npv(&a, &t, r).abs() <= 1e-6);
    assert!(r > 0.0 && r < 2.0);
}

#[test]
fn sip_beats_a_worse_terminal_value() {
    let t = ["2026-01-01", "2026-02-01", "2026-03-01", "2026-06-01"];
    assert!(
        x(&[-25_000.0, -25_000.0, -25_000.0, 82_000.0], &t)
            > x(&[-25_000.0, -25_000.0, -25_000.0, 76_000.0], &t)
    );
}

#[test]
fn loss_gives_negative_rate() {
    assert!((x(&[-100_000.0, 90_000.0], &["2026-01-01", "2027-01-01"]) + 0.10).abs() <= 1e-9);
}

#[test]
fn order_of_flows_does_not_matter() {
    let a = x(&[-100_000.0, 115_000.0], &["2026-01-01", "2027-01-01"]);
    let b = x(&[115_000.0, -100_000.0], &["2027-01-01", "2026-01-01"]);
    assert!((a - b).abs() <= 1e-12);
}

#[test]
fn all_negative_flows_have_no_solution() {
    assert!(x(&[-100.0, -100.0], &["2026-01-01", "2027-01-01"]).is_nan());
}

#[test]
fn all_positive_flows_have_no_solution() {
    assert!(x(&[100.0, 100.0], &["2026-01-01", "2027-01-01"]).is_nan());
}

#[test]
fn single_flow_is_nan() {
    assert!(x(&[-100.0], &["2026-01-01"]).is_nan());
}

#[test]
fn half_year_doubling_annualises() {
    let r = x(&[-100_000.0, 200_000.0], &["2026-01-01", "2026-07-02"]);
    let want = 2f64.powf(365.0 / 182.0) - 1.0;
    assert!((r - want).abs() <= 1e-6 * want);
}

#[test]
fn deep_loss_still_solves_via_bisection() {
    assert!((x(&[-100_000.0, 1_000.0], &["2026-01-01", "2027-01-01"]) + 0.99).abs() <= 1e-6);
}

#[test]
fn extreme_gain_solves() {
    let r = x(&[-1_000.0, 50_000.0], &["2026-01-01", "2027-01-01"]);
    assert!((r - 49.0).abs() <= 1e-6 * 49.0);
}

#[test]
fn bad_guess_still_converges() {
    let t = [d("2026-01-01"), d("2027-01-01")];
    for guess in [-0.9, 0.0, 5.0, 100.0] {
        let r = m::xirr_with(&[-100_000.0, 115_000.0], &t, guess, 1e-10, 100);
        assert!((r - 0.15).abs() <= 1e-6, "failed from guess={guess}");
    }
}

#[test]
fn many_irregular_flows_converge() {
    let a = [
        -50_000.0, -20_000.0, 10_000.0, -30_000.0, -15_000.0, 5_000.0, 125_000.0,
    ];
    let t = [
        "2026-01-05",
        "2026-02-11",
        "2026-03-03",
        "2026-05-19",
        "2026-08-01",
        "2026-09-15",
        "2027-01-20",
    ];
    let r = x(&a, &t);
    assert!(r.is_finite());
    assert!(npv(&a, &t, r).abs() <= 1e-5);
}

// =============================================================================================
// test_twr_chaining.py
// =============================================================================================

fn navs(pairs: &[(&str, f64)]) -> Series {
    pairs.iter().map(|(t, v)| (d(t), *v)).collect()
}

fn rel(a: f64, b: f64, r: f64) -> bool {
    (a - b).abs() <= r * b.abs()
}

#[test]
fn twr_equals_simple_return_with_no_cashflows() {
    let s = navs(&[
        ("2026-01-01", 1_000_000.0),
        ("2026-01-02", 1_050_000.0),
        ("2026-01-03", 1_020_000.0),
        ("2026-01-04", 1_180_000.0),
    ]);
    let simple = s[3].1 / s[0].1 - 1.0;
    assert!(rel(m::twr_total(&s, None), simple, 1e-12));
    assert!(rel(
        m::twr_chained(&s, None, 100.0)[3].1,
        100.0 * (1.0 + simple),
        1e-12
    ));
}

#[test]
fn twr_equals_simple_return_when_cashflows_are_empty_series() {
    let s = navs(&[("2026-01-01", 500_000.0), ("2026-01-02", 525_000.0)]);
    assert!(rel(m::twr_total(&s, Some(&[])), 0.05, 1e-12));
}

#[test]
fn index_starts_at_exactly_100() {
    let s = navs(&[("2026-01-01", 987_654.32), ("2026-01-02", 1_000_000.0)]);
    assert_eq!(m::twr_chained(&s, None, 100.0)[0].1, 100.0);
}

#[test]
fn chained_index_tracks_nav_ratio_without_flows() {
    let s = navs(&[
        ("2026-01-01", 1_000_000.0),
        ("2026-01-02", 1_100_000.0),
        ("2026-01-03", 990_000.0),
    ]);
    let idx = m::twr_chained(&s, None, 100.0);
    for (i, (_, nav)) in s.iter().enumerate() {
        assert!(rel(idx[i].1, 100.0 * nav / s[0].1, 1e-12));
    }
}

#[test]
fn pure_contribution_produces_zero_return() {
    let s = navs(&[("2026-02-01", 1_000_000.0), ("2026-02-02", 1_500_000.0)]);
    let cf = navs(&[("2026-02-02", -500_000.0)]);
    assert!(m::twr_total(&s, Some(&cf)).abs() <= 1e-12);
}

#[test]
fn pure_withdrawal_produces_zero_return() {
    let s = navs(&[("2026-03-01", 1_000_000.0), ("2026-03-02", 800_000.0)]);
    let cf = navs(&[("2026-03-02", 200_000.0)]);
    assert!(m::twr_total(&s, Some(&cf)).abs() <= 1e-12);
}

#[test]
fn return_measured_on_pre_flow_base() {
    let s = navs(&[("2026-02-01", 1_000_000.0), ("2026-02-02", 1_550_000.0)]);
    let cf = navs(&[("2026-02-02", -500_000.0)]);
    assert!(rel(m::twr_total(&s, Some(&cf)), 0.05, 1e-12));
}

#[test]
fn twr_across_a_rebalance_with_a_mid_period_contribution() {
    let s = navs(&[
        ("2026-04-01", 1_000_000.0),
        ("2026-04-02", 1_100_000.0),
        ("2026-04-03", 1_599_500.0),
        ("2026-04-04", 1_679_475.0),
    ]);
    let cf = navs(&[("2026-04-03", -450_000.0)]);
    assert!(rel(
        m::twr_total(&s, Some(&cf)),
        1.10 * 1.045 * 1.05 - 1.0,
        1e-9
    ));
}

#[test]
fn multiple_flows_on_the_same_day_are_netted() {
    let s = navs(&[("2026-05-01", 1_000_000.0), ("2026-05-02", 1_200_000.0)]);
    let cf = navs(&[("2026-05-02", -300_000.0), ("2026-05-02", 100_000.0)]);
    assert!(m::twr_total(&s, Some(&cf)).abs() <= 1e-12);
}

#[test]
fn flow_on_a_day_with_no_prior_capital_does_not_blow_up() {
    let s = navs(&[("2026-06-01", 0.0), ("2026-06-02", 1_000_000.0)]);
    let cf = navs(&[("2026-06-02", -1_000_000.0)]);
    assert!(m::twr_total(&s, Some(&cf)).abs() <= 1e-12);
    assert_eq!(m::twr_chained(&s, Some(&cf), 100.0)[0].1, 100.0);
}

#[test]
fn daily_returns_drop_the_first_observation() {
    let r = m::daily_returns(
        &navs(&[
            ("2026-01-01", 100.0),
            ("2026-01-02", 110.0),
            ("2026-01-03", 99.0),
        ]),
        None,
    );
    assert_eq!(r.len(), 2);
    assert!(approx(r[0].1, 0.10, 1e-12));
    assert!(approx(r[1].1, -0.10, 1e-12));
}

#[test]
fn empty_series_is_handled() {
    assert!(m::twr_chained(&[], None, 100.0).is_empty());
    assert_eq!(m::twr_total(&[], None), 0.0);
}

#[tokio::test]
async fn db_rechain_matches_metrics_twr() {
    use baskfy_desk::lanes::weekly::weekly_books::snapshot_db as db;
    use baskfy_desk::storage::{Db, DbValue};
    let dir = tempfile::tempdir().unwrap();
    let mut conn = Db::connect_sqlite_path(&dir.path().join("p.db"))
        .await
        .unwrap();
    conn.migrate().await.unwrap();
    let series = [
        ("2026-01-01", 1_000_000.0),
        ("2026-01-02", 1_050_000.0),
        ("2026-01-03", 1_600_000.0),
        ("2026-01-04", 1_680_000.0),
    ];
    db::record_cashflow(&mut conn, "2026-01-03", -500_000.0, "invest")
        .await
        .unwrap();
    for (t, nav) in series {
        let row = db::SnapRow {
            date: t.to_owned(),
            nav,
            invested: nav,
            cash: 0.0,
            holdings_json: "{}".to_owned(),
        };
        db::save_snapshot(&mut conn, &row, false).await.unwrap();
    }
    let stored = db::snapshot_series(&mut conn).await.unwrap();
    let computed = m::twr_chained(
        &navs(&series),
        Some(&navs(&[("2026-01-03", -500_000.0)])),
        100.0,
    );
    for (i, r) in stored.iter().enumerate() {
        let Some(DbValue::Float(v)) = r.get("index_value") else {
            panic!("{r:?}")
        };
        assert!(rel(*v, computed[i].1, 1e-12));
    }
}
