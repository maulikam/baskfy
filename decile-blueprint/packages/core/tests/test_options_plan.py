"""OP6 — the O1 plan builder (``06`` OP6): a reasoned no-trade or one four-leg plan.

Every expectation is recomputed from the fixture's quotes and ``04``'s rates by the rule's text;
the literals are that arithmetic written out, so a changed rule — not a changed implementation — is
what turns a test red. The fixture morning is OP4's quiet monthly (Tue 27 Oct 2026) stored as the
collector stores it; the margin figure is the shape the OP3 probe read on the box (one-lot NIFTY
condor, ``final`` ₹1,30,903.05 — ``docs/options/evidence/op3-probe-2026-09-22.json``).
"""

from __future__ import annotations

import datetime as dt
import inspect
from dataclasses import replace
from decimal import Decimal

import pytest
from options_fixtures import LOT
from options_scan_fixtures import (
    MASTER,
    QUIET_MONTHLY,
    TREND_WEEKLY,
    Smile,
    at,
    bars_from_closes,
    carry_forward,
    flat,
    market,
    quiet_monthly_bars,
    skew,
    snapshot,
    to_minutes,
    trend_weekly_bars,
    zigzag,
)

from baskfy_core.options import config as config_module
from baskfy_core.options.bars import IST, Bar, closed
from baskfy_core.options.chain import Level
from baskfy_core.options.config import (
    OPTIONS_COST_RATES_REVIEWED_ON,
    CostRates,
    Mode,
    OptionsCeilings,
    OptionsConfig,
    OptionType,
    Side,
    SizingMode,
    Sleeve,
)
from baskfy_core.options.costs import rates_review_due
from baskfy_core.options.execution import LegRole, never_naked
from baskfy_core.options.plan import (
    MARGIN_UNKNOWN,
    STALE_CHAIN,
    MarginQuote,
    O1Decision,
    O1Outcome,
    PlanState,
    Verdict,
    decide_o1,
    finalize_o1,
    plan_id_for,
)
from baskfy_core.options.scan import (
    DayContext,
    MarketDay,
    SleeveContext,
    Snapshot,
    SnapshotBook,
    scan_all,
)
from baskfy_core.options.structures import SleeveBook

CFG = OptionsConfig()
CEIL = OptionsCeilings()
SMILE = skew(0.30, 0.33)
QUIET = market(QUIET_MONTHLY, quiet_monthly_bars())
NOW = at(QUIET_MONTHLY, 10, 0) + dt.timedelta(seconds=40)
PROBE_MARGIN = MarginQuote(hedged_inr=Decimal("130903.05"), transient_inr=Decimal("98250.00"))


def chain(bars: tuple[Bar, ...], minute: dt.datetime, smile: Smile = SMILE) -> Snapshot:
    """What the collector stores at ``minute``: the two nearest expiries around the last close,
    with a cost-of-carry forward (as ``test_options_scan``)."""
    spot = closed(bars, minute)[-1].close
    expiries = sorted({c.expiry for c in MASTER if c.expiry >= minute.date()})[:2]
    forwards = {e: carry_forward(spot, minute, e) for e in expiries}
    return snapshot(minute, spot, expiries, smile, forwards=forwards)


SNAP = chain(QUIET.bars, at(QUIET_MONTHLY, 10, 0))


def decide(  # noqa: PLR0913, PLR0917 - the test's knobs
    sleeve: Sleeve = Sleeve.O1M,
    m: MarketDay = QUIET,
    snap: Snapshot | None = SNAP,
    now: dt.datetime = NOW,
    context: DayContext | None = None,
    options: OptionsConfig = CFG,
) -> O1Decision:
    return decide_o1(sleeve, m, context or DayContext(), snap, now, options=options, ceilings=CEIL)


