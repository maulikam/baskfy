"""Grade the Rust desk against the Python desk (DECISIONS-RS R8.3): same requests, same replay
Kite, same clock, same ids — every answer and every side effect compared.

    python3 tools/parity/desk_diff.py --py-port 18501 --rs-port 18502 \\
        [--cases 'tools/parity/desk_cases/*.json'] [--plans] [--backend postgres|sqlite] \\
        [--python-both] [--no-build] [--report /tmp/desk-report.txt]

Each side runs from its own empty run directory (``app`` symlinked to the worktree's desk
``app``, a fresh ``data/``), with ``DRY_RUN=true``, the replay Kite over ``--fixture``
(``desk_fake_kite.py`` / ``fake_kite.rs``), a settable clock and the harness id sequence, and
its own database: on ``--backend postgres`` (the deployment's) a bed clone ``rsd_<run>_{py,rs}``
cloned ``rsd_tpl_desk`` → py, template dropped, py → rs (so at most two ``rsd_*`` exist while
the desks run); on ``sqlite`` a file in the run directory. Redis indices 8–15 only; ports
18500–18799 only; the machine-wide lock ``<main>/rust/target/nightly_diff.lock`` is held for the
whole run, and a clone waits while six non-``baskfy`` databases already exist.

Compared, per case (in file order, each sent to Python then Rust): status; ``content-type``,
``location`` (port-normalised), ``cache-control``, ``www-authenticate``, ``allow``,
``content-disposition``; the body — JSON with key order and int/float/bool kept apart, anything
else byte for byte — after the case's ``"mask"`` (a key or a JSON pointer; every mask printed).
``"capture": {"plan_id": "json:/plan_id"}`` keeps a value per side and ``${plan_id}`` uses it
later. A file's ``"db_mask": {"public.risk_ledger": ["updated_at"]}`` compares a server-stamped
column (``now()`` — the injected clock cannot reach the database) as ``<null>``/``<set>``; every
mask is printed. ``"plan": true`` also compares the SHA-256 of the body (``plan_digest``). After the scenario:
the replay Kite's **order/GTT calls** (must be EQUAL; in DRY_RUN normally none), the order
journal and every other file under ``data/`` (byte for byte), the database (Postgres: every
table either clone touched in ``desk``/``public``, as change sets against the template's rows;
SQLite: every table), both Redis indices, and the mail each side sent. Kite *reads* are
reported, not graded, unless ``--strict-reads``.

Case file: ``{"cases": [{"name", "method", "path", "query", "form", "files": {"field":
{"path", "filename"}}, "json", "headers", "clock", "advance", "mask", "plan", "capture",
"sql", "note"}]}``; ``"sql"`` is a list of statements run on each side's clone before the case
(Postgres only: seed rows both desks then read);
``"clock"`` is RFC 3339 or a preset (``pre_open`` 08:55, ``open`` 09:16, ``mid`` 11:00, ``close``
15:29, ``after_close`` 16:05 — all on Friday 30 Oct 2026). ``--plans`` adds one ``POST
/analyze`` per ``data/uploads/*.csv`` of the regression corpus (read only) and every
``desk_cases/plans_*.json``.

``--python-both`` runs the Python desk on both sides: the harness proving itself (everything
must be EQUAL before a Rust difference means anything).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import glob
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
MAIN = Path("/Volumes/T7-Work/Dev/projects/baskfy")
DESK = REPO / "kite-momentum-rebalancer"
UPLOADS = MAIN / "kite-momentum-rebalancer" / "data" / "uploads"
DESK_PY = MAIN / "kite-momentum-rebalancer" / ".venv" / "bin" / "python"
LOCK = MAIN / "rust" / "target" / "nightly_diff.lock"
RUST = REPO / "rust"
RS_BIN = RUST / "target" / "debug" / "baskfy-desk"
BED = "postgresql://baskfy:rs-test-only@127.0.0.1:55432/{db}"
REDIS = "redis://127.0.0.1:56379"
PORTS = range(18500, 18800)
MAX_EXTRA_DBS = 6
SYSTEM_DBS = {"baskfy", "postgres", "template0", "template1"}
FRIDAY = "2026-10-30"
PRESETS = {"pre_open": "08:55:00", "open": "09:16:00", "mid": "11:00:00", "close": "15:29:00",
           "after_close": "16:05:00"}
WRITE_ROUTES = ("order.place", "order.modify", "order.cancel", "gtt.place", "gtt.modify", "gtt.delete")
HEADERS = ("content-type", "location", "cache-control", "www-authenticate", "allow", "content-disposition")

sys.path.insert(0, str(HERE))
import api_writes as aw  # noqa: E402 - tools/parity, stdlib only

aw.PREFIX = "rsd_"  # every database this harness creates, writes or drops is rsd_*


def clock_iso(value: str) -> str:
    return f"{FRIDAY}T{PRESETS[value]}+05:30" if value in PRESETS else value


# ---------------------------------------------------------------------------------------------
# JSON with key order and types
# ---------------------------------------------------------------------------------------------


class Ordered(list[tuple[str, Any]]):
    """A JSON object as its (key, value) pairs, so key order is compared too."""


def parse_ordered(raw: bytes) -> Any:  # noqa: ANN401
    return json.loads(raw, object_pairs_hook=Ordered)


def masked(value: Any, masks: list[str], path: str = "") -> Any:  # noqa: ANN401
    if isinstance(value, Ordered):
        return Ordered((k, "<masked>" if (k in masks or f"{path}/{k}" in masks) else masked(v, masks, f"{path}/{k}"))
                       for k, v in value)
    if isinstance(value, list):
        return ["<masked>" if f"{path}/{i}" in masks else masked(v, masks, f"{path}/{i}") for i, v in enumerate(value)]
    return value


def typed(value: Any) -> Any:  # noqa: ANN401
    if isinstance(value, Ordered):
        return [["<object>"]] + [[k, typed(v)] for k, v in value]
    if isinstance(value, list):
        return [typed(v) for v in value]
    if isinstance(value, bool):
        return {"bool": value}
    if isinstance(value, int):
        return {"int": value}
    if isinstance(value, float):
        return {"float": repr(value)}
    return value


# ---------------------------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------------------------


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


@dataclass
class Side:
    name: str
    port: int
    run: Path
    env: dict[str, str]
    redis_db: int
    db_name: str | None = None
    proc: subprocess.Popen[bytes] | None = None
    responses: list[dict[str, Any]] = field(default_factory=list)

    @property
    def kite_log(self) -> Path:
        return self.run / "kite_calls.jsonl"


def prepare_run_dir(base: Path, name: str) -> Path:
    run = base / name
    (run / "data" / "outputs").mkdir(parents=True)
    (run / "data" / "uploads").mkdir(parents=True)
    os.symlink(DESK / "app", run / "app")
    return run


def side_env(args: argparse.Namespace, side: Side, smtp_port: int) -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"), "LANG": "en_US.UTF-8",
        "TZ": "Asia/Kolkata",
        "PYTHONPATH": os.pathsep.join(str(p) for p in [
            DESK, REPO / "decile-blueprint/packages/core/src", REPO / "decile-blueprint/packages/providers/src",
            REPO / "decile-blueprint/packages/execution/src", HERE]),
        "DRY_RUN": "true",
        "KITE_API_KEY": "replay-api-key", "KITE_API_SECRET": "replay-api-secret",
        "KITE_TOKEN_ENCRYPTION_KEY": "", "FORCE_IPV4": "false",
        "DESK_ALLOWED_HOSTS": "127.0.0.1,localhost", "DESK_PORT": str(side.port),
        "BASKFY_REDIS_URL": f"{REDIS}/{side.redis_db}",
        "DESK_SMTP_HOST": "127.0.0.1", "DESK_SMTP_PORT": str(smtp_port), "DESK_SMTP_STARTTLS": "false",
        "DESK_NOTIFY_FROM": "desk@example.invalid", "DESK_NOTIFY_TO": "operator@example.invalid",
        "TOKEN_FILE": str(side.run / "data" / ".kite_token.json"),
    }
    if args.backend == "postgres":
        url = BED.format(db=side.db_name)
        env.update({"DESK_DB_BACKEND": "postgres", "DESK_DB_SCHEMA": "desk", "DESK_DATABASE_URL": url,
                    "SCREENER_DATABASE_URL": url})
    else:
        env.update({"DESK_DB_BACKEND": "sqlite", "DB_PATH": str(side.run / "data" / "portfolio.db"),
                    "SCREENER_DATABASE_URL": "postgresql://nobody@127.0.0.1:1/none"})
    for kv in args.env:
        k, _, v = kv.partition("=")
        if k in ("DRY_RUN",) and v.lower() == "false":
            raise SystemExit("desk_diff never runs with DRY_RUN=false")
        env[k] = v
    return env


def start(side: Side, args: argparse.Namespace, python: bool) -> None:
    log = open(side.run / "server.log", "wb")  # noqa: SIM115 - held for the process's life
    clock = clock_iso(args.clock)
    if python:
        cmd = [str(DESK_PY), str(HERE / "desk_harness.py"), "--port", str(side.port), "--fixture", str(args.fixture),
               "--kite-log", str(side.kite_log), "--clock", clock, "--seed", args.seed]
    else:
        cmd = [str(RS_BIN), "--host", "127.0.0.1", "--port", str(side.port), "--root", str(side.run),
               "--harness", str(args.fixture), "--kite-log", str(side.kite_log), "--harness-clock", clock,
               "--harness-seed", args.seed]
    side.proc = subprocess.Popen(cmd, cwd=side.run, env=side.env, stdout=log, stderr=subprocess.STDOUT)  # noqa: S603
    deadline = time.monotonic() + args.start_timeout
    while time.monotonic() < deadline:
        if side.proc.poll() is not None:
            raise SystemExit(f"{side.name} desk exited during start; see {side.run / 'server.log'}")
        with contextlib.suppress(OSError):
            if request(side, "GET", "/__harness/kite-calls", headers=harness_auth(side))["status"] == 200:
                return
        time.sleep(0.3)
    raise SystemExit(f"{side.name} desk did not come up in {args.start_timeout}s")


def harness_auth(side: Side) -> dict[str, str] | None:
    """The harness's own calls under ``--env DESK_PASSWORD=...`` (exec_guards' password variant):
    every path but ``/status`` is behind HTTP Basic, the harness routes included. Case requests
    never get it — they say for themselves what they send."""
    password = (side.env or {}).get("DESK_PASSWORD")
    if not password:
        return None
    return {"Authorization": "Basic " + base64.b64encode(f"harness:{password}".encode()).decode()}


def stop(side: Side) -> None:
    if side.proc and side.proc.poll() is None:
        side.proc.terminate()
        try:
            side.proc.wait(10)
        except subprocess.TimeoutExpired:
            side.proc.kill()


# ---------------------------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------------------------


def multipart(form: dict[str, str], files: dict[str, dict[str, str]]) -> tuple[bytes, str]:
    boundary = "deskdiff" + hashlib.sha256(json.dumps([form, files], sort_keys=True).encode()).hexdigest()[:24]
    parts: list[bytes] = []
    for k, v in form.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, f in files.items():
        data = Path(f["path"]).read_bytes()  # read only: the corpus is never written
        name = f.get("filename") or Path(f["path"]).name
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\n'
                     f"Content-Type: text/csv\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def request(side: Side, method: str, path: str, *, query: dict[str, Any] | None = None,  # noqa: PLR0913
            form: dict[str, str] | None = None, files: dict[str, dict[str, str]] | None = None,
            body_json: Any = None, headers: dict[str, str] | None = None) -> dict[str, Any]:  # noqa: ANN401
    url = path + ("?" + urllib.parse.urlencode(query, doseq=True) if query else "")
    hdrs = {"Host": f"127.0.0.1:{side.port}"}
    if method not in ("GET", "HEAD", "OPTIONS"):
        hdrs["Origin"] = f"http://127.0.0.1:{side.port}"
    body: bytes | None = None
    if files:
        body, ctype = multipart(form or {}, files)
        hdrs["Content-Type"] = ctype
    elif form is not None:
        body = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif body_json is not None:
        body = json.dumps(body_json).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    conn = http.client.HTTPConnection("127.0.0.1", side.port, timeout=120)
    try:
        conn.request(method, url, body=body, headers=hdrs)
        r = conn.getresponse()
        return {"status": r.status, "headers": {k.lower(): v for k, v in r.getheaders()}, "body": r.read()}
    finally:
        conn.close()


def pointer(doc: Any, ptr: str) -> str:  # noqa: ANN401
    for part in [p for p in ptr.split("/") if p]:
        doc = doc[int(part)] if isinstance(doc, list) else doc[part]
    return str(doc)


def substitute(value: Any, variables: dict[str, str]) -> Any:  # noqa: ANN401
    """``${name}`` → this side's captured value (``"capture": {"name": "json:/ptr"}``)."""
    if isinstance(value, str):
        for k, v in variables.items():
            value = value.replace("${" + k + "}", v)
        return value
    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    return value


