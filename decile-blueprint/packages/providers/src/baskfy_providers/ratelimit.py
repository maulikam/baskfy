"""A token bucket held in Redis, shared across processes (Prompt 2 deliverable 2).

docs/09 §"Kite specifics": "Rate limit ~ 3 req/s -> a token-bucket limiter shared across workers
(Redis)." Shared is the whole point: the nightly backfill runs with bounded concurrency and, in
production, across more than one Celery worker process. A per-process limiter would let three
workers make 9 req/s and get the API key throttled or banned.

Why Lua and not GET/SET
-----------------------
Read-modify-write from the client is not atomic: two workers can both read 1 token remaining and
both spend it. The refill and the take must happen inside one Redis round trip, so the whole
operation is a script Redis executes atomically. This is the property the concurrency test
actually exercises.

The bucket is a continuous-refill token bucket rather than a fixed window: a fixed window permits
a burst of 2N across a window boundary, which is precisely the pattern that trips broker limits.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from baskfy_providers.errors import RateLimited

#: The atomic take. Exported (rather than private) because ``baskfy_api.ratelimit`` runs the same
#: algorithm against an async Redis client for the docs/07 HTTP rate limits: two copies of a Lua
#: script are two chances to fix a bug in one of them.
#:
#: KEYS[1]  bucket key
#: ARGV[1]  capacity (max tokens)      ARGV[2] refill rate (tokens/second)
#: ARGV[3]  now (seconds, float)       ARGV[4] tokens requested
#: Returns  {granted (0|1), wait_seconds}
#:
#: State is two hash fields — the token count and the timestamp it was accurate at. Redis expires
#: the key once a full refill has elapsed, so idle buckets cost nothing and a cold bucket starts
#: full (which is correct: no requests have been made).
TAKE_SCRIPT = """
local key       = KEYS[1]
local capacity  = tonumber(ARGV[1])
local rate      = tonumber(ARGV[2])
local now       = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local state = redis.call('HMGET', key, 'tokens', 'at')
local tokens = tonumber(state[1])
local at     = tonumber(state[2])

if tokens == nil or at == nil then
  tokens = capacity
  at = now
end

-- Refill for the time elapsed since the stored reading, capped at capacity.
local elapsed = now - at
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * rate)

local granted = 0
local wait = 0.0
if tokens >= requested then
  tokens = tokens - requested
  granted = 1
else
  wait = (requested - tokens) / rate
end

redis.call('HSET', key, 'tokens', tokens, 'at', now)
redis.call('PEXPIRE', key, math.ceil(((capacity / rate) + 1) * 1000))

return {granted, tostring(wait)}
"""

#: A distributed, no-burst departure clock for broker APIs whose published limit is a hard
#: requests-per-second ceiling. Unlike a token bucket, it never releases a cold-start burst:
#: each successful caller atomically reserves one departure at least ``interval`` after the
#: preceding one, using Redis's clock so containers cannot disagree about time.
#:
#: KEYS[1]  departure-clock key
#: ARGV[1]  interval between calls       ARGV[2] maximum acceptable wait
#: Returns  {claimed (0|1), wait_seconds}
SPACING_SCRIPT = """
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


class RedisLike(Protocol):
    """The slice of redis-py this module uses.

    Narrow on purpose: it keeps the limiter testable against a real Redis without dragging the
    whole client surface into the type signature.
    """

    def register_script(self, script: str) -> ScriptLike: ...


class ScriptLike(Protocol):
    def __call__(self, keys: list[str], args: list[str | float | int]) -> object: ...


class RateLimiter(Protocol):
    """What a provider needs from a limiter: permission to make one request.

    A Protocol rather than the concrete bucket, so an adapter can be wired with a different
    limiter — a stricter one for a paid vendor, an inert one in a test — without the adapters
    knowing which. :class:`RedisTokenBucket` is the production implementation.
    """

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until ``tokens`` are available. Returns seconds waited. Raises RateLimited."""
        ...


class SpacedLimiter(Protocol):
    """A `RateLimiter` that can also say which clock it is and how fast that clock runs.

    `LayeredCallSpacer` composes these rather than concrete spacers so a lane can be built from
    anything that keeps a departure clock — and so the tests can prove the composition without
    a Redis, which is the part of it that is pure arithmetic.
    """

    @property
    def key(self) -> str: ...

    @property
    def config(self) -> CallSpacingConfig: ...

    def acquire(self, tokens: float = 1.0) -> float: ...


@dataclass(frozen=True, slots=True)
class TokenBucketConfig:
    #: Sustained rate. docs/09: ~3 req/s for Kite.
    rate_per_second: float
    #: Burst size. Defaults to one second's worth, i.e. no burst allowance beyond the rate —
    #: the conservative reading of "~3 req/s".
    capacity: float
    #: How long `acquire` will wait before giving up.
    max_wait_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.rate_per_second <= 0:
            raise ValueError(f"rate_per_second must be positive; got {self.rate_per_second}")
        if self.capacity <= 0:
            raise ValueError(f"capacity must be positive; got {self.capacity}")


class RedisTokenBucket:
    """A rate limiter that several processes can share.

    ``sleeper`` and ``clock`` are injected so tests can drive it deterministically without
    sleeping in real time, and without the limiter having to know it is under test.
    """

    def __init__(
        self,
        redis: RedisLike,
        key: str,
        config: TokenBucketConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._script = redis.register_script(TAKE_SCRIPT)
        self._key = key
        self._config = config
        self._clock = clock
        self._sleeper = sleeper

    @property
    def key(self) -> str:
        return self._key

    @property
    def config(self) -> TokenBucketConfig:
        return self._config

    def try_acquire(self, tokens: float = 1.0, *, now: float | None = None) -> float:
        """Take ``tokens`` if available.

        Returns ``0.0`` when granted, otherwise the number of seconds until enough tokens will
        have accrued. Never blocks.
        """
        moment = now if now is not None else self._clock()
        raw = self._script(
            keys=[self._key],
            args=[self._config.capacity, self._config.rate_per_second, moment, tokens],
        )
        granted, wait = _decode(raw)
        return 0.0 if granted else wait

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until ``tokens`` are available, or raise :class:`RateLimited`.

        Returns the total time waited, so callers can report queueing separately from upstream
        latency. A small jitter is added to each wait: without it, N workers released by the same
        refill all retry on the same millisecond and collide again.
        """
        waited = 0.0
        while True:
            wait = self.try_acquire(tokens)
            if wait == 0.0:
                return waited
            if waited + wait > self._config.max_wait_seconds:
                raise RateLimited(
                    f"waited {waited:.2f}s for a rate-limit token on {self._key!r} and the next "
                    f"one is {wait:.2f}s away, exceeding the {self._config.max_wait_seconds:.0f}s "
                    "budget"
                )
            pause = wait + random.uniform(0, min(0.05, wait or 0.05))
            self._sleeper(pause)
            waited += pause


