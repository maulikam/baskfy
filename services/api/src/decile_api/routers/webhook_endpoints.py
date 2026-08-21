"""``/webhook-endpoints`` — outbound webhook registration (Prompt 20 deliverable 4).

Not ``/webhooks``: that prefix already means "a gateway is posting *to* us" (``/webhooks/razorpay``)
and ``decile_api.ratelimit.is_webhook_path`` gives anything under it a 600/min per-IP bucket sized
for Razorpay's retries. Putting a user-owned CRUD surface in that bucket would meter it by address
rather than by account, so it gets its own prefix and the normal authenticated tier.

The signing secret is returned on creation **and** on rotation, and can be re-derived on demand by
its owner, because it is derived rather than stored — ``decile_api.webhooks``. That is a
deliberate difference from an API key, which is shown once: a key is a bearer credential we must
not be able to reproduce, while a webhook secret only ever verifies a message *we* sent.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Path, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from decile_api import webhooks as service
from decile_api.auth import AuthenticatedDep, Principal, settings_for
from decile_api.db import SessionDep
from decile_api.problems import Problem, ProblemType, not_found
from decile_api.schemas import (
    WebhookDeliveryListOut,
    WebhookDeliveryOut,
    WebhookEndpointCreate,
    WebhookEndpointListOut,
    WebhookEndpointOut,
    WebhookEndpointUpdate,
    WebhookEndpointWithSecretOut,
)
from decile_api.settings import Settings
from decile_core.models import Screen, WebhookDelivery, WebhookEndpoint

router = APIRouter(prefix="/webhook-endpoints", tags=["webhooks"])

#: How many recent deliveries the history returns.
HISTORY_LIMIT = 50

#: NOT IN THE BUNDLE. Ten endpoints per account, for the same reason there are ten API keys.
MAX_ENDPOINTS_PER_ACCOUNT = 10


def _bad_request(detail: str) -> Problem:
    return Problem(ProblemType.INVALID_SCREEN_DEFINITION, detail, errors=[{"message": detail}])


def _check_url(url: str, settings: Settings) -> str:
    """Refuse anything that is not an absolute ``http(s)`` URL to a named host.

    This is a *deliberate* server-side request: the operator's own service will POST wherever this
    says. A scheme other than http/https, or a URL with no host, is refused outright. Plain
    ``http`` is allowed only outside production, because a signed payload over cleartext still
    leaks the entries and exits to anyone on the path.

    It does **not** resolve the host or block private ranges. That is the real SSRF control and it
    is not implemented here — see ``docs/DECISIONS.md`` §20.9 and the module report; a webhook
    sender that can be pointed at ``169.254.169.254`` is a known gap that the feature's
    unreleased state, not this function, is currently containing.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise _bad_request("A webhook URL must be http:// or https://.")
    if not parsed.netloc:
        raise _bad_request("A webhook URL must name a host.")
    if parsed.scheme == "http" and settings.environment == "production":
        raise _bad_request("A webhook URL must be https:// in production.")
    return url


def _out(endpoint: WebhookEndpoint, screen: Screen) -> WebhookEndpointOut:
    events: list[str] = [event for event in endpoint.events if event in _KNOWN_EVENTS]
    return WebhookEndpointOut(
        public_id=endpoint.public_id,
        screen_public_id=screen.public_id,
        screen_name=screen.name,
        url=endpoint.url,
        events=[  # narrowed for the Literal in the schema
            "screen.entries" if event == "screen.entries" else "screen.exits" for event in events
        ],
        secret_version=endpoint.secret_version,
        is_active=endpoint.is_active,
        consecutive_failures=endpoint.consecutive_failures,
        disabled_at=endpoint.disabled_at,
        disabled_reason=endpoint.disabled_reason,
        last_delivery_at=endpoint.last_delivery_at,
        created_at=endpoint.created_at,
    )


_KNOWN_EVENTS = ("screen.entries", "screen.exits")


async def _screen_for(session: AsyncSession, endpoint: WebhookEndpoint) -> Screen:
    screen = (
        await session.execute(select(Screen).where(Screen.id == endpoint.screen_id))
    ).scalar_one_or_none()
    if screen is None:  # pragma: no cover - the FK cascades
        raise Problem(ProblemType.INTERNAL_ERROR, "endpoint points at a screen that is gone")
    return screen


async def _load(session: AsyncSession, principal: Principal, public_id: str) -> WebhookEndpoint:
    endpoint = (
        await session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.user_id == principal.require_user(),
                WebhookEndpoint.public_id == public_id,
            )
        )
    ).scalar_one_or_none()
    if endpoint is None:
        raise not_found("webhook endpoint", public_id)
    return endpoint


@router.get("", response_model=WebhookEndpointListOut, summary="List webhook endpoints")
async def list_endpoints(
    session: SessionDep, principal: AuthenticatedDep
) -> WebhookEndpointListOut:
    rows = (
        (
            await session.execute(
                select(WebhookEndpoint)
                .where(WebhookEndpoint.user_id == principal.require_user())
                .order_by(WebhookEndpoint.created_at.desc(), WebhookEndpoint.id.desc())
            )
        )
        .scalars()
        .all()
    )
    out: list[WebhookEndpointOut] = []
    for endpoint in rows:
        out.append(_out(endpoint, await _screen_for(session, endpoint)))
    return WebhookEndpointListOut(endpoints=out)


