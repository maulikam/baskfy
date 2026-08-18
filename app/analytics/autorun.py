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


def needed(conn, *, now: dt.datetime, is_trading_day: bool,
           forward_path: str = "data/outputs/strangle_straddle_record.jsonl"
           ) -> list[dict[str, Any]]:
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
    return out


def summary(items: Sequence[dict]) -> str:
    if not items:
        return "Nothing outstanding — today is already collected."
    return f"{len(items)} outstanding: " + ", ".join(i["label"] for i in items)
