"""SW2's acceptance: the `sw_` schema is real, idempotent, tenant-keyed, and bounded.

Four claims, and each one is a thing that would otherwise only be discovered in production:

1. **Every `sw_` table carries `user_id`, non-null.** `docs/swing/02-scope-and-gating.md` Track C
   §6. A table that forgets it is a table that needs a migration on the day a second account
   exists — which is the day nobody wants to be writing one.
2. **Migrate → seed → migrate is a no-op.** House rule 7. A seeder that resets a person's risk
   setting every time `make seed` runs is worse than no seeder.
3. **A setting above its ceiling is refused, and the refusal names the ceiling.** The M4.1
   boundary (`docs/03` §3f). Refusing without saying what the limit is makes the caller bisect.
4. **The two system-owned fields are not reachable from a patch.** A person who could set the
   exposure rung has deleted the ladder.

The structural half runs everywhere; the half that needs a database is marked `db`.
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal

import pytest
import screener_helpers
import sqlalchemy as sa
from screener_helpers import requires_db
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from baskfy_api.problems import Problem, ProblemType
from baskfy_api.seed import seed_swing_config
from baskfy_api.settings import Settings
from baskfy_api.swing_settings import (
    CEILING_ENV,
    EDITABLE_FIELDS,
    SYSTEM_OWNED_FIELDS,
    SwingCeilings,
    SwingConfigNotSeeded,
    SwingConfigPatch,
    apply_patch,
    audit_trail,
    read_config,
    record_system_change,
)
from baskfy_core.models import AppUser, Base, SwConfig
from baskfy_core.models.swing import SW_SKIP_REASONS
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG
from baskfy_worker.settings import WorkerSettings

SWING_TABLES = sorted(name for name in Base.metadata.tables if name.startswith("sw_"))

#: The migrations that create them — every ``00NN_swing*.py`` under ``alembic/versions``, read by
#: path rather than by module name because alembic version files are not a package. A glob rather
#: than one file: `0028_swing.py` made twelve tables, `0029_swing_backtest.py` the thirteenth, and a
#: test that read only the first would have missed the second (it did, for a module).
MIGRATIONS = {
    path.name: path.read_text(encoding="utf-8")
    for path in sorted((screener_helpers.API_DIR / "alembic" / "versions").glob("00*_swing*.py"))
}
MIGRATION = "\n".join(MIGRATIONS.values())


class TestEveryTableIsTenantKeyed:
    def test_there_are_swing_tables_at_all(self) -> None:
        """A guard on the guard: an empty list would make every test below vacuous."""
        assert len(SWING_TABLES) >= 11, SWING_TABLES

    @pytest.mark.parametrize("table_name", SWING_TABLES)
    def test_table_has_a_non_null_user_id(self, table_name: str) -> None:
        table = Base.metadata.tables[table_name]
        assert "user_id" in table.c, (
            f"{table_name} has no user_id. docs/swing/02 Track C §6: every sw_ row carries one, "
            f"so that the day D3 is answered nothing needs a migration."
        )
        assert not table.c["user_id"].nullable, f"{table_name}.user_id is nullable"

    @pytest.mark.parametrize("table_name", SWING_TABLES)
    def test_user_id_points_at_app_user_and_cascades(self, table_name: str) -> None:
        """A user deleted under DPDP takes their swing book with them (Prompt 12 §5)."""
        column = Base.metadata.tables[table_name].c["user_id"]
        targets = {fk.target_fullname for fk in column.foreign_keys}
        assert targets == {"app_user.id"}, f"{table_name}.user_id -> {targets}"
        assert all(fk.ondelete == "CASCADE" for fk in column.foreign_keys)

    def test_there_are_swing_migrations_at_all(self) -> None:
        assert {"0028_swing.py", "0029_swing_backtest.py", "0030_swing_primary_sources.py"} <= set(
            MIGRATIONS
        )

    @pytest.mark.parametrize("table_name", SWING_TABLES)
    def test_the_migration_drops_what_it_creates(self, table_name: str) -> None:
        """A table added to `upgrade` and forgotten in the downgrade leaks into the next database.

        `0028_swing.py` drops its twelve from a `TABLES` tuple; `0029_swing_backtest.py` drops
        its one by name. Either way the table's name appears quoted in a migration that also
        drops it, and that is what is asserted."""
        creators = [
            name
            for name, source in MIGRATIONS.items()
            if f'"{table_name}"' in source and "drop_table" in source
        ]
        assert creators, (
            f"{table_name} is in the models but no swing migration names it beside a "
            f"`drop_table`, so `make downgrade` would leave it behind"
        )


class TestTheFlagsDefaultOff:
    """Track B: "built dark, flag-off". The default is where that lives."""

    #: Read off the field declarations rather than off a constructed instance. An instance
    #: reads `.env`, so a machine with the flag exported would make the assertion pass or fail
    #: for a reason that has nothing to do with what this repository ships.
    FLAGS = ("swing_execution_enabled", "swing_monitor_enabled", "swing_ep_premarket_enabled")

    @pytest.mark.parametrize("flag", FLAGS)
    def test_api_settings_default_every_swing_flag_false(self, flag: str) -> None:
        assert Settings.model_fields[flag].default is False

    @pytest.mark.parametrize("flag", FLAGS)
    def test_worker_settings_default_every_swing_flag_false(self, flag: str) -> None:
        assert WorkerSettings.model_fields[flag].default is False

    def test_this_deployment_reads_them_as_false_too(self) -> None:
        """And the constructed settings agree, on this machine, with this `.env`."""
        settings = Settings()
        assert settings.swing_execution_enabled is False
        assert settings.swing_monitor_enabled is False
        assert settings.swing_ep_premarket_enabled is False

    def test_the_repository_ships_no_environment_file_that_enables_execution(self) -> None:
        """The same scan Prompt 20 §2 runs over `BASKFY_PUBLIC_API_ENABLED`.

        `docs/swing/02` §3: "There is no engineering path around this gate. The run ends with the
        flag false." A committed `.env.example` that turns it on would be exactly such a path,
        because a new deployment copies that file verbatim.
        """
        root = screener_helpers.API_DIR.parents[2]
        offenders: list[str] = []
        for path in sorted(root.rglob(".env*")):
            if any(part in {"node_modules", ".git", ".venv"} for part in path.parts):
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.split("#", 1)[0].strip()
                if not stripped.startswith("BASKFY_SWING_"):
                    continue
                if stripped.endswith("=true") or stripped.endswith("=True"):
                    offenders.append(f"{path.relative_to(root)}:{number}: {stripped}")
        assert offenders == [], f"a swing flag is enabled in a committed env file: {offenders}"


class TestTheCeilings:
    def setup_method(self) -> None:
        self.ceilings = SwingCeilings(
            risk_per_trade_pct=Decimal("1.0"),
            max_position_pct=Decimal("25.0"),
            max_open_positions=10,
        )

    def test_a_value_at_the_ceiling_is_allowed(self) -> None:
        """The ceiling is a maximum, not an exclusive bound — 1.0% is a legal risk."""
        self.ceilings.check("risk_per_trade_pct", Decimal("1.0"))

    def test_a_value_above_the_ceiling_is_refused_with_the_ceiling_named(self) -> None:
        with pytest.raises(Problem) as caught:
            self.ceilings.check("risk_per_trade_pct", Decimal("1.5"))
        problem = caught.value
        assert problem.status == 422
        assert problem.type is ProblemType.SETTING_ABOVE_CEILING
        assert problem.extra["ceiling"] == "1.0"
        assert problem.extra["requested"] == "1.5"
        assert problem.extra["env_var"] == "BASKFY_SWING_RISK_PER_TRADE_PCT_MAX"
        assert "1.0" in problem.detail

    @pytest.mark.parametrize("field", sorted(CEILING_ENV))
    def test_every_bounded_field_has_a_ceiling_and_an_env_var(self, field: str) -> None:
        assert getattr(self.ceilings, field) is not None
        assert CEILING_ENV[field].startswith("BASKFY_SWING_")

    def test_an_unbounded_field_is_not_refused(self) -> None:
        """Sleeve capital has no ceiling: it is the person's own money, not a risk multiplier."""
        self.ceilings.check("sleeve_capital_inr", Decimal("10000000"))

    def test_the_defaults_sit_under_the_default_ceilings(self) -> None:
        """A shipped default that already violates the shipped ceiling is a trap."""
        engine = DEFAULT_SWING_CONFIG.sizing
        risk_max = Settings.model_fields["swing_risk_per_trade_pct_max"].default
        position_max = Settings.model_fields["swing_max_position_pct_max"].default
        positions_max = Settings.model_fields["swing_max_open_positions_max"].default
        assert Decimal(str(engine.risk_per_trade_pct)) <= Decimal(str(risk_max))
        assert Decimal(str(engine.max_position_pct)) <= Decimal(str(position_max))
        assert engine.max_open_positions <= positions_max

    def test_the_ceilings_are_his_own_numbers(self) -> None:
        """`docs/swing/07` (SW9.5), quoted: risk — "I rarely risk more than 1% of my account on
        any trade" → 1.0; position — "I don't believe you should ever have more than 30% of
        your account over night in any stock or ETF" → 30 (was 25); positions — "In a good
        market 15-20 positions" → 20 (was 10, the old ladder's top rung plus two). The API and
        the worker mirror the same three, and `.env.example` ships them."""
        api = {
            "risk": Settings.model_fields["swing_risk_per_trade_pct_max"].default,
            "position": Settings.model_fields["swing_max_position_pct_max"].default,
            "positions": Settings.model_fields["swing_max_open_positions_max"].default,
        }
        worker = {
            "risk": WorkerSettings.model_fields["swing_risk_per_trade_pct_max"].default,
            "position": WorkerSettings.model_fields["swing_max_position_pct_max"].default,
            "positions": WorkerSettings.model_fields["swing_max_open_positions_max"].default,
        }
        assert (api["risk"], api["position"], api["positions"]) == (
            Decimal("1.0"),
            Decimal("30.0"),
            20,
        )
        assert (worker["risk"], worker["position"], worker["positions"]) == (1.0, 30.0, 20)
        # The ladder's top rung (his typical ten) sits under the ceiling (his fifteen to twenty),
        # so `min(rung, sizing.max_open_positions)` can be raised by a setting up to the ceiling.
        assert DEFAULT_SWING_CONFIG.market.tiers[-1][0] == 10 <= api["positions"]
        env = (screener_helpers.API_DIR.parents[2] / ".env.example").read_text(encoding="utf-8")
        assert "BASKFY_SWING_MAX_POSITION_PCT_MAX=30.0" in env
        assert "BASKFY_SWING_MAX_OPEN_POSITIONS_MAX=20" in env
        assert "BASKFY_SWING_RISK_PER_TRADE_PCT_MAX=1.0" in env

    def test_the_skip_reason_constraint_in_the_latest_migration_is_the_engines_list(self) -> None:
        """`0030_swing_primary_sources.py` rebuilds `ck_sw_plan_skip_reason_known` with the two
        reasons SW9.5 added (`SESSION_CAP`, `DRAWDOWN_LOCKOUT`). The migration writes the list
        out (a migration is frozen); this is the check that it is the engine's list, so a third
        reason cannot be added to `SkipReason` without a migration that lets the row be written."""
        source = MIGRATIONS["0030_swing_primary_sources.py"]
        namespace: dict[str, object] = {}
        block = source[source.index("NEW_SKIP_REASONS = (") : source.index("def _reason_check")]
        exec(block, namespace)
        assert namespace["NEW_SKIP_REASONS"] == SW_SKIP_REASONS
        assert {"SESSION_CAP", "DRAWDOWN_LOCKOUT"} <= set(SW_SKIP_REASONS)

    def test_the_drawdown_state_is_system_owned_and_written_by_the_evening(self) -> None:
        """`03` §1 (SW9.5): `sleeve_peak_inr`, `drawdown_pct` and `drawdown_locked` are the
        evening's measurements of the book — `swing-eod` owns them, and a form cannot touch them
        (`test_a_system_owned_field_cannot_be_patched` covers the refusal for each)."""
        for field in ("sleeve_peak_inr", "drawdown_pct", "drawdown_locked"):
            assert SYSTEM_OWNED_FIELDS[field] == "swing-eod"
            assert field not in EDITABLE_FIELDS


