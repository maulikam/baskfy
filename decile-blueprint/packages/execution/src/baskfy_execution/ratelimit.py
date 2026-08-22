"""Rate limiting for Kite Connect, tuned to its published caps.

Verified 2026: 10 req/s combined API, 10 orders/s, 400 orders/min; the docs say 5000
orders/day while Zerodha account notices say 3000, so we plan to the safer 3000. Staying
under 10 orders/sec also keeps us below SEBI's 10-OPS retail-algo registration threshold
(Feb-2025 framework).

WHY THE PER-SECOND CAP IS SPACING AND NOT A TOKEN BUCKET.
A bucket of capacity C refilling C tokens per second starts FULL, so it grants C
immediately and then another C as the window elapses: up to 2C in the first second. With
C=9 that is eighteen orders against a limit of ten.

That is not theoretical. On 18 Aug 2026 a 21-order rebalance sent all 21 through this
limiter; ten were accepted and ELEVEN came back "Maximum allowed order requests per second
exceeded". The book was left fully sold down and only three of fourteen buys placed, with
Rs 42 lakh sitting in cash that the plan intended to invest. A half-executed rebalance is
the single most expensive failure this system can have, and it was caused by the component
whose entire job was to prevent it.

So consecutive grants are now SPACED by at least 1/rate seconds. Spacing cannot burst by
construction: if every grant is >= 111ms after the previous one, no one-second window can
contain more than nine. The longer windows stay as token buckets, where a burst is
harmless and the refill maths is not load-bearing.
"""
from __future__ import annotations

import asyncio
import time


class Bucket:
    """Token bucket for the LONG windows (per minute, per day).

    Deliberately not used for the per-second cap. Over a window of `per_seconds` starting
    full this grants up to 2 * capacity, which is fine when the cap is 400/minute and fatal
    when it is 10/second.
    """

    def __init__(self, capacity: int, per_seconds: float):
        self.capacity, self.per = capacity, per_seconds
        self.tokens, self.last = float(capacity), time.monotonic()

    async def take(self) -> None:
        while True:
            now = time.monotonic()
            self.tokens = min(self.capacity,
                              self.tokens + (now - self.last) * self.capacity / self.per)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            await asyncio.sleep(max((1 - self.tokens) * self.per / self.capacity, 0.005))


class Spacer:
    """Guarantees a minimum interval between grants. Cannot burst.

    `rate` is grants per second. A lock serialises waiters so two coroutines cannot both
    observe the same free slot and take it — without it, concurrent callers reproduce
    exactly the burst this class exists to prevent.
    """

    def __init__(self, rate: float):
        self.interval = 1.0 / float(rate)
        self.last = 0.0
        self._lock = asyncio.Lock()

    async def take(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self.last + self.interval - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self.last = now


class KiteLimits:
    def __init__(self):
        # 9/s, not 10: one order of headroom for clock skew and for the broker counting a
        # request slightly before we do, and it keeps us under the SEBI threshold too.
        self.api = Spacer(9)
        self.orders_sec = Spacer(9)
        self.orders_min = Bucket(380, 60)      # under 400/min
        self.orders_day = Bucket(2900, 86400)  # under the 3000 Zerodha notices state

    async def order_slot(self):
        await self.api.take()
        await self.orders_sec.take()
        await self.orders_min.take()
        await self.orders_day.take()

    async def api_slot(self):
        await self.api.take()
