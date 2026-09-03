"""One limit in front of every Kite READ the desk makes (SW20), shared across processes (SW21).

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

**SW21: the same claim, made in Redis, so the box holds one limit and not one per container.**
The web container and the `swing-monitor` container are separate processes with separate `Kite`
objects. Until SW21 each held its own spacers and the two could together exceed a family's cap —
stated as a bound in `docs/swing/DECISIONS-SW.md` SW20.1 rather than fixed. It is fixed here:
when `BASKFY_REDIS_URL` names a reachable Redis (the box's, the same one
`baskfy_providers.factory.build_rate_limiter` uses), every family's departure clock lives at
`baskfy:ratelimit:kite:<family>` and every process on the box queues behind the same one.

**Spacing in Redis, not the providers' token bucket, and that is deliberate.**
`baskfy_providers.ratelimit.RedisTokenBucket` is the shared limiter the Baskfy pipeline uses, and
reusing it here would have been one algorithm instead of two — but it is a token bucket, and a
bucket of capacity C refilling C/s grants up to 2C inside the first second. That is not
theoretical on this desk: `baskfy_execution.ratelimit` carries the story of 18 Aug 2026, when
eleven of twenty-one orders came back "Maximum allowed order requests per second exceeded" from
exactly that arithmetic, and its per-second cap has been spacing rather than a bucket ever since.
The tightest family here is `quote` at 1 req/s, where a burst of two is a 100% overshoot. So the
Lua below is the distributed form of `Spacer.take`: claim the next departure, sleep out the
remainder, record the next one. It cannot burst by construction. The bucket keeps the pipeline's
key (`baskfy:ratelimit:kite`); the desk's families sit beside it under the same namespace.

**What is still not shared, and it is the honest residual.** The pipeline's Kite provider takes
its own bucket at `baskfy:ratelimit:kite` for the calls the worker makes, so the desk and a
nightly backfill can still, between them, exceed an endpoint's cap. The overlap is small in
practice — the backfill runs when the desk is asleep — and unifying them means giving the ingest
path these families and this algorithm, which is a change to the pipeline, not to the desk.
`docs/swing/DECISIONS-SW.md` SW21.1 records it and how to close it.

**Redis is a nicety, never a dependency.** No URL, an unreachable server, a connection that
drops mid-session, or a queue longer than `MAX_SHARED_WAIT_SECONDS` all degrade to this process's
own spacers, which are always taken as well. The desk must be able to rebalance on any Friday
(root CLAUDE.md), and a rate limiter that can stop it trading is a worse failure than the one it
prevents. Degrading is logged, and `/status` reports which of the two modes is live.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping

from .. import config as C

log = logging.getLogger("kite_limits")

#: Kite's own per-endpoint caps, and the one request of headroom on the general family.
QUOTE_PER_SECOND = 1.0
HISTORICAL_PER_SECOND = 3.0
GENERAL_PER_SECOND = 9.0

#: The three families and their caps, in one place: `DeskLimits` builds its spacers from this and
#: so does the shared wiring, so a family cannot exist locally and be missing from Redis.
RATE_FOR_FAMILY: dict[str, float] = {
    "quote": QUOTE_PER_SECOND,
    "historical": HISTORICAL_PER_SECOND,
    "general": GENERAL_PER_SECOND,
}

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

#: The namespace the Baskfy side already owns (`baskfy:ratelimit:kite` is the pipeline's bucket).
#: One key per family, because Kite's caps are per family.
SHARED_KEY_PREFIX = "baskfy:ratelimit:kite"

#: How long a caller will wait for a slot claimed by some other process before giving up on the
#: shared clock and falling back to its own. Ten seconds of queue at `general` is ninety calls in
#: flight elsewhere, which is not a state this desk reaches; reaching it means something is wrong
#: enough that a hung page would be the worse answer.
MAX_SHARED_WAIT_SECONDS = 10.0

#: After Redis fails, how long before a call tries it again. Without this every read pays the
#: connect timeout while Redis is down, which turns a degraded limiter into a slow desk.
SHARED_RETRY_AFTER_SECONDS = 30.0

#: How long a process stays per-process after Redis refused the *connection*, before it tries to
#: build the shared spacers again. A deploy restarts Redis in seconds; a desk that gave up on it
#: at start-up and never looked again would run unshared until somebody noticed, which is exactly
#: the silent state SW21 exists to end.
SHARED_REBUILD_AFTER_SECONDS = 60.0

#: Connect and read timeouts for the limiter's own Redis calls. Short on purpose: this sits in
#: front of a page refresh, and waiting on a sick Redis is not better than not asking it.
SHARED_SOCKET_TIMEOUT_SECONDS = 1.5

#: The distributed form of `Spacer.take`, and the reason it is a script: read-modify-write from
#: the client is not atomic, so two containers would both read the same departure and both take
#: it. Redis's own clock (`TIME`) is the reference, so the two processes cannot disagree about
#: what "now" is even if their container clocks drift.
#:
#: KEYS[1]  the family's departure key
#: ARGV[1]  interval (seconds between departures)   ARGV[2] the caller's patience, in seconds
#: Returns  {claimed (0|1), wait_seconds}
#:
#: A refusal (`claimed == 0`) does NOT advance the clock: a caller that will not wait must not
#: consume a slot it never used, or it would throttle the process that does wait.
TAKE_SCRIPT = """
local key      = KEYS[1]
local interval = tonumber(ARGV[1])
local max_wait = tonumber(ARGV[2])

