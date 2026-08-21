"""``Idempotency-Key`` support (docs/07 §Conventions).

    "Idempotency: `Idempotency-Key` header honoured on all POSTs that create resources"

Which, in Prompt 7's surface, means ``POST /screens`` and ``POST /screens/{id}/duplicate``. A
retried request — the browser's, or a client library's — must not leave a second screen behind.

The record is a Redis key holding the ``public_id`` the first attempt created, scoped to the
caller so one user's key cannot return another user's resource. It expires after a day: long
enough to cover any retry a client would sensibly make, short enough that the namespace does not
grow without bound.

Redis being unavailable degrades to *no* idempotency rather than to a refused write. That is the
opposite of the rate limiter's choice, and deliberately: the failure here duplicates a screen the
user can delete, whereas failing open on the limiter removes a control.
"""

from __future__ import annotations

import logging
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)

KEY_PREFIX: Final = "idempotency:api:"
TTL_SECONDS: Final = 24 * 60 * 60
HEADER: Final = "Idempotency-Key"

#: Keys longer than this are a client bug, not a key.
MAX_KEY_LENGTH: Final = 255


def _redis_key(scope: str, key: str) -> str:
    return f"{KEY_PREFIX}{scope}:{key}"


async def remember(client: Redis | None, scope: str, key: str | None, value: str) -> None:
    """Record what ``key`` created, if this deployment has a cache to record it in."""
    if client is None or not key:
        return
    try:
        await client.set(_redis_key(scope, key[:MAX_KEY_LENGTH]), value, ex=TTL_SECONDS)
    except RedisError as exc:
        log.warning("idempotency record failed", extra={"error": str(exc)})


async def replay(client: Redis | None, scope: str, key: str | None) -> str | None:
    """The ``public_id`` a previous request with this key created, if any."""
    if client is None or not key:
        return None
    try:
        stored = await client.get(_redis_key(scope, key[:MAX_KEY_LENGTH]))
    except RedisError as exc:
        log.warning("idempotency lookup failed", extra={"error": str(exc)})
        return None
    if stored is None:
        return None
    return stored.decode("utf-8") if isinstance(stored, bytes) else str(stored)
