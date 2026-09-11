"""The two stops (``docs/twt/04`` §7), and there are exactly two.

``docs/twt/06`` TW1's acceptance criterion: §7.1's 20 % hard stop, §7.2's trailing stop off the
highest high clamped to the exchange tick, **the ratchet that never lowers a stop**, and §7.4's
fill-day rule.

The ratchet is the one place in this sleeve where being wrong is silent. A stop that quietly fell
is a stop nobody notices until it does not fire, so the "never falls" rule is asserted three ways
below: the ``max`` inside the arithmetic, the stop the book carries afterwards, and the flag the
one pathological branch raises rather than swallows.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, ExitConfig
from baskfy_core.twt.exits import (
    Action,
    Bar,
    ExitReason,
    OpenPosition,
    fill_day_stop,
    initial_stop,
    manage,
    on_adjustment,
    r_multiple,
    ratchet,
    stop_fill,
    stop_is_below_entry,
    tick,
    tick_floor,
)

EXITS = DEFAULT_TWT_CONFIG.exits
TICK = tick()
ENTRY_DAY = dt.date(2024, 6, 3)
LATER = dt.date(2024, 6, 4)
FILL = Decimal("100")
#: 20 % under a ₹100 fill.
STOP = Decimal("80")


def position(
    *, stop: Decimal = STOP, high_since: Decimal = FILL, entry: dt.date = ENTRY_DAY
) -> OpenPosition:
    return OpenPosition(
        instrument_id=1,
        entry_date=entry,
        fill_price=FILL,
        quantity=100,
        stop_price=stop,
        initial_stop=STOP,
        high_since=high_since,
    )


def bar(
    *,
    session: dt.date = LATER,
    open_: Decimal | None = None,
    high: Decimal | None = None,
    low: Decimal | None = None,
    close: Decimal | None = None,
) -> Bar:
    return Bar(session=session, open=open_, high=high, low=low, close=close)


class TestTheExchangeTick:
    def test_a_level_is_rounded_down_never_up(self) -> None:
        """A stop rounded up is a stop nobody asked for."""
        assert tick_floor(Decimal("80.0499"), TICK) == Decimal("80.00")
        assert tick_floor(Decimal("80.05"), TICK) == Decimal("80.05")
        assert tick_floor(Decimal("80.0999"), TICK) == Decimal("80.05")

    def test_the_tick_is_the_exchanges_and_not_a_setting(self) -> None:
        assert Decimal("0.05") == TICK


class TestTheDisasterStop:
    """``04`` §7.1: ``stop_pct`` below the fill, floored to the tick."""

    def test_the_stop_is_twenty_percent_under_the_fill(self) -> None:
        assert EXITS.stop_pct == Decimal("20.0")
        assert initial_stop(FILL, EXITS, TICK) == STOP

    def test_it_is_floored_to_the_tick(self) -> None:
        assert initial_stop(Decimal("101.23"), EXITS, TICK) == Decimal("80.95")

    def test_it_is_measured_from_the_exchanges_fill_and_not_the_cost_inclusive_entry(self) -> None:
        """``04`` §5.3: the backtest's book entry is ``open x 1.0025`` and the stop is computed from
        the **open**, which is what the research does and what a person reading a chart would do."""
        book_entry = FILL * (Decimal(1) + EXITS.stop_pct * Decimal(0))  # the open, unmodified
        assert initial_stop(book_entry, EXITS, TICK) == STOP
        assert initial_stop(FILL * Decimal("1.0025"), EXITS, TICK) == Decimal("80.20")

    def test_a_stop_that_is_not_below_the_entry_is_refused(self) -> None:
        assert stop_is_below_entry(FILL, STOP) is True
        assert stop_is_below_entry(FILL, FILL) is False
        assert stop_is_below_entry(FILL, Decimal("101")) is False
        assert stop_is_below_entry(FILL, Decimal(0)) is False

    def test_the_measured_band_is_flat_which_is_why_this_one_is_a_setting(self) -> None:
        """15 % -> 21.6 % CAGR at -23 %, 30 % -> 19.4 % at -25 %, and **10 % breaks the strategy**:
        these names swing more than 12 % inside their own bases."""
        tighter = ExitConfig(stop_pct=Decimal("15.0"))
        assert initial_stop(FILL, tighter, TICK) == Decimal("85")


class TestTheTrailingStopRatchet:
    """``04`` §7.2. 137 of the research's 164 exits are this stop."""

    def test_the_trail_is_twenty_percent_under_the_highest_high_since_entry(self) -> None:
        assert EXITS.trail_pct == Decimal("20.0")
        moved = ratchet(position(), bar(high=Decimal("150"), close=Decimal("148")), EXITS, TICK)
        assert moved is not None
        assert moved.high_since == Decimal("150")
        assert moved.next_trigger == Decimal("120")
        assert moved.raises is True
        assert moved.stop_in_force == Decimal("120")

    def test_high_since_includes_the_entry_sessions_own_high(self) -> None:
        """``04`` §7.2: initialised to the fill price and raised with each session's high — the
        entry session's included, which is why the research raises it after the day-0 check."""
        moved = ratchet(
            position(high_since=FILL),
            bar(session=ENTRY_DAY, high=Decimal("130"), low=Decimal("99"), close=Decimal("128")),
            EXITS,
            TICK,
        )
        assert moved is not None
        assert moved.high_since == Decimal("130")

    def test_a_lower_high_does_not_lower_high_since(self) -> None:
        moved = ratchet(
            position(high_since=Decimal("150")),
            bar(high=Decimal("140"), close=Decimal("139")),
            EXITS,
            TICK,
        )
        assert moved is not None
        assert moved.high_since == Decimal("150")

    def test_a_session_with_no_bar_ratchets_nothing(self) -> None:
        assert ratchet(position(), bar(close=None), EXITS, TICK) is None


