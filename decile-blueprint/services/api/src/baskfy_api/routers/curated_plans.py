"""Curated-basket plan previews — invest / apply / exit (SC3).

``POST /api/v1/cb/plans/{invest,apply,exit}`` return desk-shaped plan objects with a
synthetic ``desk_plan_id`` (``cb-sim-{uuid}``) while the cash session is open. Outside
NSE hours the same endpoints return the closed-market payload (200) and never invent a
plan. Preview only: the web layer never hands off to the execution gateway (PACK.2 /
Track C).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.db import SessionDep
from baskfy_api.problems import Problem, ProblemType
from baskfy_core.curated_plans import (
    DeskPlan,
    build_apply_plan,
    build_exit_plan,
    build_invest_plan,
)
from baskfy_core.market_hours_cb import (
    IST,
    closed_market_payload,
    is_nse_session_open,
    next_session_open,
)
from baskfy_core.models import TradingDay

router = APIRouter(prefix="/cb/plans", tags=["curated-plans"])

#: How far ahead of *now* we load trading days for the hours guard.
_CALENDAR_LOOKAHEAD_DAYS = 30
_CALENDAR_LOOKBACK_DAYS = 7

NSE_EXCHANGE_ID = 1


class InvestBody(BaseModel):
    target_weights: dict[str, Decimal]
    prices: dict[str, Decimal]
    amount: Decimal = Field(gt=0)


class ApplyBody(BaseModel):
    holdings: dict[str, int]
    target_weights: dict[str, Decimal]
    prices: dict[str, Decimal]
    amount: Decimal = Field(gt=0)


class ExitBody(BaseModel):
    holdings: dict[str, int]
    prices: dict[str, Decimal]
    requested_amount: Decimal | None = None


class PlanLegOut(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int
    ref_price: Decimal


class PlanPreviewOut(BaseModel):
    market_open: Literal[True] = True
    kind: Literal["BUY", "REBALANCE", "EXIT"]
    status: Literal["PLANNED"] = "PLANNED"
    desk_plan_id: str
    legs: list[PlanLegOut]
    requested_amount: Decimal
    expires_at_hint: dt.datetime


class ClosedMarketOut(BaseModel):
    market_open: Literal[False] = False
    next_open_ist: str


def _now() -> dt.datetime:
    """Seam for tests — production uses wall-clock IST."""
    return dt.datetime.now(tz=IST)


async def load_trading_dates(
    session: AsyncSession,
    *,
    around: dt.date,
) -> set[dt.date]:
    """NSE trading dates near *around* from ``trading_day`` (caller-owned calendar)."""
    start = around - dt.timedelta(days=_CALENDAR_LOOKBACK_DAYS)
    end = around + dt.timedelta(days=_CALENDAR_LOOKAHEAD_DAYS)
    rows = await session.scalars(
        select(TradingDay.date).where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date >= start,
            TradingDay.date <= end,
        )
    )
    return set(rows.all())


def _preview_from_core(plan: DeskPlan) -> PlanPreviewOut:
    return PlanPreviewOut(
        kind=plan["kind"],
        desk_plan_id=f"cb-sim-{uuid.uuid4()}",
        legs=[
            PlanLegOut(
                symbol=leg["symbol"],
                side=leg["side"],
                quantity=leg["quantity"],
                ref_price=leg["ref_price"],
            )
            for leg in plan["legs"]
        ],
        requested_amount=plan["requested_amount"],
        expires_at_hint=plan["expires_at_hint"],
    )


def _domain_error(exc: ValueError) -> Problem:
    return Problem(ProblemType.INVALID_SCREEN_DEFINITION, str(exc))


async def _hours_or_closed(
    session: AsyncSession,
) -> ClosedMarketOut | None:
    """Return a closed-market body when outside session; ``None`` when open."""
    now = _now()
    trading_dates = await load_trading_dates(session, around=now.astimezone(IST).date())
    if is_nse_session_open(now, trading_dates):
        return None
    try:
        nxt = next_session_open(now, trading_dates)
    except ValueError as exc:
        raise Problem(ProblemType.PIPELINE_DEGRADED, str(exc)) from exc
    payload = closed_market_payload(nxt)
    return ClosedMarketOut(next_open_ist=str(payload["next_open_ist"]))


@router.post("/invest", response_model=None)
async def plan_invest(body: InvestBody, session: SessionDep) -> PlanPreviewOut | ClosedMarketOut:
    closed = await _hours_or_closed(session)
    if closed is not None:
        return closed
    try:
        plan = build_invest_plan(
            target_weights=body.target_weights,
            prices=body.prices,
            amount=body.amount,
            now=_now(),
        )
    except ValueError as exc:
        raise _domain_error(exc) from exc
    return _preview_from_core(plan)


@router.post("/apply", response_model=None)
async def plan_apply(body: ApplyBody, session: SessionDep) -> PlanPreviewOut | ClosedMarketOut:
    closed = await _hours_or_closed(session)
    if closed is not None:
        return closed
    try:
        plan = build_apply_plan(
            holdings=body.holdings,
            target_weights=body.target_weights,
            prices=body.prices,
            amount=body.amount,
            now=_now(),
        )
    except ValueError as exc:
        raise _domain_error(exc) from exc
    return _preview_from_core(plan)


@router.post("/exit", response_model=None)
async def plan_exit(body: ExitBody, session: SessionDep) -> PlanPreviewOut | ClosedMarketOut:
    closed = await _hours_or_closed(session)
    if closed is not None:
        return closed
    try:
        plan = build_exit_plan(
            holdings=body.holdings,
            prices=body.prices,
            now=_now(),
            requested_amount=body.requested_amount,
        )
    except ValueError as exc:
        raise _domain_error(exc) from exc
    return _preview_from_core(plan)
