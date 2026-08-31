#!/usr/bin/env bash
#
# gates/desk-retire-1.3.3-verify.sh — leaf 1.3.3's INDEPENDENT verification of leaf 1.3.2.
#
# This script shares no code with tools/migrate-desk/. It re-reads the SQLite source and the
# Postgres target itself, re-derives its own per-table digests with its own type-aware
# canonicalisation, and compares. It prints "ALL TABLES MATCH" on the last line ONLY when every
# source table is present in the target with an identical row count AND an identical digest,
# and the source file's sha256 is the one the archive claims.
#
# Exit codes:  0 = everything matched.  1 = at least one mismatch.  2 = could not run.
#
# Overrides (all optional):
#   VERIFY_SQLITE     path to the SQLite source            (default: the sealed 31-Aug archive)
#   VERIFY_SCHEMA     Postgres schema to check             (default: desk_migrated)
#   VERIFY_EXPECT_SHA expected sha256 of VERIFY_SQLITE     (default: store A's, or "skip")
#   VERIFY_PGHOST / VERIFY_PGPORT / VERIFY_PGUSER / VERIFY_PGDB
#   PGPASSWORD        if unset, read from the baskfy-postgres container's env (never printed)
#   VERIFY_PYTHON     interpreter with psycopg 3           (default: the desk venv)
#
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

SRC="${VERIFY_SQLITE:-$HOME/baskfy-safety/desk-migration-2026-08-31/portfolio-A-2026-08-31T1731-IST.db}"
SCHEMA="${VERIFY_SCHEMA:-desk_migrated}"
EXPECT_SHA="${VERIFY_EXPECT_SHA:-22a38d53d3a0ba9011851b6a3bb3a0d54874b099a41f609012c37ce06f283d1b}"
PGHOST="${VERIFY_PGHOST:-localhost}"
PGPORT="${VERIFY_PGPORT:-5433}"
PGUSER="${VERIFY_PGUSER:-baskfy}"
PGDB="${VERIFY_PGDB:-baskfy}"
PY="${VERIFY_PYTHON:-$ROOT/kite-momentum-rebalancer/.venv/bin/python}"
PGCONTAINER="${VERIFY_PGCONTAINER:-baskfy-postgres}"

[ -x "$PY" ] || { echo "FATAL: no interpreter at $PY (set VERIFY_PYTHON)"; exit 2; }
[ -f "$SRC" ] || { echo "FATAL: no SQLite source at $SRC (set VERIFY_SQLITE)"; exit 2; }

# The password is discovered, used, and never echoed. `set -x` is deliberately never enabled.
if [ -z "${PGPASSWORD:-}" ]; then
  PGPASSWORD="$(docker inspect "$PGCONTAINER" \
      --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
      | grep '^POSTGRES_PASSWORD=' | cut -d= -f2-)"
  [ -n "$PGPASSWORD" ] || { echo "FATAL: no PGPASSWORD and none found on $PGCONTAINER"; exit 2; }
fi
export PGPASSWORD

VERIFY_SQLITE="$SRC" VERIFY_SCHEMA="$SCHEMA" VERIFY_EXPECT_SHA="$EXPECT_SHA" \
VERIFY_PGHOST="$PGHOST" VERIFY_PGPORT="$PGPORT" VERIFY_PGUSER="$PGUSER" VERIFY_PGDB="$PGDB" \
"$PY" - <<'PYEOF'
"""Independent re-verification of the desk migration. Shares no code with tools/migrate-desk/."""
from __future__ import annotations

import decimal
import hashlib
import os
import sqlite3
import sys

import psycopg

SRC = os.environ["VERIFY_SQLITE"]
SCHEMA = os.environ["VERIFY_SCHEMA"]
EXPECT_SHA = os.environ["VERIFY_EXPECT_SHA"]

SEP = "\x1e"           # deliberately NOT the migrator's \x1f, so this is not the same digest
NULL = "\x00NULL\x00"

failures: list[str] = []
notes: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"  FAIL  {msg}")


# ---------------------------------------------------------------- 1. the source file itself
h = hashlib.sha256()
nbytes = 0
with open(SRC, "rb") as fh:
    for chunk in iter(lambda: fh.read(1 << 20), b""):
        h.update(chunk)
        nbytes += len(chunk)
src_sha = h.hexdigest()
print(f"source      {SRC}")
print(f"            sha256 {src_sha}  bytes {nbytes}")
if EXPECT_SHA and EXPECT_SHA != "skip" and EXPECT_SHA != src_sha:
    fail(f"source sha256 is {src_sha}, expected {EXPECT_SHA}")

# Read-only, and prove it: query_only makes a write raise rather than rely on discipline.
src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
src.execute("PRAGMA query_only = ON")
qc = src.execute("PRAGMA quick_check").fetchone()[0]
uv = src.execute("PRAGMA user_version").fetchone()[0]
print(f"            quick_check {qc}   user_version {uv}")
if qc != "ok":
    fail(f"source quick_check returned {qc!r}")

src_tables = [
    r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
]
print(f"            {len(src_tables)} tables in sqlite_master")

# ---------------------------------------------------------------- 2. the target
conn = psycopg.connect(
    host=os.environ["VERIFY_PGHOST"], port=int(os.environ["VERIFY_PGPORT"]),
    user=os.environ["VERIFY_PGUSER"], dbname=os.environ["VERIFY_PGDB"],
    password=os.environ["PGPASSWORD"], connect_timeout=10,
)
conn.execute("SET TRANSACTION READ ONLY")          # this script never writes
cur = conn.cursor()
cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (SCHEMA,))
if not cur.fetchone():
    print(f"  FAIL  schema {SCHEMA} does not exist")
    sys.exit(1)

