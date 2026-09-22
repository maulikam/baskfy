"""OP5's safety acceptance: `/options` in the API reads, and writes only two money-free things.

`docs/options/05` §2: "`GET /options/today`, …, `POST/DELETE /options/event-day`,
`GET/PATCH /options/config`. **No other verb on any path.**" `06` OP5: "exactly two mutations
(event day, settings), everything else 405; no import of `packages/execution` in `apps/web` or
the router." This file is the API half; the web half is
`apps/web/src/app/(app)/options/__tests__/read-only.test.tsx`. The pattern is
`test_twt_readonly.py`'s: structural, so it fails the moment a mutating route is added rather
than the moment somebody notices.

WHY THE TWO WRITES ARE SAFE
---------------------------
* An **event day** is a day no sleeve trades (`04` §1.3). Adding one can only make the book do
  less; removing one is limited to a day the person added (`source='USER'`) — a seeded,
  source-verified RBI date is refused.
* The **settings** patch is `baskfy_api.options_settings`, OP2's boundary unchanged: ceilings are
  server configuration, `paused_*` is not a field, and no execution flag is a field.

Neither can build, confirm or close a plan, and nothing under `/options` names a broker.
"""

from __future__ import annotations

import inspect
import typing

import pytest
from fastapi.testclient import TestClient

from baskfy_api import options_read, options_settings
from baskfy_api.app import create_app
from baskfy_api.routers import options as options_router

#: `05` §2's routes, verbatim — path → the verbs it may carry.
DOCUMENTED: dict[str, set[str]] = {
    "/api/v1/options/today": {"get"},
    "/api/v1/options/scan/{sleeve}": {"get"},
    "/api/v1/options/chain": {"get"},
    "/api/v1/options/positions": {"get"},
    "/api/v1/options/sessions": {"get"},
    "/api/v1/options/journal": {"get"},
    "/api/v1/options/backtest": {"get"},
    "/api/v1/options/calendar": {"get"},
    "/api/v1/options/event-day": {"post", "delete"},
    "/api/v1/options/config": {"get", "patch"},
}

#: The two allowed mutations, as (path, verbs). Exact equality below, not a subset.
MUTATIONS: dict[str, list[str]] = {
    "/api/v1/options/config": ["patch"],
    "/api/v1/options/event-day": ["delete", "post"],
}

OpenApiSpec = dict[str, dict[str, dict[str, object]]]


@pytest.fixture(scope="module")
def spec() -> OpenApiSpec:
    return create_app().openapi()


def _options_paths(spec: OpenApiSpec) -> list[str]:
    return sorted(path for path in spec["paths"] if path.startswith("/api/v1/options"))


class TestTheSurfaceIsExactlyTheSpec:
    def test_the_routes_exist(self, spec: OpenApiSpec) -> None:
        """Non-vacuity first: an unregistered router passes every check below trivially."""
        assert _options_paths(spec) == sorted(DOCUMENTED)

    def test_every_path_carries_exactly_its_documented_verbs(self, spec: OpenApiSpec) -> None:
        for path, verbs in DOCUMENTED.items():
            assert set(spec["paths"][path]) == verbs, path

    def test_exactly_two_mutations_event_day_and_settings(self, spec: OpenApiSpec) -> None:
        mutating = {
            path: sorted(verb for verb in spec["paths"][path] if verb != "get")
            for path in _options_paths(spec)
            if any(verb != "get" for verb in spec["paths"][path])
        }
        assert mutating == MUTATIONS

    def test_the_router_declares_only_the_documented_mutating_decorators(self) -> None:
        """Over the source, so a route added and not yet registered is still caught."""
        source = inspect.getsource(options_router)
        assert source.count("@router.post(") == 1
        assert source.count("@router.delete(") == 1
        assert source.count("@router.patch(") == 1
        assert "@router.put(" not in source
        assert '@router.post(\n    "/event-day",' in source
        assert '@router.delete("/event-day"' in source
        assert '@router.patch("/config"' in source

    def test_every_other_verb_is_405(self) -> None:
        """`06` OP5: "everything else 405" — asked of the running app, not of the document.
        Method dispatch happens before authentication, so no token is needed to prove it."""
        client = TestClient(create_app())
        for path, verbs in DOCUMENTED.items():
            concrete = path.replace("{sleeve}", "O2")
            for verb in ("get", "post", "put", "patch", "delete"):
                if verb in verbs:
                    continue
                response = client.request(verb.upper(), concrete)
                assert response.status_code == 405, (verb, concrete, response.status_code)