local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local next_at = tonumber(redis.call('GET', key))
if next_at == nil or next_at < now then
  next_at = now
end

local wait = next_at - now
if wait > max_wait then
  return {0, tostring(wait)}
end

redis.call('SET', key, next_at + interval, 'PX', math.ceil((wait + interval) * 1000) + 1000)
return {1, tostring(wait)}
"""


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


class SharedSpacer:
    """`Spacer`, with the departure clock in Redis so every process on the box shares it.

    Returns ``None`` from :meth:`take` for every way the shared clock can fail to answer — no
    Redis, a dropped connection, a queue past the patience budget. ``None`` means "this call is
    yours to space locally", never "go ahead unthrottled": :meth:`DeskLimits.slot` takes the
    process's own spacer either way.
    """

    def __init__(self, script: Callable[..., object], key: str, rate: float, *,
                 max_wait: float = MAX_SHARED_WAIT_SECONDS,
                 retry_after: float = SHARED_RETRY_AFTER_SECONDS,
                 clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        self.key = key
        self.interval = 1.0 / float(rate)
        self._script = script
        self._max_wait = max_wait
        self._retry_after = retry_after
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        #: While Redis is known bad, do not pay its timeout on every read.
        self._down_until = 0.0

    @property
    def available(self) -> bool:
        """False while the last failure's back-off is still running."""
        return self._clock() >= self._down_until

    def take(self) -> float | None:
        """Claim the box's next slot for this family, or ``None`` if the clock could not say."""
        if not self.available:
            return None
        try:
            raw = self._script(keys=[self.key], args=[self.interval, self._max_wait])
        except Exception as exc:
            # Narrow enough in intent (house rule 3): the exception is reported, not swallowed,
            # and the caller is told the shared clock did not answer. Broad in type because
            # redis-py raises connection, timeout and script errors from different trees and
            # every one of them means the same thing here.
            self._down_until = self._clock() + self._retry_after
            log.warning("shared Kite limiter %s is unavailable (%s: %s); this process's own "
                        "spacer holds the limit for the next %.0fs",
                        self.key, type(exc).__name__, exc, self._retry_after)
            return None
        claimed, wait = _claim(raw)
        if not claimed:
            log.warning("the box's next Kite %s slot is %.1fs away, past the %.0fs budget; "
                        "spacing this call locally instead", self.key, wait, self._max_wait)
            return None
        if wait > 0:
            self._sleep(wait)
        return max(wait, 0.0)


