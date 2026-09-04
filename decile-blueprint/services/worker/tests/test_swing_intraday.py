"""The plan rebuilt against the session in progress (4 Sep 2026).

Maulik: "the scans we are doing are not the live data, they would be the past data." The scan
itself was live since M85; nothing downstream of it ran. On the box that showed as thirteen
provisional setups at 14:08 and exactly one executable line, built at 09:16 from the previous
close. These tests pin the fix and — more importantly — pin what the fix must NOT do: an
intraday plan must not settle the ladder, must not count a session, and must not invent a gate.

The fixtures are `test_swing_premarket`'s, deliberately: an intraday plan and a morning plan are
the same build against a different session, so proving it on a different world would prove less.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession
from test_swing_premarket import (
    SESSION,
    _bars,
    _market,
    _sessions_before,
    _setup,
    _user,
    _watch,
)

from baskfy_core.models import SwConfig, SwPlan, SwPlanLine, SwSession
from baskfy_core.models.swing import SW_PLAN_SOURCES
from baskfy_worker.tasks.swing_intraday import INTRADAY, build_intraday_plan

pytestmark = requires_db

NOW = dt.datetime(2026, 8, 19, 13, 42, tzinfo=dt.UTC)


def test_intraday_is_a_known_plan_source() -> None:
    """Migration 0034 widened the CHECK; the model has to agree or every write is refused."""
    assert INTRADAY in SW_PLAN_SOURCES


@pytest.mark.db
class TestTheIntradayPlan:
    async def test_a_setup_detected_today_becomes_a_line_you_can_confirm_today(
        self, session: AsyncSession
    ) -> None:
        """The whole point. The setup and the market row are dated **today**, not the last
        close, and the plan that comes back is for today with a real BUY line on it."""
        user_id = await _user(session)
        flag = await make_instrument(session, "GOODFLAG")
        last = (await _sessions_before(session, SESSION, 1))[0]
        await _bars(session, flag, [last])
        await _market(session, user_id=user_id, on=SESSION)
        await _setup(session, user_id=user_id, instrument_id=flag, on=SESSION)
        await _watch(session, user_id=user_id, instrument_id=flag, on=SESSION)

        report = await build_intraday_plan(session, user_id=user_id, on=SESSION, now=NOW)

        assert report.entry_lines == 1
        assert report.plan_id is not None
        plan = (await session.execute(sa.select(SwPlan))).scalar_one()
        assert plan.source == INTRADAY
        assert plan.as_of == SESSION
        # Non-negotiable 1 is untouched: an intraday plan expires like any other.
        assert (plan.expires_at - plan.built_at) == dt.timedelta(minutes=30)
        line = (await session.execute(sa.select(SwPlanLine))).scalar_one()
        assert line.kind == "BUY_ON_TRIGGER"
        assert line.quantity > 0, "a line nobody can size is not a line anybody can buy"

    async def test_without_a_market_row_it_refuses_rather_than_inventing_a_gate(
        self, session: AsyncSession
    ) -> None:
        """A gate is a measurement. No scan today means nothing measured today, and planning
        against a tape nobody measured is worse than not planning — `build_morning_plan`
        refuses for the same reason."""
        user_id = await _user(session)
        flag = await make_instrument(session, "GOODFLAG")
        await _watch(session, user_id=user_id, instrument_id=flag, on=SESSION)

        report = await build_intraday_plan(session, user_id=user_id, on=SESSION, now=NOW)

        assert report.plan_id is None
        assert report.skipped_reason is not None
        assert "sw_market_daily" in report.skipped_reason
        planned = (
            await session.execute(sa.select(sa.func.count()).select_from(SwPlan))
        ).scalar_one()
        assert planned == 0

    async def test_it_never_settles_the_ladder_or_counts_a_session(
        self, session: AsyncSession
    ) -> None:
        """A10: the ladder settles once a day, on real closes, in the evening. A plan rebuilt
        four times an afternoon must not move the rung four times, and a session that has not
        closed has not happened."""
        user_id = await _user(session)
        flag = await make_instrument(session, "GOODFLAG")
        last = (await _sessions_before(session, SESSION, 1))[0]
        await _bars(session, flag, [last])
        await _market(session, user_id=user_id, on=SESSION)
        await _setup(session, user_id=user_id, instrument_id=flag, on=SESSION)
        await _watch(session, user_id=user_id, instrument_id=flag, on=SESSION)
        before = (
            await session.execute(sa.select(SwConfig).where(SwConfig.user_id == user_id))
        ).scalar_one()
        rung_before = before.exposure_level
        first_live_before = before.first_live_sessions_left

        for _ in range(3):
            await build_intraday_plan(session, user_id=user_id, on=SESSION, now=NOW)

        after = (
            await session.execute(sa.select(SwConfig).where(SwConfig.user_id == user_id))
        ).scalar_one()
        assert after.exposure_level == rung_before, "three rebuilds moved the rung"
        assert after.first_live_sessions_left == first_live_before
        sessions = (
            await session.execute(sa.select(sa.func.count()).select_from(SwSession))
        ).scalar_one()
        assert sessions == 0, "a session that has not closed was counted"

    async def test_rebuilding_leaves_one_plan_per_build_and_the_latest_is_the_one_to_read(
        self, session: AsyncSession
    ) -> None:
        """Each rebuild is its own row — the journal keeps what was proposed at 11:00 and at
        13:42 — and the newest `built_at` is what the desk renders."""
        user_id = await _user(session)
        flag = await make_instrument(session, "GOODFLAG")
        last = (await _sessions_before(session, SESSION, 1))[0]
        await _bars(session, flag, [last])
        await _market(session, user_id=user_id, on=SESSION)
        await _setup(session, user_id=user_id, instrument_id=flag, on=SESSION)
        await _watch(session, user_id=user_id, instrument_id=flag, on=SESSION)

        first = await build_intraday_plan(session, user_id=user_id, on=SESSION, now=NOW)
        later = await build_intraday_plan(
            session, user_id=user_id, on=SESSION, now=NOW + dt.timedelta(hours=1)
        )

        assert first.plan_id != later.plan_id
        rows = (
            (await session.execute(sa.select(SwPlan).order_by(SwPlan.built_at.desc())))
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert str(rows[0].plan_id) == later.plan_id
