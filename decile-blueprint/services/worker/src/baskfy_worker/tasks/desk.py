"""The desk's two scheduled jobs, as Celery tasks — M19 §1.

`scripts/daily.py` and `scripts/autorun.py` run on the Mumbai box under systemd timers
(`momentum-daily.timer`, Mon-Fri 18:30 IST). This puts the same work on Beat, so the merged
product has one scheduler instead of two.

**The timers stay running.** M19 §2: they are retired only after five green Beat runs are recorded,
and that retirement is a box operation performed by hand — not by this file, and not today. Until
then both schedulers can fire, which is safe for exactly one reason, stated next.

IDEMPOTENCE IS THE WHOLE DESIGN
--------------------------------
`scripts/daily.py`'s own docstring: *"Every step is independently idempotent, so re-running changes
nothing. A step that fails never stops the rest."* That is what makes a duplicate run harmless and
what makes the overlap period safe. It is asserted by test rather than assumed, because the entire
retirement protocol rests on it.

WHY IT IMPORTS THE DESK RATHER THAN SHELLING OUT
--------------------------------------------------
The desk's `app.analytics` imports cleanly inside this workspace's environment, so these are real
calls with real return codes rather than a subprocess whose failure mode is a number. The desk's
working directory matters to it (relative `data/` paths), so it is set and restored around the call
rather than assumed.

NOTHING HERE PLACES AN ORDER
-----------------------------
Both jobs are collection: reads from Kite and writes to the desk's own database. The order path is
`packages/execution`, and nothing in this module touches it. `scripts/autorun.py` says the same
thing in its own docstring, and a test asserts it of this file.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from baskfy_worker.celery_app import app

log = logging.getLogger(__name__)

#: The desk lives beside this workspace in the monorepo. Resolved rather than configured: a wrong
#: path here would fail at import time in an obvious way, which is better than a silent no-op.
DESK_ROOT = Path(__file__).resolve().parents[6] / "kite-momentum-rebalancer"


@contextlib.contextmanager
def desk_context() -> Iterator[None]:
    """Run inside the desk's own directory and import path, then put everything back.

    `scripts/daily.py` resolves `data/portfolio.db` relative to the process's working directory.
    A Celery worker's cwd is wherever it was started, so without this the job would either fail or
    — much worse — create a second, empty database and quietly succeed against it.
    """
    previous_cwd = Path.cwd()
    added = str(DESK_ROOT) not in sys.path
    if added:
        sys.path.insert(0, str(DESK_ROOT))
    os.chdir(DESK_ROOT)
    try:
        yield
    finally:
        os.chdir(previous_cwd)
        if added:
            sys.path.remove(str(DESK_ROOT))


def _run(argv: list[str], entry: str) -> dict[str, Any]:
    """Invoke one of the desk's `main()` functions with a constructed argv."""
    started = dt.datetime.now(dt.UTC)
    with desk_context():
        module = __import__(f"scripts.{entry}", fromlist=["main"])
        original = sys.argv
        sys.argv = [entry, *argv]
        try:
            code = int(module.main())
        except SystemExit as exc:  # argparse and explicit exits both land here
            code = int(exc.code or 0)
        finally:
            sys.argv = original

    elapsed = (dt.datetime.now(dt.UTC) - started).total_seconds()
    result = {"entry": entry, "argv": argv, "exit_code": code, "seconds": round(elapsed, 2)}
    # A non-zero exit is reported, not raised. The desk's convention is that one failure never
    # stops the rest, and a Celery retry storm over a job whose steps are already idempotent and
    # already logged would add noise without adding a single collected row.
    log.info("desk %s finished: %s", entry, result)
    return result


@app.task(name="baskfy.desk.daily", bind=False)
def run_desk_daily(source: str = "schedule") -> dict[str, Any]:
    """`python -m scripts.daily --quiet --source schedule` — the 18:30 IST collection."""
    return _run(["--quiet", "--source", source], "daily")


@app.task(name="baskfy.desk.autorun", bind=False)
def run_desk_autorun() -> dict[str, Any]:
    """`python -m scripts.autorun` — whatever today still needs collected.

    Its trigger has always been a human logging in, because both jobs are blocked on a Kite token
    that expires overnight with no refresh. On Beat it becomes a safety net rather than the primary
    path: if the token arrived late, this picks up what the 18:30 run could not do.
    """
    return _run([], "autorun")


@app.task(name="baskfy.desk.daily_check", bind=False)
def check_desk_daily() -> dict[str, Any]:
    """`--check`: report state, change nothing. Safe to run anywhere, including a test."""
    return _run(["--check"], "daily")
