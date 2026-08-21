"""The index board: it must stay up when NSE does not, and must not overstate breadth.

Every test here stubs the network. A test that reaches nseindia.com would be slow, would
fail on a plane, and — worse — would be the exact burst of archive requests that got a
development IP blocked while this feature was being built.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import main as M
from app.analytics import index_view as IV
from app.analytics import nse_indices as NSE


BOARD_ROW = {
    "key": "BROAD MARKET INDICES", "index": "NIFTY 50", "indexSymbol": "NIFTY 50",
    "last": 24252.0, "variation": 20.15, "percentChange": 0.08,
    "open": 24284.05, "high": 24284.05, "low": 24206.8, "previousClose": 24231.85,
    "yearHigh": 26373.2, "yearLow": 22182.55, "pe": "20.5", "pb": "2.94", "dy": "1.16",
    "advances": "25", "declines": "24", "unchanged": "1",
    "perChange365d": -3.32, "perChange30d": 0.27,
}


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Point every cache at a temp dir and reset the module's in-process state."""
    monkeypatch.setattr(NSE, "BOARD_CACHE", str(tmp_path / "board.json"))
    monkeypatch.setattr(NSE, "CONSTITUENT_CACHE", str(tmp_path / "cons.json"))
    monkeypatch.setattr(NSE, "CLOSE_CACHE", str(tmp_path / "close.json"))
    monkeypatch.setattr(NSE, "MIN_ARCHIVE_GAP", 0.0)
    monkeypatch.setattr(NSE, "_blocked_until", 0.0)
    monkeypatch.setattr(NSE, "_session", None)
    yield


# =====================================================================================
# staying up when NSE is down
# =====================================================================================
def test_board_serves_the_last_good_copy_when_nse_fails(monkeypatch):
    calls = {"n": 0}

    class R:
        status_code = 200

        def raise_for_status(self): pass

        def json(self): return {"data": [BOARD_ROW]}

    def ok(*a, **k):
        calls["n"] += 1
        return R()

    monkeypatch.setattr(NSE, "_session_get", lambda: type("S", (), {"get": staticmethod(ok)})())
    first = NSE.board()
    assert first["rows"] and first["stale"] is False

    def boom(*a, **k):
        raise RuntimeError("nse down")

    monkeypatch.setattr(NSE, "_session_get", lambda: type("S", (), {"get": staticmethod(boom)})())
    monkeypatch.setattr(NSE, "BOARD_TTL", -1)          # force a refetch
    second = NSE.board()
    assert second["stale"] is True
    assert second["rows"][0]["name"] == "NIFTY 50"     # the old board, not an empty page
    assert second["error"]


def test_board_with_nothing_cached_returns_empty_rather_than_raising(monkeypatch):
    monkeypatch.setattr(NSE, "_session_get",
                        lambda: type("S", (), {"get": staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))})())
    b = NSE.board()
    assert b["rows"] == [] and b["stale"] is True


def test_constituents_never_raise(monkeypatch):
    monkeypatch.setattr(NSE, "_fetch_constituents",
                        lambda name: (_ for _ in ()).throw(RuntimeError("archive 403")))
    c = NSE.constituents("NIFTY MEDIA")
    assert c["rows"] == [] and "403" in c["error"]


def test_a_403_stops_the_module_asking_again(monkeypatch):
    """A ban is host-wide, so continuing to poll turns a short block into a long one."""
    seen = {"n": 0}

    class R:
        status_code = 403
        text = ""

    def get(*a, **k):
        seen["n"] += 1
        return R()

    monkeypatch.setattr(NSE.requests, "get", get)
    with pytest.raises(RuntimeError, match="403"):
        NSE._fetch_constituents("NIFTY MEDIA")
    n_after_first = seen["n"]

    # A second index must not produce another request while the backoff holds.
    with pytest.raises(RuntimeError, match="backing off"):
        NSE._fetch_constituents("NIFTY BANK")
    assert seen["n"] == n_after_first


# =====================================================================================
# honesty about what the breadth numbers cover
# =====================================================================================
def test_unscored_constituents_are_excluded_not_counted_as_failing():
    """The denominator is the scanned subset. Counting an unknown name as "below its
    200 DMA" would report a thin overlap as a weak sector, which is a different claim."""
    scored = [
        {"above_200dma": True, "above_50dma": True, "away_ath": -5.0, "ret_1y": 20.0},
        {"above_200dma": False, "above_50dma": True, "away_ath": -40.0, "ret_1y": -3.0},
    ]
    b = IV._breadth(scored)
    assert b["n"] == 2
    assert b["above_200dma"] == 50.0      # not 25% against a 4-name index
    assert b["near_ath"] == 50.0
    assert b["positive_1y"] == 50.0


