"""The screener's cost on a full-size universe — Prompt 6's fifth acceptance criterion.

    "Performance test: the generated SQL for the full universe returns in under 300 ms cold on
     the seeded dataset; assert with EXPLAIN that it uses the (date, marketcap_cr) index."

The 271-row reference export cannot answer either half of that: PostgreSQL will sequentially scan
a table that small no matter what indexes exist, and 271 rows are fast whatever the plan. So this
module builds a dataset the size of the real one — every NSE instrument, over enough trading days
that ``WHERE date = :as_of`` is actually selective — and measures against that.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from screener_helpers import (
    AS_OF,
    DATA_VERSION,
    requires_db,
    seeded_database,
    synthetic_universe_sql,
)
from sqlalchemy import create_mock_engine, text
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from baskfy_api.screener import execute_screen
from baskfy_core.screen_definition import ExtraFactor, ScreenDefinition
from baskfy_core.screener import build_screen_query
from baskfy_core.seed_data import EXAMPLE_SCREENS
from baskfy_core.universes import UNIVERSE_BY_SLUG

pytestmark = [pytest.mark.db, requires_db]

#: docs/02 §"Why Postgres": "~2,300 NSE instruments".
PERF_INSTRUMENTS = 2300
#: Enough trading days that a single date is ~1% of the table, as it is in production.
PERF_DAYS = 120

#: docs/11 §"Performance budgets" allows 800 ms cold for a whole request; Prompt 6 holds the SQL
#: itself to 300 ms, which is the part this measures — no HTTP, no serialisation, no cache.
COLD_BUDGET_MS = 300.0

#: The index that serves ``WHERE factor_daily.date = :as_of``. Prompt 6's acceptance criterion
#: names it; migration 0008 (Prompt 16) dropped the narrower ``ix_factor_daily_date`` that used to
#: shadow it. See :class:`TestThePlan`.
DATE_MARKETCAP_INDEX = "ix_factor_daily_date_marketcap_cr"

#: Rendering-only PostgreSQL dialect. ``create_mock_engine`` is the typed way to get one; the
#: dialect classes have untyped constructors (see ``baskfy_core.screener``).
PG_DIALECT: Dialect = create_mock_engine("postgresql+asyncpg://", lambda *args: None).dialect

TOTAL_MARKET = UNIVERSE_BY_SLUG["nifty-total-market"].index_id
INVESTING_001 = next(s for s in EXAMPLE_SCREENS if s.name == "Investing 001").definition


@pytest.fixture(scope="module")
def perf_url() -> str:
    """A seeded database with a production-sized universe added, and its statistics analysed.

    ``ANALYZE`` is not tuning: without it the planner has no row estimates for a table it has
    never seen, and an EXPLAIN assertion would be measuring the absence of statistics rather than
    the shape of the query.
    """
    url = seeded_database()

    async def build() -> None:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as connection:
                for statement in synthetic_universe_sql(TOTAL_MARKET, PERF_INSTRUMENTS, PERF_DAYS):
                    await connection.execute(text(statement))
                await connection.execute(text("ANALYZE factor_daily"))
                await connection.execute(text("ANALYZE index_member_daily"))
                await connection.execute(text("ANALYZE instrument"))
        finally:
            await engine.dispose()

    asyncio.run(build())
    return url


@pytest.fixture
async def perf_session(perf_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(perf_url)
    connection = await engine.connect()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await connection.close()
        await engine.dispose()


def full_universe_screen(**overrides: object) -> ScreenDefinition:
    payload: dict[str, object] = {
        "index": "nifty-total-market",
        "sort_by": "avg_sharpe_12_6_3_1",
        "median_volume_1y": 10_000_000,
        "series": ["EQ", "BE"],
    }
    payload.update(overrides)
    return ScreenDefinition.model_validate(payload)


async def _explain(session: AsyncSession, definition: ScreenDefinition) -> str:
    query = build_screen_query(definition, AS_OF)
    compiled = query.statement.compile(
        dialect=PG_DIALECT,
        compile_kwargs={"literal_binds": True, "render_postcompile": True},
    )
    rows = await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {compiled}"))
    return "\n".join(str(row[0]) for row in rows)


class TestFullUniverseCost:
    async def test_the_dataset_is_production_sized(self, perf_session: AsyncSession) -> None:
        rows = (await perf_session.execute(text("SELECT count(*) FROM factor_daily"))).scalar_one()
        assert rows >= PERF_INSTRUMENTS * PERF_DAYS

    async def test_a_full_universe_screen_returns_under_the_cold_budget(
        self, perf_session: AsyncSession
    ) -> None:
        """Cold: a fresh connection, no cache, statement compiled from scratch."""
        definition = full_universe_screen()
        started = time.perf_counter()
        result = await execute_screen(
            perf_session, definition, as_of=AS_OF, data_version=DATA_VERSION
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert result.result_count >= PERF_INSTRUMENTS
        assert elapsed_ms < COLD_BUDGET_MS, f"{elapsed_ms:.1f} ms > {COLD_BUDGET_MS} ms"

    async def test_the_heaviest_shape_also_fits_the_budget(
        self, perf_session: AsyncSession
    ) -> None:
        """Decile bucketing plus three ranking factors — four window functions in one statement."""
        definition = full_universe_screen(
            apply_filters_on="decile_5",
            min_return_1y=Decimal("-50"),
            factor_two=ExtraFactor(enabled=True, sort_by="vol_12m", sort_direction="asc"),
            factor_three=ExtraFactor(enabled=True, sort_by="beta_12m", sort_direction="asc"),
        )
        started = time.perf_counter()
        result = await execute_screen(
            perf_session, definition, as_of=AS_OF, data_version=DATA_VERSION
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert result.result_count > 0
        assert elapsed_ms < COLD_BUDGET_MS, f"{elapsed_ms:.1f} ms > {COLD_BUDGET_MS} ms"


class TestThePlan:
    """What the planner does with the statement.

    Prompt 6 asks this suite to "assert with EXPLAIN that it uses the (date, marketcap_cr) index",
    and until Prompt 16 it could not: ``factor_daily`` carried **two** indexes whose leading
    column is ``date`` — ``ix_factor_daily_date`` and ``ix_factor_daily_date_marketcap_cr`` — and
    the narrower one was a strictly cheaper way to answer ``WHERE date = :as_of``, so PostgreSQL
    picked it every time and the composite was dead weight.

    Migration 0008 drops the narrow index and rebuilds the composite with
    ``INCLUDE (instrument_id)``. ``(date)`` is a prefix of ``(date, marketcap_cr)``, so nothing
    the narrow index served is lost, the nightly ``compute_factors`` step writes one index instead
    of two, and docs/06 §step 3's decile bucketing can now read ``(instrument_id, marketcap_cr)``
    for one date index-only. Prompt 6's criterion is therefore asserted here **as written**.
    """

    async def test_factor_daily_is_reached_by_an_index_not_a_sequential_scan(
        self, perf_session: AsyncSession
    ) -> None:
        plan = await _explain(perf_session, full_universe_screen())
        assert "Seq Scan on factor_daily" not in plan, plan
        assert DATE_MARKETCAP_INDEX in plan, plan

    async def test_the_point_in_time_universe_join_is_index_driven(
        self, perf_session: AsyncSession
    ) -> None:
        """``ix_index_member_daily_date_index_id`` — the other half of the hot path."""
        plan = await _explain(perf_session, full_universe_screen())
        assert "Seq Scan on index_member_daily" not in plan, plan
        assert "ix_index_member_daily_date_index_id" in plan, plan

    async def test_baskfy_bucketing_stays_index_driven(self, perf_session: AsyncSession) -> None:
        plan = await _explain(perf_session, full_universe_screen(apply_filters_on="decile_1"))
        assert "Seq Scan on factor_daily" not in plan, plan
