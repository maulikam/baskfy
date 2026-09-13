"""The desk's collection jobs, as they used to run on Beat — M19 §1, retired 13 Sep 2026.

Beat used to fire ``baskfy.desk.daily`` and ``baskfy.desk.autorun`` on this worker. The
``baskfy-py`` image does not contain the desk tree, so both entries failed every weekday
(NEEDS-MAULIK §33). Maulik: collection runs in the ``desk-daily`` container. Beat no
longer schedules these names.

This module stays for two reasons, and neither is "run the collection from here":

1. ``check_desk_daily`` / ``_run`` still exercise ``scripts.daily --check`` in the
   monorepo, where ``DESK_ROOT`` exists, so the idempotence tests keep a real subject.
2. The Celery task names stay registered as refusal stubs so a leftover Redis message
   from the old Beat entries logs the move instead of looking unregistered.

NOTHING HERE PLACES AN ORDER. Collection is reads from Kite and writes to the desk's
own database. The order path is ``packages/execution``.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

from baskfy_worker.celery_app import app

log = logging.getLogger(__name__)

#: The desk lives beside this workspace in the monorepo. Resolved rather than configured: a wrong
#: path here would fail at import time in an obvious way, which is better than a silent no-op.
DESK_ROOT = Path(__file__).resolve().parents[6] / "kite-momentum-rebalancer"

#: What the Celery stubs return and log. A leftover Beat message hitting this worker must not
#: look like a successful collection — exit 2 is the desk's "did not collect" code (no token).
_MOVED = (
    "retired from Beat; collection runs in the desk-daily container "
    "(NEEDS-MAULIK §33, Maulik 13 Sep 2026)"
)


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


class DeskRunResult(TypedDict):
    """What a desk job hands back to Celery.

    A TypedDict rather than a loosely-typed dict, because PROMPTS.md §"House rules" forbids the
    dynamic escape hatch and the shape is fixed anyway: an operator reading a task result wants
    these four things, and a loose mapping would let a fifth appear without anyone noticing.
    (`test_no_escape_hatches.py` scans source text, so this paragraph describes the forbidden
    annotation rather than spelling it.)
    """

    entry: str
    argv: list[str]
    exit_code: int
    seconds: float


def _missing_tree_result(argv: list[str], entry: str) -> DeskRunResult:
    log.error(
        "desk tree missing at %s; collection runs in the desk-daily container "
        "(NEEDS-MAULIK §33, Maulik 13 Sep 2026)",
        DESK_ROOT,
    )
    return {"entry": entry, "argv": argv, "exit_code": 2, "seconds": 0.0}


def _run(argv: list[str], entry: str) -> DeskRunResult:
    """Invoke one of the desk's `main()` functions with a constructed argv."""
    if not DESK_ROOT.is_dir():
        return _missing_tree_result(argv, entry)

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
    result: DeskRunResult = {
        "entry": entry,
        "argv": argv,
        "exit_code": code,
        "seconds": round(elapsed, 2),
    }
    # A non-zero exit is reported, not raised. The desk's convention is that one failure never
    # stops the rest, and a Celery retry storm over a job whose steps are already idempotent and
    # already logged would add noise without adding a single collected row.
    log.info("desk %s finished: %s", entry, result)
    return result


def _refused(entry: str, argv: list[str]) -> DeskRunResult:
    log.error("baskfy.desk.%s: %s", entry, _MOVED)
    return {"entry": entry, "argv": argv, "exit_code": 2, "seconds": 0.0}


@app.task(name="baskfy.desk.daily", bind=False)
def run_desk_daily(source: str = "schedule") -> DeskRunResult:
    """Was Beat's 18:30 collection. Now a refusal: the desk-daily container runs it."""
    return _refused("daily", ["--quiet", "--source", source])


@app.task(name="baskfy.desk.autorun", bind=False)
def run_desk_autorun() -> DeskRunResult:
    """Was Beat's 18:50 safety net. Now a refusal: the desk-daily container runs it."""
    return _refused("autorun", [])


@app.task(name="baskfy.desk.daily_check", bind=False)
def check_desk_daily() -> DeskRunResult:
    """`--check`: report state, change nothing. Safe to run anywhere, including a test."""
    return _run(["--check"], "daily")
