"""PM1 AC1: a feature at date d changes only when a filing with available_on <= d exists."""

from datetime import date, datetime

import polars as pl

from portfolioml import fundamentals as F

QUARTERS = [(2023, 3), (2023, 6), (2023, 9), (2023, 12), (2024, 3), (2024, 6), (2024, 9), (2024, 12)]
DAYS = {3: 31, 6: 30, 9: 30, 12: 31}


def row(y: int, m: int, revenue: float, available: date, kind: str = "consolidated", pat: float = 10.0) -> dict:
    return {
        "instrument_id": 1, "period_end": date(y, m, DAYS[m]), "period_type": kind, "available_on": available,
        "filed_at": available, "revenue": revenue, "pat": pat, "ebitda": 20.0, "depreciation": 2.0,
        "total_debt": None, "total_equity": None, "cfo": None,
    }


def history(kind: str = "consolidated") -> list[dict]:
    return [row(y, m, 100.0 + 10 * i, date(y + (m == 12), m % 12 + 1, 20), kind) for i, (y, m) in enumerate(QUARTERS)]


def frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema_overrides={c: pl.Float64 for c in ("revenue", "pat", "ebitda", "depreciation", "total_debt", "total_equity", "cfo")})


def sessions(*days: date) -> pl.DataFrame:
    return pl.DataFrame({"instrument_id": [1] * len(days), "date": list(days)})


def at(rows: list[dict], day: date) -> dict:
    return F.join_features(sessions(day), F.snapshots(frame(rows))).to_dicts()[0]


def test_a_filing_is_invisible_before_it_is_available():
    last_day = date(2025, 1, 20)
    assert at(history(), date(2025, 1, 19)) == at(history()[:-1], date(2025, 1, 19))
    assert at(history(), last_day)["ttm_rev_yoy"] != at(history(), date(2025, 1, 19))["ttm_rev_yoy"]


def test_a_filing_made_after_the_close_counts_from_its_available_on_not_its_period_end():
    # period_end 31 Dec 2024 is long before 20 Jan 2025; only available_on may gate it.
    assert at(history(), date(2025, 1, 1))["rev_q_yoy"] == at(history()[:-1], date(2025, 1, 1))["rev_q_yoy"]


def test_a_restatement_changes_features_only_from_the_day_it_was_published():
    restated = [*history(), row(2024, 9, 500.0, date(2025, 3, 1))]
    before, after = date(2025, 2, 28), date(2025, 3, 1)
    assert at(restated, before) == at(history(), before)
    assert at(restated, after)["ttm_rev_yoy"] != at(history(), after)["ttm_rev_yoy"]


def test_features_are_constant_between_filings():
    days = [date(2025, 1, 20), date(2025, 1, 21), date(2025, 2, 27)]
    got = F.join_features(sessions(*days), F.snapshots(frame(history()))).sort("date")
    assert got.select(F.FEATURES).unique().height == 1


def test_a_lone_standalone_quarter_never_enters_a_consolidated_series():
    rows = [*history(), row(2024, 12, 9999.0, date(2025, 1, 19), "standalone")]
    assert at(rows, date(2025, 1, 25))["rev_q_yoy"] == at(history(), date(2025, 1, 25))["rev_q_yoy"]


def test_no_figure_exists_before_the_first_filing():
    assert at(history(), date(2023, 1, 1))["ttm_rev_yoy"] is None


def _filing(url):
    return pl.DataFrame({
        "instrument_id": [1], "period_end": [date(2024, 12, 31)], "period_type": ["consolidated"],
        "available_on": [date(2025, 1, 20)], "filed_at": [datetime(2025, 1, 19, 18, 0)], "revenue": [100.0], "pat": [10.0],
        "ebitda": [20.0], "depreciation": [5.0], "total_debt": [None], "total_equity": [None], "cfo": [None], "source_url": [url],
    }, schema_overrides={"total_debt": pl.Float64, "total_equity": pl.Float64, "cfo": pl.Float64})


def test_is_bank_comes_from_the_filing_format():
    base = "https://nsearchives.nseindia.com/corporate/xbrl/"
    got = {name: F.snapshots(_filing(base + name))["is_bank"][0] for name in
           ("BANKING_51549_208046_21012020011136_WEB.xml", "INTEGRATED_FILING_BANKING_1661430_29042026070224_WEB.xml",
            "INDAS_38795_34792_14082018123759_WEB.xml", "NBFC_1_2_3_WEB.xml", "INTEGRATED_FILING_NBFC_1_2_WEB.xml")}
    assert list(got.values()) == [1.0, 1.0, 0.0, 0.0, 0.0]

