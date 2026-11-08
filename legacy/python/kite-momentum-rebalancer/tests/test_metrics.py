"""Coverage for the rest of the metrics engine: risk, trades, diagnostics, benchmarking."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.analytics import metrics as M


def days(n, start="2026-01-01"):
    return pd.date_range(start, periods=n, freq="D")


def ts(date: str) -> float:
    return pd.Timestamp(date).timestamp()


# =====================================================================================
# returns
# =====================================================================================
def test_absolute_and_cagr():
    s = pd.Series([100.0, 121.0], index=[pd.Timestamp("2026-01-01"),
                                         pd.Timestamp("2028-01-01")])
    assert M.absolute(s) == pytest.approx(0.21)
    # 2 years (731 days incl. a leap year) -> ~10%/yr
    assert M.cagr(s) == pytest.approx(0.0998, abs=1e-3)


def test_cagr_of_flat_series_is_zero():
    s = pd.Series([100.0, 100.0], index=[pd.Timestamp("2026-01-01"),
                                         pd.Timestamp("2027-01-01")])
    assert M.cagr(s) == pytest.approx(0.0)


def test_rolling_returns_windows():
    s = pd.Series(np.linspace(100, 200, 300), index=days(300))
    rr = M.rolling_returns(s, {"5d": 5, "20d": 20})
    assert list(rr.columns) == ["5d", "20d"]
    assert rr["5d"].isna().sum() == 5
    assert rr["5d"].iloc[-1] == pytest.approx(s.iloc[-1] / s.iloc[-6] - 1.0)


def test_monthly_returns_measure_the_first_partial_month():
    idx = pd.date_range("2026-01-15", "2026-03-31", freq="D")
    s = pd.Series(np.linspace(100, 130, len(idx)), index=idx)
    m = M.monthly_returns(s)
    assert len(m) == 3                       # Jan (partial), Feb, Mar
    assert (1.0 + m).prod() == pytest.approx(s.iloc[-1] / s.iloc[0], rel=1e-9)


def test_monthly_return_matrix_shape():
    idx = pd.date_range("2026-01-01", "2027-06-30", freq="D")
    s = pd.Series(np.linspace(100, 150, len(idx)), index=idx)
    mat = M.monthly_return_matrix(s)
    assert list(mat.columns) == list(range(1, 13))
    assert set(mat.index) == {2026, 2027}
    # Was `assert mat.loc[2027, 12] != mat.loc[2027, 12] or True` until 12 Sep 2026: the `x != x`
    # NaN idiom with `or True` appended, which made it always pass. The line below already says
    # the same thing correctly, so the disabled one was removed rather than repaired.
    assert pd.isna(mat.loc[2027, 12])


# =====================================================================================
# risk
# =====================================================================================
def test_ann_volatility_scales_by_sqrt_252():
    r = pd.Series([0.01, -0.01] * 50, index=days(100))
    assert M.ann_volatility(r) == pytest.approx(r.std(ddof=1) * np.sqrt(252))


def test_downside_deviation_ignores_upside():
    up = pd.Series([0.01] * 50, index=days(50))
    assert M.downside_deviation(up) == pytest.approx(0.0)
    mixed = pd.Series([0.02, -0.02] * 25, index=days(50))
    assert M.downside_deviation(mixed) > 0


def test_downside_deviation_is_below_total_volatility():
    r = pd.Series([0.03, -0.01, 0.02, -0.005, 0.01] * 10, index=days(50))
    assert M.downside_deviation(r) < M.ann_volatility(r)


def test_sharpe_of_a_riskless_gain_is_undefined_not_zero():
    """Constant positive returns => zero volatility. The ratio is undefined, and
    reporting 0.0 would understate it as 'no risk-adjusted return'."""
    r = pd.Series([0.001] * 30, index=days(30))
    assert np.isnan(M.sharpe(r))


def test_sharpe_of_a_flat_zero_series_is_zero():
    r = pd.Series([0.0] * 30, index=days(30))
    assert M.sharpe(r) == 0.0


def test_sharpe_falls_when_risk_free_rises():
    r = pd.Series(np.random.default_rng(1).normal(0.001, 0.01, 250), index=days(250))
    assert M.sharpe(r, rf=0.0) > M.sharpe(r, rf=0.06)


def test_sortino_exceeds_sharpe_for_upward_skew():
    r = pd.Series([0.05] * 20 + [-0.005] * 80, index=days(100))
    assert M.sortino(r) > M.sharpe(r)


def test_beta_of_a_series_against_itself_is_one():
    r = pd.Series(np.random.default_rng(2).normal(0, 0.01, 200), index=days(200))
    assert M.beta(r, r) == pytest.approx(1.0)
    assert M.alpha_jensen(r, r) == pytest.approx(0.0, abs=1e-12)
    assert M.tracking_error(r, r) == pytest.approx(0.0)


def test_beta_of_a_doubled_series_is_two():
    b = pd.Series(np.random.default_rng(3).normal(0, 0.01, 200), index=days(200))
    assert M.beta(b * 2.0, b) == pytest.approx(2.0)


def test_beta_is_zero_against_a_constant_benchmark():
    r = pd.Series(np.random.default_rng(4).normal(0, 0.01, 50), index=days(50))
    assert M.beta(r, pd.Series(0.0, index=days(50))) == 0.0


def test_information_ratio_sign_follows_excess_return():
    rng = np.random.default_rng(5)
    b = pd.Series(rng.normal(0.0005, 0.01, 250), index=days(250))
    noise = pd.Series(rng.normal(0.0, 0.002, 250), index=days(250))
    assert M.information_ratio(b + 0.0004 + noise, b) > 0
    assert M.information_ratio(b - 0.0004 + noise, b) < 0


def test_information_ratio_is_undefined_with_zero_tracking_error():
    """Beating the benchmark by a CONSTANT means no tracking error at all — the ratio
    is undefined rather than zero."""
    b = pd.Series(np.random.default_rng(9).normal(0.0005, 0.01, 100), index=days(100))
    assert np.isnan(M.information_ratio(b + 0.0004, b))
    assert M.information_ratio(b, b) == 0.0


def test_parametric_var_matches_the_formula():
    r = pd.Series(np.random.default_rng(6).normal(0.0, 0.01, 500), index=days(500))
    assert M.var_parametric(r, z=1.645) == pytest.approx(
        -(r.mean() - 1.645 * r.std(ddof=1)))


def test_alignment_handles_mismatched_dates():
    a = pd.Series([0.01, 0.02, 0.03], index=days(3))
    b = pd.Series([0.01, 0.02], index=days(2, "2026-01-02"))
    # Was `assert M.beta(a, b) != 0 or True  # must not raise` until 12 Sep 2026 — always true, so
    # it asserted neither "not zero" nor "did not raise". The real property is that alignment
    # intersects the two indices and measures on the overlap: the shared dates are 2026-01-02 and
    # 2026-01-03, where `a` moves 0.02 -> 0.03 against `b`'s 0.01 -> 0.02, so beta is exactly 1.
    beta = M.beta(a, b)
    assert math.isfinite(beta)
    assert beta == pytest.approx(1.0)
    assert M.tracking_error(a, b) >= 0


# =====================================================================================
# trades
# =====================================================================================
@pytest.fixture()
def trades() -> pd.DataFrame:
    return pd.DataFrame([
        {"symbol": "DIXON", "pnl": 50_000.0, "costs": 500.0, "entry_score": 84.0,
         "entry_rank": 1, "qty": 100, "entry_price": 600.0, "exit_reason": "rank_drop",
         "entry_ts": ts("2026-01-01"), "exit_ts": ts("2026-03-01"),
         "fwd_return_20d": -0.08},
        {"symbol": "KAYNES", "pnl": -20_000.0, "costs": 400.0, "entry_score": 82.0,
         "entry_rank": 2, "qty": 50, "entry_price": 3_000.0, "exit_reason": "stop_loss",
         "entry_ts": ts("2026-01-01"), "exit_ts": ts("2026-02-01"),
         "fwd_return_20d": -0.15},
        {"symbol": "POLYCAB", "pnl": 30_000.0, "costs": 300.0, "entry_score": 65.0,
         "entry_rank": 18, "qty": 40, "entry_price": 2_200.0, "exit_reason": "rank_drop",
         "entry_ts": ts("2026-01-01"), "exit_ts": ts("2026-04-01"),
         "fwd_return_20d": 0.05},
        {"symbol": "SUZLON", "pnl": -10_000.0, "costs": 200.0, "entry_score": 55.0,
         "entry_rank": 22, "qty": 200, "entry_price": 500.0, "exit_reason": "stop_loss",
         "entry_ts": ts("2026-01-01"), "exit_ts": ts("2026-02-15"),
         "fwd_return_20d": 0.10},
    ])


def test_win_rate_and_averages(trades):
    assert M.win_rate(trades) == pytest.approx(0.5)
    assert M.avg_win(trades) == pytest.approx(40_000.0)
    assert M.avg_loss(trades) == pytest.approx(-15_000.0)
    assert M.payoff_ratio(trades) == pytest.approx(40_000 / 15_000)


def test_profit_factor_and_expectancy(trades):
    assert M.profit_factor(trades) == pytest.approx(80_000 / 30_000)
    assert M.expectancy(trades) == pytest.approx(0.5 * 40_000 + 0.5 * -15_000)


def test_profit_factor_is_infinite_without_losses():
    df = pd.DataFrame([{"symbol": "A", "pnl": 100.0, "exit_ts": ts("2026-01-01")}])
    assert M.profit_factor(df) == float("inf")


def test_empty_trades_do_not_raise():
    empty = pd.DataFrame()
    assert M.win_rate(empty) == 0.0
    assert M.expectancy(empty) == 0.0
    assert M.per_symbol_contribution(empty).empty


def test_open_trades_are_excluded():
    df = pd.DataFrame([
        {"symbol": "A", "pnl": 100.0, "exit_ts": ts("2026-01-01")},
        {"symbol": "B", "pnl": -999.0, "exit_ts": None},        # still open
    ])
    assert M.win_rate(df) == 1.0


def test_avg_holding_period(trades):
    assert M.avg_holding_period(trades) == pytest.approx((59 + 31 + 90 + 45) / 4, abs=0.1)


def test_hit_rate_by_exit_reason(trades):
    out = M.hit_rate_by_exit_reason(trades)
    assert out.loc["rank_drop", "hit_rate"] == pytest.approx(1.0)
    assert out.loc["stop_loss", "hit_rate"] == pytest.approx(0.0)
    assert out.loc["rank_drop", "trades"] == 2


def test_turnover_and_cost_drag():
    assert M.turnover_pct(2_500_000, 10_000_000) == pytest.approx(25.0)
    assert M.turnover_pct(100, 0) == 0.0
    assert M.cost_drag(50_000, 10_000_000) == pytest.approx(0.5)


def test_slippage_signs_so_positive_always_means_worse():
    orders = pd.DataFrame([
        {"side": "BUY", "planned_ref_price": 100.0, "avg_fill_price": 101.0,
         "filled_qty": 10},     # paid more -> worse
        {"side": "SELL", "planned_ref_price": 100.0, "avg_fill_price": 99.0,
         "filled_qty": 10},     # received less -> worse
    ])
    s = M.slippage(orders)
    assert s["orders"] == 2
    assert s["mean_bps"] == pytest.approx(100.0)
    assert s["total_cost"] == pytest.approx(20.0)


def test_favourable_fills_are_negative_slippage():
    orders = pd.DataFrame([
        {"side": "SELL", "planned_ref_price": 100.0, "avg_fill_price": 101.0,
         "filled_qty": 5},
    ])
    assert M.slippage(orders)["mean_bps"] == pytest.approx(-100.0)


def test_unfilled_orders_are_ignored_by_slippage():
    orders = pd.DataFrame([
        {"side": "BUY", "planned_ref_price": 100.0, "avg_fill_price": None,
         "filled_qty": 0},
    ])
    assert M.slippage(orders)["orders"] == 0


# --- tax --------------------------------------------------------------------------------
def test_tax_drag_splits_stcg_and_ltcg_with_the_exemption():
    df = pd.DataFrame([
        # 151 days -> short term. gross = 100000 + 500 (STT added back, not deductible)
        {"symbol": "A", "pnl": 100_000.0, "costs": 500.0,
         "entry_ts": ts("2026-01-01"), "exit_ts": ts("2026-06-01")},
        # 516 days -> long term. gross = 300000 + 1000
        {"symbol": "B", "pnl": 300_000.0, "costs": 1_000.0,
         "entry_ts": ts("2025-01-01"), "exit_ts": ts("2026-06-01")},
    ])
    out = M.tax_drag(df)
    assert out["stcg_tax"] == pytest.approx(100_500 * 0.20)
    assert out["ltcg_tax"] == pytest.approx((301_000 - 125_000) * 0.125)
    assert out["total_tax"] == pytest.approx(20_100 + 22_000)


def test_ltcg_exemption_can_absorb_the_whole_gain():
    df = pd.DataFrame([{"symbol": "A", "pnl": 100_000.0, "costs": 0.0,
                        "entry_ts": ts("2024-01-01"), "exit_ts": ts("2026-06-01")}])
    assert M.tax_drag(df)["ltcg_tax"] == pytest.approx(0.0)


def test_losses_never_produce_negative_tax():
    df = pd.DataFrame([{"symbol": "A", "pnl": -500_000.0, "costs": 0.0,
                        "entry_ts": ts("2026-01-01"), "exit_ts": ts("2026-06-01")}])
    out = M.tax_drag(df)
    assert out["total_tax"] == pytest.approx(0.0)


def test_exemption_applies_per_financial_year():
    """Two long-term gains in DIFFERENT Indian FYs each get their own Rs 1.25L shield."""
    df = pd.DataFrame([
        {"symbol": "A", "pnl": 200_000.0, "costs": 0.0,
         "entry_ts": ts("2024-01-01"), "exit_ts": ts("2026-03-31")},   # FY 2025-26
        {"symbol": "B", "pnl": 200_000.0, "costs": 0.0,
         "entry_ts": ts("2024-01-01"), "exit_ts": ts("2026-04-01")},   # FY 2026-27
    ])
    out = M.tax_drag(df)
    assert set(out["by_fy"].index) == {"2025-26", "2026-27"}
    assert out["total_tax"] == pytest.approx(2 * (200_000 - 125_000) * 0.125)


def test_costs_deductible_flag_lowers_the_bill():
    df = pd.DataFrame([{"symbol": "A", "pnl": 100_000.0, "costs": 10_000.0,
                        "entry_ts": ts("2026-01-01"), "exit_ts": ts("2026-06-01")}])
    assert M.tax_drag(df, costs_deductible=True)["total_tax"] < \
           M.tax_drag(df, costs_deductible=False)["total_tax"]


def test_tax_drag_pct_uses_capital():
    df = pd.DataFrame([{"symbol": "A", "pnl": 100_000.0, "costs": 0.0,
                        "entry_ts": ts("2026-01-01"), "exit_ts": ts("2026-06-01")}])
    assert M.tax_drag(df, avg_capital=10_000_000)["drag_pct"] == pytest.approx(0.2)


# --- attribution -------------------------------------------------------------------------
def test_per_symbol_contribution(trades):
    out = M.per_symbol_contribution(trades)
    assert out.index[0] == "DIXON"
    assert out.loc["DIXON", "pnl"] == pytest.approx(50_000.0)
    assert out["share_pct"].abs().sum() == pytest.approx(100.0)


def test_per_sector_contribution_uses_supplied_mapping(trades):
    sectors = {"DIXON": "EMS", "KAYNES": "EMS", "POLYCAB": "cables"}
    out = M.per_sector_contribution(trades, sectors)
    assert out.loc["EMS", "pnl"] == pytest.approx(30_000.0)
    assert out.loc["other", "pnl"] == pytest.approx(-10_000.0)   # SUZLON unmapped


# =====================================================================================
# momentum diagnostics
# =====================================================================================
def test_perf_by_score_bucket(trades):
    out = M.perf_by_score_bucket(trades)
    assert out.loc["80-101", "trades"] == 2      # DIXON 84, KAYNES 82
    assert out.loc["60-70", "trades"] == 1       # POLYCAB 65
    assert out.loc["50-60", "trades"] == 1       # SUZLON 55


def test_perf_by_rank_bucket(trades):
    out = M.perf_by_rank_bucket(trades)
    assert out.loc["1-5", "trades"] == 2
    assert out.loc["15-25", "trades"] == 2
    assert out.loc["1-5", "total_pnl"] == pytest.approx(30_000.0)


def test_score_decay_correlation_is_positive_when_score_predicts():
    df = pd.DataFrame({
        "symbol": list("ABCDE"),
        "entry_score": [50.0, 60.0, 70.0, 80.0, 90.0],
        "pnl": [-1_000.0, 0.0, 1_000.0, 2_000.0, 3_000.0],
        "qty": [10] * 5, "entry_price": [100.0] * 5,
        "exit_ts": [ts("2026-01-01")] * 5,
    })
    out = M.score_decay_vs_return(df)
    assert out["n"] == 5
    assert out["pearson"] == pytest.approx(1.0, abs=1e-9)
    assert out["spearman"] == pytest.approx(1.0, abs=1e-9)


def test_score_decay_handles_too_few_rows():
    df = pd.DataFrame({"symbol": ["A"], "entry_score": [70.0], "pnl": [1.0],
                       "qty": [1], "entry_price": [1.0], "exit_ts": [ts("2026-01-01")]})
    assert np.isnan(M.score_decay_vs_return(df)["pearson"])


def test_exit_efficiency_rewards_selling_before_a_fall(trades):
    out = M.exit_efficiency(trades)
    assert out["n"] == 4
    assert out["efficiency"] == pytest.approx(-np.mean([-0.08, -0.15, 0.05, 0.10]))
    assert out["pct_good_exits"] == pytest.approx(50.0)


def test_exit_efficiency_without_forward_data():
    assert M.exit_efficiency(pd.DataFrame({"pnl": [1.0], "exit_ts": [1.0]}))["n"] == 0


def test_stop_loss_effectiveness_isolates_stop_outs(trades):
    out = M.stop_loss_effectiveness(trades)
    assert out["n"] == 2                                   # KAYNES + SUZLON
    assert out["saved_by_stop_pct"] == pytest.approx(50.0)  # KAYNES kept falling
    assert out["shaken_out_pct"] == pytest.approx(50.0)     # SUZLON rebounded


def test_cash_drag_reports_average_weight():
    idx = days(100)
    nav = pd.Series(np.linspace(1_000_000, 1_200_000, 100), index=idx)
    cash = pd.Series(200_000.0, index=idx)
    out = M.cash_drag(nav, cash)
    assert 15.0 < out["avg_cash_weight_pct"] < 21.0
    assert out["estimated_drag_pct"] > 0


def test_cash_drag_is_zero_without_cash():
    idx = days(50)
    nav = pd.Series(np.linspace(1_000_000, 1_100_000, 50), index=idx)
    out = M.cash_drag(nav, pd.Series(0.0, index=idx))
    assert out["avg_cash_weight_pct"] == pytest.approx(0.0)
    assert out["estimated_drag_pct"] == pytest.approx(0.0)


def test_weekly_turnover_trend():
    orders = pd.DataFrame([
        {"created_ts": ts("2026-01-05"), "filled_qty": 100, "avg_fill_price": 1_000.0},
        {"created_ts": ts("2026-01-06"), "filled_qty": 50, "avg_fill_price": 2_000.0},
        {"created_ts": ts("2026-01-13"), "filled_qty": 10, "avg_fill_price": 1_000.0},
    ])
    out = M.weekly_turnover_trend(orders, 10_000_000.0)
    assert len(out) >= 2
    assert out["traded_value"].sum() == pytest.approx(100 * 1000 + 50 * 2000 + 10 * 1000)
    assert out["turnover_pct"].iloc[0] == pytest.approx(2.0)


def test_weekly_turnover_trend_handles_empty():
    assert M.weekly_turnover_trend(pd.DataFrame(), 1_000_000).empty


# =====================================================================================
# benchmarking
# =====================================================================================
def test_up_and_down_capture_of_identical_series():
    b = pd.Series([0.05, -0.03, 0.02, -0.01],
                  index=pd.date_range("2026-01-31", periods=4, freq="ME"))
    assert M.up_capture(b, b) == pytest.approx(1.0)
    assert M.down_capture(b, b) == pytest.approx(1.0)


def test_defensive_portfolio_has_low_down_capture():
    b = pd.Series([0.10, -0.10, 0.10, -0.10],
                  index=pd.date_range("2026-01-31", periods=4, freq="ME"))
    p = pd.Series([0.10, -0.05, 0.10, -0.05], index=b.index)
    assert M.down_capture(p, b) < 1.0
    assert M.up_capture(p, b) == pytest.approx(1.0)


def test_capture_is_nan_without_qualifying_months():
    b = pd.Series([0.05, 0.02], index=pd.date_range("2026-01-31", periods=2, freq="ME"))
    assert np.isnan(M.down_capture(b, b))


def test_equity_curve_rebases_everything_to_100_and_labels_the_series():
    idx = days(30)
    port = pd.Series(np.linspace(100, 130, 30), index=idx)
    n500 = pd.Series(np.linspace(20_000, 21_000, 30), index=idx)
    mom30 = pd.Series(np.linspace(30_000, 33_000, 30), index=idx)

    out = M.equity_curve_vs_benchmark(
        port, {"NIFTY 500": n500, "NIFTY200 MOMENTM 30": mom30},
        series_types={"NIFTY 500": "TRI", "NIFTY200 MOMENTM 30": "TRI"})

    curve = out["curve"]
    assert curve.iloc[0].tolist() == pytest.approx([100.0, 100.0, 100.0])
    assert curve["portfolio"].iloc[-1] == pytest.approx(130.0)
    assert curve["NIFTY 500"].iloc[-1] == pytest.approx(105.0)
    assert out["series_used"] == {"portfolio": "NAV", "NIFTY 500": "TRI",
                                  "NIFTY200 MOMENTM 30": "TRI"}
    assert out["inception"] == "2026-01-01"


def test_equity_curve_marks_unlabelled_benchmarks_unknown():
    idx = days(10)
    out = M.equity_curve_vs_benchmark(pd.Series(np.linspace(100, 110, 10), index=idx),
                                      {"X": pd.Series(np.linspace(1, 2, 10), index=idx)})
    assert out["series_used"]["X"] == "UNKNOWN"


def test_equity_curve_clips_benchmarks_to_the_portfolio_inception():
    """Benchmark history predating inception is trimmed, not used to rebase."""
    port = pd.Series([100.0, 110.0], index=days(2, "2026-06-01"))
    bench = pd.Series(np.linspace(1_000, 1_200, 120), index=days(120, "2026-04-01"))
    out = M.equity_curve_vs_benchmark(port, {"B": bench})
    assert out["curve"].index[0] == pd.Timestamp("2026-06-01")
    assert out["curve"]["B"].iloc[0] == pytest.approx(100.0)
    assert out["dropped"] == []


def test_non_overlapping_benchmark_is_reported_not_silently_dropped():
    port = pd.Series([100.0, 110.0], index=days(2, "2026-06-01"))
    stale = pd.Series(np.linspace(1_000, 1_100, 30), index=days(30, "2026-01-01"))
    out = M.equity_curve_vs_benchmark(port, {"STALE": stale})
    assert out["dropped"] == ["STALE"]
    assert "STALE" not in out["curve"].columns


# =====================================================================================
# summary bundle
# =====================================================================================
def test_summary_bundles_headline_metrics():
    idx = days(300)
    nav = pd.Series(np.linspace(1_000_000, 1_300_000, 300), index=idx)
    out = M.summary(nav)
    assert out["inception"] == "2026-01-01"
    assert out["index_value"] == pytest.approx(130.0)
    assert out["cagr"] > 0
    assert out["max_drawdown"] == pytest.approx(0.0)
    assert out["benchmark_series"] is None


def test_summary_adds_relative_metrics_and_records_the_series_used():
    idx = days(300)
    nav = pd.Series(np.linspace(1_000_000, 1_300_000, 300), index=idx)
    bench = pd.Series(np.linspace(20_000, 24_000, 300), index=idx)
    out = M.summary(nav, benchmark=bench, series_type="TRI")
    assert out["benchmark_series"] == "TRI"
    for k in ("beta", "alpha_jensen", "tracking_error", "information_ratio",
              "up_capture", "down_capture"):
        assert k in out
