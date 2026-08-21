"""Fixtures for the tests that need a live PostgreSQL + TimescaleDB.

They are marked ``db`` and skip when ``DECILE_TEST_DATABASE_URL`` is unset, so the default
``make test`` stays runnable without Docker while ``make test-db`` exercises the real thing.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import api_helpers
import httpx
import pytest
import pytest_asyncio
import screener_helpers
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine

from decile_api import idempotency

ENV_VAR = "DECILE_TEST_DATABASE_URL"


def database_url() -> str | None:
    return os.environ.get(ENV_VAR)


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    url = database_url()
    if url is None:  # pragma: no cover - guarded by requires_db
        pytest.skip(f"{ENV_VAR} is not set")
    created = create_async_engine(url, poolclass=None)
    try:
        yield created
    finally:
        await created.dispose()


@pytest_asyncio.fixture
async def clean_database(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """An empty schema, so migration tests start from nothing every time."""
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    async with engine.connect() as connection:
        yield connection


# --- Prompt 6: the screener suites -------------------------------------------
#
# A module-scoped, freshly seeded database rather than a session-scoped one, because the
# neighbouring suites in this directory drop the schema as part of what they assert. See
# ``screener_helpers``.


@pytest.fixture(scope="module")
def seeded_url() -> str:
    return screener_helpers.seeded_database()


@pytest_asyncio.fixture
async def screener_session(seeded_url: str) -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is always rolled back.

    Tests add synthetic instruments and factor rows freely; the seeded reference export is
    restored for the next test by the rollback, not by a truncate-and-reseed.
    """
    engine = create_async_engine(seeded_url)
    connection = await engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


@pytest_asyncio.fixture
async def screen_cache() -> AsyncIterator[Redis]:
    """A live Redis with the ``screen:`` namespace emptied around the test.

    The cache is process-external state: a key left behind by one test is a cache hit in the next,
    and the determinism assertions would then be comparing a stale payload with itself.

    Async, because that is what ``decile_api.screener.ScreenCache`` is — see the note there.
    """
    client: Redis = Redis.from_url(
        os.environ.get("DECILE_REDIS_URL", "redis://localhost:6380/0"), decode_responses=True
    )
    try:
        await client.ping()
    except RedisError:  # pragma: no cover - guarded by the redis marker
        pytest.skip("no Redis; run `make up`")
    await screener_helpers.flush_screen_namespace(client)
    try:
        yield client
    finally:
        await screener_helpers.flush_screen_namespace(client)
        await client.aclose()


# --- Prompt 7: the HTTP contract suites --------------------------------------
#
# ``api_settings`` and ``running_app`` live in ``api_helpers`` so the test modules can import them
# (mypy does not check conftest files, and a module that imports from one loses its types).


@pytest_asyncio.fixture
async def clean_redis_namespaces() -> AsyncIterator[None]:
    """Empty the screen cache and the idempotency records around every HTTP test.

    Both outlive a rolled-back transaction — the database forgets a screen the test created, Redis
    does not — so without this a replayed ``Idempotency-Key`` from an earlier run would name a row
    that no longer exists, and a cached screen result would be served to a later assertion.
    """
    client: Redis = Redis.from_url(os.environ.get("DECILE_REDIS_URL", "redis://localhost:6380/0"))
    try:
        await client.ping()
    except RedisError:  # pragma: no cover - guarded by the redis marker
        pytest.skip("no Redis; run `make up`")
    await _flush(client)
    try:
        yield
    finally:
        await _flush(client)
        await client.aclose()


async def _flush(client: Redis) -> None:
    await screener_helpers.flush_screen_namespace(client)
    keys = [key async for key in client.scan_iter(match=f"{idempotency.KEY_PREFIX}*")]
    if keys:
        await client.delete(*keys)


@pytest_asyncio.fixture
async def api(
    seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
) -> AsyncIterator[httpx.AsyncClient]:
    del clean_redis_namespaces
    async with api_helpers.running_app(
        api_helpers.api_settings(seeded_url), screener_session
    ) as client:
        yield client
