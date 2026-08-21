"""Async engine, session factory, and the request-scoped session dependency.

Database I/O lives here rather than in ``packages/core`` because docs/02 §"Repo layout" requires
``packages/core`` to stay I/O-free: "it takes DataFrames in and returns DataFrames out. That is
what makes the factor math unit-testable and the backtest engine reusable."

One engine per process, created at startup and disposed at shutdown (see
``baskfy_api.app.lifespan``). A pool that is rebuilt per request would spend more time opening
PostgreSQL connections than running the screen.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from baskfy_api.settings import Settings, get_settings


def create_engine(url: str | None = None, settings: Settings | None = None) -> AsyncEngine:
    """One pooled engine for the process.

    Pool sizing (Prompt 16 deliverable 6)
    -------------------------------------
    The ceiling is PostgreSQL's ``max_connections`` (100 by default), shared by every API process,
    every Celery worker and every human with ``psql`` open. The budget that matters is
    ``pool_size + max_overflow`` **per process**, multiplied by the number of processes.

    The defaults — 10 steady, 10 overflow — are sized for the box docs/11 §"Cost envelope" names
    (8 vCPU / 32 GB) running one uvicorn worker per core: 8 x 20 = 160 at absolute peak against a
    ``max_connections`` that must therefore be raised, or 8 x 10 = 80 in the steady state a
    healthy service actually sits in. The overflow is what absorbs a burst without refusing it;
    the steady size is what a screen run finds already open, because opening a PostgreSQL
    connection costs more than the docs/11 warm budget for the whole request.

    Why 10 and not 30: the API's queries are short and CPU-bound *inside PostgreSQL*, so beyond
    roughly the server's core count more concurrent statements do not finish sooner — they finish
    together, later, with every one of them past its budget. A pool smaller than the arrival rate
    queues in the *application*, where ``pool_timeout`` can refuse cleanly, rather than in the
    database, where nothing can.

    ``pool_pre_ping`` costs one round trip per checkout and is kept: a connection killed by a
    failover or an idle timeout otherwise surfaces as a 500 on a user's request.

    pgbouncer
    ---------
    docs/02 locks no connection proxy, and none is required at this size. If one is introduced it
    must run in *transaction* pooling mode with ``BASKFY_DB_STATEMENT_CACHE_SIZE=0``: asyncpg's
    prepared-statement cache is per *server* connection, and transaction pooling breaks that
    assumption. This is documented in ``docs/DECISIONS.md`` §16.
    """
    resolved = settings or get_settings()
    connect_args: dict[str, object] = {
        # asyncpg's own knob. Named here rather than left to the driver default so that the
        # pgbouncer case above is one environment variable, not a code change.
        "statement_cache_size": resolved.db_statement_cache_size,
        "prepared_statement_cache_size": resolved.db_statement_cache_size,
    }
    return create_async_engine(
        url or resolved.database_url,
        pool_pre_ping=True,
        pool_size=resolved.db_pool_size,
        max_overflow=resolved.db_max_overflow,
        pool_timeout=resolved.db_pool_timeout_seconds,
        pool_recycle=resolved.db_pool_recycle_seconds,
        connect_args=connect_args,
    )


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(url: str | None = None) -> AsyncIterator[AsyncSession]:
    """One transactional session; commits on clean exit, rolls back on any exception."""
    engine = create_engine(url)
    try:
        async with session_factory(engine)() as session, session.begin():
            yield session
    finally:
        await engine.dispose()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """The request-scoped session (Prompt 7 deliverable 1).

    One transaction per request, committed only if the handler returns without raising. A read
    endpoint commits nothing, which costs nothing; a write endpoint gets atomicity for free and
    cannot half-apply a change because serialisation failed afterwards.
    """
    maker: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


SessionDep = Annotated[AsyncSession, Depends(get_session)]
