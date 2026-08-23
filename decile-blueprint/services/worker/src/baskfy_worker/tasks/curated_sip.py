"""Celery Beat SIP REMINDER fires — docs/smallcase/04 §9, SC7 / leaf 2.1.

Raises ``SIP_DUE`` pending actions for ACTIVE REMINDER plans due on *as_of*. Pure calendar
math lives in ``baskfy_core.curated_sip``; this module only loads rows and persists. No
broker path; REMINDER only.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.curated_sip import (
    SIP_DUE,
    SIP_MODE_REMINDER,
    SIP_STATUS_ACTIVE,
    evaluate_plan_for_fire,
)
from baskfy_core.models import CbInvestment, CbPendingAction, CbSipPlan, TradingDay
from baskfy_core.models.base import JsonObject

#: NSE equity exchange id — same constant curated plan hours use.
_NSE_EXCHANGE_ID = 1
_CALENDAR_LOOKBACK_DAYS = 14
_CALENDAR_LOOKAHEAD_DAYS = 90


async def _load_trading_dates(session: AsyncSession, around: dt.date) -> set[dt.date]:
    start = around - dt.timedelta(days=_CALENDAR_LOOKBACK_DAYS)
    end = around + dt.timedelta(days=_CALENDAR_LOOKAHEAD_DAYS)
    rows = await session.scalars(
        select(TradingDay.date).where(
            TradingDay.exchange_id == _NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date >= start,
            TradingDay.date <= end,
        )
    )
    return set(rows.all())


async def _already_fired_keys(session: AsyncSession) -> set[str]:
    """Fire keys already consumed (idempotent per plan_id:YYYY-MM)."""
    rows = await session.scalars(
        select(CbPendingAction.payload).where(CbPendingAction.type == SIP_DUE)
    )
    keys: set[str] = set()
    for payload in rows.all():
        if isinstance(payload, dict):
            raw = payload.get("fire_key")
            if isinstance(raw, str) and raw:
                keys.add(raw)
    return keys


def _plan_mapping(plan: CbSipPlan, user_id: int) -> dict[str, object]:
    return {
        "id": plan.id,
        "investment_id": plan.investment_id,
        "user_id": user_id,
        "amount": plan.amount,
        "day_of_month": plan.day_of_month,
        "mode": plan.mode,
        "status": plan.status,
        "next_fire_date": plan.next_fire_date,
    }


async def run_curated_sip_reminders(
    session: AsyncSession,
    as_of: dt.date,
    *,
    trading_dates: set[dt.date] | None = None,
) -> JsonObject:
    """Create pending ``SIP_DUE`` rows for ACTIVE REMINDER plans due on *as_of*.

    Idempotent per ``(plan_id, YYYY-MM)`` via ``fire_key`` in the pending payload. Non-REMINDER
    modes are never selected. Returns counts only — never opens a broker path.
    """
    dates = trading_dates if trading_dates is not None else await _load_trading_dates(session, as_of)
    if not dates:
        return {
            "as_of": as_of.isoformat(),
            "examined": 0,
            "raised": 0,
            "skipped": 0,
            "reason": "no_trading_dates",
        }

    fired_keys = await _already_fired_keys(session)
    plans = list(
        (
            await session.scalars(
                select(CbSipPlan).where(
                    CbSipPlan.status == SIP_STATUS_ACTIVE,
                    CbSipPlan.mode == SIP_MODE_REMINDER,
                )
            )
        ).all()
    )

    raised = 0
    skipped = 0
    for plan in plans:
        investment = await session.get(CbInvestment, plan.investment_id)
        if investment is None:
            skipped += 1
            continue
        action, advanced = evaluate_plan_for_fire(
            _plan_mapping(plan, int(investment.user_id)),
            as_of=as_of,
            trading_dates=dates,
            already_fired_keys=fired_keys,
        )
        if action is None or advanced is None:
            skipped += 1
            continue
        payload: dict[str, Any] = dict(action["payload"])
        session.add(
            CbPendingAction(
                user_id=int(action["user_id"]),
                type=str(action["type"]),
                payload=payload,
            )
        )
        plan.next_fire_date = advanced
        fire_key = payload.get("fire_key")
        if isinstance(fire_key, str):
            fired_keys.add(fire_key)
        raised += 1

    await session.flush()
    return {
        "as_of": as_of.isoformat(),
        "examined": len(plans),
        "raised": raised,
        "skipped": skipped,
    }
