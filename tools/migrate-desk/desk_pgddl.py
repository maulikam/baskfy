"""SQLite schema -> PostgreSQL DDL, including the keys the old migrator threw away.

THE NUMERIC DECISION (root house rule 9 vs DECISIONS-MERGE M19.3)
-----------------------------------------------------------------
Root `CLAUDE.md` house rule 9: *"Money and prices are `numeric`, never `float`."*
`scripts/migrate_to_postgres.py:58` maps `REAL -> DOUBLE PRECISION` **and**
`NUMERIC -> DOUBLE PRECISION`, so every price in the laptop's `desk` schema is a float today.

That is not carelessness. M19.3 argues the opposite case on purpose, and the argument is good:
SQLite REAL has been the desk's system of record since it existed, so *"every number the desk has
ever computed, displayed or traded on was already a float. Keeping `Decimal` here would make the
Postgres backend arithmetically different from the record it was copied from, and a migration
that changes the numbers is not a migration."* `app/analytics/pg.py:_native` acts on that: it
converts every `Decimal` psycopg returns back to `float` before the desk's 9,500 lines of float
arithmetic ever see it.

**This tool lands money as `numeric`, and M19.3 is not overruled — it is satisfied.** The two
positions only conflict if you assume the stored type dictates the arithmetic, and here it does
not:

* The value written is `Decimal(repr(float_value))`. `repr()` is the shortest decimal string that
  round-trips a float64 exactly, so `float(Decimal(repr(x))) == x` bit for bit. Nothing is
  rounded, widened, or re-interpreted on the way in.
* `numeric` in Postgres is arbitrary-precision and stores that string exactly.
* `pg.py:_native` already converts `Decimal -> float` on the way out, so the desk's arithmetic is
  unchanged: it still computes on precisely the float64 it computed on before.
* `desk_assertions.canonical()` renders a `Decimal` as `repr(float(v))`, identical to how it
  renders the SQLite float, so the checksum still proves the copy cell by cell.

So the house rule gets the column type it requires and M19.3 gets the arithmetic it requires.
The reversal, if Maulik prefers M19.3's literal reading, is one flag: `--float-money`.

WHAT STAYS `double precision`, AND WHY
--------------------------------------
House rule 9 governs *money and prices*. Four columns are epoch clocks and two are stopwatches;
calling those "money" to get a tidier rule would be the kind of over-reach the root's precedence
list warns about. They stay `double precision`, listed explicitly rather than matched by a name
heuristic — a heuristic on column names is how `duration_s` ends up as currency.

BOOLEANS, JSON, TIMESTAMPS: unchanged on purpose
------------------------------------------------
* `regime_evaluations.committed` and eight siblings are `INTEGER` 0/1 and the desk's SQL compares
  them to `0`/`1`. They land as `bigint`. Landing them as `boolean` is a change to every one of
  those comparisons — a code change wearing a schema change's clothes.
* `*_json` columns land as `text`. `jsonb` normalises whitespace and reorders keys, which changes
  the bytes of an evidence column and breaks any checksum taken over it. Convert later, as a
  separate reviewed change, or never.
* ISO-text dates land as `text`. `pg.py:_native` already has to convert a Postgres `date` back to
  an ISO string because the desk compares strings throughout; moving to `timestamptz` also means
  deciding, per column, that the naive strings are IST and the epoch floats are UTC. That is a
  stated assumption and a code change, not a cast, and it is not this leaf's to make.
"""
from __future__ import annotations

import decimal
import math
from typing import Any

from desk_introspect import Column, ForeignKey, Index, Schema, SchemaRefusal, Table

#: Everything that is not a REAL. REAL is decided per column by `pg_type`.
_BASE_TYPES = {
    "INTEGER": "bigint",
    "TEXT": "text",
    "BLOB": "bytea",
    "": "text",
}

#: REAL columns that are NOT money or a price, and therefore stay `double precision`.
#: (table, column) -> why. Anything REAL and absent from this map becomes `numeric`.
FLOAT_BY_DESIGN: dict[tuple[str, str], str] = {
    ("fills", "when_ts"): "epoch seconds (UTC), a clock reading — not a price",
    ("trades", "entry_ts"): "epoch seconds (UTC), a clock reading — not a price",
    ("trades", "exit_ts"): "epoch seconds (UTC), a clock reading — not a price",
    ("rebalance_versions", "created_ts"): "epoch seconds (UTC), a clock reading — not a price",
    ("daily_runs", "duration_s"): "a stopwatch in seconds — not money",
    ("ops_jobs", "duration_s"): "a stopwatch in seconds — not money",
    ("option_arms", "dte_at_entry"): "days to expiry — a count of days, not money",
}

