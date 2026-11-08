"""Minting, verifying and metering API keys — Prompt 20 deliverable 1.

    "API keys: creation, scoping (read-only), rotation, revocation, per-key rate limits, and a
     usage dashboard. Keys are hashed at rest and shown once."

The shape of a key is ``baskfy_core.api_keys`` (pure); this module is everything that touches the
database. ``baskfy_api.routers.api_keys`` is the lifecycle over HTTP and
``baskfy_api.routers.public`` is the only place a key is accepted as a credential.

Why there is no cache
---------------------
Prompt 20's first acceptance criterion is:

    "A test asserts a revoked key is rejected within one second (no cached-auth window)."

So :func:`authenticate` reads the row on **every** request. That is one indexed lookup on
``api_key.prefix`` — a unique b-tree probe — plus a constant-time digest comparison, which is
cheaper than the JWT verification every other authenticated route already pays. Caching it behind
even a five-second TTL would mean a leaked key keeps working for five seconds after its owner
presses Revoke, and "we noticed the leak and it kept working" is precisely the failure the
criterion is written to prevent. Recorded in ``docs/DECISIONS.md`` §20.3.

The usage counters are written on the request path
--------------------------------------------------
One ``INSERT ... ON CONFLICT DO UPDATE`` into ``api_key_usage_daily`` per accepted request, plus
an occasional ``last_used_at`` touch. That is a write per read, which is the honest cost of the
dashboard Prompt 20 asks for at this scale; a Redis counter flushed by a periodic task would be
cheaper and would lose the tail on a restart. If the public API ever carries real volume this is
the first thing to move — noted in ``docs/DECISIONS.md`` §20.6.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.security import digest, digests_match
from baskfy_api.settings import Settings
from baskfy_core.api_keys import (
    KEY_PREFIX_LENGTH,
    KEY_SECRET_BYTES,
    MAX_KEYS_PER_ACCOUNT,
    InvalidKeyFormat,
    ParsedKey,
    Scope,
    format_key,
    parse_key,
)
from baskfy_core.models import ApiKey, ApiKeyUsageDaily

log = logging.getLogger(__name__)

__all__ = [
    "IssuedKey",
    "KeyLimitReached",
    "UsagePoint",
    "authenticate",
    "create_key",
    "list_keys",
    "load_key",
    "record_usage",
    "revoke_key",
    "rotate_key",
    "usage_for_key",
]

#: Bytes behind ``api_key.public_id``. Same size as a backtest's, and unrelated to the key secret:
#: the public id names the *row* (it is in URLs), the secret authenticates.
PUBLIC_ID_BYTES: Final = 12

#: How stale ``last_used_at`` may get before it is written again. Every request would be a row
#: update per request purely to move a timestamp; a minute is precise enough to answer "is this
#: key still in use" and turns a per-request write into an occasional one.
LAST_USED_RESOLUTION_SECONDS: Final = 60

#: How long an error message keeps of a rejected key. Never the secret — see
#: ``baskfy_core.api_keys.redact``.
REDACTED: Final = "<redacted>"


class KeyLimitReached(ValueError):
    """The account already holds :data:`MAX_KEYS_PER_ACCOUNT` live keys."""


@dataclass(frozen=True, slots=True)
class IssuedKey:
    """A freshly minted key: the row, and the plaintext that will never exist again."""

    key: ApiKey
    #: **Shown once.** Nothing stores this and nothing logs it.
    plaintext: str


@dataclass(frozen=True, slots=True)
class UsagePoint:
    """One day of one key's usage, as the dashboard renders it."""

    date: dt.date
    requests: int
    throttled: int


def _new_public_id() -> str:
    return secrets.token_hex(PUBLIC_ID_BYTES)


def _new_prefix() -> str:
    """12 hex characters. Collisions are caught by the unique index, not hoped away."""
    return secrets.token_hex(KEY_PREFIX_LENGTH // 2)


def _new_secret() -> str:
    return secrets.token_urlsafe(KEY_SECRET_BYTES)


async def _live_key_count(session: AsyncSession, user_id: int) -> int:
    """Un-revoked keys. An expired-but-not-revoked key still counts: it is a row the owner can
    see and rotate, and letting expiries silently free up slots would make the cap unpredictable.
    """
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(ApiKey)
                .where(ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None))
            )
        ).scalar_one()
    )


