"""The holdings table.

The rule the module is built on: every figure comes from Kite, the fills table, the
uploaded scan or the GTT book, and anything that cannot be sourced is ABSENT rather than
approximated. On a page about money an invented number is worse than a missing one, and a
sector guessed from a symbol would turn a concentration warning into fiction.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.analytics import holdings_view as H


def held(sym, qty, avg, ltp, pledged=0):
    return {"symbol": sym, "quantity": qty, "average_price": avg, "last_price": ltp,
            "pledged_qty": pledged, "exchange": "NSE"}


def kite_row(sym, day_change=0.0, day_pct=0.0, t1=0):
    return {"tradingsymbol": sym, "day_change": day_change,
            "day_change_percentage": day_pct, "t1_quantity": t1, "isin": "IN" + sym}


# =====================================================================================
# the arithmetic
# =====================================================================================
def test_a_row_carries_cost_value_and_the_gap_between_them():
    r = H.rows([held("AAA", 100, 90.0, 100.0)], raw=[kite_row("AAA")])[0]
    assert r["invested"] == 9000 and r["value"] == 10000
    assert r["pnl"] == 1000 and r["pnl_pct"] == pytest.approx(11.11, abs=0.01)


def test_day_pnl_uses_kites_own_day_change_not_a_stored_close():
    """Deriving it from yesterday's stored close drifts across a holiday; Kite's figure is
    against the actual previous close and is what the broker app shows."""
    r = H.rows([held("AAA", 100, 90.0, 100.0)],
               raw=[kite_row("AAA", day_change=-2.5, day_pct=-2.44)])[0]
    assert r["day_pnl"] == -250 and r["day_change_pct"] == -2.44


def test_weights_are_of_the_equity_book_and_sum_to_a_hundred():
    rs = H.rows([held("AAA", 100, 10, 10), held("BBB", 100, 10, 30)],
                raw=[kite_row("AAA"), kite_row("BBB")])
    assert sum(r["weight_pct"] for r in rs) == pytest.approx(100.0, abs=0.05)
    assert rs[0]["symbol"] == "BBB", "rows lead with the largest position"


def test_a_missing_day_figure_is_absent_not_zero():
    """Zero would read as "flat today", which is a different statement from "not known"."""
    r = H.rows([held("AAA", 10, 10, 10)], raw=[{"tradingsymbol": "AAA"}])[0]
    assert r["day_pnl"] is None and r["day_change_pct"] is None


# =====================================================================================
# context that has to come from somewhere real
# =====================================================================================
def test_a_name_absent_from_the_scan_is_unclassified_not_guessed():
    r = H.rows([held("AAA", 10, 10, 10)], raw=[kite_row("AAA")], scan={})[0]
    assert r["cap_band"] is None and r["marketcap_cr"] is None and r["beta"] is None


@pytest.mark.parametrize("cr,band", [(150_000, "largecap"), (40_000, "midcap"),
                                     (5_000, "smallcap")])
def test_bands_match_the_ones_the_planner_uses(cr, band):
    r = H.rows([held("AAA", 10, 10, 10)], raw=[kite_row("AAA")],
               scan={"AAA": {"marketcap": cr}})[0]
    assert r["cap_band"] == band


def test_holding_period_comes_from_the_fill_history():
    old = dt.datetime.now() - dt.timedelta(days=400)
    fills = [{"symbol": "AAA", "side": "BUY", "when_ts": old.timestamp()}]
    r = H.rows([held("AAA", 10, 10, 10)], raw=[kite_row("AAA")], fills=fills)[0]
    assert r["term"] == "long" and r["held_days"] >= 400


def test_a_recent_buy_is_short_term():
    recent = dt.datetime.now() - dt.timedelta(days=30)
    fills = [{"symbol": "AAA", "side": "BUY", "when_ts": recent.timestamp()}]
    r = H.rows([held("AAA", 10, 10, 10)], raw=[kite_row("AAA")], fills=fills)[0]
    assert r["term"] == "short"


def test_no_fill_history_means_no_claim_about_the_holding_period():
    r = H.rows([held("AAA", 10, 10, 10)], raw=[kite_row("AAA")], fills=[])[0]
    assert "term" not in r and "held_days" not in r


# =====================================================================================
# warnings that can be established
# =====================================================================================
def test_a_position_over_the_cap_is_flagged():
    rs = H.rows([held("AAA", 100, 10, 10), held("BBB", 10, 10, 10)],
                raw=[kite_row("AAA"), kite_row("BBB")])
    w = H.warnings(rs, H.summary(rs))
    assert any(x["symbol"] == "AAA" and "cap" in x["text"] for x in w)


def test_an_untouchable_instrument_is_not_measured_against_the_cap():
    """An SGB is held on purpose and outside the strategy's sizing. Measuring it against a
    cap the planner will never apply to it is noise, and noise is what makes the real
    warnings get skipped."""
    rs = H.rows([held("SGBDE31III-GB", 100, 10, 10), held("BBB", 1, 10, 10)],
                raw=[kite_row("SGBDE31III-GB"), kite_row("BBB")])
    assert rs[0]["weight_pct"] > 90
    assert not [w for w in H.warnings(rs, H.summary(rs)) if w["symbol"]]


def test_positions_without_full_stop_cover_are_named():
    rs = H.rows([held("AAA", 10, 10, 10)], raw=[kite_row("AAA")],
                protected={"AAA": "missing"})
    w = H.warnings(rs, H.summary(rs))
    assert any("without full stop cover" in x["text"] for x in w)


def test_the_summary_reports_concentration_and_weighted_beta():
    rs = H.rows([held("AAA", 100, 10, 10), held("BBB", 100, 10, 30)],
                raw=[kite_row("AAA"), kite_row("BBB")],
                scan={"AAA": {"beta": 1.0}, "BBB": {"beta": 2.0}})
    s = H.summary(rs, cash=1000.0)
    assert s["top5_weight"] == pytest.approx(100.0, abs=0.05)
    assert s["largest"] == "BBB"
    assert s["weighted_beta"] == pytest.approx(1.75, abs=0.01)
    assert s["cash"] == 1000


# =====================================================================================
# the gaps are stated, not hidden
# =====================================================================================
def test_every_gap_says_why_it_cannot_be_shown():
    assert H.GAPS
    for name, why in H.GAPS:
        assert name and len(why) > 20, f"{name} has no reason"
    named = {n for n, _ in H.GAPS}
    assert "Sector allocation" in named and "XIRR" in named
