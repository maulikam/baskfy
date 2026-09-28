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
import functools
import json
import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Final

from celery import Task, shared_task
from redis import Redis as SyncRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import overlap as overlap_service
from baskfy_api import overlap_scan
from baskfy_api.broker_oauth import (
    KiteLoginUrl,
    kite_login_url,
    token_encryption_key,
    token_store_path,
)
from baskfy_api.broker_trades import capture_kite_trades
from baskfy_api.curated_seed import resolve_sole_user_id
from baskfy_api.problems import Problem
from baskfy_api.screener import (
    WARM_CACHE_SCREEN_LIMIT,
    ScreenCache,
    WarmCacheResult,
    warm_screen_cache,
)
from baskfy_api.settings import get_settings as get_api_settings
from baskfy_api.swing_health import is_session_day
from baskfy_api.swing_scan import request_scan
from baskfy_core.models import PipelineRun
from baskfy_core.models.base import JsonObject
from baskfy_core.options.config import OptionsConfig, Sleeve
from baskfy_providers.errors import TransientProviderError
from baskfy_providers.factory import (
    KiteFamily,
    KiteLane,
    build_kite_family_provider,
    build_kite_provider,
    build_nse_provider,
)
from baskfy_providers.kite import KiteProvider
from baskfy_providers.records import QuoteRecord
from baskfy_providers.settings import get_provider_settings
from baskfy_providers.tokens import AccessTokenStore
from baskfy_worker import catch_up, eq_bars, kite_session_cli, ops
from baskfy_worker.alerts import Alert, AlertName, Severity, dispatch
from baskfy_worker.bhavcopy_backfill import backfill_bars_from_bhavcopy
from baskfy_worker.celery_app import IST, QUEUE_COMPUTE, QUEUES
from baskfy_worker.db import run_checkpointed, run_in_session, session_scope
from baskfy_worker.fno import index_daily as fno_index_daily
from baskfy_worker.fno.alerts import RedisMarkers as FnoRedisMarkers
from baskfy_worker.fno.alerts import bhavcopy_missing_alert as fno_bhavcopy_missing_alert
from baskfy_worker.fno.alerts import run_fno_alerts as fno_run_alerts
from baskfy_worker.fno.nightly import run_night as fno_run_night
from baskfy_worker.fno.retest import run_retest as fno_run_retest
from baskfy_worker.fno.scan import run_scan as fno_run_scan
from baskfy_worker.fno.scan import scan_users as fno_scan_users
from baskfy_worker.fno.spreads import in_session as fno_in_session
from baskfy_worker.fno.spreads import run_spread_sample
from baskfy_worker.fno.weekly import build_weekly as fno_build_weekly
from baskfy_worker.options import index_bars as options_index_bars
from baskfy_worker.options import options_ceilings
from baskfy_worker.options.backtest_run import TIERS as OPTIONS_TIERS
from baskfy_worker.options.backtest_run import git_sha, run_backtest
from baskfy_worker.options.checks import Switches, check_alert, due_checks, run_checks
from baskfy_worker.options.collector import collect_gate, collect_minute, in_session
from baskfy_worker.options.master import EmptyMaster, master_alert, refresh_master_all
from baskfy_worker.options.plan import kite_margin_reader, plan_gate_free, plan_o1_minute
from baskfy_worker.options.plan_o2 import plan_o2_gate_free, plan_o2_minute
from baskfy_worker.options.plan_o3 import plan_o3_gate_free, plan_o3_minute
from baskfy_worker.options.reads import build_options_kite
from baskfy_worker.options.scan import scan_gate_free, scan_minute_for
from baskfy_worker.options.weekly import week_rows, weekly_alert
from baskfy_worker.orchestrator import PipelineOutcome, run_nightly_pipeline
from baskfy_worker.providers import build_cache, build_pipeline_dependencies, sole_user_id
from baskfy_worker.settings import get_worker_settings
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks import vbt_ops
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
from baskfy_worker.tasks.live_scan import run_twt_live, run_vbt_live
from baskfy_worker.tasks.portfolio_nav_job import run_portfolio_nav
from baskfy_worker.tasks.published_session import last_published_session
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
from baskfy_worker.tasks.twt import detect_session as twt_detect_session
from baskfy_worker.tasks.twt_evening import SOURCE_MORNING as TWT_SOURCE_MORNING
from baskfy_worker.tasks.twt_evening import (
    last_detected_session as twt_last_detected_session,
)
from baskfy_worker.tasks.twt_evening import run_twt_evening
from baskfy_worker.tasks.twt_scan import run_twt_scan
from baskfy_worker.tasks.twt_scan import unpublished_runs as twt_unpublished_scan_runs
from baskfy_worker.tasks.vbt import published_signal_count, run_detect_vbt
from baskfy_worker.tasks.vbt_backtest import DEFAULT_START as VBT_BACKTEST_START
from baskfy_worker.tasks.vbt_backtest import run_vbt_backtest
from baskfy_worker.tasks.vbt_evening import (
    SOURCE_MORNING,
    last_detected_session,
    run_vbt_evening,
)
from baskfy_worker.tasks.vbt_rescan import run_vbt_rescan, unpublished_runs
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


async def _probe_and_demote_derived(session: AsyncSession, day: dt.date) -> bool:
    """Publication probe + AF 3.11 demotion for a derived holiday after one miss."""
    from baskfy_providers.publication import bhavcopy_publication_check  # noqa: PLC0415

    probe = bhavcopy_publication_check(build_pipeline_dependencies().provider)
    published = bool(probe is not None and await probe(day))
    return await catch_up.demote_derived_after_missing_bhavcopy(session, day, published=published)


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
    demoted: list[str] = []
    for day in missing[:max_sessions]:
        log.warning("catch-up: %s never landed; running the chain for it", day.isoformat())
        # The nightly's own body, with an explicit date — which also bypasses its "is today
        # finished" guard, correctly: this list only ever holds sessions that are already over.
        chain = nightly_pipeline(day.isoformat())
        ran.append(chain)
        if not _published(chain):
            # AF 3.11: a derived calendar guess that yields no bhavcopy must not be re-proposed
            # every sweep. Probe once; demote on a clear "nothing published".
            was_demoted = run_in_session(partial(_probe_and_demote_derived, day=day))
            if was_demoted:
                demoted.append(day.isoformat())
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
        "demoted_derived_holidays": demoted,
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


@shared_task(name="baskfy.pipeline.capture_kite_trades")
def capture_kite_trades_task() -> JsonObject:
    """Keep today's Zerodha executions before Kite flushes them tonight (NEEDS-MAULIK §32).

    Kite's ``/trades`` takes no date and is emptied nightly, so a day not read by the evening is
    gone from the API for good; only a Console export can recover it. Scheduled after the close,
    silent on a day that is not a session, and idempotent — a second run the same day inserts
    nothing (``broker_trade``'s unique trade id). A read-only call: no order is touched.
    """

    async def capture(session: AsyncSession) -> JsonObject:
        today = dt.datetime.now(tz=IST).date()
        if not await is_session_day(session, today):
            return {"captured": False, "reason": f"{today} is not an NSE session"}
        report = await capture_kite_trades(session, user_id=await resolve_sole_user_id(session))
        store = report.store
        return {
            "captured": report.captured,
            "reason": report.reason,
            "inserted": store.inserted if store else 0,
            "dated_holdings": report.history.dated if report.history else 0,
        }

    return run_in_session(capture)


