"""Grade one Celery task, Python vs Rust: two clones, the replay, an injected clock, every effect compared.

    python3 tools/parity/task_diff.py --cases tools/parity/task_cases/<lane>.json \
        [--case NAME ...] [--sides py,rs] --report /tmp/<lane>-tasks.txt

(the repository's root; plain ``python3`` — each side runs in its own interpreter or binary.)

``nightly_diff.py`` generalised to any task of the worker (wave 6; DECISIONS-RS R8.2). Each **case**
names a task, its message arguments and a fixed **IST moment** — the tasks read the clock (a
session window, a publish cutoff, a token's issue day), so the moment is part of the input. Each
case runs on its own two fresh clones of a template; the two **sides** run one after the other:

* ``py`` — ``task_py.py run NAME --args JSON --now T``: the production Celery task body
  (``app.tasks[NAME](...)``, once) with the replay provider, the replay upstream and the clock
  injected;
* ``rs`` — ``baskfy-worker task NAME --args JSON --now T --replay-bundle DIR --replay-upstream URL``.

Compared, and all must be EQUAL (every mask is printed):

* the **payload** — exit code and the returned JSON *with key order* (``1`` ≠ ``1.0``), or the
  raised class and message; a case's ``payload_mask`` (JSON pointers) compares as ``<masked>``;
* every **table** either side touched, by ``nightly_diff``'s order-free digest (``TABLE_MASKS``
  plus the case's ``mask``), and ``pipeline_run``/``pipeline_run_step`` in full;
* every **sequence**;
* each side's **Redis** index (1–7 only): every key by type, Celery messages normalised (task ids,
  ``origin``, delivery tags, and the absolute ``eta``/``expires`` stamps, which Celery computes
  from the real clock, as ``<set>``) — so **anything enqueued** is compared message by message;
  the case's ``redis_presence`` patterns compare by presence;
* every **mail** sent to the sink (``raised_at`` masked);
* the **upstream** requests each side made to the replay upstream (method, path, query), and any
  write verb (a law-2 violation, which fails the case);
* the **raw archive** each side wrote (new files under its ``BASKFY_RAW_ARCHIVE_DIR``, by sha256).

Case files: ``tools/parity/task_cases/<lane>.json`` —
``{"lane": "...", "cases": [{"name", "task", "args" (array or object), "now" ("YYYY-MM-DDTHH:MM[:SS]",
IST), "scenario" ("rerun" | "fresh", default "rerun"), "date" (the bundle day, default 2026-10-29),
"bundle" (variant, default "base"), "kite" (bool: a test Kite key and a token issued at ``now``),
"seed_sql", "env" (``{archive}`` in a value is the side's raw archive directory), "mask" ({table: {column: "stamp"|"elapsed"}}), "payload_mask", "redis_presence",
"mail_mask" (regexes masked in each mail's text, beside ``raised_at``), "note"}]}``.

Refusals and the machine (as ``nightly_diff``): only ``127.0.0.1:55432``; only databases named
``rst_*`` (templates ``rst_tpl_<kind>_<date>`` cached and evicted least-recently-used, run clones
dropped on exit, SIGINT included); never ``baskfy`` (a clone source only); Redis indices 1–7 only;
ports 18800–18999 only; one heavy run machine-wide (the same ``fcntl`` lock); at most ``--max-dbs``
(6) databases besides ``baskfy``, shared with every other harness on the bed.
"""

from __future__ import annotations

import argparse
import atexit
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nightly_diff as nd  # noqa: E402
from api_writes import MailSink, check_container, mail_view, port_free, psql, redis_dump, redis_flush, sequences  # noqa: E402
from replay_upstream import UpstreamServer  # noqa: E402

nd.PREFIX = "rst_"  # every database this harness creates, clones or drops is rst_*
REPO = nd.REPO
BLUEPRINT = nd.BLUEPRINT
TARGET = nd.TARGET
REDIS_URL = nd.REDIS_URL
PORT_RANGE = range(18800, 19000)
REDIS_RANGE = range(1, 8)
CASES_DIR = Path(__file__).resolve().parent / "task_cases"

