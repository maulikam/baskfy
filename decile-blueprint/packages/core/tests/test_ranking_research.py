"""Phase 1.3's research names delegate to the C1 stored factors — one formula per number.

docs/ranking/PLAN.md Phase 2 C1 defined each research candidate exactly and stored it; gate 2.A G6
requires ``ranking_research`` to stop carrying a second implementation. So these tests assert the
promotion (names, preferences, rankability) and that the research frame *is* the stored row.
"""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path

import numpy as np
import polars as pl

from baskfy_core import ranking_research as rr
from baskfy_core.factor_registry import FACTORS
from baskfy_core.factors import compute_factors
from baskfy_core.ranking import FactorPreference

AS_OF = dt.date(2026, 8, 18)
MODULE = Path(rr.__file__)


def weekdays(start: dt.date, end: dt.date) -> list[dt.date]:
    days: list[dt.date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


def test_research_keys_are_not_sort_by_factors() -> None:
    for key in rr.RESEARCH_CANDIDATE_KEYS:
        assert key not in FACTORS, f"{key} must not be in FACTORS: the registry carries the C1 key"


def test_every_research_name_is_promoted_to_a_stored_c1_factor() -> None:
    assert rr.PROMOTED_TO == {
        "atr_extension": "atr_ext_20",
        "ma_slope": "ma50_slope_20",
        "efficiency_ratio": "eff_ratio_63",
        "acceleration_nonoverlap": "accel_21_105",
        "excess_return_common_index": "excess_ret_12m",
    }
    for stored in rr.PROMOTED_TO.values():
        assert FACTORS[stored].is_stored


def test_the_phase_1_3_corrections_survive_in_the_registry() -> None:
    """Excess over a common index is a filter/column (it cannot reorder); ATR extension is a
    target range; slope, efficiency and non-overlapping acceleration are higher-is-better."""
    assert rr.RESEARCH_PREFERENCE["excess_return_common_index"] == FactorPreference.ELIGIBILITY
    assert FACTORS["excess_ret_12m"].rankable is False
    assert rr.RESEARCH_PREFERENCE["atr_extension"] == FactorPreference.TARGET_RANGE
    for name in ("ma_slope", "efficiency_ratio", "acceleration_nonoverlap"):
        assert rr.RESEARCH_PREFERENCE[name] == FactorPreference.HIGHER
        assert FACTORS[rr.PROMOTED_TO[name]].rankable is True


def test_the_module_carries_no_arithmetic_of_its_own() -> None:
    """No NumPy, no pandas, no binary arithmetic: a second formula cannot hide in it."""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])
    assert "numpy" not in imported
    assert "pandas" not in imported
    # `X | Y` in a type annotation is a BinOp too; arithmetic is anything else.
    arithmetic = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and not isinstance(node.op, ast.BitOr)
    ]
    assert arithmetic == []


def test_the_research_frame_is_the_stored_row_under_the_research_names() -> None:
    days = weekdays(dt.date(2024, 1, 1), AS_OF)
    rng = np.random.default_rng(3)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, len(days))))
    bars = pl.DataFrame(
        {
            "instrument_id": [7] * len(days),
            "date": days,
            "close": closes,
            "close_raw": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "volume_raw": [10_000.0] * len(days),
        }
    )
    index = pl.DataFrame({"date": days, "level": 20_000.0 * np.exp(np.linspace(0, 0.2, len(days)))})

    research = rr.research_frame(bars, AS_OF, days, benchmark=index, market_benchmark=index)
    stored = compute_factors(bars, AS_OF, days, index, market_benchmark=index).frame

    assert research.columns == ["instrument_id", *rr.PROMOTED_TO]
    row = research.to_dicts()[0]
    expected = stored.to_dicts()[0]
    for name, column in rr.PROMOTED_TO.items():
        assert row[name] == expected[column], name
        assert row[name] is not None, f"{name} is NULL, so the comparison proves nothing"
