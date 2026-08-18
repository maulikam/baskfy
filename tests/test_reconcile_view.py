"""Plan against broker.

Every defect found on 18 Aug 2026 produced plausible output rather than an error: ten of
twenty-one orders "succeeded", stops looked armed while over-covering by 905 shares, the
desk banner read green, and the database said 438 PARAS filled while the broker showed the
order open with none traded. The common shape is a system reporting its own intentions back
to itself. These tests pin the one view that reads the broker instead.
"""
from __future__ import annotations

import pytest

from app.analytics import db, plan_store as PS, reconcile_view as RV


@pytest.fixture()
def conn(tmp_path):
    with db.connect(str(tmp_path / "p.db")) as c:
        db.migrate(c)
        yield c


def plan(pid="p1"):
    return {"plan_id": pid, "constituents": ["AAA"], "weights": {"AAA": 1.0},
            "orders": [{"symbol": "AAA", "side": "BUY", "delta": 10, "qty_final": 10,
                        "ref_price": 100.0}]}


def bo(sym, status="COMPLETE", filled=10, oid="X1"):
    return {"tradingsymbol": sym, "status": status, "filled_quantity": filled,
            "order_id": oid, "average_price": 100.0}


# =====================================================================================
# the exact failures this page exists to catch
# =====================================================================================
def test_a_local_fill_the_broker_does_not_confirm_is_wrong(conn):
    """The live case: the DB said 438 PARAS filled, the broker said OPEN 0/438."""
    PS.save_plan(conn, plan())
    conn.execute("UPDATE rebalance_orders SET status='FILLED', filled_qty=10,"
                 " order_id='X1' WHERE symbol='AAA'")
    v = RV.orders_view(conn, "p1", [bo("AAA", status="OPEN", filled=0)])
    row = v["rows"][0]
    assert row["state"] == RV.WRONG
    assert "local says 10 filled, broker says 0" in row["why"]


def test_an_order_the_broker_has_never_heard_of_is_wrong(conn):
    PS.save_plan(conn, plan())
    conn.execute("UPDATE rebalance_orders SET status='SUBMITTED', order_id='X9'")
    v = RV.orders_view(conn, "p1", [])
    assert v["rows"][0]["state"] == RV.WRONG
    assert "no such order at the broker" in v["rows"][0]["why"]


def test_a_settled_order_not_yet_reconciled_is_drift_not_wrong(conn):
    """Nothing is broken; the local record simply has not caught up."""
    PS.save_plan(conn, plan())
    conn.execute("UPDATE rebalance_orders SET status='SUBMITTED', filled_qty=10,"
                 " order_id='X1' WHERE symbol='AAA'")
    v = RV.orders_view(conn, "p1", [bo("AAA")])
    assert v["rows"][0]["state"] == RV.DRIFT


def test_agreement_reads_ok(conn):
    PS.save_plan(conn, plan())
    PS.record_execution(conn, "p1", [{"symbol": "AAA", "status": "PLACED",
                                      "order_id": "X1"}])
    PS.reconcile_fills(conn, "p1", [bo("AAA")])
    assert RV.orders_view(conn, "p1", [bo("AAA")])["state"] == RV.OK


def test_an_unsubmitted_order_is_not_an_error(conn):
    """PENDING with nothing at the broker is a plan that was never executed."""
    PS.save_plan(conn, plan())
    assert RV.orders_view(conn, "p1", [])["rows"][0]["state"] == RV.OK


# =====================================================================================
# positions
# =====================================================================================
def test_a_position_short_of_its_target_is_reported():
    v = RV.positions_view(plan(), [{"symbol": "AAA", "quantity": 3}])
    assert v["rows"][0]["diff"] == -7 and v["rows"][0]["state"] == RV.WRONG


def test_a_holding_outside_the_plan_is_drift_not_an_error():
    """A runner or an untouchable is held deliberately without being in the plan."""
    v = RV.positions_view(plan(), [{"symbol": "AAA", "quantity": 10},
                                   {"symbol": "SGBDE31III", "quantity": 392}])
    rows = {r["symbol"]: r for r in v["rows"]}
    assert "AAA" not in rows                       # exact match is not reported
    assert rows["SGBDE31III"]["state"] == RV.DRIFT


def test_a_book_matching_its_plan_reports_nothing():
    assert RV.positions_view(plan(), [{"symbol": "AAA", "quantity": 10}])["rows"] == []


# =====================================================================================
# the whole view
# =====================================================================================
def test_the_worst_section_sets_the_headline(conn):
    PS.save_plan(conn, plan())
    conn.execute("UPDATE rebalance_orders SET status='FILLED', filled_qty=10,"
                 " order_id='X1' WHERE symbol='AAA'")
    v = RV.build(conn, plan_id="p1", plan=plan(),
                 broker_orders=[bo("AAA", status="OPEN", filled=0)], gtts=[],
                 holdings=[{"symbol": "AAA", "quantity": 10, "last_price": 100.0}])
    assert v["state"] == RV.WRONG
    assert v["sections"]["orders"] == RV.WRONG


def test_an_unprotected_book_makes_the_stops_section_wrong(conn):
    v = RV.build(conn, plan_id=None, plan=None, broker_orders=[], gtts=[],
                 holdings=[{"symbol": "AAA", "quantity": 10, "last_price": 100.0}])
    assert v["sections"]["stops"] == RV.WRONG


def test_the_view_only_reads():
    src = open("app/analytics/reconcile_view.py").read()
    for token in ("place_order", "place_gtt", "delete_gtt", "UPDATE ", "INSERT "):
        assert token not in src, token


def test_a_pending_row_is_not_matched_to_another_plans_order(conn):
    """The first live run reported twelve false disagreements: a plan that was never
    executed had its PENDING rows symbol-matched against a DIFFERENT plan's fills from
    earlier the same day."""
    PS.save_plan(conn, plan())
    v = RV.orders_view(conn, "p1", [bo("AAA", status="COMPLETE", filled=10, oid="OTHER")])
    assert v["rows"][0]["state"] == RV.OK
    assert v["rows"][0]["why"] == "never submitted"
