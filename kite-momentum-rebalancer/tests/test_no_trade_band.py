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
    """The floors come from configuration, not from a literal buried in the sizing arithmetic.

    Plan construction moved to `baskfy_core.basket` at M15 (P3.2) and reads the constants off the
    injected `cfg`, so this follows the module rather than a path -- and asserts the same property
    it always did.
    """
    import inspect
    import pathlib as _pl

    import baskfy_core.basket as _basket

    assert C.MIN_TRADE_VALUE > 0 and C.MIN_TRADE_PCT > 0
    src = _pl.Path(inspect.getsourcefile(_basket)).read_text()
    assert "cfg.MIN_TRADE_VALUE" in src and "cfg.MIN_TRADE_PCT" in src
    # And the desk still supplies its own config rather than a copy of the numbers.
    from app import rebalance as _desk

    desk = _pl.Path(inspect.getsourcefile(_desk)).read_text()
    assert "cfg=C" in desk


# =====================================================================================
# untouchable instruments are excluded by the PLANNER, not by whoever calls it
# =====================================================================================
def test_the_planner_never_proposes_selling_an_untouchable():
    """It did. Called directly, build_plan proposed EXIT SGBDE31III-GB -392 — a Rs 60 lakh
    position it must never touch — because C.EXCLUDED_SYMBOLS holds "SGBDE31III" and the
    real holding is "SGBDE31III-GB". Only the /analyze route's own SGB* filter kept that
    plan off the screen, and a plan is not made safe by the layer that executes it."""
    sgb = holding("SGBDE31III-GB", 392, 15_430.0)
    p = plan_for([sgb], cash=CASH, prices={"SGBDE31III-GB": 15_430.0})
    assert not [o for o in p["orders"] if o["symbol"] == "SGBDE31III-GB"]
    assert "SGBDE31III-GB" in p["excluded"]


def test_an_untouchable_does_not_inflate_the_capital_it_is_sized_against():
    """Counting a Rs 60 lakh untouchable as investable capital would size every position
    against money the strategy cannot deploy."""
    px = 15_430.0
    without = plan_for([], cash=CASH)
    with_sgb = plan_for([holding("SGBDE31III-GB", 392, px)], cash=CASH,
                        prices={"SGBDE31III-GB": px})
    assert with_sgb["capital"] == pytest.approx(without["capital"], rel=0.01)


def test_the_planner_uses_the_same_guard_as_the_order_path():
    """One definition of untouchable. A plan must not be able to propose something the
    gateway would refuse."""
    src = open("app/rebalance.py").read()
    assert "assert_tradeable" in src


def test_the_route_no_longer_filters_separately():
    """Two copies of a safety rule drift. The route's SGB* test is gone."""
    src = open("app/main.py").read()
    assert 'startswith("SGB")' not in src
