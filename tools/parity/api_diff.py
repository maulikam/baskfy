#!/usr/bin/env python3
"""Differential harness for the Rust API: every route case sent to Python and to Rust, compared.

The grading tool for the API wave (docs/rust-rewrite/README.md §"Grading the API"). It starts —
or, with ``--attach``, attaches to — the Python API (uvicorn, ``baskfy_api.app:get_app``) and the
Rust API (``baskfy-api``) on the ports given, both on the **test bed** database and Redis with the
same test settings and the same test JWT secret; mints bearer tokens for real users of the bed;
sends each case in ``tools/parity/api_cases/*.json`` to both; and compares

* the HTTP **status**,
* selected **headers** — ``content-type``, ``cache-control``, ``etag`` (presence always; value
  too when no mask applies), ``vary``, ``www-authenticate``, ``retry-after``, ``allow``,
  ``location`` (port-normalised), the CORS headers, and ``x-request-id`` presence,
* the **body** — JSON parsed with key order kept (``object_pairs_hook``) and compared exactly:
  same keys, same order, same values, ``1`` is not ``1.0``, ``"5.10"`` is not ``"5.1"``;
  anything that is not JSON is compared byte for byte.

Volatile fields are masked **only** through a case's explicit ``"mask"`` list, and the report
prints every mask it applied. It writes a report (EQUAL / DIFF per case, with a unified diff of
the first mismatch of each failing case) and exits non-zero on any difference.

Five lanes run this concurrently, each on its own ports: it never assumes the default ports are
free, refuses to start a server on a port that is already listening (use ``--attach`` for that),
and only ever stops the processes it started.

Safety (root CLAUDE.md): the database URL must be the bed's (127.0.0.1:55432) unless
``--allow-db-url`` says otherwise; never the live stack (port 5432/8480). The JWT secret is a
test secret kept in an untracked file (``rust/target/parity-jwt-secret``, generated on first use)
and is never printed. No case may place an order: there is no such route in either API.

Usage (from the repo root)::

    python3 tools/parity/api_diff.py --py-port 18101 --rs-port 18102 \\
        --cases tools/parity/api_cases/foundation.json --report /tmp/foundation.txt

Case file: a JSON list (or ``{"cases": [...]}``) of objects::

    {"name": "me-owner",            # unique within the file (defaults to METHOD path)
     "method": "GET", "path": "/api/v1/me",
     "query": {"from": "2026-10-01"} | "from=a&from=b",   # optional
     "user": "owner",               # see USERS below; default anonymous
     "headers": {"Origin": "http://localhost:3000"},      # optional extra request headers
     "body": {...},                 # optional JSON body (no route here writes; for 405 checks)
     "mask": ["served_at", "/rows/0/as_of"],              # optional, printed in the report
     "note": "why this case exists"}

A mask entry is a key name (masked wherever it appears) or a JSON pointer (``/a/0/b``).

Writes (``--writes --lane NAME``): a case file whose top level says ``"mode": "writes"`` is a
**scenario** run against two fresh databases cloned from the bed, and graded on its side effects
too — tables, sequences, Redis and mail. See ``api_writes.py`` for the mechanics. GET mode skips
such files; writes mode accepts nothing else.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import difflib
import glob
import hashlib
import hmac
import http.client
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api_writes as writes_mod  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
BLUEPRINT = REPO / "decile-blueprint"
RUST = REPO / "rust"
DEFAULT_SECRET_FILE = RUST / "target" / "parity-jwt-secret"
DEFAULT_RS_BIN = RUST / "target" / "debug" / "baskfy-api"

#: The test bed (lead-provisioned copy of the database). Anything else needs --allow-db-url.
BED_DATABASE_URL = "postgresql+asyncpg://baskfy:rs-test-only@127.0.0.1:55432/baskfy"
BED_REDIS_URL = "redis://127.0.0.1:56379/0"
BED_DB_HOSTPORT = ("127.0.0.1", 55432)
#: Maulik's live stack. Never.
FORBIDDEN_PORTS = {5432, 8480, 6379}

#: Real users of the bed (public_id), by role. The bed is a copy of the production database, so
#: these are the accounts as they were copied; the facts in the comments are what the foundation
#: cases rely on.
USERS: dict[str, dict[str, Any]] = {
    # The local sign-in account: staff, six entitlement overrides, no subscription row.
    "owner": {"public_id": "2fe482ed2015"},
    # A second account with an active "forever" subscription (staff by allowlist).
    "subscriber": {"public_id": "0159baaac142"},
    # A plain account: not staff, no plan, no override — the tenant-isolation counterpart.
    "plain": {"public_id": "a0a1fd14fa3e"},
    # The owner with a token minted before a revocation: epoch below the account's (0).
    "stale_epoch": {"public_id": "2fe482ed2015", "epoch": -1},
    # Expired an hour ago (beyond the 30 s leeway).
    "expired": {"public_id": "2fe482ed2015", "issued_ago": 7200, "lifetime": 600},
    # A lifetime above BASKFY_JWT_MAX_LIFETIME_SECONDS (900).
    "too_long": {"public_id": "2fe482ed2015", "lifetime": 3600},
    # Signed with another key.
    "wrong_key": {"public_id": "2fe482ed2015", "secret": "a-different-secret-of-the-same-len!!"},
    # A subject that is not an account.
    "unknown": {"public_id": "ghost0000000"},
    # alg=none.
    "alg_none": {"public_id": "2fe482ed2015", "alg": "none"},
    # Not a JWT at all.
    "garbage": {"raw": "not-a-token"},
    # "Bearer " with nothing after it: anonymous, not an error.
    "empty_bearer": {"raw": ""},
}

#: Headers compared by value (lower-case). `location` is port-normalised.
COMPARED_HEADERS = (
    "content-type",
    "cache-control",
    "vary",
    "www-authenticate",
    "retry-after",
    "allow",
    "location",
    "access-control-allow-origin",
    "access-control-allow-credentials",
    "access-control-allow-methods",
    "access-control-allow-headers",
    "access-control-expose-headers",
    "access-control-max-age",
    "x-baskfy-as-of",
    "x-baskfy-data-version",
)
#: Headers compared by presence only.
PRESENCE_HEADERS = ("x-request-id",)

TEST_ENV: dict[str, str] = {
    "BASKFY_ENVIRONMENT": "test",
    "BASKFY_LOG_JSON": "false",
    "BASKFY_LOG_LEVEL": "WARNING",
    # The documented limits (10/min anonymous) would exhaust inside one run, and Python and Rust
    # share one bucket per caller on the bed's Redis (and five lanes share the Redis): limits that
    # never bite. The 429 shape is graded by the crates' own tests.
    "BASKFY_RATE_LIMIT_ANONYMOUS_PER_MINUTE": "1000000",
    "BASKFY_RATE_LIMIT_AUTHENTICATED_PER_MINUTE": "1000000",
    "BASKFY_RATE_LIMIT_AUTH_PER_MINUTE": "1000000",
    "BASKFY_COOKIE_SECURE": "false",
    "BASKFY_EMAIL_TRANSPORT": "console",
    # No telemetry, no Sentry, no mail, no live quotes: nothing leaves the box.
    "BASKFY_OTEL_ENABLED": "false",
    "BASKFY_SENTRY_DSN": "",
}


# ---------------------------------------------------------------------------------------------
# Tokens (HS256, stdlib only — the same bytes PyJWT writes)
# ---------------------------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint(spec: dict[str, Any], secret: str) -> str | None:
    """The bearer token for a USERS entry (None = no Authorization header)."""
    if "raw" in spec:
        return str(spec["raw"])
    now = int(time.time()) - int(spec.get("issued_ago", 0))
    claims = {
        "sub": spec["public_id"],
        "iat": now,
        "exp": now + int(spec.get("lifetime", 600)),
        "epoch": int(spec.get("epoch", 0)),
    }
    alg = spec.get("alg", "HS256")
    header = _b64(json.dumps({"alg": alg, "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signing = f"{header}.{payload}"
    if alg == "none":
        return f"{signing}."
    key = str(spec.get("secret", secret)).encode()
    signature = hmac.new(key, signing.encode(), hashlib.sha256).digest()
    return f"{signing}.{_b64(signature)}"


def load_secret(path: Path) -> str:
    """The bed's test JWT secret; generated (owner-only file) on first use. Never printed."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(24) + "\n")
    return path.read_text().strip()


