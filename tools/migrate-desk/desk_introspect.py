"""Read the desk's SQLite schema completely — including the parts the old migrator missed.

`scripts/migrate_to_postgres.py` builds its target schema from `PRAGMA table_info` alone. That
is columns and nothing else, and the result is measurable: against the laptop's Postgres,
`select count(*) from pg_indexes where schemaname='desk'` returns **0**. Zero indexes, zero
primary keys, zero unique constraints, zero foreign keys. Two live consequences (leaf 1.3.1 §5.5):
67 rows of `desk.rebalance_orders` carry `id IS NULL` because the PK was never there to stop
them, and `INSERT OR IGNORE` — which `pg.py:_DDL` rewrites to a plain `INSERT` — has nothing to
conflict against, so house rule 7 does not hold on that backend.

So this module reads all of it:

    PRAGMA table_info        columns, declared types, NOT NULL, defaults, PK ordinals
    PRAGMA index_list        every index, with its origin (pk / u / c) and partial flag
    PRAGMA index_xinfo       key columns in order, with DESC
    PRAGMA foreign_key_list  the one FK this database has
    sqlite_master.sql        the partial-index WHERE clause, which no pragma exposes
    sqlite_sequence          the AUTOINCREMENT high-water marks (see `Table.sequence`)

Two rules it follows, both learned from this specific database:

1. **Columns come from the pragma, never from parsing the DDL text.** Five columns were added
   by `ALTER TABLE` and sit *after* the closing paren of the stored `CREATE TABLE`
   (`rebalance_orders.order_id`, `.reconciled_at`, `rebalance_versions.evaluation_id`,
   `option_arms.contracts_json`, `.underlying_token`). A DDL parser gets those wrong.
2. **Anything it cannot read exactly, it refuses.** An expression index, an unrecognised
   declared type, a partial predicate it cannot isolate — all raise. A schema translator that
   guesses is how you get a `desk` schema with no keys in it.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any


class SchemaRefusal(RuntimeError):
    """The schema contains something this tool will not guess at. Fix the tool, not the data."""


@dataclass(frozen=True)
class Column:
    name: str
    decl_type: str          # as declared, upper-cased, e.g. "INTEGER", "REAL", "TEXT"
    notnull: bool
    default: str | None
    pk_ordinal: int         # 0 = not part of the PK; 1..n = position within it


@dataclass(frozen=True)
class Index:
    name: str
    unique: bool
    origin: str             # "pk" | "u" (UNIQUE in the table body) | "c" (CREATE INDEX)
    columns: tuple[tuple[str, bool], ...]   # (column, descending)
    where: str | None       # partial-index predicate, verbatim from sqlite_master


@dataclass(frozen=True)
class ForeignKey:
    columns: tuple[str, ...]
    ref_table: str
    ref_columns: tuple[str, ...]

    @property
    def name(self) -> str:
        return "fk_" + "_".join(self.columns) + "_" + self.ref_table


@dataclass
class Table:
    name: str
    columns: list[Column]
    indexes: list[Index]
    foreign_keys: list[ForeignKey]
    autoincrement: bool          # INTEGER PRIMARY KEY AUTOINCREMENT — a rowid alias
    sequence: int | None         # sqlite_sequence.seq, or None if the table never inserted
    ddl: str

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    @property
    def pk_columns(self) -> tuple[str, ...]:
        pk = sorted((c for c in self.columns if c.pk_ordinal), key=lambda c: c.pk_ordinal)
        return tuple(c.name for c in pk)

    @property
    def identity_column(self) -> str | None:
        """The single INTEGER PRIMARY KEY AUTOINCREMENT column, if any."""
        if not self.autoincrement:
            return None
        pk = self.pk_columns
        if len(pk) != 1:  # pragma: no cover — SQLite forbids composite AUTOINCREMENT
            raise SchemaRefusal(f"{self.name}: AUTOINCREMENT with a composite primary key")
        return pk[0]

    @property
    def unique_constraints(self) -> list[Index]:
        """UNIQUE declared in the table body — becomes a Postgres UNIQUE constraint."""
        return [i for i in self.indexes if i.origin == "u"]

    @property
    def standalone_indexes(self) -> list[Index]:
        """CREATE INDEX / CREATE UNIQUE INDEX — becomes a Postgres index of the same name."""
        return [i for i in self.indexes if i.origin == "c"]


@dataclass
class Schema:
    tables: list[Table] = field(default_factory=list)
    user_version: int = 0

    def table(self, name: str) -> Table:
        for t in self.tables:
            if t.name == name:
                return t
        raise KeyError(name)

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(t.name for t in self.tables)


# `WHERE` at the tail of a CREATE INDEX, which is where SQLite puts a partial predicate.
_PARTIAL = re.compile(r"\)\s*WHERE\s+(?P<pred>.+?)\s*;?\s*$", re.I | re.S)


def open_readonly(path: str, immutable: bool = False) -> sqlite3.Connection:
    """Open the source so that no statement this tool issues can possibly write to it.

    `mode=ro` is the default. `immutable=1` additionally opens without taking any lock and
    without creating the `-shm` sidecar — the mode leaf 1.3.1 used against the live box, and the
    only one to use when the file being read is one you have promised not to touch. It is only
    safe when nothing is writing concurrently, which is why it is opt-in.
    """
    uri = f"file:{path}?immutable=1" if immutable else f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _sequences(conn: sqlite3.Connection) -> dict[str, int]:
    """`sqlite_sequence` — the AUTOINCREMENT high-water marks.

    `migrate_to_postgres.py:110` filters `name NOT LIKE 'sqlite_%'` out of its table list, so it
    never sees this table and none of the counters is carried. `max(id) + 1` is not a substitute:
    on the live box `regime_evaluations.seq` is 32 against 26 rows and `settings_audit.seq` is 6
    against 1, because rows have been deleted. Restart a Postgres sequence from `max(id)` and the
    first insert after cutover re-issues an id this database has already used, in an evidence
    table.
    """
    exists = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
    ).fetchone()[0]
    if not exists:
        return {}
    return {r[0]: int(r[1]) for r in conn.execute("SELECT name, seq FROM sqlite_sequence")}


def _index_where(ddl: str | None, index_name: str) -> str | None:
    if not ddl:
        raise SchemaRefusal(
            f"index {index_name} is marked partial but has no DDL in sqlite_master; "
            "its predicate cannot be recovered and must not be guessed"
        )
    match = _PARTIAL.search(ddl)
    if not match:
        raise SchemaRefusal(
            f"index {index_name} is marked partial but its WHERE clause could not be "
            f"isolated from: {ddl!r}"
        )
    return match.group("pred").strip()


def _indexes_of(conn: sqlite3.Connection, table: str) -> list[Index]:
    out: list[Index] = []
    for row in conn.execute(f'PRAGMA index_list("{table}")'):
        _, name, unique, origin, partial = row
        # index_xinfo lists every column including the trailing rowid ones; key=1 marks the
        # ones the index is actually ordered on.
        cols: list[tuple[str, bool]] = []
        for x in conn.execute(f'PRAGMA index_xinfo("{name}")'):
            _seqno, _cid, colname, desc, _coll, key = x
            if not key:
                continue
            if colname is None:
                raise SchemaRefusal(
                    f"index {name} on {table} has an expression key column; this tool does "
                    "not translate expression indexes rather than translate one wrongly"
                )
            cols.append((str(colname), bool(desc)))
        where = None
        if partial:
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
            ).fetchone()
            where = _index_where(ddl[0] if ddl else None, name)
        out.append(
            Index(
                name=str(name),
                unique=bool(unique),
                origin=str(origin),
                columns=tuple(cols),
                where=where,
            )
        )
    return out


def _foreign_keys_of(conn: sqlite3.Connection, table: str) -> list[ForeignKey]:
    grouped: dict[int, list[Any]] = {}
    for row in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
        # id, seq, ref_table, from, to, on_update, on_delete, match
        grouped.setdefault(int(row[0]), []).append(row)
    out: list[ForeignKey] = []
    for _fk_id, rows in sorted(grouped.items()):
        rows.sort(key=lambda r: int(r[1]))
        ref_table = str(rows[0][2])
        cols = tuple(str(r[3]) for r in rows)
        refs = tuple(str(r[4]) for r in rows)
        if any(r is None or r == "None" for r in refs):
            # SQLite allows `REFERENCES t` with no column, meaning t's PK. Resolve it rather
            # than emit an FK that points nowhere.
            pk = [
                r[1] for r in conn.execute(f'PRAGMA table_info("{ref_table}")') if r[5]
            ]
            refs = tuple(str(c) for c in pk)
        out.append(ForeignKey(columns=cols, ref_table=ref_table, ref_columns=refs))
    return out


ALLOWED_DECL_TYPES = frozenset({"INTEGER", "REAL", "TEXT", "NUMERIC", "BLOB", ""})


def read_schema(conn: sqlite3.Connection) -> Schema:
    """The whole schema, in one pass, refusing anything it cannot represent exactly."""
    sequences = _sequences(conn)
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    schema = Schema(user_version=int(conn.execute("PRAGMA user_version").fetchone()[0]))
    for name in names:
        ddl_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        ddl = str(ddl_row[0]) if ddl_row and ddl_row[0] else ""
        columns = []
        for cid, cname, decl, notnull, default, pk in conn.execute(
            f'PRAGMA table_info("{name}")'
        ):
            del cid
            base = str(decl or "").strip().upper().split("(")[0]
            if base not in ALLOWED_DECL_TYPES:
                raise SchemaRefusal(
                    f"{name}.{cname} is declared {decl!r}; this tool maps only "
                    f"{sorted(ALLOWED_DECL_TYPES)} and will not guess at a new one"
                )
            columns.append(
                Column(
                    name=str(cname),
                    decl_type=base,
                    notnull=bool(notnull),
                    default=None if default is None else str(default),
                    pk_ordinal=int(pk),
                )
            )
        table = Table(
            name=name,
            columns=columns,
            indexes=_indexes_of(conn, name),
            foreign_keys=_foreign_keys_of(conn, name),
            autoincrement="AUTOINCREMENT" in ddl.upper(),
            sequence=sequences.get(name),
            ddl=ddl,
        )
        # Touch it once so a composite-AUTOINCREMENT schema fails here, not mid-load.
        _ = table.identity_column
        schema.tables.append(table)
    return schema


def read_rows(
    conn: sqlite3.Connection, table: Table
) -> list[tuple[Any, ...]]:
    """Every row, columns named explicitly so the order is ours and not `SELECT *`'s."""
    cols = ", ".join(f'"{c}"' for c in table.column_names)
    return [tuple(r) for r in conn.execute(f'SELECT {cols} FROM "{table.name}"')]
