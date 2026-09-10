"""VB7 as a theorem: a limit works three sessions, and exactly three.

`04` §7.2 is **the parameter with a cliff** — two sessions returns 11.4% a year where three
returns 18.2% — so the window is worth proving rather than sampling. Over random calendars and
random signal positions, two properties have to hold together:

* **Nothing fills on the fourth session.** An order still alive after its window is a limit that
  outstayed the setup it was placed for, and by then the pullback either arrived or the name has
  moved on.
* **Nothing expires before its third session has closed.** The cliff is on this side: an order
  cancelled a session early is the two-session book, which loses seven CAGR points.

The generated calendars deliberately include long gaps. A limit resting over Diwali has not
worked a session, and the arithmetic that gets this right counts positions in a calendar rather
than subtracting two dates.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from baskfy_core.vbt.calendar import SessionCalendar, build_calendar
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, EntryConfig
from baskfy_core.vbt.orders import (
    CancelReason,
    OrderState,
    WorkingOrder,
    expire_orders,
    expires_after,
    is_expired,
    sessions_since,
)

ENTRY = DEFAULT_VBT_CONFIG.entry


def calendar_of(gaps: list[int]) -> SessionCalendar:
    """A calendar whose sessions are separated by the given numbers of calendar days."""
    days: list[dt.date] = [dt.date(2026, 1, 5)]
    for gap in gaps:
        days.append(days[-1] + dt.timedelta(days=gap))
    frame = pl.DataFrame({"instrument_id": [1] * len(days), "date": days})
    return build_calendar(frame)


def an_order(signal: dt.date, state: OrderState = OrderState.SENT) -> WorkingOrder:
    return WorkingOrder(
        instrument_id=1,
        signal_date=signal,
        limit_price=Decimal("100.00"),
        stop_price=Decimal("88.00"),
        quantity=100,
        state=state,
    )


#: Gaps of one to twelve days: weekends, long weekends, Diwali, a two-week exchange holiday.
GAPS = st.lists(st.integers(min_value=1, max_value=12), min_size=8, max_size=40)


@given(gaps=GAPS, at=st.integers(min_value=0, max_value=30))
@settings(max_examples=200, deadline=None)
def test_nothing_fills_on_the_fourth_session(gaps: list[int], at: int) -> None:
    calendar = calendar_of(gaps)
    signal_index = at % max(len(calendar.sessions) - 4, 1)
    signal = calendar.sessions[signal_index]
    for offset, session in enumerate(calendar.sessions[signal_index:], start=0):
        expired = is_expired(an_order(signal), session, calendar, ENTRY)
        if offset >= ENTRY.valid_sessions:
            assert expired, f"session {offset} after the signal is still alive"


@given(gaps=GAPS, at=st.integers(min_value=0, max_value=30))
@settings(max_examples=200, deadline=None)
def test_nothing_expires_before_its_third_session_has_closed(gaps: list[int], at: int) -> None:
    calendar = calendar_of(gaps)
    signal_index = at % max(len(calendar.sessions) - 4, 1)
    signal = calendar.sessions[signal_index]
    for offset, session in enumerate(calendar.sessions[signal_index:], start=0):
        if offset < ENTRY.valid_sessions:
            assert not is_expired(an_order(signal), session, calendar, ENTRY), (
                f"session {offset} after the signal is already expired — the two-session book "
                f"loses seven CAGR points"
            )


@given(gaps=GAPS, at=st.integers(min_value=0, max_value=30))
@settings(max_examples=200, deadline=None)
def test_a_holiday_never_consumes_a_session(gaps: list[int], at: int) -> None:
    """The property the whole design turns on: the count is positions in a calendar, never days."""
    calendar = calendar_of(gaps)
    index = at % max(len(calendar.sessions) - 4, 1)
    signal = calendar.sessions[index]
    last = expires_after(signal, calendar, ENTRY)
    assert last is not None
    assert sessions_since(signal, last, calendar) == ENTRY.valid_sessions
    # …and it is later in the year than three days, on any calendar with a gap in it.
    assert (last - signal).days >= ENTRY.valid_sessions


@given(gaps=GAPS, at=st.integers(min_value=0, max_value=30))
@settings(max_examples=200, deadline=None)
def test_an_order_the_broker_holds_is_cancelled_and_one_it_never_saw_expires(
    gaps: list[int], at: int
) -> None:
    """`04` §7.2 and DECISIONS-VB VB6.4 — the two states have different endings, always."""
    calendar = calendar_of(gaps)
    index = at % max(len(calendar.sessions) - 4, 1)
    signal = calendar.sessions[index]
    session = calendar.sessions[index + ENTRY.valid_sessions]
    for state, expected in (
        (OrderState.SENT, OrderState.CANCELLED),
        (OrderState.PARTIAL, OrderState.CANCELLED),
        (OrderState.PROPOSED, OrderState.EXPIRED),
        (OrderState.CONFIRMED, OrderState.EXPIRED),
    ):
        live, done = expire_orders([an_order(signal, state)], session, calendar, ENTRY)
        assert live == []
        assert done[0].state is expected
        assert done[0].cancel_reason is CancelReason.EXPIRY_SWEEP


@pytest.mark.parametrize("window", [1, 2, 3, 5, 10])
def test_the_window_is_a_field_and_the_arithmetic_follows_it(window: int) -> None:
    """Nothing in the expiry compares against a literal three."""
    config = EntryConfig(valid_sessions=window)
    calendar = calendar_of([1] * 30)
    signal = calendar.sessions[0]
    alive = [
        offset
        for offset, session in enumerate(calendar.sessions)
        if not is_expired(an_order(signal), session, calendar, config)
    ]
    assert alive == list(range(window))
