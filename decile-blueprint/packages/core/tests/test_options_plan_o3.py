"""OP8 — the O3 plan builder (``06`` OP8): a reasoned no-trade or one two-leg debit spread.

Every expectation is recomputed from the fixture's quotes and ``04`` §5's text: the long is
``atm(spot)`` at the decision minute, the short ``width_points`` [100] further in the move's
direction, ``debit = long.ask - short.bid``. The literals are that arithmetic written out, so a
changed rule — not a changed implementation — is what turns a test red.

``06`` OP8's acceptance criteria, in order: fixtures for each setup and each direction plan the
expected strikes and debit (:class:`TestEachSetupAndDirection`); a debit above 55 % of the width is
``REJECTED_DEBIT`` (:class:`TestTheDebitCap`); O3-A and O3-B both firing → O3-B holds
(:class:`TestOneO3ADay`); a confirmed O1 → O3 ``SLOT_TAKEN`` (:class:`TestTheExpiryDaySlot`).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal

import pytest
from options_fixtures import LOT
from options_scan_fixtures import (
    GAP_HOLD,
    MASTER,
    O2_UP_BREAK,
    TREND_WEEKLY,
    at,
    bars_from_closes,
    carry_forward,
    flat,
    gap_hold_bars,
    market,
    ramp,
    snapshot,
    to_minutes,
    trend_weekly_bars,
    zigzag,
)

from baskfy_core.options.bars import Bar, closed
from baskfy_core.options.config import (
    Mode,
    OptionsCeilings,
    OptionsConfig,
    OptionType,
    Side,
    Sleeve,
)
from baskfy_core.options.execution import LegRole
from baskfy_core.options.plan import STALE_CHAIN, MarginQuote, PlanState, Verdict
from baskfy_core.options.plan_o3 import (
    BEFORE_PLAN_TIME,
    ENTRY_WINDOW_CLOSED,
    NO_DECISION_SNAPSHOT,
    NO_TRIGGER,
    NO_TRIGGER_YET,
    O3B_HOLDS,
    SETUP_DISABLED,
    WINDOW_NOT_SETTLED,
    O3Decision,
    O3Outcome,
    decide_o3,
    finalize_o3,
    wanted_minutes,
)
from baskfy_core.options.scan import (
    DayContext,
    MarketDay,
    ScanState,
    SleeveContext,
    Snapshot,
    SnapshotBook,
    scan_all,
)
from baskfy_core.options.session import SessionState
from baskfy_core.options.structures import Direction, Rejection, Structure

CFG = OptionsConfig()
CEIL = OptionsCeilings()
SMILE = flat(0.14)
WIDTH = Decimal("100")


def chain(
    bars: tuple[Bar, ...],
    minute: dt.datetime,
    *,
    forward_shift: Decimal = Decimal(0),
) -> Snapshot:
    """What the collector stores at ``minute``: the two nearest expiries around the last close,
    with a cost-of-carry forward (the scan's and the O1/O2 builders' fixture). ``forward_shift``
    moves every forward off the spot — a chain whose ATM-by-spot call is in the money."""
    spot = closed(bars, minute)[-1].close
    expiries = sorted({c.expiry for c in MASTER if c.expiry >= minute.date()})[:2]
    forwards = {e: carry_forward(spot, minute, e) + forward_shift for e in expiries}
    return snapshot(minute, spot, expiries, SMILE, forwards=forwards)


def plus20(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return at(day, hh, mm) + dt.timedelta(seconds=20)


def decide(  # noqa: PLR0913 - the test's knobs
    sleeve: Sleeve,
    m: MarketDay,
    now: dt.datetime,
    *,
    context: DayContext | None = None,
    options: OptionsConfig = CFG,
    forward_shift: Decimal = Decimal(0),
    lag_minutes: int = 0,
) -> O3Decision:
    """The worker's two phases: learn the minute wanted, load it from the fixture, decide."""
    ctx = context or DayContext()
    loaded: dict[dt.datetime | None, Snapshot | None] = {}
    for minute in wanted_minutes(sleeve, m, ctx, now, options, CEIL):
        if minute is not None:
            stored = minute + dt.timedelta(minutes=lag_minutes)
            snap = chain(m.bars, stored, forward_shift=forward_shift)
            loaded[minute] = replace(snap, ts=stored)
    return decide_o3(sleeve, m, ctx, SnapshotBook(loaded), now, options=options, ceilings=CEIL)


