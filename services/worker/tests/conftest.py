"""Fixtures for the pipeline tests.

These run against a real PostgreSQL + TimescaleDB, because most of what Prompt 3 asks to be proven
is about database state: idempotent upserts, a checksum that does not move, a ``data_version``
that does not advance, a cursor that survives a kill. A fake would only prove the fake behaves.

Skips cleanly when ``DECILE_TEST_DATABASE_URL`` is unset, exactly like the Prompt 1 suite.
Shared helpers live in ``helpers.py`` so test modules can import them.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest_asyncio
from helpers import database_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from decile_api.seed import seed_reference, seed_trading_days

API_DIR = Path(__file__).resolve().parents[2] / "api"

#: Every table the pipeline writes. Truncated between tests so each starts from a known state.
#:
#: `index_def` is included because `refresh_index_snapshots` registers dashboard indices on the
#: fly (docs/01 §7's ~145); without truncation those leak between tests and an "is this index new"
#: assertion silently depends on execution order. `seed_reference` restores the 14 pinned
#: universes afterwards.
PIPELINE_TABLES = (
    "ohlcv_daily, factor_daily, index_member_daily, index_snapshot_daily, market_health_daily, "
    "corporate_action, ingest_cursor, pipeline_run_step, pipeline_run, instrument, trading_day, "
    # Prompt 12's account purge is a worker task too, and it writes `app_user` — which nothing
    # else here truncates, so without these a second test run finds the first one's accounts.
    "index_def"
)

#: Accounts are *not* truncated with the rest. `TRUNCATE ... CASCADE` takes an ACCESS EXCLUSIVE
#: lock on `app_user`, and the API suite in the neighbouring tree keeps module-scoped connections
#: open against the same database — which deadlocked once. The purge tests delete their own rows
#: instead, which needs no table-level lock.
ACCOUNT_TABLES = "account_deletion, payment, app_user"


@pytest_asyncio.fixture(scope="session")
def migrated_url() -> Iterator[str]:
    """A freshly migrated database for the whole session."""
    url = database_url()
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=API_DIR,
        env={
            **{k: v for k, v in os.environ.items() if k != "DECILE_DATABASE_URL"},
            "DECILE_DATABASE_URL": url,
        },
        capture_output=True,
        text=True,
        check=True,
    )
    yield url


@pytest_asyncio.fixture
async def engine(migrated_url: str) -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(migrated_url)
    try:
        yield created
    finally:
        await created.dispose()


@pytest_asyncio.fixture
async def clean_db(engine: AsyncEngine) -> AsyncIterator[None]:
    """Truncate everything the pipeline touches, then seed the reference rows it needs."""
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {PIPELINE_TABLES} RESTART IDENTITY CASCADE"))
        # Row-level, so it cannot deadlock with a reader elsewhere in the suite. The two
        # dependent tables go first: Prompt 15's backtest suite leaves a `screen` and a
        # `backtest` row behind, and both carry a NOT NULL `user_id` that would make the account
        # delete a foreign-key violation.
        owned = "SELECT id FROM app_user WHERE email LIKE '%@example.com'"
        await connection.execute(text(f"DELETE FROM backtest WHERE user_id IN ({owned})"))
        await connection.execute(text(f"DELETE FROM screen WHERE user_id IN ({owned})"))
        await connection.execute(text("DELETE FROM app_user WHERE email LIKE '%@example.com'"))
    maker = async_sessionmaker(engine)
    async with maker() as seeding, seeding.begin():
        await seed_reference(seeding)
        await seed_trading_days(seeding, today=dt.date(2026, 12, 31))
    yield


@pytest_asyncio.fixture
async def session(engine: AsyncEngine, clean_db: None) -> AsyncIterator[AsyncSession]:
    del clean_db
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active, active.begin():
        yield active
