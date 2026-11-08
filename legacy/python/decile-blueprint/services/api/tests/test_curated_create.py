"""SC8 create API — PRIVATE STOCK basket + GENESIS version; no broker path (leaf 2.2)."""

from __future__ import annotations

import inspect
from decimal import Decimal
from typing import Protocol
from unittest.mock import AsyncMock, MagicMock

import pytest

from baskfy_api.app import create_app
from baskfy_api.problems import Problem
from baskfy_api.routers import curated_create
from baskfy_api.routers.curated_create import (
    ConstituentIn,
    CreateBasketIn,
    CreateBasketOut,
    create_private_basket,
)


class _HasPrimaryKey(Protocol):
    """What ``AsyncSession.refresh`` fills in on the rows this router flushes.

    The fake below stands in for the database's identity assignment. Typing the parameter as
    ``object`` and reaching for ``obj.id`` needs an ``attr-defined`` type-ignore comment, which
    CLAUDE.md house rule 3 forbids; the structural type says the honest thing instead — this
    callback only ever sees rows that have a nullable integer primary key.
    """

    id: int | None


def test_openapi_exposes_create_basket() -> None:
    paths = create_app().openapi()["paths"]
    assert "/api/v1/cb/baskets" in paths
    assert "post" in paths["/api/v1/cb/baskets"]


def test_create_router_has_no_broker_path() -> None:
    source = inspect.getsource(curated_create)
    for forbidden in ("OrderGateway", "place_order", "kc.place_order", "/execute"):
        assert forbidden not in source


def test_create_requires_private_stock_manual_genesis_in_source() -> None:
    source = inspect.getsource(curated_create)
    assert 'visibility="PRIVATE"' in source
    assert 'type="STOCK"' in source
    assert 'source="MANUAL"' in source
    assert 'label="GENESIS"' in source
    assert "assert_weights_sum_to_one" in source
    assert "scoped_sole_user_id" in source


@pytest.mark.asyncio
async def test_create_rejects_weight_sum_not_one(monkeypatch: pytest.MonkeyPatch) -> None:
    session = AsyncMock()
    principal = MagicMock(user_id=9)

    async def _sole(_session: object, _uid: int | None) -> int:
        return 42

    monkeypatch.setattr(curated_create, "scoped_sole_user_id", _sole)

    body = CreateBasketIn(
        name="Bad Weights",
        constituents=[
            ConstituentIn(symbol="AAA", weight=Decimal("0.6000")),
            ConstituentIn(symbol="BBB", weight=Decimal("0.3000")),
        ],
    )
    with pytest.raises(Problem):
        await create_private_basket(body, session, principal)


@pytest.mark.asyncio
async def test_create_persists_private_genesis(monkeypatch: pytest.MonkeyPatch) -> None:
    session = AsyncMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.add = MagicMock()
    session.scalar = AsyncMock(return_value=11)  # manager id

    inst_a = MagicMock(id=101, symbol="AAA")
    inst_b = MagicMock(id=102, symbol="BBB")
    scalar_result = MagicMock()
    scalar_result.all = MagicMock(return_value=[inst_a, inst_b])
    session.scalars = AsyncMock(return_value=scalar_result)

    async def _refresh(obj: _HasPrimaryKey) -> None:
        if obj.id is None:
            obj.id = 1

    session.refresh = AsyncMock(side_effect=_refresh)

    principal = MagicMock(user_id=9)

    async def _sole(_session: object, _uid: int | None) -> int:
        return 42

    async def _slug(_session: object, base: str) -> str:
        return base

    monkeypatch.setattr(curated_create, "scoped_sole_user_id", _sole)
    monkeypatch.setattr(curated_create, "_unique_slug", _slug)

    body = CreateBasketIn(
        name="My Basket",
        constituents=[
            ConstituentIn(symbol="AAA", weight=Decimal("0.5000")),
            ConstituentIn(symbol="BBB", weight=Decimal("0.5000")),
        ],
    )
    result = await create_private_basket(body, session, principal)
    assert isinstance(result, CreateBasketOut)
    assert result.visibility == "PRIVATE"
    assert result.type == "STOCK"
    assert result.source == "MANUAL"
    assert result.version_no == 1
    assert result.label == "GENESIS"
    assert len(result.constituents) == 2
    session.commit.assert_awaited()
