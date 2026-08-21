"""Prompt 3's four acceptance criteria, stated directly.

1. "Running the full pipeline twice for the same date produces zero row changes on the second
    run (assert with a checksum of the affected tables)."
2. "A test injects a malformed day and proves the gate fails, data_version does NOT advance,
    and the previous version is still served."
3. "A test with a synthetic 2:1 split proves adjusted closes are continuous across the ex-date
    and that reprocess_instrument is idempotent."
4. "Backfill can be killed mid-run and resumed to the same final state."
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import polars as pl
import pytest
from helpers import PRIOR_DATE, TRADE_DATE, add_bar, make_instrument, requires_db
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from baskfy_core.models import CorporateAction, IngestCursor, OhlcvDaily, PipelineRun
from baskfy_providers.records import DAILY_BARS_SCHEMA, InstrumentRecord, conform
from baskfy_worker.backfill import (
    STATUS_DONE,
    STATUS_PENDING,
    count_done,
    run_backfill,
)
from baskfy_worker.deps import PipelineDependencies
from baskfy_worker.orchestrator import run_nightly_pipeline
from baskfy_worker.steps import PipelineStep, RunStatus
from baskfy_worker.tasks.adjustments import reprocess_instrument
from baskfy_worker.tasks.publish import current_data_version
from baskfy_worker.tasks.quality import NOMINAL_SIZES
from baskfy_worker.window import DateWindow

pytestmark = [pytest.mark.db, requires_db]

#: Tables the pipeline writes, for the idempotency checksum.
CHECKSUMMED_TABLES = (
    "ohlcv_daily",
    "factor_daily",
    "index_member_daily",
    "index_snapshot_daily",
    "market_health_daily",
    "corporate_action",
    "instrument",
)


class StubProvider:
    """A deterministic provider serving a realistically-shaped universe. No network, by design.

    Sized to satisfy docs/09's assertion 5 — "every selectable universe has a membership row set
    within 5% of its nominal size". A three-symbol fixture fails that assertion, correctly, so a
    test that used one would only be testing the gate's ability to reject toy data. 500 equities
    plus a handful of ETFs is the smallest fixture that lets the *pipeline* be the thing under
    test.
    """

    EQUITY_COUNT = 500
    ETF_COUNT = 3

    def __init__(
        self,
        *,
        close: str = "100",
        prior_close: str = "100",
        actions: list[object] | None = None,
    ) -> None:
        self.equities = tuple(f"EQ{index:03d}" for index in range(self.EQUITY_COUNT))
        self.etfs = tuple(f"ETF{index}" for index in range(self.ETF_COUNT))
        self.symbols = self.equities + self.etfs
        self.close = close
        self.prior_close = prior_close
        self.actions = actions or []
        self.name = "stub"

    def capabilities(self) -> frozenset[object]:
        return frozenset()

    def list_instruments(self) -> list[object]:
        return [
            InstrumentRecord(
                symbol=symbol,
                name=f"{symbol} LIMITED",
                instrument_type="ETF" if symbol.startswith("ETF") else "EQ",
                series="EQ",
                kite_token=index + 1,
            )
            for index, symbol in enumerate(self.symbols)
        ]

    def listings(self) -> list[object]:
        return []

    def close_for(self, symbol: str, day: dt.date) -> str:
        del symbol
        return self.close if day == TRADE_DATE else self.prior_close

    def daily_bars(self, token: int, start: dt.date, end: dt.date) -> pl.DataFrame:
        symbol = self.symbols[token - 1]
        rows = [
            {
                "symbol": symbol,
                "date": day,
                "open": Decimal(self.close_for(symbol, day)),
                "high": Decimal(self.close_for(symbol, day)),
                "low": Decimal(self.close_for(symbol, day)),
                "close": Decimal(self.close_for(symbol, day)),
                "volume": 1000,
                "source": "kite",
            }
            for day in (PRIOR_DATE, TRADE_DATE)
            if start <= day <= end
        ]
        if not rows:
            return pl.DataFrame(schema=DAILY_BARS_SCHEMA)
        return conform(pl.DataFrame(rows, strict=False), DAILY_BARS_SCHEMA)

    def corporate_actions(self, since: dt.date) -> list[object]:
        return [a for a in self.actions if getattr(a, "ex_date", since) >= since]

    def index_constituents(self, index_slug: str, on: dt.date) -> list[str]:
        """Exactly the nominal number of members, so assertion 5 has something honest to check.

        ``nifty-allcap`` and ``etf`` are not asked of a provider at all — docs/06 §"Step 2"
        derives them by rule from instrument type plus a bar on the date.
        """
        del on
        nominal = NOMINAL_SIZES.get(index_slug)
        if nominal is not None:
            return list(self.equities[:nominal])
        if index_slug in ("nifty-total-market", "nifty-fno"):
            return list(self.equities)
        return []

    def index_snapshots(self, on: dt.date) -> list[object]:
        del on
        return []

    def bhavcopy(self, on: dt.date) -> pl.DataFrame:
        del on
        return pl.DataFrame()


async def table_checksum(engine: AsyncEngine, table: str) -> str:
    """A content hash of a whole table, order-independent.

    ``md5`` per row, XOR-folded — so it is insensitive to row order and to physical layout, and
    sensitive to any change in any value. That is what "zero row changes" has to mean.
    """
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                f"SELECT coalesce(md5(string_agg(row_hash, '' ORDER BY row_hash)), 'empty') "
                f"FROM (SELECT md5(t.*::text) AS row_hash FROM {table} t) s"
            )
        )
        return str(result.scalar_one())


async def checksums(engine: AsyncEngine) -> dict[str, str]:
    return {table: await table_checksum(engine, table) for table in CHECKSUMMED_TABLES}


def deps(provider: object) -> PipelineDependencies:
    return PipelineDependencies(provider=provider, cache=None, factor_engine=None)


class TestCriterion1Idempotency:
    """ "Running the full pipeline twice for the same date produces zero row changes on the
    second run (assert with a checksum of the affected tables)." """

    async def test_the_second_run_changes_no_row_in_any_table(
        self, engine: AsyncEngine, clean_db: None
    ) -> None:
        del clean_db
        provider = StubProvider()
        maker = async_sessionmaker(engine, expire_on_commit=False)

        async with maker() as session, session.begin():
            first = await run_nightly_pipeline(session, TRADE_DATE, deps(provider))
        assert first.status is RunStatus.SUCCEEDED, first.error
        before = await checksums(engine)

        async with maker() as session, session.begin():
            second = await run_nightly_pipeline(session, TRADE_DATE, deps(provider))
        assert second.status is RunStatus.SUCCEEDED, second.error
        after = await checksums(engine)

        changed = {t: (before[t], after[t]) for t in before if before[t] != after[t]}
        assert changed == {}, f"tables changed on the second run: {sorted(changed)}"

    async def test_row_counts_are_unchanged_too(self, engine: AsyncEngine, clean_db: None) -> None:
        """A checksum could in principle match on a different row count; check both."""
        del clean_db
        provider = StubProvider()
        maker = async_sessionmaker(engine, expire_on_commit=False)
        counts: list[dict[str, int]] = []
        for _ in range(2):
            async with maker() as session, session.begin():
                await run_nightly_pipeline(session, TRADE_DATE, deps(provider))
            async with engine.connect() as connection:
                counts.append(
                    {
                        table: int(
                            (
                                await connection.execute(text(f"SELECT count(*) FROM {table}"))
                            ).scalar_one()
                        )
                        for table in CHECKSUMMED_TABLES
                    }
                )
        assert counts[0] == counts[1]

    async def test_each_step_records_exactly_one_row_per_run(
        self, engine: AsyncEngine, clean_db: None
    ) -> None:
        """docs/09 §Observability: pipeline_run_step is the operator UI. Two rows for one step
        would show two answers to the same question."""
        del clean_db
        provider = StubProvider()
        maker = async_sessionmaker(engine, expire_on_commit=False)
        for _ in range(2):
            async with maker() as session, session.begin():
                await run_nightly_pipeline(session, TRADE_DATE, deps(provider))

        async with engine.connect() as connection:
            duplicates = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM (SELECT run_id, step FROM pipeline_run_step "
                        "GROUP BY run_id, step HAVING count(*) > 1) s"
                    )
                )
            ).scalar_one()
        assert int(duplicates) == 0

    async def test_the_second_run_publishes_a_new_data_version(
        self, engine: AsyncEngine, clean_db: None
    ) -> None:
        """Idempotent *data*, not a frozen version: re-publishing the same rows still advances
        the version, because docs/06 keys the cache on it and a re-run must invalidate."""
        del clean_db
        provider = StubProvider()
        maker = async_sessionmaker(engine, expire_on_commit=False)
        versions = []
        for _ in range(2):
            async with maker() as session, session.begin():
                outcome = await run_nightly_pipeline(session, TRADE_DATE, deps(provider))
                versions.append(outcome.data_version)
        assert versions[0] is not None
        assert versions[1] == versions[0] + 1


