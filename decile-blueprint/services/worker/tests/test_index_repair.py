"""SW16 — ``baskfy_worker.index_repair`` rewrites a corrupt window from what NSE publishes.

The spec: per trading day, ascending, the window is re-fetched through the provider and rewritten
with an upsert; the writer's sanity guard is in the path, so a value that fails it is refused and
named, never written; a day the provider cannot serve is recorded and the rest of the window
still runs; a second run over the same window rewrites identical rows (house rule 7); the report
prints per-day per-slug before/after and the anchor day one is judged against.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from baskfy_core.models import IndexDef, IndexSnapshotDaily
from baskfy_providers.errors import UnexpectedPayload
from baskfy_providers.records import IndexSnapshot
from baskfy_worker.index_repair import format_report, repair_index_snapshots
from baskfy_worker.tasks.snapshots import store_snapshots
from baskfy_worker.window import DateWindow

#: The corrupt window on the box began the day after Kite's last real row.
ANCHOR = dt.date(2026, 7, 7)
WINDOW = DateWindow(dt.date(2026, 7, 8), dt.date(2026, 7, 10))

#: Kite's real levels for 2026-07-07 (what the box holds) and NSE's for the three days after —
#: the shape of the real repair, at test size.
REAL: dict[dt.date, dict[str, str]] = {
    ANCHOR: {"nifty-50": "24398.70", "nifty-500": "23368.85"},
    dt.date(2026, 7, 8): {"nifty-50": "24476.10", "nifty-500": "23440.25"},
    dt.date(2026, 7, 9): {"nifty-50": "24512.40", "nifty-500": "23490.90"},
    dt.date(2026, 7, 10): {"nifty-50": "24388.15", "nifty-500": "23401.05"},
}
#: The fixture builder's random walk, as the box held it.
FIXTURE: dict[dt.date, dict[str, str]] = {
    dt.date(2026, 7, 8): {"nifty-50": "1004.6026", "nifty-500": "1576.9012"},
    dt.date(2026, 7, 9): {"nifty-50": "1022.5254", "nifty-500": "1588.8075"},
    dt.date(2026, 7, 10): {"nifty-50": "1014.1944", "nifty-500": "1606.5549"},
}


class NSEStub:
    """Serves ``levels`` by day, the way the archived provider path does; fails where told."""

    def __init__(
        self,
        levels: dict[dt.date, dict[str, str]],
        *,
        failing: frozenset[dt.date] = frozenset(),
    ) -> None:
        self._levels = levels
        self._failing = failing
        self.asked: list[dt.date] = []

    def index_snapshots(self, on: dt.date) -> list[IndexSnapshot]:
        self.asked.append(on)
        if on in self._failing:
            raise UnexpectedPayload(
                f"GET ind_close_all_{on:%d%m%Y}.csv returned 404", provider="nse"
            )
        return [
            # Published under NSE's names, mixed case, as the real file does.
            IndexSnapshot(
                index_slug={"nifty-50": "Nifty 50", "nifty-500": "Nifty 500"}[slug],
                date=on,
                level=Decimal(level),
                pe=Decimal("20.37"),
            )
            for slug, level in self._levels.get(on, {}).items()
        ]


async def _seed_corrupt_window(engine: AsyncEngine) -> None:
    """The box's shape: a real anchor day, then the fixture walk written straight into the table."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        result = await store_snapshots(session, ANCHOR, _rows(ANCHOR, REAL[ANCHOR]))
        assert result.refused == []
    # The fixture rows went in through the seeder, which had no guard; write them the same way.
    async with maker() as session, session.begin():
        ids = dict((await session.execute(select(IndexDef.slug, IndexDef.id))).tuples().all())
        for day, levels in FIXTURE.items():
            for slug, level in levels.items():
                session.add(
                    IndexSnapshotDaily(
                        index_id=ids[slug], date=day, level=Decimal(level), pe=Decimal("20.0")
                    )
                )


def _rows(on: dt.date, levels: dict[str, str]) -> list[IndexSnapshot]:
    return [IndexSnapshot(index_slug=slug, date=on, level=Decimal(v)) for slug, v in levels.items()]


async def _levels(engine: AsyncEngine, slug: str) -> dict[dt.date, Decimal]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        rows = await session.execute(
            select(IndexSnapshotDaily.date, IndexSnapshotDaily.level)
            .join(IndexDef, IndexDef.id == IndexSnapshotDaily.index_id)
            .where(IndexDef.slug == slug)
            .order_by(IndexSnapshotDaily.date)
        )
        return {day: Decimal(level) for day, level in rows.tuples() if level is not None}


