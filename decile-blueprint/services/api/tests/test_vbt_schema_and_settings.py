"""VB3's acceptance: the `vb_` schema is real, idempotent, tenant-keyed and bounded.

Four claims, each one a thing that would otherwise only be discovered in production:

1. **Every `vb_` table carries `user_id`, non-null.** `docs/vbt/02-scope-and-gating.md` Track C
   §6. A table that forgets it needs a migration on the day a second account exists — which is
   the day nobody wants to be writing one.
2. **Migrate → seed → migrate is a no-op.** House rule 7. A seeder that resets a person's sleeve
   capital every time `make seed` runs is worse than no seeder.
3. **A setting above its ceiling is refused, and the refusal names the ceiling.** The M4.1
   boundary (`docs/03` §3f). Refusing without saying what the limit is makes the caller bisect.
4. **The two system-owned fields are not reachable from a patch.** A person who could set
   `dry_run_sessions` to 20 has deleted `02` §3.1's gate.

The structural half runs everywhere; the half that needs a database is marked `db`.
"""

from __future__ import annotations

import datetime as dt
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
from baskfy_api.seed import seed_vbt_config
from baskfy_api.settings import Settings
from baskfy_api.vbt_settings import (
    CEILING_ENV,
    EDITABLE_FIELDS,
    STRATEGY_MAX_SLOTS,
    SYSTEM_OWNED_FIELDS,
    VbtCeilings,
    VbtConfigNotSeeded,
    VbtConfigPatch,
    apply_patch,
    audit_trail,
    read_config,
    record_system_change,
)
from baskfy_core.models import AppUser, Base, VbConfig
from baskfy_core.models.vbt import (
    VB_GATES,
    VB_LINE_KINDS,
    VB_SIGNAL_STATES,
    VB_SKIP_REASONS,
)
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, Gate, SignalState
from baskfy_core.vbt.plan import LineKind, SkipReason
from baskfy_core.vbt.sizing import SizeRefusal, size_entry
from baskfy_worker.settings import WorkerSettings

VBT_TABLES = sorted(name for name in Base.metadata.tables if name.startswith("vb_"))

#: The migration that creates them, read by path rather than by module name because alembic
#: version files are not a package. A glob, so a second `*_vbt.py` is picked up automatically.
MIGRATIONS = {
    path.name: path.read_text(encoding="utf-8")
    for path in sorted((screener_helpers.API_DIR / "alembic" / "versions").glob("00*_vbt*.py"))
}
MIGRATION = "\n".join(MIGRATIONS.values())


class TestEveryTableIsTenantKeyed:
    def test_there_are_vbt_tables_at_all(self) -> None:
        """A guard on the guard: an empty list would make every test below vacuous."""
        assert len(VBT_TABLES) == 12, VBT_TABLES

    @pytest.mark.parametrize("table_name", VBT_TABLES)
    def test_table_has_a_non_null_user_id(self, table_name: str) -> None:
        table = Base.metadata.tables[table_name]
        assert "user_id" in table.c, (
            f"{table_name} has no user_id. docs/vbt/02 Track C §6: every vb_ row carries one, so "
            f"that the day multi-tenancy arrives nothing needs a migration."
        )
        assert not table.c["user_id"].nullable, f"{table_name}.user_id is nullable"

    @pytest.mark.parametrize("table_name", VBT_TABLES)
    def test_user_id_points_at_app_user_and_cascades(self, table_name: str) -> None:
        """A user deleted under DPDP takes their sleeve with them (Prompt 12 §5)."""
        column = Base.metadata.tables[table_name].c["user_id"]
        targets = {fk.target_fullname for fk in column.foreign_keys}
        assert targets == {"app_user.id"}, f"{table_name}.user_id -> {targets}"
        assert all(fk.ondelete == "CASCADE" for fk in column.foreign_keys)

    def test_there_is_a_vbt_migration_at_all(self) -> None:
        assert MIGRATIONS, "no *_vbt.py under alembic/versions"

    @pytest.mark.parametrize("table_name", VBT_TABLES)
    def test_the_migration_creates_and_drops_every_table(self, table_name: str) -> None:
        """A table added to `upgrade` and forgotten in the downgrade leaks into the next
        developer's database, where it is then a table nobody can explain."""
        assert f'"{table_name}"' in MIGRATION, f"{table_name} is in the models, not the migration"
        assert "drop_table" in MIGRATION


