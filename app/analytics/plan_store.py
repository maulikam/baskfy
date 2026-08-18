"""Persistence for rebalance plans and their orders.

WHY THIS EXISTS
The tables have been in the schema since v1 and nothing ever wrote them. Three things
depended on that never being noticed:

- metrics.slippage() computes fill quality against the plan's reference price by reading
  rebalance_orders. With no writer it could only ever return zeros.
- The /regime page must answer "R3 says at most 40% equity — did the book actually get
  there?" That needs the plan a decision produced and what it filled, not just the tier.
- A plan lived in an in-memory dict, so a server restart erased the record of what was
  proposed and what came back.

What is planned and what filled are DIFFERENT facts and are stored separately: the plan is
written once at /analyze, and execution results update those rows in place. An order that
was never submitted, one rejected by a guard and one that filled are three distinct states,
and a panel claiming policy was implemented has to be able to tell them apart.
"""
from __future__ import annotations

import json
import time
from typing import Iterable, Mapping, Sequence

from . import db

# States an order can reach.
#
# SUBMITTED IS NOT FILLED, and conflating them put a lie in the database. record_execution
# used to mark a PLACED order FILLED for its whole planned quantity, so on 18 Aug 2026 the
# DB recorded 438 PARAS shares as filled while the broker still showed the order OPEN with
# zero traded. /regime reads exactly this to decide whether a policy was implemented, and
# slippage is measured against it, so the error propagates into every downstream judgement.
#
# An order reaching the exchange is knowable at submission. Whether it TRADED is not:
# these are LIMIT orders and some rest all day. So submission records SUBMITTED, and
# reconcile_fills() later reads the broker's own order book to settle it.
PENDING, SUBMITTED, FILLED, PARTIAL, FAILED, BLOCKED, DUPLICATE, SIMULATED, LAPSED = (
    "PENDING", "SUBMITTED", "FILLED", "PARTIAL", "FAILED", "BLOCKED", "DUPLICATE",
    "DRY_RUN", "LAPSED")

_OK = {"OK", "PLACED"}                  # reached the exchange; nothing yet about trading
_TERMINAL_BROKER = {"COMPLETE", "REJECTED", "CANCELLED"}


def save_plan(conn, plan: Mapping, *, evaluation_id: str | None = None,
              note: str = "") -> str:
    """Store a plan and its orders. Returns the version id (the plan_id).

    Idempotent by plan_id: re-analysing produces a new id, and re-saving the same plan
    replaces its rows rather than duplicating them.
    """
    pid = str(plan["plan_id"])
    orders = [o for o in plan.get("orders", []) if o.get("delta")]
    weights = {o["symbol"]: o.get("weight") for o in plan.get("orders", [])}
    constituents = sorted({o["symbol"] for o in plan.get("orders", [])})

    with db.transaction(conn):
        conn.execute("DELETE FROM rebalance_orders WHERE version_id=?", (pid,))
        conn.execute(
            "INSERT OR REPLACE INTO rebalance_versions(version_id, created_ts,"
            " constituents_json, weights_json, note, evaluation_id) VALUES(?,?,?,?,?,?)",
            (pid, float(plan.get("created_at") or time.time()),
             json.dumps(constituents), json.dumps(weights), note, evaluation_id))
        conn.executemany(
            "INSERT INTO rebalance_orders(version_id, symbol, side, planned_qty,"
            " planned_ref_price, filled_qty, avg_fill_price, status)"
            " VALUES(?,?,?,?,?,0,NULL,?)",
            [(pid, o["symbol"], "BUY" if o["delta"] > 0 else "SELL",
              abs(int(o["delta"])), float(o.get("ref_price") or 0.0), PENDING)
             for o in orders])
    return pid


def record_execution(conn, plan_id: str, results: Iterable[Mapping]) -> dict:
    """Fold execution results back onto the stored orders.

    A result with no matching planned row is counted but not invented as an order: the
    plan is the record of intent, and an order that was never planned belongs in the
    journal, not here.
    """
    rows = {r["symbol"]: r for r in conn.execute(
        "SELECT * FROM rebalance_orders WHERE version_id=?", (plan_id,))}
    seen, unknown = set(), []
    with db.transaction(conn):
        for res in results:
            sym = res.get("symbol")
            if sym not in rows:
                unknown.append(sym)
                continue
            seen.add(sym)
            raw = str(res.get("status") or "").upper()
            if raw in _OK:
                status, filled = SUBMITTED, 0
            elif raw.startswith("DRY"):
                status, filled = SIMULATED, 0
            elif raw == "DUPLICATE":
                status, filled = DUPLICATE, rows[sym]["filled_qty"] or 0
            elif raw == "BLOCKED":
                status, filled = BLOCKED, 0
            else:
                status, filled = FAILED, 0
            conn.execute(
                "UPDATE rebalance_orders SET status=?, filled_qty=?, avg_fill_price=?,"
                " order_id=? WHERE version_id=? AND symbol=?",
                (status, filled, res.get("avg_price") or res.get("price"),
                 res.get("order_id"), plan_id, sym))
    return {"updated": len(seen), "unknown": unknown,
            "not_submitted": sorted(set(rows) - seen)}


