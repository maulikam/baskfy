"""OP7 — the O2 plan builder (``06`` OP7): a reasoned no-trade or one long-option plan.

Every expectation is recomputed from the fixture's quotes and ``04``'s rates by the rule's text;
the literals are that arithmetic written out, so a changed rule — not a changed implementation — is
what turns a test red. The fixture morning is OP4's ``O2_UP_BREAK`` (Mon 19 Oct 2026) stored as the
collector stores it: an up-trend, a 09:15-09:29 range of 25,008-25,032, and the 10:00-10:04 bar
closing 25,050 through 25,044.52 — the trigger. Being the Monday before a Tuesday expiry, the
sleeve buys **Tuesday's** contract (``04`` §1.5).

``06`` OP7's acceptance criteria, in order: the Monday fixture uses Tuesday's contract and the
Tuesday fixture next week's (:class:`TestTheContract`); a counter-trend break plans nothing
(:class:`TestTheTrigger`); ``VIX_TOO_HIGH`` and ``GAP_TOO_BIG`` days plan nothing with the reason
(:class:`TestTheDayFilters`); a 0.45-delta "ITM" strike on a fast day is ``REJECTED_DELTA``
(:class:`TestTheDelta`); and the gap-through worst case is on the plan
(:class:`TestTheFixtureMorning`).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal

import pytest
from options_fixtures import LOT
from options_scan_fixtures import (
    MASTER,
    O2_COUNTER,
    O2_TUESDAY,
    O2_UP_BREAK,
    PREV_CLOSE,
    Smile,
    at,
    bars_from_closes,
    carry_forward,
    falling_closes,
    flat,
    market,
    o2_counter_bars,
    o2_up_break_bars,
    skew,
    snapshot,
    to_minutes,
    trend_weekly_bars,
    zigzag,
)

from baskfy_core.options.bars import IST, Bar, closed
from baskfy_core.options.config import (
    Mode,
    OptionsCeilings,
    OptionsConfig,
    OptionType,
    Side,
    SizingMode,
    Sleeve,
)
from baskfy_core.options.execution import LegRole
from baskfy_core.options.plan import STALE_CHAIN, PlanState, Verdict, plan_id_for
from baskfy_core.options.plan_o2 import (
    BEFORE_ENTRY_WINDOW,
    ENTRY_WINDOW_CLOSED,
    NO_DECISION_SNAPSHOT,
    NO_TRIGGER,
    NO_TRIGGER_YET,
    RANGE_NOT_SETTLED,
    O2Decision,
    O2Outcome,
    decide_o2,
    decision_minute_for,
    finalize_o2,
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
from baskfy_core.options.structures import Direction, SleeveBook, Structure

CFG = OptionsConfig()
CEIL = OptionsCeilings()
SMILE = skew(0.30, 0.33)
DAY = O2_UP_BREAK
BARS = o2_up_break_bars()
UP = market(DAY, BARS)
#: The trigger bar closes at 10:04; the plan prices from the 10:05 chain (OP4.4).
TRIGGER_MINUTE = at(DAY, 10, 5)
NOW = TRIGGER_MINUTE + dt.timedelta(seconds=45)


def chain(bars: tuple[Bar, ...], minute: dt.datetime, smile: Smile = SMILE) -> Snapshot:
    """What the collector stores at ``minute``: the two nearest expiries around the last close,
    with a cost-of-carry forward (as ``test_options_scan`` and ``test_options_plan``)."""
    spot = closed(bars, minute)[-1].close
    expiries = sorted({c.expiry for c in MASTER if c.expiry >= minute.date()})[:2]
    forwards = {e: carry_forward(spot, minute, e) for e in expiries}
    return snapshot(minute, spot, expiries, smile, forwards=forwards)


SNAP = chain(BARS, TRIGGER_MINUTE)


def decide(  # noqa: PLR0913, PLR0917 - the test's knobs
    m: MarketDay = UP,
    snap: Snapshot | None = SNAP,
    minute: dt.datetime | None = TRIGGER_MINUTE,
    now: dt.datetime = NOW,
    context: DayContext | None = None,
    options: OptionsConfig = CFG,
    ceilings: OptionsCeilings = CEIL,
) -> O2Decision:
    book = SnapshotBook({minute: snap} if minute is not None else {})
    return decide_o2(m, context or DayContext(), book, now, options=options, ceilings=ceilings)


def finalize(decision: O2Decision, *, mode: Mode = Mode.PAPER, options: OptionsConfig = CFG) -> (
    O2Outcome
):  # fmt: skip
    return finalize_o2(decision, user_id=7, mode=mode, options=options)


def booked(book: SleeveBook, paused: bool = False) -> DayContext:
    return DayContext(sleeves={Sleeve.O2: SleeveContext(book=book, paused=paused)})


# --- the fixture morning, to the rupee -----------------------------------------------------------


class TestTheFixtureMorning:
    def test_the_plan_to_the_rupee(self) -> None:
        outcome = finalize(decide())
        plan = outcome.plan
        assert outcome.state is PlanState.PLANNED and plan is not None
        # One step ITM on the nearest non-expiring weekly: ATM 25,050 - 1 x 50 (04 §4.3).
        (leg,) = plan.legs
        assert (leg.seq, leg.role, leg.side) == (1, LegRole.LONG_CALL, Side.BUY)
        assert (leg.strike, leg.option_type) == (Decimal(25000), OptionType.CE)
        assert leg.tradingsymbol == "NIFTY26102025000CE"
        assert plan.expiry == dt.date(2026, 10, 20)
        assert plan.structure is Structure.LONG_OPTION and plan.direction is Direction.UP
        # Attempt-1 limit (04 §8.2): a buy at ask + one tick.
        assert (leg.bid, leg.ask, leg.limit_price) == (
            Decimal("201.65"),
            Decimal("203.70"),
            Decimal("203.75"),
        )
        assert (
            leg.delta is not None
            and CFG.directional.delta_min <= Decimal(repr(leg.delta)) <= CFG.directional.delta_max
        )
        assert plan.debit_points == Decimal("203.75")
        assert (plan.lots, plan.lot_size, plan.quantity) == (1, LOT, LOT)
        assert plan.sizing_mode is SizingMode.PAPER_ONE_LOT and plan.mode is Mode.PAPER
        # Risk per lot = E x stop_frac x lot + 300 (04 §4.6); R = one lot's risk in paper.
        assert plan.max_loss_inr == Decimal("3973.13")  # 203.75 x 0.30 x 65 = 3,973.125
        assert plan.risk_per_lot_inr == Decimal("4273.13") == plan.risk_budget_inr
        assert plan.premium_inr == Decimal("203.75") * LOT == Decimal("13243.75")
        # The gap-through worst case: the whole premium, on the plan (04 §4.6, the AC).
        assert plan.gap_through_inr == plan.premium_inr
        assert plan.profit_target_inr == Decimal("7946.25")  # 0.60 x 203.75 x 65
        assert plan.stop_inr == plan.max_loss_inr
        assert plan.expires_at == NOW + dt.timedelta(minutes=30)
        assert plan.entry_window_end == dt.datetime(2026, 10, 19, 13, 30, tzinfo=IST)
        assert plan.as_of_minute == TRIGGER_MINUTE
        assert plan.warnings == ()

    def test_the_costs_to_the_paisa_from_the_verified_rates(self) -> None:
        """``04`` §6.1 by hand over the **two** orders at the plan's prices (OP4.5)."""
        plan = finalize(decide()).plan
        assert plan is not None
        buys = Decimal("203.75") * LOT  # in at ask + tick
        sells = Decimal("201.60") * LOT  # out at bid - tick (04 §8.2's attempt 1)
        assert (buys, sells) == (Decimal("13243.75"), Decimal("13104.00"))
        c = plan.costs
        assert c.orders == 2
        assert c.brokerage == Decimal("40.00")  # 2 x ₹20
        assert c.stt == Decimal("19.66")  # 13,104.00 x 0.15 % = 19.656
        assert c.exchange_txn == Decimal("9.36")  # 26,347.75 x 0.03553 % = 9.3614
        assert c.sebi == Decimal("0.03")  # 26,347.75 x ₹10 / crore = 0.0263
        assert c.ipft == Decimal("0.00")  # 26,347.75 x ₹0.01 / crore
        assert c.stamp == Decimal("0.40")  # 13,243.75 x 0.003 % = 0.3973
        assert c.gst == Decimal("8.89")  # 18 % of (40 + 9.36 + 0.03 + 0.00) = 8.8902
        assert c.total == plan.expected_cost_inr == Decimal("78.34")
        # Cost share = 78.34 / (0.60 x 203.75 x 65) = 0.0099 (04 §6.4) <= 0.15.
        assert plan.cost_share == Decimal("0.0099")
        assert plan.cost_share <= CFG.directional.cost_share_max

    def test_the_exits_are_04_4_5(self) -> None:
        plan = finalize(decide()).plan
        assert plan is not None
        e = plan.exits
        assert e.stop_price == Decimal("142.63")  # 203.75 x (1 - 0.30)
        assert e.target_price == Decimal("326.00")  # 203.75 x (1 + 0.60)
        assert e.time_stop_minutes == 45
        assert e.time_stop_min_price == Decimal("224.13")  # 203.75 x 1.10
        # A call is invalidated by a 5-minute close back below the range's high.
        assert e.invalidation_level == Decimal("25032")
        assert e.hard_exit_at == dt.datetime(2026, 10, 19, 15, 0, tzinfo=IST)

    def test_the_plan_is_the_scans_candidate(self) -> None:
        """``04`` §10: the scan and the plan are one computation over the same snapshot."""
        decision = decide()
        book = SnapshotBook({TRIGGER_MINUTE: SNAP, None: SNAP})
        scanned = {r.sleeve: r for r in scan_all(UP, DayContext(), NOW, book, CFG, CEIL)}
        row = scanned[Sleeve.O2]
        assert row.state is ScanState.TRIGGERED
        (candidate,) = row.candidates
        assert decision.candidate == candidate
        assert decision.costs is not None and decision.costs.total == candidate.round_trip_inr

    def test_the_only_leg_is_long_and_the_book_is_never_naked(self) -> None:
        plan = finalize(decide()).plan
        assert plan is not None
        assert [lg.role for lg in plan.legs] == [LegRole.LONG_CALL]
        assert all(lg.side is Side.BUY for lg in plan.legs)

    def test_plan_id_is_deterministic_and_a_valid_client_id_half(self) -> None:
        a, b = finalize(decide()).plan, finalize(decide()).plan
        assert a is not None and b is not None
        assert a.plan_id == b.plan_id and a.plan_id.startswith("O2-20261019-")
        assert ":" not in a.plan_id and not any(ch.isspace() for ch in a.plan_id)
        assert plan_id_for(8, decide()) != a.plan_id  # another tenant, another id

    def test_expires_at_is_min_of_thirty_minutes_and_the_window(self) -> None:
        narrow = replace(
            CFG, directional=replace(CFG.directional, entry_window_end=dt.time(10, 20))
        )
        plan = finalize(decide(options=narrow), options=narrow).plan
        assert plan is not None
        assert plan.expires_at == dt.datetime(2026, 10, 19, 10, 20, tzinfo=IST)
        assert plan.expires_at == plan.entry_window_end < NOW + dt.timedelta(minutes=30)

    def test_the_builder_asks_for_exactly_the_trigger_minute(self) -> None:
        assert wanted_minutes(UP, DayContext(), NOW, CFG, CEIL) == frozenset({TRIGGER_MINUTE})
        decision = decide()
        assert decision.trigger is not None
        assert decision_minute_for(decision.trigger, DAY) == TRIGGER_MINUTE