#: Every REAL column in the desk's schema that this tool lands as `numeric`, with what it is.
#: Not consulted at runtime — `pg_type` works by exclusion so a column added tomorrow defaults to
#: the safe side — but written out so a reviewer can see the whole list without running anything.
NUMERIC_COLUMNS: dict[tuple[str, str], str] = {
    ("benchmark", "close"): "index close",
    ("benchmark", "tri"): "total-return index level",
    ("breadth_readings", "pct_above_20dma"): "percentage",
    ("breadth_readings", "coverage_pct"): "percentage",
    ("cashflows", "amount"): "money in/out",
    ("corporate_actions", "ratio_new"): "corporate-action ratio",
    ("corporate_actions", "ratio_old"): "corporate-action ratio",
    ("fills", "price"): "execution price",
    ("fills", "charges"): "money",
    ("index_series", "open"): "price",
    ("index_series", "high"): "price",
    ("index_series", "low"): "price",
    ("index_series", "close"): "price",
    ("option_arms", "entry_spot"): "price",
    ("option_arms", "exit_spot"): "price",
    ("option_arms", "entry_credit"): "money",
    ("option_arms", "max_loss"): "money",
    ("option_arms", "margin"): "money",
    ("option_arms", "spot_move_pct"): "percentage",
    ("rebalance_orders", "planned_ref_price"): "price",
    ("rebalance_orders", "avg_fill_price"): "price",
    ("regime_book_snapshots", "coverage_pct"): "percentage",
    ("regime_evaluations", "breadth_pct"): "percentage",
    ("regime_evaluations", "breadth_coverage_pct"): "percentage",
    ("regime_exposure", "actual_equity_pct"): "percentage of the book",
    ("regime_exposure", "target_equity_cap_pct"): "percentage of the book",
    ("regime_exposure", "exposure_gap_pct"): "percentage of the book",
    ("regime_exposure", "pending_buy_pct"): "percentage of the book",
    ("regime_exposure", "pending_sell_pct"): "percentage of the book",
    ("snapshots", "nav"): "money — the NAV series itself",
    ("snapshots", "invested"): "money",
    ("snapshots", "cash"): "money",
    ("snapshots", "index_value"): "the base-100 portfolio index",
    ("trades", "entry_price"): "price",
    ("trades", "exit_price"): "price",
    ("trades", "entry_score"): "a momentum score; numeric for consistency with its neighbours",
    ("trades", "pnl"): "money",
    ("trades", "costs"): "money",
}


#: SQLite default expressions this tool will reproduce. Everything else is refused rather than
#: translated, because a default is silent: get it wrong and nobody sees it until a row is written
#: without that column and lands with the wrong value.
_LITERAL_DEFAULT = __import__("re").compile(r"^(?:-?\d+(?:\.\d+)?|'(?:[^']|'')*'|NULL)$", 2)

#: Refused by name, with the reason, so the message is useful when one turns up.
_REFUSED_DEFAULTS = {
    "CURRENT_TIMESTAMP": "SQLite renders it as UTC text 'YYYY-MM-DD HH:MM:SS'; Postgres renders a "
                         "timestamptz. Not the same value, so it is not a translation",
    "CURRENT_DATE": "same problem as CURRENT_TIMESTAMP",
    "CURRENT_TIME": "same problem as CURRENT_TIMESTAMP",
}


def default_clause(table: str, column: Column) -> str:
    """`DEFAULT 0`, `DEFAULT 'PENDING'` — carried, because the desk's INSERTs rely on them.

    Ten live columns have one: `fills.charges DEFAULT 0`, `rebalance_orders.filled_qty DEFAULT 0`
    and `.status DEFAULT 'PENDING'`, `index_series.is_final DEFAULT 1`, `daily_runs.failed_steps
    DEFAULT 0`, and the `regime_evaluations` / `regime_exposure` flags. The desk writes
    `INSERT INTO regime_evaluations(evaluation_id, run_id) …` and expects `committed` to be 0.
    Drop the default and that insert either fails on NOT NULL or writes a NULL into a flag the
    reporting code reads as a boolean.
    """
    if column.default is None:
        return ""
    raw = column.default.strip()
    upper = raw.upper()
    if upper in _REFUSED_DEFAULTS:
        raise SchemaRefusal(
            f"{table}.{column.name} defaults to {raw}: {_REFUSED_DEFAULTS[upper]}"
        )
    if not _LITERAL_DEFAULT.match(raw):
        raise SchemaRefusal(
            f"{table}.{column.name} has default expression {raw!r}; this tool carries only "
            "literal defaults and refuses to translate an expression it might get wrong"
        )
    return f" DEFAULT {raw}"


