"""OP9 — ``exits.evaluate``: one open position's exit per tick (``04`` §3-§5 exits, §8.5, §9.2).

The per-sleeve rules are tested where they live (``test_options_condor``, ``_directional``,
``_expiry_setups``); this file asserts the composition — the mark each rule is handed (longs at the
bid, shorts at the ask), the marked loss, staleness, the missing-mark case and feed loss — so the
desk's monitor can call one function per tick and trust what it gets.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from baskfy_core.options.bars import IST, Bar
from baskfy_core.options.config import OptionsConfig, Sleeve
from baskfy_core.options.execution import LegRole
from baskfy_core.options.exits import (
    FEED_LOST,
    RULE,
    IndexState,
    LegMark,
    OpenLeg,
    OpenPosition,
    evaluate,
    latest_bar_after,
    latest_five_minute_close,
    mark,
    marked_loss_inr,
)
from baskfy_core.options.structures import Direction, Structure

CFG = OptionsConfig()
DAY = dt.date(2026, 10, 27)
QTY = 65


def at(hh: int, mm: int, ss: int = 0) -> dt.datetime:
    return dt.datetime(DAY.year, DAY.month, DAY.day, hh, mm, ss, tzinfo=IST)


def q(bid: str, ask: str, when: dt.datetime) -> LegMark:
    return LegMark(Decimal(bid), Decimal(ask), when)


def bar(hh: int, mm: int, close: str, *, high: str | None = None, low: str | None = None) -> Bar:
    c = Decimal(close)
    return Bar(
        ts=at(hh, mm),
        open=c,
        high=Decimal(high) if high else c + 2,
        low=Decimal(low) if low else c - 2,
        close=c,
    )


def flat_bars(start: tuple[int, int], end: tuple[int, int], close: str) -> tuple[Bar, ...]:
    out: list[Bar] = []
    t = at(*start)
    while t <= at(*end):
        out.append(bar(t.hour, t.minute, close))
        t += dt.timedelta(minutes=1)
    return tuple(out)


def condor(credit: str = "40.00", opened: dt.datetime | None = None) -> OpenPosition:
    return OpenPosition(
        sleeve=Sleeve.O1M,
        structure=Structure.IRON_CONDOR,
        legs=(
            OpenLeg(LegRole.LONG_PUT, Decimal("24700"), QTY, Decimal("5.00"), 1),
            OpenLeg(LegRole.LONG_CALL, Decimal("25300"), QTY, Decimal("5.00"), 2),
            OpenLeg(LegRole.SHORT_PUT, Decimal("24850"), QTY, Decimal("25.00"), 3),
            OpenLeg(LegRole.SHORT_CALL, Decimal("25150"), QTY, Decimal("25.00"), 4),
        ),
        entry_points=Decimal(credit),
        opened_at=opened or at(10, 5),
        risk_budget_inr=Decimal("50000"),
    )


def condor_marks(short: str, wing: str, when: dt.datetime) -> dict[LegRole, LegMark]:
    """Both shorts quoted ``short`` (bid) / +0.10 (ask), both wings ``wing`` / +0.10."""
    s, w = Decimal(short), Decimal(wing)
    tick = Decimal("0.10")
    return {
        LegRole.SHORT_CALL: LegMark(s, s + tick, when),
        LegRole.SHORT_PUT: LegMark(s, s + tick, when),
        LegRole.LONG_CALL: LegMark(w, w + tick, when),
        LegRole.LONG_PUT: LegMark(w, w + tick, when),
    }


def long_call(entry: str = "200.00", opened: dt.datetime | None = None) -> OpenPosition:
    return OpenPosition(
        sleeve=Sleeve.O2,
        structure=Structure.LONG_OPTION,
        legs=(OpenLeg(LegRole.LONG_CALL, Decimal("25000"), QTY, Decimal(entry), 9),),
        entry_points=Decimal(entry),
        opened_at=opened or at(10, 6),
        risk_budget_inr=Decimal("50000"),
        direction=Direction.UP,
        range_high=Decimal("25032"),
        range_low=Decimal("25008"),
    )


def spread(sleeve: Sleeve = Sleeve.O3A, debit: str = "30.00") -> OpenPosition:
    return OpenPosition(
        sleeve=sleeve,
        structure=Structure.DEBIT_SPREAD,
        legs=(
            OpenLeg(LegRole.LONG_CALL, Decimal("25200"), QTY, Decimal("35.00"), 11),
            OpenLeg(LegRole.SHORT_CALL, Decimal("25300"), QTY, Decimal("5.00"), 12),
        ),
        entry_points=Decimal(debit),
        opened_at=at(10, 22),
        risk_budget_inr=Decimal("50000"),
        direction=Direction.UP,
        range_high=Decimal("25182"),
        range_low=Decimal("25003"),
        half_gap=Decimal("25100"),
        width_points=Decimal("100"),
    )


def spread_marks(long: str, short: str, when: dt.datetime) -> dict[LegRole, LegMark]:
    tick = Decimal("0.10")
    return {
        LegRole.LONG_CALL: LegMark(Decimal(long), Decimal(long) + tick, when),
        LegRole.SHORT_CALL: LegMark(Decimal(short), Decimal(short) + tick, when),
    }


def live(spot: str, when: dt.datetime, bars: tuple[Bar, ...] = ()) -> IndexState:
    return IndexState(spot=Decimal(spot), last_tick_at=when, bars=bars)


# --- the mark and the loss -----------------------------------------------------------------------


class TestTheMark:
    def test_a_condor_is_marked_at_the_cost_to_close(self) -> None:
        """Shorts bought back at the ask, wings sold at the bid: 2 x 10.10 - 2 x 2.00 = 16.20."""
        now = at(11, 0)
        m = mark(condor(), condor_marks("10.00", "2.00", now), now, CFG)
        assert m == type(m)(Decimal("16.20"), False)

    def test_a_spread_is_marked_at_what_closing_receives(self) -> None:
        """Long sold at the bid, short bought at the ask: 50.00 - 10.10 = 39.90."""
        now = at(11, 0)
        m = mark(spread(), spread_marks("50.00", "10.00", now), now, CFG)
        assert m.value_points == Decimal("39.90") and not m.stale

    def test_an_old_quote_makes_the_mark_stale(self) -> None:
        now = at(11, 0)
        old = now - dt.timedelta(seconds=CFG.chain.stale_quote_seconds + 1)
        marks = spread_marks("50.00", "10.00", now) | {LegRole.SHORT_CALL: q("10.00", "10.10", old)}
        assert mark(spread(), marks, now, CFG).stale

    def test_a_one_sided_leg_has_no_mark(self) -> None:
        now = at(11, 0)
        marks = spread_marks("50.00", "10.00", now) | {
            LegRole.SHORT_CALL: LegMark(Decimal("10.00"), None, now)
        }
        m = mark(spread(), marks, now, CFG)
        assert m.value_points is None and m.stale

    def test_the_marked_loss_is_signed_by_structure(self) -> None:
        # A condor loses when the cost to close rises above the credit.
        assert marked_loss_inr(condor("40.00"), Decimal("50.00")) == Decimal("650.00")
        # A spread loses when what closing receives falls below the debit.
        assert marked_loss_inr(spread(debit="30.00"), Decimal("20.00")) == Decimal("650.00")
        assert marked_loss_inr(long_call("200.00"), Decimal("210.00")) == Decimal("-650.00")


# --- each structure's rules, through one function ------------------------------------------------


class TestTheCondor:
    def test_profit_at_half_the_credit(self) -> None:
        """Condor §7: PROFIT when D <= 0.5 x C = 20 (D = 16.20 here)."""
        now = at(11, 0)
        v = evaluate(
            condor(), condor_marks("10.00", "2.00", now), live("25000", now), now, options=CFG
        )
        assert v is not None and (v.code, v.reason) == ("PROFIT", RULE)
        assert v.value_points == Decimal("16.20")

    def test_a_stale_mark_drops_the_profit_but_not_the_strike_touch(self) -> None:
        now = at(11, 0)
        old = now - dt.timedelta(seconds=60)
        marks = condor_marks("10.00", "2.00", old)
        assert evaluate(condor(), marks, live("25000", now), now, options=CFG) is None
        touched = evaluate(condor(), marks, live("25150", now), now, options=CFG)
        assert touched is not None and touched.code == "STRIKE_TOUCH"

    def test_the_hard_exit_outranks_everything(self) -> None:
        now = at(14, 30)
        v = evaluate(
            condor(), condor_marks("10.00", "2.00", now), live("25150", now), now, options=CFG
        )
        assert v is not None and v.code == "HARD_EXIT"

    def test_holding_between_the_rules(self) -> None:
        now = at(11, 0)
        marks = condor_marks("20.00", "1.00", now)  # D = 2 x 20.10 - 2 x 1.00 = 38.20
        assert evaluate(condor(), marks, live("25000", now), now, options=CFG) is None

    def test_an_unknown_index_holds_the_touch_rule_only(self) -> None:
        """OP13.2: a restart before the first index tick. Nothing raises; the premium rules and
        the clock still judge, and only the strike touch waits for a spot."""
        now = at(11, 0)
        blind = IndexState(spot=None, last_tick_at=None)
        assert evaluate(condor(), condor_marks("20.00", "1.00", now), blind, now,
                        options=CFG) is None  # fmt: skip
        stop = evaluate(condor(), condor_marks("35.00", "1.00", now), blind, now, options=CFG)
        assert stop is not None and stop.code == "STOP"  # D = 68.20 >= 1.5 x 40
        late = at(14, 30)
        hard = evaluate(condor(), condor_marks("20.00", "1.00", late), blind, late, options=CFG)
        assert hard is not None and hard.code == "HARD_EXIT"


class TestTheLong:
    def test_the_time_stop_after_45_minutes_without_the_gain(self) -> None:
        """``04`` §4.5: held 45 minutes and the bid below E x (1 + time_stop_min_gain)."""
        opened = at(10, 6)
        now = opened + dt.timedelta(minutes=CFG.directional.time_stop_minutes)
        bars = flat_bars((10, 0), (10, 50), "25060")  # above the range: not invalidated
        marks = {LegRole.LONG_CALL: q("201.00", "201.50", now)}
        v = evaluate(long_call(opened=opened), marks, live("25060", now, bars), now, options=CFG)
        assert v is not None and v.code == "TIME_STOP"

    def test_before_45_minutes_it_holds(self) -> None:
        opened = at(10, 6)
        now = opened + dt.timedelta(minutes=CFG.directional.time_stop_minutes - 1)
        bars = flat_bars((10, 0), (10, 50), "25060")
        marks = {LegRole.LONG_CALL: q("201.00", "201.50", now)}
        assert (
            evaluate(long_call(opened=opened), marks, live("25060", now, bars), now, options=CFG)
            is None
        )

    def test_a_five_minute_close_back_inside_the_range_invalidates(self) -> None:
        now = at(10, 21)
        bars = flat_bars((10, 5), (10, 19), "25020")  # the 10:15-10:19 bar closes inside
        marks = {LegRole.LONG_CALL: q("199.00", "199.50", now)}
        v = evaluate(long_call(), marks, live("25020", now, bars), now, options=CFG)
        assert v is not None and v.code == "INVALIDATED"

    def test_target_on_the_bid(self) -> None:
        now = at(10, 30)
        target = Decimal("200.00") * (1 + CFG.directional.target_frac)
        marks = {LegRole.LONG_CALL: LegMark(target, target + 1, now)}
        v = evaluate(
            long_call(),
            marks,
            live("25100", now, flat_bars((10, 5), (10, 29), "25100")),
            now,
            options=CFG,
        )
        assert v is not None and v.code == "TARGET"


class TestTheSpread:
    def test_o3_target_at_80_percent_of_the_width(self) -> None:
        now = at(11, 0)
        bars = flat_bars((10, 20), (10, 59), "25300")
        v = evaluate(
            spread(), spread_marks("85.00", "4.90", now), live("25300", now, bars), now, options=CFG
        )
        assert v is not None and v.code == "TARGET"  # V = 85.00 - 5.00 = 80.00

    def test_o3_stop_at_half_the_debit(self) -> None:
        now = at(11, 0)
        bars = flat_bars((10, 20), (10, 59), "25250")
        v = evaluate(
            spread(), spread_marks("20.00", "5.90", now), live("25250", now, bars), now, options=CFG
        )
        assert v is not None and v.code == "STOP"  # V = 20.00 - 6.00 = 14.00 <= 15.00

    def test_o3a_invalidated_by_a_close_back_inside_the_morning_range(self) -> None:
        now = at(10, 31)
        bars = flat_bars((10, 20), (10, 29), "25150")  # the 10:25-10:29 bar closes below 25,182
        v = evaluate(
            spread(), spread_marks("35.00", "5.00", now), live("25150", now, bars), now, options=CFG
        )
        assert v is not None and v.code == "INVALIDATED"

    def test_o3b_invalidated_by_any_bar_through_half_gap(self) -> None:
        now = at(10, 31)
        bars = (bar(10, 29, "25120", low="25099"),)
        v = evaluate(
            spread(Sleeve.O3B),
            spread_marks("35.00", "5.00", now),
            live("25120", now, bars),
            now,
            options=CFG,
        )
        assert v is not None and v.code == "INVALIDATED"


# --- no mark, the budget, the feed ---------------------------------------------------------------


class TestNoMarkTheBudgetAndTheFeed:
    def test_with_no_mark_only_the_clock_can_close(self) -> None:
        early, late = at(11, 0), at(14, 45)
        assert evaluate(spread(), {}, live("25000", early), early, options=CFG) is None
        v = evaluate(spread(), {}, live("25000", late), late, options=CFG)
        assert v is not None and v.code == "HARD_EXIT" and v.value_points is None

    def test_the_budget_breach_closes_as_stop(self) -> None:
        """``04`` §9.2: a marked loss beyond the risk budget is a STOP whatever the sleeve says."""
        now = at(11, 0)
        position = spread()
        small = OpenPosition(
            sleeve=position.sleeve,
            structure=position.structure,
            legs=position.legs,
            entry_points=position.entry_points,
            opened_at=position.opened_at,
            risk_budget_inr=Decimal("100"),
            direction=position.direction,
            range_high=position.range_high,
            range_low=position.range_low,
            width_points=position.width_points,
        )
        bars = flat_bars((10, 20), (10, 59), "25250")
        v = evaluate(
            small, spread_marks("33.00", "5.90", now), live("25250", now, bars), now, options=CFG
        )
        # V = 27.00: the spread rule holds (stop at 15.00), but the loss 3.00 x 65 = ₹195 > ₹100.
        assert v is not None and v.code == "STOP" and v.marked_loss_inr == Decimal("195.00")

    @pytest.mark.parametrize(
        ("now", "lost"),
        [(at(13, 59, 0), False), (at(14, 5, 0), True)],
    )
    def test_o1_loses_the_feed_only_after_14_00(self, now: dt.datetime, lost: bool) -> None:
        silent_since = now - dt.timedelta(seconds=CFG.chain.stale_index_seconds + 5)
        index = IndexState(spot=Decimal("25000"), last_tick_at=silent_since)
        v = evaluate(condor(), condor_marks("20.00", "1.00", now), index, now, options=CFG)
        if lost:
            assert v is not None and (v.code, v.reason) == ("HARD_EXIT", FEED_LOST)
        else:
            assert v is None

    def test_o2_loses_the_feed_at_any_time_after_the_grace(self) -> None:
        opened = at(10, 6)
        now = opened + dt.timedelta(seconds=CFG.execution.feed_grace_seconds + 30)
        index = IndexState(spot=Decimal("25060"), last_tick_at=now - dt.timedelta(minutes=2))
        marks = {LegRole.LONG_CALL: q("205.00", "205.50", now)}
        v = evaluate(long_call(opened=opened), marks, index, now, options=CFG)
        assert v is not None and (v.code, v.reason) == ("HARD_EXIT", FEED_LOST)


class TestTheBarsTheRulesRead:
    def test_the_five_minute_close_is_a_completed_bar_after_entry(self) -> None:
        bars = flat_bars((10, 0), (10, 13), "25050")
        # At 10:14:30 the 10:10-10:14 bar is not complete (its 10:14 minute is not closed).
        assert latest_five_minute_close(bars, at(10, 14, 30), at(10, 6), dt.time(9, 15), 5) == (
            Decimal("25050")
        )  # the 10:05-10:09 bar, which ended after the 10:06 entry
        assert latest_five_minute_close(bars, at(10, 14, 30), at(10, 10), dt.time(9, 15), 5) is None

    def test_the_bar_after_entry(self) -> None:
        bars = flat_bars((10, 0), (10, 5), "25050")
        found = latest_bar_after(bars, at(10, 6), at(10, 3))
        assert found is not None and found.ts == at(10, 5)
        assert latest_bar_after(bars, at(10, 6), at(10, 7)) is None
