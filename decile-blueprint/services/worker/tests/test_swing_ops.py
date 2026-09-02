"""SW11 — the swing book's four in-process alert checks (STANDING-ANSWERS B8) and the facts
they read (`baskfy_api.swing_health`), against a real database.

The 15:15 sweep itself is the desk's (`kite-momentum-rebalancer/app/swing_clock.py`, its own
suite); what the worker owns is the *assertion* behind `SWING_GTT_MISSING_AT_1515` — no filled
quantity without a GTT after the sweep — and the alert it raises when the assertion fails.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import cast

import pytest
import sqlalchemy as sa
from celery.schedules import crontab
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.email import Mailer, Message
from baskfy_api.settings import Settings
from baskfy_api.swing_health import IST, read_swing_health
from baskfy_core.models import (
    AppUser,
    PipelineRun,
    SwConfig,
    SwMarketDaily,
    SwPlan,
    SwPlanLine,
    SwPosition,
    SwSession,
    TradingDay,
)
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_worker.alerts import AlertName
from baskfy_worker.celery_app import BEAT_SCHEDULE, QUEUE_DEFAULT, TASK_ROUTES
from baskfy_worker.tasks.swing_ops import (
    RUNBOOK,
    check_detect_fresh,
    check_gtt_at_1515,
    check_monitor_started,
    check_orders_after_cutoff,
)

pytestmark = [requires_db, pytest.mark.db]

DAY = dt.date(2026, 9, 3)  # a Thursday


def _at(hhmm: str, day: dt.date = DAY) -> dt.datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST)


def _settings(**overrides: object) -> Settings:
    base = Settings(environment="test", log_json=False, log_level="CRITICAL")
    return base.model_copy(update=dict(overrides))


class Recording:
    def __init__(self) -> None:
        self.sent: list[Message] = []

    async def send(self, message: Message) -> None:
        self.sent.append(message)


async def _user(session: AsyncSession) -> int:
    user = AppUser(public_id="sw11-user", email="sw11@example.com")
    session.add(user)
    await session.flush()
    session.add(SwConfig(user_id=user.id, updated_by="test"))
    await session.flush()
    return int(user.id)


async def _position(
    session: AsyncSession,
    *,
    user_id: int,
    symbol: str,
    gtt_id: str | None,
    quantity_open: int = 100,
) -> int:
    instrument_id = await make_instrument(session, symbol)
    row = SwPosition(
        user_id=user_id,
        instrument_id=instrument_id,
        setup="FLAG",
        entry_date=DAY,
        entry_avg=Decimal("100.0000"),
        quantity_entered=100,
        initial_stop=Decimal("96.00"),
        stop=Decimal("96.00"),
        gtt_id=gtt_id,
        trail="MA20",
        partial_done=False,
        quantity_open=quantity_open,
        state="OPEN" if quantity_open else "CLOSED",
        simulated=True,
    )
    session.add(row)
    await session.flush()
    return int(row.id)


async def _sent_buy(
    session: AsyncSession, *, user_id: int, symbol: str, state: str = "SENT"
) -> None:
    instrument_id = await make_instrument(session, symbol)
    plan = SwPlan(
        plan_id=f"00000000-0000-0000-0000-0000000000{len(symbol):02d}",
        user_id=user_id,
        as_of=DAY,
        source="SIGNAL",
        built_at=_at("09:31"),
        expires_at=_at("10:01"),
        plan_hash="x",
        gate="GREEN",
        exposure_level=1,
    )
    session.add(plan)
    await session.flush()
    session.add(
        SwPlanLine(
            plan_id=plan.id,
            user_id=user_id,
            kind="BUY_ON_TRIGGER",
            instrument_id=instrument_id,
            setup="FLAG",
            quantity=100,
            trigger=Decimal("100.00"),
            stop=Decimal("96.00"),
            state=state,
            client_id=f"{plan.plan_id}:{symbol}:BUY_ON_TRIGGER",
            journal_ref="240903000001",
        )
    )
    await session.flush()


async def _published(session: AsyncSession, trade_date: dt.date) -> None:
    session.add(
        PipelineRun(
            trade_date=trade_date,
            status="succeeded",
            started_at=_at("19:00", trade_date),
            finished_at=_at("19:30", trade_date),
            data_version=int(trade_date.strftime("%Y%m%d")),
        )
    )
    await session.flush()


# --- the 15:15 sweep's assertion --------------------------------------------------------------


class TestTheSweepAssertionAt1515:
    async def test_the_1515_sweep_check_raises_swing_gtt_missing_for_a_naked_position(
        self, session: AsyncSession
    ) -> None:
        """A8: no filled quantity without a GTT. One naked, one covered, one closed → the
        alert names one, with the runbook, through the mailer."""
        user_id = await _user(session)
        await _position(session, user_id=user_id, symbol="NAKED", gtt_id=None)
        await _position(session, user_id=user_id, symbol="COVERED", gtt_id="DRY-1")
        await _position(session, user_id=user_id, symbol="DONE", gtt_id=None, quantity_open=0)
        mailer = Recording()

        result = await check_gtt_at_1515(
            session,
            now=_at("15:20"),
            settings=_settings(ops_alert_email="ops@example.com"),
            mailer=Mailer(mailer),
        )

        assert result["naked"] == 1
        alert = cast(dict[str, object], result["alert"])
        assert alert["alert"] == AlertName.SWING_GTT_MISSING_AT_1515.value
        assert alert["runbook"] == RUNBOOK and "email" in cast(list[str], alert["delivered_to"])
        assert "1 swing position(s)" in mailer.sent[0].subject

    async def test_the_1515_sweep_check_is_silent_when_every_position_has_a_gtt(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await _position(session, user_id=user_id, symbol="COVERED", gtt_id="DRY-1")
        result = await check_gtt_at_1515(session, now=_at("15:20"), settings=_settings())
        assert result == {"date": DAY.isoformat(), "checked": True, "naked": 0, "alert": None}

    async def test_the_1515_sweep_check_does_not_run_on_a_saturday(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await _position(session, user_id=user_id, symbol="NAKED", gtt_id=None)
        result = await check_gtt_at_1515(session, now=_at("15:20", dt.date(2026, 9, 5)))
        assert result["checked"] is False


# --- the other three ------------------------------------------------------------------------


class TestTheMonitorCheck:
    async def test_it_alerts_at_0920_with_the_flag_on_and_no_monitor_ran(
        self, session: AsyncSession
    ) -> None:
        await _user(session)
        result = await check_monitor_started(
            session, now=_at("09:20"), monitor_enabled=True, settings=_settings()
        )
        alert = cast(dict[str, object], result["alert"])
        assert alert["alert"] == AlertName.SWING_MONITOR_DID_NOT_START.value
        assert alert["labels"] == {"trade_date": DAY.isoformat()}

    async def test_it_is_silent_once_the_monitor_has_written_its_row(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        session.add(
            SwSession(
                user_id=user_id,
                session_date=DAY,
                mode="DRY_RUN",
                monitor_ran=True,
                notes="swing-monitor",
            )
        )
        await session.flush()
        result = await check_monitor_started(
            session, now=_at("09:20"), monitor_enabled=True, settings=_settings()
        )
        assert result["alert"] is None

    async def test_it_does_nothing_with_the_flag_off(self, session: AsyncSession) -> None:
        result = await check_monitor_started(session, now=_at("09:20"), monitor_enabled=False)
        assert result["checked"] is False and result["reason"] == "monitor flag is off"


class TestTheCutoffCheck:
    async def test_it_alerts_for_a_buy_still_sent_after_1045(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _sent_buy(session, user_id=user_id, symbol="OPEN")
        await _sent_buy(session, user_id=user_id, symbol="FILLEDONE", state="FILLED")
        result = await check_orders_after_cutoff(session, now=_at("10:50"), settings=_settings())
        assert result["open_orders"] == 1
        alert = cast(dict[str, object], result["alert"])
        assert alert["alert"] == AlertName.SWING_ORDER_OPEN_AFTER_CUTOFF.value

    async def test_it_is_silent_when_nothing_is_open(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _sent_buy(session, user_id=user_id, symbol="FILLEDONE", state="FILLED")
        result = await check_orders_after_cutoff(session, now=_at("10:50"), settings=_settings())
        assert result["alert"] is None


class TestTheDetectCheck:
    async def test_it_alerts_when_the_published_date_has_no_market_row(
        self, session: AsyncSession
    ) -> None:
        await _user(session)
        await _published(session, DAY)
        result = await check_detect_fresh(session, now=_at("21:30"), settings=_settings())
        alert = cast(dict[str, object], result["alert"])
        assert alert["alert"] == AlertName.SWING_DETECT_STALE.value
        assert alert["labels"] == {"trade_date": DAY.isoformat()}

    async def test_it_is_silent_when_detect_wrote_the_row(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _published(session, DAY)
        session.add(
            SwMarketDaily(
                user_id=user_id,
                date=DAY,
                constituent_count=1,
                pct_up_strong_1m=Decimal(0),
                gate="RED",
                exposure_level=0,
                max_open_positions=2,
                max_exposure_pct=Decimal("25.00"),
                new_entries_allowed=False,
                parabolic_count=0,
                detail={},
            )
        )
        await session.flush()
        result = await check_detect_fresh(session, now=_at("21:30"), settings=_settings())
        assert result["alert"] is None and result["published"] == DAY.isoformat()

    async def test_nothing_published_is_nothing_to_detect(self, session: AsyncSession) -> None:
        result = await check_detect_fresh(session, now=_at("21:30"), settings=_settings())
        assert result["alert"] is None and result["published"] is None


class TestTheFactsTheGaugesRead:
    async def test_read_swing_health_carries_all_five_facts(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _position(session, user_id=user_id, symbol="NAKED", gtt_id=None)
        await _sent_buy(session, user_id=user_id, symbol="OPEN")
        await _published(session, DAY)
        health = await read_swing_health(session, now=_at("11:00"))
        assert health.naked_positions == 1
        assert health.monitor_ran_today is False
        assert health.open_buy_orders_today == 1
        assert health.detect_ran_for_published_date is False
        assert (health.published_date, health.today) == (DAY, DAY)


class TestTheBeatEntries:
    @pytest.mark.parametrize(
        ("entry", "task", "hhmm"),
        [
            ("swing-check-monitor-started", "baskfy.swing.check_monitor_started", "09:20"),
            ("swing-check-orders-after-cutoff", "baskfy.swing.check_orders_after_cutoff", "10:50"),
            ("swing-check-gtt-at-1515", "baskfy.swing.check_gtt_at_1515", "15:20"),
            ("swing-check-detect-fresh", "baskfy.swing.check_detect_fresh", "21:30"),
        ],
    )
    def test_each_check_runs_at_the_moment_its_rule_is_about(
        self, entry: str, task: str, hhmm: str
    ) -> None:
        hour, minute = (int(x) for x in hhmm.split(":"))
        row = BEAT_SCHEDULE[entry]
        schedule = cast(crontab, row["schedule"])
        assert row["task"] == task
        assert (schedule.hour, schedule.minute) == ({hour}, {minute})
        assert schedule.day_of_week == {1, 2, 3, 4, 5}
        assert row["options"] == {"queue": QUEUE_DEFAULT}
        assert TASK_ROUTES["baskfy.swing.check_*"] == {"queue": QUEUE_DEFAULT}


class TestHolidays:
    async def test_an_nse_holiday_on_a_weekday_keeps_the_intraday_checks_quiet(
        self, session: AsyncSession
    ) -> None:
        """The calendar names the day a holiday: the 15:15 sweep check, the cutoff check and
        the monitor check all say why they did not look; the evening's detect check is not
        a session question and still runs."""
        user_id = await _user(session)
        await _position(session, user_id=user_id, symbol="NAKED", gtt_id=None)
        holiday = dt.date(2026, 9, 4)  # a Friday; the seeded calendar carries it as a session
        await session.execute(
            sa.update(TradingDay)
            .where(TradingDay.exchange_id == NSE_EXCHANGE_ID, TradingDay.date == holiday)
            .values(is_trading_day=False, source="holiday")
        )
        await session.flush()
        at = _at("15:20", holiday)
        assert (await check_gtt_at_1515(session, now=at))["reason"] == "an NSE holiday"
        assert (await check_orders_after_cutoff(session, now=at))["reason"] == "an NSE holiday"
        assert (await check_monitor_started(session, now=at, monitor_enabled=True))[
            "reason"
        ] == "an NSE holiday"
        # A day the calendar does not carry is still checked — silence needs a named holiday.
        result = await check_gtt_at_1515(session, now=_at("15:20"), settings=_settings())
        assert result["checked"] is True and result["naked"] == 1
