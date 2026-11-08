"""Drift scan and ledger rebase (T8.6). Compares intended holdings to broker qty.

Scan may raise a ``DRIFT`` pending action. Fix rebases ``cb_investment_holding`` to broker
qty and resolves the pending row. No order path.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.curated_investments import load_investment_for_user
from baskfy_api.db import SessionDep
from baskfy_api.problems import Problem, ProblemType
from baskfy_core.curated_drift import (
    DRIFT_ACTION_TYPE,
    BrokerHolding,
    LedgerHolding,
    detect_drift,
    rebase_holdings_after_drift,
)
from baskfy_core.models import CbInvestmentHolding, CbPendingAction, Instrument

router = APIRouter(tags=["curated-drift"])

DESK_SCHEMA = "desk"
UTC = dt.UTC


class BrokerHoldingIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    qty: Decimal = Field(ge=0)


class DriftScanBody(BaseModel):
    broker_holdings: list[BrokerHoldingIn] | None = None


class DriftDeltaOut(BaseModel):
    symbol: str
    ledger_qty: Decimal
    broker_qty: Decimal
    shortfall: Decimal
    excess: Decimal


class DriftScanOut(BaseModel):
    action_type: str | None
    deltas: list[DriftDeltaOut]
    pending_action_id: int | None = None


class DriftFixOut(BaseModel):
    holdings: list[BrokerHoldingIn]
    cleared_action: bool
    synthetic_exit_count: int


async def _instruments_by_symbol(
    session: AsyncSession, symbols: list[str]
) -> dict[str, Instrument]:
    if not symbols:
        return {}
    rows = list(
        (
            await session.scalars(
                select(Instrument).where(
                    Instrument.symbol.in_(symbols),
                    Instrument.is_active.is_(True),
                )
            )
        ).all()
    )
    return {str(row.symbol).upper(): row for row in rows}


async def _desk_holdings(session: AsyncSession) -> list[BrokerHoldingIn]:
    try:
        row = (
            (
                await session.execute(
                    text(
                        f'select holdings_json from "{DESK_SCHEMA}".snapshots '
                        "order by date desc limit 1"
                    )
                )
            )
            .mappings()
            .first()
        )
    except ProgrammingError:
        # Desk schema absent — a normal state for an API-only database.
        return []
    except SQLAlchemyError:
        # An aborted transaction must not be continued (audit 4.15). Roll back and stop.
        await session.rollback()
        return []
    if row is None:
        return []
    raw = row["holdings_json"]
    if not isinstance(raw, list):
        return []
    out: list[BrokerHoldingIn] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip()
        if not symbol:
            continue
        qty = Decimal(str(item.get("quantity") or item.get("qty") or 0))
        out.append(BrokerHoldingIn(symbol=symbol, qty=qty))
    return out


async def _resolve_broker(
    session: AsyncSession, body: DriftScanBody
) -> tuple[list[BrokerHolding], dict[int, str]]:
    posted = body.broker_holdings
    lots = posted if posted else await _desk_holdings(session)
    if not lots:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            "declare broker_holdings or sync a desk holdings snapshot first",
        )
    symbols = [lot.symbol.strip().upper() for lot in lots]
    by_symbol = await _instruments_by_symbol(session, symbols)
    missing = [sym for sym in symbols if sym not in by_symbol]
    if missing:
        raise Problem(ProblemType.NOT_FOUND, f"unknown symbols: {', '.join(missing)}")
    holdings: list[BrokerHolding] = []
    id_to_symbol: dict[int, str] = {}
    for lot in lots:
        inst = by_symbol[lot.symbol.strip().upper()]
        holdings.append(BrokerHolding(instrument_id=int(inst.id), qty=lot.qty))
        id_to_symbol[int(inst.id)] = str(inst.symbol)
    return holdings, id_to_symbol


async def _ledger(
    session: AsyncSession, investment_id: int
) -> tuple[list[LedgerHolding], dict[int, Decimal], dict[int, str]]:
    rows = list(
        (
            await session.scalars(
                select(CbInvestmentHolding).where(
                    CbInvestmentHolding.investment_id == investment_id
                )
            )
        ).all()
    )
    if not rows:
        return [], {}, {}
    instruments = list(
        (
            await session.scalars(
                select(Instrument).where(Instrument.id.in_([int(h.instrument_id) for h in rows]))
            )
        ).all()
    )
    symbols = {int(i.id): str(i.symbol) for i in instruments}
    ledger = [
        LedgerHolding(instrument_id=int(h.instrument_id), qty=h.qty, avg_price=h.avg_price)
        for h in rows
    ]
    avg = {int(h.instrument_id): h.avg_price for h in rows}
    return ledger, avg, symbols


@router.post("/cb/investments/{investment_id}/drift/scan", response_model=DriftScanOut)
async def scan_investment_drift(
    investment_id: int,
    body: DriftScanBody,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> DriftScanOut:
    """Compare ledger vs broker qty. Raises a DRIFT pending action on shortfall."""
    inv = await load_investment_for_user(
        session, principal_user_id=principal.user_id, investment_id=investment_id
    )
    broker, broker_symbols = await _resolve_broker(session, body)
    ledger, _avg, ledger_symbols = await _ledger(session, int(inv.id))
    symbols = {**ledger_symbols, **broker_symbols}
    detection = detect_drift(ledger, broker)
    pending_id: int | None = None
    if detection.action_type == DRIFT_ACTION_TYPE:
        open_rows = list(
            (
                await session.scalars(
                    select(CbPendingAction).where(
                        CbPendingAction.user_id == inv.user_id,
                        CbPendingAction.type == DRIFT_ACTION_TYPE,
                        CbPendingAction.dismissed_at.is_(None),
                        CbPendingAction.resolved_at.is_(None),
                    )
                )
            ).all()
        )
        existing = next(
            (
                row
                for row in open_rows
                if isinstance(row.payload, dict) and row.payload.get("investment_id") == int(inv.id)
            ),
            None,
        )
        payload = {
            "investment_id": int(inv.id),
            "deltas": [
                {
                    "instrument_id": d.instrument_id,
                    "shortfall": str(d.shortfall),
                    "excess": str(d.excess),
                }
                for d in detection.deltas
            ],
        }
        if existing is None:
            row = CbPendingAction(
                user_id=int(inv.user_id),
                type=DRIFT_ACTION_TYPE,
                payload=payload,
            )
            session.add(row)
            await session.flush()
            pending_id = int(row.id)
        else:
            existing.payload = payload
            pending_id = int(existing.id)
            await session.flush()
    return DriftScanOut(
        action_type=detection.action_type,
        pending_action_id=pending_id,
        deltas=[
            DriftDeltaOut(
                symbol=symbols.get(d.instrument_id, str(d.instrument_id)),
                ledger_qty=d.ledger_qty,
                broker_qty=d.broker_qty,
                shortfall=d.shortfall,
                excess=d.excess,
            )
            for d in detection.deltas
        ],
    )


@router.post("/cb/investments/{investment_id}/drift/fix", response_model=DriftFixOut)
async def fix_investment_drift(
    investment_id: int,
    body: DriftScanBody,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> DriftFixOut:
    """Rebase the intended ledger to broker qty. Resolves DRIFT. Places no order."""
    inv = await load_investment_for_user(
        session, principal_user_id=principal.user_id, investment_id=investment_id
    )
    broker, broker_symbols = await _resolve_broker(session, body)
    ledger, avg, ledger_symbols = await _ledger(session, int(inv.id))
    symbols = {**ledger_symbols, **broker_symbols}
    intended = {row.instrument_id: row.qty for row in ledger}
    broker_map = {row.instrument_id: row.qty for row in broker}
    result = rebase_holdings_after_drift(intended, broker_map, avg)

    existing_rows = list(
        (
            await session.scalars(
                select(CbInvestmentHolding).where(CbInvestmentHolding.investment_id == inv.id)
            )
        ).all()
    )
    by_id = {int(row.instrument_id): row for row in existing_rows}
    now = dt.datetime.now(tz=UTC)
    for instrument_id, qty in result.new_intended.items():
        row = by_id.get(instrument_id)
        if row is None:
            session.add(
                CbInvestmentHolding(
                    investment_id=int(inv.id),
                    instrument_id=instrument_id,
                    qty=qty,
                    avg_price=avg.get(instrument_id, Decimal("0")),
                )
            )
        else:
            row.qty = qty
    for instrument_id, row in by_id.items():
        if instrument_id not in result.new_intended:
            await session.delete(row)

    open_actions = list(
        (
            await session.scalars(
                select(CbPendingAction).where(
                    CbPendingAction.user_id == inv.user_id,
                    CbPendingAction.type == DRIFT_ACTION_TYPE,
                    CbPendingAction.dismissed_at.is_(None),
                    CbPendingAction.resolved_at.is_(None),
                )
            )
        ).all()
    )
    cleared = False
    for action in open_actions:
        payload = action.payload if isinstance(action.payload, dict) else {}
        if payload.get("investment_id") in (None, int(inv.id)):
            action.resolved_at = now
            cleared = True
    await session.flush()
    return DriftFixOut(
        holdings=[
            BrokerHoldingIn(
                symbol=symbols.get(iid, str(iid)),
                qty=qty,
            )
            for iid, qty in sorted(result.new_intended.items())
        ],
        cleared_action=cleared,
        synthetic_exit_count=len(result.synthetic_exits),
    )
