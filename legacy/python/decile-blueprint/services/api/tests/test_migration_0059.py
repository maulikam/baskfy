"""Migration 0059 — F3's sleeves, group, structure, plan kind and ``fo_index_daily``
(``gates/f3-2-data.md`` S1; DECISIONS-FO M.5)."""

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


async def _enum_labels(connection: AsyncConnection, name: str) -> set[str]:
    rows = await connection.execute(
        text(
            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = :n"
        ),
        {"n": name},
    )
    return {r[0] for r in rows}


@pytest.mark.asyncio
async def test_upgrade_widens_the_sleeves_and_vocabulary_and_creates_fo_index_daily(
    clean_database: AsyncConnection,
) -> None:
    _alembic("upgrade", "head")
    assert {"F3N", "F3B"} <= await _enum_labels(clean_database, "fo_sleeve")
    assert "'F3'" in await _check(clean_database, "ck_fo_sleeve_config_sleeve_group_known")
    assert "'F3'" in await _check(clean_database, "ck_fo_config_audit_scope_known")
    for table in ("fo_plan", "fo_position", "fo_journal"):
        assert "'CREDIT_SPREAD'" in await _check(clean_database, f"ck_{table}_structure_known")
    assert "'ADD'" in await _check(clean_database, "ck_fo_plan_kind_known")
    columns = await _columns(clean_database, "fo_index_daily")
    assert columns == {c.name for c in Base.metadata.tables["fo_index_daily"].columns}
    assert "'BANKNIFTY'" in await _check(clean_database, "ck_fo_index_daily_underlying_known")


@pytest.mark.asyncio
async def test_fo_index_daily_is_idempotent_on_underlying_and_date(
    clean_database: AsyncConnection,
) -> None:
    _alembic("upgrade", "head")
    insert = text(
        "INSERT INTO fo_index_daily (underlying, trade_date, open, high, low, close) "
        "VALUES ('NIFTY', '2026-09-25', 25000, 25100, 24900, :c) "
        "ON CONFLICT (underlying, trade_date) DO UPDATE SET close = EXCLUDED.close"
    )
    await clean_database.execute(insert, {"c": 25050})
    await clean_database.execute(insert, {"c": 25060})
    rows = await clean_database.execute(
        text("SELECT count(*), max(close) FROM fo_index_daily WHERE underlying = 'NIFTY'")
    )
    assert tuple(rows.one()) == (1, 25060)
    with pytest.raises(Exception, match="ck_fo_index_daily_underlying_known"):
        await clean_database.execute(
            text(
                "INSERT INTO fo_index_daily (underlying, trade_date, open, high, low, close) "
                "VALUES ('FINNIFTY', '2026-09-25', 1, 1, 1, 1)"
            )
        )


@pytest.mark.asyncio
async def test_downgrade_drops_the_table_and_narrows_the_checks_but_keeps_the_labels(
    clean_database: AsyncConnection,
) -> None:
    _alembic("upgrade", "head")
    _alembic("downgrade", "0058_qulla_exits")
    rows = await clean_database.execute(text("SELECT to_regclass('public.fo_index_daily')"))
    assert rows.scalar_one() is None
    assert "'F3'" not in await _check(clean_database, "ck_fo_sleeve_config_sleeve_group_known")
    assert "'CREDIT_SPREAD'" not in await _check(clean_database, "ck_fo_plan_structure_known")
    assert "'ADD'" not in await _check(clean_database, "ck_fo_plan_kind_known")
    # Postgres cannot drop an enum value: the labels stay, unused, and the migration says so.
    assert {"F3N", "F3B"} <= await _enum_labels(clean_database, "fo_sleeve")
    _alembic("upgrade", "head")
