"""``/cb/trending`` — the ranked lists the home surface shows (SC9).

Read-only, and deliberately **not** tenant-scoped in its aggregates. Popularity is a
platform-wide question — "how many people watchlisted this" is not a number that can be asked
one tenant at a time — so the counts below run over every row, while
:func:`baskfy_api.curated_tenant.scoped_sole_user_id` still gates *who may ask*. What stops
those aggregates from being a disclosure about individuals is
:data:`baskfy_core.curated_trending.MIN_POPULATION`, applied in the pure layer: below five
distinct people a popularity list withholds itself instead of publishing.

Computed on request, not read from a snapshot. The catalog is small enough that six grouped
counts cost less than the machinery of an EOD table plus a Beat job would, and a live answer
cannot go stale. ``docs/DECISIONS-MERGE.md`` HOME2 records that choice and how to reverse it:
the pure module this router calls is the same one a persisted snapshot would fill.

**No order route.** Nothing here reaches ``packages/execution`` — Track C / PACK.2.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.db import SessionDep
from baskfy_core.curated_metrics import return_convention_fields
from baskfy_core.curated_trending import (
    MIN_ENTRIES,
    MIN_POPULATION,
    TrendingCandidate,
    TrendingList,
    TrendingPopulation,
    rank_lists,
)
from baskfy_core.models import (
    CbBasket,
    CbBasketVersion,
    CbInvestment,
    CbMetrics,
    CbOrderBatch,
    CbWatchlistItem,
)

router = APIRouter(tags=["curated-trending"])

#: ``cb_investment.status`` for a holding that still exists. An exited investment is history,
#: not a vote, so "most invested in" must not count it.
_ACTIVE: str = "ACTIVE"

#: ``cb_order_batch.kind`` values that put money in. SELL and EXIT take it out, and CUSTOMIZE
#: moves it sideways; counting any of them as an inflow would flatter the number.
_INFLOW_KINDS: tuple[str, ...] = ("BUY",)


class TrendingEntryOut(BaseModel):
    rank: int
    basket_slug: str
    basket_name: str
    metric_value: Decimal | None = None
    metric_date: dt.date | None = None
    #: Already rounded by the pure layer — house rule 8, so no surface can re-round it.
    metric_display: str


class TrendingListOut(BaseModel):
    key: str
    title: str
    #: The sentence the UI must render beside the title: what this list actually ranks.
    ranks_by: str
    metric_label: str
    metric_kind: str
    population_based: bool
    population: int | None = None
    eligible: int
    entries: list[TrendingEntryOut]
    withheld_reason: str | None = None
    withheld_note: str | None = None
    #: True when the ranked metric is a price return, so the reader is owed the M39.3 sentence.
    price_return_caveat: bool


class TrendingOut(BaseModel):
    items: list[TrendingListOut]
    count: int
    #: How many published baskets the whole ranking was computed over.
    catalog_size: int
    min_entries: int
    min_population: int
    #: The price-return caveat, machine-readable, from ``baskfy_core.curated_metrics``.
    return_convention: str
    dividends_included: bool
    return_convention_note: str


def _visible_basket_ids() -> Select[tuple[int]]:
    """Ids of baskets that may be shown at all — the same predicate ``/explore`` uses.

    Spelled through a subquery rather than a join so every aggregate below filters on it: a
    PRIVATE basket must not be able to appear in a ranking any more than in the catalog, and
    an aggregate that forgot the predicate would leak its name through a "most watched" row.
    """
    return select(CbBasket.id).where(
        CbBasket.archived_at.is_(None), CbBasket.visibility == "PUBLISHED"
    )


async def _counts_by_basket(
    session: AsyncSession, statement: Select[tuple[int, int]]
) -> dict[int, int]:
    return {basket_id: int(count) for basket_id, count in (await session.execute(statement)).all()}


async def _amounts_by_basket(
    session: AsyncSession, statement: Select[tuple[int, Decimal | None]]
) -> dict[int, Decimal]:
    """``SUM`` over an empty group is NULL, so the None arm is a real case, not defensiveness."""
    return {
        basket_id: amount
        for basket_id, amount in (await session.execute(statement)).all()
        if amount is not None
    }


async def _scalar_int(session: AsyncSession, statement: Select[tuple[int]]) -> int:
    value = (await session.execute(statement)).scalar_one_or_none()
    return int(value) if value is not None else 0


async def _candidates(session: AsyncSession) -> tuple[list[TrendingCandidate], TrendingPopulation]:
    """Every visible basket with every metric a list might rank it on, in six grouped reads.

    Six statements rather than one join: joining five one-to-many tables in a single SELECT
    multiplies rows before it aggregates them, which is how a basket with three versions and
    two watchers ends up reported as having six watchers. Grouped separately, each count is
    the count it claims to be.
    """
    visible = _visible_basket_ids().subquery()

    latest_metric_date = (
        select(CbMetrics.basket_id, func.max(CbMetrics.as_of_date).label("as_of_date"))
        .group_by(CbMetrics.basket_id)
        .subquery()
    )
    catalog = (
        await session.execute(
            select(CbBasket, CbMetrics)
            .join(visible, visible.c.id == CbBasket.id)
            .outerjoin(latest_metric_date, latest_metric_date.c.basket_id == CbBasket.id)
            .outerjoin(
                CbMetrics,
                (CbMetrics.basket_id == CbBasket.id)
                & (CbMetrics.as_of_date == latest_metric_date.c.as_of_date),
            )
            .order_by(CbBasket.slug)
        )
    ).all()

    reviewed = {
        basket_id: effective
        for basket_id, effective in (
            await session.execute(
                select(CbBasketVersion.basket_id, func.max(CbBasketVersion.effective_date))
                .where(CbBasketVersion.basket_id.in_(select(visible.c.id)))
                .group_by(CbBasketVersion.basket_id)
            )
        ).all()
    }

    watchers = await _counts_by_basket(
        session,
        select(CbWatchlistItem.basket_id, func.count(func.distinct(CbWatchlistItem.user_id)))
        .where(CbWatchlistItem.basket_id.in_(select(visible.c.id)))
        .group_by(CbWatchlistItem.basket_id),
    )
    investors = await _counts_by_basket(
        session,
        select(CbInvestment.basket_id, func.count(func.distinct(CbInvestment.user_id)))
        .where(
            CbInvestment.basket_id.in_(select(visible.c.id)),
            CbInvestment.status == _ACTIVE,
        )
        .group_by(CbInvestment.basket_id),
    )
    inflows = await _amounts_by_basket(
        session,
        select(CbInvestment.basket_id, func.sum(CbOrderBatch.requested_amount))
        .join(CbInvestment, CbInvestment.id == CbOrderBatch.investment_id)
        .where(
            CbInvestment.basket_id.in_(select(visible.c.id)),
            CbOrderBatch.kind.in_(_INFLOW_KINDS),
            CbOrderBatch.requested_amount.is_not(None),
        )
        .group_by(CbInvestment.basket_id),
    )

    candidates = [
        TrendingCandidate(
            slug=basket.slug,
            name=basket.name,
            ret_1m=metrics.ret_1m if metrics is not None else None,
            ret_1y=metrics.ret_1y if metrics is not None else None,
            cagr_5y=metrics.cagr_5y if metrics is not None else None,
            min_amount=metrics.min_amount if metrics is not None else None,
            launched_at=basket.launched_at,
            last_rebalanced_on=reviewed.get(basket.id),
            watchers=watchers.get(basket.id, 0),
            investors=investors.get(basket.id, 0),
            inflow_amount=inflows.get(basket.id),
        )
        for basket, metrics in catalog
    ]

    population = TrendingPopulation(
        watchers=await _scalar_int(
            session, select(func.count(func.distinct(CbWatchlistItem.user_id)))
        ),
        investors=await _scalar_int(
            session,
            select(func.count(func.distinct(CbInvestment.user_id))).where(
                CbInvestment.status == _ACTIVE
            ),
        ),
        contributors=await _scalar_int(
            session,
            select(func.count(func.distinct(CbOrderBatch.user_id))).where(
                CbOrderBatch.kind.in_(_INFLOW_KINDS)
            ),
        ),
    )
    return candidates, population


def _list_out(row: TrendingList) -> TrendingListOut:
    return TrendingListOut(
        key=row.key,
        title=row.title,
        ranks_by=row.ranks_by,
        metric_label=row.metric_label,
        metric_kind=row.metric_kind,
        population_based=row.population_based,
        population=row.population,
        eligible=row.eligible,
        entries=[
            TrendingEntryOut(
                rank=entry.rank,
                basket_slug=entry.slug,
                basket_name=entry.name,
                metric_value=entry.metric_value,
                metric_date=entry.metric_date,
                metric_display=entry.metric_display,
            )
            for entry in row.entries
        ],
        withheld_reason=row.withheld_reason,
        withheld_note=row.withheld_note,
        price_return_caveat=row.price_return_caveat,
    )


@router.get("/cb/trending", response_model=TrendingOut)
async def list_trending(session: SessionDep, principal: AuthenticatedDep) -> TrendingOut:
    """Every ranked list, published or withheld with its reason.

    All nine come back every time. A list the product cannot yet stand behind is *information*
    — it says the ranking exists and the data does not — and dropping it would be
    indistinguishable from never having built it.
    """
    # Auth + sole-tenant gate. The aggregates below are platform-wide on purpose (see module
    # docstring); this decides who may read them, and MIN_POPULATION decides what they see.
    await scoped_sole_user_id(session, principal.user_id)

    candidates, population = await _candidates(session)
    rows = rank_lists(candidates, population=population)
    convention = return_convention_fields()
    return TrendingOut(
        items=[_list_out(row) for row in rows],
        count=len(rows),
        catalog_size=len(candidates),
        min_entries=MIN_ENTRIES,
        min_population=MIN_POPULATION,
        return_convention=str(convention["return_convention"]),
        dividends_included=bool(convention["dividends_included"]),
        return_convention_note=str(convention["return_convention_note"]),
    )