# --- the contract (04 §1.5, §4.3) -----------------------------------------------------------------


class TestTheContract:
    def test_the_monday_before_an_expiry_uses_tuesdays_contract(self) -> None:
        decision = decide()
        assert DAY.weekday() == 0 and decision.expiry == dt.date(2026, 10, 20)

    def test_an_expiry_tuesday_uses_next_weeks_contract(self) -> None:
        """O2 never buys the expiring contract — 0-DTE is O3's domain (PACK.5)."""
        bars = trend_weekly_bars()
        m = market(O2_TUESDAY, bars)
        minute = at(O2_TUESDAY, 9, 35)  # the 09:30-09:34 bar closed 25,060, through 25,059.52
        now = minute + dt.timedelta(seconds=45)
        plan = finalize(decide(m=m, snap=chain(bars, minute), minute=minute, now=now)).plan
        assert plan is not None
        assert dt.date(2026, 10, 20) == O2_TUESDAY and plan.expiry == dt.date(2026, 10, 27)
        assert plan.legs[0].tradingsymbol == "NIFTY26102725000CE"
        assert plan.debit_points == Decimal("471.95")

    def test_a_down_trend_buys_a_put_one_step_above_the_money(self) -> None:
        bars = o2_counter_bars()  # the same tape, read against a falling 20-day EMA
        m = market(O2_COUNTER, bars, daily=falling_closes())
        minute = at(O2_COUNTER, 10, 10)  # the 10:05-10:09 bar closed 24,990, under 24,995.50
        decision = decide(
            m=m, snap=chain(bars, minute), minute=minute, now=minute + dt.timedelta(seconds=45)
        )
        plan = finalize(decision).plan
        assert plan is not None and plan.direction is Direction.DOWN
        (leg,) = plan.legs
        assert (leg.role, leg.option_type, leg.strike) == (
            LegRole.LONG_PUT,
            OptionType.PE,
            Decimal(25050),
        )
        # A put is invalidated by a close back above the range's low.
        assert plan.exits.invalidation_level == Decimal("25008")


