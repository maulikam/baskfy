"""Curated-basket schema acceptance — docs/smallcase/03 (SC1)."""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import subprocess
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final, get_args

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy import insert as sa_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from baskfy_api import curated_metrics_service
from baskfy_api.curated_metrics_service import (
    CLOSE_RAW_LOOKBACK_DAYS,
    BasketMetricValues,
    CatalogContext,
    compute_all_metrics,
    upsert_metrics_row,
)
from baskfy_api.curated_seed import count_curated_managers, seed_curated_managers
from baskfy_core.curated_baskets import assert_weights_sum_to_one
from baskfy_core.curated_metrics import (
    DIVIDENDS_INCLUDED,
    RETURN_CONVENTION,
    VolatilityBasis,
)
from baskfy_core.models import (
    Base,
    CbBasket,
    CbBasketVersion,
    CbConstituent,
    CbManager,
    CbMetrics,
    Exchange,
    Instrument,
    OhlcvDaily,
)
from baskfy_core.models.curated_baskets import RETURN_CONVENTIONS, VOLATILITY_BASES

ENV_VAR = "BASKFY_TEST_DATABASE_URL"
API_DIR = Path(__file__).resolve().parents[1]

CB_TABLES = frozenset(name for name in Base.metadata.tables if name.startswith("cb_"))

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        os.environ.get(ENV_VAR) is None,
        reason=f"{ENV_VAR} is not set; run `make up` and export it",
    ),
]


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    url = os.environ[ENV_VAR]
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=API_DIR,
        env={k: v for k, v in os.environ.items() if k != "BASKFY_DATABASE_URL"}
        | {"BASKFY_DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def migrated(clean_database: object) -> None:
    _alembic("upgrade", "head")


@pytest.mark.asyncio
async def test_migration_creates_all_cb_tables(engine: AsyncEngine, migrated: None) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name LIKE 'cb_%'"
            )
        )
        present = {r[0] for r in rows}
    assert present >= CB_TABLES
    assert len(CB_TABLES) == 18


@pytest.mark.asyncio
async def test_re_migrate_is_noop(engine: AsyncEngine, migrated: None) -> None:
    _alembic("upgrade", "head")
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name LIKE 'cb_%'"
            )
        )
        present = {r[0] for r in rows}
    assert present >= CB_TABLES


def test_bad_constituent_weights_fail_at_domain_assertion() -> None:
    with pytest.raises(ValueError, match="must sum to"):
        assert_weights_sum_to_one([Decimal("0.6000"), Decimal("0.5000")])


@pytest.mark.asyncio
async def test_seed_managers_twice_is_idempotent(engine: AsyncEngine, migrated: None) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await seed_curated_managers(session)
        await session.commit()
        first = await count_curated_managers(session)

        await seed_curated_managers(session)
        await session.commit()
        second = await count_curated_managers(session)

        rows = (await session.execute(select(CbManager.slug))).scalars().all()
        await session.commit()

    assert first == 2
    assert second == 2
    assert set(rows) == {"baskfy-engine", "maulik"}


@pytest.mark.asyncio
async def test_cb_manager_kind_check_rejects_invalid(engine: AsyncEngine, migrated: None) -> None:
    async with engine.begin() as conn:
        with pytest.raises(Exception):  # noqa: B017 — DB raises on check violation
            await conn.execute(
                text("INSERT INTO cb_manager (slug, name, kind) VALUES ('bad', 'Bad', 'ROBOT')")
            )


@pytest.mark.asyncio
async def test_cb_tables_have_no_float_columns(engine: AsyncEngine, migrated: None) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name LIKE 'cb_%' "
                "AND data_type IN ('real', 'double precision')"
            )
        )
        assert [tuple(r) for r in rows] == []


# --- SC-hardening: the four facts that used to die in the job payload --------
#
# ``compute_all_metrics`` computed ``volatility_basis``, ``months_available`` and the A6 return
# convention and had nowhere to put them, so the API could not serve a single one. Migration
# 0015 adds the columns; these tests assert the spec of each, including the one detail a
# from-memory implementation gets wrong — the accepted ``volatility_basis`` values are the
# literals ``baskfy_core`` actually writes, not a plausible re-spelling of them.


