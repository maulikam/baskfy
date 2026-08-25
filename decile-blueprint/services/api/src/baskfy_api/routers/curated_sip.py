"""SIP reminder plans on an investment (T8.4). REMINDER only — AUTO is refused.

``POST /cb/investments/{id}/sip`` writes ``cb_sip_plan`` so Beat ``baskfy.cb.sip_reminders``
has rows to fire. No auto-debit, no order path.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.curated_investments import load_investment_for_user
from baskfy_api.db import SessionDep
from baskfy_api.problems import Problem, ProblemType, not_found
from baskfy_core.curated_sip import (
    SIP_MODE_REMINDER,
    SIP_STATUS_ACTIVE,
    assert_reminder_mode,
    next_fire_date,
)
from baskfy_core.models import CbInvestment, CbSipPlan, TradingDay

router = APIRouter(tags=["curated-sip"])

_NSE_EXCHANGE_ID = 1
_CALENDAR_LOOKAHEAD_DAYS = 90


class SipBody(BaseModel):
    amount: Decimal = Field(gt=0)
    day_of_month: int = Field(ge=1, le=28)
    mode: str = SIP_MODE_REMINDER


class SipOut(BaseModel):
    id: int
    mode: str
    status: str
    amount: Decimal
    day_of_month: int
    next_fire_date: dt.date


async def _trading_dates(session: AsyncSession, around: dt.date) -> set[dt.date]:
    end = around + dt.timedelta(days=_CALENDAR_LOOKAHEAD_DAYS)
    rows = await session.scalars(
        select(TradingDay.date).where(
            TradingDay.exchange_id == _NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date >= around,
            TradingDay.date <= end,
        )
    )
    return set(rows.all())


def _out(plan: CbSipPlan) -> SipOut:
    return SipOut(
        id=int(plan.id),
        mode=str(plan.mode),
        status=str(plan.status),
        amount=plan.amount,
        day_of_month=int(plan.day_of_month),
        next_fire_date=plan.next_fire_date,
    )


async def persist_sip_plan(
    session: AsyncSession,
    inv: CbInvestment,
    body: SipBody,
    *,
    as_of: dt.date,
    trading_dates: set[dt.date],
) -> SipOut:
    """Write one ACTIVE REMINDER plan. Used by the route and by tests."""
    try:
        assert_reminder_mode(body.mode)
    except ValueError as exc:
        raise Problem(ProblemType.INVALID_SCREEN_DEFINITION, str(exc)) from exc

    existing = await session.scalar(
        select(CbSipPlan.id).where(
            CbSipPlan.investment_id == inv.id,
            CbSipPlan.status == SIP_STATUS_ACTIVE,
        )
    )
    if existing is not None:
        raise Problem(
            ProblemType.STALE_DATA_VERSION,
            "an ACTIVE SIP reminder already exists for this investment",
            sent_data_version=int(existing),
            current_data_version=int(existing),
        )
    if not trading_dates:
        raise Problem(
            ProblemType.NO_TRADING_DAY,
            "no trading days loaded to schedule the next SIP reminder",
        )
    try:
        fire = next_fire_date(
            day_of_month=body.day_of_month, as_of=as_of, trading_dates=trading_dates
        )
    except ValueError as exc:
        raise Problem(ProblemType.INVALID_SCREEN_DEFINITION, str(exc)) from exc

    plan = CbSipPlan(
        investment_id=int(inv.id),
        amount=body.amount,
        day_of_month=body.day_of_month,
        mode=SIP_MODE_REMINDER,
        status=SIP_STATUS_ACTIVE,
        next_fire_date=fire,
    )
    session.add(plan)
    await session.flush()
    return _out(plan)


@router.post("/cb/investments/{investment_id}/sip", response_model=SipOut, status_code=201)
async def create_sip_plan(
    investment_id: int,
    body: SipBody,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> SipOut:
    """Persist an ACTIVE REMINDER plan. AUTO is refused."""
    try:
        assert_reminder_mode(body.mode)
    except ValueError as exc:
        raise Problem(ProblemType.INVALID_SCREEN_DEFINITION, str(exc)) from exc

    inv = await load_investment_for_user(
        session, principal_user_id=principal.user_id, investment_id=investment_id
    )
    today = dt.datetime.now(tz=dt.UTC).date()
    dates = await _trading_dates(session, today)
    return await persist_sip_plan(session, inv, body, as_of=today, trading_dates=dates)


@router.get("/cb/investments/{investment_id}/sip", response_model=SipOut)
async def get_sip_plan(
    investment_id: int,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> SipOut:
    inv = await load_investment_for_user(
        session, principal_user_id=principal.user_id, investment_id=investment_id
    )
    plan = await session.scalar(
        select(CbSipPlan)
        .where(CbSipPlan.investment_id == inv.id)
        .order_by(CbSipPlan.id.desc())
        .limit(1)
    )
    if plan is None:
        raise not_found("sip plan", str(investment_id))
    return _out(plan)
