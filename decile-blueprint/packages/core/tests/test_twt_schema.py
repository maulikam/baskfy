"""TW3's acceptance: the ``tw_`` schema is real, tenant-keyed, constrained and idempotent.

Five claims, each one a thing that would otherwise be discovered in production:

1. **Every ``tw_`` table carries ``user_id``, non-null, cascading.** ``docs/twt/02`` Track C §6.
   A table that forgets it needs a migration on the day a second account exists — which is the
   day nobody wants to be writing one.
2. **The models and the migration agree, on a real database.** They are written separately and
   they can drift; the only way to know they have not is to migrate and look.
3. **The constraints that are rules actually refuse.** A stop that falls, a close reason the
   strategy cannot produce, a trigger with no session, a thin session with an open gate, an
   order for a signal nobody stored. Each is asserted by making the database say no, because a
   ``CHECK`` nobody has ever triggered is a sentence in a file rather than a guarantee.
4. **The seed ships ``sleeve_capital_inr = 0`` and is idempotent.** House rule 7, and the safety
   rail of ``docs/twt/02`` §3: a sleeve at ₹0 plans nothing, and re-running the seeder must
   never reset a capital, a stop or a trail a person has chosen.
5. **The vocabulary is the document's.** Every state, kind and reason the check constraints
   admit is transcribed from ``docs/twt/03`` and ``04`` and asserted against them here, because
   ``baskfy_core.twt`` (TW1) does not exist yet to import them from. DECISIONS-TW **TW3.1**.

The structural half runs everywhere; the half that needs a database is marked ``db``.

This module carries its own database bootstrap rather than importing ``screener_helpers``,
which lives in ``services/api/tests`` and is only importable once pytest has collected from
that directory. ``uv run pytest packages/core/tests/test_twt_schema.py`` has to work on its own.
**And it deliberately never commits**: every database test runs inside a transaction that is
rolled back, so this file adds no row to any database and leaves the schema exactly as it found
it. The shared local Postgres is not this suite's to empty.
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
from typing import ClassVar, Final

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_api.problems import Problem, ProblemType
from baskfy_api.seed import seed_twt_config
from baskfy_api.twt_settings import (
    TwtCeilings,
    TwtConfigNotSeeded,
    TwtConfigPatch,
    apply_patch,
    audit_trail,
    read_config,
    record_system_change,
)
from baskfy_core.models import (
    TW_BACKTEST_SOURCES,
    TW_CLOSE_REASONS,
    TW_FAILED_FILTERS,
    TW_FILL_SIDES,
    TW_GATES,
    TW_LINE_KINDS,
    TW_LINE_STATES,
    TW_ORDER_SIDES,
    TW_ORDER_STATES,
    TW_PLAN_SOURCES,
    TW_PLAN_TTL_MINUTES,
    TW_POSITION_STATES,
    TW_SESSION_MODES,
    TW_SIGNAL_STATES,
    TW_SKIP_REASONS,
    AppUser,
    Base,
    Exchange,
    Instrument,
    TwConfig,
)

ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"
#: ``packages/core/tests`` -> ``packages/core`` -> ``packages`` -> the repository root.
REPO_ROOT: Final = Path(__file__).resolve().parents[3]
MONOREPO_ROOT: Final = Path(__file__).resolve().parents[4]
API_DIR: Final = REPO_ROOT / "services" / "api"


def _database_url() -> str | None:
    """``BASKFY_TEST_DATABASE_URL`` from the environment, or from the ``.env`` the stack writes.

    The ``.env`` fallback is deliberate and is not how ``services/api/tests`` does it. Those
    suites are run by ``make test-db`` from a shell that has exported the variable; **this file
    is run by a gate whose command is exactly**
    ``cd decile-blueprint && uv run pytest packages/core/tests/test_twt_schema.py -q``.
    A db-marked test that silently skips under the command that is supposed to prove it is worse
    than no test, because the skip reads as a pass. ``pydantic-settings`` reads the same two
    paths for the same reason (``baskfy_api.settings``), so this is the repository's own
    convention rather than a new one.
    """
    from_env = os.environ.get(ENV_VAR)
    if from_env:
        return from_env
    for candidate in (REPO_ROOT / ".env", MONOREPO_ROOT / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == ENV_VAR and value.strip():
                return value.strip()
    return None


#: Both marks: ``db`` so ``make test-db`` selects it, and the skip so a laptop with no stack
#: running says why rather than erroring.
requires_db = pytest.mark.db(
    pytest.mark.skipif(
        _database_url() is None,
        reason=f"{ENV_VAR} is set neither in the environment nor in .env; run `make up`",
    )
)

TWT_TABLES: Final[list[str]] = sorted(
    name for name in Base.metadata.tables if name.startswith("tw_")
)

#: The migration that creates them, read by path rather than by module name because alembic
#: version files are not a package. A glob, so a second ``*_twt.py`` is picked up automatically.
MIGRATIONS: Final[dict[str, str]] = {
    path.name: path.read_text(encoding="utf-8")
    for path in sorted((API_DIR / "alembic" / "versions").glob("00*_twt*.py"))
}
MIGRATION: Final = "\n".join(MIGRATIONS.values())

DATA_MODEL: Final = (MONOREPO_ROOT / "docs" / "twt" / "03-data-model.md").read_text(
    encoding="utf-8"
)
BUSINESS_RULES: Final = (MONOREPO_ROOT / "docs" / "twt" / "04-business-rules.md").read_text(
    encoding="utf-8"
)

#: `docs/twt/03`: thirteen tables from `0041_twt`, plus `tw_scan_run` from `0042_twt_scan_run`
#: (TW12's "Scan now" button — `03` §11, DECISIONS-TW TW12.1). Fourteen.
#:
#: The count is asserted rather than inferred because every test below is parametrized over the
#: list, and an empty or truncated list would make all of them vacuous. When it changes, it should
#: change **here**, in one place, next to the reason.
EXPECTED_TABLE_COUNT: Final = 14


# --- the structural half -----------------------------------------------------


class TestEveryTableIsTenantKeyed:
    def test_there_are_fourteen_twt_tables(self) -> None:
        """A guard on the guard: an empty list would make every test below vacuous."""
        assert len(TWT_TABLES) == EXPECTED_TABLE_COUNT, TWT_TABLES

    @pytest.mark.parametrize("table_name", TWT_TABLES)
    def test_table_has_a_non_null_user_id(self, table_name: str) -> None:
        table = Base.metadata.tables[table_name]
        assert "user_id" in table.c, (
            f"{table_name} has no user_id. docs/twt/02 Track C §6: every tw_ row carries one, so "
            f"that the day multi-tenancy arrives nothing needs a migration."
        )
        assert not table.c["user_id"].nullable, f"{table_name}.user_id is nullable"

    #: ``tw_order.user_id`` takes part in a second foreign key as well as its own: the composite
    #: ``(user_id, signal_date, instrument_id) -> tw_signal_daily`` of ``03`` §6, which is what
    #: makes an order answerable by naming the stored signal that produced it. Written out here
    #: so the strict assertion below stays strict everywhere else.
    EXTRA_USER_ID_TARGETS: ClassVar[dict[str, set[str]]] = {"tw_order": {"tw_signal_daily.user_id"}}

    @pytest.mark.parametrize("table_name", TWT_TABLES)
    def test_user_id_points_at_app_user_and_cascades(self, table_name: str) -> None:
        """A user deleted under DPDP takes their sleeve with them (Prompt 12 §5)."""
        column = Base.metadata.tables[table_name].c["user_id"]
        targets = {fk.target_fullname for fk in column.foreign_keys}
        expected = {"app_user.id"} | self.EXTRA_USER_ID_TARGETS.get(table_name, set())
        assert targets == expected, f"{table_name}.user_id -> {targets}"
        tenant_keys = [fk for fk in column.foreign_keys if fk.target_fullname == "app_user.id"]
        assert len(tenant_keys) == 1
        assert tenant_keys[0].ondelete == "CASCADE"


class TestTheMigration:
    def test_there_is_a_twt_migration_at_all(self) -> None:
        assert MIGRATIONS, "no *_twt.py under alembic/versions"

    def test_it_revises_the_head_the_document_names(self) -> None:
        """`docs/twt/03`: 0041_twt revising 0040_vbt_scan_run, the single head on 11 Sep 2026."""
        assert 'revision: str = "0041_twt"' in MIGRATION
        assert 'down_revision: str | None = "0040_vbt_scan_run"' in MIGRATION

    def test_it_has_a_downgrade_that_drops_something(self) -> None:
        """A downgrade that does not undo is worse than none: it is the path taken at 3am."""
        assert "def downgrade() -> None:" in MIGRATION
        assert "op.drop_table(table)" in MIGRATION
        assert 'op.drop_constraint(POSITION_ORDER_FK, "tw_position"' in MIGRATION

    @pytest.mark.parametrize("table_name", TWT_TABLES)
    def test_the_migration_creates_and_drops_every_table(self, table_name: str) -> None:
        """A table added to `upgrade` and forgotten in the downgrade leaks into the next
        developer's database, where it is then a table nobody can explain."""
        assert f'"{table_name}"' in MIGRATION, f"{table_name} is in the models, not the migration"
        assert f'    "{table_name}",\n' in MIGRATION, f"{table_name} is not in TABLES"


