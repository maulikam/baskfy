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

1. it is one of the five documented non-GET routes on the surface, and the only one that
   writes a setting;
2. its request model cannot name `exposure_level` or `first_live_sessions_left` — the two fields
   that decide how much the system lets the book carry — so a caller cannot climb the ladder by
   asking, and `extra="forbid"` means they are *told* rather than silently ignored;
3. neither the router nor the service it calls names the execution package, a broker, or an
   order, and the whole API still exposes no path that mentions one.
"""

from __future__ import annotations

import inspect
import typing

import pytest

from baskfy_api import swing as swing_service
from baskfy_api import swing_journal, swing_scan, swing_watch
from baskfy_api.app import create_app
from baskfy_api.routers import swing as swing_router
from baskfy_api.swing_settings import SYSTEM_OWNED_FIELDS, SwingConfigPatch

MUTATING = ("post", "put", "patch", "delete")

#: Every non-GET route on the surface, and why each one is allowed.
#:
#: `docs/swing/02` Track A permits exactly the writes that "change no money" — "watchlist edits,
#: notes and the catalyst field". Each entry here is one of those, and a route that is not in this
#: table fails `test_every_route_is_a_get_except_the_documented_writes` the moment it is added.
#:
#: `PATCH /swing/config` writes seven settings and cannot name the exposure rung (SW4.1).
#: `POST /swing/watch` adds a name to a list. `PATCH /swing/watch/{id}` edits a note and a
#: catalyst — and nothing else, because a level a person can revise after the fact is a level
#: that can be revised to match a price they already paid. `DELETE /swing/watch/{id}` is a state
#: change to DISMISSED, not a delete.
#:
#: `POST /swing/scan` (SW15) is the fifth: it inserts one `sw_scan_run` row and publishes a task
#: name, and the worker behind that name reads quotes and writes detection rows — the same rows
#: the nightly writes, labelled provisional. A scan is never an order; the module behind the
#: route (`baskfy_api.swing_scan`) writes to that one table and names no broker
#: (`test_the_scan_writer_touches_only_the_run_table`).
DELIBERATE_MUTATING_ROUTES: dict[str, set[str]] = {
    "/api/v1/swing/config": {"patch"},
    "/api/v1/swing/watch": {"post"},
    "/api/v1/swing/watch/{watch_id}": {"patch", "delete"},
    "/api/v1/swing/scan": {"post"},
}

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
        assert paths, "the swing routes are not registered at all"
        assert len(paths) == 12, f"expected twelve swing paths, found {paths}"

    def test_every_route_is_a_get_except_the_documented_writes(self, spec: OpenApiSpec) -> None:
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

    def test_the_router_declares_only_the_documented_mutating_decorators(self) -> None:
        """Over the source, so a route added and not yet registered is still caught."""
        source = inspect.getsource(swing_router)
        assert source.count("@router.patch(") == 2, "settings and the watchlist annotation"
        assert source.count("@router.post(") == 2, "watching a name, and Scan now"
        assert source.count("@router.delete(") == 1, "dismissing one"
        assert "@router.put(" not in source, "routers/swing.py declares a PUT"

    def test_the_read_services_write_nothing(self) -> None:
        """`baskfy_api.swing` and `baskfy_api.swing_journal` are the read layer, and neither has
        a write in it at all.

        The watchlist's writes live in `baskfy_api.swing_watch`, which is a different module for
        exactly this reason: one file that both reads and writes cannot be asserted about. The
        ladder rung the journal shows is written by the EOD job, never by the page that shows it.
        """
        for module in (swing_service, swing_journal):
            source = inspect.getsource(module)
            for forbidden in ("insert(", "update(", "delete(", "session.add", "session.commit"):
                assert forbidden not in source, f"{module.__name__} contains {forbidden}"

    def test_the_watchlist_writer_touches_only_the_watchlist(self) -> None:
        """`swing_watch` may write, and only to `sw_watch`.

        A module allowed to write is a module worth checking the *target* of: a watchlist service
        that could add an `SwPlanLine` or an `SwPosition` would move money under a name that says
        it does not.
        """
        source = inspect.getsource(swing_watch)
        for forbidden in ("SwPlan", "SwPosition", "SwFill", "SwSignal", "SwConfig"):
            assert forbidden not in source, f"swing_watch names {forbidden}"
        assert "SwWatch(" in source, "swing_watch does not construct an sw_watch row at all"

    def test_the_scan_writer_touches_only_the_run_table(self) -> None:
        """SW15: `swing_scan` may write, and only to `sw_scan_run` — one row saying "a person
        asked" — and publish one task name. A module that could write a plan line or a
        position under the word "scan" would be the hole this file exists to close."""
        source = inspect.getsource(swing_scan)
        for forbidden in (
            "SwPlan",
            "SwPosition",
            "SwFill",
            "SwSignal",
            "SwConfig",
            "SwSetupDaily",
            "SwMarketDaily",
            "SwWatch",
        ):
            assert forbidden not in source, f"swing_scan names {forbidden}"
        assert "SwScanRun(" in source, "swing_scan does not construct an sw_scan_run row at all"
        assert 'SCAN_TASK_NAME: Final = "baskfy.swing.scan_now"' in source


class TestItCannotReachAnOrder:
    def test_no_module_names_the_execution_package(self) -> None:
        for module in (swing_router, swing_service, swing_journal, swing_scan):
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

    def test_every_route_requires_an_authenticated_principal(self) -> None:
        """A swing book is one person's positions, levels and results.

        Unlike `/market-health` there is no public view of it and no anonymous one. Asserted over
        the *signatures*: every handler takes `AuthenticatedDep`, so a route added without it —
        which would answer 200 to a stranger — fails here rather than in production.
        `scoped_sole_user_id` then refuses a principal who is not the sole tenant, rather than
        serving them somebody else's book (M43.4).
        """
        handlers = [
            value
            for name, value in vars(swing_router).items()
            if callable(value)
            and not name.startswith("_")
            and getattr(value, "__module__", "") == swing_router.__name__
            and name.startswith(("get_", "post_", "patch_", "delete_"))
        ]
        assert len(handlers) >= 10, f"only found {len(handlers)} route handlers"
        for handler in handlers:
            hints = typing.get_type_hints(handler, include_extras=True)
            assert "principal" in hints, f"{handler.__name__} takes no principal"

    def test_every_route_scopes_to_the_sole_tenant(self) -> None:
        """One call to `scoped_sole_user_id` per handler. A route that read `principal.user_id`
        directly would serve whoever asked."""
        source = inspect.getsource(swing_router)
        assert source.count("await scoped_sole_user_id(") >= 10


class TestTheLadderCannotBeClimbedByAsking:
    @pytest.mark.parametrize("field", sorted(SYSTEM_OWNED_FIELDS))
    def test_a_system_owned_field_is_not_in_the_request_model(self, field: str) -> None:
        assert field not in SwingConfigPatch.model_fields
        with pytest.raises(ValueError, match=r"extra_forbidden|Extra inputs"):
            SwingConfigPatch.model_validate({field: 3})

    def test_the_request_model_forbids_anything_it_does_not_know(self) -> None:
        assert SwingConfigPatch.model_config.get("extra") == "forbid"
