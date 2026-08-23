"""M22's acceptance: the web app can see a basket and can never place an order.

The desk's non-negotiable #1 is that ``packages/execution`` is the only path to an order. M22 puts
the desk's *output* on a web surface, and the entire value of doing that safely rests on the
surface being read-only — not by convention, and not because nobody has added a POST yet, but
structurally, in a way that fails a test the moment someone does.

This is also the SEBI gate. The desk places orders for one account, its owner's. A web surface
that could place an order for a logged-in user is a different regulated activity.
"""

from __future__ import annotations

import inspect

import pytest

from baskfy_api import baskets as basket_data
from baskfy_api.app import create_app
from baskfy_api.routers import baskets, curated_create

MUTATING = ("post", "put", "patch", "delete")

#: What `app.openapi()` returns, to the depth these tests read it.
OpenApiSpec = dict[str, dict[str, dict[str, object]]]

#: Paths matching "basket" that serve a non-GET verb **on purpose**, with the reason each one
#: does not cross the order gate this file guards. A mutating basket route that is not listed
#: here still fails `test_every_basket_route_is_a_get`, so adding one stays a deliberate act.
#:
#: SC8 added `POST /api/v1/cb/baskets` and this invariant went red. It is scoped rather than
#: deleted because the gate is about *execution*, not about the HTTP verb: `docs/smallcase/
#: 02-scope-and-gating.md` puts "the create/customize builder and private baskets" in **Track
#: A — build now, fully live**, and puts web-app execution in Track C with the instruction
#: that "the M23-era test (no order route reachable from the web app) stays green and is
#: extended to the new routes". Extending it is what this table does.
#:
#: `POST /api/v1/cb/baskets` writes one PRIVATE/STOCK `cb_basket` row plus its GENESIS version
#: and constituents for the sole tenant. It moves no shares, no money and no broker state; its
#: router imports no gateway (`test_curated_create.py::test_create_router_has_no_broker_path`
#: asserts `OrderGateway`, `place_order` and `/execute` are absent from that source, and
#: `test_the_whole_api_has_no_order_route` below still covers it by path). Naming a basket for
#: yourself is not the regulated activity in the module docstring above — placing an order for
#: a logged-in user is, and nothing here can.
DELIBERATE_MUTATING_BASKET_ROUTES: dict[str, set[str]] = {
    "/api/v1/cb/baskets": {"post"},
}


@pytest.fixture(scope="module")
def spec() -> OpenApiSpec:
    return create_app().openapi()


class TestNoOrderPlacingRouteIsReachable:
    def test_every_basket_route_is_a_get(self, spec: OpenApiSpec) -> None:
        routes = {p: sorted(spec["paths"][p]) for p in spec["paths"] if "basket" in p}
        assert routes, "M22's routes are not registered at all"
        for path, methods in routes.items():
            allowed = {"get"} | DELIBERATE_MUTATING_BASKET_ROUTES.get(path, set())
            assert set(methods) <= allowed, f"{path} exposes {methods}"

    def test_the_mutating_exemptions_are_still_real_routes(self, spec: OpenApiSpec) -> None:
        """A stale exemption is a hole nobody is watching; it must fail loudly, not linger."""
        for path, verbs in DELIBERATE_MUTATING_BASKET_ROUTES.items():
            assert path in spec["paths"], f"{path} is exempted but no longer served"
            assert verbs <= set(spec["paths"][path]), (
                f"{path} is exempted for {sorted(verbs)} but serves {sorted(spec['paths'][path])}"
            )

    def test_the_exempted_routers_cannot_reach_the_execution_package(self) -> None:
        """The exemption is only defensible while the router behind it has no order path."""
        source = inspect.getsource(curated_create)
        for forbidden in ("baskfy_execution", "OrderGateway", "place_order", "kiteconnect"):
            assert forbidden not in source, f"curated_create.py references {forbidden}"

    def test_the_whole_api_has_no_order_route(self, spec: OpenApiSpec) -> None:
        """Not just the basket router — the entire surface the web app can reach.

        M41 adds ``POST /brokers/{id}/connect``. That starts an OAuth redirect (or explains why
        it cannot); it never places an order. The forbidden token used to be the substring
        ``broker``, which would have blocked the connect route for the wrong reason.
        """
        forbidden = ("/order", "execute", "gtt", "trade/place", "place_order")
        offenders: list[str] = []
        for path in spec["paths"]:
            for method in spec["paths"][path]:
                if method not in MUTATING:
                    continue
                lower = path.lower()
                if method == "post" and "/brokers/" in lower and lower.endswith("/connect"):
                    continue
                if any(word in lower for word in forbidden):
                    offenders.append(f"{method.upper()} {path}")
        assert offenders == [], f"the web API exposes {offenders}"

    def test_the_router_source_declares_no_mutating_verb(self) -> None:
        """Structural, over the source: a route added in a hurry fails here."""
        source = inspect.getsource(baskets)
        for verb in MUTATING:
            assert f"@router.{verb}" not in source, f"baskets.py declares a {verb.upper()}"

    def test_the_router_cannot_reach_the_execution_package(self) -> None:
        source = inspect.getsource(baskets)
        for forbidden in ("baskfy_execution", "OrderGateway", "place_order", "kiteconnect"):
            assert forbidden not in source, f"baskets.py references {forbidden}"

    def test_the_data_layer_cannot_reach_it_either(self) -> None:
        """The router is thin; the layer under it is where an import would actually be added."""
        source = inspect.getsource(basket_data)
        for forbidden in ("baskfy_execution", "OrderGateway", "place_order", "kiteconnect"):
            assert forbidden not in source, f"the basket data layer references {forbidden}"

    def test_it_reads_the_desk_schema_and_only_reads(self) -> None:
        """It queries the desk's own records. It must never write to them."""
        source = inspect.getsource(baskets).lower()
        for statement in ("insert into", "update ", "delete from", "drop ", "truncate"):
            assert statement not in source, f"the basket router contains {statement.strip()!r}"


class TestItShowsWhatTheDeskWouldShow:
    def test_the_plan_response_carries_the_pledged_columns(self) -> None:
        """M22 §2: every column the Jinja page shows, including the pledged-share flags.

        `pledged` reaches the web app through the order's own fields; the desk records it per
        order and the page renders it. A plan view that dropped it would hide the one flag that
        changes what a sell actually does to collateral margin.
        """
        fields = set(baskets.RebalanceOrderOut.model_fields)
        assert {"symbol", "side", "planned_qty", "planned_ref_price", "status"} <= fields
        assert {"filled_qty", "avg_fill_price", "order_id", "reconciled_at"} <= fields

    def test_the_basket_response_surfaces_contaminated_symbols(self) -> None:
        """A clean-looking list would assert a cleanliness nobody has established."""
        assert "suspect_symbols" in baskets.BasketOut.model_fields

    def test_the_basket_carries_the_score_components(self) -> None:
        """The score is /100 from six parts. Showing the total without them is a number, not a
        reason."""
        fields = set(baskets.BasketRowOut.model_fields)
        assert {
            "a_trend",
            "b_momentum",
            "c_sharpe",
            "d_consistency",
            "e_liquidity",
            "f_penalty",
        } <= fields

    def test_it_imports_the_desks_config_rather_than_restating_it(self) -> None:
        """A restated weight would let the web app show a basket the desk would never trade."""
        source = inspect.getsource(basket_data)
        assert "from app import config" in source
        for weight in ("MOMENTUM_BLEND =", "SHARPE_BLEND =", "CASH_BANDS ="):
            assert weight not in source, f"{weight} is restated instead of imported"
