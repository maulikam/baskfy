"""Precomputing the basket in the nightly chain (M30).

`/baskets` built its answer per request — every bar the scanned symbols ever had, into Polars,
scored, planned. About a second at two years of history; **67 seconds** at nine (M29.6). The
inputs change once a night, so now it is computed once a night and the page reads a row.

Four properties decide whether that is safe rather than merely fast:

* the step must be **idempotent**, or a re-run of a night piles up rows (house rule 7);
* it must **never fail the pipeline** — a presentation cache that can hold back a good
  `data_version` is worse than a slow page;
* it must run **after publish**, because the basket is identified by the version publish bumps;
* a **cold cache must fall back**, not 404.
"""

from __future__ import annotations

import datetime as dt

import pytest
from helpers import requires_db
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import BasketSnapshot
from baskfy_worker.steps import NIGHTLY_CHAIN, PipelineStep, StepOutcome
from baskfy_worker.tasks.basket import latest_snapshot, run_refresh_basket

AS_OF = dt.date(2026, 8, 18)


class TestWhereItSitsInTheChain:
    def test_it_runs_after_publish(self) -> None:
        """The basket carries the `data_version` publish bumps.

        Computed earlier, the snapshot would be labelled with a version it was not built from —
        the page would then say "data version 4" over numbers derived from version 3.
        """
        assert PipelineStep.REFRESH_BASKET in NIGHTLY_CHAIN
        assert NIGHTLY_CHAIN.index(PipelineStep.REFRESH_BASKET) > NIGHTLY_CHAIN.index(
            PipelineStep.PUBLISH
        )

    def test_it_is_the_last_step(self) -> None:
        """Because it is a cache. Nothing downstream may depend on it having succeeded."""
        assert NIGHTLY_CHAIN[-1] == PipelineStep.REFRESH_BASKET


@requires_db
@pytest.mark.db
class TestAgainstTheDatabase:
    async def test_a_basket_that_cannot_be_built_is_recorded_not_raised(
        self, session: AsyncSession
    ) -> None:
        """The property that keeps a cache from breaking a pipeline.

        On a database with no bars and no uploaded scan there is no basket to build. The step has
        to say so and return, because a `data_version` that is otherwise good must not be held
        back by a page that cannot be rendered.
        """
        outcome = StepOutcome()

        names = await run_refresh_basket(session, outcome, AS_OF)

        assert names == 0
        assert outcome.detail, "it must say why, not fail silently"
        stored = (
            await session.execute(select(func.count()).select_from(BasketSnapshot))
        ).scalar_one()
        assert stored == 0, "nothing may be written when nothing could be built"

    async def test_a_cold_cache_reads_as_none_rather_than_raising(
        self, session: AsyncSession
    ) -> None:
        """`latest_snapshot` returning None is what makes the endpoint's live fallback reachable."""
        assert await latest_snapshot(session) is None

    async def test_a_stored_snapshot_comes_back_whole(self, session: AsyncSession) -> None:
        payload = {"as_of": "2026-08-18", "rows": [{"symbol": "CUPID"}], "data_version": 7}
        session.add(
            BasketSnapshot(as_of=AS_OF, screen_run_id="abc123", data_version=7, payload=payload)
        )
        await session.flush()

        assert await latest_snapshot(session) == payload

    async def test_the_newest_version_wins(self, session: AsyncSession) -> None:
        """A second publish on the same date supersedes the first."""
        for version in (1, 2):
            session.add(
                BasketSnapshot(
                    as_of=AS_OF,
                    screen_run_id=f"run{version}",
                    data_version=version,
                    payload={"data_version": version},
                )
            )
        await session.flush()

        got = await latest_snapshot(session)
        assert got == {"data_version": 2}

    async def test_one_row_per_date_and_version(self, session: AsyncSession) -> None:
        """House rule 7: re-running a night replaces its own row rather than adding one.

        Asserted against the constraint rather than the code path, because the constraint is what
        actually holds when someone writes a second caller.
        """
        session.add(
            BasketSnapshot(as_of=AS_OF, screen_run_id="a", data_version=1, payload={"n": 1})
        )
        await session.flush()

        duplicate = await session.execute(
            text(
                "insert into basket_snapshot (as_of, screen_run_id, data_version, payload) "
                "values (:d, 'b', 1, '{}'::jsonb) on conflict do nothing returning id"
            ),
            {"d": AS_OF},
        )
        assert duplicate.first() is None, "the unique constraint did not hold"
