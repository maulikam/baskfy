"""``baskfy_api.seed twt --capital`` — the surface `NEEDS-MAULIK.md` T3 said did not exist.

T3 records the sleeve's capital as "your keystroke" and, in the same entry, that **there was
nowhere to type it**: no `PATCH /api/v1/twt/config`, no `me/twt` form, and a `--capital` flag the
seeder refused for every command but `swing`. The only thing that worked was a direct `UPDATE` on
`tw_config`, which is the worse option because it bypasses the audit — the change arrives with no
author and no trail, on a sleeve whose whole design is that a person decided each number.

So what is asserted here is not "the number can be written". It is that the only way to write it is
the *same* write the settings form makes: the engine's bounds first, one `tw_config_audit` row per
field that actually moves, and nothing at all on a re-run. A second path into `tw_config` without a
trail would pass a test that only checked the value.

**And the safety rail is asserted too** (`TestTheZeroSurvives`): funding the sleeve is something a
person opts into with an explicit flag. Seeding without one still writes ₹0, which is what makes
`gates/twt-root.md` R11 true of the changed seeder and not only of the old one.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from screener_helpers import requires_db, seeded_database
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from baskfy_api import seed as seed_module
from baskfy_api.seed import main, seed_twt_config, set_twt_sleeve
from baskfy_api.twt_settings import read_config
from baskfy_core.models import AppUser, TwConfigAudit


class TestTheCommandLine:
    def test_capital_now_reaches_the_twt_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The regression this file exists for.

        Before TW11 the parser answered `seed twt --capital 2500000` with exit 2 and the message
        "--capital/--risk apply to the `swing` command only", which is the whole of why
        `NEEDS-MAULIK.md` T3 could call the capital "your keystroke" and, four lines later, say
        there was nowhere to type it.

        `_run` is replaced so no database is touched: what is asserted is that the flag survives
        argument parsing **and arrives at the seeder as a number**. A test that only checked the
        exit code would pass just as well if the capital were parsed and then dropped.
        """
        seen: dict[str, object] = {}

        async def fake_run(
            command: str,
            database_url: str | None,
            *,
            capital: Decimal | None = None,
            risk: Decimal | None = None,
        ) -> dict[str, int]:
            seen.update(command=command, capital=capital, risk=risk)
            return {}

        monkeypatch.setattr(seed_module, "_run", fake_run)
        assert main(["twt", "--capital", "2500000"]) == 0
        assert seen == {"command": "twt", "capital": Decimal("2500000"), "risk": None}

    def test_capital_is_still_refused_where_there_is_no_sleeve(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Widening the flag must not make it silently ignorable everywhere else."""
        with pytest.raises(SystemExit) as exc:
            main(["reference", "--capital", "2500000"])
        assert exc.value.code == 2
        assert "--capital" in capsys.readouterr().err

    def test_risk_stays_a_swing_idea(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`tw_config` has no risk-per-trade column: this book sizes by slot, not by stop."""
        with pytest.raises(SystemExit) as exc:
            main(["twt", "--risk", "0.5"])
        assert exc.value.code == 2
        assert "--risk" in capsys.readouterr().err


class TestTheZeroSurvives:
    def test_the_seeder_still_writes_an_explicit_zero(self) -> None:
        """`gates/twt-root.md` R11's `capital_seed=1`, asserted here rather than only by grep.

        The safety rail is that creation writes ₹0 and only an explicit `--capital` moves it. If
        someone ever wires `set_twt_sleeve` into the `twt` branch unconditionally, this fails.
        """
        source = inspect.getsource(seed_module.seed_twt_config)
        assert 'sleeve_capital_inr=Decimal("0")' in source
        run = inspect.getsource(seed_module._run)
        twt_branch = run[run.index('if command in ("all", "twt")') :]
        twt_branch = twt_branch[: twt_branch.index('if command in ("all", "market")')]
        assert "if capital is not None:" in twt_branch, "the sleeve must only be funded on request"


@pytest.fixture(scope="module")
def twt_url() -> str:
    return seeded_database()


async def _session(url: str) -> tuple[AsyncSession, AsyncEngine]:
    engine = create_async_engine(url)
    return async_sessionmaker(engine, expire_on_commit=False)(), engine


async def _ensure_user(session: AsyncSession) -> int:
    existing = (
        await session.execute(sa.select(AppUser.id).order_by(AppUser.id).limit(1))
    ).scalar_one_or_none()
    if existing is not None:
        return int(existing)
    user = AppUser(public_id="tw11-test", email="tw11@example.test")
    session.add(user)
    await session.flush()
    return int(user.id)


async def _audit_count(session: AsyncSession, user_id: int) -> int:
    return int(
        (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(TwConfigAudit)
                .where(TwConfigAudit.user_id == user_id)
            )
        ).scalar_one()
    )


