"""Celery configuration (Prompt 3 deliverable 1).

    "Celery configured with Redis broker/result backend, task-level retries, and separate queues:
     `ingest`, `compute`, `backtest`, `default`."

No broker is needed to assert any of this — the configuration is the deliverable.
"""

from __future__ import annotations

import datetime as dt

from celery import Celery
from celery.schedules import crontab

from baskfy_api.admin import NIGHTLY_TASK_NAME, REPROCESS_TASK_NAME
from baskfy_api.queue import CeleryTaskQueue
from baskfy_api.resync import RESYNC_TASK_NAME
from baskfy_worker.celery_app import (
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
from baskfy_worker.deps import PipelineDependencies
from baskfy_worker.settings import WorkerSettings


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
        """The pattern read ``baskfy.comgest.*`` until M45.5, and so did this assertion.

        M2's namespace pass rewrote the domain ``decile.in`` -> ``baskfy.com``, and the routing
        pattern was ``decile.ingest.*``, so the substitution cut the word in half. The test then
        pinned the result — which is house rule 2 exactly: it asserted what the code happened to
        do rather than what the spec says, so the one check that could have caught the mangling
        was the thing that made it permanent.

        Nothing is misrouted by it today because no task is named ``baskfy.ingest.*`` yet. The
        first one added would have been, silently, onto ``default``.
        """
        assert TASK_ROUTES["baskfy.ingest.*"]["queue"] == QUEUE_INGEST
        assert not any("comgest" in pattern for pattern in TASK_ROUTES), (
            "the mangled pattern must not come back"
        )

    def test_compute_and_backtest_have_their_own_queues(self) -> None:
        """docs/03 §"Scaling plan" step 4 moves backtest fan-out to its own pool; the queue is
        already there, so that becomes a deployment change rather than a code change."""
        assert TASK_ROUTES["baskfy.compute.*"]["queue"] == QUEUE_COMPUTE
        assert TASK_ROUTES["baskfy.backtest.*"]["queue"] == QUEUE_BACKTEST


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

    def test_the_bhavcopy_lands_the_day_before_the_chain_needs_it(self) -> None:
        """M84: NSE's own file at 18:15, so the 18:45 chain finds the session already landed.

        Ordering is the whole point. Behind the chain it would be a fallback again; before it, the
        day is on disk whatever Kite does — which is what 18-29 Aug and 3 Sep 2026 each cost.
        """
        entry = BEAT_SCHEDULE["bhavcopy-eod"]["schedule"]
        nightly = BEAT_SCHEDULE["refresh-reference-data"]["schedule"]
        assert isinstance(entry, crontab)
        assert entry.day_of_week == {1, 2, 3, 4, 5}
        assert entry.hour == {18}
        assert entry.minute == {15}
        assert _earliest(entry) < _earliest(nightly), "the bhavcopy must not queue behind the chain"

    def test_the_catch_up_sweep_runs_before_the_market_opens(self) -> None:
        """M84: a session nobody noticed was missing is re-run at 06:45, not tomorrow evening.

        Saturday too — a Friday night that failed must not wait until Monday to be found.
        """
        entry = BEAT_SCHEDULE["session-catch-up"]["schedule"]
        assert isinstance(entry, crontab)
        assert entry.hour == {6}
        assert entry.minute == {45}
        assert entry.day_of_week == {1, 2, 3, 4, 5, 6}

    def test_the_weekly_integrity_audit_is_scheduled(self) -> None:
        """docs/09 §Schedule: "Sat 02:00 — full-history integrity audit"."""
        entry = BEAT_SCHEDULE["weekly-integrity-audit"]["schedule"]
        assert isinstance(entry, crontab)
        assert entry.day_of_week == {6}  # Saturday
        assert entry.hour == {2}


class TestRegisteredTasks:
    def test_the_pipeline_tasks_are_registered(self) -> None:
        app = build_celery(settings())
        names = {name for name in app.tasks if name.startswith("baskfy.")}
        assert "baskfy.pipeline.nightly" in names
        assert "baskfy.compute.reprocess_instrument" in names
        assert "baskfy.pipeline.integrity_audit" in names
        # Leaf 3.1's resync button. A name the API publishes and the worker does not bind is a
        # 202 the operator reads as "queued" and nothing ever runs.
        assert RESYNC_TASK_NAME in names

    def test_the_backtest_task_is_registered_and_routed_to_its_own_queue(self) -> None:
        """PROMPTS.md Prompt 15 §4: "Celery task on the `backtest` queue".

        Registration and routing are separate facts and both matter: a task that exists but is
        not routed runs on `default`, where a fifteen-year simulation would sit in front of the
        nightly publish.
        """
        app = build_celery(settings())
        assert "baskfy.backtest.run" in app.tasks
        assert TASK_ROUTES["baskfy.backtest.*"]["queue"] == QUEUE_BACKTEST

    def test_results_expire(self) -> None:
        """Result rows for a nightly job are useful for a week, not forever."""
        assert build_celery(settings()).conf.result_expires == dt.timedelta(days=7)


class TestTheProducerRoutesWhereTheWorkerListens:
    """M45.5. ``task_routes`` is resolved by the PRODUCER, not by the consumer.

    ``baskfy_api.queue`` published with a bare Celery app and no routing table, on the stated
    premise that ``baskfy_worker.celery_app.TASK_ROUTES`` would place the message. It does not:
    Celery resolves routes at ``send_task`` time from the config of the app doing the publishing,
    and the worker's table governs only what the worker publishes.

    Measured before the fix, against the real producer::

        baskfy.backtest.run                  -> celery   (worker consumes: backtest)
        baskfy.pipeline.nightly              -> celery   (worker consumes: default)
        baskfy.compute.reprocess_instrument  -> celery   (worker consumes: compute)

    All three, not only the backtest — both admin publish routes were misrouted the same way.
    Nothing consumes ``celery``, so with a broker configured and a worker running, every one of
    them would have been accepted, published, and never executed. The API would have answered 202.

    These tests live in the worker suite because it is the one place allowed to import both sides.
    """

    def test_every_registered_task_routes_to_the_queue_the_worker_gives_it(self) -> None:
        """The anti-drift check that lets the producer keep its own copy of the table."""
        producer = CeleryTaskQueue("redis://localhost:6380/0")
        worker = build_celery(settings())
        registered = sorted(name for name in worker.tasks if name.startswith("baskfy."))
        assert registered, "no baskfy tasks were registered, so this would prove nothing"

        mismatched = []
        for name in registered:
            here = _routed_queue(producer._app, name)
            there = _routed_queue(worker, name)
            if here != there:
                mismatched.append(f"{name}: producer -> {here}, worker -> {there}")
        assert not mismatched, "\n".join(mismatched)

    def test_every_name_the_api_actually_publishes_reaches_a_consumed_queue(self) -> None:
        """Named explicitly, because the general check would still pass if both sides agreed on a
        queue nobody consumes — which is precisely the state this fixes."""
        producer = CeleryTaskQueue("redis://localhost:6380/0")._app
        expected = {
            "baskfy.backtest.run": QUEUE_BACKTEST,
            NIGHTLY_TASK_NAME: QUEUE_DEFAULT,
            REPROCESS_TASK_NAME: QUEUE_COMPUTE,
            RESYNC_TASK_NAME: QUEUE_DEFAULT,
        }
        actual = {name: _routed_queue(producer, name) for name in expected}
        assert actual == expected
        assert set(actual.values()) <= set(QUEUES), "published onto a queue no worker consumes"


def _earliest(entry: crontab) -> int:
    """The first minute of the hour a crontab fires on.

    Celery ships `crontab.minute` untyped, so mypy sees `object` and `min()` refuses it. Narrowed
    here once, rather than at each reading, and the narrowing is checked: a Celery release that
    changes the representation fails this helper instead of silently comparing nothing.
    """
    minutes = entry.minute
    assert isinstance(minutes, (set, frozenset)), f"crontab.minute is now a {type(minutes)}"
    return min(int(minute) for minute in minutes)


def _routed_queue(app: Celery, task_name: str) -> str:
    """Where ``app`` would actually publish ``task_name``, resolving as Celery does."""
    options = app.amqp.router.route({}, task_name)
    queue = options.get("queue")
    name = getattr(queue, "name", queue)
    return str(name or app.conf.task_default_queue)


class TestTheBrokerDoesNotRedeliverALongNight:
    """M75. `acks_late` plus Redis's one-hour default is a duplicate-run generator.

    An `acks_late` task acks when it finishes. Redis makes an un-acked message visible again after
    `visibility_timeout`, so any task that runs longer than that is handed to a second worker while
    the first is still going. On 1 Sep 2026 the same nightly task id was seen on two pool processes
    started an hour apart, both writing `ohlcv_daily` for the same night.

    The nightly crossed that line when the SME universe roughly tripled the instrument count — a
    night went from ~2,980 bars to 9,225, and at Kite's 3 req/s the fetch step alone is ~51 minutes.
    """

    def test_the_visibility_timeout_is_set_and_clears_a_long_run(self) -> None:
        options = build_celery().conf.broker_transport_options
        timeout = options.get("visibility_timeout")
        assert timeout is not None, (
            "no visibility_timeout: Redis falls back to 3600s, and a nightly that now takes over "
            "an hour will be redelivered while it is still running"
        )
        # The observed fetch step is ~51 minutes and the whole chain is longer. Two hours is the
        # floor at which redelivery stops being a certainty; the configured value is well past it.
        assert timeout >= 2 * 60 * 60
        # And not so long that a worker killed mid-night waits until tomorrow to be retried.
        assert timeout <= 12 * 60 * 60


class TestTheMarketGateReadsTheMidSmallcap400:
    """SW17's decision, and the guard against "fixing" it back (9 Sep 2026).

    `67df9b4`: *"Maulik's call: the book trades mid- and small-caps, so the tape it asks about is
    nifty-mid-small-400, with nifty-500 as the fallback."* The book screens mid- and small-cap
    breakouts; a large-cap-weighted index answers a question about somebody else's market.

    M87 reverted that to `nifty-500` because `docs/swing/04` §8.2 still said so, and pinned the
    wrong value with a test asserting the setting matched THE DOCUMENT. That is the failure this
    class now guards against from the other side: the decision is the authority, and a doc that
    disagrees is the thing to fix.
    """

    def test_worker_settings_index_slug_matches_deps(self) -> None:
        settings_default = WorkerSettings.model_fields["swing_index_slug"].default
        deps_default = PipelineDependencies.__dataclass_fields__["swing_index_slug"].default

        assert settings_default == deps_default, (
            "the benchmark the gate actually reads has drifted from the other default"
        )

    def test_the_benchmark_is_the_midsmallcap_400(self) -> None:
        """Pinned to SW17's decision. It changed the verdict when M87 undid it: on 3 Sep the
        MidSmall 400 read GREEN (10-day 21,592.34 over 20-day 21,581.78) where the Nifty 500
        read RED, and the gate decides whether the book may enter at all."""
        assert WorkerSettings.model_fields["swing_index_slug"].default == "nifty-mid-small-400", (
            "nifty-500 is the FALLBACK, not the benchmark — see SW17 and M87's reversal"
        )