@shared_task(name="baskfy.ops.check_kite_token")
def check_kite_token_task() -> JsonObject:
    """docs/09 calls Kite token expiry "the #1 pipeline failure" — so warn before it bites.

    Hourly. The check is arithmetic over the stored issue time and makes no network call, so
    running it often costs nothing and the warning lands with hours to spare.

    Hourly evaluation is not hourly email (14 Sep 2026, an NSE holiday that sent 24): it raises
    nothing on a day that is not a session, and delivers at most once per stored token per IST
    day — ``ops.run_kite_token_check`` carries the rule and DECISIONS-MERGE carries why.
    """

    async def calendar(day: dt.date) -> bool:
        async with session_scope() as session:
            return await is_session_day(session, day)

    async def check() -> JsonObject:
        cache = build_cache()
        try:
            return await ops.run_kite_token_check(calendar, cache)
        finally:
            if cache is not None:
                await cache.aclose()

    return asyncio.run(check())


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


@shared_task(name="baskfy.vbt.detect", acks_late=True)
def vbt_detect_task(trade_date: str | None = None) -> JsonObject:
    """VB4: detect the session's volume-breakout signals and write its breadth row.

    Idempotent per ``(user_id, date)``: re-running a date overwrites its own rows and moves no
    counter (house rule 7). Defaults to today in IST when Beat fires without an argument.

    **This is the 21:00 retry as well as the CLI's entry point.** The nightly chain already runs
    the same detector as its thirteenth step, where it cannot fail the night; if the chain has not
    published by the time Beat fires — a slow bhavcopy, a provider stall — this is what writes the
    session anyway. It asks first: a date that already has rows is answered without work, because
    a retry that re-detects a session the chain already wrote would burn a few minutes of Polars
    to arrive at the same rows.
    """
    day = dt.date.fromisoformat(trade_date) if trade_date else dt.datetime.now(tz=IST).date()
    deps = build_pipeline_dependencies()
    if not deps.vbt_nightly_enabled:
        return {"date": day.isoformat(), "skipped": "BASKFY_VBT_NIGHTLY_ENABLED is false"}
    if deps.vbt_user_id is None:
        return {"date": day.isoformat(), "skipped": "no BASKFY_SOLE_USER_ID configured"}

    async def _run(session: AsyncSession) -> JsonObject:
        user_id = int(deps.vbt_user_id or 0)
        already = await published_signal_count(session, user_id, day)
        if already:
            return {"date": day.isoformat(), "skipped": "already detected", "rows": already}
        outcome = StepOutcome()
        signals = await run_detect_vbt(session, outcome, day, user_id=user_id)
        return {"date": day.isoformat(), "signals": signals, "detail": outcome.detail}

    return run_in_session(_run)


@shared_task(name="baskfy.options.refresh_master", acks_late=True)
def options_refresh_master_task(as_of: str | None = None) -> JsonObject:
    """OP2, widened by FO2: tonight's NFO options master into ``op_contract`` and ``op_expiry``.

    One read-only Kite call (``instruments("NFO")``) on the bulk lane, then an idempotent upsert
    for **every** F&O underlying (``docs/fno/06`` FO2; it was NIFTY only). Contracts are never
    deleted, each underlying's calendar is rebuilt from the master and never from a weekday rule
    (``docs/options/04`` §1.1). The O-sleeves still read NIFTY only: every one of their reads
    filters on the underlying. A lot-size change, a kind change or a withdrawn future expiry on
    NIFTY raises ``OPTIONS_MASTER_CHANGED``; the other underlyings' changes are in the result. No
    Kite session → skipped, not failed. A dump without NIFTY is refused rather than applied.
    """
    day = dt.date.fromisoformat(as_of) if as_of else dt.datetime.now(tz=IST).date()
    if not kite_session_usable():
        return {"date": day.isoformat(), "skipped": "no usable Kite session"}
    provider = build_kite_provider(
        get_provider_settings(), provider_retry_hooks(), lane=KiteLane.BULK
    )
    records = provider.fno_option_contracts()

    async def _run(session: AsyncSession) -> JsonObject:
        try:
            reports = await refresh_master_all(session, records, as_of=day)
        except EmptyMaster as exc:
            return {"date": day.isoformat(), "refused": str(exc)}
        nifty = reports.nifty
        alert = master_alert(nifty)
        out = {**nifty.as_dict(), **reports.summary()}
        if alert is not None:
            out["alert"] = await dispatch(alert)
        return out

    return run_in_session(_run)


FNO_INGEST_TASK: Final = "baskfy.fno.ingest_bhavcopy"


@shared_task(name=FNO_INGEST_TASK, acks_late=True)
def fno_ingest_bhavcopy_task(trade_date: str | None = None, at: str | None = None) -> JsonObject:
    """FO2: one session's F&O bhavcopy into ``fo_contract_daily``, and the next session's ban list.

    Beat fires it at 18:30 and hourly to 23:30 on weekdays (``docs/fno/04`` §4). Refuses, before
    any NSE request, when ``BASKFY_FNO_SCAN_ENABLED`` is false (the default) or the day is not a
    session. A day already ``INGESTED`` with its ban list stored is answered from the database, so
    the later retries cost nothing. A day with no file at the 23:30 attempt is ``MISSING`` in
    ``fo_ingest_day``, never interpolated. Reads NSE only; moves no money.
    """
    now = _options_now(at)
    day = dt.date.fromisoformat(trade_date) if trade_date else now.date()
    if not get_worker_settings().fno_scan_enabled:
        return {"trade_date": day.isoformat(), "skipped": "BASKFY_FNO_SCAN_ENABLED is false"}
    provider = build_nse_provider(get_provider_settings(), retry_hooks=provider_retry_hooks())

    async def _run(session: AsyncSession) -> JsonObject:
        out = await fno_run_night(session, provider, day, now_ist=now)
        # FO12: the 23:30 attempt that records MISSING raises FNO_BHAVCOPY_MISSING, here, from
        # the result. Every earlier attempt is PENDING and says nothing; the retry is the schedule.
        missing = fno_bhavcopy_missing_alert(out)
        if missing is not None:
            out["alert"] = await dispatch(missing)
        return out

    return run_in_session(_run)


FNO_INDEX_DAILY_TASK: Final = "baskfy.fno.index_daily"


@shared_task(name=FNO_INDEX_DAILY_TASK, acks_late=True)
def fno_index_daily_task(at: str | None = None, start: str | None = None) -> JsonObject:
    """F3 (``docs/fno/03`` §9): the two indices' daily OHLC into ``fo_index_daily`` from Kite's
    history — the evening extension, or a backfill from ``start``.

    Behind ``BASKFY_FNO_SCAN_ENABLED`` like the bhavcopy ingest; skipped without a usable Kite
    session (the ``historical`` family needs one). One call per index per 2,000 days; reads
    only, moves no money. Idempotent: a stored day is overwritten with Kite's reading.
    """
    now = _options_now(at)
    if not get_worker_settings().fno_scan_enabled:
        return {"date": now.date().isoformat(), "skipped": "BASKFY_FNO_SCAN_ENABLED is false"}
    if not kite_session_usable():
        return {"date": now.date().isoformat(), "skipped": "no usable Kite session"}

    async def _run(session: AsyncSession) -> JsonObject:
        # The historical family's adapter (``KiteProvider.daily_bars``), on that family's clock.
        kite = build_kite_family_provider(
            get_provider_settings(), KiteFamily.HISTORICAL, provider_retry_hooks()
        )
        if start:
            report = await fno_index_daily.backfill(
                session, kite, dt.date.fromisoformat(start), now.date()
            )
        else:
            report = await fno_index_daily.extend(session, kite, now.date())
        return {"date": now.date().isoformat(), **report.as_dict()}

    return run_in_session(_run)


