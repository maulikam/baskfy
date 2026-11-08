"""Migration 0056 — ``eq_minute_bar`` exists as a hypertable after ``upgrade head``, matches the
model, and is gone after ``downgrade`` (``gates/live-5-eq-bars.md`` B1)."""

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
TABLE = "eq_minute_bar"


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
async def test_the_table_exists_matches_the_model_and_is_a_hypertable(
    clean_database: AsyncConnection,
) -> None:
    _alembic("upgrade", "head")
    assert TABLE in await _tables(clean_database)
    rows = await clean_database.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": TABLE},
    )
    assert {r[0] for r in rows} == {c.name for c in Base.metadata.tables[TABLE].columns}
    pk = await clean_database.execute(
        text(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = cast(:t AS regclass) AND i.indisprimary ORDER BY a.attnum"
        ),
        {"t": TABLE},
    )
    assert [r[0] for r in pk] == ["instrument_id", "ts"]
    hyper = await clean_database.execute(
        text(
            "SELECT hypertable_name FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = :t"
        ),
        {"t": TABLE},
    )
    assert [r[0] for r in hyper] == [TABLE]


@pytest.mark.asyncio
async def test_downgrade_drops_it(clean_database: AsyncConnection) -> None:
    _alembic("upgrade", "head")
    _alembic("downgrade", "0055_live_desk_state")
    assert TABLE not in await _tables(clean_database)
    _alembic("upgrade", "head")
    assert TABLE in await _tables(clean_database)
