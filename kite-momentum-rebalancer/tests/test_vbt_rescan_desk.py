"""VB12 — the desk's **Re-detect** button: the row it writes and the two refusals.

`docs/vbt/05` §3's newest control, and the narrowest one on the page. It asks for a session that
has already closed to be detected again — the night the chain's step was skipped because the
quality gate refused the day, and the morning after a threshold changed.

What is asserted here:

* **it writes one `vb_scan_run` row and nothing else** — no plan, no order, no broker;
* **one in flight** — a second press while a scan is QUEUED or RUNNING gets the same id back
  rather than starting a second detection over the same bars;
* **one a minute**, answered from the table rather than a cache, so the rule holds on a box with
  no Redis and is testable against sqlite alone;
* **a dead worker does not wedge the button** — past the stale window a new row is allowed;
* **the desk's copy of the two numbers matches the worker's**, because the desk cannot import
  the worker (different venv, no Celery) and two constants that must agree will not unless
  something checks.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from pathlib import Path

import pytest

# The schema and the clock live beside this file; `tests/` is not a package, so the path is
# explicit — the same insert `test_vbt_safety.py` makes.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_vbt_desk import (  # noqa: E402 - the sys.path insert above has to come first
    DDL,
    NOW,
)

from app import vbt_desk as W  # noqa: E402

DESK = Path(__file__).resolve().parents[1]
WORKER = (
    DESK.parent
    / "decile-blueprint"
    / "services"
    / "worker"
    / "src"
    / "baskfy_worker"
    / "tasks"
    / "vbt_rescan.py"
)


@pytest.fixture
def store(tmp_path):  # noqa: ANN001, ANN201 - the desk's own fixture idiom
    conn = sqlite3.connect(tmp_path / "vbt.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    return W.PgVbtStore(conn, user_id=1, schema="", broker_account_id=1)


def _rows(store) -> list[sqlite3.Row]:  # noqa: ANN001
    return store.conn.execute("SELECT * FROM vb_scan_run ORDER BY id").fetchall()


class TestTheRowItWrites:
    def test_a_request_inserts_one_queued_row_for_this_user(self, store) -> None:  # noqa: ANN001
        run_id = store.request_scan(now=NOW)

        rows = _rows(store)
        assert len(rows) == 1
        assert rows[0]["id"] == run_id
        assert rows[0]["status"] == "QUEUED"
        assert rows[0]["source"] == "desk"
        assert rows[0]["user_id"] == 1

    def test_the_row_has_no_task_id_which_is_what_the_sweep_looks_for(self, store) -> None:  # noqa: ANN001
        """The desk has no Celery client. A row with no `task_id` is the sweep's whole input."""
        store.request_scan(now=NOW)
        assert _rows(store)[0]["task_id"] is None

    def test_it_decides_no_session_because_only_the_worker_can(self, store) -> None:  # noqa: ANN001
        """The desk asks for "the latest published session" and does not know which that is —
        the exchange calendar is the worker's to read."""
        store.request_scan(now=NOW)
        assert _rows(store)[0]["session_date"] is None

    def test_newest_scan_reads_it_back(self, store) -> None:  # noqa: ANN001
        run_id = store.request_scan(now=NOW)
        newest = store.newest_scan()
        assert newest is not None
        assert newest["id"] == run_id
        assert newest["status"] == "QUEUED"

    def test_newest_scan_is_none_before_the_button_has_ever_been_pressed(self, store) -> None:  # noqa: ANN001
        assert store.newest_scan() is None

    def test_it_writes_nothing_but_the_scan_row(self, store) -> None:  # noqa: ANN001
        """The property the whole feature rests on: a re-detect is not a plan and not an order."""
        store.request_scan(now=NOW)
        for table in ("vb_plan", "vb_plan_line", "vb_order", "vb_position", "vb_fill"):
            count = store.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            assert count == 0, f"a re-detect wrote to {table}"

    def test_the_newest_of_several_is_the_one_returned(self, store) -> None:  # noqa: ANN001
        store.request_scan(now=NOW - dt.timedelta(hours=2))
        latest = store.request_scan(now=NOW)
        assert store.newest_scan()["id"] == latest


