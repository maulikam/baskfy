"""``/admin/*`` — the staff surface (PROMPTS.md Prompt 17 deliverable 4).

docs/09 §Observability: "`pipeline_run_step` is the operator UI; expose it at `/admin/pipeline`
behind staff auth."

Every route on this router depends on :func:`decile_api.auth.require_staff`, declared once on the
router rather than once per route — a staff gate that has to be remembered at each new endpoint is
a staff gate that will eventually be forgotten at one. A non-staff caller gets a 404, not a 403;
see ``require_staff`` for why.

Not in docs/07
--------------
docs/07 describes the product's API and says nothing about ``/admin``. These routes are still in
the OpenAPI document and the generated client, because docs/02 rule 5 is "typed end to end ...
no hand-written fetch types" and a staff page is not an exemption from it. They are tagged
``admin`` so a reader of the spec can see at a glance which part of it is not the product.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from decile_api import admin
from decile_api.auth import StaffDep, require_staff
from decile_api.db import SessionDep
from decile_api.routers import public
from decile_api.schemas import (
    AdminActionListOut,
    AdminActionOut,
    AdminUserDetailOut,
    AdminUserListOut,
    AdminUserOut,
    DataVersionListOut,
    DataVersionOut,
    EntitlementOverrideIn,
    EntitlementOverrideOut,
    EntitlementsOut,
    PipelineRunDetailOut,
    PipelineRunListOut,
    PipelineStepOut,
    ProviderHealthListOut,
    ProviderHealthOut,
    PublicApiGateOut,
    TaskAcceptedOut,
)
from decile_api.settings import Settings, get_settings
from decile_core.models import AdminAction, AppUser, EntitlementOverride, PipelineRun
from decile_core.public_api import (
    DATA_REDISTRIBUTION_REVIEW,
    PUBLIC_API_PREFIX,
    PUBLIC_API_VERSION,
    PUBLIC_COLUMNS,
    WITHHELD_COLUMNS,
)

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_staff)])


def _settings(request: Request) -> Settings:
    resolved = getattr(request.app.state, "settings", None)
    return resolved if isinstance(resolved, Settings) else get_settings()


def _run_out(entry: admin.RunWithSteps) -> PipelineRunDetailOut:
    return PipelineRunDetailOut(
        id=entry.run.id,
        trade_date=entry.run.trade_date,
        status=entry.run.status,
        started_at=entry.run.started_at,
        finished_at=entry.run.finished_at,
        data_version=entry.run.data_version,
        publish_latency_seconds=entry.publish_latency_seconds,
        steps=[
            PipelineStepOut(
                step=step.step,
                status=step.status,
                rows_in=step.rows_in,
                rows_out=step.rows_out,
                duration_ms=step.duration_ms,
                detail=step.error,
            )
            for step in entry.steps
        ],
    )


@router.get("/pipeline/runs", response_model=PipelineRunListOut, summary="Pipeline run history")
async def list_pipeline_runs(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=admin.MAX_RUN_LIMIT)] = admin.DEFAULT_RUN_LIMIT,
) -> PipelineRunListOut:
    """docs/09 §Observability's operator UI, newest first, each run with its ten step rows."""
    entries = await admin.list_runs(session, limit=limit)
    return PipelineRunListOut(data=[_run_out(entry) for entry in entries])


@router.get(
    "/pipeline/runs/{run_id}",
    response_model=PipelineRunDetailOut,
    summary="One pipeline run, with per-step detail",
)
async def get_pipeline_run(
    session: SessionDep, run_id: Annotated[int, Path(ge=1)]
) -> PipelineRunDetailOut:
    return _run_out(await admin.load_run(session, run_id))


