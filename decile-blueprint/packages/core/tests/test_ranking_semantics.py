"""Ranking semantics — modes and scopes (docs/ranking/PLAN.md Phase 1)."""

from __future__ import annotations

import datetime as dt

from baskfy_core.screen_definition import ExtraFactor, ScreenDefinition
from baskfy_core.screener import build_screen_query


AS_OF = dt.date(2026, 8, 18)


def _definition(**kwargs: object) -> ScreenDefinition:
    payload: dict[str, object] = {
        "index": "nifty-500",
        "sort_by": "ret_12m",
        "sort_direction": "desc",
    }
    payload.update(kwargs)
    return ScreenDefinition.model_validate(payload)


def test_defaults_preserve_docs_06_filtered_composite() -> None:
    definition = _definition()
    assert definition.ranking_mode == "composite"
    assert definition.ranking_scope == "filtered_results"


def test_single_mode_rejects_extra_factors() -> None:
    try:
        _definition(
            ranking_mode="single",
            factor_two=ExtraFactor(enabled=True, sort_by="vol_12m", sort_direction="asc"),
        )
    except ValueError as exc:
        assert "single" in str(exc)
    else:
        raise AssertionError("expected ValueError for single + factor_two")


def test_within_sector_is_reserved() -> None:
    try:
        _definition(ranking_scope="within_sector")
    except ValueError as exc:
        assert "within_sector" in str(exc)
    else:
        raise AssertionError("expected ValueError for within_sector")


def test_fixed_universe_emits_pre_ranked_cte() -> None:
    filtered = build_screen_query(_definition(ranking_scope="filtered_results"), AS_OF)
    fixed = build_screen_query(_definition(ranking_scope="fixed_universe"), AS_OF)
    filtered_sql = str(filtered.statement.compile(compile_kwargs={"literal_binds": False}))
    fixed_sql = str(fixed.statement.compile(compile_kwargs={"literal_binds": False}))
    assert "pre_ranked" not in filtered_sql
    assert "pre_ranked" in fixed_sql


def test_sequential_orders_by_primary_then_tiebreakers() -> None:
    from baskfy_core.screener import _ranked_pipeline

    pipeline = _ranked_pipeline(
        _definition(
            ranking_mode="sequential",
            factor_two=ExtraFactor(enabled=True, sort_by="vol_12m", sort_direction="asc"),
        ),
        AS_OF,
    )
    rendered = [str(clause) for clause in pipeline.order_by]
    assert any("r1" in clause for clause in rendered)
    assert rendered[0].startswith("r1") or "r1" in rendered[0]
