"""The four checks behind the volume-breakout sleeve's alerts (``docs/vbt/05`` §4).

    21:30  VBT_DETECT_STALE        no ``vb_breadth_daily`` row for the published session
    21:40  VBT_ORDER_PAST_EXPIRY   a working limit still live after its third session
    21:40  VBT_POSITION_NAKED      a position with shares open and no resting GTT
    21:40  VBT_POSITION_NO_BAR     a held name that has stopped printing (``04`` §6.5)

Three of the four run after the evening job, because that is when each becomes answerable: the
sweep has produced its cancel lines, the plan has been written, and whatever is still wrong is
wrong for a reason a person needs to know tonight rather than at nine tomorrow.

**The worker cannot fix any of them.** The cancel and the GTT are order-shaped and belong to the
desk's confirm; a check only tells. Silent when all is well; never raises into the evening.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.email import Mailer
from baskfy_api.settings import Settings
from baskfy_core.models import OhlcvDaily, VbBreadthDaily, VbOrder, VbPosition
from baskfy_core.models.base import JsonObject
from baskfy_worker.alerts import Alert, AlertName, Severity, dispatch
from baskfy_worker.ops import RUNBOOKS

log = logging.getLogger(__name__)

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))

#: Runbook 8, read from `ops.RUNBOOKS` so the map and the page cannot disagree.
RUNBOOK: Final = RUNBOOKS[AlertName.VBT_ORDER_PAST_EXPIRY]

__all__ = [
    "RUNBOOK",
    "check_detect_fresh",
    "check_naked_positions",
    "check_orders_past_expiry",
    "check_positions_without_bars",
]


def _day(now: dt.datetime) -> dt.date:
    return now.astimezone(IST).date()


#: Monday..Friday in `datetime.weekday()` terms, the same constant `swing_ops` keeps.
_SATURDAY: Final = 5


def _weekday(now: dt.datetime) -> bool:
    """Every one of these four is an evening check, and there is no evening on a Saturday.

    A naked position over a weekend cannot come to harm — the exchange is shut — and paging for a
    weekend trains people to ignore pages, which is the failure mode that matters. Each condition
    is still true on Monday at 21:40, and that is when it is worth waking someone for.
    """
    return now.astimezone(IST).weekday() < _SATURDAY


async def check_detect_fresh(
    session: AsyncSession,
    *,
    now: dt.datetime,
    user_id: int,
    settings: Settings | None = None,
    mailer: Mailer | None = None,
) -> JsonObject:
    """21:30: the sleeve must have a breadth row for a session by the end of the evening.

    Its absence is the difference between "no signals today" and "the detector did not run", and
    a page cannot tell them apart — which is the whole reason this check exists.
    """
    day = _day(now)
    if not _weekday(now):
        return {"date": day.isoformat(), "checked": False, "reason": "not a weekday"}
    latest = (
        await session.execute(
            select(func.max(VbBreadthDaily.date)).where(VbBreadthDaily.user_id == user_id)
        )
    ).scalar_one_or_none()
    if latest is not None and latest >= day - dt.timedelta(days=4):
        return {
            "date": day.isoformat(),
            "checked": True,
            "latest": latest.isoformat(),
            "alert": None,
        }
    alert = Alert(
        name=AlertName.VBT_DETECT_STALE,
        severity=Severity.WARNING,
        summary=(
            f"The volume-breakout detector has written nothing since "
            f"{latest.isoformat() if latest else 'never'} (checked {day.isoformat()})."
        ),
        labels={"trade_date": day.isoformat()},
        detail={"latest": latest.isoformat() if latest else None},
        runbook=RUNBOOK,
    )
    return {
        "date": day.isoformat(),
        "checked": True,
        "alert": await dispatch(alert, settings, mailer=mailer),
    }


async def check_orders_past_expiry(
    session: AsyncSession,
    *,
    now: dt.datetime,
    user_id: int,
    settings: Settings | None = None,
    mailer: Mailer | None = None,
) -> JsonObject:
    """21:40: VB7's own alarm — a limit that has outlived its window is still an order.

    The evening's sweep writes a ``CANCEL_LIMIT`` line and a person confirms it, so an order
    still live the next evening means the line was never confirmed. That is a real position risk:
    a bid resting in a name whose setup is four days old can fill on news nobody planned for.
    """
    day = _day(now)
    if not _weekday(now):
        return {"date": day.isoformat(), "checked": False, "reason": "not a weekday"}
    rows = (
        await session.execute(
            select(VbOrder).where(
                VbOrder.user_id == user_id,
                VbOrder.state.in_(("PROPOSED", "CONFIRMED", "SENT", "PARTIAL")),
                VbOrder.expires_after_session.is_not(None),
                VbOrder.expires_after_session < day,
            )
        )
    ).scalars()
    stale = list(rows)
    if not stale:
        return {"date": day.isoformat(), "checked": True, "past_expiry": 0, "alert": None}
    alert = Alert(
        name=AlertName.VBT_ORDER_PAST_EXPIRY,
        severity=Severity.CRITICAL,
        summary=(
            f"{len(stale)} volume-breakout limit order(s) are still working past their third "
            f"session on {day.isoformat()}."
        ),
        labels={"trade_date": day.isoformat()},
        detail={
            "orders": [
                {
                    "id": row.id,
                    "instrument_id": row.instrument_id,
                    "signal_date": row.signal_date.isoformat(),
                    "expires_after_session": (
                        row.expires_after_session.isoformat() if row.expires_after_session else None
                    ),
                    "state": row.state,
                }
                for row in stale[:10]
            ]
        },
        runbook=RUNBOOK,
    )
    return {
        "date": day.isoformat(),
        "checked": True,
        "past_expiry": len(stale),
        "alert": await dispatch(alert, settings, mailer=mailer),
    }


async def check_naked_positions(
    session: AsyncSession,
    *,
    now: dt.datetime,
    user_id: int,
    settings: Settings | None = None,
    mailer: Mailer | None = None,
) -> JsonObject:
    """21:40: a position with shares open and no resting GTT — the one state the method forbids."""
    day = _day(now)
    if not _weekday(now):
        return {"date": day.isoformat(), "checked": False, "reason": "not a weekday"}
    rows = (
        await session.execute(
            select(VbPosition).where(
                VbPosition.user_id == user_id,
                VbPosition.state == "OPEN",
                VbPosition.quantity_open > 0,
                VbPosition.gtt_id.is_(None),
            )
        )
    ).scalars()
    naked = list(rows)
    if not naked:
        return {"date": day.isoformat(), "checked": True, "naked": 0, "alert": None}
    alert = Alert(
        name=AlertName.VBT_POSITION_NAKED,
        severity=Severity.CRITICAL,
        summary=(
            f"{len(naked)} volume-breakout position(s) have shares open and no GTT stop on "
            f"{day.isoformat()}."
        ),
        labels={"trade_date": day.isoformat()},
        detail={
            "positions": [
                {"id": row.id, "instrument_id": row.instrument_id, "qty": row.quantity_open}
                for row in naked[:10]
            ]
        },
        runbook=RUNBOOK,
    )
    return {
        "date": day.isoformat(),
        "checked": True,
        "naked": len(naked),
        "alert": await dispatch(alert, settings, mailer=mailer),
    }


async def check_positions_without_bars(
    session: AsyncSession,
    *,
    now: dt.datetime,
    user_id: int,
    settings: Settings | None = None,
    mailer: Mailer | None = None,
) -> JsonObject:
    """21:40: a held name that has stopped printing (``04`` §6.5).

    The evening writes the position off after five blank sessions, at its last close. **A person
    has to see it before that**, because a delisting is a fact, not a price, and the five-session
    write-off is a bookkeeping rule rather than an exit anybody chose.
    """
    day = _day(now)
    if not _weekday(now):
        return {"date": day.isoformat(), "checked": False, "reason": "not a weekday"}
    held = list(
        (
            await session.execute(
                select(VbPosition).where(
                    VbPosition.user_id == user_id,
                    VbPosition.state == "OPEN",
                    VbPosition.quantity_open > 0,
                )
            )
        ).scalars()
    )
    if not held:
        return {"date": day.isoformat(), "checked": True, "silent": 0, "alert": None}
    last_bars = {
        int(instrument_id): last
        for instrument_id, last in (
            await session.execute(
                select(OhlcvDaily.instrument_id, func.max(OhlcvDaily.date))
                .where(OhlcvDaily.instrument_id.in_([row.instrument_id for row in held]))
                .group_by(OhlcvDaily.instrument_id)
            )
        ).all()
    }
    silent = [
        row
        for row in held
        if (last := last_bars.get(row.instrument_id)) is None or (day - last).days > _QUIET_DAYS
    ]
    if not silent:
        return {"date": day.isoformat(), "checked": True, "silent": 0, "alert": None}
    alert = Alert(
        name=AlertName.VBT_POSITION_NO_BAR,
        severity=Severity.WARNING,
        summary=(
            f"{len(silent)} volume-breakout position(s) have not printed a bar in more than "
            f"{_QUIET_DAYS} days as of {day.isoformat()}."
        ),
        labels={"trade_date": day.isoformat()},
        detail={
            "positions": [
                {
                    "id": row.id,
                    "instrument_id": row.instrument_id,
                    "last_bar": (
                        last_bars[row.instrument_id].isoformat()
                        if row.instrument_id in last_bars
                        else None
                    ),
                }
                for row in silent[:10]
            ]
        },
        runbook=RUNBOOK,
    )
    return {
        "date": day.isoformat(),
        "checked": True,
        "silent": len(silent),
        "alert": await dispatch(alert, settings, mailer=mailer),
    }


#: Calendar days, not sessions: this check runs before the calendar is loaded and a week of
#: silence is a week of silence however many of its days the exchange was open. `04` §6.5's
#: five-session write-off is the rule; this is the warning that comes first.
_QUIET_DAYS: Final = 7
