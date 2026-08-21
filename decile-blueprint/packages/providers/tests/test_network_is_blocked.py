"""Prompt 2 acceptance criterion 1: zero network calls in the test suite.

The block itself lives in the repo-root ``conftest.py`` and is autouse for the whole session.
These tests prove it is actually armed — a socket-blocking fixture that silently stopped working
would leave the suite quietly making real calls, which is the exact failure it exists to prevent.
"""

from __future__ import annotations

import socket

import pytest
from network_guard import NetworkAccessBlocked, is_loopback

from baskfy_providers.errors import ProviderUnavailable
from baskfy_providers.fixtures import FixtureProvider
from baskfy_providers.nse import NSEProvider
from baskfy_providers.settings import ProviderSettings


class TestTheBlockIsArmed:
    def test_connecting_to_a_remote_host_is_refused(self) -> None:
        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
            pytest.raises(NetworkAccessBlocked),
        ):
            sock.connect(("www.nseindia.com", 443))

    def test_create_connection_is_refused(self) -> None:
        with pytest.raises(NetworkAccessBlocked):
            socket.create_connection(("api.kite.trade", 443))

    def test_dns_resolution_is_refused(self) -> None:
        """Blocking connect alone is not enough — a resolver call is already a network call."""
        with pytest.raises(NetworkAccessBlocked):
            socket.getaddrinfo("nsearchives.nseindia.com", 443)

    def test_the_refusal_names_the_address(self) -> None:
        """An operator debugging a failure needs to know which call escaped."""
        with pytest.raises(NetworkAccessBlocked, match="nseindia"):
            socket.getaddrinfo("www.nseindia.com", 443)


class TestLoopbackRemainsUsable:
    """The `db`- and `redis`-marked tests need the containers `make up` starts."""

    def test_loopback_ipv4_is_allowed(self) -> None:
        assert is_loopback(("127.0.0.1", 5433))

    def test_localhost_is_allowed(self) -> None:
        assert is_loopback(("localhost", 6380))

    def test_unix_socket_paths_are_allowed(self) -> None:
        assert is_loopback("/tmp/postgres.sock")

    def test_a_public_address_is_not_loopback(self) -> None:
        assert not is_loopback(("13.107.42.14", 443))

    def test_resolver_arguments_are_forwarded_for_permitted_hosts(self) -> None:
        """The guard must not quietly change what a permitted call means.

        asyncpg asks ``getaddrinfo`` for SOCK_STREAM specifically. A guard that dropped the
        socket type would answer with UDP and raw entries too, and the database connection would
        fail with a bare "connection refused" that looks nothing like a blocked socket.
        """
        results = socket.getaddrinfo("127.0.0.1", 5433, socket.AF_INET, socket.SOCK_STREAM)
        assert results
        assert {entry[1] for entry in results} == {socket.SOCK_STREAM}
        assert {entry[0] for entry in results} == {socket.AF_INET}


class TestProvidersDoNotReachTheNetwork:
    """The adapters that tests exercise must be reachable without any egress at all."""

    def test_fixture_provider_serves_with_the_block_armed(self) -> None:
        provider = FixtureProvider()
        assert provider.check().available
        assert provider.list_instruments()

    def test_an_unwired_nse_provider_never_dials_out(self) -> None:
        """docs/09 requires archive-before-parse; with no client wired there is nothing to dial."""
        provider = NSEProvider(ProviderSettings(_env_file=None))
        with pytest.raises(ProviderUnavailable):
            provider.listings()
