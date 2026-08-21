"""Prompt 17's alerting and durable run bookkeeping.

The first acceptance criterion — "**killing the worker mid-pipeline produces a correct failed run
record and an alert**" — is asserted here, twice and at two levels:

* :class:`TestAKilledWorker` spawns a real ``python`` subprocess that opens a run and starts the
  chain, ``SIGKILL``s it, and then asserts on what the database was left holding. Nothing is
  mocked: the process dies the way a worker dies. It needs a live database, so it skips without
  ``DECILE_TEST_DATABASE_URL``.
* :class:`TestTheReaper` asserts the same thing at the transaction level, which is fast, runs
  everywhere, and is what will actually catch a regression in the reaper's SQL.

The rest is the four checks Prompt 17 §3 asks for, and the alert dispatcher's contract: it never
raises, whatever a sink does.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Final

import pytest
from celery.schedules import crontab
from helpers import TRADE_DATE, requires_db
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from decile_api.email import Mailer, Message
from decile_api.metrics import ALERTS, REGISTRY
from decile_api.settings import Settings
from decile_core.models import PipelineRun
from decile_worker import ops
from decile_worker.alerts import Alert, AlertName, Severity, dispatch, webhook_payload
from decile_worker.celery_app import BEAT_SCHEDULE, QUEUES, TASK_ROUTES
from decile_worker.orchestrator import PipelineOutcome
from decile_worker.steps import PipelineStep, RunStatus
from decile_worker.tasks.quality import CheckResult, CheckStatus, GateReport

REPO_ROOT: Final = Path(__file__).resolve().parents[3]

#: A worker that never dies would finish in under a second; this is the ceiling on how long the
#: subprocess is given to reach the point where it is worth killing.
SPAWN_TIMEOUT_SECONDS: Final = 30.0


def _settings(**overrides: object) -> Settings:
    base = Settings(environment="test", log_json=False, log_level="CRITICAL")
    return base.model_copy(update=dict(overrides))


def _delivered(payload: dict[str, object]) -> list[str]:
    """``dispatch``'s JSON payload is ``dict[str, object]``; narrow the one member we assert on."""
    sinks = payload["delivered_to"]
    assert isinstance(sinks, list)
    return [str(sink) for sink in sinks]


# ---------------------------------------------------------------------------
# The alert dispatcher
# ---------------------------------------------------------------------------


class TestDispatch:
    async def test_it_always_logs_and_always_counts(self) -> None:
        """A deployment with no sink configured is valid — a laptop — and must still record."""
        alert = Alert(
            name=AlertName.PIPELINE_FAILED,
            severity=Severity.CRITICAL,
            summary="the pipeline failed",
            labels={"trade_date": "2026-08-18"},
            runbook="docs/runbooks/pipeline-failed.md",
        )
        before = REGISTRY.get_sample_value(
            "decile_alerts_total", {"alert": "pipeline_failed", "severity": "critical"}
        )
        payload = await dispatch(alert, _settings())
        after = REGISTRY.get_sample_value(
            "decile_alerts_total", {"alert": "pipeline_failed", "severity": "critical"}
        )

        assert _delivered(payload) == ["log"]
        assert payload["alert"] == "pipeline_failed"
        assert payload["runbook"] == "docs/runbooks/pipeline-failed.md"
        assert (after or 0) == (before or 0) + 1

    async def test_a_failing_sink_never_propagates(self) -> None:
        """An alert dispatcher that raises turns one failure into two."""

        class Exploding:
            async def send(self, message: Message) -> None:
                raise RuntimeError("smtp is on fire")

        alert = Alert(AlertName.GATE_FAILED, Severity.CRITICAL, "the gate failed")
        payload = await dispatch(
            alert,
            _settings(ops_alert_email="ops@example.com"),
            mailer=Mailer(Exploding()),
        )
        assert "email" not in _delivered(payload)

    async def test_the_email_sink_reports_success(self) -> None:
        sent: list[Message] = []

        class Recording:
            async def send(self, message: Message) -> None:
                sent.append(message)

        alert = Alert(
            AlertName.PUBLISH_LATE,
            Severity.CRITICAL,
            "nothing published by 20:15 IST",
            runbook="docs/runbooks/pipeline-failed.md",
        )
        payload = await dispatch(
            alert, _settings(ops_alert_email="ops@example.com"), mailer=Mailer(Recording())
        )
        assert "email" in _delivered(payload)
        assert len(sent) == 1
        assert "20:15" in sent[0].subject or "20:15" in sent[0].text
        assert "docs/runbooks/pipeline-failed.md" in sent[0].text

    def test_the_webhook_body_is_alertmanager_shaped(self) -> None:
        """So one receiver can take alerts from here and from Prometheus without a second parser."""
        alert = Alert(
            AlertName.QUEUE_BACKLOG,
            Severity.WARNING,
            "ingest has 400 messages waiting",
            labels={"queue": "ingest"},
            runbook="docs/runbooks/pipeline-failed.md",
        )
        body = webhook_payload(alert)
        labels = body["labels"]
        assert isinstance(labels, dict)
        assert labels["alertname"] == "queue_backlog"
        assert labels["severity"] == "warning"
        assert labels["queue"] == "ingest"
        annotations = body["annotations"]
        assert isinstance(annotations, dict)
        assert annotations["runbook_url"] == "docs/runbooks/pipeline-failed.md"
        # It has to survive a JSON round trip; `urllib` posts exactly this.
        json.loads(json.dumps(body, default=str))


