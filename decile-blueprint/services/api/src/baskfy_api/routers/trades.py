"""Trade history — import a Console tradebook, capture today's trades from Kite, list them.

    POST /trades/import      multipart CSV (Zerodha Console → Reports → Tradebook)
    POST /trades/sync-today  Kite's read-only GET /trades, stored and applied
    GET  /trades             what is stored, newest first

Bookkeeping only. Nothing here places, modifies or cancels an order (law 2): the Kite call is the
read-only ``/trades`` list, and every write is to ``broker_trade`` or a holding's purchase date.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.broker_accounts import ensure_default_broker_account
from baskfy_api.broker_trades import (
    HistoryReport,
    StoreReport,
    TradeSource,
    apply_trade_history,
    capture_kite_trades,
    store_fills,
)
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.db import SessionDep
from baskfy_api.problems import Problem, ProblemType
from baskfy_core.models import BrokerTrade
from baskfy_core.tradebook import TradebookError, parse_tradebook_csv

router = APIRouter(prefix="/trades", tags=["trades"])

#: A decade of an active account's tradebook is a few megabytes.
MAX_TRADEBOOK_BYTES = 20 * 1024 * 1024


class TradebookUndatedHoldingOut(BaseModel):
    symbol: str
    held: Decimal
    traded_net: Decimal
    reason: str


class TradebookImportOut(BaseModel):
    """What an import or a capture did, in the numbers a person checks it against."""

    received: int
    inserted: int
    already_present: int
    skipped_non_equity: int = 0
    unresolved_symbols: list[str] = Field(default_factory=list)
    #: Holdings whose purchase date now comes from these trades.
    dated_holdings: int = 0
    #: Holdings the trades touch but do not add up to — each with why.
    undated_holdings: list[TradebookUndatedHoldingOut] = Field(default_factory=list)
    note: str = ""


class BrokerTradeOut(BaseModel):
    trade_date: dt.date
    executed_at: dt.datetime | None = None
    symbol: str
    exchange: str
    side: str
    quantity: Decimal
    price: Decimal
    value: Decimal
    trade_id: str
    source: str


class BrokerTradesOut(BaseModel):
    rows: list[BrokerTradeOut] = Field(default_factory=list)
    total: int
    first_trade_on: dt.date | None = None
    last_trade_on: dt.date | None = None


def _report(
    store: StoreReport | None,
    history: HistoryReport | None,
    *,
    skipped: int = 0,
    note: str = "",
) -> TradebookImportOut:
    return TradebookImportOut(
        received=store.received if store else 0,
        inserted=store.inserted if store else 0,
        already_present=store.already_present if store else 0,
        skipped_non_equity=skipped,
        unresolved_symbols=list(store.unresolved) if store else [],
        dated_holdings=history.dated if history else 0,
        undated_holdings=[
            TradebookUndatedHoldingOut(
                symbol=item.symbol, held=item.held, traded_net=item.traded_net, reason=item.reason
            )
            for item in (history.undated if history else ())
        ],
        note=note,
    )


@router.post("/import", response_model=TradebookImportOut)
async def import_tradebook(
    session: SessionDep,
    principal: AuthenticatedDep,
    file: Annotated[UploadFile, File(description="Zerodha Console tradebook CSV")],
    broker_id: Annotated[str, Query(min_length=2, max_length=32)] = "zerodha",
) -> TradebookImportOut:
    """Read the whole file or refuse it with the line that could not be read. Re-importable."""
    user_id = principal.require_user()
    raw = await file.read()
    if len(raw) > MAX_TRADEBOOK_BYTES:
        raise Problem(
            ProblemType.BAD_REQUEST,
            f"The file is {len(raw)} bytes; the limit is {MAX_TRADEBOOK_BYTES}. Export one year "
            "at a time.",
        )
    try:
        parsed = parse_tradebook_csv(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise Problem(ProblemType.BAD_REQUEST, "The file is not UTF-8 text.") from exc
    except TradebookError as exc:
        raise Problem(ProblemType.BAD_REQUEST, f"The tradebook could not be read: {exc}") from exc

    broker_account_id = await ensure_default_broker_account(session, user_id, broker_id=broker_id)
    store = await store_fills(
        session,
        parsed.fills,
        user_id=user_id,
        broker_account_id=broker_account_id,
        source=TradeSource.CONSOLE_CSV,
    )
    history = await apply_trade_history(
        session, user_id=user_id, broker_account_id=broker_account_id
    )
    await session.commit()
    return _report(store, history, skipped=parsed.skipped_non_equity)


@router.post("/sync-today", response_model=TradebookImportOut)
async def sync_todays_trades(
    session: SessionDep, principal: AuthenticatedDep
) -> TradebookImportOut:
    """Kite's ``/trades`` has today and nothing else; this is the only way to keep a day of it.

    Sole-tenant, like holdings sync: the stored Kite session belongs to one account.
    """
    user_id = await scoped_sole_user_id(
        session, principal.require_user(), surface="broker trades sync"
    )
    report = await capture_kite_trades(session, user_id=user_id)
    if report.captured:
        await session.commit()
    return _report(report.store, report.history, note=report.reason)


@router.get("", response_model=BrokerTradesOut)
async def list_trades(
    session: SessionDep,
    principal: AuthenticatedDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    symbol: Annotated[str | None, Query(max_length=40)] = None,
) -> BrokerTradesOut:
    user_id = principal.require_user()
    scope = select(BrokerTrade).where(BrokerTrade.user_id == user_id)
    if symbol:
        scope = scope.where(BrokerTrade.symbol == symbol.strip().upper())
    bounds = (
        await session.execute(
            select(
                func.count(),
                func.min(BrokerTrade.trade_date),
                func.max(BrokerTrade.trade_date),
            ).select_from(scope.subquery())
        )
    ).one()
    rows = (
        await session.scalars(
            scope.order_by(
                BrokerTrade.trade_date.desc(),
                BrokerTrade.executed_at.desc().nulls_last(),
                BrokerTrade.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return BrokerTradesOut(
        rows=[
            BrokerTradeOut(
                trade_date=row.trade_date,
                executed_at=row.executed_at,
                symbol=row.symbol,
                exchange=row.exchange,
                side=row.side,
                quantity=row.quantity,
                price=row.price,
                value=(row.quantity * row.price).quantize(Decimal("0.01")),
                trade_id=row.trade_id,
                source=row.source,
            )
            for row in rows
        ],
        total=int(bounds[0]),
        first_trade_on=bounds[1],
        last_trade_on=bounds[2],
    )