FNO_SCAN_TASK: Final = "baskfy.fno.scan"


@shared_task(name=FNO_SCAN_TASK, acks_late=True)
def fno_scan_task(trade_date: str | None = None) -> JsonObject:
    """FO4: ``fo_scan`` for the session after ``trade_date``, from the database alone.

    The nightly chains the same scan after each ingested night (``run_night``), so this task has
    no Beat entry: it is the re-run by hand. Refuses when ``BASKFY_FNO_SCAN_ENABLED`` is false
    (the default). Reads no network; moves no money.
    """
    day = dt.date.fromisoformat(trade_date) if trade_date else dt.datetime.now(tz=IST).date()
    if not get_worker_settings().fno_scan_enabled:
        return {"trade_date": day.isoformat(), "skipped": "BASKFY_FNO_SCAN_ENABLED is false"}

    async def _run(session: AsyncSession) -> JsonObject:
        return await fno_run_scan(session, day)

    return run_in_session(_run)


FNO_WEEKLY_TASK: Final = "baskfy.fno.weekly"


@shared_task(name=FNO_WEEKLY_TASK, acks_late=False)
def fno_weekly_task(at: str | None = None) -> JsonObject:
    """FO10: the week's FO journal per ``(sleeve, simulated)``, the month per book and the pauses
    per mode, as one ``FNO_WEEKLY`` alert per FO tenant. Read-only. Refused before any database
    session unless ``BASKFY_FNO_MONITOR_ENABLED`` is true."""
    now = _options_now(at)
    if not get_worker_settings().fno_monitor_enabled:
        return {"at": now.isoformat(), "skipped": "BASKFY_FNO_MONITOR_ENABLED is false"}

    async def _run(session: AsyncSession) -> JsonObject:
        today = now.astimezone(IST).date()
        sent: dict[str, object] = {}
        for user_id in await fno_scan_users(session):
            alert = await fno_build_weekly(session, user_id, today)
            sent[str(user_id)] = {"summary": alert.summary, "sent": await dispatch(alert)}
        return {"at": now.isoformat(), "users": sent}

    return run_in_session(_run)


FNO_ALERTS_TASK: Final = "baskfy.fno.alerts"


@shared_task(name=FNO_ALERTS_TASK, acks_late=False)
def fno_alerts_task(at: str | None = None) -> JsonObject:
    """FO12: today's FO events — a plan issued, an exit done, a hard exit tomorrow (from 18:00), a
    ``LATE_EXIT`` — raised once each (a Redis marker per event) from what the desk wrote.
    Read-only; reaches no broker. Refused before any database session unless
    ``BASKFY_FNO_MONITOR_ENABLED`` is true (with the monitor off the desk writes no plan)."""
    now = _options_now(at)
    if not get_worker_settings().fno_monitor_enabled:
        return {"at": now.isoformat(), "skipped": "BASKFY_FNO_MONITOR_ENABLED is false"}

    async def _run(session: AsyncSession) -> JsonObject:
        cache = build_cache()
        try:
            markers = None if cache is None else FnoRedisMarkers(cache)
            return await fno_run_alerts(session, await fno_scan_users(session), markers, now)
        finally:
            if cache is not None:
                await cache.aclose()

    return run_in_session(_run)


FNO_RETEST_TASK: Final = "baskfy.fno.retest"


@shared_task(name=FNO_RETEST_TASK, acks_late=True)
def fno_retest_task(families: list[str] | None = None, force: bool = False) -> JsonObject:
    """FO9: every ``RESEARCH.md`` family over ``fo_contract_daily`` into ``fo_backtest_run``.

    Beat fires it on the first Saturday of January, April, July and October (``docs/fno/04``
    §6); ``run_retest`` refuses a second run in one month unless ``force``. Behind
    ``BASKFY_FNO_SCAN_ENABLED`` like the rest of the data layer. Reads no network; moves no
    money; a positive family becomes a decision for Maulik, never a flag.
    """
    today = dt.datetime.now(tz=IST).date()
    if not get_worker_settings().fno_scan_enabled:
        return {"skipped": "BASKFY_FNO_SCAN_ENABLED is false"}

    async def _run(session: AsyncSession) -> JsonObject:
        return await fno_run_retest(session, today=today, force=force, families=families)

    return run_in_session(_run)


FNO_SPREAD_TASK: Final = "baskfy.fno.spread_sample"


@shared_task(name=FNO_SPREAD_TASK, acks_late=False)
def fno_spread_sample_task(at: str | None = None) -> JsonObject:
    """FO3: the 15:00 spread sample into ``fo_spread_sample``, and the forward results calendar.

    Refuses before any database session or provider when ``BASKFY_FNO_SCAN_ENABLED`` is false
    (the default) or the moment is outside 09:15-15:30 IST; then on a day the NSE calendar names
    a holiday. The Kite reader is built only when a Kite session exists — without one, no Kite
    call is made and only the NSE results calendar is read (``docs/fno/06`` FO3). One ``quote()``
    on the box's shared 1 req/s quote clock. ``acks_late=False``: a redelivered sample would stamp
    a later book as the 15:00 one. Moves no money.
    """
    now = _options_now(at)
    if not get_worker_settings().fno_scan_enabled:
        return {"at": now.isoformat(), "skipped": "BASKFY_FNO_SCAN_ENABLED is false"}
    if not fno_in_session(now):
        return {"at": now.isoformat(), "skipped": "outside 09:15-15:30 IST"}

    async def _run(session: AsyncSession) -> JsonObject:
        if not await ops.is_trading_day(session, now.astimezone(IST).date()):
            return {"at": now.isoformat(), "skipped": "not an NSE trading day"}
        report = await run_spread_sample(
            session,
            now,
            kite_ok=kite_session_usable,
            quotes=lambda: build_options_kite(retry_hooks=provider_retry_hooks()).quotes,
            results=build_nse_provider(get_provider_settings(), retry_hooks=provider_retry_hooks()),
        )
        return report.as_dict()

    return run_in_session(_run)


def _options_idle_reason(*, enabled: bool) -> str:
    return "BASKFY_OPTIONS_COLLECT_ENABLED is false" if not enabled else "outside 09:15-15:30 IST"


def _options_now(at: str | None) -> dt.datetime:
    """The IST moment an options task acts for: ``at`` (ISO) when given, else now."""
    if at:
        parsed = dt.datetime.fromisoformat(at)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=IST)
    return dt.datetime.now(tz=IST)


