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
from baskfy_api.problems import Problem
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
async def test_scoped_sole_refuses_a_foreign_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A principal that is not the sole tenant is refused, not promoted to it (M43.4).

    This test asserted the opposite until M43.4 — that a foreign principal "still resolves to
    the sole tenant" — and that assertion was the vulnerability written down as a specification.
    `POST /auth/register` is ungated, so anybody who could reach the API could create an account
    and be collapsed onto the operator.

    Proved by execution before the change: sole tenant 43, a freshly registered account 44,
    `scoped_sole_user_id(session, 44) -> 43`, and `GET /watchlist` with that account's token
    answering 200 with the operator's rows. After: 404 on both the read and the delete.

    Sole-tenant means one tenant. Refusing is the honest expression of that, and it is what Law
    2's multi-tenant clause says to do with a mismatch.
    """
    sole = 7
    foreign = 1001

    async def _fake_resolve(_session: object) -> int:
        return sole

    monkeypatch.setattr(curated_tenant, "resolve_sole_user_id", _fake_resolve)
    session = AsyncMock()

    with pytest.raises(Problem) as refused:
        await scoped_sole_user_id(session, foreign)
    assert refused.value.status == 404, "a 403 would confirm the surface holds somebody's data"

    # The sole tenant itself, and an unauthenticated internal caller, are unaffected.
    assert await scoped_sole_user_id(session, sole) == sole
    assert await scoped_sole_user_id(session, None) == sole


@pytest.mark.asyncio
async def test_a_foreign_principal_never_reaches_a_query_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M43.4 strengthened this from "scopes to sole_user" to "never runs".

    It used to assert that a foreign principal's list query compiled with ``user_id = 42`` and
    returned nothing. That reads as isolation and is the opposite: the query WAS the sole
    tenant's, and it came back empty only because the mock had no rows. Against a real database
    it returned the operator's watchlist — measured, 200 with their rows.

    The stronger property is that the handler refuses before it builds a statement, so there is
    no query to inspect and nothing that could return the wrong rows.
    """
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

    with pytest.raises(Problem) as refused:
        await explore.list_watchlist(session, principal)
    assert refused.value.status == 404
    assert captured == [], "the handler must refuse before it queries anything"

    # And the sole tenant itself still gets a properly scoped statement.
    principal.user_id = sole
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