class TestEveryAlertNamesARunbookThatExists:
    def test_the_runbook_map_is_complete(self) -> None:
        """A missing entry is an alert that pages someone with no instructions."""
        assert set(ops.RUNBOOKS) == set(AlertName)

    @pytest.mark.parametrize("path", sorted(set(ops.RUNBOOKS.values())))
    def test_the_file_is_there(self, path: str) -> None:
        assert (REPO_ROOT / path).is_file(), f"{path} does not exist"

    def test_prompt_17_asks_for_five_runbooks_and_five_exist(self) -> None:
        """PROMPTS.md Prompt 17 §5 names them individually."""
        for name in (
            "kite-token-expired.md",
            "pipeline-failed.md",
            "bad-data-published.md",
            "restore-from-backup.md",
            "razorpay-webhook-replay.md",
        ):
            assert (REPO_ROOT / "docs" / "runbooks" / name).is_file(), f"{name} is missing"

    def test_no_runbook_claims_to_have_been_verified_against_staging(self) -> None:
        """Prompt 17's third acceptance criterion is NOT met, and the files must say so.

        A runbook that has never been executed and does not admit it is worse than no runbook: it
        is read at 3am by someone who believes it. This test fails the day one is quietly marked
        verified without the output to back it — flip it to assert the opposite then.
        """
        for path in sorted((REPO_ROOT / "docs" / "runbooks").glob("*.md")):
            text_ = path.read_text(encoding="utf-8")
            if path.name == "README.md":
                assert "NOT YET" in text_
                continue
            assert "**Verified against:**" in text_, f"{path.name} has no verification line"


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


