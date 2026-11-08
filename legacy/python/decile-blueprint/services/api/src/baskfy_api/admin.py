"""The staff surface's queries and actions (PROMPTS.md Prompt 17 deliverable 4).

    "/admin (staff-only): pipeline run history with per-step detail and a re-run button,
     data_version history, provider health, user lookup, entitlement override, and a
     reprocess-instrument action."

docs/09 §Observability is more specific about the first of those: "`pipeline_run_step` is the
operator UI; expose it at `/admin/pipeline` behind staff auth." So this reads the tables the
pipeline already writes rather than keeping a parallel record of its own — an operator UI whose
numbers can disagree with the run history is worse than no operator UI.

The two actions do not do the work
-----------------------------------
"Re-run" and "reprocess instrument" **enqueue** ``baskfy.pipeline.nightly`` and
``baskfy.compute.reprocess_instrument`` and answer 202. A nightly chain takes minutes and a
reprocess rewrites every bar an instrument has; doing either inside a request would hold a
connection from the API's pool for the duration and time out behind whatever proxy is in front.
The same producer-by-task-name arrangement as ``POST /backtests`` (see ``baskfy_api.queue`` for
why the API cannot import the worker).

Every action writes an ``admin_action`` row in the same transaction as the action itself. See
``baskfy_core.models.admin`` for why that table exists when nothing in the bundle asks for it.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.auth import Principal, PrincipalKind
from baskfy_api.entitlements import entitlements_for, resolve_active_grant
from baskfy_api.metrics import publish_latency_seconds
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.queue import TaskQueue
from baskfy_api.resync import (
    RESYNC_REQUESTED_ACTION,
    RESYNC_TASK_NAME,
    PublicationSource,
    ResyncPlan,
)
from baskfy_api.settings import Settings
from baskfy_core.entitlements import MAX_SCREENS_KEY, Entitlements, Feature
from baskfy_core.models import (
    AdminAction,
    AppUser,
    EntitlementOverride,
    Instrument,
    PipelineRun,
    PipelineRunStep,
    Plan,
    Screen,
    Subscription,
)
from baskfy_providers.publication import PublicationCheck

log = logging.getLogger(__name__)

__all__ = [
    "NIGHTLY_TASK_NAME",
    "REPROCESS_TASK_NAME",
    "AdminUserDetail",
    "clear_override",
    "data_version_history",
    "enqueue_reprocess",
    "enqueue_rerun",
    "enqueue_resync",
    "list_runs",
    "load_run",
    "provider_health",
    "publication_source",
    "publish_latency_seconds",
    "recent_actions",
    "record_action",
    "search_users",
    "set_override",
    "user_detail",
]

#: The task names the two actions publish. Duplicated from the worker's ``@shared_task(name=...)``
#: for the reason ``baskfy_api.queue`` gives — ``baskfy-worker`` depends on ``baskfy-api``, so the
#: import cannot go the other way. ``services/worker/tests/test_celery_config.py`` asserts every
#: name the API publishes is a task the worker actually binds.
NIGHTLY_TASK_NAME: Final = "baskfy.pipeline.nightly"
REPROCESS_TASK_NAME: Final = "baskfy.compute.reprocess_instrument"

#: How many runs the history page shows at once. A nightly pipeline produces one row a day, so
#: this is roughly a quarter's worth — enough to see a pattern, small enough to render.
DEFAULT_RUN_LIMIT: Final = 60
MAX_RUN_LIMIT: Final = 365

#: Ceiling on a user search, so a one-character query cannot page the whole table into memory.
MAX_USER_HITS: Final = 50

#: The keys an override may name: docs/07's six boolean features plus ``max_screens``.
OVERRIDABLE: Final[frozenset[str]] = frozenset(
    {feature.value for feature in Feature} | {MAX_SCREENS_KEY}
)


# ---------------------------------------------------------------------------
# Pipeline history
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunWithSteps:
    run: PipelineRun
    steps: tuple[PipelineRunStep, ...]

    @property
    def publish_latency_seconds(self) -> float | None:
        """docs/09 §Observability's "publish latency (EOD close -> data live)", per run."""
        if self.run.finished_at is None or self.run.data_version is None:
            return None
        return publish_latency_seconds(self.run.trade_date, self.run.finished_at)


