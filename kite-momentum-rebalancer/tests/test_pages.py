"""Performance and tradebook pages: reachability, honesty about thin data, signalling."""
from __future__ import annotations

import json
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from app import config as C
from app import main as M
from app.analytics import db
from app.analytics import performance_view as PV
from app.analytics import tradebook as TB


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    path = str(tmp_path / "p.db")
    monkeypatch.setattr(C, "DB_PATH", path)
    with db.connect(path) as c:
        db.migrate(c)
        yield c


def snap(conn, date, nav, invested, cash, positions=None):
    db.save_snapshot(conn, {"date": date, "nav": nav, "invested": invested, "cash": cash,
                            "holdings_json": json.dumps({"positions": positions or []})})


# =====================================================================================
# performance: thin history must never be presented as evidence
# =====================================================================================
def test_no_snapshots_gives_an_empty_state_with_the_command(conn):
    v = PV.build(conn)
    assert v["available"] is False and "snapshot" in v["how_to"]


def test_metrics_carry_their_sample_size(conn):
    for i, nav in enumerate([1_000_000, 1_010_000, 1_005_000, 1_030_000], start=1):
        snap(conn, f"2026-06-0{i}", nav, nav, 0.0)
    v = PV.build(conn)
    assert v["observations"] == 4
    for m in v["metrics"]:
        assert "flag" in m and {"n", "need", "meaningful"} <= set(m["flag"])


def test_ratios_are_marked_not_meaningful_on_a_short_record(conn):
    for i, nav in enumerate([1_000_000, 1_010_000, 1_005_000, 1_030_000], start=1):
        snap(conn, f"2026-06-0{i}", nav, nav, 0.0)
    by = {m["label"]: m for m in PV.build(conn)["metrics"]}
    assert by["Sharpe"]["flag"]["meaningful"] is False
    assert by["Volatility"]["flag"]["meaningful"] is False
    assert by["CAGR"]["flag"]["meaningful"] is False


def test_a_long_record_marks_ratios_meaningful(conn):
    import datetime as dt
    d = dt.date(2025, 1, 1)
    nav = 1_000_000.0
    for i in range(80):
        nav *= 1.001 if i % 3 else 0.999
        snap(conn, (d + dt.timedelta(days=i)).isoformat(), nav, nav, 0.0)
    by = {m["label"]: m for m in PV.build(conn)["metrics"]}
    assert by["Sharpe"]["flag"]["meaningful"] is True
    assert by["Volatility"]["flag"]["meaningful"] is True


def test_metric_polarity_never_paints_a_loss_as_a_gain(conn):
    """Volatility and VaR rising is not good news; green would assert the opposite."""
    for i, nav in enumerate([1_000_000, 1_010_000, 1_005_000, 1_030_000], start=1):
        snap(conn, f"2026-06-0{i}", nav, nav, 0.0)
    by = {m["label"]: m for m in PV.build(conn)["metrics"]}
    assert by["Volatility"]["polarity"] == "down"
    assert by["VaR 95%"]["polarity"] == "loss"
    assert by["Max drawdown"]["polarity"] == "loss"
    assert by["Total return"]["polarity"] == "up"
    assert by["Sharpe"]["polarity"] == "up"


def test_index_is_time_weighted_so_a_deposit_is_not_a_gain(conn):
    snap(conn, "2026-06-01", 1_000_000, 1_000_000, 0.0)
    db.record_cashflow(conn, "2026-06-02", -500_000.0, "invest")
    snap(conn, "2026-06-02", 1_500_000, 1_500_000, 0.0)
    v = PV.build(conn)
    total = next(m for m in v["metrics"] if m["label"] == "Total return")
    assert abs(total["value"]) < 1e-6, "a deposit registered as performance"


