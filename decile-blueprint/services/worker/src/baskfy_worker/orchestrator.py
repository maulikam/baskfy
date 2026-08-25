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

Prompt 17 pays part of that cost back. The ``pipeline_run`` row itself can be opened *before* this
transaction, on a session of its own, and passed in as ``run_id`` — see
:func:`baskfy_worker.ops.begin_run`. The row then survives a worker that is killed mid-chain,
which is what "a correct failed run record" needs to be possible at all; the step rows still do
not, and the abandoned run is reconciled by :func:`baskfy_worker.ops.reap_abandoned_runs`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import PipelineRun
from baskfy_providers.errors import ProviderError
from baskfy_worker.calendar import (
    CALENDAR_LOOKBACK_DAYS,
    NotATradingDay,
    reconcile_calendar,
    require_trading_day,
)
from baskfy_worker.deps import PipelineDependencies
from baskfy_worker.steps import (
    HardFailure,
    PipelineStep,
    RunStatus,
    StepOutcome,
    StepStatus,
    adopt_run,
    close_run,
    open_run,
    record_step,
)
from baskfy_worker.tasks import (
    adjustments,
    bars,
    basket,
    corporate_actions,
    factors,
    listings,
    market_health,
    membership,
    publish,
    quality,
    snapshots,
)
from baskfy_worker.tasks import fundamentals as fundamentals_task
from baskfy_worker.tasks import instruments as instruments_task
from baskfy_worker.tasks.quality import GateReport
from baskfy_worker.window import DateWindow

#: How far back ``fetch_corporate_actions`` looks each night. A week covers a long weekend plus a
#: late NSE publication without re-reading the whole calendar every evening.
CORPORATE_ACTION_LOOKBACK_DAYS: int = 7


@dataclass(slots=True)
class PipelineOutcome:
    """What a run did, in memory — survives the rollback that a hard failure triggers."""

    trade_date: dt.date
    status: RunStatus
    #: The ``pipeline_run`` row this outcome describes. ``None`` only when the run never opened —
    #: a non-trading day. Carried so an alert and the admin page can name the row.
    run_id: int | None = None
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
    *,
    run_id: int | None = None,
) -> PipelineOutcome:
    """Run docs/03's ten steps for ``trade_date``.

    Returns rather than raises: a caller (Beat, the CLI, a test) wants the report either way, and
    an exception would lose the gate's structured findings.

    ``run_id`` names a ``pipeline_run`` row that has **already been committed**, by
    :func:`baskfy_worker.ops.begin_run` on a session of its own. Passing it is what makes a run
    record survive a worker that is killed mid-chain: this transaction rolls back, that row does
    not. Omitting it keeps the original behaviour — the row is opened inside this transaction and
    shares its fate — which is what the in-process acceptance tests want.
    """
    try:
        await require_trading_day(session, trade_date)
    except NotATradingDay as exc:
        # docs/09 §Schedule and Prompt 3 deliverable 7: "never attempt to ingest or compute for a
        # non-trading day". A weekend is not a failure.
        if run_id is not None:
            # A row was committed before the check ran, so it has to be closed here or the
            # stale-run reaper will eventually call a Sunday an abandoned pipeline.
            await close_run(session, await adopt_run(session, run_id), RunStatus.ABORTED)
        return PipelineOutcome(trade_date, RunStatus.ABORTED, run_id=run_id, error=str(exc))

    run = (
        await adopt_run(session, run_id)
        if run_id is not None
        else await open_run(session, trade_date)
    )
    outcome = PipelineOutcome(trade_date, RunStatus.RUNNING, run_id=run.id)

    try:
        await _run_chain(session, run, trade_date, deps, outcome)
    except HardFailure as exc:
        outcome.status = RunStatus.FAILED
        outcome.error = str(exc)
        await close_run(session, run, RunStatus.FAILED)
        return outcome

    outcome.status = RunStatus.SUCCEEDED
    return outcome


