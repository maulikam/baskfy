"""``/explore`` catalog + ``/watchlist`` - curated-basket discovery API (SC2).

Read-heavy surfaces for the catalog cards in docs/smallcase/05-ui-spec.md. Watchlist CRUD is
user-scoped via the authenticated principal (sole user in Track A). **No order or execute
routes** - Track C / PACK.2.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import ColumnExpressionArgument, asc, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.db import SessionDep
from baskfy_api.problems import not_found
from baskfy_core.curated_metrics import headline_return, whole_months_between
from baskfy_core.models import (
    CbBasket,
    CbCollection,
    CbManager,
    CbMetrics,
    CbWatchlistItem,
)

router = APIRouter(tags=["explore"])

SortField = Literal[
    "min_amount",
    "ret_1y",
    "cagr_3y",
    "cagr_5y",
    "name",
    "launched_at",
    "volatility",
]
SortDir = Literal["asc", "desc"]

#: Query params documented for SC5 URL state (05-ui-spec chips + filter dialog).
DOCUMENTED_LIST_PARAMS: Final[tuple[str, ...]] = (
    "max_min_amount",
    "access",
    "volatility",
    "category",
    "rebalance_frequency",
    "basket_type",
    "include_new",
    "sort",
    "order",
    "q",
)


class MetricsOut(BaseModel):
    as_of_date: dt.date | None = None
    min_amount: Decimal | None = None
    volatility_bucket: str | None = None
    volatility_value: Decimal | None = None
    ret_1m: Decimal | None = None
    ret_6m: Decimal | None = None
    ret_1y: Decimal | None = None
    cagr_3y: Decimal | None = None
    cagr_5y: Decimal | None = None
    since_inception_pct: Decimal | None = None
    headline_label: str | None = None
    headline_pct: Decimal | None = None


class ManagerBriefOut(BaseModel):
    slug: str
    name: str
    kind: str


class BasketCardOut(BaseModel):
    slug: str
    name: str
    access: str
    visibility: str
    type: str
    categories: list[str]
    rebalance_frequency: str
    source: str
    description_md: str | None = None
    launched_at: dt.date | None = None
    manager: ManagerBriefOut
    metrics: MetricsOut | None = None


class BasketListOut(BaseModel):
    items: list[BasketCardOut]
    total: int


class ManagerOut(BaseModel):
    slug: str
    name: str
    kind: str
    sebi_reg_no: str | None = None
    bio: str | None = None
    strategies: list[str]
    disclosures_md: str | None = None


class ManagerListOut(BaseModel):
    items: list[ManagerOut]


class CollectionOut(BaseModel):
    slug: str
    title: str
    subtitle: str | None = None
    basket_slugs: list[str]
    position: int


class CollectionListOut(BaseModel):
    items: list[CollectionOut]


class WatchlistItemOut(BaseModel):
    basket_slug: str
    basket_name: str
    watched_at: dt.datetime
    nav_at_watch: Decimal | None = None
    moved_pct: Decimal | None = None


class WatchlistOut(BaseModel):
    items: list[WatchlistItemOut]
    count: int


class WatchlistAddIn(BaseModel):
    basket_slug: str = Field(min_length=1, max_length=120)
    nav_at_watch: Decimal | None = None


def _metrics_out(row: CbMetrics | None, launched_at: dt.date | None) -> MetricsOut | None:
    if row is None:
        return None
    # The label and the number must describe the same span. Passing the months the basket has
    # actually existed lets `headline_return` choose both from one row of its table, which is
    # what stops a card reading "6M returns" over the 21-day figure.
    months_available = (
        whole_months_between(launched_at, row.as_of_date) if launched_at is not None else 0
    )
    headline = headline_return(
        months_available=months_available,
        ret_1m=row.ret_1m,
        ret_6m=row.ret_6m,
        ret_1y=row.ret_1y,
        cagr_3y=row.cagr_3y,
        cagr_5y=row.cagr_5y,
        since_inception_pct=row.since_inception_pct,
    )
    return MetricsOut(
        as_of_date=row.as_of_date,
        min_amount=row.min_amount,
        volatility_bucket=row.volatility_bucket,
        volatility_value=row.volatility_value,
        ret_1m=row.ret_1m,
        ret_6m=row.ret_6m,
        ret_1y=row.ret_1y,
        cagr_3y=row.cagr_3y,
        cagr_5y=row.cagr_5y,
        since_inception_pct=row.since_inception_pct,
        headline_label=headline.label,
        headline_pct=headline.value_pct,
    )


def _card(basket: CbBasket, manager: CbManager, metrics: CbMetrics | None) -> BasketCardOut:
    return BasketCardOut(
        slug=basket.slug,
        name=basket.name,
        access=basket.access,
        visibility=basket.visibility,
        type=basket.type,
        categories=list(basket.categories or []),
        rebalance_frequency=basket.rebalance_frequency,
        source=basket.source,
        description_md=basket.description_md,
        launched_at=basket.launched_at,
        manager=ManagerBriefOut(slug=manager.slug, name=manager.name, kind=manager.kind),
        metrics=_metrics_out(metrics, basket.launched_at),
    )


def _visible() -> tuple[ColumnExpressionArgument[bool], ...]:
    """The predicate that decides a basket may be shown at all.

    One helper, used by every route that reaches ``cb_basket``, because this used to be spelled
    out at the list route and *nowhere else*: the detail route, the watchlist writes and the
    collection expansion each selected by slug or id alone. ``cb_basket`` has no owner column,
    so ``visibility`` is the only thing standing between a PRIVATE basket and the caller, and
    three of the four places that needed it did not have it.
    """
    return (CbBasket.archived_at.is_(None), CbBasket.visibility == "PUBLISHED")


_SORT_COLUMNS: dict[SortField, ColumnExpressionArgument[object]] = {
    "min_amount": CbMetrics.min_amount,
    "ret_1y": CbMetrics.ret_1y,
    "cagr_3y": CbMetrics.cagr_3y,
    "cagr_5y": CbMetrics.cagr_5y,
    "name": CbBasket.name,
    "launched_at": CbBasket.launched_at,
    "volatility": CbMetrics.volatility_value,
}


def _order_by(sort: SortField, order: SortDir) -> ColumnExpressionArgument[object]:
    """The catalog ordering, with NULLs last in **both** directions.

    Five of the seven sort fields are ``cb_metrics`` columns reached through an outer join, so
    every one of them can be NULL. Postgres puts NULLs first on ``DESC``, which meant
    ``?sort=cagr_5y&order=desc`` -- the "best five-year performers" view -- opened with every
    basket that has no metrics row at all. A basket we could not measure is not the best
    performer. ``cb_basket.id`` breaks ties so the order is stable between requests, which
    also has to hold before any pagination is added.
    """
    column = _SORT_COLUMNS[sort]
    ordered = desc(column) if order == "desc" else asc(column)
    return ordered.nullslast()


@router.get("/explore", response_model=BasketListOut)
async def list_explore_baskets(  # noqa: PLR0913, PLR0917 - one query param per documented facet
    session: SessionDep,
    principal: AuthenticatedDep,
    max_min_amount: Annotated[Decimal | None, Query(description="Under INR N chip")] = None,
    access: Annotated[str | None, Query(pattern="^(FREE|FEE)$")] = None,
    volatility: Annotated[str | None, Query(pattern="^(LOW|MED|HIGH)$")] = None,
    category: Annotated[str | None, Query(max_length=60)] = None,
    rebalance_frequency: Annotated[
        str | None,
        Query(pattern="^(WEEKLY|MONTHLY|QUARTERLY|ANNUAL|NEED_BASIS)$"),
    ] = None,
    basket_type: Annotated[
        str | None, Query(pattern="^(STOCK|MF|US)$", description="STOCK|MF|US")
    ] = None,
    include_new: Annotated[bool, Query()] = True,
    sort: Annotated[SortField, Query()] = "name",
    order: Annotated[SortDir, Query()] = "asc",
    q: Annotated[str | None, Query(max_length=80)] = None,
) -> BasketListOut:
    """Catalog list - filters/sorts match docs/smallcase/05 (DECISIONS-SC SC2).

    Perf (SC11 / leaf-1.8.3): single query joining basket + manager + latest metrics —
    N+1 avoided (no per-card follow-up SELECTs). Budget: catalog p95 < 1s on dev hardware
    (docs/smallcase/06 SC11).
    """
    principal.require_user()
    latest = (
        select(CbMetrics.basket_id, func.max(CbMetrics.as_of_date).label("as_of_date"))
        .group_by(CbMetrics.basket_id)
        .subquery()
    )
    # One JOINed SELECT for the card grid — N+1 avoided; p95 budget < 1s (leaf-1.8.3).
    stmt = (
        select(CbBasket, CbManager, CbMetrics)
        .join(CbManager, CbManager.id == CbBasket.manager_id)
        .outerjoin(latest, latest.c.basket_id == CbBasket.id)
        .outerjoin(
            CbMetrics,
            (CbMetrics.basket_id == CbBasket.id) & (CbMetrics.as_of_date == latest.c.as_of_date),
        )
        .where(*_visible())
    )
    if max_min_amount is not None:
        stmt = stmt.where(CbMetrics.min_amount.is_not(None), CbMetrics.min_amount <= max_min_amount)
    if access is not None:
        stmt = stmt.where(CbBasket.access == access)
    if volatility is not None:
        stmt = stmt.where(CbMetrics.volatility_bucket == volatility)
    if category is not None:
        stmt = stmt.where(CbBasket.categories.contains([category]))
    if rebalance_frequency is not None:
        stmt = stmt.where(CbBasket.rebalance_frequency == rebalance_frequency)
    if basket_type is not None:
        stmt = stmt.where(CbBasket.type == basket_type)
    if not include_new:
        cutoff = dt.date.today() - dt.timedelta(days=30)
        stmt = stmt.where(or_(CbBasket.launched_at.is_(None), CbBasket.launched_at < cutoff))
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(or_(CbBasket.name.ilike(pattern), CbBasket.slug.ilike(pattern)))

    stmt = stmt.order_by(_order_by(sort, order), CbBasket.id.asc())

    rows = (await session.execute(stmt)).all()
    items = [_card(b, m, met) for b, m, met in rows]
    return BasketListOut(items=items, total=len(items))


@router.get("/explore/managers", response_model=ManagerListOut)
async def list_managers(session: SessionDep, principal: AuthenticatedDep) -> ManagerListOut:
    principal.require_user()
    rows = (await session.execute(select(CbManager).order_by(CbManager.slug))).scalars().all()
    return ManagerListOut(
        items=[
            ManagerOut(
                slug=r.slug,
                name=r.name,
                kind=r.kind,
                sebi_reg_no=r.sebi_reg_no,
                bio=r.bio,
                strategies=list(r.strategies or []),
                disclosures_md=r.disclosures_md,
            )
            for r in rows
        ]
    )


@router.get("/explore/managers/{slug}", response_model=ManagerOut)
async def get_manager(slug: str, session: SessionDep, principal: AuthenticatedDep) -> ManagerOut:
    principal.require_user()
    row = (
        await session.execute(select(CbManager).where(CbManager.slug == slug))
    ).scalar_one_or_none()
    if row is None:
        raise not_found("manager", slug)
    return ManagerOut(
        slug=row.slug,
        name=row.name,
        kind=row.kind,
        sebi_reg_no=row.sebi_reg_no,
        bio=row.bio,
        strategies=list(row.strategies or []),
        disclosures_md=row.disclosures_md,
    )


@router.get("/explore/collections", response_model=CollectionListOut)
async def list_collections(session: SessionDep, principal: AuthenticatedDep) -> CollectionListOut:
    principal.require_user()
    rows = (
        (
            await session.execute(
                select(CbCollection).order_by(CbCollection.position, CbCollection.slug)
            )
        )
        .scalars()
        .all()
    )
    items: list[CollectionOut] = []
    for row in rows:
        items.append(await _collection_out(session, row))
    return CollectionListOut(items=items)


@router.get("/explore/collections/{slug}", response_model=CollectionOut)
async def get_collection(
    slug: str, session: SessionDep, principal: AuthenticatedDep
) -> CollectionOut:
    principal.require_user()
    row = (
        await session.execute(select(CbCollection).where(CbCollection.slug == slug))
    ).scalar_one_or_none()
    if row is None:
        raise not_found("collection", slug)
    return await _collection_out(session, row)


async def _collection_out(session: AsyncSession, row: CbCollection) -> CollectionOut:
    by_id: dict[int, str] = {}
    if row.basket_ids:
        for basket in (
            await session.execute(
                select(CbBasket).where(CbBasket.id.in_(list(row.basket_ids)), *_visible())
            )
        ).scalars():
            by_id[basket.id] = basket.slug
    return CollectionOut(
        slug=row.slug,
        title=row.title,
        subtitle=row.subtitle,
        basket_slugs=[by_id[i] for i in (row.basket_ids or []) if i in by_id],
        position=row.position,
    )


@router.get("/explore/{slug}", response_model=BasketCardOut)
async def get_explore_basket(
    slug: str, session: SessionDep, principal: AuthenticatedDep
) -> BasketCardOut:
    principal.require_user()
    row = (
        await session.execute(
            select(CbBasket, CbManager)
            .join(CbManager, CbManager.id == CbBasket.manager_id)
            .where(CbBasket.slug == slug, *_visible())
        )
    ).one_or_none()
    if row is None:
        raise not_found("basket", slug)
    basket, manager = row
    metrics = (
        await session.execute(
            select(CbMetrics)
            .where(CbMetrics.basket_id == basket.id)
            .order_by(CbMetrics.as_of_date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return _card(basket, manager, metrics)


@router.get("/watchlist", response_model=WatchlistOut)
async def list_watchlist(session: SessionDep, principal: AuthenticatedDep) -> WatchlistOut:
    # Sole-tenant: always filter user_id == sole_user (leaf-1.8.2); never raw principal.
    user_id = await scoped_sole_user_id(session, principal.user_id)
    items_rows = (
        await session.execute(
            select(CbWatchlistItem, CbBasket)
            .join(CbBasket, CbBasket.id == CbWatchlistItem.basket_id)
            .where(CbWatchlistItem.user_id == user_id, *_visible())
            .order_by(CbWatchlistItem.watched_at.desc())
        )
    ).all()
    items = [
        WatchlistItemOut(
            basket_slug=basket.slug,
            basket_name=basket.name,
            watched_at=item.watched_at,
            nav_at_watch=item.nav_at_watch,
            moved_pct=None,
        )
        for item, basket in items_rows
    ]
    return WatchlistOut(items=items, count=len(items))


@router.post("/watchlist", response_model=WatchlistItemOut, status_code=201)
async def add_watchlist(
    body: WatchlistAddIn,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> WatchlistItemOut:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    basket = (
        await session.execute(
            select(CbBasket).where(CbBasket.slug == body.basket_slug, *_visible())
        )
    ).scalar_one_or_none()
    if basket is None:
        raise not_found("basket", body.basket_slug)
    existing = (
        await session.execute(
            select(CbWatchlistItem).where(
                CbWatchlistItem.user_id == user_id,
                CbWatchlistItem.basket_id == basket.id,
            )
        )
    ).scalar_one_or_none()
    now = dt.datetime.now(tz=dt.UTC)
    if existing is not None:
        existing.watched_at = now
        existing.nav_at_watch = body.nav_at_watch
        item = existing
    else:
        item = CbWatchlistItem(
            user_id=user_id,
            basket_id=basket.id,
            watched_at=now,
            nav_at_watch=body.nav_at_watch,
        )
        session.add(item)
    await session.commit()
    await session.refresh(item)
    return WatchlistItemOut(
        basket_slug=basket.slug,
        basket_name=basket.name,
        watched_at=item.watched_at,
        nav_at_watch=item.nav_at_watch,
        moved_pct=None,
    )


@router.delete("/watchlist/{slug}", status_code=204)
async def remove_watchlist(
    slug: str,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> None:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    basket = (
        await session.execute(select(CbBasket).where(CbBasket.slug == slug, *_visible()))
    ).scalar_one_or_none()
    if basket is None:
        raise not_found("basket", slug)
    item = (
        await session.execute(
            select(CbWatchlistItem).where(
                CbWatchlistItem.user_id == user_id,
                CbWatchlistItem.basket_id == basket.id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise not_found("watchlist item", slug)
    await session.delete(item)
    await session.commit()
