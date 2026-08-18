"""Plan persistence: the audit trail from a regime decision to what actually filled.

rebalance_versions and rebalance_orders sat in the schema since v1 with no writer, while
metrics.slippage() read them and /regime needed a plan id it could not get.
"""
from __future__ import annotations

import pytest

from app.analytics import db
from app.analytics import metrics as M
from app.analytics import plan_store as PS


@pytest.fixture()
def conn(tmp_path):
    with db.connect(str(tmp_path / "p.db")) as c:
        db.migrate(c)
        yield c


def plan(pid="p1", orders=None):
    return {"plan_id": pid, "created_at": 1_700_000_000.0,
            "orders": orders if orders is not None else [
                {"symbol": "AAA", "delta": 10, "ref_price": 100.0, "weight": 5.0},
                {"symbol": "BBB", "delta": -5, "ref_price": 200.0, "weight": 0.0},
                {"symbol": "CCC", "delta": 0, "ref_price": 300.0, "weight": 7.0}]}


# =====================================================================================
# saving
# =====================================================================================
def test_a_plan_and_its_orders_are_stored(conn):
    PS.save_plan(conn, plan())
    assert conn.execute("SELECT COUNT(*) c FROM rebalance_versions").fetchone()["c"] == 1
    # the zero-delta HOLD is not an order
    assert conn.execute("SELECT COUNT(*) c FROM rebalance_orders").fetchone()["c"] == 2


def test_sides_are_derived_from_the_delta(conn):
    PS.save_plan(conn, plan())
    rows = {r["symbol"]: r["side"] for r in conn.execute("SELECT * FROM rebalance_orders")}
    assert rows == {"AAA": "BUY", "BBB": "SELL"}


def test_resaving_the_same_plan_does_not_duplicate_orders(conn):
    PS.save_plan(conn, plan())
    PS.save_plan(conn, plan())
    assert conn.execute("SELECT COUNT(*) c FROM rebalance_orders").fetchone()["c"] == 2


def test_the_evaluation_link_is_stored(conn):
    PS.save_plan(conn, plan(), evaluation_id="eval-1")
    assert PS.latest(conn, evaluation_id="eval-1")["version_id"] == "p1"


def test_a_plan_without_an_evaluation_is_still_stored(conn):
    """The overlay may be off; that is not a reason to lose the plan."""
    PS.save_plan(conn, plan())
    assert PS.latest(conn)["evaluation_id"] is None


def test_orders_start_pending(conn):
    PS.save_plan(conn, plan())
    assert {r["status"] for r in conn.execute("SELECT * FROM rebalance_orders")} == {"PENDING"}


# =====================================================================================
# execution results
# =====================================================================================
def broker(symbol, status="COMPLETE", filled=10, avg=None, oid=None):
    return {"tradingsymbol": symbol, "status": status, "filled_quantity": filled,
            "average_price": avg, "order_id": oid}


def test_a_placed_order_records_submitted_not_filled(conn):
    """CHANGED 18 Aug 2026. Marking a PLACED order FILLED for its whole planned quantity
    put a lie in the database: PARAS was recorded as 438 filled while the broker still
    showed the order OPEN with zero traded. Reaching the exchange is knowable at
    submission; trading is not."""
    PS.save_plan(conn, plan())
    PS.record_execution(conn, "p1", [{"symbol": "AAA", "status": "PLACED",
                                      "order_id": "X1"}])
    r = conn.execute("SELECT * FROM rebalance_orders WHERE symbol='AAA'").fetchone()
    assert r["status"] == "SUBMITTED" and r["filled_qty"] == 0
    assert r["order_id"] == "X1"


def test_reconciliation_settles_it_from_the_order_book(conn):
    PS.save_plan(conn, plan())
    PS.record_execution(conn, "p1", [{"symbol": "AAA", "status": "PLACED", "order_id": "X1"}])
    PS.reconcile_fills(conn, "p1", [broker("AAA", filled=10, avg=101.0, oid="X1")])
    r = conn.execute("SELECT * FROM rebalance_orders WHERE symbol='AAA'").fetchone()
    assert r["status"] == "FILLED" and r["filled_qty"] == 10 and r["reconciled_at"]


def test_a_part_filled_order_is_not_called_filled(conn):
    """A limit order that traded 4 of 10 is not a filled order, and rounding it up is the
    same class of error this reconciliation exists to undo."""
    PS.save_plan(conn, plan())
    PS.record_execution(conn, "p1", [{"symbol": "AAA", "status": "PLACED", "order_id": "X1"}])
    PS.reconcile_fills(conn, "p1", [broker("AAA", filled=4, oid="X1")])
    r = conn.execute("SELECT * FROM rebalance_orders WHERE symbol='AAA'").fetchone()
    assert r["status"] == "PARTIAL" and r["filled_qty"] == 4


def test_an_order_still_working_is_corrected_back_to_submitted(conn):
    """The exact live case: a row already carrying a wrong FILLED keeps it forever if
    reconciliation skips open orders. The truth for an open order is what has traded."""
    PS.save_plan(conn, plan())
    conn.execute("UPDATE rebalance_orders SET status='FILLED', filled_qty=10,"
                 " order_id='X1' WHERE symbol='AAA'")
    PS.reconcile_fills(conn, "p1", [broker("AAA", status="OPEN", filled=0, oid="X1")])
    r = conn.execute("SELECT * FROM rebalance_orders WHERE symbol='AAA'").fetchone()
    assert r["status"] == "SUBMITTED" and r["filled_qty"] == 0