@shared_task(name="baskfy.options.collect_chain", acks_late=False)
def options_collect_chain_task(at: str | None = None) -> JsonObject:
    """OP3: one minute of the NIFTY chain into ``op_chain_snapshot`` (``docs/options/03`` §5).

    Beat fires it every minute 09:00-15:59 on weekdays; :func:`collect_gate` refuses — before any
    Kite call — when ``BASKFY_OPTIONS_COLLECT_ENABLED`` is false (the default), outside
    09:15-15:30, on a day the NSE calendar names a holiday, or with no usable Kite session. One
    ``quote()`` call on the box's shared 1 req/s quote clock. ``acks_late=False``: a minute that
    fails is gone, and redelivering it a minute later would stamp an old minute with new prices.
    Moves no money.
    """
    now = _options_now(at)
    enabled = get_worker_settings().options_collect_enabled
    if not enabled or not in_session(now):
        # The two free refusals, before a database connection: Beat fires this 420 times a day.
        return {"at": now.isoformat(), "skipped": _options_idle_reason(enabled=enabled)}

    async def _run(session: AsyncSession) -> JsonObject:
        refused = await collect_gate(
            now,
            enabled=enabled,
            kite_ok=kite_session_usable,
            trading_day=partial(is_session_day, session),
        )
        if refused is not None:
            return {"at": now.isoformat(), "skipped": refused}
        kite = build_options_kite(retry_hooks=provider_retry_hooks())
        report = await collect_minute(session, kite.quotes, now)
        return report.as_dict()

    return run_in_session(_run)


@shared_task(name="baskfy.options.index_bars", acks_late=False)
def options_index_bars_task(at: str | None = None) -> JsonObject:
    """OP3: NIFTY 50 and INDIA VIX closed minute bars since the last stored one (``03`` §4).

    Same Beat slot and the same gate as the collector — it is half of the collector's load and
    its ATM hint (DECISIONS-OP OP3.3). Two ``historical_data`` calls on the box's shared 3 req/s
    historical clock.
    """
    now = _options_now(at)
    enabled = get_worker_settings().options_collect_enabled
    if not enabled or not in_session(now):
        # The two free refusals, before a database connection: Beat fires this 420 times a day.
        return {"at": now.isoformat(), "skipped": _options_idle_reason(enabled=enabled)}

    async def _run(session: AsyncSession) -> JsonObject:
        refused = await collect_gate(
            now,
            enabled=enabled,
            kite_ok=kite_session_usable,
            trading_day=partial(is_session_day, session),
        )
        if refused is not None:
            return {"at": now.isoformat(), "skipped": refused}
        kite = build_options_kite(retry_hooks=provider_retry_hooks())
        report = await options_index_bars.pull_intraday(session, kite.bars, now)
        return {"at": now.isoformat(), **report.as_dict()}

    return run_in_session(_run)


@shared_task(name="baskfy.eq_bars.session", acks_late=True)
def eq_bars_session_task(trade_date: str | None = None) -> JsonObject:
    """LV5: after the close, the session's one-minute bars for every liquid name.

    Behind ``BASKFY_EQ_BARS_ENABLED`` (default on — read-only market data); skipped on a holiday
    or with no Kite session. The universe is the swing book's ``liquid_universe`` as of the last
    **published** session (today is not published at 15:45). One ``historical_data`` call per
    name on the bulk lane, ~570 names, about three minutes; committed every 25 names so an
    interruption keeps the evening. Idempotent: the upsert overwrites a minute with Kite's final
    reading (house rule 7).
    """
    now = dt.datetime.now(tz=IST)
    day = dt.date.fromisoformat(trade_date) if trade_date else now.date()
    if not get_worker_settings().eq_bars_enabled:
        return {"date": day.isoformat(), "skipped": "BASKFY_EQ_BARS_ENABLED is false"}
    if not kite_session_usable():
        return {"date": day.isoformat(), "skipped": "no usable Kite session"}
    user_id = sole_user_id()
    if user_id is None:
        return {"date": day.isoformat(), "skipped": "BASKFY_SOLE_USER_ID is not set"}

    async def _run(session: AsyncSession) -> JsonObject:
        if not await is_session_day(session, day):
            return {"date": day.isoformat(), "skipped": "not an NSE trading day"}
        as_of = await last_published_session(session, on_or_before=day - dt.timedelta(days=1))
        if as_of is None:
            return {"date": day.isoformat(), "skipped": "nothing published yet"}
        names = await eq_bars.liquid_names(session, as_of=as_of, user_id=user_id)
        if not names:
            return {
                "date": day.isoformat(),
                "as_of": as_of.isoformat(),
                "skipped": "no liquid names",
            }
        kite = build_options_kite(retry_hooks=provider_retry_hooks(), bars_lane=KiteLane.BULK)
        report = await eq_bars.reconcile_session(
            session, kite.bars, day, now=now, names=names, checkpoint=session.commit
        )
        return {"date": day.isoformat(), "as_of": as_of.isoformat(), **report.as_dict()}

    return run_checkpointed(_run)


@shared_task(name="baskfy.options.index_bars_eod", acks_late=True)
def options_index_bars_eod_task(trade_date: str | None = None) -> JsonObject:
    """OP3: after the close, the whole session's index bars again — the reconcile (``03`` §4).

    Behind the same flag (it is the intraday task's correction); skipped on a holiday or with no
    Kite session. Two calls. Idempotent: the upsert overwrites a minute with Kite's final reading.
    """
    now = dt.datetime.now(tz=IST)
    day = dt.date.fromisoformat(trade_date) if trade_date else now.date()
    if not get_worker_settings().options_collect_enabled:
        return {"date": day.isoformat(), "skipped": "BASKFY_OPTIONS_COLLECT_ENABLED is false"}
    if not kite_session_usable():
        return {"date": day.isoformat(), "skipped": "no usable Kite session"}

    async def _run(session: AsyncSession) -> JsonObject:
        if not await is_session_day(session, day):
            return {"date": day.isoformat(), "skipped": "not an NSE trading day"}
        kite = build_options_kite(retry_hooks=provider_retry_hooks())
        report = await options_index_bars.reconcile_day(session, kite.bars, day, now)
        return {"date": day.isoformat(), **report.as_dict()}

    return run_in_session(_run)


@shared_task(name="baskfy.options.scan", acks_late=False)
def options_scan_task(at: str | None = None) -> JsonObject:
    """OP4: each sleeve's scan state and candidates for this minute into ``op_scan``.

    Reads only the database — the collector's snapshot, the index bars, the master, the sole
    tenant's config and sessions — and makes **no Kite call** (PACK.11). Beat sends it every minute
    09:00-15:59 on weekdays, 20 s after the collector, with ``expires=55``. Refused before a
    database session unless ``BASKFY_OPTIONS_SCAN_ENABLED`` **and**
    ``BASKFY_OPTIONS_COLLECT_ENABLED`` are true (both default false), the clock is inside
    09:15-15:30 and a sole tenant is configured; then refused on an NSE holiday. Idempotent per
    minute (an upsert on ``(user_id, sleeve, ts)``). ``acks_late=False``, like the collector: a
    minute redelivered later would stamp an old minute with a later snapshot. Moves no money;
    has no order path.
    """
    now = _options_now(at)
    settings = get_worker_settings()
    user_id = sole_user_id()
    refused = scan_gate_free(
        now,
        scan_enabled=settings.options_scan_enabled,
        collect_enabled=settings.options_collect_enabled,
        user_id=user_id,
    )
    if refused is not None or user_id is None:
        return {"at": now.isoformat(), "skipped": refused or "no BASKFY_SOLE_USER_ID configured"}
    tenant = user_id

    async def _run(session: AsyncSession) -> JsonObject:
        if not await is_session_day(session, now.astimezone(IST).date()):
            return {"at": now.isoformat(), "skipped": "not an NSE trading day"}
        report = await scan_minute_for(session, tenant, now, trading_day=True)
        return report.as_dict()

    return run_in_session(_run)


