"""Migration acceptance criteria for Prompt 1, run against a real TimescaleDB.

* `make migrate` on an empty database succeeds
* `alembic downgrade base` then `upgrade head` round-trips cleanly
* every table in docs/04 exists with the documented primary key
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from decile_core.models import Base
from decile_core.universes import UNIVERSES

ENV_VAR = "DECILE_TEST_DATABASE_URL"

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        os.environ.get(ENV_VAR) is None,
        reason=f"{ENV_VAR} is not set; run `make up` and export it",
    ),
]

API_DIR = Path(__file__).resolve().parents[1]

#: Prompt 1 deliverable 2 names these three explicitly.
HYPERTABLES = ("ohlcv_daily", "factor_daily", "index_member_daily")


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    url = os.environ[ENV_VAR]
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=API_DIR,
        env={**_clean_env(), "DECILE_DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=True,
    )


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "DECILE_DATABASE_URL"}


@pytest.mark.asyncio
async def test_upgrade_creates_every_documented_table(clean_database: AsyncConnection) -> None:
    _alembic("upgrade", "head")
    rows = await clean_database.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    )
    present = {r[0] for r in rows}
    assert set(Base.metadata.tables) <= present


@pytest.mark.asyncio
async def test_primary_keys_match_the_models(clean_database: AsyncConnection) -> None:
    _alembic("upgrade", "head")
    for table_name, table in Base.metadata.tables.items():
        rows = await clean_database.execute(
            text(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = cast(:t AS regclass) AND i.indisprimary ORDER BY a.attnum"
            ),
            {"t": table_name},
        )
        actual = {r[0] for r in rows}
        expected = {c.name for c in table.primary_key.columns}
        assert actual == expected, f"{table_name} primary key mismatch"


@pytest.mark.asyncio
async def test_hypertables_are_created(clean_database: AsyncConnection) -> None:
    """docs/04 partitions these three on `date`; without it the compression policy is invalid."""
    _alembic("upgrade", "head")
    rows = await clean_database.execute(
        text("SELECT hypertable_name FROM timescaledb_information.hypertables")
    )
    assert {r[0] for r in rows} == set(HYPERTABLES)


@pytest.mark.asyncio
async def test_ohlcv_compression_policy_is_ninety_days(clean_database: AsyncConnection) -> None:
    """docs/04: add_compression_policy('ohlcv_daily', INTERVAL '90 days')."""
    _alembic("upgrade", "head")
    rows = await clean_database.execute(
        text(
            "SELECT hypertable_name, config FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_compression'"
        )
    )
    policies = {r[0]: r[1] for r in rows}
    assert "ohlcv_daily" in policies
    assert policies["ohlcv_daily"]["compress_after"] == "90 days"


@pytest.mark.asyncio
async def test_citext_is_used_for_user_email(clean_database: AsyncConnection) -> None:
    """docs/04: app_user.email is citext, so uniqueness is case-insensitive."""
    _alembic("upgrade", "head")
    row = await clean_database.execute(
        text(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_name = 'app_user' AND column_name = 'email'"
        )
    )
    assert row.scalar_one() == "citext"


@pytest.mark.asyncio
async def test_downgrade_to_base_then_upgrade_round_trips(
    clean_database: AsyncConnection,
) -> None:
    """The acceptance criterion, exactly as Prompt 1 words it."""
    _alembic("upgrade", "head")
    _alembic("downgrade", "base")

    rows = await clean_database.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
        )
    )
    assert {r[0] for r in rows} == set()

    _alembic("upgrade", "head")
    rows = await clean_database.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    )
    assert set(Base.metadata.tables) <= {r[0] for r in rows}

    hypertables = await clean_database.execute(
        text("SELECT hypertable_name FROM timescaledb_information.hypertables")
    )
    assert {r[0] for r in hypertables} == set(HYPERTABLES)


@pytest.mark.asyncio
async def test_no_float_columns_in_the_created_database(
    clean_database: AsyncConnection,
) -> None:
    """The acceptance criterion is about the real database, not just the model metadata."""
    _alembic("upgrade", "head")
    rows = await clean_database.execute(
        text(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND data_type IN ('real', 'double precision')"
        )
    )
    assert [tuple(r) for r in rows] == []


@pytest.mark.asyncio
async def test_universe_mask_fits_the_column(clean_database: AsyncConnection) -> None:
    """universe_mask is `integer`; a 15th universe would need a wider column, not a silent wrap."""
    _alembic("upgrade", "head")
    row = await clean_database.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'factor_daily' AND column_name = 'universe_mask'"
        )
    )
    assert row.scalar_one() == "integer"
    assert max(u.mask_bit for u in UNIVERSES) < 31
