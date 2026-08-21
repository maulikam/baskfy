"""Calibration, chain snapshot, state machine and journal.

The calibration tests carry the most weight: they pin down WHY the bands cannot be
reconstructed from history, so that a future attempt to "just widen the lookback" fails
loudly instead of producing a confident, wrong table.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from app.strategies.strangle import calibrate as CAL
from app.strategies.strangle import clock as C
from app.strategies.strangle import journal as JN
from app.strategies.strangle import market as MK
from app.strategies.strangle import state as ST

D = dt.date


# =====================================================================================
# calibration — the reconstruction limit, measured
# =====================================================================================
class FakeKC:
    """Enough of Kite to drive collect(): an index series and per-token option opens."""

    def __init__(self, spot_by_day, opens_by_token):
        self.spot_by_day, self.opens = spot_by_day, opens_by_token
        self.calls = 0

    def historical_data(self, token, start, end, interval, **kw):
        self.calls += 1
        src = self.spot_by_day if token == 256265 else self.opens.get(token, {})
        return [{"date": dt.datetime(d.year, d.month, d.day, 9, 15),
                 "open": v, "high": v, "low": v, "close": v}
                for d, v in sorted(src.items()) if start <= d <= end]


def instruments_for(expiries, strikes, lot=65):
    # The INDICES row is not decoration. collect() used to fall back to a hardcoded 256265
    # when it could not find one, so these fixtures passed WITHOUT ever supplying an index
    # — which is precisely the silent wrong-index path the fallback allowed in production.
    out = [{"name": "NIFTY 50", "segment": "INDICES", "tradingsymbol": "NIFTY 50",
            "instrument_token": 256265, "exchange": "NSE"}]
    token = 1000
    for e in expiries:
        for k in strikes:
            for kind in ("CE", "PE"):
                token += 1
                out.append({"name": "NIFTY", "segment": "NFO-OPT", "expiry": e,
                            "strike": float(k), "instrument_type": kind,
                            "lot_size": lot, "instrument_token": token,
                            "tradingsymbol": f"NIFTY{e:%y%m%d}{k}{kind}",
                            "exchange": "NFO"})
    return out


def test_only_a_contracts_final_week_is_trustworthy():
    """A past session's dte is defined against the series that GOVERNED it, and that
    series has expired and been delisted. Resolving against the live dump silently labels
    a three-week straddle as 'dte 3+'. The first live run produced 23 observations of which
    22 were mislabelled, with a median of 484 against a true 307."""
    expiry = D(2026, 8, 18)
    days = [D(2026, 7, 15), D(2026, 7, 22), D(2026, 8, 12), D(2026, 8, 13), D(2026, 8, 14)]
    spot = {d: 24_400.0 for d in days}
    ins = instruments_for([expiry], [24_400])
    opens = {}
    for i in ins:
        # A far-dated straddle is worth far more; that is exactly the contamination.
        opens[i["instrument_token"]] = {
            d: (400.0 if (expiry - d).days > 7 else 150.0) for d in days}
    kc = FakeKC(spot, opens)
    obs = CAL.collect(kc, instruments=ins, index_key="NSE:NIFTY 50", name="NIFTY", lookback_days=60,
                      step=50, today=D(2026, 8, 14), sleep=lambda _s: None)
    assert {o.session for o in obs} == {D(2026, 8, 12), D(2026, 8, 13), D(2026, 8, 14)}
    assert all(o.straddle == 300.0 for o in obs), "a pre-final-week session leaked in"


def test_a_wider_lookback_does_not_buy_more_history():
    """The limit is structural, not a parameter. Widening the window must not appear to
    help, or someone will widen it until the table looks full."""
    expiry = D(2026, 8, 18)
    days = [D(2026, 6, 1), D(2026, 7, 1), D(2026, 8, 13)]
    ins = instruments_for([expiry], [24_400])
    opens = {i["instrument_token"]: {d: 150.0 for d in days} for i in ins}
    kc = FakeKC({d: 24_400.0 for d in days}, opens)
    short = CAL.collect(kc, instruments=ins, index_key="NSE:NIFTY 50", name="NIFTY", lookback_days=30,
                        step=50, today=D(2026, 8, 14), sleep=lambda _s: None)
    long_ = CAL.collect(kc, instruments=ins, index_key="NSE:NIFTY 50", name="NIFTY", lookback_days=365,
                        step=50, today=D(2026, 8, 14), sleep=lambda _s: None)
    assert len(short) == len(long_) == 1


def test_bands_report_the_veto_rate_they_would_have_produced():
    """A band is not calibrated because it was computed. It is calibrated when you can see
    what share of your own history it would have refused to trade."""
    obs = [CAL.Observation(D(2026, 8, 10 + i), D(2026, 8, 18), 3, "3+", 24_400, 24_400,
                           c, c) for i, c in enumerate([100, 120, 140, 160, 500])]
    bands = CAL.build_bands(obs, min_samples=3)
    b = bands["3+"]
    assert b["n"] == 5 and b["sufficient"]
    assert b["low"] < b["median"] < b["high"]
    assert b["would_have_vetoed"] >= 1        # the 1000-point outlier
    assert b["effective_veto_below"] == pytest.approx(b["low"] * 0.85)


def test_a_thin_bucket_is_marked_not_ready():
    obs = [CAL.Observation(D(2026, 8, 14), D(2026, 8, 18), 2, "2", 24_400, 24_400, 100, 100)]
    r = CAL.readiness(CAL.build_bands(obs, min_samples=8))
    assert not r["ready"] and "1" in r["missing_buckets"] and "2" in r["thin_buckets"]


def test_percentile_interpolates_and_survives_a_single_sample():
    assert CAL.percentile([10], 0.25) == 10
    assert CAL.percentile([0, 10], 0.5) == pytest.approx(5.0)


# =====================================================================================
# forward collection — the only sound path
# =====================================================================================
def test_the_forward_record_keeps_the_first_write_for_a_session(tmp_path):
    """First, not last: the first write is the one taken at entry time, which is what the
    gate compares against. A re-run later in the day is a different point on the decay
    curve and must not overwrite it."""
    path = str(tmp_path / "fwd.jsonl")
    first = CAL.Observation(D(2026, 8, 19), D(2026, 8, 25), 4, "3+", 24_400, 24_400, 150, 150)
    later = CAL.Observation(D(2026, 8, 19), D(2026, 8, 25), 4, "3+", 24_400, 24_400, 90, 90)
    CAL.record_forward(path, first)
    CAL.record_forward(path, later)
    rows = CAL.load_forward(path)
    assert len(rows) == 1 and rows[0].straddle == 300.0


def test_the_forward_record_is_append_only(tmp_path):
    path = str(tmp_path / "fwd.jsonl")
    for i in range(3):
        CAL.record_forward(path, CAL.Observation(D(2026, 8, 19 + i), D(2026, 8, 25),
                                                 4 - i, "3+", 24_400, 24_400, 150, 150))
    assert len(open(path).read().strip().splitlines()) == 3
    assert len(CAL.load_forward(path)) == 3


def test_a_corrupt_line_does_not_destroy_the_record(tmp_path):
    path = str(tmp_path / "fwd.jsonl")
    CAL.record_forward(path, CAL.Observation(D(2026, 8, 19), D(2026, 8, 25), 4, "3+",
                                             24_400, 24_400, 150, 150))
    with open(path, "a") as fh:
        fh.write("{not json\n")
    assert len(CAL.load_forward(path)) == 1


# =====================================================================================
# chain snapshot
# =====================================================================================
class QuoteKC:
    def __init__(self, last_trade_time):
        self.ltt = last_trade_time

    def quote(self, keys):
        if keys == ["NSE:NIFTY 50"]:
            return {"NSE:NIFTY 50": {"last_price": 24_400.0}}
        out = {}
        for k in keys:
            out[k] = {"last_price": 100.0, "oi": 9_000_000, "volume": 1000,
                      "last_trade_time": self.ltt,
                      "depth": {"buy": [{"price": 99.9, "quantity": 5000}],
                                "sell": [{"price": 100.1, "quantity": 5000}]}}
        return out


def test_a_naive_last_trade_time_does_not_raise_or_shift_staleness_by_the_offset():
    """Kite returns last_trade_time naive. Mixing it with an aware `now` raises; coercing
    the wrong way shifts staleness by 5.5 hours, which would mark every quote stale and
    veto every session."""
    now = dt.datetime(2026, 8, 17, 9, 30, 0)
    kc = QuoteKC(dt.datetime(2026, 8, 17, 9, 29, 30))
    ins = instruments_for([D(2026, 8, 18)], [24_400])
    snap = MK.snapshot(kc, instruments=ins, index_key="NSE:NIFTY 50", name="NIFTY",
                       expiry=D(2026, 8, 18), now=now)
    assert snap.stale_seconds == pytest.approx(30.0)


def test_the_atm_straddle_comes_from_mids_and_the_range_from_the_full_sum():
    kc = QuoteKC(dt.datetime(2026, 8, 17, 9, 29, 30))
    ins = instruments_for([D(2026, 8, 18)], [24_400])
    snap = MK.snapshot(kc, instruments=ins, index_key="NSE:NIFTY 50", name="NIFTY",
                       expiry=D(2026, 8, 18))
    asp, k = MK.atm_straddle(snap, 50)
    assert k == 24_400 and asp == pytest.approx(200.0)     # two legs at mid 100


def test_a_chain_with_no_two_sided_quotes_is_not_complete():
    snap = MK.ChainSnapshot(as_of=dt.datetime(2026, 8, 17, 9, 0), spot=24_400,
                            expiry=D(2026, 8, 18),
                            rows=({"bid": 0.0, "ask": 0.0},))
    assert not snap.complete


# =====================================================================================
# state machine and lockout
# =====================================================================================
def test_the_happy_path_walks_to_locked_out(tmp_path):
    s = ST.Session(D(2026, 8, 24), str(tmp_path / "lock.json"))
    for nxt in (ST.State.GATED, ST.State.WAITING_ENTRY, ST.State.MANAGING,
                ST.State.EXITING, ST.State.LOCKED_OUT):
        s.to(nxt, "step")
    assert s.is_terminal


def test_a_restart_cannot_resume_a_day_that_hit_its_stop(tmp_path):
    """This is how one bad session becomes three."""
    path = str(tmp_path / "lock.json")
    s = ST.Session(D(2026, 8, 24), path)
    s.to(ST.State.GATED, ""); s.to(ST.State.WAITING_ENTRY, "")
    s.to(ST.State.MANAGING, ""); s.to(ST.State.EXITING, "E1"); s.to(ST.State.LOCKED_OUT, "E1")
    assert ST.Session.locked_out(D(2026, 8, 24), path)["reason"] == "E1"


def test_yesterdays_lockout_does_not_lock_today(tmp_path):
    """A stale lockout that is never cleared would refuse to trade forever."""
    path = str(tmp_path / "lock.json")
    s = ST.Session(D(2026, 8, 24), path)
    s.to(ST.State.GATED, ""); s.to(ST.State.SKIPPED, "veto")
    assert ST.Session.locked_out(D(2026, 8, 25), path) is None


def test_an_illegal_transition_is_refused_rather_than_drifting(tmp_path):
    s = ST.Session(D(2026, 8, 24), str(tmp_path / "lock.json"))
    with pytest.raises(ST.IllegalTransition):
        s.to(ST.State.MANAGING, "skipping the gates")


def test_emergency_exit_is_reachable_from_every_live_state(tmp_path):
    for start in (ST.State.PREOPEN, ST.State.GATED, ST.State.WAITING_ENTRY,
                  ST.State.MANAGING, ST.State.ADJUSTING):
        assert ST.State.EMERGENCY_EXIT in ST._ALLOWED[start], start


def test_a_corrupt_lockout_file_does_not_block_the_day(tmp_path):
    path = tmp_path / "lock.json"
    path.write_text("{broken")
    assert ST.Session.locked_out(D(2026, 8, 24), str(path)) is None


# =====================================================================================
# journal
# =====================================================================================
def test_the_journal_appends_and_never_rewrites(tmp_path):
    j = JN.Journal(str(tmp_path / "j.jsonl"))
    j.write("entered", pair="A")
    j.write("exited", code="E1")
    rows = list(j.read())
    assert [r["event"] for r in rows] == ["entered", "exited"]
    assert len(open(j.path).read().strip().splitlines()) == 2


def test_a_corrupt_journal_line_is_skipped_not_fatal(tmp_path):
    j = JN.Journal(str(tmp_path / "j.jsonl"))
    j.write("entered")
    with open(j.path, "a") as fh:
        fh.write("garbage\n")
    j.write("exited")
    assert [r["event"] for r in j.read()] == ["entered", "exited"]


def test_the_journal_path_comes_from_config_so_tests_never_touch_the_real_one():
    from app.strategies.strangle import config as SC
    assert SC.load()["operational"]["journal_path"].startswith("data/outputs/")


# =====================================================================================
# the runner cannot trade
# =====================================================================================
def test_the_runner_places_no_orders():
    src = open("scripts/strangle.py").read()
    for token in ("place_order", "place_gtt", "modify_order", "cancel_order"):
        assert token not in src
    assert "simulate_basket" in src


def test_the_runner_refuses_a_non_trading_day():
    src = open("scripts/strangle.py").read()
    assert "NOT_A_TRADING_DAY" in src and "is_trading_day" in src


# =====================================================================================
# the NSE calendar — weekly Tuesdays, monthly last-Tuesdays, and the holiday shift
# =====================================================================================
def index_rows(days):
    return [{"date": dt.datetime(d.year, d.month, d.day, 15, 30), "close": 24_400.0}
            for d in days]


def weekdays(start, end):
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def test_monthly_expiries_are_the_last_tuesday_of_their_month():
    """Verified against the live dump: 13 of 13 monthlies matched."""
    from app.strategies.strangle import calendar_nse as CAL
    assert CAL.last_weekday_of_month(2026, 8) == D(2026, 8, 25)
    assert CAL.last_weekday_of_month(2026, 9) == D(2026, 9, 29)
    assert CAL.last_weekday_of_month(2031, 6) == D(2031, 6, 24)


def test_a_missing_weekday_candle_is_a_holiday():
    """The exchange's own record of what happened, not a list somebody typed."""
    from app.strategies.strangle import calendar_nse as CAL
    days = weekdays(D(2026, 9, 28), D(2026, 10, 9))
    days.remove(D(2026, 10, 2))                      # Gandhi Jayanti, a Friday
    cal = CAL.Calendar.build(index_rows=index_rows(days))
    assert D(2026, 10, 2) in cal.holidays
    assert not cal.is_trading_day(D(2026, 10, 2))
    assert cal.is_trading_day(D(2026, 10, 1))