@shared_task(name="baskfy.options.plan_o1", acks_late=False)
def options_plan_o1_task(at: str | None = None) -> JsonObject:
    """OP6: lapse expired O1 plans, then decide O1-M and O1-W for today — once each.

    Refused before any database session unless ``BASKFY_OPTIONS_MONITOR_ENABLED`` and
    ``BASKFY_OPTIONS_COLLECT_ENABLED`` are true (both default false), inside 10:00-10:16 and with a
    sole tenant; then on an NSE holiday. The only Kite read is the margin **calculator** for a
    plan's two baskets, and only with a usable session (otherwise the plan carries
    ``MARGIN_UNKNOWN``). ``OPTIONS_PLAN`` is sent once per sleeve per day. No order path.
    """
    now = _options_now(at)
    settings = get_worker_settings()
    user_id = sole_user_id()
    refused = plan_gate_free(
        now,
        monitor_enabled=settings.options_monitor_enabled,
        collect_enabled=settings.options_collect_enabled,
        user_id=user_id,
    )
    if refused is not None or user_id is None:
        return {"at": now.isoformat(), "skipped": refused or "no BASKFY_SOLE_USER_ID configured"}
    tenant = user_id
    margin = kite_margin_reader(build_options_kite().general) if kite_session_usable() else None

    async def _run(session: AsyncSession) -> JsonObject:
        if not await is_session_day(session, now.astimezone(IST).date()):
            return {"at": now.isoformat(), "skipped": "not an NSE trading day"}
        report, alerts = await plan_o1_minute(session, tenant, now, trading_day=True, margin=margin)
        await session.flush()
        report["alerts"] = [await dispatch(alert) for alert in alerts]
        return report

    return run_in_session(_run)


@shared_task(name="baskfy.options.plan_o2", acks_late=False)
def options_plan_o2_task(at: str | None = None) -> JsonObject:
    """OP7: lapse expired plans, then decide today's O2 session — once.

    Refused before any database session unless ``BASKFY_OPTIONS_MONITOR_ENABLED`` and
    ``BASKFY_OPTIONS_COLLECT_ENABLED`` are true (both default false), inside 09:30-13:34 and with a
    sole tenant; then on an NSE holiday. **No Kite call at all**: a long option's ceiling is the
    premium cap, not a margin the broker computes (OP7.3). ``OPTIONS_PLAN`` is sent once per day.
    No order path.
    """
    now = _options_now(at)
    settings = get_worker_settings()
    user_id = sole_user_id()
    refused = plan_o2_gate_free(
        now,
        monitor_enabled=settings.options_monitor_enabled,
        collect_enabled=settings.options_collect_enabled,
        user_id=user_id,
    )
    if refused is not None or user_id is None:
        return {"at": now.isoformat(), "skipped": refused or "no BASKFY_SOLE_USER_ID configured"}
    tenant = user_id

    async def _run(session: AsyncSession) -> JsonObject:
        if not await is_session_day(session, now.astimezone(IST).date()):
            return {"at": now.isoformat(), "skipped": "not an NSE trading day"}
        report, alerts = await plan_o2_minute(session, tenant, now, trading_day=True)
        await session.flush()
        report["alerts"] = [await dispatch(alert) for alert in alerts]
        return report

    return run_in_session(_run)


@shared_task(name="baskfy.options.plan_o3", acks_late=False)
def options_plan_o3_task(at: str | None = None) -> JsonObject:
    """OP8: lapse expired plans, then decide today's O3-B and O3-A sessions — once each.

    Refused before any database session unless ``BASKFY_OPTIONS_MONITOR_ENABLED`` and
    ``BASKFY_OPTIONS_COLLECT_ENABLED`` are true (both default false), inside 09:45-13:34 and with a
    sole tenant; then on an NSE holiday. The only Kite read is the margin **calculator** for the
    hedged two-leg basket, and only with a usable session (otherwise the plan carries
    ``MARGIN_UNKNOWN``). ``OPTIONS_PLAN`` is sent once per setup per day. No order path.
    """
    now = _options_now(at)
    settings = get_worker_settings()
    user_id = sole_user_id()
    refused = plan_o3_gate_free(
        now,
        monitor_enabled=settings.options_monitor_enabled,
        collect_enabled=settings.options_collect_enabled,
        user_id=user_id,
    )
    if refused is not None or user_id is None:
        return {"at": now.isoformat(), "skipped": refused or "no BASKFY_SOLE_USER_ID configured"}
    tenant = user_id
    margin = kite_margin_reader(build_options_kite().general) if kite_session_usable() else None

    async def _run(session: AsyncSession) -> JsonObject:
        if not await is_session_day(session, now.astimezone(IST).date()):
            return {"at": now.isoformat(), "skipped": "not an NSE trading day"}
        report, alerts = await plan_o3_minute(session, tenant, now, trading_day=True, margin=margin)
        await session.flush()
        report["alerts"] = [await dispatch(alert) for alert in alerts]
        return report

    return run_in_session(_run)


@shared_task(name="baskfy.options.weekly", acks_late=False)
def options_weekly_task(at: str | None = None) -> JsonObject:
    """OP11: the week's options journal, summarised per pool, as one ``OPTIONS_WEEKLY`` alert.

    Read-only. Refused before any database session unless ``BASKFY_OPTIONS_MONITOR_ENABLED`` is
    true and a sole tenant is configured.
    """
    now = _options_now(at)
    settings = get_worker_settings()
    user_id = sole_user_id()
    if not settings.options_monitor_enabled:
        return {"at": now.isoformat(), "skipped": "BASKFY_OPTIONS_MONITOR_ENABLED is false"}
    if user_id is None:
        return {"at": now.isoformat(), "skipped": "no BASKFY_SOLE_USER_ID configured"}
    tenant = user_id

    async def _run(session: AsyncSession) -> JsonObject:
        today = now.astimezone(IST).date()
        alert = weekly_alert(await week_rows(session, tenant, today), today)
        return {"at": now.isoformat(), "summary": alert.summary, "sent": await dispatch(alert)}

    return run_in_session(_run)


@shared_task(name="baskfy.options.checks", acks_late=False)
def options_checks_task(at: str | None = None) -> JsonObject:
    """OP14: the options checks due this minute; each failure is one ``OPTIONS_CHECK_FAILED``.

    Read-only. Returns before a database session when no check is due at the minute, and on a
    day the NSE calendar names a holiday. ``FLAT_AFTER_HARD_EXIT`` runs whatever the flags say.
    """
    now = _options_now(at)
    names = due_checks(now, OptionsConfig())
    if not names:
        return {"at": now.isoformat(), "skipped": "no options check is due at this minute"}
    settings = get_worker_settings()
    switches = Switches(
        scan_enabled=settings.options_scan_enabled,
        collect_enabled=settings.options_collect_enabled,
        monitor_enabled=settings.options_monitor_enabled,
    )

    async def _run(session: AsyncSession) -> JsonObject:
        if not await is_session_day(session, now.astimezone(IST).date()):
            return {"at": now.isoformat(), "skipped": "not an NSE trading day"}
        results = await run_checks(session, names, now, switches)
        sent = [await dispatch(check_alert(r, now)) for r in results if not r.ok]
        return {"at": now.isoformat(), "checks": [r.as_dict() for r in results], "sent": sent}

    return run_in_session(_run)