async def list_runs(session: AsyncSession, *, limit: int = DEFAULT_RUN_LIMIT) -> list[RunWithSteps]:
    """The run history, newest first, each with its step rows.

    Two queries rather than a join: a join returns one row per step and the run's columns
    repeated ten times, and the step rows are needed grouped anyway.
    """
    capped = max(1, min(limit, MAX_RUN_LIMIT))
    runs = list(
        (
            await session.execute(
                select(PipelineRun)
                .order_by(PipelineRun.trade_date.desc(), PipelineRun.id.desc())
                .limit(capped)
            )
        ).scalars()
    )
    if not runs:
        return []

    ids = [run.id for run in runs]
    steps = list(
        (
            await session.execute(
                select(PipelineRunStep)
                .where(PipelineRunStep.run_id.in_(ids))
                .order_by(PipelineRunStep.run_id.desc(), PipelineRunStep.id.asc())
            )
        ).scalars()
    )
    by_run: dict[int, list[PipelineRunStep]] = {run_id: [] for run_id in ids}
    for step in steps:
        by_run[step.run_id].append(step)
    return [RunWithSteps(run, tuple(by_run[run.id])) for run in runs]


async def load_run(session: AsyncSession, run_id: int) -> RunWithSteps:
    """One run and its steps, or a 404."""
    run = await session.get(PipelineRun, run_id)
    if run is None:
        raise Problem(ProblemType.NOT_FOUND, f"No pipeline run with id {run_id}.")
    steps = list(
        (
            await session.execute(
                select(PipelineRunStep)
                .where(PipelineRunStep.run_id == run_id)
                .order_by(PipelineRunStep.id.asc())
            )
        ).scalars()
    )
    return RunWithSteps(run, tuple(steps))


async def data_version_history(
    session: AsyncSession, *, limit: int = DEFAULT_RUN_LIMIT
) -> list[PipelineRun]:
    """Every published version, newest first.

    ``data_version`` lives on ``pipeline_run`` and is set only by step 10 on a run whose gate
    passed (``baskfy_worker.tasks.publish``), so "the history of data_version" and "the runs that
    published" are the same list. No separate table, which is what keeps them from disagreeing.
    """
    capped = max(1, min(limit, MAX_RUN_LIMIT))
    return list(
        (
            await session.execute(
                select(PipelineRun)
                .where(PipelineRun.data_version.is_not(None))
                .order_by(PipelineRun.data_version.desc())
                .limit(capped)
            )
        ).scalars()
    )


def provider_health() -> list[dict[str, object]]:
    """What each adapter can serve right now — ``providers doctor``, over HTTP.

    Reuses ``baskfy_providers.cli.doctor_report`` rather than reimplementing the checks, so the
    admin page and the command-line doctor can never give different answers. It makes no network
    call by construction (Prompt 2 acceptance criterion 4), which is what makes it safe to render
    on a page load.
    """
    from baskfy_providers.cli import doctor_report  # noqa: PLC0415
    from baskfy_providers.factory import build_provider_stack  # noqa: PLC0415

    stack = build_provider_stack()
    return [
        {
            "name": report.name,
            "available": report.available,
            "detail": report.detail,
            "serves_now": sorted(c.value for c in report.served_capabilities),
            "can_serve": sorted(c.value for c in report.capabilities),
        }
        for report in doctor_report(stack)
    ]


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


async def record_action(
    session: AsyncSession,
    actor: Principal,
    *,
    action: str,
    target: str,
    detail: dict[str, object] | None = None,
) -> AdminAction:
    """Append one audit row. Called by every action below, in its own transaction."""
    row = AdminAction(
        actor_user_id=actor.require_user(), action=action, target=target, detail=detail or {}
    )
    session.add(row)
    await session.flush()
    log.info(
        "admin action",
        extra={"action": action, "target": target, "actor": actor.public_id},
    )
    return row


async def enqueue_rerun(
    session: AsyncSession, actor: Principal, trade_date: dt.date, queue: TaskQueue | None
) -> dict[str, object]:
    """The "re-run" button. Publishes ``baskfy.pipeline.nightly`` for one date.

    Idempotent in the sense that matters: the chain itself upserts on ``(instrument_id, date)``
    (docs/02 rule 3), and ``open_run`` reuses a run already in ``running``, so a double-click
    costs a wasted message rather than a duplicated night.
    """
    if queue is None:
        raise Problem(
            ProblemType.INTERNAL_ERROR,
            "No task broker is configured, so the pipeline cannot be re-run from here.",
        )
    task_id = str(queue.send_task(NIGHTLY_TASK_NAME, [trade_date.isoformat()]))
    await record_action(
        session,
        actor,
        action="pipeline_rerun",
        target=trade_date.isoformat(),
        detail={"task_id": task_id, "task": NIGHTLY_TASK_NAME},
    )
    return {"task_id": task_id, "trade_date": trade_date.isoformat()}