# ---------------------------------------------------------------------------------------------
# Servers
# ---------------------------------------------------------------------------------------------


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_healthy(port: int, proc: subprocess.Popen[bytes] | None, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise SystemExit(f"server on :{port} exited with {proc.returncode} before it was healthy")
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/health")
            if conn.getresponse().status == 200:
                return
        except OSError:
            pass
        time.sleep(0.3)
    raise SystemExit(f"server on :{port} was not healthy within {timeout:.0f}s")


@dataclass
class Servers:
    """The processes this run started — and only those are ever stopped."""

    started: list[tuple[str, subprocess.Popen[bytes]]] = field(default_factory=list)
    workdirs: list[tempfile.TemporaryDirectory[str]] = field(default_factory=list)

    def stop(self) -> None:
        for _, proc in self.started:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for _, proc in self.started:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.started.clear()
        for d in self.workdirs:
            d.cleanup()


def server_env(args: argparse.Namespace, secret: str) -> dict[str, str]:
    env = {
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        **TEST_ENV,
        "BASKFY_DATABASE_URL": args.database_url,
        "BASKFY_REDIS_URL": args.redis_url,
        "BASKFY_JWT_SECRET": secret,
    }
    for pair in args.env:
        key, _, value = pair.partition("=")
        env[key] = value
    return env


def start(name: str, cmd: list[str], env: dict[str, str], port: int, servers: Servers, log_dir: Path) -> None:
    if port_open(port):
        raise SystemExit(
            f"port {port} already has a listener; pick free ports or pass --attach "
            f"(this harness never stops a process it did not start)"
        )
    # A fresh, empty working directory: pydantic-settings and the Rust settings both read `.env`
    # and `../../.env` relative to it, and no real .env may leak into the bed's servers.
    work = tempfile.TemporaryDirectory(prefix=f"api-diff-{name}-")
    servers.workdirs.append(work)
    deep = Path(work.name) / "a" / "b"
    deep.mkdir(parents=True)
    log = (log_dir / f"api_diff_{name}_{port}.log").open("wb")
    proc = subprocess.Popen(cmd, cwd=deep, env=env, stdout=log, stderr=subprocess.STDOUT)
    servers.started.append((name, proc))


def start_python(args: argparse.Namespace, env: dict[str, str], servers: Servers, log_dir: Path) -> None:
    py_env = dict(env)
    py_env["PYTHONPATH"] = os.pathsep.join(
        str(p)
        for p in (
            REPO,
            BLUEPRINT / "packages/core/src",
            BLUEPRINT / "packages/providers/src",
            BLUEPRINT / "packages/execution/src",
            BLUEPRINT / "services/api/src",
            BLUEPRINT / "services/worker/src",
        )
    )
    cmd = [
        str(BLUEPRINT / ".venv/bin/python"),
        "-m",
        "uvicorn",
        "baskfy_api.app:get_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        str(args.py_port),
        "--no-access-log",
    ]
    start("python", cmd, py_env, args.py_port, servers, log_dir)


def build_rust(args: argparse.Namespace) -> None:
    cargo = Path.home() / ".cargo/bin/cargo"
    env = dict(os.environ)
    env.update({"CARGO_TARGET_DIR": str(RUST / "target"), "CARGO_BUILD_JOBS": "6"})
    subprocess.run(
        ["nice", "-n", "10", str(cargo), "build", "-p", "baskfy-api"],
        cwd=RUST,
        env=env,
        check=True,
    )


def start_rust(args: argparse.Namespace, env: dict[str, str], servers: Servers, log_dir: Path) -> None:
    binary = Path(args.rs_bin)
    if not binary.exists():
        raise SystemExit(f"{binary} does not exist; build it (or drop --no-build)")
    cmd = [str(binary), "--host", "127.0.0.1", "--port", str(args.rs_port)]
    start("rust", cmd, env, args.rs_port, servers, log_dir)


# ---------------------------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------------------------


@dataclass
class Case:
    source: str
    name: str
    method: str
    path: str
    query: str
    user: str
    headers: dict[str, str]
    body: Any
    mask: list[str]
    note: str
    capture: dict[str, str] = field(default_factory=dict)
    raw_body: str | None = None
    mail_mask: list[str] = field(default_factory=list)


def case_files(patterns: list[str], writes: bool) -> list[str]:
    """The files the patterns name, keeping only GET-mode files or only writes-mode ones."""
    files: list[str] = []
    for pattern in patterns:
        files.extend(sorted(glob.glob(pattern)))
    if not files:
        raise SystemExit(f"no case files match {patterns}")
    kept = []
    for f in files:
        raw = json.loads(Path(f).read_text())
        is_writes = isinstance(raw, dict) and raw.get("mode") == "writes"
        if is_writes == writes:
            kept.append(f)
        elif writes:
            raise SystemExit(f"{f} is not a writes scenario (no \"mode\": \"writes\")")
    if not kept:
        raise SystemExit(f"no {'writes' if writes else 'GET'}-mode case files among {files}")
    return kept


def load_cases(patterns: list[str], writes: bool = False) -> list[Case]:
    files = case_files(patterns, writes)
    cases: list[Case] = []
    for f in files:
        raw = json.loads(Path(f).read_text())
        items = raw["cases"] if isinstance(raw, dict) else raw
        seen: set[str] = set()
        for item in items:
            q = item.get("query", "")
            query = q if isinstance(q, str) else urllib.parse.urlencode(q, doseq=True)
            name = item.get("name") or f"{item.get('method', 'GET')} {item['path']}"
            if name in seen:
                raise SystemExit(f"{f}: duplicate case name {name!r}")
            seen.add(name)
            user = item.get("user", "anonymous")
            if user != "anonymous" and user not in USERS:
                raise SystemExit(f"{f}: case {name!r} names unknown user {user!r}")
            cases.append(
                Case(
                    source=Path(f).name,
                    name=name,
                    method=item.get("method", "GET").upper(),
                    path=item["path"],
                    query=query,
                    user=user,
                    headers=dict(item.get("headers", {})),
                    body=item.get("body"),
                    mask=list(item.get("mask", [])),
                    note=item.get("note", ""),
                    capture=dict(item.get("capture", {})),
                    raw_body=item.get("raw_body"),
                    mail_mask=list(item.get("mail_mask", [])),
                )
            )
    return cases


@dataclass
class Answer:
    status: int
    headers: dict[str, str]
    body: bytes
    raw_headers: list[tuple[str, str]] = field(default_factory=list)


def send(port: int, case: Case, token: str | None) -> Answer:
    target = case.path + (f"?{case.query}" if case.query else "")
    headers = dict(case.headers)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    payload: bytes | None = None
    if case.raw_body is not None:
        payload = case.raw_body.encode()
    elif case.body is not None:
        payload = json.dumps(case.body).encode()
        headers.setdefault("Content-Type", "application/json")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request(case.method, target, body=payload, headers=headers)
    response = conn.getresponse()
    body = response.read()
    # Duplicate header names are joined, as a client sees them.
    merged: dict[str, str] = {}
    for k, v in response.getheaders():
        key = k.lower()
        merged[key] = f"{merged[key]}, {v}" if key in merged else v
    conn.close()
    return Answer(response.status, merged, body, list(response.getheaders()))


# ---------------------------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------------------------


class Ordered(list[tuple[str, Any]]):
    """A JSON object kept as its (key, value) pairs, so key order is compared too."""


def parse_ordered(raw: bytes) -> Any:
    return json.loads(raw, object_pairs_hook=Ordered)


def apply_masks(value: Any, masks: list[str], path: str = "") -> Any:
    if isinstance(value, Ordered):
        out = Ordered()
        for k, v in value:
            here = f"{path}/{k}"
            if k in masks or here in masks:
                out.append((k, "<masked>"))
            else:
                out.append((k, apply_masks(v, masks, here)))
        return out
    if isinstance(value, list):
        return [
            "<masked>" if f"{path}/{i}" in masks else apply_masks(v, masks, f"{path}/{i}")
            for i, v in enumerate(value)
        ]
    return value


def typed(value: Any) -> Any:
    """Make int/float/bool distinct for comparison and display (1 != 1.0 != True)."""
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


def plain(value: Any) -> Any:
    """Back to dicts (insertion-ordered) for display; json.dumps keeps 1, 1.0 and true apart."""
    if isinstance(value, Ordered):
        return {k: plain(v) for k, v in value}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def pretty(value: Any) -> list[str]:
    return json.dumps(plain(value), indent=1, ensure_ascii=False).splitlines()


def normalise_location(value: str, port: int) -> str:
    return value.replace(f":{port}/", ":<port>/").replace(f":{port}?", ":<port>?")


@dataclass
class Result:
    case: Case
    equal: bool
    problems: list[str]
    diff: list[str]


def compare(case: Case, py: Answer, rs: Answer, py_port: int, rs_port: int) -> Result:
    problems: list[str] = []
    diff: list[str] = []
    if py.status != rs.status:
        problems.append(f"status: python {py.status} != rust {rs.status}")
    for h in COMPARED_HEADERS:
        a, b = py.headers.get(h), rs.headers.get(h)
        if h == "location":
            a = normalise_location(a, py_port) if a else a
            b = normalise_location(b, rs_port) if b else b
        if a != b:
            problems.append(f"header {h}: python {a!r} != rust {b!r}")
    for h in PRESENCE_HEADERS:
        if (h in py.headers) != (h in rs.headers):
            problems.append(f"header {h} presence: python {h in py.headers} != rust {h in rs.headers}")
    if ("etag" in py.headers) != ("etag" in rs.headers):
        problems.append(f"header etag presence: python {'etag' in py.headers} != rust {'etag' in rs.headers}")
    elif not case.mask and py.headers.get("etag") != rs.headers.get("etag"):
        problems.append(f"header etag: python {py.headers.get('etag')!r} != rust {rs.headers.get('etag')!r}")

    cookie_masks = {m.split(":", 1)[1] for m in case.mask if m.startswith("cookie:")}
    py_cookies = writes_mod.set_cookies(py.raw_headers)
    rs_cookies = writes_mod.set_cookies(rs.raw_headers)
    attr_masks = {m.split(":", 1)[1].lower() for m in case.mask if m.startswith("cookie-attr:")}
    for jar in (py_cookies, rs_cookies):
        for c in jar:
            if c["name"] in cookie_masks and c["value"]:
                c["value"] = "<masked>"
            for attr in c["attrs"]:
                if attr[0] in attr_masks and len(attr) == 2:
                    attr[1] = "<masked>"
    if py_cookies != rs_cookies:
        problems.append(f"set-cookie: python {py_cookies!r} != rust {rs_cookies!r}")

    is_json = "json" in py.headers.get("content-type", "") or "json" in rs.headers.get("content-type", "")
    if is_json and py.body and rs.body:
        try:
            a = apply_masks(parse_ordered(py.body), case.mask)
            b = apply_masks(parse_ordered(rs.body), case.mask)
        except ValueError as e:
            problems.append(f"body is not JSON: {e}")
            a = b = None
        if a is not None and typed(a) != typed(b):
            problems.append("body: JSON differs")
            diff = list(difflib.unified_diff(pretty(a), pretty(b), "python", "rust", lineterm="", n=3))
    elif py.body != rs.body:
        problems.append(f"body bytes differ ({len(py.body)} vs {len(rs.body)} bytes)")
        diff = list(
            difflib.unified_diff(
                py.body.decode("utf-8", "replace").splitlines(),
                rs.body.decode("utf-8", "replace").splitlines(),
                "python",
                "rust",
                lineterm="",
            )
        )
    return Result(case, not problems, problems, diff)


# ---------------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------------


def guard_urls(args: argparse.Namespace) -> None:
    for label, url in (("database", args.database_url), ("redis", args.redis_url)):
        parsed = urllib.parse.urlsplit(url.replace("+asyncpg", ""))
        if parsed.hostname not in ("127.0.0.1", "localhost"):
            raise SystemExit(f"{label} URL is not loopback; the harness only runs on the bed")
        if parsed.port in FORBIDDEN_PORTS:
            raise SystemExit(f"{label} URL points at port {parsed.port}: that is the live stack")
    parsed = urllib.parse.urlsplit(args.database_url.replace("+asyncpg", ""))
    if (parsed.hostname, parsed.port) != BED_DB_HOSTPORT and not args.allow_db_url:
        raise SystemExit("the database URL is not the bed's (127.0.0.1:55432); pass --allow-db-url to override")
    for port in (args.py_port, args.rs_port):
        if port in FORBIDDEN_PORTS:
            raise SystemExit(f"port {port} belongs to the live stack")


def report_lines(results: list[Result], args: argparse.Namespace) -> Iterator[str]:
    equal = sum(r.equal for r in results)
    yield f"api_diff: {equal} equal, {len(results) - equal} different, {len(results)} cases"
    yield f"python :{args.py_port}  rust :{args.rs_port}  database {urllib.parse.urlsplit(args.database_url).hostname}:{urllib.parse.urlsplit(args.database_url).port}"
    yield ""
    for r in results:
        c = r.case
        tag = "EQUAL" if r.equal else "DIFF "
        target = c.path + (f"?{c.query}" if c.query else "")
        yield f"{tag} [{c.source}] {c.name}: {c.method} {target} as {c.user}"
        if c.mask:
            yield f"      masked: {', '.join(c.mask)}"
        for p in r.problems:
            yield f"      - {p}"
        if r.diff:
            yield from (f"      {line}" for line in r.diff[:80])
            if len(r.diff) > 80:
                yield f"      ... ({len(r.diff) - 80} more diff lines)"


def run_writes(args: argparse.Namespace, secret: str) -> int:
    """Every writes scenario: clone, serve, send in order, stop, compare side effects."""
    if not args.lane or not args.smtp_port:
        raise SystemExit("--writes needs --lane and --smtp-port")
    bed = urllib.parse.urlsplit(args.database_url.replace("+asyncpg", ""))
    if args.allow_db_url or (bed.hostname, bed.port) != BED_DB_HOSTPORT:
        raise SystemExit("--writes runs only on the bed's database (127.0.0.1:55432); --allow-db-url is refused")
    lane = writes_mod.lane_ok(args.lane)
    if args.redis_db_py == args.redis_db_rs or 0 in (args.redis_db_py, args.redis_db_rs):
        raise SystemExit("--redis-db-py and --redis-db-rs must differ and neither may be 0")
    if args.smtp_port in FORBIDDEN_PORTS or not writes_mod.port_free(args.smtp_port):
        raise SystemExit(f"--smtp-port {args.smtp_port} is taken or forbidden")
    writes_mod.check_container()
    files = case_files(args.cases, writes=True)
    if not args.no_build:
        build_rust(args)
    sink = writes_mod.MailSink(args.smtp_port)
    sink.start()
    lines: list[str] = []
    all_equal = True
    tpl = writes_mod.ensure_lane_template(lane, args.template_wait)
    try:
        for f in files:
            equal, report = run_scenario(args, secret, f, tpl, lane, sink)
            all_equal &= equal
            lines.extend(report)
    finally:
        sink.stop()
        if args.drop_template:
            writes_mod.drop(tpl)
    text = "\n".join(lines) + "\n"
    sys.stdout.write(text)
    if args.report:
        Path(args.report).write_text(text)
    return 0 if all_equal else 1


def run_scenario(
    args: argparse.Namespace, secret: str, path: str, tpl: str, lane: str, sink: Any
) -> tuple[bool, list[str]]:
    spec = json.loads(Path(path).read_text())
    cases = load_cases([path], writes=True)
    if args.only:
        cases = [c for c in cases if args.only in c.name]
    run = writes_mod.new_run_id()
    names = {side: f"rsw_{lane}_{run}_{side}" for side in ("base", "py", "rs")}
    db_mask: dict[str, list[str]] = {k: list(v) for k, v in spec.get("db_mask", {}).items()}
    presence_only = list(writes_mod.DEFAULT_PRESENCE_ONLY) + list(spec.get("redis_presence_only", []))
    extra_env = {str(k): str(v) for k, v in spec.get("env", {}).items()}
    results: list[Result] = []
    mail_problems: list[str] = []
    effects: list[str] = []
    servers = Servers()
    try:
        return _scenario(args, secret, path, spec, cases, run, names, db_mask, presence_only,
                         extra_env, results, mail_problems, effects, servers, tpl, sink)
    finally:
        servers.stop()
        if not args.keep_dbs:
            for name in names.values():
                writes_mod.drop(name)


def _scenario(  # noqa: PLR0913 - the scenario's state, threaded through once
    args: argparse.Namespace, secret: str, path: str, spec: dict[str, Any], cases: list[Case],
    run: str, names: dict[str, str], db_mask: dict[str, list[str]], presence_only: list[str],
    extra_env: dict[str, str], results: list[Result], mail_problems: list[str],
    effects: list[str], servers: Servers, tpl: str, sink: Any,
) -> tuple[bool, list[str]]:
    try:
        for name in names.values():
            writes_mod.clone(tpl, name)
            for statement in spec.get("seed_sql", []):
                writes_mod.psql(name, statement if statement.rstrip().endswith(";") else statement + ";")
        base_env = server_env(args, secret)
        bed = urllib.parse.urlsplit(args.database_url.replace("+asyncpg", ""))
        side_env = {}
        for side, redis_db in (("py", args.redis_db_py), ("rs", args.redis_db_rs)):
            db_url = f"postgresql+asyncpg://{bed.username}:{bed.password}@127.0.0.1:55432/{names[side]}"
            side_env[side] = writes_mod.env_for_side(
                base_env, db_url, args.redis_url, redis_db, args.smtp_port, extra_env
            )
            writes_mod.redis_flush(args.redis_url, redis_db)
        log_dir = Path(args.log_dir)
        start_python(args, side_env["py"], servers, log_dir)
        start_rust(args, side_env["rs"], servers, log_dir)
        for name, port in (("python", args.py_port), ("rust", args.rs_port)):
            proc = next((p for n, p in servers.started if n == name), None)
            wait_healthy(port, proc, args.startup_timeout)

        variables: dict[str, dict[str, str]] = {"py": {}, "rs": {}}
        for case in cases:
            spec_user = USERS.get(case.user)
            token = mint(spec_user, secret) if spec_user is not None else None
            answers = {}
            mails = {}
            capture_failures: list[str] = []
            for side, port in (("py", args.py_port), ("rs", args.rs_port)):
                concrete = Case(
                    **{
                        **case.__dict__,
                        "path": writes_mod.substitute(case.path, variables[side]),
                        "query": writes_mod.substitute(case.query, variables[side]),
                        "headers": writes_mod.substitute(case.headers, variables[side]),
                        "body": writes_mod.substitute(case.body, variables[side]),
                        "raw_body": writes_mod.substitute(case.raw_body, variables[side]),
                    }
                )
                mark = sink.mark()
                answers[side] = send(port, concrete, token)
                sent = sink.since(mark)
                mails[side] = [writes_mod.mail_view(m, case.mail_mask) for m in sent]
                for var, where in case.capture.items():
                    try:
                        variables[side][var] = writes_mod.capture(
                            where, answers[side].body, answers[side].raw_headers, sent
                        )
                    except (SystemExit, ValueError, KeyError, IndexError) as exc:
                        capture_failures.append(f"{side}: {exc}")
                        variables[side][var] = "<not-captured>"
            if args.dump_dir:
                dump = Path(args.dump_dir)
                dump.mkdir(parents=True, exist_ok=True)
                for side in ("py", "rs"):
                    a = answers[side]
                    (dump / f"{case.name}.{side}.txt").write_text(
                        f"{a.status}\n"
                        + "".join(f"{k}: {v}\n" for k, v in a.raw_headers)
                        + "\n"
                        + a.body.decode("utf-8", "replace")
                        + "\n\n"
                        + json.dumps(mails[side], indent=1, ensure_ascii=False)
                    )
            result = compare(case, answers["py"], answers["rs"], args.py_port, args.rs_port)
            if capture_failures:
                result.equal = False
                result.problems.extend(f"capture failed on {f}" for f in capture_failures)
            if mails["py"] != mails["rs"]:
                result.equal = False
                result.problems.append("mail differs")
                result.diff.extend(
                    difflib.unified_diff(
                        json.dumps(mails["py"], indent=1, ensure_ascii=False).splitlines(),
                        json.dumps(mails["rs"], indent=1, ensure_ascii=False).splitlines(),
                        "python mail",
                        "rust mail",
                        lineterm="",
                    )
                )
            elif mails["py"]:
                result.problems.append(f"mail: {len(mails['py'])} message(s) per side, EQUAL")
            results.append(result)
    finally:
        servers.stop()
    # Side effects, with both servers gone (their backends have exited and flushed their stats).
    time.sleep(1.5)
    try:
        touched = (
            writes_mod.touched_tables(names["py"])
            | writes_mod.touched_tables(names["rs"])
            | set(spec.get("tables", []))
        )
        effects_equal = True
        for table in sorted(touched):
            key = writes_mod.primary_key(names["base"], table)
            base = writes_mod.table_rows(names["base"], table, key)
            masked = set(db_mask.get(table, []))
            py_changes = writes_mod.change_set(base, writes_mod.table_rows(names["py"], table, key), masked)
            rs_changes = writes_mod.change_set(base, writes_mod.table_rows(names["rs"], table, key), masked)
            tag = "EQUAL" if py_changes == rs_changes else "DIFF "
            effects_equal &= py_changes == rs_changes
            effects.append(
                f"{tag} table {table}: python {len(py_changes)} row change(s), rust {len(rs_changes)}"
                + (f"; masked {', '.join(sorted(masked))}" if masked else "")
            )
            if py_changes != rs_changes:
                effects.extend(
                    "      " + line
                    for line in list(
                        difflib.unified_diff(
                            [json.dumps(c, ensure_ascii=False) for c in py_changes],
                            [json.dumps(c, ensure_ascii=False) for c in rs_changes],
                            "python",
                            "rust",
                            lineterm="",
                        )
                    )[:120]
                )
            else:
                effects.extend(f"      {json.dumps(c, ensure_ascii=False)}"[:400] for c in py_changes[:20])
        seq_py, seq_rs = writes_mod.sequences(names["py"]), writes_mod.sequences(names["rs"])
        moved = {k for k in seq_py if seq_py.get(k) != seq_rs.get(k)}
        effects_equal &= not moved
        effects.append(("DIFF  sequences: " + ", ".join(sorted(moved))) if moved else "EQUAL sequences")
        r_py = writes_mod.redis_dump(args.redis_url, args.redis_db_py, presence_only)
        r_rs = writes_mod.redis_dump(args.redis_url, args.redis_db_rs, presence_only)
        # A value one side generated at random (a backtest's public id) and the scenario captured:
        # the file's "redis_mask_captures" names it, and each side's own value reads "<name>".
        for names_ in spec.get("redis_mask_captures", []):
            for side, dump in (("py", r_py), ("rs", r_rs)):
                value = variables[side].get(names_)
                if value:
                    text = json.dumps(dump).replace(value, f"<{names_}>")
                    dump.clear()
                    dump.update(json.loads(text))
        effects_equal &= r_py == r_rs
        effects.append(
            f"{'EQUAL' if r_py == r_rs else 'DIFF '} redis: python {len(r_py)} key(s), rust {len(r_rs)}"
            f"; presence only: {', '.join(presence_only)}"
        )
        if r_py != r_rs:
            effects.extend(
                "      " + line
                for line in difflib.unified_diff(
                    json.dumps(r_py, indent=1, sort_keys=True, default=str).splitlines(),
                    json.dumps(r_rs, indent=1, sort_keys=True, default=str).splitlines(),
                    "python",
                    "rust",
                    lineterm="",
                )
            )
        for db in (args.redis_db_py, args.redis_db_rs):
            writes_mod.redis_flush(args.redis_url, db)
    finally:
        pass
    equal = all(r.equal for r in results) and effects_equal
    header = [
        f"api_diff --writes [{Path(path).name}] run {run}: "
        f"{sum(r.equal for r in results)} of {len(results)} responses equal, "
        f"side effects {'EQUAL' if effects_equal else 'DIFFERENT'}",
        f"databases {', '.join(names.values())} cloned from {tpl}"
        + (" (kept)" if args.keep_dbs else " (dropped)"),
    ]
    if db_mask:
        header.append("db masks: " + "; ".join(f"{t}: {', '.join(c)}" for t, c in sorted(db_mask.items())))
    body = list(report_lines(results, args))[3:]
    return equal, header + body + ["", "side effects:"] + effects + mail_problems + [""]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--py-port", type=int, default=18001)
    parser.add_argument("--rs-port", type=int, default=18002)
    parser.add_argument("--cases", nargs="+", default=[str(REPO / "tools/parity/api_cases/*.json")])
    parser.add_argument("--attach", action="store_true", help="use servers already listening on the ports")
    parser.add_argument("--no-build", action="store_true", help="do not run cargo build first")
    parser.add_argument("--rs-bin", default=str(DEFAULT_RS_BIN))
    parser.add_argument("--database-url", default=BED_DATABASE_URL)
    parser.add_argument("--redis-url", default=BED_REDIS_URL)
    parser.add_argument("--allow-db-url", action="store_true")
    parser.add_argument("--secret-file", default=str(DEFAULT_SECRET_FILE))
    parser.add_argument("--env", action="append", default=[], help="KEY=VALUE for both servers")
    parser.add_argument("--report", help="also write the report to this file")
    parser.add_argument("--log-dir", default=tempfile.gettempdir(), help="server logs go here")
    parser.add_argument("--startup-timeout", type=float, default=90.0)
    parser.add_argument("--only", help="run only cases whose name contains this")
    parser.add_argument("--writes", action="store_true", help="run writes scenarios (api_writes.py)")
    parser.add_argument("--lane", help="writes: the lane name in every database it clones")
    parser.add_argument("--smtp-port", type=int, help="writes: the mail sink's port (your lane's own)")
    parser.add_argument("--redis-db-py", type=int, default=12, help="writes: Python's Redis db index")
    parser.add_argument("--redis-db-rs", type=int, default=13, help="writes: Rust's Redis db index")
    parser.add_argument("--keep-dbs", action="store_true", help="writes: keep the run's databases")
    parser.add_argument("--drop-template", action="store_true", help="writes: drop the lane template after")
    parser.add_argument("--dump-dir", help="writes: write each side's raw answer per case here")
    parser.add_argument("--template-wait", type=float, default=900.0, help="writes: seconds to wait to clone")
    args = parser.parse_args()

    guard_urls(args)
    secret = load_secret(Path(args.secret_file))
    if args.writes:
        if args.attach:
            raise SystemExit("--writes starts its own servers per scenario; --attach is refused")
        return run_writes(args, secret)
    cases = load_cases(args.cases)
    if args.only:
        cases = [c for c in cases if args.only in c.name]

    servers = Servers()
    try:
        if not args.attach:
            if not args.no_build:
                build_rust(args)
            env = server_env(args, secret)
            log_dir = Path(args.log_dir)
            start_python(args, env, servers, log_dir)
            start_rust(args, env, servers, log_dir)
        for name, port in (("python", args.py_port), ("rust", args.rs_port)):
            proc = next((p for n, p in servers.started if n == name), None)
            wait_healthy(port, proc, args.startup_timeout)

        results: list[Result] = []
        for case in cases:
            spec = USERS.get(case.user)
            token = mint(spec, secret) if spec is not None else None
            py = send(args.py_port, case, token)
            rs = send(args.rs_port, case, token)
            results.append(compare(case, py, rs, args.py_port, args.rs_port))
    finally:
        servers.stop()

    lines = list(report_lines(results, args))
    text = "\n".join(lines) + "\n"
    sys.stdout.write(text)
    if args.report:
        Path(args.report).write_text(text)
    return 0 if all(r.equal for r in results) else 1


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
    sys.exit(130)
