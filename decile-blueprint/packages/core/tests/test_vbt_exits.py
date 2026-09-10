"""The stop and the exit (``docs/vbt/04`` §6).

688 of the study's 761 trades left on a close below the 21-day EMA and 62 hit the 12% stop. The
precedence is the rule: a stop that fired this morning is not a position the evening gets to
decide about, and a close below the EMA is tomorrow's sell, not tonight's.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, TICK_INR, ExitConfig
from baskfy_core.vbt.exits import (
    Action,
    Bar,
    ExitReason,
    OpenPosition,
    apply_stop,
    initial_stop,
    manage,
    r_multiple,
    stop_fill,
    tick_floor,
)

EXITS = DEFAULT_VBT_CONFIG.exits
TICK = Decimal(TICK_INR)
DAY = dt.date(2026, 3, 2)


def position(*, entry: str = "100.00", stop: str = "88.00", quantity: int = 100) -> OpenPosition:
    return OpenPosition(
        instrument_id=1,
        entry_date=dt.date(2026, 2, 2),
        entry_price=Decimal(entry),
        quantity=quantity,
        stop_price=Decimal(stop),
        initial_stop=Decimal(stop),
    )


def bar(
    *,
    open: str | None = "101",
    high: str | None = "103",
    low: str | None = "99",
    close: str | None = "102",
    ema: str | None = "95",
) -> Bar:
    def dec(value: str | None) -> Decimal | None:
        return None if value is None else Decimal(value)

    return Bar(
        session=DAY,
        open=dec(open),
        high=dec(high),
        low=dec(low),
        close=dec(close),
        ema_exit=dec(ema),
    )


class TestTheInitialStop:
    def test_twelve_percent_below_the_fill(self) -> None:
        assert initial_stop(Decimal("100.00"), EXITS, TICK) == Decimal("88.00")

    def test_it_is_floored_to_the_tick_never_raised_to_it(self) -> None:
        """A stop rounded up is a stop nobody asked for — it takes the position out earlier
        than the rule says."""
        stop = initial_stop(Decimal("137.35"), EXITS, TICK)
        assert stop == Decimal("120.85")  # 120.868 floored to the 5-paisa tick
        assert stop < Decimal("137.35") * Decimal("0.88")

    @pytest.mark.parametrize(("pct", "expected"), [(10.0, "90.00"), (15.0, "85.00")])
    def test_the_percentage_is_a_field(self, pct: float, expected: str) -> None:
        """STRATEGY §4 measured 10% → 16.4% CAGR and 15% → 16.6%: insurance either way, which
        is why this one number is a bounded ``vb_config`` setting."""
        config = ExitConfig(stop_pct=pct)
        assert initial_stop(Decimal("100.00"), config, TICK) == Decimal(expected)

    def test_tick_floor_leaves_an_exact_tick_alone(self) -> None:
        assert tick_floor(Decimal("88.00"), TICK) == Decimal("88.00")


class TestAStopNeverFalls:
    def test_a_lower_proposal_is_ignored(self) -> None:
        assert apply_stop(Decimal("90.00"), Decimal("85.00")) == Decimal("90.00")

    def test_a_higher_proposal_is_taken(self) -> None:
        assert apply_stop(Decimal("90.00"), Decimal("95.00")) == Decimal("95.00")


class TestTheStopFill:
    def test_a_gap_through_the_stop_fills_at_the_open(self) -> None:
        """`04` §6.4 — in the book this is what actually happens to a GTT."""
        assert stop_fill(bar(open="80", low="79"), Decimal("88.00")) == (
            ExitReason.STOP_GAP,
            Decimal("80"),
        )

    def test_a_stop_touched_intraday_fills_at_the_stop(self) -> None:
        assert stop_fill(bar(open="95", low="87"), Decimal("88.00")) == (
            ExitReason.STOP_HIT,
            Decimal("88.00"),
        )

    def test_a_session_that_never_reached_it_is_no_fill(self) -> None:
        assert stop_fill(bar(open="95", low="90"), Decimal("88.00")) is None

    def test_the_low_exactly_on_the_stop_fills(self) -> None:
        hit = stop_fill(bar(open="95", low="88"), Decimal("88.00"))
        assert hit is not None
        assert hit[0] is ExitReason.STOP_HIT


class TestManage:
    def test_an_ordinary_session_holds_and_says_so(self) -> None:
        """`04` §6.6 — said explicitly, so a page never has to infer "nothing happened"."""
        action = manage(position(), bar(), config=EXITS)
        assert action.action is Action.HOLD
        assert action.reason is None
        assert action.note

    def test_a_close_below_the_ema_queues_tomorrow_morning_s_sell(self) -> None:
        action = manage(position(), bar(close="94", ema="95"), config=EXITS)
        assert action.action is Action.QUEUE_SELL_AT_OPEN
        assert action.reason is ExitReason.EMA_EXIT
        assert action.price is None  # tomorrow's open is not knowable tonight
        assert "21-day EMA" in action.note

    def test_a_close_exactly_on_the_ema_is_a_hold(self) -> None:
        assert manage(position(), bar(close="95", ema="95"), config=EXITS).action is Action.HOLD

    def test_the_stop_outranks_the_ema(self) -> None:
        """A position the stop took out this morning is not one the evening decides about."""
        action = manage(position(), bar(open="95", low="80", close="82", ema="95"), config=EXITS)
        assert action.action is Action.STOPPED_OUT
        assert action.reason is ExitReason.STOP_HIT

    def test_a_null_ema_is_a_hold_not_a_sell(self) -> None:
        """A name whose average is still warming up is held, never sold on an absence."""
        assert manage(position(), bar(ema=None), config=EXITS).action is Action.HOLD

    def test_a_blank_session_inside_the_tolerance_holds(self) -> None:
        action = manage(
            position(),
            bar(open=None, high=None, low=None, close=None),
            blank_sessions=2,
            config=EXITS,
        )
        assert action.action is Action.HOLD

    def test_a_name_that_stops_printing_is_written_off(self) -> None:
        action = manage(
            position(),
            bar(open=None, high=None, low=None, close=None),
            blank_sessions=5,
            config=EXITS,
        )
        assert action.action is Action.WRITE_OFF
        assert action.reason is ExitReason.NO_BAR

    def test_a_position_entered_today_can_be_stopped_out_today(self) -> None:
        """`04` §6.6's last line — the same session's low is checked against the fresh stop."""
        fresh = OpenPosition(
            instrument_id=1,
            entry_date=DAY,
            entry_price=Decimal("100.00"),
            quantity=50,
            stop_price=Decimal("88.00"),
            initial_stop=Decimal("88.00"),
        )
        action = manage(fresh, bar(open="100", low="85", close="86"), config=EXITS)
        assert action.action is Action.STOPPED_OUT


class TestR:
    def test_a_winner(self) -> None:
        assert r_multiple(Decimal("100"), Decimal("88"), Decimal("124")) == Decimal("2.00")

    def test_a_full_stop_out_is_minus_one(self) -> None:
        assert r_multiple(Decimal("100"), Decimal("88"), Decimal("88")) == Decimal("-1.00")

    def test_no_risk_is_no_answer(self) -> None:
        assert r_multiple(Decimal("100"), Decimal("100"), Decimal("120")) is None


class TestWhatDoesNotExist:
    def test_there_is_no_target_or_partial_in_the_action_vocabulary(self) -> None:
        """`04` §6.3 — each was tested and each lowered the result, because the book's profit is
        a right tail. A vocabulary that cannot express a target cannot grow one by accident."""
        assert {member.value for member in Action} == {
            "HOLD",
            "QUEUE_SELL_AT_OPEN",
            "STOPPED_OUT",
            "WRITE_OFF",
        }
