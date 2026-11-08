"""VB10: the two ways this sleeve could lose money quietly, over random books.

Both failures are silent — no exception, no alert, a book that looks fine and a loss bigger than
the rules allowed — which is why they get a property test rather than an example.

* **A stop never falls** (`04` §6.2). The 12% stop is set from the fill and does not move for the
  life of the position: there is no trail in this strategy and the EMA is the exit, not a stop. A
  stop that could fall would turn a bounded 12% loss into whatever the market felt like.
* **A sell is for exactly what is held**, and no line is produced for a name the sleeve does not
  hold (`02` Track C: it never sells a holding it did not buy). This sleeve has no partial exits,
  so the property is an **equality** — which is a stronger statement than "not more than".

The desk's half of the same claims — the refusals as the route answers them — is in
`kite-momentum-rebalancer/tests/test_vbt_safety.py`. This half is the arithmetic, so it can be
driven over thousands of random books instead of a handful of hand-written ones.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, TICK_INR
from baskfy_core.vbt.exits import (
    Action,
    Bar,
    ExitReason,
    ManageAction,
    OpenPosition,
    apply_stop,
    initial_stop,
    stop_fill,
    tick_floor,
)
from baskfy_core.vbt.orders import OrderState, WorkingOrder
from baskfy_core.vbt.plan import BookState, LineKind, exit_lines

DAY = dt.date(2026, 9, 9)
TICK = Decimal(TICK_INR)
EXITS = DEFAULT_VBT_CONFIG.exits

prices = st.decimals(
    min_value=Decimal("1.00"), max_value=Decimal("50000.00"), places=2, allow_nan=False
)
quantities = st.integers(min_value=1, max_value=100_000)


def _position(entry: Decimal, quantity: int) -> OpenPosition:
    stop = initial_stop(entry, EXITS, TICK)
    return OpenPosition(
        instrument_id=1,
        entry_date=DAY,
        entry_price=entry,
        quantity=quantity,
        stop_price=stop,
        initial_stop=stop,
    )


class TestAStopNeverFalls:
    @given(current=prices, proposed=prices)
    @settings(max_examples=400, deadline=None)
    def test_no_proposal_can_lower_a_stop(self, current: Decimal, proposed: Decimal) -> None:
        """`04` §6.2, over any pair at all — a proposal above, below, or equal."""
        assert apply_stop(current, proposed) >= current

    @given(start=prices, proposals=st.lists(prices, min_size=1, max_size=40))
    @settings(max_examples=300, deadline=None)
    def test_a_whole_session_history_never_lowers_it(
        self, start: Decimal, proposals: list[Decimal]
    ) -> None:
        """The property that matters is over a *sequence*: one step could be monotone while the
        series ratchets down through a rounding seam."""
        stop = start
        for proposed in proposals:
            after = apply_stop(stop, proposed)
            assert after >= stop
            stop = after
        assert stop >= start

    @given(fill=prices)
    @settings(max_examples=300, deadline=None)
    def test_the_stop_is_below_the_fill_and_on_the_tick(self, fill: Decimal) -> None:
        """A stop at or above its own fill is not a stop; it is an immediate exit. And a stop
        off the tick is a stop the exchange will refuse."""
        stop = initial_stop(fill, EXITS, TICK)
        assert stop < fill
        assert stop == tick_floor(stop, TICK)

    @given(fill=prices, low=prices, open_=prices)
    @settings(max_examples=400, deadline=None)
    def test_a_stop_never_fills_above_its_own_trigger(
        self, fill: Decimal, low: Decimal, open_: Decimal
    ) -> None:
        """A stop that filled above its trigger would be a loss smaller than reality — the
        flattering direction, and the one nobody checks."""
        stop = initial_stop(fill, EXITS, TICK)
        bar = Bar(
            session=DAY,
            open=open_,
            high=max(open_, low),
            low=min(open_, low),
            close=low,
            ema_exit=None,
        )
        filled = stop_fill(bar, stop)
        if filled is None:
            return
        reason, price = filled
        assert price <= stop
        assert reason in {ExitReason.STOP_GAP, ExitReason.STOP_HIT}


class TestASellIsForExactlyWhatIsHeld:
    @given(quantity=quantities, entry=prices)
    @settings(max_examples=300, deadline=None)
    def test_a_queued_sell_line_carries_the_whole_position_and_no_more(
        self, quantity: int, entry: Decimal
    ) -> None:
        managed = [
            (
                1,
                "VBTCO",
                quantity,
                ManageAction(Action.QUEUE_SELL_AT_OPEN, ExitReason.EMA_EXIT, None),
            )
        ]
        lines = exit_lines(managed, [], BookState())

        sells = [line for line in lines if line.kind is LineKind.SELL_AT_OPEN]
        assert len(sells) == 1
        assert sells[0].quantity == quantity

    @given(quantity=quantities)
    @settings(max_examples=200, deadline=None)
    def test_a_hold_produces_no_sell_at_all(self, quantity: int) -> None:
        """The quiet failure in the other direction: a line for a position nobody exited."""
        managed = [(1, "VBTCO", quantity, ManageAction(Action.HOLD, None, None))]

        lines = exit_lines(managed, [], BookState())

        assert [line for line in lines if line.kind is LineKind.SELL_AT_OPEN] == []

    def test_an_empty_book_produces_no_lines_of_any_kind(self) -> None:
        """`02` Track C: it never sells a holding it did not buy. With nothing held there is
        nothing to sell, and the plan is empty rather than defaulted."""
        assert exit_lines([], [], BookState()) == []

    @given(remaining=quantities, limit=prices)
    @settings(max_examples=200, deadline=None)
    def test_a_cancel_line_carries_the_remainder_and_not_the_original(
        self, remaining: int, limit: Decimal
    ) -> None:
        """`04` §7.5: a partly filled order is cancelled for its remainder. Cancelling the
        original quantity would ask the broker to cancel shares that already became a position."""
        order = WorkingOrder(
            instrument_id=1,
            signal_date=DAY,
            limit_price=limit,
            stop_price=limit,
            quantity=remaining + 10,
            filled_quantity=10,
            state=OrderState.PARTIAL,
        )
        lines = exit_lines([], [(order, "VBTCO")], BookState())

        cancels = [line for line in lines if line.kind is LineKind.CANCEL_LIMIT]
        assert len(cancels) == 1
        assert cancels[0].quantity == order.remaining == remaining
