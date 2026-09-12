"""Audit 4.13 — scan Celery publish waits for the request commit."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import deferred_publish as mod
from baskfy_api.deferred_publish import defer_task_publish, drain_deferred_publishes


class _RecordingQueue:
    def __init__(self) -> None:
        self.sent: list[tuple[str, list[object]]] = []

    def send_task(self, name: str, args: list[object]) -> object:
        self.sent.append((name, list(args)))
        return f"task-{len(self.sent)}"


class _FakeAsyncSession:
    """Minimal stand-in: info dict only. No real after_commit hook needed for unit drain."""

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}
        self.sync_session = SimpleNamespace()


def test_defer_does_not_publish_until_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeAsyncSession()
    queue = _RecordingQueue()
    row = SimpleNamespace(id=42, task_id=None)
    calls: list[tuple[object, str, dict[str, object]]] = []

    def _fake_listens_for(
        target: object, identifier: str, **kwargs: object
    ) -> object:
        def decorator(fn: object) -> object:
            calls.append((target, identifier, dict(kwargs)))
            return fn

        return decorator

    monkeypatch.setattr(mod.event, "listens_for", _fake_listens_for)

    defer_task_publish(
        cast(AsyncSession, session),
        cast(mod.TaskQueue, queue),
        "baskfy.vbt.rescan",
        42,
        row=row,
    )

    assert queue.sent == []
    assert row.task_id is None
    assert calls and calls[0][1] == "after_commit"
    assert calls[0][2].get("once") is True

    drain_deferred_publishes(cast(AsyncSession, session))
    assert queue.sent == [("baskfy.vbt.rescan", [42])]
    assert row.task_id == "task-1"
