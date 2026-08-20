"""The nightly pipeline orchestrator (Prompt 3 deliverable 3).

    "A PipelineRun orchestrator that runs the chain for a trade date, stops on the first hard
     failure, and only bumps `data_version` after the quality gate passes."

docs/03: the ten steps run in order, "Every step writes a row in `pipeline_run_step` with status,
duration, row counts and an error payload. Step 9 is a hard gate: if it fails, `data_version` is
**not** bumped, so the site continues serving yesterday's consistent snapshot rather than today's
broken one."

Transaction shape
-----------------
The whole chain runs inside one transaction, and a hard failure rolls it back. That is deliberate:
a run that dies between ``apply_adjustments`` and ``compute_factors`` would otherwise leave
adjusted prices with stale factor rows — a state where the gate's assertion 4 passes on count
while every number behind it is wrong. Either the trade date lands whole or it does not land.

The cost is that the ``pipeline_run_step`` audit trail rolls back too, so a failed run is recorded
separately, after the rollback, from the report the orchestrator carries in memory.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from decile_core.models import PipelineRun
from decile_worker.calendar import NotATradingDay, reconcile_calendar, require_trading_day
from decile_worker.deps import PipelineDependencies
from decile_worker.steps import (
    HardFailure,
    PipelineStep,
    RunStatus,
    StepOutcome,
    StepStatus,
    close_run,
    open_run,
    record_step,
)
from decile_worker.tasks import (
    adjustments,
    bars,
    corporate_actions,
    factors,
    listings,
    market_health,
    membership,
    publish,
    quality,
    snapshots,
)
from decile_worker.tasks import instruments as instruments_task
from decile_worker.tasks.quality import GateReport
from decile_worker.window import DateWindow

#: How far back ``fetch_corporate_actions`` looks each night. A week covers a long weekend plus a
#: late NSE publication without re-reading the whole calendar every evening.
CORPORATE_ACTION_LOOKBACK_DAYS: int = 7


@dataclass(slots=True)
class PipelineOutcome:
    """What a run did, in memory — survives the rollback that a hard failure triggers."""

    trade_date: dt.date
    status: RunStatus
    data_version: int | None = None
    gate: GateReport | None = None
    failed_step: PipelineStep | None = None
    error: str | None = None
    steps_completed: list[PipelineStep] = field(default_factory=list)

    @property
    def published(self) -> bool:
        return self.status is RunStatus.SUCCEEDED and self.data_version is not None


async def run_nightly_pipeline(
    session: AsyncSession,
    trade_date: dt.date,
    deps: PipelineDependencies,
) -> PipelineOutcome:
    """Run docs/03's ten steps for ``trade_date``.

    Returns rather than raises: a caller (Beat, the CLI, a test) wants the report either way, and
    an exception would lose the gate's structured findings.
    """
    try:
        await require_trading_day(session, trade_date)
    except NotATradingDay as exc:
        # docs/09 §Schedule and Prompt 3 deliverable 7: "never attempt to ingest or compute for a
        # non-trading day". A weekend is not a failure.
        return PipelineOutcome(trade_date, RunStatus.ABORTED, error=str(exc))

    run = await open_run(session, trade_date)
    outcome = PipelineOutcome(trade_date, RunStatus.RUNNING)

    try:
        await _run_chain(session, run, trade_date, deps, outcome)
    except HardFailure as exc:
        outcome.status = RunStatus.FAILED
        outcome.error = str(exc)
        await close_run(session, run, RunStatus.FAILED)
        return outcome

    outcome.status = RunStatus.SUCCEEDED
    return outcome


async def _run_chain(
    session: AsyncSession,
    run: PipelineRun,
    trade_date: dt.date,
    deps: PipelineDependencies,
    outcome: PipelineOutcome,
) -> None:
    window = deps.bars_window or DateWindow.single(trade_date)

    # --- 1. refresh_instruments ------------------------------------------
    # docs/03 step 1 is "Kite instruments dump + NSE series/listing files", so the listings
    # register is folded into this step rather than given a step of its own — docs/03's chain has
    # ten steps and adding an eleventh would put the pipeline out of step with its own spec.
    # `refresh_listings` remains callable on its own for the backfill (Prompt 4 deliverable 4).
    async with record_step(session, run.id, PipelineStep.REFRESH_INSTRUMENTS) as step:
        await instruments_task.run_refresh_instruments(
            session, deps.provider, step, as_of=trade_date
        )
        # Its own outcome: sharing `step` would let the register's row counts overwrite the
        # instrument merge's, and the step row would then describe neither accurately.
        register = StepOutcome()
        await listings.run_refresh_listings(session, deps.provider, register)
        step.note(listings=register.detail, listings_rows=register.rows_out)
    outcome.steps_completed.append(PipelineStep.REFRESH_INSTRUMENTS)

    active = await instruments_task.active_instruments(session)

    # --- 2. fetch_daily_bars ---------------------------------------------
    async with record_step(session, run.id, PipelineStep.FETCH_DAILY_BARS) as step:
        await bars.run_fetch_daily_bars(session, deps.provider, step, active, window)
    outcome.steps_completed.append(PipelineStep.FETCH_DAILY_BARS)

    # docs/09 §Schedule: the calendar is "asserted against observed bar dates". Doing it here,
    # after the bars land and before anything computes, is what lets a date the seed list wrongly
    # called a holiday become a fact rather than staying a guess (docs/04a).
    await reconcile_calendar(session, window.start, window.end)

    # --- 3. fetch_corporate_actions --------------------------------------
    async with record_step(session, run.id, PipelineStep.FETCH_CORPORATE_ACTIONS) as step:
        since = trade_date - dt.timedelta(days=CORPORATE_ACTION_LOOKBACK_DAYS)
        touched = await corporate_actions.run_fetch_corporate_actions(
            session, deps.provider, step, since
        )
    outcome.steps_completed.append(PipelineStep.FETCH_CORPORATE_ACTIONS)

    # --- 4. apply_adjustments --------------------------------------------
    async with record_step(session, run.id, PipelineStep.APPLY_ADJUSTMENTS) as step:
        if not touched:
            step.status = StepStatus.SKIPPED
        await adjustments.run_apply_adjustments(session, step, touched)
    outcome.steps_completed.append(PipelineStep.APPLY_ADJUSTMENTS)

    # --- 5. refresh_index_membership -------------------------------------
    async with record_step(session, run.id, PipelineStep.REFRESH_INDEX_MEMBERSHIP) as step:
        await membership.run_refresh_index_membership(session, deps.provider, step, trade_date)
    outcome.steps_completed.append(PipelineStep.REFRESH_INDEX_MEMBERSHIP)

    # --- 6. refresh_index_snapshots --------------------------------------
    async with record_step(session, run.id, PipelineStep.REFRESH_INDEX_SNAPSHOTS) as step:
        await snapshots.run_refresh_index_snapshots(session, deps.provider, step, trade_date)
    outcome.steps_completed.append(PipelineStep.REFRESH_INDEX_SNAPSHOTS)

    # --- 7. compute_factors ----------------------------------------------
    async with record_step(session, run.id, PipelineStep.COMPUTE_FACTORS) as step:
        await factors.run_compute_factors(session, step, trade_date, deps.factor_engine)
    outcome.steps_completed.append(PipelineStep.COMPUTE_FACTORS)

    # --- 8. compute_market_health ----------------------------------------
    async with record_step(session, run.id, PipelineStep.COMPUTE_MARKET_HEALTH) as step:
        await market_health.run_compute_market_health(session, step, trade_date)
    outcome.steps_completed.append(PipelineStep.COMPUTE_MARKET_HEALTH)

    # --- 9. data_quality_gate (hard blocker) -----------------------------
    async with record_step(session, run.id, PipelineStep.DATA_QUALITY_GATE) as step:
        report = await quality.run_data_quality_gate(session, step, trade_date)
        outcome.gate = report
        if not report.passed:
            step.status = StepStatus.FAILED
    outcome.steps_completed.append(PipelineStep.DATA_QUALITY_GATE)

    if outcome.gate is not None and not outcome.gate.passed:
        outcome.failed_step = PipelineStep.DATA_QUALITY_GATE
        raise HardFailure(
            "the data-quality gate failed: "
            + "; ".join(f"[{r.assertion}] {r.message}" for r in outcome.gate.failures)
        )

    # --- 10. publish ------------------------------------------------------
    async with record_step(session, run.id, PipelineStep.PUBLISH) as step:
        result = await publish.run_publish(session, step, run, deps.cache)
        outcome.data_version = result.data_version
    outcome.steps_completed.append(PipelineStep.PUBLISH)
