"""Grade the worker's nightly: two sides, two clones of the bed, the same replayed day, every write compared.

    python3 tools/parity/nightly_diff.py --chain --scenario fresh [--sides py,rs] --report /tmp/n.txt
    python3 tools/parity/nightly_diff.py --step compute_factors --scenario fresh --report /tmp/s.txt

(the repository's root; plain ``python3`` — the sides run in their own interpreters.)

Each **side** is one implementation of the worker: ``py`` (``nightly_py.py``: the production
Python with the replay provider injected) or ``rs`` (``rust/target/<profile>/baskfy-worker``
with ``--replay-bundle``). ``--sides py,py`` grades the harness itself (determinism and masks);
``py,rs`` grades the port. The two sides run **one after the other** on their own database, Redis
index and mail stream, against identical inputs: both clones come from one template, and both
read the same replay bundle (``replay_build.py``, ``replay_provider.py``, ``replay.rs``).

Modes
-----
``--chain``    both sides run ``nightly --date D`` (the Celery task's explicit-date body).
``--step S``   one clone is **primed** by running Python's chain up to the step before ``S``
               (``nightly_py.py prime``) and kept as a cached template
               ``rsn_primed_<s>_<hash>``; it is cloned twice and both sides run just ``S`` into the
               same open run (``step S --run-id N --context ctx.json``). The hash covers the
               bundle and the Python sources (``baskfy_worker``, ``baskfy_core``,
               ``baskfy_providers``, ``baskfy_api``, ``nightly_py.py``), so a cached prime is reused
               only while neither changed.

Scenarios (``--scenario``)
--------------------------
``rerun``                     the unmodified bed (it already holds the day): house rule 7.
``fresh``                     the bed with the day's chain output removed (``FRESH_DELETES``), so the
                              day is new: the day's ``pipeline_run`` (its steps cascade), and the
                              day's rows of ``ohlcv_daily``, ``factor_daily``, ``desk_score_daily``,
                              ``market_health_daily``, ``index_member_daily``,
                              ``index_snapshot_daily``, ``fundamental_daily``, ``screen_run``,
                              ``sw_setup_daily``, ``sw_market_daily``, ``vb_signal_daily``,
                              ``vb_breadth_daily``, ``tw_state_daily``, ``tw_breadth_daily``,
                              ``tw_signal_daily``. Upserts that leave no dated trace (``instrument``,
                              ``corporate_action``, ``trading_day``, the adjusted history) stay as
                              the bed has them; the chain rewrites them idempotently.
``gate-refuse``               ``fresh`` with the ``gate-refuse`` bundle (two symbols in five): the
                              gate must fail, the run must close ``failed``, the alert must mail.
``non-trading-day``           the bed, the first non-trading day after ``--date``: aborted, no step.
``provider-failure-midway``   ``fresh`` with ``index_snapshots`` refused: step 6 raises, the chain
                              rolls back, the run is failed on a third transaction, the alert mails.

``--mutate FILE``             any scenario, plus a SQL file applied identically to both sides' clones
                              after cloning and before either side runs: an edge the bed's day does
                              not reach (a provisional leftover, a real sleeve book, a thin session).
                              Its sha256 is printed; rows it inserts should carry fixed timestamps, or
                              the two clones' ``now()`` differ.

Compared (all must be EQUAL; every mask is printed)
---------------------------------------------------
* the side's **payload** (the task's return value, or the raised error) — ``run_id`` masked;
* **tables**: every table either side touched (``pg_stat_user_tables``; TimescaleDB chunks mapped
  to their hypertable), by an order-free digest of every row in 1024 key buckets, and the
  differing buckets' rows printed side by side. A column in ``TABLE_MASKS`` compares as ``<null>``,
  ``<before>`` (earlier than the side's start) or ``<during>`` (stamped by this run);
* ``pipeline_run`` and ``pipeline_run_step`` in full, ids masked as ranks, ``started_at``,
  ``finished_at`` as ``<before>``/``<during>``, ``duration_ms`` as ``<null>``/``<set>``, and an
  error's ``traceback`` as ``<set>`` (Python's and Rust's cannot match; R7.4);
* every **sequence**'s ``last_value``;
* each side's **Redis** index (never 0), dumped by type;
* every **mail** the side sent to the SMTP sink (``raised_at`` masked).

Refusals and the machine
------------------------
Only ``127.0.0.1:55432`` (container ``baskfy-rs-pg``); only databases named ``rsn_*``; never
``baskfy`` (a clone source only); never Redis index 0. A machine-wide ``fcntl`` lock on
``rust/target/nightly_diff.lock`` serialises heavy runs across lanes. At most ``--max-dbs`` (5)
databases other than ``baskfy`` may exist: before creating one the harness evicts its own cached
``rsn_tpl_*``/``rsn_primed_*`` templates (least recently used first) and otherwise waits. Every
database created for the run is dropped on exit — error and SIGINT included; the cached templates
persist by design (``--drop-cache`` removes them).
"""

from __future__ import annotations

import argparse
import atexit
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_writes import (  # noqa: E402
    MailSink,
    check_container,
    mail_view,
    port_free,
    psql,
    redis_dump,
    redis_flush,
    sequences,
)