NEW_METRIC_COLUMNS: frozenset[str] = frozenset(
    {"volatility_basis", "months_available", "return_convention", "dividends_included"}
)


async def _metrics_columns(engine: AsyncEngine) -> dict[str, tuple[str, str, str | None]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'cb_metrics'"
            )
        )
        return {r[0]: (r[1], r[2], r[3]) for r in rows}


def test_model_volatility_bases_are_the_literals_core_writes() -> None:
    """The check constraint's values must be the ones the compute produces, not near-misses.

    ``CatalogVolatility.basis`` is written straight into the column: if the model and the
    migration spell it ``TRAILING_252`` while ``baskfy_core`` writes ``BASKET_252D``, every row
    the job produces violates the constraint and the nightly job dies on its first basket.
    """
    assert get_args(VolatilityBasis) == VOLATILITY_BASES
    assert RETURN_CONVENTION in RETURN_CONVENTIONS


@pytest.mark.asyncio
async def test_cb_metrics_carries_the_four_computed_facts(
    engine: AsyncEngine, migrated: None
) -> None:
    columns = await _metrics_columns(engine)
    assert set(columns) >= NEW_METRIC_COLUMNS
    assert columns["volatility_basis"][:2] == ("text", "YES")
    assert columns["months_available"][:2] == ("integer", "YES")
    assert columns["return_convention"][:2] == ("text", "NO")
    assert columns["dividends_included"][:2] == ("boolean", "NO")
    # The convention is a property of the table, so pre-0015 rows are covered by the default.
    assert columns["return_convention"][2] is not None
    assert "PRICE_RETURN" in columns["return_convention"][2]
    assert columns["dividends_included"][2] == "false"


async def _insert_basket(engine: AsyncEngine) -> int:
    """A minimal published basket, enough to hang a ``cb_metrics`` row off."""
    async with engine.begin() as conn:
        manager_id = (
            await conn.execute(
                text(
                    "INSERT INTO cb_manager (slug, name, kind) "
                    "VALUES ('m', 'M', 'ENGINE') RETURNING id"
                )
            )
        ).scalar_one()
        return int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO cb_basket "
                        "(slug, name, manager_id, type, access, visibility, "
                        " rebalance_frequency, source) "
                        "VALUES ('b', 'B', :m, 'STOCK', 'FREE', 'PUBLISHED', 'WEEKLY', 'MANUAL') "
                        "RETURNING id"
                    ),
                    {"m": manager_id},
                )
            ).scalar_one()
        )


_INSERT_METRICS = (
    "INSERT INTO cb_metrics (basket_id, as_of_date, computed_at, volatility_basis, "
    "months_available, return_convention, dividends_included) "
    "VALUES (:b, :d, now(), :basis, :months, :convention, :dividends)"
)


@pytest.mark.asyncio
async def test_volatility_basis_check_accepts_every_literal_core_writes(
    engine: AsyncEngine, migrated: None
) -> None:
    basket_id = await _insert_basket(engine)
    for offset, basis in enumerate(get_args(VolatilityBasis)):
        async with engine.begin() as conn:
            await conn.execute(
                text(_INSERT_METRICS),
                {
                    "b": basket_id,
                    "d": dt.date(2024, 6, 3) + dt.timedelta(days=offset),
                    "basis": basis,
                    "months": 12,
                    "convention": "PRICE_RETURN",
                    "dividends": False,
                },
            )
    async with engine.connect() as conn:
        stored = {
            r[0]
            for r in await conn.execute(text("SELECT DISTINCT volatility_basis FROM cb_metrics"))
        }
    assert stored == set(get_args(VolatilityBasis))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("basis", "months", "convention"),
    [
        ("TRAILING_252", 12, "PRICE_RETURN"),  # a plausible spelling core never writes
        ("BASKET_252D", -1, "PRICE_RETURN"),  # a span cannot be negative
        ("BASKET_252D", 12, "GROSS_RETURN"),  # only the two documented conventions
    ],
)
async def test_cb_metrics_rejects_values_outside_the_spec(
    engine: AsyncEngine, migrated: None, basis: str, months: int, convention: str
) -> None:
    basket_id = await _insert_basket(engine)
    # IntegrityError, not any DBAPIError: a missing column raises ProgrammingError, and a test
    # that accepts either passes when the column does not exist at all.
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(_INSERT_METRICS),
                {
                    "b": basket_id,
                    "d": dt.date(2024, 6, 3),
                    "basis": basis,
                    "months": months,
                    "convention": convention,
                    "dividends": False,
                },
            )


