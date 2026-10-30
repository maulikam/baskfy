"""Grade the catch-up decision: which sessions would the sweep run, at a given IST moment?

    python3 tools/parity/catch_up_diff.py [--report /tmp/cu.txt] [--sides py,rs] [--no-build]

``session_catch_up`` (the Celery task) asks ``catch_up.unlanded_sessions(through=today IST,
lookback_days=7, now=now IST)`` and runs the nightly for each answer. The Rust binary ports that
decision (``baskfy-worker catch-up --now ...``, R-gate.4); running the chain for each day is
graded by ``nightly_diff.py``. This harness pins the **clock** (never the system's) and asks both
sides the same question on one clone of the bed, in two states:

* ``bed``   — the bed as it is (2026-10-29 landed as run 103; earlier failed runs left as they are);
* ``fresh`` — the same clone with 2026-10-29's ``pipeline_run`` deleted, so that day is missing.

and at moments inside the session, a second before ``SESSION_DATA_READY_IST`` (18:00 IST), at it,
and after it — **today is never proposed before 18:00** (the published series never claims a day
still in session; CLAUDE.md "Which date the product shows"). The question is read-only on both
sides (Rust's transaction is rolled back); the clone is dropped on exit, error and SIGINT.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nightly_diff as nd  # noqa: E402

MOMENTS = {
    "bed": [
        "2026-10-30T09:15:00",  # today (a derived session) in session
        "2026-10-30T13:15:00",
        "2026-10-30T17:59:59",  # a second before the bhavcopy time
        "2026-10-30T18:00:00",  # at it: today is proposed
        "2026-10-30T21:30:00",
        "2026-10-31T10:00:00",  # a Saturday: through is not a session
        "2026-11-02T08:00:00",  # Monday before the open: 10-30 is past and unlanded
    ],
    "fresh": [
        "2026-10-29T13:15:00",  # 10-29 in session: never proposed
        "2026-10-29T17:59:59",
        "2026-10-29T18:00:00",  # 10-29 proposed from here
        "2026-10-30T06:00:00",
    ],
}

PY_SNIPPET = r"""
import asyncio, datetime as dt, json, sys
from zoneinfo import ZoneInfo
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from baskfy_worker import catch_up
async def main():
    engine = create_async_engine(sys.argv[1])
    now = dt.datetime.fromisoformat(sys.argv[2]).replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    async with AsyncSession(engine) as session:
        days = await catch_up.unlanded_sessions(
            session, through=now.date(), lookback_days=catch_up.DEFAULT_LOOKBACK_DAYS, now=now)
        await session.rollback()
    await engine.dispose()
    print(json.dumps({"checked_through": now.date().isoformat(),
                      "sessions": [d.isoformat() for d in days]}, separators=(",", ":")))
asyncio.run(main())
"""


def ask(side: str, db: str, moment: str, args: argparse.Namespace) -> str:
    env = {"HOME": "/tmp", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", **nd.TEST_ENV,
           "BASKFY_DATABASE_URL": nd.db_url(db, asyncpg=True)}
    if side == "py":
        env.update(nd.python_env())
        cmd = [str(nd.BLUEPRINT / ".venv/bin/python"), "-c", PY_SNIPPET, nd.db_url(db, asyncpg=True), moment]
    else:
        cmd = [str(nd.TARGET / args.profile / "baskfy-worker"), "catch-up", "--now", moment]
    proc = subprocess.run(cmd, cwd="/tmp", env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return f"<exit {proc.returncode}> {proc.stdout.strip()} {proc.stderr.strip()[-400:]}"
    return json.dumps(json.loads(proc.stdout.strip().splitlines()[-1]), sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sides", default="py,rs")
    parser.add_argument("--profile", default="debug", choices=("debug", "release"))
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--max-dbs", type=int, default=5)
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    sides = args.sides.split(",")
    if not args.no_build and "rs" in sides:
        nd.build_rust(args.profile)
    signal.signal(signal.SIGINT, nd.on_signal)
    signal.signal(signal.SIGTERM, nd.on_signal)
    lock = nd.take_lock()
    db = "rsn_run_catchup"
    lines: list[str] = []
    equal = total = 0
    try:
        if nd.db_exists(db):
            nd.drop(db)
        # The cached `rerun` template is the bed unmodified; cloning it leaves `baskfy` alone
        # while other harnesses hold connections to it.
        source = "rsn_tpl_rerun_20261029" if nd.db_exists("rsn_tpl_rerun_20261029") else nd.SOURCE_DB
        nd.clone(source, db, args.max_dbs, set())
        for state, moments in MOMENTS.items():
            if state == "fresh":
                nd.psql(db, "DELETE FROM pipeline_run WHERE trade_date = DATE '2026-10-29';")
            for moment in moments:
                answers = [ask(side, db, moment, args) for side in sides]
                same = len(set(answers)) == 1 and not answers[0].startswith("<exit")
                total += 1
                equal += same
                lines.append(f"{'EQUAL    ' if same else 'DIFFERENT'} {state:5} now={moment} IST")
                for side, answer in zip(sides, answers):
                    lines.append(f"    {side}: {answer}")
    finally:
        nd.cleanup()
        lock.close()
    lines.append(f"{equal} of {total} moments EQUAL")
    text = "\n".join(lines)
    print(text)
    if args.report:
        Path(args.report).write_text(text + "\n")
    return 0 if equal == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
