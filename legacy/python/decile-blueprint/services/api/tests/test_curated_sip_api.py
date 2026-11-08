"""T8.4 SIP reminder writer — REMINDER only; AUTO refused."""

from __future__ import annotations

import datetime as dt
import inspect
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from baskfy_api.app import create_app
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.routers import curated_sip
from baskfy_api.routers.curated_sip import SipBody, persist_sip_plan
from baskfy_core.curated_sip import SIP_MODE_REMINDER, SIP_STATUS_ACTIVE
from baskfy_core.models import CbSipPlan


def test_openapi_exposes_sip() -> None:
    paths = create_app().openapi()["paths"]
    key = "/api/v1/cb/investments/{investment_id}/sip"
    assert "post" in paths[key]
    assert "get" in paths[key]


def test_sip_router_has_no_broker_path() -> None:
    source = inspect.getsource(curated_sip)
    for forbidden in ("OrderGateway", "place_order", "kc.place_order", "/execute", "kiteconnect"):
        assert forbidden not in source


@pytest.mark.asyncio
async def test_create_refuses_auto() -> None:
    session = AsyncMock()
    inv = MagicMock()
    inv.id = 1
    body = SipBody(amount=Decimal("5000"), day_of_month=21, mode="AUTO")
    with pytest.raises(Problem) as err:
        await persist_sip_plan(
            session,
            inv,
            body,
            as_of=dt.date(2026, 8, 24),
            trading_dates={dt.date(2026, 8, 24)},
        )
    assert err.value.type is ProblemType.INVALID_SCREEN_DEFINITION


@pytest.mark.asyncio
async def test_create_persists_reminder() -> None:
    inv = MagicMock()
    inv.id = 3
    added: list[object] = []
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    session.add = added.append

    async def _flush() -> None:
        for obj in added:
            if isinstance(obj, CbSipPlan) and getattr(obj, "id", None) is None:
                obj.id = 99

    session.flush = _flush
    body = SipBody(amount=Decimal("5000"), day_of_month=21, mode=SIP_MODE_REMINDER)
    as_of = dt.date(2026, 8, 24)
    trading = {as_of, dt.date(2026, 9, 21)}
    out = await persist_sip_plan(session, inv, body, as_of=as_of, trading_dates=trading)
    assert out.id == 99
    assert out.mode == SIP_MODE_REMINDER
    assert out.status == SIP_STATUS_ACTIVE
    assert out.next_fire_date == dt.date(2026, 8, 24)
    assert len(added) == 1
    plan = added[0]
    assert isinstance(plan, CbSipPlan)
    assert plan.mode == SIP_MODE_REMINDER
