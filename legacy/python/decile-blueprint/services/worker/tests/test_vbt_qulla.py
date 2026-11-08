"""LV9 — Qullamaggie's exits on the VBT evening (``gates/live-9-qulla-exits.md`` Q4).

Maulik, 28 Sep 2026 (DECISIONS-LV LV9.0, VB17). The stop and the no-bar write-off are still the
sleeve's own ``manage``; the working exit is the shared rule: a third into strength on bars 3-5,
the stop to breakeven, the remainder on a close below the trail MA. The 21-EMA exit no longer
fires while ``qulla_exits`` is on; with it off, the tested EMA exit is exactly what it was.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession
from test_vbt_evening import AS_OF, SESSIONS, _bar, _calendar, _evening, _history, _lines, _user

from baskfy_core.exits.qulla import OhlcBar
from baskfy_core.models import VbPosition
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG
from baskfy_core.vbt.exits import Action, Bar, ExitReason
from baskfy_core.vbt.plan import LineKind
from baskfy_worker.tasks.vbt_evening import managed_actions

pytestmark = [requires_db, pytest.mark.db]


async def _position(
    session: AsyncSession, user_id: int, instrument_id: int, *, entry_date: dt.date, entry: str
) -> VbPosition:
    row = VbPosition(
        user_id=user_id,
        instrument_id=instrument_id,
        entry_date=entry_date,
        entry_avg=Decimal(entry),
        quantity_entered=300,
        quantity_open=300,
        initial_stop=Decimal(entry) * Decimal("0.88"),
        stop_price=Decimal(entry) * Decimal("0.88"),
        state="OPEN",
        gtt_id="gtt-1",
        gtt_trigger=Decimal(entry) * Decimal("0.88"),
    )
    session.add(row)
    await session.flush()
    return row


def _flat_then(closes: dict[dt.date, str], default: str = "100.00") -> dict[dt.date, str]:
    return {day: closes.get(day, default) for day in _history(60)}


class TestThePartialAndTheBreakeven:
    async def test_a_green_bar_three_sessions_after_entry_sells_a_third_and_raises_to_breakeven(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await _calendar(session, user_id)
        instrument_id = await make_instrument(session, "RUNNER")
        days = _history(60)
        entry_day = days[-4]  # bar 0; AS_OF is bar 3
        for day in days:
            session.add(_bar(instrument_id, day, "110.00" if day == AS_OF else "100.00"))
        await session.flush()
        position = await _position(
            session, user_id, instrument_id, entry_date=entry_day, entry="100.00"
        )

        report = await _evening(session, user_id)

        assert report is not None and report.sells == 1
        sell = (await _lines(session, LineKind.SELL_AT_OPEN))[0]
        assert (sell.quantity, sell.reason, sell.position_id) == (100, "PARTIAL", position.id)
        raise_ = (await _lines(session, LineKind.RAISE_GTT_STOP))[0]
        assert raise_.stop_price == Decimal("100.00") and raise_.position_id == position.id
        await session.refresh(position)
        assert position.trail == "MA20"
        assert position.exit_queued_for is None, "a partial is not a full exit"

    async def test_one_r_showing_outside_the_window_raises_without_selling(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await _calendar(session, user_id)
        instrument_id = await make_instrument(session, "STEADY")
        days = _history(60)
        for day in days:
            session.add(_bar(instrument_id, day, "114.00" if day == AS_OF else "100.00"))
        await session.flush()
        await _position(session, user_id, instrument_id, entry_date=days[-2], entry="100.00")

        report = await _evening(session, user_id)

        assert report is not None and report.sells == 0
        assert len(await _lines(session, LineKind.RAISE_GTT_STOP)) == 1

    async def test_a_young_history_holds(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _calendar(session, user_id)
        instrument_id = await make_instrument(session, "YOUNG")
        days = _history(60)
        for day in days[-8:]:
            session.add(_bar(instrument_id, day, "70.00" if day == AS_OF else "100.00"))
        await session.flush()
        await _position(session, user_id, instrument_id, entry_date=days[-7], entry="100.00")

        report = await _evening(session, user_id)

        # the close 70 is under the 88 stop: that is the stop's business (STOPPED_OUT stays with
        # ``manage``), not a trail sale — no SELL_AT_OPEN from the rule
        assert report is not None and report.sells == 0


class TestTheEmaExitIsGoneWhileTheRuleIsOn:
    def _row(self) -> VbPosition:
        return VbPosition(
            id=9,
            user_id=1,
            instrument_id=5,
            entry_date=SESSIONS[0],
            entry_avg=Decimal("100"),
            quantity_entered=300,
            quantity_open=300,
            initial_stop=Decimal("88"),
            stop_price=Decimal("88"),
            state="OPEN",
            gtt_id="g",
            gtt_trigger=Decimal("88"),
            partial_done=False,
            trail=None,
            exit_queued_for=None,
        )

    def _bar_below_ema_above_ma20(self) -> Bar:
        # history: 20 closes at 100 then today 95; MA20 (inc. today) = 99.75, EMA21 ≈ 99.5 — the
        # close is below both, so both rules would sell; use a longer flat run at 90 to separate:
        # MA20 ≈ 90.25 (close 95 above it), EMA21 (given) 96 above the close.
        history = (
            *tuple(
                OhlcBar(
                    dt.date(2026, 7, 1) + dt.timedelta(days=i),
                    Decimal(90),
                    Decimal(91),
                    Decimal(89),
                    Decimal(90),
                )
                for i in range(30)
            ),
            OhlcBar(AS_OF, Decimal(95), Decimal(96), Decimal(94), Decimal(95)),
        )
        return Bar(
            session=AS_OF,
            open=Decimal(95),
            high=Decimal(96),
            low=Decimal(94),
            close=Decimal(95),
            ema_exit=Decimal(96),
            history=history,
        )

    def test_with_the_rule_on_a_close_under_the_ema_but_over_the_ma_holds(self) -> None:
        actions = managed_actions(
            [self._row()],
            {5: self._bar_below_ema_above_ma20()},
            {5: "X"},
            DEFAULT_VBT_CONFIG,
            {},
            {9: 12},
        )
        assert actions[0][3].action is Action.HOLD and actions[0][4] == 9

    def test_with_the_rule_off_the_tested_ema_exit_is_exactly_what_it_was(self) -> None:
        legacy = dataclasses.replace(
            DEFAULT_VBT_CONFIG,
            exits=dataclasses.replace(DEFAULT_VBT_CONFIG.exits, qulla_exits=False),
        )
        actions = managed_actions(
            [self._row()], {5: self._bar_below_ema_above_ma20()}, {5: "X"}, legacy, {}, {9: 12}
        )
        assert (
            actions[0][3].action is Action.QUEUE_SELL_AT_OPEN
            and actions[0][3].reason is ExitReason.EMA_EXIT
        )

    def test_the_stop_traded_through_is_still_the_stop_rules(self) -> None:
        bar = self._bar_below_ema_above_ma20()
        low = dataclasses.replace(
            bar,
            low=Decimal(80),
            history=(
                *bar.history[:-1],
                OhlcBar(AS_OF, Decimal(95), Decimal(96), Decimal(80), Decimal(95)),
            ),
        )
        actions = managed_actions(
            [self._row()], {5: low}, {5: "X"}, DEFAULT_VBT_CONFIG, {}, {9: 12}
        )
        assert actions[0][3].action is Action.STOPPED_OUT
