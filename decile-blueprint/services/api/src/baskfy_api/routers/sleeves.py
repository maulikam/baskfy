"""``/portfolios/{id}/sleeves`` and ``/allocation`` — a portfolio run as several screens (M34).

    GET  /portfolios/{id}/sleeves      how the portfolio is currently divided
    PUT  /portfolios/{id}/sleeves      replace the division
    GET  /portfolios/{id}/allocation   the rupee amounts that follow, and the stance behind them

The Rebalance Tracker answers *"which symbols changed"*. This answers the other question a person
with a crore actually asks: **how much goes where**. A sleeve is one slice with its own capital
and its own source — a saved screen, or ``manual`` for capital the owner runs themselves.

WHAT THIS RETURNS, AND WHAT IT REFUSES TO
-----------------------------------------
**Rupee amounts and target weights, and nothing that could be handed to a broker.** Turning an
amount into a number of units needs a market quote, and that turns a plan into a buy list — one
step from an order on a web surface, while execution stays in the desk console until D3 has a
written answer. The arithmetic lives in `baskfy_core.sleeves`, which receives no quote at all, so
this is structural rather than a promise.

THE MARKET STANCE IS REPORTED, NOT RECOMMENDED
----------------------------------------------
``/allocation`` reads the desk's current policy tier from the ``desk`` schema and reports it as a
fact: *"current stance R1 — the strategy caps equity at 100% under R1"*. It does **not** apply the
cap unless the caller asks (``apply_regime_cap=true``), and it never says the caller should.

That wording is not fastidiousness. Baskfy publishes no advice and is not SEBI-registered — it says
so on every page. A sentence urging the reader to deploy a sum is advice; *"under R2 the strategy
caps equity at 70%"* is a description of the strategy. Only the second belongs here.
`DECISIONS-MERGE.md` M34 carries the reasoning.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.db import SessionDep
from baskfy_api.problems import not_found
from baskfy_core.models import Portfolio, PortfolioSleeve, Screen
from baskfy_core.sleeves import MANUAL, SCREEN, SleeveSpec, allocate, cap_for_tier

router = APIRouter(tags=["portfolios"])

#: The schema M19's cutover put the desk's records in. Same read the desk pages use (M26).
DESK_SCHEMA: Final = "desk"

#: How many names a screen sleeve takes, in rank order. The tracker's own default.
DEFAULT_TOP_N: Final = 15

MAX_SLEEVES: Final = 12


class SleeveIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: str = Field(pattern="^(screen|manual)$")
    capital: Decimal = Field(ge=0)
    #: Required for a screen sleeve, refused for a manual one.
    screen_public_id: str | None = None
    top_n: int = Field(default=DEFAULT_TOP_N, ge=1, le=100)


class SleeveSetIn(BaseModel):
    """The whole division, validated as one thing.

    These rules live on the model rather than in the handler because `docs/07`'s error catalogue
    has no "validation" member and is pinned by a test — inventing one to describe a duplicate
    sleeve name would be changing a published contract to report a typo. FastAPI answers a failed
    model validation with 422 and the RFC 9457 body the rest of the API uses.
    """

    sleeves: list[SleeveIn]

    @model_validator(mode="after")
    def _check(self) -> SleeveSetIn:
        if len(self.sleeves) > MAX_SLEEVES:
            raise ValueError(f"a portfolio may have at most {MAX_SLEEVES} sleeves")
        names = [s.name.strip() for s in self.sleeves]
        if len(set(names)) != len(names):
            raise ValueError("two sleeves cannot share a name")
        for spec in self.sleeves:
            if spec.kind == SCREEN and not spec.screen_public_id:
                raise ValueError(f"{spec.name}: a screen sleeve must name a screen")
            if spec.kind == MANUAL and spec.screen_public_id:
                raise ValueError(f"{spec.name}: a manual sleeve cannot name a screen")
        return self


class SleeveOut(BaseModel):
    id: int
    name: str
    kind: str
    capital: Decimal
    screen_public_id: str | None = None
    screen_name: str | None = None
    top_n: int = DEFAULT_TOP_N


class SleeveListOut(BaseModel):
    sleeves: list[SleeveOut]
    total_capital: Decimal


class StanceOut(BaseModel):
    """The desk's current policy tier, as a fact."""

    tier: str
    label: str
    #: The equity cap this tier implies, as a percentage.
    equity_cap_pct: Decimal | None = None
    evaluated_at: str | None = None
    #: Plain-English sentences the desk wrote when it evaluated. Its reasoning, not ours.
    reasons: list[str] = []


