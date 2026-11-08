"""Sole-operator HTTP contract for curated version publishing and timelines."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Body, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from baskfy_api.curated_publishing import (
    TIMELINE_DEFAULT_LIMIT,
    TIMELINE_MAX_LIMIT,
    CuratedPublishingError,
    ForeignInvestmentError,
    PublishedVersionOut,
    PublishingConflictError,
    PublishingNotFoundError,
    PublishingSourceError,
    VersionTimelineOut,
    get_version_timeline,
    publish_manual_version,
    publish_scan_version,
)
from baskfy_api.db import SessionDep
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.routers.curated_catalog import Slug, SoleOperatorDep
from baskfy_core.curated_baskets import BasketValidationError, ConstituentDraft, VersionDraft
from baskfy_core.screener import canonical_json

router = APIRouter(tags=["curated-publishing"])
JSON_MEDIA_TYPE: Final = "application/json"
MAX_NOTES_LENGTH: Final = 20_000
MAX_SEGMENT_LENGTH: Final = 80
MAX_MANUAL_CONSTITUENTS: Final = 500


class ManualConstituentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instrument_id: int = Field(gt=0)
    segment: str = Field(min_length=1, max_length=MAX_SEGMENT_LENGTH)
    weight: Decimal = Field(gt=0, le=1, decimal_places=4)


class ManualPublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effective_date: dt.date
    constituents: list[ManualConstituentIn] = Field(
        min_length=1, max_length=MAX_MANUAL_CONSTITUENTS
    )
    notes_md: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)


class ScanPublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    screen_run_id: int = Field(gt=0)
    notes_md: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)


def _json(model: BaseModel, *, status_code: int = status.HTTP_200_OK) -> Response:
    return Response(
        content=canonical_json(model.model_dump(mode="python")),
        media_type=JSON_MEDIA_TYPE,
        status_code=status_code,
    )


def _problem(exc: Exception) -> Problem:
    if isinstance(exc, PublishingNotFoundError):
        return Problem(ProblemType.NOT_FOUND, "Not found.")
    if isinstance(exc, (PublishingConflictError, ForeignInvestmentError)):
        return Problem(ProblemType.STALE_DATA_VERSION, str(exc))
    if isinstance(exc, (PublishingSourceError, BasketValidationError, ValueError)):
        return Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            str(exc),
            errors=[{"field": "version", "message": str(exc)}],
        )
    if isinstance(exc, CuratedPublishingError):  # pragma: no cover - future typed subclasses
        return Problem(ProblemType.INTERNAL_ERROR, "Version publishing was refused.")
    raise exc


@router.post(
    "/catalog/{slug}/versions/manual",
    response_model=PublishedVersionOut,
    responses={200: {"model": PublishedVersionOut}},
    status_code=status.HTTP_201_CREATED,
    summary="Publish a hand-authored immutable basket version",
)
async def publish_manual(
    slug: Slug,
    session: SessionDep,
    principal: SoleOperatorDep,
    payload: Annotated[ManualPublishIn, Body()],
) -> Response:
    draft = VersionDraft(
        effective_date=payload.effective_date,
        constituents=tuple(
            ConstituentDraft(
                instrument_id=row.instrument_id,
                segment=row.segment.strip(),
                weight=row.weight,
            )
            for row in payload.constituents
        ),
        notes_md=payload.notes_md,
        source_scan_run_id=None,
    )
    try:
        result = await publish_manual_version(session, principal.require_user(), slug, draft)
    except (CuratedPublishingError, BasketValidationError, ValueError) as exc:
        raise _problem(exc) from exc
    code = status.HTTP_200_OK if result.idempotent_replay else status.HTTP_201_CREATED
    return _json(result, status_code=code)


@router.post(
    "/catalog/{slug}/versions/from-scan",
    response_model=PublishedVersionOut,
    responses={200: {"model": PublishedVersionOut}},
    status_code=status.HTTP_201_CREATED,
    summary="Publish an immutable version from the reserved private scan",
)
async def publish_from_scan(
    slug: Slug,
    session: SessionDep,
    principal: SoleOperatorDep,
    payload: Annotated[ScanPublishIn, Body()],
) -> Response:
    try:
        result = await publish_scan_version(
            session,
            principal.require_user(),
            slug,
            payload.screen_run_id,
            payload.notes_md,
        )
    except (CuratedPublishingError, BasketValidationError, ValueError) as exc:
        raise _problem(exc) from exc
    code = status.HTTP_200_OK if result.idempotent_replay else status.HTTP_201_CREATED
    return _json(result, status_code=code)


@router.get(
    "/catalog/{slug}/timeline",
    response_model=VersionTimelineOut,
    summary="Read a bounded chronological basket-version timeline",
)
async def timeline(
    slug: Slug,
    session: SessionDep,
    principal: SoleOperatorDep,
    limit: Annotated[
        int,
        Query(ge=1, le=TIMELINE_MAX_LIMIT),
    ] = TIMELINE_DEFAULT_LIMIT,
    before_version_no: Annotated[int | None, Query(gt=0)] = None,
) -> Response:
    del principal  # Authentication and sole-operator scoping are the dependency's whole job.
    try:
        result = await get_version_timeline(
            session,
            slug,
            limit=limit,
            before_version_no=before_version_no,
        )
    except (CuratedPublishingError, ValueError) as exc:
        raise _problem(exc) from exc
    return _json(result)
