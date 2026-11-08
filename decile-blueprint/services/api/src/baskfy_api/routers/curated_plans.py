"""Sole-operator curated plan previews and desk-console handoffs.

Every route here creates intent only.  There is deliberately no confirm, execute, submit, broker,
or order-placement handler.  The returned ``desk_plan_id`` is reviewed in the desk console.
"""

from __future__ import annotations

from collections.abc import Awaitable
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from baskfy_api.auth import settings_for
from baskfy_api.curated_plans import (
    CuratedPlanConflictError,
    CuratedPlanInputError,
    CuratedPlanNotFoundError,
    MarketClosedResult,
    PlanCreatedResult,
    PlanGenerationResult,
    PlanJournalUnavailableError,
    create_apply_plan,
    create_exit_plan,
    create_invest_more_plan,
    create_invest_plan,
    create_partial_exit_plan,
)
from baskfy_api.db import SessionDep
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.routers.curated_catalog import SoleOperatorDep, _json
from baskfy_core.curated_rebalance import PlanPreview

router = APIRouter(tags=["curated-plans"])

InvestmentId = Annotated[int, Path(gt=0)]
BasketSlug = Annotated[
    str,
    Path(min_length=1, max_length=160, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
]


class AmountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=2)


class ApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: int = Field(gt=0)
    cash_contribution: Decimal = Field(default=Decimal("0"), ge=0, max_digits=20, decimal_places=2)


class PlanLineOut(BaseModel):
    instrument_id: int
    side: Literal["BUY", "SELL"]
    quantity: int
    price: Decimal
    notional: Decimal


class ResultingHoldingOut(BaseModel):
    instrument_id: int
    quantity: Decimal


class PlanPreviewOut(BaseModel):
    kind: Literal["BUY", "INVEST_MORE", "REBALANCE", "EXIT", "PARTIAL_EXIT"]
    lines: list[PlanLineOut]
    resulting_holdings: list[ResultingHoldingOut]
    requested_amount: Decimal
    minimum_amount: Decimal
    current_value: Decimal
    sell_proceeds: Decimal
    buy_cost: Decimal
    residual_cash: Decimal
    top_up_amount: Decimal
    unallocated_amount: Decimal
    executable: bool


class PlanCreatedOut(BaseModel):
    state: Literal["PLANNED"] = "PLANNED"
    batch_id: int
    investment_id: int
    desk_plan_id: str
    created_at: str
    expires_at: str
    handoff: Literal["DESK_CONSOLE"] = "DESK_CONSOLE"
    preview: PlanPreviewOut


class MarketClosedOut(BaseModel):
    state: Literal["MARKET_CLOSED"] = "MARKET_CLOSED"
    observed_at_ist: str
    next_open_at: str | None
    reason: Literal["MARKET_CLOSED", "CALENDAR_UNAVAILABLE"]


PlanOutcomeOut = PlanCreatedOut | MarketClosedOut


@router.post(
    "/catalog/{basket_slug}/plans/invest",
    response_model=PlanOutcomeOut,
    summary="Create an initial curated-basket plan for desk review",
)
async def plan_invest(
    basket_slug: BasketSlug,
    body: AmountIn,
    request: Request,
    session: SessionDep,
    principal: SoleOperatorDep,
) -> Response:
    settings = settings_for(request)
    return await _outcome(
        create_invest_plan(
            session,
            user_id=principal.require_user(),
            broker_account_id=settings.sole_broker_account_id,
            basket_slug=basket_slug,
            requested_amount=body.amount,
        )
    )


@router.post(
    "/investments/{investment_id}/plans/invest-more",
    response_model=PlanOutcomeOut,
    summary="Create an invest-more plan for desk review",
)
async def plan_invest_more(
    investment_id: InvestmentId,
    body: AmountIn,
    request: Request,
    session: SessionDep,
    principal: SoleOperatorDep,
) -> Response:
    return await _outcome(
        create_invest_more_plan(
            session,
            user_id=principal.require_user(),
            broker_account_id=settings_for(request).sole_broker_account_id,
            investment_id=investment_id,
            requested_amount=body.amount,
        )
    )


