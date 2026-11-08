"""``/portfolios/{id}/sleeves`` and ``/allocation`` — a portfolio run as several sources (M34).

    GET  /portfolios/{id}/sleeves      how the portfolio is currently divided
    PUT  /portfolios/{id}/sleeves      replace the division
    GET  /portfolios/{id}/allocation   the amounts and unit counts that follow, and the stance

The Rebalance Tracker answers *"which symbols changed"*. This answers the other question a person
with a crore actually asks: **how much goes where**. A sleeve is one slice with its own capital
and its own source — a saved screen, a curated basket, or ``manual`` for capital the owner runs
themselves.

THE THREE KINDS, AND WHY ``basket`` HAD TO EXIST
------------------------------------------------
The landing page has promised "a momentum basket sitting beside a long-term core inside one
portfolio" for as long as there has been a landing page. Until migration 0019 that sentence was
not expressible: ``portfolio_sleeve.kind`` admitted ``screen`` and ``manual`` and nothing else, so
the basket half of the product and the portfolio half did not join. 0019 added ``basket``, paired
with ``basket_id``, and this router is where a person can now say it.

The pairing is exclusive in all three directions and the database owns it
(``portfolio_sleeve_source``): ``screen`` names a screen and no basket, ``basket`` names a basket
and no screen, ``manual`` names neither. :class:`SleeveSetIn` refuses the same combinations one
layer earlier, so the caller gets a sentence rather than an integrity error — and the accepted
kinds are read from :data:`SLEEVE_KINDS` rather than restated, so the API cannot drift from the
check constraint.

A basket sleeve names its basket by ``basket_slug``, the same public identifier ``/explore`` and
``/cb`` use, for the reason a screen sleeve names ``screen_public_id`` rather than ``screen.id``:
a surrogate key in a request body is a tenancy leak waiting to be probed. The row underneath
still stores ``basket_id``, which is what the constraint is written against.

WHAT THIS RETURNS, AND WHAT IT STILL REFUSES TO
-----------------------------------------------
Amounts, target weights **and whole unit counts** — and nothing that could be handed to a broker.

The unit column is new, and it is the narrower half of the line M34 drew. M34's wording was that
a unit count "is one step from an order list", and the module docstring of
:mod:`baskfy_core.sleeves` still says so: that module receives no quote and cannot produce one.
What changed is not the rule but who does the arithmetic. :mod:`baskfy_core.portfolio_units` is a
pure module that takes prices as an argument, and it opens with the distinction that matters —
a unit count is a *report* ("this sleeve targets 47 units"), which names no side, no product, no
order type and no venue. An order needs all four. Execution stays in the desk console behind an
explicit confirmation (non-negotiable #1); this router imports nothing from the execution package
and reaches no broker, and ``test_sleeves_are_not_orders.py`` fails if that ever changes.

A sleeve that says "₹4,00,000 of RELIANCE" and cannot say "132 shares" is asking its reader to do
the division themselves, on a phone calculator, from a price they looked up somewhere else. That
is not a safety property; it is a missing feature that looked like one.

WHERE THE TWO KINDS OF ROW GET THEIR NUMBERS, AND WHY THEY DIFFER
-----------------------------------------------------------------
Both unit counts come from :mod:`baskfy_core.portfolio_units`; nothing here divides money by a
price itself. What differs is which of that module's two entry points fits.

*Screen sleeves* keep the rupee amounts :func:`baskfy_core.sleeves.allocate` already computes —
an equal split of the sleeve's budget, rounded down to whole rupees. Those amounts are what the
surface has always shown and what this change must not move, so the unit count is taken from the
displayed amount: ``units_affordable(budget=row.amount, price=price)``. ``units x price <= amount``
then holds by construction, per row, and one unpriceable name blanks one unit cell rather than a
column.

*Basket sleeves* have real per-name weights (``cb_constituent.weight``), not an equal split, and
``allocate`` cannot size them: it returns early for any kind that is not ``screen``. So their
rupee rows are produced here by ``allocate_units``, together with their unit counts, from the same
call — ``amount`` is the row's ``target_value``, which *is* ``units x price``. The two columns
cannot disagree because they are one computation. ``deployed + cash == capital`` still holds,
which is the invariant :mod:`baskfy_core.sleeves` and ``basket_sizing`` also keep.

The visible consequence is deliberate: a basket row whose weighted share buys no whole unit shows
₹0.00 and 0 units, and its share of the capital appears in the sleeve's cash. That is what would
actually happen to the money.

WHY A MISSING PRICE PRODUCES ``null`` AND NEVER ``0``
-----------------------------------------------------
``allocate_units`` refuses loudly — every unpriceable name in one exception — because a pure
allocator that returned a half-answer would have its ``deployed`` read by a caller who forgot to
check the other field. An HTTP surface cannot refuse the whole portfolio because one of sixty
names has no bar: the other fifty-nine are fine and the reader needs them. So the refusal is
caught **here**, at the boundary, and turned into a partial answer that says which names it could
not count and why:

* ``units`` is ``null``, never ``0`` — zero is a number, and this is the absence of one;
* ``price`` is ``null`` for the same name;
* the sleeve carries ``unpriced`` (the names) and ``units_note`` (one sentence saying why);
* the rupee columns are unaffected for a screen sleeve, and fall back to the weighted split of
  capital for a basket sleeve. ``units_note`` says so when it happens.

AND WHY AN EMPTY SLEEVE ALSO HAS TO SAY WHY IT IS EMPTY
-------------------------------------------------------
The same rule one level up. A sleeve with no rows, ₹0 deployed and its whole capital in cash used
to mean two different things that rendered identically: *the screen ran and matched nothing*,
which is an answer, and *the screen could not be run*, which is an outage. The second used to be
caught here and dropped on the floor — no log, no field, no sentence — so ₹50 lakh could sit
against a broken screen and look like a screen with a strict filter.

``source_note`` carries that reason now, for a screen that cannot run, a screen that has been
deleted, and a basket with no published version. It stays ``None`` when the source answered
normally, including when the honest answer was "no names". The degradation itself is unchanged
and deliberate: one unavailable screen must not fail the allocation of the three beside it.

Prices are ``ohlcv_daily.close_raw`` — the exchange print, house rule 6's "display uses
``close_raw`` where the user expects a real price" — on or before the latest **published** trade
date, and no older than :data:`CLOSE_RAW_LOOKBACK_DAYS`. A three-month-old close is not a price
anybody can buy at, so a name whose last bar predates the floor is reported unpriced rather than
counted at a stale number.

EVERY MONEY FIELD IS TWO DECIMAL PLACES (HOUSE RULE 8)
------------------------------------------------------
``portfolio_sleeve.capital`` is ``numeric(18, 2)``; every amount, deployment, cash figure and
price in the response is rounded to the same two places by :func:`baskfy_core.precision.quantise`
before it leaves. Rounding at write time rather than at render time is what stops the API, the UI
and the CSV export from each rounding independently and disagreeing in the last digit.

THE MARKET STANCE IS REPORTED, NOT RECOMMENDED
----------------------------------------------
``/allocation`` reads the desk's current policy tier from the ``desk`` schema and reports it as a
fact: *"current stance R1 — the strategy caps equity at 100% under R1"*. It does **not** apply the
cap unless the caller asks (``apply_regime_cap=true``), and it never says the caller should. When
it is asked, the cap scales screen **and** basket sleeves — both are capital run to a published
rule — and never a manual sleeve, which is somebody's own money being run their own way.

That wording is not fastidiousness. Baskfy publishes no advice and is not SEBI-registered — it says
so on every page. A sentence urging the reader to deploy a sum is advice; *"under R2 the strategy
caps equity at 70%"* is a description of the strategy. Only the second belongs here.
`DECISIONS-MERGE.md` M34 carries the reasoning.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from decimal import ROUND_DOWN, Decimal
from typing import Annotated, Final, NamedTuple

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.curated_metrics_service import CLOSE_RAW_LOOKBACK_DAYS
from baskfy_api.curated_seed import resolve_sole_user_id
from baskfy_api.db import SessionDep
from baskfy_api.problems import Problem, ProblemType, not_found
from baskfy_core.models import (
    CbBasket,
    CbBasketVersion,
    CbConstituent,
    Instrument,
    OhlcvDaily,
    Portfolio,
    PortfolioSleeve,
    Screen,
)
from baskfy_core.models.accounts import SLEEVE_KINDS
from baskfy_core.portfolio_units import PriceRefusedError, allocate_units, units_affordable
from baskfy_core.precision import PRICE_DP, quantise
from baskfy_core.sleeves import (
    FULL_ALLOCATION_PCT,
    MANUAL,
    RUPEE,
    SCREEN,
    SleeveAllocation,
    SleeveSpec,
    allocate,
    cap_for_tier,
)

router = APIRouter(tags=["portfolios"])

#: The schema M19's cutover put the desk's records in. Same read the desk pages use (M26).
DESK_SCHEMA: Final = "desk"

#: How many names a screen sleeve takes, in rank order. The tracker's own default.
DEFAULT_TOP_N: Final = 15

MAX_SLEEVES: Final = 12

#: A sleeve whose names come from a curated basket's live version (0019). ``screen`` and
#: ``manual`` live in :mod:`baskfy_core.sleeves` because the pure allocator reasons about them;
#: this one is named here because that module deliberately does not size it — see the docstring.
BASKET: Final = "basket"

#: Built from the model's own tuple, so a kind the database accepts and this router does not
#: (or the reverse) is impossible rather than merely unlikely.
KIND_PATTERN: Final = "^(" + "|".join(SLEEVE_KINDS) + ")$"

#: A basket is visible to everyone once it is published; a PRIVATE one is the sole tenant's.
PUBLISHED: Final = "PUBLISHED"

ZERO_MONEY: Final = Decimal("0.00")


class ScreenNames(NamedTuple):
    """A screen sleeve's names, and the reason there are none when there are none.

    Two different facts used to arrive as the same empty tuple: "the screen ran and matched
    nothing" and "the screen could not be run". They render identically — no rows, ₹0 deployed —
    and they are not the same news for somebody with capital in that sleeve, so the second one
    now carries a sentence.
    """

    symbols: tuple[str, ...]
    note: str | None


class SleeveIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: str = Field(pattern=KIND_PATTERN)
    capital: Decimal = Field(ge=0)
    #: Required for a screen sleeve, refused for every other kind.
    screen_public_id: str | None = None
    #: Required for a basket sleeve, refused for every other kind.
    basket_slug: str | None = None
    top_n: int = Field(default=DEFAULT_TOP_N, ge=1, le=100)


def _check_source(spec: SleeveIn) -> None:
    """The three arms of the ``portfolio_sleeve_source`` constraint, one layer earlier.

    Spelled the same way round as the constraint and stated exhaustively — each kind requires its
    own source column and forbids the other one — rather than as "not the one I need", so a
    fourth kind cannot slip past by being unmentioned. The database is still the authority; this
    exists so the caller gets a sentence naming the sleeve instead of an integrity error naming a
    constraint.
    """
    if spec.kind == SCREEN:
        if not spec.screen_public_id:
            raise ValueError(f"{spec.name}: a screen sleeve must name a screen")
        if spec.basket_slug:
            raise ValueError(f"{spec.name}: a screen sleeve cannot name a basket")
    elif spec.kind == BASKET:
        if not spec.basket_slug:
            raise ValueError(f"{spec.name}: a basket sleeve must name a basket")
        if spec.screen_public_id:
            raise ValueError(f"{spec.name}: a basket sleeve cannot name a screen")
    elif spec.kind == MANUAL:
        if spec.screen_public_id:
            raise ValueError(f"{spec.name}: a manual sleeve cannot name a screen")
        if spec.basket_slug:
            raise ValueError(f"{spec.name}: a manual sleeve cannot name a basket")
    else:  # pragma: no cover - `kind` is pattern-checked against SLEEVE_KINDS on the field
        raise ValueError(f"{spec.name}: unknown sleeve kind {spec.kind!r}")


class SleeveSetIn(BaseModel):
    """The whole division, validated as one thing.

    These rules live on the model rather than in the handler because `docs/07`'s error catalogue
    has no "validation" member and is pinned by a test — inventing one to describe a duplicate
    sleeve name would be changing a published contract to report a typo. A failed model
    validation reaches the service's own handler, which renders it as the catalogue's
    ``invalid-screen-definition`` (400) with the offending message in ``errors[]``, in the RFC
    9457 body the rest of the API uses.
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
            _check_source(spec)
        return self


