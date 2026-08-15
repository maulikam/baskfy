"""Collection health: a failed daily job must be visible without reading a log."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from app import config as C
from app import main as M
from app.analytics import daily_runs as DR
from app.analytics import db

OK, SKIP, FAIL = DR.OK, DR.SKIP, DR.FAIL
D = dt.date


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "p.db"))
    with db.connect(str(tmp_path / "p.db")) as c:
        db.migrate(c)
        yield c


def steps(*pairs):
    return [(n, s, "") for n, s in pairs]


# =====================================================================================
# classification
# =====================================================================================
def test_every_step_landing_is_ok(conn):
    assert DR.classify(steps(("a", OK), ("b", OK))) == DR.FINE


def test_a_legitimately_skipped_step_does_not_spoil_the_run(conn):
    """breadth skips when no scan was given. That is by design, not a failure."""
    assert DR.classify(steps(("a", OK), ("breadth", SKIP))) == DR.FINE


def test_some_failures_is_partial(conn):
    assert DR.classify(steps(("a", OK), ("b", FAIL))) == DR.PARTIAL


def test_all_failures_is_failed(conn):
    assert DR.classify(steps(("a", FAIL), ("b", FAIL))) == DR.FAILED


def test_a_run_where_nothing_executed_is_failed(conn):
    assert DR.classify(steps(("a", SKIP), ("b", SKIP))) == DR.FAILED


# =====================================================================================
# status — what the interface says
# =====================================================================================
def test_before_any_run_the_status_says_so(conn):
    s = DR.status(conn)
    assert s["ever_ran"] is False and s["healthy"] is False
    assert "never run" in s["message"]


def test_a_clean_run_today_is_healthy(conn):
    DR.record(conn, steps=steps(("a", OK)), trigger="schedule", session_date=D.today())
    s = DR.status(conn)
    assert s["healthy"] is True and s["outcome"] == DR.FINE
    assert s["consecutive_failures"] == 0


def test_an_auth_failure_names_the_cause_and_is_not_healthy(conn):
    DR.record(conn, steps=steps(("a", FAIL)), trigger="schedule",
              session_date=D.today(), outcome=DR.AUTH)
    s = DR.status(conn)
    assert s["healthy"] is False and s["outcome"] == DR.AUTH
    assert "log in" in s["message"].lower()


def test_consecutive_failures_are_counted(conn):
    for _ in range(3):
        DR.record(conn, steps=steps(("a", FAIL)), trigger="schedule",
                  session_date=D.today(), outcome=DR.AUTH)
    assert DR.status(conn)["consecutive_failures"] == 3


def test_a_success_resets_the_streak(conn):
    DR.record(conn, steps=steps(("a", FAIL)), trigger="schedule",
              session_date=D.today(), outcome=DR.AUTH)
    DR.record(conn, steps=steps(("a", OK)), trigger="schedule", session_date=D.today())
    assert DR.status(conn)["consecutive_failures"] == 0


def test_a_failed_run_does_not_reset_the_staleness_clock(conn):
    """A run that collected nothing is not a collection. Counting it as one would hide a
    growing hole in the history behind a recent timestamp."""
    DR.record(conn, steps=steps(("a", OK)), trigger="schedule", session_date=D(2026, 8, 1))
    conn.execute("UPDATE daily_runs SET ran_at='2026-08-01T18:30:00'")
    DR.record(conn, steps=steps(("a", FAIL)), trigger="schedule",
              session_date=D(2026, 8, 15), outcome=DR.AUTH)
    s = DR.status(conn, today=D(2026, 8, 15))
    assert s["stale_days"] == 14 and s["healthy"] is False


def test_a_partial_run_still_counts_as_collected(conn):
    DR.record(conn, steps=steps(("a", OK), ("b", FAIL)), trigger="schedule",
              session_date=D.today())
    s = DR.status(conn)
    assert s["outcome"] == DR.PARTIAL and s["stale_days"] == 0
    assert s["healthy"] is True          # something landed today
    assert "b" in s["message"]           # but it names what broke


def test_the_failing_step_is_named(conn):
    DR.record(conn, steps=[("index history", OK, ""), ("EOD snapshot", FAIL, "boom")],
              trigger="schedule", session_date=D.today())
    assert "EOD snapshot" in DR.status(conn)["message"]


def test_the_trigger_is_recorded(conn):
    DR.record(conn, steps=steps(("a", OK)), trigger="schedule", session_date=D.today())
    assert DR.status(conn)["trigger"] == "schedule"


def test_steps_survive_a_round_trip(conn):
    DR.record(conn, steps=[("a", OK, "wrote 3 rows")], trigger="cli",
              session_date=D.today())
    assert json.loads(DR.history(conn)[0]["steps_json"])[0][2] == "wrote 3 rows"


# =====================================================================================
# the interface
# =====================================================================================
def test_the_desk_warns_when_the_collection_is_broken(conn):
    DR.record(conn, steps=steps(("a", FAIL)), trigger="schedule",
              session_date=D.today(), outcome=DR.AUTH)
    page = TestClient(M.app).get("/").text
    assert "last run failed" in page.lower()
    assert "could not log in" in page.lower()


def test_the_desk_stays_quiet_when_collection_is_healthy(conn):
    """An always-present status line stops being read. This one has to be believed on
    the day it turns red."""
    DR.record(conn, steps=steps(("a", OK)), trigger="schedule", session_date=D.today())
    page = TestClient(M.app).get("/").text
    assert "last run failed" not in page.lower()
    assert "collection idle" not in page.lower()


def test_the_desk_flags_a_collection_that_never_ran(conn):
    page = TestClient(M.app).get("/").text
    assert "collection idle" in page.lower()


def test_ops_shows_the_collection_history_including_scheduled_runs(conn):
    DR.record(conn, steps=[("EOD snapshot", FAIL, "boom")], trigger="schedule",
              session_date=D.today())
    page = TestClient(M.app).get("/ops").text
    assert "Daily collection history" in page
    assert "schedule" in page and "EOD snapshot" in page


def test_ops_states_health_even_when_it_is_good(conn):
    """Unlike the desk, this page is where you come to check — silence would be
    ambiguous here."""
    DR.record(conn, steps=steps(("a", OK)), trigger="schedule", session_date=D.today())
    assert "collection healthy" in TestClient(M.app).get("/ops").text.lower()


# =====================================================================================
# the daily script records its own outcome
# =====================================================================================
def test_a_partial_run_is_not_recorded_as_a_full_collection():
    """--only collects part of a session by design; recording it would mark the day done."""
    import scripts.daily as SD
    assert "trade capture" in SD.STEPS
    src = open("scripts/daily.py").read()
    assert "record_run = not a.only" in src


def test_the_auth_failure_path_records(conn):
    """This is THE failure mode of the scheduled job. Unrecorded it goes nowhere."""
    src = open("scripts/daily.py").read()
    head = src.split("Kite session expired")[1].split("return 2")[0]
    assert "_safe_record" in head and "DR.AUTH" in head


def test_recording_failure_cannot_crash_a_good_collection():
    src = open("scripts/daily.py").read()
    body = src.split("def _safe_record")[1].split("def _state")[0]
    assert "except Exception" in body