def test_xirr_appears_only_with_recorded_cashflows(conn):
    snap(conn, "2026-06-01", 1_000_000, 1_000_000, 0.0)
    snap(conn, "2026-06-02", 1_100_000, 1_100_000, 0.0)
    assert next(m for m in PV.build(conn)["metrics"]
                if m["label"] == "XIRR")["value"] is None

    db.record_cashflow(conn, "2026-06-01", -1_000_000.0, "invest")
    v = PV.build(conn)
    assert v["has_cashflows"] is True


def test_benchmark_series_type_is_recorded(conn):
    from app.analytics import benchmark as B
    import pandas as pd
    snap(conn, "2026-06-01", 1_000_000, 1_000_000, 0.0)
    snap(conn, "2026-06-02", 1_010_000, 1_010_000, 0.0)
    B.upsert_benchmark(conn, "NIFTY 500",
                       pd.Series({pd.Timestamp("2026-06-01"): 20_000.0,
                                  pd.Timestamp("2026-06-02"): 20_100.0}), kind=B.PRI)
    v = PV.build(conn)
    assert v["series_used"]["NIFTY 500"] == "PRI"
    assert any(s["type"] == "PRI" for s in v["chart"]["series"])


def test_chart_points_are_server_rendered(conn):
    for i, nav in enumerate([1_000_000, 1_010_000, 1_005_000], start=1):
        snap(conn, f"2026-06-0{i}", nav, nav, 0.0)
    pts = PV.build(conn)["chart"]["series"][0]["points"]
    assert pts and "," in pts and len(pts.split()) == 3


def test_trades_section_is_absent_without_closed_trades(conn):
    snap(conn, "2026-06-01", 1_000_000, 1_000_000, 0.0)
    assert PV.build(conn)["trades"] is None


# =====================================================================================
# tradebook status
# =====================================================================================
def test_status_reports_no_lots_cleanly(conn):
    st = TB.status(conn, [{"symbol": "X", "quantity": 10, "value": 100.0,
                           "average_price": 10.0}])
    assert st["has_lots"] is False
    assert st["coverage"]["coverage_pct"] == 0.0
    assert st["detail"][0]["status"] == "missing"


def test_status_reconciles_and_flags_a_mismatch(conn, tmp_path):
    csv = tmp_path / "tb.csv"
    csv.write_text("symbol,trade_date,trade_type,quantity,price\n"
                   "X,2025-01-01,buy,100,50\n")
    TB.import_tradebook(conn, str(csv))
    ok = TB.status(conn, [{"symbol": "X", "quantity": 100, "value": 10_000.0,
                           "average_price": 50.0}])
    assert ok["detail"][0]["status"] == "ok" and ok["problems"] == []
    assert ok["coverage"]["coverage_pct"] == 100.0

    bonus = TB.status(conn, [{"symbol": "X", "quantity": 200, "value": 20_000.0,
                              "average_price": 25.0}])
    assert bonus["detail"][0]["status"] == "mismatch"
    assert "corporate action" in bonus["problems"][0]["likely_cause"]


def test_status_surfaces_lot_average_against_broker_average(conn, tmp_path):
    csv = tmp_path / "tb.csv"
    csv.write_text("symbol,trade_date,trade_type,quantity,price\n"
                   "X,2025-01-01,buy,100,50\n")
    TB.import_tradebook(conn, str(csv))
    d = TB.status(conn, [{"symbol": "X", "quantity": 100, "value": 10_000.0,
                          "average_price": 50.0}])["detail"][0]
    assert d["avg_cost"] == pytest.approx(50.0)
    assert d["broker_avg"] == pytest.approx(50.0)


# =====================================================================================
# the pages
# =====================================================================================
@pytest.fixture()
def client(monkeypatch, conn):
    monkeypatch.setattr(M, "_kite", None)
    return TestClient(M.app)


@pytest.mark.parametrize("path", ["/performance", "/tradebook",
                                  "/performance/data", "/tradebook/data"])
def test_pages_render(client, path):
    assert client.get(path).status_code == 200