def finalize(  # noqa: PLR0913 - the test's knobs
    decision: O1Decision,
    margin: MarginQuote | None = PROBE_MARGIN,
    *,
    mode: Mode = Mode.PAPER,
    pool: Decimal = Decimal(0),
    in_use: Decimal = Decimal(0),
    options: OptionsConfig = CFG,
) -> O1Outcome:
    return finalize_o1(
        decision,
        margin,
        user_id=7,
        mode=mode,
        margin_pool_inr=pool,
        margin_in_use_inr=in_use,
        options=options,
    )


# --- the fixture morning, to the rupee -----------------------------------------------------------


class TestTheFixtureMorning:
    def test_the_plan_to_the_rupee(self) -> None:
        outcome = finalize(decide())
        plan = outcome.plan
        assert outcome.state is PlanState.PLANNED and plan is not None
        # Legs in send order, never naked: both wings, then the shorts (condor 04 §4.6).
        assert [(lg.seq, lg.role, lg.strike, lg.side) for lg in plan.legs] == [
            (1, LegRole.LONG_PUT, Decimal(24700), Side.BUY),
            (2, LegRole.LONG_CALL, Decimal(25300), Side.BUY),
            (3, LegRole.SHORT_PUT, Decimal(24850), Side.SELL),
            (4, LegRole.SHORT_CALL, Decimal(25150), Side.SELL),
        ]
        assert [lg.tradingsymbol for lg in plan.legs] == [
            "NIFTY26102724700PE",
            "NIFTY26102725300CE",
            "NIFTY26102724850PE",
            "NIFTY26102725150CE",
        ]
        # Quotes and attempt-1 limits (04 §8.2): buy ask + tick, sell bid - tick.
        assert [(lg.bid, lg.ask, lg.limit_price) for lg in plan.legs] == [
            (Decimal("5.30"), Decimal("5.40"), Decimal("5.45")),
            (Decimal("5.65"), Decimal("5.80"), Decimal("5.85")),
            (Decimal("23.80"), Decimal("24.05"), Decimal("23.75")),
            (Decimal("27.05"), Decimal("27.35"), Decimal("27.00")),
        ]
        # Credit: shorts at bid, wings at ask (condor §4.4) = 23.80 + 27.05 - 5.40 - 5.80.
        assert plan.credit_points == Decimal("39.65")
        assert plan.credit_inr == Decimal("39.65") * LOT == Decimal("2577.25")
        assert (plan.lots, plan.lot_size, plan.quantity) == (1, LOT, LOT)
        assert plan.sizing_mode is SizingMode.PAPER_ONE_LOT and plan.mode is Mode.PAPER
        # Risk per lot (150 - C) * 65 + 1,000; R = one lot's risk in paper (PACK.6).
        assert plan.risk_per_lot_inr == Decimal("8172.75") == plan.risk_budget_inr
        assert plan.max_loss_inr == (150 - Decimal("39.65")) * LOT == Decimal("7172.75")
        # PROFIT at D <= 0.5 C keeps 0.5 C; STOP at D >= 1.5 C loses 0.5 C (condor §7).
        assert plan.profit_target_inr == Decimal("1288.63")  # 0.5 * 2,577.25, half up
        assert plan.stop_inr == Decimal("1288.63")
        assert plan.margin_required_inr == Decimal("130903.05")
        assert plan.margin_transient_inr == Decimal("98250.00")
        assert plan.warnings == ("MARGIN_POOL_UNSET",)  # paper, pool ₹0 (04 §7.4)
        assert plan.expires_at == dt.datetime(2026, 10, 27, 10, 15, tzinfo=IST)
        assert plan.entry_window_end == plan.expires_at
        assert plan.as_of_minute == at(QUIET_MONTHLY, 10, 0)

    def test_the_costs_to_the_paisa_from_the_verified_rates(self) -> None:
        """``04`` §6.1 by hand over the eight orders at the plan's prices (OP4.5)."""
        plan = finalize(decide()).plan
        assert plan is not None
        entry_sells = (Decimal("23.75") + Decimal("27.00")) * LOT  # 3,298.75
        exit_sells = (Decimal("5.25") + Decimal("5.60")) * LOT  # wings out at bid - tick: 705.25
        entry_buys = (Decimal("5.45") + Decimal("5.85")) * LOT  # 734.50
        exit_buys = (Decimal("24.10") + Decimal("27.40")) * LOT  # shorts back at ask + tick
        sells, buys = entry_sells + exit_sells, entry_buys + exit_buys
        assert (sells, buys) == (Decimal("4004.00"), Decimal("4082.00"))
        c = plan.costs
        assert c.orders == 8
        assert c.brokerage == Decimal("160.00")  # 8 x ₹20
        assert c.stt == Decimal("6.01")  # 4,004.00 x 0.15 % = 6.006
        assert c.exchange_txn == Decimal("2.87")  # 8,086.00 x 0.03553 % = 2.8730
        assert c.sebi == Decimal("0.01")  # 8,086 x ₹10 / crore = 0.0081
        assert c.ipft == Decimal("0.00")  # 8,086 x ₹0.01 / crore
        assert c.stamp == Decimal("0.12")  # 4,082.00 x 0.003 % = 0.1225
        assert c.gst == Decimal("29.32")  # 18 % of (160 + 2.87 + 0.01 + 0.00) = 29.318
        assert c.total == plan.expected_cost_inr == Decimal("198.33")
        # Cost share = 198.33 / (0.5 x 2,577.25) = 0.1539 (04 §6.4) <= 0.20.
        assert plan.cost_share == Decimal("0.1539")
        assert plan.cost_share <= CFG.condor_monthly.cost_share_max

    def test_the_plan_is_the_scans_candidate(self) -> None:
        """``04`` §10: the scan and the plan are one computation over the same snapshot."""
        decision = decide()
        book = SnapshotBook({at(QUIET_MONTHLY, 10, 0): SNAP, None: SNAP})
        scanned = {r.sleeve: r for r in scan_all(QUIET, DayContext(), NOW, book, CFG, CEIL)}
        (candidate,) = scanned[Sleeve.O1M].candidates
        assert decision.candidate == candidate
        assert decision.costs is not None and decision.costs.total == candidate.round_trip_inr

    def test_never_naked_at_every_prefix_of_the_send_order(self) -> None:
        plan = finalize(decide()).plan
        assert plan is not None
        held: dict[LegRole, int] = {}
        for lg in plan.legs:
            held[lg.role] = held.get(lg.role, 0) + lg.quantity
            assert never_naked(held)
        assert {lg.role for lg in plan.legs[:2]} == {LegRole.LONG_PUT, LegRole.LONG_CALL}

    def test_the_transient_basket_is_the_wings_and_the_first_short(self) -> None:
        hedged, transient = decide().margin_baskets()
        assert [(m.tradingsymbol, m.side) for m in hedged][-1] == ("NIFTY26102725150CE", Side.SELL)
        assert transient == hedged[:3]
        assert all(m.quantity == LOT for m in hedged)

    def test_plan_id_is_deterministic_and_a_valid_client_id_half(self) -> None:
        a, b = finalize(decide()).plan, finalize(decide()).plan
        assert a is not None and b is not None
        assert a.plan_id == b.plan_id and a.plan_id.startswith("O1M-20261027-")
        assert ":" not in a.plan_id and not any(ch.isspace() for ch in a.plan_id)
        assert plan_id_for(8, decide()) != a.plan_id  # another tenant, another id

    def test_expires_at_is_min_of_thirty_minutes_and_the_window(self) -> None:
        wide = replace(
            CFG,
            condor_monthly=replace(CFG.condor_monthly, entry_window_end=dt.time(11, 0)),
        )
        plan = finalize(decide(options=wide), options=wide).plan
        assert plan is not None
        assert plan.expires_at == NOW + dt.timedelta(minutes=30)
        assert plan.expires_at < plan.entry_window_end


