"""T8.1 mark-as-invested — records a broker book; never an order path."""

from __future__ import annotations

import inspect
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from baskfy_api.app import create_app
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.routers import curated_investments
from baskfy_api.routers.curated_investments import HoldingBody, MarkBody, mark_as_invested
from baskfy_core.curated_accounting import platform_fee
from baskfy_core.models import (
    BrokerAccount,
    CbFeeLedger,
    CbInvestment,
    CbInvestmentHolding,
    CbOrderBatch,
)


def test_openapi_exposes_mark_and_reads() -> None:
    paths = create_app().openapi()["paths"]
    assert "post" in paths["/api/v1/cb/investments/mark"]
    assert "get" in paths["/api/v1/cb/investments"]
    assert "post" not in paths["/api/v1/cb/investments"]
    assert "get" in paths["/api/v1/cb/investments/{investment_id}"]
    assert "get" in paths["/api/v1/cb/fees"]


def test_investment_row_names_basket_source_and_visibility() -> None:
    """The one-book view classifies manager / rule / by-hand from these fields, not the name."""
    props = create_app().openapi()["components"]["schemas"]["InvestmentRowOut"]["properties"]
    assert "basket_source" in props
    assert "visibility" in props


def test_router_has_no_broker_path() -> None:
    source = inspect.getsource(curated_investments)
    for forbidden in ("OrderGateway", "place_order", "kc.place_order", "/execute", "kiteconnect"):
        assert forbidden not in source
    service = inspect.getsource(
        __import__("baskfy_api.curated_investments", fromlist=["mark_invested"])
    )
    for forbidden in ("OrderGateway", "place_order", "/execute", "kiteconnect"):
        assert forbidden not in service


def test_mark_source_never_writes_executed() -> None:
    from baskfy_api import curated_investments as service  # noqa: PLC0415 - local to this test

    source = inspect.getsource(service.mark_invested)
    assert 'status="PLANNED"' in source
    assert "EXECUTED" not in source


@pytest.mark.asyncio
async def test_mark_requires_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    session = AsyncMock()
    principal = MagicMock(user_id=9)
    body = MarkBody(
        basket_slug="momentum-scan",
        amount=Decimal("10000"),
        confirmed=False,
        holdings=[HoldingBody(symbol="CUPID", qty=Decimal("10"), avg_price=Decimal("80"))],
    )
    with pytest.raises(Problem) as err:
        await mark_as_invested(body, session, principal)
    assert err.value.type is ProblemType.INVALID_SCREEN_DEFINITION


@pytest.mark.asyncio
# 57 statements, nearly all of it mock scaffolding for one write path. Splitting it would put
# the arrangement in a fixture and leave the assertions unable to say what they depend on.
async def test_mark_persists_planned_batch_and_fee(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from baskfy_api import curated_investments as service  # noqa: PLC0415 - local to this test
    from baskfy_api.curated_investments import HoldingIn, mark_invested  # noqa: PLC0415

    session = AsyncMock()
    session.commit = AsyncMock()
    added: list[object] = []

    def _add(obj: object) -> None:
        added.append(obj)

    session.add = _add

    async def _assign_ids() -> None:
        for obj in added:
            if getattr(obj, "id", None) is not None:
                continue
            if isinstance(obj, CbInvestment):
                obj.id = 7
            elif isinstance(obj, CbOrderBatch):
                obj.id = 3
            elif isinstance(obj, CbFeeLedger):
                obj.id = 11
            # Five models reach `session.add` on this path: four from `mark_invested` itself and
            # `BrokerAccount` from `ensure_broker_account`, which it calls. The bare `else` this
            # replaced hid that fifth one — it took the default silently, so nothing said the
            # write path touches an account table.
            elif isinstance(obj, CbInvestmentHolding | BrokerAccount):
                obj.id = 1

    session.flush = AsyncMock(side_effect=_assign_ids)
    session.refresh = AsyncMock()

    basket = MagicMock(id=5, slug="momentum-scan", name="Momentum")
    version = MagicMock(id=9)
    inst = MagicMock(id=101, symbol="CUPID")
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=[inst])
    session.scalars = AsyncMock(return_value=scalars)

    scalar_calls = {"n": 0}

    async def _scalar(_stmt: object) -> object:
        scalar_calls["n"] += 1
        n = scalar_calls["n"]
        if n == 1:
            return basket
        if n == 2:
            return None
        if n == 3:
            return version
        return None

    session.scalar = AsyncMock(side_effect=_scalar)

    async def _sole(_session: object, _uid: int | None) -> int:
        return 42

    monkeypatch.setattr(service, "scoped_sole_user_id", _sole)

    result = await mark_invested(
        session,
        principal_user_id=9,
        basket_slug="momentum-scan",
        amount=Decimal("7000"),
        confirmed=True,
        holdings=[HoldingIn(symbol="CUPID", qty=Decimal("10"), avg_price=Decimal("80"))],
        desk_plan_id=None,
    )
    assert result.status == "ACTIVE"
    kinds = [type(obj).__name__ for obj in added]
    assert "CbInvestment" in kinds
    assert "CbInvestmentHolding" in kinds
    assert "CbOrderBatch" in kinds
    assert "CbFeeLedger" in kinds
    batch = next(obj for obj in added if isinstance(obj, CbOrderBatch))
    assert batch.status == "PLANNED"
    assert batch.kind == "BUY"
    assert batch.user_id == 42
    assert batch.broker_account_id is not None
    investment = next(obj for obj in added if isinstance(obj, CbInvestment))
    assert investment.broker_account_id == batch.broker_account_id
    fee = next(obj for obj in added if isinstance(obj, CbFeeLedger))
    expected = platform_fee("BUY", Decimal("7000"))
    assert fee.total == expected.total
    assert fee.collected is False
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_mark_rejects_second_active(monkeypatch: pytest.MonkeyPatch) -> None:
    from baskfy_api import curated_investments as service  # noqa: PLC0415 - local to this test
    from baskfy_api.curated_investments import HoldingIn, mark_invested  # noqa: PLC0415

    session = AsyncMock()
    basket = MagicMock(id=5, slug="momentum-scan")
    scalar_calls = {"n": 0}

    async def _scalar(_stmt: object) -> object:
        scalar_calls["n"] += 1
        if scalar_calls["n"] == 1:
            return basket
        return 99

    session.scalar = AsyncMock(side_effect=_scalar)

    async def _sole(_session: object, _uid: int | None) -> int:
        return 42

    monkeypatch.setattr(service, "scoped_sole_user_id", _sole)

    with pytest.raises(Problem) as err:
        await mark_invested(
            session,
            principal_user_id=9,
            basket_slug="momentum-scan",
            amount=Decimal("1000"),
            confirmed=True,
            holdings=[HoldingIn(symbol="CUPID", qty=Decimal("1"), avg_price=Decimal("10"))],
            desk_plan_id=None,
        )
    assert err.value.type is ProblemType.STALE_DATA_VERSION
    assert err.value.status == 409
