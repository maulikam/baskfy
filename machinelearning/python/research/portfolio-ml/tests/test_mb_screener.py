"""PM1.13: reading the Screener export, the operator's cross-check, and the scale_suspect rule."""

from flag_scale_suspect import flagged, scale_gap
from portfolioml import screener as SC


def test_banks_and_companies_read_their_own_revenue_line():
    company = {"quarters": {"headers": ["Dec 2016"], "data": {"Sales": {"Dec 2016": 100}, "Net Profit": {"Dec 2016": 9}}}}
    bank = {"quarters": {"headers": ["Dec 2016"], "data": {"Revenue": {"Dec 2016": 70}, "Net Profit": {"Dec 2016": 5}}}}
    assert SC.quarters(company)["2016-12-31"]["revenue"] == 100
    assert SC.quarters(bank)["2016-12-31"]["revenue"] == 70


def test_cross_check_flags_only_over_two_percent():
    assert SC.differs(100.0, 103.0) and SC.differs(-100.0, -97.0)
    assert not SC.differs(100.0, 101.5) and not SC.differs(100.0, None)


def row(url: str, revenue: float, pat: float) -> dict:
    return {"source_url": url, "revenue": revenue, "pat": pat}


def test_a_scale_gap_needs_an_exact_power_of_ten_on_revenue_and_a_consistent_pat():
    assert scale_gap(55.72, 4.75, 5572.0, 475.0) == 2
    assert scale_gap(5572.0, 475.0, 557.2, 47.5) == -1
    assert scale_gap(100.0, 10.0, 130.0, 13.0) == 0
    assert scale_gap(55.72, 4.75, 5572.0, -30.0) == 0
    assert scale_gap(0.03, 0.01, 0.30, 0.10) == 0


def test_the_earlier_filing_is_flagged_when_a_refiling_shows_a_scale_error():
    group = [row("first", 55.72, 4.75), row("second", 5572.0, 475.0)]
    assert flagged(group, None) == {"first"}


def test_the_reference_decides_which_filing_is_wrong():
    group = [row("first", 5572.0, 475.0), row("second", 55.72, 4.75)]
    assert flagged(group, {"revenue": 5572.0, "net_profit": 475.0}) == {"second"}


def test_a_single_filing_is_flagged_only_against_the_reference():
    assert flagged([row("only", 251.86, -29.17)], {"revenue": 2519.0, "net_profit": -292.0}) == {"only"}
    assert flagged([row("only", 251.86, -29.17)], None) == set()
