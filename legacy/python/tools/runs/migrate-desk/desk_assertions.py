"""Row-count and checksum assertions for the desk migration (D8).

D8 says the desk's SQLite "migrates with row-count + checksum assertions". The word that
matters is *assertions*: a mismatch here raises `MigrationFailure` and the caller rolls the
transaction back. Nothing in this module warns, logs-and-continues, or returns a flag that a
tired reader can forget to check. If you find yourself wanting one, the answer is that a
partially-migrated evidence table is worse than no migration at all.

TWO ASSERTIONS PER TABLE, AND WHY BOTH
--------------------------------------
* **row_count** — cheap, catches a truncated load, a failed `executemany`, a filtered read.
* **checksum** — a SHA-256 over every cell of every row. Catches what row_count cannot: a
  column silently reordered, a float re-rendered by a different driver, a NULL that became an
  empty string, a `numeric` that lost a digit on the way in.

ONE RENDERING, CALLED FOR BOTH SIDES
------------------------------------
`canonical()` is the whole trick, inherited from `scripts/migrate_to_postgres.py:75` — the one
genuinely good part of the migrator this tool replaces. Both databases' values go through the
*same* function before hashing, so a difference in how psycopg and sqlite3 spell a float cannot
be mistaken for a difference in the data.

THE FLOAT/NUMERIC BRIDGE
------------------------
This tool lands money as Postgres `numeric` (house rule 9) where SQLite held `REAL`. psycopg
returns `numeric` as `decimal.Decimal`. So `canonical()` renders a Decimal as
`repr(float(value))` — exactly what it renders the SQLite float as. That is not a fudge: the
migration writes `Decimal(repr(float_value))`, and `repr()` is the shortest decimal string that
round-trips a float64 exactly, so `float(Decimal(repr(x))) is bit-identical to x`. The checksum
therefore still proves the copy, while the stored type obeys the house rule. See README §"The
numeric decision".
"""
from __future__ import annotations

import datetime as dt
import decimal
import hashlib
from dataclasses import dataclass, field
from typing import Any

#: The byte a row's cells are joined with before hashing. ASCII unit separator: it cannot occur
#: in a JSON payload, a symbol, or an ISO date, so no value can forge a cell boundary.
_CELL = "\x1f"

#: How a SQL NULL renders. Deliberately not "" — an empty TEXT and a NULL are different rows.
_NULL = "\\N"


class MigrationFailure(RuntimeError):
    """Raised the moment an assertion fails. The caller must not catch and continue."""