def finalize(
    decision: O3Decision,
    margin: MarginQuote | None = None,
    *,
    mode: Mode = Mode.PAPER,
    pool: Decimal = Decimal(0),
    in_use: Decimal = Decimal(0),
) -> O3Outcome:
    return finalize_o3(
        decision,
        margin,
        user_id=7,
        mode=mode,
        margin_pool_inr=pool,
        margin_in_use_inr=in_use,
        options=CFG,
    )


def gap_down_bars() -> tuple[Bar, ...]:
    """GAP_HOLD's mirror: open 24,800 (-0.8 %), half-gap 24,900, a ±10 zig-zag that never nears
    it."""
    closes = zigzag(Decimal("24800"), Decimal("10"), to_minutes(15, 30))
    return bars_from_closes(GAP_HOLD, Decimal("24800"), closes)


def trend_down_bars() -> tuple[Bar, ...]:
    """TREND_WEEKLY's mirror: -3 a minute to 10:14, then -6 — the 10:15-10:19 bar closes below
    the range's low x 0.9995."""
    first = ramp(Decimal("25000"), Decimal("-3"), to_minutes(10, 15))
    rest = ramp(first[-1], Decimal("-6"), to_minutes(15, 30) - len(first))
    return bars_from_closes(TREND_WEEKLY, Decimal("25000"), first + rest)


def gap_then_break_bars() -> tuple[Bar, ...]:
    """A day both setups fire on: open 25,200 (+0.8 %) and drift +0.5 a minute (the gap holds;
    the 60-bar range stays under 1.2 %), then +6 a minute from 10:15 — a clean range break."""
    first = ramp(Decimal("25200"), Decimal("0.5"), to_minutes(10, 15))
    rest = ramp(first[-1], Decimal("6"), to_minutes(15, 30) - len(first))
    return bars_from_closes(GAP_HOLD, Decimal("25200"), first + rest)


GAP_UP = market(GAP_HOLD, gap_hold_bars())
GAP_DOWN = market(GAP_HOLD, gap_down_bars())
TREND_UP = market(TREND_WEEKLY, trend_weekly_bars())
TREND_DOWN = market(TREND_WEEKLY, trend_down_bars())
BOTH = market(GAP_HOLD, gap_then_break_bars())
O3B_NOW = plus20(GAP_HOLD, 9, 46)
O3A_NOW = plus20(TREND_WEEKLY, 10, 21)


def planned(outcome: O3Outcome) -> O3Outcome:
    assert outcome.state is PlanState.PLANNED and outcome.plan is not None, outcome.reasons
    return outcome


# --- each setup and each direction ---------------------------------------------------------------


