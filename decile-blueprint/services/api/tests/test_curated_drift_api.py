"""T8.6 drift scan/fix — rebase ledger; no order path."""

from __future__ import annotations

import inspect
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from baskfy_api.app import create_app
from baskfy_api.routers import curated_drift
from baskfy_api.routers.curated_drift import (
    BrokerHoldingIn,
    DriftScanBody,
    fix_investment_drift,
    scan_investment_drift,
)
from baskfy_core.curated_drift import BrokerHolding, LedgerHolding, detect_drift
from baskfy_core.models import CbPendingAction


def test_openapi_exposes_drift() -> None:
    paths = create_app().openapi()["paths"]
    assert "post" in paths["/api/v1/cb/investments/{investment_id}/drift/scan"]
    assert "post" in paths["/api/v1/cb/investments/{investment_id}/drift/fix"]


def test_drift_router_has_no_broker_path() -> None:
    source = inspect.getsource(curated_drift)
    for forbidden in ("OrderGateway", "place_order", "kc.place_order", "/execute", "kiteconnect"):
        assert forbidden not in source


@pytest.mark.asyncio
async def test_scan_raises_drift_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    inv = MagicMock()
    inv.id = 5
    inv.user_id = 9

    async def _load(*_args: object, **_kwargs: object) -> MagicMock:
        return inv

    monkeypatch.setattr(curated_drift, "load_investment_for_user", _load)

    ledger = [LedgerHolding(instrument_id=1, qty=Decimal("10"), avg_price=Decimal("80"))]
    broker = [BrokerHolding(instrument_id=1, qty=Decimal("4"))]
    assert detect_drift(ledger, broker).action_type == "DRIFT"

    async def _resolve(
        _session: object, _body: object
    ) -> tuple[list[BrokerHolding], dict[int, str]]:
        return broker, {1: "CUPID"}

    async def _ledger_rows(_session: object, _id: int) -> tuple:
        return ledger, {1: Decimal("80")}, {1: "CUPID"}

    monkeypatch.setattr(curated_drift, "_resolve_broker", _resolve)
    monkeypatch.setattr(curated_drift, "_ledger", _ledger_rows)

    added: list[object] = []
    session = AsyncMock()
    pending_scalars = MagicMock()
    pending_scalars.all = MagicMock(return_value=[])
    session.scalars = AsyncMock(return_value=pending_scalars)
    session.add = added.append

    async def _flush() -> None:
        for obj in added:
            if isinstance(obj, CbPendingAction) and getattr(obj, "id", None) is None:
                obj.id = 44

    session.flush = _flush
    principal = MagicMock(user_id=9)
    body = DriftScanBody(broker_holdings=[BrokerHoldingIn(symbol="CUPID", qty=Decimal("4"))])
    out = await scan_investment_drift(5, body, session, principal)
    assert out.action_type == "DRIFT"
    assert out.pending_action_id == 44
    assert out.deltas[0].shortfall == Decimal("6")
    assert len(added) == 1


@pytest.mark.asyncio
async def test_fix_rebases_qty(monkeypatch: pytest.MonkeyPatch) -> None:
    inv = MagicMock()
    inv.id = 5
    inv.user_id = 9

    async def _load(*_args: object, **_kwargs: object) -> MagicMock:
        return inv

    monkeypatch.setattr(curated_drift, "load_investment_for_user", _load)

    ledger = [LedgerHolding(instrument_id=1, qty=Decimal("10"), avg_price=Decimal("80"))]
    broker = [BrokerHolding(instrument_id=1, qty=Decimal("4"))]

    async def _resolve(
        _session: object, _body: object
    ) -> tuple[list[BrokerHolding], dict[int, str]]:
        return broker, {1: "CUPID"}

    async def _ledger_rows(_session: object, _id: int) -> tuple:
        return ledger, {1: Decimal("80")}, {1: "CUPID"}

    monkeypatch.setattr(curated_drift, "_resolve_broker", _resolve)
    monkeypatch.setattr(curated_drift, "_ledger", _ledger_rows)

    holding_row = MagicMock()
    holding_row.instrument_id = 1
    holding_row.qty = Decimal("10")
    hold_scalars = MagicMock()
    hold_scalars.all = MagicMock(return_value=[holding_row])
    action_scalars = MagicMock()
    action_scalars.all = MagicMock(return_value=[])

    session = AsyncMock()
    session.scalars = AsyncMock(side_effect=[hold_scalars, action_scalars])
    session.delete = AsyncMock()
    session.flush = AsyncMock()
    principal = MagicMock(user_id=9)
    body = DriftScanBody(broker_holdings=[BrokerHoldingIn(symbol="CUPID", qty=Decimal("4"))])
    out = await fix_investment_drift(5, body, session, principal)
    assert holding_row.qty == Decimal("4")
    assert out.synthetic_exit_count == 1
    assert out.holdings[0].qty == Decimal("4")
