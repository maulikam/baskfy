"""Set-based reads and sole-operator watchlists for the curated-basket catalog.

This module deliberately has no execution, broker, payment, or network dependency.  It projects
stored ``FEE`` baskets as ``FREE`` while subscriptions are dark (docs/smallcase/02), and accepts
the already-authenticated database user id from the router rather than any request-owned tenant
identifier.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal

from pydantic import BaseModel
from sqlalchemy import Select, case, delete, false, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import Subquery

from baskfy_api.problems import Problem, ProblemType, not_found
from baskfy_core.models import (
    BasketCollection,
    BasketConstituent,
    BasketMetrics,
    BasketVersion,
    BasketWatchlistItem,
    CuratedBasket,
    CuratedManager,
    Instrument,
    TradingDay,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID

BasketType = Literal["STOCK", "MF", "US"]
DisplayAccess = Literal["FREE"]
AccessFilter = Literal["FREE", "FEE"]
VolatilityBucket = Literal["LOW", "MED", "HIGH"]
RebalanceFrequency = Literal["WEEKLY", "MONTHLY", "QUARTERLY", "ANNUAL", "NEED_BASIS"]
CatalogSort = Literal["popular", "returns", "min_amount", "newest"]

CATALOG_RESPONSE_LIMIT: Final = 500
NEW_BASKET_MONTHS: Final = 12
LATEST_METRIC_RANK: Final = 1
PREVIOUS_METRIC_RANK: Final = 2
CONSECUTIVE_TRADING_SESSIONS: Final = 1
NAV_QUANTUM: Final = Decimal("0.0001")
RETURN_QUANTUM: Final = Decimal("0.0000000001")
BASE_NAV: Final = Decimal("100")


class ManagerSummaryOut(BaseModel):
    slug: str
    name: str
    kind: str
    sebi_reg_no: str | None


class MetricsOut(BaseModel):
    as_of_date: dt.date
    min_amount: Decimal
    volatility_bucket: str
    volatility_value: Decimal
    ret_1m: Decimal | None
    ret_6m: Decimal | None
    ret_1y: Decimal | None
    cagr_3y: Decimal | None
    cagr_5y: Decimal | None
    since_inception_pct: Decimal | None


class VersionSummaryOut(BaseModel):
    version_no: int
    effective_date: dt.date
    label: str
    added_count: int
    removed_count: int


class HeadlineReturnOut(BaseModel):
    """The age-selected card statistic; SC5 displays it without choosing a window again."""

    label: str
    value: Decimal | None


class CatalogBasketOut(BaseModel):
    slug: str
    name: str
    manager: ManagerSummaryOut
    type: str
    access: DisplayAccess
    categories: list[str]
    pitch: str
    rebalance_frequency: str
    launched_at: dt.date
    next_review_at: dt.date | None
    source: str
    latest_version: VersionSummaryOut | None
    metrics: MetricsOut | None
    headline_return: HeadlineReturnOut | None
    watched: bool


class CatalogQueryOut(BaseModel):
    category: list[str]
    volatility: VolatilityBucket | None
    max_min_amount: Decimal | None
    rebalance_frequency: RebalanceFrequency | None
    type: BasketType | None
    include_new: bool
    access: AccessFilter | None
    sort: CatalogSort


class CatalogOut(BaseModel):
    query: CatalogQueryOut
    total: int
    truncated: bool
    baskets: list[CatalogBasketOut]


class ConstituentOut(BaseModel):
    instrument_id: int
    symbol: str
    name: str
    segment: str
    weight: Decimal


class BasketDetailOut(CatalogBasketOut):
    description_md: str
    rationale_md: str
    benchmark_instrument_id: int | None
    constituents: list[ConstituentOut]


class ManagerOut(BaseModel):
    slug: str
    name: str
    kind: str
    sebi_reg_no: str | None
    bio: str
    strategies: list[str]
    disclosures_md: str
    baskets: list[CatalogBasketOut]


class CollectionOut(BaseModel):
    slug: str
    title: str
    subtitle: str
    curated_meta: dict[str, object]
    baskets: list[CatalogBasketOut]


class WatchlistItemOut(BaseModel):
    basket: CatalogBasketOut
    watched_at: dt.datetime
    nav_at_watch: Decimal
    nav_today: Decimal
    moved_pct: Decimal
    daily_change_pct: Decimal | None


class WatchlistOut(BaseModel):
    count: int
    truncated: bool
    items: list[WatchlistItemOut]


@dataclass(frozen=True, slots=True)
class CatalogFilters:
    """Validated, round-trippable catalog state from the router."""

    category: tuple[str, ...] = ()
    volatility: VolatilityBucket | None = None
    max_min_amount: Decimal | None = None
    rebalance_frequency: RebalanceFrequency | None = None
    basket_type: BasketType | None = None
    include_new: bool = True
    access: AccessFilter | None = None
    sort: CatalogSort = "popular"

    def output(self) -> CatalogQueryOut:
        return CatalogQueryOut(
            category=list(self.category),
            volatility=self.volatility,
            max_min_amount=self.max_min_amount,
            rebalance_frequency=self.rebalance_frequency,
            type=self.basket_type,
            include_new=self.include_new,
            access=self.access,
            sort=self.sort,
        )


@dataclass(frozen=True, slots=True)
class _CardRow:
    basket: CuratedBasket
    manager: CuratedManager
    metric: BasketMetrics | None
    version: BasketVersion | None
    watched: bool
    total: int


@dataclass(frozen=True, slots=True)
class _CardScope:
    basket_ids: Sequence[int] | None = None
    basket_slug: str | None = None
    manager_id: int | None = None


CardDatabaseRow = Row[tuple[CuratedBasket, CuratedManager, BasketMetrics, BasketVersion, int, int]]


def _latest_metric_alias() -> tuple[type[BasketMetrics], Subquery]:
    latest_dates = (
        select(
            BasketMetrics.basket_id.label("basket_id"),
            func.max(BasketMetrics.as_of_date).label("as_of_date"),
        )
        .group_by(BasketMetrics.basket_id)
        .subquery()
    )
    return aliased(BasketMetrics), latest_dates


def _latest_version_alias(latest_dates: Subquery) -> tuple[type[BasketVersion], Subquery]:
    latest_numbers = (
        select(
            BasketVersion.basket_id.label("basket_id"),
            func.max(BasketVersion.version_no).label("version_no"),
        )
        .outerjoin(latest_dates, latest_dates.c.basket_id == BasketVersion.basket_id)
        .where(
            BasketVersion.effective_date
            <= func.coalesce(latest_dates.c.as_of_date, func.current_date())
        )
        .group_by(BasketVersion.basket_id)
        .subquery()
    )
    return aliased(BasketVersion), latest_numbers


def _card_statement(
    user_id: int,
    *,
    filters: CatalogFilters | None = None,
    scope: _CardScope | None = None,
) -> Select[tuple[CuratedBasket, CuratedManager, BasketMetrics, BasketVersion, int, int]]:
    metric, latest_dates = _latest_metric_alias()
    version, latest_numbers = _latest_version_alias(latest_dates)
    watched = aliased(BasketWatchlistItem)
    stmt = (
        select(
            CuratedBasket,
            CuratedManager,
            metric,
            version,
            watched.id,
            func.count().over().label("catalog_total"),
        )
        .join(CuratedManager, CuratedManager.id == CuratedBasket.manager_id)
        .outerjoin(latest_dates, latest_dates.c.basket_id == CuratedBasket.id)
        .outerjoin(
            metric,
            (metric.basket_id == latest_dates.c.basket_id)
            & (metric.as_of_date == latest_dates.c.as_of_date),
        )
        .outerjoin(latest_numbers, latest_numbers.c.basket_id == CuratedBasket.id)
        .outerjoin(
            version,
            (version.basket_id == latest_numbers.c.basket_id)
            & (version.version_no == latest_numbers.c.version_no),
        )
        .outerjoin(
            watched,
            (watched.basket_id == CuratedBasket.id) & (watched.user_id == user_id),
        )
        .where(
            CuratedBasket.visibility == "PUBLISHED",
            CuratedBasket.archived_at.is_(None),
        )
    )
    selected = scope or _CardScope()
    if selected.basket_ids is not None:
        stmt = stmt.where(CuratedBasket.id.in_(selected.basket_ids))
    if selected.basket_slug is not None:
        stmt = stmt.where(CuratedBasket.slug == selected.basket_slug)
    if selected.manager_id is not None:
        stmt = stmt.where(CuratedBasket.manager_id == selected.manager_id)
    return _filter_and_order_cards(stmt, metric, filters or CatalogFilters())


def _filter_and_order_cards(
    stmt: Select[tuple[CuratedBasket, CuratedManager, BasketMetrics, BasketVersion, int, int]],
    metric: type[BasketMetrics],
    resolved: CatalogFilters,
) -> Select[tuple[CuratedBasket, CuratedManager, BasketMetrics, BasketVersion, int, int]]:
    if resolved.category:
        stmt = stmt.where(CuratedBasket.categories.overlap(list(resolved.category)))
    if resolved.volatility is not None:
        stmt = stmt.where(metric.volatility_bucket == resolved.volatility)
    if resolved.max_min_amount is not None:
        stmt = stmt.where(metric.min_amount <= resolved.max_min_amount)
    if resolved.rebalance_frequency is not None:
        stmt = stmt.where(CuratedBasket.rebalance_frequency == resolved.rebalance_frequency)
    if resolved.basket_type is not None:
        stmt = stmt.where(CuratedBasket.type == resolved.basket_type)
    if not resolved.include_new:
        catalog_as_of = select(func.max(BasketMetrics.as_of_date)).scalar_subquery()
        stmt = stmt.where(
            CuratedBasket.launched_at
            <= catalog_as_of - text(f"INTERVAL '{NEW_BASKET_MONTHS} months'")
        )
    # With subscriptions dark every card is logically FREE.  Applying the stored FEE value here
    # would expose a paywall category the product is required to hide.
    if resolved.access == "FEE":
        stmt = stmt.where(false())

    # The metric's own as-of date is the age boundary.  CURRENT_DATE would let a stale row cross
    # into a longer window it does not contain and make the same stored snapshot sort differently
    # tomorrow.
    headline_return = case(
        (
            CuratedBasket.launched_at <= metric.as_of_date - text("INTERVAL '60 months'"),
            metric.cagr_5y,
        ),
        (
            CuratedBasket.launched_at <= metric.as_of_date - text("INTERVAL '36 months'"),
            metric.cagr_3y,
        ),
        (
            CuratedBasket.launched_at <= metric.as_of_date - text("INTERVAL '12 months'"),
            metric.ret_1y,
        ),
        else_=metric.since_inception_pct,
    )
    if resolved.sort == "returns":
        stmt = stmt.order_by(headline_return.desc().nullslast(), CuratedBasket.id)
    elif resolved.sort == "min_amount":
        stmt = stmt.order_by(metric.min_amount.asc().nullslast(), CuratedBasket.id)
    else:
        # SC2 has no popularity ranking yet.  ``popular`` therefore shares the deterministic
        # launched-date/id fallback with ``newest``; SC9 replaces only the former.
        stmt = stmt.order_by(CuratedBasket.launched_at.desc(), CuratedBasket.id)
    return stmt.limit(CATALOG_RESPONSE_LIMIT)


def _metric_out(metric: BasketMetrics | None) -> MetricsOut | None:
    if metric is None:
        return None
    return MetricsOut(
        as_of_date=metric.as_of_date,
        min_amount=metric.min_amount,
        volatility_bucket=metric.volatility_bucket,
        volatility_value=metric.volatility_value,
        ret_1m=metric.ret_1m,
        ret_6m=metric.ret_6m,
        ret_1y=metric.ret_1y,
        cagr_3y=metric.cagr_3y,
        cagr_5y=metric.cagr_5y,
        since_inception_pct=metric.since_inception_pct,
    )


def _version_out(version: BasketVersion | None) -> VersionSummaryOut | None:
    if version is None:
        return None
    return VersionSummaryOut(
        version_no=version.version_no,
        effective_date=version.effective_date,
        label=version.label,
        added_count=version.added_count,
        removed_count=version.removed_count,
    )


def _headline_return(
    launched_at: dt.date, metric: BasketMetrics | None
) -> HeadlineReturnOut | None:
    if metric is None:
        return None
    if launched_at <= _subtract_months(metric.as_of_date, 60):
        label, value = "5Y CAGR", metric.cagr_5y
    elif launched_at <= _subtract_months(metric.as_of_date, 36):
        label, value = "3Y CAGR", metric.cagr_3y
    elif launched_at <= _subtract_months(metric.as_of_date, 12):
        label, value = "1Y returns", metric.ret_1y
    else:
        months = max(1, _complete_calendar_months(launched_at, metric.as_of_date))
        label, value = f"{months}M returns", metric.since_inception_pct
    return HeadlineReturnOut(label=label, value=value)


def _subtract_months(day: dt.date, months: int) -> dt.date:
    """Calendar-month subtraction matching core's headline-window boundaries."""
    zero_based_month = day.year * 12 + day.month - 1 - months
    year, month_index = divmod(zero_based_month, 12)
    month = month_index + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _complete_calendar_months(start: dt.date, end: dt.date) -> int:
    months = (end.year - start.year) * 12 + end.month - start.month
    if end.day < start.day:
        months -= 1
    return max(0, months)