class TestItCannotReachAnOrder:
    MODULES = (options_router, options_read, options_settings)

    def test_no_module_names_the_execution_package_or_a_broker(self) -> None:
        for module in self.MODULES:
            source = inspect.getsource(module)
            for forbidden in (
                "baskfy_execution",
                "OrderGateway",
                "kiteconnect",
                "place_order",
                "place_gtt",
                "kite_client",
                "build_kite_provider",
                "live_quote",
            ):
                assert forbidden not in source, f"{module.__name__} names {forbidden}"

    def test_no_options_path_mentions_an_order(self, spec: OpenApiSpec) -> None:
        forbidden = ("/order", "execute", "confirm", "close", "plan", "gtt")
        offenders = [
            path for path in _options_paths(spec) if any(w in path.lower() for w in forbidden)
        ]
        assert offenders == []

    def test_the_whole_api_serves_no_execute_or_confirm_route(self, spec: OpenApiSpec) -> None:
        """App-wide, not only `/options`: the web API never gains an order route (root `CLAUDE.md`
        non-negotiable 1, `02` Track C §4). Orders are confirmed on the desk console, and a path
        that spelt one anywhere under `/api/v1` would be the first."""
        forbidden = ("execute", "confirm", "place_order", "place-order", "/gtt")
        offenders = [p for p in spec["paths"] if any(w in p.lower() for w in forbidden)]
        assert offenders == []

    def test_no_module_names_an_auto_execute_flag(self) -> None:
        """PACK.3: there is no auto-execute flag for any options sleeve, and none may be added."""
        for module in self.MODULES:
            assert "AUTO_EXECUTE" not in inspect.getsource(module).upper()

    def test_the_router_reports_the_execution_flags_and_never_branches_on_them(self) -> None:
        """They are handed to the page as values (`Execution: disabled on this server`), never
        read into a decision — the TWT rule (TW13.2), per sleeve group."""
        source = inspect.getsource(options_router)
        for group in ("o1m", "o1w", "o2", "o3"):
            name = f"settings.options_{group}_execution_enabled"
            assert source.count(name) == 1, name
            assert f"if {name}" not in source
            assert f"if not {name}" not in source

    def test_the_settings_patch_has_no_flag_or_pause_field(self) -> None:
        fields = set(options_router.OptionsConfigPatch.model_fields)
        assert fields == {"book", "sleeves"}
        for model in (options_settings.OptionsBookPatch, options_settings.OptionsSleevePatch):
            names = set(model.model_fields)
            assert not names & {"paused_until", "paused_reason"}
            assert not any("execution" in name or name == "enabled" for name in names)

    def test_every_route_requires_an_authenticated_principal(self) -> None:
        handlers = [
            value
            for name, value in vars(options_router).items()
            if callable(value)
            and getattr(value, "__module__", "") == options_router.__name__
            and name.startswith(("get_", "post_", "patch_", "delete_"))
        ]
        assert len(handlers) == 12, [h.__name__ for h in handlers]
        for handler in handlers:
            hints = typing.get_type_hints(handler, include_extras=True)
            assert "principal" in hints, f"{handler.__name__} takes no principal"

    def test_every_route_scopes_to_the_sole_tenant(self) -> None:
        source = inspect.getsource(options_router)
        assert source.count("scoped_sole_user_id(") == 12
