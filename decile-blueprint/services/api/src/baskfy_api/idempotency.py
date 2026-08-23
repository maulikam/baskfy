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

Reserving versus remembering (M45.6)
------------------------------------
``replay`` then ``remember`` is a check followed by a write, with the whole request in between.
Two requests carrying one key that arrive together both find nothing, both do the work, and the
second ``SET`` overwrites the first — so the key ends up naming one of the two resources that were
created, and the guarantee the header promises never held under the only condition it is for.
Measured against a live Redis before the fix: two concurrent ``replay`` calls both returned
``None``, and two ``remember`` calls left one key with the second value.

``reserve`` closes it with ``SET … NX``, which is atomic: exactly one caller wins the key and the
losers are told the request is already in flight. That is a reservation rather than a record — the
key is claimed *before* the work, and ``release`` gives it back if the work does not happen, so a
failed attempt does not burn the key for a day.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)

KEY_PREFIX: Final = "idempotency:api:"
TTL_SECONDS: Final = 24 * 60 * 60
HEADER: Final = "Idempotency-Key"

#: Keys longer than this are a client bug, not a key.
MAX_KEY_LENGTH: Final = 255

#: What a reservation holds until the real value replaces it.
#:
#: Not a value any caller could store: every `public_id` in this service is 24 hex characters, so
#: the hyphens make this impossible to mistake for one and read back as a resource id.
#:
#: Deliberately free of NUL. An earlier draft used `"\x00pending"`, and reverting the router to
#: the old `replay` shape to prove this fix showed what that costs: the marker reached a
#: `WHERE public_id = …` and Postgres answered `invalid byte sequence for encoding "UTF8": 0x00`,
#: turning a duplicate request into a 500. The guard above it means that cannot happen now, but a
#: sentinel that is safe everywhere is worth more than one that is safe here.
PENDING: Final = "-pending-"

#: How long a reservation survives without being completed.
#:
#: Short on purpose. It bounds how long a request that died mid-flight can lock its own key out,
#: and every route that reserves finishes in well under a second. The successful path replaces it
#: with `TTL_SECONDS`, so this is only ever the lifetime of a failure.
RESERVATION_TTL_SECONDS: Final = 120

#: Compare-and-delete. A plain DELETE would race: between reading and deleting, the winner could
#: have written the real value, and releasing then would throw away a completed record.
_RELEASE_SCRIPT: Final = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class Outcome(StrEnum):
    """What :func:`reserve` found."""

    #: This caller owns the key and should do the work.
    RESERVED = "reserved"
    #: A previous request with this key already finished; ``public_id`` is what it made.
    REPLAY = "replay"
    #: Another request is holding this key right now.
    IN_FLIGHT = "in-flight"


@dataclass(frozen=True, slots=True)
class Reservation:
    outcome: Outcome
    public_id: str | None = None


def _redis_key(scope: str, key: str) -> str:
    return f"{KEY_PREFIX}{scope}:{key}"


async def reserve(client: Redis | None, scope: str, key: str | None) -> Reservation:
    """Claim ``key`` atomically, or say who already has it.

    Degrades to :attr:`Outcome.RESERVED` whenever there is nothing to reserve *with* — no cache,
    no key, or Redis refusing — which is the same bargain the rest of this module makes: no
    idempotency rather than a refused write.
    """
    if client is None or not key:
        return Reservation(Outcome.RESERVED)
    redis_key = _redis_key(scope, key[:MAX_KEY_LENGTH])
    try:
        won = await client.set(redis_key, PENDING, nx=True, ex=RESERVATION_TTL_SECONDS)
        if won:
            return Reservation(Outcome.RESERVED)
        stored = await client.get(redis_key)
    except RedisError as exc:
        log.warning("idempotency reservation failed", extra={"error": str(exc)})
        return Reservation(Outcome.RESERVED)
    if stored is None:
        # It expired between the SET and the GET. Rare, and letting the work proceed is the same
        # outcome as if this request had arrived a moment later.
        return Reservation(Outcome.RESERVED)
    value = stored.decode("utf-8") if isinstance(stored, bytes) else str(stored)
    if value == PENDING:
        return Reservation(Outcome.IN_FLIGHT)
    return Reservation(Outcome.REPLAY, value)


async def release(client: Redis | None, scope: str, key: str | None) -> None:
    """Give back a reservation this caller did not complete.

    Without this a request that failed would hold its key for the full reservation window, so the
    retry the client is about to make — the entire point of sending the header — would be refused.
    """
    if client is None or not key:
        return
    try:
        # redis-py types `eval` as `Awaitable[str] | str` because the sync and async clients share
        # a base; on `redis.asyncio` it is always the awaitable. A `cast` rather than a
        # `type: ignore`, which house rule 3 forbids and which would hide more than this narrows.
        deleted = cast(
            "Awaitable[object]",
            client.eval(_RELEASE_SCRIPT, 1, _redis_key(scope, key[:MAX_KEY_LENGTH]), PENDING),
        )
        await deleted
    except RedisError as exc:
        log.warning("idempotency release failed", extra={"error": str(exc)})


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