def quote(identifier: str) -> str:
    if '"' in identifier:  # pragma: no cover — no such identifier exists in this database
        raise SchemaRefusal(f"identifier contains a double quote: {identifier!r}")
    return f'"{identifier}"'


def qualified(schema: str, table: str) -> str:
    return f"{quote(schema)}.{quote(table)}"


def pg_type(table: str, column: Column, float_money: bool = False) -> str:
    """The Postgres type for one column. REAL is the only interesting case."""
    if column.decl_type in _BASE_TYPES:
        return _BASE_TYPES[column.decl_type]
    if column.decl_type in ("REAL", "NUMERIC"):
        if float_money:
            return "double precision"
        if (table, column.name) in FLOAT_BY_DESIGN:
            return "double precision"
        return "numeric"
    raise SchemaRefusal(  # pragma: no cover — read_schema rejects these first
        f"{table}.{column.name}: no Postgres type for declared {column.decl_type!r}"
    )


def lands_as_numeric(table: str, column: Column, float_money: bool = False) -> bool:
    return pg_type(table, column, float_money) == "numeric"


def to_pg_value(value: Any, numeric: bool) -> Any:  # noqa: ANN401 — a SQLite cell
    """Convert one SQLite cell for insertion, pinning the float -> numeric rendering.

    `Decimal(repr(x))` is the shortest decimal string that round-trips `x` exactly. Any other
    route — `Decimal(x)` from the binary float, or a rounded string — either explodes into
    seventeen digits of binary noise or loses the value. Both would still checksum-match on the
    way back out, which is exactly why the rendering has to be pinned here rather than trusted.
    """
    if value is None:
        return None
    if not numeric:
        return value
    if isinstance(value, bool):  # pragma: no cover — sqlite3 never returns bool
        return decimal.Decimal(int(value))
    if isinstance(value, int):
        return decimal.Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SchemaRefusal(
                f"a non-finite float ({value!r}) cannot be stored as Postgres numeric; "
                "this database has none today and the migration refuses to invent a policy"
            )
        return decimal.Decimal(repr(value))
    raise SchemaRefusal(
        f"a {type(value).__name__} ({value!r}) turned up in a column destined for numeric; "
        "SQLite affinity should have made this a float, so the schema and the data disagree"
    )


def create_table(schema: str, table: Table, float_money: bool = False) -> str:
    """`CREATE TABLE` with the primary key, the unique constraints, and identity columns.

    Foreign keys are deliberately NOT here — see `add_foreign_keys`.
    """
    identity = table.identity_column
    parts: list[str] = []
    for column in table.columns:
        piece = f"  {quote(column.name)} {pg_type(table.name, column, float_money)}"
        if column.name == identity:
            # `GENERATED BY DEFAULT AS IDENTITY` is what stops the defect leaf 1.3.1 measured in
            # store C: an identity column is implicitly NOT NULL, so the desk's
            # `INSERT INTO rebalance_orders(version_id, symbol, …)` gets a generated id instead of
            # silently writing NULL into the primary key of an evidence table. "BY DEFAULT" rather
            # than "ALWAYS" because this migration inserts the historical ids explicitly.
            piece += " GENERATED BY DEFAULT AS IDENTITY"
        else:
            piece += default_clause(table.name, column)
            if column.notnull:
                piece += " NOT NULL"
        parts.append(piece)
    if table.pk_columns:
        cols = ", ".join(quote(c) for c in table.pk_columns)
        parts.append(f"  CONSTRAINT {quote('pk_' + table.name)} PRIMARY KEY ({cols})")
    for constraint in table.unique_constraints:
        cols = ", ".join(quote(c) for c, _desc in constraint.columns)
        name = "ux_" + table.name + "_" + "_".join(c for c, _ in constraint.columns)
        parts.append(f"  CONSTRAINT {quote(name)} UNIQUE ({cols})")
    body = ",\n".join(parts)
    return f"CREATE TABLE {qualified(schema, table.name)} (\n{body}\n)"


def create_index(schema: str, table: Table, index: Index) -> str:
    """A `CREATE INDEX`, rebuilt from the pragmas rather than string-edited from the DDL.

    Partial indexes carry their predicate verbatim. `ux_regime_canonical`
    (`… ON regime_evaluations(evaluation_id) WHERE run_id = 'canonical'`) is the one that matters:
    it is what enforces "a regime evaluation is committed exactly once", Postgres supports partial
    unique indexes natively, and store C does not have it at all.
    """
    unique = "UNIQUE " if index.unique else ""
    cols = ", ".join(quote(c) + (" DESC" if desc else "") for c, desc in index.columns)
    sql = (
        f"CREATE {unique}INDEX {quote(index.name)} ON {qualified(schema, table.name)} ({cols})"
    )
    if index.where:
        sql += f" WHERE {index.where}"
    return sql


