"""The FO book's event alerts (``docs/fno/06`` FO12): plan issued, exit done, hard exit tomorrow,
``LATE_EXIT``, and a missing F&O bhavcopy.

WHY THE WORKER RAISES WHAT THE DESK DID
---------------------------------------
The desk issues the plans, runs the exits and records ``LATE_EXIT`` (FO7), but the desk process
cannot reach the data plant's alert sinks (a different venv; ``app/twt_execute.py`` says the same
of TWT's sweep). So the worker reads what the desk wrote — ``fo_plan``, ``fo_journal``,
``fo_position`` — and raises each event once through :func:`baskfy_worker.alerts.dispatch`, the
one mechanism that already tells Maulik something (the options pack's OP14 checks are the same
shape). **Read-only**: nothing here writes an ``fo_`` row, and nothing reaches a broker.

* ``FNO_PLAN`` — an ENTRY plan issued today and not refused (``REJECTED_*``): the plan to confirm
  on the desk before it expires. ROLL and EXIT plans run under the entry's confirm and are not
  asked about.
* ``FNO_EXIT`` — each ``fo_journal`` row closed today (one per structure), in ₹ and R, paper or
  LIVE, never pooled.
* ``FNO_HARD_EXIT_TOMORROW`` — from 18:00 IST, every open structure whose hard exit (F1's
  ``hard_exit_date``; F2's ``time_exit_date`` in its carry, the nav badge's rule, FO8.3) is the
  next NSE session.
* ``FNO_LATE_EXIT`` — each ``LATE_EXIT`` the desk appended to an entry plan's
  ``detail.violations`` today (FO7.12).
* ``FNO_BHAVCOPY_MISSING`` — the 23:30 attempt of ``baskfy.fno.ingest_bhavcopy`` recorded
  ``MISSING``: raised in that task, from its result (:func:`bhavcopy_missing_alert`).

ONCE EACH. Beat runs :func:`run_fno_alerts` every five minutes; each event has a Redis marker
(``SET NX``), so a second run finds it claimed and says nothing. Redis unavailable delivers anyway
(fail open, as ``ops.run_kite_token_check`` does): a repeated plan email is noise, a lost one is a
missed entry. Only today's events are considered, so the first run after a deploy does not replay
history. DARK unless ``BASKFY_FNO_MONITOR_ENABLED`` (the Celery task refuses first); the bhavcopy
alert is dark with the ingest itself, behind ``BASKFY_FNO_SCAN_ENABLED``.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.email import Mailer
from baskfy_api.settings import Settings
from baskfy_core.fno.config import PlanKind
from baskfy_core.models import FoJournal, FoPlan, FoPosition
from baskfy_core.models.base import JsonObject
from baskfy_worker.alerts import Alert, AlertName, Severity, dispatch
from baskfy_worker.fno.ingest import STATUS_MISSING
from baskfy_worker.fno.nightly import next_session
from baskfy_worker.ops import RUNBOOKS

log = logging.getLogger(__name__)

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))
#: From this IST time the evening's ``FNO_HARD_EXIT_TOMORROW`` is raised (after the close and the
#: 18:15 equity bhavcopy; the F&O nightly starts at 18:30).
HARD_EXIT_NOTICE_FROM: Final = dt.time(18, 0)
MARKER_PREFIX: Final = "fno:alert:"
#: A marker outlives the event's day; the key names the event, so the TTL is only housekeeping.
MARKER_TTL_SECONDS: Final = 3 * 24 * 60 * 60
LATE_EXIT: Final = "LATE_EXIT"


class MarkerCache(Protocol):
    """The two Redis calls the de-duplication needs (``redis.asyncio.Redis`` satisfies it)."""

    async def set(
        self, name: str, value: str, *, nx: bool = ..., ex: int | None = ...
    ) -> bool | None: ...


@dataclass(frozen=True, slots=True)
class RedisMarkers:
    """The worker's Redis client behind :class:`MarkerCache` (the Celery task's adapter)."""

    redis: Redis

    async def set(
        self, name: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        return bool(await self.redis.set(name, value, nx=nx, ex=ex))


@dataclass(frozen=True, slots=True)
class Pending:
    """One event: its de-duplication key and the alert it raises."""

    marker: str
    alert: Alert


def _mode(simulated: bool) -> str:
    return "paper" if simulated else "LIVE"


def _ist(moment: dt.datetime) -> dt.datetime:
    return moment.astimezone(IST)


def _day(raw: object) -> dt.date | None:
    if isinstance(raw, dt.datetime):
        return _ist(raw).date() if raw.tzinfo is not None else raw.date()
    if isinstance(raw, dt.date):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = dt.datetime.fromisoformat(raw)
        except ValueError:
            return None
        return _ist(parsed).date() if parsed.tzinfo is not None else parsed.date()
    return None


# ------------------------------------------------------------------------------------------------
# Pure: one event, one alert
# ------------------------------------------------------------------------------------------------


def plan_alert(plan: FoPlan) -> Alert:
    """``FNO_PLAN``: an entry plan to confirm on the desk before it expires."""
    price = (
        f"credit {plan.credit_points} pts"
        if plan.credit_points is not None
        else f"entry {plan.debit_points} pts"
        if plan.debit_points is not None
        else "price on the card"
    )
    expires = _ist(plan.expires_at).strftime("%H:%M")
    parts = [
        f"{plan.sleeve} {plan.symbol} {plan.structure} plan {plan.plan_id} issued",
        f"{plan.lots} lot(s) of {plan.lot_size}",
        price,
    ]
    if plan.max_loss_inr is not None:
        parts.append(f"max loss ₹{plan.max_loss_inr}")
    if plan.hard_exit_date is not None:
        parts.append(f"hard exit {plan.hard_exit_date.isoformat()}")
    parts.append(f"confirm on the desk's F&O tab by {expires} IST or it lapses")
    return Alert(
        name=AlertName.FNO_PLAN,
        severity=Severity.WARNING,
        summary="; ".join(parts) + ".",
        labels={"sleeve": plan.sleeve, "symbol": plan.symbol, "trade_date": str(plan.trade_date)},
        detail={
            "plan_id": plan.plan_id,
            "structure": plan.structure,
            "lots": plan.lots,
            "lot_size": plan.lot_size,
            "credit_points": None if plan.credit_points is None else str(plan.credit_points),
            "debit_points": None if plan.debit_points is None else str(plan.debit_points),
            "max_loss_inr": None if plan.max_loss_inr is None else str(plan.max_loss_inr),
            "margin_required_inr": (
                None if plan.margin_required_inr is None else str(plan.margin_required_inr)
            ),
            "expires_at": plan.expires_at.isoformat(),
            "status": plan.status,
        },
        runbook=RUNBOOKS[AlertName.FNO_PLAN],
    )


def exit_alert(row: FoJournal) -> Alert:
    """``FNO_EXIT``: one structure closed, in ₹ and R, its mode named."""
    return Alert(
        name=AlertName.FNO_EXIT,
        severity=Severity.WARNING,
        summary=(
            f"{row.sleeve} {_mode(row.simulated)} {row.symbol} {row.structure} closed "
            f"({row.closed_reason}) on {row.closed_on.isoformat()}: net ₹{row.net_pnl_inr} "
            f"after ₹{row.costs_inr} costs, {row.r_multiple}R, {row.sessions_held} session(s), "
            f"{row.rolls} roll(s)."
        ),
        labels={"sleeve": row.sleeve, "symbol": row.symbol, "closed_on": str(row.closed_on)},
        detail={
            "position_id": row.position_id,
            "closed_reason": row.closed_reason,
            "simulated": row.simulated,
            "net_pnl_inr": str(row.net_pnl_inr),
            "r_multiple": str(row.r_multiple),
        },
        runbook=RUNBOOKS[AlertName.FNO_EXIT],
    )


def hard_exit_tomorrow_alert(position: FoPosition, exit_on: dt.date, which: str) -> Alert:
    """``FNO_HARD_EXIT_TOMORROW``: an open structure whose hard (F2: time) exit is next session."""
    return Alert(
        name=AlertName.FNO_HARD_EXIT_TOMORROW,
        severity=Severity.WARNING,
        summary=(
            f"{position.sleeve} {_mode(position.simulated)} {position.symbol} "
            f"{position.structure} (position {position.id}) is still open and its {which} is "
            f"tomorrow, {exit_on.isoformat()}: the desk's monitor closes it under the entry's "
            "confirm; have the Kite login done before 09:15."
        ),
        labels={"sleeve": position.sleeve, "symbol": position.symbol, "exit_on": str(exit_on)},
        detail={
            "position_id": position.id,
            "which": which,
            "entry_plan_id": position.entry_plan_id,
        },
        runbook=RUNBOOKS[AlertName.FNO_HARD_EXIT_TOMORROW],
    )


def late_exit_alert(plan: FoPlan, message: str, at: str) -> Alert:
    """``FNO_LATE_EXIT``: the desk found a structure open past its hard exit (FO7.12)."""
    return Alert(
        name=AlertName.FNO_LATE_EXIT,
        severity=Severity.CRITICAL,
        summary=(
            f"{plan.sleeve} {plan.symbol} {plan.structure} (entry {plan.plan_id}) is past its "
            f"hard exit — LATE_EXIT: {message}. A paper-checklist violation; check the desk's "
            "F&O tab that the exit filled."
        ),
        labels={"sleeve": plan.sleeve, "symbol": plan.symbol, "trade_date": str(plan.trade_date)},
        detail={"plan_id": plan.plan_id, "message": message, "at": at},
        runbook=RUNBOOKS[AlertName.FNO_LATE_EXIT],
    )


def bhavcopy_missing_alert(result: JsonObject) -> Alert | None:
    """``FNO_BHAVCOPY_MISSING`` from a ``run_night`` result, or ``None`` when the day is not
    ``MISSING`` (ingested, pending, skipped)."""
    ingest = result.get("ingest")
    if not isinstance(ingest, dict) or ingest.get("status") != STATUS_MISSING:
        return None
    day = str(ingest.get("trade_date") or result.get("trade_date"))
    return Alert(
        name=AlertName.FNO_BHAVCOPY_MISSING,
        severity=Severity.CRITICAL,
        summary=(
            f"The F&O bhavcopy for {day} is MISSING after the 23:30 attempt: fo_contract_daily "
            "has no rows for it, so no FO mark, F2 trail or scan was written from it. Never "
            "interpolated (docs/fno/04 §4); re-run `fno_cli ingest` once NSE publishes it."
        ),
        labels={"trade_date": day},
        detail={"error": ingest.get("error")},
        runbook=RUNBOOKS[AlertName.FNO_BHAVCOPY_MISSING],
    )


# ------------------------------------------------------------------------------------------------
# The database: today's events for one tenant (read-only)
# ------------------------------------------------------------------------------------------------


def _hard_exit_of(position: FoPosition) -> tuple[dt.date, str] | None:
    if position.hard_exit_date is not None:
        return position.hard_exit_date, "hard exit"
    carry = position.legs.get("carry") if isinstance(position.legs, dict) else None
    time_exit = _day(carry.get("time_exit_date")) if isinstance(carry, dict) else None
    return (time_exit, "time exit") if time_exit is not None else None


def _day_bounds(today: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(today, dt.time(0), tzinfo=IST)
    return start, start + dt.timedelta(days=1)


async def pending_events(session: AsyncSession, user_id: int, now: dt.datetime) -> list[Pending]:
    """Every FO event of ``now``'s IST day for ``user_id``, each with its marker."""
    local = _ist(now)
    today = local.date()
    start, end = _day_bounds(today)
    out: list[Pending] = []

    plans = (
        await session.execute(
            select(FoPlan)
            .where(
                FoPlan.user_id == user_id,
                FoPlan.kind == PlanKind.ENTRY.value,
                FoPlan.issued_at >= start,
                FoPlan.issued_at < end,
            )
            .order_by(FoPlan.issued_at, FoPlan.id)
        )
    ).scalars()
    for plan in plans:
        if not plan.status.startswith("REJECTED"):
            out.append(Pending(f"{MARKER_PREFIX}plan:{user_id}:{plan.plan_id}", plan_alert(plan)))

    journal = (
        await session.execute(
            select(FoJournal)
            .where(FoJournal.user_id == user_id, FoJournal.closed_on == today)
            .order_by(FoJournal.position_id)
        )
    ).scalars()
    for row in journal:
        out.append(Pending(f"{MARKER_PREFIX}exit:{user_id}:{row.position_id}", exit_alert(row)))

    flagged = (
        await session.execute(
            select(FoPlan)
            .where(FoPlan.user_id == user_id, FoPlan.kind == PlanKind.ENTRY.value)
            .where(FoPlan.detail["violations"].isnot(None))
            .order_by(FoPlan.id)
        )
    ).scalars()
    for plan in flagged:
        raw = (plan.detail or {}).get("violations")
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict) or item.get("code") != LATE_EXIT:
                continue
            at = str(item.get("at") or "")
            if _day(at) != today:
                continue
            out.append(
                Pending(
                    f"{MARKER_PREFIX}late:{user_id}:{plan.plan_id}:{at}",
                    late_exit_alert(plan, str(item.get("message") or ""), at),
                )
            )

    if local.time() >= HARD_EXIT_NOTICE_FROM:
        tomorrow = await next_session(session, today)
        if tomorrow is not None:
            open_rows = (
                await session.execute(
                    select(FoPosition)
                    .where(FoPosition.user_id == user_id, FoPosition.closed_at.is_(None))
                    .order_by(FoPosition.id)
                )
            ).scalars()
            for position in open_rows:
                found = _hard_exit_of(position)
                if found is not None and found[0] == tomorrow:
                    out.append(
                        Pending(
                            f"{MARKER_PREFIX}hard:{user_id}:{position.id}:{tomorrow.isoformat()}",
                            hard_exit_tomorrow_alert(position, tomorrow, found[1]),
                        )
                    )
    return out