# --- the day filters (04 §4.1) --------------------------------------------------------------------


class TestTheDayFilters:
    def _skips(self, decision: O2Decision, reason: str) -> None:
        assert decision.state is PlanState.SKIPPED and decision.reasons == (reason,)
        assert decision.verdict is Verdict.SKIP and finalize(decision).plan is None

    def test_a_gap_beyond_gap_max_plans_nothing_with_the_reason(self) -> None:
        opened = Decimal("25300")  # 1.20 % above the 25,000 previous close > 1.0
        bars = bars_from_closes(DAY, opened, zigzag(opened, Decimal("10"), to_minutes(15, 30)))
        decision = decide(m=market(DAY, bars), minute=None)
        self._skips(decision, "GAP_TOO_BIG")
        assert decision.numbers["gap_pct"] == "1.2000"

    def test_a_vix_above_vix_max_plans_nothing_with_the_reason(self) -> None:
        decision = decide(m=market(DAY, BARS, vix=Decimal("23")), minute=None)
        self._skips(decision, "VIX_TOO_HIGH")
        assert decision.numbers["vix"] == "23.00" and decision.numbers["vix_max"] == "22"

    def test_an_opening_range_wider_than_or_max_is_refused(self) -> None:
        bars = bars_from_closes(
            DAY, Decimal("25020"), zigzag(Decimal("25150"), Decimal("120"), to_minutes(15, 30))
        )
        decision = decide(m=market(DAY, bars), minute=None)
        self._skips(decision, "RANGE_TOO_WIDE")
        assert decision.numbers["or_pct"] == "1.0160"

    def test_a_flat_trend_is_refused(self) -> None:
        flat_closes = (PREV_CLOSE,) * 60  # every close equal: the EMA is the previous close
        decision = decide(m=market(DAY, BARS, daily=flat_closes), minute=None)
        self._skips(decision, "TREND_FLAT")

    def test_no_vix_is_refused_rather_than_assumed(self) -> None:
        self._skips(decide(m=market(DAY, BARS, vix=None), minute=None), "VIX_UNKNOWN")

    def test_an_event_day_is_skipped_never_shifted(self) -> None:
        decision = decide(context=DayContext(event_days=frozenset({DAY})))
        self._skips(decision, "EVENT_DAY")

    def test_a_holiday_has_no_session_at_all(self) -> None:
        decision = decide(m=market(DAY, BARS, trading_day=False), minute=None)
        assert decision.state is PlanState.NO_SESSION and not decision.writes_session
        assert decision.reasons == ("NOT_TRADING_DAY",)


