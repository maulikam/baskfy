"""Async engine, session factory, and the request-scoped session dependency.

Database I/O lives here rather than in ``packages/core`` because docs/02 §"Repo layout" requires
``packages/core`` to stay I/O-free: "it takes DataFrames in and returns DataFrames out. That is
what makes the factor math unit-testable and the backtest engine reusable."

One engine per process, created at startup and disposed at shutdown (see
``decile_api.app.lifespan``). A pool that is rebuilt per request would spend more time opening
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

from decile_api.settings import get_settings


def create_engine(url: str | None = None) -> AsyncEngine:
    return create_async_engine(url or get_settings().database_url, pool_pre_ping=True)


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