@dataclass(frozen=True, slots=True)
class CallSpacingConfig:
    """No-burst request spacing shared by every process using the Redis key."""

    rate_per_second: float
    max_wait_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.rate_per_second <= 0:
            raise ValueError(f"rate_per_second must be positive; got {self.rate_per_second}")
        if self.max_wait_seconds < 0:
            raise ValueError(f"max_wait_seconds must be non-negative; got {self.max_wait_seconds}")


class RedisCallSpacer:
    """Reserve evenly spaced call times in Redis; a cold limiter grants no burst.

    One invocation is one upstream HTTP request. The reservation happens atomically before the
    sleep, so concurrent API, worker and desk processes queue on the same clock rather than each
    believing it owns the next slot.
    """

    def __init__(
        self,
        redis: RedisLike,
        key: str,
        config: CallSpacingConfig,
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._script = redis.register_script(SPACING_SCRIPT)
        self._key = key
        self._config = config
        self._sleeper = sleeper

    @property
    def key(self) -> str:
        return self._key

    @property
    def config(self) -> CallSpacingConfig:
        return self._config

    def acquire(self, tokens: float = 1.0) -> float:
        """Reserve one request departure, sleep until it, and return seconds waited."""
        if tokens != 1.0:
            raise ValueError("RedisCallSpacer grants exactly one HTTP request per acquire")
        interval = 1.0 / self._config.rate_per_second
        raw = self._script(
            keys=[self._key],
            args=[interval, self._config.max_wait_seconds],
        )
        claimed, wait = _decode(raw)
        if not claimed:
            raise RateLimited(
                f"the next rate-limit slot on {self._key!r} is {wait:.2f}s away, exceeding "
                f"the {self._config.max_wait_seconds:.0f}s budget"
            )
        if wait > 0:
            self._sleeper(wait)
        return max(wait, 0.0)


class LayeredCallSpacer:
    """Take a slot from every clock in order, so one caller can be held to two rates at once.

    THE PROBLEM THIS SOLVES (M85). Kite publishes one combined ceiling — three HTTP requests a
    second across every endpoint — and Baskfy has two kinds of caller behind it:

    * **bulk** — a backfill or a nightly bar pass. Thousands of `historical_data` calls, an hour
      or more of them, and nobody is watching. It should use the ceiling, but not all of it.
    * **interactive** — the holdings read and the live swing scan that a broker login starts, the
      desk's own page reads. Tens of calls, and a person is waiting for every one.

    With a single shared departure clock the bulk caller reserves every slot for the next hour
    and the interactive caller's wait exceeds any sane budget: on 4 Sep 2026 that is exactly the
    shape of "I log in at 1pm and the data is still yesterday's". Giving the bulk caller a
    *second*, slower clock of its own fixes it arithmetically rather than by priority: while bulk
    departs at 2/s and the ceiling is 3/s, the shared clock is drained faster than bulk fills it,
    so it never runs ahead of now and an interactive call finds a slot free.

    Order matters. The lane clock is taken **first** and the ceiling last, so the ceiling
    reservation — the one that is visible to every other process — happens at the lane's pace.
    """

    def __init__(self, spacers: Sequence[SpacedLimiter]) -> None:
        if not spacers:
            raise ValueError("a layered spacer needs at least one clock")
        self._spacers = tuple(spacers)

    @property
    def spacers(self) -> tuple[SpacedLimiter, ...]:
        return self._spacers

    @property
    def key(self) -> str:
        """The ceiling's key — the last one taken, and the one the whole box shares."""
        return self._spacers[-1].key

    def acquire(self, tokens: float = 1.0) -> float:
        return sum(spacer.acquire(tokens) for spacer in self._spacers)


#: The Lua script returns exactly {granted, wait_seconds}.
_REPLY_ARITY: Final = 2


def _decode(raw: object) -> tuple[bool, float]:
    """Redis returns Lua tables as lists, with numbers as ints and our wait as a string."""
    if not isinstance(raw, (list, tuple)) or len(raw) != _REPLY_ARITY:
        raise ValueError(f"unexpected reply from the rate-limit script: {raw!r}")
    granted_raw, wait_raw = raw
    granted = bool(int(_as_scalar(granted_raw)))
    return granted, float(_as_scalar(wait_raw))


def _as_scalar(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, (int, float, str)):
        return str(value)
    raise ValueError(f"unexpected scalar in the rate-limit reply: {value!r}")