def test_breadth_over_no_scored_names_reports_nothing_rather_than_zero():
    assert IV._breadth([]) == {"n": 0}


def test_drilldown_marks_which_names_the_scan_covers(monkeypatch):
    monkeypatch.setattr(NSE, "constituents", lambda name: {
        "rows": [{"symbol": "AAA", "company": "A Ltd", "industry": "Power", "series": "EQ", "isin": ""},
                 {"symbol": "ZZZ", "company": "Z Ltd", "industry": "Power", "series": "EQ", "isin": ""}],
        "fetched_at": "x", "stale": False, "error": None})
    monkeypatch.setattr(IV, "scan_scores", lambda: {"by_symbol": {
        "AAA": {"score": 71.0, "rank": 3, "reject": None, "close": 100.0, "ma_50": 90.0,
                "ma_200": 80.0, "rsi": 60.0, "ret_1y": 25.0, "ret_3m": 5.0, "ret_1m": 1.0,
                "away_ath": -4.0, "vol_1y": 0.3, "beta": 1.1, "trend": 20.0, "momentum": 25.0}},
        "as_of": "2026-08-18", "path": "scan.csv", "count": 1})

    d = IV.drilldown("NIFTY POWER", kite=None)
    assert d["count"] == 2 and d["scored_count"] == 1
    assert d["rows"][0]["symbol"] == "AAA"           # scored names sort first
    assert d["rows"][1]["score"] is None             # and an unknown stays blank, not 0
    assert d["breadth"]["n"] == 1


def test_drilldown_survives_a_dead_broker_session(monkeypatch):
    monkeypatch.setattr(NSE, "constituents", lambda name: {
        "rows": [{"symbol": "AAA", "company": "A", "industry": "", "series": "EQ", "isin": ""}],
        "fetched_at": "x", "stale": False, "error": None})
    monkeypatch.setattr(IV, "scan_scores",
                        lambda: {"by_symbol": {}, "as_of": None, "path": None, "count": 0})

    class DeadKite:
        def quotes(self, symbols, exchange="NSE"):
            raise RuntimeError("Token is invalid or has expired")

    d = IV.drilldown("NIFTY POWER", kite=DeadKite())
    assert d["count"] == 1
    assert "expired" in d["quote_error"]
    assert d["rows"][0]["last"] is None              # blank price, working panel


# =====================================================================================
# classification and routing
# =====================================================================================
@pytest.mark.parametrize("name,fam", [
    ("NIFTY 50", "Broad"),
    ("NIFTY BANK", "Sector"),
    ("NIFTY500 MOMENTUM 50", "Factor"),
    ("NIFTY INDIA DEFENCE", "Theme"),
    ("INDIA VIX", "Derived"),
    ("NIFTY50 TR 2X LEVERAGE", "Derived"),
])
def test_families(name, fam):
    assert IV.family(name) == fam


def test_formula_indices_offer_no_constituent_drilldown():
    """India VIX is not an index OF anything; a leveraged series has no basket. Offering
    the control would guarantee a failed fetch and, worse, spend an archive request."""
    assert IV._drillable("INDIA VIX") is False
    assert IV._drillable("NIFTY50 PR 1X INVERSE") is False
    assert IV._drillable("NIFTY BANK") is True


def test_slug_strips_every_separator_nse_drops():
    assert NSE.slug("NIFTY OIL & GAS") == "niftyoilgas"
    assert NSE.slug("NIFTY MIDSMALLCAP400 50:50") == "niftymidsmallcap4005050"


def test_camel_keeps_acronyms_whole():
    """ind_niftyNBFC_list.csv is the real filename — "Nbfc" would 404."""
    assert NSE.camel("Nifty Sugar & Ethanol") == "niftySugarEthanol"
    assert NSE.camel("Nifty NBFC") == "niftyNBFC"
    assert NSE.camel("Nifty Hospitals") == "niftyHospitals"
    assert NSE.camel("Nifty India Defence") == "niftyIndiaDefence"
    # A full-caps name from the live feed must not become niftyPRIVATEBANK.
    assert NSE.camel("NIFTY PRIVATE BANK") == "niftyPrivateBank"
    assert NSE.camel("NIFTY IT") == "niftyIT"