class TestTheVocabularyIsTheDocuments:
    """``baskfy_core.twt`` is TW1's and does not exist yet, so the tuples in
    ``baskfy_core.models.twt`` are transcribed from ``docs/twt/03`` and ``04`` — and pinned to
    them here. Two lists are a way to disagree; this is the test that keeps them from it.
    DECISIONS-TW **TW3.1**.
    """

    def test_the_signal_states_are_the_documents(self) -> None:
        assert TW_SIGNAL_STATES == ("SIGNAL", "SCAN_ONLY")
        assert "`SIGNAL`" in DATA_MODEL
        assert "`SCAN_ONLY`" in DATA_MODEL

    def test_the_only_filter_that_can_reject_an_entry_event_is_the_liquidity_floor(self) -> None:
        """`04` §3.5: an entry event below `min_turnover_inr` is stored, never dropped."""
        assert TW_FAILED_FILTERS == ("TURNOVER",)
        assert "`{TURNOVER}`" in DATA_MODEL

    def test_the_gate_has_two_values(self) -> None:
        """`04` §4.3: two values, not three — no amber, because there is no ladder to feed."""
        assert TW_GATES == ("OPEN", "SHUT")
        assert "There is no amber and no exposure ladder" in BUSINESS_RULES

    def test_there_is_no_ema_exit_and_no_time_exit(self) -> None:
        """`03` §5, stated in the document as an absence and asserted here as one.

        The 50-SMA exit is a *different strategy* with the same signal (`01` §5). A reason that
        exists is a reason somebody writes code for.
        """
        assert TW_CLOSE_REASONS == ("STOP_HIT", "STOP_GAP", "STOP_DAY0", "NO_BAR", "MANUAL")
        assert "EMA_EXIT" not in TW_CLOSE_REASONS
        assert "TIME_EXIT" not in TW_CLOSE_REASONS
        assert "There is no `EMA_EXIT` and no `TIME_EXIT`" in DATA_MODEL

    def test_sell_at_open_exists_in_the_schema_and_is_produced_by_nothing(self) -> None:
        """`03` §7: the kind is here so a MANUAL exit needs no migration, and TW10 asserts that
        no TWT rule ever emits one. The schema admitting it is the deliberate half."""
        assert TW_LINE_KINDS == ("BUY_AT_OPEN", "ARM_GTT", "RAISE_GTT_STOP", "SELL_AT_OPEN")
        assert "produced by nothing in TWT-1" in DATA_MODEL

    def test_the_skip_reasons_are_the_documents_twelve(self) -> None:
        """`03` §7 lists them; `04` §10.1 is where each is applied."""
        assert len(TW_SKIP_REASONS) == 12
        for reason in TW_SKIP_REASONS:
            assert f"`{reason}`" in DATA_MODEL, f"{reason} is not in docs/twt/03 §7"

    def test_the_order_states_have_no_expiry_because_there_is_no_working_order(self) -> None:
        """`03` §6: TWT-1 buys at the next open at market, so no order works for sessions."""
        assert TW_ORDER_STATES == (
            "PROPOSED",
            "CONFIRMED",
            "SENT",
            "FILLED",
            "PARTIAL",
            "CANCELLED",
            "REJECTED",
        )
        assert "EXPIRED" not in TW_ORDER_STATES

    def test_the_remaining_vocabularies_are_the_documents(self) -> None:
        assert TW_ORDER_SIDES == ("BUY", "SELL")
        assert TW_FILL_SIDES == ("BUY", "SELL")
        assert TW_POSITION_STATES == ("OPEN", "CLOSED")
        assert TW_PLAN_SOURCES == ("EVENING", "MORNING", "MANUAL")
        assert TW_SESSION_MODES == ("DRY_RUN", "LIVE")
        assert TW_BACKTEST_SOURCES == ("PLANT", "RESEARCH_EXPORT")
        assert TW_LINE_STATES == (
            "PROPOSED",
            "CONFIRMED",
            "SENT",
            "FILLED",
            "REJECTED",
            "EXPIRED",
            "SKIPPED",
        )

    def test_the_plan_lives_thirty_minutes(self) -> None:
        """`03` §7 and `04` §10.4 — the desk's own plan lifetime, restated for this surface."""
        assert TW_PLAN_TTL_MINUTES == 30
        assert "built_at\n+ 30 min" in DATA_MODEL

    @pytest.mark.parametrize(
        "values",
        [
            TW_SIGNAL_STATES,
            TW_GATES,
            TW_LINE_KINDS,
            TW_SKIP_REASONS,
            TW_CLOSE_REASONS,
            TW_ORDER_STATES,
            TW_LINE_STATES,
            TW_PLAN_SOURCES,
            TW_SESSION_MODES,
            TW_BACKTEST_SOURCES,
        ],
    )
    def test_the_migration_constrains_every_value_the_engine_can_write(
        self, values: tuple[str, ...]
    ) -> None:
        """A value the engine emits and the constraint rejects is an outage at 21:00."""
        for value in values:
            assert f"'{value}'" in MIGRATION, f"{value} is not in a CHECK constraint"


