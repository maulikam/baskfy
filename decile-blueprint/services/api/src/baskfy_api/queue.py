"""Handing work to Celery from the API (Prompt 15 deliverable 4).

``baskfy_worker`` imports ``baskfy_api``, so this service cannot import the worker back to reach
its task functions — and it does not need to. Celery routes by **task name**: the API publishes
``baskfy.backtest.run`` to the broker and ``baskfy_worker.celery_app.TASK_ROUTES`` puts it on the
``backtest`` queue. The producer needs the broker URL and nothing else, which is why this module
is twenty lines rather than a shared task package.

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

__all__ = ["TaskQueue", "build_task_queue"]


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
            task_serializer="json", accept_content=["json"], result_serializer="json"
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