def test_a_shifted_expiry_reveals_a_future_holiday():
    """The only forward-looking holiday signal Kite carries. 2029-12-24 is a Monday in the
    live dump because 25 December is Christmas."""
    from app.strategies.strangle import calendar_nse as CAL
    cal = CAL.Calendar.build(expiries=[D(2029, 12, 24), D(2029, 12, 31)])
    assert D(2029, 12, 25) in cal.holidays


def test_a_legitimately_shifted_expiry_is_accepted():
    """Without the calendar this refused startup, which is the assertion misfiring rather
    than catching anything."""
    from app.strategies.strangle import calendar_nse as CAL
    cal = CAL.Calendar.build(expiries=[D(2029, 12, 24)])
    ok, why = cal.expiry_is_valid(D(2029, 12, 24))
    assert ok and "holiday" in why


def test_an_unjustified_shift_is_still_refused():
    from app.strategies.strangle import calendar_nse as CAL
    cal = CAL.Calendar.build()
    ok, why = cal.expiry_is_valid(D(2026, 8, 17))       # a Monday, Tuesday trades fine
    assert not ok and "trading day" in why


def test_dte_skips_holidays():
    """Counting a holiday as a session puts the day in the wrong target and size bucket."""
    from app.strategies.strangle import calendar_nse as CAL
    cal = CAL.Calendar.build(extra=[D(2026, 10, 2)])
    assert cal.trading_days_between(D(2026, 9, 30), D(2026, 10, 6)) == 3
    assert C.trading_days_between(D(2026, 9, 30), D(2026, 10, 6)) == 4   # blind


