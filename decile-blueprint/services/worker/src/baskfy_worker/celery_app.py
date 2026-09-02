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

from baskfy_worker.settings import WorkerSettings, get_worker_settings

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
    # "comgest" until M45.5, which is not a word. M2's namespace pass rewrote the domain
    # `decile.in` -> `baskfy.com` and this pattern read `decile.ingest.*`, so the sed cut it in
    # half. No task is named `baskfy.ingest.*` yet, so nothing was misrouted by it; the first one
    # added would have been, silently, onto the default queue.
    "baskfy.ingest.*": {"queue": QUEUE_INGEST},
    "baskfy.compute.*": {"queue": QUEUE_COMPUTE},
    "baskfy.backtest.*": {"queue": QUEUE_BACKTEST},
    "baskfy.pipeline.*": {"queue": QUEUE_DEFAULT},
    # Prompt 17's operational checks. Short, frequent and latency-sensitive — an alert that
    # queues behind a two-hour backfill chunk is an alert nobody gets.
    "baskfy.ops.*": {"queue": QUEUE_DEFAULT},
    # Prompt 20's screen alerts and webhook deliveries. Short, latency-sensitive and idempotent;
    # the same queue the publish step runs on, and for the same reason.
    "baskfy.alerts.*": {"queue": QUEUE_DEFAULT},
    # M19 §1. The desk's collection jobs: short, idempotent, and blocked on a Kite token rather
    # than on CPU. They must not queue behind a backfill chunk, so they take the default queue.
    "baskfy.desk.*": {"queue": QUEUE_DEFAULT},
    # SC2: curated-basket EOD metrics. Compute-bound over price history; same queue as factors.
    "baskfy.cb.*": {"queue": QUEUE_COMPUTE},
    # PORTFOLIO_REDESIGN.md §5.1. The nightly EOD NAV job values every user's holdings against
    # the day's closes: compute-bound over price history, exactly like the factor and curated
    # metric steps, and it must not sit behind a backfill chunk on the ingest queue.
    "baskfy.portfolio.*": {"queue": QUEUE_COMPUTE},
}

