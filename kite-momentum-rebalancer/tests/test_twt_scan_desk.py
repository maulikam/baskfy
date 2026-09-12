"""TW12 — the three-weeks-tight desk's **Scan now** button: two routes and one table.

What is asserted, and where it comes from:

* ``PLAN-SCAN-SYNC.md`` "The contract": ``POST /twt/scan`` answers **202** queued, **409** one
  already in flight, **429** rate-limited (one a minute); ``GET /twt/scan/{run_id}`` answers that
  run's status; the vocabulary is the swing book's ``QUEUED | RUNNING | DONE | FAILED``.
* ``docs/twt/02`` §3: a scan is **money-free**. The route writes one row and nothing else, and it
  never asks for a broker gateway — asserted here by making ``twt_gateway`` explode if it is
  touched, and again over the whole surface in
  ``decile-blueprint/packages/core/tests/test_twt_safety_properties.py``.
* The desk cannot import the worker (different venv, no Celery), so its two refusal windows are
  **copied** from ``baskfy_worker.tasks.twt_scan``. :class:`TestTheTwoCopiesAgree` reads both
  files and asserts the numbers match, the way ``test_vbt_rescan_desk.py`` does for VB12 — a
  copied constant that nobody checks is a constant that has already drifted.

THE DATABASE
------------
``tw_scan_run`` is built here from a DDL that mirrors
``alembic/versions/0042_twt_scan_run.py`` in sqlite's spelling, on a per-test file, and the store
runs the same SQL it runs on Postgres with the schema prefix set to ``""``. A desk suite cannot
depend on a Postgres being up, and a fake connection would prove nothing about the SQL.

THE CLOCK
---------
Every test drives ``_now`` itself. The two refusals are both *times*, and a test that let the wall
clock decide whether a scan was "a minute ago" is a test that passes on a fast machine and fails
on a slow one — which is worse than no test, because it gets deleted rather than fixed.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import re
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from app import config as C
from app import main as M
from app import twt_desk as W

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime(2026, 9, 11, 14, 20, tzinfo=IST)

#: ``alembic/versions/0042_twt_scan_run.py``, in sqlite's spelling, for the columns the store
#: reads. ``INTEGER PRIMARY KEY AUTOINCREMENT`` stands in for the Postgres identity column.
DDL = """
CREATE TABLE tw_scan_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  requested_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  session_date TEXT,
  status TEXT NOT NULL DEFAULT 'QUEUED',
  source TEXT NOT NULL DEFAULT 'desk',
  detail TEXT,
  error TEXT,
  task_id TEXT,
  CHECK (status IN ('QUEUED', 'RUNNING', 'DONE', 'FAILED')),
  CHECK (source IN ('desk', 'web', 'cli')));
