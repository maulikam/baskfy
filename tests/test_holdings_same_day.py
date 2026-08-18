"""Holdings must include shares bought TODAY.

On 18 Aug 2026 a rebalance filled CUPID, HSCL and SANSERA in the morning. A re-run plan an
hour later showed all three at "Now 0" and proposed buying them again — about Rs 16 lakh of
duplicate purchases — because Kite does not move a same-day buy into holdings() until T+1.
The user caught it by reading the plan. Nothing in the code would have.
"""
from __future__ import annotations

import pytest

from app.kite_client import Kite


class FakeKC:
    def __init__(self, holdings, positions, positions_raises=False):
        self._h, self._p, self._raises = holdings, positions, positions_raises

    def holdings(self):
        return self._h

    def positions(self):
        if self._raises:
            raise RuntimeError("network")
        return {"net": self._p}


def hold(sym, qty, t1=0, collat=0, avg=100.0, ltp=100.0):
    return {"tradingsymbol": sym, "exchange": "NSE", "quantity": qty, "t1_quantity": t1,
            "collateral_quantity": collat, "average_price": avg, "last_price": ltp}


def pos(sym, qty, product="CNC"):
    return {"tradingsymbol": sym, "exchange": "NSE", "quantity": qty, "product": product,
            "average_price": 100.0, "last_price": 100.0}


def held(kc) -> dict:
    k = Kite.__new__(Kite)
    k.kc = kc
    return {r["symbol"]: r["quantity"] for r in k.holdings()}


# =====================================================================================
# the regression
# =====================================================================================
def test_a_stock_bought_today_is_counted():
    """CUPID: absent from holdings, +2536 in positions. Reported as 0, it would be bought
    again."""
    q = held(FakeKC([], [pos("CUPID", 2536)]))
    assert q["CUPID"] == 2536


def test_a_sale_made_today_is_not_subtracted_twice():
    """Kite already reduced the holding: SAILIFE showed 286+149=435 after selling 112 from
    547, while its position showed -112. Adding that negative would report 323."""
    q = held(FakeKC([hold("SAILIFE", 286, collat=149)], [pos("SAILIFE", -112)]))
    assert q["SAILIFE"] == 435


def test_a_full_exit_reads_as_zero_not_negative():
    q = held(FakeKC([hold("EMCURE", 0)], [pos("EMCURE", -97)]))
    assert "EMCURE" not in q or q["EMCURE"] == 0


def test_topping_up_an_existing_position_sums_both_sources():
    """An ADD that filled today: 108 settled plus 211 bought this morning."""
    q = held(FakeKC([hold("LAURUSLABS", 108)], [pos("LAURUSLABS", 211)]))
    assert q["LAURUSLABS"] == 319


def test_pledged_shares_still_count():
    """The 103-share undercount this docstring warns about must not regress."""
    q = held(FakeKC([hold("ATHERENERG", 0, collat=277)], []))
    assert q["ATHERENERG"] == 277


def test_t1_shares_still_count():
    q = held(FakeKC([hold("X", 10, t1=5, collat=2)], []))
    assert q["X"] == 17


# =====================================================================================
# what must NOT be folded in
# =====================================================================================
def test_intraday_and_derivative_positions_are_ignored():
    """Only CNC delivery counts as owning shares. An MIS or NFO leg is not a holding."""
    q = held(FakeKC([], [pos("NIFTY26AUG24000CE", 650, product="MIS"),
                         pos("RELIANCE", 10, product="MIS")]))
    assert q == {}


def test_a_zero_quantity_position_adds_nothing():
    q = held(FakeKC([hold("X", 50)], [pos("X", 0)]))
    assert q["X"] == 50


# =====================================================================================
# failure must not under-report
# =====================================================================================
def test_a_failed_positions_call_raises_rather_than_omitting_todays_buys():
    """Under-reporting holdings is the input to a plan that buys them again. Refusing is
    the safe direction; a quiet fallback to holdings-only is not."""
    k = Kite.__new__(Kite)
    k.kc = FakeKC([hold("X", 50)], [], positions_raises=True)
    with pytest.raises(RuntimeError, match="today's purchases"):
        k.holdings()


def test_the_whole_18_aug_plan_reconciles():
    """The exact state that produced the duplicate-buy plan."""
    kc = FakeKC(
        [hold("SAILIFE", 286, collat=149), hold("HFCL", 1308), hold("LAURUSLABS", 108),
         hold("WELCORP", 302), hold("EMCURE", 0)],
        [pos("CUPID", 2536), pos("HSCL", 864), pos("SANSERA", 172),
         pos("SAILIFE", -112), pos("HFCL", -835), pos("EMCURE", -97)])
    q = held(kc)
    assert q["CUPID"] == 2536 and q["HSCL"] == 864 and q["SANSERA"] == 172
    assert q["SAILIFE"] == 435 and q["HFCL"] == 1308 and q["WELCORP"] == 302
    assert q.get("EMCURE", 0) == 0
