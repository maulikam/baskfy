"""Drawdown — depth, duration, recovery on synthetic curves with known answers."""
from __future__ import annotations

import pandas as pd
import pytest

from app.analytics.metrics import (calmar, drawdown_series, max_drawdown,
                                   var_historical, var_parametric)


def curve(values, start="2026-01-01") -> pd.Series:
    idx = pd.date_range(start, periods=len(values), freq="D")
    return pd.Series([float(v) for v in values], index=idx)


# --- acceptance: exact depth + duration --------------------------------------------------
def test_known_curve_depth_and_duration():
    """100 -> 120 (peak) -> 90 (trough, -25%) -> 120 (recovered on day 8)."""
    s = curve([100, 110, 120, 110, 100, 90, 100, 110, 120, 130])
    dd = max_drawdown(s)

    assert dd["depth"] == pytest.approx(-0.25)          # 90/120 - 1
    assert dd["depth_pct"] == pytest.approx(-25.0)
    assert dd["peak_date"] == pd.Timestamp("2026-01-03")
    assert dd["trough_date"] == pd.Timestamp("2026-01-06")
    assert dd["recovery_date"] == pd.Timestamp("2026-01-09")
    assert dd["duration_days"] == 6                     # 03 Jan -> 09 Jan
    assert dd["recovered"] is True
    assert dd["current_dd"] == pytest.approx(0.0)       # ends at a new high


def test_unrecovered_drawdown_runs_to_the_last_observation():
    s = curve([100, 150, 120, 110, 105])
    dd = max_drawdown(s)
    assert dd["depth"] == pytest.approx(105 / 150 - 1.0)
    assert dd["peak_date"] == pd.Timestamp("2026-01-02")
    assert dd["trough_date"] == pd.Timestamp("2026-01-05")
    assert dd["recovery_date"] is None
    assert dd["recovered"] is False
    assert dd["duration_days"] == 3                     # peak -> last observation
    assert dd["current_dd"] == pytest.approx(dd["depth"])


def test_deepest_of_several_drawdowns_wins():
    #        peak 110, -18.2%        peak 130, -30.8%
    s = curve([100, 110, 90, 105, 130, 90, 100])
    dd = max_drawdown(s)
    assert dd["depth"] == pytest.approx(90 / 130 - 1.0)
    assert dd["peak_date"] == pd.Timestamp("2026-01-05")
    assert dd["trough_date"] == pd.Timestamp("2026-01-06")


def test_monotonic_rise_has_no_drawdown():
    dd = max_drawdown(curve([100, 101, 102, 103]))
    assert dd["depth"] == pytest.approx(0.0)
    assert dd["current_dd"] == pytest.approx(0.0)
    assert dd["recovered"] is True


def test_flat_curve_has_no_drawdown():
    dd = max_drawdown(curve([100, 100, 100]))
    assert dd["depth"] == pytest.approx(0.0)


def test_peak_is_the_first_date_reaching_that_high():
    """A plateau at the peak must anchor duration to the FIRST day at that level."""
    s = curve([100, 120, 120, 120, 60, 120])
    dd = max_drawdown(s)
    assert dd["peak_date"] == pd.Timestamp("2026-01-02")
    assert dd["depth"] == pytest.approx(-0.5)
    assert dd["recovery_date"] == pd.Timestamp("2026-01-06")
    assert dd["duration_days"] == 4


def test_single_point_and_empty_curves():
    assert max_drawdown(curve([100]))["depth"] == pytest.approx(0.0)
    empty = max_drawdown(pd.Series(dtype=float))
    assert empty["depth"] == 0.0 and empty["peak_date"] is None


def test_recovery_requires_reaching_the_peak_not_merely_approaching_it():
    s = curve([100, 200, 100, 199.99])
    dd = max_drawdown(s)
    assert dd["recovery_date"] is None
    assert dd["recovered"] is False


# --- underwater series -------------------------------------------------------------------
def test_drawdown_series_matches_max_drawdown():
    s = curve([100, 110, 120, 90, 120])
    ds = drawdown_series(s)
    assert ds.min() == pytest.approx(max_drawdown(s)["depth"])
    assert ds.iloc[0] == pytest.approx(0.0)
    assert (ds <= 1e-12).all()          # never positive


def test_drawdown_series_length_matches_input():
    s = curve([100, 90, 95, 80])
    assert len(drawdown_series(s)) == len(s)


# --- calmar ------------------------------------------------------------------------------
def test_calmar_is_cagr_over_abs_max_drawdown():
    s = curve([100.0] + [100.0] * 180 + [75.0] + [150.0] * 184)
    dd = max_drawdown(s)
    assert calmar(s) == pytest.approx(
        __import__("app.analytics.metrics", fromlist=["cagr"]).cagr(s) / abs(dd["depth"]))


def test_calmar_without_a_drawdown_is_undefined_not_zero():
    """No drawdown means the denominator is zero; growth with no observed downside is
    undefined, not a Calmar of 0."""
    import math
    assert math.isnan(calmar(curve([100, 110, 120])))
    assert calmar(curve([100, 100, 100])) == 0.0


# --- VaR sanity (positive loss magnitudes) ------------------------------------------------
def test_var_is_reported_as_a_positive_loss():
    r = pd.Series([-0.03, -0.01, 0.00, 0.01, 0.02, 0.015, -0.025, 0.005],
                  index=pd.date_range("2026-01-01", periods=8, freq="D"))
    assert var_parametric(r) > 0
    assert var_historical(r) > 0


def test_historical_var_is_the_5th_percentile_loss():
    r = pd.Series([-0.10, -0.05, 0.0, 0.05, 0.10] * 4,
                  index=pd.date_range("2026-01-01", periods=20, freq="D"))
    assert var_historical(r, 0.95) == pytest.approx(-r.quantile(0.05))
