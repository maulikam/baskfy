"""SC11 / leaf-1.8.2 — sole-tenant watchlist + investment isolation.

Track A is single-tenant: every user-scoped ``cb_*`` query filters
``user_id == sole_user`` (``BASKFY_SOLE_USER_ID``). Another user id must not see
sole-tenant rows; foreign principals collapse to the sole id.
"""

from __future__ import annotations

import inspect
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select

from baskfy_api import curated_tenant
from baskfy_api.curated_tenant import (
    investments_for_user_stmt,
    scoped_sole_user_id,
    watchlist_items_for_user_stmt,
)
from baskfy_api.routers import explore
from baskfy_core.curated_baskets import SOLE_USER_ENV


def test_sole_tenant_documented_in_helper_module() -> None:
    """Document Track A sole-tenant: env name + helper module contract."""
    assert SOLE_USER_ENV == "BASKFY_SOLE_USER_ID"
    doc = inspect.getdoc(curated_tenant)
    assert doc is not None
    assert "sole" in doc.lower()
    assert "tenant" in doc.lower()


def test_watchlist_and_investment_stmts_always_filter_user_id() -> None:
    sole = 42
    other = 99
    wl_sql = str(
        watchlist_items_for_user_stmt(sole).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    inv_sql = str(
        investments_for_user_stmt(sole).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "user_id" in wl_sql
    assert "42" in wl_sql
    assert "user_id" in inv_sql
    assert "42" in inv_sql
    # Cross-user filter must not silently widen to another id in the sole stmt.
    assert "99" not in wl_sql
    assert "99" not in inv_sql
    other_wl = str(
        watchlist_items_for_user_stmt(other).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "99" in other_wl
    assert "42" not in other_wl


@pytest.mark.asyncio
async def test_scoped_sole_collapses_foreign_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another authenticated user_id still resolves to the sole tenant (Track A)."""
    sole = 7
    foreign = 1001

    async def _fake_resolve(_session: object) -> int:
        return sole

    monkeypatch.setattr(curated_tenant, "resolve_sole_user_id", _fake_resolve)
    session = AsyncMock()
    assert await scoped_sole_user_id(session, foreign) == sole
    assert await scoped_sole_user_id(session, sole) == sole
    assert await scoped_sole_user_id(session, None) == sole


@pytest.mark.asyncio
async def test_another_user_id_cannot_see_sole_watchlist_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """List path scopes to sole_user; foreign principal never queries another user's rows."""
    sole = 42
    foreign = 99
    captured: list[object] = []

    async def _fake_resolve(_session: object) -> int:
        return sole

    monkeypatch.setattr(curated_tenant, "resolve_sole_user_id", _fake_resolve)

    async def _execute(stmt: object, *_a: object, **_k: object) -> MagicMock:
        captured.append(stmt)
        result = MagicMock()
        result.all.return_value = []
        return result

    session = AsyncMock()
    session.execute = _execute
    principal = MagicMock()
    principal.user_id = foreign

    out = await explore.list_watchlist(session, principal)
    assert out.count == 0
    assert len(captured) == 1
    stmt = cast(Select[Any], captured[0])
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "user_id" in sql
    assert "42" in sql
    assert "99" not in sql


def test_explore_watchlist_handlers_use_scoped_sole_user() -> None:
    """Source contract: watchlist mutate/list always call scoped_sole_user_id + user_id filter."""
    for name in ("list_watchlist", "add_watchlist", "remove_watchlist"):
        src = inspect.getsource(getattr(explore, name))
        assert "scoped_sole_user_id" in src
        assert "user_id ==" in src or "CbWatchlistItem.user_id ==" in src
