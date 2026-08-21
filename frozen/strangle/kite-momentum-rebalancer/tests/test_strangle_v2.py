"""V2 acceptance criteria: the winner roll, its throttle, and what it contributed.

The two tests that matter most are the ones asserting a roll cannot half-happen. A short
whose wing did not move with it is still a condor to look at; it is simply a wider one,
with a different max loss and a different margin, and nothing else in the system notices.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.strategies.strangle import adjust as A
from app.strategies.strangle import attribution as AT
from app.strategies.strangle import book as B
from app.strategies.strangle import config as SC
from app.strategies.strangle import levels as L
from app.strategies.strangle import rules as R

CFG = SC.load()
LOT, UNITS = 65, 1300
NOW = dt.datetime(2026, 8, 24, 11, 0)


def hedged_book(call_k=24_800, put_k=24_000, width=500):
    bk = B.Book(units_at_entry=UNITS, stop_points=5.0, target_points=10.0,
                entry_credit=54.0 * UNITS)
    bk.add(B.Leg(f"C{call_k}", "CE", call_k, "SELL", UNITS, 27.0, "short_call"))
    bk.add(B.Leg(f"P{put_k}", "PE", put_k, "SELL", UNITS, 27.0, "short_put"))
    bk.add(B.Leg(f"C{call_k + width}", "CE", call_k + width, "BUY", UNITS, 8.0, "wing_call"))
    bk.add(B.Leg(f"P{put_k - width}", "PE", put_k - width, "BUY", UNITS, 8.0, "wing_put"))
    return bk


def chain(strikes_ce, strikes_pe, bid=lambda k: 10.0):
    rows = []
    for k in strikes_ce:
        rows.append({"strike": float(k), "kind": "CE", "bid": bid(k), "ask": bid(k) + 0.2,
                     "oi": 9_000_000, "symbol": f"C{int(k)}"})
    for k in strikes_pe:
        rows.append({"strike": float(k), "kind": "PE", "bid": bid(k), "ask": bid(k) + 0.2,
                     "oi": 9_000_000, "symbol": f"P{int(k)}"})
    return rows


LEVELS = L.Levels(resistance=24_500, support=23_900, resistance_source="s",
                  support_source="s")
OI = L.OIStructure(pomc=24_400, call_wall=24_500, put_wall=23_900)


# =====================================================================================
# break confirmation — never act on a touch
# =====================================================================================
def test_a_single_close_beyond_a_level_is_not_a_break():
    t = A.BreakTracker(needed=2)
    assert t.observe(24_600, resistance=24_500, support=23_900) is None


def test_two_consecutive_closes_confirm():
    t = A.BreakTracker(needed=2)
    t.observe(24_600, resistance=24_500, support=23_900)
    assert t.observe(24_620, resistance=24_500, support=23_900) == A.UP


def test_a_touch_that_closes_back_inside_never_confirms():
    """'Either it breaks and your stop hits, or it rejects and your loss never happened.'"""
    t = A.BreakTracker(needed=2)
    for close in (24_600, 24_450, 24_610, 24_400):
        assert t.observe(close, resistance=24_500, support=23_900) is None


def test_a_trending_day_needs_two_breaks_and_spot_still_beyond():
    t = A.BreakTracker(needed=2)
    for c in (24_600, 24_620):
        t.observe(c, resistance=24_500, support=23_900)
    assert not t.is_trending(A.UP, 24_620)                    # one break only
    t.observe(24_400, resistance=24_500, support=23_900)      # back inside
    t.observe(24_300, resistance=24_500, support=23_900)      # down break
    for c in (24_700, 24_750):
        t.observe(c, resistance=24_600, support=23_900)       # second up break
    assert t.is_trending(A.UP, 24_800)
    assert not t.is_trending(A.UP, 24_500), "spot fell back inside the more recent break"


# =====================================================================================
# the throttle — section 4.6
# =====================================================================================
def marks_for(call=10.0, put=30.0, wings=2.0):
    return {"C24800": call, "P24000": put, "C25300": wings, "P23500": wings}


def test_a_balanced_book_does_not_trigger():
    chk = A.check_rebalance(hedged_book(), marks=marks_for(call=25.0, put=27.0),
                            now=NOW, state={}, cfg=CFG)
    assert not chk.allowed and "under" in chk.reason


def test_the_imbalance_ratio_identifies_winner_and_loser_by_current_ask():
    chk = A.check_rebalance(hedged_book(), marks=marks_for(call=10.0, put=30.0),
                            now=NOW, state={}, cfg=CFG)
    assert chk.allowed and chk.ratio == pytest.approx(3.0)
    assert chk.winner.symbol == "C24800" and chk.loser.symbol == "P24000"


def test_the_daily_adjustment_cap_is_enforced():
    """The V2 acceptance criterion: never more than three in a session."""
    chk = A.check_rebalance(hedged_book(), marks=marks_for(), now=NOW,
                            state={"adjustments_today": 3}, cfg=CFG)
    assert not chk.allowed and "already made today" in chk.reason


def test_the_five_minute_throttle_is_enforced():
    """Without it the loop fires every tick and the strategy dies of costs, not of risk."""
    chk = A.check_rebalance(hedged_book(), marks=marks_for(), now=NOW,
                            state={"last_adjustment_at": NOW - dt.timedelta(minutes=2)},
                            cfg=CFG)
    assert not chk.allowed and "since the last adjustment" in chk.reason
    ok = A.check_rebalance(hedged_book(), marks=marks_for(), now=NOW,
                           state={"last_adjustment_at": NOW - dt.timedelta(minutes=6)},
                           cfg=CFG)
    assert ok.allowed


def test_a_book_near_its_stop_may_not_rebalance():
    bk = hedged_book()
    # call +28,600, put -55,900, call wing -9,100, put wing +28,600 => -7,800,
    # comfortably past the -5,525 floor (0.85 x a 6,500 risk budget).
    deep = {"C24800": 5.0, "P24000": 70.0, "C25300": 1.0, "P23500": 30.0}
    chk = A.check_rebalance(bk, marks=deep, now=NOW, state={}, cfg=CFG)
    assert not chk.allowed and "at or below" in chk.reason


def test_lever_b_is_reachable_inside_the_widened_window():
    """The gate was -0.70 in an earlier draft while Lever B arms at -0.60, leaving a
    0.5-point band at 20 lots that would essentially never be sampled. At -0.85 the window
    is about 1.25 points wide."""
    m = CFG["management"]
    floor = abs(float(m["rebalance_allowed_above_pct_of_stop"]))
    arm = float(m["risk_off"]["trigger_at_pct_of_stop"])
    window_points = (floor - arm) * 5.0
    assert floor > arm and window_points >= 1.0


# =====================================================================================
# the decision — exits first, then adjustments
# =====================================================================================
def snap(marks, vwaps=None, spot=24_400):
    return R.Snapshot(now=NOW, spot=spot, asp=200.0, marks=marks, vwaps=vwaps or {})


def test_no_adjustment_fires_without_a_confirmed_break():
    d = R.evaluate_adjustment(hedged_book(), snap(marks_for()), CFG, state={},
                              confirmed_break=None)
    assert d.action is R.Action.HOLD and d.code == "unconfirmed"


def test_a_confirmed_break_with_an_imbalance_rolls_the_winner():
    d = R.evaluate_adjustment(hedged_book(), snap(marks_for()), CFG, state={},
                              confirmed_break=A.DOWN)
    assert d.action is R.Action.ROLL_WINNER
    assert d.detail["winner"] == "C24800" and d.detail["loser"] == "P24000"


def test_the_winner_is_not_rolled_toward_spot_while_it_is_above_its_own_vwap():
    """Lever A adds risk on the winner's side. If that leg is already above its session
    VWAP it is under pressure too, and rolling it closer adds to a squeezing book."""
    vw = {"C24800": 8.0, "P24000": 40.0}          # call 10 > vwap 8
    d = R.evaluate_adjustment(hedged_book(), snap(marks_for(), vw), CFG, state={},
                              confirmed_break=A.DOWN)
    assert d.action is R.Action.HOLD and d.code == "vwap_veto"


def test_both_legs_above_vwap_exits_before_any_adjustment_is_considered():
    bk = hedged_book()
    vw = {"C24800": 8.0, "P24000": 25.0}
    s = snap(marks_for(call=10.0, put=30.0), vw)
    assert R.evaluate_exits(bk, s, CFG, state={}).code == "E4a"


def test_a_flat_book_is_never_adjusted():
    d = R.evaluate_adjustment(B.Book(), snap({}), CFG, state={}, confirmed_break=A.UP)
    assert d.action is R.Action.HOLD


# =====================================================================================
# the roll itself — the wing moves with the short
# =====================================================================================
def test_the_roll_moves_the_wing_by_exactly_the_same_width():
    """A short rolled 300 points with a static wing turns a 500-wide spread into an
    800-wide one: max loss and margin both change and the book still looks like a condor."""
    bk = hedged_book()
    ch = chain([24_600, 24_650, 25_100, 25_150], [24_000, 23_500],
               bid=lambda k: 30.0 if k in (24_600, 24_650) else 5.0)
    plan = A.plan_winner_roll(bk, winner=bk.open_legs[0], loser=bk.open_legs[1],
                              chain=ch, marks=marks_for(), spot=24_400, asp=200.0,
                              levels=LEVELS, oi=OI, cfg=CFG, step=50)
    assert plan.open_wing is not None
    assert plan.open_wing.strike - plan.open_short.strike == pytest.approx(500.0)


def test_a_roll_with_no_available_wing_is_abandoned_entirely():
    """Never 'fixed later' — in between, the book is short a naked option."""
    bk = hedged_book()
    ch = chain([24_600, 24_650], [24_000], bid=lambda k: 30.0)   # no wing strikes listed
    with pytest.raises(A.RollRefused, match="wing"):
        A.plan_winner_roll(bk, winner=bk.open_legs[0], loser=bk.open_legs[1], chain=ch,
                           marks=marks_for(), spot=24_400, asp=200.0, levels=LEVELS,
                           oi=OI, cfg=CFG, step=50)


def test_the_wing_invariant_rejects_a_widened_spread():
    with pytest.raises(A.RollRefused, match="wing width"):
        A.assert_wing_invariant(24_600, 25_200, 500.0, is_call=True)
    A.assert_wing_invariant(24_600, 25_100, 500.0, is_call=True)      # exact: fine


def test_the_book_invariant_catches_a_short_whose_wing_did_not_move():
    bk = hedged_book()
    bk.legs[0] = B.Leg("C24600", "CE", 24_600, "SELL", UNITS, 30.0, "short_call")
    with pytest.raises(A.RollRefused, match="wing width"):
        A.check_book_invariant(bk, CFG)


def test_boundaries_are_recomputed_against_live_spot_not_the_open():
    """Matching a threatened leg's premium needs a strike about as close to spot as the
    loser is, which is inside the 09:15 boundary. A static frame makes Lever A refuse to
    act in exactly the case it exists for."""
    bk = hedged_book()
    ch = chain([24_650, 25_150], [24_000, 23_500],
               bid=lambda k: 30.0 if k == 24_650 else 5.0)
    # Spot has fallen to 24,200, so the live upper boundary is 24,400 + a strike step.
    plan = A.plan_winner_roll(bk, winner=bk.open_legs[0], loser=bk.open_legs[1], chain=ch,
                              marks=marks_for(), spot=24_200, asp=200.0, levels=LEVELS,
                              oi=OI, cfg=CFG, step=50)
    assert plan.open_short.strike == 24_650


def test_a_roll_that_finds_no_qualifying_strike_does_nothing():
    bk = hedged_book()
    ch = chain([26_000], [24_000], bid=lambda k: 0.5)      # nothing pays the target
    with pytest.raises(A.RollRefused, match="no CE"):
        A.plan_winner_roll(bk, winner=bk.open_legs[0], loser=bk.open_legs[1], chain=ch,
                           marks=marks_for(), spot=24_400, asp=200.0, levels=LEVELS,
                           oi=OI, cfg=CFG, step=50)


def test_a_trending_day_raises_the_premium_target():
    bk = hedged_book()
    ch = chain([24_600, 24_650, 25_100, 25_150], [24_000, 23_500],
               bid=lambda k: 30.0 if k in (24_600, 24_650) else 5.0)
    calm = A.plan_winner_roll(bk, winner=bk.open_legs[0], loser=bk.open_legs[1], chain=ch,
                              marks=marks_for(), spot=24_400, asp=200.0, levels=LEVELS,
                              oi=OI, cfg=CFG, step=50, trending=False)
    hot = A.plan_winner_roll(bk, winner=bk.open_legs[0], loser=bk.open_legs[1], chain=ch,
                             marks=marks_for(), spot=24_400, asp=200.0, levels=LEVELS,
                             oi=OI, cfg=CFG, step=50, trending=True)
    assert hot.target_premium == pytest.approx(calm.target_premium * 1.05)


# =====================================================================================
# applying it
# =====================================================================================
class FakeFill:
    def __init__(self, price):
        self.avg_price, self.complete = price, True


def test_applying_a_roll_replaces_both_legs_and_keeps_four_open():
    bk = hedged_book()
    ch = chain([24_650, 25_150], [24_000, 23_500],
               bid=lambda k: 30.0 if k == 24_650 else 5.0)
    plan = A.plan_winner_roll(bk, winner=bk.open_legs[0], loser=bk.open_legs[1], chain=ch,
                              marks=marks_for(), spot=24_400, asp=200.0, levels=LEVELS,
                              oi=OI, cfg=CFG, step=50)
    fills = {"C24800": FakeFill(10.2), "C24650": FakeFill(30.0),
             "C25300": FakeFill(1.8), "C25150": FakeFill(5.0)}
    A.apply_roll(bk, plan, fills, CFG, lot_size=LOT)
    assert len(bk.open_legs) == 4
    assert {l.symbol for l in bk.open_legs} == {"C24650", "P24000", "C25150", "P23500"}
    A.check_book_invariant(bk, CFG)


def test_a_partially_fillable_roll_leaves_the_book_untouched():
    bk = hedged_book()
    before = [l.symbol for l in bk.open_legs]
    ch = chain([24_650, 25_150], [24_000, 23_500],
               bid=lambda k: 30.0 if k == 24_650 else 5.0)
    plan = A.plan_winner_roll(bk, winner=bk.open_legs[0], loser=bk.open_legs[1], chain=ch,
                              marks=marks_for(), spot=24_400, asp=200.0, levels=LEVELS,
                              oi=OI, cfg=CFG, step=50)
    with pytest.raises(A.RollRefused, match="not fillable"):
        A.apply_roll(bk, plan, {"C24800": FakeFill(10.2)}, CFG, lot_size=LOT)
    assert [l.symbol for l in bk.open_legs] == before


def test_the_rolled_leg_keeps_its_realised_pnl_in_the_book():
    """A closed leg does not stop counting. Book P&L is realised plus unrealised across
    every leg the session ever held."""
    bk = hedged_book()
    ch = chain([24_650, 25_150], [24_000, 23_500],
               bid=lambda k: 30.0 if k == 24_650 else 5.0)
    plan = A.plan_winner_roll(bk, winner=bk.open_legs[0], loser=bk.open_legs[1], chain=ch,
                              marks=marks_for(), spot=24_400, asp=200.0, levels=LEVELS,
                              oi=OI, cfg=CFG, step=50)
    A.apply_roll(bk, plan, {"C24800": FakeFill(10.0), "C24650": FakeFill(30.0),
                            "C25300": FakeFill(2.0), "C25150": FakeFill(5.0)},
                 CFG, lot_size=LOT)
    closed = [l for l in bk.closed_legs if l.symbol == "C24800"][0]
    assert closed.realised() == pytest.approx((27.0 - 10.0) * UNITS)


# =====================================================================================
# attribution — what rolling actually contributed
# =====================================================================================
def test_attribution_compares_against_the_never_rolled_book():
    bk = hedged_book()
    originals = list(bk.legs)
    ch = chain([24_650, 25_150], [24_000, 23_500],
               bid=lambda k: 30.0 if k == 24_650 else 5.0)
    plan = A.plan_winner_roll(bk, winner=bk.open_legs[0], loser=bk.open_legs[1], chain=ch,
                              marks=marks_for(), spot=24_400, asp=200.0, levels=LEVELS,
                              oi=OI, cfg=CFG, step=50)
    A.apply_roll(bk, plan, {"C24800": FakeFill(10.0), "C24650": FakeFill(30.0),
                            "C25300": FakeFill(2.0), "C25150": FakeFill(5.0)},
                 CFG, lot_size=LOT)
    marks = {"C24650": 20.0, "P24000": 20.0, "C25150": 3.0, "P23500": 3.0,
             "C24800": 6.0, "C25300": 1.0}
    a = AT.attribution(bk, originals, marks, LOT, adjustments=1)
    assert a["actual_pnl"] is not None and a["counterfactual_pnl"] is not None
    assert a["roll_contribution"] == pytest.approx(a["actual_pnl"] - a["counterfactual_pnl"])


def test_a_counterfactual_that_would_have_stopped_is_flagged_not_hidden():
    """Comparing against a book that would already have been closed overstates what the
    roll saved. The flag is the whole point of reporting it."""
    bk = hedged_book()
    originals = list(bk.legs)
    marks = {"C24800": 6.0, "P24000": 60.0, "C25300": 1.0, "P23500": 12.0}
    a = AT.attribution(bk, originals, marks, LOT, adjustments=1)
    assert a["counterfactual_would_have_stopped"] and not a["comparison_valid"]
    assert "overstatement" in a["note"]


def test_the_session_summary_excludes_invalid_comparisons():
    rows = [{"roll_contribution": 5000, "adjustments": 1, "comparison_valid": True},
            {"roll_contribution": -2000, "adjustments": 1, "comparison_valid": True},
            {"roll_contribution": 90000, "adjustments": 2, "comparison_valid": False}]
    s = AT.session_summary(rows)
    assert s["comparable"] == 2 and s["excluded_would_have_stopped"] == 1
    assert s["total_roll_contribution"] == pytest.approx(3000)
    assert s["positive_sessions"] == 1 and s["negative_sessions"] == 1


def test_the_counterfactual_needs_a_mark_for_every_original_leg():
    """It returns None rather than valuing the missing legs at zero. (book.pnl refuses
    outright for the same reason, which is why attribution is not asked to limp on.)"""
    bk = hedged_book()
    assert AT.counterfactual_pnl(list(bk.legs), {"C24800": 6.0}, LOT) is None
