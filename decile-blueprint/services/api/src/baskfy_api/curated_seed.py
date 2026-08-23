"""Idempotent seed rows for the curated-basket layer (SC1-SC2).

Managers are written by ``make seed`` via ``seed_reference``. The Momentum Scan basket
(``source=SCAN``) is seeded when enough instruments exist for the fixture ranking.
"""

from __future__ import annotations

import datetime as dt
import os

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.curated_baskets import (
    MANAGER_SLUG_BASKFY_ENGINE,
    MANAGER_SLUG_MAULIK,
    SOLE_USER_ENV,
    assert_weights_sum_to_one,
)
from baskfy_core.models import (
    AppUser,
    CbBasket,
    CbBasketVersion,
    CbConstituent,
    CbManager,
    Instrument,
)
from baskfy_core.scan_projection import (
    DEFAULT_SCAN_TOP_N,
    FIXTURE_SCAN_SYMBOLS,
    MOMENTUM_SCAN_BASKET_SLUG,
    project_scan_top_n,
)

MANAGER_SEED_ROWS: tuple[dict[str, object], ...] = (
    {
        "slug": MANAGER_SLUG_BASKFY_ENGINE,
        "name": "Baskfy Engine",
        "kind": "ENGINE",
        "bio": "Automated momentum strategies from the nightly MomentumScan pipeline.",
        "strategies": ["momentum-scan"],
        "disclosures_md": None,
    },
    {
        "slug": MANAGER_SLUG_MAULIK,
        "name": "Maulik",
        "kind": "HUMAN",
        "bio": "Operator-curated baskets.",
        "strategies": [],
        "disclosures_md": None,
    },
)

#: Stable scan-run id for the fixture genesis cut (deterministic seed).
FIXTURE_SCAN_RUN_ID = "fixture-momentum-scan-sc2"


async def resolve_sole_user_id(session: AsyncSession) -> int:
    """Return the sole tenant's ``app_user.id`` for user-scoped ``cb_*`` rows.

    Prefers ``BASKFY_SOLE_USER_ID`` from the environment. When unset, ensures the e2e account
    exists and returns its id - the same account the browser suite signs in as.
    """
    env_val = os.environ.get(SOLE_USER_ENV)
    if env_val is not None:
        return int(env_val)
    # Local import breaks ``seed`` ↔ ``curated_seed`` circular dependency.
    from baskfy_api.seed import E2E_PUBLIC_ID, seed_e2e_account  # noqa: PLC0415

    await seed_e2e_account(session)
    return (
        await session.execute(select(AppUser.id).where(AppUser.public_id == E2E_PUBLIC_ID))
    ).scalar_one()


async def seed_curated_managers(session: AsyncSession) -> int:
    """Upsert the two seed managers by slug (docs/smallcase/03)."""
    for row in MANAGER_SEED_ROWS:
        stmt = insert(CbManager).values(**row)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[CbManager.slug],
                set_={
                    "name": stmt.excluded.name,
                    "kind": stmt.excluded.kind,
                    "bio": stmt.excluded.bio,
                    "strategies": stmt.excluded.strategies,
                    "disclosures_md": stmt.excluded.disclosures_md,
                },
            )
        )
    return len(MANAGER_SEED_ROWS)


async def count_curated_managers(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(CbManager))).scalar_one())


async def seed_momentum_scan_basket(
    session: AsyncSession,
    *,
    ranked_symbols: tuple[str, ...] = FIXTURE_SCAN_SYMBOLS,
    top_n: int = DEFAULT_SCAN_TOP_N,
    as_of: dt.date | None = None,
    scan_run_id: str = FIXTURE_SCAN_RUN_ID,
) -> int:
    """Project a MomentumScan-like top-N into ``cb_basket(source=SCAN)`` + genesis version.

    Idempotent: re-running does not add a second version when the genesis row already exists.
    Returns 0 when fewer than ``top_n`` of the ranked symbols exist in ``instrument``.
    """
    await seed_curated_managers(session)
    projection = project_scan_top_n(ranked_symbols, top_n=top_n, scan_run_id=scan_run_id)
    assert_weights_sum_to_one(projection.weights)

    symbols = [c.symbol for c in projection.constituents]
    rows = (
        await session.execute(
            select(Instrument.symbol, Instrument.id).where(Instrument.symbol.in_(symbols))
        )
    ).all()
    by_symbol = {str(sym): int(iid) for sym, iid in rows}
    if len(by_symbol) < top_n:
        return 0

    manager_id = (
        await session.execute(
            select(CbManager.id).where(CbManager.slug == MANAGER_SLUG_BASKFY_ENGINE)
        )
    ).scalar_one()
    effective = as_of or dt.date(2024, 1, 2)

    basket_stmt = insert(CbBasket).values(
        slug=projection.slug,
        name=projection.name,
        manager_id=manager_id,
        type="STOCK",
        access="FREE",
        visibility="PUBLISHED",
        categories=["momentum"],
        description_md="Equal-weight top-N projection of the MomentumScan strategy.",
        rationale_md=None,
        rebalance_frequency="WEEKLY",
        benchmark_instrument_id=None,
        launched_at=effective,
        next_review_at=None,
        source="SCAN",
        scan_strategy_key=projection.strategy_key,
        archived_at=None,
    )
    await session.execute(
        basket_stmt.on_conflict_do_update(
            index_elements=[CbBasket.slug],
            set_={
                "name": basket_stmt.excluded.name,
                "description_md": basket_stmt.excluded.description_md,
                "categories": basket_stmt.excluded.categories,
                "rebalance_frequency": basket_stmt.excluded.rebalance_frequency,
                "scan_strategy_key": basket_stmt.excluded.scan_strategy_key,
            },
        )
    )
    basket_id = (
        await session.execute(select(CbBasket.id).where(CbBasket.slug == MOMENTUM_SCAN_BASKET_SLUG))
    ).scalar_one()

    existing = (
        await session.execute(
            select(CbBasketVersion.id).where(
                CbBasketVersion.basket_id == basket_id,
                CbBasketVersion.version_no == projection.version_no,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return 1

    version = CbBasketVersion(
        basket_id=basket_id,
        version_no=projection.version_no,
        effective_date=effective,
        label=projection.label,
        added_count=len(projection.constituents),
        removed_count=0,
        notes_md="SC2 fixture genesis cut.",
        source_scan_run_id=projection.source_scan_run_id,
    )
    session.add(version)
    await session.flush()
    for constituent in projection.constituents:
        session.add(
            CbConstituent(
                version_id=version.id,
                instrument_id=by_symbol[constituent.symbol],
                segment=constituent.segment,
                weight=constituent.weight,
            )
        )
    await session.flush()
    return 1