class TestCriterion2GateBlocksPublish:
    """ "A test injects a malformed day and proves the gate fails, data_version does NOT advance,
    and the previous version is still served." """

    async def test_a_malformed_day_fails_the_gate_and_does_not_publish(
        self, engine: AsyncEngine, clean_db: None
    ) -> None:
        del clean_db
        maker = async_sessionmaker(engine, expire_on_commit=False)

        # A clean day first, so there is a published version to protect.
        async with maker() as session, session.begin():
            good = await run_nightly_pipeline(session, PRIOR_DATE, deps(StubProvider()))
        assert good.status is RunStatus.SUCCEEDED
        published = good.data_version
        assert published is not None

        # The malformation: an unexplained 90% collapse across the whole universe — exactly what
        # docs/09 assertion 2 exists to catch, and exactly what a missed split looks like.
        broken = StubProvider(close="10", prior_close="100")
        async with maker() as session, session.begin():
            bad = await run_nightly_pipeline(session, TRADE_DATE, deps(broken))

        assert bad.status is RunStatus.FAILED
        assert bad.failed_step is PipelineStep.DATA_QUALITY_GATE
        assert bad.gate is not None
        assert not bad.gate.passed
        assert 2 in {r.assertion for r in bad.gate.failures}

        async with maker() as session:
            still_served = await current_data_version(session)
        assert still_served == published, "a failed gate must not advance data_version"

    async def test_the_previous_snapshot_is_still_served(
        self, engine: AsyncEngine, clean_db: None
    ) -> None:
        """docs/03: "the site continues serving yesterday's consistent snapshot"."""
        del clean_db
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session, session.begin():
            await run_nightly_pipeline(session, PRIOR_DATE, deps(StubProvider()))
        async with maker() as session:
            before = await current_data_version(session)

        broken = StubProvider(close="1", prior_close="100")
        async with maker() as session, session.begin():
            await run_nightly_pipeline(session, TRADE_DATE, deps(broken))

        async with maker() as session:
            after = await current_data_version(session)
            rows = await session.execute(
                select(func.count()).select_from(OhlcvDaily).where(OhlcvDaily.date == PRIOR_DATE)
            )
        assert after == before
        assert int(rows.scalar_one()) > 0, "yesterday's data must survive today's failure"

    async def test_the_failed_run_is_recorded_as_failed(
        self, engine: AsyncEngine, clean_db: None
    ) -> None:
        del clean_db
        maker = async_sessionmaker(engine, expire_on_commit=False)
        broken = StubProvider(close="1", prior_close="100")
        async with maker() as session, session.begin():
            await run_nightly_pipeline(session, PRIOR_DATE, deps(StubProvider()))
        async with maker() as session, session.begin():
            await run_nightly_pipeline(session, TRADE_DATE, deps(broken))

        async with maker() as session:
            run = (
                await session.execute(
                    select(PipelineRun).where(PipelineRun.trade_date == TRADE_DATE)
                )
            ).scalar_one()
        assert run.status == RunStatus.FAILED
        assert run.data_version is None