class TestTheVocabularyIsTheEngines:
    """The database constrains what the engine writes. Two lists are a way to disagree."""

    def test_the_signal_states_are_the_engines(self) -> None:
        assert tuple(state.value for state in SignalState) == VB_SIGNAL_STATES
        assert set(VB_SIGNAL_STATES) == {"SIGNAL", "SCAN_ONLY"}

    def test_the_gate_has_two_values(self) -> None:
        """DECISIONS-VB VB0.4: no amber, because there is no ladder for one to feed."""
        assert VB_GATES == tuple(gate.value for gate in Gate) == ("OPEN", "SHUT")

    def test_the_line_kinds_are_the_engines_and_none_of_them_shorts(self) -> None:
        assert tuple(kind.value for kind in LineKind) == VB_LINE_KINDS
        assert set(VB_LINE_KINDS) == {
            "PLACE_LIMIT",
            "SELL_AT_OPEN",
            "CANCEL_LIMIT",
            "ARM_GTT",
        }

    def test_the_skip_reasons_are_the_engines(self) -> None:
        assert tuple(reason.value for reason in SkipReason) == VB_SKIP_REASONS

    @pytest.mark.parametrize("values", [VB_SIGNAL_STATES, VB_GATES, VB_LINE_KINDS])
    def test_the_migration_constrains_every_value_the_engine_can_write(
        self, values: tuple[str, ...]
    ) -> None:
        """A value the engine emits and the constraint rejects is an outage at 21:00."""
        for value in values:
            assert f"'{value}'" in MIGRATION, f"{value} is not in a CHECK constraint"


class TestTheFlagsDefaultOff:
    """Track B: "built dark, flag-off". The default is where that lives."""

    #: Read off the field declarations rather than off a constructed instance: an instance reads
    #: `.env`, so a machine with the flag exported would make the assertion pass or fail for a
    #: reason that has nothing to do with what this repository ships.
    def test_api_settings_default_execution_false(self) -> None:
        assert Settings.model_fields["vbt_execution_enabled"].default is False

    def test_worker_settings_default_execution_false(self) -> None:
        assert WorkerSettings.model_fields["vbt_execution_enabled"].default is False

    def test_detection_defaults_on_because_it_moves_no_money(self) -> None:
        """`02` Track B — and a sleeve with no history is a sleeve with no evidence."""
        assert Settings.model_fields["vbt_nightly_enabled"].default is True
        assert WorkerSettings.model_fields["vbt_nightly_enabled"].default is True

    def test_this_deployment_reads_execution_as_false_too(self) -> None:
        """And the constructed settings agree, on this machine, with this `.env`."""
        assert Settings().vbt_execution_enabled is False

    def test_no_auto_execute_setting_exists_anywhere(self) -> None:
        """DECISIONS-VB PACK.2 and `02` Track C §3. Non-negotiable 1's named exception is the
        swing sleeve's; this run neither widens it nor adds a second one."""
        offenders = [
            name
            for name in (*Settings.model_fields, *WorkerSettings.model_fields)
            if name.startswith("vbt_") and "auto" in name
        ]
        assert offenders == []

    def test_the_repository_ships_no_environment_file_that_enables_execution(self) -> None:
        """The same scan Prompt 20 §2 runs over `BASKFY_PUBLIC_API_ENABLED`.

        A committed `.env.example` that turned it on would be an engineering path around `02`
        §3's gate, because a new deployment copies that file verbatim. `NIGHTLY` is allowed to be
        true: it detects, and detection places nothing.
        """
        root = screener_helpers.API_DIR.parents[2]
        offenders: list[str] = []
        for path in sorted(root.rglob(".env*")):
            if any(part in {"node_modules", ".git", ".venv"} for part in path.parts):
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.split("#", 1)[0].strip()
                if not stripped.startswith("BASKFY_VBT_"):
                    continue
                if "NIGHTLY" in stripped:
                    continue
                if stripped.lower().endswith("=true"):
                    offenders.append(f"{path.relative_to(root)}:{number}: {stripped}")
        assert offenders == [], f"a VBT flag is enabled in a committed env file: {offenders}"


