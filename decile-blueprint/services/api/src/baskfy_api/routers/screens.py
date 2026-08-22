"""``/screens/*`` — docs/07 §Screens and §"Running a screen" (Prompt 7 deliverable 2).

Visibility rules, stated once here because every route depends on them:

* A screen with ``user_id IS NULL`` is a system/example screen (docs/04). Everyone can read it,
  including anonymous callers, and nobody can edit it — docs/01 §1 calls them "read-only
  templates". ``POST /screens/{id}/duplicate`` is how a user makes one their own.
* A screen with a ``user_id`` is visible only to that user. Not-yours is reported as ``404``
  rather than ``403`` so the API does not confirm that a screen id exists.

Why the run endpoints return raw bytes
--------------------------------------
``ScreenResult.to_json`` is the response body, verbatim. docs/06 §"Determinism guarantee"
promises byte-identical results for the same definition, ``as_of`` and ``data_version``, and that
promise is only worth something if the bytes the client sees are the bytes that were hashed and
cached. Re-serialising through a Pydantic model would also route every ``numeric`` through
``float`` and turn ``13.00`` into ``13.0``, breaking CLAUDE.md house rule 8. ``response_model``
still documents the shape in OpenAPI, which is what the generated TypeScript client reads.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import idempotency
from baskfy_api.auth import AuthenticatedDep, Principal, PrincipalDep
from baskfy_api.csv_export import filename_for, stream_screen_csv
from baskfy_api.db import SessionDep
from baskfy_api.entitlements import Entitlements, EntitlementsDep, Feature
from baskfy_api.problems import (
    Problem,
    ProblemType,
    not_found,
    stale_data_version,
)
from baskfy_api.schemas import (
    DEFAULT_PAGE_SIZE,
    Limit,
    PreviewRequest,
    RunRequest,
    ScreenCreate,
    ScreenDuplicate,
    ScreenListOut,
    ScreenOut,
    ScreenRunPage,
    ScreenRunResponse,
    ScreenRunSummaryOut,
    ScreenUpdate,
)
from baskfy_api.screener import (
    AsOfResolution,
    ScreenRunResult,
    current_data_version,
    record_run,
    resolve_as_of,
    run_screen,
)
from baskfy_core.models import Screen, ScreenRun
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_core.screener import DEFAULT_RESULT_COLUMNS, resolve_columns
from baskfy_core.seed_data import DEFAULT_COLUMNS

log = logging.getLogger(__name__)

router = APIRouter(prefix="/screens", tags=["screens"])

#: ``exmpl0000001`` in the seed data is twelve characters; new ids match that length so the column
#: reads uniformly. 48 bits of entropy is ample for a non-guessable id that is also a lookup key.
PUBLIC_ID_BYTES = 6

JSON_MEDIA_TYPE = "application/json"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _new_public_id() -> str:
    return secrets.token_hex(PUBLIC_ID_BYTES)


def _is_visible(screen: Screen, principal: Principal) -> bool:
    return screen.user_id is None or screen.user_id == principal.user_id


def _is_editable(screen: Screen, principal: Principal) -> bool:
    return screen.user_id is not None and screen.user_id == principal.user_id


def _to_out(screen: Screen, principal: Principal) -> ScreenOut:
    return ScreenOut(
        public_id=screen.public_id,
        name=screen.name,
        definition=ScreenDefinition.model_validate(screen.definition),
        columns=list(screen.columns),
        is_example=screen.is_example,
        editable=_is_editable(screen, principal),
        created_at=screen.created_at,
        updated_at=screen.updated_at,
    )


async def _load_visible(session: AsyncSession, public_id: str, principal: Principal) -> Screen:
    screen = (
        await session.execute(select(Screen).where(Screen.public_id == public_id))
    ).scalar_one_or_none()
    if screen is None or not _is_visible(screen, principal):
        raise not_found("screen", public_id)
    return screen


async def _load_editable(session: AsyncSession, public_id: str, principal: Principal) -> Screen:
    screen = await _load_visible(session, public_id, principal)
    if not _is_editable(screen, principal):
        # docs/01 §1: the example screens are read-only templates. Reported as `not-found`
        # because the catalogue in docs/07 has no 403, and because "duplicate it first" is the
        # actionable answer rather than an argument about permissions.
        raise Problem(
            ProblemType.NOT_FOUND,
            f"Screen {public_id!r} is read-only; duplicate it to make an editable copy.",
        )
    return screen


def _validated_columns(requested: Sequence[str] | None) -> list[str]:
    """Reject an unknown column here, so a bad save is a 400 rather than a broken run later."""
    if requested is None:
        return list(DEFAULT_COLUMNS)
    resolve_columns(requested)
    return list(requested)


def _check_custom_columns(columns: Sequence[str], entitlements: Entitlements) -> None:
    """Prompt 7 §3 gates custom columns; the default set is not custom.

    Enforced where the value is delivered — on a run, a preview and an export — rather than on
    save, so that a lapsed subscriber keeps their saved screens intact and simply sees the default
    columns' worth of data until they renew.
    """
    if set(columns) - set(DEFAULT_RESULT_COLUMNS) - set(DEFAULT_COLUMNS):
        entitlements.require(Feature.CUSTOM_COLUMNS)


def _check_universe(definition: ScreenDefinition, entitlements: Entitlements) -> None:
    """Prompt 13 §5: the optional ₹0 tier is "a ₹0 free tier with a **limited universe**".

    A no-op for every plan that is not restricted — `Entitlements.universes` is None then — so
    this costs a set membership test on the paid path and exists only because the flag can be on.
    """
    entitlements.require_universe(definition.index)


def _check_historical(resolution: AsOfResolution, entitlements: Entitlements) -> None:
    """docs/01 §2.13's "Historical Ranks" — running the screen as of a past date."""
    if resolution.requested is not None and resolution.as_of != resolution.latest_published:
        entitlements.require(Feature.HISTORICAL_RANKS)


