"""THE FORK DECISION. Read this before running anything.

**This tool does not decide it. Maulik does, and `--fork-policy` is where he says so.**
There is no default. A run without the flag is refused, on purpose: the failure mode this
guards against is not a wrong answer, it is a migration that silently drops evidence and reports
success.

WHAT THE FORK IS (leaf 1.3.1 §1a, driver-verified)
--------------------------------------------------
There are three copies of the desk's database and they are three lineages, not three views:

    A  desk@65.0.226.77:~/kite-momentum-rebalancer/data/portfolio.db
           43,411 rows, live, DRY_RUN=false — THE RECORD
    B  laptop kite-momentum-rebalancer/data/portfolio.db
           42,285 rows, frozen 22 Aug 08:16
    C  laptop Postgres `desk` schema (baskfy-postgres, port 5433)
           42,359 rows, frozen 22 Aug 08:52

A and B share history only to 19 Aug ~13:07. Migrating A alone is correct for 43,411 of 43,411
rows — and loses exactly this, which exists nowhere on the box:

    B  rebalance_versions 39efc9eefca7  ("scan Investing_001 (1).csv", 19 Aug 18:17 IST)
       + its rebalance_orders
    C  the same 39efc9eefca7, plus six plans written on the cutover morning
       (cef40037bcc6, 42c0b852a80c, a71689f13032, 9cf46614a665, 9915a620a4b0, 1bd7b94babc6),
       carrying 67 rebalance_orders between them — all 67 of which have `id IS NULL` in
       Postgres today, because the migrator that wrote them dropped the primary key.

Seven plans and their orders. None of them corresponds to a real order on the box; they are
DRY_RUN-era planning. That is an argument for discarding them and not a reason to do it by
accident.

THE THREE POLICIES
------------------
``box-only``
    Migrate A. Nothing else is read, nothing else is written. B and C keep existing as files and
    as a Postgres schema; this policy simply does not merge them. Leaf 1.3.1's own recommendation,
    and the cheapest to reverse — `box-plus-orphans` can be run later against the same archive.

``box-only-quarantine``
    Migrate A into `<schema>`, and additionally write every orphan row into `<schema>_quarantine`
    with a `_orphan_source` column naming the store it came from. The live schema is A and only A,
    so no query, report or NAV calculation can accidentally include a plan the desk never ran —
    but nothing has been destroyed and the rows are one `INSERT … SELECT` away if the decision
    changes. **The recommended answer if Maulik does not want to decide today.**

``box-plus-orphans``
    Migrate A, then insert the orphan rows into the live tables. `rebalance_versions.version_id`
    is TEXT and globally unique, so plans merge cleanly. `rebalance_orders.id` does NOT:
    A's identity high-water mark is 479 and the orphans' ids either collide with it or are NULL,
    so every orphan order is given a fresh id above the mark and the remapping is recorded in the
    run report. The FK to `rebalance_versions` is satisfied because the parent plans come with
    them.

WHAT IS ELIGIBLE TO BE AN ORPHAN, AND WHAT IS NOT
--------------------------------------------------
Only `rebalance_versions` and `rebalance_orders`. Those are the only tables where the forked
stores hold rows the box does not, and — critically — the only ones with a key stable enough to
tell "absent from A" from "the same row wearing a different id":

    rebalance_versions   version_id, TEXT, globally unique. Safe.
    rebalance_orders     reachable only through version_id. Safe as a child of an orphan plan.

Deliberately excluded, with reasons:

    trades               `id` is re-issued on every FIFO rebuild (§5.2: the box has 7,862 rows
                         with id <= 11831 where the laptop has 8,198 with the same maximum), and
                         the natural key (symbol, entry_ts, qty) is not unique — 8,530 rows
                         collapse to 5,815 triples. There is no way to say "this lot is missing"
                         that is not a guess. It is also 100% derivable from `fills` today.
    daily_runs           `id` only. Re-importing duplicates silently.
    ops_jobs             `id` only. Same.
    settings_audit       `id` only. Same.
    fills                keyed on the broker's `trade_id`, so orphans *would* be detectable — but
                         B and C hold none the box lacks, and a fill that existed only on a
                         laptop would be a finding to investigate, not a row to merge. If this
                         ever returns a non-empty set, stop and read it.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from desk_introspect import Table

BOX_ONLY = "box-only"
QUARANTINE = "box-only-quarantine"
MERGE = "box-plus-orphans"
FORK_POLICIES = (BOX_ONLY, QUARANTINE, MERGE)

#: Tables an orphan row may belong to, and the key that identifies it. See the module docstring
#: for why every other table is excluded.
ORPHAN_TABLES = {
    "rebalance_versions": ("version_id",),
    "rebalance_orders": ("version_id",),   # a child: carried iff its plan is an orphan
}

#: Read but never merged — a non-empty result here is a finding, not a migration input.
TRIPWIRE_TABLES = {"fills": ("trade_id",)}


class ForkRefusal(RuntimeError):
    """The fork policy cannot be honoured with the stores that were supplied."""


@dataclass
class SecondaryStore:
    """One of the forked copies, opened read-only."""

    label: str
    kind: str                       # "sqlite" | "postgres"
    locator: str                    # a file path or a DSN (never printed if it holds a secret)
    schema: str = "desk"
    _sqlite: sqlite3.Connection | None = None
    _pg: Any = None

    @property
    def safe_locator(self) -> str:
        """A DSN can carry a password. Never let one into a report or a log line."""
        if self.kind == "postgres" and "@" in self.locator:
            return "postgresql://<redacted>@" + self.locator.rsplit("@", 1)[1]
        return self.locator

    def rows(self, table: str, columns: tuple[str, ...]) -> list[tuple[Any, ...]]:
        cols_sqlite = ", ".join(f'"{c}"' for c in columns)
        if self.kind == "sqlite":
            assert self._sqlite is not None
            return [tuple(r) for r in self._sqlite.execute(f'SELECT {cols_sqlite} FROM "{table}"')]
        assert self._pg is not None
        with self._pg.cursor() as cur:
            cur.execute(f'SELECT {cols_sqlite} FROM "{self.schema}"."{table}"')
            return [tuple(r) for r in cur.fetchall()]

    def has_table(self, table: str) -> bool:
        if self.kind == "sqlite":
            assert self._sqlite is not None
            return bool(
                self._sqlite.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()[0]
            )
        assert self._pg is not None
        with self._pg.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema=%s AND table_name=%s",
                (self.schema, table),
            )
            return bool(cur.fetchone()[0])

    def close(self) -> None:
        if self._sqlite is not None:
            self._sqlite.close()
        if self._pg is not None:
            self._pg.close()


def open_sqlite_secondary(label: str, path: str) -> SecondaryStore:
    conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return SecondaryStore(label=label, kind="sqlite", locator=path, _sqlite=conn)


def open_postgres_secondary(label: str, dsn: str, schema: str = "desk") -> SecondaryStore:
    import psycopg  # noqa: PLC0415 — only needed when a Postgres secondary is actually supplied

    conn = psycopg.connect(dsn, autocommit=False)
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
    return SecondaryStore(label=label, kind="postgres", locator=dsn, schema=schema, _pg=conn)


@dataclass
class OrphanSet:
    """Rows that exist in a forked store and not in A."""

    plans: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)   # (source, row)
    orders: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    plan_columns: tuple[str, ...] = ()
    order_columns: tuple[str, ...] = ()
    tripwires: dict[str, list[str]] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    #: version_id -> {store: order rows that store also holds for a plan it does not own}.
    #: A count here that differs from the owning store's is a discrepancy to look at before
    #: choosing `box-plus-orphans`; it is never merged.
    also_seen_in: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.plans and not self.orders

    def _orders_by_plan(self) -> dict[str, int]:
        at = self.order_columns.index("version_id") if self.order_columns else 1
        out: dict[str, int] = {}
        for _source, row in self.orders:
            out[str(row[at])] = out.get(str(row[at]), 0) + 1
        return dict(sorted(out.items()))

    def as_dict(self) -> dict[str, Any]:
        by_source: dict[str, dict[str, int]] = {}
        for source, _row in self.plans:
            by_source.setdefault(source, {"plans": 0, "orders": 0})["plans"] += 1
        for source, _row in self.orders:
            by_source.setdefault(source, {"plans": 0, "orders": 0})["orders"] += 1
        plan_index = self.plan_columns.index("version_id") if self.plan_columns else 0
        return {
            "sources": self.sources,
            "plans": len(self.plans),
            "orders": len(self.orders),
            "by_source": by_source,
            "plan_ids": sorted({str(row[plan_index]) for _s, row in self.plans}),
            "orders_by_owning_plan": self._orders_by_plan(),
            "duplicated_across_stores": self.also_seen_in,
            "tripwires": self.tripwires,
        }


def discover(
    primary: sqlite3.Connection,
    primary_tables: dict[str, Table],
    secondaries: list[SecondaryStore],
) -> OrphanSet:
    """Everything the forked stores hold that A does not, for the eligible tables only."""
    out = OrphanSet(sources=[s.label for s in secondaries])
    if not secondaries:
        return out

    plans_table = primary_tables["rebalance_versions"]
    orders_table = primary_tables["rebalance_orders"]
    out.plan_columns = plans_table.column_names
    out.order_columns = orders_table.column_names

    known_plans = {
        str(r[0]) for r in primary.execute("SELECT version_id FROM rebalance_versions")
    }
    # A plan can exist in more than one forked store — `39efc9eefca7` is in B *and* in C, because
    # C was migrated from B on 22 Aug. Its orders must be carried once, from one store, or a
    # 2-order plan arrives with 4 orders. First store on the command line owns the plan and
    # supplies its orders; any other store's differing order count is reported, not merged.
    owner: dict[str, str] = {}

    for store in secondaries:
        if not store.has_table("rebalance_versions"):
            raise ForkRefusal(
                f"secondary store {store.label} has no rebalance_versions table; it is not a "
                "copy of the desk's database"
            )
        # Column sets can differ between stores (C carries an extra `schema_version` table, and a
        # store frozen before an ALTER may lack a column). Read A's column list from whichever
        # subset the secondary actually has, and NULL-fill the rest, recording it.
        plan_cols = _intersect(store, "rebalance_versions", plans_table.column_names)
        for row in store.rows("rebalance_versions", plan_cols):
            version_id = str(row[plan_cols.index("version_id")])
            if version_id in known_plans or version_id in owner:
                continue
            owner[version_id] = store.label
            out.plans.append((store.label, _widen(row, plan_cols, plans_table.column_names)))

        order_cols = _intersect(store, "rebalance_orders", orders_table.column_names)
        vid_at = order_cols.index("version_id")
        for row in store.rows("rebalance_orders", order_cols):
            version_id = str(row[vid_at])
            if version_id not in owner:
                continue
            if owner[version_id] == store.label:
                out.orders.append(
                    (store.label, _widen(row, order_cols, orders_table.column_names))
                )
            else:
                out.also_seen_in.setdefault(version_id, {}).setdefault(store.label, 0)
                out.also_seen_in[version_id][store.label] += 1

        for table, key in TRIPWIRE_TABLES.items():
            if not store.has_table(table):
                continue
            here = {str(r[0]) for r in store.rows(table, key)}
            there = {
                str(r[0]) for r in primary.execute(f'SELECT "{key[0]}" FROM "{table}"')
            }
            extra = sorted(here - there)
            if extra:
                out.tripwires[f"{store.label}.{table}"] = extra[:50]
    return out


def _intersect(store: SecondaryStore, table: str, wanted: tuple[str, ...]) -> tuple[str, ...]:
    """The columns A wants that this secondary actually has, in A's order."""
    if store.kind == "sqlite":
        assert store._sqlite is not None
        have = {str(r[1]) for r in store._sqlite.execute(f'PRAGMA table_info("{table}")')}
    else:
        assert store._pg is not None
        with store._pg.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s",
                (store.schema, table),
            )
            have = {str(r[0]) for r in cur.fetchall()}
    present = tuple(c for c in wanted if c in have)
    if not present:
        raise ForkRefusal(f"{store.label}.{table} shares no columns with the box's table")
    return present