#: Wall-clock columns the tasks' tables stamp with Postgres's own `now()` (a column default or an
#: `ON CONFLICT ... SET updated_at = now()`), which no clock injection reaches — compared as
#: `<null>`/`<before>`/`<during>` the side's start, as `nightly_diff.TABLE_MASKS` does for the
#: nightly's. Found by `--sides py,py`; the report prints each one used.
TASK_TABLE_MASKS: dict[str, dict[str, str]] = {
    "fo_underlying_daily": {"updated_at": "stamp"},
    "fo_ingest_day": {"updated_at": "stamp"},
}

#: Celery header stamps computed from the real clock at publish time (never the injected one).
CELERY_CLOCK_HEADERS = ("eta", "expires")

#: A case's ``mask`` kind ``"uuid"`` (lane api_routes, R8.ar3): the column is compared as text with
#: every UUID replaced by ``<uuid>`` and everything else intact — for a stored report that names
#: the Celery task id of the message it queued (``admin_action.detail`` after ``baskfy.ops.resync``).
#: The messages themselves are compared in Redis with their ids normalised.
_UUID_RE = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_nd_masked_expr = nd.masked_expr


def _masked_expr(col: str, kind: str, start: dt.datetime) -> str:
    if kind == "uuid":
        return f"regexp_replace({col}::text, '{_UUID_RE}', '<uuid>', 'g')"
    return _nd_masked_expr(col, kind, start)


nd.masked_expr = _masked_expr


def log(message: str) -> None:
    print(message, flush=True)


@dataclass
class Side:
    label: str
    impl: str
    db: str
    redis_db: int
    archive: Path
    started: dt.datetime | None = None
    exit_code: int | None = None
    payload: Any = None
    stdout_tail: str = ""
    mails: list[Any] = field(default_factory=list)
    upstream: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------------------------


def load_cases(path: Path, only: list[str]) -> tuple[str, list[dict[str, Any]]]:
    data = json.loads(path.read_text())
    cases = data["cases"]
    if only:
        missing = set(only) - {c["name"] for c in cases}
        if missing:
            raise SystemExit(f"no such case(s) in {path}: {sorted(missing)}")
        cases = [c for c in cases if c["name"] in only]
    for c in cases:
        for key in ("name", "task", "now"):
            if key not in c:
                raise SystemExit(f"case {c.get('name')!r} lacks {key!r}")
        dt.datetime.fromisoformat(c["now"])
        if c.get("scenario", "rerun") not in ("rerun", "fresh"):
            raise SystemExit(f"case {c['name']}: scenario must be rerun or fresh")
    return data.get("lane", path.stem), cases


def ensure_bundle(day: dt.date, variant: str) -> Path:
    bundle = nd.REPLAY_ROOT / day.isoformat() / variant
    if not (bundle / "manifest.json").exists():
        log(f"building the {day} bundles ...")
        subprocess.run(
            [str(BLUEPRINT / ".venv/bin/python"), str(Path(__file__).resolve().parent / "replay_build.py"),
             "--date", day.isoformat()],
            cwd=BLUEPRINT, env={**os.environ, **nd.python_env()}, check=True,
        )
    if not (bundle / "manifest.json").exists():
        raise SystemExit(f"no bundle {bundle}")
    return bundle


def build_rust(profile: str) -> None:
    env = {**os.environ, "CARGO_TARGET_DIR": str(TARGET), "CARGO_BUILD_JOBS": "4"}
    cmd = ["nice", "-n", "10", str(nd.CARGO), "build", "-p", "baskfy-worker"]
    if profile == "release":
        cmd.append("--release")
    log("building baskfy-worker ...")
    subprocess.run(cmd, cwd=REPO / "rust", env=env, check=True)


# ---------------------------------------------------------------------------------------------
# The Kite test credentials (test secrets only, generated per run, never printed)
# ---------------------------------------------------------------------------------------------


