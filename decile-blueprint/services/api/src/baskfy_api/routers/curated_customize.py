"""Customize plan preview — investor weight edits on an investment (leaf 4.7).

``POST /api/v1/cb/investments/{id}/customize`` returns a CUSTOMIZE-kind desk plan while
the cash session is open (synthetic ``cb-sim-…`` desk_plan_id). Outside hours: closed-market
payload. Preview only — never places broker orders.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from baskfy_api.db import SessionDep
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.routers.curated_plans import (
    ClosedMarketOut,
    PlanLegOut,
    _hours_or_closed,
)
from baskfy_core.curated_plans import build_customize_plan
from baskfy_core.market_hours_cb import IST

router = APIRouter(prefix="/cb/investments", tags=["curated-customize"])


class CustomizeBody(BaseModel):
    """Weight-diff inputs. *investment_id* in the path is the scoping key (ledger later)."""

    holdings: dict[str, int]
    target_weights: dict[str, Decimal]
    prices: dict[str, Decimal]
    amount: Decimal = Field(gt=0)


class CustomizePlanOut(BaseModel):
    market_open: Literal[True] = True
    kind: Literal["CUSTOMIZE"] = "CUSTOMIZE"
    status: Literal["PLANNED"] = "PLANNED"
    desk_plan_id: str
    investment_id: int
    legs: list[PlanLegOut]
    requested_amount: Decimal
    expires_at_hint: dt.datetime


def _now() -> dt.datetime:
    return dt.datetime.now(tz=IST)


def _domain_error(exc: ValueError) -> Problem:
    return Problem(ProblemType.INVALID_SCREEN_DEFINITION, str(exc))


@router.post("/{investment_id}/customize", response_model=None)
async def customize_investment(
    investment_id: int,
    body: CustomizeBody,
    session: SessionDep,
) -> CustomizePlanOut | ClosedMarketOut:
    """Return a CUSTOMIZE plan preview from holdings vs target weights. Never executes."""
    if investment_id < 1:
        raise Problem(ProblemType.INVALID_SCREEN_DEFINITION, "investment_id must be >= 1")

    closed = await _hours_or_closed(session)
    if closed is not None:
        return closed

    try:
        plan = build_customize_plan(
            holdings=body.holdings,
            target_weights=body.target_weights,
            prices=body.prices,
            amount=body.amount,
            now=_now(),
        )
    except ValueError as exc:
        raise _domain_error(exc) from exc

    return CustomizePlanOut(
        desk_plan_id=f"cb-sim-{uuid.uuid4()}",
        investment_id=investment_id,
        legs=[PlanLegOut(**leg) for leg in plan["legs"]],
        requested_amount=plan["requested_amount"],
        expires_at_hint=plan["expires_at_hint"],
    )
