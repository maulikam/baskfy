"""Live-Postgres tests for the curated-basket version write seam."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import cast

import pytest
import screener_helpers
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.curated_baskets import (
    BasketNotFoundError,
    CuratedBasketRepository,
    EffectiveDateNotIncreasingError,
    InvalidBasketIdError,
)
from baskfy_core.curated_baskets import (
    BasketValidationError,
    ConstituentDraft,
    VersionDraft,
)
from baskfy_core.models import BasketConstituent, BasketVersion, CuratedBasket, CuratedManager

pytestmark = [pytest.mark.db, screener_helpers.requires_db]


class ExplodingSession:
    """Fails if invalid identity validation reaches any database-session operation."""

    def __getattribute__(self, name: str) -> object:
        raise AssertionError(f"invalid basket_id reached session.{name}")


async def add_basket(session: AsyncSession, suffix: str) -> int:
    manager = CuratedManager(
        slug=f"manager-{suffix}",
        name=f"Manager {suffix}",
        kind="HUMAN",
        sebi_reg_no=None,
        bio="",
        strategies=[],
        disclosures_md="",
    )
    session.add(manager)
    await session.flush()
    basket = CuratedBasket(
        slug=f"basket-{suffix}",
        name=f"Basket {suffix}",
        manager_id=manager.id,
        type="STOCK",
        access="FREE",
        visibility="PUBLISHED",
        categories=[],
        description_md="",
        rationale_md="",
        rebalance_frequency="MONTHLY",
        benchmark_instrument_id=None,
        launched_at=dt.date(2026, 1, 1),
        next_review_at=None,
        source="MANUAL",
        scan_strategy_key=None,
        archived_at=None,
    )
    session.add(basket)
    await session.flush()
    return basket.id


def draft(
    on: dt.date,
    *constituents: ConstituentDraft,
    notes_md: str | None = None,
) -> VersionDraft:
    return VersionDraft(effective_date=on, constituents=constituents, notes_md=notes_md)


def constituent(instrument_id: int, segment: str, weight: str) -> ConstituentDraft:
    return ConstituentDraft(
        instrument_id=instrument_id,
        segment=segment,
        weight=Decimal(weight),
    )


async def row_counts(session: AsyncSession) -> tuple[int, int]:
    versions = await session.scalar(select(func.count()).select_from(BasketVersion))
    constituents = await session.scalar(select(func.count()).select_from(BasketConstituent))
    assert versions is not None
    assert constituents is not None
    return versions, constituents


@pytest.mark.parametrize("basket_id", [0, -1, True])
@pytest.mark.asyncio
async def test_invalid_basket_id_is_rejected_before_any_query(basket_id: int) -> None:
    repository = CuratedBasketRepository(cast("AsyncSession", ExplodingSession()))

    with pytest.raises(InvalidBasketIdError, match="positive integer"):
        await repository.create_version(
            basket_id,
            draft(
                dt.date(2026, 8, 1),
                constituent(1, "Largecap", "1.0000"),
            ),
        )


@pytest.mark.asyncio
async def test_invalid_draft_leaves_zero_rows(screener_session: AsyncSession) -> None:
    basket_id = await add_basket(screener_session, "invalid")
    instrument_id = await screener_helpers.add_instrument(screener_session, "CBINVALID")
    repository = CuratedBasketRepository(screener_session)

    with pytest.raises(BasketValidationError, match="total exactly"):
        await repository.create_version(
            basket_id,
            draft(
                dt.date(2026, 8, 1),
                constituent(instrument_id, "Largecap", "0.9999"),
            ),
        )

    assert await row_counts(screener_session) == (0, 0)


@pytest.mark.asyncio
async def test_missing_basket_raises_typed_error_without_writes(
    screener_session: AsyncSession,
) -> None:
    instrument_id = await screener_helpers.add_instrument(screener_session, "CBMISSING")
    repository = CuratedBasketRepository(screener_session)

    with pytest.raises(BasketNotFoundError, match="does not exist"):
        await repository.create_version(
            9_999_999,
            draft(
                dt.date(2026, 8, 1),
                constituent(instrument_id, "Largecap", "1.0000"),
            ),
        )

    assert await row_counts(screener_session) == (0, 0)


@pytest.mark.asyncio
async def test_two_versions_compute_diff_label_and_counts(
    screener_session: AsyncSession,
) -> None:
    basket_id = await add_basket(screener_session, "diff")
    first_id = await screener_helpers.add_instrument(screener_session, "CBFIRST")
    removed_id = await screener_helpers.add_instrument(screener_session, "CBREMOVE")
    added_id = await screener_helpers.add_instrument(screener_session, "CBADDED")
    repository = CuratedBasketRepository(screener_session)

    genesis = await repository.create_version(
        basket_id,
        draft(
            dt.date(2026, 8, 1),
            constituent(first_id, "Largecap", "0.6000"),
            constituent(removed_id, "Midcap", "0.4000"),
            notes_md="first",
        ),
    )
    changed = await repository.create_version(
        basket_id,
        draft(
            dt.date(2026, 8, 8),
            constituent(first_id, "Largecap", "0.5000"),
            constituent(added_id, "Smallcap", "0.5000"),
            notes_md="second",
        ),
    )

    assert (genesis.version_no, genesis.label, genesis.added_count, genesis.removed_count) == (
        1,
        "GENESIS",
        2,
        0,
    )
    assert (changed.version_no, changed.label, changed.added_count, changed.removed_count) == (
        2,
        "CHANGED",
        1,
        1,
    )
    stored = (
        await screener_session.execute(
            select(
                BasketConstituent.instrument_id,
                BasketConstituent.segment,
                BasketConstituent.weight,
            )
            .where(BasketConstituent.version_id == changed.id)
            .order_by(BasketConstituent.instrument_id)
        )
    ).tuples()
    assert list(stored) == sorted(
        [
            (first_id, "Largecap", Decimal("0.5000")),
            (added_id, "Smallcap", Decimal("0.5000")),
        ]
    )


@pytest.mark.asyncio
async def test_identical_constituents_get_no_change_label_and_zero_counts(
    screener_session: AsyncSession,
) -> None:
    basket_id = await add_basket(screener_session, "same")
    first_id = await screener_helpers.add_instrument(screener_session, "CBSAME1")
    second_id = await screener_helpers.add_instrument(screener_session, "CBSAME2")
    repository = CuratedBasketRepository(screener_session)
    holdings = (
        constituent(first_id, "Largecap", "0.5000"),
        constituent(second_id, "Midcap", "0.5000"),
    )
    await repository.create_version(basket_id, draft(dt.date(2026, 8, 1), *holdings))

    no_change = await repository.create_version(
        basket_id,
        draft(dt.date(2026, 8, 8), *reversed(holdings)),
    )

    assert (no_change.label, no_change.added_count, no_change.removed_count) == (
        "NO_CHANGE",
        0,
        0,
    )


@pytest.mark.parametrize(
    ("segment", "first_weight", "second_weight"),
    [
        ("Midcap", "0.6000", "0.4000"),
        ("Largecap", "0.7000", "0.3000"),
    ],
)
@pytest.mark.asyncio
async def test_segment_or_weight_diff_gets_changed_label(
    screener_session: AsyncSession,
    segment: str,
    first_weight: str,
    second_weight: str,
) -> None:
    basket_id = await add_basket(screener_session, f"attribute-{segment}-{first_weight}")
    first_id = await screener_helpers.add_instrument(
        screener_session, f"CBATTR{segment[:1]}{first_weight[-1]}"
    )
    second_id = await screener_helpers.add_instrument(
        screener_session, f"CBATTR2{segment[:1]}{first_weight[-1]}"
    )
    repository = CuratedBasketRepository(screener_session)
    await repository.create_version(
        basket_id,
        draft(
            dt.date(2026, 8, 1),
            constituent(first_id, "Largecap", "0.6000"),
            constituent(second_id, "Midcap", "0.4000"),
        ),
    )

    changed = await repository.create_version(
        basket_id,
        draft(
            dt.date(2026, 8, 8),
            constituent(first_id, segment, first_weight),
            constituent(second_id, "Midcap", second_weight),
        ),
    )

    assert (changed.label, changed.added_count, changed.removed_count) == ("CHANGED", 0, 0)


@pytest.mark.parametrize("on", [dt.date(2026, 8, 1), dt.date(2026, 7, 31)])
@pytest.mark.asyncio
async def test_effective_date_must_strictly_increase(
    screener_session: AsyncSession,
    on: dt.date,
) -> None:
    basket_id = await add_basket(screener_session, f"date-{on.isoformat()}")
    instrument_id = await screener_helpers.add_instrument(screener_session, f"CBDATE{on.day}")
    repository = CuratedBasketRepository(screener_session)
    holdings = (constituent(instrument_id, "Largecap", "1.0000"),)
    await repository.create_version(basket_id, draft(dt.date(2026, 8, 1), *holdings))

    with pytest.raises(EffectiveDateNotIncreasingError, match="strictly later"):
        await repository.create_version(basket_id, draft(on, *holdings))

    assert await row_counts(screener_session) == (1, 1)


@pytest.mark.asyncio
async def test_written_version_mutation_is_rejected_by_database_trigger(
    screener_session: AsyncSession,
) -> None:
    basket_id = await add_basket(screener_session, "immutable")
    instrument_id = await screener_helpers.add_instrument(screener_session, "CBIMMUTABLE")
    version = await CuratedBasketRepository(screener_session).create_version(
        basket_id,
        draft(
            dt.date(2026, 8, 1),
            constituent(instrument_id, "Largecap", "1.0000"),
        ),
    )

    with pytest.raises(DBAPIError, match="append-only"):
        await screener_session.execute(
            update(BasketVersion).where(BasketVersion.id == version.id).values(notes_md="rewrite")
        )