# --- the trigger (04 §4.2) ------------------------------------------------------------------------


class TestTheTrigger:
    def test_a_counter_trend_break_plans_nothing_and_is_recorded(self) -> None:
        """Counter-trend breaks are seen and never traded (``04`` §4.2)."""
        bars = o2_counter_bars()  # up-trend history, the tape falls
        m = market(O2_COUNTER, bars)
        armed = decide(m=m, minute=None, now=at(O2_COUNTER, 10, 15))
        assert armed.state is PlanState.NOT_READY and armed.reasons == (NO_TRIGGER_YET,)
        breaks = armed.numbers["counter_trend_breaks"]
        assert isinstance(breaks, list) and breaks
        assert breaks[0] == {"close_time": "10:09", "close": "24990.00", "direction": "DOWN"}
        assert armed.numbers["direction"] == "UP" and finalize(armed).plan is None

    def test_the_window_closing_with_no_trigger_is_a_skipped_session(self) -> None:
        bars = o2_counter_bars()
        decision = decide(m=market(O2_COUNTER, bars), minute=None, now=at(O2_COUNTER, 13, 33))
        assert decision.state is PlanState.SKIPPED and decision.reasons == (NO_TRIGGER,)
        assert decision.verdict is Verdict.SKIP and decision.writes_session
        assert finalize(decision).plan is None

    def test_one_trigger_a_day_whatever_the_tape_does_later(self) -> None:
        """The 10:04 break is the day's trigger; a later minute still prices from 10:05."""
        late = decide(now=at(DAY, 11, 0))
        assert late.trigger is not None and late.trigger.close_time == dt.time(10, 4)
        assert late.as_of_minute == TRIGGER_MINUTE

    def test_a_trigger_after_the_entry_window_is_never_planned(self) -> None:
        decision = decide(minute=None, now=at(DAY, 13, 30) + dt.timedelta(seconds=45))
        assert decision.state is PlanState.WINDOW_CLOSED
        assert decision.reasons == (ENTRY_WINDOW_CLOSED,) and not decision.writes_session