class TestThePatchShape:
    @pytest.mark.parametrize("field", sorted(SYSTEM_OWNED_FIELDS))
    def test_a_system_owned_field_cannot_be_patched(self, field: str) -> None:
        """`exposure_level` and `first_live_sessions_left` are the system's memory, not a form."""
        with pytest.raises(ValueError, match=r"extra_forbidden|Extra inputs"):
            SwingConfigPatch.model_validate({field: 3})

    def test_an_unknown_field_is_refused_rather_than_ignored(self) -> None:
        with pytest.raises(ValueError, match=r"extra_forbidden|Extra inputs"):
            SwingConfigPatch.model_validate({"risk_per_trade": 0.5})

    def test_an_empty_patch_changes_nothing(self) -> None:
        assert SwingConfigPatch().changes() == {}

    def test_only_the_fields_the_caller_set_are_changes(self) -> None:
        patch = SwingConfigPatch.model_validate({"risk_per_trade_pct": "0.25"})
        assert patch.changes() == {"risk_per_trade_pct": Decimal("0.25")}

    @pytest.mark.parametrize("window", [2, 15, 0])
    def test_an_opening_range_window_the_engine_does_not_know_is_refused(self, window: int) -> None:
        """`docs/swing/04` §7.1 admits 1, 5 and 60. A 15-minute range has no code behind it."""
        with pytest.raises(ValueError, match=r"literal_error|Input should be"):
            SwingConfigPatch.model_validate({"or_window_minutes": window})

    def test_the_stop_mode_is_the_engine_s_enum(self) -> None:
        patch = SwingConfigPatch.model_validate({"stop_mode": "OPENING_RANGE_LOW"})
        assert patch.changes() == {"stop_mode": "OPENING_RANGE_LOW"}
        with pytest.raises(ValueError, match=r"enum|Input should be"):
            SwingConfigPatch.model_validate({"stop_mode": "GUT_FEEL"})


