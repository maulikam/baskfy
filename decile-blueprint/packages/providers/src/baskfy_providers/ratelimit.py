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
from collections.abc import Callable
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