def test_the_previous_trading_day_walks_back_over_a_holiday():
    from app.strategies.strangle import calendar_nse as CAL
    cal = CAL.Calendar.build(extra=[D(2026, 10, 2)])
    assert cal.previous_trading_day(D(2026, 10, 5)) == D(2026, 10, 1)


def test_the_calendar_reports_how_far_it_actually_reaches():
    """A holiday that shifts no expiry leaves no trace in any Kite endpoint. The gap is
    reported rather than assumed away."""
    from app.strategies.strangle import calendar_nse as CAL
    cal = CAL.Calendar.build(index_rows=index_rows(weekdays(D(2026, 8, 3), D(2026, 8, 14))),
                             expiries=[D(2029, 12, 24)])
    cov = cal.coverage(D(2026, 8, 17))
    assert cov["exact_through"] == D(2026, 8, 14)
    assert cov["inferred_forward"] == [D(2029, 12, 25)]
    assert "leaves no trace" in cov["note"]


def test_the_runner_uses_the_calendar_not_the_weekday():
    src = open("scripts/strangle.py").read()
    assert "cal.is_trading_day(today)" in src
    assert "holidays=cal.holidays" in src


# =====================================================================================
# scheduling
# =====================================================================================
def test_a_second_session_process_is_refused(tmp_path):
    """The /options button and the launchd job cannot see each other's in-process locks.
    Two sessions on one day would both enter and both journal, producing a paper record
    describing a position nobody held — and that record is what gates live trading."""
    import scripts.strangle as SR
    lock = str(tmp_path / "s.lock")
    assert SR._acquire_session_lock(lock)
    assert not SR._acquire_session_lock(lock)
    SR._release_session_lock(lock)
    assert SR._acquire_session_lock(lock)
    SR._release_session_lock(lock)