async def _check_data_version(session: AsyncSession, sent: int | None) -> int:
    """docs/07: "409 `stale-data-version` — client sent a `data_version` that no longer exists"."""
    current = await current_data_version(session)
    if sent is not None and sent != current:
        raise stale_data_version(sent, current)
    return current


def _json(outcome: ScreenRunResult) -> Response:
    headers = {
        "X-Baskfy-As-Of": outcome.resolution.as_of.isoformat(),
        "X-Baskfy-Data-Version": str(outcome.data_version),
        "X-Baskfy-Cache": "hit" if outcome.cache_hit else "miss",
    }
    return Response(content=outcome.payload, media_type=JSON_MEDIA_TYPE, headers=headers)


def _cache(request: Request) -> Redis | None:
    """The process-wide Redis client, or ``None`` when this deployment has none."""
    client = getattr(request.app.state, "cache", None)
    return client if isinstance(client, Redis) else None


async def _replayed_screen(
    session: AsyncSession,
    principal: Principal,
    cache: Redis | None,
    scope: str,
    key: str | None,
) -> ScreenOut | None:
    """The screen a previous request with this ``Idempotency-Key`` created, if it still exists.

    "If it still exists" is load-bearing. A replay record outlives the resource it names — the
    user can delete the screen, and the record's day-long TTL keeps pointing at it. Answering 404
    to a *retry* would be a worse lie than simply creating the screen again, so an unresolvable
    record is treated as no record at all.
    """
    remembered = await idempotency.replay(cache, scope, key)
    if remembered is None:
        return None
    screen = (
        await session.execute(select(Screen).where(Screen.public_id == remembered))
    ).scalar_one_or_none()
    if screen is None or not _is_visible(screen, principal):
        return None
    return _to_out(screen, principal)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=ScreenListOut, summary="List screens")
