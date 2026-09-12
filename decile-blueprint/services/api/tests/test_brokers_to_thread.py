"""AUDIT 4.11 — broker Kite I/O runs in a worker thread, not on the event loop."""

from __future__ import annotations

import inspect

from baskfy_api.routers import brokers


def test_broker_kite_calls_use_anyio_to_thread() -> None:
    source = inspect.getsource(brokers)
    assert "anyio.to_thread.run_sync" in source
    # The three audit-cited call sites must not invoke the blocking helpers bare.
    assert "exchange = exchange_request_token(" not in source
    assert "result = holdings_for_broker(" not in source
