"""Basket version list + diff between any two versions (AF I.1).

Read-only. Mounted onto the explore router so ``app.py`` does not need a second include.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from baskfy_api.auth import AuthenticatedDep
from baskfy_api.db import SessionDep
from baskfy_api.problems import bad_request, not_found
from baskfy_core.models import CbBasket, CbBasketVersion, CbConstituent, Instrument

router = APIRouter(tags=["explore"])

SLUG_MAX = 120
SlugPath = Annotated[str, Path(min_length=1, max_length=SLUG_MAX)]


def _visible() -> tuple[object, ...]:
    return (CbBasket.archived_at.is_(None), CbBasket.visibility == "LISTED")


class VersionSummaryOut(BaseModel):
    version_no: int
    effective_date: dt.date
    label: str
    added_count: int
    removed_count: int
    notes_md: str | None = None
    constituent_count: int


class VersionsOut(BaseModel):
    slug: str
    versions: list[VersionSummaryOut]
    count: int


class DiffLineOut(BaseModel):
    symbol: str
    name: str | None
    change: str  # added | removed | weight_changed | unchanged
    weight_from: Decimal | None = None
    weight_to: Decimal | None = None
    weight_pct_from: Decimal | None = None
    weight_pct_to: Decimal | None = None


class VersionDiffOut(BaseModel):
    slug: str
    from_version: int
    to_version: int
    from_effective_date: dt.date
    to_effective_date: dt.date
    added: list[DiffLineOut]
    removed: list[DiffLineOut]
    weight_changed: list[DiffLineOut]
    unchanged_count: int


def _weight_pct(weight: Decimal) -> Decimal:
    return (weight * Decimal("100")).quantize(Decimal("0.01"))


def diff_weight_maps(
    from_weights: dict[str, tuple[str | None, Decimal]],
    to_weights: dict[str, tuple[str | None, Decimal]],
) -> tuple[list[DiffLineOut], list[DiffLineOut], list[DiffLineOut], int]:
    """Pure diff used by the route and by unit tests (fails on the empty/old no-diff behaviour)."""
    added: list[DiffLineOut] = []
    removed: list[DiffLineOut] = []
    changed: list[DiffLineOut] = []
    unchanged = 0
    all_symbols = sorted(set(from_weights) | set(to_weights))
    for symbol in all_symbols:
        left = from_weights.get(symbol)
        right = to_weights.get(symbol)
        if left is None and right is not None:
            name, weight = right
            added.append(
                DiffLineOut(
                    symbol=symbol,
                    name=name,
                    change="added",
                    weight_to=weight,
                    weight_pct_to=_weight_pct(weight),
                )
            )
        elif left is not None and right is None:
            name, weight = left
            removed.append(
                DiffLineOut(
                    symbol=symbol,
                    name=name,
                    change="removed",
                    weight_from=weight,
                    weight_pct_from=_weight_pct(weight),
                )
            )
        elif left is not None and right is not None:
            name_from, w_from = left
            name_to, w_to = right
            if w_from != w_to:
                changed.append(
                    DiffLineOut(
                        symbol=symbol,
                        name=name_to or name_from,
                        change="weight_changed",
                        weight_from=w_from,
                        weight_to=w_to,
                        weight_pct_from=_weight_pct(w_from),
                        weight_pct_to=_weight_pct(w_to),
                    )
                )
            else:
                unchanged += 1
    return added, removed, changed, unchanged


async def _weights_for_version(
    session: SessionDep, version_id: int
) -> dict[str, tuple[str | None, Decimal]]:
    rows = (
        await session.execute(
            select(Instrument.symbol, Instrument.name, CbConstituent.weight)
            .join(Instrument, Instrument.id == CbConstituent.instrument_id)
            .where(CbConstituent.version_id == version_id)
        )
    ).all()
    out: dict[str, tuple[str | None, Decimal]] = {}
    for symbol, name, weight in rows:
        out[str(symbol)] = (str(name) if name is not None else None, Decimal(weight))
    return out


@router.get("/explore/{slug}/versions", response_model=VersionsOut)
async def list_basket_versions(
    slug: SlugPath, session: SessionDep, principal: AuthenticatedDep
) -> VersionsOut:
    principal.require_user()
    basket = (
        await session.execute(select(CbBasket).where(CbBasket.slug == slug, *_visible()))
    ).scalar_one_or_none()
    if basket is None:
        raise not_found("basket", slug)

    versions = (
        await session.execute(
            select(CbBasketVersion)
            .where(CbBasketVersion.basket_id == basket.id)
            .order_by(CbBasketVersion.version_no.desc())
        )
    ).scalars().all()

    counts = {
        int(version_id): int(n)
        for version_id, n in (
            await session.execute(
                select(CbConstituent.version_id, func.count())
                .where(
                    CbConstituent.version_id.in_([v.id for v in versions] or [0]),
                )
                .group_by(CbConstituent.version_id)
            )
        ).all()
    }

    items = [
        VersionSummaryOut(
            version_no=v.version_no,
            effective_date=v.effective_date,
            label=v.label,
            added_count=v.added_count,
            removed_count=v.removed_count,
            notes_md=v.notes_md,
            constituent_count=counts.get(v.id, 0),
        )
        for v in versions
    ]
    return VersionsOut(slug=slug, versions=items, count=len(items))


@router.get("/explore/{slug}/versions/diff", response_model=VersionDiffOut)
async def diff_basket_versions(
    slug: SlugPath,
    session: SessionDep,
    principal: AuthenticatedDep,
    from_version: Annotated[int, Query(ge=1, alias="from")],
    to_version: Annotated[int, Query(ge=1, alias="to")],
) -> VersionDiffOut:
    """Diff constituent weights between any two published versions of a basket."""
    principal.require_user()
    if from_version == to_version:
        raise bad_request("from and to must name different versions")

    basket = (
        await session.execute(select(CbBasket).where(CbBasket.slug == slug, *_visible()))
    ).scalar_one_or_none()
    if basket is None:
        raise not_found("basket", slug)

    async def _load(version_no: int) -> CbBasketVersion:
        row = (
            await session.execute(
                select(CbBasketVersion).where(
                    CbBasketVersion.basket_id == basket.id,
                    CbBasketVersion.version_no == version_no,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise not_found("basket version", f"{slug}#{version_no}")
        return row

    left = await _load(from_version)
    right = await _load(to_version)
    from_map = await _weights_for_version(session, left.id)
    to_map = await _weights_for_version(session, right.id)
    added, removed, changed, unchanged = diff_weight_maps(from_map, to_map)
    return VersionDiffOut(
        slug=slug,
        from_version=from_version,
        to_version=to_version,
        from_effective_date=left.effective_date,
        to_effective_date=right.effective_date,
        added=added,
        removed=removed,
        weight_changed=changed,
        unchanged_count=unchanged,
    )
