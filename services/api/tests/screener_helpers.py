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

from decile_api.screener import purge_screen_cache
from decile_api.seed import (
    seed_index_snapshots,
    seed_market_health,
    seed_reference,
    seed_reference_fixture,
    seed_trading_days,
)
from decile_core.models import FactorDaily, IndexMemberDaily, Instrument, PipelineRun
from decile_core.reference_export import to_rows
from decile_core.seed_data import NSE_EXCHANGE_ID

ENV_VAR: Final = "DECILE_TEST_DATABASE_URL"
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
    return {k: v for k, v in os.environ.items() if k != "DECILE_DATABASE_URL"}


def migrate(url: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=API_DIR,
        env={**_clean_env(), "DECILE_DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=True,
    )


async def _reset_and_seed(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
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
    """
    now = dt.datetime(2026, 8, 18, 14, 0, tzinfo=dt.UTC)
    session.add(
        PipelineRun(
            trade_date=trade_date,
            status="succeeded",
            started_at=now,
            finished_at=now,
            data_version=data_version,
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
    return [str(row["symbol"]) for row in to_rows().factors]
