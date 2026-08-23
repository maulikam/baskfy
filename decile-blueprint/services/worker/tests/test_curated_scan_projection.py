"""Deterministic, non-executing projection of the first MomentumScan product basket."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import helpers
import polars as pl
import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.seed import (
    CURATED_SCAN_BASKET_SLUG,
    CURATED_SCAN_SCREEN_PUBLIC_ID,
    CuratedScanProjectionError,
    seed_curated_momentum_scan,
)
from baskfy_core.models import (
    BasketConstituent,
    BasketVersion,
    CuratedBasket,
    Screen,
    ScreenRun,
)
from baskfy_core.momentum_scan import MomentumScan
from baskfy_core.screen_definition import ScreenDefinition

pytestmark = [pytest.mark.db, helpers.requires_db]

AS_OF = dt.date(2026, 8, 18)


def fixture_scan(
    *symbols: str,
    identity: str = "fixture-momentum-scan-v1",
    universe: str = "nse_cash",
    data_version: int = 7,
) -> MomentumScan:
    """A selected top-N scan artifact; projection deliberately does not re-run ranking."""
    return MomentumScan(
        as_of=AS_OF,
        universe=universe,
        frame=pl.DataFrame({"symbol": list(symbols)}),
        screen_run_id=identity,
        data_version=data_version,
        carried=(),
        suspect_symbols=(),
    )


async def seed_symbols(session: AsyncSession, *symbols: str) -> dict[str, int]:
    return {symbol: await helpers.make_instrument(session, symbol) for symbol in symbols}


async def clear_fixture_run(session: AsyncSession, identity: str) -> None:
    """The worker suite preserves screens between runs; remove only this fixture source."""
    del identity
    source_screen = select(Screen.id).where(Screen.public_id == CURATED_SCAN_SCREEN_PUBLIC_ID)
    await session.execute(delete(ScreenRun).where(ScreenRun.screen_id.in_(source_screen)))
    await session.execute(delete(Screen).where(Screen.public_id == CURATED_SCAN_SCREEN_PUBLIC_ID))


@pytest.mark.asyncio
async def test_fixture_scan_projects_one_scan_basket_and_exact_genesis(
    session: AsyncSession,
) -> None:
    await clear_fixture_run(session, "fixture-genesis-v1")
    ids = await seed_symbols(session, "ZZZ", "AAA", "MMM")

    counts = await seed_curated_momentum_scan(
        session,
        fixture_scan("ZZZ", "AAA", "MMM", identity="fixture-genesis-v1"),
    )

    assert counts == {
        "screen_run": 1,
        "cb_basket": 1,
        "cb_basket_version": 1,
        "cb_constituent": 3,
    }
    basket = await session.scalar(
        select(CuratedBasket).where(CuratedBasket.slug == CURATED_SCAN_BASKET_SLUG)
    )
    assert basket is not None
    assert (
        basket.source,
        basket.scan_strategy_key,
        basket.visibility,
        basket.access,
        basket.launched_at,
    ) == ("SCAN", "MomentumScan", "PUBLISHED", "FREE", AS_OF)

    version = await session.scalar(
        select(BasketVersion).where(BasketVersion.basket_id == basket.id)
    )
    assert version is not None
    assert (version.version_no, version.label, version.added_count, version.removed_count) == (
        1,
        "GENESIS",
        3,
        0,
    )
    assert version.source_scan_run_id is not None
    source = await session.get(ScreenRun, version.source_scan_run_id)
    assert source is not None
    assert len(source.definition_hash) == 64
    assert version.notes_md is not None and "fixture-genesis-v1" in version.notes_md
    source_screen = await session.get(Screen, source.screen_id)
    assert source_screen is not None
    assert source_screen.user_id is not None
    assert source_screen.is_example is False
    ScreenDefinition.model_validate(source_screen.definition)
    assert source.results == [
        {"rank": 1, "instrument_id": ids["ZZZ"], "factor_value": None},
        {"rank": 2, "instrument_id": ids["AAA"], "factor_value": None},
        {"rank": 3, "instrument_id": ids["MMM"], "factor_value": None},
    ]

    stored = dict(
        (
            await session.execute(
                select(BasketConstituent.instrument_id, BasketConstituent.weight).where(
                    BasketConstituent.version_id == version.id
                )
            )
        )
        .tuples()
        .all()
    )
    assert stored == {
        ids["AAA"]: Decimal("0.3334"),
        ids["MMM"]: Decimal("0.3333"),
        ids["ZZZ"]: Decimal("0.3333"),
    }
    assert sum(stored.values(), start=Decimal(0)) == Decimal("1.0000")


@pytest.mark.asyncio
async def test_projection_is_content_idempotent_and_creates_no_forbidden_rows(
    session: AsyncSession,
) -> None:
    await clear_fixture_run(session, "fixture-idempotent-v1")
    await seed_symbols(session, "FIRST", "SECOND", "THIRD")
    scan = fixture_scan("THIRD", "SECOND", "FIRST", identity="fixture-idempotent-v1")
    first = await seed_curated_momentum_scan(session, scan)
    await session.flush()
    snapshot = (
        (
            await session.execute(
                text(
                    """
                SELECT b.id, b.slug, b.source, v.id, v.version_no, v.effective_date,
                       v.source_scan_run_id, c.instrument_id, c.weight, r.created_at
                FROM cb_basket b
                JOIN cb_basket_version v ON v.basket_id = b.id
                JOIN cb_constituent c ON c.version_id = v.id
                JOIN screen_run r ON r.id = v.source_scan_run_id
                WHERE b.slug = :slug
                ORDER BY c.instrument_id
                """
                ),
                {"slug": CURATED_SCAN_BASKET_SLUG},
            )
        )
        .tuples()
        .all()
    )

    second = await seed_curated_momentum_scan(session, scan)
    await session.flush()
    rerun_snapshot = (
        (
            await session.execute(
                text(
                    """
                SELECT b.id, b.slug, b.source, v.id, v.version_no, v.effective_date,
                       v.source_scan_run_id, c.instrument_id, c.weight, r.created_at
                FROM cb_basket b
                JOIN cb_basket_version v ON v.basket_id = b.id
                JOIN cb_constituent c ON c.version_id = v.id
                JOIN screen_run r ON r.id = v.source_scan_run_id
                WHERE b.slug = :slug
                ORDER BY c.instrument_id
                """
                ),
                {"slug": CURATED_SCAN_BASKET_SLUG},
            )
        )
        .tuples()
        .all()
    )

    assert first["cb_basket_version"] == 1
    assert second == {
        "screen_run": 0,
        "cb_basket": 0,
        "cb_basket_version": 0,
        "cb_constituent": 0,
    }
    assert rerun_snapshot == snapshot
    forbidden = (
        "cb_order_batch",
        "cb_fee_ledger",
        "cb_subscription",
        "subscription",
        "payment",
        "auth_verification_token",
        "auth_token",
        "refresh_token",
    )
    for table in forbidden:
        count = await session.scalar(text(f"SELECT count(*) FROM {table}"))
        assert count == 0, table


@pytest.mark.asyncio
async def test_missing_instrument_fails_before_any_projection_write(session: AsyncSession) -> None:
    baskets_before = await session.scalar(select(func.count()).select_from(CuratedBasket))
    runs_before = await session.scalar(select(func.count()).select_from(ScreenRun))

    with pytest.raises(CuratedScanProjectionError, match="missing NSE EQ instruments"):
        await seed_curated_momentum_scan(
            session,
            fixture_scan("NOT-IN-MASTER", identity="fixture-missing-v1"),
        )

    assert await session.scalar(select(func.count()).select_from(CuratedBasket)) == baskets_before
    assert await session.scalar(select(func.count()).select_from(ScreenRun)) == runs_before


@pytest.mark.asyncio
async def test_same_scan_identity_with_different_output_is_refused(session: AsyncSession) -> None:
    await clear_fixture_run(session, "fixture-conflict-v1")
    await seed_symbols(session, "SAMEA", "SAMEB", "SAMEC")
    await seed_curated_momentum_scan(
        session,
        fixture_scan("SAMEA", "SAMEB", identity="fixture-conflict-v1"),
    )

    with pytest.raises(CuratedScanProjectionError, match="same identity"):
        await seed_curated_momentum_scan(
            session,
            fixture_scan("SAMEA", "SAMEC", identity="fixture-conflict-v1"),
        )

    assert await session.scalar(select(func.count()).select_from(BasketVersion)) == 1


@pytest.mark.asyncio
async def test_same_output_from_different_scan_provenance_does_not_relabel_genesis(
    session: AsyncSession,
) -> None:
    await clear_fixture_run(session, "fixture-first-provenance-v1")
    await seed_symbols(session, "PROVA", "PROVB")
    await seed_curated_momentum_scan(
        session,
        fixture_scan("PROVA", "PROVB", identity="fixture-first-provenance-v1", data_version=7),
    )

    with pytest.raises(CuratedScanProjectionError, match="immutable versions are never reseeded"):
        await seed_curated_momentum_scan(
            session,
            fixture_scan(
                "PROVA",
                "PROVB",
                identity="fixture-second-provenance-v1",
                data_version=8,
            ),
        )

    version = (await session.scalars(select(BasketVersion))).one()
    assert version.notes_md == (
        "Genesis projection from MomentumScan fixture-first-provenance-v1; data version 7."
    )


@pytest.mark.parametrize(
    ("universe", "data_version", "message"),
    [
        ("nifty_500", 7, "universe must be nse_cash"),
        ("nse_cash", 0, "data_version must be a positive integer"),
        ("nse_cash", True, "data_version must be a positive integer"),
    ],
)
@pytest.mark.asyncio
async def test_malformed_scan_provenance_is_rejected_before_writes(
    session: AsyncSession,
    universe: str,
    data_version: int,
    message: str,
) -> None:
    before = (
        await session.scalar(select(func.count()).select_from(CuratedBasket)),
        await session.scalar(select(func.count()).select_from(ScreenRun)),
    )
    malformed = fixture_scan(
        "INVALID",
        identity="fixture-malformed-v1",
        universe=universe,
        data_version=data_version,
    )

    with pytest.raises(CuratedScanProjectionError, match=message):
        await seed_curated_momentum_scan(session, malformed)

    after = (
        await session.scalar(select(func.count()).select_from(CuratedBasket)),
        await session.scalar(select(func.count()).select_from(ScreenRun)),
    )
    assert after == before


@pytest.mark.asyncio
async def test_projection_has_an_explicit_fifty_constituent_memory_bound(
    session: AsyncSession,
) -> None:
    symbols = tuple(f"BOUND{index:02d}" for index in range(51))
    before = await session.scalar(select(func.count()).select_from(CuratedBasket))

    with pytest.raises(CuratedScanProjectionError, match="capped at 50 constituents"):
        await seed_curated_momentum_scan(
            session,
            fixture_scan(*symbols, identity="fixture-too-wide-v1"),
        )

    assert await session.scalar(select(func.count()).select_from(CuratedBasket)) == before
