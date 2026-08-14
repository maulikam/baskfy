"""Async order gateway — the ONLY module allowed to place/modify/cancel orders.
Enforces: instrument guards → risk manager → rate limits → idempotency → journal.
Sync kiteconnect calls run in a thread pool so the event loop never blocks."""
from __future__ import annotations
import asyncio, json, os, time, uuid, logging
from .guards import assert_tradeable
from .ratelimit import KiteLimits
from .risk import RiskManager
from .. import config as C

log = logging.getLogger("gateway")
JOURNAL = "data/outputs/orders_journal.jsonl"


class OrderGateway:
    def __init__(self, kc, risk: RiskManager):
        self.kc, self.risk, self.limits = kc, risk, KiteLimits()
        self._sent: dict[str, str] = {}   # client_id -> broker order_id (idempotency)
        os.makedirs(os.path.dirname(JOURNAL), exist_ok=True)

    def _journal(self, rec: dict):
        rec["ts"] = time.time()
        with open(JOURNAL, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    async def place(self, *, symbol: str, qty: int, side: str, product: str = "CNC",
                    order_type: str = "LIMIT", price: float | None = None,
                    exchange: str = "NSE", variety: str = "regular",
                    client_id: str | None = None, gross_exposure: float = 0.0,
                    series: str | None = None) -> dict:
        assert_tradeable(symbol, series)                       # layer 1: untouchables
        if product != "CNC" and not C.INTRADAY_ENABLED and exchange in ("NSE", "BSE"):
            return {"symbol": symbol, "status": "BLOCKED",
                    "error": "MIS/intraday disabled (config.INTRADAY_ENABLED)"}
        if exchange in ("NFO", "BFO") and not C.OPTIONS_ENABLED:
            return {"symbol": symbol, "status": "BLOCKED",
                    "error": "F&O disabled (config.OPTIONS_ENABLED)"}
        value = abs(qty) * float(price or 0)
        ok, why = self.risk.pre_order(symbol, value, gross_exposure)  # layer 2: risk
        if not ok:
            self._journal({"event": "risk_block", "symbol": symbol, "why": why})
            return {"symbol": symbol, "status": "RISK_BLOCKED", "error": why}
        cid = client_id or uuid.uuid4().hex[:10]
        if cid in self._sent:                                   # layer 3: idempotency
            return {"symbol": symbol, "status": "DUPLICATE", "order_id": self._sent[cid]}
        await self.limits.order_slot()                          # layer 4: rate limits
        if C.DRY_RUN:
            self._sent[cid] = f"DRY-{cid}"
            self._journal({"event": "dry_run", "symbol": symbol, "side": side,
                           "qty": qty, "price": price, "product": product})
            return {"symbol": symbol, "status": "DRY_RUN", "order_id": self._sent[cid]}
        params = dict(variety=variety, exchange=exchange, tradingsymbol=symbol,
                      quantity=abs(int(qty)), transaction_type=side, product=product,
                      order_type=order_type, validity="DAY")
        if price and order_type == "LIMIT":
            params["price"] = round(float(price), 1)
        try:
            oid = await asyncio.to_thread(self.kc.place_order, **params)
            self._sent[cid] = oid
            self._journal({"event": "placed", "order_id": oid, **params})
            return {"symbol": symbol, "status": "PLACED", "order_id": oid}
        except Exception as exc:
            self._journal({"event": "error", "symbol": symbol, "error": str(exc)})
            return {"symbol": symbol, "status": "ERROR", "error": str(exc)}
