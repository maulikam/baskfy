"""TW12's safety acceptance: `/twt` in the API can ask for a scan and can never trade.

`docs/twt/02-scope-and-gating.md` Track C §4: **"`apps/web` gets no route under `/twt` that can
reach the gateway."** This is the assertion behind that sentence, extending the pattern
`test_desk_readonly.py`, `test_swing_readonly.py` and `test_vbt_readonly.py` already set.

Structural rather than conventional: it fails the moment somebody adds a mutating route, not the
moment somebody notices.

WHY `POST /twt/scan` IS ALLOWED AND STILL SAFE
-----------------------------------------------
It is the **only** non-GET route on this surface — TW12 built it for `PLAN-SCAN-SYNC.md`'s "Scan
now" contract — and the claim for it is narrow: it writes **one row in one table** and publishes
**one task name**, and that name is `baskfy.twt.scan`, whose worker calls the detector
`baskfy.twt.detect` already ships. A detection is not a plan and a plan is not an order. The
evening job is still what turns a signal into a plan line, and a person pressing Confirm on the
desk is still what turns a plan line into an order.

Track A described this hub as read-only "except notes and dismissals, which change no money". A
scan changes no money by the same test, and the widening is **recorded** as DECISIONS-TW TW12.3
rather than assumed — which is why `test_the_one_mutating_route_is_the_scan_and_nothing_else` is
an exact equality and not a subset check.

WHAT THIS FILE DELIBERATELY DOES NOT COVER
-------------------------------------------
`/twt/today` and `/twt/backtest`, which `apps/web`'s `src/lib/twt/fetch.ts` already asks for and
which nothing serves yet (TW8 built the page ahead of its data). When somebody serves them, the
path count below fails and they get read here — which is the point of asserting a count.
"""

from __future__ import annotations

import inspect
import typing

import pytest

from baskfy_api import twt_scan, twt_sleeve
from baskfy_api.app import create_app
from baskfy_api.routers import twt as twt_router

#: Every non-GET route on the surface, and why it is allowed. A route that is not in this table
#: fails `test_every_route_is_a_get_except_the_documented_write` the moment it is added.
DELIBERATE_MUTATING_ROUTES: dict[str, set[str]] = {
    "/api/v1/twt/scan": {"post"},
}

#: What `app.openapi()` returns, to the depth these tests read it.
OpenApiSpec = dict[str, dict[str, dict[str, object]]]


@pytest.fixture(scope="module")
def spec() -> OpenApiSpec:
    return create_app().openapi()


def _twt_paths(spec: OpenApiSpec) -> list[str]:
    return sorted(path for path in spec["paths"] if "/twt" in path)