# --- refusals -------------------------------------------------------------------------------------


def _widen(snap: Snapshot, strike: Decimal, kind: OptionType, bid: Decimal) -> Snapshot:
    quotes = tuple(
        replace(q, bid=bid, bids=(Level(bid, 1300),) * 3)
        if q.expiry == QUIET_MONTHLY and q.strike == strike and q.option_type is kind
        else q
        for q in snap.quotes
    )
    return replace(snap, quotes=quotes)


class TestTheWideShortPut:
    """``06`` OP6 AC "wide short-put spread → REJECTED_COST", scoped by OP6.1: a spread wide
    enough to matter fails ``04`` §2.4's liquidity first, and one inside it is already in the
    credit (shorts at bid), so it cannot be what makes the cost test bind."""

    def test_beyond_max_spread_the_put_is_not_a_short(self) -> None:
        # 24850 PE ask 24.05; bid 23.30 is a 3.17 % spread > 3.0 %.
        d = decide(snap=_widen(SNAP, Decimal(24850), OptionType.PE, Decimal("23.30")))
        assert d.state is PlanState.SKIPPED and d.reasons == ("REJECTED_NO_SHORT_PUT",)
        assert d.verdict is Verdict.TRADE

    def test_inside_max_spread_it_only_lowers_the_credit(self) -> None:
        d = decide(snap=_widen(SNAP, Decimal(24850), OptionType.PE, Decimal("23.40")))
        assert d.state is PlanState.PLANNED and d.candidate is not None
        assert d.candidate.points == Decimal("39.25")  # 0.40 lower, exactly the bid's move
        assert d.candidate.cost_share is not None and d.candidate.cost_share < Decimal("0.16")


