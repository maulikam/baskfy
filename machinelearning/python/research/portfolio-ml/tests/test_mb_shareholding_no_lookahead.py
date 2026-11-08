"""PM2 AC: a feature at date d changes only when a filing with available_on <= d exists."""

from datetime import date

import polars as pl

from portfolioml import shareholding as S

QUARTERS = [(2023, 3), (2023, 6), (2023, 9), (2023, 12), (2024, 3), (2024, 6), (2024, 9), (2024, 12)]
DAYS = {3: 31, 6: 30, 9: 30, 12: 31}


def row(y: int, m: int, promoter: float, available: date, pledged: float = 1.0) -> dict:
    return {
        "instrument_id": 1, "period_end": date(y, m, DAYS[m]), "available_on": available, "filed_at": available,
        "promoter_pct": promoter, "promoter_pledged_pct": pledged, "fii_pct": 10.0, "dii_pct": 10.0,
        "mf_pct": 5.0, "public_pct": 100.0 - promoter,
    }


def history() -> list[dict]:
    return [row(y, m, 50.0 + i * 0.1, date(y + (m == 12), m % 12 + 1, 20)) for i, (y, m) in enumerate(QUARTERS)]


def sessions(*days: date) -> pl.DataFrame:
    return pl.DataFrame({"instrument_id": [1] * len(days), "date": list(days)})


def at(rows: list[dict], day: date) -> dict:
    return S.join_features(sessions(day), S.snapshots(pl.DataFrame(rows))).to_dicts()[0]


def test_a_filing_is_invisible_before_it_is_available():
    assert at(history(), date(2025, 1, 19)) == at(history()[:-1], date(2025, 1, 19))
    assert at(history(), date(2025, 1, 20))["promoter_pct"] != at(history(), date(2025, 1, 19))["promoter_pct"]


def test_a_filing_counts_from_its_available_on_not_its_period_end():
    assert at(history(), date(2025, 1, 1))["promoter_pct"] == at(history()[:-1], date(2025, 1, 1))["promoter_pct"]


def test_a_revision_changes_features_only_from_the_day_it_was_published():
    revised = [*history(), row(2024, 9, 70.0, date(2025, 3, 1))]
    assert at(revised, date(2025, 2, 28)) == at(history(), date(2025, 2, 28))
    assert at(revised, date(2025, 3, 1))["promoter_pct_chg1q"] != at(history(), date(2025, 3, 1))["promoter_pct_chg1q"]


def test_features_are_constant_between_filings():
    days = [date(2025, 1, 20), date(2025, 1, 21), date(2025, 2, 27)]
    got = S.join_features(sessions(*days), S.snapshots(pl.DataFrame(history()))).sort("date")
    assert got.select(S.FEATURES).unique().height == 1


def test_a_missing_quarter_gives_no_change_rather_than_a_wrong_one():
    rows = [r for r in history() if r["period_end"] != date(2024, 9, 30)]
    got = at(rows, date(2025, 2, 1))
    assert got["promoter_pct_chg1q"] is None
    assert got["promoter_pct"] is not None


def test_no_figure_exists_before_the_first_filing():
    assert at(history(), date(2023, 1, 1))["promoter_pct"] is None


def test_promoter_buying_needs_half_a_point():
    rows = [row(2024, 9, 50.0, date(2024, 10, 20)), row(2024, 12, 50.49, date(2025, 1, 20))]
    assert at(rows, date(2025, 1, 25))["promoter_buying"] == 0.0
    rows[1] = row(2024, 12, 50.5, date(2025, 1, 20))
    assert at(rows, date(2025, 1, 25))["promoter_buying"] == 1.0


def test_pledge_flags_follow_the_change():
    rows = [row(2024, 9, 50.0, date(2024, 10, 20), 10.0), row(2024, 12, 50.0, date(2025, 1, 20), 12.0)]
    got = at(rows, date(2025, 1, 25))
    assert (got["pledge_rising"], got["pledge_falling"]) == (1.0, 0.0)