def view(resp: dict[str, Any], port: int, masks: list[str], run: Path | None = None) -> dict[str, Any]:
    if run is not None:
        # Each side runs from its own temporary directory (``…/py``, ``…/rs``); a page that prints
        # a path under it (the settings page's KITE_TOKEN_FILE) is compared with the run
        # directory written ``<run>``. Normalised like the port, never masked.
        resp = {**resp, "body": resp["body"].replace(str(run).encode(), b"<run>")}
    heads = {}
    for h in HEADERS:
        if h in resp["headers"]:
            heads[h] = resp["headers"][h].replace(f":{port}", ":<port>")
    body: Any
    if "json" in resp["headers"].get("content-type", ""):
        try:
            body = typed(masked(parse_ordered(resp["body"]), masks))
        except ValueError:
            body = resp["body"].decode("utf-8", "replace")
    else:
        body = resp["body"].decode("utf-8", "replace").replace(f":{port}", ":<port>")
    return {"status": resp["status"], "headers": heads, "body": body}


# ---------------------------------------------------------------------------------------------
# Side effects
# ---------------------------------------------------------------------------------------------


def kite_calls(side: Side) -> list[dict[str, Any]]:
    if not side.kite_log.exists():
        return []
    return [json.loads(line) for line in side.kite_log.read_text().splitlines() if line.strip()]