class TestEachSetupAndDirection:
    """``04`` §5: long ``atm(spot)``, short 100 further in the move's direction, CE up / PE down,
    ``debit = long.ask - short.bid``, on the contract expiring today."""

    @pytest.mark.parametrize(
        ("sleeve", "m", "now", "direction", "decision_minute"),
        [
            (Sleeve.O3B, GAP_UP, O3B_NOW, Direction.UP, at(GAP_HOLD, 9, 45)),
            (Sleeve.O3B, GAP_DOWN, O3B_NOW, Direction.DOWN, at(GAP_HOLD, 9, 45)),
            (Sleeve.O3A, TREND_UP, O3A_NOW, Direction.UP, at(TREND_WEEKLY, 10, 20)),
            (Sleeve.O3A, TREND_DOWN, O3A_NOW, Direction.DOWN, at(TREND_WEEKLY, 10, 20)),
        ],
    )
    def test_the_strikes_and_the_debit_are_04_5(
        self,
        sleeve: Sleeve,
        m: MarketDay,
        now: dt.datetime,
        direction: Direction,
        decision_minute: dt.datetime,
    ) -> None:
        plan = planned(finalize(decide(sleeve, m, now))).plan
        assert plan is not None
        snap = chain(m.bars, decision_minute)
        assert snap.spot is not None
        atm = (snap.spot / 50).to_integral_value() * 50
        right = OptionType.CE if direction is Direction.UP else OptionType.PE
        k_short = atm + WIDTH if direction is Direction.UP else atm - WIDTH
        long_leg, short_leg = plan.legs
        assert (long_leg.strike, short_leg.strike) == (atm, k_short)
        assert long_leg.option_type is right and short_leg.option_type is right
        assert plan.expiry == m.trade_date  # the expiring contract (04 §1.5)
        assert plan.direction is direction and plan.structure is Structure.DEBIT_SPREAD
        assert long_leg.ask is not None and short_leg.bid is not None
        assert plan.debit_points == long_leg.ask - short_leg.bid
        assert plan.width_points == WIDTH
        assert plan.as_of_minute == decision_minute

    def test_o3b_up_to_the_rupee(self) -> None:
        """GAP_HOLD at 09:45: spot 25,190 → ATM 25,200; the 25,200/25,300 CE spread."""
        plan = planned(finalize(decide(Sleeve.O3B, GAP_UP, O3B_NOW))).plan
        assert plan is not None
        long_leg, short_leg = plan.legs
        assert (long_leg.tradingsymbol, short_leg.tradingsymbol) == (
            "NIFTY26101325200CE",
            "NIFTY26101325300CE",
        )
        assert (long_leg.bid, long_leg.ask, short_leg.bid, short_leg.ask) == (
            Decimal("31.60"),
            Decimal("31.95"),
            Decimal("5.00"),
            Decimal("5.10"),
        )
        assert plan.debit_points == Decimal("26.95")  # 31.95 - 5.00
        assert plan.lots == 1 and plan.quantity == LOT  # capital ₹0: paper one lot (04 §7.3)
        assert plan.debit_inr == plan.max_loss_inr == Decimal("1751.75")  # 26.95 x 65
        assert plan.risk_per_lot_inr == Decimal("2251.75")  # + the ₹500 reserve (04 §5.5)
        assert plan.profit_target_inr == Decimal("3448.25")  # (0.80 x 100 - 26.95) x 65
        assert plan.stop_inr == Decimal("875.88")  # 0.50 x 26.95 x 65, to the paisa

    def test_o3a_up_to_the_rupee(self) -> None:
        """TREND_WEEKLY: the 10:15-10:19 bar breaks 25,182 x 1.0005; priced from the 10:20
        chain — the 25,200/25,300 CE spread."""
        decision = decide(Sleeve.O3A, TREND_UP, O3A_NOW)
        plan = planned(finalize(decision)).plan
        assert plan is not None
        assert decision.numbers["trigger_time"] == "10:19"
        long_leg, short_leg = plan.legs
        assert (long_leg.strike, short_leg.strike) == (Decimal("25200"), Decimal("25300"))
        assert plan.debit_points == Decimal("33.55")  # 40.15 - 6.60
        assert plan.exits.invalidation_level == Decimal("25182")  # the range's high

    def test_the_plan_is_the_scans_candidate(self) -> None:
        """``04`` §10: the scan's candidate is the plan builder's computation."""
        for sleeve, m, now, minute in (
            (Sleeve.O3B, GAP_UP, O3B_NOW, at(GAP_HOLD, 9, 45)),
            (Sleeve.O3A, TREND_UP, O3A_NOW, at(TREND_WEEKLY, 10, 20)),
        ):
            decision = decide(sleeve, m, now)
            book = SnapshotBook({minute: chain(m.bars, minute), None: chain(m.bars, minute)})
            row = next(
                r for r in scan_all(m, DayContext(), now, book, CFG, CEIL) if r.sleeve is sleeve
            )
            assert row.state is ScanState.TRIGGERED
            assert row.candidates == (decision.candidate,)


