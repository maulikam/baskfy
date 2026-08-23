"""Tree-3 leaf 3.2 — OAuth callback + encrypted token helpers."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

from baskfy_api import broker_oauth
from baskfy_api.app import create_app
from baskfy_api.broker_oauth import (
    clear_oauth_states,
    consume_oauth_state,
    exchange_request_token_stub,
    register_oauth_state,
    token_store_for,
)
from baskfy_api.problems import Problem
from baskfy_api.routers import brokers as brokers_router
from baskfy_api.routers.brokers import oauth_callback
from baskfy_core.broker_connections import BROKER_OAUTH_REVIEW


@pytest.fixture(autouse=True)
def _clean_oauth_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_oauth_states()
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("BASKFY_KITE_API_SECRET", raising=False)
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("BASKFY_KITE_TOKEN_ENCRYPTION_KEY", key)
    monkeypatch.setenv("BASKFY_BROKER_TOKEN_PATH", str(tmp_path / "broker-token.enc"))
    monkeypatch.setenv("BASKFY_KITE_API_KEY", "test-api-key")
    yield
    clear_oauth_states()


@pytest.fixture(scope="module")
def spec() -> dict[str, object]:
    return create_app().openapi()


class TestGateOpenAndCallbackRoute:
    def test_gate_is_open(self) -> None:
        assert BROKER_OAUTH_REVIEW.signed_off is True
        assert BROKER_OAUTH_REVIEW.blocks_live_oauth is False

    def test_callback_is_in_openapi(self, spec: dict[str, object]) -> None:
        paths = spec["paths"]
        assert isinstance(paths, dict)
        assert "/api/v1/brokers/callback" in paths
        callback = paths["/api/v1/brokers/callback"]
        assert isinstance(callback, dict)
        assert "get" in callback


class TestOauthStateAndStub:
    def test_stub_is_deterministic(self) -> None:
        a = exchange_request_token_stub(api_key="k", request_token="rt-one", user_id=7)
        b = exchange_request_token_stub(api_key="k", request_token="rt-one", user_id=7)
        assert a == b
        assert a.startswith("sim_")

    def test_bad_state_is_rejected_by_helper(self) -> None:
        assert consume_oauth_state("never-issued-state-value") is None

    async def test_happy_path_stores_encrypted_bytes(self, tmp_path: Path) -> None:
        register_oauth_state(state="good-state-token-abc12345", user_id=42, broker_id="zerodha")
        principal = MagicMock()
        principal.require_user.return_value = 42

        result = await oauth_callback(
            principal,
            request_token="request-token-xyz789",
            state="good-state-token-abc12345",
        )
        assert result.connected is True
        assert result.token_stored is True
        assert result.simulated is True
        assert result.broker_id == "zerodha"

        path = tmp_path / "broker-token.enc"
        assert path.is_file()
        raw = path.read_bytes()
        assert raw
        assert b"sim_" not in raw

        loaded = token_store_for().load()
        assert loaded.value.startswith("sim_")
        assert consume_oauth_state("good-state-token-abc12345") is None

    async def test_callback_rejects_bad_state(self) -> None:
        principal = MagicMock()
        principal.require_user.return_value = 1
        with pytest.raises(Problem) as caught:
            await oauth_callback(
                principal,
                request_token="request-token-xyz789",
                state="bogus-state-xxxxxxxx",
            )
        assert caught.value.status == 400


class TestNoPlaceOrder:
    def test_oauth_modules_never_place_an_order(self) -> None:
        for module in (brokers_router, broker_oauth):
            source = inspect.getsource(module)
            for forbidden in ("place_order", "OrderGateway", "confirm=true", "kc.place"):
                assert forbidden not in source, f"{module.__name__} names {forbidden}"
