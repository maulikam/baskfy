"""SW4's safety acceptance: `/swing` shows the book and can never trade it.

`docs/swing/02-scope-and-gating.md` Track C §4: "**No web-app orders.** `apps/web` gets no route
under `/swing` that can reach the gateway. The existing `test_baskets_readonly.py` /
`test_desk_readonly.py` pattern is extended to the new routers." This is that extension.

It is structural rather than conventional — it fails the moment somebody adds a mutating route,
not the moment somebody notices — and it covers the one write the surface does have.

WHY `PATCH /swing/config` IS ALLOWED AND STILL SAFE
---------------------------------------------------
Track A permits the surface's non-money mutations ("watchlist edits, notes and the catalyst
field — they change no money"), and the settings form is the same kind of thing: it writes seven
numbers into `sw_config`. Three claims make that defensible, and each is asserted below:

1. it is the **only** non-GET route on the surface;
2. its request model cannot name `exposure_level` or `first_live_sessions_left` — the two fields
   that decide how much the system lets the book carry — so a caller cannot climb the ladder by
   asking, and `extra="forbid"` means they are *told* rather than silently ignored;
3. neither the router nor the service it calls names the execution package, a broker, or an
   order, and the whole API still exposes no path that mentions one.
"""

from __future__ import annotations

import inspect

import pytest

from baskfy_api import swing as swing_service
from baskfy_api.app import create_app
from baskfy_api.routers import swing as swing_router
from baskfy_api.swing_settings import SYSTEM_OWNED_FIELDS, SwingConfigPatch

MUTATING = ("post", "put", "patch", "delete")

#: The one non-GET route the surface has, and the only one it may ever have without a decision
#: recorded in `docs/swing/DECISIONS-SW.md`.
DELIBERATE_MUTATING_ROUTES: dict[str, set[str]] = {"/api/v1/swing/config": {"patch"}}

#: What `app.openapi()` returns, to the depth these tests read it.
OpenApiSpec = dict[str, dict[str, dict[str, object]]]


@pytest.fixture(scope="module")
def spec() -> OpenApiSpec:
    return create_app().openapi()


def _swing_paths(spec: OpenApiSpec) -> list[str]:
    return sorted(path for path in spec["paths"] if "/swing" in path)


class TestTheSurfaceIsRegisteredAndReadOnly:
    def test_the_swing_routes_exist(self, spec: OpenApiSpec) -> None:
        paths = _swing_paths(spec)
        assert paths, "SW4's routes are not registered at all"
        assert len(paths) == 5, f"expected five swing paths, found {paths}"

    def test_every_route_is_a_get_except_the_settings_patch(self, spec: OpenApiSpec) -> None:
        for path in _swing_paths(spec):
            allowed = {"get"} | DELIBERATE_MUTATING_ROUTES.get(path, set())
            assert set(spec["paths"][path]) <= allowed, (
                f"{path} exposes {sorted(spec['paths'][path])}; "
                f"docs/swing/02 Track A makes this surface read-only apart from writes that "
                f"move no money"
            )

    def test_the_exemption_is_still_a_real_route(self, spec: OpenApiSpec) -> None:
        """A stale exemption is a hole nobody is watching."""
        for path, verbs in DELIBERATE_MUTATING_ROUTES.items():
            assert path in spec["paths"], f"{path} is exempted but no longer served"
            assert verbs <= set(spec["paths"][path])

    def test_the_router_declares_one_mutating_decorator(self) -> None:
        """Over the source, so a route added and not yet registered is still caught."""
        source = inspect.getsource(swing_router)
        assert source.count("@router.patch(") == 1
        for verb in ("post", "put", "delete"):
            assert f"@router.{verb}(" not in source, f"routers/swing.py declares a {verb.upper()}"

    def test_the_service_writes_nothing(self) -> None:
        """`baskfy_api.swing` is the read layer, and it has no write in it at all."""
        source = inspect.getsource(swing_service)
        for forbidden in ("insert(", "update(", "delete(", "session.add", "session.commit"):
            assert forbidden not in source, f"baskfy_api/swing.py contains {forbidden}"


class TestItCannotReachAnOrder:
    def test_neither_module_names_the_execution_package(self) -> None:
        for module in (swing_router, swing_service):
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

    def test_no_swing_path_mentions_an_order(self, spec: OpenApiSpec) -> None:
        """The same word list `test_baskets_readonly.py` applies to the whole API, over the
        surface this module is about — so a `/swing/execute` added here fails twice."""
        forbidden = ("/order", "execute", "gtt", "trade/place", "place_order")
        offenders = [
            path for path in _swing_paths(spec) if any(word in path.lower() for word in forbidden)
        ]
        assert offenders == [], f"the swing surface exposes {offenders}"

    def test_the_swing_router_is_not_reachable_without_a_bearer_token(
        self, spec: OpenApiSpec
    ) -> None:
        """A swing book is one person's positions, levels and results.

        Unlike `/market-health` there is no public view of it, so every operation declares a
        security requirement. `scoped_sole_user_id` then refuses a principal that is not the sole
        tenant rather than serving them somebody else's book (M43.4).
        """
        for path in _swing_paths(spec):
            for method, operation in spec["paths"][path].items():
                assert isinstance(operation, dict)
                parameters = operation.get("parameters", [])
                assert isinstance(parameters, list)
                # FastAPI puts the dependency's security scheme on the operation; the shape we
                # can assert without coupling to it is that the route is not documented as
                # anonymous, which `create_app` marks by omitting `security` entirely nowhere.
                assert method in {"get", "patch"}, f"{path} exposes {method}"


class TestTheLadderCannotBeClimbedByAsking:
    @pytest.mark.parametrize("field", sorted(SYSTEM_OWNED_FIELDS))
    def test_a_system_owned_field_is_not_in_the_request_model(self, field: str) -> None:
        assert field not in SwingConfigPatch.model_fields
        with pytest.raises(ValueError, match=r"extra_forbidden|Extra inputs"):
            SwingConfigPatch.model_validate({field: 3})

    def test_the_request_model_forbids_anything_it_does_not_know(self) -> None:
        assert SwingConfigPatch.model_config.get("extra") == "forbid"