async def enqueue_reprocess(
    session: AsyncSession, actor: Principal, symbol: str, queue: TaskQueue | None
) -> dict[str, object]:
    """The "reprocess instrument" action — docs/09 §"Adjustment algorithm"'s rebuild rule.

    Takes a symbol rather than an id because that is what an operator has in front of them when a
    corporate action turns out to have been wrong.
    """
    instrument = (
        await session.execute(select(Instrument).where(Instrument.symbol == symbol.upper()))
    ).scalar_one_or_none()
    if instrument is None:
        raise Problem(ProblemType.NOT_FOUND, f"No instrument with symbol {symbol!r}.")
    if queue is None:
        raise Problem(
            ProblemType.INTERNAL_ERROR,
            "No task broker is configured, so the instrument cannot be reprocessed from here.",
        )
    task_id = str(queue.send_task(REPROCESS_TASK_NAME, [instrument.id]))
    await record_action(
        session,
        actor,
        action="instrument_reprocess",
        target=instrument.symbol,
        detail={"task_id": task_id, "instrument_id": instrument.id, "task": REPROCESS_TASK_NAME},
    )
    return {"task_id": task_id, "symbol": instrument.symbol, "instrument_id": instrument.id}


def publication_source() -> PublicationSource:
    """The detector's "did NSE publish a bhavcopy for this date?" probe, built lazily.

    Lazily because :data:`baskfy_api.resync.PublicationSource` is only invoked when the calendar
    actually holds an inferred holiday to ask about, and building the provider stack pings Redis
    and constructs an S3 client — work no ordinary inspection should pay for.

    Reuses ``baskfy_providers.publication`` rather than probing NSE a second way, so the answer
    the resync detector gets and the answer ``reconcile_calendar`` gets are produced by one piece
    of code (M62, leaf 3.1).
    """

    def build() -> PublicationCheck | None:
        from baskfy_providers.factory import build_provider_stack  # noqa: PLC0415
        from baskfy_providers.publication import (  # noqa: PLC0415
            bhavcopy_publication_check,
        )

        return bhavcopy_publication_check(build_provider_stack())

    return build


async def enqueue_resync(
    session: AsyncSession,
    actor: Principal,
    queue: TaskQueue | None,
    *,
    days: int,
    plan: ResyncPlan,
) -> dict[str, object]:
    """The "resync" button. Publishes ``baskfy.ops.resync``, which repairs and then re-inspects.

    Takes the *plan* the caller has already inspected, purely so the audit row records what the
    operator was looking at when they pressed. The worker re-inspects for itself rather than
    trusting it: minutes may pass in the queue, and acting on a stale picture is how a repair
    ends up fixing something that has already healed and missing something that has not.

    ``actor.require_user()`` travels with the message so the worker can write the completion row
    against the same person. Nothing else does — no token, no credential, and no order: this task
    reaches ingestion and the calendar, never ``packages/execution``.
    """
    if queue is None:
        raise Problem(
            ProblemType.INTERNAL_ERROR,
            "No task broker is configured, so a resync cannot be started from here.",
        )
    target = f"{plan.window_start.isoformat()}..{plan.window_end.isoformat()}"
    task_id = str(queue.send_task(RESYNC_TASK_NAME, [actor.require_user(), days]))
    await record_action(
        session,
        actor,
        action=RESYNC_REQUESTED_ACTION,
        target=target,
        detail={
            "task_id": task_id,
            "task": RESYNC_TASK_NAME,
            "days": days,
            "pending_at_request": len(plan.findings),
            "kinds_at_request": list(plan.kinds()),
        },
    )
    return {"task_id": task_id, "target": target, "pending": len(plan.findings)}


# ---------------------------------------------------------------------------
# Users and entitlement overrides
# ---------------------------------------------------------------------------


async def search_users(session: AsyncSession, query: str, *, limit: int = 20) -> list[AppUser]:
    """Look an account up by email fragment or exact ``public_id``.

    ``ILIKE`` on a ``citext`` column: the search is for a support conversation, where what is to
    hand is half an email address. Deleted accounts are included and flagged rather than hidden —
    "where did their account go" is precisely the question that brings someone here.
    """
    needle = query.strip()
    if not needle:
        return []
    capped = max(1, min(limit, MAX_USER_HITS))
    return list(
        (
            await session.execute(
                select(AppUser)
                .where(or_(AppUser.email.ilike(f"%{needle}%"), AppUser.public_id == needle))
                .order_by(AppUser.created_at.desc())
                .limit(capped)
            )
        ).scalars()
    )


@dataclass(frozen=True, slots=True)
class AdminUserDetail:
    """Everything the user-lookup page shows about one account."""

    user: AppUser
    plan_code: str | None
    subscription_status: str | None
    screen_count: int
    entitlements: Entitlements
    overrides: tuple[EntitlementOverride, ...]


async def _load_user(session: AsyncSession, public_id: str) -> AppUser:
    user = (
        await session.execute(select(AppUser).where(AppUser.public_id == public_id))
    ).scalar_one_or_none()
    if user is None:
        raise Problem(ProblemType.NOT_FOUND, f"No account with id {public_id!r}.")
    return user


