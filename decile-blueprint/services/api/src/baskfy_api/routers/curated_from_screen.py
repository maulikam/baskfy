"""``POST /cb/baskets/from-screen`` — save a screen as an investable basket (SB1).

The screener answers *"which stocks"*. This route answers the two questions it does not: how much
money goes in, and across how many of its names. The arithmetic is
:mod:`baskfy_core.basket_sizing`; everything here is the I/O around it.

**THE SERVER RUNS THE SCREEN ITSELF, AND THAT IS THE WHOLE POINT.** The obvious shape for this
endpoint would have been to accept the symbols and weights the browser already computed — the web
layer materializes a basket on every screen run, so they are sitting right there. It would also
have made ``source = 'SCREEN'`` a lie: a caller could post any list at all and have it recorded as
the output of a rule it never came from. So the body names a *screen*, never a holding, and the
constituents are whatever that screen returns when this route runs it. The browser's numbers are a
preview; these are the record.

The client's sizing preview and this route must agree, which is why both read the same profile
table — the web copy is ``apps/web/src/lib/basket/profiles.ts`` and
``test_holding_profile_parity.py`` fails if the two drift.

NO ORDER PATH. A basket and a version are catalog rows. Turning them into orders needs the desk
console, exactly as it does for ``POST /cb/baskets``; ``test_baskets_readonly.py`` is where that
gate is decided and this route is inside it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.db import SessionDep
from baskfy_api.entitlements import EntitlementsDep
from baskfy_api.problems import Problem, ProblemType, not_found
from baskfy_api.routers.curated_create import slug_for
from baskfy_api.screener import current_data_version, execute_screen, resolve_as_of
from baskfy_core.basket_sizing import (
    MAX_HOLDINGS,
    MIN_HOLDINGS,
    Candidate,
    HoldingProfile,
    SizedBasket,
    WeightMethod,
    cash_pct_for_tier,
    size_basket,
)
from baskfy_core.curated_baskets import MANAGER_SLUG_MAULIK
from baskfy_core.models import (
    CbBasket,
    CbBasketVersion,
    CbConstituent,
    CbManager,
    Instrument,
    Screen,
)
from baskfy_core.screen_definition import ScreenDefinition
from baskfy_core.screener import resolve_columns

router = APIRouter(tags=["curated-create"])

#: The price column every screen result carries by default (house rule 6: the exchange print).
PRICE_COLUMN = "close_raw"
#: 1-year volatility — default result column, required by ``INV_VOL``.
VOL_COLUMN = "vol_12m"
#: The projected ranking-factor column every screen result carries (docs/06).
SCORE_COLUMN = "sorting_factor"


class CustomWeightIn(BaseModel):
    """One investor-typed weight. Applied to a name the screen already selected — never instead."""

    symbol: str = Field(min_length=1, max_length=32)
    weight: Decimal = Field(gt=0)


class FromScreenIn(BaseModel):
    """What the investor chose. Everything except the screen and the amount is optional."""

    screen_public_id: str = Field(min_length=1, max_length=64)
    amount: Decimal = Field(gt=0)
    name: str | None = Field(default=None, max_length=120)
    description_md: str | None = None
    #: Suggests the name count. Ignored when ``holdings`` is given.
    profile: HoldingProfile | None = None
    #: The investor's own count, which always beats the profile's suggestion.
    holdings: int | None = Field(default=None, ge=MIN_HOLDINGS, le=MAX_HOLDINGS)
    #: The desk's current exposure tier (R1-R4), which decides the cash share. Not a name count —
    #: see ``baskfy_core.basket_sizing`` on why the two are kept apart.
    exposure_tier: str | None = Field(default=None, max_length=8)
    #: Explicit cash percent (0-95). Beats :func:`cash_pct_for_tier` when set (SB7).
    cash_pct: Decimal | None = Field(default=None, ge=0, le=95)
    #: How to split the deployed money. Defaults to equal, so existing clients do not move.
    method: WeightMethod | None = None
    #: Required when ``method`` is ``CUSTOM``. Keyed by symbol; cannot name a holding the
    #: screen did not select (SB1.3 / SB4). Named ``custom_weights`` on purpose — a bare
    #: ``weights`` field would look like the caller is supplying the basket.
    custom_weights: list[CustomWeightIn] | None = None


class FromScreenHoldingOut(BaseModel):
    """One sized name. Prefixed because a bare ``HoldingOut`` already exists in
    ``baskfy_api.schemas``, and two schemas of one name make the generated TypeScript client
    fall back to module-qualified names for both."""

    rank: int
    symbol: str
    instrument_id: int
    weight: Decimal
    weight_pct_of_amount: Decimal
    amount: Decimal


class FromScreenOut(BaseModel):
    id: int
    slug: str
    name: str
    visibility: str
    type: str
    source: str
    version_no: int
    label: str
    screen_public_id: str
    as_of: dt.date
    amount: Decimal
    deployed: Decimal
    cash: Decimal
    cash_pct: Decimal
    profile: HoldingProfile | None
    holdings_overridden: bool
    minimum_amount: Decimal | None
    fundable: bool
    method: WeightMethod
    holdings: list[FromScreenHoldingOut]


def _price(row_values: object) -> Decimal | None:
    """The exchange print as a ``Decimal``, or ``None`` when the row does not carry one.

    A missing or unparseable price is not an error: it only means this name cannot contribute to
    the minimum-amount floor. Inventing a number here would produce a threshold the reader
    would trust.
    """
    if row_values is None:
        return None
    try:
        price = Decimal(str(row_values))
    except (InvalidOperation, ValueError):
        return None
    return price if price > 0 else None


def _custom_map(entries: list[CustomWeightIn] | None) -> dict[str, Decimal] | None:
    """Symbol → weight. Duplicates are a bad request, not a last-write-wins surprise."""
    if entries is None:
        return None
    out: dict[str, Decimal] = {}
    for entry in entries:
        symbol = entry.symbol.strip().upper()
        if symbol in out:
            raise Problem(
                ProblemType.INVALID_SCREEN_DEFINITION,
                f"custom weights name {symbol} more than once",
            )
        out[symbol] = entry.weight
    return out


async def _sized(
    session: SessionDep,
    definition: ScreenDefinition,
    body: FromScreenIn,
) -> tuple[SizedBasket, dt.date]:
    """Run the screen and size the result. Raises a 400 for an unfillable request."""
    resolution = await resolve_as_of(session, definition.historical_date)
    data_version = await current_data_version(session)
    columns = list(resolve_columns())
    result = await execute_screen(
        session,
        definition,
        as_of=resolution.as_of,
        data_version=data_version,
        columns=columns,
        requested_as_of=resolution.requested,
        # Bounded by the largest basket anyone may hold: this route never needs the other
        # 3,950 rows docs/03 permits, and pulling them would be work nobody reads.
        limit=MAX_HOLDINGS,
    )
    score_key = result.sorting_factor.key
    candidates = [
        Candidate(
            symbol=row.symbol,
            rank=row.rank,
            price=_price(row.values.get(PRICE_COLUMN)),
            score=_price(row.values.get(SCORE_COLUMN, row.values.get(score_key))),
            vol=_price(row.values.get(VOL_COLUMN)),
        )
        for row in result.rows
    ]
    try:
        resolved_cash = (
            body.cash_pct
            if body.cash_pct is not None
            else cash_pct_for_tier(body.exposure_tier)
        )
        sized = size_basket(
            candidates,
            amount=body.amount,
            profile=body.profile,
            holdings=body.holdings,
            cash_pct=resolved_cash,
            method=body.method,
            custom_weights=_custom_map(body.custom_weights),
        )
    except ValueError as exc:
        raise Problem(ProblemType.INVALID_SCREEN_DEFINITION, str(exc)) from exc
    return sized, result.as_of


@router.post(
    "/cb/baskets/from-screen",
    response_model=FromScreenOut,
    status_code=status.HTTP_201_CREATED,
    summary="Save a screen as a basket",
)
async def create_basket_from_screen(
    body: FromScreenIn,
    session: SessionDep,
    principal: AuthenticatedDep,
    entitlements: EntitlementsDep,
) -> FromScreenOut:
    """Cut a ``source = 'SCREEN'`` basket and its GENESIS version from a saved screen."""
    await scoped_sole_user_id(session, principal.user_id)

    screen = (
        await session.execute(select(Screen).where(Screen.public_id == body.screen_public_id))
    ).scalar_one_or_none()
    # An example screen (``user_id IS NULL``) is readable by everyone and is a legitimate source;
    # somebody else's private screen is reported absent rather than forbidden, matching
    # ``routers/screens.py`` — the API does not confirm that another user's screen id exists.
    if screen is None or not (screen.user_id is None or screen.user_id == principal.user_id):
        raise not_found("screen", body.screen_public_id)

    definition = ScreenDefinition.model_validate(screen.definition)
    entitlements.require_universe(definition.index)

    sized, as_of = await _sized(session, definition, body)

    symbols = [holding.symbol for holding in sized.holdings]
    instruments = (
        await session.scalars(
            select(Instrument).where(
                func.upper(Instrument.symbol).in_(symbols),
                Instrument.is_active.is_(True),
            )
        )
    ).all()
    by_symbol = {str(row.symbol).upper(): row for row in instruments}
    missing = [symbol for symbol in symbols if symbol not in by_symbol]
    if missing:
        # The screen ranked a name this table cannot resolve, which is a data problem on our side
        # rather than a bad request: the caller chose a screen, not a symbol list.
        raise Problem(
            ProblemType.INTERNAL_ERROR,
            f"the screen ranked symbols with no active instrument row: {', '.join(missing)}",
        )

    manager_id = await session.scalar(
        select(CbManager.id).where(CbManager.slug == MANAGER_SLUG_MAULIK)
    )
    if manager_id is None:
        raise Problem(ProblemType.INTERNAL_ERROR, "curated manager seed is missing")

    today = dt.datetime.now(tz=dt.UTC).date()
    name = (body.name or screen.name).strip()
    basket = CbBasket(
        slug=await slug_for(session, name),
        name=name,
        manager_id=int(manager_id),
        type="STOCK",
        access="FREE",
        visibility="PRIVATE",
        categories=["screen"],
        description_md=body.description_md,
        rationale_md=None,
        # A screen is a live rule, so the basket it produced can be re-cut whenever its owner
        # wants — which is a decision, not a schedule this route is entitled to set.
        rebalance_frequency="NEED_BASIS",
        benchmark_instrument_id=None,
        launched_at=today,
        next_review_at=None,
        source="SCREEN",
        scan_strategy_key=None,
        source_screen_id=int(screen.id),
        archived_at=None,
    )
    session.add(basket)
    await session.flush()

    version = CbBasketVersion(
        basket_id=basket.id,
        version_no=1,
        effective_date=as_of,
        label="GENESIS",
        added_count=len(symbols),
        removed_count=0,
        notes_md=(
            f"Cut from screen {screen.public_id!r} as of {as_of.isoformat()}, "
            f"{sized.method} weights."
        ),
        source_scan_run_id=None,
    )
    session.add(version)
    await session.flush()

    out: list[FromScreenHoldingOut] = []
    for holding in sized.holdings:
        instrument = by_symbol[holding.symbol]
        session.add(
            CbConstituent(
                version_id=version.id,
                instrument_id=instrument.id,
                segment="Equity",
                weight=holding.weight,
            )
        )
        out.append(
            FromScreenHoldingOut(
                rank=holding.rank,
                symbol=holding.symbol,
                instrument_id=int(instrument.id),
                weight=holding.weight,
                weight_pct_of_amount=holding.weight_pct_of_amount,
                amount=holding.amount,
            )
        )

    await session.commit()
    await session.refresh(basket)
    await session.refresh(version)

    return FromScreenOut(
        id=int(basket.id),
        slug=basket.slug,
        name=basket.name,
        visibility=basket.visibility,
        type=basket.type,
        source=basket.source,
        version_no=int(version.version_no),
        label=version.label,
        screen_public_id=screen.public_id,
        as_of=as_of,
        amount=sized.amount,
        deployed=sized.deployed,
        cash=sized.cash,
        cash_pct=sized.cash_pct,
        profile=sized.profile,
        holdings_overridden=sized.holdings_overridden,
        minimum_amount=sized.minimum,
        fundable=sized.is_fundable,
        method=sized.method,
        holdings=out,
    )
