"""OP4 — the scan (``04`` §10): each fixture day yields its expected state, reasons and candidate;
the candidate *is* the plan builder's computation; the slot is exclusive; states are one-way;
the same inputs give the same row (the task's per-minute idempotence rests on it)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal

import pytest
from options_fixtures import LOT, TICK
from options_scan_fixtures import (
    GAP_HOLD,
    MASTER,
    O2_COUNTER,
    O2_TUESDAY,
    O2_UP_BREAK,
    QUIET_MONTHLY,
    SETTLE,
    TREND_WEEKLY,
    Smile,
    at,
    carry_forward,
    flat,
    gap_hold_bars,
    market,
    o2_counter_bars,
    o2_up_break_bars,
    quiet_monthly_bars,
    skew,
    snapshot,
    trend_weekly_bars,
)

from baskfy_core.options import condor, directional, expiry_setups
from baskfy_core.options.bars import IST, Bar, closed
from baskfy_core.options.calendar import expiry_for_o2
from baskfy_core.options.config import OptionsCeilings, OptionsConfig, Sleeve
from baskfy_core.options.scan import (
    DayContext,
    MarketDay,
    ScanResult,
    ScanState,
    SleeveContext,
    Snapshot,
    SnapshotBook,
    scan_all,
    wanted_minutes,
)
from baskfy_core.options.session import SessionState
from baskfy_core.options.structures import (
    Direction,
    Rejection,
    SleeveBook,
    chain_view,
    to_json,
)

CFG = OptionsConfig()
CEIL = OptionsCeilings()


def two_nearest(day: dt.date) -> list[dt.date]:
    return sorted({c.expiry for c in MASTER if c.expiry >= day})[:2]


def snapshot_at(bars: tuple[Bar, ...], minute: dt.datetime, smile: Smile) -> Snapshot | None:
    """What the collector would have stored at ``minute``: the chain around the last close."""
    seen = closed(bars, minute)
    if not seen:
        return None
    spot = seen[-1].close
    exps = two_nearest(minute.date())
    fwd = {e: carry_forward(spot, minute, e) for e in exps}
    return snapshot(minute, spot, exps, smile, forwards=fwd)


def run(  # noqa: PLR0913 - the test's knobs
    m: MarketDay,
    now: dt.datetime,
    smile: Smile,
    context: DayContext | None = None,
    *,
    latest_lag_minutes: int = 0,
    options: OptionsConfig = CFG,
) -> dict[Sleeve, ScanResult]:
    """The worker's two phases, with fixture snapshots standing in for ``op_chain_snapshot``."""
    ctx = context or DayContext()
    minute = now.replace(second=0, microsecond=0)
    loaded: dict[dt.datetime | None, Snapshot | None] = {}
    for wanted in wanted_minutes(m, ctx, now, options, CEIL):
        at_minute = wanted or minute - dt.timedelta(minutes=latest_lag_minutes)
        loaded[wanted] = snapshot_at(m.bars, at_minute, smile)
    return {r.sleeve: r for r in scan_all(m, ctx, now, SnapshotBook(loaded), options, CEIL)}


