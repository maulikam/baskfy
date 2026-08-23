"""Track A sole-tenant scoping for user-owned curated-basket rows (SC11 / leaf-1.8.2).

Until D3 opens Phase 4, every watchlist and investment query must filter
``user_id == sole_user`` (``BASKFY_SOLE_USER_ID`` / ``resolve_sole_user_id``). Callers never
infer "the user" from an unscoped SELECT (docs/smallcase/03 rule 1, DECISIONS-SC SC1).

A principal that is not the sole tenant is **refused**. It used to be collapsed onto the sole
tenant instead — see :func:`scoped_sole_user_id` for why that inverted the contract it claimed
to keep (M43.4).
"""

from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.curated_seed import resolve_sole_user_id
from baskfy_api.problems import not_found
from baskfy_core.models import CbInvestment, CbWatchlistItem


async def scoped_sole_user_id(session: AsyncSession, principal_user_id: int | None) -> int:
    """Return the sole tenant id, or refuse a principal that is not it.

    ## What this used to do, and why it was the opposite of scoping

    Both arms of the guard returned ``sole``::

        sole = await resolve_sole_user_id(session)
        if principal_user_id is not None and principal_user_id != sole:
            return sole
        return sole

    It computed the mismatch and threw it away. So a principal that was **not** the sole tenant
    was silently *promoted* to it, and `POST /auth/register` is ungated — no entitlement, no
    signup flag, no allowlist. Anyone who could reach the API could create an account and then
    read, overwrite and delete the operator's watchlist.

    Proved by execution before the change: sole tenant 43, a freshly registered account 44,
    ``scoped_sole_user_id(session, 44) -> 43``, and ``GET /watchlist`` with that account's bearer
    token answering **200** with the sole tenant's rows.

    The docstring's claim that "rows written under a foreign ``user_id`` are invisible to
    sole-tenant list queries" was vacuous rather than wrong: no row is ever written under a
    foreign id, because the write path collapsed too. There was nothing for the isolation to
    isolate.

    ## What it does now

    Refuses. Sole-tenant means *one* tenant, and the honest expression of that is to turn away a
    principal who is not it — which is also what the multi-tenant clause of Law 2 says the gateway
    must do with a mismatch, rather than something Phase 4 will add later.

    404 and not 403, following the convention the rest of this service uses: a 403 would confirm
    that the surface holds somebody's data.
    """
    sole = await resolve_sole_user_id(session)
    if principal_user_id is not None and principal_user_id != sole:
        raise not_found("watchlist", str(principal_user_id))
    return sole


def watchlist_items_for_user_stmt(user_id: int) -> Select[tuple[CbWatchlistItem]]:
    """Watchlist rows for one user — always ``user_id ==`` the scoped sole tenant."""
    return select(CbWatchlistItem).where(CbWatchlistItem.user_id == user_id)


def investments_for_user_stmt(user_id: int) -> Select[tuple[CbInvestment]]:
    """Investment rows for one user — always ``user_id ==`` the scoped sole tenant."""
    return select(CbInvestment).where(CbInvestment.user_id == user_id)
