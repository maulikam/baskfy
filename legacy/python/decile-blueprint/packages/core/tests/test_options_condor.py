"""OP4 — O1, the condor: condor ``04`` §2 (gate), §4 (structure), §7 (exits) as ``04`` §3 ports it.

Every expectation is recomputed from the fixture's own bars and quotes by the rule's text; the
literal numbers are the same arithmetic written out, so a changed rule — not a changed
implementation — is what turns a test red.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from options_fixtures import LOT, TICK
from options_scan_fixtures import (
    PREV_CLOSE,
    QUIET_MONTHLY,
    SETTLE,
    TREND_WEEKLY,
    at,
    bars_from_closes,
    quiet_monthly_bars,
    ramp,
    skew,
    snapshot,
    trend_weekly_bars,
    zigzag,
)
from test_options_execution import _walk_entry, _walk_exit

from baskfy_core.options import condor
from baskfy_core.options.bars import IST, Bar, efficiency_ratio, er_points
from baskfy_core.options.condor import CondorExit, GateReason
from baskfy_core.options.config import (
    CondorConfig,
    CondorVariant,
    Mode,
    OptionsCeilings,
    OptionsConfig,
    OptionType,
    Side,
    SizingMode,
)
from baskfy_core.options.costs import CostFill, charges
from baskfy_core.options.execution import LegRole, entry_sequence, exit_sequence
from baskfy_core.options.scan import Snapshot
from baskfy_core.options.structures import (
    Candidate,
    ChainView,
    Rejection,
    SleeveBook,
    Structure,
    chain_view,
)

CFG = OptionsConfig()
M = CFG.condor_monthly
W = CFG.condor_weekly
CEIL = OptionsCeilings()
GRACE = CFG.chain.stale_scan_seconds
NOV3 = dt.date(2026, 11, 3)


def observe(
    bars: tuple[Bar, ...], now: dt.datetime, *, event: bool = False, config: CondorConfig = M
) -> condor.DayVerdict:
    return condor.observe(
        bars, PREV_CLOSE, now=now, event_day=event, config=config, grace_seconds=GRACE
    )


# --- Kaufman's ER (condor 04 §2.4, §2.8) ---------------------------------------------------------


class TestEfficiencyRatio:
    def test_a_straight_line_of_45_rising_closes_is_one(self) -> None:
        bars = bars_from_closes(QUIET_MONTHLY, Decimal(25000), ramp(Decimal(25000), Decimal(2), 45))
        assert efficiency_ratio(er_points(bars)) == 1

    def test_a_perfect_zigzag_is_near_zero(self) -> None:
        closes = zigzag(Decimal(25000), Decimal(10), 45)
        bars = bars_from_closes(QUIET_MONTHLY, Decimal(25000), closes)
        # Points: the 09:15 open 25,000, then the closes of bars 2..45 (24,990, 25,010, ...).
        # Net move 10 over a path of 10 + 43 * 20 = 870.
        assert efficiency_ratio(er_points(bars)) == Decimal(10) / Decimal(870)

    def test_no_path_is_zero_not_a_division(self) -> None:
        assert efficiency_ratio([Decimal(1)] * 45) == 0
        assert efficiency_ratio([]) == 0

    def test_c0_is_the_first_bars_open_not_its_close(self) -> None:
        bars = (
            Bar(at(QUIET_MONTHLY, 9, 15), Decimal(100), Decimal(111), Decimal(99), Decimal(110)),
            Bar(at(QUIET_MONTHLY, 9, 16), Decimal(110), Decimal(111), Decimal(104), Decimal(105)),
        )
        # Path open 100 → close 105: |105 - 100| / |105 - 100| = 1 (the 09:15 close is not a point).
        assert er_points(bars) == (Decimal(100), Decimal(105))
        assert efficiency_ratio(er_points(bars)) == 1


# --- the gate (condor 04 §2) ---------------------------------------------------------------------


class TestTheGate:
    def test_the_quiet_monthly_trades_with_its_numbers(self) -> None:
        v = observe(quiet_monthly_bars(), at(QUIET_MONTHLY, 10, 0))
        assert v.final and v.trade and v.reasons == ()
        n = v.numbers
        assert n.bars == 45
        assert n.open_0915 == Decimal(25010)
        assert n.gap_pct == Decimal("0.04")  # |25,010 / 25,000 - 1| * 100
        # ±8 around 25,010 with 2-point wicks: high 25,020, low 25,000 → 20 / 25,000.
        assert (n.obs_high, n.obs_low) == (Decimal(25020), Decimal(25000))
        assert n.range_pct == Decimal("0.08")
        assert n.contained is True
        assert n.er is not None and n.er <= M.er_max

    def test_not_final_while_the_window_fills_and_no_incomplete_reason(self) -> None:
        v = observe(quiet_monthly_bars(), at(QUIET_MONTHLY, 9, 50))
        assert not v.final and not v.trade
        assert GateReason.INCOMPLETE_OBSERVATION not in v.reasons
        assert v.numbers.bars == 35  # 09:15-09:49 closed by 09:50

    def test_the_trend_weekly_skips_for_containment_and_er_every_reason_reported(self) -> None:
        v = observe(trend_weekly_bars(), at(TREND_WEEKLY, 10, 0), config=W)
        assert v.final
        assert v.reasons == (GateReason.NOT_CONTAINED, GateReason.ER_TOO_HIGH)
        assert v.numbers.er == 1
        assert v.numbers.gap_pct == 0

    def test_every_reason_in_condor_order(self) -> None:
        # A 1 % gap, a wide straight run (range and ER), out of the opening range, on an event day.
        bars = bars_from_closes(QUIET_MONTHLY, Decimal(25250), ramp(Decimal(25250), Decimal(6), 45))
        v = observe(bars, at(QUIET_MONTHLY, 10, 0), event=True)
        assert v.reasons == (
            GateReason.GAP_TOO_BIG,
            GateReason.RANGE_TOO_WIDE,
            GateReason.NOT_CONTAINED,
            GateReason.ER_TOO_HIGH,
            GateReason.EVENT_DAY,
        )

    def test_gap_exactly_at_the_limit_is_not_a_reason(self) -> None:
        open_ = PREV_CLOSE * (1 + M.gap_max_pct / 100)  # 25,187.5
        bars = bars_from_closes(QUIET_MONTHLY, open_, zigzag(open_, Decimal(5), 45))
        v = observe(bars, at(QUIET_MONTHLY, 10, 0))
        assert v.numbers.gap_pct == M.gap_max_pct
        assert GateReason.GAP_TOO_BIG not in v.reasons

    def test_a_missing_minute_waits_for_the_grace_then_is_incomplete(self) -> None:
        bars = tuple(b for b in quiet_monthly_bars() if b.ts != at(QUIET_MONTHLY, 9, 59))
        early = observe(bars, dt.datetime(2026, 10, 27, 10, 1, 0, tzinfo=IST))
        assert not early.final
        late = observe(bars, at(QUIET_MONTHLY, 10, 0) + dt.timedelta(seconds=60 + GRACE))
        assert late.final
        assert late.reasons == (GateReason.INCOMPLETE_OBSERVATION,)

    def test_a_forming_minute_is_not_read(self) -> None:
        # At 09:59:30 the 09:59 bar has not closed: 44 bars, not final.
        v = observe(quiet_monthly_bars(), dt.datetime(2026, 10, 27, 9, 59, 30, tzinfo=IST))
        assert v.numbers.bars == 44 and not v.final

    def test_the_two_variants_are_distinct_objects_that_start_equal_but_for_risk(self) -> None:
        assert M is not W
        assert (M.variant, W.variant) == (CondorVariant.MONTHLY, CondorVariant.WEEKLY)
        aligned = replace(
            W,
            variant=M.variant,
            risk_per_trade_pct=M.risk_per_trade_pct,
            max_lots=M.max_lots,
            tier3_min_sessions=M.tier3_min_sessions,
        )
        assert aligned == M


# --- the structure (condor 04 §4, 04 §3, §6, §7) -------------------------------------------------


NOW = at(QUIET_MONTHLY, 10, 0)
SPOT = Decimal(25018)  # the 09:59 close
OR_HIGH, OR_LOW = Decimal(25020), Decimal(25000)


def monthly_snapshot(
    call_vol: float = 0.30, put_vol: float = 0.33, *, depth: int = 1300
) -> Snapshot:
    return snapshot(NOW, SPOT, [QUIET_MONTHLY, NOV3], skew(call_vol, put_vol), depth=depth)


def view_of(snap: Snapshot) -> ChainView:
    view = chain_view(
        snap.quotes,
        expiry=QUIET_MONTHLY,
        spot=SPOT,
        now=NOW,
        tick=TICK,
        config=CFG.chain,
        settle=SETTLE,
    )
    assert view is not None
    return view


def build(  # noqa: PLR0913 - the test's knobs
    view: ChainView | None,
    *,
    or_high: Decimal = OR_HIGH,
    or_low: Decimal = OR_LOW,
    book: SleeveBook | None = None,
    config: CondorConfig = M,
    slot_free: bool = True,
    paused: bool = False,
    lot_size: int | None = LOT,
) -> Candidate:
    return condor.build(
        view,
        or_high=or_high,
        or_low=or_low,
        lot_size=lot_size,
        book=book or SleeveBook(),
        config=config,
        options=CFG,
        ceilings=CEIL,
        slot_free=slot_free,
        paused=paused,
        expiry=QUIET_MONTHLY,
    )


def expected_short(view: ChainView, kind: OptionType, bound: Decimal) -> Decimal:
    """Condor §4.1 read literally over the priced chain: |delta| in band AND beyond the range,
    nearest the target."""
    eligible = [
        q
        for q in view.of_type(kind)
        if q.delta is not None
        and M.delta_min <= abs(Decimal(repr(q.delta))) <= M.delta_max
        and (q.strike > bound if kind is OptionType.CE else q.strike < bound)
    ]
    best = min(eligible, key=lambda q: abs(abs(Decimal(repr(q.delta or 0))) - M.delta_target))
    return best.strike


class TestTheStructure:
    def test_the_quiet_monthly_condor_to_the_rupee(self) -> None:
        view = view_of(monthly_snapshot())
        c = build(view)
        assert c.viable and c.structure is Structure.IRON_CONDOR
        roles = {lg.role: lg for lg in c.legs}
        sc, sp = roles[LegRole.SHORT_CALL], roles[LegRole.SHORT_PUT]
        lc, lp = roles[LegRole.LONG_CALL], roles[LegRole.LONG_PUT]
        # Delta ∧ range, nearest 0.22 — recomputed from the chain.
        assert sc.strike == expected_short(view, OptionType.CE, OR_HIGH) == Decimal(25150)
        assert sp.strike == expected_short(view, OptionType.PE, OR_LOW) == Decimal(24850)
        # Wings 150 further out (condor §4.3).
        assert (lc.strike, lp.strike) == (Decimal(25300), Decimal(24700))
        # Credit: shorts at bid, wings at ask (§4.4).
        assert c.points == (sc.bid or 0) + (sp.bid or 0) - (lc.ask or 0) - (lp.ask or 0)
        assert c.points == Decimal("39.55")  # 26.80 + 24.00 - 5.75 - 5.50
        assert c.points >= M.credit_floor_frac * M.wing_width_points
        # Limits: a wing at ask + tick, a short at bid - tick (§4.5).
        assert lc.limit_price == (lc.ask or 0) + TICK and sc.limit_price == (sc.bid or 0) - TICK
        # Paper, capital ₹0: one lot; R = one lot's risk (PACK.6, 04 §7.3).
        assert (c.lots, c.sizing_mode, c.lot_size) == (1, SizingMode.PAPER_ONE_LOT, LOT)
        assert c.risk_per_lot_inr == (150 - Decimal("39.55")) * LOT + 1000 == Decimal("8179.25")
        assert c.r_inr == c.risk_per_lot_inr
        assert c.max_loss_inr == (150 - Decimal("39.55")) * LOT == Decimal("7179.25")

    def test_the_round_trip_is_eight_orders_at_the_plans_prices(self) -> None:
        c = build(view_of(monthly_snapshot()))
        fills = []
        for lg in c.legs:
            bid, ask = lg.bid or Decimal(0), lg.ask or Decimal(0)
            entry_side = Side.BUY if lg.role.is_long else Side.SELL
            exit_side = Side.SELL if lg.role.is_long else Side.BUY
            entry = ask + TICK if entry_side is Side.BUY else bid - TICK
            close = bid - TICK if exit_side is Side.SELL else ask + TICK
            fills += [CostFill(entry_side, entry, LOT), CostFill(exit_side, close, LOT)]
        expected = charges(fills, CFG.costs)
        assert expected.orders == 8
        assert c.round_trip_inr == expected.total == Decimal("198.32")
        gain = (1 - M.profit_take_frac) * Decimal("39.55") * LOT
        assert c.expected_gain_inr == gain
        assert c.cost_share == Decimal("198.32") / gain
        assert c.cost_share is not None and c.cost_share <= M.cost_share_max
        assert c.warnings == ()  # 198.32 per lot is inside the ₹1,000 reserve

    def test_delta_and_range_neither_relaxed_for_the_call(self) -> None:
        view = view_of(monthly_snapshot())
        # Range pushed past the in-band strike: 25,200 is beyond 25,150 but out of the band.
        c = build(view, or_high=Decimal(25150))
        assert c.rejection is Rejection.REJECTED_NO_SHORT_CALL
        # A range that allows everything, a band nothing meets.
        tight = replace(M, delta_min=Decimal("0.245"), delta_max=Decimal("0.246"))
        assert build(view, config=tight).rejection is Rejection.REJECTED_NO_SHORT_CALL

    def test_delta_and_range_for_the_put(self) -> None:
        c = build(view_of(monthly_snapshot()), or_low=Decimal(24850))
        assert c.rejection is Rejection.REJECTED_NO_SHORT_PUT

    def test_a_missing_wing_rejects(self) -> None:
        snap = monthly_snapshot()
        gone = Snapshot(
            ts=snap.ts,
            spot=snap.spot,
            quotes=tuple(
                q
                for q in snap.quotes
                if not (q.strike == Decimal(25300) and q.option_type is OptionType.CE)
            ),
        )
        assert build(view_of(gone)).rejection is Rejection.REJECTED_NO_WING

    def test_a_credit_below_the_floor_rejects(self) -> None:
        # The same 39.55 against a 30 % floor (45 points).
        c = build(view_of(monthly_snapshot()), config=replace(M, credit_floor_frac=Decimal("0.30")))
        assert c.rejection is Rejection.REJECTED_CREDIT
        assert c.points == Decimal("39.55")

    def test_the_budget_sizes_with_the_first_live_half(self) -> None:
        view = view_of(monthly_snapshot())
        # ₹10 lakh at 1 %: ₹10,000 * 0.5 (first live) = ₹5,000 < ₹8,179.25 → 0 lots.
        small = build(view, book=SleeveBook(sleeve_capital_inr=Decimal(1_000_000)))
        assert small.rejection is Rejection.REJECTED_BUDGET
        # ₹50 lakh at 1 % = ₹50,000, capped at ₹25,000, halved = ₹12,500 → 1 lot, tagged half.
        big = build(view, book=SleeveBook(sleeve_capital_inr=Decimal(5_000_000)))
        assert (big.lots, big.sizing_mode, big.half_size) == (1, SizingMode.BUDGET, True)
        assert big.r_inr == Decimal(12500)

    def test_live_with_no_capital_is_refused(self) -> None:
        c = build(view_of(monthly_snapshot()), book=SleeveBook(mode=Mode.LIVE))
        assert c.rejection is Rejection.REJECTED_NO_SLEEVE_CAPITAL

    def test_liquidity_is_checked_at_the_sized_quantity(self) -> None:
        # 50 units a level * 3 levels = 150: one lot (65) passes selection, three lots (195) do not.
        thin = view_of(monthly_snapshot(depth=50))
        assert build(thin).viable
        # ₹50 lakh, past the first five real trades: ₹25,000 (the ceiling) / ₹8,179.25 → 3 lots.
        sized = SleeveBook(sleeve_capital_inr=Decimal(5_000_000), real_journal_rows=5)
        assert build(view_of(monthly_snapshot()), book=sized).lots == 3
        assert build(thin, book=sized).rejection is Rejection.REJECTED_ILLIQUID

    def test_refusals_before_pricing(self) -> None:
        view = view_of(monthly_snapshot())
        assert build(view, paused=True).rejection is Rejection.REJECTED_PAUSED
        assert build(view, slot_free=False).rejection is Rejection.REJECTED_SLOT_TAKEN
        assert build(None).rejection is Rejection.REJECTED_NO_CHAIN
        assert build(view, lot_size=None).rejection is Rejection.REJECTED_NO_LOT_SIZE


class TestNeverNaked:
    """Track C §2 through the candidate's own legs and the OP1 harness."""

    def test_the_candidate_has_both_wings_and_the_sequences_protect(self) -> None:
        c = build(view_of(monthly_snapshot()))
        roles = tuple(lg.role for lg in c.legs)
        assert entry_sequence(roles) == (
            LegRole.LONG_PUT, LegRole.LONG_CALL, LegRole.SHORT_PUT, LegRole.SHORT_CALL,
        )  # fmt: skip
        assert exit_sequence(roles) == (
            LegRole.SHORT_CALL, LegRole.SHORT_PUT, LegRole.LONG_CALL, LegRole.LONG_PUT,
        )  # fmt: skip

    @settings(max_examples=300, deadline=None, derandomize=True)
    @given(fills=st.lists(st.integers(min_value=0, max_value=LOT), max_size=12))
    def test_entry(self, fills: list[int]) -> None:
        c = build(view_of(monthly_snapshot()))
        _walk_entry(tuple(lg.role for lg in c.legs), LOT, fills)

    @settings(max_examples=300, deadline=None, derandomize=True)
    @given(fills=st.lists(st.integers(min_value=1, max_value=LOT), max_size=40))
    def test_exit(self, fills: list[int]) -> None:
        c = build(view_of(monthly_snapshot()))
        _walk_exit(tuple(lg.role for lg in c.legs), LOT, fills)


