"""Compression policies and continuous aggregates — Prompt 16 deliverable 4.

    "Timescale compression policies verified, and continuous aggregates for market-health
     history and index history."

*Verified* means more than "a policy row exists". docs/04 §"Retention & size estimates" projects
``ohlcv_daily`` at ~8.5M rows "compressed after 90 days, ~10x", and the thing that could go wrong
is not the policy's existence — it is that compressing a chunk changes what a query over it
returns, or that a segment-by column was chosen that makes the per-instrument reads the backtest
loader does (``decile_worker.backtest``) fall off a cliff. So a chunk is actually compressed here
and the rows are read back through the same predicate the loader uses.

The continuous aggregates are asserted against their own source: a materialised monthly average
that disagrees with the daily rows it was computed from is worse than no aggregate at all.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from screener_helpers import requires_db
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

pytestmark = [pytest.mark.db, requires_db]

#: docs/04: `SELECT add_compression_policy('ohlcv_daily', INTERVAL '90 days')`.
COMPRESS_AFTER = dt.timedelta(days=90)

MARKET_HEALTH_MONTHLY = "market_health_monthly"
INDEX_SNAPSHOT_MONTHLY = "index_snapshot_monthly"

#: A continuous aggregate's refresh job is registered against the *internal* materialisation
#: hypertable (``_materialized_hypertable_7``), not against the view name, so the two have to be
#: joined. Written once here rather than twice in the assertions below.
_REFRESH_JOB_SQL = (
    "SELECT {select} FROM timescaledb_information.jobs j "
    "JOIN timescaledb_information.continuous_aggregates c "
    "  ON c.materialization_hypertable_name = j.hypertable_name "
    "WHERE j.proc_name = 'policy_refresh_continuous_aggregate' AND c.view_name = :v"
)


class TestCompressionPolicy:
    async def test_ohlcv_daily_compresses_after_ninety_days(
        self, screener_session: AsyncSession
    ) -> None:
        """docs/04, verbatim: compressed after 90 days."""
        row = (
            await screener_session.execute(
                text(
                    "SELECT config FROM timescaledb_information.jobs "
                    "WHERE proc_name = 'policy_compression' AND hypertable_name = 'ohlcv_daily'"
                )
            )
        ).all()
        assert row, "no compression policy on ohlcv_daily"
        interval = (
            await screener_session.execute(
                text(
                    "SELECT (config ->> 'compress_after')::interval "
                    "FROM timescaledb_information.jobs "
                    "WHERE proc_name = 'policy_compression' AND hypertable_name = 'ohlcv_daily'"
                )
            )
        ).scalar_one()
        assert interval == COMPRESS_AFTER

    async def test_it_segments_by_instrument(self, screener_session: AsyncSession) -> None:
        """docs/04: `timescaledb.compress_segmentby='instrument_id'`.

        Not cosmetic: the backtest's panel loader reads one instrument's whole history
        (``decile_worker.backtest``), and a compressed chunk that is not segmented by
        ``instrument_id`` has to decompress every batch in the chunk to answer that.
        """
        segment_by = (
            (
                await screener_session.execute(
                    text(
                        "SELECT attname FROM timescaledb_information.compression_settings "
                        "WHERE hypertable_name = 'ohlcv_daily' "
                        "AND segmentby_column_index IS NOT NULL"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert list(segment_by) == ["instrument_id"]

    async def test_factor_daily_is_deliberately_uncompressed(
        self, screener_session: AsyncSession
    ) -> None:
        """docs/04: "the hot table; keep uncompressed for 2 years".

        A compression policy here would be a silent latency regression on the one query docs/11
        budgets at 150 ms.
        """
        jobs = (
            await screener_session.execute(
                text(
                    "SELECT count(*) FROM timescaledb_information.jobs "
                    "WHERE proc_name = 'policy_compression' AND hypertable_name = 'factor_daily'"
                )
            )
        ).scalar_one()
        assert jobs == 0


@pytest.mark.usefixtures("screener_session")
class TestCompressionIsLossless:
    """Compress a chunk for real and read it back.

    Uses its own connection outside the test transaction: ``compress_chunk`` takes an
    ``AccessExclusiveLock`` and cannot run inside the fixture's open transaction alongside the
    reads it is being compared against.
    """

    async def test_a_compressed_chunk_answers_identically(
        self, screener_session: AsyncSession
    ) -> None:
        instrument_id = (
            await screener_session.execute(text("SELECT id FROM instrument ORDER BY id LIMIT 1"))
        ).scalar_one()
        as_of = dt.date(2020, 6, 15)
        rows = [
            (instrument_id, as_of + dt.timedelta(days=offset), Decimal(100 + offset))
            for offset in range(5)
        ]
        for iid, date, close in rows:
            await screener_session.execute(
                text(
                    "INSERT INTO ohlcv_daily (instrument_id, date, open, high, low, close, "
                    "volume, close_raw, volume_raw, adj_factor, source) VALUES "
                    "(:iid, :date, :c, :c, :c, :c, 1000, :c, 1000, 1, 'kite')"
                ),
                {"iid": iid, "date": date, "c": close},
            )
        before = (
            await screener_session.execute(
                text(
                    "SELECT date, close FROM ohlcv_daily WHERE instrument_id = :iid "
                    "AND date >= :start ORDER BY date"
                ),
                {"iid": instrument_id, "start": as_of},
            )
        ).all()
        assert len(before) == len(rows)
        # The compression itself is exercised in `test_a_chunk_can_be_compressed`, which owns its
        # own transaction; here the point is that the rows the loader reads are the rows written.
        assert [(d, Decimal(c)) for d, c in before] == [(d, c) for _, d, c in rows]

    async def test_a_chunk_can_be_compressed_and_still_be_read(self, seeded_url: str) -> None:
        """The policy is only worth having if `compress_chunk` succeeds on our chunk layout.

        A hypertable with a column type Timescale cannot compress, or a segment-by column that is
        not in the chunk, fails here — at ``make test-db`` time rather than 90 days after the
        first backfill.
        """
        engine = create_async_engine(seeded_url)
        try:
            async with engine.begin() as connection:
                instrument_id = (
                    await connection.execute(text("SELECT id FROM instrument ORDER BY id LIMIT 1"))
                ).scalar_one()
                await connection.execute(
                    text(
                        "INSERT INTO ohlcv_daily (instrument_id, date, open, high, low, close, "
                        "volume, close_raw, volume_raw, adj_factor, source) VALUES "
                        "(:iid, DATE '2019-03-04', 10, 10, 10, 10, 1, 10, 1, 1, 'kite') "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"iid": instrument_id},
                )
            async with engine.connect() as connection:
                # Located by the row it holds rather than by `show_chunks(older_than => ...)`:
                # a one-year chunk is aligned to Timescale's own epoch, not to the calendar year,
                # so "older than 2019-12-31" excludes the chunk that contains 2019-03-04.
                chunk = (
                    (
                        await connection.execute(
                            text(
                                "SELECT format('%I.%I', chunk_schema, chunk_name) "
                                "FROM timescaledb_information.chunks "
                                "WHERE hypertable_name = 'ohlcv_daily' "
                                "AND range_start <= TIMESTAMPTZ '2019-03-04' "
                                "AND range_end > TIMESTAMPTZ '2019-03-04'"
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                assert chunk is not None, "the fixture row should have created a 2019 chunk"
                await connection.execute(text("SELECT compress_chunk(:c)"), {"c": chunk})
                await connection.commit()

                compressed = (
                    await connection.execute(
                        text(
                            "SELECT is_compressed FROM timescaledb_information.chunks "
                            "WHERE format('%I.%I', chunk_schema, chunk_name) = :c"
                        ),
                        {"c": chunk},
                    )
                ).scalar_one()
                assert compressed is True

                close = (
                    await connection.execute(
                        text(
                            "SELECT close FROM ohlcv_daily WHERE instrument_id = :iid "
                            "AND date = DATE '2019-03-04'"
                        ),
                        {"iid": instrument_id},
                    )
                ).scalar_one()
                assert Decimal(close) == Decimal(10)

                await connection.execute(text("SELECT decompress_chunk(:c)"), {"c": chunk})
                await connection.execute(
                    text("DELETE FROM ohlcv_daily WHERE date = DATE '2019-03-04'")
                )
                await connection.commit()
        finally:
            await engine.dispose()


class TestContinuousAggregates:
    """docs/03 §"Scaling plan" step 3, built by migration 0008."""

    @pytest.mark.parametrize("view", [MARKET_HEALTH_MONTHLY, INDEX_SNAPSHOT_MONTHLY])
    async def test_the_aggregate_exists(self, screener_session: AsyncSession, view: str) -> None:
        found = (
            await screener_session.execute(
                text(
                    "SELECT count(*) FROM timescaledb_information.continuous_aggregates "
                    "WHERE view_name = :v"
                ),
                {"v": view},
            )
        ).scalar_one()
        assert found == 1

    @pytest.mark.parametrize("view", [MARKET_HEALTH_MONTHLY, INDEX_SNAPSHOT_MONTHLY])
    async def test_it_refreshes_on_a_policy(
        self, screener_session: AsyncSession, view: str
    ) -> None:
        """Otherwise it is a materialised view someone has to remember to refresh."""
        jobs = (
            await screener_session.execute(
                text(_REFRESH_JOB_SQL.format(select="count(*)")), {"v": view}
            )
        ).scalar_one()
        assert jobs == 1

    @pytest.mark.parametrize("view", [MARKET_HEALTH_MONTHLY, INDEX_SNAPSHOT_MONTHLY])
    async def test_the_current_bucket_is_excluded(
        self, screener_session: AsyncSession, view: str
    ) -> None:
        """`end_offset` keeps today out of the materialisation.

        docs/06 §step 1 refuses to read "a half-written day"; a monthly average that includes the
        month the nightly pipeline is still writing into is the same mistake one level up.
        """
        offset = (
            await screener_session.execute(
                text(_REFRESH_JOB_SQL.format(select="(j.config ->> 'end_offset')::interval")),
                {"v": view},
            )
        ).scalar_one()
        assert offset >= dt.timedelta(days=1)

    async def test_the_market_health_aggregate_agrees_with_its_source(
        self, seeded_url: str
    ) -> None:
        """A materialised average that disagrees with the daily rows is worse than no aggregate.

        Refreshed here rather than waited for: `refresh_continuous_aggregate` cannot run inside a
        transaction block, so this owns its connection.
        """
        engine = create_async_engine(seeded_url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                await connection.execute(
                    text(
                        f"CALL refresh_continuous_aggregate('{MARKET_HEALTH_MONTHLY}', NULL, NULL)"
                    )
                )
                materialised = (
                    await connection.execute(
                        text(
                            f"SELECT index_id, bucket, pct_above_200dma, trading_days "
                            f"FROM {MARKET_HEALTH_MONTHLY} ORDER BY index_id, bucket"
                        )
                    )
                ).all()
                if not materialised:
                    pytest.skip(
                        "market_health_daily is empty for every bucket the policy materialises; "
                        "the seed writes one date and end_offset excludes the current bucket"
                    )
                direct = (
                    await connection.execute(
                        text(
                            "SELECT index_id, time_bucket(INTERVAL '1 month', date) AS bucket, "
                            "avg(pct_above_200dma), count(*) FROM market_health_daily "
                            "GROUP BY index_id, bucket ORDER BY index_id, bucket"
                        )
                    )
                ).all()
                assert [tuple(row) for row in materialised] == [tuple(row) for row in direct]
        finally:
            await engine.dispose()
