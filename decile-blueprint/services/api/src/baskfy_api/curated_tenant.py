"""Track A sole-tenant scoping for user-owned curated-basket rows (SC11 / leaf-1.8.2).

Until D3 opens Phase 4, every watchlist and investment query must filter
``user_id == sole_user`` (``BASKFY_SOLE_USER_ID`` / ``resolve_sole_user_id``). Callers never
infer "the user" from an unscoped SELECT (docs/smallcase/03 rule 1, DECISIONS-SC SC1).

Cross-tenant principal ids are collapsed to the sole tenant today. Rows written under a
foreign ``user_id`` are invisible to sole-tenant list queries — the isolation contract the
multi-tenant clause will keep when Phase 4 lands.
"""

from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.curated_seed import resolve_sole_user_id
from baskfy_core.models import CbInvestment, CbWatchlistItem


async def scoped_sole_user_id(session: AsyncSession, principal_user_id: int | None) -> int:
    """Return the sole tenant id for every user-scoped ``cb_*`` read/write.

    *principal_user_id* is accepted for API symmetry but never used to select a different
    tenant while Track A is sole-tenant.
    """
    sole = await resolve_sole_user_id(session)
    if principal_user_id is not None and principal_user_id != sole:
        return sole
    return sole


def watchlist_items_for_user_stmt(user_id: int) -> Select[tuple[CbWatchlistItem]]:
    """Watchlist rows for one user — always ``user_id ==`` the scoped sole tenant."""
    return select(CbWatchlistItem).where(CbWatchlistItem.user_id == user_id)


def investments_for_user_stmt(user_id: int) -> Select[tuple[CbInvestment]]:
    """Investment rows for one user — always ``user_id ==`` the scoped sole tenant."""
    return select(CbInvestment).where(CbInvestment.user_id == user_id)