@router.post(
    "/investments/{investment_id}/plans/apply",
    response_model=PlanOutcomeOut,
    summary="Create an apply-rebalance plan for desk review",
)
async def plan_apply(
    investment_id: InvestmentId,
    body: ApplyIn,
    request: Request,
    session: SessionDep,
    principal: SoleOperatorDep,
) -> Response:
    return await _outcome(
        create_apply_plan(
            session,
            user_id=principal.require_user(),
            broker_account_id=settings_for(request).sole_broker_account_id,
            investment_id=investment_id,
            version_id=body.version_id,
            cash_contribution=body.cash_contribution,
        )
    )


@router.post(
    "/investments/{investment_id}/plans/exit",
    response_model=PlanOutcomeOut,
    summary="Create a full-exit plan for desk review",
)
async def plan_exit(
    investment_id: InvestmentId,
    request: Request,
    session: SessionDep,
    principal: SoleOperatorDep,
) -> Response:
    return await _outcome(
        create_exit_plan(
            session,
            user_id=principal.require_user(),
            broker_account_id=settings_for(request).sole_broker_account_id,
            investment_id=investment_id,
        )
    )


@router.post(
    "/investments/{investment_id}/plans/partial-exit",
    response_model=PlanOutcomeOut,
    summary="Create a partial-exit plan for desk review",
)
async def plan_partial_exit(
    investment_id: InvestmentId,
    body: AmountIn,
    request: Request,
    session: SessionDep,
    principal: SoleOperatorDep,
) -> Response:
    return await _outcome(
        create_partial_exit_plan(
            session,
            user_id=principal.require_user(),
            broker_account_id=settings_for(request).sole_broker_account_id,
            investment_id=investment_id,
            requested_amount=body.amount,
        )
    )


async def _outcome(pending: Awaitable[PlanGenerationResult]) -> Response:
    try:
        result = await pending
    except CuratedPlanNotFoundError as exc:
        raise Problem(ProblemType.NOT_FOUND, str(exc)) from exc
    except CuratedPlanConflictError as exc:
        raise Problem(ProblemType.STALE_DATA_VERSION, str(exc)) from exc
    except CuratedPlanInputError as exc:
        raise Problem(ProblemType.INVALID_SCREEN_DEFINITION, str(exc)) from exc
    except PlanJournalUnavailableError as exc:
        raise Problem(ProblemType.PIPELINE_DEGRADED, str(exc)) from exc
    if isinstance(result, MarketClosedResult):
        return _json(
            MarketClosedOut(
                observed_at_ist=result.observed_at_ist.isoformat(),
                next_open_at=(
                    result.next_open_at.isoformat() if result.next_open_at is not None else None
                ),
                reason=result.reason,
            )
        )
    return _json(_planned(result))


def _planned(result: PlanCreatedResult) -> PlanCreatedOut:
    return PlanCreatedOut(
        batch_id=result.batch_id,
        investment_id=result.investment_id,
        desk_plan_id=result.desk_plan_id,
        created_at=result.created_at.isoformat(),
        expires_at=result.expires_at.isoformat(),
        preview=_preview(result.preview),
    )


def _preview(preview: PlanPreview) -> PlanPreviewOut:
    return PlanPreviewOut(
        kind=preview.kind.value,
        lines=[
            PlanLineOut(
                instrument_id=line.instrument_id,
                side=line.side.value,
                quantity=line.quantity,
                price=line.price,
                notional=line.notional,
            )
            for line in preview.lines
        ],
        resulting_holdings=[
            ResultingHoldingOut(
                instrument_id=holding.instrument_id,
                quantity=holding.quantity,
            )
            for holding in preview.resulting_holdings
        ],
        requested_amount=preview.requested_amount,
        minimum_amount=preview.minimum_amount,
        current_value=preview.current_value,
        sell_proceeds=preview.sell_proceeds,
        buy_cost=preview.buy_cost,
        residual_cash=preview.residual_cash,
        top_up_amount=preview.top_up_amount,
        unallocated_amount=preview.unallocated_amount,
        executable=preview.executable,
    )