# --- the debit cap -------------------------------------------------------------------------------


class TestTheDebitCap:
    def test_a_debit_above_55_percent_of_the_width_is_rejected(self) -> None:
        """A chain whose forward sits 80 points above the spot makes the ATM-by-spot call deep in
        the money: the spread costs more than 0.55 x 100 = 55."""
        decision = decide(Sleeve.O3B, GAP_UP, O3B_NOW, forward_shift=Decimal("80"))
        assert decision.state is PlanState.SKIPPED
        assert decision.reasons == (Rejection.REJECTED_DEBIT.value,)
        candidate = decision.candidate
        assert candidate is not None and candidate.points is not None
        assert candidate.points > Decimal("55")
        assert finalize(decision).plan is None

    def test_the_cap_is_the_config_fraction(self) -> None:
        tight = replace(
            CFG, expiry_setups=replace(CFG.expiry_setups, max_debit_frac=Decimal("0.20"))
        )
        decision = decide(Sleeve.O3B, GAP_UP, O3B_NOW, options=tight)
        assert decision.reasons == (Rejection.REJECTED_DEBIT.value,)  # 26.95 > 20


# --- one O3 a day --------------------------------------------------------------------------------


class TestOneO3ADay:
    def test_both_setups_fire_on_this_day(self) -> None:
        """The fixture really does fire both: O3-B plans at 09:45, and O3-A's range breaks."""
        assert decide(Sleeve.O3B, BOTH, O3B_NOW).state is PlanState.PLANNED
        free = decide(Sleeve.O3A, BOTH, plus20(GAP_HOLD, 10, 21))
        assert free.state is PlanState.PLANNED

    @pytest.mark.parametrize(
        "o3b_state",
        [SessionState.PLANNED, SessionState.CONFIRMED, SessionState.OPEN, SessionState.CLOSED],
    )
    def test_o3b_holds_the_day_so_o3a_is_slot_taken(self, o3b_state: SessionState) -> None:
        ctx = DayContext(sleeves={Sleeve.O3B: SleeveContext(session_state=o3b_state)})
        decision = decide(Sleeve.O3A, BOTH, plus20(GAP_HOLD, 10, 21), context=ctx)
        assert decision.state is PlanState.SKIPPED
        assert decision.reasons == (Rejection.REJECTED_SLOT_TAKEN.value, O3B_HOLDS)

    @pytest.mark.parametrize("o3b_state", [SessionState.LAPSED, SessionState.SKIPPED])
    def test_a_lapsed_or_skipped_o3b_frees_o3a(self, o3b_state: SessionState) -> None:
        ctx = DayContext(sleeves={Sleeve.O3B: SleeveContext(session_state=o3b_state)})
        assert decide(Sleeve.O3A, BOTH, plus20(GAP_HOLD, 10, 21), context=ctx).state is (
            PlanState.PLANNED
        )

    def test_o3a_never_holds_o3b(self) -> None:
        """O3-B decides first (its window closes before O3-A's opens); the rule is one-way."""
        ctx = DayContext(sleeves={Sleeve.O3A: SleeveContext(session_state=SessionState.PLANNED)})
        assert decide(Sleeve.O3B, GAP_UP, O3B_NOW, context=ctx).state is PlanState.PLANNED


# --- the expiry-day slot -------------------------------------------------------------------------


