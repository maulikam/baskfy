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
    return app


app = build_celery()
