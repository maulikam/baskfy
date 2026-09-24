"""FO5's safety acceptance: `/fno` in the API reads, and writes one money-free thing.

`docs/fno/06` FO5: "`routers/fno.py`: GET-only, plus the settings PATCH (money-free, audited).
Every other verb is a 405, and `test_fno_readonly.py` checks both sides." `02` Track C §4: "No
web-app orders. `apps/web` and `services/api` get no route that can reach the gateway." This file
is both halves: the API's OpenAPI document and source, and the web tree's source (the pages
`/options/overnight` and `/options/fno`, their reads and components), in the census shape
`test_options_readonly.py` and the options tab's `read-only.test.tsx` set.
"""

from __future__ import annotations

import inspect
import re
import typing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baskfy_api import fno_read, fno_settings
from baskfy_api.app import create_app
from baskfy_api.routers import fno as fno_router

#: `06` FO5's surface — path -> the verbs it may carry.
DOCUMENTED: dict[str, set[str]] = {
    "/api/v1/fno/overnight": {"get"},
    "/api/v1/fno/info": {"get"},
    "/api/v1/fno/config": {"get", "patch"},
}
MUTATIONS: dict[str, list[str]] = {"/api/v1/fno/config": ["patch"]}

OpenApiSpec = dict[str, dict[str, dict[str, object]]]

WEB_SRC = Path(__file__).resolve().parents[3] / "apps" / "web" / "src"
WEB_TREES = (
    WEB_SRC / "app" / "(app)" / "options" / "overnight",
    WEB_SRC / "app" / "(app)" / "options" / "fno",
    WEB_SRC / "lib" / "fno",
    WEB_SRC / "components" / "fno",
)


@pytest.fixture(scope="module")
def spec() -> OpenApiSpec:
    return create_app().openapi()


def _fno_paths(spec: OpenApiSpec) -> list[str]:
    return sorted(path for path in spec["paths"] if path.startswith("/api/v1/fno"))


class TestTheSurfaceIsExactlyTheSpec:
    def test_the_routes_exist(self, spec: OpenApiSpec) -> None:
        """Non-vacuity first: an unregistered router passes every check below trivially."""
        assert _fno_paths(spec) == sorted(DOCUMENTED)

    def test_every_path_carries_exactly_its_documented_verbs(self, spec: OpenApiSpec) -> None:
        for path, verbs in DOCUMENTED.items():
            assert set(spec["paths"][path]) == verbs, path

    def test_exactly_one_mutation_the_settings(self, spec: OpenApiSpec) -> None:
        mutating = {
            path: sorted(verb for verb in spec["paths"][path] if verb != "get")
            for path in _fno_paths(spec)
            if any(verb != "get" for verb in spec["paths"][path])
        }
        assert mutating == MUTATIONS

    def test_the_router_declares_only_the_settings_patch(self) -> None:
        """Over the source, so a route added and not yet registered is still caught."""
        source = inspect.getsource(fno_router)
        assert source.count("@router.patch(") == 1
        assert '@router.patch("/config"' in source
        for verb in ("post", "put", "delete"):
            assert f"@router.{verb}(" not in source

    def test_every_other_verb_is_405(self) -> None:
        """Asked of the running app. Method dispatch happens before authentication."""
        client = TestClient(create_app())
        for path, verbs in DOCUMENTED.items():
            for verb in ("get", "post", "put", "patch", "delete"):
                if verb in verbs:
                    continue
                response = client.request(verb.upper(), path)
                assert response.status_code == 405, (verb, path, response.status_code)