class TestMoneyAndPricesAreExact:
    """House rule 9, over this sleeve's columns rather than over a sample."""

    @pytest.mark.parametrize("table_name", TWT_TABLES)
    def test_no_column_is_approximate(self, table_name: str) -> None:
        offenders = [
            f"{table_name}.{column.name}"
            for column in Base.metadata.tables[table_name].columns
            if getattr(column.type, "python_type", None) is float
        ]
        assert offenders == []

    @pytest.mark.parametrize(
        ("table", "column", "precision", "scale"),
        [
            # `03`'s conventions: PRICE (18,2) for prices and levels, PRICE_RAW (18,4) where an
            # exchange print must survive adjustment, INR (12,2) for money, BREADTH (7,4),
            # ADJ_FACTOR (18,10). The sleeve's percentages are numeric(5,2) per §1.
            ("tw_config", "sleeve_capital_inr", 12, 2),
            ("tw_config", "max_position_pct", 5, 2),
            ("tw_config", "stop_pct", 5, 2),
            ("tw_config", "trail_pct", 5, 2),
            ("tw_state_daily", "close", 18, 2),
            ("tw_state_daily", "close_raw", 18, 4),
            ("tw_state_daily", "adj_factor", 18, 10),
            ("tw_state_daily", "week_range_pct", 10, 4),
            ("tw_state_daily", "month_low_ratio", 10, 4),
            ("tw_signal_daily", "entry_reference_close", 18, 2),
            ("tw_signal_daily", "stop_preview", 18, 2),
            ("tw_breadth_daily", "pct_above_dma", 7, 4),
            # The exchange trades in exchange prices, and both of these are exchange prices.
            ("tw_position", "entry_avg", 18, 4),
            ("tw_position", "high_since", 18, 4),
            ("tw_position", "entry_adj_factor", 18, 10),
            ("tw_position", "initial_stop", 18, 2),
            ("tw_position", "stop_price", 18, 2),
            ("tw_position", "next_trigger", 18, 2),
            ("tw_position", "pnl_inr", 12, 2),
            ("tw_plan", "sleeve_equity_inr", 12, 2),
            ("tw_plan_line", "value_inr", 12, 2),
            ("tw_fill", "price", 18, 4),
        ],
    )
    def test_the_precision_is_the_documents(
        self, table: str, column: str, precision: int, scale: int
    ) -> None:
        column_type = Base.metadata.tables[table].columns[column].type
        assert isinstance(column_type, sa.Numeric)
        assert (column_type.precision, column_type.scale) == (precision, scale)

    def test_the_weekly_range_keeps_four_decimals_so_the_band_is_readable(self) -> None:
        """`04` §3.1's band is 3.01. At two decimals a 3.0149 range would store as 3.01 and an
        out-of-band row would read as in-band."""
        column_type = Base.metadata.tables["tw_state_daily"].columns["week_range_pct"].type
        assert isinstance(column_type, sa.Numeric)
        assert column_type.scale == 4


