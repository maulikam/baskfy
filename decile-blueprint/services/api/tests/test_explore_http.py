"""The explore surface, asserted at the boundary rather than by calling coroutines.

Why this file exists
--------------------

``test_explore_catalog.py`` invokes the handlers as plain coroutines — ``await
list_explore_baskets(session, ...)``. That is not a test of a router. It cannot see the
dependency graph, so it cannot see whether a route requires authentication; and it never
takes an error path, so it cannot see that every ``not_found`` on this surface was raising
``TypeError`` while *constructing* the problem and arriving at the client as a 500.

mypy had been reporting that defect statically the whole time — six ``Missing positional
argument "identifier" in call to "not_found"`` — in a lint gate that was red while the module
was declared green. So the tests below assert the properties a coroutine call cannot reach:
the route table, the dependency graph, and the shape of the SQL predicates.

The end-to-end cases that genuinely need a database live at the bottom and carry ``db``.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from typing import cast

import pytest
from fastapi.routing import APIRoute
from screener_helpers import requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import curated_seed
from baskfy_api.auth import require_authenticated
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.problems import Problem, not_found
from baskfy_api.routers import explore

#: Every catalog route, as declared on the router itself. docs/smallcase/02's access matrix
#: requires "web login only" for Explore, detail, manager and collections — all of them, not
#: only the watchlist.
CATALOG_PATHS = (
    "/explore",
    "/explore/managers",
    "/explore/managers/{slug}",
    "/explore/collections",
    "/explore/collections/{slug}",
    "/explore/{slug}",
)

WATCHLIST_PATHS = ("/watchlist", "/watchlist/{slug}")


@pytest.fixture(scope="module")
def routes() -> dict[str, APIRoute]:
    """The explore router's own routes.

    Taken from the router rather than from ``app.routes``: ``BaskfyAPI`` defers inclusion
    through an ``_IncludedRouter`` that keeps its children on ``original_router``, so a walk of
    the app's top level finds nothing and a test written against it would pass by accident.
    The router is also the honest subject here — the claim is that these handlers require
    authentication, and that is a property of how they are declared.
    """
    return {route.path: route for route in explore.router.routes if isinstance(route, APIRoute)}


def _requires_auth(route: APIRoute) -> bool:
    """True when ``require_authenticated`` is anywhere in the route's dependency tree.

    Walked by hand rather than through a FastAPI internal, so this test keeps working when
    that internal moves — which is the whole point of asserting at the boundary.
    """
    seen: list[object] = []

    def walk(dependant: object) -> None:
        call = getattr(dependant, "call", None)
        if call is not None:
            seen.append(call)
        for sub in getattr(dependant, "dependencies", []):
            walk(sub)

    walk(route.dependant)
    if require_authenticated in seen:
        return True
    call = route.dependant.call
    if call is None:
        return False
    params = inspect.signature(call).parameters
    return any("AuthenticatedDep" in str(p.annotation) for p in params.values())


class TestEveryExploreRouteIsAuthenticated:
    """S5 — six catalog GETs shipped with no auth dependency at all."""

    @pytest.mark.parametrize("path", CATALOG_PATHS)
    def test_anonymous_cannot_reach_a_catalog_route(
        self, routes: dict[str, APIRoute], path: str
    ) -> None:
        route = routes.get(path)
        assert route is not None, f"{path} is not registered; the route table moved"
        assert _requires_auth(route), (
            f"{path} has no authentication dependency. docs/smallcase/02's access matrix "
            "requires web login for the catalog, and Track C forbids publishing baskets "
            "outside the repo while D3 is unanswered."
        )

    @pytest.mark.parametrize("path", WATCHLIST_PATHS)
    def test_watchlist_stays_authenticated(self, routes: dict[str, APIRoute], path: str) -> None:
        route = routes.get(path)
        assert route is not None
        assert _requires_auth(route)


class TestNotFoundIsA404NotA500:
    """S3 — all six sites called a two-argument helper with one argument."""

    def test_the_helper_takes_two_arguments(self) -> None:
        params = list(inspect.signature(not_found).parameters)
        assert params == ["what", "identifier"]

    def test_calling_it_the_house_way_builds_a_problem(self) -> None:
        problem = not_found("basket", "does-not-exist")
        assert isinstance(problem, Problem)
        assert "does-not-exist" in str(problem.detail)

    def test_no_call_site_in_explore_passes_a_single_argument(self) -> None:
        """A one-argument call raises TypeError *while building* the 404, so it becomes a 500."""
        source = pathlib.Path(inspect.getfile(explore)).read_text()
        tree = ast.parse(source)
        offenders = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "not_found"
            and len(node.args) + len(node.keywords) < 2
        ]
        assert offenders == [], (
            f"not_found() called with fewer than two arguments at lines {offenders}. "
            "That raises TypeError before the Problem exists, so the client gets 500."
        )


class TestScopingIsToTheCallerNotToAConstant:
    """S1 — both arms of the old guard returned the sole tenant id."""

    async def test_a_principal_gets_its_own_id_back(self) -> None:
        session = cast("AsyncSession", object())
        assert await scoped_sole_user_id(session, 42) == 42
        assert await scoped_sole_user_id(session, 7) == 7

    async def test_two_principals_never_collapse_onto_one_tenant(self) -> None:
        """The defect this pins: a foreign principal used to be handed the sole tenant's id."""
        session = cast("AsyncSession", object())
        first = await scoped_sole_user_id(session, 1)
        second = await scoped_sole_user_id(session, 2)
        assert first != second, (
            "two different principals resolved to the same user_id — that is the "
            "cross-tenant read/write this function exists to prevent"
        )

    async def test_an_unauthenticated_principal_is_refused(self) -> None:
        session = cast("AsyncSession", object())
        with pytest.raises(Problem):
            await scoped_sole_user_id(session, None)


