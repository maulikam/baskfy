"""PM0.10: the target is relative to the same day; the filter removes the worst of each date only."""

from datetime import date

import numpy as np
import polars as pl

from portfolioml import downside as D

A, B = date(2024, 1, 1), date(2024, 1, 2)


def test_target_subtracts_only_that_days_median():
    f = pl.DataFrame({"date": [A, A, A, B], "fwd_ret_h": [0.0, 0.2, 1.0, 5.0]})
    assert D.relative_return(f)["y"].to_list() == [-0.2, 0.0, 0.8, 0.0]


def test_filter_drops_the_worst_fraction_within_each_date():
    f = pl.DataFrame({"date": [A] * 5 + [B] * 5, "p10": [-5.0, -4.0, -3.0, -2.0, -1.0, 1.0, 2.0, 3.0, 4.0, 5.0], "k": list(range(10))})
    assert D.drop_worst(f, 0.2)["k"].to_list() == [1, 2, 3, 4, 6, 7, 8, 9]


def test_coverage_counts_below_and_inside():
    y = np.array([-2.0, 0.0, 2.0])
    assert D.coverage(y, np.array([-1.0] * 3), np.array([1.0] * 3)) == (1 / 3, 1 / 3)


def test_pinball_penalises_a_missed_low_quantile_more_on_the_downside_side():
    assert D.pinball(np.array([1.0]), np.array([0.0]), 0.1) == 0.1
    assert D.pinball(np.array([-1.0]), np.array([0.0]), 0.1) == 0.9


def test_a_year_counts_only_when_halved_falls_and_lift_holds():
    y = pl.DataFrame({"arm": ["none", "f", "none", "f"], "year": [1, 1, 2, 2], "halved": [0.1, 0.05, 0.1, 0.05], "lift": [2.0, 2.0, 2.0, 1.9]})
    assert D.improved_years(y, "none", "f") == 1
