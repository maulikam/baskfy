"""LV9 — Qullamaggie's exits on the TWT evening (``gates/live-9-qulla-exits.md`` Q3).

Maulik, 28 Sep 2026 (DECISIONS-LV LV9.0, TW20). ``run_twt_manage_qulla`` replaces the 20 %
high-water ratchet: it writes the partial, the breakeven raise and the MA-trail exit onto the
position; ``book_state`` reads them; ``exit_lines`` turns them into ``SELL_AT_OPEN`` and
``RAISE_GTT_STOP`` lines dated the session just closed, each naming its position.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa
from helpers import add_bar, make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession
from test_twt_evening import AS_OF, _breadth, _user

from baskfy_api.twt_sleeve import book_state
from baskfy_core.models import TwPlanLine, TwPosition
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG
from baskfy_core.twt.plan import LineKind, exit_lines
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks import twt as T
from baskfy_worker.tasks.twt_evening import run_twt_evening

pytestmark = [requires_db, pytest.mark.db]


def _sessions(count: int, end: dt.date = AS_OF) -> list[dt.date]:
    days: list[dt.date] = []
    day = end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= dt.timedelta(days=1)
    return sorted(days)


async def _held(  # noqa: PLR0913 - a held position is its closes, its dates and its entry
    session: AsyncSession,
    user_id: int,
    symbol: str,
    *,
    closes: dict[dt.date, str],
    entry_date: dt.date,
    entry: str = "100.00",
) -> tuple[int, TwPosition]:
    instrument_id = await make_instrument(session, symbol)
    for day, close in closes.items():
        await add_bar(session, instrument_id, day, close)
    stop = (Decimal(entry) * Decimal("0.8")).quantize(Decimal("0.01"))
    position = TwPosition(
        user_id=user_id,
        instrument_id=instrument_id,
        signal_date=entry_date,
        entry_date=entry_date,
        entry_avg=Decimal(entry),
        entry_adj_factor=Decimal(1),
        quantity_entered=300,
        quantity_open=300,
        initial_stop=stop,
        stop_price=stop,
        high_since=Decimal(entry),
        high_since_date=entry_date,
        gtt_id="GTT-1",
        gtt_trigger=stop,
        state="OPEN",
        simulated=True,
    )
    session.add(position)
    await session.flush()
    return instrument_id, position


class TestTheEveningWritesTheDecision:
    async def test_a_green_bar_three_sessions_in_queues_a_third_and_the_breakeven(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session, "q-partial")
        days = _sessions(60)
        closes = {day: ("110.00" if day == AS_OF else "100.00") for day in days}
        _, position = await _held(session, user_id, "RUNNER", closes=closes, entry_date=days[-4])

        report = await T.run_twt_manage_qulla(
            session, AS_OF, user_id=user_id, config=DEFAULT_TWT_CONFIG
        )

        assert (report.positions, report.partials, report.raises, report.exits) == (1, 1, 1, 0)
        await session.refresh(position)
        assert (position.partial_queued_for, position.partial_quantity) == (AS_OF, 100)
        assert position.next_trigger == Decimal("100.00") and position.next_trigger_for == AS_OF
        assert position.trail == "MA20"
        assert position.stop_price == Decimal("80.00"), "the stop in force moves only at the desk"
        assert position.quantity_open == 300, "the shares move only at the broker"

    async def test_a_close_below_the_trail_queues_the_remainder(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session, "q-trail")
        days = _sessions(60)
        closes = {day: ("100.00" if day == AS_OF else "120.00") for day in days}
        _, position = await _held(
            session, user_id, "FADING", closes=closes, entry_date=days[-15], entry="120.00"
        )
        position.partial_done = True
        position.stop_price = position.initial_stop = Decimal("90.00")
        position.gtt_trigger = Decimal("90.00")
        await session.flush()

        report = await T.run_twt_manage_qulla(
            session, AS_OF, user_id=user_id, config=DEFAULT_TWT_CONFIG
        )

        assert report.exits == 1 and report.partials == 0
        await session.refresh(position)
        assert (position.exit_queued_for, position.exit_reason_queued) == (AS_OF, "MA_TRAIL")

    async def test_tonights_decision_replaces_last_nights(self, session: AsyncSession) -> None:
        user_id = await _user(session, "q-stale")
        days = _sessions(60)
        closes = {day: "100.00" for day in days}  # flat and red-ish: nothing to do tonight
        _, position = await _held(session, user_id, "QUIET", closes=closes, entry_date=days[-10])
        position.partial_queued_for = days[-2]
        position.partial_quantity = 100
        await session.flush()

        await T.run_twt_manage_qulla(session, AS_OF, user_id=user_id, config=DEFAULT_TWT_CONFIG)

        row = (
            await session.execute(sa.select(TwPosition).where(TwPosition.id == position.id))
        ).scalar_one()
        assert (row.partial_queued_for, row.partial_quantity) == (None, None)

    async def test_a_young_history_holds_and_a_name_without_a_bar_is_counted(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session, "q-young")
        days = _sessions(60)
        closes = {day: ("80.00" if day == AS_OF else "100.00") for day in days[-6:]}
        _, position = await _held(session, user_id, "YOUNG", closes=closes, entry_date=days[-5])
        no_bar = await make_instrument(session, "SILENT")
        session.add(
            TwPosition(
                user_id=user_id,
                instrument_id=no_bar,
                signal_date=days[-5],
                entry_date=days[-5],
                entry_avg=Decimal(100),
                entry_adj_factor=Decimal(1),
                quantity_entered=10,
                quantity_open=10,
                initial_stop=Decimal(80),
                stop_price=Decimal(80),
                high_since=Decimal(100),
                high_since_date=days[-5],
                state="OPEN",
                simulated=True,
            )
        )
        await session.flush()

        report = await T.run_twt_manage_qulla(
            session, AS_OF, user_id=user_id, config=DEFAULT_TWT_CONFIG
        )

        assert report.without_a_bar == 1 and report.exits == 0 and report.partials == 0
        await session.refresh(position)
        assert position.exit_queued_for is None


class TestThePlanCarriesTheLines:
    async def test_the_book_state_and_the_plan_carry_the_partial_and_the_raise(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session, "q-plan")
        await _breadth(session, user_id)
        days = _sessions(60)
        closes = {day: ("110.00" if day == AS_OF else "100.00") for day in days}
        _, position = await _held(session, user_id, "RUNNER", closes=closes, entry_date=days[-4])
        await T.run_twt_manage_qulla(session, AS_OF, user_id=user_id, config=DEFAULT_TWT_CONFIG)

        book = await book_state(session, user_id=user_id, as_of=AS_OF)
        lines = exit_lines(book, AS_OF)

        kinds = {(line.kind, line.quantity, line.position_id) for line in lines}
        assert kinds == {
            (LineKind.SELL_AT_OPEN, 100, position.id),
            (LineKind.RAISE_GTT_STOP, 300, position.id),
        }
        assert exit_lines(book, AS_OF + dt.timedelta(days=1)) == [], "dated the session just closed"

    async def test_the_stored_plan_line_names_its_position(self, session: AsyncSession) -> None:
        user_id = await _user(session, "q-stored")
        await _breadth(session, user_id)
        days = _sessions(60)
        closes = {day: ("110.00" if day == AS_OF else "100.00") for day in days}
        _, position = await _held(session, user_id, "RUNNER", closes=closes, entry_date=days[-4])
        await T.run_twt_manage_qulla(session, AS_OF, user_id=user_id, config=DEFAULT_TWT_CONFIG)

        await run_twt_evening(session, StepOutcome(), AS_OF, user_id=user_id)

        rows = (await session.execute(sa.select(TwPlanLine.kind, TwPlanLine.position_id))).all()
        assert {(kind, pid) for kind, pid in rows} >= {
            ("SELL_AT_OPEN", position.id),
            ("RAISE_GTT_STOP", position.id),
        }


class TestTheRatchetIsSidelined:
    async def test_the_twenty_percent_ratchet_does_not_run_while_the_rule_is_on(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(*_: object, **__: object) -> object:
            raise AssertionError("the ratchet ran")

        monkeypatch.setattr(T, "run_twt_ratchet", boom)
        user_id = await _user(session, "q-noratchet")
        days = _sessions(60)
        closes = {day: "100.00" for day in days}
        await _held(session, user_id, "ANY", closes=closes, entry_date=days[-10])
        # the detector's tail calls the management; with no signals it still manages the book
        await T.run_detect_twt(session, StepOutcome(), AS_OF, user_id=user_id)
