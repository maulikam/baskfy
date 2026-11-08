"""06 PM4 AC1: each window must pass both tests, against the investable universe."""

from datetime import date

import polars as pl

import run_pm4 as R


def picks(by_year: dict[int, tuple[float, float]]) -> pl.DataFrame:
    rows = []
    for y, (mean, hit) in by_year.items():
        rows += [{"date": date(y, 6, 1), "fwd_ret_h": mean, "label": int(i < round(hit * 100))} for i in range(100)]
    return pl.DataFrame(rows)


BASE = picks({2024: (0.0, 0.10), 2025: (0.0, 0.10)})


def table(by_year: dict[int, tuple[float, float]]) -> pl.DataFrame:
    return R.gate_table({"m": picks(by_year)}, BASE)


def test_passing_both_years_passes_every_window():
    assert R.verdict(table({2024: (0.05, 0.20), 2025: (0.05, 0.20)}), "m")


def test_one_bad_year_fails_even_if_the_pool_passes():
    got = table({2024: (0.30, 0.40), 2025: (-0.05, 0.10)})
    assert got.filter(pl.col("window") == "2024+2025")["pass"].item()
    assert not R.verdict(got, "m")


def test_the_hit_rate_needs_one_and_a_half_times_the_universe():
    assert not R.verdict(table({2024: (0.05, 0.14), 2025: (0.05, 0.14)}), "m")
    assert R.verdict(table({2024: (0.05, 0.16), 2025: (0.05, 0.16)}), "m")


def test_coverage_counts_only_rows_with_a_ttm_figure_from_2019():
    keys = pl.DataFrame({"instrument_id": [1, 1], "date": [date(2018, 1, 1), date(2019, 6, 1)]})
    filings = pl.DataFrame(schema={"instrument_id": pl.Int64, "period_end": pl.Date, "period_type": pl.String,
                                   "available_on": pl.Date, "filed_at": pl.Date, "revenue": pl.Float64, "pat": pl.Float64,
                                   "ebitda": pl.Float64, "depreciation": pl.Float64, "total_debt": pl.Float64,
                                   "total_equity": pl.Float64, "cfo": pl.Float64})
    share, _ = R.fundamentals_coverage(keys, filings)
    assert share == 0.0
