"""Database acceptance tests for the SC1 curated-basket migration.

This module refuses to connect to anything except the two disposable SC databases.  The driver
uses ``baskfy_sc_schema_test``; branch integration uses ``baskfy_sc_test``.  Both names are
deliberately exact because the fixture drops and recreates ``public``.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from sqlalchemy import Numeric, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from baskfy_core.models import Base

ENV_VAR = "BASKFY_TEST_DATABASE_URL"
SAFE_DATABASES = frozenset({"baskfy_sc_schema_test", "baskfy_sc_test"})
API_DIR = Path(__file__).resolve().parents[1]
CB_TABLES = frozenset(name for name in Base.metadata.tables if name.startswith("cb_"))

pytestmark = [
    pytest.mark.db,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(
        os.environ.get(ENV_VAR) is None,
        reason=f"{ENV_VAR} is not set; use a disposable SC test database",
    ),
]


def _safe_url() -> str:
    url = os.environ[ENV_VAR]
    database = urlparse(url).path.removeprefix("/")
    if database not in SAFE_DATABASES:
        expected = ", ".join(sorted(SAFE_DATABASES))
        raise RuntimeError(
            f"refusing destructive schema test against {database!r}; expected {expected}"
        )
    return url


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    url = _safe_url()
    env = {key: value for key, value in os.environ.items() if key != "BASKFY_DATABASE_URL"}
    env.update({"BASKFY_DATABASE_URL": url, "DRY_RUN": "true", "BASKFY_DRY_RUN": "true"})
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def schema_database() -> AsyncIterator[AsyncConnection]:
    # AUTOCOMMIT prevents a read-only assertion in one test from retaining an implicit
    # transaction (and an AccessShareLock) while a later round-trip drops the same tables.
    engine = create_async_engine(_safe_url(), isolation_level="AUTOCOMMIT")
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


async def _present_cb_tables(connection: AsyncConnection) -> set[str]:
    rows = await connection.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name LIKE 'cb\\_%' ESCAPE '\\'"
        )
    )
    return {str(row[0]) for row in rows}


async def test_migration_creates_all_and_only_documented_cb_tables(
    schema_database: AsyncConnection,
) -> None:
    _alembic("upgrade", "head")
    assert await _present_cb_tables(schema_database) == CB_TABLES
    assert len(CB_TABLES) == 18
    rows = await schema_database.execute(
        text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name LIKE 'cb\\_%' ESCAPE '\\'"
        )
    )
    actual_columns = {tuple(row) for row in rows}
    expected_columns = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if table.name.startswith("cb_")
    }
    assert actual_columns == expected_columns


async def test_upgrade_is_idempotent(schema_database: AsyncConnection) -> None:
    _alembic("upgrade", "head")
    before = await _present_cb_tables(schema_database)
    _alembic("upgrade", "head")
    after = await _present_cb_tables(schema_database)
    assert before == after == CB_TABLES


async def test_curated_schema_round_trip(schema_database: AsyncConnection) -> None:
    _alembic("upgrade", "head")
    _alembic("downgrade", "0013_portfolio_sleeves")
    assert await _present_cb_tables(schema_database) == set()
    _alembic("upgrade", "head")
    assert await _present_cb_tables(schema_database) == CB_TABLES


async def test_numeric_columns_use_no_approximate_database_type(
    schema_database: AsyncConnection,
) -> None:
    _alembic("upgrade", "head")
    rows = await schema_database.execute(
        text(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name LIKE 'cb\\_%' ESCAPE '\\' "
            "AND data_type IN ('real', 'double precision')"
        )
    )
    assert [tuple(row) for row in rows] == []

    numeric_columns = await schema_database.execute(
        text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name LIKE 'cb\\_%' ESCAPE '\\' "
            "AND data_type = 'numeric'"
        )
    )
    actual = {tuple(row) for row in numeric_columns}
    expected = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if table.name.startswith("cb_") and isinstance(column.type, Numeric)
    }
    assert actual == expected


async def test_foreign_key_access_path_indexes_exist(schema_database: AsyncConnection) -> None:
    _alembic("upgrade", "head")
    rows = await schema_database.execute(
        text(
            "SELECT c.conrelid::regclass::text AS table_name, a.attname AS column_name "
            "FROM pg_constraint c "
            "JOIN LATERAL unnest(c.conkey) AS key(attnum) ON true "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = key.attnum "
            "WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace "
            "AND c.conrelid::regclass::text LIKE 'cb\\_%' ESCAPE '\\'"
        )
    )
    foreign_keys = {(str(row[0]), str(row[1])) for row in rows}
    missing: list[str] = []
    for table_name, column_name in sorted(foreign_keys):
        indexed = await schema_database.scalar(
            text(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_index i "
                "WHERE i.indrelid = cast(:table_name AS regclass) "
                "AND pg_get_indexdef(i.indexrelid, 1, true) = :column_name"
                ")"
            ),
            {"table_name": table_name, "column_name": column_name},
        )
        if not indexed:
            missing.append(f"{table_name}.{column_name}")
    assert missing == []


async def test_checked_vocabularies_match_the_product_spec(
    schema_database: AsyncConnection,
) -> None:
    _alembic("upgrade", "head")
    expected: dict[str, tuple[str, ...]] = {
        "ck_cb_manager_cb_manager_kind": ("ENGINE", "HUMAN", "EXTERNAL"),
        "ck_cb_basket_cb_basket_type": ("STOCK", "MF", "US"),
        "ck_cb_basket_cb_basket_access": ("FREE", "FEE"),
        "ck_cb_basket_cb_basket_visibility": ("PUBLISHED", "PRIVATE"),
        "ck_cb_basket_version_cb_basket_version_label": (
            "CHANGED",
            "NO_CHANGE",
            "GENESIS",
        ),
        "ck_cb_metrics_cb_metrics_volatility_bucket": ("LOW", "MED", "HIGH"),
        "ck_cb_investment_cb_investment_status": ("ACTIVE", "EXITED"),
        "ck_cb_order_batch_cb_order_batch_kind": (
            "BUY",
            "INVEST_MORE",
            "SIP",
            "REBALANCE",
            "EXIT",
            "PARTIAL_EXIT",
            "CUSTOMIZE",
        ),
        "ck_cb_order_batch_cb_order_batch_status": (
            "DRAFT",
            "PLANNED",
            "EXPIRED",
            "EXECUTED",
            "PARTIAL",
            "CANCELLED",
        ),
        "ck_cb_sip_plan_cb_sip_plan_mode": ("REMINDER",),
        "ck_cb_pending_action_cb_pending_action_type": (
            "DRIFT",
            "REBALANCE_AVAILABLE",
            "SIP_DUE",
            "GENERIC",
        ),
        "ck_cb_plan_cb_plan_duration": ("M1", "M3", "M6", "Y1"),
        "ck_cb_subscription_cb_subscription_status": ("ACTIVE", "CANCELLED", "LAPSED"),
    }
    rows = await schema_database.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = ANY(cast(:names AS text[]))"
        ),
        {"names": list(expected)},
    )
    definitions = {str(row[0]): str(row[1]) for row in rows}
    assert definitions.keys() == expected.keys()
    for name, values in expected.items():
        definition = definitions[name]
        assert all(f"'{value}'" in definition for value in values)
    assert "AUTO" not in definitions["ck_cb_sip_plan_cb_sip_plan_mode"]


async def test_postgresql_document_types_and_version_constraints(
    schema_database: AsyncConnection,
) -> None:
    _alembic("upgrade", "head")
    rows = await schema_database.execute(
        text(
            "SELECT table_name, column_name, data_type, udt_name "
            "FROM information_schema.columns WHERE table_schema = 'public' "
            "AND (table_name, column_name) IN ("
            "('cb_manager', 'strategies'), ('cb_basket', 'categories'), "
            "('cb_collection', 'basket_ids'), ('cb_collection', 'curated_meta'), "
            "('cb_pending_action', 'payload'))"
        )
    )
    types = {(str(row[0]), str(row[1])): (str(row[2]), str(row[3])) for row in rows}
    assert types == {
        ("cb_manager", "strategies"): ("ARRAY", "_varchar"),
        ("cb_basket", "categories"): ("ARRAY", "_varchar"),
        ("cb_collection", "basket_ids"): ("ARRAY", "_int8"),
        ("cb_collection", "curated_meta"): ("jsonb", "jsonb"),
        ("cb_pending_action", "payload"): ("jsonb", "jsonb"),
    }

    constraints = await schema_database.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname IN ('uq_cb_basket_version_basket_version_no', "
            "'uq_cb_basket_version_basket_effective_date', "
            "'ck_cb_constituent_cb_constituent_weight_range')"
        )
    )
    definitions = {str(row[0]): str(row[1]) for row in constraints}
    assert "UNIQUE (basket_id, version_no)" in definitions["uq_cb_basket_version_basket_version_no"]
    assert (
        "UNIQUE (basket_id, effective_date)"
        in definitions["uq_cb_basket_version_basket_effective_date"]
    )
    weight = definitions["ck_cb_constituent_cb_constituent_weight_range"]
    assert "weight >" in weight and "weight <=" in weight


async def test_order_batch_identity_and_fee_cycle_are_structurally_safe(
    schema_database: AsyncConnection,
) -> None:
    _alembic("upgrade", "head")
    columns = await schema_database.execute(
        text(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'cb_order_batch' "
            "AND column_name IN ('user_id', 'broker_account_id', 'desk_plan_id')"
        )
    )
    nullability = {str(row[0]): str(row[1]) for row in columns}
    assert nullability == {
        "user_id": "NO",
        "broker_account_id": "NO",
        "desk_plan_id": "YES",
    }

    rows = await schema_database.execute(
        text(
            "SELECT tc.table_name, kcu.column_name, ccu.table_name AS target_table "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "ON tc.constraint_name = kcu.constraint_name "
            "AND tc.constraint_schema = kcu.constraint_schema "
            "JOIN information_schema.constraint_column_usage ccu "
            "ON tc.constraint_name = ccu.constraint_name "
            "AND tc.constraint_schema = ccu.constraint_schema "
            "WHERE tc.constraint_type = 'FOREIGN KEY' "
            "AND tc.table_name IN ('cb_order_batch', 'cb_fee_ledger')"
        )
    )
    foreign_keys = {(str(row[0]), str(row[1]), str(row[2])) for row in rows}
    assert ("cb_order_batch", "fee_entry_id", "cb_fee_ledger") in foreign_keys
    assert ("cb_fee_ledger", "batch_id", "cb_order_batch") in foreign_keys
    assert not any(
        table == "cb_order_batch" and column == "desk_plan_id" for table, column, _ in foreign_keys
    )


async def test_versions_and_constituents_are_immutable_after_write(
    schema_database: AsyncConnection,
) -> None:
    _alembic("upgrade", "head")
    instrument_id = await schema_database.scalar(
        text(
            "WITH exchange_row AS ("
            "INSERT INTO exchange (id, code) VALUES (90, 'SCHEMA_TEST') RETURNING id"
            ") "
            "INSERT INTO instrument "
            "(exchange_id, symbol, name, instrument_type, is_active, created_at, updated_at) "
            "SELECT id, 'SCHEMA', 'Schema Test', 'EQ', true, now(), now() FROM exchange_row "
            "RETURNING instrument.id"
        )
    )
    manager_id = await schema_database.scalar(
        text(
            "INSERT INTO cb_manager "
            "(slug, name, kind, bio, strategies, disclosures_md) "
            "VALUES ('schema-manager', 'Schema Manager', 'ENGINE', '', ARRAY[]::varchar[], '') "
            "RETURNING id"
        )
    )
    basket_id = await schema_database.scalar(
        text(
            "INSERT INTO cb_basket "
            "(slug, name, manager_id, type, access, visibility, categories, description_md, "
            "rationale_md, rebalance_frequency, launched_at, source) "
            "VALUES ('schema-basket', 'Schema Basket', :manager_id, 'STOCK', 'FREE', "
            "'PRIVATE', ARRAY[]::varchar[], '', '', 'NEED_BASIS', DATE '2026-08-23', 'MANUAL') "
            "RETURNING id"
        ),
        {"manager_id": manager_id},
    )
    version_id = await schema_database.scalar(
        text(
            "INSERT INTO cb_basket_version "
            "(basket_id, version_no, effective_date, label, added_count, removed_count) "
            "VALUES (:basket_id, 1, DATE '2026-08-23', 'GENESIS', 1, 0) RETURNING id"
        ),
        {"basket_id": basket_id},
    )
    constituent_id = await schema_database.scalar(
        text(
            "INSERT INTO cb_constituent (version_id, instrument_id, segment, weight) "
            "VALUES (:version_id, :instrument_id, 'Largecap', 1.0000) RETURNING id"
        ),
        {"version_id": version_id, "instrument_id": instrument_id},
    )
    await schema_database.execute(
        text(
            "DO $body$ BEGIN "
            "BEGIN UPDATE cb_basket_version SET notes_md = 'rewrite' "
            "WHERE id = (SELECT id FROM cb_basket_version WHERE basket_id = "
            "(SELECT id FROM cb_basket WHERE slug = 'schema-basket')); "
            "RAISE EXCEPTION 'version update was accepted'; "
            "EXCEPTION WHEN SQLSTATE '55000' THEN NULL; END; "
            "BEGIN DELETE FROM cb_basket_version WHERE basket_id = "
            "(SELECT id FROM cb_basket WHERE slug = 'schema-basket'); "
            "RAISE EXCEPTION 'version delete was accepted'; "
            "EXCEPTION WHEN SQLSTATE '55000' THEN NULL; END; "
            "BEGIN UPDATE cb_constituent SET weight = 0.5000 WHERE id = "
            "(SELECT min(id) FROM cb_constituent); "
            "RAISE EXCEPTION 'constituent update was accepted'; "
            "EXCEPTION WHEN SQLSTATE '55000' THEN NULL; END; "
            "BEGIN DELETE FROM cb_constituent WHERE id = (SELECT min(id) FROM cb_constituent); "
            "RAISE EXCEPTION 'constituent delete was accepted'; "
            "EXCEPTION WHEN SQLSTATE '55000' THEN NULL; END; "
            "END $body$"
        ),
    )
    assert (
        await schema_database.scalar(
            text("SELECT count(*) FROM cb_basket_version WHERE id = :id"), {"id": version_id}
        )
        == 1
    )
    assert (
        await schema_database.scalar(
            text("SELECT count(*) FROM cb_constituent WHERE id = :id"), {"id": constituent_id}
        )
        == 1
    )