def kite_env(work: Path, now_ist: str) -> dict[str, str]:
    """A throwaway Fernet key and a token file issued at the case's moment, written by Python's
    own ``AccessTokenStore`` so both sides read the production format."""
    key_file = work / "kite-key"
    token_path = work / "kite-token.enc"
    if not key_file.exists():
        code = (
            "import datetime as dt, sys\n"
            "from cryptography.fernet import Fernet\n"
            "from baskfy_providers.tokens import AccessTokenStore\n"
            "key = Fernet.generate_key().decode()\n"
            "open(sys.argv[1], 'w').write(key)\n"
            "IST = dt.timezone(dt.timedelta(hours=5, minutes=30))\n"
            "at = dt.datetime.fromisoformat(sys.argv[3]).replace(tzinfo=IST)\n"
            "AccessTokenStore(sys.argv[2], key).save('replay-access-token', issued_at=at)\n"
        )
        done = subprocess.run(
            [str(BLUEPRINT / ".venv/bin/python"), "-c", code, str(key_file), str(token_path), now_ist],
            cwd=work, env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"), **nd.python_env()},
            capture_output=True, text=True, check=False,
        )
        if done.returncode != 0:
            raise SystemExit(f"could not write the test Kite token: {done.stderr[-2000:]}")
        key_file.chmod(0o600)
    return {
        "BASKFY_KITE_API_KEY": "replay-api-key",
        "BASKFY_KITE_API_SECRET": "replay-api-secret",
        "BASKFY_KITE_TOKEN_ENCRYPTION_KEY": key_file.read_text().strip(),
        "BASKFY_KITE_TOKEN_PATH": str(token_path),
    }


# ---------------------------------------------------------------------------------------------
# Running a side
# ---------------------------------------------------------------------------------------------


def side_command(side: Side, case: dict[str, Any], bundle: Path, upstream: str, profile: str) -> list[str]:
    args = json.dumps(case.get("args", {}))
    tail = [case["task"], "--args", args, "--now", case["now"], "--replay-bundle", str(bundle),
            "--replay-upstream", upstream]
    if side.impl == "py":
        return [str(BLUEPRINT / ".venv/bin/python"), str(Path(__file__).resolve().parent / "task_py.py"),
                "run", *tail]
    return [str(TARGET / profile / "baskfy-worker"), "task", *tail]


def run_side(side: Side, case: dict[str, Any], bundle: Path, args: argparse.Namespace,
             sink: MailSink, server: UpstreamServer, report_dir: Path, extra_env: dict[str, str]) -> None:
    redis_flush(REDIS_URL, side.redis_db)
    env = {
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        **nd.TEST_ENV,
        "BASKFY_DATABASE_URL": nd.db_url(side.db, asyncpg=True),
        "BASKFY_REDIS_URL": f"{REDIS_URL}/{side.redis_db}",
        "BASKFY_CELERY_BROKER_URL": f"{REDIS_URL}/{side.redis_db}",
        "BASKFY_CELERY_RESULT_BACKEND": f"{REDIS_URL}/{side.redis_db}",
        "BASKFY_SMTP_PORT": str(args.smtp_port),
        "BASKFY_RAW_ARCHIVE_DIR": str(side.archive),
        "BASKFY_NSE_ARCHIVE_URL": server.url,
        "BASKFY_NSE_BASE_URL": server.url,
        **extra_env,
        # `{archive}` in a case's env value is this side's raw archive directory, so a task that
        # writes files under another setting (the backtest's BASKFY_INVOICE_LOCAL_DIR) has them
        # compared with the archive's (lane backtest_engine).
        **{k: str(v).replace("{archive}", str(side.archive)) for k, v in case.get("env", {}).items()},
    }
    if side.impl == "py":
        env.update(nd.python_env())
    with tempfile.TemporaryDirectory(prefix=f"task-diff-{side.label}-") as work:
        deep = Path(work) / "a" / "b"
        deep.mkdir(parents=True)  # no .env can leak in
        mark, up = sink.mark(), server.mark()
        side.started = dt.datetime.now(dt.UTC)
        t0 = time.monotonic()
        proc = subprocess.run(side_command(side, case, bundle, server.url, args.profile),
                              cwd=deep, env=env, capture_output=True, text=True, check=False)
        log(f"    side {side.label} ({side.impl}) exited {proc.returncode} after {time.monotonic() - t0:.1f}s")
    side.exit_code = proc.returncode
    (report_dir / f"{case['name']}_{side.label}_{side.impl}.log").write_text(proc.stderr)
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    side.stdout_tail = proc.stdout[-2000:]
    try:
        side.payload = json.loads(lines[-1], object_pairs_hook=lambda pairs: [["<obj>"]] + [list(p) for p in pairs]) if lines else None
    except ValueError:
        side.payload = {"unparseable stdout": proc.stdout[-2000:]}
    time.sleep(0.3)
    # A case's `mail_mask` (regexes) adds to raised_at: e.g. a plan's uuid4 `plan_id`, which a
    # TWT/VBT evening digest carries and which no two runs share (lane twt_jobs, R8.x).
    side.mails = [mail_view(m, [r"\"raised_at\": \"[^\"]+\"", r"raised_at&quot;: &quot;[^&]+&quot;",
                                *case.get("mail_mask", [])])
                  for m in sink.since(mark)]
    side.upstream = server.since(up)


