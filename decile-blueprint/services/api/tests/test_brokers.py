"""API surface for the broker catalog — M41."""

from __future__ import annotations

import pytest

from baskfy_api.app import create_app
from baskfy_api.routers.brokers import ConnectOut, _broker_out, _gate_out
from baskfy_core.broker_connections import BROKER_OAUTH_REVIEW, broker_catalog


@pytest.fixture(scope="module")
def spec() -> dict[str, object]:
    return create_app().openapi()


class TestBrokerRoutesAreRegistered:
    def test_catalog_and_connect_are_in_openapi(self, spec: dict[str, object]) -> None:
        paths = spec["paths"]
        assert isinstance(paths, dict)
        assert "/api/v1/brokers" in paths
        assert "/api/v1/brokers/{broker_id}" in paths
        assert "/api/v1/brokers/{broker_id}/connect" in paths
        brokers = paths["/api/v1/brokers"]
        assert isinstance(brokers, dict)
        assert "get" in brokers
        connect = paths["/api/v1/brokers/{broker_id}/connect"]
        assert isinstance(connect, dict)
        assert "post" in connect


class TestGateAndCatalog:
    def test_gate_is_closed(self) -> None:
        assert BROKER_OAUTH_REVIEW.signed_off is False
        gate = _gate_out()
        assert gate.live_oauth_enabled is False
        assert "D3" in gate.requirement

    def test_catalog_is_ten_and_leads_with_the_named_four(self) -> None:
        brokers = [_broker_out(b) for b in broker_catalog()]
        assert len(brokers) == 10
        assert [b.id for b in brokers[:4]] == ["zerodha", "hdfc", "kotak", "icici"]
        assert all(b.connected is False for b in brokers)
        assert sum(1 for b in brokers if b.adapter_wired) == 1

    def test_gated_connect_never_carries_a_redirect(self) -> None:
        assert BROKER_OAUTH_REVIEW.blocks_live_oauth is True
        out = ConnectOut(broker_id="zerodha", oauth_available=False, reason="gated")
        assert out.redirect_url is None
