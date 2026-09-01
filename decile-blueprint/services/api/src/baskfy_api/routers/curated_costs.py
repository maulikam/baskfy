"""Costs and returns after accrued (uncollected) fees (T8.5). Collection stays off."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.curated_investments import load_investment_for_user, snapshot_dict
from baskfy_api.db import SessionDep
from baskfy_core.curated_accounting import HoldingPosition
from baskfy_core.gst import money
from baskfy_core.models import CbFeeLedger, CbInvestmentHolding, CbOrderBatch

router = APIRouter(tags=["curated-costs"])

UTC = dt.UTC


class CostsSnapshotOut(BaseModel):
    money_put_in: Decimal
    current_investment: Decimal
    current_value: Decimal
    current_returns: Decimal
    current_returns_pct: Decimal
    realized_pnl: Decimal
    dividends: Decimal
    xirr: Decimal | None = None
    xirr_displayable: bool


class CostsOut(BaseModel):
    snapshot: CostsSnapshotOut
    accrued_fees_total: Decimal
    returns_after_fees: Decimal
    collected: bool = False


@router.get("/cb/investments/{investment_id}/costs", response_model=CostsOut)
async def get_investment_costs(
    investment_id: int,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> CostsOut:
    """Snapshot plus accrued platform fees. Does not collect."""
    inv = await load_investment_for_user(
        session, principal_user_id=principal.user_id, investment_id=investment_id
    )
    now = dt.datetime.now(tz=UTC)
    holding_rows = list(
        (
            await session.scalars(
                select(CbInvestmentHolding).where(CbInvestmentHolding.investment_id == inv.id)
            )
        ).all()
    )
    batches = list(
        (
            await session.scalars(select(CbOrderBatch).where(CbOrderBatch.investment_id == inv.id))
        ).all()
    )
    buy_amounts = [
        b.requested_amount
        for b in batches
        if b.kind in ("BUY", "INVEST_MORE", "SIP") and b.requested_amount is not None
    ]
    positions = [
        HoldingPosition(instrument_id=int(h.instrument_id), qty=h.qty, avg_price=h.avg_price)
        for h in holding_rows
    ]
    prices = {int(h.instrument_id): h.avg_price for h in holding_rows}
    first = (inv.created_at or now).date()
    raw = snapshot_dict(
        buy_amounts=buy_amounts or [Decimal("0")],
        holdings=positions,
        prices=prices,
        first_invested=first,
        as_of=now.date(),
    )
    batch_ids = [int(b.id) for b in batches]
    fee_rows = []
    if batch_ids:
        fee_rows = list(
            (
                await session.scalars(
                    select(CbFeeLedger).where(CbFeeLedger.batch_id.in_(batch_ids))
                )
            ).all()
        )
    accrued = money(sum((row.total for row in fee_rows), Decimal("0")))
    returns = Decimal(str(raw["current_returns"]))
    return CostsOut(
        snapshot=CostsSnapshotOut.model_validate(raw),
        accrued_fees_total=accrued,
        returns_after_fees=money(returns - accrued),
        collected=False,
    )
