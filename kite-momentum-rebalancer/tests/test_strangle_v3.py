"""V3: the risk-off lever, the trail's widening and overrun, and the single re-entry.

The last test is the acceptance criterion: the Friday sequence from the source, reproduced
on synthetic data — enter, roll the winner down, abort on proximity, re-enter wider, trail
out green.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.strategies.strangle import adjust as A
from app.strategies.strangle import book as B
from app.strategies.strangle import config as SC
from app.strategies.strangle import journal as JN
from app.strategies.strangle import levels as L
from app.strategies.strangle import market as MK
from app.strategies.strangle import rules as R
from app.strategies.strangle import session as S

CFG = SC.load()
LOT, UNITS = 65, 1300
D = dt.date
START = dt.datetime(2026, 8, 24, 9, 45)
LEVELS = L.Levels(24_500, 23_900, "s", "s")
OI = L.OIStructure(24_400, 24_500, 23_900)


def hedged_book(stop=5.0, target=10.0):
    bk = B.Book(units_at_entry=UNITS, stop_points=stop, target_points=target,
                entry_credit=54.0 * UNITS)
    bk.add(B.Leg("C24800", "CE", 24_800, "SELL", UNITS, 27.0, "short_call"))
    bk.add(B.Leg("P24000", "PE", 24_000, "SELL", UNITS, 27.0, "short_put"))
    bk.add(B.Leg("C25300", "CE", 25_300, "BUY", UNITS, 8.0, "wing_call"))
    bk.add(B.Leg("P23500", "PE", 23_500, "BUY", UNITS, 8.0, "wing_put"))
    return bk


def rows_for(prices: dict, avg: dict | None = None):
    out = []
    for sym, (strike, kind, px) in prices.items():
        a = (avg or {}).get(sym, px * 2.0)
        out.append({"symbol": sym, "strike": float(strike), "kind": kind,
                    "bid": round(px - 0.1, 2), "ask": round(px + 0.1, 2), "last": px,
                    "oi": 9_000_000.0, "volume": 5000.0, "average_price": float(a),
                    "depth": {"buy": [{"price": round(px - 0.1, 2), "quantity": 500_000}],
                              "sell": [{"price": round(px + 0.1, 2), "quantity": 500_000}]}})
    return out


# =====================================================================================
# Lever B — risk-off
# =====================================================================================
def marks(call=10.0, put=30.0, wing=2.0):
    return {"C24800": call, "P24000": put, "C25300": wing, "P23500": wing}


def test_risk_off_does_not_arm_on_a_healthy_book():
    chk = A.check_risk_off(hedged_book(), marks=marks(), vwaps={}, now=START,
                           state={}, cfg=CFG)
    assert not chk.armed and "above the arming level" in chk.reason


def test_risk_off_arms_inside_its_window():
    """Between 60% and 85% of the stop. Below that, the stop governs, not an adjustment."""
    bk = hedged_book()
    m = {"C24800": 5.0, "P24000": 44.0, "C25300": 1.0, "P23500": 6.0}
    chk = A.check_risk_off(bk, marks=m, vwaps={}, now=START, state={}, cfg=CFG)
    assert -0.85 * bk.risk_budget < bk.session_pnl(m) <= -0.60 * bk.risk_budget
    assert chk.armed and chk.loser.symbol == "P24000"


def test_risk_off_stops_once_the_book_is_past_the_adjustment_floor():
    bk = hedged_book()
    m = {"C24800": 5.0, "P24000": 60.0, "C25300": 1.0, "P23500": 6.0}
    chk = A.check_risk_off(bk, marks=m, vwaps={}, now=START, state={}, cfg=CFG)
    assert not chk.armed and "the stop governs" in chk.reason


def test_risk_off_refuses_to_buy_back_a_leg_that_is_squeezing():
    """The VWAP rule, read as 'never cover mid-squeeze'. Paying up to close a short at its
    most expensive is the worst execution available."""
    bk = hedged_book()
    m = {"C24800": 5.0, "P24000": 44.0, "C25300": 1.0, "P23500": 6.0}
    chk = A.check_risk_off(bk, marks=m, vwaps={"P24000": 20.0}, now=START, state={},
                           cfg=CFG)
    assert not chk.armed and "squeeze" in chk.reason


def test_the_loser_roll_requires_a_materially_cheaper_strike():
    """Further out is measured in premium, not distance: a strike two steps away that still
    costs 90% as much has reduced nothing."""
    bk = hedged_book()
    # 28.0 is only 7% below the 30.0 loser: not the 25% reduction the lever requires.
    ch = rows_for({"P23800": (23_800, "PE", 28.0)})
    with pytest.raises(A.RollRefused, match="under"):
        A.plan_loser_roll(bk, loser=bk.open_legs[1], chain=ch, marks=marks(put=30.0),
                          cfg=CFG)


def test_the_loser_roll_moves_its_wing_too():
    bk = hedged_book()
    ch = rows_for({"P23800": (23_800, "PE", 20.0), "P23300": (23_300, "PE", 4.0)})
    plan = A.plan_loser_roll(bk, loser=bk.open_legs[1], chain=ch, marks=marks(put=30.0),
                             cfg=CFG)
    assert plan.open_short.strike == 23_800
    assert plan.close_wing.symbol == "P23500" and plan.open_wing.strike == 23_300


def test_the_refusal_reasons_are_counted_so_a_zero_can_be_explained():
    """Section 4.6 asks for the fire count AND the reasons behind a zero, because a lever
    that never fires needs its thresholds re-derived rather than widened until it does."""
    d = R.evaluate_risk_off(hedged_book(),
                            R.Snapshot(now=START, spot=24_400, asp=200.0, marks=marks()),
                            CFG, state={})
    assert d.action is R.Action.HOLD and d.code == "risk_off_idle" and d.reason


# =====================================================================================
# trail widening and target overrun
# =====================================================================================
def test_widening_uses_a_fraction_of_the_current_premium_not_an_absolute():
    """By the time a book is 80% to target its legs are worth a few rupees. An absolute
    floor of 8 could never be met, so the widening would be a silent no-op."""
    bk = hedged_book()
    ch = rows_for({"C24850": (24_850, "CE", 3.0), "C25350": (25_350, "CE", 0.5),
                   "P23950": (23_950, "PE", 3.0), "P23450": (23_450, "PE", 0.5)})
    plans = A.plan_widen(bk, chain=ch, marks=marks(call=4.0, put=4.0), cfg=CFG, step=50)
    assert {p.open_short.strike for p in plans} == {24_850, 23_950}
    for p in plans:
        assert abs(p.open_wing.strike - p.open_short.strike) == 500


def test_a_leg_whose_next_strike_is_too_cheap_is_left_alone():
    bk = hedged_book()
    ch = rows_for({"C24850": (24_850, "CE", 0.5), "C25350": (25_350, "CE", 0.2)})
    assert A.plan_widen(bk, chain=ch, marks=marks(call=4.0), cfg=CFG, step=50) == []


def test_the_target_is_raised_only_when_neither_leg_is_threatened():
    bk = hedged_book()
    m = {"C24800": 2.0, "P24000": 2.0, "C25300": 0.5, "P23500": 0.5}
    calm = R.Snapshot(now=START, spot=24_400, asp=200.0, marks=m,
                      vwaps={"C24800": 9.0, "P24000": 9.0})
    assert R.target_overrun(bk, calm, CFG) == pytest.approx(bk.target_cash * 1.5)
    hot = R.Snapshot(now=START, spot=24_400, asp=200.0, marks=m,
                     vwaps={"C24800": 1.0, "P24000": 9.0})
    assert R.target_overrun(bk, hot, CFG) is None


def test_the_stop_never_moves_when_the_target_does():
    bk = hedged_book()
    before = bk.risk_budget
    m = {"C24800": 2.0, "P24000": 2.0, "C25300": 0.5, "P23500": 0.5}
    R.target_overrun(bk, R.Snapshot(now=START, spot=24_400, asp=200.0, marks=m), CFG)
    assert bk.risk_budget == before


# =====================================================================================
# the single re-entry
# =====================================================================================
def snap_at(hour, minute=0, marks_=None):
    return R.Snapshot(now=dt.datetime(2026, 8, 24, hour, minute), spot=24_400,
                      asp=200.0, marks=marks_ or {})


def test_the_new_target_is_what_is_left_of_the_original():
    """A day cannot double its objective by aborting and starting again."""
    p = R.plan_reentry(hedged_book(), snap_at(11), CFG, exit_code="E4b",
                       reentries_used=0, banked_points=3.0, step=50)
    assert p.allowed and p.target_points == pytest.approx(7.0)
    assert p.stop_points == pytest.approx(3.5)      # structural 2:1, not a fixed 2


def test_a_re_entry_with_too_little_left_is_refused():
    """With 7 points booked only 3 remain, under the 4-point floor — not worth the risk."""
    p = R.plan_reentry(hedged_book(), snap_at(11), CFG, exit_code="E4b",
                       reentries_used=0, banked_points=7.0, step=50)
    assert not p.allowed and "floor" in p.reason


def test_an_overrun_book_cannot_produce_a_negative_target():
    """target_overrun can push the book past 1.5x the original, which made new_target
    negative and the behaviour undefined in an earlier draft."""
    bk = hedged_book()
    p = R.plan_reentry(bk, snap_at(11), CFG, exit_code="E4c", reentries_used=0,
                       banked_points=15.0, step=50)
    assert not p.allowed


def test_the_hard_stop_never_permits_a_re_entry():
    p = R.plan_reentry(hedged_book(), snap_at(11), CFG, exit_code="E1",
                       reentries_used=0, banked_points=3.0, step=50)
    assert not p.allowed and "does not permit" in p.reason


def test_only_one_re_entry_per_day():
    p = R.plan_reentry(hedged_book(), snap_at(11), CFG, exit_code="E4b",
                       reentries_used=1, banked_points=3.0, step=50)
    assert not p.allowed and "has been used" in p.reason


def test_no_re_entry_after_the_cutoff():
    p = R.plan_reentry(hedged_book(), snap_at(12, 45), CFG, exit_code="E4b",
                       reentries_used=0, banked_points=3.0, step=50)
    assert not p.allowed and "past 12:30" in p.reason


def test_no_re_entry_from_a_red_book():
    p = R.plan_reentry(hedged_book(), snap_at(11), CFG, exit_code="E4b",
                       reentries_used=0, banked_points=-1.0, step=50)
    assert not p.allowed and "not green" in p.reason


# =====================================================================================
# THE ACCEPTANCE CRITERION — the Friday sequence, end to end
# =====================================================================================
FRIDAY = {
    # Each phase needs its OWN at-the-money pair, or the priced range cannot be computed
    # at that spot. They must also be PRICED only in their own phase: an ATM call left at
    # 100 while spot is elsewhere is the nearest qualifying strike to roll into, and its
    # wing does not exist.
    "C23800": (23_800, "CE"), "P23800": (23_800, "PE"),
    "C24600": (24_600, "CE"), "P24600": (24_600, "PE"),
    "C24650": (24_650, "CE"), "C25150": (25_150, "CE"),
    "C24800": (24_800, "CE"), "C25300": (25_300, "CE"),
    "C25400": (25_400, "CE"), "C25900": (25_900, "CE"),
    "P24000": (24_000, "PE"), "P23500": (23_500, "PE"),
    "P23400": (23_400, "PE"), "P22900": (22_900, "PE"),
}


def friday_frame(minute: int, spot: float, px: dict, avg: dict | None = None):
    prices = {sym: (k[0], k[1], px.get(sym, 1.0)) for sym, k in FRIDAY.items()}
    return MK.ChainSnapshot(as_of=START + dt.timedelta(minutes=minute), spot=spot,
                            expiry=D(2026, 8, 25),
                            rows=tuple(rows_for(prices, avg)))


def test_the_friday_sequence_is_reproducible(tmp_path):
    """THE V3 ACCEPTANCE CRITERION. Enter, roll the winner down on a confirmed break,
    abort on proximity, re-enter wider, and finish green — the sequence the source
    described, and the one E4b and the single re-entry exist to make possible.

    The prices are chosen so each gate is cleared in turn rather than by luck: the book
    stays above the adjustment floor during the roll, ends the first position only a few
    points green so the re-entry has target left, and puts the rolled call inside
    0.60 x ASP of spot so the proximity abort is what fires.
    """
    bk = hedged_book()
    low_atm = {"C23800": 100.0, "P23800": 100.0}       # phase 1, spot 23,800
    high_atm = {"C24600": 100.0, "P24600": 100.0}      # phases 2-3, spot 24,600
    frames = []

    # 1. confirmed break below support, call winning, put losing: Lever A rolls C24800 down
    for m in range(0, 12):
        frames.append(friday_frame(m, 23_800, {
            "C24800": 20.0, "C24650": 36.0, "C25150": 5.0, "C25300": 9.0,
            "P24000": 34.0, "P23500": 9.0, **low_atm}))

    # 2. spot rips back to 24,600: the rolled 24,650 call is 50 points away, inside
    #    0.60 x ASP (120), so E4b aborts with the book a few points green.
    wider = {"C25400": 12.0, "C25900": 2.0, "P23400": 12.0, "P22900": 2.0}
    for m in range(12, 15):
        frames.append(friday_frame(m, 24_600, {
            "C24650": 42.0, "C25150": 13.0, "P24000": 30.0, "P23500": 6.0,
            "C24800": 55.0, "C25300": 18.0, **wider, **high_atm}))

    # 3. the wider re-entered legs decay to nothing: E5 books it
    for m in range(15, 45):
        frames.append(friday_frame(m, 24_600, {
            "C25400": 1.5, "C25900": 0.4, "P23400": 1.5, "P22900": 0.4,
            "C24650": 42.0, "C25150": 13.0, "C24800": 55.0, "C25300": 18.0,
            "P24000": 30.0, "P23500": 6.0, **high_atm}))

    jr = JN.Journal(str(tmp_path / "friday.jsonl"))
    events: list[tuple[str, dict]] = []
    rec = S.run_session(book=bk, provider=iter(frames), cfg=CFG, levels=LEVELS, oi=OI,
                        journal=jr, lot_size=LOT, step=50,
                        on_event=lambda k, p: events.append((k, p)))
    kinds = [k for k, _ in events]
    codes = [p["code"] for k, p in events if k == "exit_signal"]

    assert "rolled" in kinds, f"the winner was never rolled: {sorted(set(kinds))}"
    assert "E4b" in codes, f"no proximity abort; exits were {codes}"
    assert "reentered" in kinds, f"no re-entry; events were {sorted(set(kinds))}"
    assert rec["reentries_used"] == 1
    assert rec["adjustments"] <= int(CFG["management"]["max_adjustments_per_day"])
    assert rec["flat"], "the session did not end flat"
    assert rec["pnl"] > 0, f"the session did not finish green: {rec['pnl']}"