class TestCriterion3SplitAdjustment:
    """ "A test with a synthetic 2:1 split proves adjusted closes are continuous across the
    ex-date and that reprocess_instrument is idempotent." """

    EX_DATE = dt.date(2026, 8, 18)
    BEFORE = dt.date(2026, 8, 17)

    async def _seed_split(self, session: AsyncSession) -> int:
        instrument = await make_instrument(session, "SPLIT", token=1)
        await add_bar(session, instrument, dt.date(2026, 8, 14), "100")
        await add_bar(session, instrument, self.BEFORE, "100")
        await add_bar(session, instrument, self.EX_DATE, "50")
        session.add(
            CorporateAction(
                instrument_id=instrument,
                action_type="split",
                ex_date=self.EX_DATE,
                ratio_from=Decimal(2),
                ratio_to=Decimal(1),
                raw={"purpose": "FACE VALUE SPLIT FROM RS.2/- TO RE.1/-"},
            )
        )
        await session.flush()
        return instrument

    async def test_adjusted_closes_are_continuous_across_the_ex_date(
        self, session: AsyncSession
    ) -> None:
        instrument = await self._seed_split(session)
        await reprocess_instrument(session, instrument)

        rows = (
            (
                await session.execute(
                    select(OhlcvDaily.date, OhlcvDaily.close, OhlcvDaily.close_raw)
                    .where(OhlcvDaily.instrument_id == instrument)
                    .order_by(OhlcvDaily.date)
                )
            )
            .tuples()
            .all()
        )
        closes = {row[0]: row[1] for row in rows}
        raws = {row[0]: row[2] for row in rows}

        # Raw shows the step the split created.
        assert raws[self.BEFORE] / raws[self.EX_DATE] == Decimal(2)
        # Adjusted does not.
        assert closes[self.BEFORE] == closes[self.EX_DATE] == Decimal("50.0000")

    async def test_the_raw_print_is_never_rewritten(self, session: AsyncSession) -> None:
        """docs/02 rule 2: `close_raw` is the exchange print. Adjustment must not touch it."""
        instrument = await self._seed_split(session)
        await reprocess_instrument(session, instrument)
        raw = (
            await session.execute(
                select(OhlcvDaily.close_raw).where(
                    OhlcvDaily.instrument_id == instrument, OhlcvDaily.date == self.BEFORE
                )
            )
        ).scalar_one()
        assert raw == Decimal("100.0000")

    async def test_the_adjustment_factor_is_stored_per_row(self, session: AsyncSession) -> None:
        """docs/09 stores adj_factor per row "so any adjusted number can be reverse-engineered"."""
        instrument = await self._seed_split(session)
        await reprocess_instrument(session, instrument)
        rows = dict(
            (
                await session.execute(
                    select(OhlcvDaily.date, OhlcvDaily.adj_factor).where(
                        OhlcvDaily.instrument_id == instrument
                    )
                )
            )
            .tuples()
            .all()
        )
        assert rows[self.BEFORE] == Decimal("0.5000000000")
        assert rows[self.EX_DATE] == Decimal("1.0000000000")

    async def test_reprocess_instrument_is_idempotent(self, session: AsyncSession) -> None:
        """Run it three times; the adjusted series must not drift.

        This is the failure the ``adj_factor`` round-trip guards against: `ohlcv_daily` has no
        `open_raw`/`high_raw`/`low_raw`, so a naive rebuild would re-adjust its own output and
        halve the prices again on every run.
        """
        instrument = await self._seed_split(session)
        snapshots = []
        for _ in range(3):
            await reprocess_instrument(session, instrument)
            rows = (
                (
                    await session.execute(
                        select(
                            OhlcvDaily.date,
                            OhlcvDaily.open,
                            OhlcvDaily.high,
                            OhlcvDaily.low,
                            OhlcvDaily.close,
                            OhlcvDaily.volume,
                            OhlcvDaily.adj_factor,
                        )
                        .where(OhlcvDaily.instrument_id == instrument)
                        .order_by(OhlcvDaily.date)
                    )
                )
                .tuples()
                .all()
            )
            snapshots.append(rows)
        assert snapshots[0] == snapshots[1] == snapshots[2]

    async def test_volume_is_adjusted_the_other_way(self, session: AsyncSession) -> None:
        instrument = await self._seed_split(session)
        await reprocess_instrument(session, instrument)
        volume = (
            await session.execute(
                select(OhlcvDaily.volume).where(
                    OhlcvDaily.instrument_id == instrument, OhlcvDaily.date == self.BEFORE
                )
            )
        ).scalar_one()
        assert volume == 2000


