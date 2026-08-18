"""Async order gateway — the ONLY module allowed to place/modify/cancel orders.
Enforces: instrument guards → risk manager → rate limits → idempotency → journal.
Sync kiteconnect calls run in a thread pool so the event loop never blocks."""
from __future__ import annotations
import asyncio, json, os, time, uuid, logging
from .guards import (OvernightOptionError, assert_not_overnight_option,
                     assert_tradeable)
from .ratelimit import KiteLimits
from .risk import RiskManager
from .. import config as C

log = logging.getLogger("gateway")
JOURNAL = "data/outputs/orders_journal.jsonl"


# Statuses that mean the order did NOT reach the exchange, and say why. The circuit
# breaker in /execute counts consecutive identical failures against this set, so a status
# missing from it is a systemic refusal the breaker cannot see: RISK_BLOCKED was absent,
# and a tripped loss cap would have refused all twenty-one orders one at a time — the exact
# shape of the 18 Aug "No IPs configured" batch the breaker was built after.
#
# DUPLICATE and DRY_RUN are deliberately NOT here: neither is a failure, and neither
# carries an error to compare.
FAILED_STATUSES: frozenset[str] = frozenset({"ERROR", "BLOCKED", "RISK_BLOCKED"})


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
                    series: str | None = None, tick_size: float | None = None) -> dict:
        assert_tradeable(symbol, series)                       # layer 1: untouchables
        # Same layer: an option under a carry product would still be open tomorrow morning.
        # Returned as BLOCKED rather than raised so one refused leg cannot abort a batch
        # that has already placed real orders.
        try:
            assert_not_overnight_option(symbol, exchange, product)
        except OvernightOptionError as exc:
            self._journal({"event": "overnight_option_block", "symbol": symbol,
                           "product": product, "side": side, "exchange": exchange})
            return {"symbol": symbol, "status": "BLOCKED", "error": str(exc)}
        # MIS is an intraday product on every segment. The old NSE/BSE-only check let an
        # NFO MIS order through with INTRADAY_ENABLED=false as soon as OPTIONS_ENABLED was
        # set, so enabling options silently enabled an intraday engine as well.
        if product == "MIS" and not C.INTRADAY_ENABLED:
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
            # Option ticks are currently Rs 0.05; rounding every limit to one decimal can
            # move a price away from the selected quote. Live contract metadata remains
            # authoritative because exchange tick sizes can change.
            px = float(price)
            params["price"] = (round(round(px / tick_size) * tick_size, 2)
                               if tick_size and tick_size > 0 else round(px, 1))
        try:
            oid = await asyncio.to_thread(self.kc.place_order, **params)
            self._sent[cid] = oid
            self._journal({"event": "placed", "order_id": oid, **params})
            return {"symbol": symbol, "status": "PLACED", "order_id": oid}
        except Exception as exc:
            self._journal({"event": "error", "symbol": symbol, "error": str(exc)})
            return {"symbol": symbol, "status": "ERROR", "error": str(exc)}
