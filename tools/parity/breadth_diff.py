"""Grade the Rust breadth reader (`lanes/swing/breadth.rs`, `PgPipeline`) against Python's
`app.breadth_source.pipeline_breadth` + `breadth_for_plan` on a real Postgres (the test bed).

    python3 tools/parity/breadth_diff.py   # run with the decile venv's python (psycopg)

`breadth_for_plan` has no caller in the Rust desk yet (the weekly lane wires it), so no
desk_diff case reaches it; this runs both readers over the same rows instead. It creates two
small databases on the bed, `rsd_breadth_<pid>` (the four tables the two statements read, a few
rows) and `rsd_breadth_empty_<pid>` (no tables: psycopg's `UndefinedTable`), never touching
the `baskfy` template, under the machine-wide lock, waiting while six non-`baskfy` databases
exist, and drops both on exit (error included). Every case's Python dict is written as JSON
and the Rust side (`cargo test ... pipeline_parity_on_the_bed -- --ignored`) must produce the
same dict, key order and float values included.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psycopg

WT = Path(__file__).resolve().parents[2]
MAIN = Path("/Volumes/T7-Work/Dev/projects/baskfy")
LOCK = MAIN / "rust" / "target" / "nightly_diff.lock"
ADMIN = "postgresql://baskfy:rs-test-only@127.0.0.1:55432/postgres"
BASE = "postgresql://baskfy:rs-test-only@127.0.0.1:55432/"
SLUG = "nifty-total-market"

DDL = [
    "CREATE TABLE index_def (id integer PRIMARY KEY, slug text NOT NULL UNIQUE)",
    "CREATE TABLE instrument (id integer PRIMARY KEY, symbol text NOT NULL)",
    "CREATE TABLE market_health_daily (index_id integer NOT NULL, date date NOT NULL, "
    "pct_above_20dma numeric(7,4), constituent_count integer NOT NULL DEFAULT 0, "
    "PRIMARY KEY (index_id, date))",
    "CREATE TABLE index_member_daily (index_id integer NOT NULL, instrument_id integer NOT NULL, "
    "date date NOT NULL, PRIMARY KEY (index_id, instrument_id, date))",
]

ROWS = [
    "INSERT INTO index_def VALUES (1, 'nifty-total-market'), (2, 'nifty-50')",
    "INSERT INTO instrument VALUES (1, 'A'), (2, 'B'), (3, 'C'), (4, 'D')",
    # 18 Aug: the same population (A, B) at 68.6347; 19 Aug: a NULL figure; 20 Aug: A, B, C
    "INSERT INTO market_health_daily VALUES (1, '2026-08-18', 68.6347, 2), "
    "(1, '2026-08-19', NULL, 2), (1, '2026-08-20', 12.3456, 3), (2, '2026-08-18', 50.0, 1)",
    "INSERT INTO index_member_daily VALUES (1, 1, '2026-08-18'), (1, 2, '2026-08-18'), "
    "(1, 1, '2026-08-19'), (1, 2, '2026-08-19'), (1, 1, '2026-08-20'), (1, 2, '2026-08-20'), "
    "(1, 3, '2026-08-20'), (2, 4, '2026-08-18')",
]

#: (scan_breadth, scan symbols, date, which database)
CASES = [
    (68.6, ["A", "B"], "2026-08-18", "full"),
    (68.6, ["A", "D"], "2026-08-18", "full"),
    (70.0, ["A", "B"], "2026-08-19", "full"),
    (55.5, ["A", "B"], "2026-08-20", "full"),
    (55.5, ["A", "B", "C"], "2026-08-20", "full"),
    (55.5, ["A"], "2020-01-02", "full"),
    (55.5, ["A"], "2026-08-18", "empty"),
]


def count_others(admin) -> int:
    return admin.execute(
        "SELECT count(*) FROM pg_database WHERE datname NOT IN ('postgres', 'baskfy') "
        "AND NOT datistemplate").fetchone()[0]


def main() -> int:
    pid = os.getpid()
    names = {"full": f"rsd_breadth_{pid}", "empty": f"rsd_breadth_empty_{pid}"}
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with psycopg.connect(ADMIN, autocommit=True) as admin:
                while count_others(admin) > 4:
                    time.sleep(5)
                for n in names.values():
                    admin.execute(f'CREATE DATABASE "{n}"')
            try:
                with psycopg.connect(BASE + names["full"], autocommit=True) as conn:
                    for sql in DDL + ROWS:
                        conn.execute(sql)
                return grade(names)
            finally:
                with psycopg.connect(ADMIN, autocommit=True) as admin:
                    for n in names.values():
                        admin.execute(f'DROP DATABASE IF EXISTS "{n}"')
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def grade(names: dict[str, str]) -> int:
    sys.path.insert(0, str(WT / "kite-momentum-rebalancer"))
    os.environ["DESK_UNIVERSE_SLUG"] = SLUG
    from app import breadth_source as B  # noqa: PLC0415

    expected = []
    for scan, symbols, on, db in CASES:
        B.SCREENER_DSN = BASE + names[db]
        result = B.breadth_for_plan(scan, set(symbols), dt.date.fromisoformat(on))
        expected.append({"dsn": BASE + names[db], "scan": scan, "symbols": symbols, "on": on,
                         "python": result})
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(expected, f)
        path = f.name
    env = {**os.environ, "RSD_BREADTH_CASES": path, "CARGO_TARGET_DIR": str(WT / "rust" / "target"),
           "CARGO_BUILD_JOBS": "4"}
    r = subprocess.run(  # noqa: S603
        ["nice", "-n", "10", str(Path.home() / ".cargo/bin/cargo"), "test", "-p", "baskfy-desk",
         "--lib", "--", "--ignored", "--exact",
         "lanes::swing::breadth::tests::pipeline_parity_on_the_bed", "--nocapture"],
        cwd=os.environ.get("RSD_BREADTH_RUST_DIR", str(WT / "rust")), env=env, check=False)
    os.unlink(path)
    for c in expected:
        print(f"  {c['on']} {c['symbols']} -> {c['python']['source']}: {c['python']['reason']}")
    print("breadth_diff:", "EQUAL" if r.returncode == 0 else "DIFFERENT",
          f"({len(expected)} cases)")
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
