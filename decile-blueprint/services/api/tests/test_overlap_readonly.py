"""`/overlap` can list the day's candidates, take a person's word on a headline tag, and can
never trade, scan, rank or size.

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
page that decides when the sleeves run, and it is not.

WHY THERE IS A PUT AND A DELETE, AND WHY THAT IS STILL SAFE
-----------------------------------------------------------
`baskfy_core.catalyst_tags` settled the order for the headline tag: a keyword baseline, Laya's
answer beside it, **corrections collected against both**, and only then a fine-tune. The
corrections have to be written somewhere, and the chip is where a person sees the tag they
disagree with. So the surface has two writes on one path — a correction and its removal — and
they are the whole of what may be written: one table, `catalyst_tag_correction`, holding a label
on display context that never reached a rank, a size or an order. `DELIBERATE_MUTATING_ROUTES`
below is an exact equality, so a third verb or a second path is a second decision.
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

#: Every non-GET route on the surface, and why it is allowed: a person's correction of a headline
#: tag, and its removal. A route that is not in this table fails
#: `test_every_route_is_a_get_except_the_documented_writes` the moment it is added.
DELIBERATE_MUTATING_ROUTES: dict[str, set[str]] = {
    "/api/v1/overlap/tags": {"put", "delete"},
}

#: The read model: everything that serves `GET /overlap`. These are scanned for a write verb;
#: the corrections section at the bottom of the module is the one place a write may live.
READ_FUNCTIONS = (
    overlap_service.overlap,
    overlap_service._tag,
    overlap_service.laya_answers,
    overlap_service.corrections_for,
    overlap_service._swing,
    overlap_service._volume_breakout,
    overlap_service._three_weeks_tight,
    overlap_service._screens,
)


@pytest.fixture(scope="module")
def spec() -> OpenApiSpec:
    return create_app().openapi()


def _overlap_paths(spec: OpenApiSpec) -> list[str]:
    return sorted(path for path in spec["paths"] if "/overlap" in path)


class TestTheSurfaceIsRegisteredAndReadOnly:
    def test_the_three_routes_exist(self, spec: OpenApiSpec) -> None:
        assert _overlap_paths(spec) == [
            "/api/v1/overlap",
            "/api/v1/overlap/tags",
            "/api/v1/overlap/tags/export",
        ]
        assert set(spec["paths"]["/api/v1/overlap"]) == {"get"}
        assert set(spec["paths"]["/api/v1/overlap/tags/export"]) == {"get"}

    def test_every_route_is_a_get_except_the_documented_writes(self, spec: OpenApiSpec) -> None:
        for path in _overlap_paths(spec):
            allowed = {"get"} | DELIBERATE_MUTATING_ROUTES.get(path, set())
            assert set(spec["paths"][path]) <= allowed, (
                f"{path} exposes {sorted(spec['paths'][path])}; /overlap is read-only apart from "
                f"a headline-tag correction"
            )

    def test_the_exemption_is_still_a_real_route(self, spec: OpenApiSpec) -> None:
        """A stale exemption is a hole nobody is watching."""
        for path, verbs in DELIBERATE_MUTATING_ROUTES.items():
            assert path in spec["paths"], f"{path} is exempted but no longer served"
            assert verbs <= set(spec["paths"][path])

    def test_the_mutating_routes_are_the_correction_and_nothing_else(
        self, spec: OpenApiSpec
    ) -> None:
        """Exact equality, not a subset: a third verb or a second path would be a second
        decision."""
        mutating = {
            path: {verb for verb in spec["paths"][path] if verb != "get"}
            for path in _overlap_paths(spec)
            if any(verb != "get" for verb in spec["paths"][path])
        }
        assert mutating == DELIBERATE_MUTATING_ROUTES

    def test_the_router_declares_only_the_documented_mutating_decorators(self) -> None:
        """Over the source, so a route added and not yet registered is still caught."""
        source = inspect.getsource(overlap_router)
        assert source.count("@router.put(") == 1, "the correction, and nothing else"
        assert source.count("@router.delete(") == 1, "its removal, and nothing else"
        assert "@router.post(" not in source, "routers/overlap.py declares a POST"
        assert "@router.patch(" not in source, "routers/overlap.py declares a PATCH"

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
        """An intersection of stored rows: `select` only. No insert, update, delete or commit
        in any function that serves the GET."""
        for function in READ_FUNCTIONS:
            source = inspect.getsource(function)
            for verb in (
                "session.add",
                "insert(",
                "update(",
                "delete(",
                ".commit(",
                ".execute(text",
            ):
                assert verb not in source, f"{function.__name__} must not {verb}"

    def test_the_writer_touches_the_corrections_table_and_nothing_else(self) -> None:
        """The one write is a label on display context. It may construct and delete a
        `CatalystTagCorrection` and must name no strategy table, no plan and no position."""
        source = inspect.getsource(overlap_service.correct_tag) + inspect.getsource(
            overlap_service.clear_correction
        )
        assert "CatalystTagCorrection(" in source
        for forbidden in (
            "SwSetupDaily",
            "SwWatch",
            "SwPlan",
            "SwPosition",
            "VbSignalDaily",
            "TwStateDaily",
            "TwSignalDaily",
            "Screen",
            ".commit(",
        ):
            assert forbidden not in source, f"the correction writer names {forbidden}"

    def test_every_route_requires_an_authenticated_principal(self) -> None:
        handlers = [
            value
            for name, value in vars(overlap_router).items()
            if callable(value)
            and getattr(value, "__module__", "") == overlap_router.__name__
            and name.startswith(("get_", "put_", "delete_"))
        ]
        assert len(handlers) == 4, f"found {len(handlers)} route handlers, expected four"
        for handler in handlers:
            hints = typing.get_type_hints(handler, include_extras=True)
            assert "principal" in hints, f"{handler.__name__} takes no principal"

    def test_the_strategies_are_read_for_the_sole_tenant_only(self) -> None:
        """The screens are the caller's; the sleeves are one person's. The read resolves the
        sole tenant and passes ``None`` for anyone else rather than trusting the principal;
        the writes and the export refuse anyone else outright, the way `/swing`'s do."""
        source = inspect.getsource(overlap_router)
        assert source.count("scoped_sole_user_id(") == 4
        assert "strategies_user_id=sole" in source
        for handler in (
            overlap_router.put_tag,
            overlap_router.delete_tag,
            overlap_router.get_tags_export,
        ):
            handler_source = inspect.getsource(handler)
            assert "user_id = await scoped_sole_user_id(" in handler_source
            assert "except Problem" not in handler_source, (
                f"{handler.__name__} swallows the refusal"
            )


