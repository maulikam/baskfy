"""The entitlement service — docs/07 §Entitlements (Prompt 13 deliverable 3).

    "A single EntitlementService used by BOTH the API dependency and the web UI (via /me), so the
     UI only reflects server truth. Replace the stub from Prompt 7." — PROMPTS.md Prompt 13 §3.

This module **is** that service. It answers one question — what may this caller do — from one
place: the ``plan.features`` row of the plan behind their active subscription. Nothing else in
the service decides an entitlement, and the web app decides none at all: it renders what ``GET
/me`` returns (Prompt 13 acceptance criterion 4).

What replaced the stub
----------------------
Prompt 7 hard-coded "an active subscription grants the three gated features". That is now read
from the plan row, so a plan an operator edits changes what it grants without a deploy, and the
₹0 tier can grant a *different* set (Prompt 13 §5's "limited universe") rather than a subset of
one. Two behaviours changed as a result, both deliberate:

* **Backtests are now paid.** The stub granted them to everyone; Prompt 13 §6 lists them among
  the features gating applies to. No endpoint enforces it yet — Prompt 15 builds ``/backtests``
  — so today this shows up only in the ``/me`` payload.
* **An unpaid account's ``max_screens`` is 5, not 50.** docs/07's example payload shows 50 for a
  subscriber and the bundle gives no free-tier number; see ``decile_core.entitlements``.

The shape of the answer is docs/07's, exactly seven keys. ``Feature`` and ``Entitlements`` are
defined in ``decile_core.entitlements`` so the seed data, this service and ``GET /plans`` share
one definition; only the database lookup is here, because ``packages/core`` does no I/O.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Final

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from decile_api.auth import Principal, PrincipalDep, settings_for
from decile_api.db import SessionDep
from decile_api.settings import Settings, get_settings
from decile_core.entitlements import (
    ANONYMOUS,
    FREE_TIER,
    REGISTERED,
    Entitlements,
    Feature,
    FeatureNotEntitled,
    Override,
    apply_overrides,
)
from decile_core.models import EntitlementOverride, Plan, Subscription
from decile_core.seed_data import FREE_PLAN

__all__ = [
    "ANONYMOUS",
    "Entitlements",
    "EntitlementsDep",
    "Feature",
    "FeatureNotEntitled",
    "active_overrides",
    "current_entitlements",
    "entitlements_for",
    "resolve_active_grant",
]

#: The subscription statuses that entitle. docs/04 lists four; only one of them is paying.
#: ``past_due`` deliberately does not entitle — a card that stopped working is the moment the
#: gated features stop, which is what Prompt 13's second acceptance criterion asserts.
ENTITLING_STATUSES: Final[frozenset[str]] = frozenset({"active"})


async def resolve_active_grant(
    session: AsyncSession, user_id: int, *, now: dt.datetime | None = None
) -> tuple[Subscription, Plan] | None:
    """The subscription that is paying for this account right now, with its plan.

    ``current_period_end`` in the past is treated as expired even if the status column still says
    ``active``: the row is only as fresh as the last webhook, and an entitlement that outlives the
    period because a ``subscription.expired`` event was never delivered is a free subscription.
    A NULL ``current_period_end`` never expires, which is what the one-time "Forever" plan is.
    """
    moment = now or dt.datetime.now(tz=dt.UTC)
    rows = (
        await session.execute(
            select(Subscription, Plan)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(
                Subscription.user_id == user_id,
                Subscription.status.in_(ENTITLING_STATUSES),
            )
            .order_by(Subscription.started_at.desc(), Subscription.id.desc())
        )
    ).all()
    for subscription, plan in rows:
        if subscription.current_period_end is None or subscription.current_period_end > moment:
            return subscription, plan
    return None


def _baseline(settings: Settings) -> Entitlements:
    """What a signed-in account with no paying subscription gets.

    With the ₹0 tier flag off that is :data:`REGISTERED` — the screener, five saved screens, and
    none of the gated features. With it on, the account is on the ``free`` plan, which is the same
    thing restricted to one universe (Prompt 13 §5).
    """
    if not settings.free_tier_enabled:
        return REGISTERED
    return Entitlements.from_plan_features(FREE_PLAN.features)


async def active_overrides(
    session: AsyncSession, user_id: int, *, now: dt.datetime | None = None
) -> list[Override]:
    """The staff overrides currently in force for one account (Prompt 17 deliverable 4).

    Expiry is filtered in Python rather than in SQL so that ``expires_at IS NULL`` and
    ``expires_at > now`` are one rule in one place — :meth:`EntitlementOverride.is_active` — which
    the admin page also renders from. Two rows per account at the very most, so there is nothing
    to gain from pushing it down.
    """
    moment = now or dt.datetime.now(tz=dt.UTC)
    rows = (
        await session.execute(
            select(EntitlementOverride).where(EntitlementOverride.user_id == user_id)
        )
    ).scalars()
    return [
        Override(feature=row.feature, grant=row.effect == "grant", value=row.value)
        for row in rows
        if row.is_active(now=moment)
    ]


async def entitlements_for(
    session: AsyncSession, principal: Principal, *, settings: Settings | None = None
) -> Entitlements:
    """The one resolution path. Every gated endpoint and ``GET /me`` go through it.

    Order matters: the plan decides the baseline, then staff overrides are layered on top
    (Prompt 17 deliverable 4). A support grant that a nightly subscription sync could silently
    undo would not be a grant.
    """
    resolved = settings or get_settings()
    if principal.user_id is None:
        # An anonymous caller has no account, so there is nothing an override could hang off.
        return ANONYMOUS

    grant = await resolve_active_grant(session, principal.user_id)
    base = (
        _baseline(resolved) if grant is None else Entitlements.from_plan_features(grant[1].features)
    )

    overrides = await active_overrides(session, principal.user_id)
    return base if not overrides else apply_overrides(base, overrides)


async def current_entitlements(
    request: Request, session: SessionDep, principal: PrincipalDep
) -> Entitlements:
    """The FastAPI dependency. Reads *this application's* settings, not the process-wide cache,
    so a test app built with the ₹0 tier enabled resolves differently from one built without."""
    return await entitlements_for(session, principal, settings=settings_for(request))


EntitlementsDep = Annotated[Entitlements, Depends(current_entitlements)]

#: Re-exported for the ₹0 tier's tests, which need the shape without a database.
FREE_TIER_ENTITLEMENTS: Final = FREE_TIER
