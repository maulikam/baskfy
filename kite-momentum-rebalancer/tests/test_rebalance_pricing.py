"""Where a plan gets its prices from.

Found in the first end-to-end rehearsal: a name already held was priced from live LTP
while a NEW name was priced from the scan CSV's close. Quantity is capital*weight/price,
so a price wrong by a factor is a position size wrong by that factor — the sample scan
priced DIXON at 639 against a live 14,130, which would have sized 1,509 shares (Rs 2.1cr)
into a Rs 9.6L slot.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app import config as C
from app import rebalance as RB


def scan_row(symbol, rank, score, close, reject="", rsi=55.0, ma20=None):
    return {"symbol": symbol, "rank": rank, "SCORE": score, "close": close,
            "reject": reject, "rsi_one_month": rsi,
            "ma_20": close * 0.95 if ma20 is None else ma20,
            "volatility_one_year": 0.30,
            "median_volume_one_year": 1e12}


@pytest.fixture()
def scored():
    return pd.DataFrame([
        scan_row("AAA", 1, 90.0, 100.0),
        scan_row("BBB", 2, 85.0, 200.0),
        scan_row("CCC", 3, 80.0, 300.0),
        scan_row("DDD", 4, 75.0, 400.0),
        # rejected: never a buy candidate, so a missing price is irrelevant to it
        scan_row("REJ", 5, 70.0, 500.0, reject="circuit"),
        # Below its 20-DMA so breadth is not trivially 100%. Rejected, because breadth
        # counts every scan row while only eligible names are buy candidates — this way
        # it exercises breadth without muddying the unpriced list.
        scan_row("LOW", 6, 65.0, 50.0, reject="circuit", ma20=60.0),
    ])


def holding(symbol, qty, px):
    return {"symbol": symbol, "quantity": qty, "pledged_qty": 0,
            "last_price": px, "average_price": px}


# =====================================================================================
# the defect
# =====================================================================================
def test_a_new_buy_is_priced_from_live_not_from_the_scan(scored):
    """The scan says 100; the market says 250. The order must use 250."""
    plan = RB.build_plan(scored, [], 1_000_000.0,
                         live_prices={"AAA": 250.0, "BBB": 200.0,
                                      "CCC": 300.0, "DDD": 400.0})
    aaa = next(o for o in plan["orders"] if o["symbol"] == "AAA")
    assert aaa["ref_price"] == 250.0


def test_position_size_follows_the_live_price(scored):
    """This is the money bug: quantity is capital*weight/price."""
    cheap = RB.build_plan(scored, [], 1_000_000.0,
                          live_prices={"AAA": 100.0, "BBB": 200.0,
                                       "CCC": 300.0, "DDD": 400.0})
    dear = RB.build_plan(scored, [], 1_000_000.0,
                         live_prices={"AAA": 400.0, "BBB": 200.0,
                                      "CCC": 300.0, "DDD": 400.0})
    q_cheap = next(o for o in cheap["orders"] if o["symbol"] == "AAA")["delta"]
    q_dear = next(o for o in dear["orders"] if o["symbol"] == "AAA")["delta"]
    assert q_cheap > q_dear
    # roughly inverse: 4x the price, about a quarter of the shares
    assert abs(q_cheap / q_dear - 4.0) < 0.35


def test_a_held_name_still_uses_its_refreshed_last_price(scored):
    plan = RB.build_plan(scored, [holding("AAA", 100, 260.0)], 500_000.0,
                         live_prices={"BBB": 200.0, "CCC": 300.0, "DDD": 400.0})
    aaa = next(o for o in plan["orders"] if o["symbol"] == "AAA")
    assert aaa["ref_price"] == 260.0


def test_live_price_wins_over_a_stale_holding_price(scored):
    plan = RB.build_plan(scored, [holding("AAA", 100, 260.0)], 500_000.0,
                         live_prices={"AAA": 275.0, "BBB": 200.0,
                                      "CCC": 300.0, "DDD": 400.0})
    aaa = next(o for o in plan["orders"] if o["symbol"] == "AAA")
    assert aaa["ref_price"] == 275.0


# =====================================================================================
# refusing rather than guessing
# =====================================================================================
def test_an_unpriceable_candidate_is_dropped_and_named(scored):
    plan = RB.build_plan(scored, [], 1_000_000.0,
                         live_prices={"BBB": 200.0, "CCC": 300.0, "DDD": 400.0})
    assert plan["unpriced"] == ["AAA"]
    assert "AAA" not in [o["symbol"] for o in plan["orders"]]


def test_a_rejected_name_without_a_price_is_not_reported_as_unpriced(scored):
    """It was never going to be bought, so its missing price is not a finding."""
    plan = RB.build_plan(scored, [], 1_000_000.0,
                         live_prices={"AAA": 100.0, "BBB": 200.0,
                                      "CCC": 300.0, "DDD": 400.0})
    assert "REJ" not in plan["unpriced"]


def test_a_zero_or_negative_price_is_treated_as_missing(scored):
    plan = RB.build_plan(scored, [], 1_000_000.0,
                         live_prices={"AAA": 0.0, "BBB": 200.0,
                                      "CCC": 300.0, "DDD": 400.0})
    assert "AAA" in plan["unpriced"]


def test_dropping_a_candidate_does_not_leave_the_book_under_invested(scored):
    """Weights must renormalise over what will actually be bought."""
    full = RB.build_plan(scored, [], 1_000_000.0,
                         live_prices={"AAA": 100.0, "BBB": 200.0,
                                      "CCC": 300.0, "DDD": 400.0})
    short = RB.build_plan(scored, [], 1_000_000.0,
                          live_prices={"BBB": 200.0, "CCC": 300.0, "DDD": 400.0})
    assert abs(full["buys_value"] - short["buys_value"]) / full["buys_value"] < 0.02


# =====================================================================================
# the regression this fix nearly introduced
# =====================================================================================
def test_breadth_is_measured_across_the_whole_scan(scored):
    """Filtering unpriced rows out of `scored` moved breadth from 83.9% to 100% on the
    live book. Breadth feeds the cash target and the regime overlay."""
    everything = RB.build_plan(scored, [], 1_000_000.0,
                               live_prices={"AAA": 100.0, "BBB": 200.0,
                                            "CCC": 300.0, "DDD": 400.0})
    missing = RB.build_plan(scored, [], 1_000_000.0,
                            live_prices={"BBB": 200.0, "CCC": 300.0, "DDD": 400.0})
    assert everything["breadth_above_20dma"] == missing["breadth_above_20dma"]


def test_breadth_counts_the_below_ma_name(scored):
    plan = RB.build_plan(scored, [], 1_000_000.0,
                         live_prices={"AAA": 100.0, "BBB": 200.0,
                                      "CCC": 300.0, "DDD": 400.0})
    assert plan["breadth_above_20dma"] == pytest.approx(100 * 5 / 6, abs=0.1)


def test_the_cash_target_is_unaffected_by_a_missing_price(scored):
    a = RB.build_plan(scored, [], 1_000_000.0,
                      live_prices={"AAA": 100.0, "BBB": 200.0,
                                   "CCC": 300.0, "DDD": 400.0})
    b = RB.build_plan(scored, [], 1_000_000.0,
                      live_prices={"BBB": 200.0, "CCC": 300.0, "DDD": 400.0})
    assert a["cash_target_pct"] == b["cash_target_pct"]


# =====================================================================================
# the guard still holds through the priced path
# =====================================================================================
def test_an_excluded_holding_never_reaches_the_orders(scored):
    plan = RB.build_plan(scored, [holding("AAA", 10, 100.0),
                                  holding(next(iter(C.EXCLUDED_SYMBOLS), "SGBDE31III"),
                                          392, 15306.0)],
                         500_000.0,
                         live_prices={"AAA": 100.0, "BBB": 200.0,
                                      "CCC": 300.0, "DDD": 400.0})
    assert not [o for o in plan["orders"] if o["symbol"].upper().startswith("SGB")]