class TestTheClamp:
    """``04`` §7.2's clamp is not decoration: a trigger at or above the last traded price fires the
    moment it is armed, which on a GTT means selling the position at the next tick for no reason."""

    def test_a_trigger_that_would_sit_above_the_close_is_pulled_under_it(self) -> None:
        moved = ratchet(position(), bar(high=Decimal("150"), close=Decimal("120.005")), EXITS, TICK)
        assert moved is not None
        assert moved.raw_trigger == Decimal("120")
        assert moved.next_trigger == Decimal("119.95")
        assert moved.next_trigger < Decimal("120.005")

    def test_a_trigger_at_or_above_the_close_falls_back_to_the_second_fraction(self) -> None:
        moved = ratchet(position(), bar(high=Decimal("150"), close=Decimal("118")), EXITS, TICK)
        assert moved is not None
        assert moved.raw_trigger == Decimal("120")
        assert moved.next_trigger == tick_floor(Decimal("118") * EXITS.close_clamp_fallback, TICK)
        assert moved.next_trigger == Decimal("117.85")

    def test_the_two_fractions_are_the_researchs_own(self) -> None:
        """Reproduced exactly so TW2's goldens can be a tick comparison rather than an
        approximation (``research/volume-breakout/vbt/sim.py``'s ``0.9999`` / ``0.999``)."""
        assert EXITS.close_clamp_fraction == Decimal("0.9999")
        assert EXITS.close_clamp_fallback == Decimal("0.999")