class TestTheExpiryDaySlot:
    @pytest.mark.parametrize("holder", [Sleeve.O1W, Sleeve.O1M])
    @pytest.mark.parametrize(
        ("sleeve", "m", "now"),
        [(Sleeve.O3B, GAP_UP, O3B_NOW), (Sleeve.O3A, TREND_UP, O3A_NOW)],
    )
    def test_a_confirmed_o1_makes_o3_slot_taken(
        self, holder: Sleeve, sleeve: Sleeve, m: MarketDay, now: dt.datetime
    ) -> None:
        decision = decide(sleeve, m, now, context=DayContext(slot_holder=holder))
        assert decision.state is PlanState.SKIPPED
        assert decision.reasons == (Rejection.REJECTED_SLOT_TAKEN.value,)
        assert finalize(decision).plan is None

    def test_holding_the_slot_itself_is_not_a_refusal(self) -> None:
        ctx = DayContext(slot_holder=Sleeve.O3B)
        assert decide(Sleeve.O3B, GAP_UP, O3B_NOW, context=ctx).state is PlanState.PLANNED

    def test_an_o1_plan_that_is_only_planned_does_not_hold_the_slot(self) -> None:
        """``04`` §8.6: the first plan *confirmed* holds it; the context's holder is that."""
        ctx = DayContext(sleeves={Sleeve.O1W: SleeveContext(session_state=SessionState.PLANNED)})
        assert decide(Sleeve.O3B, GAP_UP, O3B_NOW, context=ctx).state is PlanState.PLANNED


# --- the plan's shape ----------------------------------------------------------------------------


class TestThePlan:
    def test_long_first_then_short_never_naked(self) -> None:
        """``04`` §5.4: the short is sent only after the long is filled."""
        for sleeve, m, now in (
            (Sleeve.O3B, GAP_DOWN, O3B_NOW),
            (Sleeve.O3A, TREND_UP, O3A_NOW),
        ):
            plan = planned(finalize(decide(sleeve, m, now))).plan
            assert plan is not None
            assert [(lg.seq, lg.side) for lg in plan.legs] == [(1, Side.BUY), (2, Side.SELL)]
            assert plan.legs[0].role in (LegRole.LONG_CALL, LegRole.LONG_PUT)
            assert all(lg.quantity == plan.quantity for lg in plan.legs)

    def test_o3b_lapses_at_its_window_end(self) -> None:
        """Issued 09:46:20: ``min(+30 min, 10:00)`` = 10:00 (``04`` §5.2)."""
        plan = planned(finalize(decide(Sleeve.O3B, GAP_UP, O3B_NOW))).plan
        assert plan is not None
        assert plan.expires_at == at(GAP_HOLD, 10, 0)
        assert plan.entry_window_end == at(GAP_HOLD, 10, 0)

    def test_o3a_gets_thirty_minutes(self) -> None:
        """Issued 10:21:20: ``min(+30 min, 13:30)`` = 10:51:20 (OP8.1)."""
        plan = planned(finalize(decide(Sleeve.O3A, TREND_UP, O3A_NOW))).plan
        assert plan is not None
        assert plan.expires_at == O3A_NOW + dt.timedelta(minutes=30)
        assert plan.entry_window_end == at(TREND_WEEKLY, 13, 30)

    def test_the_exits_are_04_5_3(self) -> None:
        up = planned(finalize(decide(Sleeve.O3B, GAP_UP, O3B_NOW))).plan
        down = planned(finalize(decide(Sleeve.O3A, TREND_DOWN, O3A_NOW))).plan
        assert up is not None and down is not None
        assert up.exits.target_value == Decimal("80.00")  # 0.80 x 100
        assert up.exits.stop_value == Decimal("13.48")  # 0.50 x 26.95, to the paisa
        assert up.exits.invalidation_level == Decimal("25100")  # 25,000 + 200 / 2
        assert up.exits.hard_exit_at == at(GAP_HOLD, 14, 45)
        # O3-A down: invalidated by a 5-minute close back above the range's low.
        low = down.numbers["range_low"]
        assert isinstance(low, str) and down.exits.invalidation_level == Decimal(low)

    def test_the_plan_id_is_deterministic_and_per_setup(self) -> None:
        a = planned(finalize(decide(Sleeve.O3B, GAP_UP, O3B_NOW))).plan
        b = planned(finalize(decide(Sleeve.O3B, GAP_UP, O3B_NOW))).plan
        assert a is not None and b is not None and a.plan_id == b.plan_id
        assert a.plan_id.startswith("O3B-20261013-") and ":" not in a.plan_id

    def test_costs_are_itemised_over_four_orders(self) -> None:
        plan = planned(finalize(decide(Sleeve.O3B, GAP_UP, O3B_NOW))).plan
        assert plan is not None
        assert plan.costs.brokerage == Decimal("80.00")  # ₹20 x 4 orders (04 §6.1)
        assert plan.expected_cost_inr == plan.costs.total
        assert plan.cost_share < CFG.expiry_setups.cost_share_max


