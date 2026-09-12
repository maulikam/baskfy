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
from fastapi.testclient import TestClient

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


class TestTheScanRoutesTheContractFixed:
    """`PLAN-SCAN-SYNC.md`'s shape, over HTTP: **202** queued, **409** one in flight, **429** one
    a minute, and `GET /vbt/scan/{run_id}` for the poll.

    One shape across the sleeves, so a page written against the swing book's button works here
    without learning a second vocabulary. `/vbt/rescan` is the older name the desk page's own
    form posts and it is unchanged — the two go through the same `PgVbtStore.request_scan`, so
    there is one rule rather than two copies of it.
    """

    @pytest.fixture
    def store(self, tmp_path):  # noqa: ANN001, ANN201 - shadows the module's, for one reason
        """The module's fixture, with ``check_same_thread=False``.

        FastAPI runs a ``def`` endpoint in a worker thread, and sqlite refuses a connection used
        from a thread other than the one that made it. Postgres, which is what the desk actually
        runs on, has no such rule; this is a property of the test's twin, not of the code.
        """
        conn = sqlite3.connect(tmp_path / "vbt.db", isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(DDL)
        return W.PgVbtStore(conn, user_id=1, schema="", broker_account_id=1)

    @pytest.fixture
    def client(self, store, monkeypatch):  # noqa: ANN001, ANN201 - the desk's fixture idiom
        import contextlib

        from app import main as M

        monkeypatch.setattr(W, "open_store", lambda: contextlib.nullcontext(store))
        monkeypatch.setattr(W, "_now", lambda: NOW)
        return TestClient(M.app)

    def test_a_press_is_a_202_with_the_run_to_poll(self, client, store) -> None:  # noqa: ANN001
        response = client.post("/vbt/scan")
        assert response.status_code == 202, response.text
        rows = _rows(store)
        assert len(rows) == 1
        assert response.json() == {
            "run_id": rows[0]["id"],
            "status": "QUEUED",
            "requested_at": NOW.isoformat(),
        }
        assert rows[0]["source"] == "desk"
        assert rows[0]["task_id"] is None, "the sweep publishes it; the desk has no Celery"

    def test_a_second_press_while_one_is_in_flight_is_a_409_naming_it(self, client, store) -> None:  # noqa: ANN001
        first = client.post("/vbt/scan").json()["run_id"]
        second = client.post("/vbt/scan")
        assert second.status_code == 409
        assert f"Scan {first} is queued" in second.json()["detail"]
        assert len(_rows(store)) == 1, "the second press wrote nothing"

    def test_a_press_inside_a_minute_of_a_finished_one_is_a_429_with_retry_after(
        self, client, store
    ) -> None:  # noqa: ANN001
        store.request_scan(now=NOW - dt.timedelta(seconds=20))
        store.conn.execute("UPDATE vb_scan_run SET status = 'DONE'")
        response = client.post("/vbt/scan")
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "40"
        assert "one a minute is the limit" in response.json()["detail"]
        assert len(_rows(store)) == 1

    def test_a_dead_worker_does_not_wedge_the_button(self, client, store) -> None:  # noqa: ANN001
        store.request_scan(now=NOW - dt.timedelta(seconds=W.RESCAN_STALE_AFTER_SECONDS + 1))
        store.conn.execute("UPDATE vb_scan_run SET status = 'RUNNING'")
        assert client.post("/vbt/scan").status_code == 202
        assert len(_rows(store)) == 2

    @pytest.mark.parametrize("status", ["QUEUED", "RUNNING", "DONE", "FAILED"])
    def test_get_scan_reads_the_run_back_in_the_contract_s_vocabulary(
        self, client, store, status: str
    ) -> None:  # noqa: ANN001
        run_id = store.request_scan(now=NOW)
        store.conn.execute("UPDATE vb_scan_run SET status = ?", (status,))
        body = client.get(f"/vbt/scan/{run_id}").json()
        assert body["run_id"] == run_id
        assert body["status"] == status
        assert body["source"] == "desk"
        assert body["session_date"] is None, "only the worker decides which session"
        assert body["error"] is None

    def test_get_scan_carries_the_funnel_and_the_reason_the_worker_wrote(
        self, client, store
    ) -> None:  # noqa: ANN001
        run_id = store.request_scan(now=NOW)
        store.conn.execute(
            "UPDATE vb_scan_run SET status = 'DONE', session_date = ?, detail = ?",
            ("2026-09-11", '{"signals": 19, "scan_hits": 41}'),
        )
        body = client.get(f"/vbt/scan/{run_id}").json()
        assert body["session_date"] == "2026-09-11"
        assert body["detail"] == {"signals": 19, "scan_hits": 41}

    def test_an_unknown_run_is_a_404_rather_than_a_five_hundred(self, client) -> None:  # noqa: ANN001
        assert client.get("/vbt/scan/999999").status_code == 404

    def test_a_run_that_is_not_this_desk_s_user_is_a_404_rather_than_a_peek(
        self, client, store
    ) -> None:  # noqa: ANN001
        store.conn.execute(
            "INSERT INTO vb_scan_run (user_id, requested_at, status, source) "
            "VALUES (2, ?, 'DONE', 'desk')",
            (NOW.isoformat(),),
        )
        theirs = store.conn.execute(
            "SELECT id FROM vb_scan_run WHERE user_id = 2"
        ).fetchone()["id"]
        assert client.get(f"/vbt/scan/{theirs}").status_code == 404

    def test_the_older_rescan_name_still_answers_exactly_as_the_page_expects(
        self, client, store
    ) -> None:  # noqa: ANN001
        """`app/templates/vbt.html` reads `body.accepted`, `body.reason` and
        `body.retry_after_seconds`. A contract kept, not a route renamed under a live page."""
        accepted = client.post("/vbt/rescan")
        assert accepted.status_code == 200
        assert accepted.json()["accepted"] is True
        in_flight = client.post("/vbt/rescan").json()
        assert in_flight == {
            "accepted": False,
            "reason": "a re-detect is already in flight",
            "run_id": accepted.json()["run_id"],
            "status": "QUEUED",
        }
        store.conn.execute("UPDATE vb_scan_run SET status = 'DONE'")
        too_soon = client.post("/vbt/rescan").json()
        assert too_soon["accepted"] is False
        assert too_soon["reason"] == "too soon"
        assert too_soon["retry_after_seconds"] == 60

    def test_both_names_write_the_scan_row_and_nothing_else(self, client, store) -> None:  # noqa: ANN001
        """The non-negotiable, behaviourally: a scan queues a detector and is money-free."""
        assert client.post("/vbt/scan").status_code == 202
        store.conn.execute("UPDATE vb_scan_run SET status = 'DONE'")
        assert client.post("/vbt/rescan", json={}).json()["accepted"] is False
        for table in ("vb_plan", "vb_plan_line", "vb_order", "vb_position", "vb_fill"):
            count = store.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            assert count == 0, f"a scan wrote to {table}"

    def test_the_scan_route_is_covered_by_the_desk_s_origin_check(self, store, monkeypatch) -> None:  # noqa: ANN001, E501
        """A POST a link preview or another origin can fire is a POST anybody can fire."""
        import contextlib

        from app import main as M

        monkeypatch.setattr(W, "open_store", lambda: contextlib.nullcontext(store))
        monkeypatch.setattr(W, "_now", lambda: NOW)
        hostile = TestClient(M.app, headers={"Origin": "http://evil.example"})
        assert hostile.post("/vbt/scan").status_code == 403
        assert len(_rows(store)) == 0
