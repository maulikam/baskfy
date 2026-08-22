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
        assert 'DESK_SCHEMA: Final = "desk"' in source
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