REPO = Path(__file__).resolve().parents[2]
BLUEPRINT = REPO / "decile-blueprint"
TARGET = REPO / "rust" / "target"
LOCK_FILE = TARGET / "nightly_diff.lock"
CACHE_DIR = TARGET / "nightly_diff_cache"
REPLAY_ROOT = TARGET / "replay"
CONTAINER = "baskfy-rs-pg"
BED_URL_HOST = "127.0.0.1:55432"
DB_USER, DB_PASSWORD = "baskfy", "rs-test-only"
SOURCE_DB = "baskfy"
PREFIX = "rsn_"
SYSTEM_DBS = {"postgres", "template0", "template1", SOURCE_DB}
REDIS_URL = "redis://127.0.0.1:56379"
CARGO = Path.home() / ".cargo" / "bin" / "cargo"
BUCKETS = 1024
SHOW_ROWS = 12
ALERT_ADDRESS = "ops-alerts@example.test"

SCENARIOS = {
    # name: (template kind, bundle variant)
    "rerun": ("rerun", "base"),
    "fresh": ("fresh", "base"),
    "gate-refuse": ("fresh", "gate-refuse"),
    "non-trading-day": ("rerun", "base"),
    "provider-failure-midway": ("fresh", "provider-failure-midway"),
    "index-edges": ("fresh", "index-edges"),
    "fundamentals-refused": ("fresh", "fundamentals-refused"),
}

#: The day's chain output, removed from the ``fresh`` template (see the module docs).
FRESH_DELETES = (
    "DELETE FROM pipeline_run WHERE trade_date = DATE '{d}'",
    "DELETE FROM ohlcv_daily WHERE date = DATE '{d}'",
    "DELETE FROM factor_daily WHERE date = DATE '{d}'",
    "DELETE FROM desk_score_daily WHERE date = DATE '{d}'",
    "DELETE FROM market_health_daily WHERE date = DATE '{d}'",
    "DELETE FROM index_member_daily WHERE date = DATE '{d}'",
    "DELETE FROM index_snapshot_daily WHERE date = DATE '{d}'",
    "DELETE FROM fundamental_daily WHERE date = DATE '{d}'",
    "DELETE FROM screen_run WHERE as_of = DATE '{d}'",
    "DELETE FROM sw_setup_daily WHERE date = DATE '{d}'",
    "DELETE FROM sw_market_daily WHERE date = DATE '{d}'",
    "DELETE FROM vb_signal_daily WHERE date = DATE '{d}'",
    "DELETE FROM vb_breadth_daily WHERE date = DATE '{d}'",
    "DELETE FROM tw_state_daily WHERE date = DATE '{d}'",
    "DELETE FROM tw_breadth_daily WHERE date = DATE '{d}'",
    "DELETE FROM tw_signal_daily WHERE date = DATE '{d}'",
)

#: Values that differ between two correct runs (found by `--sides py,py`), each with its kind:
#: ``stamp`` — a wall-clock column, compared as <null>/<before>/<during> the side's start;
#: ``elapsed`` — a measured duration, compared as <null>/<set>. The report prints every one used.
TABLE_MASKS: dict[str, dict[str, str]] = {
    # `ON CONFLICT DO UPDATE SET updated_at = now()` on every merged instrument; `created_at` on a
    # new one (server default now()).
    "instrument": {"created_at": "stamp", "updated_at": "stamp"},
    # tasks/basket.py: "Re-running a night changes nothing except `computed_at`" — and the
    # build's own timing, `computed_ms`.
    "basket_snapshot": {"computed_ms": "elapsed", "computed_at": "stamp"},
    # A row first written by this run takes `created_at DEFAULT now()` (on a rerun the upsert
    # keeps the old stamp, so these compare <before> on both sides).
    "screen_run": {"created_at": "stamp"},
    "sw_market_daily": {"created_at": "stamp"},
    "sw_setup_daily": {"created_at": "stamp"},
    "tw_breadth_daily": {"created_at": "stamp"},
    "tw_signal_daily": {"created_at": "stamp"},
    "tw_state_daily": {"created_at": "stamp"},
    "vb_breadth_daily": {"created_at": "stamp"},
    "vb_signal_daily": {"created_at": "stamp"},
    # `run_twt_manage_qulla`: the ORM's `onupdate=func.now()` on a position whose queue changed
    # (only a `--mutate` scenario changes one; on the bed's day both sides leave it <before>).
    "tw_position": {"updated_at": "stamp"},
}

#: Keys of a step row's `error` detail that are measured durations (compared as <set>):
#: `refresh_basket` notes `computed_ms`, the basket build's own timing.
STEP_DETAIL_MASKS = ("computed_ms",)

#: `steps.NIGHTLY_CHAIN`'s values, in order.
NIGHTLY_STEPS = (
    "refresh_instruments", "fetch_daily_bars", "fetch_corporate_actions", "apply_adjustments",
    "refresh_index_membership", "refresh_index_snapshots", "compute_factors", "compute_market_health",
    "data_quality_gate", "publish", "refresh_basket", "compute_swing", "compute_vbt", "compute_twt",
)

#: The two run tables are compared in full by `run_tables`, never by digest.
RUN_TABLES = ("pipeline_run", "pipeline_run_step")

