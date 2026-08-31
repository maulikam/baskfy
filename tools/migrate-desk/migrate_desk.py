#!/usr/bin/env python
"""Migrate the desk's SQLite record into Baskfy's Postgres — D8, with the assertions D8 asks for.

    D8  "The desk's SQLite **migrates** with row-count + checksum assertions; the file is
         archived forever."  — root CLAUDE.md

WHY THIS EXISTS RATHER THAN `scripts/migrate_to_postgres.py`
------------------------------------------------------------
That script's checksum machinery is good and is kept here almost verbatim. Its schema generation
is not, and leaf 1.3.1 measured the consequences in the laptop's `desk` schema, which is that
script's actual output:

    select count(*) from pg_indexes where schemaname='desk';   ->  0

Zero indexes, zero primary keys, zero unique constraints, zero foreign keys — because it builds
`CREATE TABLE` from `PRAGMA table_info` and never reads `sqlite_master`'s index DDL. Live effects,
all four present in that schema today:

  1. 67 rows of `desk.rebalance_orders` have `id IS NULL`. Every row the desk has written since
     the M19 cutover. The primary key of an evidence table, discarded for nine days, unnoticed.
  2. `pg.py:_DDL` rewrites `INSERT OR IGNORE` / `INSERT OR REPLACE` to a plain `INSERT`. With the
     uniques gone both become unconditional inserts, so house rule 7 — "re-running any day's job
     produces identical rows" — does not hold on that backend.
  3. `ux_regime_canonical`, the partial unique index that enforces "committed exactly once", is
     not enforced.
  4. `_TYPE_MAP:58` maps `REAL` *and* `NUMERIC` to `double precision`, so money lands as float,
     against house rule 9. See desk_pgddl.py for how this tool settles that against M19.3.
  5. `sqlite_tables()` filters `sqlite_%`, so the eight AUTOINCREMENT high-water marks in
     `sqlite_sequence` are not carried — and `max(id)+1` will not substitute, because
     `regime_evaluations.seq` is 32 against 26 rows.

Each of those is a named, failing assertion here.

THE THREE SUBCOMMANDS
---------------------
    rehearse   Take a consistent copy of the source through SQLite's backup API, migrate the copy
               into a scratch schema, run every assertion, drop the scratch schema, and write a
               receipt keyed by the source's sha256. Nothing outside the scratch schema is
               touched.
    apply      The real run. Refuses without a verified backup, a dated archive outside the repo,
               and (with --require-rehearsal) a receipt for this exact source. Rolls back unless
               --commit is given AND every assertion passed.
    verify     Read-only. Re-assert an already-migrated schema against the source. This is how you
               prove, later and cheaply, that the copy still matches — and how house rule 7 is
               demonstrated: apply twice, verify, compare schema digests.

    python tools/migrate-desk/migrate_desk.py rehearse \\
        --sqlite ~/baskfy-safety/…/portfolio-A-….db \\
        --postgres postgresql://baskfy:…@localhost:5433/baskfy \\
        --fork-policy box-only-quarantine \\
        --secondary-sqlite B=~/baskfy-safety/sqlite-archive/portfolio-…-FINAL-pre-postgres.db \\
        --secondary-postgres C=postgresql://…/baskfy:desk

`--fork-policy` has no default and a run without it is refused. See desk_fork.py: there are three
forked copies of this database, and 7 plans plus their orders exist outside the box. Whether they
are preserved or discarded is Maulik's call, not a side effect of running a script.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import pathlib
import shutil
import sqlite3
import sys
import tempfile
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import desk_fork as fork
import desk_pgddl as pgddl
import desk_preflight as preflight
from desk_assertions import (
    AssertionLedger,
    MigrationFailure,
    canonical,
    row_count,
    schema_checksum,
    verdict_for,
)
from desk_introspect import Schema, Table, open_readonly, read_rows, read_schema

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_RECEIPTS = pathlib.Path.home() / "baskfy-safety" / "migration-receipts"

#: How far `100 * nav / nav[0]` may drift from the stored `index_value` before the
#: series is judged not to recompute. Float64 rounding over 15 chained NAVs, no more.
INDEX_TOLERANCE = 1e-9


# --------------------------------------------------------------------------- copying the source


def consistent_copy(
    source: str, destination: pathlib.Path, immutable: bool = False
) -> pathlib.Path:
    """A snapshot through SQLite's backup API, safe to take while the desk is writing.

    `scripts/backup.py` says it best: *"A LIVE SQLITE FILE MUST NOT BE COPIED. A plain cp can
    capture a write in progress and produce an archive that opens fine and is missing the last
    transaction — the worst kind of backup, because it looks like one."*

    `immutable=True` reads without a lock and without creating the `-shm` sidecar next to the
    source. Use it when the source is a sealed archive you have promised not to leave a mark on;
    `mode=ro` otherwise, which is the safe choice when something may be writing.
    """
    uri = f"file:{source}?immutable=1" if immutable else f"file:{source}?mode=ro"
    with sqlite3.connect(uri, uri=True) as src, sqlite3.connect(str(destination)) as dst:
        src.backup(dst)
    return destination


# --------------------------------------------------------------------------- the migration


def _load_table(
    cur: Any,  # noqa: ANN401 — a psycopg cursor; naming it would import psycopg at module scope
    schema: str,
    table: Table,
    rows: list[tuple[Any, ...]],
    float_money: bool,
) -> None:
    if not rows:
        return
    numeric_flags = [
        pgddl.lands_as_numeric(table.name, c, float_money) for c in table.columns
    ]
    converted = [
        tuple(pgddl.to_pg_value(v, n) for v, n in zip(row, numeric_flags, strict=True))
        for row in rows
    ]
    cols = ", ".join(pgddl.quote(c) for c in table.column_names)
    marks = ", ".join(["%s"] * len(table.column_names))
    cur.executemany(
        f"INSERT INTO {pgddl.qualified(schema, table.name)} ({cols}) VALUES ({marks})",
        converted,
    )


def _read_back(
    cur: Any,  # noqa: ANN401 — a psycopg cursor
    schema: str,
    table: Table,
) -> list[tuple[Any, ...]]:
    cols = ", ".join(pgddl.quote(c) for c in table.column_names)
    cur.execute(f"SELECT {cols} FROM {pgddl.qualified(schema, table.name)}")
    return [tuple(r) for r in cur.fetchall()]


def _create_quarantine(
    cur: Any,  # noqa: ANN401 — a psycopg cursor
    schema: str,
    tables: list[Table],
    float_money: bool,
) -> str:
    quarantine = f"{schema}_quarantine"
    cur.execute(f"DROP SCHEMA IF EXISTS {pgddl.quote(quarantine)} CASCADE")
    cur.execute(f"CREATE SCHEMA {pgddl.quote(quarantine)}")
    for table in tables:
        # No PK, no identity, no FK: quarantine holds rows precisely because their keys are
        # untrustworthy. `_orphan_source` says which forked store each came from.
        cols = ",\n".join(
            f"  {pgddl.quote(c.name)} {pgddl.pg_type(table.name, c, float_money)}"
            for c in table.columns
        )
        cur.execute(
            f"CREATE TABLE {pgddl.qualified(quarantine, table.name)} (\n{cols},\n"
            "  _orphan_source text NOT NULL\n)"
        )
    return quarantine


def _semantic_checks(  # noqa: PLR0913, PLR0917, PLR0915 — a linear list of independent checks.
    #  Splitting it would scatter them across call sites and make "did this one run?" a question
    #  you answer by reading, instead of by looking at one numbered list.
    ledger: AssertionLedger,
    src: sqlite3.Connection,
    cur: Any,  # noqa: ANN401 — a psycopg cursor
    schema: str,
    schema_obj: Schema,
    float_money: bool,
) -> dict[str, Any]:
    """The checks a byte-faithful copy can still fail. Every one of them fails the run."""
    out: dict[str, Any] = {}

    # --- 1. The NAV series survives, and its arithmetic still holds. -----------------------
    #     Kept from scripts/migrate_to_postgres.py:219 — a copy that broke the arithmetic would
    #     pass every row_count and checksum and still be wrong.
    a = src.execute("SELECT date, nav, index_value FROM snapshots ORDER BY date").fetchall()
    cur.execute(
        f"SELECT date, nav, index_value FROM {pgddl.qualified(schema, 'snapshots')} ORDER BY date"
    )
    b = cur.fetchall()
    identical = [(str(d), canonical(n), canonical(i)) for d, n, i in a] == [
        (str(d), canonical(n), canonical(i)) for d, n, i in b
    ]
    ledger.add_semantic("nav_series_identical", identical, f"{len(a)} snapshot rows")
    cashflows = int(src.execute("SELECT COUNT(*) FROM cashflows").fetchone()[0])
    if not cashflows and b:
        base = float(b[0][1])
        recomputes = all(
            abs(100.0 * float(nav) / base - float(idx)) < INDEX_TOLERANCE for _d, nav, idx in b
        )
        detail = "index_value re-derived as 100 * nav / nav[0]"
    else:
        recomputes, detail = True, (
            "not re-derived: cashflows exist, so the index is not a simple rebase"
            if cashflows else "no snapshots"
        )
    ledger.add_semantic("index_value_recomputes", recomputes, detail)
    out["nav"] = {"rows": len(a), "identical": identical, "cashflows": cashflows,
                  "index_recomputes": recomputes, "note": detail}

    # --- 2. The keys exist. This is the direct anti-regression for §5.5. -------------------
    cur.execute(
        "SELECT contype, count(*) FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE n.nspname = %s GROUP BY contype",
        (schema,),
    )
    got = {str(k): int(v) for k, v in cur.fetchall()}
    cur.execute("SELECT count(*) FROM pg_indexes WHERE schemaname = %s", (schema,))
    index_count = int(cur.fetchone()[0])

    # +1 for `migration_provenance`, which this tool adds and which has its own primary key.
    want_pk = sum(1 for t in schema_obj.tables if t.pk_columns) + 1
    want_unique = sum(len(t.unique_constraints) for t in schema_obj.tables)
    want_fk = sum(len(t.foreign_keys) for t in schema_obj.tables)
    want_std = sum(len(t.standalone_indexes) for t in schema_obj.tables)

    ledger.add_semantic(
        "primary_keys_present", got.get("p", 0) == want_pk,
        f"{got.get('p', 0)} primary keys in schema {schema}, expected {want_pk}",
    )
    ledger.add_semantic(
        "unique_constraints_present", got.get("u", 0) == want_unique,
        f"{got.get('u', 0)} unique constraints, expected {want_unique}",
    )
    ledger.add_semantic(
        "foreign_keys_present", got.get("f", 0) == want_fk,
        f"{got.get('f', 0)} foreign keys, expected {want_fk}: "
        + "; ".join(
            f"{t.name}.{'+'.join(fk.columns)} -> {fk.ref_table}"
            for t in schema_obj.tables for fk in t.foreign_keys
        )
        + " (store C, migrated by the old script, has none of them)",
    )
    ledger.add_semantic(
        "indexes_present", index_count >= want_std,
        f"{index_count} indexes in pg_indexes (store C, migrated by the old script, has 0); "
        f"expected at least {want_std} standalone indexes plus the constraint-backing ones",
    )
    out["keys"] = {"primary_keys": got.get("p", 0), "unique_constraints": got.get("u", 0),
                   "foreign_keys": got.get("f", 0), "check_constraints": got.get("c", 0),
                   "pg_indexes": index_count,
                   "expected": {"primary_keys": want_pk, "unique_constraints": want_unique,
                                "foreign_keys": want_fk, "standalone_indexes": want_std}}

    # --- 3. The partial unique index specifically. ------------------------------------------
    cur.execute(
        "SELECT count(*) FROM pg_indexes WHERE schemaname = %s AND indexname = %s",
        (schema, "ux_regime_canonical"),
    )
    canonical_ix = int(cur.fetchone()[0])
    ledger.add_semantic(
        "ux_regime_canonical_present", canonical_ix == 1,
        "the partial unique index enforcing 'a regime evaluation is committed exactly once'",
    )

    # --- 4. No NULL in any identity column. The store-C defect, checked for by name. --------
    null_ids: dict[str, int] = {}
    for table in schema_obj.tables:
        column = table.identity_column
        if column is None:
            continue
        cur.execute(
            f"SELECT count(*) FROM {pgddl.qualified(schema, table.name)} "
            f"WHERE {pgddl.quote(column)} IS NULL"
        )
        null_ids[table.name] = int(cur.fetchone()[0])
    ledger.add_semantic(
        "no_null_identity_values", not any(null_ids.values()),
        f"NULL identity values per table: {null_ids} "
        "(store C has 67 in rebalance_orders right now)",
    )
    out["null_identity_values"] = null_ids

    # --- 5. Every sequence is above every id it must never re-issue. ------------------------
    sequences: dict[str, Any] = {}
    seq_ok = True
    for table in schema_obj.tables:
        column = table.identity_column
        if column is None:
            continue
        cur.execute("SELECT pg_get_serial_sequence(%s, %s)",
                    (f"{schema}.{table.name}", column))
        seq_name = cur.fetchone()[0]
        # Read the sequence relation itself: pg_sequences exposes last_value but not is_called,
        # and without is_called you cannot tell "next is 480" from "next is 479".
        cur.execute(f"SELECT last_value, is_called FROM {seq_name}")
        row = cur.fetchone()
        cur.execute(
            f"SELECT coalesce(max({pgddl.quote(column)}), 0) "
            f"FROM {pgddl.qualified(schema, table.name)}"
        )
        max_id = int(cur.fetchone()[0])
        last_value = int(row[0]) if row else 0
        is_called = bool(row[1]) if row else False
        next_value = last_value + 1 if is_called else last_value
        good = next_value > max_id and (table.sequence is None or next_value > table.sequence)
        seq_ok = seq_ok and good
        sequences[table.name] = {
            "sqlite_sequence": table.sequence, "max_id_migrated": max_id,
            "postgres_next_value": next_value, "ok": good,
        }
    ledger.add_semantic(
        "sequences_carried", seq_ok,
        "each identity sequence starts above both sqlite_sequence.seq and max(id); "
        "regime_evaluations is the case that proves max(id)+1 is not a substitute "
        "(seq 32 against 26 rows)",
    )
    out["sequences"] = sequences

    # --- 6. House rule 9: money and prices are numeric. Gate G5, checked in the catalogue. ---
    cur.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s ORDER BY table_name, ordinal_position",
        (schema,),
    )
    catalogue = {(str(t), str(c)): str(d) for t, c, d in cur.fetchall()}
    wrong: list[str] = []
    numeric_landed: list[str] = []
    float_landed: list[str] = []
    for table in schema_obj.tables:
        for column in table.columns:
            if column.decl_type not in ("REAL", "NUMERIC"):
                continue
            actual = catalogue.get((table.name, column.name), "<missing>")
            expected = pgddl.pg_type(table.name, column, float_money)
            if actual != expected:
                wrong.append(f"{table.name}.{column.name}: {actual} (expected {expected})")
            (numeric_landed if actual == "numeric" else float_landed).append(
                f"{table.name}.{column.name}"
            )
    ledger.add_semantic(
        "money_is_numeric_not_float",
        not wrong and (float_money or bool(numeric_landed)),
        f"{len(numeric_landed)} REAL columns landed as numeric, {len(float_landed)} as "
        f"double precision by design; mismatches: {wrong or 'none'}",
    )
    out["column_types"] = {
        "numeric": sorted(numeric_landed),
        "double_precision_by_design": sorted(float_landed),
        "mismatches": wrong,
        "float_money_flag": float_money,
    }

    # --- 7. Referential integrity of the one FK, stated as a number. ------------------------
    cur.execute(
        f"SELECT count(*) FROM {pgddl.qualified(schema, 'rebalance_orders')} o "
        f"LEFT JOIN {pgddl.qualified(schema, 'rebalance_versions')} v "
        "  ON v.version_id = o.version_id WHERE v.version_id IS NULL"
    )
    dangling = int(cur.fetchone()[0])
    ledger.add_semantic(
        "no_dangling_rebalance_orders", dangling == 0,
        f"{dangling} orders point at a plan that is not in the target",
    )

    # --- 8. `fills.trade_id` is the stable anchor; it must not have collapsed. --------------
    src_fills = int(src.execute("SELECT count(DISTINCT trade_id) FROM fills").fetchone()[0])
    cur.execute(f"SELECT count(DISTINCT trade_id) FROM {pgddl.qualified(schema, 'fills')}")
    dst_fills = int(cur.fetchone()[0])
    ledger.add_semantic(
        "fills_anchor_intact", src_fills == dst_fills,
        f"distinct broker trade_id: {src_fills} -> {dst_fills} "
        "(the root record; Zerodha flushes /trades nightly and cannot re-cut it)",
    )
    out["fills_distinct_trade_id"] = {"source": src_fills, "target": dst_fills}
    return out


def migrate(  # noqa: PLR0913, PLR0912, PLR0915 — one transaction, start to finish. Every
    #  statement here has to be inside the same transaction as every assertion, so extracting
    #  halves of it would mean passing the open cursor around and losing that guarantee.
    *,
    sqlite_path: str,
    postgres_url: str,
    schema: str,
    fork_policy: str,
    drop_existing: bool,
    commit: bool,
    float_money: bool = False,
    immutable_source: bool = False,
    secondaries: list[fork.SecondaryStore] | None = None,
    label: str = "apply",
) -> dict[str, Any]:
    """One transaction. Every assertion inside it. Commit only if all of them held."""
    import psycopg  # noqa: PLC0415 — imported here so --help works without the driver

    if fork_policy not in fork.FORK_POLICIES:
        raise MigrationFailure(
            f"unknown fork policy {fork_policy!r}; choose one of {list(fork.FORK_POLICIES)}"
        )
    secondaries = secondaries or []
    before = preflight.fingerprint(sqlite_path)

    report: dict[str, Any] = {
        "tool": "tools/migrate-desk/migrate_desk.py",
        "run": label,
        "at": dt.datetime.now(dt.UTC).isoformat(),
        "source": before.as_dict(),
        "schema": schema,
        "fork_policy": fork_policy,
        "money_type": "double precision (--float-money)" if float_money else "numeric",
        "committed": False,
    }

    src = open_readonly(sqlite_path, immutable=immutable_source)
    ledger = AssertionLedger()
    try:
        schema_obj = read_schema(src)
        by_name = {t.name: t for t in schema_obj.tables}
        report["user_version"] = schema_obj.user_version
        report["tables_found"] = list(schema_obj.table_names)

        orphans = fork.discover(src, by_name, secondaries)
        report["fork"] = orphans.as_dict()
        report["fork"]["policy"] = fork_policy
        report["fork"]["effect"] = {
            fork.BOX_ONLY: "orphan rows are NOT migrated and NOT copied anywhere by this run; "
                           "they remain only in the forked stores",
            fork.QUARANTINE: f"orphan rows land in schema {schema}_quarantine, tagged with the "
                             "store they came from; the live schema is the box and only the box",
            fork.MERGE: "orphan rows are merged into the live tables, with rebalance_orders.id "
                        "reissued above the box's high-water mark",
        }[fork_policy]
        if orphans.tripwires:
            raise MigrationFailure(
                "a forked store holds `fills` the box does not:\n  "
                + json.dumps(orphans.tripwires, indent=2)
                + "\nThat is a finding to investigate, not a row to merge. Migration refused."
            )

        with psycopg.connect(postgres_url, autocommit=False) as dst, dst.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
                (schema,),
            )
            existing = int(cur.fetchone()[0])
            if existing and not drop_existing:
                raise MigrationFailure(
                    f'schema "{schema}" already holds {existing} tables.\n'
                    "This migration is a reload, not an upsert: the SQLite file is the record "
                    "and this schema is a projection of it, so a re-run replaces the "
                    "projection wholesale (that is what makes it idempotent — house rule 7).\n"
                    "Re-run with --drop-existing, or point --schema somewhere else.\n"
                    "Nothing has been changed."
                )
            report["mode"] = "reload" if existing else "initial load"
            cur.execute(f"DROP SCHEMA IF EXISTS {pgddl.quote(schema)} CASCADE")
            cur.execute(f"CREATE SCHEMA {pgddl.quote(schema)}")

            # ---- schema: tables, then data, then indexes, then FKs -------------------
            for table in schema_obj.tables:
                cur.execute(pgddl.create_table(schema, table, float_money))
            for statement in pgddl.create_provenance(schema):
                cur.execute(statement)

            extra_orders_max = 0
            remap: list[dict[str, Any]] = []
            for table in schema_obj.tables:
                rows = read_rows(src, table)
                expected = list(rows)
                note = ""
                if fork_policy == fork.MERGE and table.name == "rebalance_versions":
                    extra = [r for _s, r in orphans.plans]
                    expected += extra
                    note = f"+{len(extra)} orphan plans merged from {orphans.sources}"
                if fork_policy == fork.MERGE and table.name == "rebalance_orders":
                    high = max(
                        [table.sequence or 0]
                        + [int(r[table.column_names.index("id")]) for r in rows if r]
                    )
                    remapped, remap, extra_orders_max = fork.remap_order_ids(
                        orphans.orders, table.column_names, high
                    )
                    extra = [r for _s, r in remapped]
                    expected += extra
                    note = (
                        f"+{len(extra)} orphan orders merged, ids reissued "
                        f"{high + 1}..{extra_orders_max} above the box's high-water mark"
                    )
                _load_table(cur, schema, table, expected, float_money)
                got = _read_back(cur, schema, table)
                ledger.add(
                    verdict_for(
                        table.name, expected, got, table.column_names,
                        note or f"{row_count(rows)} rows from the box",
                    )
                )

            for table in schema_obj.tables:
                for index in table.standalone_indexes:
                    cur.execute(pgddl.create_index(schema, table, index))
            for table in schema_obj.tables:
                for statement in pgddl.add_foreign_keys(schema, table):
                    cur.execute(statement)

            # ---- AUTOINCREMENT high-water marks --------------------------------------
            carried: dict[str, str] = {}
            for table in schema_obj.tables:
                plan = pgddl.setval(schema, table)
                if plan is None:
                    continue
                sql, params, provenance = plan
                if (fork_policy == fork.MERGE and table.name == "rebalance_orders"
                        and extra_orders_max):
                    sql = "SELECT setval(pg_get_serial_sequence(%s, %s), %s, true)"
                    params = (f"{schema}.{table.name}", table.identity_column,
                              extra_orders_max)
                    provenance = (
                        f"raised to {extra_orders_max} to clear the reissued orphan ids"
                    )
                cur.execute(sql, params)
                carried[table.name] = provenance
            report["sequences_carried"] = carried

            # ---- quarantine ----------------------------------------------------------
            if fork_policy == fork.QUARANTINE and not orphans.empty:
                q_tables = [by_name["rebalance_versions"], by_name["rebalance_orders"]]
                quarantine = _create_quarantine(cur, schema, q_tables, float_money)
                for table, payload in (
                    (q_tables[0], orphans.plans),
                    (q_tables[1], orphans.orders),
                ):
                    if not payload:
                        continue
                    flags = [
                        pgddl.lands_as_numeric(table.name, c, float_money)
                        for c in table.columns
                    ]
                    rows = [
                        (
                            *(
                                pgddl.to_pg_value(v, n)
                                for v, n in zip(row, flags, strict=True)
                            ),
                            source,
                        )
                        for source, row in payload
                    ]
                    cols = ", ".join(
                        [pgddl.quote(c) for c in table.column_names] + ['"_orphan_source"']
                    )
                    marks = ", ".join(["%s"] * (len(table.column_names) + 1))
                    cur.executemany(
                        f"INSERT INTO {pgddl.qualified(quarantine, table.name)} ({cols}) "
                        f"VALUES ({marks})",
                        rows,
                    )
                    cur.execute(
                        f"SELECT {', '.join(pgddl.quote(c) for c in table.column_names)}, "
                        f'"_orphan_source" FROM {pgddl.qualified(quarantine, table.name)}'
                    )
                    back = [tuple(r) for r in cur.fetchall()]
                    want = [
                        (*row, source) for source, row in payload
                    ]
                    ledger.add(
                        verdict_for(
                            f"{quarantine}.{table.name}", want, back,
                            (*table.column_names, "_orphan_source"),
                            "orphan rows preserved out of the live schema",
                        )
                    )
                report["quarantine_schema"] = quarantine

            # ---- schema version + semantic checks ------------------------------------
            cur.execute(
                f"INSERT INTO {pgddl.qualified(schema, 'schema_version')} VALUES (%s)",
                (schema_obj.user_version,),
            )
            report["semantic"] = _semantic_checks(
                ledger, src, cur, schema, schema_obj, float_money
            )
            report["fork"]["id_remap"] = remap
            report["assertions"] = ledger.as_dict()
            report["schema_digest"] = schema_checksum(ledger.verdicts)
            report["type_summary"] = {
                k: len(v) for k, v in pgddl.summarise_types(schema_obj, float_money).items()
            }

            after = preflight.assert_source_unmoved(before, sqlite_path)
            report["source_after"] = after.as_dict()
            report["source_unchanged"] = True

            try:
                ledger.assert_all()
            except MigrationFailure:
                dst.rollback()
                report["assertions"] = ledger.as_dict()
                report["ok"] = False
                report["committed"] = False
                return report

            cur.execute(
                f"INSERT INTO {pgddl.qualified(schema, 'migration_provenance')} "
                "(migrated_at, source_path, source_sha256, source_bytes, user_version, "
                " fork_policy, money_type, row_total, schema_digest, report_json) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (report["at"], before.path, before.sha256, before.bytes,
                 schema_obj.user_version, fork_policy, report["money_type"],
                 report["assertions"]["row_count_total_target"], report["schema_digest"],
                 json.dumps(report, default=str)),
            )
            if commit:
                dst.commit()
                report["committed"] = True
            else:
                dst.rollback()
                report["committed"] = False
                report["note"] = (
                    "every assertion passed; --commit was not given, so the transaction was "
                    "rolled back and the target is unchanged"
                )
    finally:
        src.close()

    report["ok"] = not ledger.failures()
    return report


# --------------------------------------------------------------------------- verify (read-only)


def verify(
    *, sqlite_path: str, postgres_url: str, schema: str, float_money: bool = False,
    immutable_source: bool = False,
) -> dict[str, Any]:
    """Re-assert an already-migrated schema against the source. Writes nothing, anywhere."""
    import psycopg  # noqa: PLC0415

    before = preflight.fingerprint(sqlite_path)
    src = open_readonly(sqlite_path, immutable=immutable_source)
    ledger = AssertionLedger()
    report: dict[str, Any] = {
        "tool": "tools/migrate-desk/migrate_desk.py", "run": "verify",
        "at": dt.datetime.now(dt.UTC).isoformat(), "source": before.as_dict(), "schema": schema,
    }
    try:
        schema_obj = read_schema(src)
        with psycopg.connect(postgres_url, autocommit=False) as dst, dst.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            for table in schema_obj.tables:
                rows = read_rows(src, table)
                got = _read_back(cur, schema, table)
                ledger.add(verdict_for(table.name, rows, got, table.column_names))
            report["semantic"] = _semantic_checks(
                ledger, src, cur, schema, schema_obj, float_money
            )
            cur.execute(
                f"SELECT source_sha256, schema_digest, migrated_at, fork_policy "
                f"FROM {pgddl.qualified(schema, 'migration_provenance')} ORDER BY id DESC "
                "LIMIT 1"
            )
            row = cur.fetchone()
            report["provenance"] = (
                {"source_sha256": row[0], "schema_digest": row[1], "migrated_at": row[2],
                 "fork_policy": row[3]} if row else None
            )
            dst.rollback()
    finally:
        src.close()
    report["assertions"] = ledger.as_dict()
    report["schema_digest"] = schema_checksum(ledger.verdicts)
    report["ok"] = not ledger.failures()
    return report


# --------------------------------------------------------------------------- CLI


def _parse_secondary(spec: str, kind: str) -> fork.SecondaryStore:
    """`LABEL=locator`, where a Postgres locator may end in `:schema`."""
    if "=" not in spec:
        raise SystemExit(f"--secondary-{kind} wants LABEL=locator, got {spec!r}")
    label, locator = spec.split("=", 1)
    if kind == "sqlite":
        return fork.open_sqlite_secondary(label, str(pathlib.Path(locator).expanduser()))
    schema = "desk"
    if locator.rsplit("/", 1)[-1].count(":") == 1:
        locator, schema = locator.rsplit(":", 1)
    return fork.open_postgres_secondary(label, locator, schema)


def _common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--sqlite", required=True, help="the source SQLite file (opened read-only)")
    ap.add_argument("--postgres", required=True, help="target DSN")
    ap.add_argument("--schema", default="desk")
    ap.add_argument(
        "--float-money", action="store_true",
        help="land money as double precision instead of numeric — M19.3's literal reading, "
             "against root house rule 9. See desk_pgddl.py.",
    )
    ap.add_argument(
        "--immutable-source", action="store_true",
        help="open the source with immutable=1: no lock, no -shm sidecar. Use when the file is "
             "one you have promised not to touch and nothing is writing to it.",
    )
    ap.add_argument("--secondary-sqlite", action="append", default=[], metavar="LABEL=PATH")
    ap.add_argument("--secondary-postgres", action="append", default=[],
                    metavar="LABEL=DSN[:SCHEMA]")
    ap.add_argument(
        "--fork-policy", required=True, choices=list(fork.FORK_POLICIES),
        help="REQUIRED, no default. There are three forked copies of this database and 7 plans "
             "exist outside the box; see desk_fork.py. Migrating the box while silently dropping "
             "them would destroy evidence, so the choice has to be typed.",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="migrate_desk.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_reh = sub.add_parser("rehearse", help="migrate a COPY into a scratch schema, then drop it")
    _common(p_reh)
    p_reh.add_argument("--rehearsal-schema", default=None)
    p_reh.add_argument("--receipts-dir", default=str(DEFAULT_RECEIPTS))

    p_app = sub.add_parser("apply", help="the real run")
    _common(p_app)
    p_app.add_argument("--backup-manifest", required=True,
                       help="manifest.json from `python -m scripts.backup` (must say ok)")
    p_app.add_argument("--archive-copy", required=True,
                       help="the dated copy outside the repo (opened, quick_check'd)")
    p_app.add_argument("--receipts-dir", default=str(DEFAULT_RECEIPTS))
    p_app.add_argument("--require-rehearsal", action="store_true", default=True)
    p_app.add_argument("--no-require-rehearsal", dest="require_rehearsal", action="store_false")
    p_app.add_argument("--drop-existing", action="store_true")
    p_app.add_argument("--commit", action="store_true",
                       help="without this the run asserts everything and then rolls back")

    p_ver = sub.add_parser("verify", help="read-only re-assertion of a migrated schema")
    p_ver.add_argument("--sqlite", required=True)
    p_ver.add_argument("--postgres", required=True)
    p_ver.add_argument("--schema", default="desk")
    p_ver.add_argument("--float-money", action="store_true")
    p_ver.add_argument("--immutable-source", action="store_true")

    args = ap.parse_args(argv)

    if args.command == "verify":
        report = verify(sqlite_path=args.sqlite, postgres_url=args.postgres, schema=args.schema,
                        float_money=args.float_money, immutable_source=args.immutable_source)
        print(json.dumps(report, indent=2, default=str))
        return 0 if report["ok"] else 1

    secondaries = [_parse_secondary(s, "sqlite") for s in args.secondary_sqlite]
    secondaries += [_parse_secondary(s, "postgres") for s in args.secondary_postgres]

    try:
        if args.command == "rehearse":
            report = _rehearse(args, secondaries)
        else:
            report = _apply(args, secondaries)
    except (MigrationFailure, preflight.PreflightRefusal, fork.ForkRefusal) as exc:
        print(f"REFUSED / FAILED:\n{exc}", file=sys.stderr)
        return 1
    finally:
        for store in secondaries:
            with contextlib.suppress(Exception):
                store.close()

    print(json.dumps(report, indent=2, default=str))
    if report["ok"]:
        total = report["assertions"]["row_count_total_target"]
        print(
            f"\nOK: {len(report['assertions']['tables'])} tables, {total} rows, every row_count "
            f"and checksum matched. committed={report['committed']}",
            file=sys.stderr,
        )
        return 0
    print("\nFAILED, nothing committed:\n  " + "\n  ".join(report["assertions"]["failures"]),
          file=sys.stderr)
    return 1


def _rehearse(args: argparse.Namespace, secondaries: list[fork.SecondaryStore]) -> dict[str, Any]:
    """Rehearse against a copy. The source is read once, through the backup API, and no more."""
    original = preflight.fingerprint(args.sqlite)
    scratch_schema = args.rehearsal_schema or f"{args.schema}_rehearsal"
    if scratch_schema == args.schema:
        raise MigrationFailure(
            "the rehearsal schema is the real schema; a rehearsal must not be able to touch the "
            "target it is rehearsing for"
        )
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="desk-rehearsal-"))
    try:
        copy = consistent_copy(
            args.sqlite, workdir / "portfolio-copy.db", immutable=args.immutable_source
        )
        copy_fp = preflight.fingerprint(copy)
        report = migrate(
            sqlite_path=str(copy), postgres_url=args.postgres, schema=scratch_schema,
            fork_policy=args.fork_policy, drop_existing=True, commit=False,
            float_money=args.float_money, secondaries=secondaries, label="rehearse",
        )
        report["rehearsal"] = {
            "original": original.as_dict(),
            "copy": copy_fp.as_dict(),
            "copy_quick_check": preflight.quick_check(copy),
            "scratch_schema": scratch_schema,
            "note": "the copy was taken through SQLite's backup API, not cp; the scratch schema "
                    "was built, asserted and rolled back, so nothing persists",
        }
        # The receipt is keyed on the ORIGINAL's sha256 — that is what `apply` will be asked for.
        report["source"] = original.as_dict()
        preflight.write_receipt(
            args.receipts_dir,
            {
                "at": report["at"], "ok": report["ok"], "fork_policy": args.fork_policy,
                "source": original.as_dict(), "copy": copy_fp.as_dict(),
                "schema_digest": report.get("schema_digest"),
                "row_count_total": report["assertions"]["row_count_total_target"],
                "tables": len(report["assertions"]["tables"]),
                "money_type": report["money_type"],
                "failures": report["assertions"]["failures"],
            },
        )
        report["receipt"] = str(
            preflight.receipt_path(args.receipts_dir, original.sha256, args.fork_policy)
        )
        return report
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _apply(args: argparse.Namespace, secondaries: list[fork.SecondaryStore]) -> dict[str, Any]:
    """The real run — every safety rail first, then one transaction."""
    rails: dict[str, Any] = {}
    rails["backup_manifest"] = preflight.verify_backup_manifest(args.backup_manifest)
    rails["archive_copy"] = preflight.verify_archive(args.archive_copy, REPO_ROOT)
    source = preflight.fingerprint(args.sqlite)
    if args.require_rehearsal:
        rails["rehearsal"] = preflight.require_rehearsal(
            args.receipts_dir, source.sha256, args.fork_policy
        )
    else:
        rails["rehearsal"] = "SKIPPED by --no-require-rehearsal"

    report = migrate(
        sqlite_path=args.sqlite, postgres_url=args.postgres, schema=args.schema,
        fork_policy=args.fork_policy, drop_existing=args.drop_existing, commit=args.commit,
        float_money=args.float_money, immutable_source=args.immutable_source,
        secondaries=secondaries, label="apply",
    )
    report["preflight"] = rails
    return report


if __name__ == "__main__":
    raise SystemExit(main())
