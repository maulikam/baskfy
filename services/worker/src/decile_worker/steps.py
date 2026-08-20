"""Pipeline step identity and bookkeeping (Prompt 3 deliverable 2).

    "Each of the ten tasks as an idempotent unit that upserts (never blind-inserts) and writes a
     pipeline_run_step row with rows_in/rows_out/duration/error."

docs/03: "Every step writes a row in `pipeline_run_step` with status, duration, row counts and an
error payload." docs/09 §Observability makes that table the operator UI, exposed at
`/admin/pipeline` in Prompt 17 — so what gets recorded here is what an operator will have at 3am.
"""

from __future__ import annotations

import datetime as dt
import time
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from decile_core.models import JsonObject, PipelineRun, PipelineRunStep


class PipelineStep(StrEnum):
    """The ten steps of docs/03 §"Nightly pipeline", in order."""

    REFRESH_INSTRUMENTS = "refresh_instruments"
    FETCH_DAILY_BARS = "fetch_daily_bars"
    FETCH_CORPORATE_ACTIONS = "fetch_corporate_actions"
    APPLY_ADJUSTMENTS = "apply_adjustments"
    REFRESH_INDEX_MEMBERSHIP = "refresh_index_membership"
    REFRESH_INDEX_SNAPSHOTS = "refresh_index_snapshots"
    COMPUTE_FACTORS = "compute_factors"
    COMPUTE_MARKET_HEALTH = "compute_market_health"
    DATA_QUALITY_GATE = "data_quality_gate"
    PUBLISH = "publish"


#: The chain, in the order docs/03 lists it. The orchestrator walks exactly this.
NIGHTLY_CHAIN: Final[tuple[PipelineStep, ...]] = tuple(PipelineStep)


class StepStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: The step had nothing to do — e.g. a non-trading day, or no instruments with new actions.
    SKIPPED = "skipped"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass(slots=True)
class StepOutcome:
    """What a step did. Mutated by the step body, then persisted by :func:`record_step`."""

    rows_in: int = 0
    rows_out: int = 0
    status: StepStatus = StepStatus.SUCCEEDED
    #: Free-form detail folded into the ``error`` JSONB column even on success, because the
    #: interesting facts about a *successful* run (which engine ran, what was skipped and why)
    #: are exactly what you want when explaining a number six months later.
    detail: JsonObject = field(default_factory=dict)

    def note(self, **facts: object) -> None:
        self.detail.update(facts)


class HardFailure(Exception):
    """A failure that must stop the chain rather than be retried.

    docs/03 step 9 is "a hard gate: if it fails, `data_version` is **not** bumped, so the site
    continues serving yesterday's consistent snapshot rather than today's broken one." Retrying a
    quality-gate failure would only publish the same broken day later.
    """


async def open_run(session: AsyncSession, trade_date: dt.date) -> PipelineRun:
    """Start (or reuse) the run row for ``trade_date``.

    Reused rather than duplicated so that re-running a date — which docs/02 rule 3 requires to be
    safe — leaves one run history entry per date per attempt sequence, not a growing pile.
    """
    existing = (
        await session.execute(
            select(PipelineRun)
            .where(PipelineRun.trade_date == trade_date)
            .order_by(PipelineRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None and existing.status == RunStatus.RUNNING:
        return existing

    run = PipelineRun(
        trade_date=trade_date,
        status=RunStatus.RUNNING,
        started_at=dt.datetime.now(tz=dt.UTC),
    )
    session.add(run)
    await session.flush()
    return run


async def close_run(
    session: AsyncSession, run: PipelineRun, status: RunStatus, data_version: int | None = None
) -> None:
    run.status = status
    run.finished_at = dt.datetime.now(tz=dt.UTC)
    if data_version is not None:
        run.data_version = data_version
    await session.flush()


@asynccontextmanager
async def record_step(
    session: AsyncSession, run_id: int, step: PipelineStep
) -> AsyncIterator[StepOutcome]:
    """Time a step and persist exactly one ``pipeline_run_step`` row for it.

    Upserted on ``(run_id, step)`` so re-running a step inside the same run replaces its record
    rather than appending a second, contradictory one.

    A raised exception is recorded with its type, message and traceback, and then re-raised — the
    house rule is no silently swallowed exceptions, and a step that failed must look failed to the
    orchestrator as well as to the operator.
    """
    outcome = StepOutcome()
    started = time.monotonic()
    await _upsert_step(
        session, run_id, step, status=StepStatus.RUNNING, outcome=outcome, duration_ms=0, error=None
    )
    try:
        yield outcome
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        await _upsert_step(
            session,
            run_id,
            step,
            status=StepStatus.FAILED,
            outcome=outcome,
            duration_ms=duration_ms,
            error={
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(limit=20),
                **outcome.detail,
            },
        )
        raise
    duration_ms = int((time.monotonic() - started) * 1000)
    await _upsert_step(
        session,
        run_id,
        step,
        status=outcome.status,
        outcome=outcome,
        duration_ms=duration_ms,
        error=dict(outcome.detail) or None,
    )


async def _upsert_step(  # noqa: PLR0913 - one parameter per persisted column
    session: AsyncSession,
    run_id: int,
    step: PipelineStep,
    *,
    status: StepStatus,
    outcome: StepOutcome,
    duration_ms: int,
    error: JsonObject | None,
) -> None:
    stmt = insert(PipelineRunStep).values(
        run_id=run_id,
        step=step.value,
        status=status.value,
        rows_in=outcome.rows_in,
        rows_out=outcome.rows_out,
        duration_ms=duration_ms,
        error=error,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[PipelineRunStep.run_id, PipelineRunStep.step],
            set_={
                "status": stmt.excluded.status,
                "rows_in": stmt.excluded.rows_in,
                "rows_out": stmt.excluded.rows_out,
                "duration_ms": stmt.excluded.duration_ms,
                "error": stmt.excluded.error,
            },
        )
    )
    await session.flush()
