"""Benchmark ingestion: TRI CSV parsing, PRI/TRI coexistence, and series labelling."""
from __future__ import annotations

import pandas as pd
import pytest

from app.analytics import benchmark as B
from app.analytics import db


@pytest.fixture()
def conn(tmp_path):
    with db.connect(str(tmp_path / "p.db")) as c:
        db.migrate(c)
        yield c


# =====================================================================================
# TRI CSV parsing
# =====================================================================================
def test_tri_csv_standard_niftyindices_export(tmp_path):
    p = tmp_path / "tri.csv"
    # Real exports QUOTE values containing thousands separators.
    p.write_text(
        'Date,Total Returns Index,Net Total Return Index\n'
        '01 Jan 2026,"12,345.67","12,100.10"\n'
        '02 Jan 2026,"12,400.00","12,150.00"\n')
    df = B.tri_csv_loader(str(p))
    assert list(df["date"]) == ["2026-01-01", "2026-01-02"]
    assert df["tri"].tolist() == [12_345.67, 12_400.00]
    assert df["net_tri"].tolist() == [12_100.10, 12_150.00]


def test_tri_csv_prefers_gross_over_net(tmp_path):
    p = tmp_path / "tri.csv"
    p.write_text("Date,Net Total Return Index,Total Returns Index\n"
                 "01 Jan 2026,100.0,200.0\n")
    df = B.tri_csv_loader(str(p))
    assert df["tri"].iloc[0] == 200.0        # gross, not net


def test_tri_csv_falls_back_to_net_when_gross_absent(tmp_path):
    p = tmp_path / "tri.csv"
    p.write_text("Date,Net Total Return Index\n01 Jan 2026,100.0\n")
    assert B.tri_csv_loader(str(p))["tri"].iloc[0] == 100.0


@pytest.mark.parametrize("raw,expected", [
    ("01 Jan 2026", "2026-01-01"),
    ("01-Jan-2026", "2026-01-01"),
    ("01/02/2026", "2026-02-01"),     # day-first, as niftyindices exports
    ("2026-01-01", "2026-01-01"),
    ("1 January 2026", "2026-01-01"),
])
def test_tri_csv_date_formats(tmp_path, raw, expected):
    p = tmp_path / "tri.csv"
    p.write_text(f"Date,Total Returns Index\n{raw},100.0\n")
    assert B.tri_csv_loader(str(p))["date"].iloc[0] == expected


def test_tri_csv_skips_unparseable_rows(tmp_path):
    p = tmp_path / "tri.csv"
    p.write_text("Date,Total Returns Index\n"
                 "01 Jan 2026,100.0\n"
                 ",\n"
                 "not a date,200.0\n"
                 "02 Jan 2026,-\n"
                 "03 Jan 2026,300.0\n")
    df = B.tri_csv_loader(str(p))
    assert list(df["date"]) == ["2026-01-01", "2026-01-03"]


def test_tri_csv_deduplicates_and_sorts(tmp_path):
    p = tmp_path / "tri.csv"
    p.write_text("Date,Total Returns Index\n"
                 "03 Jan 2026,300.0\n01 Jan 2026,100.0\n03 Jan 2026,999.0\n")
    df = B.tri_csv_loader(str(p))
    assert list(df["date"]) == ["2026-01-01", "2026-01-03"]


