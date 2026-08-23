"""SC3 plan generation API — invest / apply / exit; never executes (PACK.2)."""

from __future__ import annotations

import datetime as dt
import inspect
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from baskfy_api.app import create_app
from baskfy_api.routers import curated_plans
from baskfy_api.routers.curated_plans import (
    ApplyBody,
    ClosedMarketOut,
    ExitBody,
    InvestBody,
    PlanPreviewOut,
    plan_apply,
    plan_exit,
    plan_invest,
)
from baskfy_core.market_hours_cb import IST

TRADING = {dt.date(2026, 8, 24), dt.date(2026, 8, 25), dt.date(2026, 8, 26)}


def _ist(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 8, 24, hour, minute, tzinfo=IST)


def _d(value: str) -> Decimal:
    return Decimal(value)


@pytest.fixture
def open_session(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    monkeypatch.setattr(curated_plans, "_now", lambda: _ist(11, 0))

    async def _dates(_session: object, *, around: dt.date) -> set[dt.date]:
        del around
        return TRADING

    monkeypatch.setattr(curated_plans, "load_trading_dates", _dates)
    return AsyncMock()


@pytest.fixture
def closed_session(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    monkeypatch.setattr(curated_plans, "_now", lambda: _ist(8, 0))

    async def _dates(_session: object, *, around: dt.date) -> set[dt.date]:
        del around
        return TRADING

    monkeypatch.setattr(curated_plans, "load_trading_dates", _dates)
    return AsyncMock()


@pytest.mark.asyncio
async def test_invest_outside_hours_returns_closed_market_not_a_plan(
    closed_session: AsyncMock,
) -> None:
    body = InvestBody(
        target_weights={"AAA": _d("1")},
        prices={"AAA": _d("100")},
        amount=_d("1000"),
    )
    result = await plan_invest(body, closed_session)
    assert isinstance(result, ClosedMarketOut)
    assert result.market_open is False
    assert "09:15" in result.next_open_ist
    assert not hasattr(result, "desk_plan_id") or not getattr(result, "desk_plan_id", None)


@pytest.mark.asyncio
async def test_invest_when_open_returns_planned_preview(open_session: AsyncMock) -> None:
    body = InvestBody(
        target_weights={"AAA": _d("0.5"), "BBB": _d("0.5")},
        prices={"AAA": _d("100"), "BBB": _d("200")},
        amount=_d("400"),
    )
    result = await plan_invest(body, open_session)
    assert isinstance(result, PlanPreviewOut)
    assert result.market_open is True
    assert result.status == "PLANNED"
    assert result.kind == "BUY"
    assert result.desk_plan_id.startswith("cb-sim-")
    assert len(result.legs) == 2


@pytest.mark.asyncio
async def test_apply_and_exit_when_open(open_session: AsyncMock) -> None:
    apply = await plan_apply(
        ApplyBody(
            holdings={"AAA": 10},
            target_weights={"AAA": _d("0.5"), "BBB": _d("0.5")},
            prices={"AAA": _d("100"), "BBB": _d("100")},
            amount=_d("1000"),
        ),
        open_session,
    )
    assert isinstance(apply, PlanPreviewOut)
    assert apply.kind == "REBALANCE"
    assert apply.status == "PLANNED"
    assert apply.desk_plan_id.startswith("cb-sim-")

    exit_plan = await plan_exit(
        ExitBody(
            holdings={"AAA": 3, "BBB": 1},
            prices={"AAA": _d("50"), "BBB": _d("40")},
        ),
        open_session,
    )
    assert isinstance(exit_plan, PlanPreviewOut)
    assert exit_plan.kind == "EXIT"
    assert all(leg.side == "SELL" for leg in exit_plan.legs)


@pytest.mark.asyncio
async def test_apply_outside_hours_never_invents_plan(closed_session: AsyncMock) -> None:
    result = await plan_apply(
        ApplyBody(
            holdings={"AAA": 1},
            target_weights={"AAA": _d("1")},
            prices={"AAA": _d("10")},
            amount=_d("100"),
        ),
        closed_session,
    )
    assert isinstance(result, ClosedMarketOut)
    assert result.market_open is False


def test_curated_plans_router_source_names_no_execution() -> None:
    source = inspect.getsource(curated_plans)
    for forbidden in ("OrderGateway", "place_order", "place_gtt", "kc.place", "confirm=true"):
        assert forbidden not in source, forbidden


def test_plan_routes_are_mounted_and_not_execute() -> None:
    spec = create_app().openapi()
    paths = {p for p in spec["paths"] if "/cb/plans/" in p}
    assert "/api/v1/cb/plans/invest" in paths
    assert "/api/v1/cb/plans/apply" in paths
    assert "/api/v1/cb/plans/exit" in paths
    for path in paths:
        assert "execute" not in path.lower()
        assert "place_order" not in path.lower()
        methods = set(spec["paths"][path])
        assert "post" in methods
