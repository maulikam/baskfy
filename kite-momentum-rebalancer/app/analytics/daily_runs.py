"""Outcome of each daily collection, so a broken streak is visible without reading a log.

WHY THIS EXISTS
The daily job runs unattended under launchd and fails on any day the Kite session was not
logged in — tokens expire around 6am IST with no refresh. That failure previously went
nowhere but data/outputs/daily.log, which nobody opens. A missed session is permanent:
margins() has no history, /trades is same-day only, and breadth cannot be rebuilt from a
scan you no longer have. So the cost of not noticing is measured in days of lost history,
and the fix is to put the streak where the interface already looks.

This is deliberately not ops_jobs. That table records "a process was started from the
page"; a scheduled run has no row there at all. This records what the COLLECTION did,
whoever launched it.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Sequence

from . import db

OK, SKIP, FAIL = "ok", "skipped", "FAILED"

# Outcomes, worst first. `auth` is separated from `failed` because it is the common case
# and has a specific fix (log in), rather than being a fault in the collection itself.
AUTH, FAILED, PARTIAL, FINE = "auth", "failed", "partial", "ok"


def classify(steps: Sequence[tuple[str, str, str]]) -> str:
    ran = [s for s in steps if s[1] != SKIP]
    if not ran:
        return FAILED
    bad = sum(1 for s in ran if s[1] == FAIL)
    if bad == 0:
        return FINE
    return FAILED if bad == len(ran) else PARTIAL


def record(conn, *, steps: Sequence[tuple[str, str, str]], trigger: str,
           session_date: dt.date, outcome: str | None = None,
           duration_s: float | None = None) -> int:
    """Store one collection outcome. Never raises — a failure to record must not turn a
    partially successful collection into a crash."""
    steps = list(steps)
    outcome = outcome or classify(steps)
    failed = sum(1 for s in steps if s[1] == FAIL)
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO daily_runs(ran_at, session_date, trigger, outcome,"
            " failed_steps, steps_json, duration_s) VALUES(?,?,?,?,?,?,?)",
            (dt.datetime.now().isoformat(timespec="seconds"), session_date.isoformat(),
             trigger, outcome, failed, json.dumps(steps), duration_s))
    return int(cur.lastrowid)


def history(conn, limit: int = 20) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM daily_runs ORDER BY id DESC LIMIT ?", (limit,))]


def status(conn, *, today: dt.date | None = None) -> dict:
    """What the interface needs to say about collection health.

    `stale_days` counts calendar days since the last SUCCESSFUL (or partial) collection —
    a run that failed outright collected nothing, so it does not reset the clock.
    """
    today = today or dt.date.today()
    rows = history(conn, limit=60)
    if not rows:
        return {"ever_ran": False, "last": None, "outcome": None, "stale_days": None,
                "consecutive_failures": 0, "healthy": False,
                "message": "The daily collection has never run."}

    last = rows[0]
    streak = 0
    for r in rows:
        if r["outcome"] in (AUTH, FAILED):
            streak += 1
        else:
            break

    landed = next((r for r in rows if r["outcome"] in (FINE, PARTIAL)), None)
    stale_days = ((today - dt.date.fromisoformat(landed["ran_at"][:10])).days
                  if landed else None)

    if last["outcome"] == AUTH:
        msg = ("The last run could not log in to Kite. Tokens expire daily with no "
               "refresh, so the job cannot recover on its own.")
    elif last["outcome"] == FAILED:
        msg = "The last run collected nothing."
    elif last["outcome"] == PARTIAL:
        failed_names = [s[0] for s in json.loads(last["steps_json"]) if s[1] == FAIL]
        msg = f"The last run failed on: {', '.join(failed_names)}."
    else:
        msg = "The last run collected everything."

    return {
        "ever_ran": True,
        "last": last,
        "outcome": last["outcome"],
        "ran_at": last["ran_at"],
        "trigger": last["trigger"],
        "steps": json.loads(last["steps_json"]),
        "stale_days": stale_days,
        "consecutive_failures": streak,
        # Healthy means the last run landed something AND it was recent enough that no
        # trading session has gone uncollected.
        "healthy": last["outcome"] in (FINE, PARTIAL) and (stale_days or 0) <= 1,
        "message": msg,
    }
