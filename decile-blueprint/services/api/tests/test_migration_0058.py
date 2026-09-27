"""Migration 0058 — Qullamaggie's exit state on both position tables, the widened close reasons and
VBT's RAISE_GTT_STOP kind (``gates/live-9-qulla-exits.md`` Q2; DECISIONS-LV LV9.0)."""

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


async def _columns(connection: AsyncConnection, table: str) -> set[str]:
    rows = await connection.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    )
    return {r[0] for r in rows}


async def _check(connection: AsyncConnection, name: str) -> str:
    rows = await connection.execute(
        text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
        {"n": name},
    )
    return str(rows.scalar_one())


@pytest.mark.asyncio
async def test_upgrade_adds_the_exit_state_and_widens_the_checks(
    clean_database: AsyncConnection,
) -> None:
    _alembic("upgrade", "head")
    for table in ("tw_position", "vb_position"):
        columns = await _columns(clean_database, table)
        assert {"partial_done", "trail"} <= columns, table
        assert columns == {c.name for c in Base.metadata.tables[table].columns}, table
    assert {"partial_queued_for", "partial_quantity", "exit_queued_for", "exit_reason_queued"} <= (
        await _columns(clean_database, "tw_position")
    )
    assert "'MA_TRAIL'" in await _check(clean_database, "ck_tw_position_close_reason_known")
    vb_close = await _check(clean_database, "ck_vb_position_close_reason_known")
    assert "'MA_TRAIL'" in vb_close and "'PARTIAL'" in vb_close
    assert "'RAISE_GTT_STOP'" in await _check(clean_database, "ck_vb_plan_line_kind_known")


@pytest.mark.asyncio
async def test_partial_done_defaults_false_so_existing_positions_are_untouched(
    clean_database: AsyncConnection,
) -> None:
    _alembic("upgrade", "head")
    rows = await clean_database.execute(
        text(
            "SELECT table_name, column_default, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'partial_done' "
            "AND table_name IN ('tw_position', 'vb_position')"
        )
    )
    seen = {r[0]: (r[1], r[2]) for r in rows}
    assert set(seen) == {"tw_position", "vb_position"}
    assert all(default == "false" and nullable == "NO" for default, nullable in seen.values())


@pytest.mark.asyncio
async def test_downgrade_removes_the_columns_and_restores_the_narrow_checks(
    clean_database: AsyncConnection,
) -> None:
    _alembic("upgrade", "head")
    _alembic("downgrade", "0057_live_scans")
    for table in ("tw_position", "vb_position"):
        assert "partial_done" not in await _columns(clean_database, table), table
    assert "'MA_TRAIL'" not in await _check(clean_database, "ck_tw_position_close_reason_known")
    assert "'RAISE_GTT_STOP'" not in await _check(clean_database, "ck_vb_plan_line_kind_known")
    _alembic("upgrade", "head")