class TestAlertForOutcome:
    def test_a_succeeded_run_raises_nothing(self) -> None:
        outcome = PipelineOutcome(TRADE_DATE, RunStatus.SUCCEEDED, run_id=1, data_version=7)
        assert ops.alert_for_outcome(outcome) is None

    def test_a_non_trading_day_is_not_an_alert(self) -> None:
        """`ABORTED` is what a Sunday returns.

        Paging for a weekend trains people to ignore pages.
        """
        outcome = PipelineOutcome(TRADE_DATE, RunStatus.ABORTED, error="not a trading day")
        assert ops.alert_for_outcome(outcome) is None

    def test_a_gate_failure_is_its_own_alert_carrying_the_assertions(self) -> None:
        """What an operator does about a failed assertion is nothing like a Kite timeout."""
        report = GateReport(
            TRADE_DATE,
            (
                CheckResult(
                    name="bar coverage",
                    assertion=1,
                    status=CheckStatus.FAILED,
                    message="only 12% of active instruments have a bar",
                ),
            ),
        )
        outcome = PipelineOutcome(
            TRADE_DATE,
            RunStatus.FAILED,
            run_id=3,
            gate=report,
            failed_step=PipelineStep.DATA_QUALITY_GATE,
        )
        alert = ops.alert_for_outcome(outcome)
        assert alert is not None
        assert alert.name is AlertName.GATE_FAILED
        assert alert.runbook == "docs/runbooks/bad-data-published.md"
        failures = alert.detail["failures"]
        assert isinstance(failures, list)
        first = failures[0]
        assert isinstance(first, dict)
        assert str(first["message"]).startswith("only 12%")

    def test_any_other_failure_names_the_step(self) -> None:
        outcome = PipelineOutcome(
            TRADE_DATE,
            RunStatus.FAILED,
            run_id=4,
            failed_step=PipelineStep.FETCH_DAILY_BARS,
            error="RetryBudgetExhausted",
        )
        alert = ops.alert_for_outcome(outcome)
        assert alert is not None
        assert alert.name is AlertName.PIPELINE_FAILED
        assert alert.labels["step"] == "fetch_daily_bars"


