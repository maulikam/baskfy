"""Every hot query's plan, and the regression gate — Prompt 16 deliverable 2.

    "Query tuning: EXPLAIN ANALYZE every hot query, add or adjust indexes, and add a CI check
     that fails if any hot query's plan changes to a sequential scan on the seeded dataset."

Three assertions, in increasing strength.

1. **Every hot query still plans and runs.** ``EXPLAIN ANALYZE`` executes the statement, so a
   query that has drifted out of sync with the schema fails here rather than in production.
2. **No relation has *become* a sequential scan** since ``query_plan_baseline.json`` was recorded.
   That is the deliverable's wording, and it is the right shape: on 271 rows PostgreSQL is *right*
   to scan sequentially, so a blanket ban would either fail permanently or have to be enforced
   with ``enable_seqscan = off``, which measures a plan production never runs.
3. **An index exists that could serve each hot query.** This is the one that catches a missing
   index today rather than at 8.5M rows: with ``enable_seqscan = off``, a planner that *still*
   chooses a sequential scan is telling you there is no applicable index at any table size. This
   is how ``ix_instrument_listings_page`` was found (migration 0008).

The limitation, stated rather than discovered later: a plan captured against docs/13's single-date
export says little about the plan against the ~8.5M rows docs/04 §"Retention & size estimates"
projects. What this suite catches is a *lost* index, which is invisible until the table is big.
"""

from __future__ import annotations

import datetime as dt

import pytest
from screener_helpers import AS_OF, requires_db
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.market_data import SPARKLINE_WINDOW, _sparklines
from baskfy_api.query_plans import (
    GUARDED_RELATIONS,
    explain,
    hot_queries,
    load_baseline,
    observe,
    regressions,
    resolve_chunk_names,
    scan_nodes,
    unrecorded,
)

pytestmark = [pytest.mark.db, requires_db]


class TestHotQueries:
    async def test_every_hot_query_plans_and_runs(self, screener_session: AsyncSession) -> None:
        """`EXPLAIN ANALYZE` executes it, so this is also a smoke test of the statement."""
        await resolve_chunk_names(screener_session)
        for query in hot_queries(AS_OF):
            plan = await explain(screener_session, query.sql)
            assert "Node Type" in plan, query.name

    async def test_the_hot_query_list_covers_every_budgeted_surface(self) -> None:
        """docs/11 §"Performance budgets" names the surfaces; each one needs a plan here."""
        names = {query.name for query in hot_queries(AS_OF)}
        assert {"screen_run", "dashboard", "breadth_history", "listings_page"} <= names
        assert any(name.startswith("factsheet") for name in names)


class TestPlanRegression:
    async def test_no_hot_query_has_become_a_sequential_scan(
        self, screener_session: AsyncSession
    ) -> None:
        """The deliverable's CI check, in its own words."""
        observed = await observe(screener_session, AS_OF)
        baseline = load_baseline()
        found = regressions(baseline, observed)
        assert not found, "\n".join(
            [
                "A hot query regressed to a sequential scan on a guarded relation:",
                *(f"  {regression}" for regression in found),
                "",
                "If the change is deliberate, re-record with:",
                "  uv run python -m baskfy_api.query_plans --write",
                "and say in the commit message why the index is no longer worth it.",
            ]
        )

    async def test_every_hot_query_is_recorded(self, screener_session: AsyncSession) -> None:
        """A new hot query with no baseline is not silently exempt from the gate."""
        observed = await observe(screener_session, AS_OF)
        missing = unrecorded(load_baseline(), observed)
        assert not missing, (
            f"{list(missing)} have no recorded plan. Run "
            "`uv run python -m baskfy_api.query_plans --write`."
        )

    async def test_the_baseline_has_no_stale_entries(self, screener_session: AsyncSession) -> None:
        """A baseline entry for a query that no longer exists guards nothing."""
        observed = await observe(screener_session, AS_OF)
        stale = sorted(set(load_baseline()) - set(observed))
        assert not stale, f"{stale} are recorded but are no longer hot queries"


class TestIndexAvailability:
    """With sequential scans disabled, is there an index that *could* serve each query?

    A relation the planner still reads sequentially under ``enable_seqscan = off`` has no
    applicable index at all — the seq scan is not a size decision, it is the only option. That is
    a real finding at any table size, and it is what this asserts.
    """

    async def test_an_index_applies_to_every_guarded_relation(
        self, screener_session: AsyncSession
    ) -> None:
        await resolve_chunk_names(screener_session)
        # SET LOCAL, so it dies with the fixture's transaction rather than leaking into the
        # session the next test gets.
        await screener_session.execute(text("SET LOCAL enable_seqscan = off"))
        stranded: list[str] = []
        for query in hot_queries(AS_OF):
            plan = await explain(screener_session, query.sql, analyze=False)
            for node in scan_nodes(plan):
                if node.relation in GUARDED_RELATIONS and node.is_sequential:
                    stranded.append(f"{query.name}: {node.relation}")
        assert not stranded, (
            "No index applies to these, at any table size — the planner chose a sequential scan "
            f"even with enable_seqscan off: {sorted(set(stranded))}"
        )


class TestSparklineIsBounded:
    """The dashboard's sparkline window function used to sort the whole snapshot archive.

    docs/11 budgets the dashboard at 500 ms for "145 indices". `_sparklines` keeps the newest
    thirty levels per index; without a lower bound on ``date`` the sort input is every snapshot
    row ever written, so the cost of one page grows with the age of the service. The floor is
    ``SPARKLINE_WINDOW`` (Prompt 16 deliverable 2).
    """

    async def test_the_window_function_reads_a_bounded_date_range(
        self, screener_session: AsyncSession
    ) -> None:
        assert dt.timedelta(days=42) < SPARKLINE_WINDOW, (
            "thirty NSE trading days is 42-46 calendar days; the window must cover them"
        )
        # And it still returns a series, which is what makes the bound safe rather than clever.
        series = await _sparklines(screener_session, AS_OF)
        assert series, "the seeded dashboard has snapshots; the bounded query must still find them"