@pytest.mark.asyncio
async def test_downgrade_then_upgrade_restores_the_columns(
    engine: AsyncEngine, migrated: None
) -> None:
    """0015 must be reversible: a migration with no working downgrade is a one-way door."""
    assert set(await _metrics_columns(engine)) >= NEW_METRIC_COLUMNS

    _alembic("downgrade", "0014_curated_baskets")
    after_down = await _metrics_columns(engine)
    assert NEW_METRIC_COLUMNS & set(after_down) == frozenset()
    assert "volatility_bucket" in after_down  # the rest of the table survives

    _alembic("upgrade", "head")
    assert set(await _metrics_columns(engine)) >= NEW_METRIC_COLUMNS


@pytest.mark.asyncio
async def test_upsert_metrics_row_writes_and_updates_all_four(
    engine: AsyncEngine, migrated: None
) -> None:
    """On conflict the four must be overwritten too.

    Leaving ``volatility_basis`` at yesterday's value while replacing ``volatility_value``
    publishes a number labelled with a measurement that did not produce it.
    """
    basket_id = await _insert_basket(engine)
    as_of = dt.date(2024, 6, 3)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await upsert_metrics_row(
            session,
            basket_id=basket_id,
            as_of=as_of,
            min_amt=None,
            vol_bucket="LOW",
            vol_value=Decimal("0.1000000000"),
            vol_basis="CONSTITUENT_WEIGHTED",
            months_available=2,
            ret_1m=None,
            ret_6m=None,
            ret_1y=None,
            cagr_3y=None,
            cagr_5y=None,
            since_inception_pct=None,
            computed_at=dt.datetime(2024, 6, 3, 15, 0, tzinfo=dt.UTC),
        )
        await session.commit()
        await upsert_metrics_row(
            session,
            basket_id=basket_id,
            as_of=as_of,
            min_amt=None,
            vol_bucket="HIGH",
            vol_value=Decimal("0.3000000000"),
            vol_basis="BASKET_252D",
            months_available=14,
            ret_1m=None,
            ret_6m=None,
            ret_1y=None,
            cagr_3y=None,
            cagr_5y=None,
            since_inception_pct=None,
            computed_at=dt.datetime(2024, 6, 4, 15, 0, tzinfo=dt.UTC),
        )
        await session.commit()
        row = (
            await session.execute(select(CbMetrics).where(CbMetrics.basket_id == basket_id))
        ).scalar_one()

    assert row.volatility_basis == "BASKET_252D"
    assert row.months_available == 14
    assert row.return_convention == RETURN_CONVENTION
    assert row.dividends_included == DIVIDENDS_INCLUDED


# --- SC-hardening: what the EOD metrics job costs ----------------------------
#
# The job's shape was measured, not guessed: 952 round-trips for a 50-basket catalog, 750 of
# them a per-instrument `close_raw` lookup issued inside a Python loop; price history re-read
# once per basket though the catalog's baskets share most of their constituents; and one
# transaction held open across the whole run, which pins the xmin horizon and stops TimescaleDB
# compressing `ohlcv_daily` while it runs. These tests assert the shape, in round-trips and
# commits, because a wall-clock budget on a laptop asserts the laptop.

AS_OF: Final = dt.date(2024, 6, 3)
NOW: Final = dt.datetime(2024, 6, 3, 15, 0, tzinfo=dt.UTC)
OLD_INCEPTION: Final = dt.date(2019, 1, 2)
YOUNG_INCEPTION: Final = dt.date(2024, 1, 2)
PERF_SYMBOLS: Final = ("AAA", "BBB", "CCC", "DDD")
#: AAA is the old basket's alone, DDD the young one's; BBB and CCC sit in both. That overlap is
#: the duplication the union load exists to remove.
OLD_BASKET_SYMBOLS: Final = ("AAA", "BBB", "CCC")
YOUNG_BASKET_SYMBOLS: Final = ("BBB", "CCC", "DDD")


