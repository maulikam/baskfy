"""Celery bindings for the pipeline steps.

The work itself lives in the ``run_*`` coroutines beside this module; these are thin wrappers that
give each one a broker identity, a queue and a retry policy. Keeping them separate is what lets
the acceptance tests drive the whole chain in-process against a real database, with no broker and
no worker — a pipeline you can only exercise through Celery is a pipeline you cannot test.

Task names follow the routing prefixes in ``baskfy_worker.celery_app``:
``baskfy.comgest.*`` -> the ingest queue, ``baskfy.compute.*`` -> compute,
``baskfy.pipeline.*`` -> default.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
from typing import Final

from celery import shared_task
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.screener import (
    WARM_CACHE_SCREEN_LIMIT,
    ScreenCache,
    WarmCacheResult,
    warm_screen_cache,
)
from baskfy_core.models.base import JsonObject
from baskfy_providers.errors import TransientProviderError
from baskfy_providers.factory import build_kite_provider
from baskfy_providers.settings import get_provider_settings
from baskfy_worker import kite_session_cli, ops
from baskfy_worker.alerts import Alert, AlertName, Severity, dispatch
from baskfy_worker.celery_app import IST, QUEUE_COMPUTE, QUEUES
from baskfy_worker.db import run_checkpointed, run_in_session
from baskfy_worker.orchestrator import PipelineOutcome, run_nightly_pipeline
from baskfy_worker.providers import build_cache, build_pipeline_dependencies
from baskfy_worker.settings import get_worker_settings
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.adjustments import instruments_with_actions, reprocess_instrument
from baskfy_worker.tasks.alerts import SWEEP_BATCH, run_alert_dispatch, run_webhook_sweep
from baskfy_worker.tasks.backtests import (
    BacktestNotRunnable,
    build_backtest_archive,
    build_publisher,
    run_backtest_job,
)
from baskfy_worker.tasks.curated_batch_sync import run_curated_batch_sync
from baskfy_worker.tasks.curated_dividends import run_curated_dividends
from baskfy_worker.tasks.curated_metrics import run_curated_metrics
from baskfy_worker.tasks.curated_rebalance_notify import run_curated_rebalance_notify
from baskfy_worker.tasks.curated_sip import run_curated_sip_reminders
from baskfy_worker.tasks.portfolio_nav_job import run_portfolio_nav
from baskfy_worker.tasks.purge_accounts import run_purge_accounts
from baskfy_worker.tasks.resync import run_resync
from baskfy_worker.tasks.swing import (
    WEEKEND_SCAN_SESSIONS,
    recent_trading_days,
    run_detect_swing,
)
from baskfy_worker.tasks.swing_backtest import DEFAULT_START, SWING_BACKTEST_TASK, run_and_commit
from baskfy_worker.tasks.swing_eod import run_swing_eod
from baskfy_worker.tasks.swing_premarket import STAGE_GAPS, QuoteSource, run_swing_premarket
from baskfy_worker.telemetry import provider_retry_hooks

#: Earliest IST wall-clock at which a session's own data can exist. NSE closes at 15:30 and
#: publishes the bhavcopy afterwards; the schedule itself fires at 18:45 for that reason. Used to
#: tell "today, already closed" from "today, still ahead of us" — a distinction `is_trading_day`
#: cannot make and which redelivered tasks waking after midnight get wrong.
SESSION_DATA_READY_IST: Final = dt.time(18, 0)

#: docs/09 §"Kite specifics" — a rate-limited or flaky upstream is worth retrying; a malformed
#: payload or a missing credential is not. Only transient provider failures auto-retry.
RETRYABLE: tuple[type[Exception], ...] = (TransientProviderError,)


@shared_task(name="baskfy.pipeline.nightly", acks_late=True)
def nightly_pipeline(trade_date: str | None = None) -> JsonObject:
    """docs/03's ten-step chain for one trade date.

    Deliberately not auto-retried. A failed gate must not be re-attempted on a timer: docs/03 says
    the site keeps serving the previous ``data_version``, and a retry loop would either publish
    the same broken day later or bury the alert under repeated identical failures.

    Three transactions, not one (Prompt 17 deliverable 3 and its first acceptance criterion):

    1. ``begin_run`` commits the ``pipeline_run`` row **before** the chain starts, so the run
       exists even if this process is killed a second later.
    2. the chain itself, in the one transaction ``baskfy_worker.orchestrator`` describes.
    3. if the chain raised something it did not convert into a ``HardFailure``, its transaction
       has already rolled back — so the failure is recorded on a third session that can still
       commit. A ``SIGKILL`` reaches none of these, which is what
       ``baskfy.ops.reap_abandoned_runs`` is for.
    """
    day = dt.date.fromisoformat(trade_date) if trade_date else dt.datetime.now(tz=IST).date()

    # Prompt 3 deliverable 7: "never attempt to ingest or compute for a non-trading day."
    #
    # The schedule fires every evening, and until now the task took whatever date that was. On
    # 2026-08-28 — a holiday — it ran the full chain against a day with no session, wrote zero
    # bars, and the quality gate correctly refused it: "0 bars against a 10-day median of 2532".
    # That is a **failed run and a CRITICAL alert for a day the exchange was shut**, which trains
    # an operator to ignore the alert that matters. Every weekend did the same.
    #
    # An explicit `trade_date` is honoured regardless: a human asking for a specific day has a
    # reason, and refusing them here would make a backfill impossible to drive by hand.
    if trade_date is None and not run_in_session(lambda session: ops.is_trading_day(session, day)):
        return {
            "trade_date": day.isoformat(),
            "status": "skipped",
            "reason": "not a trading day",
        }

    # A trading day is not the same as a *finished* trading day, and the difference bit us on
    # 2026-09-01. The schedule fires at 18:45, when `now().date()` is the session that just
    # closed — correct. But this task resolves its date at EXECUTION time, and Celery redelivers
    # an unacknowledged task when a worker restarts. Three deploys on the evening of 31 Aug
    # redelivered the nightly; each copy woke past midnight, computed `now().date()` as the NEXT
    # day, found it a perfectly good trading Tuesday, and ran the full chain against a session
    # that had not happened. Runs 17 and 18 both failed on zero bars.
    #
    # That is the same wrong lesson the holiday guard above exists to prevent — a CRITICAL alert
    # for a day the data could not possibly exist for. NSE publishes the bhavcopy well after the
    # 15:30 close, so before the cutoff there is nothing to ingest and nothing to gate.
    #
    # An explicit `trade_date` is still honoured: a human asking for today at noon is doing a
    # rehearsal and knows it.
    if trade_date is None:
        now_ist = dt.datetime.now(tz=IST)
        if now_ist.date() == day and now_ist.time() < SESSION_DATA_READY_IST:
            return {
                "trade_date": day.isoformat(),
                "status": "skipped",
                "reason": (
                    f"the {day.isoformat()} session has not published yet "
                    f"(now {now_ist.time().strftime('%H:%M')} IST, data expected after "
                    f"{SESSION_DATA_READY_IST.strftime('%H:%M')})"
                ),
            }

    # Borrow the desk's Kite session before the chain starts (`kite_session_cli`, M58). A Kite
    # access token dies overnight, so the one stored yesterday is already useless; this is the
    # moment to replace it, while the desk's morning login is still current.
    #
    # Best-effort **by design**. The day's bars come from the NSE bhavcopy, which needs no
    # credential — what a missing session costs is history before 2024, holdings sync and
    # instrument metadata. Letting an unreachable desk fail the night would trade a working
    # pipeline for a broker login, which is the wrong way round.
    kite_session_cli.refresh_quietly()

    deps = build_pipeline_dependencies()
    run_id: int = run_in_session(lambda session: ops.begin_run(session, day))

    try:
        outcome: PipelineOutcome = run_in_session(
            lambda session: run_nightly_pipeline(session, day, deps, run_id=run_id)
        )
    except Exception as exc:
        # Bound to locals before the lambda: Python unbinds the `except` name at the end of the
        # block, and a closure that outlives it would raise `NameError` instead of reporting the
        # failure it was written to report.
        message, kind = str(exc), type(exc).__name__
        run_in_session(lambda session: ops.fail_run(session, run_id, message))
        _raise_alert(
            Alert(
                name=AlertName.PIPELINE_FAILED,
                severity=Severity.CRITICAL,
                summary=f"The nightly pipeline raised {kind} for {day.isoformat()}.",
                labels={"trade_date": day.isoformat(), "run_id": str(run_id)},
                detail={"error": message, "error_type": kind},
                runbook=ops.RUNBOOKS[AlertName.PIPELINE_FAILED],
            )
        )
        raise

    alert = ops.alert_for_outcome(outcome)
    if alert is not None:
        _raise_alert(alert)
    return {
        "run_id": outcome.run_id,
        "trade_date": outcome.trade_date.isoformat(),
        "status": outcome.status.value,
        "data_version": outcome.data_version,
        "failed_step": outcome.failed_step.value if outcome.failed_step else None,
        "error": outcome.error,
        "steps_completed": [s.value for s in outcome.steps_completed],
        "alert": alert.name.value if alert else None,
    }


def _raise_alert(alert: Alert) -> JsonObject:
    """Dispatch one alert from synchronous Celery code.

    ``dispatch`` is a coroutine because the mail transport is; a Celery task body is not. Same
    ``asyncio.run`` boundary as ``baskfy_worker.db.run_in_session``, and for the same reason.
    """
    return asyncio.run(dispatch(alert))


@shared_task(name="baskfy.ops.reap_abandoned_runs")
def reap_abandoned_runs_task() -> JsonObject:
    """Find pipeline runs nobody is running any more, fail them, and alert.

    Prompt 17's first acceptance criterion in one task. Scheduled every fifteen minutes rather
    than nightly: an abandoned run is an outage in progress, and finding it the following evening
    is finding it after the market has already opened on stale data.
    """

    async def sweep(session: AsyncSession) -> list[JsonObject]:
        alerts = await ops.reap_abandoned_runs(session)
        return [await dispatch(alert) for alert in alerts]

    dispatched = run_in_session(sweep)
    return {"reaped": len(dispatched), "alerts": dispatched}


@shared_task(name="baskfy.ops.check_publish_deadline")
def check_publish_deadline_task() -> JsonObject:
    """docs/11 §Reliability: "data published by 20:15 IST on >=95% of trading days."

    Scheduled at the deadline itself. Silent on a non-trading day and on a day that published.
    """

    async def check(session: AsyncSession) -> JsonObject | None:
        alert = await ops.check_publish_deadline(session)
        return None if alert is None else await dispatch(alert)

    dispatched = run_in_session(check)
    return {"alert": dispatched}


@shared_task(name="baskfy.ops.check_kite_token")
def check_kite_token_task() -> JsonObject:
    """docs/09 calls Kite token expiry "the #1 pipeline failure" — so warn before it bites.

    Hourly. The check is arithmetic over the stored issue time and makes no network call, so
    running it often costs nothing and the warning lands with hours to spare.
    """
    alert = ops.check_kite_token()
    return {"alert": None if alert is None else _raise_alert(alert)}


@shared_task(name="baskfy.ops.check_queue_backlog")
def check_queue_backlog_task() -> JsonObject:
    """One alert per Celery queue that has backed up past the configured threshold."""
    cache = build_cache()
    if cache is None:
        return {"alerts": [], "detail": "no broker client could be built"}

    async def check() -> list[JsonObject]:
        try:
            alerts = await ops.check_queue_backlog(cache, QUEUES)
            return [await dispatch(alert) for alert in alerts]
        finally:
            await cache.aclose()

    dispatched = asyncio.run(check())
    return {"alerts": dispatched}


@shared_task(name="baskfy.ops.resync", acks_late=True)
def resync_task(actor_user_id: int, days: int) -> JsonObject:
    """Leaf 3.1's resync button: find every pending gap, close what can be closed, report the rest.

    Deliberately **not** auto-retried. Every remedy it runs is already idempotent — the bhavcopy
    ingest upserts, the calendar correction is an upsert, the Kite pull re-verifies before
    storing — so a retry would be safe, but it would also silently repeat a run whose report the
    operator is waiting to read. The honest answer to "it did not work" is the report saying so,
    not another attempt nobody asked for. Pressing the button again is one tap.

    Never places an order: it reaches ingestion, the trading calendar and the Kite *session*
    bridge, and imports nothing from ``packages/execution``.
    """
    return asyncio.run(run_resync(actor_user_id, days=days)).as_json()


@shared_task(
    name="baskfy.compute.reprocess_instrument",
    autoretry_for=RETRYABLE,
    retry_backoff=True,
    retry_jitter=True,
)
def reprocess_instrument_task(instrument_id: int) -> JsonObject:
    """docs/09 §"Adjustment algorithm" rebuild rule — idempotent by construction."""
    result = run_in_session(lambda session: reprocess_instrument(session, instrument_id))
    return {
        "instrument_id": result.instrument_id,
        "bars_rewritten": result.bars_rewritten,
        "actions_applied": result.actions_applied,
        "unquantified": list(result.unquantified),
    }


@shared_task(name="baskfy.pipeline.integrity_audit")
def integrity_audit() -> JsonObject:
    """docs/09 §Schedule: "Sat 02:00 — full-history integrity audit".

    Rebuilds every instrument that carries a corporate action, which is the only way to notice an
    adjusted series that has drifted from its raw prints. Because ``reprocess_instrument`` is
    idempotent, a healthy history is rewritten to identical values and the audit is a no-op.
    """
    return run_in_session(_audit_all)


async def _audit_all(session: AsyncSession) -> JsonObject:
    targets = await instruments_with_actions(session)
    rewritten = 0
    unquantified: list[str] = []
    for instrument_id in targets:
        result = await reprocess_instrument(session, instrument_id)
        rewritten += result.bars_rewritten
        unquantified.extend(f"instrument {instrument_id}: {u}" for u in result.unquantified)
    return {
        "instruments": len(targets),
        "bars_rewritten": rewritten,
        "unquantified_actions": unquantified or None,
    }


@shared_task(name="baskfy.accounts.purge")
def purge_accounts_task() -> JsonObject:
    """Prompt 12 §5: close the seven-day soft-delete window.

    Daily rather than on a timer per account: an erasure a day late is within any reasonable
    reading of DPDP, and a scheduled sweep is one moving part instead of one per deletion.
    """

    async def purge(session: AsyncSession) -> JsonObject:
        outcome = StepOutcome()
        purged = await run_purge_accounts(session, outcome)
        return {"purged": purged, "considered": outcome.rows_in, "notes": outcome.detail or None}

    return run_in_session(purge)


@shared_task(name="baskfy.compute.warm_screen_cache")
def warm_screen_cache_task(limit: int = WARM_CACHE_SCREEN_LIMIT) -> JsonObject:
    """docs/06 §Caching: "Warm the top 200 most-run screen definitions after each publish."

    ``publish`` already warms inline, so this exists for the two cases that need it out of band:
    re-warming after a manual cache flush, and warming a freshly restarted Redis without waiting
    for the next nightly run. Not auto-retried — a cold cache is a slow first request, not an
    incident, and the next publish warms it anyway.
    """
    cache = build_cache()
    if cache is None or not isinstance(cache, ScreenCache):
        return {"warmed": 0, "skipped": 0, "failures": ["no usable cache client configured"]}

    async def warm(session: AsyncSession) -> WarmCacheResult:
        try:
            return await warm_screen_cache(session, cache, limit=limit)
        finally:
            await cache.aclose()

    result: WarmCacheResult = run_in_session(warm)
    return {
        "warmed": result.warmed,
        "skipped": result.skipped,
        "failures": list(result.failures),
    }


@shared_task(name="baskfy.backtest.run", acks_late=True)
def run_backtest_task(public_id: str, fragility: bool = True) -> JsonObject:
    """PROMPTS.md Prompt 15 §4: the backtest, on its own queue.

    Routed to ``backtest`` by the ``baskfy.backtest.*`` prefix in
    ``baskfy_worker.celery_app.TASK_ROUTES``, which is docs/03 §"Scaling plan" step 4: "Move
    backtest fan-out to a dedicated worker pool with its own queue."

    Deliberately **not** auto-retried. A backtest that failed on bad configuration will fail the
    same way in sixty seconds, and one that failed on a missing price series will fail until the
    backfill runs; both belong on the row where the user can read them (``backtest.error``), not
    in a retry loop that hides them. The concurrency caps are enforced when the run is *enqueued*
    (`baskfy_api.backtests.capacity_check`), so a retry storm would also breach them.
    """
    archive = build_backtest_archive()
    publisher = build_publisher()
    try:
        outcome = run_in_session(
            lambda session: run_backtest_job(
                session, public_id, archive=archive, publisher=publisher, fragility=fragility
            )
        )
    except BacktestNotRunnable as exc:
        # A redelivered message for a run somebody else already took. Not an error.
        return {"public_id": public_id, "status": "skipped", "detail": str(exc)}
    finally:
        if publisher is not None:
            publisher.close()
    return outcome.as_dict()


@shared_task(name="baskfy.alerts.dispatch", acks_late=True)
def dispatch_screen_alerts_task(trade_date: str | None = None) -> JsonObject:
    """PROMPTS.md Prompt 20 §3: the nightly screen-alert email, after publish.

    Deliberately **not** auto-retried. Every send is recorded in ``screen_alert_delivery``, which
    is unique on ``(alert, as_of)``, so re-running the task for a date is a no-op rather than a
    second email — but a retry loop on a broken mail transport would still hammer the provider
    for the alerts that *had* not been recorded yet, and the failure belongs in an alert (Prompt
    17) rather than in a loop.
    """
    day = dt.date.fromisoformat(trade_date) if trade_date else dt.datetime.now(tz=IST).date()
    return run_in_session(lambda session: run_alert_dispatch(session, day))


@shared_task(name="baskfy.alerts.sweep_webhooks")
def sweep_webhooks_task(limit: int = SWEEP_BATCH) -> JsonObject:
    """PROMPTS.md Prompt 20 §4's "retry with backoff", driven by a timer rather than by Celery.

    The backoff schedule lives on the ``webhook_delivery`` row (``next_attempt_at``), not in
    Celery's retry machinery, for one reason: a retry held inside a task is lost when the worker
    dies, and a webhook that a receiver never got and nobody remembers to resend is worse than a
    late one. The row survives a restart; the sweep picks it up.
    """
    return run_in_session(lambda session: run_webhook_sweep(session, limit=limit))


@shared_task(name="baskfy.cb.compute_metrics", acks_late=True)
def compute_curated_metrics_task(trade_date: str | None = None) -> JsonObject:
    """SC2: upsert ``cb_metrics`` for every curated basket on a trading day.

    Idempotent per ``(basket_id, as_of_date)``. Defaults to today in IST when Beat fires without
    an argument.

    Uses its own session (no outer ``begin()``): ``compute_all_metrics`` commits per chunk and
    per basket. Wrapping it in ``run_in_session``'s transaction context closes the transaction
    on the first commit and raises ``Can't operate on closed transaction``.
    """
    day = dt.date.fromisoformat(trade_date) if trade_date else dt.datetime.now(tz=IST).date()

    async def _run() -> JsonObject:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: PLC0415

        from baskfy_api.settings import get_settings  # noqa: PLC0415

        engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as session:
                return await run_curated_metrics(session, day)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


@shared_task(name="baskfy.cb.sip_reminders", acks_late=True)
def curated_sip_reminders_task(as_of: str | None = None) -> JsonObject:
    """SC7: raise ``SIP_DUE`` pending actions for ACTIVE REMINDER plans due on *as_of*.

    Idempotent per ``(plan_id, YYYY-MM)``. Never places an order; AUTO plans are not selected.
    Defaults to today in IST when Beat fires without an argument.
    """
    day = dt.date.fromisoformat(as_of) if as_of else dt.datetime.now(tz=IST).date()
    return run_in_session(lambda session: run_curated_sip_reminders(session, day))


@shared_task(name="baskfy.cb.derive_dividends", acks_late=True)
def curated_dividends_task(as_of: str | None = None) -> JsonObject:
    """SC4: derive ``cb_dividend`` from cash corporate actions x ACTIVE holdings.

    Idempotent per ``(investment_id, instrument_id, ex_date)``. Never places an order.
    Defaults to today in IST when Beat fires without an argument.
    """
    day = dt.date.fromisoformat(as_of) if as_of else dt.datetime.now(tz=IST).date()
    return run_in_session(lambda session: run_curated_dividends(session, day))


@shared_task(name="baskfy.cb.sync_batches", acks_late=True)
def curated_batch_sync_task() -> JsonObject:
    """T8.2: promote PLANNED batches to EXECUTED from the desk journal.

    Synthetic ``cb-sim-*`` ids stay PLANNED. Never places an order.
    """
    return run_in_session(run_curated_batch_sync)


@shared_task(name="baskfy.cb.rebalance_notify", acks_late=True)
def curated_rebalance_notify_task() -> JsonObject:
    """T8.3: email once per open REBALANCE_AVAILABLE lacking payload.notified_at."""
    return run_in_session(run_curated_rebalance_notify)


@shared_task(name="baskfy.portfolio.eod_nav", acks_late=True)
def portfolio_eod_nav_task(as_of: str | None = None) -> JsonObject:
    """PORTFOLIO_REDESIGN.md §5.1: the official end-of-day NAV for every user, for one date.

    Idempotent per ``(user_id, date, portfolio_id)``, so re-running a date after a late ingest
    corrects the day rather than duplicating it. Writes no rows at all on a date the market did
    not print, which is why it is not auto-retried: "no close exists" is an answer, not a failure,
    and a retry loop would only ask it again.

    Never places an order and reaches no broker. Defaults to today in IST when Beat fires without
    an argument.
    """
    day = dt.date.fromisoformat(as_of) if as_of else dt.datetime.now(tz=IST).date()
    return run_in_session(lambda session: run_portfolio_nav(session, day)).as_json()


# --- SW3: the swing book's own detection job ---------------------------------
#
# Callable on its own as well as from the nightly chain, because the two have different
# audiences. The chain runs it after `publish` and cannot let it fail the night; this task is
# what `make swing DATE=...` and the Saturday weekend scan use, and there a failure should be
# visible rather than swallowed.


@shared_task(name="baskfy.swing.detect", acks_late=True)
def swing_detect_task(trade_date: str | None = None) -> JsonObject:
    """Detect the day's setups and write the day's market row (`docs/swing/06` SW3).

    Idempotent per `(user_id, date)`: re-running a date overwrites its own rows. Defaults to
    today in IST when Beat fires without an argument.
    """
    day = dt.date.fromisoformat(trade_date) if trade_date else dt.datetime.now(tz=IST).date()
    deps = build_pipeline_dependencies()
    if deps.swing_user_id is None:
        return {"date": day.isoformat(), "skipped": "no BASKFY_SOLE_USER_ID configured"}

    async def _run(session: AsyncSession) -> JsonObject:
        outcome = StepOutcome()
        written = await run_detect_swing(
            session,
            outcome,
            day,
            user_id=int(deps.swing_user_id or 0),
            index_slug=deps.swing_index_slug,
            execution_enabled=deps.swing_execution_enabled,
        )
        return {"date": day.isoformat(), "candidates": written, "detail": outcome.detail}

    return run_in_session(_run)


@shared_task(name="baskfy.swing.weekend", acks_late=True)
def swing_weekend_task(as_of: str | None = None) -> JsonObject:
    """The Saturday scan (`docs/swing/06` SW4): re-detect the last five sessions.

    Not the same thing as running the nightly job five times for fun. `04` §2's flag statuses are
    a snapshot of one day, and a Saturday scan is how a person builds next week's watchlist —
    "which names were setting up at any point this week" is a different question from "which were
    setting up on Friday", and a base that tightened on Wednesday and drifted on Friday is
    exactly the one he wants to see.

    Idempotent for the same reason the nightly job is: each date overwrites its own rows.
    """
    day = dt.date.fromisoformat(as_of) if as_of else dt.datetime.now(tz=IST).date()
    deps = build_pipeline_dependencies()
    if deps.swing_user_id is None:
        return {"as_of": day.isoformat(), "skipped": "no BASKFY_SOLE_USER_ID configured"}

    async def _run(session: AsyncSession) -> JsonObject:
        sessions = await recent_trading_days(session, day, WEEKEND_SCAN_SESSIONS)
        results: list[JsonObject] = []
        for one in sessions:
            outcome = StepOutcome()
            written = await run_detect_swing(
                session,
                outcome,
                one,
                user_id=int(deps.swing_user_id or 0),
                index_slug=deps.swing_index_slug,
                execution_enabled=deps.swing_execution_enabled,
            )
            results.append({"date": one.isoformat(), "candidates": written})
        return {"as_of": day.isoformat(), "sessions": results}

    return run_in_session(_run)


@shared_task(name="baskfy.swing.eod", acks_late=True)
def swing_eod_task(trade_date: str | None = None) -> JsonObject:
    """SW5: manage the book, plan tomorrow, email the summary, count the session.

    Separate from `baskfy.swing.detect` and always **after** it: the plan is built from the
    day's candidates and the day's gate, and an evening job that ran before the detectors would
    plan against yesterday's tape.
    """
    day = dt.date.fromisoformat(trade_date) if trade_date else dt.datetime.now(tz=IST).date()
    deps = build_pipeline_dependencies()
    if deps.swing_user_id is None:
        return {"date": day.isoformat(), "skipped": "no BASKFY_SOLE_USER_ID configured"}

    async def _run(session: AsyncSession) -> JsonObject:
        outcome = StepOutcome()
        report = await run_swing_eod(
            session,
            outcome,
            day,
            user_id=int(deps.swing_user_id or 0),
            execution_enabled=deps.swing_execution_enabled,
        )
        return {"date": day.isoformat(), **report.as_detail()}

    return run_in_session(_run)


@shared_task(name=SWING_BACKTEST_TASK, acks_late=True, queue=QUEUE_COMPUTE)
def swing_backtest_task(
    start: str | None = None,
    end: str | None = None,
    sleeve_inr: str | None = None,
    cost_pct_per_side: str | None = None,
) -> JsonObject:
    """SW9: run `04` §11 over ``start..end`` and store the run in ``sw_backtest_run``.

    No Beat entry — a run over nine years is something a person asks for, from
    ``tools/swing/backtest.py`` or by name — and the compute queue, because it is a few minutes
    of Polars over every bar since 2017 and must not sit in front of an alert. ``start``
    defaults to `04` §11's 2017; ``end`` to today in IST. Money arrives as strings so it can be
    ``Decimal`` all the way (house rule 9). The body owns its commits (``run_checkpointed``) so
    a failed run is a durable row with its error, not a rollback.
    """
    first = dt.date.fromisoformat(start) if start else DEFAULT_START
    last = dt.date.fromisoformat(end) if end else dt.datetime.now(tz=IST).date()
    deps = build_pipeline_dependencies()
    if deps.swing_user_id is None:
        return {"start": first.isoformat(), "skipped": "no BASKFY_SOLE_USER_ID configured"}
    user_id = int(deps.swing_user_id or 0)

    async def _run(session: AsyncSession) -> JsonObject:
        return await run_and_commit(
            session,
            user_id=user_id,
            start=first,
            end=last,
            sleeve_inr=Decimal(sleeve_inr) if sleeve_inr else None,
            cost_pct_per_side=Decimal(cost_pct_per_side) if cost_pct_per_side else None,
        )

    return run_checkpointed(_run)


@shared_task(name="baskfy.swing.premarket", acks_late=True)
def swing_premarket_task(session_date: str | None = None, stage: str = STAGE_GAPS) -> JsonObject:
    """SW6: refresh the levels (08:50), scan the pre-open for gaps and plan the morning (09:09).

    The quote source is the Kite provider, built here and only when
    ``BASKFY_SWING_EP_PREMARKET_ENABLED`` is true — with the flag off the task makes no Kite
    call at all, and the tests assert that by handing the job a fake that counts.
    """
    day = dt.date.fromisoformat(session_date) if session_date else dt.datetime.now(tz=IST).date()
    deps = build_pipeline_dependencies()
    if deps.swing_user_id is None:
        return {"date": day.isoformat(), "skipped": "no BASKFY_SOLE_USER_ID configured"}
    settings = get_worker_settings()
    quotes: QuoteSource | None = None
    if settings.swing_ep_premarket_enabled and stage == STAGE_GAPS:
        quotes = build_kite_provider(get_provider_settings(), provider_retry_hooks())

    async def _run(session: AsyncSession) -> JsonObject:
        outcome = StepOutcome()
        report = await run_swing_premarket(
            session,
            outcome,
            day,
            user_id=int(deps.swing_user_id or 0),
            stage=stage,
            ep_premarket_enabled=settings.swing_ep_premarket_enabled,
            quotes=quotes,
            now=dt.datetime.now(tz=IST).replace(tzinfo=None),
            execution_enabled=settings.swing_execution_enabled,
        )
        return {"date": day.isoformat(), **report.as_detail()}

    return run_in_session(_run)
