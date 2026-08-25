"""``GET /search`` — the federated ⌘K palette query (`baskfynavrefactorreport` §F11).

What these assert is the *spec*, not the implementation (house rule 2): the report asks for one
search covering "stocks, indices, baskets, and screens", so the first thing here is that a single
request returns all four kinds. Everything after that is the property that makes a federated search
dangerous rather than merely useful — it reaches four tables with four different visibility rules,
and a search box that quietly widens one of them is a data leak with a nice UI.

Driven over HTTP rather than by awaiting the handler, for the reason ``test_explore_http`` gives at
length: a coroutine call cannot see the dependency graph, so it cannot see whether a route requires
authentication, and it never takes the error path.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from collections.abc import Mapping, Sequence
from typing import get_args

import httpx
import pytest
from api_helpers import assert_problem, bearer, make_user, url
from fastapi.routing import APIRoute
from screener_helpers import add_instrument, requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import search as search_service
from baskfy_api.curated_seed import seed_curated_managers
from baskfy_api.routers import explore
from baskfy_api.routers import search as search_router
from baskfy_core.curated_baskets import MANAGER_SLUG_BASKFY_ENGINE
from baskfy_core.models import CbBasket, CbManager, Instrument, Screen

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]

#: An index every seeded database has (`baskfy_core.seed_data.index_def_rows`).
SEEDED_INDEX_NAME = "NIFTY MIDCAP 150"
SEEDED_INDEX_SLUG = "nifty-midcap-150"

#: A screen definition the model accepts. The search never reads it; it only has to be valid.
SCREEN_DEFINITION: Mapping[str, object] = {
    "universe": "nifty-50",
    "filters": [],
    "sort": {"key": "ret_12m", "direction": "desc"},
    "limit": 25,
}


def kinds_in(body: Mapping[str, object]) -> list[str]:
    return [hit["kind"] for hit in rows_of(body)]


def rows_of(body: Mapping[str, object]) -> list[dict[str, str]]:
    data = body["data"]
    assert isinstance(data, list), body
    for row in data:
        assert isinstance(row, dict), row
    return list(data)


def ids_of(body: Mapping[str, object], kind: str) -> list[str]:
    return [hit["id"] for hit in rows_of(body) if hit["kind"] == kind]


async def _manager_id(session: AsyncSession) -> int:
    await seed_curated_managers(session)
    return (
        await session.execute(
            select(CbManager.id).where(CbManager.slug == MANAGER_SLUG_BASKFY_ENGINE)
        )
    ).scalar_one()


async def add_basket(
    session: AsyncSession, slug: str, name: str, *, visibility: str = "PUBLISHED"
) -> None:
    session.add(
        CbBasket(
            slug=slug,
            name=name,
            manager_id=await _manager_id(session),
            type="STOCK",
            access="FREE",
            visibility=visibility,
            categories=[],
            rebalance_frequency="MONTHLY",
            source="MANUAL",
        )
    )
    await session.flush()


async def add_screen(
    session: AsyncSession, public_id: str, name: str, *, user_id: int | None, example: bool = False
) -> None:
    session.add(
        Screen(
            public_id=public_id,
            user_id=user_id,
            name=name,
            definition=dict(SCREEN_DEFINITION),
            columns=[],
            is_example=example,
        )
    )
    await session.flush()


# --- the report's actual ask -------------------------------------------------------------


async def test_one_request_returns_all_four_kinds(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    """F11: "One global search should cover stocks, indices, baskets, and screens."

    The word that matters is *one*. The palette used to make a single scoped call and filter a nav
    array in the browser; four scoped calls per keystroke would have satisfied the sentence and
    missed the point.
    """
    user_id, public_id = await make_user(screener_session, "allfour@example.com")
    await add_instrument(screener_session, "NIFTYWORKS")
    await add_basket(screener_session, "nifty-momentum-basket", "Nifty Momentum Basket")
    await add_screen(
        screener_session, "srchall00001", "Nifty Momentum Screen", user_id=user_id, example=False
    )

    response = await api.get(url("/search"), params={"q": "nifty"}, headers=bearer(public_id))
    assert response.status_code == 200, response.text[:300]

    found = set(kinds_in(response.json()))
    assert found == {"instrument", "index", "basket", "screen"}, (
        f"a federated search returned only {sorted(found)}; F11 asks for all four kinds"
    )


async def test_the_query_is_echoed_so_a_debounced_client_can_drop_stale_answers(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    """The palette re-issues on every keystroke; a response has to say which one it answers."""
    _, public_id = await make_user(screener_session, "echo@example.com")
    response = await api.get(url("/search"), params={"q": "  nifty  "}, headers=bearer(public_id))
    assert response.status_code == 200
    assert response.json()["query"] == "nifty", "the echoed query was not the normalised needle"


async def test_a_hit_carries_identity_and_no_url(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    """`kind` + `id` is the contract; routing is the web app's business (DECISIONS-MERGE M40.1).

    Asserted because the tempting shortcut is to mint `/basket/{slug}` here — and Tree 6 moved
    half these routes, which would have made a nav refactor an API deploy.
    """
    _, public_id = await make_user(screener_session, "identity@example.com")
    await add_instrument(screener_session, "IDENTITYCO")

    response = await api.get(url("/search"), params={"q": "IDENTITYCO"}, headers=bearer(public_id))
    hits = rows_of(response.json())
    assert hits, response.text[:300]
    for hit in hits:
        assert set(hit) <= {"kind", "id", "title", "subtitle"}, hit
        assert "href" not in hit and "url" not in hit, f"a route leaked into the API: {hit}"
    assert hits[0]["id"] == "IDENTITYCO"
    assert hits[0]["title"] == "IDENTITYCO", "a stock's title should be the ticker a person types"
    assert hits[0]["subtitle"] == "IDENTITYCO LIMITED"


# --- ranking and bounds ------------------------------------------------------------------


async def test_an_exact_match_outranks_a_prefix_which_outranks_a_substring(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    """The rule `baskfy_api.instruments` already applied to symbols, now applied to all four."""
    _, public_id = await make_user(screener_session, "rank@example.com")
    await add_instrument(screener_session, "ZXCUPZ")  # substring, and sorts last alphabetically
    await add_instrument(screener_session, "CUPIDX")  # prefix
    await add_instrument(screener_session, "CUP")  # exact

    response = await api.get(
        url("/search"),
        params={"q": "CUP", "limit": search_service.MAX_LIMIT},
        headers=bearer(public_id),
    )
    # The seeded reference export (docs/13) contains CUPID, which is also a prefix match — so the
    # assertion is on the relative order of the three rows this test owns, not on the whole list.
    symbols = [s for s in ids_of(response.json(), "instrument") if s in {"CUP", "CUPIDX", "ZXCUPZ"}]
    assert symbols == ["CUP", "CUPIDX", "ZXCUPZ"], symbols


async def test_limit_is_per_kind_so_one_kind_cannot_crowd_out_the_others(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    """A total limit would let 2,300 instruments bury the one basket the user meant."""
    _, public_id = await make_user(screener_session, "perkind@example.com")
    for n in range(6):
        await add_instrument(screener_session, f"CROWD{n}")
    await add_basket(screener_session, "crowded-basket", "Crowd Basket")

    response = await api.get(
        url("/search"), params={"q": "crowd", "limit": 2}, headers=bearer(public_id)
    )
    body = response.json()
    assert len(ids_of(body, "instrument")) == 2, ids_of(body, "instrument")
    assert ids_of(body, "basket") == ["crowded-basket"], (
        "the basket was crowded out by instruments, so the limit is a total, not per kind"
    )


async def test_bounds_are_declared_not_merely_hoped_for(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    """An empty `q` and an over-large `limit` are refused at the boundary, not swallowed.

    400 rather than 422: `create_app` maps every `RequestValidationError` onto the documented
    `invalid-screen-definition` problem (docs/07 §Conventions), so this is the house shape for a
    bad parameter and not a special case for search.
    """
    _, public_id = await make_user(screener_session, "bounds@example.com")
    headers = bearer(public_id)

    empty = await api.get(url("/search"), params={"q": ""}, headers=headers)
    assert_problem(empty, 400, "invalid-screen-definition")

    over = await api.get(
        url("/search"),
        params={"q": "nifty", "limit": search_service.MAX_LIMIT + 1},
        headers=headers,
    )
    assert_problem(over, 400, "invalid-screen-definition")

    missing = await api.get(url("/search"), headers=headers)
    assert_problem(missing, 400, "invalid-screen-definition")


async def test_an_index_is_found_by_slug_as_well_as_by_name(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    """ "NIFTY MIDCAP 150" and `nifty-midcap-150` differ only in punctuation; people type both."""
    _, public_id = await make_user(screener_session, "byslug@example.com")
    headers = bearer(public_id)

    by_name = await api.get(url("/search"), params={"q": "MIDCAP 150"}, headers=headers)
    by_slug = await api.get(url("/search"), params={"q": "midcap-150"}, headers=headers)

    assert SEEDED_INDEX_SLUG in ids_of(by_name.json(), "index")
    assert SEEDED_INDEX_SLUG in ids_of(by_slug.json(), "index")
    titles = [hit["title"] for hit in rows_of(by_name.json()) if hit["kind"] == "index"]
    assert SEEDED_INDEX_NAME in titles


# --- visibility: the reason a federated search is worth testing hard ----------------------


async def test_an_anonymous_caller_gets_public_kinds_and_no_baskets(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    """`/explore` requires a user, so search must not become the door that skips the lock.

    And the whole search must still answer: a 401 for one of four groups would take ⌘K away from
    every marketing page.
    """
    await add_basket(screener_session, "anon-invisible-basket", "Anon Invisible Basket")
    await add_instrument(screener_session, "ANONCO")

    response = await api.get(url("/search"), params={"q": "anon"})
    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert ids_of(body, "basket") == [], "an anonymous caller was served the curated catalog"
    assert "ANONCO" in ids_of(body, "instrument"), "public instruments should still answer"


async def test_a_private_or_archived_basket_is_never_returned(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    """`routers.explore._visible()`: `visibility = 'PUBLISHED'` and `archived_at IS NULL`."""
    _, public_id = await make_user(screener_session, "vis@example.com")
    await add_basket(screener_session, "hidden-idea", "Hidden Idea", visibility="PRIVATE")
    await add_basket(screener_session, "shown-idea", "Shown Idea")

    body = (await api.get(url("/search"), params={"q": "idea"}, headers=bearer(public_id))).json()
    slugs = ids_of(body, "basket")
    assert "shown-idea" in slugs
    assert "hidden-idea" not in slugs, "a PRIVATE basket was reachable through search"


async def test_another_users_screen_is_never_returned(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    """`routers.screens`' rule: examples for everyone, own screens for their owner, nothing else."""
    mine_id, mine_public = await make_user(screener_session, "mine@example.com")
    theirs_id, _ = await make_user(screener_session, "theirs@example.com")
    await add_screen(screener_session, "srchmine0001", "Zeta Mine", user_id=mine_id)
    await add_screen(screener_session, "srchtheirs01", "Zeta Theirs", user_id=theirs_id)
    await add_screen(screener_session, "srchexampl01", "Zeta Example", user_id=None, example=True)

    body = (await api.get(url("/search"), params={"q": "zeta"}, headers=bearer(mine_public))).json()
    found = ids_of(body, "screen")
    assert "srchmine0001" in found
    assert "srchexampl01" in found, "example screens are read-only templates everyone may see"
    assert "srchtheirs01" not in found, "another user's saved screen was reachable through search"