class SleeveOut(BaseModel):
    id: int
    name: str
    kind: str
    capital: Decimal
    screen_public_id: str | None = None
    screen_name: str | None = None
    basket_slug: str | None = None
    basket_name: str | None = None
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
    #: The close the units were counted at, rounded to storage precision. ``None`` when this name
    #: could not be priced — in which case ``units`` is ``None`` too and the sleeve says why.
    price: Decimal | None = None
    #: Whole units ``amount`` buys at ``price``, never rounded up. ``None`` — deliberately not
    #: ``0`` — when the name is unpriced: zero is a number, and this is the absence of one.
    units: int | None = None


class SleeveAllocationOut(BaseModel):
    name: str
    kind: str
    capital: Decimal
    deployed: Decimal
    cash: Decimal
    screen_name: str | None = None
    basket_slug: str | None = None
    basket_name: str | None = None
    rows: list[AllocationRowOut]
    #: Names in this sleeve with no usable close. Their ``units`` and ``price`` are ``null``.
    unpriced: list[str] = []
    #: One sentence saying why units are missing. ``None`` when every row has a count.
    units_note: str | None = None
    #: One sentence saying why this sleeve proposes no names, when the reason is a failure rather
    #: than an answer — a screen that could not be run, or a basket with no live version. ``None``
    #: when the sleeve's source spoke for itself, including when it genuinely matched nothing.
    source_note: str | None = None


