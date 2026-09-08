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
import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Final

from celery import Task, shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.broker_oauth import (
    KiteLoginUrl,
    kite_login_url,
    token_encryption_key,
    token_store_path,
)
from baskfy_api.problems import Problem
from baskfy_api.screener import (
    WARM_CACHE_SCREEN_LIMIT,
    ScreenCache,
    WarmCacheResult,
    warm_screen_cache,
)
from baskfy_api.settings import get_settings as get_api_settings
from baskfy_api.swing_scan import request_scan
from baskfy_core.models import PipelineRun
from baskfy_core.models.base import JsonObject
from baskfy_providers.errors import TransientProviderError
from baskfy_providers.factory import KiteLane, build_kite_provider, build_nse_provider
from baskfy_providers.kite import KiteProvider
from baskfy_providers.records import QuoteRecord
from baskfy_providers.settings import get_provider_settings
from baskfy_providers.tokens import AccessTokenStore
from baskfy_worker import catch_up, kite_session_cli, ops
from baskfy_worker.alerts import Alert, AlertName, Severity, dispatch
from baskfy_worker.bhavcopy_backfill import backfill_bars_from_bhavcopy
from baskfy_worker.celery_app import IST, QUEUE_COMPUTE, QUEUES
from baskfy_worker.db import run_checkpointed, run_in_session
from baskfy_worker.orchestrator import PipelineOutcome, run_nightly_pipeline
from baskfy_worker.providers import build_cache, build_pipeline_dependencies, sole_user_id
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
from baskfy_worker.tasks.kite_login_nudge import NudgeWindow, run_login_nudge
from baskfy_worker.tasks.portfolio_nav_job import run_portfolio_nav
from baskfy_worker.tasks.purge_accounts import run_purge_accounts
from baskfy_worker.tasks.resync import run_resync
from baskfy_worker.tasks.swing import (
    WEEKEND_SCAN_SESSIONS,
    recent_trading_days,
    run_detect_swing,
)
from baskfy_worker.tasks.swing_backtest import DEFAULT_START, SWING_BACKTEST_TASK, run_and_commit
from baskfy_worker.tasks.swing_catalyst import run_swing_catalyst
from baskfy_worker.tasks.swing_eod import run_swing_eod
from baskfy_worker.tasks.swing_intraday import build_intraday_plan
from baskfy_worker.tasks.swing_ops import (
    check_detect_fresh,
    check_gtt_at_1515,
    check_monitor_started,
    check_orders_after_cutoff,
)
from baskfy_worker.tasks.swing_premarket import STAGE_GAPS, QuoteSource, run_swing_premarket
from baskfy_worker.tasks.swing_scan_now import (
    ScanNotRunnable,
    decide_session,
    run_scan_now,
    sweep_queued,
)
from baskfy_worker.tasks.swing_timing_probe import DONE_MARKER, probe_once
from baskfy_worker.telemetry import provider_retry_hooks
from baskfy_worker.window import DateWindow

log = logging.getLogger("baskfy_worker.tasks")

