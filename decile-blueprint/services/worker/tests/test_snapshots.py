"""SW16 — the index snapshot writer's sanity guard, against the rows that got past it.

docs/swing/STATUS.md (SW13, deploy #2): the first real swing scan read NIFTY 500 at 1,696.57 on
2026-08-18 and 23,386 on 2026-08-19, NIFTY 50 at 1,004.60 on 2026-07-08, and computed the market
gate's index averages from that. Those rows were the fixture builder's synthetic random walk
(``tests/fixtures/providers/index_snapshots.parquet``), not anything NSE published, and nothing
in the writer noticed a level falling 96 % overnight.

The spec the tests assert: a level more than 40 % away from the last stored level for the same
index is refused and named; the sane rows of the same file are still written; a first-ever row is
accepted; a re-run writes identical rows (house rule 7).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import IndexDef, IndexSnapshotDaily
from baskfy_providers.fixtures import FixtureProvider
from baskfy_providers.records import IndexSnapshot
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.snapshots import (
    MAX_DAY_ON_DAY_CHANGE,
    run_refresh_index_snapshots,
    store_snapshots,
)

#: What the box held for NIFTY 50 / NIFTY 500 on 2026-07-07: Kite's levels (M31), real.
REAL_DAY = dt.date(2026, 7, 7)
#: The first fixture day on the box, and its recorded values (identical to the parquet's).
FIXTURE_DAY = dt.date(2026, 7, 8)
FIXTURE_NIFTY_50 = Decimal("1004.6026")
FIXTURE_NIFTY_500 = Decimal("1576.9012")


def _row(slug: str, level: str, on: dt.date, **fundamentals: Decimal) -> IndexSnapshot:
    return IndexSnapshot(index_slug=slug, date=on, level=Decimal(level), **fundamentals)


async def _level(session: AsyncSession, slug: str, on: dt.date) -> Decimal | None:
    value = (
        await session.execute(
            select(IndexSnapshotDaily.level)
            .join(IndexDef, IndexDef.id == IndexSnapshotDaily.index_id)
            .where(IndexDef.slug == slug, IndexSnapshotDaily.date == on)
        )
    ).scalar_one_or_none()
    return None if value is None else Decimal(value)


async def _seed_real_day(session: AsyncSession) -> None:
    result = await store_snapshots(
        session,
        REAL_DAY,
        [_row("nifty-50", "24398.70", REAL_DAY), _row("nifty-500", "23368.85", REAL_DAY)],
    )
    assert result.refused == []


class TestTheRecordedFixtureIsWhatTheBoxHeld:
    """The cause, named from the fixture that produced it."""

    def test_the_fixture_publishes_the_exact_levels_found_on_the_box(self) -> None:
        """``FixtureProvider.index_snapshots(2026-07-08)`` is the row the box held.

        ``fixture_builder._index_snapshots`` walks ``1000 + position * 137.5`` for thirty
        trading days ending ``FIXTURE_AS_OF``; NIFTY 50 is position 0, NIFTY 500 position 4.
        The box's ``nifty-50 2026-07-08 = 1004.6026`` and ``nifty-500 = 1576.9012`` are these.
        """
        by_slug = {s.index_slug: s for s in FixtureProvider().index_snapshots(FIXTURE_DAY)}
        assert by_slug["nifty-50"].level == FIXTURE_NIFTY_50
        assert by_slug["nifty-500"].level == FIXTURE_NIFTY_500
        # The constant fundamentals the box carried on every fixture day (20 + 0.1 * position).
        assert by_slug["nifty-50"].pe == Decimal("20.0")
        assert by_slug["nifty-500"].pe == Decimal("20.4")

    async def test_the_writer_refuses_the_fixture_rows_next_to_a_real_day(
        self, session: AsyncSession
    ) -> None:
        """Feed the writer the recorded fixture for 2026-07-08 after a real 2026-07-07."""
        await _seed_real_day(session)
        fixture = FixtureProvider().index_snapshots(FIXTURE_DAY)

        result = await store_snapshots(session, FIXTURE_DAY, fixture)

        refused = "\n".join(result.refused)
        assert f"nifty-50 {FIXTURE_DAY.isoformat()}: level {FIXTURE_NIFTY_50}" in refused
        assert f"nifty-500 {FIXTURE_DAY.isoformat()}: level {FIXTURE_NIFTY_500}" in refused
        assert "against 24398.7000 on 2026-07-07" in refused
        assert await _level(session, "nifty-50", FIXTURE_DAY) is None
        assert await _level(session, "nifty-500", FIXTURE_DAY) is None
        # Indices with no stored neighbour are first-ever rows: accepted, as the spec says.
        assert result.rows_written == len(fixture) - 2


class TestSanityGuard:
    async def test_a_move_over_forty_percent_is_refused_and_named(
        self, session: AsyncSession
    ) -> None:
        await _seed_real_day(session)
        result = await store_snapshots(
            session, FIXTURE_DAY, [_row("nifty-50", "1004.6026", FIXTURE_DAY)]
        )
        assert result.rows_written == 0
        assert len(result.refused) == 1
        assert result.refused[0].startswith("nifty-50 2026-07-08: level 1004.6026 is -95.9%")
        assert await _level(session, "nifty-50", FIXTURE_DAY) is None

    async def test_the_sane_rows_of_the_same_file_are_still_written(
        self, session: AsyncSession
    ) -> None:
        await _seed_real_day(session)
        result = await store_snapshots(
            session,
            FIXTURE_DAY,
            [
                _row("nifty-50", "1004.6026", FIXTURE_DAY),
                _row("nifty-500", "23500.10", FIXTURE_DAY),
            ],
        )
        assert result.rows_written == 1
        assert [r.split(" ")[0] for r in result.refused] == ["nifty-50"]
        assert await _level(session, "nifty-500", FIXTURE_DAY) == Decimal("23500.10")

    async def test_a_move_at_the_threshold_is_accepted(self, session: AsyncSession) -> None:
        """40 % exactly is inside the bound; the guard is for 96 %, not for a bad session."""
        await _seed_real_day(session)
        level = (Decimal("24398.70") * (1 - MAX_DAY_ON_DAY_CHANGE)).quantize(Decimal("0.0001"))
        result = await store_snapshots(
            session, FIXTURE_DAY, [_row("nifty-50", str(level), FIXTURE_DAY)]
        )
        assert result.refused == []
        assert await _level(session, "nifty-50", FIXTURE_DAY) == level

    async def test_a_first_ever_row_is_accepted(self, session: AsyncSession) -> None:
        result = await store_snapshots(
            session, FIXTURE_DAY, [_row("nifty-50", "1004.6026", FIXTURE_DAY)]
        )
        assert result.refused == []
        assert result.rows_written == 1

    async def test_the_anchor_is_the_last_stored_row_across_a_gap(
        self, session: AsyncSession
    ) -> None:
        """A long weekend does not blind the guard: the anchor is the last row within 14 days."""
        await _seed_real_day(session)
        after_gap = REAL_DAY + dt.timedelta(days=6)
        result = await store_snapshots(session, after_gap, [_row("nifty-50", "1004.60", after_gap)])
        assert len(result.refused) == 1

    async def test_rewriting_a_day_with_the_same_values_is_idempotent(
        self, session: AsyncSession
    ) -> None:
        """House rule 7, with the guard in the path: the second run changes nothing."""
        await _seed_real_day(session)
        rows = [_row("nifty-50", "24500.35", FIXTURE_DAY, pe=Decimal("22.4"))]
        first = await store_snapshots(session, FIXTURE_DAY, rows)
        second = await store_snapshots(session, FIXTURE_DAY, rows)
        assert (first.rows_written, first.refused) == (1, [])
        assert (second.rows_written, second.refused) == (1, [])
        assert await _level(session, "nifty-50", FIXTURE_DAY) == Decimal("24500.35")

    async def test_the_step_notes_every_refusal(self, session: AsyncSession) -> None:
        """The nightly step must be loud about a refused row, not merely short one."""
        await _seed_real_day(session)

        class Provider:
            def index_snapshots(self, on: dt.date) -> list[IndexSnapshot]:
                return [_row("nifty-50", "1004.6026", on), _row("nifty-500", "23500.10", on)]

        outcome = StepOutcome()
        written = await run_refresh_index_snapshots(session, Provider(), outcome, FIXTURE_DAY)
        assert written == 1
        assert outcome.rows_in == 2
        refused = outcome.detail["refused"]
        assert isinstance(refused, list)
        assert len(refused) == 1
        assert refused[0].startswith("nifty-50 2026-07-08")