def _card(row: _CardRow) -> CatalogBasketOut:
    return CatalogBasketOut(
        slug=row.basket.slug,
        name=row.basket.name,
        manager=ManagerSummaryOut(
            slug=row.manager.slug,
            name=row.manager.name,
            kind=row.manager.kind,
            sebi_reg_no=row.manager.sebi_reg_no,
        ),
        type=row.basket.type,
        access="FREE",
        categories=list(row.basket.categories),
        pitch=row.basket.description_md,
        rebalance_frequency=row.basket.rebalance_frequency,
        launched_at=row.basket.launched_at,
        next_review_at=row.basket.next_review_at,
        source=row.basket.source,
        latest_version=_version_out(row.version),
        metrics=_metric_out(row.metric),
        headline_return=_headline_return(row.basket.launched_at, row.metric),
        watched=row.watched,
    )


def _unpack_card_rows(rows: Sequence[CardDatabaseRow]) -> list[_CardRow]:
    unpacked: list[_CardRow] = []
    for value in rows:
        unpacked.append(
            _CardRow(
                basket=value[0],
                manager=value[1],
                metric=value[2],
                version=value[3],
                watched=value[4] is not None,
                total=int(value[5]),
            )
        )
    return unpacked


async def _load_card_rows(
    session: AsyncSession,
    user_id: int,
    *,
    filters: CatalogFilters | None = None,
    scope: _CardScope | None = None,
) -> list[_CardRow]:
    rows = (
        await session.execute(
            _card_statement(
                user_id,
                filters=filters,
                scope=scope,
            )
        )
    ).all()
    return _unpack_card_rows(rows)


