"""The options book's in-process checks, raised the moment each is about (OP14).

``docs/options/06`` OP14: "in-process checks at 09:20 (sessions exist), 10:20 (O1 verdict or plan on
an O1 day), each hard exit + 3 min (nothing open), 15:35 (collector wrote >= 360 minutes)". The
Prometheus rules (``infra/prometheus/alerts.yml`` ``baskfy-options``) watch the same facts from the
outside; these run inside the worker at the minute that matters, so a failure is an alert even on a
box whose Prometheus is not deployed — the swing and VBT books' convention.

Each check reads rows and returns a :class:`CheckResult`; a failed one becomes one
``OPTIONS_CHECK_FAILED`` alert naming the check. A check whose switch is off is ``skipped``, never a
failure: with the scan flag off there is no scan to have run.

* ``SCAN_STARTED`` (09:20) — "sessions exist": every sleeve's scan row for today has been written
  since 09:15, which is where each sleeve's day (its role, its state) begins (DECISIONS-OP OP14.1).
  Needs the scan flag.
* ``O1_DECIDED`` (10:20) — on a day the calendar gives O1-M or O1-W a role, that sleeve's
  ``op_session`` exists: the verdict (``SKIPPED``) or the plan. Needs the monitor flag (the plan
  builders are dark behind it).
* ``FLAT_AFTER_HARD_EXIT`` (each hard exit + 3 min) — no ``op_position`` of a sleeve whose hard exit
  has passed is still open, today or any earlier day. Always checked: an open position is wrong
  whatever the flags say.
* ``COLLECTOR_FULL_DAY`` (15:35) — today has at least 360 distinct ``op_chain_snapshot`` minutes of
  the session's 375. Needs the collect flag.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import OpChainSnapshot, OpEventDay, OpPosition, OpScan, OpSession
from baskfy_core.models.base import JsonObject
from baskfy_core.options.calendar import role
from baskfy_core.options.config import OptionsConfig, Sleeve
from baskfy_worker.alerts import Alert, AlertName, Severity
from baskfy_worker.ops import RUNBOOKS
from baskfy_worker.options.index_bars import IST
from baskfy_worker.options.master import load_contracts

SESSION_OPEN: Final = dt.time(9, 15)
SESSION_CLOSE: Final = dt.time(15, 30)
SCAN_STARTED_AT: Final = dt.time(9, 20)
O1_DECIDED_AT: Final = dt.time(10, 20)
COLLECTOR_CHECKED_AT: Final = dt.time(15, 35)
HARD_EXIT_GRACE: Final = dt.timedelta(minutes=3)
COLLECTOR_MIN_MINUTES: Final = 360
RUNBOOK: Final = RUNBOOKS[AlertName.OPTIONS_CHECK_FAILED]


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    skipped: bool = False

    def as_dict(self) -> JsonObject:
        return {"name": self.name, "ok": self.ok, "skipped": self.skipped, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Switches:
    scan_enabled: bool
    collect_enabled: bool
    monitor_enabled: bool


def hard_exit_checks(options: OptionsConfig) -> tuple[dt.time, ...]:
    """Each distinct hard exit + 3 minutes: the moments ``FLAT_AFTER_HARD_EXIT`` runs."""
    times = {options.hard_exit_time(s) for s in Sleeve}
    return tuple(
        sorted(
            (dt.datetime.combine(dt.date(2000, 1, 3), t) + HARD_EXIT_GRACE).time() for t in times
        )
    )


def due_checks(now: dt.datetime, options: OptionsConfig) -> tuple[str, ...]:
    """The checks whose minute ``now`` is (IST, to the minute)."""
    minute = now.astimezone(IST).time().replace(second=0, microsecond=0)
    due: list[str] = []
    if minute == SCAN_STARTED_AT:
        due.append("SCAN_STARTED")
    if minute == O1_DECIDED_AT:
        due.append("O1_DECIDED")
    if minute in hard_exit_checks(options):
        due.append("FLAT_AFTER_HARD_EXIT")
    if minute == COLLECTOR_CHECKED_AT:
        due.append("COLLECTOR_FULL_DAY")
    return tuple(due)


async def scan_started(session: AsyncSession, now: dt.datetime, switches: Switches) -> CheckResult:
    if not switches.scan_enabled:
        return CheckResult("SCAN_STARTED", True, "BASKFY_OPTIONS_SCAN_ENABLED is false", True)
    start = dt.datetime.combine(now.astimezone(IST).date(), SESSION_OPEN, IST)
    seen = set(
        (await session.execute(select(OpScan.sleeve).where(OpScan.ts >= start).distinct()))
        .scalars()
        .all()
    )
    missing = sorted(s.value for s in Sleeve if s.value not in seen)
    if missing:
        return CheckResult(
            "SCAN_STARTED", False, f"no scan row since 09:15 for {', '.join(missing)}"
        )
    return CheckResult("SCAN_STARTED", True, "every sleeve has a scan row today")


async def o1_decided(
    session: AsyncSession, now: dt.datetime, switches: Switches, options: OptionsConfig
) -> CheckResult:
    if not switches.monitor_enabled:
        return CheckResult("O1_DECIDED", True, "BASKFY_OPTIONS_MONITOR_ENABLED is false", True)
    day = now.astimezone(IST).date()
    contracts = await load_contracts(session, options.calendar.underlying)
    events = (await session.execute(select(OpEventDay.date))).scalars().all()
    o1_today = [
        s
        for s in (Sleeve.O1M, Sleeve.O1W)
        if role(day, s, rows=contracts, event_days=events, trading_day=True).trades
    ]
    if not o1_today:
        return CheckResult("O1_DECIDED", True, "not an O1 day", True)
    decided = set(
        (
            await session.execute(
                select(OpSession.sleeve).where(
                    OpSession.trade_date == day,
                    OpSession.sleeve.in_([s.value for s in o1_today]),
                )
            )
        )
        .scalars()
        .all()
    )
    missing = [s.value for s in o1_today if s.value not in decided]
    if missing:
        return CheckResult("O1_DECIDED", False, f"no verdict or plan by 10:20 for {missing[0]}")
    return CheckResult("O1_DECIDED", True, f"{', '.join(s.value for s in o1_today)} decided")


async def flat_after_hard_exit(
    session: AsyncSession, now: dt.datetime, options: OptionsConfig
) -> CheckResult:
    moment = now.astimezone(IST)
    rows = (
        await session.execute(
            select(OpSession.sleeve, OpSession.trade_date, OpSession.plan_id)
            .join(OpPosition, OpPosition.session_id == OpSession.id)
            .where(OpPosition.closed_at.is_(None))
        )
    ).all()
    late = [
        f"{sleeve} {plan_id}"
        for sleeve, trade_date, plan_id in rows
        if moment
        >= dt.datetime.combine(trade_date, options.hard_exit_time(Sleeve(sleeve)), IST)
        + HARD_EXIT_GRACE
    ]
    if late:
        return CheckResult("FLAT_AFTER_HARD_EXIT", False, f"still open: {'; '.join(late)}")
    return CheckResult("FLAT_AFTER_HARD_EXIT", True, "nothing open past its hard exit")


async def collector_full_day(
    session: AsyncSession, now: dt.datetime, switches: Switches
) -> CheckResult:
    if not switches.collect_enabled:
        return CheckResult(
            "COLLECTOR_FULL_DAY", True, "BASKFY_OPTIONS_COLLECT_ENABLED is false", True
        )
    day = now.astimezone(IST).date()
    start = dt.datetime.combine(day, SESSION_OPEN, IST)
    end = dt.datetime.combine(day, SESSION_CLOSE, IST)
    minutes = (
        await session.execute(
            select(func.count(func.distinct(OpChainSnapshot.ts))).where(
                OpChainSnapshot.ts >= start, OpChainSnapshot.ts < end
            )
        )
    ).scalar_one()
    count = int(minutes or 0)
    if count < COLLECTOR_MIN_MINUTES:
        return CheckResult(
            "COLLECTOR_FULL_DAY", False, f"{count} chain minutes today, fewer than 360 of 375"
        )
    return CheckResult("COLLECTOR_FULL_DAY", True, f"{count} chain minutes today")


async def run_checks(
    session: AsyncSession,
    names: tuple[str, ...],
    now: dt.datetime,
    switches: Switches,
    options: OptionsConfig | None = None,
) -> list[CheckResult]:
    """Run ``names`` at ``now``. The caller has already asked the NSE calendar: not on a holiday."""
    cfg = options or OptionsConfig()
    out: list[CheckResult] = []
    for name in names:
        if name == "SCAN_STARTED":
            out.append(await scan_started(session, now, switches))
        elif name == "O1_DECIDED":
            out.append(await o1_decided(session, now, switches, cfg))
        elif name == "FLAT_AFTER_HARD_EXIT":
            out.append(await flat_after_hard_exit(session, now, cfg))
        elif name == "COLLECTOR_FULL_DAY":
            out.append(await collector_full_day(session, now, switches))
        else:
            raise ValueError(f"unknown options check {name!r}")
    return out


def check_alert(result: CheckResult, now: dt.datetime) -> Alert:
    """One failed check as ``OPTIONS_CHECK_FAILED``."""
    critical = result.name == "FLAT_AFTER_HARD_EXIT"
    at = now.astimezone(IST)
    return Alert(
        name=AlertName.OPTIONS_CHECK_FAILED,
        severity=Severity.CRITICAL if critical else Severity.WARNING,
        summary=f"Options check {result.name} failed at {at:%H:%M}: {result.detail}",
        runbook=RUNBOOK,
        labels={"check": result.name},
    )


__all__ = [
    "CheckResult",
    "Switches",
    "check_alert",
    "due_checks",
    "hard_exit_checks",
    "run_checks",
]
