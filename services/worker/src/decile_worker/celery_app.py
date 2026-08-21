"""The Celery application (Prompt 3 deliverable 1).

docs/02 §"Why Celery, not Prefect/Airflow/Dagster": "The pipeline is ~8 tasks with a linear
dependency chain, run once a day. Airflow-class tooling is more operational surface than the job
graph justifies. Celery Beat + a `pipeline_run` table with explicit step states gives us retries,
idempotency and a run history in ~200 lines."

Queues (deliverable 1). Separate queues exist so one workload cannot starve another:

    ingest    network-bound, rate-limited, long. Runs at the broker's mercy for hours during a
              backfill. Given its own workers so a slow Kite night cannot delay everything else.
    compute   CPU-bound Polars work. Wants few workers with a lot of memory, not many small ones.
    backtest  docs/03 §"Scaling plan" step 4: "Move backtest fan-out to a dedicated worker pool
              with its own queue." The queue exists now so that move is a deployment change.
    default   orchestration, publishing, alerts. Short and latency-sensitive; must never queue
              behind a two-hour backfill chunk.

Schedule: docs/09 §Schedule (IST). Beat is configured in IST because every time in that table is
an IST wall-clock time tied to the NSE session, and expressing them in UTC would silently shift
the whole pipeline twice a year for anyone reading it from a country that observes DST.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from celery import Celery
from celery.schedules import crontab

from decile_worker.settings import WorkerSettings, get_worker_settings

#: NSE trades in IST and docs/09's schedule is stated in IST.
IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST")
IST_NAME: Final = "Asia/Kolkata"

QUEUE_INGEST: Final = "ingest"
QUEUE_COMPUTE: Final = "compute"
QUEUE_BACKTEST: Final = "backtest"
QUEUE_DEFAULT: Final = "default"

QUEUES: Final[tuple[str, ...]] = (QUEUE_INGEST, QUEUE_COMPUTE, QUEUE_BACKTEST, QUEUE_DEFAULT)

#: Task name -> queue. Routing lives here rather than on each task so the whole topology is
#: readable in one place, which is what an operator needs when a queue backs up.
TASK_ROUTES: Final[dict[str, dict[str, str]]] = {
    "decile.ingest.*": {"queue": QUEUE_INGEST},
    "decile.compute.*": {"queue": QUEUE_COMPUTE},
    "decile.backtest.*": {"queue": QUEUE_BACKTEST},
    "decile.pipeline.*": {"queue": QUEUE_DEFAULT},
    # Prompt 17's operational checks. Short, frequent and latency-sensitive — an alert that
    # queues behind a two-hour backfill chunk is an alert nobody gets.
    "decile.ops.*": {"queue": QUEUE_DEFAULT},
    # Prompt 20's screen alerts and webhook deliveries. Short, latency-sensitive and idempotent;
    # the same queue the publish step runs on, and for the same reason.
    "decile.alerts.*": {"queue": QUEUE_DEFAULT},
}

#: docs/09 §Schedule (IST), weekdays. Times are the doc's; the task names are docs/03's.
BEAT_SCHEDULE: Final[dict[str, dict[str, object]]] = {
    "refresh-reference-data": {
        "task": "decile.pipeline.nightly",
        "schedule": crontab(hour=18, minute=45, day_of_week="mon-fri"),
        "options": {"queue": QUEUE_DEFAULT},
    },
    "purge-deleted-accounts": {
        # Prompt 12 §5's seven-day soft-delete window closes on its own. Daily at 03:00 IST,
        # after the nightly chain and before anyone is awake to be surprised by it.
        "task": "decile.accounts.purge",
        "schedule": crontab(hour=3, minute=0),
        "options": {"queue": QUEUE_DEFAULT},
    },
    "weekly-integrity-audit": {
        # docs/09: "Sat 02:00 — full-history integrity audit".
        "task": "decile.pipeline.integrity_audit",
        "schedule": crontab(hour=2, minute=0, day_of_week="sat"),
        "options": {"queue": QUEUE_COMPUTE},
    },
    # --- Prompt 17 deliverable 3: the alerting rules this codebase evaluates itself ---
    "reap-abandoned-pipeline-runs": {
        # A run left in `running` is a worker that died. Every fifteen minutes, because the
        # alternative — finding it the next evening — finds it after the market has opened on
        # stale data. The sweep is one indexed query and is a no-op when nothing is stale.
        "task": "decile.ops.reap_abandoned_runs",
        "schedule": crontab(minute="*/15"),
        "options": {"queue": QUEUE_DEFAULT},
    },
    "publish-deadline-slo": {
        # docs/11 §Reliability: "data published by 20:15 IST on >=95% of trading days." Fired at
        # the deadline itself; silent on a non-trading day and on a day that published.
        "task": "decile.ops.check_publish_deadline",
        "schedule": crontab(hour=20, minute=15, day_of_week="mon-fri"),
        "options": {"queue": QUEUE_DEFAULT},
    },
    "kite-token-expiry": {
        # docs/09: token expiry is "the #1 pipeline failure". Hourly, and it makes no network
        # call — the expiry is computed from the stored issue time.
        "task": "decile.ops.check_kite_token",
        "schedule": crontab(minute=5),
        "options": {"queue": QUEUE_DEFAULT},
    },
    "queue-backlog": {
        "task": "decile.ops.check_queue_backlog",
        "schedule": crontab(minute="*/10"),
        "options": {"queue": QUEUE_DEFAULT},
    },
    # --- Prompt 20 deliverables 3 and 4 -----------------------------------
    "dispatch-screen-alerts": {
        # PROMPTS.md Prompt 20 §3: "daily/weekly **after publish**". docs/11 §Reliability puts
        # the publish deadline at 20:15 IST, so 20:30 gives the chain fifteen minutes of slack
        # and still lands the email the same evening. A day that did not publish sends nothing —
        # `resolve_runs` finds no `screen_run` for the date and every alert is skipped with a
        # recorded reason.
        "task": "decile.alerts.dispatch",
        "schedule": crontab(hour=20, minute=30, day_of_week="mon-fri"),
        "options": {"queue": QUEUE_DEFAULT},
    },
    "sweep-webhook-deliveries": {
        # The backoff schedule is on the row, not in Celery's retry machinery — see the task.
        # Every two minutes: fine enough that the first retry (30s) is not delayed much beyond
        # its schedule, coarse enough to be a no-op query the rest of the time.
        "task": "decile.alerts.sweep_webhooks",
        "schedule": crontab(minute="*/2"),
        "options": {"queue": QUEUE_DEFAULT},
    },
}


def build_celery(settings: WorkerSettings | None = None) -> Celery:
    resolved = settings or get_worker_settings()
    app = Celery("decile", broker=resolved.broker_url(), backend=resolved.result_backend())
    app.conf.update(
        task_default_queue=QUEUE_DEFAULT,
        task_routes=TASK_ROUTES,
        task_acks_late=True,
        # A pipeline step that dies with its worker must be re-delivered, not lost: the
        # orchestrator's idea of "this step ran" comes from pipeline_run_step, not from the broker.
        task_reject_on_worker_lost=True,
        # One long ingest chunk at a time per worker process. Prefetching several would let a
        # single slow chunk hold up work that another worker could have taken.
        worker_prefetch_multiplier=1,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone=IST_NAME,
        enable_utc=False,
        beat_schedule=BEAT_SCHEDULE,
        task_track_started=True,
        result_expires=dt.timedelta(days=7),
        task_default_retry_delay=resolved.task_retry_backoff_seconds,
        task_annotations={"*": {"max_retries": resolved.task_max_retries}},
    )
    # `include` rather than `autodiscover_tasks`: the bindings live in one module, and naming it
    # here keeps `decile_worker.tasks.__init__` free of an import cycle back through the
    # orchestrator.
    app.conf.include = ["decile_worker.tasks.celery_tasks"]
    app.autodiscover_tasks(["decile_worker.tasks"], related_name="celery_tasks", force=True)
    _install_observability_signals()
    return app


def _install_observability_signals() -> None:
    """Wire tracing, Sentry and the metrics endpoint into the worker's process lifecycle.

    ``worker_process_init`` rather than module scope: Celery's prefork pool forks *after* this
    module is imported, so anything built here would be inherited by every child — one batch span
    queue and one metrics socket shared across processes that each think they own it. See
    ``decile_worker.telemetry``.

    Registered from ``build_celery`` rather than at import so that importing this module to read
    ``BEAT_SCHEDULE`` (which the tests do) installs no signal handlers.
    """
    from celery.signals import worker_process_init  # noqa: PLC0415

    @worker_process_init.connect(weak=False)
    def _init(**_kwargs: object) -> None:
        from decile_worker.telemetry import install_worker_observability  # noqa: PLC0415

        install_worker_observability()


app = build_celery()
