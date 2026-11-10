"""PM0.11: how paths, first touch, pre-touch lows and write-offs are read."""

import numpy as np

from portfolioml import census as C


def one(*closes):
    return np.array(closes, dtype=float)[:, None]


def test_a_gap_carries_the_last_close():
    p = C.paths(one(10.0, np.nan, 12.0), np.array([10.0]), np.array([3]), np.array([False]), "a")
    assert p[:, 0].tolist() == [1.0, 1.0, 1.2]


def test_variant_b_zeroes_only_write_offs_after_sixty_sessions_past_the_last_bar():
    closes = np.full((70, 2), np.nan)
    closes[:5] = 10.0
    p = C.paths(closes, np.array([10.0, 10.0]), np.array([5, 5]), np.array([True, False]), "b")
    assert p[64, 0] == 1.0 and p[65, 0] == 0.0 and p[69, 1] == 1.0
    assert (C.paths(closes, np.array([10.0, 10.0]), np.array([5, 5]), np.array([True, False]), "a")[69] == 1.0).all()


def test_first_touch_counts_sessions_from_entry_and_zero_means_never():
    p = np.array([[1.0, 1.0], [1.5, 1.2], [2.0, 1.3]])
    assert C.first_touch(p, 2.0).tolist() == [3, 0]


def test_low_before_touch_ignores_later_falls_and_counts_entry():
    p = one(0.7, 2.1, 0.2)
    assert C.low_before(p, C.first_touch(p, 2.0))[0] == 0.7
    q = one(1.1, 2.1)
    assert C.low_before(q, C.first_touch(q, 2.0))[0] == 1.0


def test_peak_to_trough_runs_from_the_high_not_from_entry():
    assert abs(C.peak_to_trough(one(2.0, 1.0, 1.5))[0] + 0.5) < 1e-9


def test_a_huge_one_day_step_is_flagged_and_a_write_off_is_not():
    assert C.suspicious_step(one(1.0, 2.0))[0]
    assert not C.suspicious_step(one(1.0, 1.2, 0.0))[0]