@router.post(
    "/pipeline/runs/{trade_date}/rerun",
    response_model=TaskAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-run the nightly pipeline for one trade date",
)
async def rerun_pipeline(
    request: Request,
    session: SessionDep,
    staff: StaffDep,
    trade_date: dt.date,
) -> TaskAcceptedOut:
    """The "re-run button". 202, not 200: the chain runs in a worker and takes minutes.

    Keyed by *trade date* rather than by run id, because re-running "run 41" and re-running
    "18 August" are the same request and the date is the one the operator has.
    """
    queue = getattr(request.app.state, "task_queue", None)
    result = await admin.enqueue_rerun(session, staff, trade_date, queue)
    return TaskAcceptedOut(
        task=admin.NIGHTLY_TASK_NAME,
        task_id=str(result["task_id"]),
        target=trade_date.isoformat(),
        detail="The nightly chain has been queued. Watch the run history for its steps.",
    )


@router.post(
    "/instruments/{symbol}/reprocess",
    response_model=TaskAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Rebuild one instrument's adjusted price history",
)
async def reprocess_instrument(
    request: Request,
    session: SessionDep,
    staff: StaffDep,
    symbol: Annotated[str, Path(min_length=1, max_length=64)],
) -> TaskAcceptedOut:
    """docs/09 §"Adjustment algorithm"'s rebuild rule, on demand.

    Idempotent by construction: a healthy history is rewritten to identical values, which is what
    makes it safe to offer as a button rather than as a runbook step.
    """
    queue = getattr(request.app.state, "task_queue", None)
    result = await admin.enqueue_reprocess(session, staff, symbol, queue)
    return TaskAcceptedOut(
        task=admin.REPROCESS_TASK_NAME,
        task_id=str(result["task_id"]),
        target=str(result["symbol"]),
        detail="Adjusted bars will be rebuilt from close_raw and the corporate-action history.",
    )


@router.get(
    "/data-versions", response_model=DataVersionListOut, summary="Every published data_version"
)
async def list_data_versions(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=admin.MAX_RUN_LIMIT)] = admin.DEFAULT_RUN_LIMIT,
) -> DataVersionListOut:
    """The publish history. ``current`` is the top of it — the version every read is served at."""
    runs: list[PipelineRun] = await admin.data_version_history(session, limit=limit)
    rows = [
        DataVersionOut(
            data_version=run.data_version or 0,
            trade_date=run.trade_date,
            published_at=run.finished_at,
            publish_latency_seconds=(
                None
                if run.finished_at is None
                else admin.publish_latency_seconds(run.trade_date, run.finished_at)
            ),
        )
        for run in runs
    ]
    return DataVersionListOut(data=rows, current=rows[0].data_version if rows else 0)


@router.get("/providers", response_model=ProviderHealthListOut, summary="Provider health")
async def get_provider_health() -> ProviderHealthListOut:
    """``providers doctor``, rendered. Makes no network call — see ``decile_api.admin``."""
    return ProviderHealthListOut(
        data=[ProviderHealthOut.model_validate(report) for report in admin.provider_health()]
    )


def _user_out(user: AppUser) -> AdminUserOut:
    return AdminUserOut(
        public_id=user.public_id,
        email=user.email,
        name=user.name,
        created_at=user.created_at,
        email_verified=user.email_verified_at is not None,
        is_staff=user.is_staff,
        deleted_at=user.deleted_at,
    )


@router.get("/users", response_model=AdminUserListOut, summary="Look an account up")
async def search_users(
    session: SessionDep,
    q: Annotated[
        str, Query(min_length=1, max_length=200, description="Email fragment or public id.")
    ],
    limit: Annotated[int, Query(ge=1, le=admin.MAX_USER_HITS)] = 20,
) -> AdminUserListOut:
    users = await admin.search_users(session, q, limit=limit)
    return AdminUserListOut(data=[_user_out(user) for user in users])


def _override_out(row: EntitlementOverride, granted_by: str | None) -> EntitlementOverrideOut:
    effect = "grant" if row.effect == "grant" else "revoke"
    return EntitlementOverrideOut(
        feature=row.feature,
        effect=effect,
        value=row.value,
        reason=row.reason,
        granted_by=granted_by,
        expires_at=row.expires_at,
        created_at=row.created_at,
        active=row.is_active(),
    )


