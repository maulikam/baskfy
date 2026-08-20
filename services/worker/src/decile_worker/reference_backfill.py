"""Backfill the reference data over a historical range (Prompt 4 deliverable 5).

    python -m decile_worker.reference_backfill --from 2018-01-01 --to 2026-08-18

docs/09 §Backfill fixes the ordering — "instruments -> corporate actions -> bars ... -> index
membership ... -> factors ... -> market health" — and this covers the reference half of it:
listings once, then membership, snapshots and market health per trading day.

Why per trading day, and only trading days
------------------------------------------
Prompt 3 deliverable 7: "never attempt to ingest or compute for a non-trading day". Membership and
snapshots are point-in-time facts about a session; writing a row for a Sunday would put a date in
``index_member_daily`` that no screen can ever legitimately resolve, and would quietly inflate
every "rows per universe" count an operator uses to sanity-check a backfill.

Ordering within a day matters too: market health reads ``index_member_daily`` joined to
``factor_daily``, so membership must land first. The loop enforces that rather than trusting a
caller to.

Resumability
------------
Each day commits in its own transaction, and every write is an upsert, so an interrupted run is
resumed simply by running it again over the same range — the days already done are rewritten to
identical values. That is cheaper than a cursor table here because a day of reference data is
seconds of work, unlike the multi-year bar chunks ``decile_worker.backfill`` manages.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from decile_core.models import TradingDay
from decile_core.seed_data import NSE_EXCHANGE_ID
from decile_worker.db import session_scope
from decile_worker.providers import build_pipeline_dependencies
from decile_worker.steps import StepOutcome
from decile_worker.tasks.listings import run_refresh_listings
from decile_worker.tasks.market_health import run_compute_market_health
from decile_worker.tasks.membership import run_refresh_index_membership
from decile_worker.tasks.snapshots import run_refresh_index_snapshots
from decile_worker.window import DateWindow


@dataclass(slots=True)
class ReferenceBackfillReport:
    trading_days: int = 0
    listings_written: int = 0
    membership_rows: int = 0
    snapshot_rows: int = 0
    market_health_rows: int = 0
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return not self.failures


async def trading_days_in(session: AsyncSession, window: DateWindow) -> list[dt.date]:
    rows = await session.execute(
        select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date >= window.start,
            TradingDay.date <= window.end,
        )
        .order_by(TradingDay.date)
    )
    return [row[0] for row in rows]


async def backfill_reference_data(
    provider: object,
    window: DateWindow,
    *,
    database_url: str | None = None,
    include_listings: bool = True,
    allow_reconstruction: bool = True,
) -> ReferenceBackfillReport:
    """Run listings once, then membership + snapshots + market health for each trading day."""
    report = ReferenceBackfillReport()

    if include_listings:
        # Once, not per day: the register is a current-state file with no historical variants.
        async with session_scope(database_url) as session:
            outcome = StepOutcome()
            report.listings_written = await run_refresh_listings(session, provider, outcome)

    async with session_scope(database_url) as session:
        days = await trading_days_in(session, window)
    report.trading_days = len(days)

    for day in days:
        try:
            async with session_scope(database_url) as session:
                report.membership_rows += await run_refresh_index_membership(
                    session,
                    provider,
                    StepOutcome(),
                    day,
                    allow_reconstruction=allow_reconstruction,
                )
                report.snapshot_rows += await run_refresh_index_snapshots(
                    session, provider, StepOutcome(), day
                )
                report.market_health_rows += await run_compute_market_health(
                    session, StepOutcome(), day
                )
        except Exception as exc:
            # Recorded per day rather than raised: a fifteen-year backfill that aborts on one
            # malformed NSE file has to be restarted from the beginning, and docs/09 already
            # gives the quality gate the job of deciding what is publishable.
            report.failures[day.isoformat()] = f"{type(exc).__name__}: {exc}"

    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m decile_worker.reference_backfill",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--from", dest="start", required=True, help="ISO start date")
    parser.add_argument("--to", dest="end", default=None, help="ISO end date (default: today)")
    parser.add_argument(
        "--skip-listings", action="store_true", help="do not re-read the NSE listings register"
    )
    parser.add_argument(
        "--no-reconstruct",
        action="store_true",
        help=(
            "leave membership empty where NSE publishes no constituent file, instead of "
            "carrying it back from the earliest available one (docs/09 §Backfill)"
        ),
    )
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    window = DateWindow(
        dt.date.fromisoformat(args.start),
        dt.date.fromisoformat(args.end) if args.end else dt.date.today(),
    )

    provider = build_pipeline_dependencies().provider
    report = asyncio.run(
        backfill_reference_data(
            provider,
            window,
            database_url=args.database_url,
            include_listings=not args.skip_listings,
            allow_reconstruction=not args.no_reconstruct,
        )
    )
    print(
        f"{report.trading_days} trading day(s) in {window}; "
        f"listings {report.listings_written}; membership {report.membership_rows}; "
        f"snapshots {report.snapshot_rows}; market health {report.market_health_rows}"
    )
    for day, error in list(report.failures.items())[:20]:
        print(f"  {day}: {error}")
    return 0 if report.succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
