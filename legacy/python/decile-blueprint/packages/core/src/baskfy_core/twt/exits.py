"""The two stops, and there are exactly two (``docs/twt/04`` §7).

137 of the research's 164 exits are the trailing stop. There is no target, no partial, no time stop
and no moving-average exit: ``01`` §5 measured all four and each lowered the result, and the 50-SMA
exit is a *different strategy* with the same signal (543 trades, 15.6 % CAGR, -38.3 % drawdown).

Pure: a position and a bar in, an action out. Nothing here places, cancels or arms anything; the
desk does that from a plan a person confirmed (``02`` Track C §3 — **the ratchet is a plan line,
never a job**, and there is no auto-execute flag for this sleeve).

Money and every level are :class:`~decimal.Decimal`. A stop is a number the exchange will hold for
months, and a float that is a tick out is a position protected at a price nobody chose.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from baskfy_core.twt.config import TICK_INR, ExitConfig

_ZERO = Decimal(0)
_ONE = Decimal(1)
_PCT = Decimal(100)


class ExitReason(StrEnum):
    """Why a position left, or is about to (``03`` §5's ``close_reason``).

    **There is no ``EMA_EXIT`` and no ``TIME_EXIT``**: TWT-1 has no exit that is not a stop, and a
    reason that exists is a reason somebody writes code for.
    """

    #: The session's low traded through the stop; the fill is the stop.
    STOP_HIT = "STOP_HIT"
    #: The session opened at or below the stop; the fill is the open.
    STOP_GAP = "STOP_GAP"
    #: ``04`` §7.4 — the **entry session's own** low reached the initial stop. Rare, and real.
    STOP_DAY0 = "STOP_DAY0"
    #: The name stopped printing. Written off at its last close; on the live book, an alert too.
    NO_BAR = "NO_BAR"
    #: The backtest liquidating what is still open on its last session, so the curve reads clean.
    END_OF_RUN = "END_OF_RUN"
    #: A person sold it.
    MANUAL = "MANUAL"


class Action(StrEnum):
    """What one close says about one position. **Three, and no fourth.**

    There is deliberately no ``QUEUE_SELL_AT_OPEN``: the shape this package copied from VBT-1 has
    such a rule and TWT-1 does not, and ``03`` §7 asks for the absence to be asserted rather than
    assumed.
    """

    HOLD = "HOLD"
    #: The stop fired (or should have — the resting GTT is the exchange's copy of this rule).
    STOPPED_OUT = "STOPPED_OUT"
    WRITE_OFF = "WRITE_OFF"


@dataclass(frozen=True, slots=True)
class OpenPosition:
    """What :func:`manage` and :func:`ratchet` need to know. The database row carries more."""

    instrument_id: int
    entry_date: dt.date
    #: The **exchange's** fill price, not the cost-inclusive book entry (``04`` §5.3).
    fill_price: Decimal
    quantity: int
    #: The stop resting at the exchange right now.
    stop_price: Decimal
    #: What the stop was at entry, so ``r_multiple`` means something after a hundred ratchets.
    initial_stop: Decimal
    #: The highest high since entry, an exchange print. Initialised to the fill price (``04`` §7.2).
    high_since: Decimal


@dataclass(frozen=True, slots=True)
class Bar:
    """One session for one instrument. ``close is None`` means the name did not print."""

    session: dt.date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None


@dataclass(frozen=True, slots=True)
class ManageAction:
    """What to do with one position after one close."""

    action: Action
    reason: ExitReason | None
    #: The price the exit is expected at — the stop, the open, or the last close. ``None`` for a
    #: hold.
    price: Decimal | None = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class Ratchet:
    """One session's trailing-stop arithmetic (``04`` §7.2), and whether it is a line.

    ``next_trigger`` is ``04`` §7.2 evaluated exactly — the research's expression, tick for tick, so
    TW2's goldens can be a comparison rather than an approximation. ``stop_in_force`` is what the
    book carries afterwards, and it is ``max(the stop that was resting, next_trigger)`` because **a
    stop never falls**.
    """

    high_since: Decimal
    raw_trigger: Decimal
    next_trigger: Decimal
    stop_in_force: Decimal
    #: True when ``next_trigger`` is strictly above the resting stop — the one case that becomes a
    #: ``RAISE_GTT_STOP`` line (``04`` §10.2).
    raises: bool
    #: True when the clamp produced a trigger **below** the resting stop. It cannot happen while
    #: the close is above the stop, which is the only state an open position is supposed to be in;
    #: it is surfaced rather than swallowed because a stop that quietly fell is the failure nobody
    #: notices until it does not fire.
    clamped_below_stop: bool


@dataclass(frozen=True, slots=True)
class AdjustmentOutcome:
    """What a corporate action does to a stop that has rested for months (``04`` §7.3, TW0.7).

    ``high_since`` and ``gtt_trigger`` are **exchange prices** while the bar series underneath them
    is adjusted, so a split during a 600-session hold leaves the resting GTT quoting a pre-split
    price on a post-split instrument. Arithmetically, re-deriving the trigger from the newly
    adjusted highs produces a **lower** number.

    Cancelling a resting stop and arming a lower one is the one thing this sleeve must never do on
    its own, and a split is the one event that would make doing it look correct. So: a re-derived
    trigger above the resting one is a line; one below it is an **alert and nothing else**.
    """

    high_since: Decimal
    next_trigger: Decimal
    #: Emit a ``RAISE_GTT_STOP`` carrying the adjustment as its reason.
    emit_raise: bool
    #: Raise ``TWT_ADJUSTMENT_RESET``: a person has to look at the resting order.
    alert: bool


def tick(tick_inr: str | None = None) -> Decimal:
    """The exchange's tick, as a ``Decimal``. ``None`` means NSE cash equities' own."""
    return Decimal(tick_inr if tick_inr is not None else TICK_INR)


def tick_floor(price: Decimal, tick_size: Decimal) -> Decimal:
    """Round **down** to the tick. A stop rounded up is a stop nobody asked for."""
    return (price / tick_size).to_integral_value(rounding=ROUND_DOWN) * tick_size


def initial_stop(fill_price: Decimal, config: ExitConfig, tick_size: Decimal) -> Decimal:
    """``04`` §7.1: ``stop_pct`` below **the fill**, floored to the tick.

    From the fill — the exchange's price — and not from the cost-inclusive book entry of ``04``
    §5.3, which is what the research does and what a person reading a chart would do.

    Measured: 15 % -> 21.6 % CAGR at -23 %, 30 % -> 19.4 % at -25 %, **10 % breaks the strategy**.
    """
    return tick_floor(fill_price * (_ONE - config.stop_pct / _PCT), tick_size)


def stop_is_below_entry(fill_price: Decimal, stop: Decimal) -> bool:
    """``04`` §7.1's refusal. It cannot arise at 20 % and the check is there because a ceiling edit
    could make it arise."""
    return _ZERO < stop < fill_price


def trail_from(high_since: Decimal, config: ExitConfig) -> Decimal:
    """``high_since x (1 - trail_pct / 100)`` — the trail, before anything is done to it."""
    return high_since * (_ONE - config.trail_pct / _PCT)


def clamp_under_close(
    raw_trigger: Decimal, close: Decimal, config: ExitConfig, tick_size: Decimal
) -> Decimal:
    """``04`` §7.2's clamp, floored to the tick::

        tick_floor(min(raw_trigger, close x close_clamp_fraction))  if raw_trigger < close
        tick_floor(close x close_clamp_fallback)                    otherwise

    **The clamp is not decoration.** A trigger at or above the last traded price fires the moment it
    is armed, which on a GTT means selling the position at the next tick for no reason. The research
    code clamps for the same purpose and this sleeve reproduces it exactly so TW2's goldens can be a
    tick comparison. The desk's own ``_raise_stop`` refuses a trigger at or above the last price
    independently, so the clamp is belt and braces — but the belt is what makes the plan line
    *correct*, and the braces only make it *safe*.
    """
    if raw_trigger < close:
        return tick_floor(min(raw_trigger, close * config.close_clamp_fraction), tick_size)
    return tick_floor(close * config.close_clamp_fallback, tick_size)


def ratchet(
    position: OpenPosition,
    bar: Bar,
    config: ExitConfig,
    tick_size: Decimal,
) -> Ratchet | None:
    """``04`` §7.2, after one close. ``None`` when the name did not print.

    ::

        high_since  = max(high_since, session_high)                  # exchange prints
        raw_trigger = max(stop_in_force, high_since x (1 - trail_pct / 100))
        trigger     = tick_floor(min(raw_trigger, close x close_clamp_fraction))
                      if raw_trigger < close
                      else tick_floor(close x close_clamp_fallback)

    **The clamp is not decoration.** A trigger at or above the last traded price fires the moment it
    is armed, which on a GTT means selling the position at the next tick for no reason. The research
    code clamps for the same purpose and this sleeve reproduces it exactly so TW2's goldens can be a
    tick comparison. The desk's own ``_raise_stop`` refuses a trigger at or above the last price
    independently, so the clamp is belt and braces — but the belt is what makes the plan line
    *correct*, and the braces only make it *safe*.

    **A stop never falls.** ``raw_trigger`` takes the maximum with the stop in force;
    ``tw_position`` has a check constraint; and the desk refuses a ``RAISE_GTT_STOP`` at or below
    the resting trigger. Three places, because this is the rule whose violation is silent. Here it
    is :attr:`Ratchet.stop_in_force`, which is never below the stop that was resting, and
    :attr:`Ratchet.raises`, which is the only thing that becomes a line.
    """
    if bar.close is None:
        return None
    high_since = position.high_since if bar.high is None else max(position.high_since, bar.high)
    trailed = trail_from(high_since, config)
    raw_trigger = max(position.stop_price, trailed)
    next_trigger = clamp_under_close(raw_trigger, bar.close, config, tick_size)
    return Ratchet(
        high_since=high_since,
        raw_trigger=raw_trigger,
        next_trigger=next_trigger,
        stop_in_force=max(position.stop_price, next_trigger),
        raises=next_trigger > position.stop_price,
        clamped_below_stop=next_trigger < position.stop_price,
    )


def on_adjustment(
    position: OpenPosition,
    rederived_high_since: Decimal,
    bar: Bar,
    config: ExitConfig,
    tick_size: Decimal,
) -> AdjustmentOutcome:
    """``04`` §7.3 and DECISIONS-TW **TW0.7**, when ``adj_factor`` moves under an open position.

    ``rederived_high_since`` is the highest high of the hold read off the **newly adjusted** series
    and scaled by the new factor, so the level means the same fraction of the price it meant
    yesterday. The caller derives it; this function decides what may be done with it.

    The trigger is derived from the re-derived high **alone** — :func:`ratchet`'s ``max`` with the
    stop in force is deliberately not applied here, because that floor would make the re-derived
    trigger unable to come out below the resting one and §7.3's whole second branch unreachable.
    The protection against a falling stop is the refusal below, not an arithmetic that cannot
    produce the number.

    * Above the resting trigger -> ``next_trigger`` is written and the morning carries a
      ``RAISE_GTT_STOP`` whose reason names the adjustment.
    * Below it — which is what a split does arithmetically — **no line is emitted** and
      ``TWT_ADJUSTMENT_RESET`` is raised instead, because the resting GTT at the exchange is now
      quoting a pre-split price on a post-split instrument and a person has to look at it.

    It cannot silently lower a stop. That is the whole of the rule.
    """
    if bar.close is None:
        return AdjustmentOutcome(
            high_since=rederived_high_since,
            next_trigger=position.stop_price,
            emit_raise=False,
            alert=True,
        )
    trigger = clamp_under_close(
        trail_from(rederived_high_since, config), bar.close, config, tick_size
    )
    raises = trigger > position.stop_price
    return AdjustmentOutcome(
        high_since=rederived_high_since,
        next_trigger=trigger,
        emit_raise=raises,
        alert=not raises,
    )


def stop_fill(bar: Bar, stop: Decimal) -> tuple[ExitReason, Decimal] | None:
    """The open first, then the low. ``None`` when the stop was not reached.

    A session that gapped below the stop fills at the **open**, not at the stop: the stop was never
    a price the market offered that morning. A session that traded down through it fills at the
    stop, which is what a resting GTT would have done.
    """
    if bar.open is not None and bar.open <= stop:
        return ExitReason.STOP_GAP, bar.open
    if bar.low is not None and bar.low <= stop:
        return ExitReason.STOP_HIT, stop
    return None


def fill_day_stop(position: OpenPosition, bar: Bar) -> ManageAction | None:
    """``04`` §7.4's fill-day rule. ``None`` when the entry session did not reach the stop.

    A position whose **entry session's own low** is at or below its initial stop is out that
    session, filled at the stop, or at the open when the open was already below it. The research
    calls it ``stop_day0`` and it is 20 %-down-from-the-open on the day of purchase: rare, and real.

    In the live book the GTT armed in the **same session as the fill** *is* this rule, which is why
    non-negotiable 4 is not negotiable here: a fill without a same-session GTT is a position with
    no fill-day protection at all.
    """
    if bar.session != position.entry_date or bar.low is None:
        return None
    if bar.low > position.stop_price:
        return None
    gapped = bar.open is not None and bar.open <= position.stop_price
    price = bar.open if gapped and bar.open is not None else position.stop_price
    return ManageAction(
        Action.STOPPED_OUT,
        ExitReason.STOP_DAY0,
        price,
        note=f"the entry session traded to {bar.low} against a stop of {position.stop_price}",
    )


def manage(
    position: OpenPosition,
    bar: Bar,
    *,
    blank_sessions: int = 0,
    config: ExitConfig,
) -> ManageAction:
    """What one close says about one position (``04`` §7's precedence, top-down).

    1. No bar for ``no_bar_sessions`` [5] sessions -> write it off at its last close (§7.5).
    2. The **fill-day** rule, when this is the entry session (§7.4).
    3. The stop, gapped through at the open or touched intraday (§7.1, §7.2).
    4. Otherwise hold, and say so explicitly.

    **There is no fifth branch**, and that is the strategy rather than an omission: no target, no
    partial, no time stop, no moving-average exit. The ratchet is not here either — it is not an
    action on a position, it is a level for tomorrow, and :func:`ratchet` computes it.
    """
    if bar.close is None:
        if blank_sessions >= config.no_bar_sessions:
            return ManageAction(
                Action.WRITE_OFF,
                ExitReason.NO_BAR,
                None,
                note=f"no bar for {blank_sessions} sessions",
            )
        return ManageAction(Action.HOLD, None, None, note="no bar today")

    day_zero = fill_day_stop(position, bar)
    if day_zero is not None:
        return day_zero

    hit = stop_fill(bar, position.stop_price)
    if hit is not None:
        reason, price = hit
        return ManageAction(Action.STOPPED_OUT, reason, price, note=f"stop {position.stop_price}")
    return ManageAction(Action.HOLD, None, None, note="above the stop; the trail ratchets tonight")


def r_multiple(entry: Decimal, initial: Decimal, exit_price: Decimal) -> Decimal | None:
    """``(exit - entry) / (entry - initial stop)``, 2 dp.

    ``None`` when the risk was not positive.
    """
    risk = entry - initial
    if risk <= _ZERO:
        return None
    return ((exit_price - entry) / risk).quantize(Decimal("0.01"))
