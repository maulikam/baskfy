"""``GET /cb/trending`` at the boundary — SC9's ranked lists.

Asserted over HTTP rather than by awaiting the handler, for the reason
``test_explore_http.py`` gives at length: a coroutine call cannot see the dependency graph, so
it cannot see whether a route requires authentication, and it never takes an error path.

The claims here are the ones the pure layer cannot make on its own:

- the route exists, is a GET, and refuses an anonymous caller;
- the aggregates are counted per basket, not multiplied by a join;
- a PRIVATE basket cannot appear in a ranking any more than in the catalog;
- nothing on this surface reaches an order path.
"""

from __future__ import annotations

import datetime as dt
import inspect
from decimal import Decimal

import httpx
import pytest
from api_helpers import bearer, make_user, url
from fastapi.routing import APIRoute
from screener_helpers import requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.app import create_app
from baskfy_api.auth import require_authenticated
from baskfy_api.curated_seed import seed_curated_managers
from baskfy_api.routers import curated_trending
from baskfy_core.curated_baskets import MANAGER_SLUG_BASKFY_ENGINE, SOLE_USER_ENV
from baskfy_core.curated_trending import MIN_ENTRIES, MIN_POPULATION, TRENDING_LISTS
from baskfy_core.models import (
    CbBasket,
    CbBasketVersion,
    CbManager,
    CbMetrics,
    CbWatchlistItem,
)

TRENDING_PATH = "/cb/trending"


# --- the route, as declared --------------------------------------------------------------


def test_the_document_carries_the_route_and_only_as_a_get() -> None:
    paths = create_app().openapi()["paths"]
    assert "/api/v1/cb/trending" in paths
    assert set(paths["/api/v1/cb/trending"]) == {"get"}


def test_the_route_requires_authentication() -> None:
    """docs/smallcase/02's access matrix is "web login only" for every discovery surface."""
    routes = {
        route.path: route for route in curated_trending.router.routes if isinstance(route, APIRoute)
    }
    assert TRENDING_PATH in routes
    dependants = [routes[TRENDING_PATH].dependant]
    seen = []
    while dependants:
        current = dependants.pop()
        if current.call is not None:
            seen.append(current.call)
        dependants.extend(current.dependencies)
    assert require_authenticated in seen


def test_the_module_cannot_reach_an_order_path() -> None:
    source = inspect.getsource(curated_trending)
    for forbidden in ("place_order", "OrderGateway", "/execute", "confirm=true"):
        assert forbidden not in source, forbidden


def test_only_inflow_shaped_batches_count_as_inflows() -> None:
    """SELL and EXIT take money out; counting them would flatter "biggest inflows"."""
    assert curated_trending._INFLOW_KINDS == ("BUY",)
    assert curated_trending._ACTIVE == "ACTIVE"


def test_every_defined_list_is_representable_in_the_response_model() -> None:
    fields = curated_trending.TrendingListOut.model_fields
    for name in ("key", "ranks_by", "population", "withheld_reason", "withheld_note", "eligible"):
        assert name in fields, name
    assert "price_return_caveat" in fields, (
        "a surface showing a return owes the reader the M39.3 sentence"
    )


async def _engine_manager_id(session: AsyncSession) -> int:
    await seed_curated_managers(session)
    return (
        await session.execute(
            select(CbManager.id).where(CbManager.slug == MANAGER_SLUG_BASKFY_ENGINE)
        )
    ).scalar_one()


async def _basket(
    session: AsyncSession,
    manager_id: int,
    slug: str,
    *,
    visibility: str = "PUBLISHED",
    ret_1y: Decimal | None = None,
) -> int:
    """One published (or deliberately private) basket, with a metrics row when it has a return."""
    basket = CbBasket(
        slug=slug,
        name=slug.replace("-", " ").title(),
        manager_id=manager_id,
        type="STOCK",
        access="FREE",
        visibility=visibility,
        categories=[],
        rebalance_frequency="MONTHLY",
        source="MANUAL",
    )
    session.add(basket)
    await session.flush()
    if ret_1y is not None:
        session.add(
            CbMetrics(
                basket_id=basket.id,
                as_of_date=dt.date(2026, 8, 21),
                ret_1y=ret_1y,
                computed_at=dt.datetime(2026, 8, 21, tzinfo=dt.UTC),
            )
        )
        await session.flush()
    return int(basket.id)


# --- end to end, against a disposable database -------------------------------------------


