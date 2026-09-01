"""``/admin/*`` — the staff surface (PROMPTS.md Prompt 17 deliverable 4).

docs/09 §Observability: "`pipeline_run_step` is the operator UI; expose it at `/admin/pipeline`
behind staff auth."

Every route on this router depends on :func:`baskfy_api.auth.require_staff`, declared once on the
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

from baskfy_api import admin
from baskfy_api.auth import StaffDep, require_staff
from baskfy_api.db import SessionDep
from baskfy_api.resync import (
    DEFAULT_LOOKBACK_DAYS,
    MAX_LOOKBACK_DAYS,
    RESYNC_TASK_NAME,
    ResyncPlan,
    inspect_pending,
    last_completed_resync,
)
from baskfy_api.routers import public
from baskfy_api.schemas import (
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
    ResyncFindingOut,
    ResyncOutcomeOut,
    ResyncPlanOut,
    TaskAcceptedOut,
)
from baskfy_api.settings import Settings, get_settings
from baskfy_core.models import AdminAction, AppUser, EntitlementOverride, PipelineRun
from baskfy_core.public_api import (
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


# ---------------------------------------------------------------------------
# Resync — leaf 3.1's one button
# ---------------------------------------------------------------------------


async def _resync_plan(request: Request, session: SessionDep, days: int) -> ResyncPlan:
    return await inspect_pending(
        session,
        settings=_settings(request),
        publication=admin.publication_source(),
        days=days,
    )


async def _last_outcome(session: SessionDep) -> ResyncOutcomeOut | None:
    """The previous repair's own report, read back out of the staff audit trail.

    Rendered beside the current plan so the page can distinguish the two answers an operator most
    needs to tell apart: "nothing is pending" because the last resync worked, and "nothing is
    pending" because nothing has ever looked.
    """
    row = await last_completed_resync(session)
    if row is None:
        return None
    detail = row.detail or {}
    actor = await session.get(AppUser, row.actor_user_id)

    def _strings(key: str) -> list[str]:
        value = detail.get(key)
        return [str(item) for item in value] if isinstance(value, list) else []

    return ResyncOutcomeOut(
        completed_at=row.created_at,
        actor=actor.email if actor else None,
        repaired=_strings("repaired"),
        failed=_strings("failed"),
        deferred=_strings("deferred"),
        still_pending=_strings("still_pending"),
        unresolved=_strings("unresolved"),
        complete=detail.get("complete") is True,
    )


@router.get(
    "/resync",
    response_model=ResyncPlanOut,
    summary="What data is pending, and why — a dry inspection that changes nothing",
)
async def inspect_resync(
    request: Request,
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=MAX_LOOKBACK_DAYS)] = DEFAULT_LOOKBACK_DAYS,
) -> ResyncPlanOut:
    """Leaf 3.1's inspection half: find every gap, change nothing.

    A **GET**, and deliberately so. The operator is on a phone, and a repair is worth previewing
    before it runs; making the preview a side-effect-free read is also what lets G3's idempotence
    be tested without performing a repair to test it.

    Four classes are looked for, and the reason none of them is "does the day have any bars" is
    in ``baskfy_api.resync``: on 2026-02-01 the box held 322 bars against a neighbouring 2,310,
    and a presence check called that day fine.
    """
    plan = await _resync_plan(request, session, days)
    return ResyncPlanOut(
        window_start=plan.window_start,
        window_end=plan.window_end,
        trading_days_checked=plan.trading_days_checked,
        pending=plan.pending,
        findings=[
            ResyncFindingOut(
                kind=finding.kind.value,
                trade_date=finding.trade_date,
                summary=finding.summary,
                remedy=finding.remedy,
                observed_bars=finding.observed_bars,
                expected_bars=finding.expected_bars,
            )
            for finding in plan.findings
        ],
        unresolved=list(plan.unresolved),
        last_resync=await _last_outcome(session),
    )


@router.post(
    "/resync",
    response_model=TaskAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Close every gap the inspection found",
)
async def start_resync(
    request: Request,
    session: SessionDep,
    staff: StaffDep,
    days: Annotated[int, Query(ge=1, le=MAX_LOOKBACK_DAYS)] = DEFAULT_LOOKBACK_DAYS,
) -> TaskAcceptedOut:
    """The acting half. 202, for the same reason the re-run button answers 202: a bhavcopy
    re-ingest is minutes of work and a nightly chain is more.

    Separate from the GET rather than a ``dry_run`` flag on one endpoint, because inspect-then-act
    is the whole shape of this feature: a flag that changes a read into a write is one typo away
    from a repair nobody asked for.

    **This cannot place an order.** The task it publishes reaches ingestion, the calendar and the
    Kite *session* bridge — never ``packages/execution``, never the order gateway, never a GTT.
    ``services/api/tests/test_admin_resync.py`` asserts it over this file's source.
    """
    plan = await _resync_plan(request, session, days)
    queue = getattr(request.app.state, "task_queue", None)
    result = await admin.enqueue_resync(session, staff, queue, days=days, plan=plan)
    pending = int(str(result["pending"]))
    detail = (
        "Nothing was pending when this was queued; the task re-checks before acting and will "
        "report that it had nothing to do."
        if pending == 0
        else f"{pending} pending item(s) queued for repair. The result appears here when it "
        "finishes — including anything it could not fix."
    )
    return TaskAcceptedOut(
        task=RESYNC_TASK_NAME,
        task_id=str(result["task_id"]),
        target=str(result["target"]),
        detail=detail,
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
    """``providers doctor``, rendered. Makes no network call — see ``baskfy_api.admin``."""
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
