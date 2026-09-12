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
from baskfy_api.problems import Problem, ProblemType
from baskfy_core.models import CbInvestment, CbWatchlistItem

#: RFC 9457 §3.2 extension member carried on the refusal below, and the *only* machine-readable
#: difference between "you are not the tenant this deployment serves" and "there is nothing
#: here". The status cannot carry it: this refusal is deliberately a 404 rather than a 403
#: (M43.4, and the docstring below), which makes it indistinguishable from absence to anything
#: reading the status alone — and that was C7, a page telling a second account the strategy had
#: never run. `apps/web/src/lib/api/sleeve-read.ts` reads this member; changing the string
#: changes a contract with the web layer, so it is a constant on both sides.
SOLE_TENANT_REFUSED = "not-the-sole-tenant"


async def scoped_sole_user_id(
    session: AsyncSession, principal_user_id: int | None, *, surface: str | None = None
) -> int:
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

    ## Why the refusal no longer says "watchlist"

    `gates/sleeve-read-contract.md` C7. This guard is shared by every sole-tenant surface — the
    swing book, VBT, TWT, investments, the curated shelves — and it used to raise
    ``not_found("watchlist", principal_user_id)`` whatever it was guarding. So a second account
    reading the **swing book** was answered ``No watchlist with id '6'.``, and the one record
    anybody had of that refusal pointed an operator at the curated-basket watchlist. Worse, the
    id in that sentence is the *caller's user id* dressed up as a watchlist id.

    The refusal now says what actually happened — this deployment serves one account and the
    caller is not it — which is the same fact a 404 on every one of these routes already
    discloses, and is what an operator reading the log needs. ``surface`` lets a route name the
    thing it guards ("the swing book"); it is optional so that adding it to a route is a
    one-line change rather than a flag day.
    """
    sole = await resolve_sole_user_id(session)
    if principal_user_id is not None and principal_user_id != sole:
        raise Problem(
            ProblemType.NOT_FOUND,
            f"This deployment serves a single account and this caller "
            f"(user {principal_user_id}) is not it"
            + (f", so {surface} is not readable here." if surface else "."),
            reason=SOLE_TENANT_REFUSED,
        )
    return sole


def watchlist_items_for_user_stmt(user_id: int) -> Select[tuple[CbWatchlistItem]]:
    """Watchlist rows for one user — always ``user_id ==`` the scoped sole tenant."""
    return select(CbWatchlistItem).where(CbWatchlistItem.user_id == user_id)


def investments_for_user_stmt(user_id: int) -> Select[tuple[CbInvestment]]:
    """Investment rows for one user — always ``user_id ==`` the scoped sole tenant."""
    return select(CbInvestment).where(CbInvestment.user_id == user_id)