#: The side's environment, beyond the database, Redis and mail: nothing leaves the box.
TEST_ENV = {
    "BASKFY_ENVIRONMENT": "test",
    "BASKFY_LOG_JSON": "false",
    "BASKFY_LOG_LEVEL": "WARNING",
    "BASKFY_OTEL_ENABLED": "false",
    "BASKFY_SENTRY_DSN": "",
    "BASKFY_OPS_ALERT_WEBHOOK_URL": "",
    "BASKFY_OPS_ALERT_EMAIL": ALERT_ADDRESS,
    "BASKFY_EMAIL_TRANSPORT": "smtp",
    "BASKFY_SMTP_HOST": "127.0.0.1",
    "BASKFY_SMTP_STARTTLS": "false",
    # The box's sole tenant (the bed's owner), so the sleeves' steps run as they do there.
    "BASKFY_SOLE_USER_ID": "1",
    # No fixture directory: the replay is the only provider.
    "BASKFY_FIXTURE_DIR": "/nonexistent-nightly-diff-fixtures",
    "BASKFY_KITE_API_KEY": "",
    "BASKFY_S3_BUCKET": "",
    "TZ": "UTC",
    # Step 11 imports the desk's `app.config`, which runs `load_dotenv()` from its own folder and
    # so finds `kite-momentum-rebalancer/.env` (a real desk's keys) whatever the cwd is.
    # python-dotenv never overrides a key already in the environment, so every key that file may
    # carry is pinned here: no real secret reaches a side, and `FULLY_INVESTED` (the one key the
    # basket reads) is the desk's default on both sides (DECISIONS-RS R7.14).
    "FULLY_INVESTED": "false",
    "KITE_API_KEY": "",
    "KITE_API_SECRET": "",
    "DRY_RUN": "true",
    "PORT": "8420",
    "DESK_DB_BACKEND": "",
    "DESK_DB_SCHEMA": "",
    "DESK_DATABASE_URL": "",
}


def log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------------------------
# Guards, the lock, the budget, cleanup
# ---------------------------------------------------------------------------------------------


def guard(name: str) -> str:
    if name in SYSTEM_DBS or not re.fullmatch(rf"{PREFIX}[a-z0-9_]{{1,55}}", name):
        raise SystemExit(f"refusing to create, write or drop database {name!r} (only {PREFIX}* names)")
    return name


def db_url(name: str, *, asyncpg: bool) -> str:
    scheme = "postgresql+asyncpg" if asyncpg else "postgresql"
    return f"{scheme}://{DB_USER}:{DB_PASSWORD}@{BED_URL_HOST}/{guard(name)}"


def databases() -> list[str]:
    return [l for l in psql("postgres", "SELECT datname FROM pg_database ORDER BY 1").split() if l]


def db_exists(name: str) -> bool:
    return name in databases()


CREATED: list[str] = []


def drop(name: str) -> None:
    guard(name)
    psql("postgres", f"DROP DATABASE IF EXISTS {name} WITH (FORCE);", check=False)


def cleanup() -> None:
    while CREATED:
        name = CREATED.pop()
        try:
            drop(name)
        except Exception as exc:  # cleanup must try every database
            print(f"could not drop {name}: {exc}", file=sys.stderr)


def on_signal(signum: int, _frame: object) -> None:
    cleanup()
    raise SystemExit(128 + signum)


def take_lock() -> Any:
    TARGET.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(f"another heavy run holds {LOCK_FILE}; waiting for it")
        fcntl.flock(handle, fcntl.LOCK_EX)
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid {os.getpid()} since {dt.datetime.now(dt.UTC).isoformat()}\n")
    handle.flush()
    return handle


def cache_meta(name: str) -> Path:
    return CACHE_DIR / f"{name}.json"


def touch_cache(name: str, extra: dict[str, Any] | None = None) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    meta = cache_meta(name)
    data = json.loads(meta.read_text()) if meta.exists() else {}
    data.update(extra or {})
    data["used"] = time.time()
    meta.write_text(json.dumps(data))


def make_room(max_dbs: int, keep: set[str], wait_seconds: float = 900) -> None:
    """Ensure one more database fits under ``max_dbs`` (evicting our own caches, then waiting)."""
    deadline = time.monotonic() + wait_seconds
    while True:
        others = [d for d in databases() if d not in SYSTEM_DBS]
        if len(others) < max_dbs:
            return
        caches = [d for d in others if re.match(rf"{PREFIX}(tpl|primed)_", d) and d not in keep]
        if caches:
            caches.sort(key=lambda d: json.loads(cache_meta(d).read_text()).get("used", 0)
                        if cache_meta(d).exists() else 0)
            victim = caches[0]
            log(f"  evicting cached template {victim} (database budget {max_dbs})")
            drop(victim)
            cache_meta(victim).unlink(missing_ok=True)
            continue
        if time.monotonic() > deadline:
            raise SystemExit(f"{len(others)} databases besides {SOURCE_DB} exist ({others}); "
                             f"the budget is {max_dbs} — refusing to create another")
        log(f"  {len(others)} databases besides {SOURCE_DB} exist; waiting for room")
        time.sleep(10)


def clone(source: str, name: str, max_dbs: int, keep: set[str], *, track: bool = True) -> None:
    guard(name)
    make_room(max_dbs, keep | {source})
    last: Exception | None = None
    for _ in range(30):
        psql("postgres", f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{source}';",
             check=False)
        try:
            psql("postgres", f"CREATE DATABASE {name} TEMPLATE {source} STRATEGY FILE_COPY;")
            if track:
                CREATED.append(name)
            return
        except RuntimeError as exc:
            last = exc
            time.sleep(2)
    raise SystemExit(f"could not clone {source} into {name}: {last}")