# --- exits (condor 04 §7) ------------------------------------------------------------------------


def decide(  # noqa: PLR0913 - the test's knobs
    *,
    d: str,
    c: str = "40",
    spot: str = "25010",
    clock: tuple[int, int] = (11, 0),
    stale: bool = False,
    loss: str = "0",
    manual: bool = False,
) -> CondorExit | None:
    return condor.exit_decision(
        entry_credit=Decimal(c),
        cost_now=Decimal(d),
        spot=Decimal(spot),
        short_call_strike=Decimal(25150),
        short_put_strike=Decimal(24850),
        now=at(QUIET_MONTHLY, *clock),
        stale=stale,
        marked_loss_inr=Decimal(loss),
        risk_budget_inr=Decimal(8000),
        config=M,
        options=CFG,
        manual=manual,
    )


class TestExits:
    @pytest.mark.parametrize(
        ("d", "expected"),
        [("20", CondorExit.PROFIT), ("20.05", None), ("59.95", None), ("60", CondorExit.STOP)],
    )
    def test_profit_at_half_c_and_stop_at_one_and_a_half_c(
        self, d: str, expected: CondorExit | None
    ) -> None:
        assert decide(d=d) is expected

    def test_a_strike_touch_on_either_side(self) -> None:
        assert decide(d="30", spot="25150") is CondorExit.STRIKE_TOUCH
        assert decide(d="30", spot="24850") is CondorExit.STRIKE_TOUCH

    def test_hard_exit_at_1430_outranks_everything(self) -> None:
        assert decide(d="70", spot="25200", clock=(14, 30)) is CondorExit.HARD_EXIT
        assert decide(d="30", clock=(14, 29)) is None

    def test_precedence_touch_over_stop_over_profit(self) -> None:
        assert decide(d="70", spot="25200") is CondorExit.STRIKE_TOUCH

    def test_profit_is_never_taken_on_a_stale_mark_but_stops_are(self) -> None:
        assert decide(d="10", stale=True) is None
        assert decide(d="70", stale=True) is CondorExit.STOP

    def test_a_budget_breach_stops_whatever_d_over_c_says(self) -> None:
        # "Beyond" risk_budget * budget_breach_frac [1.0]: ₹8,000 exactly is not yet a breach.
        assert decide(d="30", loss="8000.01") is CondorExit.STOP
        assert decide(d="30", loss="8000") is None

    def test_manual_is_the_button_and_loses_to_automatic_rules(self) -> None:
        assert decide(d="30", manual=True) is CondorExit.MANUAL
        assert decide(d="10", manual=True) is CondorExit.PROFIT

    def test_the_cost_to_close_reads_shorts_at_ask_and_wings_at_bid(self) -> None:
        c = build(view_of(monthly_snapshot()))
        roles = {lg.role: lg for lg in c.legs}
        expected = (
            (roles[LegRole.SHORT_CALL].ask or 0)
            + (roles[LegRole.SHORT_PUT].ask or 0)
            - (roles[LegRole.LONG_CALL].bid or 0)
            - (roles[LegRole.LONG_PUT].bid or 0)
        )
        assert condor.cost_to_close(c.legs) == expected