# --- margin (04 §7.4) ----------------------------------------------------------------------------


class TestMargin:
    def test_the_calculator_is_asked_about_the_hedged_basket_only(self) -> None:
        decision = decide(Sleeve.O3B, GAP_UP, O3B_NOW)
        hedged, transient = decision.margin_baskets()
        assert [(lg.tradingsymbol, lg.side) for lg in hedged] == [
            ("NIFTY26101325200CE", Side.BUY),
            ("NIFTY26101325300CE", Side.SELL),
        ]
        assert transient == ()  # the long alone costs its premium (OP8.4)

    def test_no_answer_is_a_warning_on_paper_and_a_rejection_live(self) -> None:
        decision = decide(Sleeve.O3B, GAP_UP, O3B_NOW)
        paper = planned(finalize(decision))
        assert paper.plan is not None and "MARGIN_UNKNOWN" in paper.plan.warnings
        live = finalize(decision, mode=Mode.LIVE)
        assert live.state is PlanState.SKIPPED and live.reasons == ("REJECTED_MARGIN",)

    def test_a_basket_above_the_free_pool_is_rejected_live(self) -> None:
        decision = decide(Sleeve.O3B, GAP_UP, O3B_NOW)
        quote = MarginQuote(hedged_inr=Decimal("30000"), transient_inr=None)
        live = finalize(
            decision, quote, mode=Mode.LIVE, pool=Decimal("50000"), in_use=Decimal("25000")
        )
        assert live.state is PlanState.SKIPPED and live.reasons == ("REJECTED_MARGIN",)
        fits = finalize(decision, quote, mode=Mode.LIVE, pool=Decimal("60000"))
        assert fits.plan is not None and fits.plan.margin_required_inr == Decimal("30000.00")


# --- the clock and the day -----------------------------------------------------------------------