# ---------------------------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------------------------


def _pointer_mask(value: Any, pointers: list[str]) -> Any:
    """Mask JSON pointers in a payload decoded with key order kept (objects as tagged lists)."""
    def walk(v: Any, path: str) -> Any:
        if path in pointers:
            return "<masked>"
        if isinstance(v, list) and v[:1] == [["<obj>"]]:
            return [["<obj>"]] + [[k, walk(x, f"{path}/{k}")] for k, x in v[1:]]
        if isinstance(v, list):
            return [walk(x, f"{path}/{i}") for i, x in enumerate(v)]
        return v
    return walk(value, "")


def _clock_headers(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in CELERY_CLOCK_HEADERS and v is not None and isinstance(value.get("task"), str):
                out[k] = "<set>"
            else:
                out[k] = _clock_headers(v)
        return out
    if isinstance(value, list):
        return [_clock_headers(v) for v in value]
    return value


def enqueued(dump: dict[str, Any]) -> list[str]:
    """A readable summary of the Celery messages on the queues (already compared in Redis)."""
    out = []
    for key, entry in sorted(dump.items()):
        if not isinstance(entry, dict) or entry.get("type") != "list":
            continue
        for message in entry["value"]:
            if isinstance(message, dict) and isinstance(message.get("headers"), dict):
                h = message["headers"]
                out.append(f"{key}: {h.get('task')} args={h.get('argsrepr')} kwargs={h.get('kwargsrepr')}")
    return out


def archive_files(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()[:16]
            for p in sorted(root.rglob("*")) if p.is_file()}


def compare(case: dict[str, Any], a: Side, b: Side, seq_before: dict[str, Any], seeded: dict[str, str],
            violations: list[str], lines: list[str]) -> bool:
    ok = True
    pm = case.get("payload_mask", [])
    pa, pb = _pointer_mask(a.payload, pm), _pointer_mask(b.payload, pm)
    tag = f" (masked: {', '.join(pm)})" if pm else ""
    if pa == pb and a.exit_code == b.exit_code:
        lines.append(f"  EQUAL   payload (exit {a.exit_code}){tag}: {a.stdout_tail.strip()[:500]}")
    else:
        ok = False
        lines.append(f"  DIFFER  payload{tag}\n    {a.label} exit {a.exit_code}: {a.stdout_tail.strip()[:1500]}\n"
                     f"    {b.label} exit {b.exit_code}: {b.stdout_tail.strip()[:1500]}")
    tables = sorted((nd.touched_tables(a.db) | nd.touched_tables(b.db)) - set(nd.RUN_TABLES))
    lines.append(f"  tables touched on either side: {', '.join(tables) or '(none)'}")
    ok &= nd.compare_runs(a, b, lines)  # type: ignore[arg-type]
    saved = {t: dict(m) for t, m in nd.TABLE_MASKS.items()}
    try:
        for table, cols in [*TASK_TABLE_MASKS.items(), *case.get("mask", {}).items()]:
            nd.TABLE_MASKS.setdefault(table, {}).update(cols)
        for table in tables:
            ok &= nd.compare_table(a, b, table, lines)  # type: ignore[arg-type]
    finally:
        nd.TABLE_MASKS.clear()
        nd.TABLE_MASKS.update(saved)
    sa, sb = sequences(a.db), sequences(b.db)
    moved = sorted(k for k in sa if sa.get(k) != seq_before.get(k) or sb.get(k) != seq_before.get(k))
    if sa == sb:
        lines.append(f"  EQUAL   sequences ({len(sa)}; moved: {', '.join(moved) or 'none'})")
    else:
        ok = False
        diff = {k: (sa.get(k), sb.get(k)) for k in sorted(set(sa) | set(sb)) if sa.get(k) != sb.get(k)}
        lines.append(f"  DIFFER  sequences: {diff}")
    presence = case.get("redis_presence", [])
    ra = _clock_headers(redis_dump(REDIS_URL, a.redis_db, presence))
    rb = _clock_headers(redis_dump(REDIS_URL, b.redis_db, presence))
    rtag = "Celery ids/origin/delivery_tag normalised, eta/expires as <set>" + (
        f"; by presence: {', '.join(presence)}" if presence else "")
    if ra == rb:
        lines.append(f"  EQUAL   redis ({len(ra)} keys; {rtag})")
        for line in enqueued(ra):
            lines.append(f"            enqueued {line}")
    else:
        ok = False
        keys = sorted(k for k in set(ra) | set(rb) if ra.get(k) != rb.get(k))
        lines.append(f"  DIFFER  redis: {len(ra)} vs {len(rb)} keys; differing: {keys[:20]} ({rtag})")
        for k in keys[:5]:
            lines.append(f"    {k}\n      a: {json.dumps(ra.get(k), default=str)[:600]}\n"
                         f"      b: {json.dumps(rb.get(k), default=str)[:600]}")
    if a.mails == b.mails:
        extra = "".join(f", {m}" for m in case.get("mail_mask", []))
        lines.append(f"  EQUAL   mail ({len(a.mails)} message(s); masked: raised_at{extra})")
    else:
        ok = False
        lines.append(f"  DIFFER  mail: {len(a.mails)} vs {len(b.mails)}\n    a: {json.dumps(a.mails)[:1500]}\n"
                     f"    b: {json.dumps(b.mails)[:1500]}")
    if a.upstream == b.upstream:
        lines.append(f"  EQUAL   upstream requests ({len(a.upstream)})")
    else:
        ok = False
        lines.append(f"  DIFFER  upstream requests: {len(a.upstream)} vs {len(b.upstream)}\n"
                     f"    a: {json.dumps(a.upstream)[:1200]}\n    b: {json.dumps(b.upstream)[:1200]}")
    if violations:
        ok = False
        lines.append(f"  LAW 2   write verbs reached the upstream: {violations}")
    fa = {k: v for k, v in archive_files(a.archive).items() if seeded.get(k) != v}
    fb = {k: v for k, v in archive_files(b.archive).items() if seeded.get(k) != v}
    if fa == fb:
        lines.append(f"  EQUAL   raw archive writes ({len(fa)})")
    else:
        ok = False
        lines.append(f"  DIFFER  raw archive writes: {fa} vs {fb}")
    return ok


# ---------------------------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------------------------


def run_case(case: dict[str, Any], args: argparse.Namespace, sides: list[str], sink: MailSink,
             report_dir: Path) -> tuple[bool, list[str]]:
    day = dt.date.fromisoformat(case.get("date", "2026-10-29"))
    kind = case.get("scenario", "rerun")
    bundle = ensure_bundle(day, case.get("bundle", "base"))
    tpl = nd.ensure_template(kind, day, args.max_dbs)
    tag = f"{int(time.time()) % 100000:05d}{secrets.token_hex(2)}"
    work = Path(tempfile.mkdtemp(prefix=f"task-diff-{case['name']}-", dir=report_dir))
    seed_archive = bundle / "upstream" / "archive"
    sides_ = []
    for label, impl, redis_db in (("a", sides[0], args.redis_db_a), ("b", sides[1], args.redis_db_b)):
        archive = work / f"archive_{label}"
        if seed_archive.is_dir():
            shutil.copytree(seed_archive, archive)
        else:
            archive.mkdir()
        sides_.append(Side(label, impl, nd.guard(f"rst_run_{tag}_{label}"), redis_db, archive))
    a, b = sides_
    nd.clone(tpl, a.db, args.max_dbs, {tpl})
    nd.clone(tpl, b.db, args.max_dbs, {tpl, a.db})
    seed = case.get("seed_sql")
    if seed:
        for side in (a, b):
            psql(side.db, seed)
    seq_before = sequences(a.db)
    seeded = archive_files(a.archive)
    extra = kite_env(work, case["now"]) if case.get("kite") else {}
    server = UpstreamServer(bundle, args.upstream_port)
    server.set_clock(case["now"])
    server.start()
    try:
        log(f"  case {case['name']}: {case['task']} {json.dumps(case.get('args', {}))} at {case['now']} IST "
            f"({kind} template {tpl})")
        for side in (a, b):
            run_side(side, case, bundle, args, sink, server, report_dir, extra)
    finally:
        server.stop()
    lines = [f"case {case['name']}: task {case['task']}, args {json.dumps(case.get('args', {}))}, "
             f"now {case['now']} IST, scenario {kind}, bundle {bundle.name} "
             f"(sha256 {nd.bundle_hash(bundle)[:16]}), sides {a.impl},{b.impl}"
             + (f", seed_sql sha256 {hashlib.sha256(seed.encode()).hexdigest()[:16]}" if seed else "")
             + (", kite: test key + token issued at now" if case.get("kite") else "")]
    if case.get("note"):
        lines.append(f"  note: {case['note']}")
    ok = compare(case, a, b, seq_before, seeded, server.violations, lines)
    for side in (a, b):
        redis_flush(REDIS_URL, side.redis_db)
    if not args.keep_dbs:
        for side in (a, b):
            nd.drop(side.db)
            if side.db in nd.CREATED:
                nd.CREATED.remove(side.db)
    lines.append(f"  RESULT {case['name']}: " + ("EQUAL" if ok else "DIFFERENT"))
    return ok, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--case", action="append", default=[], help="run only this case (repeatable)")
    parser.add_argument("--sides", default="py,rs", help="two of py/rs, e.g. py,py to grade the harness")
    parser.add_argument("--profile", choices=("debug", "release"), default="debug")
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--redis-db-a", type=int, default=6)
    parser.add_argument("--redis-db-b", type=int, default=7)
    parser.add_argument("--smtp-port", type=int, default=18899)
    parser.add_argument("--upstream-port", type=int, default=18898)
    parser.add_argument("--max-dbs", type=int, default=6)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--keep-dbs", action="store_true")
    args = parser.parse_args(argv)

    if args.redis_db_a == args.redis_db_b or not {args.redis_db_a, args.redis_db_b} <= set(REDIS_RANGE):
        raise SystemExit("the two sides need two different Redis indices in 1-7 (never 0)")
    for port in (args.smtp_port, args.upstream_port):
        if port not in PORT_RANGE:
            raise SystemExit(f"port {port} is outside this harness's range 18800-18999")
    sides = args.sides.split(",")
    if len(sides) != 2 or any(s not in ("py", "rs") for s in sides):
        raise SystemExit("--sides takes two of py/rs")
    lane, cases = load_cases(args.cases, args.case)
    check_container()
    lock = nd.take_lock()
    for port in (args.smtp_port, args.upstream_port):  # checked under the lock: a queued run waits
        if not port_free(port):
            lock.close()
            raise SystemExit(f"port {port} is in use")
    atexit.register(nd.cleanup)
    signal.signal(signal.SIGINT, nd.on_signal)
    signal.signal(signal.SIGTERM, nd.on_signal)
    report_dir = Path(tempfile.mkdtemp(prefix=f"task-diff-{lane}-"))
    lines = [f"task_diff: lane {lane}, {len(cases)} case(s) from {args.cases}, sides {','.join(sides)}; "
             f"side logs in {report_dir}"]
    equal = 0
    try:
        if "rs" in sides and not args.no_build:
            build_rust(args.profile)
        sink = MailSink(args.smtp_port)
        sink.start()
        try:
            for case in cases:
                ok, case_lines = run_case(case, args, sides, sink, report_dir)
                equal += ok
                lines += case_lines
                log("\n".join(case_lines))
        finally:
            sink.stop()
    finally:
        if args.keep_dbs:
            nd.CREATED.clear()
        nd.cleanup()
        lock.close()
    lines.append(f"RESULT: {equal} of {len(cases)} case(s) EQUAL")
    text = "\n".join(lines)
    if args.report:
        args.report.write_text(text + "\n")
    log(f"RESULT: {equal} of {len(cases)} case(s) EQUAL")
    return 0 if equal == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
