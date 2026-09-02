"""Stops and the day-by-day management of an open position (docs/swing/04 §6).

The rules, as he states them: stop at the low of the day (or the opening-range low); sell a
third to a half into strength on day 3-5; then trail the rest with the 10-day SMA for fast
movers or the 20-day for slower ones, out on a close below it; move the stop to breakeven once
the position has paid for its risk. None of it is discretionary here — the point of writing it
down is that the same rule fires on every position.

Everything is a pure function of the position and one daily bar. The worker calls
:func:`manage` after the close and turns the actions into the next morning's plan lines; the
hard stop itself is a GTT the desk armed on entry day, so a gap through it is the broker's
problem and not this module's.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from baskfy_core.swing.config import StopConfig

_ZERO = Decimal(0)


class TrailMa(StrEnum):
    MA10 = "MA10"
    MA20 = "MA20"


class StopMode(StrEnum):
    """Where the initial stop comes from."""

    LOW_OF_DAY = "LOW_OF_DAY"
    OPENING_RANGE_LOW = "OPENING_RANGE_LOW"


class ActionKind(StrEnum):
    SELL_PARTIAL = "SELL_PARTIAL"
    SELL_ALL = "SELL_ALL"
    RAISE_STOP = "RAISE_STOP"
    STOPPED_OUT = "STOPPED_OUT"
    HOLD = "HOLD"


class ActionReason(StrEnum):
    PARTIAL_INTO_STRENGTH = "PARTIAL_INTO_STRENGTH"
    BREAKEVEN_AFTER_PARTIAL = "BREAKEVEN_AFTER_PARTIAL"
    BREAKEVEN_AT_R = "BREAKEVEN_AT_R"
    CLOSE_BELOW_TRAIL_MA = "CLOSE_BELOW_TRAIL_MA"
    EP_FAILED_RED_ON_DAY = "EP_FAILED_RED_ON_DAY"
    HARD_STOP_HIT = "HARD_STOP_HIT"
    NOTHING_TO_DO = "NOTHING_TO_DO"


@dataclass(frozen=True, slots=True)
class OpenPosition:
    """The state the rules read. Quantities are shares; prices are exchange (raw) prices."""

    symbol: str
    entry_date: dt.date
    entry: Decimal
    initial_stop: Decimal
    stop: Decimal
    quantity: int
    partial_done: bool
    trail: TrailMa
    #: True for an EP bought on its gap day: a red close on that day is a failed EP.
    is_ep_gap_day: bool = False


@dataclass(frozen=True, slots=True)
class DailyBar:
    date: dt.date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    ma10: Decimal | None
    ma20: Decimal | None
    #: Bars since entry, counting the entry day as 0. Supplied by the caller, which owns the
    #: trading calendar (law 1: no calendar arithmetic in core).
    bars_since_entry: int


@dataclass(frozen=True, slots=True)
class Action:
    kind: ActionKind
    reason: ActionReason
    quantity: int = 0
    new_stop: Decimal | None = None


def initial_stop(
    *,
    entry: Decimal,
    low_of_day: Decimal,
    opening_range_low: Decimal | None,
    mode: StopMode,
) -> Decimal:
    """The entry-day stop. The tighter reference when the opening range is asked for and known."""
    if mode is StopMode.OPENING_RANGE_LOW and opening_range_low is not None:
        stop = max(opening_range_low, low_of_day)
    else:
        stop = low_of_day
    if stop >= entry:
        raise ValueError(f"stop {stop} is not below entry {entry}")
    return stop


def choose_trail(adr_pct: Decimal, config: StopConfig) -> TrailMa:
    """Fast movers trail the 10-day, slower names the 20-day."""
    if adr_pct >= Decimal(str(config.fast_trail_min_adr_pct)):
        return TrailMa.MA10
    return TrailMa.MA20


def partial_quantity(quantity: int, config: StopConfig) -> int:
    """How many shares the partial sale is; never the whole position, never zero unless tiny."""
    part = quantity * config.partial_numerator // config.partial_denominator
    return min(max(part, 0), max(quantity - 1, 0))


def _trail_level(position: OpenPosition, bar: DailyBar) -> Decimal | None:
    return bar.ma10 if position.trail is TrailMa.MA10 else bar.ma20


def manage(position: OpenPosition, bar: DailyBar, config: StopConfig) -> list[Action]:
    """The actions after ``bar``'s close, in the order they should be applied.

    Precedence: a hard stop hit inside the bar ends the position (the GTT fired, or should
    have); a failed EP on its gap day is sold at the next open; a close below the trail MA sells
    the remainder at the next open; otherwise the partial and the breakeven rules may fire,
    and a position with nothing to do says so explicitly.
    """
    if bar.low <= position.stop:
        return [Action(ActionKind.STOPPED_OUT, ActionReason.HARD_STOP_HIT, position.quantity)]
    if position.is_ep_gap_day and bar.bars_since_entry == 0 and bar.close < bar.open:
        return [Action(ActionKind.SELL_ALL, ActionReason.EP_FAILED_RED_ON_DAY, position.quantity)]

    trail = _trail_level(position, bar)
    if trail is not None and bar.bars_since_entry > 0 and bar.close < trail:
        return [Action(ActionKind.SELL_ALL, ActionReason.CLOSE_BELOW_TRAIL_MA, position.quantity)]

    actions: list[Action] = []
    green = bar.close > position.entry
    in_partial_window = (
        config.partial_earliest_bar <= bar.bars_since_entry <= config.partial_latest_bar
    )
    if not position.partial_done and in_partial_window and green:
        part = partial_quantity(position.quantity, config)
        if part > 0:
            actions.append(
                Action(ActionKind.SELL_PARTIAL, ActionReason.PARTIAL_INTO_STRENGTH, part)
            )
            if position.stop < position.entry:
                actions.append(
                    Action(
                        ActionKind.RAISE_STOP,
                        ActionReason.BREAKEVEN_AFTER_PARTIAL,
                        new_stop=position.entry,
                    )
                )
            return actions

    one_r = position.entry - position.initial_stop
    showing_r = (bar.close - position.entry) / one_r if one_r > _ZERO else _ZERO
    if position.stop < position.entry and showing_r >= Decimal(str(config.breakeven_after_r)):
        actions.append(
            Action(ActionKind.RAISE_STOP, ActionReason.BREAKEVEN_AT_R, new_stop=position.entry)
        )
    if not actions:
        actions.append(Action(ActionKind.HOLD, ActionReason.NOTHING_TO_DO))
    return actions


def apply(position: OpenPosition, actions: list[Action]) -> OpenPosition:
    """The position after ``actions``; a fully sold position has ``quantity == 0``."""
    quantity = position.quantity
    stop = position.stop
    partial_done = position.partial_done
    for action in actions:
        if action.kind in (ActionKind.SELL_ALL, ActionKind.STOPPED_OUT):
            quantity = 0
        elif action.kind is ActionKind.SELL_PARTIAL:
            quantity -= action.quantity
            partial_done = True
        elif action.kind is ActionKind.RAISE_STOP and action.new_stop is not None:
            stop = max(stop, action.new_stop)
    return OpenPosition(
        symbol=position.symbol,
        entry_date=position.entry_date,
        entry=position.entry,
        initial_stop=position.initial_stop,
        stop=stop,
        quantity=quantity,
        partial_done=partial_done,
        trail=position.trail,
        is_ep_gap_day=position.is_ep_gap_day,
    )