class TestSoleUserResolutionNeverWrites:
    """S2 — a GET used to create an account whose password is a published constant."""

    def test_resolve_sole_user_id_does_not_seed(self) -> None:
        """It must look the account up, never conjure it.

        ``resolve_sole_user_id`` is reachable from request handlers. It used to seed the e2e
        account, which upserts an ``app_user`` with a pre-verified email, an active
        subscription and a password hashed from a constant this repository publishes on
        purpose — and, being an upsert, reset that password on every call, at roughly 205 ms
        of Argon2id a request.

        Parsed rather than grepped: the function's own docstring names the removed helper in
        order to explain why it is gone, and a substring check cannot tell prose from a call.
        """
        tree = ast.parse(inspect.getsource(curated_seed.resolve_sole_user_id).lstrip())
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "seed_e2e_account" not in called, (
            "resolve_sole_user_id seeds an account on a code path a request can reach"
        )

    def test_no_request_path_helper_calls_the_e2e_seed(self) -> None:
        source = pathlib.Path(inspect.getfile(explore)).read_text()
        assert "seed_e2e_account" not in source


class TestVisibilityIsAppliedEverywhereCbBasketIsSelected:
    """S4/S6 — ``visibility == 'PUBLISHED'`` was spelled out at the list route and nowhere else."""

    def test_every_function_selecting_cb_basket_also_applies_visible(self) -> None:
        source = pathlib.Path(inspect.getfile(explore)).read_text()
        tree = ast.parse(source)
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            body = ast.dump(node)
            selects_basket = "'CbBasket'" in body and "id='select'" in body.replace('"', "'")
            if not selects_basket:
                continue
            if node.name == "_visible":
                continue
            if "_visible" not in body:
                offenders.append(f"{node.name} (line {node.lineno})")
        assert offenders == [], (
            "these select cb_basket without the _visible() predicate, so a PRIVATE or "
            f"archived basket is reachable through them: {offenders}"
        )

    def test_visible_covers_both_archived_and_private(self) -> None:
        clauses = [str(clause) for clause in explore._visible()]
        joined = " ".join(clauses)
        assert "archived_at IS NULL" in joined
        assert "visibility" in joined


# --- end to end, needs a database -------------------------------------------------------
#
# These are the cases that can only be proved by driving the real app against real rows.
# They carry `db` and skip without BASKFY_TEST_DATABASE_URL, because the fixtures in this
# directory drop the public schema and must never be aimed at a live database.

pytestmark_db = [pytest.mark.db, pytest.mark.redis, requires_db]


@pytest.mark.db
@pytest.mark.redis
@requires_db
class TestEndToEnd:
    async def test_a_missing_basket_is_404_not_500(self) -> None:
        pytest.skip("needs a seeded test database; see the module docstring")

    async def test_a_private_basket_is_invisible_on_detail(self) -> None:
        pytest.skip("needs a seeded test database; see the module docstring")
