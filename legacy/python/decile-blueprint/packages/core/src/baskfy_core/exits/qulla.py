"""LV9 — Qullamaggie's exit rule, as the swing book already runs it, for TWT and VBT too.

MAULIK'S DECISION, 28 Sep 2026 (DECISIONS-LV LV9.0, in his words: *"based on Kristjan
Kullamägi's style"*, then "TWT and VBT, replacing their tested exits"). The rule itself lives in
one place, :func:`baskfy_core.swing.stops.manage`, and this module is the adapter the two other
sleeves call so there is **one** implementation of the partial, the breakeven and the trail:

* a third sold into strength between bar 3 and bar 5 after entry, if the position is green;
* the stop to breakeven after the partial, or once +1R is showing;
* the remainder trails the 10-day MA for fast names (ADR ≥ 6 %) or the 20-day MA, and is sold
  at the **next open** on a close below it;
* the GTT stays the hard stop (non-negotiable 4). Whether it fired is the reconciler's finding,
  not this rule's; a ``STOPPED_OUT`` answer here is reported and nothing else.

Everything is exchange prices (``close_raw`` space) — the same space the sleeves' positions and
GTTs are in — and the caller owns the calendar (``bars_since_entry``), as the swing book does.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from baskfy_core.swing.config import StopConfig
from baskfy_core.swing.stops import (
    ActionKind,
    ActionReason,
    DailyBar,
    OpenPosition,
    TrailMa,
    choose_trail,
    manage,
)

#: The reasons this rule writes onto a sell line or a closed position. ``PARTIAL`` is a sale that
#: leaves the position open; ``MA_TRAIL`` closes it.
PARTIAL: Final = "PARTIAL"
MA_TRAIL: Final = "MA_TRAIL"

#: The two moving averages the trail reads, the swing book's numbers (``docs/swing/04`` §6.3).
MA_FAST: Final = 10
MA_SLOW: Final = 20
#: ADR% window — twenty sessions, as the swing screen measures it.
ADR_WINDOW: Final = 20

__all__ = [
    "ADR_WINDOW",
    "MA_FAST",
    "MA_SLOW",
    "MA_TRAIL",
    "PARTIAL",
    "Decision",
    "OhlcBar",
    "Position",
    "adr_pct",
    "decide",
    "mean",
    "trail_for",
]


@dataclass(frozen=True, slots=True)
class OhlcBar:
    """One session of one name, exchange prices, oldest first when in a series."""

    date: dt.date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True, slots=True)
class Position:
    """What the rule needs to know about one open entry (one ``tw_position`` / ``vb_position``)."""

    position_id: int
    symbol: str
    entry_date: dt.date
    entry: Decimal
    initial_stop: Decimal
    stop: Decimal
    quantity: int
    partial_done: bool
    #: ``None`` until the first evening chooses it from the ADR; then fixed for the position.
    trail: TrailMa | None


@dataclass(frozen=True, slots=True)
class Decision:
    """What one close says about one position. At most one sell and one raise."""

    position_id: int
    symbol: str
    #: The trail in force after this decision (chosen tonight if it was unset).
    trail: TrailMa
    #: Shares to sell at the next open, and why (``PARTIAL`` or ``MA_TRAIL``). Zero is no sale.
    sell_quantity: int = 0
    sell_reason: str = ""
    #: A stop to raise the resting GTT to (breakeven), or ``None``.
    raise_to: Decimal | None = None
    #: The rule saw the hard stop traded through inside the bar. Reported, never acted on here.
    stop_hit: bool = False
    note: str = ""

    @property
    def holds(self) -> bool:
        return self.sell_quantity == 0 and self.raise_to is None and not self.stop_hit


def mean(values: Sequence[Decimal], window: int) -> Decimal | None:
    """The trailing mean of the last ``window`` values, or ``None`` before the window is full —
    never a partial average, which would sell a position on a number that is not the rule's."""
    if len(values) < window:
        return None
    return sum(values[-window:], Decimal(0)) / window


def adr_pct(series: Sequence[OhlcBar], window: int = ADR_WINDOW) -> Decimal | None:
    """Average daily range in percent over the last ``window`` bars: mean of ``high/low - 1``."""
    if len(series) < window:
        return None
    ranges = [
        (bar.high / bar.low - Decimal(1)) * Decimal(100) for bar in series[-window:] if bar.low > 0
    ]
    if len(ranges) < window:
        return None
    return (sum(ranges, Decimal(0)) / window).quantize(Decimal("0.01"))


def trail_for(series: Sequence[OhlcBar], config: StopConfig) -> TrailMa:
    """Choose the trail once, from the name's ADR; a name too young to have one trails the slow
    average, the conservative choice."""
    adr = adr_pct(series)
    if adr is None:
        return TrailMa.MA20
    return choose_trail(adr, config)


def decide(
    position: Position,
    series: Sequence[OhlcBar],
    *,
    on: dt.date,
    bars_since_entry: int,
    config: StopConfig,
) -> Decision | None:
    """The rule over today's close. ``None`` when the name did not print today.

    The series is the published history for the name, oldest first, ending on ``on``; the
    averages are computed here from its closes so both sleeves read the same numbers.
    """
    if not series or series[-1].date != on:
        return None
    trail = position.trail or trail_for(series, config)
    closes = [bar.close for bar in series]
    today = series[-1]
    bar = DailyBar(
        date=on,
        open=today.open,
        high=today.high,
        low=today.low,
        close=today.close,
        ma10=mean(closes, MA_FAST),
        ma20=mean(closes, MA_SLOW),
        bars_since_entry=bars_since_entry,
    )
    actions = manage(
        OpenPosition(
            symbol=position.symbol,
            entry_date=position.entry_date,
            entry=position.entry,
            initial_stop=position.initial_stop,
            stop=position.stop,
            quantity=position.quantity,
            partial_done=position.partial_done,
            trail=trail,
        ),
        bar,
        config,
    )
    sell_quantity = 0
    sell_reason = ""
    raise_to: Decimal | None = None
    stop_hit = False
    notes: list[str] = []
    for action in actions:
        if action.kind is ActionKind.STOPPED_OUT:
            stop_hit = True
            notes.append(f"the bar traded through the stop {position.stop}")
        elif action.kind is ActionKind.SELL_ALL:
            sell_quantity, sell_reason = position.quantity, MA_TRAIL
            level = bar.ma10 if trail is TrailMa.MA10 else bar.ma20
            notes.append(f"close {today.close} below the {trail.value} {level}")
        elif action.kind is ActionKind.SELL_PARTIAL:
            sell_quantity, sell_reason = action.quantity, PARTIAL
            notes.append(
                f"bar {bars_since_entry} after entry, green: sell {action.quantity} into strength"
            )
        elif action.kind is ActionKind.RAISE_STOP and action.new_stop is not None:
            raise_to = action.new_stop
            why = (
                "after the partial"
                if action.reason is ActionReason.BREAKEVEN_AFTER_PARTIAL
                else f"+{config.breakeven_after_r:g}R showing"
            )
            notes.append(f"stop to breakeven {action.new_stop} ({why})")
    return Decision(
        position_id=position.position_id,
        symbol=position.symbol,
        trail=trail,
        sell_quantity=sell_quantity,
        sell_reason=sell_reason,
        raise_to=raise_to,
        stop_hit=stop_hit,
        note="; ".join(notes),
    )
