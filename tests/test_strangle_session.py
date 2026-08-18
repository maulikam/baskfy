"""The management loop, driven by scripted snapshots.

This is where the V2 acceptance criteria stop being assertions about functions and become
facts about a replayed session: the adjustment cap holds across a whole day, nothing fires
on an unconfirmed touch, and the attribution is reported per session.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.strategies.strangle import book as B
from app.strategies.strangle import config as SC
from app.strategies.strangle import journal as JN
from app.strategies.strangle import levels as L
from app.strategies.strangle import market as MK
from app.strategies.strangle import session as S

CFG = SC.load()
LOT, UNITS = 65, 1300
D = dt.date
START = dt.datetime(2026, 8, 24, 9, 45)

CONTRACTS = {
    "C23500": (23_500, "CE"), "C24000": (24_000, "CE"), "C24400": (24_400, "CE"),
    "C24650": (24_650, "CE"), "C24800": (24_800, "CE"), "C25150": (25_150, "CE"),
    "C25300": (25_300, "CE"),
    "P23500": (23_500, "PE"), "P24000": (24_000, "PE"), "P24400": (24_400, "PE"),
    "P24650": (24_650, "PE"), "P24800": (24_800, "PE"), "P25150": (25_150, "PE"),
    "P25300": (25_300, "PE"),
}
DEFAULT_PX = {"C24800": 10.0, "P24000": 30.0, "C25300": 2.0, "P23500": 2.0,
              "C24650": 30.0, "C25150": 5.0, "C24400": 100.0, "P24400": 100.0,
              "C24000": 200.0, "P24800": 200.0, "P24650": 60.0, "P25150": 300.0,
              "C23500": 900.0, "P25300": 900.0}
LEVELS = L.Levels(resistance=24_500, support=23_900, resistance_source="s",
                  support_source="s")
OI = L.OIStructure(pomc=24_400, call_wall=24_500, put_wall=23_900)


def frame(minutes: int, spot: float, px: dict | None = None, avg: dict | None = None,
          seconds: int = 0):
    prices = {**DEFAULT_PX, **(px or {})}
    rows = []
    for sym, (strike, kind) in CONTRACTS.items():
        p = prices[sym]
        rows.append({
            "symbol": sym, "strike": float(strike), "kind": kind,
            "bid": round(p - 0.1, 2), "ask": round(p + 0.1, 2), "last": p,
            "oi": 9_000_000.0, "volume": 5000.0,
            # Default well ABOVE the ask: a short is unthreatened when it trades below
            # its own session VWAP. Defaulting to the last price made every mark (the ask)
            # sit fractionally above its VWAP, so E4a fired on every frame.
            "average_price": float((avg or {}).get(sym, p * 2.0)),
            "depth": {"buy": [{"price": round(p - 0.1, 2), "quantity": 500_000}],
                      "sell": [{"price": round(p + 0.1, 2), "quantity": 500_000}]},
        })
    return MK.ChainSnapshot(as_of=START + dt.timedelta(minutes=minutes, seconds=seconds),
                            spot=spot, expiry=D(2026, 8, 25), rows=tuple(rows))


def hedged_book():
    bk = B.Book(units_at_entry=UNITS, stop_points=5.0, target_points=10.0,
                entry_credit=54.0 * UNITS)
    bk.add(B.Leg("C24800", "CE", 24_800, "SELL", UNITS, 27.0, "short_call"))
    bk.add(B.Leg("P24000", "PE", 24_000, "SELL", UNITS, 27.0, "short_put"))
    bk.add(B.Leg("C25300", "CE", 25_300, "BUY", UNITS, 8.0, "wing_call"))
    bk.add(B.Leg("P23500", "PE", 23_500, "BUY", UNITS, 8.0, "wing_put"))
    return bk


def run(frames, book=None, tmp_path=None):
    bk = book or hedged_book()
    jr = JN.Journal(str((tmp_path or dt) and (tmp_path / "j.jsonl")))
    events: list[tuple[str, dict]] = []
    rec = S.run_session(book=bk, provider=iter(frames), cfg=CFG, levels=LEVELS, oi=OI,
                        journal=jr, lot_size=LOT, step=50,
                        on_event=lambda k, p: events.append((k, p)))
    return bk, rec, events


# =====================================================================================
# exits run before adjustments, every tick
# =====================================================================================
def test_a_session_that_hits_the_stop_flattens_and_ends_the_day(tmp_path):
    frames = [frame(0, 24_400), frame(1, 24_100, {"P24000": 45.0})]
    bk, rec, events = run(frames, tmp_path=tmp_path)
    assert rec["exit_code"] == "E1" and rec["ends_day"] and rec["flat"]
    assert bk.is_flat and [k for k, _ in events if k == "flattened"]


def test_an_exitable_book_is_never_rolled_first(tmp_path):
    """A book past its stop is also wildly imbalanced. If adjustments were evaluated first
    it would pay to roll on the way out."""
    frames = [frame(0, 24_400), frame(1, 24_100, {"P24000": 45.0}), frame(2, 24_100)]
    _bk, rec, events = run(frames, tmp_path=tmp_path)
    assert rec["adjustments"] == 0
    assert [k for k, _ in events].index("exit_signal") < len(events)


def test_the_time_exit_closes_the_book(tmp_path):
    late = dt.datetime(2026, 8, 24, 15, 10)
    f = frame(0, 24_400)
    frames = [f, MK.ChainSnapshot(as_of=late, spot=f.spot, expiry=f.expiry, rows=f.rows)]
    _bk, rec, _ = run(frames, tmp_path=tmp_path)
    assert rec["exit_code"] == "E3" and rec["flat"]


def test_both_legs_above_vwap_exits_the_session(tmp_path):
    avg = {"C24800": 5.0, "P24000": 20.0}         # both marks above their own VWAP
    frames = [frame(0, 24_400, avg=avg)]
    _bk, rec, _ = run(frames, tmp_path=tmp_path)
    assert rec["exit_code"] == "E4a"


# =====================================================================================
# break confirmation across the loop
# =====================================================================================
def test_no_adjustment_fires_on_an_unconfirmed_touch(tmp_path):
    """The V2 acceptance criterion. Spot dips under support and closes back inside; the
    premium imbalance is there the whole time, and nothing rolls."""
    frames = [frame(0, 24_400), frame(1, 23_850), frame(2, 24_100),
              frame(3, 23_880), frame(4, 24_050)]
    _bk, rec, events = run(frames, tmp_path=tmp_path)
    assert rec["adjustments"] == 0
    assert not [k for k, _ in events if k == "rolled"]


def test_breaks_are_confirmed_on_minute_closes_not_on_ticks(tmp_path):
    """Five-second samples ten seconds apart are not two one-minute closes. Confirming off
    ticks would turn a touch into a break wearing a costume."""
    frames = [frame(0, 23_800, seconds=s) for s in (0, 5, 10, 15, 20)]
    _bk, rec, _ = run(frames, tmp_path=tmp_path)
    assert rec["adjustments"] == 0


def test_a_confirmed_break_rolls_the_winner_and_moves_its_wing(tmp_path):
    frames = [frame(0, 24_400), frame(1, 23_800), frame(2, 23_800), frame(3, 23_800)]
    bk, rec, events = run(frames, tmp_path=tmp_path)
    assert rec["adjustments"] == 1
    rolled = [p for k, p in events if k == "rolled"][0]
    assert rolled["close_short"] == "C24800" and rolled["open_short"] == "C24650"
    assert rolled["wing_opened"] == "C25150"
    open_syms = {l.symbol for l in bk.open_legs}
    assert open_syms == {"C24650", "P24000", "C25150", "P23500"}


# =====================================================================================
# the throttle, across a whole session
# =====================================================================================
def test_the_adjustment_cap_holds_across_a_full_session(tmp_path):
    """Sixty minutes of a confirmed, sustained break with a standing imbalance. The
    five-minute throttle and the daily cap are the only things stopping this rolling on
    every poll, which is how the strategy would die of costs rather than of risk."""
    frames = [frame(m, 23_800) for m in range(60)]
    _bk, rec, events = run(frames, tmp_path=tmp_path)
    assert rec["adjustments"] <= int(CFG["management"]["max_adjustments_per_day"])
    rolls = [p for k, p in events if k == "rolled"]
    assert len(rolls) == rec["adjustments"]


def test_rolls_are_at_least_five_minutes_apart(tmp_path):
    frames = [frame(m, 23_800) for m in range(60)]
    _bk, _rec, events = run(frames, tmp_path=tmp_path)
    times = [p["at"] for k, p in events if k == "rolled"]
    gaps = [(b - a).total_seconds() / 60.0 for a, b in zip(times, times[1:])]
    assert all(g >= float(CFG["management"]["min_adjustment_interval_minutes"])
               for g in gaps), gaps


def test_a_roll_that_cannot_be_completed_is_recorded_and_the_book_is_untouched(tmp_path):
    """No wing strike listed, so the roll is abandoned rather than half-applied."""
    frames = []
    for m in range(4):
        f = frame(m, 23_800)
        rows = tuple(r for r in f.rows if r["symbol"] != "C25150")
        frames.append(MK.ChainSnapshot(as_of=f.as_of, spot=f.spot, expiry=f.expiry,
                                       rows=rows))
    bk, rec, events = run(frames, tmp_path=tmp_path)
    assert rec["adjustments"] == 0
    assert [p for k, p in events if k == "roll_refused"]
    assert {l.symbol for l in bk.open_legs} == {"C24800", "P24000", "C25300", "P23500"}


# =====================================================================================
# the session record
# =====================================================================================
def test_attribution_is_reported_for_every_completed_session(tmp_path):
    frames = [frame(m, 23_800) for m in range(20)] + [
        MK.ChainSnapshot(as_of=dt.datetime(2026, 8, 24, 15, 10), spot=23_800,
                         expiry=D(2026, 8, 25), rows=frame(0, 23_800).rows)]
    _bk, rec, _ = run(frames, tmp_path=tmp_path)
    assert rec["flat"] and "attribution" in rec
    a = rec["attribution"]
    assert a["adjustments"] == rec["adjustments"]
    assert a["actual_pnl"] is not None


def test_a_provider_that_runs_dry_reports_unclosed_rather_than_a_trade(tmp_path):
    """Pretending an unfinished session ended flat is how a paper record acquires trades
    that never closed."""
    _bk, rec, _ = run([frame(0, 24_400)], tmp_path=tmp_path)
    assert rec["status"] == "UNCLOSED" and not rec["flat"]
    assert "did NOT end flat" in rec["note"]


def test_the_journal_can_reconstruct_what_happened(tmp_path):
    """Operational rule R7: the book must be reconstructable from the journal alone."""
    frames = [frame(m, 23_800) for m in range(20)]
    jr = JN.Journal(str(tmp_path / "j.jsonl"))
    S.run_session(book=hedged_book(), provider=iter(frames), cfg=CFG, levels=LEVELS,
                  oi=OI, journal=jr, lot_size=LOT, step=50)
    events = [r["event"] for r in jr.read()]
    assert "rolled" in events and "session_closed" in events


def test_a_leg_without_a_two_sided_quote_pauses_rather_than_valuing_it_at_zero(tmp_path):
    f = frame(0, 24_400)
    rows = tuple({**r, "ask": 0.0, "bid": 0.0} if r["symbol"] == "P24000" else r
                 for r in f.rows)
    blind = MK.ChainSnapshot(as_of=f.as_of, spot=f.spot, expiry=f.expiry, rows=rows)
    _bk, rec, events = run([blind, blind], tmp_path=tmp_path)
    assert rec["adjustments"] == 0
    assert [k for k, _ in events if k == "mark_missing"]


def test_the_trail_arms_and_then_governs_the_exit(tmp_path):
    """80% of a 10-point target on 1,300 units is 10,400; the trail then exits two points
    below the high-water mark rather than at a fixed floor."""
    # Shorts at 6.0 leave a 14,820 close cost, above the 10,530 premium-exhausted
    # threshold, so E5 does not pre-empt the trail. Book is +34,580 against a 10,400
    # arming level; giving back exactly two points then trips E2.
    rich = {"C24800": 6.0, "P24000": 6.0, "C25300": 0.5, "P23500": 0.5}
    frames = [frame(0, 24_400, rich), frame(1, 24_400, rich),
              frame(2, 24_400, {**rich, "C24800": 7.0, "P24000": 7.0})]
    _bk, rec, events = run(frames, tmp_path=tmp_path)
    assert [k for k, _ in events if k == "trail_armed"]
    assert rec["exit_code"] == "E2"


# =====================================================================================
# the frozen snapshot, after the book changes underneath it
# =====================================================================================
WIDEN_CONTRACTS = {**CONTRACTS,
                   "C24850": (24_850, "CE"), "C25350": (25_350, "CE"),
                   "P23950": (23_950, "PE"), "P23450": (23_450, "PE")}
WIDEN_PX = {**DEFAULT_PX, "C24850": 4.0, "C25350": 0.4,
            "P23950": 4.0, "P23450": 0.4}


def widen_frame(minutes: int, spot: float, px: dict | None = None):
    """Like frame(), but the chain carries the strike one step beyond each short — which
    is what plan_widen asks for. The shared fixture stops at the shorts themselves, so a
    widen could never be planned there and this path went unexercised."""
    prices = {**WIDEN_PX, **(px or {})}
    rows = []
    for sym, (strike, kind) in WIDEN_CONTRACTS.items():
        p = prices[sym]
        rows.append({
            "symbol": sym, "strike": float(strike), "kind": kind,
            "bid": round(p - 0.1, 2), "ask": round(p + 0.1, 2), "last": p,
            "oi": 9_000_000.0, "volume": 5000.0, "average_price": p * 2.0,
            "depth": {"buy": [{"price": round(p - 0.1, 2), "quantity": 500_000}],
                      "sell": [{"price": round(p + 0.1, 2), "quantity": 500_000}]},
        })
    return MK.ChainSnapshot(as_of=START + dt.timedelta(minutes=minutes), spot=spot,
                            expiry=D(2026, 8, 25), rows=tuple(rows))


def test_a_widen_does_not_kill_the_session_with_a_stale_snapshot(tmp_path):
    """R.Snapshot is frozen and carries a COPY of the marks. The widen replaces legs
    mid-iteration, and every rule consulted afterwards — target_overrun,
    evaluate_adjustment, evaluate_risk_off — was still handed the snapshot built before
    it. book.pnl deliberately refuses to value a book with a missing leg, so the very next
    call raised KeyError on the freshly rolled leg and the session died on the tick it
    started winning on.

    Found by driving a real BANKNIFTY chain through trail activation; unreachable in the
    shared fixture because its chain has no strike beyond the shorts to widen into.
    """
    rich = {"C24800": 6.0, "P24000": 6.0, "C25300": 0.5, "P23500": 0.5}
    frames = [widen_frame(0, 24_400, rich), widen_frame(1, 24_400, rich)]
    bk, rec, events = run(frames, tmp_path=tmp_path)          # must not raise

    kinds = [k for k, _ in events]
    assert "trail_armed" in kinds
    assert "widened" in kinds, "the widen path still did not fire; the test proves nothing"
    assert rec["status"] in ("OK", "UNCLOSED")


def test_every_rule_after_a_widen_sees_the_legs_the_widen_created(tmp_path):
    """The narrower guarantee behind the fix: once the book has changed, the marks handed
    to the remaining rules cover every open leg. A rule reading a stale copy would be
    valuing a position that no longer exists."""
    rich = {"C24800": 6.0, "P24000": 6.0, "C25300": 0.5, "P23500": 0.5}
    bk, _rec, events = run([widen_frame(0, 24_400, rich)], tmp_path=tmp_path)
    assert [k for k, _ in events if k == "widened"]
    snap = widen_frame(1, 24_400, rich)
    marks = S.marks_from(snap, bk)
    missing = {l.symbol for l in bk.open_legs} - set(marks)
    assert not missing, f"{missing} would raise inside book.pnl"