# --- the database half -------------------------------------------------------


@pytest.fixture(scope="module")
def swing_url() -> str:
    return screener_helpers.seeded_database()


async def _session(url: str) -> tuple[AsyncSession, AsyncEngine]:
    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    return maker(), engine


async def _ensure_user(session: AsyncSession) -> int:
    existing = (
        await session.execute(sa.select(AppUser.id).order_by(AppUser.id).limit(1))
    ).scalar_one_or_none()
    if existing is not None:
        return int(existing)
    user = AppUser(public_id="sw2-test", email="sw2@example.test")
    session.add(user)
    await session.flush()
    return int(user.id)


@requires_db
class TestTheSchemaOnARealDatabase:
    async def test_every_modelled_column_exists_with_the_modelled_nullability(
        self, swing_url: str
    ) -> None:
        """The models and the migration agree. They are written separately; they can drift."""
        session, engine = await _session(swing_url)
        try:
            rows = (
                await session.execute(
                    sa.text(
                        "SELECT table_name, column_name, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name LIKE 'sw\\_%'"
                    )
                )
            ).all()
        finally:
            await session.close()
            await engine.dispose()

        actual = {(t, c): (n == "YES") for t, c, n in rows}
        missing: list[str] = []
        mismatched: list[str] = []
        for name in SWING_TABLES:
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

    async def test_migrate_seed_migrate_is_a_noop(self, swing_url: str) -> None:
        """House rule 7, over the whole SW2 chain rather than over one insert.

        The capital is deliberately changed between the two seeds: a seeder written with
        ``ON CONFLICT DO UPDATE`` would pass a naive "the row still exists" check and would
        silently reset a person's sleeve to zero on the next deploy.
        """
        session, engine = await _session(swing_url)
        try:
            async with session.begin():
                user_id = await _ensure_user(session)
                assert await seed_swing_config(session) == 1
            async with session.begin():
                row = await read_config(session, user_id)
                assert row.sleeve_capital_inr == Decimal("0.00")
                row.sleeve_capital_inr = Decimal("500000.00")
                row.risk_per_trade_pct = Decimal("0.250")

            screener_helpers.migrate(swing_url)

            async with session.begin():
                assert await seed_swing_config(session) == 1
            session.expire_all()
            async with session.begin():
                row = await read_config(session, user_id)
                assert row.sleeve_capital_inr == Decimal("500000.00")
                assert row.risk_per_trade_pct == Decimal("0.250")
                count = (
                    await session.execute(sa.select(sa.func.count()).select_from(SwConfig))
                ).scalar_one()
                assert count == 1
        finally:
            await session.close()
            await engine.dispose()

    async def test_a_missing_row_is_never_created_by_a_read(self, swing_url: str) -> None:
        session, engine = await _session(swing_url)
        try:
            with pytest.raises(SwingConfigNotSeeded):
                await read_config(session, 9_999_999)
        finally:
            await session.close()
            await engine.dispose()

    async def test_a_patch_above_the_ceiling_changes_nothing_at_all(self, swing_url: str) -> None:
        """The refusal is atomic: a two-field patch that fails on the second writes neither."""
        session, engine = await _session(swing_url)
        ceilings = SwingCeilings(
            risk_per_trade_pct=Decimal("1.0"),
            max_position_pct=Decimal("25.0"),
            max_open_positions=10,
        )
        try:
            async with session.begin():
                user_id = await _ensure_user(session)
                await seed_swing_config(session)
            async with session.begin():
                before = await read_config(session, user_id)
                original_capital = before.sleeve_capital_inr
                original_risk = before.risk_per_trade_pct

            with pytest.raises(Problem) as caught:
                async with session.begin():
                    await apply_patch(
                        session,
                        user_id=user_id,
                        patch=SwingConfigPatch.model_validate(
                            {"sleeve_capital_inr": "1000000", "risk_per_trade_pct": "1.5"}
                        ),
                        ceilings=ceilings,
                        changed_by="test",
                        now=dt.datetime.now(tz=dt.UTC),
                    )
            assert caught.value.extra["field"] == "risk_per_trade_pct"

            session.expire_all()
            async with session.begin():
                after = await read_config(session, user_id)
                assert after.sleeve_capital_inr == original_capital
                assert after.risk_per_trade_pct == original_risk
        finally:
            await session.close()
            await engine.dispose()

    async def test_an_accepted_patch_writes_one_audit_row_per_changed_field(
        self, swing_url: str
    ) -> None:
        session, engine = await _session(swing_url)
        ceilings = SwingCeilings(
            risk_per_trade_pct=Decimal("1.0"),
            max_position_pct=Decimal("25.0"),
            max_open_positions=10,
        )
        now = dt.datetime.now(tz=dt.UTC)
        try:
            async with session.begin():
                user_id = await _ensure_user(session)
                await seed_swing_config(session)
                await session.execute(
                    sa.update(SwConfig)
                    .where(SwConfig.user_id == user_id)
                    .values(risk_per_trade_pct=Decimal("0.500"), max_position_pct=Decimal("20.00"))
                )
            async with session.begin():
                await apply_patch(
                    session,
                    user_id=user_id,
                    patch=SwingConfigPatch.model_validate(
                        {"risk_per_trade_pct": "0.250", "max_position_pct": "20.00"}
                    ),
                    ceilings=ceilings,
                    changed_by="user:1",
                    now=now,
                    note="halving risk after a bad week",
                )
            async with session.begin():
                trail = await audit_trail(session, user_id=user_id)
                keys = [row.key for row in trail]
                # `max_position_pct` was set to the value it already had, so it did not change
                # and must not have produced a row: an audit full of no-ops is an audit nobody
                # reads.
                assert keys.count("max_position_pct") == 0
                latest = next(row for row in trail if row.key == "risk_per_trade_pct")
                assert latest.old_value == "0.500"
                assert latest.new_value == "0.250"
                assert latest.changed_by == "user:1"
                assert latest.note == "halving risk after a bad week"
        finally:
            await session.close()
            await engine.dispose()

    async def test_a_system_field_is_written_only_through_the_system_path(
        self, swing_url: str
    ) -> None:
        session, engine = await _session(swing_url)
        try:
            async with session.begin():
                user_id = await _ensure_user(session)
                await seed_swing_config(session)
            async with session.begin():
                row = await record_system_change(
                    session,
                    user_id=user_id,
                    field="exposure_level",
                    value=1,
                    changed_by="swing-eod",
                    now=dt.datetime.now(tz=dt.UTC),
                )
                assert row.exposure_level == 1
            async with session.begin():
                with pytest.raises(ValueError, match="not a system-owned field"):
                    await record_system_change(
                        session,
                        user_id=user_id,
                        field="risk_per_trade_pct",
                        value=1,
                        changed_by="swing-eod",
                        now=dt.datetime.now(tz=dt.UTC),
                    )
        finally:
            await session.close()
            await engine.dispose()

    async def test_a_stop_can_never_be_written_below_the_initial_one(self, swing_url: str) -> None:
        """`ck_sw_position_stop_never_below_initial` — the floor a bug cannot get under.

        The rule ("never widen a stop") is enforced in `stops.apply` and again in the desk's
        `/swing/execute`. This asserts the third line of defence, because the first two are code
        and the failure they guard against is the one that turns a losing trade into a lost
        account.
        """
        session, engine = await _session(swing_url)
        try:
            async with session.begin():
                user_id = await _ensure_user(session)
                instrument_id = (
                    await session.execute(sa.text("SELECT id FROM instrument ORDER BY id LIMIT 1"))
                ).scalar_one()
                await session.execute(
                    sa.text(
                        "INSERT INTO sw_position (user_id, instrument_id, setup, entry_date, "
                        "entry_avg, quantity_entered, initial_stop, stop, trail, quantity_open, "
                        "state, simulated) VALUES (:u, :i, 'FLAG', '2026-09-01', 100.0, 10, "
                        "95.00, 95.00, 'MA20', 10, 'OPEN', true)"
                    ),
                    {"u": user_id, "i": instrument_id},
                )
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin():
                    await session.execute(
                        sa.text(
                            "UPDATE sw_position SET stop = 90.00 WHERE user_id = :u AND "
                            "instrument_id = :i"
                        ),
                        {"u": user_id, "i": instrument_id},
                    )
        finally:
            await session.close()
            await engine.dispose()


def test_the_environment_this_suite_runs_in_does_not_enable_execution() -> None:
    """A belt-and-braces check on the *process*, not on the defaults.

    Everything above asserts what an unconfigured deployment does. This asserts that the machine
    running the tests has not been configured differently — because a developer with the flag
    exported would see every "defaults to false" test pass while their own stack was live.
    """
    assert os.environ.get("BASKFY_SWING_EXECUTION_ENABLED", "false").lower() != "true"
