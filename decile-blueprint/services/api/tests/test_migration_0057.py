"""Migration 0057 — LIVE plans, ``BUY_AT_MARKET`` VBT lines and ``provisional`` on the two
scan-run tables and the five detection tables (``gates/live-8-live-scans.md`` V1; DECISIONS-LV
LV8.0, Maulik 28 Sep 2026)."""

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
PROVISIONAL_TABLES = (
    "tw_scan_run",
    "vb_scan_run",
    "tw_signal_daily",
    "tw_state_daily",
    "tw_breadth_daily",
    "vb_signal_daily",
    "vb_breadth_daily",
)


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
async def test_upgrade_adds_provisional_everywhere_and_widens_the_three_checks(
    clean_database: AsyncConnection,
) -> None:
    _alembic("upgrade", "head")
    for table in PROVISIONAL_TABLES:
        columns = await _columns(clean_database, table)
        assert "provisional" in columns, table
        assert columns == {c.name for c in Base.metadata.tables[table].columns}, table
    assert "'LIVE'" in await _check(clean_database, "ck_tw_plan_source_known")
    assert "'LIVE'" in await _check(clean_database, "ck_vb_plan_source_known")
    assert "'BUY_AT_MARKET'" in await _check(clean_database, "ck_vb_plan_line_kind_known")


@pytest.mark.asyncio
async def test_provisional_defaults_false_so_the_nightly_writes_are_unchanged(
    clean_database: AsyncConnection,
) -> None:
    _alembic("upgrade", "head")
    rows = await clean_database.execute(
        text(
            "SELECT table_name, column_default, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'provisional' "
            "AND table_name = ANY(:t)"
        ),
        {"t": list(PROVISIONAL_TABLES)},
    )
    seen = {r[0]: (r[1], r[2]) for r in rows}
    assert set(seen) == set(PROVISIONAL_TABLES)
    assert all(default == "false" and nullable == "NO" for default, nullable in seen.values())


@pytest.mark.asyncio
async def test_downgrade_removes_the_columns_and_restores_the_narrow_checks(
    clean_database: AsyncConnection,
) -> None:
    _alembic("upgrade", "head")
    _alembic("downgrade", "0056_eq_minute_bar")
    for table in PROVISIONAL_TABLES:
        assert "provisional" not in await _columns(clean_database, table), table
    assert "'LIVE'" not in await _check(clean_database, "ck_tw_plan_source_known")
    assert "'BUY_AT_MARKET'" not in await _check(clean_database, "ck_vb_plan_line_kind_known")
    _alembic("upgrade", "head")
