"""Leaf 4.7 — CUSTOMIZE plan preview; never executes (PACK.2)."""

from __future__ import annotations

import datetime as dt
import inspect
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from baskfy_api.app import create_app
from baskfy_api.routers import curated_customize, curated_plans
from baskfy_api.routers.curated_customize import (
    CustomizeBody,
    CustomizePlanOut,
    customize_investment,
)
from baskfy_api.routers.curated_plans import ClosedMarketOut
from baskfy_core.market_hours_cb import IST

TRADING = {dt.date(2026, 8, 24), dt.date(2026, 8, 25), dt.date(2026, 8, 26)}


def _ist(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 8, 24, hour, minute, tzinfo=IST)


def _d(value: str) -> Decimal:
    return Decimal(value)


@pytest.fixture
def open_session(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    monkeypatch.setattr(curated_customize, "_now", lambda: _ist(11, 0))
    monkeypatch.setattr(curated_plans, "_now", lambda: _ist(11, 0))

    async def _dates(_session: object, *, around: dt.date) -> set[dt.date]:
        del around
        return TRADING

    monkeypatch.setattr(curated_plans, "load_trading_dates", _dates)
    return AsyncMock()


@pytest.fixture
def closed_session(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    monkeypatch.setattr(curated_customize, "_now", lambda: _ist(8, 0))
    monkeypatch.setattr(curated_plans, "_now", lambda: _ist(8, 0))

    async def _dates(_session: object, *, around: dt.date) -> set[dt.date]:
        del around
        return TRADING

    monkeypatch.setattr(curated_plans, "load_trading_dates", _dates)
    return AsyncMock()


@pytest.mark.asyncio
async def test_customize_when_open_returns_customize_kind(open_session: AsyncMock) -> None:
    body = CustomizeBody(
        holdings={"AAA": 10, "BBB": 0},
        target_weights={"AAA": _d("0.5"), "BBB": _d("0.5")},
        prices={"AAA": _d("100"), "BBB": _d("100")},
        amount=_d("1000"),
    )
    result = await customize_investment(42, body, open_session)
    assert isinstance(result, CustomizePlanOut)
    assert result.kind == "CUSTOMIZE"
    assert result.status == "PLANNED"
    assert result.investment_id == 42
    assert result.desk_plan_id.startswith("cb-sim-")
    assert result.legs
    assert any(leg.side == "BUY" for leg in result.legs)


@pytest.mark.asyncio
async def test_customize_outside_hours_returns_closed(closed_session: AsyncMock) -> None:
    body = CustomizeBody(
        holdings={"AAA": 1},
        target_weights={"AAA": _d("1")},
        prices={"AAA": _d("10")},
        amount=_d("100"),
    )
    result = await customize_investment(1, body, closed_session)
    assert isinstance(result, ClosedMarketOut)
    assert result.market_open is False
    assert not hasattr(result, "desk_plan_id") or not getattr(result, "desk_plan_id", None)


def test_customize_router_source_names_no_execution() -> None:
    source = inspect.getsource(curated_customize)
    for forbidden in ("OrderGateway", "place_order", "place_gtt", "kc.place", "confirm=true"):
        assert forbidden not in source, forbidden


def test_customize_route_mounted_and_not_execute() -> None:
    spec = create_app().openapi()
    path = "/api/v1/cb/investments/{investment_id}/customize"
    assert path in spec["paths"]
    assert "post" in spec["paths"][path]
    for p in spec["paths"]:
        if "customize" in p:
            assert "execute" not in p.lower()
