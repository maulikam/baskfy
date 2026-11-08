"""FO12: the FO book's event alerts (``docs/fno/06`` FO12).

"Alerts: plan issued, exit done, hard exit tomorrow, ``LATE_EXIT``, a missing bhavcopy." The desk
writes the rows; ``baskfy_worker.fno.alerts`` reads them and raises each event **once** through
``dispatch``, dark unless ``BASKFY_FNO_MONITOR_ENABLED`` (the bhavcopy alert: with the ingest,
behind ``BASKFY_FNO_SCAN_ENABLED``). Each alert is rendered through a capturing mail transport.
The database half runs against the test Postgres in a rolled-back transaction, dated 2031.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Final

import pytest
from celery.schedules import crontab
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_api.email import Mailer, Message
from baskfy_api.settings import Settings
from baskfy_core.models import AppUser, Exchange, FoJournal, FoPlan, FoPosition, TradingDay
from baskfy_core.models.base import JsonObject
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_worker import ops
from baskfy_worker.alerts import Alert, AlertName, Severity, dispatch
from baskfy_worker.celery_app import BEAT_SCHEDULE
from baskfy_worker.fno import alerts as A
from baskfy_worker.settings import get_worker_settings
from baskfy_worker.tasks import celery_tasks

ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"
IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))
THU: Final = dt.date(2031, 7, 24)
FRI: Final = dt.date(2031, 7, 25)
MON: Final = dt.date(2031, 7, 28)
RUNBOOK: Final = "docs/runbooks/13-fno-book.md"
FNO_EVENTS: Final = (
    AlertName.FNO_PLAN,
    AlertName.FNO_EXIT,
    AlertName.FNO_HARD_EXIT_TOMORROW,
    AlertName.FNO_LATE_EXIT,
    AlertName.FNO_BHAVCOPY_MISSING,
)

requires_db = pytest.mark.db(
    pytest.mark.skipif(os.environ.get(ENV_VAR) is None, reason=f"{ENV_VAR} is not set")
)


def at(day: dt.date, hh: int, mm: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hh, mm), tzinfo=IST)


class Recording:
    """The transport Mailpit would be: every message the real ``Mailer`` hands it, kept."""

    def __init__(self) -> None:
        self.sent: list[Message] = []

    async def send(self, message: Message) -> None:
        self.sent.append(message)


class FakeRedis:
    """``SET NX`` over a dict: the one call the de-duplication makes."""

    def __init__(self, *, broken: bool = False) -> None:
        self.keys: dict[str, str] = {}
        self.broken = broken

    async def set(
        self, name: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        if self.broken:
            raise OSError("connection refused")
        assert nx and ex == A.MARKER_TTL_SECONDS
        if name in self.keys:
            return None
        self.keys[name] = value
        return True


def _settings() -> Settings:
    return Settings(ops_alert_email="ops@x.test", email_transport="console")


async def _mailed(alert: Alert) -> Message:
    sink = Recording()
    sent = await dispatch(alert, _settings(), mailer=Mailer(sink))
    assert "email" in str(sent["delivered_to"])
    (message,) = sink.sent
    return message


# --- the names --------------------------------------------------------------------------------


def test_the_five_names_are_stable_and_share_the_fno_runbook() -> None:
    assert [n.value for n in FNO_EVENTS] == [
        "FNO_PLAN", "FNO_EXIT", "FNO_HARD_EXIT_TOMORROW", "FNO_LATE_EXIT", "FNO_BHAVCOPY_MISSING",
    ]  # fmt: skip
    assert {ops.RUNBOOKS[n] for n in FNO_EVENTS} == {RUNBOOK}


# --- pure: one event, one alert, through the mail transport --------------------------------------


def _plan(**over: object) -> FoPlan:
    fields: dict[str, object] = {
        "id": 1, "user_id": 7, "plan_id": "F1N-20310725-1", "sleeve": "F1N", "symbol": "NIFTY",
        "trade_date": FRI, "structure": "IRON_CONDOR", "kind": "ENTRY", "sizing_mode": "BUDGET",
        "issued_at": at(FRI, 9, 20), "expires_at": at(FRI, 9, 50), "lots": 2, "lot_size": 75,
        "credit_points": Decimal("112.40"), "max_loss_inr": Decimal("24500.00"),
        "hard_exit_date": dt.date(2031, 8, 21), "status": "ISSUED", "detail": {},
    }  # fmt: skip
    fields.update(over)
    return FoPlan(**fields)


def _journal(**over: object) -> FoJournal:
    fields: dict[str, object] = {
        "position_id": 11, "user_id": 7, "sleeve": "F2", "symbol": "ZQA", "structure": "FUTURE",
        "opened_on": THU, "closed_on": FRI, "entry_inr": Decimal(0), "exit_inr": Decimal(0),
        "gross_pnl_inr": Decimal("3200"), "costs_inr": Decimal("180.50"),
        "net_pnl_inr": Decimal("3019.50"), "risk_budget_inr": Decimal("10000"),
        "r_multiple": Decimal("0.30"), "closed_reason": "STOP", "sessions_held": 1, "rolls": 0,
        "simulated": True, "sizing_mode": "BUDGET",
    }  # fmt: skip
    fields.update(over)
    return FoJournal(**fields)


def _position(**over: object) -> FoPosition:
    fields: dict[str, object] = {
        "id": 21, "user_id": 7, "sleeve": "F1B", "symbol": "BANKNIFTY", "structure": "IRON_CONDOR",
        "entry_plan_id": "F1B-20310701-1", "legs": {"legs": []}, "lots": 1, "lot_size": 35,
        "hard_exit_date": MON, "opened_at": at(THU, 9, 30), "simulated": True,
    }  # fmt: skip
    fields.update(over)
    return FoPosition(**fields)


async def test_a_plan_alert_carries_what_the_confirm_needs() -> None:
    alert = A.plan_alert(_plan())
    assert alert.name is AlertName.FNO_PLAN and alert.severity is Severity.WARNING
    message = await _mailed(alert)
    for text in (
        "F1N NIFTY IRON_CONDOR plan F1N-20310725-1 issued",
        "2 lot(s) of 75",
        "credit 112.40 pts",
        "max loss ₹24500.00",
        "hard exit 2031-08-21",
        "by 09:50 IST or it lapses",
        "FNO_PLAN",
        RUNBOOK,
    ):
        assert text in message.text, text


async def test_an_exit_alert_names_its_mode_and_is_in_rupees_and_r() -> None:
    message = await _mailed(A.exit_alert(_journal()))
    for text in ("F2 paper ZQA FUTURE closed (STOP) on 2031-07-25", "net ₹3019.50",
                 "₹180.50 costs", "0.30R", "FNO_EXIT"):  # fmt: skip
        assert text in message.text, text
    assert "F2 LIVE" in A.exit_alert(_journal(simulated=False)).summary


async def test_hard_exit_tomorrow_and_late_exit() -> None:
    notice = A.hard_exit_tomorrow_alert(_position(), MON, "hard exit")
    message = await _mailed(notice)
    assert "F1B paper BANKNIFTY IRON_CONDOR (position 21)" in message.text
    assert "hard exit is tomorrow, 2031-07-28" in message.text
    assert "FNO_HARD_EXIT_TOMORROW" in message.text
    late = A.late_exit_alert(_plan(), "open past the 15:00 hard exit", "2031-07-25T15:05:00+05:30")
    assert late.name is AlertName.FNO_LATE_EXIT and late.severity is Severity.CRITICAL
    assert "LATE_EXIT: open past the 15:00 hard exit" in (await _mailed(late)).text


async def test_a_missing_bhavcopy_alerts_and_nothing_else_does() -> None:
    result: JsonObject = {
        "trade_date": "2031-07-25",
        "ingest": {"trade_date": "2031-07-25", "status": "MISSING", "error": "ProviderError: 404"},
    }
    alert = A.bhavcopy_missing_alert(result)
    assert alert is not None and alert.name is AlertName.FNO_BHAVCOPY_MISSING
    assert alert.severity is Severity.CRITICAL
    message = await _mailed(alert)
    assert "F&O bhavcopy for 2031-07-25 is MISSING after the 23:30 attempt" in message.text
    for status in ("PENDING", "INGESTED"):
        assert A.bhavcopy_missing_alert({"ingest": {"status": status}}) is None
    assert A.bhavcopy_missing_alert({"skipped": "not an NSE trading day"}) is None


# --- the tasks: dark, and scheduled ---------------------------------------------------------------


def test_the_alerts_task_is_dark_behind_the_monitor_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BASKFY_FNO_MONITOR_ENABLED", raising=False)
    get_worker_settings.cache_clear()
    try:
        out = celery_tasks.fno_alerts_task.run(at="2031-07-25T09:25:00+05:30")
    finally:
        get_worker_settings.cache_clear()
    assert out["skipped"] == "BASKFY_FNO_MONITOR_ENABLED is false"


def test_beat_runs_the_alerts_every_five_minutes_on_weekdays() -> None:
    entry = BEAT_SCHEDULE["fno-alerts"]
    assert entry["task"] == celery_tasks.FNO_ALERTS_TASK == "baskfy.fno.alerts"
    schedule = entry["schedule"]
    assert isinstance(schedule, crontab)
    assert schedule.minute == set(range(0, 60, 5))
    assert schedule.hour == set(range(9, 24))
    assert schedule.day_of_week == {1, 2, 3, 4, 5}


def test_the_2330_missing_night_dispatches_from_the_ingest_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing: JsonObject = {
        "trade_date": "2031-07-25",
        "ingest": {"trade_date": "2031-07-25", "status": "MISSING", "error": "no file"},
    }
    sent: list[Alert] = []

    async def night(*_: object, **__: object) -> JsonObject:
        return dict(missing)

    async def record(alert: Alert, *_: object, **__: object) -> JsonObject:
        sent.append(alert)
        return {"delivered_to": ["log"]}

    def in_session(fn: Callable[[None], Coroutine[object, object, JsonObject]]) -> JsonObject:
        return asyncio.run(fn(None))

    monkeypatch.setenv("BASKFY_FNO_SCAN_ENABLED", "true")
    monkeypatch.setattr(celery_tasks, "build_nse_provider", lambda *_, **__: object())
    monkeypatch.setattr(celery_tasks, "fno_run_night", night)
    monkeypatch.setattr(celery_tasks, "dispatch", record)
    monkeypatch.setattr(celery_tasks, "run_in_session", in_session)
    get_worker_settings.cache_clear()
    try:
        out = celery_tasks.fno_ingest_bhavcopy_task.run(
            trade_date="2031-07-25", at="2031-07-25T23:30:00+05:30"
        )
        missing["ingest"] = {"trade_date": "2031-07-25", "status": "PENDING"}
        pending = celery_tasks.fno_ingest_bhavcopy_task.run(
            trade_date="2031-07-25", at="2031-07-25T22:30:00+05:30"
        )
    finally:
        get_worker_settings.cache_clear()
    assert [a.name for a in sent] == [AlertName.FNO_BHAVCOPY_MISSING]
    assert "alert" in out and "alert" not in pending


# --- the database: today's events, once each ------------------------------------------------------


@asynccontextmanager
async def _rolled_back() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(os.environ[ENV_VAR])
    session = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


async def _seed(session: AsyncSession) -> int:
    user = AppUser(public_id="fo12alerts01", email="fo12-alerts@example.com", name="FO12")
    session.add(user)
    await session.flush()
    uid = int(user.id)
    await session.execute(
        insert(Exchange).values(id=NSE_EXCHANGE_ID, code="NSE").on_conflict_do_nothing()
    )
    await session.execute(
        insert(TradingDay)
        .values(
            [
                {"exchange_id": NSE_EXCHANGE_ID, "date": d, "is_trading_day": True,
                 "source": "derived"}
                for d in (THU, FRI, MON)
            ]
        )
        .on_conflict_do_nothing()
    )  # fmt: skip
    late = {"violations": [{"code": "LATE_EXIT", "message": "open past 15:00",
                            "at": "2031-07-25T15:05:00+05:30"}]}  # fmt: skip
    old_late = {"violations": [{"code": "LATE_EXIT", "message": "old",
                                "at": "2031-07-24T15:05:00+05:30"}]}  # fmt: skip
    session.add_all(
        [
            _plan(id=None, user_id=uid, plan_id="fo12-issued"),
            _plan(id=None, user_id=uid, plan_id="fo12-refused", status="REJECTED_MARGIN"),
            _plan(id=None, user_id=uid, plan_id="fo12-roll", kind="ROLL"),
            _plan(id=None, user_id=uid, plan_id="fo12-yesterday", trade_date=THU,
                  issued_at=at(THU, 9, 20), expires_at=at(THU, 9, 50)),
            _plan(id=None, user_id=uid, plan_id="fo12-late", status="CLOSED",
                  issued_at=at(THU, 9, 20), trade_date=THU, detail=late),
            _plan(id=None, user_id=uid, plan_id="fo12-old-late", status="CLOSED",
                  issued_at=at(THU, 9, 20), trade_date=THU, detail=old_late),
        ]
    )  # fmt: skip
    await session.flush()
    tomorrow = _position(id=None, user_id=uid)
    f2 = _position(id=None, user_id=uid, sleeve="F2", symbol="ZQA", structure="FUTURE",
                   entry_plan_id="fo12-f2", hard_exit_date=None,
                   legs={"legs": [], "carry": {"time_exit_date": "2031-07-28"}})  # fmt: skip
    later = _position(id=None, user_id=uid, entry_plan_id="fo12-later",
                      hard_exit_date=dt.date(2031, 8, 21))  # fmt: skip
    closed = _position(id=None, user_id=uid, entry_plan_id="fo12-closed",
                       closed_at=at(FRI, 15, 1), closed_reason="STOP")  # fmt: skip
    session.add_all([tomorrow, f2, later, closed])
    await session.flush()
    session.add_all(
        [
            _journal(user_id=uid, position_id=closed.id),
            _journal(user_id=uid, position_id=later.id, closed_on=THU),
        ]
    )
    await session.flush()
    return uid


@requires_db
class TestTodaysEvents:
    async def test_the_morning_raises_the_plan_the_exit_and_the_late_exit(self) -> None:
        async with _rolled_back() as session:
            uid = await _seed(session)
            events = await A.pending_events(session, uid, at(FRI, 9, 25))
            names = sorted((e.alert.name.value, e.marker.split(":")[-1]) for e in events)
            # The issued entry only: not the refused one, not the roll, not yesterday's. The
            # journal row closed today, not yesterday's. The LATE_EXIT recorded today. No hard
            # exit notice before 18:00.
            assert [n for n, _ in names] == ["FNO_EXIT", "FNO_LATE_EXIT", "FNO_PLAN"]
            plan = next(e for e in events if e.alert.name is AlertName.FNO_PLAN)
            assert plan.marker == f"fno:alert:plan:{uid}:fo12-issued"

    async def test_the_evening_names_every_open_structure_exiting_next_session(self) -> None:
        async with _rolled_back() as session:
            uid = await _seed(session)
            events = await A.pending_events(session, uid, at(FRI, 18, 5))
            hard = [e.alert for e in events if e.alert.name is AlertName.FNO_HARD_EXIT_TOMORROW]
            # Friday's next session is Monday: F1B's hard exit and F2's time exit; the one
            # exiting in August and the closed one are not named.
            assert sorted(str(a.detail["which"]) for a in hard) == ["hard exit", "time exit"]
            assert all("tomorrow, 2031-07-28" in a.summary for a in hard)

    async def test_each_event_is_delivered_once(self) -> None:
        async with _rolled_back() as session:
            uid = await _seed(session)
            cache = FakeRedis()
            sink = Recording()
            first = await A.run_fno_alerts(
                session, [uid], cache, at(FRI, 18, 5), settings=_settings(), mailer=Mailer(sink)
            )
            second = await A.run_fno_alerts(
                session, [uid], cache, at(FRI, 18, 10), settings=_settings(), mailer=Mailer(sink)
            )
            sent = first["sent"]
            assert isinstance(sent, list) and len(sent) == 5
            assert second["sent"] == [] and second["events"] == 5
            assert len(sink.sent) == 5

    async def test_redis_down_delivers_rather_than_silences(self) -> None:
        async with _rolled_back() as session:
            uid = await _seed(session)
            out = await A.run_fno_alerts(
                session, [uid], FakeRedis(broken=True), at(FRI, 9, 25),
                settings=_settings(), mailer=Mailer(Recording()),
            )  # fmt: skip
            sent = out["sent"]
            assert isinstance(sent, list) and len(sent) == 3
