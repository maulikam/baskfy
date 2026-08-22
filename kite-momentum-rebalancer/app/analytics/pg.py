"""A Postgres backend wearing sqlite3's interface — M19 §3.

The desk's analytics is 9,500 lines that speak sqlite3: `conn.execute(sql, params)`, `?`
placeholders, `sqlite3.Row` accessed both by name and by position, `lastrowid`, `executemany`.
Rewriting all of it in one commit, on the code that keeps the NAV ledger and the tax lots, is not
a migration anyone should want to review.

So the *interface* moves and the call sites do not. This adapter presents exactly the surface the
desk uses — measured, not guessed: `execute`, `executemany`, `cursor`, `commit`, `close`,
`lastrowid`, `rowcount` — over psycopg, translating as it goes.

WHAT IT TRANSLATES, AND WHAT IT REFUSES TO
--------------------------------------------
Placeholders (`?` → `%s`) and a small set of DDL and function differences are translated. Anything
outside that set is **left alone and allowed to fail loudly** rather than guessed at. A silent
mistranslation in a money query is worse than a crash, because a crash gets fixed on Sunday and a
wrong number gets believed until quarter-end.

THE QUOTE-AWARE PART
---------------------
`?` inside a string literal is data, not a placeholder — `where note like '%?%'` must survive. The
translator tracks single quotes and doubled `''` escapes rather than running a regex over the whole
statement, because the regex version works until the first query with a question mark in a comment.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import decimal
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

#: SQLite spellings the desk uses, and their Postgres equivalents. Deliberately short: every entry
#: is a place where the two dialects genuinely differ, and a long list here would mean the port is
#: really a rewrite in disguise.
_DDL: tuple[tuple[str, str], ...] = (
    ("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY"),
    ("INSERT OR REPLACE INTO", "INSERT INTO"),
    ("INSERT OR IGNORE INTO", "INSERT INTO"),
    ("AUTOINCREMENT", ""),
)

#: `strftime('%Y-%m-%d', x)` is SQLite's. Postgres spells it `to_char(x::date, 'YYYY-MM-DD')`.
_STRFTIME = re.compile(r"strftime\(\s*'([^']+)'\s*,\s*([^)]+)\)", re.I)
_FORMATS = {"%Y-%m-%d": "YYYY-MM-DD", "%Y-%m": "YYYY-MM", "%Y": "YYYY", "%W": "IW", "%w": "ID"}


def translate(sql: str) -> str:
    """SQLite SQL to Postgres SQL, for the constructs the desk actually uses."""
    out = sql
    for old, new in _DDL:
        out = re.sub(re.escape(old), new, out, flags=re.I)

    def _fmt(match: re.Match[str]) -> str:
        pattern, column = match.group(1), match.group(2).strip()
        if pattern not in _FORMATS:
            # Not guessed at. An unknown format string reaching Postgres as `strftime(...)` is an
            # immediate, obvious error; translated wrongly it is a date column that is off by a
            # week and nobody notices until a monthly report disagrees with itself.
            raise ValueError(f"no Postgres translation for strftime pattern {pattern!r}")
        return f"to_char(({column})::date, '{_FORMATS[pattern]}')"

    out = _STRFTIME.sub(_fmt, out)
    return placeholders(out)


def placeholders(sql: str) -> str:
    """`?` → `%s`, but only outside string literals, and escaping any literal `%`.

    psycopg treats `%` as its own format character, so a `like '%x%'` that survives placeholder
    translation still has to be escaped or psycopg raises on a query that is perfectly valid SQL.
    """
    out: list[str] = []
    in_string = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "'":
            # '' inside a string is an escaped quote, not the end of one.
            if in_string and index + 1 < len(sql) and sql[index + 1] == "'":
                out.append("''")
                index += 2
                continue
            in_string = not in_string
            out.append(char)
        elif char == "%":
            out.append("%%")  # psycopg's own format character, inside a literal or not
        elif char == "?" and not in_string:
            out.append("%s")
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _native(value: Any) -> Any:  # noqa: ANN401 — whatever the driver returned
    """Postgres NUMERIC arrives as `Decimal`; SQLite REAL arrives as `float`.

    The desk's analytics is nine and a half thousand lines of float arithmetic, and mixing the two
    raises `TypeError: unsupported operand type(s) for /: 'float' and 'decimal.Decimal'` — which is
    exactly how `/tradebook` failed on the first cutover attempt.

    Converting to float is the faithful choice, not the lazy one. SQLite has been the system of
    record since the desk existed and it stores these columns as REAL, so every number the desk has
    ever computed, displayed or traded on was already a float. Keeping `Decimal` here would make
    the Postgres backend arithmetically *different* from the record it was copied from, and a
    migration that changes the numbers is not a migration.
    """
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        # SQLite has no date type; the desk stores and compares ISO strings throughout.
        return value.isoformat()
    return value


class Row(dict[str, Any]):
    """A `sqlite3.Row` stand-in: subscriptable by name *and* by position.

    26 call sites do `dict(row)` and 29 do `row["name"]`, so a mapping is the right base. The
    positional case exists because `conn.execute("select count(*)...").fetchone()[0]` appears
    throughout, and rewriting those would have made this a code change rather than a backend one.
    """

    def __getitem__(self, key: Any) -> Any:  # noqa: ANN401 — mirrors sqlite3.Row exactly
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

    def keys(self) -> Any:  # noqa: ANN401 — sqlite3.Row exposes keys(); call sites use it
        return list(super().keys())


class Cursor:
    """The subset of `sqlite3.Cursor` the desk touches."""

    def __init__(self, raw: Any) -> None:  # noqa: ANN401 — a psycopg cursor
        self._raw = raw

    def _rows(self, record: Sequence[Any] | None) -> Row | None:
        if record is None:
            return None
        names = [d[0] for d in (self._raw.description or [])]
        return Row(zip(names, (_native(v) for v in record), strict=False))

    def fetchone(self) -> Row | None:
        return self._rows(self._raw.fetchone())

    def fetchall(self) -> list[Row]:
        return [r for r in (self._rows(rec) for rec in self._raw.fetchall()) if r is not None]

    def fetchmany(self, size: int = 1) -> list[Row]:
        return [r for r in (self._rows(rec) for rec in self._raw.fetchmany(size)) if r is not None]

    def __iter__(self) -> Iterator[Row]:
        return iter(self.fetchall())

    @property
    def rowcount(self) -> int:
        return int(self._raw.rowcount)

    @property
    def lastrowid(self) -> int | None:
        """Postgres has no lastrowid. Call sites that need it must use RETURNING.

        Returning a wrong id is how you attach a fill to the wrong trade, so this refuses rather
        than approximates. Four call sites use it; they are listed in DECISIONS-MERGE M19.
        """
        raise NotImplementedError(
            "lastrowid has no Postgres equivalent — use `INSERT ... RETURNING id`"
        )


class Connection:
    """The subset of `sqlite3.Connection` the desk touches."""

    def __init__(self, dsn: str) -> None:
        import psycopg  # noqa: PLC0415 — only needed when this backend is actually selected

        self._conn = psycopg.connect(dsn, autocommit=True)
        # Accepted and ignored: rows are already mappings. `db.connect` sets it unconditionally,
        # and refusing it here would mean editing the one line this whole adapter exists to avoid.
        self.row_factory: Any = None

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Cursor:
        cur = self._conn.cursor()
        cur.execute(translate(sql), tuple(params) if params else None)
        return Cursor(cur)

    def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> Cursor:
        cur = self._conn.cursor()
        cur.executemany(translate(sql), [tuple(p) for p in seq])
        return Cursor(cur)

    def cursor(self) -> Cursor:
        return Cursor(self._conn.cursor())

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    @property
    def raw(self) -> Any:  # noqa: ANN401 — it is a psycopg connection; naming it would import it
        """The psycopg connection, for the places that genuinely need it."""
        return self._conn


@contextlib.contextmanager
def connect(dsn: str) -> Iterator[Connection]:
    conn = Connection(dsn)
    try:
        yield conn
    finally:
        conn.close()
