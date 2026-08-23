"""DB, schedule, idempotence, coverage, and scale proofs for the SC2 EOD metrics job."""

from __future__ import annotations

import datetime as dt
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from decimal import Decimal
from time import perf_counter

import helpers
import pytest
from celery.schedules import crontab
from sqlalchemy import delete, event, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import baskfy_worker.curated_metrics as metrics_job
from baskfy_api.curated_baskets import CuratedBasketRepository
from baskfy_api.seed import seed_published_run
from baskfy_core.curated_baskets import ConstituentDraft, VersionDraft
from baskfy_core.curated_metrics import TargetWeight, VersionWeights
from baskfy_core.models import (
    BasketMetrics,
    CuratedBasket,
    CuratedManager,
    OhlcvDaily,
)
from baskfy_worker.celery_app import (
    BEAT_SCHEDULE,
    QUEUE_COMPUTE,
    TASK_ROUTES,
    build_celery,
)
from baskfy_worker.curated_metrics import CuratedMetricsJobError, compute_curated_metrics
from baskfy_worker.orchestrator import PipelineOutcome
from baskfy_worker.steps import RunStatus
from baskfy_worker.tasks import celery_tasks

pytestmark = pytest.mark.db

AS_OF = dt.date(2026, 8, 18)
LAUNCHED = dt.date(2026, 8, 12)
PRICE_DAYS = (
    dt.date(2026, 8, 10),
    dt.date(2026, 8, 11),
    dt.date(2026, 8, 12),
    dt.date(2026, 8, 13),
    dt.date(2026, 8, 14),
    dt.date(2026, 8, 17),
    AS_OF,
)


@dataclass(frozen=True)
class _Queued:
    id: str


async def add_price(
    session: AsyncSession,
    instrument_id: int,
    day: dt.date,
    adjusted: Decimal,
    raw: Decimal,
) -> None:
    session.add(
        OhlcvDaily(
            instrument_id=instrument_id,
            date=day,
            open=raw,
            high=raw,
            low=raw,
            close=adjusted,
            close_raw=raw,
            volume=1_000,
            volume_raw=1_000,
            adj_factor=Decimal(1),
            source="nse",
        )
    )


async def add_price_series(
    session: AsyncSession,
    instrument_id: int,
    adjusted: tuple[str, ...],
    raw: tuple[str, ...] | None = None,
    *,
    omit: dt.date | None = None,
) -> None:
    raw_values = raw or adjusted
    assert len(adjusted) == len(PRICE_DAYS) == len(raw_values)
    for day, adjusted_value, raw_value in zip(PRICE_DAYS, adjusted, raw_values, strict=True):
        if day != omit:
            await add_price(
                session,
                instrument_id,
                day,
                Decimal(adjusted_value),
                Decimal(raw_value),
            )
    await session.flush()


async def add_basket(
    session: AsyncSession,
    suffix: str,
    constituents: tuple[tuple[int, str], ...],
    *,
    launched_at: dt.date = LAUNCHED,
) -> int:
    manager = await session.scalar(
        select(CuratedManager).where(CuratedManager.slug == "baskfy-engine")
    )
    assert manager is not None
    basket = CuratedBasket(
        slug=f"metrics-{suffix}",
        name=f"Metrics {suffix}",
        manager_id=manager.id,
        type="STOCK",
        access="FREE",
        visibility="PUBLISHED",
        categories=["Test"],
        description_md="fixture",
        rationale_md="fixture",
        rebalance_frequency="WEEKLY",
        benchmark_instrument_id=None,
        launched_at=launched_at,
        next_review_at=None,
        source="MANUAL",
        scan_strategy_key=None,
        archived_at=None,
    )
    session.add(basket)
    await session.flush()
    await CuratedBasketRepository(session).create_version(
        basket.id,
        VersionDraft(
            effective_date=LAUNCHED,
            constituents=tuple(
                ConstituentDraft(instrument_id, "Equity", Decimal(weight))
                for instrument_id, weight in constituents
            ),
        ),
    )
    return basket.id


