"""The working limit order — the one mechanism the desk does not have today.

``docs/vbt/04`` §7. STRATEGY §3: *"Do not chase the open."* A VBT-1 entry is a limit at the
**signal bar's close**, working for three sessions, and then cancelled. That single rule is worth
about nine CAGR points against buying the next open (18.2% vs 9.6%), because the open gap — +1%
on the median signal — is exactly the part of the move that reverts.

**Three is the parameter with a cliff**: two sessions returns 11.4%, five returns 17.1%. Anyone
shortening it is changing the strategy.

Sessions, not days. A limit that rests over a holiday has not worked a session, so everything
here counts positions in a :class:`~baskfy_core.vbt.calendar.SessionCalendar` and never subtracts
two dates. Pure: no clock is consulted — the caller says which session it is.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from baskfy_core.vbt.calendar import SessionCalendar
from baskfy_core.vbt.config import EntryConfig


class OrderState(StrEnum):
    """Where a working order is. ``PROPOSED`` is a plan line nobody has confirmed yet."""

    PROPOSED = "PROPOSED"
    CONFIRMED = "CONFIRMED"
    SENT = "SENT"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


#: States in which the order is still capable of filling, and still holds a slot (``04`` §9.1).
LIVE_STATES: frozenset[OrderState] = frozenset(
    {OrderState.PROPOSED, OrderState.CONFIRMED, OrderState.SENT, OrderState.PARTIAL}
)

#: States nothing further happens to.
TERMINAL_STATES: frozenset[OrderState] = frozenset(
    {OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED, OrderState.REJECTED}
)


class CancelReason(StrEnum):
    EXPIRY_SWEEP = "EXPIRY_SWEEP"
    MANUAL = "MANUAL"
    GATE_SHUT = "GATE_SHUT"
    SLOT_TAKEN = "SLOT_TAKEN"


@dataclass(frozen=True, slots=True)
class WorkingOrder:
    """A limit bid resting for a bounded number of sessions."""

    instrument_id: int
    signal_date: dt.date
    limit_price: Decimal
    stop_price: Decimal
    quantity: int
    state: OrderState = OrderState.PROPOSED
    #: How many sessions it has actually been live for. Advanced once per **published** session,
    #: which is why a re-run of the evening, a holiday and a restart all change nothing.
    sessions_worked: int = 0
    filled_quantity: int = 0
    cancel_reason: CancelReason | None = None

    @property
    def is_live(self) -> bool:
        return self.state in LIVE_STATES

    @property
    def remaining(self) -> int:
        return max(self.quantity - self.filled_quantity, 0)


@dataclass(frozen=True, slots=True)
class Fill:
    """What one session did to one working order."""

    order: WorkingOrder
    quantity: int
    price: Decimal
    session: dt.date


def expires_after(
    signal_date: dt.date, calendar: SessionCalendar, config: EntryConfig
) -> dt.date | None:
    """The **last** session the order may fill on: ``valid_sessions`` after the signal.

    ``None`` when the calendar does not reach that far — an order placed on the last session the
    plant knows about has no expiry yet, and the sweep that runs on a later session gives it one.
    """
    return calendar.advance(signal_date, config.valid_sessions)


def sessions_since(signal_date: dt.date, session: dt.date, calendar: SessionCalendar) -> int:
    """Sessions of the calendar between the signal and ``session``. A holiday counts for none."""
    return calendar.sessions_between(signal_date, session)


def is_expired(
    order: WorkingOrder, session: dt.date, calendar: SessionCalendar, config: EntryConfig
) -> bool:
    """Has this order worked out its window by the **close** of ``session``?

    ``sessions_since > valid_sessions`` would be one session too late: the order may fill on
    t+1, t+2 and t+3, and it is cancelled at the close of t+3. So the test is ``>=``, evaluated
    after the session's fills have been considered.
    """
    return sessions_since(order.signal_date, session, calendar) >= config.valid_sessions


def advance_session(order: WorkingOrder) -> WorkingOrder:
    """One published session has passed with this order live."""
    return replace(order, sessions_worked=order.sessions_worked + 1)


def expire_orders(
    orders: list[WorkingOrder],
    session: dt.date,
    calendar: SessionCalendar,
    config: EntryConfig,
    reason: CancelReason = CancelReason.EXPIRY_SWEEP,
) -> tuple[list[WorkingOrder], list[WorkingOrder]]:
    """``(still working, expired this session)``.

    An order that never reached the broker becomes ``EXPIRED``; one the broker holds becomes
    ``CANCELLED`` and the desk sends the cancel. A **partially** filled order is cancelled for
    its remainder and the position it opened stands (``04`` §7.5).
    """
    live, done = [], []
    for order in orders:
        if not order.is_live:
            continue
        if is_expired(order, session, calendar, config):
            state = (
                OrderState.EXPIRED
                if order.state in (OrderState.PROPOSED, OrderState.CONFIRMED)
                else OrderState.CANCELLED
            )
            done.append(replace(order, state=state, cancel_reason=reason))
        else:
            live.append(order)
    return live, done


def fill_if_touched(  # noqa: PLR0913 - the bar arrives as its four prices, not as an object
    order: WorkingOrder,
    session: dt.date,
    *,
    open_price: Decimal | None,
    high: Decimal | None,
    low: Decimal | None,
    config: EntryConfig,
) -> Fill | None:
    """``04`` §7.3's fill model, for the backtest and for the drill.

    The order fills when the session's low trades at or through the limit, at **the better of the
    open and the limit** — a gap-down open fills below the bid, which is what actually happens.
    ``fill_through_pct`` demands the low trade that far *through* the limit first; it is 0 by
    default because 0.25 changes CAGR by 0.02 pt, and it exists for anyone who thinks the
    backtest's fills are optimistic.

    A session whose open, high and low are the same price is a locked circuit: no fill is
    possible, the order keeps working, and ``04`` §7.4 does not count the session against its
    window — which is the caller's business, not this function's.
    """
    if open_price is None or low is None or high is None or open_price <= Decimal(0):
        return None
    if open_price == high == low:
        return None
    threshold = order.limit_price * (
        Decimal(1) - Decimal(str(config.fill_through_pct)) / Decimal(100)
    )
    if low > threshold:
        return None
    price = min(open_price, order.limit_price)
    quantity = order.remaining
    if quantity <= 0:
        return None
    return Fill(order=order, quantity=quantity, price=price, session=session)


def apply_fill(order: WorkingOrder, fill: Fill) -> WorkingOrder:
    """The order after a fill: ``FILLED`` when nothing is left, ``PARTIAL`` while some is."""
    filled = order.filled_quantity + fill.quantity
    state = OrderState.FILLED if filled >= order.quantity else OrderState.PARTIAL
    return replace(order, filled_quantity=filled, state=state)
