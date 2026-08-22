"""Outbound webhooks for screen entries and exits — Prompt 20 deliverable 4.

    "Webhooks for entries/exits on a screen, with HMAC signing and retry with backoff."

Three things live here: how an endpoint's signing secret comes to exist, how a request is signed,
and how a delivery is retried. The rows are ``baskfy_core.models.integrations``; the schedule that
drives attempts is ``baskfy_worker.tasks.webhooks``.

The signing scheme
------------------
Header ``X-Baskfy-Signature: t=<unix seconds>,v1=<hex sha256 hmac>``, over
``"<t>.<raw request body>"``. It is Stripe's scheme, deliberately: it is the one a receiver is
most likely to already have code for, and the timestamp inside the signed payload is what makes a
captured delivery un-replayable after the receiver's tolerance window. The alternative — signing
the body alone — is simpler and lets anyone who ever saw one delivery repeat it forever.

The secret is **derived, never stored**::

    secret = "whsec_" + HMAC-SHA256(master, f"{endpoint.public_id}:{secret_version}")[:32 hex]

so a database dump contains no signing key, rotation is ``secret_version += 1``, and the value can
be shown again to its owner without us keeping a decryptable copy. The master is
``BASKFY_WEBHOOK_SIGNING_SECRET``, falling back to the JWT secret (which production already
requires to be present and at least 32 bytes). A deployment with neither cannot create an
endpoint — :func:`master_secret` raises rather than signing with an empty key, which would make
every signature forgeable by anyone who read this file.

The retry schedule
------------------
Exponential, from settings: ``base * factor**(attempt-1)`` — 30s, 2m, 8m, 32m, 2h8m by default,
five attempts over about three hours. A 2xx is success. A 4xx that is not 408 or 429 is
**permanent**: the receiver understood the request and refused it, and retrying a 401 twenty
times is how a misconfigured integration becomes a self-inflicted denial of service. Everything
else — a timeout, a connection error, a 5xx, a 429 — is retried until the attempts run out.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from typing import Final

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.settings import Settings
from baskfy_core.models import WebhookDelivery, WebhookEndpoint
from baskfy_core.models.base import JsonObject

log = logging.getLogger(__name__)

__all__ = [
    "SIGNATURE_HEADER",
    "DeliveryOutcome",
    "attempt_delivery",
    "backoff_seconds",
    "due_deliveries",
    "enqueue",
    "signature_for",
    "signing_secret",
    "verify_signature",
]

#: What a receiver reads. ``X-`` prefixed because RFC 6648 deprecates the convention for
#: *standardised* headers and this is not one; every gateway in this space still spells it so.
SIGNATURE_HEADER: Final = "X-Baskfy-Signature"
EVENT_HEADER: Final = "X-Baskfy-Event"
DELIVERY_HEADER: Final = "X-Baskfy-Delivery"
#: Receivers are told to reject a signature older than this. Five minutes is Stripe's tolerance
#: and is long enough to survive a clock that is a couple of minutes out.
SIGNATURE_TOLERANCE_SECONDS: Final = 300

SECRET_DISPLAY_PREFIX: Final = "whsec_"
#: Hex characters kept from the derivation. 32 hex = 128 bits, which is plenty for an HMAC key
#: and short enough to paste into a config file without wrapping.
SECRET_HEX_LENGTH: Final = 32

#: Public id bytes for an endpoint row.
PUBLIC_ID_BYTES: Final = 12

#: A 4xx that still deserves a retry: the receiver is asking us to slow down or timed out.
RETRYABLE_CLIENT_STATUSES: Final[frozenset[int]] = frozenset({408, 425, 429})

_CLIENT_ERROR_FLOOR: Final = 400
_SERVER_ERROR_FLOOR: Final = 500

#: How much of a failing receiver's response we keep. A body is attacker-controlled text; we store
#: enough to debug and not enough to be used as storage.
MAX_ERROR_CHARS: Final = 500


class WebhookNotConfigured(RuntimeError):
    """No master signing secret, so no endpoint can be created or signed for."""


def new_public_id() -> str:
    return secrets.token_hex(PUBLIC_ID_BYTES)


def master_secret(settings: Settings) -> str:
    """What every endpoint's secret derives from. Raises rather than signing with an empty key."""
    master = settings.webhook_signing_secret or settings.jwt_secret
    if not master:
        raise WebhookNotConfigured(
            "BASKFY_WEBHOOK_SIGNING_SECRET (or BASKFY_JWT_SECRET) is empty; webhooks cannot be "
            "signed, so no endpoint can be created."
        )
    return master