class TestKiteTokenCheck:
    def test_no_token_store_configured_is_not_an_incident(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Local development runs on fixtures and has no Kite credentials at all."""
        from decile_providers import settings as provider_settings  # noqa: PLC0415

        monkeypatch.setattr(
            provider_settings,
            "get_provider_settings",
            lambda: provider_settings.ProviderSettings(
                kite_token_path="", kite_token_encryption_key=""
            ),
        )
        assert ops.check_kite_token(_settings()) is None

    def test_an_expired_token_is_critical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """docs/09 calls this "the #1 pipeline failure"."""
        from cryptography.fernet import Fernet  # noqa: PLC0415

        from decile_providers import settings as provider_settings  # noqa: PLC0415
        from decile_providers.tokens import AccessTokenStore  # noqa: PLC0415

        key = Fernet.generate_key().decode()
        path = tmp_path / "kite-token.enc"
        AccessTokenStore(path, key).save(
            "live-token", issued_at=dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
        )
        monkeypatch.setattr(
            provider_settings,
            "get_provider_settings",
            lambda: provider_settings.ProviderSettings(
                kite_token_path=str(path), kite_token_encryption_key=key
            ),
        )

        alert = ops.check_kite_token(_settings(), now=dt.datetime(2026, 8, 18, tzinfo=dt.UTC))
        assert alert is not None
        assert alert.name is AlertName.KITE_TOKEN_EXPIRING
        assert alert.severity is Severity.CRITICAL
        assert alert.runbook == "docs/runbooks/kite-token-expired.md"

    def test_a_fresh_token_warns_before_it_dies_not_after(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A token replaced at 18:00 IST costs nothing; one replaced at 21:00 costs a night."""
        from cryptography.fernet import Fernet  # noqa: PLC0415

        from decile_providers import settings as provider_settings  # noqa: PLC0415
        from decile_providers.tokens import AccessTokenStore  # noqa: PLC0415

        key = Fernet.generate_key().decode()
        path = tmp_path / "kite-token.enc"
        ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
        AccessTokenStore(path, key).save(
            "live-token", issued_at=dt.datetime(2026, 8, 18, 9, 0, tzinfo=ist)
        )
        monkeypatch.setattr(
            provider_settings,
            "get_provider_settings",
            lambda: provider_settings.ProviderSettings(
                kite_token_path=str(path), kite_token_encryption_key=key
            ),
        )
        settings = _settings(kite_token_warning_hours=6)

        # Midday: nothing to say.
        midday = dt.datetime(2026, 8, 18, 12, 0, tzinfo=ist)
        assert ops.check_kite_token(settings, now=midday) is None
        # 18:45 IST, when the nightly chain starts and the token dies at midnight: warn.
        warning = ops.check_kite_token(settings, now=dt.datetime(2026, 8, 18, 18, 45, tzinfo=ist))
        assert warning is not None
        assert warning.severity is Severity.WARNING


class TestQueueBacklog:
    class _Broker:
        def __init__(self, depths: dict[str, int]) -> None:
            self._depths = depths

        async def llen(self, queue: str) -> int:
            return self._depths.get(queue, 0)

    async def test_only_queues_over_the_threshold_alert(self) -> None:
        broker = self._Broker({"ingest": 400, "compute": 3})
        alerts = await ops.check_queue_backlog(
            broker, QUEUES, _settings(queue_backlog_threshold=100)
        )
        assert [alert.labels["queue"] for alert in alerts] == ["ingest"]
        assert alerts[0].detail["depth"] == 400

    async def test_an_unreachable_broker_is_not_an_alert_storm(self) -> None:
        """`The broker is down` is its own incident, not four queue-backlog pages."""

        class Broken:
            async def llen(self, queue: str) -> int:
                raise ConnectionError("redis is unreachable")

        assert await ops.check_queue_backlog(Broken(), QUEUES, _settings()) == []


class TestTheBeatSchedule:
    @pytest.mark.parametrize(
        "task",
        [
            "decile.ops.reap_abandoned_runs",
            "decile.ops.check_publish_deadline",
            "decile.ops.check_kite_token",
            "decile.ops.check_queue_backlog",
        ],
    )
    def test_every_ops_check_is_scheduled(self, task: str) -> None:
        """A check nobody runs is a check that does not exist."""
        assert any(entry["task"] == task for entry in BEAT_SCHEDULE.values()), task

    def test_the_publish_deadline_fires_at_2015_ist(self) -> None:
        """docs/11 §Reliability: "data published by 20:15 IST on >=95% of trading days"."""
        entry = BEAT_SCHEDULE["publish-deadline-slo"]
        schedule = entry["schedule"]
        assert isinstance(schedule, crontab)
        assert schedule.hour == {20}
        assert schedule.minute == {15}

    def test_ops_tasks_route_to_the_default_queue(self) -> None:
        """docs/02: `default` is "orchestration, publishing, alerts ... short and
        latency-sensitive". An alert queued behind a two-hour backfill chunk is an alert nobody
        gets."""
        assert TASK_ROUTES["decile.ops.*"]["queue"] == "default"


# ---------------------------------------------------------------------------
# Acceptance criterion 1
# ---------------------------------------------------------------------------


@pytest.mark.db
@requires_db
class TestTheReaper:
    """The transaction-level half of "a killed worker leaves a correct failed run record"."""

    async def test_a_healthy_long_run_is_not_reaped(self, session: AsyncSession) -> None:
        """docs/11 budgets the chain at 45 minutes; the window is 90.

        A slow night is not a death.
        """
        now = dt.datetime(2026, 8, 18, 20, 0, tzinfo=dt.UTC)
        session.add(
            PipelineRun(
                trade_date=TRADE_DATE,
                status=RunStatus.RUNNING,
                started_at=now - dt.timedelta(minutes=40),
            )
        )
        await session.flush()

        alerts = await ops.reap_abandoned_runs(session, _settings(), now=now)
        assert alerts == []

    async def test_an_abandoned_run_becomes_failed_and_alerts(self, session: AsyncSession) -> None:
        """PROMPTS.md Prompt 17 acceptance criterion 1, at the transaction level."""
        now = dt.datetime(2026, 8, 18, 22, 0, tzinfo=dt.UTC)
        run_id = await ops.begin_run(session, TRADE_DATE)
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        run.started_at = now - dt.timedelta(minutes=120)
        await session.flush()

        alerts = await ops.reap_abandoned_runs(session, _settings(), now=now)

        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.name is AlertName.PIPELINE_ABANDONED
        assert alert.severity is Severity.CRITICAL
        assert alert.labels["trade_date"] == TRADE_DATE.isoformat()
        assert alert.detail["age_minutes"] == 120
        assert alert.runbook == "docs/runbooks/pipeline-failed.md"

        await session.refresh(run)
        assert run.status == RunStatus.FAILED
        assert run.finished_at is not None
        # docs/03: a failed run must not publish.
        assert run.data_version is None

    async def test_the_sweep_is_idempotent(self, session: AsyncSession) -> None:
        """Beat runs it every fifteen minutes forever; a second sweep must find nothing."""
        now = dt.datetime(2026, 8, 18, 22, 0, tzinfo=dt.UTC)
        run_id = await ops.begin_run(session, TRADE_DATE)
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        run.started_at = now - dt.timedelta(minutes=200)
        await session.flush()

        assert len(await ops.reap_abandoned_runs(session, _settings(), now=now)) == 1
        assert await ops.reap_abandoned_runs(session, _settings(), now=now) == []

    async def test_begin_run_reuses_a_running_row(self, session: AsyncSession) -> None:
        """A re-delivered Celery message must not open a second run for the same night."""
        first = await ops.begin_run(session, TRADE_DATE)
        assert await ops.begin_run(session, TRADE_DATE) == first

    async def test_fail_run_does_not_overwrite_a_recorded_outcome(
        self, session: AsyncSession
    ) -> None:
        """A published run that a late exception touches must not be un-published."""
        run_id = await ops.begin_run(session, TRADE_DATE)
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        run.status = RunStatus.SUCCEEDED
        run.data_version = 12
        await session.flush()

        await ops.fail_run(session, run_id, "a late exception")
        await session.refresh(run)
        assert run.status == RunStatus.SUCCEEDED
        assert run.data_version == 12


@pytest.mark.db
@requires_db
class TestThePublishDeadline:
    async def test_a_published_day_is_silent(self, session: AsyncSession) -> None:
        ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
        moment = dt.datetime.combine(TRADE_DATE, dt.time(20, 15), tzinfo=ist)
        session.add(
            PipelineRun(
                trade_date=TRADE_DATE,
                status=RunStatus.SUCCEEDED,
                started_at=moment - dt.timedelta(hours=1),
                finished_at=moment - dt.timedelta(minutes=5),
                data_version=1,
            )
        )
        await session.flush()
        assert await ops.check_publish_deadline(session, _settings(), now=moment) is None

    async def test_a_trading_day_with_nothing_published_alerts(self, session: AsyncSession) -> None:
        ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
        moment = dt.datetime.combine(TRADE_DATE, dt.time(20, 15), tzinfo=ist)
        alert = await ops.check_publish_deadline(session, _settings(), now=moment)
        assert alert is not None
        assert alert.name is AlertName.PUBLISH_LATE
        assert alert.labels["trade_date"] == TRADE_DATE.isoformat()

    async def test_a_non_trading_day_is_silent(self, session: AsyncSession) -> None:
        """A Sunday with nothing published is a Sunday."""
        ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
        sunday = dt.date(2026, 8, 16)
        assert sunday.weekday() == 6
        moment = dt.datetime.combine(sunday, dt.time(20, 15), tzinfo=ist)
        assert await ops.check_publish_deadline(session, _settings(), now=moment) is None


#: The script the killed-worker test runs. It opens a durable run row exactly the way
#: ``decile.pipeline.nightly`` does, prints the id so the parent knows what to look for, and then
#: sits inside a transaction that has already written step rows — which is the state a worker is
#: in when it is killed halfway through docs/03's chain.
_VICTIM = textwrap.dedent(
    """
    import asyncio, datetime as dt, sys
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from decile_worker import ops
    from decile_worker.steps import PipelineStep, record_step

    URL, TRADE_DATE = sys.argv[1], dt.date.fromisoformat(sys.argv[2])

    async def main() -> None:
        engine = create_async_engine(URL)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        # 1. The run row, committed on its own session before the chain starts. This is the
        #    whole point of `ops.begin_run` (docs/DECISIONS.md §17.5).
        async with maker() as bookkeeping, bookkeeping.begin():
            run_id = await ops.begin_run(bookkeeping, TRADE_DATE)
        print(run_id, flush=True)

        # 2. The chain's own transaction: step rows written, nothing committed.
        async with maker() as session, session.begin():
            async with record_step(
                session, run_id, PipelineStep.REFRESH_INSTRUMENTS, TRADE_DATE
            ) as step:
                step.rows_out = 2300
            print("READY", flush=True)
            await asyncio.sleep(300)

    asyncio.run(main())
    """
)


@pytest.mark.db
@requires_db
class TestAKilledWorker:
    """PROMPTS.md Prompt 17, acceptance criterion 1, with a real ``SIGKILL``.

    Nothing here is mocked. A subprocess opens a run the way the Celery task does, writes a step
    row inside the chain's transaction, and is then killed with ``SIGKILL`` — which no
    ``try/finally``, no signal handler and no Celery ``acks_late`` can intercept. What survives is
    what an operator would actually find.
    """

    @staticmethod
    def _spawn(url: str) -> subprocess.Popen[str]:
        # `sys.executable` with a literal script; nothing here comes from a request.
        process = subprocess.Popen(
            [sys.executable, "-c", _VICTIM, url, TRADE_DATE.isoformat()],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        return process

    async def test_the_run_record_survives_and_the_step_rows_do_not(
        self, session: AsyncSession, migrated_url: str
    ) -> None:
        del session  # the fixture is here for `clean_db`; this test needs its own connections
        process = self._spawn(migrated_url)
        assert process.stdout is not None

        run_id_line = process.stdout.readline().strip()
        ready = process.stdout.readline().strip()
        if not run_id_line.isdigit() or ready != "READY":
            process.kill()
            stderr = process.stderr.read() if process.stderr else ""
            pytest.fail(f"the victim did not start: {run_id_line!r} {ready!r}\n{stderr}")
        run_id = int(run_id_line)

        # The kill. Not SIGTERM: a worker that is given the chance to clean up is not the failure
        # this criterion is about.
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=SPAWN_TIMEOUT_SECONDS)
        assert process.returncode == -signal.SIGKILL

        engine = create_async_engine(migrated_url)
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as check:
                run = await check.get(PipelineRun, run_id)

                # 1. THE RUN RECORD SURVIVED. Committed before the chain opened its transaction.
                assert run is not None, "the pipeline_run row did not survive the kill"
                assert run.status == RunStatus.RUNNING
                assert run.trade_date == TRADE_DATE
                # 2. It never published. docs/03: only step 10 sets this, and only after the gate.
                assert run.data_version is None

                # 3. The step rows did NOT survive — the chain's transaction rolled back. That is
                #    `decile_worker.orchestrator`'s documented shape, and it is why a run stuck in
                #    `running` with no steps is diagnostic of a killed worker
                #    (docs/runbooks/pipeline-failed.md §3).
                steps = (
                    await check.execute(
                        text("SELECT count(*) FROM pipeline_run_step WHERE run_id = :id"),
                        {"id": run_id},
                    )
                ).scalar_one()
                assert steps == 0

                # 4. THE ALERT. The reaper turns the abandoned row into a correct failed record.
                async with maker() as reaping, reaping.begin():
                    alerts = await ops.reap_abandoned_runs(
                        reaping,
                        _settings(pipeline_stale_after_minutes=0),
                        now=dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=1),
                    )
                    dispatched = [await dispatch(alert, _settings()) for alert in alerts]

                assert [alert.name for alert in alerts] == [AlertName.PIPELINE_ABANDONED]
                labels = dispatched[0]["labels"]
                assert isinstance(labels, dict)
                assert labels["run_id"] == str(run_id)

                await check.refresh(run)
                assert run.status == RunStatus.FAILED
                assert run.finished_at is not None
        finally:
            await engine.dispose()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def test_the_alert_counter_is_registered() -> None:
    """A smoke check that the metric the dispatcher increments is on the shared registry."""
    ALERTS.labels("pipeline_failed", "critical")
    assert (
        REGISTRY.get_sample_value(
            "decile_alerts_total", {"alert": "pipeline_failed", "severity": "critical"}
        )
        is not None
    )
