"""Database access for Celery tasks.

Celery tasks are synchronous; the rest of the codebase is async SQLAlchemy (docs/02). Rather than
add a second driver and a second connection story, each task runs the async session inside its
own ``asyncio.run`` — a Celery task is a process-level unit of work, so owning an event loop for
its duration costs nothing and keeps exactly one database configuration in the repo.

The alternative — adding psycopg for a sync engine — would mean two drivers, two pool
configurations and two sets of type quirks, for no behavioural gain.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_api.settings import get_settings


@asynccontextmanager
async def session_scope(database_url: str | None = None) -> AsyncIterator[AsyncSession]:
    """One transactional session; commits on clean exit, rolls back on any exception."""
    engine = create_async_engine(database_url or get_settings().database_url, pool_pre_ping=True)
    try:
        session_maker = async_sessionmaker(engine, expire_on_commit=False)
        async with session_maker() as session, session.begin():
            yield session
    finally:
        await engine.dispose()


def run_in_session[T](
    operation: Callable[[AsyncSession], Awaitable[T]], database_url: str | None = None
) -> T:
    """Run ``operation`` against a fresh transactional session, from synchronous code."""

    async def runner() -> T:
        async with session_scope(database_url) as session:
            return await operation(session)

    return asyncio.run(runner())