class TestTheSurfaceIsRegisteredAndReadOnly:
    def test_the_twt_routes_exist(self, spec: OpenApiSpec) -> None:
        """Non-vacuity first: every assertion below passes trivially against an unregistered
        router, which is exactly the state this surface was in before TW12."""
        paths = _twt_paths(spec)
        assert paths, "the twt routes are not registered at all"
        assert len(paths) == 2, f"expected two twt paths, found {paths}"
        assert "/api/v1/twt/scan" in paths
        assert "/api/v1/twt/scan/{run_id}" in paths

    def test_every_route_is_a_get_except_the_documented_write(self, spec: OpenApiSpec) -> None:
        for path in _twt_paths(spec):
            allowed = {"get"} | DELIBERATE_MUTATING_ROUTES.get(path, set())
            assert set(spec["paths"][path]) <= allowed, (
                f"{path} exposes {sorted(spec['paths'][path])}; docs/twt/02 Track C §4 makes "
                f"this surface read-only apart from the scan"
            )

    def test_the_exemption_is_still_a_real_route(self, spec: OpenApiSpec) -> None:
        """A stale exemption is a hole nobody is watching."""
        for path, verbs in DELIBERATE_MUTATING_ROUTES.items():
            assert path in spec["paths"], f"{path} is exempted but no longer served"
            assert verbs <= set(spec["paths"][path])

    def test_the_one_mutating_route_is_the_scan_and_nothing_else(self, spec: OpenApiSpec) -> None:
        """Exact equality, not a subset: a second money-free write would be a second decision."""
        mutating = {
            path: sorted(verb for verb in spec["paths"][path] if verb != "get")
            for path in _twt_paths(spec)
            if any(verb != "get" for verb in spec["paths"][path])
        }
        assert mutating == {"/api/v1/twt/scan": ["post"]}

    def test_the_router_declares_only_the_documented_mutating_decorators(self) -> None:
        """Over the source, so a route added and not yet registered is still caught."""
        source = inspect.getsource(twt_router)
        assert source.count("@router.post(") == 1, "Scan now, and nothing else"
        assert '@router.post(\n    "/scan",' in source, "the one POST is the scan"
        assert "@router.patch(" not in source, "routers/twt.py declares a PATCH"
        assert "@router.put(" not in source, "routers/twt.py declares a PUT"
        assert "@router.delete(" not in source, "routers/twt.py declares a DELETE"

    def test_the_scan_writer_touches_only_the_run_table(self) -> None:
        """`twt_scan` may write, and only to `tw_scan_run` — one row saying "a person asked" —
        and publish one task name. A module that could write a plan line, an order or a position
        under the word "scan" would be the hole this file exists to close.

        It also publishes the task whose worker calls the detector TW4 **already shipped**, rather
        than a second detector: the name is asserted literally, because a scan route that queued
        something new would be a second code path to the same rows with none of TW4's tests
        behind it.
        """
        source = inspect.getsource(twt_scan)
        for forbidden in (
            "TwPlan",
            "TwPlanLine",
            "TwOrder",
            "TwPosition",
            "TwFill",
            "TwSignalDaily",
            "TwBreadthDaily",
            "TwStateDaily",
            "TwConfig",
            "TwSession",
        ):
            assert forbidden not in source, f"twt_scan names {forbidden}"
        assert "TwScanRun(" in source, "twt_scan does not construct a tw_scan_run row at all"
        assert 'SCAN_TASK_NAME: Final = "baskfy.twt.scan"' in source

    def test_no_twt_module_writes_the_sleeve_s_capital(self) -> None:
        """The repo's strictest rail on the repo's strictest sleeve: nothing an agent wrote may
        set `tw_config.sleeve_capital_inr` (`docs/twt/02` §3, root CLAUDE.md's safety rails)."""
        for module in (twt_router, twt_scan):
            source = inspect.getsource(module)
            assert "sleeve_capital_inr" not in source, f"{module.__name__} names the capital"


class TestItCannotReachAnOrder:
    def test_no_module_names_the_execution_package(self) -> None:
        for module in (twt_router, twt_scan, twt_sleeve):
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

    def test_no_twt_path_mentions_an_order(self, spec: OpenApiSpec) -> None:
        """A `/twt/execute` added here fails twice — once on the verb, once on the word."""
        forbidden = ("/order", "execute", "gtt", "trade/place", "place_order")
        offenders = [
            path for path in _twt_paths(spec) if any(word in path.lower() for word in forbidden)
        ]
        assert offenders == [], f"the twt surface exposes {offenders}"

    def test_no_twt_module_names_the_auto_execute_flag_that_does_not_exist(self) -> None:
        """Non-negotiable #1's exception is the swing sleeve's alone, and `docs/twt/02` Track B
        says in so many words that this sleeve has no equivalent and gains none."""
        for module in (twt_router, twt_scan):
            source = inspect.getsource(module)
            assert "AUTO_EXECUTE" not in source.upper(), f"{module.__name__} names an auto-execute"

    def test_no_twt_module_reads_the_execution_flag_at_all(self) -> None:
        """Stronger than the line above and worth stating separately: a scan does not care
        whether execution is enabled, so a scan module that read the flag would be a scan module
        with a branch nobody asked for."""
        for module in (twt_router, twt_scan):
            source = inspect.getsource(module)
            assert "twt_execution_enabled" not in source, f"{module.__name__} reads the flag"

    def test_every_route_requires_an_authenticated_principal(self) -> None:
        """A sleeve is one person's money. There is no public view of it and no anonymous one.

        Asserted over the *signatures*, so a route added without a principal — which would answer
        200 to a stranger — fails here rather than in production.
        """
        handlers = [
            value
            for name, value in vars(twt_router).items()
            if callable(value)
            and not name.startswith("_")
            and getattr(value, "__module__", "") == twt_router.__name__
            and name.startswith(("get_", "post_"))
        ]
        assert len(handlers) == 2, f"found {len(handlers)} route handlers, expected two"
        for handler in handlers:
            hints = typing.get_type_hints(handler, include_extras=True)
            assert "principal" in hints, f"{handler.__name__} takes no principal"

    def test_every_route_scopes_to_the_sole_tenant(self) -> None:
        """`02` Track C §6. A route that read `principal.user_id` directly would serve whoever
        asked, which on a sleeve is somebody else's book."""
        source = inspect.getsource(twt_router)
        assert source.count("scoped_sole_user_id(") == 2, (
            "every /twt route must resolve the sole tenant rather than trust the principal"
        )