class AllocationOut(BaseModel):
    capital: Decimal
    deployed: Decimal
    cash: Decimal
    #: The cap that was applied, or None when sized at full capital.
    equity_cap_pct: Decimal | None = None
    applied_regime_cap: bool = False
    stance: StanceOut | None = None
    sleeves: list[SleeveAllocationOut]
    #: The published trade date the closes were read on or before. ``None`` when no pipeline run
    #: has been published, in which case nothing can be priced and every ``units`` is ``null``.
    priced_as_of: dt.date | None = None
    #: Every unpriced name across every sleeve, sorted — so a UI can show one banner.
    unpriced: list[str] = []


TIER_LABELS: Final[dict[str, str]] = {
    "R1": "Risk-on",
    "R2": "Cautious",
    "R3": "Defensive",
    "R4": "Risk-off",
}


def _money(value: Decimal) -> Decimal:
    """House rule 8: round at write time, to the two places ``numeric(_, 2)`` stores.

    Every input here is already exact to two places or fewer, so this is a no-op on the value and
    a normalisation of the *presentation* — ``Decimal("333")`` and ``Decimal("333.00")`` are equal
    and serialise differently, and a table where one column shows paise and another does not is
    the disagreement house rule 8 exists to prevent.
    """
    rounded = quantise(value, PRICE_DP)
    if rounded is None:  # pragma: no cover - quantise returns None only for a non-finite input
        raise Problem(ProblemType.INTERNAL_ERROR, f"a money value was not finite: {value}")
    return rounded