#: Earliest IST wall-clock at which a session's own data can exist. NSE closes at 15:30 and
#: publishes the bhavcopy afterwards; the schedule itself fires at 18:45 for that reason. Used to
#: tell "today, already closed" from "today, still ahead of us" — a distinction `is_trading_day`
#: cannot make and which redelivered tasks waking after midnight get wrong.
#:
#: Defined in `baskfy_worker.catch_up` since M84, because the sweep needs the same hour and a
#: constant with two definitions is a constant with two values. Re-exported here, where it has
#: always been imported from.
SESSION_DATA_READY_IST = catch_up.SESSION_DATA_READY_IST

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
        # 18:00 IS THE BHAVCOPY'S HOUR AND IT STAYS THAT WAY (M88 reverted, 8 Sep 2026).
        #
        # M88 moved this to 15:45 on the evidence that Kite returns the day's bars minutes
        # after the close. It does — for the ~3,000 liquid names it covers, not for the ~4,855
        # this universe holds. The remainder arrive in NSE's bhavcopy, so an early run writes a
        # partial day and the quality gate refuses it: 7 Sep ingested 3,056 bars at 15:37 and
        # was refused three times, where 4 Sep with the bhavcopy top-up ingested 4,454 and
        # published. `catch_up.SESSION_DATA_READY_IST` carries the numbers.
        cutoff = SESSION_DATA_READY_IST
        # AND NOT A DAY WE HAVE ALREADY PUBLISHED. Two scheduled entries now run this task for
        # the same date (15:50 and 18:45), and re-running a landed day is about two hours of
        # Kite calls to write the rows it already wrote. It also closes an older hole: on
        # 31 Aug 2026 three deploys redelivered the nightly and the copies re-ran a session
        # that was already done. An explicit `trade_date` still forces the work — that is the
        # reprocess path, and a human asking for a specific day means it.
        if run_already_published(day):
            return {
                "trade_date": day.isoformat(),
                "status": "skipped",
                "reason": f"{day.isoformat()} is already published; nothing to re-run",
            }
        if now_ist.date() == day and now_ist.time() < cutoff:
            return {
                "trade_date": day.isoformat(),
                "status": "skipped",
                "reason": (
                    f"the {day.isoformat()} session has not published yet "
                    f"(now {now_ist.time().strftime('%H:%M')} IST, data expected after "
                    f"{cutoff.strftime('%H:%M')})"
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


@shared_task(name="baskfy.pipeline.bhavcopy_ingest", acks_late=True)
def bhavcopy_ingest(trade_date: str | None = None) -> JsonObject:
    """The day's bars from NSE's own file, on a schedule, owing nothing to Kite (M84).

    WHY THIS IS ITS OWN JOB AND NOT ONLY A FALLBACK
    -----------------------------------------------
    `run_fetch_daily_bars` reaches for the bhavcopy when Kite produced *nothing*. That is a
    fallback, and a fallback is only as good as the failure that triggers it: a Kite session that
    dies halfway leaves a partial day, which is not zero, so the bhavcopy is never asked and the
    gate judges a half-day. Meanwhile the bhavcopy is the exchange's own end-of-day record, it
    needs no credential, it carries `turnover` and both circuit bands natively (docs/05 §12, §13),
    and it is one 200 KB file.

    So it runs on its own schedule at 18:15 — after NSE publishes, before the 18:45 chain — and
    the chain's Kite pass becomes a top-up over a day that has already landed rather than the only
    thing standing between the product and a stale session. Two independent sources, either
    sufficient, which is what "the desk must not depend on one login" means in practice.

    **Verified on the box, 4 Sep 2026.** `nsearchives.nseindia.com` answers the Phase-A host: 200
    and 203,909 bytes for 3 Sep, parsed to 3,635 rows. Only `www.nseindia.com` returns 403 there,
    and cookie priming ignores the status it gets, so the archive path is unaffected. An older
    note in `kite_session_cli.refresh_quietly` says the bhavcopy "does not exist" on this box; it
    is out of date, and this task is the standing evidence.

    Idempotent (house rule 7): the upsert rewrites the same rows to the same values.
    """
    day = dt.date.fromisoformat(trade_date) if trade_date else dt.datetime.now(tz=IST).date()

    # The same two guards the nightly carries, and for the same reasons: never ingest a day the
    # exchange was shut, and never call a session missing before it has had time to publish.
    if trade_date is None:
        if not run_in_session(lambda session: ops.is_trading_day(session, day)):
            return {
                "trade_date": day.isoformat(),
                "status": "skipped",
                "reason": "not a trading day",
            }
        now_ist = dt.datetime.now(tz=IST)
        if now_ist.date() == day and now_ist.time() < SESSION_DATA_READY_IST:
            return {
                "trade_date": day.isoformat(),
                "status": "skipped",
                "reason": f"the {day.isoformat()} bhavcopy is not published yet",
            }

    provider = build_nse_provider(get_provider_settings(), retry_hooks=provider_retry_hooks())
    report = asyncio.run(
        backfill_bars_from_bhavcopy(provider, DateWindow.single(day), progress_every=0)
    )
    return {
        "trade_date": day.isoformat(),
        "status": "succeeded" if report.succeeded else "failed",
        "bars_written": report.bars_written,
        "days_written": report.days_written,
        "missing_days": [d.isoformat() for d in report.missing_days] or None,
        "unmatched_symbol_count": len(report.unmatched_symbols) or None,
        "failures": report.failures or None,
    }


def run_already_published(day: dt.date) -> bool:
    """Has this trade date already produced a run with a ``data_version``?

    The product's own test for "fit to serve", and the same one `catch_up.landed_sessions` and
    M86's `_published` use. Failure to read answers ``False`` — a box that cannot tell should do
    the work rather than skip a day it might be missing.
    """

    async def _check(session: AsyncSession) -> bool:
        rows = await session.execute(
            select(PipelineRun.id)
            .where(PipelineRun.trade_date == day, PipelineRun.data_version.is_not(None))
            .limit(1)
        )
        return rows.first() is not None

    try:
        return bool(run_in_session(_check))
    except Exception:
        log.warning("could not tell whether %s is published; running the chain", day.isoformat())
        return False


def kite_session_usable() -> bool:
    """Is there a Kite token this box could fetch today's bars with, right now?

    Local and cheap — the stored token's own issue date against the IST calendar day, the same
    test `_connection_state` uses. No network call: this runs inside a Beat-triggered guard and
    "is Kite reachable" is a different, slower question that the chain itself will answer a
    moment later anyway.

    Any failure to read reads as *no session*, which selects the conservative 18:00 cutoff — a
    box that cannot tell should wait for the bhavcopy rather than run early and fail.
    """
    try:
        from baskfy_api.broker_oauth import token_store_for  # noqa: PLC0415 - optional at import

        store = token_store_for()
        if not store.exists():
            return False
        return not store.load().is_expired()
    except Exception:
        return False


def _published(chain: JsonObject) -> bool:
    """Did this chain run reach `publish`? `data_version` is the column that decides.

    The same test `catch_up.landed_sessions` uses, and for the same reason: only a run that
    passed the quality gate reaches `publish`, and only `publish` sets `data_version`. A run
    that failed, was abandoned, or was refused has none.
    """
    return chain.get("data_version") is not None


@shared_task(name="baskfy.pipeline.session_catch_up", acks_late=True)
def session_catch_up(
    lookback_days: int = catch_up.DEFAULT_LOOKBACK_DAYS,
    max_sessions: int = catch_up.DEFAULT_MAX_SESSIONS,
) -> JsonObject:
    """Run the chain for any session that never landed. Fired by a verified Kite token (M84).

    `baskfy_worker.catch_up` carries the reasoning and the definition of "landed". This is the
    hand that acts on it: oldest session first, in this process, one after another, bounded by
    ``max_sessions`` so a worker slot is never taken for an unbounded stretch. What it cannot
    reach is reported in ``remaining`` and picked up by the next trigger.

    **Two triggers, deliberately.** The morning Kite login publishes this the moment a real token
    is stored (`routers/brokers.py`), because that is when the system knows it can talk to the
    broker and it is before the market opens; and Beat publishes it at 06:45 so a missed session
    still heals on a morning nobody logs in. Both are idempotent — a published day is not in the
    list — so firing it twice costs one query.

    It runs the nightly **in process** rather than publishing one message per day: the chain takes
    about two hours, and two of them arriving on a worker with `--concurrency=2` would run the
    same instrument refresh and the same index membership concurrently. Sequential is the only
    ordering that is obviously correct, and this task's whole job is to be obviously correct at
    06:45 with nobody watching.
    """
    now_ist = dt.datetime.now(tz=IST)
    missing = run_in_session(
        lambda session: catch_up.unlanded_sessions(
            session, through=now_ist.date(), lookback_days=lookback_days, now=now_ist
        )
    )
    if not missing:
        return {
            "status": "nothing to do",
            "checked_through": now_ist.date().isoformat(),
            "lookback_days": lookback_days,
            "sessions": [],
        }

    ran: list[JsonObject] = []
    planned: list[JsonObject] = []
    for day in missing[:max_sessions]:
        log.warning("catch-up: %s never landed; running the chain for it", day.isoformat())
        # The nightly's own body, with an explicit date — which also bypasses its "is today
        # finished" guard, correctly: this list only ever holds sessions that are already over.
        chain = nightly_pipeline(day.isoformat())
        ran.append(chain)
        # AND THEN THE SWING PLAN FOR IT (M85, closing M84's own open item).
        #
        # The chain's twelfth step detects the day's setups; `baskfy.swing.eod` is what turns
        # them into a plan, and it has only ever existed as a 21:05 Beat entry. So a caught-up
        # session used to land every bar, every factor and every setup — and no plan, which is
        # the one artefact Maulik reads in the morning. It ran, the swing book stayed on the
        # last session that had a plan, and that is "the swing data is lagging" exactly.
        #
        # In process and immediately after its own chain, for the ordering `swing_eod_task`'s
        # own docstring requires: the plan is built from the day's candidates and the day's gate.
        # Idempotent per date, so a day whose 21:05 entry did fire is rewritten to itself.
        # Fail soft — a plan that cannot be built must not undo a session that landed.
        #
        # ONLY IF THE CHAIN ACTUALLY PUBLISHED (5 Sep 2026 — this was wrong as M85 wrote it).
        #
        # The evening ran unconditionally, and on 4-5 Sep that ran it on a day the quality gate
        # had REFUSED: TCC's uningested 1:5 split made the day's data wrong, the chain said so,
        # and the evening then built a swing plan on exactly that data and counted a LIVE
        # session against `first_live_sessions_left` — spending one of the five half-risk
        # sessions on numbers the pipeline had just rejected. Nothing downstream of a refused
        # gate should run, and `data_version` is the product's own test for "fit to serve".
        if not _published(chain):
            planned.append({"date": day.isoformat(), "skipped": "the chain did not publish"})
            continue
        try:
            planned.append(swing_eod_task(day.isoformat()))
        except Exception as exc:
            log.exception("catch-up: %s landed but its swing plan did not", day.isoformat())
            planned.append({"date": day.isoformat(), "error": type(exc).__name__})
    return {
        "status": "ran",
        "checked_through": now_ist.date().isoformat(),
        "lookback_days": lookback_days,
        "sessions": [day.isoformat() for day in missing],
        "ran": ran,
        "swing_plans": planned,
        "remaining": [day.isoformat() for day in missing[max_sessions:]],
    }


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
    """SW6: refresh the levels (08:50), scan the open for gaps and plan the morning (09:16).

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
        # INTERACTIVE: 09:16 is one minute after the open and the gap read is worthless late.
        quotes = build_kite_provider(
            get_provider_settings(), provider_retry_hooks(), lane=KiteLane.INTERACTIVE
        )

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


# --- SW15: "Scan now" (docs/swing/DECISIONS-SW SW15.1) -----------------------------------------


#: The two names the API and the sweep publish. `test_celery_config.py` holds the producer's
#: routing table and this worker's equal for both.
SWING_SCAN_NOW_TASK: Final = "baskfy.swing.scan_now"
SWING_SCAN_SWEEP_TASK: Final = "baskfy.swing.scan_sweep"
SWING_SCAN_AFTER_LOGIN_TASK: Final = "baskfy.swing.scan_after_login"


#: The API publishes the task inside the request whose transaction inserts the row, so the
#: worker can be handed the id a moment before the row is visible. A row that is not there yet
#: is retried a few seconds later; five tries covers a slow commit, not a row that never was.
SCAN_ROW_RETRY_SECONDS: Final = 2
SCAN_ROW_RETRIES: Final = 5


class _CurrentTaskMarker:
    """Give ``request_scan`` this task's id without publishing a second Celery message."""

    def __init__(self, task_id: str) -> None:
        self._task_id = task_id

    def send_task(self, name: str, args: Sequence[object]) -> object:
        if name != SWING_SCAN_NOW_TASK or len(args) != 1:
            raise ValueError(f"unexpected scan publication {name!r} {list(args)!r}")
        return self._task_id


async def request_login_scan(
    session: AsyncSession,
    *,
    user_id: int,
    market_now: dt.datetime,
    requested_at: dt.datetime,
    task_id: str,
) -> JsonObject:
    """Create one current-session scan row, or explain why login needs no new scan."""
    settings = get_api_settings()
    try:
        decision = await decide_session(session, market_now)
    except ScanNotRunnable as exc:
        return {"skipped": str(exc)}
    if not decision.provisional:
        return {"skipped": decision.reason, "session_date": decision.session_date.isoformat()}
    try:
        row = await request_scan(
            session,
            user_id=user_id,
            now=requested_at,
            min_interval=dt.timedelta(seconds=settings.swing_scan_min_interval_seconds),
            stale_after=dt.timedelta(seconds=settings.swing_scan_stale_after_seconds),
            source="broker-login",
            queue=_CurrentTaskMarker(task_id),
        )
    except Problem as exc:
        return {"skipped": exc.type.value, **exc.extra}
    return {"run_id": int(row.id)}


def _execute_swing_scan(run_id: int) -> JsonObject:
    """Run one already-committed scan row through the ordinary SW15 implementation."""
    deps = build_pipeline_dependencies()

    def quote_source() -> QuoteSource:
        # INTERACTIVE, said out loud (M85). A person pressed a button or has just finished a
        # broker login and is waiting for this; it must not queue behind the catch-up chain
        # running beside it on the default queue. `KiteLane` carries the arithmetic.
        return build_kite_provider(
            get_provider_settings(), provider_retry_hooks(), lane=KiteLane.INTERACTIVE
        )

    async def _run(session: AsyncSession) -> JsonObject:
        report = await run_scan_now(
            session,
            run_id=run_id,
            user_id=int(deps.swing_user_id or 0),
            index_slug=deps.swing_index_slug,
            execution_enabled=deps.swing_execution_enabled,
            quote_source=quote_source,
            now=dt.datetime.now(tz=IST).replace(tzinfo=None),
        )
        return report.as_detail()

    return run_in_session(_run)


@shared_task(name=SWING_SCAN_NOW_TASK, acks_late=True, queue=QUEUE_COMPUTE, bind=True)
def swing_scan_now_task(self: Task, run_id: int) -> JsonObject:
    """SW15: one press of "Scan now" — `run_scan_now` over the `sw_scan_run` row ``run_id``.

    The Kite provider is built **lazily**, and only on the provisional path: outside market
    hours the task re-detects the last published session and opens no Kite session at all. A
    Kite session that cannot be built (no token) is a `FAILED` row with the reason, not a
    crash — the body owns that (fail soft), so the task never retries a press. The one retry
    is for a row the publisher has not committed yet (`SCAN_ROW_RETRY_SECONDS`).
    """
    deps = build_pipeline_dependencies()
    if deps.swing_user_id is None:
        return {"run_id": run_id, "skipped": "no BASKFY_SOLE_USER_ID configured"}

    try:
        scan = _execute_swing_scan(int(run_id))
    except ScanNotRunnable as exc:
        raise self.retry(
            exc=exc, countdown=SCAN_ROW_RETRY_SECONDS, max_retries=SCAN_ROW_RETRIES
        ) from exc
    # The button rebuilds the plan too — pressing "Scan now" and getting setups you still cannot
    # act on is the same complaint in a smaller box. Same fail-soft order as the login path.
    try:
        plan = _rebuild_intraday_plan(int(deps.swing_user_id), scan)
    except Exception as exc:
        log.exception("scan %s landed but its intraday plan did not", run_id)
        plan = {"error": f"{type(exc).__name__}: {exc}"}
    return {**scan, "intraday_plan": plan}


@shared_task(
    name=SWING_SCAN_AFTER_LOGIN_TASK,
    acks_late=True,
    queue=QUEUE_COMPUTE,
    bind=True,
)
def swing_scan_after_login_task(self: Task, user_id: int) -> JsonObject:
    """Start today's provisional swing scan immediately after a verified broker login.

    Historical catch-up runs on ``default`` while this task runs on ``compute``. The two can
    therefore make progress in parallel, but every Kite read still queues on the same Redis
    departure clock. Outside an open trading session this task records nothing: the published
    data and missed-session catch-up remain authoritative.
    """
    deps = build_pipeline_dependencies()
    configured = deps.swing_user_id
    if configured is None:
        return {"skipped": "no BASKFY_SOLE_USER_ID configured"}
    if int(user_id) != int(configured):
        return {"skipped": "login user does not match BASKFY_SOLE_USER_ID"}

    requested_at = dt.datetime.now(tz=dt.UTC)
    market_now = dt.datetime.now(tz=IST).replace(tzinfo=None)
    task_id = str(getattr(self.request, "id", "") or f"broker-login-{requested_at.isoformat()}")

    async def _request(session: AsyncSession) -> JsonObject:
        return await request_login_scan(
            session,
            user_id=int(user_id),
            market_now=market_now,
            requested_at=requested_at,
            task_id=task_id,
        )

    request_result = run_in_session(_request)
    run_id = request_result.get("run_id")
    if not isinstance(run_id, int):
        return request_result
    scan = _execute_swing_scan(run_id)
    # AND THEN THE PLAN, WHICH IS THE HALF THAT WAS MISSING (4 Sep 2026).
    #
    # A scan that nothing acts on is a page nobody can trade from: on the box this afternoon the
    # 14:08 scan wrote thirteen provisional setups while the only executable line was a MORNING
    # plan built at 09:16 from the previous close. The rebuild is the evening's own sequence run
    # against the provisional session; `swing_intraday` carries the full reasoning.
    #
    # Fail soft, and in that order: the scan's rows are already committed and are worth having on
    # their own, so a plan that cannot be built must not lose them.
    plan: JsonObject
    try:
        plan = _rebuild_intraday_plan(int(user_id), scan)
    except Exception as exc:
        log.exception("the live scan landed but its intraday plan did not")
        plan = {"error": f"{type(exc).__name__}: {exc}"}
    return {**scan, "intraday_plan": plan}


def _rebuild_intraday_plan(user_id: int, scan: JsonObject) -> JsonObject:
    """Rebuild today's plan from the session the scan just measured.

    Only after a scan that actually detected on **today** — `provisional` is the scan's own word
    for "this session is still in progress". A scan that re-detected a published session has not
    changed what tomorrow's plan should be, and the evening owns that one.
    """
    if not scan.get("provisional"):
        return {"skipped": "the scan was not of a session in progress"}
    session_date = scan.get("session_date")
    if not isinstance(session_date, str):
        return {"skipped": "the scan recorded no session date"}
    deps = build_pipeline_dependencies()
    on = dt.date.fromisoformat(session_date)
    now = dt.datetime.now(tz=dt.UTC)

    async def _run(session: AsyncSession) -> JsonObject:
        report = await build_intraday_plan(
            session,
            user_id=user_id,
            on=on,
            now=now,
            execution_enabled=deps.swing_execution_enabled,
        )
        return report.as_detail()

    return run_in_session(_run)


@shared_task(name=SWING_SCAN_SWEEP_TASK)
def swing_scan_sweep_task() -> JsonObject:
    """SW15: publish every queued `sw_scan_run` row nobody has published (the desk's rows, and
    the API's when it had no broker). Beat, every minute; one indexed SELECT when idle."""
    deps = build_pipeline_dependencies()
    if deps.swing_user_id is None:
        return {"published": [], "skipped": "no BASKFY_SOLE_USER_ID configured"}

    def publish(run_id: int) -> str:
        result = swing_scan_now_task.apply_async(args=[run_id], queue=QUEUE_COMPUTE)
        return str(result.id)

    async def _run(session: AsyncSession) -> JsonObject:
        published = await sweep_queued(
            session, user_id=int(deps.swing_user_id or 0), publish=publish
        )
        return {"published": published}

    return run_in_session(_run)


# --- SW11: the S2 timing probe and the four alert checks (docs/swing/STANDING-ANSWERS A4, B8) --


#: The liquid names the probe quotes when the instrument table has them; any five with a Kite
#: token otherwise. Large-cap, always trading, so a pre-open print exists to observe.
PROBE_SYMBOLS: Final[tuple[str, ...]] = ("RELIANCE", "TCS", "HDFCBANK", "INFY", "SBIN")


class KiteProbeSource:
    """`swing_timing_probe.ProbeSource` over the Kite provider: its own `quotes` and one
    minute-candle read through the provider's throttled call — a read, like every other."""

    def __init__(self, provider: KiteProvider) -> None:
        self._provider = provider

    def quotes(self, symbols: Sequence[str]) -> list[QuoteRecord]:
        return self._provider.quotes(symbols)

    def minute_candles(
        self, token: int, start: dt.datetime, end: dt.datetime
    ) -> list[dict[str, object]]:
        # The provider has no public minute-candle read (that file is another leaf's); its
        # throttled, retried, translated call is the right seam and is reached by name here.
        return self._provider._call(
            lambda client: client.historical_data(token, start, end, "minute")
        )


async def _probe_names(session: AsyncSession) -> list[tuple[str, int]]:
    """``(symbol, kite_token)`` for the probe's names — the well-known five when the table has
    them, else any five instruments with a token."""
    from sqlalchemy import select  # noqa: PLC0415 - one query, local to the probe

    from baskfy_core.models import Instrument  # noqa: PLC0415

    rows = (
        await session.execute(
            select(Instrument.symbol, Instrument.kite_token)
            .where(Instrument.symbol.in_(PROBE_SYMBOLS), Instrument.kite_token.is_not(None))
            .order_by(Instrument.symbol)
        )
    ).all()
    if not rows:
        rows = (
            await session.execute(
                select(Instrument.symbol, Instrument.kite_token)
                .where(Instrument.kite_token.is_not(None))
                .order_by(Instrument.id)
                .limit(len(PROBE_SYMBOLS))
            )
        ).all()
    return [(str(symbol), int(token)) for symbol, token in rows]


@shared_task(name="baskfy.swing.timing_probe", acks_late=False, time_limit=25 * 60)
def swing_timing_probe_task() -> JsonObject:
    """SW11 / A4: one morning of Kite timing samples, then never again.

    With ``BASKFY_SWING_TIMING_PROBE`` false — the default — this returns at once and touches
    nothing. With it true and no ``.done`` marker in ``BASKFY_SWING_TIMING_PROBE_DIR``, it
    samples until ~09:21, writes ``S2-kite-timing.md`` there, and writes the marker after a good
    run. ``acks_late=False``: a probe re-delivered after the worker died would start at 09:2x
    and sample nothing useful.
    """
    settings = get_worker_settings()
    if not settings.swing_timing_probe:
        return {"skipped": "BASKFY_SWING_TIMING_PROBE is false"}
    out_dir = Path(settings.swing_timing_probe_dir)
    marker = out_dir / DONE_MARKER
    if marker.exists():
        return {"skipped": f"{marker} exists — the probe already had its good run"}
    names = run_in_session(_probe_names)
    if not names:
        return {"skipped": "no instrument with a Kite token to probe"}
    provider = build_kite_provider(get_provider_settings(), provider_retry_hooks())
    return probe_once(
        KiteProbeSource(provider),
        out_dir=out_dir,
        symbols=[symbol for symbol, _ in names],
        token=names[0][1],
        now=lambda: dt.datetime.now(tz=IST),
    )


def _swing_check(check: Callable[[AsyncSession], Awaitable[JsonObject]]) -> JsonObject:
    return run_in_session(check)


@shared_task(name="baskfy.swing.check_monitor_started")
def swing_check_monitor_started_task() -> JsonObject:
    """09:20: SWING_MONITOR_DID_NOT_START when the flag is on and no `monitor_ran` today."""
    settings = get_worker_settings()
    return _swing_check(
        lambda session: check_monitor_started(
            session, now=dt.datetime.now(tz=IST), monitor_enabled=settings.swing_monitor_enabled
        )
    )


@shared_task(name="baskfy.swing.check_orders_after_cutoff")
def swing_check_orders_after_cutoff_task() -> JsonObject:
    """10:50: SWING_ORDER_OPEN_AFTER_CUTOFF for a BUY line still SENT."""
    return _swing_check(
        lambda session: check_orders_after_cutoff(session, now=dt.datetime.now(tz=IST))
    )


@shared_task(name="baskfy.swing.check_gtt_at_1515")
def swing_check_gtt_at_1515_task() -> JsonObject:
    """15:20: SWING_GTT_MISSING_AT_1515 for shares open with no GTT after the desk's sweep."""
    return _swing_check(lambda session: check_gtt_at_1515(session, now=dt.datetime.now(tz=IST)))


@shared_task(name="baskfy.swing.check_detect_fresh")
def swing_check_detect_fresh_task() -> JsonObject:
    """21:30: SWING_DETECT_STALE when the published date has no market row."""
    return _swing_check(lambda session: check_detect_fresh(session, now=dt.datetime.now(tz=IST)))


@shared_task(name="baskfy.swing.catalyst", acks_late=True)
def swing_catalyst_task(session_date: str | None = None) -> JsonObject:
    """SW11B: the catalyst feed at 09:10 (`docs/swing/STANDING-ANSWERS.md` A3).

    Announcements and result dates for the WATCHING names and the day's EP candidates, through
    the NSE provider's cookie discipline and shared limiter; `sw_catalyst` upserted,
    `sw_watch.catalyst` filled where empty, `sw_watch.earnings_date` refreshed. Fail soft: a
    provider error is a note on a SUCCEEDED step, never a raise into the morning.
    """
    day = dt.date.fromisoformat(session_date) if session_date else dt.datetime.now(tz=IST).date()
    deps = build_pipeline_dependencies()
    if deps.swing_user_id is None:
        return {"date": day.isoformat(), "skipped": "no BASKFY_SOLE_USER_ID configured"}
    provider = build_nse_provider(get_provider_settings(), retry_hooks=provider_retry_hooks())

    async def _run(session: AsyncSession) -> JsonObject:
        outcome = StepOutcome()
        report = await run_swing_catalyst(
            session,
            outcome,
            day,
            user_id=int(deps.swing_user_id or 0),
            provider=provider,
            now=dt.datetime.now(tz=IST).replace(tzinfo=None),
        )
        return {"date": day.isoformat(), **report.as_detail()}

    return run_in_session(_run)


# --- SW18: the 08:45 Kite login nudge (docs/swing/DECISIONS-SW SW18.1) -------------------------

KITE_LOGIN_NUDGE_TASK: Final = "baskfy.kite.login_nudge"


def _login_url_for(user_id: int) -> Callable[[NudgeWindow], KiteLoginUrl]:
    """A fresh one-time state and login URL per window, from the API's own builder.

    The window is taken and ignored, deliberately: it exists so the *caller* cannot reuse the
    08:45 state at 09:05. A state lives 30 minutes, and the 09:05 link has to still work when
    somebody taps it at 09:20.
    """

    def build(window: NudgeWindow) -> KiteLoginUrl:
        return kite_login_url(
            api_key=os.environ.get("BASKFY_KITE_API_KEY", "").strip(), user_id=user_id
        )

    return build


@shared_task(name=KITE_LOGIN_NUDGE_TASK)
def kite_login_nudge_task(window: str = NudgeWindow.FIRST.value) -> JsonObject:
    """08:45 and 09:05 IST: one login link per morning, only when there is no usable token.

    Off unless ``BASKFY_KITE_LOGIN_NUDGE_ENABLED``; silent unless ``BASKFY_KITE_LOGIN_NUDGE_TO``
    names a recipient. The marker that makes it once-per-morning lives beside the token blob,
    which puts it on the state volume without a third setting to remember.
    """
    settings = get_worker_settings()
    if not settings.kite_login_nudge_enabled:
        return {"skipped": "BASKFY_KITE_LOGIN_NUDGE_ENABLED is false"}
    user_id = sole_user_id()
    if user_id is None:
        # The state the link carries is bound to a user id and the callback refuses one that
        # does not match the signed-in account, so there is no tenant to guess here.
        return {"skipped": "no BASKFY_SOLE_USER_ID configured"}
    if not os.environ.get("BASKFY_BROKER_OAUTH_STATE_PATH", "").strip():
        # A `state` minted here lives in THIS process's memory unless a shared file backs it,
        # and the API — a different container — is the one that has to consume it. Without the
        # file the link starts a real Kite login and dies at the callback with "Invalid or
        # expired OAuth state". Refusing is the same choice `routers/brokers.py` makes for a
        # login it cannot finish (leaf 1.1.4): a wasted tap at 09:05 is worse than silence,
        # and the /brokers page still works.
        return {
            "skipped": (
                "BASKFY_BROKER_OAUTH_STATE_PATH is not set, so a state minted by the worker "
                "could not be read by the API's callback and the login would fail at the end"
            )
        }
    chosen = NudgeWindow(window)
    token_path = token_store_path()
    store = AccessTokenStore(token_path, token_encryption_key())

    async def _run(session: AsyncSession) -> JsonObject:
        outcome = StepOutcome()
        report = await run_login_nudge(
            session,
            outcome,
            now=dt.datetime.now(tz=IST),
            token_store=store,
            login_url_for=_login_url_for(user_id),
            to=settings.kite_login_nudge_to.strip(),
            state_dir=token_path.parent,
            window=chosen,
        )
        return report.as_detail()

    return run_in_session(_run)