async def list_screens(session: SessionDep, principal: PrincipalDep) -> ScreenListOut:
    """docs/07: "GET /screens -> user screens + example screens"."""
    condition = (
        Screen.user_id.is_(None)
        if principal.user_id is None
        else (Screen.user_id.is_(None) | (Screen.user_id == principal.user_id))
    )
    rows = (
        (
            await session.execute(
                select(Screen)
                .where(condition)
                .order_by(Screen.is_example.desc(), Screen.updated_at.desc(), Screen.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return ScreenListOut(data=[_to_out(screen, principal) for screen in rows])


@router.post(
    "", response_model=ScreenOut, status_code=status.HTTP_201_CREATED, summary="Create a screen"
)
async def create_screen(  # noqa: PLR0913, PLR0917 - FastAPI injects one parameter per dependency
    request: Request,
    body: ScreenCreate,
    session: SessionDep,
    principal: AuthenticatedDep,
    entitlements: EntitlementsDep,
    idempotency_key: Annotated[str | None, Header(alias=idempotency.HEADER)] = None,
) -> ScreenOut:
    user_id = principal.require_user()
    cache = _cache(request)
    replayed = await _replayed_screen(
        session, principal, cache, f"screen:create:{user_id}", idempotency_key
    )
    if replayed is not None:
        return replayed

    count = (
        await session.execute(
            select(func.count()).select_from(Screen).where(Screen.user_id == user_id)
        )
    ).scalar_one()
    if count >= entitlements.max_screens:
        raise Problem(
            ProblemType.PAYMENT_REQUIRED,
            f"Your plan allows {entitlements.max_screens} saved screens.",
            feature="max_screens",
            upgrade_url="/pricing",
        )

    screen = Screen(
        public_id=_new_public_id(),
        user_id=user_id,
        name=body.name,
        definition=body.definition.model_dump(mode="json", by_alias=True),
        columns=_validated_columns(body.columns),
        is_example=False,
    )
    session.add(screen)
    await session.flush()
    await idempotency.remember(cache, f"screen:create:{user_id}", idempotency_key, screen.public_id)
    return _to_out(screen, principal)


@router.get("/{public_id}", response_model=ScreenOut, summary="Fetch one screen")
async def get_screen(public_id: str, session: SessionDep, principal: PrincipalDep) -> ScreenOut:
    return _to_out(await _load_visible(session, public_id, principal), principal)


@router.patch("/{public_id}", response_model=ScreenOut, summary="Update a screen")
async def update_screen(
    public_id: str,
    body: ScreenUpdate,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> ScreenOut:
    screen = await _load_editable(session, public_id, principal)
    if body.name is not None:
        screen.name = body.name
    if body.definition is not None:
        screen.definition = body.definition.model_dump(mode="json", by_alias=True)
    if body.columns is not None:
        screen.columns = _validated_columns(body.columns)
    screen.updated_at = dt.datetime.now(tz=dt.UTC)
    await session.flush()
    return _to_out(screen, principal)


@router.delete("/{public_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a screen")
async def delete_screen(
    public_id: str, session: SessionDep, principal: AuthenticatedDep
) -> Response:
    screen = await _load_editable(session, public_id, principal)
    await session.execute(delete(Screen).where(Screen.id == screen.id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{public_id}/duplicate",
    response_model=ScreenOut,
    status_code=status.HTTP_201_CREATED,
    summary="Duplicate a screen",
)
async def duplicate_screen(  # noqa: PLR0913, PLR0917 - FastAPI injects one parameter per dependency
    request: Request,
    public_id: str,
    session: SessionDep,
    principal: AuthenticatedDep,
    body: ScreenDuplicate | None = None,
    idempotency_key: Annotated[str | None, Header(alias=idempotency.HEADER)] = None,
) -> ScreenOut:
    """The only way to edit an example screen: take a copy you own."""
    user_id = principal.require_user()
    source = await _load_visible(session, public_id, principal)
    cache = _cache(request)
    scope = f"screen:duplicate:{user_id}:{public_id}"
    replayed = await _replayed_screen(session, principal, cache, scope, idempotency_key)
    if replayed is not None:
        return replayed

    copy = Screen(
        public_id=_new_public_id(),
        user_id=user_id,
        name=(body.name if body and body.name else f"{source.name} (copy)"),
        definition=dict(source.definition),
        columns=list(source.columns),
        is_example=False,
    )
    session.add(copy)
    await session.flush()
    await idempotency.remember(cache, scope, idempotency_key, copy.public_id)
    return _to_out(copy, principal)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


@router.post(
    "/preview",
    response_model=ScreenRunResponse,
    summary="Run an unsaved definition",
)
async def preview_screen(
    request: Request,
    body: PreviewRequest,
    session: SessionDep,
    principal: PrincipalDep,
    entitlements: EntitlementsDep,
) -> Response:
    """docs/07: "run an unsaved definition (the edit form's live preview)".

    Nothing is persisted — there is no screen to attribute a ``screen_run`` row to, and a preview
    is a keystroke, not an event worth auditing.
    """
    del principal
    data_version = await _check_data_version(session, body.data_version)
    columns = _validated_columns(body.columns) if body.columns is not None else []
    _check_custom_columns(columns, entitlements)
    _check_universe(body.definition, entitlements)
    resolution = await resolve_as_of(session, body.as_of or body.definition.historical_date)
    _check_historical(resolution, entitlements)

    outcome = await run_screen(
        session,
        body.definition,
        requested_as_of=resolution.requested,
        columns=columns,
        cache=_cache(request),
    )
    del data_version
    return _json(outcome)


@router.post(
    "/{public_id}/run",
    response_model=ScreenRunResponse,
    summary="Run a saved screen",
)
async def run_saved_screen(  # noqa: PLR0913, PLR0917 - FastAPI injects one parameter per dependency
    request: Request,
    public_id: str,
    session: SessionDep,
    principal: PrincipalDep,
    entitlements: EntitlementsDep,
    body: RunRequest | None = None,
) -> Response:
    """docs/07 §"Running a screen"."""
    payload = body or RunRequest()
    screen = await _load_visible(session, public_id, principal)
    await _check_data_version(session, payload.data_version)

    definition = payload.override_definition or ScreenDefinition.model_validate(screen.definition)
    columns = list(screen.columns)
    _check_custom_columns(columns, entitlements)
    _check_universe(definition, entitlements)

    requested = payload.as_of or definition.historical_date
    resolution = await resolve_as_of(session, requested)
    _check_historical(resolution, entitlements)

    outcome = await run_screen(
        session,
        definition,
        requested_as_of=requested,
        columns=columns,
        cache=_cache(request),
    )
    if outcome.result is not None:
        await record_run(session, screen.id, outcome.result, definition.definition_hash())
    return _json(outcome)


@router.get("/{public_id}/csv", summary="Export a screen as CSV", response_class=StreamingResponse)
async def export_screen_csv(
    public_id: str,
    session: SessionDep,
    principal: PrincipalDep,
    entitlements: EntitlementsDep,
    as_of: Annotated[dt.date | None, Query()] = None,
) -> StreamingResponse:
    """docs/07: "GET /screens/{public_id}/csv?as_of=… -> text/csv (entitlement-gated)"."""
    entitlements.require(Feature.EXPORT_CSV)
    screen = await _load_visible(session, public_id, principal)
    definition = ScreenDefinition.model_validate(screen.definition)
    _check_universe(definition, entitlements)
    resolution = await resolve_as_of(session, as_of or definition.historical_date)
    _check_historical(resolution, entitlements)
    columns = list(screen.columns)
    _check_custom_columns(columns, entitlements)

    filename = filename_for(screen.name, resolution.as_of)
    return StreamingResponse(
        stream_screen_csv(session, definition, as_of=resolution.as_of),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Baskfy-As-Of": resolution.as_of.isoformat(),
            "X-Baskfy-Data-Version": str(await current_data_version(session)),
        },
    )


@router.get("/{public_id}/runs", response_model=ScreenRunPage, summary="Past runs of a screen")
async def list_screen_runs(
    public_id: str,
    session: SessionDep,
    principal: PrincipalDep,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> ScreenRunPage:
    """docs/07: "historical run summaries", cursor-paginated per §Conventions.

    Keyset pagination on ``screen_run.id`` descending. An offset would skip or repeat rows as new
    runs are recorded between pages, which for an audit trail is worse than useless.
    """
    screen = await _load_visible(session, public_id, principal)
    statement = select(ScreenRun).where(ScreenRun.screen_id == screen.id)
    if cursor is not None:
        statement = statement.where(ScreenRun.id < _decode_cursor(cursor))
    rows = (
        (await session.execute(statement.order_by(ScreenRun.id.desc()).limit(limit + 1)))
        .scalars()
        .all()
    )
    page = rows[:limit]
    return ScreenRunPage(
        data=[
            ScreenRunSummaryOut(
                as_of=row.as_of,
                definition_hash=row.definition_hash,
                result_count=row.result_count,
                created_at=row.created_at,
            )
            for row in page
        ],
        next_cursor=str(page[-1].id) if len(rows) > limit and page else None,
    )


def _decode_cursor(cursor: str) -> int:
    try:
        return int(cursor)
    except ValueError as exc:
        raise Problem(
            ProblemType.NOT_FOUND, f"{cursor!r} is not a valid pagination cursor."
        ) from exc
