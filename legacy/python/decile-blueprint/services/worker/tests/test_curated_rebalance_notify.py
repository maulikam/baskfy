"""T8.3 — rebalance notification is delivered once; second run is a no-op."""

from __future__ import annotations

import datetime as dt
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
from celery.schedules import crontab

from baskfy_api.email.sender import Mailer
from baskfy_api.email.templates import Message
from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_COMPUTE, TASK_ROUTES, build_celery
from baskfy_worker.settings import WorkerSettings
from baskfy_worker.tasks import curated_rebalance_notify
from baskfy_worker.tasks.curated_rebalance_notify import run_curated_rebalance_notify


def _settings() -> WorkerSettings:
    return WorkerSettings(
        _env_file=None,
        redis_url="redis://localhost:6380/0",
        celery_broker_url="",
        task_max_retries=3,
        task_retry_backoff_seconds=60,
    )


class RecordingTransport:
    def __init__(self) -> None:
        self.sent: list[Message] = []

    async def send(self, message: Message) -> None:
        self.sent.append(message)


def test_cb_rebalance_notify_beat_entry_exists() -> None:
    entry = BEAT_SCHEDULE["cb-rebalance-notify"]
    assert entry["task"] == "baskfy.cb.rebalance_notify"
    assert isinstance(entry["schedule"], crontab)
    options = entry["options"]
    assert isinstance(options, dict)
    assert options["queue"] == QUEUE_COMPUTE


def test_cb_rebalance_notify_task_is_registered() -> None:
    app = build_celery(_settings())
    assert "baskfy.cb.rebalance_notify" in app.tasks
    assert TASK_ROUTES["baskfy.cb.*"]["queue"] == QUEUE_COMPUTE


def test_notify_source_has_no_broker_path() -> None:
    source = inspect.getsource(curated_rebalance_notify)
    for forbidden in ("OrderGateway", "place_order", "kc.place_order"):
        assert forbidden not in source


@pytest.mark.asyncio
async def test_first_send_stamps_notified_at_second_is_noop() -> None:
    transport = RecordingTransport()
    mailer = Mailer(transport)
    now = dt.datetime(2026, 8, 24, 10, 0, tzinfo=dt.UTC)

    action = MagicMock()
    action.id = 11
    action.user_id = 42
    action.payload = {"basket_id": 7, "version_no": 3}
    action.dismissed_at = None
    action.resolved_at = None

    user = MagicMock()
    user.email = "owner@example.com"
    basket = MagicMock()
    basket.name = "Momentum"

    scalars = MagicMock()
    scalars.all = MagicMock(return_value=[action])
    session = AsyncMock()
    session.scalars = AsyncMock(return_value=scalars)
    session.get = AsyncMock(side_effect=[user, basket])
    session.flush = AsyncMock()

    first = await run_curated_rebalance_notify(session, mailer=mailer, now=now)
    assert first["sent"] == 1
    assert len(transport.sent) == 1
    assert "Momentum" in transport.sent[0].subject
    assert action.payload["notified_at"] == now.isoformat()
    assert action.payload["delivery"] == "email"

    session.get = AsyncMock(side_effect=[user, basket])
    second = await run_curated_rebalance_notify(session, mailer=mailer, now=now)
    assert second["sent"] == 0
    assert second["skipped"] == 1
    assert len(transport.sent) == 1