class AllocationRowOut(BaseModel):
    symbol: str
    weight_pct: Decimal
    amount: Decimal


class SleeveAllocationOut(BaseModel):
    name: str
    kind: str
    capital: Decimal
    deployed: Decimal
    cash: Decimal
    screen_name: str | None = None
    rows: list[AllocationRowOut]


class AllocationOut(BaseModel):
    capital: Decimal
    deployed: Decimal
    cash: Decimal
    #: The cap that was applied, or None when sized at full capital.
    equity_cap_pct: Decimal | None = None
    applied_regime_cap: bool = False
    stance: StanceOut | None = None
    sleeves: list[SleeveAllocationOut]


TIER_LABELS: Final[dict[str, str]] = {
    "R1": "Risk-on",
    "R2": "Cautious",
    "R3": "Defensive",
    "R4": "Risk-off",
}


async def _portfolio(session: AsyncSession, portfolio_id: int, user_id: int) -> Portfolio:
    row = (
        await session.execute(
            select(Portfolio).where(Portfolio.id == portfolio_id, Portfolio.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("portfolio", str(portfolio_id))
    return row


async def _screens_by_public_id(session: AsyncSession, ids: list[str]) -> dict[str, Screen]:
    if not ids:
        return {}
    rows = (await session.execute(select(Screen).where(Screen.public_id.in_(ids)))).scalars().all()
    return {row.public_id: row for row in rows}


@router.get(
    "/portfolios/{portfolio_id}/sleeves",
    response_model=SleeveListOut,
    summary="How a portfolio is divided",
)
async def list_sleeves(
    portfolio_id: int, session: SessionDep, principal: AuthenticatedDep
) -> SleeveListOut:
    portfolio = await _portfolio(session, portfolio_id, principal.require_user())
    rows = (
        (
            await session.execute(
                select(PortfolioSleeve)
                .where(PortfolioSleeve.portfolio_id == portfolio.id)
                .order_by(PortfolioSleeve.sort_order, PortfolioSleeve.id)
            )
        )
        .scalars()
        .all()
    )
    screens = {
        row.id: row
        for row in (
            (
                await session.execute(
                    select(Screen).where(
                        Screen.id.in_([s.screen_id for s in rows if s.screen_id is not None])
                    )
                )
            )
            .scalars()
            .all()
        )
    }
    out = [
        SleeveOut(
            id=sleeve.id,
            name=sleeve.name,
            kind=sleeve.kind,
            capital=sleeve.capital,
            top_n=sleeve.top_n,
            screen_public_id=(
                screens[sleeve.screen_id].public_id
                if sleeve.screen_id is not None and sleeve.screen_id in screens
                else None
            ),
            screen_name=(
                screens[sleeve.screen_id].name
                if sleeve.screen_id is not None and sleeve.screen_id in screens
                else None
            ),
        )
        for sleeve in rows
    ]
    return SleeveListOut(sleeves=out, total_capital=sum((s.capital for s in out), Decimal(0)))


@router.put(
    "/portfolios/{portfolio_id}/sleeves",
    response_model=SleeveListOut,
    summary="Replace how a portfolio is divided",
)
async def replace_sleeves(
    portfolio_id: int,
    body: SleeveSetIn,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> SleeveListOut:
    """Replace the whole set, rather than patching one at a time.

    A portfolio's division is one decision — the slices have to add up to something the owner
    meant — so it is edited and saved whole. Patching sleeve 2 while sleeve 3 still holds last
    week's capital is how a total silently stops being the portfolio.
    """
    portfolio = await _portfolio(session, portfolio_id, principal.require_user())
    specs = body.sleeves
    screens = await _screens_by_public_id(
        session, [s.screen_public_id for s in specs if s.screen_public_id]
    )
    for spec in specs:
        if spec.kind == SCREEN and spec.screen_public_id not in screens:
            raise not_found("screen", spec.screen_public_id or "")

    await session.execute(
        delete(PortfolioSleeve).where(PortfolioSleeve.portfolio_id == portfolio.id)
    )
    for order, spec in enumerate(specs):
        session.add(
            PortfolioSleeve(
                portfolio_id=portfolio.id,
                name=spec.name.strip(),
                kind=spec.kind,
                screen_id=(
                    screens[spec.screen_public_id].id
                    if spec.kind == SCREEN and spec.screen_public_id
                    else None
                ),
                capital=spec.capital,
                top_n=spec.top_n,
                sort_order=order,
            )
        )
    await session.flush()
    return await list_sleeves(portfolio_id, session, principal)


async def _stance(session: AsyncSession) -> StanceOut | None:
    """The desk's most recent policy tier, read from the `desk` schema. Reported, not chosen."""
    row = (
        (
            await session.execute(
                text(
                    f"select policy_tier, created_at, reasons_json "
                    f'from "{DESK_SCHEMA}".regime_evaluations order by id desc limit 1'
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None

    tier = str(row["policy_tier"] or "").upper()
    raw = row["reasons_json"]
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    return StanceOut(
        tier=tier,
        label=TIER_LABELS.get(tier, tier),
        equity_cap_pct=cap_for_tier(tier),
        evaluated_at=str(row["created_at"]) if row["created_at"] else None,
        reasons=[str(r) for r in parsed] if isinstance(parsed, list) else [],
    )


@router.get(
    "/portfolios/{portfolio_id}/allocation",
    response_model=AllocationOut,
    summary="What each sleeve would hold, in rupees",
)
async def allocation(
    portfolio_id: int,
    session: SessionDep,
    principal: AuthenticatedDep,
    apply_regime_cap: Annotated[
        bool, Query(description="size the screen sleeves to the current stance's equity cap")
    ] = False,
) -> AllocationOut:
    """Amounts and weights. Never share counts — see the module docstring."""
    # Called for the 404 it raises, not for the row: this is the ownership check, and reading
    # somebody else's allocation must fail here rather than deeper.
    await _portfolio(session, portfolio_id, principal.require_user())
    listing = await list_sleeves(portfolio_id, session, principal)
    stance = await _stance(session)

    cap = stance.equity_cap_pct if (apply_regime_cap and stance is not None) else None
    specs: list[SleeveSpec] = []
    for sleeve in listing.sleeves:
        symbols = (
            await _screen_symbols(session, sleeve.screen_public_id, sleeve.top_n)
            if sleeve.kind == SCREEN and sleeve.screen_public_id
            else ()
        )
        specs.append(
            SleeveSpec(name=sleeve.name, kind=sleeve.kind, capital=sleeve.capital, symbols=symbols)
        )

    result = allocate(specs, equity_cap_pct=cap)
    by_name = {s.name: s for s in listing.sleeves}
    return AllocationOut(
        capital=result.capital,
        deployed=result.deployed,
        cash=result.cash,
        equity_cap_pct=result.equity_cap_pct,
        applied_regime_cap=cap is not None,
        stance=stance,
        sleeves=[
            SleeveAllocationOut(
                name=sleeve.name,
                kind=sleeve.kind,
                capital=sleeve.capital,
                deployed=sleeve.deployed,
                cash=sleeve.cash,
                screen_name=by_name[sleeve.name].screen_name if sleeve.name in by_name else None,
                rows=[
                    AllocationRowOut(
                        symbol=row.symbol, weight_pct=row.weight_pct, amount=row.amount
                    )
                    for row in sleeve.rows
                ],
            )
            for sleeve in result.sleeves
        ],
    )


async def _screen_symbols(session: AsyncSession, public_id: str, top_n: int) -> tuple[str, ...]:
    """The screen's top names on the latest published date, in rank order.

    The screen is **run**, not read from a stored snapshot: a sleeve is a standing instruction
    ("this much, from this screen"), so it must follow the screen when the screen changes. A
    stored list would answer last week's version of the question.

    ``run_screen`` returns the serialised response and sets ``result`` to ``None`` on a cache hit
    — the cached bytes *are* the answer, and rebuilding the object graph from them would only let
    the two drift. So the payload is parsed, which works on a hit and a miss alike.
    """
    from baskfy_api import screener as screener_service  # noqa: PLC0415 - avoids an import cycle
    from baskfy_core.screen_definition import ScreenDefinition  # noqa: PLC0415

    screen = (
        await session.execute(select(Screen).where(Screen.public_id == public_id))
    ).scalar_one_or_none()
    if screen is None:
        return ()

    definition = ScreenDefinition.model_validate(screen.definition)
    try:
        run = await screener_service.run_screen(session, definition)
    except Exception:
        # A sleeve whose screen cannot run right now shows no names and keeps its capital as
        # cash. Failing the whole allocation because one of four screens is unavailable would
        # hide the three that are fine.
        return ()

    parsed = json.loads(run.payload)
    rows = parsed.get("rows") if isinstance(parsed, dict) else None
    if not isinstance(rows, list):
        return ()
    return tuple(
        str(row["symbol"])
        for row in rows[:top_n]
        if isinstance(row, dict) and row.get("symbol") is not None
    )
