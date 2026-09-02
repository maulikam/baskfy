"""docs/swing/04 §6 — the stop and exit rules, one bar at a time."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal

import pytest

from baskfy_core.swing.config import StopConfig
from baskfy_core.swing.stops import (
    Action,
    ActionKind,
    ActionReason,
    DailyBar,
    OpenPosition,
    StopMode,
    TrailMa,
    apply,
    choose_trail,
    initial_stop,
    manage,
    partial_quantity,
    widest_stop_pct,
)

CONFIG = StopConfig()
D = Decimal


BASE = OpenPosition(
    symbol="X",
    entry_date=dt.date(2026, 9, 1),
    entry=D("100"),
    initial_stop=D("95"),
    stop=D("95"),
    quantity=300,
    partial_done=False,
    trail=TrailMa.MA10,
    is_ep_gap_day=False,
)


def position(
    *,
    stop: Decimal = BASE.stop,
    partial_done: bool = False,
    trail: TrailMa = TrailMa.MA10,
    is_ep_gap_day: bool = False,
) -> OpenPosition:
    return replace(
        BASE, stop=stop, partial_done=partial_done, trail=trail, is_ep_gap_day=is_ep_gap_day
    )


def bar(  # noqa: PLR0913 - one keyword per bar field a rule reads
    close: str,
    *,
    low: str | None = None,
    ma10: str = "90",
    ma20: str = "88",
    day: int = 1,
    open_: str | None = None,
) -> DailyBar:
    c = D(close)
    return DailyBar(
        date=dt.date(2026, 9, 1) + dt.timedelta(days=day),
        open=D(open_) if open_ else c,
        high=c * D("1.01"),
        low=D(low) if low else c * D("0.99"),
        close=c,
        ma10=D(ma10),
        ma20=D(ma20),
        bars_since_entry=day,
    )


def test_initial_stop_is_the_low_of_day_by_default() -> None:
    assert initial_stop(
        entry=D("100"), low_of_day=D("96"), opening_range_low=D("97"), mode=StopMode.LOW_OF_DAY
    ) == D("96")


def test_opening_range_low_gives_the_tighter_stop_when_asked_for() -> None:
    assert initial_stop(
        entry=D("100"),
        low_of_day=D("96"),
        opening_range_low=D("97"),
        mode=StopMode.OPENING_RANGE_LOW,
    ) == D("97")
    assert initial_stop(
        entry=D("100"), low_of_day=D("96"), opening_range_low=None, mode=StopMode.OPENING_RANGE_LOW
    ) == D("96")


def test_a_stop_above_entry_is_an_error_not_a_position() -> None:
    with pytest.raises(ValueError, match="not below"):
        initial_stop(
            entry=D("100"), low_of_day=D("101"), opening_range_low=None, mode=StopMode.LOW_OF_DAY
        )


def test_widest_stop_is_one_adr_capped_at_the_absolute_limit() -> None:
    """§6: one ADR is the widest stop; a 14%-ADR name is still capped at 10%."""
    assert widest_stop_pct(D("4.5"), CONFIG) == D("4.50")
    assert widest_stop_pct(D("14"), CONFIG) == D("10.00")


def test_fast_movers_trail_the_ten_day_slower_names_the_twenty() -> None:
    assert choose_trail(D("7"), CONFIG) is TrailMa.MA10
    assert choose_trail(D("4"), CONFIG) is TrailMa.MA20


def test_partial_is_a_third_rounded_down_and_never_the_whole_position() -> None:
    assert partial_quantity(300, CONFIG) == 100
    assert partial_quantity(2, CONFIG) == 0
    assert partial_quantity(4, CONFIG) == 1


def test_hard_stop_hit_inside_the_bar_ends_the_position() -> None:
    actions = manage(position(), bar("97", low="94.5"), CONFIG)
    assert actions == [Action(ActionKind.STOPPED_OUT, ActionReason.HARD_STOP_HIT, 300)]
    assert apply(position(), actions).quantity == 0


def test_an_ep_that_closes_red_on_its_gap_day_is_sold() -> None:
    actions = manage(position(is_ep_gap_day=True), bar("98", open_="101", day=0), CONFIG)
    assert actions[0].kind is ActionKind.SELL_ALL
    assert actions[0].reason is ActionReason.EP_FAILED_RED_ON_DAY


def test_close_below_the_trail_ma_sells_the_remainder() -> None:
    actions = manage(position(trail=TrailMa.MA20), bar("102", ma20="103", day=8), CONFIG)
    assert [a.kind for a in actions] == [ActionKind.SELL_ALL]
    assert actions[0].reason is ActionReason.CLOSE_BELOW_TRAIL_MA


def test_trail_is_not_consulted_on_the_entry_day() -> None:
    actions = manage(position(), bar("102", ma10="103", day=0), CONFIG)
    assert actions[0].kind is not ActionKind.SELL_ALL


def test_partial_into_strength_on_day_three_moves_the_stop_to_breakeven() -> None:
    actions = manage(position(), bar("108", day=3), CONFIG)
    assert [(a.kind, a.reason) for a in actions] == [
        (ActionKind.SELL_PARTIAL, ActionReason.PARTIAL_INTO_STRENGTH),
        (ActionKind.RAISE_STOP, ActionReason.BREAKEVEN_AFTER_PARTIAL),
    ]
    after = apply(position(), actions)
    assert after.quantity == 200
    assert after.partial_done is True
    assert after.stop == D("100")


def test_no_partial_when_the_position_is_red_in_the_window() -> None:
    actions = manage(position(), bar("99", day=4), CONFIG)
    assert all(a.kind is not ActionKind.SELL_PARTIAL for a in actions)


def test_no_partial_outside_the_window_or_twice() -> None:
    assert all(
        a.kind is not ActionKind.SELL_PARTIAL for a in manage(position(), bar("108", day=2), CONFIG)
    )
    assert all(
        a.kind is not ActionKind.SELL_PARTIAL for a in manage(position(), bar("108", day=6), CONFIG)
    )
    assert all(
        a.kind is not ActionKind.SELL_PARTIAL
        for a in manage(position(partial_done=True), bar("108", day=4), CONFIG)
    )


def test_breakeven_at_one_r_without_a_partial() -> None:
    actions = manage(position(), bar("105", day=1), CONFIG)
    assert actions == [
        Action(ActionKind.RAISE_STOP, ActionReason.BREAKEVEN_AT_R, new_stop=D("100"))
    ]


def test_a_stop_is_never_lowered() -> None:
    raised = position(stop=D("104"))
    actions = manage(raised, bar("105", day=1), CONFIG)
    assert apply(raised, actions).stop == D("104")


def test_nothing_to_do_is_said_explicitly() -> None:
    actions = manage(position(), bar("101", day=1), CONFIG)
    assert [(a.kind, a.reason) for a in actions] == [(ActionKind.HOLD, ActionReason.NOTHING_TO_DO)]
