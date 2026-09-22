"""OP4 — O3, the expiry-day setups (``04`` §5; PACK.10): O3-A range break, O3-B gap hold, the
debit spread, its exits, and the never-naked property through the OP1 harness."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from options_fixtures import LOT, TICK
from options_scan_fixtures import (
    GAP_HOLD,
    PREV_CLOSE,
    SETTLE,
    TREND_WEEKLY,
    at,
    bars_from_closes,
    carry_forward,
    flat,
    gap_hold_bars,
    ramp,
    snapshot,
    trend_weekly_bars,
    zigzag,
)
from test_options_execution import _walk_entry, _walk_exit

from baskfy_core.options import expiry_setups
from baskfy_core.options.bars import IST, Bar
from baskfy_core.options.config import (
    ExpirySetupsConfig,
    OptionsCeilings,
    OptionsConfig,
    OptionType,
    Side,
    SizingMode,
)
from baskfy_core.options.costs import CostFill, charges
from baskfy_core.options.execution import LegRole, entry_sequence, exit_sequence
from baskfy_core.options.expiry_setups import SetupReason, SpreadExit
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
X = CFG.expiry_setups
CEIL = OptionsCeilings()
GRACE = CFG.chain.stale_scan_seconds
OPEN = CFG.calendar.market_open


def range_break(
    bars: tuple[Bar, ...], now: dt.datetime, *, event: bool = False
) -> expiry_setups.RangeBreakScan:
    return expiry_setups.range_break(
        bars, PREV_CLOSE, now=now, event_day=event, config=X, session_open=OPEN,
        grace_seconds=GRACE,
    )  # fmt: skip


def gap_hold(
    bars: tuple[Bar, ...], now: dt.datetime, *, event: bool = False
) -> expiry_setups.GapHoldScan:
    return expiry_setups.gap_hold(
        bars, PREV_CLOSE, now=now, event_day=event, config=X, session_open=OPEN,
        grace_seconds=GRACE,
    )  # fmt: skip


class TestO3ARangeBreak:
    def test_the_trend_weekly_triggers_at_1019_with_a_high_er(self) -> None:
        r = range_break(trend_weekly_bars(), at(TREND_WEEKLY, 10, 20))
        assert r.final and r.reasons == ()
        # 60 bars 09:15-10:14, closes 25,003 … 25,180, 2-point wicks.
        assert r.bars == X.o3a_range_bars
        assert (r.range_high, r.range_low) == (Decimal(25182), Decimal(24998))
        assert r.range_pct == Decimal("0.736")
        assert r.level_up == Decimal(25182) * Decimal("1.0005")
        assert (r.trigger_time, r.trigger_close, r.direction) == (
            dt.time(10, 19), Decimal(25210), Direction.UP,
        )  # fmt: skip
        assert r.trigger_er == 1

    def test_the_range_must_be_settled_first(self) -> None:
        r = range_break(trend_weekly_bars(), dt.datetime(2026, 10, 20, 10, 14, 30, tzinfo=IST))
        assert not r.final and r.trigger_time is None

    def test_a_wide_morning_range_skips(self) -> None:
        bars = bars_from_closes(TREND_WEEKLY, Decimal(25000), ramp(Decimal(25000), Decimal(6), 120))
        r = range_break(bars, at(TREND_WEEKLY, 10, 30))
        assert r.reasons == (SetupReason.RANGE_TOO_WIDE,)
        assert r.trigger_time is None

    def test_a_break_with_a_low_er_is_recorded_and_the_watch_goes_on(self) -> None:
        # A ±60 chop for the morning (range 0.49 %), then a slow grind up: the first break's ER
        # over 09:15→break is far below 0.40 — recorded, not traded.
        chop = zigzag(Decimal(25000), Decimal(60), 60)
        grind = ramp(chop[-1], Decimal(3), 60)
        r = range_break(bars_from_closes(TREND_WEEKLY, Decimal(25000), chop + grind),
                        at(TREND_WEEKLY, 11, 15))  # fmt: skip
        assert r.trigger_time is None
        assert r.low_er_breaks, "the break must be seen"
        assert all(er < X.o3a_er_min for _, _, er in r.low_er_breaks)

    def test_a_break_after_1300_is_not_a_trigger(self) -> None:
        flat_day = [Decimal(25000)] * (13 * 60 + 5 - (9 * 60 + 15))
        bars = bars_from_closes(TREND_WEEKLY, Decimal(25000), flat_day + [Decimal(25300)] * 10)
        r = range_break(bars, at(TREND_WEEKLY, 13, 30))
        assert r.trigger_time is None and r.window_closed

    def test_event_day(self) -> None:
        r = range_break(trend_weekly_bars(), at(TREND_WEEKLY, 10, 20), event=True)
        assert r.reasons == (SetupReason.EVENT_DAY,)


class TestO3BGapHold:
    def test_the_gap_holds_and_triggers_at_0945(self) -> None:
        g = gap_hold(gap_hold_bars(), at(GAP_HOLD, 9, 45))
        assert g.final and g.reasons == () and g.triggered
        assert g.gap_points == Decimal(200) and g.gap_pct == Decimal("0.8")
        assert g.half_gap == Decimal(25100) and g.direction is Direction.UP
        assert g.held_so_far is True and g.bars == 30

    def test_not_before_the_plan_time(self) -> None:
        g = gap_hold(gap_hold_bars(), dt.datetime(2026, 10, 13, 9, 44, 50, tzinfo=IST))
        assert not g.triggered

    @pytest.mark.parametrize(
        ("open_", "reason"),
        [("25120", SetupReason.GAP_TOO_SMALL), ("25400", SetupReason.GAP_TOO_BIG)],
    )
    def test_the_gap_band(self, open_: str, reason: SetupReason) -> None:
        o = Decimal(open_)
        g = gap_hold(bars_from_closes(GAP_HOLD, o, zigzag(o, Decimal(5), 40)), at(GAP_HOLD, 9, 45))
        assert reason in g.reasons and not g.triggered

    def test_the_band_edges_are_inclusive(self) -> None:
        for pct in (X.o3b_gap_min_pct, X.o3b_gap_max_pct):
            o = PREV_CLOSE * (1 + pct / 100)
            g = gap_hold(
                bars_from_closes(GAP_HOLD, o, zigzag(o, Decimal(1), 40)), at(GAP_HOLD, 9, 45)
            )
            assert g.reasons == () and g.triggered, pct

    def test_a_bar_through_half_the_gap_breaks_the_hold(self) -> None:
        closes = zigzag(Decimal(25200), Decimal(10), 40)
        closes[20] = Decimal(25101)  # its low wicks to 25,099 ≤ 25,100
        g = gap_hold(bars_from_closes(GAP_HOLD, Decimal(25200), closes), at(GAP_HOLD, 9, 45))
        assert g.reasons == (SetupReason.HOLD_BROKEN,) and not g.triggered

    def test_a_down_gap_is_symmetric(self) -> None:
        o = Decimal(24800)
        g = gap_hold(bars_from_closes(GAP_HOLD, o, zigzag(o, Decimal(10), 40)), at(GAP_HOLD, 9, 45))
        assert g.direction is Direction.DOWN and g.half_gap == Decimal(24900) and g.triggered


# --- the debit spread (04 §5, §5.5, §6.4) --------------------------------------------------------


NOW = at(GAP_HOLD, 9, 45)
SPOT = Decimal(25190)  # the 09:44 close


def gap_snapshot(*, vol: float = 0.14, depth: int = 1300) -> Snapshot:
    exps = [GAP_HOLD, dt.date(2026, 10, 20)]
    fwd = {e: carry_forward(SPOT, NOW, e) for e in exps}
    return snapshot(NOW, SPOT, exps, flat(vol), forwards=fwd, depth=depth)


def view_of(snap: Snapshot) -> ChainView:
    view = chain_view(
        snap.quotes, expiry=GAP_HOLD, spot=SPOT, now=NOW, tick=TICK, config=CFG.chain,
        settle=SETTLE,
    )  # fmt: skip
    assert view is not None
    return view


def build(
    view: ChainView | None,
    direction: Direction = Direction.UP,
    *,
    config: ExpirySetupsConfig = X,
    slot_free: bool = True,
    book: SleeveBook | None = None,
) -> Candidate:
    return expiry_setups.build(
        view,
        direction,
        lot_size=LOT,
        book=book or SleeveBook(),
        config=config,
        options=CFG,
        ceilings=CEIL,
        expiry=GAP_HOLD,
        slot_free=slot_free,
    )


class TestTheSpread:
    def test_the_call_spread_to_the_rupee(self) -> None:
        view = view_of(gap_snapshot())
        c = build(view)
        assert c.viable and c.structure is Structure.DEBIT_SPREAD
        long_leg, short_leg = c.legs
        assert view.atm == Decimal(25200)
        assert (long_leg.role, long_leg.strike) == (LegRole.LONG_CALL, Decimal(25200))
        assert (short_leg.role, short_leg.strike) == (LegRole.SHORT_CALL, Decimal(25300))
        assert c.points == (long_leg.ask or 0) - (short_leg.bid or 0) == Decimal("26.95")
        assert c.points <= X.max_debit_frac * X.width_points
        assert c.risk_per_lot_inr == Decimal("26.95") * LOT + X.reserve_per_lot_inr
        assert c.max_loss_inr == Decimal("26.95") * LOT
        assert (c.lots, c.sizing_mode) == (1, SizingMode.PAPER_ONE_LOT)
        fills = [
            CostFill(Side.BUY, (long_leg.ask or 0) + TICK, LOT),
            CostFill(Side.SELL, (long_leg.bid or 0) - TICK, LOT),
            CostFill(Side.SELL, (short_leg.bid or 0) - TICK, LOT),
            CostFill(Side.BUY, (short_leg.ask or 0) + TICK, LOT),
        ]
        assert c.round_trip_inr == charges(fills, CFG.costs).total
        assert c.expected_gain_inr == (X.target_frac_of_width * X.width_points - c.points) * LOT
        assert c.cost_share is not None and c.cost_share <= X.cost_share_max

    def test_the_put_spread_goes_down_from_atm(self) -> None:
        long_leg, short_leg = build(view_of(gap_snapshot()), Direction.DOWN).legs
        assert (long_leg.role, long_leg.strike, long_leg.option_type) == (
            LegRole.LONG_PUT, Decimal(25200), OptionType.PE,
        )  # fmt: skip
        assert (short_leg.role, short_leg.strike) == (LegRole.SHORT_PUT, Decimal(25100))

    def test_a_debit_above_the_cap_rejects(self) -> None:
        tight = replace(X, max_debit_frac=Decimal("0.25"))  # 25 points < 26.95
        assert build(view_of(gap_snapshot()), config=tight).rejection is Rejection.REJECTED_DEBIT

    def test_the_slot_must_be_free(self) -> None:
        c = build(view_of(gap_snapshot()), slot_free=False)
        assert c.rejection is Rejection.REJECTED_SLOT_TAKEN

    def test_illiquid_at_the_quantity(self) -> None:
        assert build(view_of(gap_snapshot(depth=10))).rejection is Rejection.REJECTED_ILLIQUID

    def test_a_missing_short_strike(self) -> None:
        snap = gap_snapshot()
        gone = Snapshot(
            ts=snap.ts,
            spot=snap.spot,
            quotes=tuple(q for q in snap.quotes if q.strike != Decimal(25300)),
        )
        assert build(view_of(gone)).rejection is Rejection.REJECTED_NO_CONTRACT

    def test_never_naked_sequences(self) -> None:
        roles = tuple(lg.role for lg in build(view_of(gap_snapshot())).legs)
        assert entry_sequence(roles) == (LegRole.LONG_CALL, LegRole.SHORT_CALL)
        assert exit_sequence(roles) == (LegRole.SHORT_CALL, LegRole.LONG_CALL)

    @settings(max_examples=300, deadline=None, derandomize=True)
    @given(fills=st.lists(st.integers(min_value=0, max_value=LOT), max_size=6))
    def test_never_naked_through_entry(self, fills: list[int]) -> None:
        _walk_entry((LegRole.LONG_PUT, LegRole.SHORT_PUT), LOT, fills)

    @settings(max_examples=300, deadline=None, derandomize=True)
    @given(fills=st.lists(st.integers(min_value=1, max_value=LOT), max_size=20))
    def test_never_naked_through_exit(self, fills: list[int]) -> None:
        _walk_exit((LegRole.LONG_CALL, LegRole.SHORT_CALL), LOT, fills)


# --- exits (04 §5.3) -----------------------------------------------------------------------------


def decide(
    value: str, *, clock: tuple[int, int] = (11, 0), invalid: bool = False, stale: bool = False
) -> SpreadExit | None:
    return expiry_setups.exit_decision(
        value=Decimal(value),
        entry_debit=Decimal(30),
        width=X.width_points,
        is_invalidated=invalid,
        now=at(GAP_HOLD, *clock),
        stale=stale,
        marked_loss_inr=Decimal(0),
        risk_budget_inr=Decimal(2450),
        config=X,
        options=CFG,
    )


class TestExits:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("15", SpreadExit.STOP), ("15.05", None), ("79.95", None), ("80", SpreadExit.TARGET)],
    )
    def test_target_at_80_pct_of_width_stop_at_half_the_debit(
        self, value: str, expected: SpreadExit | None
    ) -> None:
        assert decide(value) is expected

    def test_hard_exit_at_1445_first(self) -> None:
        assert decide("10", clock=(14, 45)) is SpreadExit.HARD_EXIT
        assert decide("40", clock=(14, 44)) is None

    def test_precedence_and_staleness(self) -> None:
        assert decide("10", invalid=True) is SpreadExit.STOP
        assert decide("90", invalid=True) is SpreadExit.INVALIDATED
        assert decide("90", stale=True) is None
        assert decide("10", stale=True) is SpreadExit.STOP

    def test_the_close_value_is_long_bid_minus_short_ask(self) -> None:
        assert expiry_setups.close_value(Decimal("50.10"), Decimal("12.40")) == Decimal("37.70")

    def test_invalidation_rules(self) -> None:
        hi, lo = Decimal(25182), Decimal(24998)
        assert expiry_setups.range_break_invalidated(Decimal("25181.95"), Direction.UP, hi, lo)
        assert not expiry_setups.range_break_invalidated(Decimal(25182), Direction.UP, hi, lo)
        assert expiry_setups.range_break_invalidated(Decimal("24998.05"), Direction.DOWN, hi, lo)
        bar = Bar(
            at(GAP_HOLD, 10, 0), Decimal(25110), Decimal(25112), Decimal(25100), Decimal(25105)
        )
        assert expiry_setups.gap_hold_invalidated(bar, Direction.UP, Decimal(25100))
        assert not expiry_setups.gap_hold_invalidated(bar, Direction.UP, Decimal("25099.95"))
        assert expiry_setups.gap_hold_invalidated(bar, Direction.DOWN, Decimal(25112))
