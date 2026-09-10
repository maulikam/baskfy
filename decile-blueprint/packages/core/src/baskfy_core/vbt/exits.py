"""The stop and the exit (``docs/vbt/04`` §6). **The EMA is the exit; the stop is insurance.**

688 of the study's 761 trades left on a close below the 21-day EMA and 62 hit the 12% stop, which
is the whole shape of this strategy: a flat disaster stop that rarely fires, and a trend-following
exit that does the work. There is no target, no partial and no time stop — each was tested and
each lowered the result, because the book's profit is a right tail (average winner +16.5%,
average loser -6.1%, best trade +242%, the ten best trades 24% of gross profit).

Pure: a position and a bar in, an action out. Nothing here places, cancels or arms anything; the
desk does that from a plan a person confirmed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from baskfy_core.vbt.config import ExitConfig


class ExitReason(StrEnum):
    """Why a position left, or is about to."""

    #: The close fell below the 21-day EMA; the sell is at the **next** session's open.
    EMA_EXIT = "EMA_EXIT"
    #: The session's low traded through the stop; the fill is the stop.
    STOP_HIT = "STOP_HIT"
    #: The session opened at or below the stop; the fill is the open.
    STOP_GAP = "STOP_GAP"
    #: The name stopped printing. Written off at its last close; on the live book, an alert too.
    NO_BAR = "NO_BAR"
    #: The backtest liquidating what is still open on its last session, so the curve reads clean.
    END_OF_RUN = "END_OF_RUN"
    #: A person sold it.
    MANUAL = "MANUAL"


class Action(StrEnum):
    HOLD = "HOLD"
    #: Queue a sell for the next session's open.
    QUEUE_SELL_AT_OPEN = "QUEUE_SELL_AT_OPEN"
    #: The stop fired (or should have — the GTT is the exchange's copy of this rule).
    STOPPED_OUT = "STOPPED_OUT"
    WRITE_OFF = "WRITE_OFF"


@dataclass(frozen=True, slots=True)
class OpenPosition:
    """What ``manage`` needs to know about a position. The database row carries more."""

    instrument_id: int
    entry_date: dt.date
    entry_price: Decimal
    quantity: int
    stop_price: Decimal
    initial_stop: Decimal
    exit_queued_for: dt.date | None = None


@dataclass(frozen=True, slots=True)
class Bar:
    """One session for one instrument. ``close is None`` means the name did not print."""

    session: dt.date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    ema_exit: Decimal | None


@dataclass(frozen=True, slots=True)
class ManageAction:
    """What to do with one position after one close."""

    action: Action
    reason: ExitReason | None
    #: The price the exit is expected at — the stop, the open, or the last close. ``None`` for a
    #: hold and for a queued sell, whose price is tomorrow's open and is not knowable tonight.
    price: Decimal | None = None
    note: str = ""


def tick_floor(price: Decimal, tick: Decimal) -> Decimal:
    """Round **down** to the tick. A stop rounded up is a stop nobody asked for."""
    return (price / tick).to_integral_value(rounding=ROUND_DOWN) * tick


def initial_stop(fill_price: Decimal, config: ExitConfig, tick: Decimal) -> Decimal:
    """``04`` §6.1: ``stop_pct`` below **the fill**, floored to the tick.

    From the fill, not from the signal close and not from the cost-adjusted entry: the stop is a
    statement about the price this book actually paid.
    """
    raw = fill_price * (Decimal(1) - Decimal(str(config.stop_pct)) / Decimal(100))
    return tick_floor(raw, tick)


def apply_stop(current: Decimal, proposed: Decimal) -> Decimal:
    """A stop never falls. VBT-1 has no trail, so in practice it never moves either."""
    return max(current, proposed)


def stop_fill(bar: Bar, stop: Decimal) -> tuple[ExitReason, Decimal] | None:
    """``04`` §6.4: the open first, then the low. ``None`` when the stop was not reached."""
    if bar.open is not None and bar.open <= stop:
        return ExitReason.STOP_GAP, bar.open
    if bar.low is not None and bar.low <= stop:
        return ExitReason.STOP_HIT, stop
    return None


def manage(
    position: OpenPosition,
    bar: Bar,
    *,
    blank_sessions: int = 0,
    config: ExitConfig,
) -> ManageAction:
    """What one close says about one position (``04`` §6.6's precedence, top-down).

    1. No bar for ``no_bar_tolerance_sessions`` sessions → write it off at its last close.
    2. The stop, gapped through at the open or touched intraday.
    3. The close below the 21-day EMA → queue a sell for the **next** open.
    4. Otherwise hold, and say so explicitly.

    A position entered today can be stopped out today: the caller passes today's bar and the
    fresh stop, and 2 fires. The EMA is never evaluated on a null — a name whose average is still
    warming up is held, not sold on an absence.
    """
    if bar.close is None:
        if blank_sessions >= config.no_bar_tolerance_sessions:
            return ManageAction(
                Action.WRITE_OFF,
                ExitReason.NO_BAR,
                None,
                note=f"no bar for {blank_sessions} sessions",
            )
        return ManageAction(Action.HOLD, None, None, note="no bar today")

    hit = stop_fill(bar, position.stop_price)
    if hit is not None:
        reason, price = hit
        return ManageAction(Action.STOPPED_OUT, reason, price, note=f"stop {position.stop_price}")

    if bar.ema_exit is not None and bar.close < bar.ema_exit:
        return ManageAction(
            Action.QUEUE_SELL_AT_OPEN,
            ExitReason.EMA_EXIT,
            None,
            note=f"close {bar.close} below the {config.trail_ema_bars}-day EMA {bar.ema_exit}",
        )
    return ManageAction(Action.HOLD, None, None, note="above the EMA, stop intact")


def r_multiple(entry: Decimal, initial: Decimal, exit_price: Decimal) -> Decimal | None:
    """``(exit - entry) / (entry - initial stop)``, 2 dp.

    ``None`` when the risk was not positive.
    """
    risk = entry - initial
    if risk <= Decimal(0):
        return None
    return ((exit_price - entry) / risk).quantize(Decimal("0.01"))
