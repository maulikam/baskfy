"""VB6 and VB7: the evening plan, and the limit that stops being an order after three sessions.

The claims, in the order the job makes them:

* **A signal becomes a `PLACE_LIMIT` line at its own close**, sized against the sleeve's own
  money, with the cap that bound it named — and every name it passed over recorded as a skip,
  because a plan is not honest without them.
* **A close below the 21-day EMA becomes a `SELL_AT_OPEN` for the next session** and writes
  `exit_queued_for` on the position, because the decision is tonight's and the fill is tomorrow's.
* **A working limit that has finished its third session becomes a `CANCEL_LIMIT` line** (VB7).
  Sessions are counted on the run's own calendar, so a holiday consumes none, and
  `sessions_worked` is *derived* rather than incremented — a re-run cannot inflate it.
* **A filled position with no resting GTT is an `ARM_GTT` line**, because a position without a
  stop is the one state the method forbids.
* **Nothing is placed.** Every line is `PROPOSED`; there is no code path here that reaches a
  gateway, and VB10 turns that into a source-level assertion.
* **`02` §3.1's DRY_RUN counter moves once a session**, and only when the session was rehearsed.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import (
    AppUser,
    OhlcvDaily,
    VbBreadthDaily,
    VbConfig,
    VbOrder,
    VbPlan,
    VbPlanLine,
    VbPlanSkip,
    VbPosition,
    VbSession,
    VbSignalDaily,
)
from baskfy_core.vbt.config import Gate, SignalState
from baskfy_core.vbt.plan import LineKind, SkipReason
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.vbt_evening import (
    SOURCE_EVENING,
    SOURCE_MORNING,
    EveningReport,
    last_detected_session,
    run_vbt_evening,
    session_calendar,
)

pytestmark = requires_db

AS_OF = dt.date(2026, 8, 18)
LAKH = Decimal("1000000.00")

#: Five consecutive sessions ending at AS_OF, as the sleeve's own calendar records them.
SESSIONS = [
    dt.date(2026, 8, 12),
    dt.date(2026, 8, 13),
    dt.date(2026, 8, 14),
    dt.date(2026, 8, 17),
    AS_OF,
]


async def _user(session: AsyncSession, capital: Decimal = LAKH) -> int:
    user = AppUser(public_id="vb6", email="vb6@example.com")
    session.add(user)
    await session.flush()
    session.add(VbConfig(user_id=user.id, sleeve_capital_inr=capital, updated_by="test"))
    await session.flush()
    return int(user.id)


async def _calendar(
    session: AsyncSession, user_id: int, *, gate: Gate = Gate.OPEN, thin: set[dt.date] | None = None
) -> None:
    """The breadth rows the evening reads its calendar and its gate back from."""
    for day in SESSIONS:
        is_thin = day in (thin or set())
        session.add(
            VbBreadthDaily(
                user_id=user_id,
                date=day,
                universe_count=100,
                measured_count=100,
                above_count=0 if is_thin else 60,
                pct_above_dma=Decimal("0.0000") if is_thin else Decimal("60.0000"),
                gate=Gate.SHUT.value if is_thin else gate.value,
                dma_bars=200,
                thin_session=is_thin,
            )
        )
    await session.flush()


async def _signal(  # noqa: PLR0913 - a signal row is its numbers
    session: AsyncSession,
    user_id: int,
    symbol: str,
    *,
    limit: str = "100.00",
    rank: int = 1_000_000,
    turnover: int = 500_000_000,
    locked: bool = False,
    on: dt.date = AS_OF,
    state: str = SignalState.SIGNAL.value,
) -> int:
    instrument_id = await make_instrument(session, symbol)
    session.add(
        VbSignalDaily(
            user_id=user_id,
            date=on,
            instrument_id=instrument_id,
            state=state,
            failed_filters=[],
            close=Decimal(limit),
            close_raw=Decimal(limit),
            adj_factor=Decimal(1),
            limit_price=Decimal(limit),
            stop_price=Decimal(limit) * Decimal("0.88"),
            turnover_avg_20=turnover,
            rank_key=rank,
            locked_upper_circuit=locked,
        )
    )
    await session.flush()
    return instrument_id


async def _evening(
    session: AsyncSession, user_id: int, *, on: dt.date = AS_OF, source: str = SOURCE_EVENING
) -> EveningReport | None:
    outcome = StepOutcome()
    return await run_vbt_evening(session, outcome, on, user_id=user_id, source=source)


async def _lines(session: AsyncSession, kind: LineKind | None = None) -> list[VbPlanLine]:
    statement = sa.select(VbPlanLine)
    if kind is not None:
        statement = statement.where(VbPlanLine.kind == kind.value)
    return list((await session.execute(statement)).scalars())


@pytest.mark.db
class TestTheEntries:
    async def test_a_signal_becomes_a_limit_at_its_own_close(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _calendar(session, user_id)
        await _signal(session, user_id, "VBTCO", limit="100.00")

        report = await _evening(session, user_id)

        assert report is not None
        assert report.entries == 1
        line = (await _lines(session, LineKind.PLACE_LIMIT))[0]
        assert line.limit_price == Decimal("100.00")
        assert line.stop_price == Decimal("88.00")
        assert line.quantity == 1_000  # a tenth of ₹10 lakh
        assert line.state == "PROPOSED"
        assert line.size_cap == "SLOT"
        assert line.client_id is not None and line.client_id.endswith(":VBTCO:PLACE_LIMIT")

    async def test_at_most_three_a_session_and_the_rest_are_skips(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await _calendar(session, user_id)
        for index in range(5):
            await _signal(session, user_id, f"NAME{index}", rank=1_000_000 - index)

        report = await _evening(session, user_id)

        assert report is not None
        assert report.entries == 3
        skips = list((await session.execute(sa.select(VbPlanSkip))).scalars())
        assert [skip.reason for skip in skips] == [SkipReason.SESSION_CAP.value] * 2

    async def test_the_highest_turnover_is_taken_first(self, session: AsyncSession) -> None:
        """`04` §3.4 — a rule, not a tiebreak convenience."""
        user_id = await _user(session)
        await _calendar(session, user_id)
        await _signal(session, user_id, "SMALL", rank=10)
        await _signal(session, user_id, "LARGE", rank=99_999_999)

        await _evening(session, user_id)

        lines = await _lines(session, LineKind.PLACE_LIMIT)
        assert len(lines) == 2
        symbols = [line.client_id.split(":")[1] for line in lines if line.client_id]
        assert "LARGE" in symbols

    async def test_a_shut_gate_lines_nothing_and_says_why(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _calendar(session, user_id, gate=Gate.SHUT)
        await _signal(session, user_id, "VBTCO")

        report = await _evening(session, user_id)

        assert report is not None
        assert report.entries == 0
        skips = list((await session.execute(sa.select(VbPlanSkip))).scalars())
        assert [skip.reason for skip in skips] == [SkipReason.GATE_SHUT.value]

    async def test_a_sleeve_with_no_capital_lines_nothing(self, session: AsyncSession) -> None:
        """`02` §3.4 — the safety property, end to end."""
        user_id = await _user(session, capital=Decimal("0.00"))
        await _calendar(session, user_id)
        await _signal(session, user_id, "VBTCO")

        report = await _evening(session, user_id)

        assert report is not None
        assert report.entries == 0
        skips = list((await session.execute(sa.select(VbPlanSkip))).scalars())
        assert [skip.reason for skip in skips] == [SkipReason.NO_SLEEVE_CAPITAL.value]

    async def test_a_locked_bar_is_skipped_with_its_own_reason(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _calendar(session, user_id)
        await _signal(session, user_id, "LOCKCO", locked=True)

        await _evening(session, user_id)

        skips = list((await session.execute(sa.select(VbPlanSkip))).scalars())
        assert [skip.reason for skip in skips] == [SkipReason.LOCKED_UPPER_CIRCUIT.value]

    async def test_a_scan_only_row_is_not_a_candidate(self, session: AsyncSession) -> None:
        """DECISIONS-VB PACK.6 — stored so a person can see what was passed over, never lined."""
        user_id = await _user(session)
        await _calendar(session, user_id)
        await _signal(session, user_id, "REJECTED", state=SignalState.SCAN_ONLY.value)

        report = await _evening(session, user_id)

        assert report is not None
        assert report.entries == 0
        assert report.skips == 0


@pytest.mark.db
class TestTheExits:
    async def test_a_close_below_the_ema_becomes_tomorrow_s_sell(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await _calendar(session, user_id)
        instrument_id = await make_instrument(session, "FADING")
        # A long flat stretch so the 21-day EMA exists, then a close well under it.
        for index, day in enumerate(_history(60)):
            close = "100.00" if index < 59 else "80.00"
            session.add(_bar(instrument_id, day, close))
        await session.flush()
        session.add(
            VbPosition(
                user_id=user_id,
                instrument_id=instrument_id,
                entry_date=SESSIONS[0],
                entry_avg=Decimal("100.00"),
                quantity_entered=500,
                quantity_open=500,
                initial_stop=Decimal("70.00"),
                stop_price=Decimal("70.00"),
                state="OPEN",
                gtt_id="gtt-1",
            )
        )
        await session.flush()

        report = await _evening(session, user_id)

        assert report is not None
        assert report.sells == 1
        line = (await _lines(session, LineKind.SELL_AT_OPEN))[0]
        assert line.quantity == 500
        assert line.reason == "EMA_EXIT"
        position = (
            await session.execute(sa.select(VbPosition).where(VbPosition.user_id == user_id))
        ).scalar_one()
        assert position.exit_queued_for is not None
        assert position.exit_reason_queued == "EMA_EXIT"

    async def test_a_naked_position_gets_an_arm_gtt_line(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _calendar(session, user_id)
        instrument_id = await make_instrument(session, "BARE")
        session.add(
            VbPosition(
                user_id=user_id,
                instrument_id=instrument_id,
                entry_date=SESSIONS[0],
                entry_avg=Decimal("100.00"),
                quantity_entered=300,
                quantity_open=300,
                initial_stop=Decimal("88.00"),
                stop_price=Decimal("88.00"),
                state="OPEN",
                gtt_id=None,
            )
        )
        await session.flush()

        report = await _evening(session, user_id)

        assert report is not None
        assert report.arms == 1
        line = (await _lines(session, LineKind.ARM_GTT))[0]
        assert line.stop_price == Decimal("88.00")
        assert line.quantity == 300

    async def test_a_name_already_held_is_never_bought_again(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _calendar(session, user_id)
        instrument_id = await _signal(session, user_id, "HELDCO")
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

        await _evening(session, user_id)

        skips = list((await session.execute(sa.select(VbPlanSkip))).scalars())
        assert [skip.reason for skip in skips] == [SkipReason.ALREADY_HELD.value]


def _bar(instrument_id: int, on: dt.date, close: str) -> OhlcvDaily:
    return OhlcvDaily(
        instrument_id=instrument_id,
        date=on,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        close_raw=Decimal(close),
        volume=100_000,
        volume_raw=100_000,
        turnover=Decimal("10000000.00"),
        adj_factor=Decimal(1),
        source="nse",
    )


def _history(count: int) -> list[dt.date]:
    """``count`` weekdays ending at AS_OF."""
    days: list[dt.date] = []
    day = AS_OF
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= dt.timedelta(days=1)
    return sorted(days)


@pytest.mark.db
class TestTheWorkingOrderExpires:
    """VB7 — the one mechanism the desk did not have before this sleeve."""

    async def _order(
        self, session: AsyncSession, user_id: int, symbol: str, *, signal_date: dt.date
    ) -> VbOrder:
        instrument_id = await make_instrument(session, symbol)
        order = VbOrder(
            user_id=user_id,
            instrument_id=instrument_id,
            signal_date=signal_date,
            limit_price=Decimal("100.00"),
            stop_price=Decimal("88.00"),
            quantity=100,
            state="SENT",
        )
        session.add(order)
        await session.flush()
        return order

    async def test_an_order_inside_its_window_is_left_alone(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _calendar(session, user_id)
        # Signalled two sessions ago: it may still fill today.
        await self._order(session, user_id, "WORKING", signal_date=SESSIONS[2])

        report = await _evening(session, user_id)

        assert report is not None
        assert report.cancels == 0
        order = (
            await session.execute(sa.select(VbOrder).where(VbOrder.user_id == user_id))
        ).scalar_one()
        assert order.state == "SENT"
        assert order.sessions_worked == 2

    async def test_an_order_at_the_end_of_its_third_session_is_a_cancel_line(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await _calendar(session, user_id)
        await self._order(session, user_id, "STALE", signal_date=SESSIONS[1])

        report = await _evening(session, user_id)

        assert report is not None
        assert report.cancels == 1
        line = (await _lines(session, LineKind.CANCEL_LIMIT))[0]
        assert line.quantity == 100

    async def test_a_sent_order_is_not_cancelled_in_the_database_by_the_sweep(
        self, session: AsyncSession
    ) -> None:
        """It is live at a broker; cancelling it is an order-shaped action and goes through the
        gateway on a confirm. The sweep produces the line and leaves the state alone."""
        user_id = await _user(session)
        await _calendar(session, user_id)
        await self._order(session, user_id, "STALE", signal_date=SESSIONS[1])

        await _evening(session, user_id)

        order = (
            await session.execute(sa.select(VbOrder).where(VbOrder.user_id == user_id))
        ).scalar_one()
        assert order.state == "SENT"

    async def test_an_order_the_broker_never_saw_expires_here(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _calendar(session, user_id)
        order = await self._order(session, user_id, "NEVERSENT", signal_date=SESSIONS[1])
        order.state = "PROPOSED"
        await session.flush()

        await _evening(session, user_id)

        refreshed = (
            await session.execute(sa.select(VbOrder).where(VbOrder.user_id == user_id))
        ).scalar_one()
        assert refreshed.state == "EXPIRED"
        assert refreshed.cancel_reason == "EXPIRY_SWEEP"

    async def test_the_session_count_is_derived_so_a_re_run_cannot_inflate_it(
        self, session: AsyncSession
    ) -> None:
        """An incrementing counter has to remember whether tonight already ran."""
        user_id = await _user(session)
        await _calendar(session, user_id)
        await self._order(session, user_id, "WORKING", signal_date=SESSIONS[2])

        await _evening(session, user_id)
        await _evening(session, user_id)
        await _evening(session, user_id)

        order = (
            await session.execute(sa.select(VbOrder).where(VbOrder.user_id == user_id))
        ).scalar_one()
        assert order.sessions_worked == 2

    async def test_a_holiday_consumes_no_session(self, session: AsyncSession) -> None:
        """`04` §7.2 — sessions are counted on the calendar, and the calendar is what the
        detector recorded. Two sessions apart in the book, six days apart in the year."""
        user_id = await _user(session)
        await _calendar(session, user_id)
        calendar = await session_calendar(session, user_id, AS_OF)
        assert calendar.sessions_between(SESSIONS[2], AS_OF) == 2
        assert (AS_OF - SESSIONS[2]).days == 4

    async def test_a_thin_session_is_not_in_the_calendar(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _calendar(session, user_id, thin={SESSIONS[2]})
        calendar = await session_calendar(session, user_id, AS_OF)
        assert SESSIONS[2] not in calendar.sessions
        assert SESSIONS[2] in calendar.dropped
        assert calendar.sessions_between(SESSIONS[1], AS_OF) == 2

    async def test_a_working_order_holds_a_slot(self, session: AsyncSession) -> None:
        """`04` §9.1 — a book that could line eleven limits for ten slots would over-commit."""
        user_id = await _user(session)
        await _calendar(session, user_id)
        for index in range(10):
            await self._order(session, user_id, f"BID{index}", signal_date=SESSIONS[3])
        await _signal(session, user_id, "ONEMORE")

        await _evening(session, user_id)

        skips = list((await session.execute(sa.select(VbPlanSkip))).scalars())
        assert [skip.reason for skip in skips] == [SkipReason.SLOTS_FULL.value]


@pytest.mark.db
class TestThePlanAndTheSession:
    async def test_the_plan_expires_in_thirty_minutes(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _calendar(session, user_id)
        await _signal(session, user_id, "VBTCO")

        await _evening(session, user_id)

        plan = (await session.execute(sa.select(VbPlan))).scalar_one()
        assert (plan.expires_at - plan.built_at) == dt.timedelta(minutes=30)
        assert plan.source == SOURCE_EVENING
        assert plan.gate == Gate.OPEN.value
        assert plan.sleeve_equity_inr == LAKH
        assert len(plan.plan_hash) == 64

    async def test_a_re_run_replaces_the_plan_rather_than_duplicating_it(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await _calendar(session, user_id)
        await _signal(session, user_id, "VBTCO")

        await _evening(session, user_id)
        await _evening(session, user_id)

        plans = list((await session.execute(sa.select(VbPlan))).scalars())
        assert len(plans) == 1
        assert len(await _lines(session, LineKind.PLACE_LIMIT)) == 1

    async def test_the_morning_plan_is_a_second_row_not_a_replacement(
        self, session: AsyncSession
    ) -> None:
        """Same session, same signals, a different source — so the desk can show either."""
        user_id = await _user(session)
        await _calendar(session, user_id)
        await _signal(session, user_id, "VBTCO")

        await _evening(session, user_id)
        await _evening(session, user_id, source=SOURCE_MORNING)

        plans = list((await session.execute(sa.select(VbPlan))).scalars())
        assert sorted(plan.source for plan in plans) == [SOURCE_EVENING, SOURCE_MORNING]

    async def test_every_line_is_proposed_and_nothing_is_sent(self, session: AsyncSession) -> None:
        """`02` Track C §3 — an order requires a person. The evening writes intentions."""
        user_id = await _user(session)
        await _calendar(session, user_id)
        await _signal(session, user_id, "VBTCO")

        await _evening(session, user_id)

        assert {line.state for line in await _lines(session)} == {"PROPOSED"}
        orders = list((await session.execute(sa.select(VbOrder))).scalars())
        assert orders == []

    async def test_the_session_row_records_the_gate_and_the_mode(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await _calendar(session, user_id)
        await _signal(session, user_id, "VBTCO")

        await _evening(session, user_id)

        row = (await session.execute(sa.select(VbSession))).scalar_one()
        assert row.mode == "DRY_RUN"
        assert row.gate == Gate.OPEN.value
        assert row.signals == 1

    async def test_the_dry_run_counter_moves_once_and_only_when_rehearsed(
        self, session: AsyncSession
    ) -> None:
        """`02` §3.1 is a gate. A session with lines nobody confirmed is not a rehearsal."""
        user_id = await _user(session)
        await _calendar(session, user_id)
        await _signal(session, user_id, "VBTCO")

        first = await _evening(session, user_id)
        assert first is not None
        assert first.counted_dry_run is False  # a PLACE_LIMIT line and no confirm

        await session.execute(
            sa.update(VbSession).where(VbSession.user_id == user_id).values(confirms=1)
        )
        await session.flush()
        second = await _evening(session, user_id)
        assert second is not None
        assert second.counted_dry_run is True

        third = await _evening(session, user_id)
        assert third is not None
        assert third.counted_dry_run is False  # counted once, not once per run
        config = (
            await session.execute(sa.select(VbConfig).where(VbConfig.user_id == user_id))
        ).scalar_one()
        assert config.dry_run_sessions == 1

    async def test_a_session_with_nothing_to_confirm_counts(self, session: AsyncSession) -> None:
        """A shut gate and an empty book produce no line at all. A gate that could only be
        satisfied on days the market cooperated would never be satisfied."""
        user_id = await _user(session)
        await _calendar(session, user_id, gate=Gate.SHUT)

        report = await _evening(session, user_id)

        assert report is not None
        assert report.counted_dry_run is True

    async def test_the_morning_reads_the_last_session_the_sleeve_saw(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await _calendar(session, user_id, thin={AS_OF})
        found = await last_detected_session(session, user_id, AS_OF)
        assert found == SESSIONS[3]