def plus20(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return at(day, hh, mm) + dt.timedelta(seconds=20)


QUIET = market(QUIET_MONTHLY, quiet_monthly_bars())
TREND = market(TREND_WEEKLY, trend_weekly_bars())
GAP = market(GAP_HOLD, gap_hold_bars())
UP = market(O2_UP_BREAK, o2_up_break_bars())
COUNTER = market(O2_COUNTER, o2_counter_bars())
MONTHLY_SMILE = skew(0.30, 0.33)


class TestEachFixtureDay:
    def test_quiet_monthly_o1m_would_trade(self) -> None:
        r = run(QUIET, plus20(QUIET_MONTHLY, 10, 1), MONTHLY_SMILE)[Sleeve.O1M]
        assert r.state is ScanState.WOULD_TRADE and r.reasons == ()
        (c,) = r.candidates
        # The collector's chain carries a cost-of-carry forward, so the credit is 39.65 here
        # (test_options_condor prices the same smile on forward = spot: 39.55).
        assert c.viable and c.points == Decimal("39.65") and c.lots == 1
        assert r.as_of_minute == at(QUIET_MONTHLY, 10, 0)  # the plan_time snapshot
        assert r.numbers["gap_pct"] == "0.0400" and r.numbers["contained"] is True

    def test_quiet_monthly_observes_before_and_is_done_after_the_window(self) -> None:
        assert run(QUIET, plus20(QUIET_MONTHLY, 9, 50), MONTHLY_SMILE)[Sleeve.O1M].state is (
            ScanState.OBSERVING
        )
        after = run(QUIET, plus20(QUIET_MONTHLY, 10, 15), MONTHLY_SMILE)[Sleeve.O1M]
        assert after.state is ScanState.DONE
        assert after.numbers["verdict"] == ScanState.WOULD_TRADE.value

    def test_on_the_monthly_o1w_is_not_today(self) -> None:
        r = run(QUIET, plus20(QUIET_MONTHLY, 10, 1), MONTHLY_SMILE)[Sleeve.O1W]
        assert r.state is ScanState.NOT_TODAY and r.reasons == ("MONTHLY_EXPIRY",)
        assert r.numbers["next_date"] == "2026-11-03"

    def test_trend_weekly_o1w_skips_with_er_and_containment(self) -> None:
        r = run(TREND, plus20(TREND_WEEKLY, 10, 5), flat(0.14))[Sleeve.O1W]
        assert r.state is ScanState.WOULD_SKIP
        assert r.reasons == ("NOT_CONTAINED", "ER_TOO_HIGH")
        assert r.candidates == ()

    def test_trend_weekly_o3a_triggers(self) -> None:
        r = run(TREND, plus20(TREND_WEEKLY, 10, 21), flat(0.14))[Sleeve.O3A]
        assert r.state is ScanState.TRIGGERED and r.reasons == ()
        assert r.numbers["trigger_time"] == "10:19" and r.numbers["direction"] == "UP"
        (c,) = r.candidates
        assert c.viable and c.direction is Direction.UP and c.expiry == TREND_WEEKLY
        assert r.as_of_minute == at(TREND_WEEKLY, 10, 20)  # the minute the trigger bar closed

    def test_trend_weekly_o3b_has_no_gap(self) -> None:
        r = run(TREND, plus20(TREND_WEEKLY, 9, 46), flat(0.14))[Sleeve.O3B]
        assert r.state is ScanState.DAY_SKIPPED and r.reasons == ("GAP_TOO_SMALL",)

    def test_gap_hold_o3b_triggers_at_0945(self) -> None:
        r = run(GAP, plus20(GAP_HOLD, 9, 46), flat(0.14))[Sleeve.O3B]
        assert r.state is ScanState.TRIGGERED
        (c,) = r.candidates
        assert c.viable and c.points == Decimal("26.95")
        assert [lg.strike for lg in c.legs] == [Decimal(25200), Decimal(25300)]
        assert r.as_of_minute == at(GAP_HOLD, 9, 45)

    def test_gap_hold_o3b_builds_before_the_plan_time(self) -> None:
        r = run(GAP, plus20(GAP_HOLD, 9, 30), flat(0.14))[Sleeve.O3B]
        assert r.state is ScanState.BUILDING_RANGE
        assert r.numbers["held_so_far"] is True and r.numbers["direction"] == "UP"

    def test_o2_up_break_monday_uses_tuesdays_contract(self) -> None:
        r = run(UP, plus20(O2_UP_BREAK, 10, 6), flat(0.13))[Sleeve.O2]
        assert r.state is ScanState.TRIGGERED
        assert r.numbers["trigger_time"] == "10:04" and r.numbers["index"] == "NIFTY 50"
        assert r.numbers["expiry"] == "2026-10-20"
        (c,) = r.candidates
        assert c.viable and c.expiry == dt.date(2026, 10, 20)
        assert c.legs[0].strike == Decimal(25000) and c.points == Decimal("106.75")

    def test_o2_on_a_tuesday_expiry_uses_next_weeks_contract(self) -> None:
        assert expiry_for_o2(O2_TUESDAY, MASTER) == dt.date(2026, 10, 27)
        r = run(TREND, plus20(TREND_WEEKLY, 9, 40), flat(0.13))[Sleeve.O2]
        assert r.numbers["expiry"] == "2026-10-27"
        assert all(c.expiry == dt.date(2026, 10, 27) for c in r.candidates)

    def test_o2_counter_trend_break_is_seen_not_traded(self) -> None:
        r = run(COUNTER, plus20(O2_COUNTER, 10, 30), flat(0.13))[Sleeve.O2]
        assert r.state is ScanState.ARMED
        breaks = r.numbers["counter_trend_breaks"]
        assert isinstance(breaks, list) and breaks[0] == {
            "close_time": "10:09",
            "close": "24990.00",
            "direction": "DOWN",
        }
        closing = run(COUNTER, plus20(O2_COUNTER, 13, 33), flat(0.13))[Sleeve.O2]
        assert closing.state is ScanState.WINDOW_CLOSED

    def test_o2_armed_shows_the_level_and_the_distance(self) -> None:
        r = run(UP, plus20(O2_UP_BREAK, 9, 45), flat(0.13))[Sleeve.O2]
        assert r.state is ScanState.ARMED and r.numbers["direction"] == "UP"
        last = Decimal(str(r.numbers["last_close"]))
        level = Decimal(25032) * Decimal("1.0005")
        assert r.numbers["trigger_level"] == str(level.quantize(Decimal("0.01")))
        assert Decimal(str(r.numbers["distance_points"])) == (level - last).quantize(
            Decimal("0.01")
        )
        assert r.candidates and r.candidates[0].viable

    def test_o2_builds_its_range_before_0930(self) -> None:
        assert run(UP, plus20(O2_UP_BREAK, 9, 20), flat(0.13))[Sleeve.O2].state is (
            ScanState.BUILDING_RANGE
        )

    def test_o2_day_skipped_on_high_vix(self) -> None:
        m = market(O2_UP_BREAK, o2_up_break_bars(), vix=Decimal(25))
        r = run(m, plus20(O2_UP_BREAK, 9, 40), flat(0.13))[Sleeve.O2]
        assert r.state is ScanState.DAY_SKIPPED and r.reasons == ("VIX_TOO_HIGH",)

    def test_o3_on_a_non_expiry_is_not_today(self) -> None:
        r = run(UP, plus20(O2_UP_BREAK, 10, 6), flat(0.13))
        assert r[Sleeve.O3A].state is ScanState.NOT_TODAY
        assert r[Sleeve.O3A].numbers["next_date"] == "2026-10-20"

    def test_an_event_day_is_not_today_for_every_sleeve(self) -> None:
        ctx = DayContext(event_days=frozenset({QUIET_MONTHLY}))
        r = run(QUIET, plus20(QUIET_MONTHLY, 10, 1), MONTHLY_SMILE, ctx)
        assert {s: x.state for s, x in r.items()} == dict.fromkeys(Sleeve, ScanState.NOT_TODAY)
        assert all(x.reasons == ("EVENT_DAY",) for x in r.values())

    def test_a_holiday_is_not_today(self) -> None:
        m = market(QUIET_MONTHLY, quiet_monthly_bars(), trading_day=False)
        r = run(m, plus20(QUIET_MONTHLY, 10, 1), MONTHLY_SMILE)
        assert all(x.reasons == ("NOT_TRADING_DAY",) for x in r.values())


class TestTheCandidateIsThePlan:
    """``04`` §10: a plan built from the same inputs equals the scan's candidate."""

    def test_o1(self) -> None:
        r = run(QUIET, plus20(QUIET_MONTHLY, 10, 1), MONTHLY_SMILE)[Sleeve.O1M]
        snap = snapshot_at(QUIET.bars, at(QUIET_MONTHLY, 10, 0), MONTHLY_SMILE)
        assert snap is not None and snap.spot is not None
        view = chain_view(
            snap.quotes, expiry=QUIET_MONTHLY, spot=snap.spot, now=snap.ts, tick=TICK,
            config=CFG.chain, settle=SETTLE,
            listed_strikes=[c.strike for c in MASTER if c.expiry == QUIET_MONTHLY],
        )  # fmt: skip
        plan = condor.build(
            view,
            or_high=Decimal(str(r.numbers["or_high"])),
            or_low=Decimal(str(r.numbers["or_low"])),
            lot_size=LOT,
            book=SleeveBook(),
            config=CFG.condor_monthly,
            options=CFG,
            ceilings=CEIL,
            expiry=QUIET_MONTHLY,
        )
        assert r.candidates == (plan,)

    def test_o2(self) -> None:
        r = run(UP, plus20(O2_UP_BREAK, 10, 6), flat(0.13))[Sleeve.O2]
        snap = snapshot_at(UP.bars, at(O2_UP_BREAK, 10, 5), flat(0.13))
        assert snap is not None and snap.spot is not None
        tue = dt.date(2026, 10, 20)
        view = chain_view(
            snap.quotes, expiry=tue, spot=snap.spot, now=snap.ts, tick=TICK, config=CFG.chain,
            settle=SETTLE, listed_strikes=[c.strike for c in MASTER if c.expiry == tue],
        )  # fmt: skip
        plan = directional.build(
            view, Direction.UP, lot_size=LOT, book=SleeveBook(), config=CFG.directional,
            options=CFG, ceilings=CEIL, expiry=tue,
        )  # fmt: skip
        assert r.candidates == (plan,)

    def test_o3(self) -> None:
        r = run(GAP, plus20(GAP_HOLD, 9, 46), flat(0.14))[Sleeve.O3B]
        snap = snapshot_at(GAP.bars, at(GAP_HOLD, 9, 45), flat(0.14))
        assert snap is not None and snap.spot is not None
        view = chain_view(
            snap.quotes, expiry=GAP_HOLD, spot=snap.spot, now=snap.ts, tick=TICK,
            config=CFG.chain, settle=SETTLE,
            listed_strikes=[c.strike for c in MASTER if c.expiry == GAP_HOLD],
        )  # fmt: skip
        plan = expiry_setups.build(
            view, Direction.UP, lot_size=LOT, book=SleeveBook(), config=CFG.expiry_setups,
            options=CFG, ceilings=CEIL, expiry=GAP_HOLD,
        )  # fmt: skip
        assert r.candidates == (plan,)


class TestTheSlot:
    def test_o3_holding_the_slot_makes_o1_slot_taken_and_leaves_o2_alone(self) -> None:
        ctx = DayContext(slot_holder=Sleeve.O3B)
        r = run(QUIET, plus20(QUIET_MONTHLY, 10, 1), MONTHLY_SMILE, ctx)
        assert r[Sleeve.O1M].state is ScanState.SLOT_TAKEN
        assert r[Sleeve.O1M].reasons == (Rejection.REJECTED_SLOT_TAKEN.value,)
        assert r[Sleeve.O2].state is not ScanState.SLOT_TAKEN

    def test_a_sleeve_holding_the_slot_itself_is_not_refused(self) -> None:
        ctx = DayContext(slot_holder=Sleeve.O1M)
        r = run(QUIET, plus20(QUIET_MONTHLY, 10, 1), MONTHLY_SMILE, ctx)
        assert r[Sleeve.O1M].state is ScanState.WOULD_TRADE

    def test_o3b_holds_the_day_so_o3a_cannot(self) -> None:
        r = run(GAP, plus20(GAP_HOLD, 10, 30), flat(0.14))
        assert r[Sleeve.O3B].state is ScanState.TRIGGERED
        assert r[Sleeve.O3A].state is ScanState.SLOT_TAKEN
        assert "O3B_HOLDS" in r[Sleeve.O3A].reasons

    def test_a_lapsed_o3b_frees_o3a(self) -> None:
        ctx = DayContext(sleeves={Sleeve.O3B: SleeveContext(session_state=SessionState.LAPSED)})
        r = run(GAP, plus20(GAP_HOLD, 10, 30), flat(0.14), ctx)
        assert r[Sleeve.O3A].state is ScanState.ARMED


class TestOverrides:
    def test_paused_overrides_and_keeps_the_numbers(self) -> None:
        ctx = DayContext(sleeves={Sleeve.O1M: SleeveContext(paused=True)})
        r = run(QUIET, plus20(QUIET_MONTHLY, 10, 1), MONTHLY_SMILE, ctx)[Sleeve.O1M]
        assert r.state is ScanState.PAUSED
        assert Rejection.REJECTED_PAUSED.value in r.reasons
        assert r.numbers["er"] is not None
        assert r.candidates[0].rejection is Rejection.REJECTED_PAUSED

    def test_a_planned_session_shows_planned(self) -> None:
        ctx = DayContext(sleeves={Sleeve.O1M: SleeveContext(session_state=SessionState.CONFIRMED)})
        r = run(QUIET, plus20(QUIET_MONTHLY, 10, 5), MONTHLY_SMILE, ctx)[Sleeve.O1M]
        assert r.state is ScanState.PLANNED

    def test_no_chain_is_a_refusal_not_a_guess(self) -> None:
        m = QUIET
        ctx = DayContext()
        now = plus20(QUIET_MONTHLY, 10, 1)
        results = {r.sleeve: r for r in scan_all(m, ctx, now, SnapshotBook(), CFG, CEIL)}
        r = results[Sleeve.O1M]
        assert r.state is ScanState.WOULD_SKIP and r.reasons == ("REJECTED_NO_CHAIN",)
        assert r.stale is True and r.as_of_minute is None


class TestDecisionSnapshots:
    def test_wanted_minutes_names_each_decision_and_the_latest(self) -> None:
        wanted = wanted_minutes(GAP, DayContext(), plus20(GAP_HOLD, 10, 30), CFG, CEIL)
        assert at(GAP_HOLD, 9, 45) in wanted  # O3-B's plan time
        assert None in wanted  # O2 and O3-A are live
        # Before any sleeve prices anything (O1 observing, O2 building, O3-B's gap too small),
        # no snapshot is read at all.
        early = wanted_minutes(QUIET, DayContext(), plus20(QUIET_MONTHLY, 9, 20), CFG, CEIL)
        assert early == frozenset()

    def test_a_decided_candidate_does_not_move_with_later_premiums(self) -> None:
        now = plus20(QUIET_MONTHLY, 10, 10)
        a = run(QUIET, now, MONTHLY_SMILE)[Sleeve.O1M]
        # Premiums later in the morning are different; the verdict reads the 10:00 snapshot.
        book = SnapshotBook(
            {
                at(QUIET_MONTHLY, 10, 0): snapshot_at(
                    QUIET.bars, at(QUIET_MONTHLY, 10, 0), MONTHLY_SMILE
                ),
                None: snapshot_at(QUIET.bars, at(QUIET_MONTHLY, 10, 10), skew(0.5, 0.5)),
            }
        )
        b = {r.sleeve: r for r in scan_all(QUIET, DayContext(), now, book, CFG, CEIL)}[Sleeve.O1M]
        assert a.candidates == b.candidates

    def test_a_snapshot_minutes_old_is_stale(self) -> None:
        r = run(UP, plus20(O2_UP_BREAK, 9, 45), flat(0.13), latest_lag_minutes=3)[Sleeve.O2]
        assert r.stale is True
        fresh = run(UP, plus20(O2_UP_BREAK, 9, 45), flat(0.13))[Sleeve.O2]
        assert fresh.stale is False


class TestIdempotenceAndShape:
    def test_the_same_minute_gives_the_same_rows(self) -> None:
        now = plus20(TREND_WEEKLY, 10, 21)
        assert run(TREND, now, flat(0.14)) == run(TREND, now, flat(0.14))

    def test_the_row_minute_is_floored_ist(self) -> None:
        r = run(TREND, plus20(TREND_WEEKLY, 10, 21), flat(0.14))[Sleeve.O2]
        assert r.ts == dt.datetime(2026, 10, 20, 10, 21, tzinfo=IST)

    def test_candidates_json_has_no_floats(self) -> None:
        def walk(value: object) -> None:
            assert not isinstance(value, float), value
            if isinstance(value, dict):
                for v in value.values():
                    walk(v)
            if isinstance(value, list):
                for v in value:
                    walk(v)

        for day, now, smile in (
            (QUIET, plus20(QUIET_MONTHLY, 10, 1), MONTHLY_SMILE),
            (GAP, plus20(GAP_HOLD, 10, 30), flat(0.14)),
        ):
            for r in run(day, now, smile).values():
                walk(r.numbers)
                for c in r.candidates:
                    walk(to_json(c))


#: ``04`` §10's one-way order per sleeve: each state's rank may only rise within a day.
ORDER: dict[Sleeve, Callable[[ScanState], int]] = {
    s: {
        ScanState.OBSERVING: 0, ScanState.WOULD_TRADE: 1, ScanState.WOULD_SKIP: 1,
        ScanState.SLOT_TAKEN: 2, ScanState.PLANNED: 2, ScanState.DONE: 3,
    }.__getitem__
    for s in (Sleeve.O1M, Sleeve.O1W)
} | {
    s: {
        ScanState.BUILDING_RANGE: 0, ScanState.DAY_SKIPPED: 1, ScanState.ARMED: 1,
        ScanState.SLOT_TAKEN: 2, ScanState.TRIGGERED: 2, ScanState.WINDOW_CLOSED: 2,
    }.__getitem__
    for s in (Sleeve.O2, Sleeve.O3A, Sleeve.O3B)
}  # fmt: skip


@pytest.mark.parametrize(
    ("m", "smile"),
    [(QUIET, MONTHLY_SMILE), (TREND, flat(0.14)), (GAP, flat(0.14)), (UP, flat(0.13))],
    ids=["quiet-monthly", "trend-weekly", "gap-hold", "o2-up-break"],
)
def test_states_are_one_way_through_the_day(m: MarketDay, smile: Smile) -> None:
    ranks: dict[Sleeve, int] = {}
    minute = at(m.trade_date, 9, 15)
    end = at(m.trade_date, 15, 30)
    while minute <= end:
        for sleeve, r in run(m, minute + dt.timedelta(seconds=20), smile).items():
            if r.state is ScanState.NOT_TODAY:
                continue
            rank = ORDER[sleeve](r.state)
            assert rank >= ranks.get(sleeve, 0), (sleeve, minute.time(), r.state)
            ranks[sleeve] = rank
        minute += dt.timedelta(minutes=5)
