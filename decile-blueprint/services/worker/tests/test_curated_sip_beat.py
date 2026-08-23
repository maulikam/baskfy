"""SC7 Beat entry for curated-basket SIP REMINDER fires (leaf 2.1)."""

from __future__ import annotations

import datetime as dt
import inspect
from decimal import Decimal
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from celery.schedules import crontab

from baskfy_core.curated_sip import SIP_DUE, SIP_MODE_REMINDER, SIP_STATUS_ACTIVE
from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_COMPUTE, TASK_ROUTES, build_celery
from baskfy_worker.settings import WorkerSettings
from baskfy_worker.tasks import curated_sip
from baskfy_worker.tasks.curated_sip import run_curated_sip_reminders


def _settings() -> WorkerSettings:
    return WorkerSettings(
        _env_file=None,
        redis_url="redis://localhost:6380/0",
        celery_broker_url="",
        task_max_retries=3,
        task_retry_backoff_seconds=60,
    )


def test_cb_sip_reminders_beat_entry_exists() -> None:
    entry = BEAT_SCHEDULE["cb-sip-reminders"]
    assert entry["task"] == "baskfy.cb.sip_reminders"
    schedule = cast(crontab, entry["schedule"])
    assert schedule.hour == {9}
    assert schedule.minute == {0}
    assert entry["options"]["queue"] == QUEUE_COMPUTE
    # Sibling SC2 entry must survive the merge.
    assert BEAT_SCHEDULE["cb-eod-metrics"]["task"] == "baskfy.cb.compute_metrics"


def test_cb_sip_reminders_task_is_registered_and_routed() -> None:
    app = build_celery(_settings())
    assert "baskfy.cb.sip_reminders" in app.tasks
    assert TASK_ROUTES["baskfy.cb.*"]["queue"] == QUEUE_COMPUTE


def test_sip_task_source_has_no_broker_path() -> None:
    source = inspect.getsource(curated_sip)
    for forbidden in ("OrderGateway", "place_order", "kc.place_order"):
        assert forbidden not in source


@pytest.mark.asyncio
async def test_run_raises_sip_due_idempotently() -> None:
    as_of = dt.date(2026, 8, 21)
    trading = {as_of, dt.date(2026, 9, 21)}

    plan = MagicMock()
    plan.id = 7
    plan.investment_id = 3
    plan.amount = Decimal("5000")
    plan.day_of_month = 21
    plan.mode = SIP_MODE_REMINDER
    plan.status = SIP_STATUS_ACTIVE
    plan.next_fire_date = as_of

    investment = MagicMock()
    investment.user_id = 42

    plan_scalars = MagicMock()
    plan_scalars.all = MagicMock(return_value=[plan])
    fired_scalars = MagicMock()
    fired_scalars.all = MagicMock(return_value=[])

    session = AsyncMock()
    session.scalars = AsyncMock(side_effect=[fired_scalars, plan_scalars])
    session.get = AsyncMock(return_value=investment)
    session.add = MagicMock()
    session.flush = AsyncMock()

    first = await run_curated_sip_reminders(session, as_of, trading_dates=trading)
    assert first["raised"] == 1
    assert first["examined"] == 1
    session.add.assert_called_once()
    pending = session.add.call_args.args[0]
    assert pending.type == SIP_DUE
    assert pending.user_id == 42
    assert pending.payload["fire_key"] == "7:2026-08"
    assert pending.payload["mode"] == SIP_MODE_REMINDER
    assert plan.next_fire_date == dt.date(2026, 9, 21)

    # Second pass: fire_key already present → no second pending row.
    session.add.reset_mock()
    fired_scalars.all = MagicMock(return_value=[{"fire_key": "7:2026-08"}])
    plan.next_fire_date = as_of  # re-due for the test; key still blocks
    plan_scalars.all = MagicMock(return_value=[plan])
    session.scalars = AsyncMock(side_effect=[fired_scalars, plan_scalars])
    session.get = AsyncMock(return_value=investment)

    second = await run_curated_sip_reminders(session, as_of, trading_dates=trading)
    assert second["raised"] == 0
    session.add.assert_not_called()
