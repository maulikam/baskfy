"""TWR chaining — flows must never register as performance.

Acceptance: twr_chained() equals the simple return when there are no external cashflows.

The index-continuity-across-a-rebalance-version test named in the brief needs
rebalance_versions and the T+1 OHLC-average chaining, which are PHASE 4 deliverables.
The pieces that exist today are covered here; that test lands in phase 4 in this file.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.analytics import db
from app.analytics.metrics import daily_returns, twr_chained, twr_total


def navs(pairs) -> pd.Series:
    return pd.Series({pd.Timestamp(d): float(v) for d, v in pairs})


# --- acceptance: no cashflows -> TWR == simple return ------------------------------------
def test_twr_equals_simple_return_with_no_cashflows():
    s = navs([("2026-01-01", 1_000_000), ("2026-01-02", 1_050_000),
              ("2026-01-03", 1_020_000), ("2026-01-04", 1_180_000)])
    simple = s.iloc[-1] / s.iloc[0] - 1.0
    assert twr_total(s) == pytest.approx(simple, rel=1e-12)
    assert twr_chained(s).iloc[-1] == pytest.approx(100.0 * (1.0 + simple), rel=1e-12)


def test_twr_equals_simple_return_when_cashflows_are_empty_series():
    s = navs([("2026-01-01", 500_000), ("2026-01-02", 525_000)])
    empty = pd.Series(dtype=float)
    assert twr_total(s, empty) == pytest.approx(0.05, rel=1e-12)


def test_index_starts_at_exactly_100():
    s = navs([("2026-01-01", 987_654.32), ("2026-01-02", 1_000_000)])
    assert twr_chained(s).iloc[0] == 100.0


def test_chained_index_tracks_nav_ratio_without_flows():
    s = navs([("2026-01-01", 1_000_000), ("2026-01-02", 1_100_000),
              ("2026-01-03", 990_000)])
    idx = twr_chained(s)
    for date, nav in s.items():
        assert idx.loc[date] == pytest.approx(100.0 * nav / s.iloc[0], rel=1e-12)


# --- flows are neutralised ---------------------------------------------------------------
def test_pure_contribution_produces_zero_return():
    s = navs([("2026-02-01", 1_000_000), ("2026-02-02", 1_500_000)])
    cf = pd.Series({pd.Timestamp("2026-02-02"): -500_000.0})   # invest = negative
    assert twr_total(s, cf) == pytest.approx(0.0, abs=1e-12)


def test_pure_withdrawal_produces_zero_return():
    s = navs([("2026-03-01", 1_000_000), ("2026-03-02", 800_000)])
    cf = pd.Series({pd.Timestamp("2026-03-02"): 200_000.0})    # withdraw = positive
    assert twr_total(s, cf) == pytest.approx(0.0, abs=1e-12)


def test_return_measured_on_pre_flow_base():
    s = navs([("2026-02-01", 1_000_000), ("2026-02-02", 1_550_000)])
    cf = pd.Series({pd.Timestamp("2026-02-02"): -500_000.0})
    assert twr_total(s, cf) == pytest.approx(0.05, rel=1e-12)


def test_twr_across_a_rebalance_with_a_mid_period_contribution():
    """Three market moves plus one deposit: TWR must be the product of the market legs."""
    s = navs([("2026-04-01", 1_000_000),
              ("2026-04-02", 1_100_000),      # +10%
              ("2026-04-03", 1_599_500),      # +4.5% then a 450k deposit: 1.1M*1.045 + 450k
              ("2026-04-04", 1_679_475)])     # +5% on the post-deposit base
    cf = pd.Series({pd.Timestamp("2026-04-03"): -450_000.0})
    expected = 1.10 * 1.045 * 1.05 - 1.0
    assert twr_total(s, cf) == pytest.approx(expected, rel=1e-9)


def test_multiple_flows_on_the_same_day_are_netted():
    s = navs([("2026-05-01", 1_000_000), ("2026-05-02", 1_200_000)])
    cf = pd.Series([-300_000.0, 100_000.0],
                   index=[pd.Timestamp("2026-05-02"), pd.Timestamp("2026-05-02")])
    # net flow in = 200k, so the market contributed nothing
    assert twr_total(s, cf) == pytest.approx(0.0, abs=1e-12)


def test_flow_on_a_day_with_no_prior_capital_does_not_blow_up():
    s = navs([("2026-06-01", 0.0), ("2026-06-02", 1_000_000)])
    cf = pd.Series({pd.Timestamp("2026-06-02"): -1_000_000.0})
    assert twr_total(s, cf) == pytest.approx(0.0, abs=1e-12)
    assert twr_chained(s, cf).iloc[0] == 100.0


# --- daily returns -----------------------------------------------------------------------
def test_daily_returns_drop_the_first_observation():
    s = navs([("2026-01-01", 100.0), ("2026-01-02", 110.0), ("2026-01-03", 99.0)])
    r = daily_returns(s)
    assert len(r) == 2
    assert r.iloc[0] == pytest.approx(0.10)
    assert r.iloc[1] == pytest.approx(-0.10)


def test_empty_series_is_handled():
    assert twr_chained(pd.Series(dtype=float)).empty
    assert twr_total(pd.Series(dtype=float)) == 0.0


# --- the stored index agrees with the computed one ---------------------------------------
def test_db_rechain_matches_metrics_twr(tmp_path):
    """db.rechain_index() and metrics.twr_chained() must not drift apart."""
    series = [("2026-01-01", 1_000_000.0), ("2026-01-02", 1_050_000.0),
              ("2026-01-03", 1_600_000.0), ("2026-01-04", 1_680_000.0)]
    with db.connect(str(tmp_path / "p.db")) as conn:
        db.migrate(conn)
        db.record_cashflow(conn, "2026-01-03", -500_000.0, "invest")
        for d, nav in series:
            db.save_snapshot(conn, {"date": d, "nav": nav, "invested": nav, "cash": 0.0,
                                    "holdings_json": "{}"})
        stored = {r["date"]: r["index_value"] for r in db.snapshot_series(conn)}

    computed = twr_chained(navs(series),
                           pd.Series({pd.Timestamp("2026-01-03"): -500_000.0}))
    for d, _ in series:
        assert stored[d] == pytest.approx(float(computed.loc[pd.Timestamp(d)]), rel=1e-12)
