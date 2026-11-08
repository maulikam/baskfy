"""The limit that works for three sessions (``docs/vbt/04`` §7).

The one mechanism the desk does not have today, and **the parameter with a cliff**: two sessions
returns 11.4% a year, three returns 18.2%, five returns 17.1%. Anyone shortening it is changing
the strategy, so the window is tested from both ends — nothing fills on the fourth session, and
nothing expires before the third has closed.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl
import pytest
from vbt_fixtures import sessions

from baskfy_core.vbt.calendar import SessionCalendar, build_calendar
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, EntryConfig
from baskfy_core.vbt.orders import (
    CancelReason,
    Fill,
    OrderState,
    WorkingOrder,
    advance_session,
    apply_fill,
    expire_orders,
    expires_after,
    fill_if_touched,
    is_expired,
    sessions_since,
)

ENTRY = DEFAULT_VBT_CONFIG.entry
DAYS = sessions(12)


def calendar(days: list[dt.date] | None = None) -> SessionCalendar:
    chosen = days or DAYS
    return build_calendar(pl.DataFrame({"instrument_id": [1] * len(chosen), "date": chosen}))


def order(*, signal: dt.date | None = None, state: OrderState = OrderState.SENT) -> WorkingOrder:
    return WorkingOrder(
        instrument_id=1,
        signal_date=signal or DAYS[0],
        limit_price=Decimal("100.00"),
        stop_price=Decimal("88.00"),
        quantity=500,
        state=state,
    )


class TestTheWindow:
    def test_the_last_session_it_may_fill_on_is_three_after_the_signal(self) -> None:
        assert expires_after(DAYS[0], calendar(), ENTRY) == DAYS[3]

    def test_a_calendar_that_does_not_reach_that_far_answers_nothing(self) -> None:
        assert expires_after(DAYS[10], calendar(), ENTRY) is None

    @pytest.mark.parametrize(("index", "expired"), [(1, False), (2, False), (3, True), (4, True)])
    def test_it_is_alive_on_t1_t2_and_t3_and_dead_after(self, index: int, expired: bool) -> None:
        assert is_expired(order(), DAYS[index], calendar(), ENTRY) is expired

    def test_a_holiday_does_not_consume_a_session(self) -> None:
        """`04` §7.2: sessions are counted on the calendar, so "three sessions" means the same
        thing in the book as it does in the backtest."""
        trading = [DAYS[0], DAYS[1], DAYS[5], DAYS[6]]  # a long weekend in the middle
        assert sessions_since(DAYS[0], DAYS[6], calendar(trading)) == 3
        assert (DAYS[6] - DAYS[0]).days > 3

    def test_shortening_the_window_is_a_visible_change(self) -> None:
        two = EntryConfig(valid_sessions=2)
        assert is_expired(order(), DAYS[2], calendar(), two) is True
        assert is_expired(order(), DAYS[2], calendar(), ENTRY) is False


class TestExpiry:
    def test_an_order_the_broker_never_saw_simply_expires(self) -> None:
        live, done = expire_orders([order(state=OrderState.PROPOSED)], DAYS[3], calendar(), ENTRY)
        assert live == []
        assert done[0].state is OrderState.EXPIRED
        assert done[0].cancel_reason is CancelReason.EXPIRY_SWEEP

    def test_an_order_the_broker_holds_is_cancelled(self) -> None:
        _, done = expire_orders([order(state=OrderState.SENT)], DAYS[3], calendar(), ENTRY)
        assert done[0].state is OrderState.CANCELLED

    def test_an_order_inside_its_window_is_left_alone(self) -> None:
        live, done = expire_orders([order()], DAYS[2], calendar(), ENTRY)
        assert done == []
        assert live[0].state is OrderState.SENT

    def test_a_terminal_order_is_neither_kept_nor_re_expired(self) -> None:
        """Running the sweep twice for one session cancels once."""
        _, done = expire_orders([order(state=OrderState.CANCELLED)], DAYS[3], calendar(), ENTRY)
        assert done == []

    def test_a_partial_is_cancelled_for_its_remainder(self) -> None:
        """`04` §7.5 — the position it opened stands."""
        partial = WorkingOrder(
            instrument_id=1,
            signal_date=DAYS[0],
            limit_price=Decimal("100.00"),
            stop_price=Decimal("88.00"),
            quantity=500,
            state=OrderState.PARTIAL,
            filled_quantity=200,
        )
        _, done = expire_orders([partial], DAYS[3], calendar(), ENTRY)
        assert done[0].state is OrderState.CANCELLED
        assert done[0].remaining == 300
        assert done[0].filled_quantity == 200


class TestSessionsWorked:
    def test_advancing_counts_one_session(self) -> None:
        assert advance_session(order()).sessions_worked == 1
        assert advance_session(advance_session(order())).sessions_worked == 2


class TestTheFillModel:
    def test_a_touch_fills_at_the_limit(self) -> None:
        fill = fill_if_touched(
            order(),
            DAYS[1],
            open_price=Decimal("104"),
            high=Decimal("105"),
            low=Decimal("99"),
            config=ENTRY,
        )
        assert fill is not None
        assert fill.price == Decimal("100.00")
        assert fill.quantity == 500

    def test_a_gap_below_the_limit_fills_at_the_open(self) -> None:
        """The better of the open and the limit — which on a gap-down is the open, and is what
        actually happens to a resting bid."""
        fill = fill_if_touched(
            order(),
            DAYS[1],
            open_price=Decimal("96"),
            high=Decimal("99"),
            low=Decimal("95"),
            config=ENTRY,
        )
        assert fill is not None
        assert fill.price == Decimal("96")

    def test_a_session_that_never_traded_through_leaves_it_working(self) -> None:
        assert (
            fill_if_touched(
                order(),
                DAYS[1],
                open_price=Decimal("104"),
                high=Decimal("108"),
                low=Decimal("102"),
                config=ENTRY,
            )
            is None
        )

    def test_the_low_exactly_on_the_limit_fills(self) -> None:
        fill = fill_if_touched(
            order(),
            DAYS[1],
            open_price=Decimal("103"),
            high=Decimal("104"),
            low=Decimal("100.00"),
            config=ENTRY,
        )
        assert fill is not None

    def test_requiring_a_trade_through_refuses_a_bare_touch(self) -> None:
        """`04` §7.3: 0.25 changes CAGR by 0.02 pt, and the field exists for anyone who thinks
        the backtest's fills are optimistic."""
        strict = EntryConfig(fill_through_pct=0.25)
        assert (
            fill_if_touched(
                order(),
                DAYS[1],
                open_price=Decimal("103"),
                high=Decimal("104"),
                low=Decimal("100.00"),
                config=strict,
            )
            is None
        )

    def test_a_locked_session_cannot_fill(self) -> None:
        """`04` §7.4 — open == high == low is a circuit, and no bid gets served."""
        assert (
            fill_if_touched(
                order(),
                DAYS[1],
                open_price=Decimal("95"),
                high=Decimal("95"),
                low=Decimal("95"),
                config=ENTRY,
            )
            is None
        )

    def test_a_session_with_no_bar_cannot_fill(self) -> None:
        assert (
            fill_if_touched(order(), DAYS[1], open_price=None, high=None, low=None, config=ENTRY)
            is None
        )


class TestApplyingAFill:
    def test_a_full_fill_closes_the_order(self) -> None:
        fill = fill_if_touched(
            order(),
            DAYS[1],
            open_price=Decimal("104"),
            high=Decimal("105"),
            low=Decimal("99"),
            config=ENTRY,
        )
        assert fill is not None
        assert apply_fill(order(), fill).state is OrderState.FILLED

    def test_a_partial_keeps_working(self) -> None:
        base = order()
        after = apply_fill(
            base, Fill(order=base, quantity=200, price=Decimal("100"), session=DAYS[1])
        )
        assert after.state is OrderState.PARTIAL
        assert after.remaining == 300
        assert after.is_live


def test_three_sessions_is_the_documented_window() -> None:
    assert ENTRY.valid_sessions == 3
    assert ENTRY.limit_at == "SIGNAL_CLOSE"
