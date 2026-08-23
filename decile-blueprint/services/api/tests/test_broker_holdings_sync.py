"""Tree-3 leaf 3.3 — holdings sync shaped like HoldingRow; no OrderGateway."""

from __future__ import annotations

import inspect
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from baskfy_execution.broker_ports import normalize_holding, total_quantity

from baskfy_api import broker_holdings
from baskfy_api.app import create_app
from baskfy_api.routers import brokers as brokers_router
from baskfy_api.routers.brokers import sync_holdings


@pytest.fixture(autouse=True)
def _dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("BASKFY_BROKER_HOLDINGS_FIXTURE", raising=False)


@pytest.fixture(scope="module")
def spec() -> dict[str, object]:
    return create_app().openapi()


class TestSyncHoldingsRoute:
    def test_sync_holdings_is_in_openapi(self, spec: dict[str, object]) -> None:
        paths = spec["paths"]
        assert isinstance(paths, dict)
        assert "/api/v1/brokers/{broker_id}/sync-holdings" in paths
        route = paths["/api/v1/brokers/{broker_id}/sync-holdings"]
        assert isinstance(route, dict)
        assert "post" in route

    async def test_empty_under_dry_run_without_fixture(self) -> None:
        principal = MagicMock()
        principal.require_user.return_value = 1
        out = await sync_holdings(principal, "zerodha")
        assert out.broker_id == "zerodha"
        assert out.holdings == []
        assert out.dry_run is True

    async def test_fixture_shape_matches_holding_row(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fixture = tmp_path / "holdings.json"
        fixture.write_text(
            json.dumps(
                [
                    {
                        "symbol": "infy",
                        "quantity": 10,
                        "t1_quantity": 2,
                        "collateral_quantity": 3,
                        "average_price": "1400.50",
                        "exchange": "NSE",
                    }
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("BASKFY_BROKER_HOLDINGS_FIXTURE", str(fixture))

        principal = MagicMock()
        principal.require_user.return_value = 1
        out = await sync_holdings(principal, "zerodha")
        assert len(out.holdings) == 1
        row = out.holdings[0]
        assert row.symbol == "INFY"
        assert row.quantity == Decimal("10")
        assert row.t1_quantity == Decimal("2")
        assert row.collateral_quantity == Decimal("3")
        # Desk non-negotiable #2: total = qty + t1 + collateral.
        assert row.total_quantity == Decimal("15")
        expected = normalize_holding(
            symbol="INFY",
            quantity=10,
            t1_quantity=2,
            collateral_quantity=3,
            average_price="1400.50",
        )
        assert total_quantity(expected) == row.total_quantity

    async def test_unknown_broker_404(self) -> None:
        from baskfy_api.problems import Problem

        principal = MagicMock()
        principal.require_user.return_value = 1
        with pytest.raises(Problem) as caught:
            await sync_holdings(principal, "not-a-broker")
        assert caught.value.status == 404


class TestSumQtyRuleDocumented:
    def test_total_quantity_field_documents_non_negotiable_two(self) -> None:
        source = inspect.getsource(brokers_router.HoldingOut)
        assert "quantity + t1_quantity + collateral_quantity" in source
        assert "non-negotiable" in source.lower() or "#2" in source


class TestNoOrderGateway:
    def test_holdings_path_never_places_an_order(self) -> None:
        for module in (brokers_router, broker_holdings):
            source = inspect.getsource(module)
            for forbidden in ("OrderGateway", "place_order", "confirm=true", "kc.place"):
                assert forbidden not in source, f"{module.__name__} names {forbidden}"
