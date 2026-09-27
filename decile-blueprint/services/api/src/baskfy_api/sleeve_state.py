"""Per-sleeve state for the pages' Scan buttons — what each book is doing right now, and why (LV4).

Review P1.3: "clearly show missed-window / waiting-for-login / signal-ready / plan-ready /
monitoring / blocked states and their reasons", so a 10:30 login "produces an explicit,
deterministic outcome per sleeve" and "an EOD detector is never labelled a new intraday strategy".

Everything here is **read from stored rows**: the calendar, the token blob, each sleeve's newest
scan run and newest plan, the swing watchlist, the desk processes' heartbeats (``lv_heartbeat``,
written by the session supervisor, the reconciler, the swing monitor and ``twt-auto``) and the
reconciler's open protection issues. :func:`derive_state` is pure — facts and a clock in, one
state and its reason out — so every branch is a unit test; :func:`sleeve_states` gathers the
facts. Nothing here queues, scans, plans or places.

The vocabulary is honest about what each sleeve can do (the review's structural finding): only
swing scans "today so far"; TWT's and VBT's Scan re-detects the last **published** session, and
their copy says so.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from typing import Final, Literal, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.broker_oauth import is_simulated_token, token_store_for
from baskfy_api.schemas import SleeveStateOut
from baskfy_api.swing_health import is_session_day
from baskfy_core.market_hours_cb import IST, is_nse_session_open
from baskfy_core.models import (
    LvHeartbeat,
    LvProtectionIssue,
    SwPlan,
    SwScanRun,
    SwWatch,
    TwPlan,
    TwScanRun,
    VbPlan,
    VbScanRun,
)
from baskfy_providers.errors import CredentialsMissing

Sleeve = Literal["swing", "twt", "vbt"]
SLEEVES: Final[tuple[Sleeve, ...]] = ("swing", "twt", "vbt")

#: A heartbeat older than this is a process that is not running (the supervisor beats every
#: ten seconds; the monitor every loop pass, at most every fifteen).
HEARTBEAT_FRESH_SECONDS: Final = 120
#: NSE's cash session, IST wall-clock. Facts about the exchange, not thresholds.
MARKET_OPEN: Final = dt.time(9, 15)
MARKET_CLOSE: Final = dt.time(15, 30)
#: The TWT morning plan's window: built 09:05, expires 09:35 (twt-auto's whole life).
TWT_ENTRY_WINDOW_END: Final = dt.time(9, 35)

_SCAN_MODELS: Final[dict[str, type[SwScanRun] | type[TwScanRun] | type[VbScanRun]]] = {
    "swing": SwScanRun,
    "twt": TwScanRun,
    "vbt": VbScanRun,
}
_PLAN_MODELS: Final[dict[str, type[SwPlan] | type[TwPlan] | type[VbPlan]]] = {
    "swing": SwPlan,
    "twt": TwPlan,
    "vbt": VbPlan,
}

#: Which desk process speaks for which sleeve's "monitoring".
MONITOR_PROCESS: Final[dict[str, str]] = {"swing": "swing_monitor", "twt": "twt_auto"}

#: What each sleeve's Scan button actually does — said beside the state so nobody reads a
#: re-detection of a closed session as a live scan (review P1.3).
SCAN_MEANS: Final[dict[str, str]] = {
    "swing": "Scan reads today so far from Kite quotes (provisional bars) during the session",
    "twt": "Scan re-detects the last published session; TWT enters at the next open only",
    "vbt": "Scan re-detects the last published session; VBT bids at the signal close",
}


@dataclass(frozen=True)
class SleeveFacts:
    """Everything :func:`derive_state` reads. Dates and times are IST-aware."""

    sleeve: str
    session_day: bool
    market_open: bool
    broker_session: bool
    scan_status: str | None = None
    scan_found: int | None = None
    scan_finished_at: dt.datetime | None = None
    scan_provisional: bool = False
    plan_source: str | None = None
    plan_session_date: dt.date | None = None
    plan_expires_at: dt.datetime | None = None
    heartbeat_at: dt.datetime | None = None
    heartbeat_state: str | None = None
    open_issues: int = 0
    issue_kinds: tuple[str, ...] = ()
    watching: int = 0


def _fresh(stamp: dt.datetime | None, now: dt.datetime) -> bool:
    return stamp is not None and (now - stamp).total_seconds() <= HEARTBEAT_FRESH_SECONDS


def derive_state(facts: SleeveFacts, now: dt.datetime) -> SleeveStateOut:  # noqa: PLR0911 - one branch per state, in precedence order
    """One state and one reason per sleeve, deterministic in ``facts`` and ``now``.

    Precedence: blocked > closed > waiting_for_login > scanning > monitoring > plan_ready >
    missed_window > signal_ready > idle. Blocked first because it is the one a person must act
    on; closed next because nothing else means anything on a holiday.
    """
    local = now.astimezone(IST)
    today = local.date()
    sleeve = facts.sleeve
    means = SCAN_MEANS[sleeve]

    def out(state: str, reason: str, nxt: str) -> SleeveStateOut:
        return SleeveStateOut(
            sleeve=sleeve,
            state=state,
            reason=reason,
            scan_means=means,
            as_of=facts.plan_session_date,
            next=nxt,
            updated_at=now,
        )

    if facts.open_issues:
        kinds = ", ".join(facts.issue_kinds) or "protection"
        return out(
            "blocked",
            f"{facts.open_issues} open protection issue(s): {kinds}. Every {sleeve} entry is "
            f"refused until the reconciler sees the book whole again",
            "resolve the issue on the desk's lifecycle page",
        )
    if not facts.session_day:
        return out("closed", "not an NSE session today", "the next session's login")
    if facts.scan_status in ("QUEUED", "RUNNING"):
        return out("scanning", f"a scan is {facts.scan_status.lower()}", "its result")
    after_close = local.time() >= MARKET_CLOSE
    if not facts.broker_session and not after_close:
        what = {
            "swing": "the live scan, the monitor and the desk's entries",
            "twt": "the 09:15 entry run and the desk's entries",
            "vbt": "the desk's confirms",
        }[sleeve]
        return out(
            "waiting_for_login",
            f"no Kite session — {what} wait for a login",
            "log in to Kite from the brokers page",
        )
    if sleeve == "swing" and facts.market_open and _fresh(facts.heartbeat_at, now):
        return out(
            "monitoring",
            f"the monitor is watching {facts.watching} name(s) and confirms its own triggers "
            f"until {MARKET_CLOSE:%H:%M}",
            "a trigger",
        )
    plan_today = facts.plan_session_date == today or (
        sleeve == "swing" and facts.plan_session_date is not None
    )
    plan_live = plan_today and facts.plan_expires_at is not None and facts.plan_expires_at > now
    if plan_live and facts.plan_expires_at is not None:
        return out(
            "plan_ready",
            f"a {facts.plan_source or ''} plan is ready; it expires at "
            f"{facts.plan_expires_at.astimezone(IST):%H:%M}",
            "confirm on the desk" + (" (twt-auto confirms at 09:15)" if sleeve == "twt" else ""),
        )
    if sleeve == "twt" and facts.market_open and local.time() >= TWT_ENTRY_WINDOW_END:
        had = facts.plan_source == "MORNING" and facts.plan_session_date == today
        return out(
            "missed_window",
            (
                "the 09:05 MORNING plan expired at 09:35"
                if had
                else "no MORNING plan was built for today"
            )
            + " — TWT enters at the open only, and today's open has passed",
            "tomorrow's plan, built at 21:20 and re-sized at 09:05",
        )
    if sleeve == "vbt" and facts.market_open and plan_today and not plan_live:
        return out(
            "missed_window",
            "today's plan has expired; VBT's limit at the signal close is placed from a plan",
            "this evening's plan, built at 21:15",
        )
    if facts.scan_status == "DONE" and (facts.scan_found or 0) > 0:
        when = (
            f" at {facts.scan_finished_at.astimezone(IST):%H:%M}" if facts.scan_finished_at else ""
        )
        prov = " (provisional, today so far)" if facts.scan_provisional else ""
        return out(
            "signal_ready",
            f"{facts.scan_found} candidate(s) from the last scan{when}{prov}; a plan is built in "
            f"the evening",
            "the evening plan, or the desk",
        )
    if after_close:
        return out("idle", "the session has closed", "tonight's detect and plan")
    if facts.scan_status == "DONE":
        return out("idle", "the last scan found nothing", "the next scan")
    if facts.scan_status == "FAILED":
        return out("idle", "the last scan failed; see the run", "press Scan again")
    return out("idle", "nothing is queued, planned or watched", "Scan, or the evening jobs")


def broker_session_exists() -> bool:
    """A real, unexpired, non-simulated Kite token on disk — the same local checks the brokers
    page uses for "connected"; no network call and no market-data flag (this is not a quote)."""
    if not os.environ.get("BASKFY_KITE_API_KEY", "").strip():
        return False
    try:
        token = token_store_for().load()
    except CredentialsMissing:
        return False
    return not (is_simulated_token(token.value) or token.is_expired())


def _found_in(detail: object) -> int | None:
    """The scans store their funnel under different keys; the count the page shows is the last
    non-zero integer among the ones every sleeve uses for "candidates"."""
    if not isinstance(detail, dict):
        return None
    for key in ("found", "signals", "setups", "candidates", "detected"):
        value = detail.get(key)
        if isinstance(value, int):
            return value
    return None


async def _facts(  # noqa: PLR0913 - one read per fact, named
    session: AsyncSession,
    *,
    sleeve: str,
    user_id: int,
    session_day: bool,
    market_open: bool,
    broker_session: bool,
) -> SleeveFacts:
    scan_model = _SCAN_MODELS[sleeve]
    scan = cast(
        "SwScanRun | TwScanRun | VbScanRun | None",
        (
            await session.execute(
                select(scan_model)
                .where(scan_model.user_id == user_id)
                .order_by(scan_model.requested_at.desc(), scan_model.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none(),
    )
    plan_model = _PLAN_MODELS[sleeve]
    plan = cast(
        "SwPlan | TwPlan | VbPlan | None",
        (
            await session.execute(
                select(plan_model)
                .where(plan_model.user_id == user_id)
                .order_by(plan_model.built_at.desc(), plan_model.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none(),
    )
    process = MONITOR_PROCESS.get(sleeve)
    beat = None
    if process is not None:
        beat = (
            await session.execute(
                select(LvHeartbeat).where(
                    LvHeartbeat.user_id == user_id, LvHeartbeat.process == process
                )
            )
        ).scalar_one_or_none()
    issue_rows = (
        await session.execute(
            select(LvProtectionIssue.kind, func.count())
            .where(
                LvProtectionIssue.user_id == user_id,
                LvProtectionIssue.sleeve == sleeve,
                LvProtectionIssue.resolved_at.is_(None),
            )
            .group_by(LvProtectionIssue.kind)
        )
    ).all()
    watching = 0
    if sleeve == "swing":
        watching = int(
            (
                await session.execute(
                    select(func.count()).where(
                        SwWatch.user_id == user_id, SwWatch.state == "WATCHING"
                    )
                )
            ).scalar_one()
        )
    plan_date = None
    if plan is not None:
        plan_date = plan.as_of if isinstance(plan, SwPlan) else plan.session_date
    return SleeveFacts(
        sleeve=sleeve,
        session_day=session_day,
        market_open=market_open,
        broker_session=broker_session,
        scan_status=None if scan is None else str(scan.status),
        scan_found=None if scan is None else _found_in(scan.detail),
        scan_finished_at=None if scan is None else scan.finished_at,
        scan_provisional=bool(getattr(scan, "provisional", False)) if scan is not None else False,
        plan_source=None if plan is None else str(plan.source),
        plan_session_date=plan_date,
        plan_expires_at=None if plan is None else plan.expires_at,
        heartbeat_at=None if beat is None else beat.at,
        heartbeat_state=None if beat is None else str(beat.state),
        open_issues=sum(int(count) for _kind, count in issue_rows),
        issue_kinds=tuple(sorted(str(kind) for kind, _count in issue_rows)),
        watching=watching,
    )


async def sleeve_states(
    session: AsyncSession, *, user_id: int, now: dt.datetime | None = None
) -> list[SleeveStateOut]:
    """The three sleeves' states, from stored rows and the clock. Read-only."""
    stamp = now or dt.datetime.now(tz=IST)
    today = stamp.astimezone(IST).date()
    session_day = today.weekday() < 5 and await is_session_day(session, today)  # noqa: PLR2004 - Saturday
    market_open = is_nse_session_open(stamp, {today} if session_day else set())
    broker = broker_session_exists()
    return [
        derive_state(
            await _facts(
                session,
                sleeve=sleeve,
                user_id=user_id,
                session_day=session_day,
                market_open=market_open,
                broker_session=broker,
            ),
            stamp,
        )
        for sleeve in SLEEVES
    ]
