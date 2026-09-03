"""One limit in front of every Kite READ this process makes (SW20).

The order path has been rate-limited since the gateway existed: `baskfy_execution.ratelimit`
spaces placements at 9/s, buckets them at 380/min and 2,900/day, and every order takes those
slots. The reads never had anything. `app/kite_client.py` makes twenty-six calls on the broker
client and `app/analytics/` made twenty-five more directly on the same handle, and none of them
waited for anybody. That was survivable while the desk was one person clicking Analyze; it stops
being survivable on 4 Sep 2026, when the swing monitor polls through the session while the desk
page refreshes every five seconds beside it.

**Kite's caps are per endpoint, not global** (Kite Connect's own documentation):

| family | cap | what is in it |
|---|---|---|
| `quote` | **1 req/s** | `quote`, `ltp`, `ohlc` — the tightest limit in the API and the one a polling page trips first |
| `historical` | **3 req/s** | `historical_data` |
| `general` | 10 req/s | everything else: `holdings`, `positions`, `orders`, `trades`, `margins`, `instruments`, `profile`, `get_gtts` |
| orders | 10/s, 400/min, 3,000/day | `place_order`, `place_gtt`, `delete_gtt` — NOT here; they stay on the gateway's own limiter |

So this is a spacer **per family**: a slow historical backfill must not starve the quote the
monitor needs, and a burst of quotes must not eat the order path's headroom. `general` is set to
9/s rather than 10 for the same reason `KiteLimits` does — one request of headroom for clock skew
and for the broker counting a request slightly before we do.

**Why a second, synchronous limiter instead of reusing `KiteLimits`.** That one is `async`: it
is awaited inside the gateway's event loop. The desk's client is blocking and is called from
synchronous request handlers and from the monitor's own thread, so an `await` is not available at
those call sites. The semantics here are deliberately the same as `Spacer.take` — remember when
the last call went out, sleep out the remainder of the interval, record the new time — with a
threading lock instead of an asyncio one. The order path keeps its async limiter untouched.

**This limit is per process, and that is a real limit of it.** The web container and the
`swing-monitor` container are separate processes with separate `Kite` objects, so each holds its
own spacers: two processes can together exceed a family's cap. Making it global needs the shared
Redis bucket the Baskfy side already uses (`baskfy_providers.factory.build_rate_limiter`, key
`baskfy:ratelimit:kite`), and the box runs that Redis. Recorded in `docs/swing/DECISIONS-SW.md`
SW20.1 rather than fixed here: the two desk processes make very different calls (the page reads
holdings and the database; the monitor reads ticks and, rarely, one quote batch), so the realistic
overlap is one `quote` slot, and a wrong shared-state design would be worse than a stated bound.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

log = logging.getLogger("kite_limits")

#: Kite's own per-endpoint caps, and the one request of headroom on the general family.
QUOTE_PER_SECOND = 1.0
HISTORICAL_PER_SECOND = 3.0
GENERAL_PER_SECOND = 9.0

#: Which family each method of `app/kite_client.py` spends. The test in
#: `tests/test_kite_limits.py` reads this table, so a method added without an entry fails there
#: rather than silently going out unlimited.
FAMILY_FOR_CALL: dict[str, str] = {
    "ltp": "quote",
    "quote": "quote",
    "ohlc": "quote",
    "historical_data": "historical",
    "holdings": "general",
    "positions": "general",
    "orders": "general",
    "order_history": "general",
    "trades": "general",
    "margins": "general",
    "instruments": "general",
    "profile": "general",
    "get_gtts": "general",
}


class Spacer:
    """At most one call every ``1/rate`` seconds, across every thread in this process.

    The sleep happens **outside** the lock's critical section for the caller's own wait, but the
    slot is claimed **inside** it: two threads asking at once get two different departure times
    rather than the same one. Holding the lock across the sleep would serialise the wait and make
    the second caller wait twice.
    """

    def __init__(self, rate: float, *, clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        self.interval = 1.0 / float(rate)
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._next_at = 0.0

    def take(self) -> float:
        """Claim the next slot; returns how long the caller was made to wait."""
        with self._lock:
            now = self._clock()
            departure = max(now, self._next_at)
            self._next_at = departure + self.interval
        wait = departure - self._clock()
        if wait > 0:
            self._sleep(wait)
        return max(wait, 0.0)


class DeskLimits:
    """The desk's read limiter: one spacer per Kite endpoint family.

    Injectable clock and sleep so the tests can prove the elapsed floor without spending the
    seconds — a burst test that really slept would take a minute and would be the first thing
    somebody deleted.
    """

    def __init__(self, *, clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        self.quote = Spacer(QUOTE_PER_SECOND, clock=clock, sleep=sleep)
        self.historical = Spacer(HISTORICAL_PER_SECOND, clock=clock, sleep=sleep)
        self.general = Spacer(GENERAL_PER_SECOND, clock=clock, sleep=sleep)
        self.waits: dict[str, float] = {"quote": 0.0, "historical": 0.0, "general": 0.0}

    def spacer(self, family: str) -> Spacer:
        try:
            return getattr(self, family)  # type: ignore[no-any-return]
        except AttributeError as exc:                                     # pragma: no cover
            raise KeyError(f"no such Kite family: {family}") from exc

    def slot(self, family: str) -> float:
        """Wait for this family's turn. Returns the wait, and totals it for the status page."""
        waited = self.spacer(family).take()
        self.waits[family] = self.waits.get(family, 0.0) + waited
        if waited > 0.5:
            log.info("waited %.2fs for a Kite %s slot", waited, family)
        return waited

    def slot_for_call(self, call: str) -> float:
        """The slot for a named Kite method — `FAMILY_FOR_CALL`, defaulting to the general pool.

        An unknown name defaults to `general` rather than to no limit at all: a method somebody
        adds tomorrow is throttled by accident rather than unthrottled by accident.
        """
        return self.slot(FAMILY_FOR_CALL.get(call, "general"))