cur.execute(
    "SELECT table_name, column_name, data_type FROM information_schema.columns "
    "WHERE table_schema = %s ORDER BY table_name, ordinal_position",
    (SCHEMA,),
)
pg_cols: dict[str, dict[str, str]] = {}
for t, c, d in cur.fetchall():
    pg_cols.setdefault(str(t), {})[str(c)] = str(d)
print(f"target      {SCHEMA} on {os.environ['VERIFY_PGHOST']}:{os.environ['VERIFY_PGPORT']}"
      f"/{os.environ['VERIFY_PGDB']}  ({len(pg_cols)} tables)")
extra = sorted(set(pg_cols) - set(src_tables))
if extra:
    notes.append(f"target-only tables (not an error, this tool adds them): {', '.join(extra)}")

# ------------------------------------------------- 3. one type-aware rendering for both sides
#
# Type-TAGGED on purpose, and column-class-aware rather than value-aware: an int 1, a float 1.0
# and the text "1" must not collide, but a SQLite int in a NUMERIC-affinity column and the
# Postgres numeric it became must. The class comes from the TARGET catalogue, so both sides are
# rendered by the same rule.
FLOATY = {"numeric", "double precision", "real"}
INTY = {"bigint", "integer", "smallint"}


def render(value: object, cls: str) -> str:
    if value is None:
        return NULL
    if cls == "F":
        if isinstance(value, decimal.Decimal):
            f = float(value)
            # STRICTER than the migrator's canonical(): a numeric carrying precision the source
            # float never had renders differently instead of being flattened onto the float.
            if decimal.Decimal(repr(f)) != value:
                return "D" + str(value.normalize())
            return "F" + repr(f)
        if isinstance(value, (int, float)):
            return "F" + repr(float(value))
        return "F?" + str(value)
    if cls == "I":
        if isinstance(value, bool):
            return "I1" if value else "I0"
        return "I" + str(int(value))
    if cls == "B":
        return "B" + hashlib.sha256(bytes(value)).hexdigest()  # type: ignore[arg-type]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "B" + hashlib.sha256(bytes(value)).hexdigest()
    return "T" + str(value)


def digest(rows, classes: list[str]) -> str:
    per_row = sorted(
        hashlib.sha256(
            SEP.join(render(v, c) for v, c in zip(row, classes)).encode()
        ).hexdigest()
        for row in rows
    )
    return hashlib.sha256("\n".join(per_row).encode()).hexdigest()


# ---------------------------------------------------------------- 4. every table, both assertions
print()
print(f"{'table':<24} {'src':>7} {'tgt':>7}  count  digest")
print("-" * 72)
total_src = total_tgt = 0
matched = 0
per_table_digest: dict[str, str] = {}

for table in src_tables:
    cols = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")').fetchall()]
    if table not in pg_cols:
        fail(f"{table}: present in the source, MISSING from {SCHEMA}")
        continue
    missing = [c for c in cols if c not in pg_cols[table]]
    if missing:
        fail(f"{table}: columns missing from the target: {missing}")
        continue
    classes = []
    for c in cols:
        d = pg_cols[table][c]
        classes.append("F" if d in FLOATY else "I" if d in INTY else "B" if d == "bytea" else "T")

    q = ", ".join(f'"{c}"' for c in cols)
    s_rows = src.execute(f'SELECT {q} FROM "{table}"').fetchall()
    cur.execute(f'SELECT {q} FROM "{SCHEMA}"."{table}"')
    t_rows = cur.fetchall()

    s_n, t_n = len(s_rows), len(t_rows)
    s_d, t_d = digest(s_rows, classes), digest(t_rows, classes)
    per_table_digest[table] = t_d
    total_src += s_n
    total_tgt += t_n
    cnt_ok = s_n == t_n
    dig_ok = s_d == t_d
    flag = "" if s_n else "   (empty: this assertion is vacuous)"
    print(f"{table:<24} {s_n:>7} {t_n:>7}  {'OK ' if cnt_ok else 'BAD'}    "
          f"{'OK ' if dig_ok else 'BAD'}  {t_d[:16]}…{flag}")
    if not cnt_ok:
        fail(f"{table}: row_count {s_n} -> {t_n}")
    if not dig_ok:
        fail(f"{table}: digest {s_d[:16]}… -> {t_d[:16]}…")
    if cnt_ok and dig_ok:
        matched += 1

# ---------------------------------------------------------------- 5. an independent schema digest
schema_digest = hashlib.sha256(
    "\n".join(f"{t}={d}" for t, d in sorted(per_table_digest.items())).encode()
).hexdigest()

print("-" * 72)
print(f"{'TOTAL':<24} {total_src:>7} {total_tgt:>7}")
print()
print(f"independent schema digest (1.3.3's own, NOT the migrator's): {schema_digest}")
for n in notes:
    print(f"note: {n}")
print()

if failures:
    print(f"MISMATCHES: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    print(f"VERIFICATION FAILED — {matched}/{len(src_tables)} tables matched, "
          f"{total_src} source rows vs {total_tgt} target rows")
    sys.exit(1)

print(f"{len(src_tables)} tables checked, {total_src} source rows, {total_tgt} target rows, "
      f"{matched} matched on BOTH row_count and digest")
print("ALL TABLES MATCH")
sys.exit(0)
PYEOF
