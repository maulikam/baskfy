"""Collections — the editorial shelves ``/baskets`` is browsed by.

`cb_collection` has existed since migration 0014 and held nothing. These tests assert the three
things that were missing: content that seeds itself, a payload a page can render in one call, and
a visibility rule that does not quietly leak a PRIVATE basket onto a public shelf.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from api_helpers import bearer, make_user, url
from screener_helpers import requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.curated_seed import (
    COLLECTION_SEED_ROWS,
    count_curated_collections,
    seed_curated_collections,
)
from baskfy_core.models import CbBasket, CbCollection, CbManager

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]


@pytest.fixture
def session(screener_session: AsyncSession) -> AsyncSession:
    """The same transaction the ``api`` fixture serves requests from."""
    return screener_session


async def _headers(session: AsyncSession, tag: str) -> dict[str, str]:
    _, public_id = await make_user(session, f"{tag}@example.com")
    return bearer(public_id)


async def _a_basket(session: AsyncSession, slug: str, **over: object) -> CbBasket:
    manager = (await session.execute(select(CbManager).limit(1))).scalar_one()
    fields: dict[str, object] = {
        "slug": slug,
        "name": slug,
        "manager_id": manager.id,
        "type": "STOCK",
        "access": "FREE",
        "visibility": "PUBLISHED",
        "categories": [],
        "rebalance_frequency": "QUARTERLY",
        "source": "MANUAL",
    }
    fields.update(over)
    basket = CbBasket(**fields)
    session.add(basket)
    await session.flush()
    return basket


async def _shelf(session: AsyncSession, slug: str, basket_ids: list[int]) -> CbCollection:
    collection = CbCollection(
        slug=slug, title=slug.title(), subtitle=None, basket_ids=basket_ids, position=99
    )
    session.add(collection)
    await session.flush()
    return collection


class TestSeeding:
    async def test_seeding_is_idempotent(self, session: AsyncSession) -> None:
        """House rule 7: re-running any day's job produces identical rows."""
        await seed_curated_collections(session)
        first = {
            row.slug: list(row.basket_ids)
            for row in (await session.execute(select(CbCollection))).scalars()
        }
        count_after_first = await count_curated_collections(session)

        await seed_curated_collections(session)
        second = {
            row.slug: list(row.basket_ids)
            for row in (await session.execute(select(CbCollection))).scalars()
        }
        assert second == first, "a second run changed the rows"
        assert await count_curated_collections(session) == count_after_first

    async def test_every_seeded_shelf_is_created_even_when_its_rule_matches_nothing(
        self, session: AsyncSession
    ) -> None:
        """A missing shelf is a false statement about the product; an empty one is a true one."""
        await seed_curated_collections(session)
        slugs = {row.slug for row in (await session.execute(select(CbCollection))).scalars()}
        assert slugs >= {shelf.slug for shelf in COLLECTION_SEED_ROWS}

    async def test_a_shelf_never_names_a_basket_that_is_absent_or_hidden(
        self, session: AsyncSession
    ) -> None:
        """Membership is resolved from what exists, so it cannot name a basket that does not."""
        await _a_basket(session, "col-hidden-seed", visibility="PRIVATE")
        await seed_curated_collections(session)
        hidden = (
            await session.execute(select(CbBasket).where(CbBasket.slug == "col-hidden-seed"))
        ).scalar_one()
        for row in (await session.execute(select(CbCollection))).scalars():
            assert hidden.id not in list(row.basket_ids), f"{row.slug} named a PRIVATE basket"

    async def test_membership_refreshes_rather_than_accumulating(
        self, session: AsyncSession
    ) -> None:
        """A derived list must not grow by one copy per run."""
        await seed_curated_collections(session)
        await _a_basket(session, "col-late-arrival", categories=["momentum"])
        await seed_curated_collections(session)
        momentum = (
            await session.execute(select(CbCollection).where(CbCollection.slug == "momentum"))
        ).scalar_one()
        ids = list(momentum.basket_ids)
        assert len(ids) == len(set(ids)), "membership accumulated duplicates"