# --- the contract's own refusals ------------------------------------------------------------------


class TestTheDelta:
    """``06`` OP7's "a 0.45-delta ITM strike on a fast day → ``REJECTED_DELTA``".

    The fast day: the index prints 25,150 in the minute after the trigger while the option book's
    own parity forward is still 25,000, so the nominally one-step-ITM 25,100 call is not in the
    money in premium terms at all — ``|delta|`` 0.4508, below the 0.50 floor of ``04`` §4.3.
    """

    def _fast_chain(self) -> Snapshot:
        expiries = sorted({c.expiry for c in MASTER if c.expiry >= DAY})[:2]
        return snapshot(
            TRIGGER_MINUTE,
            Decimal("25150"),
            expiries,
            flat(0.50),
            forwards=dict.fromkeys(expiries, Decimal("25000")),
        )

    def test_an_out_of_band_delta_is_rejected_with_the_number(self) -> None:
        decision = decide(snap=self._fast_chain())
        assert decision.state is PlanState.SKIPPED and decision.reasons == ("REJECTED_DELTA",)
        assert decision.verdict is Verdict.TRADE  # the gate traded; the contract refused
        candidate = decision.candidate
        assert candidate is not None and candidate.legs[0].strike == Decimal(25100)
        assert candidate.legs[0].delta is not None
        assert round(candidate.legs[0].delta, 4) == 0.4508
        assert "outside [0.50, 0.75]" in candidate.message
        assert finalize(decision).plan is None