def test_candidates_cover_both_hosts_and_both_spellings():
    c = NSE._archive_candidates("Nifty Hospitals")
    assert any(u.endswith("/ind_niftyhospitalslist.csv") for u in c)
    assert any(u.endswith("/ind_niftyHospitals_list.csv") for u in c)
    assert any("nsearchives" in u for u in c) and any("niftyindices" in u for u in c)


def test_a_wrong_name_answering_200_with_html_is_not_treated_as_an_empty_index(monkeypatch):
    """niftyindices.com soft-404s: it returns 200 and an HTML page. Trusting the status
    would cache "this index has no constituents" for a month."""
    class HTML:
        status_code = 200
        text = "<!DOCTYPE html><html><head><title>Not found</title></head></html>"

    monkeypatch.setattr(NSE.requests, "get", lambda *a, **k: HTML())
    with pytest.raises(RuntimeError, match="no constituent list"):
        NSE._fetch_constituents("Nifty Nonexistent")


def test_an_unresolvable_index_is_not_re_probed_on_every_open(monkeypatch):
    calls = {"n": 0}

    def fetch(name):
        calls["n"] += 1
        raise RuntimeError("no constituent list for 'X' (ind_x.csv -> HTTP 404)")

    monkeypatch.setattr(NSE, "_fetch_constituents", fetch)
    NSE.constituents("Nifty Nonexistent")
    NSE.constituents("Nifty Nonexistent")
    assert calls["n"] == 1


def test_being_rate_limited_is_never_cached_as_no_such_index(monkeypatch):
    """A 403 says nothing about whether the index has a list. Remembering it as "absent"
    would blank a real index for hours after a transient block."""
    calls = {"n": 0}

    def fetch(name):
        calls["n"] += 1
        raise RuntimeError("NSE archive returned 403 — backing off for an hour")

    monkeypatch.setattr(NSE, "_fetch_constituents", fetch)
    NSE.constituents("Nifty Media")
    NSE.constituents("Nifty Media")
    assert calls["n"] == 2


def test_pages_render_without_network(monkeypatch):
    monkeypatch.setattr(NSE, "full_board", lambda: {
        "rows": [{**NSE._board_row(BOARD_ROW), "volume": None, "turnover": None,
                  "as_of": None, "source": "live"}],
        "fetched_at": "21 Aug 2026 17:00", "live_count": 1, "eod_only": 0,
        "close_as_of": "2026-08-21", "close_stale": False, "stale": False, "error": None})
    monkeypatch.setattr(M, "_kite", None)
    c = TestClient(M.app)

    page = c.get("/indices")
    assert page.status_code == 200
    assert "NIFTY 50" in page.text

    data = c.get("/indices/data")
    assert data.status_code == 200
    assert json.loads(data.text)["rows"][0]["name"] == "NIFTY 50"


def test_the_board_page_survives_nse_being_unreachable(monkeypatch):
    monkeypatch.setattr(NSE, "full_board", lambda: {
        "rows": [], "fetched_at": None, "live_count": 0, "eod_only": 0,
        "close_as_of": None, "close_stale": True, "stale": True,
        "error": "connection refused"})
    monkeypatch.setattr(M, "_kite", None)
    r = TestClient(M.app).get("/indices")
    assert r.status_code == 200
    assert "No index data" in r.text


# =====================================================================================
# the merge: the live feed alone silently drops 21 equity indices
# =====================================================================================
CLOSE_CSV = (
    "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
    "Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\n"
    "Nifty 50,20-08-2026,24225.45,24265.15,24184.55,24231.85,153.55,0.64,253928495,18777.61,20.48,2.94,1.17\n"
    "Nifty Sugar & Ethanol,20-08-2026,-,-,-,2864.73,154.78,5.71,405949718,3965.22,28.98,2.59,.42\n"
)


def test_the_close_report_parses_nses_dashes_and_bare_decimals():
    rows = {r["name"]: r for r in NSE._parse_close(CLOSE_CSV)}
    sugar = rows["Nifty Sugar & Ethanol"]
    assert sugar["open"] is None                 # "-" is absent, not zero
    assert sugar["last"] == 2864.73
    assert sugar["pct"] == 5.71
    assert sugar["dy"] == 0.42                   # ".42", not 42
    assert sugar["turnover"] == 3965.22