@pytest.mark.db
@pytest.mark.redis
@requires_db
class TestEndToEnd:
    async def test_an_anonymous_caller_is_refused(self, api: httpx.AsyncClient) -> None:
        response = await api.get(url(TRENDING_PATH))
        assert response.status_code in {401, 403}, response.status_code

    async def test_every_list_comes_back_with_its_floors_and_its_reason(
        self,
        api: httpx.AsyncClient,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id, public_id = await make_user(screener_session, "trending-empty@example.com")
        monkeypatch.setenv(SOLE_USER_ENV, str(user_id))
        response = await api.get(url(TRENDING_PATH), headers=bearer(public_id))
        assert response.status_code == 200, response.text[:400]
        body = response.json()

        assert body["count"] == len(TRENDING_LISTS)
        assert [row["key"] for row in body["items"]] == [d.key for d in TRENDING_LISTS]
        assert body["min_entries"] == MIN_ENTRIES
        assert body["min_population"] == MIN_POPULATION
        # A6 / M39.3 — the surface is handed the price-return sentence, not left to invent one.
        assert body["dividends_included"] is False
        assert body["return_convention_note"]

        for row in body["items"]:
            assert row["ranks_by"], row["key"]
            if not row["entries"]:
                assert row["withheld_reason"] in {"TOO_FEW_BASKETS", "TOO_FEW_PEOPLE"}
                assert row["withheld_note"]

    async def test_a_ranking_publishes_once_there_are_enough_baskets(
        self,
        api: httpx.AsyncClient,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager_id = await _engine_manager_id(screener_session)
        for slug, ret in (("t-alpha", "12.00"), ("t-bravo", "8.00"), ("t-charlie", "4.00")):
            await _basket(screener_session, manager_id, slug, ret_1y=Decimal(ret))

        user_id, public_id = await make_user(screener_session, "trending-ranked@example.com")
        monkeypatch.setenv(SOLE_USER_ENV, str(user_id))
        response = await api.get(url(TRENDING_PATH), headers=bearer(public_id))
        assert response.status_code == 200, response.text[:400]
        lists = {row["key"]: row for row in response.json()["items"]}

        top_1y = lists["TOP_1Y"]
        assert top_1y["withheld_reason"] is None, top_1y["withheld_note"]
        assert [entry["basket_slug"] for entry in top_1y["entries"]] == [
            "t-alpha",
            "t-bravo",
            "t-charlie",
        ]
        assert top_1y["entries"][0]["metric_display"] == "12.00%"

    async def test_a_private_basket_never_appears_in_a_ranking(
        self,
        api: httpx.AsyncClient,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The catalog's own rule. A ranking that forgot it would leak the name through a row."""
        manager_id = await _engine_manager_id(screener_session)
        for slug, ret in (("v-alpha", "3.00"), ("v-bravo", "2.00"), ("v-charlie", "1.00")):
            await _basket(screener_session, manager_id, slug, ret_1y=Decimal(ret))
        await _basket(
            screener_session,
            manager_id,
            "v-secret",
            visibility="PRIVATE",
            ret_1y=Decimal("99.00"),
        )

        user_id, public_id = await make_user(screener_session, "trending-private@example.com")
        monkeypatch.setenv(SOLE_USER_ENV, str(user_id))
        response = await api.get(url(TRENDING_PATH), headers=bearer(public_id))
        assert response.status_code == 200, response.text[:400]
        body = response.json()
        slugs = {entry["basket_slug"] for row in body["items"] for entry in row["entries"]}
        assert "v-secret" not in slugs
        assert body["catalog_size"] == 3

    async def test_counts_are_not_multiplied_by_a_join(
        self,
        api: httpx.AsyncClient,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Three versions and one watcher must report one watcher, not three."""
        manager_id = await _engine_manager_id(screener_session)
        basket_id = await _basket(screener_session, manager_id, "j-alpha", ret_1y=Decimal("5.00"))
        for version_no, day in enumerate((1, 8, 15), start=1):
            screener_session.add(
                CbBasketVersion(
                    basket_id=basket_id,
                    version_no=version_no,
                    effective_date=dt.date(2026, 8, day),
                    label="CHANGED" if version_no > 1 else "GENESIS",
                )
            )
        user_id, public_id = await make_user(screener_session, "trending-join@example.com")
        monkeypatch.setenv(SOLE_USER_ENV, str(user_id))
        screener_session.add(
            CbWatchlistItem(
                user_id=user_id,
                basket_id=basket_id,
                watched_at=dt.datetime(2026, 8, 20, tzinfo=dt.UTC),
            )
        )
        await screener_session.flush()

        candidates, population = await curated_trending._candidates(screener_session)
        subject = next(row for row in candidates if row.slug == "j-alpha")
        assert subject.watchers == 1, "the version join multiplied the watcher count"
        assert subject.last_rebalanced_on == dt.date(2026, 8, 15)
        assert population.watchers == 1

        response = await api.get(url(TRENDING_PATH), headers=bearer(public_id))
        assert response.status_code == 200, response.text[:400]
        watched = next(row for row in response.json()["items"] if row["key"] == "MOST_WATCHED")
        # One person is below the floor, so the honest answer is a withheld list.
        assert watched["withheld_reason"] == "TOO_FEW_PEOPLE"
        assert watched["population"] == 1
