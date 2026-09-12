"""Shared setup for the screener's database tests (Prompt 6).

Kept out of ``conftest.py`` so the test modules can import it: pytest gives test files no package,
so one test module cannot import another, and two ``conftest`` modules in different test trees
collide for mypy. Same arrangement as ``services/worker/tests/helpers.py``.

Every module that uses these gets a freshly migrated schema seeded with the docs/13 reference
export, because the neighbouring suites (``test_migrations``, ``test_seed``) drop the schema as
part of what they assert. Depending on test-file ordering for a working database would make this
suite pass or fail for reasons that have nothing to do with the screener.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_api.screener import purge_screen_cache
from baskfy_api.seed import (
    seed_index_snapshots,
    seed_market_health,
    seed_reference,
    seed_reference_fixture,
    seed_trading_days,
)
from baskfy_core.models import (
    FactorDaily,
    IndexMemberDaily,
    Instrument,
    PipelineRun,
    PipelineRunStep,
)
from baskfy_core.reference_export import ReferenceRows, read_export, to_rows
from baskfy_core.seed_data import NSE_EXCHANGE_ID

_REFERENCE_EXPORT = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "fixtures"
    / "reference-screen-export-2026-08-18.csv"
)


def _reference_rows() -> ReferenceRows:
    return to_rows(read_export(_REFERENCE_EXPORT))


ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"
API_DIR: Final = Path(__file__).resolve().parents[1]

#: docs/13: the reference export's trade date, and therefore this suite's as-of.
AS_OF: Final = dt.date(2026, 8, 18)

#: The ``data_version`` the seeded ``pipeline_run`` publishes. Any positive integer would do; a
#: fixed one keeps cache keys stable between runs, which the determinism tests read.
DATA_VERSION: Final = 1

requires_db = pytest.mark.skipif(
    os.environ.get(ENV_VAR) is None,
    reason=f"{ENV_VAR} is not set; run `make up` and export it",
)


def database_url() -> str:
    url = os.environ.get(ENV_VAR)
    if url is None:  # pragma: no cover - guarded by requires_db
        pytest.skip(f"{ENV_VAR} is not set")
    return url


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "BASKFY_DATABASE_URL"}


def migrate(url: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=API_DIR,
        env={**_clean_env(), "BASKFY_DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=True,
    )


async def _reset_and_seed(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            # DROPPING THE SCHEMA IS NOT ENOUGH, AND THE DIFFERENCE IS INVISIBLE UNTIL AN INSERT.
            #
            # TimescaleDB keeps its chunks, compressed hypertables and continuous-aggregate
            # materialisations in `_timescaledb_internal`, not in `public`. `DROP SCHEMA public
            # CASCADE` removes the hypertable *parents* and leaves those behind: after a reset
            # `timescaledb_information.chunks` reports zero while `_timescaledb_internal` still
            # holds `_hyper_*_chunk`, `_materialized_hypertable_*` and `_compressed_hypertable_*`
            # relations from the previous generation.
            #
            # Each orphan carries its own foreign keys, still pointing at the dropped tables. The
            # first insert into a hypertable is then routed into stale storage and fails with
            # something that looks like a seeding bug and is not — the shape it took here was
            #
            #   insert on "_hyper_3_5_chunk" violates
            #   "5_10_fk_index_member_daily_index_id_index_def"
            #   Key (index_id)=(5) is not present in table "index_def"
            #
            # while `index_def` demonstrably held all fourteen rows. Dropping the extension is what
            # clears the internal schema; migration 0001 recreates it with `CREATE EXTENSION IF NOT
            # EXISTS`, so the reset stays a reset.
            await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await connection.execute(text("DROP EXTENSION IF EXISTS timescaledb CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()

    migrate(url)

    engine = create_async_engine(url)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session, session.begin():
            await seed_reference(session)
            await seed_trading_days(session, today=dt.date(2026, 12, 31))
            await seed_reference_fixture(session)
            # Prompt 11's surfaces. Cheap — 117 indices x 30 days is one bulk insert, and twelve
            # breadth rows are twelve aggregates over the 271 factor rows already loaded.
            await seed_index_snapshots(session)
            await seed_market_health(session, AS_OF)
            await publish_run(session, AS_OF, DATA_VERSION)
    finally:
        await engine.dispose()


def seeded_database() -> str:
    """A migrated database holding the 271-row reference export and one published run."""
    url = database_url()
    asyncio.run(_reset_and_seed(url))
    return url


async def publish_run(session: AsyncSession, trade_date: dt.date, data_version: int) -> None:
    """A ``pipeline_run`` whose gate passed — what docs/06 §step 1 calls "published".

    Without one, ``resolve_as_of`` refuses to serve any date at all, which is the correct
    behaviour and exactly why the fixture has to write one.

    **Not** ``baskfy_api.seed.seed_published_run``, and the difference matters: the seeder is
    idempotent and returns without inserting when a run already exists for the date (re-running
    `make seed` must not pile up runs), whereas the cache-invalidation tests exist precisely to
    publish a *second* version for the same date. Same shape, different contract.

    The ``publish`` step row is written for the same reason the seeder writes one: docs/03 says
    "every step writes a row in `pipeline_run_step`", and `baskfy_api.integrity`'s
    ``published_runs_have_steps`` assertion would otherwise fire on the fixture rather than on a
    bad backup.
    """
    now = dt.datetime(2026, 8, 18, 14, 0, tzinfo=dt.UTC)
    run = PipelineRun(
        trade_date=trade_date,
        status="succeeded",
        started_at=now,
        finished_at=now,
        data_version=data_version,
    )
    session.add(run)
    await session.flush()
    session.add(
        PipelineRunStep(
            run_id=run.id,
            step="publish",
            status="succeeded",
            rows_in=1,
            rows_out=1,
            duration_ms=0,
            error={"seeded": True, "data_version": data_version},
        )
    )
    await session.flush()


async def add_instrument(
    session: AsyncSession,
    symbol: str,
    *,
    series: str = "EQ",
    instrument_type: str = "EQ",
) -> int:
    instrument = Instrument(
        exchange_id=NSE_EXCHANGE_ID,
        symbol=symbol,
        name=f"{symbol} LIMITED",
        series=series,
        instrument_type=instrument_type,
        is_active=True,
    )
    session.add(instrument)
    await session.flush()
    return instrument.id


async def add_member(
    session: AsyncSession, index_id: int, instrument_id: int, on: dt.date = AS_OF
) -> None:
    session.add(
        IndexMemberDaily(index_id=index_id, date=on, instrument_id=instrument_id, source="nse_file")
    )
    await session.flush()


async def add_factor_row(
    session: AsyncSession,
    instrument_id: int,
    on: dt.date = AS_OF,
    values: Mapping[str, object] | None = None,
) -> None:
    """One ``factor_daily`` row with only the columns a test cares about set.

    Everything else stays NULL, which is the point: docs/06 §step 4's NULL rule is most of what
    these tests are checking, and a helper that quietly filled in defaults would hide it.
    """
    payload: dict[str, object] = {"instrument_id": instrument_id, "date": on}
    payload.update(values or {})
    session.add(FactorDaily(**payload))
    await session.flush()


async def make_row(  # noqa: PLR0913 - one parameter per row a screen needs to exist
    session: AsyncSession,
    symbol: str,
    index_id: int,
    *,
    on: dt.date = AS_OF,
    series: str = "EQ",
    member: bool = True,
    values: Mapping[str, object] | None = None,
) -> int:
    """An instrument, its membership and its factor row — the three rows a screen needs."""
    instrument_id = await add_instrument(session, symbol, series=series)
    if member:
        await add_member(session, index_id, instrument_id, on)
    await add_factor_row(session, instrument_id, on, {"series": series, **(values or {})})
    return instrument_id


async def ttl_of(client: object, key: str) -> int:
    """redis-py gives sync and async clients one signature; narrow the async client's reply."""
    getter = getattr(client, "ttl", None)
    if not callable(getter):
        raise TypeError("cache client has no ttl()")
    value = await getter(key)
    if not isinstance(value, int):
        raise TypeError(f"ttl() returned {type(value).__name__}, not an int")
    return value


async def flush_screen_namespace(client: object) -> int:
    """Delete every ``screen:*`` key — the same namespace docs/06 §Caching says publish purges."""
    return await purge_screen_cache(client)


def export_symbols_in_file_order() -> Sequence[str]:
    """The 271 symbols in the order the reference export lists them (docs/13)."""
    return [str(row["symbol"]) for row in _reference_rows().factors]


def synthetic_universe_sql(index_id: int, instruments: int, days: int) -> tuple[str, str, str]:
    """Three statements that build a production-sized universe of synthetic instruments.

    Shared by ``test_screener_performance`` (Prompt 6's cold-query budget) and
    ``test_benchmarks`` (Prompt 16's CSV-export budget, which needs 4,000 rows and the reference
    export has 271). Both need the *same* dataset shape, and two copies of it would eventually
    disagree about what "production-sized" means.

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
