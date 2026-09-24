"""FO2's acceptance for the ``fo_`` schema (``docs/fno/03``, ``06`` FO2).

1. **Every user-scoped ``fo_`` table carries ``user_id``**, non-null, cascading from ``app_user``;
   the four market-data tables carry none (``03``'s opening paragraph, §8).
2. **The migration creates and drops every table**, revises the head FO0 recorded, and its
   downgrade is exercised against the test database (round trip), lot sizes included.
3. **The models and the migration agree on a real database** — columns, nullability, CHECKs.
4. **``fo_contract_daily`` is partitioned by month** from Jan 2022 (the backfill's start) to
   Dec 2027, refuses a row outside every partition, and is idempotent on its key.
5. **migrate -> seed -> migrate is a no-op**, the seed writes Maulik's F1 ₹10,00,000 (M.1) and F2
   ₹0 at 1.0 %, audits each row it inserts, and never resets a number a person chose.
6. The constraints that are rules refuse: a future is ``XX`` and only a future is, a sleeve group
   is ``F1`` or ``F2``, capital is never negative, a plan cannot expire before it is issued.

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

from baskfy_core.models import (
    FO_SLEEVE_GROUPS,
    FO_SLEEVES,
    AppUser,
    Base,
    FoBookConfig,
    FoConfigAudit,
    FoPlan,
    FoSleeveConfig,
)
from baskfy_worker.seeds.fno_config import seed_fno

ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"
REPO_ROOT: Final = Path(__file__).resolve().parents[3]
MONOREPO_ROOT: Final = Path(__file__).resolve().parents[4]
API_DIR: Final = REPO_ROOT / "services" / "api"
MIGRATION_PATH: Final = API_DIR / "alembic" / "versions" / "0052_fno.py"
FNO_HEAD: Final = "0052_fno"
PREVIOUS_HEAD: Final = "0051_op_position_extremes"
MIGRATION: Final = MIGRATION_PATH.read_text(encoding="utf-8")
DATA_MODEL: Final = (MONOREPO_ROOT / "docs" / "fno" / "03-data-model.md").read_text(
    encoding="utf-8"
)
METHOD: Final = (MONOREPO_ROOT / "docs" / "fno" / "01-method.md").read_text(encoding="utf-8")

MARKET_TABLES: Final = frozenset(
    {"fo_contract_daily", "fo_underlying_daily", "fo_ingest_day", "fo_spread_sample"}
)
FO_TABLES: Final[list[str]] = sorted(t for t in Base.metadata.tables if t.startswith("fo_"))
USER_TABLES: Final[list[str]] = [t for t in FO_TABLES if t not in MARKET_TABLES]
#: ``03`` §1-§7's thirteen plus §8's ``fo_ingest_day`` and ``fo_config_audit``.
EXPECTED_TABLE_COUNT: Final = 15
#: Jan 2022 .. Dec 2027 inclusive.
PARTITIONS: Final = 72


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


def test_there_are_fifteen_fo_tables() -> None:
    assert len(FO_TABLES) == EXPECTED_TABLE_COUNT, FO_TABLES
    assert set(FO_TABLES) >= MARKET_TABLES


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


@pytest.mark.parametrize("table_name", FO_TABLES)
def test_every_table_is_named_in_the_data_model_and_the_migration(table_name: str) -> None:
    assert f"`{table_name}`" in DATA_MODEL
    assert f'"{table_name}"' in MIGRATION
    assert f'    "{table_name}",\n' in MIGRATION, f"{table_name} missing from TABLES (downgrade)"


def test_the_migration_revises_the_head_fo0_recorded() -> None:
    """FO0 recorded ``0051_op_position_extremes`` as the single head; FO2 takes the next number."""
    assert f'revision: str = "{FNO_HEAD}"' in MIGRATION
    assert f'down_revision: str | None = "{PREVIOUS_HEAD}"' in MIGRATION


def test_the_sleeve_enum_is_the_methods() -> None:
    """``03``: "the codes in ``01``". ``01`` §4 names them."""
    assert FO_SLEEVES == ("F1N", "F1B", "F2")
    assert "`F1N` (NIFTY), `F1B` (BANKNIFTY) and `F2`" in METHOD
    assert FO_SLEEVE_GROUPS == ("F1", "F2")
    assert "Flags are grouped\nas `F1` and `F2`" in METHOD


def test_the_contract_key_is_the_documents() -> None:
    """``04`` §4: idempotent upsert on ``(trade_date, symbol, expiry, strike, option_type)``."""
    key = tuple(c.name for c in Base.metadata.tables["fo_contract_daily"].primary_key.columns)
    assert key == ("trade_date", "symbol", "expiry", "strike", "option_type")


def test_money_and_prices_are_numeric() -> None:
    """House rule 9 over every ``fo_`` column: no float anywhere."""
    for name in FO_TABLES:
        for column in Base.metadata.tables[name].c:
            assert getattr(column.type, "python_type", object) is not float, f"{name}.{column.name}"


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
def fo_url() -> str:
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
    user = AppUser(public_id=f"fo2-{tag}-{uuid.uuid4().hex[:8]}", email=f"fo2-{tag}@example.test")
    session.add(user)
    await session.flush()
    return int(user.id)


_CONTRACT = (
    "INSERT INTO fo_contract_daily (trade_date, instrument, symbol, expiry, strike, option_type, "
    "close, settle, source_key) VALUES (:d, :i, 'SBIN', '2026-09-29', :k, :t, 800, 800, 'k')"
)


@requires_db
class TestTheSchemaOnARealDatabase:
    async def test_every_modelled_column_exists_with_the_modelled_nullability(
        self, fo_url: str
    ) -> None:
        async with _rolled_back(fo_url) as session:
            rows = (
                await session.execute(
                    sa.text(
                        "SELECT table_name, column_name, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = ANY(:names)"
                    ),
                    {"names": FO_TABLES},
                )
            ).all()
        actual = {(t, c): n == "YES" for t, c, n in rows}
        for name in FO_TABLES:
            for column in Base.metadata.tables[name].c:
                key = (name, column.name)
                assert key in actual, f"{name}.{column.name} is modelled but not migrated"
                assert actual[key] == column.nullable, f"{name}.{column.name} nullability"
        modelled = {(t, c.name) for t in FO_TABLES for c in Base.metadata.tables[t].c}
        assert set(actual) == modelled, "a migrated column is not modelled"

    async def test_every_modelled_check_constraint_exists(self, fo_url: str) -> None:
        async with _rolled_back(fo_url) as session:
            present = set(
                (
                    await session.execute(
                        sa.text(
                            "SELECT conname FROM pg_constraint c JOIN pg_class t ON t.oid = "
                            "c.conrelid WHERE c.contype = 'c' AND t.relname = ANY(:names)"
                        ),
                        {"names": FO_TABLES},
                    )
                ).scalars()
            )
        for name in FO_TABLES:
            for constraint in Base.metadata.tables[name].constraints:
                if isinstance(constraint, sa.CheckConstraint):
                    assert str(constraint.name) in present, f"{constraint.name} not migrated"

    async def test_the_master_lot_size_holds_a_stock_lot_above_a_smallint(
        self, fo_url: str
    ) -> None:
        """IDEA's lot (71,475) must fit once the master holds every underlying (FO2)."""
        async with _rolled_back(fo_url) as session:
            rows = (
                await session.execute(
                    sa.text(
                        "SELECT table_name, data_type FROM information_schema.columns "
                        "WHERE table_name IN ('op_contract', 'op_expiry') "
                        "AND column_name = 'lot_size'"
                    )
                )
            ).all()
            types = {str(table): str(kind) for table, kind in rows}
        assert types == {"op_contract": "integer", "op_expiry": "integer"}

    async def test_the_bhavcopy_is_partitioned_by_month_from_2022(self, fo_url: str) -> None:
        async with _rolled_back(fo_url) as session:
            strategy = (
                await session.execute(
                    sa.text(
                        "SELECT partstrat::text FROM pg_partitioned_table p JOIN pg_class c "
                        "ON c.oid = p.partrelid WHERE c.relname = 'fo_contract_daily'"
                    )
                )
            ).scalar_one()
            parts = set(
                (
                    await session.execute(
                        sa.text(
                            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = "
                            "i.inhrelid JOIN pg_class p ON p.oid = i.inhparent "
                            "WHERE p.relname = 'fo_contract_daily'"
                        )
                    )
                ).scalars()
            )
        assert strategy == "r"
        assert {"fo_contract_daily_202201", "fo_contract_daily_202712"} <= parts
        assert len({p for p in parts if p <= "fo_contract_daily_202712"}) == PARTITIONS

    async def test_a_row_outside_every_partition_is_refused_loudly(self, fo_url: str) -> None:
        async with _rolled_back(fo_url) as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    sa.text(_CONTRACT),
                    {"d": dt.date(2019, 1, 2), "i": "FUTSTK", "k": 0, "t": "XX"},
                )

    async def test_a_contract_day_is_idempotent_on_its_key(self, fo_url: str) -> None:
        async with _rolled_back(fo_url) as session:
            stmt = sa.text(_CONTRACT + " ON CONFLICT DO NOTHING")
            params = {"d": dt.date(2026, 9, 24), "i": "FUTSTK", "k": 0, "t": "XX"}
            await session.execute(stmt, params)
            await session.execute(stmt, params)
            count = (
                await session.execute(
                    sa.text(
                        "SELECT count(*) FROM fo_contract_daily WHERE trade_date = '2026-09-24' "
                        "AND symbol = 'SBIN'"
                    )
                )
            ).scalar_one()
        assert count == 1

    @pytest.mark.parametrize(
        ("instrument", "option_type"),
        [("FUTSTK", "CE"), ("OPTSTK", "XX"), ("FUTIDX", "PE"), ("OPTIDX", "XX")],
    )
    async def test_a_future_is_xx_and_only_a_future_is(
        self, fo_url: str, instrument: str, option_type: str
    ) -> None:
        async with _rolled_back(fo_url) as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    sa.text(_CONTRACT),
                    {"d": dt.date(2026, 9, 24), "i": instrument, "k": 800, "t": option_type},
                )

    @pytest.mark.parametrize("sleeve", ["F1N", "F1B", "F3", "O1M"])
    async def test_config_is_per_sleeve_group_only(self, fo_url: str, sleeve: str) -> None:
        """F1's capital is one number for both underlyings (M.1), so ``F1N`` is not a row."""
        async with _rolled_back(fo_url) as session:
            user_id = await _fresh_user(session, "group")
            session.add(FoSleeveConfig(user_id=user_id, sleeve=sleeve))
            with pytest.raises(IntegrityError):
                await session.flush()

    async def test_a_negative_capital_is_refused(self, fo_url: str) -> None:
        async with _rolled_back(fo_url) as session:
            user_id = await _fresh_user(session, "neg")
            session.add(FoSleeveConfig(user_id=user_id, sleeve="F2", capital_inr=Decimal("-1")))
            with pytest.raises(IntegrityError):
                await session.flush()

    async def test_a_plan_cannot_expire_before_it_is_issued(self, fo_url: str) -> None:
        async with _rolled_back(fo_url) as session:
            user_id = await _fresh_user(session, "plan")
            issued = dt.datetime(2026, 9, 25, 9, 20, tzinfo=dt.UTC)
            session.add(
                FoPlan(
                    user_id=user_id,
                    plan_id=f"fo2-{uuid.uuid4().hex[:8]}",
                    sleeve="F1N",
                    symbol="NIFTY",
                    trade_date=dt.date(2026, 9, 25),
                    structure="IRON_CONDOR",
                    kind="ENTRY",
                    sizing_mode="BUDGET",
                    issued_at=issued,
                    expires_at=issued,
                    lots=1,
                    lot_size=65,
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()

    async def test_deleting_the_user_takes_their_fo_rows_with_them(self, fo_url: str) -> None:
        async with _rolled_back(fo_url) as session:
            user_id = await _fresh_user(session, "cascade")
            await seed_fno(session, user_id)
            await session.execute(sa.delete(AppUser).where(AppUser.id == user_id))
            left = (
                await session.execute(
                    sa.text(
                        "SELECT (SELECT count(*) FROM fo_book_config WHERE user_id = :u) + "
                        "(SELECT count(*) FROM fo_sleeve_config WHERE user_id = :u) + "
                        "(SELECT count(*) FROM fo_config_audit WHERE user_id = :u)"
                    ),
                    {"u": user_id},
                )
            ).scalar_one()
        assert left == 0


@requires_db
class TestMigrateSeedMigrate:
    async def test_migrate_then_seed_then_migrate_is_a_no_op(self, fo_url: str) -> None:
        head = _alembic(fo_url, "heads").stdout.split()[0]
        assert head == FNO_HEAD, "one head, and it is the FO schema"
        async with _rolled_back(fo_url) as session:
            user_id = await _fresh_user(session, "mseed")
            first = await seed_fno(session, user_id)
            assert first == {"fo_book_config": 1, "fo_sleeve_config": 2, "fo_config_audit": 2}
            # A person changes a number; a second seed must not reset it, nor audit anything.
            row = await session.get(FoSleeveConfig, (user_id, "F1"))
            assert row is not None
            row.capital_inr = Decimal("500000.00")
            await session.flush()
            second = await seed_fno(session, user_id)
            assert second == {"fo_book_config": 0, "fo_sleeve_config": 0, "fo_config_audit": 0}
            session.expire_all()
            row = await session.get(FoSleeveConfig, (user_id, "F1"))
            assert row is not None and row.capital_inr == Decimal("500000.00")
        again = _alembic(fo_url, "upgrade", "head")
        assert "Running upgrade" not in again.stderr + again.stdout
        assert _alembic(fo_url, "current").stdout.split()[0] == FNO_HEAD

    async def test_the_seed_writes_maulik_s_capital_and_the_documents_defaults(
        self, fo_url: str
    ) -> None:
        async with _rolled_back(fo_url) as session:
            user_id = await _fresh_user(session, "defaults")
            await seed_fno(session, user_id)
            rows = {
                r.sleeve: r
                for r in (
                    await session.execute(
                        sa.select(FoSleeveConfig).where(FoSleeveConfig.user_id == user_id)
                    )
                ).scalars()
            }
            book = await session.get(FoBookConfig, user_id)
            audits = {
                a.scope: a
                for a in (
                    await session.execute(
                        sa.select(FoConfigAudit).where(FoConfigAudit.user_id == user_id)
                    )
                ).scalars()
            }
            assert set(rows) == {"F1", "F2"}
            # 01 §1 / 04 §3 / DECISIONS-FO M.1: F1 Rs 10,00,000 for both underlyings; F2 Rs 0.
            assert rows["F1"].capital_inr == Decimal("1000000.00")
            assert rows["F2"].capital_inr == Decimal("0.00")
            for row in rows.values():
                assert row.risk_per_trade_pct == Decimal("1.00")  # 04 §3
                assert row.max_lots == 2  # 04 §3 fo_max_lots
                assert row.paper_enabled is True
            # One per underlying x NIFTY and BANKNIFTY (04 §1); f2_max_open (04 §10).
            assert (rows["F1"].max_open_positions, rows["F2"].max_open_positions) == (2, 5)
            # 1.0 % of Rs 10 lakh is Rs 10,000 per structure, under the Rs 25,000 ceiling (04 §3).
            assert rows["F1"].capital_inr * rows["F1"].risk_per_trade_pct / 100 == Decimal("10000")
            assert book is not None and book.monthly_pause_inr == 0
            assert set(audits) == {"F1", "F2"}
            assert audits["F1"].new_value == "1000000.00" and audits["F1"].old_value is None
            assert "M.1" in (audits["F1"].note or "")


@requires_db
class TestTheRoundTrip:
    async def test_downgrade_one_revision_then_upgrade_restores_everything(
        self, fo_url: str
    ) -> None:
        _alembic(fo_url, "downgrade", PREVIOUS_HEAD)
        engine = create_async_engine(fo_url)
        try:
            async with engine.connect() as conn:
                left = (
                    await conn.execute(
                        sa.text(
                            "SELECT count(*) FROM pg_class WHERE relname LIKE 'fo\\_%' "
                            "AND relkind IN ('r', 'p')"
                        )
                    )
                ).scalar_one()
                enum = (
                    await conn.execute(
                        sa.text("SELECT count(*) FROM pg_type WHERE typname = 'fo_sleeve'")
                    )
                ).scalar_one()
                lot = (
                    await conn.execute(
                        sa.text(
                            "SELECT data_type FROM information_schema.columns WHERE "
                            "table_name = 'op_contract' AND column_name = 'lot_size'"
                        )
                    )
                ).scalar_one()
            assert (left, enum) == (0, 0), "the downgrade left fo_ tables or the enum behind"
            assert lot == "smallint", "the downgrade restores 0051's lot-size type"
        finally:
            await engine.dispose()
            _alembic(fo_url, "upgrade", "head")
        assert _alembic(fo_url, "current").stdout.split()[0] == FNO_HEAD