class TestTheCeilings:
    def setup_method(self) -> None:
        self.ceilings = VbtCeilings(
            max_open_positions=15,
            max_position_pct=Decimal("15.0"),
            stop_pct=Decimal("15.0"),
        )

    def test_a_value_at_the_ceiling_is_allowed(self) -> None:
        """The ceiling is a maximum, not an exclusive bound — a 15% stop is legal."""
        self.ceilings.check("stop_pct", Decimal("15.0"))

    def test_a_value_above_the_ceiling_is_refused_with_the_ceiling_named(self) -> None:
        with pytest.raises(Problem) as caught:
            self.ceilings.check("stop_pct", Decimal("25.0"))
        problem = caught.value
        assert problem.status == 422
        assert problem.type is ProblemType.SETTING_ABOVE_CEILING
        assert problem.extra["ceiling"] == "15.0"
        assert problem.extra["requested"] == "25.0"
        assert problem.extra["env_var"] == "BASKFY_VBT_STOP_PCT_MAX"
        assert "15.0" in problem.detail

    @pytest.mark.parametrize("field", sorted(CEILING_ENV))
    def test_every_bounded_field_has_a_ceiling_and_an_env_var(self, field: str) -> None:
        assert getattr(self.ceilings, field) is not None
        assert CEILING_ENV[field].startswith("BASKFY_VBT_")

    def test_sleeve_capital_has_no_ceiling(self) -> None:
        """It is the person's own money, not a risk multiplier."""
        self.ceilings.check("sleeve_capital_inr", Decimal("100000000"))

    def test_the_defaults_sit_under_the_default_ceilings(self) -> None:
        """A shipped default that already violates the shipped ceiling is a trap."""
        sizing = DEFAULT_VBT_CONFIG.sizing
        assert sizing.max_slots <= Settings.model_fields["vbt_max_open_positions_max"].default
        assert Decimal(str(sizing.max_position_pct)) <= Decimal(
            str(Settings.model_fields["vbt_max_position_pct_max"].default)
        )
        assert Decimal(str(DEFAULT_VBT_CONFIG.exits.stop_pct)) <= Decimal(
            str(Settings.model_fields["vbt_stop_pct_max"].default)
        )

    def test_the_ceilings_are_the_strategys_own_numbers(self) -> None:
        """`docs/vbt/04` and STRATEGY §4: ten slots is the measured optimum (eight cost 2.7 CAGR
        points, fifteen cost 5.3), the slot is 10% with a 12.5% cap, and the stop was measured
        across 10-15%. The ceilings leave room to *lower* the book, never to widen it into a
        different instrument. The API and the worker mirror the same three, and `.env.example`
        ships them."""
        api = (
            Settings.model_fields["vbt_max_open_positions_max"].default,
            Settings.model_fields["vbt_max_position_pct_max"].default,
            Settings.model_fields["vbt_stop_pct_max"].default,
        )
        worker = (
            WorkerSettings.model_fields["vbt_max_open_positions_max"].default,
            WorkerSettings.model_fields["vbt_max_position_pct_max"].default,
            WorkerSettings.model_fields["vbt_stop_pct_max"].default,
        )
        assert api == (15, Decimal("15.0"), Decimal("15.0"))
        assert worker == (15, 15.0, 15.0)
        env = (screener_helpers.API_DIR.parents[2] / ".env.example").read_text(encoding="utf-8")
        assert "BASKFY_VBT_MAX_OPEN_POSITIONS_MAX=15" in env
        assert "BASKFY_VBT_MAX_POSITION_PCT_MAX=15.0" in env
        assert "BASKFY_VBT_STOP_PCT_MAX=15.0" in env

    def test_raising_the_position_setting_can_never_widen_the_book_past_ten(self) -> None:
        """`04` §9.1 takes ``min(setting, sizing.max_slots)``, so a setting above the strategy's
        own slot count is a form that lies rather than a wider book. The ceiling being higher
        than the slot count is deliberate: it leaves the number visible as a setting."""
        assert STRATEGY_MAX_SLOTS == 10
        assert Settings.model_fields["vbt_max_open_positions_max"].default > STRATEGY_MAX_SLOTS


