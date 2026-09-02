"""Spans and metrics over the swing jobs cannot raise into them (SW11, `06`: "unable to raise
into the order path"; B8: "spans optional").

`swing_span` / `swing_timed` are the worker's guarded helpers; the gauges the alert rules read
are `baskfy_api.metrics`'s and are refreshed from `baskfy_api.swing_health` at scrape time.
"""

from __future__ import annotations

import datetime as dt

import pytest
from helpers import requires_db
from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import metrics
from baskfy_core.models import AppUser, SwConfig
from baskfy_worker import telemetry
from baskfy_worker.steps import StepOutcome, StepStatus
from baskfy_worker.tasks.swing import run_detect_swing
from baskfy_worker.tasks.swing_premarket import run_swing_premarket


class ExplodingTracer:
    def start_as_current_span(self, name: str) -> object:
        raise RuntimeError("collector unreachable")


class ScopeThatFailsToClose:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def __enter__(self) -> ScopeThatFailsToClose:
        return self

    def __exit__(self, *exc: object) -> None:
        raise RuntimeError("exporter is on fire")

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


class TracerThatFailsToClose:
    def __init__(self) -> None:
        self.scope = ScopeThatFailsToClose()

    def start_as_current_span(self, name: str) -> ScopeThatFailsToClose:
        return self.scope


class TestSwingSpanNeverRaises:
    def test_swing_span_runs_the_body_when_the_tracer_cannot_open_a_span(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(trace, "get_tracer", lambda name: ExplodingTracer())
        ran = False
        with telemetry.swing_span("swing.detect", date="2026-09-03"):
            ran = True
        assert ran

    def test_swing_span_keeps_the_bodys_own_exception_when_the_span_cannot_close(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tracer = TracerThatFailsToClose()
        monkeypatch.setattr(trace, "get_tracer", lambda name: tracer)
        with pytest.raises(ValueError, match="the body"), telemetry.swing_span("swing.eod", a=1):
            raise ValueError("the body")
        assert tracer.scope.attributes == {"a": 1}
        # ...and a body that succeeds under a scope that cannot close is still a success.
        with telemetry.swing_span("swing.eod"):
            pass

    def test_swing_timed_records_ok_and_failed_and_survives_a_raising_metric(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: list[tuple[str, str]] = []

        def observe(*, task: str, status: str, duration_seconds: float) -> None:
            recorded.append((task, status))
            assert duration_seconds >= 0

        monkeypatch.setattr(telemetry, "observe_swing_task", observe)
        with telemetry.swing_timed("detect"):
            pass
        with pytest.raises(RuntimeError), telemetry.swing_timed("eod"):
            raise RuntimeError("the job")
        assert recorded == [("detect", "ok"), ("eod", "failed")]

        def boom(**_: object) -> None:
            raise RuntimeError("metrics sink down")

        monkeypatch.setattr(telemetry, "observe_swing_task", boom)
        with telemetry.swing_timed("premarket_gaps"):
            pass

    def test_observe_swing_task_metric_is_in_the_registry_and_never_raises(self) -> None:
        metrics.observe_swing_task(task="detect", status="ok", duration_seconds=1.5)
        value = metrics.REGISTRY.get_sample_value(
            "baskfy_swing_task_duration_seconds_count", {"task": "detect", "status": "ok"}
        )
        assert value is not None and value >= 1


@requires_db
@pytest.mark.db
class TestTheJobsCompleteWithARaisingSink:
    async def _user(self, session: AsyncSession) -> int:
        user = AppUser(public_id="sw11-tel", email="sw11-tel@example.com")
        session.add(user)
        await session.flush()
        session.add(SwConfig(user_id=user.id, updated_by="test"))
        await session.flush()
        return int(user.id)

    async def test_run_detect_swing_completes_when_telemetry_raises(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The tracer explodes on open and the histogram explodes on observe; the step still
        answers — here a SKIPPED with its reason, on a date with no bars."""
        monkeypatch.setattr(trace, "get_tracer", lambda name: ExplodingTracer())

        def boom(**_: object) -> None:
            raise RuntimeError("metrics sink down")

        monkeypatch.setattr(telemetry, "observe_swing_task", boom)
        user_id = await self._user(session)
        outcome = StepOutcome()
        written = await run_detect_swing(session, outcome, dt.date(2026, 8, 18), user_id=user_id)
        assert written == 0 and outcome.status is StepStatus.SKIPPED
        assert "no published bars" in str(outcome.detail["skipped_reason"])

    async def test_run_swing_premarket_completes_when_telemetry_raises(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(trace, "get_tracer", lambda name: ExplodingTracer())
        monkeypatch.setattr(
            telemetry, "observe_swing_task", lambda **_: (_ for _ in ()).throw(RuntimeError("x"))
        )
        user_id = await self._user(session)
        report = await run_swing_premarket(
            session, StepOutcome(), dt.date(2026, 8, 18), user_id=user_id, stage="LEVELS"
        )
        assert report.stage == "LEVELS"