"""

#: sqlite3 has no ``datetime``, in a spelling that sorts. Python's default adapter writes a space
#: where ``isoformat()`` writes a ``T``, and sqlite compares timestamps as text — so a bound
#: ``now`` would sort before every stored ``requested_at`` and "was this a minute ago" would
#: answer "no" forever. Postgres compares real timestamps and has no such hazard; this keeps the
#: two sides of the test agreeing. (The same adapter ``test_twt_desk.py`` registers, for the same
#: reason, restated because either module may be run alone.)
sqlite3.register_adapter(dt.datetime, lambda value: value.isoformat())
sqlite3.register_adapter(Decimal, str)


@pytest.fixture
def store(tmp_path):  # noqa: ANN001, ANN201 - the desk's own fixture idiom
    # `check_same_thread=False` because `TestClient` runs the app in a worker thread while the
    # test holds the connection; the desk's own Postgres pool has no such constraint.
    conn = sqlite3.connect(tmp_path / "twt-scan.db", isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    return W.PgTwtStore(conn, user_id=1, schema="")


class _Clock:
    """A clock the test moves by hand."""

    def __init__(self, at: dt.datetime) -> None:
        self.at = at

    def __call__(self) -> dt.datetime:
        return self.at

    def advance(self, seconds: int) -> None:
        self.at = self.at + dt.timedelta(seconds=seconds)


@pytest.fixture
def clock():  # noqa: ANN201
    return _Clock(NOW)


@pytest.fixture
def client(store, clock, monkeypatch, tmp_path):  # noqa: ANN001, ANN201
    """The mounted app, pointed at the scenario's sqlite file, with **no broker at all**.

    ``twt_gateway`` and ``last_price`` raise if anything touches them. A scan that needed either
    would be a scan that had stopped being money-free, and the failure should name the line that
    did it rather than show up as a mysterious dry-run order in a journal.
    """
    from fastapi.testclient import TestClient

    def _no_gateway():  # noqa: ANN202
        raise AssertionError("a scan route asked for a broker gateway")

    def _no_price(_symbol):  # noqa: ANN001, ANN202
        raise AssertionError("a scan route read a broker quote")

    monkeypatch.setattr(C, "TWT_EXECUTION_ENABLED", False)
    monkeypatch.setattr(C, "DRY_RUN", True)
    monkeypatch.setattr(M, "_kite", None)
    monkeypatch.setattr(W, "open_store", lambda: contextlib.nullcontext(store))
    monkeypatch.setattr(W, "_now", clock)
    monkeypatch.setattr(W, "twt_gateway", _no_gateway)
    monkeypatch.setattr(W, "last_price", _no_price)
    monkeypatch.setattr(C, "TOKEN_FILE", str(tmp_path / "no-such-token.json"))
    return TestClient(M.app, headers={"Origin": "http://testserver:8420"})


# =========================================================================================
# The contract: 202, 409, 429, and the run you poll
# =========================================================================================
class TestTheContract:
    def test_a_scan_is_accepted_with_202_and_the_run_to_poll(self, client, store) -> None:  # noqa: ANN001
        answer = client.post("/twt/scan")
        assert answer.status_code == 202
        body = answer.json()
        assert body["status"] == "QUEUED"
        assert int(body["run_id"]) >= 1
        assert body["requested_at"] == NOW.isoformat()
        # And the row is really there, QUEUED, with no task_id — the desk has no Celery client,
        # so the worker's sweep is what publishes it.
        run = store.scan_run(int(body["run_id"]))
        assert run["status"] == "QUEUED"
        assert run["source"] == "desk"
        assert run["task_id"] is None

    def test_a_second_press_while_one_is_in_flight_is_409_naming_the_run(self, client) -> None:  # noqa: ANN001
        first = client.post("/twt/scan").json()["run_id"]
        answer = client.post("/twt/scan")
        assert answer.status_code == 409
        assert f"Scan {first} is queued" in answer.json()["detail"]

    def test_a_press_a_second_after_a_finished_run_is_429_with_retry_after(
        self,
        client,  # noqa: ANN001
        store,  # noqa: ANN001
        clock,  # noqa: ANN001
    ) -> None:
        """The 429 is the *other* refusal, and it needs a run that is no longer in flight.

        Without finishing the first row this would answer 409 and the rate limit would never be
        exercised — which is how a rate-limit test quietly stops testing a rate limit.
        """
        run_id = client.post("/twt/scan").json()["run_id"]
        store.conn.execute("UPDATE tw_scan_run SET status = 'DONE' WHERE id = ?", (run_id,))
        clock.advance(1)
        answer = client.post("/twt/scan")
        assert answer.status_code == 429
        assert answer.headers["Retry-After"] == "59"
        assert "one a minute is the limit" in answer.json()["detail"]

    def test_a_press_after_the_minute_is_accepted_again(self, client, store, clock) -> None:  # noqa: ANN001
        run_id = client.post("/twt/scan").json()["run_id"]
        store.conn.execute("UPDATE tw_scan_run SET status = 'DONE' WHERE id = ?", (run_id,))
        clock.advance(61)
        answer = client.post("/twt/scan")
        assert answer.status_code == 202
        assert answer.json()["run_id"] != run_id

    def test_a_stale_in_flight_row_does_not_lock_the_button_forever(self, client, clock) -> None:  # noqa: ANN001
        """A worker that died mid-scan leaves a ``RUNNING`` row. Ten minutes later the button
        works again, and the dead row is **left alone** rather than rewritten — the desk does not
        know whether that worker is coming back."""
        first = client.post("/twt/scan").json()["run_id"]
        clock.advance(W.SCAN_STALE_AFTER_SECONDS + 1)
        answer = client.post("/twt/scan")
        assert answer.status_code == 202
        assert answer.json()["run_id"] != first
        assert client.get(f"/twt/scan/{first}").json()["status"] == "QUEUED"

    def test_reading_a_run_gives_the_swing_vocabulary(self, client, store) -> None:  # noqa: ANN001
        run_id = client.post("/twt/scan").json()["run_id"]
        for status in ("QUEUED", "RUNNING", "DONE", "FAILED"):
            store.conn.execute("UPDATE tw_scan_run SET status = ? WHERE id = ?", (status, run_id))
            answer = client.get(f"/twt/scan/{run_id}")
            assert answer.status_code == 200
            assert answer.json()["status"] == status

    def test_a_finished_run_carries_its_session_and_its_funnel(self, client, store) -> None:  # noqa: ANN001
        run_id = client.post("/twt/scan").json()["run_id"]
        store.conn.execute(
            "UPDATE tw_scan_run SET status = 'DONE', session_date = '2026-09-10', "
            "detail = '{\"signals\": 2, \"status\": \"OK\"}' WHERE id = ?",
            (run_id,),
        )
        body = client.get(f"/twt/scan/{run_id}").json()
        assert body["session_date"] == "2026-09-10"
        assert body["detail"] == {"signals": 2, "status": "OK"}
        assert body["error"] is None

    def test_a_failed_run_carries_its_reason(self, client, store) -> None:  # noqa: ANN001
        run_id = client.post("/twt/scan").json()["run_id"]
        store.conn.execute(
            "UPDATE tw_scan_run SET status = 'FAILED', error = 'ValueError: nothing published' "
            "WHERE id = ?",
            (run_id,),
        )
        body = client.get(f"/twt/scan/{run_id}").json()
        assert body["status"] == "FAILED"
        assert body["error"] == "ValueError: nothing published"

    def test_an_unknown_run_is_404(self, client) -> None:  # noqa: ANN001
        assert client.get("/twt/scan/99999").status_code == 404

    def test_another_users_run_is_404_and_not_somebody_elses_row(self, client, store) -> None:  # noqa: ANN001
        """One tenant today, and the scoping is still written. A surface that serves by id
        without a user clause is a multi-tenant bug waiting for P4 to make it reachable."""
        store.conn.execute(
            "INSERT INTO tw_scan_run (user_id, requested_at, status) VALUES (2, ?, 'DONE')",
            (NOW.isoformat(),),
        )
        other = store.conn.execute("SELECT id FROM tw_scan_run WHERE user_id = 2").fetchone()["id"]
        assert client.get(f"/twt/scan/{other}").status_code == 404


# =========================================================================================
# `docs/twt/02` §3 — the scan is money-free, and the CSRF rule still applies to it
# =========================================================================================
class TestTheScanIsMoneyFree:
    def test_the_scan_never_asks_for_a_gateway_or_a_quote(self, client) -> None:  # noqa: ANN001
        """The fixture's ``twt_gateway`` and ``last_price`` both raise. The 202 is the proof."""
        assert client.post("/twt/scan").status_code == 202

    def test_the_scan_writes_one_row_and_nothing_else(self, client, store) -> None:  # noqa: ANN001
        """Non-vacuity for the test above: the route did do something.

        The scenario database contains **only** ``tw_scan_run``, so a route that reached for a
        config row, a position or a plan would raise ``no such table`` rather than pass quietly.
        """
        client.post("/twt/scan")
        assert store.conn.execute("SELECT count(*) FROM tw_scan_run").fetchone()[0] == 1

    def test_a_scan_post_without_an_origin_header_is_refused(self, store, monkeypatch) -> None:  # noqa: ANN001
        """``app/core/websec.py``'s rule covers the new POST too, which is the whole reason
        `FIRST-LIVE-MORNING` §1 writes it down: the 403 reads exactly like a permissions
        problem and is not one."""
        from fastapi.testclient import TestClient

        monkeypatch.setattr(W, "open_store", lambda: contextlib.nullcontext(store))
        monkeypatch.setattr(W, "_now", lambda: NOW)
        client = TestClient(M.app)
        client.headers.pop("origin", None)
        answer = client.post("/twt/scan")
        assert answer.status_code == 403
        assert store.conn.execute("SELECT count(*) FROM tw_scan_run").fetchone()[0] == 0

    def test_reading_a_run_is_a_get_and_needs_no_origin(self, store, monkeypatch) -> None:  # noqa: ANN001
        from fastapi.testclient import TestClient

        monkeypatch.setattr(W, "open_store", lambda: contextlib.nullcontext(store))
        monkeypatch.setattr(W, "_now", lambda: NOW)
        client = TestClient(M.app)
        client.headers.pop("origin", None)
        assert client.get("/twt/scan/1").status_code == 404  # not 403