class TestThePatchShape:
    @pytest.mark.parametrize("field", sorted(SYSTEM_OWNED_FIELDS))
    def test_a_system_owned_field_cannot_be_patched(self, field: str) -> None:
        """`dry_run_sessions` is `02` §3.1's gate; a person who could set it has deleted it."""
        with pytest.raises(ValueError, match=r"extra_forbidden|Extra inputs"):
            VbtConfigPatch.model_validate({field: 20})

    def test_an_unknown_field_is_refused_rather_than_ignored(self) -> None:
        with pytest.raises(ValueError, match=r"extra_forbidden|Extra inputs"):
            VbtConfigPatch.model_validate({"breadth_gate_pct": 30})

    def test_an_empty_patch_changes_nothing(self) -> None:
        assert VbtConfigPatch().changes() == {}

    def test_only_the_fields_the_caller_set_are_changes(self) -> None:
        patch = VbtConfigPatch.model_validate({"stop_pct": "10.00"})
        assert patch.changes() == {"stop_pct": Decimal("10.00")}

    @pytest.mark.parametrize("value", [0, -1])
    def test_a_nonsense_stop_is_a_four_hundred_not_a_ceiling_question(self, value: int) -> None:
        """The engine's limits and the server's ceilings are different questions with different
        answers: nonsense is a 400 here, and "too large" is a 422 in `apply_patch`."""
        with pytest.raises(ValueError, match=r"greater_than|Input should be"):
            VbtConfigPatch.model_validate({"stop_pct": value})

    def test_the_editable_fields_are_the_four_pack_five_names(self) -> None:
        """DECISIONS-VB PACK.5 — everything else in `04` is code, with a DECISIONS entry."""
        assert EDITABLE_FIELDS == (
            "sleeve_capital_inr",
            "max_open_positions",
            "max_position_pct",
            "stop_pct",
        )


# --- the database half -------------------------------------------------------


@pytest.fixture(scope="module")
def vbt_url() -> str:
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
    user = AppUser(public_id="vb3-test", email="vb3@example.test")
    session.add(user)
    await session.flush()
    return int(user.id)


async def _fresh_user(session: AsyncSession, tag: str) -> int:
    """A user nobody else's test has touched.

    ``_ensure_user`` reuses the first account, which is right for the tests that only need *a*
    tenant — and wrong for the one below, whose whole claim is about the state of a **freshly
    seeded** row. Sharing a user there would make it assert whatever the previous test left.
    """
    user = AppUser(public_id=f"vb3-{tag}", email=f"vb3-{tag}@example.test")
    session.add(user)
    await session.flush()
    return int(user.id)


def _ceilings() -> VbtCeilings:
    return VbtCeilings(
        max_open_positions=15, max_position_pct=Decimal("15.0"), stop_pct=Decimal("15.0")
    )


