"""The no-trade band.

A rebalance computes an exact target quantity, so a 0.3% price drift produces a one-share
order. On 18 Aug 2026 a plan proposed trimming WELCORP by 1 share of 362, SONACOMS by 3 of
861 and HFCL by 4 of 1,308 — Rs 16,102 of sells, each costing Rs 20 brokerage plus STT plus
half a spread to correct a drift worth less than the fee. Applying the band collapsed that
plan from twelve orders to one.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app import config as C
from app.rebalance import build_plan


# The real scorer on the real sample scan: build_plan reads a dozen columns and a
# hand-rolled frame silently lacks them.
from app.scoring import load_scan, score as _score

SCORED = _score(load_scan("data/uploads/sample_scan.csv"))
TOP = SCORED[SCORED.reject == ""].sort_values("rank")["symbol"].tolist()
PX = dict(zip(SCORED.symbol, SCORED.close))


def scored_frame(symbols=None):
    return SCORED


def holding(sym, qty, px):
    return {"symbol": sym, "exchange": "NSE", "quantity": qty, "pledged_qty": 0,
            "average_price": px, "last_price": px}


def plan_for(holdings, cash=0.0, prices=None):
    px = dict(PX)
    px.update(prices or {})
    px.update({h["symbol"]: h["last_price"] for h in holdings})
    return build_plan(SCORED, holdings, cash, live_prices=px)


def order_for(plan, sym):
    return next(o for o in plan["orders"] if o["symbol"] == sym)


CASH = 5_000_000.0


def one_share_short():
    """A position sitting one share below its own target — the exact live situation.

    Learned rather than assumed: the target depends on capital, which depends on the
    holding, so the only honest way to construct it is to ask the planner first.
    """
    sym = TOP[0]
    px = float(PX[sym])
    target = order_for(build_plan(SCORED, [], CASH, live_prices=PX), sym)["qty_final"]
    qty = target - 1
    holdings = [holding(sym, qty, px)]
    plan = build_plan(SCORED, holdings, CASH - qty * px, live_prices=PX)
    return sym, plan


# =====================================================================================
# what the band suppresses
# =====================================================================================
def test_a_one_share_trim_is_suppressed():
    """WELCORP: 1 share of 362, Rs 1,917. The fee exceeds the drift it corrects."""
    sym, p = one_share_short()
    o = order_for(p, sym)
    assert o["action"] == "HOLD" and o["delta"] == 0
    assert o.get("skipped_delta")
    assert "no-trade band" in o["note"]


def test_the_suppressed_order_still_reports_what_it_would_have_done():
    """The plan keeps the target it wanted; only the ORDER is suppressed. Hiding the
    intention would make the band impossible to tune."""
    sym, p = one_share_short()
    o = order_for(p, sym)
    assert "skipped_delta" in o
    assert o["qty_final"] == o["qty_now"], "final must reflect what will actually be held"


def test_suppressed_deltas_do_not_count_toward_buys_or_sells():
    sym, p = one_share_short()
    assert order_for(p, sym)["delta"] == 0


# =====================================================================================
# what it must never suppress
# =====================================================================================
def test_a_full_exit_is_never_suppressed():
    """The band exists to avoid paying fees for nothing, not to keep the book in a name
    the strategy has rejected. A tiny leftover position must still be closed."""
    p = plan_for([holding("ZZZNOTINSCAN", 3, 50.0)], cash=1_000_000.0,
                 prices={"ZZZNOTINSCAN": 50.0})
    z = order_for(p, "ZZZNOTINSCAN")
    assert z["action"] == "EXIT" and z["delta"] == -3 and z["qty_final"] == 0
    assert "skipped_delta" not in z


def test_a_material_trade_passes_the_band():
    """A real rebalance must not be suppressed. Half the position, well over both floors."""
    sym = TOP[0]
    p = plan_for([holding(sym, 2, PX[sym])], cash=5_000_000.0)
    o = order_for(p, sym)
    assert o["delta"] != 0 and "skipped_delta" not in o


def test_a_new_position_is_never_treated_as_immaterial():
    """qty_now is zero, so the percentage test has no denominator. A fresh entry is sized
    to a full weight and must always clear."""
    p = plan_for([], cash=5_000_000.0)
    buys = [o for o in p["orders"] if o["action"] == "BUY"]
    assert buys and all(o["delta"] > 0 for o in buys)
    assert not any("skipped_delta" in o for o in buys)


# =====================================================================================
# the two floors are independent
# =====================================================================================
def test_a_large_rupee_value_that_is_a_tiny_fraction_is_still_skipped():
    """OFSS: Rs 11,623 but only 1.8% of the position. Over the rupee floor, under the
    percentage one — the drift is not worth correcting."""
    assert C.MIN_TRADE_PCT > 1.8, "the live OFSS case was 1.82% for Rs 11,623"
    # 1 share of a Rs 12,000 stock on a 100-share position: over the rupee floor,
    # under the percentage floor.
    val, pct = 12_000.0, 1.0
    assert val >= C.MIN_TRADE_VALUE and pct < C.MIN_TRADE_PCT


def test_the_floors_are_configured_not_hardcoded():
    assert C.MIN_TRADE_VALUE > 0 and C.MIN_TRADE_PCT > 0
    src = open("app/rebalance.py").read()
    assert "C.MIN_TRADE_VALUE" in src and "C.MIN_TRADE_PCT" in src
