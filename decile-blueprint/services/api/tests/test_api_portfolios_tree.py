"""``/portfolios`` as a forest — tree 5, leaf B1.

Migration 0019 gave ``portfolio`` a ``parent_id`` and a ``broker_account_id``, and gave
``portfolio_holding`` a primary key that includes the account. This module is the contract those
columns bought at the HTTP boundary:

* a portfolio may be created inside another one, and a parent that is not the caller's own is
  ``NOT_FOUND`` — never ``FORBIDDEN``, which would confirm that the id exists;
* ``GET /portfolios`` nests: a child appears **under** its parent, not beside it;
* a portfolio can be moved, and the two moves a naive check misses — under one's own descendant,
  and a legal parent plus a legal subtree that are illegal together — are refused;
* ``GET /portfolios/{id}/holdings`` says whose money is where, split per broker account, with a
  total that is exactly the sum of the lines;
* deleting a grouping node promotes its children rather than deleting the money underneath them.

Read and record only. Nothing here places, plans or previews an order — non-negotiable #1 and the
second law — and ``test_no_order_path_in_the_router`` asserts that structurally, over the source,
so a route added in a hurry fails here rather than in review.
"""

from __future__ import annotations

import datetime as dt
import inspect
from decimal import Decimal

import httpx
import pytest
from api_helpers import assert_problem, bearer, make_user, url
from screener_helpers import requires_db
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.routers import portfolios as router
from baskfy_core.models import BrokerAccount, Instrument, Portfolio, PortfolioHolding
from baskfy_core.portfolio_graph import MAX_DEPTH
from baskfy_core.seed_data import NSE_EXCHANGE_ID

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]

#: One decoded JSON object. ``object`` rather than ``Any`` — CLAUDE.md house rule 3 — with
#: :func:`obj` and :func:`arr` doing the narrowing an ``Any`` would have skipped.
Json = dict[str, object]

#: docs/07's single 400. See ``baskfy_api.routers.portfolios.TREE_VALIDATION`` for why the tree's
#: refusals answer with the screen-shaped member rather than one of their own.
VALIDATION = "invalid-screen-definition"


def obj(value: object) -> Json:
    assert isinstance(value, dict), value
    return value


def arr(value: object) -> list[Json]:
    assert isinstance(value, list), value
    return [obj(item) for item in value]


def body_of(response: httpx.Response) -> Json:
    return obj(response.json())


def num(value: object) -> int:
    assert isinstance(value, int), value
    return value


def dec(value: object) -> Decimal:
    """A money or quantity field off the wire.

    Asserted to arrive as a **string**, not a JSON number: house rule 9 says money is never a
    float, and a JSON number is exactly the shape that becomes one in the first client that
    parses it.
    """
    assert isinstance(value, str), value
    return Decimal(value)


async def owner(session: AsyncSession, email: str) -> tuple[int, dict[str, str]]:
    """A signed-in user. Returns ``(user_id, auth headers)``.

    The addresses are opaque tokens — ``b1u007@example.com`` — rather than descriptive names,
    and deliberately: :func:`api_helpers.make_user` derives ``app_user.public_id`` from the local
    part with dots removed and **truncated to twelve characters**, so two readable addresses like
    ``isolation.money.a`` and ``isolation.money.b`` both become ``isolationmon`` and the second
    insert dies on ``uq_app_user_public_id``. A test that fails on its own fixture proves
    nothing, so uniqueness is made obvious here instead of being hoped for.
    """
    user_id, public_id = await make_user(session, email)
    return user_id, bearer(public_id)