@shared_task(name="baskfy.options.backtest", acks_late=True, queue=QUEUE_COMPUTE)
def options_backtest_task(
    sleeve: str, tier: str, date_from: str, date_to: str | None = None
) -> JsonObject:
    """OP12: one sleeve at one tier over ``[date_from, date_to]``, appended to ``op_backtest_run``.

    On demand only (no Beat entry) and on the compute queue: a person asks for a run, from
    ``tools/options/backtest.py`` or by name. Reads stored bars, the master and (Tier 3) stored
    chain minutes; makes no Kite call and reaches no broker, so no flag gates it.
    """
    start = dt.date.fromisoformat(date_from)
    end = dt.date.fromisoformat(date_to) if date_to else dt.datetime.now(tz=IST).date()
    user_id = sole_user_id()
    if user_id is None:
        return {"from": start.isoformat(), "skipped": "no BASKFY_SOLE_USER_ID configured"}
    tenant = user_id

    async def _run(session: AsyncSession) -> JsonObject:
        report = await run_backtest(
            session, tenant, Sleeve(sleeve), OPTIONS_TIERS[tier], start, end,
            options=OptionsConfig(), ceilings=options_ceilings(), sha=git_sha(),
        )  # fmt: skip
        return report.as_dict()

    return run_in_session(_run)


@shared_task(name="baskfy.options.backfill_index_bars", acks_late=True)
def options_backfill_index_bars_task(date_from: str, date_to: str | None = None) -> JsonObject:
    """OP3: the Tier-1 index minute backfill over ``[date_from, date_to]`` — resumable.

    On demand only (no Beat entry). The **bulk** lane under the historical clock, so it yields to
    the collector and the desk; committed per 60-day window, and a re-run resumes from the newest
    stored bar. Not behind the collect flag: a person asked for it, and it is not a recurring load.
    """
    start = dt.date.fromisoformat(date_from)
    end = dt.date.fromisoformat(date_to) if date_to else dt.datetime.now(tz=IST).date()
    if not kite_session_usable():
        return {"from": start.isoformat(), "skipped": "no usable Kite session"}
    kite = build_options_kite(retry_hooks=provider_retry_hooks(), bars_lane=KiteLane.BULK)

    async def _run(session: AsyncSession) -> JsonObject:
        report = await options_index_bars.backfill(
            session,
            kite.bars,
            start,
            end,
            now=dt.datetime.now(tz=IST),
            checkpoint=session.commit,
        )
        return {"from": start.isoformat(), "to": end.isoformat(), **report.as_dict()}

    return run_checkpointed(_run)


@shared_task(name="baskfy.twt.detect", acks_late=True)
def twt_detect_task(trade_date: str | None = None) -> JsonObject:
    """TW4: detect the session's tight state and entry events, write its breadth row, ratchet.

    Idempotent per ``(user_id, date)``: re-running a date upserts its own rows and moves no
    counter (house rule 7). Defaults to today in IST when Beat fires without an argument.

    **This is the 21:00 retry as well as the CLI's entry point.** The nightly chain already runs
    the same detector as its fourteenth step, where it cannot fail the night; if the chain has
    not published by the time Beat fires — a slow bhavcopy, a provider stall — this is what
    writes the session anyway.

    It asks first, and it asks the **breadth** table rather than the signal table: this strategy
    signals about eighteen times a year, so "no signals" is the ordinary state of a session that
    ran perfectly, and a retry keyed on it would densify 260 sessions over the whole cash
    universe every single night to arrive at the same answer (DECISIONS-TW TW4.3).

    Nothing here places, arms or cancels anything, whichever way ``BASKFY_TWT_EXECUTION_ENABLED``
    is set: the ratchet it computes is a level stored for a plan a person confirms.
    """
    day = dt.date.fromisoformat(trade_date) if trade_date else dt.datetime.now(tz=IST).date()
    deps = build_pipeline_dependencies()
    if not deps.twt_nightly_enabled:
        return {"date": day.isoformat(), "skipped": "BASKFY_TWT_NIGHTLY_ENABLED is false"}
    if deps.twt_user_id is None:
        return {"date": day.isoformat(), "skipped": "no BASKFY_SOLE_USER_ID configured"}

    return run_in_session(
        lambda session: twt_detect_session(
            session, day, user_id=int(deps.twt_user_id or 0), force=False
        )
    )


@shared_task(name="baskfy.twt.evening", acks_late=True)
def twt_evening_task(trade_date: str | None = None) -> JsonObject:
    """TW6: the exits, then the entries, then the session row — the plan for tomorrow morning.

    Always **after** ``baskfy.twt.detect`` and never instead of it: the plan is built from the
    session's signals, the session's gate and the ``next_trigger`` the detector computed, and an
    evening that ran before the detector would plan against yesterday's tape.

    Nothing here places an order. Every line it writes is ``PROPOSED`` until it is confirmed on
    the desk — by a person, or, when ``BASKFY_TWT_AUTO_EXECUTE`` is on, by the desk's own
    ``app.twt_auto`` against the 09:05 MORNING plan (TW17, Maulik, in session, 21 Sep 2026).
    That flag is the desk's alone; this worker has no auto-execute setting and never confirms.
    """
    day = dt.date.fromisoformat(trade_date) if trade_date else dt.datetime.now(tz=IST).date()
    deps = build_pipeline_dependencies()
    if deps.twt_user_id is None:
        return {"date": day.isoformat(), "skipped": "no BASKFY_SOLE_USER_ID configured"}

    async def _run(session: AsyncSession) -> JsonObject:
        outcome = StepOutcome()
        report = await run_twt_evening(
            session,
            outcome,
            day,
            user_id=int(deps.twt_user_id or 0),
            execution_enabled=deps.twt_execution_enabled,
        )
        if report is None:
            return {"date": day.isoformat(), "skipped": outcome.detail}
        return report.as_detail()

    return run_in_session(_run)


@shared_task(name="baskfy.twt.morning", acks_late=True)
def twt_morning_task(trade_date: str | None = None) -> JsonObject:
    """TW6: the same plan, rebuilt before the open — **re-sized, not re-detected** (``04`` §11.3).

    Same session, same signals, same levels; the sizing moves because the sleeve's equity has
    moved with its marks. ``trade_date`` is the **signal** session, which before the open is the
    previous one, and a plan expires thirty minutes after it is built — which is why last
    night's cannot be confirmed at 09:15 and this exists.
    """
    today = dt.datetime.now(tz=IST).date()
    deps = build_pipeline_dependencies()
    if deps.twt_user_id is None:
        return {"date": today.isoformat(), "skipped": "no BASKFY_SOLE_USER_ID configured"}

    async def _run(session: AsyncSession) -> JsonObject:
        user_id = int(deps.twt_user_id or 0)
        day = (
            dt.date.fromisoformat(trade_date)
            if trade_date
            else await twt_last_detected_session(session, user_id, today)
        )
        if day is None:
            return {"date": today.isoformat(), "skipped": "no detected session to plan from"}
        outcome = StepOutcome()
        report = await run_twt_evening(
            session,
            outcome,
            day,
            user_id=user_id,
            execution_enabled=deps.twt_execution_enabled,
            source=TWT_SOURCE_MORNING,
        )
        if report is None:
            return {"date": day.isoformat(), "skipped": outcome.detail}
        return report.as_detail()

    return run_in_session(_run)


