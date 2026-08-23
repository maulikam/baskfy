"""SC11 / leaf-1.8.1 — explore & watchlist never grow an order path."""

from __future__ import annotations

import inspect

import pytest

from baskfy_api.app import create_app
from baskfy_api.routers import explore

MUTATING = ("post", "put", "patch", "delete")
ORDER_SHAPED = ("execute", "place_order", "/order", "gtt", "trade/place")

#: What `app.openapi()` returns, to the depth these tests read it — the same alias
#: `test_baskets_readonly.py` uses, so the two order-gate tests read the spec identically.
OpenApiSpec = dict[str, dict[str, dict[str, object]]]


@pytest.fixture(scope="module")
def spec() -> OpenApiSpec:
    return create_app().openapi()


def test_explore_and_watchlist_have_no_order_shaped_routes(spec: OpenApiSpec) -> None:
    offenders: list[str] = []
    for path, ops in spec["paths"].items():
        if "explore" not in path and "watchlist" not in path:
            continue
        for method in ops:
            if method not in MUTATING:
                continue
            lower = path.lower()
            if any(w in lower for w in ORDER_SHAPED):
                offenders.append(f"{method.upper()} {path}")
    assert offenders == [], offenders


def test_explore_router_source_names_no_execution() -> None:
    source = inspect.getsource(explore)
    for forbidden in ("OrderGateway", "place_order", "place_gtt", "kc.place", "confirm=true"):
        assert forbidden not in source, forbidden