async def _run_chain(  # noqa: PLR0915 - one block per pipeline step, and docs/03 defines
    # eleven of them. Splitting a linear chain into helpers to satisfy a statement count
    # would hide the order the steps run in, which is the one thing this function is for.
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
    async with record_step(session, run.id, PipelineStep.REFRESH_INSTRUMENTS, trade_date) as step:
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
    async with record_step(session, run.id, PipelineStep.FETCH_DAILY_BARS, trade_date) as step:
        await bars.run_fetch_daily_bars(session, deps.provider, step, active, window)
    outcome.steps_completed.append(PipelineStep.FETCH_DAILY_BARS)

    # docs/09 §Schedule: the calendar is "asserted against observed bar dates". Doing it here,
    # after the bars land and before anything computes, is what lets a date the seed list wrongly
    # called a holiday become a fact rather than staying a guess (docs/04a).
    #
    # Look back the longest factor window, not only tonight: a single-day reconcile left lunar
    # holidays in the trailing year as ``derived``, so 9M/12M resolved long (T9.2).
    calendar_start = min(window.start, trade_date - dt.timedelta(days=CALENDAR_LOOKBACK_DAYS))
    await reconcile_calendar(session, calendar_start, window.end)

    # --- 3. fetch_corporate_actions --------------------------------------
    async with record_step(
        session, run.id, PipelineStep.FETCH_CORPORATE_ACTIONS, trade_date
    ) as step:
        since = trade_date - dt.timedelta(days=CORPORATE_ACTION_LOOKBACK_DAYS)
        touched = await corporate_actions.run_fetch_corporate_actions(
            session, deps.provider, step, since
        )
    outcome.steps_completed.append(PipelineStep.FETCH_CORPORATE_ACTIONS)

    # --- 4. apply_adjustments --------------------------------------------
    async with record_step(session, run.id, PipelineStep.APPLY_ADJUSTMENTS, trade_date) as step:
        if not touched:
            step.status = StepStatus.SKIPPED
        await adjustments.run_apply_adjustments(session, step, touched)
    outcome.steps_completed.append(PipelineStep.APPLY_ADJUSTMENTS)

    # --- 5. refresh_index_membership -------------------------------------
    async with record_step(
        session, run.id, PipelineStep.REFRESH_INDEX_MEMBERSHIP, trade_date
    ) as step:
        await membership.run_refresh_index_membership(session, deps.provider, step, trade_date)
    outcome.steps_completed.append(PipelineStep.REFRESH_INDEX_MEMBERSHIP)

    # --- 6. refresh_index_snapshots --------------------------------------
    async with record_step(
        session, run.id, PipelineStep.REFRESH_INDEX_SNAPSHOTS, trade_date
    ) as step:
        await snapshots.run_refresh_index_snapshots(session, deps.provider, step, trade_date)
        # T9.1: equity PE/mcap from NSE, folded into this step the way listings are folded into
        # refresh_instruments — not a twelfth pipeline identity. A quote failure is recorded and
        # the night continues: NULL is already the publishable state.
        #
        # Scoped by `fundamentals_scope` (the day's traded names) and not by `active` (every
        # non-delisted instrument). At NSE's 1 req/s that is ~2,540 requests instead of ~10,481,
        # which is the difference between forty minutes and most of a night — and the names it
        # drops have no bar to price a market cap on.
        fundamentals = StepOutcome()
        try:
            await fundamentals_task.run_fetch_fundamentals(
                session,
                deps.provider,
                fundamentals,
                trade_date,
                await fundamentals_task.fundamentals_scope(session, trade_date),
            )
        except ProviderError as exc:
            fundamentals.note(error=str(exc))
        step.note(fundamentals=fundamentals.detail, fundamentals_rows=fundamentals.rows_out)
    outcome.steps_completed.append(PipelineStep.REFRESH_INDEX_SNAPSHOTS)

    # --- 7. compute_factors ----------------------------------------------
    async with record_step(session, run.id, PipelineStep.COMPUTE_FACTORS, trade_date) as step:
        await factors.run_compute_factors(session, step, trade_date, deps.factor_engine)
    outcome.steps_completed.append(PipelineStep.COMPUTE_FACTORS)

    # --- 8. compute_market_health ----------------------------------------
    async with record_step(session, run.id, PipelineStep.COMPUTE_MARKET_HEALTH, trade_date) as step:
        await market_health.run_compute_market_health(session, step, trade_date)
    outcome.steps_completed.append(PipelineStep.COMPUTE_MARKET_HEALTH)

    # --- 9. data_quality_gate (hard blocker) -----------------------------
    async with record_step(session, run.id, PipelineStep.DATA_QUALITY_GATE, trade_date) as step:
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
    async with record_step(session, run.id, PipelineStep.PUBLISH, trade_date) as step:
        result = await publish.run_publish(session, step, run, deps.cache)
        outcome.data_version = result.data_version
    outcome.steps_completed.append(PipelineStep.PUBLISH)

    # --- 11. refresh_basket ----------------------------------------------
    #
    # M30. `/baskets` computed this per request and took 67 seconds once M29 gave it nine years
    # of bars to load. The inputs change once a night; so does the answer.
    #
    # AFTER publish, deliberately: the basket is identified by the `data_version` publish bumps,
    # so computing it earlier would store a basket labelled with a data set it was not built
    # from. And it runs last because it is a *cache* -- `run_refresh_basket` records its own
    # failure and returns rather than raising, so a basket that cannot be built (no uploaded
    # scan, say) never holds back a `data_version` that is otherwise good.
    async with record_step(session, run.id, PipelineStep.REFRESH_BASKET, trade_date) as step:
        names = await basket.run_refresh_basket(session, step, trade_date)
        if names == 0:
            step.status = StepStatus.SKIPPED
    outcome.steps_completed.append(PipelineStep.REFRESH_BASKET)
