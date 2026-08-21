"""Operations registry: the command-line surface, runnable from one page.

SECURITY MODEL — READ BEFORE ADDING AN OPERATION
This is a FIXED ALLOWLIST. Each operation owns a hardcoded argv template; the browser
sends only an operation name and a small set of typed, validated parameters. There is no
free-form command field, nothing is interpolated into a shell, and every subprocess runs
with shell=False. A page that can run an arbitrary string is remote code execution on the
machine holding your broker credentials.

Parameters are validated against their own rules before they reach argv: a file parameter
must resolve inside data/uploads, a date must parse as ISO, a flag is a boolean. An
operation with an invalid parameter never starts.

NOTHING HERE PLACES AN ORDER. Order flow stays on the desk behind its confirmation gate
and the DRY_RUN guarantee. These are data collection, evaluation and diagnostics only.

ONE AT A TIME. Every operation touches the same SQLite file and the same broker session,
so a second run is refused while one is in flight rather than racing it.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import db

UPLOADS = Path("data/uploads")
PYTHON = sys.executable
_LOCK = threading.Lock()
_LIVE: set[int] = set()          # job ids this process is actually working on


class OpsError(ValueError):
    pass


# =====================================================================================
# parameters
# =====================================================================================
@dataclass(frozen=True)
class Param:
    key: str
    label: str
    kind: str                       # flag | date | upload | text_choice
    default: Any = None
    choices: tuple[str, ...] = ()
    help: str = ""

    def validate(self, raw: Any) -> Any:
        if self.kind == "flag":
            return str(raw).lower() in {"1", "true", "on", "yes"}
        if raw in (None, ""):
            return None
        s = str(raw).strip()
        if self.kind == "date":
            try:
                dt.date.fromisoformat(s)
            except ValueError:
                raise OpsError(f"{self.label}: {s!r} is not a YYYY-MM-DD date")
            return s
        if self.kind == "upload":
            # Resolve and confine to data/uploads. A path parameter that escapes its
            # directory is how a file picker becomes an arbitrary file read.
            target = (UPLOADS / Path(s).name).resolve()
            if UPLOADS.resolve() not in target.parents:
                raise OpsError(f"{self.label}: must be a file inside {UPLOADS}")
            if not target.exists():
                raise OpsError(f"{self.label}: {target.name} does not exist")
            return str(target)
        if self.kind == "text_choice":
            if s not in self.choices:
                raise OpsError(f"{self.label}: {s!r} is not one of {', '.join(self.choices)}")
            return s
        raise OpsError(f"unknown parameter kind {self.kind!r}")


@dataclass(frozen=True)
class Operation:
    name: str
    label: str
    group: str
    summary: str
    build: Callable[[Mapping[str, Any]], list[str]]     # -> argv, never a shell string
    timeout: int = 300
    params: tuple[Param, ...] = ()
    needs_kite: bool = False
    writes: bool = True
    long_running: bool = False

    def argv(self, values: Mapping[str, Any]) -> list[str]:
        clean = {p.key: p.validate(values.get(p.key)) for p in self.params}
        return [PYTHON, *self.build(clean)]

    def cli(self, values: Mapping[str, Any] | None = None) -> str:
        try:
            argv = self.argv(values or {})
        except OpsError:
            argv = [PYTHON, *self.build({p.key: p.default for p in self.params})]
        return "python " + " ".join(argv[1:])


def _scan_choices() -> tuple[str, ...]:
    if not UPLOADS.exists():
        return ()
    return tuple(sorted(p.name for p in UPLOADS.glob("*.csv")))


SCAN = Param("scan", "Scan CSV", "upload", help="A weekly momentum scan in data/uploads")

# How many 5-second polls a paper session runs for. Bounded on purpose: operations share
# one lock, so an unbounded session would hold it until 15:10 and block the equity daily
# job behind it. A full unattended session belongs in the scheduler, not in a web request.
TICKS = Param("ticks", "Poll for", "text_choice", default="60",
              choices=("12", "60", "360"),
              help="12 polls is about a minute, 60 about five, 360 about thirty")

# Which underlying an options control acts on. Each keeps its own config, journal,
# lockout, straddle record and session lock, so these never interfere with each other.
#
# FROZEN SUBSYSTEM (M6). The strangle package lives in frozen/strangle/ and is outside
# every gate, so this import must not run at module scope: importing app.analytics.ops is
# on the path of the *equity* /ops page, and the desk has to boot whether or not the
# options lab is present. Both the import and the controls it builds are therefore
# deferred into _options_operations(), which returns nothing unless OPTIONS_ENABLED is
# set AND the subsystem has been thawed. See frozen/strangle/README.md.
def _slug(v: Mapping[str, Any]) -> str:
    from ..strategies.strangle import instruments as _INS
    return str(v.get("instrument") or _INS.DEFAULT)


def _options_operations() -> tuple["Operation", ...]:
    """The strangle controls, or nothing.

    Nothing is the normal answer: OPTIONS_ENABLED defaults off, and since M6 the code it
    would drive is frozen. A missing subsystem is reported once, plainly, rather than
    raising an ImportError out of a page that has nothing to do with options.
    """
    from .. import config as C

    if not C.OPTIONS_ENABLED:
        return ()
    try:
        from ..strategies.strangle import instruments as _INS
    except ImportError:
        logging.warning(
            "OPTIONS_ENABLED is set, but the strangle subsystem is frozen "
            "(frozen/strangle/). The options controls are unavailable until it is thawed; "
            "everything else is unaffected.")
        return ()

    INSTRUMENT = Param("instrument", "Underlying", "text_choice", default=_INS.DEFAULT,
                       choices=tuple(_INS.all_slugs()),
                       help="NIFTY and SENSEX expire weekly (Tue / Thu); BANKNIFTY is "
                            "monthly and only trades its final week")
    return _OPTIONS_OPERATIONS(INSTRUMENT)


def _OPTIONS_OPERATIONS(INSTRUMENT: "Param") -> tuple["Operation", ...]:
    """The five strangle controls, built only when the subsystem is present.

    Lifted out of OPERATIONS at M6 so that importing this module never reaches
    frozen/strangle/. The bodies are unchanged; only the indentation and the INSTRUMENT
    parameter moved.
    """
    return (
        # --- options: the intraday strangle. PAPER ONLY. ---------------------------------
        # Live execution sits behind seven locks (app/strategies/strangle/live.py) and every
        # one of them is shut, so none of these can reach a real order however they are run.
        Operation(
            "strangle_check", "Check the strangle", "Options",
            "Market facts, the trading calendar, today's session parameters and every veto "
            "standing between now and an entry. Places nothing and writes nothing.",
            lambda v: ["-m", "scripts.strangle", "--instrument", _slug(v), "--check"],
            timeout=180, params=(INSTRUMENT,), needs_kite=True, writes=False),
        Operation(
            "strangle_collect", "Record today's straddle", "Options",
            "One ATM straddle observation for the reference bands, then stops. This is the "
            "daily job while the bands are being built: the IV gates cannot be calibrated "
            "from history because Kite drops expired contracts.",
            lambda v: ["-m", "scripts.strangle", "--instrument", _slug(v), "--collect"],
            timeout=180, params=(INSTRUMENT,), needs_kite=True),
        Operation(
            "strangle_calibrate", "Calibrate bands (report)", "Options",
            "Rebuild the ATM straddle bands from the forward record and report the veto rate "
            "each would produce on its own history. Writes nothing.",
            lambda v: ["-m", "scripts.strangle_calibrate", "--instrument", _slug(v)],
            timeout=900, params=(INSTRUMENT,), needs_kite=True, writes=False,
            long_running=True),
        Operation(
            "strangle_calibrate_write", "Calibrate bands and save", "Options",
            "The same, but writes the bands into the selected instrument's config. Refuses "
            "to write while any tradeable bucket is missing or thin.",
            lambda v: ["-m", "scripts.strangle_calibrate", "--instrument", _slug(v), "--write"],
            timeout=900, params=(INSTRUMENT,), needs_kite=True, long_running=True),
        Operation(
            "strangle_session", "Run a paper session", "Options",
            "Enter on live quotes, then manage the book: exits first, then at most one "
            "adjustment per poll. Fills are simulated against real depth; no order is placed.",
            lambda v: ["-m", "scripts.strangle", "--instrument", _slug(v),
                       "--max-ticks", str(v.get("ticks") or "60")],
            timeout=3600, params=(INSTRUMENT, TICKS), needs_kite=True, long_running=True),
    )


OPERATIONS: tuple[Operation, ...] = (
    # --- daily ---------------------------------------------------------------------
    Operation(
        "daily", "Run the daily collection", "Daily",
        "Index history, benchmark PRI, EOD snapshot, breadth if a scan is given, and an "
        "observe-mode regime preview. Idempotent: re-running changes nothing.",
        lambda v: ["-m", "scripts.daily", "--source", "page"]
                  + (["--scan", v["scan"]] if v.get("scan") else []),
        timeout=900, params=(SCAN,), needs_kite=True, long_running=True),
    Operation(
        "daily_check", "Check collection state", "Daily",
        "Row counts, snapshot span and how many days since the last one. Reads only.",
        lambda v: ["-m", "scripts.daily", "--check"], timeout=60, writes=False),

    # --- data ----------------------------------------------------------------------
    Operation(
        "snapshot", "Take an EOD snapshot", "Data",
        "NAV, holdings and cash into the TWR index chain. Run after 15:30 IST; a run "
        "before the close would record intraday prices as the close.",
        lambda v: ["-m", "app.analytics.snapshot"] + (["--force"] if v.get("force") else []),
        timeout=180, needs_kite=True,
        params=(Param("force", "Overwrite today's row", "flag",
                      help="Default keeps the stored values untouched"),)),
    Operation(
        "index_update", "Update index history", "Data",
        "Fetch missing daily candles for the structural indices and the momentum "
        "sentinel. Only missing dates are requested, and only this step of the daily "
        "run happens — no snapshot, no regime preview.",
        lambda v: ["-m", "scripts.daily", "--only", "index history"],
        timeout=600, needs_kite=True, long_running=True),
    Operation(
        "benchmark_pri", "Refresh benchmark PRI", "Data",
        "Price-return levels for NIFTY 500 from Kite. TRI remains authoritative for "
        "reported numbers; PRI understates the index by roughly its dividend yield.",
        lambda v: ["-m", "app.analytics.benchmark", "--pri", "NIFTY 500",
                   "--from", v.get("start") or "2020-01-01"],
        timeout=600, needs_kite=True, long_running=True,
        params=(Param("start", "From date", "date", default="2020-01-01"),)),
    Operation(
        "benchmark_list", "List index symbols", "Data",
        "Every index instrument Kite exposes. Tradingsymbols drift, so verify one here "
        "before wiring it into a config.",
        lambda v: ["-m", "app.analytics.benchmark", "--list", "NIFTY"],
        timeout=120, needs_kite=True, writes=False),
    Operation(
        "benchmark_show", "Show stored benchmarks", "Data",
        "Coverage per index: how many PRI and TRI rows exist and over what span.",
        lambda v: ["-m", "app.analytics.benchmark"], timeout=60, writes=False),

    # --- tax lots -------------------------------------------------------------------
    Operation(
        "tradebook_reconcile", "Reconcile tax lots", "Tax lots",
        "Compare reconstructed FIFO lots against live holdings. A quantity mismatch "
        "usually means a corporate action moved shares without a trade.",
        lambda v: ["-m", "app.analytics.tradebook", "--reconcile"],
        timeout=180, needs_kite=True, writes=False),
    Operation(
        "protection", "Check stop coverage", "Diagnostics",
        "Compare live holdings against live GTT triggers. Every buy is supposed to get a "
        "stop, but one can fail to place, expire, be cancelled, or cover fewer shares "
        "than are now held — and each of those is silent. Reads only; never places or "
        "cancels a stop.",
        lambda v: ["-m", "app.analytics.protection"],
        timeout=90, needs_kite=True, writes=False),
    Operation(
        "tradebook_capture", "Capture today's fills", "Tax lots",
        "Record today's executed trades and rebuild the lots they touch. Kite's trade "
        "book is same-day only, so a fill not captured on its own session survives "
        "nowhere but a Console export. Idempotent: repeating it changes nothing.",
        lambda v: ["-m", "app.analytics.tradebook", "--capture"],
        timeout=120, needs_kite=True),
    Operation(
        "tradebook_summary", "Summarise tax lots", "Tax lots",
        "How many lots are stored and how many are still open. Reads only.",
        lambda v: ["-m", "app.analytics.tradebook", "--summary"], timeout=60, writes=False),

    # --- regime ----------------------------------------------------------------------
    Operation(
        "backtest", "Run the regime backtest", "Regime",
        "Every variant and ablation over the cached history, saved for the backtest "
        "page. Reports limitations before results; nothing here is endorsed.",
        lambda v: ["-m", "scripts.backtest_regime", "--run",
                   "--start", v.get("start") or "2005-04-01", "--save"],
        timeout=1800, long_running=True,
        params=(Param("start", "From date", "date", default="2005-04-01"),)),
    Operation(
        "backtest_full", "Backtest with robustness", "Regime",
        "The above plus the parameter grid and event timelines. Slow.",
        lambda v: ["-m", "scripts.backtest_regime", "--run", "--robustness", "--timelines",
                   "--start", v.get("start") or "2005-04-01", "--save"],
        timeout=3600, long_running=True,
        params=(Param("start", "From date", "date", default="2005-04-01"),)),
    Operation(
        "backtest_fetch", "Fetch maximum index history", "Regime",
        "Download every available daily candle for the regime indices. Long, and only "
        "needed once.",
        lambda v: ["-m", "scripts.backtest_regime", "--fetch", "--start", "2005-01-01"],
        timeout=1800, needs_kite=True, long_running=True),

    # --- diagnostics --------------------------------------------------------------------
    Operation(
        "db_info", "Database status", "Diagnostics",
        "Schema version, journal mode, row counts and the snapshot span.",
        lambda v: ["-m", "app.analytics.db", "--info"], timeout=60, writes=False),
    Operation(
        "scoring_smoke", "Score a scan", "Diagnostics",
        "Run the scoring engine over a scan and print the top ranks. Changes nothing.",
        lambda v: ["-m", "app.scoring", v.get("scan") or str(UPLOADS / "sample_scan.csv")],
        timeout=120, writes=False, params=(SCAN,)),
    Operation(
        "autorun", "Collect what today still needs", "Daily",
        "Runs whatever is outstanding for today and nothing else: the daily collection if "
        "no complete run has landed, and the ATM straddle observation if none is recorded. "
        "Fires automatically after a Kite login, because that is the moment the token that "
        "blocks both jobs becomes available.",
        lambda v: ["-m", "scripts.autorun"],
        timeout=1800, needs_kite=True, long_running=True),
    Operation(
        "autorun_check", "What does today still need?", "Daily",
        "Reports the outstanding collection and why, without running anything.",
        lambda v: ["-m", "scripts.autorun", "--check"],
        timeout=180, needs_kite=True, writes=False),

    Operation(
        "tests", "Run the test suite", "Diagnostics",
        "The full suite. Slow, but the fastest way to know the system is intact after a "
        "settings change or an upgrade.",
        lambda v: ["-m", "pytest", "tests/", "-q"], timeout=900, writes=False,
        long_running=True),
)

def all_operations() -> tuple[Operation, ...]:
    """Every operation the page may offer, options included when they are available.

    A function rather than a constant: whether the options controls exist depends on
    OPTIONS_ENABLED and on whether the frozen subsystem has been thawed, and neither is
    knowable at import time (M6).
    """
    return OPERATIONS + _options_operations()


def by_name() -> Mapping[str, Operation]:
    """Name -> operation, options included when available.

    BY_NAME is layered on last on purpose. It is the module attribute tests monkeypatch to
    inject a fake operation, and resolving `start()` through a freshly built dict would have
    silently ignored the patch — which is exactly how four ops tests failed when this became
    a function at M6.
    """
    return {**{o.name: o for o in _options_operations()}, **BY_NAME}


def groups() -> tuple[str, ...]:
    return tuple(dict.fromkeys(o.group for o in all_operations()))


#: Back-compatible module attributes. The equity operations are fixed at import; callers
#: that need the options ones too use all_operations() / by_name() / groups().
BY_NAME: Mapping[str, Operation] = {o.name: o for o in OPERATIONS}
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(o.group for o in OPERATIONS))


# =====================================================================================
# running
# =====================================================================================
def reclaim_orphans(conn) -> int:
    """Close out jobs whose worker no longer exists.

    The worker is a daemon thread: stopping the server mid-run kills it without ever
    writing a finish row. Left alone that row reads as 'running' forever, which disables
    every button on the page — the page would be bricked until someone edited SQLite by
    hand. A 'running' row whose id this process is not actually working on belongs to a
    process that is gone, so it is closed as interrupted.
    """
    rows = conn.execute("SELECT id FROM ops_jobs WHERE status='running'").fetchall()
    orphans = [r["id"] for r in rows if r["id"] not in _LIVE]
    if orphans:
        with db.transaction(conn):
            conn.executemany(
                "UPDATE ops_jobs SET status='interrupted',"
                " output=COALESCE(NULLIF(output,''),?) WHERE id=?",
                [("the server stopped while this was running; nothing here is left "
                  "half-written — every operation is idempotent, so just run it again.",
                  i) for i in orphans])
    return len(orphans)


def running_job(conn) -> dict | None:
    reclaim_orphans(conn)
    row = conn.execute(
        "SELECT * FROM ops_jobs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def _finish(job_id: int, status: str, code: int | None, output: str, started: dt.datetime):
    now = dt.datetime.now()
    with db.connect() as conn:
        with db.transaction(conn):
            conn.execute(
                "UPDATE ops_jobs SET status=?, exit_code=?, finished_at=?, duration_s=?,"
                " output=? WHERE id=?",
                (status, code, now.isoformat(timespec="seconds"),
                 round((now - started).total_seconds(), 2), output[-20000:], job_id))


def _execute(job_id: int, argv: list[str], timeout: int, started: dt.datetime):
    try:
        # shell=False and a fixed argv list: nothing is ever interpreted by a shell.
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              cwd=os.getcwd(), shell=False)
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        _finish(job_id, "ok" if proc.returncode == 0 else "failed", proc.returncode,
                out.strip(), started)
    except subprocess.TimeoutExpired:
        _finish(job_id, "timeout", None,
                f"exceeded the {timeout}s limit and was stopped", started)
    except Exception as exc:
        _finish(job_id, "failed", None, f"could not start: {exc}", started)
    finally:
        _LIVE.discard(job_id)
        if _LOCK.locked():
            _LOCK.release()


def start(conn, name: str, values: Mapping[str, Any] | None = None) -> dict:
    """Validate, record and launch. Refuses if another operation is already running."""
    op = by_name().get(name)
    if op is None:
        raise OpsError(f"{name!r} is not a known operation")
    argv = op.argv(values or {})            # raises OpsError before anything starts

    if not _LOCK.acquire(blocking=False):
        raise OpsError("another operation is already running; they share the database "
                       "and the broker session, so they are not run concurrently")
    try:
        started = dt.datetime.now()
        with db.transaction(conn):
            cur = conn.execute(
                "INSERT INTO ops_jobs(name, argv_json, status, started_at) VALUES(?,?,?,?)",
                (name, json.dumps(argv[1:]), "running",
                 started.isoformat(timespec="seconds")))
            job_id = int(cur.lastrowid)
        _LIVE.add(job_id)
    except Exception:
        _LOCK.release()
        raise

    threading.Thread(target=_execute, args=(job_id, argv, op.timeout, started),
                     daemon=True).start()
    return {"job_id": job_id, "name": name, "cli": op.cli(values or {})}


def history(conn, limit: int = 25) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, name, argv_json, status, exit_code, started_at, finished_at,"
        " duration_s, output FROM ops_jobs ORDER BY id DESC LIMIT ?", (limit,))]


def last_run(conn) -> dict[str, dict]:
    """Most recent outcome per operation, for the card badges.

    `id` is selected because the badge links to /ops/job/<id> for the output. It was
    missing, so that link silently never rendered — Jinja resolves the absent key to
    Undefined, which is falsy, and the {% if %} guarding it simply never fired.

    `argv_json` is selected so a caller can tell WHICH instrument a run was for. Options
    operations are one control per operation with an instrument picker, so without it a
    SENSEX run shows up as NIFTY's last run.
    """
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT id, name, status, started_at, duration_s, argv_json "
                          "FROM ops_jobs ORDER BY id DESC"):
        out.setdefault(r["name"], dict(r))
    return out


def argv_instrument(argv_json: str | None) -> str | None:
    """The --instrument a recorded run was given, if any."""
    if not argv_json:
        return None
    try:
        argv = json.loads(argv_json)
    except ValueError:
        return None
    for i, tok in enumerate(argv):
        if tok == "--instrument" and i + 1 < len(argv):
            return str(argv[i + 1])
    return None


def last_run_per_instrument(conn, names: Sequence[str]) -> dict[tuple[str, str], dict]:
    """Most recent outcome per (operation, instrument), for the options controls."""
    out: dict[tuple[str, str], dict] = {}
    want = set(names)
    for r in conn.execute("SELECT id, name, status, started_at, duration_s, argv_json "
                          "FROM ops_jobs ORDER BY id DESC"):
        if r["name"] not in want:
            continue
        slug = argv_instrument(r["argv_json"])
        if slug:
            out.setdefault((r["name"], slug), dict(r))
    return out


def view(conn) -> dict:
    from .. import config as C
    running = running_job(conn)
    last = last_run(conn)
    return {
        "groups": groups(),
        "operations": [{
            "op": o, "scans": _scan_choices() if any(p.kind == "upload" for p in o.params)
            else (), "last": last.get(o.name), "cli": o.cli()} for o in all_operations()],
        "running": running,
        "history": history(conn),
        "dry_run": C.DRY_RUN,
        "has_scan": bool(_scan_choices()),
    }
