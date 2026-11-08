"""LV10 — the builders hand the planners per-name entry counts and every exit line names its
position (``gates/live-10-pyramiding.md`` P2). Maulik, 28 Sep 2026 (DECISIONS-LV LV9.0 (3))."""

from __future__ import annotations

from decimal import Decimal

import pytest
import sqlalchemy as sa
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession
from test_swing_eod import AS_OF as SWING_AS_OF
from test_swing_eod import _bars as swing_bars
from test_swing_eod import _position as swing_position
from test_swing_eod import _sessions_before
from test_swing_eod import _user as swing_user
from test_twt_evening import _breadth, _position, _signal
from test_twt_evening import _user as twt_user
from test_vbt_evening import SESSIONS, _calendar, _evening
from test_vbt_evening import _signal as vbt_signal
from test_vbt_evening import _user as vbt_user

from baskfy_api.twt_sleeve import book_state
from baskfy_core.models import VbPlanLine, VbPosition
from baskfy_core.swing.plan import LineKind as SwingKind
from baskfy_core.vbt.plan import LineKind as VbtKind
from baskfy_worker.tasks.swing_eod import manage_open_positions, sleeve_account

pytestmark = [requires_db, pytest.mark.db]


class TestTwt:
    async def test_the_book_state_counts_entries_per_name(self, session: AsyncSession) -> None:
        user_id = await twt_user(session, "counts")
        await _breadth(session, user_id)
        instrument_id = await _signal(session, user_id, "TWTCO")
        await _position(session, user_id, instrument_id)
        await _position(session, user_id, instrument_id)

        book = await book_state(session, user_id=user_id, as_of=_signal.__globals__["AS_OF"])

        assert book.open_entry_counts == {instrument_id: 2}
        assert book.slots_taken == 2 and book.entries_in(instrument_id) == 2


class TestVbt:
    async def test_one_held_is_a_second_limit_and_the_line_count_carries(
        self, session: AsyncSession
    ) -> None:
        user_id = await vbt_user(session)
        await _calendar(session, user_id)
        instrument_id = await vbt_signal(session, user_id, "HELDCO", limit="100.00")
        session.add(
            VbPosition(
                user_id=user_id,
                instrument_id=instrument_id,
                entry_date=SESSIONS[0],
                entry_avg=Decimal("90.00"),
                quantity_entered=100,
                quantity_open=100,
                initial_stop=Decimal("80.00"),
                stop_price=Decimal("80.00"),
                state="OPEN",
                gtt_id="gtt-held",
            )
        )
        await session.flush()

        report = await _evening(session, user_id)

        assert report is not None and report.entries == 1
        line = (
            await session.execute(
                sa.select(VbPlanLine).where(VbPlanLine.kind == VbtKind.PLACE_LIMIT.value)
            )
        ).scalar_one()
        assert line.instrument_id == instrument_id


class TestSwing:
    async def test_the_account_counts_entries_and_each_exit_line_names_its_position(
        self, session: AsyncSession
    ) -> None:
        user_id = await swing_user(session)
        dates = await _sessions_before(session, SWING_AS_OF, 40)
        instrument_id = await make_instrument(session, "FLAGCO")
        closes = [100.0] * (len(dates) - 4) + [100.0, 104.0, 107.0, 110.0]
        await swing_bars(session, instrument_id, dates, closes)
        first = await swing_position(
            session, user_id=user_id, instrument_id=instrument_id, entry_date=dates[-4]
        )
        second = await swing_position(
            session,
            user_id=user_id,
            instrument_id=instrument_id,
            entry_date=dates[-4],
            quantity=150,
        )

        account = await sleeve_account(session, user_id=user_id, config_row=None)
        lines, _naked, managed = await manage_open_positions(
            session, user_id=user_id, on=SWING_AS_OF
        )

        assert account.open_entry_counts == {"FLAGCO": 2} and account.open_count == 2
        assert managed == 2
        sells = [
            (line.quantity, line.position_id)
            for line in lines
            if line.kind is SwingKind.SELL_AT_OPEN
        ]
        assert sorted(sells) == [(50, second), (100, first)]
        assert all(line.position_id in (first, second) for line in lines)