class TestTheCostTest:
    def test_a_credit_the_costs_eat_is_rejected_cost(self) -> None:
        """A flat 16 % chain with the credit floor at 0.10 (a recalibration a Tier-3 finding
        could make): C = 25.35, share = 194.67 / (0.5 x 25.35 x 65) > 0.20."""
        low = replace(
            CFG, condor_monthly=replace(CFG.condor_monthly, credit_floor_frac=Decimal("0.10"))
        )
        snap = chain(QUIET.bars, at(QUIET_MONTHLY, 10, 0), flat(0.16))
        d = decide(snap=snap, options=low)
        assert d.state is PlanState.SKIPPED and d.reasons == ("REJECTED_COST",)
        c = d.candidate
        assert c is not None and c.points == Decimal("25.35")
        assert c.cost_share is not None and c.cost_share > low.condor_monthly.cost_share_max
        assert c.round_trip_inr is not None
        assert c.cost_share == c.round_trip_inr / (Decimal("0.5") * Decimal("25.35") * LOT)

    def test_at_the_default_floor_the_cost_test_cannot_bind_for_one_lot(self) -> None:
        """OP6.1's arithmetic: at C = 0.25 x 150 the gain is 0.5 x 37.5 x 65 = ₹1,218.75, and a
        round trip near ₹200 is a share near 0.16 — so the floor, not the cost test, refuses."""
        gain = (
            (1 - CFG.condor_monthly.profit_take_frac)
            * (CFG.condor_monthly.credit_floor_frac * CFG.condor_monthly.wing_width_points)
            * LOT
        )
        assert gain == Decimal("1218.75")
        assert Decimal("198.33") / gain < CFG.condor_monthly.cost_share_max


def spike_bars() -> tuple[Bar, ...]:
    """The quiet monthly with one early spike to 25,150: OR 25,000-25,152 (0.61 %), the 09:59
    close back inside it, ER low — the gate passes, and 25,150 CE is no longer beyond the range."""
    closes = zigzag(Decimal("25010"), Decimal("8"), to_minutes(15, 30))
    closes[5] = Decimal("25150")
    return bars_from_closes(QUIET_MONTHLY, Decimal("25010"), closes)


