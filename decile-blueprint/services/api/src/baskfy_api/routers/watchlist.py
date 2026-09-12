"""Instrument watchlist + discover preferences (AF I.2 / I.4).

Paths keep the word ``watchlist`` so explore's mutating-route contract stays true when this
router is included there. No order or execute routes.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Path
from pydantic import BaseModel, Field
from sqlalchemy import select

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.db import SessionDep
from baskfy_api.problems import not_found
from baskfy_api.schemas import _In
from baskfy_core.models import Instrument, OhlcvDaily
from baskfy_core.models.instrument_watch import InstrumentWatchItem, UserDiscoverPreferences

router = APIRouter(tags=["watchlist"])

SYMBOL_MAX = 32
SymbolPath = Annotated[str, Path(min_length=1, max_length=SYMBOL_MAX)]

GoalKey = Literal["steady-compounding", "long-term-growth", "high-growth"]
HorizonKey = Literal["1-3", "3-5", "5-plus"]
RiskKey = Literal["lower", "moderate", "higher"]
RebalanceKey = Literal["any", "WEEKLY", "MONTHLY", "QUARTERLY"]


class InstrumentWatchAddIn(_In):
    symbol: Annotated[str, Field(min_length=1, max_length=SYMBOL_MAX)]


class InstrumentWatchItemOut(BaseModel):
    symbol: str
    name: str
    watched_at: dt.datetime
    close_at_watch: Decimal | None = None
    last_close: Decimal | None = None
    moved_pct: Decimal | None = None


class InstrumentWatchlistOut(BaseModel):
    items: list[InstrumentWatchItemOut]
    count: int


class DiscoverPreferencesIn(_In):
    goal: GoalKey
    horizon: HorizonKey
    risk: RiskKey
    amount: Annotated[Decimal, Field(ge=Decimal("1000"), le=Decimal("100000000"))]
    rebalance: RebalanceKey
    onboarding_completed: bool = False


class DiscoverPreferencesOut(BaseModel):
    goal: str
    horizon: str
    risk: str
    amount: Decimal
    rebalance: str
    onboarding_completed_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None
    saved: bool


async def _resolve_active_instrument(session: SessionDep, symbol: str) -> Instrument | None:
    needle = symbol.strip().upper()
    rows = (
        await session.execute(
            select(Instrument)
            .where(Instrument.symbol == needle, Instrument.is_active.is_(True))
            .order_by(Instrument.id.asc())
            .limit(2)
        )
    ).scalars().all()
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        # Prefer EQ cash over ETF/index when the symbol collides across types.
        for row in rows:
            if row.instrument_type == "EQ":
                return row
        return rows[0]
    return None


async def _last_close(session: SessionDep, instrument_id: int) -> Decimal | None:
    close = (
        await session.execute(
            select(OhlcvDaily.close)
            .where(OhlcvDaily.instrument_id == instrument_id)
            .order_by(OhlcvDaily.date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return Decimal(close) if close is not None else None


def _moved_pct(close_at_watch: Decimal | None, last_close: Decimal | None) -> Decimal | None:
    if close_at_watch is None or last_close is None or close_at_watch == 0:
        return None
    return ((last_close - close_at_watch) / close_at_watch * Decimal("100")).quantize(
        Decimal("0.01")
    )


@router.get("/watchlist/instruments", response_model=InstrumentWatchlistOut)
async def list_instrument_watchlist(
    session: SessionDep, principal: AuthenticatedDep
) -> InstrumentWatchlistOut:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    rows = (
        await session.execute(
            select(InstrumentWatchItem, Instrument)
            .join(Instrument, Instrument.id == InstrumentWatchItem.instrument_id)
            .where(InstrumentWatchItem.user_id == user_id)
            .order_by(InstrumentWatchItem.watched_at.desc())
        )
    ).all()
    items: list[InstrumentWatchItemOut] = []
    for item, instrument in rows:
        last = await _last_close(session, instrument.id)
        items.append(
            InstrumentWatchItemOut(
                symbol=instrument.symbol,
                name=instrument.name,
                watched_at=item.watched_at,
                close_at_watch=item.close_at_watch,
                last_close=last,
                moved_pct=_moved_pct(item.close_at_watch, last),
            )
        )
    return InstrumentWatchlistOut(items=items, count=len(items))


@router.post("/watchlist/instruments", response_model=InstrumentWatchItemOut, status_code=201)
async def add_instrument_watch(
    body: InstrumentWatchAddIn,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> InstrumentWatchItemOut:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    instrument = await _resolve_active_instrument(session, body.symbol)
    if instrument is None:
        raise not_found("instrument", body.symbol.strip().upper())
    existing = (
        await session.execute(
            select(InstrumentWatchItem).where(
                InstrumentWatchItem.user_id == user_id,
                InstrumentWatchItem.instrument_id == instrument.id,
            )
        )
    ).scalar_one_or_none()
    now = dt.datetime.now(tz=dt.UTC)
    last = await _last_close(session, instrument.id)
    if existing is not None:
        existing.watched_at = now
        existing.close_at_watch = last
        item = existing
    else:
        item = InstrumentWatchItem(
            user_id=user_id,
            instrument_id=instrument.id,
            watched_at=now,
            close_at_watch=last,
        )
        session.add(item)
    await session.commit()
    await session.refresh(item)
    return InstrumentWatchItemOut(
        symbol=instrument.symbol,
        name=instrument.name,
        watched_at=item.watched_at,
        close_at_watch=item.close_at_watch,
        last_close=last,
        moved_pct=_moved_pct(item.close_at_watch, last),
    )


@router.delete("/watchlist/instruments/{symbol}", status_code=204)
async def remove_instrument_watch(
    symbol: SymbolPath,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> None:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    instrument = await _resolve_active_instrument(session, symbol)
    if instrument is None:
        raise not_found("instrument", symbol.strip().upper())
    item = (
        await session.execute(
            select(InstrumentWatchItem).where(
                InstrumentWatchItem.user_id == user_id,
                InstrumentWatchItem.instrument_id == instrument.id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise not_found("watchlist instrument", symbol.strip().upper())
    await session.delete(item)
    await session.commit()


@router.get("/watchlist/discover-preferences", response_model=DiscoverPreferencesOut)
async def get_discover_preferences(
    session: SessionDep, principal: AuthenticatedDep
) -> DiscoverPreferencesOut:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    row = (
        await session.execute(
            select(UserDiscoverPreferences).where(UserDiscoverPreferences.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        return DiscoverPreferencesOut(
            goal="long-term-growth",
            horizon="5-plus",
            risk="moderate",
            amount=Decimal("500000"),
            rebalance="any",
            onboarding_completed_at=None,
            updated_at=None,
            saved=False,
        )
    return DiscoverPreferencesOut(
        goal=row.goal,
        horizon=row.horizon,
        risk=row.risk,
        amount=row.amount,
        rebalance=row.rebalance,
        onboarding_completed_at=row.onboarding_completed_at,
        updated_at=row.updated_at,
        saved=True,
    )


@router.put("/watchlist/discover-preferences", response_model=DiscoverPreferencesOut)
async def put_discover_preferences(
    body: DiscoverPreferencesIn,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> DiscoverPreferencesOut:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    now = dt.datetime.now(tz=dt.UTC)
    row = (
        await session.execute(
            select(UserDiscoverPreferences).where(UserDiscoverPreferences.user_id == user_id)
        )
    ).scalar_one_or_none()
    amount = body.amount.quantize(Decimal("0.01"))
    if row is None:
        row = UserDiscoverPreferences(
            user_id=user_id,
            goal=body.goal,
            horizon=body.horizon,
            risk=body.risk,
            amount=amount,
            rebalance=body.rebalance,
            onboarding_completed_at=now if body.onboarding_completed else None,
            updated_at=now,
        )
        session.add(row)
    else:
        row.goal = body.goal
        row.horizon = body.horizon
        row.risk = body.risk
        row.amount = amount
        row.rebalance = body.rebalance
        row.updated_at = now
        if body.onboarding_completed and row.onboarding_completed_at is None:
            row.onboarding_completed_at = now
    await session.commit()
    await session.refresh(row)
    return DiscoverPreferencesOut(
        goal=row.goal,
        horizon=row.horizon,
        risk=row.risk,
        amount=row.amount,
        rebalance=row.rebalance,
        onboarding_completed_at=row.onboarding_completed_at,
        updated_at=row.updated_at,
        saved=True,
    )
