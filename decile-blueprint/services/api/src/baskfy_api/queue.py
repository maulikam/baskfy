"""Handing work to Celery from the API (Prompt 15 deliverable 4).

``baskfy_worker`` imports ``baskfy_api``, so this service cannot import the worker back to reach
its task functions — and it does not need to. Celery routes by **task name**, so the producer
needs the broker URL and a routing table, which is why this module is short rather than a shared
task package.

.. warning::

   This docstring used to say that the API publishes ``baskfy.backtest.run`` and
   ``baskfy_worker.celery_app.TASK_ROUTES`` "puts it on the ``backtest`` queue". **That is not how
   Celery works and it was never true.** ``task_routes`` is resolved by the *producer*, at
   ``send_task`` time, from the config of the app doing the publishing. The worker's table governs
   what the worker publishes; it has no say over what this app publishes.

   Measured against the real producer before M45.5, resolving each name this service actually
   sends::

       baskfy.backtest.run                  -> celery   (worker consumes: backtest)
       baskfy.pipeline.nightly              -> celery   (worker consumes: default)
       baskfy.compute.reprocess_instrument  -> celery   (worker consumes: compute)

   Every one of them, not just the backtest — the two admin routes were misrouted the same way.
   Nothing consumes ``celery``, so with a broker configured and a worker running, all three would
   have been accepted, published, and never executed.

Celery itself is already the locked choice (docs/02 §"The decision in one table": "Jobs — Celery
+ Celery Beat"). What is new is that ``baskfy-api`` now depends on the library directly, as a
*producer*; before Prompt 15 nothing in the API enqueued anything. Recorded in
``docs/DECISIONS.md`` §15.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

from celery import Celery

from baskfy_api.settings import Settings

log = logging.getLogger(__name__)

__all__ = ["PRODUCER_TASK_ROUTES", "TaskQueue", "build_task_queue"]

#: Where this producer publishes each task name.
#:
#: Deliberately a copy of ``baskfy_worker.celery_app.TASK_ROUTES`` rather than an import: the
#: dependency runs worker -> api, and inverting it to share one dict would close a cycle. The copy
#: is not left to discipline — ``services/worker/tests/test_celery_config.py`` imports both and
#: asserts that every task name the worker registers resolves to the same queue through either
#: table, so drift fails the build rather than silently misrouting work.
PRODUCER_TASK_ROUTES: dict[str, dict[str, str]] = {
    "baskfy.ingest.*": {"queue": "ingest"},
    "baskfy.compute.*": {"queue": "compute"},
    "baskfy.backtest.*": {"queue": "backtest"},
    "baskfy.pipeline.*": {"queue": "default"},
    "baskfy.ops.*": {"queue": "default"},
    "baskfy.alerts.*": {"queue": "default"},
    "baskfy.desk.*": {"queue": "default"},
    "baskfy.cb.*": {"queue": "compute"},
    # PORTFOLIO_REDESIGN.md §5.1's nightly EOD NAV job. Compute-bound over price history, like the
    # factor and curated-metric steps.
    "baskfy.portfolio.*": {"queue": "compute"},
    # SW15: the swing book's "Scan now" — the one swing task this producer publishes. The
    # worker's table names the same queue; the sweep beside it is Beat's, never published here.
    "baskfy.swing.scan_now": {"queue": "compute"},
    "baskfy.swing.scan_after_login": {"queue": "compute"},
    "baskfy.swing.scan_sweep": {"queue": "default"},
    # SW18: the 08:45 / 09:05 Kite login nudge. Beat's, never published from here — mirrored
    # only because the anti-drift test walks every task the worker registers.
    "baskfy.kite.*": {"queue": "compute"},
    # VB9: the volume-breakout backtest is compute-bound — minutes of Polars and NumPy over nine
    # years of bars. Mirrored here for the same reason as the line above: it is invoked by hand
    # and by Beat, never published from the API, but the anti-drift test walks every name the
    # worker registers and a task on `compute` there and `default` here is a real misroute.
    #
    # The sleeve's other tasks need no line: `baskfy.vbt.detect`, `.evening` and `.morning` take
    # their queue from their Beat entries' `options`, and `baskfy.vbt.check_*` routes to
    # `default` on the worker, which is this table's default too — so both sides already agree.
    "baskfy.vbt.backtest": {"queue": "compute"},
}

#: The queue an unrouted name lands on. Must match the worker's ``task_default_queue``, which is
#: ``default`` and not Celery's own ``celery`` — that difference is what turned a missing routing
#: table into silence rather than into a slow queue.
DEFAULT_QUEUE: str = "default"


class TaskQueue(Protocol):
    """The one call the API makes on a broker."""

    def send_task(self, name: str, args: Sequence[object]) -> object: ...


class CeleryTaskQueue:
    """A producer-only Celery application.

    No task is registered on it and no worker runs from it: registering the worker's tasks here
    would mean importing ``baskfy_worker``, which is the dependency direction this repository
    does not have. ``send_task`` publishes by name, which is all a producer needs.
    """

    def __init__(self, broker_url: str) -> None:
        self._app = Celery("baskfy-api-producer", broker=broker_url)
        self._app.conf.update(
            task_serializer="json",
            accept_content=["json"],
            result_serializer="json",
            # Both of these are the M45.5 fix. Without `task_routes` every name went to `celery`;
            # without `task_default_queue` a name this table misses would still go there.
            task_routes=PRODUCER_TASK_ROUTES,
            task_default_queue=DEFAULT_QUEUE,
        )

    def send_task(self, name: str, args: Sequence[object]) -> object:
        return self._app.send_task(name, args=list(args))


def build_task_queue(settings: Settings) -> TaskQueue | None:
    """``None`` when there is no broker to publish to — the caller logs and leaves work queued."""
    url = settings.redis_url
    if not url:
        return None
    try:
        return CeleryTaskQueue(url)
    except ValueError as exc:  # pragma: no cover - a malformed URL
        log.error("no task queue", extra={"error": str(exc)})
        return None