def test_performance_page_states_the_short_history_plainly(client, conn):
    for i, nav in enumerate([1_000_000, 1_010_000, 1_005_000], start=1):
        snap(conn, f"2026-06-0{i}", nav, nav, 0.0)
    text = client.get("/performance").text
    assert "short history" in text.lower()
    assert "sessions needed" in text.lower()


def test_tradebook_page_explains_the_export_route(client):
    text = client.get("/tradebook").text
    assert "console.zerodha.com" in text.lower()
    assert "TAX_DATA_UNKNOWN" in text


def test_every_page_links_to_every_other_page(client):
    """Checked on the RENDERED page, not the template source. The nav moved into the
    shared shell when the UI was rebuilt, so every page inherits it rather than repeating
    it — reading each file for links would now pass or fail for the wrong reason."""
    import re
    routes = ["/", "/performance", "/regime", "/regime/backtest", "/tradebook",
              "/settings"]
    for page in routes:
        html = client.get(page).text
        links = set(re.findall(r'href="([^"]+)"', html))
        missing = [r for r in routes if r not in links]
        assert missing == [], f"{page} does not link to {missing}"


def test_the_nav_absorbs_the_shrink_and_never_wraps_to_a_stray_line():
    """The badge used to be pushed onto its own line when the nav grew, reading as a
    stray sentence rather than as status. The three flex rules that fixed it lived in
    every page's own stylesheet; the shell now owns the bar, so the guarantee is asserted
    once, where it is implemented."""
    shell = pathlib.Path("app/templates/base.html").read_text()
    assert 'class="sc-topbar__in"' in shell and "sc-nav" in shell

    # A flex item defaults to min-width:auto and so refuses to shrink below its content.
    # Without min-width:0 the nav widens the bar, the bar widens the page, and every page
    # scrolls sideways on a phone. This is the rule that stops it.
    css = pathlib.Path("app/static/app.css").read_text().replace(" ", "").replace("\n", "")
    import re as _re
    nav = _re.search(r"\.sc-nav\{([^}]*)\}", css)
    assert nav, "the nav rule is gone"
    assert "min-width:0" in nav.group(1) or "min-width:0px" in nav.group(1), \
        "the nav can no longer shrink; the page will scroll sideways"
    assert "overflow-x:auto" in nav.group(1), "the nav must scroll rather than wrap"


def test_regime_tables_scroll_at_every_width_not_just_on_a_phone():
    """The rule was gated behind max-width:780px on the assumption that anything wider
    fits. The exposure table still overflowed the page at 900px: whether a dense table
    fits depends on its content, not on the viewport.

    Now asserted against the BUILT stylesheet, which is where the rule lives since the
    per-page style blocks were consolidated. The breakpoint check is the point: the rule
    must not sit inside an @media query."""
    css = pathlib.Path("app/static/app.css").read_text().replace(" ", "").replace("\n", "")
    rules = re.findall(r"([^{}]*table[^{}]*)\{([^}]*)\}", css)
    scrollers = [sel for sel, body in rules
                 if "overflow-x:auto" in body and "display:block" in body]
    assert scrollers, "the table scroll rule is gone from the stylesheet"

    # and it is not gated behind a breakpoint: every @media opened before it must have
    # closed again.
    head = css[:css.index(scrollers[0])]
    assert "@media" not in head or head.rfind("}") > head.rfind("@media"), \
        "the table scroll rule is gated behind a breakpoint again"


def test_the_options_pages_are_absent_while_the_lab_is_frozen(client):
    """They were in the page sweep above until M6. Removing a route from a sweep without
    saying why is how coverage quietly disappears, so the sweep lost them and this took
    their place: while the subsystem is frozen the routes must 404, not 500 and not render.
    """
    from app import config as C

    if C.OPTIONS_ENABLED:
        pytest.skip("the options lab is enabled; the page sweep covers these")
    for path in ("/options", "/options/data"):
        assert client.get(path).status_code == 404