def test_tri_csv_rejects_a_file_without_the_expected_columns(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("Date,Close\n01 Jan 2026,100.0\n")
    with pytest.raises(ValueError, match="Total Returns Index"):
        B.tri_csv_loader(str(p))


def test_tri_csv_rejects_unquoted_thousands_separators(tmp_path):
    """An unquoted "12,345.67" would silently parse as 12.0 — a 1000x error. Refuse it."""
    p = tmp_path / "unquoted.csv"
    p.write_text("Date,Total Returns Index\n01 Jan 2026,12,345.67\n")
    with pytest.raises(ValueError, match="more fields than the header"):
        B.tri_csv_loader(str(p))


def test_tri_csv_rejects_an_empty_file(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text("Date,Total Returns Index\n")
    with pytest.raises(ValueError, match="no parseable rows"):
        B.tri_csv_loader(str(p))


# =====================================================================================
# persistence: PRI and TRI must coexist
# =====================================================================================
def test_pri_and_tri_occupy_separate_columns(conn):
    pri = pd.Series({pd.Timestamp("2026-01-01"): 20_000.0,
                     pd.Timestamp("2026-01-02"): 20_100.0})
    B.upsert_benchmark(conn, "NIFTY 500", pri, kind=B.PRI)
    tri = pd.DataFrame([{"date": "2026-01-01", "tri": 30_000.0},
                        {"date": "2026-01-02", "tri": 30_200.0}])
    B.upsert_benchmark(conn, "NIFTY 500", tri, kind=B.TRI)

    rows = conn.execute("SELECT date, close, tri FROM benchmark ORDER BY date").fetchall()
    assert [r["close"] for r in rows] == [20_000.0, 20_100.0]
    assert [r["tri"] for r in rows] == [30_000.0, 30_200.0]


def test_loading_tri_does_not_clobber_pri_in_either_order(conn):
    """The two ingestion paths run independently — neither may erase the other."""
    tri = pd.DataFrame([{"date": "2026-01-01", "tri": 30_000.0}])
    B.upsert_benchmark(conn, "X", tri, kind=B.TRI)
    B.upsert_benchmark(conn, "X", pd.Series({pd.Timestamp("2026-01-01"): 20_000.0}),
                       kind=B.PRI)
    row = conn.execute("SELECT close, tri FROM benchmark WHERE index_name='X'").fetchone()
    assert row["close"] == 20_000.0 and row["tri"] == 30_000.0


def test_reingesting_updates_in_place(conn):
    s = pd.Series({pd.Timestamp("2026-01-01"): 20_000.0})
    B.upsert_benchmark(conn, "X", s, kind=B.PRI)
    B.upsert_benchmark(conn, "X", pd.Series({pd.Timestamp("2026-01-01"): 20_500.0}),
                       kind=B.PRI)
    rows = conn.execute("SELECT close FROM benchmark WHERE index_name='X'").fetchall()
    assert len(rows) == 1 and rows[0]["close"] == 20_500.0


def test_upsert_rejects_an_unknown_kind(conn):
    with pytest.raises(ValueError, match="must be PRI or TRI"):
        B.upsert_benchmark(conn, "X", pd.Series(dtype=float), kind="TOTAL")


def test_upsert_accepts_plain_pairs(conn):
    B.upsert_benchmark(conn, "X", [("2026-01-01", 100.0), ("2026-01-02", 101.0)],
                       kind=B.TRI)
    assert conn.execute("SELECT COUNT(*) c FROM benchmark").fetchone()["c"] == 2


# =====================================================================================
# reading back: the series MUST say which one it is
# =====================================================================================
def test_series_prefers_tri_and_labels_it(conn):
    B.upsert_benchmark(conn, "N500", pd.Series({pd.Timestamp("2026-01-01"): 20_000.0,
                                                pd.Timestamp("2026-01-02"): 20_100.0}),
                       kind=B.PRI)
    B.upsert_benchmark(conn, "N500", pd.DataFrame(
        [{"date": "2026-01-01", "tri": 30_000.0}, {"date": "2026-01-02", "tri": 30_300.0}]),
        kind=B.TRI)

    s = B.benchmark_series(conn, "N500")
    assert s.attrs["series_type"] == B.TRI
    assert s.iloc[0] == 30_000.0
    # TRI must show the higher return — dividends are reinvested
    assert s.iloc[-1] / s.iloc[0] > 20_100.0 / 20_000.0


def test_series_falls_back_to_pri_when_no_tri_exists(conn):
    B.upsert_benchmark(conn, "N500", pd.Series({pd.Timestamp("2026-01-01"): 20_000.0}),
                       kind=B.PRI)
    s = B.benchmark_series(conn, "N500")
    assert s.attrs["series_type"] == B.PRI
    assert s.iloc[0] == 20_000.0


def test_series_can_be_forced_to_pri(conn):
    B.upsert_benchmark(conn, "N500", pd.Series({pd.Timestamp("2026-01-01"): 20_000.0}),
                       kind=B.PRI)
    B.upsert_benchmark(conn, "N500", pd.DataFrame([{"date": "2026-01-01", "tri": 30_000.0}]),
                       kind=B.TRI)
    assert B.benchmark_series(conn, "N500", prefer=B.PRI).iloc[0] == 20_000.0


def test_unknown_index_returns_an_empty_series(conn):
    assert B.benchmark_series(conn, "NOPE").empty


def test_available_benchmarks_summarises_coverage(conn):
    B.upsert_benchmark(conn, "A", pd.Series({pd.Timestamp("2026-01-01"): 1.0,
                                             pd.Timestamp("2026-01-02"): 2.0}), kind=B.PRI)
    B.upsert_benchmark(conn, "B", pd.DataFrame([{"date": "2026-01-01", "tri": 5.0}]),
                       kind=B.TRI)
    out = B.available_benchmarks(conn).set_index("index_name")
    assert out.loc["A", "pri"] == 2 and out.loc["A", "tri"] == 0
    assert out.loc["B", "tri"] == 1 and out.loc["B", "pri"] == 0
    assert out.loc["A", "first"] == "2026-01-01" and out.loc["A", "last"] == "2026-01-02"


def test_common_index_aliases_match_the_live_dump_strings():
    """Verified against Kite's instrument dump on 2026-08-14. The momentum index has NO
    spaces, unlike every other Nifty symbol — a plausible-looking 'fix' would break it."""
    assert B.COMMON_INDICES["nifty500"] == "NIFTY 500"
    assert B.COMMON_INDICES["momentum30"] == "NIFTY200MOMENTM30"
    assert " " not in B.COMMON_INDICES["momentum30"]
