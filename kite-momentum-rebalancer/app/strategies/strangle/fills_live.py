"""V4 live order execution. The only module here that can reach a real order.

IT ROUTES THROUGH app/core/gateway.py AND NOWHERE ELSE. That is not a style choice: the
gateway is where the untouchable-instrument guard, the overnight-option block, the product
gates, the risk manager, the rate limiter, the idempotency key and the journal all live.
An order placed around it would have none of them, and every one of those exists because
something went wrong once.

RULE R5 CANNOT BE IMPLEMENTED AS WRITTEN, AND PRETENDING OTHERWISE WOULD BE THE DANGEROUS
CHOICE. It says "never leg out sequentially on exit — always one basket/multi-leg order".
Kite Connect has no such order: the API exposes place_order, modify_order, cancel_order and
place_autoslice_order, and basket_order_margins computes margin only. Verified against
kiteconnect 5.2.1.

So the closest achievable thing is done instead, and named honestly: all legs are submitted
CONCURRENTLY rather than one after another, so the window between the first and last is
milliseconds rather than seconds. What that does not give is atomicity. If a leg fails
while others filled, the position is not the position the risk model describes, and this
module treats that as an incident: it reports PARTIAL and the caller must flatten what did
fill rather than continue with a broken structure.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

TERMINAL_OK = {"COMPLETE"}
TERMINAL_BAD = {"REJECTED", "CANCELLED"}


class LiveExecutionError(RuntimeError):
    """The basket did not execute as one position. The caller must flatten, not retry."""


@dataclass(frozen=True)
class LiveFill:
    """Mirrors fills_paper.SimFill so the session loop is transport-agnostic."""
    symbol: str
    side: str
    requested: int
    filled: int
    avg_price: float | None
    order_id: str | None
    status: str
    error: str = ""
    submitted_at: dt.datetime | None = None
    settled_at: dt.datetime | None = None

    @property
    def complete(self) -> bool:
        return self.filled == self.requested and self.avg_price is not None

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "side": self.side, "requested": self.requested,
                "filled": self.filled, "avg_price": self.avg_price,
                "order_id": self.order_id, "status": self.status,
                "error": self.error, "complete": self.complete,
                "submitted_at": self.submitted_at, "settled_at": self.settled_at}


def execute_basket(orders: Sequence[Mapping[str, Any]], cfg: dict, *, gateway, kc,
                   client_prefix: str, timeout_seconds: float = 30.0,
                   poll_seconds: float = 0.5, now=None) -> list[LiveFill]:
    """Submit every leg concurrently through the gateway, then reconcile to terminal state.

    Raises LiveExecutionError when the basket does not end up whole. It never retries: a
    retry after a partial fill is how one leg becomes three.
    """
    now = now or dt.datetime.now
    submitted = asyncio.run(_submit_all(orders, cfg, gateway, client_prefix, now))
    fills = _reconcile(submitted, kc, timeout_seconds=timeout_seconds,
                       poll_seconds=poll_seconds, now=now)
    bad = [f for f in fills if not f.complete]
    if bad:
        raise LiveExecutionError(
            "basket did not execute whole: "
            + "; ".join(f"{f.symbol} {f.status} {f.filled}/{f.requested} {f.error}".strip()
                        for f in bad)
            + ". Flatten what filled; do not retry.")
    return fills


async def _submit_all(orders, cfg, gateway, client_prefix, now):
    """All legs at once. Concurrency is the whole point — see the module docstring."""
    ins = cfg["instrument"]

    async def one(o):
        res = await gateway.place(
            symbol=o["symbol"], qty=int(o["quantity"]), side=o["side"],
            product="MIS",                       # never NRML: the overnight guard blocks it
            order_type="LIMIT", price=o.get("price"),
            exchange=ins["exchange"], variety="regular",
            # Deterministic per session+leg+intent, so a retry of the WHOLE call cannot
            # double-send an individual leg.
            client_id=f"{client_prefix}:{o['symbol']}:{o['side']}",
            tick_size=float(cfg["fills"].get("tick_size") or 0.05))
        return o, res, now()

    return await asyncio.gather(*(one(o) for o in orders))


def _reconcile(submitted, kc, *, timeout_seconds: float, poll_seconds: float,
               now) -> list[LiveFill]:
    """Poll order status until every leg is terminal or the timeout expires.

    A leg still open at the timeout is CANCELLED rather than left working. An order you
    have stopped watching is an unbounded position, and this strategy has no rule that
    tolerates one.
    """
    import time

    pending = {}
    fills: dict[str, LiveFill] = {}
    for o, res, at in submitted:
        status = str(res.get("status") or "")
        oid = res.get("order_id")
        if status in ("BLOCKED", "RISK_BLOCKED", "ERROR", "DUPLICATE"):
            fills[o["symbol"]] = LiveFill(
                symbol=o["symbol"], side=o["side"], requested=int(o["quantity"]),
                filled=0, avg_price=None, order_id=oid, status=status,
                error=str(res.get("error") or ""), submitted_at=at)
            continue
        if status == "DRY_RUN":
            fills[o["symbol"]] = LiveFill(
                symbol=o["symbol"], side=o["side"], requested=int(o["quantity"]),
                filled=0, avg_price=None, order_id=oid, status="DRY_RUN",
                error="DRY_RUN is on; no order reached the exchange", submitted_at=at)
            continue
        pending[o["symbol"]] = (o, oid, at)

    deadline = now() + dt.timedelta(seconds=timeout_seconds)
    while pending and now() < deadline:
        for symbol, (o, oid, at) in list(pending.items()):
            row = _order_row(kc, oid)
            if row is None:
                continue
            status = str(row.get("status") or "")
            if status in TERMINAL_OK or status in TERMINAL_BAD:
                filled = int(row.get("filled_quantity") or 0)
                price = row.get("average_price")
                fills[symbol] = LiveFill(
                    symbol=symbol, side=o["side"], requested=int(o["quantity"]),
                    filled=filled,
                    avg_price=float(price) if price else None, order_id=oid,
                    status=status, error=str(row.get("status_message") or ""),
                    submitted_at=at, settled_at=now())
                pending.pop(symbol)
        if pending:
            time.sleep(poll_seconds)

    for symbol, (o, oid, at) in pending.items():
        cancelled = _cancel(kc, oid)
        row = _order_row(kc, oid) or {}
        filled = int(row.get("filled_quantity") or 0)
        price = row.get("average_price")
        fills[symbol] = LiveFill(
            symbol=symbol, side=o["side"], requested=int(o["quantity"]), filled=filled,
            avg_price=float(price) if price else None, order_id=oid,
            status="TIMEOUT",
            error=f"open after {timeout_seconds:.0f}s; cancel {'ok' if cancelled else 'FAILED'}",
            submitted_at=at, settled_at=now())
    return [fills[o["symbol"]] for o, _r, _a in submitted]


def _order_row(kc, order_id) -> dict | None:
    if not order_id:
        return None
    try:
        history = kc.order_history(order_id)
    except Exception:                                        # noqa: BLE001
        return None
    return dict(history[-1]) if history else None


def _cancel(kc, order_id) -> bool:
    """Cancel through the broker directly.

    The gateway has no cancel method, and adding one is a wider change than V4 should make
    on its own. Noted rather than hidden: this is the single call in the strangle package
    that reaches Kite outside the gateway, it can only ever REDUCE exposure, and it is the
    alternative to leaving an order working unwatched.
    """
    if not order_id:
        return False
    try:
        kc.cancel_order(variety="regular", order_id=order_id)
        return True
    except Exception:                                        # noqa: BLE001
        return False
