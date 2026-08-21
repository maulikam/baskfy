"""Shared fixtures for the provider tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
import redis
from cryptography.fernet import Fernet

from baskfy_providers.archive import LocalRawArchive
from baskfy_providers.fixtures import FixtureProvider
from baskfy_providers.ratelimit import RedisLike
from baskfy_providers.settings import ProviderSettings

REDIS_ENV_VAR: Final = "BASKFY_REDIS_URL"
DEFAULT_REDIS_URL: Final = "redis://localhost:6380/0"


def redis_url() -> str:
    return os.environ.get(REDIS_ENV_VAR, DEFAULT_REDIS_URL)


@pytest.fixture
def settings(tmp_path: Path) -> ProviderSettings:
    """Settings with nothing configured except paths, so tests start from "unavailable"."""
    return ProviderSettings(
        _env_file=None,
        kite_token_path=str(tmp_path / "kite-token.enc"),
        redis_url=redis_url(),
    )


@pytest.fixture
def configured_settings(tmp_path: Path) -> ProviderSettings:
    """Settings with Kite credentials and an encryption key present."""
    return ProviderSettings(
        _env_file=None,
        kite_api_key="test-key",
        kite_api_secret="test-secret",
        kite_token_encryption_key=Fernet.generate_key().decode(),
        kite_token_path=str(tmp_path / "kite-token.enc"),
        redis_url=redis_url(),
    )


@pytest.fixture
def archive(tmp_path: Path) -> LocalRawArchive:
    return LocalRawArchive(tmp_path / "archive")


@pytest.fixture(scope="session")
def fixture_provider() -> FixtureProvider:
    return FixtureProvider()


@pytest.fixture
def redis_client() -> Iterator[RedisLike]:
    """A live Redis, or skip. `make up` provides one on localhost.

    The token bucket's whole purpose is atomicity across processes, and that property lives in
    Redis's execution of the Lua script. A hand-written fake would only prove the fake is
    consistent with itself, so these tests use the real thing (on loopback, which the suite's
    network block permits) and skip when it is absent.
    """
    client = redis.Redis.from_url(redis_url())
    try:
        client.ping()
    except Exception:
        pytest.skip(f"no Redis at {redis_url()}; run `make up`")
    limiter_client: RedisLike = client
    yield limiter_client
    client.close()
