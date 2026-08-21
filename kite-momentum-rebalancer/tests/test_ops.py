"""Operations page: the allowlist boundary, parameter validation, and the job log.

This page runs subprocesses on the machine holding the broker credentials, so most of
these tests are about what it REFUSES.
"""
from __future__ import annotations

import html
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import config as C
from app import main as M
from app.analytics import db
from app.analytics import ops as O


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    path = str(tmp_path / "p.db")
    monkeypatch.setattr(C, "DB_PATH", path)
    with db.connect(path) as c:
        db.migrate(c)
        yield c


@pytest.fixture()
def client(monkeypatch, conn):
    monkeypatch.setattr(M, "_kite", None)
    return TestClient(M.app)


def wait(conn, timeout=25):
    for _ in range(int(timeout * 10)):
        if O.running_job(conn) is None:
            return
        time.sleep(0.1)
    raise AssertionError("job did not finish")


# =====================================================================================
# the allowlist boundary
# =====================================================================================
def test_an_unknown_operation_is_refused(conn):
    with pytest.raises(O.OpsError, match="not a known operation"):
        O.start(conn, "definitely_not_real")


@pytest.mark.parametrize("attempt", [
    "rm -rf /", "daily; whoami", "daily && curl evil.example", "daily | nc host 1",
    "$(whoami)", "../../bin/sh",
])
def test_command_injection_cannot_name_an_operation(conn, attempt):
    """There is no shell and no free-form field: an injection string is just an unknown
    name. This asserts the property rather than trusting it."""
    with pytest.raises(O.OpsError, match="not a known operation"):
        O.start(conn, attempt)


def test_no_operation_uses_a_shell():
    """shell=True anywhere would make every argument an injection surface."""
    src = open("app/analytics/ops.py").read()
    assert "shell=True" not in src
    assert "shell=False" in src
    assert "os.system" not in src and "subprocess.call(" not in src


def test_every_operation_builds_a_list_not_a_string():
    for op in O.OPERATIONS:
        argv = op.argv({p.key: p.default for p in op.params})
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        assert argv[0] == O.PYTHON


def test_no_two_operations_run_the_same_command():
    """Two buttons with different labels and identical argv means at least one label is
    lying about what it does — which is how index_update shipped as a full daily run."""
    seen = {}
    for op in O.OPERATIONS:
        argv = tuple(op.argv({p.key: p.default for p in op.params}))
        assert argv not in seen, f"{op.name} runs the same command as {seen[argv]}"
        seen[argv] = op.name


def test_the_index_step_runs_only_the_index_step():
    argv = O.BY_NAME["index_update"].argv({})
    assert "--only" in argv and "index history" in argv


def test_no_operation_can_place_an_order():
    """Execution stays on the desk behind its confirmation gate."""
    for op in O.OPERATIONS:
        argv = " ".join(op.argv({p.key: p.default for p in op.params}))
        assert "execute" not in argv.lower()
        assert "place_order" not in argv


# =====================================================================================
# parameter validation
# =====================================================================================
def test_a_file_parameter_cannot_escape_the_uploads_directory(conn):
    with pytest.raises(O.OpsError):
        O.start(conn, "scoring_smoke", {"scan": "../../../etc/passwd"})


def test_a_file_parameter_must_exist(conn):
    with pytest.raises(O.OpsError, match="does not exist"):
        O.start(conn, "daily", {"scan": "nope.csv"})


def test_a_date_parameter_must_be_iso(conn):
    with pytest.raises(O.OpsError, match="not a YYYY-MM-DD date"):
        O.start(conn, "backtest", {"start": "yesterday"})


def test_an_invalid_parameter_starts_nothing(conn):
    with pytest.raises(O.OpsError):
        O.start(conn, "backtest", {"start": "not-a-date"})
    assert conn.execute("SELECT COUNT(*) c FROM ops_jobs").fetchone()["c"] == 0


def test_a_flag_parameter_reaches_argv(conn):
    op = O.BY_NAME["snapshot"]
    assert "--force" in op.argv({"force": "true"})
    assert "--force" not in op.argv({"force": "false"})


def test_a_valid_scan_is_accepted(tmp_path, monkeypatch, conn):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "scan.csv").write_text("symbol\nX\n")
    monkeypatch.setattr(O, "UPLOADS", uploads)
    argv = O.BY_NAME["daily"].argv({"scan": "scan.csv"})
    assert "--scan" in argv and argv[-1].endswith("scan.csv")


# =====================================================================================
# job lifecycle
# =====================================================================================
def test_a_job_is_recorded_with_its_exact_command(conn):
    O.start(conn, "db_info")
    wait(conn)
    job = O.history(conn)[0]
    assert job["name"] == "db_info"
    assert json.loads(job["argv_json"]) == ["-m", "app.analytics.db", "--info"]
    assert job["status"] == "ok" and job["exit_code"] == 0
    assert job["output"] and job["duration_s"] >= 0


