"""Re-fetch a window of ``index_snapshot_daily`` from NSE and rewrite it (SW16).

    python -m baskfy_worker.index_repair --from 2026-07-01 --to 2026-09-02
    python -m baskfy_worker.index_repair --from 2026-07-01 --to 2026-09-02 \\
        --slugs nifty-50,nifty-500
    make index-repair FROM=2026-07-01 TO=2026-09-02

What it repairs
---------------
The first real swing scan (docs/swing/STATUS.md, SW13 deploy #2) read NIFTY 500 at 1,696.57 on
2026-08-18 and 23,386 on 2026-08-19, with no rows at all for 24-26 Aug, and computed the market
gate's index averages from that. The 1/14-scale rows were never NSE's: they are the fixture
builder's synthetic random walk (``baskfy_providers.fixture_builder._index_snapshots``), thirty
trading days ending ``FIXTURE_AS_OF`` = 2026-08-18, written to the development database by
``baskfy_api.seed`` and copied to the box with the market tables. The Kite backfill (M31) was
``ON CONFLICT DO NOTHING`` when it ran and left them in place; the NSE ingest first wrote on
27 Aug, once its archive path was writable (M56). The missing days are the gap between the two.

How it repairs
--------------
Per trading day in the window, ascending: fetch ``ind_close_all_DDMMYYYY.csv`` through the
**NSE provider directly** — never the composite, whose fallback is the very ``FixtureProvider``
that produced the bad rows — under its own rate limiter and archive-then-parse discipline (a day
already archived is re-parsed from the archive, not re-fetched: docs/09). Then upsert through the
same :func:`baskfy_worker.tasks.snapshots.store_snapshots` the nightly chain uses, so the sanity
guard applies: a level more than 40 % away from the last stored one is refused and named, never
written. Ascending order matters for that guard — each repaired day becomes the anchor for the
next — so start the window at a day whose predecessor is sane; the report prints the anchor.

Each day commits on its own, every write is an upsert, and a day that fails (a 404 on a session
NSE has no file for, a malformed file) is recorded and skipped rather than aborting the run
(house rule 7: re-running the same window rewrites identical rows).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import IndexDef, IndexSnapshotDaily
from baskfy_core.universes import slugify_index
from baskfy_providers.errors import ProviderError
from baskfy_providers.factory import build_nse_provider
from baskfy_providers.nse import INDEX_SLUG_TO_NSE_NAME
from baskfy_providers.records import IndexSnapshot
from baskfy_providers.settings import get_provider_settings
from baskfy_worker.db import session_scope
from baskfy_worker.reference_backfill import trading_days_in
from baskfy_worker.tasks.snapshots import GUARD_LOOKBACK_DAYS, SnapshotResult, store_snapshots
from baskfy_worker.telemetry import provider_retry_hooks
from baskfy_worker.window import DateWindow

#: The slugs whose before/after lines are printed when ``--slugs`` does not narrow the run: the
#: universes NSE publishes by the names we map, which are the ones a screen or the swing gate
#: reads. Every other index is still rewritten, and counted.
REPORTED_SLUGS: Final[tuple[str, ...]] = tuple(INDEX_SLUG_TO_NSE_NAME)


class _DryRunRollback(Exception):
    """Raised inside the day's transaction so `session_scope` rolls it back; carries the result."""

    def __init__(
        self,
        result: SnapshotResult,
        before: dict[str, Decimal | None],
        after: dict[str, Decimal | None],
    ) -> None:
        super().__init__("dry run")
        self.result = result
        self.before = before
        self.after = after


class SnapshotSource(Protocol):
    """The one provider method the repair needs. NSE in production, a stub in tests."""

    def index_snapshots(self, on: dt.date) -> list[IndexSnapshot]: ...


@dataclass(slots=True)
class DayChange:
    day: dt.date
    slug: str
    before: Decimal | None
    after: Decimal | None

    def line(self) -> str:
        mark = "=" if self.before == self.after else "->"
        return f"{self.day.isoformat()} {self.slug}: {self.before} {mark} {self.after}"


@dataclass(slots=True)
class RepairReport:
    window: DateWindow
    trading_days: int = 0
    days_done: int = 0
    rows_written: int = 0
    changes: list[DayChange] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    anchors: dict[str, str] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return not self.failures and self.trading_days > 0

    def rewritten(self) -> int:
        return sum(1 for change in self.changes if change.before != change.after)