#: TW12's publisher task name. **Spelled as a literal in the decorator below as well**, not
#: only here, and that is deliberate: ``packages/core/tests/test_twt_safety_properties.py``
#: discovers this sleeve's task surface from source, and its discovery resolves a constant only
#: because TW12 hardened it — before that it read string literals alone, and registering a TWT
#: task through a constant would have added a scheduled path the safety property never looked at.
#:
#: **It is not called a sweep, and the swing book's and VBT-1's equivalents are.** On this sleeve
#: "sweep" means ``sweep_naked`` — the 15:15 chore that re-arms GTT stops through the gateway —
#: and ``services/worker/tests/test_twt_beat.py::test_the_sweep_is_not_on_a_timer`` refuses any
#: Beat entry whose name or task contains the word, because a scheduled TWT sweep would be a
#: second auto-execute exception. That test caught this task on 12 Sep 2026 and it was right to:
#: a reader scanning Beat for "twt sweep" must not find a publisher. DECISIONS-TW **TW12.4**.
TWT_SCAN_PUBLISH_TASK: Final = "baskfy.twt.scan_publish"


@shared_task(name="baskfy.twt.scan", acks_late=True)
def twt_scan_task(run_id: int) -> JsonObject:
    """TW12: one press of **Scan now** — detect the latest published session for one
    ``tw_scan_run`` row.

    **It is the existing detector.** ``run_twt_scan`` calls ``twt.detect_session``, the same
    function ``baskfy.twt.detect`` above calls, with ``force=True`` because a person asking for a
    scan is asking for exactly the session the nightly's "already detected" rule skips. There is
    no second detector (DECISIONS-TW **TW12.2**); the provisional path is the same detector
    over one more bar (TW19).

    Never raises: a failure is ``FAILED`` with its reason on the row, because the button has to be
    able to show what went wrong rather than leaving a request that simply stopped.

    It places, arms and cancels nothing, whichever way ``BASKFY_TWT_EXECUTION_ENABLED`` is set.

    **LV8 (28 Sep 2026, TW19):** during the session, with a Kite session, it is today's
    provisional bar and a LIVE plan — the quote source is built exactly as the swing scan's, on
    the INTERACTIVE lane, and only when the decision is provisional.
    """
    deps = build_pipeline_dependencies()

    def quote_source() -> QuoteSource:
        return build_kite_provider(
            get_provider_settings(), provider_retry_hooks(), lane=KiteLane.INTERACTIVE
        )

    async def _run(session: AsyncSession) -> JsonObject:
        return await run_twt_scan(
            session,
            int(run_id),
            quote_source=quote_source,
            live=functools.partial(run_twt_live, execution_enabled=deps.twt_execution_enabled),
        )

    return run_in_session(_run)


@shared_task(name="baskfy.twt.scan_publish")
def twt_scan_publish_task() -> JsonObject:
    """TW12: publish every ``QUEUED`` ``tw_scan_run`` row nobody has published.

    The desk writes those rows and has no Celery client, so without this the button would insert a
    row that sat there. Beat, every minute; one indexed SELECT when idle.
    """

    def publish(run_id: int) -> str:
        return str(twt_scan_task.apply_async(args=[run_id], queue=QUEUE_COMPUTE).id)

    async def _run(session: AsyncSession) -> JsonObject:
        rows = await twt_unpublished_scan_runs(session)
        published: list[int] = []
        for row in rows:
            row.task_id = publish(int(row.id))
            published.append(int(row.id))
        await session.flush()
        return {"published": published}

    return run_in_session(_run)


@shared_task(name="baskfy.vbt.evening", acks_late=True)
def vbt_evening_task(trade_date: str | None = None) -> JsonObject:
    """VB6: cancel what expired, queue the exits, plan the entries, settle the session.

    Always **after** ``baskfy.vbt.detect`` and never instead of it: the plan is built from the
    session's signals and the session's gate, and an evening that ran before the detector would
    plan against yesterday's tape.

    Nothing here places an order. Every line it writes is ``PROPOSED`` until a person confirms it
    on the desk (``docs/vbt/02`` Track C §3, DECISIONS-VB PACK.2).
    """
    day = dt.date.fromisoformat(trade_date) if trade_date else dt.datetime.now(tz=IST).date()
    deps = build_pipeline_dependencies()
    if deps.vbt_user_id is None:
        return {"date": day.isoformat(), "skipped": "no BASKFY_SOLE_USER_ID configured"}

    async def _run(session: AsyncSession) -> JsonObject:
        outcome = StepOutcome()
        report = await run_vbt_evening(
            session,
            outcome,
            day,
            user_id=int(deps.vbt_user_id or 0),
            execution_enabled=deps.vbt_execution_enabled,
        )
        if report is None:
            return {"date": day.isoformat(), "skipped": outcome.detail}
        return report.as_detail()

    return run_in_session(_run)


@shared_task(name="baskfy.vbt.morning", acks_late=True)
def vbt_morning_task(trade_date: str | None = None) -> JsonObject:
    """VB6: the same plan, rebuilt before the open.

    Same signals, same levels, re-sized against the sleeve as it stands — because the desk's plans
    expire in thirty minutes and last night's cannot be confirmed at 09:20. ``trade_date`` is the
    **signal** session, which before the open is the previous one.
    """
    today = dt.datetime.now(tz=IST).date()
    deps = build_pipeline_dependencies()
    if deps.vbt_user_id is None:
        return {"date": today.isoformat(), "skipped": "no BASKFY_SOLE_USER_ID configured"}

    async def _run(session: AsyncSession) -> JsonObject:
        user_id = int(deps.vbt_user_id or 0)
        day = (
            dt.date.fromisoformat(trade_date)
            if trade_date
            else await last_detected_session(session, user_id, today)
        )
        if day is None:
            return {"date": today.isoformat(), "skipped": "no detected session to plan from"}
        outcome = StepOutcome()
        report = await run_vbt_evening(
            session,
            outcome,
            day,
            user_id=user_id,
            execution_enabled=deps.vbt_execution_enabled,
            source=SOURCE_MORNING,
        )
        if report is None:
            return {"date": day.isoformat(), "skipped": outcome.detail}
        return report.as_detail()

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
    # Stamp this task's id BEFORE the commit (DECISIONS-MERGE AF C.1). `request_scan` defers its
    # publish to after commit (c944a22), which for this path is only the marker handing back
    # `task_id` and a follow-up UPDATE — so the committed row was briefly QUEUED with a null
    # `task_id`, exactly what the minute sweep publishes, and the scan could run twice. Nothing
    # is published here: this task is the one that runs the row, and it already exists.
    row.task_id = task_id
    await session.flush()
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


def _vbt_check(
    check: Callable[[AsyncSession, int], Awaitable[JsonObject]],
) -> JsonObject:
    """VB7: the sleeve's checks all need its user id, and none of them runs without one.

    A box with no `BASKFY_SOLE_USER_ID` has no sleeve to check, so the honest answer is `skipped`
    rather than a check that queries user zero and reports all-clear.
    """
    deps = build_pipeline_dependencies()
    if deps.vbt_user_id is None:
        return {"skipped": "no BASKFY_SOLE_USER_ID configured"}
    user_id = int(deps.vbt_user_id)
    return run_in_session(lambda session: check(session, user_id))