class TestTheApiCannotReachAnOrder:
    MODULES = (fno_router, fno_read, fno_settings)

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
                "fno_gates",
            ):
                assert forbidden not in source, f"{module.__name__} names {forbidden}"

    def test_no_fno_path_mentions_an_order(self, spec: OpenApiSpec) -> None:
        forbidden = ("/order", "execute", "confirm", "close", "plan", "gtt", "roll")
        offenders = [p for p in _fno_paths(spec) if any(w in p.lower() for w in forbidden)]
        assert offenders == []

    def test_the_whole_api_serves_no_execute_or_confirm_route(self, spec: OpenApiSpec) -> None:
        forbidden = ("execute", "confirm", "place_order", "place-order", "/gtt")
        offenders = [p for p in spec["paths"] if any(w in p.lower() for w in forbidden)]
        assert offenders == []

    def test_no_module_names_an_auto_execute_flag(self) -> None:
        """`02` Track B: there is no FO auto-execute flag, and none may be added."""
        for module in self.MODULES:
            source = inspect.getsource(module).upper()
            assert "AUTO_EXECUTE" not in source
            assert not re.search(r"FNO_\w*AUTO", source)

    def test_the_router_reports_the_execution_flags_and_never_branches_on_them(self) -> None:
        source = inspect.getsource(fno_router)
        for group in ("f1", "f2"):
            name = f"settings.fno_{group}_execution_enabled"
            assert source.count(name) == 1, name
            assert f"if {name}" not in source
            assert f"if not {name}" not in source

    def test_the_settings_patch_has_no_flag_or_pause_field(self) -> None:
        assert set(fno_router.FnoConfigPatch.model_fields) == {"book", "sleeves"}
        for model in (fno_settings.FnoBookPatch, fno_settings.FnoSleevePatch):
            names = set(model.model_fields)
            assert not names & set(fno_settings.SYSTEM_OWNED_FIELDS)
            assert not any("execution" in n or n == "enabled" or "carry" in n for n in names)

    def test_every_route_requires_an_authenticated_principal_and_the_sole_tenant(self) -> None:
        handlers = [
            value
            for name, value in vars(fno_router).items()
            if callable(value)
            and getattr(value, "__module__", "") == fno_router.__name__
            and name.startswith(("get_", "post_", "patch_", "delete_", "put_"))
        ]
        assert len(handlers) == 4, [h.__name__ for h in handlers]
        for handler in handlers:
            hints = typing.get_type_hints(handler, include_extras=True)
            assert "principal" in hints, f"{handler.__name__} takes no principal"
        assert inspect.getsource(fno_router).count("scoped_sole_user_id(") == 4


def _web_sources() -> list[Path]:
    files: list[Path] = []
    for tree in WEB_TREES:
        files.extend(
            p for p in tree.rglob("*") if p.suffix in {".ts", ".tsx"} and "__tests__" not in p.parts
        )
    return files


class TestTheWebCannotReachAnOrder:
    """The other side of the wire: the two pages and what they import."""

    FORBIDDEN = (
        "/desk/",
        "/execute",
        "/fno/execute",
        "place_order",
        "placeorder",
        "place_gtt",
        "kiteconnect",
        "kite_client",
        "ordergateway",
        "baskfy_execution",
        "@baskfy/execution",
        "confirm=true",
        "auto_execute",
        "auto-execute",
        "use server",
        'method: "post"',
        'method: "put"',
        'method: "delete"',
        'method: "patch"',
    )

    def test_the_trees_exist(self) -> None:
        """Non-vacuity: both pages, their reads and their components."""
        files = _web_sources()
        assert (WEB_TREES[0] / "page.tsx") in files
        assert (WEB_TREES[1] / "page.tsx") in files
        assert (WEB_TREES[2] / "fetch.ts") in files
        assert len(files) >= 6

    def test_no_source_names_an_execute_path_a_broker_or_a_write(self) -> None:
        for path in _web_sources():
            source = path.read_text(encoding="utf8").lower()
            for word in self.FORBIDDEN:
                assert word not in source, f"{path} names {word}"

    def test_no_route_handler_or_server_action_under_the_trees(self) -> None:
        for tree in WEB_TREES:
            assert not [p for p in tree.rglob("route.*")], tree
            assert not [p for p in tree.rglob("actions.*")], tree

    def test_the_reads_fetch_only_the_two_get_paths(self) -> None:
        fetch = (WEB_TREES[2] / "fetch.ts").read_text(encoding="utf8")
        paths = sorted(set(re.findall(r'"(/fno/[a-z/-]+)"', fetch)))
        assert paths == ["/fno/info", "/fno/overnight"]
