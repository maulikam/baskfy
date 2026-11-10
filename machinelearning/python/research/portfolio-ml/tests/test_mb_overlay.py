"""PM0.13: the signal lag, the five-session hold, switching costs, cash and the rough tax."""

import datetime as dt

import numpy as np

from portfolioml import overlay as O


def days(n):
    return [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(n)]


def test_a_signal_change_at_a_close_trades_next_session_and_shows_after_that():
    assert O.positions(np.array([True, True, False, False, False])).tolist() == [True, True, True, True, False]


def test_debounce_needs_five_straight_closes_each_way():
    raw = np.array([True] * 2 + [False] * 4 + [True] + [False] * 5 + [True] * 2)
    got = O.debounce(raw)
    assert got[:11].all() and not got[11:].any()


def book(n, step):
    s = np.ones((n, 1))
    s[:, 0] = np.cumprod(np.r_[1.0, np.repeat(step, n - 1)])
    return s


def test_cash_accrues_and_held_earns_the_book():
    n = 5
    s = book(n, 1.01)
    assert abs(O.run(s, [], np.ones(n, dtype=bool), days(n))[-1] - s[-1, 0]) < 1e-12
    assert abs(O.run(s, [], np.zeros(n, dtype=bool), days(n))[-1] - 1.06 ** (5 / 252)) < 1e-12


def test_a_round_trip_pays_cost_twice():
    n = 6
    held = np.array([True, True, False, False, True, True])
    v = O.run(np.ones((n, 1)), [], held, days(n))
    assert abs(v[-1] - (1 - O.COST) ** 2 * 1.06 ** (2 / 252)) < 1e-12


def test_tax_takes_a_fifth_of_a_short_term_gain_and_nothing_on_a_loss():
    n = 3
    up = O.run(np.array([[1.0], [1.5], [1.5]]), [(0, 0, 1)], np.ones(n, dtype=bool), days(n), tax=True)
    assert abs(up[-1] - (1.5 - 0.5 * O.TAX_RATE)) < 1e-12
    down = O.run(np.array([[1.0], [0.5], [0.5]]), [(0, 0, 1)], np.ones(n, dtype=bool), days(n), tax=True)
    assert abs(down[-1] - 0.5) < 1e-12


def test_a_gain_held_past_a_year_is_not_taxed():
    d = [dt.date(2023, 1, 1), dt.date(2024, 6, 1), dt.date(2024, 6, 2)]
    v = O.run(np.array([[1.0], [2.0], [2.0]]), [(0, 0, 1)], np.ones(3, dtype=bool), d, tax=True)
    assert abs(v[-1] - 2.0) < 1e-12


def test_votes_count_above_and_majority_is_strict():
    a = np.array([[True, True, False, False], [True, True, True, False]])
    assert O.majority(a).tolist() == [False, True]
    assert O.at_least(a, 3).tolist() == [False, True]