async def get_catalog(session: AsyncSession, user_id: int, filters: CatalogFilters) -> CatalogOut:
    rows = await _load_card_rows(session, user_id, filters=filters)
    total = rows[0].total if rows else 0
    return CatalogOut(
        query=filters.output(),
        total=total,
        truncated=total > len(rows),
        baskets=[_card(row) for row in rows],
    )


async def get_basket_detail(session: AsyncSession, user_id: int, slug: str) -> BasketDetailOut:
    # One fixed statement selects the basket, current metric/version and all current constituents.
    metric, latest_dates = _latest_metric_alias()
    version, latest_numbers = _latest_version_alias(latest_dates)
    watched = aliased(BasketWatchlistItem)
    constituent = aliased(BasketConstituent)
    instrument = aliased(Instrument)
    rows = (
        await session.execute(
            select(
                CuratedBasket,
                CuratedManager,
                metric,
                version,
                watched.id,
                constituent,
                instrument,
            )
            .join(CuratedManager, CuratedManager.id == CuratedBasket.manager_id)
            .outerjoin(latest_dates, latest_dates.c.basket_id == CuratedBasket.id)
            .outerjoin(
                metric,
                (metric.basket_id == latest_dates.c.basket_id)
                & (metric.as_of_date == latest_dates.c.as_of_date),
            )
            .outerjoin(latest_numbers, latest_numbers.c.basket_id == CuratedBasket.id)
            .outerjoin(
                version,
                (version.basket_id == latest_numbers.c.basket_id)
                & (version.version_no == latest_numbers.c.version_no),
            )
            .outerjoin(
                watched,
                (watched.basket_id == CuratedBasket.id) & (watched.user_id == user_id),
            )
            .outerjoin(constituent, constituent.version_id == version.id)
            .outerjoin(instrument, instrument.id == constituent.instrument_id)
            .where(
                CuratedBasket.slug == slug,
                CuratedBasket.visibility == "PUBLISHED",
                CuratedBasket.archived_at.is_(None),
            )
            .order_by(constituent.segment, instrument.symbol)
        )
    ).all()
    if not rows:
        raise not_found("basket", slug)
    first = rows[0]
    basket: CuratedBasket = first[0]
    card = _card(
        _CardRow(
            basket=basket,
            manager=first[1],
            metric=first[2],
            version=first[3],
            watched=first[4] is not None,
            total=1,
        )
    )
    constituents = [
        ConstituentOut(
            instrument_id=row[5].instrument_id,
            symbol=row[6].symbol,
            name=row[6].name,
            segment=row[5].segment,
            weight=row[5].weight,
        )
        for row in rows
        if row[5] is not None and row[6] is not None
    ]
    return BasketDetailOut(
        **card.model_dump(mode="python"),
        description_md=basket.description_md,
        rationale_md=basket.rationale_md,
        benchmark_instrument_id=basket.benchmark_instrument_id,
        constituents=constituents,
    )


