"""Defer Celery publish until the request transaction commits (audit 4.13).

``backtests.py`` solved the same race with FastAPI ``BackgroundTasks`` after the handler
returns (which is after ``get_session`` commits). Scan modules are called from routers we do
not own, so they register an ``after_commit`` hook on the session instead.

Tests that use a session override that never commits call :func:`drain_deferred_publishes`
to simulate the commit, then flush so ``task_id`` lands on the row.
"""

from __future__ import annotations

import logging
from typing import Protocol, cast

from sqlalchemy import Table, event, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, object_mapper

from baskfy_api.queue import TaskQueue

log = logging.getLogger(__name__)

_PENDING: str = "_baskfy_deferred_publishes"
_HOOK: str = "_baskfy_deferred_publish_hook"


class TaskIdRow(Protocol):
    """ORM row (or test stand-in) that carries the Celery ``task_id`` column."""

    id: int
    task_id: str | None


def defer_task_publish(
    session: AsyncSession,
    queue: TaskQueue,
    task_name: str,
    run_id: int,
    *,
    row: TaskIdRow | None,
) -> None:
    """Queue ``send_task`` for after this session's transaction commits."""
    pending: list[tuple[TaskQueue, str, int, TaskIdRow | None]] = session.info.setdefault(
        _PENDING, []
    )
    pending.append((queue, task_name, run_id, row))
    if session.info.get(_HOOK):
        return
    session.info[_HOOK] = True
    sync = session.sync_session

    @event.listens_for(sync, "after_commit", once=True)
    def _on_commit(sess: Session) -> None:
        drain_deferred_publishes(sess)


def drain_deferred_publishes(
    info_or_session: AsyncSession | Session | dict[str, object],
) -> None:
    """Publish every deferred task. Idempotent; safe to call from tests after a fake commit."""
    info: dict[str, object]
    persist_session: Session | None = None
    if isinstance(info_or_session, dict):
        info = info_or_session
    elif isinstance(info_or_session, Session):
        info = info_or_session.info
        persist_session = info_or_session
    else:
        info = info_or_session.info
    pending_raw = info.pop(_PENDING, [])
    pending = cast(
        list[tuple[TaskQueue, str, int, TaskIdRow | None]],
        pending_raw if isinstance(pending_raw, list) else [],
    )
    info.pop(_HOOK, None)
    for queue, task_name, run_id, row in pending:
        try:
            task_id = str(queue.send_task(task_name, [run_id]))
        except Exception as exc:
            log.warning(
                "scan now %s could not be published (%s); the sweep will",
                run_id,
                exc,
            )
            continue
        if row is None:
            continue
        row.task_id = task_id
        # after_commit has already closed the request txn — write task_id in a short follow-up.
        if persist_session is not None:
            _persist_task_id(persist_session, row, task_id)


def _persist_task_id(session: Session, row: TaskIdRow, task_id: str) -> None:
    mapper = object_mapper(row)
    table = cast(Table, mapper.persist_selectable)
    bind = session.get_bind()
    with Session(bind) as short:
        short.execute(update(table).where(table.c.id == row.id).values(task_id=task_id))
        short.commit()
