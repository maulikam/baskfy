"""The suite-wide network block (Prompt 2 acceptance criterion 1).

    "Zero network calls in the test suite (assert this with a socket-blocking fixture)."

Kept in its own module rather than inside ``conftest.py`` so that tests can import and assert it:
pytest resolves ``conftest`` to the nearest one on the path, so a nested package's conftest would
shadow the root one and the import would silently bind to the wrong module.

Loopback is exempt. The `db`- and `redis`-marked tests talk to the PostgreSQL and Redis containers
`make up` starts on localhost; those are fixtures on the developer's own machine, not the network.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Final

import pytest

#: Hosts a test may still reach: the local containers `make up` provides.
ALLOWED_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0"})

#: What ``socket.connect`` accepts: a host/port pair, or a filesystem path for AF_UNIX.
SocketAddress = tuple[str, int] | tuple[str, int, int, int] | str | bytes


class NetworkAccessBlocked(RuntimeError):
    """Raised when a test tries to open a socket to anything but loopback."""


def is_loopback(address: object) -> bool:
    """True for AF_UNIX paths and for loopback host/port pairs."""
    if isinstance(address, (str, bytes)):
        # A filesystem socket path. Local by construction.
        return True
    if isinstance(address, tuple) and address:
        host = address[0]
        return isinstance(host, str) and host in ALLOWED_HOSTS
    return False


def _refuse(address: object, where: str) -> NetworkAccessBlocked:
    return NetworkAccessBlocked(
        f"{where} tried to reach {address!r}. The test suite makes no network calls "
        "(PROMPTS.md Prompt 2, acceptance criterion 1) — use FixtureProvider, or inject a fake "
        "client through the provider's Runtime object."
    )


@contextmanager
def blocked_network() -> Iterator[None]:
    """Refuse every non-loopback socket connection inside this context."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self: socket.socket, address: SocketAddress) -> None:
        if not is_loopback(address):
            raise _refuse(address, "socket.connect")
        real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: SocketAddress) -> int:
        if not is_loopback(address):
            raise _refuse(address, "socket.connect_ex")
        return real_connect_ex(self, address)

    def guarded_create_connection(
        address: tuple[str | None, int],
        timeout: float | None = None,
        source_address: tuple[str, int] | None = None,
        *,
        all_errors: bool = False,
    ) -> socket.socket:
        if not is_loopback(address):
            raise _refuse(address, "socket.create_connection")
        # Forward every argument. Dropping `timeout` would silently make a connection
        # non-blocking-forever, which is a far more confusing failure than a refusal.
        if timeout is None:
            return real_create_connection(
                address, source_address=source_address, all_errors=all_errors
            )
        return real_create_connection(address, timeout, source_address, all_errors=all_errors)

    # Signature mirrors socket.getaddrinfo exactly, including its positional arity and its
    # builtin-shadowing `type` parameter; a guard that changed the shape would break callers.
    def guarded_getaddrinfo(  # noqa: PLR0913, PLR0917
        host: str | bytes | None,
        port: str | bytes | int | None,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> Sequence[object]:
        if not is_loopback((host, port)):
            raise _refuse((host, port), "socket.getaddrinfo")
        # `family`/`type`/`proto`/`flags` must be forwarded: asyncpg asks for SOCK_STREAM
        # specifically, and answering with every socket type makes it dial the wrong one.
        return real_getaddrinfo(host, port, family, type, proto, flags)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, "connect", guarded_connect)
        patch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
        patch.setattr(socket, "create_connection", guarded_create_connection)
        patch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
        yield