async def get_manager(session: AsyncSession, user_id: int, slug: str) -> ManagerOut:
    manager = (
        await session.execute(select(CuratedManager).where(CuratedManager.slug == slug))
    ).scalar_one_or_none()
    if manager is None:
        raise not_found("manager", slug)
    rows = await _load_card_rows(session, user_id, scope=_CardScope(manager_id=manager.id))
    return ManagerOut(
        slug=manager.slug,
        name=manager.name,
        kind=manager.kind,
        sebi_reg_no=manager.sebi_reg_no,
        bio=manager.bio,
        strategies=list(manager.strategies),
        disclosures_md=manager.disclosures_md,
        baskets=[_card(row) for row in rows],
    )


async def get_collection(session: AsyncSession, user_id: int, slug: str) -> CollectionOut:
    collection = (
        await session.execute(select(BasketCollection).where(BasketCollection.slug == slug))
    ).scalar_one_or_none()
    if collection is None:
        raise not_found("collection", slug)
    rows = await _load_card_rows(
        session, user_id, scope=_CardScope(basket_ids=collection.basket_ids)
    )
    position = {basket_id: index for index, basket_id in enumerate(collection.basket_ids)}
    rows.sort(key=lambda row: position.get(row.basket.id, len(position)))
    return CollectionOut(
        slug=collection.slug,
        title=collection.title,
        subtitle=collection.subtitle,
        curated_meta=dict(collection.curated_meta),
        baskets=[_card(row) for row in rows],
    )


