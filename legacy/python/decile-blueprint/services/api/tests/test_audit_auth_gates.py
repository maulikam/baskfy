"""AUDIT 0.1 / 2.1 — desk, baskets and kite hand-off are authenticated."""

from __future__ import annotations

import httpx
import pytest
from api_helpers import assert_problem, url
from screener_helpers import requires_db

pytestmark = [pytest.mark.db, requires_db]


@pytest.mark.parametrize(
    "path",
    [
        "/desk/performance",
        "/desk/holdings",
        "/baskets",
        "/baskets/plan",
        "/baskets/plan/kite",
        "/explore/broad-market-sharpe/kite?amount=100000",
    ],
)
async def test_anonymous_desk_basket_kite_routes_are_401(api: httpx.AsyncClient, path: str) -> None:
    assert_problem(await api.get(url(path)), 401, "unauthenticated")
