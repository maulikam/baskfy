"""What the day still needs collected, and why.

Both daily jobs are blocked on the same thing: a Kite token that expires overnight with no
refresh. The scheduled runs fire at 09:20 and 18:30 and exit 2 if nobody has logged in, and
on 17-18 Aug that is exactly what happened — two sessions of straddle observations lost and
one EOD snapshot missed, none of it recoverable.

Logging in is therefore the natural trigger: it is the moment the blocker clears. This
module decides what is outstanding; scripts/autorun.py does it.

PURE. It reads the database and a clock and returns a list of reasons. It runs nothing,
so the decision can be tested without a broker or a subprocess.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Sequence

# The first tick of the session at which an ATM straddle can be observed. Before 09:15
# there is no NFO quote at all — there is no pre-open session for options.
OPTIONS_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)
SESSION_LOCK = "data/outputs/strangle_session.lock"


def _observed_today(forward_path: str, today: dt.date) -> bool:
    from ..strategies.strangle import calibrate as CAL
    return any(o.session == today for o in CAL.load_forward(forward_path))


def _daily_ok_today(conn, today: dt.date, after_close: bool = False) -> bool:
    """A COMPLETE run today, not merely a successful one.

    `--only` restricts the job to one step and still records outcome 'ok', so a diagnostic
    run would otherwise mask the fact that the day was never actually collected. It fooled
    this function the first time it was tried against the live database.
    """
    import json
    for row in conn.execute(
            "SELECT steps_json FROM daily_runs WHERE session_date=? AND outcome='ok'",
            (today.isoformat(),)):
        try:
            steps = json.loads(row["steps_json"] or "[]")
        except ValueError:
            continue
        if any("not selected by --only" in str(s[2]) for s in steps if len(s) > 2):
            continue
        if after_close:
            # AFTER THE CLOSE THE SNAPSHOT IS THE POINT. A morning run legitimately skips
            # it — nothing can be snapshotted before 15:30 — so the day is not collected
            # until one has actually landed. This is the case that lost Monday's snapshot:
            # the 18:30 job hit an expired token and nothing noticed the gap.
            snap = next((s for s in steps if s and s[0] == "EOD snapshot"), None)
            if not snap or snap[1] != "ok":
                continue
        return True
    return False


def _session_live(lock_path: str) -> bool:
    """Is a strangle session process already running?

    The PID lock is the only signal that crosses processes: the scheduled job, the button
    on /options and this all start the same runner and cannot see each other's memory.
    """
    import os
    import pathlib as _p
    f = _p.Path(lock_path)
    if not f.exists():
        return False
    try:
        os.kill(int(f.read_text().split()[0]), 0)
        return True
    except (ValueError, IndexError, ProcessLookupError, PermissionError, OSError):
        return False


def _session_ran_today(journal_path: str, today: dt.date) -> bool:
    from ..strategies.strangle import journal as _j
    return any(str(r.get("ts", "")).startswith(today.isoformat())
               and r.get("event") in ("session_closed", "entry_window_closed", "skipped",
                                      "no_entry", "entry_cost_veto")
               for r in _j.Journal(journal_path).read())


def needed(conn, *, now: dt.datetime, is_trading_day: bool,
           forward_path: str = "data/outputs/strangle_straddle_record.jsonl",
           entry_window_end: dt.time = dt.time(9, 45),
           session_journal: str = "data/outputs/strangle_journal.jsonl",
           lock_path: str = SESSION_LOCK) -> list[dict[str, Any]]:
    """The outstanding collection for today, in the order it should run.

    Each entry says WHY, because a job that runs itself without explanation is one nobody
    trusts. An empty list means the day is already covered.
    """
    today = now.date()
    if not is_trading_day:
        return []

    out: list[dict[str, Any]] = []

    after_close = now.time() >= MARKET_CLOSE
    if not _daily_ok_today(conn, today, after_close):
        # Worth running before the close even though the snapshot step will refuse: index
        # history, the benchmark series and trade capture are all same-day-only reads, and
        # Kite's /trades endpoint keeps nothing from yesterday.
        out.append({
            "op": "daily", "label": "Daily collection",
            "why": ("the EOD snapshot for today has still not landed; kc.margins() has no "
                    "history, so a missed session cannot be backfilled"
                    if after_close else
                    "no complete run recorded today; index history, benchmarks and today's "
                    "fills are same-day reads that cannot be backfilled"),
            "note": ("the EOD snapshot will be skipped before 15:30 and is left to the "
                     "18:30 job" if now.time() < MARKET_CLOSE else "")})

    if now.time() >= OPTIONS_OPEN and not _observed_today(forward_path, today):
        out.append({
            "op": "strangle_collect", "label": "Record today's straddle",
            "why": "no ATM straddle observation for today; the IV reference bands can only "
                   "be built forward because Kite drops expired contracts",
            "note": ("recorded near the open is best — the bands are built on opening "
                     "prints" if now.time() < dt.time(10, 0) else
                     "later than the open, so this observation sits further down the "
                     "decay curve than the gate that will consume it")})

    # --- the paper session -----------------------------------------------------------
    # Only inside the opening window. A first entry taken at midday runs on parameters
    # written for a full day of decay, against a range the session has already set — the
    # runner refuses it, and starting a process only to be refused is noise.
    if OPTIONS_OPEN <= now.time() <= entry_window_end:
        if _session_live(lock_path):
            pass                                    # already running; nothing to start
        elif _session_ran_today(session_journal, today):
            pass                                    # already had its say today
        else:
            out.append({
                "op": "strangle_session", "label": "Paper session",
                "detached": True,
                "why": "no session has run today and the entry window is still open",
                "note": "runs until 15:10 in its own process, so it cannot hold the "
                        "operations lock; it will veto by itself if the day does not "
                        "qualify"})
    return out


def summary(items: Sequence[dict]) -> str:
    if not items:
        return "Nothing outstanding — today is already collected."
    return f"{len(items)} outstanding: " + ", ".join(i["label"] for i in items)
