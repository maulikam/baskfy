"""PM1.13: Screener rows are lagged to SEBI's deadlines, excluded by default, and never replace an NSE row."""

from datetime import date

import polars as pl

from portfolioml import fundamentals as F, screener as SC


def test_lagged_date_is_sebis_deadline_and_march_gets_sixty():
    assert SC.lagged_available_on(date(2016, 12, 31)) == date(2017, 2, 14)
    assert SC.lagged_available_on(date(2016, 3, 31)) == date(2016, 5, 30)


def test_banks_and_companies_read_their_own_revenue_line():
    company = {"quarters": {"headers": ["Dec 2016"], "data": {"Sales": {"Dec 2016": 100}, "Net Profit": {"Dec 2016": 9}}}}
    bank = {"quarters": {"headers": ["Dec 2016"], "data": {"Revenue": {"Dec 2016": 70}, "Net Profit": {"Dec 2016": 5}}}}
    assert SC.quarters(company)["2016-12-31"]["revenue"] == 100
    assert SC.quarters(bank)["2016-12-31"]["revenue"] == 70


def frame(*rows: tuple[int, date, str]) -> pl.DataFrame:
    return pl.DataFrame([{"instrument_id": i, "period_end": p, "pit_quality": q} for i, p, q in rows])


def test_lagged_rows_stay_out_unless_asked_and_lose_to_an_nse_row():
    rows = frame((1, date(2016, 12, 31), "lagged"), (2, date(2018, 12, 31), "lagged"), (2, date(2018, 12, 31), "as_filed"))
    assert F.usable(rows).height == 1
    kept = F.usable(rows, lagged=True)
    assert sorted(kept["pit_quality"].to_list()) == ["as_filed", "lagged"]
    assert kept.filter(pl.col("instrument_id") == 2)["pit_quality"].to_list() == ["as_filed"]


def test_cross_check_flags_only_over_two_percent():
    assert SC.differs(100.0, 103.0) and SC.differs(-100.0, -97.0)
    assert not SC.differs(100.0, 101.5) and not SC.differs(100.0, None)
