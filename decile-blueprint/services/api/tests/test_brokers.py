"""API surface for the broker catalog — M41."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from baskfy_api.app import create_app
from baskfy_api.broker_oauth import SIMULATED_TOKEN_PREFIX
from baskfy_api.routers.brokers import ConnectOut, _broker_out, _gate_out
from baskfy_core.broker_connections import BROKER_OAUTH_REVIEW, broker_catalog
from baskfy_providers.tokens import AccessTokenStore


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
    def test_gate_is_open_after_d3(self) -> None:
        assert BROKER_OAUTH_REVIEW.signed_off is True
        gate = _gate_out()
        assert gate.live_oauth_enabled is True
        assert "D3" in gate.decision_reference or "Posture B" in gate.requirement

    def test_catalog_is_ten_and_leads_with_the_named_four(self) -> None:
        brokers = [_broker_out(b) for b in broker_catalog()]
        assert len(brokers) == 10
        assert [b.id for b in brokers[:4]] == ["zerodha", "hdfc", "kotak", "icici"]
        assert all(b.connected is False for b in brokers)
        assert sum(1 for b in brokers if b.adapter_wired) >= 2

    def test_connect_out_can_carry_a_redirect_when_available(self) -> None:
        assert BROKER_OAUTH_REVIEW.blocks_live_oauth is False
        out = ConnectOut(
            broker_id="zerodha",
            oauth_available=True,
            redirect_url="https://kite.zerodha.com/connect/login?v=3",
            state="test-state",
            reason="",
        )
        assert out.redirect_url is not None
        assert out.state == "test-state"


class TestTheCatalogReadsTheSessionBack:
    """M75. `_broker_out` hardcoded `connected=False`, so a finished login was invisible.

    Maulik connected Zerodha on 1 Sep 2026 — Kite came back, the token was written, `/user/profile`
    answered 200 for the account — and the page still offered "Connect Zerodha", because nothing
    in the catalog ever looked at the stored session.
    """

    def test_a_stored_token_makes_the_wired_broker_connected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = Fernet.generate_key().decode()
        blob = tmp_path / "broker-token.enc"
        monkeypatch.setenv("BASKFY_BROKER_TOKEN_PATH", str(blob))
        monkeypatch.setenv("BASKFY_KITE_TOKEN_ENCRYPTION_KEY", key)
        AccessTokenStore(blob, key).save("real-token-from-kite")

        zerodha = _broker_out(next(b for b in broker_catalog() if b.id == "zerodha"))
        assert zerodha.connected is True
        assert zerodha.connection_status == "connected"

    def test_no_token_is_not_connected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BASKFY_BROKER_TOKEN_PATH", str(tmp_path / "absent.enc"))
        monkeypatch.setenv("BASKFY_KITE_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
        zerodha = _broker_out(next(b for b in broker_catalog() if b.id == "zerodha"))
        assert zerodha.connected is False
        assert zerodha.connection_status == "not_connected"

    def test_a_simulated_token_is_never_reported_as_connected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The `sim_` guard, at the one place a human reads the answer.

        DRY_RUN mints `sim_` tokens. Showing "connected" for one would tell Maulik a broker is on
        the line when the stub is — the same confusion
        `TestSimulatedTokenNeverPoisonsTheRealSession` exists to prevent, one layer further out.
        """
        key = Fernet.generate_key().decode()
        blob = tmp_path / "broker-token.enc"
        monkeypatch.setenv("BASKFY_BROKER_TOKEN_PATH", str(blob))
        monkeypatch.setenv("BASKFY_KITE_TOKEN_ENCRYPTION_KEY", key)
        AccessTokenStore(blob, key).save(f"{SIMULATED_TOKEN_PREFIX}whatever")

        zerodha = _broker_out(next(b for b in broker_catalog() if b.id == "zerodha"))
        assert zerodha.connected is False
        assert zerodha.connection_status == "simulated"

    def test_no_other_broker_is_lit_up_by_the_kite_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One store, ten brokers. This caught the first version of the fix.

        Gating on `_WIRED_AUTHORIZE` — the five brokers with an authorize URL — reported Upstox as
        connected off Zerodha's token. The blob is Kite's alone, so `_OAUTH_COMPLETABLE` is the set
        that matches what is actually stored.
        """
        key = Fernet.generate_key().decode()
        blob = tmp_path / "broker-token.enc"
        monkeypatch.setenv("BASKFY_BROKER_TOKEN_PATH", str(blob))
        monkeypatch.setenv("BASKFY_KITE_TOKEN_ENCRYPTION_KEY", key)
        AccessTokenStore(blob, key).save("real-token-from-kite")

        for broker in broker_catalog():
            if broker.id == "zerodha":
                continue
            assert _broker_out(broker).connected is False, broker.id