def _nav(metric: BasketMetrics | None) -> Decimal:
    if metric is None or metric.since_inception_pct is None:
        raise Problem(
            ProblemType.PIPELINE_DEGRADED,
            "The basket's chain-linked NAV has not been computed yet.",
        )
    nav = (BASE_NAV + metric.since_inception_pct).quantize(NAV_QUANTUM, ROUND_HALF_UP)
    if nav <= 0:
        raise Problem(
            ProblemType.PIPELINE_DEGRADED,
            "The basket's stored chain-linked NAV is not positive.",
        )
    return nav


def _watchlist_item(
    card_row: _CardRow, watch: BasketWatchlistItem, previous_nav: Decimal | None
) -> WatchlistItemOut:
    today = _nav(card_row.metric)
    if watch.nav_at_watch <= 0:
        raise Problem(ProblemType.PIPELINE_DEGRADED, "The stored watch baseline is not positive.")
    if previous_nav is not None and previous_nav <= 0:
        raise Problem(
            ProblemType.PIPELINE_DEGRADED, "The previous chain-linked NAV is not positive."
        )
    moved = ((today / watch.nav_at_watch) - Decimal(1)).quantize(RETURN_QUANTUM, ROUND_HALF_UP)
    daily_change = (
        None
        if previous_nav is None
        else ((today / previous_nav) - Decimal(1)).quantize(RETURN_QUANTUM, ROUND_HALF_UP)
    )
    return WatchlistItemOut(
        basket=_card(card_row),
        watched_at=watch.watched_at,
        nav_at_watch=watch.nav_at_watch,
        nav_today=today,
        moved_pct=moved,
        daily_change_pct=daily_change,
    )


