"""Audit 4.14 — allocation locks holdings rows before mutating quantities."""

from __future__ import annotations

import inspect

from baskfy_api.routers import portfolio_overview as mod


def test_apply_allocation_locks_holdings_with_for_update() -> None:
    source = inspect.getsource(mod._apply_allocation)
    assert ".with_for_update()" in source
    assert "PortfolioHolding" in source