async def seed_two_asset_market(session: AsyncSession) -> tuple[int, int]:
    first = await helpers.make_instrument(session, "METRICA")
    second = await helpers.make_instrument(session, "METRICB")
    await add_price_series(
        session,
        first,
        ("100", "101", "102", "103", "104", "105", "106"),
        ("50", "50.5", "51", "51.5", "52", "52.5", "53"),
    )
    await add_price_series(
        session,
        second,
        ("200", "198", "202", "204", "208", "212", "216"),
    )
    return first, second


async def publish(session: AsyncSession) -> None:
    assert await seed_published_run(session, AS_OF, data_version=77) in (0, 1)


@pytest.mark.asyncio
async def test_metrics_job_writes_exact_raw_affordability_and_adjusted_returns(
    session: AsyncSession,
) -> None:
    first, second = await seed_two_asset_market(session)
    basket_id = await add_basket(session, "exact", ((first, "0.4000"), (second, "0.6000")))
    await publish(session)

    result = await compute_curated_metrics(session, AS_OF)

    assert result.as_dict() == {
        "as_of_date": "2026-08-18",
        "status": "computed",
        "baskets": 1,
        "written": 1,
        "unchanged": 0,
        "price_rows": 14,
    }
    row = await session.get(BasketMetrics, (basket_id, AS_OF))
    assert row is not None
    # 53 / .4 = 132.5, while 216 / .6 = 360; raw prints, never adjusted closes.
    assert row.min_amount == Decimal("360.00")
    assert row.since_inception_pct == Decimal("5.7201485562")
    assert row.volatility_value > 0
    assert row.volatility_bucket in {"LOW", "MED", "HIGH"}


@pytest.mark.asyncio
async def test_metrics_job_is_idempotent_including_computed_timestamp(
    session: AsyncSession,
) -> None:
    first, second = await seed_two_asset_market(session)
    basket_id = await add_basket(
        session,
        "idempotent",
        ((first, "0.5000"), (second, "0.5000")),
    )
    await publish(session)
    first_result = await compute_curated_metrics(session, AS_OF)
    row = await session.get(BasketMetrics, (basket_id, AS_OF))
    assert row is not None
    assert row.min_amount == Decimal("432.00")
    assert row.since_inception_pct == Decimal("5.4189511451")
    snapshot = metrics_job._stored_metric_content(row), row.computed_at

    second_result = await compute_curated_metrics(session, AS_OF)
    await session.refresh(row)

    assert first_result.written == 1
    assert second_result.written == 0
    assert second_result.unchanged == 1
    assert (metrics_job._stored_metric_content(row), row.computed_at) == snapshot


@pytest.mark.asyncio
async def test_missing_price_coverage_rolls_back_all_baskets_before_any_write(
    session: AsyncSession,
) -> None:
    first, second = await seed_two_asset_market(session)
    await add_basket(session, "good", ((first, "0.5000"), (second, "0.5000")))
    third = await helpers.make_instrument(session, "METRICC")
    fourth = await helpers.make_instrument(session, "METRICD")
    await add_price_series(session, third, ("90", "91", "92", "93", "94", "95", "96"))
    await add_price_series(
        session,
        fourth,
        ("70", "71", "72", "73", "74", "75", "76"),
        omit=dt.date(2026, 8, 17),
    )
    await add_basket(session, "broken", ((third, "0.5000"), (fourth, "0.5000")))
    await publish(session)

    with pytest.raises(CuratedMetricsJobError, match="incomplete adjusted-close coverage"):
        await compute_curated_metrics(session, AS_OF)

    assert await session.scalar(select(func.count()).select_from(BasketMetrics)) == 0