# =========================================================================================
# The copied constants, checked rather than trusted
# =========================================================================================
class TestTheTwoCopiesAgree:
    """The desk cannot import ``baskfy_worker``. So the numbers are copied — and read back.

    VB12 made the same copy and wrote the same test (`tests/test_vbt_rescan_desk.py`). A third
    sleeve inventing its own window would be three answers to one question, and the answer that
    matters is the one the worker uses when it decides a row is stale.
    """

    def _worker_source(self) -> str:
        path = (
            Path(__file__).resolve().parents[2]
            / "decile-blueprint"
            / "services"
            / "worker"
            / "src"
            / "baskfy_worker"
            / "tasks"
            / "twt_scan.py"
        )
        assert path.exists(), path
        return path.read_text(encoding="utf-8")

    def _constant(self, source: str, name: str) -> int:
        found = re.search(rf"^{name}: Final = (\d+)$", source, re.MULTILINE)
        assert found is not None, f"{name} is not a module constant in that file any more"
        return int(found.group(1))

    def test_the_stale_window_is_the_same_number_on_both_sides(self) -> None:
        assert W.SCAN_STALE_AFTER_SECONDS == self._constant(
            self._worker_source(), "STALE_AFTER_SECONDS"
        )

    def test_the_minimum_interval_is_the_same_number_on_both_sides(self) -> None:
        assert W.SCAN_MIN_INTERVAL_SECONDS == self._constant(
            self._worker_source(), "MIN_INTERVAL_SECONDS"
        )

    def test_the_in_flight_states_are_the_same_pair_on_both_sides(self) -> None:
        source = self._worker_source()
        assert W.SCAN_IN_FLIGHT == ("QUEUED", "RUNNING")
        assert 'IN_FLIGHT: Final[tuple[str, ...]] = ("QUEUED", "RUNNING")' in source
