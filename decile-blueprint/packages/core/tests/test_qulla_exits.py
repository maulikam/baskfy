"""LV9 — one exit rule for three sleeves: ``baskfy_core.exits.qulla`` is an adapter over
``baskfy_core.swing.stops.manage`` and re-implements nothing (``gates/live-9-qulla-exits.md`` Q1).

Maulik, 28 Sep 2026 (DECISIONS-LV LV9.0): *"based on Kristjan Kullamägi's style"*. The numbers are
the swing book's ``StopConfig``: a third on bars 3-5 if green, breakeven after the partial or at
+1R, the 10-day MA for names with ADR ≥ 6 % and the 20-day otherwise, sold at the next open on a
close below it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import inspect
from decimal import Decimal

from baskfy_core.exits import qulla
from baskfy_core.exits.qulla import (
    MA_TRAIL,
    PARTIAL,
    OhlcBar,
    Position,
    adr_pct,
    decide,
    mean,
    trail_for,
)
from baskfy_core.swing.config import StopConfig
from baskfy_core.swing.stops import TrailMa

D = Decimal
CONFIG = StopConfig()
ENTRY = dt.date(2026, 9, 1)


def _series(
    closes: list[str], *, spread: str = "0.02", start: dt.date = dt.date(2026, 8, 1)
) -> list[OhlcBar]:
    """A flat-range series: high/low a fixed fraction around each close (ADR about twice the
    spread)."""
    out = []
    day = start
    for close in closes:
        c = D(close)
        out.append(
            OhlcBar(
                day,
                c,
                (c * (1 + D(spread))).quantize(D("0.01")),
                (c * (1 - D(spread))).quantize(D("0.01")),
                c,
            )
        )
        day += dt.timedelta(days=1)
    return out


BASE = Position(
    position_id=7,
    symbol="ACME",
    entry_date=ENTRY,
    entry=D("100"),
    initial_stop=D("90"),
    stop=D("90"),
    quantity=300,
    partial_done=False,
    trail=TrailMa.MA20,
)


NINETY = D("90")


def _position(
    *,
    quantity: int = 300,
    stop: Decimal = NINETY,
    initial_stop: Decimal = NINETY,
    partial_done: bool = False,
    trail: TrailMa | None = TrailMa.MA20,
) -> Position:
    return dataclasses.replace(
        BASE,
        quantity=quantity,
        stop=stop,
        initial_stop=initial_stop,
        partial_done=partial_done,
        trail=trail,
    )


class TestTheAdapterIsAnAdapter:
    def test_it_calls_the_swing_rule_and_carries_none_of_its_own(self) -> None:
        source = inspect.getsource(qulla)
        assert "manage(" in source and "from baskfy_core.swing.stops import" in source
        for own_rule in ("partial_earliest_bar <=", "breakeven_after_r))", "bar.close < trail"):
            assert own_rule not in source, f"the adapter re-implements the rule: {own_rule}"

    def test_mean_is_none_before_the_window_is_full(self) -> None:
        assert mean([D(1)] * 19, 20) is None
        assert mean([D(2)] * 20, 20) == D(2)

    def test_adr_is_the_mean_high_low_range_in_percent(self) -> None:
        series = _series(["100"] * 20, spread="0.03")
        adr = adr_pct(series)
        assert adr is not None and D("6.1") <= adr <= D("6.2")  # 1.03/0.97 - 1 ≈ 6.19 %
        assert adr_pct(series[:19]) is None

    def test_the_trail_is_the_fast_ma_for_a_fast_name_and_the_slow_one_otherwise(self) -> None:
        assert trail_for(_series(["100"] * 20, spread="0.03"), CONFIG) is TrailMa.MA10
        assert trail_for(_series(["100"] * 20, spread="0.01"), CONFIG) is TrailMa.MA20
        assert trail_for(_series(["100"] * 5), CONFIG) is TrailMa.MA20, "too young: the slow one"


class TestWhatTheRuleSays:
    def test_no_bar_today_is_none(self) -> None:
        series = _series(["100"] * 25)
        assert (
            decide(_position(), series, on=dt.date(2030, 1, 1), bars_since_entry=3, config=CONFIG)
            is None
        )

    def test_a_green_bar_in_the_window_sells_a_third_and_moves_the_stop_to_breakeven(self) -> None:
        series = _series(["100"] * 24 + ["110"])
        decision = decide(
            _position(), series, on=series[-1].date, bars_since_entry=3, config=CONFIG
        )
        assert decision is not None
        assert (decision.sell_quantity, decision.sell_reason) == (100, PARTIAL)
        assert decision.raise_to == D("100")
        assert decision.trail is TrailMa.MA20 and decision.stop_hit is False

    def test_a_red_bar_in_the_window_sells_nothing(self) -> None:
        # Not green (close 98 < entry 100) and still above the 20-day average (about 95): hold.
        series = _series(["95"] * 24 + ["98"])
        decision = decide(
            _position(), series, on=series[-1].date, bars_since_entry=4, config=CONFIG
        )
        assert decision is not None and decision.holds

    def test_outside_the_window_the_partial_waits_but_one_r_moves_the_stop(self) -> None:
        series = _series(["100"] * 24 + ["111"])  # +1.1R on a 10-point risk
        decision = decide(
            _position(), series, on=series[-1].date, bars_since_entry=1, config=CONFIG
        )
        assert decision is not None
        assert decision.sell_quantity == 0 and decision.raise_to == D("100")

    def test_a_close_below_the_trail_ma_sells_the_remainder(self) -> None:
        series = _series(["120"] * 24 + ["100"])  # MA20 ≈ 119; close 100 below it
        decision = decide(
            _position(partial_done=True, quantity=200, stop=D("80"), initial_stop=D("80")),
            series,
            on=series[-1].date,
            bars_since_entry=9,
            config=CONFIG,
        )
        assert decision is not None
        assert (decision.sell_quantity, decision.sell_reason) == (200, MA_TRAIL)

    def test_the_hard_stop_traded_through_is_reported_not_acted_on(self) -> None:
        series = _series(["100"] * 24 + ["85"], spread="0.02")
        decision = decide(
            _position(), series, on=series[-1].date, bars_since_entry=2, config=CONFIG
        )
        assert decision is not None and decision.stop_hit is True and decision.sell_quantity == 0

    def test_a_young_history_holds_because_the_average_is_not_a_number_yet(self) -> None:
        series = _series(["100"] * 10 + ["80"])  # MA20 is None; the trail cannot fire
        decision = decide(
            _position(partial_done=True, stop=D("70"), initial_stop=D("70")),
            series,
            on=series[-1].date,
            bars_since_entry=8,
            config=CONFIG,
        )
        assert decision is not None and decision.holds

    def test_the_trail_is_chosen_once_when_the_position_has_none(self) -> None:
        series = _series(["100"] * 25, spread="0.04")
        decision = decide(
            _position(trail=None), series, on=series[-1].date, bars_since_entry=1, config=CONFIG
        )
        assert decision is not None and decision.trail is TrailMa.MA10
