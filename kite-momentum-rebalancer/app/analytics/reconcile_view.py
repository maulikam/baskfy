"""Plan against broker: orders, stops and positions, side by side.

WHY THIS EXISTS. Every defect found on 18 Aug 2026 produced plausible output rather than an
error. Ten of twenty-one orders "succeeded" and the other eleven were rate-limited. Stops
looked "armed" while covering 10,383 shares against 9,478 held. The desk banner read green.
The database said 438 PARAS filled while the broker showed the order open with none traded.
Nothing raised, and each was found only because someone looked.

The common shape is a system reporting its own intentions back to itself. So this reads the
BROKER as the authority — order book, GTT list, holdings — and reports where the local
record disagrees. It is the one view that would have caught all five.

Read-only. It compares; it does not repair.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from . import plan_store as PS

OK, DRIFT, WRONG = "ok", "drift", "wrong"


def _sev(rows: Sequence[Mapping]) -> str:
    if any(r["state"] == WRONG for r in rows):
        return WRONG
    return DRIFT if any(r["state"] == DRIFT for r in rows) else OK


def orders_view(conn, plan_id: str, broker_orders: Iterable[Mapping]) -> dict:
    """What the plan believes each order did, against what the order book says."""
    by_id, by_sym = {}, {}
    for o in broker_orders:
        if str(o.get("order_id") or ""):
            by_id[str(o["order_id"])] = o
        by_sym.setdefault(str(o.get("tradingsymbol") or ""), []).append(o)

    rows = []
    for r in conn.execute("SELECT * FROM rebalance_orders WHERE version_id=?", (plan_id,)):
        r = dict(r)
        o = by_id.get(str(r.get("order_id") or ""))
        if o is None and r["status"] != PS.PENDING:
            # Symbol fallback ONLY for rows that were actually submitted. A PENDING row
            # was never sent, so any broker order wearing its symbol belongs to a different
            # plan — matching it reported twelve false disagreements the first time this
            # ran against the live book.
            cands = by_sym.get(r["symbol"], [])
            o = cands[0] if len(cands) == 1 else None
        broker_status = str((o or {}).get("status") or "")
        broker_filled = int((o or {}).get("filled_quantity") or 0)
        local_filled = int(r.get("filled_qty") or 0)

        if r["status"] == PS.PENDING and o is None:
            state, why = OK, "never submitted"
        elif o is None:
            state, why = WRONG, "recorded as sent, but no such order at the broker"
        elif local_filled != broker_filled:
            state, why = WRONG, (f"local says {local_filled} filled, broker says "
                                 f"{broker_filled}")
        elif r["status"] == PS.SUBMITTED and broker_status in ("COMPLETE", "REJECTED",
                                                               "CANCELLED"):
            state, why = DRIFT, f"settled at the broker ({broker_status}); not reconciled yet"
        else:
            state, why = OK, broker_status or r["status"]
        rows.append({"symbol": r["symbol"], "side": r["side"],
                     "planned": r["planned_qty"], "local_status": r["status"],
                     "local_filled": local_filled, "broker_status": broker_status,
                     "broker_filled": broker_filled, "state": state, "why": why})
    rows.sort(key=lambda x: (x["state"] != WRONG, x["state"] != DRIFT, x["symbol"]))
    return {"rows": rows, "state": _sev(rows) if rows else OK}


def positions_view(plan: Mapping | None, holdings: Sequence[Mapping]) -> dict:
    """Where the book landed against where the plan aimed.

    A plan that fully executed leaves no differences. Anything here is either an order that
    did not fill or a fill nobody recorded — both worth seeing before the next rebalance
    sizes itself against these quantities.
    """
    held = {h["symbol"]: int(h["quantity"]) for h in holdings}
    rows = []
    for o in (plan or {}).get("orders", []) or []:
        want = int(o.get("qty_final") or 0)
        have = held.pop(o["symbol"], 0)
        if want == have:
            continue
        rows.append({"symbol": o["symbol"], "planned_final": want, "held": have,
                     "diff": have - want,
                     "state": WRONG if abs(have - want) > 0 else OK,
                     "why": "target not reached" if have < want else "more than planned"})
    for sym, q in held.items():
        rows.append({"symbol": sym, "planned_final": None, "held": q, "diff": q,
                     "state": DRIFT, "why": "held but not in this plan"})
    rows.sort(key=lambda r: -abs(r["diff"]))
    return {"rows": rows, "state": _sev(rows) if rows else OK}


def build(conn, *, plan_id: str | None, plan: Mapping | None,
          broker_orders: Sequence[Mapping], gtts: Sequence[Mapping],
          holdings: Sequence[Mapping]) -> dict[str, Any]:
    """The whole comparison. One call, three sections, the broker as the authority."""
    from . import protection as P

    stops = P.review(holdings, gtts)
    orders = orders_view(conn, plan_id, broker_orders) if plan_id else {"rows": [], "state": OK}
    positions = positions_view(plan, holdings)

    sections = {"orders": orders["state"], "stops": OK if stops["healthy"] else WRONG,
                "positions": positions["state"]}
    worst = WRONG if WRONG in sections.values() else (
        DRIFT if DRIFT in sections.values() else OK)
    return {
        "plan_id": plan_id,
        "state": worst,
        "sections": sections,
        "orders": orders,
        "positions": positions,
        "stops": stops,
        "counts": {
            "orders_wrong": sum(1 for r in orders["rows"] if r["state"] == WRONG),
            "orders_drift": sum(1 for r in orders["rows"] if r["state"] == DRIFT),
            "positions_off": sum(1 for r in positions["rows"] if r["state"] == WRONG),
            "stop_findings": len(stops["findings"]),
        },
    }
