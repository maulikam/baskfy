"""AF 3.11 — KiteConnect is constructed with an explicit timeout."""

from __future__ import annotations

import pytest

from baskfy_providers.kite import _default_client_factory
from baskfy_providers.settings import ProviderSettings


def test_kite_client_factory_passes_an_explicit_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeKite:
        def __init__(self, api_key: str, timeout: float | None = None) -> None:
            captured["api_key"] = api_key
            captured["timeout"] = timeout

    monkeypatch.setattr("baskfy_providers.kite.KiteConnect", FakeKite)
    settings = ProviderSettings(_env_file=None, kite_request_timeout_seconds=42.0)
    _default_client_factory("key", timeout=settings.kite_request_timeout_seconds)
    assert captured["timeout"] == 42.0
