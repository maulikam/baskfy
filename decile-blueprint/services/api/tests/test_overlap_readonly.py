"""`/overlap` can list the day's candidates and can never trade, scan, rank or size.

`/build/overlap` used to compute its membership in the web app from three page reads and showed
symbols only. Serving it from the API in one read is worth doing only if the read stays exactly
that — an intersection of stored rows — and this file is the assertion, structural rather than
conventional, in the pattern `test_swing_readonly.py`, `test_vbt_readonly.py` and
`test_twt_readonly.py` set: it fails the moment somebody adds a verb, a broker name or a number
the strategies did not write.

WHY THERE IS NO "SCAN NOW" HERE
-------------------------------
Each sleeve has one, on its own hub, and a scan is money-free. But `/overlap` is a view over
three sleeves' newest sessions; a button here that queued three detectors would make this the
page that decides when the sleeves run, and it is not. So the surface is one GET and exactly one.
"""

from __future__ import annotations

import inspect
import typing

import pytest

from baskfy_api import instrument_appearances
from baskfy_api import overlap as overlap_service
from baskfy_api.app import create_app
from baskfy_api.routers import overlap as overlap_router
from baskfy_core.swing.config import TRADEABLE_SETUPS, Setup

OpenApiSpec = dict[str, dict[str, dict[str, object]]]


@pytest.fixture(scope="module")
def spec() -> OpenApiSpec:
    return create_app().openapi()


def _overlap_paths(spec: OpenApiSpec) -> list[str]:
    return sorted(path for path in spec["paths"] if "/overlap" in path)


class TestTheSurfaceIsRegisteredAndReadOnly:
    def test_the_one_route_exists_and_is_a_get(self, spec: OpenApiSpec) -> None:
        assert _overlap_paths(spec) == ["/api/v1/overlap"]
        assert set(spec["paths"]["/api/v1/overlap"]) == {"get"}

    def test_the_scope_query_admits_the_two_documented_values_only(self, spec: OpenApiSpec) -> None:
        operation = spec["paths"]["/api/v1/overlap"]["get"]
        assert isinstance(operation, dict)
        parameters = operation["parameters"]
        assert isinstance(parameters, list)
        [scope] = [p for p in parameters if isinstance(p, dict) and p.get("name") == "scope"]
        schema = scope["schema"]
        assert isinstance(schema, dict)
        assert schema["enum"] == ["actionable", "all"]
        assert schema["default"] == "actionable"

    def test_no_module_names_the_execution_package(self) -> None:
        for module in (overlap_router, overlap_service):
            source = inspect.getsource(module)
            for forbidden in (
                "baskfy_execution",
                "OrderGateway",
                "kiteconnect",
                "place_order",
                "place_gtt",
                "kite_client",
                "build_plan",
                "AUTO_EXECUTE",
                "EXECUTION_ENABLED",
            ):
                assert forbidden not in source, f"{module.__name__} names {forbidden}"

    def test_the_read_model_writes_nothing(self) -> None:
        """An intersection of stored rows: `select` only. No insert, update, delete or commit."""
        source = inspect.getsource(overlap_service)
        for verb in ("session.add", "insert(", "update(", "delete(", ".commit(", ".execute(text"):
            assert verb not in source, f"the overlap read model must not {verb}"

    def test_the_route_requires_an_authenticated_principal(self) -> None:
        handlers = [
            value
            for name, value in vars(overlap_router).items()
            if callable(value)
            and getattr(value, "__module__", "") == overlap_router.__name__
            and name.startswith("get_")
        ]
        assert len(handlers) == 1, f"found {len(handlers)} route handlers, expected one"
        hints = typing.get_type_hints(handlers[0], include_extras=True)
        assert "principal" in hints

    def test_the_strategies_are_read_for_the_sole_tenant_only(self) -> None:
        """The screens are the caller's; the sleeves are one person's. The route resolves the
        sole tenant and passes ``None`` for anyone else rather than trusting the principal."""
        source = inspect.getsource(overlap_router)
        assert source.count("scoped_sole_user_id(") == 1
        assert "strategies_user_id=sole" in source


class TestActionableIsTheStrategysOwnWord:
    """The flag restates what each strategy's plan builder already accepts; nothing is added."""

    def test_swing_actionable_is_the_tradeable_setups_and_nothing_else(self) -> None:
        assert frozenset({Setup.FLAG, Setup.EP}) == TRADEABLE_SETUPS
        assert "TRADEABLE_SETUPS" in inspect.getsource(overlap_service._swing)

    def test_the_vocabulary_is_the_instrument_page_s(self) -> None:
        """`/instruments/{symbol}/appearances` and `/overlap` describe the same row in the same
        words, so a person cannot read two facts about one stock."""
        assert overlap_service._VBT_STATE == instrument_appearances._VBT_STATE
        for strategy in overlap_service.Strategy:
            assert strategy.value in {kind.value for kind in instrument_appearances.AppearanceKind}
