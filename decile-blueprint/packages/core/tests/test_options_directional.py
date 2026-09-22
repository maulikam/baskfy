"""OP4 — O2, directional buying (``04`` §4; PACK.5): filters, trigger, contract, exits.

The trend is NIFTY 50's own EMA20 on purpose (kickoff note) — these tests feed NIFTY 50 closes and
nothing else, and the scan labels the index it read (``test_options_scan``).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal

import pytest
from options_fixtures import LOT, TICK
from options_scan_fixtures import (
    O2_COUNTER,
    O2_UP_BREAK,
    PREV_CLOSE,
    SETTLE,
    at,
    bars_from_closes,
    carry_forward,
    falling_closes,
    flat,
    o2_counter_bars,
    o2_up_break_bars,
    rising_closes,
    snapshot,
    zigzag,
)

from baskfy_core.options import directional
from baskfy_core.options.bars import IST, Bar
from baskfy_core.options.config import (
    DirectionalConfig,
    OptionsCeilings,
    OptionsConfig,
    OptionType,
    Side,
    SizingMode,
)
from baskfy_core.options.costs import CostFill, charges
from baskfy_core.options.directional import DayReason, LongExit
from baskfy_core.options.execution import LegRole
from baskfy_core.options.scan import Snapshot
from baskfy_core.options.structures import (
    Candidate,
    ChainView,
    Direction,
    Rejection,
    SleeveBook,
    Structure,
    chain_view,
)

CFG = OptionsConfig()
D = CFG.directional
CEIL = OptionsCeilings()
GRACE = CFG.chain.stale_scan_seconds
TUE = dt.date(2026, 10, 20)


def filters(
    bars: tuple[Bar, ...] | None = None,
    now: dt.datetime | None = None,
    *,
    daily: tuple[Decimal, ...] | None = None,
    vix: Decimal | None = Decimal(14),
    event: bool = False,
) -> directional.DayFilters:
    return directional.day_filters(
        bars if bars is not None else o2_up_break_bars(),
        daily if daily is not None else rising_closes(),
        vix,
        now=now or at(O2_UP_BREAK, 9, 30),
        event_day=event,
        config=D,
        grace_seconds=GRACE,
    )


class TestTheTrend:
    def test_the_ema_is_sma_seeded_then_recursive(self) -> None:
        # Seed mean(1, 2, 3) = 2; alpha = 2 / 4 = 0.5; 4 → 3; 5 → 4.
        closes = tuple(Decimal(x) for x in (1, 2, 3, 4, 5))
        assert directional.ema(closes, 3) == Decimal(4)
        assert directional.ema(closes[:2], 3) is None

    def test_up_down_flat_unknown(self) -> None:
        assert directional.trend(rising_closes(), 20)[0] is Direction.UP
        assert directional.trend(falling_closes(), 20)[0] is Direction.DOWN
        assert directional.trend((PREV_CLOSE,) * 30, 20) == (None, PREV_CLOSE)
        assert directional.trend(rising_closes(count=19), 20) == (None, None)


class TestTheDayFilters:
    def test_the_up_break_day_passes_at_0930_with_its_numbers(self) -> None:
        f = filters()
        assert f.final and f.trade and f.reasons == ()
        assert f.trend is Direction.UP
        assert f.prev_close == PREV_CLOSE
        assert f.gap_pct == Decimal("0.08")  # 25,020 / 25,000
        # A ±10 zig-zag around 25,020 with 2-point wicks: 25,032 / 25,008.
        assert (f.or_high, f.or_low) == (Decimal(25032), Decimal(25008))
        assert f.or_pct == Decimal("0.096")
        assert f.bars == D.opening_range_bars

    def test_not_final_before_the_0929_bar_closes(self) -> None:
        f = filters(now=dt.datetime(2026, 10, 19, 9, 29, 40, tzinfo=IST))
        assert not f.final and f.bars == 14

    @pytest.mark.parametrize(
        ("kwargs", "reason"),
        [
            ({"daily": (PREV_CLOSE,) * 30}, DayReason.TREND_FLAT),
            ({"daily": rising_closes(count=10)}, DayReason.TREND_UNKNOWN),
            ({"vix": Decimal("22.01")}, DayReason.VIX_TOO_HIGH),
            ({"vix": None}, DayReason.VIX_UNKNOWN),
            ({"event": True}, DayReason.EVENT_DAY),
        ],
    )
    def test_each_reason(self, kwargs: dict[str, object], reason: DayReason) -> None:
        f = _filters_from(kwargs)
        assert f.reasons == (reason,)

    def test_vix_at_the_limit_is_allowed(self) -> None:
        assert filters(vix=D.vix_max).reasons == ()

    def test_gap_and_range_and_all_reasons_together(self) -> None:
        # Open +1.2 % and a ±150 zig-zag: gap, range, and a flat trend on an event day.
        bars = bars_from_closes(
            O2_UP_BREAK, Decimal(25300), zigzag(Decimal(25300), Decimal(150), 30)
        )
        f = filters(bars, daily=(PREV_CLOSE,) * 30, vix=Decimal(30), event=True)
        assert f.reasons == (
            DayReason.TREND_FLAT,
            DayReason.GAP_TOO_BIG,
            DayReason.RANGE_TOO_WIDE,
            DayReason.VIX_TOO_HIGH,
            DayReason.EVENT_DAY,
        )

    def test_a_missing_minute_is_incomplete_once_settled(self) -> None:
        bars = tuple(b for b in o2_up_break_bars() if b.ts != at(O2_UP_BREAK, 9, 20))
        f = filters(bars, at(O2_UP_BREAK, 9, 31))
        assert f.final  # the 09:29 bar is stored
        assert f.reasons == (DayReason.INCOMPLETE_OBSERVATION,)


def _filters_from(kwargs: dict[str, object]) -> directional.DayFilters:
    daily = kwargs.get("daily")
    vix = kwargs.get("vix", Decimal(14))
    return filters(
        daily=daily if isinstance(daily, tuple) else None,
        vix=vix if isinstance(vix, Decimal) else None,
        event=bool(kwargs.get("event", False)),
    )


def trigger(bars: tuple[Bar, ...], now: dt.datetime) -> directional.TriggerScan:
    return directional.find_trigger(
        bars,
        or_high=Decimal(25032),
        or_low=Decimal(25008),
        direction=Direction.UP,
        now=now,
        config=D,
        session_open=CFG.calendar.market_open,
        grace_seconds=GRACE,
    )


class TestTheTrigger:
    def test_the_levels_carry_the_buffer(self) -> None:
        up, down = directional.levels(Decimal(25032), Decimal(25008), D.buffer_pct)
        assert up == Decimal(25032) * Decimal("1.0005") == Decimal("25044.516")
        assert down == Decimal(25008) * Decimal("0.9995") == Decimal("24995.496")

    def test_the_first_completed_five_minute_close_beyond_the_level(self) -> None:
        t = trigger(o2_up_break_bars(), at(O2_UP_BREAK, 10, 5))
        assert t.trigger == directional.Break(dt.time(10, 4), Decimal(25050), Direction.UP)
        assert t.counter_breaks == ()

    def test_not_before_the_bar_has_closed(self) -> None:
        t = trigger(o2_up_break_bars(), dt.datetime(2026, 10, 19, 10, 4, 50, tzinfo=IST))
        assert t.trigger is None and not t.window_closed

    def test_a_counter_trend_break_is_recorded_never_traded(self) -> None:
        t = trigger(o2_counter_bars(), at(O2_COUNTER, 10, 30))
        assert t.trigger is None
        # 25,030 - 4 a minute: the 10:05-10:09 bar closes 24,990 < 24,995.496.
        assert t.counter_breaks[0] == directional.Break(
            dt.time(10, 9), Decimal(24990), Direction.DOWN
        )

    def test_the_window_closes_after_1330_plus_the_grace(self) -> None:
        bars = o2_counter_bars()
        assert not trigger(bars, at(O2_COUNTER, 13, 32)).window_closed
        assert trigger(bars, at(O2_COUNTER, 13, 33)).window_closed

    def test_a_break_after_the_window_is_not_a_trigger(self) -> None:
        # Flat at 25,020 until 13:30, then a jump: the 13:30-13:34 bar closes past the window.
        closes = [Decimal(25020)] * (4 * 60 + 15) + [Decimal(25100)] * 30
        bars = bars_from_closes(O2_UP_BREAK, Decimal(25020), closes, wick=Decimal(0))
        t = directional.find_trigger(
            bars,
            or_high=Decimal(25020),
            or_low=Decimal(25020),
            direction=Direction.UP,
            now=at(O2_UP_BREAK, 14, 0),
            config=D,
            session_open=CFG.calendar.market_open,
            grace_seconds=GRACE,
        )
        assert t.trigger is None and t.window_closed


# --- the contract (04 §4.3-§4.6) -----------------------------------------------------------------


NOW = at(O2_UP_BREAK, 10, 5)
SPOT = Decimal(25050)


def o2_snapshot(*, depth: int = 1300, vol: float = 0.13) -> Snapshot:
    fwd = {e: carry_forward(SPOT, NOW, e) for e in (TUE, dt.date(2026, 10, 27))}
    return snapshot(NOW, SPOT, [TUE, dt.date(2026, 10, 27)], flat(vol), forwards=fwd, depth=depth)


def view_of(snap: Snapshot, expiry: dt.date = TUE) -> ChainView:
    view = chain_view(
        snap.quotes, expiry=expiry, spot=SPOT, now=NOW, tick=TICK, config=CFG.chain, settle=SETTLE
    )
    assert view is not None
    return view


def build(
    view: ChainView | None,
    direction: Direction = Direction.UP,
    *,
    book: SleeveBook | None = None,
    config: DirectionalConfig = D,
    paused: bool = False,
) -> Candidate:
    return directional.build(
        view,
        direction,
        lot_size=LOT,
        book=book or SleeveBook(),
        config=config,
        options=CFG,
        ceilings=CEIL,
        expiry=TUE,
        paused=paused,
    )


class TestTheContract:
    def test_one_step_itm_call_on_the_up_break_to_the_rupee(self) -> None:
        view = view_of(o2_snapshot())
        c = build(view)
        assert c.viable and c.structure is Structure.LONG_OPTION
        (lg,) = c.legs
        assert view.atm == Decimal(25050)
        assert (lg.role, lg.strike, lg.option_type) == (
            LegRole.LONG_CALL, Decimal(25000), OptionType.CE,
        )  # fmt: skip
        assert lg.delta is not None and D.delta_min <= Decimal(repr(abs(lg.delta))) <= D.delta_max
        entry = (lg.ask or 0) + TICK
        assert c.points == lg.limit_price == entry == Decimal("106.75")
        assert c.risk_per_lot_inr == entry * D.stop_frac * LOT + D.reserve_per_lot_inr
        assert c.risk_per_lot_inr == Decimal("2381.625")
        assert (c.lots, c.sizing_mode) == (1, SizingMode.PAPER_ONE_LOT)
        assert c.extra["gap_through_inr"] == str(entry * LOT)
        fills = [
            CostFill(Side.BUY, entry, LOT),
            CostFill(Side.SELL, (lg.bid or 0) - TICK, LOT),
        ]
        assert c.round_trip_inr == charges(fills, CFG.costs).total
        gain = D.target_frac * entry * LOT
        assert c.expected_gain_inr == gain
        assert c.cost_share is not None and c.cost_share <= D.cost_share_max

    def test_one_step_itm_put_on_a_down_break(self) -> None:
        c = build(view_of(o2_snapshot()), Direction.DOWN)
        (lg,) = c.legs
        assert (lg.role, lg.strike, lg.option_type) == (
            LegRole.LONG_PUT, Decimal(25100), OptionType.PE,
        )  # fmt: skip

    def test_delta_outside_the_band_rejects(self) -> None:
        deep = replace(D, itm_steps=8)  # 400 points in the money: |delta| near 1
        assert build(view_of(o2_snapshot()), config=deep).rejection is Rejection.REJECTED_DELTA

    def test_the_premium_cap(self) -> None:
        # ₹20 lakh at 0.5 % = ₹10,000, halved (first live) = ₹5,000 / ₹2,381.625 → 2 lots, whose
        # premium 2 * 106.75 * 65 = ₹13,877.50 is inside 10 % of capital (₹2 lakh) but not 0.5 %.
        rich = build(view_of(o2_snapshot()), book=SleeveBook(sleeve_capital_inr=Decimal(2_000_000)))
        assert rich.lots == 2 and rich.half_size
        capped = replace(D, premium_cap_pct=Decimal("0.5"))
        c = build(
            view_of(o2_snapshot()),
            book=SleeveBook(sleeve_capital_inr=Decimal(2_000_000)),
            config=capped,
        )
        assert c.rejection is Rejection.REJECTED_PREMIUM_CAP

    def test_illiquid_at_the_quantity_rejects(self) -> None:
        c = build(view_of(o2_snapshot(depth=10)))
        assert c.rejection is Rejection.REJECTED_ILLIQUID

    def test_paused_and_no_chain(self) -> None:
        assert build(view_of(o2_snapshot()), paused=True).rejection is Rejection.REJECTED_PAUSED
        assert build(None).rejection is Rejection.REJECTED_NO_CHAIN


# --- exits (04 §4.5) -----------------------------------------------------------------------------


E = Decimal(100)
ENTERED = at(O2_UP_BREAK, 10, 6)


def decide(  # noqa: PLR0913 - the test's knobs
    bid: str,
    *,
    minutes: int = 5,
    clock: dt.datetime | None = None,
    invalidated: bool = False,
    stale: bool = False,
    loss: str = "0",
) -> LongExit | None:
    return directional.exit_decision(
        entry=E,
        bid=Decimal(bid),
        entered_at=ENTERED,
        now=clock or ENTERED + dt.timedelta(minutes=minutes),
        is_invalidated=invalidated,
        stale=stale,
        marked_loss_inr=Decimal(loss),
        risk_budget_inr=Decimal(2000),
        config=D,
        options=CFG,
    )


class TestExits:
    @pytest.mark.parametrize(
        ("bid", "expected"),
        [("70", LongExit.STOP), ("70.05", None), ("159.95", None), ("160", LongExit.TARGET)],
    )
    def test_stop_at_minus_30_target_at_plus_60(self, bid: str, expected: LongExit | None) -> None:
        assert decide(bid) is expected

    def test_the_time_stop_at_45_minutes_below_plus_10(self) -> None:
        assert decide("109.95", minutes=45) is LongExit.TIME_STOP
        assert decide("110", minutes=45) is None
        assert decide("100", minutes=44) is None

    def test_hard_exit_at_1500(self) -> None:
        assert decide("100", clock=at(O2_UP_BREAK, 15, 0)) is LongExit.HARD_EXIT

    def test_invalidation_by_a_close_back_inside_the_range(self) -> None:
        assert directional.invalidated(
            Decimal("25031.95"), Direction.UP, Decimal(25032), Decimal(25008)
        )
        assert not directional.invalidated(
            Decimal(25032), Direction.UP, Decimal(25032), Decimal(25008)
        )
        assert directional.invalidated(
            Decimal("25008.05"), Direction.DOWN, Decimal(25032), Decimal(25008)
        )
        assert decide("120", invalidated=True) is LongExit.INVALIDATED

    def test_precedence(self) -> None:
        assert decide("60", clock=at(O2_UP_BREAK, 15, 0)) is LongExit.HARD_EXIT
        assert decide("60", invalidated=True) is LongExit.STOP
        assert decide("170", invalidated=True) is LongExit.INVALIDATED
        assert decide("170", minutes=50) is LongExit.TARGET

    def test_no_target_on_a_stale_mark_but_the_stop_holds(self) -> None:
        assert decide("170", stale=True) is None
        assert decide("60", stale=True) is LongExit.STOP

    def test_a_budget_breach_is_a_stop(self) -> None:
        assert decide("95", loss="2000.01") is LongExit.STOP