@router.post(
    "",
    response_model=WebhookEndpointWithSecretOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a webhook endpoint",
)
async def create_endpoint(
    body: WebhookEndpointCreate,
    session: SessionDep,
    principal: AuthenticatedDep,
    request: Request,
) -> WebhookEndpointWithSecretOut:
    settings: Settings = settings_for(request)
    user_id = principal.require_user()
    try:
        service.master_secret(settings)
    except service.WebhookNotConfigured as exc:
        # A 503 rather than a 500: the deployment is missing a secret, the request was fine.
        raise Problem(ProblemType.PIPELINE_DEGRADED, str(exc)) from exc
    _check_url(body.url, settings)
    if not body.events:
        raise _bad_request("An endpoint with no events would never fire.")

    screen = (
        await session.execute(select(Screen).where(Screen.public_id == body.screen_public_id))
    ).scalar_one_or_none()
    if screen is None or (screen.user_id is not None and screen.user_id != user_id):
        raise not_found("screen", body.screen_public_id)

    existing = (
        (await session.execute(select(WebhookEndpoint).where(WebhookEndpoint.user_id == user_id)))
        .scalars()
        .all()
    )
    if len(existing) >= MAX_ENDPOINTS_PER_ACCOUNT:
        raise _bad_request(
            f"This account already has {MAX_ENDPOINTS_PER_ACCOUNT} webhook endpoints."
        )
    if any(row.screen_id == screen.id and row.url == body.url for row in existing):
        raise _bad_request("That screen already posts to that URL.")

    endpoint = WebhookEndpoint(
        public_id=service.new_public_id(),
        user_id=user_id,
        screen_id=screen.id,
        url=body.url,
        secret_version=1,
        events=list(body.events),
        is_active=True,
        consecutive_failures=0,
    )
    session.add(endpoint)
    await session.flush()
    return WebhookEndpointWithSecretOut(
        endpoint=_out(endpoint, screen),
        signing_secret=service.signing_secret(settings, endpoint.public_id, 1),
    )


@router.patch(
    "/{public_id}", response_model=WebhookEndpointOut, summary="Update a webhook endpoint"
)
async def update_endpoint(
    body: WebhookEndpointUpdate,
    session: SessionDep,
    principal: AuthenticatedDep,
    request: Request,
    public_id: str = Path(...),
) -> WebhookEndpointOut:
    settings: Settings = settings_for(request)
    endpoint = await _load(session, principal, public_id)
    if body.url is not None:
        _check_url(body.url, settings)
        endpoint.url = body.url
    if body.events is not None:
        if not body.events:
            raise _bad_request("An endpoint with no events would never fire.")
        endpoint.events = list(body.events)
    if body.is_active is not None:
        endpoint.is_active = body.is_active
        if body.is_active:
            # Re-enabling clears the circuit breaker, otherwise the next failure disables it again
            # immediately and the owner cannot tell whether their fix worked.
            endpoint.consecutive_failures = 0
            endpoint.disabled_at = None
            endpoint.disabled_reason = None
    await session.flush()
    return _out(endpoint, await _screen_for(session, endpoint))


@router.post(
    "/{public_id}/rotate-secret",
    response_model=WebhookEndpointWithSecretOut,
    summary="Rotate a webhook signing secret",
)
async def rotate_secret(
    session: SessionDep,
    principal: AuthenticatedDep,
    request: Request,
    public_id: str = Path(...),
) -> WebhookEndpointWithSecretOut:
    """Bumps ``secret_version``. Every delivery after this signs with the new secret."""
    settings: Settings = settings_for(request)
    endpoint = await _load(session, principal, public_id)
    endpoint.secret_version += 1
    await session.flush()
    return WebhookEndpointWithSecretOut(
        endpoint=_out(endpoint, await _screen_for(session, endpoint)),
        signing_secret=service.signing_secret(
            settings, endpoint.public_id, endpoint.secret_version
        ),
    )


@router.delete(
    "/{public_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a webhook endpoint",
)
async def delete_endpoint(
    session: SessionDep, principal: AuthenticatedDep, public_id: str = Path(...)
) -> Response:
    endpoint = await _load(session, principal, public_id)
    await session.delete(endpoint)
    await session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{public_id}/deliveries",
    response_model=WebhookDeliveryListOut,
    summary="Recent deliveries for one endpoint",
)
async def endpoint_deliveries(
    session: SessionDep, principal: AuthenticatedDep, public_id: str = Path(...)
) -> WebhookDeliveryListOut:
    endpoint = await _load(session, principal, public_id)
    rows = (
        (
            await session.execute(
                select(WebhookDelivery)
                .where(WebhookDelivery.endpoint_id == endpoint.id)
                .order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc())
                .limit(HISTORY_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    return WebhookDeliveryListOut(
        deliveries=[
            WebhookDeliveryOut(
                id=row.id,
                event="screen.entries" if row.event == "screen.entries" else "screen.exits",
                status="delivered"
                if row.status == "delivered"
                else ("failed" if row.status == "failed" else "pending"),
                attempts=row.attempts,
                response_status=row.response_status,
                last_error=row.last_error,
                next_attempt_at=row.next_attempt_at,
                created_at=row.created_at,
                delivered_at=row.delivered_at,
            )
            for row in rows
        ]
    )
