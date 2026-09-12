"""AF I.1 — version diff is pure and refuses a same-version compare."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import pytest
from sqlalchemy import ColumnElement

from baskfy_api.app import create_app
from baskfy_api.curated_visibility import visible_baskets
from baskfy_api.routers import curated_versions, explore, watchlist
from baskfy_api.routers.curated_versions import diff_weight_maps
from baskfy_core.models.curated_baskets import BASKET_VISIBILITY

WeightMap = dict[str, tuple[str | None, Decimal]]


def test_diff_weight_maps_reports_added_removed_and_changed() -> None:
    """Old code had no consumer diff; an empty result for a real change must fail this."""
    left: WeightMap = {
        "AAA": ("Alpha", Decimal("0.5000")),
        "BBB": ("Beta", Decimal("0.5000")),
    }
    right: WeightMap = {
        "AAA": ("Alpha", Decimal("0.4000")),
        "CCC": ("Gamma", Decimal("0.6000")),
    }
    added, removed, changed, unchanged = diff_weight_maps(left, right)
    assert [line.symbol for line in added] == ["CCC"]
    assert [line.symbol for line in removed] == ["BBB"]
    assert [line.symbol for line in changed] == ["AAA"]
    assert changed[0].weight_pct_from == Decimal("50.00")
    assert changed[0].weight_pct_to == Decimal("40.00")
    assert unchanged == 0


def test_diff_weight_maps_counts_unchanged() -> None:
    left: WeightMap = {"AAA": ("Alpha", Decimal("1.0000"))}
    right: WeightMap = {"AAA": ("Alpha", Decimal("1.0000"))}
    added, removed, changed, unchanged = diff_weight_maps(left, right)
    assert added == removed == changed == []
    assert unchanged == 1


def test_explore_mounts_version_and_instrument_watch_routes() -> None:
    paths = set(create_app().openapi()["paths"])
    assert "/api/v1/explore/{slug}/versions" in paths
    assert "/api/v1/explore/{slug}/versions/diff" in paths
    assert "/api/v1/watchlist/instruments" in paths
    assert "/api/v1/watchlist/discover-preferences" in paths


def _visibility_asked_for(clauses: Sequence[ColumnElement[bool]]) -> set[str]:
    """The string literals a visibility predicate compiles to."""
    values: set[str] = set()
    for clause in clauses:
        values.update(value for value in clause.compile().params.values() if isinstance(value, str))
    return values


def test_every_basket_route_asks_for_the_same_visibility() -> None:
    """A mounted route is not a working one.

    The version routes shipped filtering on ``visibility == "LISTED"`` — a value
    ``BASKET_VISIBILITY`` does not contain and a check constraint forbids — so both of them
    answered 404 for every basket in the table while the two tests below still passed: one
    asserts the path is in the OpenAPI document, the other that the router carries it. Neither
    makes a request, and neither could have caught it.

    This asserts the predicate against the model's own vocabulary, so a second copy that
    invents a value fails here rather than in a 404 nobody reads.
    """
    shared = _visibility_asked_for(visible_baskets())
    assert shared == {"PUBLISHED"}, shared
    assert shared <= set(BASKET_VISIBILITY), "a basket cannot hold a visibility the model forbids"

    for module in (curated_versions, explore):
        asked = _visibility_asked_for(module._visible())
        assert asked == shared, f"{module.__name__} asks for {asked}, not {shared}"


def test_version_and_watchlist_routers_expose_own_paths() -> None:
    version_paths = {getattr(route, "path", "") for route in curated_versions.router.routes}
    watch_paths = {getattr(route, "path", "") for route in watchlist.router.routes}
    assert "/explore/{slug}/versions/diff" in version_paths
    assert "/watchlist/instruments" in watch_paths
    assert "/watchlist/discover-preferences" in watch_paths


def test_mutating_watchlist_instrument_paths_keep_watchlist_token() -> None:
    """explore's contract: every mutating explore-mounted route must name watchlist."""
    for route in explore.router.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        for method in methods:
            if method in {"POST", "PUT", "PATCH", "DELETE"}:
                assert "watchlist" in path.lower(), f"{method} {path}"


@pytest.mark.parametrize(
    ("from_w", "to_w"),
    [
        ({}, {"ZZZ": (None, Decimal("1"))}),
        ({"ZZZ": (None, Decimal("1"))}, {}),
    ],
)
def test_diff_handles_one_sided_maps(
    from_w: dict[str, tuple[str | None, Decimal]],
    to_w: dict[str, tuple[str | None, Decimal]],
) -> None:
    added, removed, changed, unchanged = diff_weight_maps(from_w, to_w)
    assert unchanged == 0
    assert changed == []
    assert len(added) + len(removed) == 1