async def _detail_out(session: AsyncSession, detail: admin.AdminUserDetail) -> AdminUserDetailOut:
    authors: dict[int, str] = {}
    for override in detail.overrides:
        if override.granted_by_user_id not in authors:
            author = await session.get(AppUser, override.granted_by_user_id)
            authors[override.granted_by_user_id] = author.email if author else "(deleted account)"
    return AdminUserDetailOut(
        user=_user_out(detail.user),
        plan_code=detail.plan_code,
        subscription_status=detail.subscription_status,
        screen_count=detail.screen_count,
        entitlements=EntitlementsOut.model_validate(detail.entitlements.as_dict()),
        overrides=[
            _override_out(row, authors.get(row.granted_by_user_id)) for row in detail.overrides
        ],
    )


@router.get("/users/{public_id}", response_model=AdminUserDetailOut, summary="One account, in full")
async def get_user(request: Request, session: SessionDep, public_id: str) -> AdminUserDetailOut:
    detail = await admin.user_detail(session, public_id, settings=_settings(request))
    return await _detail_out(session, detail)


@router.put(
    "/users/{public_id}/entitlements",
    response_model=AdminUserDetailOut,
    summary="Override one entitlement for one account",
)
async def put_entitlement_override(
    request: Request,
    session: SessionDep,
    staff: StaffDep,
    public_id: str,
    body: Annotated[EntitlementOverrideIn, Body()],
) -> AdminUserDetailOut:
    """Grant or revoke one feature, with a reason and (usually) an expiry.

    Answers the whole account detail rather than the override alone, so the caller sees the
    *effective* entitlements the change produced instead of having to re-resolve them itself.
    """
    await admin.set_override(
        session,
        staff,
        public_id,
        feature=body.feature,
        grant=body.effect == "grant",
        reason=body.reason,
        value=body.value,
        expires_at=body.expires_at,
    )
    detail = await admin.user_detail(session, public_id, settings=_settings(request))
    return await _detail_out(session, detail)


@router.delete(
    "/users/{public_id}/entitlements/{feature}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an entitlement override",
)
async def delete_entitlement_override(
    session: SessionDep, staff: StaffDep, public_id: str, feature: str
) -> Response:
    await admin.clear_override(session, staff, public_id, feature)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/actions", response_model=AdminActionListOut, summary="The staff audit trail")
async def list_actions(
    session: SessionDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> AdminActionListOut:
    """Every privileged action, newest first. Append-only; nothing here can edit it."""
    rows: list[AdminAction] = await admin.recent_actions(session, limit=limit)
    actors: dict[int, str] = {}
    for row in rows:
        if row.actor_user_id not in actors:
            actor = await session.get(AppUser, row.actor_user_id)
            actors[row.actor_user_id] = actor.email if actor else "(deleted account)"
    return AdminActionListOut(
        data=[
            AdminActionOut(
                action=row.action,
                target=row.target,
                actor=actors.get(row.actor_user_id),
                detail=row.detail,
                created_at=row.created_at,
            )
            for row in rows
        ]
    )


@router.get(
    "/public-api",
    response_model=PublicApiGateOut,
    summary="Whether the public read API may serve, and why not",
)
async def public_api_gate(request: Request, staff: StaffDep) -> PublicApiGateOut:
    """PROMPTS.md Prompt 20 §2: "make that dependency explicit in the code **and the admin UI**".

    This is a read of two constants and one setting — no database, no side effect. It exists so
    that "why is the public API not serving" has an answer a staff member can read, in the exact
    words docs/11 uses, rather than a shrug and a grep.
    """
    del staff
    settings = _settings(request)
    review = DATA_REDISTRIBUTION_REVIEW
    return PublicApiGateOut(
        flag_enabled=settings.public_api_enabled,
        review_signed_off=review.signed_off,
        serving=public.is_enabled(settings),
        requirement=review.requirement,
        opinion_reference=review.opinion_reference,
        signed_off_on=review.signed_off_on,
        signed_off_by=review.signed_off_by,
        api_version=PUBLIC_API_VERSION,
        prefix=PUBLIC_API_PREFIX,
        served_fields=list(PUBLIC_COLUMNS),
        withheld_fields=list(WITHHELD_COLUMNS),
    )