@shared_task(name="baskfy.vbt.rescan", acks_late=True)
def vbt_rescan_task(run_id: int) -> JsonObject:
    """VB12: re-detect the latest published session for one `vb_scan_run` row.

    Never raises: a failure is `FAILED` with its reason on the row, because the desk's button has
    to be able to show what went wrong rather than leaving a request that simply stopped.

    **LV8 (28 Sep 2026, VB14):** during the session, with a Kite session, it is today's
    provisional bar and a LIVE plan of ``BUY_AT_MARKET`` lines — the quote source is built as the
    swing scan's, on the INTERACTIVE lane, and only when the decision is provisional.
    """
    deps = build_pipeline_dependencies()

    def quote_source() -> QuoteSource:
        return build_kite_provider(
            get_provider_settings(), provider_retry_hooks(), lane=KiteLane.INTERACTIVE
        )

    async def _run(session: AsyncSession) -> JsonObject:
        return await run_vbt_rescan(
            session,
            int(run_id),
            quote_source=quote_source,
            live=functools.partial(run_vbt_live, execution_enabled=deps.vbt_execution_enabled),
        )

    return run_in_session(_run)


@shared_task(name="baskfy.vbt.rescan_sweep")
def vbt_rescan_sweep_task() -> JsonObject:
    """VB12: publish every `QUEUED` `vb_scan_run` row nobody has published.

    The desk writes those rows and has no Celery client, so without this the button would insert
    a row that sat there. Beat, every minute; one indexed SELECT when idle.
    """

    def publish(run_id: int) -> str:
        return str(vbt_rescan_task.apply_async(args=[run_id], queue=QUEUE_COMPUTE).id)

    async def _run(session: AsyncSession) -> JsonObject:
        rows = await unpublished_runs(session)
        published: list[int] = []
        for row in rows:
            row.task_id = publish(int(row.id))
            published.append(int(row.id))
        await session.flush()
        return {"published": published}

    return run_in_session(_run)


@shared_task(name="baskfy.vbt.backtest", acks_late=True)
def vbt_backtest_task(start: str | None = None, end: str | None = None) -> JsonObject:
    """VB9: re-run the study over the plant's bars and append one `vb_backtest_run` row.

    On demand rather than on Beat. The full history takes minutes, the answer only moves when the
    bars or the code do, and a nightly re-run would put a long compute job in the same window as
    the chain it would be competing with. `make vbt-backtest` is the handle.
    """
    deps = build_pipeline_dependencies()
    if deps.vbt_user_id is None:
        return {"skipped": "no BASKFY_SOLE_USER_ID configured"}
    user_id = int(deps.vbt_user_id)

    async def _run(session: AsyncSession) -> JsonObject:
        row = await run_vbt_backtest(
            session,
            user_id=user_id,
            start=dt.date.fromisoformat(start) if start else VBT_BACKTEST_START,
            end=dt.date.fromisoformat(end) if end else None,
        )
        return {
            "run_id": row.id,
            "source": row.source,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "drift": row.drift,
        }

    return run_in_session(_run)


@shared_task(name="baskfy.vbt.check_orders_past_expiry")
def vbt_check_orders_past_expiry_task() -> JsonObject:
    """21:40: VBT_ORDER_PAST_EXPIRY for a limit still working past its third session."""
    return _vbt_check(
        lambda session, user_id: vbt_ops.check_orders_past_expiry(
            session, now=dt.datetime.now(tz=IST), user_id=user_id
        )
    )


@shared_task(name="baskfy.vbt.check_naked_positions")
def vbt_check_naked_positions_task() -> JsonObject:
    """21:40: VBT_POSITION_NAKED for shares open with no resting GTT."""
    return _vbt_check(
        lambda session, user_id: vbt_ops.check_naked_positions(
            session, now=dt.datetime.now(tz=IST), user_id=user_id
        )
    )


@shared_task(name="baskfy.vbt.check_positions_without_bars")
def vbt_check_positions_without_bars_task() -> JsonObject:
    """21:40: VBT_POSITION_NO_BAR for a held name that has stopped printing."""
    return _vbt_check(
        lambda session, user_id: vbt_ops.check_positions_without_bars(
            session, now=dt.datetime.now(tz=IST), user_id=user_id
        )
    )


@shared_task(name="baskfy.vbt.check_detect_fresh")
def vbt_check_detect_fresh_task() -> JsonObject:
    """21:30: VBT_DETECT_STALE when the detector has written nothing for four days."""
    return _vbt_check(
        lambda session, user_id: vbt_ops.check_detect_fresh(
            session, now=dt.datetime.now(tz=IST), user_id=user_id
        )
    )


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


@shared_task(name=overlap_scan.SCAN_TASK, acks_late=False)
def overlap_catalyst_scan_task(user_id: int, scope: str = "actionable") -> JsonObject:
    """The overlap page's "Scan filings" button (`baskfy_api.overlap_scan`).

    Resolves the names `/build/overlap` lists for ``scope`` exactly as the page does, reads each
    one's filings and result dates through the morning feed's own loop (`run_swing_catalyst`
    with an explicit symbol set), then wakes the Laya sidecar so the new headlines are tagged
    now. Progress goes to Redis after every name; the lock the API took is released at the end,
    whatever happened. ``acks_late=False``: a redelivered scan would read NSE twice for nothing.
    """
    cache = SyncRedis.from_url(get_worker_settings().redis_url)
    key = overlap_scan.status_key(user_id)
    status: dict[str, object] = {
        "state": "running",
        "scope": scope,
        "started_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        "total": None,
        "done": 0,
    }

    def _save() -> None:
        cache.set(key, json.dumps(status), ex=overlap_scan.STATUS_TTL_SECONDS)

    def _progress(done: int, total: int) -> None:
        status["done"] = done
        status["total"] = total
        _save()

    _save()
    day = dt.datetime.now(tz=IST).date()
    provider = build_nse_provider(get_provider_settings(), retry_hooks=provider_retry_hooks())
    view_scope: overlap_service.Scope = "all" if scope == "all" else "actionable"

    async def _run(session: AsyncSession) -> JsonObject:
        view = await overlap_service.overlap(
            session, user_id=user_id, strategies_user_id=user_id, scope=view_scope
        )
        symbols = dict(sorted({row.symbol: row.instrument_id for row in view.rows}.items()))
        _progress(0, len(symbols))
        report = await run_swing_catalyst(
            session,
            StepOutcome(),
            day,
            user_id=user_id,
            provider=provider,
            symbols=symbols,
            on_symbol=_progress,
        )
        return report.as_detail()

    try:
        detail = run_in_session(_run)
    except Exception as exc:
        status.update(state="failed", error=f"{type(exc).__name__}: {exc}"[:500])
        raise
    else:
        status.update(
            state="done",
            announcements=detail.get("announcements"),
            earnings_dates=detail.get("earnings_dates"),
            rows_written=detail.get("rows_written"),
            failed=detail.get("failed") or [],
        )
        cache.set(overlap_scan.LAYA_WAKE_KEY, "1", ex=3600)
        return {"user_id": user_id, "scope": scope, **detail}
    finally:
        status["finished_at"] = dt.datetime.now(tz=dt.UTC).isoformat()
        _save()
        cache.delete(overlap_scan.lock_key(user_id))


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
