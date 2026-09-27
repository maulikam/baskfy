"""LV4 — a Kite login queues every refresh it unlocks, and never fails because one could not go.

`gates/live-4-login-event.md` L5. Mirrors `test_broker_oauth`'s queue tests: the catch-up and the
live swing scan as before, then the TWT and VBT re-detections of the last published session
through the same `request_scan` the pages' buttons use.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from fastapi import Request

from baskfy_api import twt_scan, vbt_scan
from baskfy_api.routers import brokers as brokers_router


def request_stub(queue: MagicMock) -> Request:
    settings = SimpleNamespace(
        twt_scan_min_interval_seconds=60,
        twt_scan_stale_after_seconds=600,
        vbt_scan_min_interval_seconds=60,
        vbt_scan_stale_after_seconds=600,
    )
    return cast(
        Request,
        SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(task_queue=queue, settings=settings))
        ),
    )


def fake_request_scan(
    name: str, calls: list[dict[str, object]]
) -> Callable[..., Awaitable[SimpleNamespace]]:
    async def _request(session: object, **kwargs: object) -> SimpleNamespace:
        del session
        calls.append({"name": name, **kwargs})
        queue = kwargs["queue"]
        assert isinstance(queue, MagicMock)
        queue.send_task(name, [7])
        return SimpleNamespace(id=7)

    return _request


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(brokers_router, "settings_for", lambda request: request.app.state.settings)


@pytest.mark.asyncio
async def test_a_login_queues_the_catch_up_the_swing_scan_and_both_closed_session_scans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(twt_scan, "request_scan", fake_request_scan("baskfy.twt.scan", calls))
    monkeypatch.setattr(vbt_scan, "request_scan", fake_request_scan("baskfy.vbt.rescan", calls))
    queue = MagicMock()
    note = await brokers_router._queue_post_login_refresh(request_stub(queue), 42, MagicMock())
    names = [c.args[0] for c in queue.send_task.call_args_list]
    assert names == [
        brokers_router.SESSION_CATCH_UP_TASK,
        brokers_router.SWING_SCAN_AFTER_LOGIN_TASK,
        "baskfy.twt.scan",
        "baskfy.vbt.rescan",
    ]
    assert [c["source"] for c in calls] == ["web", "desk"]
    assert all(c["user_id"] == 42 and c["min_interval"] == dt.timedelta(seconds=60) for c in calls)
    assert "TWT scan" in note and "VBT scan" in note and "could not" not in note


@pytest.mark.asyncio
async def test_a_scan_that_is_refused_is_a_note_not_a_failed_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refused(session: object, **kwargs: object) -> None:
        del session, kwargs
        raise RuntimeError("Scan 4 is queued; its result is on its way.")

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(twt_scan, "request_scan", refused)
    monkeypatch.setattr(vbt_scan, "request_scan", fake_request_scan("baskfy.vbt.rescan", calls))
    queue = MagicMock()
    note = await brokers_router._queue_post_login_refresh(request_stub(queue), 42, MagicMock())
    assert "could not be queued: TWT scan of the last published session (RuntimeError)" in note
    assert "VBT scan" in note
    assert len(queue.send_task.call_args_list) == 3


@pytest.mark.asyncio
async def test_without_a_session_only_the_two_original_jobs_are_queued() -> None:
    queue = MagicMock()
    note = await brokers_router._queue_post_login_refresh(request_stub(queue), 42)
    assert len(queue.send_task.call_args_list) == 2
    assert "TWT" not in note


@pytest.mark.asyncio
async def test_a_dead_queue_still_answers_with_a_note() -> None:
    queue = MagicMock()
    queue.send_task.side_effect = OSError("redis is not listening")
    note = await brokers_router._queue_post_login_refresh(request_stub(queue), 42, MagicMock())
    assert "could not be queued" in note
