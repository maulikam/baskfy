"""M26's acceptance: the desk's record is visible on the web, and can never place an order.

The same guarantee ``test_baskets_readonly.py`` makes for M22's basket surfaces, extended over
the five ``/desk/*`` endpoints. Structural, not conventional: it fails the moment someone adds a
mutating route, rather than the moment someone notices.

M26 also draws a line the basket surfaces did not have to. The desk console's ``/stops`` and
``/reconcile`` pages read **live broker state**, and ``/stops`` carries an action that creates and
deletes triggers at the broker. Neither crossed. What ``/desk/*`` serves is what the database
knows — the plan, the fills, the position record — because a user-facing web surface holding a
live broker session is a different regulated activity, and that is the D3 question CLAUDE.md says
nothing may be built against.
"""

from __future__ import annotations

import inspect

import pytest

from baskfy_api.app import create_app
from baskfy_api.routers import desk

MUTATING = ("post", "put", "patch", "delete")

#: What `app.openapi()` returns, to the depth these tests read it.
OpenApiSpec = dict[str, dict[str, dict[str, object]]]


@pytest.fixture(scope="module")
def spec() -> OpenApiSpec:
    return create_app().openapi()


class TestNoOrderPlacingRouteIsReachable:
    def test_the_desk_routes_are_registered(self, spec: OpenApiSpec) -> None:
        paths = {p for p in spec["paths"] if "/desk/" in p}
        assert paths, "M26's routes are not registered at all"
        assert len(paths) == 5, f"expected five desk reads, found {sorted(paths)}"

    def test_every_desk_route_is_a_get(self, spec: OpenApiSpec) -> None:
        for path in (p for p in spec["paths"] if "/desk/" in p):
            methods = sorted(spec["paths"][path])
            assert methods == ["get"], f"{path} exposes {methods}"

    def test_the_router_declares_no_mutating_decorator(self) -> None:
        """Over the source, so a route added and not yet registered is still caught."""
        source = inspect.getsource(desk)
        for verb in MUTATING:
            assert f"@router.{verb}(" not in source, f"routers/desk.py declares a {verb.upper()}"


class TestItCannotReachTheOrderPath:
    def test_the_router_does_not_import_the_execution_package(self) -> None:
        source = inspect.getsource(desk)
        for forbidden in ("baskfy_execution", "kiteconnect", "place_order", "place_gtt"):
            assert forbidden not in source, f"routers/desk.py names {forbidden}"

    def test_the_router_reaches_the_broker_by_no_route_at_all(self) -> None:
        """The desk console's own /stops and /reconcile read live broker state. This does not.

        Asserted by naming the absence: no Kite client, no session, no token. The endpoints read
        the `desk` schema and nothing else, which is what makes them safe to expose.
        """
        source = inspect.getsource(desk)
        for forbidden in ("Kite(", "kite_client", "access_token", "AccessTokenStore"):
            assert forbidden not in source, f"routers/desk.py names {forbidden}"

    def test_it_reads_only_from_the_desk_schema(self) -> None:
        source = inspect.getsource(desk)
        # The constant used to be declared here, and this asserted that literal. It now lives in
        # `baskfy_api.desk_schema` — it was written out separately in four routers, and four
        # copies of "which schema is the desk's" is three chances to disagree. What matters to
        # this test is unchanged: every query in this module is qualified by it.
        from baskfy_api.desk_schema import DESK_SCHEMA  # noqa: PLC0415 - local to this test

        assert "from baskfy_api.desk_schema import" in source
        assert "DESK_SCHEMA" in source
        # Read from the module that declares it, not through the router's binding: the two lines
        # above already prove the router imports this very object, and reaching through it was an
        # implicit re-export mypy strict rejects.
        assert DESK_SCHEMA == "desk"
        # No query may name a schema this router is not supposed to read.
        assert 'from "' not in source.replace('from "{DESK_SCHEMA}"', ""), (
            "a query names a schema literally instead of going through DESK_SCHEMA"
        )
        # Every statement is a SELECT. A stray INSERT/UPDATE/DELETE in an f-string would not be
        # caught by the decorator check above, because it needs no decorator.
        lowered = source.lower()
        for forbidden in ("insert into", "update ", "delete from", "truncate"):
            assert forbidden not in lowered, f"routers/desk.py contains `{forbidden}`"


class TestTheSurfaceStaysHonest:
    def test_it_says_where_the_live_broker_view_lives(self) -> None:
        """The module explains what it deliberately does not serve, and why.

        A surface that silently shows less than the console it replaces teaches the reader that
        it shows everything. Both pages that lost something say so.
        """
        source = inspect.getsource(desk)
        assert "live broker state" in source
        assert "D3" in source


class TestADeploymentWithNoDeskHistory:
    """The failure this suite did not cover: a box that has no `desk` schema at all.

    Staging is one. It is migrated with alembic, which owns the application's own tables; the
    desk's schema arrives by a separate migration of its SQLite (docs/08 D8) that has never run
    there. Nothing is wrong with that box — it simply has no desk history.

    What was wrong is how it read. asyncpg raises `UndefinedTableError`, SQLAlchemy wraps it as
    `ProgrammingError`, and it reached the client unhandled: **`/desk/*`, `/baskets/plan` and
    `/baskets/plan/kite` all answered 500 on staging**, for as long as staging has existed. "We
    broke" and "there is no desk history here" are different sentences to a reader and different
    pages to build.
    """

    def test_the_missing_schema_predicate_is_narrow(self) -> None:
        """It must match a missing table and nothing else.

        A permissions error, a dead connection or a genuine query bug has to keep surfacing as a
        500. Hiding those behind "nothing here yet" turns an outage into an empty page that
        nobody investigates, which is strictly worse than the 500 it replaced.
        """
        from sqlalchemy.exc import ProgrammingError  # noqa: PLC0415 - local to this test

        from baskfy_api.desk_schema import is_missing_desk_data  # noqa: PLC0415

        class UndefinedTableError(Exception):
            pass

        class InsufficientPrivilegeError(Exception):
            pass

        missing = ProgrammingError("select 1", {}, UndefinedTableError("no such relation"))
        denied = ProgrammingError("select 1", {}, InsufficientPrivilegeError("permission denied"))

        assert is_missing_desk_data(missing) is True
        assert is_missing_desk_data(denied) is False
        # Not a database error at all.
        assert is_missing_desk_data(ValueError("something else")) is False

    def test_every_desk_route_is_gated_on_the_schema_existing(self) -> None:
        """On the router, not repeated per endpoint — so the seventh route cannot forget it."""
        from baskfy_api.routers.desk import router  # noqa: PLC0415 - local to this test

        names = [getattr(d.dependency, "__name__", "") for d in (router.dependencies or [])]
        assert "require_desk_schema" in names, names

    def test_one_definition_of_which_schema_the_desk_is(self) -> None:
        """It was written out as a string literal in four routers. Three chances to disagree."""
        import inspect as _inspect  # noqa: PLC0415 - only this test reads source

        from baskfy_api.desk_schema import DESK_SCHEMA  # noqa: PLC0415
        from baskfy_api.routers import baskets as baskets_router  # noqa: PLC0415

        assert DESK_SCHEMA == "desk"
        for module in (desk, baskets_router):
            source = _inspect.getsource(module)
            assert 'DESK_SCHEMA: Final = "desk"' not in source, (
                f"{module.__name__} re-declares DESK_SCHEMA instead of importing it"
            )