@requires_db
@pytest.mark.asyncio
class TestSetTwtSleeve:
    async def test_it_funds_the_sleeve_through_the_audited_path_and_re_runs_are_silent(
        self, twt_url: str
    ) -> None:
        session, engine = await _session(twt_url)
        try:
            async with session.begin():
                user_id = await _ensure_user(session)
                assert await seed_twt_config(session) == 1
                before = await _audit_count(session, user_id)
                assert await set_twt_sleeve(session, capital_inr=Decimal("2500000")) == 1
            session.expire_all()
            async with session.begin():
                row = await read_config(session, user_id)
                assert row.sleeve_capital_inr == Decimal("2500000.00")
                assert row.updated_by == "seed"
                # The trail is the point of the function. A raw UPDATE would pass every
                # assertion above this line and none below it.
                assert await _audit_count(session, user_id) == before + 1
                trail = (
                    await session.execute(
                        sa.select(
                            TwConfigAudit.key, TwConfigAudit.new_value, TwConfigAudit.changed_by
                        )
                        .where(TwConfigAudit.user_id == user_id)
                        .order_by(TwConfigAudit.id.desc())
                        .limit(1)
                    )
                ).one()
                assert tuple(trail) == ("sleeve_capital_inr", "2500000.00", "seed")

            # House rule 7: a deploy runs this every time and the second run changes nothing.
            # `2500000` against a stored `2500000.00` is the case the quantise in the function
            # exists for; without it this writes a second audit row every deploy.
            async with session.begin():
                assert await set_twt_sleeve(session, capital_inr=Decimal("2500000")) == 1
            session.expire_all()
            async with session.begin():
                assert await _audit_count(session, user_id) == before + 1
        finally:
            await session.close()
            await engine.dispose()

    async def test_twt_sleeve_rejects_a_negative_capital_and_writes_nothing(
        self, twt_url: str
    ) -> None:
        """A negative capital is nonsense at any bound, so it is refused before anything is
        written — the engine's limit, not the server's ceiling."""
        session, engine = await _session(twt_url)
        try:
            async with session.begin():
                user_id = await _ensure_user(session)
                await seed_twt_config(session)
                row = await read_config(session, user_id)
                capital_before = row.sleeve_capital_inr
                audits_before = await _audit_count(session, user_id)
            with pytest.raises(ValidationError):
                async with session.begin():
                    await set_twt_sleeve(session, capital_inr=Decimal("-1"))
            session.expire_all()
            async with session.begin():
                row = await read_config(session, user_id)
                assert row.sleeve_capital_inr == capital_before
                assert await _audit_count(session, user_id) == audits_before
        finally:
            await session.close()
            await engine.dispose()

    async def test_nothing_to_set_touches_nothing(self, twt_url: str) -> None:
        session, engine = await _session(twt_url)
        try:
            async with session.begin():
                user_id = await _ensure_user(session)
                await seed_twt_config(session)
                before = await _audit_count(session, user_id)
                assert await set_twt_sleeve(session) == 1
                assert await _audit_count(session, user_id) == before
        finally:
            await session.close()
            await engine.dispose()