def test_a_stale_lock_from_a_dead_process_is_reclaimed(tmp_path):
    """A crash at 09:31 must not cost the whole day."""
    import scripts.strangle as SR
    lock = tmp_path / "s.lock"
    lock.write_text("999999 2026-08-17T09:31:00\n")      # a pid that does not exist
    assert SR._acquire_session_lock(str(lock))
    SR._release_session_lock(str(lock))


def test_a_corrupt_lock_file_does_not_block_the_day(tmp_path):
    import scripts.strangle as SR
    lock = tmp_path / "s.lock"
    lock.write_text("not a pid\n")
    assert SR._acquire_session_lock(str(lock))
    SR._release_session_lock(str(lock))


def test_agent_logs_live_outside_the_tcc_protected_repo():
    """macOS protects ~/Documents. A launchd agent has no grant for a log file it did not
    create there, so it cannot open it, never spawns the process, and exits 78 with no
    output at all — a failure that looks like nothing happening.

    Measured, not theorised: the first install of this job did exactly that. The older
    daily job kept working only because its log file carries a com.apple.macl grant from an
    approval given long ago, which is a grant that vanishes if the file is ever deleted.
    """
    import pathlib
    import plistlib
    templates = sorted(pathlib.Path("scripts").glob("com.*.plist.example"))
    assert len(templates) >= 3, "expected the daily job and both strangle jobs"
    for f in templates:
        with open(f, "rb") as fh:
            pl = plistlib.load(fh)
        for key in ("StandardOutPath", "StandardErrorPath"):
            path = pl[key]
            assert "/Library/Logs/" in path, f"{f.name}.{key} -> {path}"
            assert "/Documents/" not in path, f"{f.name}.{key} is in a TCC-protected tree"


