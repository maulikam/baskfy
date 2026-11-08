"""SC2 Beat entry for curated-basket EOD metrics."""

from __future__ import annotations

from typing import cast

from celery.schedules import crontab

from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_COMPUTE, TASK_ROUTES, build_celery
from baskfy_worker.settings import WorkerSettings


def _settings() -> WorkerSettings:
    return WorkerSettings(
        _env_file=None,
        redis_url="redis://localhost:6380/0",
        celery_broker_url="",
        task_max_retries=3,
        task_retry_backoff_seconds=60,
    )


def test_cb_metrics_beat_entry_exists() -> None:
    entry = BEAT_SCHEDULE["cb-eod-metrics"]
    assert entry["task"] == "baskfy.cb.compute_metrics"
    schedule = cast(crontab, entry["schedule"])
    assert schedule.hour == {20}
    assert schedule.minute == {20}
    # `BEAT_SCHEDULE` is `dict[str, dict[str, object]]`, so `entry["options"]` is `object`.
    # Narrowing it is the assertion that the entry has an options mapping at all — the
    # alternative was a `cast`, which would assert the shape instead of checking it.
    options = entry["options"]
    assert isinstance(options, dict), "the beat entry must carry an options mapping"
    assert options["queue"] == QUEUE_COMPUTE


def test_cb_metrics_task_is_registered_and_routed() -> None:
    app = build_celery(_settings())
    assert "baskfy.cb.compute_metrics" in app.tasks
    assert TASK_ROUTES["baskfy.cb.*"]["queue"] == QUEUE_COMPUTE
