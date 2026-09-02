"""``baskfy_api.seed swing --capital --risk`` — the deploy's stand-in for PATCH /swing/config.

The box has no session token at deploy time, so the sleeve (MD1: ₹25,00,000) and the risk per
trade (MD2: 0.5 %) are written by the seed CLI. What is asserted is that it is the *same* write
the settings form makes — ceilings first, one audit row per field that moves, nothing on a
re-run — and not a second path that could put a number in ``sw_config`` without a trail.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import sqlalchemy as sa
from screener_helpers import requires_db, seeded_database
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from baskfy_api.problems import Problem
from baskfy_api.seed import main, seed_swing_config, set_swing_sleeve
from baskfy_api.swing_settings import read_config
from baskfy_core.models import AppUser, SwConfigAudit


class TestTheCommandLine:
    def test_capital_and_risk_belong_to_the_swing_command_only(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`seed reference --capital 1` must not be answered by silently ignoring the number."""
        with pytest.raises(SystemExit) as exc:
            main(["reference", "--capital", "2500000"])
        assert exc.value.code == 2
        assert "swing" in capsys.readouterr().err


@pytest.fixture(scope="module")
def swing_url() -> str:
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
    user = AppUser(public_id="sw13-test", email="sw13@example.test")
    session.add(user)
    await session.flush()
    return int(user.id)


async def _audit_count(session: AsyncSession, user_id: int) -> int:
    return int(
        (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(SwConfigAudit)
                .where(SwConfigAudit.user_id == user_id)
            )
        ).scalar_one()
    )


@requires_db
@pytest.mark.asyncio
class TestSetSwingSleeve:
    async def test_it_writes_both_numbers_through_the_audited_path_and_re_runs_are_silent(
        self, swing_url: str
    ) -> None:
        session, engine = await _session(swing_url)
        try:
            async with session.begin():
                user_id = await _ensure_user(session)
                assert await seed_swing_config(session) == 1
                before = await _audit_count(session, user_id)
                assert (
                    await set_swing_sleeve(
                        session, capital_inr=Decimal("2500000"), risk_pct=Decimal("0.5")
                    )
                    == 1
                )
            session.expire_all()
            async with session.begin():
                row = await read_config(session, user_id)
                assert row.sleeve_capital_inr == Decimal("2500000.00")
                assert row.risk_per_trade_pct == Decimal("0.500")
                assert row.updated_by == "seed"
                # One row per field that moved. The seed's default risk is already 0.500, so
                # only the capital leaves a trail here — that is the settings form's rule, and
                # the point of going through it.
                assert await _audit_count(session, user_id) == before + 1
                trail = (
                    await session.execute(
                        sa.select(
                            SwConfigAudit.key, SwConfigAudit.new_value, SwConfigAudit.changed_by
                        )
                        .where(SwConfigAudit.user_id == user_id)
                        .order_by(SwConfigAudit.id.desc())
                        .limit(1)
                    )
                ).one()
                assert tuple(trail) == ("sleeve_capital_inr", "2500000.00", "seed")

            # House rule 7: the deploy runs this every time; the second run changes nothing.
            async with session.begin():
                assert (
                    await set_swing_sleeve(
                        session, capital_inr=Decimal("2500000"), risk_pct=Decimal("0.5")
                    )
                    == 1
                )
            session.expire_all()
            async with session.begin():
                assert await _audit_count(session, user_id) == before + 1
        finally:
            await session.close()
            await engine.dispose()

    async def test_a_risk_above_the_server_ceiling_is_refused_and_writes_nothing(
        self, swing_url: str
    ) -> None:
        """The ceiling is 1.0 % (PACK.9). `--risk 5` from a deploy script is the same mistake as
        `5` in the form, and gets the same 422, with the capital in the same call untouched."""
        session, engine = await _session(swing_url)
        try:
            async with session.begin():
                user_id = await _ensure_user(session)
                await seed_swing_config(session)
                row = await read_config(session, user_id)
                capital_before = row.sleeve_capital_inr
            with pytest.raises(Problem):
                async with session.begin():
                    await set_swing_sleeve(session, capital_inr=Decimal("1"), risk_pct=Decimal("5"))
            session.expire_all()
            async with session.begin():
                row = await read_config(session, user_id)
                assert row.sleeve_capital_inr == capital_before
        finally:
            await session.close()
            await engine.dispose()

    async def test_nothing_to_set_touches_nothing(self, swing_url: str) -> None:
        session, engine = await _session(swing_url)
        try:
            async with session.begin():
                user_id = await _ensure_user(session)
                await seed_swing_config(session)
                before = await _audit_count(session, user_id)
                assert await set_swing_sleeve(session) == 1
                assert await _audit_count(session, user_id) == before
        finally:
            await session.close()
            await engine.dispose()