async def repair_index_snapshots(
    provider: SnapshotSource,
    window: DateWindow,
    *,
    database_url: str | None = None,
    slugs: Sequence[str] | None = None,
    dry_run: bool = False,
) -> RepairReport:
    """Re-fetch every trading day in ``window`` and rewrite the rows, ascending, one commit each."""
    report = RepairReport(window=window)
    wanted = frozenset(slugs) if slugs else None
    reported = wanted if wanted is not None else frozenset(REPORTED_SLUGS)

    async with session_scope(database_url) as session:
        days = await trading_days_in(session, window)
        report.anchors = await _anchors(session, window.start, reported)
    report.trading_days = len(days)

    for day in days:
        try:
            snapshots = _select(provider.index_snapshots(day), wanted)
        except ProviderError as exc:
            report.failures[day.isoformat()] = f"{type(exc).__name__}: {exc}"
            continue
        if not snapshots:
            report.failures[day.isoformat()] = "NSE published no row for the requested slugs"
            continue
        try:
            async with session_scope(database_url) as session:
                before = await _levels(session, day, reported)
                result = await store_snapshots(session, day, snapshots)
                after = await _levels(session, day, reported)
                if dry_run:
                    # `session_scope` rolls back on any exception; this one carries the answer.
                    raise _DryRunRollback(result, before, after)
        except _DryRunRollback as rolled:
            result, before, after = rolled.result, rolled.before, rolled.after
        except Exception as exc:
            # Per day, like the reference backfill: one malformed file must not abort the window.
            report.failures[day.isoformat()] = f"{type(exc).__name__}: {exc}"
            continue
        report.days_done += 1
        report.rows_written += result.rows_written
        report.refused.extend(result.refused)
        for slug in sorted(reported):
            if slug in before or slug in after:
                report.changes.append(DayChange(day, slug, before.get(slug), after.get(slug)))
    return report


def _select(snapshots: list[IndexSnapshot], wanted: frozenset[str] | None) -> list[IndexSnapshot]:
    if wanted is None:
        return snapshots
    return [
        snapshot
        for snapshot in snapshots
        if snapshot.index_slug in wanted or slugify_index(snapshot.index_slug) in wanted
    ]


async def _levels(
    session: AsyncSession, day: dt.date, slugs: frozenset[str]
) -> dict[str, Decimal | None]:
    rows = await session.execute(
        select(IndexDef.slug, IndexSnapshotDaily.level)
        .join(IndexDef, IndexDef.id == IndexSnapshotDaily.index_id)
        .where(IndexSnapshotDaily.date == day, IndexDef.slug.in_(sorted(slugs)))
    )
    return {str(slug): (None if level is None else Decimal(level)) for slug, level in rows.tuples()}


async def _anchors(session: AsyncSession, start: dt.date, slugs: frozenset[str]) -> dict[str, str]:
    """The last stored level before the window, per reported slug — what day one is judged against.

    Printed so an operator can see when a window starts inside the corruption it is meant to
    repair: the guard would then refuse the correct value against a wrong anchor.
    """
    rows = await session.execute(
        select(IndexDef.slug, IndexSnapshotDaily.date, IndexSnapshotDaily.level)
        .join(IndexDef, IndexDef.id == IndexSnapshotDaily.index_id)
        .where(
            IndexDef.slug.in_(sorted(slugs)),
            IndexSnapshotDaily.date < start,
            IndexSnapshotDaily.date >= start - dt.timedelta(days=GUARD_LOOKBACK_DAYS),
            IndexSnapshotDaily.level.is_not(None),
        )
        .order_by(IndexDef.slug, IndexSnapshotDaily.date)
    )
    anchors: dict[str, str] = {}
    for slug, day, level in rows.tuples():
        if level is not None:
            anchors[str(slug)] = f"{Decimal(level)} on {day.isoformat()}"
    return anchors


def format_report(report: RepairReport) -> str:
    lines = [
        f"{report.trading_days} trading day(s) in {report.window}; "
        f"{report.days_done} repaired; {report.rows_written} row(s) written; "
        f"{report.rewritten()} reported level(s) changed; {len(report.refused)} refused; "
        f"{len(report.failures)} day(s) failed",
        *(f"anchor {slug}: {anchor}" for slug, anchor in sorted(report.anchors.items())),
        *(change.line() for change in report.changes),
        *(f"REFUSED {message}" for message in report.refused),
        *(f"FAILED {day}: {error}" for day, error in report.failures.items()),
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m baskfy_worker.index_repair",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--from", dest="start", required=True, help="ISO start date")
    parser.add_argument("--to", dest="end", required=True, help="ISO end date")
    parser.add_argument(
        "--slugs",
        default=None,
        help="comma-separated index_def slugs to rewrite (default: every index NSE publishes)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="fetch, archive and compare; write nothing"
    )
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    window = DateWindow(dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end))
    slugs = [s.strip() for s in args.slugs.split(",") if s.strip()] if args.slugs else None
    # NSE directly. The composite's fallback for this capability is the FixtureProvider, which is
    # how the rows being repaired came to exist in the first place.
    provider = build_nse_provider(get_provider_settings(), retry_hooks=provider_retry_hooks())
    report = asyncio.run(
        repair_index_snapshots(
            provider,
            window,
            database_url=args.database_url,
            slugs=slugs,
            dry_run=args.dry_run,
        )
    )
    print(format_report(report))
    return 0 if report.succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