class TestThePayloadCanBeRendered:
    async def test_a_collection_returns_cards_not_only_slugs(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """One request must be enough to draw the page; slugs alone force N+1."""
        headers = await _headers(session, "colcards")
        basket = await _a_basket(session, "col-card-1")
        await _shelf(session, "col-cards", [basket.id])
        body = (await api.get(url("/explore/collections/col-cards"), headers=headers)).json()
        assert body["baskets"], "no cards returned"
        card = body["baskets"][0]
        for field in ("slug", "name", "manager", "access", "rebalance_frequency"):
            assert field in card, f"a card without {field} cannot be rendered"

    async def test_basket_slugs_and_cards_never_disagree(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """Two spellings of one list is how they drift; this is the standing objection."""
        headers = await _headers(session, "colagree")
        one = await _a_basket(session, "col-agree-1")
        two = await _a_basket(session, "col-agree-2")
        await _shelf(session, "col-agree", [one.id, two.id])
        body = (await api.get(url("/explore/collections/col-agree"), headers=headers)).json()
        assert body["basket_slugs"] == [card["slug"] for card in body["baskets"]]

    async def test_the_editorial_order_is_preserved(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """`basket_ids` order is the curator's decision, not the database's."""
        headers = await _headers(session, "colorder")
        first = await _a_basket(session, "col-order-zulu")
        second = await _a_basket(session, "col-order-alpha")
        await _shelf(session, "col-order", [first.id, second.id])
        body = (await api.get(url("/explore/collections/col-order"), headers=headers)).json()
        assert body["basket_slugs"] == ["col-order-zulu", "col-order-alpha"]

    async def test_an_empty_shelf_answers_with_an_empty_list_not_an_error(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        headers = await _headers(session, "colempty")
        await _shelf(session, "col-empty", [])
        response = await api.get(url("/explore/collections/col-empty"), headers=headers)
        assert response.status_code == 200
        assert response.json()["baskets"] == []
        assert response.json()["withheld"] == 0


class TestVisibility:
    async def test_a_private_basket_named_by_a_shelf_is_not_returned(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """`cb_basket` has no owner column, so `visibility` is the only thing standing here."""
        headers = await _headers(session, "colprivate")
        shown = await _a_basket(session, "col-vis-shown")
        hidden = await _a_basket(session, "col-vis-hidden", visibility="PRIVATE")
        await _shelf(session, "col-vis", [shown.id, hidden.id])
        body = (await api.get(url("/explore/collections/col-vis"), headers=headers)).json()
        assert body["basket_slugs"] == ["col-vis-shown"]
        assert body["withheld"] == 1, "a withheld basket must be counted, not silently dropped"

    async def test_an_archived_basket_named_by_a_shelf_is_not_returned(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        headers = await _headers(session, "colarchived")
        shown = await _a_basket(session, "col-arch-shown")
        gone = await _a_basket(
            session,
            "col-arch-gone",
            archived_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )
        await _shelf(session, "col-arch", [shown.id, gone.id])
        body = (await api.get(url("/explore/collections/col-arch"), headers=headers)).json()
        assert body["basket_slugs"] == ["col-arch-shown"]
        assert body["withheld"] == 1

    async def test_a_shelf_of_only_hidden_baskets_reads_as_empty_not_as_missing(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        headers = await _headers(session, "colallhidden")
        hidden = await _a_basket(session, "col-allhidden-1", visibility="PRIVATE")
        await _shelf(session, "col-allhidden", [hidden.id])
        body = (await api.get(url("/explore/collections/col-allhidden"), headers=headers)).json()
        assert body["baskets"] == []
        assert body["withheld"] == 1


class TestTheRoutes:
    async def test_listing_requires_a_user(self, api: httpx.AsyncClient) -> None:
        assert (await api.get(url("/explore/collections"))).status_code == 401

    async def test_the_detail_route_requires_a_user(self, api: httpx.AsyncClient) -> None:
        assert (await api.get(url("/explore/collections/momentum"))).status_code == 401

    async def test_an_unknown_slug_is_not_found(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        headers = await _headers(session, "colmissing")
        response = await api.get(url("/explore/collections/no-such-shelf"), headers=headers)
        assert response.status_code == 404

    async def test_the_listing_is_ordered_by_position(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        headers = await _headers(session, "colposition")
        await seed_curated_collections(session)
        items = (await api.get(url("/explore/collections"), headers=headers)).json()["items"]
        positions = [item["position"] for item in items]
        assert positions == sorted(positions)
