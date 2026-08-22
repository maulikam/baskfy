"""One-way SQLite -> PostgreSQL migration for the desk's database (M18, P3.9).

`data/portfolio.db` holds the only irreplaceable thing in this repository: 8,198 trades, 9,262
fills, 133 rebalance orders and every regime decision this book has been run on. Zerodha flushes
`/trades` nightly and `kc.margins()` has no history, so a lost row is not a restore away -- it is
gone. This script therefore asserts far more than "it ran".

WHAT IT ASSERTS
---------------
1. **Row counts** match, per table.
2. **Checksums** match, per table -- a SHA-256 over every row, with values normalised by ONE
   function that both sides call, so a difference in how a float or a NULL is rendered cannot be
   mistaken for a difference in the data.
3. **The NAV series recomputes.** With no cashflows the desk's index is `100 * nav / nav[0]`, so
   the stored `index_value` is re-derived from the migrated `nav` column and compared to what
   SQLite holds. A faithful byte copy that broke the arithmetic would pass 1 and 2 and fail this.

RE-RUNNABLE, AND IT IS TRUNCATE-AND-RELOAD, NOT UPSERT
------------------------------------------------------
The SQLite file is the source of truth and the `desk` schema is a **projection** of it, so a
re-run replaces the projection wholesale: `--drop-existing` drops the schema and rebuilds it
inside one transaction. There is no merge, no `ON CONFLICT`, and no attempt to reconcile rows
changed on both sides -- because until M19 flips the desk's backend, only SQLite is ever written
to, so "both sides" cannot happen. An upsert here would be machinery for a situation that does not
exist, and it would quietly paper over the one that does: a row deleted in SQLite would survive in
Postgres forever.

Without `--drop-existing`, a populated schema is **refused** with an actionable message rather than
a `DuplicateTable` traceback half-way through. Nothing is ever partially applied: every table is
created, filled and checked inside a single transaction that commits only if every assertion
passed.

WHERE IT PUTS THINGS
--------------------
A dedicated **`desk` schema**, not `public`. The desk's `trades`, `settings` and `fills` do not
collide with the screener's 42 tables today, and relying on that is how a collision happens later
-- `docs/04` §4 renames these at P4 anyway. `desk.trades` also says where a row came from, and
rollback is `DROP SCHEMA desk CASCADE`.

    python -m scripts.migrate_to_postgres --sqlite data/portfolio.db \
        --postgres postgresql://baskfy:baskfy@localhost:5433/baskfy [--drop-existing]

Exit 0 only if every assertion passed. The report goes to stdout as JSON.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
import sys
from typing import Any

import psycopg

#: SQLite's declared types are advisory; these are the ones this database actually uses.
_TYPE_MAP = {
    "INTEGER": "BIGINT",
    "INT": "BIGINT",
    "REAL": "DOUBLE PRECISION",
    "FLOAT": "DOUBLE PRECISION",
    "DOUBLE": "DOUBLE PRECISION",
    "NUMERIC": "DOUBLE PRECISION",
    "TEXT": "TEXT",
    "BLOB": "BYTEA",
    "": "TEXT",
}


def _pg_type(declared: str) -> str:
    return _TYPE_MAP.get(declared.strip().upper().split("(")[0], "TEXT")


def canonical(value: Any) -> str:
    """ONE rendering of a value, called for both databases.

    The whole point of a checksum here is to compare *data*, so the normalisation cannot live
    on one side only: a float formatted differently by two drivers would read as corruption.
    """
    if value is None:
        return "\\N"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        # repr() round-trips a float exactly and renders it identically on both sides.
        return repr(float(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return hashlib.sha256(bytes(value)).hexdigest()
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def checksum(rows: list[tuple[Any, ...]]) -> str:
    """Order-independent digest: each row hashed, the hashes sorted, then hashed again.

    Sorting the digests rather than the rows means the comparison does not depend on either
    database's idea of row order, which is not guaranteed and is not part of the data.
    """
    per_row = sorted(
        hashlib.sha256("\x1f".join(canonical(v) for v in row).encode()).hexdigest()
        for row in rows
    )
    return hashlib.sha256("\n".join(per_row).encode()).hexdigest()


def sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def columns_of(conn: sqlite3.Connection, table: str) -> list[tuple[str, str, int]]:
    return [(r[1], r[2], r[3]) for r in conn.execute(f'PRAGMA table_info("{table}")')]


def migrate(sqlite_path: str, postgres_url: str, schema: str, drop_existing: bool) -> dict:
    report: dict[str, Any] = {
        "sqlite": sqlite_path, "schema": schema,
        "at": dt.datetime.now(dt.UTC).isoformat(), "tables": {}, "failures": [],
    }
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        tables = sqlite_tables(src)
        with psycopg.connect(postgres_url, autocommit=False) as dst:
            with dst.cursor() as cur:
                existing = cur.execute(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
                    (schema,),
                ).fetchone()
                populated = bool(existing and existing[0])
                if populated and not drop_existing:
                    raise SystemExit(
                        f'schema "{schema}" already holds {existing[0]} tables.\n'
                        f"This migration is truncate-and-reload, not an upsert: the SQLite file is\n"
                        f"the source of truth and this schema is a projection of it. Re-run with\n"
                        f"--drop-existing to rebuild it, or point --schema somewhere else.\n"
                        f"Nothing has been changed."
                    )
                if drop_existing:
                    cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                report["mode"] = "truncate-and-reload" if populated else "initial load"

                for table in tables:
                    cols = columns_of(src, table)
                    ddl_cols = ", ".join(
                        f'"{name}" {_pg_type(decl)}{" NOT NULL" if notnull else ""}'
                        for name, decl, notnull in cols
                    )
                    cur.execute(f'CREATE TABLE "{schema}"."{table}" ({ddl_cols})')

                    names = [c[0] for c in cols]
                    rows = src.execute(
                        f'SELECT {", ".join(chr(34) + n + chr(34) for n in names)} FROM "{table}"'
                    ).fetchall()
                    if rows:
                        placeholders = ", ".join(["%s"] * len(names))
                        collist = ", ".join(f'"{n}"' for n in names)
                        cur.executemany(
                            f'INSERT INTO "{schema}"."{table}" ({collist}) VALUES ({placeholders})',
                            rows,
                        )

                    got = cur.execute(
                        f'SELECT {", ".join(chr(34) + n + chr(34) for n in names)} '
                        f'FROM "{schema}"."{table}"'
                    ).fetchall()

                    src_sum, dst_sum = checksum(rows), checksum([tuple(r) for r in got])
                    entry = {
                        "rows_sqlite": len(rows), "rows_postgres": len(got),
                        "checksum_sqlite": src_sum, "checksum_postgres": dst_sum,
                        "rows_match": len(rows) == len(got), "checksum_match": src_sum == dst_sum,
                    }
                    report["tables"][table] = entry
                    if not entry["rows_match"]:
                        report["failures"].append(f"{table}: {len(rows)} rows -> {len(got)}")
                    if not entry["checksum_match"]:
                        report["failures"].append(f"{table}: checksum differs")

                report["nav"] = _check_nav(src, cur, schema)
                if not report["nav"]["identical"]:
                    report["failures"].append("the NAV series does not match")
                if not report["nav"]["index_recomputes"]:
                    report["failures"].append("index_value does not recompute from nav")

                if report["failures"]:
                    dst.rollback()
                    report["committed"] = False
                else:
                    dst.commit()
                    report["committed"] = True
    finally:
        src.close()
    report["ok"] = not report["failures"]
    return report


def _check_nav(src: sqlite3.Connection, cur: psycopg.Cursor, schema: str) -> dict:
    """The semantic check: the series must survive, and its arithmetic must still hold."""
    a = src.execute("SELECT date, nav, index_value FROM snapshots ORDER BY date").fetchall()
    b = cur.execute(
        f'SELECT date, nav, index_value FROM "{schema}".snapshots ORDER BY date'
    ).fetchall()
    identical = [(str(d), canonical(n), canonical(i)) for d, n, i in a] == [
        (str(d), canonical(n), canonical(i)) for d, n, i in b
    ]

    # With no cashflows the desk's index is nav rebased to 100 (app/config.py INDEX_BASE).
    cashflows = src.execute("SELECT COUNT(*) FROM cashflows").fetchone()[0]
    recomputes = None
    if not cashflows and b:
        base = float(b[0][1])
        recomputes = all(
            abs(100.0 * float(nav) / base - float(idx)) < 1e-9 for _, nav, idx in b
        )
    return {
        "rows": len(a), "identical": identical, "cashflows": cashflows,
        "index_recomputes": True if recomputes is None else recomputes,
        "note": ("index_value not re-derived: cashflows exist, so the series is not a simple "
                 "rebase" if cashflows else "index_value re-derived as 100 * nav / nav[0]"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", default="data/portfolio.db")
    ap.add_argument("--postgres", required=True)
    ap.add_argument("--schema", default="desk")
    ap.add_argument("--drop-existing", action="store_true")
    args = ap.parse_args()

    report = migrate(args.sqlite, args.postgres, args.schema, args.drop_existing)
    print(json.dumps(report, indent=2))
    if report["ok"]:
        print(f"\nOK: {len(report['tables'])} tables migrated into "
              f'"{args.schema}", every count and checksum matched.', file=sys.stderr)
    else:
        print(f"\nFAILED, nothing committed:\n  " + "\n  ".join(report["failures"]),
              file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