def signing_secret(settings: Settings, public_id: str, secret_version: int) -> str:
    """The secret for one endpoint at one rotation. Deterministic — see the module docstring."""
    material = f"{public_id}:{secret_version}".encode()
    derived = hmac.new(master_secret(settings).encode("utf-8"), material, hashlib.sha256)
    return f"{SECRET_DISPLAY_PREFIX}{derived.hexdigest()[:SECRET_HEX_LENGTH]}"


def signature_for(secret: str, body: bytes, *, timestamp: int) -> str:
    """``t=<unix>,v1=<hex>`` over ``"<t>.<body>"``."""
    signed = f"{timestamp}.".encode() + body
    mac = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify_signature(
    secret: str, body: bytes, header: str, *, now: int, tolerance: int = SIGNATURE_TOLERANCE_SECONDS
) -> bool:
    """The receiver's half, written here so the documentation's example is executable.

    Not used by the service — nothing here receives its own webhooks — but it is what the
    interactive reference's Python sample shows, and a sample that has never run is a sample that
    is wrong. ``services/api/tests/test_api_webhooks.py`` drives it against :func:`signature_for`.
    """
    parts: dict[str, str] = {}
    for piece in header.split(","):
        name, separator, value = piece.partition("=")
        if separator:
            parts[name.strip()] = value.strip()
    raw_timestamp = parts.get("t")
    presented = parts.get("v1")
    if raw_timestamp is None or presented is None:
        return False
    try:
        timestamp = int(raw_timestamp)
    except ValueError:
        return False
    if abs(now - timestamp) > tolerance:
        return False
    expected = signature_for(secret, body, timestamp=timestamp).split("v1=", 1)[1]
    return hmac.compare_digest(expected, presented)


def backoff_seconds(attempt: int, settings: Settings) -> int:
    """Seconds to wait before attempt number ``attempt`` (1-based, so attempt 1 is immediate).

    ``attempt`` is the count of attempts *already made*; the first retry is attempt 1 having
    failed, so the wait is ``base``.
    """
    if attempt < 1:
        return 0
    growth = settings.webhook_backoff_factor ** (attempt - 1)
    return int(settings.webhook_backoff_base_seconds * growth)


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """What one attempt did, for the caller and for the log."""

    delivered: bool
    status: int | None
    error: str | None
    retrying: bool
    next_attempt_at: dt.datetime | None


async def enqueue(  # noqa: PLR0913 - the endpoint, the event, its body, its key and the clock
    session: AsyncSession,
    endpoint: WebhookEndpoint,
    *,
    event: str,
    payload: JsonObject,
    idempotency_key: str,
    now: dt.datetime | None = None,
) -> None:
    """Record a delivery to be attempted, once per ``(endpoint, idempotency_key)``.

    ``ON CONFLICT DO NOTHING``: a night's dispatch that runs twice must not send twice
    (CLAUDE.md house rule 7), and the constraint is the thing enforcing it rather than a check
    that races.
    """
    moment = now or dt.datetime.now(tz=dt.UTC)
    statement = (
        insert(WebhookDelivery)
        .values(
            endpoint_id=endpoint.id,
            event=event,
            idempotency_key=idempotency_key,
            payload=payload,
            status="pending",
            attempts=0,
            next_attempt_at=moment,
        )
        .on_conflict_do_nothing(
            index_elements=[WebhookDelivery.endpoint_id, WebhookDelivery.idempotency_key]
        )
    )
    await session.execute(statement)