class _StatementLog:
    """Every statement the job sends, so round-trips can be counted rather than hoped about."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[object, ...]]] = []

    def __call__(  # noqa: PLR0913, PLR0917 - SQLAlchemy's before_cursor_execute signature
        self,
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        flat = tuple(parameters) if isinstance(parameters, list | tuple) else ()
        self.statements.append((statement, flat))

    def matching(self, needle: str) -> list[tuple[str, tuple[object, ...]]]:
        return [row for row in self.statements if needle in row[0]]


class _CommitCounter:
    """How many transactions the job actually ended."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, session: object) -> None:
        self.count += 1


@contextlib.contextmanager
def _recording(engine: AsyncEngine) -> Iterator[_StatementLog]:
    log = _StatementLog()
    event.listen(engine.sync_engine, "before_cursor_execute", log)
    try:
        yield log
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", log)


async def _reset_schema_and_timescale(engine: AsyncEngine) -> None:
    """Drop ``public`` *and* the extension.

    Dropping ``public`` alone leaves ``_timescaledb_internal`` chunks behind that the next
    insert is routed into; see ``screener_helpers._reset_and_seed`` for the failure that
    produces.
    """
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await connection.execute(text("DROP EXTENSION IF EXISTS timescaledb CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))


@pytest_asyncio.fixture
async def migrated_hypertables(engine: AsyncEngine) -> AsyncIterator[None]:
    """A fresh schema for the tests that insert five years of bars into a hypertable.

    The compression policy is removed for the duration, and the schema is reset again on the
    way out. Both for the same reason, and it is the reason task (c) exists: ``ohlcv_daily``
    carries a 90-day compression policy, which is a *background job*. Bars from 2019 are past
    the threshold the moment they land, so the job wakes, takes locks on the hypertable, and
    deadlocks against the next test's ``DROP SCHEMA``. Leaving the data behind pushes the same
    deadlock into the next run of the suite.
    """
    await _reset_schema_and_timescale(engine)
    _alembic("upgrade", "head")
    async with engine.begin() as connection:
        await connection.execute(text("SELECT remove_compression_policy('ohlcv_daily')"))
    try:
        yield
    finally:
        await _reset_schema_and_timescale(engine)


def _weekdays(start: dt.date, end: dt.date) -> list[dt.date]:
    days: list[dt.date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


def _close_on(symbol: str, index: int) -> Decimal:
    """A deterministic sawtooth. Exact Decimals — these are prices (house rule 9)."""
    offset = Decimal((index * 7 + len(symbol) * 3 + PERF_SYMBOLS.index(symbol) * 11) % 23)
    return (Decimal(100) + offset + Decimal("0.50")).quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class _SeededCatalog:
    """Two baskets that overlap: an eleven-year-style old one and a young one."""

    old_basket_id: int
    young_basket_id: int


async def _add_basket(  # noqa: PLR0913 - a basket, its version and its constituents
    session: AsyncSession,
    *,
    slug: str,
    manager_id: int,
    launched_at: dt.date,
    effective_date: dt.date,
    symbols: Sequence[str],
    instrument_ids: Mapping[str, int],
) -> int:
    basket = CbBasket(
        slug=slug,
        name=slug.upper(),
        manager_id=manager_id,
        type="STOCK",
        access="FREE",
        visibility="PUBLISHED",
        rebalance_frequency="WEEKLY",
        source="MANUAL",
        launched_at=launched_at,
    )
    session.add(basket)
    await session.flush()
    version = CbBasketVersion(
        basket_id=basket.id, version_no=1, effective_date=effective_date, label="GENESIS"
    )
    session.add(version)
    await session.flush()
    weights = [Decimal("0.4000"), Decimal("0.3000"), Decimal("0.3000")]
    for symbol, weight in zip(symbols, weights, strict=True):
        session.add(
            CbConstituent(
                version_id=version.id,
                instrument_id=instrument_ids[symbol],
                segment="EQ",
                weight=weight,
            )
        )
    await session.flush()
    return int(basket.id)


async def _seed_overlapping_catalog(engine: AsyncEngine) -> _SeededCatalog:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Exchange(id=1, code="NSE"))
        await session.flush()
        instrument_ids: dict[str, int] = {}
        for symbol in PERF_SYMBOLS:
            instrument = Instrument(
                exchange_id=1,
                symbol=symbol,
                name=f"{symbol} LIMITED",
                series="EQ",
                instrument_type="EQ",
                is_active=True,
            )
            session.add(instrument)
            await session.flush()
            instrument_ids[symbol] = int(instrument.id)

        bars: list[dict[str, object]] = []
        for index, day in enumerate(_weekdays(OLD_INCEPTION, AS_OF)):
            for symbol in PERF_SYMBOLS:
                close = _close_on(symbol, index)
                bars.append(
                    {
                        "instrument_id": instrument_ids[symbol],
                        "date": day,
                        "open": close,
                        "high": close,
                        "low": close,
                        "close": close,
                        "volume": 1000,
                        "close_raw": close,
                        "volume_raw": 1000,
                        "adj_factor": Decimal("1"),
                        "source": "nse",
                    }
                )
        await session.execute(sa_insert(OhlcvDaily), bars)

        await seed_curated_managers(session)
        await session.flush()
        manager_id = int(
            (await session.execute(select(CbManager.id).order_by(CbManager.id))).scalars().first()
            or 0
        )
        old_id = await _add_basket(
            session,
            slug="old-basket",
            manager_id=manager_id,
            launched_at=OLD_INCEPTION,
            effective_date=OLD_INCEPTION,
            symbols=OLD_BASKET_SYMBOLS,
            instrument_ids=instrument_ids,
        )
        young_id = await _add_basket(
            session,
            slug="young-basket",
            manager_id=manager_id,
            launched_at=YOUNG_INCEPTION,
            effective_date=YOUNG_INCEPTION,
            symbols=YOUNG_BASKET_SYMBOLS,
            instrument_ids=instrument_ids,
        )
        await session.commit()
    return _SeededCatalog(old_basket_id=old_id, young_basket_id=young_id)


@pytest.mark.asyncio
async def test_metrics_job_reads_prices_once_per_chunk_not_once_per_instrument(
    engine: AsyncEngine, migrated_hypertables: None
) -> None:
    """The two price reads are per chunk, not per instrument and not per basket.

    Before: one ``close_raw`` statement per constituent (six here; 750 for a 50-basket catalog)
    and one history statement per basket, re-reading the constituents the baskets share.
    """
    await _seed_overlapping_catalog(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        with _recording(engine) as log:
            await compute_all_metrics(session, AS_OF, now=NOW)

    ohlcv = log.matching("ohlcv_daily")
    close_raw = [row for row in ohlcv if "DISTINCT ON" in row[0]]
    history = [row for row in ohlcv if "DISTINCT ON" not in row[0]]
    assert len(close_raw) == 1
    assert len(history) == 1
    # The cursor a resumed run walks has to be stable, so the basket select is ordered.
    assert any("ORDER BY cb_basket.id" in statement for statement, _ in log.statements)


@pytest.mark.asyncio
async def test_close_raw_lookup_is_bounded_below(
    engine: AsyncEngine, migrated_hypertables: None
) -> None:
    """A lower date bound, not just batching.

    ``ohlcv_daily`` is a hypertable with one-year chunks: an open-ended ``date <= as_of`` makes
    the planner consider every chunk of every year it has, and that per-statement planning is
    most of the cost. Batching alone measured no better than the loop it replaced.
    """
    await _seed_overlapping_catalog(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        with _recording(engine) as log:
            await compute_all_metrics(session, AS_OF, now=NOW)

    close_raw = [row for row in log.matching("ohlcv_daily") if "DISTINCT ON" in row[0]]
    statement, parameters = close_raw[0]
    assert AS_OF - dt.timedelta(days=CLOSE_RAW_LOOKBACK_DAYS) in parameters
    assert AS_OF in parameters
    assert "ORDER BY" in statement


@pytest.mark.asyncio
async def test_history_load_is_bounded_by_the_earliest_anchor_in_the_chunk(
    engine: AsyncEngine, migrated_hypertables: None
) -> None:
    """One union load for the chunk, reaching back only as far as its oldest basket needs.

    Since-inception is anchored at the first version's ``effective_date`` (a correctness fix,
    not a regression to revert), so the old basket needs its whole record. The young basket
    rides along on that one read instead of issuing a second, shorter one.
    """
    await _seed_overlapping_catalog(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        with _recording(engine) as log:
            await compute_all_metrics(session, AS_OF, now=NOW)

    history = [row for row in log.matching("ohlcv_daily") if "DISTINCT ON" not in row[0]]
    assert len(history) == 1
    _, parameters = history[0]
    assert OLD_INCEPTION in parameters
    assert YOUNG_INCEPTION not in parameters


@pytest.mark.asyncio
async def test_metrics_job_commits_per_basket(
    engine: AsyncEngine, migrated_hypertables: None
) -> None:
    """Not one transaction for the whole catalog: that pins the xmin horizon for its duration."""
    seeded = await _seed_overlapping_catalog(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    counter = _CommitCounter()
    async with factory() as session:
        event.listen(session.sync_session, "after_commit", counter)
        try:
            await compute_all_metrics(session, AS_OF, now=NOW)
        finally:
            event.remove(session.sync_session, "after_commit", counter)

        written = (
            (await session.execute(select(CbMetrics.basket_id).order_by(CbMetrics.basket_id)))
            .scalars()
            .all()
        )

    assert list(written) == sorted([seeded.old_basket_id, seeded.young_basket_id])
    assert counter.count >= len(written)


@pytest.mark.asyncio
async def test_a_run_that_dies_half_way_keeps_the_baskets_it_finished(
    engine: AsyncEngine, migrated_hypertables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial run is resumable: finished baskets are on disk, in id order.

    With one transaction for the run, a failure on the second basket threw the first one away
    and the re-run started from nothing. The upsert is idempotent per ``(basket_id,
    as_of_date)``, so re-running rewrites the finished rows identically.
    """
    seeded = await _seed_overlapping_catalog(engine)
    original = curated_metrics_service._write_metric_values
    calls = {"n": 0}

    async def dying_write(
        session: AsyncSession,
        values: BasketMetricValues,
        as_of: dt.date,
        context: CatalogContext,
    ) -> dict[str, object]:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("the job died half-way")
        return await original(session, values, as_of, context)

    monkeypatch.setattr(curated_metrics_service, "_write_metric_values", dying_write)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        with pytest.raises(RuntimeError, match="died half-way"):
            await compute_all_metrics(session, AS_OF, now=NOW)
        await session.rollback()

    async with factory() as session:
        survived = (
            (await session.execute(select(CbMetrics.basket_id).order_by(CbMetrics.basket_id)))
            .scalars()
            .all()
        )
    assert list(survived) == [min(seeded.old_basket_id, seeded.young_basket_id)]


@pytest.mark.asyncio
async def test_job_writes_the_basis_and_span_it_measured(
    engine: AsyncEngine, migrated_hypertables: None
) -> None:
    """End to end: the facts reach the row, and the two baskets get different bases.

    The old basket clears 252 returns and is measured on the trailing window; the young one has
    fewer and is measured on its full history. Publishing both as one unlabelled number is what
    the column exists to stop.
    """
    seeded = await _seed_overlapping_catalog(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await compute_all_metrics(session, AS_OF, now=NOW)
        rows = {
            row.basket_id: row for row in (await session.execute(select(CbMetrics))).scalars().all()
        }

    old = rows[seeded.old_basket_id]
    young = rows[seeded.young_basket_id]
    assert old.volatility_basis == "BASKET_252D"
    assert young.volatility_basis == "BASKET_FULL_HISTORY"
    assert old.months_available == 65  # 2019-01-02 -> 2024-06-03
    assert young.months_available == 5  # 2024-01-02 -> 2024-06-03
    assert old.return_convention == RETURN_CONVENTION
    assert old.dividends_included == DIVIDENDS_INCLUDED
