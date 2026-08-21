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
import logging
from typing import Any, Sequence

# The first tick of the session at which an ATM straddle can be observed. Before this
# there is no NFO or BFO quote at all — there is no pre-open session for options.
#
# Imported, not redeclared, and that has not changed. This module held its own copy while
# clock.entry_window_state had no floor at all, so the same fact was enforced in one place
# and missing from the other: autorun correctly refused to start a session at 06:00, while
# --check and the runner both reported the entry window open.
#
# What changed at M6 is only WHEN: the strangle subsystem is frozen (frozen/strangle/), and
# autorun runs on every login, so importing it at module scope would make the equity desk
# depend on the options lab being present. The import moved into _options_items(), which is
# the one place that reads it and which no longer runs at all unless OPTIONS_ENABLED.
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


# Events that mean the day HAS been decided. A run that declined to start is not on this
# list: entry_window_not_open is deliberately absent, because a pre-open run has evaluated
# nothing and must not stand in for the session it was too early to be.
DECIDED = ("session_closed", "entry_window_closed", "skipped", "no_entry",
           "entry_cost_veto")


def _session_ran_today(journal_path: str, today: dt.date) -> bool:
    from ..strategies.strangle import journal as _j
    return any(str(r.get("ts", "")).startswith(today.isoformat())
               and r.get("event") in DECIDED
               for r in _j.Journal(journal_path).read())


def needed(conn, *, now: dt.datetime, is_trading_day: bool,
           forward_path: str | None = None,
           entry_window_end: dt.time = dt.time(9, 45),
           session_journal: str | None = None,
           lock_path: str | None = None) -> list[dict[str, Any]]:
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

    # The options lab is a separate, paper-only subsystem and it is frozen (M6). Off by
    # default, and when off this loop must not import it — autorun runs on every login.
    from .. import config as C

    if C.OPTIONS_ENABLED:
        try:
            for spec in _underlyings(forward_path, session_journal, lock_path):
                out.extend(_options_items(spec, now=now, today=today,
                                          entry_window_end=entry_window_end))
        except ImportError:
            logging.warning(
                "OPTIONS_ENABLED is set, but the strangle subsystem is frozen "
                "(frozen/strangle/). Today's options collection is not offered; the equity "
                "collection above is unaffected.")
    return out


def _underlyings(forward_path, session_journal, lock_path) -> list[dict]:
    """One entry per instrument to collect for.

    LEGACY PATHS WIN. When a caller names explicit paths it is asking about one specific
    record, and silently fanning that out to three underlyings would answer a different
    question than the one asked.
    """
    if forward_path or session_journal or lock_path:
        return [{"slug": None, "label": "", "config": None,
                 "forward": forward_path or "data/outputs/strangle_straddle_record.jsonl",
                 "journal": session_journal or "data/outputs/strangle_journal.jsonl",
                 "lock": lock_path or SESSION_LOCK}]
    from ..strategies.strangle import instruments as INS
    return [{"slug": u.slug, "label": u.label, "config": u.config,
             "forward": u.forward(), "journal": u.journal(), "lock": u.lock()}
            for u in INS.configured()]


def _options_items(spec: dict, *, now: dt.datetime, today: dt.date,
                   entry_window_end: dt.time) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    slug, label = spec["slug"], spec["label"]
    tag = f" ({label})" if label else ""
    args = {"instrument": slug} if slug else {}

    from ..strategies.strangle.clock import OPTIONS_OPEN

    if now.time() >= OPTIONS_OPEN and not _observed_today(spec["forward"], today):
        out.append({
            "op": "strangle_collect", "label": f"Record today's straddle{tag}",
            "args": args, "instrument": slug,
            "why": f"no ATM straddle observation for today{tag}; the IV reference bands "
                   "can only be built forward because Kite drops expired contracts",
            "note": ("recorded near the open is best — the bands are built on opening "
                     "prints" if now.time() < dt.time(10, 0) else
                     "later than the open, so this observation sits further down the "
                     "decay curve than the gate that will consume it")})

    # --- the paper session -------------------------------------------------------------
    cfg = None
    if spec["config"]:
        try:
            from ..strategies.strangle import config as _sc
            cfg = _sc.load(spec["config"])
        except Exception:                                          # noqa: BLE001
            cfg = None
    # THE SAME helper the runner enters on. Deciding here that a session is worth starting
    # while the runner refuses it — or the reverse — is a silent lost session either way.
    from ..strategies.strangle import clock as _clock
    win = _clock.entry_window_state(cfg, now.time(), default_end=entry_window_end)

    # win carries the floor now, so this is one question with one answer.
    if win["veto"]:
        return out
    if _session_live(spec["lock"]) or _session_ran_today(spec["journal"], today):
        return out                      # already running, or already had its say today

    after_window = win["state"] == "late"
    deadline = win["deadline"]
    out.append({
        "op": "strangle_session", "label": f"Paper session{tag}",
        "detached": True, "args": args, "instrument": slug,
        "why": ("no session has run today and a late first entry is still permitted "
                f"until {deadline.strftime('%H:%M')}" if after_window else
                "no session has run today and the entry window is still open"),
        "note": ("past the preferred window, so the runner will size it down rather than "
                 "pretend it has a full day of decay ahead of it" if after_window else
                 "runs until 15:10 in its own process, so it cannot hold the operations "
                 "lock; it will veto by itself if the day does not qualify")})
    return out


def summary(items: Sequence[dict]) -> str:
    if not items:
        return "Nothing outstanding — today is already collected."
    return f"{len(items)} outstanding: " + ", ".join(i["label"] for i in items)
