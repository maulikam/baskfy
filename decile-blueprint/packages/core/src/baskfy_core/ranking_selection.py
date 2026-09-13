"""Portfolio-aware selection over a quality rank — does not mutate SCORE.

docs/ranking/PLAN.md Phase 1.4. Stock quality rank (desk SCORE or any screen rank) and
portfolio selection are separate outputs. This module takes an already-ranked screen and
current holdings, and returns enter / hold / exit lists via the existing rank-buffer rule
(:func:`baskfy_core.rank_buffer.plan_rebalance`).

Hard rules
----------
* Selection **must not mutate SCORE** (or any quality column). Ranks are inputs; the scorer
  is upstream.
* Does not place orders. Desk non-negotiable #1 (confirm path) is untouched.
* Does not invent a second hold-band formula — one rule lives in ``rank_buffer``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import pandas as pd

from baskfy_core.rank_buffer import (
    HeldName,
    RebalancePlan,
    ScreenRank,
    plan_rebalance,
)

__all__ = [
    "QualityRow",
    "SelectionInput",
    "select_from_ranks",
    "assert_score_unchanged",
]

SCORE_COLUMN: Final = "SCORE"


@dataclass(frozen=True, slots=True)
class QualityRow:
    """One ranked name. ``score`` is informational; selection keys only on ``rank``."""

    instrument_id: int
    symbol: str
    name: str
    rank: int
    score: float | None = None


@dataclass(frozen=True, slots=True)
class SelectionInput:
    """Quality ranks + holdings. Selection never writes back into ``ranked``."""

    ranked: Sequence[QualityRow]
    holdings: Sequence[HeldName]
    top_n: int
    hold_buffer: int


def select_from_ranks(inp: SelectionInput) -> RebalancePlan:
    """Overlay holdings on a quality rank. Does not mutate SCORE.

    Delegates to :func:`plan_rebalance` so the web portfolio path and the ranking engine share
    one hold-band rule.
    """
    screen = tuple(
        ScreenRank(
            instrument_id=row.instrument_id,
            symbol=row.symbol,
            name=row.name,
            rank=row.rank,
        )
        for row in inp.ranked
    )
    return plan_rebalance(
        screen,
        tuple(inp.holdings),
        top_n=inp.top_n,
        hold_buffer=inp.hold_buffer,
    )


def assert_score_unchanged(
    before: Mapping[str, float | None] | pd.DataFrame,
    after: Mapping[str, float | None] | pd.DataFrame,
    *,
    score_column: str = SCORE_COLUMN,
) -> None:
    """Raise if selection (or anything) changed quality scores.

    Callers that hold a scored DataFrame should snapshot SCORE before selection and pass both
    frames here. Selection itself never touches the frame; this is the contract pin.
    """
    if isinstance(before, pd.DataFrame) and isinstance(after, pd.DataFrame):
        if score_column not in before.columns:
            raise ValueError(f"before frame has no {score_column!r} column")
        if score_column not in after.columns:
            raise ValueError(f"after frame has no {score_column!r} column")
        left = before[score_column]
        right = after[score_column]
        if not left.equals(right):
            raise AssertionError(f"{score_column} changed during selection — forbidden")
        return

    if set(before) != set(after):
        raise AssertionError("score maps have different symbol keys")
    for symbol, value in before.items():
        if after[symbol] != value:
            raise AssertionError(
                f"{score_column} for {symbol!r} changed: {value!r} -> {after[symbol]!r}"
            )


def quality_rows_from_scored(
    scored: pd.DataFrame,
    *,
    score_column: str = SCORE_COLUMN,
    rank_column: str = "screen_rank",
) -> tuple[QualityRow, ...]:
    """Build selection inputs from a frame ``rerank_survivors_by_desk_score`` already ordered."""
    if rank_column not in scored.columns and "rank" in scored.columns:
        rank_column = "rank"
    rows: list[QualityRow] = []
    for position, (_, row) in enumerate(scored.iterrows(), start=1):
        rank_val = row[rank_column] if rank_column in scored.columns else position
        score_val = row[score_column] if score_column in scored.columns else None
        score: float | None
        if score_val is None or (isinstance(score_val, float) and pd.isna(score_val)):
            score = None
        else:
            score = float(score_val)
        rows.append(
            QualityRow(
                instrument_id=int(row["instrument_id"]),
                symbol=str(row["symbol"]),
                name=str(row.get("name", row["symbol"])),
                rank=int(rank_val),
                score=score,
            )
        )
    return tuple(rows)


def held_from_quantities(
    holdings: Mapping[str, Decimal | int],
    *,
    id_by_symbol: Mapping[str, int],
    name_by_symbol: Mapping[str, str] | None = None,
) -> tuple[HeldName, ...]:
    """Convenience for tests: symbol → quantity map to :class:`HeldName`."""
    names = name_by_symbol or {}
    return tuple(
        HeldName(
            instrument_id=id_by_symbol[symbol],
            symbol=symbol,
            name=names.get(symbol, symbol),
            quantity=Decimal(quantity),
        )
        for symbol, quantity in holdings.items()
    )