async def create(  # noqa: PLR0913 - the two 0019 columns are separate optional arguments
    api: httpx.AsyncClient,
    headers: dict[str, str],
    name: str,
    *,
    parent_id: int | None = None,
    broker_account_id: int | None = None,
    holdings: list[Json] | None = None,
) -> httpx.Response:
    body: Json = {"name": name, "holdings": holdings or []}
    if parent_id is not None:
        body["parent_id"] = parent_id
    if broker_account_id is not None:
        body["broker_account_id"] = broker_account_id
    response = await api.post(url("/portfolios"), json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response


async def make(  # noqa: PLR0913 - mirrors :func:`create`, one argument per column
    api: httpx.AsyncClient,
    headers: dict[str, str],
    name: str,
    *,
    parent_id: int | None = None,
    broker_account_id: int | None = None,
    holdings: list[Json] | None = None,
) -> int:
    """A created portfolio's id."""
    response = await create(
        api,
        headers,
        name,
        parent_id=parent_id,
        broker_account_id=broker_account_id,
        holdings=holdings,
    )
    return num(obj(body_of(response)["portfolio"])["id"])


async def patch(
    api: httpx.AsyncClient, headers: dict[str, str], portfolio_id: int, body: Json
) -> httpx.Response:
    return await api.patch(url(f"/portfolios/{portfolio_id}"), json=body, headers=headers)


async def listing(api: httpx.AsyncClient, headers: dict[str, str]) -> Json:
    response = await api.get(url("/portfolios"), headers=headers)
    assert response.status_code == 200, response.text
    return body_of(response)


async def rollup(
    api: httpx.AsyncClient, headers: dict[str, str], portfolio_id: int
) -> httpx.Response:
    response = await api.get(url(f"/portfolios/{portfolio_id}/holdings"), headers=headers)
    assert response.status_code == 200, response.text
    return response


async def chain(
    api: httpx.AsyncClient, headers: dict[str, str], depth: int, *, prefix: str = "L"
) -> list[int]:
    """``depth`` portfolios, each the child of the one before it. Returns them root-first."""
    ids: list[int] = []
    parent: int | None = None
    for level in range(1, depth + 1):
        parent = await make(api, headers, f"{prefix}{level}", parent_id=parent)
        ids.append(parent)
    return ids


async def second_broker_account(session: AsyncSession, user_id: int, broker_id: str) -> int:
    """A second ``broker_account`` for a user, so a subtree can genuinely span two of them."""
    row = BrokerAccount(user_id=user_id, broker_id=broker_id, label=broker_id)
    session.add(row)
    await session.flush()
    return row.id


async def instrument_id(session: AsyncSession, symbol: str) -> int:
    found = await session.scalar(
        select(Instrument.id).where(
            Instrument.exchange_id == NSE_EXCHANGE_ID, Instrument.symbol == symbol
        )
    )
    assert found is not None, symbol
    return int(found)


async def stored_holdings(
    session: AsyncSession, portfolio_id: int
) -> list[tuple[int, int, Decimal | None, dt.date]]:
    """``(instrument_id, broker_account_id, quantity, added_on)`` for one portfolio."""
    rows = (
        await session.execute(
            select(
                PortfolioHolding.instrument_id,
                PortfolioHolding.broker_account_id,
                PortfolioHolding.quantity,
                PortfolioHolding.added_on,
            )
            .where(PortfolioHolding.portfolio_id == portfolio_id)
            .order_by(PortfolioHolding.instrument_id, PortfolioHolding.broker_account_id)
        )
    ).all()
    return [(row.instrument_id, row.broker_account_id, row.quantity, row.added_on) for row in rows]


def node_by_id(nodes: list[Json], portfolio_id: int) -> Json:
    for node in nodes:
        if node["id"] == portfolio_id:
            return node
    raise AssertionError(f"{portfolio_id} not among {[node['id'] for node in nodes]}")


def flatten(nodes: list[Json]) -> list[int]:
    """Every id in a nested listing, parents before children."""
    found: list[int] = []
    for node in nodes:
        found.append(num(node["id"]))
        found.extend(flatten(arr(node["children"])))
    return found


# ---------------------------------------------------------------------------
# Creating inside another portfolio
# ---------------------------------------------------------------------------


class TestCreateWithAParent:
    async def test_a_portfolio_can_be_created_inside_a_parent(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, headers = await owner(screener_session, "b1u001@example.com")
        root = await make(api, headers, "Retirement")
        response = await create(api, headers, "Equity", parent_id=root)

        child = obj(body_of(response)["portfolio"])
        assert child["parent_id"] == root
        assert child["depth"] == 2

    async def test_a_portfolio_created_without_a_parent_is_still_a_root(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The nesting is additive: a client that has never heard of 0019 keeps working."""
        _, headers = await owner(screener_session, "b1u002@example.com")
        response = await create(api, headers, "Core")
        created = obj(body_of(response)["portfolio"])
        assert created["parent_id"] is None
        assert created["depth"] == 1
        assert created["broker_account_id"] is None

    async def test_a_parent_owned_by_another_user_is_not_found(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """Not 403: a 403 confirms the id exists and tells the caller whose it is."""
        _, alice = await owner(screener_session, "b1u003@example.com")
        _, bob = await owner(screener_session, "b1u004@example.com")
        hers = await make(api, alice, "Alice's book")

        refused = await api.post(
            url("/portfolios"),
            json={"name": "Mine", "holdings": [], "parent_id": hers},
            headers=bob,
        )
        problem = assert_problem(refused, 404, "not-found")
        assert "Alice" not in str(problem), problem

    async def test_a_parent_that_does_not_exist_answers_identically(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The two answers must be the same shape, or the difference is the leak."""
        _, alice = await owner(screener_session, "b1u005@example.com")
        _, bob = await owner(screener_session, "b1u006@example.com")
        hers = await make(api, alice, "Hers")

        foreign = await api.post(
            url("/portfolios"),
            json={"name": "Mine", "holdings": [], "parent_id": hers},
            headers=bob,
        )
        absent = await api.post(
            url("/portfolios"),
            json={"name": "Mine", "holdings": [], "parent_id": 2_000_000_001},
            headers=bob,
        )
        assert foreign.status_code == absent.status_code == 404
        assert obj(foreign.json())["title"] == obj(absent.json())["title"]

    async def test_a_broker_account_owned_by_another_user_is_not_found(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        alice_id, _ = await owner(screener_session, "b1u007@example.com")
        _, bob = await owner(screener_session, "b1u008@example.com")
        hers = await second_broker_account(screener_session, alice_id, "upstox")

        refused = await api.post(
            url("/portfolios"),
            json={"name": "Mine", "holdings": [], "broker_account_id": hers},
            headers=bob,
        )
        assert_problem(refused, 404, "not-found")

    async def test_a_portfolio_may_declare_its_own_broker_account(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        user_id, headers = await owner(screener_session, "b1u009@example.com")
        account = await second_broker_account(screener_session, user_id, "upstox")
        created = await make(api, headers, "Upstox book", broker_account_id=account)

        fetched = body_of(await api.get(url(f"/portfolios/{created}"), headers=headers))
        assert fetched["broker_account_id"] == account


# ---------------------------------------------------------------------------
# The depth cap
# ---------------------------------------------------------------------------


class TestDepth:
    async def test_a_chain_of_exactly_six_is_legal(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """``MAX_DEPTH`` is inclusive: six is legal, seven is not."""
        _, headers = await owner(screener_session, "b1u010@example.com")
        ids = await chain(api, headers, MAX_DEPTH)
        deepest = body_of(await api.get(url(f"/portfolios/{ids[-1]}"), headers=headers))
        assert deepest["depth"] == MAX_DEPTH

    async def test_creating_a_seventh_level_is_refused(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, headers = await owner(screener_session, "b1u011@example.com")
        ids = await chain(api, headers, MAX_DEPTH)
        refused = await api.post(
            url("/portfolios"),
            json={"name": "Too deep", "holdings": [], "parent_id": ids[-1]},
            headers=headers,
        )
        problem = assert_problem(refused, 400, VALIDATION)
        detail = str(problem["detail"])
        assert "depth 7" in detail, detail
        assert str(MAX_DEPTH) in detail, detail

    async def test_the_refused_seventh_level_was_not_written(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """A refusal that still created the row would be worse than no check at all."""
        _, headers = await owner(screener_session, "b1u012@example.com")
        ids = await chain(api, headers, MAX_DEPTH)
        refused = await api.post(
            url("/portfolios"),
            json={"name": "Too deep", "holdings": [], "parent_id": ids[-1]},
            headers=headers,
        )
        assert refused.status_code == 400, refused.text

        body = await listing(api, headers)
        assert flatten(arr(body["data"])) == ids

    async def test_move_that_would_exceed_the_depth_cap_only_in_combination_is_refused(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """A legal parent and a legal subtree can still be illegal **together**.

        The parent sits at depth 4 and the subtree is three levels tall; each is fine on its own
        and the move would put the subtree's leaves at depth 7. A check that looked only at the
        parent, or only at the moved row, would allow it.
        """
        _, headers = await owner(screener_session, "b1u013@example.com")
        trunk = await chain(api, headers, 4, prefix="T")
        branch = await chain(api, headers, 3, prefix="B")

        refused = await patch(api, headers, branch[0], {"parent_id": trunk[-1]})
        problem = assert_problem(refused, 400, VALIDATION)
        assert "depth 7" in str(problem["detail"]), problem

    async def test_the_same_subtree_moves_legally_one_level_higher(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The mirror of the test above: three levels under depth 3 lands exactly on the cap."""
        _, headers = await owner(screener_session, "b1u014@example.com")
        trunk = await chain(api, headers, 3, prefix="T")
        branch = await chain(api, headers, 3, prefix="B")

        moved = await patch(api, headers, branch[0], {"parent_id": trunk[-1]})
        assert moved.status_code == 200, moved.text
        assert body_of(moved)["depth"] == 4

        deepest = body_of(await api.get(url(f"/portfolios/{branch[-1]}"), headers=headers))
        assert deepest["depth"] == MAX_DEPTH


# ---------------------------------------------------------------------------
# Cycles
# ---------------------------------------------------------------------------


class TestCycles:
    async def test_a_portfolio_cannot_become_its_own_parent(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, headers = await owner(screener_session, "b1u015@example.com")
        alone = await make(api, headers, "Alone")

        refused = await patch(api, headers, alone, {"parent_id": alone})
        problem = assert_problem(refused, 400, VALIDATION)
        assert f"{alone} -> {alone}" in str(problem["detail"]), problem

    async def test_a_cycle_through_a_descendant_is_refused_and_names_the_ring(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """Moving a portfolio under its own child severs the branch into a free-floating ring."""
        _, headers = await owner(screener_session, "b1u016@example.com")
        top = await make(api, headers, "Top")
        below = await make(api, headers, "Below", parent_id=top)

        refused = await patch(api, headers, top, {"parent_id": below})
        problem = assert_problem(refused, 400, VALIDATION)
        detail = str(problem["detail"])
        assert "cycle" in detail, detail
        assert str(top) in detail and str(below) in detail, detail

    async def test_a_cycle_two_levels_down_is_refused_too(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The naive check — "is the new parent this portfolio?" — passes this one."""
        _, headers = await owner(screener_session, "b1u017@example.com")
        ids = await chain(api, headers, 3, prefix="G")

        refused = await patch(api, headers, ids[0], {"parent_id": ids[2]})
        assert_problem(refused, 400, VALIDATION)

    async def test_a_cycle_already_in_the_database_is_reported_not_a_500(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """Damage that this API cannot create is still damage this API may have to read.

        The ring is planted with SQL, behind the router's back, because no request can make one.
        Reading it must answer the documented problem — an infinite loop or a 500 would be the
        two worse alternatives.
        """
        _, headers = await owner(screener_session, "b1u018@example.com")
        first = await make(api, headers, "First")
        second = await make(api, headers, "Second", parent_id=first)
        await screener_session.execute(
            update(Portfolio).where(Portfolio.id == first).values(parent_id=second)
        )

        response = await api.get(url("/portfolios"), headers=headers)
        problem = assert_problem(response, 400, VALIDATION)
        assert "cycle" in str(problem["detail"]), problem


# ---------------------------------------------------------------------------
# The nested listing
# ---------------------------------------------------------------------------


class TestTheNestedListing:
    async def test_a_child_appears_nested_under_its_parent(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, headers = await owner(screener_session, "b1u019@example.com")
        root = await make(api, headers, "Retirement")
        child = await make(api, headers, "Equity", parent_id=root)

        body = await listing(api, headers)
        roots = arr(body["data"])
        assert [node["id"] for node in roots] == [root]
        assert [node["id"] for node in arr(roots[0]["children"])] == [child]

    async def test_a_child_is_never_also_a_root(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The bug this whole shape exists to prevent: a child rendered as a flat sibling."""
        _, headers = await owner(screener_session, "b1u020@example.com")
        root = await make(api, headers, "Retirement")
        child = await make(api, headers, "Equity", parent_id=root)

        body = await listing(api, headers)
        assert child not in [node["id"] for node in arr(body["data"])]

    async def test_every_portfolio_appears_exactly_once(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, headers = await owner(screener_session, "b1u021@example.com")
        deep = await chain(api, headers, 4)
        loose = await make(api, headers, "Loose")

        body = await listing(api, headers)
        found = flatten(arr(body["data"]))
        assert sorted(found) == sorted([*deep, loose])
        assert len(found) == len(set(found))

    async def test_the_nesting_carries_depth_and_the_parent_id(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, headers = await owner(screener_session, "b1u022@example.com")
        ids = await chain(api, headers, 3)

        body = await listing(api, headers)
        root = arr(body["data"])[0]
        assert root["depth"] == 1 and root["parent_id"] is None
        middle = arr(root["children"])[0]
        assert middle["depth"] == 2 and middle["parent_id"] == ids[0]
        leaf = arr(middle["children"])[0]
        assert leaf["depth"] == 3 and leaf["parent_id"] == ids[1]

    async def test_holdings_count_stays_this_portfolios_own(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """It counted this portfolio's rows before 0019 and it counts them now.

        A count that silently became "the subtree's" the day nesting landed would change every
        existing client's meaning without changing its code.
        """
        _, headers = await owner(screener_session, "b1u023@example.com")
        root = await make(api, headers, "Root", holdings=[{"symbol": "CUPID"}])
        await make(api, headers, "Child", parent_id=root, holdings=[{"symbol": "HFCL"}])

        body = await listing(api, headers)
        node = arr(body["data"])[0]
        assert node["holdings_count"] == 1
        assert arr(node["children"])[0]["holdings_count"] == 1

    async def test_a_dangling_parent_is_reported_as_an_orphan_not_dropped(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """A fragment whose parent is not in the caller's set is holdings that must not vanish.

        Unreachable through the API — a foreign parent is refused — so it is planted with SQL.
        """
        _, alice = await owner(screener_session, "b1u024@example.com")
        _, bob = await owner(screener_session, "b1u025@example.com")
        hers = await make(api, alice, "Hers")
        his = await make(api, bob, "His")
        await screener_session.execute(
            update(Portfolio).where(Portfolio.id == his).values(parent_id=hers)
        )

        body = await listing(api, bob)
        assert [node["id"] for node in arr(body["data"])] == []
        orphans = arr(body["orphans"])
        assert [node["id"] for node in orphans] == [his]
        assert orphans[0]["parent_id"] == hers


# ---------------------------------------------------------------------------
# Moving
# ---------------------------------------------------------------------------


class TestMove:
    async def test_move_to_a_new_parent(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, headers = await owner(screener_session, "b1u026@example.com")
        first = await make(api, headers, "First")
        second = await make(api, headers, "Second")
        wanderer = await make(api, headers, "Wanderer", parent_id=first)

        moved = await patch(api, headers, wanderer, {"parent_id": second})
        assert moved.status_code == 200, moved.text
        assert body_of(moved)["parent_id"] == second

        body = await listing(api, headers)
        under_second = node_by_id(arr(body["data"]), second)
        assert [node["id"] for node in arr(under_second["children"])] == [wanderer]
        assert arr(node_by_id(arr(body["data"]), first)["children"]) == []

    async def test_move_to_null_promotes_a_child_to_a_root(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """``"parent_id": null`` is a request; omitting the key is not."""
        _, headers = await owner(screener_session, "b1u027@example.com")
        root = await make(api, headers, "Root")
        child = await make(api, headers, "Child", parent_id=root)

        promoted = await patch(api, headers, child, {"parent_id": None})
        assert promoted.status_code == 200, promoted.text
        assert body_of(promoted)["parent_id"] is None
        assert body_of(promoted)["depth"] == 1

        body = await listing(api, headers)
        assert sorted(node["id"] for node in arr(body["data"])) == sorted([root, child])

    async def test_move_under_its_own_descendant_is_refused(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, headers = await owner(screener_session, "b1u028@example.com")
        ids = await chain(api, headers, 3, prefix="D")

        refused = await patch(api, headers, ids[0], {"parent_id": ids[2]})
        assert_problem(refused, 400, VALIDATION)

        body = await listing(api, headers)
        assert [node["id"] for node in arr(body["data"])] == [ids[0]]

    async def test_move_carries_the_subtree_with_it(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, headers = await owner(screener_session, "b1u029@example.com")
        elsewhere = await make(api, headers, "Elsewhere")
        branch = await chain(api, headers, 3, prefix="S")

        moved = await patch(api, headers, branch[1], {"parent_id": elsewhere})
        assert moved.status_code == 200, moved.text

        body = await listing(api, headers)
        under = node_by_id(arr(body["data"]), elsewhere)
        assert [node["id"] for node in arr(under["children"])] == [branch[1]]
        assert [node["id"] for node in arr(arr(under["children"])[0]["children"])] == [branch[2]]

    async def test_move_leaves_the_name_alone_and_a_rename_leaves_the_parent_alone(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """Only the fields present in the request are touched."""
        _, headers = await owner(screener_session, "b1u030@example.com")
        root = await make(api, headers, "Root")
        other = await make(api, headers, "Other")
        child = await make(api, headers, "Original", parent_id=root)

        moved = body_of(await patch(api, headers, child, {"parent_id": other}))
        assert moved["name"] == "Original"

        renamed = body_of(await patch(api, headers, child, {"name": "Renamed"}))
        assert renamed["name"] == "Renamed"
        assert renamed["parent_id"] == other

    async def test_a_patch_with_no_fields_changes_nothing(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, headers = await owner(screener_session, "b1u031@example.com")
        root = await make(api, headers, "Root")
        child = await make(api, headers, "Child", parent_id=root)

        unchanged = body_of(await patch(api, headers, child, {}))
        assert unchanged["name"] == "Child"
        assert unchanged["parent_id"] == root

    async def test_move_can_re_attribute_the_broker_account(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        user_id, headers = await owner(screener_session, "b1u032@example.com")
        account = await second_broker_account(screener_session, user_id, "upstox")
        portfolio = await make(api, headers, "Book")

        attributed = body_of(await patch(api, headers, portfolio, {"broker_account_id": account}))
        assert attributed["broker_account_id"] == account

        spanning = body_of(await patch(api, headers, portfolio, {"broker_account_id": None}))
        assert spanning["broker_account_id"] is None

    async def test_move_under_another_users_portfolio_is_not_found(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, alice = await owner(screener_session, "b1u033@example.com")
        _, bob = await owner(screener_session, "b1u034@example.com")
        hers = await make(api, alice, "Hers")
        his = await make(api, bob, "His")

        refused = await patch(api, bob, his, {"parent_id": hers})
        assert_problem(refused, 404, "not-found")

        still_his = body_of(await api.get(url(f"/portfolios/{his}"), headers=bob))
        assert still_his["parent_id"] is None


# ---------------------------------------------------------------------------
# The per-broker roll-up
# ---------------------------------------------------------------------------


class TestBrokerRollup:
    async def test_a_subtree_is_split_by_broker_account_with_a_total(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        user_id, headers = await owner(screener_session, "b1u035@example.com")
        upstox = await second_broker_account(screener_session, user_id, "upstox")

        top = await make(api, headers, "Everything")
        await make(
            api,
            headers,
            "At the default broker",
            parent_id=top,
            holdings=[{"symbol": "CUPID", "quantity": "10", "avg_price": "100"}],
        )
        await make(
            api,
            headers,
            "At upstox",
            parent_id=top,
            broker_account_id=upstox,
            holdings=[{"symbol": "HFCL", "quantity": "20", "avg_price": "50"}],
        )

        body = body_of(await rollup(api, headers, top))
        lines = arr(body["by_broker"])
        assert len(lines) == 2, lines
        assert upstox in [line["broker_account_id"] for line in lines]

        by_account = {num(line["broker_account_id"]): obj(line["totals"]) for line in lines}
        assert dec(by_account[upstox]["quantity"]) == Decimal("20")
        assert dec(by_account[upstox]["cost"]) == Decimal("1000")
        assert dec(obj(body["total"])["quantity"]) == Decimal("30")
        assert dec(obj(body["total"])["cost"]) == Decimal("2000")
        assert body["spans_brokers"] is True

    async def test_the_total_is_exactly_the_sum_of_the_lines(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """To the last digit. Rounding each line independently is what breaks this."""
        user_id, headers = await owner(screener_session, "b1u036@example.com")
        upstox = await second_broker_account(screener_session, user_id, "upstox")

        top = await make(api, headers, "Everything")
        await make(
            api,
            headers,
            "One",
            parent_id=top,
            holdings=[{"symbol": "CUPID", "quantity": "3", "avg_price": "284.5567"}],
        )
        await make(
            api,
            headers,
            "Two",
            parent_id=top,
            broker_account_id=upstox,
            holdings=[{"symbol": "HFCL", "quantity": "7", "avg_price": "89.1234"}],
        )

        body = body_of(await rollup(api, headers, top))
        lines = [obj(line["totals"]) for line in arr(body["by_broker"])]
        unattributed = obj(body["unattributed"])
        total = obj(body["total"])

        assert sum((dec(line["cost"]) for line in lines), Decimal(0)) + dec(
            unattributed["cost"]
        ) == dec(total["cost"])
        assert sum((dec(line["quantity"]) for line in lines), Decimal(0)) + dec(
            unattributed["quantity"]
        ) == dec(total["quantity"])
        assert sum(num(line["holdings"]) for line in lines) + num(unattributed["holdings"]) == num(
            total["holdings"]
        )

    async def test_a_siblings_money_is_not_in_the_subtree(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, headers = await owner(screener_session, "b1u037@example.com")
        top = await make(api, headers, "Top")
        mine = await make(
            api,
            headers,
            "Mine",
            parent_id=top,
            holdings=[{"symbol": "CUPID", "quantity": "10", "avg_price": "100"}],
        )
        await make(
            api,
            headers,
            "Sibling",
            parent_id=top,
            holdings=[{"symbol": "HFCL", "quantity": "99", "avg_price": "100"}],
        )

        body = body_of(await rollup(api, headers, mine))
        assert dec(obj(body["total"])["quantity"]) == Decimal("10")
        assert [row["symbol"] for row in arr(body["rows"])] == ["CUPID"]

    async def test_a_holding_with_no_quantity_counts_as_a_row_and_not_as_money(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """Both columns are nullable — an import may carry symbols and nothing else.

        Zero money with a non-zero row count is the honest reading; dropping the row would make
        a position disappear from a list because its quantity was never filled in.
        """
        _, headers = await owner(screener_session, "b1u038@example.com")
        portfolio = await make(api, headers, "Symbols only", holdings=[{"symbol": "CUPID"}])

        body = body_of(await rollup(api, headers, portfolio))
        total = obj(body["total"])
        assert num(total["holdings"]) == 1
        assert dec(total["quantity"]) == Decimal(0)
        assert dec(total["cost"]) == Decimal(0)

    async def test_the_roll_up_names_the_broker_but_only_the_callers_own(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        user_id, headers = await owner(screener_session, "b1u039@example.com")
        upstox = await second_broker_account(screener_session, user_id, "upstox")
        portfolio = await make(
            api,
            headers,
            "Upstox book",
            broker_account_id=upstox,
            holdings=[{"symbol": "CUPID", "quantity": "1", "avg_price": "10"}],
        )

        body = body_of(await rollup(api, headers, portfolio))
        line = arr(body["by_broker"])[0]
        assert line["broker_account_id"] == upstox
        assert line["broker_id"] == "upstox"
        assert line["label"] == "upstox"
        assert body["declared_broker_account_id"] == upstox
        assert body["declaration_conflicts"] is False
        assert body["spans_brokers"] is False

    async def test_a_declared_account_that_disagrees_with_the_money_is_reported(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The root claims one account; a child holds elsewhere. The conflict is not hidden."""
        user_id, headers = await owner(screener_session, "b1u040@example.com")
        upstox = await second_broker_account(screener_session, user_id, "upstox")

        top = await make(api, headers, "Claims upstox", broker_account_id=upstox)
        await make(
            api,
            headers,
            "Holds at the default",
            parent_id=top,
            holdings=[{"symbol": "CUPID", "quantity": "5", "avg_price": "10"}],
        )

        body = body_of(await rollup(api, headers, top))
        assert body["declared_broker_account_id"] == upstox
        assert body["declaration_conflicts"] is True

    async def test_another_users_roll_up_is_not_found(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, alice = await owner(screener_session, "b1u041@example.com")
        _, bob = await owner(screener_session, "b1u042@example.com")
        hers = await make(
            api,
            alice,
            "Hers",
            holdings=[{"symbol": "CUPID", "quantity": "10", "avg_price": "100"}],
        )
        assert_problem(
            await api.get(url(f"/portfolios/{hers}/holdings"), headers=bob), 404, "not-found"
        )

    async def test_the_subtree_ids_say_what_was_summed(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """Root first, breadth-first — a caller can check the total against the rows itself."""
        _, headers = await owner(screener_session, "b1u043@example.com")
        ids = await chain(api, headers, 3, prefix="R")

        body = body_of(await rollup(api, headers, ids[0]))
        summed = body["subtree_portfolio_ids"]
        assert isinstance(summed, list), summed
        assert [num(value) for value in summed] == ids


# ---------------------------------------------------------------------------
# Broker attribution on the write path (``replace_holdings``)
# ---------------------------------------------------------------------------


class TestTheWriterAttributesTheBrokerAccount:
    async def test_a_written_holding_names_the_portfolios_declared_account(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        user_id, headers = await owner(screener_session, "b1u044@example.com")
        upstox = await second_broker_account(screener_session, user_id, "upstox")
        portfolio = await make(
            api,
            headers,
            "Upstox book",
            broker_account_id=upstox,
            holdings=[{"symbol": "CUPID", "quantity": "5", "avg_price": "10"}],
        )

        rows = await stored_holdings(screener_session, portfolio)
        assert [row[1] for row in rows] == [upstox]

    async def test_the_writer_attributes_it_with_the_trigger_disabled(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The point of the fix: the application names the account, the trigger is a backstop.

        Migration 0019 installed ``portfolio_holding_attribute_broker_account`` so that *no*
        writer could produce an unattributed row. With the trigger switched off inside this
        transaction, a write that still lands attributed proves the router is not leaning on it.
        """
        _, headers = await owner(screener_session, "b1u045@example.com")
        await screener_session.execute(
            text(
                "ALTER TABLE portfolio_holding "
                "DISABLE TRIGGER portfolio_holding_attribute_broker_account"
            )
        )
        portfolio = await make(
            api,
            headers,
            "No trigger",
            holdings=[{"symbol": "CUPID", "quantity": "5", "avg_price": "10"}],
        )

        rows = await stored_holdings(screener_session, portfolio)
        assert len(rows) == 1
        assert rows[0][1] is not None

        owned = await screener_session.scalar(
            select(BrokerAccount.id).where(BrokerAccount.id == rows[0][1])
        )
        assert owned is not None

    async def test_replacing_collapses_a_name_held_at_two_brokers_to_one_row(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """PUT is a replacement, and the body has no broker column: one row per instrument.

        The two rows are planted directly, because they are exactly the shape the old
        two-column primary key could not express and no API route creates yet.
        """
        user_id, headers = await owner(screener_session, "b1u046@example.com")
        upstox = await second_broker_account(screener_session, user_id, "upstox")
        portfolio = await make(
            api,
            headers,
            "Two brokers",
            holdings=[{"symbol": "CUPID", "quantity": "10", "avg_price": "100"}],
        )
        cupid = await instrument_id(screener_session, "CUPID")
        screener_session.add(
            PortfolioHolding(
                portfolio_id=portfolio,
                instrument_id=cupid,
                broker_account_id=upstox,
                quantity=Decimal("30"),
                avg_price=Decimal("200"),
                added_on=dt.date(2026, 1, 2),
            )
        )
        await screener_session.flush()
        assert len(await stored_holdings(screener_session, portfolio)) == 2

        replaced = await api.put(
            url(f"/portfolios/{portfolio}/holdings"),
            json={"holdings": [{"symbol": "CUPID", "quantity": "7", "avg_price": "111"}]},
            headers=headers,
        )
        assert replaced.status_code == 200, replaced.text
        assert obj(body_of(replaced)["report"])["imported"] == 1

        rows = await stored_holdings(screener_session, portfolio)
        assert len(rows) == 1, rows
        assert rows[0][2] == Decimal("7.0000")

    async def test_replacing_with_an_empty_list_clears_every_broker_line(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """An empty ``holdings`` array is a request to hold nothing, at every account.

        The delete is one statement with an ``IN`` over the names to keep; an empty set is the
        edge where a hand-written ``NOT IN ()`` quietly matches nothing and leaves the portfolio
        full.
        """
        user_id, headers = await owner(screener_session, "b1u061@example.com")
        upstox = await second_broker_account(screener_session, user_id, "upstox")
        portfolio = await make(
            api,
            headers,
            "Everything",
            holdings=[{"symbol": "CUPID", "quantity": "10", "avg_price": "100"}],
        )
        cupid = await instrument_id(screener_session, "CUPID")
        screener_session.add(
            PortfolioHolding(
                portfolio_id=portfolio,
                instrument_id=cupid,
                broker_account_id=upstox,
                quantity=Decimal("30"),
                avg_price=Decimal("200"),
                added_on=dt.date(2026, 1, 2),
            )
        )
        await screener_session.flush()

        cleared = await api.put(
            url(f"/portfolios/{portfolio}/holdings"), json={"holdings": []}, headers=headers
        )
        assert cleared.status_code == 200, cleared.text
        assert await stored_holdings(screener_session, portfolio) == []

        body = body_of(await rollup(api, headers, portfolio))
        assert arr(body["by_broker"]) == []
        assert num(obj(body["total"])["holdings"]) == 0

    async def test_a_retry_replays_even_after_the_parent_was_deleted(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The idempotent replay is answered before the parent is validated.

        The first attempt already created the portfolio; answering the retry with 404 because a
        parent has since gone would be the worse lie.
        """
        _, base = await owner(screener_session, "b1u062@example.com")
        parent = await make(api, base, "Parent")
        # The key goes on the child's request only: one `Idempotency-Key` names one created
        # resource, so re-using it for the parent would make the child's POST replay the parent.
        headers = {**base, "Idempotency-Key": "b1-tree-replay-1"}
        first = await create(api, headers, "Child", parent_id=parent)
        child = num(obj(body_of(first)["portfolio"])["id"])

        assert (await api.delete(url(f"/portfolios/{parent}"), headers=base)).status_code == 204

        retry = await api.post(
            url("/portfolios"),
            json={"name": "Child", "holdings": [], "parent_id": parent},
            headers=headers,
        )
        assert retry.status_code == 201, retry.text
        assert obj(body_of(retry)["portfolio"])["id"] == child

    async def test_replacing_keeps_the_earliest_added_on(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The rule migration 0019's ``downgrade()`` uses when it merges rows, so the two agree."""
        user_id, headers = await owner(screener_session, "b1u047@example.com")
        upstox = await second_broker_account(screener_session, user_id, "upstox")
        portfolio = await make(api, headers, "Dates", broker_account_id=upstox)
        cupid = await instrument_id(screener_session, "CUPID")
        default_account = await screener_session.scalar(
            select(BrokerAccount.id).where(
                BrokerAccount.user_id == user_id, BrokerAccount.broker_id == "zerodha"
            )
        )
        if default_account is None:
            default_account = await second_broker_account(screener_session, user_id, "zerodha")
        for account, day in ((upstox, dt.date(2026, 3, 3)), (default_account, dt.date(2026, 1, 1))):
            screener_session.add(
                PortfolioHolding(
                    portfolio_id=portfolio,
                    instrument_id=cupid,
                    broker_account_id=account,
                    quantity=Decimal("1"),
                    avg_price=Decimal("1"),
                    added_on=day,
                )
            )
        await screener_session.flush()

        replaced = await api.put(
            url(f"/portfolios/{portfolio}/holdings"),
            json={"holdings": [{"symbol": "CUPID", "quantity": "2", "avg_price": "2"}]},
            headers=headers,
        )
        assert replaced.status_code == 200, replaced.text

        rows = await stored_holdings(screener_session, portfolio)
        assert len(rows) == 1, rows
        assert rows[0][3] == dt.date(2026, 1, 1)


# ---------------------------------------------------------------------------
# Deleting a grouping node
# ---------------------------------------------------------------------------


class TestDelete:
    async def test_delete_promotes_the_children_to_roots(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """``ON DELETE SET NULL``: removing a grouping node must not remove the money under it."""
        _, headers = await owner(screener_session, "b1u048@example.com")
        root = await make(api, headers, "Root")
        child = await make(
            api,
            headers,
            "Child",
            parent_id=root,
            holdings=[{"symbol": "CUPID", "quantity": "10", "avg_price": "100"}],
        )

        assert (await api.delete(url(f"/portfolios/{root}"), headers=headers)).status_code == 204

        body = await listing(api, headers)
        assert [node["id"] for node in arr(body["data"])] == [child]
        assert arr(body["orphans"]) == []
        assert body_of(await api.get(url(f"/portfolios/{child}"), headers=headers))["depth"] == 1

    async def test_delete_does_not_cascade_the_childs_holdings(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, headers = await owner(screener_session, "b1u049@example.com")
        root = await make(api, headers, "Root")
        child = await make(
            api,
            headers,
            "Child",
            parent_id=root,
            holdings=[{"symbol": "CUPID", "quantity": "10", "avg_price": "100"}],
        )
        await api.delete(url(f"/portfolios/{root}"), headers=headers)

        rows = await stored_holdings(screener_session, child)
        assert len(rows) == 1
        assert rows[0][2] == Decimal("10.0000")

    async def test_delete_removes_only_its_own_holdings(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, headers = await owner(screener_session, "b1u050@example.com")
        root = await make(
            api,
            headers,
            "Root",
            holdings=[{"symbol": "HFCL", "quantity": "1", "avg_price": "1"}],
        )
        await api.delete(url(f"/portfolios/{root}"), headers=headers)
        assert await stored_holdings(screener_session, root) == []

    async def test_a_promoted_child_never_lands_in_another_users_view(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, alice = await owner(screener_session, "b1u051@example.com")
        _, bob = await owner(screener_session, "b1u052@example.com")
        root = await make(api, alice, "Root")
        child = await make(api, alice, "Child", parent_id=root)
        await api.delete(url(f"/portfolios/{root}"), headers=alice)

        his = await listing(api, bob)
        assert flatten(arr(his["data"])) == []
        assert arr(his["orphans"]) == []
        assert_problem(await api.get(url(f"/portfolios/{child}"), headers=bob), 404, "not-found")


# ---------------------------------------------------------------------------
# Tenant isolation, gathered
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    async def test_another_user_cannot_read_move_or_delete_a_portfolio(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, alice = await owner(screener_session, "b1u053@example.com")
        _, bob = await owner(screener_session, "b1u054@example.com")
        hers = await make(api, alice, "Hers", holdings=[{"symbol": "CUPID"}])

        assert_problem(await api.get(url(f"/portfolios/{hers}"), headers=bob), 404, "not-found")
        assert_problem(await patch(api, bob, hers, {"name": "Mine now"}), 404, "not-found")
        assert_problem(await patch(api, bob, hers, {"parent_id": None}), 404, "not-found")
        assert_problem(await api.delete(url(f"/portfolios/{hers}"), headers=bob), 404, "not-found")
        assert_problem(
            await api.get(url(f"/portfolios/{hers}/holdings"), headers=bob), 404, "not-found"
        )

    async def test_the_refused_writes_left_her_portfolio_untouched(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, alice = await owner(screener_session, "b1u055@example.com")
        _, bob = await owner(screener_session, "b1u056@example.com")
        hers = await make(api, alice, "Hers", holdings=[{"symbol": "CUPID"}])

        await patch(api, bob, hers, {"name": "Mine now"})
        await api.delete(url(f"/portfolios/{hers}"), headers=bob)

        still = body_of(await api.get(url(f"/portfolios/{hers}"), headers=alice))
        assert still["name"] == "Hers"
        assert len(arr(still["holdings"])) == 1

    async def test_one_users_tree_never_appears_in_anothers_listing(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, alice = await owner(screener_session, "b1u057@example.com")
        _, bob = await owner(screener_session, "b1u058@example.com")
        hers = await chain(api, alice, 3, prefix="A")
        his = await make(api, bob, "His")

        assert flatten(arr((await listing(api, bob))["data"])) == [his]
        assert flatten(arr((await listing(api, alice))["data"])) == hers

    async def test_another_users_money_is_never_in_a_roll_up(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, alice = await owner(screener_session, "b1u059@example.com")
        _, bob = await owner(screener_session, "b1u060@example.com")
        await make(
            api,
            alice,
            "Hers",
            holdings=[{"symbol": "CUPID", "quantity": "1000", "avg_price": "100"}],
        )
        his = await make(
            api, bob, "His", holdings=[{"symbol": "HFCL", "quantity": "1", "avg_price": "1"}]
        )

        body = body_of(await rollup(api, bob, his))
        assert dec(obj(body["total"])["quantity"]) == Decimal("1")
        assert [row["symbol"] for row in arr(body["rows"])] == ["HFCL"]


# ---------------------------------------------------------------------------
# The line this tree may not cross
# ---------------------------------------------------------------------------


class TestNoOrderPath:
    def test_the_router_names_no_order_path(self) -> None:
        """Non-negotiable #1 and the second law, asserted over the source.

        This router records a book. It never places anything, and it never previews an order
        either: the roll-up sums what is stored and receives no quote.
        """
        source = inspect.getsource(router)
        for forbidden in (
            "place_order",
            "OrderGateway",
            "baskfy_execution",
            "kiteconnect",
            "confirm=True",
        ):
            assert forbidden not in source, f"routers/portfolios.py references {forbidden}"

    def test_the_router_declares_no_execute_route(self) -> None:
        source = inspect.getsource(router)
        assert '"/execute"' not in source
        assert "/execute" not in source
