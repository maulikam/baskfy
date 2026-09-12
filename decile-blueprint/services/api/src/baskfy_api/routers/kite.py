"""``/baskets/plan/kite`` — the Zerodha Publisher hand-off payload.

    GET /baskets/plan/kite       the desk's latest plan, shaped as a Kite Publisher basket
    GET /explore/{slug}/kite     a curated basket at a chosen amount, likewise

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

import datetime as dt
from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, select

from baskfy_api.auth import AuthenticatedDep, settings_for
from baskfy_api.db import SessionDep
from baskfy_api.invoices import today_ist
from baskfy_api.kite_basket import build_basket
from baskfy_api.problems import Problem, ProblemType, not_found
from baskfy_api.routers.baskets import latest_plan
from baskfy_api.settings import Settings
from baskfy_core.curated_plans import build_invest_plan
from baskfy_core.models import CbBasket, CbBasketVersion, CbConstituent, Instrument, OhlcvDaily

router = APIRouter(tags=["baskets"])


def _settings(request: Request) -> Settings:
    return settings_for(request)


SettingsDep = Annotated[Settings, Depends(_settings)]


def _published() -> tuple[ColumnElement[bool], ...]:
    """Same visibility gate as explore: archived-out and non-PUBLISHED baskets are 404."""
    return (CbBasket.archived_at.is_(None), CbBasket.visibility == "PUBLISHED")


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
    #: The same items split into baskets Kite will accept — at most ten each, which is Kite's
    #: documented limit (https://kite.trade/docs/connect/v3/publisher/). The ordinary momentum
    #: basket is fifteen names, so this is the normal path and not an edge case: the browser
    #: renders one form per batch rather than posting a basket Kite would refuse.
    batches: list[list[KiteBasketItemOut]] = Field(default_factory=list)
    #: Symbols the guard or the sanity checks dropped. Surfaced so the interface can say "nine of
    #: ten, and here is the tenth" rather than quietly presenting a short basket as a whole one.
    excluded: list[str] = Field(default_factory=list)


@router.get(
    "/baskets/plan/kite",
    response_model=KiteBasketOut,
    summary="The latest plan as a Kite Publisher basket (hand-off, not execution)",
)
async def plan_as_kite_basket(
    session: SessionDep, settings: SettingsDep, principal: AuthenticatedDep
) -> KiteBasketOut:
    """Read the desk's latest plan and shape it for Kite's basket form.

    404 when the desk has recorded no plans — inherited from `latest_plan`, which is called rather
    than re-queried so there is one piece of SQL reading `rebalance_versions` and not two.
    """
    del principal
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
        batches=[
            [KiteBasketItemOut(**item.as_dict()) for item in batch] for batch in payload.batches
        ],
        excluded=list(payload.excluded),
    )


#: How far back to look for a close when the latest trading day has none for an instrument.
#: The same floor `curated_metrics_service` uses, and for the same reason: without it the planner
#: scans every year-chunk of the hypertable.
CLOSE_LOOKBACK_DAYS: Final = 30


@router.get(
    "/explore/{slug}/kite",
    response_model=KiteBasketOut,
    summary="A curated basket at a chosen amount, as a Kite Publisher basket",
)
async def basket_as_kite_basket(
    slug: str,
    amount: Annotated[Decimal, Query(gt=0, description="Rupees to invest")],
    session: SessionDep,
    settings: SettingsDep,
    principal: AuthenticatedDep,
) -> KiteBasketOut:
    """Turn a basket's *weights* into *quantities* for a given amount, then into a Kite basket.

    WHY THIS EXISTS BESIDE `/baskets/plan/kite`
    -------------------------------------------
    That one hands over **the desk's** rebalance plan, which is the desk owner's orders for the
    desk owner's book. Offering it to a signed-in stranger was always the wrong semantics — it is
    the finding recorded as `NEEDS-MAULIK.md` §27. This is the one a user actually wants: *this
    basket, this much money, what do I buy?*

    NOTHING HERE IS RE-DERIVED
    --------------------------
    Weights → quantities is `baskfy_core.curated_plans.build_invest_plan`, the same pure function
    `POST /cb/plans/invest` calls. It already handles the arithmetic that looks trivial and is not:
    whole shares only, the remainder that cannot be spent, and a weight whose share price exceeds
    its slice of the amount. Writing a second version of that here would have produced a basket
    that disagreed with the plan preview the user had just been shown, in rupees.

    Prices are the latest `close_raw` on or before today — the exchange print, per house rule 6,
    because this figure becomes a share count somebody buys. They are a *reference*: the basket
    goes to Kite as MARKET orders and the user sees live prices there before confirming.

    Visibility is the same `_published()` gate every explore route uses (AUDIT 2.1): a PRIVATE
    draft or an unpublished basket is a 404, never a hand-off payload.
    """
    del principal
    basket = await session.scalar(
        select(CbBasket).where(CbBasket.slug == slug.strip(), *_published())
    )
    if basket is None:
        raise not_found("basket", slug)

    version = await session.scalar(
        select(CbBasketVersion)
        .where(CbBasketVersion.basket_id == basket.id)
        .order_by(CbBasketVersion.version_no.desc())
        .limit(1)
    )
    if version is None:
        raise Problem(ProblemType.NOT_FOUND, f"basket {slug!r} has no published version.")

    rows = (
        await session.execute(
            select(Instrument.id, Instrument.symbol, CbConstituent.weight)
            .join(CbConstituent, CbConstituent.instrument_id == Instrument.id)
            .where(CbConstituent.version_id == version.id)
        )
    ).all()
    if not rows:
        raise Problem(ProblemType.NOT_FOUND, f"basket {slug!r} has no constituents.")

    weights = {str(symbol): Decimal(weight) for _, symbol, weight in rows}
    by_id = {int(iid): str(symbol) for iid, symbol, _ in rows}

    today = today_ist()
    price_rows = (
        await session.execute(
            select(OhlcvDaily.instrument_id, OhlcvDaily.close_raw)
            .distinct(OhlcvDaily.instrument_id)
            .where(
                OhlcvDaily.instrument_id.in_(list(by_id)),
                OhlcvDaily.date >= today - dt.timedelta(days=CLOSE_LOOKBACK_DAYS),
                OhlcvDaily.date <= today,
            )
            .order_by(OhlcvDaily.instrument_id, OhlcvDaily.date.desc())
        )
    ).all()
    prices = {by_id[int(iid)]: Decimal(close) for iid, close in price_rows}

    missing = sorted(set(weights) - set(prices))
    if missing:
        # Named, not counted. "Some prices are unavailable" leaves a reader unable to tell whether
        # the basket is stale, delisted or simply not ingested yet.
        raise Problem(
            ProblemType.PIPELINE_DEGRADED,
            f"no recent close for {', '.join(missing)}, so a share count cannot be worked out.",
        )

    try:
        plan = build_invest_plan(
            target_weights=weights,
            prices=prices,
            amount=amount,
            now=dt.datetime.now(tz=dt.UTC),
        )
    except ValueError as error:
        raise Problem(ProblemType.BAD_REQUEST, str(error)) from error

    # `DeskPlan` is a `TypedDict`, so the legs are subscripted rather than attributes — the same
    # way `routers/curated_plans._preview_from_core` reads them.
    payload = build_basket(
        [(leg["symbol"], leg["side"], leg["quantity"]) for leg in plan["legs"]],
        api_key=settings.kite_publisher_api_key,
    )
    return KiteBasketOut(
        url=payload.url,
        api_key=payload.api_key,
        configured=payload.configured,
        items=[KiteBasketItemOut(**item.as_dict()) for item in payload.items],
        batches=[
            [KiteBasketItemOut(**item.as_dict()) for item in batch] for batch in payload.batches
        ],
        excluded=list(payload.excluded),
    )