class TestAStopNeverFalls:
    """``04`` §7.2. Three places say so; two of them are here and the third is the desk's refusal
    of a ``RAISE_GTT_STOP`` at or below the resting trigger."""

    def test_the_raw_trigger_takes_the_maximum_with_the_stop_in_force(self) -> None:
        moved = ratchet(
            position(stop=Decimal("130")),
            bar(high=Decimal("140"), close=Decimal("139")),
            EXITS,
            TICK,
        )
        assert moved is not None
        assert moved.raw_trigger == Decimal("130")
        assert moved.next_trigger == Decimal("130")
        assert moved.raises is False

    def test_a_price_that_falls_back_towards_the_stop_does_not_move_it(self) -> None:
        for close in (Decimal("139"), Decimal("135"), Decimal("131")):
            moved = ratchet(
                position(stop=Decimal("130"), high_since=Decimal("150")),
                bar(high=Decimal("140"), close=close),
                EXITS,
                TICK,
            )
            assert moved is not None
            assert moved.stop_in_force == Decimal("130"), close
            assert moved.raises is False, close

    def test_the_one_branch_that_can_produce_a_lower_number_says_so_rather_than_using_it(
        self,
    ) -> None:
        """A close *below* the resting stop is a state an open position is not supposed to be in —
        the GTT should already have fired. The research's expression produces a lower trigger there
        and this sleeve reproduces the expression; what it does not do is carry the number.
        ``raises`` is False, ``stop_in_force`` is unchanged, and ``clamped_below_stop`` is the flag
        a caller can act on."""
        moved = ratchet(
            position(stop=Decimal("130"), high_since=Decimal("150")),
            bar(high=Decimal("131"), close=Decimal("125")),
            EXITS,
            TICK,
        )
        assert moved is not None
        assert moved.next_trigger == Decimal("124.85")
        assert moved.clamped_below_stop is True
        assert moved.raises is False
        assert moved.stop_in_force == Decimal("130")


class TestWhatACorporateActionDoesToAStopThatHasRestedForMonths:
    """``04`` §7.3 and DECISIONS-TW **TW0.7**. A split is the one event that would make lowering a
    stop look correct."""

    def test_a_re_derived_trigger_above_the_resting_one_is_a_line(self) -> None:
        outcome = on_adjustment(
            position(stop=Decimal("100")),
            Decimal("160"),
            bar(high=Decimal("150"), close=Decimal("155")),
            EXITS,
            TICK,
        )
        assert outcome.next_trigger == Decimal("128")
        assert outcome.emit_raise is True
        assert outcome.alert is False

    def test_a_re_derived_trigger_below_the_resting_one_emits_nothing_and_alerts(self) -> None:
        """Which is what a 1:2 split does arithmetically. Cancelling a resting stop and arming a
        lower one is the one thing this sleeve must never do on its own."""
        outcome = on_adjustment(
            position(stop=Decimal("130"), high_since=Decimal("300")),
            Decimal("150"),
            bar(high=Decimal("150"), close=Decimal("148")),
            EXITS,
            TICK,
        )
        assert outcome.next_trigger == Decimal("120")
        assert outcome.emit_raise is False
        assert outcome.alert is True

    def test_a_session_with_no_bar_alerts_rather_than_guessing(self) -> None:
        outcome = on_adjustment(position(), Decimal("150"), bar(close=None), EXITS, TICK)
        assert outcome.emit_raise is False
        assert outcome.alert is True
        assert outcome.next_trigger == STOP


class TestTheFillDayRule:
    """``04`` §7.4: a position whose **entry session's own low** is at or below its initial stop is
    out that session. Rare, and real — 20 % down from the open on the day of purchase."""

    def test_a_low_through_the_stop_on_the_entry_session_fills_at_the_stop(self) -> None:
        action = fill_day_stop(
            position(),
            bar(session=ENTRY_DAY, open_=Decimal("99"), low=Decimal("79"), close=Decimal("82")),
        )
        assert action is not None
        assert action.reason is ExitReason.STOP_DAY0
        assert action.price == STOP

    def test_an_open_already_below_the_stop_fills_at_the_open(self) -> None:
        action = fill_day_stop(
            position(),
            bar(session=ENTRY_DAY, open_=Decimal("78"), low=Decimal("75"), close=Decimal("77")),
        )
        assert action is not None
        assert action.price == Decimal("78")

    def test_a_low_exactly_at_the_stop_is_out(self) -> None:
        action = fill_day_stop(
            position(), bar(session=ENTRY_DAY, open_=Decimal("99"), low=STOP, close=STOP)
        )
        assert action is not None

    def test_a_low_above_the_stop_is_not(self) -> None:
        assert (
            fill_day_stop(
                position(),
                bar(session=ENTRY_DAY, open_=Decimal("99"), low=Decimal("81"), close=Decimal("90")),
            )
            is None
        )

    def test_it_is_the_entry_session_and_no_other(self) -> None:
        later = bar(session=LATER, open_=Decimal("99"), low=Decimal("79"), close=Decimal("82"))
        assert fill_day_stop(position(), later) is None
        assert manage(position(), later, config=EXITS).reason is ExitReason.STOP_HIT