@pytest.mark.asyncio
async def test_late_first_price_date_is_rejected_as_incomplete_coverage(
    session: AsyncSession,
) -> None:
    first, second = await seed_two_asset_market(session)
    # Remove inception-day observations while retaining later prices and prehistory.
    await session.execute(
        delete(OhlcvDaily).where(
            OhlcvDaily.instrument_id.in_((first, second)), OhlcvDaily.date == LAUNCHED
        )
    )
    await add_basket(session, "late", ((first, "0.5000"), (second, "0.5000")))
    await publish(session)

    with pytest.raises(CuratedMetricsJobError, match="incomplete adjusted-close coverage"):
        await compute_curated_metrics(session, AS_OF)

    assert await session.scalar(select(func.count()).select_from(BasketMetrics)) == 0


@pytest.mark.asyncio
async def test_removed_then_readded_current_name_has_contiguous_volatility_fallback(
    session: AsyncSession,
) -> None:
    first, second = await seed_two_asset_market(session)
    basket_id = await add_basket(session, "readded", ((first, "0.5000"), (second, "0.5000")))
    third = await helpers.make_instrument(session, "METRICC")
    fourth = await helpers.make_instrument(session, "METRICD")
    await add_price_series(session, third, ("90", "91", "92", "93", "94", "95", "96"))
    await add_price_series(session, fourth, ("70", "71", "72", "73", "74", "75", "76"))
    repository = CuratedBasketRepository(session)
    await repository.create_version(
        basket_id,
        VersionDraft(
            effective_date=dt.date(2026, 8, 14),
            constituents=(
                ConstituentDraft(third, "Equity", Decimal("0.5000")),
                ConstituentDraft(fourth, "Equity", Decimal("0.5000")),
            ),
        ),
    )
    await repository.create_version(
        basket_id,
        VersionDraft(
            effective_date=AS_OF,
            constituents=(
                ConstituentDraft(first, "Equity", Decimal("0.5000")),
                ConstituentDraft(second, "Equity", Decimal("0.5000")),
            ),
        ),
    )
    await publish(session)

    result = await compute_curated_metrics(session, AS_OF)

    assert result.written == 1
    row = await session.get(BasketMetrics, (basket_id, AS_OF))
    assert row is not None and row.volatility_value > 0