def _claim(raw: object) -> tuple[bool, float]:
    """The Lua reply, which is a two-element table: an integer flag and the wait as a string."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError(f"unexpected reply from the Kite limiter script: {raw!r}")
    claimed_raw, wait_raw = raw
    return bool(int(_scalar(claimed_raw))), float(_scalar(wait_raw))


def _scalar(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, (int, float, str)):
        return str(value)
    raise ValueError(f"unexpected scalar in the Kite limiter reply: {value!r}")


_shared_lock = threading.Lock()
_shared: dict[str, SharedSpacer] | None = None
#: When to try building them again after a failed connection; ``None`` means never (either they
#: are built, or the operator turned sharing off and there is nothing to retry).
_shared_retry_at: float | None = None


def shared_spacers() -> dict[str, SharedSpacer]:
    """The box's spacers, built once per process. Empty when the desk is on its own.

    Built lazily rather than at import so that a Redis which is slow to come up costs the first
    Kite read of the process rather than the process itself, and so that `python -m app.*` on a
    laptop never blocks on a server nobody asked for. A failed connection is retried, once a
    minute, for the reason on `SHARED_REBUILD_AFTER_SECONDS`.
    """
    global _shared, _shared_retry_at
    with _shared_lock:
        if _shared is None or (
            _shared_retry_at is not None and time.monotonic() >= _shared_retry_at
        ):
            _shared = _build_shared_spacers()
            _shared_retry_at = (
                time.monotonic() + SHARED_REBUILD_AFTER_SECONDS if _needs_retry(_shared) else None
            )
        return _shared


def _needs_retry(built: dict[str, SharedSpacer]) -> bool:
    """A configured desk with no spacers could not reach Redis, and should look again."""
    return not built and bool(C.SHARED_READ_LIMITS and C.SHARED_READ_LIMITS_REDIS_URL)


def reset_shared_spacers() -> None:
    """Forget the connection, so the next caller rebuilds it. For tests and for a reconfigure."""
    global _shared, _shared_retry_at
    with _shared_lock:
        _shared = None
        _shared_retry_at = None


def shared_mode() -> str:
    """``"shared"`` or ``"per-process"`` — what `/status` reports, and what a deploy verifies."""
    return "shared" if shared_spacers() else "per-process"


def _build_shared_spacers() -> dict[str, SharedSpacer]:
    if not C.SHARED_READ_LIMITS:
        log.info("Kite read limiter: per-process (DESK_SHARED_READ_LIMITS is off)")
        return {}
    if not C.SHARED_READ_LIMITS_REDIS_URL:
        log.info("Kite read limiter: per-process (BASKFY_REDIS_URL is unset)")
        return {}
    try:
        import redis

        client = redis.Redis.from_url(
            C.SHARED_READ_LIMITS_REDIS_URL,
            socket_timeout=SHARED_SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=SHARED_SOCKET_TIMEOUT_SECONDS,
            health_check_interval=30,
        )
        client.ping()
        script = client.register_script(TAKE_SCRIPT)
    except Exception as exc:
        # The desk runs without Redis. It ran without one until SW21 and it must still start
        # when the container is down — a limiter is not allowed to be the reason Friday's
        # rebalance cannot happen.
        log.warning("Kite read limiter: per-process — Redis at the configured URL did not answer "
                    "(%s: %s)", type(exc).__name__, exc)
        return {}
    log.info("Kite read limiter: shared, keys %s:{%s}",
             SHARED_KEY_PREFIX, ",".join(RATE_FOR_FAMILY))
    return {
        family: SharedSpacer(script, f"{SHARED_KEY_PREFIX}:{family}", rate)
        for family, rate in RATE_FOR_FAMILY.items()
    }


class DeskLimits:
    """The desk's read limiter: one spacer per Kite endpoint family, and the box's own beside it.

    Injectable clock and sleep so the tests can prove the elapsed floor without spending the
    seconds — a burst test that really slept would take a minute and would be the first thing
    somebody deleted. ``shared`` is the same seam: ``None`` takes the process's shared spacers
    (built from the environment, empty when there is no Redis), and ``{}`` is a limiter that is
    deliberately alone — which is what `tests/conftest.py` gives every test by default.
    """

    def __init__(self, *, clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None,
                 shared: Mapping[str, SharedSpacer] | None = None) -> None:
        self.quote = Spacer(QUOTE_PER_SECOND, clock=clock, sleep=sleep)
        self.historical = Spacer(HISTORICAL_PER_SECOND, clock=clock, sleep=sleep)
        self.general = Spacer(GENERAL_PER_SECOND, clock=clock, sleep=sleep)
        self.shared: dict[str, SharedSpacer] = dict(
            shared if shared is not None else shared_spacers()
        )
        self.waits: dict[str, float] = {"quote": 0.0, "historical": 0.0, "general": 0.0}
        #: How many times the shared clock could not answer and this process spaced alone. The
        #: number the operator wants when asking whether the box's limit is actually one limit.
        self.local_only: dict[str, int] = {"quote": 0, "historical": 0, "general": 0}

    @property
    def is_shared(self) -> bool:
        return bool(self.shared)

    def spacer(self, family: str) -> Spacer:
        try:
            return getattr(self, family)  # type: ignore[no-any-return]
        except AttributeError as exc:                                     # pragma: no cover
            raise KeyError(f"no such Kite family: {family}") from exc

    def slot(self, family: str) -> float:
        """Wait for this family's turn. Returns the wait, and totals it for the status page.

        The box's clock first, then this process's own. Both, always: the shared claim bounds
        what the whole box sends, the local spacer bounds what this process sends, and when the
        desk is alone on the box the second wait is zero because the first already spent it.
        """
        waited = 0.0
        shared = self.shared.get(family)
        if shared is not None:
            claimed = shared.take()
            if claimed is None:
                self.local_only[family] = self.local_only.get(family, 0) + 1
            else:
                waited += claimed
        waited += self.spacer(family).take()
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
