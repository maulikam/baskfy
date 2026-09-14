"""Trade history into Postgres, and what it lets the holdings say — NEEDS-MAULIK §32.

Two writes, and neither moves a share or an allocation:

1. :func:`store_fills` puts executions in ``broker_trade``. ``ON CONFLICT DO NOTHING`` on
   ``(broker_account_id, exchange, trade_id)``, so a re-imported file or a CSV overlapping a day
   captured live changes nothing (house rule 7).
2. :func:`apply_trade_history` dates the holdings the trades account for. For each instrument this
   broker account holds, the trades are replayed FIFO; only when they net to exactly the held
   quantity does ``portfolio_holding.first_bought_on`` become the oldest open lot's date and
   ``history_source`` become ``BROKER``. A bonus, a split or a transfer-in leaves the holding as it
   was and is named in the report. A ``CAS`` or ``MANUAL`` history is never overwritten.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

import anyio
import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.broker_accounts import ensure_default_broker_account
from baskfy_api.broker_oauth import (
    dry_run_enabled,
    token_encryption_key,
    token_store_for,
    token_store_path,
)
from baskfy_api.portfolios import resolve_symbols
from baskfy_core.allocation_ledger import PortfolioKind
from baskfy_core.models import BrokerTrade, PortfolioHolding
from baskfy_core.tradebook import TradeFill, TradeSide, history_for
from baskfy_providers.errors import ProviderError
from baskfy_providers.factory import build_kite_provider
from baskfy_providers.records import BrokerAccountRef
from baskfy_providers.settings import get_provider_settings

log = logging.getLogger(__name__)

#: What a live read can fail with that is the broker's fault, not ours — the holdings read's list.
_LIVE_READ_ERRORS: Final[tuple[type[Exception], ...]] = (
    httpx.HTTPError,
    UnicodeDecodeError,
    OSError,
    ProviderError,
)

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")

#: ``history_source`` values a trade replay may replace. CAS and MANUAL are a person's word.
_REPLACEABLE_HISTORY: Final = ("NONE", "BROKER")


class TradeSource(StrEnum):
    CONSOLE_CSV = "CONSOLE_CSV"
    KITE_API = "KITE_API"


@dataclass(frozen=True, slots=True)
class StoreReport:
    received: int
    inserted: int
    #: Already in ``broker_trade`` under the same trade id — an overlap, not an error.
    already_present: int
    #: Symbols that did not resolve to one instrument. Their trades are kept, undated.
    unresolved: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UndatedHolding:
    symbol: str
    held: Decimal
    traded_net: Decimal
    reason: str


@dataclass(frozen=True, slots=True)
class HistoryReport:
    dated: int
    undated: tuple[UndatedHolding, ...]


def _executed_at(fill: TradeFill) -> dt.datetime | None:
    if fill.executed_at is None:
        return None
    if fill.executed_at.tzinfo is None:
        # Console and Kite both print IST wall time.
        return fill.executed_at.replace(tzinfo=IST)
    return fill.executed_at


async def store_fills(
    session: AsyncSession,
    fills: Sequence[TradeFill],
    *,
    user_id: int,
    broker_account_id: int,
    source: TradeSource,
) -> StoreReport:
    """Insert what is new; count what was already there. One statement per 1,000 rows."""
    if not fills:
        return StoreReport(received=0, inserted=0, already_present=0, unresolved=())
    resolutions = await resolve_symbols(session, [fill.symbol for fill in fills])
    unresolved: set[str] = set()
    rows: list[dict[str, object]] = []
    for fill in fills:
        resolution = resolutions.get(fill.symbol)
        instrument_id = resolution.instrument_id if resolution is not None else None
        if instrument_id is None:
            unresolved.add(fill.symbol)
        rows.append(
            {
                "user_id": user_id,
                "broker_account_id": broker_account_id,
                "instrument_id": instrument_id,
                "symbol": fill.symbol,
                "isin": fill.isin,
                "exchange": fill.exchange,
                "side": fill.side.value,
                "quantity": fill.quantity,
                "price": fill.price,
                "trade_date": fill.trade_date,
                "executed_at": _executed_at(fill),
                "trade_id": fill.trade_id,
                "order_id": fill.order_id,
                "source": source.value,
            }
        )
    inserted = 0
    for start in range(0, len(rows), 1000):
        statement = (
            insert(BrokerTrade)
            .values(rows[start : start + 1000])
            .on_conflict_do_nothing(constraint="uq_broker_trade_account_trade")
            .returning(BrokerTrade.id)
        )
        inserted += len((await session.execute(statement)).all())
    return StoreReport(
        received=len(rows),
        inserted=inserted,
        already_present=len(rows) - inserted,
        unresolved=tuple(sorted(unresolved)),
    )


def fill_from_row(row: BrokerTrade) -> TradeFill:
    return TradeFill(
        symbol=row.symbol,
        exchange=row.exchange,
        side=TradeSide(row.side),
        quantity=row.quantity,
        price=row.price,
        trade_date=row.trade_date,
        trade_id=row.trade_id,
        order_id=row.order_id,
        isin=row.isin,
        executed_at=row.executed_at,
    )


async def fills_by_instrument(
    session: AsyncSession, *, user_id: int, broker_account_id: int | None = None
) -> dict[int, list[TradeFill]]:
    """Every resolved trade for the user (optionally one account), grouped by instrument."""
    query = select(BrokerTrade).where(
        BrokerTrade.user_id == user_id, BrokerTrade.instrument_id.is_not(None)
    )
    if broker_account_id is not None:
        query = query.where(BrokerTrade.broker_account_id == broker_account_id)
    grouped: dict[int, list[TradeFill]] = defaultdict(list)
    for row in (await session.scalars(query)).all():
        if row.instrument_id is not None:
            grouped[int(row.instrument_id)].append(fill_from_row(row))
    return dict(grouped)


async def apply_trade_history(
    session: AsyncSession, *, user_id: int, broker_account_id: int
) -> HistoryReport:
    """Date every holding at this account whose trades add up to it. See the module docstring."""
    held_rows = (
        await session.execute(
            select(
                PortfolioHolding.instrument_id,
                func.sum(PortfolioHolding.quantity).label("held"),
            )
            .where(
                PortfolioHolding.broker_account_id == broker_account_id,
                PortfolioHolding.portfolio_kind == PortfolioKind.CAPITAL.value,
            )
            .group_by(PortfolioHolding.instrument_id)
        )
    ).all()
    if not held_rows:
        return HistoryReport(dated=0, undated=())
    fills = await fills_by_instrument(session, user_id=user_id, broker_account_id=broker_account_id)
    symbol_rows = (
        await session.execute(
            select(BrokerTrade.instrument_id, func.min(BrokerTrade.symbol))
            .where(BrokerTrade.broker_account_id == broker_account_id)
            .group_by(BrokerTrade.instrument_id)
        )
    ).all()
    symbols: dict[int, str] = {
        int(instrument): str(symbol) for instrument, symbol in symbol_rows if instrument is not None
    }

    dated = 0
    undated: list[UndatedHolding] = []
    for instrument_id, held in held_rows:
        history = history_for(fills.get(int(instrument_id), []), Decimal(held))
        if not history.reconciles or history.first_bought_on is None:
            if int(instrument_id) in fills:
                undated.append(
                    UndatedHolding(
                        symbol=symbols.get(int(instrument_id), str(instrument_id)),
                        held=Decimal(held),
                        traded_net=history.net_quantity,
                        reason=(
                            "the file starts after this position was opened"
                            if history.unmatched_sell_quantity > 0
                            else "trades do not add up to what is held (a bonus, split or "
                            "transfer-in changes quantity without a trade)"
                        ),
                    )
                )
            continue
        await session.execute(
            update(PortfolioHolding)
            .where(
                PortfolioHolding.broker_account_id == broker_account_id,
                PortfolioHolding.instrument_id == instrument_id,
                PortfolioHolding.history_source.in_(_REPLACEABLE_HISTORY),
            )
            .values(first_bought_on=history.first_bought_on, history_source="BROKER")
        )
        dated += 1
    return HistoryReport(dated=dated, undated=tuple(undated))


# ---------------------------------------------------------------------------
# Kite's same-day read
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaptureReport:
    """What one same-day capture did. ``captured`` is False with a reason when nothing was read."""

    captured: bool
    reason: str
    store: StoreReport | None = None
    history: HistoryReport | None = None


def read_kite_trades_today() -> list[TradeFill] | str:
    """Today's Zerodha executions, or the sentence saying why there are none.

    The same guards as the holdings read: DRY_RUN reads nothing from a broker, and a missing key
    or session is a reason, not an exception. Only Kite's read-only ``GET /trades`` is called.
    """
    if dry_run_enabled():
        return "DRY_RUN is on; no broker read was made"
    api_key = os.environ.get("BASKFY_KITE_API_KEY", "").strip()
    if not api_key:
        return "no broker app key is configured on this deployment"
    try:
        store = token_store_for()
        if not store.exists():
            return "no Kite session has been stored yet"
        store.require_fresh()
        settings = get_provider_settings().model_copy(
            update={
                "kite_api_key": api_key,
                "kite_token_path": str(token_store_path()),
                "kite_token_encryption_key": token_encryption_key(),
            }
        )
        return build_kite_provider(settings).broker_trades(
            BrokerAccountRef(broker_account_id=1, broker_id="zerodha")
        )
    except _LIVE_READ_ERRORS as exc:
        # The type only: a provider message can name the token path.
        log.warning("trades: the Kite read failed (%s)", type(exc).__name__)
        return f"the Kite trades read failed ({type(exc).__name__})"


async def capture_kite_trades(session: AsyncSession, *, user_id: int) -> CaptureReport:
    """Read today's trades from Kite, store them, and re-date the holdings. Caller commits."""
    read = await anyio.to_thread.run_sync(read_kite_trades_today)
    if isinstance(read, str):
        return CaptureReport(captured=False, reason=read)
    broker_account_id = await ensure_default_broker_account(session, user_id, broker_id="zerodha")
    store = await store_fills(
        session,
        read,
        user_id=user_id,
        broker_account_id=broker_account_id,
        source=TradeSource.KITE_API,
    )
    history = await apply_trade_history(
        session, user_id=user_id, broker_account_id=broker_account_id
    )
    return CaptureReport(
        captured=True,
        reason=f"{store.inserted} new of {store.received} trades read from Kite",
        store=store,
        history=history,
    )
