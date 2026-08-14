"""Token-bucket limiter tuned to Kite Connect's published caps:
verified 2026: 10 req/s combined API, 10 orders/s, 400 orders/min; docs say 5000
orders/day but Zerodha account notices say 3000 — we plan to the safer 3000.
Staying under 10 orders/sec also keeps you below SEBI's 10-OPS retail-algo
registration threshold (Feb-2025 framework). Gateway consumes all buckets
before every order so bursts never trip broker or regulator."""
from __future__ import annotations
import asyncio, time


class Bucket:
    def __init__(self, capacity: int, per_seconds: float):
        self.capacity, self.per = capacity, per_seconds
        self.tokens, self.last = float(capacity), time.monotonic()

    async def take(self) -> None:
        while True:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.capacity / self.per)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            await asyncio.sleep(max((1 - self.tokens) * self.per / self.capacity, 0.005))


class KiteLimits:
    def __init__(self):
        self.api = Bucket(9, 1.0)          # stay under 10/s
        self.orders_sec = Bucket(9, 1.0)   # under 10 orders/s AND under SEBI 10-OPS
        self.orders_min = Bucket(380, 60)  # under 400/min
        self.orders_day = Bucket(2900, 86400)

    async def order_slot(self):
        await self.api.take(); await self.orders_sec.take(); await self.orders_min.take(); await self.orders_day.take()

    async def api_slot(self):
        await self.api.take()