async def _previous_nav_by_basket(
    session: AsyncSession, basket_ids: Sequence[int]
) -> dict[int, Decimal | None]:
    if not basket_ids:
        return {}
    ranked = (
        select(
            BasketMetrics.basket_id.label("basket_id"),
            BasketMetrics.since_inception_pct.label("since_inception_pct"),
            BasketMetrics.as_of_date.label("as_of_date"),
            func.row_number()
            .over(
                partition_by=BasketMetrics.basket_id,
                order_by=BasketMetrics.as_of_date.desc(),
            )
            .label("metric_rank"),
        )
        .where(BasketMetrics.basket_id.in_(basket_ids))
        .subquery()
    )
    latest = (
        select(
            ranked.c.basket_id,
            ranked.c.since_inception_pct,
            ranked.c.as_of_date,
        )
        .where(ranked.c.metric_rank == LATEST_METRIC_RANK)
        .subquery()
    )
    previous = (
        select(
            ranked.c.basket_id,
            ranked.c.since_inception_pct,
            ranked.c.as_of_date,
        )
        .where(ranked.c.metric_rank == PREVIOUS_METRIC_RANK)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                previous.c.basket_id,
                previous.c.since_inception_pct,
                func.count(func.distinct(TradingDay.date)).label("trading_sessions"),
            )
            .join(latest, latest.c.basket_id == previous.c.basket_id)
            .outerjoin(
                TradingDay,
                (TradingDay.exchange_id == NSE_EXCHANGE_ID)
                & TradingDay.is_trading_day.is_(True)
                & (TradingDay.date > previous.c.as_of_date)
                & (TradingDay.date <= latest.c.as_of_date),
            )
            .group_by(previous.c.basket_id, previous.c.since_inception_pct)
        )
    ).all()
    result: dict[int, Decimal | None] = {}
    for basket_id, since_inception_pct, trading_sessions in rows:
        result[int(basket_id)] = (
            None
            if since_inception_pct is None or int(trading_sessions) != CONSECUTIVE_TRADING_SESSIONS
            else (BASE_NAV + since_inception_pct).quantize(NAV_QUANTUM, ROUND_HALF_UP)
        )
    return result


