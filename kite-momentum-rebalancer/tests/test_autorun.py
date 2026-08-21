"""What the day still needs collected, decided from the database and a clock.

Both daily jobs are blocked on a Kite token that expires overnight with no refresh. On
17-18 Aug 2026 both scheduled runs exited 2 for want of one, losing two sessions of
straddle observations and an EOD snapshot that cannot be backfilled. Logging in is the
moment that blocker clears, so it is the moment to catch up.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from ._frozen import options_lab

from app.analytics import autorun as AR, db

D, T = dt.date, dt.time
TODAY = D(2026, 8, 19)          # a Wednesday


@pytest.fixture()
def conn(tmp_path):
    with db.connect(str(tmp_path / "p.db")) as c:
        db.migrate(c)
        yield c


def at(h, m=0):
    return dt.datetime.combine(TODAY, T(h, m))


def record_daily(conn, steps, outcome="ok", day=TODAY):
    conn.execute(
        "INSERT INTO daily_runs(ran_at, session_date, trigger, outcome, failed_steps,"
        " steps_json, duration_s) VALUES(?,?,?,?,?,?,?)",
        (f"{day}T09:30:00", day.isoformat(), "page", outcome, 0, json.dumps(steps), 1.0))


FULL = [["index history", "ok", ""], ["benchmark PRI", "ok", ""],
        ["EOD snapshot", "ok", ""], ["trade capture", "ok", ""],
        ["breadth", "skipped", "no --scan given"], ["regime preview", "ok", ""]]
PRE_CLOSE = [[s[0], "skipped" if s[0] == "EOD snapshot" else s[1],
              "it is 09:30, before the 15:30 close" if s[0] == "EOD snapshot" else s[2]]
             for s in FULL]
ONLY_RUN = [["index history", "ok", ""]] + [
    [s[0], "skipped", "not selected by --only"] for s in FULL[1:]]


def ops(items):
    return [i["op"] for i in items]


# =====================================================================================
# holidays and weekends
# =====================================================================================
def test_nothing_is_due_on_a_non_trading_day(conn):
    assert AR.needed(conn, now=at(10), is_trading_day=False) == []


def test_before_the_options_open_the_straddle_is_not_due(conn):
    """There is no NFO pre-open session: option quotes do not exist before 09:15."""
    record_daily(conn, PRE_CLOSE)
    assert "strangle_collect" not in ops(AR.needed(conn, now=at(8, 45),
                                                   is_trading_day=True))


# =====================================================================================
# the daily collection
# =====================================================================================
def test_a_day_with_no_run_needs_one(conn):
    assert "daily" in ops(AR.needed(conn, now=at(10), is_trading_day=True))


def test_a_partial_only_run_does_not_count_as_collected(conn):
    """--only restricts the job to one step and still records outcome 'ok'. It fooled this
    check the first time it was tried against the live database."""
    record_daily(conn, ONLY_RUN)
    assert "daily" in ops(AR.needed(conn, now=at(10), is_trading_day=True))


def test_before_the_close_a_skipped_snapshot_still_counts(conn):
    """Nothing can be snapshotted before 15:30, so demanding one would re-run the job all
    morning to no purpose."""
    record_daily(conn, PRE_CLOSE)
    assert "daily" not in ops(AR.needed(conn, now=at(11), is_trading_day=True))


def test_after_the_close_a_missing_snapshot_is_outstanding(conn):
    """THE CASE THAT LOST MONDAY. The morning run legitimately skipped the snapshot, the
    18:30 job hit an expired token, and nothing noticed the gap."""
    record_daily(conn, PRE_CLOSE)
    items = AR.needed(conn, now=at(16), is_trading_day=True)
    assert "daily" in ops(items)
    assert "cannot be backfilled" in next(i for i in items if i["op"] == "daily")["why"]


def test_after_the_close_a_landed_snapshot_settles_the_day(conn):
    record_daily(conn, FULL)
    assert "daily" not in ops(AR.needed(conn, now=at(16), is_trading_day=True))


def test_a_failed_run_does_not_count(conn):
    record_daily(conn, FULL, outcome="auth")
    assert "daily" in ops(AR.needed(conn, now=at(16), is_trading_day=True))


def test_yesterdays_run_does_not_settle_today(conn):
    record_daily(conn, FULL, day=D(2026, 8, 18))
    assert "daily" in ops(AR.needed(conn, now=at(16), is_trading_day=True))


# =====================================================================================
# the straddle observation
# =====================================================================================
@options_lab
def test_the_straddle_is_due_when_none_was_recorded(conn, tmp_path):
    fwd = str(tmp_path / "fwd.jsonl")
    assert "strangle_collect" in ops(AR.needed(conn, now=at(10), is_trading_day=True,
                                               forward_path=fwd))


@options_lab
def test_an_observation_already_recorded_settles_it(conn, tmp_path):
    from app.strategies.strangle import calibrate as CAL
    fwd = str(tmp_path / "fwd.jsonl")
    CAL.record_forward(fwd, CAL.Observation(TODAY, D(2026, 8, 25), 4, "3+",
                                            24_400, 24_400, 150, 150))
    assert "strangle_collect" not in ops(AR.needed(conn, now=at(10), is_trading_day=True,
                                                   forward_path=fwd))


@options_lab
def test_a_late_observation_says_it_is_late(conn, tmp_path):
    """The bands are built on opening prints, so an afternoon reading sits further down
    the decay curve than the gate that will consume it. Recorded anyway — a late point
    beats a gap — but not silently."""
    fwd = str(tmp_path / "fwd.jsonl")
    late = next(i for i in AR.needed(conn, now=at(14), is_trading_day=True,
                                     forward_path=fwd) if i["op"] == "strangle_collect")
    assert "decay curve" in late["note"]


# =====================================================================================
# wiring
# =====================================================================================
def test_the_decision_module_runs_nothing():
    """It reads a database and a clock and returns reasons. scripts/autorun.py acts.

    Checked by IMPORTS, not substrings: the module docstring says it runs without a
    subprocess, and a substring test failed on its own explanation.
    """
    import ast
    tree = ast.parse(open("app/analytics/autorun.py").read())
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    # subprocess is the real prohibition: this module must not RUN anything.
    # os is permitted for exactly one thing — os.kill(pid, 0), which asks whether a
    # process exists and starts nothing. That probe is the only signal that crosses
    # process boundaries, and the alternative is starting a second session because this
    # module could not see the first.
    assert "subprocess" not in imported, sorted(imported)
    src = open("app/analytics/autorun.py").read()
    assert src.count("os.") == 1 and "os.kill" in src


def test_login_starts_the_collection():
    src = open("app/main.py").read()
    assert '_ops.start(conn, "autorun")' in src
    assert "logged_in=1" in src


def test_a_failed_autostart_never_costs_the_login():
    """The one thing the user came to the page to do."""
    src = open("app/main.py").read()
    i = src.index('_ops.start(conn, "autorun")')
    assert "except Exception" in src[i:i + 400]


def test_the_runner_places_no_orders():
    src = open("scripts/autorun.py").read()
    for token in ("place_order", "place_gtt", "modify_order", "cancel_order"):
        assert token not in src


# =====================================================================================
# the paper session — started on login, whenever that happens
# =====================================================================================
def sess_kwargs(tmp_path, **over):
    d = {"forward_path": str(tmp_path / "fwd.jsonl"),
         "session_journal": str(tmp_path / "sj.jsonl"),
         "lock_path": str(tmp_path / "s.lock")}
    d.update(over)
    return d


@options_lab
def test_a_session_is_started_inside_the_opening_window(conn, tmp_path):
    """The login time is not fixed, so the session starts whenever the login happens —
    provided the window is still open."""
    items = AR.needed(conn, now=at(9, 30), is_trading_day=True, **sess_kwargs(tmp_path))
    assert "strangle_session" in ops(items)
    s = next(i for i in items if i["op"] == "strangle_session")
    assert s["detached"] is True


def test_a_session_is_not_started_after_the_entry_window(conn, tmp_path):
    """A first entry at midday runs on parameters written for a full day of decay, against
    a range the session has already set. The runner refuses it; starting a process only to
    be refused is noise."""
    assert "strangle_session" not in ops(
        AR.needed(conn, now=at(11), is_trading_day=True, **sess_kwargs(tmp_path)))


def test_a_session_is_not_started_before_the_options_open(conn, tmp_path):
    assert "strangle_session" not in ops(
        AR.needed(conn, now=at(9, 0), is_trading_day=True, **sess_kwargs(tmp_path)))


def test_a_live_session_is_not_started_twice(conn, tmp_path):
    """Two sessions on one day would both enter and both journal, producing a paper record
    describing a position nobody held — and that record is what gates live trading."""
    import os
    lock = tmp_path / "s.lock"
    lock.write_text(f"{os.getpid()} now\n")
    assert "strangle_session" not in ops(
        AR.needed(conn, now=at(9, 30), is_trading_day=True,
                  **sess_kwargs(tmp_path, lock_path=str(lock))))


@options_lab
def test_a_stale_lock_does_not_block_a_session(conn, tmp_path):
    """A crash at 09:31 must not cost the day."""
    lock = tmp_path / "s.lock"
    lock.write_text("999999 dead\n")
    assert "strangle_session" in ops(
        AR.needed(conn, now=at(9, 30), is_trading_day=True,
                  **sess_kwargs(tmp_path, lock_path=str(lock))))


@options_lab
def test_a_session_that_already_had_its_say_is_not_restarted(conn, tmp_path):
    """Including one that vetoed. A day the strategy declined is a day it decided about,
    not a gap to fill."""
    from app.strategies.strangle import journal as J
    sj = tmp_path / "sj.jsonl"
    j = J.Journal(str(sj))
    j.write("skipped", vetoes=["gap 2%"])
    rows = sj.read_text().replace(j.read().__iter__().__next__()["ts"][:10],
                                  TODAY.isoformat())
    sj.write_text(rows)
    assert "strangle_session" not in ops(
        AR.needed(conn, now=at(9, 30), is_trading_day=True,
                  **sess_kwargs(tmp_path, session_journal=str(sj))))


def test_a_session_is_never_started_on_a_non_trading_day(conn, tmp_path):
    assert AR.needed(conn, now=at(9, 30), is_trading_day=False,
                     **sess_kwargs(tmp_path)) == []


def test_the_session_is_detached_so_it_cannot_hold_the_operations_lock():
    """It runs until 15:10. Waiting for it would block the equity daily job for five
    hours."""
    src = open("scripts/autorun.py").read()
    assert "start_new_session=True" in src
    assert 'item.get("detached")' in src


@options_lab
def test_the_runner_refuses_a_first_entry_after_the_window():
    """Never enforced until the start time became a human login."""
    src = open("scripts/strangle.py").read()
    assert "ENTRY_WINDOW_CLOSED" in src
    assert src.index("ENTRY_WINDOW_CLOSED") < src.index("SEL.select_pair"), \
        "the window must be checked before any strike is chosen"
