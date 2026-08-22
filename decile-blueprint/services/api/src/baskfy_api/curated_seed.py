"""Idempotent seed rows for the curated-basket layer (SC1).

Managers are written by ``make seed`` via ``seed_reference``. The sole-user id resolver reads
``BASKFY_SOLE_USER_ID`` when set, otherwise the e2e account after ``seed_e2e_account``.
"""

from __future__ import annotations

import os

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.curated_baskets import (
    MANAGER_SLUG_BASKFY_ENGINE,
    MANAGER_SLUG_MAULIK,
    SOLE_USER_ENV,
)
from baskfy_core.models import AppUser, CbManager

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


async def resolve_sole_user_id(session: AsyncSession) -> int:
    """Return the sole tenant's ``app_user.id`` for user-scoped ``cb_*`` rows.

    Prefers ``BASKFY_SOLE_USER_ID`` from the environment. When unset, ensures the e2e account
    exists and returns its id — the same account the browser suite signs in as.
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
