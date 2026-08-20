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
from screener_helpers import AS_OF, DATA_VERSION, requires_db, seeded_database
from sqlalchemy import create_mock_engine, text
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from decile_api.screener import execute_screen
from decile_core.screen_definition import ExtraFactor, ScreenDefinition
from decile_core.screener import build_screen_query
from decile_core.seed_data import EXAMPLE_SCREENS
from decile_core.universes import UNIVERSE_BY_SLUG

pytestmark = [pytest.mark.db, requires_db]

#: docs/02 §"Why Postgres": "~2,300 NSE instruments".
PERF_INSTRUMENTS = 2300
#: Enough trading days that a single date is ~1% of the table, as it is in production.
PERF_DAYS = 120

#: docs/11 §"Performance budgets" allows 800 ms cold for a whole request; Prompt 6 holds the SQL
#: itself to 300 ms, which is the part this measures — no HTTP, no serialisation, no cache.
COLD_BUDGET_MS = 300.0

#: The two indexes that can serve ``WHERE factor_daily.date = :as_of``. Prompt 6 names the second
#: one; see :class:`TestThePlan` for why PostgreSQL never picks it.
DATE_INDEX = "ix_factor_daily_date"
DATE_MARKETCAP_INDEX = "ix_factor_daily_date_marketcap_cr"

#: Rendering-only PostgreSQL dialect. ``create_mock_engine`` is the typed way to get one; the
#: dialect classes have untyped constructors (see ``decile_core.screener``).
PG_DIALECT: Dialect = create_mock_engine("postgresql+asyncpg://", lambda *args: None).dialect

TOTAL_MARKET = UNIVERSE_BY_SLUG["nifty-total-market"].index_id
INVESTING_001 = next(s for s in EXAMPLE_SCREENS if s.name == "Investing 001").definition


def _generate_sql(index_id: int, instruments: int, days: int) -> tuple[str, str, str]:
    """Three statements that build a production-sized single-date universe.

    Written as ``INSERT … SELECT`` over ``generate_series`` rather than as Python round trips:
    276,000 fact rows through the ORM would take longer to insert than the whole rest of the
    suite takes to run, and none of that work is what is under test.
    """
    instrument_sql = f"""
        INSERT INTO instrument (exchange_id, symbol, name, series, instrument_type, is_active)
        SELECT 1, 'PERF' || lpad(i::text, 5, '0'), 'PERF ' || i, 'EQ', 'EQ', true
        FROM generate_series(1, {instruments}) AS i
    """
    dates_cte = f"""
        WITH perf_dates AS (
            SELECT date FROM trading_day
            WHERE exchange_id = 1 AND is_trading_day AND date <= DATE '{AS_OF.isoformat()}'
            ORDER BY date DESC LIMIT {days}
        ),
        perf_instruments AS (
            SELECT id, row_number() OVER (ORDER BY id) AS n
            FROM instrument WHERE symbol LIKE 'PERF%'
        )
    """
    factor_sql = f"""
        {dates_cte}
        INSERT INTO factor_daily (
            instrument_id, date, close, close_raw, ret_12m, sharpe_12m, sharpe_6m, sharpe_3m,
            sharpe_1m, vol_12m, beta_12m, ma_200, marketcap_cr, median_vol_12m, series,
            universe_mask, top_beta_mask, top_volatility_mask
        )
        SELECT p.id, d.date,
               100 + (p.n % 900), 100 + (p.n % 900),
               (p.n % 500) - 100, ((p.n % 500) - 100) / 40.0, ((p.n % 470) - 100) / 40.0,
               ((p.n % 430) - 100) / 40.0, ((p.n % 390) - 100) / 40.0,
               0.15 + (p.n % 400) / 1000.0, 0.5 + (p.n % 150) / 100.0, 90 + (p.n % 800),
               1000 + (p.n * 37) % 900000, 20000000 + (p.n * 991) % 5000000, 'EQ',
               {1 << (index_id - 1)}, 0, 0
        FROM perf_instruments p CROSS JOIN perf_dates d
    """
    membership_sql = f"""
        {dates_cte}
        INSERT INTO index_member_daily (index_id, date, instrument_id, source)
        SELECT {index_id}, d.date, p.id, 'nse_file'
        FROM perf_instruments p CROSS JOIN perf_dates d
    """
    return instrument_sql, factor_sql, membership_sql


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
                for statement in _generate_sql(TOTAL_MARKET, PERF_INSTRUMENTS, PERF_DAYS):
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
    """What the planner does with the statement, and one finding about the schema.

    Prompt 6 asks this suite to "assert with EXPLAIN that it uses the (date, marketcap_cr) index".
    It does not, and the reason is not the query: ``factor_daily`` carries **two** indexes whose
    leading column is ``date`` — ``ix_factor_daily_date`` and ``ix_factor_daily_date_marketcap_cr``
    — and the narrower one is a strictly cheaper way to answer ``WHERE date = :as_of``. Neither
    can be an index-only scan, because the screen needs ``instrument_id`` and the row itself, so
    the wider index buys nothing and costs more pages to walk. Dropping the narrow index makes
    PostgreSQL choose the composite immediately, which is how we know the composite is shadowed
    rather than unusable.

    That is a redundancy in the Prompt 1 schema, not a property of this query, so it is written up
    in ``docs/06a-screener-implementation-notes.md`` for Prompt 16 rather than fixed here by a
    migration written to make a test pass. What these tests assert is the thing that actually
    matters and that the criterion is a proxy for: the hot path is index-driven at both ends.
    """

    async def test_factor_daily_is_reached_by_an_index_not_a_sequential_scan(
        self, perf_session: AsyncSession
    ) -> None:
        plan = await _explain(perf_session, full_universe_screen())
        assert "Seq Scan on factor_daily" not in plan, plan
        assert DATE_INDEX in plan or DATE_MARKETCAP_INDEX in plan, plan

    async def test_the_point_in_time_universe_join_is_index_driven(
        self, perf_session: AsyncSession
    ) -> None:
        """``ix_index_member_daily_date_index_id`` — the other half of the hot path."""
        plan = await _explain(perf_session, full_universe_screen())
        assert "Seq Scan on index_member_daily" not in plan, plan
        assert "ix_index_member_daily_date_index_id" in plan, plan

    async def test_decile_bucketing_stays_index_driven(self, perf_session: AsyncSession) -> None:
        plan = await _explain(perf_session, full_universe_screen(apply_filters_on="decile_1"))
        assert "Seq Scan on factor_daily" not in plan, plan
