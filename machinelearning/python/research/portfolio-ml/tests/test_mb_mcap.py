"""PM3: market cap uses only shares known by the date, and features are computed per day."""

from datetime import date

import polars as pl

from portfolioml import sector as M


def filing(shares: int | None, available: date) -> dict:
    return {"instrument_id": 1, "available_on": available, "filed_at": available, "shares_outstanding": shares}


def test_shares_are_not_known_before_available_on():
    filings = pl.DataFrame([filing(100, date(2024, 1, 20)), filing(200, date(2024, 4, 20))])
    panel = pl.DataFrame({"instrument_id": [1, 1, 1], "date": [date(2024, 1, 19), date(2024, 4, 19), date(2024, 4, 20)],
                          "close_raw": [1e5, 1e5, 1e5]})
    got = M.join_mcap(panel, filings).sort("date")["mcap_cr"].to_list()
    assert got == [None, 1.0, 2.0]


def test_deciles_run_smallest_to_largest_within_the_day():
    panel = pl.DataFrame({"date": [date(2024, 1, 1)] * 10, "mcap_cr": [float(i + 1) for i in range(10)]})
    assert M.mcap_features(panel)["mcap_decile"].to_list() == [float(i + 1) for i in range(10)]


def test_a_name_without_a_sector_has_no_sector_feature():
    panel = pl.DataFrame({"date": [date(2024, 1, 1)] * 3, "sector": ["A", "A", None], "r21": [0.1, 0.3, 0.5], "r63": [0.1, 0.3, 0.5]})
    got = M.sector_features(panel)
    assert got["sector_r21"].to_list()[:2] == [0.2, 0.2] and got["sector_r21"][2] is None
    assert round(got["rel_sector_r21"][1], 6) == 0.1


def test_the_check_reports_the_median_relative_error():
    panel = pl.DataFrame({"instrument_id": [1, 2], "date": [date(2025, 9, 1)] * 2, "mcap_cr": [110.0, 100.0]})
    daily = pl.DataFrame({"instrument_id": [1, 2], "date": [date(2025, 9, 1)] * 2, "marketcap_cr": [100, 100]})
    got = M.mcap_check(panel, daily)
    assert got["rows"] == 2 and round(got["median_rel_error"], 9) == 0.05
