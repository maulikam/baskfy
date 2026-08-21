"""The Prompt 20 tasks and their schedule — deliverables 3 and 4, from the worker's side.

No broker and no database: what is asserted here is the *wiring*. The dispatch itself is exercised
against a real database in ``services/api/tests/test_api_alerts.py``, and the delivery in
``services/api/tests/test_api_webhooks.py``, because both live in ``baskfy_api`` — the worker
depends on it and not the other way round.
"""

from __future__ import annotations

from celery.schedules import crontab

from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_DEFAULT, TASK_ROUTES, build_celery
from baskfy_worker.settings import WorkerSettings
from baskfy_worker.tasks import alerts as task_module

ALERT_TASK = "baskfy.alerts.dispatch"
SWEEP_TASK = "baskfy.alerts.sweep_webhooks"


def settings() -> WorkerSettings:
    return WorkerSettings(_env_file=None, redis_url="redis://localhost:6380/0")


class TestRouting:
    def test_alert_tasks_route_to_the_default_queue(self) -> None:
        """Short and latency-sensitive: an alert must never queue behind a backfill chunk."""
        assert TASK_ROUTES["baskfy.alerts.*"] == {"queue": QUEUE_DEFAULT}

    def test_both_tasks_are_registered(self) -> None:
        app = build_celery(settings())
        assert ALERT_TASK in app.tasks
        assert SWEEP_TASK in app.tasks


class TestSchedule:
    def test_the_dispatch_runs_after_the_publish_deadline(self) -> None:
        """docs/11 §Reliability puts publish at 20:15 IST. The alert follows it, never with it."""
        entry = BEAT_SCHEDULE["dispatch-screen-alerts"]
        assert entry["task"] == ALERT_TASK
        assert entry["schedule"] == crontab(hour=20, minute=30, day_of_week="mon-fri")

    def test_the_dispatch_only_runs_on_weekdays(self) -> None:
        """NSE does not publish at the weekend, so neither does an alert."""
        schedule = BEAT_SCHEDULE["dispatch-screen-alerts"]["schedule"]
        assert isinstance(schedule, crontab)
        assert schedule.day_of_week == {1, 2, 3, 4, 5}

    def test_the_webhook_sweep_runs_often_enough_for_the_first_retry(self) -> None:
        """The first backoff is 30 s; a sweep every two minutes does not delay it materially."""
        entry = BEAT_SCHEDULE["sweep-webhook-deliveries"]
        assert entry["task"] == SWEEP_TASK
        assert entry["schedule"] == crontab(minute="*/2")


class TestTheSweepBatch:
    def test_it_is_bounded(self) -> None:
        """An unbounded sweep is a task that holds a worker for as long as the backlog is deep."""
        assert 0 < task_module.SWEEP_BATCH <= 1000