def test_both_plists_are_valid_and_point_at_this_checkout():
    import os
    import plistlib
    here = os.getcwd()
    for name, args in (("collect", ["-m", "scripts.strangle", "--collect"]),
                       ("session", ["-m", "scripts.strangle"])):
        path = f"scripts/com.strangle.{name}.plist.example"
        with open(path, "rb") as fh:
            pl = plistlib.load(fh)
        assert pl["Label"] == f"com.strangle.{name}"
        assert pl["ProgramArguments"][1:] == args, name
        assert pl["WorkingDirectory"] == here, name
        assert pl["RunAtLoad"] is False, "a scheduled job must not fire on load"


def test_the_scheduled_jobs_run_inside_market_hours():
    """09:20 and 09:30 IST. There is no NFO pre-open session, so option quotes do not
    exist before 09:15 and an earlier run would find an empty chain."""
    import plistlib
    for name, want in (("collect", (9, 20)), ("session", (9, 30))):
        with open(f"scripts/com.strangle.{name}.plist.example", "rb") as fh:
            pl = plistlib.load(fh)
        slots = pl["StartCalendarInterval"]
        assert len(slots) == 5, "weekdays only"
        assert {(s["Hour"], s["Minute"]) for s in slots} == {want}
        assert {s["Weekday"] for s in slots} == {1, 2, 3, 4, 5}


def test_the_session_job_is_never_restarted_automatically():
    """A session that died mid-book must be looked at, not relaunched into a position it
    has forgotten about."""
    import plistlib
    with open("scripts/com.strangle.session.plist.example", "rb") as fh:
        assert plistlib.load(fh)["KeepAlive"] is False
