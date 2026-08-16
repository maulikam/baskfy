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
    out, token = [], 1000
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
    obs = CAL.collect(kc, instruments=ins, index_key="NSE:NIFTY 50", lookback_days=60,
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
    short = CAL.collect(kc, instruments=ins, index_key="NSE:NIFTY 50", lookback_days=30,
                        step=50, today=D(2026, 8, 14), sleep=lambda _s: None)
    long_ = CAL.collect(kc, instruments=ins, index_key="NSE:NIFTY 50", lookback_days=365,
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
    snap = MK.snapshot(kc, instruments=ins, index_key="NSE:NIFTY 50",
                       expiry=D(2026, 8, 18), now=now)
    assert snap.stale_seconds == pytest.approx(30.0)


def test_the_atm_straddle_comes_from_mids_and_the_range_from_the_full_sum():
    kc = QuoteKC(dt.datetime(2026, 8, 17, 9, 29, 30))
    ins = instruments_for([D(2026, 8, 18)], [24_400])
    snap = MK.snapshot(kc, instruments=ins, index_key="NSE:NIFTY 50",
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
