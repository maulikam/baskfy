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


def test_pages_render_without_network(monkeypatch):
    monkeypatch.setattr(NSE, "board", lambda force=False: {
        "rows": [NSE._board_row(BOARD_ROW)], "fetched_at": "21 Aug 2026 17:00",
        "stale": False, "error": None})
    monkeypatch.setattr(M, "_kite", None)
    c = TestClient(M.app)

    page = c.get("/indices")
    assert page.status_code == 200
    assert "NIFTY 50" in page.text

    data = c.get("/indices/data")
    assert data.status_code == 200
    assert json.loads(data.text)["rows"][0]["name"] == "NIFTY 50"


def test_the_board_page_survives_nse_being_unreachable(monkeypatch):
    monkeypatch.setattr(NSE, "board", lambda force=False: {
        "rows": [], "fetched_at": None, "stale": True, "error": "connection refused"})
    monkeypatch.setattr(M, "_kite", None)
    r = TestClient(M.app).get("/indices")
    assert r.status_code == 200
    assert "No index data" in r.text