def files_under(side: Side) -> dict[str, str]:
    out = {}
    root = side.run / "data"
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_file() and not rel.startswith("portfolio.db") and not rel.startswith(".kite_token"):
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def sqlite_dump(side: Side) -> dict[str, Any]:
    path = side.run / "data" / "portfolio.db"
    if not path.exists():
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            rows = conn.execute(f'SELECT * FROM "{name}"').fetchall()  # noqa: S608 - names from sqlite_master
            out[name] = sorted(json.dumps(list(r), default=repr) for r in rows)
        out["<user_version>"] = conn.execute("PRAGMA user_version").fetchone()[0]
        return out
    finally:
        conn.close()


def pg_touched(db: str) -> set[tuple[str, str]]:
    rows = aw.json_rows(db, "SELECT schemaname, relname FROM pg_stat_user_tables WHERE schemaname IN ('desk','public') "
                            "AND n_tup_ins + n_tup_upd + n_tup_del > 0")
    return {(r["schemaname"], r["relname"]) for r in rows}


def pg_rows(db: str, schema: str, table: str, mask: list[str]) -> list[str]:
    """Every row as JSON; a masked column (one the server stamps with ``now()``, which the
    injected clock cannot reach) compares as ``<null>`` or ``<set>``."""
    rows = aw.json_rows(db, f'SELECT * FROM "{schema}"."{table}"')
    for r in rows:
        for col in mask:
            if col in r:
                r[col] = "<null>" if r[col] is None else "<set>"
    return sorted(json.dumps(r, sort_keys=False) for r in rows)


