"""The swing book's health facts (SW11, STANDING-ANSWERS B8) — one query each, read twice.

The five alert rules in ``infra/prometheus/alerts.yml`` read gauges ``baskfy_api.metrics``
refreshes at scrape time (the M20 pattern: a durable fact in the database, read by the process
Prometheus scrapes); the four worker checks in ``baskfy_worker.tasks.swing_ops`` raise the same
alerts in-process at the moment each rule is about. Both read the facts from **here**, so a
rule and its check cannot disagree about what "naked" or "open after cutoff" means.

Every reader is over all users — the book has one, and a gauge with a user label would be a
cardinality nobody wants — and every reader is a single indexed query, because the scrape runs
every fifteen seconds forever.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Final

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import (
    PipelineRun,
    SwMarketDaily,
    SwPlan,
    SwPlanLine,
    SwPosition,
    SwSession,
    TradingDay,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID

__all__ = [
    "IST",
    "SwingHealth",
    "detect_ran_for_published_date",
    "is_session_day",
    "monitor_ran_on",
    "naked_positions",
    "open_buy_orders_on",
    "read_swing_health",
]

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST")

#: ``sw_plan_line.kind`` / ``state`` the cutoff rule reads: a BUY the broker accepted and has
#: not completed (A8). After 10:45 the cutoff should have cancelled or closed every one.
BUY_KIND: Final = "BUY_ON_TRIGGER"
SENT_STATE: Final = "SENT"
#: ``sw_position.state`` values with shares on the book.
OPEN_STATES: Final[tuple[str, ...]] = ("OPEN", "PARTIAL")


async def is_session_day(session: AsyncSession, day: dt.date) -> bool:
    """Whether the NSE calendar names ``day`` a trading day. A day the calendar does not
    carry at all is treated as a session — a check must not fall silent because the calendar
    was not loaded that far — and a named holiday is not."""
    row = await session.execute(
        select(TradingDay.is_trading_day).where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID, TradingDay.date == day
        )
    )
    known = row.scalar_one_or_none()
    return True if known is None else bool(known)


async def naked_positions(session: AsyncSession) -> int:
    """Positions with shares open and no resting GTT — `SWING_POSITION_NAKED`'s number."""
    count = await session.execute(
        select(func.count())
        .select_from(SwPosition)
        .where(
            SwPosition.state.in_(OPEN_STATES),
            SwPosition.quantity_open > 0,
            SwPosition.gtt_id.is_(None),
        )
    )
    return int(count.scalar_one())


async def monitor_ran_on(session: AsyncSession, day: dt.date) -> bool:
    """Any ``sw_session`` row for ``day`` with ``monitor_ran`` — written by the monitor on its
    first tick since SW11, so 09:20 reads a fact and not the 10:45 outcome."""
    found = await session.execute(
        select(exists().where(SwSession.session_date == day, SwSession.monitor_ran.is_(True)))
    )
    return bool(found.scalar_one())


async def open_buy_orders_on(session: AsyncSession, day: dt.date) -> int:
    """BUY lines still ``SENT`` on plans of ``day`` — `SWING_ORDER_OPEN_AFTER_CUTOFF`'s number."""
    count = await session.execute(
        select(func.count())
        .select_from(SwPlanLine)
        .join(SwPlan, SwPlan.id == SwPlanLine.plan_id)
        .where(
            SwPlan.as_of == day,
            SwPlanLine.kind == BUY_KIND,
            SwPlanLine.state == SENT_STATE,
        )
    )
    return int(count.scalar_one())


async def published_trade_date(session: AsyncSession) -> dt.date | None:
    """The trade date of the most recently published pipeline run, or None before the first."""
    row = await session.execute(
        select(PipelineRun.trade_date)
        .where(PipelineRun.data_version.is_not(None))
        .order_by(PipelineRun.data_version.desc())
        .limit(1)
    )
    return row.scalar_one_or_none()


async def detect_ran_for_published_date(session: AsyncSession) -> tuple[bool, dt.date | None]:
    """Whether ``sw_market_daily`` carries the published date — the row the detect job writes
    on every run, candidates or none (a day with no setup still has a gate). ``(True, None)``
    when nothing has been published: there is nothing to detect yet, and `publish_late` is
    the alert for that."""
    published = await published_trade_date(session)
    if published is None:
        return True, None
    found = await session.execute(select(exists().where(SwMarketDaily.date == published)))
    return bool(found.scalar_one()), published


@dataclass(frozen=True, slots=True)
class SwingHealth:
    naked_positions: int
    monitor_ran_today: bool
    open_buy_orders_today: int
    detect_ran_for_published_date: bool
    published_date: dt.date | None
    today: dt.date


async def read_swing_health(
    session: AsyncSession, *, now: dt.datetime | None = None
) -> SwingHealth:
    """All five facts, for the scrape."""
    moment = (now or dt.datetime.now(tz=dt.UTC)).astimezone(IST)
    today = moment.date()
    detect_ok, published = await detect_ran_for_published_date(session)
    return SwingHealth(
        naked_positions=await naked_positions(session),
        monitor_ran_today=await monitor_ran_on(session, today),
        open_buy_orders_today=await open_buy_orders_on(session, today),
        detect_ran_for_published_date=detect_ok,
        published_date=published,
        today=today,
    )
