"""Create PRIVATE stock baskets for the sole tenant (SC8 / leaf 2.2).

``POST /cb/baskets`` persists a MANUAL + PRIVATE basket with a GENESIS version and ≥2
weighted constituents. Weights are asserted via ``assert_weights_sum_to_one``. No broker
path, no invest/apply hand-off on this route.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.db import SessionDep
from baskfy_api.problems import Problem, ProblemType
from baskfy_core.curated_baskets import MANAGER_SLUG_MAULIK, assert_weights_sum_to_one
from baskfy_core.models import (
    CbBasket,
    CbBasketVersion,
    CbConstituent,
    CbManager,
    Instrument,
)

router = APIRouter(tags=["curated-create"])

_MIN_CONSTITUENTS = 2
_SLUG_MAX = 64


class ConstituentIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    weight: Decimal


class CreateBasketIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    constituents: list[ConstituentIn] = Field(min_length=_MIN_CONSTITUENTS)
    description_md: str | None = None


class ConstituentOut(BaseModel):
    symbol: str
    instrument_id: int
    weight: Decimal
    segment: str


class CreateBasketOut(BaseModel):
    id: int
    slug: str
    name: str
    visibility: str
    type: str
    source: str
    version_no: int
    label: str
    constituents: list[ConstituentOut]


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return (slug or "basket")[:_SLUG_MAX]


async def _unique_slug(session: SessionDep, base: str) -> str:
    candidate = base[:_SLUG_MAX]
    exists = await session.scalar(select(CbBasket.id).where(CbBasket.slug == candidate))
    if exists is None:
        return candidate
    suffix = 2
    while suffix < 10_000:  # noqa: PLR2004 - bounded collision search
        trimmed = base[: max(1, _SLUG_MAX - len(str(suffix)) - 1)]
        candidate = f"{trimmed}-{suffix}"
        exists = await session.scalar(select(CbBasket.id).where(CbBasket.slug == candidate))
        if exists is None:
            return candidate
        suffix += 1
    raise Problem(ProblemType.INTERNAL_ERROR, "could not allocate a unique basket slug")


@router.post("/cb/baskets", response_model=CreateBasketOut, status_code=201)
async def create_private_basket(
    body: CreateBasketIn,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> CreateBasketOut:
    """Create a PRIVATE STOCK basket with a GENESIS version for the sole user."""
    await scoped_sole_user_id(session, principal.user_id)

    symbols = [c.symbol.strip().upper() for c in body.constituents]
    if len(symbols) < _MIN_CONSTITUENTS:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            f"at least {_MIN_CONSTITUENTS} constituents are required",
        )
    if len(set(symbols)) != len(symbols):
        raise Problem(ProblemType.INVALID_SCREEN_DEFINITION, "duplicate symbols are not allowed")

    weights = [c.weight for c in body.constituents]
    try:
        assert_weights_sum_to_one(weights)
    except ValueError as exc:
        raise Problem(ProblemType.INVALID_SCREEN_DEFINITION, str(exc)) from exc

    instruments = list(
        (
            await session.scalars(
                select(Instrument).where(
                    func.upper(Instrument.symbol).in_(symbols),
                    Instrument.is_active.is_(True),
                )
            )
        ).all()
    )
    by_symbol = {str(row.symbol).upper(): row for row in instruments}
    missing = [sym for sym in symbols if sym not in by_symbol]
    if missing:
        raise Problem(
            ProblemType.NOT_FOUND,
            f"unknown symbols: {', '.join(missing)}",
        )

    manager_id = await session.scalar(
        select(CbManager.id).where(CbManager.slug == MANAGER_SLUG_MAULIK)
    )
    if manager_id is None:
        raise Problem(ProblemType.INTERNAL_ERROR, "curated manager seed is missing")

    today = dt.datetime.now(tz=dt.UTC).date()
    slug = await _unique_slug(session, _slugify(body.name))

    basket = CbBasket(
        slug=slug,
        name=body.name.strip(),
        manager_id=int(manager_id),
        type="STOCK",
        access="FREE",
        visibility="PRIVATE",
        categories=["custom"],
        description_md=body.description_md,
        rationale_md=None,
        rebalance_frequency="NEED_BASIS",
        benchmark_instrument_id=None,
        launched_at=today,
        next_review_at=None,
        source="MANUAL",
        scan_strategy_key=None,
        archived_at=None,
    )
    session.add(basket)
    await session.flush()

    version = CbBasketVersion(
        basket_id=basket.id,
        version_no=1,
        effective_date=today,
        label="GENESIS",
        added_count=len(symbols),
        removed_count=0,
        notes_md="User-created PRIVATE genesis version.",
        source_scan_run_id=None,
    )
    session.add(version)
    await session.flush()

    out_constituents: list[ConstituentOut] = []
    for symbol, weight in zip(symbols, weights, strict=True):
        instrument = by_symbol[symbol]
        session.add(
            CbConstituent(
                version_id=version.id,
                instrument_id=instrument.id,
                segment="Equity",
                weight=weight,
            )
        )
        out_constituents.append(
            ConstituentOut(
                symbol=symbol,
                instrument_id=int(instrument.id),
                weight=weight,
                segment="Equity",
            )
        )

    await session.commit()
    await session.refresh(basket)
    await session.refresh(version)

    return CreateBasketOut(
        id=int(basket.id),
        slug=basket.slug,
        name=basket.name,
        visibility=basket.visibility,
        type=basket.type,
        source=basket.source,
        version_no=int(version.version_no),
        label=version.label,
        constituents=out_constituents,
    )