class TestTheClockAndTheDay:
    def test_o3b_before_its_plan_time_is_not_ready(self) -> None:
        d = decide(Sleeve.O3B, GAP_UP, plus20(GAP_HOLD, 9, 40))
        assert (d.state, d.reasons) == (PlanState.NOT_READY, (BEFORE_PLAN_TIME,))
        assert not d.writes_session

    def test_o3a_before_its_first_trigger_bar_is_not_ready(self) -> None:
        d = decide(Sleeve.O3A, TREND_UP, plus20(TREND_WEEKLY, 10, 10))
        assert (d.state, d.reasons) == (PlanState.NOT_READY, (BEFORE_PLAN_TIME,))

    def test_o3a_armed_is_not_ready_and_a_quiet_day_is_skipped(self) -> None:
        """GAP_HOLD's zig-zag never breaks its range: armed until the 13:00 bar has closed and
        ``stale_scan_seconds`` [120] have passed (OP4.3), then decided."""
        armed = decide(Sleeve.O3A, GAP_UP, plus20(GAP_HOLD, 11, 0))
        assert (armed.state, armed.reasons) == (PlanState.NOT_READY, (NO_TRIGGER_YET,))
        waiting = decide(Sleeve.O3A, GAP_UP, plus20(GAP_HOLD, 13, 2))
        assert waiting.reasons == (NO_TRIGGER_YET,)
        done = decide(Sleeve.O3A, GAP_UP, plus20(GAP_HOLD, 13, 3))
        assert (done.state, done.reasons) == (PlanState.SKIPPED, (NO_TRIGGER,))
        assert done.verdict is Verdict.SKIP and done.writes_session

    def test_o3b_on_a_day_with_no_gap_is_skipped_with_the_reason(self) -> None:
        d = decide(Sleeve.O3B, TREND_UP, plus20(TREND_WEEKLY, 9, 46))
        assert d.state is PlanState.SKIPPED and d.reasons == ("GAP_TOO_SMALL",)

    def test_a_decision_first_asked_after_the_window_writes_nothing(self) -> None:
        d = decide(Sleeve.O3B, GAP_UP, plus20(GAP_HOLD, 10, 1))
        assert (d.state, d.reasons) == (PlanState.WINDOW_CLOSED, (ENTRY_WINDOW_CLOSED,))
        assert not d.writes_session

    def test_no_decision_snapshot_waits(self) -> None:
        d = decide_o3(
            Sleeve.O3B, GAP_UP, DayContext(), SnapshotBook(), O3B_NOW, options=CFG, ceilings=CEIL
        )
        assert (d.state, d.reasons) == (PlanState.NOT_READY, (NO_DECISION_SNAPSHOT,))

    def test_a_stale_decision_chain_is_skipped(self) -> None:
        """The 09:45 chain asked for at 09:50:20 is five minutes old (``04`` §2.5)."""
        d = decide(Sleeve.O3B, GAP_UP, plus20(GAP_HOLD, 9, 50))
        assert d.state is PlanState.SKIPPED and d.reasons == (STALE_CHAIN,)

    def test_the_hold_not_yet_settled_waits(self) -> None:
        cfg = replace(CFG, expiry_setups=replace(CFG.expiry_setups, o3b_plan_time=dt.time(9, 30)))
        d = decide(Sleeve.O3B, GAP_UP, plus20(GAP_HOLD, 9, 31), options=cfg)
        assert (d.state, d.reasons) == (PlanState.NOT_READY, (WINDOW_NOT_SETTLED,))

    def test_a_non_expiry_day_has_no_session(self) -> None:
        m = market(O2_UP_BREAK, gap_hold_bars())
        d = decide(Sleeve.O3B, m, plus20(O2_UP_BREAK, 9, 46))
        assert d.state is PlanState.NO_SESSION and not d.writes_session

    def test_an_event_expiry_is_skipped_never_shifted(self) -> None:
        ctx = DayContext(event_days=frozenset({GAP_HOLD}))
        d = decide(Sleeve.O3B, GAP_UP, O3B_NOW, context=ctx)
        assert (d.state, d.reasons) == (PlanState.SKIPPED, ("EVENT_DAY",))

    @pytest.mark.parametrize("sleeve", [Sleeve.O3A, Sleeve.O3B])
    def test_a_disabled_setup_has_no_session(self, sleeve: Sleeve) -> None:
        off = replace(
            CFG,
            expiry_setups=replace(CFG.expiry_setups, o3a_enabled=False, o3b_enabled=False),
        )
        d = decide(sleeve, GAP_UP, O3B_NOW, options=off)
        assert (d.state, d.reasons) == (PlanState.NO_SESSION, (SETUP_DISABLED,))

    def test_a_paused_setup_is_skipped_with_the_code(self) -> None:
        ctx = DayContext(sleeves={Sleeve.O3B: SleeveContext(paused=True)})
        d = decide(Sleeve.O3B, GAP_UP, O3B_NOW, context=ctx)
        assert d.reasons == (Rejection.REJECTED_PAUSED.value,)

    def test_only_o3_setups_are_accepted(self) -> None:
        with pytest.raises(ValueError, match="not an O3 setup"):
            decide_o3(
                Sleeve.O2, GAP_UP, DayContext(), SnapshotBook(), O3B_NOW, options=CFG, ceilings=CEIL
            )
