"""The nightly stopped publishing for five days; these are the two defects behind that.

17 Sep 2026. `apply_adjustments` rewrites an instrument's whole adjusted history, `ohlcv_daily`
compresses chunks older than 90 days, and TimescaleDB caps tuple decompression at 100,000 per DML
transaction. A new action on SANSERA needed 350,845, so the step raised, the chain's single
transaction aborted, and the product served the 11 Sep session until someone looked.

The failure was invisible: `record_step`'s own write then failed on the aborted transaction, and
`InvalidRequestError: Can't operate on closed transaction` is what reached the log every night.
"""

from __future__ import annotations

import contextlib
import datetime as dt

import pytest
from helpers import TRADE_DATE, add_bar, make_instrument, requires_db
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_worker.steps import PipelineStep, StepOutcome, StepStatus, record_step
from baskfy_worker.tasks.adjustments import run_apply_adjustments

pytestmark = [pytest.mark.db, requires_db]

SETTING = "timescaledb.max_tuples_decompressed_per_dml_transaction"


async def test_the_step_lifts_the_decompression_cap_for_its_own_transaction(
    session: AsyncSession,
) -> None:
    """Unlimited inside the step, because the rewrite it must do is bounded by its target list."""
    instrument_id = await make_instrument(session, "SANSERA")
    await add_bar(session, instrument_id, TRADE_DATE, "100.00")
    await session.flush()

    outcome = StepOutcome()
    await run_apply_adjustments(session, outcome, [instrument_id])

    setting = (await session.execute(text(f"show {SETTING}"))).scalar_one()
    assert setting == "0"


async def test_no_target_means_no_setting_change(session: AsyncSession) -> None:
    """A night with no corporate action does not touch the cap: it writes nothing to lift it for."""
    before = (await session.execute(text(f"show {SETTING}"))).scalar_one()
    outcome = StepOutcome()
    assert await run_apply_adjustments(session, outcome, []) == 0
    assert (await session.execute(text(f"show {SETTING}"))).scalar_one() == before


async def test_a_step_reports_its_own_error_even_when_the_row_cannot_be_written(
    session: AsyncSession,
) -> None:
    """The recorder is a nice-to-have; the exception is the evidence.

    The step body breaks the transaction — exactly what a decompression-limit error does — so the
    `pipeline_run_step` write is impossible. What must surface is the step's own error.
    """
    from baskfy_core.models import PipelineRun  # noqa: PLC0415 - one test needs the run row

    run = PipelineRun(trade_date=TRADE_DATE, status="running", started_at=dt.datetime.now(dt.UTC))
    session.add(run)
    await session.flush()

    class TheStepsOwnError(RuntimeError):
        pass

    with pytest.raises(TheStepsOwnError):
        async with record_step(session, run.id, PipelineStep.APPLY_ADJUSTMENTS, TRADE_DATE):
            # Break the transaction the way a failed statement does, then fail.
            with contextlib.suppress(Exception):
                await session.execute(text("select 1 from does_not_exist_anywhere"))
            raise TheStepsOwnError("the cause that must reach the log")

    await session.rollback()
    assert StepStatus.FAILED.value == "failed"