# --- the database half -------------------------------------------------------


def _migrate(url: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
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
def twt_url() -> str:
    """A database migrated to head. **Nothing is dropped**: other suites share this server."""
    url = _database_url()
    if url is None:  # pragma: no cover - guarded by requires_db
        pytest.skip(f"{ENV_VAR} is not set")
    _migrate(url)
    return url


@asynccontextmanager
async def _rolled_back(url: str) -> AsyncIterator[AsyncSession]:
    """A session whose work is **never committed.**

    Every test below inserts rows; none of them should survive. Rolling back rather than
    cleaning up means a failed assertion leaves nothing behind either.
    """
    engine = create_async_engine(url)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


async def _fresh_user(session: AsyncSession, tag: str) -> int:
    user = AppUser(public_id=f"tw3-{tag}-{uuid.uuid4().hex[:8]}", email=f"tw3-{tag}@example.test")
    session.add(user)
    await session.flush()
    return int(user.id)


async def _instrument(session: AsyncSession, symbol: str) -> int:
    exchange_id = (
        await session.execute(sa.select(Exchange.id).order_by(Exchange.id).limit(1))
    ).scalar_one_or_none()
    if exchange_id is None:
        exchange = Exchange(id=1, code="TW3")
        session.add(exchange)
        await session.flush()
        exchange_id = exchange.id
    instrument = Instrument(
        exchange_id=exchange_id,
        symbol=f"{symbol}{uuid.uuid4().hex[:6].upper()}",
        name="TW3 fixture",
        instrument_type="EQ",
        is_active=True,
    )
    session.add(instrument)
    await session.flush()
    return int(instrument.id)


async def _insert(session: AsyncSession, table: str, **values: object) -> None:
    await session.execute(sa.insert(Base.metadata.tables[table]).values(**values))


@requires_db
class TestTheSchemaOnARealDatabase:
    async def test_every_modelled_column_exists_with_the_modelled_nullability(
        self, twt_url: str
    ) -> None:
        """The models and the migration are written separately; they can drift."""
        async with _rolled_back(twt_url) as session:
            rows = (
                await session.execute(
                    sa.text(
                        "SELECT table_name, column_name, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name LIKE 'tw\\_%'"
                    )
                )
            ).all()

        actual = {(t, c): (n == "YES") for t, c, n in rows}
        missing: list[str] = []
        mismatched: list[str] = []
        for name in TWT_TABLES:
            for column in Base.metadata.tables[name].columns:
                key = (name, column.name)
                if key not in actual:
                    missing.append(f"{name}.{column.name}")
                elif actual[key] != column.nullable:
                    mismatched.append(
                        f"{name}.{column.name}: model nullable={column.nullable}, "
                        f"database nullable={actual[key]}"
                    )
        assert missing == [], f"the migration did not create: {missing}"
        assert mismatched == [], mismatched

    async def test_every_modelled_check_constraint_exists_in_the_database(
        self, twt_url: str
    ) -> None:
        """A CHECK in the model and not in the migration is a rule nothing enforces."""
        async with _rolled_back(twt_url) as session:
            rows = (
                await session.execute(
                    sa.text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE connamespace = 'public'::regnamespace AND contype = 'c' "
                        "AND conrelid::regclass::text LIKE 'tw\\_%'"
                    )
                )
            ).scalars()
        present = set(rows)
        expected = {
            constraint.name
            for name in TWT_TABLES
            for constraint in Base.metadata.tables[name].constraints
            if isinstance(constraint, sa.CheckConstraint) and constraint.name is not None
        }
        assert expected - present == set(), f"modelled but not migrated: {expected - present}"

    async def test_every_modelled_index_exists_in_the_database(self, twt_url: str) -> None:
        async with _rolled_back(twt_url) as session:
            rows = (
                await session.execute(
                    sa.text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = 'public' AND tablename LIKE 'tw\\_%'"
                    )
                )
            ).scalars()
        present = set(rows)
        expected = {
            index.name
            for name in TWT_TABLES
            for index in Base.metadata.tables[name].indexes
            if index.name is not None
        }
        assert expected - present == set(), f"modelled but not migrated: {expected - present}"

    async def test_the_plans_rank_index_is_descending(self, twt_url: str) -> None:
        """`03` §3: the plan's one query is today's signals, best turnover first."""
        async with _rolled_back(twt_url) as session:
            definition = (
                await session.execute(
                    sa.text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
                    {"name": "ix_tw_signal_daily_rank"},
                )
            ).scalar_one()
        assert "rank_key DESC" in definition

    async def test_the_mutually_referential_foreign_key_actually_exists(self, twt_url: str) -> None:
        """``tw_position.order_id`` and ``tw_order.position_id`` point at each other, so one of
        the two is added after both tables exist.

        This is the test that would have caught the trap: SQLAlchemy's ``CREATE TABLE`` silently
        *omits* a constraint marked ``use_alter``, and Alembic emits no follow-up ``ALTER``. The
        foreign key would simply not be there, and nothing would say so.
        """
        async with _rolled_back(twt_url) as session:
            rows = (
                await session.execute(
                    sa.text(
                        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conrelid = 'tw_position'::regclass AND contype = 'f'"
                    )
                )
            ).all()
            definitions: dict[str, str] = {str(name): str(body) for name, body in rows}
        assert "fk_tw_position_order_id_tw_order" in definitions
        assert "REFERENCES tw_order(id)" in definitions["fk_tw_position_order_id_tw_order"]


