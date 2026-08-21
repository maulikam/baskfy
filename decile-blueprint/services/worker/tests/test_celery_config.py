"""Celery configuration (Prompt 3 deliverable 1).

    "Celery configured with Redis broker/result backend, task-level retries, and separate queues:
     `ingest`, `compute`, `backtest`, `default`."

No broker is needed to assert any of this — the configuration is the deliverable.
"""

from __future__ import annotations

import datetime as dt

from celery.schedules import crontab

from decile_worker.celery_app import (
    BEAT_SCHEDULE,
    IST_NAME,
    QUEUE_BACKTEST,
    QUEUE_COMPUTE,
    QUEUE_DEFAULT,
    QUEUE_INGEST,
    QUEUES,
    TASK_ROUTES,
    build_celery,
)
from decile_worker.settings import WorkerSettings


def settings(
    *,
    redis_url: str = "redis://localhost:6380/0",
    celery_broker_url: str = "",
    task_max_retries: int = 3,
    task_retry_backoff_seconds: int = 60,
) -> WorkerSettings:
    """Settings built explicitly rather than by kwargs splat, so mypy checks each field."""
    return WorkerSettings(
        _env_file=None,
        redis_url=redis_url,
        celery_broker_url=celery_broker_url,
        task_max_retries=task_max_retries,
        task_retry_backoff_seconds=task_retry_backoff_seconds,
    )


class TestQueues:
    def test_the_four_queues_the_prompt_names_exist(self) -> None:
        assert set(QUEUES) == {"ingest", "compute", "backtest", "default"}

    def test_orchestration_defaults_to_the_default_queue(self) -> None:
        """Short, latency-sensitive work must never queue behind a two-hour backfill chunk."""
        assert build_celery(settings()).conf.task_default_queue == QUEUE_DEFAULT

    def test_ingest_work_is_routed_away_from_everything_else(self) -> None:
        assert TASK_ROUTES["decile.ingest.*"]["queue"] == QUEUE_INGEST

    def test_compute_and_backtest_have_their_own_queues(self) -> None:
        """docs/03 §"Scaling plan" step 4 moves backtest fan-out to its own pool; the queue is
        already there, so that becomes a deployment change rather than a code change."""
        assert TASK_ROUTES["decile.compute.*"]["queue"] == QUEUE_COMPUTE
        assert TASK_ROUTES["decile.backtest.*"]["queue"] == QUEUE_BACKTEST


class TestBroker:
    def test_redis_is_the_broker_and_the_result_backend(self) -> None:
        """docs/02: "Cache / queue broker — Redis 7"."""
        app = build_celery(settings(redis_url="redis://localhost:6380/3"))
        assert app.conf.broker_url == "redis://localhost:6380/3"
        assert app.conf.result_backend == "redis://localhost:6380/3"

    def test_the_broker_can_be_pointed_somewhere_else(self) -> None:
        app = build_celery(
            settings(redis_url="redis://a:6379/0", celery_broker_url="redis://b:6379/1")
        )
        assert app.conf.broker_url == "redis://b:6379/1"


class TestReliability:
    def test_tasks_are_acknowledged_late(self) -> None:
        """A step that dies with its worker must be re-delivered, not lost."""
        assert build_celery(settings()).conf.task_acks_late is True

    def test_a_lost_worker_rejects_rather_than_drops(self) -> None:
        assert build_celery(settings()).conf.task_reject_on_worker_lost is True

    def test_one_long_task_at_a_time_per_process(self) -> None:
        """Prefetching several would let one slow ingest chunk hold up work another worker
        could have taken."""
        assert build_celery(settings()).conf.worker_prefetch_multiplier == 1

    def test_retries_are_configured_at_task_level(self) -> None:
        app = build_celery(settings(task_max_retries=7, task_retry_backoff_seconds=30))
        assert app.conf.task_annotations["*"]["max_retries"] == 7
        assert app.conf.task_default_retry_delay == 30


class TestSchedule:
    def test_beat_runs_in_ist(self) -> None:
        """docs/09 §Schedule states every time as IST wall-clock, tied to the NSE session.

        Expressing them in UTC would silently shift the whole pipeline twice a year for anyone
        reading it from a country that observes DST.
        """
        app = build_celery(settings())
        assert app.conf.timezone == IST_NAME
        assert app.conf.enable_utc is False

    def test_the_nightly_run_is_scheduled_on_weekdays_only(self) -> None:
        """NSE does not trade at the weekend; Beat should not fire then."""
        entry = BEAT_SCHEDULE["refresh-reference-data"]["schedule"]
        assert isinstance(entry, crontab)
        # Celery numbers days from Sunday=0, so Monday-Friday is {1..5}.
        assert entry.day_of_week == {1, 2, 3, 4, 5}

    def test_the_nightly_run_fires_after_nse_files_settle(self) -> None:
        """docs/03: "NSE EOD files settle ~18:00-19:00 IST"; docs/09 puts the first job at 18:45."""
        entry = BEAT_SCHEDULE["refresh-reference-data"]["schedule"]
        assert isinstance(entry, crontab)
        assert entry.hour == {18}
        assert entry.minute == {45}

    def test_the_weekly_integrity_audit_is_scheduled(self) -> None:
        """docs/09 §Schedule: "Sat 02:00 — full-history integrity audit"."""
        entry = BEAT_SCHEDULE["weekly-integrity-audit"]["schedule"]
        assert isinstance(entry, crontab)
        assert entry.day_of_week == {6}  # Saturday
        assert entry.hour == {2}


class TestRegisteredTasks:
    def test_the_pipeline_tasks_are_registered(self) -> None:
        app = build_celery(settings())
        names = {name for name in app.tasks if name.startswith("decile.")}
        assert "decile.pipeline.nightly" in names
        assert "decile.compute.reprocess_instrument" in names
        assert "decile.pipeline.integrity_audit" in names

    def test_the_backtest_task_is_registered_and_routed_to_its_own_queue(self) -> None:
        """PROMPTS.md Prompt 15 §4: "Celery task on the `backtest` queue".

        Registration and routing are separate facts and both matter: a task that exists but is
        not routed runs on `default`, where a fifteen-year simulation would sit in front of the
        nightly publish.
        """
        app = build_celery(settings())
        assert "decile.backtest.run" in app.tasks
        assert TASK_ROUTES["decile.backtest.*"]["queue"] == QUEUE_BACKTEST

    def test_results_expire(self) -> None:
        """Result rows for a nightly job are useful for a week, not forever."""
        assert build_celery(settings()).conf.result_expires == dt.timedelta(days=7)
