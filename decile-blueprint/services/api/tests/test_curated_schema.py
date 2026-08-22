"""Curated-basket schema acceptance — docs/smallcase/03 (SC1)."""

from __future__ import annotations

import os
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from baskfy_api.curated_seed import count_curated_managers, seed_curated_managers
from baskfy_core.curated_baskets import assert_weights_sum_to_one
from baskfy_core.models import Base, CbManager

ENV_VAR = "BASKFY_TEST_DATABASE_URL"
API_DIR = Path(__file__).resolve().parents[1]

CB_TABLES = frozenset(
    name for name in Base.metadata.tables if name.startswith("cb_")
)

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        os.environ.get(ENV_VAR) is None,
        reason=f"{ENV_VAR} is not set; run `make up` and export it",
    ),
]


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    url = os.environ[ENV_VAR]
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=API_DIR,
        env={k: v for k, v in os.environ.items() if k != "BASKFY_DATABASE_URL"}
        | {"BASKFY_DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def migrated(clean_database: object) -> None:
    _alembic("upgrade", "head")


@pytest.mark.asyncio
async def test_migration_creates_all_cb_tables(
    engine: AsyncEngine, migrated: None
) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name LIKE 'cb_%'"
            )
        )
        present = {r[0] for r in rows}
    assert present >= CB_TABLES
    assert len(CB_TABLES) == 18


@pytest.mark.asyncio
async def test_re_migrate_is_noop(engine: AsyncEngine, migrated: None) -> None:
    _alembic("upgrade", "head")
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name LIKE 'cb_%'"
            )
        )
        present = {r[0] for r in rows}
    assert present >= CB_TABLES


def test_bad_constituent_weights_fail_at_domain_assertion() -> None:
    with pytest.raises(ValueError, match="must sum to"):
        assert_weights_sum_to_one([Decimal("0.6000"), Decimal("0.5000")])


@pytest.mark.asyncio
async def test_seed_managers_twice_is_idempotent(engine: AsyncEngine, migrated: None) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await seed_curated_managers(session)
        await session.commit()
        first = await count_curated_managers(session)

        await seed_curated_managers(session)
        await session.commit()
        second = await count_curated_managers(session)

        rows = (await session.execute(select(CbManager.slug))).scalars().all()
        await session.commit()

    assert first == 2
    assert second == 2
    assert set(rows) == {"baskfy-engine", "maulik"}


@pytest.mark.asyncio
async def test_cb_manager_kind_check_rejects_invalid(engine: AsyncEngine, migrated: None) -> None:
    async with engine.begin() as conn:
        with pytest.raises(Exception):  # noqa: B017 — DB raises on check violation
            await conn.execute(
                text(
                    "INSERT INTO cb_manager (slug, name, kind) "
                    "VALUES ('bad', 'Bad', 'ROBOT')"
                )
            )


@pytest.mark.asyncio
async def test_cb_tables_have_no_float_columns(engine: AsyncEngine, migrated: None) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name LIKE 'cb_%' "
                "AND data_type IN ('real', 'double precision')"
            )
        )
        assert [tuple(r) for r in rows] == []
