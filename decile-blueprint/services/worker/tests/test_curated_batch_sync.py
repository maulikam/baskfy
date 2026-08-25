"""T8.2 — desk fills promote PLANNED batches; synthetic ids stay PLANNED."""

from __future__ import annotations

import datetime as dt
import inspect
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from celery.schedules import crontab

from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_COMPUTE, TASK_ROUTES, build_celery
from baskfy_worker.settings import WorkerSettings
from baskfy_worker.tasks import curated_batch_sync
from baskfy_worker.tasks.curated_batch_sync import (
    DeskOrderFill,
    classify_fills,
    run_curated_batch_sync,
)


def _settings() -> WorkerSettings:
    return WorkerSettings(
        _env_file=None,
        redis_url="redis://localhost:6380/0",
        celery_broker_url="",
        task_max_retries=3,
        task_retry_backoff_seconds=60,
    )


def test_cb_sync_batches_beat_entry_exists() -> None:
    entry = BEAT_SCHEDULE["cb-sync-batches"]
    assert entry["task"] == "baskfy.cb.sync_batches"
    assert isinstance(entry["schedule"], crontab)
    options = entry["options"]
    assert isinstance(options, dict)
    assert options["queue"] == QUEUE_COMPUTE


def test_cb_sync_batches_task_is_registered_and_routed() -> None:
    app = build_celery(_settings())
    assert "baskfy.cb.sync_batches" in app.tasks
    assert TASK_ROUTES["baskfy.cb.*"]["queue"] == QUEUE_COMPUTE


def test_sync_source_has_no_broker_path() -> None:
    source = inspect.getsource(curated_batch_sync)
    for forbidden in ("OrderGateway", "place_order", "kc.place_order"):
        assert forbidden not in source


def test_classify_settled_partial_and_untouched() -> None:
    settled = (
        DeskOrderFill(planned_qty=Decimal("10"), filled_qty=Decimal("10")),
        DeskOrderFill(planned_qty=Decimal("5"), filled_qty=Decimal("5")),
    )
    assert classify_fills(settled) == "EXECUTED"
    partial = (
        DeskOrderFill(planned_qty=Decimal("10"), filled_qty=Decimal("4")),
        DeskOrderFill(planned_qty=Decimal("5"), filled_qty=Decimal("0")),
    )
    assert classify_fills(partial) == "PARTIAL"
    none = (DeskOrderFill(planned_qty=Decimal("10"), filled_qty=Decimal("0")),)
    assert classify_fills(none) is None


class _MapReader:
    def __init__(self, mapping: dict[str, tuple[DeskOrderFill, ...] | None]) -> None:
        self._mapping = mapping

    async def fills_for_plan(self, desk_plan_id: str) -> tuple[DeskOrderFill, ...] | None:
        if desk_plan_id not in self._mapping:
            return None
        return self._mapping[desk_plan_id]


def _batch(*, plan_id: str, status: str = "PLANNED") -> MagicMock:
    row = MagicMock()
    row.desk_plan_id = plan_id
    row.status = status
    row.executed_at = None
    return row


@pytest.mark.asyncio
async def test_settled_marks_executed() -> None:
    batch = _batch(plan_id="desk-v1")
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=[batch])
    session = AsyncMock()
    session.scalars = AsyncMock(return_value=scalars)
    session.flush = AsyncMock()
    reader = _MapReader(
        {
            "desk-v1": (DeskOrderFill(planned_qty=Decimal("10"), filled_qty=Decimal("10")),),
        }
    )
    now = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)
    result = await run_curated_batch_sync(session, reader=reader, now=now)
    assert result["executed"] == 1
    assert batch.status == "EXECUTED"
    assert batch.executed_at == now


@pytest.mark.asyncio
async def test_partial_fill_marks_partial() -> None:
    batch = _batch(plan_id="desk-v2")
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=[batch])
    session = AsyncMock()
    session.scalars = AsyncMock(return_value=scalars)
    session.flush = AsyncMock()
    reader = _MapReader(
        {
            "desk-v2": (DeskOrderFill(planned_qty=Decimal("10"), filled_qty=Decimal("3")),),
        }
    )
    result = await run_curated_batch_sync(session, reader=reader)
    assert result["partial"] == 1
    assert batch.status == "PARTIAL"
    assert batch.executed_at is None


@pytest.mark.asyncio
async def test_synthetic_cb_sim_stays_planned() -> None:
    batch = _batch(plan_id="cb-sim-aaaaaaaa")
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=[batch])
    session = AsyncMock()
    session.scalars = AsyncMock(return_value=scalars)
    session.flush = AsyncMock()
    reader = _MapReader(
        {
            "cb-sim-aaaaaaaa": (
                DeskOrderFill(planned_qty=Decimal("10"), filled_qty=Decimal("10")),
            ),
        }
    )
    result = await run_curated_batch_sync(session, reader=reader)
    assert result["executed"] == 0
    assert result["skipped"] == 1
    assert batch.status == "PLANNED"