class TestTheShortCallInsideTheRange:
    def test_only_in_band_call_inside_the_or_is_rejected_no_short_call(self) -> None:
        m = market(QUIET_MONTHLY, spike_bars())
        d = decide(m=m, snap=chain(m.bars, at(QUIET_MONTHLY, 10, 0)))
        assert d.numbers["or_high"] == "25152.00" and d.numbers["contained"] is True
        assert d.verdict is Verdict.TRADE
        assert d.state is PlanState.SKIPPED and d.reasons == ("REJECTED_NO_SHORT_CALL",)


class TestWhichDaysHaveASession:
    def test_o1w_on_the_monthly_tuesday_has_no_session(self) -> None:
        d = decide(Sleeve.O1W)
        assert d.state is PlanState.NO_SESSION and not d.writes_session
        assert d.reasons == ("MONTHLY_EXPIRY",)
        assert finalize(d).plan is None

    def test_o1m_on_a_weekly_has_no_session(self) -> None:
        m = market(TREND_WEEKLY, trend_weekly_bars())
        assert decide(Sleeve.O1M, m=m).state is PlanState.NO_SESSION

    def test_an_event_day_on_the_monthly_is_skipped_not_shifted(self) -> None:
        d = decide(context=DayContext(event_days=frozenset({QUIET_MONTHLY})))
        assert (d.state, d.reasons, d.verdict) == (
            PlanState.SKIPPED,
            ("EVENT_DAY",),
            Verdict.SKIP,
        )

    def test_the_gate_skips_with_every_reason(self) -> None:
        m = market(TREND_WEEKLY, trend_weekly_bars())
        d = decide(
            Sleeve.O1W,
            m=m,
            snap=chain(m.bars, at(TREND_WEEKLY, 10, 0)),
            now=at(TREND_WEEKLY, 10, 1),
        )
        assert d.state is PlanState.SKIPPED and d.verdict is Verdict.SKIP
        assert {"NOT_CONTAINED", "ER_TOO_HIGH"} <= set(d.reasons)
        assert d.candidate is None


class TestTheSlotAndThePause:
    def test_slot_held_by_o3_is_rejected_slot_taken(self) -> None:
        d = decide(context=DayContext(slot_holder=Sleeve.O3B))
        assert d.state is PlanState.SKIPPED and d.reasons == ("REJECTED_SLOT_TAKEN",)

    def test_a_slot_held_by_itself_is_not_refused(self) -> None:
        assert decide(context=DayContext(slot_holder=Sleeve.O1M)).state is PlanState.PLANNED

    def test_a_paused_sleeve_is_rejected_paused(self) -> None:
        ctx = DayContext(sleeves={Sleeve.O1M: SleeveContext(paused=True)})
        assert decide(context=ctx).reasons == ("REJECTED_PAUSED",)

    def test_live_with_capital_zero_is_rejected_no_sleeve_capital(self) -> None:
        ctx = DayContext(sleeves={Sleeve.O1M: SleeveContext(book=SleeveBook(mode=Mode.LIVE))})
        assert decide(context=ctx).reasons == ("REJECTED_NO_SLEEVE_CAPITAL",)


class TestTheClock:
    @pytest.mark.parametrize(
        ("clock", "state", "reason"),
        [
            ((9, 59, 50), PlanState.NOT_READY, "BEFORE_PLAN_TIME"),
            ((10, 15, 0), PlanState.WINDOW_CLOSED, "ENTRY_WINDOW_CLOSED"),
        ],
    )
    def test_outside_the_window_nothing_is_decided(
        self, clock: tuple[int, int, int], state: PlanState, reason: str
    ) -> None:
        now = dt.datetime(2026, 10, 27, *clock, tzinfo=IST)
        d = decide(now=now)
        assert (d.state, d.reasons) == (state, (reason,)) and not d.writes_session

    def test_no_decision_snapshot_yet_waits(self) -> None:
        d = decide(snap=None)
        assert (d.state, d.reasons) == (PlanState.NOT_READY, ("NO_DECISION_SNAPSHOT",))

    def test_the_0959_bar_not_stored_yet_waits(self) -> None:
        m = replace(QUIET, bars=tuple(b for b in QUIET.bars if b.ts < at(QUIET_MONTHLY, 9, 59)))
        d = decide(m=m)
        assert (d.state, d.reasons) == (PlanState.NOT_READY, ("WINDOW_NOT_SETTLED",))

    def test_a_decision_snapshot_older_than_the_stale_limit_is_not_planned(self) -> None:
        late = at(QUIET_MONTHLY, 10, 2) + dt.timedelta(seconds=1)  # 121 s after the 10:00 minute
        d = decide(now=late)
        assert (d.state, d.reasons) == (PlanState.SKIPPED, (STALE_CHAIN,))