@requires_db
class TestTheSchemaOnARealDatabase:
    async def test_every_modelled_column_exists_with_the_modelled_nullability(
        self, vbt_url: str
    ) -> None:
        """The models and the migration are written separately; they can drift."""
        session, engine = await _session(vbt_url)
        try:
            rows = (
                await session.execute(
                    sa.text(
                        "SELECT table_name, column_name, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name LIKE 'vb\\_%'"
                    )
                )
            ).all()
        finally:
            await session.close()
            await engine.dispose()

        actual = {(t, c): (n == "YES") for t, c, n in rows}
        missing: list[str] = []
        mismatched: list[str] = []
        for name in VBT_TABLES:
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

    async def test_migrate_seed_migrate_is_a_noop(self, vbt_url: str) -> None:
        """House rule 7, over the whole VB3 chain rather than over one insert.

        The capital and the stop are deliberately changed between the two seeds: a seeder written
        with ``ON CONFLICT DO UPDATE`` would pass a naive "the row still exists" check and would
        silently reset a person's sleeve to zero on the next deploy.
        """
        session, engine = await _session(vbt_url)
        try:
            async with session.begin():
                user_id = await _ensure_user(session)
                assert await seed_vbt_config(session) == 1
            async with session.begin():
                row = await read_config(session, user_id)
                assert row.sleeve_capital_inr == Decimal("0.00")
                row.sleeve_capital_inr = Decimal("1000000.00")
                row.stop_pct = Decimal("10.00")

            screener_helpers.migrate(vbt_url)

            async with session.begin():
                assert await seed_vbt_config(session) == 1
            session.expire_all()
            async with session.begin():
                row = await read_config(session, user_id)
                assert row.sleeve_capital_inr == Decimal("1000000.00")
                assert row.stop_pct == Decimal("10.00")
                count = (
                    await session.execute(sa.select(sa.func.count()).select_from(VbConfig))
                ).scalar_one()
                assert count == 1
        finally:
            await session.close()
            await engine.dispose()

    async def test_a_seeded_sleeve_has_no_capital_and_therefore_plans_nothing(
        self, vbt_url: str
    ) -> None:
        """`02` §3.4 — the safety property of this table, asserted rather than assumed."""
        session, engine = await _session(vbt_url)
        try:
            async with session.begin():
                user_id = await _fresh_user(session, "seeded-sleeve")
                statement = sa.insert(VbConfig).values(user_id=user_id, updated_by="seed")
                await session.execute(statement)
            async with session.begin():
                row = await read_config(session, user_id)
            sized = size_entry(
                equity=row.sleeve_capital_inr,
                cash_available=row.sleeve_capital_inr,
                limit_price=Decimal("100.00"),
                stop_price=Decimal("88.00"),
                turnover_avg_inr=None,
                config=DEFAULT_VBT_CONFIG.sizing,
            )
            assert sized.quantity == 0
            assert sized.refusal is SizeRefusal.NO_SLEEVE_CAPITAL
        finally:
            await session.close()
            await engine.dispose()

    async def test_a_missing_row_is_never_created_by_a_read(self, vbt_url: str) -> None:
        session, engine = await _session(vbt_url)
        try:
            with pytest.raises(VbtConfigNotSeeded):
                await read_config(session, 9_999_999)
        finally:
            await session.close()
            await engine.dispose()

    async def test_a_patch_above_the_ceiling_changes_nothing_at_all(self, vbt_url: str) -> None:
        """The refusal is atomic: a two-field patch that fails on the second writes neither."""
        session, engine = await _session(vbt_url)
        try:
            async with session.begin():
                user_id = await _ensure_user(session)
                await seed_vbt_config(session)
            async with session.begin():
                before = await read_config(session, user_id)
                original_capital = before.sleeve_capital_inr
                original_stop = before.stop_pct

            with pytest.raises(Problem) as caught:
                async with session.begin():
                    await apply_patch(
                        session,
                        user_id=user_id,
                        patch=VbtConfigPatch.model_validate(
                            {"sleeve_capital_inr": "1000000", "stop_pct": "25"}
                        ),
                        ceilings=_ceilings(),
                        changed_by="test",
                        now=dt.datetime.now(tz=dt.UTC),
                    )
            assert caught.value.extra["field"] == "stop_pct"

            session.expire_all()
            async with session.begin():
                after = await read_config(session, user_id)
                assert after.sleeve_capital_inr == original_capital
                assert after.stop_pct == original_stop
        finally:
            await session.close()
            await engine.dispose()

    async def test_an_accepted_patch_writes_one_audit_row_per_changed_field(
        self, vbt_url: str
    ) -> None:
        session, engine = await _session(vbt_url)
        now = dt.datetime.now(tz=dt.UTC)
        try:
            async with session.begin():
                user_id = await _ensure_user(session)
                await seed_vbt_config(session)
            async with session.begin():
                await apply_patch(
                    session,
                    user_id=user_id,
                    patch=VbtConfigPatch.model_validate(
                        {"sleeve_capital_inr": "1500000", "stop_pct": "11.00"}
                    ),
                    ceilings=_ceilings(),
                    changed_by="test",
                    now=now,
                    note="VB3",
                )
            async with session.begin():
                trail = await audit_trail(session, user_id=user_id)
                keys = [entry.key for entry in trail]
                assert "sleeve_capital_inr" in keys
                assert "stop_pct" in keys
                row = await read_config(session, user_id)
                assert row.sleeve_capital_inr == Decimal("1500000.00")
                assert row.stop_pct == Decimal("11.00")
        finally:
            await session.close()
            await engine.dispose()

    async def test_only_a_job_may_move_the_dry_run_counter(self, vbt_url: str) -> None:
        """`02` §3.1 is a gate. It is moved by the evening job, with an audit row saying so."""
        session, engine = await _session(vbt_url)
        try:
            async with session.begin():
                user_id = await _ensure_user(session)
                await seed_vbt_config(session)
            async with session.begin():
                row = await record_system_change(
                    session,
                    user_id=user_id,
                    field="dry_run_sessions",
                    value=1,
                    changed_by="vbt-evening",
                    now=dt.datetime.now(tz=dt.UTC),
                )
                assert row.dry_run_sessions == 1
            async with session.begin():
                trail = await audit_trail(session, user_id=user_id)
                counted = [e for e in trail if e.key == "dry_run_sessions"]
                assert counted and counted[0].changed_by == "vbt-evening"

            with pytest.raises(ValueError, match="not a system-owned field"):
                async with session.begin():
                    await record_system_change(
                        session,
                        user_id=user_id,
                        field="stop_pct",
                        value=9,
                        changed_by="vbt-evening",
                        now=dt.datetime.now(tz=dt.UTC),
                    )
        finally:
            await session.close()
            await engine.dispose()
