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

import httpx
import pytest
from api_helpers import assert_problem, bearer, make_user, url
from fastapi.routing import APIRoute
from screener_helpers import requires_db
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import curated_seed, curated_tenant
from baskfy_api.auth import require_authenticated
from baskfy_api.curated_seed import seed_curated_managers
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.problems import Problem, not_found
from baskfy_api.routers import explore
from baskfy_core.curated_baskets import MANAGER_SLUG_BASKFY_ENGINE, SOLE_USER_ENV
from baskfy_core.models import AppUser, CbBasket, CbManager

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


class TestScopingRefusesAForeignPrincipal:
    """S1 — both arms of the old guard returned the sole tenant id.

    Fixed upstream by M43.4, which chose to *refuse* a foreign principal where this branch had
    chosen to scope to the caller. Both close the hole; M43.4 landed first and reads Law 2's
    "the gateway refuses a mismatch" literally, so these assertions follow it rather than the
    version this branch originally carried.
    """

    async def test_the_sole_tenant_is_allowed_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _resolve(_session: object) -> int:
            return 43

        monkeypatch.setattr(curated_tenant, "resolve_sole_user_id", _resolve)
        session = cast("AsyncSession", object())
        assert await scoped_sole_user_id(session, 43) == 43

    async def test_a_foreign_principal_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The defect: a registered account used to be promoted to the operator."""

        async def _resolve(_session: object) -> int:
            return 43

        monkeypatch.setattr(curated_tenant, "resolve_sole_user_id", _resolve)
        session = cast("AsyncSession", object())
        with pytest.raises(Problem):
            await scoped_sole_user_id(session, 44)

    async def test_a_principal_with_no_user_id_is_caught_by_require_user(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one case scoped_sole_user_id does not refuse, and what does refuse it.

        M43.4 guards on ``principal_user_id is not None``, so a principal carrying no user id
        still receives the sole tenant. Nothing reaches it that way, because every handler on
        this router calls ``principal.require_user()`` first — which is the other half of this
        branch. Asserted here so the two halves stay tied together: if the require_user calls
        are ever removed, this records what they were holding.
        """

        async def _resolve(_session: object) -> int:
            return 43

        monkeypatch.setattr(curated_tenant, "resolve_sole_user_id", _resolve)
        session = cast("AsyncSession", object())
        assert await scoped_sole_user_id(session, None) == 43

        source = pathlib.Path(inspect.getfile(explore)).read_text()
        for handler in ("list_watchlist", "add_watchlist", "remove_watchlist"):
            body = inspect.getsource(getattr(explore, handler))
            assert "scoped_sole_user_id" in body
        assert source.count("principal.require_user()") >= 6


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

# --- end to end, over HTTP, against a disposable database ------------------------------
#
# These were skipping until a throwaway database existed: the fixtures in this directory run
# `DROP SCHEMA IF EXISTS public CASCADE`, and the only Postgres reachable when this branch
# started was the live one holding an in-progress factor recompute. `baskfy_fixtest` exists
# now, so they run.


@pytest.mark.db
@pytest.mark.redis
@requires_db
class TestEndToEnd:
    async def test_a_missing_basket_is_404_not_500(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """The defect: not_found() took two arguments and every site passed one.

        The TypeError fired while *constructing* the problem, so the Problem was never raised
        and the catch-all turned it into a 500. Only an HTTP-level request can see that; a
        test that awaits the handler coroutine never takes the error path at all.
        """
        _, public_id = await make_user(screener_session, "explore404@example.com")
        response = await api.get(url("/explore/no-such-basket"), headers=bearer(public_id))
        assert response.status_code != 500, response.text[:300]
        assert_problem(response, 404, "not-found")

    async def test_a_missing_watchlist_delete_is_404_not_500(
        self,
        api: httpx.AsyncClient,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both halves of the fix have to hold for this to be a 404.

        The caller must BE the sole tenant, because M43.4 refuses anyone else; and the sole
        tenant has to already exist, because resolve_sole_user_id no longer conjures it on the
        request path. With both satisfied, a delete of something not watched is a 404 — where
        it used to be a 500 from the not_found arity defect.
        """
        user_id, public_id = await make_user(screener_session, "wldel@example.com")
        monkeypatch.setenv(SOLE_USER_ENV, str(user_id))

        response = await api.delete(url("/watchlist/no-such-basket"), headers=bearer(public_id))
        assert response.status_code != 500, response.text[:300]
        assert response.status_code == 404, response.text[:300]

    async def test_an_unconfigured_sole_tenant_refuses_rather_than_seeding(
        self,
        api: httpx.AsyncClient,
        screener_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """S2: a GET used to CREATE an account whose password is a published constant.

        With no sole tenant configured and none seeded, the request is now refused. The
        assertion that matters is the second one: no app_user appeared as a side effect of a
        read.
        """
        monkeypatch.delenv(SOLE_USER_ENV, raising=False)
        _, public_id = await make_user(screener_session, "noseed@example.com")
        before = (
            await screener_session.execute(select(func.count()).select_from(AppUser))
        ).scalar_one()

        response = await api.get(url("/watchlist"), headers=bearer(public_id))
        assert response.status_code != 200

        after = (
            await screener_session.execute(select(func.count()).select_from(AppUser))
        ).scalar_one()
        assert after == before, "a read created an app_user row"

    async def test_the_catalog_refuses_an_anonymous_caller(self, api: httpx.AsyncClient) -> None:
        """The access matrix in docs/smallcase/02 is "web login only" for the catalog.

        Track C also keeps it off the public web until D3 is answered.
        """
        for path in ("/explore", "/explore/managers", "/explore/collections"):
            response = await api.get(url(path))
            assert response.status_code in {401, 403}, (
                f"{path} answered {response.status_code} to an anonymous caller"
            )

    async def test_a_private_basket_is_invisible_on_detail_and_in_the_list(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """cb_basket has no owner column, so visibility is the only control there is."""
        await seed_curated_managers(screener_session)
        manager_id = (
            await screener_session.execute(
                select(CbManager.id).where(CbManager.slug == MANAGER_SLUG_BASKFY_ENGINE)
            )
        ).scalar_one()
        screener_session.add(
            CbBasket(
                slug="a-private-idea",
                name="A Private Idea",
                manager_id=manager_id,
                type="STOCK",
                access="FREE",
                visibility="PRIVATE",
                categories=[],
                rebalance_frequency="MONTHLY",
                source="MANUAL",
            )
        )
        await screener_session.flush()

        _, public_id = await make_user(screener_session, "private@example.com")
        headers = bearer(public_id)

        detail = await api.get(url("/explore/a-private-idea"), headers=headers)
        assert detail.status_code == 404, (
            f"a PRIVATE basket was readable on the detail route: {detail.status_code}"
        )

        listing = await api.get(url("/explore"), headers=headers)
        if listing.status_code == 200:
            slugs = [row["slug"] for row in listing.json()["items"]]
            assert "a-private-idea" not in slugs