class TestTheMargin:
    def test_a_pool_too_small_is_rejected_margin(self) -> None:
        out = finalize(decide(), pool=Decimal("150000"), in_use=Decimal("20000"))
        assert out.plan is None and out.state is PlanState.SKIPPED
        assert out.reasons == ("REJECTED_MARGIN",)
        assert "130903.05" in str(out.decision.numbers["margin_message"])

    def test_the_transient_figure_is_a_ceiling_too(self) -> None:
        big = MarginQuote(hedged_inr=Decimal("100000"), transient_inr=Decimal("160000"))
        out = finalize(decide(), big, pool=Decimal("150000"))
        assert out.reasons == ("REJECTED_MARGIN",)
        assert "transient" in str(out.decision.numbers["margin_message"])

    def test_a_pool_that_fits_is_ok_with_no_warning(self) -> None:
        plan = finalize(decide(), pool=Decimal("200000")).plan
        assert plan is not None and plan.warnings == ()
        assert plan.margin is not None and plan.margin.code.value == "OK"

    def test_no_answer_is_a_warning_in_paper_and_a_refusal_live(self) -> None:
        paper = finalize(decide(), None).plan
        assert paper is not None and MARGIN_UNKNOWN in paper.warnings
        assert paper.margin_required_inr is None
        live = finalize(decide(), None, mode=Mode.LIVE)
        assert live.plan is None and live.reasons == ("REJECTED_MARGIN",)

    def test_margin_never_sizes(self) -> None:
        """Track C §10: lots are fixed before the calculator is asked."""
        a = finalize(decide(), pool=Decimal("10000000")).plan
        b = finalize(decide(), pool=Decimal("200000")).plan
        assert a is not None and b is not None and a.lots == b.lots == 1


class TestTheRatesArePinned:
    def test_each_rate_is_opzeros_verified_value(self) -> None:
        r = CostRates()
        assert (
            r.brokerage_per_order_inr,
            r.stt_sell_premium_pct,
            r.stt_exercise_intrinsic_pct,
            r.exchange_txn_pct,
            r.sebi_per_crore_inr,
            r.ipft_per_crore_inr,
            r.stamp_buy_pct,
            r.gst_pct,
            r.auto_squareoff_inr,
        ) == (
            Decimal("20"),
            Decimal("0.15"),
            Decimal("0.15"),
            Decimal("0.03553"),
            Decimal("10"),
            Decimal("0.01"),
            Decimal("0.003"),
            Decimal("18"),
            Decimal("50"),
        )

    def test_each_source_is_named_beside_the_rates(self) -> None:
        doc = inspect.getdoc(CostRates) or ""
        for source in (
            "https://www.indiabudget.gov.in/doc/memo.pdf",
            "https://zerodha.com/charges/",
            "NSE/FA/73061",
        ):
            assert source in doc

    def test_reviewed_on_is_the_constant_and_warns_after_90_days(self) -> None:
        assert dt.date(2026, 9, 22) == OPTIONS_COST_RATES_REVIEWED_ON
        assert CostRates().reviewed_on == config_module.OPTIONS_COST_RATES_REVIEWED_ON
        assert not rates_review_due(CostRates(), dt.date(2026, 12, 21))
        assert rates_review_due(CostRates(), dt.date(2026, 12, 22))