async def claim(cache: MarkerCache | None, marker: str) -> bool:
    """``SET NX`` the marker: True when this run is the first to see the event, or when Redis
    cannot say (fail open)."""
    if cache is None:
        return True
    try:
        claimed = await cache.set(marker, "1", nx=True, ex=MARKER_TTL_SECONDS)
    except (RedisError, OSError) as exc:
        log.warning("fno alerts: Redis unavailable; delivering without de-duplication: %s", exc)
        return True
    return bool(claimed)


async def run_fno_alerts(  # noqa: PLR0913 - the seams, named
    session: AsyncSession,
    user_ids: Sequence[int],
    cache: MarkerCache | None,
    now: dt.datetime,
    *,
    settings: Settings | None = None,
    mailer: Mailer | None = None,
) -> JsonObject:
    """Raise each of today's unseen FO events once, per tenant. Returns what was sent."""
    sent: list[JsonObject] = []
    seen = 0
    for user_id in user_ids:
        for event in await pending_events(session, user_id, now):
            seen += 1
            if not await claim(cache, event.marker):
                continue
            payload = await dispatch(event.alert, settings, mailer=mailer)
            sent.append({"marker": event.marker, "alert": event.alert.name.value,
                         "delivered_to": payload.get("delivered_to")})  # fmt: skip
    return {"at": now.isoformat(), "events": seen, "sent": sent}


__all__ = [
    "HARD_EXIT_NOTICE_FROM",
    "MARKER_PREFIX",
    "Pending",
    "RedisMarkers",
    "bhavcopy_missing_alert",
    "claim",
    "exit_alert",
    "hard_exit_tomorrow_alert",
    "late_exit_alert",
    "pending_events",
    "plan_alert",
    "run_fno_alerts",
]