#: docs/09 §Schedule (IST), weekdays. Times are the doc's; the task names are docs/03's.
BEAT_SCHEDULE: Final[dict[str, dict[str, object]]] = {
    # --- M19 §1: the desk's own schedule, mirroring deploy/systemd/momentum-daily.timer ---
    # The systemd timers stay running until five green Beat runs are recorded (M19 §2). Both
    # firing is safe because every step of scripts/daily.py is independently idempotent, which
    # `tests/test_desk_tasks.py` asserts rather than assumes.
    "desk-daily-collection": {
        "task": "baskfy.desk.daily",
        "schedule": crontab(hour=18, minute=30, day_of_week="mon-fri"),
        "options": {"queue": QUEUE_DEFAULT},
    },
    # Twenty minutes later, and only useful when the 18:30 run found no token. Idempotent, so on
    # an ordinary day it collects nothing and says so.
    "desk-autorun-safety-net": {
        "task": "baskfy.desk.autorun",
        "schedule": crontab(hour=18, minute=50, day_of_week="mon-fri"),
        "options": {"queue": QUEUE_DEFAULT},
    },
    "refresh-reference-data": {
        "task": "baskfy.pipeline.nightly",
        "schedule": crontab(hour=18, minute=45, day_of_week="mon-fri"),
        "options": {"queue": QUEUE_DEFAULT},
    },
    "purge-deleted-accounts": {
        # Prompt 12 §5's seven-day soft-delete window closes on its own. Daily at 03:00 IST,
        # after the nightly chain and before anyone is awake to be surprised by it.
        "task": "baskfy.accounts.purge",
        "schedule": crontab(hour=3, minute=0),
        "options": {"queue": QUEUE_DEFAULT},
    },
    "weekly-integrity-audit": {
        # docs/09: "Sat 02:00 — full-history integrity audit".
        "task": "baskfy.pipeline.integrity_audit",
        "schedule": crontab(hour=2, minute=0, day_of_week="sat"),
        "options": {"queue": QUEUE_COMPUTE},
    },
    # --- Prompt 17 deliverable 3: the alerting rules this codebase evaluates itself ---
    "reap-abandoned-pipeline-runs": {
        # A run left in `running` is a worker that died. Every fifteen minutes, because the
        # alternative — finding it the next evening — finds it after the market has opened on
        # stale data. The sweep is one indexed query and is a no-op when nothing is stale.
        "task": "baskfy.ops.reap_abandoned_runs",
        "schedule": crontab(minute="*/15"),
        "options": {"queue": QUEUE_DEFAULT},
    },
    "publish-deadline-slo": {
        # docs/11 §Reliability: "data published by 20:15 IST on >=95% of trading days." Fired at
        # the deadline itself; silent on a non-trading day and on a day that published.
        "task": "baskfy.ops.check_publish_deadline",
        "schedule": crontab(hour=20, minute=15, day_of_week="mon-fri"),
        "options": {"queue": QUEUE_DEFAULT},
    },
    "kite-token-expiry": {
        # docs/09: token expiry is "the #1 pipeline failure". Hourly, and it makes no network
        # call — the expiry is computed from the stored issue time.
        "task": "baskfy.ops.check_kite_token",
        "schedule": crontab(minute=5),
        "options": {"queue": QUEUE_DEFAULT},
    },
    "queue-backlog": {
        "task": "baskfy.ops.check_queue_backlog",
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
        "task": "baskfy.alerts.dispatch",
        "schedule": crontab(hour=20, minute=30, day_of_week="mon-fri"),
        "options": {"queue": QUEUE_DEFAULT},
    },
    "sweep-webhook-deliveries": {
        # The backoff schedule is on the row, not in Celery's retry machinery — see the task.
        # Every two minutes: fine enough that the first retry (30s) is not delayed much beyond
        # its schedule, coarse enough to be a no-op query the rest of the time.
        "task": "baskfy.alerts.sweep_webhooks",
        "schedule": crontab(minute="*/2"),
        "options": {"queue": QUEUE_DEFAULT},
    },
    # --- SC2: curated-basket catalog metrics ---------------------------------
    "cb-eod-metrics": {
        # After the nightly ingest/publish window (20:15 SLO), before alert dispatch at 20:30.
        # Idempotent per (basket_id, as_of_date); re-runs are no-ops.
        "task": "baskfy.cb.compute_metrics",
        "schedule": crontab(hour=20, minute=20, day_of_week="mon-fri"),
        "options": {"queue": QUEUE_COMPUTE},
    },
    # --- SC7 / leaf 2.1: SIP REMINDER pending actions (no AUTO, no orders) -----
    "cb-sip-reminders": {
        # Pre-open IST so the sole user sees SIP_DUE before the cash session.
        # Idempotent per (plan_id, YYYY-MM) via fire_key; REMINDER mode only.
        "task": "baskfy.cb.sip_reminders",
        "schedule": crontab(hour=9, minute=0, day_of_week="mon-fri"),
        "options": {"queue": QUEUE_COMPUTE},
    },
    # --- SC4 / leaf 3.4: cash dividends from CA x holdings (no orders) ---------
    "cb-dividends": {
        # After EOD metrics (20:20); before alert dispatch (20:30). Idempotent per
        # (investment_id, instrument_id, ex_date); derive_dividends is pure.
        "task": "baskfy.cb.derive_dividends",
        "schedule": crontab(hour=20, minute=25, day_of_week="mon-fri"),
        "options": {"queue": QUEUE_COMPUTE},
    },
    # --- T8.2: promote PLANNED batches when the desk journal is settled ----------
    "cb-sync-batches": {
        # Intra-session: a Friday rebalance fill should show EXECUTED the same afternoon.
        # Idempotent; synthetic cb-sim-* ids are skipped.
        "task": "baskfy.cb.sync_batches",
        "schedule": crontab(minute="*/15", day_of_week="mon-fri"),
        "options": {"queue": QUEUE_COMPUTE},
    },
    # --- PORTFOLIO_REDESIGN.md §5.1: the official end-of-day NAV series ---------
    "portfolio-eod-nav": {
        # After the pipeline has published. docs/11 §Reliability puts the publish deadline at
        # 20:15 IST, and the two jobs that write rows this one must see run at 20:20 (curated
        # metrics) and 20:25 (dividends, which are portfolio cash flows). 20:35 is after all of
        # them and before nothing — the NAV series is read the next morning, not tonight.
        #
        # Deliberately not chained onto the pipeline task. §5.1's NAV is a claim about a *date*,
        # and a date whose bars never landed must produce no rows at all rather than a retry
        # storm; the job checks for a close itself and is silent when there is none, so a missed
        # or failed publish costs one empty run instead of a false series.
        #
        # Idempotent per (user_id, date, portfolio_id), so a manual re-run for the same date —
        # after a late ingest, say — corrects the day instead of duplicating it.
        "task": "baskfy.portfolio.eod_nav",
        "schedule": crontab(hour=20, minute=35, day_of_week="mon-fri"),
        "options": {"queue": QUEUE_COMPUTE},
    },
    # --- SW3/SW4: the swing book's scans (docs/swing/06) -------------------------
    #
    # The nightly chain already runs `compute_swing` as its twelfth step, after `publish`. This
    # entry is the belt to that brace: on a night the chain failed its quality gate the bars are
    # still there and yesterday's setups are still worth having, and `docs/swing/01` §8's routine
    # ("End of day: 15-30 minutes on positions and scans") does not care why the screener had a
    # bad night. Idempotent, so on an ordinary evening it rewrites the rows the chain just wrote.
    #
    # 21:00, which is after the 20:15 publish deadline (docs/11 §Reliability) and after the four
    # 20:2x-20:35 jobs, so it neither races the chain nor queues behind it.
    "swing-eod": {
        "task": "baskfy.swing.detect",
        "schedule": crontab(hour=21, minute=0, day_of_week="mon-fri"),
        "options": {"queue": QUEUE_COMPUTE},
    },
    # docs/swing/01 §8: "Weekend: a full scan, a watchlist of a few dozen forming flags, the
    # levels that would trigger next week." Saturday morning, before anyone looks.
    "swing-weekend": {
        "task": "baskfy.swing.weekend",
        "schedule": crontab(hour=7, minute=0, day_of_week="sat"),
        "options": {"queue": QUEUE_COMPUTE},
    },
    # --- T8.3: one email per REBALANCE_AVAILABLE (payload.notified_at) -----------
    "cb-rebalance-notify": {
        # After SIP reminders (09:00); before the cash session is busy.
        "task": "baskfy.cb.rebalance_notify",
        "schedule": crontab(hour=9, minute=30, day_of_week="mon-fri"),
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
        # THE NIGHTLY NOW RUNS LONGER THAN AN HOUR, AND THAT BROKE `acks_late` (M75).
        #
        # With Redis as the broker, an un-acked message becomes visible again after
        # `visibility_timeout` and is handed to another worker. The default is 3600s. `acks_late`
        # means the nightly acks only when it FINISHES, so the moment the chain takes longer than
        # an hour the broker redelivers it and a second copy starts while the first is still
        # fetching — two writers on the same `ohlcv_daily` rows for the same night.
        #
        # That is not hypothetical: on 1 Sep 2026 the same task id was observed running on two
        # pool processes, started 19:18 and 20:19, almost exactly an hour apart. The cause is that
        # the NSE SME universe roughly tripled the instrument count (a night went from ~2,980 bars
        # to 9,225), and at Kite's 3 req/s the fetch step alone is now ~51 minutes.
        #
        # Six hours: comfortably above any plausible run, comfortably below the daily cadence, so
        # a genuinely dead worker's message still comes back the same night. A worker that dies is
        # still covered by `task_reject_on_worker_lost` below, which redelivers immediately rather
        # than waiting this out.
        broker_transport_options={"visibility_timeout": 6 * 60 * 60},
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
    # here keeps `baskfy_worker.tasks.__init__` free of an import cycle back through the
    # orchestrator.
    app.conf.include = ["baskfy_worker.tasks.celery_tasks"]
    app.autodiscover_tasks(["baskfy_worker.tasks"], related_name="celery_tasks", force=True)
    _install_observability_signals()
    return app


def _install_observability_signals() -> None:
    """Wire tracing, Sentry and the metrics endpoint into the worker's process lifecycle.

    ``worker_process_init`` rather than module scope: Celery's prefork pool forks *after* this
    module is imported, so anything built here would be inherited by every child — one batch span
    queue and one metrics socket shared across processes that each think they own it. See
    ``baskfy_worker.telemetry``.

    Registered from ``build_celery`` rather than at import so that importing this module to read
    ``BEAT_SCHEDULE`` (which the tests do) installs no signal handlers.
    """
    from celery.signals import worker_process_init  # noqa: PLC0415

    @worker_process_init.connect(weak=False)
    def _init(**_kwargs: object) -> None:
        from baskfy_worker.telemetry import install_worker_observability  # noqa: PLC0415

        install_worker_observability()


app = build_celery()