def test_full_board_keeps_indices_the_live_feed_does_not_quote(monkeypatch):
    """The regression this exists to prevent: allIndices carries 139 of 164 indices, so a
    board-only page loses Sugar & Ethanol, NBFC, Insurance, Power and 17 others outright."""
    monkeypatch.setattr(NSE, "close_report", lambda today=None: {
        "rows": NSE._parse_close(CLOSE_CSV), "as_of": "2026-08-20",
        "fetched_at": "x", "stale": False, "error": None})
    monkeypatch.setattr(NSE, "board", lambda force=False: {
        "rows": [NSE._board_row(BOARD_ROW)], "fetched_at": "y", "stale": False, "error": None})

    fb = NSE.full_board()
    by = {r["name"]: r for r in fb["rows"]}
    assert set(by) == {"Nifty 50", "Nifty Sugar & Ethanol"}
    assert fb["live_count"] == 1 and fb["eod_only"] == 1

    # The live row wins on level, but keeps what only the report publishes.
    assert by["Nifty 50"]["source"] == "live"
    assert by["Nifty 50"]["last"] == 24252.0          # live, not the report's 24231.85
    assert by["Nifty 50"]["turnover"] == 18777.61     # report-only field, carried over
    assert by["Nifty 50"]["advances"] == 25           # live-only field, kept

    # The report-only row is labelled, so its move cannot read as today's.
    sugar = by["Nifty Sugar & Ethanol"]
    assert sugar["source"] == "eod"
    assert sugar["as_of"] == "20-08-2026"
    assert sugar["pct"] == 5.71
    assert sugar["advances"] is None                  # not invented


def test_close_report_walks_back_over_a_weekend(monkeypatch):
    """Today's file does not exist until after the close, and never on a Sunday."""
    import datetime as dt
    asked: list[dt.date] = []

    def fetch(d):
        asked.append(d)
        return NSE._parse_close(CLOSE_CSV) if d == dt.date(2026, 8, 21) else []

    monkeypatch.setattr(NSE, "_fetch_close", fetch)
    rep = NSE.close_report(today=dt.date(2026, 8, 23))   # a Sunday
    assert rep["rows"] and rep["stale"] is True
    assert dt.date(2026, 8, 22) not in asked            # Saturday never requested
    assert dt.date(2026, 8, 23) not in asked            # nor the Sunday itself
    assert dt.date(2026, 8, 21) in asked


def test_a_past_days_report_is_cached_and_never_refetched(monkeypatch):
    import datetime as dt
    calls = {"n": 0}

    def fetch(d):
        calls["n"] += 1
        return NSE._parse_close(CLOSE_CSV)

    monkeypatch.setattr(NSE, "_fetch_close", fetch)
    # today = the 22nd, so the 21st's report is finished and immutable.
    NSE.close_report(today=dt.date(2026, 8, 22))
    NSE.close_report(today=dt.date(2026, 8, 22))
    assert calls["n"] == 1


def test_todays_report_is_re_read_because_nse_is_still_writing_it(monkeypatch):
    """The file published just after the close held 148 indices; the finished one held 164.
    Caching the first copy for the day would keep 16 indices off the page until tomorrow."""
    import datetime as dt
    calls = {"n": 0}

    def fetch(d):
        calls["n"] += 1
        return NSE._parse_close(CLOSE_CSV)

    monkeypatch.setattr(NSE, "_fetch_close", fetch)
    day = dt.date(2026, 8, 21)
    NSE.close_report(today=day)
    monkeypatch.setattr(NSE, "CLOSE_TODAY_TTL", -1)      # pretend the window elapsed
    NSE.close_report(today=day)
    assert calls["n"] == 2


def test_a_failure_to_refresh_today_keeps_the_copy_already_held(monkeypatch):
    import datetime as dt
    monkeypatch.setattr(NSE, "_fetch_close", lambda d: NSE._parse_close(CLOSE_CSV))
    day = dt.date(2026, 8, 21)
    first = NSE.close_report(today=day)
    assert first["rows"]

    monkeypatch.setattr(NSE, "CLOSE_TODAY_TTL", -1)
    monkeypatch.setattr(NSE, "_fetch_close",
                        lambda d: (_ for _ in ()).throw(RuntimeError("403")))
    again = NSE.close_report(today=day)
    assert len(again["rows"]) == len(first["rows"])       # not blanked
    assert again["stale"] is True and "403" in again["error"]