class TestRepairIndexSnapshots:
    async def test_the_corrupt_window_is_rewritten_from_the_provider(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        del clean_db
        await _seed_corrupt_window(engine)
        assert (await _levels(engine, "nifty-50"))[dt.date(2026, 7, 8)] == Decimal("1004.6026")

        report = await repair_index_snapshots(NSEStub(REAL), WINDOW, database_url=migrated_url)

        assert report.succeeded
        assert report.trading_days == 3
        assert report.days_done == 3
        assert report.refused == []
        levels = await _levels(engine, "nifty-50")
        assert levels[dt.date(2026, 7, 8)] == Decimal("24476.10")
        assert levels[dt.date(2026, 7, 10)] == Decimal("24388.15")
        # The anchor day is outside the window and untouched.
        assert levels[ANCHOR] == Decimal("24398.70")
        assert (await _levels(engine, "nifty-500"))[dt.date(2026, 7, 9)] == Decimal("23490.90")

    async def test_the_window_is_walked_ascending_over_trading_days_only(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        """Each repaired day is the next day's anchor, so order is part of the contract."""
        del clean_db
        await _seed_corrupt_window(engine)
        stub = NSEStub(REAL)
        window = DateWindow(dt.date(2026, 7, 8), dt.date(2026, 7, 12))  # spans a weekend
        await repair_index_snapshots(stub, window, database_url=migrated_url)
        assert stub.asked == [dt.date(2026, 7, 8), dt.date(2026, 7, 9), dt.date(2026, 7, 10)]

    async def test_a_value_that_fails_the_sanity_check_is_refused_not_written(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        """The guard is in the repair's path: a provider serving the fixture walk writes nothing."""
        del clean_db
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session, session.begin():
            await store_snapshots(session, ANCHOR, _rows(ANCHOR, REAL[ANCHOR]))

        report = await repair_index_snapshots(NSEStub(FIXTURE), WINDOW, database_url=migrated_url)

        assert report.days_done == 3
        assert report.rows_written == 0
        assert len(report.refused) == 6
        assert report.refused[0].startswith("nifty-50 2026-07-08: level 1004.6026 is -95.9%")
        assert await _levels(engine, "nifty-50") == {ANCHOR: Decimal("24398.70")}

    async def test_a_day_the_provider_cannot_serve_is_recorded_and_the_rest_still_runs(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        del clean_db
        await _seed_corrupt_window(engine)
        stub = NSEStub(REAL, failing=frozenset({dt.date(2026, 7, 9)}))

        report = await repair_index_snapshots(stub, WINDOW, database_url=migrated_url)

        assert not report.succeeded
        assert list(report.failures) == ["2026-07-09"]
        assert "404" in report.failures["2026-07-09"]
        levels = await _levels(engine, "nifty-50")
        assert levels[dt.date(2026, 7, 8)] == Decimal("24476.10")
        assert levels[dt.date(2026, 7, 9)] == Decimal("1022.5254")  # untouched, and named
        # The day after a failed day is judged against the row the failure left in place, so the
        # correct value is refused rather than written next to a wrong anchor. The report names
        # it; the operator re-runs from the failed day once NSE serves it.
        assert levels[dt.date(2026, 7, 10)] == Decimal("1014.1944")
        assert any(r.startswith("nifty-50 2026-07-10: level 24388.15") for r in report.refused)

    async def test_a_second_run_rewrites_identical_rows(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        """House rule 7: the repair is an upsert, and running it twice is the same as once."""
        del clean_db
        await _seed_corrupt_window(engine)
        await repair_index_snapshots(NSEStub(REAL), WINDOW, database_url=migrated_url)
        first = await _levels(engine, "nifty-50"), await _levels(engine, "nifty-500")

        second_report = await repair_index_snapshots(
            NSEStub(REAL), WINDOW, database_url=migrated_url
        )

        assert second_report.succeeded
        assert second_report.rewritten() == 0
        assert (await _levels(engine, "nifty-50"), await _levels(engine, "nifty-500")) == first

    async def test_slugs_narrow_what_is_written(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        del clean_db
        await _seed_corrupt_window(engine)
        await repair_index_snapshots(
            NSEStub(REAL), WINDOW, database_url=migrated_url, slugs=["nifty-500"]
        )
        assert (await _levels(engine, "nifty-500"))[dt.date(2026, 7, 8)] == Decimal("23440.25")
        assert (await _levels(engine, "nifty-50"))[dt.date(2026, 7, 8)] == Decimal("1004.6026")

    async def test_a_dry_run_writes_nothing_and_still_reports(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        del clean_db
        await _seed_corrupt_window(engine)
        report = await repair_index_snapshots(
            NSEStub(REAL), WINDOW, database_url=migrated_url, dry_run=True
        )
        # Day one is judged against the real anchor and reported as a change; nothing lands, so
        # days two and three are judged against the still-corrupt rows and refused. A dry run
        # therefore proves the first day and the guard, not the whole window.
        assert report.rewritten() == 2
        assert "2026-07-08 nifty-50: 1004.6026 -> 24476.1000" in format_report(report)
        assert len(report.refused) == 4
        assert (await _levels(engine, "nifty-50"))[dt.date(2026, 7, 8)] == Decimal("1004.6026")

    async def test_the_report_prints_before_after_per_day_per_slug_and_the_anchor(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        del clean_db
        await _seed_corrupt_window(engine)
        report = await repair_index_snapshots(NSEStub(REAL), WINDOW, database_url=migrated_url)
        text = format_report(report)
        assert "anchor nifty-50: 24398.7000 on 2026-07-07" in text
        assert "2026-07-08 nifty-50: 1004.6026 -> 24476.1000" in text
        assert "2026-07-10 nifty-500: 1606.5549 -> 23401.0500" in text
        assert "3 trading day(s)" in text
        assert "6 reported level(s) changed" in text