def test_a_failing_job_is_recorded_as_failed(conn, monkeypatch):
    bad = O.Operation("bad", "Bad", "Diagnostics", "always fails",
                      lambda v: ["-c", "import sys; sys.exit(3)"], timeout=30)
    monkeypatch.setitem(dict(O.BY_NAME), "bad", bad)
    monkeypatch.setattr(O, "BY_NAME", {**O.BY_NAME, "bad": bad})
    O.start(conn, "bad")
    wait(conn)
    job = O.history(conn)[0]
    assert job["status"] == "failed" and job["exit_code"] == 3


def test_a_second_job_is_refused_while_one_runs(conn, monkeypatch):
    slow = O.Operation("slow", "Slow", "Diagnostics", "sleeps",
                       lambda v: ["-c", "import time; time.sleep(2)"], timeout=30)
    monkeypatch.setattr(O, "BY_NAME", {**O.BY_NAME, "slow": slow})
    O.start(conn, "slow")
    try:
        with pytest.raises(O.OpsError, match="already running"):
            O.start(conn, "db_info")
    finally:
        wait(conn)


def test_the_lock_is_released_after_a_job(conn):
    O.start(conn, "db_info")
    wait(conn)
    O.start(conn, "db_info")          # must not raise
    wait(conn)
    assert len(O.history(conn)) == 2


def test_running_job_reports_in_flight_state(conn, monkeypatch):
    slow = O.Operation("slow", "Slow", "Diagnostics", "sleeps",
                       lambda v: ["-c", "import time; time.sleep(1.5)"], timeout=30)
    monkeypatch.setattr(O, "BY_NAME", {**O.BY_NAME, "slow": slow})
    O.start(conn, "slow")
    assert O.running_job(conn)["name"] == "slow"
    wait(conn)
    assert O.running_job(conn) is None


def test_a_job_orphaned_by_a_restart_does_not_brick_the_page(conn):
    """A daemon worker dies with the server. Without reclaim, the stale 'running' row
    disables every button forever."""
    conn.execute("INSERT INTO ops_jobs(name, argv_json, status, started_at)"
                 " VALUES('daily','[]','running','2026-08-14T10:00:00')")
    conn.commit()
    assert O.running_job(conn) is None            # reclaimed, not reported as running
    job = O.history(conn)[0]
    assert job["status"] == "interrupted"
    assert "run it again" in job["output"]


def test_reclaim_leaves_a_genuinely_running_job_alone(conn, monkeypatch):
    slow = O.Operation("slow", "Slow", "Diagnostics", "sleeps",
                       lambda v: ["-c", "import time; time.sleep(1.5)"], timeout=30)
    monkeypatch.setattr(O, "BY_NAME", {**O.BY_NAME, "slow": slow})
    O.start(conn, "slow")
    assert O.reclaim_orphans(conn) == 0
    assert O.running_job(conn) is not None
    wait(conn)
    assert O.history(conn)[0]["status"] == "ok"


def test_last_run_gives_the_latest_outcome_per_operation(conn):
    O.start(conn, "db_info"); wait(conn)
    last = O.last_run(conn)
    assert last["db_info"]["status"] == "ok"


# =====================================================================================
# the page
# =====================================================================================
def test_ops_page_renders_every_operation(client):
    # unescape first: a label with an apostrophe is escaped in the markup, and comparing
    # the raw string would fail on presentation rather than on a missing operation.
    text = html.unescape(client.get("/ops").text)
    for op in O.OPERATIONS:
        assert op.label in text, f"{op.name} is not on the page"


def test_page_shows_the_command_for_each_operation(client):
    text = client.get("/ops").text
    assert "python -m scripts.daily" in text
    assert "python -m app.analytics.db --info" in text


def test_page_has_no_free_form_command_field():
    html = open("app/templates/ops.html").read()
    # every input must be a named, typed parameter or the hidden operation name
    assert 'name="cmd"' not in html and 'name="command"' not in html
    assert "remote code execution" in html.lower(), "the constraint must be stated"


def test_unknown_operation_via_the_route_redirects_with_an_error(client):
    r = client.post("/ops/run", data={"op": "whoami"}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]


def test_a_read_only_operation_runs_through_the_route(client, conn):
    r = client.post("/ops/run", data={"op": "db_info"}, follow_redirects=False)
    assert r.status_code == 303
    wait(conn)
    assert O.history(conn)[0]["status"] == "ok"


def test_job_detail_page_shows_output(client, conn):
    O.start(conn, "db_info"); wait(conn)
    job_id = O.history(conn)[0]["id"]
    text = client.get(f"/ops/job/{job_id}").text
    assert "schema version" in text.lower() or "db " in text.lower()


def test_unknown_job_is_404(client):
    assert client.get("/ops/job/999999").status_code == 404


def test_ops_data_lists_operations(client):
    d = client.get("/ops/data").json()
    assert len(d["operations"]) == len(O.OPERATIONS)
    assert all({"name", "label", "group", "cli"} <= set(o) for o in d["operations"])