def add_foreign_keys(schema: str, table: Table) -> list[str]:
    """FKs as a separate `ALTER TABLE` pass, run after every table exists and is loaded.

    Doing it this way means table creation needs no dependency ordering and the load needs no
    insert ordering, and it still ends with the constraint enforced and validated by Postgres
    against the copied rows — which is itself a check the old migrator could not perform, because
    it had no FK at all. The desk's one FK is
    `rebalance_orders.version_id -> rebalance_versions(version_id)`.
    """
    out: list[str] = []
    for fk in table.foreign_keys:
        cols = ", ".join(quote(c) for c in fk.columns)
        refs = ", ".join(quote(c) for c in fk.ref_columns)
        out.append(
            f"ALTER TABLE {qualified(schema, table.name)} "
            f"ADD CONSTRAINT {quote(fk.name)} FOREIGN KEY ({cols}) "
            f"REFERENCES {qualified(schema, fk.ref_table)} ({refs})"
        )
    return out


def setval(schema: str, table: Table) -> tuple[str, tuple[Any, ...], str] | None:
    """Carry the AUTOINCREMENT high-water mark onto the Postgres identity sequence.

    Returns (sql, params, provenance) or None for a table with no identity column.

    `sqlite_sequence` is the source, NOT `max(id)`. On the live box `regime_evaluations.seq` is 32
    against 26 rows and `settings_audit.seq` is 6 against 1 — rows have been deleted, and SQLite's
    counter remembers. Restart from `max(id)` and the first insert after cutover re-issues an id
    that has already been used in an evidence table.

    A table with AUTOINCREMENT and no `sqlite_sequence` row has never had an insert (`cashflows`
    is the live example, at 0 rows). Its sequence starts at 1.
    """
    column = table.identity_column
    if column is None:
        return None
    if table.sequence is None:
        return (
            "SELECT setval(pg_get_serial_sequence(%s, %s), %s, false)",
            (f"{schema}.{table.name}", column, 1),
            "no sqlite_sequence row: the table has never been inserted into; sequence starts at 1",
        )
    return (
        "SELECT setval(pg_get_serial_sequence(%s, %s), %s, true)",
        (f"{schema}.{table.name}", column, table.sequence),
        f"sqlite_sequence.seq = {table.sequence}; next id will be {table.sequence + 1}",
    )


def create_provenance(schema: str) -> list[str]:
    """A table that records what was migrated, from which file, when, and under which policy.

    The old migrator carried `schema_version` and nothing else, so nothing in the target could
    answer "which SQLite file is this a copy of?" — which is the first question anyone asks of a
    migrated evidence store nine months later.
    """
    return [
        f"CREATE TABLE {qualified(schema, 'migration_provenance')} (\n"
        "  id            bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,\n"
        "  migrated_at   text        NOT NULL,\n"
        "  source_path   text        NOT NULL,\n"
        "  source_sha256 text        NOT NULL,\n"
        "  source_bytes  bigint      NOT NULL,\n"
        "  user_version  bigint      NOT NULL,\n"
        "  fork_policy   text        NOT NULL,\n"
        "  money_type    text        NOT NULL,\n"
        "  row_total     bigint      NOT NULL,\n"
        "  schema_digest text        NOT NULL,\n"
        "  report_json   text        NOT NULL\n"
        ")",
        f"CREATE TABLE {qualified(schema, 'schema_version')} (version integer NOT NULL)",
    ]


def summarise_types(schema_obj: Schema, float_money: bool = False) -> dict[str, list[str]]:
    """Which columns landed as which type — the evidence for gate G5."""
    out: dict[str, list[str]] = {"numeric": [], "double precision": [], "bigint": [], "text": []}
    for table in schema_obj.tables:
        for column in table.columns:
            kind = pg_type(table.name, column, float_money)
            out.setdefault(kind, []).append(f"{table.name}.{column.name}")
    return out


def declared_real_columns(schema_obj: Schema) -> list[str]:
    return [
        f"{t.name}.{c.name}"
        for t in schema_obj.tables
        for c in t.columns
        if c.decl_type in ("REAL", "NUMERIC")
    ]


__all__ = [
    "FLOAT_BY_DESIGN",
    "NUMERIC_COLUMNS",
    "ForeignKey",
    "add_foreign_keys",
    "create_index",
    "create_provenance",
    "create_table",
    "declared_real_columns",
    "default_clause",
    "lands_as_numeric",
    "pg_type",
    "qualified",
    "quote",
    "setval",
    "summarise_types",
    "to_pg_value",
]
