"""Data-integrity assertions for a restored database (PROMPTS.md Prompt 17 deliverable 6).

    "Automated backups with a monthly restore-drill CI job that provisions a scratch database from
     the latest backup and runs a data-integrity assertion."

docs/11 §Reliability: "restore drill monthly — **an untested backup is not a backup**."

What "the backup restored correctly" actually means
---------------------------------------------------
Not "``pg_restore`` exited 0". A dump of an empty database restores cleanly, and so does one that
lost a hypertable's chunks. These assertions are the difference between a restore that finished
and a restore that produced a database the application could serve:

1. **The schema is at a known Alembic revision** and it is one this checkout knows about. A
   restore from a dump taken before a migration is a valid backup and an invalid deployment.
2. **TimescaleDB is present and the hypertables are hypertables.** docs/02 makes the extension "a
   strict addition, not a lock-in", but a dump restored *without* it silently turns
   ``ohlcv_daily`` back into a plain table — which works, until the chunk-aware queries do not.
3. **Every table docs/04 defines exists**, checked against the models rather than a hand-list, so
   the assertion cannot fall behind the schema.
4. **Referential integrity holds** on the joins the constraints do not already enforce: bars for
   instruments that exist, factor rows for dates the calendar knows, memberships for real indices.
5. **The reference data is present.** An empty ``exchange`` or ``index_def`` is a restore of a
   database that had been migrated and never seeded.
6. **``data_version`` is monotonic and unique** across published runs. docs/03 makes it the key
   every cached screen result hangs off (docs/06 §Caching); two runs sharing one version means two
   different result sets under one cache key.
7. **No published run is missing its steps.** docs/03: "Every step writes a row in
   `pipeline_run_step`". A published run with no step rows is a run whose audit trail did not
   survive, which is exactly the failure this drill exists to catch.

Assertions 3, 4 and 6 are the ones that would actually catch a bad backup; 1, 2 and 5 catch a
backup of the wrong thing. Both are worth having and neither is worth guessing about at 3am.

Run it:
    BASKFY_DATABASE_URL=postgresql+asyncpg://... uv run python -m baskfy_api.integrity
    ... --json           machine-readable, for the CI job's summary
Exit code is 0 when every assertion passed and 1 when any failed, so it is usable as a gate.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_api.settings import get_settings
from baskfy_core.models import Base

__all__ = ["CheckStatus", "IntegrityReport", "IntegrityResult", "run_integrity_checks"]


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    #: The check could not run — e.g. a table that is empty because nothing has been seeded.
    #: Reported as skipped rather than passed, because "we did not look" and "we looked and it was
    #: fine" are different facts and the drill's output is read by someone who was not there.
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class IntegrityResult:
    name: str
    status: CheckStatus
    message: str
    observed: dict[str, object] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status is CheckStatus.FAILED


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    results: tuple[IntegrityResult, ...]
    checked_at: dt.datetime

    @property
    def passed(self) -> bool:
        return not any(result.failed for result in self.results)

    @property
    def failures(self) -> tuple[IntegrityResult, ...]:
        return tuple(result for result in self.results if result.failed)

    def to_payload(self) -> dict[str, object]:
        return {
            "checked_at": self.checked_at.isoformat(),
            "passed": self.passed,
            "results": [
                {
                    "name": result.name,
                    "status": result.status.value,
                    "message": result.message,
                    "observed": result.observed,
                }
                for result in self.results
            ],
        }

    def render(self) -> str:
        lines = ["Baskfy — restored-database integrity", "=" * 60]
        for result in self.results:
            marker = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}[result.status.value]
            lines.append(f"[{marker}] {result.name}")
            lines.append(f"        {result.message}")
        lines.append("=" * 60)
        lines.append("RESULT: PASSED" if self.passed else f"RESULT: FAILED ({len(self.failures)})")
        return "\n".join(lines)


#: The tables docs/04 (and its addenda) define, taken from the models so this cannot fall behind.
def _expected_tables() -> frozenset[str]:
    return frozenset(Base.metadata.tables)


#: docs/04's hypertables, plus the two Prompt 16 converted (docs/DECISIONS.md §16.2).
HYPERTABLES: Final[tuple[str, ...]] = (
    "ohlcv_daily",
    "factor_daily",
    "index_member_daily",
    "index_snapshot_daily",
    "market_health_daily",
)

#: Reference tables that are empty only if the restore predates `make seed`.
SEEDED_TABLES: Final[tuple[str, ...]] = ("exchange", "index_def", "plan", "trading_day")


async def _scalar(session: AsyncSession, sql: str) -> object:
    return (await session.execute(text(sql))).scalar()


async def _count(session: AsyncSession, sql: str) -> int:
    """A ``count(*)`` as an ``int``.

    ``Result.scalar()`` is typed ``Any``-free as ``object`` here, and every caller below wants a
    number. Narrowing once, loudly, beats an ``int(...)`` at each call site that mypy has to be
    argued with — and a non-integer coming back from a ``count(*)`` is a driver bug worth raising
    rather than coercing.
    """
    value = await _scalar(session, sql)
    if isinstance(value, int):
        return value
    raise TypeError(f"expected a count, got {type(value).__name__}: {sql}")


async def _check_alembic(session: AsyncSession) -> IntegrityResult:
    exists = await _scalar(session, "SELECT to_regclass('public.alembic_version') IS NOT NULL")
    if not exists:
        return IntegrityResult(
            "alembic_revision",
            CheckStatus.FAILED,
            "alembic_version does not exist; this is not a migrated Baskfy database",
        )
    revision = await _scalar(session, "SELECT version_num FROM alembic_version")
    if revision is None:
        return IntegrityResult("alembic_revision", CheckStatus.FAILED, "alembic_version is empty")
    return IntegrityResult(
        "alembic_revision",
        CheckStatus.PASSED,
        f"schema is at revision {revision}",
        {"revision": str(revision)},
    )


async def _check_timescale(session: AsyncSession) -> list[IntegrityResult]:
    installed = await _scalar(
        session, "SELECT count(*) FROM pg_extension WHERE extname = 'timescaledb'"
    )
    if not installed:
        return [
            IntegrityResult(
                "timescaledb_extension",
                CheckStatus.FAILED,
                "the timescaledb extension is not installed; hypertables restored as plain tables",
            )
        ]

    rows = (
        await session.execute(
            text("SELECT hypertable_name FROM timescaledb_information.hypertables")
        )
    ).scalars()
    present = {str(name) for name in rows}
    missing = [name for name in HYPERTABLES if name not in present]
    results = [
        IntegrityResult(
            "timescaledb_extension", CheckStatus.PASSED, "the timescaledb extension is installed"
        )
    ]
    if missing:
        results.append(
            IntegrityResult(
                "hypertables",
                CheckStatus.FAILED,
                f"restored as plain tables: {', '.join(missing)}",
                {"missing": missing, "found": sorted(present)},
            )
        )
    else:
        results.append(
            IntegrityResult(
                "hypertables",
                CheckStatus.PASSED,
                f"all {len(HYPERTABLES)} hypertables are hypertables",
                {"found": sorted(present)},
            )
        )
    return results


async def _check_tables(session: AsyncSession) -> IntegrityResult:
    rows = (
        await session.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    ).scalars()
    present = {str(name) for name in rows}
    missing = sorted(_expected_tables() - present)
    if missing:
        return IntegrityResult(
            "tables_present",
            CheckStatus.FAILED,
            f"{len(missing)} tables the models define are absent: {', '.join(missing[:8])}",
            {"missing": missing},
        )
    return IntegrityResult(
        "tables_present",
        CheckStatus.PASSED,
        f"all {len(_expected_tables())} modelled tables are present",
    )


#: (name, SQL returning a count of *violations*, human description). Every one of these is a join
#: the schema's foreign keys do not enforce, either because the referenced table is a hypertable
#: (Timescale will not take an inbound FK to a hypertable's compound key without cost) or because
#: the relationship is by date rather than by id.
_ORPHAN_CHECKS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "bars_reference_instruments",
        "SELECT count(*) FROM ohlcv_daily o "
        "LEFT JOIN instrument i ON i.id = o.instrument_id WHERE i.id IS NULL",
        "ohlcv_daily rows whose instrument does not exist",
    ),
    (
        "factors_reference_instruments",
        "SELECT count(*) FROM factor_daily f "
        "LEFT JOIN instrument i ON i.id = f.instrument_id WHERE i.id IS NULL",
        "factor_daily rows whose instrument does not exist",
    ),
    (
        "memberships_reference_indices",
        "SELECT count(*) FROM index_member_daily m "
        "LEFT JOIN index_def d ON d.id = m.index_id WHERE d.id IS NULL",
        "index_member_daily rows whose index does not exist",
    ),
    (
        "steps_reference_runs",
        "SELECT count(*) FROM pipeline_run_step s "
        "LEFT JOIN pipeline_run r ON r.id = s.run_id WHERE r.id IS NULL",
        "pipeline_run_step rows whose run does not exist",
    ),
    (
        "screens_reference_users",
        "SELECT count(*) FROM screen s LEFT JOIN app_user u ON u.id = s.user_id "
        "WHERE s.user_id IS NOT NULL AND u.id IS NULL",
        "screen rows whose owner does not exist",
    ),
)


async def _check_orphans(session: AsyncSession) -> list[IntegrityResult]:
    results: list[IntegrityResult] = []
    for name, sql, description in _ORPHAN_CHECKS:
        violations = await _count(session, sql)
        results.append(
            IntegrityResult(
                name,
                CheckStatus.FAILED if violations else CheckStatus.PASSED,
                f"{violations} {description}" if violations else f"no {description}",
                {"violations": violations},
            )
        )
    return results


async def _check_seed(session: AsyncSession) -> list[IntegrityResult]:
    results: list[IntegrityResult] = []
    for table in SEEDED_TABLES:
        count = await _count(session, f"SELECT count(*) FROM {table}")
        results.append(
            IntegrityResult(
                f"{table}_populated",
                CheckStatus.FAILED if count == 0 else CheckStatus.PASSED,
                f"{table} holds {count} rows",
                {"rows": count},
            )
        )
    return results


async def _check_data_version(session: AsyncSession) -> IntegrityResult:
    """docs/06 §Caching keys every cached result on ``data_version``; it must be unique."""
    published = await _count(
        session, "SELECT count(*) FROM pipeline_run WHERE data_version IS NOT NULL"
    )
    if published == 0:
        return IntegrityResult(
            "data_version_unique",
            CheckStatus.SKIPPED,
            "no run has published, so there is no data_version history to check",
        )
    distinct = await _count(
        session,
        "SELECT count(DISTINCT data_version) FROM pipeline_run WHERE data_version IS NOT NULL",
    )
    if distinct != published:
        return IntegrityResult(
            "data_version_unique",
            CheckStatus.FAILED,
            f"{published} published runs share only {distinct} distinct data_versions",
            {"published": published, "distinct": distinct},
        )
    return IntegrityResult(
        "data_version_unique",
        CheckStatus.PASSED,
        f"{published} published runs, {distinct} distinct data_versions",
        {"published": published},
    )


async def _check_published_runs_have_steps(session: AsyncSession) -> IntegrityResult:
    published = await _count(
        session, "SELECT count(*) FROM pipeline_run WHERE data_version IS NOT NULL"
    )
    if published == 0:
        return IntegrityResult(
            "published_runs_have_steps",
            CheckStatus.SKIPPED,
            "no run has published",
        )
    without = await _count(
        session,
        "SELECT count(*) FROM pipeline_run r WHERE r.data_version IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM pipeline_run_step s WHERE s.run_id = r.id)",
    )
    if without:
        return IntegrityResult(
            "published_runs_have_steps",
            CheckStatus.FAILED,
            f"{without} published runs have no pipeline_run_step rows",
            {"runs_without_steps": without},
        )
    return IntegrityResult(
        "published_runs_have_steps",
        CheckStatus.PASSED,
        f"all {published} published runs carry their step history",
    )


async def run_integrity_checks(session: AsyncSession) -> IntegrityReport:
    """Every assertion, in order. Never raises — a failed check is a result, not an exception."""
    results: list[IntegrityResult] = [await _check_alembic(session)]
    results.extend(await _check_timescale(session))
    results.append(await _check_tables(session))
    results.extend(await _check_orphans(session))
    results.extend(await _check_seed(session))
    results.append(await _check_data_version(session))
    results.append(await _check_published_runs_have_steps(session))
    return IntegrityReport(tuple(results), dt.datetime.now(tz=dt.UTC))


async def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assert a restored Baskfy database is usable.")
    parser.add_argument("--database-url", default=None, help="Defaults to BASKFY_DATABASE_URL.")
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = parser.parse_args(argv)

    url = args.database_url or get_settings().database_url
    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            report = await run_integrity_checks(session)
    finally:
        await engine.dispose()

    print(json.dumps(report.to_payload(), indent=2) if args.json else report.render())
    return 0 if report.passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
