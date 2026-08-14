"""XIRR — money-weighted return. Acceptance: 2-cashflow case exact to 4 decimals."""
from __future__ import annotations

import math

import pytest

from app.analytics.metrics import xirr


# --- acceptance ------------------------------------------------------------------------
def test_two_cashflow_example_to_four_decimals():
    """-100000 on day 0, +115000 on day 365 -> exactly 15.00%."""
    rate = xirr([-100_000, 115_000], ["2026-01-01", "2027-01-01"])
    assert round(rate, 4) == 0.1500


def test_two_cashflow_example_is_exact_beyond_four_decimals():
    rate = xirr([-100_000, 115_000], ["2026-01-01", "2027-01-01"])
    assert rate == pytest.approx(0.15, abs=1e-9)


# --- SIP -------------------------------------------------------------------------------
def test_four_cashflow_sip():
    """Monthly SIP then a terminal value. Verified by discounting back to zero NPV."""
    amounts = [-25_000, -25_000, -25_000, 79_000]
    dates = ["2026-01-01", "2026-02-01", "2026-03-01", "2026-06-01"]
    rate = xirr(amounts, dates)

    assert math.isfinite(rate)
    npv = sum(
        a / (1.0 + rate) ** ((__import__("pandas").Timestamp(d)
                              - __import__("pandas").Timestamp(dates[0])).days / 365.0)
        for a, d in zip(amounts, dates)
    )
    assert npv == pytest.approx(0.0, abs=1e-6)
    assert 0.0 < rate < 2.0          # a real, positive, non-degenerate root


def test_sip_beats_a_worse_terminal_value():
    dates = ["2026-01-01", "2026-02-01", "2026-03-01", "2026-06-01"]
    good = xirr([-25_000, -25_000, -25_000, 82_000], dates)
    poor = xirr([-25_000, -25_000, -25_000, 76_000], dates)
    assert good > poor


# --- sign / ordering robustness ---------------------------------------------------------
def test_loss_gives_negative_rate():
    rate = xirr([-100_000, 90_000], ["2026-01-01", "2027-01-01"])
    assert rate == pytest.approx(-0.10, abs=1e-9)


def test_order_of_flows_does_not_matter():
    a = xirr([-100_000, 115_000], ["2026-01-01", "2027-01-01"])
    b = xirr([115_000, -100_000], ["2027-01-01", "2026-01-01"])
    assert a == pytest.approx(b, abs=1e-12)


def test_all_negative_flows_have_no_solution():
    assert math.isnan(xirr([-100, -100], ["2026-01-01", "2027-01-01"]))


def test_all_positive_flows_have_no_solution():
    assert math.isnan(xirr([100, 100], ["2026-01-01", "2027-01-01"]))


def test_single_flow_is_nan():
    assert math.isnan(xirr([-100], ["2026-01-01"]))


def test_half_year_doubling_annualises():
    """2x in 182.5 days annualises to ~300% (1.5 years-squared -> (1+r)^0.5 = 2)."""
    rate = xirr([-100_000, 200_000], ["2026-01-01", "2026-07-02"])
    # 182 days -> (1+r)^(182/365) = 2
    assert rate == pytest.approx(2.0 ** (365.0 / 182.0) - 1.0, rel=1e-6)


# --- solver robustness ------------------------------------------------------------------
def test_deep_loss_still_solves_via_bisection():
    """A near-total loss pushes Newton toward r <= -1; the bisection fallback must catch it."""
    rate = xirr([-100_000, 1_000], ["2026-01-01", "2027-01-01"])
    assert rate == pytest.approx(-0.99, abs=1e-6)


def test_extreme_gain_solves():
    rate = xirr([-1_000, 50_000], ["2026-01-01", "2027-01-01"])
    assert rate == pytest.approx(49.0, rel=1e-6)


def test_bad_guess_still_converges():
    for guess in (-0.9, 0.0, 5.0, 100.0):
        rate = xirr([-100_000, 115_000], ["2026-01-01", "2027-01-01"], guess=guess)
        assert rate == pytest.approx(0.15, abs=1e-6), f"failed from guess={guess}"


def test_many_irregular_flows_converge():
    amounts = [-50_000, -20_000, 10_000, -30_000, -15_000, 5_000, 125_000]
    dates = ["2026-01-05", "2026-02-11", "2026-03-03", "2026-05-19",
             "2026-08-01", "2026-09-15", "2027-01-20"]
    rate = xirr(amounts, dates)
    assert math.isfinite(rate)
    import pandas as pd
    npv = sum(a / (1.0 + rate) ** ((pd.Timestamp(d) - pd.Timestamp(dates[0])).days / 365.0)
              for a, d in zip(amounts, dates))
    assert npv == pytest.approx(0.0, abs=1e-5)