class TestTheTwoRefusals:
    """Both are the route's, and both read the table rather than a cache."""

    def _decide(self, store, now: dt.datetime) -> dict:  # noqa: ANN001
        """The route's own logic, over the store — the route itself needs a running app."""
        newest = store.newest_scan()
        if newest is not None:
            age = (now - W._as_datetime(newest["requested_at"])).total_seconds()
            if newest["status"] in W.RESCAN_IN_FLIGHT and age < W.RESCAN_STALE_AFTER_SECONDS:
                return {"accepted": False, "reason": "a re-detect is already in flight"}
            if age < W.RESCAN_MIN_INTERVAL_SECONDS:
                return {"accepted": False, "reason": "too soon"}
        return {"accepted": True, "run_id": store.request_scan(now=now)}

    def test_a_second_press_while_one_is_in_flight_is_refused(self, store) -> None:  # noqa: ANN001
        store.request_scan(now=NOW)
        assert self._decide(store, NOW + dt.timedelta(seconds=5)) == {
            "accepted": False,
            "reason": "a re-detect is already in flight",
        }
        assert len(_rows(store)) == 1

    @pytest.mark.parametrize("status", ["QUEUED", "RUNNING"])
    def test_both_in_flight_states_refuse(self, store, status: str) -> None:  # noqa: ANN001
        store.request_scan(now=NOW)
        store.conn.execute("UPDATE vb_scan_run SET status = ?", (status,))
        assert self._decide(store, NOW + dt.timedelta(seconds=5))["accepted"] is False

    def test_a_finished_scan_inside_the_minute_is_too_soon_rather_than_in_flight(
        self, store
    ) -> None:  # noqa: ANN001
        """Different refusal, different reason: the work is done, the rate limit is not."""
        store.request_scan(now=NOW)
        store.conn.execute("UPDATE vb_scan_run SET status = 'DONE'")
        assert self._decide(store, NOW + dt.timedelta(seconds=30)) == {
            "accepted": False,
            "reason": "too soon",
        }

    def test_a_minute_later_it_is_allowed(self, store) -> None:  # noqa: ANN001
        store.request_scan(now=NOW)
        store.conn.execute("UPDATE vb_scan_run SET status = 'DONE'")
        assert self._decide(store, NOW + dt.timedelta(seconds=61))["accepted"] is True
        assert len(_rows(store)) == 2

    def test_a_dead_worker_does_not_wedge_the_button(self, store) -> None:  # noqa: ANN001
        """A RUNNING row older than the stale window is a worker that died. The button has to
        keep working, or one crash costs an afternoon."""
        store.request_scan(now=NOW - dt.timedelta(seconds=W.RESCAN_STALE_AFTER_SECONDS + 1))
        store.conn.execute("UPDATE vb_scan_run SET status = 'RUNNING'")
        assert self._decide(store, NOW)["accepted"] is True
        assert len(_rows(store)) == 2

    def test_a_failed_scan_can_be_retried_after_the_interval(self, store) -> None:  # noqa: ANN001
        store.request_scan(now=NOW)
        store.conn.execute("UPDATE vb_scan_run SET status = 'FAILED', error = 'boom'")
        assert self._decide(store, NOW + dt.timedelta(seconds=61))["accepted"] is True


class TestTheDeskAndTheWorkerAgree:
    """Two copies of two numbers, in two venvs that cannot import each other.

    `app/swing_desk.py` has the same shape and the same hazard. Read off the worker's source as
    text, because importing it from the desk's venv is exactly what is impossible.
    """

    def test_the_worker_module_is_where_this_says_it_is(self) -> None:
        assert WORKER.is_file(), f"{WORKER} has moved; this test cannot check the copies agree"

    @pytest.mark.parametrize(
        ("desk_value", "worker_name"),
        [
            (W.RESCAN_STALE_AFTER_SECONDS, "STALE_AFTER_SECONDS"),
            (W.RESCAN_MIN_INTERVAL_SECONDS, "MIN_INTERVAL_SECONDS"),
        ],
    )
    def test_the_numbers_match(self, desk_value: int, worker_name: str) -> None:
        source = WORKER.read_text(encoding="utf-8")
        line = next(
            line for line in source.splitlines() if line.startswith(f"{worker_name}: Final")
        )
        assert int(line.split("=")[1].strip()) == desk_value

    def test_the_in_flight_states_match(self) -> None:
        source = WORKER.read_text(encoding="utf-8")
        assert 'IN_FLIGHT: Final[tuple[str, ...]] = ("QUEUED", "RUNNING")' in source
        assert W.RESCAN_IN_FLIGHT == ("QUEUED", "RUNNING")


class TestThePage:
    def test_the_button_posts_to_the_rescan_route_and_nothing_else(self) -> None:
        body = (DESK / "app" / "templates" / "vbt.html").read_text(encoding="utf-8")
        assert 'action="/vbt/rescan"' in body
        assert body.count('class="vb-rescan"') == 1

    def test_the_button_says_what_it_does_rather_than_scan_now(self) -> None:
        """It is not the swing book's control and must not read like it — that one scans today
        from live quotes, and this one cannot."""
        body = (DESK / "app" / "templates" / "vbt.html").read_text(encoding="utf-8")
        assert "Re-detect the last session" in body
        assert "Scan now" not in body
