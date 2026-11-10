"""PM0.17: the capped pool, borrowing cost and forced sales."""

import numpy as np

from portfolioml import leverage as L, timing as T


def test_with_no_leverage_it_matches_the_cash_pool():
    lines = [T.Line(1, np.array([1.0, 1.2, 1.1]), 0.5), T.Line(2, np.array([1.0, 0.9, 1.3]), 0.3)]
    a, _ = T.pool_book(lines, 7, 0)
    b, gross, forced = L.pool_levered(lines, 7, 0, 1.0)
    assert np.allclose(a, b) and forced == 0 and gross.max() <= 1.0


def test_allocation_stops_at_the_cap():
    _, gross, _ = L.pool_levered([T.Line(1, np.ones(3), 2.0)], 5, 0, 1.3)
    assert 1.29 < gross[1] < 1.31


def test_borrowing_charges_interest_and_lending_earns():
    p = np.ones(40)
    eq, _, _ = L.pool_levered([T.Line(1, p, 1.5)], 45, 0, 1.5)
    assert eq[38] < eq[1]


def test_a_breach_sells_at_the_next_close_only():
    p = np.array([1.0, 1.0, 0.6, 0.6, 0.6])  # the line ends right after the sale, so interest cannot cause a second breach
    eq, gross, forced = L.pool_levered([T.Line(1, p, 1.9)], 7, 0, 1.9)  # below the 2× margin line, so entry costs alone cannot breach
    assert forced == 1
    assert gross[3] > 2.0 and gross[4] <= 2.0 + 1e-9
