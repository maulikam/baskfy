"""A sleeve allocation is a plan, not a buy list (M34).

The feature answers "how much goes where" for a portfolio run as several screens. The line it must
not cross is the one M22 and M26 drew: **execution stays in the desk console** until D3 has a
written answer, and a web surface that emits share counts is one step from an order.

Amounts and weights need no price. Share counts do. So the structural guarantee is that neither
the pure allocator nor the router ever touches a price — asserted over the source, because a
future change would look entirely reasonable line by line.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from baskfy_api.app import create_app
from baskfy_api.routers import sleeves as router
from baskfy_core import sleeves as core

MUTATING = ("post", "patch", "delete")

OpenApiSpec = dict[str, dict[str, dict[str, object]]]


@pytest.fixture(scope="module")
def spec() -> OpenApiSpec:
    return create_app().openapi()


class TestItCannotBecomeAnOrderList:
    def test_the_allocator_takes_no_price_and_returns_no_quantity(self) -> None:
        """The structural half: with no price in, a share count cannot come out."""
        source = inspect.getsource(core)
        for forbidden in ("price", "quantity", "shares", "lot_size"):
            assert forbidden not in source.lower(), f"baskfy_core.sleeves mentions {forbidden}"

    def test_the_allocation_row_carries_an_amount_and_a_weight_only(self) -> None:
        row = core.AllocationRow(
            symbol="CUPID", weight_pct=Decimal("6.67"), amount=Decimal(266_000)
        )
        assert set(row.__slots__) == {"symbol", "weight_pct", "amount"}

    def test_the_router_reaches_no_broker_and_no_execution_package(self) -> None:
        source = inspect.getsource(router)
        forbidden_names = (
            "baskfy_execution",
            "kiteconnect",
            "place_order",
            "place_gtt",
            "/execute",
        )
        for forbidden in forbidden_names:
            assert forbidden not in source, f"routers/sleeves.py names {forbidden}"

    def test_no_route_here_places_anything(self, spec: OpenApiSpec) -> None:
        """PUT exists -- a division is edited. Nothing else mutating does."""
        paths = {
            p: sorted(spec["paths"][p]) for p in spec["paths"] if "sleeve" in p or "allocation" in p
        }
        assert paths, "M34's routes are not registered"
        for path, methods in paths.items():
            assert set(methods) <= {"get", "put"}, f"{path} exposes {methods}"


class TestItStatesRatherThanRecommends:
    def test_the_regime_cap_is_an_input_the_caller_chooses(self) -> None:
        """`allocate` cannot fetch a tier, so it cannot apply one nobody asked for."""
        signature = inspect.signature(core.allocate)
        assert signature.parameters["equity_cap_pct"].default is None

    def test_the_cap_is_not_applied_unless_requested(self, spec: OpenApiSpec) -> None:
        operation = spec["paths"]["/api/v1/portfolios/{portfolio_id}/allocation"]["get"]
        assert isinstance(operation, dict)
        parameters = operation["parameters"]
        assert isinstance(parameters, list)
        flag = next(p for p in parameters if p["name"] == "apply_regime_cap")
        assert flag["schema"]["default"] is False, "the cap must be opt-in"

    def test_neither_module_uses_advice_language(self) -> None:
        """Baskfy publishes no advice and is not SEBI-registered. It says so on every page.

        "Under R2 the strategy caps equity at 70%" is a fact about the strategy. "We recommend you
        deploy 70 lakh" is advice. The difference is the whole reason the wording was chosen, so
        the words are asserted rather than left to a future editor's judgement.
        """
        for module in (core, router):
            prose = inspect.getsource(module).lower()
            advice = ("we recommend", "you should buy", "you should sell", "recommended for you")
            for phrase in advice:
                assert phrase not in prose, f"{module.__name__} says '{phrase}'"


class TestTheManualSleeveIsNeverTouched:
    def test_it_is_reported_and_never_allocated(self) -> None:
        result = core.allocate(
            [core.SleeveSpec(name="Mine", kind=core.MANUAL, capital=Decimal(2_000_000))],
            equity_cap_pct=Decimal(40),
        )
        assert result.sleeves[0].rows == ()
        assert result.sleeves[0].capital == Decimal(2_000_000)