class TestActionableIsTheStrategysOwnWord:
    """The flag restates what each strategy's plan builder already accepts; nothing is added."""

    def test_swing_actionable_is_the_tradeable_setups_and_nothing_else(self) -> None:
        assert frozenset({Setup.FLAG, Setup.EP}) == TRADEABLE_SETUPS
        assert "TRADEABLE_SETUPS" in inspect.getsource(overlap_service._swing)

    def test_the_tag_is_read_from_the_headline_alone(self) -> None:
        """The baseline sees a string. Not the setup, not the price, not the filing — so it can
        never become a feature of the row it sits on, and `docs` can call it context truthfully.
        A correction is keyed on the same string and nothing else."""
        source = (
            inspect.getsource(overlap_service._tag)
            + inspect.getsource(overlap_service.laya_answers)
            + inspect.getsource(overlap_service.corrections_for)
            + inspect.getsource(overlap_service.correct_tag)
        )
        assert "tag_headline(view.headline)" in source
        assert "cache_key(headline)" in source
        for forbidden in ("close", "score", "rvol", "gap_pct", "setup", "state"):
            assert forbidden not in source, f"the tag must not read {forbidden}"

    def test_the_vocabulary_is_the_instrument_page_s(self) -> None:
        """`/instruments/{symbol}/appearances` and `/overlap` describe the same row in the same
        words, so a person cannot read two facts about one stock."""
        assert overlap_service._VBT_STATE == instrument_appearances._VBT_STATE
        for strategy in overlap_service.Strategy:
            assert strategy.value in {kind.value for kind in instrument_appearances.AppearanceKind}