async def test_an_anonymous_caller_sees_example_screens_only(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    owner_id, _ = await make_user(screener_session, "owner@example.com")
    await add_screen(screener_session, "srchowned001", "Kappa Owned", user_id=owner_id)
    await add_screen(screener_session, "srchtmpl0001", "Kappa Template", user_id=None, example=True)

    body = (await api.get(url("/search"), params={"q": "kappa"})).json()
    assert ids_of(body, "screen") == ["srchtmpl0001"], ids_of(body, "screen")


async def test_a_delisted_instrument_is_not_offered_as_a_destination(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    """Kept for point-in-time correctness (docs/04); there is nowhere useful to navigate."""
    _, public_id = await make_user(screener_session, "delisted@example.com")
    instrument_id = await add_instrument(screener_session, "GONECO")
    row = await screener_session.get(Instrument, instrument_id)
    assert row is not None
    row.is_active = False
    await screener_session.flush()

    body = (await api.get(url("/search"), params={"q": "GONECO"}, headers=bearer(public_id))).json()
    assert ids_of(body, "instrument") == []


# --- structural: the properties an HTTP round trip cannot see -----------------------------


def test_the_route_is_open_to_anonymous_callers_by_declaration() -> None:
    """The mixed-surface decision, asserted where it is made rather than only where it shows."""
    routes = {r.path: r for r in search_router.router.routes if isinstance(r, APIRoute)}
    route = routes.get("/search")
    assert route is not None, "the /search route moved"
    handler = route.dependant.call
    assert handler is not None
    params = inspect.signature(handler).parameters
    annotations = " ".join(str(p.annotation) for p in params.values())
    assert "PrincipalDep" in annotations, "search must see the caller to scope baskets and screens"
    assert "AuthenticatedDep" not in annotations, (
        "search is a mixed surface: requiring a login would take ⌘K away from marketing pages"
    )


def test_the_basket_predicate_is_the_same_one_explore_applies() -> None:
    """Two copies of a visibility rule is how one of them drifts.

    `baskfy_api.search` spells the predicate out rather than importing `explore._visible()` (a
    service must not import a router), so this ties the copy to the original: whatever `_visible`
    constrains, the search statement must constrain too.
    """
    constrained = {
        str(clause).split(".", maxsplit=1)[-1].split()[0] for clause in explore._visible()
    }
    source = pathlib.Path(inspect.getfile(search_service)).read_text()
    body = inspect.getsource(search_service.search_baskets)
    for column in constrained:
        assert column in body, (
            f"explore._visible() constrains {column!r} and the search statement does not"
        )
    assert "PUBLISHED" in source


def test_every_searcher_bounds_its_result_set() -> None:
    """A palette query with no LIMIT is a table scan rendered into a dialog."""
    tree = ast.parse(pathlib.Path(inspect.getfile(search_service)).read_text())
    searchers: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("search_"):
            if node.name == "search_catalog":
                continue
            searchers.append(node.name)
            assert "limit" in ast.dump(node), f"{node.name} does not bound its result set"
    assert sorted(searchers) == [
        "search_baskets",
        "search_indices",
        "search_instruments",
        "search_screens",
    ], searchers


def test_kind_order_covers_every_kind_the_literal_admits() -> None:
    """A kind added to the union and forgotten in the order is a group that never renders."""
    literal_kinds: Sequence[str] = get_args(search_service.CatalogKind)
    assert sorted(literal_kinds) == sorted(search_service.KIND_ORDER)
