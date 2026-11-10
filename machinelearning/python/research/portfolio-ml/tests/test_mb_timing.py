"""PM0.15: past-only sizing, the cash pool, and the help rule."""

import datetime as dt

import numpy as np

from portfolioml import timing as T


def test_terciles_use_only_past_scores():
    s = np.arange(300.0)
    assert T.past_tercile_size(s, 100) == 1.0
    a = s.copy(); a[299] = -1.0
    assert T.past_tercile_size(a, 299) == 0.5
    b = s.copy(); b[299] = 1e6
    assert T.past_tercile_size(b, 299) == 1.5
    # a later score must not change the answer at i
    c = s.copy(); c[295:] = 1e9
    assert T.past_tercile_size(s, 290) == T.past_tercile_size(c, 290)


def test_idle_money_earns_cash():
    eq, share = T.pool_book([], 5, 0)
    assert abs(eq[-1] - (1 + T.CASH_RATE) ** (5 / 252)) < 1e-12 and share == 1.0


def test_a_line_pays_entry_and_exit_cost():
    p = np.array([1.0, 1.1, 1.2])
    eq, _ = T.pool_book([T.Line(1, p, 1.0)], 5, 0)
    daily = (1 + T.CASH_RATE) ** (1 / 252)
    expect = daily * (1 - T.COST) ** 2 * 1.2 * daily ** 2 / daily ** 0
    assert eq[3] > 1.15 and abs(eq[-1] / eq[3] - daily) < 1e-12 and expect > 0


def test_allocation_is_capped_by_cash():
    p = np.array([1.0, 1.0])
    eq, share = T.pool_book([T.Line(1, p, 1.0), T.Line(1, p, 1.0)], 4, 0)
    assert abs(share - 0.5) < 1e-9
    assert eq[-1] > 0


def test_help_needs_two_points_and_no_deeper_drawdown():
    base = {"cagr": 0.10, "max_dd": -0.30}
    assert T.helps(base, {"cagr": 0.125, "max_dd": -0.29})
    assert not T.helps(base, {"cagr": 0.11, "max_dd": -0.20})
    assert not T.helps(base, {"cagr": 0.20, "max_dd": -0.31})


def test_period_stats_reads_cagr_and_drawdown():
    d = [dt.date(2020, 1, 1), dt.date(2021, 1, 1)]
    s = T.period_stats(np.array([1.0, 1.1]), d)
    assert abs(s["cagr"] - 0.1) < 0.002 and s["max_dd"] == 0.0