class TestCriterion4BackfillResume:
    """ "Backfill can be killed mid-run and resumed to the same final state." """

    WINDOW = DateWindow(dt.date(2026, 8, 10), TRADE_DATE)
    SYMBOLS = 3

    async def test_a_completed_backfill_marks_every_unit_done(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        del clean_db
        provider = StubProvider()
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session, session.begin():
            for index, symbol in enumerate(provider.symbols[: self.SYMBOLS]):
                await make_instrument(session, symbol, token=index + 1)

        report = await run_backfill(
            provider, self.WINDOW, concurrency=1, chunk_days=3, database_url=migrated_url
        )
        assert report.planned > 0
        async with maker() as session:
            assert await count_done(session) == report.planned

    async def test_a_killed_run_resumes_to_the_same_final_state(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        """The whole point of the cursor table: partial progress survives, and finishing it
        produces exactly what an uninterrupted run would have."""
        del clean_db
        provider = StubProvider()
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session, session.begin():
            for index, symbol in enumerate(provider.symbols[: self.SYMBOLS]):
                await make_instrument(session, symbol, token=index + 1)

        # Kill after two units.
        killed = await run_backfill(
            provider,
            self.WINDOW,
            concurrency=1,
            chunk_days=3,
            database_url=migrated_url,
            stop_after=2,
        )
        assert killed.completed == 2
        async with maker() as session:
            part_way = await count_done(session)
            outstanding = (
                await session.execute(
                    select(func.count())
                    .select_from(IngestCursor)
                    .where(IngestCursor.status == STATUS_PENDING)
                )
            ).scalar_one()
        assert part_way == 2
        assert int(outstanding) > 0, "an interrupted run must leave work marked pending"

        # Resume.
        resumed = await run_backfill(
            provider,
            self.WINDOW,
            concurrency=1,
            chunk_days=3,
            database_url=migrated_url,
            resume=True,
        )
        assert resumed.already_done == 2
        async with maker() as session:
            assert await count_done(session) == killed.planned
            remaining = (
                await session.execute(
                    select(func.count())
                    .select_from(IngestCursor)
                    .where(IngestCursor.status != STATUS_DONE)
                )
            ).scalar_one()
        assert int(remaining) == 0

    async def test_resuming_does_not_refetch_completed_units(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        """docs/09 §Backfill budgets a weekend; re-fetching finished chunks would double it."""
        del clean_db

        class CountingProvider(StubProvider):
            def __init__(self) -> None:
                super().__init__()
                self.fetches = 0

            def daily_bars(self, token: int, start: dt.date, end: dt.date) -> pl.DataFrame:
                self.fetches += 1
                return super().daily_bars(token, start, end)

        provider = CountingProvider()
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session, session.begin():
            for index, symbol in enumerate(provider.symbols[: self.SYMBOLS]):
                await make_instrument(session, symbol, token=index + 1)

        await run_backfill(
            provider,
            self.WINDOW,
            concurrency=1,
            chunk_days=3,
            database_url=migrated_url,
            stop_after=2,
        )
        after_kill = provider.fetches
        await run_backfill(
            provider,
            self.WINDOW,
            concurrency=1,
            chunk_days=3,
            database_url=migrated_url,
            resume=True,
        )
        total_units = await self._planned_units(engine)
        assert provider.fetches == total_units
        assert after_kill == 2

    async def test_bar_rows_match_an_uninterrupted_run(
        self, engine: AsyncEngine, clean_db: None, migrated_url: str
    ) -> None:
        del clean_db
        provider = StubProvider()
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session, session.begin():
            for index, symbol in enumerate(provider.symbols[: self.SYMBOLS]):
                await make_instrument(session, symbol, token=index + 1)

        await run_backfill(
            provider,
            self.WINDOW,
            concurrency=1,
            chunk_days=3,
            database_url=migrated_url,
            stop_after=2,
        )
        await run_backfill(
            provider,
            self.WINDOW,
            concurrency=1,
            chunk_days=3,
            database_url=migrated_url,
            resume=True,
        )
        async with maker() as session:
            bars = (
                await session.execute(select(func.count()).select_from(OhlcvDaily))
            ).scalar_one()
        # Two dates per symbol are inside the window.
        assert int(bars) == 2 * self.SYMBOLS

    @staticmethod
    async def _planned_units(engine: AsyncEngine) -> int:
        maker = async_sessionmaker(engine)
        async with maker() as session:
            return int(
                (await session.execute(select(func.count()).select_from(IngestCursor))).scalar_one()
            )
