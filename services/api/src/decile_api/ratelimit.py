"""HTTP rate limiting (Prompt 7 deliverable 5).

docs/07 §Conventions: "Rate limits: 60 req/min authenticated, 10 req/min anonymous, 600 req/min
for API keys". docs/11 §Security: "Rate limiting per IP and per user".

The algorithm is the token bucket ``decile_providers.ratelimit`` already runs for Kite — same Lua
script, executed against an async Redis client so a limit check does not block the event loop.
Reusing it is not laziness: the reason that module chose a continuous-refill bucket over a fixed
window applies here verbatim — "a fixed window permits a burst of 2N across a window boundary".
A client that sends 60 requests at 11:59:59 and 60 more at 12:00:00 has sent 120 in a second, and
a fixed window would call that within the limit.

The bucket refills at ``limit / 60`` tokens per second with a capacity of ``limit``, so a caller
may burst its whole minute at once and then waits — which is what a per-minute quota means.

Redis is required, not optional: an unreachable cache must **not** silently disable the limit.
:class:`RateLimiter` fails closed with a 503 rather than letting the service run unmetered.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Final

from fastapi import Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

from decile_api.auth import Principal, PrincipalDep, PrincipalKind
from decile_api.problems import Problem, ProblemType, rate_limited
from decile_api.settings import Settings
from decile_providers.ratelimit import TAKE_SCRIPT

log = logging.getLogger(__name__)

SECONDS_PER_MINUTE: Final = 60

#: One namespace, so an operator can see and clear the limiter without touching the screen cache.
KEY_PREFIX: Final = "ratelimit:api:"

#: The Lua script returns exactly {granted, wait_seconds}.
_REPLY_ARITY: Final = 2


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    limit_per_minute: int
    retry_after_seconds: int


#: docs/11 §Security: "exponential backoff on **auth endpoints**". A separate, much tighter
#: bucket, keyed by address alone — an unauthenticated caller *is* the threat model here, and
#: sharing the anonymous tier would let ten password guesses a minute through per endpoint.
AUTH_KEY_PREFIX: Final = "ratelimit:auth:"

#: The path prefix the tighter bucket applies to.
AUTH_PATH_PREFIX: Final = "/auth/"


def auth_bucket_key(client_ip: str | None) -> str:
    return f"{AUTH_KEY_PREFIX}ip:{client_ip or 'unknown'}"


def is_auth_path(path: str) -> bool:
    return AUTH_PATH_PREFIX in path


#: docs/07's tiers are about *callers*: 60/min authenticated, 10/min anonymous. A payment gateway
#: is neither. Razorpay retries a failed delivery, so metering it at the anonymous rate would drop
#: events and lose payments; leaving it unmetered would remove a control. Its own bucket, wide
#: enough not to bite and narrow enough to notice (`rate_limit_webhook_per_minute`).
WEBHOOK_PATH_PREFIX: Final = "/webhooks/"


def is_webhook_path(path: str) -> bool:
    return WEBHOOK_PATH_PREFIX in path


def bucket_key(principal: Principal, client_ip: str | None) -> str:
    """Per user when we know one, per IP when we do not (docs/11: "per IP and per user").

    An anonymous caller is identified by address, which is imperfect behind a shared NAT and is
    the only identifier that exists before authentication. An authenticated caller is identified
    by account, so rotating IPs does not multiply their quota.
    """
    if principal.public_id is not None:
        return f"{KEY_PREFIX}user:{principal.public_id}"
    return f"{KEY_PREFIX}ip:{client_ip or 'unknown'}"


def limit_for(principal: Principal, settings: Settings) -> int:
    if principal.kind is PrincipalKind.API_KEY:
        return settings.rate_limit_api_key_per_minute
    if principal.is_authenticated:
        return settings.rate_limit_authenticated_per_minute
    return settings.rate_limit_anonymous_per_minute


def _decode(raw: object) -> tuple[bool, float]:
    """Redis returns Lua tables as lists, with numbers as ints and the wait as a string."""
    if not isinstance(raw, (list, tuple)) or len(raw) != _REPLY_ARITY:
        raise TypeError(f"unexpected reply from the rate-limit script: {raw!r}")
    granted, wait = raw
    return bool(int(granted)), float(wait)


class RateLimiter:
    """One shared script handle; one Redis round trip per request."""

    def __init__(self, client: Redis, settings: Settings) -> None:
        self._client = client
        self._script = client.register_script(TAKE_SCRIPT)
        self._settings = settings

    async def check(
        self,
        principal: Principal,
        client_ip: str | None,
        *,
        now: float | None = None,
        auth_endpoint: bool = False,
        webhook: bool = False,
    ) -> Decision:
        """One bucket per caller, or a path-specific one on ``/auth/*`` and ``/webhooks/*``."""
        if webhook:
            limit = self._settings.rate_limit_webhook_per_minute
            key = f"{KEY_PREFIX}webhook:{client_ip or 'unknown'}"
        elif auth_endpoint:
            limit = self._settings.rate_limit_auth_per_minute
            key = auth_bucket_key(client_ip)
        else:
            limit = limit_for(principal, self._settings)
            key = bucket_key(principal, client_ip)
        rate = limit / SECONDS_PER_MINUTE
        moment = now if now is not None else time.monotonic()
        try:
            raw = await self._script(
                keys=[key],
                args=[limit, rate, moment, 1],
            )
        except RedisError as exc:
            # Failing open would remove the limit exactly when the cache is unhealthy — the
            # moment the database can least afford unmetered traffic. docs/11 §Reliability's
            # "graceful degradation" is about serving stale *data*, not about dropping controls.
            log.error("rate limiter unavailable", extra={"error": str(exc)})
            raise Problem(
                ProblemType.PIPELINE_DEGRADED,
                "Rate limiting is unavailable, so the request was refused.",
            ) from exc

        granted, wait = _decode(raw)
        return Decision(
            allowed=granted,
            limit_per_minute=limit,
            retry_after_seconds=max(1, math.ceil(wait)),
        )

    async def enforce(
        self,
        principal: Principal,
        client_ip: str | None,
        *,
        auth_endpoint: bool = False,
        webhook: bool = False,
    ) -> Decision:
        decision = await self.check(
            principal, client_ip, auth_endpoint=auth_endpoint, webhook=webhook
        )
        if not decision.allowed:
            raise rate_limited(decision.retry_after_seconds, decision.limit_per_minute)
        return decision


async def enforce_rate_limit(request: Request, principal: PrincipalDep) -> None:
    """The dependency every ``/api/v1`` route carries (see ``decile_api.app``).

    A dependency rather than middleware because the limit depends on *who* is calling, and that
    answer comes from the same token verification the routes use. Doing it in middleware would
    mean decoding the bearer token twice, once outside the dependency graph, which is how the two
    copies drift apart.
    """
    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return
    client_ip = request.client.host if request.client is not None else None
    path = request.url.path
    await limiter.enforce(
        principal,
        client_ip,
        auth_endpoint=is_auth_path(path),
        webhook=is_webhook_path(path),
    )