def canonical(  # noqa: PLR0911 — one return per type is the point; a branchy version
               #                would hide which rule rendered a value
    value: Any,  # noqa: ANN401 — any cell either database returns
) -> str:
    """ONE rendering of a value, called for both databases.

    Order of the isinstance checks matters: `bool` is a subclass of `int`, and
    `datetime.datetime` is a subclass of `datetime.date`.
    """
    if value is None:
        return _NULL
    if isinstance(value, bool):
        # The desk stores booleans as INTEGER 0/1 and this migration keeps them that way
        # (README §"Booleans"); this branch exists only so a driver that decides otherwise
        # cannot silently change the digest.
        return "1" if value else "0"
    if isinstance(value, float):
        # repr() round-trips a float64 exactly and renders identically on both sides.
        return repr(float(value))
    if isinstance(value, decimal.Decimal):
        # A Postgres `numeric` that came from a SQLite REAL. Render it as the float it was.
        return repr(float(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return hashlib.sha256(bytes(value)).hexdigest()
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def row_digest(row: tuple[Any, ...]) -> str:
    return hashlib.sha256(_CELL.join(canonical(v) for v in row).encode()).hexdigest()


def checksum(rows: list[tuple[Any, ...]]) -> str:
    """Order-independent digest: hash each row, sort the hashes, hash the sorted list.

    Sorting digests rather than rows means the comparison does not depend on either database's
    idea of row order — which is not guaranteed, and is not part of the data. It does mean two
    identical rows are indistinguishable from each other, which is correct: they *are* the same
    data. The row *count* assertion is what catches a duplicate.
    """
    per_row = sorted(row_digest(row) for row in rows)
    return hashlib.sha256("\n".join(per_row).encode()).hexdigest()


def row_count(rows: list[tuple[Any, ...]]) -> int:
    """Named, so that `grep row_count` finds every place a count assertion is made."""
    return len(rows)


@dataclass(frozen=True)
class TableVerdict:
    """The evidence for one table. Serialised into the run report verbatim."""

    table: str
    row_count_source: int
    row_count_target: int
    checksum_source: str
    checksum_target: str
    columns: tuple[str, ...] = ()
    note: str = ""

    @property
    def row_count_ok(self) -> bool:
        return self.row_count_source == self.row_count_target

    @property
    def checksum_ok(self) -> bool:
        return self.checksum_source == self.checksum_target

    @property
    def ok(self) -> bool:
        return self.row_count_ok and self.checksum_ok

    def failures(self) -> list[str]:
        out: list[str] = []
        if not self.row_count_ok:
            out.append(
                f"{self.table}: row_count {self.row_count_source} -> {self.row_count_target}"
            )
        if not self.checksum_ok:
            out.append(
                f"{self.table}: checksum {self.checksum_source[:16]}… -> "
                f"{self.checksum_target[:16]}…"
            )
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "row_count_source": self.row_count_source,
            "row_count_target": self.row_count_target,
            "row_count_ok": self.row_count_ok,
            "checksum_source": self.checksum_source,
            "checksum_target": self.checksum_target,
            "checksum_ok": self.checksum_ok,
            "ok": self.ok,
            "note": self.note,
        }


def verdict_for(
    table: str,
    source_rows: list[tuple[Any, ...]],
    target_rows: list[tuple[Any, ...]],
    columns: tuple[str, ...] = (),
    note: str = "",
) -> TableVerdict:
    """Compute both assertions for one table. Does not raise — see `assert_all`."""
    return TableVerdict(
        table=table,
        row_count_source=row_count(source_rows),
        row_count_target=row_count(target_rows),
        checksum_source=checksum(source_rows),
        checksum_target=checksum(target_rows),
        columns=columns,
        note=note,
    )


@dataclass
class AssertionLedger:
    """Every verdict in a run, plus the semantic checks that are not per-table."""

    verdicts: list[TableVerdict] = field(default_factory=list)
    semantic: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, verdict: TableVerdict) -> TableVerdict:
        self.verdicts.append(verdict)
        return verdict

    def add_semantic(self, name: str, passed: bool, detail: str = "") -> None:
        self.semantic.append((name, passed, detail))

    def failures(self) -> list[str]:
        out: list[str] = []
        for v in self.verdicts:
            out.extend(v.failures())
        out.extend(f"semantic check failed: {n} — {d}" for n, ok, d in self.semantic if not ok)
        return out

    def assert_all(self) -> None:
        """The point of the whole module. Raises on the first sign of a bad copy."""
        problems = self.failures()
        if problems:
            raise MigrationFailure(
                "migration assertions failed, nothing was committed:\n  "
                + "\n  ".join(problems)
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "tables": [v.as_dict() for v in self.verdicts],
            "row_count_total_source": sum(v.row_count_source for v in self.verdicts),
            "row_count_total_target": sum(v.row_count_target for v in self.verdicts),
            "semantic": [
                {"check": n, "ok": ok, "detail": d} for n, ok, d in self.semantic
            ],
            "failures": self.failures(),
            "ok": not self.failures(),
        }


def schema_checksum(verdicts: list[TableVerdict]) -> str:
    """One digest over the whole target, for proving a re-run is byte-identical (house rule 7).

    Built from the per-table *target* checksums so that it says something about what landed,
    not about what was read.
    """
    joined = "\n".join(
        f"{v.table}={v.checksum_target}" for v in sorted(verdicts, key=lambda v: v.table)
    )
    return hashlib.sha256(joined.encode()).hexdigest()
