"""M19 §1 — the desk's jobs on Beat, and the property the retirement protocol rests on.

The systemd timers on the Mumbai box keep running until five green Beat runs are recorded (M19 §2).
During that overlap both schedulers can fire the same job on the same evening, and that is safe for
exactly one reason: every step is independently idempotent. This asserts it instead of trusting the
docstring that claims it.
"""

from __future__ import annotations

import inspect
import os
import sqlite3
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from celery.schedules import crontab

from baskfy_worker import celery_app
from baskfy_worker.tasks import desk


class _FakeScript(types.ModuleType):
    """A stand-in for one of the desk's scripts. `_run` only ever reaches for `.main`.

    A `ModuleType` subclass with a declared attribute, rather than a bare module with one attached
    afterwards. The latter needs a type-checker suppression comment, which PROMPTS.md §"House
    rules" forbids and `packages/core/tests/test_no_escape_hatches.py` enforces -- including in
    prose, which is why this sentence describes the pattern instead of quoting it.
    """

    def __init__(self, name: str, main: Callable[[], int]) -> None:
        super().__init__(name)
        self.main = main


STATE_TABLES = (
    "snapshots",
    "breadth_readings",
    "trades",
    "index_series",
    "regime_evaluations",
    "benchmark",
)


def _counts(db_path: Path) -> dict[str, int]:
    """Row counts for every table the desk's own `--check` reports on."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {t: conn.execute(f"select count(*) from {t}").fetchone()[0] for t in STATE_TABLES}
    finally:
        conn.close()


@pytest.fixture
def desk_db() -> Path:
    path = desk.DESK_ROOT / "data" / "portfolio.db"
    if not path.exists():
        pytest.skip("no desk database on this machine")
    return path


# =====================================================================================
# idempotence — the property M19 §2's overlap depends on
# =====================================================================================
def test_running_the_check_twice_changes_nothing(desk_db: Path) -> None:
    before = _counts(desk_db)
    first = desk.check_desk_daily()
    middle = _counts(desk_db)
    second = desk.check_desk_daily()
    after = _counts(desk_db)

    assert first["exit_code"] == second["exit_code"] == 0
    assert before == middle == after, "a --check run wrote to the database"


def test_the_working_directory_is_restored(desk_db: Path) -> None:
    """The desk resolves `data/portfolio.db` relative to cwd.

    A Celery worker's cwd is wherever it was started. If this task left the process in the desk's
    directory, the *next* task on that worker would resolve its own paths against it — and if it
    never entered the desk's directory at all, `daily.py` would create a second, empty database
    and succeed quietly against it. Both failures are silent, which is why this is a test.
    """
    before = Path.cwd()
    desk.check_desk_daily()
    assert Path.cwd() == before


def test_sys_path_is_restored() -> None:
    before = list(sys.path)
    desk.check_desk_daily()
    assert sys.path == before


def test_the_context_manager_restores_even_when_the_body_raises() -> None:
    before_cwd, before_path = Path.cwd(), list(sys.path)
    with pytest.raises(RuntimeError), desk.desk_context():
        raise RuntimeError("boom")
    assert Path.cwd() == before_cwd
    assert sys.path == before_path


def test_it_finds_the_desk() -> None:
    """A wrong DESK_ROOT would fail obviously here rather than silently at 18:30 on a Friday."""
    assert desk.DESK_ROOT.is_dir()
    assert (desk.DESK_ROOT / "scripts" / "daily.py").is_file()
    assert (desk.DESK_ROOT / "scripts" / "autorun.py").is_file()


# =====================================================================================
# the schedule mirrors the timers it will eventually replace
# =====================================================================================
def test_beat_fires_when_the_systemd_timer_fires() -> None:
    """`momentum-daily.timer` is `OnCalendar=Mon..Fri 18:30`. Beat must match it.

    Not for tidiness: during the overlap both fire, and a Beat entry at a different hour would be
    collecting a *different* session's data while claiming to be the same job.
    """
    entry = celery_app.BEAT_SCHEDULE["desk-daily-collection"]
    # BEAT_SCHEDULE is typed `dict[str, object]` because celery's entries are heterogeneous;
    # the cast says what this key actually holds rather than loosening the annotation upstream.
    schedule = cast(crontab, entry["schedule"])

    assert entry["task"] == "baskfy.desk.daily"
    assert schedule.hour == {18}
    assert schedule.minute == {30}
    assert schedule.day_of_week == {1, 2, 3, 4, 5}  # Monday..Friday


def test_the_autorun_net_is_after_the_collection_not_before() -> None:
    """It exists to pick up what 18:30 could not. Firing first would make it the primary path."""
    daily = cast(crontab, celery_app.BEAT_SCHEDULE["desk-daily-collection"]["schedule"])
    net = cast(crontab, celery_app.BEAT_SCHEDULE["desk-autorun-safety-net"]["schedule"])
    assert (min(net.hour), min(net.minute)) > (min(daily.hour), min(daily.minute))


def test_the_desk_tasks_are_routed() -> None:
    assert "baskfy.desk.*" in celery_app.TASK_ROUTES


def test_the_task_names_are_the_ones_beat_calls() -> None:
    """A Beat entry naming a task that does not exist fails at 18:30, silently, forever."""
    registered = {
        desk.run_desk_daily.name,
        desk.run_desk_autorun.name,
        desk.check_desk_daily.name,
    }
    for key in ("desk-daily-collection", "desk-autorun-safety-net"):
        assert celery_app.BEAT_SCHEDULE[key]["task"] in registered


# =====================================================================================
# nothing here places an order
# =====================================================================================
def test_the_scheduled_jobs_cannot_reach_the_order_path() -> None:
    """Both jobs are collection. `packages/execution` is the only path to an order, and this
    module must not be a second one."""
    source = inspect.getsource(desk)
    for forbidden in ("place_order", "OrderGateway", "baskfy_execution", "execute("):
        assert forbidden not in source, f"the desk tasks must not reference {forbidden}"


def test_a_failing_step_is_reported_not_raised() -> None:
    """The desk's convention: one failure never stops the rest.

    A Celery retry over a job whose steps are already idempotent and already logged would add
    noise without collecting a single extra row, so a non-zero exit comes back as data.
    """
    # Behaviour, not prose. The first version of this grepped the source for "raise" and failed
    # on the word "raised" in the comment explaining why it does not raise — which teaches nobody
    # anything and trains you to weaken the assertion until it passes.
    # 2 is the desk's "no Kite token" exit code.
    sys.modules["scripts.failing"] = _FakeScript("scripts.failing", lambda: 2)
    try:
        result = desk._run([], "failing")
    finally:
        del sys.modules["scripts.failing"]

    assert result["exit_code"] == 2
    assert result["entry"] == "failing"


def test_a_step_that_calls_sys_exit_is_also_reported_not_raised() -> None:
    """argparse exits rather than returns, and so does the desk in a few places."""

    def main() -> int:
        raise SystemExit(3)

    sys.modules["scripts.exiting"] = _FakeScript("scripts.exiting", main)
    try:
        result = desk._run([], "exiting")
    finally:
        del sys.modules["scripts.exiting"]

    assert result["exit_code"] == 3


def test_the_environment_is_not_leaked_between_runs(desk_db: Path) -> None:
    """`os.environ` must look the same afterwards — a task that mutates it poisons the worker."""
    before = dict(os.environ)
    desk.check_desk_daily()
    assert dict(os.environ) == before
