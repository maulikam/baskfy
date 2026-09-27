"""Migration 0055 — the live pack's five tables exist after ``upgrade head``, match the models, and
leave nothing behind on ``downgrade`` (``gates/live-2-reconcile.md`` F8)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from baskfy_core.models import Base

ENV_VAR = "BASKFY_TEST_DATABASE_URL"

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        os.environ.get(ENV_VAR) is None,
        reason=f"{ENV_VAR} is not set; run `make up` and export it",
    ),
]

API_DIR = Path(__file__).resolve().parents[1]
TABLES = ("lv_protection_issue", "lv_heartbeat", "lv_exit_order", "lv_adoption", "risk_ledger")


def _alembic(*args: str) -> None:
    url = os.environ[ENV_VAR]
    env = {k: v for k, v in os.environ.items() if k != "BASKFY_DATABASE_URL"}
    subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=API_DIR,
        env={**env, "BASKFY_DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=True,
    )


async def _tables(connection: AsyncConnection) -> set[str]:
    rows = await connection.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    )
    return {r[0] for r in rows}


@pytest.mark.asyncio
async def test_the_five_tables_exist_and_match_the_models(
    clean_database: AsyncConnection,
) -> None:
    _alembic("upgrade", "head")
    present = await _tables(clean_database)
    assert set(TABLES) <= present
    for name in TABLES:
        model = Base.metadata.tables[name]
        rows = await clean_database.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": name},
        )
        assert {r[0] for r in rows} == {c.name for c in model.columns}, name


@pytest.mark.asyncio
async def test_one_open_issue_per_position_and_kind(clean_database: AsyncConnection) -> None:
    """The partial unique index: a second open row for the same finding is refused; a resolved
    one does not block a new open one."""
    _alembic("upgrade", "head")
    rows = await clean_database.execute(
        text(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'lv_protection_issue' "
            "AND indexname = 'uq_lv_protection_issue_open'"
        )
    )
    definition = rows.scalar_one()
    assert "UNIQUE" in definition
    assert "resolved_at IS NULL" in definition


@pytest.mark.asyncio
async def test_downgrade_drops_all_five(clean_database: AsyncConnection) -> None:
    _alembic("upgrade", "head")
    _alembic("downgrade", "0054_candidate_review_label")
    present = await _tables(clean_database)
    assert not (set(TABLES) & present)
    _alembic("upgrade", "head")
    assert set(TABLES) <= await _tables(clean_database)