def _pct(weight: Decimal) -> Decimal:
    """A share of a sleeve (``0.0625``) as the percentage the row displays (``6.25``)."""
    return _money(weight * FULL_ALLOCATION_PCT)


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


async def _baskets_by_slug(
    session: AsyncSession, slugs: list[str], user_id: int
) -> dict[str, CbBasket]:
    """The baskets this caller may build a sleeve from, keyed by slug.

    ``cb_basket`` has no owner column — ``routers/explore.py`` says so in as many words — so
    ``visibility`` is the whole of the tenancy boundary: a ``PUBLISHED`` basket is everyone's, and
    a ``PRIVATE`` one belongs to the sole tenant, who is the only account that can create one
    (``routers/curated_create.py``, ``routers/curated_from_screen.py``).

    A basket the caller may not use is reported as **absent**, not forbidden, and the sole-tenant
    lookup happens only for a basket that is actually private. Both halves matter:

    * 404 rather than 403 is the convention the rest of this service keeps, and the reason is
      that a 403 confirms the slug names something real. ``docs/07``'s catalogue has no
      ``forbidden`` member either.
    * resolving the sole tenant is a read that *fails* on a deployment where the account is not
      configured. Doing it unconditionally would turn "you asked for a published basket" into a
      503, so it is done only on the path that genuinely needs to know who the owner is.

    Archived baskets are excluded for everyone: an archived basket is a retired one, and a sleeve
    standing on it would be a standing instruction to follow something nobody maintains.
    """
    if not slugs:
        return {}
    rows = (
        (
            await session.execute(
                select(CbBasket).where(CbBasket.slug.in_(slugs), CbBasket.archived_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    visible: dict[str, CbBasket] = {}
    sole_user_id: int | None = None
    for row in rows:
        if row.visibility != PUBLISHED:
            if sole_user_id is None:
                sole_user_id = await resolve_sole_user_id(session)
            if sole_user_id != user_id:
                continue
        visible[row.slug] = row
    return visible


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
    baskets = {
        row.id: row
        for row in (
            (
                await session.execute(
                    select(CbBasket).where(
                        CbBasket.id.in_([s.basket_id for s in rows if s.basket_id is not None])
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
            capital=_money(sleeve.capital),
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
            basket_slug=(
                baskets[sleeve.basket_id].slug
                if sleeve.basket_id is not None and sleeve.basket_id in baskets
                else None
            ),
            basket_name=(
                baskets[sleeve.basket_id].name
                if sleeve.basket_id is not None and sleeve.basket_id in baskets
                else None
            ),
        )
        for sleeve in rows
    ]
    return SleeveListOut(
        sleeves=out, total_capital=_money(sum((s.capital for s in out), Decimal(0)))
    )


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
    user_id = principal.require_user()
    portfolio = await _portfolio(session, portfolio_id, user_id)
    specs = body.sleeves
    screens = await _screens_by_public_id(
        session, [s.screen_public_id for s in specs if s.screen_public_id]
    )
    baskets = await _baskets_by_slug(
        session, [s.basket_slug for s in specs if s.basket_slug], user_id
    )
    for spec in specs:
        if spec.kind == SCREEN and spec.screen_public_id not in screens:
            raise not_found("screen", spec.screen_public_id or "")
        if spec.kind == BASKET and spec.basket_slug not in baskets:
            raise not_found("basket", spec.basket_slug or "")

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
                basket_id=(
                    baskets[spec.basket_slug].id
                    if spec.kind == BASKET and spec.basket_slug
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
    """The desk's most recent policy tier, read from the `desk` schema. Reported, not chosen.

    ``to_regclass`` first, because the ``desk`` schema is created by M19's cutover and not by a
    migration: a deployment that has never run the cutover has no such table, and selecting from
    it raises ``UndefinedTable`` — which not only fails this read but aborts the surrounding
    transaction, so every query the allocation makes *after* it fails too. A portfolio's
    allocation is not the operator console's dependant; a deployment with no desk simply has no
    stance to report, which is the same ``None`` this returns when the table exists and is empty.

    ``to_regclass`` answers NULL for a missing relation instead of raising, so the check costs one
    catalogue lookup and leaves the transaction clean. Catching the exception instead would have
    to guess which ``ProgrammingError`` meant "no desk here" and could not undo the abort.
    """
    installed = await session.scalar(
        text(f"select to_regclass('{DESK_SCHEMA}.regime_evaluations')")
    )
    if installed is None:
        return None
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
    summary="What each sleeve would hold, in rupees and in whole units",
)
async def allocation(
    portfolio_id: int,
    session: SessionDep,
    principal: AuthenticatedDep,
    apply_regime_cap: Annotated[
        bool, Query(description="size the sourced sleeves to the current stance's equity cap")
    ] = False,
) -> AllocationOut:
    """Amounts, weights and whole unit counts. Never an order — see the module docstring."""
    # Called for the 404 it raises, not for the row: this is the ownership check, and reading
    # somebody else's allocation must fail here rather than deeper.
    await _portfolio(session, portfolio_id, principal.require_user())
    listing = await list_sleeves(portfolio_id, session, principal)
    stance = await _stance(session)

    cap = stance.equity_cap_pct if (apply_regime_cap and stance is not None) else None

    # One pass to learn every name the portfolio touches, so the closes are one query rather than
    # one per sleeve — and so a name held by two sleeves is priced identically in both.
    # Keyed by row id rather than by name: sleeve names are unique per portfolio today, but a
    # lookup that would silently mix two sleeves up if that constraint were ever relaxed is a
    # dependency worth not having.
    screen_symbols: dict[int, ScreenNames] = {}
    basket_weights: dict[int, dict[str, Decimal]] = {}
    for sleeve in listing.sleeves:
        if sleeve.kind == SCREEN and sleeve.screen_public_id:
            screen_symbols[sleeve.id] = await _screen_symbols(
                session, sleeve.screen_public_id, sleeve.top_n
            )
        elif sleeve.kind == BASKET and sleeve.basket_slug:
            basket_weights[sleeve.id] = await _basket_weights(session, sleeve.basket_slug)

    wanted = {symbol for names in screen_symbols.values() for symbol in names.symbols}
    wanted.update(symbol for weights in basket_weights.values() for symbol in weights)
    prices, priced_as_of = await _close_prices(session, sorted(wanted))

    # Basket sleeves reach `allocate` too, and come back untouched: it returns early for any kind
    # that is not `screen`. Passing them keeps the sleeve order and the manual-sleeve handling in
    # one place; their rows are then replaced with the ones `allocate_units` produced.
    specs = [
        SleeveSpec(
            name=sleeve.name,
            kind=sleeve.kind,
            capital=sleeve.capital,
            symbols=screen_symbols[sleeve.id].symbols if sleeve.id in screen_symbols else (),
        )
        for sleeve in listing.sleeves
    ]
    result = allocate(specs, equity_cap_pct=cap)

    out: list[SleeveAllocationOut] = []
    for sleeve, sized in zip(listing.sleeves, result.sleeves, strict=True):
        if sleeve.kind == BASKET:
            out.append(
                _basket_sleeve(sleeve, basket_weights.get(sleeve.id, {}), prices, priced_as_of, cap)
            )
        else:
            found = screen_symbols.get(sleeve.id)
            out.append(
                _sourced_sleeve(sleeve, sized, prices, priced_as_of, found.note if found else None)
            )

    capital = _money(sum((s.capital for s in out), Decimal(0)))
    deployed = _money(sum((s.deployed for s in out), Decimal(0)))
    return AllocationOut(
        capital=capital,
        deployed=deployed,
        cash=_money(capital - deployed),
        equity_cap_pct=result.equity_cap_pct,
        applied_regime_cap=cap is not None,
        stance=stance,
        sleeves=out,
        priced_as_of=priced_as_of,
        unpriced=sorted({symbol for s in out for symbol in s.unpriced}),
    )


def _units_note(unpriced: Sequence[str], as_of: dt.date | None) -> str | None:
    """One sentence for the reader, or ``None`` when every row has a unit count."""
    if not unpriced:
        return None
    if as_of is None:
        return (
            "No pipeline run has been published, so there is no closing price to count units at. "
            "Units are reported as null rather than zero: zero is a number, and this is the "
            "absence of one."
        )
    return (
        f"No usable closing price within {CLOSE_RAW_LOOKBACK_DAYS} days of {as_of.isoformat()} "
        f"for {', '.join(unpriced)} — no bar, or a print of zero or less, which buys nothing. "
        "Units are reported as null rather than zero for those names: zero is a number, and "
        "this is the absence of one."
    )


def _sourced_sleeve(
    sleeve: SleeveOut,
    sized: SleeveAllocation,
    prices: Mapping[str, Decimal],
    as_of: dt.date | None,
    source_note: str | None,
) -> SleeveAllocationOut:
    """A screen or manual sleeve: the rupee rows ``allocate`` produced, plus a unit column.

    ``sized`` is the :class:`baskfy_core.sleeves.SleeveAllocation` for this sleeve. Its amounts
    are not recomputed here — they are the numbers this surface has always shown, and the unit
    count is taken *from* them so the two columns cannot contradict each other. A manual sleeve
    has no rows at all, so it falls through this loop and gains no unit column, which is right:
    nothing was ever proposed for it.
    """
    rows: list[AllocationRowOut] = []
    unpriced: list[str] = []
    for row in sized.rows:
        amount = _money(row.amount)
        price = prices.get(row.symbol)
        if price is None:
            unpriced.append(row.symbol)
        rows.append(
            AllocationRowOut(
                symbol=row.symbol,
                weight_pct=_money(row.weight_pct),
                amount=amount,
                price=price,
                units=None if price is None else units_affordable(budget=amount, price=price),
            )
        )
    return SleeveAllocationOut(
        name=sleeve.name,
        kind=sleeve.kind,
        capital=_money(sleeve.capital),
        deployed=_money(sized.deployed),
        cash=_money(sized.cash),
        screen_name=sleeve.screen_name,
        rows=rows,
        unpriced=unpriced,
        units_note=_units_note(unpriced, as_of),
        source_note=source_note,
    )


def _basket_sleeve(
    sleeve: SleeveOut,
    weights: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    as_of: dt.date | None,
    cap: Decimal | None,
) -> SleeveAllocationOut:
    """A basket sleeve: rupee rows and unit counts from one ``allocate_units`` call.

    The fallback arm is the honest half of gate G5. ``allocate_units`` refuses the whole sleeve
    when any name is unpriceable — correctly, because its ``deployed`` would otherwise be short
    by exactly the missing names' worth — so the sleeve degrades to the money-only view it had
    before units existed: the weighted split of its budget, whole rupees, and ``units: null``
    with the reason attached. The exception is not swallowed; its message becomes the answer.
    """
    budget = sleeve.capital
    if cap is not None:
        # The same arithmetic `baskfy_core.sleeves._one` applies to a screen sleeve, reusing its
        # constants rather than restating them, so one cap cannot mean two things.
        budget = (sleeve.capital * cap / FULL_ALLOCATION_PCT).quantize(RUPEE, rounding=ROUND_DOWN)

    if not weights:
        # A basket with no live version, or one whose version is empty. Nothing to propose, and
        # the capital is reported as cash rather than quietly dropped from the totals -- with the
        # reason attached, for the same reason a broken screen now carries one.
        return SleeveAllocationOut(
            name=sleeve.name,
            kind=sleeve.kind,
            capital=_money(sleeve.capital),
            basket_slug=sleeve.basket_slug,
            basket_name=sleeve.basket_name,
            deployed=ZERO_MONEY,
            cash=_money(sleeve.capital),
            rows=[],
            source_note=(
                f"The basket {sleeve.basket_slug!r} has no published version with constituents, "
                "so this sleeve proposes no names and its capital is reported as cash."
            ),
        )

    ordered = sorted(weights.items(), key=lambda item: (-item[1], item[0]))
    try:
        counted = allocate_units(
            capital=budget,
            weights=dict(ordered),
            prices={symbol: prices.get(symbol) for symbol in weights},
        )
    except PriceRefusedError as refusal:
        rows = [
            AllocationRowOut(
                symbol=symbol,
                weight_pct=_pct(weight),
                amount=_money((budget * weight).quantize(RUPEE, rounding=ROUND_DOWN)),
                price=prices.get(symbol),
                units=None,
            )
            for symbol, weight in ordered
        ]
        deployed = _money(sum((row.amount for row in rows), Decimal(0)))
        # `str(refusal)` is A3's own sentence, which already names every offending symbol; it
        # is the fallback only for the impossible case of a refusal that named none.
        note = _units_note(list(refusal.symbols), as_of) or str(refusal)
        return SleeveAllocationOut(
            name=sleeve.name,
            kind=sleeve.kind,
            capital=_money(sleeve.capital),
            basket_slug=sleeve.basket_slug,
            basket_name=sleeve.basket_name,
            deployed=deployed,
            cash=_money(sleeve.capital - deployed),
            rows=rows,
            unpriced=list(refusal.symbols),
            units_note=(
                f"{note} The rupee amounts below are this sleeve's weighted split of its "
                "capital, which is what it showed before unit counts existed."
            ),
        )
    except ValueError as invalid:
        # `PriceRefusedError` is itself a ValueError and is handled above, so what reaches here
        # is the other refusal `allocate_units` makes: weights that do not fit inside one whole
        # sleeve. That is a defect in the stored basket rather than in the request, and it is
        # named rather than rendered as a bare 500 — an operator reading the log needs to know
        # which basket to go and look at.
        raise Problem(
            ProblemType.INTERNAL_ERROR,
            f"basket {sleeve.basket_slug!r} cannot be sized: {invalid}",
        ) from invalid

    rows = [
        AllocationRowOut(
            symbol=line.symbol,
            weight_pct=_pct(line.weight),
            amount=_money(line.target_value),
            price=line.price,
            units=line.target_units,
        )
        for line in counted.rows
    ]
    deployed = _money(counted.deployed)
    return SleeveAllocationOut(
        name=sleeve.name,
        kind=sleeve.kind,
        capital=_money(sleeve.capital),
        basket_slug=sleeve.basket_slug,
        basket_name=sleeve.basket_name,
        deployed=deployed,
        cash=_money(sleeve.capital - deployed),
        rows=rows,
    )


async def _basket_weights(session: AsyncSession, slug: str) -> dict[str, Decimal]:
    """``symbol -> weight`` for a basket's live version, or ``{}`` when it has none.

    The **live** version, not the one a sleeve was created against: a sleeve is a standing
    instruction ("this much, from this basket"), the same reading ``_screen_symbols`` takes of a
    screen sleeve. A frozen list would answer last quarter's version of the question.

    Two instruments sharing a symbol inside one version would collapse into a single dictionary
    key and silently drop somebody's weight, so the collision is refused instead — the same
    reasoning ``baskfy_core.portfolio_units._normalise_keys`` applies one layer down.
    """
    basket_id = await session.scalar(select(CbBasket.id).where(CbBasket.slug == slug))
    if basket_id is None:
        return {}
    version_id = await session.scalar(
        select(CbBasketVersion.id)
        .where(CbBasketVersion.basket_id == basket_id)
        .order_by(CbBasketVersion.version_no.desc())
        .limit(1)
    )
    if version_id is None:
        return {}
    rows = (
        await session.execute(
            select(Instrument.symbol, CbConstituent.weight)
            .join(Instrument, Instrument.id == CbConstituent.instrument_id)
            .where(CbConstituent.version_id == version_id)
        )
    ).all()
    weights: dict[str, Decimal] = {}
    for symbol, weight in rows:
        key = str(symbol).strip().upper()
        if key in weights:
            raise Problem(
                ProblemType.INTERNAL_ERROR,
                f"basket {slug!r} names {key} twice in its live version; one weight would be "
                "dropped silently, so the allocation is refused instead",
            )
        weights[key] = Decimal(weight)
    return weights


async def _close_prices(
    session: AsyncSession, symbols: Sequence[str]
) -> tuple[dict[str, Decimal], dt.date | None]:
    """``symbol -> close_raw`` at storage precision, and the date they were read on or before.

    ``close_raw`` and not ``close``: house rule 6 reserves the adjusted series for factors and
    puts the exchange print wherever the user expects a real price, and "how many units does this
    buy" is exactly that. One ``DISTINCT ON`` for the whole portfolio, floored at
    :data:`CLOSE_RAW_LOOKBACK_DAYS` — the floor keeps the planner off every year-chunk of the
    hypertable, and it is also the honesty rule: a bar older than that is not a price anybody can
    buy at, and its absence from this map is what makes the row report ``units: null``.

    A symbol with no bar simply does not appear. Nothing here invents a fallback price, because
    a made-up price produces a real-looking unit count.
    """
    from baskfy_api.screener import latest_published_date  # noqa: PLC0415 - avoids an import cycle

    as_of = await latest_published_date(session)
    if as_of is None or not symbols:
        return {}, as_of
    floor = as_of - dt.timedelta(days=CLOSE_RAW_LOOKBACK_DAYS)
    rows = (
        await session.execute(
            select(Instrument.symbol, OhlcvDaily.close_raw)
            .join(OhlcvDaily, OhlcvDaily.instrument_id == Instrument.id)
            .where(
                Instrument.symbol.in_(list(symbols)),
                OhlcvDaily.date >= floor,
                OhlcvDaily.date <= as_of,
            )
            .distinct(Instrument.symbol)
            # `Instrument.id` last so two instruments sharing a symbol resolve the same way twice.
            .order_by(Instrument.symbol, OhlcvDaily.date.desc(), Instrument.id)
        )
    ).all()
    prices: dict[str, Decimal] = {}
    for symbol, close_raw in rows:
        price = quantise(close_raw, PRICE_DP)
        # A suspended or unbacked instrument prints zero. `allocate_units` refuses that loudly and
        # `units_affordable` raises on it; leaving it out of the map routes it down the same
        # "unpriced" path as a missing bar, which is the honest report either way.
        if price is None or price <= 0:
            continue
        prices[str(symbol).strip().upper()] = price
    return prices, as_of


async def _screen_symbols(session: AsyncSession, public_id: str, top_n: int) -> ScreenNames:
    """The screen's top names on the latest published date, in rank order — and whether it ran.

    The screen is **run**, not read from a stored snapshot: a sleeve is a standing instruction
    ("this much, from this screen"), so it must follow the screen when the screen changes. A
    stored list would answer last week's version of the question.

    ``run_screen`` returns the serialised response and sets ``result`` to ``None`` on a cache hit
    — the cached bytes *are* the answer, and rebuilding the object graph from them would only let
    the two drift. So the payload is parsed, which works on a hit and a miss alike.

    A screen that cannot run still yields no names and keeps its sleeve's capital as cash —
    failing the whole allocation because one of four screens is unavailable would hide the three
    that are fine. What changed is that it no longer does so **silently**: ``note`` carries the
    reason to the response, because "no names, deployed ₹0" and "no names, deployed ₹0, and by
    the way this screen is broken" look identical on a screen and are not the same fact for
    somebody with ₹50 lakh in that sleeve. An empty ``note`` means the screen ran and genuinely
    matched nothing, which is a real answer rather than an outage.
    """
    from baskfy_api import screener as screener_service  # noqa: PLC0415 - avoids an import cycle
    from baskfy_core.screen_definition import ScreenDefinition  # noqa: PLC0415

    screen = (
        await session.execute(select(Screen).where(Screen.public_id == public_id))
    ).scalar_one_or_none()
    if screen is None:
        return ScreenNames(
            (),
            f"The screen {public_id!r} this sleeve draws from no longer exists, so it proposes "
            "no names and its capital is reported as cash.",
        )

    definition = ScreenDefinition.model_validate(screen.definition)
    try:
        run = await screener_service.run_screen(session, definition)
    except Exception as failure:
        # Reported, not swallowed: the reason travels to the caller in `note`.
        return ScreenNames(
            (),
            f"The screen {screen.name!r} could not be run just now "
            f"({type(failure).__name__}), so this sleeve proposes no names and its capital is "
            "reported as cash. The other sleeves are unaffected.",
        )

    parsed = json.loads(run.payload)
    rows = parsed.get("rows") if isinstance(parsed, dict) else None
    if not isinstance(rows, list):
        return ScreenNames(
            (),
            f"The screen {screen.name!r} returned a result this surface could not read, so this "
            "sleeve proposes no names and its capital is reported as cash.",
        )
    return ScreenNames(
        tuple(
            str(row["symbol"])
            for row in rows[:top_n]
            if isinstance(row, dict) and row.get("symbol") is not None
        ),
        None,
    )
