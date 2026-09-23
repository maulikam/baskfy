"""The options book's health, read from the database for the Prometheus rules (OP14).

``docs/options/05`` §4 names five rules: an open position after its hard exit, a gap in the
collector, a stale scan, the limiter share, and no session on a trading day. Each is a fact in a
table the desk or the worker writes, not a counter in whichever process last touched it — the same
reason the swing gauges are read this way (``swing_health``). Read once per scrape; nothing here
writes, and nothing reaches Kite.

What each number means, exactly:

* ``open_after_hard_exit`` — ``op_position`` rows still open (``closed_at`` null) whose sleeve's
  hard exit (``04`` §3.2, §4.5, §5.3) plus two minutes has passed: today's after the time, and any
  earlier day's at all. Should be impossible (``05`` §4 "a loud page").
* ``collector_gap_minutes`` — inside 09:15-15:30 IST, whole minutes since the newest
  ``op_chain_snapshot`` of today (since 09:15 when there is none); 0 outside the session.
* ``scan_stale_minutes`` — the same over ``op_scan``.
* ``limiter_share_pct`` — the collector's quote calls over the last ten minutes (one batch per
  stored minute — OP3 caps a pick at one call) as a percentage of the quote family's budget
  (``KITE_FAMILY_RATE_PER_SECOND["quote"]`` x 60 per minute), rounded up. The desk's own fallback
  polls (at most one per 5 s, and only when ticks stop) are not in any table and are not counted
  (DECISIONS-OP OP14.2).
* ``sessions_today`` — ``op_session`` rows for today, every sleeve: O2 decides every trading day,
  so by 10:20 a trading day with the plan builders running has at least one.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import OpChainSnapshot, OpPosition, OpScan, OpSession
from baskfy_core.options.config import OptionsConfig, Sleeve
from baskfy_providers.factory import KITE_FAMILY_RATE_PER_SECOND, KiteFamily

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))
SESSION_OPEN: Final = dt.time(9, 15)
SESSION_CLOSE: Final = dt.time(15, 30)
#: ``04`` §8.5 / ``05`` §4: the grace after a hard exit before an open position is a page.
HARD_EXIT_GRACE: Final = dt.timedelta(minutes=2)
#: The quote family's budget per minute — the shared limiter's own rate, x 60.
QUOTE_CALLS_PER_MINUTE: Final = KITE_FAMILY_RATE_PER_SECOND[KiteFamily.QUOTE] * 60
SHARE_WINDOW_MINUTES: Final = 10


@dataclass(frozen=True, slots=True)
class OptionsHealth:
    trading_day: bool
    open_after_hard_exit: int
    collector_gap_minutes: int
    scan_stale_minutes: int
    limiter_share_pct: int
    sessions_today: int


def _in_session(now: dt.datetime) -> bool:
    return SESSION_OPEN <= now.time() < SESSION_CLOSE


def _gap(now: dt.datetime, newest: dt.datetime | None) -> int:
    since = (
        newest.astimezone(IST)
        if newest is not None
        else now.replace(
            hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute, second=0, microsecond=0
        )
    )
    return max(0, int((now - since).total_seconds() // 60))


async def read_options_health(
    session: AsyncSession,
    *,
    trading_day: bool,
    now: dt.datetime | None = None,
    options: OptionsConfig | None = None,
) -> OptionsHealth:
    """The five facts at ``now`` (IST). ``trading_day`` is the NSE calendar's answer for today."""
    moment = (now or dt.datetime.now(tz=IST)).astimezone(IST)
    today = moment.date()
    cfg = options or OptionsConfig()
    open_rows = (
        await session.execute(
            select(OpSession.sleeve, OpSession.trade_date)
            .join(OpPosition, OpPosition.session_id == OpSession.id)
            .where(OpPosition.closed_at.is_(None))
        )
    ).all()
    late = 0
    for sleeve, trade_date in open_rows:
        due = dt.datetime.combine(trade_date, cfg.hard_exit_time(Sleeve(sleeve)), IST)
        if moment >= due + HARD_EXIT_GRACE:
            late += 1
    start = dt.datetime.combine(today, SESSION_OPEN, IST)
    collector_gap = scan_stale = 0
    if trading_day and _in_session(moment):
        newest_chain = (
            await session.execute(
                select(func.max(OpChainSnapshot.ts)).where(OpChainSnapshot.ts >= start)
            )
        ).scalar_one_or_none()
        newest_scan = (
            await session.execute(select(func.max(OpScan.ts)).where(OpScan.ts >= start))
        ).scalar_one_or_none()
        collector_gap = _gap(moment, newest_chain)
        scan_stale = _gap(moment, newest_scan)
    window = moment - dt.timedelta(minutes=SHARE_WINDOW_MINUTES)
    calls = (
        await session.execute(
            select(func.count(func.distinct(OpChainSnapshot.ts))).where(
                OpChainSnapshot.ts > window, OpChainSnapshot.ts <= moment
            )
        )
    ).scalar_one()
    per_minute = int(calls or 0) / SHARE_WINDOW_MINUTES
    share = math.ceil(100 * per_minute / QUOTE_CALLS_PER_MINUTE) if per_minute else 0
    sessions = (
        await session.execute(
            select(func.count()).select_from(OpSession).where(OpSession.trade_date == today)
        )
    ).scalar_one()
    return OptionsHealth(
        trading_day=trading_day,
        open_after_hard_exit=late,
        collector_gap_minutes=collector_gap,
        scan_stale_minutes=scan_stale,
        limiter_share_pct=share,
        sessions_today=int(sessions or 0),
    )


__all__ = ["OptionsHealth", "read_options_health"]