def pg_changes(base: list[str], side: list[str]) -> dict[str, list[str]]:
    b, s = set(base), set(side)
    return {"added": sorted(s - b), "removed": sorted(b - s)}


# ---------------------------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------------------------


def extra_dbs() -> list[str]:
    names = aw.psql("postgres", "SELECT datname FROM pg_database").split()
    return [n for n in names if n not in SYSTEM_DBS]


def wait_for_db_room(needed: int, wait_seconds: float) -> None:
    deadline = time.monotonic() + wait_seconds
    while len(extra_dbs()) + needed > MAX_EXTRA_DBS:
        if time.monotonic() > deadline:
            raise SystemExit(f"no room for {needed} more databases ({len(extra_dbs())} exist); try later")
        print(f"  {len(extra_dbs())} non-baskfy databases exist; waiting for room for {needed}")
        time.sleep(10)


def load_db_masks(patterns: list[str]) -> dict[str, list[str]]:
    """Every case file's ``"db_mask": {"schema.table": [columns]}``, merged."""
    out: dict[str, list[str]] = {}
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            for table, cols in (json.loads(Path(path).read_text()).get("db_mask") or {}).items():
                out.setdefault(table, [])
                out[table] += [c for c in cols if c not in out[table]]
    return out


def load_cases(patterns: list[str], plans: bool) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            if Path(path).name.startswith("plans_") and not plans:
                continue
            for c in json.loads(Path(path).read_text())["cases"]:
                cases.append({**c, "file": Path(path).name})
    if plans:
        for path in sorted(UPLOADS.glob("*.csv")):
            cases.append({"name": f"analyze {path.name}", "file": "<plans>", "method": "POST", "path": "/analyze",
                          "files": {"scan": {"path": str(path), "filename": path.name}}, "plan": True,
                          "clock": "open"})
        for kind in ("swing MORNING", "swing LIVE", "twt MORNING", "twt LIVE", "vbt", "fno exits"):
            if not any(kind.split()[0] in c.get("file", "") for c in cases):
                print(f"  plan kind '{kind}': no builder case yet (desk_cases/plans_<sleeve>.json, its lane's)")
    return cases