async def create_key(  # noqa: PLR0913 - name, scopes, limit, expiry and provenance are all inputs
    session: AsyncSession,
    *,
    user_id: int,
    name: str,
    scopes: Sequence[Scope],
    settings: Settings,
    rate_limit_per_minute: int | None = None,
    expires_at: dt.datetime | None = None,
    rotated_from_id: int | None = None,
) -> IssuedKey:
    """Mint a key, store its digest, and hand back the plaintext exactly once.

    ``rate_limit_per_minute`` is clamped to ``api_key_max_rate_limit_per_minute``: an owner may
    ask for *less* than docs/07's 600/min tier for a key that runs somewhere untrusted, never for
    more. A value above the ceiling is an error rather than a silent clamp — a caller who asked
    for 10,000 and got 600 without being told would build against a number that does not exist.
    """
    if not name.strip():
        raise ValueError("a key needs a name; an unlabelled key is one nobody dares revoke")
    ceiling = settings.api_key_max_rate_limit_per_minute
    if rate_limit_per_minute is not None:
        if rate_limit_per_minute < 1:
            raise ValueError("rate_limit_per_minute must be at least 1")
        if rate_limit_per_minute > ceiling:
            raise ValueError(f"rate_limit_per_minute may not exceed {ceiling}")
    if await _live_key_count(session, user_id) >= MAX_KEYS_PER_ACCOUNT:
        raise KeyLimitReached(
            f"This account already holds {MAX_KEYS_PER_ACCOUNT} live keys. Revoke one first."
        )

    expiry = expires_at
    if expiry is None and settings.api_key_default_ttl_days is not None:
        expiry = dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=settings.api_key_default_ttl_days)

    prefix = _new_prefix()
    secret = _new_secret()
    row = ApiKey(
        public_id=_new_public_id(),
        user_id=user_id,
        name=name.strip(),
        prefix=prefix,
        token_hash=digest(secret),
        scopes=[scope.value for scope in scopes],
        rate_limit_per_minute=rate_limit_per_minute,
        expires_at=expiry,
        rotated_from_id=rotated_from_id,
    )
    session.add(row)
    await session.flush()
    log.info("api key created", extra={"key_public_id": row.public_id, "prefix": prefix})
    return IssuedKey(key=row, plaintext=format_key(prefix, secret))


async def rotate_key(
    session: AsyncSession, key: ApiKey, *, settings: Settings, now: dt.datetime | None = None
) -> IssuedKey:
    """Issue a replacement and revoke the original **immediately**.

    No grace window. A rotation with an overlap is friendlier to an integration mid-deploy, and it
    is also indistinguishable, from the outside, from a key that was not really rotated — which
    is the wrong default for a credential whose whole rotation story exists because it may have
    leaked. An owner who wants an overlap creates a second key, deploys it, then revokes the
    first: two explicit steps rather than one implicit window. ``docs/DECISIONS.md`` §20.5.
    """
    moment = now or dt.datetime.now(tz=dt.UTC)
    issued = await create_key(
        session,
        user_id=key.user_id,
        name=key.name,
        scopes=[Scope(value) for value in key.scopes],
        settings=settings,
        rate_limit_per_minute=key.rate_limit_per_minute,
        expires_at=key.expires_at,
        rotated_from_id=key.id,
    )
    key.revoked_at = moment
    key.revoked_reason = "rotated"
    await session.flush()
    return issued


async def revoke_key(
    session: AsyncSession, key: ApiKey, *, reason: str = "revoked", now: dt.datetime | None = None
) -> ApiKey:
    """Idempotent: revoking an already-revoked key keeps the first revocation's timestamp."""
    if key.revoked_at is None:
        key.revoked_at = now or dt.datetime.now(tz=dt.UTC)
        key.revoked_reason = reason
        await session.flush()
        log.info("api key revoked", extra={"key_public_id": key.public_id, "reason": reason})
    return key


async def list_keys(session: AsyncSession, user_id: int) -> Sequence[ApiKey]:
    """Every key the account has ever held, newest first. Revoked keys stay visible.

    A revoked key that vanished from the list would take its usage history with it, and "what did
    the key we turned off actually do" is the first question asked after a leak.
    """
    return (
        (
            await session.execute(
                select(ApiKey)
                .where(ApiKey.user_id == user_id)
                .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
            )
        )
        .scalars()
        .all()
    )


async def load_key(session: AsyncSession, user_id: int, public_id: str) -> ApiKey | None:
    return (
        await session.execute(
            select(ApiKey).where(ApiKey.user_id == user_id, ApiKey.public_id == public_id)
        )
    ).scalar_one_or_none()