@pytest.mark.asyncio
async def test_bounded_price_stream_refuses_excess_before_write(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = await seed_two_asset_market(session)
    await add_basket(session, "bounded", ((first, "0.5000"), (second, "0.5000")))
    await publish(session)
    monkeypatch.setattr(metrics_job, "MAX_PRICE_ROWS", 5)

    with pytest.raises(CuratedMetricsJobError, match="bounded limit 5"):
        await compute_curated_metrics(session, AS_OF)

    assert await session.scalar(select(func.count()).select_from(BasketMetrics)) == 0


def fifteen_year_trading_days() -> tuple[dt.date, ...]:
    days: list[dt.date] = []
    cursor = dt.date(2011, 1, 3)
    while len(days) < 4_000:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += dt.timedelta(days=1)
    return tuple(days)


def test_bounded_interval_plan_fits_fifteen_years_of_fixed_fifty_names() -> None:
    trading_days = fifteen_year_trading_days()
    constituents = tuple(
        TargetWeight(instrument_id, Decimal("0.0200")) for instrument_id in range(1, 51)
    )
    definition = metrics_job._BasketDefinition(
        1,
        trading_days[0],
        (VersionWeights(trading_days[0], constituents),),
    )

    intervals = metrics_job._bounded_price_intervals((definition,), trading_days, trading_days[-1])
    estimated_rows = sum(
        bisect_right(trading_days, end) - bisect_left(trading_days, start)
        for _instrument_id, start, end in intervals
    )

    assert len(intervals) == 50
    assert estimated_rows == 200_000
    assert estimated_rows <= metrics_job.MAX_PRICE_ROWS


def test_bounded_interval_plan_refuses_pathological_weekly_full_churn_before_sql() -> None:
    trading_days = fifteen_year_trading_days()
    versions = tuple(
        VersionWeights(
            effective_date=trading_days[version_index * 5],
            constituents=tuple(
                TargetWeight(version_index * 50 + offset + 1, Decimal("0.0200"))
                for offset in range(50)
            ),
        )
        for version_index in range(800)
    )
    definition = metrics_job._BasketDefinition(1, trading_days[0], versions)

    intervals = metrics_job._price_intervals((definition,), trading_days, trading_days[-1])
    estimated_rows = sum(
        bisect_right(trading_days, end) - bisect_left(trading_days, start)
        for _instrument_id, start, end in intervals
    )

    assert len(intervals) == 40_000
    assert estimated_rows <= metrics_job.MAX_PRICE_ROWS
    assert estimated_rows < len(definition.instrument_ids) * len(trading_days) // 500
    with pytest.raises(CuratedMetricsJobError, match="interval count exceeds bounded limit"):
        metrics_job._bounded_price_intervals((definition,), trading_days, trading_days[-1])


@pytest.mark.asyncio
async def test_query_count_and_shared_price_memory_stay_flat_across_twenty_baskets(
    session: AsyncSession,
    engine: AsyncEngine,
) -> None:
    first, second = await seed_two_asset_market(session)
    for index in range(20):
        await add_basket(
            session,
            f"scale-{index}",
            ((first, "0.5000"), (second, "0.5000")),
        )
    await publish(session)
    await session.flush()
    statements: list[str] = []

    def count_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _many: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", count_statement)
    started = perf_counter()
    try:
        result = await compute_curated_metrics(session, AS_OF)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_statement)
    elapsed = perf_counter() - started

    assert result.baskets == result.written == 20
    assert result.price_rows == 14  # shared once, not 14 rows per basket
    assert len(statements) <= 7
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_unpublished_day_is_an_honest_no_write(session: AsyncSession) -> None:
    first, second = await seed_two_asset_market(session)
    await add_basket(session, "unpublished", ((first, "0.5000"), (second, "0.5000")))

    result = await compute_curated_metrics(session, AS_OF)

    assert result.status == "not-published"
    assert await session.scalar(select(func.count()).select_from(BasketMetrics)) == 0


def test_beat_schedule_runs_metrics_after_publish_deadline_on_weekdays() -> None:
    entry = BEAT_SCHEDULE["curated-basket-metrics"]
    schedule = entry["schedule"]

    assert isinstance(schedule, crontab)
    assert schedule.hour == {20}
    assert schedule.minute == {20}
    assert schedule.day_of_week == {1, 2, 3, 4, 5}
    assert entry["task"] == "baskfy.compute.curated_metrics"
    assert entry["options"] == {"queue": QUEUE_COMPUTE}


def test_route_and_celery_binding_are_registered_without_execution_surface() -> None:
    assert TASK_ROUTES["baskfy.compute.*"] == {"queue": QUEUE_COMPUTE}
    celery = build_celery()
    assert "baskfy.compute.curated_metrics" in celery.tasks
    assert all("execute" not in name for name in celery.tasks if name.startswith("baskfy.compute"))


def test_beat_recovery_is_backed_by_causal_delayed_publish_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatched: list[tuple[list[str], str]] = []

    def fake_apply_async(*, args: list[str], queue: str) -> _Queued:
        dispatched.append((args, queue))
        return _Queued("metrics-task-1")

    monkeypatch.setattr(celery_tasks.curated_metrics_task, "apply_async", fake_apply_async)
    late_success = PipelineOutcome(
        trade_date=AS_OF,
        status=RunStatus.SUCCEEDED,
        data_version=99,
    )
    failed = PipelineOutcome(trade_date=AS_OF, status=RunStatus.FAILED)

    assert celery_tasks.enqueue_curated_metrics_after_publish(late_success) == "metrics-task-1"
    assert dispatched == [(["2026-08-18"], QUEUE_COMPUTE)]
    assert celery_tasks.enqueue_curated_metrics_after_publish(failed) is None
    assert dispatched == [(["2026-08-18"], QUEUE_COMPUTE)]
