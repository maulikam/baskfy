"""``/indices/*``, ``/market-health*`` and ``/listings`` — docs/07 §"Market data surfaces"
and §Instruments' last line (Prompt 11 deliverable 1).

    GET /indices/dashboard?date=            → ~145 rows: level, chg, chg_pct, pe, pb, div_yield
    GET /market-health?universe=nifty-500&date=
    GET /market-health/history?universe=&from=&to=   → the four series for charting
    GET /listings?from&to&series=&cursor=            → NSE listings, newest first

All four are public. They describe the market, not a user, and docs/08 §Routes marks the dashboard
and market-health pages as ISR/SSG surfaces — a page that has to authenticate cannot be either.

The three analytics responses carry ``as_of`` and ``data_version`` per docs/07 §Conventions.
``/listings`` does not: it is a register of what is listed, not a measurement of a trading day,
and there is no date at which its rows would have been different. It carries the
``{data, next_cursor}`` shape §Conventions specifies for pagination instead. `docs/11a` §5.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Final

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel

from baskfy_api.db import SessionDep
from baskfy_api.http_cache import snapshot_headers
from baskfy_api.market_data import (
    InvalidCursor,
    ListingQuery,
    UnknownUniverse,
    index_dashboard,
    listings,
    market_health,
    market_health_history,
    universe_or_raise,
)
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.schemas import (
    DEFAULT_PAGE_SIZE,
    GaugeOut,
    IndexDashboardOut,
    IndexMembershipOut,
    IndexRowOut,
    Limit,
    ListingOut,
    ListingsPage,
    MarketHealthHistoryOut,
    MarketHealthOut,
    MarketHealthPointOut,
)
from baskfy_api.screener import current_data_version, resolve_as_of
from baskfy_core.screener import canonical_json
from baskfy_core.universes import MARKET_HEALTH_SLUGS

router = APIRouter(tags=["market-data"])

JSON_MEDIA_TYPE: Final = "application/json"

#: docs/01 §6's universe selector lists twelve. The other two — `nifty-fno` and `etf` — are
#: classifications rather than size bands, `Universe.market_health` is false for both, and
#: `baskfy_worker.tasks.market_health` writes no row for them. Asking for one is a 422 rather than
#: an empty page, because the difference between "no data yet" and "never any data" matters.
HEALTH_UNIVERSES: Final[frozenset[str]] = frozenset(MARKET_HEALTH_SLUGS)

#: The widest history one call will answer. The chart's longest preset is five years.
MAX_HISTORY_SPAN: Final = dt.timedelta(days=5 * 366)


def _json(
    model: BaseModel, *, as_of: dt.date | None = None, data_version: int | None = None
) -> Response:
    """The canonical encoder, for the same reason `/screens/{id}/run` uses it.

    Pydantic renders `Decimal` through `float`, which turns a stored `57.7000` into `57.7`.
    CLAUDE.md house rule 8 makes the stored precision the contract; `canonical_json` keeps it.
    """
    return Response(
        content=canonical_json(model.model_dump(mode="python", by_alias=True)),
        media_type=JSON_MEDIA_TYPE,
        # Prompt 16 deliverable 3: the ETag `baskfy_api.http_cache` attaches is derived from
        # `data_version`, and it reads it from this header.
        headers=snapshot_headers(as_of, data_version),
    )


def _health_universe(slug: str) -> str:
    try:
        universe = universe_or_raise(slug)
    except UnknownUniverse as exc:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            f"{slug!r} is not a universe.",
            errors=[{"field": "universe", "message": f"choose one of {sorted(HEALTH_UNIVERSES)}"}],
        ) from exc
    if universe.slug not in HEALTH_UNIVERSES:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            f"{universe.name} has no breadth series; docs/01 §6 covers twelve universes.",
            errors=[{"field": "universe", "message": f"choose one of {sorted(HEALTH_UNIVERSES)}"}],
        )
    return universe.slug


@router.get("/indices/dashboard", response_model=IndexDashboardOut, summary="The indices dashboard")
async def get_index_dashboard(
    session: SessionDep,
    date: Annotated[dt.date | None, Query()] = None,
) -> Response:
    """docs/01 §7 — "~145 index rows ... Sorted by % change descending"."""
    resolution = await resolve_as_of(session, date)
    data_version = await current_data_version(session)
    board = await index_dashboard(session, resolution.as_of)
    return _json(
        IndexDashboardOut(
            as_of=board.as_of,
            data_version=data_version,
            data=[
                IndexRowOut(
                    slug=row.slug,
                    name=row.name,
                    is_universe=row.is_universe,
                    level=row.level,
                    change_abs=row.change_abs,
                    change_pct=row.change_pct,
                    pe=row.pe,
                    pb=row.pb,
                    div_yield=row.div_yield,
                    sparkline=list(row.sparkline),
                )
                for row in board.rows
            ],
        ),
        as_of=board.as_of,
        data_version=data_version,
    )


@router.get("/market-health", response_model=MarketHealthOut, summary="Breadth for one universe")
async def get_market_health(
    session: SessionDep,
    universe: Annotated[str, Query()] = "nifty-500",
    date: Annotated[dt.date | None, Query()] = None,
) -> Response:
    """docs/01 §6 — the four gauges, read from the daily snapshot rather than recomputed."""
    slug = _health_universe(universe)
    resolution = await resolve_as_of(session, date)
    data_version = await current_data_version(session)
    health = await market_health(session, slug, resolution.as_of)
    return _json(
        MarketHealthOut(
            as_of=health.as_of,
            data_version=data_version,
            universe=IndexMembershipOut(slug=health.universe.slug, name=health.universe.name),
            gauges=[
                GaugeOut(key=gauge.key, label=gauge.label, value=gauge.value)
                for gauge in health.gauges
            ],
            constituent_count=health.constituent_count,
            data_available_from=health.data_available_from,
        ),
        as_of=health.as_of,
        data_version=data_version,
    )


@router.get(
    "/market-health/history",
    response_model=MarketHealthHistoryOut,
    summary="The four breadth series",
)
async def get_market_health_history(
    session: SessionDep,
    universe: Annotated[str, Query()] = "nifty-500",
    from_: Annotated[dt.date | None, Query(alias="from")] = None,
    to: Annotated[dt.date | None, Query()] = None,
) -> Response:
    """docs/07: "the four series for charting"; docs/08 adds the index-level overlay."""
    slug = _health_universe(universe)
    resolution = await resolve_as_of(session, to)
    end = resolution.as_of
    start = from_ or (end - dt.timedelta(days=365))
    if start > end:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            f"`from` ({start.isoformat()}) is after `to` ({end.isoformat()}).",
            errors=[{"field": "from", "message": "must be on or before `to`"}],
        )
    if end - start > MAX_HISTORY_SPAN:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            f"Range exceeds the {MAX_HISTORY_SPAN.days}-day maximum for one request.",
            errors=[{"field": "from", "message": "narrow the range"}],
        )

    points = await market_health_history(session, slug, start, end)
    found = universe_or_raise(slug)
    return _json(
        MarketHealthHistoryOut(
            universe=IndexMembershipOut(slug=found.slug, name=found.name),
            to=end,
            points=[
                MarketHealthPointOut(
                    date=point.date,
                    pct_above_200dma=point.pct_above_200dma,
                    pct_above_50dma=point.pct_above_50dma,
                    pct_within_10pct_ath=point.pct_within_10pct_ath,
                    pct_ret_1y_positive=point.pct_ret_1y_positive,
                    constituent_count=point.constituent_count,
                    index_level=point.index_level,
                )
                for point in points
            ],
            **{"from": start},
        ),
        as_of=end,
        data_version=await current_data_version(session),
    )


@router.get("/listings", response_model=ListingsPage, summary="The NSE listings register")
async def get_listings(  # noqa: PLR0913, PLR0917 - FastAPI injects one parameter per query field
    session: SessionDep,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
    series: Annotated[str | None, Query(max_length=8)] = None,
    search: Annotated[str | None, Query(max_length=64)] = None,
    from_: Annotated[dt.date | None, Query(alias="from")] = None,
    to: Annotated[dt.date | None, Query()] = None,
) -> Response:
    """docs/07: "NSE listings, newest first"; Prompt 11 adds the search box."""
    try:
        page = await listings(
            session,
            ListingQuery(
                limit=limit,
                cursor=cursor,
                series=series,
                search=search,
                start=from_,
                end=to,
            ),
        )
    except InvalidCursor as exc:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            "That cursor did not come from this endpoint.",
            errors=[{"field": "cursor", "message": "omit it to start from the first page"}],
        ) from exc

    return _json(
        ListingsPage(
            data=[
                ListingOut(
                    symbol=row.symbol,
                    name=row.name,
                    series=row.series,
                    isin=row.isin,
                    listed_on=row.listed_on,
                )
                for row in page.rows
            ],
            next_cursor=page.next_cursor,
        ),
        data_version=await current_data_version(session),
    )