def test_a_cancelled_order_that_never_traded_is_a_lapse_not_a_failure(conn):
    """A limit order cancelled at the close was legal and simply never met its price."""
    PS.save_plan(conn, plan())
    PS.record_execution(conn, "p1", [{"symbol": "AAA", "status": "PLACED", "order_id": "X1"}])
    PS.reconcile_fills(conn, "p1", [broker("AAA", status="CANCELLED", filled=0, oid="X1")])
    r = conn.execute("SELECT * FROM rebalance_orders WHERE symbol='AAA'").fetchone()
    assert r["status"] == "LAPSED"


def test_reconciliation_reports_what_it_could_not_match(conn):
    PS.save_plan(conn, plan())
    PS.record_execution(conn, "p1", [{"symbol": "AAA", "status": "PLACED", "order_id": "X1"}])
    out = PS.reconcile_fills(conn, "p1", [])
    assert out["unresolved"] == ["AAA"]


def test_a_dry_run_order_fills_nothing(conn):
    """DRY_RUN reaching the gateway is not the book moving."""
    PS.save_plan(conn, plan())
    PS.record_execution(conn, "p1", [{"symbol": "AAA", "status": "DRY_RUN"}])
    r = conn.execute("SELECT * FROM rebalance_orders WHERE symbol='AAA'").fetchone()
    assert r["status"] == "DRY_RUN" and r["filled_qty"] == 0


def test_a_blocked_order_is_distinguished_from_a_failure(conn):
    PS.save_plan(conn, plan())
    PS.record_execution(conn, "p1", [{"symbol": "AAA", "status": "BLOCKED"},
                                     {"symbol": "BBB", "status": "REJECTED"}])
    rows = {r["symbol"]: r["status"] for r in conn.execute("SELECT * FROM rebalance_orders")}
    assert rows == {"AAA": "BLOCKED", "BBB": "FAILED"}


def test_an_order_never_submitted_stays_pending_and_is_reported(conn):
    PS.save_plan(conn, plan())
    res = PS.record_execution(conn, "p1", [{"symbol": "AAA", "status": "OK"}])
    assert res["not_submitted"] == ["BBB"]


def test_a_result_for_an_unplanned_symbol_is_reported_not_invented(conn):
    PS.save_plan(conn, plan())
    res = PS.record_execution(conn, "p1", [{"symbol": "ZZZ", "status": "OK"}])
    assert res["unknown"] == ["ZZZ"]
    assert conn.execute("SELECT COUNT(*) c FROM rebalance_orders").fetchone()["c"] == 2


# =====================================================================================
# reconciliation — the number /regime needs
# =====================================================================================
def test_planned_and_filled_are_separate_facts(conn):
    """Three facts now, not two: planned, submitted, and — only after the order book has
    been read — filled."""
    PS.save_plan(conn, plan())
    PS.record_execution(conn, "p1", [{"symbol": "AAA", "status": "PLACED", "order_id": "X1"},
                                     {"symbol": "BBB", "status": "REJECTED"}])
    mid = PS.reconciliation(conn, "p1")
    assert mid["planned_buy_value"] == 1000       # 10 x 100
    assert mid["planned_sell_value"] == 1000      # 5 x 200
    assert mid["filled_value"] == 0, "nothing is filled until the broker says so"
    assert mid["awaiting_reconciliation"] == ["AAA"] and not mid["fills_confirmed"]

    PS.reconcile_fills(conn, "p1", [broker("AAA", filled=10, oid="X1")])
    r = PS.reconciliation(conn, "p1")
    assert r["filled_value"] == 1000           # only AAA, and only once confirmed
    assert r["failed_value"] == 1000           # only BBB
    assert r["failed_symbols"] == ["BBB"]
    assert r["fills_confirmed"]


def test_a_dry_run_plan_submits_value_but_fills_nothing(conn):
    PS.save_plan(conn, plan())
    PS.record_execution(conn, "p1", [{"symbol": "AAA", "status": "DRY_RUN"},
                                     {"symbol": "BBB", "status": "DRY_RUN"}])
    r = PS.reconciliation(conn, "p1")
    assert r["submitted_value"] == 2000 and r["filled_value"] == 0


def test_reconciliation_of_an_unknown_plan_is_empty(conn):
    assert PS.reconciliation(conn, "nope") == {}


# =====================================================================================
# the metric that could never return anything
# =====================================================================================
def test_slippage_can_finally_be_measured(conn):
    PS.save_plan(conn, plan())
    PS.record_execution(conn, "p1", [{"symbol": "AAA", "status": "PLACED", "order_id": "X1"}])
    PS.reconcile_fills(conn, "p1", [broker("AAA", filled=10, avg=101.0, oid="X1")])
    out = M.slippage(PS.orders_frame(conn, "p1"))
    assert out["orders"] == 1
    # bought 1% above the reference: positive means worse than planned
    assert out["mean_bps"] == pytest.approx(100.0, abs=1.0)


def test_selling_below_the_reference_also_counts_as_cost(conn):
    PS.save_plan(conn, plan())
    PS.record_execution(conn, "p1", [{"symbol": "BBB", "status": "PLACED", "order_id": "X2"}])
    PS.reconcile_fills(conn, "p1", [broker("BBB", filled=5, avg=198.0, oid="X2")])
    assert M.slippage(PS.orders_frame(conn, "p1"))["mean_bps"] > 0