async def due_deliveries(
    session: AsyncSession, *, limit: int = 100, now: dt.datetime | None = None
) -> list[tuple[WebhookDelivery, WebhookEndpoint]]:
    """Pending deliveries whose next attempt is due, oldest first, with their endpoint."""
    moment = now or dt.datetime.now(tz=dt.UTC)
    rows = (
        await session.execute(
            select(WebhookDelivery, WebhookEndpoint)
            .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
            .where(
                WebhookDelivery.status == "pending",
                WebhookDelivery.next_attempt_at <= moment,
                WebhookEndpoint.is_active.is_(True),
            )
            .order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.id)
            .limit(limit)
        )
    ).all()
    return [(row[0], row[1]) for row in rows]


def _is_permanent(status: int) -> bool:
    """A refusal the receiver meant. See the module docstring for why these are not retried."""
    if status not in range(_CLIENT_ERROR_FLOOR, _SERVER_ERROR_FLOOR):
        return False
    return status not in RETRYABLE_CLIENT_STATUSES


async def attempt_delivery(  # noqa: PLR0913 - the row, its endpoint, config, transport and clock
    session: AsyncSession,
    delivery: WebhookDelivery,
    endpoint: WebhookEndpoint,
    *,
    settings: Settings,
    client: httpx.AsyncClient,
    now: dt.datetime | None = None,
) -> DeliveryOutcome:
    """POST once, sign it, and record what happened.

    The transport is injected so the suite can drive an ``httpx.MockTransport`` — the whole test
    run is network-blocked (``network_guard.py``), and a webhook sender that could only be tested
    against a real socket would not be tested at all.
    """
    moment = now or dt.datetime.now(tz=dt.UTC)
    body = json.dumps(delivery.payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    secret = signing_secret(settings, endpoint.public_id, endpoint.secret_version)
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: signature_for(secret, body, timestamp=int(moment.timestamp())),
        EVENT_HEADER: delivery.event,
        DELIVERY_HEADER: str(delivery.id),
        "User-Agent": "Baskfy-Webhooks/1",
    }

    delivery.attempts += 1
    status: int | None = None
    error: str | None = None
    try:
        response = await client.post(
            endpoint.url,
            content=body,
            headers=headers,
            timeout=settings.webhook_timeout_seconds,
        )
        status = response.status_code
        if status >= _CLIENT_ERROR_FLOOR:
            error = f"receiver answered {status}"
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]

    if status is not None and status < _CLIENT_ERROR_FLOOR:
        delivery.status = "delivered"
        delivery.delivered_at = moment
        delivery.next_attempt_at = None
        delivery.response_status = status
        delivery.last_error = None
        endpoint.consecutive_failures = 0
        endpoint.last_delivery_at = moment
        await session.flush()
        return DeliveryOutcome(
            delivered=True, status=status, error=None, retrying=False, next_attempt_at=None
        )

    delivery.response_status = status
    delivery.last_error = (error or "no response")[:MAX_ERROR_CHARS]
    permanent = status is not None and _is_permanent(status)
    exhausted = delivery.attempts >= settings.webhook_max_attempts
    if permanent or exhausted:
        delivery.status = "failed"
        delivery.next_attempt_at = None
        next_at = None
        retrying = False
    else:
        delivery.next_attempt_at = moment + dt.timedelta(
            seconds=backoff_seconds(delivery.attempts, settings)
        )
        next_at = delivery.next_attempt_at
        retrying = True

    endpoint.consecutive_failures += 1
    if endpoint.consecutive_failures >= settings.webhook_failure_threshold:
        endpoint.is_active = False
        endpoint.disabled_at = moment
        endpoint.disabled_reason = f"{endpoint.consecutive_failures} consecutive failed deliveries"
        log.warning(
            "webhook endpoint disabled",
            extra={"endpoint_public_id": endpoint.public_id},
        )
    await session.flush()
    log.info(
        "webhook delivery failed",
        extra={
            "endpoint_public_id": endpoint.public_id,
            "attempts": delivery.attempts,
            "status": status,
            "retrying": retrying,
        },
    )
    return DeliveryOutcome(
        delivered=False,
        status=status,
        error=delivery.last_error,
        retrying=retrying,
        next_attempt_at=next_at,
    )
