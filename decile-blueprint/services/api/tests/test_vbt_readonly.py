"""VB8's safety acceptance: `/vbt` shows the sleeve and can never trade it.

`docs/vbt/02-scope-and-gating.md` Track C §4: **"`apps/web` gets no route under `/vbt` that can
reach the gateway."** This is the assertion behind that sentence, extending the pattern
`test_desk_readonly.py` and `test_swing_readonly.py` already set.

Structural rather than conventional: it fails the moment somebody adds a mutating route, not the
moment somebody notices. And it covers the one write the surface does have.

WHY `PATCH /vbt/config` IS ALLOWED AND STILL SAFE
--------------------------------------------------
It writes four numbers into `vb_config`. Three claims make that defensible and each is asserted
below:

1. it is the **only** non-GET route on the surface;
2. its request model cannot name `dry_run_sessions` or `first_live_sessions_left` — the two
   fields that decide whether the sleeve is still on paper and whether it is still starting small
   — so a caller cannot declare the paper run finished by asking, and `extra="forbid"` means they
   are *told* rather than silently ignored;
3. neither the router nor the service it calls names the execution package, a broker, or an
   order, and no `/vbt` path mentions one.

There is a fourth claim this file does not have to make, because there is nothing to check: the
sleeve has **no auto-execute flag anywhere**, and VB10 asserts that over the whole tree. The
desk's non-negotiable #1 exception belongs to the swing sleeve alone.
"""

from __future__ import annotations

import inspect
import typing

import pytest

from baskfy_api import vbt as vbt_service
from baskfy_api import vbt_sleeve
from baskfy_api.app import create_app
from baskfy_api.routers import vbt as vbt_router
from baskfy_api.vbt_settings import SYSTEM_OWNED_FIELDS, VbtConfigPatch

#: The one non-GET route on the surface, and why it is allowed. A route that is not in this table
#: fails `test_every_route_is_a_get_except_the_documented_write` the moment it is added.
DELIBERATE_MUTATING_ROUTES: dict[str, set[str]] = {
    "/api/v1/vbt/config": {"patch"},
}

#: What `app.openapi()` returns, to the depth these tests read it.
OpenApiSpec = dict[str, dict[str, dict[str, object]]]


@pytest.fixture(scope="module")
def spec() -> OpenApiSpec:
    return create_app().openapi()


def _vbt_paths(spec: OpenApiSpec) -> list[str]:
    return sorted(path for path in spec["paths"] if "/vbt" in path)


class TestTheSurfaceIsRegisteredAndReadOnly:
    def test_the_vbt_routes_exist(self, spec: OpenApiSpec) -> None:
        paths = _vbt_paths(spec)
        assert paths, "the vbt routes are not registered at all"
        assert len(paths) == 6, f"expected six vbt paths, found {paths}"

    def test_every_route_is_a_get_except_the_documented_write(self, spec: OpenApiSpec) -> None:
        for path in _vbt_paths(spec):
            allowed = {"get"} | DELIBERATE_MUTATING_ROUTES.get(path, set())
            assert set(spec["paths"][path]) <= allowed, (
                f"{path} exposes {sorted(spec['paths'][path])}; docs/vbt/02 Track C §4 makes "
                f"this surface read-only apart from the settings write"
            )

    def test_the_exemption_is_still_a_real_route(self, spec: OpenApiSpec) -> None:
        """A stale exemption is a hole nobody is watching."""
        for path, verbs in DELIBERATE_MUTATING_ROUTES.items():
            assert path in spec["paths"], f"{path} is exempted but no longer served"
            assert verbs <= set(spec["paths"][path])

    def test_the_router_declares_one_mutating_decorator(self) -> None:
        """Over the source, so a route added and not yet registered is still caught."""
        source = inspect.getsource(vbt_router)
        assert source.count("@router.patch(") == 1, "the settings write, and nothing else"
        assert "@router.post(" not in source, "routers/vbt.py declares a POST"
        assert "@router.put(" not in source, "routers/vbt.py declares a PUT"
        assert "@router.delete(" not in source, "routers/vbt.py declares a DELETE"

    def test_the_read_services_write_nothing(self) -> None:
        """`baskfy_api.vbt` and `baskfy_api.vbt_sleeve` are the read layer, and neither has a
        write in it at all. The settings write lives in `baskfy_api.vbt_settings`, a different
        module for exactly this reason: one file that both reads and writes cannot be asserted
        about."""
        for module in (vbt_service, vbt_sleeve):
            source = inspect.getsource(module)
            for forbidden in ("insert(", "update(", "delete(", "session.add", "session.commit"):
                assert forbidden not in source, f"{module.__name__} contains {forbidden}"

    def test_the_patch_model_cannot_name_a_system_owned_field(self) -> None:
        """`02` §3.1's counter is the system's. A caller who could set `dry_run_sessions` to 20
        could declare the paper run finished from a form."""
        assert SYSTEM_OWNED_FIELDS
        for field in SYSTEM_OWNED_FIELDS:
            assert field not in VbtConfigPatch.model_fields, f"the patch model names {field}"
        assert VbtConfigPatch.model_config.get("extra") == "forbid"


class TestItCannotReachAnOrder:
    def test_no_module_names_the_execution_package(self) -> None:
        for module in (vbt_router, vbt_service, vbt_sleeve):
            source = inspect.getsource(module)
            for forbidden in (
                "baskfy_execution",
                "OrderGateway",
                "kiteconnect",
                "place_order",
                "place_gtt",
                "kite_client",
            ):
                assert forbidden not in source, f"{module.__name__} names {forbidden}"

    def test_no_vbt_path_mentions_an_order(self, spec: OpenApiSpec) -> None:
        """A `/vbt/execute` added here fails twice — once on the verb, once on the word."""
        forbidden = ("/order", "execute", "gtt", "trade/place", "place_order")
        offenders = [
            path for path in _vbt_paths(spec) if any(word in path.lower() for word in forbidden)
        ]
        assert offenders == [], f"the vbt surface exposes {offenders}"

    def test_no_vbt_module_names_the_auto_execute_flag_that_does_not_exist(self) -> None:
        """Non-negotiable #1's exception is the swing sleeve's alone, and this sleeve has no
        equivalent. The name is asserted absent so that adding one is a deliberate, visible act
        rather than a copied line."""
        for module in (vbt_router, vbt_service, vbt_sleeve):
            source = inspect.getsource(module)
            assert "AUTO_EXECUTE" not in source.upper(), f"{module.__name__} names an auto-execute"

    def test_every_route_requires_an_authenticated_principal(self) -> None:
        """A sleeve is one person's money. There is no public view of it and no anonymous one.

        Asserted over the *signatures*, so a route added without a principal — which would answer
        200 to a stranger — fails here rather than in production.
        """
        handlers = [
            value
            for name, value in vars(vbt_router).items()
            if callable(value)
            and not name.startswith("_")
            and getattr(value, "__module__", "") == vbt_router.__name__
            and name.startswith(("get_", "patch_"))
        ]
        assert len(handlers) == 7, f"found {len(handlers)} route handlers, expected seven"
        for handler in handlers:
            hints = typing.get_type_hints(handler, include_extras=True)
            assert "principal" in hints, f"{handler.__name__} takes no principal"

    def test_every_route_scopes_to_the_sole_tenant(self) -> None:
        """One call to `scoped_sole_user_id` per handler. A route that read `principal.user_id`
        directly would serve whoever asked (M43.4)."""
        source = inspect.getsource(vbt_router)
        assert source.count("await scoped_sole_user_id(") == 7