def _widen(
    row: tuple[Any, ...], present: tuple[str, ...], full: tuple[str, ...]
) -> tuple[Any, ...]:
    """Re-shape a secondary's row to A's full column list, NULL-filling what it lacks."""
    lookup = dict(zip(present, row, strict=True))
    return tuple(lookup.get(c) for c in full)


def remap_order_ids(
    orders: list[tuple[str, tuple[Any, ...]]],
    order_columns: tuple[str, ...],
    start_above: int,
) -> tuple[list[tuple[str, tuple[Any, ...]]], list[dict[str, Any]], int]:
    """Give every orphan order an id above A's high-water mark, and record the remapping.

    Orphan ids are unusable as they stand: in store B they collide with ids A has issued to
    different rows, and in store C all 67 of them are NULL because the old migrator dropped the
    primary key. Neither can be carried verbatim. Rewriting them is lossless — nothing joins on
    `rebalance_orders.id`; the plan is addressed by `version_id` — but it is still a change to
    evidence, so every old->new pair goes into the run report.
    """
    id_at = order_columns.index("id")
    next_id = start_above + 1
    remapped: list[tuple[str, tuple[Any, ...]]] = []
    ledger: list[dict[str, Any]] = []
    for source, row in orders:
        as_list = list(row)
        old = as_list[id_at]
        as_list[id_at] = next_id
        ledger.append({"source": source, "old_id": old, "new_id": next_id,
                       "version_id": row[order_columns.index("version_id")]})
        remapped.append((source, tuple(as_list)))
        next_id += 1
    return remapped, ledger, next_id - 1