class TestTheOrdinaryStopFill:
    def test_a_gap_through_the_stop_fills_at_the_open(self) -> None:
        assert stop_fill(bar(open_=Decimal("70"), low=Decimal("68")), STOP) == (
            ExitReason.STOP_GAP,
            Decimal("70"),
        )

    def test_a_low_through_the_stop_fills_at_the_stop(self) -> None:
        assert stop_fill(bar(open_=Decimal("95"), low=Decimal("79")), STOP) == (
            ExitReason.STOP_HIT,
            STOP,
        )

    def test_a_session_that_never_reached_it_is_not_a_fill(self) -> None:
        assert stop_fill(bar(open_=Decimal("95"), low=Decimal("81")), STOP) is None


class TestTheNoBarWriteOff:
    """``04`` §7.5: a book that carries a delisted line forever reports an equity it cannot
    realise."""

    def test_five_blank_sessions_write_the_position_off(self) -> None:
        assert EXITS.no_bar_sessions == 5
        action = manage(position(), bar(close=None), blank_sessions=5, config=EXITS)
        assert action.action is Action.WRITE_OFF
        assert action.reason is ExitReason.NO_BAR

    def test_four_blank_sessions_do_not(self) -> None:
        action = manage(position(), bar(close=None), blank_sessions=4, config=EXITS)
        assert action.action is Action.HOLD


class TestThereAreExactlyTwoExits:
    """``04`` §7.6. No target, no partial, no time stop, no moving-average exit — ``01`` §5
    measured all four, and the 50-SMA exit is a *different strategy* with the same signal."""

    def test_a_healthy_session_is_a_hold_and_says_so(self) -> None:
        action = manage(
            position(),
            bar(open_=Decimal("120"), low=Decimal("118"), close=Decimal("125")),
            config=EXITS,
        )
        assert action.action is Action.HOLD
        assert action.reason is None

    def test_there_is_no_queue_sell_at_open_action(self) -> None:
        """The shape this package copied from VBT-1 has such an action and TWT-1 does not."""
        assert {member.value for member in Action} == {"HOLD", "STOPPED_OUT", "WRITE_OFF"}

    def test_there_is_no_moving_average_or_time_exit_reason(self) -> None:
        reasons = {member.value for member in ExitReason}
        assert "EMA_EXIT" not in reasons
        assert "TIME_EXIT" not in reasons
        assert reasons == {
            "STOP_HIT",
            "STOP_GAP",
            "STOP_DAY0",
            "NO_BAR",
            "END_OF_RUN",
            "MANUAL",
        }

    @pytest.mark.parametrize("field", ["target_pct", "partial_r", "max_hold", "trail_ema_bars"])
    def test_no_field_exists_for_an_exit_the_method_does_not_have(self, field: str) -> None:
        """**A field that exists is a field somebody turns on.**"""
        assert not hasattr(EXITS, field)


class TestTheRMultiple:
    def test_it_is_measured_against_the_initial_stop(self) -> None:
        assert r_multiple(FILL, STOP, Decimal("140")) == Decimal("2.00")

    def test_a_non_positive_risk_has_no_r(self) -> None:
        assert r_multiple(FILL, FILL, Decimal("140")) is None
