"""Repo-wide test configuration.

The network block lives here rather than in a package's own ``tests/`` directory because Prompt
2's first acceptance criterion is about the *suite*, not one module: "Zero network calls in the
test suite (assert this with a socket-blocking fixture)."

The implementation is in ``network_guard`` so tests can import and assert it; see that module.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from network_guard import blocked_network


@pytest.fixture(autouse=True, scope="session")
def block_network() -> Iterator[None]:
    """Refuse every non-loopback socket connection for the whole session."""
    with blocked_network():
        yield
