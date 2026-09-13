"""Portfolio-aware selection must not mutate SCORE."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from baskfy_core.rank_buffer import HeldName
from baskfy_core.ranking_selection import (
    QualityRow,
    SelectionInput,
    assert_score_unchanged,
    quality_rows_from_scored,
    select_from_ranks,
)


def test_select_from_ranks_uses_buffer_and_preserves_scores() -> None:
    ranked = (
        QualityRow(1, "AAA", "A", rank=1, score=90.0),
        QualityRow(2, "BBB", "B", rank=2, score=80.0),
        QualityRow(3, "CCC", "C", rank=3, score=70.0),
        QualityRow(4, "DDD", "D", rank=4, score=60.0),
        QualityRow(5, "EEE", "E", rank=5, score=50.0),
    )
    holdings = (
        HeldName(1, "AAA", "A", quantity=Decimal(10)),
        HeldName(5, "EEE", "E", quantity=Decimal(10)),  # rank 5 → exit when top_n=2, buffer=1
    )
    score_before = {r.symbol: r.score for r in ranked}

    plan = select_from_ranks(
        SelectionInput(ranked=ranked, holdings=holdings, top_n=2, hold_buffer=1)
    )

    assert_score_unchanged(score_before, {r.symbol: r.score for r in ranked})
    assert {r.symbol for r in plan.exits} == {"EEE"}
    assert {r.symbol for r in plan.entries} == {"BBB"}
    assert {r.symbol for r in plan.holds} == {"AAA"}


def test_assert_score_unchanged_on_dataframe() -> None:
    frame = pd.DataFrame(
        {
            "instrument_id": [1, 2],
            "symbol": ["AAA", "BBB"],
            "SCORE": [90.0, 80.0],
            "screen_rank": [1, 2],
            "name": ["A", "B"],
        }
    )
    snapshot = frame.copy()
    rows = quality_rows_from_scored(frame)
    plan = select_from_ranks(
        SelectionInput(
            ranked=rows,
            holdings=(HeldName(1, "AAA", "A", quantity=Decimal(1)),),
            top_n=1,
            hold_buffer=0,
        )
    )
    assert plan.holds[0].symbol == "AAA"
    # Selection never touched the frame.
    assert_score_unchanged(snapshot, frame)


def test_assert_score_unchanged_detects_mutation() -> None:
    before = pd.DataFrame({"SCORE": [1.0, 2.0]})
    after = pd.DataFrame({"SCORE": [1.0, 99.0]})
    with pytest.raises(AssertionError, match="SCORE"):
        assert_score_unchanged(before, after)
