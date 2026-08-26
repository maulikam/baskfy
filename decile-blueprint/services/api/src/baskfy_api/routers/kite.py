"""``/baskets/plan/kite`` — the Zerodha Publisher hand-off payload.

    GET /baskets/plan/kite   the desk's latest plan, shaped as a Kite Publisher basket

ONE GET, AND IT WILL STAY ONE GET
---------------------------------
This route reads. It returns the `api_key` and the `data` array that the *user's browser* posts
to `kite.zerodha.com/connect/basket`, where Kite renders the basket inside the user's own session
for them to review and confirm. Nothing in this file talks to a broker, holds a credential, or
produces a fill.

That distinction is the whole design. The desk's non-negotiable #1 is that orders fire only from
`POST /execute` with `confirm=true`; law #2 is that `packages/execution` is the only path to an
order. Neither is bent here, because there is no order — there is a suggestion, handed to the
person whose account it is, in an application we do not control, which they then act on or do not.
`baskfy_core.broker_connections.BROKER_OAUTH_REVIEW` states the posture this implements: "publish
baskets; user executes in their own broker account after confirm."

`services/api/tests/test_baskets_readonly.py` asserts the shape of that promise over this file's
source, the same way it does for `routers/baskets.py`.

NO INSTRUMENT IS FILTERED ON POLICY GROUNDS
-------------------------------------------
The desk's untouchable-instrument list (non-negotiable #7) is **not** applied. It protects one
account — the desk owner's — from a momentum strategy selling his own long-term holdings, and it
has no business deciding what somebody else may trade in their own Kite session. See
`baskfy_api.kite_basket` for the full reasoning, and `docs/DECISIONS-MERGE.md` M47 for the call.
The desk's own guard is untouched and still absolute for the account it was written for.

WHY IT IS A SERVER ROUTE AND NOT TWENTY LINES OF TYPESCRIPT
------------------------------------------------------------
Because the plan is read here, and the payload the browser posts should be built once, beside the
data, in the language the rest of the plan lives in — not re-derived in a second place where the
field names Kite expects can drift out of step with the ones the API produced.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from baskfy_api.auth import settings_for
from baskfy_api.db import SessionDep
from baskfy_api.kite_basket import build_basket
from baskfy_api.routers.baskets import latest_plan
from baskfy_api.settings import Settings

router = APIRouter(tags=["baskets"])


def _settings(request: Request) -> Settings:
    return settings_for(request)


SettingsDep = Annotated[Settings, Depends(_settings)]


class KiteBasketItemOut(BaseModel):
    """One row, in Kite's field names rather than ours — it is Kite's form that receives it."""

    variety: str
    tradingsymbol: str
    exchange: str
    transaction_type: str
    order_type: str
    quantity: int
    product: str
    readonly: bool


class KiteBasketOut(BaseModel):
    """Everything the browser needs to render the form, and nothing it does not."""

    #: Where to post. Returned rather than hard-coded in the client so there is one definition.
    url: str
    #: The Publisher key. Public by design — it travels in the form itself.
    api_key: str
    #: False when no key is configured. The button is not rendered; it is never rendered dead.
    configured: bool
    items: list[KiteBasketItemOut]
    #: Symbols the guard or the sanity checks dropped. Surfaced so the interface can say "nine of
    #: ten, and here is the tenth" rather than quietly presenting a short basket as a whole one.
    excluded: list[str] = Field(default_factory=list)


@router.get(
    "/baskets/plan/kite",
    response_model=KiteBasketOut,
    summary="The latest plan as a Kite Publisher basket (hand-off, not execution)",
)
async def plan_as_kite_basket(session: SessionDep, settings: SettingsDep) -> KiteBasketOut:
    """Read the desk's latest plan and shape it for Kite's basket form.

    404 when the desk has recorded no plans — inherited from `latest_plan`, which is called rather
    than re-queried so there is one piece of SQL reading `rebalance_versions` and not two.
    """
    plan = await latest_plan(session)
    payload = build_basket(
        [(o.symbol, o.side, o.planned_qty) for o in plan.orders],
        api_key=settings.kite_publisher_api_key,
    )
    return KiteBasketOut(
        url=payload.url,
        api_key=payload.api_key,
        configured=payload.configured,
        items=[KiteBasketItemOut(**item.as_dict()) for item in payload.items],
        excluded=list(payload.excluded),
    )