async def get_watchlist(session: AsyncSession, user_id: int) -> WatchlistOut:
    metric, latest_dates = _latest_metric_alias()
    version, latest_numbers = _latest_version_alias(latest_dates)
    rows = (
        await session.execute(
            select(
                BasketWatchlistItem,
                CuratedBasket,
                CuratedManager,
                metric,
                version,
                func.count().over().label("watchlist_total"),
            )
            .join(CuratedBasket, CuratedBasket.id == BasketWatchlistItem.basket_id)
            .join(CuratedManager, CuratedManager.id == CuratedBasket.manager_id)
            .outerjoin(latest_dates, latest_dates.c.basket_id == CuratedBasket.id)
            .outerjoin(
                metric,
                (metric.basket_id == latest_dates.c.basket_id)
                & (metric.as_of_date == latest_dates.c.as_of_date),
            )
            .outerjoin(latest_numbers, latest_numbers.c.basket_id == CuratedBasket.id)
            .outerjoin(
                version,
                (version.basket_id == latest_numbers.c.basket_id)
                & (version.version_no == latest_numbers.c.version_no),
            )
            .where(
                BasketWatchlistItem.user_id == user_id,
                CuratedBasket.visibility == "PUBLISHED",
                CuratedBasket.archived_at.is_(None),
            )
            .order_by(BasketWatchlistItem.watched_at.desc(), BasketWatchlistItem.id.desc())
            .limit(CATALOG_RESPONSE_LIMIT)
        )
    ).all()
    previous_nav = await _previous_nav_by_basket(
        session, [basket.id for _, basket, _, _, _, _ in rows]
    )
    items: list[WatchlistItemOut] = []
    for watch, basket, manager, metric_row, version, _ in rows:
        card_row = _CardRow(
            basket=basket,
            manager=manager,
            metric=metric_row,
            version=version,
            watched=True,
            total=len(rows),
        )
        items.append(_watchlist_item(card_row, watch, previous_nav.get(basket.id)))
    total = int(rows[0][5]) if rows else 0
    return WatchlistOut(count=total, truncated=total > len(items), items=items)


async def put_watchlist(session: AsyncSession, user_id: int, basket_slug: str) -> WatchlistItemOut:
    rows = await _load_card_rows(session, user_id, scope=_CardScope(basket_slug=basket_slug))
    if not rows:
        raise not_found("basket", basket_slug)
    row = rows[0]
    baseline = _nav(row.metric)
    await session.execute(
        insert(BasketWatchlistItem)
        .values(
            user_id=user_id,
            basket_id=row.basket.id,
            nav_at_watch=baseline,
            watched_at=func.clock_timestamp(),
        )
        .on_conflict_do_nothing(
            index_elements=[BasketWatchlistItem.user_id, BasketWatchlistItem.basket_id]
        )
    )
    watch = (
        await session.execute(
            select(BasketWatchlistItem).where(
                BasketWatchlistItem.user_id == user_id,
                BasketWatchlistItem.basket_id == row.basket.id,
            )
        )
    ).scalar_one()
    row = _CardRow(
        basket=row.basket,
        manager=row.manager,
        metric=row.metric,
        version=row.version,
        watched=True,
        total=1,
    )
    previous_nav = await _previous_nav_by_basket(session, [row.basket.id])
    return _watchlist_item(row, watch, previous_nav.get(row.basket.id))


async def delete_watchlist(session: AsyncSession, user_id: int, basket_slug: str) -> None:
    basket_id = (
        await session.execute(
            select(CuratedBasket.id).where(
                CuratedBasket.slug == basket_slug,
                CuratedBasket.visibility == "PUBLISHED",
                CuratedBasket.archived_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if basket_id is None:
        raise not_found("basket", basket_slug)
    await session.execute(
        delete(BasketWatchlistItem).where(
            BasketWatchlistItem.user_id == user_id,
            BasketWatchlistItem.basket_id == basket_id,
        )
    )
