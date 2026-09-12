"""``/keys`` — the API-key lifecycle and its usage dashboard (Prompt 20 deliverable 1).

    "API keys: creation, scoping (read-only), rotation, revocation, per-key rate limits, and a
     usage dashboard. Keys are hashed at rest and shown once."

Not in docs/07, which mentions ``X-API-Key`` in its header section and describes no endpoint that
issues one. These routes are still in the OpenAPI document and the generated client, for the same
reason ``/admin`` is: docs/02 rule 5 is "typed end to end", and a surface the web app calls is not
an exemption from it.

**Every route here requires a signed-in account, never a key.** A key cannot mint another key,
cannot rotate itself and cannot read its own usage. That is what keeps a leaked read-only
credential read-only: the blast radius of a key is exactly the data it can read, and never the
ability to extend its own life.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Path, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import api_keys as service
from baskfy_api.auth import AuthenticatedDep, Principal, settings_for
from baskfy_api.db import SessionDep
from baskfy_api.problems import Problem, ProblemType, not_found
from baskfy_api.schemas import (
    ApiKeyCreate,
    ApiKeyIssuedOut,
    ApiKeyListOut,
    ApiKeyOut,
    ApiKeyRevokeIn,
    ApiKeyUsageOut,
    ApiKeyUsagePointOut,
)
from baskfy_api.settings import Settings
from baskfy_core.api_keys import KEY_TOKEN_MARKER, Scope, normalise_scopes
from baskfy_core.models import ApiKey
from baskfy_core.public_api import DATA_REDISTRIBUTION_REVIEW

router = APIRouter(prefix="/keys", tags=["api-keys"])

#: The window the list view's counters cover, and the default for the usage endpoint.
USAGE_WINDOW_DAYS = 30

#: The longest window the usage endpoint will serve. A year and a day; beyond that the answer is
#: a report, not a dashboard.
MAX_USAGE_WINDOW_DAYS = 366


def _out(key: ApiKey, *, requests: int = 0, throttled: int = 0) -> ApiKeyOut:
    return ApiKeyOut(
        public_id=key.public_id,
        name=key.name,
        display=f"{KEY_TOKEN_MARKER}_{key.prefix}",
        scopes=list(key.scopes),
        rate_limit_per_minute=key.rate_limit_per_minute,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        expires_at=key.expires_at,
        revoked_at=key.revoked_at,
        revoked_reason=key.revoked_reason,
        requests_30d=requests,
        throttled_30d=throttled,
        active=key.is_active(now=dt.datetime.now(tz=dt.UTC)),
    )


def _key_out_with_usage(key: ApiKey, totals: dict[int, tuple[int, int]]) -> ApiKeyOut:
    requests, throttled = totals.get(key.id, (0, 0))
    return _out(key, requests=requests, throttled=throttled)


def _bad_request(detail: str) -> Problem:
    """docs/07's catalogue has one 400 (`invalid-screen-definition`) and no general one.

    Reused here rather than invented, the same way ``routers/portfolios.py`` reuses it for a
    malformed upload: a client that has to handle an undocumented ``type`` is worse off than one
    handling a documented type with a precise ``detail``. Recorded in ``docs/DECISIONS.md`` §20.7.
    """
    return Problem(ProblemType.INVALID_SCREEN_DEFINITION, detail, errors=[{"message": detail}])


async def _load(session: AsyncSession, principal: Principal, public_id: str) -> ApiKey:
    key = await service.load_key(session, principal.require_user(), public_id)
    if key is None:
        raise not_found("API key", public_id)
    return key


@router.get("", response_model=ApiKeyListOut, summary="List API keys")
async def list_api_keys(
    session: SessionDep, principal: AuthenticatedDep, request: Request
) -> ApiKeyListOut:
    """Every key the account holds, revoked ones included, with 30 days of usage."""
    settings: Settings = settings_for(request)
    keys = await service.list_keys(session, principal.require_user())
    since = dt.datetime.now(tz=dt.UTC).date() - dt.timedelta(days=USAGE_WINDOW_DAYS)
    totals = await service.usage_totals(session, [key.id for key in keys], since=since)
    return ApiKeyListOut(
        keys=[_key_out_with_usage(key, totals) for key in keys],
        public_api_enabled=settings.public_api_enabled and DATA_REDISTRIBUTION_REVIEW.signed_off,
        data_redistribution_signed_off=DATA_REDISTRIBUTION_REVIEW.signed_off,
    )


@router.post(
    "",
    response_model=ApiKeyIssuedOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an API key",
)
async def create_api_key(
    body: ApiKeyCreate, session: SessionDep, principal: AuthenticatedDep, request: Request
) -> ApiKeyIssuedOut:
    """Mint a key. **The response is the only place its secret ever appears.**"""
    settings: Settings = settings_for(request)
    try:
        scopes = normalise_scopes(body.scopes)
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    try:
        issued = await service.create_key(
            session,
            user_id=principal.require_user(),
            name=body.name,
            scopes=scopes,
            settings=settings,
            rate_limit_per_minute=body.rate_limit_per_minute,
            expires_at=body.expires_at,
        )
    except service.KeyLimitReached as exc:
        raise _bad_request(str(exc)) from exc
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    return ApiKeyIssuedOut(key=_out(issued.key), secret=issued.plaintext)


@router.post(
    "/{public_id}/rotate",
    response_model=ApiKeyIssuedOut,
    summary="Rotate an API key",
)
async def rotate_api_key(
    session: SessionDep,
    principal: AuthenticatedDep,
    request: Request,
    public_id: str = Path(...),
) -> ApiKeyIssuedOut:
    """Issue a replacement and revoke the original immediately — see ``baskfy_api.api_keys``."""
    settings: Settings = settings_for(request)
    key = await _load(session, principal, public_id)
    if not key.is_active(now=dt.datetime.now(tz=dt.UTC)):
        raise _bad_request("That key is already revoked; create a new one instead.")
    try:
        issued = await service.rotate_key(session, key, settings=settings)
    except service.KeyLimitReached as exc:
        raise _bad_request(str(exc)) from exc
    return ApiKeyIssuedOut(key=_out(issued.key), secret=issued.plaintext)


@router.post(
    "/{public_id}/revoke",
    response_model=ApiKeyOut,
    summary="Revoke an API key",
)
async def revoke_api_key(
    body: ApiKeyRevokeIn,
    session: SessionDep,
    principal: AuthenticatedDep,
    public_id: str = Path(...),
) -> ApiKeyOut:
    """Idempotent. The next request presenting this key is refused — there is no cached window."""
    key = await _load(session, principal, public_id)
    await service.revoke_key(session, key, reason=body.reason)
    return _out(key)


@router.delete(
    "/{public_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a revoked API key",
)
async def delete_api_key(
    session: SessionDep, principal: AuthenticatedDep, public_id: str = Path(...)
) -> Response:
    """Only a revoked key can be deleted.

    Deleting a live key would be a revocation dressed as a tidy-up, and it would take the usage
    history with it. Revoke first, look at what it did, then delete.
    """
    key = await _load(session, principal, public_id)
    if key.is_active(now=dt.datetime.now(tz=dt.UTC)):
        raise _bad_request("Revoke the key before deleting it.")
    await session.delete(key)
    await session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{public_id}/usage",
    response_model=ApiKeyUsageOut,
    summary="Daily usage for one API key",
)
async def api_key_usage(
    session: SessionDep,
    principal: AuthenticatedDep,
    public_id: str = Path(...),
    days: int = USAGE_WINDOW_DAYS,
) -> ApiKeyUsageOut:
    """The dashboard series. Days with no traffic are absent rather than zero-filled."""
    if days < 1 or days > MAX_USAGE_WINDOW_DAYS:
        raise _bad_request(f"days must be between 1 and {MAX_USAGE_WINDOW_DAYS}")
    key = await _load(session, principal, public_id)
    until = dt.datetime.now(tz=dt.UTC).date()
    since = until - dt.timedelta(days=days - 1)
    points = await service.usage_for_key(session, key.id, since=since, until=until)
    return ApiKeyUsageOut(
        public_id=key.public_id,
        **{"from": since},
        to=until,
        points=[
            ApiKeyUsagePointOut(date=point.date, requests=point.requests, throttled=point.throttled)
            for point in points
        ],
        total_requests=sum(point.requests for point in points),
        total_throttled=sum(point.throttled for point in points),
    )


#: Re-exported so tests can assert the scope list the router accepts is the registry's.
ACCEPTED_SCOPES = tuple(scope.value for scope in Scope)
