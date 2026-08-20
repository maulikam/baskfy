"""The entitlement service (Prompt 7 deliverable 3 — the stub half).

    "For now, everyone is entitled to everything except export/custom columns/historical ranks,
     which check a stub entitlement service (replaced in Prompt 13)."

docs/07 §Entitlements fixes the wire shape:

```json
{ "entitlements": { "screener": true, "export_csv": true, "custom_columns": true,
                    "historical_ranks": true, "backtests": true, "api_access": false,
                    "max_screens": 50 } }
```

and the rule: "Enforcement is server-side on every gated endpoint; the UI only *reflects*
entitlements."

What the stub actually decides
------------------------------
The three gated features are granted to a caller with an **active subscription**, and to nobody
else. That is the shape Prompt 13 will implement for real against Razorpay, so the call sites do
not have to change when it does — only this file. Anonymous callers are never entitled, which is
what makes the 402 path testable today.

docs/01 §1 lists five paid features: "export, custom columns, historical ranks, community Slack,
AMAs". The last two are not API surfaces, so they appear in ``seed_data.GATED_FEATURES`` and not
here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Final

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from decile_api.auth import Principal, PrincipalDep
from decile_api.db import SessionDep
from decile_api.problems import payment_required
from decile_core.models import Subscription


class Feature(StrEnum):
    """The keys docs/07 §Entitlements returns, in the document's order."""

    SCREENER = "screener"
    EXPORT_CSV = "export_csv"
    CUSTOM_COLUMNS = "custom_columns"
    HISTORICAL_RANKS = "historical_ranks"
    BACKTESTS = "backtests"
    API_ACCESS = "api_access"


#: Prompt 7: "everyone is entitled to everything except export/custom columns/historical ranks".
GATED: Final[frozenset[Feature]] = frozenset(
    {Feature.EXPORT_CSV, Feature.CUSTOM_COLUMNS, Feature.HISTORICAL_RANKS}
)

#: docs/07's own example payload shows ``"api_access": false``, which is also the only honest
#: answer while the public API does not exist — Prompt 20 builds it. Read literally, Prompt 7's
#: "everyone is entitled to everything except [the three]" would make this true; docs/07 is the
#: source of truth and the safer of the two readings. Noted in docs/07a §4.
API_ACCESS_AVAILABLE: Final = False

#: docs/07's example. Not present in ``seed_data.PLANS[*].features``, so it is a constant here
#: until Prompt 13 puts a per-plan number on the plan row.
DEFAULT_MAX_SCREENS: Final = 50


@dataclass(frozen=True, slots=True)
class Entitlements:
    """What one caller may do. Rendered into ``GET /me`` by Prompt 12."""

    granted: frozenset[Feature]
    max_screens: int = DEFAULT_MAX_SCREENS

    def allows(self, feature: Feature) -> bool:
        return feature in self.granted

    def require(self, feature: Feature) -> None:
        """docs/07: a missing entitlement is a 402 carrying ``upgrade_url``."""
        if not self.allows(feature):
            raise payment_required(feature.value)

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {feature.value: self.allows(feature) for feature in Feature}
        payload["max_screens"] = self.max_screens
        return payload


async def _has_active_subscription(session: AsyncSession, user_id: int) -> bool:
    found = (
        await session.execute(
            select(Subscription.id)
            .where(Subscription.user_id == user_id, Subscription.status == "active")
            .limit(1)
        )
    ).scalar_one_or_none()
    return found is not None


async def entitlements_for(session: AsyncSession, principal: Principal) -> Entitlements:
    """The stub Prompt 13 replaces."""
    granted: set[Feature] = {Feature.SCREENER, Feature.BACKTESTS}
    if API_ACCESS_AVAILABLE:
        granted.add(Feature.API_ACCESS)

    if principal.user_id is not None and await _has_active_subscription(session, principal.user_id):
        granted |= GATED

    return Entitlements(granted=frozenset(granted))


async def current_entitlements(session: SessionDep, principal: PrincipalDep) -> Entitlements:
    return await entitlements_for(session, principal)


EntitlementsDep = Annotated[Entitlements, Depends(current_entitlements)]