def close_template(name: str) -> None:
    psql("postgres", f"ALTER DATABASE {guard(name)} WITH ALLOW_CONNECTIONS false;")
    psql("postgres", f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{name}';")


# ---------------------------------------------------------------------------------------------
# Templates: the scenario's starting bed, and primed steps
# ---------------------------------------------------------------------------------------------


def ensure_template(kind: str, day: dt.date, max_dbs: int) -> str:
    name = guard(f"{PREFIX}tpl_{kind}_{day.strftime('%Y%m%d')}")
    if db_exists(name):
        touch_cache(name)
        return name
    log(f"building template {name} from {SOURCE_DB}")
    make_room(max_dbs, set())
    deadline = time.monotonic() + 900
    while True:
        busy = psql("postgres", f"SELECT count(*) FROM pg_stat_activity WHERE datname = '{SOURCE_DB}' "
                                "AND application_name NOT LIKE 'TimescaleDB%'").strip()
        if busy == "0":
            done = subprocess.run(
                ["docker", "exec", "-i", CONTAINER, "psql", "-U", DB_USER, "-d", "postgres", "-At", "-q"],
                input=(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{SOURCE_DB}' "
                       "AND application_name LIKE 'TimescaleDB%';\n"
                       f"CREATE DATABASE {name} TEMPLATE {SOURCE_DB} STRATEGY FILE_COPY;\n"),
                capture_output=True, text=True, check=False,
            )
            if db_exists(name):
                break
            log(f"  clone not yet possible ({done.stderr.strip().splitlines()[-1:]}); retrying")
        else:
            log(f"  {busy} session(s) on {SOURCE_DB}; waiting to clone")
        if time.monotonic() > deadline:
            raise SystemExit(f"could not clone {SOURCE_DB}")
        time.sleep(3)
    try:
        psql(name, "SELECT alter_job(job_id, scheduled => false) FROM timescaledb_information.jobs;")
        if kind == "fresh":
            d = day.isoformat()
            psql(name, ";\n".join(s.format(d=d) for s in FRESH_DELETES) + ";")
        close_template(name)
    except BaseException:
        drop(name)
        raise
    touch_cache(name, {"kind": kind, "day": day.isoformat()})
    return name


def python_sources_hash() -> str:
    digest = hashlib.sha256()
    roots = [
        BLUEPRINT / "services/worker/src/baskfy_worker",
        BLUEPRINT / "packages/core/src/baskfy_core",
        BLUEPRINT / "packages/providers/src/baskfy_providers",
        BLUEPRINT / "services/api/src/baskfy_api",
    ]
    files = [p for r in roots for p in r.rglob("*.py")]
    files += [Path(__file__).resolve().parent / f for f in ("nightly_py.py", "replay_provider.py")]
    for path in sorted(files):
        digest.update(str(path.relative_to(REPO)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def bundle_hash(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(directory)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


# ---------------------------------------------------------------------------------------------
# Sides
# ---------------------------------------------------------------------------------------------


@dataclass
class Side:
    label: str
    impl: str
    db: str
    redis_db: int
    started: dt.datetime | None = None
    payload: Any = None
    exit_code: int | None = None
    mails: list[Any] = field(default_factory=list)


def python_env() -> dict[str, str]:
    return {
        "PYTHONPATH": os.pathsep.join(
            str(p) for p in (
                REPO,
                BLUEPRINT / "packages/core/src",
                BLUEPRINT / "packages/providers/src",
                BLUEPRINT / "packages/execution/src",
                BLUEPRINT / "services/api/src",
                BLUEPRINT / "services/worker/src",
            )
        )
    }


def side_command(side: Side, args: argparse.Namespace, bundle: Path, day: dt.date,
                 run_id: int | None, context: Path | None) -> list[str]:
    tail: list[str]
    if args.step:
        tail = ["step", args.step, "--date", day.isoformat(), "--run-id", str(run_id)]
        if context is not None:
            tail += ["--context", str(context)]
    else:
        tail = ["nightly", "--date", day.isoformat()]
    tail += ["--replay-bundle", str(bundle)]
    if side.impl == "py":
        return [str(BLUEPRINT / ".venv/bin/python"), str(Path(__file__).resolve().parent / "nightly_py.py"), *tail]
    return [str(TARGET / args.profile / "baskfy-worker"), *tail]


def run_side(side: Side, args: argparse.Namespace, bundle: Path, day: dt.date, run_id: int | None,
             context: Path | None, sink: MailSink, report_dir: Path) -> None:
    redis_flush(REDIS_URL, side.redis_db)
    env = {
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        **TEST_ENV,
        "BASKFY_DATABASE_URL": db_url(side.db, asyncpg=True),
        "BASKFY_REDIS_URL": f"{REDIS_URL}/{side.redis_db}",
        "BASKFY_SMTP_PORT": str(args.smtp_port),
    }
    if side.impl == "py":
        env.update(python_env())
    with tempfile.TemporaryDirectory(prefix=f"nightly-diff-{side.label}-") as work:
        deep = Path(work) / "a" / "b"
        deep.mkdir(parents=True)  # no .env can leak in: settings read ./.env and ../../.env
        mark = sink.mark()
        side.started = dt.datetime.now(dt.UTC)
        t0 = time.monotonic()
        log(f"  side {side.label} ({side.impl}) on {side.db} ...")
        proc = subprocess.run(side_command(side, args, bundle, day, run_id, context),
                              cwd=deep, env=env, capture_output=True, text=True, check=False)
        log(f"  side {side.label} exited {proc.returncode} after {time.monotonic() - t0:.0f}s")
    side.exit_code = proc.returncode
    (report_dir / f"side_{side.label}_{side.impl}.log").write_text(proc.stderr)
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    try:
        side.payload = json.loads(lines[-1]) if lines else None
    except ValueError:
        side.payload = {"unparseable stdout": proc.stdout[-2000:]}
    time.sleep(0.5)
    side.mails = [mail_view(m, [r"\"raised_at\": \"[^\"]+\"", r"raised_at&quot;: &quot;[^&]+&quot;"])
                  for m in sink.since(mark)]


# ---------------------------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------------------------


def json_rows(db: str, sql: str) -> list[dict[str, Any]]:
    text = psql(db, f"SELECT coalesce(json_agg(q), '[]'::json) FROM ({sql}) q;")
    return json.loads(text) if text.strip() else []


def touched_tables(db: str) -> set[str]:
    rows = json_rows(
        db,
        "SELECT s.schemaname, s.relname FROM pg_stat_user_tables s "
        "WHERE s.n_tup_ins + s.n_tup_upd + s.n_tup_del > 0",
    )
    chunks = json_rows(
        db,
        "SELECT c.schema_name, c.table_name, coalesce(p.table_name, h.table_name) AS hyper, "
        "coalesce(p.schema_name, h.schema_name) AS hyper_schema "
        "FROM _timescaledb_catalog.chunk c JOIN _timescaledb_catalog.hypertable h ON h.id = c.hypertable_id "
        "LEFT JOIN _timescaledb_catalog.hypertable p ON p.compressed_hypertable_id = h.id",
    )
    by_chunk = {(c["schema_name"], c["table_name"]): (c["hyper_schema"], c["hyper"]) for c in chunks}
    out: set[str] = set()
    for r in rows:
        schema, table = by_chunk.get((r["schemaname"], r["relname"]), (r["schemaname"], r["relname"]))
        if schema == "public":
            out.add(table)
    return out


def columns(db: str, table: str) -> list[tuple[str, str]]:
    rows = json_rows(
        db,
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS t FROM pg_attribute a "
        f"WHERE a.attrelid = 'public.{table}'::regclass AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum",
    )
    return [(r["attname"], r["t"]) for r in rows]


def primary_key(db: str, table: str) -> list[str]:
    rows = json_rows(
        db,
        "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
        f"AND a.attnum = ANY(i.indkey) WHERE i.indrelid = 'public.{table}'::regclass AND i.indisprimary "
        "ORDER BY array_position(i.indkey, a.attnum)",
    )
    return [r["attname"] for r in rows]


def masked_expr(col: str, kind: str, start: dt.datetime) -> str:
    if kind == "elapsed":
        return f"CASE WHEN {col} IS NULL THEN '<null>' ELSE '<set>' END"
    return (f"CASE WHEN {col} IS NULL THEN '<null>' WHEN {col} < TIMESTAMPTZ '{start.isoformat()}' "
            f"THEN '<before>' ELSE '<during>' END")


def row_sql(db: str, table: str, start: dt.datetime) -> tuple[str, str, list[str]]:
    masks = TABLE_MASKS.get(table, {})
    cols = columns(db, table)
    exprs = [masked_expr(f'"{c}"', masks[c], start) if c in masks else f'"{c}"' for c, _ in cols]
    pk = primary_key(db, table)
    key = f"json_build_array({', '.join(chr(34) + k + chr(34) for k in pk)})::text" if pk else None
    row = f"json_build_array({', '.join(exprs)})::text"
    return (key or row), row, [c for c, _ in cols]


def digest(db: str, table: str, start: dt.datetime) -> dict[int, tuple[int, str]]:
    key, row, _ = row_sql(db, table, start)
    rows = json_rows(
        db,
        f"SELECT b, count(*) AS n, md5(string_agg(h, '' ORDER BY h)) AS d FROM ("
        f"SELECT (hashtext(k) & {BUCKETS - 1}) AS b, md5(r) AS h FROM ("
        f"SELECT {key} AS k, {row} AS r FROM public.{table}) x) y GROUP BY b",
    )
    return {r["b"]: (r["n"], r["d"]) for r in rows}


def bucket_rows(db: str, table: str, start: dt.datetime, buckets: list[int]) -> dict[str, str]:
    key, row, _ = row_sql(db, table, start)
    rows = json_rows(
        db,
        f"SELECT k, r FROM (SELECT {key} AS k, {row} AS r FROM public.{table}) x "
        f"WHERE (hashtext(k) & {BUCKETS - 1}) = ANY(ARRAY[{','.join(map(str, buckets))}]::int[]) "
        "ORDER BY k LIMIT 20000",
    )
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["k"], []).append(r["r"])
    return {k: " | ".join(sorted(v)) for k, v in out.items()}


def compare_table(a: Side, b: Side, table: str, lines: list[str]) -> bool:
    assert a.started is not None and b.started is not None
    da, db_ = digest(a.db, table, a.started), digest(b.db, table, b.started)
    bad = sorted(k for k in set(da) | set(db_) if da.get(k) != db_.get(k))
    na, nb = sum(n for n, _ in da.values()), sum(n for n, _ in db_.values())
    masks = TABLE_MASKS.get(table, {})
    tag = f" (masked: {', '.join(f'{c} as {k}' for c, k in masks.items())})" if masks else ""
    if not bad:
        lines.append(f"  EQUAL   {table}: {na} rows{tag}")
        return True
    lines.append(f"  DIFFER  {table}: {na} vs {nb} rows, {len(bad)} of {BUCKETS} buckets differ{tag}")
    ra, rb = bucket_rows(a.db, table, a.started, bad[:64]), bucket_rows(b.db, table, b.started, bad[:64])
    shown = 0
    for k in sorted(set(ra) | set(rb)):
        if ra.get(k) == rb.get(k):
            continue
        lines.append(f"    key {k}\n      {a.label}: {ra.get(k, '<absent>')[:600]}\n      {b.label}: {rb.get(k, '<absent>')[:600]}")
        shown += 1
        if shown >= SHOW_ROWS:
            lines.append("    ...")
            break
    return False


def stamp(value: str | None, start: dt.datetime) -> str:
    if value is None:
        return "<null>"
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return "<before>" if parsed < start else "<during>"


def run_tables(side: Side) -> dict[str, Any]:
    assert side.started is not None
    runs = json_rows(side.db, "SELECT id, trade_date, status, started_at, finished_at, data_version "
                              "FROM pipeline_run ORDER BY id")
    rank = {r["id"]: i for i, r in enumerate(runs)}
    run_view = [
        {"rank": rank[r["id"]], "trade_date": r["trade_date"], "status": r["status"],
         "data_version": r["data_version"], "started_at": stamp(r["started_at"], side.started),
         "finished_at": stamp(r["finished_at"], side.started)}
        for r in runs
    ]
    steps = json_rows(side.db, "SELECT run_id, step, status, rows_in, rows_out, duration_ms, error "
                               "FROM pipeline_run_step ORDER BY run_id, step")
    step_view = []
    for s in steps:
        error = s["error"]
        if isinstance(error, dict):
            error = {k: ("<set>" if (k == "traceback" or k in STEP_DETAIL_MASKS) and v is not None else v)
                     for k, v in error.items()}
        step_view.append({"run": rank.get(s["run_id"], "<orphan>"), "step": s["step"], "status": s["status"],
                          "rows_in": s["rows_in"], "rows_out": s["rows_out"],
                          "duration_ms": "<null>" if s["duration_ms"] is None else "<set>", "error": error})
    return {"pipeline_run": run_view, "pipeline_run_step": step_view}


def compare_runs(a: Side, b: Side, lines: list[str]) -> bool:
    ra, rb = run_tables(a), run_tables(b)
    ok = True
    for table in RUN_TABLES:
        if ra[table] == rb[table]:
            lines.append(f"  EQUAL   {table}: {len(ra[table])} rows "
                         "(masked: ids as ranks, started_at/finished_at as <before>/<during>, "
                         "duration_ms as <set>, error.traceback and error." + "/".join(STEP_DETAIL_MASKS) + " as <set>)")
            continue
        ok = False
        lines.append(f"  DIFFER  {table}: {len(ra[table])} vs {len(rb[table])} rows")
        for x, y in zip(ra[table] + [None] * len(rb[table]), rb[table] + [None] * len(ra[table])):
            if x != y:
                lines.append(f"    {a.label}: {json.dumps(x)[:800]}\n    {b.label}: {json.dumps(y)[:800]}")
                if len(lines) > 400:
                    break
    return ok


def mask_payload(p: Any) -> Any:
    if isinstance(p, dict):
        return {k: ("<id>" if k == "run_id" and v is not None else mask_payload(v)) for k, v in p.items()}
    if isinstance(p, list):
        return [mask_payload(x) for x in p]
    return p


# ---------------------------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------------------------


def next_non_trading_day(day: dt.date) -> dt.date:
    out = psql(SOURCE_DB, "SELECT min(date) FROM trading_day WHERE exchange_id = 1 AND NOT is_trading_day "
                          f"AND date > DATE '{day.isoformat()}'").strip()
    return dt.date.fromisoformat(out)


def build_rust(profile: str) -> None:
    env = {**os.environ, "CARGO_TARGET_DIR": str(TARGET), "CARGO_BUILD_JOBS": "6"}
    cmd = ["nice", "-n", "10", str(CARGO), "build", "-p", "baskfy-worker"]
    if profile == "release":
        cmd.append("--release")
    log("building baskfy-worker ...")
    subprocess.run(cmd, cwd=REPO / "rust", env=env, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--chain", action="store_true")
    mode.add_argument("--step")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="fresh")
    parser.add_argument("--date", default="2026-10-29")
    parser.add_argument("--sides", default="py,rs", help="two of py/rs, e.g. py,py to grade the harness")
    parser.add_argument("--profile", choices=("debug", "release"), default="debug")
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--redis-db-a", type=int, default=11)
    parser.add_argument("--redis-db-b", type=int, default=12)
    parser.add_argument("--smtp-port", type=int, default=18399)
    parser.add_argument("--max-dbs", type=int, default=5)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--keep-dbs", action="store_true")
    parser.add_argument("--mutate", type=Path,
                        help="a SQL file applied identically to both sides' clones before either side runs "
                             "(a scenario edge the bed's day does not reach; printed in the report)")
    parser.add_argument("--drop-cache", action="store_true", help="drop every cached rsn_ template and exit")
    parser.add_argument("--probe-steps", action="store_true",
                        help="run every step of the Rust binary once into an open run on a clone of the "
                             "rerun template and print what each answered (the skeleton's report)")
    args = parser.parse_args(argv)

    if 0 in (args.redis_db_a, args.redis_db_b) or args.redis_db_a == args.redis_db_b:
        raise SystemExit("refusing Redis index 0, and the two sides need two indices")
    if not (args.chain or args.step or args.drop_cache or args.probe_steps):
        raise SystemExit("pass --chain or --step NAME")
    sides = args.sides.split(",")
    if len(sides) != 2 or any(s not in ("py", "rs") for s in sides):
        raise SystemExit("--sides takes two of py/rs")
    check_container()
    lock = take_lock()
    atexit.register(cleanup)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    try:
        if args.drop_cache:
            for name in databases():
                if re.match(rf"{PREFIX}(tpl|primed)_", name):
                    drop(name)
                    cache_meta(name).unlink(missing_ok=True)
                    log(f"dropped {name}")
            return 0
        if args.probe_steps:
            return probe_steps(args)
        return run(args, sides)
    finally:
        if args.keep_dbs:
            CREATED.clear()
        cleanup()
        lock.close()


def run(args: argparse.Namespace, sides: list[str]) -> int:
    day = dt.date.fromisoformat(args.date)
    kind, variant = SCENARIOS[args.scenario]
    run_day = next_non_trading_day(day) if args.scenario == "non-trading-day" else day
    bundle = REPLAY_ROOT / day.isoformat() / variant
    if not (bundle / "manifest.json").exists():
        log(f"building the {day} bundles ...")
        subprocess.run(
            [str(BLUEPRINT / ".venv/bin/python"), str(Path(__file__).resolve().parent / "replay_build.py"),
             "--date", day.isoformat()],
            cwd=BLUEPRINT, env={**os.environ, **python_env()}, check=True,
        )
    if "rs" in sides and not args.no_build:
        build_rust(args.profile)
    if not port_free(args.smtp_port):
        raise SystemExit(f"--smtp-port {args.smtp_port} is in use")

    report_dir = Path(tempfile.mkdtemp(prefix="nightly-diff-"))
    tpl = ensure_template(kind, day, args.max_dbs)
    source, run_id, context = tpl, None, None
    if args.step:
        source, run_id, context = ensure_primed(args, tpl, bundle, day)
    tag = f"{int(time.time()) % 100000:05d}{secrets.token_hex(2)}"
    a = Side("a", sides[0], guard(f"{PREFIX}run_{tag}_a"), args.redis_db_a)
    b = Side("b", sides[1], guard(f"{PREFIX}run_{tag}_b"), args.redis_db_b)
    keep = {tpl, source}
    clone(source, a.db, args.max_dbs, keep)
    clone(source, b.db, args.max_dbs, keep | {a.db})
    mutation = args.mutate.read_text() if args.mutate else None
    if mutation is not None:
        for side in (a, b):
            psql(side.db, mutation)
    seq_before = sequences(a.db)
    sink = MailSink(args.smtp_port)
    sink.start()
    try:
        for side in (a, b):
            run_side(side, args, bundle, run_day, run_id, context, sink, report_dir)
    finally:
        sink.stop()

    lines = [
        f"nightly_diff: {'step ' + args.step if args.step else 'chain'}, scenario {args.scenario}, "
        f"date {run_day}, bundle {bundle} (sha256 {bundle_hash(bundle)[:16]}), sides {a.impl},{b.impl}",
        f"  template {tpl}" + (f", primed {source} (run {run_id})" if args.step else ""),
        f"  side logs in {report_dir}",
    ]
    if mutation is not None:
        lines.append(f"  mutation {args.mutate} (sha256 {hashlib.sha256(mutation.encode()).hexdigest()[:16]}) "
                     "applied to both clones before the sides ran")
    ok = True
    pa, pb = mask_payload(a.payload), mask_payload(b.payload)
    if pa == pb and a.exit_code == b.exit_code:
        lines.append(f"  EQUAL   payload (exit {a.exit_code}; masked: run_id): {json.dumps(pa)[:600]}")
    else:
        ok = False
        lines.append(f"  DIFFER  payload\n    {a.label} exit {a.exit_code}: {json.dumps(pa)[:1500]}\n"
                     f"    {b.label} exit {b.exit_code}: {json.dumps(pb)[:1500]}")
    tables = sorted((touched_tables(a.db) | touched_tables(b.db)) - set(RUN_TABLES))
    lines.append(f"  tables touched on either side: {', '.join(tables) or '(none)'}")
    ok &= compare_runs(a, b, lines)
    for table in tables:
        ok &= compare_table(a, b, table, lines)
    sa, sb = sequences(a.db), sequences(b.db)
    moved = sorted(k for k in sa if sa.get(k) != seq_before.get(k) or sb.get(k) != seq_before.get(k))
    if sa == sb:
        lines.append(f"  EQUAL   sequences ({len(sa)}; moved: {', '.join(moved) or 'none'})")
    else:
        ok = False
        diff = {k: (sa.get(k), sb.get(k)) for k in sorted(set(sa) | set(sb)) if sa.get(k) != sb.get(k)}
        lines.append(f"  DIFFER  sequences: {diff}")
    ra, rb = redis_dump(REDIS_URL, a.redis_db, []), redis_dump(REDIS_URL, b.redis_db, [])
    if ra == rb:
        lines.append(f"  EQUAL   redis ({len(ra)} keys)")
    else:
        ok = False
        keys = sorted(k for k in set(ra) | set(rb) if ra.get(k) != rb.get(k))
        lines.append(f"  DIFFER  redis: {len(ra)} vs {len(rb)} keys; differing: {keys[:20]}")
        for k in keys[:5]:
            lines.append(f"    {k}\n      a: {json.dumps(ra.get(k), default=str)[:400]}\n"
                         f"      b: {json.dumps(rb.get(k), default=str)[:400]}")
    if a.mails == b.mails:
        lines.append(f"  EQUAL   mail ({len(a.mails)} message(s); masked: raised_at)")
    else:
        ok = False
        lines.append(f"  DIFFER  mail: {len(a.mails)} vs {len(b.mails)}\n    a: {json.dumps(a.mails)[:1500]}\n"
                     f"    b: {json.dumps(b.mails)[:1500]}")
    for side in (a, b):
        redis_flush(REDIS_URL, side.redis_db)
    lines.append("RESULT: " + ("EQUAL" if ok else "DIFFERENT"))
    text = "\n".join(lines)
    print(text)
    if args.report:
        args.report.write_text(text + "\n")
    return 0 if ok else 1


def probe_steps(args: argparse.Namespace) -> int:
    """Every step of the Rust binary, once, into one open run (no comparison: a report)."""
    day = dt.date.fromisoformat(args.date)
    bundle = REPLAY_ROOT / day.isoformat() / "base"
    if not args.no_build:
        build_rust(args.profile)
    tpl = ensure_template("rerun", day, args.max_dbs)
    name = guard(f"{PREFIX}probe_{secrets.token_hex(3)}")
    clone(tpl, name, args.max_dbs, {tpl})
    run_id = int(psql(name, "INSERT INTO pipeline_run (trade_date, status, started_at) VALUES "
                            f"(DATE '{day.isoformat()}', 'running', now()) RETURNING id;").split()[0])
    side = Side("rs", "rs", name, args.redis_db_b)
    report_dir = Path(tempfile.mkdtemp(prefix="nightly-probe-"))
    ctx = report_dir / "ctx.json"
    ctx.write_text(json.dumps({"touched": []}))
    sink = MailSink(args.smtp_port)
    sink.start()
    results = []
    try:
        for step in NIGHTLY_STEPS:
            args.step = step
            run_side(side, args, bundle, day, run_id, ctx, sink, report_dir)
            results.append((step, side.exit_code, side.payload))
    finally:
        sink.stop()
        args.step = None
        redis_flush(REDIS_URL, side.redis_db)
    for step, code, payload in results:
        print(f"  {step:<26} exit {code}: {json.dumps(payload)}")
    steps_rows = psql(name, f"SELECT count(*) FROM pipeline_run_step WHERE run_id = {run_id};").strip()
    print(f"  pipeline_run_step rows left for run {run_id}: {steps_rows} (each raise rolled back)")
    return 0


def ensure_primed(args: argparse.Namespace, tpl: str, bundle: Path, day: dt.date) -> tuple[str, int, Path]:
    from_step = args.step
    key = hashlib.sha256(
        f"{tpl}|{from_step}|{bundle_hash(bundle)}|{python_sources_hash()}".encode()
    ).hexdigest()[:12]
    short = re.sub(r"[^a-z]", "", from_step)[:24]
    name = guard(f"{PREFIX}primed_{short}_{key}")
    ctx = CACHE_DIR / f"{name}.context.json"
    if db_exists(name) and ctx.exists():
        touch_cache(name)
        data = json.loads(ctx.read_text())
        log(f"reusing primed template {name} (run {data['run_id']})")
        return name, int(data["run_id"]), ctx
    if db_exists(name):
        drop(name)
    log(f"priming {name}: Python's chain up to {from_step}")
    clone(tpl, name, args.max_dbs, {tpl}, track=False)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        env = {"HOME": os.environ.get("HOME", "/tmp"), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", **TEST_ENV,
               **python_env(), "BASKFY_DATABASE_URL": db_url(name, asyncpg=True),
               "BASKFY_REDIS_URL": f"{REDIS_URL}/{args.redis_db_a}", "BASKFY_SMTP_PORT": str(args.smtp_port)}
        with tempfile.TemporaryDirectory(prefix="nightly-diff-prime-") as work:
            deep = Path(work) / "a" / "b"
            deep.mkdir(parents=True)
            proc = subprocess.run(
                [str(BLUEPRINT / ".venv/bin/python"), str(Path(__file__).resolve().parent / "nightly_py.py"),
                 "prime", "--until", from_step, "--date", day.isoformat(), "--replay-bundle", str(bundle),
                 "--context-out", str(ctx)],
                cwd=deep, env=env, capture_output=True, text=True, check=False,
            )
        redis_flush(REDIS_URL, args.redis_db_a)
        if proc.returncode != 0:
            raise SystemExit(f"priming failed:\n{proc.stderr[-4000:]}")
        close_template(name)
    except BaseException:
        drop(name)
        ctx.unlink(missing_ok=True)
        raise
    touch_cache(name, {"kind": "primed", "step": from_step})
    data = json.loads(ctx.read_text())
    return name, int(data["run_id"]), ctx


if __name__ == "__main__":
    sys.exit(main())