def reconciliation(conn, plan_id: str) -> dict:
    """Planned against submitted against filled, in rupees.

    This is the number that matters on /regime: a tier on screen is a policy, and only
    the filled column says whether the book moved.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM rebalance_orders WHERE version_id=?", (plan_id,))]
    if not rows:
        return {}

    def value(r, qty_key):
        return abs(int(r[qty_key] or 0)) * float(r["planned_ref_price"] or 0.0)

    sells = [r for r in rows if r["side"] == "SELL"]
    buys = [r for r in rows if r["side"] == "BUY"]
    submitted = [r for r in rows if r["status"] != PENDING]
    # FILLED means the broker's order book says so. SUBMITTED alone never counts, which is
    # the whole point of separating them.
    filled = [r for r in rows if r["status"] in (FILLED, PARTIAL)]
    failed = [r for r in rows if r["status"] in (FAILED, BLOCKED)]
    working = [r for r in rows if r["status"] == SUBMITTED]
    return {
        "plan_id": plan_id,
        "orders": len(rows),
        "planned_sell_value": round(sum(value(r, "planned_qty") for r in sells)),
        "planned_buy_value": round(sum(value(r, "planned_qty") for r in buys)),
        "submitted_value": round(sum(value(r, "planned_qty") for r in submitted)),
        "filled_value": round(sum(value(r, "filled_qty") for r in filled)),
        "failed_value": round(sum(value(r, "planned_qty") for r in failed)),
        "counts": {s: sum(1 for r in rows if r["status"] == s)
                   for s in sorted({r["status"] for r in rows})},
        "not_submitted": [r["symbol"] for r in rows if r["status"] == PENDING],
        "failed_symbols": [r["symbol"] for r in failed],
        "awaiting_reconciliation": [r["symbol"] for r in working],
        "fills_confirmed": not working,
    }


def latest(conn, *, evaluation_id: str | None = None) -> dict | None:
    """The most recent stored plan, optionally the one a decision produced."""
    sql = "SELECT * FROM rebalance_versions"
    args: tuple = ()
    if evaluation_id:
        sql += " WHERE evaluation_id=?"
        args = (evaluation_id,)
    sql += " ORDER BY created_ts DESC LIMIT 1"
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row else None


def orders_frame(conn, plan_id: str | None = None):
    """rebalance_orders as a DataFrame, for metrics.slippage()."""
    import pandas as pd
    sql = "SELECT * FROM rebalance_orders"
    args: tuple = ()
    if plan_id:
        sql += " WHERE version_id=?"
        args = (plan_id,)
    return pd.DataFrame([dict(r) for r in conn.execute(sql, args)])


def history(conn, limit: int = 20) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT v.*, (SELECT COUNT(*) FROM rebalance_orders o"
        "             WHERE o.version_id = v.version_id) AS order_count"
        " FROM rebalance_versions v ORDER BY v.created_ts DESC LIMIT ?", (limit,))]


# =====================================================================================
# reconciling submitted orders against what the broker actually did
# =====================================================================================
def reconcile_fills(conn, plan_id: str, broker_orders: Iterable[Mapping], *,
                    now: str | None = None) -> dict:
    """Settle SUBMITTED rows from the broker's own order book.

    This is the half that was missing. Submission records that an order reached the
    exchange; only the order book knows whether it TRADED. Matching is by order_id where
    one was stored and by symbol otherwise, because rows written before order_id existed
    have nothing else to join on.

    A COMPLETE order with a partial quantity is recorded PARTIAL rather than FILLED — a
    limit order that traded 200 of 438 is not a filled order, and rounding it up is the
    same class of error this function exists to undo.
    """
    import datetime as _dt

    now = now or _dt.datetime.now().isoformat(timespec="seconds")
    by_id, by_sym = {}, {}
    for o in broker_orders:
        oid = str(o.get("order_id") or "")
        if oid:
            by_id[oid] = o
        by_sym.setdefault(str(o.get("tradingsymbol") or ""), []).append(o)

    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM rebalance_orders WHERE version_id=?", (plan_id,))]
    changed, unresolved = [], []

    with db.transaction(conn):
        for r in rows:
            if r["status"] not in (SUBMITTED, FILLED, PARTIAL):
                continue
            o = by_id.get(str(r.get("order_id") or ""))
            if o is None:
                candidates = by_sym.get(r["symbol"], [])
                o = candidates[0] if len(candidates) == 1 else None
            if o is None:
                unresolved.append(r["symbol"])
                continue

            state = str(o.get("status") or "").upper()
            filled = int(o.get("filled_quantity") or 0)
            planned = int(r["planned_qty"] or 0)
            if state == "COMPLETE":
                status = FILLED if filled >= planned else PARTIAL
            elif state in ("REJECTED", "CANCELLED"):
                # Cancelled at the close with nothing traded is a lapse, not a failure:
                # the order was legal and simply never met its price.
                status = FAILED if filled == 0 and state == "REJECTED" else (
                    PARTIAL if filled else LAPSED)
            else:
                # STILL WORKING. Not skipped: a row already carrying a wrong FILLED from
                # the old code would keep it forever if this returned early. The truth for
                # an open order is what has traded SO FAR, which is usually nothing.
                status, filled = SUBMITTED, filled

            conn.execute(
                "UPDATE rebalance_orders SET status=?, filled_qty=?, avg_fill_price=?,"
                " reconciled_at=? WHERE id=?",
                (status, filled, o.get("average_price") or None, now, r["id"]))
            if status != r["status"] or filled != int(r["filled_qty"] or 0):
                changed.append({"symbol": r["symbol"], "was": r["status"],
                                "now": status, "filled": filled, "planned": planned})

    return {"plan_id": plan_id, "changed": changed, "unresolved": unresolved,
            "reconciled_at": now}


def open_plans(conn, limit: int = 5) -> list[str]:
    """Plans with orders still recorded as submitted — the ones needing reconciliation."""
    return [r["version_id"] for r in conn.execute(
        "SELECT DISTINCT version_id FROM rebalance_orders WHERE status=?"
        " ORDER BY id DESC LIMIT ?", (SUBMITTED, limit))]
