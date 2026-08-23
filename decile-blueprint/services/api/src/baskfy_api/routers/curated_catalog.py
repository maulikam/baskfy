"""Authenticated, single-operator curated-basket catalog and watchlist routes."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from baskfy_api.auth import AuthenticatedDep, Principal, settings_for
from baskfy_api.curated_catalog import (
    AccessFilter,
    BasketDetailOut,
    BasketType,
    CatalogFilters,
    CatalogOut,
    CatalogSort,
    CollectionOut,
    ManagerOut,
    RebalanceFrequency,
    VolatilityBucket,
    WatchlistItemOut,
    WatchlistOut,
    delete_watchlist,
    get_basket_detail,
    get_catalog,
    get_collection,
    get_manager,
    get_watchlist,
    put_watchlist,
)
from baskfy_api.db import SessionDep
from baskfy_api.problems import Problem, ProblemType
from baskfy_core.screener import canonical_json

router = APIRouter(tags=["curated-catalog"])

JSON_MEDIA_TYPE: Final = "application/json"
MAX_CATEGORIES: Final = 32
MAX_CATEGORY_LENGTH: Final = 64
MAX_SLUG_LENGTH: Final = 160
Slug = Annotated[
    str,
    Path(min_length=1, max_length=MAX_SLUG_LENGTH, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
]


async def require_sole_operator(request: Request, principal: AuthenticatedDep) -> Principal:
    """Hide every curated surface from accounts other than the configured operator."""
    if principal.public_id != settings_for(request).sole_user_id:
        # 404 avoids confirming that the private product surface exists to another account.
        raise Problem(ProblemType.NOT_FOUND, "Not found.")
    return principal


SoleOperatorDep = Annotated[Principal, Depends(require_sole_operator)]


def _json(model: BaseModel) -> Response:
    """Emit stored Decimal digits as JSON numbers, never through binary float."""
    return Response(
        content=canonical_json(model.model_dump(mode="python")),
        media_type=JSON_MEDIA_TYPE,
    )


def _categories(values: list[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    errors: list[dict[str, object]] = []
    if len(values) > MAX_CATEGORIES:
        errors.append(
            {
                "field": "category",
                "message": f"at most {MAX_CATEGORIES} category filters are accepted",
            }
        )
    for index, value in enumerate(values):
        if not value.strip():
            errors.append({"field": f"category.{index}", "message": "must not be blank"})
        elif len(value) > MAX_CATEGORY_LENGTH:
            errors.append(
                {
                    "field": f"category.{index}",
                    "message": f"must be at most {MAX_CATEGORY_LENGTH} characters",
                }
            )
    if errors:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            "The catalog filters are invalid.",
            errors=errors,
        )
    # Query URLs should have one canonical state: trim accidental padding and preserve only the
    # first occurrence of a repeated category.
    return tuple(dict.fromkeys(value.strip() for value in values))


class CatalogQueryParams(BaseModel):
    """The shareable catalog URL state, parsed as query fields rather than a request body."""

    model_config = ConfigDict(extra="forbid")

    category: list[str] = Field(default_factory=list)
    volatility: VolatilityBucket | None = None
    max_min_amount: Decimal | None = Field(default=None, ge=0)
    rebalance_frequency: RebalanceFrequency | None = None
    basket_type: BasketType | None = Field(default=None, alias="type")
    include_new: bool = True
    access: AccessFilter | None = None
    sort: CatalogSort = "popular"


@router.get("/catalog", response_model=CatalogOut, summary="Explore curated baskets")
async def catalog(
    session: SessionDep,
    principal: SoleOperatorDep,
    query: Annotated[CatalogQueryParams, Query()],
) -> Response:
    filters = CatalogFilters(
        category=_categories(query.category),
        volatility=query.volatility,
        max_min_amount=query.max_min_amount,
        rebalance_frequency=query.rebalance_frequency,
        basket_type=query.basket_type,
        include_new=query.include_new,
        access=query.access,
        sort=query.sort,
    )
    return _json(await get_catalog(session, principal.require_user(), filters))


@router.get("/catalog/{slug}", response_model=BasketDetailOut, summary="Curated basket detail")
async def basket_detail(slug: Slug, session: SessionDep, principal: SoleOperatorDep) -> Response:
    return _json(await get_basket_detail(session, principal.require_user(), slug))


@router.get("/managers/{slug}", response_model=ManagerOut, summary="Curated basket manager")
async def manager_detail(slug: Slug, session: SessionDep, principal: SoleOperatorDep) -> Response:
    return _json(await get_manager(session, principal.require_user(), slug))


@router.get(
    "/collections/{slug}", response_model=CollectionOut, summary="Curated basket collection"
)
async def collection_detail(
    slug: Slug, session: SessionDep, principal: SoleOperatorDep
) -> Response:
    return _json(await get_collection(session, principal.require_user(), slug))


@router.get("/watchlist", response_model=WatchlistOut, summary="Sole operator watchlist")
async def watchlist(session: SessionDep, principal: SoleOperatorDep) -> Response:
    return _json(await get_watchlist(session, principal.require_user()))


@router.put(
    "/watchlist/{basket_slug}",
    response_model=WatchlistItemOut,
    summary="Watch a curated basket",
)
async def watch(basket_slug: Slug, session: SessionDep, principal: SoleOperatorDep) -> Response:
    return _json(await put_watchlist(session, principal.require_user(), basket_slug))


@router.delete(
    "/watchlist/{basket_slug}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Stop watching a curated basket",
)
async def unwatch(basket_slug: Slug, session: SessionDep, principal: SoleOperatorDep) -> Response:
    await delete_watchlist(session, principal.require_user(), basket_slug)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
