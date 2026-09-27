"""OP2's acceptance for the ``op_`` schema (``docs/options/03``, ``06`` OP2).

1. **Every user-scoped ``op_`` table carries ``user_id``**, non-null, cascading from ``app_user``;
   the four market-data tables (``op_contract``, ``op_expiry``, ``op_index_minute``,
   ``op_chain_snapshot``) carry none (``03``'s opening paragraph).
2. **The migration creates and drops every table**, revises the head OP0 recorded, and its
   downgrade is exercised against the test database (round trip).
3. **The models and the migration agree on a real database** — columns, nullability, CHECKs.
4. **migrate -> seed -> migrate is a no-op**: the second migrate runs nothing, a second seed writes
   nothing, and neither resets a number a person chose (house rule 7).
5. The constraints that are rules refuse: NIFTY only, MIS/NFO only, capital never negative, a plan
   cannot expire before it is issued, the partition exists for the month and not outside the
   partitioned range.

Database tests run inside rolled-back transactions against ``BASKFY_TEST_DATABASE_URL`` (the
round trip is the one exception: it migrates the test database down one revision and back up).
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_core.models import AppUser, Base, OpBookConfig, OpSleeveConfig
from baskfy_core.models.options import OP_SLEEVES
from baskfy_core.options.config import DEFAULT_OPTIONS_CONFIG, Sleeve
from baskfy_worker.seeds.options_event_days import EVENT_DAYS, seed_options

ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"
REPO_ROOT: Final = Path(__file__).resolve().parents[3]
MONOREPO_ROOT: Final = Path(__file__).resolve().parents[4]
API_DIR: Final = REPO_ROOT / "services" / "api"
MIGRATION_PATH: Final = API_DIR / "alembic" / "versions" / "0050_options.py"
#: The options schema's newest revision. `0050_options` built it; OP11's `0051` added
#: `op_position.trough_value` on top (DECISIONS-OP OP11.2), so the round trip below also crosses
#: 0051's downgrade. A later options migration moves this, and only this. FO2's `0052_fno` is one:
#: it widens `op_contract.lot_size` / `op_expiry.lot_size` to integer for the all-underlyings
#: master, so the head these tests migrate to and assert is now 0052 (DECISIONS-FO FO2).
OPTIONS_HEAD: Final = "0052_fno"
MIGRATION: Final = MIGRATION_PATH.read_text(encoding="utf-8")
DATA_MODEL: Final = (MONOREPO_ROOT / "docs" / "options" / "03-data-model.md").read_text(
    encoding="utf-8"
)

MARKET_TABLES: Final = frozenset(
    {"op_contract", "op_expiry", "op_index_minute", "op_chain_snapshot"}
)
OP_TABLES: Final[list[str]] = sorted(t for t in Base.metadata.tables if t.startswith("op_"))
USER_TABLES: Final[list[str]] = [t for t in OP_TABLES if t not in MARKET_TABLES]
#: ``03`` §1-§12 plus ``op_config_audit`` (``03`` §7's "settings_audit on every write").
EXPECTED_TABLE_COUNT: Final = 17


def _database_url() -> str | None:
    from_env = os.environ.get(ENV_VAR)
    if from_env:
        return from_env
    for candidate in (REPO_ROOT / ".env", MONOREPO_ROOT / ".env"):
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip() == ENV_VAR and value.strip():
                    return value.strip()
    return None


requires_db = pytest.mark.db(
    pytest.mark.skipif(_database_url() is None, reason=f"{ENV_VAR} is not set; run `make up`")
)


# --- structural -------------------------------------------------------------------------------


def test_there_are_seventeen_op_tables() -> None:
    assert len(OP_TABLES) == EXPECTED_TABLE_COUNT, OP_TABLES
    assert set(OP_TABLES) >= MARKET_TABLES


@pytest.mark.parametrize("table_name", USER_TABLES)
def test_every_user_scoped_table_has_a_cascading_non_null_user_id(table_name: str) -> None:
    table = Base.metadata.tables[table_name]
    assert "user_id" in table.c, f"{table_name} has no user_id (P4.1, 02 Track C §11)"
    column = table.c["user_id"]
    assert not column.nullable
    fks = [fk for fk in column.foreign_keys if fk.target_fullname == "app_user.id"]
    assert len(fks) == 1 and fks[0].ondelete == "CASCADE"


@pytest.mark.parametrize("table_name", sorted(MARKET_TABLES))
def test_market_data_tables_are_shared_facts(table_name: str) -> None:
    assert "user_id" not in Base.metadata.tables[table_name].c


@pytest.mark.parametrize("table_name", OP_TABLES)
def test_every_table_is_named_in_the_data_model_and_the_migration(table_name: str) -> None:
    assert f"`{table_name}`" in DATA_MODEL
    assert f'"{table_name}"' in MIGRATION
    assert f'    "{table_name}",\n' in MIGRATION, f"{table_name} missing from TABLES (downgrade)"


def test_the_migration_revises_the_head_op0_recorded() -> None:
    """OP0 recorded ``0049_broker_trade`` as the single head; OP2 takes the next number."""
    assert 'revision: str = "0050_options"' in MIGRATION
    assert 'down_revision: str | None = "0049_broker_trade"' in MIGRATION


def test_the_sleeve_enum_is_the_documents() -> None:
    assert OP_SLEEVES == ("O1M", "O1W", "O2", "O3A", "O3B")
    assert "`O1M`, `O1W`, `O2`, `O3A`, `O3B`" in DATA_MODEL


def test_the_downgrade_drops_every_table_and_the_enum() -> None:
    assert "def downgrade() -> None:" in MIGRATION
    assert "DROP TABLE IF EXISTS {table} CASCADE" in MIGRATION
    assert "sleeve_enum.drop(" in MIGRATION


# --- database ---------------------------------------------------------------------------------


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=API_DIR,
        env={
            **{k: v for k, v in os.environ.items() if k != "BASKFY_DATABASE_URL"},
            "BASKFY_DATABASE_URL": url,
        },
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture(scope="module")
def op_url() -> str:
    url = _database_url()
    if url is None:  # pragma: no cover - guarded by requires_db
        pytest.skip(f"{ENV_VAR} is not set")
    _alembic(url, "upgrade", "head")
    return url


@asynccontextmanager
async def _rolled_back(url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(url)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


async def _fresh_user(session: AsyncSession, tag: str) -> int:
    user = AppUser(public_id=f"op2-{tag}-{uuid.uuid4().hex[:8]}", email=f"op2-{tag}@example.test")
    session.add(user)
    await session.flush()
    return int(user.id)


@requires_db
class TestTheSchemaOnARealDatabase:
    async def test_every_modelled_column_exists_with_the_modelled_nullability(
        self, op_url: str
    ) -> None:
        async with _rolled_back(op_url) as session:
            rows = (
                await session.execute(
                    sa.text(
                        "SELECT table_name, column_name, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = ANY(:names)"
                    ),
                    {"names": OP_TABLES},
                )
            ).all()
        actual = {(t, c): n == "YES" for t, c, n in rows}
        for name in OP_TABLES:
            for column in Base.metadata.tables[name].c:
                key = (name, column.name)
                assert key in actual, f"{name}.{column.name} is modelled but not migrated"
                assert actual[key] == column.nullable, f"{name}.{column.name} nullability"
        modelled = {(t, c.name) for t in OP_TABLES for c in Base.metadata.tables[t].c}
        assert set(actual) == modelled, "a migrated column is not modelled"

    async def test_every_modelled_check_constraint_exists(self, op_url: str) -> None:
        async with _rolled_back(op_url) as session:
            present = set(
                (
                    await session.execute(
                        sa.text(
                            "SELECT conname FROM pg_constraint c JOIN pg_class t ON t.oid = "
                            "c.conrelid WHERE c.contype = 'c' AND t.relname = ANY(:names)"
                        ),
                        {"names": OP_TABLES},
                    )
                ).scalars()
            )
        for name in OP_TABLES:
            for constraint in Base.metadata.tables[name].constraints:
                if isinstance(constraint, sa.CheckConstraint):
                    assert str(constraint.name) in present, f"{constraint.name} not migrated"

    async def test_the_chain_snapshot_is_partitioned_by_month(self, op_url: str) -> None:
        async with _rolled_back(op_url) as session:
            strategy = (
                await session.execute(
                    sa.text(
                        "SELECT partstrat::text FROM pg_partitioned_table p JOIN pg_class c "
                        "ON c.oid = p.partrelid WHERE c.relname = 'op_chain_snapshot'"
                    )
                )
            ).scalar_one()
            parts = set(
                (
                    await session.execute(
                        sa.text(
                            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = "
                            "i.inhrelid JOIN pg_class p ON p.oid = i.inhparent "
                            "WHERE p.relname = 'op_chain_snapshot'"
                        )
                    )
                ).scalars()
            )
        assert strategy == "r"
        assert {"op_chain_snapshot_202609", "op_chain_snapshot_202712"} <= parts
        assert len(parts) == 16  # Sep 2026 .. Dec 2027

    async def test_a_snapshot_outside_every_partition_is_refused_loudly(self, op_url: str) -> None:
        """No DEFAULT partition (OP2.5): a missing month fails rather than hiding rows."""
        async with _rolled_back(op_url) as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    sa.text(
                        "INSERT INTO op_chain_snapshot (ts, instrument_token, expiry, strike, "
                        "option_type) VALUES ('2030-01-02 10:00+05:30', 1, '2030-01-07', 24000, "
                        "'CE')"
                    )
                )

    async def test_a_snapshot_minute_is_idempotent_on_its_key(self, op_url: str) -> None:
        async with _rolled_back(op_url) as session:
            stmt = sa.text(
                "INSERT INTO op_chain_snapshot (ts, instrument_token, expiry, strike, option_type) "
                "VALUES ('2026-09-22 10:00+05:30', 42, '2026-09-29', 24000, 'CE') "
                "ON CONFLICT (ts, instrument_token) DO NOTHING"
            )
            await session.execute(stmt)
            await session.execute(stmt)
            count = (
                await session.execute(
                    sa.text("SELECT count(*) FROM op_chain_snapshot WHERE instrument_token = 42")
                )
            ).scalar_one()
        assert count == 1

    @pytest.mark.parametrize("underlying", ["BANKNIFTY", "FINNIFTY", "SENSEX"])
    async def test_the_database_refuses_any_underlying_but_nifty(
        self, op_url: str, underlying: str
    ) -> None:
        async with _rolled_back(op_url) as session:
            user_id = await _fresh_user(session, "under")
            session.add(OpBookConfig(user_id=user_id, underlying=underlying))
            with pytest.raises(IntegrityError):
                await session.flush()

    async def test_an_order_row_is_mis_on_nfo_only(self, op_url: str) -> None:
        async with _rolled_back(op_url) as session:
            names = set(
                (
                    await session.execute(
                        sa.text(
                            "SELECT conname FROM pg_constraint WHERE conrelid = "
                            "'op_order'::regclass AND contype = 'c'"
                        )
                    )
                ).scalars()
            )
        assert {"ck_op_order_product_mis_only", "ck_op_order_exchange_nfo_only"} <= names

    async def test_a_negative_capital_is_refused(self, op_url: str) -> None:
        async with _rolled_back(op_url) as session:
            user_id = await _fresh_user(session, "neg")
            session.add(
                OpSleeveConfig(user_id=user_id, sleeve="O2", sleeve_capital_inr=Decimal("-1"))
            )
            with pytest.raises(IntegrityError):
                await session.flush()

    async def test_a_config_row_for_o3a_is_refused_it_is_the_o3_group(self, op_url: str) -> None:
        """Config is per sleeve *group* (``03``): O3-A and O3-B share one row."""
        async with _rolled_back(op_url) as session:
            user_id = await _fresh_user(session, "o3a")
            session.add(OpSleeveConfig(user_id=user_id, sleeve="O3A"))
            with pytest.raises(IntegrityError):
                await session.flush()

    async def test_deleting_the_user_takes_their_options_rows_with_them(self, op_url: str) -> None:
        async with _rolled_back(op_url) as session:
            user_id = await _fresh_user(session, "cascade")
            await seed_options(session, user_id)
            await session.execute(sa.delete(AppUser).where(AppUser.id == user_id))
            left = (
                await session.execute(
                    sa.text(
                        "SELECT (SELECT count(*) FROM op_event_day WHERE user_id = :u) + "
                        "(SELECT count(*) FROM op_sleeve_config WHERE user_id = :u) + "
                        "(SELECT count(*) FROM op_book_config WHERE user_id = :u)"
                    ),
                    {"u": user_id},
                )
            ).scalar_one()
        assert left == 0


@requires_db
class TestMigrateSeedMigrate:
    async def test_migrate_then_seed_then_migrate_is_a_no_op(self, op_url: str) -> None:
        heads = _alembic(op_url, "heads").stdout.strip().splitlines()
        assert len(heads) == 1, "one head"
        assert OPTIONS_HEAD in _alembic(op_url, "history").stdout, "in the head's history"
        async with _rolled_back(op_url) as session:
            user_id = await _fresh_user(session, "mseed")
            first = await seed_options(session, user_id)
            assert first == {
                "op_event_day": len(EVENT_DAYS),
                "op_book_config": 1,
                "op_sleeve_config": 4,
            }
            # A person changes a number; a second seed must not reset it.
            row = await session.get(OpSleeveConfig, (user_id, "O2"))
            assert row is not None
            row.max_lots = 1
            await session.flush()
            second = await seed_options(session, user_id)
            assert second == {"op_event_day": 0, "op_book_config": 0, "op_sleeve_config": 0}
            session.expire_all()
            row = await session.get(OpSleeveConfig, (user_id, "O2"))
            assert row is not None and row.max_lots == 1
        again = _alembic(op_url, "upgrade", "head")
        assert "Running upgrade" not in again.stderr + again.stdout
        assert (
            _alembic(op_url, "current").stdout.split()[0]
            == _alembic(op_url, "heads").stdout.split()[0]
        )

    async def test_the_seed_writes_the_documents_defaults_and_zero_capital(
        self, op_url: str
    ) -> None:
        async with _rolled_back(op_url) as session:
            user_id = await _fresh_user(session, "defaults")
            await seed_options(session, user_id)
            rows = {
                r.sleeve: r
                for r in (
                    await session.execute(
                        sa.select(OpSleeveConfig).where(OpSleeveConfig.user_id == user_id)
                    )
                ).scalars()
            }
            book = await session.get(OpBookConfig, user_id)
            assert set(rows) == {"O1M", "O1W", "O2", "O3"}
            # 03 §7, read against the pure config so the two cannot drift.
            expected = {"O1M": Sleeve.O1M, "O1W": Sleeve.O1W, "O2": Sleeve.O2, "O3": Sleeve.O3A}
            for group, sleeve in expected.items():
                row = rows[group]
                assert row.sleeve_capital_inr == 0, "PACK.6: no agent writes a capital"
                assert row.risk_per_trade_pct == DEFAULT_OPTIONS_CONFIG.risk_per_trade_pct(sleeve)
                assert row.max_lots == DEFAULT_OPTIONS_CONFIG.max_lots(sleeve)
                assert row.hard_exit_time == DEFAULT_OPTIONS_CONFIG.hard_exit_time(sleeve)
                assert row.hard_exit_time <= dt.time(15, 0)
                assert row.paper_enabled is True
            assert (rows["O1M"].risk_per_trade_pct, rows["O1M"].max_lots) == (Decimal("1.00"), 3)
            assert (rows["O2"].hard_exit_time, rows["O3"].hard_exit_time) == (
                dt.time(15, 0),
                dt.time(14, 45),
            )
            assert book is not None and book.underlying == "NIFTY" and book.account_inr == 0


@requires_db
class TestTheRoundTrip:
    async def test_downgrade_one_revision_then_upgrade_restores_everything(
        self, op_url: str
    ) -> None:
        _alembic(op_url, "downgrade", "0049_broker_trade")
        engine = create_async_engine(op_url)
        try:
            async with engine.connect() as conn:
                left = (
                    await conn.execute(
                        sa.text(
                            "SELECT count(*) FROM pg_class WHERE relname LIKE 'op\\_%' "
                            "AND relkind IN ('r', 'p')"
                        )
                    )
                ).scalar_one()
                enum = (
                    await conn.execute(
                        sa.text("SELECT count(*) FROM pg_type WHERE typname = 'op_sleeve'")
                    )
                ).scalar_one()
            assert (left, enum) == (0, 0), "the downgrade left op_ tables or the enum behind"
        finally:
            await engine.dispose()
            _alembic(op_url, "upgrade", "head")
        assert (
            _alembic(op_url, "current").stdout.split()[0]
            == _alembic(op_url, "heads").stdout.split()[0]
        )
