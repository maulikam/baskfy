"""Fetch corporate actions for a window and upsert them. Applies NOTHING to any price.

`gates/ca-backfill.md`. Run inside the worker container on the box:

    AWS_PROFILE=baskfy-poc bash tools/deploy/box-python.sh worker ops/ca-backfill/fetch-window.py

WHY THIS DOES NOT CALL apply_adjustments
----------------------------------------
`run_fetch_corporate_actions` upserts on `(instrument_id, action_type, ex_date)` and *returns* the
instruments it touched; it is the ORCHESTRATOR that hands those to `apply_adjustments`. Calling the
fetch on its own therefore writes actions and no prices.

That separation is the point here. The missing actions are mostly dividends, and
`docs/DECISIONS-MERGE.md` M27.1 measured the convention question against the reference corpus and
got **price 42, total 3** — with the 271-row cross-check finding that applying dividends "pushes
them below even today's unadjusted baseline". Rewriting hundreds of instruments' price history as a
side effect of a capture run would revert a measured decision without measuring anything. The
capture is unambiguously right; the apply is a separate call, with its own evidence, and Maulik's.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from baskfy_api.settings import get_settings
from baskfy_worker.providers import build_pipeline_dependencies
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks import corporate_actions

SINCE = dt.date(2026, 8, 1)


async def main() -> None:
    deps = build_pipeline_dependencies()
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            outcome = StepOutcome()
            async with session.begin():
                touched = await corporate_actions.run_fetch_corporate_actions(
                    session, deps.provider, outcome, SINCE
                )
            print(
                f"since={SINCE.isoformat()} rows_in={outcome.rows_in} "
                f"rows_out={outcome.rows_out} touched_instruments={len(touched)}"
            )
            print(f"detail={outcome.detail}")
            print("APPLIED_ADJUSTMENTS=0 — this script never calls apply_adjustments")
    finally:
        await engine.dispose()


asyncio.run(main())