@requires_db
class TestTheSeedIsZeroCapitalAndIdempotent:
    async def test_the_seed_writes_zero_capital_and_the_documents_defaults(
        self, twt_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**The safety rail of this module.** `04` §9.3: a sleeve at ₹0 plans nothing, so a
        freshly seeded database can propose no line at all until Maulik enters the capital
        himself. The other five defaults are `04`'s: ten slots, 12.5 %, a 20 % stop, a 20 %
        trail, ten first-live entries."""
        async with _rolled_back(twt_url) as session:
            user_id = await _fresh_user(session, "seeded")
            monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
            assert await seed_twt_config(session) == 1
            row = await read_config(session, user_id)
            assert row.sleeve_capital_inr == Decimal("0.00")
            assert row.max_open_positions == 10
            assert row.max_position_pct == Decimal("12.50")
            assert row.stop_pct == Decimal("20.00")
            assert row.trail_pct == Decimal("20.00")
            assert row.first_live_entries_left == 10
            assert row.dry_run_sessions == 0
            assert row.updated_by == "seed"

    async def test_seeding_twice_is_idempotent_and_never_resets_a_chosen_number(
        self, twt_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """House rule 7, over the whole chain rather than over one insert.

        The capital, the stop and the trail are deliberately changed between the two seeds: a
        seeder written with ``ON CONFLICT DO UPDATE`` would pass a naive "the row still exists"
        check and would silently reset a person's sleeve on the next deploy. On this sleeve
        resetting the *trail* would rearm every stop in the book at a different level.
        """
        async with _rolled_back(twt_url) as session:
            user_id = await _fresh_user(session, "idempotent")
            monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
            assert await seed_twt_config(session) == 1
            row = await read_config(session, user_id)
            row.sleeve_capital_inr = Decimal("2500000.00")
            row.stop_pct = Decimal("22.00")
            row.trail_pct = Decimal("25.00")
            await session.flush()

            assert await seed_twt_config(session) == 1
            session.expire_all()
            row = await read_config(session, user_id)
            assert row.sleeve_capital_inr == Decimal("2500000.00")
            assert row.stop_pct == Decimal("22.00")
            assert row.trail_pct == Decimal("25.00")
            count = (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(TwConfig)
                    .where(TwConfig.user_id == user_id)
                )
            ).scalar_one()
            assert count == 1

    async def test_a_missing_row_is_never_created_by_a_read(self, twt_url: str) -> None:
        """A request handler that seeds is a request handler that writes on a GET."""
        async with _rolled_back(twt_url) as session:
            with pytest.raises(TwtConfigNotSeeded):
                await read_config(session, 9_999_999)


@requires_db
class TestTheConstraintsThatAreRulesActuallyRefuse:
    """Each of these makes the database say no. A CHECK nobody has ever triggered is a sentence
    in a file rather than a guarantee."""

    @staticmethod
    async def _position_values(session: AsyncSession, tag: str) -> dict[str, object]:
        user_id = await _fresh_user(session, tag)
        instrument_id = await _instrument(session, "TW")
        return {
            "user_id": user_id,
            "instrument_id": instrument_id,
            "signal_date": dt.date(2026, 9, 10),
            "entry_date": dt.date(2026, 9, 11),
            "entry_avg": Decimal("100.0000"),
            "quantity_entered": 10,
            "initial_stop": Decimal("80.00"),
            "stop_price": Decimal("80.00"),
            "high_since": Decimal("100.0000"),
            "quantity_open": 10,
            "state": "OPEN",
        }

    async def test_a_stop_below_the_initial_stop_is_refused(self, twt_url: str) -> None:
        """**The rule whose violation is silent.** `04` §7.2: a stop never falls. The ratchet
        takes a maximum and the desk refuses a raise below the resting trigger; this is the
        third place, the one that catches the write which got past the other two."""
        async with _rolled_back(twt_url) as session:
            values = await self._position_values(session, "stop-falls")
            with pytest.raises(IntegrityError, match="stop_never_below_initial"):
                async with session.begin_nested():
                    await _insert(
                        session, "tw_position", **{**values, "stop_price": Decimal("79.99")}
                    )

    async def test_a_stop_that_only_rises_is_accepted(self, twt_url: str) -> None:
        """The other half of the claim: the constraint refuses the fall, not the raise."""
        async with _rolled_back(twt_url) as session:
            values = await self._position_values(session, "stop-rises")
            await _insert(session, "tw_position", **{**values, "stop_price": Decimal("91.00")})

    async def test_an_ema_exit_is_not_a_close_reason_this_book_can_record(
        self, twt_url: str
    ) -> None:
        """`01` §5: the 50-SMA exit is a different strategy with the same signal. The database
        cannot store the reason, so nobody can quietly start producing it."""
        async with _rolled_back(twt_url) as session:
            values = await self._position_values(session, "ema-exit")
            with pytest.raises(IntegrityError, match="close_reason_known"):
                async with session.begin_nested():
                    await _insert(
                        session,
                        "tw_position",
                        **{
                            **values,
                            "state": "CLOSED",
                            "quantity_open": 0,
                            "closed_on": dt.date(2026, 10, 1),
                            "close_reason": "EMA_EXIT",
                        },
                    )

    async def test_a_next_trigger_with_no_session_is_refused(self, twt_url: str) -> None:
        """`04` §11.3: a plan never reads a trigger computed for another session, and a trigger
        that cannot say which session it is for cannot be checked against that rule."""
        async with _rolled_back(twt_url) as session:
            values = await self._position_values(session, "orphan-trigger")
            with pytest.raises(IntegrityError, match="next_trigger_has_a_session"):
                async with session.begin_nested():
                    await _insert(
                        session, "tw_position", **{**values, "next_trigger": Decimal("88.00")}
                    )

    async def test_an_order_side_outside_buy_and_sell_is_refused(self, twt_url: str) -> None:
        """Track C §1: no shorting. `SHORT` is not a side this schema can hold."""
        async with _rolled_back(twt_url) as session:
            user_id = await _fresh_user(session, "side")
            instrument_id = await _instrument(session, "TW")
            signal = {
                "user_id": user_id,
                "date": dt.date(2026, 9, 10),
                "instrument_id": instrument_id,
                "state": "SIGNAL",
                "entry_reference_close": Decimal("100.00"),
                "stop_preview": Decimal("80.00"),
                "sessions_out_before": 7,
            }
            await _insert(session, "tw_signal_daily", **signal)
            order = {
                "user_id": user_id,
                "instrument_id": instrument_id,
                "signal_date": dt.date(2026, 9, 10),
                "quantity": 10,
                "stop_price": Decimal("80.00"),
                "state": "PROPOSED",
            }
            with pytest.raises(IntegrityError, match="side_known"):
                async with session.begin_nested():
                    await _insert(session, "tw_order", **{**order, "side": "SHORT"})
            await _insert(session, "tw_order", **{**order, "side": "BUY"})

    async def test_an_order_for_a_signal_nobody_stored_is_refused(self, twt_url: str) -> None:
        """`03` §6: every order names the stored row that produced it, so an order is always
        answerable. TW4's detection is an upsert, so a re-run never needs to delete one."""
        async with _rolled_back(twt_url) as session:
            user_id = await _fresh_user(session, "orphan-order")
            instrument_id = await _instrument(session, "TW")
            with pytest.raises(IntegrityError, match="fk_tw_order_signal"):
                async with session.begin_nested():
                    await _insert(
                        session,
                        "tw_order",
                        user_id=user_id,
                        instrument_id=instrument_id,
                        signal_date=dt.date(2026, 9, 10),
                        side="BUY",
                        quantity=10,
                        stop_price=Decimal("80.00"),
                        state="PROPOSED",
                    )

    async def test_one_entry_order_per_name_per_signal_per_side(self, twt_url: str) -> None:
        """House rule 7: a re-run of the evening cannot double-place."""
        async with _rolled_back(twt_url) as session:
            user_id = await _fresh_user(session, "double-place")
            instrument_id = await _instrument(session, "TW")
            await _insert(
                session,
                "tw_signal_daily",
                user_id=user_id,
                date=dt.date(2026, 9, 10),
                instrument_id=instrument_id,
                state="SIGNAL",
                entry_reference_close=Decimal("100.00"),
                stop_preview=Decimal("80.00"),
                sessions_out_before=7,
            )
            order = {
                "user_id": user_id,
                "instrument_id": instrument_id,
                "signal_date": dt.date(2026, 9, 10),
                "side": "BUY",
                "quantity": 10,
                "stop_price": Decimal("80.00"),
                "state": "PROPOSED",
            }
            await _insert(session, "tw_order", **order)
            with pytest.raises(IntegrityError, match="uq_tw_order_one_per_signal"):
                async with session.begin_nested():
                    await _insert(session, "tw_order", **order)

    async def test_a_skip_reason_outside_the_twelve_is_refused(self, twt_url: str) -> None:
        """A plan is not honest without its skips, and a skip nobody can read is not a skip."""
        async with _rolled_back(twt_url) as session:
            user_id = await _fresh_user(session, "skip")
            instrument_id = await _instrument(session, "TW")
            plan_row = (
                await session.execute(
                    sa.insert(Base.metadata.tables["tw_plan"])
                    .values(
                        plan_id=uuid.uuid4(),
                        user_id=user_id,
                        session_date=dt.date(2026, 9, 10),
                        source="EVENING",
                        built_at=dt.datetime(2026, 9, 10, 16, 0, tzinfo=dt.UTC),
                        expires_at=dt.datetime(2026, 9, 10, 16, 30, tzinfo=dt.UTC),
                        plan_hash="0" * 64,
                        gate="OPEN",
                        sleeve_equity_inr=Decimal("0.00"),
                    )
                    .returning(Base.metadata.tables["tw_plan"].c.id)
                )
            ).scalar_one()
            with pytest.raises(IntegrityError, match="reason_known"):
                async with session.begin_nested():
                    await _insert(
                        session,
                        "tw_plan_skip",
                        plan_id=plan_row,
                        user_id=user_id,
                        instrument_id=instrument_id,
                        reason="EMA_BELOW",
                    )
            await _insert(
                session,
                "tw_plan_skip",
                plan_id=plan_row,
                user_id=user_id,
                instrument_id=instrument_id,
                reason="NO_SLEEVE_CAPITAL",
            )

    async def test_a_plan_cannot_expire_before_it_was_built(self, twt_url: str) -> None:
        async with _rolled_back(twt_url) as session:
            user_id = await _fresh_user(session, "expiry")
            with pytest.raises(IntegrityError, match="expiry_after_build"):
                async with session.begin_nested():
                    await _insert(
                        session,
                        "tw_plan",
                        plan_id=uuid.uuid4(),
                        user_id=user_id,
                        session_date=dt.date(2026, 9, 10),
                        source="EVENING",
                        built_at=dt.datetime(2026, 9, 10, 16, 0, tzinfo=dt.UTC),
                        expires_at=dt.datetime(2026, 9, 10, 15, 30, tzinfo=dt.UTC),
                        plan_hash="0" * 64,
                        gate="OPEN",
                        sleeve_equity_inr=Decimal("0.00"),
                    )

    async def test_a_thin_session_can_never_open_the_gate(self, twt_url: str) -> None:
        """`04` §2.1: a muhurat session is not a session this strategy trades, and `03` §4 says
        the row written for the record carries a SHUT gate and null percentages."""
        async with _rolled_back(twt_url) as session:
            user_id = await _fresh_user(session, "thin")
            breadth = {
                "user_id": user_id,
                "date": dt.date(2026, 1, 20),
                "universe_count": 200,
                "measured_count": 0,
                "above_count": 0,
                "thin_session": True,
            }
            with pytest.raises(IntegrityError, match="thin_session_is_shut"):
                async with session.begin_nested():
                    await _insert(session, "tw_breadth_daily", **{**breadth, "gate": "OPEN"})
            await _insert(session, "tw_breadth_daily", **{**breadth, "gate": "SHUT"})

    async def test_a_measured_session_must_carry_its_percentage(self, twt_url: str) -> None:
        """The null is for the thin-session row only; a session the panel measured and did not
        record a percentage for is a gate nobody can audit."""
        async with _rolled_back(twt_url) as session:
            user_id = await _fresh_user(session, "measured")
            with pytest.raises(IntegrityError, match="measured_session_has_a_percentage"):
                async with session.begin_nested():
                    await _insert(
                        session,
                        "tw_breadth_daily",
                        user_id=user_id,
                        date=dt.date(2026, 9, 10),
                        universe_count=1900,
                        measured_count=1800,
                        above_count=900,
                        gate="OPEN",
                    )

    async def test_a_signal_preview_stop_must_sit_below_its_entry_reference(
        self, twt_url: str
    ) -> None:
        """`04` §7.1's STOP_NOT_BELOW_ENTRY. It cannot arise at a 20 % stop; the check is there
        because a ceiling edit could make it arise."""
        async with _rolled_back(twt_url) as session:
            user_id = await _fresh_user(session, "stop-preview")
            instrument_id = await _instrument(session, "TW")
            with pytest.raises(IntegrityError, match="stop_below_entry"):
                async with session.begin_nested():
                    await _insert(
                        session,
                        "tw_signal_daily",
                        user_id=user_id,
                        date=dt.date(2026, 9, 10),
                        instrument_id=instrument_id,
                        state="SIGNAL",
                        entry_reference_close=Decimal("100.00"),
                        stop_preview=Decimal("100.00"),
                        sessions_out_before=7,
                    )


@requires_db
class TestDeletingAUserTakesTheSleeveWithIt:
    async def test_the_whole_sleeve_cascades(self, twt_url: str) -> None:
        """Prompt 12 §5 (DPDP erasure), and the case the composite foreign key could have
        broken: ``fk_tw_order_signal`` is NO ACTION, and a NO ACTION check is made at the end of
        the statement — by which time the cascade from ``app_user`` has removed both sides.
        """
        async with _rolled_back(twt_url) as session:
            user_id = await _fresh_user(session, "erasure")
            instrument_id = await _instrument(session, "TW")
            await _insert(
                session,
                "tw_signal_daily",
                user_id=user_id,
                date=dt.date(2026, 9, 10),
                instrument_id=instrument_id,
                state="SIGNAL",
                entry_reference_close=Decimal("100.00"),
                stop_preview=Decimal("80.00"),
                sessions_out_before=7,
            )
            await _insert(
                session,
                "tw_order",
                user_id=user_id,
                instrument_id=instrument_id,
                signal_date=dt.date(2026, 9, 10),
                side="BUY",
                quantity=10,
                stop_price=Decimal("80.00"),
                state="PROPOSED",
            )
            await session.execute(sa.delete(AppUser).where(AppUser.id == user_id))
            await session.flush()
            for table in ("tw_signal_daily", "tw_order"):
                rows = Base.metadata.tables[table]
                remaining = (
                    await session.execute(
                        sa.select(sa.func.count())
                        .select_from(rows)
                        .where(rows.c.user_id == user_id)
                    )
                ).scalar_one()
                assert remaining == 0, f"{table} kept a row after the user was erased"


@requires_db
class TestAPatchAgainstTheBoundsChangesNothingWhenItIsRefused:
    """The pure half of this claim is in ``test_twt_ceilings.py``; this is the half that needs a
    database, because "nothing was written" is a statement about rows.

    A patch that raises two values and crosses a bound on the second must change **neither** —
    otherwise a rejected save leaves the settings half-applied, and the person's next read shows
    a state they never asked for.
    """

    @staticmethod
    def _bounds() -> TwtCeilings:
        return TwtCeilings(
            max_open_positions=15,
            max_position_pct=Decimal("15.00"),
            stop_pct=Decimal("25.00"),
            trail_pct_min=Decimal("18.00"),
        )

    async def _seeded(self, session: AsyncSession, tag: str) -> int:
        user_id = await _fresh_user(session, tag)
        await _insert(session, "tw_config", user_id=user_id, updated_by="seed")
        return user_id

    async def test_a_patch_above_the_ceiling_is_refused_and_writes_nothing(
        self, twt_url: str
    ) -> None:
        async with _rolled_back(twt_url) as session:
            user_id = await self._seeded(session, "ceiling-atomic")
            with pytest.raises(Problem) as caught:
                await apply_patch(
                    session,
                    user_id=user_id,
                    patch=TwtConfigPatch.model_validate(
                        {"max_open_positions": 12, "stop_pct": "30.00"}
                    ),
                    ceilings=self._bounds(),
                    changed_by="test",
                    now=dt.datetime.now(tz=dt.UTC),
                )
            assert caught.value.type is ProblemType.SETTING_ABOVE_CEILING
            assert caught.value.extra["env_var"] == "BASKFY_TWT_STOP_PCT_MAX"
            session.expire_all()
            row = await read_config(session, user_id)
            assert row.max_open_positions == 10
            assert row.stop_pct == Decimal("20.00")
            assert await audit_trail(session, user_id=user_id) == []

    async def test_a_trail_below_the_floor_is_refused_and_writes_nothing(
        self, twt_url: str
    ) -> None:
        """DECISIONS-TW TW0.5, at the database. The value that is refused here is *smaller* than
        the one in force, which is the opposite of every other refusal in this repository."""
        async with _rolled_back(twt_url) as session:
            user_id = await self._seeded(session, "floor-atomic")
            with pytest.raises(Problem) as caught:
                await apply_patch(
                    session,
                    user_id=user_id,
                    patch=TwtConfigPatch.model_validate(
                        {"max_position_pct": "11.00", "trail_pct": "15.00"}
                    ),
                    ceilings=self._bounds(),
                    changed_by="test",
                    now=dt.datetime.now(tz=dt.UTC),
                )
            assert caught.value.type is ProblemType.SETTING_BELOW_FLOOR
            assert caught.value.extra["env_var"] == "BASKFY_TWT_TRAIL_PCT_MIN"
            session.expire_all()
            row = await read_config(session, user_id)
            assert row.max_position_pct == Decimal("12.50")
            assert row.trail_pct == Decimal("20.00")
            assert await audit_trail(session, user_id=user_id) == []

    async def test_a_wider_trail_is_accepted_and_audited(self, twt_url: str) -> None:
        """The other half: widening is conservative, not dangerous, and the audit row is what
        answers "what was ``trail_pct`` on the morning that stop was armed" months later."""
        async with _rolled_back(twt_url) as session:
            user_id = await self._seeded(session, "floor-widen")
            await apply_patch(
                session,
                user_id=user_id,
                patch=TwtConfigPatch.model_validate({"trail_pct": "25.00"}),
                ceilings=self._bounds(),
                changed_by="maulik",
                now=dt.datetime.now(tz=dt.UTC),
            )
            row = await read_config(session, user_id)
            assert row.trail_pct == Decimal("25.00")
            trail = [entry for entry in await audit_trail(session, user_id=user_id)]
            assert [(e.key, e.old_value, e.new_value) for e in trail] == [
                ("trail_pct", "20.00", "25.00")
            ]

    async def test_the_first_live_countdown_moves_only_through_the_jobs_own_door(
        self, twt_url: str
    ) -> None:
        """`04` §6.4: decremented once per **filled** entry by the session that filled it, never
        by a request. ``TwtConfigPatch`` has no field for it, and the function that can write it
        is named after the job."""
        async with _rolled_back(twt_url) as session:
            user_id = await self._seeded(session, "first-live")
            row = await record_system_change(
                session,
                user_id=user_id,
                field="first_live_entries_left",
                value=9,
                changed_by="twt-evening",
                now=dt.datetime.now(tz=dt.UTC),
            )
            assert row.first_live_entries_left == 9
            with pytest.raises(ValueError, match="not a system-owned field"):
                await record_system_change(
                    session,
                    user_id=user_id,
                    field="trail_pct",
                    value=15,
                    changed_by="twt-evening",
                    now=dt.datetime.now(tz=dt.UTC),
                )
