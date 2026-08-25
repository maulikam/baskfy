"""T8.5 costs payload — accrued fees, never a collect POST."""

from __future__ import annotations

import inspect
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from baskfy_api.app import create_app
from baskfy_api.routers import curated_costs
from baskfy_api.routers.curated_costs import get_investment_costs
from baskfy_core.curated_accounting import platform_fee


def test_openapi_exposes_costs() -> None:
    paths = create_app().openapi()["paths"]
    assert "get" in paths["/api/v1/cb/investments/{investment_id}/costs"]
    assert "post" not in paths["/api/v1/cb/investments/{investment_id}/costs"]


def test_costs_router_has_no_collect_or_broker_path() -> None:
    source = inspect.getsource(curated_costs)
    for forbidden in (
        "OrderGateway",
        "place_order",
        "/execute",
        "fees/collect",
        "kiteconnect",
    ):
        assert forbidden not in source


@pytest.mark.asyncio
async def test_costs_returns_after_accrued_fees(monkeypatch: pytest.MonkeyPatch) -> None:
    inv = MagicMock()
    inv.id = 4
    inv.created_at = None

    async def _load(*_args: object, **_kwargs: object) -> MagicMock:
        return inv

    monkeypatch.setattr(curated_costs, "load_investment_for_user", _load)

    holding = MagicMock()
    holding.instrument_id = 1
    holding.qty = Decimal("10")
    holding.avg_price = Decimal("100")

    batch = MagicMock()
    batch.id = 8
    batch.kind = "BUY"
    batch.requested_amount = Decimal("7000")

    fee = platform_fee("BUY", Decimal("7000"))
    ledger = MagicMock()
    ledger.total = fee.total
    ledger.collected = False

    hold_scalars = MagicMock()
    hold_scalars.all = MagicMock(return_value=[holding])
    batch_scalars = MagicMock()
    batch_scalars.all = MagicMock(return_value=[batch])
    fee_scalars = MagicMock()
    fee_scalars.all = MagicMock(return_value=[ledger])

    session = AsyncMock()
    session.scalars = AsyncMock(side_effect=[hold_scalars, batch_scalars, fee_scalars])
    principal = MagicMock(user_id=9)

    out = await get_investment_costs(4, session, principal)
    assert out.accrued_fees_total == fee.total
    assert out.collected is False
    assert out.returns_after_fees == out.snapshot.current_returns - fee.total