async def user_detail(
    session: AsyncSession, public_id: str, *, settings: Settings
) -> AdminUserDetail:
    """One account, with its *effective* entitlements — overrides already applied.

    Resolved through ``baskfy_api.entitlements.entitlements_for``, the same call every gated
    endpoint makes, so the admin page shows what the account actually gets rather than a second
    opinion about it.
    """
    user = await _load_user(session, public_id)
    subject = Principal(
        kind=PrincipalKind.USER,
        user_id=user.id,
        public_id=user.public_id,
        email=user.email,
        is_staff=user.is_staff,
    )
    plan_code, subscription_status = await _plan_summary(session, user.id)
    overrides = list(
        (
            await session.execute(
                select(EntitlementOverride)
                .where(EntitlementOverride.user_id == user.id)
                .order_by(EntitlementOverride.feature.asc())
            )
        ).scalars()
    )
    return AdminUserDetail(
        user=user,
        plan_code=plan_code,
        subscription_status=subscription_status,
        screen_count=await _screen_count(session, user.id),
        entitlements=await entitlements_for(session, subject, settings=settings),
        overrides=tuple(overrides),
    )


async def set_override(  # noqa: PLR0913 - one parameter per persisted column
    session: AsyncSession,
    actor: Principal,
    public_id: str,
    *,
    feature: str,
    grant: bool,
    reason: str,
    value: int | None = None,
    expires_at: dt.datetime | None = None,
) -> EntitlementOverride:
    """Create or replace one account's override of one feature.

    Upserted on ``(user_id, feature)`` rather than appended: "what does this account get" must
    have one answer, and a history of superseded grants belongs in ``admin_action``, which is
    exactly where it goes.
    """
    if feature not in OVERRIDABLE:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            f"{feature!r} is not an overridable entitlement.",
        )
    if feature == MAX_SCREENS_KEY and grant and value is None:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            "Overriding max_screens requires a value.",
        )
    user = await _load_user(session, public_id)

    existing = (
        await session.execute(
            select(EntitlementOverride).where(
                EntitlementOverride.user_id == user.id, EntitlementOverride.feature == feature
            )
        )
    ).scalar_one_or_none()
    previous = (
        None
        if existing is None
        else {"effect": existing.effect, "value": existing.value, "reason": existing.reason}
    )

    if existing is None:
        existing = EntitlementOverride(user_id=user.id, feature=feature)
        session.add(existing)
    existing.effect = "grant" if grant else "revoke"
    existing.value = value
    existing.reason = reason
    existing.expires_at = expires_at
    existing.granted_by_user_id = actor.require_user()
    await session.flush()

    await record_action(
        session,
        actor,
        action="entitlement_override_set",
        target=user.public_id,
        detail={
            "feature": feature,
            "effect": existing.effect,
            "value": value,
            "reason": reason,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "previous": previous,
        },
    )
    return existing


async def clear_override(
    session: AsyncSession, actor: Principal, public_id: str, feature: str
) -> None:
    """Remove an override. Absent is not an error — the desired end state is the same either way."""
    user = await _load_user(session, public_id)
    existing = (
        await session.execute(
            select(EntitlementOverride).where(
                EntitlementOverride.user_id == user.id, EntitlementOverride.feature == feature
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return
    detail: dict[str, object] = {
        "feature": feature,
        "effect": existing.effect,
        "value": existing.value,
    }
    await session.delete(existing)
    await session.flush()
    await record_action(
        session,
        actor,
        action="entitlement_override_cleared",
        target=user.public_id,
        detail=detail,
    )


async def recent_actions(session: AsyncSession, *, limit: int = 50) -> list[AdminAction]:
    """The audit trail, newest first."""
    capped = max(1, min(limit, 200))
    return list(
        (
            await session.execute(
                select(AdminAction)
                .order_by(AdminAction.created_at.desc(), AdminAction.id.desc())
                .limit(capped)
            )
        ).scalars()
    )


async def _screen_count(session: AsyncSession, user_id: int) -> int:
    total = (
        await session.execute(
            select(func.count()).select_from(Screen).where(Screen.user_id == user_id)
        )
    ).scalar_one()
    return int(total)


async def _plan_summary(session: AsyncSession, user_id: int) -> tuple[str | None, str | None]:
    grant = await resolve_active_grant(session, user_id)
    if grant is not None:
        subscription, plan = grant
        return plan.code, subscription.status
    latest = (
        await session.execute(
            select(Subscription, Plan)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.started_at.desc(), Subscription.id.desc())
            .limit(1)
        )
    ).first()
    if latest is None:
        return None, None
    subscription, plan = latest
    return plan.code, subscription.status