class TestSizingAndItsCaps:
    def test_a_paused_sleeve_is_rejected_paused(self) -> None:
        decision = decide(context=booked(SleeveBook(), paused=True))
        assert decision.reasons == ("REJECTED_PAUSED",) and decision.state is PlanState.SKIPPED

    def test_live_with_capital_zero_is_rejected_no_sleeve_capital(self) -> None:
        decision = decide(context=booked(SleeveBook(mode=Mode.LIVE)))
        assert decision.reasons == ("REJECTED_NO_SLEEVE_CAPITAL",)

    def test_a_budget_too_small_for_one_lot_is_rejected_budget(self) -> None:
        book = SleeveBook(mode=Mode.PAPER, sleeve_capital_inr=Decimal("100000"))
        decision = decide(context=booked(book))
        assert decision.reasons == ("REJECTED_BUDGET",)
        candidate = decision.candidate
        assert candidate is not None and "₹250.00 / risk per lot ₹4273.12" in candidate.message

    def test_the_premium_cap_cannot_bind_under_04s_ceilings(self) -> None:
        """OP7.6, the parallel of OP6.1: at ``risk_pct_max`` 1 % and a 30 % stop, one trade's
        premium is at most ``1 / 0.30`` = 3.33 % of sleeve capital — the 10 % cap is dormant."""
        cfg = CFG.directional
        most = CEIL.risk_pct_max / cfg.stop_frac  # % of capital the premium can reach
        assert most == Decimal("1.0") / Decimal("0.30")
        assert most < cfg.premium_cap_pct

    def test_the_premium_cap_refuses_when_a_ceiling_allows_it_to_bind(self) -> None:
        ceilings = replace(CEIL, risk_pct_max=Decimal("5"))
        book = SleeveBook(
            mode=Mode.PAPER,
            sleeve_capital_inr=Decimal("100000"),
            risk_per_trade_pct=Decimal("5"),
            real_journal_rows=5,
        )
        decision = decide(context=booked(book), ceilings=ceilings)
        assert decision.reasons == ("REJECTED_PREMIUM_CAP",)
        candidate = decision.candidate
        assert candidate is not None
        assert candidate.message == "1 lots * ₹203.75 * 65 > 10 % of capital"


class TestTheClock:
    @pytest.mark.parametrize(
        ("clock", "state", "reason"),
        [
            ((9, 29, 50), PlanState.NOT_READY, BEFORE_ENTRY_WINDOW),
            ((13, 30, 45), PlanState.WINDOW_CLOSED, ENTRY_WINDOW_CLOSED),
        ],
    )
    def test_outside_the_window_nothing_is_written(
        self, clock: tuple[int, int, int], state: PlanState, reason: str
    ) -> None:
        decision = decide(minute=None, now=dt.datetime(2026, 10, 19, *clock, tzinfo=IST))
        assert (decision.state, decision.reasons) == (state, (reason,))
        assert not decision.writes_session

    def test_an_unsettled_opening_range_waits(self) -> None:
        partial = replace(UP, bars=tuple(b for b in BARS if b.ts < at(DAY, 9, 29)))
        decision = decide(m=partial, minute=None, now=at(DAY, 9, 31))
        assert (decision.state, decision.reasons) == (PlanState.NOT_READY, (RANGE_NOT_SETTLED,))

    def test_no_trigger_snapshot_yet_waits(self) -> None:
        decision = decide(snap=None)
        assert (decision.state, decision.reasons) == (
            PlanState.NOT_READY,
            (NO_DECISION_SNAPSHOT,),
        )

    def test_a_trigger_snapshot_older_than_the_stale_limit_is_not_planned(self) -> None:
        late = at(DAY, 10, 7) + dt.timedelta(seconds=1)  # 121 s after the 10:05 minute
        decision = decide(now=late)
        assert (decision.state, decision.reasons) == (PlanState.SKIPPED, (STALE_CHAIN,))
        assert finalize(decision).plan is None
