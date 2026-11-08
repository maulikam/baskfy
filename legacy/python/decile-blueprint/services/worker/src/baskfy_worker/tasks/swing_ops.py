"""SW11 — the swing book's in-process alert checks (STANDING-ANSWERS B8).

Four Beat entries, each at the moment its rule is about, each one query through
``baskfy_api.swing_health`` and — when the fact is wrong — one ``Alert`` through
``baskfy_worker.alerts.dispatch`` with the date attached:

    09:20  SWING_MONITOR_DID_NOT_START   the flag is on and no sw_session.monitor_ran today
    10:50  SWING_ORDER_OPEN_AFTER_CUTOFF a BUY line still SENT after the 10:45 cutoff
    15:20  SWING_GTT_MISSING_AT_1515     a position with shares open and no GTT after the sweep
    21:30  SWING_DETECT_STALE            no sw_market_daily for the published date

The Prometheus rules in ``infra/prometheus/alerts.yml`` evaluate the same facts from the gauges
the API refreshes per scrape; this is the other half of the `publish_late` pattern — a worker
that can raise the alert itself does, so the alert does not depend on Prometheus being
deployed. `SWING_POSITION_NAKED` (any time, any naked position) is Prometheus-only: it is a
condition over time, which is what a time-series rule is for.

The worker cannot fix any of these — the cutoff and the sweep are order-shaped and the desk's
(``app.swing_clock``); a check only tells. Silent when all is well; never raises.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.email import Mailer
from baskfy_api.settings import Settings
from baskfy_api.swing_health import (
    IST,
    detect_ran_for_published_date,
    is_session_day,
    monitor_ran_on,
    naked_positions,
    open_buy_orders_on,
)
from baskfy_core.models.base import JsonObject
from baskfy_worker.alerts import Alert, AlertName, Severity, dispatch
from baskfy_worker.ops import RUNBOOKS

log = logging.getLogger(__name__)

__all__ = [
    "RUNBOOK",
    "check_detect_fresh",
    "check_gtt_at_1515",
    "check_monitor_started",
    "check_orders_after_cutoff",
]

#: Runbook 6, repo-relative like every other alert's — the same path `ops.RUNBOOKS` maps the
#: five names to, read from there so the two cannot disagree.
RUNBOOK: Final = RUNBOOKS[AlertName.SWING_GTT_MISSING_AT_1515]


def _day(now: dt.datetime) -> dt.date:
    return now.astimezone(IST).date()


#: Monday..Friday in `datetime.weekday()` terms — NSE's cash session runs on weekdays.
_SATURDAY: Final = 5


def _weekday(now: dt.datetime) -> bool:
    return now.astimezone(IST).weekday() < _SATURDAY


async def _not_a_session(session: AsyncSession, now: dt.datetime) -> str | None:
    """Why an intraday check should stay quiet: a weekend, or a weekday the NSE calendar
    names a holiday. ``None`` on a session day."""
    if not _weekday(now):
        return "not a weekday"
    if not await is_session_day(session, _day(now)):
        return "an NSE holiday"
    return None


async def check_monitor_started(
    session: AsyncSession,
    *,
    now: dt.datetime,
    monitor_enabled: bool,
    settings: Settings | None = None,
    mailer: Mailer | None = None,
) -> JsonObject:
    """09:20: with the flag on, the monitor must have written `monitor_ran` for today."""
    day = _day(now)
    if not monitor_enabled:
        return {"date": day.isoformat(), "checked": False, "reason": "monitor flag is off"}
    if (quiet := await _not_a_session(session, now)) is not None:
        return {"date": day.isoformat(), "checked": False, "reason": quiet}
    ran = await monitor_ran_on(session, day)
    if ran:
        return {"date": day.isoformat(), "checked": True, "alert": None}
    alert = Alert(
        name=AlertName.SWING_MONITOR_DID_NOT_START,
        severity=Severity.CRITICAL,
        summary=f"The swing monitor has not started for {day.isoformat()} and the flag is on.",
        labels={"trade_date": day.isoformat()},
        detail={"checked_at": now.astimezone(IST).isoformat(timespec="seconds")},
        runbook=RUNBOOK,
    )
    return {
        "date": day.isoformat(),
        "checked": True,
        "alert": await dispatch(alert, settings, mailer=mailer),
    }


async def check_orders_after_cutoff(
    session: AsyncSession,
    *,
    now: dt.datetime,
    settings: Settings | None = None,
    mailer: Mailer | None = None,
) -> JsonObject:
    """10:50: no BUY line of today may still be SENT — the 10:45 cutoff cancels remainders."""
    day = _day(now)
    if (quiet := await _not_a_session(session, now)) is not None:
        return {"date": day.isoformat(), "checked": False, "reason": quiet}
    open_orders = await open_buy_orders_on(session, day)
    if open_orders == 0:
        return {"date": day.isoformat(), "checked": True, "open_orders": 0, "alert": None}
    alert = Alert(
        name=AlertName.SWING_ORDER_OPEN_AFTER_CUTOFF,
        severity=Severity.CRITICAL,
        summary=(
            f"{open_orders} swing buy order(s) still open after the 10:45 cutoff on "
            f"{day.isoformat()}."
        ),
        labels={"trade_date": day.isoformat()},
        detail={"open_orders": open_orders},
        runbook=RUNBOOK,
    )
    return {
        "date": day.isoformat(),
        "checked": True,
        "open_orders": open_orders,
        "alert": await dispatch(alert, settings, mailer=mailer),
    }


async def check_gtt_at_1515(
    session: AsyncSession,
    *,
    now: dt.datetime,
    settings: Settings | None = None,
    mailer: Mailer | None = None,
) -> JsonObject:
    """15:20: after the desk's sweep, no position with shares may be without a GTT (A8)."""
    day = _day(now)
    if (quiet := await _not_a_session(session, now)) is not None:
        return {"date": day.isoformat(), "checked": False, "reason": quiet}
    naked = await naked_positions(session)
    if naked == 0:
        return {"date": day.isoformat(), "checked": True, "naked": 0, "alert": None}
    alert = Alert(
        name=AlertName.SWING_GTT_MISSING_AT_1515,
        severity=Severity.CRITICAL,
        summary=(
            f"{naked} swing position(s) have shares open and no GTT after the 15:15 sweep on "
            f"{day.isoformat()} — the book goes into the close unprotected."
        ),
        labels={"trade_date": day.isoformat()},
        detail={"naked_positions": naked},
        runbook=RUNBOOK,
    )
    return {
        "date": day.isoformat(),
        "checked": True,
        "naked": naked,
        "alert": await dispatch(alert, settings, mailer=mailer),
    }


async def check_detect_fresh(
    session: AsyncSession,
    *,
    now: dt.datetime,
    settings: Settings | None = None,
    mailer: Mailer | None = None,
) -> JsonObject:
    """21:30: the detect step must have written the published date's market row."""
    day = _day(now)
    ran, published = await detect_ran_for_published_date(session)
    if ran:
        return {
            "date": day.isoformat(),
            "checked": True,
            "published": None if published is None else published.isoformat(),
            "alert": None,
        }
    if published is None:  # `ran` is True whenever nothing is published; this is unreachable
        return {"date": day.isoformat(), "checked": False, "reason": "nothing published"}
    alert = Alert(
        name=AlertName.SWING_DETECT_STALE,
        severity=Severity.CRITICAL,
        summary=(
            f"No swing detection for the published date {published.isoformat()} by 21:30 — "
            f"tomorrow's plan would be built on a stale gate."
        ),
        labels={"trade_date": published.isoformat()},
        detail={"checked_at": now.astimezone(IST).isoformat(timespec="seconds")},
        runbook=RUNBOOK,
    )
    return {
        "date": day.isoformat(),
        "checked": True,
        "published": published.isoformat(),
        "alert": await dispatch(alert, settings, mailer=mailer),
    }