def run(args: argparse.Namespace) -> int:  # noqa: C901, PLR0912, PLR0915
    # Both desks start with cwd set to their run directory, so a relative fixture would resolve there.
    args.fixture = str(Path(args.fixture).resolve())
    for p in (args.py_port, args.rs_port, args.smtp_port):
        if p not in PORTS or port_open(p):
            raise SystemExit(f"port {p} is outside 18500-18799 or already listening")
    for db in (args.redis_db_py, args.redis_db_rs):
        if not 8 <= db <= 15:
            raise SystemExit("Redis indices 8-15 only")
    if not args.python_both and not args.no_build:
        subprocess.run(["nice", "-n", "10", str(Path.home() / ".cargo/bin/cargo"), "build", "-p", "baskfy-desk"],  # noqa: S603, S607
                       cwd=RUST, check=True,
                       env={**os.environ, "CARGO_TARGET_DIR": str(RUST / "target"), "CARGO_BUILD_JOBS": "4"})
    cases = load_cases(args.cases, args.plans)
    db_masks = load_db_masks(args.cases)
    base = Path(tempfile.mkdtemp(prefix="desk_diff_"))
    run_id = uuid.uuid4().hex[:8]
    py = Side("python", args.py_port, prepare_run_dir(base, "py"), {}, args.redis_db_py)
    rs = Side("python-2" if args.python_both else "rust", args.rs_port, prepare_run_dir(base, "rs"), {}, args.redis_db_rs)
    created: list[str] = []
    lines: list[str] = []
    diffs = 0
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK, "a+")  # noqa: SIM115
    print(f"waiting for {LOCK}")
    fcntl.flock(lock, fcntl.LOCK_EX)
    sink = aw.MailSink(args.smtp_port)
    try:
        if args.backend == "postgres":
            aw.check_container()
            tpl = "rsd_tpl_desk"
            wait_for_db_room(0 if aw.db_exists(tpl) else 1, args.wait_db)
            aw.ensure_lane_template("desk", args.wait_db)
            created.append(tpl)
            # tpl → py, drop tpl, py → rs: never more than three rsd_* at once, two at the peak
            # beside everyone else's (the bed's six-database limit).
            wait_for_db_room(1, args.wait_db)
            py.db_name, rs.db_name = f"rsd_{run_id}_py", f"rsd_{run_id}_rs"
            aw.clone(tpl, py.db_name)
            created.append(py.db_name)
            if not args.keep_template:
                aw.drop(tpl)
                created.remove(tpl)
            wait_for_db_room(1, args.wait_db)
            # TimescaleDB's scheduler connects to every new database; close py while it is copied.
            aw.psql("postgres", f"ALTER DATABASE {py.db_name} WITH ALLOW_CONNECTIONS false;")
            try:
                aw.clone(py.db_name, rs.db_name)
                created.append(rs.db_name)
            finally:
                aw.psql("postgres", f"ALTER DATABASE {py.db_name} WITH ALLOW_CONNECTIONS true;")
        for side in (py, rs):
            aw.redis_flush(REDIS, side.redis_db)
        sink.start()
        for side, python in ((py, True), (rs, args.python_both)):
            side.env = side_env(args, side, args.smtp_port)
            start(side, args, python)

        captured: dict[str, dict[str, str]] = {py.name: {}, rs.name: {}}
        for case in cases:
            name = f"{case['file']}::{case['name']}"
            per_side = []
            for side in (py, rs):
                sub = lambda v, side=side: substitute(v, captured[side.name])  # noqa: E731
                # ``"sql": [...]``: statements run on this side's own database before the case
                # (Postgres backend only) — a lane's seed rows, identical on both clones, so they
                # show up as the same added/changed rows on both sides and are graded like any
                # other (lane swing_exec, wave D2). Never on the template, never on ``baskfy``.
                for stmt in case.get("sql") or []:
                    if args.backend == "postgres" and side.db_name:
                        aw.psql(side.db_name, stmt)
                for move in ({"iso": clock_iso(case["clock"])} if case.get("clock") else None,
                             {"advance": case["advance"]} if case.get("advance") else None):
                    if move is not None:
                        moved = request(side, "POST", "/__harness/clock", body_json=move,
                                        headers=harness_auth(side))
                        if moved["status"] != 200:
                            raise SystemExit(f"{side.name}: the clock refused {move}: {moved['status']} {moved['body'][:300]!r}")
                mark = sink.mark()
                resp = request(side, case.get("method", "GET"), sub(case["path"]), query=sub(case.get("query")),
                               form=sub(case.get("form")), files=case.get("files"), body_json=sub(case.get("json")),
                               headers=case.get("headers"))
                for var, where in (case.get("capture") or {}).items():
                    with contextlib.suppress(ValueError, KeyError, IndexError, TypeError):
                        captured[side.name][var] = pointer(json.loads(resp["body"]), where.removeprefix("json:"))
                mails = [aw.mail_view(m, case.get("mail_mask", [])) for m in sink.since(mark)]
                per_side.append((resp, view(resp, side.port, case.get("mask", []), side.run), mails))
            (pr, pv, pm), (rr, rv, rm) = per_side
            same = pv == rv and pm == rm
            if same and case.get("plan"):
                same = hashlib.sha256(pr["body"]).hexdigest() == hashlib.sha256(rr["body"]).hexdigest() or bool(case.get("mask"))
            status = "EQUAL" if same else "DIFF"
            diffs += not same
            lines.append(f"{status}  {name}  [{pv['status']}/{rv['status']}]"
                         + (f"  plan sha256 {hashlib.sha256(pr['body']).hexdigest()[:16]}" if case.get("plan") else "")
                         + (f"  masks={case['mask']}" if case.get("mask") else ""))
            if not same:
                for label, a, b in (("status", pv["status"], rv["status"]), ("headers", pv["headers"], rv["headers"]),
                                    ("body", pv["body"], rv["body"]), ("mail", pm, rm)):
                    if a != b:
                        dump = os.environ.get("DESK_DIFF_DUMP")
                        if dump:  # full bodies for a human (the report truncates at 1,500)
                            stem = Path(dump) / re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{name}.{label}")
                            Path(dump).mkdir(parents=True, exist_ok=True)
                            for side_name, value in ((py.name, a), (rs.name, b)):
                                Path(f"{stem}.{side_name}.json").write_text(
                                    json.dumps(value, ensure_ascii=False, indent=1))
                        lines.append(f"    {label}: {py.name}={json.dumps(a, ensure_ascii=False)[:1500]}")
                        lines.append(f"    {label}: {rs.name}={json.dumps(b, ensure_ascii=False)[:1500]}")
        for side in (py, rs):
            stop(side)

        # --- side effects ------------------------------------------------------------------
        def effect(label: str, a: Any, b: Any) -> None:  # noqa: ANN401
            nonlocal diffs
            same = a == b
            diffs += not same
            lines.append(f"{'EQUAL' if same else 'DIFF'}  <{label}>")
            if not same:
                lines.append(f"    {py.name}: {json.dumps(a, ensure_ascii=False, default=str)[:3000]}")
                lines.append(f"    {rs.name}: {json.dumps(b, ensure_ascii=False, default=str)[:3000]}")

        pk, rk = kite_calls(py), kite_calls(rs)
        strip = lambda calls, writes: [{k: v for k, v in c.items() if k != "seq"} for c in calls  # noqa: E731
                                       if (c["route"] in WRITE_ROUTES) == writes]
        effect("kite order/GTT calls", strip(pk, True), strip(rk, True))
        if args.strict_reads:
            effect("kite reads", strip(pk, False), strip(rk, False))
        else:
            lines.append(f"INFO   <kite reads> python {len(strip(pk, False))}, {rs.name} {len(strip(rk, False))}")
        effect("files under data/ (journal, execution logs, retained scans)", files_under(py), files_under(rs))
        if args.backend == "sqlite":
            effect("sqlite tables", sqlite_dump(py), sqlite_dump(rs))
        else:
            touched = sorted(pg_touched(py.db_name or "") | pg_touched(rs.db_name or ""))
            lines.append(f"INFO   <tables touched> {['.'.join(t) for t in touched]}")
            for schema, table in touched:
                mask = db_masks.get(f"{schema}.{table}", [])
                if mask:
                    lines.append(f"INFO   <db_mask {schema}.{table}> {mask}")
                b = pg_rows("baskfy", schema, table, mask)
                effect(f"table {schema}.{table}", pg_changes(b, pg_rows(py.db_name or "", schema, table, mask)),
                       pg_changes(b, pg_rows(rs.db_name or "", schema, table, mask)))
        # The shared Kite read limiter's departure clocks (SW21) hold time.monotonic() and expire
        # within seconds: timing state, not behaviour. Listed, never graded.
        def graded(dump: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in dump.items() if "ratelimit:" not in k}

        pr_dump = aw.redis_dump(REDIS, py.redis_db, aw.DEFAULT_PRESENCE_ONLY)
        rs_dump = aw.redis_dump(REDIS, rs.redis_db, aw.DEFAULT_PRESENCE_ONLY)
        lines.append(f"INFO   <redis rate-limit clocks, not graded> {sorted(set(pr_dump) - set(graded(pr_dump)))} / "
                     f"{sorted(set(rs_dump) - set(graded(rs_dump)))}")
        effect("redis", graded(pr_dump), graded(rs_dump))
    finally:
        for side in (py, rs):
            stop(side)
        sink.stop()
        if not args.keep:
            for db in created:
                aw.drop(db)
            for side in (py, rs):
                with contextlib.suppress(Exception):
                    aw.redis_flush(REDIS, side.redis_db)
            shutil.rmtree(base, ignore_errors=True)
        else:
            print(f"kept: {base} {created}")
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

    header = (f"desk_diff: {len(cases)} cases, backend {args.backend}, "
              f"{'python vs python' if args.python_both else 'python vs rust'}, clock {clock_iso(args.clock)}, "
              f"seed {args.seed}, fixture {args.fixture}")
    text = "\n".join([header, *lines, f"{diffs} difference(s)"])
    print(text)
    if args.report:
        Path(args.report).write_text(text + "\n")
    return 1 if diffs else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--py-port", type=int, default=18501)
    parser.add_argument("--rs-port", type=int, default=18502)
    parser.add_argument("--smtp-port", type=int, default=18503)
    parser.add_argument("--redis-db-py", type=int, default=8)
    parser.add_argument("--redis-db-rs", type=int, default=9)
    parser.add_argument("--cases", nargs="*", default=[str(HERE / "desk_cases" / "*.json")])
    parser.add_argument("--fixture", default=str(HERE / "desk_fixtures" / "default"))
    parser.add_argument("--backend", choices=("postgres", "sqlite"), default="postgres")
    parser.add_argument("--clock", default="open")
    parser.add_argument("--seed", default="desk")
    parser.add_argument("--plans", action="store_true")
    parser.add_argument("--python-both", action="store_true")
    parser.add_argument("--strict-reads", action="store_true")
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--keep-template", action="store_true")
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--start-timeout", type=float, default=90.0)
    parser.add_argument("--wait-db", type=float, default=1800.0)
    parser.add_argument("--report")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
