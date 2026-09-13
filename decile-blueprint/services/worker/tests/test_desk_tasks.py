"""The desk's collection jobs — no longer on Beat (NEEDS-MAULIK §33, 13 Sep 2026).

Beat used to fire ``baskfy.desk.daily`` on this worker, whose image does not contain the
desk tree. Maulik: the desk-daily container runs the collection. These tests assert that
Beat no longer schedules it, that leftover Celery names refuse rather than collect, and
that the ``--check`` wrapper is still idempotent in the monorepo where ``DESK_ROOT`` exists.

The schedule itself — 18:30 daily, 18:50 autorun, Persistent=true catch-up — lives in
``kite-momentum-rebalancer/scripts/desk_daily_loop.py`` and is asserted there.
"""

from __future__ import annotations

import inspect
import os
import re
import sqlite3
import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest

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
# Beat must not fire collection — the desk-daily container does
# =====================================================================================
_COMPOSE = Path(__file__).resolve().parents[3] / "infra" / "docker" / "compose.prod.yml"


def test_beat_does_not_schedule_desk_collection() -> None:
    """A Beat entry here is a weekday failure: this image has no desk tree.

    The retired keys must stay gone, and no new entry may point at ``baskfy.desk.*``.
    """
    assert "desk-daily-collection" not in celery_app.BEAT_SCHEDULE
    assert "desk-autorun-safety-net" not in celery_app.BEAT_SCHEDULE
    leftover = [
        key
        for key, entry in celery_app.BEAT_SCHEDULE.items()
        if isinstance(entry, dict) and str(entry.get("task", "")).startswith("baskfy.desk.")
    ]
    assert leftover == [], f"Beat still fires desk collection: {leftover}"


def test_the_desk_image_runs_the_collection() -> None:
    """The other half of the route: compose starts desk-daily from the desk image."""
    text = _COMPOSE.read_text()
    match = re.search(r"^  desk-daily:(.*?)(?=^  [A-Za-z]|\Z)", text, flags=re.M | re.S)
    assert match is not None, "compose.prod.yml lost the desk-daily service"
    body = match.group(1)
    assert "<<: *desk" in body
    assert "command: [desk-daily-loop]" in body


def test_the_celery_names_refuse_rather_than_collect() -> None:
    """A leftover Redis message must not chdir into a missing tree and look like success."""
    daily = desk.run_desk_daily()
    autorun = desk.run_desk_autorun()
    assert daily["exit_code"] == autorun["exit_code"] == 2
    assert daily["entry"] == "daily"
    assert autorun["entry"] == "autorun"


def test_a_missing_desk_tree_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desk, "DESK_ROOT", tmp_path / "absent")
    result = desk._run(["--check"], "daily")
    assert result["exit_code"] == 2
    assert result["entry"] == "daily"


def test_the_desk_tasks_are_routed() -> None:
    """Leftover messages still land on default, which this worker consumes."""
    assert "baskfy.desk.*" in celery_app.TASK_ROUTES


def test_the_retired_names_are_still_registered() -> None:
    """Unregistered leftover messages look like a broker bug. A refusal is a log line."""
    registered = {
        desk.run_desk_daily.name,
        desk.run_desk_autorun.name,
        desk.check_desk_daily.name,
    }
    assert registered == {
        "baskfy.desk.daily",
        "baskfy.desk.autorun",
        "baskfy.desk.daily_check",
    }


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