async def authenticate(
    session: AsyncSession, presented: str, *, now: dt.datetime | None = None
) -> ApiKey | None:
    """Resolve a presented ``X-API-Key`` to a live key row, or ``None``.

    ``None`` covers every failure — malformed, unknown prefix, wrong secret, revoked, expired —
    deliberately: a caller that could tell "this key was revoked" from "this key never existed"
    has an oracle for enumerating our key space, and the honest answer to all of them is the same
    401. The reason is logged, not returned.
    """
    try:
        parsed: ParsedKey = parse_key(presented)
    except InvalidKeyFormat:
        log.info("api key rejected", extra={"reason": "malformed"})
        return None

    row = (
        await session.execute(select(ApiKey).where(ApiKey.prefix == parsed.prefix))
    ).scalar_one_or_none()
    if row is None:
        log.info("api key rejected", extra={"reason": "unknown-prefix", "prefix": parsed.prefix})
        return None
    if not digests_match(parsed.secret, row.token_hash):
        log.warning(
            "api key rejected", extra={"reason": "secret-mismatch", "prefix": parsed.prefix}
        )
        return None
    if not row.is_active(now=now or dt.datetime.now(tz=dt.UTC)):
        log.info(
            "api key rejected",
            extra={"reason": "revoked-or-expired", "key_public_id": row.public_id},
        )
        return None
    return row


async def touch(session: AsyncSession, key: ApiKey, *, now: dt.datetime | None = None) -> None:
    """Advance ``last_used_at``, at most once per :data:`LAST_USED_RESOLUTION_SECONDS`."""
    moment = now or dt.datetime.now(tz=dt.UTC)
    previous = key.last_used_at
    if previous is not None:
        stamped = previous if previous.tzinfo is not None else previous.replace(tzinfo=dt.UTC)
        if (moment - stamped).total_seconds() < LAST_USED_RESOLUTION_SECONDS:
            return
    await session.execute(update(ApiKey).where(ApiKey.id == key.id).values(last_used_at=moment))
    key.last_used_at = moment


async def record_usage(
    session: AsyncSession,
    key_id: int,
    *,
    requests: int = 0,
    throttled: int = 0,
    day: dt.date | None = None,
) -> None:
    """Increment one key's counters for one UTC day.

    ``ON CONFLICT DO UPDATE`` with ``column + excluded.column`` rather than a read-modify-write:
    two concurrent requests must both be counted, and a SELECT-then-UPDATE would lose one.
    """
    when = day or dt.datetime.now(tz=dt.UTC).date()
    statement = insert(ApiKeyUsageDaily).values(
        api_key_id=key_id, date=when, requests=requests, throttled=throttled
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ApiKeyUsageDaily.api_key_id, ApiKeyUsageDaily.date],
            set_={
                "requests": ApiKeyUsageDaily.requests + statement.excluded.requests,
                "throttled": ApiKeyUsageDaily.throttled + statement.excluded.throttled,
                "updated_at": func.now(),
            },
        )
    )


async def usage_for_key(
    session: AsyncSession, key_id: int, *, since: dt.date, until: dt.date
) -> list[UsagePoint]:
    """The dashboard series for one key, oldest first. Days with no traffic are simply absent."""
    rows = (
        await session.execute(
            select(ApiKeyUsageDaily)
            .where(
                ApiKeyUsageDaily.api_key_id == key_id,
                ApiKeyUsageDaily.date >= since,
                ApiKeyUsageDaily.date <= until,
            )
            .order_by(ApiKeyUsageDaily.date)
        )
    ).scalars()
    return [
        UsagePoint(date=row.date, requests=int(row.requests), throttled=int(row.throttled))
        for row in rows
    ]


async def usage_totals(
    session: AsyncSession, key_ids: Sequence[int], *, since: dt.date
) -> dict[int, tuple[int, int]]:
    """``{api_key_id: (requests, throttled)}`` since a date, for the list view.

    One grouped query for every key on the page rather than one per key — the list is the page a
    user opens most, and N+1 on a page that renders ten keys is ten round trips for a number.
    """
    if not key_ids:
        return {}
    rows = (
        await session.execute(
            select(
                ApiKeyUsageDaily.api_key_id,
                func.coalesce(func.sum(ApiKeyUsageDaily.requests), 0),
                func.coalesce(func.sum(ApiKeyUsageDaily.throttled), 0),
            )
            .where(
                ApiKeyUsageDaily.api_key_id.in_(list(key_ids)),
                ApiKeyUsageDaily.date >= since,
            )
            .group_by(ApiKeyUsageDaily.api_key_id)
        )
    ).all()
    return {int(row[0]): (int(row[1]), int(row[2])) for row in rows}
