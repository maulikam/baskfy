"""Loading the volume-breakout sleeve's own book and money (``docs/vbt/06`` VB5).

The arithmetic is in :mod:`baskfy_core.vbt.sleeve`, stated once so that the evening job, the desk
page and the confirm path cannot each derive a slightly different equity. This module is the part
that reads rows, and it lives in the API package because both sides need it — the same place
``swing_settings`` sits for the same reason.

**Two rules this module exists to keep.** The sleeve never sizes against the account
(DECISIONS-VB PACK.4): its equity is its own capital plus its own realised and unrealised profit,
and a deposit, a Friday rebalance or a swing entry cannot move it. And the sleeve never sells a
holding it did not buy (``02`` Track C §5): the book is ``vb_position`` and nothing else, so a
name in the broker's account that is not there is invisible here and cannot become a sell line.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import OhlcvDaily, VbConfig, VbOrder, VbPosition
from baskfy_core.vbt.orders import LIVE_STATES
from baskfy_core.vbt.sleeve import (
    OpenPositionValue,
    SleeveValue,
    WorkingCommitment,
    sleeve_value,
)

_ZERO = Decimal(0)

#: The order states that have spoken for money without having spent it (``04`` §9.1).
LIVE_ORDER_STATES: tuple[str, ...] = tuple(sorted(state.value for state in LIVE_STATES))


async def open_positions(session: AsyncSession, user_id: int) -> list[VbPosition]:
    """The sleeve's book: what it bought and still holds. Never the account's holdings."""
    rows = await session.execute(
        select(VbPosition).where(
            VbPosition.user_id == user_id,
            VbPosition.state == "OPEN",
            VbPosition.quantity_open > 0,
        )
    )
    return list(rows.scalars())


async def working_orders(session: AsyncSession, user_id: int) -> list[VbOrder]:
    """Limits that are still capable of filling, and therefore still hold a slot."""
    rows = await session.execute(
        select(VbOrder).where(VbOrder.user_id == user_id, VbOrder.state.in_(LIVE_ORDER_STATES))
    )
    return list(rows.scalars())


async def realised_pnl(
    session: AsyncSession, user_id: int, as_of: dt.date | None = None
) -> Decimal:
    """Σ ``pnl_inr`` over the sleeve's closed positions, up to and including ``as_of``."""
    statement = select(func.coalesce(func.sum(VbPosition.pnl_inr), 0)).where(
        VbPosition.user_id == user_id, VbPosition.state == "CLOSED"
    )
    if as_of is not None:
        statement = statement.where(VbPosition.closed_on <= as_of)
    return Decimal(str((await session.execute(statement)).scalar_one()))


async def marks(
    session: AsyncSession, instrument_ids: list[int], as_of: dt.date
) -> dict[int, Decimal]:
    """The latest published close **on or before** ``as_of``, per instrument.

    On or before, never the latest row: a position marked at a bar from after the session being
    reported would be a look-ahead in the one place it would flatter the book — its own value.
    """
    if not instrument_ids:
        return {}
    latest = (
        select(
            OhlcvDaily.instrument_id,
            func.max(OhlcvDaily.date).label("date"),
        )
        .where(OhlcvDaily.instrument_id.in_(instrument_ids), OhlcvDaily.date <= as_of)
        .group_by(OhlcvDaily.instrument_id)
        .subquery()
    )
    rows = await session.execute(
        select(OhlcvDaily.instrument_id, OhlcvDaily.close).join(
            latest,
            (OhlcvDaily.instrument_id == latest.c.instrument_id)
            & (OhlcvDaily.date == latest.c.date),
        )
    )
    return {int(instrument_id): Decimal(str(close)) for instrument_id, close in rows}


async def sleeve_capital(session: AsyncSession, user_id: int) -> Decimal:
    """``vb_config.sleeve_capital_inr``, or zero when the row has not been seeded.

    Zero rather than an error: a sleeve with no capital plans nothing, which is exactly the
    answer a system nobody has configured should give (``02`` §3.4).
    """
    row = (
        await session.execute(
            select(VbConfig.sleeve_capital_inr).where(VbConfig.user_id == user_id)
        )
    ).scalar_one_or_none()
    return Decimal(str(row)) if row is not None else _ZERO


async def load_sleeve(session: AsyncSession, user_id: int, as_of: dt.date) -> SleeveValue:
    """The sleeve's money as of a session, from its own rows and nothing else.

    A position in a name that has not printed since it was bought is marked at its **entry**
    rather than dropped: a book that quietly excluded a suspended holding would report an equity
    the sleeve does not have, and ``04`` §6.5's write-off is the rule that eventually removes it.
    """
    positions = await open_positions(session, user_id)
    orders = await working_orders(session, user_id)
    prices = await marks(session, [row.instrument_id for row in positions], as_of)
    return sleeve_value(
        capital_inr=await sleeve_capital(session, user_id),
        realised_inr=await realised_pnl(session, user_id, as_of),
        open_positions=[
            OpenPositionValue(
                instrument_id=row.instrument_id,
                quantity_open=row.quantity_open,
                entry_avg=Decimal(str(row.entry_avg)),
                mark=prices.get(row.instrument_id, Decimal(str(row.entry_avg))),
            )
            for row in positions
        ],
        working_orders=[
            WorkingCommitment(
                instrument_id=row.instrument_id,
                quantity=row.quantity - row.filled_quantity,
                limit_price=Decimal(str(row.limit_price)),
            )
            for row in orders
            if row.quantity > row.filled_quantity
        ],
    )


async def held_instrument_ids(session: AsyncSession, user_id: int) -> frozenset[int]:
    """What the sleeve owns — ``04`` §9.1's ``ALREADY_HELD``, and Track C §5's whole content."""
    return frozenset(row.instrument_id for row in await open_positions(session, user_id))


async def working_instrument_ids(session: AsyncSession, user_id: int) -> frozenset[int]:
    """What the sleeve is already bidding for — ``04`` §9.1's ``ALREADY_WORKING``."""
    return frozenset(row.instrument_id for row in await working_orders(session, user_id))
