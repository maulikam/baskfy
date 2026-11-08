"""Ensure a default ``broker_account`` row for a user (P4.1).

I/O lives here, not in ``packages/core``. One Zerodha account per ``app_user`` is the
founder shape; a second broker is another row after P4.2.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import BrokerAccount
from baskfy_core.tenancy import DEFAULT_BROKER_ID


async def ensure_default_broker_account(
    session: AsyncSession,
    user_id: int,
    *,
    broker_id: str = DEFAULT_BROKER_ID,
) -> int:
    """Return the id of this user's default broker account, creating it if needed."""
    existing = await session.scalar(
        select(BrokerAccount.id).where(
            BrokerAccount.user_id == user_id,
            BrokerAccount.broker_id == broker_id,
        )
    )
    if existing is not None:
        return int(existing)
    row = BrokerAccount(user_id=user_id, broker_id=broker_id, label="primary")
    session.add(row)
    await session.flush()
    return int(row.id)
