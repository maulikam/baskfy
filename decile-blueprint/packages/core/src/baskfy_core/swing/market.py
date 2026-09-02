"""Breadth, the market gate, and progressive exposure (docs/swing/04 §8).

    "He does not fight the tape."

Two inputs decide whether new entries are allowed and how many: the market's breadth (his
gauge is how many stocks are up 25%+ in a month, and whether the index sits above its 10- and
20-day MAs) and — above all — the trader's own recent results. When breakouts are working the
book presses; when they fail it shrinks. The tiers are a ladder the book climbs one rung at a
time and falls down faster than it climbs.

Pure: the breadth snapshot is computed from a frame the worker supplies, the gate from the
snapshot and an index reading, the tier from the closed-trade history. The worker owns dates.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

import polars as pl

from baskfy_core.swing.config import MarketConfig

_PCT: Final = 100.0
_ZERO = Decimal(0)


class MarketGate(StrEnum):
    """GREEN: press. AMBER: new entries at reduced size. RED: no new entries, manage exits."""

    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"


@dataclass(frozen=True, slots=True)
class BreadthSnapshot:
    """One day's breadth over the swing universe."""

    constituent_count: int
    pct_up_strong_1m: float
    pct_new_52w_high: float
    pct_above_ma_slow: float


@dataclass(frozen=True, slots=True)
class IndexReading:
    """The benchmark index at the close, with its two MAs."""

    close: float
    ma_fast: float
    ma_slow: float

    @property
    def above_both(self) -> bool:
        return self.close > self.ma_fast and self.close > self.ma_slow

    @property
    def below_both(self) -> bool:
        return self.close < self.ma_fast and self.close < self.ma_slow


@dataclass(frozen=True, slots=True)
class ExposureTier:
    """How much the book may carry. ``level`` is the rung, 0 lowest."""

    level: int
    max_open_positions: int
    max_exposure_pct: float
    new_entries_allowed: bool


def breadth_snapshot(at_as_of: pl.DataFrame, config: MarketConfig) -> BreadthSnapshot:
    """Breadth from one as-of frame carrying ``ret_20``, ``close``, ``ma_slow``, ``high_1y``.

    ``high_1y`` is the trailing 52-week high **excluding** today, so a new high is
    ``close >= high_1y``; a caller with the stored ``factor_daily.high_1y`` (which includes the
    day) gets the same answer, since a close at the high equals it either way.
    """
    count = at_as_of.height
    if count == 0:
        return BreadthSnapshot(0, 0.0, 0.0, 0.0)
    strong = at_as_of.filter(pl.col("ret_20") >= config.strong_move_pct).height
    highs = at_as_of.filter(pl.col("close") >= pl.col("high_1y")).height
    above = at_as_of.filter(pl.col("close") > pl.col("ma_slow")).height
    return BreadthSnapshot(
        constituent_count=count,
        pct_up_strong_1m=round(strong / count * _PCT, 2),
        pct_new_52w_high=round(highs / count * _PCT, 2),
        pct_above_ma_slow=round(above / count * _PCT, 2),
    )


def market_gate(
    breadth: BreadthSnapshot, index: IndexReading | None, config: MarketConfig
) -> MarketGate:
    """The gate. Breadth decides; the index can only make it worse, never better.

    * GREEN needs breadth at or above ``green_min_pct_up`` and the index above both MAs (or no
      index reading at all — a missing benchmark is not a bear market).
    * RED is breadth at or below ``red_max_pct_up``, or the index below both MAs.
    * Everything else is AMBER.
    """
    if breadth.constituent_count == 0:
        return MarketGate.RED
    if index is not None and index.below_both:
        return MarketGate.RED
    if breadth.pct_up_strong_1m <= config.red_max_pct_up:
        return MarketGate.RED
    if breadth.pct_up_strong_1m >= config.green_min_pct_up and (index is None or index.above_both):
        return MarketGate.GREEN
    return MarketGate.AMBER


def _loss_streak(recent_r: Sequence[Decimal]) -> int:
    streak = 0
    for value in reversed(recent_r):
        if value < _ZERO:
            streak += 1
        else:
            break
    return streak


def exposure_tier(
    *,
    current_level: int,
    closed_r_multiples: Sequence[Decimal],
    gate: MarketGate,
    config: MarketConfig,
) -> ExposureTier:
    """The rung for tomorrow, from today's rung, the last closed trades and the gate.

    Rules, in precedence order:

    1. RED: drop to rung 0 and allow no new entries, whatever the results say.
    2. A loss streak of ``step_down_loss_streak`` closed trades: one rung down.
    3. The last ``lookback_trades`` closed trades net positive R, and the gate GREEN: one rung
       up. AMBER holds the rung.
    4. Otherwise hold.

    The ladder never skips a rung upward. Progressive exposure is a description of what a
    disciplined trader does with a winning streak; it is not a leverage schedule.
    """
    top = len(config.tiers) - 1
    level = min(max(current_level, 0), top)
    if gate is MarketGate.RED:
        positions, exposure = config.tiers[0]
        return ExposureTier(0, positions, exposure, new_entries_allowed=False)

    recent = list(closed_r_multiples)[-config.lookback_trades :]
    if _loss_streak(recent) >= config.step_down_loss_streak:
        level = max(level - 1, 0)
    elif (
        gate is MarketGate.GREEN
        and len(recent) >= config.lookback_trades
        and sum(recent, _ZERO) > _ZERO
    ):
        level = min(level + 1, top)
    positions, exposure = config.tiers[level]
    return ExposureTier(level, positions, exposure, new_entries_allowed=True)
